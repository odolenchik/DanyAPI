import json
from typing import Any

import pytest

from danyapi.tools import (
    ToolCall,
    _coerce_scalar,
    _content_fingerprint,
    _content_text,
    _extract_calls,
    _extract_one_call,
    _fix_unbalanced_json,
    _has_history,
    _is_jsonish_arguments,
    _is_tool_round_tail,
    _loads_lenient,
    _normalize_single_quotes,
    _parse_bare_array_calls,
    _parse_dsml,
    _parse_xml_tool_calls,
    _render_tool_call_mention,
    _repair_dsml_bracketless_close,
    _repair_dsml_separator,
    _strip_dsml,
    _tail_after_last_user,
    _tool_function,
    build_prompt,
    degenerate_repeat_count,
    extract_last_user,
    extract_system,
    format_tool_message,
    is_degenerate_repeat,
    is_tool_round,
    limit_tool_calls,
    parse_tool_calls,
    render_json_mode,
    render_message,
    render_tool_schema,
    tool_call_deltas,
    tool_schema_map,
)
from danyapi.tools.prompt import DSML_EXAMPLE, _dsml_param, _dsml_tag

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Message:
    def __init__(self, role="user", content: Any = "", tool_calls=None, tool_call_id=None, name=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.name = name


def test_render_tool_schema_basic():
    schema = render_tool_schema([WEATHER_TOOL])
    assert schema is not None
    assert "get_weather" in schema
    assert '"city"' in schema
    # The call format is DSML: one container, an invoke per call.
    assert _dsml_tag("tool_calls") in schema
    assert _dsml_tag('invoke name="Read"') in schema


def test_render_tool_schema_has_no_json_call_format():
    """The instruction must not ask for JSON: that demand is what produced hybrids."""
    schema = render_tool_schema([WEATHER_TOOL])
    assert schema is not None
    assert "Do NOT use XML, DSML" not in schema
    assert "valid JSON object" not in schema
    assert '"tool_calls": [' not in schema


def test_render_tool_schema_example_round_trips():
    """The example must parse back into a call, or the model copies a shape the
    parser cannot read."""
    example = DSML_EXAMPLE
    calls, _wrapper = _parse_dsml(example)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "/home/user/file.py", "line_offset": 10}


def test_render_tool_schema_single_call_asks_for_one():
    schema = render_tool_schema([WEATHER_TOOL], single_call=True)
    assert schema is not None
    # The multi-call example must be gone: it taught the model to batch.
    assert "Multiple calls:" not in schema
    assert "exactly ONE function per reply" in schema
    assert "the container holds a single invoke block" in schema


def test_render_tool_schema_single_call_required_choice():
    schema = render_tool_schema([WEATHER_TOOL], "required", single_call=True)
    assert schema is not None
    assert "MUST call exactly one function" in schema


def test_render_tool_schema_single_call_named_function_choice():
    schema = render_tool_schema([WEATHER_TOOL], "get_weather", single_call=True)
    assert schema is not None
    assert "exactly the function get_weather" in schema


def test_render_tool_schema_single_call_auto_keeps_opt_out():
    schema = render_tool_schema([WEATHER_TOOL], "auto", single_call=True)
    assert schema is not None
    assert "reply normally with your answer" in schema


def test_render_tool_schema_default_allows_several_invokes():
    schema = render_tool_schema([WEATHER_TOOL])
    assert schema is not None
    assert "Several calls go in the one tool_calls container" in schema


def test_build_prompt_single_call_reaches_schema():
    prompt, tool_mode = build_prompt([Message(role="user", content="weather in Moscow?")], [WEATHER_TOOL], single_call=True)
    assert tool_mode is True
    assert "exactly ONE function per reply" in prompt


def test_render_tool_schema_empty_tools():
    assert render_tool_schema([]) is None
    assert render_tool_schema(None) is None


def test_render_tool_schema_tool_choice_none():
    assert render_tool_schema([WEATHER_TOOL], "none") is None


def test_render_tool_schema_tool_choice_required():
    schema = render_tool_schema([WEATHER_TOOL], "required")
    assert schema is not None
    assert schema is not None
    assert "MUST call" in schema


def test_render_tool_schema_tool_choice_function_dict():
    schema = render_tool_schema([WEATHER_TOOL], {"type": "function", "function": {"name": "get_weather"}})
    assert schema is not None
    assert schema is not None
    assert "get_weather" in schema


def test_render_tool_schema_strict_flag_rendered():
    tool = {
        "type": "function",
        "function": {"name": "calc", "strict": True, "parameters": {"type": "object", "properties": {"x": {"type": "number"}}}},
    }
    schema = render_tool_schema([tool])
    assert schema is not None
    assert schema is not None
    assert "strict" in schema


def test_render_tool_schema_compact_parameters_json():
    schema = render_tool_schema([WEATHER_TOOL])
    assert schema is not None
    assert schema is not None
    assert '{"type":"object"' in schema


def test_is_tool_round_plain_user():
    assert not is_tool_round([Message(role="user", content="hi")])


def test_is_tool_round_tool_role():
    assert is_tool_round([Message(role="tool", content="22C", tool_call_id="call_1")])


def test_is_tool_round_function_role():
    assert is_tool_round([Message(role="function", content="42", name="calc")])


def test_is_tool_round_assistant_tool_calls():
    msg = Message(
        role="assistant",
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
    )
    assert is_tool_round([msg])


def test_is_tool_round_assistant_content_list_tool_call():
    msg = Message(
        role="assistant",
        content=[{"type": "tool_call", "id": "c1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
    )
    assert is_tool_round([msg])


def test_extract_last_user_last_user():
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
    ]
    assert extract_last_user(messages) == "q2"


def test_extract_last_user_list_content():
    msg = Message(role="user", content=[{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}])
    assert extract_last_user([msg]) == "look"


def test_extract_last_user_no_user():
    with pytest.raises(ValueError):
        extract_last_user([Message(role="assistant", content="a")])


def test_extract_last_user_empty():
    with pytest.raises(ValueError):
        extract_last_user([])


def test_parse_tool_calls_pure_json():
    text = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == '{"city": "Moscow"}'
    assert calls[0].id.startswith("call_")
    assert wrapper == ""


def test_parse_tool_calls_markdown_fences():
    text = '```json\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "London"}}]}\n```'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "get_weather"


def test_parse_tool_calls_prose_around():
    text = 'I will help you.\n\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Rome"}}]}\nHope that helps.'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert calls[0].name == "get_weather"
    assert "I will help you" in wrapper


def test_parse_tool_calls_legacy_function_call():
    text = '{"function_call": {"name": "get_weather", "arguments": {"city": "Paris"}}}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "get_weather"


def test_parse_tool_calls_multiple_calls():
    text = '{"tool_calls": [{"name": "a", "arguments": {"x": 1}}, {"name": "b", "arguments": {"y": 2}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["a", "b"]


def test_parse_tool_calls_content_with_calls():
    text = '{"content": "checking", "tool_calls": [{"name": "get_weather", "arguments": {"city": "Kyiv"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert wrapper == "checking"


def test_parse_tool_calls_arguments_as_string():
    text = '{"tool_calls": [{"name": "get_weather", "arguments": "{\\"city\\": \\"Oslo\\"}"}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].arguments == '{"city": "Oslo"}'


def test_parse_tool_calls_tool_key_in_fenced_blocks():
    # DeepSeek sometimes emits each call as a separate ```json fence using the
    # `tool` key instead of `name` (tooltrace request_id af623d265ec749c388732f46e156d222).
    text = '```json\n{"tool": "Read", "arguments": {"path": "/a.md"}}\n```\n```json\n{"tool": "Bash", "arguments": {"command": "ls"}}\n```'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["Read", "Bash"]


def test_parse_tool_calls_not_a_tool_call():
    assert parse_tool_calls("Just a normal answer.") is None
    assert parse_tool_calls('{"answer": 42}') is None
    assert parse_tool_calls("") is None
    assert parse_tool_calls("   ") is None


def test_parse_tool_calls_trailing_comma():
    text = '{"tool_calls": [{"name": "f", "arguments": {"x": 1},}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "f"
    assert json.loads(calls[0].arguments) == {"x": 1}


def test_parse_tool_calls_single_quotes():
    text = '{"tool_calls": [{"name": "f", "arguments": {"x": "it\'s"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "f"
    assert json.loads(calls[0].arguments) == {"x": "it's"}


def test_parse_tool_calls_single_quotes_with_double_quotes_inside():
    text = "{'tool_calls': [{'name': 'f', 'arguments': {'x': 'say \"hi\"'}}]}"
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"x": 'say "hi"'}


def test_parse_tool_calls_single_quotes_with_backslashes_inside():
    text = r"{'tool_calls': [{'name': 'f', 'arguments': {'path': 'C:\\Windows'}}]}"
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"path": r"C:\Windows"}


def test_parse_tool_calls_truncated_json_repaired():
    # A tool_calls payload truncated mid-stream (one closing brace short) is now
    # repaired by _fix_unbalanced_json via the last-resort fallback in parse_tool_calls.
    text = '{"tool_calls": [{"name": "f", "arguments": {"command": "ls"}}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "f"


def test_parse_tool_calls_truncated_missing_argument_key_repaired():
    # A single-call object truncated before its final brace is also repaired.
    text = '{"name": "f", "arguments": {"command": "ls"}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "f"


def test_parse_tool_calls_markdown_fenced_truncated_json():
    # Real-world case (tooltrace 194cca02): prose + an unclosed ```json fence whose
    # tool_calls object is truncated mid-stream. The last-resort fallback repairs it.
    text = (
        "Привет! Сейчас изучу парсер.\n\n"
        '```json\n{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "parse", '
        '"path": "/x", "-i": true}}, {"name": "Bash", "arguments": {"command": "ls -la /x"}}]'
    )
    parsed = parse_tool_calls(text, {"Grep": {"pattern": "string"}, "Bash": {"command": "string"}})
    assert parsed is not None
    calls, wrapper = parsed
    assert [c.name for c in calls] == ["Grep", "Bash"]
    assert "Привет!" in wrapper


def test_parse_tool_calls_unparseable_fenced_block_returns_none():
    # Regression: prose plus a closed ```json fence whose body is not valid JSON and
    # cannot be leniently repaired. The embedded-fence last resort called
    # _loads_lenient outside try/except, so ValueError escaped parse_tool_calls and
    # aborted the streaming response (ASGI 500) instead of falling back to text.
    text = "Вот результат:\n\n```json\n{not valid json at all\n```\n"
    assert parse_tool_calls(text) is None


def test_parse_tool_calls_corrupted_separator_repaired():
    # DeepSeek v4-pro emits `"tool_calls">` (stray > instead of `: [`), with `{` as
    # the array opener. The repair step converts it to valid JSON so wrapped-calls
    # extraction handles it normally. Flat args (no "arguments" key) are recovered
    # via _flat_arguments.
    text = (
        "{\n"
        '  "tool_calls">\n'
        "    {\n"
        '      "name": "Bash",\n'
        '      "command": "ls -la /var/lib/coolercontrol/"\n'
        "    },\n"
        "    {\n"
        '      "name": "Bash",\n'
        '      "command": "dpkg -S /usr/bin/coolercontrold"\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 2
    assert all(c.name == "Bash" for c in calls)
    assert json.loads(calls[0].arguments) == {"command": "ls -la /var/lib/coolercontrol/"}
    assert json.loads(calls[1].arguments) == {"command": "dpkg -S /usr/bin/coolercontrold"}


def test_parse_tool_calls_unquoted_key_angle_wrapper():
    # Regression (tooltrace 93870405): DeepSeek dropped the quotes around the
    # `tool_calls` key and wrapped the object in angle brackets, producing
    # `<{tool_calls: [...]}>`. The payload reached the client as plain text
    # (a "dead" tool call) because no repair path handled a bare key.
    text = (
        "<{tool_calls: ["
        '{"name": "Read", "arguments": {"path": "crates/agent00x-core/src/scope.rs", "line_offset": 19, "n_lines": 50}}, '
        '{"name": "Bash", "arguments": {"command": "sed -n \'25,45p\' config.toml"}}'
        "]}>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Read", "Bash"]
    assert json.loads(calls[0].arguments) == {"path": "crates/agent00x-core/src/scope.rs", "line_offset": 19, "n_lines": 50}
    assert json.loads(calls[1].arguments) == {"command": "sed -n '25,45p' config.toml"}


def test_parse_tool_calls_unquoted_key_no_wrapper():
    text = '{tool_calls: [{"name": "Read", "arguments": {"path": "a.rs"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "a.rs"}


def test_parse_tool_calls_unquoted_nested_keys():
    # Every bare key — not just the outer `tool_calls` — is quoted, so a payload
    # that omitted all key quotes still parses.
    text = '{tool_calls: [{name: "Read", arguments: {path: "a.rs"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "a.rs"}


def test_parse_tool_calls_quoted_json_unchanged():
    # The repair must not alter already-valid JSON.
    text = '{"tool_calls": [{"name": "Read", "arguments": {"path": "a.rs"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert json.loads(calls[0].arguments) == {"path": "a.rs"}


def test_parse_tool_calls_url_prose_not_treated_as_call():
    # A bare word followed by `:` inside prose (a URL) must not be quoted into a
    # bogus object; there is no enclosing brace, so this stays prose.
    parsed = parse_tool_calls("See https://example.com/a?b=1 for details")
    assert parsed is None


def test_quote_bare_keys_leaves_string_values_intact():
    from danyapi.tools.parse import _quote_bare_keys

    assert _quote_bare_keys('{a: "x:y"}') == '{"a": "x:y"}'
    assert _quote_bare_keys('{a: "http://x/y"}') == '{"a": "http://x/y"}'
    assert _quote_bare_keys('{"a": 1}') == '{"a": 1}'


def test_parse_tool_calls_bare_dict_trailing_comma():
    text = '{"name": "f", "arguments": {"x": 1,}}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"x": 1}


def test_parse_tool_calls_valid_backslash_pairs_preserved():
    # Regression (tooltrace 17d61eff): _fix_invalid_escapes used to rewrite the
    # second backslash of an already-valid ``\\`` pair when the next char was not
    # a JSON escape, producing ``Invalid \escape`` and dropping the whole call.
    # A regex argument with ``\\(`` and ``\\|`` must survive intact.
    text = '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "run_udp_server\\\\(|set_own_hash\\\\(|a\\\\|b", "output_mode": "content"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Grep"
    # JSON ``\\(`` decodes to a single backslash; the pair must round-trip as-is.
    assert json.loads(calls[0].arguments)["pattern"] == "run_udp_server\\(|set_own_hash\\(|a\\|b"


def test_parse_tool_calls_calls_wrapper_key():
    # DeepSeek sometimes wraps the array under ``calls`` instead of ``tool_calls``
    # (tooltrace 58d77038). The payload is valid JSON, so the key name alone must
    # not make it unparsed.
    text = '{"calls": [{"name": "Bash", "arguments": {"command": "ls"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Bash"]


def test_parse_tool_calls_tools_calls_wrapper_key():
    # Tooltrace a397635c used the ``tools_calls`` key with the same shape.
    text = '{"tools_calls": [{"name": "Read", "arguments": {"path": "/x"}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Read"]


def test_parse_tool_calls_nested_wrapper_element():
    # Tooltrace c204a696: the second element of the outer array is itself a
    # {"tool_calls": [...]} wrapper. Its inner calls must be recovered, not dropped.
    text = '{"tool_calls": [{"name": "Read", "arguments": {"path": "a.md"}}, {"tool_calls": [{"name": "Read", "arguments": {"path": "plan.md"}}]}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert [json.loads(c.arguments)["path"] for c in calls] == ["a.md", "plan.md"]


def test_parse_dsml_invoke_fused_with_json_arguments():
    # Tooltrace 3ab1fd2b: a DSML invoke whose opener is fused with an inline JSON
    # argument object and no <parameter> tags — the body never reaches the normal
    # <parameter> branch, so it needs its own recovery path.
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ccalls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Bash", "arguments": {"command": "sed -n 1,20p CONTRIBUTING.md"}}]}'
        "<\uff5c\uff5cDSML\uff5c\uff5cparameter>\n"
        "<\uff5c\uff5cDSML\uff5c\uff5c/ invoke>\n"
        "<\uff5c\uff5cDSML\uff5c\uff5c/calls>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Bash"]
    assert json.loads(calls[0].arguments) == {"command": "sed -n 1,20p CONTRIBUTING.md"}


def test_parse_xml_tool_calls_bash_invoke():
    text = '<tool_calls>\n<invoke name="bash">\n<command>Get-ChildItem -Name</command>\n</invoke>\n</tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"command": "Get-ChildItem -Name"}
    assert wrapper == ""


def test_parse_xml_tool_calls_multiple_invokes():
    text = '<tool_calls><invoke name="a"><x>1</x></invoke><invoke name="b"><y>2</y></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["a", "b"]
    assert json.loads(calls[0].arguments) == {"x": "1"}
    assert json.loads(calls[1].arguments) == {"y": "2"}


def test_parse_xml_tool_calls_parameter_tag():
    text = '<tool_calls><invoke name="get_weather"><parameter name="city">Moscow</parameter></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"city": "Moscow"}


def test_parse_xml_tool_calls_xml_entities():
    text = '<tool_calls><invoke name="bash"><command>echo &quot;a&quot; &amp; b</command></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"command": 'echo "a" & b'}


def test_parse_xml_tool_calls_prose_wrapper():
    text = 'Let me check.\n\n<tool_calls><invoke name="bash"><command>ls</command></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert wrapper == "Let me check."


def test_parse_xml_tool_calls_unquoted_name():
    text = "<tool_calls><invoke name=bash><command>pwd</command></invoke></tool_calls>"
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"


def test_parse_xml_tool_calls_plain_text_invoke():
    text = '<tool_calls><invoke name="bash">Get-ChildItem</invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"content": "Get-ChildItem"}


