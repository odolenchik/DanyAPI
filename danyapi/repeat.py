"""Repeat guard: stop a turn that keeps emitting the same tool call.

The degenerate guard in `danyapi/api/streaming.py` aborts a single reply that
collapsed into repeating an empty container. This module covers the other
failure shape: every reply is well formed, but the model asks for the exact same
call round after round until the context runs out (logged sessions `a2b6de5d`
with 14 consecutive turns and `953c0ca4` with 10).

The counter lives on the upstream session (`DeepSeekSession.extra`) so it spans
requests, and any different call resets it.
"""

from __future__ import annotations

import json
from typing import Any

_STATE_KEY = "repeat_guard"


def call_signature(calls: Any) -> str | None:
    """A stable signature for a set of calls, or None when there are none.

    Argument objects are re-serialised with sorted keys so the same call written
    with a different key order still matches.
    """
    if not calls:
        return None
    parts: list[str] = []
    for call in calls:
        name = getattr(call, "name", None)
        if not isinstance(name, str) or not name:
            return None
        raw = getattr(call, "arguments", "") or ""
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        try:
            normalized = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            normalized = raw
        parts.append(f"{name}:{normalized}")
    return "|".join(parts)


def note_calls(session: Any, calls: Any, threshold: int) -> bool:
    """Record one round of calls and report whether the threshold is hit.

    Returns False when the session cannot hold state, so the guard never aborts
    a request it cannot reason about.
    """
    state = getattr(session, "extra", None)
    if not isinstance(state, dict):
        return False
    signature = call_signature(calls)
    if signature is None:
        state.pop(_STATE_KEY, None)
        return False
    current = state.get(_STATE_KEY)
    if isinstance(current, dict) and current.get("signature") == signature:
        try:
            count = int(current.get("count") or 1) + 1
        except (TypeError, ValueError):
            count = 2
    else:
        count = 1
    state[_STATE_KEY] = {"signature": signature, "count": count}
    return count >= threshold


def reset(session: Any) -> None:
    """Forget the tracked call."""
    state = getattr(session, "extra", None)
    if isinstance(state, dict):
        state.pop(_STATE_KEY, None)
