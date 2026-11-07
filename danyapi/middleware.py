"""ASGI middleware: request correlation and a last-resort error barrier.

Kept dependency-light (stdlib + starlette types) so both pieces are testable
without a running server and without importing FastAPI.

Registration order matters: with Starlette the LAST middleware added runs
FIRST, so add Recoverer *after* RequestID to have Recoverer wrap it — an
exception raised anywhere below then still carries the request id.
"""

from __future__ import annotations

import json
import logging
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging import request_id_var

log = logging.getLogger("danyapi.middleware")

REQUEST_ID_HEADER = b"x-request-id"


class RequestIdMiddleware:
    """Give every request a correlation id, expose it and put it in the logs.

    An inbound ``X-Request-Id`` is honoured only when it is a sane token: it is
    client-controlled, so anything long, empty or containing control characters
    is replaced with a fresh id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # Streaming handlers set the header themselves; adding a second
                # copy would send it twice. Keep an existing value — it is the
                # same id this request already generated.
                if not any(key.lower() == REQUEST_ID_HEADER for key, _value in headers):
                    headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)


class ExceptionBarrierMiddleware:
    """Turn an unhandled exception into a logged JSON 500.

    Without this an exception inside a handler reaches the client as a bare
    connection drop or a server-specific page, and nothing records the request
    id it belonged to. The response keeps ``X-Request-Id`` so the log line can
    be matched to what the caller saw.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            request_id = request_id_var.get()
            log.exception(
                "unhandled error on %s %s (request=%s)",
                scope.get("method"),
                scope.get("path"),
                request_id,
            )
            if response_started:
                # The status line is already on the wire; all we can do is stop.
                return
            body = json.dumps(
                {
                    "error": {
                        "message": "internal server error",
                        "type": "internal_error",
                        "request_id": request_id,
                    }
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (REQUEST_ID_HEADER, request_id.encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


def _inbound_request_id(scope: Scope) -> str | None:
    """The client-supplied id when it is safe to echo, else None."""
    for name, value in scope.get("headers") or []:
        if name.lower() != REQUEST_ID_HEADER:
            continue
        try:
            text = value.decode("latin-1").strip()
        except (UnicodeDecodeError, AttributeError):
            return None
        if not text or len(text) > 128:
            return None
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            return None
        return text
    return None
