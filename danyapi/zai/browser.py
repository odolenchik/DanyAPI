"""Browser-backed completion for chat.z.ai.

chat.z.ai generates the request signature/fingerprint inside its own SPA, so
instead of reimplementing that this module drives a real headed Chrome via
Playwright: it types the prompt into the chat UI and lets the frontend build the
request. Authentication normally comes from a persistent Chrome profile — the
user logs in once (Google/GitHub OAuth) and later runs reuse the saved session.
A token from ``ZAI_TOKENS`` is optional and merely injected as a cookie.

Both non-streaming (:meth:`ZaiBrowser.complete`) and streaming
(:meth:`ZaiBrowser.complete_stream`) are supported; the captured SSE body is
parsed here into plain text/thinking/usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import resolve_zai_model

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - depends on the environment
    async_playwright = None

log = logging.getLogger("danyapi.zai.browser")

TEXTAREA = "textarea"
SUBMIT = "button[type='submit']"
COMPLETIONS_PATH = "/chat/completions"
STREAM_POLL_SEC = 0.5
# Selector button id is ``model-selector-<wire>-button``; dots are replaced by
# underscores in the DOM id, so compare against the sanitised wire id.
MODEL_BUTTON_PREFIX = "model-selector-"
MODEL_MENU = "[role='menu']"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
# Onboarding / "model upgraded" modals ship with their own full-screen backdrop
# that sits above the composer and swallows pointer events: while it is open a
# submit click silently does nothing. The overlay also covers its own close
# button, so the modal is dismissed with Escape (falling back to removing the
# overlay node when Escape does not clear it).
MODAL_OVERLAY = "div[data-dialog-overlay], ._modal-overlay"
REMOVE_MODALS_JS = "() => { document.querySelectorAll('div[data-dialog-overlay], ._modal-overlay').forEach(e => e.remove()); }"
MODAL_SETTLE_MS = 300


class ZaiBrowserError(Exception):
    pass


class _CompletionAccumulator:
    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.usage: dict = {}
        self.done = False
        self.error: dict | None = None


def _parse_zai_sse_line(line: str) -> dict | None:
    """Extract the inner ``data`` payload of one chat.z.ai SSE line.

    Returns ``None`` for blank / non-``data:`` lines, ``[DONE]``, malformed JSON,
    or events that are not a ``chat:completion``. A missing/empty ``data`` object
    is normalised to ``{}`` so callers can treat every yielded value uniformly.
    """
    line = line.rstrip("\r")
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "chat:completion":
        return None
    data = event.get("data")
    return data if isinstance(data, dict) else {}


class _IncrementalZaiSSE:
    """Line-buffered parser for chat.z.ai SSE that tolerates arbitrary chunking.

    CDP ``Network.dataReceived`` delivers raw bytes in chunks that do not align
    with line boundaries, so we keep a trailing partial line across :meth:`feed`
    calls and only emit fully-received ``data:`` lines.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[dict]:
        self._buf += text
        events: list[dict] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            data = _parse_zai_sse_line(line)
            if data is not None:
                events.append(data)
        return events

    def finish(self) -> list[dict]:
        remaining, self._buf = self._buf, ""
        data = _parse_zai_sse_line(remaining)
        return [data] if data is not None else []


def _apply_event(acc: _CompletionAccumulator, data: dict) -> None:
    """Fold one parsed chat.z.ai completion event into an accumulator."""
    if data.get("error"):
        err = data["error"]
        acc.error = err if isinstance(err, dict) else {"detail": str(err)}
        return
    phase = data.get("phase")
    delta = data.get("delta_content") or ""
    if phase == "thinking":
        acc.reasoning += delta
    elif phase == "answer":
        acc.content += delta
    if isinstance(data.get("usage"), dict):
        acc.usage = data["usage"]
    if data.get("done") or phase == "done":
        acc.done = True


def parse_sse_body(body: str) -> _CompletionAccumulator:
    """Parse a complete chat.z.ai SSE body into an accumulator (non-stream path)."""
    acc = _CompletionAccumulator()
    parser = _IncrementalZaiSSE()
    for data in parser.feed(body):
        _apply_event(acc, data)
    for data in parser.finish():
        _apply_event(acc, data)
    return acc


