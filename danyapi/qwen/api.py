from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from .. import tools as toolemu
from ..accounts import account_lock
from ..config import settings
from ..deepseek.stream import IncrementalSSE

# Shared retry constants — imported as local names so monkeypatch in tests works.
from ..retry import (
    MAX_RETRIES,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_BACKOFF_SEC,
    _drop_session,
    _is_retryable_http,
)
from ..tooltrace import ToolTraceLogger
from ..tooltrace import classify as _classify_trace
from .client import QwenError
from .stream import QwenStreamReconstructor, error_code

log = logging.getLogger("danyapi.qwen.api")


IMAGE_CHAT_MODEL = "qwen3.8-max"


RETRYABLE_ERROR_CODES = {
    "Too_Many_Requests",
    "RateLimited",
    "quotaLimited",
    "Internal_Server_Error",
    "Server_Busy",
    "server_busy",
    "Busy",
    "busy",
}

AUTH_ERROR_CODES = {
    "unauthorized",
    "Unauthorized",
    "Invalid token",
    "Forbidden",
    "forbidden",
}

RATE_LIMIT_ERROR_CODES = {
    "Too_Many_Requests",
    "RateLimited",
    "quotaLimited",
}

# Error codes that mean the account's daily quota is used up, not a transient
# throttle. Retrying the same account is pointless and only delays the failover.
QUOTA_ERROR_CODES = {
    "quotaLimited",
    "quotaExhausted",
    "QuotaExhausted",
}

# Qwen reports an exhausted image quota as plain answer text rather than an error
# code, e.g. "you've used up your daily image generation quota". Match on whole
# phrases so an ordinary answer mentioning "quota" is never mistaken for it.
QUOTA_TEXT_MARKERS = (
    "used up your daily",
    "daily image generation quota",
    "image generation quota",
    "daily quota",
    "quota exhausted",
    "quota exceeded",
)

# Qwen also reports transient service failures as plain answer text: "there was an
# issue connecting to Qwen3.8-Omni-Flash" and "the service is experiencing high load,
# please try again later". Unlike a quota exhaustion these pass on their own, so the
# request is retried instead of failing. Detection only runs when the response has no
# images, which is exactly the failure shape, so an ordinary answer cannot match.
TRANSIENT_TEXT_MARKERS = (
    "issue connecting",
    "проблема подключения",
    "unexpected error",
    "непредвиденная ошибка",
    "try again later",
    "please try again",
    "повторите попытку",
    "попробуйте позже",
    "high load",
    "высокая нагрузка",
    "service is experiencing",
    "сервис испытывает",
    "temporarily unavailable",
    "временно недоступ",
)

CONTEXT_LIMIT_MARKERS = ("context", "maxinput", "toolong", "lengthexceeded", "tokenlimit")


class ContextLimitError(Exception):
    pass


class QuotaExhausted(HTTPException):
    """The account's (daily) quota is used up, so it must leave the rotation."""

    def __init__(self, detail: str) -> None:
        super().__init__(429, detail)


def _is_quota_error_code(code) -> bool:
    return isinstance(code, str) and code in QUOTA_ERROR_CODES


def _quota_text(content: str) -> str | None:
    """Return the matching quota phrase when ``content`` says quota is gone."""
    if not isinstance(content, str) or not content:
        return None
    lowered = content.lower()
    for marker in QUOTA_TEXT_MARKERS:
        if marker in lowered:
            return marker
    return None


def _is_quota_exhausted(rec: QwenStreamReconstructor) -> str | None:
    """Detect an exhausted quota in either of the two shapes Qwen sends it.

    Returns a human-readable reason when the response is a quota exhaustion,
    otherwise ``None``. A response with images is never treated as exhausted.
    """
    if rec.image_urls:
        return None
    code = error_code(rec.error)
    if _is_quota_error_code(code):
        return rec.error.get("details") or rec.error.get("message") or "quota exhausted"
    if not rec.has_content:
        return None
    return _quota_text(rec.content)


def _is_transient_text(rec: QwenStreamReconstructor) -> str | None:
    """Detect a transient service failure Qwen reports as plain answer text.

    "There was an issue connecting" and "the service is experiencing high load" are
    not quota problems: they pass on their own, so the request is retried. Only a
    response without images can be a failure — when images are present the text is a
    caption, not an error.
    """
    if rec.image_urls or not rec.content:
        return None
    lowered = rec.content.lower()
    for marker in TRANSIENT_TEXT_MARKERS:
        if marker in lowered:
            return marker
    return None


