"""API-layer branches added for the Qwen browser transport.

The browser transport changes two decisions in ``qwen/api.py``: text completions
also go through the browser when one is configured, and images always do. Both
map transport failures onto HTTP errors, and both have a captcha branch that must
mark the account instead of returning a generic 502. Those mappings are what is
covered here - the transport itself is tested in ``test_qwen_browser*``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import danyapi.qwen.api as qwen_api
from danyapi.qwen.browser import QwenBrowserError, QwenCaptchaRequired


class FakeCaptchaError(Exception):
    """Name contains "Captcha", which is how the API layer classifies it."""


def browser_stub(**methods) -> MagicMock:
    """Browser with coroutine methods, so ``_real_browser`` accepts it."""
    browser = MagicMock()
    browser.capture_text = AsyncMock(**methods) if "capture_text" in methods else AsyncMock(return_value="data: {}")
    browser.generate_image = AsyncMock(**methods) if "generate_image" in methods else AsyncMock()
    browser.chat_id = "c-1"
    return browser


def account_with(browser: MagicMock, pinned: str | None = None) -> MagicMock:
    account = MagicMock()
    account.browser = browser
    account.index = 0
    account.sticky_image_chat = MagicMock(return_value=pinned)
    return account


@pytest.fixture(autouse=True)
def no_pacing_and_delay(monkeypatch):
    """Pacing and the human delay are orthogonal to the branches under test."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(qwen_api, "_pace", noop)
    monkeypatch.setattr(qwen_api, "_human_delay", noop)


# --- captcha detection helper ---


def test_captcha_text_handles_undecodable_bytes(monkeypatch) -> None:
    """An undecodable body is simply not a captcha, never a crash."""

    class BadBytes(bytes):
        def decode(self, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"x", 0, 1, "broken")

    assert qwen_api._is_captcha_text(BadBytes(b"x")) is False


# --- text path through the browser ---


async def test_text_completion_uses_the_browser() -> None:
    browser = browser_stub()
    browser.capture_text = AsyncMock(return_value="data: {}")
    account = account_with(browser)
    resp = await qwen_api._send_completion(account, MagicMock(), "hi", "qwen3.8-max", False, False)
    browser.capture_text.assert_awaited_once_with("hi", "qwen3.8-max")
    assert isinstance(resp, qwen_api._BrowserResponse)


async def test_text_completion_maps_a_captcha() -> None:
    browser = browser_stub()
    browser.capture_text = AsyncMock(side_effect=QwenCaptchaRequired("challenge"))
    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api._send_completion(account_with(browser), MagicMock(), "hi", "m", False, False)


async def test_text_completion_maps_a_generic_failure() -> None:
    browser = browser_stub()
    browser.capture_text = AsyncMock(side_effect=QwenBrowserError("broken"))
    with pytest.raises(HTTPException) as excinfo:
        await qwen_api._send_completion(account_with(browser), MagicMock(), "hi", "m", False, False)
    assert excinfo.value.status_code == 502


async def test_a_captcha_body_is_detected_after_a_successful_call() -> None:
    """The browser can hand back a challenge page with HTTP 200."""
    browser = browser_stub()
    browser.capture_text = AsyncMock(return_value='{"ret":["FAIL_SYS_USER_VALIDATE"]}')
    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api._send_completion(account_with(browser), MagicMock(), "hi", "m", False, False)


async def test_image_requests_still_take_the_http_path_in_send_completion() -> None:
    """``_send_completion`` with an image model is the HTTP image call, not text."""
    account = MagicMock()
    account.browser = browser_stub()
    account.client.completion = AsyncMock(side_effect=RuntimeError("http path"))
    with pytest.raises(RuntimeError, match="http path"):
        await qwen_api._send_completion(account, MagicMock(), "hi", "m", False, False, image_model="img")


# --- image path through the browser ---


def image_result(*urls: str) -> MagicMock:
    rec = MagicMock()
    rec.image_urls = list(urls)
    rec.content = "prompt"
    rec.usage = {"total_tokens": 1}
    return rec


