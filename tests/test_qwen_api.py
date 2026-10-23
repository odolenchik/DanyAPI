import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import danyapi.qwen.api as qwen_api
from danyapi.accounts import AccountPool
from danyapi.qwen.accounts import QwenAccount
from danyapi.qwen.client import QwenClient, QwenError, QwenSession
from danyapi.qwen.stream import QwenStreamReconstructor

OK_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "Hello", "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": " world", "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)

THINK_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "", "phase": "thinking_summary",'
    ' "extra": {"summary_thought": {"content": ["Think step"]}}, "status": "typing"}}],'
    ' "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "Answer", "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)

BUSY_SSE = 'data: {"error": {"code": "Too_Many_Requests", "details": "please slow down"}, "response_id": "r1"}\n\n'

QUOTA_CODE_SSE = 'data: {"error": {"code": "quotaLimited", "details": "daily quota reached"}, "response_id": "r1"}\n\n'

QUOTA_TEXT_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "you\\u0027ve used up your daily image generation quota",'
    ' "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)

TRANSIENT_TEXT_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "There was an issue connecting to Qwen3.8-Omni-Flash.",'
    ' "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)

OVERLOAD_TEXT_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "The service is experiencing high load. Please try again later.",'
    ' "phase": "answer", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "answer"}}], "response_id": "r1"}\n'
    "\n"
)

IMAGE_OK_SSE = (
    'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n'
    "\n"
    'data: {"choices": [{"delta": {"role": "assistant", "content": "![img](https://cdn.qwenlm.ai/a.png)",'
    ' "phase": "image", "status": "typing"}}], "response_id": "r1"}\n'
    "\n"
    'data: {"choices": [{"delta": {"content": "", "role": "assistant", "status": "finished", "phase": "image"}}], "response_id": "r1"}\n'
    "\n"
)

CTX_SSE = 'data: {"error": {"code": "ContextLengthExceeded", "details": "too long"}, "response_id": "r1"}\n\n'


class JsonResp:
    def __init__(self, body):
        self._b = body.encode()
        self.status_code = 200
        self.headers = {"content-type": "application/json"}

    async def aiter_bytes(self):
        yield self._b

    async def aclose(self):
        pass

    async def aread(self):
        return self._b


class FakeSession:
    def __init__(self, sid: str = "c1", last_response_id: str | None = None) -> None:
        self.id = sid
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
        self.sem = asyncio.Semaphore(1)
        self.sessions = MagicMock()
        self.sessions.obtain = AsyncMock(return_value=(FakeSession(), "s1"))
        self.sessions.touch_last_message = MagicMock()

    def mark_broken(self):
        self.broken = True

    def mark_quota_exhausted(self, cooldown_seconds):
        self.broken = True
        self.quota_until = cooldown_seconds

    def mark_captcha(self, cooldown_seconds):
        self.broken = True
        self.captcha_until = cooldown_seconds

    def sticky_image_chat(self, model):
        return None

    def set_sticky_image_chat(self, model, chat_id):
        pass


@pytest.fixture(autouse=True)
def zero_backoff():
    orig = qwen_api.RETRY_BACKOFF_SEC
    qwen_api.RETRY_BACKOFF_SEC = 0.0
    yield
    qwen_api.RETRY_BACKOFF_SEC = orig


def _args(acct, pool=None, existing_sid: str | None = "s1", tool_mode=False):
    return {
        "account": acct,
        "pool": pool or MagicMock(),
        "existing_sid": existing_sid,
        "lock": acct.sem,
        "prompt": "x",
        "model": "qwen3.8-max",
        "model_id": "qwen3.8-max",
        "thinking": False,
        "search": False,
        "tool_mode": tool_mode,
    }


async def _send(resp):
    client = MagicMock()
    client.completion = AsyncMock(return_value=resp)
    account = MagicMock()
    account.client = client
    session = FakeSession()
    return await qwen_api._send_completion(account, session, "p", "m", False, False)


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


async def test_non_stream_collects_content():
    acct = FakeAccount([OK_SSE])
    result = await qwen_api.collect_non_stream(**_args(acct))
    assert result["choices"][0]["message"]["content"] == "Hello world"
    assert result["session_id"] == "s1"
    assert result["choices"][0]["message"]["role"] == "assistant"
    acct.sessions.touch_last_message.assert_called_once_with("s1", "r1")


