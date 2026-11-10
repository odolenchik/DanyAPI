import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.api.openai as openai_mod
import danyapi.qwen.api as qwen_api
from danyapi.accounts import AccountPool, DeepSeekAccount
from danyapi.deepseek.client import DeepSeekClient
from danyapi.qwen.accounts import QwenSessionRegistry
from danyapi.qwen.client import QwenSession
from danyapi.sessions import SessionRegistry
from danyapi.store import JsonStore
from danyapi.tools import build_prompt, context_sequence

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class Message:
    def __init__(self, role="user", content: Any = "", tool_calls=None, tool_call_id=None, name=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.name = name


class FakeSessionClient:
    def __init__(self):
        self.counter = 0

    async def create_session(self):
        self.counter += 1
        return SimpleNamespace(id=f"s{self.counter}")

    async def create_chat(self, model="", chat_mode="normal", chat_type="t2t"):
        self.counter += 1
        return f"c{self.counter}"


def make_acct(i):
    client = MagicMock(spec=DeepSeekClient)
    client.index = i
    return DeepSeekAccount(i, client)


def test_same_context_same_sequence():
    assert context_sequence([Message("user", "hello")]) == context_sequence([Message("user", "hello")])


def test_assistant_and_tool_excluded():
    full = [
        Message("user", "hi"),
        Message("assistant", "hello"),
        Message("assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
        Message("tool", "42", tool_call_id="c1"),
    ]
    # Assistant/tool messages are not part of the stable anchor, so the
    # fingerprint equals the leading user digest prefix alone.
    base = context_sequence([Message("user", "hi")])
    full_seq = context_sequence(full)
    assert full_seq == base


def test_system_included():
    seq = context_sequence([Message("system", "sys"), Message("user", "q")])
    assert len(seq) == 2


def test_content_change_changes_fingerprint():
    assert context_sequence([Message("user", "one")]) != context_sequence([Message("user", "two")])


def test_growth_keeps_prefix():
    one = context_sequence([Message("user", "hi")])
    two = context_sequence([Message("user", "hi"), Message("assistant", "yo"), Message("user", "next")])
    # assistant messages are excluded, so the sequence is user_hi → user_next.
    assert one[0] == two[0]
    assert len(two) == 2  # user_hi, user_next


def test_list_content_fingerprint():
    a = [Message("user", [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}])]
    b = [Message("user", [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,yy"}}])]
    assert context_sequence(a) != context_sequence(b)


def test_context_empty():
    assert context_sequence([]) == ()
    # assistant-only message is not part of the anchor, so the fingerprint is empty.
    seq = context_sequence([Message("assistant", "a")])
    assert seq == ()


def test_user_scopes_fingerprint():
    base = [Message("user", "hello")]
    assert context_sequence(base) == context_sequence(base)
    assert context_sequence(base, user="alice") != context_sequence(base, user="bob")
    assert context_sequence(base, user="alice") == context_sequence(base, user="alice")


def test_ephemeral_system_reminder_ignored():
    base = [Message("system", "You are Kimi Code CLI"), Message("user", "hello")]
    with_reminder = [*base, Message("system", "<system-reminder>\nThe date has changed. Today's date is now X.\n</system-reminder>")]
    assert context_sequence(base) == context_sequence(with_reminder)


def test_anchor_is_bounded_by_leading_messages():
    # A long history must not change the leading anchor fingerprint.
    first = [Message("user", f"m{i}") for i in range(30)]
    second = [Message("user", f"m{i}") for i in range(60)]
    assert context_sequence(first) == context_sequence(second)


def test_late_user_change_does_not_break_anchor():
    # The anchor is capped at the first CONTEXT_ANCHOR_MAX messages, so a
    # change deep in the history no longer invalidates the session fingerprint.
    from danyapi.tools.prompt import CONTEXT_ANCHOR_MAX

    early = [Message("user", f"q{i}") for i in range(CONTEXT_ANCHOR_MAX)]
    late = [*early, Message("user", "rewritten-1")]
    other_late = [*early, Message("user", "rewritten-2")]
    assert context_sequence(late) == context_sequence(other_late)


def test_user_keeps_continuation_prefix():
    one = context_sequence([Message("user", "hi")], user="alice")
    two = context_sequence([Message("user", "hi"), Message("assistant", "yo"), Message("user", "next")], user="alice")
    assert one[0] == two[0]
    assert context_sequence([Message("user", "hi")], user="alice") != context_sequence([Message("user", "hi")], user="bob")


def test_session_reuses_explicit():
    reg = SessionRegistry(FakeSessionClient())
    s1, k1 = asyncio.run(reg.obtain(None))
    s2, k2 = asyncio.run(reg.obtain(k1))
    assert s1 is s2
    assert k2 == k1


def test_client_key_binds_first_chat_to_itself():
    """A client key (e.g. Kimi's prompt_cache_key) given on the very first turn
    must be remembered: the next turn with the same key continues the chat
    instead of creating a new one."""
    client = FakeSessionClient()
    reg = SessionRegistry(client)
    s1, k1 = asyncio.run(reg.obtain("session_abc"))
    assert k1 == "session_abc"
    s2, k2 = asyncio.run(reg.obtain("session_abc"))
    assert s2 is s1
    assert k2 == "session_abc"
    assert client.counter == 1


def test_session_evicts_oldest():
    reg = SessionRegistry(FakeSessionClient(), maxsize=2)
    asyncio.run(reg.obtain(None))
    asyncio.run(reg.obtain(None))
    asyncio.run(reg.obtain(None))
    assert reg.get("s1") is None
    assert reg.get("s2") is not None
    assert reg.get("s3") is not None


def test_session_touch_keeps_fresh():
    reg = SessionRegistry(FakeSessionClient(), maxsize=2)
    asyncio.run(reg.obtain(None))
    asyncio.run(reg.obtain(None))
    reg.get("s1")
    asyncio.run(reg.obtain(None))
    assert reg.get("s2") is None
    assert reg.get("s1") is not None


def test_session_touch_last_message():
    reg = SessionRegistry(FakeSessionClient())
    _, k = asyncio.run(reg.obtain(None))
    reg.touch_last_message(k, "m1")
    assert reg.get(k).last_message_id == "m1"


def test_session_forget_removes():
    reg = SessionRegistry(FakeSessionClient())
    _, k = asyncio.run(reg.obtain(None))
    assert reg.get(k) is not None
    reg.forget(k)
    assert reg.get(k) is None
    # A forgotten key is no longer bound to anything, so a new chat is created
    # and stored under that same key again (this is what makes a client key
    # survive an eviction).
    _, k2 = asyncio.run(reg.obtain(k))
    assert k2 == k
    assert reg.get(k) is not None


def test_session_ttl_expires():
    reg = SessionRegistry(FakeSessionClient(), maxsize=16, ttl=0.1)
    s, k = asyncio.run(reg.obtain(None))
    assert reg.get(k) is not None
    reg._sessions[k] = (s, time.monotonic() - 10)
    assert reg.get(k) is None


def test_qwen_session_reuses_and_evicts():
    reg = QwenSessionRegistry(FakeSessionClient(), maxsize=2)
    _, k1 = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    _, k2 = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    _, _k3 = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    assert reg.get(k1) is None
    assert reg.get(k2) is not None
    s, k = asyncio.run(reg.obtain(k2, "qwen3.8-max"))
    assert k == k2
    assert s.id == k2


def test_qwen_session_touch_last_message():
    reg = QwenSessionRegistry(FakeSessionClient())
    s, k = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    reg.touch_last_message(k, "r1")
    assert s.last_response_id == "r1"


def test_qwen_model_switch_creates_new_chat():
    reg = QwenSessionRegistry(FakeSessionClient())
    _, k1 = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    _, k2 = asyncio.run(reg.obtain(k1, "qwen3.7-plus"))
    assert k2 != k1
    s, k3 = asyncio.run(reg.obtain(k1, "qwen3.8-max"))
    assert k3 == k1
    assert s.model == "qwen3.8-max"


@pytest.fixture
def sessions_store(tmp_path, monkeypatch):
    import danyapi.store as store_mod

    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    return JsonStore("sessions", "default")


def test_obtain_persists(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), store=sessions_store)
    _, key = asyncio.run(reg.obtain(None))
    assert key in sessions_store
    reg2 = SessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"))
    assert reg2.get(key) is not None


def test_restore_respects_prefix(sessions_store):
    sessions_store.set("9:s9", {"id": "cs9"})
    sessions_store.set("0:s0", {"id": "cs0"})
    reg = SessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"), key_prefix="0:")
    assert reg.get("s9") is None
    assert reg.get("s0") is not None


def test_restore_skips_invalid_records(sessions_store):
    sessions_store.set("0:ok", {"id": "cs1"})
    sessions_store.set("0:bad", "garbage")
    sessions_store.set("0:no-id", {})
    sessions_store.set("0:", {"id": "x"})
    reg = SessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"), key_prefix="0:")
    assert reg.get("ok") is not None
    assert reg.get("bad") is None
    assert reg.get("no-id") is None
    assert len(reg._sessions) == 1


def test_restore_trims_to_maxsize(sessions_store):
    for i in range(5):
        sessions_store.set(f"0:s{i}", {"id": f"cs{i}"})
    reg = SessionRegistry(FakeSessionClient(), maxsize=2, store=JsonStore("sessions", "default"), key_prefix="0:")
    assert len(reg._sessions) == 2
    assert "0:s0" not in JsonStore("sessions", "default")


def test_expiry_discards_from_store(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), maxsize=16, ttl=0.1, store=sessions_store)
    _, key = asyncio.run(reg.obtain(None))
    assert key in sessions_store
    reg._sessions[key] = (reg._sessions[key][0], time.monotonic() - 10)
    assert reg.get(key) is None
    assert key not in sessions_store


def test_eviction_discards_from_store(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), maxsize=1, store=sessions_store)
    _, k1 = asyncio.run(reg.obtain(None))
    _, k2 = asyncio.run(reg.obtain(None))
    assert k1 not in sessions_store
    assert k2 in sessions_store


def test_touch_last_message_persists(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), store=sessions_store)
    _, key = asyncio.run(reg.obtain(None))
    reg.touch_last_message(key, "m9")
    reg2 = SessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"))
    session = reg2.get(key)
    assert session is not None
    assert session.last_message_id == "m9"


