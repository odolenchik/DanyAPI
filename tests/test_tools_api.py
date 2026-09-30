import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.api.openai as openai_mod
import danyapi.qwen.api as qwen_api
from danyapi.tooltrace import ToolTraceLogger

TOOL_JSON = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]}'

DS_TOOL_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":['
    '{"id":2,"type":"RESPONSE","content":"{\\"tool_calls\\": [{\\"name\\": \\"get_weather\\""}]}}}\n'
    "\n"
    'data: {"p":"response/fragments/-1/content","o":"APPEND","v":", \\"arguments\\": '
    '{\\"city\\": \\"Moscow\\"}}]}"}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)

DS_PLAIN_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":['
    '{"id":2,"type":"RESPONSE","content":"The weather in Moscow is 22C and sunny."}]}}}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)

DS_XML_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":['
    '{"id":2,"type":"RESPONSE","content":"<tool_calls>\\n<invoke name=\\"bash\\">\\n<command>Get-ChildItem -Name</command>\\n</invoke>\\n</tool_calls>"}]}}}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)

QWEN_TOOL_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": '
    + json.dumps(TOOL_JSON)
    + ', "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)


class FakeSession:
    def __init__(self, sid="c1", last_message_id=None, last_response_id=None):
        self.id = sid
        self.last_message_id = last_message_id
        self.last_response_id = last_response_id


class FakeResp:
    def __init__(self, sse_text):
        self._b = sse_text.encode()
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream; charset=utf-8"}

    async def aiter_bytes(self):
        yield self._b

    async def aclose(self):
        pass

    async def aread(self):
        return self._b


class FakeAccount:
    def __init__(self, sse_list):
        self.index = 0
        self.broken = False
        self.client = MagicMock()
        self.client.completion = AsyncMock(side_effect=[FakeResp(s) for s in sse_list])
        self.client.create_pow_challenge = AsyncMock(return_value={})
        self.pow = MagicMock()
        self.pow.make_header = AsyncMock(return_value={})
        self.pow_upload = MagicMock()
        self.pow_upload.make_header = AsyncMock(return_value={})
        self.sem = asyncio.Semaphore(1)
        self.sessions = MagicMock()
        self.sessions.obtain = AsyncMock(return_value=(FakeSession(), "s1"))
        self.sessions.touch_last_message = MagicMock()
        self.starts = []
        self.waits = []

    async def wait_turn(self):
        self.waits.append(None)

    def note_request_start(self):
        self.starts.append(None)

    def note_request_end(self):
        return None

    def recent_request_count(self, window):
        return len(self.starts)

    @property
    def label(self):
        return f"acct#{self.index}"

    def mark_broken(self):
        self.broken = True


@pytest.fixture(autouse=True)
def zero_backoff():
    orig_ds = openai_mod.RETRY_BACKOFF_SEC
    orig_qwen = qwen_api.RETRY_BACKOFF_SEC
    openai_mod.RETRY_BACKOFF_SEC = 0.0
    qwen_api.RETRY_BACKOFF_SEC = 0.0
    yield
    openai_mod.RETRY_BACKOFF_SEC = orig_ds
    qwen_api.RETRY_BACKOFF_SEC = orig_qwen


async def collect_stream(gen):
    return [line async for line in gen]


def _deepseek_args(acct, tool_mode=True):
    return {
        "account": acct,
        "pool": MagicMock(),
        "existing_sid": "s1",
        "lock": acct.sem,
        "prompt": "x",
        "model": "deepseek-v4-flash",
        "model_type": "default",
        "thinking": False,
        "search": False,
        "tool_mode": tool_mode,
    }


def _qwen_args(acct, tool_mode=True):
    return {
        "account": acct,
        "pool": MagicMock(),
        "existing_sid": "s1",
        "lock": acct.sem,
        "prompt": "x",
        "model": "qwen3.8-max",
        "model_id": "qwen3.8-max",
        "thinking": False,
        "search": False,
        "tool_mode": tool_mode,
    }


async def test_non_stream_formats_tool_calls():
    acct = FakeAccount([DS_TOOL_SSE])
    result = await openai_mod._collect_non_stream(**_deepseek_args(acct))
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert len(message["tool_calls"]) == 1
    call = message["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Moscow"}