async def test_non_stream_collects_reasoning():
    acct = FakeAccount([THINK_SSE])
    result = await qwen_api.collect_non_stream(**_args(acct))
    message = result["choices"][0]["message"]
    assert message["content"] == "Answer"
    assert message["reasoning_content"] == "Think step"


async def test_non_stream_retries_then_success():
    acct = FakeAccount([BUSY_SSE, OK_SSE])
    result = await qwen_api.collect_non_stream(**_args(acct))
    assert result["choices"][0]["message"]["content"] == "Hello world"
    assert acct.client.completion.await_count == 2


async def test_non_stream_raises_429_after_retries():
    acct = FakeAccount([BUSY_SSE] * (qwen_api.MAX_RETRIES + 1))
    with pytest.raises(Exception) as excinfo:
        await qwen_api.collect_non_stream(**_args(acct))
    assert isinstance(excinfo.value, qwen_api.HTTPException)
    assert excinfo.value.status_code == 429


async def test_stream_emits_error_event_after_retries():
    acct = FakeAccount([BUSY_SSE] * (qwen_api.MAX_RETRIES + 1))
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    assert acct.client.completion.await_count == qwen_api.MAX_RETRIES + 1
    joined = "".join(lines)
    assert "Too_Many_Requests" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_success_streams_content():
    acct = FakeAccount([OK_SSE])
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"content": "Hello"' in joined
    assert '"content": " world"' in joined
    assert '"finish_reason": "stop"' in joined
    assert '"session_id": "s1"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_success_streams_reasoning():
    acct = FakeAccount([THINK_SSE])
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert "reasoning_content" in joined
    assert '"content": "Answer"' in joined


async def test_stream_emits_usage_when_requested():
    acct = FakeAccount([OK_SSE])
    args = _args(acct)
    args["include_usage"] = True
    gen = qwen_api.stream_openai(**args)
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"usage"' in joined
    assert '"completion_tokens"' in joined


async def test_new_session_registered():
    acct = FakeAccount([OK_SSE])
    pool = MagicMock()
    acct.sessions.obtain = AsyncMock(return_value=(FakeSession(sid="new1"), "new1"))
    await qwen_api.collect_non_stream(**_args(acct, pool=pool, existing_sid=None))
    pool.register.assert_called_once_with(0, "new1")


async def test_usage_reported():
    acct = FakeAccount([OK_SSE])
    result = await qwen_api.collect_non_stream(**_args(acct))
    assert "usage" in result


async def test_stream_emits_error_when_completion_fails():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(side_effect=httpx.ConnectError("boom"))
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"error"' in joined
    assert "Qwen request failed" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_emits_error_when_json_error():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(return_value=JsonResp('{"ret":["FAIL_SYS_USER_VALIDATE"]}'))
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"error"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_non_stream_context_limit_drops_session_and_raises_400():
    acct = FakeAccount([CTX_SSE])
    pool = MagicMock()
    with pytest.raises(Exception) as excinfo:
        await qwen_api.collect_non_stream(**_args(acct, pool=pool))
    assert isinstance(excinfo.value, qwen_api.HTTPException)
    assert excinfo.value.status_code == 400
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_stream_context_limit_drops_session_and_emits_length():
    acct = FakeAccount([CTX_SSE])
    pool = MagicMock()
    gen = qwen_api.stream_openai(**_args(acct, pool=pool))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"finish_reason": "length"' in joined
    assert "context length exceeded" in joined
    assert joined.rstrip().endswith("data: [DONE]")
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_non_stream_context_limit_json_raises_400():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(return_value=JsonResp('{"error": {"code": "ContextLengthExceeded", "details": "too long"}}'))
    pool = MagicMock()
    with pytest.raises(Exception) as excinfo:
        await qwen_api.collect_non_stream(**_args(acct, pool=pool))
    assert isinstance(excinfo.value, qwen_api.HTTPException)
    assert excinfo.value.status_code == 400
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_stream_context_limit_json_emits_length():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(return_value=JsonResp('{"error": {"code": "ContextLengthExceeded", "details": "too long"}}'))
    pool = MagicMock()
    gen = qwen_api.stream_openai(**_args(acct, pool=pool))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"finish_reason": "length"' in joined
    assert joined.rstrip().endswith("data: [DONE]")
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_stream_disconnect_stops_upstream():
    acct = FakeAccount([OK_SSE])
    acct.client.stop_stream = AsyncMock()
    gen = qwen_api.stream_openai(**_args(acct))
    first = await gen.__anext__()
    assert first.startswith("data: ")
    await gen.aclose()
    acct.client.stop_stream.assert_awaited_once()
    args, _ = acct.client.stop_stream.call_args
    assert args[0] == "c1"
    assert args[1] == "r1"


