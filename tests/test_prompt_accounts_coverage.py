"""Coverage tests for danyapi/tools/prompt.py and danyapi/accounts.py.

Focused on branches the existing suites do not reach: the store-driven
trim in ContextIndex._restore, the AccountPool._wait_free candidates
selection, and the assistant / truncation branches of the tool-tail and
compact-prompt renderers.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from danyapi.accounts import AccountPool, AccountPoolBusy, ContextIndex, DeepSeekAccount
from danyapi.deepseek.client import DeepSeekClient
from danyapi.store import JsonStore
from danyapi.tools.prompt import (
    _render_tool_tail,
    build_compact_prompt,
    build_prompt,
)


def make_acct(i):
    client = MagicMock(spec=DeepSeekClient)
    client.index = i
    return DeepSeekAccount(i, client)


@pytest.fixture
def ctx_store(tmp_path, monkeypatch):
    import danyapi.store as store_mod

    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    return JsonStore("ctx-trim", "default")


def test_restore_trims_to_maxsize(ctx_store):
    """_restore must drop the least recently touched entries down to maxsize."""
    ctx_store.set("s1", ["a"])
    ctx_store.set("s2", ["b"])
    ctx_store.set("s3", ["c"])
    idx = ContextIndex(2, store=ctx_store)
    assert len(idx._seqs) == 2
    # _restore touches every entry with the same timestamp, so the eviction
    # order falls back to dict insertion order: s1 goes first.
    assert idx.lookup(("a",)) is None
    assert idx.lookup(("b",)) == "s2"
    assert idx.lookup(("c",)) == "s3"


async def test_acquire_locked_affinity_times_out():
    """A registered session whose account is locked waits via _wait_free."""
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    pool.register(1, "sess-locked")
    await a1.sem.acquire()
    try:
        with pytest.raises(AccountPoolBusy):
            await pool.acquire("sess-locked", max_wait=0.05)
    finally:
        a1.sem.release()


async def test_wait_free_preferred_with_session():
    """_wait_free returns the preferred account together with its session."""
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    acct, sid = await pool._wait_free(a1, 1.0, "sess-9")
    assert acct is a1
    assert sid == "sess-9"


async def test_wait_free_round_robin_without_session():
    """Without a session, _wait_free advances the round-robin cursor."""
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    acct, sid = await pool._wait_free(None, 1.0, None)
    assert acct is a0
    assert sid is None
    assert pool._rr == 1


def test_render_tool_tail_assistant_text_and_inline_tool_call():
    """Assistant prose and content-list tool calls both land in the tail."""
    user = SimpleNamespace(role="user", content="start")
    prose = SimpleNamespace(role="assistant", content="  looking now  ", tool_calls=None)
    inline = SimpleNamespace(
        role="assistant",
        content=[{"type": "tool_call", "name": "Read", "arguments": '{"path": "a.py"}'}],
        tool_calls=None,
    )
    result = SimpleNamespace(role="tool", content="file body", tool_call_id="c1")

    prompt, tools_present = build_prompt([user, prose, inline, result], has_session=True)

    assert tools_present is True
    assert "looking now" in prompt
    assert '"name": "Read"' in prompt
    assert "file body" in prompt


def test_render_tool_tail_truncates_long_results():
    """Oversized tool results are truncated; short ones pass through."""
    long_result = SimpleNamespace(role="tool", content="x" * 300, tool_call_id="c1")
    short_result = SimpleNamespace(role="tool", content="ok", tool_call_id="c2")

    tail = _render_tool_tail([long_result, short_result], max_chars=300)

    assert "...[truncated]" in tail
    # The long body survives, but only its truncated head: the rendered string
    # carries a "Tool result (c1): " prefix, so the cut lands inside the x's.
    assert "x" * 100 in tail
    assert "x" * 300 not in tail
    assert "Tool result (c2): ok" in tail


def test_build_compact_prompt_keeps_current_turn_tool_results():
    """Tool results in the current turn are kept regardless of the cap.

    The cap (``max_tool_results``) limits only the older "Recent tool results"
    section; the current turn's own tool round is always preserved, because
    without it the fresh session cannot continue the request.
    """
    messages = [
        SimpleNamespace(role="user", content="q1"),
        SimpleNamespace(role="assistant", content="a1", tool_calls=None),
        SimpleNamespace(role="user", content="q2"),
        SimpleNamespace(role="tool", content="old", tool_call_id="c1"),
        SimpleNamespace(role="tool", content="new", tool_call_id="c2"),
    ]

    _, body = build_compact_prompt(messages, max_tool_results=1)

    # q2 is the request being answered now, and its tool round must survive.
    assert "q2" in body
    assert "new" in body
    assert "old" in body


def test_build_compact_prompt_caps_older_tool_results():
    """Older tool results (before the current turn) obey the cap."""
    messages = [
        SimpleNamespace(role="user", content="q1"),
        SimpleNamespace(role="tool", content="first-result", tool_call_id="c0"),
        SimpleNamespace(role="tool", content="second-result", tool_call_id="c1"),
        SimpleNamespace(role="assistant", content="a1"),
        SimpleNamespace(role="user", content="q2"),
    ]

    system, body = build_compact_prompt(messages, max_tool_results=1)

    # Only the most recent older tool result fits the cap; the earlier is dropped.
    assert "second-result" in body
    assert "first-result" not in body
    assert "User: q1" in body
    assert system == ""