async def test_image_generation_uses_the_browser_and_the_default_aspect() -> None:
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    result = await qwen_api.collect_image(
        account=account_with(browser),
        pool=MagicMock(),
        existing_sid=None,
        lock=MagicMock(),
        prompt="an apple",
        model="qwen-image-3.0-pro",
        model_id="qwen-image-3.0-pro",
    )
    # 2:3 is the Pinterest-native shape, used when the caller names no size.
    browser.generate_image.assert_awaited_once_with("an apple", aspect="2:3", chat_id=None)
    assert result["image_urls"] == ["https://cdn/x.png"]
    assert result["session_id"] == "c-1"


async def test_image_generation_forwards_the_requested_aspect() -> None:
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    await qwen_api.collect_image(
        account=account_with(browser),
        pool=MagicMock(),
        existing_sid=None,
        lock=MagicMock(),
        prompt="an apple",
        model="m",
        model_id="m",
        image_size="9:16",
    )
    browser.generate_image.assert_awaited_once_with("an apple", aspect="9:16", chat_id=None)


async def test_image_generation_marks_the_account_on_a_captcha(monkeypatch) -> None:
    """A challenge must take the account out of rotation, not just fail once."""
    marked: list = []
    monkeypatch.setattr(qwen_api, "_mark_captcha", lambda *a: marked.append(a))
    browser = browser_stub()
    browser.generate_image = AsyncMock(side_effect=QwenCaptchaRequired("challenge"))
    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api.collect_image(
            account=account_with(browser),
            pool=MagicMock(),
            existing_sid="s-1",
            lock=MagicMock(),
            prompt="an apple",
            model="m",
            model_id="m",
        )
    assert marked, "the account should be marked captcha-broken"


async def test_image_generation_maps_a_generic_failure() -> None:
    browser = browser_stub()
    browser.generate_image = AsyncMock(side_effect=QwenBrowserError("broken"))
    with pytest.raises(HTTPException) as excinfo:
        await qwen_api.collect_image(
            account=account_with(browser),
            pool=MagicMock(),
            existing_sid=None,
            lock=asyncio.Semaphore(1),
            prompt="an apple",
            model="m",
            model_id="m",
        )
    assert excinfo.value.status_code == 502
    assert "Qwen image generation failed" in str(excinfo.value.detail)


async def test_image_generation_without_an_image_is_an_error() -> None:
    """A body with no CDN urls means the generator produced nothing usable."""
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result())
    with pytest.raises(HTTPException) as excinfo:
        await qwen_api.collect_image(
            account=account_with(browser),
            pool=MagicMock(),
            existing_sid=None,
            lock=asyncio.Semaphore(1),
            prompt="an apple",
            model="m",
            model_id="m",
        )
    assert excinfo.value.status_code == 502
    assert "no image" in str(excinfo.value.detail)


# --- captcha branches inside the retry loops ---


async def test_a_captcha_marks_the_account_and_is_not_retried(monkeypatch) -> None:
    """The text loop must not retry a challenge; it rests the account instead."""
    marked: list = []
    monkeypatch.setattr(qwen_api, "_mark_captcha", lambda *a: marked.append(a))

    account = MagicMock()
    account.index = 0
    account.sem = MagicMock()
    pool = MagicMock()
    session = MagicMock()
    monkeypatch.setattr(qwen_api, "_prepare_session", AsyncMock(return_value=(session, "s-1")))
    monkeypatch.setattr(qwen_api, "_send_completion", AsyncMock(side_effect=qwen_api.CaptchaChallenge()))

    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api.collect_non_stream(
            account=account,
            pool=pool,
            existing_sid=None,
            lock=asyncio.Semaphore(1),
            prompt="hi",
            model="qwen3.8-max",
            model_id="qwen3.8-max",
            thinking=False,
            search=False,
        )
    assert marked, "the text loop must mark the account captcha-broken"


async def test_a_captcha_in_the_image_loop_marks_the_account(monkeypatch) -> None:
    """The image loop has the same rule: rest the account, do not retry."""
    marked: list = []
    monkeypatch.setattr(qwen_api, "_mark_captcha", lambda *a: marked.append(a))

    account = MagicMock()
    account.index = 0
    account.sem = MagicMock()
    account.sticky_image_chat = MagicMock(return_value=None)
    pool = MagicMock()
    session = MagicMock()
    monkeypatch.setattr(qwen_api, "_prepare_session", AsyncMock(return_value=(session, "s-1")))
    monkeypatch.setattr(qwen_api, "_send_completion", AsyncMock(side_effect=qwen_api.CaptchaChallenge()))

    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api.collect_image(
            account=account,
            pool=pool,
            existing_sid=None,
            lock=asyncio.Semaphore(1),
            prompt="an apple",
            model="m",
            model_id="m",
        )
    assert marked, "the image loop must mark the account captcha-broken"