async def test_non_stream_cancel_stops_upstream():
    acct = FakeAccount([])
    started = asyncio.Event()

    class BlockingResp(FakeResp):
        async def aiter_bytes(self):
            yield (b'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1","response_index":"0"}} \n\n')
            started.set()
            await asyncio.Event().wait()
            yield b""

    acct.client.completion = AsyncMock(return_value=BlockingResp(OK_SSE))
    acct.client.stop_stream = AsyncMock()

    task = asyncio.create_task(qwen_api.collect_non_stream(**_args(acct)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    acct.client.stop_stream.assert_awaited_once()
    args, _ = acct.client.stop_stream.call_args
    assert args[0] == "c1"
    assert args[1] == "r1"


async def test_stream_full_consumption_does_not_stop_upstream():
    acct = FakeAccount([OK_SSE])
    acct.client.stop_stream = AsyncMock()
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    assert any(line.startswith("data: ") for line in lines)
    acct.client.stop_stream.assert_not_awaited()


def test_error_status():
    assert qwen_api._error_status("Too_Many_Requests") == 429
    assert qwen_api._error_status("RateLimited") == 429
    assert qwen_api._error_status("unauthorized") == 401
    assert qwen_api._error_status("Other") == 502
    assert qwen_api._error_status(None) == 502


def test_is_context_limit_code():
    assert qwen_api._is_context_limit_code("ContextLengthExceeded")
    assert qwen_api._is_context_limit_code("The input token limit is exceeded")
    assert not qwen_api._is_context_limit_code("Too_Many_Requests")
    assert not qwen_api._is_context_limit_code("")
    assert not qwen_api._is_context_limit_code(None)
    assert not qwen_api._is_context_limit_code(123)


def test_is_retryable_error():
    rec = MagicMock()
    rec.error = {"code": "Too_Many_Requests"}
    rec.has_content = False
    assert qwen_api._is_retryable_error(rec)
    rec2 = MagicMock()
    rec2.error = {"code": "Other"}
    rec2.has_content = False
    assert not qwen_api._is_retryable_error(rec2)
    rec3 = MagicMock()
    rec3.error = None
    assert not qwen_api._is_retryable_error(rec3)
    rec4 = MagicMock()
    rec4.error = {"code": "Too_Many_Requests"}
    rec4.has_content = True
    assert not qwen_api._is_retryable_error(rec4)


def test_error_body():
    rec = MagicMock()
    rec.error = {"code": "x", "details": "boom"}
    body = json.loads(qwen_api._error_body(rec))
    assert body["error"]["message"] == "boom"
    assert body["error"]["code"] == "x"
    rec2 = MagicMock()
    rec2.error = {}
    body = json.loads(qwen_api._error_body(rec2))
    assert "error" in body["error"]["message"]


def test_accumulate_usage():
    session = FakeSession()
    rec = QwenStreamReconstructor()
    rec.usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    usage = qwen_api._accumulate_usage(session, rec)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    usage2 = qwen_api._accumulate_usage(session, rec)
    assert usage2 == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def test_drop_session():
    pool = MagicMock()
    acct = FakeAccount([])
    acct.sessions.forget = MagicMock()
    qwen_api._drop_session(pool, acct, "s1")
    pool.forget.assert_called_once_with("s1")
    pool.forget_context.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


def test_handle_account_error_auth():
    acct = FakeAccount([])
    acct.mark_broken = MagicMock()
    qwen_api._handle_account_error(acct, QwenError("unauthorized", "bad"))
    acct.mark_broken.assert_called_once_with()


def test_handle_account_error_other():
    acct = FakeAccount([])
    acct.mark_broken = MagicMock()
    qwen_api._handle_account_error(acct, QwenError(500, "bad"))
    acct.mark_broken.assert_not_called()


def _real_qwen_account(index):
    return QwenAccount(index, QwenClient(token=f"tok{index}", timeout=1))


def test_qwen_stats_separates_quota_from_auth():
    quota = _real_qwen_account(0)
    quota.mark_quota_exhausted(3600)
    auth = _real_qwen_account(1)
    auth.mark_broken()
    pool = AccountPool([quota, auth, _real_qwen_account(2)], label="qwen")
    stats = pool.stats()
    assert stats["healthy"] == 1
    assert stats["broken"] == 2
    assert stats["broken_quota"] == 1
    assert stats["broken_auth"] == 1
    assert 0 < stats["quota_resets_in"] <= 3600


async def test_try_stop_stream():
    client = MagicMock()
    client.stop_stream = AsyncMock()
    await qwen_api._try_stop_stream(client, "c1", "r1")
    client.stop_stream.assert_awaited_once_with("c1", "r1")
    await qwen_api._try_stop_stream(client, "", "r1")
    await qwen_api._try_stop_stream(client, "c1", None)
    client.stop_stream.assert_awaited_once()


async def test_try_stop_stream_error():
    client = MagicMock()
    client.stop_stream = AsyncMock(side_effect=Exception("x"))
    await qwen_api._try_stop_stream(client, "c1", "r1")


async def test_auth_error_401():
    acct = FakeAccount([])
    acct.sessions.obtain = AsyncMock(side_effect=QwenError("unauthorized", "bad"))
    with pytest.raises(Exception) as excinfo:
        await qwen_api._prepare_session(acct, MagicMock(), None, "m")
    assert excinfo.value.status_code == 401
    assert acct.broken


async def test_other_error_502():
    acct = FakeAccount([])
    acct.sessions.obtain = AsyncMock(side_effect=QwenError(500, "bad"))
    with pytest.raises(Exception) as excinfo:
        await qwen_api._prepare_session(acct, MagicMock(), None, "m")
    assert excinfo.value.status_code == 502
    assert not acct.broken


async def test_prepare_session_new_session_registered():
    acct = FakeAccount([])
    acct.sessions.obtain = AsyncMock(return_value=(QwenSession(id="new"), "new"))
    pool = MagicMock()
    _session, key = await qwen_api._prepare_session(acct, pool, "old", "m", ("u1",))
    assert key == "new"
    pool.register.assert_called_once_with(0, "new")
    pool.forget.assert_called_once_with("old")
    pool.forget_context.assert_called_once_with("old")
    acct.sessions.forget.assert_called_once_with("old")
    pool.index_context.assert_called_once_with("new", ("u1",))


async def test_existing_session_reused():
    acct = FakeAccount([])
    pool = MagicMock()
    _session, key = await qwen_api._prepare_session(acct, pool, "s1", "m")
    assert key == "s1"
    pool.register.assert_not_called()


async def test_json_error_dict():
    with pytest.raises(Exception) as excinfo:
        await _send(JsonResp('{"error": {"code": "Too_Many_Requests", "details": "slow"}}'))
    assert excinfo.value.status_code == 429


async def test_json_context_limit():
    with pytest.raises(qwen_api.ContextLimitError):
        await _send(JsonResp('{"error": {"code": "ContextLengthExceeded", "details": "long"}}'))


async def test_json_data_code():
    with pytest.raises(Exception) as excinfo:
        await _send(JsonResp('{"data": {"code": "quotaLimited", "details": "no quota"}}'))
    assert excinfo.value.status_code == 429


async def test_json_data_context_limit():
    with pytest.raises(qwen_api.ContextLimitError):
        await _send(JsonResp('{"data": {"code": "TokenLimit", "details": "long"}}'))


async def test_bad_json():
    with pytest.raises(Exception) as excinfo:
        await _send(JsonResp("not json"))
    assert excinfo.value.status_code == 502


async def test_waf_html():
    resp = JsonResp("<html>challenge</html>")
    resp.headers = {"content-type": "text/html"}
    with pytest.raises(Exception) as excinfo:
        await _send(resp)
    assert excinfo.value.status_code == 502
    assert "WAF" in str(excinfo.value.detail)


async def test_http_status_error():
    client = MagicMock()
    client.completion = AsyncMock(side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500)))
    account = MagicMock()
    account.client = client
    with pytest.raises(Exception) as excinfo:
        await qwen_api._send_completion(account, FakeSession(), "p", "m", False, False)
    assert excinfo.value.status_code == 500


