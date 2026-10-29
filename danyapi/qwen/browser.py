"""Browser-backed Qwen client.

chat.qwen.ai is protected by Alibaba's baxia risk control: a plain HTTP client
(httpx, curl_cffi with a Chrome TLS fingerprint, or a hand-made ``fetch`` from
inside the page) is answered with a ``punish`` page or a JS challenge. The only
requests that get through are the ones the SPA itself issues, because only then
is the baxia token present and the connection carries the state the WAF bound to
it.

So this module does what the site's own frontend does: it opens the real page in
a real browser, types the prompt into the composer and clicks Send, then reads
the ``/chat/completions`` SSE response the SPA receives. The response is parsed
with the same :class:`QwenStreamReconstructor` the HTTP transport uses, so the
rest of the pipeline cannot tell the two apart.

Authentication: the credential is a ``refresh_token``; it is injected as a
cookie before the page loads, which is how the site stores it. A profile that
already holds the session keeps working without injection.

A captcha is the one thing that cannot be automated: baxia asks a human to drag
a slider. When that happens the request fails fast with a detail the bot can
report verbatim - retrying only feeds the risk score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import _token_fingerprint, jwt_claims
from .stream import QwenStreamReconstructor

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page

    from ..store import JsonStore

try:
    from patchright.async_api import async_playwright
except ImportError:  # pragma: no cover - depends on the environment
    async_playwright = None  # type: ignore[assignment]

log = logging.getLogger("danyapi.qwen.browser")

BASE_URL = "https://chat.qwen.ai"

#: Composer and the button that submits it.
TEXTAREA = "textarea"
SEND_BUTTON = "button.send-button"
#: Model picker: the trigger shows the current model, the items are the choices.
MODEL_TRIGGER = ".wms-trigger"

#: Markers present in the WAF challenge page and in the ``punish`` JSON.
WAF_MARKERS = ("aliyun_waf", "_____tmd_____", "rgv587", "fail_sys_user_validate", "x5secdata")

#: Wire id -> the name the picker shows. The frontend binds the model by that
#: label, so the mapping has to match the UI, not the API.
MODEL_LABELS = {
    "qwen3.8-max": "Qwen3.8-Max",
    "qwen3.8-omni-flash": "Qwen3.8-Omni-Flash",
    "qwen3.7-max": "Qwen3.7-Max",
    "qwen3.7-plus": "Qwen3.7-Plus",
    "qwen3.6-plus": "Qwen3.6-Plus",
    "qwen3.5-plus": "Qwen3.5-Plus",
    "qwen3.5-omni-plus": "Qwen3.5-Omni-Plus",
}

#: Text the caller can match on to tell "a human must solve the captcha" apart
#: from an ordinary provider failure. Shared with the publishing bot.
CAPTCHA_DETAIL = "Qwen anti-bot captcha: the account is flagged and needs the captcha solved in a browser"


class QwenBrowserError(Exception):
    """The browser could not produce an answer."""


class QwenCaptchaRequired(QwenBrowserError):
    """baxia wants a human: retrying is pointless until the captcha is solved."""


def _looks_like_waf(text: str) -> bool:
    lowered = text[:4000].lower()
    return any(marker in lowered for marker in WAF_MARKERS)


def _token_issued_at(token: str | None) -> float | None:
    """The ``iat`` claim of a credential, or ``None`` when it cannot be read."""
    iat = jwt_claims(token).get("iat")
    if isinstance(iat, (int, float)):
        return float(iat)
    return None


def _stored_is_newer(stored: str | None, profile: str | None) -> bool:
    """True when the stored credential was issued later than the profile's.

    Qwen rotates the refresh token server-side, so of two copies the later one
    is the value the server still honours. An empty profile takes whatever the
    store has; an unreadable ``iat`` on either side leaves the profile in charge,
    which is the historical behaviour.
    """
    if not stored:
        return False
    if not profile:
        return True
    if stored == profile:
        return False
    stored_at = _token_issued_at(stored)
    profile_at = _token_issued_at(profile)
    if stored_at is None or profile_at is None:
        return False
    return stored_at > profile_at


def _browser_executable() -> str | None:
    """Path to the browser binary, or ``None`` to let patchright pick its own.

    patchright ships its own Chromium revision; a machine that already has one
    from playwright can point at it to avoid a second download.
    """
    return os.environ.get("QWEN_BROWSER_EXECUTABLE", "").strip() or None


def _profile_dir(index: int = 0) -> Path:
    """Per-account browser profile directory.

    Each account keeps its own profile: the session cookie lives in the profile,
    so two accounts sharing one directory would overwrite each other's
    ``refresh_token`` and flip the page between them. The base path can be
    overridden with ``QWEN_BROWSER_DIR``; the account index is appended to it.
    """
    base = os.environ.get("QWEN_BROWSER_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".danyapi"
    return root / f"qwen-browser-{index}"


def _chat_id_from(body: str) -> str | None:
    """Pull the chat id out of the first SSE event, so the caller can continue it."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        created = event.get("response.created") if isinstance(event, dict) else None
        if isinstance(created, dict) and created.get("chat_id"):
            return str(created["chat_id"])
    return None


