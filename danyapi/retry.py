# Shared retry/error constants and helpers.
#
# MAX_RETRIES, RETRY_BACKOFF_SEC, RETRY_BACKOFF_MAX_SEC,
# RETRYABLE_HTTP_STATUSES, _is_retryable_http, _drop_session — all duplicated
# between the DeepSeek API module and the Qwen API module; they live here so
# both sides can import them as local names (for monkeypatching in tests) while
# sharing a single source of truth.

from __future__ import annotations

import logging

from fastapi import HTTPException

log = logging.getLogger("danyapi.retry")

MAX_RETRIES: int = 5
RETRY_BACKOFF_SEC: float = 1.0
RETRY_BACKOFF_MAX_SEC: float = 8.0

RETRYABLE_HTTP_STATUSES: set[int] = {408, 425, 429, 500, 502, 503, 504}

# DeepSeek reports its per-account "Messages too frequent" throttle as a stream
# hint rather than an HTTP status. Unlike the reasons in
# RETRYABLE_FINISH_REASONS it is deliberately NOT retried: the block lasts
# minutes, so hammering the same account only prolongs it. It is recognised here
# purely so the event becomes visible in the log — see _log_rate_limit_hint.
RATE_LIMIT_HINT_REASONS: set[str] = {"too_frequent"}
RATE_LIMIT_HINT_MARKERS: tuple[str, ...] = ("too frequent", "rate limit", "too many requests")

# Upstream busy states, reported as hints. They are retried within a turn and,
# once the retries run out, must stay 429 — the account is healthy, it is just
# occupied. Anything outside both this set and RATE_LIMIT_HINT_REASONS that
# arrives as an empty-answer hint is a refusal of the content itself.
RETRYABLE_FINISH_REASONS: set[str] = {
    "expert_busy_use_default",
    "parallel_chat_limit",
    "server_busy",
    "busy",
}

# Windows (seconds) over which request starts are counted when a rate-limit hint
# is logged. Diagnostics only — nothing reads them to make decisions, they are
# just the traffic figures a pacing threshold would be derived from.
START_WINDOWS: tuple[float, ...] = (10.0, 30.0, 60.0)
# Widest window; request starts older than this are dropped from the buffer.
START_BUFFER_SEC: float = max(START_WINDOWS)


def _is_retryable_http(exc: HTTPException) -> bool:
    return exc.status_code in RETRYABLE_HTTP_STATUSES


# DeepSeek answers a stale session pointer with biz error 26 ("invalid message
# id"): the message we asked it to continue from no longer exists, so the
# session cannot be resumed. This is permanent, not transient — retrying just
# resends the same dead id five times and leaves the session broken. The session
# is dropped instead, so the next request starts a fresh one.
STALE_SESSION_MARKERS: tuple[str, ...] = ("error 26", "invalid message id")


def _is_stale_session_error(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    lowered = detail.lower()
    return any(marker in lowered for marker in STALE_SESSION_MARKERS)


def _is_rate_limit_hint(rec) -> bool:
    """True when a hint means "your account is throttled for sending too fast"."""
    hint = getattr(rec, "hint_error", None)
    if not hint:
        return False
    reason = hint.get("finish_reason")
    if isinstance(reason, str) and reason in RATE_LIMIT_HINT_REASONS:
        return True
    message = hint.get("message")
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in RATE_LIMIT_HINT_MARKERS)


