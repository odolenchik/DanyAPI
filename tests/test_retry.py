import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.api.openai as openai_mod
from danyapi.api.openai import ChatMessage, _collect_non_stream, _stream_openai
from danyapi.retry import _is_rate_limit_hint, _log_rate_limit_hint

BUSY_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"expert"}\n'
    "\n"
    "event: hint\n"
    'data: {"type":"error","content":"Server is busy. Try again later, or use Instant Mode.","clear_response":true,"finish_reason":"expert_busy_use_default"}\n'
    "\n"
    "event: close\n"
    'data: {"click_behavior":"retry","auto_resume":false}\n'
    "\n"
)

OK_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP","fragments":[{"id":2,"type":"RESPONSE","content":"При"}]}}}\n'
    "\n"
    'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"вет"}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)

CTX_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"CONTEXT_LENGTH_EXCEEDED"}\n'
    "\n"
)

# "Messages too frequent" throttle: DeepSeek sends it as a hint, not an HTTP
# status, so it is only visible in the log (never retried).
TOO_FREQUENT_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"expert"}\n'
    "\n"
    "event: hint\n"
    'data: {"type":"error","content":"Messages too frequent. Try again later.","clear_response":true,"finish_reason":"too_frequent"}\n'
    "\n"
    "event: close\n"
    'data: {"click_behavior":"retry","auto_resume":false}\n'
    "\n"
)

# Content-policy refusal: the upstream closes with a plain notice toast, no
# `type: "error"`, and no content fragments at all. Reproduced from the live
# incident where Kimi Code hung re-sending the same refused image.
REFUSAL_NOTICE_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"vision"}\n'
    "\n"
    "event: toast\n"
    'data: {"content":"Content violates terms. Message not sent."}\n'
    "\n"
    "event: close\n"
    'data: {"click_behavior":"retry","auto_resume":false}\n'
    "\n"
)

# Same refusal, but reported through the message status instead of a notice.
CONTENT_FILTER_STATUS_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"vision"}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"CONTENT_FILTER"}\n'
    "\n"
)

# A plain empty reply: no content, no notice, no status.
EMPTY_SSE = (
    'event: ready\ndata: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n\ndata: {"p":"response/status","o":"SET","v":"FINISHED"}\n\n'
)


class FakeSession:
    def __init__(self, sid: str = "c1", last_message_id: str | None = None) -> None:
        self.id = sid
        self.last_message_id = last_message_id
        self.extra: dict = {}


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
    orig = openai_mod.RETRY_BACKOFF_SEC
    openai_mod.RETRY_BACKOFF_SEC = 0.0
    yield
    openai_mod.RETRY_BACKOFF_SEC = orig


def _args(acct, pool=None, existing_sid: str | None = "s1"):
    return {
        "account": acct,
        "pool": pool or MagicMock(),
        "existing_sid": existing_sid,
        "lock": acct.sem,
        "prompt": "x",
        "model": "deepseek-v4-flash",
        "model_type": "default",
        "thinking": False,
        "search": False,
    }


async def test_non_stream_retries_then_success():
    acct = FakeAccount([BUSY_SSE, OK_SSE])
    result = await _collect_non_stream(**_args(acct))
    assert result["choices"][0]["message"]["content"] == "Привет"
    assert acct.pow.make_header.await_count == 2
    assert acct.client.completion.await_count == 2


async def test_non_stream_raises_429_after_retries():
    acct = FakeAccount([BUSY_SSE] * (openai_mod.MAX_RETRIES + 1))
    with pytest.raises(Exception) as excinfo:
        await _collect_non_stream(**_args(acct))
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 429
    assert "busy" in exc.detail.lower()