def test_parse_xml_truncated_parameter_value_recovered():
    # Real-world case (tooltrace 4492c2db): DeepSeek truncated the stream inside a
    # <parameter name="command"> value — no closing </parameter>, no </invoke>.
    text = (
        'Вытащу их request_id.\n\n<tool_calls>\n<invoke name="Bash">\n<parameter name="command">.venv/bin/python - <<\'PY\'\nimport json, time\nprint("x")\n='
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"].startswith(".venv/bin/python")
    assert "import json, time" in args["command"]
    assert wrapper == "Вытащу их request_id."


def test_parse_xml_truncated_parameter_after_complete_params():
    # Complete parameters plus a truncated tail parameter are all recovered.
    text = '<tool_calls>\n<invoke name="Read">\n<parameter name="path">/a/b.py</parameter>\n<parameter name="line_offset">160\n'
    parsed = parse_tool_calls(text, {"Read": {"path": "string", "line_offset": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["path"] == "/a/b.py"
    assert args["line_offset"] == 160


def test_parse_xml_plain_invoke_with_parameter_tags():
    # Plain-XML container with <parameter name=...> bodies (no DSML separators).
    text = (
        '<tool_calls>\n<invoke name="Grep">\n<parameter name="pattern">issue</parameter>\n<parameter name="path">/danyapi</parameter>\n</invoke>\n</tool_calls>'
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args == {"pattern": "issue", "path": "/danyapi"}


def test_parse_xml_mixed_dsml_close_plain_invoke():
    # Mixed format (tooltrace 9d29d8cc): plain <tool_calls> open, DSML close tag.
    text = (
        '<tool_calls>\n<invoke name="Read">\n'
        '<parameter name="path">/home/x/danyapi/tooltrace.py</parameter>\n'
        "</invoke>\n</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "/home/x/danyapi/tooltrace.py"}
    assert wrapper == ""


def test_parse_xml_parameter_value_json_array_stays_structured():
    # Real-world case (tooltrace 390a12f6): an XML <parameter> whose body is a JSON
    # array. Without structured coercion the client receives a string where its
    # schema expects an array ("Invalid args: /questions must be array").
    text = (
        '<tool_calls>\n<invoke name="AskUserQuestion">\n'
        '<parameter name="questions">[{"header": "H", "question": "Q?", '
        '"options": [{"label": "A"}, {"label": "B"}]}]</parameter>\n'
        "</invoke>\n</tool_calls>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert isinstance(args["questions"], list)
    assert args["questions"][0]["options"][0]["label"] == "A"


def test_parse_xml_parameter_value_json_object_stays_structured():
    text = '<tool_calls>\n<invoke name="f">\n<parameter name="cfg">{"a": 1, "b": [true, null]}</parameter>\n</invoke>\n</tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    args = json.loads(parsed[0][0].arguments)
    assert args["cfg"] == {"a": 1, "b": [True, None]}


def test_parse_xml_parameter_value_plain_string_unchanged():
    # A normal scalar string must NOT be mangled by the JSON attempt.
    text = '<tool_calls><invoke name="f"><parameter name="s">hello world</parameter></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert json.loads(parsed[0][0].arguments)["s"] == "hello world"


def test_parse_xml_parameter_value_braced_non_json_falls_back_to_string():
    # Starts with '{' but is not valid JSON — keep it as a literal string.
    text = '<tool_calls><invoke name="f"><parameter name="s">{broken</parameter></invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert json.loads(parsed[0][0].arguments)["s"] == "{broken"


def test_parse_xml_tool_calls_fenced_xml():
    text = '```\n<tool_calls>\n<invoke name="bash">\n<command>dir</command>\n</invoke>\n</tool_calls>\n```'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"


def test_parse_xml_tool_calls_invoke_inside_tool_call_not_duplicated():
    text = '<tool_call><invoke name="bash"><command>ls</command></invoke></tool_call>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["bash"]


def test_parse_bare_array_calls_bare_array():
    text = '[{"name": "bash", "arguments": {"command": "ls"}}, {"name": "get_weather", "arguments": {"city": "Moscow"}}]'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["bash", "get_weather"]
    assert json.loads(calls[1].arguments) == {"city": "Moscow"}


def test_parse_bare_array_calls_fenced_array():
    text = '```json\n[{"name": "bash", "arguments": {"command": "pwd"}}]\n```'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"


def test_parse_bare_dict_call_bare_name_arguments():
    text = '{"name": "bash", "arguments": {"command": "Get-ChildItem -Name"}}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"command": "Get-ChildItem -Name"}


def test_parse_bare_dict_call_many_tools_prose():
    text = (
        "I need to look at the code first.\n\n"
        '{"name": "bash", "arguments": {"command": "git status"}}\n\n'
        'Then I will read the file: {"name": "read", "arguments": {"filePath": "src/main.py"}}\n\n'
        "After that I will fix it."
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, wrapper = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert "git status" in json.loads(calls[0].arguments)["command"]
    assert calls[1].name == "read"
    assert json.loads(calls[1].arguments)["filePath"] == "src/main.py"
    assert "I need to look at the code first" in wrapper


def test_parse_json_in_xml_json_inside_invoke():
    text = '<tool_calls><invoke name="bash">\n{"command": "Get-ChildItem -Name"}\n</invoke></tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"command": "Get-ChildItem -Name"}


def test_parse_json_in_xml_json_inside_tool_call_block():
    text = '<tool_call>{"name": "edit", "arguments": {"filePath": "a.py", "oldString": "x", "newString": "y"}}</tool_call>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "edit"
    args = json.loads(calls[0].arguments)
    assert args["filePath"] == "a.py"


def test_parse_json_in_xml_many_xml_tools():
    text = (
        "<tool_calls>\n"
        '<invoke name="bash">\n<command>Get-ChildItem -Name</command>\n</invoke>\n'
        '<invoke name="read">\n<filePath>README.md</filePath>\n</invoke>\n'
        '<invoke name="write">\n<filePath>note.txt</filePath>\n<content>hello</content>\n</invoke>\n'
        "</tool_calls>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["bash", "read", "write"]
    assert json.loads(calls[1].arguments) == {"filePath": "README.md"}
    assert json.loads(calls[2].arguments) == {"filePath": "note.txt", "content": "hello"}


def test_parse_json_in_xml_parameter_style_xml():
    text = (
        '<tool_calls><invoke name="edit">'
        '<parameter name="filePath">a.py</parameter>'
        '<parameter name="oldString">1</parameter>'
        '<parameter name="newString">2</parameter>'
        "</invoke></tool_calls>"
    )
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "edit"
    assert json.loads(calls[0].arguments) == {"filePath": "a.py", "oldString": "1", "newString": "2"}


def test_format_tool_message():
    calls = [ToolCall.create("get_weather", {"city": "Moscow"})]
    message = format_tool_message(calls, "", "think step by step")
    assert message["role"] == "assistant"
    assert message["content"] == ""
    assert message["reasoning_content"] == "think step by step"
    assert len(message["tool_calls"]) == 1
    tool_call = message["tool_calls"][0]
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "get_weather"


def test_limit_tool_calls_keeps_first_only():
    first = ToolCall.create("Read", {"path": "a.py"})
    second = ToolCall.create("Read", {"path": "b.py"})
    assert limit_tool_calls([first, second]) == [first]
    assert limit_tool_calls([first]) == [first]
    assert limit_tool_calls([]) == []


def test_tool_call_deltas():
    calls = [ToolCall.create("get_weather", {"city": "Moscow"})]
    deltas = tool_call_deltas(calls)
    assert len(deltas) >= 2
    assert deltas[0]["role"] == "assistant"
    assert deltas[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    arguments = "".join(d["tool_calls"][0]["function"]["arguments"] for d in deltas[1:])
    assert arguments == '{"city": "Moscow"}'


def test_render_message_content_list_tool_call():
    msg = Message(
        role="assistant",
        content=[{"type": "tool_call", "id": "c1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
    )
    text = render_message(msg)
    assert "bash" in text
    assert "ls" in text


def test_render_json_mode_none():
    assert render_json_mode(None) is None


def test_render_json_mode_string():
    block = render_json_mode("json_object")
    assert block is not None
    assert block is not None
    assert "valid JSON object" in block


def test_render_json_mode_unknown_type():
    assert render_json_mode("text") is None
    assert render_json_mode({"type": "text"}) is None


def test_render_json_mode_schema():
    block = render_json_mode({"type": "json_schema", "json_schema": {"schema": {"type": "object"}}})
    assert block is not None
    assert block is not None
    assert "JSON Schema" in block
    assert '"type": "object"' in block


def test_extract_system_collects_system():
    messages = [
        Message(role="system", content="one"),
        Message(role="user", content="x"),
        Message(role="system", content="two"),
    ]
    assert extract_system(messages) == "one\ntwo"


def test_extract_system_no_system():
    assert extract_system([Message(role="user", content="x")]) == ""


def test_build_prompt_plain():
    prompt, tool_mode = build_prompt([Message(role="user", content="hello")])
    assert prompt == "hello"
    assert not tool_mode


def test_build_prompt_system_injected():
    messages = [Message(role="system", content="Be concise."), Message(role="user", content="Explain X")]
    prompt, tool_mode = build_prompt(messages)
    assert not tool_mode
    assert prompt.startswith("Be concise.")
    assert "Explain X" in prompt
    assert prompt.index("Explain X") > prompt.index("Be concise.")


def test_build_prompt_json_mode():
    messages = [Message(role="user", content="Extract JSON")]
    prompt, tool_mode = build_prompt(messages, response_format="json_object")
    assert not tool_mode
    assert "valid JSON object" in prompt
    assert "Extract JSON" in prompt


def test_build_prompt_system_and_tools_and_json():
    messages = [Message(role="system", content="sys"), Message(role="user", content="q")]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], None, False, {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}})
    assert tool_mode
    assert prompt.startswith("sys")
    assert "get_weather" in prompt
    assert "JSON Schema" in prompt
    assert "q" in prompt


def test_build_prompt_first_tool_round():
    messages = [Message(role="user", content="What is the weather?")]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], None, has_session=False)
    assert tool_mode
    assert "get_weather" in prompt
    assert "What is the weather?" in prompt


def test_build_prompt_continuation_with_session():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}}]),
        Message(role="tool", content="22C, sunny", tool_call_id="call_1"),
    ]
    prompt, tool_mode = build_prompt(messages, None, None, has_session=True)
    assert tool_mode
    assert "22C, sunny" in prompt
    assert "Continue the conversation" in prompt
    assert "What is the weather?" not in prompt


def test_build_prompt_multi_step_tool_round_sends_only_latest_exchange():
    """A multi-step tool round resends every exchange, but earlier steps are
    already in the upstream session. Only the newest call and its results are
    new material, so the tail must not repeat the earlier ones."""
    messages = [
        Message(role="user", content="Fix the bug."),
        Message(
            role="assistant",
            content="First I read it.",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"path": "a.py"}'}}],
        ),
        Message(role="tool", content="OLD-RESULT-alpha", tool_call_id="c1"),
        Message(role="assistant", tool_calls=[{"id": "c2", "type": "function", "function": {"name": "Grep", "arguments": '{"pattern": "def"}'}}]),
        Message(role="tool", content="NEW-RESULT-beta", tool_call_id="c2"),
    ]
    prompt, tool_mode = build_prompt(messages, None, None, has_session=True)
    assert tool_mode
    # The newest exchange is sent...
    assert "NEW-RESULT-beta" in prompt
    assert 'invoke name="Grep"' in prompt
    # ...and the earlier one is not repeated. The format example also names
    # Read, so the earlier call is identified by its own argument instead.
    assert "OLD-RESULT-alpha" not in prompt
    assert '"a.py"' not in prompt


def test_build_prompt_multi_step_tool_round_keeps_assistant_prose():
    """Prose that precedes the newest call in the same turn is kept, since it
    carries the model's stated intent."""
    messages = [
        Message(role="user", content="Fix the bug."),
        Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"path": "a.py"}'}}]),
        Message(role="tool", content="OLD-RESULT-alpha", tool_call_id="c1"),
        Message(role="assistant", content="Now I will grep for it."),
        Message(role="assistant", tool_calls=[{"id": "c2", "type": "function", "function": {"name": "Grep", "arguments": '{"pattern": "def"}'}}]),
        Message(role="tool", content="NEW-RESULT-beta", tool_call_id="c2"),
    ]
    prompt, _ = build_prompt(messages, None, None, has_session=True)
    assert "Now I will grep for it." in prompt
    assert "NEW-RESULT-beta" in prompt
    assert "OLD-RESULT-alpha" not in prompt


def test_build_prompt_continuation_no_session():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}}]),
        Message(role="tool", content="22C, sunny", tool_call_id="call_1"),
    ]
    prompt, tool_mode = build_prompt(messages, None, None, has_session=False)
    assert tool_mode
    assert "What is the weather?" in prompt
    assert "22C, sunny" in prompt
    assert "get_weather" in prompt


def test_build_prompt_continuation_with_session_skips_schema():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", content="It is 22C."),
        Message(role="user", content="And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], None, has_session=True)
    assert tool_mode
    assert "And in Rome?" in prompt
    assert "get_weather" not in prompt


def test_build_prompt_tool_round_with_session_skips_schema():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}}]),
        Message(role="tool", content="22C, sunny", tool_call_id="call_1"),
    ]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], None, has_session=True)
    assert tool_mode
    assert "22C, sunny" in prompt
    assert "You have access to the following functions" not in prompt


