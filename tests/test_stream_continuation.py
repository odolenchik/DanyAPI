"""A stream cut short before FINISHED must be resumed, not reported as success.

DeepSeek marks a completed turn with ``response/status = FINISHED``. When the
connection drops mid-answer the stream simply closes, and without this check
the client receives a truncated reply with ``finish_reason: stop`` — which
looks exactly like a finished answer and is therefore never retried.
"""

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

import danyapi.api.openai as openai_mod
from danyapi.api.deepseek import stream_was_interrupted
from danyapi.api.openai import _stream_openai
from danyapi.deepseek.stream import MessageReconstructor
from tests.test_retry import FakeAccount, _args, _collect

# Content, then the stream just stops: no FINISHED, no hint, no notice.
TRUNCATED_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":2,"parent_id":1,"status":"WIP",'
    '"fragments":[{"id":2,"type":"RESPONSE","content":"Первая половина"}]}}}\n'
    "\n"
)

# The continuation reply carries the rest of the answer and finishes cleanly.
CONTINUATION_SSE = (
    "event: ready\n"
    'data: {"request_message_id":3,"response_message_id":4,"model_type":"default"}\n'
    "\n"
    'data: {"v":{"response":{"message_id":4,"parent_id":3,"status":"WIP",'
    '"fragments":[{"id":4,"type":"RESPONSE","content":" и вторая половина."}]}}}\n'
    "\n"
    'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n'
    "\n"
)


def _rec(status=None, content="", reasoning="", hint=None, notice=None):
    rec = MessageReconstructor()
    if status is not None:
        rec.message["status"] = status
    if content or reasoning:
        fragments = []
        if content:
            fragments.append({"type": "RESPONSE", "content": content})
        if reasoning:
            fragments.append({"type": "THINK", "content": reasoning})
        rec.message["fragments"] = fragments
    rec.hint_error = hint
    rec.notice = notice
    return rec


def test_finished_is_not_an_interruption():
    assert stream_was_interrupted(_rec(status="FINISHED", content="ok")) is False


def test_missing_status_with_content_is_an_interruption():
    assert stream_was_interrupted(_rec(content="half an answer")) is True


@pytest.mark.parametrize("status", ["INCOMPLETE", "TIMEOUT"])
def test_explicit_cut_statuses_are_interruptions(status):
    assert stream_was_interrupted(_rec(status=status, content="half")) is True


@pytest.mark.parametrize("status", ["CONTEXT_LENGTH_EXCEEDED", "input_exceeds_limit"])
def test_limit_statuses_are_not_interruptions(status):
    """These have their own recovery paths; retrying would repeat the failure."""
    assert stream_was_interrupted(_rec(status=status)) is False


def test_hint_is_not_an_interruption():
    """A refusal or error event carries its own message for the client."""
    rec = _rec(status="WIP", hint={"message": "Content violates terms.", "finish_reason": None})
    assert stream_was_interrupted(rec) is False


def test_notice_is_not_an_interruption():
    rec = _rec(status="WIP", notice={"message": "Message not sent.", "finish_reason": None})
    assert stream_was_interrupted(rec) is False


async def test_truncated_stream_is_continued(monkeypatch):
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    acct = FakeAccount([TRUNCATED_SSE, CONTINUATION_SSE])
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert acct.client.completion.await_count == 2, "the cut stream should be resumed"
    assert "Первая половина" in joined
    assert "и вторая половина." in joined
    assert '"finish_reason": "stop"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_truncated_stream_keeps_content_when_continuation_is_empty(monkeypatch):
    """A resume that produces nothing must not swallow the text already sent."""
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    from tests.test_retry import EMPTY_SSE

    acct = FakeAccount([TRUNCATED_SSE, EMPTY_SSE])
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert acct.client.completion.await_count == 2, "the resume should have been attempted"
    assert "Первая половина" in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_auto_continue_can_be_disabled(monkeypatch):
    monkeypatch.setattr(openai_mod.settings, "auto_continue", False)
    acct = FakeAccount([TRUNCATED_SSE, CONTINUATION_SSE])
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert acct.client.completion.await_count == 1, "the flag must suppress the resume"
    assert "Первая половина" in joined
    assert "вторая половина" not in joined