def _is_context_limit_code(code) -> bool:
    if not isinstance(code, str) or not code:
        return False
    compact = "".join(ch for ch in code.lower() if ch.isalnum())
    return any(marker in compact for marker in CONTEXT_LIMIT_MARKERS)


def _is_context_limit(rec: QwenStreamReconstructor) -> bool:
    return _is_context_limit_code(error_code(rec.error))


def _error_status(code) -> int:
    if code in RATE_LIMIT_ERROR_CODES:
        return 429
    if code in AUTH_ERROR_CODES:
        return 401
    return 502


def _handle_account_error(account, exc: Exception) -> None:
    code = getattr(exc, "code", None)
    if code in AUTH_ERROR_CODES:
        account.mark_broken()
        log.warning("qwen account #%d auth error %s: %s", account.index, code, exc)
    else:
        log.warning("qwen account #%d error: %s", account.index, exc)


def _mark_quota_exhausted(account, pool, session_key: str, reason: str) -> None:
    """Drop a quota-exhausted account from the rotation and forget its session.

    The account comes back on its own once ``settings.qwen_quota_cooldown``
    elapses; the pinned image chat is dropped because Qwen's quota is per
    account/day and the session cannot serve a request until it resets.
    """
    account.mark_quota_exhausted(settings.qwen_quota_cooldown)
    _drop_session(pool, account, session_key)
    log.warning("qwen account #%d quota exhausted: %s", account.index, reason)


async def _prepare_session(account, pool, existing_sid: str | None, model_id: str, context_seq: tuple[str, ...] | None = None, chat_type: str = "t2t"):
    try:
        session, session_key = await account.sessions.obtain(existing_sid, model_id, chat_type=chat_type)
    except QwenError as exc:
        _handle_account_error(account, exc)
        raise HTTPException(_error_status(exc.code), f"Qwen error: {exc}") from exc
    if session_key != existing_sid:
        pool.register(account.index, session_key)
        if existing_sid:
            pool.forget(existing_sid)
            pool.forget_context(existing_sid)
            account.sessions.forget(existing_sid)
    if context_seq:
        pool.index_context(session_key, context_seq)
    return session, session_key


async def _send_completion(
    account,
    session,
    prompt: str,
    model_id: str,
    thinking: bool,
    search: bool,
    chat_type: str = "t2t",
    image_model: str | None = None,
    image_size: str | None = None,
):
    try:
        resp = await account.client.completion(
            chat_session_id=session.id,
            prompt=prompt,
            parent_message_id=session.last_response_id,
            model=model_id,
            thinking=thinking,
            search=search,
            chat_type=chat_type,
            image_model=image_model,
            image_size=image_size,
        )
    except QwenError as exc:
        # A refresh refused by the auth host arrives as QwenError, not httpx, so
        # without this branch a revoked credential would surface as a bare 500
        # and the account would stay "healthy" in /health while every request
        # fails. Mark it broken so the pool and the monitor see the real state.
        _handle_account_error(account, exc)
        raise HTTPException(_error_status(exc.code), f"Qwen error: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:500]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Qwen request failed: {exc}") from exc

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        raise HTTPException(resp.status_code, body[:500].decode("utf-8", errors="replace"))

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        body = await resp.aread()
        await resp.aclose()
        text = body[:500].decode("utf-8", errors="replace")
        if "text/html" in content_type or b"requestInfo" in body:
            raise HTTPException(502, "Qwen WAF challenge: request blocked by anti-bot, try again later")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(502, f"Qwen request failed: {text}") from None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                if _is_context_limit_code(code):
                    raise ContextLimitError() from None
                raise HTTPException(_error_status(code), error.get("message") or error.get("details") or "Qwen error")
            data = payload.get("data")
            if isinstance(data, dict) and data.get("code"):
                if _is_context_limit_code(data["code"]):
                    raise ContextLimitError() from None
                raise HTTPException(_error_status(data["code"]), data.get("details") or data.get("message") or "Qwen error")
        raise HTTPException(502, f"Qwen request failed: {text}")
    return resp


def _is_retryable_error(rec: QwenStreamReconstructor) -> bool:
    return bool(rec.error and error_code(rec.error) in RETRYABLE_ERROR_CODES and not rec.has_content)


def _error_body(rec: QwenStreamReconstructor) -> str:
    err = rec.error or {}
    return json.dumps(
        {
            "error": {
                "message": err.get("details") or err.get("message") or "Qwen server error, try again later",
                "code": err.get("code"),
            }
        },
        ensure_ascii=False,
    )


