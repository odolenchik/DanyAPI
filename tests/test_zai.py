"""Unit tests for the z.ai browser-proxy helper functions.

These tests exercise only pure parsing / response assembly logic. The real
``ZaiBrowser`` drives Chrome and is exercised manually; it is not covered here.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import danyapi.zai.api as zai_api
from danyapi.zai.browser import ZaiBrowser, ZaiBrowserError, _IncrementalZaiSSE, parse_sse_body

OK_BODY = (
    'data: {"type":"chat:completion","data":{"delta_content":"Think","phase":"thinking"}}\n'
    "\n"
    'data: {"type":"chat:completion","data":{"delta_content":"Hello","phase":"answer"}}\n'
    "\n"
    'data: {"type":"chat:completion","data":{"phase":"other","usage":{"prompt_tokens":19,"completion_tokens":39,"total_tokens":58}}}\n'
    "\n"
    'data: {"type":"chat:completion","data":{"phase":"done","done":true}}\n'
    "\n"
)


def test_parse_sse_body_content_reasoning_usage():
    acc = parse_sse_body(OK_BODY)
    assert acc.content == "Hello"
    assert acc.reasoning == "Think"
    assert acc.usage["total_tokens"] == 58
    assert acc.done is True
    assert acc.error is None


def test_parse_sse_body_error():
    body = 'data: {"type":"chat:completion","data":{"error":{"detail":"boom"}}}\n\n'
    acc = parse_sse_body(body)
    assert acc.error == {"detail": "boom"}


def test_parse_sse_body_ignores_non_data_and_bad_json():
    body = 'data: {"type":"chat:completion","data":{"delta_content":"x","phase":"answer"}}\nnot data\ndata: [DONE]\ndata: not-json\n'
    acc = parse_sse_body(body)
    assert acc.content == "x"


def test_usage_from_empty():
    acc = MagicMock()
    acc.usage = {}
    assert zai_api._usage_from(acc) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_usage_from_partial():
    acc = MagicMock()
    acc.usage = {"prompt_tokens": 2, "completion_tokens": 3}
    assert zai_api._usage_from(acc)["total_tokens"] == 5


@pytest.mark.asyncio
async def test_collect_non_stream_success():
    browser = MagicMock()
    browser.complete = AsyncMock(return_value=parse_sse_body(OK_BODY))

    result = await zai_api.collect_non_stream(
        browser=browser,
        prompt="hi",
        model="glm-5.3",
    )
    message = result["choices"][0]["message"]
    assert message["content"] == "Hello"
    assert message["reasoning_content"] == "Think"
    assert result["model"] == "glm-5.3"
    assert result["usage"]["total_tokens"] == 58


@pytest.mark.asyncio
async def test_collect_non_stream_browser_error():
    browser = MagicMock()
    browser.complete = AsyncMock(side_effect=ZaiBrowserError("no response"))
    with pytest.raises(HTTPException) as excinfo:
        await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3")
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_collect_non_stream_upstream_error():
    browser = MagicMock()
    acc = parse_sse_body(OK_BODY)
    acc.error = {"detail": "upstream bad"}
    browser.complete = AsyncMock(return_value=acc)
    with pytest.raises(HTTPException) as excinfo:
        await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3")
    assert excinfo.value.status_code == 502


TOOL_SCHEMAS = {"Read": {"properties": {"path": {"type": "string"}}, "required": {"path"}}}


def _acc_with(content: str) -> MagicMock:
    acc = MagicMock()
    acc.content = content
    acc.reasoning = ""
    acc.usage = {}
    acc.error = None
    return acc


@pytest.mark.asyncio
async def test_collect_non_stream_tool_call():
    browser = MagicMock()
    browser.complete = AsyncMock(return_value=_acc_with('{"tool_calls": [{"name": "Read", "arguments": {"path": "/tmp/x"}}]}'))

    result = await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3", tool_mode=True, tool_schemas=TOOL_SCHEMAS)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "Read"
    assert json.loads(call["function"]["arguments"]) == {"path": "/tmp/x"}


@pytest.mark.asyncio
async def test_collect_non_stream_tool_mode_plain_text():
    browser = MagicMock()
    browser.complete = AsyncMock(return_value=_acc_with("just an answer"))

    result = await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3", tool_mode=True, tool_schemas=TOOL_SCHEMAS)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "just an answer"
    assert "tool_calls" not in choice["message"]


@pytest.mark.asyncio
async def test_stream_openai_tool_call():
    events = [{"phase": "answer", "delta_content": '{"tool_calls": [{"name": "Read", "arguments": {"path": "/tmp/x"}}]}'}]
    lines = await _collect(zai_api.stream_openai(browser=_fake_browser(events), prompt="hi", model="glm-5.3", tool_mode=True, tool_schemas=TOOL_SCHEMAS))
    payloads = []
    for line in lines[:-1]:
        body = line.strip()
        if body.startswith("data:") and body != "data: [DONE]":
            payloads.append(json.loads(body[len("data:") :].strip()))
    deltas = [p["choices"][0]["delta"] for p in payloads if p.get("choices")]
    names = [tc["function"]["name"] for d in deltas for tc in d.get("tool_calls", []) if tc.get("function", {}).get("name")]
    assert names == ["Read"]
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_stream_openai_tool_mode_plain_text():
    events = [{"phase": "answer", "delta_content": "hello "}, {"phase": "answer", "delta_content": "world"}]
    lines = await _collect(zai_api.stream_openai(browser=_fake_browser(events), prompt="hi", model="glm-5.3", tool_mode=True, tool_schemas=TOOL_SCHEMAS))
    payloads = []
    for line in lines[:-1]:
        body = line.strip()
        if body.startswith("data:") and body != "data: [DONE]":
            payloads.append(json.loads(body[len("data:") :].strip()))
    deltas = [p["choices"][0]["delta"] for p in payloads if p.get("choices")]
    content = "".join(d.get("content", "") for d in deltas)
    assert content == "hello world"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_incremental_parser_matches_batch_under_chunking():
    """Feeding the body in arbitrary (sub-line) chunks must equal batch parsing."""
    parser = _IncrementalZaiSSE()
    events: list[dict] = []
    # worst case: one character per feed call
    for ch in OK_BODY:
        events.extend(parser.feed(ch))
    events.extend(parser.finish())

    content, reasoning, usage, done = "", "", {}, False
    for e in events:
        phase = e.get("phase")
        delta = e.get("delta_content") or ""
        if phase == "thinking":
            reasoning += delta
        elif phase == "answer":
            content += delta
        if isinstance(e.get("usage"), dict):
            usage = e["usage"]
        if e.get("done") or phase == "done":
            done = True

    assert content == "Hello"
    assert reasoning == "Think"
    assert usage.get("total_tokens") == 58
    assert done is True


def test_incremental_parser_handles_crlf_and_bad_lines():
    parser = _IncrementalZaiSSE()
    body = 'data: {"type":"chat:completion","data":{"delta_content":"hi","phase":"answer"}}\r\n\r\ndata: not-json\r\n'
    events = []
    for ch in body:
        events.extend(parser.feed(ch))
    events.extend(parser.finish())
    assert [e.get("delta_content") for e in events] == ["hi"]


def test_stream_callbacks_feed_queue():
    browser = ZaiBrowser(token="")
    queue: asyncio.Queue[str] = asyncio.Queue()
    browser._stream_queue = queue
    browser._on_stream_chunk(None, "data: x\n")
    browser._on_stream_end(None)
    assert queue.get_nowait() == "data: x\n"
    assert queue.get_nowait() == ""


def test_stream_callbacks_ignore_empty_and_inactive():
    browser = ZaiBrowser(token="")
    # no active stream -> callbacks must be no-ops, not raise
    browser._on_stream_chunk(None, "ignored")
    browser._on_stream_end(None)
    queue: asyncio.Queue[str] = asyncio.Queue()
    browser._stream_queue = queue
    browser._on_stream_chunk(None, "")  # empty chunk is skipped
    assert queue.empty()


def _page_stub():
    page = MagicMock()
    page.evaluate = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    waiter = MagicMock()
    waiter.wait_for = AsyncMock()
    locator = MagicMock()
    locator.first = waiter
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=locator)
    return page


@pytest.mark.asyncio
async def test_select_model_applies_on_first_call_and_switches():
    browser = ZaiBrowser(token="")
    browser._page = _page_stub()

    await browser._select_model("glm-5.3")
    assert browser._model_wire == "glm-5.3"
    assert browser._page.goto.await_count == 1

    # Same model again: no reload, nothing to do.
    await browser._select_model("glm-5.3")
    assert browser._page.goto.await_count == 1

    # Different model: write the wire id and start a fresh chat.
    await browser._select_model("glm-5.3-flash")
    assert browser._model_wire == "x-preview-l"
    assert browser._page.goto.await_count == 2
    written = browser._page.evaluate.await_args_list[-1]
    assert written.args[1] == "x-preview-l"


def _fake_browser(events):
    class _B:
        async def complete_stream(self, prompt, model=None):
            for e in events:
                yield e

    return _B()


async def _collect(gen):
    return [line async for line in gen]


@pytest.mark.asyncio
async def test_stream_openai_happy_path_with_usage():
    events = [
        {"phase": "thinking", "delta_content": "Think"},
        {"phase": "answer", "delta_content": "Hello"},
        {"phase": "done", "done": True, "usage": {"prompt_tokens": 19, "completion_tokens": 39, "total_tokens": 58}},
    ]
    lines = await _collect(zai_api.stream_openai(browser=_fake_browser(events), prompt="hi", model="glm-5.3", include_usage=True))

    assert lines[-1] == "data: [DONE]\n\n"
    payloads = []
    for line in lines[:-1]:  # drop the trailing [DONE]
        body = line.strip()
        if body.startswith("data:") and body != "data: [DONE]":
            payloads.append(json.loads(body[len("data:") :].strip()))

    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    deltas = [p["choices"][0].get("delta") for p in payloads if p.get("choices")]
    reasoning = "".join(d.get("reasoning_content", "") for d in deltas)
    content = "".join(d.get("content", "") for d in deltas)
    assert reasoning == "Think"
    assert content == "Hello"

    usage_chunks = [p for p in payloads if "usage" in p]
    assert usage_chunks and usage_chunks[0]["usage"]["total_tokens"] == 58


@pytest.mark.asyncio
async def test_stream_openai_browser_error():
    class _B:
        async def complete_stream(self, prompt, model=None):
            raise ZaiBrowserError("no response")
            yield  # pragma: no cover - marks this as an async generator

    lines = await _collect(zai_api.stream_openai(browser=_B(), prompt="hi", model="glm-5.3"))
    assert lines[-1] == "data: [DONE]\n\n"
    assert any("no response" in line for line in lines)
    assert any('"finish_reason": "error"' in line for line in lines)


@pytest.mark.asyncio
async def test_stream_openai_upstream_error_event():
    events = [{"error": {"detail": "upstream bad"}}]
    lines = await _collect(zai_api.stream_openai(browser=_fake_browser(events), prompt="hi", model="glm-5.3"))
    assert any("upstream bad" in line for line in lines)
    assert any('"finish_reason": "error"' in line for line in lines)


@pytest.mark.asyncio
async def test_stream_openai_empty_completion():
    class _B:
        async def complete_stream(self, prompt, model=None):
            return
            yield

    lines = await _collect(zai_api.stream_openai(browser=_B(), prompt="hi", model="glm-5.3"))
    assert any("empty completion" in line for line in lines)
    assert lines[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_stream_openai_no_usage_by_default():
    events = [{"phase": "answer", "delta_content": "Hi"}]
    lines = await _collect(zai_api.stream_openai(browser=_fake_browser(events), prompt="hi", model="glm-5.3"))
    assert not any('"usage"' in line for line in lines)
