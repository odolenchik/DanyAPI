"""Per-turn thinking heuristic (``_looks_hard`` / ``_resolve_thinking``).

The heuristic is opt-in via ``DANYAPI_THINKING_HEURISTIC`` and only consulted
when the client sent neither ``thinking`` nor ``reasoning_effort``. Signals add
up to a score: a strong one (long prompt, error/difficulty cue) clears the
threshold alone, weak ones (long history, automatic tool round) must combine.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import danyapi.api.openai as openai_mod
from danyapi.api.models import ChatCompletionRequest
from danyapi.api.openai import _looks_hard, _resolve_thinking, settings


@pytest.fixture
def heuristic_on(monkeypatch):
    monkeypatch.setattr(settings, "thinking_heuristic", True)
    monkeypatch.setattr(settings, "thinking_min_prompt_chars", 2000)
    monkeypatch.setattr(settings, "thinking_min_messages", 8)


def _req(text, *, role="user", extra_messages=(), **kwargs):
    messages = [{"role": role, "content": text}]
    messages.extend(extra_messages)
    return ChatCompletionRequest(model="deepseek-v4-flash", messages=messages, **kwargs)


# ---------------------------------------------------------------------------
# explicit client fields always win
# ---------------------------------------------------------------------------


def test_explicit_thinking_true_beats_a_plain_short_turn(heuristic_on):
    assert _resolve_thinking(_req("hi", thinking=True), default=False) is True


def test_explicit_thinking_false_beats_a_hard_turn(heuristic_on):
    stack = "Traceback (most recent call last):\n  File x.py"
    assert _resolve_thinking(_req(stack, thinking=False), default=True) is False


def test_reasoning_effort_off_beats_a_hard_turn(heuristic_on):
    stack = "Traceback (most recent call last):\n  File x.py"
    assert _resolve_thinking(_req(stack, reasoning_effort="off"), default=True) is False


def test_reasoning_effort_high_beats_a_plain_short_turn(heuristic_on):
    assert _resolve_thinking(_req("hi", reasoning_effort="high"), default=False) is True


# ---------------------------------------------------------------------------
# heuristic off: provider default wins (regression guard)
# ---------------------------------------------------------------------------


def test_flag_off_keeps_provider_default_for_hard_turn(monkeypatch):
    monkeypatch.setattr(settings, "thinking_heuristic", False)
    stack = "Traceback (most recent call last):\n  File x.py"
    assert _resolve_thinking(_req(stack), default=False) is False
    assert _resolve_thinking(_req(stack), default=True) is True


TOOLS = [{"type": "function", "function": {"name": "f"}}]


def _history(n):
    return [{"role": "assistant", "content": "ok"}] * n


# ---------------------------------------------------------------------------
# strong signals clear the threshold on their own
# ---------------------------------------------------------------------------


def test_long_prompt_is_hard(heuristic_on):
    assert _looks_hard(_req("x" * 2000)) is True


def test_error_text_is_hard(heuristic_on):
    assert _looks_hard(_req("I got this:\nTraceback (most recent call last):")) is True


def test_difficulty_keyword_is_hard(heuristic_on):
    assert _looks_hard(_req("why does this fail")) is True


def test_russian_difficulty_keyword_is_hard(heuristic_on):
    assert _looks_hard(_req("почему это падает")) is True


# ---------------------------------------------------------------------------
# weak signals stay below the threshold on their own
# ---------------------------------------------------------------------------


def test_bare_tools_are_not_hard(heuristic_on):
    # A tools list is a static property of the request, not evidence about
    # the turn: on its own it must not turn thinking on.
    assert _looks_hard(_req("hi", tools=TOOLS)) is False


def test_auto_tool_choice_is_not_hard_alone(heuristic_on):
    assert _looks_hard(_req("hi", tools=TOOLS, tool_choice="auto")) is False


def test_long_history_is_not_hard_alone(heuristic_on):
    assert _looks_hard(_req("hi", extra_messages=_history(7))) is False


# ---------------------------------------------------------------------------
# tool_choice modes: only "auto" hints at a decision
# ---------------------------------------------------------------------------


def test_required_tool_choice_is_not_hard(heuristic_on):
    # A forced call is not a decision, so it is not evidence of difficulty.
    assert _looks_hard(_req("hi", tools=TOOLS, tool_choice="required")) is False


def test_named_tool_choice_is_not_hard(heuristic_on):
    named = {"type": "function", "function": {"name": "f"}}
    assert _looks_hard(_req("hi", tools=TOOLS, tool_choice=named)) is False


def test_none_tool_choice_is_not_hard(heuristic_on):
    # "none" forbids calls outright, so it cannot make the turn harder.
    assert _looks_hard(_req("hi", tools=TOOLS, tool_choice="none")) is False


def test_tool_choice_without_tools_is_not_hard(heuristic_on):
    assert _looks_hard(_req("hi", tool_choice="auto")) is False


# ---------------------------------------------------------------------------
# weak signals combine into a hard turn
# ---------------------------------------------------------------------------


def test_tools_plus_long_history_is_hard(heuristic_on):
    assert _looks_hard(_req("hi", extra_messages=_history(7), tools=TOOLS)) is True


def test_tools_plus_difficulty_is_hard(heuristic_on):
    assert _looks_hard(_req("why does this fail", tools=TOOLS)) is True


def test_history_plus_error_is_hard(heuristic_on):
    stack = "I got this:\nTraceback (most recent call last):"
    assert _looks_hard(_req(stack, extra_messages=_history(7))) is True


def test_required_choice_does_not_help_history(heuristic_on):
    # required contributes nothing, so history stays a lone weak signal.
    assert _looks_hard(_req("hi", extra_messages=_history(7), tools=TOOLS, tool_choice="required")) is False


# ---------------------------------------------------------------------------
# "don't think" signals
# ---------------------------------------------------------------------------


def test_short_plain_prompt_stays_off(heuristic_on):
    assert _looks_hard(_req("hi")) is False


def test_short_question_stays_off(heuristic_on):
    assert _looks_hard(_req("what time is it?")) is False


def test_thresholds_are_just_below(heuristic_on):
    assert _looks_hard(_req("x" * 1999)) is False
    history = [{"role": "assistant", "content": "ok"}] * 6
    assert _looks_hard(_req("hi", extra_messages=history)) is False


# ---------------------------------------------------------------------------
# only the last user turn is inspected
# ---------------------------------------------------------------------------


def test_only_last_user_turn_is_inspected(heuristic_on):
    req = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": "Traceback (most recent call last):"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "hi"},
        ],
    )
    assert _looks_hard(req) is False


def test_multimodal_last_turn_is_flattened(heuristic_on):
    parts = [{"type": "text", "text": "why does this break"}]
    req = _req(parts)
    assert _looks_hard(req) is True


def test_non_text_content_contributes_nothing(heuristic_on):
    req = ChatCompletionRequest(model="deepseek-v4-flash", messages=[{"role": "user", "content": None}])
    assert _looks_hard(req) is False


def test_request_without_a_user_turn_is_not_hard(heuristic_on):
    req = ChatCompletionRequest(
        model="deepseek-v4-flash",
        messages=[{"role": "system", "content": "Traceback (most recent call last):"}],
    )
    assert _looks_hard(req) is False


# ---------------------------------------------------------------------------
# heuristic and the search gate are independent
# ---------------------------------------------------------------------------


def test_heuristic_does_not_flip_search(heuristic_on):
    # A hard turn that also asks for search: thinking is decided by the
    # heuristic, search by the request flag alone.
    req = _req("why does this fail", search=True, tools=[{"type": "function", "function": {"name": "f"}}])
    assert _looks_hard(req) is True
    assert bool(req.search) is True


def test_search_off_on_a_hard_turn(heuristic_on):
    req = _req("why does this fail")
    assert _looks_hard(req) is True
    assert bool(req.search) is False


# ---------------------------------------------------------------------------
# end-to-end through the DeepSeek route
# ---------------------------------------------------------------------------


class _E2eAccount:
    index = 0
    broken = False

    def __init__(self):
        self.client = MagicMock()
        self.pow = MagicMock()
        self.pow.make_header = AsyncMock(return_value={})
        self.pow_upload = MagicMock()
        self.pow_upload.make_header = AsyncMock(return_value={})
        self.sem = asyncio.Semaphore(1)
        self.sessions = MagicMock()

    async def wait_turn(self):
        pass

    def note_request_start(self):
        pass

    def note_request_end(self):
        pass

    def recent_request_count(self, window):
        return 0


async def _run_deepseek_route(monkeypatch, content, *, extra=None):
    captured = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(openai_mod, "_collect_non_stream", fake_collect)
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=(_E2eAccount(), None))
    monkeypatch.setattr(openai_mod.app.state, "pool", pool, raising=False)

    kwargs = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": content}], "stream": False}
    kwargs.update(extra or {})
    await openai_mod._chat_completions_deepseek(ChatCompletionRequest(**kwargs))
    return captured


async def test_route_plain_turn_sends_thinking_off(heuristic_on, monkeypatch):
    captured = await _run_deepseek_route(monkeypatch, "hi")
    assert captured["thinking"] is False


async def test_route_hard_turn_sends_thinking_on(heuristic_on, monkeypatch):
    captured = await _run_deepseek_route(monkeypatch, "why does this fail\nTraceback (most recent call last):")
    assert captured["thinking"] is True


async def test_route_flag_off_uses_provider_default(monkeypatch):
    monkeypatch.setattr(settings, "thinking_heuristic", False)
    captured = await _run_deepseek_route(monkeypatch, "why does this fail\nTraceback (most recent call last):")
    # deepseek-v4-flash is model_type "default", so the provider default is off.
    assert captured["thinking"] is False


async def test_route_explicit_thinking_still_wins(heuristic_on, monkeypatch):
    captured = await _run_deepseek_route(monkeypatch, "hi", extra={"thinking": True})
    assert captured["thinking"] is True
