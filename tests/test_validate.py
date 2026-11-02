import json

from danyapi.tools.prompt import ToolCall
from danyapi.tools.validate import (
    ValidationIssue,
    build_repair_prompt,
    should_retry_unparsed,
    summarize,
    type_matches,
    validate_call,
    validate_calls,
)

SCHEMAS = {
    "get_weather": {"city": "string", "line_offset": "integer", "__required__": {"city"}},
    "write_todos": {"todos": "array", "__required__": set()},
    "Grep": {"pattern": "string", "__required__": {"pattern"}},
}


def _call(name: str, args) -> ToolCall:
    return ToolCall.create(name, args)


def test_type_matches_primitives():
    assert type_matches("x", "string")
    assert type_matches(3, "integer")
    assert type_matches(3.5, "number")
    assert type_matches(3, "number")
    assert type_matches(True, "boolean")
    assert type_matches([], "array")
    assert type_matches({}, "object")
    assert type_matches(True, "integer") is False
    assert type_matches(3, "boolean") is False
    assert type_matches("x", "no_such_type")
    assert type_matches("x", None)


def test_valid_call_has_no_issues():
    call = _call("get_weather", {"city": "Moscow"})
    assert validate_call(call, SCHEMAS["get_weather"]) == []


def test_missing_required_detected():
    call = _call("get_weather", {"line_offset": 1})
    issues = validate_call(call, SCHEMAS["get_weather"])
    assert [i.kind for i in issues] == ["missing_required"]
    assert "city" in issues[0].detail


def test_extra_argument_detected():
    call = _call("get_weather", {"city": "M", "offset": 5})
    issues = validate_call(call, SCHEMAS["get_weather"])
    assert [i.kind for i in issues] == ["extra_argument"]
    assert "offset" in issues[0].detail


def test_wrong_type_detected():
    call = _call("get_weather", {"city": "M", "line_offset": 1.5})
    issues = validate_call(call, SCHEMAS["get_weather"])
    assert [i.kind for i in issues] == ["wrong_type"]
    assert "line_offset" in issues[0].detail


def test_required_key_not_treated_as_allowed_property():
    # ``__required__`` is a reserved key and must not be reported as extra.
    call = _call("Grep", {"pattern": "x"})
    issues = validate_call(call, SCHEMAS["Grep"])
    assert issues == []


def test_arguments_as_json_string_is_decoded():
    call = ToolCall("call_1", "get_weather", json.dumps({"city": "M"}))
    assert validate_call(call, SCHEMAS["get_weather"]) == []


def test_malformed_arguments_reported():
    call = ToolCall("call_1", "get_weather", '{"city": ')
    issues = validate_call(call, SCHEMAS["get_weather"])
    assert [i.kind for i in issues] == ["json_malformed"]


def test_non_object_arguments_reported_as_wrong_type():
    call = ToolCall("call_1", "get_weather", "[1, 2]")
    issues = validate_call(call, SCHEMAS["get_weather"])
    assert [i.kind for i in issues] == ["wrong_type"]


def test_unknown_tool_when_schemas_present():
    call = _call("fly_to_moon", {})
    issues = validate_calls([call], SCHEMAS)
    assert [i.kind for i in issues] == ["unknown_tool"]


def test_no_schemas_skips_unknown_tool():
    call = _call("anything", {"foo": 1})
    assert validate_calls([call], None) == []


def test_no_schemas_still_flags_malformed():
    call = ToolCall("call_1", "anything", '{"foo": ')
    issues = validate_calls([call], None)
    assert [i.kind for i in issues] == ["json_malformed"]


def test_non_dict_non_call_entries_skipped():
    assert validate_calls([1, "x", None], SCHEMAS) == []


def test_dict_call_supported():
    issues = validate_calls([{"name": "get_weather", "arguments": {"city": "M"}}], SCHEMAS)
    assert issues == []


def test_multiple_issues_priority_unknown_tool_first():
    calls = [
        _call("get_weather", {"city": "M", "offset": 1}),
        _call("ghost_tool", {}),
    ]
    issue, _detail = summarize(validate_calls(calls, SCHEMAS))
    assert issue == "unknown_tool"


def test_summarize_priority_missing_required_over_extra():
    calls = [_call("get_weather", {"offset": 1})]
    issue, _detail = summarize(validate_calls(calls, SCHEMAS))
    assert issue == "missing_required"