class QwenBrowser:
    """Drives chat.qwen.ai in a real browser and returns parsed completions."""

    def __init__(
        self,
        token: str = "",
        timeout: float = 180.0,
        captcha_wait: float = 0.0,
        profile_index: int = 0,
        token_persist: Callable[[str], None] | None = None,
        store: JsonStore | None = None,
        configured: str | None = None,
    ) -> None:
        self._token = token
        # Called when the profile holds a refresh token different from the one
        # we started with. Qwen rotates the credential server-side and the SPA
        # writes the new one into its own cookie jar, so the browser profile
        # becomes the source of truth - exactly like the HTTP client's rotation
        # store. Without this the configured token goes stale and is eventually
        # revoked, and the next start finds every account rejected.
        self._token_persist = token_persist
        # The shared rotation store, read here as well as written through the
        # callback. The HTTP client of the same account rotates independently,
        # and whichever transport rotates later holds the value the server still
        # honours; the store is what tells the other one so.
        self._store = store
        self._configured = configured if configured is not None else token
        #: The credential currently believed valid, updated as the profile rotates.
        self.token = token
        #: Which profile directory to use; one per account, so sessions never mix.
        self._profile_index = max(0, int(profile_index))
        self._timeout = timeout
        #: Seconds to wait for a human to clear an anti-bot captcha in the open
        #: window before giving up. 0 means "fail fast and report the captcha".
        self._captcha_wait = max(0.0, captcha_wait)
        self._pw: Any | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._started = False
        #: Model currently bound in the picker, so a switch costs one click.
        self._model_wire: str | None = None
        #: Chat the last answer came from, so a caller can continue it.
        self.chat_id: str | None = None

    @property
    def started(self) -> bool:
        return self._started

    def is_alive(self) -> bool:
        """True while the page behind ``started`` still exists.

        ``_started`` only records that a browser was opened once. The context can
        die under it - the process is killed, the X display goes away, Chromium
        crashes - while the flag and the page object stay in place. Every later
        call then fails on the dead handle with "Target page, context or browser
        has been closed" and nothing ever reopens it. Callers use this to tell
        the two states apart before reusing the browser.
        """
        if not self._started or self._page is None:
            return False
        try:
            if self._page.is_closed():
                return False
            # The page can report itself open while the whole browser is gone,
            # so the context is asked too. It is optional: a caller may hold a
            # page without keeping the context around.
            context = self._context
            return context is None or not context.is_closed()
        except Exception:
            # The transport is gone: the handle cannot even answer.
            return False

    async def _ensure_started(self) -> None:
        """(Re)open the browser when the current one is missing or dead."""
        if self.is_alive():
            return
        if self._started:
            log.warning("qwen browser is gone; reopening it (profile=%s)", _profile_dir(self._profile_index))
            await self.aclose()
        await self.start()

    async def start(self) -> None:
        """Open the browser, authenticate and wait for the composer."""
        if self._started:
            return
        if async_playwright is None:
            raise QwenBrowserError("patchright is not installed; run `pip install patchright` to use the browser transport")
        profile = _profile_dir(self._profile_index)
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        kwargs: dict[str, Any] = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
            "viewport": {"width": 1366, "height": 900},
            "locale": "en-US",
        }
        exe = _browser_executable()
        if exe:
            kwargs["executable_path"] = exe
        self._context = await self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
        # The profile is the source of truth. Only inject a credential when the
        # profile has none: overwriting a live session with a stale value is
        # what killed the previous tokens, because Qwen rotates the refresh
        # token and the old one is revoked the moment a new one is issued.
        existing = await self._profile_refresh_token()
        stored = self._stored_token()
        if _stored_is_newer(stored, existing):
            # The HTTP client of this account rotated while the browser was
            # closed, so the store holds the value the server still honours and
            # the profile's copy is the revoked one. Inject the stored value.
            self._token = stored or ""
            self.token = self._token
            existing = None
            log.info("qwen browser took the rotated credential from the store (%s)", _token_fingerprint(stored))
        if existing:
            self.token = existing
            if existing != self._token and self._token_persist is not None:
                self._token_persist(existing)
                log.info("qwen browser adopted the rotated token from the profile")
        elif self._token:
            # The site keeps the session in this cookie; injecting it before the
            # first navigation is what makes the page come up logged in.
            await self._context.add_cookies(
                [
                    {
                        "name": "refresh_token",
                        "value": self._token,
                        # The parent domain, not chat.qwen.ai: the SPA exchanges
                        # the credential at auth.qwen.ai, and a cookie scoped to
                        # the chat host is never sent there - the refresh then
                        # answers 401 and the page stays logged out.
                        "domain": ".qwen.ai",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        await self._page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=90_000)
        await self._wait_for_composer()
        self._started = True
        log.info("qwen browser started (profile=%s)", profile)

    async def _profile_refresh_token(self) -> str | None:
        """Return the refresh token currently held by the profile, if any."""
        if self._context is None:
            return None
        try:
            cookies = await self._context.cookies()
        except Exception:
            return None
        for cookie in cookies:
            if cookie.get("name") == "refresh_token" and cookie.get("value"):
                return str(cookie["value"])
        return None

    def _stored_token(self) -> str | None:
        """The rotated credential currently in the shared store, if any."""
        if self._store is None:
            return None
        from .accounts import load_effective_token

        effective = load_effective_token(self._configured or "", self._store)
        return effective or None

    async def _sync_token_from_profile(self) -> None:
        """Persist a refresh token the SPA rotated while we were using it.

        Called after every request: the page refreshes the access token on its
        own and may receive a new refresh token, which then lives only in the
        profile. Mirroring it into the store keeps the configured credential
        usable after a restart, the same way the HTTP client does it.

        The store is read first as well: the HTTP client of this account rotates
        independently, and if it holds the later value that one wins over the
        profile's copy - persisting the stale profile token would roll the store
        back to the credential the server already revoked.
        """
        stored = self._stored_token()
        profile = await self._profile_refresh_token()
        if _stored_is_newer(stored, profile):
            if stored != self.token:
                self.token = stored or ""
                log.info("qwen browser adopted the rotated credential from the store (%s)", _token_fingerprint(stored))
            return
        if not profile or profile == self.token:
            return
        self.token = profile
        if self._token_persist is not None:
            self._token_persist(profile)
            log.info("qwen browser persisted a rotated token from the profile")

    async def _wait_for_composer(self) -> None:
        """Wait until the chat UI is usable, or raise when a captcha blocks it.

        The page is considered ready when the composer exists. A page stuck on
        the WAF challenge never renders it, so the timeout doubles as captcha
        detection: a human has to solve it in the open window.
        """
        page = self._page
        assert page is not None
        deadline = asyncio.get_running_loop().time() + self._timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await page.locator(TEXTAREA).count():
                    return
            except Exception:
                pass
            await asyncio.sleep(2.0)
        raise QwenCaptchaRequired(f"{CAPTCHA_DETAIL}. The browser window is open: solve the challenge there, then retry.")

    async def aclose(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._started = False

    async def _ensure_model(self, model: str) -> None:
        """Bind ``model`` in the picker, clicking only when it differs."""
        label = MODEL_LABELS.get(model)
        if label is None:
            raise QwenBrowserError(f"unknown Qwen model: {model}")
        if self._model_wire == model:
            return
        page = self._page
        assert page is not None
        try:
            current = (await page.locator(MODEL_TRIGGER).first.inner_text()).strip()
        except Exception:
            raise QwenBrowserError("model picker not found on the Qwen page") from None
        if label not in current:
            await page.locator(MODEL_TRIGGER).first.click()
            await page.wait_for_timeout(1200)
            await page.get_by_text(label, exact=True).first.click()
            await page.wait_for_timeout(1200)
        self._model_wire = model

    async def _new_chat(self) -> None:
        """Return to the chat root so the next prompt starts a fresh conversation.

        Qwen binds a chat to the page once a message is sent; sending again in
        the same chat would carry the whole history, which for a stateless API
        call only grows the context (and the risk score).
        """
        page = self._page
        assert page is not None
        if "/c/" not in page.url and await page.locator(TEXTAREA).count():
            return
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=90_000)
        await self._wait_for_composer()

    async def _open_chat(self, chat_id: str) -> None:
        """Open an existing conversation instead of starting a new one.

        Reusing the chat is what keeps the account under Qwen's limit on new
        chats; the page still needs to load and render the composer.
        """
        page = self._page
        assert page is not None
        if f"/c/{chat_id}" in page.url:
            return
        await page.goto(f"{BASE_URL}/c/{chat_id}", wait_until="domcontentloaded", timeout=90_000)
        await self._wait_for_composer()

    async def capture(self, prompt: str, model: str, chat_id: str | None = None) -> tuple[str, str | None]:
        """Send ``prompt`` through the UI and return the raw SSE body.

        Returning the body rather than a parsed object keeps the API layer in
        charge of parsing: it feeds the same body through the same
        :class:`QwenStreamReconstructor` as the HTTP transport, so both transports
        behave identically downstream.
        """
        await self._ensure_started()
        page = self._page
        assert page is not None
        async with self._lock:
            if chat_id:
                await page.goto(f"{BASE_URL}/c/{chat_id}", wait_until="domcontentloaded", timeout=90_000)
                await self._wait_for_composer()
            else:
                await self._new_chat()
            await self._ensure_model(model)

            body, status = await self._send_with_captcha_retry(prompt, image_mode=False)
            if status != 200:
                raise QwenBrowserError(f"Qwen answered HTTP {status}: {body[:300]}")

            chat_id = _chat_id_from(body)
            self.chat_id = chat_id
            return body, chat_id

    async def _send_with_captcha_retry(self, prompt: str, *, image_mode: bool, aspect: str = "2:3") -> tuple[str, int]:
        """Submit ``prompt``, waiting out a captcha once if one appears.

        A captcha is a wall only a human can clear: with a wait budget the window
        stays open, the page is reloaded once the challenge is gone, and (for
        images) the composer is switched back into image mode before resending.
        Without a wait budget the challenge is reported immediately - retrying
        blindly only feeds the risk score.
        """
        try:
            body, status = await self._submit(prompt)
        except QwenCaptchaRequired:
            raise
        except Exception as exc:
            # A timeout on ``expect_response`` means the page never answered -
            # often because a captcha covered it. Surfacing the raw timeout would
            # reach the API layer as a bare 500, so it is wrapped with the advice
            # that actually helps.
            raise QwenBrowserError(f"no completion captured; if a captcha is visible in the browser window, solve it and retry ({exc})") from exc
        if not _looks_like_waf(body):
            return body, status
        if not (self._captcha_wait and await self._await_captcha_cleared()):
            raise QwenCaptchaRequired(f"{CAPTCHA_DETAIL}. Solve the challenge in the open browser window.")
        await self._recover_after_captcha()
        if image_mode:
            # The reload resets the composer to Auto and to the default ratio, so
            # both the mode and the aspect have to be re-applied before resending.
            await self._select_image_mode()
            await self._select_aspect(aspect)
        body, status = await self._submit(prompt)
        if _looks_like_waf(body):
            raise QwenCaptchaRequired(f"{CAPTCHA_DETAIL}. The challenge is still up in the browser window.")
        return body, status

    async def capture_text(self, prompt: str, model: str, chat_id: str | None = None) -> str:
        """Send a text prompt through the UI and return the raw SSE body.

        Mirrors :meth:`capture` for the text path: the body is returned unparsed
        so the caller feeds it through the same reconstructor as the HTTP
        transport.
        """
        await self._ensure_started()
        page = self._page
        assert page is not None
        async with self._lock:
            if chat_id:
                await page.goto(f"{BASE_URL}/c/{chat_id}", wait_until="domcontentloaded", timeout=90_000)
                await self._wait_for_composer()
            else:
                await self._new_chat()
            await self._ensure_model(model)
            body, _ = await self._send_with_captcha_retry(prompt, image_mode=False)
            return body

    async def complete(self, prompt: str, model: str, chat_id: str | None = None) -> QwenStreamReconstructor:
        """Send ``prompt`` and parse the response into a reconstructor."""
        body, _ = await self.capture(prompt, model, chat_id)
        return self._parse(body)

    async def _select_image_mode(self) -> None:
        """Switch the composer into image generation.

        Qwen has no image model in the chat model picker: images come from a
        separate composer mode (``Create Image``), which is what binds the
        request to ``chat_type: t2i``.

        The current mode is only reflected in the DOM (``.mode-select-current-mode``)
        *after* a mode other than the default is active, so its absence means
        "Auto" rather than "broken page". Detection therefore looks for the
        label, and the switch itself is two clicks: open the menu, pick the item.
        """
        page = self._page
        assert page is not None
        current = ""
        try:
            if await page.locator(".mode-select-current-mode").count():
                current = (await page.locator(".mode-select-current-mode").first.inner_text()).strip()
        except Exception:
            current = ""
        if "Create Image" in current:
            return
        await page.locator(".mode-select-open").first.click()
        item = page.locator(".mode-select-dropdown-item-wrapper").filter(has_text="Create Image").first
        await item.wait_for(state="visible", timeout=15_000)
        await item.click()
        # Wait until the composer actually reports the new mode; clicking and
        # returning immediately would send the prompt in the old mode.
        deadline = asyncio.get_running_loop().time() + 20.0
        while asyncio.get_running_loop().time() < deadline:
            try:
                label = (await page.locator(".mode-select-current-mode").first.inner_text()).strip()
            except Exception:
                label = ""
            if "Create Image" in label:
                return
            await asyncio.sleep(0.4)
        raise QwenBrowserError("could not switch the Qwen composer into Create Image mode")

    async def _recover_after_captcha(self) -> None:
        """Reload the page after a captcha was solved.

        Solving the challenge makes baxia reload the SPA: the composer and the
        send button are re-rendered, so every locator captured before the
        challenge is stale and the selected mode is back to "Auto". Reloading
        deliberately here makes the recovery deterministic instead of racing the
        page's own navigation.
        """
        page = self._page
        assert page is not None
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=90_000)
        await self._wait_for_composer()
        self._model_wire = None

    async def _submit(self, prompt: str) -> tuple[str, int]:
        """Type ``prompt`` into the composer, send it and return the SSE body.

        The locators are resolved inside the call, not passed in: a captcha can
        re-render the page between two requests, and a locator captured earlier
        would then point at a detached node (which is exactly how a "send button
        not found" timeout appeared after a captcha was solved).
        """
        page = self._page
        assert page is not None
        textarea = page.locator(TEXTAREA).first
        await textarea.wait_for(state="visible", timeout=30_000)
        await textarea.click()
        await textarea.fill(prompt)
        await page.wait_for_timeout(400)
        async with page.expect_response(lambda r: "/chat/completions" in r.url, timeout=int(self._timeout * 1000)) as info:
            await page.locator(SEND_BUTTON).first.click()
        resp = await info.value
        body = await resp.text()
        # The SPA refreshes the access token on its own and may receive a rotated
        # refresh token; mirror it into the store before the value is lost.
        await self._sync_token_from_profile()
        return body, resp.status

    async def _await_captcha_cleared(self) -> bool:
        """Wait for a human to solve the captcha in the open browser window.

        baxia asks for a slider the automation cannot drag, so the only way past
        it is a person: the window stays open and this polls until the chat UI
        is usable again. Returns ``True`` when the challenge looks cleared.
        """
        page = self._page
        assert page is not None
        deadline = asyncio.get_running_loop().time() + self._captcha_wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3.0)
            try:
                html = await page.content()
                if await page.locator(TEXTAREA).count() and not _looks_like_waf(html):
                    log.info("qwen captcha cleared, retrying")
                    return True
            except Exception:
                continue
        return False

    async def _select_aspect(self, aspect: str) -> None:
        """Pick the image aspect ratio in the composer's sub-config.

        The ratio lives next to the image model in the composer footer and is
        sent to the backend as ``extra.meta.size`` (``"2:3"`` and so on). It is
        part of the composer state, not the request, so it has to be set before
        every send and re-applied after a reload.
        """
        page = self._page
        assert page is not None
        current = page.locator(".message-input-subconfig-visible-item").filter(has_text=":").first
        try:
            label = (await current.inner_text()).strip()
        except Exception:
            raise QwenBrowserError("aspect ratio selector not found in the Qwen composer") from None
        if label == aspect:
            return
        await current.click()
        await page.wait_for_timeout(1200)
        option = page.locator(".qwen-chat-v2-dropdown-menu-item").filter(has_text=aspect).first
        await option.wait_for(state="visible", timeout=15_000)
        await option.click()
        await page.wait_for_timeout(1200)
        label = (await current.inner_text()).strip()
        if label != aspect:
            raise QwenBrowserError(f"could not set the image aspect ratio to {aspect} (composer shows {label})")

    async def generate_image(self, prompt: str, aspect: str = "2:3", chat_id: str | None = None) -> QwenStreamReconstructor:
        """Generate an image through the UI and return the parsed result.

        The chat model picker has no image models, so the composer is switched
        into ``Create Image`` instead; the SPA then sends ``chat_type: t2i`` and
        the answer carries CDN urls, which the shared reconstructor collects into
        ``image_urls``.

        ``chat_id`` reuses an existing conversation instead of opening a new one.
        That matters more than it looks: Qwen throttles - and eventually blocks
        for a day - an account that keeps creating new chats, so every image for
        an account belongs in one pinned conversation.
        """
        await self._ensure_started()
        page = self._page
        assert page is not None
        async with self._lock:
            if chat_id:
                await self._open_chat(chat_id)
            else:
                await self._new_chat()
            # Both are idempotent, so re-applying them to a reused chat costs
            # nothing and covers the case where the page came up in Auto mode.
            await self._select_image_mode()
            await self._select_aspect(aspect)

            body, status = await self._send_with_captcha_retry(prompt, image_mode=True, aspect=aspect)
            if status != 200:
                raise QwenBrowserError(f"Qwen answered HTTP {status}: {body[:300]}")

            self.chat_id = _chat_id_from(body)
            return self._parse(body)

    @staticmethod
    def _parse(body: str) -> QwenStreamReconstructor:
        """Feed the captured SSE body through the shared reconstructor."""
        from ..deepseek.stream import IncrementalSSE

        rec = QwenStreamReconstructor()
        incremental = IncrementalSSE()
        for event in incremental.feed(body.encode("utf-8")):
            rec.handle(event)
        for event in incremental.finish():
            rec.handle(event)
        return rec

    async def complete_stream(self, prompt: str, model: str, chat_id: str | None = None) -> AsyncIterator[str]:
        """Yield the reconstructed answer text in one piece.

        The SPA delivers the whole body through ``expect_response``; streaming it
        chunk by chunk would need the fetch tap z.ai uses. One piece keeps the
        implementation small and is enough for the pipeline, which consumes the
        reconstructed result rather than the deltas.
        """
        rec = await self.complete(prompt, model, chat_id)
        yield rec.content
