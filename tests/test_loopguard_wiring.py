"""Loop guard wiring: a looping upstream must be stopped, not streamed forever.

The guard is tested on its own in ``test_loopguard.py``; these tests cover the
places that read a DeepSeek stream, which is where the request used to hang.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_retry import OK_SSE, FakeAccount, FakeResp, FakeSession

import danyapi.api.deepseek as deepseek_mod
import danyapi.api.openai as openai_mod
import danyapi.api.streaming as streaming_mod
from danyapi.api.deepseek import _collect_continuation
from danyapi.api.openai import ChatMessage, _collect_non_stream, _stream_openai

PIPE = chr(124)
LOOP_LINE = "<" + PIPE * 2 + "DSML" + PIPE * 2
NL = chr(10)


def _event(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False) + NL + NL


def _loop_sse(*, with_prefix=False, lines=60):
    """An SSE body that repeats one line many times, like a looping upstream."""
    initial = "real answer" if with_prefix else ""
    body = "event: ready" + NL
    body += _event({"request_message_id": 1, "response_message_id": 2, "model_type": "default"})
    body += _event(
        {
            "v": {
                "response": {
                    "message_id": 2,
                    "parent_id": 1,
                    "status": "WIP",
                    "fragments": [{"id": 2, "type": "RESPONSE", "content": initial}],
                }
            }
        }
    )
    for _ in range(lines):
        body += _event({"p": "response/fragments/-1/content", "o": "APPEND", "v": NL})
        body += _event({"p": "response/fragments/-1/content", "o": "APPEND", "v": LOOP_LINE})
    body += _event({"p": "response/status", "o": "SET", "v": "FINISHED"})
    return body


def _args(acct, pool=None, existing_sid="s1"):
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


async def _collect(gen):
    return [item async for item in gen]


@pytest.fixture(autouse=True)
def zero_backoff():
    orig = openai_mod.RETRY_BACKOFF_SEC
    openai_mod.RETRY_BACKOFF_SEC = 0.0
    yield
    openai_mod.RETRY_BACKOFF_SEC = orig


async def test_stream_loop_truncates_and_stops_upstream():
    acct = FakeAccount([_loop_sse()])
    acct.client.stop_stream = AsyncMock()
    joined = "".join(await _collect(_stream_openai(**_args(acct))))
    assert '"x_loop_truncated": true' in joined
    assert joined.rstrip().endswith("data: [DONE]")
    acct.client.stop_stream.assert_awaited()


async def test_stream_loop_keeps_partial_answer():
    acct = FakeAccount([_loop_sse(with_prefix=True)])
    acct.client.stop_stream = AsyncMock()
    joined = "".join(await _collect(_stream_openai(**_args(acct))))
    assert "real answer" in joined
    assert '"x_loop_truncated": true' in joined


async def test_stream_without_loop_has_no_truncation_flag():
    acct = FakeAccount([OK_SSE])
    joined = "".join(await _collect(_stream_openai(**_args(acct))))
    assert "x_loop_truncated" not in joined


async def test_stream_loop_guard_disabled_streams_everything(monkeypatch):
    monkeypatch.setattr(streaming_mod.settings, "loop_guard", False)
    acct = FakeAccount([_loop_sse(lines=20)])
    acct.client.stop_stream = AsyncMock()
    joined = "".join(await _collect(_stream_openai(**_args(acct))))
    assert "x_loop_truncated" not in joined
    acct.client.stop_stream.assert_not_awaited()


async def test_non_stream_loop_truncates_result():
    acct = FakeAccount([_loop_sse()])
    acct.client.stop_stream = AsyncMock()
    result = await _collect_non_stream(**_args(acct))
    assert result["x_loop_truncated"] is True
    acct.client.stop_stream.assert_awaited()


async def test_non_stream_without_loop_has_no_flag():
    acct = FakeAccount([OK_SSE])
    result = await _collect_non_stream(**_args(acct))
    assert "x_loop_truncated" not in result


async def test_non_stream_loop_guard_disabled(monkeypatch):
    monkeypatch.setattr(streaming_mod.settings, "loop_guard", False)
    acct = FakeAccount([_loop_sse(lines=20)])
    acct.client.stop_stream = AsyncMock()
    result = await _collect_non_stream(**_args(acct))
    assert "x_loop_truncated" not in result
    acct.client.stop_stream.assert_not_awaited()


async def test_continuation_loop_stops_upstream():
    acct = FakeAccount([_loop_sse()])
    acct.client.stop_stream = AsyncMock()
    rec = await _collect_continuation(
        acct,
        FakeSession(sid="s1"),
        None,
        "default",
        False,
        False,
        request_id="req1",
        model="deepseek-v4-flash",
    )
    assert rec is not None
    acct.client.stop_stream.assert_awaited()


async def test_continuation_without_loop_does_not_stop():
    acct = FakeAccount([OK_SSE])
    acct.client.stop_stream = AsyncMock()
    rec = await _collect_continuation(
        acct,
        FakeSession(sid="s1"),
        None,
        "default",
        False,
        False,
        request_id="req1",
        model="deepseek-v4-flash",
    )
    assert rec is not None and rec.content == "Привет"
    acct.client.stop_stream.assert_not_awaited()


async def test_continuation_loop_guard_disabled(monkeypatch):
    monkeypatch.setattr(deepseek_mod.settings, "loop_guard", False)
    acct = FakeAccount([_loop_sse(lines=20)])
    acct.client.stop_stream = AsyncMock()
    await _collect_continuation(
        acct,
        FakeSession(sid="s1"),
        None,
        "default",
        False,
        False,
        request_id="req1",
        model="deepseek-v4-flash",
    )
    acct.client.stop_stream.assert_not_awaited()


def test_new_loop_guard_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(streaming_mod.settings, "loop_guard", False)
    monkeypatch.setattr(deepseek_mod.settings, "loop_guard", False)
    assert streaming_mod._new_loop_guard() is None
    assert deepseek_mod._new_loop_guard() is None


def test_loop_hit_ignores_none_guard_and_empty_delta():
    guard = streaming_mod._new_loop_guard()
    assert guard is not None
    assert streaming_mod._loop_hit(None, "text", where="x", session_key=None, request_id=None) is False
    assert streaming_mod._loop_hit(guard, "", where="x", session_key=None, request_id=None) is False
    assert deepseek_mod._loop_hit(None, "text", where="x", session_key=None, request_id=None) is False
    assert deepseek_mod._loop_hit(guard, "", where="x", session_key=None, request_id=None) is False


def test_loop_hit_returns_false_when_no_loop_yet():
    guard = streaming_mod._new_loop_guard()
    assert guard is not None
    assert streaming_mod._loop_hit(guard, "one" + NL, where="x", session_key=None, request_id=None) is False


async def test_compact_stream_loop_stops_upstream(monkeypatch):
    """The compact-retry stream must also stop a looping upstream."""
    acct = FakeAccount([_loop_sse()])
    acct.client.stop_stream = AsyncMock()

    async def fake_send(*args, **kwargs):
        return FakeResp(_loop_sse())

    monkeypatch.setattr(streaming_mod, "_send_with_auth", fake_send)
    monkeypatch.setattr(streaming_mod, "_fresh_pow_headers", AsyncMock(return_value={}))

    messages = [
        ChatMessage(role="user", content="the earlier question"),
        ChatMessage(role="assistant", content="the earlier answer"),
        ChatMessage(role="user", content="the current question"),
    ]
    gen = await streaming_mod._compact_retry_stream(
        acct,
        MagicMock(),
        "old",
        messages,
        "default",
        False,
        False,
        None,
        False,
        None,
        False,
        None,
        "req1",
        None,
        "chatcmpl-x",
        0,
        "deepseek-v4-flash",
    )
    assert gen is not None
    await _collect(gen)
    acct.client.stop_stream.assert_awaited()
