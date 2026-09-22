import json
from pathlib import Path

from danyapi.tooltrace import ToolTraceLogger


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_noop_when_disabled(tmp_path):
    logger = ToolTraceLogger("")  # empty path => disabled
    assert logger.enabled is False
    target = tmp_path / "should_not_exist.ndjson"
    logger.client("req-1", model="m", tools=[{"function": {"name": "get_weather"}}])
    logger.dany("req-1", prompt="hello")
    logger.upstream("req-1", content='{"tool_calls": []}')
    logger.parse("req-1", result="ok")
    assert not target.exists()


def test_writes_ndjson_events(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    assert logger.enabled is True

    logger.client(
        "req-abc",
        session_id="sess-1",
        model="deepseek-v4-flash",
        tools=[{"function": {"name": "get_weather"}}, {"function": {"name": "search"}}],
        tool_choice="auto",
        messages_count=3,
    )
    logger.dany("req-abc", prompt="the final prompt text", session_id="sess-1", model="deepseek-v4-flash")
    logger.upstream("req-abc", content='{"tool_calls": [{"name": "get_weather"}]}', reasoning="thinking...", session_id="sess-1", model="deepseek-v4-flash")
    logger.parse("req-abc", result="ok", issue=None, session_id="sess-1", model="deepseek-v4-flash")

    lines = _read_lines(path)
    assert len(lines) == 4
    actors = [line["actor"] for line in lines]
    assert actors == ["client", "dany", "upstream", "parse"]
    # All four events share the same request_id.
    assert all(line["request_id"] == "req-abc" for line in lines)

    client = lines[0]
    assert client["tool_names"] == ["get_weather", "search"]
    assert client["messages_count"] == 3
    assert client["session_id"] == "sess-1"

    dany = lines[1]
    assert dany["prompt"] == "the final prompt text"

    upstream = lines[2]
    assert "get_weather" in upstream["content"]
    assert upstream["reasoning"] == "thinking..."

    parse = lines[3]
    assert parse["result"] == "ok"


def test_continuation_event(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.continuation(
        "req-abc",
        session_id="sess-1",
        model="deepseek-v4-flash",
        status_code=502,
        detail="DeepSeek request failed: upstream timeout",
        attempt=3,
    )

    lines = _read_lines(path)
    assert len(lines) == 1
    event = lines[0]
    assert event["actor"] == "continuation"
    assert event["request_id"] == "req-abc"
    assert event["session_id"] == "sess-1"
    assert event["model"] == "deepseek-v4-flash"
    assert event["status_code"] == 502
    assert event["attempt"] == 3
    assert event["detail"] == "DeepSeek request failed: upstream timeout"


def test_continuation_detail_truncated(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.continuation("req-1", status_code=502, detail="z" * 3000, attempt=1)
    lines = _read_lines(path)
    assert len(lines[0]["detail"]) == 1000


def test_truncates_long_content(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    long_content = "x" * 6000
    logger.upstream("req-1", content=long_content)
    lines = _read_lines(path)
    assert len(lines[0]["content"]) == 5000


def test_truncates_long_prompt(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    logger.dany("req-1", prompt="y" * 9000)
    lines = _read_lines(path)
    assert len(lines[0]["prompt"]) == 4000


def test_rotation_by_size(tmp_path):
    path = tmp_path / "trace.ndjson"
    # Tiny max_bytes so a single event triggers rotation.
    logger = ToolTraceLogger(path, max_bytes=120, backup_count=3)
    for i in range(6):
        logger.upstream(f"req-{i}", content="z" * 80)
    assert path.exists()
    rotated = [p.name for p in tmp_path.iterdir() if "trace.ndjson." in p.name]
    assert len(rotated) >= 1


def test_correlation_by_request_id(tmp_path):
    """Two distinct requests produce two disjoint event groups."""
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    for rid in ("req-A", "req-B"):
        logger.client(rid, model="m")
        logger.dany(rid, prompt=f"prompt-{rid}")
        logger.upstream(rid, content="content")
        logger.parse(rid, result="ok")

    lines = _read_lines(path)
    assert len(lines) == 8
    by_id: dict[str, list] = {}
    for line in lines:
        by_id.setdefault(line["request_id"], []).append(line["actor"])
    assert set(by_id.keys()) == {"req-A", "req-B"}
    for actors in by_id.values():
        assert actors == ["client", "dany", "upstream", "parse"]


def test_tool_names_extraction(tmp_path):
    path = tmp_path / "trace.ndjson"
    logger = ToolTraceLogger(path)
    mixed_tools = [
        {"function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
        {"name": "c"},  # bare form
        {"no_name_key": True},
        "not-a-dict",
    ]
    logger.client("req-1", tools=mixed_tools)
    lines = _read_lines(path)
    assert lines[0]["tool_names"] == ["a", "b", "c"]
