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


# DSML is the canonical tool-call wire format. DeepSeek speaks it natively, so
# asking for JSON only produced hybrids: the model answered half in its own
# tags and half in the demanded shape. The prompt now asks for DSML and the
# parser in ``parse.py`` reads it back.

# Tags are assembled from parts rather than written out: the canonical marker is
# two pipes, the word DSML, two more pipes, then the tag body. Keeping the
# pieces separate also makes the shape obvious at a glance.
_DSML_BARS = "|" * 2
_DSML_WORD = "DSML"


def _dsml_tag(body: str, closing: bool = False) -> str:
    """Render one canonical DSML tag: marker, then the tag body."""
    slash = "/" if closing else ""
    return "<" + slash + _DSML_BARS + _DSML_WORD + _DSML_BARS + body + ">"


def _dsml_param(name: str, value: str, string: bool = True) -> str:
    """Render one argument: a parameter tag holding the value."""
    flag = "true" if string else "false"
    opener = _dsml_tag(f'parameter name="{name}" string="{flag}"')
    return opener + value + _dsml_tag("parameter", closing=True)


# One short example in the exact shape ``_parse_dsml`` reads back: the invoke
# carries the function name, each argument sits in its own parameter tag, and the
# ``string`` flag says whether the value is text or has to be coerced.
DSML_EXAMPLE = "\n".join(
    (
        _dsml_tag("tool_calls"),
        "  " + _dsml_tag('invoke name="Read"'),
        "    " + _dsml_param("path", "/home/user/file.py"),
        "    " + _dsml_param("line_offset", "10", string=False),
        "  " + _dsml_tag("invoke", closing=True),
        _dsml_tag("tool_calls", closing=True),
    )
)

TOOL_CALL_INSTRUCTION = (
    "You have access to the following functions. Call them when the user's request requires it.\n\n"
    "{functions}\n\n"
    "When you need to call a function, write it in DSML: one invoke block per call, "
    "every argument in its own parameter tag. Several calls go in the one tool_calls "
    "container. Use exactly this shape:\n\n"
    "{example}\n\n"
    "Rules:\n"
    "- Use EXACTLY the function names and parameter names from the definitions above.\n"
    '- One parameter tag per argument; string="true" for text, string="false" for '
    "numbers, booleans, arrays and objects.\n"
    "- Do not add parameters that are not declared above.\n"
    "{choice}"
)

# Repeats the wire format in the tail of every tool round. The schema block that
# carries the instruction sits at the top of a long session, so by the tenth round
# it is tens of thousands of characters away and the model drifts back to its own
# habits; a short reminder next to the newest result keeps the format in view.
TOOL_FORMAT_TAIL_DSML = "Tool-call format:\n\n" + DSML_EXAMPLE

CHOICE_INSTRUCTIONS = {
    "required": "You MUST call one or more functions from the list above.",
    "function": "You MUST call exactly the function specified below and no other functions.",
}

# Single-call variant of ``TOOL_CALL_INSTRUCTION``. Batching several calls into
# one reply is where most mangled payloads come from (sibling calls with no
# separator, a schema split across calls, a repeated parameter name, one
# ambiguous ``{path}`` shared by two calls): with exactly one call there is no
# boundary to get wrong. A caller that needs several actions gets them one per
# round instead.
SINGLE_CALL_INSTRUCTION = (
    "You have access to the following functions. Call them when the user's request requires it.\n\n"
    "{functions}\n\n"
    "When you need to call a function, write it in DSML: one invoke block holding the "
    "call, every argument in its own parameter tag, all inside one tool_calls container. "
    "Use exactly this shape:\n\n"
    "{example}\n\n"
    "Rules:\n"
    "- Call exactly ONE function per reply: the container holds a single invoke block.\n"
    "- If the task needs several calls, make the most important one first and stop; "
    "its result comes back and you can call the next one then.\n"
    "- Use EXACTLY the function name and parameter names from the definitions above.\n"
    '- One parameter tag per argument; string="true" for text, string="false" for '
    "numbers, booleans, arrays and objects.\n"
    "- Do not add parameters that are not declared above.\n"
    "{choice}"
)

