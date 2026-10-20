from __future__ import annotations

import asyncio
import json
import logging
import ssl
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import HTTPException

from .. import tools as toolemu
from ..accounts import AccountPoolBusy, account_lock
from ..config import settings
from ..deepseek.stream import IncrementalSSE, MessageReconstructor
from ..tokens import count_messages_tokens
from ..tools.prompt import build_compact_prompt
from ..tools.validate import (
    build_repair_prompt,
    build_unparsed_repair_prompt,
    should_retry_unparsed,
    validate_calls,
)
from ..tooltrace import ToolTraceLogger
from ..tooltrace import classify as _classify_trace
from ..usage import record_usage
from .deepseek import (
    MAX_RETRIES,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_BACKOFF_SEC,
    _busy_error_body,
    _collect_continuation,
    _drop_session,
    _fresh_pow_headers,
    _human_delay,
    _input_exceeds_hint_from_http,
    _is_context_limit,
    _is_input_exceeds_limit,
    _is_rate_limit_hint,
    _is_retryable_hint,
    _is_retryable_http,
    _is_stale_session_error,
    _log_rate_limit_hint,
    _prepare_session,
    _send_with_auth,
    _try_stop_stream,
    blank_answer_finish_reason,
    blank_answer_reason,
)
from .models import CONTEXT_LENGTH_STATUS, INPUT_EXCEEDS_LIMIT, MAX_CONTINUE_ROUNDS, _deepseek_usage, _finish_reason

log = logging.getLogger("danyapi.api")


