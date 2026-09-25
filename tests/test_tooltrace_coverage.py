"""Coverage tests for danyapi/tooltrace.py defensive/degenerate branches.

Complements tests/test_tooltrace.py (logger events) and tests/test_classify.py
(classification happy paths) by exercising the edges those two do not reach:
non-string truncation inputs, unwritable sinks, JSON payloads whose shape is
off-spec, and schema types other than string/integer.
"""

import json

from danyapi.tooltrace import ToolTraceLogger, classify

SCHEMAS = {
    "get_weather": {"city": "string", "line_offset": "integer"},
    "typed": {"num": "number", "flag": "boolean", "mystery": "no_such_type"},
}


def _read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_truncate_none_returns_none(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.dany("req-1", prompt=None)
    lines = _read_lines(path)
    assert lines[0]["prompt"] is None


def test_truncate_non_string_value_is_json_encoded(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.dany("req-1", prompt={"nested": ["a", "b"]})
    lines = _read_lines(path)
    assert json.loads(lines[0]["prompt"]) == {"nested": ["a", "b"]}


def test_truncate_unserializable_value_falls_back_to_repr(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.dany("req-1", prompt={1, 2})
    lines = _read_lines(path)
    # A set is not JSON-serializable, so the repr path is used.
    assert "{" in lines[0]["prompt"]


def test_write_line_swallows_unserializable_record(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.client("req-1", messages_count=object())
    assert not path.exists()


def test_write_line_swallows_oserror(tmp_path):
    # A directory can never be opened for append, so the OSError is swallowed.
    logger = ToolTraceLogger(tmp_path, backup_count=0)
    logger.dany("req-1", prompt="hello")
    assert tmp_path.is_dir()


def test_type_matches_number_boolean_and_unknown_type():
    args = {"num": 1.5, "flag": True, "mystery": "x"}
    content = json.dumps({"tool_calls": [{"name": "typed", "arguments": args}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_type_matches_number_rejects_bool():
    content = json.dumps({"tool_calls": [{"name": "typed", "arguments": {"num": True}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "wrong_type"
    assert "num" in (detail or "")


def test_type_matches_boolean_rejects_string():
    content = json.dumps({"tool_calls": [{"name": "typed", "arguments": {"flag": "yes"}}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "wrong_type"
    assert "flag" in (detail or "")


def test_bare_call_object_without_wrapper():
    # No tool_calls/calls wrapper: a lone name+arguments object is still a call.
    content = json.dumps({"name": "get_weather", "arguments": {"city": "Moscow"}})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_json_scalar_is_no_tool_call():
    issue, detail = classify(json.dumps(123), SCHEMAS)
    assert issue == "no_tool_call"
    assert detail is not None


def test_wrapper_key_with_non_list_value_is_no_tool_call():
    issue, _ = classify(json.dumps({"tool_calls": "not-a-list"}), SCHEMAS)
    assert issue == "no_tool_call"


def test_check_calls_skips_non_dict_entries():
    content = json.dumps({"tool_calls": ["not-a-dict"]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_arguments_string_is_not_json():
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": "{not json"}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "json_malformed"
    assert "get_weather" in (detail or "")


def test_arguments_string_parses_to_non_object():
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": "[1, 2]"}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "wrong_type"
    assert "arguments not an object" in (detail or "")


def test_arguments_neither_string_nor_object():
    content = json.dumps({"tool_calls": [{"name": "get_weather", "arguments": 42}]})
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None


def test_classify_dsml_with_non_json_arguments(monkeypatch):
    """A DSML/XML call whose decoded arguments are not valid JSON degrades to {}."""
    from danyapi.tools import parse as parse_mod

    fake_call = type("FakeCall", (), {"name": "get_weather", "arguments": "{not json"})()
    monkeypatch.setattr(parse_mod, "_parse_dsml", lambda text, schemas: ([fake_call], ""))
    content = '<invoke name="get_weather"><parameter name="city">M</parameter></invoke>'
    issue, detail = classify(content, SCHEMAS)
    assert issue == "ok"
    assert detail is None