SINGLE_CHOICE_REQUIRED = "You MUST call exactly one function from the list above."

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


def render_tool_schema(tools: list[Any] | None, tool_choice: Any = None, single_call: bool = False) -> str | None:
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
        choice_line = SINGLE_CHOICE_REQUIRED if single_call and choice == "required" else CHOICE_INSTRUCTIONS[choice]
    elif isinstance(choice, str) and choice not in ("auto", "none", "required"):
        choice_line = f"You MUST call exactly the function {choice} and no other functions."
    else:
        choice_line = "If you do not need to call any function, reply normally with your answer."
    template = SINGLE_CALL_INSTRUCTION if single_call else TOOL_CALL_INSTRUCTION
    return template.format(functions="\n".join(lines), example=DSML_EXAMPLE, choice=choice_line)


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


def _image_fingerprint(uri: str) -> str:
    """A short, stable stand-in for an image data URI.

    The digest keeps the conversation anchor identical for the same image while
    avoiding an 11 MB string copy on every request that carries one: the
    fingerprint is built from the hash, not from the URI itself.
    """
    return "image:" + hashlib.sha256(uri.encode("utf-8")).hexdigest()


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
                        parts.append(_image_fingerprint(image_url))
                    elif isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                        parts.append(_image_fingerprint(image_url["url"]))
        return "\n".join(parts)
    return ""


def _is_ephemeral_system(content: str) -> bool:
    """Heuristic: client-injected system reminders/notices are not stable anchors.

    Kimi-style clients append ``<system-reminder>...`` / ``<system>...`` blocks that
    change with the date/time or compaction cycle. Hashing them makes the session
    fingerprint drift on every turn, so we exclude them entirely.
    """
    return content.strip().lower().startswith("<system")


# A client that pulls media out of a tool result (Kimi's openai protocol does
# this, see ``extractToolMedia``) leaves the tool message as plain text and
# appends the image as a separate ``role="user"`` message carrying only this
# label. That message is a transport detail, not a user turn: treating it as the
# last user turn truncates the delta prompt to the label alone, so the model sees
# no question and no tool result and calls the same tool again forever.
MEDIA_RELAY_PROMPT = "attached media from tool result:"


def _is_media_relay(content: str) -> bool:
    return content.strip().lower() == MEDIA_RELAY_PROMPT


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


