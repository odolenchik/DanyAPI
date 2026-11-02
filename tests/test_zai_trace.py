import pytest

import danyapi.zai.api as zai_api
from danyapi.tooltrace import ToolTraceLogger
from danyapi.zai.browser import _CompletionAccumulator

# Shape produced by danyapi.tools.parse.tool_schema_map: parameter name -> type,
# plus the required set under __required__.
TOOL_SCHEMAS = {"Read": {"path": "string", "__required__": {"path"}}}
CLEAN_CALL = '{"tool_calls": [{"name": "Read", "arguments": {"path": "/tmp/x"}}]}'


class _FakeTrace:
    def __init__(self):
        self.upstream_calls = []
        self.parse_calls = []

    def upstream(self, request_id=None, **kwargs):
        self.upstream_calls.append((request_id, kwargs))

    def parse(self, request_id=None, **kwargs):
        self.parse_calls.append((request_id, kwargs))


class _NonStreamBrowser:
    def __init__(self, content: str):
        self._content = content

    async def complete(self, prompt, model=None):
        acc = _CompletionAccumulator()
        acc.content = self._content
        return acc


class _StreamBrowser:
    def __init__(self, events):
        self._events = events

    async def complete_stream(self, prompt, model=None):
        for event in self._events:
            yield event


async def _collect(gen):
    return [line async for line in gen]


def test_trace_parse_noop_without_logger():
    zai_api._trace_parse(None, "rid", "glm", "text", None, None)


def test_trace_parse_unparsed_json_malformed():
    trace = _FakeTrace()
    zai_api._trace_parse(trace, "rid", "glm", '{"tool_calls": [', None, None)
    request_id, kwargs = trace.parse_calls[0]
    assert request_id == "rid"
    assert kwargs["result"].startswith("unparsed")
    assert kwargs["model"] == "glm"


def test_trace_parse_ok_with_clean_call():
    trace = _FakeTrace()
    parsed = zai_api.toolemu.parse_tool_calls(CLEAN_CALL, None)
    zai_api._trace_parse(trace, "rid", "glm", CLEAN_CALL, None, parsed)
    _request_id, kwargs = trace.parse_calls[0]
    assert kwargs["result"] == "ok"
    assert kwargs["issue"] is None


def test_real_trace_logger_records_parse(tmp_path):
    """The helper must work against the real logger, not only a double."""
    logger = ToolTraceLogger(tmp_path / "t.ndjson")
    zai_api._trace_parse(logger, "rid", "glm", "not a call", None, None)
    lines = (tmp_path / "t.ndjson").read_text().strip().splitlines()
    assert len(lines) == 1
    assert '"actor": "parse"' in lines[0]


@pytest.mark.asyncio
async def test_collect_non_stream_traces_tool_round():
    trace = _FakeTrace()
    result = await zai_api.collect_non_stream(
        browser=_NonStreamBrowser(CLEAN_CALL),
        prompt="hi",
        model="glm-5.3",
        tool_mode=True,
        tool_schemas=TOOL_SCHEMAS,
        request_id="rid",
        trace=trace,
    )
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert trace.upstream_calls[0][0] == "rid"
    assert trace.parse_calls[0][1]["result"] == "ok"


@pytest.mark.asyncio
async def test_stream_openai_traces_tool_round():
    trace = _FakeTrace()
    lines = await _collect(
        zai_api.stream_openai(
            browser=_StreamBrowser([{"phase": "answer", "delta_content": CLEAN_CALL}]),
            prompt="hi",
            model="glm-5.3",
            tool_mode=True,
            tool_schemas=TOOL_SCHEMAS,
            request_id="rid",
            trace=trace,
        )
    )
    assert lines[-1] == "data: [DONE]\n\n"
    assert trace.upstream_calls[0][0] == "rid"
    assert trace.parse_calls[0][1]["result"] == "ok"