async def test_non_stream_stale_session_drops_and_recreates():
    """DeepSeek biz error 26 (invalid message id) means the session's parent
    pointer is dead. It must NOT be retried five times with the same id — the
    session is dropped and a fresh one is created, so the request can go on."""
    acct = FakeAccount([OK_SSE])
    # First attempt fails with the stale-session error; the retry uses a new session.
    calls = {"n": 0}

    async def stale_then_ok(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise openai_mod.HTTPException(502, "DeepSeek error 26: invalid message id")
        return FakeResp(OK_SSE)

    acct.client.completion = AsyncMock(side_effect=stale_then_ok)
    acct.sessions.obtain = AsyncMock(side_effect=[(FakeSession(sid="dead"), "dead"), (FakeSession(sid="fresh"), "fresh")])
    pool = MagicMock()
    result = await _collect_non_stream(**_args(acct, pool=pool, existing_sid=None))
    assert result["choices"][0]["message"]["content"] == "Привет"
    pool.forget.assert_called_once_with("dead")
    acct.sessions.forget.assert_called_once_with("dead")
    assert acct.client.completion.await_count == 2


async def test_non_stream_stale_session_rebuilds_prompt():
    """After dropping a dead session the retry must send the FULL conversation.

    The delta prompt was built for the old session; in a brand-new one it would
    arrive with no history behind it, silently losing context. The caller passes
    a rebuild callable for exactly this case.
    """
    acct = FakeAccount([OK_SSE])
    calls = {"n": 0}

    async def stale_then_ok(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise openai_mod.HTTPException(502, "DeepSeek error 26: invalid message id")
        return FakeResp(OK_SSE)

    acct.client.completion = AsyncMock(side_effect=stale_then_ok)
    acct.sessions.obtain = AsyncMock(side_effect=[(FakeSession(sid="dead"), "dead"), (FakeSession(sid="fresh"), "fresh")])
    rebuilt: list[int] = []

    def rebuild() -> str:
        rebuilt.append(1)
        return "FULL HISTORY PROMPT"

    args = _args(acct, pool=MagicMock(), existing_sid=None)
    args["rebuild_prompt"] = rebuild
    result = await _collect_non_stream(**args)

    assert result["choices"][0]["message"]["content"] == "Привет"
    assert rebuilt == [1], "the prompt must be rebuilt exactly once"
    first, second = acct.client.completion.call_args_list
    assert first.kwargs["prompt"] == "x"  # the original delta prompt
    assert second.kwargs["prompt"] == "FULL HISTORY PROMPT"


async def test_non_stream_stale_session_is_not_retried_forever():
    """If the fresh session also answers 26, we stop rather than loop."""
    acct = FakeAccount([OK_SSE] * 3)
    acct.client.completion = AsyncMock(side_effect=openai_mod.HTTPException(502, "DeepSeek error 26: invalid message id"))
    acct.sessions.obtain = AsyncMock(side_effect=[(FakeSession(sid=f"s{i}"), f"s{i}") for i in range(10)])
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await _collect_non_stream(**_args(acct, pool=MagicMock(), existing_sid=None))
    assert excinfo.value.status_code == 502
    # MAX_RETRIES+1 attempts total, not an unbounded loop.
    assert acct.client.completion.await_count == openai_mod.MAX_RETRIES + 1


async def test_non_stream_rate_limit_hint_does_not_record_message_id():
    """A rate-limit hint must not poison the session's parent pointer.

    Regression: ``touch_last_message`` ran before the hint check, so the id of a
    message the upstream never stored was saved as ``last_message_id``. The next
    request then sent it as ``parent_message_id`` and failed with "DeepSeek error
    26: invalid message id", retried five times, then surfaced as HTTP 502.
    """
    acct = FakeAccount([TOO_FREQUENT_SSE])
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await _collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 429
    acct.sessions.touch_last_message.assert_not_called()


async def test_stream_emits_error_event_after_retries():
    acct = FakeAccount([BUSY_SSE] * (openai_mod.MAX_RETRIES + 1))
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    assert acct.client.completion.await_count == openai_mod.MAX_RETRIES + 1
    joined = "".join(lines)
    assert '"error"' in joined
    assert "expert_busy_use_default" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_success_streams_content():
    acct = FakeAccount([OK_SSE])
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    joined = "".join(lines)
    assert '"content": "При"' in joined
    assert '"content": "вет"' in joined
    assert '"finish_reason": "stop"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_emits_usage_when_requested():
    acct = FakeAccount([OK_SSE])
    args = _args(acct)
    args["include_usage"] = True
    gen = _stream_openai(**args)
    lines = list(await _collect(gen))
    joined = "".join(lines)
    assert '"usage"' in joined
    assert '"completion_tokens"' in joined


async def test_stream_omits_usage_by_default():
    acct = FakeAccount([OK_SSE])
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    assert '"usage"' not in "".join(lines)


async def test_non_stream_refusal_notice_is_502_with_upstream_text():
    """A refused message must not come back as a successful blank answer.

    Regression: the refusal arrives as a plain toast (no `type: "error"`), so it
    was dropped, and the empty reply was returned as `finish_reason: "stop"`.
    The client saw "nothing generated" and resent the same request forever.
    """
    acct = FakeAccount([REFUSAL_NOTICE_SSE] * (openai_mod.MAX_RETRIES + 1))
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await _collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 502
    assert "Content violates terms" in str(excinfo.value.detail)
    # Not retried: the same input will be refused again.
    assert acct.client.completion.await_count == 1


async def test_stream_refusal_notice_emits_error_not_empty_stop():
    acct = FakeAccount([REFUSAL_NOTICE_SSE])
    lines = list(await _collect(_stream_openai(**_args(acct))))
    joined = "".join(lines)
    assert "Content violates terms" in joined
    assert '"finish_reason": "stop"' not in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_non_stream_content_filter_status_is_502():
    acct = FakeAccount([CONTENT_FILTER_STATUS_SSE])
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await _collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 502
    assert "refused" in str(excinfo.value.detail).lower()


async def test_stream_content_filter_status_reports_content_filter():
    acct = FakeAccount([CONTENT_FILTER_STATUS_SSE])
    lines = list(await _collect(_stream_openai(**_args(acct))))
    joined = "".join(lines)
    assert '"finish_reason": "content_filter"' in joined


async def test_non_stream_plain_empty_reply_is_502():
    """Even without any hint text, an empty reply is an error, never a success."""
    acct = FakeAccount([EMPTY_SSE])
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await _collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 502
    assert "empty" in str(excinfo.value.detail).lower()


async def test_stream_plain_empty_reply_emits_error():
    acct = FakeAccount([EMPTY_SSE])
    lines = list(await _collect(_stream_openai(**_args(acct))))
    joined = "".join(lines)
    assert '"error"' in joined
    assert '"finish_reason": "stop"' not in joined


async def test_obtain_waits_for_lock():
    acct = FakeAccount([OK_SSE])
    state = {"under_lock": False}

    async def fake_obtain(sid):
        state["under_lock"] = acct.sem.locked()
        return FakeSession(), "s1"

    acct.sessions.obtain = fake_obtain
    await _collect_non_stream(**_args(acct))
    assert state["under_lock"]


async def test_new_session_registered():
    acct = FakeAccount([OK_SSE])
    pool = MagicMock()
    acct.sessions.obtain = AsyncMock(return_value=(FakeSession(sid="new1"), "new1"))
    await _collect_non_stream(**_args(acct, pool=pool, existing_sid=None))
    pool.register.assert_called_once_with(0, "new1")


async def test_prepare_session_auth_error_is_401():
    from danyapi.deepseek.client import DeepSeekError

    acct = FakeAccount([OK_SSE])
    acct.sessions.obtain = AsyncMock(side_effect=DeepSeekError(40001, "token invalid"))
    with pytest.raises(Exception) as excinfo:
        await _collect_non_stream(**_args(acct))
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 401
    assert acct.broken


async def test_prepare_session_non_auth_error_is_502():
    from danyapi.deepseek.client import DeepSeekError

    acct = FakeAccount([OK_SSE])
    acct.sessions.obtain = AsyncMock(side_effect=DeepSeekError(5000, "session boom"))
    with pytest.raises(Exception) as excinfo:
        await _collect_non_stream(**_args(acct))
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 502
    assert not acct.broken


async def test_completion_http_401_marks_broken():
    from fastapi import HTTPException

    acct = FakeAccount([OK_SSE])
    acct.client.completion = AsyncMock(side_effect=HTTPException(401, "unauthorized"))
    with pytest.raises(Exception) as excinfo:
        await _collect_non_stream(**_args(acct))
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 401
    assert acct.broken


async def test_stream_emits_error_when_completion_fails():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(side_effect=openai_mod.HTTPException(502, "boom"))
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    joined = "".join(lines)
    assert '"error"' in joined
    assert "boom" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_emits_error_when_pow_header_fails():
    from danyapi.deepseek.client import DeepSeekError

    acct = FakeAccount([])
    acct.pow.make_header = AsyncMock(side_effect=DeepSeekError(5000, "pow boom"))
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    joined = "".join(lines)
    assert '"error"' in joined
    assert "pow boom" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_non_stream_context_limit_drops_session_and_raises_400():
    acct = FakeAccount([CTX_SSE])
    pool = MagicMock()
    with pytest.raises(Exception) as excinfo:
        await _collect_non_stream(**_args(acct, pool=pool))
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 400
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_stream_context_limit_drops_session_and_emits_length():
    acct = FakeAccount([CTX_SSE])
    pool = MagicMock()
    gen = _stream_openai(**_args(acct, pool=pool))
    lines = list(await _collect(gen))
    joined = "".join(lines)
    assert '"finish_reason": "length"' in joined
    assert "context length exceeded" in joined
    assert joined.rstrip().endswith("data: [DONE]")
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_non_stream_cancel_stops_upstream():
    acct = FakeAccount([])
    started = asyncio.Event()

    class BlockingResp(FakeResp):
        async def aiter_bytes(self):
            yield (b'event: ready\ndata: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n\n')
            started.set()
            await asyncio.Event().wait()
            yield b""

    acct.client.completion = AsyncMock(return_value=BlockingResp(OK_SSE))
    acct.client.stop_stream = AsyncMock()

    task = asyncio.create_task(_collect_non_stream(**_args(acct)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    acct.client.stop_stream.assert_awaited_once()
    args, _ = acct.client.stop_stream.call_args
    assert args[0] == "c1"
    assert args[1] == 2


async def test_stream_disconnect_stops_upstream():
    acct = FakeAccount([OK_SSE])
    acct.client.stop_stream = AsyncMock()
    gen = _stream_openai(**_args(acct))

    first = await gen.__anext__()
    assert first.startswith("data: ")
    await gen.aclose()
    acct.client.stop_stream.assert_awaited_once()
    args, _ = acct.client.stop_stream.call_args
    assert args[0] == "c1"
    assert args[1] == 2


async def test_stream_full_consumption_does_not_stop_upstream():
    acct = FakeAccount([OK_SSE])
    acct.client.stop_stream = AsyncMock()
    gen = _stream_openai(**_args(acct))
    lines = list(await _collect(gen))
    assert any(line.startswith("data: ") for line in lines)
    acct.client.stop_stream.assert_not_awaited()


def _tool_call_sse(command: str) -> str:
    S = "\uff5c\uff5cDSML\uff5c\uff5c"
    body = f'<{S} calls>\n<{S} invoke name="Bash">\n<{S} parameter name="command" string="true">{command}</{S} parameter>\n</{S} invoke>\n</{S} calls>'
    ready = 'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n\n'
    frag = json.dumps(
        {
            "v": {
                "response": {
                    "message_id": 2,
                    "parent_id": 1,
                    "status": "WIP",
                    "fragments": [{"id": 2, "type": "RESPONSE", "content": body}],
                }
            }
        },
        ensure_ascii=False,
    )
    finish = 'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n\n'
    return "event: ready\n" + ready + f"data: {frag}\n\n" + finish


async def test_non_stream_repeat_loop_returns_error(monkeypatch):
    """The non-stream path stops the turn the same way the stream does."""
    monkeypatch.setattr(openai_mod.settings, "repeat_threshold", 3)
    acct = FakeAccount([_tool_call_sse("ls")] * 3)
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {"Bash": {"command": "string"}}

    results = [await _collect_non_stream(**args) for _ in range(3)]

    last = results[-1]
    assert last["error"]["finish_reason"] == "repeat_loop"
    assert last["choices"][0]["finish_reason"] == "error"
    assert last["choices"][0]["message"]["content"] == ""


async def test_non_stream_repeat_guard_disabled(monkeypatch):
    """With the guard off the same rounds pass through untouched."""
    monkeypatch.setattr(openai_mod.settings, "repeat_guard", False)
    monkeypatch.setattr(openai_mod.settings, "repeat_threshold", 2)
    acct = FakeAccount([_tool_call_sse("ls")] * 3)
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {"Bash": {"command": "string"}}

    results = [await _collect_non_stream(**args) for _ in range(3)]

    assert all("error" not in r for r in results)


async def test_stream_repeat_loop_aborts_and_stops_upstream(monkeypatch):
    """The same tool call round after round must be stopped, not fed back forever."""
    monkeypatch.setattr(openai_mod.settings, "repeat_threshold", 3)
    acct = FakeAccount([_tool_call_sse("ls")] * 3)
    acct.client.stop_stream = AsyncMock()
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {"Bash": {"command": "string"}}

    results = []
    session = acct.sessions.obtain.return_value[0]
    for _ in range(3):
        gen = _stream_openai(**args)
        results.append(list(await _collect(gen)))

    payloads = [json.loads(line[len("data: ") :]) for block in results for line in block if line.startswith("data: {")]
    errors = [p for p in payloads if p.get("error", {}).get("finish_reason") == "repeat_loop"]
    assert errors, "third identical round must be stopped"
    assert errors[0]["choices"][0]["finish_reason"] == "error"
    acct.client.stop_stream.assert_awaited()
    assert "repeat_guard" not in session.extra


async def test_stream_distinct_calls_do_not_trip_repeat_loop(monkeypatch):
    """Different calls are a normal multi-step turn and must pass through."""
    monkeypatch.setattr(openai_mod.settings, "repeat_threshold", 2)
    acct = FakeAccount([_tool_call_sse("ls"), _tool_call_sse("pwd"), _tool_call_sse("whoami")])
    acct.client.stop_stream = AsyncMock()
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {"Bash": {"command": "string"}}

    for _ in range(3):
        await _collect(_stream_openai(**args))

    acct.client.stop_stream.assert_not_awaited()


def _degen_sse(repeats: int = 40) -> str:
    S = "\uff5c\uff5c"
    body = (f"<{S}DSML{S} calls>\n") * repeats
    ready = 'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n\n'
    frag = json.dumps(
        {
            "v": {
                "response": {
                    "message_id": 2,
                    "parent_id": 1,
                    "status": "WIP",
                    "fragments": [{"id": 2, "type": "RESPONSE", "content": body}],
                }
            }
        },
        ensure_ascii=False,
    )
    finish = 'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n\n'
    return "event: ready\n" + ready + f"data: {frag}\n\n" + finish


async def test_stream_degenerate_repeat_aborts_and_stops_upstream():
    """A runaway calls-container repeat must abort the stream, not run to limit."""
    acct = FakeAccount([_degen_sse()])
    acct.client.stop_stream = AsyncMock()
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {}
    gen = _stream_openai(**args)
    lines = list(await _collect(gen))
    assert any(line.startswith("data: ") for line in lines)
    # The stream was cut short and the upstream stop was requested.
    acct.client.stop_stream.assert_awaited()


def test_model_type_mapping():
    assert openai_mod.MODEL_TYPE_BY_NAME == {
        "deepseek-v4-flash": "default",
        "deepseek-v4-pro": "expert",
        "deepseek-v4-vision": "vision",
    }


async def test_search_gated_to_chat_models_and_thinking_allowed(monkeypatch):
    # Provider defaults are what this test pins down, so keep the per-turn
    # heuristic out of it regardless of what the environment sets.
    monkeypatch.setattr(openai_mod.settings, "thinking_heuristic", False)
    captured = {}
    orig = openai_mod._collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    openai_mod._collect_non_stream = fake_collect
    try:
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(FakeAccount([OK_SSE]), None))
        openai_mod.app.state.pool = pool

        async def run(model, search, thinking):
            req = SimpleNamespace(
                model=model,
                stream=False,
                thinking=thinking,
                search=search,
                session_id=None,
                files=None,
                messages=[ChatMessage(role="user", content="x")],
            )
            await openai_mod._chat_completions_deepseek(req)

        await run("deepseek-v4-flash", search=True, thinking=None)
        assert captured["model_type"] == "default"
        assert captured["search"] is True
        assert captured["thinking"] is False

        await run("deepseek-v4-pro", search=True, thinking=None)
        assert captured["model_type"] == "expert"
        assert captured["search"] is True
        assert captured["thinking"] is True

        await run("deepseek-v4-vision", search=True, thinking=True)
        assert captured["model_type"] == "vision"
        assert captured["search"] is False
        assert captured["thinking"] is True
    finally:
        openai_mod._collect_non_stream = orig


def test_pro_rejects_text_files():
    from danyapi.api.openai import Attachment, _validate_attachments

    with pytest.raises(Exception) as excinfo:
        _validate_attachments([Attachment(b"x", "a.txt", "text/plain", False)], "expert")
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 400


def test_pro_accepts_images():
    from danyapi.api.openai import Attachment, _validate_attachments

    _validate_attachments([Attachment(b"x", "a.png", "image/png", True)], "expert")


def test_vision_rejects_text_files():
    from danyapi.api.openai import Attachment, _validate_attachments

    with pytest.raises(Exception) as excinfo:
        _validate_attachments([Attachment(b"x", "a.txt", "text/plain", False)], "vision")
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 400


def test_vision_accepts_images():
    from danyapi.api.openai import Attachment, _validate_attachments

    _validate_attachments([Attachment(b"x", "a.png", "image/png", True)], "vision")


def test_too_many_files_rejected():
    from danyapi.api.openai import MAX_FILES_PER_REQUEST, Attachment, _validate_attachments

    many = [Attachment(b"x", f"{i}.txt", "text/plain", False) for i in range(MAX_FILES_PER_REQUEST + 1)]
    with pytest.raises(Exception) as excinfo:
        _validate_attachments(many, "default")
    exc = excinfo.value
    assert isinstance(exc, openai_mod.HTTPException)
    assert exc.status_code == 400


def test_collect_attachments_from_image_url_and_files():
    import base64 as b64

    from danyapi.api.openai import ChatMessage, _collect_attachments

    img_b64 = b64.b64encode(b"pngdata").decode()
    file_b64 = b64.b64encode(b"hello").decode()
    req = SimpleNamespace(
        model="deepseek-v4-flash",
        messages=[
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            )
        ],
        files=[SimpleNamespace(name="doc.txt", content=file_b64, content_type="text/plain")],
    )
    atts = _collect_attachments(req)
    assert len(atts) == 2
    assert atts[0].is_image
    assert atts[0].data == b"pngdata"
    assert not atts[1].is_image
    assert atts[1].data == b"hello"


async def test_upload_attachments_returns_ids():
    from danyapi.api.openai import Attachment, _upload_attachments

    acct = FakeAccount([OK_SSE])
    acct.client.upload_file = AsyncMock(
        side_effect=[
            {"id": "file-1", "status": "PENDING"},
            {"id": "file-2", "status": "PENDING"},
        ]
    )
    acct.pow_upload = MagicMock()
    acct.pow_upload.make_header = AsyncMock(return_value={"X-DS-PoW-Response": "x"})
    ids = await _upload_attachments(
        acct,
        [Attachment(b"a", "a.txt", "text/plain", False), Attachment(b"b", "b.txt", "text/plain", False)],
        "default",
        False,
    )
    assert ids == ["file-1", "file-2"]
    assert acct.client.upload_file.await_count == 2


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def test_is_rate_limit_hint_variants():
    class Rec:
        def __init__(self, hint):
            self.hint_error = hint

    assert _is_rate_limit_hint(Rec(None)) is False
    assert _is_rate_limit_hint(Rec({})) is False
    assert _is_rate_limit_hint(Rec({"finish_reason": "too_frequent"})) is True
    assert _is_rate_limit_hint(Rec({"finish_reason": "expert_busy_use_default"})) is False
    assert _is_rate_limit_hint(Rec({"message": "Messages too frequent. Try again later."})) is True
    assert _is_rate_limit_hint(Rec({"message": "Rate limit reached"})) is True
    assert _is_rate_limit_hint(Rec({"message": "all good"})) is False
    assert _is_rate_limit_hint(Rec({"message": 123})) is False


def test_log_rate_limit_hint_reports_start_counts(caplog):
    class Acct:
        def __init__(self):
            self.label = "acct#7"

        def recent_request_count(self, window):
            return {10.0: 3, 30.0: 9, 60.0: 20}[window]

    class Rec:
        def __init__(self):
            self.hint_error = {"finish_reason": "too_frequent", "message": "Messages too frequent"}

    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        _log_rate_limit_hint(
            Rec(),
            Acct(),
            where="non-stream",
            session_id="s1",
            request_id="r1",
            model="deepseek-v4-pro",
            elapsed_ms=1234.5,
        )
    assert "too_frequent" in caplog.text
    assert "starts_10s=3" in caplog.text
    assert "starts_30s=9" in caplog.text
    assert "starts_60s=20" in caplog.text
    assert "acct#7" in caplog.text
    assert "elapsed_ms=1234" in caplog.text


def test_log_rate_limit_hint_reports_end_to_start_gap(caplog):
    class Acct:
        last_end_gap = 2.5

        def recent_request_count(self, window):
            return 1

    class Rec:
        def __init__(self):
            self.hint_error = {"finish_reason": "rate_limit_reached", "message": "Messages too frequent"}

    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        _log_rate_limit_hint(Rec(), Acct(), where="stream")
    assert "end_to_start_gap=2.5" in caplog.text


def test_log_rate_limit_hint_without_end_gap(caplog):
    """When no gap has been measured the field shows a placeholder, not 0."""

    class Acct:
        def recent_request_count(self, window):
            return 1

    class Rec:
        def __init__(self):
            self.hint_error = {"finish_reason": "rate_limit_reached", "message": "Messages too frequent"}

    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        _log_rate_limit_hint(Rec(), Acct(), where="stream")
    assert "end_to_start_gap=-" in caplog.text


def test_log_rate_limit_hint_without_optional_fields(caplog):
    class Acct:
        def recent_request_count(self, window):
            return 0

    class Rec:
        def __init__(self):
            self.hint_error = None

    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        _log_rate_limit_hint(Rec(), Acct(), where="stream")
    assert "elapsed_ms=-" in caplog.text
    # No label attribute on the account -> the fallback placeholder is used.
    assert "account=?" in caplog.text


async def test_non_stream_rate_limit_hint_logs_and_raises_429(caplog):
    acct = FakeAccount([TOO_FREQUENT_SSE])
    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        with pytest.raises(Exception) as excinfo:
            await _collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 429
    assert "too_frequent" in caplog.text
    assert "where=non-stream" in caplog.text


async def test_stream_rate_limit_hint_logs_and_emits_error(caplog):
    acct = FakeAccount([TOO_FREQUENT_SSE])
    with caplog.at_level(logging.WARNING, logger="danyapi.retry"):
        lines = list(await _collect(_stream_openai(**_args(acct))))
    joined = "".join(lines)
    assert "too_frequent" in joined
    assert "where=stream" in caplog.text