async def _try_stop_stream(client, session_id: str, message_id: str | None) -> None:
    if not session_id or not message_id:
        return
    try:
        await client.stop_stream(session_id, message_id)
    except Exception as exc:
        log.debug("stop_stream failed for %s: %s", session_id, exc)


async def _human_delay() -> None:
    delay = random.uniform(settings.human_delay_min, settings.human_delay_max)
    if delay > 0:
        await asyncio.sleep(delay)


def _trace_parse(
    trace: ToolTraceLogger | None,
    request_id: str | None,
    content: str,
    tool_schemas: Any,
    session_key: str | None = None,
    model: str | None = None,
) -> None:
    if trace is None:
        return
    parsed = toolemu.parse_tool_calls(content, tool_schemas)
    issue, detail = _classify_trace(content, tool_schemas)
    result = "ok" if parsed is not None else "unparsed"
    if issue != "ok":
        result = f"{result}:{issue}"
    trace.parse(
        request_id,
        result=result,
        issue=detail if issue != "ok" else None,
        session_id=session_key,
        model=model,
    )


def _accumulate_usage(session, rec: QwenStreamReconstructor) -> dict:
    current = rec.usage_tokens
    prev_input = int(getattr(session, "accumulated_input_tokens", 0) or 0)
    prev_output = int(getattr(session, "accumulated_output_tokens", 0) or 0)
    input_tokens = prev_input + current["prompt_tokens"]
    output_tokens = prev_output + current["completion_tokens"]
    session.accumulated_input_tokens = input_tokens
    session.accumulated_output_tokens = output_tokens
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


async def collect_non_stream(
    account,
    pool,
    existing_sid,
    lock,
    prompt,
    model,
    model_id,
    thinking,
    search,
    tool_mode=False,
    tool_schemas=None,
    include_usage=False,
    context_seq: tuple[str, ...] | None = None,
    request_id: str | None = None,
    trace: ToolTraceLogger | None = None,
):
    await _human_delay()
    async with account_lock(lock, settings.acquire_timeout):
        session, session_key = await _prepare_session(account, pool, existing_sid, model_id, context_seq)
        if trace is not None:
            trace.dany(request_id, prompt=prompt, session_id=session_key, model=model)
        stop_response_id: str | None = None
        try:
            rec: QwenStreamReconstructor | None = None
            for attempt in range(MAX_RETRIES + 1):
                if attempt:
                    await asyncio.sleep(min(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX_SEC))
                try:
                    resp = await _send_completion(account, session, prompt, model_id, thinking, search)
                except ContextLimitError:
                    _drop_session(pool, account, session_key)
                    raise HTTPException(400, "context length exceeded: conversation too long, start a new conversation") from None
                except HTTPException as exc:
                    if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                        log.warning(
                            "qwen provider error (%s), attempt %d/%d",
                            exc.status_code,
                            attempt + 1,
                            MAX_RETRIES,
                        )
                        continue
                    raise
                rec = QwenStreamReconstructor()
                incremental = IncrementalSSE()
                try:
                    async for chunk in resp.aiter_bytes():
                        for event in incremental.feed(chunk):
                            rec.handle(event)
                    for event in incremental.finish():
                        rec.handle(event)
                finally:
                    if rec.response_id:
                        stop_response_id = rec.response_id
                    await resp.aclose()
                quota_reason = _is_quota_exhausted(rec)
                if quota_reason is not None:
                    _mark_quota_exhausted(account, pool, session_key, quota_reason)
                    raise QuotaExhausted(f"quota exhausted: {quota_reason}")
                if _is_retryable_error(rec) and attempt < MAX_RETRIES:
                    log.warning(
                        "qwen retryable error (%s), attempt %d/%d",
                        error_code(rec.error),
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                break
        except asyncio.CancelledError:
            await _try_stop_stream(account.client, session.id, stop_response_id)
            raise

        assert rec is not None
        if _is_context_limit(rec) and not rec.has_content:
            _drop_session(pool, account, session_key)
            raise HTTPException(400, "context length exceeded: conversation too long, start a new conversation")
        usage = _accumulate_usage(session, rec)
        account.sessions.touch_last_message(session_key, rec.response_id)

        if not rec.has_content and rec.error:
            raise HTTPException(_error_status(error_code(rec.error)), _error_body(rec))

        if tool_mode and trace is not None:
            trace.upstream(request_id, content=rec.content, reasoning=rec.reasoning or None, session_id=session_key, model=model)
        if tool_mode:
            parsed = toolemu.parse_tool_calls(rec.content, tool_schemas)
            if parsed is not None:
                tool_calls, tool_text = parsed
                message = toolemu.format_tool_message(tool_calls, tool_text, rec.reasoning)
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": rec.content}
                if rec.reasoning:
                    message["reasoning_content"] = rec.reasoning
                finish = "stop"
        else:
            message = {"role": "assistant", "content": rec.content}
            if rec.reasoning:
                message["reasoning_content"] = rec.reasoning
            finish = "stop"
        if tool_mode and trace is not None:
            _trace_parse(trace, request_id, rec.content, tool_schemas, session_key, model)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
            "session_id": session_key,
        }


