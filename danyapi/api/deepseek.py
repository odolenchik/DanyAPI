from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl

import httpx
from fastapi import HTTPException

from ..accounts import AccountPool, DeepSeekAccount
from ..config import settings
from ..deepseek.client import DeepSeekError, DeepSeekSession
from ..deepseek.stream import IncrementalSSE, MessageReconstructor
from ..loopguard import LoopGuard

# Shared retry constants — imported as local names so monkeypatch in tests works.
# RETRYABLE_HTTP_STATUSES and _drop_session are re-exported for openai.py/streaming.py consumers.
from ..retry import (  # noqa: F401
    MAX_RETRIES,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_BACKOFF_SEC,
    RETRYABLE_FINISH_REASONS,
    RETRYABLE_HTTP_STATUSES,
    _drop_session,
    _is_rate_limit_hint,
    _is_retryable_http,
    _is_stale_session_error,
    _log_rate_limit_hint,
    blank_answer_finish_reason,
    blank_answer_reason,
    is_content_refusal,
    stream_refusal_finish_reason,
    stream_refusal_message,
)
from ..tooltrace import ToolTraceLogger
from .models import (
    CONTEXT_LENGTH_STATUS,
    CONTINUE_PROMPT,
    FINISHED_STATUS,
    INPUT_EXCEEDS_LIMIT,
    INTERRUPTED_STATUSES,
)

log = logging.getLogger("danyapi.api")


def _new_loop_guard() -> LoopGuard | None:
    """A guard for one streamed channel, or None when the feature is off."""
    if not settings.loop_guard:
        return None
    return LoopGuard(
        max_period=settings.loop_max_period,
        repeats=settings.loop_repeats,
        char_max_period=settings.loop_char_max_period,
        char_repeats=settings.loop_char_repeats,
        char_min_chars=settings.loop_char_min_chars,
    )


def _loop_hit(
    guard: LoopGuard | None,
    delta: str,
    *,
    where: str,
    session_key: str | None,
    request_id: str | None,
) -> bool:
    """Feed one streamed fragment to a guard and report whether it saw a loop."""
    if guard is None or not delta:
        return False
    hit = guard.feed(delta)
    if hit is None:
        return False
    log.warning(
        "loop guard: repeated block (%d lines x %d) aborted %s (session=%s request=%s)",
        hit[0],
        hit[1],
        where,
        session_key,
        request_id,
    )
    return True


DEEPSEEK_AUTH_ERROR_CODES = {40001, 40002, 40003, 40012, 40029}


async def _human_delay() -> None:
    delay = random.uniform(settings.human_delay_min, settings.human_delay_max)
    if delay > 0:
        await asyncio.sleep(delay)


def _is_retryable_hint(rec: MessageReconstructor) -> bool:
    hint = rec.hint_error
    return bool(hint and hint.get("finish_reason") in RETRYABLE_FINISH_REASONS)


def _is_context_limit(rec: MessageReconstructor) -> bool:
    if rec.status == CONTEXT_LENGTH_STATUS:
        return True
    hint = rec.hint_error
    return bool(hint and hint.get("finish_reason") == CONTEXT_LENGTH_STATUS)


def _is_input_exceeds_limit(rec: MessageReconstructor) -> bool:
    if rec.status == INPUT_EXCEEDS_LIMIT:
        return True
    hint = rec.hint_error
    return bool(hint and hint.get("finish_reason") == INPUT_EXCEEDS_LIMIT)


def stream_was_interrupted(rec: MessageReconstructor) -> bool:
    """Whether the turn ended without a clean completion from the upstream.

    DeepSeek marks a completed turn with ``response/status = FINISHED``. A
    stream that closes without that status was cut short: the connection
    dropped, the upstream restarted, or the message was never committed. The
    client would otherwise receive a truncated answer with ``finish_reason:
    stop`` — indistinguishable from success, and never retried.

    Outcomes that are a verdict in their own right are not interruptions:
    context length and input-too-long have dedicated recovery paths, a content
    filter refusal is final, and a hint or notice carries its own message that
    the blank-answer path reports. Retrying any of those only repeats the
    failure.
    """
    status = rec.status
    if status == FINISHED_STATUS:
        return False
    if status in INTERRUPTED_STATUSES:
        return True
    if status in (CONTEXT_LENGTH_STATUS, INPUT_EXCEEDS_LIMIT):
        return False
    if rec.hint_error or rec.notice:
        return False
    return True