def _dsml_params(arguments: Any) -> str:
    """Render a call's arguments as DSML parameter tags.

    Text values keep their own form; everything else is emitted as compact JSON
    with ``string="false"`` so the parser coerces it back to the same type.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            return _dsml_param("arguments", arguments) if arguments else ""
    elif isinstance(arguments, dict):
        parsed = arguments
    else:
        return ""
    parts = []
    for key, value in parsed.items():
        if isinstance(value, str):
            parts.append(_dsml_param(str(key), value))
        else:
            parts.append(_dsml_param(str(key), json.dumps(value, ensure_ascii=False), string=False))
    return "".join("\n    " + part for part in parts)


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
    if not name:
        return ""
    # Rendered in the canonical DSML format so the model sees its own calls the
    # same way the instruction asks it to write new ones.
    return _dsml_tag(f'invoke name="{name}"') + _dsml_params(args) + "\n" + _dsml_tag("invoke", closing=True)


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
    # A flat "provide the final answer" reads as an instruction to stop calling
    # tools, which fights the agent loop on multi-step work. Offer both outcomes
    # instead so a mid-task turn keeps calling.
    trailer = "Continue the conversation: call more functions if the task still needs them, otherwise give the final answer based on the tool results."
    # The schema block that states the wire format stays at the top of the
    # session, so by a later round it is tens of thousands of characters behind
    # the newest result. Restating the format here keeps it in view.
    format_block = TOOL_FORMAT_TAIL_DSML
    fixed_len = sum(len(p) + 1 for p in parts) + len(trailer) + len(format_block) + 2
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
    parts.append(format_block)
    parts.append(trailer)
    return "\n".join(parts)


def extract_last_user(messages: list[Any]) -> str:
    if not messages:
        raise ValueError("messages is required")
    for msg in reversed(messages):
        role = getattr(msg, "role", None)
        if role not in ("user", "system"):
            continue
        content = getattr(msg, "content", "")
        # A client-injected <system-reminder> is not the user's turn. Kimi sends
        # these as a separate role="user" message after the real question, so
        # treating one as the user's text sends a prompt with no question at all
        # and the model answers as if the user had said nothing.
        text_content = _content_text(content)
        if _is_ephemeral_system(text_content) or _is_media_relay(text_content):
            continue
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
            # A client-injected <system-reminder> is not a real user turn.
            if _is_ephemeral_system(_content_text(getattr(msg, "content", ""))):
                continue
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
        role = getattr(msg, "role", None)
        if role not in ("user", "system"):
            continue
        text = _content_text(getattr(msg, "content", ""))
        if _is_ephemeral_system(text) or _is_media_relay(text):
            continue
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
            # Skip client-injected <system-reminder>/<system> blocks: they change
            # every turn (date, TodoList) and must not become the system prompt.
            if text and not _is_ephemeral_system(text):
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


COMPACT_SUMMARY_HEADER = "[Summary of earlier conversation]"
COMPACT_TRUNCATION_NOTE = "[Earlier conversation truncated — {n} message(s) omitted]"

HANDOFF_SUMMARY_INSTRUCTION = (
    "The conversation below is the earlier part of a long session that is being "
    "carried over to a new chat because the original hit its context limit. "
    "Summarize it for handoff. Keep every fact, decision, constraint, file path, "
    "command and open task that the next turn might depend on; drop chit-chat. "
    "Write plain prose, no markdown fences, and do not answer the conversation."
)


def _collect_pairs(messages: list[Any]) -> tuple[list[tuple[str, str]], str | None]:
    """Split messages into completed (user, assistant) pairs and a pending user turn.

    ``pending_user`` is the user message with no assistant reply yet — the request
    currently being answered. A client-injected ``<system-reminder>`` is skipped
    so it neither starts a pair nor replaces the real pending question.
    """
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "system":
            continue
        text = _content_text(getattr(msg, "content", ""))
        has_calls = bool(getattr(msg, "tool_calls", None))
        if role == "user":
            if _is_ephemeral_system(text):
                continue
            pending_user = text or ""
        elif role == "assistant" and (text or has_calls) and text and pending_user is not None:
            pairs.append((pending_user, text))
            pending_user = None
    return pairs, pending_user


def dropped_pairs_source(messages: list[Any], max_pairs: int, max_chars: int) -> str:
    """Render the older pairs that a compact prompt would drop, for summarization.

    Returns "" when nothing would be dropped. The rendered text is truncated to
    ``max_chars`` (keeping the newest material) so the summarize request stays
    within the per-request limit.
    """
    pairs, _ = _collect_pairs(messages)
    if len(pairs) <= max_pairs:
        return ""
    dropped = _render_pairs(pairs[:-max_pairs])
    return _truncate_head(dropped, max_chars)


def _render_pairs(pairs: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for user_text, asst_text in pairs:
        if user_text:
            parts.append(f"User: {user_text}")
        if asst_text:
            parts.append(f"Assistant: {asst_text}")
    return "\n\n".join(parts)


def _truncate_head(text: str, limit: int) -> str:
    """Keep the end of ``text`` (the most recent material) within ``limit`` chars."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "…[earlier content truncated]\n"
    if len(marker) >= limit:
        # No room for the marker plus content: just hard-cut the tail.
        return text[-limit:]
    keep = limit - len(marker)
    return marker + text[-keep:]


