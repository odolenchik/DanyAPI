from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

# DeepSeek per-request input limit: ~57000 chars confirmed working. Use 52000 for headroom.
MAX_APPEND_PROMPT_CHARS = 52000

# How many leading system/user messages form the stable session anchor.
# Keeping this bounded means later compaction/history edits by the client do not
# change the fingerprint that maps a conversation back to its upstream session.
CONTEXT_ANCHOR_MAX = 8


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    @classmethod
    def create(cls, name: str, arguments: Any) -> ToolCall:
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        if isinstance(arguments, dict):
            args_text = json.dumps(arguments, ensure_ascii=False)
        elif isinstance(arguments, str):
            args_text = arguments
        elif arguments is None:
            args_text = "{}"
        else:
            args_text = json.dumps(arguments, ensure_ascii=False)
        return cls(call_id, name, args_text)


TOOL_CALL_INSTRUCTION = (
    "You have access to the following functions. Call them when the user's request requires it.\n\n"
    "{functions}\n\n"
    "When you need to call one or more functions, your ENTIRE reply must be ONLY a valid JSON object — "
    "no markdown fences, no code blocks, no XML tags, no <invoke>, no <tool_calls> wrapper, "
    "no prose before or after. Use exactly this structure:\n\n"
    'Single call: {{"tool_calls": [{{"name": "Read", "arguments": {{"path": "/home/user/file.py", "line_offset": 10}}}}]}}\n'
    'Multiple calls: {{"tool_calls": [{{"name": "Grep", "arguments": {{"pattern": "def main"}}}}, '
    '{{"name": "Glob", "arguments": {{"pattern": "**/*"}}}}]}}\n\n'
    "Rules:\n"
    '- The outermost structure is a single JSON object with one key: "tool_calls" (an array).\n'
    '- Each entry has exactly two keys: "name" and "arguments".\n'
    "- Use EXACTLY the function names and parameter names from the definitions above.\n"
    '- The value of "arguments" must be a JSON object with only declared keys — no extra fields.\n'
    "- Do NOT use XML, DSML tags, or any non-JSON format.\n"
    "{choice}"
)

CHOICE_INSTRUCTIONS = {
    "required": "You MUST call one or more functions from the list above.",
    "function": "You MUST call exactly the function specified below and no other functions.",
}

JSON_MODE_INSTRUCTION = """You must reply with ONLY a valid JSON object, no markdown fences, no extra text, no explanations, no comments inside the JSON.
{constraints}"""


def _tool_function(tool: Any) -> dict | None:
    if not isinstance(tool, dict):
        return None
    if "function" in tool:
        fn = tool["function"]
        return fn if isinstance(fn, dict) else None
    if isinstance(tool.get("name"), str):
        return tool
    return None


def _choice_name(tool_choice: Any) -> str | None:
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
    return None


def render_tool_schema(tools: list[Any] | None, tool_choice: Any = None) -> str | None:
    if not tools:
        return None
    functions: list[dict] = []
    for tool in tools:
        fn = _tool_function(tool)
        if fn is not None and isinstance(fn.get("name"), str) and fn["name"]:
            functions.append(fn)
    if not functions:
        return None
    choice = _choice_name(tool_choice)
    if choice == "none":
        return None
    lines = []
    for i, fn in enumerate(functions, start=1):
        lines.append(f"{i}. name: {fn['name']}")
        if fn.get("description"):
            lines.append(f"   description: {fn['description']}")
        if fn.get("strict"):
            lines.append("   strict: true (output arguments must match the schema exactly)")
        params = fn.get("parameters")
        if params is not None:
            if isinstance(params, str):
                params_json = params
            else:
                params_json = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
            lines.append(f"   parameters (JSON Schema): {params_json}")
    if choice in CHOICE_INSTRUCTIONS:
        choice_line = CHOICE_INSTRUCTIONS[choice]
    elif isinstance(choice, str) and choice not in ("auto", "none", "required"):
        choice_line = f"You MUST call exactly the function {choice} and no other functions."
    else:
        choice_line = "If you do not need to call any function, reply normally with your answer."
    return TOOL_CALL_INSTRUCTION.format(functions="\n".join(lines), choice=choice_line)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image_url":
                    continue
        return "".join(parts).strip()
    return ""


