import json

from danyapi.tooltrace import classify

SCHEMAS = {
    "get_weather": {"city": "string", "line_offset": "integer"},
    "write_todos": {"todos": "array"},
    "Read": {"path": "string", "line_offset": "integer", "n_lines": "integer"},
    "Grep": {"pattern": "string"},
    "TodoList": {"todos": "array"},
}


def test_valid_call_is_ok():
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_extra_argument_detected():
    # DeepSeek adds a spurious 'offset' key not in the schema.
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow", "offset": 5}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "extra_argument"
    assert "offset" in (detail or "")


def test_wrong_type_detected():
    # line_offset should be integer but model returns a float.
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": {"city": "M", "line_offset": 1.5}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "wrong_type"
    assert "line_offset" in (detail or "")


def test_unknown_tool_detected():
    content = json.dumps({"tool_calls": [{"name": "fly_to_moon", "arguments": {}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "unknown_tool"
    assert "fly_to_moon" in (detail or "")


def test_no_tool_call_prose():
    content = "Sure! Let me check the weather for you."
    issue, _ = classify(content, SCHEMAS)
    assert issue == "no_tool_call"


def test_empty_content_is_no_tool_call():
    issue, _ = classify("", SCHEMAS)
    assert issue == "no_tool_call"
    issue, _ = classify(None, SCHEMAS)
    assert issue == "no_tool_call"


def test_json_malformed():
    content = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": '  # truncated JSON
    issue, detail = classify(content, SCHEMAS)
    assert issue == "json_malformed"
    assert detail is not None


def test_fenced_json_still_classified():
    content = "```json\n" + json.dumps({"tool_calls": [{"name": "get_weather", "arguments": {"city": "M"}}]}) + "\n```"
    issue, _ = classify(content, SCHEMAS)
    assert issue == "ok"


def test_arguments_as_json_string():
    content = json.dumps(
        {
            "tool_calls": [
                {
                    "name": "get_weather",
                    "arguments": '{"city": "M", "offset": 9}',
                }
            ]
        }
    )
    issue, _detail = classify(content, SCHEMAS)
    assert issue == "extra_argument"


def test_multiple_calls_priority_unknown_tool_first():
    calls = [
        {"name": "get_weather", "arguments": {"city": "M", "offset": 1}},
        {"name": "ghost_tool", "arguments": {}},
    ]
    content = json.dumps({"tool_calls": calls})
    issue, _ = classify(content, SCHEMAS)
    assert issue == "unknown_tool"


def test_no_schemas_means_ok_for_any_call():
    # Without schemas we can only detect malformed JSON / prose.
    content = json.dumps({"tool_calls": [{"name": "anything", "arguments": {"foo": 1}}]})
    issue, _ = classify(content, None)
    assert issue == "ok"


def test_wrong_type_todos_not_array():
    content = json.dumps({"tool_calls": [{"name": "write_todos", "arguments": {"todos": "not-a-list"}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "wrong_type"
    assert "todos" in (detail or "")


def test_xml_invoke_not_json_malformed():
    """XML invoke format is a valid non-JSON tool-call path — not malformed."""
    content = '<invoke name="Bash">\n<parameter name="command">grep -rn "#\\[test\\]" crates</parameter>\n</invoke>'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_dsml_not_json_malformed():
    """DSML-tagged tool calls are a valid non-JSON format — not malformed."""
    content = '<||DSML||invoke name="Read">\n<||DSML||parameter name="path">/a/b.md</||DSML||parameter>\n</||DSML||invoke>'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_dsml_fullwidth_pipes_not_json_malformed():
    """DeepSeek uses full-width pipes (U+FF5C) in DSML tags — also not malformed."""
    fw = "\uff5c\uff5c"  # two full-width vertical bars, as DeepSeek emits
    content = f'<{fw}DSML{fw}invoke name="Grep">\n<{fw}DSML{fw}parameter name="pattern" string="true">foo</{fw}DSML{fw}parameter>\n</{fw}DSML{fw}invoke>'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_mangled_json_still_detected():
    """Truncated JSON with tool_calls marker still flagged as malformed."""
    content = '{"tool_calls": [{"name": "get_weather", "arguments": {"city":'
    issue, _ = classify(content, SCHEMAS)
    assert issue == "json_malformed"


def test_prose_wrapped_call_is_ok_not_malformed():
    """A call preceded by prose fails a bare json.loads but the parser recovers it.

    Regression: the classifier used to report json_malformed for any non-pure-JSON
    payload, even though parse_tool_calls handles prose wrapping a call. That
    inflated the mangle rate in tooltrace.ndjson by an order of magnitude.
    """
    content = 'Let me check.\n\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]}'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_several_calls_in_a_row_are_ok_not_malformed():
    """Multiple JSON objects back to back are recovered by the parser, not malformed."""
    content = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "M"}}]}\n\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "P"}}]}'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_unrecoverable_call_text_still_malformed():
    """Text the parser cannot turn into a call keeps the json_malformed verdict."""
    content = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": '
    issue, detail = classify(content, SCHEMAS)
    assert issue == "json_malformed"
    assert detail is not None


def test_dsml_read_file_path_is_extra_argument():
    """Real incident: DeepSeek emits ``file_path`` instead of ``path`` for Read."""
    fw = "\uff5c\uff5c"
    content = f'<{fw}DSML{fw}invoke name="Read">\n<{fw}DSML{fw}parameter name="file_path" string="true">/a/b.md</{fw}DSML{fw}parameter>\n</{fw}DSML{fw}invoke>'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "extra_argument"
    assert "file_path" in (detail or "")


def test_dsml_read_offset_and_limit_are_extra_arguments():
    """Real incident: DeepSeek emits ``offset``/``limit`` instead of ``line_offset``/``n_lines``."""
    fw = "\uff5c\uff5c"
    content = (
        f'<{fw}DSML{fw}invoke name="Read">\n'
        f'<{fw}DSML{fw}parameter name="path" string="true">/a/b.md</{fw}DSML{fw}parameter>\n'
        f'<{fw}DSML{fw}parameter name="offset" string="false">245</{fw}DSML{fw}parameter>\n'
        f'<{fw}DSML{fw}parameter name="limit" string="false">12</{fw}DSML{fw}parameter>\n'
        f"</{fw}DSML{fw}invoke>"
    )
    issue, detail = classify(content, SCHEMAS)
    assert issue == "extra_argument"
    assert "offset" in (detail or "") or "limit" in (detail or "")


def test_dsml_todo_list_mangled_fields():
    """Real incident: DeepSeek sends ``content``+``completed`` instead of ``title``+``done``."""
    fw = "\uff5c\uff5c"
    content = (
        f'<{fw}DSML{fw}invoke name="TodoList">\n'
        f'<{fw}DSML{fw}parameter name="content" string="true">Remove .changeset</{fw}DSML{fw}parameter>\n'
        f'<{fw}DSML{fw}parameter name="completed" string="false">true</{fw}DSML{fw}parameter>\n'
        f"</{fw}DSML{fw}invoke>"
    )
    issue, detail = classify(content, SCHEMAS)
    assert issue == "extra_argument"
    assert "content" in (detail or "") or "completed" in (detail or "")
