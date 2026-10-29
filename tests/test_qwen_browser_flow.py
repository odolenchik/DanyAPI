"""Line-coverage tests for the Qwen Playwright driver.

The driver is exercised against a mocked Playwright, the same way the z.ai
driver is: ``async_playwright`` is replaced by a fake factory whose ``start()``
returns a fake Playwright exposing ``chromium.launch_persistent_context`` as an
``AsyncMock``. No real browser is ever launched and no network call is made.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.qwen.browser as browser_mod
from danyapi.qwen.browser import QwenBrowser, QwenBrowserError, QwenCaptchaRequired

OK_BODY = (
    'data: {"response.created":{"chat_id":"c-1","response_id":"r"}}\n\n'
    'data: {"choices":[{"delta":{"content":"hi","phase":"answer","status":"typing"}}],'
    '"response_id":"r"}\n\n'
)
WAF_BODY = '{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM"],"data":{"url":"/punish?x5secdata=1"}}'


class _Awaitable:
    """A value that can be awaited repeatedly and yields ``value``.

    ``info.value`` is awaited by the driver, so it cannot be a plain object and
    cannot be an ``AsyncMock`` either: awaiting the mock itself (rather than
    calling it) is a ``TypeError``. A real awaitable is what mirrors Playwright.
    """

    def __init__(self, value) -> None:
        self._value = value

    def __await__(self):
        return self._value
        yield  # pragma: no cover - makes this a generator, so __await__ yields nothing


class _Expect:
    """Stand-in for Playwright's ``expect_response`` async context manager."""

    def __init__(self, response) -> None:
        self._response = response

    async def __aenter__(self):
        return SimpleNamespace(value=_Awaitable(self._response))

    async def __aexit__(self, *exc) -> bool:
        return False


class _LocatorFactory:
    """``page.locator`` stand-in that answers per selector.

    Playwright resolves a locator lazily from a selector, so the driver asks for
    ``button.stop-button`` and ``textarea`` separately and gets different nodes.
    Tests also reach the shared locator through ``return_value``; both paths are
    supported here so the stop-button check cannot be faked by the shared mock.
    """

    def __init__(self, shared, stop) -> None:
        self._shared = shared
        self._stop = stop
        self.return_value = shared

    def __call__(self, selector: str):
        return self._stop if selector == browser_mod.STOP_BUTTON else self._shared

    def assert_called(self) -> None:
        return None


def page_stub(
    *,
    textarea_count: int = 1,
    url: str = "https://chat.qwen.ai/",
    stop_count: int = 0,
) -> MagicMock:
    """Page stand-in with awaitable actions and a scriptable locator."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()
    page.content = AsyncMock(return_value="<html></html>")
    page.is_closed = MagicMock(return_value=False)
    page.url = url

    locator = MagicMock()
    locator.first = locator
    locator.filter.return_value = locator
    locator.count = AsyncMock(return_value=textarea_count)
    locator.click = AsyncMock()
    locator.fill = AsyncMock()
    locator.wait_for = AsyncMock()
    locator.inner_text = AsyncMock(return_value="Create Image")

    # The stop button is its own locator: the driver waits for it to disappear
    # before typing (a busy chat has no send button at all), so "not there" is
    # the idle state every test starts from. Returning the shared locator for it
    # would make every chat look permanently busy.
    stop = MagicMock()
    stop.first = stop
    stop.count = AsyncMock(return_value=stop_count)
    stop.is_visible = AsyncMock(return_value=False)

    # A factory, not ``MagicMock(side_effect=...)``: the other tests reach the
    # shared locator through ``page.locator.return_value``, which a side effect
    # would bypass. This keeps both access styles working.
    page.locator = _LocatorFactory(locator, stop)
    page.stop_locator = stop

    # ``expect_response`` is an async context manager yielding a handle whose
    # ``value`` is awaited. It has to be a real class: special methods are looked
    # up on the type, so assigning ``__aenter__`` to an instance is silently
    # ignored (which is what made every flow test raise TypeError).
    response = MagicMock()
    response.text = AsyncMock(return_value=OK_BODY)
    response.status = 200
    page.response = response
    page.expect_response = MagicMock(side_effect=lambda *a, **k: _Expect(page.response))
    page.get_by_text = MagicMock(return_value=locator)
    return page


def context_stub(page: MagicMock, *, pages: list | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.add_cookies = AsyncMock()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()
    ctx.is_closed = MagicMock(return_value=False)
    ctx.pages = pages if pages is not None else []
    return ctx


def pw_stub(ctx: MagicMock) -> MagicMock:
    pw = MagicMock()
    pw.chromium.launch_persistent_context = AsyncMock(return_value=ctx)
    pw.stop = AsyncMock()
    return pw


def playwright_factory(pw: MagicMock) -> MagicMock:
    starter = MagicMock()
    starter.start = AsyncMock(return_value=pw)
    return MagicMock(return_value=starter)


def armed(page: MagicMock, **kwargs) -> QwenBrowser:
    b = QwenBrowser(token="t", timeout=5, **kwargs)
    b._started = True
    b._page = page
    return b


# --- lifecycle ---


def test_started_property_reflects_state() -> None:
    b = QwenBrowser(token="t")
    assert b.started is False
    b._started = True
    assert b.started is True


async def test_start_opens_the_browser_and_logs_in(monkeypatch, tmp_path) -> None:
    page = page_stub()
    ctx = context_stub(page, pages=[page])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))
    monkeypatch.delenv("QWEN_BROWSER_EXECUTABLE", raising=False)

    b = QwenBrowser(token="refresh-me", timeout=5)
    await b.start()

    assert b.started is True
    ctx.add_cookies.assert_awaited_once()
    cookie = ctx.add_cookies.await_args.args[0][0]
    assert cookie["name"] == "refresh_token"
    assert cookie["value"] == "refresh-me"
    # The parent domain is what makes the cookie reach auth.qwen.ai.
    assert cookie["domain"] == ".qwen.ai"
    page.goto.assert_awaited()
    # A profile directory is created per account.
    assert (tmp_path / "qwen-browser-0").is_dir()


async def test_start_reuses_the_first_page_when_one_exists(monkeypatch, tmp_path) -> None:
    page = page_stub()
    ctx = context_stub(page, pages=[page])
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw_stub(ctx)))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    b = QwenBrowser(token="", timeout=5)
    await b.start()
    ctx.new_page.assert_not_awaited()
    # No token means no cookie injection.
    ctx.add_cookies.assert_not_awaited()


async def test_start_is_idempotent(monkeypatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(browser_mod, "async_playwright", factory)
    b = QwenBrowser(token="t")
    b._started = True
    await b.start()
    factory.assert_not_called()


async def test_start_reports_a_missing_patchright(monkeypatch) -> None:
    monkeypatch.setattr(browser_mod, "async_playwright", None)
    b = QwenBrowser(token="t")
    with pytest.raises(QwenBrowserError, match="patchright is not installed"):
        await b.start()


async def test_start_passes_the_executable_when_configured(monkeypatch, tmp_path) -> None:
    page = page_stub()
    ctx = context_stub(page, pages=[page])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))
    monkeypatch.setenv("QWEN_BROWSER_EXECUTABLE", "/usr/bin/chromium")

    await QwenBrowser(token="", timeout=5).start()
    kwargs = pw.chromium.launch_persistent_context.await_args.kwargs
    assert kwargs["executable_path"] == "/usr/bin/chromium"


async def test_aclose_survives_errors() -> None:
    b = QwenBrowser(token="t")
    ctx = MagicMock()
    ctx.close = AsyncMock(side_effect=RuntimeError("boom"))
    pw = MagicMock()
    pw.stop = AsyncMock(side_effect=RuntimeError("boom"))
    b._context = ctx
    b._pw = pw
    b._started = True
    await b.aclose()
    assert b.started is False


async def test_aclose_without_a_browser_is_a_noop() -> None:
    await QwenBrowser(token="t").aclose()


def test_a_live_browser_reports_itself_alive() -> None:
    b = armed(page_stub())
    assert b.is_alive() is True


def test_a_never_started_browser_is_not_alive() -> None:
    assert QwenBrowser(token="t").is_alive() is False


def test_a_closed_page_is_not_alive() -> None:
    """The case behind the outage: ``_started`` is set, the page is gone."""
    page = page_stub()
    page.is_closed = MagicMock(return_value=True)
    b = armed(page)
    assert b.started is True
    assert b.is_alive() is False


def test_a_closed_context_is_not_alive() -> None:
    """A dead browser can leave the page reporting itself open."""
    page = page_stub()
    ctx = context_stub(page)
    ctx.is_closed = MagicMock(return_value=True)
    b = armed(page)
    b._context = ctx
    assert b.is_alive() is False


def test_an_unreachable_page_is_not_alive() -> None:
    """When the transport is gone the handle cannot even answer."""
    page = page_stub()
    page.is_closed = MagicMock(side_effect=RuntimeError("Target closed"))
    assert armed(page).is_alive() is False


async def test_a_dead_browser_is_reopened_before_the_next_request(monkeypatch, tmp_path) -> None:
    """The regression: a browser that died while the service kept running.

    ``_started`` stays True and the page object stays in place, so before this
    fix every later request failed on the dead handle with "Target page, context
    or browser has been closed" and the browser was never reopened.
    """
    dead = page_stub()
    dead.is_closed = MagicMock(return_value=True)
    b = armed(dead)

    fresh = page_stub()
    ctx = context_stub(fresh, pages=[fresh])
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw_stub(ctx)))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    await b._ensure_started()

    assert b.is_alive() is True
    assert b._page is fresh
    fresh.goto.assert_awaited()


async def test_a_live_browser_is_reused_without_reopening(monkeypatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(browser_mod, "async_playwright", factory)
    b = armed(page_stub())
    await b._ensure_started()
    factory.assert_not_called()


# --- composer waiting ---


async def test_composer_wait_returns_as_soon_as_it_renders() -> None:
    page = page_stub(textarea_count=1)
    await armed(page)._wait_for_composer()
    page.locator.assert_called()


async def test_composer_wait_reports_a_captcha(monkeypatch) -> None:
    """A page that never renders the composer is reported as a captcha."""
    page = page_stub(textarea_count=0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page)
    b._timeout = 0.0
    with pytest.raises(QwenCaptchaRequired):
        await b._wait_for_composer()


async def test_composer_wait_tolerates_a_mid_navigation_error(monkeypatch) -> None:
    page = page_stub()
    page.locator.return_value.count = AsyncMock(side_effect=RuntimeError("nav"))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page)
    b._timeout = 0.0
    with pytest.raises(QwenCaptchaRequired):
        await b._wait_for_composer()


# --- model picker ---


async def test_unknown_model_is_rejected() -> None:
    with pytest.raises(QwenBrowserError, match="unknown Qwen model"):
        await armed(page_stub())._ensure_model("nope")


async def test_model_already_bound_costs_no_clicks() -> None:
    page = page_stub()
    b = armed(page)
    b._model_wire = "qwen3.8-max"
    await b._ensure_model("qwen3.8-max")
    page.locator.return_value.click.assert_not_awaited()


async def test_model_is_switched_when_the_picker_shows_another() -> None:
    page = page_stub()
    page.locator.return_value.inner_text = AsyncMock(return_value="Qwen3.7-Plus")
    b = armed(page)
    await b._ensure_model("qwen3.8-max")
    page.locator.return_value.click.assert_awaited()
    assert b._model_wire == "qwen3.8-max"


async def test_a_missing_picker_is_reported() -> None:
    page = page_stub()
    page.locator.return_value.inner_text = AsyncMock(side_effect=RuntimeError("gone"))
    with pytest.raises(QwenBrowserError, match="model picker not found"):
        await armed(page)._ensure_model("qwen3.8-max")


# --- chat root ---


async def test_new_chat_stays_put_when_already_at_the_root() -> None:
    page = page_stub(textarea_count=1, url="https://chat.qwen.ai/")
    await armed(page)._new_chat()
    page.goto.assert_not_awaited()


async def test_new_chat_navigates_away_from_a_conversation() -> None:
    page = page_stub(textarea_count=1, url="https://chat.qwen.ai/c/abc")
    await armed(page)._new_chat()
    page.goto.assert_awaited()


# --- text path ---


async def test_capture_text_returns_the_body() -> None:
    page = page_stub()
    b = armed(page)
    body = await b.capture_text("hi", "qwen3.8-max")
    assert body == OK_BODY


async def test_capture_text_opens_a_given_chat() -> None:
    page = page_stub()
    b = armed(page)
    await b.capture_text("hi", "qwen3.8-max", chat_id="c-42")
    page.goto.assert_awaited()
    assert "c-42" in page.goto.await_args.args[0]


async def test_capture_rejects_a_non_200(monkeypatch) -> None:
    page = page_stub()
    page.response.status = 500
    b = armed(page)
    with pytest.raises(QwenBrowserError, match="HTTP 500"):
        await b.capture("hi", "qwen3.8-max")


async def test_capture_starts_the_browser_when_not_started(monkeypatch) -> None:
    b = QwenBrowser(token="t", timeout=5)
    started = AsyncMock()
    monkeypatch.setattr(b, "start", started)
    page = page_stub()
    b._page = page
    b._started = False

    async def fake_start():
        b._started = True
        b._page = page

    started.side_effect = fake_start
    await b.capture("hi", "qwen3.8-max")
    started.assert_awaited_once()


async def test_complete_parses_the_captured_body() -> None:
    b = armed(page_stub())
    rec = await b.complete("hi", "qwen3.8-max")
    assert rec.content == "hi"


async def test_complete_stream_yields_the_text() -> None:
    b = armed(page_stub())
    chunks = [chunk async for chunk in b.complete_stream("hi", "qwen3.8-max")]
    assert chunks == ["hi"]


# --- submit ---


async def test_submit_waits_for_a_busy_chat_before_typing(monkeypatch) -> None:
    """A running answer must finish before the next prompt is typed.

    While Qwen streams, the SPA renders ``stop-button`` and no ``send-button`` at
    all. Typing into that chat and clicking a missing button is what produced
    "no completion captured" after a 30 s wait, and the retries turned one slow
    answer into a wall of 502s. The wait is what queues the request behind it.
    """
    page = page_stub(stop_count=2)
    seen: list[int] = []

    async def fake_sleep(_delay):
        seen.append(1)
        if len(seen) >= 3:
            page.stop_locator.count = AsyncMock(return_value=0)

    monkeypatch.setattr(browser_mod.asyncio, "sleep", fake_sleep)
    b = armed(page)
    body, status = await b._submit("hello")
    assert body == OK_BODY
    assert status == 200
    assert len(seen) >= 3, "the driver must poll until the stop button is gone"
    page.locator.return_value.fill.assert_awaited_with("hello")


async def test_submit_gives_up_when_the_chat_never_goes_idle(monkeypatch) -> None:
    """An answer that never finishes is an error, not an endless wait."""

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    page = page_stub(stop_count=1)
    b = QwenBrowser(token="t", timeout=0.05)
    b._started = True
    b._page = page
    with pytest.raises(QwenBrowserError, match="still generating"):
        await b._submit("hello")
    page.locator.return_value.fill.assert_not_awaited()


async def test_submit_types_and_sends() -> None:
    page = page_stub()
    b = armed(page)
    body, status = await b._submit("hello")
    assert body == OK_BODY
    assert status == 200
    page.locator.return_value.fill.assert_awaited_with("hello")
    page.locator.return_value.click.assert_awaited()


# --- captcha waiting ---


async def test_captcha_cleared_is_detected(monkeypatch) -> None:
    page = page_stub(textarea_count=1)
    page.content = AsyncMock(return_value="<html>chat</html>")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page, captcha_wait=60)
    assert await b._await_captcha_cleared() is True


async def test_captcha_still_up_after_the_budget(monkeypatch) -> None:
    page = page_stub(textarea_count=0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page, captcha_wait=0.0)
    assert await b._await_captcha_cleared() is False


async def test_captcha_wait_ignores_a_page_error(monkeypatch) -> None:
    page = page_stub()
    page.content = AsyncMock(side_effect=RuntimeError("nav"))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page, captcha_wait=0.0)
    assert await b._await_captcha_cleared() is False


# --- recovery ---


async def test_recovery_reloads_and_forgets_the_model() -> None:
    page = page_stub()
    b = armed(page)
    b._model_wire = "qwen3.8-max"
    await b._recover_after_captcha()
    page.goto.assert_awaited()
    assert b._model_wire is None


# --- image path ---


async def test_generate_image_sets_mode_and_aspect_and_parses_urls() -> None:
    page = page_stub()
    image_body = (
        'data: {"response.created":{"chat_id":"c-img","response_id":"r"}}\n\n'
        'data: {"choices":[{"delta":{"phase":"image","content":"",'
        '"image_url":"https://cdn.qwenlm.ai/x.png"}}],"response_id":"r"}\n\n'
    )
    page.response.text = AsyncMock(return_value=image_body)
    b = armed(page)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    rec = await b.generate_image("an apple", aspect="2:3")
    b._select_image_mode.assert_awaited()
    b._select_aspect.assert_awaited_with("2:3")
    assert b.chat_id == "c-img"
    assert rec.image_urls


async def test_generate_image_rejects_a_non_200() -> None:
    page = page_stub()
    page.response.status = 503
    b = armed(page)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    with pytest.raises(QwenBrowserError, match="HTTP 503"):
        await b.generate_image("an apple")


async def test_generate_image_wraps_a_submit_failure() -> None:
    page = page_stub()
    page.expect_response.side_effect = RuntimeError("no response")
    b = armed(page)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    # Both transports share one wrapper now, so the message is the generic one.
    with pytest.raises(QwenBrowserError, match="no completion captured"):
        await b.generate_image("an apple")


async def test_generate_image_reports_a_captcha_without_a_budget() -> None:
    page = page_stub()
    page.response.text = AsyncMock(return_value=WAF_BODY)
    b = armed(page, captcha_wait=0)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    with pytest.raises(QwenCaptchaRequired):
        await b.generate_image("an apple")


async def test_generate_image_starts_the_browser_when_not_started(monkeypatch) -> None:
    b = QwenBrowser(token="t", timeout=5)
    page = page_stub()
    b._page = page
    started = AsyncMock()

    async def fake_start():
        b._started = True
        b._page = page

    started.side_effect = fake_start
    monkeypatch.setattr(b, "start", started)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    await b.generate_image("an apple")
    started.assert_awaited_once()


# --- image mode switch ---


async def test_image_mode_switch_times_out_loudly(monkeypatch) -> None:
    page = page_stub()
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.locator.return_value.inner_text = AsyncMock(return_value="Auto")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page)
    with pytest.raises(QwenBrowserError, match="Create Image"):
        await b._select_image_mode()


# --- remaining edges ---


def test_a_non_data_line_in_the_stream_is_skipped() -> None:
    """Event-stream heartbeats and comments carry no payload."""
    from danyapi.qwen.browser import _chat_id_from

    body = ': keep-alive\n\nevent: ping\n\ndata: {"response.created":{"chat_id":"c"}}\n'
    assert _chat_id_from(body) == "c"


async def test_composer_wait_skips_an_erroring_poll_then_succeeds(monkeypatch) -> None:
    """A page mid-navigation may error once; the loop must survive and retry."""
    page = page_stub()
    page.locator.return_value.count = AsyncMock(side_effect=[RuntimeError("nav"), 1])

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    await armed(page)._wait_for_composer()


async def test_capture_text_can_continue_an_existing_chat() -> None:
    page = page_stub()
    b = armed(page)
    await b.capture_text("hi", "qwen3.8-max", chat_id="c-9")
    assert "c-9" in page.goto.await_args.args[0]


async def test_a_captcha_error_from_submit_is_not_wrapped(monkeypatch) -> None:
    """A challenge must reach the caller as a challenge, not as a generic failure."""
    page = page_stub()
    b = armed(page)
    b._submit = AsyncMock(side_effect=QwenCaptchaRequired("challenge"))
    with pytest.raises(QwenCaptchaRequired):
        await b._send_with_captcha_retry("hi", image_mode=False)


async def test_capture_text_starts_the_browser_when_not_started(monkeypatch) -> None:
    b = QwenBrowser(token="t", timeout=5)
    page = page_stub()
    started = AsyncMock()

    async def fake_start():
        b._started = True
        b._page = page

    started.side_effect = fake_start
    monkeypatch.setattr(b, "start", started)
    await b.capture_text("hi", "qwen3.8-max")
    started.assert_awaited_once()


async def test_image_mode_survives_a_failing_probe(monkeypatch) -> None:
    """When the probe itself errors the mode is treated as Auto and switched."""
    page = page_stub()
    # The probe errors, then the post-switch label reads back correctly.
    page.locator.return_value.count = AsyncMock(side_effect=[RuntimeError("gone"), 1])
    page.locator.return_value.inner_text = AsyncMock(return_value="Create Image")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    await armed(page)._select_image_mode()


async def test_image_mode_switch_waits_through_an_erroring_read(monkeypatch) -> None:
    """Reading the label may fail mid-switch; the wait loop must keep polling."""
    page = page_stub()
    counter = MagicMock()
    counter.count = AsyncMock(return_value=1)
    counter.first = counter
    counter.filter.return_value = counter
    counter.inner_text = AsyncMock(side_effect=[RuntimeError("nav"), "Create Image"])
    counter.click = AsyncMock()
    counter.wait_for = AsyncMock()
    page.locator = MagicMock(return_value=counter)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    await armed(page)._select_image_mode()


async def test_captcha_wait_tolerates_an_erroring_poll(monkeypatch) -> None:
    page = page_stub()
    page.content = AsyncMock(side_effect=[RuntimeError("nav"), "<html>chat</html>"])
    page.locator.return_value.count = AsyncMock(return_value=1)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    b = armed(page, captcha_wait=60)
    assert await b._await_captcha_cleared() is True


async def test_capture_can_continue_an_existing_chat() -> None:
    """``capture`` opens the given conversation before sending."""
    page = page_stub()
    b = armed(page)
    await b.capture("hi", "qwen3.8-max", chat_id="c-77")
    assert "c-77" in page.goto.await_args.args[0]


async def test_image_mode_switch_survives_an_erroring_label_read(monkeypatch) -> None:
    """The label read may fail mid-switch; the wait loop must keep polling."""
    page = page_stub()
    # First read (pre-switch probe) says Auto, the post-switch read errors once,
    # then the mode finally reports Create Image.
    counter = MagicMock()
    counter.count = AsyncMock(return_value=1)
    counter.first = counter
    counter.filter.return_value = counter
    counter.inner_text = AsyncMock(side_effect=["Auto", RuntimeError("nav"), "Create Image"])
    counter.click = AsyncMock()
    counter.wait_for = AsyncMock()
    page.locator = MagicMock(return_value=counter)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)
    await armed(page)._select_image_mode()


def test_parse_flushes_a_trailing_event_without_a_blank_line() -> None:
    """The last SSE event often has no closing blank line; ``finish`` must flush it."""
    body = 'data: {"choices":[{"delta":{"content":"tail","phase":"answer","status":"typing"}}],"response_id":"r"}'
    assert QwenBrowser._parse(body).content == "tail"


# --- reusing an existing chat ---


async def test_open_chat_navigates_when_not_there_yet() -> None:
    """An image is added to the pinned conversation, not to a fresh one."""
    page = page_stub(url="https://chat.qwen.ai/")
    await armed(page)._open_chat("c-55")
    page.goto.assert_awaited()
    assert "c-55" in page.goto.await_args.args[0]


async def test_open_chat_is_a_noop_when_already_open() -> None:
    """Reloading the same conversation would cost a page load for nothing."""
    page = page_stub(url="https://chat.qwen.ai/c/c-55")
    await armed(page)._open_chat("c-55")
    page.goto.assert_not_awaited()


async def test_generate_image_reuses_the_given_chat() -> None:
    """The pinned chat comes from the account, so no new chat is created."""
    page = page_stub()
    b = armed(page)
    b._select_aspect = AsyncMock()
    b._select_image_mode = AsyncMock()
    await b.generate_image("an apple", chat_id="c-77")
    assert "c-77" in page.goto.await_args.args[0]
    # The chat root was never opened, which is what creating a new chat does.
    assert all("c-77" in call.args[0] for call in page.goto.await_args_list)


# --- token rotation from the browser profile ---


class _Cookies:
    def __init__(self, value: str | None) -> None:
        self._value = value

    async def __call__(self):
        if self._value is None:
            return []
        return [{"name": "refresh_token", "value": self._value}]


def browser_with_context(token: str, profile_token: str | None, persisted: list) -> QwenBrowser:
    b = QwenBrowser(token=token, timeout=5, token_persist=persisted.append)
    b._started = True
    b._page = page_stub()
    ctx = MagicMock()
    ctx.cookies = AsyncMock(side_effect=lambda: [{"name": "refresh_token", "value": profile_token}] if profile_token else [])
    b._context = ctx
    return b


async def test_the_profile_token_is_adopted_when_it_differs() -> None:
    """The profile holds the rotated credential; the configured one may be stale."""
    persisted: list[str] = []
    b = browser_with_context("stale", "rotated", persisted)
    assert await b._profile_refresh_token() == "rotated"
    await b._sync_token_from_profile()
    assert b.token == "rotated"
    assert persisted == ["rotated"]


async def test_an_unchanged_profile_token_is_not_persisted_again() -> None:
    persisted: list[str] = []
    b = browser_with_context("same", "same", persisted)
    await b._sync_token_from_profile()
    assert persisted == []


async def test_an_empty_profile_leaves_the_token_alone() -> None:
    persisted: list[str] = []
    b = browser_with_context("configured", None, persisted)
    assert await b._profile_refresh_token() is None
    await b._sync_token_from_profile()
    assert b.token == "configured"
    assert persisted == []


async def test_a_closed_context_reports_no_profile_token() -> None:
    b = QwenBrowser(token="t")
    assert await b._profile_refresh_token() is None


async def test_a_failing_cookie_read_reports_no_token() -> None:
    b = QwenBrowser(token="t")
    ctx = MagicMock()
    ctx.cookies = AsyncMock(side_effect=RuntimeError("closed"))
    b._context = ctx
    assert await b._profile_refresh_token() is None


async def test_a_successful_send_syncs_the_rotated_token() -> None:
    persisted: list[str] = []
    b = browser_with_context("stale", "rotated", persisted)
    await b._submit("hello")
    assert b.token == "rotated"
    assert persisted == ["rotated"]


async def test_start_prefers_the_profile_over_the_configured_token(monkeypatch, tmp_path) -> None:
    """Injecting a stale token over a live session is what revoked them before."""
    page = page_stub()
    ctx = context_stub(page, pages=[page])
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": "from-profile"}])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    persisted: list[str] = []
    b = QwenBrowser(token="configured", timeout=5, token_persist=persisted.append)
    await b.start()
    # The profile wins and the rotated value is stored.
    assert b.token == "from-profile"
    assert persisted == ["from-profile"]
    ctx.add_cookies.assert_not_awaited()


async def test_start_injects_the_token_when_the_profile_is_empty(monkeypatch, tmp_path) -> None:
    page = page_stub()
    ctx = context_stub(page, pages=[page])
    ctx.cookies = AsyncMock(return_value=[])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    b = QwenBrowser(token="configured", timeout=5)
    await b.start()
    ctx.add_cookies.assert_awaited_once()


# --- the shared rotation store across the two transports ---


def _jwt(iat: int, **claims) -> str:
    import base64 as _b64
    import json as _json

    payload = {"iat": iat, **claims}
    return "h." + _b64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=") + ".s"


def browser_with_store(configured: str, store) -> QwenBrowser:
    b = QwenBrowser(token=configured, timeout=5, store=store, configured=configured)
    b._started = True
    b._page = page_stub()
    return b


def _fingerprint(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def test_stored_token_is_none_without_a_store() -> None:
    assert QwenBrowser(token="t")._stored_token() is None


def test_stored_token_reads_the_rotation_slot() -> None:
    from danyapi.store import JsonStore

    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), "rotated")
    b = browser_with_store("configured", store)
    assert b._stored_token() == "rotated"


async def test_sync_adopts_the_stored_token_when_it_is_newer() -> None:
    """The HTTP client rotated: the store wins over the profile's stale copy."""
    from danyapi.store import JsonStore

    newer = _jwt(2000)
    older = _jwt(1000)
    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), newer)
    persisted: list[str] = []
    b = browser_with_store("configured", store)
    b._token_persist = persisted.append
    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": older}])
    b._context = ctx

    await b._sync_token_from_profile()

    assert b.token == newer
    # The stale profile value must not be written back over the store.
    assert persisted == []


async def test_sync_keeps_the_profile_when_the_store_is_older() -> None:
    from danyapi.store import JsonStore

    older = _jwt(1000)
    newer = _jwt(2000)
    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), older)
    persisted: list[str] = []
    b = browser_with_store("configured", store)
    b._token_persist = persisted.append
    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": newer}])
    b._context = ctx

    await b._sync_token_from_profile()

    assert b.token == newer
    assert persisted == [newer]


async def test_sync_falls_back_to_the_profile_when_iat_is_unknown() -> None:
    from danyapi.store import JsonStore

    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), "opaque-stored")
    persisted: list[str] = []
    b = browser_with_store("configured", store)
    b._token_persist = persisted.append
    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": "opaque-profile"}])
    b._context = ctx

    await b._sync_token_from_profile()

    assert b.token == "opaque-profile"
    assert persisted == ["opaque-profile"]


def test_stored_is_newer_needs_both_iat_readable() -> None:
    from danyapi.qwen.browser import _stored_is_newer

    assert _stored_is_newer(None, "profile") is False
    assert _stored_is_newer("stored", None) is True
    assert _stored_is_newer("same", "same") is False
    assert _stored_is_newer(_jwt(2000), _jwt(1000)) is True
    assert _stored_is_newer(_jwt(1000), _jwt(2000)) is False
    assert _stored_is_newer(_jwt(1000), _jwt(1000)) is False
    assert _stored_is_newer("opaque", _jwt(1000)) is False
    assert _stored_is_newer(_jwt(1000), "opaque") is False


async def test_start_injects_the_stored_token_when_it_beats_the_profile(monkeypatch, tmp_path) -> None:
    """The HTTP client rotated while the browser was closed.

    The profile still carries the revoked copy; injecting the stored (newer)
    credential is what keeps the account working.
    """
    from danyapi.store import JsonStore

    stored = _jwt(2000)
    profile = _jwt(1000)
    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), stored)

    page = page_stub()
    ctx = context_stub(page, pages=[page])
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": profile}])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    b = QwenBrowser(token="configured", timeout=5, store=store, configured="configured")
    await b.start()

    assert b.token == stored
    ctx.add_cookies.assert_awaited_once()
    assert ctx.add_cookies.await_args.args[0][0]["value"] == stored


async def test_start_keeps_the_profile_when_the_store_is_older(monkeypatch, tmp_path) -> None:
    from danyapi.store import JsonStore

    profile = _jwt(2000)
    store = JsonStore("qwen-credentials", None)
    store.set(_fingerprint("configured"), _jwt(1000))

    page = page_stub()
    ctx = context_stub(page, pages=[page])
    ctx.cookies = AsyncMock(return_value=[{"name": "refresh_token", "value": profile}])
    pw = pw_stub(ctx)
    monkeypatch.setattr(browser_mod, "async_playwright", playwright_factory(pw))
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))

    persisted: list[str] = []
    b = QwenBrowser(token="configured", timeout=5, token_persist=persisted.append, store=store, configured="configured")
    await b.start()

    assert b.token == profile
    ctx.add_cookies.assert_not_awaited()
    assert persisted == [profile]