# --- captcha branches on the HTTP fallback path ---


def http_account(client: MagicMock) -> MagicMock:
    """Account without a browser: the request goes through the HTTP client."""
    account = MagicMock()
    account.browser = None
    account.index = 0
    account.client = client
    return account


async def test_an_http_status_error_carrying_a_captcha_is_recognised() -> None:
    """``raise_for_status`` can surface the challenge as an exception body."""
    import httpx

    response = MagicMock()
    response.status_code = 403
    response.text = '{"ret":["RGV587_ERROR::SM"]}'
    client = MagicMock()
    client.completion = AsyncMock(side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=response))

    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api._send_completion(http_account(client), MagicMock(), "hi", "m", False, False)


async def test_a_non_200_body_carrying_a_captcha_is_recognised() -> None:
    response = MagicMock()
    response.status_code = 502
    response.aread = AsyncMock(return_value=b'{"ret":["FAIL_SYS_USER_VALIDATE"]}')
    response.aclose = AsyncMock()
    client = MagicMock()
    client.completion = AsyncMock(return_value=response)

    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api._send_completion(http_account(client), MagicMock(), "hi", "m", False, False)


async def test_a_200_non_sse_body_carrying_a_captcha_is_recognised() -> None:
    """Qwen can answer 200 with a challenge page instead of a stream."""
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.aread = AsyncMock(return_value=b'{"ret":["_____tmd_____","punish"]}')
    response.aclose = AsyncMock()
    client = MagicMock()
    client.completion = AsyncMock(return_value=response)

    with pytest.raises(qwen_api.CaptchaChallenge):
        await qwen_api._send_completion(http_account(client), MagicMock(), "hi", "m", False, False)


def test_quota_text_returns_none_without_a_marker() -> None:
    assert qwen_api._quota_text("an ordinary answer") is None
    assert qwen_api._quota_text("") is None
    assert qwen_api._quota_text(None) is None


# --- image chats are reused, not reopened ---


async def test_a_pinned_image_chat_is_reused() -> None:
    """Qwen blocks accounts that keep creating chats, so the pin must be used."""
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    browser.chat_id = "c-pinned"
    account = account_with(browser, pinned="c-pinned")

    await qwen_api.collect_image(
        account=account,
        pool=MagicMock(),
        existing_sid=None,
        lock=MagicMock(),
        prompt="an apple",
        model="m",
        model_id="m",
    )
    browser.generate_image.assert_awaited_once_with("an apple", aspect="2:3", chat_id="c-pinned")


async def test_a_new_image_chat_is_remembered_for_the_next_call() -> None:
    """The first image opens a chat; its id is pinned for every later image."""
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    browser.chat_id = "c-new"
    account = account_with(browser, pinned=None)

    await qwen_api.collect_image(
        account=account,
        pool=MagicMock(),
        existing_sid=None,
        lock=MagicMock(),
        prompt="an apple",
        model="m",
        model_id="m",
    )
    account.set_sticky_image_chat.assert_called_once_with("m", "c-new")


async def test_an_explicit_session_wins_over_the_pin() -> None:
    """A caller that names a chat gets that chat, not the account default."""
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    browser.chat_id = "c-explicit"
    account = account_with(browser, pinned="c-pinned")

    await qwen_api.collect_image(
        account=account,
        pool=MagicMock(),
        existing_sid="c-explicit",
        lock=MagicMock(),
        prompt="an apple",
        model="m",
        model_id="m",
    )
    browser.generate_image.assert_awaited_once_with("an apple", aspect="2:3", chat_id="c-explicit")


async def test_an_unchanged_chat_is_not_pinned_twice() -> None:
    browser = browser_stub()
    browser.generate_image = AsyncMock(return_value=image_result("https://cdn/x.png"))
    browser.chat_id = "c-same"
    account = account_with(browser, pinned="c-same")

    await qwen_api.collect_image(
        account=account,
        pool=MagicMock(),
        existing_sid=None,
        lock=MagicMock(),
        prompt="an apple",
        model="m",
        model_id="m",
    )
    account.set_sticky_image_chat.assert_not_called()