# Injected before every document load: wraps window.fetch so that the
# /chat/completions response body is teed — one branch is left untouched for the
# app, the other is decoded by the browser (which also undoes the response's
# Content-Encoding: br) and forwarded to Python via the exposed bindings.
# Reading the body through CDP instead is not viable: Network.dataReceived
# carries only byte counts, and the raw bytes are Brotli-compressed.
FETCH_TAP_SCRIPT = r"""
(() => {
  if (window.__zaiPatched) return;
  window.__zaiPatched = true;
  const orig = window.fetch;
  window.fetch = async function(...args) {
    const resp = await orig.apply(this, args);
    try {
      const a0 = args[0];
      const url = (a0 && a0.url) ? a0.url : (typeof a0 === 'string' ? a0 : '');
      if (url.includes('/chat/completions') && resp.body) {
        const [keep, tap] = resp.body.tee();
        (async () => {
          const reader = tap.getReader();
          const dec = new TextDecoder();
          try {
            for (;;) {
              const {done, value} = await reader.read();
              if (done) break;
              window.__zaiChunk(dec.decode(value, {stream: true}));
            }
          } catch (e) {}
          window.__zaiEnd();
        })();
        return new Response(keep, {status: resp.status, statusText: resp.statusText, headers: resp.headers});
      }
    } catch (e) {}
    return resp;
  };
})();
"""