def _record_deepseek_usage(
    session,
    model: str,
    prompt_tokens: int,
    *,
    user: str | None = None,
    session_key: str | None = None,
) -> None:
    """Account one DeepSeek request in the usage tracker.

    ``session.accumulated_tokens`` is a running total for the whole session, so
    only the growth since the last recorded request is counted — otherwise a
    multi-turn conversation would re-count its earlier tokens on every turn.
    """
    total = max(0, int(getattr(session, "accumulated_tokens", 0) or 0))
    accounted = max(0, int(getattr(session, "accounted_tokens", 0) or 0))
    delta = max(0, total - accounted)
    session.accounted_tokens = max(accounted, total)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    record_usage(
        "deepseek",
        model,
        prompt_tokens,
        delta,
        prompt_tokens + delta,
        user=user,
        session_id=session_key,
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _trace_parse(
    trace: ToolTraceLogger | None,
    request_id: str | None,
    content: str,
    tool_schemas: Any,
    session_key: str | None = None,
    model: str | None = None,
    *,
    degenerate: bool = False,
) -> None:
    if trace is None:
        return
    if degenerate:
        # The stream collapsed into a runaway container repeat and was aborted.
        # Report it as its own result so it is visible in the trace summary
        # instead of being buried under the (possibly ok) parse of the prefix.
        trace.parse(
            request_id,
            result="degenerate_repeat",
            issue=None,
            session_id=session_key,
            model=model,
        )
        return
    parsed = toolemu.parse_tool_calls(content, tool_schemas)
    issue, detail = _classify_trace(content, tool_schemas)
    # A valid parse can still carry a mangle (extra key / wrong type).
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


def _validate_parsed(calls: Any, tool_schemas: Any) -> list[Any]:
    """Validate already-parsed calls against the client schemas."""
    as_dicts = [{"name": call.name, "arguments": call.arguments} for call in calls]
    return validate_calls(as_dicts, tool_schemas)


async def _retry_tool_call(
    account,
    session,
    parent_message_id,
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    tool_schemas: Any,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    model: str,
    prompt: str,
    *,
    require_valid: bool = True,
    trace_repair: bool = False,
) -> tuple[list[Any], str] | None:
    """Re-ask the model once for a tool call, in the same upstream session.

    Shared plumbing for both repair paths: the model still sees its own broken
    answer in the session, so the prompt only has to say what went wrong. Returns
    the ``(calls, text)`` of the retry when it parses (and, with
    ``require_valid``, also passes schema validation); any other outcome returns
    ``None`` so the caller keeps the original result rather than losing it.

    A transport/session failure is swallowed — repair is best-effort and must
    never abort a request that already produced something.
    """
    try:
        rec = await _collect_continuation(
            account,
            session,
            parent_message_id,
            model_type,
            thinking,
            search,
            ref_file_ids,
            request_id=request_id,
            trace=trace,
            model=model,
            prompt=prompt,
        )
    except Exception as exc:
        log.warning("tool-call repair failed: %s", exc)
        return None
    if rec is None or not rec.content:
        return None
    parsed = toolemu.parse_tool_calls(rec.content, tool_schemas)
    if parsed is None:
        return None
    calls, text = parsed
    if require_valid and _validate_parsed(calls, tool_schemas):
        return None
    if trace_repair and trace is not None:
        trace.continuation(
            request_id,
            session_id=session.id,
            model=model,
            detail="unparsed repair retry succeeded",
            attempt=1,
        )
    return calls, text


async def _repair_tool_calls(
    account,
    session,
    parent_message_id,
    issues: list[Any],
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    tool_schemas: Any,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    model: str,
) -> tuple[list[Any], str] | None:
    """Ask the model once to redo a tool call that failed schema validation."""
    return await _retry_tool_call(
        account,
        session,
        parent_message_id,
        model_type,
        thinking,
        search,
        ref_file_ids,
        tool_schemas,
        request_id,
        trace,
        model,
        build_repair_prompt(issues),
    )


async def _repair_unparsed(
    account,
    session,
    parent_message_id,
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    tool_schemas: Any,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    model: str,
    tool_choice: Any,
    issue: str | None,
) -> tuple[list[Any], str] | None:
    """Re-ask once when a tool-mode reply did not parse into any call.

    Gated by ``should_retry_unparsed`` so a forced ``tool_choice`` always retries
    while a plain ``auto`` answer only retries when ``DANYAPI_TOOLCALL_RETRY`` is
    set and the reply looked like a mangled call. The retry must validate before
    it is accepted — an unparsed reply has no useful original to fall back to.
    """
    if not should_retry_unparsed(tool_choice, issue, retry_auto=settings.toolcall_retry):
        return None
    return await _retry_tool_call(
        account,
        session,
        parent_message_id,
        model_type,
        thinking,
        search,
        ref_file_ids,
        tool_schemas,
        request_id,
        trace,
        model,
        build_unparsed_repair_prompt(issue),
        trace_repair=True,
    )


# ---------------------------------------------------------------------------
# Compact retry: on CONTEXT_LENGTH_EXCEEDED, hand the conversation over to a
# fresh session. Older turns are compressed into a model-written summary (so
# facts survive instead of being dropped), the last N pairs and recent tool
# results are kept verbatim, and the current turn is always preserved. The
# summary and the final prompt are sent as single messages, each kept under
# DeepSeek's per-request input limit.
# ---------------------------------------------------------------------------

COMPACT_MAX_PAIRS = 8
COMPACT_MAX_TOOL_RESULTS = 3
# Cap on the dropped-history text handed to the summarizer, so the summarize
# request itself stays well within the per-request limit.
COMPACT_SUMMARY_MAX_CHARS = 24000


async def _summarize_for_handoff(
    account,
    dropped_source: str,
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    model: str,
) -> tuple[Any, str, str | None, str] | None:
    """Open a session and summarize the history that compaction will drop.

    Runs the summary on the *fresh* session, not the full one (the full session
    is exactly what cannot take more input). On success the session is returned
    so the caller reuses it for the real answer: the model then sees the summary
    it just wrote plus the compact prompt. Returns ``None`` when the session
    could not be created; a failed summary still returns the session with an
    empty summary, so the caller can continue without one.
    """
    prompt = f"{toolemu.HANDOFF_SUMMARY_INSTRUCTION}\n\n{dropped_source}"
    try:
        session, sid = await account.sessions.obtain(None)
    except Exception as exc:
        log.warning("compact summary: failed to create session: %s", exc)
        return None
    rec = await _collect_continuation(
        account,
        session,
        None,
        model_type,
        thinking,
        search,
        ref_file_ids,
        request_id=request_id,
        trace=trace,
        model=model,
        prompt=prompt,
    )
    if rec is None:
        log.info("compact summary: no summary produced, continuing without one")
        return session, sid, None, ""
    return session, sid, rec.id, (rec.content or "").strip()


async def _compact_retry(
    account,
    pool,
    old_session_key: str,
    messages: list[Any] | None,
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    tool_mode: bool,
    tool_schemas: Any,
    include_usage: bool,
    context_seq: tuple[str, ...] | None,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    model: str = "",
    user: str | None = None,
    tools: Any = None,
    tool_choice: Any = None,
    response_format: Any = None,
) -> dict | None:
    """Summarize the dropped history, then answer in a fresh session.

    Returns the result dict, or None when compaction could not run.
    """
    if not messages:
        return None

    # Free the overflowing session before creating the replacement.
    _drop_session(pool, account, old_session_key)

    # Summarize the older turns that compaction would otherwise drop, so facts
    # and decisions survive the move instead of being lost to a truncation note.
    summary: str | None = None
    new_session = None
    new_sid = None
    parent_id = None
    dropped_source = toolemu.dropped_pairs_source(messages, COMPACT_MAX_PAIRS, COMPACT_SUMMARY_MAX_CHARS)
    if dropped_source:
        summarized = await _summarize_for_handoff(
            account,
            dropped_source,
            model_type,
            thinking,
            search,
            ref_file_ids,
            request_id,
            trace,
            model,
        )
        if summarized is None:
            return None
        new_session, new_sid, parent_id, summary = summarized
        pool.register(account.index, new_sid)

    system, compact_body = build_compact_prompt(
        messages,
        COMPACT_MAX_PAIRS,
        COMPACT_MAX_TOOL_RESULTS,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        summary=summary,
    )
    prompt = "\n\n".join(part for part in (system, compact_body) if part).strip()
    if not prompt:
        log.warning("compact retry: empty prompt, giving up")
        return None

    if new_session is None:
        try:
            new_session, new_sid = await account.sessions.obtain(None)
            pool.register(account.index, new_sid)
        except Exception as exc:
            log.warning("compact retry: failed to create session: %s", exc)
            return None

    log.info(
        "compact retry: old_session=%s new_session=%s summary_len=%d prompt_len=%d",
        old_session_key,
        new_sid,
        len(summary or ""),
        len(prompt),
    )

    # One combined send: system prompt, summary, kept history and current turn.
    try:
        pow_headers = await _fresh_pow_headers(account)
        resp = await _send_with_auth(
            account,
            account.client,
            pow_headers,
            new_sid,
            parent_id,
            prompt,
            model_type,
            thinking,
            search,
            ref_file_ids,
        )
    except HTTPException as exc:
        log.warning("compact retry: send failed (%s)", exc.status_code)
        return None

    rec = MessageReconstructor()
    incremental = IncrementalSSE()
    try:
        async for chunk in resp.aiter_bytes():
            for event in incremental.feed(chunk):
                rec.handle(event)
        for event in incremental.finish():
            rec.handle(event)
    finally:
        await resp.aclose()

    if not (rec.content or rec.reasoning):
        log.warning("compact retry: no content from DeepSeek (status=%s)", rec.status)
        return None

    # Index the new session for context dedup.
    if context_seq:
        pool.index_context(new_sid, context_seq)
    account.sessions.touch_last_message(new_sid, rec.id)

    log.info("compact retry OK: new_session=%s content_len=%d", new_sid, len(rec.content or ""))

    # Build the response in the same shape as normal completion.
    if tool_mode and trace is not None:
        trace.upstream(request_id, content=rec.content, reasoning=rec.reasoning or None, session_id=new_sid, model=None)
    if tool_mode:
        parsed = toolemu.parse_tool_calls(rec.content, tool_schemas)
        if parsed is None:
            issue, _detail = _classify_trace(rec.content, tool_schemas)
            parsed = await _repair_unparsed(
                account,
                new_session,
                rec.id,
                model_type,
                thinking,
                search,
                ref_file_ids,
                tool_schemas,
                request_id,
                trace,
                model,
                tool_choice,
                issue,
            )
        if parsed is not None:
            tool_calls, tool_text = parsed
            finish = "tool_calls"
            message = toolemu.format_tool_message(tool_calls, tool_text, rec.reasoning)
        else:
            message = {"role": "assistant", "content": rec.content}
            if rec.reasoning:
                message["reasoning_content"] = rec.reasoning
            finish = _finish_reason(rec.status)
    else:
        finish = _finish_reason(rec.status)
        message = {"role": "assistant", "content": rec.content}
        if rec.reasoning:
            message["reasoning_content"] = rec.reasoning

    prompt_tokens = count_messages_tokens(messages) if messages else 0
    _record_deepseek_usage(
        new_session,
        model,
        prompt_tokens,
        user=user,
        session_key=new_sid,
    )
    result = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": _deepseek_usage(getattr(new_session, "accumulated_tokens", 0)),
        "session_id": new_sid,
    }
    return result