async def test_http_error():
    client = MagicMock()
    client.completion = AsyncMock(side_effect=httpx.ConnectError("boom"))
    account = MagicMock()
    account.client = client
    with pytest.raises(Exception) as excinfo:
        await qwen_api._send_completion(account, FakeSession(), "p", "m", False, False)
    assert excinfo.value.status_code == 502


async def test_non_200():
    resp = FakeResp("data: x\n\n")
    resp.status_code = 429
    with pytest.raises(Exception) as excinfo:
        await _send(resp)
    assert excinfo.value.status_code == 429


async def test_collect_non_stream_retryable_http():
    acct = FakeAccount([OK_SSE])
    acct.client.completion = AsyncMock(
        side_effect=[
            qwen_api.HTTPException(429, "slow"),
            FakeResp(OK_SSE),
        ]
    )
    result = await qwen_api.collect_non_stream(**_args(acct))
    assert result["choices"][0]["message"]["content"] == "Hello world"
    assert acct.client.completion.await_count == 2


async def test_non_stream_tool_mode_falls_back_to_content():
    acct = FakeAccount([OK_SSE])
    args = _args(acct, tool_mode=True)
    result = await qwen_api.collect_non_stream(**args)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "tool_calls" not in choice["message"]
    assert "Hello world" in choice["message"]["content"]