async def test_finished_stream_is_not_continued(monkeypatch):
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    from tests.test_retry import OK_SSE

    acct = FakeAccount([OK_SSE, CONTINUATION_SSE])
    await _collect(_stream_openai(**_args(acct)))

    assert acct.client.completion.await_count == 1


async def test_self_aborted_stream_is_not_continued(monkeypatch):
    """The degenerate guard stops on purpose; resuming would re-ask for the same output."""
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    from tests.test_retry import _degen_sse

    acct = FakeAccount([_degen_sse(), CONTINUATION_SSE])
    args = _args(acct)
    args["tool_mode"] = True
    args["tool_schemas"] = {}
    await _collect(_stream_openai(**args))

    assert acct.client.completion.await_count == 1


# ---------------------------------------------------------------------------
# Transport drops: httpx reports these as TransportError (RemoteProtocolError
# and friends), which inherits from neither ConnectionError nor OSError. The
# handler used to catch only the builtins, so a real disconnect escaped the
# generator: the client got a few chunks and then a silent dead stream.
# ---------------------------------------------------------------------------


class _DroppingResp:
    """Yield the given SSE once, then fail the way httpx does on a real drop."""

    status_code = 200
    headers: ClassVar[dict] = {"content-type": "text/event-stream; charset=utf-8"}

    def __init__(self, sse: str, exc: Exception) -> None:
        self._b = sse.encode()
        self._exc = exc
        self.closed = False

    async def aiter_bytes(self):
        yield self._b
        raise self._exc

    async def aclose(self):
        self.closed = True


def _dropping_account(sse: str, exc: Exception, follow_up: str | None = None) -> FakeAccount:
    acct = FakeAccount([sse])
    responses = [_DroppingResp(sse, exc)]
    if follow_up is not None:
        from tests.test_retry import FakeResp

        responses.append(FakeResp(follow_up))
    acct.client.completion = AsyncMock(side_effect=responses)
    acct.client.stop_stream = AsyncMock()
    return acct


async def test_transport_drop_does_not_escape_the_generator(monkeypatch):
    """A real httpx disconnect must be handled, not raised at the consumer."""
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    import httpx

    acct = _dropping_account(
        TRUNCATED_SSE,
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        follow_up=CONTINUATION_SSE,
    )
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert "Первая половина" in joined
    assert "и вторая половина." in joined
    assert '"error"' not in joined
    assert joined.rstrip().endswith("data: [DONE]")
    assert acct.client.completion.await_count == 2, "the drop should be resumed"


async def test_transport_drop_without_resume_closes_the_stream(monkeypatch):
    """With continuation disabled the client still gets a terminated stream."""
    monkeypatch.setattr(openai_mod.settings, "auto_continue", False)
    import httpx

    acct = _dropping_account(
        TRUNCATED_SSE,
        httpx.ReadError("connection reset by peer"),
    )
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert "Первая половина" in joined
    assert '"error"' in joined
    assert joined.rstrip().endswith("data: [DONE]")


async def test_continuation_drop_keeps_partial_text(monkeypatch):
    """If the resume itself drops, the turn ends cleanly with what it has."""
    monkeypatch.setattr(openai_mod.settings, "auto_continue", True)
    import httpx

    acct = _dropping_account(
        TRUNCATED_SSE,
        httpx.RemoteProtocolError("dropped"),
        follow_up=CONTINUATION_SSE,
    )
    # The follow-up drops too: no text from the resume at all.
    acct.client.completion = AsyncMock(
        side_effect=[
            _DroppingResp(TRUNCATED_SSE, httpx.RemoteProtocolError("dropped")),
            _DroppingResp("", httpx.RemoteProtocolError("dropped again")),
        ]
    )
    lines = await _collect(_stream_openai(**_args(acct)))
    joined = "".join(lines)

    assert "Первая половина" in joined
    assert joined.rstrip().endswith("data: [DONE]")
