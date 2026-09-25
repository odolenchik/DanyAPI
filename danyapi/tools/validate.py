from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Issue kinds, most severe first. Mirrors the priority contract of
# ``danyapi.tooltrace.classify`` so a tool-call verdict is reported the same way
# whether it comes from live validation or from the diagnostic classifier.
_ISSUE_ORDER = ("unknown_tool", "json_malformed", "missing_required", "extra_argument", "wrong_type")


def type_matches(value: Any, expected: str | None) -> bool:
    """Does ``value`` satisfy a JSON Schema type name from ``tool_schema_map``?"""
    if not expected:
        return True
    if expected == "integer":
        # JSON integers only; booleans are a distinct type in Python.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected in ("array", "object"):
        return isinstance(value, list) or isinstance(value, dict)
    return True


@dataclass
class ValidationIssue:
    kind: str
    detail: str


def _call_name_args(call: Any) -> tuple[str | None, Any]:
    if isinstance(call, dict):
        return call.get("name"), call.get("arguments", {})
    name = getattr(call, "name", None)
    if name is not None:
        return name, getattr(call, "arguments", {})
    return None, {}


def _parse_args(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Decode ``arguments`` into a dict, or report why it cannot be decoded."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}, "json_malformed"
        if not isinstance(parsed, dict):
            return {}, "wrong_type"
        return parsed, None
    if isinstance(raw, dict):
        return raw, None
    return {}, None


def _check_args(name: str | None, args: dict[str, Any], schema: dict[str, Any]) -> list[ValidationIssue]:
    """Per-key and required-key checks against one tool's parameter map."""
    issues: list[ValidationIssue] = []
    allowed = set(schema.keys()) - {"__required__"}
    for key, value in args.items():
        if key not in allowed:
            issues.append(ValidationIssue("extra_argument", f"{name}.{key}"))
            continue
        expected = schema.get(key)
        if not type_matches(value, expected):
            issues.append(ValidationIssue("wrong_type", f"{name}.{key}={value!r} expected {expected}"))
    required = schema.get("__required__")
    if isinstance(required, (set, frozenset, list, tuple)):
        for key in required:
            if key not in args:
                issues.append(ValidationIssue("missing_required", f"{name}.{key}"))
    return issues


def validate_call(call: Any, schema: dict[str, Any] | None) -> list[ValidationIssue]:
    """Validate one call's arguments against a single tool's parameter map.

    ``schema`` maps parameter names to JSON Schema type names, plus the reserved
    ``__required__`` key holding the set of mandatory parameter names (see
    ``tool_schema_map``). ``None`` means "no schema for this tool" — arguments are
    still checked for being well-formed JSON, but no per-parameter checks run.
    """
    name, args_raw = _call_name_args(call)
    args, err = _parse_args(args_raw)
    if err == "json_malformed":
        return [ValidationIssue("json_malformed", str(name or "?"))]
    if err == "wrong_type":
        return [ValidationIssue("wrong_type", f"{name}: arguments not an object")]
    if schema is None:
        return []
    return _check_args(name, args, schema)


def validate_calls(calls: Any, tool_schemas: dict[str, dict[str, Any]] | None) -> list[ValidationIssue]:
    """Validate every call against the client-declared tool schemas.

    Issue order within a call mirrors the historical ``classify`` behaviour: a
    malformed payload is reported before an unknown tool name, even though the
    aggregate priority (``_ISSUE_ORDER``) still ranks unknown_tool highest.
    """
    schemas = tool_schemas or {}
    issues: list[ValidationIssue] = []
    for call in calls or []:
        if not isinstance(call, dict) and not hasattr(call, "name"):
            continue
        name, args_raw = _call_name_args(call)
        args, err = _parse_args(args_raw)
        if err == "json_malformed":
            issues.append(ValidationIssue("json_malformed", str(name or "?")))
            continue
        if err == "wrong_type":
            issues.append(ValidationIssue("wrong_type", f"{name}: arguments not an object"))
            continue
        schema = schemas.get(name) if isinstance(name, str) else None
        if schema is None and schemas:
            issues.append(ValidationIssue("unknown_tool", f"tool '{name}' not declared"))
            continue
        if schema is not None:
            issues.extend(_check_args(name, args, schema))
    return issues


def summarize(issues: list[ValidationIssue]) -> tuple[str, Any]:
    """Collapse issues into the ``(issue, detail)`` contract of ``classify``."""
    if not issues:
        return "ok", None
    kinds = {issue.kind for issue in issues}
    issue = next((kind for kind in _ISSUE_ORDER if kind in kinds), "ok")
    detail = "; ".join(item.detail for item in issues[:5]) or None
    return issue, detail


def _choice_mode(tool_choice: Any) -> str:
    """Classify an OpenAI ``tool_choice`` value as ``force`` / ``none`` / ``auto``.

    ``"required"`` and a specific function name (bare string or an OpenAI
    ``{"function": {"name": ...}}`` object) force a call; ``"none"`` forbids one;
    everything else (``"auto"``, ``None``, an empty string) leaves it optional.
    """
    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return "none"
        if tool_choice in ("", "auto"):
            return "auto"
        return "force"
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn["name"]:
            return "force"
    return "auto"


def should_retry_unparsed(tool_choice: Any, issue: str | None, *, retry_auto: bool = False) -> bool:
    """Whether an unparsed tool-mode reply warrants one repair retry.

    ``parse_tool_calls`` returned no call. That is only worth re-asking upstream
    when the model was *supposed* to call a tool:

    * ``tool_choice`` forces a call (``"required"`` or a named function) — always
      retry: the model had no prose option, so a parse failure is a mangle;
    * ``"none"`` forbids tools — never retry, an unparsed reply is just text;
    * otherwise retry only when ``retry_auto`` is set *and* the classifier judged
      the text a malformed call (``"json_malformed"``), never for ordinary prose
      (``"no_tool_call"``).

    The caller supplies the classifier verdict so this stays a pure decision and a
    normal answer is never re-sent upstream.
    """
    mode = _choice_mode(tool_choice)
    if mode == "force":
        return True
    if mode == "none":
        return False
    return retry_auto and issue == "json_malformed"


def build_repair_prompt(issues: list[ValidationIssue]) -> str:
    """A follow-up instruction asking the model to redo a rejected tool call.

    Kept deliberately short: it is appended to the same upstream session, so the
    model already has its own broken answer (and the tool schema) in context. We
    only need to say what was wrong and that the reply must be the corrected JSON.
    """
    problems = "\n".join(f"- {issue.detail}" for issue in issues[:5])
    return (
        "Your previous reply did not pass tool-call validation:\n"
        f"{problems}\n\n"
        "Reply with ONLY the corrected JSON object in the exact same format "
        '(a single object with key "tool_calls", an array of {"name", "arguments"}). '
        "Fix the listed problems, keep every already-valid argument, and add no prose, "
        "no markdown fences, and no extra keys."
    )