async def test_non_stream_falls_back_to_content():
    acct = FakeAccount([DS_PLAIN_SSE])
    result = await openai_mod._collect_non_stream(**_deepseek_args(acct))
    choice = result["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "tool_calls" not in choice["message"]
    assert "22C" in choice["message"]["content"]


async def test_stream_emits_tool_call_deltas():
    acct = FakeAccount([DS_TOOL_SSE])
    gen = openai_mod._stream_openai(**_deepseek_args(acct))
    lines = await collect_stream(gen)
    joined = "".join(lines)
    assert '"tool_calls"' in joined
    assert '"name": "get_weather"' in joined
    assert '"finish_reason": "tool_calls"' in joined
    arguments = ""
    for line in lines:
        if not line.startswith("data: ") or line.startswith("data: [DONE]"):
            continue
        payload = json.loads(line[6:])
        for chunk in payload.get("choices") or []:
            for tc in (chunk.get("delta") or {}).get("tool_calls") or []:
                arguments += tc.get("function", {}).get("arguments", "")
    assert json.loads(arguments) == {"city": "Moscow"}


async def test_stream_falls_back_to_content():
    acct = FakeAccount([DS_PLAIN_SSE])
    gen = openai_mod._stream_openai(**_deepseek_args(acct))
    lines = await collect_stream(gen)
    joined = "".join(lines)
    assert '"tool_calls"' not in joined
    assert '"content": "The weather in Moscow is 22C and sunny."' in joined
    assert '"finish_reason": "stop"' in joined


async def test_qwen_non_stream_formats_tool_calls():
    acct = FakeAccount([QWEN_TOOL_SSE])
    result = await qwen_api.collect_non_stream(**_qwen_args(acct))
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert len(message["tool_calls"]) == 1
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"


async def test_qwen_stream_emits_tool_call_deltas():
    acct = FakeAccount([QWEN_TOOL_SSE])
    gen = qwen_api.stream_openai(**_qwen_args(acct))
    lines = await collect_stream(gen)
    joined = "".join(lines)
    assert '"tool_calls"' in joined
    assert '"finish_reason": "tool_calls"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# Tool-trace wiring: verify the four correlated events fire for both providers,
# non-stream and stream.
# ---------------------------------------------------------------------------


def _trace(tmp_path):
    return ToolTraceLogger(str(tmp_path / "trace.ndjson"))


def _read_events(path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    return [json.loads(rec) for rec in raw.splitlines() if rec.strip()]


async def test_deepseek_non_stream_trace_events(tmp_path):
    acct = FakeAccount([DS_TOOL_SSE])
    trace = _trace(tmp_path)
    args = dict(_deepseek_args(acct), request_id="req-1", trace=trace, tool_schemas={"get_weather": {"city": "string"}})
    result = await openai_mod._collect_non_stream(**args)
    assert result["choices"][0]["finish_reason"] == "tool_calls"

    events = _read_events(tmp_path / "trace.ndjson")
    # dany + upstream + parse (client is logged at the openai.py layer).
    assert [e["actor"] for e in events] == ["dany", "upstream", "parse"]
    assert all(e["request_id"] == "req-1" for e in events)
    assert events[0]["prompt"] == "x"
    assert events[2]["result"].startswith("ok")


async def test_deepseek_stream_trace_events(tmp_path):
    acct = FakeAccount([DS_TOOL_SSE])
    trace = _trace(tmp_path)
    args = dict(_deepseek_args(acct), request_id="req-2", trace=trace, tool_schemas={"get_weather": {"city": "string"}})
    await collect_stream(openai_mod._stream_openai(**args))

    events = _read_events(tmp_path / "trace.ndjson")
    assert [e["actor"] for e in events] == ["dany", "upstream", "parse"]
    assert all(e["request_id"] == "req-2" for e in events)


async def test_qwen_non_stream_trace_events(tmp_path):
    acct = FakeAccount([QWEN_TOOL_SSE])
    trace = _trace(tmp_path)
    args = dict(_qwen_args(acct), request_id="req-3", trace=trace, tool_schemas={"get_weather": {"city": "string"}})
    result = await qwen_api.collect_non_stream(**args)
    assert result["choices"][0]["finish_reason"] == "tool_calls"

    events = _read_events(tmp_path / "trace.ndjson")
    assert [e["actor"] for e in events] == ["dany", "upstream", "parse"]
    assert all(e["request_id"] == "req-3" for e in events)


async def test_qwen_stream_trace_events(tmp_path):
    acct = FakeAccount([QWEN_TOOL_SSE])
    trace = _trace(tmp_path)
    args = dict(_qwen_args(acct), request_id="req-4", trace=trace, tool_schemas={"get_weather": {"city": "string"}})
    await collect_stream(qwen_api.stream_openai(**args))

    events = _read_events(tmp_path / "trace.ndjson")
    assert [e["actor"] for e in events] == ["dany", "upstream", "parse"]
    assert all(e["request_id"] == "req-4" for e in events)


async def test_trace_none_is_noop(tmp_path):
    """When trace is None (default), no file is created and behavior is unchanged."""
    acct = FakeAccount([DS_TOOL_SSE])
    result = await openai_mod._collect_non_stream(**_deepseek_args(acct))
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert not (tmp_path / "trace.ndjson").exists()


async def test_parse_event_flags_mangle(tmp_path):
    """A valid parse that still carries an extra key is flagged in the parse event."""
    mangled_sse = DS_TOOL_SSE.replace('{\\"city\\": \\"Moscow\\"}', '{\\"city\\": \\"Moscow\\", \\"offset\\": 5}')
    acct = FakeAccount([mangled_sse])
    trace = _trace(tmp_path)
    args = dict(_deepseek_args(acct), request_id="req-5", trace=trace, tool_schemas={"get_weather": {"city": "string"}})
    await openai_mod._collect_non_stream(**args)

    events = _read_events(tmp_path / "trace.ndjson")
    parse_event = next(e for e in events if e["actor"] == "parse")
    assert "extra_argument" in parse_event["result"]
    assert "offset" in (parse_event.get("issue") or "")


# ---------------------------------------------------------------------------
# Repair loop: a call that parses but fails schema validation is re-requested
# once; a good retry replaces it, a bad/absent one keeps the original result.
# ---------------------------------------------------------------------------

_WEATHER_SCHEMAS = {"get_weather": {"city": "string", "__required__": {"city"}}}


def _ds_tool_sse(content: str) -> str:
    return (
        "event: ready\n"
        'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
        "\n"
        'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":['
        '{"id":2,"type":"RESPONSE","content":' + json.dumps(content) + "}]}}}\n"
        "\n"
        'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
        "\n"
    )


_MANGLED_TOOL_JSON = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow", "offset": 5}}]}'
_FIXED_TOOL_JSON = '{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Moscow"}}]}'


def _stream_arguments(lines: list[str]) -> str:
    arguments = ""
    for line in lines:
        if not line.startswith("data: ") or line.startswith("data: [DONE]"):
            continue
        payload = json.loads(line[6:])
        for chunk in payload.get("choices") or []:
            for tc in (chunk.get("delta") or {}).get("tool_calls") or []:
                arguments += tc.get("function", {}).get("arguments", "")
    return arguments


async def test_non_stream_repairs_mangled_tool_call():
    acct = FakeAccount([_ds_tool_sse(_MANGLED_TOOL_JSON), _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS)
    result = await openai_mod._collect_non_stream(**args)
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"city": "Moscow"}
    # One repair round-trip = two upstream completions.
    assert acct.client.completion.await_count == 2


async def test_non_stream_keeps_original_when_repair_does_not_help():
    acct = FakeAccount([_ds_tool_sse(_MANGLED_TOOL_JSON), _ds_tool_sse(_MANGLED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS)
    result = await openai_mod._collect_non_stream(**args)
    call = result["choices"][0]["message"]["tool_calls"][0]
    # The imperfect original is preserved rather than dropped.
    assert json.loads(call["function"]["arguments"])["offset"] == 5


async def test_non_stream_keeps_original_when_repair_raises():
    # Only one upstream response is staged; the repair call exhausts the mock and
    # raises, which must not take down a request that already has a call.
    acct = FakeAccount([_ds_tool_sse(_MANGLED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS)
    result = await openai_mod._collect_non_stream(**args)
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"])["offset"] == 5


async def test_stream_repairs_mangled_tool_call():
    acct = FakeAccount([_ds_tool_sse(_MANGLED_TOOL_JSON), _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS)
    lines = await collect_stream(openai_mod._stream_openai(**args))
    assert json.loads(_stream_arguments(lines)) == {"city": "Moscow"}


async def test_valid_call_does_not_trigger_repair():
    acct = FakeAccount([_ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS)
    await openai_mod._collect_non_stream(**args)
    assert acct.client.completion.await_count == 1


# ---------------------------------------------------------------------------
# Unparsed retry: a reply that yields no tool call at all is re-asked once when
# tool_choice forced a call (or the auto opt-in flag is set); a plain auto
# answer is left untouched.
# ---------------------------------------------------------------------------

# Parses to nothing and classifies as json_malformed (call-shaped, truncated).
_GARBAGE_TOOL_JSON = '{"tool_calls": [{"name": "get_weather", "arguments": '


async def test_non_stream_repairs_unparsed_forced_choice():
    acct = FakeAccount([_ds_tool_sse(_GARBAGE_TOOL_JSON), _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="required")
    result = await openai_mod._collect_non_stream(**args)
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"city": "Moscow"}
    assert acct.client.completion.await_count == 2


async def test_non_stream_unparsed_auto_no_retry_by_default(monkeypatch):
    # Auto choice + flag off: an unparsed reply stays a plain content answer.
    # Pin the flag explicitly: it defaults off, but a developer's local .env may
    # set DANYAPI_TOOLCALL_RETRY=1 and would otherwise flip this test's behaviour.
    from danyapi.config import settings as _settings

    monkeypatch.setattr(_settings, "toolcall_retry", False)
    acct = FakeAccount([_ds_tool_sse(_GARBAGE_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="auto")
    result = await openai_mod._collect_non_stream(**args)
    message = result["choices"][0]["message"]
    assert "tool_calls" not in message
    assert message["content"] == _GARBAGE_TOOL_JSON
    assert acct.client.completion.await_count == 1


async def test_non_stream_unparsed_auto_retries_with_flag(monkeypatch):
    from danyapi.config import settings as _settings

    monkeypatch.setattr(_settings, "toolcall_retry", True)
    acct = FakeAccount([_ds_tool_sse(_GARBAGE_TOOL_JSON), _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="auto")
    result = await openai_mod._collect_non_stream(**args)
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"city": "Moscow"}
    assert acct.client.completion.await_count == 2


async def test_non_stream_unparsed_plain_prose_never_retries(monkeypatch):
    # Even with the flag on, ordinary prose (no_tool_call) is not re-sent.
    from danyapi.config import settings as _settings

    monkeypatch.setattr(_settings, "toolcall_retry", True)
    acct = FakeAccount([DS_PLAIN_SSE, _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="auto")
    result = await openai_mod._collect_non_stream(**args)
    assert "22C" in result["choices"][0]["message"]["content"]
    assert acct.client.completion.await_count == 1


async def test_stream_repairs_unparsed_forced_choice():
    acct = FakeAccount([_ds_tool_sse(_GARBAGE_TOOL_JSON), _ds_tool_sse(_FIXED_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="required")
    lines = await collect_stream(openai_mod._stream_openai(**args))
    assert json.loads(_stream_arguments(lines)) == {"city": "Moscow"}


async def test_non_stream_unparsed_keeps_text_when_retry_fails():
    # Retry still unparsed: the original content answer is preserved, not dropped.
    acct = FakeAccount([_ds_tool_sse(_GARBAGE_TOOL_JSON), _ds_tool_sse(_GARBAGE_TOOL_JSON)])
    args = dict(_deepseek_args(acct), tool_schemas=_WEATHER_SCHEMAS, tool_choice="required")
    result = await openai_mod._collect_non_stream(**args)
    assert result["choices"][0]["message"]["content"] == _GARBAGE_TOOL_JSON