class ZaiBrowser:
    def __init__(self, token: str, timeout: float = 120.0) -> None:
        self._token = token
        self._timeout = timeout
        self._pw: Any | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._started = False
        # Wire id of the model currently bound to the open chat, or None before
        # the first selection. chat.z.ai binds a model to the conversation, so a
        # change of model also means starting a fresh chat.
        self._model_wire: str | None = None
        # Set while complete_stream is active; the injected fetch tap forwards
        # decoded response text here ("" on end-of-body).
        self._stream_queue: asyncio.Queue[str] | None = None

    @property
    def page(self) -> Page | None:
        return self._page

    async def _dismiss_modals(self) -> bool:
        """Close any onboarding / "model upgraded" modal blocking the composer.

        These popups render a full-screen backdrop above the page and swallow
        pointer events, so an open modal turns ``fill`` + ``click`` into silent
        no-ops. The backdrop also covers the modal's own close button, hence
        Escape first and a DOM removal as the fallback. Returns ``True`` when
        something was dismissed.
        """
        page = self._page
        if page is None:
            return False
        try:
            count = await page.locator(MODAL_OVERLAY).count()
        except Exception:
            return False
        if not count:
            return False
        log.info("zai modal detected (%d overlay(s)), dismissing", count)
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(MODAL_SETTLE_MS)
            count = await page.locator(MODAL_OVERLAY).count()
        except Exception:
            # Escape failed, so nothing can be assumed about the overlay: still
            # try the DOM removal below.
            count = 1
        if count:
            try:
                await page.evaluate(REMOVE_MODALS_JS)
                await page.wait_for_timeout(MODAL_SETTLE_MS)
            except Exception:
                pass
        return True

    async def _select_model(self, model_id: str) -> None:
        """Bind ``model_id`` to the browser, switching between requests.

        chat.z.ai ignores any model we send: the SPA stamps whichever model is
        selected in its own dropdown into the request body, and binds that
        choice to the conversation — once a chat has run the picker is disabled
        and the page sits on ``/c/<id>``. So to apply a model (and to change it
        on a later request) we write the desired wire id to
        ``localStorage.selectedModels`` and navigate back to the chat root,
        which drops the bound conversation and lets the fresh chat pick up the
        new selection.
        """
        spec = resolve_zai_model(model_id)
        if spec.wire == self._model_wire:
            return
        assert self._page is not None
        page = self._page
        await page.evaluate(
            "(w) => window.localStorage.setItem('selectedModels', JSON.stringify([w]))",
            spec.wire,
        )
        await page.goto("https://chat.z.ai/", wait_until="domcontentloaded")
        await self._dismiss_modals()
        await page.locator(TEXTAREA).first.wait_for(state="visible", timeout=30_000)
        self._model_wire = spec.wire
        log.info("zai model selected: %s (wire=%s)", spec.label, spec.wire)

    def _on_stream_chunk(self, _source: Any, text: str) -> None:
        """Receives decoded response text from the injected fetch tap."""
        if self._stream_queue is not None and text:
            self._stream_queue.put_nowait(text)

    def _on_stream_end(self, _source: Any) -> None:
        """Signals that the tapped response body reached EOF."""
        if self._stream_queue is not None:
            self._stream_queue.put_nowait("")

    async def start(self) -> None:
        if self._started:
            return
        if async_playwright is None:
            raise ZaiBrowserError("playwright is not installed; run `pip install playwright && playwright install chrome` to use the z.ai provider")

        profile_dir = Path(os.environ.get("ZAI_BROWSER_DIR", str(Path.home() / ".danyapi" / "zai-browser")))
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        # Persistent context: cookies/localStorage survive restarts. First run
        # the user logs in manually (Google/GitHub OAuth); subsequent runs reuse
        # the saved session without re-authentication.
        self._context = await self._pw.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
        )
        await self._context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        await self._context.expose_binding("__zaiChunk", self._on_stream_chunk)
        await self._context.expose_binding("__zaiEnd", self._on_stream_end)
        await self._context.add_init_script(FETCH_TAP_SCRIPT)
        # If a token was provided via env var, also inject it (works when the
        # user extracted a valid web session token from DevTools).
        if self._token:
            try:
                await self._context.add_cookies([{"name": "token", "value": self._token, "domain": ".z.ai", "path": "/"}])
            except Exception:
                pass  # cookie may already exist from persistent profile
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        await self._page.goto("https://chat.z.ai/", wait_until="domcontentloaded")
        await self._page.wait_for_timeout(4000)
        if self._token:
            try:
                await self._page.evaluate("t => localStorage.setItem('token', t)", self._token)
                await self._page.reload(wait_until="domcontentloaded")
                await self._page.wait_for_timeout(8000)
            except Exception:
                pass
        # A first-run onboarding / "model upgraded" modal can cover the composer;
        # clear it before waiting for the textarea (and before every later send).
        await self._dismiss_modals()
        # Generous timeout: first run the user must complete OAuth login manually.
        await self._page.locator(TEXTAREA).first.wait_for(state="visible", timeout=120_000)
        self._started = True
        log.info("zai browser started (profile=%s)", profile_dir)

    async def aclose(self) -> None:
        # With launch_persistent_context the context IS the browser; close it directly.
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

    async def complete(self, prompt: str, model: str | None = None) -> _CompletionAccumulator:
        if not self._started or self._page is None:
            await self.start()
        assert self._page is not None
        async with self._lock:
            await self._select_model(model)
            captured: list[str] = []

            async def on_response(resp):
                if resp.request.method == "POST" and COMPLETIONS_PATH in resp.url and not captured:
                    try:
                        captured.append(await resp.text())
                    except Exception as exc:
                        captured.append(f"<response body error: {exc}>")

            self._page.on("response", on_response)
            try:
                # The model may already be bound, in which case no navigation
                # happened and a modal could still be covering the composer.
                await self._dismiss_modals()
                await self._page.locator(TEXTAREA).first.fill(prompt)
                await self._page.locator(SUBMIT).first.click()
                deadline = asyncio.get_event_loop().time() + self._timeout
                while not captured and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.5)
            finally:
                self._page.remove_listener("response", on_response)

            if not captured:
                raise ZaiBrowserError("no chat.completions response captured; solve the CAPTCHA in the open browser window and retry")
            return parse_sse_body(captured[0])

    async def complete_stream(self, prompt: str, model: str | None = None) -> AsyncIterator[dict]:
        """Stream parsed chat.z.ai completion events as the frontend receives them.

        The body is read by the fetch tap injected in :meth:`start`, which hands
        the browser-decoded text to Python chunk by chunk; this method parses the
        SSE incrementally and yields each event's inner ``data`` payload, so
        callers can emit OpenAI chunks progressively. Reading is bounded by
        ``self._timeout``: if no ``/chat/completions`` response is seen the stream
        ends gracefully with whatever (if anything) arrived.
        """
        if not self._started or self._page is None:
            await self.start()
        assert self._page is not None
        page = self._page
        async with self._lock:
            await self._select_model(model)
            parser = _IncrementalZaiSSE()
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._stream_queue = queue
            try:
                # The model may already be bound, in which case no navigation
                # happened and a modal could still be covering the composer.
                await self._dismiss_modals()
                await page.locator(TEXTAREA).first.fill(prompt)
                await page.locator(SUBMIT).first.click()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._timeout
                while True:
                    try:
                        text = await asyncio.wait_for(queue.get(), timeout=STREAM_POLL_SEC)
                    except asyncio.TimeoutError:  # noqa: UP041 (py3.10: distinct from builtin)
                        if loop.time() > deadline:
                            break  # no response within timeout -> end gracefully
                        continue
                    if text == "":
                        break  # response body finished; flush the trailing line
                    for data in parser.feed(text):
                        yield data
            finally:
                self._stream_queue = None
            for data in parser.finish():
                yield data