def test_forget_removes_from_store(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), store=sessions_store)
    _, key = asyncio.run(reg.obtain(None))
    reg.forget(key)
    assert key not in sessions_store


def test_close_all(sessions_store):
    reg = SessionRegistry(FakeSessionClient(), store=sessions_store, key_prefix="0:")
    _, k0 = asyncio.run(reg.obtain(None))
    other = SessionRegistry(FakeSessionClient(), store=sessions_store, key_prefix="1:")
    _, k1 = asyncio.run(other.obtain(None))
    reg.close_all()
    assert len(reg._sessions) == 0
    assert "0:" + k0 not in sessions_store
    assert "1:" + k1 in sessions_store


def test_get_missing_and_empty():
    reg = SessionRegistry(FakeSessionClient())
    assert reg.get(None) is None
    assert reg.get("") is None


def test_qwen_persist_roundtrip(sessions_store):
    reg = QwenSessionRegistry(FakeSessionClient(), store=sessions_store)
    _, key = asyncio.run(reg.obtain(None, "qwen3.8-max"))
    reg.touch_last_message(key, "r1")
    reg2 = QwenSessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"))
    session = reg2.get(key)
    assert session is not None
    assert session.model == "qwen3.8-max"
    assert session.last_response_id == "r1"


def test_qwen_broken_records_skipped(sessions_store):
    sessions_store.set("0:bad", {"id": ""})
    sessions_store.set("0:ok", {"id": "cs1", "model": "m"})
    reg = QwenSessionRegistry(FakeSessionClient(), store=JsonStore("sessions", "default"), key_prefix="0:")
    assert reg.get("ok") is not None
    assert reg.get("bad") is None


class FakeQwenChatClient(FakeSessionClient):
    def __init__(self, detail=None, error=None):
        super().__init__()
        self._detail = detail
        self._error = error
        self.fetch_calls = 0

    async def fetch_chat(self, chat_id):
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return self._detail


def _t2t_detail(current_id="r9", chat_type="t2t", title="T"):
    return {
        "title": title,
        "chat_type": chat_type,
        "chat": {
            "history": {"currentId": current_id},
            "messages": [],
        },
    }


def test_qwen_attach_recovers_from_history():
    client = FakeQwenChatClient(_t2t_detail(current_id="r9"))
    reg = QwenSessionRegistry(client)
    session = asyncio.run(reg.attach("chat-1", model="qwen3.8-max", chat_type="t2t"))
    assert session is not None
    assert session.id == "chat-1"
    assert session.last_response_id == "r9"
    assert session.chat_type == "t2t"
    assert session.reattached is True
    assert reg.get("chat-1") is session


def test_qwen_attach_falls_back_to_last_assistant():
    detail = {
        "title": "T",
        "chat_type": "t2t",
        "chat": {
            "history": {},
            "messages": [
                {"role": "user", "id": "u1"},
                {"role": "assistant", "id": "a1"},
                {"role": "assistant", "id": "a2"},
            ],
        },
    }
    reg = QwenSessionRegistry(FakeQwenChatClient(detail))
    session = asyncio.run(reg.attach("chat-2", model="m", chat_type="t2t"))
    assert session is not None
    assert session.last_response_id == "a2"


