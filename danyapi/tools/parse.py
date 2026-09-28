from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .prompt import ToolCall, _tool_function

_DSML_TAG = re.compile(
    r"<\s*/?\s*(?:\||\uff5c){2}\s*DSML\s*(?:\||\uff5c){2}[^>]*>"
    r"|(?:\||\uff5c){2}\s*DSML\s*(?:\||\uff5c){2}",
    re.IGNORECASE,
)


def _strip_dsml(text: str) -> str:
    if not text:
        return text
    return _DSML_TAG.sub(" ", text)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", stripped, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def _extract_embedded_fence(text: str) -> tuple[str, str] | None:
    """Find a fenced JSON block embedded within larger text.

    Returns (fenced_content, prose_surrounding) or None if no such block exists.
    The prose surrounding is everything outside the fence — used as wrapper text.
    """
    m = re.search(r"(```[a-zA-Z0-9_-]*\s*\n)(.+?)(\n?```)", text, re.DOTALL)
    if not m:
        return None
    inner = m.group(2).strip()
    if inner[:1] not in "{[":
        return None
    prose = (text[: m.start()] + " " + text[m.end() :]).strip()
    return inner, prose


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize_single_quotes(text: str) -> str:
    out: list[str] = []
    in_double = False
    in_single = False
    escaped = False
    for ch in text:
        if in_double:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if in_single:
            if escaped:
                if ch == "'":
                    out.append("'")
                elif ch == '"':
                    out.append('\\"')
                elif ch == "\\":
                    out.append("\\\\")
                else:
                    out.append("\\" + ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                out.append('"')
                in_single = False
            elif ch == '"':
                out.append('\\"')
            else:
                out.append(ch)
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
        elif ch == "'":
            in_single = True
            out.append('"')
        else:
            out.append(ch)
    return "".join(out)


# DeepSeek emits regex patterns with single backslashes inside JSON strings
# (e.g. "\.verify\(" instead of the valid "\\." / "\\("). Repair invalid escape
# sequences so json.loads accepts them. Valid JSON escapes: \" \\ \/ \b \f \n \r \t \uXXXX.
_VALID_ESCAPE_CHARS = '"\\/bfnrtu'


def _fix_invalid_escapes(text: str) -> str:
    """Double stray backslashes while leaving every valid escape sequence intact.

    A regex substitution cannot do this correctly: in ``\\\\|`` (a valid escaped
    backslash followed by a pipe) the naive ``\\([^...])`` pattern matches at the
    second backslash and rewrites the pair into three backslashes, producing a
    fresh ``Invalid \\escape`` where the input had none. That single bug made the
    last-resort repair fail on otherwise recoverable payloads (tooltrace
    request_ids 85c42f56, b9031792, 17d61eff). Scan left to right and consume ``\\\\``
    as one unit instead.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt == "\\" or nxt in _VALID_ESCAPE_CHARS:
            out.append(ch)
            if nxt:
                out.append(nxt)
                i += 2
            else:
                i += 1
            continue
        # Stray backslash (e.g. a regex ``\.`` or ``\(``): escape it.
        out.append("\\\\")
        i += 1
    return "".join(out)


def _quote_bare_keys(text: str) -> str:
    """Wrap unquoted object keys in double quotes.

    DeepSeek sometimes drops the quotes around the ``tool_calls`` wrapper key,
    emitting ``{tool_calls: [...]}`` (often inside an angle-bracket wrapper,
    ``<{tool_calls: ...}>``). Every existing repair leaves the bare key alone, so
    the payload reaches the client as plain text instead of a tool call. Only
    identifiers sitting where a key is expected — right after ``{`` or ``,`` —
    are quoted, and never inside a string, so URLs and prose values are safe.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    expect_key = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch in "{,":
            out.append(ch)
            expect_key = True
            i += 1
            continue
        if expect_key:
            if ch in " \t\r\n":
                out.append(ch)
                i += 1
                continue
            if ch.isalpha() or ch == "_":
                j = i
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                k = j
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == ":":
                    out.append('"' + text[i:j] + '"')
                    i = j
                    expect_key = False
                    continue
            expect_key = False
        out.append(ch)
        i += 1
    return "".join(out)


def _loads_lenient(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # DeepSeek sometimes emits raw control characters (newlines/tabs) inside
    # JSON string values instead of \n escapes — strict=False tolerates these.
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    stripped = _strip_trailing_commas(text)
    if stripped != text:
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    normalized = _normalize_single_quotes(stripped)
    if normalized != stripped:
        try:
            return json.loads(normalized)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Unquoted keys: `{tool_calls: [...]}` instead of `{"tool_calls": [...]}`.
    # An angle-bracket wrapper (`<{tool_calls: ...}>`) needs no separate handling:
    # the balanced-brace scan ignores the surrounding `<`/`>` characters.
    quoted = _quote_bare_keys(normalized)
    if quoted != normalized:
        try:
            return json.loads(quoted)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Repair invalid escape sequences (regex patterns with single backslashes).
    escaped = _fix_invalid_escapes(stripped)
    if escaped != stripped:
        try:
            return json.loads(escaped)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Combined: unquoted keys and/or invalid escapes on a truncated payload.
    escaped_quoted = _quote_bare_keys(escaped)
    if escaped_quoted != escaped:
        try:
            return json.loads(escaped_quoted)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    base = escaped_quoted if escaped_quoted != escaped else (escaped if escaped != stripped else text)
    fixed = _fix_unbalanced_json(base)
    if fixed is not None and fixed != text:
        try:
            return json.loads(fixed)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    raise ValueError("invalid json")


def _fix_unbalanced_json(text: str) -> str | None:
    stack: list[str] = []
    insert_before: dict[int, int] = {}
    drop: set[int] = set()
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            else:
                drop.add(i)
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            elif stack:
                count = 0
                while stack and stack[-1] != "[":
                    count += 1
                    stack.pop()
                insert_before[i] = count
                if stack:
                    stack.pop()
                else:
                    drop.add(i)
            else:
                drop.add(i)

    if not insert_before and not drop and not stack and not in_string:
        return None

    result: list[str] = []
    for i, ch in enumerate(text):
        if i in insert_before:
            result.append("}" * insert_before[i])
        if i in drop:
            continue
        result.append(ch)
    if in_string:
        if escaped:
            result.append("\\")
        result.append('"')
    result.append("".join("}" if opening == "{" else "]" for opening in reversed(stack)))
    return "".join(result)


# DeepSeek sometimes drops the closing brace of individual tool-call objects inside
# a tool_calls array, producing e.g. {"name": "X", "arguments": {...}, {"name": "Y" ...}
# instead of the correct {"name": "X", "arguments": {...}}, {"name": "Y" ...}. Detect
# and repair by inserting the missing '}' before each ', {"name"' that follows an
# unclosed inner object.
def _fix_missing_call_braces(text: str) -> str | None:
    """Insert missing '}' between tool_calls array elements.

    DeepSeek sometimes omits the closing '}' for a call object, leaving only
    the arguments sub-object closed:
        {"name":"Grep","arguments":{"pattern":"x","-i":true}, {"name":...}}
    instead of:
        {"name":"Grep","arguments":{"pattern":"x","-i":true}}, {"name":...}

    Strategy: track brace depth. Between array elements the depth should be 1
    (inside the outer ``{tool_calls:...}`` object). If we encounter a ','
    followed by '{"name"' while still at depth > 1, an object is unclosed —
    insert '}' before that comma.
    """
    # Quick check: count braces outside strings to see if there's a deficit.
    open_count = 0
    close_count = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            open_count += 1
        elif ch == "}":
            close_count += 1
    deficit = open_count - close_count
    if deficit <= 0:
        return None

    # Walk through tracking depth. When a ',' is followed by '{"name"' (start of
    # a new call element) and our current depth > 1, insert '}' before the comma.
    result = list(text)
    inserted = 0
    in_string = False
    escaped = False
    depth = 0

    i = 0
    while i < len(result):
        ch = result[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue

        # At a ',' — check if next non-whitespace starts a new call element.
        if ch == "," and inserted < deficit:
            j = i + 1
            while j < len(result) and result[j] in " \t\n\r":
                j += 1
            snippet = "".join(result[j : j + 8])
            if snippet.startswith('{"name"') or snippet.startswith('{ "name"'):
                # Between elements we expect depth 1 (inside outer object).
                if depth > 1:
                    result.insert(i, "}")
                    inserted += 1
                    depth -= 1  # the inserted } closes an unclosed object
                    i += 2  # skip past inserted } and re-process the comma
                    continue
        i += 1

    if inserted == 0:
        return None
    fixed = "".join(result)
    # Verify it now has balanced braces.
    oc = fc = 0
    in_str = False
    esc = False
    for ch in fixed:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            oc += 1
        elif ch == "}":
            fc += 1
    return fixed if oc == fc else None


def _balanced_json(text: str) -> tuple[int, int] | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i
    return None


def _extract_json_object(text: str) -> tuple[dict, int, int] | None:
    stripped = _strip_fences(text)
    bounds = _balanced_json(stripped)
    if bounds is None:
        return None
    start, end = bounds
    try:
        obj = _loads_lenient(stripped[start : end + 1])
    except ValueError:
        return None
    if isinstance(obj, dict):
        return obj, start, end


def _is_empty_arguments(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return len(value) == 0
    if isinstance(value, str):
        return value.strip() in ("", "{}", "null")
    return False


def _flat_arguments(container: dict[str, Any]) -> dict[str, Any]:
    # DeepSeek v4-pro sometimes emits tool parameters as flat keys next to
    # `name` instead of nested under `arguments`. Collect them so the call
    # still carries real arguments instead of an empty object.
    return {key: value for key, value in container.items() if key not in ("name", "tool", "function", "arguments", "parameters", "id", "type")}


def _nested_arguments(container: dict[str, Any]) -> Any:
    # ``parameters`` is DeepSeek's synonym for ``arguments`` in the single-object
    # call form (tooltrace 6e02a45f: {"tool": "nmap", "parameters": {...}}).
    # Prefer ``arguments`` when present; otherwise fall back to ``parameters``.
    arguments = container.get("arguments")
    if _is_empty_arguments(arguments):
        parameters = container.get("parameters")
        if not _is_empty_arguments(parameters):
            return parameters
    return arguments


def _extract_one_call(item: Any) -> ToolCall | None:
    if not isinstance(item, dict):
        return None
    fn = item.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        arguments = _nested_arguments(fn)
        if _is_empty_arguments(arguments):
            flat = _flat_arguments(fn)
            if flat:
                arguments = flat
    else:
        name = item.get("name") or item.get("tool")
        arguments = _nested_arguments(item)
        if _is_empty_arguments(arguments):
            flat = _flat_arguments(item)
            if flat:
                arguments = flat
    if not isinstance(name, str) or not name:
        return None
    return ToolCall.create(name, arguments)


def _is_jsonish_arguments(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    if isinstance(value, str):
        return value.strip().startswith("{")
    return False


# DeepSeek v4-pro sometimes wraps the call array under a provider-side key other
# than the client's canonical ``tool_calls`` — observed ``calls`` (tooltrace
# 58d77038, 6d1557e5), ``tools_calls`` (tooltrace a397635c) and ``"tool calls"``
# with a space instead of underscore (tooltrace 7205b1ac). Accept them so the
# payload isn't discarded as unparsed:json_malformed purely for the key name.
_WRAPPED_CALL_KEYS = ("tool_calls", "tools_calls", "calls", "function_calls", "tool calls")


def _extract_wrapped_calls(obj: dict) -> list[ToolCall] | None:
    calls: list[ToolCall] = []
    raw: Any = None
    for key in _WRAPPED_CALL_KEYS:
        if key in obj:
            raw = obj[key]
            break
    if isinstance(raw, list):
        for item in raw:
            call = _extract_one_call(item)
            if call is not None:
                calls.append(call)
            elif isinstance(item, dict):
                # A nested wrapper element (e.g. tooltrace c204a696 emits a second
                # array element that is itself {"tool_calls": [...]}): recurse so
                # its inner calls are recovered instead of being dropped.
                nested = _extract_wrapped_calls(item)
                if nested:
                    calls.extend(nested)
    elif isinstance(raw, dict):
        call = _extract_one_call(raw)
        if call is not None:
            calls.append(call)
    else:
        legacy = obj.get("function_call")
        if isinstance(legacy, dict):
            call = _extract_one_call(legacy)
            if call is not None:
                calls.append(call)
    return calls or None


def _extract_calls(obj: dict) -> list[ToolCall] | None:
    calls = _extract_wrapped_calls(obj)
    if calls is not None:
        return calls
    name = obj.get("name") or obj.get("tool")
    if isinstance(name, str) and name and _is_jsonish_arguments(_nested_arguments(obj)):
        call = _extract_one_call(obj)
        if call is not None:
            return [call]
    return None


def _unescape_xml(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def _coerce_scalar(value: str, json_type: Any) -> Any:
    if not isinstance(value, str):
        return value
    # A parameter value that is itself a JSON array/object (e.g. an XML <parameter>
    # whose body is `[{"header": ...}]`) must stay structured — otherwise the client
    # receives a string where the schema expects an array and rejects the call.
    # Only apply the "starts with [{\"" heuristic when we don't know the expected type;
    # if the schema explicitly says "string", a value like `{"a":1}` is valid text.
    stripped = value.strip()
    if json_type in ("array", "object") or (not json_type and stripped[:1] in "[{"):
        try:
            return _loads_lenient(stripped)
        except ValueError:
            pass
    if json_type in ("integer", "number"):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            return value
    if json_type == "boolean":
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
        return value
    if json_type == "null":
        return None
    return value


def tool_schema_map(tools: list[Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not tools:
        return result
    for tool in tools:
        fn = _tool_function(tool)
        if not fn:
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            continue
        properties = params.get("properties")
        if not isinstance(properties, dict):
            continue
        required: set[str] = set()
        required_raw = params.get("required")
        if isinstance(required_raw, list):
            required = {item for item in required_raw if isinstance(item, str)}
        prop_types: dict[str, Any] = {}
        for prop, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            typ = spec.get("type")
            if isinstance(typ, list):
                for candidate in ("integer", "number", "boolean", "null", "string"):
                    if candidate in typ:
                        typ = candidate
                        break
            elif not typ or typ == "null":
                # anyOf / oneOf variant — e.g. {"anyOf": [{"type":"integer"},{"type":"null"}]}
                for wrapper in ("anyOf", "oneOf"):
                    subs = spec.get(wrapper)
                    if isinstance(subs, list):
                        for sub in subs:
                            if not isinstance(sub, dict):
                                continue
                            st = sub.get("type")
                            if isinstance(st, str) and st != "null":
                                typ = st
                                break
                        if typ:
                            break
            if typ:
                prop_types[prop] = typ
        if not properties or prop_types or required:
            # Keep the set of required parameter names alongside the type map so
            # empty-body <invoke> parsing can tell "no required args" apart from
            # "missing mandatory args" (e.g. ExitPlanMode with optional content).
            prop_types["__required__"] = required
            result[name] = prop_types
    return result


def _xml_invoke_arguments(body: str, param_types: dict[str, Any] | None = None) -> dict[str, Any] | None:
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            parsed = _loads_lenient(stripped)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    params: dict[str, Any] = {}
    for match in re.finditer(r'<parameter\s+name=["\']([^"\']+)["\'][^>]*>(.*?)</parameter>', body, re.DOTALL | re.IGNORECASE):
        params[match.group(1).strip()] = _coerce_scalar(_unescape_xml(match.group(2).strip()), (param_types or {}).get(match.group(1).strip()))
    if not params and "<parameter" not in body.lower():
        # No parameter tags at all — leave the legacy child-element / plain-text
        # fallback to handle this body.
        return None
    # A truncated invoke body ends inside an unterminated <parameter> value (the
    # closing </parameter> tag was never streamed). Recover it via the last
    # name="..." attribute followed by a '>' and no closing tag. The lazy [^<]*$
    # cannot be anchored to the true end when earlier parameters' closing tags are
    # present, so walk parameter openers in order instead.
    tail: re.Match[str] | None = None
    for pm in re.finditer(r'<parameter\s+name=["\']([^"\']+)["\'][^>]*>', body, re.IGNORECASE):
        if "</parameter" not in body[pm.end() :]:
            tail = pm
            break
    if tail:
        value = body[tail.end() :].split("</", 1)[0]
        params[tail.group(1).strip()] = _coerce_scalar(_unescape_xml(value.strip()), (param_types or {}).get(tail.group(1).strip()))
    return params


def _xml_invoke_legacy_children(body: str, param_types: dict[str, Any] | None = None) -> dict[str, Any] | None:
    params: dict[str, Any] = {}
    has_children = False
    for match in re.finditer(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\s*>(.*?)</\1>", body, re.DOTALL | re.IGNORECASE):
        params[match.group(1).strip()] = _coerce_scalar(_unescape_xml(match.group(2).strip()), (param_types or {}).get(match.group(1).strip()))
        has_children = True
    if has_children:
        # Handle truncated last element: an opening tag with no matching close extends to end of body.
        last_match_end = 0
        for m in re.finditer(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\s*>(.*?)</\1>", body, re.DOTALL | re.IGNORECASE):
            last_match_end = m.end()
        tail = re.search(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\s*>", body[last_match_end:])
        if tail:
            tag_name = tail.group(1).strip()
            value = body[last_match_end + tail.end() :]
            params[tag_name] = _coerce_scalar(_unescape_xml(value.strip()), (param_types or {}).get(tag_name))
        return params
    inner = _unescape_xml(body.strip())
    if inner:
        return {"content": inner}
    return None


def _xml_tag_attrs(body: str, param_types: dict[str, Any] | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    pattern = re.compile(r"([a-zA-Z_][a-zA-Z0-9_.-]*)\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
    for match in pattern.finditer(body):
        key = match.group(1)
        raw = match.group(2)
        value = raw[1:-1]
        attrs[key] = _coerce_scalar(_unescape_xml(value), (param_types or {}).get(key))
    return attrs


_INVOKE_OPENER_RE = re.compile(r"<(?:invoke|use_tool|tool_use)\s+name=([\"']?)([^\s>\"']+)\1\s*>", re.IGNORECASE)


def _xml_invoke_call(opener_start: int, text: str, tool_schemas: dict[str, dict[str, Any]] | None) -> tuple[ToolCall | None, int]:
    """Build a ToolCall from an <invoke> opener at ``opener_start``.

    The body spans from the opener's '>' up to the next invoke opener or end of
    text — this also covers truncated streams where </invoke> was never written,
    so a cut-off parameter value can still be recovered.

    Returns (call, block_end) where block_end is the index just past the consumed
    block (used for remainder/blanking bookkeeping).
    """
    opener = _INVOKE_OPENER_RE.match(text, opener_start)
    tool_name = opener.group(2)
    body_start = text.index(">", opener.end() - 1) + 1
    next_opener = re.search(r"<(?:invoke|use_tool|tool_use)\s+name=", text[body_start:], re.IGNORECASE)
    block_end = body_start + next_opener.start() if next_opener else len(text)
    body = text[body_start:block_end]
    # A complete invoke ends with </invoke>; the lazy close tag used to be part of
    # the old regex match. Cut the body there when present so trailing tags don't
    # leak into a plain-text argument value.
    close_tag = re.search(r"</(?:invoke|use_tool|tool_use)\s*>", body, re.IGNORECASE)
    if close_tag:
        body = body[: close_tag.start()]
    param_types = (tool_schemas or {}).get(tool_name)
    arguments = _xml_invoke_arguments(body, param_types)
    if arguments is None:
        # Legacy child-element form (<command>...</command>) and the plain-text
        # fallback live in a separate path so that parameter-style bodies with
        # zero complete <parameter/> tags are treated as truncated (None) rather
        # than swallowed as {"content": ...}.
        arguments = _xml_invoke_legacy_children(body, param_types)
    if arguments is None:
        # An invoke with an empty body is a valid zero-argument call only when the
        # tool declares no parameters (e.g. <invoke name="EnterPlanMode"></invoke>).
        # When the tool expects parameters, an empty body is more likely a dropped
        # or mangled call, so keep skipping it as before.
        if not body.strip():
            if not param_types:
                return ToolCall.create(tool_name, {}), block_end
            required = param_types.get("__required__")
            if isinstance(required, set) and not required:
                return ToolCall.create(tool_name, {}), block_end
            return ToolCall.create(tool_name, {}), block_end
        return None, block_end
    return ToolCall.create(tool_name, arguments), block_end


def _parse_xml_tool_calls(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall] | None, str]:
    calls: list[ToolCall] = []
    remainder = text
    consumed: list[tuple[int, int]] = []
    for opener in _INVOKE_OPENER_RE.finditer(text):
        call, block_end = _xml_invoke_call(opener.start(), text, tool_schemas)
        if call is None:
            continue
        calls.append(call)
        remainder = remainder.replace(text[opener.start() : block_end], " ")
        consumed.append((opener.start(), block_end))

    block_pattern = re.compile(r"<(?:tool_call|function_call)>(.*?)</(?:tool_call|function_call)>", re.DOTALL | re.IGNORECASE)
    for match in block_pattern.finditer(text):
        parsed = _extract_json_object(match.group(1))
        if parsed is None:
            continue
        obj, _, _ = parsed
        extracted = _extract_calls(obj)
        if extracted:
            calls.extend(extracted)
            remainder = remainder.replace(match.group(0), " ")
            consumed.append(match.span())
    for tool_name, param_types in (tool_schemas or {}).items():
        escaped = re.escape(tool_name)
        open_pattern = re.compile(rf"<{escaped}(?=[\s/>])([^>]*?)>(.*?)</{escaped}>", re.DOTALL | re.IGNORECASE)
        selfclose_pattern = re.compile(rf"<{escaped}(?=[\s/>])([^>]*?)/>", re.DOTALL | re.IGNORECASE)
        for match in open_pattern.finditer(text):
            start, end = match.span()
            if any(s <= start and end <= e for s, e in consumed):
                continue
            body = match.group(2)
            merged = _xml_tag_attrs(match.group(1), param_types)
            for extractor in (_xml_invoke_arguments, _xml_invoke_legacy_children):
                extracted_args = extractor(body, param_types)
                if extracted_args:
                    merged.update(extracted_args)
            if not merged:
                continue
            calls.append(ToolCall.create(tool_name, merged))
            consumed.append((start, end))
            remainder = remainder.replace(match.group(0), " ")
        for match in selfclose_pattern.finditer(text):
            start, end = match.span()
            if any(s <= start and end <= e for s, e in consumed):
                continue
            arguments = _xml_tag_attrs(match.group(1), param_types)
            if not arguments:
                continue
            calls.append(ToolCall.create(tool_name, arguments))
            consumed.append((start, end))
            remainder = remainder.replace(match.group(0), " ")
    if not calls:
        return None, ""
    remainder = re.sub(r"<(?:tool_calls|function_calls|tool_call|function_call)>", " ", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"</(?:tool_calls|function_calls|tool_call|function_call)>", " ", remainder, flags=re.IGNORECASE)
    wrapper = " ".join(remainder.split())
    return calls, wrapper


def _parse_bare_array_calls(text: str) -> list[ToolCall] | None:
    stripped = _strip_fences(text).strip()
    if not stripped.startswith("["):
        return None
    try:
        items = _loads_lenient(stripped)
    except ValueError:
        return None
    calls: list[ToolCall] = []
    for item in items:
        call = _extract_one_call(item)
        if call is not None:
            calls.append(call)
    return calls or None


def _iter_json_objects(text: str) -> Iterator[tuple[dict, int, int]]:
    i = 0
    length = len(text)
    while True:
        start = text.find("{", i)
        if start == -1:
            return
        depth = 0
        in_string = False
        escaped = False
        end = start
        while end < length:
            ch = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : end + 1]
                    try:
                        obj = _loads_lenient(candidate)
                        if isinstance(obj, dict):
                            yield obj, start, end
                    except ValueError:
                        pass
                    break
            end += 1
        i = end + 1


# --- DSML (DeepSeek Markup Language) tool-call parsing -------------------------
# DeepSeek sometimes emits tool calls in a proprietary tag format instead of JSON, e.g.:
#   <||DSML||tool_calls>
#     <||DSML||invoke name="Read">
#       <||DSML||parameter name="path" string="true">/a/b.md</||DSML||parameter>
#     </||DSML||invoke>
#   </||DSML||tool_calls>
# The tag separator is two pipe chars (U+FF5C or ASCII '|') around the word DSML.

_DSML_SEP = "(?:\\||\uff5c){2}"
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*invoke\s+name=(?P<q>['\"])(?P<name>[^'\">]+)(?P=q)"
    r"(?:\s+string=(?P<sq>['\"])(?P<sval>true|false)(?P=sq))?"
    r"\s*>",
    re.IGNORECASE,
)
_DSML_INVOKE_CLOSE_RE = re.compile(rf"</\s*{_DSML_SEP}DSML{_DSML_SEP}\s*invoke\s*>", re.IGNORECASE)

# Mixed-format variant: DeepSeek sometimes emits a bare <invoke> (no DSML prefix)
# while still using DSML-prefixed parameter tags inside it. We only accept bare
# invokes when at least one DSML-prefixed param is present, to avoid stealing
# pure-XML invoke blocks from the generic XML path.
_BARE_INVOKE_RE = re.compile(
    r"<\s*invoke\s+name=(?P<q>['\"])(?P<name>[^'\">]+)(?P=q)\s*>",
    re.IGNORECASE,
)
_BARE_INVOKE_CLOSE_RE = re.compile(r"</\s*invoke\s*>", re.IGNORECASE)
_DSML_PARAM_PRESENT_RE = re.compile(rf"<\s*{_DSML_SEP}DSML{_DSML_SEP}\s*parameter", re.IGNORECASE)

_DSML_PARAM_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*parameter\s+name=(?P<q>['\"])(?P<pname>[^'\">]+)(?P=q)"
    rf"(?:\s+string=(?P<sq>['\"])(?P<sval>true|false)(?P=sq))?"
    # Close tag: DeepSeek emits </||DSML||> (bare) or </||DSML|| parameter>; the
    # "parameter" word is optional. Without this, bare closes slip past the regex
    # and _dsml_param_value falls through to _coerce_dsml which maps "" → False.
    rf"\s*>\s*(?P<value>.*?)\s*</\s*(?:{_DSML_SEP}DSML{_DSML_SEP}\s*)?(?:parameter)?\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Malformed DSML variant: string="/path" instead of string="true">/path
# Handles both self-closing (string="/path"/>) and missing-quote closing (string="value"</...>)
_DSML_PARAM_STRING_VALUE_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*parameter\s+name=(?P<q>['\"])(?P<pname>[^'\">]+)(?P=q)"
    rf"\s+string=(?P<sq>['\"])(?P<sval>(?!true|false$)[^'\"]*?)?(?:(?=\")(?P=sq)|(?=<))\s*(?:/>)?"
    rf"(?:\s*</{_DSML_SEP}DSML{_DSML_SEP}\s*parameter\s*>)?",
    re.IGNORECASE,
)


# Malformed DSML separator (tooltrace b1c6a1cc): DeepSeek sometimes emits the DSML
# opening tags with a SINGLE pipe before DSML (`<|DSML||parameter ...>`) while the
# matching close tags keep the canonical two-pipe form. _DSML_SEP requires two pipes
# on each side, so these openers are invisible to every DSML branch. Rather than
# loosening the canonical separator, normalise only this malformed opener to the
# canonical form; the negative lookahead leaves canonical tags untouched.
_DSML_SINGLE_PIPE_OPENER_RE = re.compile(
    r"<(?P<slash>/?)\s*(?P<pipe>\||\uff5c)(?!\s*(?:\||\uff5c))\s*(?=DSML)",
    re.IGNORECASE,
)


def _repair_dsml_separator(text: str) -> str:
    """Expand a single-pipe DSML opener to the canonical two-pipe separator.

    Rewrites ``<|DSML`` to ``<||DSML`` (and the ``</`` close variant); canonical
    two-pipe tags are left unchanged because the regex rejects a second pipe. The
    ``DSML`` word itself is only asserted via lookahead, never consumed, so the tag
    body is left intact.
    """
    if not text or "DSML" not in text:
        return text
    return _DSML_SINGLE_PIPE_OPENER_RE.sub(
        lambda m: f"<{m.group('slash')}{m.group('pipe')}{m.group('pipe')}",
        text,
    )


def _dsml_param_value(sstring: str | None, raw: str, json_type: Any = None) -> Any:
    """Decode a DSML parameter value using its optional string="true|false" flag.

    ``string="true"`` means the value is a literal string; ``string="false"`` (or absent)
    means it should be type-coerced (bool/int/float/string).  When ``json_type`` is
    provided from the tool schema, numeric types are coerced even if DeepSeek marked
    the parameter as ``string="true"`` (a known DSML quirk where all params get that flag).
    """
    if sstring is not None and sstring.strip().lower() == "true":
        # Schema says this should be numeric — trust schema over DSML's blanket string="true".
        if json_type in ("integer", "number"):
            return _coerce_dsml(raw)
        return raw.strip()
    return _coerce_dsml(raw)


def _coerce_dsml(raw: str) -> Any:
    """Best-effort coercion of a DSML parameter value without an explicit string flag."""
    stripped = raw.strip()
    low = stripped.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    # JSON array/object (e.g. string="false" with a structured value like [{...}])
    if stripped[:1] in "[{":
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            pass
    return stripped


# Fused DSML+JSON form (tooltrace 3ab1fd2b): the invoke opener and the JSON
# arguments share one line — ``invoke name="Bash", "arguments": {...}}]}`` — with
# no <parameter> child tags. The normal opener regexes require a '>' right after
# the name, so this form slips past every DSML branch and the call is dropped.
_DSML_INVOKE_FUSED_RE = re.compile(
    rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*invoke\s+name=(?P<q>['\"])(?P<name>[^'\">]+)(?P=q)"
    rf"\s*,(?P<rest>.*?)(?=</\s*{_DSML_SEP}DSML{_DSML_SEP}\s*invoke\s*>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _parse_dsml_fused(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall] | None, str]:
    """Parse a DSML invoke whose body is inline JSON rather than <parameter> tags.

    Recovers the JSON object following ``name="..."`` and treats it as the
    argument set (unwrapping an ``arguments`` sub-object when present). Returns
    ``(calls, wrapper_text)``; ``calls`` is None when nothing was recovered.
    """
    calls: list[ToolCall] = []
    consumed: list[tuple[int, int]] = []
    for m in _DSML_INVOKE_FUSED_RE.finditer(text):
        name = m.group("name")
        rest = m.group("rest")
        args: dict[str, Any] = {}
        brace = rest.find("{")
        if brace != -1:
            bounds = _balanced_json(rest[brace:])
            if bounds is not None:
                start, end = bounds
                try:
                    loaded = _loads_lenient(rest[brace + start : brace + end + 1])
                except ValueError:
                    loaded = None
                if isinstance(loaded, dict):
                    inner = loaded.get("arguments")
                    if isinstance(inner, dict):
                        args = inner
                    else:
                        args = {k: v for k, v in loaded.items() if k not in ("name", "arguments", "id", "type")}
        calls.append(ToolCall.create(name, args))
        consumed.append((m.start(), m.end()))
    if not calls:
        return None, ""
    wrapper_chars = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(wrapper_chars))):
            if wrapper_chars[i] != "\n":
                wrapper_chars[i] = " "
    return calls, _strip_dsml("".join(wrapper_chars)).strip()


# Lenient DSML param opener: matches just the opening tag (name + optional string attr).
# The optional "/" after "<" handles the fused close-open layout where one param's
# closing tag doubles as the next param's opening tag.
_DSML_PARAM_OPENER_RE = re.compile(
    rf"</?{_DSML_SEP}DSML{_DSML_SEP}\s*parameter\s+name=(?P<q>['\"])(?P<pname>[^'\">]+)(?P=q)"
    rf"(?:\s+string=(?P<sq>['\"])(?P<sval>true|false)(?P=sq))?"
    r"\s*>",
    re.IGNORECASE,
)

# Malformed DSML param opener variant: the argument name sits directly in the
# ``parameter`` attribute (`parameter="command"`) with no ``name=`` word at all.
# _DSML_PARAM_OPENER_RE requires ``parameter name=``, so these tags are invisible to
# every DSML branch and the whole block is dropped (tooltrace 32629734; reported as
# `parameter="command">sleep 120"`). The value also carries a stray trailing quote
# because the opener reuses its attribute quote around the value.
_DSML_PARAM_EQ_OPENER_RE = re.compile(
    rf"</?{_DSML_SEP}DSML{_DSML_SEP}\s*parameter=(?P<q>['\"])(?P<pname>[^'\">]+)(?P=q)"
    rf"(?:\s+string=(?P<sq>['\"])(?P<sval>true|false)(?P=sq))?"
    r"\s*>",
    re.IGNORECASE,
)


# Bare k=v attributes fused into a DSML close tag (tooltrace 24b809c9): DeepSeek
# compresses trailing params into the close tag (`line_offset=170 n_lines=18` before
# the closing `>`) instead of emitting their own <parameter> tags.
_DSML_CLOSE_ATTR_RE = re.compile(r"(?P<key>[a-zA-Z_][\w.-]*)\s*=\s*(?P<val>\"[^\"]*\"|'[^']*'|[^\s>]+)")


def _dsml_close_tag_attrs(tag: str) -> dict[str, Any]:
    """Extract bare ``k=v`` attributes fused into a DSML close tag.

    The ``DSML||`` marker is skipped so it is never mistaken for a key; an ordinary
    close tag (no k=v tail) yields an empty dict.
    """
    marker = re.search(rf"DSML\s*{_DSML_SEP}", tag, re.IGNORECASE)
    body = tag[marker.end() :] if marker else tag
    attrs: dict[str, Any] = {}
    for m in _DSML_CLOSE_ATTR_RE.finditer(body):
        raw = m.group("val")
        if raw[:1] in ('"', "'"):
            raw = raw[1:-1]
        attrs[m.group("key")] = _coerce_dsml(raw)
    return attrs


def _parse_dsml_param_only(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> list[ToolCall] | None:
    """Extract tool calls from DSML parameter tags that have no enclosing <invoke>.

    DeepSeek sometimes emits bare ``<parameter>`` tags inside ``<calls>`` without any
    invoke wrapper (tooltrace dcc0519a, bea80392, 626e51d6). The tool name must be
    inferred from the parameter names via schema matching. Handles malformed close
    tags (missing the word "parameter" after the DSML separator) by scanning to the
    next opener or end of text instead of relying on a strict close regex.

    One block may encode several calls: a repeated parameter name starts a new call
    (tooltrace 24b809c9, f4275506), and trailing arguments fused into a close tag
    (the ``line_offset=170 n_lines=18`` tail of a close tag) are folded into the
    current call.

    Two opener spellings are accepted: the canonical ``parameter name="X"`` and the
    malformed ``parameter="X"`` where the name is the attribute itself, whose values
    carry a stray trailing quote (tooltrace 32629734).
    """
    # Pair each match with whether it came from the ``parameter="X"`` variant, whose
    # values carry a stray trailing quote (`sleep 120"`) because the opener reuses its
    # attribute quote around the value.
    openers: list[tuple[re.Match[str], bool]] = [(m, False) for m in _DSML_PARAM_OPENER_RE.finditer(text)]
    openers += [(m, True) for m in _DSML_PARAM_EQ_OPENER_RE.finditer(text)]
    openers.sort(key=lambda item: item[0].start())
    if not openers:
        return None

    # A single bare-parameter block can encode SEVERAL calls: DeepSeek repeats the
    # same parameter name once per call (tooltrace 24b809c9: four <parameter
    # name="path"> = four Read calls; f4275506: two <parameter name="command"> =
    # two Bash calls). It also fuses trailing params into the close tag
    # (`line_offset=170 n_lines=18` before the closing `>`). Splitting the run on a
    # repeated name is what turns the old single (last-wins) call into the full set.
    _dsml_tag_any_re = re.compile(
        rf"(?:</?)?\s*{_DSML_SEP}DSML{_DSML_SEP}\s*(?:\w+\s*)?[^>]*>",
        re.IGNORECASE,
    )
    # Each param: (pname, raw_value, string_flag, fused_close_attrs).
    groups: list[list[tuple[str, str, Any, dict[str, Any]]]] = []
    current: list[tuple[str, str, Any, dict[str, Any]]] = []
    seen: set[str] = set()
    for idx, (m, is_eq) in enumerate(openers):
        pname = m.group("pname")
        value_start = m.end()
        value_end = openers[idx + 1][0].start() if idx + 1 < len(openers) else len(text)
        region = text[value_start:value_end]
        # The value ends at the first DSML tag (close or open) in the region.
        # Any further tag in the same region is a call separator (see below).
        region_tags = list(_dsml_tag_any_re.finditer(region))
        tag_match = region_tags[0] if region_tags else None
        raw_value = region[: tag_match.start()] if tag_match else region
        raw_value = raw_value.strip()
        # The ``parameter="X"`` variant reuses the attribute quote around the value,
        # leaving a stray trailing quote (tooltrace 32629734: `sleep 120"`).
        if is_eq and raw_value.endswith(('"', "'")):
            raw_value = raw_value[:-1].rstrip()
        attrs = _dsml_close_tag_attrs(tag_match.group(0)) if tag_match else {}
        # A repeated parameter name starts the next call. Tool calls are sets of
        # uniquely-named args, so a repeat — whether of an argument (two "command")
        # or of "name" (two tool names in one block) — always marks a new call.
        if pname in seen:
            groups.append(current)
            current = []
            seen = set()
        current.append((pname, raw_value, m.group("sval"), attrs))
        seen.add(pname)
        # A second DSML tag after this parameter's own close tag is a bare call
        # separator: DeepSeek splits sibling calls in a param-only block with a
        # standalone `` `` / `` `` line that carries no
        # parameter word. The repeated-name split above cannot see it, because
        # sibling calls may have disjoint argument names (a Grep then a Read, or
        # two Greps sharing only some keys). tooltrace 0885f2d2, 8b1c632c, 0241e65f.
        if len(region_tags) > 1:
            groups.append(current)
            current = []
            seen = set()
    if current:
        groups.append(current)

    def _group_args(group: list[tuple[str, str, Any, dict[str, Any]]]) -> tuple[str | None, dict[str, Any]]:
        name_value: str | None = None
        args: dict[str, Any] = {}
        for pname, raw_val, sval, attrs in group:
            if pname == "name" and name_value is None:
                name_value = _dsml_param_value(sval, raw_val)
                continue
            args[pname] = _dsml_param_value(sval, raw_val)
            for key, value in attrs.items():
                args.setdefault(key, value)
        # A sole "arguments" param carrying JSON is unwrapped (tooltrace 626e51d6).
        if list(args.keys()) == ["arguments"]:
            inner = args["arguments"]
            if isinstance(inner, str):
                try:
                    inner = _loads_lenient(inner)
                except (TypeError, ValueError):
                    pass
            if isinstance(inner, dict):
                args = inner
        return name_value, args

    built = [_group_args(group) for group in groups]

    # Resolve the tool name per call. A bare-parameter block is normally one tool
    # invoked repeatedly, so an unresolved call (e.g. a lone {path}, ambiguous
    # between Read/Edit/Write) borrows the block's single resolved name when that
    # tool's schema accepts the call's keys.
    resolved: list[str | None] = []
    for name_value, args in built:
        if isinstance(name_value, str) and name_value:
            resolved.append(name_value)
        else:
            resolved.append(_infer_tool_from_params(args, tool_schemas))
    unique_names = {name for name in resolved if name}
    fallback = next(iter(unique_names)) if len(unique_names) == 1 else None

    calls: list[ToolCall] = []
    for (_name_value, args), resolved_name in zip(built, resolved, strict=True):
        name = resolved_name
        if name is None and fallback is not None:
            schema = (tool_schemas or {}).get(fallback, {})
            if isinstance(schema, dict) and all(key in schema for key in args):
                name = fallback
        if name is None:
            continue
        calls.append(ToolCall.create(name, args))
    return calls or None


def _parse_dsml(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall] | None, str]:
    """Parse DeepSeek DSML-tagged tool calls into ToolCall objects.

    Returns (calls, wrapper_text). ``calls`` is None when no DSML invoke block was
    found. The wrapper text is the surrounding prose with all DSML blocks blanked out.
    """
    if _DSML_INVOKE_RE.search(text):
        invoke_re = _DSML_INVOKE_RE
        close_re = _DSML_INVOKE_CLOSE_RE
    elif _BARE_INVOKE_RE.search(text) and _DSML_PARAM_PRESENT_RE.search(text):
        # Mixed format: bare <invoke> coexisting with DSML-prefixed params.
        invoke_re = _BARE_INVOKE_RE
        close_re = _BARE_INVOKE_CLOSE_RE
    elif _DSML_PARAM_PRESENT_RE.search(text):
        # No <invoke> opener at all — DeepSeek sometimes emits bare DSML parameter
        # tags inside <calls> without wrapping them in an invoke (tooltrace
        # dcc0519a, bea80392, 626e51d6). Extract params and infer the tool name.
        calls = _parse_dsml_param_only(text, tool_schemas)
        if not calls:
            return None, ""
        wrapper_chars = list(text)
        # Blank out all DSML-tagged regions (params + wrappers).
        for pm in re.finditer(rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*parameter[^>]*>", text):
            start = pm.start()
            end = len(text)
            # Find the next param opener or DSML close tag after this one.
            nxt = re.search(rf"<{_DSML_SEP}DSML{_DSML_SEP}\s*(?:parameter|calls|invoke)", text[pm.end() :])
            if nxt:
                end = pm.end() + max(0, nxt.start() - 3)
            for i in range(start, min(end, len(wrapper_chars))):
                if wrapper_chars[i] != "\n":
                    wrapper_chars[i] = " "
        return calls, _strip_dsml("".join(wrapper_chars)).strip()
    else:
        return None, ""
    calls: list[ToolCall] = []
    consumed: list[tuple[int, int]] = []
    for m in invoke_re.finditer(text):
        name = m.group("name")
        close = close_re.search(text, m.end())
        body_end = close.start() if close else len(text)
        block_end = close.end() if close else len(text)
        param_types = (tool_schemas or {}).get(name)
        args: dict[str, Any] = {}
        matched_params: set[str] = set()
        # DeepSeek emits two parameter layouts inside an invoke body:
        #  1. Well-formed: each <parameter> has its own </parameter> close tag.
        #     _DSML_PARAM_RE handles this correctly (lazy value stops at the close).
        #  2. Shared-close: multiple params share a single closing tag at the end.
        #     The lazy value group then swallows subsequent param openers into the
        #     first param's value.  Detect this by checking whether the captured
        #     value contains another parameter opener; if so, re-extract using
        #     position-bounded scanning (same strategy as _parse_dsml_param_only).
        shared_close = False
        for pm in _DSML_PARAM_RE.finditer(text, m.end(), body_end):
            pname = pm.group("pname")
            value = pm.group("value")
            if "<parameter" not in value.lower() and "\uff5c\uff5cDSML" not in value:
                # Clean match — no embedded param opener in the value.
                if pname not in matched_params:
                    ptype = (param_types or {}).get(pname)
                    args[pname] = _dsml_param_value(pm.group("sval"), value, ptype)
                    matched_params.add(pname)
            else:
                shared_close = True

        if shared_close:
            # Re-extract all params in this invoke body using bounded scanning:
            # each param's value ends at the next param opener or a close tag.
            _dsml_tag_any_re = re.compile(
                rf"(?:</?)?\s*{_DSML_SEP}DSML{_DSML_SEP}\s*(?:\w+\s*)?[^>]*>",
                re.IGNORECASE,
            )
            openers: list[re.Match[str]] = []
            for pm in _DSML_PARAM_OPENER_RE.finditer(text, m.end(), body_end):
                openers.append(pm)
            # Reset args from the first pass — clean params were already added above,
            # but we need to re-extract ALL params via bounded scanning to get correct
            # values for the shared-close group.  Keep only cleanly-matched params.
            clean_args = {k: v for k, v in args.items() if k in matched_params}
            args.clear()
            for idx, om in enumerate(openers):
                pname = om.group("pname")
                if pname in clean_args:
                    continue  # already correctly extracted via _DSML_PARAM_RE
                value_start = om.end()
                if idx + 1 < len(openers):
                    value_end = openers[idx + 1].start()
                else:
                    value_end = body_end
                raw_value = text[value_start:value_end]
                tag_match = _dsml_tag_any_re.search(raw_value)
                if tag_match:
                    raw_value = raw_value[: tag_match.start()]
                ptype = (param_types or {}).get(pname)
                args[pname] = _dsml_param_value(om.group("sval"), raw_value.strip(), ptype)
                matched_params.add(pname)  # prevent _DSML_PARAM_STRING_VALUE_RE from overwriting
            for k, v in clean_args.items():
                args.setdefault(k, v)

        for pm in _DSML_PARAM_STRING_VALUE_RE.finditer(text, m.end(), body_end):
            pname = pm.group("pname")
            if pname not in matched_params:
                ptype = (param_types or {}).get(pname)
                args[pname] = _dsml_param_value(pm.group("sval"), pm.group("sval"), ptype)
                matched_params.add(pname)
        if not args:
            # No <parameter> children found — the invoke body may carry inline JSON
            # directly (tooltrace a25a0b91 emits an extra string="false" attribute on
            # the invoke tag followed by raw JSON arguments instead of parameter tags).
            body = text[m.end() : body_end]
            brace = body.find("{")
            if brace != -1:
                bounds = _balanced_json(body[brace:])
                if bounds is not None:
                    s, e = bounds
                    try:
                        loaded = _loads_lenient(body[brace + s : brace + e + 1])
                    except ValueError:
                        loaded = None
                    if isinstance(loaded, dict):
                        inner = loaded.get("arguments")
                        if isinstance(inner, dict):
                            args = inner
                        else:
                            args = {k: v for k, v in loaded.items() if k not in ("name", "arguments", "id", "type")}
        calls.append(ToolCall.create(name, args))
        consumed.append((m.start(), block_end))
    wrapper_chars = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(wrapper_chars))):
            if wrapper_chars[i] != "\n":
                wrapper_chars[i] = " "
    return calls, "".join(wrapper_chars).strip()


# DeepSeek v4-pro occasionally emits a *parameter-style* tool call: a <tool_calls>
# block whose body is flat <parameter name="...">...</parameter> tags with NO inner
# <invoke>. The tool name arrives as its own parameter (name="Read") and the real
# arguments follow as sibling parameters. This is not JSON, XML <invoke>, or DSML,
# so every generic path misses it and the call lands as unparsed:json_malformed.
_PARAMETER_TAG_RE = re.compile(
    r"<\s*parameter\s+name=(?P<q>[\"'])(?P<pname>[^\"']+)(?P=q)"
    r"(?:\s+string=(?P<sq>['\"])(?P<sval>true|false)(?P=sq))?"
    r"\s*>\s*(?P<value>.*?)\s*</\s*parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _infer_tool_from_params(args: dict[str, Any], tool_schemas: dict[str, dict[str, Any]] | None) -> str | None:
    """Infer the tool name from parameter names when DeepSeek omits it.

    Returns the tool name if exactly one schema has all provided param names as
    valid fields; otherwise returns None (ambiguous or no match).
    """
    if not tool_schemas or not args:
        return None
    candidates = [name for name, params in tool_schemas.items() if isinstance(params, dict) and all(k in params for k in args)]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _parse_parameter_tool_calls(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall] | None, str]:
    """Parse DeepSeek's ``<tool_calls><parameter name="name">...`` format.

    Each maximal run of consecutive ``<parameter>`` tags (with no other tag between
    them) is one tool call: the parameter literally named ``"name"`` carries the tool
    name and every remaining parameter in that run becomes an argument. Handles both
    the plain form and DeepSeek's doubled ``<tool_calls><tool_calls>`` wrapper, because
    only ``<parameter>`` tags are consumed here.

    Returns ``(calls, remainder)`` where ``remainder`` is the input with the consumed
    parameter runs blanked (so the caller can recover surrounding prose as the text
    payload).
    """
    param_spans = [m.span() for m in _PARAMETER_TAG_RE.finditer(text)]
    if not param_spans:
        return None, ""

    # Group consecutive parameters into calls. A gap that contains any '<' is a real
    # tag (e.g. <invoke> or </tool_calls>) and therefore separates two distinct calls;
    # whitespace/newlines between params keep them in the same call.
    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    for span in param_spans:
        if not current:
            current.append(span)
            continue
        prev_end = current[-1][1]
        gap_has_tag = "<" in text[prev_end : span[0]]
        if gap_has_tag:
            groups.append(current)
            current = [span]
        else:
            current.append(span)
    if current:
        groups.append(current)

    calls: list[ToolCall] = []
    consumed: list[tuple[int, int]] = []
    for group in groups:
        name_value: Any = None
        args: dict[str, Any] = {}
        for start, _end in group:
            m = _PARAMETER_TAG_RE.match(text, start)
            pname = m.group("pname")
            value = _dsml_param_value(m.group("sval"), m.group("value"), None)
            if pname == "name" and name_value is None:
                name_value = value
            else:
                args[pname] = value
        # A call needs a resolvable tool name; without it the parameters are just prose.
        if not isinstance(name_value, str) or not name_value:
            # DeepSeek sometimes omits <parameter name="name"> entirely — infer
            # the tool from available schemas by matching parameter names.
            # Skip inference when an <invoke> wrapper is present (the XML parser
            # handles truncated parameters better).
            if "<invoke" in text.lower():
                continue
            inferred = _infer_tool_from_params(args, tool_schemas)
            if inferred is None:
                continue
            name_value = inferred
        calls.append(ToolCall.create(name_value, args))
        consumed.extend(group)

    if not calls:
        return None, ""
    remainder_chars = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(remainder_chars))):
            if remainder_chars[i] != "\n":
                remainder_chars[i] = " "
    # Blank the (possibly doubled) <tool_calls> wrapper tags too.
    remainder = "".join(remainder_chars)
    remainder = re.sub(r"</?\s*tool_calls\s*>", " ", remainder, flags=re.IGNORECASE)
    return calls, " ".join(remainder.split())


# DeepSeek v4-pro also emits a *tag-name* XML tool call: the tool name is a bare
# element tag inside <tool_calls> (e.g. <Bash><command>…</command></Bash>) rather than
# an <invoke name="Bash"> opener with child parameters. _parse_xml_tool_calls only
# matches <invoke>/declared-name tags, so this form fell through to
# unparsed:json_malformed (request_id e687cea733f541aab53881d94d6d7382). Each
# "<ToolName>…</ToolName>" block is one call; its child elements become the arguments.
# A truncated stream (e.g. the provider's token limit, or the 1000-char upstream log
# cap) can leave the final </tool_calls> unwritten; match a balanced close when present
# but let an unterminated last block run to end-of-text so its calls still recover.
_TOOLCALLS_BLOCK_RE = re.compile(r"<\s*tool_calls\s*>(.*?)(?:</\s*tool_calls\s*>|$)", re.DOTALL | re.IGNORECASE)


def _parse_tagname_tool_calls(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall] | None, str]:
    """Parse DeepSeek's ``<tool_calls><Bash>…</Bash></tool_calls>`` tag-name form.

    Only invoked when the text actually contains a <tool_calls> wrapper and no
    DSML/parameter-style markers (those are handled by their own branches first).
    Within each tool_calls block, every ``<Name>…</Name>`` element whose Name is not a
    known sub-parameter tag becomes one ToolCall; its inner child elements are parsed
    as arguments via the shared legacy-children extractor. Returns ``(calls, remainder)``
    with consumed blocks blanked so surrounding prose survives as the text payload.
    """
    if "<tool_calls" not in text.lower():
        return None, ""

    # If the wrapper contains an actual <invoke>/<use_tool>/<tool_use name=...> opener,
    # this is the classic XML form — let _parse_xml_tool_calls handle it. We only claim
    # the tag-name form when no such named invoke exists (otherwise its <command>/… child
    # tags would be mistaken for top-level tools).
    if re.search(r"<(?:invoke|use_tool|tool_use)\s+name=", text, re.IGNORECASE):
        return None, ""

    # Sub-parameter tag names that must NOT be mistaken for a tool name: these are
    # argument carriers (child elements) inside a <ToolName> block, never tools.
    reserved = {"parameter", "invoke", "use_tool", "tool_use"}
    schemas = tool_schemas or {}

    calls: list[ToolCall] = []
    consumed: list[tuple[int, int]] = []  # absolute spans in `text` of consumed tool elements
    for block in _TOOLCALLS_BLOCK_RE.finditer(text):
        body = block.group(1)
        base = block.start(1)
        inner_spans: list[tuple[int, int]] = []  # element spans within the block body
        for m in re.finditer(r"<([a-zA-Z_][a-zA-Z0-9_-]*)\s*>", body, re.IGNORECASE):
            name = m.group(1).strip()
            if name.lower() in reserved:
                continue
            # Skip openers nested inside an already-handled tool element (e.g. the
            # <command>/<description> children of a <Bash> block) — only top-level
            # elements within the block are candidate tools.
            if any(s <= m.start() and m.end() <= e for s, e in inner_spans):
                continue
            close = re.search(rf"</{re.escape(name)}\s*>", body[m.end() :], re.IGNORECASE)
            param_types = schemas.get(name)
            if close:
                element_end = m.end() + close.end()
                inner_body = body[m.end() : m.end() + close.start()]
            else:
                # Truncated stream (provider token limit / log cap): the tool element
                # has no </Name>. Recover its arguments from whatever child text was
                # streamed before the cut, mirroring _xml_invoke_arguments' tail logic.
                element_end = len(body)
                inner_body = body[m.end() :]
            args = _xml_invoke_arguments(inner_body, param_types)
            if not args:
                args = _xml_invoke_legacy_children(inner_body, param_types)
            if not close and not args:
                # Truncated with no recoverable child text — a bare "<Name>" opener at
                # the cut point is more likely prose than a real call; skip it.
                continue
            inner_spans.append((m.start(), element_end))
            calls.append(ToolCall.create(name, args or {}))
            consumed.append((base + m.start(), base + element_end))

    if not calls:
        return None, ""

    remainder_chars = list(text)
    for start, end in consumed:
        for i in range(start, min(end, len(remainder_chars))):
            if remainder_chars[i] != "\n":
                remainder_chars[i] = " "
    remainder = "".join(remainder_chars)
    remainder = re.sub(r"</?\s*tool_calls\s*>", " ", remainder, flags=re.IGNORECASE)
    return calls, " ".join(remainder.split())


# DeepSeek routinely emits tool parameters under names that differ from the
# client's declared schema. These aliases map those provider-side names onto the
# canonical parameter name, so the call survives the client's strict argument
# validation instead of being rejected as an extra/missing property.
_ARG_ALIASES: dict[str, dict[str, str]] = {
    "Read": {"file_path": "path", "offset": "line_offset", "limit": "n_lines"},
    # Bash aliases observed in tooltrace.ndjson (request_id dc4ee19c): DeepSeek
    # emits ``cmd``/``desc`` instead of the schema's ``command``/``description``.
    "Bash": {"cmd": "command", "desc": "description"},
    "Edit": {"file_path": "path"},
    "Write": {"file_path": "path"},
    # TodoList alias observed in tooltrace.ndjson (request_id 86a22871): DeepSeek
    # names the todo array ``items`` instead of the schema's canonical ``todos``,
    # which the client rejects as an extra property.
    "TodoList": {"items": "todos"},
}


def _unwrap_nested_arguments(tool_name: str, args: dict) -> dict:
    """Flatten a call whose real parameters were nested under an "arguments" key.

    DeepSeek sometimes emits a single DSML parameter literally named "arguments"
    whose value is the JSON object of the real parameters, instead of emitting one
    parameter per argument (e.g. ``arguments`` carrying ``{"command": "ls"}`` for a
    Bash call). The DSML parser stores it verbatim, so the client receives
    ``{"arguments": {"command": "ls"}}`` -- a Bash call with no ``command`` -- and
    rejects it with "must have required property 'command'".

    This unwraps the nested object into the top level, keeping any genuine sibling
    keys. A redundant ``name`` sibling that merely repeats the tool's own name
    (DeepSeek re-emits it as a parameter) is dropped when it matches ``tool_name``.
    """
    inner = args.get("arguments")
    if inner is None:
        return args
    if isinstance(inner, str):
        # The nested value is itself frequently a *mangled* JSON object: DeepSeek
        # emits shell commands whose inner quotes were not escaped, so strict
        # json.loads fails with "Extra data". Use the parser's lenient loader, which
        # repairs exactly this class of damage.
        try:
            inner = _loads_lenient(inner)
        except (TypeError, ValueError):
            return args
    if not isinstance(inner, dict):
        return args
    merged = dict(inner)
    for key, value in args.items():
        if key == "arguments":
            continue
        if key == "name" and value == tool_name:
            # DeepSeek sometimes repeats the tool name as a parameter; the invoke
            # tag already carries it, so this is not a real argument.
            continue
        merged.setdefault(key, value)
    return merged


def _normalize_tool_args(tool_name: str, args: dict) -> dict:
    """Rename provider-side parameter aliases to the client's canonical names.

    An alias is only applied when the canonical name is not already present, so a
    well-formed call (e.g. one that legitimately carries ``cwd`` for Bash) keeps
    its real arguments untouched and no value is clobbered.
    """
    aliases = _ARG_ALIASES.get(tool_name)
    if not aliases:
        return args
    normalized: dict[str, Any] = {}
    existing = set(args.keys())
    for key, value in args.items():
        canonical = aliases.get(key)
        if canonical is not None and canonical not in existing and canonical != key:
            # Skip when the call already carries the real name (avoid clobbering).
            normalized[canonical] = value
        else:
            normalized[key] = value
    return normalized


# DeepSeek emits TodoList todo ``status`` values outside the client's enum. The
# client accepts only done / pending / in_progress; map observed synonyms to the
# closest canonical state so validation stops rejecting "must be equal to one of
# the allowed values". Keys are lowercase for case-insensitive lookup.
_TODO_STATUS_MAP = {
    # → done
    "completed": "done",
    "complete": "done",
    "finished": "done",
    "succeeded": "done",
    "confirmed": "done",
    "ok": "done",
    "passing": "done",
    # → in_progress
    "active": "in_progress",
    "doing": "in_progress",
    "working": "in_progress",
    "started": "in_progress",
    "probing": "in_progress",
    # → pending
    "not_started": "pending",
    "todo": "pending",
    "queued": "pending",
    "waiting": "pending",
}


# DeepSeek emits TodoList items with a ``content`` field where the client's schema
# expects ``title`` (see tooltrace.ndjson request_id 86a22871); map it so validation
# stops rejecting the missing property.
_TODO_ITEM_FIELD_ALIASES = {"content": "title"}

# Parameters that are always numeric regardless of how the tool schema declares them.
# The XML invoke / DSML paths decode parameter values as strings before alias renaming,
# and if the client's tool schema doesn't carry an extractable type (e.g. anyOf with
# nested constraints), _normalize_calls has no way to know they should be integers.
# This map provides a hard fallback so the client stops rejecting "must be integer".
_KNOWN_NUMERIC_PARAMS: dict[str, set[str]] = {
    "Read": {"line_offset", "n_lines"},
}


def _normalize_todo_items(args: dict) -> dict:
    """Normalize TodoList todo entries to the client's expected shape.

    For each item in ``args["todos"]``: map non-canonical ``status`` values through
    ``_TODO_STATUS_MAP``, and rename provider-side field aliases (e.g. ``content`` →
    ``title``). Already-valid fields are left untouched. Runs after the top-level
    parameter aliasing, so it reads the canonical ``todos`` key.
    """
    todos = args.get("todos")
    if not isinstance(todos, list):
        return args
    for item in todos:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str) and status.lower() in _TODO_STATUS_MAP:
            item["status"] = _TODO_STATUS_MAP[status.lower()]
        # Field alias: rename only when the canonical field is absent (no clobber).
        for alias, canonical in _TODO_ITEM_FIELD_ALIASES.items():
            if alias in item and canonical not in item:
                item[canonical] = item.pop(alias)
    return args


def _normalize_calls(
    calls: list[ToolCall],
    tool_schemas: dict[str, dict[str, Any]] | None = None,
) -> list[ToolCall]:
    """Normalize tool-call arguments after parsing.

    - Tool name case normalization: DeepSeek sometimes emits a tool name with
      different casing than the client declared (e.g. ``bash`` instead of
      ``Bash``). When the generated name doesn't match any schema key exactly
      but matches one case-insensitively, rewrite it to the declared form.
    - Provider-side aliases: Read file_path→path / offset→line_offset /
      limit→n_lines; Bash cmd→command / desc→description; Edit & Write
      file_path→path (see ``_ARG_ALIASES``). DeepSeek emits these alternate names
      and the client rejects them as extra/missing properties.
    - Schema-typed boolean coercion: a string ``"true"``/``"false"`` becomes a
      real bool when the tool schema marks the parameter as boolean (e.g. Grep
      ``-n``). DeepSeek sometimes emits booleans as strings and the client
      rejects the wrong type.
    """
    # Build a lowercase→declared-name lookup for case normalization.
    lower_to_name: dict[str, str] = {}
    if tool_schemas:
        for name in tool_schemas:
            lower_to_name[name.lower()] = name

    for call in calls:
        # Case-normalize the tool name before looking up its schema/aliases.
        if tool_schemas and call.name not in tool_schemas:
            canonical = lower_to_name.get(call.name.lower())
            if canonical is not None:
                call.name = canonical
        try:
            args = json.loads(call.arguments)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(args, dict):
            continue
        args = _unwrap_nested_arguments(call.name, args)
        args = _normalize_tool_args(call.name, args)
        if call.name == "TodoList":
            args = _normalize_todo_items(args)
        param_types = (tool_schemas or {}).get(call.name, {})
        numeric_fallback = _KNOWN_NUMERIC_PARAMS.get(call.name)
        for key, value in list(args.items()):
            expected = param_types.get(key)
            if not isinstance(value, str):
                continue
            if expected == "boolean":
                low = value.strip().lower()
                if low in ("true", "false"):
                    args[key] = low == "true"
            elif expected in ("integer", "number"):
                # Provider aliases (e.g. Read offset→line_offset) are renamed above,
                # but the raw value was decoded before the schema type was known, so a
                # numeric parameter can still be a string here. Coerce it so the client
                # stops rejecting "line_offset must be integer".
                args[key] = _coerce_scalar(value, expected)
            elif expected is None and key in (numeric_fallback or ()):
                # Schema didn't declare this param's type; use known-numeric fallback.
                stripped = value.strip()
                if re.fullmatch(r"-?\d+", stripped):
                    args[key] = int(stripped)
        call.arguments = json.dumps(args, ensure_ascii=False)
    return calls


# Strip trailing characters that are clearly not part of a JSON object: code-fence
# markers (```) and XML closing tags (</invoke>, </tool_calls>). These appear when
# DeepSeek wraps or truncates its tool-call payload inside a markdown fence or mixes
# formats, leaving non-JSON debris after the last valid token.
_TRAILING_FENCE_RE = re.compile(r"\n?```[a-zA-Z]*\s*$")
_TRAILING_XML_TAG_RE = re.compile(r"</[a-zA-Z_][\w.-]*>\s*$")


def _strip_trailing_non_json(text: str) -> str:
    while True:
        m = _TRAILING_FENCE_RE.search(text)
        if m:
            text = text[: m.start()].rstrip()
            continue
        m = _TRAILING_XML_TAG_RE.search(text)
        if m:
            text = text[: m.start()].rstrip()
            continue
        break
    return text


# DeepSeek v4-pro sometimes emits a corrupted JSON separator after the "tool_calls"
# key: `"tool_calls">` instead of `"tool_calls": [`, with `{` as the array opener.
# The result is invalid JSON that no downstream path can parse (stray char defeats
# _loads_lenient, mismatched brackets defeat _fix_unbalanced_json). Repair it to
# valid JSON so the standard wrapped-calls extraction handles it normally.
_TOOL_CALLS_SEP_RE = re.compile(r'("tool_calls")\s*([^\[{:\s])')


def _repair_tool_calls_separator(text: str) -> str:
    m = _TOOL_CALLS_SEP_RE.search(text)
    if m:
        return text[: m.start()] + m.group(1) + ": [" + text[m.end() :]
    return text


def parse_tool_calls(text: str, tool_schemas: dict[str, dict[str, Any]] | None = None) -> tuple[list[ToolCall], str] | None:
    if not text or not text.strip():
        return None
    # Normalise the malformed single-pipe DSML opener before any branch inspects
    # the text, so the canonical-format DSML parsers can handle it unchanged.
    text = _repair_dsml_separator(text)
    # DSML is a distinct tag format (not JSON), so parse it before the generic
    # strip/JSON path — _strip_dsml would otherwise discard the tags and their attrs.
    dsml_calls, dsml_wrapper = _parse_dsml(text, tool_schemas)
    if dsml_calls:
        return _normalize_calls(dsml_calls, tool_schemas), " ".join(_strip_dsml(dsml_wrapper).split())
    # Fused DSML+JSON invokes must be handled before _strip_dsml erases the tags
    # (the tag text carries the tool name for this form).
    fused_calls, fused_wrapper = _parse_dsml_fused(text, tool_schemas)
    if fused_calls:
        return _normalize_calls(fused_calls, tool_schemas), " ".join(_strip_dsml(fused_wrapper).split())
    stripped = _repair_tool_calls_separator(_strip_fences(_strip_dsml(text)))
    extracted = _extract_json_object(stripped)
    if extracted is not None:
        obj, start, end = extracted
        wrapped_calls = _extract_wrapped_calls(obj)
        if wrapped_calls is None and "tool" in obj:
            # DeepSeek sometimes emits a flat single-object format:
            # {"tool": "Grep", "arguments": {...}} instead of the expected
            # {"tool_calls": [{"name": ..., "arguments": ...}]}.
            # Only take this path when there's no other JSON object in the text;
            # multiple objects should go through _iter_json_objects.
            surrounding = stripped[:start] + stripped[end + 1 :]
            if "{" not in surrounding:
                wrapped_calls = _extract_calls(obj)
        if wrapped_calls is not None:
            wrapper_parts = []
            surrounding = (stripped[:start].strip() + " " + stripped[end + 1 :].strip()).strip()
            if surrounding:
                wrapper_parts.append(surrounding)
            inner = obj.get("content")
            if isinstance(inner, str) and inner.strip():
                wrapper_parts.append(inner.strip())
            return _normalize_calls(wrapped_calls, tool_schemas), " ".join(wrapper_parts).strip()
    array_calls = _parse_bare_array_calls(stripped)
    if array_calls:
        return _normalize_calls(array_calls, tool_schemas), ""
    param_calls, param_wrapper = _parse_parameter_tool_calls(stripped, tool_schemas)
    if param_calls:
        return _normalize_calls(param_calls, tool_schemas), param_wrapper
    tagname_calls, tagname_wrapper = _parse_tagname_tool_calls(stripped, tool_schemas)
    if tagname_calls:
        return _normalize_calls(tagname_calls, tool_schemas), tagname_wrapper
    xml_calls, wrapper = _parse_xml_tool_calls(stripped, tool_schemas)
    if xml_calls:
        return _normalize_calls(xml_calls, tool_schemas), wrapper
    calls: list[ToolCall] = []
    removed = bytearray(len(stripped))
    for obj, start, end in _iter_json_objects(stripped):
        found = _extract_calls(obj)
        if found:
            calls.extend(found)
            removed[start : end + 1] = b" " * (end - start + 1)
    if calls:
        wrapper = "".join((stripped[i] if removed[i] == 0 else " ") for i in range(len(stripped)))
        return _normalize_calls(calls, tool_schemas), " ".join(wrapper.split())
    # Last resort: the standard paths only accept *balanced* JSON objects. DeepSeek
    # sometimes truncates a tool_calls payload mid-stream (hitting its token limit),
    # leaving an unbalanced object that _loads_lenient can still repair via
    # _fix_unbalanced_json. Try loading everything from the first '{' to the end.
    brace = stripped.find("{")
    if brace != -1:
        candidate = _strip_trailing_non_json(stripped[brace:])
        obj: Any = None
        try:
            obj = _loads_lenient(candidate)
        except ValueError:
            pass
        # Repair missing intermediate braces in tool_calls array elements.
        if not isinstance(obj, dict):
            repaired = _fix_missing_call_braces(candidate)
            if repaired is not None:
                try:
                    obj = _loads_lenient(repaired)
                except ValueError:
                    pass
        # Try progressively shorter prefixes ending at each '}' position.
        # This handles trailing garbage with phantom quotes that confuse
        # string tracking (e.g. `</result>"` after valid JSON).
        if not isinstance(obj, dict):
            for pos in range(len(candidate) - 1, brace, -1):
                if candidate[pos] != "}":
                    continue
                prefix = candidate[: pos + 1]
                try:
                    obj = _loads_lenient(prefix)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    break
                obj = None
        if isinstance(obj, dict):
            found = _extract_calls(obj) or _extract_wrapped_calls(obj)
            if found:
                wrapper = stripped[:brace].strip()
                return _normalize_calls(found, tool_schemas), wrapper
    # Last resort before giving up: DeepSeek sometimes emits prose + fenced JSON block.
    # The standard path above strips only full-wrap fences; this catches embedded ones.
    embedded = _extract_embedded_fence(text)
    if embedded is not None:
        inner, prose = embedded
        obj = None
        # _loads_lenient raises ValueError when the inner block cannot be repaired;
        # parse_tool_calls' contract is to return None on failure, not to propagate.
        # Letting it escape crashed the streaming response (ASGI 500) instead of
        # falling back to plain content.
        try:
            obj = _loads_lenient(inner)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            found = _extract_calls(obj) or _extract_wrapped_calls(obj)
            if found:
                return _normalize_calls(found, tool_schemas), prose
    return None