async def collect_image(
    account,
    pool,
    existing_sid,
    lock,
    prompt,
    model,
    model_id,
    context_seq: tuple[str, ...] | None = None,
    image_size: str | None = None,
    user=None,
):
    await _human_delay()
    async with account_lock(lock, settings.acquire_timeout):
        # Keep every prompt for this account + image model in one chat. Qwen
        # starts limiting (then blocks for a day) once an account opens many new
        # chats, so reuse the pinned chat unless the caller explicitly pinned
        # one, and reattach it if its local registry entry was lost.
        pinned = existing_sid or account.sticky_image_chat(model_id)
        if pinned is not None and account.sessions.get(pinned) is None:
            await account.sessions.attach(pinned, model=IMAGE_CHAT_MODEL, chat_type="t2i")
        session, session_key = await _prepare_session(account, pool, pinned, IMAGE_CHAT_MODEL, context_seq, chat_type="t2i")
        if existing_sid is None:
            account.set_sticky_image_chat(model_id, session_key)
        stop_response_id: str | None = None
        try:
            rec: QwenStreamReconstructor | None = None
            for attempt in range(MAX_RETRIES + 1):
                if attempt:
                    await asyncio.sleep(min(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX_SEC))
                try:
                    resp = await _send_completion(
                        account,
                        session,
                        prompt,
                        IMAGE_CHAT_MODEL,
                        False,
                        False,
                        chat_type="t2i",
                        image_model=model_id,
                        image_size=image_size,
                    )
                except ContextLimitError:
                    _drop_session(pool, account, session_key)
                    raise HTTPException(400, "context length exceeded: conversation too long, start a new conversation") from None
                except HTTPException as exc:
                    if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                        log.warning(
                            "qwen image provider error (%s), attempt %d/%d",
                            exc.status_code,
                            attempt + 1,
                            MAX_RETRIES,
                        )
                        continue
                    raise
                rec = QwenStreamReconstructor()
                incremental = IncrementalSSE()
                try:
                    async for chunk in resp.aiter_bytes():
                        for event in incremental.feed(chunk):
                            rec.handle(event)
                    for event in incremental.finish():
                        rec.handle(event)
                finally:
                    if rec.response_id:
                        stop_response_id = rec.response_id
                    await resp.aclose()
                quota_reason = _is_quota_exhausted(rec)
                if quota_reason is not None:
                    _mark_quota_exhausted(account, pool, session_key, quota_reason)
                    raise QuotaExhausted(f"image generation quota exhausted: {quota_reason}")
                transient = _is_transient_text(rec)
                if transient is not None and attempt < MAX_RETRIES:
                    log.warning(
                        "qwen image transient failure (%s), attempt %d/%d",
                        transient,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                if _is_retryable_error(rec) and attempt < MAX_RETRIES:
                    log.warning(
                        "qwen image retryable error (%s), attempt %d/%d",
                        error_code(rec.error),
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                break
        except asyncio.CancelledError:
            await _try_stop_stream(account.client, session.id, stop_response_id)
            raise

        assert rec is not None
        if _is_context_limit(rec) and not rec.has_content:
            _drop_session(pool, account, session_key)
            raise HTTPException(400, "context length exceeded: conversation too long, start a new conversation")
        usage = _accumulate_usage(session, rec)
        account.sessions.touch_last_message(session_key, rec.response_id)

        if not rec.has_content and rec.error:
            raise HTTPException(_error_status(error_code(rec.error)), _error_body(rec))

        return {
            "image_urls": rec.image_urls,
            "revised_prompt": rec.content or "",
            "usage": usage,
            "session_id": session_key,
        }


async def stream_openai(
    account,
    pool,
    existing_sid,
    lock,
    prompt,
    model,
    model_id,
    thinking,
    search,
    tool_mode=False,
    tool_schemas=None,
    include_usage=False,
    context_seq: tuple[str, ...] | None = None,
    request_id: str | None = None,
    trace: ToolTraceLogger | None = None,
):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    await _human_delay()
    async with account_lock(lock, settings.acquire_timeout):
        try:
            session, session_key = await _prepare_session(account, pool, existing_sid, model_id, context_seq)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {"message": detail},
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
            )
            yield sse(
                {
                    "id": chunk_id,
                    "session_id": None,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return

        if trace is not None:
            trace.dany(request_id, prompt=prompt, session_id=session_key, model=model)
        rec: QwenStreamReconstructor | None = None
        content_buf = ""
        stop_response_id: str | None = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                await asyncio.sleep(min(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX_SEC))
            try:
                resp = await _send_completion(account, session, prompt, model_id, thinking, search)
            except ContextLimitError:
                _drop_session(pool, account, session_key)
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {
                            "message": "context length exceeded: conversation too long, start a new conversation",
                            "code": "context_length_exceeded",
                        },
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "length",
                            }
                        ],
                    }
                )
                yield sse(
                    {
                        "id": chunk_id,
                        "session_id": session_key,
                        "object": "chat.completion.chunk",
                        "choices": [],
                    }
                )
                yield "data: [DONE]\n\n"
                return
            except HTTPException as exc:
                if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                    log.warning(
                        "qwen provider error (%s), attempt %d/%d",
                        exc.status_code,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {"message": detail},
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                    }
                )
                yield sse(
                    {
                        "id": chunk_id,
                        "session_id": session_key,
                        "object": "chat.completion.chunk",
                        "choices": [],
                    }
                )
                yield "data: [DONE]\n\n"
                return
            rec = QwenStreamReconstructor()
            incremental = IncrementalSSE()
            got_content = False
            pending: list[str] = []
            try:
                async for chunk in resp.aiter_bytes():
                    for event in incremental.feed(chunk):
                        rec.handle(event)
                        c_diff, r_diff = rec.take_diffs()
                        if c_diff or r_diff:
                            got_content = True
                        if not pending:
                            pending.append(
                                sse(
                                    {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"role": "assistant"},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                )
                            )
                        delta: dict = {}
                        if c_diff:
                            if tool_mode:
                                content_buf += c_diff
                            else:
                                delta["content"] = c_diff
                        if r_diff:
                            delta["reasoning_content"] = r_diff
                        if delta:
                            pending.append(
                                sse(
                                    {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": delta,
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                )
                            )
                        if got_content:
                            for line in pending:
                                yield line
                            pending.clear()
            finally:
                if rec.response_id:
                    stop_response_id = rec.response_id
                if sys.exc_info()[0] is not None:
                    await _try_stop_stream(account.client, session.id, stop_response_id)
                await resp.aclose()
            if got_content:
                break
            if _is_retryable_error(rec) and attempt < MAX_RETRIES:
                log.warning(
                    "qwen retryable error (%s), attempt %d/%d",
                    error_code(rec.error),
                    attempt + 1,
                    MAX_RETRIES,
                )
                continue
            break

        assert rec is not None
        if _is_context_limit(rec) and not rec.has_content:
            _drop_session(pool, account, session_key)
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": "context length exceeded: conversation too long, start a new conversation",
                        "code": "context_length_exceeded",
                    },
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "length",
                        }
                    ],
                }
            )
            yield sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return
        usage = _accumulate_usage(session, rec)
        account.sessions.touch_last_message(session_key, rec.response_id)

        if not rec.has_content and rec.error:
            err = rec.error
            code = error_code(rec.error)
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": err.get("details") or err.get("message") or "Qwen server error, try again later",
                        "code": code,
                    },
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": code or "error",
                        }
                    ],
                }
            )
            yield sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return

        if tool_mode and trace is not None:
            trace.upstream(request_id, content=content_buf, reasoning=None, session_id=session_key, model=model)
        if tool_mode:
            parsed = toolemu.parse_tool_calls(content_buf, tool_schemas)
            if parsed is not None:
                tool_calls, tool_text = parsed
                for delta in toolemu.tool_call_deltas(tool_calls, tool_text):
                    yield sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                finish = "tool_calls"
            else:
                if content_buf:
                    yield sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": content_buf},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                finish = "stop"
        else:
            finish = "stop"

        if tool_mode and trace is not None:
            _trace_parse(trace, request_id, content_buf, tool_schemas, session_key, model)

        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            }
        )
        if include_usage:
            yield sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": usage,
                }
            )
        yield sse(
            {
                "id": chunk_id,
                "session_id": session_key,
                "object": "chat.completion.chunk",
                "choices": [],
            }
        )
        yield "data: [DONE]\n\n"