def _input_exceeds_hint_from_http(exc: HTTPException) -> dict | None:
    detail = exc.detail
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return None
    if not isinstance(detail, dict):
        return None
    if detail.get("finish_reason") != INPUT_EXCEEDS_LIMIT:
        return None
    message = detail.get("message")
    return {
        "message": message if isinstance(message, str) else "Content is too long",
        "finish_reason": INPUT_EXCEEDS_LIMIT,
    }


def _deepseek_status(exc: DeepSeekError) -> int:
    return 401 if exc.biz_code in DEEPSEEK_AUTH_ERROR_CODES else 502


async def _send_with_auth(account, *args, **kwargs):
    # Every upstream DeepSeek call funnels through here, so pacing lives here too:
    # one choke point instead of seven call sites. The caller already holds the
    # account lock, so wait_turn() serialises the actual request starts.
    await account.wait_turn()
    account.note_request_start()
    try:
        return await _send_completion(*args, **kwargs)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            account.mark_broken()
        raise


def _deepseek_error_detail(exc: DeepSeekError) -> str:
    if exc.biz_code in DEEPSEEK_AUTH_ERROR_CODES:
        return f"DeepSeek auth error: {exc}"
    return f"DeepSeek error: {exc}"


def _handle_account_error(account: DeepSeekAccount, exc: Exception, stage: str) -> None:
    code = getattr(exc, "biz_code", None)
    if code in DEEPSEEK_AUTH_ERROR_CODES:
        account.mark_broken()
        log.warning("account #%d auth error at %s %s: %s", account.index, stage, code, exc)
    else:
        log.warning("account #%d error at %s: %s", account.index, stage, exc)


async def _fresh_pow_headers(account) -> dict:
    try:
        return await account.pow.make_header(account.client.create_pow_challenge)
    except DeepSeekError as exc:
        _handle_account_error(account, exc, "chat pow challenge")
        raise HTTPException(_deepseek_status(exc), _deepseek_error_detail(exc)) from exc


