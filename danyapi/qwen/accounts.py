from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from ..sessions import SessionRegistry
from ..store import JsonStore
from .client import QwenClient, QwenError, QwenSession

log = logging.getLogger("danyapi.qwen")


def _token_fingerprint(token: str) -> str:
    """Stable id for a credential, used to bind a rotation store to its source."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


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
    """
    if store is None:
        return
    store.set(_token_fingerprint(configured), new_token)


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
    __slots__ = ("_sticky", "_sticky_store", "broken", "client", "index", "quota_until", "sem", "sessions")

    def __init__(
        self,
        index: int,
        client: QwenClient,
        session_cache_size: int = 128,
        ttl: float = 0.0,
        store: JsonStore | None = None,
        sticky_store: JsonStore | None = None,
    ) -> None:
        self.index = index
        self.client = client
        self.sem = asyncio.Semaphore(1)
        self.sessions = QwenSessionRegistry(client, session_cache_size, ttl, store=store, key_prefix=f"{index}:")
        self.broken = False
        # Monotonic deadline until which a quota-exhausted account stays out of
        # rotation. 0.0 means no timed block (either healthy, or broken forever
        # by an auth error).
        self.quota_until = 0.0
        self._sticky_store = sticky_store
        self._sticky: dict[str, str] = {}

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
            log.warning("qwen account #%d marked broken (invalid/expired token)", self.index)

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

    def is_available(self) -> bool:
        """True when the account may be used for a request right now.

        A quota cooldown that has elapsed automatically puts the account back
        into rotation. Accounts broken by ``mark_broken`` (auth errors) carry
        ``quota_until == 0.0`` and stay unavailable forever.
        """
        if self.broken and self.quota_until and time.monotonic() >= self.quota_until:
            self.broken = False
            self.quota_until = 0.0
            log.info("qwen account #%d quota cooldown elapsed, back in rotation", self.index)
        return not self.broken

    @property
    def label(self) -> str:
        return f"qwen-acct#{self.index}"