async def _compact_retry_stream(
    account,
    pool,
    old_session_key: str,
    messages: list[Any] | None,
    model_type: str,
    thinking: bool,
    search: bool,
    ref_file_ids: Any,
    tool_mode: bool,
    tool_schemas: Any,
    include_usage: bool,
    context_seq: tuple[str, ...] | None,
    request_id: str | None,
    trace: ToolTraceLogger | None,
    chunk_id: str,
    created: int,
    model: str,
    user: str | None = None,
    tools: Any = None,
    tool_choice: Any = None,
    response_format: Any = None,
) -> AsyncIterator[str] | None:
    """Summarize the dropped history, then answer in a fresh session.

    Returns an async generator, or None when compaction could not run.
    """
    if not messages:
        return None

    _drop_session(pool, account, old_session_key)

    summary: str | None = None
    new_session = None
    new_sid = None
    parent_id = None
    dropped_source = toolemu.dropped_pairs_source(messages, COMPACT_MAX_PAIRS, COMPACT_SUMMARY_MAX_CHARS)
    if dropped_source:
        summarized = await _summarize_for_handoff(
            account,
            dropped_source,
            model_type,
            thinking,
            search,
            ref_file_ids,
            request_id,
            trace,
            model,
        )
        if summarized is None:
            return None
        new_session, new_sid, parent_id, summary = summarized
        pool.register(account.index, new_sid)

    system, compact_body = build_compact_prompt(
        messages,
        COMPACT_MAX_PAIRS,
        COMPACT_MAX_TOOL_RESULTS,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
        summary=summary,
    )
    prompt = "\n\n".join(part for part in (system, compact_body) if part).strip()
    if not prompt:
        log.warning("compact retry (stream): empty prompt, giving up")
        return None

    if new_session is None:
        try:
            new_session, new_sid = await account.sessions.obtain(None)
            pool.register(account.index, new_sid)
        except Exception as exc:
            log.warning("compact retry (stream): failed to create session: %s", exc)
            return None

    log.info(
        "compact retry (stream): old_session=%s new_session=%s summary_len=%d prompt_len=%d",
        old_session_key,
        new_sid,
        len(summary or ""),
        len(prompt),
    )

    # One combined send: system prompt, summary, kept history and current turn.
    try:
        pow_headers = await _fresh_pow_headers(account)
        resp = await _send_with_auth(
            account,
            account.client,
            pow_headers,
            new_sid,
            parent_id,
            prompt,
            model_type,
            thinking,
            search,
            ref_file_ids,
        )
    except HTTPException as exc:
        log.warning("compact retry (stream): send failed (%s)", exc.status_code)
        return None

    if context_seq:
        pool.index_context(new_sid, context_seq)

    async def _gen() -> AsyncIterator[str]:
        rec = MessageReconstructor()
        incremental = IncrementalSSE()
        content_buf = ""
        got_content = False
        try:
            async for chunk in resp.aiter_bytes():
                for event in incremental.feed(chunk):
                    rec.handle(event)
                c_diff, r_diff = rec.take_diffs()
                if c_diff or r_diff:
                    got_content = True
                    delta: dict = {}
                    if c_diff:
                        if tool_mode:
                            content_buf += c_diff
                        else:
                            delta["content"] = c_diff
                    if r_diff:
                        delta["reasoning_content"] = r_diff
                    if delta:
                        yield _sse(
                            {
                                "id": chunk_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }
                        )
            for event in incremental.finish():
                rec.handle(event)
        finally:
            await resp.aclose()

        if not got_content and not (rec.content or rec.reasoning):
            log.warning("compact retry (stream): no content (status=%s)", rec.status)
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {"message": "context compact failed: no response from DeepSeek"},
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
            )
            yield _sse({"id": chunk_id, "session_id": new_sid, "object": "chat.completion.chunk", "choices": []})
            yield "data: [DONE]\n\n"
            return

        account.sessions.touch_last_message(new_sid, rec.id)
        log.info("compact retry (stream) OK: new_session=%s content_len=%d", new_sid, len(rec.content or ""))

        if tool_mode and trace is not None:
            trace.upstream(request_id, content=content_buf, reasoning=None, session_id=new_sid, model=model)
        if tool_mode:
            parsed = toolemu.parse_tool_calls(content_buf, tool_schemas)
            if parsed is None:
                issue, _detail = _classify_trace(content_buf, tool_schemas)
                parsed = await _repair_unparsed(
                    account,
                    new_session,
                    rec.id,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    tool_schemas,
                    request_id,
                    trace,
                    model,
                    tool_choice,
                    issue,
                )
                if parsed is not None:
                    content_buf = parsed[1] or content_buf
            if parsed is not None:
                tool_calls, tool_text = parsed
                issues = _validate_parsed(tool_calls, tool_schemas)
                if issues:
                    repaired = await _repair_tool_calls(
                        account,
                        new_session,
                        rec.id,
                        issues,
                        model_type,
                        thinking,
                        search,
                        ref_file_ids,
                        tool_schemas,
                        request_id,
                        trace,
                        model,
                    )
                    if repaired is not None:
                        tool_calls, tool_text = repaired
                for delta in toolemu.tool_call_deltas(tool_calls, tool_text):
                    yield _sse(
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
                    yield _sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": content_buf}, "finish_reason": None}],
                        }
                    )
                finish = _finish_reason(rec.status)
        else:
            finish = _finish_reason(rec.status)

        if tool_mode and trace is not None:
            _trace_parse(trace, request_id, content_buf, tool_schemas, new_sid, model)

        yield _sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            }
        )
        _record_deepseek_usage(
            new_session,
            model,
            count_messages_tokens(messages) if messages else 0,
            user=user,
            session_key=new_sid,
        )
        if include_usage:
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": _deepseek_usage(getattr(new_session, "accumulated_tokens", 0)),
                }
            )
        yield _sse({"id": chunk_id, "session_id": new_sid, "object": "chat.completion.chunk", "choices": []})
        yield "data: [DONE]\n\n"

    return _gen()


