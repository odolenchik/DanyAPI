"""Line-coverage tests for the z.ai Playwright driver (``danyapi/zai/browser.py``).

The driver is exercised against a mocked Playwright: ``async_playwright`` is
replaced by a fake factory whose ``start()`` returns a fake Playwright object
exposing ``chromium.launch_persistent_context`` as an ``AsyncMock``. No real
browser is ever launched.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.zai.browser as browser_mod
from danyapi.zai.browser import ZaiBrowser, ZaiBrowserError, _parse_zai_sse_line, parse_sse_body

OK_BODY = 'data: {"type":"chat:completion","data":{"delta_content":"Hello","phase":"answer"}}\n\n'

# wire id of the default model (glm-5.3-flash); pre-setting it makes
# ``_select_model`` a no-op so tests do not need a scriptable picker.
DEFAULT_WIRE = "x-preview-l"

COMPLETION_URL = "https://chat.z.ai/api/chat/completions"


def _page_stub() -> MagicMock:
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()
    page.reload = AsyncMock()
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    waiter = MagicMock()
    waiter.wait_for = AsyncMock()
    waiter.fill = AsyncMock()
    waiter.click = AsyncMock()
    locator = MagicMock()
    locator.first = waiter
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=locator)
    return page


def _context_stub(page: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.add_init_script = AsyncMock()
    ctx.expose_binding = AsyncMock()
    ctx.add_cookies = AsyncMock()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()
    ctx.pages = []
    return ctx


def _pw_stub(ctx: MagicMock) -> MagicMock:
    pw = MagicMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=ctx)
    pw.stop = AsyncMock()
    return pw


def _playwright_factory(pw: MagicMock) -> MagicMock:
    """Fake ``async_playwright``: calling it returns an object with async start()."""
    starter = MagicMock()
    starter.start = AsyncMock(return_value=pw)
    return MagicMock(return_value=starter)


def _armed_browser(page: MagicMock, timeout: float = 5.0) -> ZaiBrowser:
    browser = ZaiBrowser(token="", timeout=timeout)
    browser._started = True
    browser._page = page
    browser._model_wire = DEFAULT_WIRE
    return browser


def _response(body: str | Exception, method: str = "POST", url: str = COMPLETION_URL) -> SimpleNamespace:
    text = AsyncMock(return_value=body) if not isinstance(body, Exception) else AsyncMock(side_effect=body)
    return SimpleNamespace(request=SimpleNamespace(method=method), url=url, text=text)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_line_returns_none_for_non_completion_event():
    assert _parse_zai_sse_line('data: {"type":"other","data":{}}') is None


def test_parse_sse_body_flushes_trailing_partial_line():
    # No trailing newline: the event is only emitted by ``finish()``.
    body = 'data: {"type":"chat:completion","data":{"delta_content":"z","phase":"answer"}}'
    acc = parse_sse_body(body)
    assert acc.content == "z"


# ---------------------------------------------------------------------------
# page property
# ---------------------------------------------------------------------------


def test_page_property_exposes_underlying_page():
    browser = ZaiBrowser(token="")
    assert browser.page is None
    page = _page_stub()
    browser._page = page
    assert browser.page is page


# ---------------------------------------------------------------------------
# _dismiss_modals()
# ---------------------------------------------------------------------------


def _page_with_overlays(counts):
    """Page stub whose ``locator(overlay).count()`` walks ``counts`` in order."""
    page = _page_stub()
    seq = list(counts)
    page.locator.return_value.count = AsyncMock(side_effect=lambda: seq.pop(0) if seq else 0)
    return page


@pytest.mark.asyncio
async def test_dismiss_modals_without_page_is_false():
    browser = ZaiBrowser(token="")

    assert await browser._dismiss_modals() is False


@pytest.mark.asyncio
async def test_dismiss_modals_no_overlay_is_false():
    page = _page_with_overlays([0])
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is False
    page.keyboard.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_dismiss_modals_escape_clears_overlay():
    page = _page_with_overlays([1, 0])
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is True
    page.keyboard.press.assert_awaited_once_with("Escape")
    page.evaluate.assert_not_awaited()  # Escape sufficed, no DOM surgery


@pytest.mark.asyncio
async def test_dismiss_modals_falls_back_to_removing_nodes():
    page = _page_with_overlays([1, 1])
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is True
    page.keyboard.press.assert_awaited_once_with("Escape")
    page.evaluate.assert_awaited_once()
    assert "modal-overlay" in page.evaluate.await_args.args[0]


@pytest.mark.asyncio
async def test_dismiss_modals_treats_count_failure_as_no_modal():
    page = _page_stub()
    page.locator.return_value.count = AsyncMock(side_effect=RuntimeError("gone"))
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is False


@pytest.mark.asyncio
async def test_dismiss_modals_survives_escape_failure():
    page = _page_with_overlays([1])
    page.keyboard.press = AsyncMock(side_effect=RuntimeError("no keyboard"))
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is True
    # Escape raised -> count treated as still blocking -> DOM removal attempted.
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_dismiss_modals_swallows_removal_failure():
    page = _page_with_overlays([1, 1])
    page.evaluate = AsyncMock(side_effect=RuntimeError("blocked"))
    browser = _armed_browser(page)

    assert await browser._dismiss_modals() is True


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_started(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(browser_mod, "async_playwright", factory)
    browser = ZaiBrowser(token="")
    browser._started = True

    await browser.start()

    factory.assert_not_called()
    assert browser._pw is None


@pytest.mark.asyncio
async def test_start_raises_when_playwright_missing(monkeypatch):
    monkeypatch.setattr(browser_mod, "async_playwright", None)
    browser = ZaiBrowser(token="")

    with pytest.raises(ZaiBrowserError, match="playwright is not installed"):
        await browser.start()


@pytest.mark.asyncio
async def test_start_launches_persistent_context_without_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ZAI_BROWSER_DIR", str(tmp_path / "profile"))
    page = _page_stub()
    ctx = _context_stub(page)
    ctx.pages = []  # no page yet -> new_page() is used
    pw = _pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", _playwright_factory(pw))

    browser = ZaiBrowser(token="")
    await browser.start()

    assert browser._started is True
    assert browser._pw is pw
    assert browser._context is ctx
    assert browser._page is page
    ctx.add_cookies.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    ctx.expose_binding.assert_awaited()
    page.wait_for_timeout.assert_awaited_with(4000)
    page.locator.return_value.first.wait_for.assert_awaited()


@pytest.mark.asyncio
async def test_start_uses_existing_page_and_injects_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ZAI_BROWSER_DIR", str(tmp_path / "profile"))
    page = _page_stub()
    ctx = _context_stub(page)
    ctx.pages = [page]  # reuse the page the persistent context already opened
    pw = _pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", _playwright_factory(pw))

    browser = ZaiBrowser(token="tok")
    await browser.start()

    assert browser._page is page
    ctx.new_page.assert_not_awaited()
    ctx.add_cookies.assert_awaited_once()
    page.evaluate.assert_awaited_once()
    page.reload.assert_awaited_once()
    assert [c.args[0] for c in page.wait_for_timeout.await_args_list] == [4000, 8000]


@pytest.mark.asyncio
async def test_start_swallows_cookie_and_storage_injection_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("ZAI_BROWSER_DIR", str(tmp_path / "profile"))
    page = _page_stub()
    page.evaluate = AsyncMock(side_effect=RuntimeError("storage blocked"))
    ctx = _context_stub(page)
    ctx.add_cookies = AsyncMock(side_effect=RuntimeError("cookie rejected"))
    pw = _pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", _playwright_factory(pw))

    browser = ZaiBrowser(token="tok")
    await browser.start()

    assert browser._started is True
    page.reload.assert_not_awaited()


# ---------------------------------------------------------------------------
# aclose()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_context_and_stops_playwright():
    browser = ZaiBrowser(token="")
    browser._started = True
    browser._context = MagicMock(close=AsyncMock())
    browser._pw = MagicMock(stop=AsyncMock())

    await browser.aclose()

    assert browser._started is False
    browser._context.close.assert_awaited_once()
    browser._pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_swallows_shutdown_errors():
    browser = ZaiBrowser(token="")
    browser._started = True
    browser._context = MagicMock(close=AsyncMock(side_effect=RuntimeError("gone")))
    browser._pw = MagicMock(stop=AsyncMock(side_effect=RuntimeError("gone")))

    await browser.aclose()

    assert browser._started is False
    browser._context.close.assert_awaited_once()
    browser._pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_without_resources_is_a_noop():
    browser = ZaiBrowser(token="")
    browser._started = True

    await browser.aclose()

    assert browser._started is False


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_parsed_body():
    page = _page_stub()
    browser = _armed_browser(page)

    async def fill(prompt, *args, **kwargs):
        callback = page.on.call_args[0][1]
        await callback(_response(OK_BODY))

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    acc = await browser.complete("hi", None)

    assert acc.content == "Hello"
    assert page.locator.return_value.first.click.await_count == 1
    page.remove_listener.assert_called_once()


@pytest.mark.asyncio
async def test_complete_starts_browser_when_not_started():
    page = _page_stub()
    browser = ZaiBrowser(token="", timeout=5.0)

    async def fake_start() -> None:
        browser._started = True
        browser._page = page
        browser._model_wire = DEFAULT_WIRE

    browser.start = AsyncMock(side_effect=fake_start)

    async def fill(prompt, *args, **kwargs):
        callback = page.on.call_args[0][1]
        await callback(_response(OK_BODY))

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    acc = await browser.complete("hi", None)

    browser.start.assert_awaited_once()
    assert acc.content == "Hello"


@pytest.mark.asyncio
async def test_complete_records_response_body_error():
    page = _page_stub()
    browser = _armed_browser(page)

    async def fill(prompt, *args, **kwargs):
        callback = page.on.call_args[0][1]
        await callback(_response(RuntimeError("body gone")))

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    acc = await browser.complete("hi", None)

    assert acc.content == ""


@pytest.mark.asyncio
async def test_complete_ignores_non_matching_responses_then_times_out():
    page = _page_stub()
    browser = _armed_browser(page, timeout=0.0)

    async def fill(prompt, *args, **kwargs):
        callback = page.on.call_args[0][1]
        await callback(_response(OK_BODY, method="GET"))
        await callback(_response(OK_BODY, url="https://chat.z.ai/api/other"))

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    with pytest.raises(ZaiBrowserError, match=re.escape("no chat.completions response captured")):
        await browser.complete("hi", None)

    page.remove_listener.assert_called_once()


# ---------------------------------------------------------------------------
# complete_stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_stream_yields_events_then_flushes_tail(monkeypatch):
    monkeypatch.setattr(browser_mod, "STREAM_POLL_SEC", 0.001)
    page = _page_stub()
    browser = _armed_browser(page, timeout=5.0)
    first = 'data: {"type":"chat:completion","data":{"delta_content":"Hello","phase":"answer"}}\n'
    tail = 'data: {"type":"chat:completion","data":{"delta_content":" world","phase":"answer"}}'

    async def fill(prompt, *args, **kwargs):
        queue = browser._stream_queue
        assert queue is not None
        loop = asyncio.get_running_loop()
        # First poll(s) see an empty queue (exercising the timeout/continue path)
        # before the tap delivers a whole line, a partial line, then EOF.
        loop.call_later(0.05, queue.put_nowait, first)
        loop.call_later(0.1, queue.put_nowait, tail)
        loop.call_later(0.15, queue.put_nowait, "")

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    events = [event async for event in browser.complete_stream("hi", None)]

    assert [e["delta_content"] for e in events] == ["Hello", " world"]
    assert browser._stream_queue is None
    page.locator.return_value.first.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_stream_ends_gracefully_on_timeout(monkeypatch):
    monkeypatch.setattr(browser_mod, "STREAM_POLL_SEC", 0.001)
    page = _page_stub()
    browser = _armed_browser(page, timeout=0.0)

    events = [event async for event in browser.complete_stream("hi", None)]

    assert events == []
    assert browser._stream_queue is None


@pytest.mark.asyncio
async def test_complete_stream_starts_browser_when_not_started(monkeypatch):
    monkeypatch.setattr(browser_mod, "STREAM_POLL_SEC", 0.001)
    page = _page_stub()
    browser = ZaiBrowser(token="", timeout=0.0)

    async def fake_start() -> None:
        browser._started = True
        browser._page = page
        browser._model_wire = DEFAULT_WIRE

    browser.start = AsyncMock(side_effect=fake_start)

    events = [event async for event in browser.complete_stream("hi", None)]

    browser.start.assert_awaited_once()
    assert events == []


@pytest.mark.asyncio
async def test_complete_polls_until_response_arrives(monkeypatch):
    page = _page_stub()
    browser = _armed_browser(page, timeout=5.0)
    polls = {"n": 0}

    async def fill(prompt, *args, **kwargs):
        callback = page.on.call_args[0][1]
        previous = asyncio.sleep

        async def one_shot(delay):
            # Deliver the captured response on the first poll so the loop body
            # runs exactly once and then exits on the re-check.
            asyncio.sleep = previous
            polls["n"] += 1
            await callback(_response(OK_BODY))

        asyncio.sleep = one_shot

    page.locator.return_value.first.fill = AsyncMock(side_effect=fill)

    acc = await browser.complete("hi", None)

    assert acc.content == "Hello"
    assert polls["n"] == 1
