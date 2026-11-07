"""Middleware: correlation id and the last-resort error barrier.

Both pieces are exercised against a bare ASGI app, so the tests do not depend
on FastAPI, the router, or a running server.
"""

from __future__ import annotations

import json
import logging

import pytest

from danyapi.logging import DEFAULT_FORMAT, RequestIdFilter, request_id_var
from danyapi.middleware import (
    ExceptionBarrierMiddleware,
    RequestIdMiddleware,
    _inbound_request_id,
)

SCOPE = {
    "type": "http",
    "method": "GET",
    "path": "/probe",
    "headers": [],
}


def http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    scope = dict(SCOPE)
    scope["headers"] = list(headers or [])
    return scope


async def noop_receive() -> dict:
    return {"type": "http.request"}


class Recorder:
    """Collect the messages an app sends."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def start(self) -> dict:
        return next(m for m in self.messages if m["type"] == "http.response.start")

    def header(self, name: bytes) -> bytes | None:
        for key, value in self.start.get("headers") or []:
            if key.lower() == name:
                return value
        return None

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")


async def ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


# --- RequestIdMiddleware ----------------------------------------------------


async def test_response_carries_a_generated_request_id():
    send = Recorder()
    await RequestIdMiddleware(ok_app)(http_scope(), noop_receive, send)

    request_id = send.header(b"x-request-id")
    assert request_id is not None
    assert len(request_id.decode()) == 32


async def test_context_var_is_set_during_the_request_and_restored_after():
    seen = {}

    async def probe(scope, receive, send):
        seen["id"] = request_id_var.get()
        await ok_app(scope, receive, send)

    send = Recorder()
    await RequestIdMiddleware(probe)(http_scope(), noop_receive, send)

    assert seen["id"] == send.header(b"x-request-id").decode()
    assert request_id_var.get() == "-", "the id must not leak past the request"


async def test_inbound_id_is_honoured_when_sane():
    send = Recorder()
    scope = http_scope([(b"x-request-id", b"client-supplied-123")])
    await RequestIdMiddleware(ok_app)(scope, noop_receive, send)

    assert send.header(b"x-request-id") == b"client-supplied-123"


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"   ",
        b"a" * 200,
        b"bad\x00id",
        b"with\nnewline",
    ],
)
async def test_unsafe_inbound_id_is_replaced(bad):
    send = Recorder()
    scope = http_scope([(b"x-request-id", bad)])
    await RequestIdMiddleware(ok_app)(scope, noop_receive, send)

    generated = send.header(b"x-request-id").decode()
    assert generated != bad.decode("latin-1", errors="replace")
    assert len(generated) == 32


async def test_handler_set_request_id_is_not_duplicated():
    """A streaming handler sets the header itself; the middleware must not add a second."""

    async def handler_sets_header(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-request-id", b"handler-id")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    send = Recorder()
    await RequestIdMiddleware(handler_sets_header)(http_scope(), noop_receive, send)

    values = [value for key, value in send.start["headers"] if key.lower() == b"x-request-id"]
    assert values == [b"handler-id"], "the header must appear exactly once"


async def test_non_http_scope_is_passed_through():
    sentinel = {"type": "lifespan"}

    async def app(scope, receive, send):
        sentinel["seen"] = scope["type"]

    async def send(_message):
        raise AssertionError("lifespan must not send")

    await RequestIdMiddleware(app)(sentinel, noop_receive, send)
    assert sentinel["seen"] == "lifespan"


# --- ExceptionBarrierMiddleware --------------------------------------------


async def test_exception_becomes_a_json_500_with_request_id():
    async def boom(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("kaboom")

    async def failing_before_start(scope, receive, send):
        raise RuntimeError("kaboom")

    send = Recorder()
    inner = RequestIdMiddleware(ExceptionBarrierMiddleware(failing_before_start))
    await inner(http_scope(), noop_receive, send)

    assert send.start["status"] == 500
    assert send.header(b"content-type") == b"application/json"
    payload = json.loads(send.body)
    assert payload["error"]["type"] == "internal_error"
    assert payload["error"]["request_id"] == send.header(b"x-request-id").decode()


async def test_exception_after_response_started_does_not_rewrite_it():
    async def partial(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial"})
        raise RuntimeError("late failure")

    send = Recorder()
    await ExceptionBarrierMiddleware(partial)(http_scope(), noop_receive, send)

    starts = [m for m in send.messages if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200


async def test_barrier_logs_the_failure_with_the_request_id(caplog):
    async def boom(scope, receive, send):
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger="danyapi.middleware"):
        send = Recorder()
        await RequestIdMiddleware(ExceptionBarrierMiddleware(boom))(http_scope(), noop_receive, send)

    assert any("unhandled error on GET /probe" in rec.message for rec in caplog.records)


async def test_barrier_passes_non_http_through():
    seen = {}

    async def app(scope, receive, send):
        seen["type"] = scope["type"]

    await ExceptionBarrierMiddleware(app)({"type": "websocket"}, noop_receive, Recorder())
    assert seen["type"] == "websocket"


# --- log filter -------------------------------------------------------------


def test_request_id_filter_injects_the_current_id():
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "hello", None, None)
    token = request_id_var.set("abc123")
    try:
        assert RequestIdFilter().filter(record) is True
    finally:
        request_id_var.reset(token)

    assert record.request_id == "abc123"


def test_request_id_filter_defaults_outside_a_request():
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "hello", None, None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-"


def test_default_format_renders_with_the_filter():
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "hello", None, None)
    RequestIdFilter().filter(record)
    rendered = logging.Formatter(DEFAULT_FORMAT).format(record)
    assert "[request_id]" not in rendered
    assert "[-]" in rendered
    assert "hello" in rendered


# --- header parsing ---------------------------------------------------------


def test_inbound_request_id_returns_none_without_the_header():
    assert _inbound_request_id(http_scope()) is None
    assert _inbound_request_id(http_scope([(b"accept", b"*/*")])) is None


def test_inbound_request_id_is_case_insensitive():
    assert _inbound_request_id(http_scope([(b"X-Request-Id", b"abc")])) == "abc"