def _log_rate_limit_hint(
    rec,
    account,
    *,
    where: str,
    session_id: str | None = None,
    request_id: str | None = None,
    model: str | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Record a rate-limit hint together with how fast the account was driven.

    Diagnostics only — no retry or delay is changed here. The counts per window
    are the numbers a pacing threshold would be derived from, so log the hint
    even when nothing else reacts to it.
    """
    hint = getattr(rec, "hint_error", None) or {}
    counts = {f"starts_{int(window)}s": account.recent_request_count(window) for window in START_WINDOWS}
    fields = " ".join(f"{name}={value}" for name, value in counts.items())
    # Seconds between the previous request's end and this request's start. The
    # starts_* counters alone hide the failure mode this diagnoses: a long
    # request makes starts look far apart while the account is re-hit seconds
    # after the previous stream closed.
    gap = getattr(account, "last_end_gap", None)
    log.warning(
        "rate limit hint (%s) where=%s account=%s session=%s request=%s model=%s reason=%s message=%.200s elapsed_ms=%s end_to_start_gap=%s %s",
        hint.get("finish_reason"),
        where,
        getattr(account, "label", "?"),
        session_id,
        request_id,
        model,
        hint.get("finish_reason"),
        hint.get("message") or "",
        f"{elapsed_ms:.0f}" if elapsed_ms is not None else "-",
        f"{gap:.1f}" if gap is not None else "-",
        fields,
    )


def _drop_session(pool, account, session_key: str) -> None:
    pool.forget(session_key)
    pool.forget_context(session_key)
    account.sessions.forget(session_key)


# Upstream can answer with no content at all: a content-policy refusal, or a
# bare notice toast. Reported as a plain "stop" with an empty message, that
# looks like a successful blank answer, and the client just retries the same
# request forever (Kimi did, re-uploading the same refused image each time).
# A blank answer is therefore never passed through as success.
CONTENT_FILTER_STATUS = "CONTENT_FILTER"
EMPTY_ANSWER_MESSAGE = "DeepSeek returned an empty response"
REFUSAL_MESSAGE = "DeepSeek refused the message (content policy); nothing was sent"


def blank_answer_reason(rec) -> str | None:
    """Return why a reply is empty, or None when it actually carries text.

    The refusal text is preferred over a generic message: it is the only thing
    that tells the user *why* nothing came back.
    """
    if getattr(rec, "content", "") or getattr(rec, "reasoning", ""):
        return None
    hint = getattr(rec, "hint_error", None) or {}
    notice = getattr(rec, "notice", None) or {}
    for payload in (hint, notice):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if getattr(rec, "status", None) == CONTENT_FILTER_STATUS:
        return REFUSAL_MESSAGE
    return EMPTY_ANSWER_MESSAGE


def blank_answer_finish_reason(rec) -> str:
    """The OpenAI finish_reason to report for an empty answer."""
    hint = getattr(rec, "hint_error", None) or {}
    notice = getattr(rec, "notice", None) or {}
    for payload in (hint, notice):
        reason = payload.get("finish_reason")
        if isinstance(reason, str) and reason:
            return reason
    if getattr(rec, "status", None) == CONTENT_FILTER_STATUS:
        return "content_filter"
    return "error"


def stream_refusal_message(rec) -> str:
    """The text to send as a delta when the content itself was refused."""
    return blank_answer_reason(rec) or REFUSAL_MESSAGE


def stream_refusal_finish_reason(rec) -> str:
    """The finish_reason to put on a stream error chunk for an empty answer.

    OpenAI clients decide retryability from this field alone: Kimi maps
    ``content_filter`` to ``filtered`` (never retried) and anything it does not
    recognise to ``other``, which it retries. DeepSeek's own reason
    (``file_audit_reject``) is unknown to such a client, so a content refusal is
    reported as ``content_filter``; the real reason stays in
    ``error.finish_reason`` and in the message.
    """
    if is_content_refusal(rec):
        return "content_filter"
    return blank_answer_finish_reason(rec)


def is_content_refusal(rec) -> bool:
    """True when the upstream refused the *content*, not the request rate.

    A moderation refusal ("Content violates terms", finish_reason
    ``file_audit_reject``) arrives as the same empty-answer hint as a throttle.
    It must not be reported as 429: the request will never succeed, so a client
    that honours 429 retries it forever, re-uploading the same refused file.
    A hint whose reason is a throttle, or whose text reads like one, is not a
    refusal.
    """
    if getattr(rec, "content", "") or getattr(rec, "reasoning", ""):
        return False
    hint = getattr(rec, "hint_error", None)
    if not hint:
        return False
    if _is_rate_limit_hint(rec):
        return False
    reason = hint.get("finish_reason")
    if isinstance(reason, str) and reason in RETRYABLE_FINISH_REASONS:
        return False
    return True