def build_compact_prompt(
    messages: list[Any],
    max_pairs: int = 10,
    max_tool_results: int = 3,
    tools: list[Any] | None = None,
    tool_choice: Any = None,
    response_format: Any = None,
    summary: str | None = None,
    max_chars: int = MAX_APPEND_PROMPT_CHARS,
    single_call: bool = False,
) -> tuple[str, str]:
    """Build a compact prompt for retry after CONTEXT_LENGTH_EXCEEDED.

    Returns (system_text, compact_body) for a NEW session. The caller sends them
    as a single prompt (system + body); splitting it into two messages is no
    longer required.

    The body keeps, in order: an optional summary of the dropped history, the
    last N completed user/assistant pairs, the last M raw tool results, and the
    **current turn** (the pending user question plus, for a tool round, the
    latest tool call and its results). Preserving the current turn is essential:
    without it the fresh session answers an older question. The assembled body
    is trimmed to ``max_chars`` so the compact request cannot itself overflow.
    """
    system = "\n\n".join(
        block
        for block in (
            extract_system(messages),
            render_tool_schema(tools, tool_choice, single_call=single_call),
            render_json_mode(response_format),
        )
        if block
    )

    pairs, pending_user = _collect_pairs(messages)
    body_budget = max(0, max_chars - len(system) - 2)

    # The current turn is everything after the last real user message. When it
    # is a tool round, its tail is rendered as the current turn below and
    # already carries the round's own tool results, so only tool results from
    # *before* that turn go into the standalone "Recent tool results" section.
    tail = _tail_after_last_user(messages)
    tail_start = len(messages) - len(tail)
    tail_has_tools = _is_tool_round_tail(tail)

    current_parts: list[str] = []
    if pending_user:
        current_parts.append(f"User: {pending_user}")
    if tail_has_tools:
        exchange = _latest_tool_exchange(tail) or tail
        rendered_tail = _render_tool_tail(exchange, max_chars=max(2000, body_budget))
        if rendered_tail:
            current_parts.append(rendered_tail)
    current_text = "\n\n".join(current_parts)

    # Last M older tool results, newest-kept, rendered back to chronological
    # order. Tool results inside the current turn are skipped (see above).
    tool_results: list[str] = []
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if getattr(msg, "role", None) not in ("tool", "function"):
            continue
        if tail_has_tools and index >= tail_start:
            continue
        rendered = render_message(msg)
        if rendered:
            tool_results.append(rendered)
        if len(tool_results) >= max_tool_results:
            break
    tool_results.reverse()
    tool_text = "\n".join(tool_results)

    header: list[str] = []
    if summary and summary.strip():
        header.append(f"{COMPACT_SUMMARY_HEADER}\n{summary.strip()}")

    kept = pairs[-max_pairs:] if len(pairs) > max_pairs else list(pairs)
    omitted = len(pairs) - len(kept)

    def assemble() -> str:
        parts = list(header)
        if omitted:
            parts.append(COMPACT_TRUNCATION_NOTE.format(n=omitted))
        rendered_pairs = _render_pairs(kept)
        if rendered_pairs:
            parts.append(rendered_pairs)
        if tool_text:
            parts.append("Recent tool results:\n" + tool_text)
        if current_text:
            parts.append(current_text)
        return "\n\n".join(p for p in parts if p).strip()

    body = assemble()
    while len(body) > body_budget:
        if kept:
            kept.pop(0)
            omitted += 1
        elif tool_text:
            shrunk = _truncate_head(tool_text, max(0, len(tool_text) - (len(body) - body_budget) - 64))
            tool_text = "" if len(shrunk) < 64 else shrunk
        else:
            body = _truncate_head(body, body_budget)
            break
        body = assemble()
    return system, body


def build_prompt(
    messages: list[Any],
    tools: list[Any] | None = None,
    tool_choice: Any = None,
    has_session: bool = False,
    response_format: Any = None,
    single_call: bool = False,
) -> tuple[str, bool]:
    schema = render_tool_schema(tools, tool_choice, single_call=single_call)
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
            # A client-injected reminder is not conversation history.
            if text and not _is_ephemeral_system(text):
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
