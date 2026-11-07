"""Diagnostic NDJSON tool-call trace logger.

Accumulates one line per correlated event (client / dany / upstream / parse / continuation)
sharing a single ``request_id`` so that, across all sessions, we can see the
exact patterns in which providers mangle tool calls. Kept separate from the
rotating application log so history of mangling is not overwritten by rotation.

The whole module degrades to no-op when disabled (no file configured), and every
write is wrapped so a logging failure never breaks a request.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _truncate(value: Any, limit: int) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text[:limit]


def _tool_names(tools: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(tools, Sequence) or isinstance(tools, str):
        return names
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else (tool.get("name") if isinstance(tool, dict) else None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


class ToolTraceLogger:
    """Append-only NDJSON logger correlating tool-call events by request_id."""

    def __init__(self, file_path: str | Path, max_bytes: int = 50 * 1024 * 1024, backup_count: int = 10) -> None:
        raw = "" if file_path is None else str(file_path)
        self._raw_path = raw
        self.file_path = Path(raw) if raw.strip() else Path(".")
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        # An empty/whitespace path means the logger is disabled (no-op).
        return self._raw_path.strip() != ""

    def _write_line(self, record: dict) -> None:
        if not self.enabled:
            return
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                parent = self.file_path.parent
                if str(parent):
                    parent.mkdir(parents=True, exist_ok=True)
                # Rotate first so the active file is always kept under max_bytes.
                if self.backup_count and self.file_path.exists() and self.file_path.stat().st_size > self.max_bytes:
                    for i in range(self.backup_count - 1, 0, -1):
                        src = self._backup_path(i)
                        dst = self._backup_path(i + 1)
                        if src.exists():
                            src.replace(dst)
                    rotated = self._backup_path(1)
                    self.file_path.replace(rotated)
                with open(self.file_path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def _backup_path(self, index: int) -> Path:
        return self.file_path.with_name(f"{self.file_path.name}.{index}")

    def _event(self, request_id: str | None, actor: str, session_id: Any, model: Any, **fields: Any) -> dict:
        record = {
            "ts": time.time(),
            "request_id": request_id or "",
            "actor": actor,
            "session_id": session_id if isinstance(session_id, str) else None,
            "model": model if isinstance(model, str) else None,
        }
        record.update(fields)
        self._write_line(record)

    def client(
        self,
        request_id: str | None = None,
        *,
        session_id: Any = None,
        model: Any = None,
        tools: Any = None,
        tool_choice: Any = None,
        messages_count: int | None = None,
    ) -> None:
        """What the client (e.g. Kimi) declared for this request."""
        self._event(
            request_id,
            "client",
            session_id,
            model,
            tool_names=_tool_names(tools),
            tool_choice=tool_choice if isinstance(tool_choice, str) else None,
            messages_count=messages_count,
        )

    def dany(self, request_id: str | None = None, *, prompt: Any = "", session_id: Any = None, model: Any = None) -> None:
        """Final prompt Dany actually sent to the upstream model."""
        self._event(request_id, "dany", session_id, model, prompt=_truncate(prompt, 4000))

    def upstream(
        self,
        request_id: str | None = None,
        *,
        content: Any = "",
        reasoning: Any = None,
        session_id: Any = None,
        model: Any = None,
        hint: Any = None,
        notice: Any = None,
    ) -> None:
        """Raw provider response (content + reasoning), before tool-call parsing.

        ``hint`` and ``notice`` carry the upstream's own explanation when the
        reply came back empty or refused. Without them a blank answer in the
        trace cannot be told apart from a throttle, a refusal or a dropped
        stream — which is exactly what decides whether retrying is safe.
        """
        self._event(
            request_id,
            "upstream",
            session_id,
            model,
            content=_truncate(content, 5000),
            reasoning=_truncate(reasoning, 500) if reasoning else None,
            hint=_truncate(hint, 500) if hint else None,
            notice=_truncate(notice, 500) if notice else None,
        )

    def parse(self, request_id: str | None = None, *, result: Any = None, issue: Any = None, session_id: Any = None, model: Any = None) -> None:
        """How the raw response was parsed into tool calls (and any mangle class)."""
        self._event(request_id, "parse", session_id, model, result=result if isinstance(result, str) else None, issue=issue if isinstance(issue, str) else None)

    def continuation(
        self,
        request_id: str | None = None,
        *,
        session_id: Any = None,
        model: Any = None,
        status_code: int | None = None,
        detail: Any = "",
        attempt: int | None = None,
    ) -> None:
        """A continuation ('Continue') request to the upstream provider after input_exceeds_limit."""
        self._event(
            request_id,
            "continuation",
            session_id,
            model,
            status_code=status_code,
            detail=_truncate(detail, 1000),
            attempt=attempt,
        )


_CALL_MARKERS = ("tool_calls", "invoke", "<function", '"name"', "'name'")


def _looks_like_call(text: str) -> bool:
    """Heuristic: does non-JSON text look like a (broken) tool-call attempt?"""
    compact = text.lower()
    return any(marker in compact for marker in _CALL_MARKERS)


def _classify_via_parser(text: str, tool_schemas: dict[str, dict[str, Any]]) -> tuple[str, Any] | None:
    """Validate a response with the real parser instead of guessing at its shape.

    ``classify`` sees the raw provider text, but the authoritative answer to
    "could the client act on this?" comes from ``parse_tool_calls``: it also
    understands non-JSON shapes (prose wrapping a call, several objects in a
    row, bare arrays, repaired truncation). Returns ``None`` when the parser
    produced no call at all, so the caller can fall back to its own verdict.
    """
    from .tools.parse import parse_tool_calls

    parsed = parse_tool_calls(text, tool_schemas)
    if parsed is None:
        return None
    calls, _wrapper = parsed
    as_dicts = [{"name": call.name, "arguments": call.arguments} for call in calls]
    return _check_calls(as_dicts, tool_schemas)


def classify(content: Any, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[str, Any]:
    """Classify a raw provider response against the client's declared tools.

    Returns ``(issue, detail)`` where ``issue`` is one of:
      ok, no_tool_call, json_malformed, extra_argument, wrong_type, unknown_tool.
    ``detail`` carries the offending key / tool name for diagnostics.

    ``json_malformed`` is reserved for responses the parser genuinely cannot
    turn into a call. Text that merely fails a bare ``json.loads`` — prose
    around a call, several objects in a row, a repaired truncation — is routed
    through ``parse_tool_calls`` first, so a recoverable call is not misreported
    as a parse failure.
    """
    if not isinstance(content, str) or not content.strip():
        return "no_tool_call", None

    text = content.strip()
    # Strip common wrappers so we judge the actual call payload.
    stripped = text
    while True:
        s2 = _strip_fences(stripped).strip()
        if s2 == stripped:
            break
        stripped = s2
    tool_schemas = tool_schemas or {}

    try:
        obj = json.loads(stripped)
    except ValueError:
        # Not pure JSON — prose (no tool-call markers) vs a mangled structure.
        if _looks_like_call(text):
            # XML invoke / DSML are valid non-JSON formats handled by parse_tool_calls;
            # they aren't "malformed" just because json.loads fails on them. DSML blocks
            # still get their arguments checked against the schema (see _classify_dsml).
            lower = text.lower()
            if "<invoke" in lower or "dsml" in lower:
                return _classify_dsml(text, tool_schemas)
            # Non-JSON but not DSML: the parser also handles several shapes that
            # json.loads rejects, so consult it before declaring a parse failure.
            recovered = _classify_via_parser(text, tool_schemas)
            if recovered is not None:
                return recovered
            return "json_malformed", _truncate(text, 200)
        return "no_tool_call", _truncate(text, 200)

    calls = None
    if isinstance(obj, dict):
        # Match the parser's accepted wrapper keys so a payload that parses is not
        # still reported as no_tool_call just because it used a provider alias
        # (``calls`` / ``tools_calls``) instead of the canonical ``tool_calls``.
        inner = None
        for key in ("tool_calls", "tools_calls", "calls", "function_calls"):
            if key in obj:
                inner = obj[key]
                break
        if isinstance(inner, list):
            calls = inner
        elif isinstance(obj.get("name"), str):
            calls = [obj]
    if not calls:
        return "no_tool_call", _truncate(text, 200)

    return _check_calls(calls, tool_schemas)


def _classify_dsml(text: str, tool_schemas: dict[str, dict[str, Any]]) -> tuple[str, Any]:
    """Classify a DSML-tagged tool call by validating its parameters.

    DSML is the primary tool-call format DeepSeek emits, and unlike the JSON path it
    previously skipped argument validation entirely, so mangled parameter names (e.g.
    ``file_path`` instead of ``path``, ``offset`` instead of ``line_offset``) went
    unreported. Reuse the parser's own DSML decoding, then run the same schema check.
    """
    from .tools.parse import _parse_dsml

    dsml_calls, _wrapper = _parse_dsml(text, tool_schemas)
    if not dsml_calls:
        # A bare XML <invoke> (not the DSML variant) — still a valid call format; leave
        # it as "ok" rather than mislabelling it, matching the pre-existing behaviour.
        return "ok", None
    calls: list[dict[str, Any]] = []
    for tc in dsml_calls:
        args: dict[str, Any] = {}
        if isinstance(tc.arguments, str):
            try:
                parsed = json.loads(tc.arguments)
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict):
                args = parsed
        calls.append({"name": tc.name, "arguments": args})
    return _check_calls(calls, tool_schemas)


def _check_calls(calls: Any, tool_schemas: dict[str, dict[str, Any]]) -> tuple[str, Any]:
    """Validate a list of tool-call dicts (name + arguments) against the schema.

    Delegates to :mod:`danyapi.tools.validate` so live validation and this
    diagnostic classifier share one implementation and cannot drift apart.
    """
    from .tools.validate import summarize, validate_calls

    return summarize(validate_calls(calls, tool_schemas))


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped
