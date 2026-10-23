"""Browser transport for Qwen: helpers, adapter and captcha policy.

The transport itself drives a real browser, which has no place in the unit suite.
What is covered here is everything around it: the pure helpers (WAF detection,
chat-id extraction, profile layout), the httpx-compatible wrapper the API layer
feeds into the existing pipeline, and the captcha policy - the part most likely
to regress silently, because a wrong decision there either retries into a blocked
account or gives up while a human is still solving the challenge.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.qwen.api as qwen_api
import danyapi.qwen.browser as browser_mod
from danyapi.qwen.browser import (
    QwenBrowser,
    QwenBrowserError,
    QwenCaptchaRequired,
    _chat_id_from,
    _looks_like_waf,
    _profile_dir,
)


def fake_locator(*, text: str = "", texts: list[str] | None = None, count: int = 1) -> MagicMock:
    """Locator stand-in: chaining returns itself and actions are awaitable."""
    loc = MagicMock()
    loc.first = loc
    loc.filter.return_value = loc
    loc.nth.return_value = loc
    loc.count = AsyncMock(return_value=count)
    loc.click = AsyncMock()
    loc.wait_for = AsyncMock()
    if texts is not None:
        loc.inner_text = AsyncMock(side_effect=texts)
    else:
        loc.inner_text = AsyncMock(return_value=text)
    return loc


# --- pure helpers ---


def test_waf_markers_are_detected() -> None:
    assert _looks_like_waf('{"ret":["FAIL_SYS_USER_VALIDATE"]}')
    assert _looks_like_waf("aliyun_waf_aa")
    assert _looks_like_waf("RGV587_ERROR::SM")
    assert _looks_like_waf(".../_____tmd_____/punish?x5secdata=1")


def test_an_ordinary_stream_is_not_a_waf_page() -> None:
    assert not _looks_like_waf('data: {"choices":[{"delta":{"content":"hi"}}]}')
    assert not _looks_like_waf("")


def test_waf_scan_is_bounded_to_the_head() -> None:
    """A marker far into a long body does not turn a real answer into a challenge."""
    assert not _looks_like_waf("x" * 4000 + "aliyun_waf")


def test_profile_dir_is_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_BROWSER_DIR", raising=False)
    assert _profile_dir(0) != _profile_dir(1)
    assert _profile_dir(0).name == "qwen-browser-0"
    assert _profile_dir(1).name == "qwen-browser-1"


def test_profile_dir_honours_the_env_base(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("QWEN_BROWSER_DIR", str(tmp_path))
    assert _profile_dir(2) == tmp_path / "qwen-browser-2"


def test_browser_executable_comes_from_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_BROWSER_EXECUTABLE", raising=False)
    assert browser_mod._browser_executable() is None
    monkeypatch.setenv("QWEN_BROWSER_EXECUTABLE", " /usr/bin/chromium ")
    assert browser_mod._browser_executable() == "/usr/bin/chromium"


def test_chat_id_comes_from_the_first_event() -> None:
    body = 'data: {"response.created":{"chat_id":"c-1","response_id":"r"}}\n\ndata: {"choices":[]}\n\n'
    assert _chat_id_from(body) == "c-1"


def test_chat_id_is_absent_in_broken_bodies() -> None:
    assert _chat_id_from("") is None
    assert _chat_id_from("data: [DONE]") is None
    assert _chat_id_from("data: not json") is None
    assert _chat_id_from('data: {"response.created":{}}') is None
    assert _chat_id_from('data: {"choices":[]}') is None


# --- response adapter and parsing ---


async def test_browser_response_behaves_like_a_stream() -> None:
    resp = qwen_api._BrowserResponse("data: hi\n\n")
    assert [chunk async for chunk in resp.aiter_bytes()] == [b"data: hi\n\n"]
    assert await resp.aread() == b"data: hi\n\n"
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    await resp.aclose()


def test_parse_collects_the_answer_text() -> None:
    # ``phase`` is what the reconstructor keys on: an answer delta without it is
    # indistinguishable from a thinking or image delta and is dropped.
    body = (
        'data: {"response.created":{"chat_id":"c","response_id":"r"}}\n\n'
        'data: {"choices":[{"delta":{"role":"assistant","content":"Hello",'
        '"phase":"answer","status":"typing"}}],"response_id":"r"}\n\n'
        'data: {"choices":[{"delta":{"content":" world","phase":"answer","status":"typing"}}],'
        '"response_id":"r"}\n\n'
        'data: {"choices":[{"delta":{"status":"finished","phase":"answer"}}],"response_id":"r"}\n\n'
    )
    rec = QwenBrowser._parse(body)
    assert rec.content == "Hello world"
    assert rec.error is None


# --- transport selection in the API layer ---


def test_mock_account_is_not_treated_as_a_browser() -> None:
    """A MagicMock answers any attribute, so the check must look at the method."""
    assert qwen_api._real_browser(MagicMock()) is None


def test_real_browser_is_returned() -> None:
    browser = QwenBrowser(token="t")
    account = MagicMock()
    account.browser = browser
    assert qwen_api._real_browser(account) is browser


def test_account_without_a_browser_falls_back_to_http() -> None:
    account = MagicMock()
    account.browser = None
    assert qwen_api._real_browser(account) is None


# --- политика капчи ---


def fake_page(locator: MagicMock) -> MagicMock:
    """Page stand-in whose waits and locators are awaitable."""
    page = MagicMock()
    page.locator.return_value = locator
    page.wait_for_timeout = AsyncMock()
    return page


def browser_with_page(page: MagicMock, *, captcha_wait: float = 0.0) -> QwenBrowser:
    b = QwenBrowser(token="t", timeout=30, captcha_wait=captcha_wait)
    b._started = True
    b._page = page
    return b


async def test_captcha_without_a_wait_budget_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """No budget means no waiting: the challenge is reported immediately.

    This is the default, so a headless deployment never blocks a request for
    minutes on a challenge nobody is there to solve.
    """
    b = QwenBrowser(token="t", timeout=30, captcha_wait=0)
    b._started = True
    b._page = MagicMock()
    b._submit = AsyncMock(return_value=('{"ret":["RGV587_ERROR::SM"]}', 200))
    cleared = AsyncMock(return_value=True)
    b._await_captcha_cleared = cleared
    with pytest.raises(QwenCaptchaRequired):
        await b._send_with_captcha_retry("hi", image_mode=False)
    cleared.assert_not_awaited()


async def test_captcha_with_a_budget_waits_then_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a budget the human is given time, then the prompt goes again."""
    b = QwenBrowser(token="t", timeout=30, captcha_wait=60)
    b._started = True
    b._page = MagicMock()
    b._submit = AsyncMock(side_effect=[('{"ret":["RGV587_ERROR::SM"]}', 200), ('data: {"choices":[]}', 200)])
    b._await_captcha_cleared = AsyncMock(return_value=True)
    b._recover_after_captcha = AsyncMock()
    body, status = await b._send_with_captcha_retry("hi", image_mode=False)
    assert status == 200
    assert "RGV587" not in body
    b._recover_after_captcha.assert_awaited_once()


async def test_image_mode_and_aspect_are_restored_after_a_captcha() -> None:
    """Reloading resets the composer, so both image mode and aspect are re-applied.

    Restoring only the mode would leave the ratio at the page default (16:9), and
    the retried request would silently produce a horizontal pin.
    """
    b = QwenBrowser(token="t", timeout=30, captcha_wait=60)
    b._started = True
    b._page = MagicMock()
    b._submit = AsyncMock(side_effect=[('{"ret":["RGV587_ERROR::SM"]}', 200), ('data: {"choices":[]}', 200)])
    b._await_captcha_cleared = AsyncMock(return_value=True)
    b._recover_after_captcha = AsyncMock()
    b._select_image_mode = AsyncMock()
    b._select_aspect = AsyncMock()
    await b._send_with_captcha_retry("hi", image_mode=True, aspect="2:3")
    b._select_image_mode.assert_awaited_once()
    b._select_aspect.assert_awaited_once_with("2:3")


async def test_a_second_wall_after_the_wait_is_reported() -> None:
    """If the challenge is still up after the wait, the request fails clearly."""
    b = QwenBrowser(token="t", timeout=30, captcha_wait=60)
    b._started = True
    b._page = MagicMock()
    b._submit = AsyncMock(return_value=('{"ret":["RGV587_ERROR::SM"]}', 200))
    b._await_captcha_cleared = AsyncMock(return_value=True)
    b._recover_after_captcha = AsyncMock()
    with pytest.raises(QwenCaptchaRequired):
        await b._send_with_captcha_retry("hi", image_mode=False)


async def test_a_clean_answer_is_not_retried() -> None:
    b = QwenBrowser(token="t", timeout=30, captcha_wait=60)
    b._started = True
    b._page = MagicMock()
    b._submit = AsyncMock(return_value=('data: {"choices":[]}', 200))
    b._await_captcha_cleared = AsyncMock()
    _body, status = await b._send_with_captcha_retry("hi", image_mode=False)
    assert status == 200
    b._await_captcha_cleared.assert_not_awaited()


# --- соотношение сторон ---


async def test_aspect_is_left_alone_when_it_already_matches() -> None:
    current = fake_locator(text="2:3")
    page = fake_page(current)
    b = browser_with_page(page)
    await b._select_aspect("2:3")
    current.click.assert_not_awaited()


async def test_aspect_is_switched_through_the_menu() -> None:
    current = fake_locator(texts=["16:9", "2:3"])
    page = fake_page(current)
    b = browser_with_page(page)
    await b._select_aspect("2:3")
    current.click.assert_awaited()
    page.wait_for_timeout.assert_awaited()


async def test_a_failed_aspect_switch_is_loud() -> None:
    """Staying on the wrong ratio would silently produce horizontal pins."""
    current = fake_locator(texts=["16:9", "16:9"])
    page = fake_page(current)
    b = browser_with_page(page)
    with pytest.raises(QwenBrowserError, match="aspect ratio"):
        await b._select_aspect("2:3")


async def test_a_missing_aspect_selector_is_reported() -> None:
    current = fake_locator()
    current.inner_text = AsyncMock(side_effect=RuntimeError("gone"))
    page = fake_page(current)
    b = browser_with_page(page)
    with pytest.raises(QwenBrowserError, match="aspect ratio selector"):
        await b._select_aspect("2:3")


def test_default_aspect_is_pinterest_vertical() -> None:

    sig = inspect.signature(QwenBrowser.generate_image)
    assert sig.parameters["aspect"].default == "2:3"


# --- режим изображения ---


async def test_image_mode_is_skipped_when_already_active() -> None:
    current = fake_locator(text="Create Image")
    page = fake_page(current)
    b = browser_with_page(page)
    await b._select_image_mode()
    current.click.assert_not_awaited()


async def test_image_mode_is_switched_from_auto() -> None:
    page = MagicMock()
    # In Auto mode the .mode-select-current-mode node does not exist at all, so
    # the first count returns 0 and the one after the switch returns 1.
    counter = fake_locator(text="Create Image")
    # Absent in Auto mode (count 0), present after the switch.
    counter.count = AsyncMock(side_effect=[0, 1])
    page = fake_page(counter)
    b = browser_with_page(page)
    await b._select_image_mode()
    counter.click.assert_awaited()