def test_build_prompt_new_chat_with_history_renders_full_context():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", content="It is 22C."),
        Message(role="user", content="And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, None, None, has_session=False)
    assert not tool_mode
    assert "What is the weather?" in prompt
    assert "It is 22C." in prompt
    assert "And in Rome?" in prompt


def test_build_prompt_new_chat_with_history_and_tools_renders_full_context():
    messages = [
        Message(role="user", content="What is the weather?"),
        Message(role="assistant", content="It is 22C."),
        Message(role="user", content="And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], None, has_session=False)
    assert tool_mode
    assert "get_weather" in prompt
    assert "What is the weather?" in prompt
    assert "It is 22C." in prompt
    assert "And in Rome?" in prompt


def test_build_prompt_new_chat_huge_history_is_truncated():
    """When a new session must be created without a live session to continue,
    the rendered history must stay under the per-request input limit instead of
    sending the whole conversation verbatim."""
    messages = [Message(role="system", content="Sys")]
    for i in range(200):
        messages.append(Message(role="user", content=f"Q{i}"))
        messages.append(Message(role="assistant", content="x" * 500))
    prompt, tool_mode = build_prompt(messages, None, None, has_session=False)
    assert not tool_mode
    # Older history is dropped with a marker.
    assert "Earlier conversation truncated" in prompt
    assert "Q0" not in prompt
    # Most recent pair is retained.
    assert "Q199" in prompt


def test_fix_unbalanced_json_stray_closers_dropped():
    assert _fix_unbalanced_json("]{") == "{}"
    assert _fix_unbalanced_json("}") == ""
    assert _fix_unbalanced_json("[}") == "[]"


def test_fix_unbalanced_json_missing_brace_before_bracket():
    assert _fix_unbalanced_json('{"a": 1]') == '{"a": 1}'


def test_fix_unbalanced_json_wrong_closer_repaired():
    assert _fix_unbalanced_json('{"a": [1}]') == '{"a": [1]}'


def test_fix_unbalanced_json_truncated_nested():
    assert _fix_unbalanced_json('[{"a": 1],') == '[{"a": 1}],'


def test_fix_unbalanced_json_unterminated_string_closed():
    assert _fix_unbalanced_json('{"a": "abc') == '{"a": "abc"}'
    assert _fix_unbalanced_json('{"a": "unterminated{[') == '{"a": "unterminated{["}'
    assert _fix_unbalanced_json('[{"a": "x') == '[{"a": "x"}]'


def test_fix_unbalanced_json_escaped_backslash_at_end_closed():
    fixed = _fix_unbalanced_json('[{"a": "x\\')
    assert fixed is not None
    assert fixed is not None
    assert fixed == '[{"a": "x\\\\"}]'
    assert json.loads(fixed) == [{"a": "x\\"}]


def test_fix_unbalanced_json_balanced_returns_none():
    assert _fix_unbalanced_json('{"a": [1, 2]}') is None
    assert _fix_unbalanced_json('"just string"') is None
    assert _fix_unbalanced_json('{"a": "\\"b\\"}", "c": [1, 2]}') is None


def test_strip_dsml_empty():
    assert _strip_dsml("") == ""
    assert _strip_dsml(None) is None


# --- DSML (DeepSeek Markup Language) tool-call parsing -------------------------

_DSML_SAMPLE = (
    "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
    '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
    '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path" string="true">/a/b.md</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
    "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
    "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
)


def test_parse_dsml_single_call():
    calls, _ = _parse_dsml(_DSML_SAMPLE, {"Read": {"path": "string"}})
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Read"
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert args == {"path": "/a/b.md"}


def test_parse_dsml_boolean_and_string_params():
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Grep">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="pattern" string="true">foo|bar</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="-i" string="false">true</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text, {"Grep": {"pattern": "string", "-i": "boolean"}})
    assert calls is not None and len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert args["pattern"] == "foo|bar"  # string="true" -> literal string
    assert args["-i"] is True  # string="false" -> coerced boolean


def test_parse_dsml_fused_close_open_chained_params():
    """A close tag carrying name= doubles as the next param's opener.

    DeepSeek chains arguments so ``</…parameter name="path" string="true">`` both
    closes ``pattern`` and opens ``path`` (tooltrace 92b6a328, 793dd6ec, 35bbad80).
    Before the fix _DSML_PARAM_RE matched nothing, so the call parsed with EMPTY
    arguments and the client rejected it as missing required on every retry.
    """
    sep = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{sep}tool_calls>\n"
        f'<{sep}invoke name="Grep">\n'
        f'<{sep}parameter name="pattern" string="true">foo</{sep}parameter name="path" string="true">/a.py</{sep}parameter>\n'
        f"</{sep}invoke>\n"
        f"</{sep}tool_calls>"
    )
    calls, _ = _parse_dsml(text, {"Grep": {"pattern": "string", "path": "string"}})
    assert calls is not None and len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert args == {"pattern": "foo", "path": "/a.py"}


def test_parse_dsml_numeric_params_with_string_true_flag():
    """DeepSeek puts string=\"true\" on ALL params including numeric ones.

    When the tool schema says a param is integer/number, coerce it even if
    DSML marked it as string.  Regression: request_id 7c162769 — Read's
    line_offset/n_lines came through as strings and Kimi rejected them.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path" string="true">/a/b.rs</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="line_offset" string="true">1</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="n_lines" string="true">400</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    schemas = {"Read": {"path": "string", "line_offset": "integer", "n_lines": "integer"}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert isinstance(args["path"], str)  # string stays string
    assert isinstance(args["line_offset"], int)  # numeric coerced despite string="true"
    assert isinstance(args["n_lines"], int)
    assert args["line_offset"] == 1
    assert args["n_lines"] == 400


def test_parse_dsml_json_array_param():
    """string=\"false\" with a JSON array value must produce a list, not a string.

    Regression: 762a4d07 — DeepSeek sent todos as DSML parameter with
    string="false" but the parser returned it as a raw JSON string.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="TodoList">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="todos" string="false">'
        '[{"title": "task", "status": "in_progress"}]'
        "</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text)
    assert calls is not None and len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert isinstance(args["todos"], list)
    assert args["todos"] == [{"title": "task", "status": "in_progress"}]


def test_dsml_todolist_items_alias_and_content_title():
    """Regression: request 86a22871 — DeepSeek names the todo array ``items`` (not
    ``todos``) and each item's text field ``content`` (not ``title``), with status
    values outside the client enum. The full normalization pipeline must rename all
    three so the call survives the client's strict argument validation."""
    fw = "\uff5c\uff5c"
    text = (
        f"<{fw}DSML{fw}_calls>\n"
        f'<{fw}DSML{fw}invoke name="TodoList">\n'
        f'<{fw}DSML{fw}parameter name="items" string="false">'
        '[{"content": "read spec", "status": "completed"}, '
        '{"content": "implement fix", "status": "pending"}]'
        f"</{fw}DSML{fw}parameter>\n"
        f"</{fw}DSML{fw}invoke>\n"
        f"</{fw}DSML{fw}_calls>"
    )
    result = parse_tool_calls(text, {"TodoList": {"todos": "array"}})
    assert result is not None
    calls, _wrapper = result
    args = json.loads(calls[0].arguments)
    # Top-level param renamed items -> todos.
    assert list(args.keys()) == ["todos"]
    assert len(args["todos"]) == 2
    # Per-item: content -> title; completed -> done; pending untouched.
    assert args["todos"][0] == {"title": "read spec", "status": "done"}
    assert args["todos"][1] == {"title": "implement fix", "status": "pending"}


def test_dsml_nested_arguments_param_is_unwrapped():
    """Regression: requests 0b27b9fc / e0925968 — DeepSeek collapsed a Bash call's
    arguments into a single DSML parameter literally named ``arguments`` (sometimes
    double-encoded as a JSON string) instead of one parameter per argument. The
    parser stored it verbatim, so the client received ``{"arguments": {...}}`` — a
    Bash call with no ``command`` — and rejected it with "must have required
    property 'command'". The normalization pipeline must flatten it.
    """
    fw = "\uff5c\uff5c"
    text = (
        f"<{fw}DSML{fw}_calls>\n"
        f'<{fw}DSML{fw}invoke name="Bash">\n'
        f'<{fw}DSML{fw}parameter name="arguments" string="false">'
        '{"command": "ls -la", "timeout": 30}'
        f"</{fw}DSML{fw}parameter>\n"
        f"</{fw}DSML{fw}invoke>\n"
        f"</{fw}DSML{fw}_calls>"
    )
    result = parse_tool_calls(text, {"Bash": {"command": "string", "timeout": "integer"}})
    assert result is not None
    calls, _wrapper = result
    args = json.loads(calls[0].arguments)
    assert args == {"command": "ls -la", "timeout": 30}


def test_dsml_nested_arguments_string_double_encoded_is_unwrapped():
    """The nested value may be a double-encoded JSON *string* whose inner quotes are
    mangled (unescaped shell quoting), which strict json.loads rejects as "Extra
    data". The lenient loader must still recover the real parameters."""
    fw = "\uff5c\uff5c"
    inner = '{"command": "ssh host \'echo hi\' 2>&1"}}'
    text = (
        f"<{fw}DSML{fw}_calls>\n"
        f'<{fw}DSML{fw}invoke name="Bash">\n'
        f'<{fw}DSML{fw}parameter name="arguments" string="true">' + inner + f"</{fw}DSML{fw}parameter>\n"
        f"</{fw}DSML{fw}invoke>\n"
        f"</{fw}DSML{fw}_calls>"
    )
    result = parse_tool_calls(text, {"Bash": {"command": "string"}})
    assert result is not None
    calls, _wrapper = result
    args = json.loads(calls[0].arguments)
    assert "command" in args
    assert args["command"].startswith("ssh host")


def test_parse_dsml_json_object_param():
    """string=\"false\" with a JSON object value must produce a dict, not a string."""
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="SomeTool">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="config" string="false">'
        '{"key": "value", "count": 42}'
        "</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text)
    assert calls is not None and len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert isinstance(args["config"], dict)
    assert args["config"] == {"key": "value", "count": 42}


def test_parse_dsml_multiple_invokes():
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path" string="true">/x</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Glob">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="pattern" string="true">**/*.py</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text)
    assert calls is not None and len(calls) == 2
    assert [c.name for c in calls] == ["Read", "Glob"]


def test_parse_dsml_no_invoke_returns_none():
    text = "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls></\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    calls, wrapper = _parse_dsml(text)
    assert calls is None
    assert wrapper == ""


def test_parse_dsml_malformed_string_value_self_closing():
    """DeepSeek outputs string="/path"/> instead of string="true">/path.

    Regression: tooltrace ndjson — model put the actual value in the string=
    attribute and used a self-closing <parameter/> tag with no body content.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path"'
        ' string="/home/user/config.toml"/>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Read"
    import json as _json

    assert _json.loads(calls[0].arguments) == {"path": "/home/user/config.toml"}


def test_parse_dsml_malformed_string_value_missing_quote():
    """DeepSeek outputs string="value"</parameter> with no closing quote before the tag close.

    Regression: tooltrace ndjson — Grep parameter had string="(?i)provider"
    followed directly by `</\uff5c\uff5cDSML\uff5c\uff5cparameter>` with no " between value and <.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Grep">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="pattern"'
        ' string="(?i)provider"</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path"'
        ' string="/home/user/project"</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    calls, _ = _parse_dsml(text)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Grep"
    import json as _json

    assert _json.loads(calls[0].arguments) == {
        "pattern": "(?i)provider",
        "path": "/home/user/project",
    }


def test_parse_tool_calls_dispatches_to_dsml():
    """End-to-end: parse_tool_calls routes DSML-tagged content to the DSML parser."""
    text = "Изучаю проект.\n\n" + _DSML_SAMPLE
    parsed = parse_tool_calls(text, {"Read": {"path": "string"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    import json as _json

    assert _json.loads(calls[0].arguments) == {"path": "/a/b.md"}


def test_parse_dsml_bare_close_tag_numeric_params():
    """DeepSeek emits </||DSML||> (bare, no 'parameter' word) as the close tag.

    The old regex required "parameter" in the close, so bare closes slipped past
    _DSML_PARAM_RE and _dsml_param_value fell through to _coerce_dsml which maps
    "" → False.  This reproduces tooltrace request_ids 6dd4d58e, 15df48bf, etc.
    where Bash.timeout / Read.line_offset / Read.n_lines arrived as ``false``.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="path">/home/x.py</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="line_offset">88</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="n_lines" string="false">70</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    parsed = parse_tool_calls(text, {"Read": {"path": "string", "line_offset": "integer", "n_lines": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert args["path"] == "/home/x.py"
    assert args["line_offset"] == 88, f"expected int 88, got {args['line_offset']!r}"
    assert args["n_lines"] == 70, f"expected int 70, got {args['n_lines']!r}"


def test_parse_dsml_bare_close_tag_string_params():
    """string='true' params with bare DSML closes must keep their literal value.

    Reproduces tooltrace request_id 6dd4d58e where Bash.description and
    Bash.command used </||DSML||> (bare) instead of </||DSML|| parameter>.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Bash">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="command" string="true">echo hello</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="description" string="true">Say hi</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="timeout" string="false">120</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    parsed = parse_tool_calls(text, {"Bash": {"command": "string", "description": "string", "timeout": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    import json as _json

    args = _json.loads(calls[0].arguments)
    assert args["command"] == "echo hello"
    assert args["description"] == "Say hi"
    assert args["timeout"] == 120, f"expected int 120, got {args['timeout']!r}"


def test_tool_call_create_variants():
    assert ToolCall.create("f", None).arguments == "{}"
    assert ToolCall.create("f", "raw").arguments == "raw"
    assert ToolCall.create("f", {"a": 1}).arguments == '{"a": 1}'
    assert ToolCall.create("f", [1, 2]).arguments == "[1, 2]"
    assert ToolCall.create("f", 5).arguments == "5"


def test_tool_function_variants():
    assert _tool_function(None) is None
    assert _tool_function("str") is None
    assert _tool_function({"function": 42}) is None
    assert _tool_function({"name": "x"}) == {"name": "x"}
    assert _tool_function({"function": {"name": "y"}}) == {"name": "y"}


def test_render_tool_schema_empty_names():
    assert render_tool_schema([{"function": {"name": ""}}]) is None
    assert render_tool_schema([{"function": {"parameters": {}}}]) is None


def test_render_tool_schema_string_params():
    tool = {"function": {"name": "f", "parameters": '{"type":"object"}'}}
    schema = render_tool_schema([tool])
    assert schema is not None
    assert schema is not None
    assert '{"type":"object"}' in schema


def test_content_text_variants():
    assert _content_text(42) == ""
    assert _content_text([42, {"type": "image_url"}]) == ""


def test_content_fingerprint_variants():
    assert _content_fingerprint(42) == ""
    # An image URI is fingerprinted by digest, not copied verbatim.
    fp = _content_fingerprint([{"type": "image_url", "image_url": "data:x"}])
    assert fp.startswith("image:")
    assert "data:x" not in fp


def test_render_tool_call_mention_variants():
    assert _render_tool_call_mention(None) == ""
    # A non-object argument list has no declared parameter to name, so no
    # parameter tag is rendered — only the invoke wrapper.
    assert _render_tool_call_mention({"name": "f", "arguments": [1]}) == _dsml_tag('invoke name="f"') + "\n" + _dsml_tag("invoke", closing=True)
    assert _render_tool_call_mention({"function": {"name": "g", "arguments": "{}"}}) == _dsml_tag('invoke name="g"') + "\n" + _dsml_tag("invoke", closing=True)
    # Empty name → empty string (no mention rendered)
    assert _render_tool_call_mention({"name": "", "arguments": "{}"}) == ""


def test_render_tool_call_mention_is_dsml():
    """A past call is shown in the same format the instruction asks for, and it
    parses back into the same arguments."""
    mention = _render_tool_call_mention({"function": {"name": "Read", "arguments": '{"path": "/a.py", "line_offset": 10}'}})
    assert _dsml_tag('invoke name="Read"') in mention
    assert "tool_calls" not in mention
    calls, _wrapper = _parse_dsml(mention)
    assert calls is not None
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "/a.py", "line_offset": 10}


def test_render_tool_call_mention_keeps_non_string_arguments_typed():
    mention = _render_tool_call_mention({"name": "f", "arguments": {"n": 10, "on": True}})
    calls, _wrapper = _parse_dsml(mention)
    assert calls is not None
    assert json.loads(calls[0].arguments) == {"n": 10, "on": True}


def test_render_tool_call_mention_handles_non_object_arguments():
    """A raw string that is not a JSON object, an empty one, and a type with no
    parameter form each fall back without inventing arguments."""
    # Not JSON at all: the text is passed through as a single arguments parameter.
    assert "not json" in _render_tool_call_mention({"name": "f", "arguments": "not json"})
    # A JSON array is not a parameter map, so it goes through the same fallback.
    assert "[1, 2]" in _render_tool_call_mention({"name": "f", "arguments": "[1, 2]"})
    # An empty argument string adds no parameter tag at all.
    bare = _render_tool_call_mention({"name": "f", "arguments": ""})
    assert _dsml_param("arguments", "x") not in bare
    # An argument type with no parameter form yields only the invoke wrapper.
    assert _render_tool_call_mention({"name": "f", "arguments": 42}) == _dsml_tag('invoke name="f"') + "\n" + _dsml_tag("invoke", closing=True)


def test_render_message_roles():
    msg = Message(role="function", name="calc", content="42")
    assert render_message(msg) == "Function calc returned: 42"
    msg = Message(role="tool", content="ok")
    assert render_message(msg) == "Tool result: ok"
    msg = Message(role="other", content="x")
    assert render_message(msg) == "x"


def test_extract_last_user_skips_trailing_user_role_reminder():
    """A trailing <system-reminder> must not hide the user's actual question.

    Real failure, verified against the wire log: Kimi appends these reminders as
    a separate role="user" message AFTER the user's turn, and the content is a
    list part. extract_last_user walked backwards, matched the reminder (it is a
    user role), and returned it as "the user's message" — so the prompt sent
    upstream carried no question and the model answered as if the user had said
    nothing. The skip must therefore be role-agnostic.
    """
    reminder = [{"type": "text", "text": "<system-reminder>\nThe TodoList tool has not been updated recently.\n</system-reminder>"}]
    messages = [
        Message(role="user", content="tool_schema_map это что?"),
        Message(role="assistant", content="Коротко."),
        Message(role="user", content="как лучше?"),
        Message(role="user", content=reminder),
    ]
    assert extract_last_user(messages) == "как лучше?"


def test_extract_last_user_skips_system_role_reminder():
    """The same skip applies when a reminder arrives as role="system"."""
    reminder = "<system-reminder>\nThe TodoList tool has not been updated recently.\n</system-reminder>"
    messages = [
        Message(role="user", content="как лучше?"),
        Message(role="system", content=reminder),
    ]
    assert extract_last_user(messages) == "как лучше?"


def test_extract_last_user_uses_real_system_when_no_user():
    """A genuine system instruction is still a valid fallback."""
    messages = [Message(role="system", content="Be concise.")]
    assert extract_last_user(messages) == "Be concise."


def test_extract_last_user_real_question_kept_before_reminder():
    """A real user turn that merely precedes a reminder is still returned."""
    messages = [
        Message(role="user", content="вопрос"),
        Message(role="user", content="<system-reminder>\nnoise\n</system-reminder>"),
    ]
    assert extract_last_user(messages) == "вопрос"


def test_tail_after_last_user_ignores_trailing_reminder():
    """The tail must start after the user's turn, not after the reminder."""
    reminder = "<system-reminder>\nreminder text\n</system-reminder>"
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="second"),
        Message(role="user", content=reminder),
    ]
    tail = _tail_after_last_user(messages)
    # The reminder is after the last user turn, so it is part of the tail.
    assert len(tail) == 1
    assert "reminder text" in tail[0].content


def test_extract_last_user_image_only():
    msg = Message(role="user", content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}])
    with pytest.raises(ValueError):
        extract_last_user([msg])


def test_has_history_multiple_users():
    assert _has_history([Message("user", "a"), Message("user", "b")])
    assert not _has_history([Message("user", "a")])


def test_tail_after_last_user_no_user():
    msgs = [Message("assistant", "a"), Message("assistant", "b")]
    assert _tail_after_last_user(msgs) == msgs


def test_render_json_mode_unknown():
    assert render_json_mode({"type": "text"}) is None
    assert render_json_mode(42) is None
    assert render_json_mode("text") is None


def test_normalize_single_quotes_escapes():
    assert _normalize_single_quotes(r"{'a': 'say \"hi\"'}") == '{"a": "say \\"hi\\""}'
    assert _normalize_single_quotes(r"{'a': 'it\'s'}") == '{"a": "it\'s"}'
    assert _normalize_single_quotes(r"{'a': 'x\\y'}") == '{"a": "x\\\\y"}'


def test_coerce_scalar():
    assert _coerce_scalar("5", "integer") == 5
    assert _coerce_scalar("5.5", "number") == 5.5
    assert _coerce_scalar("abc", "integer") == "abc"
    assert _coerce_scalar("true", "boolean") is True
    assert _coerce_scalar("false", "boolean") is False
    assert _coerce_scalar("maybe", "boolean") == "maybe"
    assert _coerce_scalar("x", "null") is None
    assert _coerce_scalar(5, "integer") == 5
    assert _coerce_scalar("str", "string") == "str"


def test_coerce_scalar_string_type_keeps_json_looking_text():
    """When schema says 'string', a value like {"a":1} stays as text (e.g. Write content)."""
    assert _coerce_scalar('{"schema_version": 1}', "string") == '{"schema_version": 1}'


def test_coerce_scalar_unknown_type_still_parses_json():
    """When type is unknown, a value starting with { or [ is still parsed as JSON."""
    assert _coerce_scalar('[{"header": "x"}]', None) == [{"header": "x"}]
    assert _coerce_scalar('{"a": 1}', None) == {"a": 1}


def test_coerce_scalar_explicit_array_still_parses():
    """When schema says 'array', JSON parsing applies regardless of type heuristic."""
    assert _coerce_scalar("[1, 2]", "array") == [1, 2]


def test_is_jsonish_arguments():
    assert _is_jsonish_arguments({"a": 1})
    assert _is_jsonish_arguments('{"a": 1}')
    assert not _is_jsonish_arguments("nope")
    assert not _is_jsonish_arguments(42)


def test_extract_one_call_variants():
    call = _extract_one_call({"function": {"name": "f", "arguments": {}}})
    assert call.name == "f"
    call = _extract_one_call({"name": "g", "arguments": {}})
    assert call.name == "g"
    assert _extract_one_call({"name": ""}) is None
    assert _extract_one_call("str") is None


def test_extract_one_call_flat_arguments():
    # DeepSeek v4-pro: flat keys next to `name` instead of nested `arguments`.
    call = _extract_one_call({"name": "Bash", "command": "rm -f x", "description": "delete"})
    assert call is not None
    assert call.name == "Bash"
    assert json.loads(call.arguments) == {"command": "rm -f x", "description": "delete"}

    # Nested `function` form with flat keys.
    call = _extract_one_call({"function": {"name": "Read", "path": "/a.md"}})
    assert call is not None
    assert json.loads(call.arguments) == {"path": "/a.md"}

    # Metadata keys are not collected as arguments.
    call = _extract_one_call({"name": "Bash", "id": "c1", "type": "function", "command": "ls"})
    assert json.loads(call.arguments) == {"command": "ls"}

    # An explicit `arguments` wins over flat keys.
    call = _extract_one_call({"name": "Bash", "arguments": {"command": "pwd"}, "command": "ls"})
    assert json.loads(call.arguments) == {"command": "pwd"}


def test_extract_one_call_parameters_synonym():
    # DeepSeek emits `parameters` instead of `arguments` in the single-object form
    # (tooltrace 6e02a45f: {"tool": "nmap", "parameters": {...}}).
    call = _extract_one_call({"tool": "nmap", "parameters": {"target": "1.2.3.4"}})
    assert call is not None
    assert call.name == "nmap"
    assert json.loads(call.arguments) == {"target": "1.2.3.4"}

    # `function` wrapper form with `parameters`.
    call = _extract_one_call({"function": {"name": "nmap", "parameters": {"target": "1.2.3.4"}}})
    assert call is not None
    assert json.loads(call.arguments) == {"target": "1.2.3.4"}

    # An explicit `arguments` still wins over `parameters`.
    call = _extract_one_call({"tool": "nmap", "arguments": {"target": "a"}, "parameters": {"target": "b"}})
    assert json.loads(call.arguments) == {"target": "a"}

    # `parameters` is not leaked as a flat argument when arguments are also flat.
    call = _extract_one_call({"name": "nmap", "target": "1.2.3.4"})
    assert json.loads(call.arguments) == {"target": "1.2.3.4"}


def test_extract_calls_bare_dict():
    calls = _extract_calls({"name": "f", "arguments": {"x": 1}})
    assert calls is not None
    assert calls is not None
    assert calls[0].name == "f"


def test_extract_calls_bare_dict_parameters_synonym():
    calls = _extract_calls({"tool": "nmap", "parameters": {"target": "1.2.3.4"}})
    assert calls is not None
    assert calls[0].name == "nmap"
    assert json.loads(calls[0].arguments) == {"target": "1.2.3.4"}


def test_tool_schema_map():
    tools = [
        {
            "function": {
                "name": "a",
                "parameters": {"properties": {"x": {"type": ["null", "integer"]}, "y": {"type": "string"}, "z": {}}},
            }
        },
        {"function": {"name": "b"}},
    ]
    result = tool_schema_map(tools)
    assert "b" not in result
    assert result["a"] == {"x": "integer", "y": "string", "__required__": set()}


def test_xml_invoke_inline_json():
    from danyapi.tools import _xml_invoke_arguments

    args = _xml_invoke_arguments('{"cmd": "ls"}')
    assert args == {"cmd": "ls"}
    assert _xml_invoke_arguments("  ") is None


def test_xml_tag_attrs():
    from danyapi.tools import _xml_tag_attrs

    attrs = _xml_tag_attrs('key="value" num="5" flag="true"', {"num": "integer", "flag": "boolean"})
    assert attrs == {"key": "value", "num": 5, "flag": True}
    attrs = _xml_tag_attrs('key="value"')
    assert attrs == {"key": "value"}


def test_parse_xml_self_closing():
    text = '<tool_calls><bash command="ls"/></tool_calls>'
    parsed = _parse_xml_tool_calls(text, {"bash": {"command": "string"}})
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"command": "ls"}


def test_parse_bare_array_non_array():
    assert _parse_bare_array_calls("hello") is None
    assert _parse_bare_array_calls("not [json") is None


def test_loads_lenient_paths():
    assert _loads_lenient('{"a": 1,}') == {"a": 1}
    assert _loads_lenient("{'a': 1}") == {"a": 1}
    assert _loads_lenient('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        _loads_lenient("not json at all")


def test_build_prompt_history_no_session():
    messages = [Message("user", "q1"), Message("assistant", "a1"), Message("user", "q2")]
    prompt, tool_mode = build_prompt(messages, has_session=False)
    assert "q1" in prompt
    assert "q2" in prompt
    assert not tool_mode


def test_build_prompt_session_json():
    messages = [Message("user", "hello")]
    prompt, _ = build_prompt(messages, has_session=True, response_format="json_object")
    assert "valid JSON object" in prompt
    assert "hello" in prompt


def test_tool_call_deltas_with_text():
    calls = [ToolCall.create("f", {"x": 1})]
    deltas = tool_call_deltas(calls, "pre")
    assert deltas[0] == {"role": "assistant", "content": "pre"}


def test_tool_function_non_string_name():
    assert _tool_function({"name": 5}) is None


def test_content_text_string_items():
    assert _content_text(["hello"]) == "hello"
    assert _content_text([{"text": "x"}]) == "x"


def test_content_fingerprint_string_items():
    assert _content_fingerprint(["a"]) == "a"


def test_extract_last_user_string_items():
    msg = Message(role="user", content=["a", {"type": "text", "text": "b"}])
    assert extract_last_user([msg]) == "ab"


def test_has_history_tool_role():
    assert _has_history([Message("user", "a"), Message("tool", "42", tool_call_id="c")])


def test_is_tool_round_tail_variants():
    assert _is_tool_round_tail([Message("tool", "x")])
    assert _is_tool_round_tail([Message("assistant", tool_calls=[{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}])])
    assert _is_tool_round_tail([Message("assistant", content=[{"type": "tool_call", "id": "c", "function": {"name": "f", "arguments": "{}"}}])])
    assert not _is_tool_round_tail([Message("user", "hi")])


def test_build_prompt_history_with_json_mode():
    messages = [Message("user", "q1"), Message("assistant", "a1"), Message("user", "q2")]
    prompt, _ = build_prompt(messages, has_session=False, response_format="json_object")
    assert "valid JSON object" in prompt
    assert prompt.index("valid JSON") < prompt.index("q1")


def test_build_prompt_empty_history_fallback():
    messages = [Message("user", ""), Message("user", "")]
    _prompt, tool_mode = build_prompt(messages, has_session=False)
    assert tool_mode is False


def test_normalize_single_quotes_escaped_other():
    assert _normalize_single_quotes(r"{'a': '\t'}") == '{"a": "\\t"}'


def test_extract_wrapped_calls_tool_calls_dict():
    from danyapi.tools import _extract_wrapped_calls

    calls = _extract_wrapped_calls({"tool_calls": {"name": "f", "arguments": {"x": 1}}})
    assert calls is not None
    assert calls[0].name == "f"


def test_parse_two_objects_second_is_tool_call():
    text = '{"a": 1} {"tool_calls": [{"name": "f", "arguments": {"x": 1}}]}'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert calls[0].name == "f"


def test_xml_invoke_empty_body_with_optional_params_parses():
    # Empty-body invoke with a tool that has optional-only params (no __required__)
    # is a valid zero-arg call. request_id 30ff21ed: ExitPlanMode emits empty body.
    text = '<tool_calls><invoke name="bash">   </invoke></tool_calls>'
    parsed = _parse_xml_tool_calls(text, {"bash": {"cmd": "string"}})
    assert parsed is not None
    calls, _wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {}


def test_dsml_wrapper_empty_invoke_parses():
    # Exact pattern from request_id 30ff21ed: DSML _calls wrapper around a plain
    # <invoke> with an empty body. The _strip_dsml path removes the wrapper, then
    # _parse_xml_tool_calls handles the invoke.
    text = '<\uff5c\uff5cDSML\uff5c\uff5c_calls>\n<invoke name="ExitPlanMode">\n\n</invoke>\n</\uff5c\uff5cDSML\uff5c\uff5c_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "ExitPlanMode"
    assert json.loads(calls[0].arguments) == {}


def test_tool_calls_empty_invoke_with_optional_schema():
    # <tool_calls><invoke name="..."></invoke></tool_calls> where the tool has
    # optional params in its schema — must parse as an empty-arg call.
    schemas = {"ExitPlanMode": {"content": "string", "__required__": set()}}
    text = '<tool_calls>\n<invoke name="ExitPlanMode">\n\n</invoke>\n</tool_calls>'
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "ExitPlanMode"
    assert json.loads(calls[0].arguments) == {}


def test_xml_invoke_no_arg_tool_parses_as_empty_call():
    # A tool with no declared parameters (e.g. EnterPlanMode) emits
    # <invoke name="..."></invoke> with an empty body. That is a valid zero-arg
    # call, not a mangled one — it must parse with empty arguments, not be dropped.
    text = '<tool_calls>\n<invoke name="EnterPlanMode">\n\n</invoke>\n</tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "EnterPlanMode"
    assert json.loads(calls[0].arguments) == {}
    assert wrapper == ""


def test_xml_open_pattern_overlap_skipped():
    text = '<invoke name="other"><bash>x</bash></invoke>'
    parsed = _parse_xml_tool_calls(text, {"other": {"a": "string"}, "bash": {"cmd": "string"}})
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["other"]


def test_xml_open_pattern_empty_merged_skipped():
    text = "<bash></bash>"
    parsed = _parse_xml_tool_calls(text, {"bash": {"cmd": "string"}})
    assert parsed == (None, "")


def test_xml_open_pattern_with_schema():
    text = "<bash><cmd>ls</cmd></bash>"
    parsed = _parse_xml_tool_calls(text, {"bash": {"cmd": "string"}})
    assert parsed is not None
    calls, _wrapper = parsed
    assert calls is not None
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"cmd": "ls"}


def test_xml_selfclose_overlap_skipped():
    text = '<invoke name="other"><bash/></invoke>'
    parsed = _parse_xml_tool_calls(text, {"other": {"a": "string"}, "bash": {"cmd": "string"}})
    assert parsed is not None
    calls, _ = parsed
    assert calls is not None
    assert [c.name for c in calls] == ["other"]


def test_tool_schema_map_skips_bad_tools():
    tools = [
        {"function": {}},
        {"function": {"name": ""}},
        {"function": {"name": "a", "parameters": "not dict"}},
        {"function": {"name": "b", "parameters": {"properties": "not dict"}}},
        {"function": {"name": "c", "parameters": {"properties": {"x": "not dict"}}}},
    ]
    assert tool_schema_map(tools) == {}


def test_xml_invoke_broken_json():
    # A body that looks like JSON but is malformed falls through to the legacy
    # plain-text fallback ({"content": ...}) rather than being dropped.
    from danyapi.tools.parse import _xml_invoke_arguments, _xml_invoke_legacy_children

    args = _xml_invoke_arguments("{broken")
    if args is None:
        args = _xml_invoke_legacy_children("{broken")
    assert args == {"content": "{broken"}


def test_parse_xml_empty_merged_skipped():
    text = "<bash />"
    parsed = _parse_xml_tool_calls(text, {"bash": {"cmd": "string"}})
    assert parsed == (None, "")


def test_parse_xml_selfclose_no_args():
    text = "<bash />"
    parsed = _parse_xml_tool_calls(text, {"bash": {}})
    assert parsed == (None, "")


def test_balanced_json_no_brace():
    from danyapi.tools import _balanced_json

    assert _balanced_json("no braces here") is None


def test_extract_json_object_balanced_but_invalid():
    from danyapi.tools import _extract_json_object

    assert _extract_json_object('pre {"a": } post') is None


def test_parse_bare_array_broken_json():
    assert _parse_bare_array_calls("[{broken") is None


def test_iter_json_objects_skips_bad_candidates():
    from danyapi.tools import _iter_json_objects

    text = '{"ok": 1} not-json {"bad": broken}'
    objs = [obj for obj, _, _ in _iter_json_objects(text)]
    assert objs == [{"ok": 1}]


def test_tool_schema_map_anyof_integer():
    """Kimi declares optional int params as anyOf [{type:integer},{type:null}]."""
    tools = [
        {
            "function": {
                "name": "Read",
                "parameters": {
                    "properties": {
                        "path": {"type": "string"},
                        "line_offset": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "n_lines": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
                    }
                },
            }
        },
    ]
    result = tool_schema_map(tools)
    assert result["Read"] == {"path": "string", "line_offset": "integer", "n_lines": "integer", "__required__": set()}


def test_xml_invoke_anyof_integer_coerced():
    """<parameter name="line_offset">48</parameter> must yield int 48, not str '48'."""
    tools = [
        {
            "function": {
                "name": "Read",
                "parameters": {
                    "properties": {
                        "path": {"type": "string"},
                        "line_offset": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    }
                },
            }
        },
    ]
    sm = tool_schema_map(tools)
    text = '<invoke name="Read">\n<parameter name="path">/tmp/x.rs</parameter>\n<parameter name="line_offset">48</parameter>\n</invoke>'
    calls, _ = _parse_xml_tool_calls(text, sm)
    assert calls is not None and len(calls) == 1
    args = json.loads(calls[0].arguments) if isinstance(calls[0].arguments, str) else calls[0].arguments
    assert args["line_offset"] == 48
    assert isinstance(args["line_offset"], int)


# ---------------------------------------------------------------------------
# build_compact_prompt tests
# ---------------------------------------------------------------------------


def test_compact_basic_conversation():
    """System + a few user/assistant pairs."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="system", content="You are Kimi Code CLI."),
        Message(role="user", content="What is 2+2?"),
        Message(role="assistant", content="4"),
        Message(role="user", content="And 3*7?"),
        Message(role="assistant", content="21"),
    ]
    system, body = build_compact_prompt(msgs)
    assert system == "You are Kimi Code CLI."
    assert "User: What is 2+2?" in body
    assert "Assistant: 4" in body
    assert "User: And 3*7?" in body
    assert "Assistant: 21" in body


def test_compact_truncates_old_pairs():
    """Only last N pairs are kept, older ones get a truncation note."""
    from danyapi.tools import build_compact_prompt

    msgs = [Message(role="system", content="Sys")]
    for i in range(15):
        msgs.append(Message(role="user", content=f"Q{i}"))
        msgs.append(Message(role="assistant", content=f"A{i}"))
    system, body = build_compact_prompt(msgs, max_pairs=5)
    assert system == "Sys"
    # Should have truncation note (10 pairs omitted)
    assert "truncated" in body.lower() or "omitted" in body.lower()
    # Last 5 pairs should be present
    assert "User: Q14" in body
    assert "Assistant: A14" in body
    # Earlier pairs should NOT be present
    assert "Q0" not in body


def test_compact_includes_tool_results():
    """Last M tool results are appended raw."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="system", content="Sys"),
        Message(role="user", content="Read the file"),
        Message(
            role="assistant",
            content="",
            tool_calls=[{"function": {"name": "read_file", "arguments": '{"path":"/a.txt"}'}}],
        ),
        Message(role="tool", content="file contents here", tool_call_id="call_1"),
        Message(role="user", content="What did it say?"),
    ]
    _, body = build_compact_prompt(msgs)
    assert "Recent tool results:" in body
    assert "file contents here" in body


def test_compact_no_system():
    """Works without a system message."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi there!"),
    ]
    system, body = build_compact_prompt(msgs)
    assert system == ""
    assert "User: Hello" in body
    assert "Assistant: Hi there!" in body


def test_compact_keeps_pending_user_and_tool_round():
    """A tool-call-only assistant forms no pair, but the current turn is kept.

    The user message before such an assistant is the request being answered now,
    so it must survive compaction (as the current turn), together with the tool
    call and its result.
    """
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="system", content="Sys"),
        Message(role="user", content="Do something"),
        Message(
            role="assistant",
            content="",  # no text, only tool_calls
            tool_calls=[{"function": {"name": "run_cmd", "arguments": "{}"}}],
        ),
        Message(role="tool", content="done", tool_call_id="call_1"),
    ]
    _, body = build_compact_prompt(msgs)
    # The pending question is the current turn, and the tool round is its context.
    assert "User: Do something" in body
    assert "run_cmd" in body
    assert "done" in body


def test_compact_empty_messages():
    """Empty list produces empty results."""
    from danyapi.tools import build_compact_prompt

    system, body = build_compact_prompt([])
    assert system == ""
    assert body == ""


def test_compact_custom_max_pairs():
    """max_pairs=1 keeps only the last pair."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="one"),
        Message(role="user", content="second"),
        Message(role="assistant", content="two"),
    ]
    _, body = build_compact_prompt(msgs, max_pairs=1)
    assert "User: second" in body
    assert "Assistant: two" in body
    assert "first" not in body


def test_compact_keeps_current_pending_question():
    """The unanswered last user message must survive compaction.

    Regression: the compact view used to keep only completed user/assistant
    pairs, so the question the request was actually answering was dropped and
    the fresh session answered an older one.
    """
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="THE CURRENT QUESTION"),
    ]
    _, body = build_compact_prompt(msgs)
    assert "old question" in body
    assert "THE CURRENT QUESTION" in body


def test_compact_includes_summary_of_dropped_history():
    """A provided handoff summary is rendered above the kept history."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="current"),
    ]
    _, body = build_compact_prompt(msgs, summary="We chose Postgres and must keep the API stable.")
    assert "Summary of earlier conversation" in body
    assert "We chose Postgres" in body


def test_compact_renders_tool_schema_and_json_mode():
    """Tool schema and JSON mode survive the move to the fresh chat."""
    from danyapi.tools import build_compact_prompt

    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
    msgs = [Message(role="user", content="hi")]
    system, _ = build_compact_prompt(msgs, tools=[tool], response_format="json_object")
    assert "get_weather" in system
    assert "JSON" in system


def test_compact_respects_max_chars_budget():
    """The assembled body never exceeds max_chars even with huge history."""
    from danyapi.tools import build_compact_prompt

    msgs = [Message(role="system", content="sys")]
    for i in range(10):
        msgs.append(Message(role="user", content=f"question {i}"))
        msgs.append(Message(role="assistant", content="A" * 5000))
    msgs.append(Message(role="tool", content="T" * 40000, tool_call_id="c1"))
    msgs.append(Message(role="user", content="CURRENT"))
    _, body = build_compact_prompt(msgs, max_chars=20000)
    assert len(body) <= 20000
    assert "CURRENT" in body


def test_compact_truncates_tool_results_to_fit_budget():
    """When even the kept pairs fit but tool results do not, tool results shrink."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content="a"),
        Message(role="tool", content="T" * 30000, tool_call_id="c1"),
        Message(role="user", content="CURRENT"),
    ]
    _, body = build_compact_prompt(msgs, max_tool_results=3, max_chars=2000)
    assert len(body) <= 2000
    assert "CURRENT" in body


def test_compact_drops_tool_results_entirely_when_too_small():
    """A tiny budget drops the tool-results section rather than leaving a stub."""
    from danyapi.tools import build_compact_prompt

    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content="a"),
        Message(role="tool", content="T" * 30000, tool_call_id="c1"),
        Message(role="user", content="CURRENT"),
    ]
    _, body = build_compact_prompt(msgs, max_tool_results=3, max_chars=40)
    assert len(body) <= 40


def test_truncate_head_edge_cases():
    """`_truncate_head` returns empty for a non-positive limit and keeps the tail."""
    from danyapi.tools.prompt import _truncate_head

    assert _truncate_head("abc", 0) == ""
    assert _truncate_head("abc", -5) == ""
    assert _truncate_head("abc", 10) == "abc"
    out = _truncate_head("x" * 100, 20)
    assert len(out) <= 20
    assert out.endswith("x")


def test_dropped_pairs_source_reports_older_pairs():
    """The summarizer source is the prefix a compact prompt would drop."""
    from danyapi.tools import dropped_pairs_source

    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="one"),
        Message(role="user", content="second"),
        Message(role="assistant", content="two"),
        Message(role="user", content="third"),
    ]
    assert dropped_pairs_source(msgs, max_pairs=1, max_chars=10000) == "User: first\n\nAssistant: one"
    assert dropped_pairs_source(msgs, max_pairs=10, max_chars=10000) == ""


def test_parse_tool_calls_normalizes_read_aliases():
    """Read aliases file_path/offset/limit are renamed to path/line_offset/n_lines.

    Regression: request_id 72e74c67 — DeepSeek emits ``offset`` (and, in earlier
    incidents, ``file_path``/``limit``) instead of the schema names, and Kimi
    rejects the call as extra/missing args. Normalization must run on every
    parse path, including DSML.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{fw}tool_calls>\n"
        f'<{fw}invoke name="Read">\n'
        f'<{fw}parameter name="file_path" string="true">/a/b.md</{fw}parameter>\n'
        f'<{fw}parameter name="offset" string="false">40</{fw}parameter>\n'
        f'<{fw}parameter name="limit" string="false">30</{fw}parameter>\n'
        f"</{fw}invoke>\n"
        f"</{fw}tool_calls>"
    )
    schemas = {"Read": {"path": "string", "line_offset": "integer", "n_lines": "integer"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args == {"path": "/a/b.md", "line_offset": 40, "n_lines": 30}


def test_parse_xml_invoke_read_offset_alias_coerces_integer():
    """Plain-XML <invoke> with the provider's ``offset`` alias must yield int.

    Unlike the DSML path (whose ``_coerce_dsml`` already yields int for
    string="false"), the XML invoke path decodes the value via ``_coerce_scalar``
    *before* the alias name is known, so ``offset`` arrives as the string ``"25"``
    and only later becomes ``line_offset``. Without integer coercion in
    ``_normalize_calls`` Kimi rejects it as "/line_offset must be integer".
    Regression: request_id 72e74c67.
    """
    text = (
        "<tool_calls>\n"
        '<invoke name="Read">\n'
        '<parameter name="path">/a/b.md</parameter>\n'
        '<parameter name="offset">25</parameter>\n'
        '<parameter name="limit">10</parameter>\n'
        "</invoke>\n"
        "</tool_calls>"
    )
    parsed = parse_tool_calls(text, None)  # no schema — fallback must kick in
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["line_offset"] == 25
    assert isinstance(args["line_offset"], int)
    assert args["n_lines"] == 10
    assert isinstance(args["n_lines"], int)


def test_parse_xml_invoke_read_line_offset_no_schema():
    """XML invoke with direct ``line_offset``/``n_lines`` and no tool schema.

    In production the client's Read tool may not carry an extractable type for
    these parameters (e.g. anyOf structure), so _normalize_calls must fall back
    to the known-numeric map instead of leaving them as strings. Kimi rejects
    "line_offset must be integer" when a string is passed.

    Regression: request_id 7e844d706bdb — DeepSeek emitted XML invoke with
    ``string="true"`` on numeric params; tool_schema_map returned no type for
    line_offset, so the value stayed as "509".
    """
    text = (
        "<tool_calls>\n"
        '<invoke name="Read">\n'
        '<parameter name="path" string="true">crates/agent00x/src/memory.rs</parameter>\n'
        '<parameter name="line_offset" string="true">509</parameter>\n'
        '<parameter name="n_lines" string="true">40</parameter>\n'
        "</invoke>\n"
        "</tool_calls>"
    )
    parsed = parse_tool_calls(text, None)  # no schema at all
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["line_offset"] == 509
    assert isinstance(args["line_offset"], int)
    assert args["n_lines"] == 40
    assert isinstance(args["n_lines"], int)
    # path must remain a string even though it's also in the XML invoke
    assert args["path"] == "crates/agent00x/src/memory.rs"
    assert isinstance(args["path"], str)


def test_parse_tool_calls_coerces_grep_boolean_flag():
    """Grep's boolean ``-n`` arrives as a string and must become a real bool.

    Regression: request_id 103549897c0f — DeepSeek emitted ``-n`` as ``"true"``
    and Kimi rejected it with ``expected boolean``. Schema-typed coercion must
    apply on every parse path.
    """
    text = '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "x", "-n": "true"}}]}'
    schemas = {"Grep": {"pattern": "string", "-n": "boolean"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args == {"pattern": "x", "-n": True}
    assert isinstance(args["-n"], bool)


def test_parse_dsml_normalizes_bash_aliases():
    """Bash aliases cmd→command and desc→description are renamed to the schema names.

    Regression: request_id dc4ee19c — DeepSeek emitted a DSML ``<invoke name="Bash">``
    with parameters ``cmd``/``desc`` (plus, in other incidents, ``cwd``) instead of the
    client's ``command``/``description``, and Kimi rejected it as an extra property.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{fw}tool_calls>\n"
        f'<{fw}invoke name="Bash">\n'
        f'<{fw}parameter name="cmd" string="true">git status --short</{fw}parameter>\n'
        f'<{fw}parameter name="desc" string="true">Check git state</{fw}parameter>\n'
        f"</{fw}invoke>\n"
        f"</{fw}tool_calls>"
    )
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args == {"command": "git status --short", "description": "Check git state"}


def test_parse_dsml_bash_cwd_kept_when_declared():
    """A legitimate ``cwd`` parameter is preserved (not dropped or renamed).

    DeepSeek also emits ``cwd`` for Bash; when the client's schema declares it, the
    value must survive normalization untouched.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{fw}tool_calls>\n"
        f'<{fw}invoke name="Bash">\n'
        f'<{fw}parameter name="cmd" string="true">ls</{fw}parameter>\n'
        f'<{fw}parameter name="cwd" string="true">/tmp/proj</{fw}parameter>\n'
        f"</{fw}invoke>\n"
        f"</{fw}tool_calls>"
    )
    schemas = {"Bash": {"command": "string", "description": "string", "cwd": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args == {"command": "ls", "cwd": "/tmp/proj"}


def test_parse_dsml_normalizes_edit_write_file_path():
    """Edit/Write alias file_path→path is renamed to the schema name.

    DeepSeek emits ``file_path`` for Edit (11 incidents) and Write (1 incident); Kimi
    expects ``path`` and rejects ``file_path`` as an extra property.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    edit_text = (
        f"<{fw}tool_calls>\n"
        f'<{fw}invoke name="Edit">\n'
        f'<{fw}parameter name="file_path" string="true">/a/b.py</{fw}parameter>\n'
        f'<{fw}parameter name="old_string" string="true">foo</{fw}parameter>\n'
        f'<{fw}parameter name="new_string" string="true">bar</{fw}parameter>\n'
        f"</{fw}invoke>\n"
        f"</{fw}tool_calls>"
    )
    write_text = (
        f"<{fw}tool_calls>\n"
        f'<{fw}invoke name="Write">\n'
        f'<{fw}parameter name="file_path" string="true">/a/c.txt</{fw}parameter>\n'
        f'<{fw}parameter name="content" string="true">hello</{fw}parameter>\n'
        f"</{fw}invoke>\n"
        f"</{fw}tool_calls>"
    )
    schemas = {
        "Edit": {"path": "string", "old_string": "string", "new_string": "string"},
        "Write": {"path": "string", "content": "string"},
    }
    parsed_edit = parse_tool_calls(edit_text, schemas)
    assert parsed_edit is not None
    edit_args = json.loads(parsed_edit[0][0].arguments)
    assert edit_args == {"path": "/a/b.py", "old_string": "foo", "new_string": "bar"}

    parsed_write = parse_tool_calls(write_text, schemas)
    assert parsed_write is not None
    write_args = json.loads(parsed_write[0][0].arguments)
    assert write_args == {"path": "/a/c.txt", "content": "hello"}


def test_parse_dsml_mixed_bare_invoke_with_prefixed_params():
    """Mixed format: bare <invoke> (no DSML prefix) + DSML-prefixed parameters.

    Regression: request_id 882feca7 — DeepSeek emitted a bare ``<invoke name="Bash">``
    inside a DSML _calls wrapper while keeping the DSML prefix on all parameter tags.
    The DSML parser missed it (no prefixed invoke), then _strip_dsml stripped the param
    tags leaving orphaned text that the XML fallback wrapped as {"content": ...} — Kimi
    rejected this as missing 'command' / extra 'content'.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    cmd = "cargo +nightly build --release -p sysmon-agent 2>&1 | tail -n 20"
    desc = "\u041f\u0435\u0440\u0435\u0441\u043e\u0431\u0440\u0430\u0442\u044c sysmon-agent x86_64"
    text = (
        f"<{fw}_calls>\n"
        '<invoke name="Bash">\n'
        f'<{fw}parameter name="command" string="true">{cmd}</{fw}parameter>\n'
        f'<{fw}parameter name="description" string="true">{desc}</{fw}parameter>\n'
        f'<{fw}parameter name="timeout" string="false">300</{fw}parameter>\n'
        "</invoke>\n"
        f"</{fw}_calls>"
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["command"] == "cargo +nightly build --release -p sysmon-agent 2>&1 | tail -n 20"
    assert args["description"] == "\u041f\u0435\u0440\u0435\u0441\u043e\u0431\u0440\u0430\u0442\u044c sysmon-agent x86_64"
    assert args["timeout"] == 300


def test_parse_dsml_mixed_inconsistent_param_closing_tags():
    """Mixed closing tags: first param closes bare, second with DSML prefix.

    Regression: request_id b8cb5a9f — DeepSeek emitted the first parameter's
    closing tag as a bare ``</parameter>`` while the second used the
    DSML-prefixed close. The lazy value regex in _DSML_PARAM_RE skipped past
    the bare close and swallowed subsequent params into the first value,
    producing a single mangled "command" arg instead of separate command/description.
    """
    fw = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{fw}_calls>\n"
        '<invoke name="Bash">\n'
        f'<{fw}parameter name="command" string="true">tail -n 40 /some/log</parameter>\n'
        f'<{fw}parameter name="description" string="true">Read log tail</{fw}parameter>\n'
        "</invoke>\n"
        f"</{fw}_calls>"
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert "command" in args and "description" in args
    assert args["command"] == "tail -n 40 /some/log"
    assert args["description"] == "Read log tail"


def test_normalize_keeps_real_name_when_alias_absent():
    """A well-formed call already using the canonical name is left untouched."""
    text = '{"tool_calls": [{"name": "Bash", "arguments": {"command": "pwd"}}]}'
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    args = json.loads(parsed[0][0].arguments)
    assert args == {"command": "pwd"}


def test_parse_parameter_style_plain():
    """DeepSeek's invoke-less <tool_calls><parameter> form parses into a call.

    Real incident (request_id 26f9caa2): the tool name arrives as its own
    ``<parameter name="name">Read</parameter>`` with sibling argument parameters,
    and no inner <invoke>. Previously this fell through every generic path to
    unparsed:json_malformed.
    """
    text = (
        "<tool_calls>"
        '<parameter name="name">Read</parameter>'
        '<parameter name="path">/home/odolen/ssd_new/DanyAPI/docs/tooltrace-playbook.md</parameter>'
        "</tool_calls>"
    )
    schemas = {"Read": {"path": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    call = parsed[0][0]
    assert call.name == "Read"
    args = json.loads(call.arguments)
    assert args == {"path": "/home/odolen/ssd_new/DanyAPI/docs/tooltrace-playbook.md"}


def test_parse_parameter_style_nested_wrapper():
    """The doubled <tool_calls><tool_calls> wrapper also parses.

    DeepSeek occasionally nests the wrapper twice; only the <parameter> tags are
    consumed, so both forms resolve to the same call.
    """
    text = (
        "<tool_calls>"
        "<tool_calls>"
        '<parameter name="name">Read</parameter>'
        '<parameter name="path">/home/odolen/ssd_new/DanyAPI/docs/tooltrace-playbook.md</parameter>'
        "</tool_calls>"
        "</tool_calls>"
    )
    schemas = {"Read": {"path": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    call = parsed[0][0]
    assert call.name == "Read"
    args = json.loads(call.arguments)
    assert args == {"path": "/home/odolen/ssd_new/DanyAPI/docs/tooltrace-playbook.md"}


def test_parse_parameter_style_multiple_params():
    """Multiple argument parameters map to the call's arguments dict."""
    text = (
        "<tool_calls>"
        '<parameter name="name">Bash</parameter>'
        '<parameter name="command">git status --short</parameter>'
        '<parameter name="description">check working tree state</parameter>'
        "</tool_calls>"
    )
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    call = parsed[0][0]
    assert call.name == "Bash"
    args = json.loads(call.arguments)
    assert args == {"command": "git status --short", "description": "check working tree state"}


def test_parse_parameter_style_no_name_param_is_prose():
    """Parameters without a 'name' entry are not a tool call (left as prose)."""
    text = '<tool_calls><parameter name="note">just some metadata</parameter></tool_calls>'
    parsed = parse_tool_calls(text, {"Read": {"path": "string"}})
    assert parsed is None


def test_parse_parameter_style_surrounding_prose_preserved():
    """Prose outside the parameter run survives as the wrapper payload."""
    text = (
        "I'll read the file now.\n"
        "<tool_calls>"
        '<parameter name="name">Read</parameter>'
        '<parameter name="path">/a/b.txt</parameter>'
        "</tool_calls>\n"
        "Let me know if that helps."
    )
    schemas = {"Read": {"path": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    call = parsed[0][0]
    assert call.name == "Read"
    args = json.loads(call.arguments)
    assert args == {"path": "/a/b.txt"}
    wrapper = " ".join(parsed[1].split())
    assert "I'll read the file now." in wrapper
    assert "Let me know if that helps." in wrapper


def test_parse_tagname_tool_calls_single():
    """<tool_calls><Bash>…</Bash></tool_calls>: tool name is a bare element tag."""
    text = (
        "<tool_calls>"
        "<Bash>"
        "<command>sudo -n true && echo SUDO_OK || echo NEEDS_PASSWORD</command>"
        "<description>Verify sudo is unlocked</description>"
        "</Bash>"
        "</tool_calls>"
    )
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "sudo -n true && echo SUDO_OK || echo NEEDS_PASSWORD"
    assert args["description"] == "Verify sudo is unlocked"


def test_parse_tagname_tool_calls_multiple():
    """Multiple <Name> elements in one block each become a call (request_id f706e842)."""
    text = (
        "<tool_calls>"
        "<Bash><command>sudo -n true 2>&1</command><description>Check sudo</description></Bash>"
        "<Bash><command>whoami; id</command><description>Show user</description></Bash>"
        "</tool_calls>"
    )
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Bash", "Bash"]
    assert json.loads(calls[0].arguments)["command"] == "sudo -n true 2>&1"
    assert json.loads(calls[1].arguments)["command"] == "whoami; id"


def test_parse_tagname_tool_calls_child_tags_not_tools():
    """<command>/<description> children of a <Bash> block must not become their own calls."""
    text = "<tool_calls><Grep><pattern>eula|EULA</pattern><path>/usr/bin/amdgpu-install</path></Grep></tool_calls>"
    schemas = {"Grep": {"pattern": "string", "path": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    # Only the Grep call; pattern/path are its args, not separate tools.
    assert [c.name for c in calls] == ["Grep"]
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == "eula|EULA"
    assert args["path"] == "/usr/bin/amdgpu-install"


def test_parse_tagname_tool_calls_truncated_stream():
    """Unterminated final element (log 1000-char cap) still recovers its args."""
    text = (
        "<tool_calls><Bash><command>ls -l /tmp/deb.deb</command><description>Check deb"
        # no </Bash>, no </tool_calls> — stream cut mid-description
    )
    schemas = {"Bash": {"command": "string", "description": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "ls -l /tmp/deb.deb"


def test_parse_tagname_truncated_write_with_angle_brackets_in_content():
    """Truncated <Write> with < in content value (log b728b75a).

    DeepSeek emits tag-name form, hits max_tokens mid-content. The old regex
    [^<]* stopped at the first < inside the Rust code; now (.*?) + tail recovery
    captures everything from <content> to end of body.
    """
    text = (
        "Записываю файл.\n"
        "<tool_calls>\n"
        "<Write>\n"
        "<path>net-agent/tests/protocol_frames.rs</path>\n"
        "<content>//! Roundtrip tests.\n"
        "fn check() {\n"
        "    assert!(frame.len() <= MAX_SIZE);\n"
        "    let x: Vec<u8> = vec![1, 2];\n"
    )
    schemas = {"Write": {"path": "string", "content": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Write"
    args = json.loads(calls[0].arguments)
    assert args["path"] == "net-agent/tests/protocol_frames.rs"
    assert "<= MAX_SIZE" in args["content"]
    assert "Vec<u8>" in args["content"]


def test_parse_tagname_defers_to_invoke_form():
    """When a real <invoke name=...> is present, the tag-name branch must not fire."""
    text = '<tool_calls>\n<invoke name="bash">\n<command>Get-ChildItem -Name</command>\n</invoke>\n</tool_calls>'
    parsed = parse_tool_calls(text)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "bash"


def test_parse_tagname_surrounding_prose_preserved():
    """Prose before the <tool_calls> block survives as the wrapper payload."""
    text = "Checking that sudo is unlocked.\n<tool_calls><Bash><command>sudo -n true</command></Bash></tool_calls>\nDone."
    schemas = {"Bash": {"command": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    norm = " ".join(wrapper.split())
    assert "Checking that sudo is unlocked." in norm
    assert "Done." in norm


def test_normalize_calls_tool_name_case_bash():
    # DeepSeek emits <invoke name="bash"> (lowercase) but client declares "Bash".
    schemas = {"Bash": {"command": "string", "__required__": set()}}
    text = '<tool_calls><invoke name="bash"><parameter name="command" string="true">ls</parameter></invoke></tool_calls>'
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"


def test_normalize_calls_tool_name_case_grep():
    # Same pattern with Grep: model emits "grep", client declares "Grep".
    schemas = {"Grep": {"pattern": "string", "__required__": set()}}
    text = '{"tool_calls": [{"name": "grep", "arguments": {"pattern": "foo"}}]}'
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Grep"


def test_normalize_calls_no_schema_keeps_original_case():
    # Without a schema map there's nothing to normalize against — name stays as-is.
    text = '<tool_calls><invoke name="myTool"><parameter name="x" string="true">1</parameter></invoke></tool_calls>'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert calls[0].name == "myTool"


def test_normalize_todo_status_completed_to_done():
    # DeepSeek emits status="completed"; client only accepts done/pending/in_progress.
    text = (
        '{"tool_calls": [{"name": "TodoList", "arguments": {"todos": ['
        '{"title": "a", "status": "completed"}, '
        '{"title": "b", "status": "active"}, '
        '{"title": "c", "status": "not_started"}]}}]}'
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    statuses = [t["status"] for t in args["todos"]]
    assert statuses == ["done", "in_progress", "pending"]


def test_normalize_todo_status_valid_values_untouched():
    # Already-canonical statuses must pass through unchanged.
    text = (
        '{"tool_calls": [{"name": "TodoList", "arguments": {"todos": ['
        '{"title": "a", "status": "done"}, '
        '{"title": "b", "status": "in_progress"}, '
        '{"title": "c", "status": "pending"}]}}]}'
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    statuses = [t["status"] for t in args["todos"]]
    assert statuses == ["done", "in_progress", "pending"]


def test_normalize_todo_status_case_insensitive():
    # Status casing should not matter: "Completed" normalizes to "done".
    text = '{"tool_calls": [{"name": "TodoList", "arguments": {"todos": [{"title": "a", "status": "Completed"}]}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["todos"][0]["status"] == "done"


def test_normalize_todo_status_ignores_non_todolist():
    # A different tool with a todos-like arg must not be touched.
    text = '{"tool_calls": [{"name": "OtherTool", "arguments": {"todos": [{"title": "a", "status": "completed"}]}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    # Not a TodoList call → status left as generated.
    assert args["todos"][0]["status"] == "completed"


# --- Invalid escape sequences (regex patterns with single backslashes) ---


def test_fix_invalid_escapes_in_regex_pattern():
    # DeepSeek emits regex like \. and \( which are invalid JSON escapes.
    text = '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "\\.foo\\(bar\\)"}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == r"\.foo\(bar\)"


def test_fix_invalid_escapes_mixed_with_valid():
    # Mix of valid escapes (\", \n) and invalid ones (\d).
    text = '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "\\d+\\."}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == r"\d+\."


def test_fix_invalid_escapes_no_false_positive():
    # Valid JSON escapes (\n, \t, \") should NOT be doubled.
    text = '{"tool_calls": [{"name": "Write", "arguments": {"content": "line1\\nline2"}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["content"] == "line1\nline2"


# --- Trailing non-JSON garbage (fences, XML tags) ---


def test_strip_trailing_fenced_code_block():
    # JSON followed by a closing ``` fence.
    text = '{"tool_calls": [{"name": "Read", "arguments": {"path": "/a.rs"}}]}\n```'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["path"] == "/a.rs"


def test_strip_trailing_fenced_code_block_with_lang():
    # JSON followed by a closing ```json fence.
    text = '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "foo"}}]}\n```json'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == "foo"


def test_strip_trailing_xml_closing_tag():
    # JSON followed by a stray XML closing tag.
    text = '{"tool_calls": [{"name": "Read", "arguments": {"path": "/x"}}]}</invoke>'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert args["path"] == "/x"


def test_prose_before_fenced_json():
    # DeepSeek emits prose before a fenced JSON tool_calls block.
    # The fence doesn't wrap the entire text so the full-wrap regex misses it,
    # but the embedded-fence fallback in _strip_fences extracts the JSON.
    text = (
        "Сначала дочитаю нужные фрагменты — конец `run`, вызовы в mission и web.\n\n"
        "```json\n"
        '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "foo|bar", "-i": true}}, '
        '{"name": "Read", "arguments": {"path": "/a.rs", "line_offset": 10, "n_lines": 50}}]}\n'
        "```"
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 2
    assert calls[0].name == "Grep"
    assert calls[1].name == "Read"


def test_prose_before_fenced_json_with_schemas():
    # Same pattern but with tool schemas (as in production).
    text = (
        "Let me inspect the relevant parts.\n\n"
        "```json\n"
        '{"tool_calls": [{"name": "Grep", "arguments": {"pattern": "use crate::llm|ChatMessage", "path": "/a/mod.rs", "-n": true, "head_limit": 30}}]}\n'
        "```"
    )
    schemas = {
        "Grep": {"pattern": "string", "path": "string", "-n": "boolean", "head_limit": "integer"},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["-n"] is True
    assert args["head_limit"] == 30


# --- Missing intermediate braces between tool_calls elements ---


def test_fix_missing_call_braces_single_gap():
    # Third call missing its closing '}' — only the arguments sub-object is closed.
    text = (
        '{"tool_calls": ['
        '{"name": "Read", "arguments": {"path": "/a.rs"}}, '
        '{"name": "Grep", "arguments": {"pattern": "foo|bar", "-i": true}, '
        '{"name": "Grep", "arguments": {"pattern": "baz"}}]}'
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 3
    assert calls[0].name == "Read"
    assert calls[1].name == "Grep"
    args1 = json.loads(calls[1].arguments)
    assert args1["pattern"] == "foo|bar"
    assert args1.get("-i") is True


def test_fix_missing_call_braces_already_valid():
    # Properly closed calls should NOT be modified.
    text = '{"tool_calls": [{"name": "Read", "arguments": {"path": "/a.rs"}}, {"name": "Grep", "arguments": {"pattern": "x"}}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 2


def test_fix_missing_call_braces_multiple_gaps():
    # Two consecutive calls missing their closing braces.
    text = (
        '{"tool_calls": ['
        '{"name": "Read", "arguments": {"path": "/a.rs"}}, '
        '{"name": "Grep", "arguments": {"pattern": "x"}, '
        '{"name": "Glob", "arguments": {"pattern": "*.rs"}, '
        '{"name": "Read", "arguments": {"path": "/b.rs"}}]}'
    )
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 4


# --- Raw control characters inside JSON strings (strict=False) ---


def test_raw_newlines_in_json_string():
    # DeepSeek emits actual newline bytes inside string values instead of \n escapes.
    cmd = "python3 - <<'PY'\nimport json\nprint('ok')\nPY"
    text = '{"tool_calls":[{"name":"Bash","command":"' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '","description":"run script"}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert "import json" in args["command"]


def test_raw_newlines_in_edit_strings():
    # Edit tool with multi-line old_string/new_string containing raw newlines.
    old_s = "let x = 1;\n\nfn main() {}\n"
    new_s = 'let x = 2;\n\nfn main() { println!("hi"); }\n'

    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    text = '{"tool_calls":[{"name":"Edit","path":"/a.rs","old_string":"' + esc(old_s) + '","new_string":"' + esc(new_s) + '"}]}'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert "let x = 1;" in args["old_string"]
    assert 'println!("hi")' in args["new_string"]


def test_raw_newlines_with_prose_and_fences():
    # Full pattern: prose + ```json fence + JSON with raw newlines + closing fence.
    cmd = 'python3 -c "import sys; print(sys.version)"'
    json_part = '{"tool_calls":[{"name":"Bash","command":"' + cmd.replace('"', '\\"') + '","description":"check version"}]}'
    text = "Проверяю версию.\n\n```json\n" + json_part + "\n```"
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    args = json.loads(calls[0].arguments)
    assert "sys.version" in args["command"]


def test_parameter_inference_from_schemas():
    # <tool_calls><parameter name="todos">...</parameter></tool_calls> without a
    # <parameter name="name"> tag — tool inferred from schemas.
    text = (
        "<tool_calls>\n"
        '  <parameter name="todos">'
        '[{"status":"in_progress","title":"Обновить PROGRESS.md"},{"status":"done","title":"Выровнять нумерацию"}]'
        "</parameter>\n"
        "</tool_calls>"
    )
    schemas = {
        "TodoList": {"todos": [], "merge": True},
        "Bash": {"command": "", "description": ""},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "TodoList"
    args = json.loads(calls[0].arguments)
    assert isinstance(args["todos"], list)
    assert args["todos"][0]["title"] == "Обновить PROGRESS.md"


def test_parameter_inference_no_schemas_falls_through():
    # Same pattern but no schemas provided → cannot infer → falls through to prose.
    text = '<tool_calls>\n<parameter name="todos">[{"status":"done","title":"x"}]</parameter>\n</tool_calls>'
    parsed = parse_tool_calls(text, None)
    assert parsed is None


def test_parameter_inference_ambiguous_schemas_skips():
    # Two tools share the same param name → ambiguous → no inference.
    text = '<tool_calls>\n<parameter name="command">ls -la</parameter>\n</tool_calls>'
    schemas = {
        "Bash": {"command": "", "description": ""},
        "Shell": {"command": "", "timeout": 30},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is None


def test_flat_tool_key_single_object():
    # DeepSeek emits a flat single-object format: {"tool": "Grep", "arguments": {...}}
    # instead of the expected {"tool_calls": [{"name": ..., "arguments": ...}]}.
    text = '```json\n{"tool": "Read", "arguments": {"path": "/a.md", "offset": 10}}\n```'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    args = json.loads(calls[0].arguments)
    assert args["path"] == "/a.md"


def test_flat_tool_key_with_prose():
    # Prose before the JSON fence — the object is still extracted correctly.
    text = 'Коммичу.\n\n```json\n{"tool": "Bash", "arguments": {"command": "git status"}}\n```'
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, wrapper = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    assert "Коммичу" in wrapper


def test_flat_tool_key_parameters_synonym_with_prose():
    """tooltrace 6e02a45f: the flat single-object form uses `parameters` instead of
    `arguments`, wrapped in a markdown fence with prose before it.
    """
    text = 'Запускаю скан.\n\n```json\n{"tool": "nmap", "parameters": {"target": "76.158.52.121", "ports": "80,443"}}\n```'
    schemas = {"nmap": {"target": "", "ports": ""}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "nmap"
    args = json.loads(calls[0].arguments)
    assert args["target"] == "76.158.52.121"
    assert args["ports"] == "80,443"


# ---------------------------------------------------------------------------
# Regression tests from tooltrace.ndjson — new structural patterns (Sept 2025)
# ---------------------------------------------------------------------------


def test_dsml_invoke_string_false_attr_inline_json_body():
    """tooltrace a25a0b91: invoke tag carries string=\"false\" after name, and the
    body contains raw JSON instead of <parameter> children.

    The regex must tolerate the extra attribute and the main loop must fall back
    to inline-JSON extraction when no parameter tags are found inside the body.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Bash" string="false">'
        '{"command": "git status", "timeout": 30}'
        "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    schemas = {"Bash": {"command": "", "timeout": 30}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "git status"
    assert args["timeout"] == 30


def test_dsml_params_no_invoke_proper_close():
    """tooltrace dcc0519a: bare DSML parameter tags inside <calls> with no enclosing
    <invoke>. The tool name is inferred from the param names against schemas.

    Proper close tags (with 'parameter' after the DSML separator) are used here.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="command" string="true">ls -la /tmp</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
    )
    schemas = {
        "Bash": {"command": "", "timeout": 30},
        "Read": {"path": ""},
    }
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "ls -la /tmp"


def test_dsml_params_no_invoke_malformed_close():
    """tooltrace bea80392: same as dcc0519a but the close tag is missing the word
    'parameter' after the DSML separator. The lenient tag regex must still cut the value.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="command" string="true">echo hello</\uff5c\uff5cDSML\uff5c\uff5c>'
    )
    schemas = {
        "Bash": {"command": "", "timeout": 30},
        "Read": {"path": ""},
    }
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "echo hello"


def test_dsml_param_arguments_json_missing_bracket_close():
    """tooltrace 626e51d6: a single parameter named 'arguments' whose value is a JSON
    object. The close tag lacks the opening bracket before the DSML separator.

    Tests both the lenient close-tag matching AND the arguments-unwrap logic that
    extracts the inner dict as the actual tool args.
    """
    text = (
        "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
        '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="arguments" string="false">'
        '{"command": "git log --oneline -5", "cwd": "/repo"}'
        "\uff5c\uff5cDSML\uff5c\uff5c parameter>"
    )
    schemas = {
        "Bash": {"command": "", "cwd": "", "timeout": 30},
        "Read": {"path": ""},
    }
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "git log --oneline -5"
    assert args["cwd"] == "/repo"


def test_dsml_single_pipe_opener_is_repaired():
    """tooltrace b1c6a1cc: DeepSeek emitted DSML openers with a SINGLE pipe before
    DSML ('<|DSML||parameter ...>') while close tags kept the canonical two-pipe form.
    The canonical _DSML_SEP never matched these openers, so the call was dropped.
    """
    text = (
        "<\uff5cDSML\uff5c\uff5c calls>\n"
        '<\uff5cDSML\uff5c\uff5c parameter name="command">'
        "cd /home/odolen/AI/Work/SX && cargo test --workspace 2>&1 | tail -60"
        '</\uff5c\uff5cDSML\uff5c\uff5c timeout="400"</\uff5c\uff5cDSML\uff5c\uff5c>\n'
        "</\uff5c\uff5cDSML\uff5c\uff5c calls>"
    )
    schemas = {
        "Bash": {"command": "", "timeout": 30},
        "Read": {"path": ""},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args["command"] == "cd /home/odolen/AI/Work/SX && cargo test --workspace 2>&1 | tail -60"


def test_dsml_selfclosed_marker_and_double_single_pipe_is_repaired():
    """tooltrace 2a0ddca7: the block combines TWO malformations the earlier
    single-LEFT-pipe fix did not cover -- a both-single separator (right side also
    one pipe) and a self-closed marker (`marker >` instead of `marker`). The
    canonical _DSML_SEP never matched, so the whole browser_snapshot call was
    dropped.
    """
    text = (
        "<|DSML|> calls>\n"
        '<|DSML|> invoke name="browser_snapshot">\n'
        '<|DSML|> parameter name="url">https://75.189.221.120</|DSML|> parameter>\n'
        '<|DSML|> parameter name="stealth">true</|DSML|> parameter>\n'
        '<|DSML|> parameter name="wait">10000</|DSML|> parameter>\n'
        "</|DSML|> invoke>\n"
        "</|DSML|> calls>"
    )
    schemas = {
        "browser_snapshot": {"url": "string", "stealth": "boolean", "wait": "integer"},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "browser_snapshot"
    assert json.loads(calls[0].arguments) == {
        "url": "https://75.189.221.120",
        "stealth": True,
        "wait": 10000,
    }


def test_dsml_single_pipe_repair_leaves_canonical_tags():
    """_repair_dsml_separator must expand every 1-or-2-pipe spelling to the canonical
    two pipes per side while leaving canonical tags (and the pipe character) untouched.
    """
    canonical = '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">'
    assert _repair_dsml_separator(canonical) == canonical
    # A canonical close tag must survive unchanged too.
    assert _repair_dsml_separator("</\uff5c\uff5cDSML\uff5c\uff5c>") == "</\uff5c\uff5cDSML\uff5c\uff5c>"
    # 1|2 (left-single, b1c6a1cc), 2|1 (right-single) and 1|1 (both-single, 2a0ddca7)
    # all expand to the canonical 2|2 form.
    assert _repair_dsml_separator("<\uff5cDSML\uff5c\uff5c calls>") == "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
    assert _repair_dsml_separator("</\uff5cDSML\uff5c\uff5c>") == "</\uff5c\uff5cDSML\uff5c\uff5c>"
    assert _repair_dsml_separator("<\uff5c\uff5cDSML\uff5c calls>") == "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
    assert _repair_dsml_separator("<\uff5cDSML\uff5c calls>") == "<\uff5c\uff5cDSML\uff5c\uff5c calls>"
    # ASCII and fullwidth separators are kept as-is (not silently converted).
    assert _repair_dsml_separator("<||DSML| calls>") == "<||DSML|| calls>"
    assert _repair_dsml_separator("<\uff5c\uff5cDSML\uff5c calls>") == "<\uff5c\uff5cDSML\uff5c\uff5c calls>"


def test_dsml_param_only_repeated_name_splits_calls():
    """tooltrace 24b809c9: a bare-parameter block (no <invoke>) encodes SEVERAL calls
    by repeating the same argument name — four <parameter name="path"> are four Read
    calls, with trailing params fused into the close tag (line_offset/n_lines).

    The old param-only path collapsed the whole run into one dict (last value wins),
    so only the last path survived.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter name="path">net-agent/AGENTS.md</{S}>\n'
        f'<{S} parameter name="path">CONTRIBUTING.md</{S} line_offset=170 n_lines=18>\n'
        f'<{S} parameter name="path">AGENTS.md</{S} line_offset=145 n_lines=15>\n'
        f'<{S} parameter name="path">docs/en/architecture/commands.md</{S} line_offset=30 n_lines=8>\n'
        f"</{S} calls>"
    )
    schemas = {
        "Read": {"path": "", "line_offset": 1, "n_lines": 1},
        "Edit": {"path": "", "old_string": "", "new_string": ""},
        "Write": {"path": "", "content": ""},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Read", "Read", "Read", "Read"]
    args = [json.loads(c.arguments) for c in calls]
    assert args[0] == {"path": "net-agent/AGENTS.md"}
    assert args[1] == {"path": "CONTRIBUTING.md", "line_offset": 170, "n_lines": 18}
    assert args[2] == {"path": "AGENTS.md", "line_offset": 145, "n_lines": 15}
    assert args[3] == {"path": "docs/en/architecture/commands.md", "line_offset": 30, "n_lines": 8}


def test_dsml_param_only_repeated_command_splits_calls():
    """tooltrace f4275506: two <parameter name="command"> are two distinct Bash
    calls, not one (last-wins).
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} parameter name="command">sed -n 1,10p a.md</{S}>\n<{S} parameter name="command">sed -n 25,40p b.md</{S}>\n</{S} calls>'
    schemas = {"Bash": {"command": "", "timeout": 1}, "Read": {"path": ""}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None
    assert [c.name for c in calls] == ["Bash", "Bash"]
    args = [json.loads(c.arguments) for c in calls]
    assert args[0] == {"command": "sed -n 1,10p a.md"}
    assert args[1] == {"command": "sed -n 25,40p b.md"}


def test_dsml_param_only_repeated_command_fused_timeout():
    """tooltrace 50ad2c8a: a bare-parameter block whose close tag fuses timeout=300
    onto the first of two repeated command params.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} parameter name="command">cargo test</{S} timeout="300"{S}>\n<{S} parameter name="command">wc -l mod.rs</{S}>\n</{S} calls>'
    schemas = {"Bash": {"command": "", "timeout": 1}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None
    assert [c.name for c in calls] == ["Bash", "Bash"]
    first = json.loads(calls[0].arguments)
    assert first == {"command": "cargo test", "timeout": 300}
    assert json.loads(calls[1].arguments) == {"command": "wc -l mod.rs"}


def test_dsml_param_only_mixed_names_infer_per_call():
    """A bare-parameter block mixing explicit <parameter name="name"> with a
    repeated argument: each call resolves its own tool name.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter name="name">Bash</{S}>'
        f'<{S} parameter name="command">ls</{S}>\n'
        f'<{S} parameter name="name">Read</{S}>'
        f'<{S} parameter name="path">/a.md</{S}>\n'
        f"</{S} calls>"
    )
    schemas = {"Bash": {"command": ""}, "Read": {"path": ""}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None
    assert [(c.name, json.loads(c.arguments)) for c in calls] == [
        ("Bash", {"command": "ls"}),
        ("Read", {"path": "/a.md"}),
    ]


def test_dsml_param_eq_opener_with_trailing_quote():
    """A DSML parameter block whose argument name sits directly in the ``parameter``
    attribute (``parameter="command"``) with no ``name=`` word, and whose values carry
    a stray trailing quote (reported pattern: ``parameter="command">sleep 120"``). The
    name-in-attribute opener must match and the trailing quote must be stripped from
    every value.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter="command">sleep 120"{S}>\n'
        f'<{S} parameter="description">Wait for image generation and draft delivery"{S}>\n'
        f'<{S} parameter="timeout">140"{S}>\n'
        f"</{S} calls>"
    )
    schemas = {"Bash": {"command": "", "description": "", "timeout": 1}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    args = json.loads(calls[0].arguments)
    assert args == {
        "command": "sleep 120",
        "description": "Wait for image generation and draft delivery",
        "timeout": 140,
    }


def test_dsml_param_eq_opener_single_quotes_and_string_flag():
    """The ``parameter='X'`` variant, with a trailing ``string="true"`` flag: the
    name comes from the attribute, the flag is honoured, and the stray quote is gone.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f"<{S} calls>\n<{S} parameter='command' string=\"true\">pwd'{S}>\n</{S} calls>"
    calls, _ = _parse_dsml(text, {"Bash": {"command": ""}})
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    assert json.loads(calls[0].arguments) == {"command": "pwd"}


def test_dsml_param_eq_opener_canonical_untouched():
    """The new ``parameter="X"`` opener must not swallow the canonical
    ``parameter name="X"`` form: both appear here and both must parse.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} parameter name="command">echo hi</{S}>\n<{S} parameter="command">pwd"{S}>\n</{S} calls>'
    calls, _ = _parse_dsml(text, {"Bash": {"command": ""}})
    assert calls is not None
    assert [json.loads(c.arguments)["command"] for c in calls] == ["echo hi", "pwd"]


def test_dsml_param_eq_opener_inside_invoke():
    """tooltrace 2baa4153, 0d19b84c: the ``parameter="X"`` opener also appears INSIDE
    an ``<invoke name="Bash">`` body, not only in the bare-parameter layout. The
    invoke branch's bounded re-scan only looked for ``parameter name=``, so the args
    came out empty: the call was emitted as ``Bash {}``, the client rejected it as
    missing required, and the repair loop re-asked the same shape — 14 identical
    ``ok:missing_required`` turns in one session until the context ran out.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} invoke name="Bash">\n'
        f'<{S} parameter="command">V=/tmp/venv; "$V/bin/python3" -c "print(1)"{S}>\n'
        f'<{S} parameter="timeout">300{S}>\n'
        f"</{S} calls>"
    )
    schemas = {"Bash": {"command": "string", "timeout": "integer"}}
    calls, _ = _parse_dsml(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Bash"
    assert json.loads(calls[0].arguments) == {
        "command": 'V=/tmp/venv; "$V/bin/python3" -c "print(1)',
        "timeout": 300,
    }


def test_dsml_colon_equals_attributes_recovered():
    """tooltrace b1240bcd: DeepSeek replaced every ``=`` with ``:=""`` and every
    attribute separator with ``,=""`` -- ``name="timeout" :="" 600}``. No opener
    regex matches that spelling, so the call went out with empty args and the
    repair loop re-asked. The mangled pairs are recovered instead, and a bare word
    elsewhere in the body (the DSML tag name) is not mistaken for an argument.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} invoke name="WaitFor">\n<{S} task_id" :="" "bash-ru1nql6e",="" "timeout":600}}\n<{S} >\n<{S} >'
    schemas = {"WaitFor": {"task_id": "string", "timeout": "integer"}}
    calls, _ = parse_tool_calls(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "WaitFor"
    assert json.loads(calls[0].arguments) == {"task_id": "bash-ru1nql6e", "timeout": 600}


def test_dsml_bare_param_opener_inside_dsml_invoke():
    """tooltrace 42585858: the invoke and close tags carry the DSML separator, but the
    parameter opener was written as plain XML (`<parameter name="path">`). Every param
    regex keys on the separator, so the argument was invisible and the call went out
    with empty args -- the same missing_required repair loop.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} invoke name="Read">\n'
        f'<parameter name="path">/tmp/f.py</{S} parameter>\n'
        f'<parameter name="line_offset">55</{S} parameter>\n'
        f"</{S} invoke>\n"
        f"</{S} calls>"
    )
    schemas = {"Read": {"path": "string", "line_offset": "integer"}}
    calls, _ = parse_tool_calls(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {"path": "/tmp/f.py", "line_offset": 55}


def test_dsml_naked_attribute_name_inside_invoke():
    """tooltrace 5186a5ff, 6ea8d3ab, ab9045d8: the opener loses the whole
    ``parameter name=`` prefix and the ``=`` with it, leaving only the key and a
    dangling quote — `` ``. Every param regex wants
    ``parameter name="X"``, so the argument was invisible and the call went out with
    empty args; the repair loop re-asked the same shape (10 identical
    ``ok:missing_required`` turns in session 953c0ca4). The repair restores the
    canonical opener and keeps the ``string`` flag.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} invoke name="Edit">\n'
        f'<{S} path" string="true">/tmp/f.py{S}>\n'
        f'<{S} old_string" string="true">a = 1{S}>\n'
        f'<{S} new_string" string="true">a = 2{S}>\n'
        f"</{S} calls>"
    )
    schemas = {"Edit": {"path": "string", "old_string": "string", "new_string": "string"}}
    calls, _ = parse_tool_calls(text, schemas)
    assert calls is not None and len(calls) == 1
    assert calls[0].name == "Edit"
    assert json.loads(calls[0].arguments) == {
        "path": "/tmp/f.py",
        "old_string": "a = 1",
        "new_string": "a = 2",
    }


def test_dsml_param_eq_opener_glued_marker_before_gt():
    """tooltrace 4e3abd5c: the ``parameter="X"`` opener carries a stray DSML marker
    glued between the name quote and the closing ``>`` (``parameter="path"`` + marker +
    ``>``) instead of the canonical ``parameter="path">``. Both opener regexes require
    ``>`` right after the quote, so the whole block used to vanish as
    unparsed:no_tool_call. The repair must drop the marker and recover BOTH sibling
    Edit calls with clean argument values (no leading ``>``, no renamed ``parameter``
    argument from the bare-attribute repair).
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter="path"{S} >/tmp/test_alert_points.py{S} >\n'
        f'<{S} parameter="old_string"{S} >\nasync def old() -> None:\n'
        f'<{S} parameter="new_string"{S} >\nasync def new() -> None:\n'
        f"<{S} /{S} >\n"
        f'<{S} parameter="path"{S} >/tmp/test_alert_points.py{S} >\n'
        f'<{S} parameter="old_string"{S} >\nfrom a import B\n'
        f'<{S} parameter="new_string"{S} >\nfrom a import B, C\n'
        f"<{S} /{S} >\n"
        f"</{S} calls>"
    )
    schemas = {
        "Edit": {
            "path": "string",
            "old_string": "string",
            "new_string": "string",
            "__required__": {"path", "old_string", "new_string"},
        }
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 2
    for call in calls:
        assert call.name == "Edit"
        args = json.loads(call.arguments)
        assert args["path"] == "/tmp/test_alert_points.py"
        assert "parameter" not in args
    assert json.loads(calls[0].arguments)["old_string"].startswith("async def old")
    assert json.loads(calls[1].arguments)["new_string"] == "from a import B, C"


def test_dsml_param_glued_marker_mixed_brackets_one_run():
    """tooltrace 7b1bb070: within one param-only run DeepSeek mixes THREE glued-opener
    spellings for three different arguments — leading ``<`` (line_offset), leading
    ``</`` (n_lines), and no leading bracket at all (path) — each with a stray DSML
    marker glued before ``>``. None matched a canonical opener, so the whole Read
    call was dropped as unparsed:no_tool_call. The repair must normalise all three
    and recover one Read with the right integer/string argument values.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} parameter="line_offset"{S} >117</{S} parameter="n_lines"{S} >10{S} parameter="path"{S} >/tmp/f.py{S} >\n</{S} </{S} calls>'
    schemas = {
        "Read": {
            "path": "string",
            "line_offset": "integer",
            "n_lines": "integer",
            "__required__": {"path"},
        }
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    assert json.loads(calls[0].arguments) == {
        "line_offset": 117,
        "n_lines": 10,
        "path": "/tmp/f.py",
    }


def test_dsml_param_only_ambiguous_path_resolves_by_declaration_order():
    """tooltrace 533f7fa6: a bare-parameter block with two ``parameter="path"`` (no
    <invoke>, no other argument) is structurally ambiguous — both Read and
    ReadMediaFile accept exactly ``path`` — so _infer_tool_from_params returns None
    and both calls were dropped as unparsed:no_tool_call. With the real 26-tool
    Kimi schema the block must resolve to Read (declared before ReadMediaFile) while
    Edit/Write (declared earlier than Read, but with unsatisfied required keys) are
    skipped. Regression for the client-order priority rule.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = f'<{S} calls>\n<{S} parameter="path"{S} >/a/AGENTS.md{S} >\n<{S} parameter="path"{S} >/a/test_x.py{S} >\n</{S} calls>'
    schemas = {
        "Bash": {"command": "string", "__required__": {"command"}},
        "Edit": {
            "path": "string",
            "old_string": "string",
            "new_string": "string",
            "__required__": {"path", "old_string", "new_string"},
        },
        "Read": {"path": "string", "line_offset": "integer", "n_lines": "integer", "__required__": {"path"}},
        "ReadMediaFile": {"path": "string", "__required__": {"path"}},
        "Write": {"path": "string", "content": "string", "__required__": {"path", "content"}},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Read", "Read"]
    assert [json.loads(c.arguments) for c in calls] == [
        {"path": "/a/AGENTS.md"},
        {"path": "/a/test_x.py"},
    ]


def test_dsml_param_only_bare_separator_splits_sibling_calls():
    """tooltrace 0885f2d2 / 8b1c632c / 0241e65f: a param-only block packs SEVERAL
    sibling calls separated by a standalone bare `` `` / `` ``
    line that carries no word. The sibling calls have disjoint argument names
    (Grep uses -n/output_mode/pattern; Read uses line_offset/n_lines/path), so the
    repeated-name split cannot see the boundary and the whole block collapses into
    one group that matches no schema — the call was dropped.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter name="-n" string="false">true</{S}>\n'
        f'<{S} parameter name="output_mode" string="true">content</{S}>\n'
        f'<{S} parameter name="pattern" string="true">day_offset</{S}>\n'
        f"<{S}>\n"
        f'<{S} parameter name="line_offset" string="false">125</{S}>\n'
        f'<{S} parameter name="n_lines" string="false">30</{S}>\n'
        f'<{S} parameter name="path" string="true">docs/ARCHITECTURE.md</{S}>\n'
        f"</{S} calls>"
    )
    schemas = {
        "Grep": {"pattern": "", "output_mode": "", "-n": "", "path": ""},
        "Read": {"path": "", "line_offset": 1, "n_lines": 1},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Grep", "Read"]
    assert json.loads(calls[0].arguments) == {
        "-n": True,
        "output_mode": "content",
        "pattern": "day_offset",
    }
    assert json.loads(calls[1].arguments) == {
        "line_offset": 125,
        "n_lines": 30,
        "path": "docs/ARCHITECTURE.md",
    }


def test_dsml_param_only_bare_separator_three_calls():
    """tooltrace 0885f2d2: three sibling calls in one param-only block — Read,
    then a path-less Grep, then a Grep with a path — separated by a bare
    `` `` line. The middle Grep is inferred from its lone schema match.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter name="line_offset" string="false">248</{S}>\n'
        f'<{S} parameter name="n_lines" string="false">60</{S}>\n'
        f'<{S} parameter name="path" string="true">src/jobs.py</{S}>\n'
        f"</{S}>\n"
        f'<{S} parameter name="output_mode" string="true">files_with_matches</{S}>\n'
        f'<{S} parameter name="pattern" string="true">day_offset</{S}>\n'
        f"<{S}>\n"
        f'<{S} parameter name="output_mode" string="true">content</{S}>\n'
        f'<{S} parameter name="-n" string="false">true</{S}>\n'
        f'<{S} parameter name="pattern" string="true">day_offset|planned_for</{S}>\n'
        f'<{S} parameter name="path" string="true">docs/ARCHITECTURE.md</{S}>\n'
        f"</{S} calls>"
    )
    schemas = {
        "Grep": {"pattern": "", "output_mode": "", "-n": "", "path": ""},
        "Read": {"path": "", "line_offset": 1, "n_lines": 1},
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Read", "Grep", "Grep"]
    args = [json.loads(c.arguments) for c in calls]
    assert args[0] == {"line_offset": 248, "n_lines": 60, "path": "src/jobs.py"}
    assert args[1] == {"output_mode": "files_with_matches", "pattern": "day_offset"}
    assert args[2] == {
        "output_mode": "content",
        "-n": True,
        "pattern": "day_offset|planned_for",
        "path": "docs/ARCHITECTURE.md",
    }


def test_dsml_param_only_schema_split_recovers_multi_tool_block():
    """tooltrace 9cf75302: a bare-parameter block with no <invoke>, no repeated name
    and no separator tag glues parameters from several tools into one run — Bash
    command/cwd/description/run_in_background/timeout/disable_timeout, then Glob/Grep
    path/offset/head_limit/glob/type/-n/include_ignored.

    No single schema accepts every key, so _infer_tool_from_params returned None and
    the whole block was dropped as no_tool_call. The run must instead split where the
    next name leaves no tool accepting the whole group: the leading Bash args form one
    call, and the trailing keys (all of which Grep accepts) resolve to Grep.
    """
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} calls>\n"
        f'<{S} parameter name="command" string="true">cd /opt/app && ./run.sh</{S} parameter>\n'
        f'<{S} parameter name="cwd" string="true">/opt/app</{S} parameter>\n'
        f'<{S} parameter name="description" string="true">Start server</{S} parameter>\n'
        f'<{S} parameter name="run_in_background" string="false">false</{S} parameter>\n'
        f'<{S} parameter name="timeout" string="false">120</{S} parameter>\n'
        f'<{S} parameter name="disable_timeout" string="false">false</{S} parameter>\n'
        f'<{S} parameter name="path" string="true">/opt/app/output</{S} parameter>\n'
        f'<{S} parameter name="offset" string="false">0</{S} parameter>\n'
        f'<{S} parameter name="head_limit" string="false">0</{S} parameter>\n'
        f'<{S} parameter name="glob" string="true">**/*.py</{S} parameter>\n'
        f'<{S} parameter name="type" string="true">py</{S} parameter>\n'
        f'<{S} parameter name="-n" string="false">true</{S} parameter>\n'
        f'<{S} parameter name="include_ignored" string="false">false</{S} parameter>\n'
        f"</{S} calls>"
    )
    schemas = {
        "Bash": {
            "command": "",
            "cwd": "",
            "description": "",
            "run_in_background": "",
            "timeout": 1,
            "disable_timeout": "",
        },
        "Read": {"path": "", "line_offset": 1, "n_lines": 1},
        "Grep": {
            "pattern": "",
            "path": "",
            "glob": "",
            "type": "",
            "offset": 1,
            "head_limit": 1,
            "-n": "",
            "include_ignored": "",
        },
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert [c.name for c in calls] == ["Bash", "Grep"]
    first = json.loads(calls[0].arguments)
    assert first["command"] == "cd /opt/app && ./run.sh"
    assert first["timeout"] == 120
    assert "path" not in first
    second = json.loads(calls[1].arguments)
    assert second["path"] == "/opt/app/output"
    assert second["glob"] == "**/*.py"
    assert second["-n"] is True


def test_wrapped_key_tool_calls_with_space():
    """DeepSeek emits a JSON wrapper with the key 'tool calls' (space instead of
    underscore). _WRAPPED_CALL_KEYS must include this variant.
    """
    text = json.dumps({"tool calls": [{"name": "Read", "arguments": {"path": "/x/y.md"}}]})
    parsed = parse_tool_calls(text, None)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    args = json.loads(calls[0].arguments)
    assert args["path"] == "/x/y.md"


def test_dsml_shared_close_fused_params():
    """DeepSeek shared-close layout: multiple params share one closing tag, and the
    close of one param doubles as the open of the next (fused close-open).

    Pattern from tooltrace f0fddb83297c. _DSML_PARAM_OPENER_RE must match params
    in the fused position so that bounded re-extraction finds all openers.
    """
    S = "\uff5c\uff5c"  # DSML separator
    text = (
        f'<{S}DSML{S}invoke name="Grep">'
        f'<{S}DSML{S}parameter name="pattern" string="true">foo</{S}DSML{S}>'
        f'<{S}DSML{S}parameter name="path" string="true">/tmp</{S}DSML{S}>'
        f'<{S}DSML{S}parameter name="-n" string="false">true'
        f'</{S}DSML{S}parameter name="-C" string="false">8'
        f'</{S}DSML{S}parameter name="head_limit" string="false">100'
        f"</{S}DSML{S}>"
        f"</{S}DSML{S}invoke>"
    )
    schemas = {
        "Grep": {
            "pattern": "string",
            "path": "string",
            "-n": "boolean",
            "-C": "integer",
            "head_limit": "integer",
        }
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == "foo"
    assert args["path"] == "/tmp"
    assert args["-n"] is True
    assert args["-C"] == 8
    assert isinstance(args["-C"], int) and not isinstance(args["-C"], bool)
    assert args["head_limit"] == 100
    assert isinstance(args["head_limit"], int) and not isinstance(args["head_limit"], bool)


def test_dsml_shared_close_three_fused_params():
    """Three params fused in a single shared-close chain (tooltrace dc43106db358).

    The -n param's value is 'true' (boolean), followed by another param whose
    opener appears in close-tag position. All must be extracted with correct types.
    """
    S = "\uff5c\uff5c"  # DSML separator
    text = (
        f'<{S}DSML{S}invoke name="Grep">'
        f'<{S}DSML{S}parameter name="pattern" string="true">bar</{S}DSML{S}>'
        f'<{S}DSML{S}parameter name="-n" string="false">true'
        f'</{S}DSML{S}parameter name="head_limit" string="false">120'
        f"</{S}DSML{S}>"
        f"</{S}DSML{S}invoke>"
    )
    schemas = {
        "Grep": {
            "pattern": "string",
            "-n": "boolean",
            "head_limit": "integer",
        }
    }
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args["pattern"] == "bar"
    assert args["-n"] is True
    assert args["head_limit"] == 120
    assert isinstance(args["head_limit"], int) and not isinstance(args["head_limit"], bool)


def test_dsml_glued_invoke_marker_before_gt_is_repaired():
    """An invoke opener whose name is followed by a stray DSML marker before '>'.

    tooltrace 797cd20e / fa460fef / c24d8828: DeepSeek closes the opener with an
    extra marker (``...name="X" <marker> >``) instead of a bare ``>``, hiding the
    whole block from every DSML branch. The call must still be recovered.
    """
    S = "\uff5c\uff5c"  # DSML separator
    text = (
        f'<{S}DSML{S}invoke name="TodoList"{S}DSML{S}>'
        f'<{S}DSML{S}parameter name="todos" string="false">'
        '[{"title": "t", "status": "done"}]'
        f"</{S}DSML{S}parameter>"
        f"</{S}DSML{S}invoke>"
    )
    parsed = parse_tool_calls(text, {"TodoList": {"todos": "array"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "TodoList"
    args = json.loads(calls[0].arguments)
    assert args["todos"] == [{"title": "t", "status": "done"}]


def test_dsml_bare_arguments_attribute_is_repaired():
    """A bare ``arguments="<escaped json>"`` DSML tag must be decoded into real args.

    tooltrace 797cd20e / f9f81609 / 86931c23: instead of one ``<parameter>`` per
    argument, DeepSeek emits a single tag named ``arguments`` whose quoted value is
    the escaped JSON object. The wrapper is flattened into the real parameters.
    """
    S = "\uff5c\uff5c"  # DSML separator
    inner = '{\\"active_only\\": false, \\"limit\\": 10}'
    text = f'<{S}DSML{S}invoke name="TaskList"{S}DSML{S}><{S}DSML{S}arguments="{inner}"{S}DSML{S}></{S}DSML{S}calls>'
    parsed = parse_tool_calls(text, {"TaskList": {"active_only": "boolean", "limit": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "TaskList"
    args = json.loads(calls[0].arguments)
    assert args == {"active_only": False, "limit": 10}


def test_dsml_bare_arguments_single_line_real_trace():
    """Full shape from the log: glued opener + bare arguments + </calls>."""
    S = "\uff5c\uff5c"  # DSML separator
    inner = '{\\"path\\": \\"/tmp/x.py\\", \\"line_offset\\": 135, \\"n_lines\\": 70}'
    text = f'<{S}DSML{S}calls><{S}DSML{S}invoke name="Read"{S}DSML{S}><{S}DSML{S}arguments="{inner}"{S}DSML{S}></{S}DSML{S}calls>'
    parsed = parse_tool_calls(text, {"Read": {"path": "string", "line_offset": "integer", "n_lines": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Read"
    args = json.loads(calls[0].arguments)
    assert args == {"path": "/tmp/x.py", "line_offset": 135, "n_lines": 70}


def test_dsml_bare_arguments_unescaped_falls_back():
    """A bare arguments value that is not valid JSON-escaped still decodes.

    When the quoted attribute cannot be wrapped as a JSON string (embedded
    unescaped quotes), _repair_dsml_bare_arguments falls back to a plain
    backslash-unescape instead of dropping the call.
    """
    S = "\uff5c\uff5c"  # DSML separator
    inner = '{"path": "/a/b.py", "n": 1}'
    text = f"<{S}DSML{S}invoke name=\"Read\"{S}DSML{S}><{S}DSML{S}arguments='{inner}'{S}DSML{S}></{S}DSML{S}invoke>"
    parsed = parse_tool_calls(text, {"Read": {"path": "string", "n": "integer"}})
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    args = json.loads(calls[0].arguments)
    assert args == {"path": "/a/b.py", "n": 1}


def test_dsml_bare_attributes_recovered():
    """Bare ``key="value"`` DSML attributes (no ``parameter name=``) are recovered.

    tooltrace 7e2f6788 / 64cd8579: DeepSeek collapses each argument into a bare
    attribute on its own tag, either bare or fused behind a name= prefix. The call
    used to parse with EMPTY arguments, the client rejected it as missing required
    properties, and the repair loop re-asked forever. Both spellings must decode.
    """
    S = "\uff5c\uff5c"  # DSML separator
    text = (
        f"<{S}DSML{S}calls>"
        f'<{S}DSML{S}invoke name="Edit">'
        f'<{S}DSML{S}new_string="NEW</{S}DSML{S}>'
        f'<{S}DSML{S}parameter name="old_string="OLD</{S}DSML{S}>'
        f'<{S}DSML{S}path="src/main.py"{S}DSML{S}>'
        f"</{S}DSML{S}invoke>"
        f"</{S}DSML{S}calls>"
    )
    schemas = {"Edit": {"path": "string", "old_string": "string", "new_string": "string"}}
    parsed = parse_tool_calls(text, schemas)
    assert parsed is not None
    calls, _ = parsed
    assert len(calls) == 1
    assert calls[0].name == "Edit"
    args = json.loads(calls[0].arguments)
    assert args == {"new_string": "NEW", "old_string": "OLD", "path": "src/main.py"}


def test_degenerate_repeat_normal_multi_call_not_flagged():
    """A legitimate multi-call answer interleaves bodies; it must never trip."""
    S = "\uff5c\uff5c"
    text = (
        f"<{S}DSML{S} calls>"
        f'<{S}DSML{S} invoke name="Read">'
        f'<{S}DSML{S} parameter name="path" string="true">/a</{S}DSML{S} parameter>'
        f"</{S}DSML{S} invoke></{S}DSML{S} calls>"
        f"<{S}DSML{S} calls>"
        f'<{S}DSML{S} invoke name="Read">'
        f'<{S}DSML{S} parameter name="path" string="true">/b</{S}DSML{S} parameter>'
        f"</{S}DSML{S} invoke></{S}DSML{S} calls>"
    )
    assert degenerate_repeat_count(text) == 0
    assert is_degenerate_repeat(text) is False


def test_degenerate_repeat_few_empty_containers_not_flagged():
    """A handful of empty containers is below threshold and stays valid."""
    S = "\uff5c\uff5c"
    text = (f"<{S}DSML{S} calls>") * 5
    assert degenerate_repeat_count(text) == 5
    assert is_degenerate_repeat(text) is False


def test_degenerate_repeat_runaway_flagged():
    """A long adjacent run of empty containers is the runaway degeneration."""
    S = "\uff5c\uff5c"
    text = (f"<{S}DSML{S} calls>\n") * 40
    assert degenerate_repeat_count(text) == 40
    assert is_degenerate_repeat(text) is True


def test_degenerate_repeat_custom_threshold():
    S = "\uff5c\uff5c"
    text = (f"<{S}DSML{S} calls>\n") * 6
    assert is_degenerate_repeat(text) is False
    assert is_degenerate_repeat(text, threshold=5) is True


def test_degenerate_repeat_plain_prose_zero():
    assert degenerate_repeat_count("just a normal answer") == 0
    assert is_degenerate_repeat("") is False


def test_extract_system_skips_ephemeral_reminder():
    """A client-injected <system-reminder> must not become the system prompt."""
    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="x"),
        Message(role="system", content="<system-reminder>\nToday is 2026-09-23.\n</system-reminder>"),
    ]
    assert extract_system(messages) == "You are helpful."


def test_has_history_ignores_user_role_reminder():
    """A single real user turn plus a reminder is still not 'history'."""
    reminder = "<system-reminder>\nThe TodoList tool has not been updated recently.\n</system-reminder>"
    messages = [
        Message(role="user", content="hello"),
        Message(role="user", content=reminder),
    ]
    assert not _has_history(messages)


def test_has_history_counts_real_user_turns_only():
    reminder = "<system-reminder>\nnoise\n</system-reminder>"
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="second"),
        Message(role="user", content=reminder),
    ]
    assert _has_history(messages)


def test_compact_ignores_user_role_reminder():
    """A trailing reminder must not replace the real last user turn."""
    from danyapi.tools import build_compact_prompt

    reminder = "<system-reminder>\nThe TodoList tool has not been updated recently.\n</system-reminder>"
    msgs = [
        Message(role="user", content="first"),
        Message(role="assistant", content="one"),
        Message(role="user", content="second"),
        Message(role="user", content=reminder),
        Message(role="assistant", content="two"),
    ]
    _, body = build_compact_prompt(msgs)
    assert "User: second" in body
    assert "system-reminder" not in body


def test_build_prompt_history_skips_reminder_in_rendered_context():
    """On the new-chat history path a reminder must not appear as User: text."""
    reminder = "<system-reminder>\nThe TodoList tool has not been updated recently.\n</system-reminder>"
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="a1"),
        Message(role="user", content=reminder),
        Message(role="user", content="second"),
        Message(role="assistant", content="a2"),
    ]
    prompt, _ = build_prompt(messages)
    assert "User: second" in prompt
    assert "system-reminder" not in prompt


def test_dsml_bracketless_close_keeps_every_argument():
    """tooltrace eb14e2aa: a close tag lost BOTH its opening bracket and its slash,
    so it read as a plain opener and the previous parameter swallowed the rest of
    the call as its value. Every argument must survive the repair."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f'<{S} invoke name="Grep">\n'
        f'<{S} parameter name="-C" string="false">8</{S} parameter>'
        f'<{S} parameter name="output_mode" string="true">content'
        f'<{S} parameter name="path" string="true">/a/b.py</{S} parameter>'
        f'<{S} parameter name="pattern" string="true">def main</{S} parameter>\n'
        f"</{S} invoke>"
    )
    calls = parse_tool_calls(text)
    assert calls is not None
    args = json.loads(calls[0][0].arguments)
    assert args == {"-C": 8, "output_mode": "content", "path": "/a/b.py", "pattern": "def main"}


def test_dsml_bracketless_close_leaves_canonical_tags_alone():
    """A canonical opener and a canonical close must not be rewritten."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    canonical = f'<{S} parameter name="path" string="true">/a.py</{S} parameter>'
    assert _repair_dsml_bracketless_close(canonical) == canonical
    # A fused close (carrying a slash) belongs to _repair_dsml_fused_close_open.
    fused = f'</{S} parameter name="next">'
    assert _repair_dsml_bracketless_close(fused) == fused


def test_dsml_bracketless_close_restores_close_and_opener():
    """The repaired token must be a canonical close plus a canonical opener."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    broken = f'value{S} parameter name="x" string="false">'
    assert _repair_dsml_bracketless_close(broken) == (f'value</{S} parameter><{S} parameter name="x" string="false">')


def test_dsml_bracketless_close_bare_restores_plain_close():
    """tooltrace aa405112: the close lost ``<``, ``/`` AND the ``name=`` attribute.
    Only a plain close comes back — restoring an opener here would invent a parameter."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    assert _repair_dsml_bracketless_close(f"value{S} parameter>") == f"value</{S} parameter>"


def test_dsml_bare_close_without_name_keeps_every_argument():
    """tooltrace aa405112, 1139becd, d4ccafca: a nameless bare close read as garbage
    glued to the previous value, so Edit lost old_string and the repair loop repeated
    the same shape."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} tool_calls>\n"
        f'<{S} invoke name="Edit">\n'
        f'<{S} parameter name="path" string="true">/a/b.rs{S} parameter>\n'
        f'<{S} parameter name="old_string" string="true">old{S} parameter>\n'
        f'<{S} parameter name="new_string" string="true">new{S} parameter>\n'
        f"</{S} invoke>\n"
        f"</{S} tool_calls>"
    )
    calls = parse_tool_calls(text)
    assert calls is not None
    assert json.loads(calls[0][0].arguments) == {"path": "/a/b.rs", "old_string": "old", "new_string": "new"}


def test_dsml_lost_close_does_not_turn_timeout_into_false():
    """tooltrace af2a57f0, 2d3bf5b8: with the close broken, the string-value regex
    matched the opener prefix, ``string="false"`` was coerced to False, and the client
    rejected the call as ``Bash.timeout=False expected integer``."""
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    text = (
        f"<{S} tool_calls>\n"
        f'<{S} invoke name="Bash">\n'
        f'<{S} parameter name="command" string="true">echo hi</{S} parameter>\n'
        f'<{S} parameter name="timeout" string="false">300{S} parameter>\n'
        f"</{S} invoke>\n"
        f"</{S} tool_calls>"
    )
    calls = parse_tool_calls(text)
    assert calls is not None
    assert json.loads(calls[0][0].arguments) == {"command": "echo hi", "timeout": 300}