def test_summarize_ok_and_detail_none():
    assert summarize([]) == ("ok", None)


def test_summarize_detail_joins_at_most_five():
    args = {"city": "M", **{f"k{i}": i for i in range(8)}}
    call = _call("get_weather", args)
    issue, detail = summarize(validate_calls([call], SCHEMAS))
    assert issue == "extra_argument"
    assert detail is not None
    assert detail.count(";") == 4


def test_issue_dataclass_fields():
    issue = ValidationIssue("wrong_type", "Grep.pattern")
    assert issue.kind == "wrong_type"
    assert issue.detail == "Grep.pattern"


def test_build_repair_prompt_lists_issues_and_demands_dsml():
    issues = [ValidationIssue("extra_argument", "get_weather.offset")]
    prompt = build_repair_prompt(issues)
    assert "get_weather.offset" in prompt
    assert "tool_calls" in prompt
    assert "DSML" in prompt
    # The repair must ask for the same format the schema block asks for. A JSON
    # demand here contradicts it and brings back the hybrid replies.
    assert "corrected JSON" not in prompt
    assert "no XML/DSML" not in prompt


def test_build_repair_prompt_caps_listed_issues():
    issues = [ValidationIssue("extra_argument", f"t.k{i}") for i in range(8)]
    prompt = build_repair_prompt(issues)
    assert "t.k4" in prompt
    assert "t.k5" not in prompt


# --- should_retry_unparsed: gate for the retry-on-unparsed repair ------------


def test_should_retry_unparsed_forced_choice_always_retries():
    # tool_choice forces a call, so an unparsed reply is a mangle, not prose.
    assert should_retry_unparsed("required", "no_tool_call") is True
    assert should_retry_unparsed("required", None) is True
    assert should_retry_unparsed("get_weather", "no_tool_call") is True
    assert should_retry_unparsed({"type": "function", "function": {"name": "get_weather"}}, "no_tool_call") is True


def test_should_retry_unparsed_none_choice_never_retries():
    # tool_choice forbids tools, so an unparsed reply is a normal text answer.
    assert should_retry_unparsed("none", "json_malformed", retry_auto=True) is False
    assert should_retry_unparsed("none", "no_tool_call") is False


def test_should_retry_unparsed_auto_off_by_default():
    # Auto (and unset) choices must not re-send a normal answer upstream.
    assert should_retry_unparsed("auto", "json_malformed") is False
    assert should_retry_unparsed(None, "json_malformed") is False
    assert should_retry_unparsed("", "json_malformed") is False


def test_should_retry_unparsed_auto_retries_only_malformed():
    # With the opt-in flag, only a call that looked mangled is retried; ordinary
    # prose (no_tool_call) never is.
    assert should_retry_unparsed("auto", "json_malformed", retry_auto=True) is True
    assert should_retry_unparsed("auto", "no_tool_call", retry_auto=True) is False
    assert should_retry_unparsed("auto", None, retry_auto=True) is False
    assert should_retry_unparsed(None, "json_malformed", retry_auto=True) is True


def test_should_retry_unparsed_non_string_choice_is_auto():
    # A malformed tool_choice object without a function name is treated as auto.
    assert should_retry_unparsed({"type": "function"}, "json_malformed") is False
    assert should_retry_unparsed(123, "json_malformed", retry_auto=True) is True


# --- build_unparsed_repair_prompt: parse-fail variant of the repair prompt ----


def test_build_unparsed_repair_prompt_demands_dsml():
    from danyapi.tools.validate import build_unparsed_repair_prompt

    prompt = build_unparsed_repair_prompt("json_malformed")
    assert "could not be parsed" in prompt
    assert "json_malformed" in prompt
    assert "DSML" in prompt
    # A JSON demand would contradict the schema block's DSML instruction.
    assert "ONLY the JSON object" not in prompt
    assert "no XML/DSML" not in prompt
    # The model's broken reply is still in the session, so no issue list is sent.
    assert "did not pass tool-call validation" not in prompt


def test_build_unparsed_repair_prompt_without_issue():
    from danyapi.tools.validate import build_unparsed_repair_prompt

    prompt = build_unparsed_repair_prompt(None)
    assert "could not be parsed" in prompt
    assert "diagnosis" not in prompt