def test_qwen_attach_rejects_chat_type_mismatch():
    reg = QwenSessionRegistry(FakeQwenChatClient(_t2t_detail(chat_type="t2t")))
    assert asyncio.run(reg.attach("chat-3", model="m", chat_type="t2i")) is None
    assert reg.get("chat-3") is None


def test_qwen_attach_returns_none_on_error():
    from danyapi.qwen.client import QwenError

    reg = QwenSessionRegistry(FakeQwenChatClient(error=QwenError("not found", "x")))
    assert asyncio.run(reg.attach("chat-4", model="m", chat_type="t2t")) is None


def test_qwen_attach_returns_none_without_any_tail():
    detail = {"title": "T", "chat_type": "t2t", "chat": {"history": {}, "messages": []}}
    reg = QwenSessionRegistry(FakeQwenChatClient(detail))
    assert asyncio.run(reg.attach("chat-5", model="m", chat_type="t2t")) is None


def test_qwen_attach_returns_none_on_non_dict():
    reg = QwenSessionRegistry(FakeQwenChatClient(detail=None))
    assert asyncio.run(reg.attach("chat-6", model="m", chat_type="t2t")) is None


def test_qwen_reattached_flag_persists(sessions_store):
    reg = QwenSessionRegistry(FakeQwenChatClient(), store=sessions_store)
    reg._store_session(
        "chat-p",
        QwenSession(id="chat-p", model="m", chat_type="t2i", last_response_id="r1", reattached=True),
    )
    reg2 = QwenSessionRegistry(FakeQwenChatClient(), store=JsonStore("sessions", "default"))
    restored = reg2.get("chat-p")
    assert restored is not None
    assert restored.reattached is True
    assert restored.chat_type == "t2i"


def test_qwen_reuse_rejects_chat_type_change():
    reg = QwenSessionRegistry(FakeQwenChatClient())
    _, key = asyncio.run(reg.obtain(None, "qwen3.8-max", chat_type="t2t"))
    # Same model but requesting an image chat must not reuse the text chat.
    _, key2 = asyncio.run(reg.obtain(key, "qwen3.8-max", chat_type="t2i"))
    assert key2 != key


def test_account_pool_resolve_routes_to_owner():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    pool.register(1, "sess-a")
    pool.index_context("sess-a", ("u1",))
    cached = pool.resolve_context(("u1",))
    assert cached == "sess-a"


def test_account_pool_forget_removes_mapping():
    pool = AccountPool([make_acct(0)])
    pool.register(0, "s1")
    assert pool.account_for_session("s1").index == 0
    pool.forget("s1")
    assert pool.account_for_session("s1") is None


def test_account_pool_stats_reports_cache_state():
    pool = AccountPool([make_acct(0), make_acct(1)])
    pool.register(0, "s1")
    pool.index_context("s1", ("a",))
    stats = pool.stats()
    assert stats["accounts"] == 2
    assert stats["healthy"] == 2
    assert stats["session_affinities"] == 1
    assert stats["context_entries"] == 1
    pool.resolve_context(("a",))
    assert pool.stats()["context_hits"] == 1