async def test_stream_tool_mode_falls_back_to_content():
    acct = FakeAccount([OK_SSE])
    args = _args(acct, tool_mode=True)
    gen = qwen_api.stream_openai(**args)
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"tool_calls"' not in joined
    assert '"content": "Hello world"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_stream_prepare_session_error():
    from fastapi import HTTPException

    acct = FakeAccount([OK_SSE])
    acct.sessions.obtain = AsyncMock(side_effect=HTTPException(401, "bad"))
    gen = qwen_api.stream_openai(**_args(acct))
    lines = await _collect(gen)
    joined = "".join(lines)
    assert '"error"' in joined
    assert "bad" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_collect_non_stream_retryable_http_exhausted():
    acct = FakeAccount([OK_SSE])
    acct.client.completion = AsyncMock(side_effect=[qwen_api.HTTPException(429, "slow")] * (qwen_api.MAX_RETRIES + 1))
    with pytest.raises(Exception) as excinfo:
        await qwen_api.collect_non_stream(**_args(acct))
    assert excinfo.value.status_code == 429


async def test_collect_non_stream_finish_buffer():
    sse = (
        'data: {"response.created":{"chat_id":"c1","parent_id":"p0","response_id":"r1"}} \n'
        "\n"
        'data: {"choices": [{"delta": {"content": "Hello", "phase": "answer"}}], "response_id": "r1"}\n'
        "\n"
        'data: {"choices": [{"delta": {"status": "finished", "phase": "answer"}}], "response_id": "r1"}'
    )
    acct = FakeAccount([sse])
    result = await qwen_api.collect_non_stream(**_args(acct))
    assert result["choices"][0]["message"]["content"] == "Hello"


async def test_non_stream_tool_mode_with_reasoning():
    acct = FakeAccount([THINK_SSE])
    args = _args(acct, tool_mode=True)
    result = await qwen_api.collect_non_stream(**args)
    message = result["choices"][0]["message"]
    assert message["content"] == "Answer"
    assert message["reasoning_content"] == "Think step"


def _image_args(acct, pool=None, existing_sid="s1"):
    return {
        "account": acct,
        "pool": pool or MagicMock(),
        "existing_sid": existing_sid,
        "lock": acct.sem,
        "prompt": "x",
        "model": "qwen3.8-max",
        "model_id": "qwen3.8-max",
    }


async def test_collect_image_text_quota_marks_and_raises():
    acct = FakeAccount([QUOTA_TEXT_SSE])
    pool = MagicMock()
    with pytest.raises(qwen_api.QuotaExhausted) as excinfo:
        await qwen_api.collect_image(**_image_args(acct, pool=pool))
    assert excinfo.value.status_code == 429
    assert acct.broken
    pool.forget.assert_called_once_with("s1")
    acct.sessions.forget.assert_called_once_with("s1")


async def test_collect_image_code_quota_marks_and_raises():
    acct = FakeAccount([QUOTA_CODE_SSE])
    pool = MagicMock()
    with pytest.raises(qwen_api.QuotaExhausted) as excinfo:
        await qwen_api.collect_image(**_image_args(acct, pool=pool))
    assert excinfo.value.status_code == 429
    assert acct.broken
    # No retries on the same account: the provider was hit exactly once.
    assert acct.client.completion.await_count == 1