def _busy_error_body(rec: MessageReconstructor) -> str:
    hint = rec.hint_error or {}
    return json.dumps(
        {
            "error": {
                "message": hint.get("message") or "DeepSeek server is busy, try again later",
                "finish_reason": hint.get("finish_reason"),
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


async def _collect_continuation(
    account,
    session,
    parent_message_id,
    model_type,
    thinking,
    search,
    ref_file_ids=None,
    request_id: str | None = None,
    trace: ToolTraceLogger | None = None,
    model: str | None = None,
    prompt: str = CONTINUE_PROMPT,
) -> MessageReconstructor | None:
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            await asyncio.sleep(min(RETRY_BACKOFF_SEC * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX_SEC))
        try:
            pow_headers = await _fresh_pow_headers(account)
            resp = await _send_with_auth(
                account,
                account.client,
                pow_headers,
                session.id,
                parent_message_id,
                prompt,
                model_type,
                thinking,
                search,
                ref_file_ids,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            if trace is not None:
                trace.continuation(
                    request_id,
                    session_id=session.id,
                    model=model,
                    status_code=exc.status_code,
                    detail=detail,
                    attempt=attempt + 1,
                )
            if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                log.warning(
                    "deepseek continuation error (%s), attempt %d/%d: %.300s",
                    exc.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    detail,
                )
                # If DeepSeek says the parent message doesn't exist (error 26),
                # the ID was not committed to the session. Retry without it so
                # DeepSeek picks the latest valid parent automatically.
                if "invalid message id" in detail and parent_message_id is not None:
                    log.info("continuation: clearing stale parent_message_id=%s", parent_message_id)
                    parent_message_id = None
                continue
            log.warning("deepseek continuation failed after retries (%s): %.300s", exc.status_code, detail)
            return None
        rec = MessageReconstructor()
        incremental = IncrementalSSE()
        content_guard = _new_loop_guard()
        reasoning_guard = _new_loop_guard()
        loop_truncated = False
        transport_error: Exception | None = None
        try:
            async for chunk in resp.aiter_bytes():
                for event in incremental.feed(chunk):
                    rec.handle(event)
                c_diff, r_diff = rec.take_diffs()
                if _loop_hit(
                    content_guard,
                    c_diff,
                    where="continuation content stream",
                    session_key=session.id,
                    request_id=request_id,
                ) or _loop_hit(
                    reasoning_guard,
                    r_diff,
                    where="continuation reasoning stream",
                    session_key=session.id,
                    request_id=request_id,
                ):
                    loop_truncated = True
                    break
            for event in incremental.finish():
                rec.handle(event)
        except (httpx.TransportError, ssl.SSLError, ConnectionError) as exc:
            # The continuation stream dropped. Keep whatever text arrived (the
            # caller concatenates it with the main reply) and report the cut as
            # a missing FINISHED rather than letting a transport error escape —
            # this function's callers treat None/partial results as data, and
            # an escaping exception would kill the client stream outright.
            transport_error = exc
            log.warning(
                "continuation transport error (%s) after %d chars",
                type(exc).__name__,
                len(rec.content or ""),
            )
        finally:
            await resp.aclose()
        if transport_error is not None and not (rec.content or rec.reasoning):
            # Nothing arrived: let the caller decide (it may retry or stop).
            return None
        if loop_truncated:
            await _try_stop_stream(account.client, session.id, rec.id)
        if not (rec.content or rec.reasoning) and _is_retryable_hint(rec) and attempt < MAX_RETRIES:
            continue
        if not (rec.content or rec.reasoning) and _is_rate_limit_hint(rec):
            _log_rate_limit_hint(
                rec,
                account,
                where="continuation",
                session_id=session.id,
                request_id=request_id,
                model=model,
            )
        return rec


async def _prepare_session(
    account: DeepSeekAccount,
    pool: AccountPool,
    existing_sid: str | None,
    context_seq: tuple[str, ...] | None = None,
) -> tuple[DeepSeekSession, str, str | None]:
    try:
        session, session_key = await account.sessions.obtain(existing_sid)
    except DeepSeekError as exc:
        _handle_account_error(account, exc, "session create")
        raise HTTPException(_deepseek_status(exc), _deepseek_error_detail(exc)) from exc
    if session_key != existing_sid:
        pool.register(account.index, session_key)
        if existing_sid:
            pool.forget(existing_sid)
            pool.forget_context(existing_sid)
            account.sessions.forget(existing_sid)
    # Note: context_seq is indexed AFTER successful send (see streaming.py),
    # so that non-retryable errors don't leave a stale fingerprint pointing at
    # a session that never received the message.
    return session, session_key, session.last_message_id


async def _send_completion(
    client,
    pow_headers,
    session_id,
    parent_message_id,
    prompt,
    model_type,
    thinking,
    search,
    ref_file_ids=None,
):
    try:
        resp = await client.completion(
            chat_session_id=session_id,
            prompt=prompt,
            parent_message_id=parent_message_id,
            model_type=model_type,
            thinking_enabled=thinking,
            search_enabled=search,
            ref_file_ids=ref_file_ids,
            pow_headers=pow_headers,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:500]) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"DeepSeek request failed: {exc}") from exc

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        raise HTTPException(resp.status_code, body[:500].decode("utf-8", errors="replace"))

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        body = await resp.aread()
        await resp.aclose()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(502, body[:500].decode("utf-8", errors="replace")) from exc
        data = payload.get("data") or {}
        if data.get("biz_code"):
            code = data["biz_code"]
            status = 401 if code in DEEPSEEK_AUTH_ERROR_CODES else 502
            raise HTTPException(status, f"DeepSeek error {code}: {data.get('biz_msg')}")
        if payload.get("code"):
            code = payload["code"]
            status = 401 if code in DEEPSEEK_AUTH_ERROR_CODES else 502
            raise HTTPException(
                status,
                f"DeepSeek error {code}: {payload.get('msg') or payload.get('message')}",
            )
        raise HTTPException(502, "unexpected non-stream response")
    return resp