def test_plain_continuation_sends_only_last_user():
    messages = [
        Message("user", "What is the weather?"),
        Message("assistant", "It is 22C."),
        Message("user", "And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, has_session=True)
    assert not tool_mode
    assert prompt == "And in Rome?"


def test_continuation_after_tool_round_with_new_user():
    messages = [
        Message("user", "What is the weather?"),
        Message("assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}}]),
        Message("tool", "22C, sunny", tool_call_id="c1"),
        Message("assistant", "It is 22C in Moscow."),
        Message("user", "And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, has_session=True)
    assert not tool_mode
    assert prompt == "And in Rome?"
    assert "22C, sunny" not in prompt


def test_immediate_tool_round_uses_tail():
    messages = [
        Message("user", "What is the weather?"),
        Message("assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}}]),
        Message("tool", "22C, sunny", tool_call_id="c1"),
    ]
    prompt, tool_mode = build_prompt(messages, has_session=True)
    assert tool_mode
    assert "22C, sunny" in prompt
    assert "Continue the conversation" in prompt
    assert "What is the weather?" not in prompt


def test_session_continuation_with_tools():
    messages = [
        Message("user", "What is the weather?"),
        Message("assistant", "It is 22C."),
        Message("user", "And in Rome?"),
    ]
    prompt, tool_mode = build_prompt(messages, [WEATHER_TOOL], has_session=True)
    assert tool_mode
    assert "And in Rome?" in prompt
    assert "get_weather" not in prompt


def test_session_skips_system():
    messages = [Message("system", "Be concise."), Message("user", "hi")]
    prompt, _ = build_prompt(messages, has_session=True)
    assert prompt == "hi"


def test_deepseek_stateless_request_resolves_cached_session():
    captured = {}
    orig = openai_mod._collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    openai_mod._collect_non_stream = fake_collect
    try:
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(MagicMock(), "sess-a"))
        pool.resolve_context = MagicMock(return_value="sess-a")
        openai_mod.app.state.pool = pool
        req = SimpleNamespace(
            model="deepseek-v4-flash",
            stream=False,
            thinking=False,
            search=False,
            session_id=None,
            files=None,
            tools=None,
            tool_choice=None,
            response_format=None,
            messages=[openai_mod.ChatMessage(role="user", content="hello")],
        )
        asyncio.run(openai_mod._chat_completions_deepseek(req))
        pool.resolve_context.assert_called_once()
        assert captured["existing_sid"] == "sess-a"
    finally:
        openai_mod._collect_non_stream = orig


def test_deepseek_explicit_session_bypasses_context_resolution():
    captured = {}
    orig = openai_mod._collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    openai_mod._collect_non_stream = fake_collect
    try:
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(MagicMock(), "explicit-1"))
        pool.resolve_context = MagicMock()
        openai_mod.app.state.pool = pool
        req = SimpleNamespace(
            model="deepseek-v4-flash",
            stream=False,
            thinking=False,
            search=False,
            session_id="explicit-1",
            files=None,
            tools=None,
            tool_choice=None,
            response_format=None,
            messages=[openai_mod.ChatMessage(role="user", content="hello")],
        )
        asyncio.run(openai_mod._chat_completions_deepseek(req))
        pool.acquire.assert_called_once()
        pool.resolve_context.assert_not_called()
        assert captured["existing_sid"] == "explicit-1"
    finally:
        openai_mod._collect_non_stream = orig


def test_qwen_stateless_request_resolves_cached_session():
    captured = {}
    orig = qwen_api.collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    qwen_api.collect_non_stream = fake_collect
    try:
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(MagicMock(), "sess-q"))
        pool.resolve_context = MagicMock(return_value="sess-q")
        openai_mod.app.state.qwen_pool = pool
        req = SimpleNamespace(
            model="qwen3.8-max",
            stream=False,
            thinking=False,
            search=False,
            session_id=None,
            files=None,
            tools=None,
            tool_choice=None,
            response_format=None,
            messages=[openai_mod.ChatMessage(role="user", content="hello")],
        )
        asyncio.run(openai_mod._chat_completions_qwen(req))
        pool.resolve_context.assert_called_once()
        assert captured["existing_sid"] == "sess-q"
    finally:
        qwen_api.collect_non_stream = orig


def test_deepseek_non_stream_accepts_include_usage():
    assert "include_usage" in openai_mod._collect_non_stream.__code__.co_varnames


def test_qwen_non_stream_accepts_include_usage():
    assert "include_usage" in qwen_api.collect_non_stream.__code__.co_varnames


class FakeHistoryClient(FakeSessionClient):
    def __init__(self, messages=None, error=None):
        super().__init__()
        self._messages = messages or []
        self._error = error
        self.history_calls = 0

    async def history_messages(self, session_id):
        self.history_calls += 1
        if self._error is not None:
            raise self._error
        return self._messages


def _ds_registry(client, store=None, key_prefix=""):
    from danyapi.accounts import DeepSeekSessionRegistry

    return DeepSeekSessionRegistry(client, store=store, key_prefix=key_prefix)


def test_attach_base_returns_none():
    reg = SessionRegistry(FakeSessionClient())
    assert asyncio.run(reg.attach("missing")) is None
    assert reg.get("missing") is None


def test_attach_returns_existing_without_recovering():
    client = FakeHistoryClient([{"message_id": 9}])
    reg = _ds_registry(client)
    _, key = asyncio.run(reg.obtain(None))
    got = asyncio.run(reg.attach(key))
    assert got is not None
    assert client.history_calls == 0


def test_attach_recovers_session_from_history():
    client = FakeHistoryClient(
        [
            {"message_id": 1, "accumulated_token_usage": 10},
            {"message_id": 2, "accumulated_token_usage": 30},
        ]
    )
    reg = _ds_registry(client)
    session = asyncio.run(reg.attach("chat-1"))
    assert session is not None
    assert session.id == "chat-1"
    assert session.last_message_id == 2
    assert session.accumulated_tokens == 30
    assert session.accounted_tokens == 30
    assert session.reattached is True
    # Persisted under the recovered id, so the next lookup is a plain hit.
    assert reg.get("chat-1") is session


def test_attach_picks_max_message_id_regardless_of_order():
    client = FakeHistoryClient(
        [
            {"message_id": 5, "accumulated_token_usage": 50},
            {"message_id": 2, "accumulated_token_usage": 20},
        ]
    )
    reg = _ds_registry(client)
    session = asyncio.run(reg.attach("chat-2"))
    assert session is not None
    assert session.last_message_id == 5
    assert session.accumulated_tokens == 50


def test_attach_returns_none_on_error():
    from danyapi.deepseek.client import DeepSeekError

    client = FakeHistoryClient(error=DeepSeekError(1, "invalid chat session id"))
    reg = _ds_registry(client)
    assert asyncio.run(reg.attach("nope")) is None
    assert reg.get("nope") is None


def test_attach_returns_none_on_empty_history():
    reg = _ds_registry(FakeHistoryClient([]))
    assert asyncio.run(reg.attach("empty")) is None


def test_attach_ignores_records_without_message_id():
    client = FakeHistoryClient([{"role": "USER"}, "garbage", {"message_id": 7}])
    reg = _ds_registry(client)
    session = asyncio.run(reg.attach("chat-3"))
    assert session is not None
    assert session.last_message_id == 7


def test_attach_empty_session_id():
    reg = _ds_registry(FakeHistoryClient([{"message_id": 1}]))
    assert asyncio.run(reg.attach(None)) is None
    assert asyncio.run(reg.attach("")) is None


def test_reattached_flag_persists(sessions_store):
    from danyapi.deepseek.client import DeepSeekSession

    reg = _ds_registry(FakeHistoryClient(), store=sessions_store)
    reg._store_session(
        "chat-p",
        DeepSeekSession(id="chat-p", last_message_id=3, reattached=True),
    )
    reg2 = _ds_registry(FakeHistoryClient(), store=JsonStore("sessions", "default"))
    restored = reg2.get("chat-p")
    assert restored is not None
    assert restored.reattached is True


def test_attach_recovers_then_continues_via_obtain():
    client = FakeHistoryClient([{"message_id": 4}])
    reg = _ds_registry(client)
    assert asyncio.run(reg.attach("chat-r")) is not None
    session, key = asyncio.run(reg.obtain("chat-r"))
    assert key == "chat-r"
    assert session.id == "chat-r"
    assert client.counter == 0  # no new chat created


def test_openai_reattaches_before_build_prompt():
    captured = {}
    orig = openai_mod._collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    openai_mod._collect_non_stream = fake_collect
    try:
        account = MagicMock()
        account.index = 0
        account.sem = MagicMock()
        recovered = SimpleNamespace(id="chat-x", last_message_id="7", reattached=True)
        # First get() is the reattach check (miss), second is has_session (hit).
        account.sessions.get = MagicMock(side_effect=[None, recovered])

        async def fake_attach(sid, **kwargs):
            return recovered

        account.sessions.attach = AsyncMock(side_effect=fake_attach)
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(account, "chat-x"))
        openai_mod.app.state.pool = pool
        req = SimpleNamespace(
            model="deepseek-v4-flash",
            stream=False,
            thinking=False,
            search=False,
            session_id="chat-x",
            files=None,
            tools=None,
            tool_choice=None,
            response_format=None,
            messages=[
                openai_mod.ChatMessage(role="user", content="first"),
                openai_mod.ChatMessage(role="assistant", content="reply"),
                openai_mod.ChatMessage(role="user", content="second"),
            ],
        )
        asyncio.run(openai_mod._chat_completions_deepseek(req))
        account.sessions.attach.assert_awaited_once()
        pool.register.assert_called_once_with(0, "chat-x")
        # A successful reattach sends only the new turn, not the whole history.
        assert captured["prompt"] == "second"
        assert captured["existing_sid"] == "chat-x"
    finally:
        openai_mod._collect_non_stream = orig


def _sid_req(**kwargs):
    base = {
        "model": "deepseek-v4-flash",
        "stream": False,
        "thinking": False,
        "search": False,
        "session_id": None,
        "prompt_cache_key": None,
        "files": None,
        "tools": None,
        "tool_choice": None,
        "response_format": None,
        "messages": [openai_mod.ChatMessage(role="user", content="hello")],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_client_session_id_prefers_session_id():
    req = _sid_req(session_id="chat-1", prompt_cache_key="session_abc")
    assert openai_mod._client_session_id(req) == "chat-1"


def test_client_session_id_falls_back_to_prompt_cache_key():
    # Kimi Code CLI sends only prompt_cache_key; without this fallback two
    # sessions opening with the same first message share one upstream chat.
    req = _sid_req(prompt_cache_key="session_77d8e7f8")
    assert openai_mod._client_session_id(req) == "session_77d8e7f8"


def test_client_session_id_none_when_both_missing_or_blank():
    assert openai_mod._client_session_id(_sid_req()) is None
    assert openai_mod._client_session_id(_sid_req(session_id="  ", prompt_cache_key=" ")) is None


def test_openai_uses_prompt_cache_key_as_session(monkeypatch):
    captured = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(openai_mod, "_collect_non_stream", fake_collect)
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=(MagicMock(index=0, sem=MagicMock()), "session_abc"))
    monkeypatch.setattr(openai_mod.app.state, "pool", pool)
    asyncio.run(openai_mod._chat_completions_deepseek(_sid_req(prompt_cache_key="session_abc")))
    pool.acquire.assert_awaited_once_with("session_abc", openai_mod.settings.acquire_timeout)


def test_two_kimi_sessions_with_same_opening_do_not_collide(monkeypatch):
    """Same first message, different prompt_cache_key → different sessions."""
    seen = []

    async def fake_collect(**kwargs):
        return {"ok": True}

    monkeypatch.setattr(openai_mod, "_collect_non_stream", fake_collect)
    pool = MagicMock()
    pool.acquire = AsyncMock(side_effect=lambda sid, _t: seen.append(sid) or (MagicMock(index=0, sem=MagicMock()), sid))
    monkeypatch.setattr(openai_mod.app.state, "pool", pool)

    asyncio.run(openai_mod._chat_completions_deepseek(_sid_req(prompt_cache_key="session_aaa")))
    asyncio.run(openai_mod._chat_completions_deepseek(_sid_req(prompt_cache_key="session_bbb")))

    assert seen == ["session_aaa", "session_bbb"]
    pool.resolve_context.assert_not_called()


def test_openai_falls_back_when_reattach_fails():
    captured = {}
    orig = openai_mod._collect_non_stream

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    openai_mod._collect_non_stream = fake_collect
    try:
        account = MagicMock()
        account.index = 0
        account.sem = MagicMock()
        account.sessions.get = MagicMock(return_value=None)
        account.sessions.attach = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = AsyncMock(return_value=(account, "chat-y"))
        openai_mod.app.state.pool = pool
        req = SimpleNamespace(
            model="deepseek-v4-flash",
            stream=False,
            thinking=False,
            search=False,
            session_id="chat-y",
            files=None,
            tools=None,
            tool_choice=None,
            response_format=None,
            messages=[openai_mod.ChatMessage(role="user", content="hello")],
        )
        asyncio.run(openai_mod._chat_completions_deepseek(req))
        account.sessions.attach.assert_awaited_once()
        pool.register.assert_not_called()
        assert captured["existing_sid"] == "chat-y"
    finally:
        openai_mod._collect_non_stream = orig