async def test_collect_non_stream_code_quota_marks_and_raises():
    acct = FakeAccount([QUOTA_CODE_SSE])
    pool = MagicMock()
    with pytest.raises(qwen_api.QuotaExhausted) as excinfo:
        await qwen_api.collect_non_stream(**_args(acct, pool=pool))
    assert excinfo.value.status_code == 429
    assert acct.broken
    assert acct.client.completion.await_count == 1


def test_is_quota_exhausted_variants():
    rec = QwenStreamReconstructor()
    rec.content = "You have used up your daily image generation quota"
    assert qwen_api._is_quota_exhausted(rec) is not None
    rec2 = QwenStreamReconstructor()
    rec2.error = {"code": "quotaLimited"}
    assert qwen_api._is_quota_exhausted(rec2) is not None
    rec3 = QwenStreamReconstructor()
    rec3.content = "Here is a picture of a quota"
    assert qwen_api._is_quota_exhausted(rec3) is None
    rec4 = QwenStreamReconstructor()
    rec4.image_urls = ["https://cdn.qwenlm.ai/a.png"]
    rec4.content = "used up your daily quota"
    assert qwen_api._is_quota_exhausted(rec4) is None


def test_is_transient_text_variants():
    connect = QwenStreamReconstructor()
    connect.content = "Oops! There was an issue connecting to Qwen3.8-Omni-Flash."
    assert qwen_api._is_transient_text(connect) == "issue connecting"

    overload = QwenStreamReconstructor()
    overload.content = "Упс! Возникла проблема подключения. Попробуйте позже."
    assert qwen_api._is_transient_text(overload) is not None

    # A real answer that merely mentions a connection is not a failure.
    answer = QwenStreamReconstructor()
    answer.content = "Your chart shows a strong connection to Saturn."
    assert qwen_api._is_transient_text(answer) is None

    # An image response is a result, never a transient failure.
    with_image = QwenStreamReconstructor()
    with_image.image_urls = ["https://cdn.qwenlm.ai/a.png"]
    with_image.content = "issue connecting"
    assert qwen_api._is_transient_text(with_image) is None

    empty = QwenStreamReconstructor()
    assert qwen_api._is_transient_text(empty) is None


async def test_collect_image_retries_transient_then_succeeds():
    """Transient text failure is retried; the next attempt returns the image."""
    acct = FakeAccount([TRANSIENT_TEXT_SSE, IMAGE_OK_SSE])
    pool = MagicMock()

    result = await qwen_api.collect_image(**_image_args(acct, pool=pool))

    assert result["image_urls"] == ["https://cdn.qwenlm.ai/a.png"]
    assert acct.client.completion.await_count == 2
    # Transient is not a quota problem: the account stays in rotation.
    assert not acct.broken


async def test_collect_image_transient_does_not_mark_quota():
    """A transient failure never takes the account out of rotation, unlike a quota."""
    acct = FakeAccount([OVERLOAD_TEXT_SSE, IMAGE_OK_SSE])
    pool = MagicMock()

    result = await qwen_api.collect_image(**_image_args(acct, pool=pool))

    assert result["image_urls"]
    assert acct.client.completion.await_count == 2
    assert not acct.broken


async def test_send_completion_marks_account_broken_on_qwen_error():
    """A refused refresh (QwenError) must mark the account broken and return 401.

    Without the QwenError branch the exception escaped as a bare 500 and /health
    kept reporting the account healthy while every request failed.
    """
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(side_effect=QwenError("Unauthorized", "Token is revoked, please login again."))
    with pytest.raises(qwen_api.HTTPException) as excinfo:
        await qwen_api._send_completion(acct, FakeSession(), "p", "m", False, False)
    assert excinfo.value.status_code == 401
    assert acct.broken


async def test_send_completion_qwen_error_not_auth_is_502():
    acct = FakeAccount([])
    acct.client.completion = AsyncMock(side_effect=QwenError(-1, "auth refresh returned no access_token"))
    with pytest.raises(qwen_api.HTTPException) as excinfo:
        await qwen_api._send_completion(acct, FakeSession(), "p", "m", False, False)
    assert excinfo.value.status_code == 502
    assert not acct.broken