async def _collect_non_stream(
    account,
    pool,
    existing_sid,
    lock,
    prompt,
    model,
    model_type,
    thinking,
    search,
    ref_file_ids=None,
    tool_mode=False,
    tool_schemas=None,
    include_usage=False,
    context_seq: tuple[str, ...] | None = None,
    request_id: str | None = None,
    trace: ToolTraceLogger | None = None,
    messages: list[Any] | None = None,
    user: str | None = None,
    rebuild_prompt: Callable[[], str] | None = None,
    tools: Any = None,
    tool_choice: Any = None,
    response_format: Any = None,
):
    await _human_delay()
    async with account_lock(lock, settings.acquire_timeout, account):
        session, session_key, parent_message_id = await _prepare_session(account, pool, existing_sid, context_seq)
        if trace is not None:
            trace.dany(request_id, prompt=prompt, session_id=session_key, model=model)
        stop_message_id: str | None = None
        started = time.monotonic()
        try:
            rec: MessageReconstructor | None = None
            response_message_id = None
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
                    input_hint = _input_exceeds_hint_from_http(exc)
                    if input_hint is not None:
                        rec = MessageReconstructor()
                        rec.hint_error = input_hint
                        break
                    if _is_stale_session_error(exc) and attempt < MAX_RETRIES:
                        # The session's parent pointer is dead upstream (the message
                        # it refers to no longer exists). Retrying would resend the
                        # same dead id, so drop the session and continue in a fresh
                        # one instead of burning five attempts on a permanent error.
                        log.warning("stale session (%s), re-creating session", exc.detail)
                        _drop_session(pool, account, session_key)
                        session, session_key, parent_message_id = await _prepare_session(account, pool, None, context_seq)
                        if rebuild_prompt is not None:
                            # The dead session is gone, so the delta prompt would
                            # arrive with no history behind it. Send the full
                            # conversation instead, as for a brand-new session.
                            prompt = rebuild_prompt()
                        continue
                    if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                        log.warning(
                            "deepseek provider error (%s), attempt %d/%d",
                            exc.status_code,
                            attempt + 1,
                            MAX_RETRIES,
                        )
                        continue
                    raise
                rec = MessageReconstructor()
                incremental = IncrementalSSE()
                response_message_id = None
                try:
                    async for chunk in resp.aiter_bytes():
                        for event in incremental.feed(chunk):
                            if event.event == "ready" and isinstance(event.data, dict):
                                response_message_id = event.data.get("response_message_id")
                                if response_message_id:
                                    stop_message_id = response_message_id
                            rec.handle(event)
                    for event in incremental.finish():
                        rec.handle(event)
                finally:
                    await resp.aclose()
                if rec.id:
                    stop_message_id = rec.id
                if not (rec.content or rec.reasoning) and _is_retryable_hint(rec) and attempt < MAX_RETRIES:
                    log.warning(
                        "retryable hint (%s), attempt %d/%d",
                        (rec.hint_error or {}).get("finish_reason"),
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                break
        except asyncio.CancelledError:
            await _try_stop_stream(account.client, session.id, stop_message_id)
            raise

        assert rec is not None
        cont_exhausted = False
        if _is_context_limit(rec) and not (rec.content or rec.reasoning):
            compact_result = await _compact_retry(
                account,
                pool,
                session_key,
                messages,
                model_type,
                thinking,
                search,
                ref_file_ids,
                tool_mode,
                tool_schemas,
                include_usage,
                context_seq,
                request_id,
                trace,
                model=model,
                user=user,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
            )
            if compact_result is not None:
                return compact_result
            # Compact retry unavailable or failed — drop session and raise.
            _drop_session(pool, account, session_key)
            raise HTTPException(400, "context length exceeded: conversation too long, start a new conversation")
        if _is_input_exceeds_limit(rec):
            # A fresh session with no committed parent means we just sent a full
            # rebuild prompt that DeepSeek rejected as too long. "Continue" has
            # nothing valid to parent off (the response_message_id is not a
            # committed message), so compress the history into a fresh session
            # instead of retrying with a phantom ID.
            if session_key != existing_sid:
                compact_result = await _compact_retry(
                    account,
                    pool,
                    session_key,
                    messages,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    tool_mode,
                    tool_schemas,
                    include_usage,
                    context_seq,
                    request_id,
                    trace,
                    model=model,
                    user=user,
                    tools=tools,
                    tool_choice=tool_choice,
                    response_format=response_format,
                )
                if compact_result is not None:
                    return compact_result
                _drop_session(pool, account, session_key)
                raise HTTPException(400, "input exceeds limit: message too long, shorten the message and try again")

            cont_parent = parent_message_id or rec.id or response_message_id
            log.info("input_exceeds_limit: starting continuation (parent=%s)", cont_parent)
            for round_num in range(MAX_CONTINUE_ROUNDS):
                cont_rec = await _collect_continuation(
                    account,
                    session,
                    cont_parent,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    request_id=request_id,
                    trace=trace,
                    model=model,
                )
                if cont_rec is None:
                    log.warning("continuation round %d: no response (parent=%s)", round_num + 1, cont_parent)
                    break
                log.info(
                    "continuation round %d: id=%s content_len=%d still_exceeds=%s",
                    round_num + 1,
                    cont_rec.id,
                    len(cont_rec.content or ""),
                    _is_input_exceeds_limit(cont_rec),
                )
                rec.extend_with(cont_rec)
                cont_parent = cont_rec.id or cont_parent
                if not _is_input_exceeds_limit(cont_rec):
                    break
            else:
                cont_exhausted = True
                log.warning(
                    "continuation exhausted after %d rounds (last_parent=%s)",
                    MAX_CONTINUE_ROUNDS,
                    cont_parent,
                )
        # Input message itself is too long, not the whole conversation. Do not
        # switch to a fresh session or compress history here — keep the session
        # and report the error honestly.
        if _is_input_exceeds_limit(rec) and not (rec.content or rec.reasoning):
            raise HTTPException(400, "input exceeds limit: message too long, shorten the message and try again")

        session.accumulated_tokens = max(getattr(session, "accumulated_tokens", 0) or 0, rec.accumulated_tokens)

        # Check the hint BEFORE recording the message id. On a rate-limit hint the
        # upstream never stored the message, so remembering its id would poison the
        # session's parent pointer and the next request would fail with
        # "invalid message id". The streaming path below already orders these the
        # same way; keep the two in sync.
        if not (rec.content or rec.reasoning) and rec.hint_error:
            if _is_rate_limit_hint(rec):
                _log_rate_limit_hint(
                    rec,
                    account,
                    where="non-stream",
                    session_id=session_key,
                    request_id=request_id,
                    model=model,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            raise HTTPException(429, _busy_error_body(rec))

        # A blank reply is never a success: a content-policy refusal or a bare
        # notice arrives with no content, and passing it on as "stop" makes the
        # client resend the same request indefinitely.
        blank_reason = blank_answer_reason(rec)
        if blank_reason is not None:
            log.warning(
                "empty answer where=non-stream session=%s request=%s reason=%s",
                session_key,
                request_id,
                blank_reason,
            )
            raise HTTPException(502, blank_reason)

        account.sessions.touch_last_message(session_key, rec.id or response_message_id)
        if context_seq:
            pool.index_context(session_key, context_seq)
        log.info("deepseek completion OK (%.0fms)", (time.monotonic() - started) * 1000)

        content = rec.content
        reasoning = rec.reasoning
        if tool_mode and trace is not None:
            trace.upstream(request_id, content=content, reasoning=rec.reasoning or None, session_id=session_key, model=model)
        if tool_mode:
            parsed = toolemu.parse_tool_calls(content, tool_schemas)
            if parsed is None:
                issue, _detail = _classify_trace(content, tool_schemas)
                parsed = await _repair_unparsed(
                    account,
                    session,
                    rec.id or response_message_id,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    tool_schemas,
                    request_id,
                    trace,
                    model,
                    tool_choice,
                    issue,
                )
                if parsed is not None:
                    content = parsed[1] or content
            if parsed is not None:
                tool_calls, tool_text = parsed
                issues = _validate_parsed(tool_calls, tool_schemas)
                if issues:
                    repaired = await _repair_tool_calls(
                        account,
                        session,
                        rec.id or response_message_id,
                        issues,
                        model_type,
                        thinking,
                        search,
                        ref_file_ids,
                        tool_schemas,
                        request_id,
                        trace,
                        model,
                    )
                    if repaired is not None:
                        tool_calls, tool_text = repaired
                finish = "tool_calls"
                message = toolemu.format_tool_message(tool_calls, tool_text, reasoning)
            else:
                message = {"role": "assistant", "content": content}
                if reasoning:
                    message["reasoning_content"] = reasoning
                finish = _finish_reason(rec.status)
        else:
            finish = _finish_reason(rec.status)
            message = {"role": "assistant", "content": content}
            if reasoning:
                message["reasoning_content"] = reasoning
        if tool_mode and trace is not None:
            _trace_parse(trace, request_id, content, tool_schemas, session_key, model)
        _record_deepseek_usage(
            session,
            model,
            count_messages_tokens(messages) if messages else 0,
            user=user,
            session_key=session_key,
        )
        result = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish,
                }
            ],
            "usage": _deepseek_usage(getattr(session, "accumulated_tokens", 0)),
            "session_id": session_key,
        }
        if cont_exhausted:
            result["x_continuation_truncated"] = True
        return result


