from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..sessions import SessionRegistry
from ..store import JsonStore
from .client import QwenClient, QwenError, QwenSession

log = logging.getLogger("danyapi.qwen")


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
    __slots__ = ("_sticky", "_sticky_store", "broken", "client", "index", "sem", "sessions")

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

    @property
    def label(self) -> str:
        return f"qwen-acct#{self.index}"
