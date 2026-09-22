"""OpenAI-compatible response assembly for the z.ai browser proxy."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException

from .. import tools as toolemu
from ..config import settings
from .browser import ZaiBrowser, ZaiBrowserError, _CompletionAccumulator
from .models import ZaiModelError

log = __import__("logging").getLogger("danyapi.zai.api")


def _usage_dict(usage: dict) -> dict:
    prompt = int((usage or {}).get("prompt_tokens", 0) or 0)
    completion = int((usage or {}).get("completion_tokens", 0) or 0)
    total = int((usage or {}).get("total_tokens", 0) or 0) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _usage_from(acc: _CompletionAccumulator) -> dict:
    return _usage_dict(getattr(acc, "usage", {}) or {})


async def collect_non_stream(
    browser: ZaiBrowser,
    prompt: str,
    model: str,
    tool_mode: bool = False,
    tool_schemas: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    try:
        acc = await asyncio.wait_for(browser.complete(prompt, model), timeout=settings.timeout + 60)
    except ZaiModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ZaiBrowserError as exc:
        raise HTTPException(502, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "z.ai browser completion timed out") from exc

    if acc.error:
        detail = acc.error.get("detail") or acc.error.get("message") or "z.ai error"
        raise HTTPException(502, str(detail))

    if not acc.content and not acc.reasoning:
        raise HTTPException(502, "z.ai returned an empty completion")

    if tool_mode:
        parsed = toolemu.parse_tool_calls(acc.content, tool_schemas)
        if parsed is not None:
            tool_calls, tool_text = parsed
            message = toolemu.format_tool_message(tool_calls, tool_text, acc.reasoning)
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": acc.content}
            if acc.reasoning:
                message["reasoning_content"] = acc.reasoning
            finish = "stop"
    else:
        message = {"role": "assistant", "content": acc.content}
        if acc.reasoning:
            message["reasoning_content"] = acc.reasoning
        finish = "stop"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _usage_from(acc),
    }


async def stream_openai(
    browser: ZaiBrowser,
    prompt: str,
    model: str,
    include_usage: bool = False,
    tool_mode: bool = False,
    tool_schemas: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """Stream an OpenAI ``chat.completion.chunk`` SSE response for z.ai.

    Mirrors the chunk shape used by the Qwen/DeepSeek paths (role chunk first,
    then incremental ``content`` / ``reasoning_content`` deltas, a final
    finish-reason chunk, optional usage chunk, and ``[DONE]``).

    In ``tool_mode`` the answer text is buffered instead of streamed, because a
    tool call is only recognisable once the whole JSON object has arrived; at the
    end it is parsed into ``tool_calls`` deltas, or emitted as plain content if
    the model answered normally.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    usage: dict = {}
    got_content = False
    error: dict | None = None
    content_buf = ""

    yield sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    try:
        async for data in browser.complete_stream(prompt, model):
            err = data.get("error")
            if isinstance(err, (dict, str)):
                error = err if isinstance(err, dict) else {"detail": str(err)}
                break
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            phase = data.get("phase")
            delta_text = data.get("delta_content") or ""
            delta: dict[str, Any] = {}
            if phase == "thinking" and delta_text:
                delta["reasoning_content"] = delta_text
                got_content = True
            elif delta_text:
                if tool_mode:
                    content_buf += delta_text
                else:
                    delta["content"] = delta_text
                got_content = True
            if delta:
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                )
    except ZaiBrowserError as exc:
        error = {"detail": str(exc)}

    if error is not None and not got_content:
        detail = (error.get("detail") or error.get("message")) if isinstance(error, dict) else "z.ai error"
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "error": {"message": str(detail)},
                "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
            }
        )
    elif not got_content:
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "error": {"message": "z.ai returned an empty completion"},
                "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
            }
        )
    else:
        finish = "stop"
        if tool_mode:
            parsed = toolemu.parse_tool_calls(content_buf, tool_schemas)
            if parsed is not None:
                tool_calls, tool_text = parsed
                for delta in toolemu.tool_call_deltas(tool_calls, tool_text):
                    yield sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        }
                    )
                finish = "tool_calls"
            elif content_buf:
                yield sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": content_buf}, "finish_reason": None}],
                    }
                )
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            }
        )

    if include_usage and (usage or got_content):
        yield sse(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": _usage_dict(usage),
            }
        )
    yield "data: [DONE]\n\n"
