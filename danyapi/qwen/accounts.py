from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any

from ..sessions import SessionRegistry
from ..store import JsonStore
from .client import QwenClient, QwenError, QwenSession, jwt_claims

log = logging.getLogger("danyapi.qwen")


def _token_fingerprint(token: str) -> str:
    """Stable id for a credential, used to bind a rotation store to its source."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _token_issued_at(token: str | None) -> float | None:
    """The ``iat`` claim of a token, or ``None`` when it cannot be read.

    Qwen rotates the refresh token server-side and stamps each one with its
    issue time, so ``iat`` orders two credentials without trusting either one:
    the later value is the one the server still honours. Anything that is not a
    decodable JWT carrying a numeric ``iat`` returns ``None`` and is treated as
    incomparable.
    """
    iat = jwt_claims(token).get("iat")
    if isinstance(iat, (int, float)):
        return float(iat)
    return None


def load_effective_token(configured: str, store: JsonStore | None) -> str:
    """Return the credential to use, preferring a rotated one from ``store``.

    Qwen rotates the refresh token server-side, so the value in ``.env`` goes
    stale while a freshly rotated one lives in the store. Each account's rotated
    value is keyed by the fingerprint of its own configured token, so several
    accounts sharing one store never overwrite each other. The stored token is
    honoured only while the configured value still matches the one it was
    derived from, so editing ``.env`` deliberately replaces it.
    """
    if store is None:
        return configured
    fingerprint = _token_fingerprint(configured)
    stored = store.get(fingerprint)
    if not (isinstance(stored, str) and stored):
        # Legacy single-slot format ({source, token}), from before rotations were
        # stored per account. Read it once so the value is not lost on upgrade.
        if store.get("source") == fingerprint:
            stored = store.get("token")
    if isinstance(stored, str) and stored:
        if stored != configured:
            log.info("qwen using rotated refresh token from credential store")
        return stored
    return configured


def persist_rotated_token(configured: str, store: JsonStore | None, new_token: str) -> None:
    """Persist a rotated refresh token so it survives a restart.

    Keyed by the fingerprint of the configured token, so every account in the
    pool keeps its own rotated credential instead of sharing a single slot.

    The store is the single source of truth for a rotated credential, and a
    stale writer must never roll it back: an account has two independent
    transports (HTTP client and browser), and the one that rotated later holds
    the value the server still honours. If the slot already holds a token issued
    later than ``new_token`` (by ``iat``), that copy is kept; an incomparable
    pair is written as before.
    """
    if store is None:
        return
    key = _token_fingerprint(configured)
    existing = store.get(key)
    if isinstance(existing, str) and existing and existing != new_token:
        stored_at = _token_issued_at(existing)
        new_at = _token_issued_at(new_token)
        if stored_at is not None and new_at is not None and stored_at >= new_at:
            log.info(
                "qwen kept the newer stored credential (%s) over an older rotation (%s)",
                _token_fingerprint(existing),
                _token_fingerprint(new_token),
            )
            return
    store.set(key, new_token)


class QwenSessionRegistry(SessionRegistry):
    def __init__(
        self,
        client: QwenClient,
        maxsize: int = 128,
        ttl: float = 0.0,
        store: JsonStore | None = None,
        key_prefix: str = "",
    ) -> None:
        super().__init__(client, maxsize, ttl, store=store, key_prefix=key_prefix)

    def _serialize(self, session: QwenSession) -> dict:
        return {
            "id": session.id,
            "title": session.title,
            "last_response_id": session.last_response_id,
            "model": session.model,
            "chat_type": getattr(session, "chat_type", "t2t"),
            "accumulated_input_tokens": getattr(session, "accumulated_input_tokens", 0),
            "accumulated_output_tokens": getattr(session, "accumulated_output_tokens", 0),
            "reattached": bool(getattr(session, "reattached", False)),
        }

    def _deserialize(self, record: Any) -> QwenSession:
        if not isinstance(record, dict) or not record.get("id"):
            raise ValueError("invalid session record")
        input_tokens = record.get("accumulated_input_tokens")
        output_tokens = record.get("accumulated_output_tokens")
        return QwenSession(
            id=record["id"],
            title=record.get("title") or "",
            last_response_id=record.get("last_response_id"),
            model=record.get("model"),
            chat_type=record.get("chat_type") or "t2t",
            accumulated_input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else 0,
            accumulated_output_tokens=int(output_tokens) if isinstance(output_tokens, (int, float)) else 0,
            reattached=bool(record.get("reattached", False)),
        )

    async def _create(self, **kwargs) -> QwenSession:
        model = kwargs.get("model") or ""
        chat_type = kwargs.get("chat_type") or "t2t"
        chat_id = await self._client.create_chat(model=model, chat_mode="normal", chat_type=chat_type)
        return QwenSession(id=chat_id, model=model, chat_type=chat_type)

    async def _attach(self, session_id: str, **kwargs) -> QwenSession | None:
        try:
            detail = await self._client.fetch_chat(session_id)
        except QwenError:
            return None
        if not isinstance(detail, dict):
            return None
        chat_type = detail.get("chat_type") or "t2t"
        if chat_type != (kwargs.get("chat_type") or "t2t"):
            # Never resume a text chat with an image request (or vice versa).
            return None
        history = (detail.get("chat") or {}).get("history") or {}
        last_response_id = history.get("currentId")
        if not last_response_id:
            messages = (detail.get("chat") or {}).get("messages") or []
            assistants = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
            if assistants:
                last_response_id = assistants[-1].get("id")
        if not last_response_id:
            return None
        return QwenSession(
            id=session_id,
            title=detail.get("title") or "",
            last_response_id=last_response_id,
            model=kwargs.get("model"),
            chat_type=chat_type,
            reattached=True,
        )

    def _reuse(self, session: QwenSession, session_id: str, **kwargs) -> bool:
        if session.model != (kwargs.get("model") or ""):
            return False
        return getattr(session, "chat_type", "t2t") == (kwargs.get("chat_type") or "t2t")

    def _update_last(self, session: QwenSession, message_id: str) -> None:
        session.last_response_id = message_id

    async def obtain(
        self,
        session_id: str | None = None,
        model: str | None = None,
        chat_type: str | None = None,
        **kwargs,
    ) -> tuple[QwenSession, str]:
        return await super().obtain(session_id, model=model or "", chat_type=chat_type or "t2t")


class QwenAccount:
    __slots__ = (
        "_browser_interval",
        "_browser_interval_jitter",
        "_burst",
        "_burst_pause",
        "_burst_pause_jitter",
        "_cycle_start",
        "_min_interval",
        "_min_interval_jitter",
        "_pacing_delay",
        "_since_gap",
        "_starts",
        "_sticky",
        "_sticky_store",
        "broken",
        "broken_at",
        "browser",
        "captcha_at",
        "captcha_until",
        "client",
        "index",
        "last_revive_at",
        "quota_until",
        "sem",
        "sessions",
    )

    def __init__(
        self,
        index: int,
        client: QwenClient,
        session_cache_size: int = 128,
        ttl: float = 0.0,
        store: JsonStore | None = None,
        sticky_store: JsonStore | None = None,
        burst: int = 1,
        min_interval: float = 0.0,
        min_interval_jitter: float = 0.0,
        burst_pause: float = 0.0,
        burst_pause_jitter: float = 0.0,
        browser: object | None = None,
        browser_interval: float = 0.0,
        browser_interval_jitter: float = 0.0,
    ) -> None:
        self.index = index
        self.client = client
        # Optional browser transport. Qwen's anti-bot wall rejects every plain
        # HTTP request for image generation, so a browser is the only way to get
        # images; text keeps using the fast HTTP client.
        self.browser = browser
        self.sem = asyncio.Semaphore(1)
        self.sessions = QwenSessionRegistry(client, session_cache_size, ttl, store=store, key_prefix=f"{index}:")
        self.broken = False
        # When the account broke (monotonic) and when the revive loop last
        # probed it. The revive loop uses both to pick its cadence; both are
        # cleared by ``revive()``.
        self.broken_at: float | None = None
        self.last_revive_at: float | None = None
        # Monotonic deadline until which a quota-exhausted account stays out of
        # rotation. 0.0 means no timed block (either healthy, or broken forever
        # by an auth error).
        self.quota_until = 0.0
        # Anti-bot captcha (``RGV587_ERROR``) is not a dead token and not a
        # quota: the credential still refreshes, but Qwen answers every request
        # with a punish page until a human solves the captcha. Keep the account
        # out of rotation for a while and report it separately from an auth
        # break, so /health tells "captcha" apart from "token dead".
        self.captcha_until = 0.0
        self.captcha_at = 0.0
        self._sticky_store = sticky_store
        self._sticky: dict[str, str] = {}
        # Pacing: ``burst`` requests, a jittered pause between them, then a long
        # jittered rest. Qwen punishes a driven account with a captcha that
        # sticks for hours, so the only defence is to stay under the radar.
        self._burst = max(1, burst)
        self._min_interval = max(0.0, min_interval)
        self._min_interval_jitter = max(0.0, min_interval_jitter)
        self._burst_pause = max(0.0, burst_pause)
        self._burst_pause_jitter = max(0.0, burst_pause_jitter)
        # A browser request is slow on its own, so it is paced by an even think
        # time rather than by bursts: ``browser_interval`` plus jitter before
        # every request, no long rests.
        self._browser_interval = max(0.0, browser_interval)
        self._browser_interval_jitter = max(0.0, browser_interval_jitter)
        self._since_gap = 0
        self._cycle_start = 0.0
        self._pacing_delay: float | None = None
        self._starts: list[float] = []

    def _sticky_key(self, model: str) -> str:
        return f"{self.index}:{model}"

    def sticky_image_chat(self, model: str) -> str | None:
        """Return the pinned image chat for ``model`` on this account, if any.

        Image generation must keep reusing one chat: Qwen starts limiting (and
        eventually blocks for a day) once an account creates many new chats.
        A pinned chat per account + image model keeps every prompt in the same
        conversation.
        """
        if model in self._sticky:
            return self._sticky[model]
        if self._sticky_store is not None:
            value = self._sticky_store.get(self._sticky_key(model))
            if isinstance(value, str) and value:
                self._sticky[model] = value
                return value
        return None

    def set_sticky_image_chat(self, model: str, chat_id: str) -> None:
        if not chat_id:
            return
        self._sticky[model] = chat_id
        if self._sticky_store is not None:
            self._sticky_store.set(self._sticky_key(model), chat_id)

    def forget_sticky_image_chat(self, model: str) -> None:
        self._sticky.pop(model, None)
        if self._sticky_store is not None:
            self._sticky_store.discard(self._sticky_key(model))

    def mark_broken(self) -> None:
        if not self.broken:
            self.broken = True
            self.broken_at = time.monotonic()
            log.warning("qwen account #%d marked broken (invalid/expired token)", self.index)

    def revive(self) -> None:
        """Return a broken account to rotation after its credential checked out.

        An auth break is not always permanent: the refresh token in memory can
        be stale while the store already holds the rotated one (the HTTP client
        and the browser rotate independently), and an upstream outage can fail
        a probe. The revive loop calls this once ``check_auth`` reports success
        so the account is not out until the process restarts.
        """
        if self.broken:
            self.broken = False
            self.broken_at = None
            self.last_revive_at = None
            log.info("qwen account #%d revived", self.index)

    def mark_quota_exhausted(self, cooldown_seconds: float) -> None:
        """Take the account out of rotation until its quota window resets.

        Unlike an auth failure this is temporary: the deadline stored in
        ``quota_until`` lets ``is_available`` return the account on its own.
        """
        self.broken = True
        self.quota_until = time.monotonic() + max(0.0, cooldown_seconds)
        log.warning(
            "qwen account #%d marked quota-exhausted, cooling down %.0fs",
            self.index,
            max(0.0, cooldown_seconds),
        )

    def mark_captcha(self, cooldown_seconds: float) -> None:
        """Take the account out of rotation: Qwen wants a captcha solved.

        The credential is fine — the access token keeps refreshing — but the
        anti-bot system answers every request with a punish page until a human
        solves the captcha. Retrying is worse than useless: each attempt feeds
        the risk score. So the account rests, and since only a human can clear
        the challenge the cooldown is long by default.
        """
        self.broken = True
        self.captcha_at = time.monotonic()
        self.captcha_until = self.captcha_at + max(0.0, cooldown_seconds)
        log.warning(
            "qwen account #%d hit the anti-bot captcha, cooling down %.0fs until it is solved",
            self.index,
            max(0.0, cooldown_seconds),
        )

    def is_available(self) -> bool:
        """True when the account may be used for a request right now.

        A quota or captcha cooldown that has elapsed automatically puts the
        account back into rotation. Accounts broken by ``mark_broken`` (auth
        errors) carry ``quota_until == 0.0`` and stay unavailable forever.
        """
        now = time.monotonic()
        if self.broken and self.quota_until and now >= self.quota_until:
            self.broken = False
            self.quota_until = 0.0
            log.info("qwen account #%d quota cooldown elapsed, back in rotation", self.index)
        if self.broken and self.captcha_until and now >= self.captcha_until:
            self.broken = False
            self.captcha_until = 0.0
            log.info("qwen account #%d captcha cooldown elapsed, back in rotation", self.index)
        return not self.broken

    @property
    def pacing_delay(self) -> float | None:
        """Seconds the last ``wait_turn`` call had to sleep, ``None`` if none."""
        return self._pacing_delay

    @property
    def burst(self) -> int:
        return self._burst

    def seconds_until_ready(self, now: float | None = None) -> float:
        """How long before this account may start another request.

        Pacing runs in cycles: ``burst`` starts are spaced by ``min_interval``
        plus a random jitter (so the gaps never repeat exactly), and once the
        burst is used up the account rests for ``burst_pause`` plus jitter.
        The jitter is drawn once per gap, when the previous request starts,
        and stored — otherwise every reading of the deadline would shuffle it.
        """
        if self._min_interval <= 0 and self._burst_pause <= 0 and self._browser_interval <= 0:
            return 0.0
        ts = time.monotonic() if now is None else now
        if self._pacing_delay is None:
            return 0.0
        return max(0.0, self._cycle_start + self._pacing_delay - ts)

    async def wait_turn(self) -> None:
        """Sleep until this account is allowed to start another request."""
        delay = self.seconds_until_ready()
        if delay > 0:
            log.debug("qwen account #%d pacing: sleeping %.1fs", self.index, delay)
            await asyncio.sleep(delay)

    def note_request_start(self, now: float | None = None) -> None:
        """Record a request start and advance the pacing cycle.

        The gap that must follow this request is chosen here, by jittering the
        base interval that applies next: ``min_interval`` inside a burst, the
        long ``burst_pause`` after it.
        """
        ts = time.monotonic() if now is None else now
        starts = self._starts
        starts.append(ts)
        cutoff = ts - 60.0
        while starts and starts[0] < cutoff:
            starts.pop(0)
        if self._browser_interval > 0:
            # Browser transport: one even think time before every request. The
            # request itself already takes tens of seconds, so bursts and long
            # rests would only stall the pipeline.
            extra = random.uniform(0.0, self._browser_interval_jitter) if self._browser_interval_jitter > 0 else 0.0
            self._cycle_start = ts
            self._pacing_delay = self._browser_interval + extra
            return
        if self._min_interval <= 0 and self._burst_pause <= 0:
            return
        self._since_gap += 1
        if self._since_gap >= self._burst:
            base = self._burst_pause
            jitter = self._burst_pause_jitter
            self._since_gap = 0
        else:
            base = self._min_interval
            jitter = self._min_interval_jitter
        extra = random.uniform(0.0, jitter) if jitter > 0 else 0.0
        self._cycle_start = ts
        self._pacing_delay = base + extra

    def recent_request_count(self, window: float, now: float | None = None) -> int:
        """How many request starts fall inside the trailing ``window`` seconds."""
        ts = time.monotonic() if now is None else now
        cutoff = ts - max(0.0, window)
        return sum(1 for start in self._starts if start >= cutoff)

    @property
    def label(self) -> str:
        return f"qwen-acct#{self.index}"