async def _stream_openai(
    account,
    pool,
    existing_sid,
    lock,
    prompt,
    model,
    model_type,
    thinking,
    search,
    ref_file_ids=None,
    tool_mode=False,
    tool_schemas=None,
    include_usage=False,
    context_seq: tuple[str, ...] | None = None,
    request_id: str | None = None,
    trace: ToolTraceLogger | None = None,
    messages: list[Any] | None = None,
    user: str | None = None,
    rebuild_prompt: Callable[[], str] | None = None,
    tools: Any = None,
    tool_choice: Any = None,
    response_format: Any = None,
):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    await _human_delay()
    async with account_lock(lock, settings.acquire_timeout, account):
        try:
            session, session_key, parent_message_id = await _prepare_session(account, pool, existing_sid, context_seq)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {"message": detail},
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
            )
            yield _sse(
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
        rec: MessageReconstructor | None = None
        response_message_id = None
        stop_message_id: str | None = None
        content_buf = ""
        started = time.monotonic()
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
                input_hint = _input_exceeds_hint_from_http(exc)
                if input_hint is not None:
                    rec = MessageReconstructor()
                    rec.hint_error = input_hint
                    break
                if _is_stale_session_error(exc) and attempt < MAX_RETRIES:
                    # Same permanent error as the non-stream path: drop the dead
                    # session and continue in a fresh one rather than retrying.
                    log.warning("stale session (%s), re-creating session", exc.detail)
                    _drop_session(pool, account, session_key)
                    session, session_key, parent_message_id = await _prepare_session(account, pool, None, context_seq)
                    if rebuild_prompt is not None:
                        # Same as the non-stream path: the fresh session has no
                        # history, so send the full conversation, not the delta.
                        prompt = rebuild_prompt()
                    continue
                if _is_retryable_http(exc) and attempt < MAX_RETRIES:
                    log.warning(
                        "deepseek provider error (%s), attempt %d/%d",
                        exc.status_code,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    continue
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {"message": detail},
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                    }
                )
                yield _sse(
                    {
                        "id": chunk_id,
                        "session_id": session_key,
                        "object": "chat.completion.chunk",
                        "choices": [],
                    }
                )
                yield "data: [DONE]\n\n"
                return
            rec = MessageReconstructor()
            incremental = IncrementalSSE()
            response_message_id = None
            got_content = False
            pending: list[str] = []
            degenerate_aborted = False
            degenerate_next_check = 0
            try:
                async for chunk in resp.aiter_bytes():
                    for event in incremental.feed(chunk):
                        if event.event == "ready" and isinstance(event.data, dict):
                            response_message_id = event.data.get("response_message_id")
                            if response_message_id:
                                stop_message_id = response_message_id
                        rec.handle(event)
                        c_diff, r_diff = rec.take_diffs()
                        if c_diff or r_diff:
                            got_content = True
                        if not pending:
                            pending.append(
                                _sse(
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
                                _sse(
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
                        if settings.degenerate_guard and tool_mode and len(content_buf) > degenerate_next_check:
                            degenerate_next_check = len(content_buf)
                            if toolemu.is_degenerate_repeat(content_buf, threshold=settings.degenerate_threshold):
                                log.warning(
                                    "degenerate stream: runaway calls-container repeat (len=%d), aborting",
                                    len(content_buf),
                                )
                                degenerate_aborted = True
                                break
                    if degenerate_aborted:
                        break
            except (ssl.SSLError, ConnectionError) as exc:
                log.warning(
                    "stream transport error (%s), attempt %d/%d",
                    type(exc).__name__,
                    attempt + 1,
                    MAX_RETRIES,
                )
                if got_content:
                    yield _sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "error": {"message": f"connection lost mid-stream: {type(exc).__name__}"},
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        }
                    )
                    yield _sse(
                        {
                            "id": chunk_id,
                            "session_id": session_key,
                            "object": "chat.completion.chunk",
                            "choices": [],
                        }
                    )
                    yield "data: [DONE]\n\n"
                    break
                if attempt < MAX_RETRIES:
                    continue
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {"message": f"connection lost: {type(exc).__name__}"},
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                    }
                )
                yield _sse(
                    {
                        "id": chunk_id,
                        "session_id": session_key,
                        "object": "chat.completion.chunk",
                        "choices": [],
                    }
                )
                yield "data: [DONE]\n\n"
                break
            finally:
                if rec.id:
                    stop_message_id = rec.id
                if degenerate_aborted or sys.exc_info()[0] is not None:
                    await _try_stop_stream(account.client, session.id, stop_message_id)
                await resp.aclose()
            if degenerate_aborted:
                break
            if got_content:
                break
            if _is_retryable_hint(rec) and attempt < MAX_RETRIES:
                log.warning(
                    "retryable hint (%s), attempt %d/%d",
                    (rec.hint_error or {}).get("finish_reason"),
                    attempt + 1,
                    MAX_RETRIES,
                )
                continue
            break

        assert rec is not None
        cont_exhausted = False
        if _is_context_limit(rec) and not (rec.content or rec.reasoning):
            compact_gen = await _compact_retry_stream(
                account,
                pool,
                session_key,
                messages,
                model_type,
                thinking,
                search,
                ref_file_ids,
                tool_mode,
                tool_schemas,
                include_usage,
                context_seq,
                request_id,
                trace,
                chunk_id,
                created,
                model,
                user=user,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
            )
            if compact_gen is not None:
                async for item in compact_gen:
                    yield item
                return
            # Compact retry unavailable or failed — drop session and emit error.
            _drop_session(pool, account, session_key)
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": "context length exceeded: conversation too long, start a new conversation",
                        "finish_reason": CONTEXT_LENGTH_STATUS,
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
            yield _sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return
        if _is_input_exceeds_limit(rec):
            # Fresh session + rejected rebuild prompt: there is no committed
            # parent for "Continue", so compress into a fresh session instead of
            # retrying with the uncommitted response_message_id (error 26).
            if session_key != existing_sid:
                compact_gen = await _compact_retry_stream(
                    account,
                    pool,
                    session_key,
                    messages,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    tool_mode,
                    tool_schemas,
                    include_usage,
                    context_seq,
                    request_id,
                    trace,
                    chunk_id,
                    created,
                    model,
                    user=user,
                    tools=tools,
                    tool_choice=tool_choice,
                    response_format=response_format,
                )
                if compact_gen is not None:
                    async for item in compact_gen:
                        yield item
                    return
                _drop_session(pool, account, session_key)
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {
                            "message": "input exceeds limit: message too long, shorten the message and try again",
                            "finish_reason": INPUT_EXCEEDS_LIMIT,
                        },
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                    }
                )
                yield _sse(
                    {
                        "id": chunk_id,
                        "session_id": None,
                        "object": "chat.completion.chunk",
                        "choices": [],
                    }
                )
                yield "data: [DONE]\n\n"
                return

            cont_parent = parent_message_id or rec.id or response_message_id
            log.info("input_exceeds_limit: starting continuation (parent=%s)", cont_parent)
            for round_num in range(MAX_CONTINUE_ROUNDS):
                cont_rec = await _collect_continuation(
                    account,
                    session,
                    cont_parent,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    request_id=request_id,
                    trace=trace,
                    model=model,
                )
                if cont_rec is None:
                    log.warning("continuation round %d: no response (parent=%s)", round_num + 1, cont_parent)
                    break
                log.info(
                    "continuation round %d: id=%s content_len=%d still_exceeds=%s",
                    round_num + 1,
                    cont_rec.id,
                    len(cont_rec.content or ""),
                    _is_input_exceeds_limit(cont_rec),
                )
                rec.extend_with(cont_rec)
                cont_parent = cont_rec.id or cont_parent
                if cont_rec.content:
                    if tool_mode:
                        content_buf += cont_rec.content
                    else:
                        yield _sse(
                            {
                                "id": chunk_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": cont_rec.content}, "finish_reason": None}],
                            }
                        )
                if cont_rec.reasoning:
                    yield _sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"reasoning_content": cont_rec.reasoning}, "finish_reason": None}],
                        }
                    )
                if not _is_input_exceeds_limit(cont_rec):
                    break
            else:
                cont_exhausted = True
                log.warning(
                    "continuation exhausted after %d rounds (last_parent=%s)",
                    MAX_CONTINUE_ROUNDS,
                    cont_parent,
                )
        # Input message itself is too long, not the whole conversation. Keep the
        # session and report the error honestly instead of switching sessions.
        if _is_input_exceeds_limit(rec) and not (rec.content or rec.reasoning):
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": "input exceeds limit: message too long, shorten the message and try again",
                        "finish_reason": INPUT_EXCEEDS_LIMIT,
                    },
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
            )
            yield _sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return
        if not (rec.content or rec.reasoning) and rec.hint_error:
            hint = rec.hint_error
            if _is_rate_limit_hint(rec):
                _log_rate_limit_hint(
                    rec,
                    account,
                    where="stream",
                    session_id=session_key,
                    request_id=request_id,
                    model=model,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {
                        "message": hint.get("message") or "DeepSeek server is busy, try again later",
                        "finish_reason": hint.get("finish_reason"),
                    },
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": hint.get("finish_reason") or "error",
                        }
                    ],
                }
            )
            yield _sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return

        # A blank reply is never a success: a content-policy refusal or a bare
        # notice arrives with no content, and reporting it as a normal "stop"
        # makes the client resend the same request indefinitely.
        blank_reason = blank_answer_reason(rec)
        if blank_reason is not None:
            log.warning(
                "empty answer where=stream session=%s request=%s reason=%s",
                session_key,
                request_id,
                blank_reason,
            )
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "error": {"message": blank_reason},
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": blank_answer_finish_reason(rec),
                        }
                    ],
                }
            )
            yield _sse(
                {
                    "id": chunk_id,
                    "session_id": session_key,
                    "object": "chat.completion.chunk",
                    "choices": [],
                }
            )
            yield "data: [DONE]\n\n"
            return

        session.accumulated_tokens = max(getattr(session, "accumulated_tokens", 0) or 0, rec.accumulated_tokens)
        account.sessions.touch_last_message(session_key, rec.id or response_message_id)
        if context_seq:
            pool.index_context(session_key, context_seq)
        log.info("deepseek completion OK (%.0fms)", (time.monotonic() - started) * 1000)

        if tool_mode and trace is not None:
            trace.upstream(request_id, content=content_buf, reasoning=None, session_id=session_key, model=model)
        if tool_mode:
            parsed = toolemu.parse_tool_calls(content_buf, tool_schemas)
            if parsed is None:
                issue, _detail = _classify_trace(content_buf, tool_schemas)
                parsed = await _repair_unparsed(
                    account,
                    session,
                    rec.id or response_message_id,
                    model_type,
                    thinking,
                    search,
                    ref_file_ids,
                    tool_schemas,
                    request_id,
                    trace,
                    model,
                    tool_choice,
                    issue,
                )
                if parsed is not None:
                    content_buf = parsed[1] or content_buf
            if parsed is not None:
                tool_calls, tool_text = parsed
                issues = _validate_parsed(tool_calls, tool_schemas)
                if issues:
                    repaired = await _repair_tool_calls(
                        account,
                        session,
                        rec.id or response_message_id,
                        issues,
                        model_type,
                        thinking,
                        search,
                        ref_file_ids,
                        tool_schemas,
                        request_id,
                        trace,
                        model,
                    )
                    if repaired is not None:
                        tool_calls, tool_text = repaired
                for delta in toolemu.tool_call_deltas(tool_calls, tool_text):
                    yield _sse(
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
                    yield _sse(
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
                finish = _finish_reason(rec.status)
        else:
            finish = _finish_reason(rec.status)

        if tool_mode and trace is not None:
            _trace_parse(trace, request_id, content_buf, tool_schemas, session_key, model, degenerate=degenerate_aborted)

        final_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
        if cont_exhausted:
            final_chunk["x_continuation_truncated"] = True
        yield _sse(final_chunk)
        _record_deepseek_usage(
            session,
            model,
            count_messages_tokens(messages) if messages else 0,
            user=user,
            session_key=session_key,
        )
        if include_usage:
            yield _sse(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": _deepseek_usage(getattr(session, "accumulated_tokens", 0)),
                }
            )
        yield _sse(
            {
                "id": chunk_id,
                "session_id": session_key,
                "object": "chat.completion.chunk",
                "choices": [],
            }
        )
        yield "data: [DONE]\n\n"


async def _stream_guard(gen, model: str):
    try:
        async for item in gen:
            yield item
    except AccountPoolBusy:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        yield _sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "error": {"message": "all accounts are busy, try again later"},
                "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
            }
        )
        yield _sse(
            {
                "id": chunk_id,
                "session_id": None,
                "object": "chat.completion.chunk",
                "choices": [],
            }
        )
        yield "data: [DONE]\n\n"