def _content_fingerprint(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image_url":
                    image_url = item.get("image_url")
                    if isinstance(image_url, str):
                        parts.append(image_url)
                    elif isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                        parts.append(image_url["url"])
        return "\n".join(parts)
    return ""


def _is_ephemeral_system(content: str) -> bool:
    """Heuristic: client-injected system reminders/notices are not stable anchors.

    Kimi-style clients append ``<system-reminder>...`` / ``<system>...`` blocks that
    change with the date/time or compaction cycle. Hashing them makes the session
    fingerprint drift on every turn, so we exclude them entirely.
    """
    return content.strip().lower().startswith("<system")


def context_sequence(messages: list[Any], user: str | None = None) -> tuple[str, ...]:
    """Build a stable anchor fingerprint of the conversation for session dedup.

    Only the first ``CONTEXT_ANCHOR_MAX`` system/user messages contribute
    content digests. Assistant, tool, and ephemeral ``<system...>`` messages are
    ignored. The result is therefore bounded and append-stable: later turns (and
    even later compaction by the client) do not change the leading anchor, so a
    session can be re-found with prefix matching after the tail of the history
    has been rewritten.
    """
    scope = f"\0{user or ''}"
    sequence: list[str] = []
    for msg in messages:
        if len(sequence) >= CONTEXT_ANCHOR_MAX:
            break
        role = getattr(msg, "role", "user")
        if role not in ("system", "user"):
            continue
        content = _content_fingerprint(getattr(msg, "content", ""))
        if not content.strip():
            continue
        if _is_ephemeral_system(content):
            continue
        digest = hashlib.sha256(f"{role}\0{content}{scope}".encode()).hexdigest()
        sequence.append(digest)
    return tuple(sequence)


def _render_tool_call_mention(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function")
    if isinstance(fn, dict):
        name = fn.get("name") or ""
        args = fn.get("arguments") or ""
    else:
        name = call.get("name") or ""
        args = call.get("arguments") or ""
    if isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False)
    # Render in the target JSON format so DeepSeek mimics it instead of its native XML/DSML.
    return f'{{"tool_calls": [{{"name": "{name}", "arguments": {args}}}]}}' if name else ""


def render_message(msg: Any) -> str:
    role = getattr(msg, "role", "user")
    text = _content_text(getattr(msg, "content", ""))
    if role in ("user", "system"):
        return text
    if role == "assistant":
        parts = []
        if text:
            parts.append(text)
        for call in getattr(msg, "tool_calls", None) or []:
            mention = _render_tool_call_mention(call)
            if mention:
                parts.append(mention)
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_call":
                    mention = _render_tool_call_mention(item)
                    if mention:
                        parts.append(mention)
        return "; ".join(parts)
    if role == "tool":
        tool_call_id = getattr(msg, "tool_call_id", None) or ""
        prefix = f"Tool result ({tool_call_id})" if tool_call_id else "Tool result"
        return f"{prefix}: {text}"
    if role == "function":
        name = getattr(msg, "name", None) or ""
        return f"Function {name} returned: {text}"
    return text


def _has_tool_call(msg: Any) -> bool:
    """True when an assistant message carries at least one tool call."""
    if getattr(msg, "tool_calls", None):
        return True
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_call":
                return True
    return False


def _latest_tool_exchange(messages: list[Any]) -> list[Any]:
    """The newest assistant tool-call plus the results answering it.

    A client resends the whole tool round on every step, but steps 1..k-1 are
    already in the upstream session: each exchange was sent once, when it was
    new. Only the last call and its results are new material, so send just those
    instead of the whole round. Leading assistant messages of the same turn are
    kept, since prose and its call are often separate messages.
    """
    last = -1
    for index, msg in enumerate(messages):
        if getattr(msg, "role", None) == "assistant" and _has_tool_call(msg):
            last = index
    if last < 0:
        return []
    start = last
    while start > 0 and getattr(messages[start - 1], "role", None) == "assistant":
        start -= 1
    return messages[start:]


def _render_tool_tail(messages: list[Any], max_chars: int | None = None) -> str:
    parts = []
    tool_parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "assistant":
            text = _content_text(getattr(msg, "content", ""))
            if text.strip():
                parts.append(text.strip())  # assistant's own words (incl. intent) come first
            calls = getattr(msg, "tool_calls", None) or []
            for call in calls:
                mention = _render_tool_call_mention(call)
                if mention:
                    parts.append(mention)
            content_list = getattr(msg, "content", None)
            if isinstance(content_list, list):
                for item in content_list:
                    if isinstance(item, dict) and item.get("type") == "tool_call":
                        mention = _render_tool_call_mention(item)
                        if mention:
                            parts.append(mention)
        elif role in ("tool", "function"):
            text = render_message(msg)
            if text:
                tool_parts.append(text)
    # Truncate individual tool results if total exceeds max_chars.
    trailer = "Continue the conversation and provide the final answer based on the tool results."
    fixed_len = sum(len(p) + 1 for p in parts) + len(trailer) + 1
    if max_chars is not None and tool_parts:
        budget = max_chars - fixed_len
        total_tool_len = sum(len(t) + 1 for t in tool_parts)
        if total_tool_len > budget:
            per_result = max(200, (budget // len(tool_parts)) - 1)
            truncated = []
            for t in tool_parts:
                if len(t) > per_result:
                    truncated.append(t[:per_result] + "\n...[truncated]")
                else:
                    truncated.append(t)
            tool_parts = truncated
    parts.extend(tool_parts)
    parts.append(trailer)
    return "\n".join(parts)


def extract_last_user(messages: list[Any]) -> str:
    if not messages:
        raise ValueError("messages is required")
    for msg in reversed(messages):
        if getattr(msg, "role", None) in ("user", "system"):
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        if item.get("type") == "text" and isinstance(item.get("text"), str):
                            parts.append(item["text"])
                        elif item.get("type") == "image_url":
                            continue
                text = "".join(parts).strip()
                if text:
                    return text
                continue
            raise ValueError("unsupported message content")
    raise ValueError("no user message found")


def is_tool_round(messages: list[Any]) -> bool:
    for msg in messages:
        role = getattr(msg, "role", None)
        if role in ("tool", "function"):
            return True
        if role == "assistant" and getattr(msg, "tool_calls", None):
            return True
        if role == "assistant" and isinstance(getattr(msg, "content", None), list):
            for item in msg.content:
                if isinstance(item, dict) and item.get("type") == "tool_call":
                    return True
    return False


def _has_history(messages: list[Any]) -> bool:
    user_count = 0
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "user":
            user_count += 1
            continue
        text = _content_text(getattr(msg, "content", ""))
        if role == "assistant" and (text or getattr(msg, "tool_calls", None)):
            return True
        if role in ("tool", "function") and text:
            return True
    return user_count > 1


def _tail_after_last_user(messages: list[Any]) -> list[Any]:
    index = -1
    for i, msg in enumerate(messages):
        if getattr(msg, "role", None) in ("user", "system"):
            index = i
    if index < 0:
        return list(messages)
    return list(messages[index + 1 :])


def _is_tool_round_tail(messages: list[Any]) -> bool:
    for msg in messages:
        role = getattr(msg, "role", None)
        if role in ("tool", "function"):
            return True
        if role == "assistant" and getattr(msg, "tool_calls", None):
            return True
        if role == "assistant" and isinstance(getattr(msg, "content", None), list):
            for item in msg.content:
                if isinstance(item, dict) and item.get("type") == "tool_call":
                    return True
    return False


def extract_system(messages: list[Any]) -> str:
    parts = []
    for msg in messages:
        if getattr(msg, "role", None) == "system":
            text = _content_text(getattr(msg, "content", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def render_json_mode(response_format: Any) -> str | None:
    if response_format is None:
        return None
    constraints = "The JSON object must be the only thing in your reply."
    schema: Any = None
    if isinstance(response_format, str):
        if response_format != "json_object":
            return None
    elif isinstance(response_format, dict):
        rtype = response_format.get("type")
        if rtype == "json_schema":
            raw = response_format.get("json_schema")
            schema = raw.get("schema") if isinstance(raw, dict) else None
        elif rtype != "json_object":
            return None
    else:
        return None
    if schema is not None:
        constraints = f"The JSON object must match this JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    return JSON_MODE_INSTRUCTION.format(constraints=constraints)


def build_compact_prompt(
    messages: list[Any],
    max_pairs: int = 10,
    max_tool_results: int = 3,
    tool_schemas: dict | None = None,
) -> tuple[str, str]:
    """Build a compact prompt for retry after CONTEXT_LENGTH_EXCEEDED.

    Returns (system_text, compact_body). The caller must create a NEW session
    and send system first, then compact_body as the second message. This keeps
    each individual prompt under DeepSeek's per-request limit (~15000 chars).

    Strategy: keep system + last N user/assistant pairs (no tool results in
    middle) + last M raw tool results at the tail for immediate context.
    """
    system = extract_system(messages)

    # Collect user/assistant pairs, skipping tool messages and intermediate
    # assistant tool-call-only turns.
    pairs: list[tuple[str, str]] = []  # (user_text, assistant_text)
    pending_user: str | None = None
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "system":
            continue
        text = _content_text(getattr(msg, "content", ""))
        has_calls = bool(getattr(msg, "tool_calls", None))
        if role == "user":
            pending_user = text or ""
        elif role == "assistant" and (text or has_calls):
            # Only include assistant messages that have actual content
            if text:
                if pending_user is not None:
                    pairs.append((pending_user, text))
                    pending_user = None
            # tool-call-only assistants are skipped in the compact view

    # Take last N pairs
    recent_pairs = pairs[-max_pairs:] if len(pairs) > max_pairs else pairs

    # Render conversation body
    parts: list[str] = []
    if len(pairs) > max_pairs:
        parts.append(f"[Earlier conversation truncated — {len(pairs) - max_pairs} message(s) omitted]")
    for user_text, asst_text in recent_pairs:
        if user_text:
            parts.append(f"User: {user_text}")
        if asst_text:
            parts.append(f"Assistant: {asst_text}")

    # Append last M tool results raw (these are the most actionable context)
    tool_results: list[str] = []
    for msg in reversed(messages):
        role = getattr(msg, "role", None)
        if role in ("tool", "function"):
            rendered = render_message(msg)
            if rendered:
                tool_results.append(rendered)
            if len(tool_results) >= max_tool_results:
                break
    if tool_results:
        parts.append("")
        parts.append("Recent tool results:")
        for tr in reversed(tool_results):  # restore chronological order
            parts.append(tr)

    compact_body = "\n\n".join(parts).strip()
    return system, compact_body


def build_prompt(
    messages: list[Any],
    tools: list[Any] | None = None,
    tool_choice: Any = None,
    has_session: bool = False,
    response_format: Any = None,
) -> tuple[str, bool]:
    schema = render_tool_schema(tools, tool_choice)
    tools_present = schema is not None
    json_block = render_json_mode(response_format)

    if has_session:
        tail = _tail_after_last_user(messages)
        if _is_tool_round_tail(tail):
            exchange = _latest_tool_exchange(tail) or tail
            tail_prompt = _render_tool_tail(exchange, max_chars=MAX_APPEND_PROMPT_CHARS)
            return tail_prompt, True
        base = extract_last_user(messages)
        blocks = []
        if json_block:
            blocks.append(json_block)
        blocks.append(base)
        return "\n\n".join(blocks), tools_present

    tool_round_active = is_tool_round(messages)
    if tool_round_active or _has_history(messages):
        system = extract_system(messages)
        history_parts = []
        for msg in messages:
            role = getattr(msg, "role", "user")
            if role == "system":
                continue
            text = render_message(msg)
            if text:
                history_parts.append(f"{role.capitalize()}: {text}")
        history = "\n".join(history_parts)
        fixed_blocks = [b for b in (system, schema, json_block) if b]
        fixed_len = sum(len(b) + 2 for b in fixed_blocks)
        history_budget = max(0, MAX_APPEND_PROMPT_CHARS - fixed_len)
        if history and len(history) > history_budget:
            marker = "[Earlier conversation truncated]"
            keep = max(0, history_budget - len(marker))
            history = marker + history[-keep:]
        blocks = fixed_blocks + ([history] if history else [])
        prompt = "\n\n".join(b for b in blocks if b.strip())
        if not prompt.strip():
            prompt = schema or extract_last_user(messages)
        return prompt, tools_present or tool_round_active

    base = extract_last_user(messages)
    blocks = []
    system = extract_system(messages)
    if system:
        blocks.append(system)
    if schema:
        blocks.append(schema)
    if json_block:
        blocks.append(json_block)
    blocks.append(base)
    return "\n\n".join(blocks), tools_present
