"""Coverage for small error/edge branches across several modules.

Each test here targets a branch the existing suites do not reach: numeric
settings that fall back on a bad value, a missing-attribute fallback, the
rarely-hit parts of token estimation and usage persistence, error mapping in
the z.ai response assembly, the trace helper in the Qwen path, the trim guard
in ContextIndex, and the rate-limit hint on the continuation path.
"""

import logging as stdlib_logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import danyapi.api.deepseek as ds_mod
import danyapi.qwen.api as qwen_api
import danyapi.zai.api as zai_api
from danyapi import logging as dlog
from danyapi.accounts import ContextIndex
from danyapi.config import Settings
from danyapi.store import JsonStore
from danyapi.tokens import count_message_tokens
from danyapi.usage import UsageTracker
from danyapi.zai.models import ZaiModelError, resolve_zai_model

# ---------------------------------------------------------------------------
# config.py: numeric settings that fall back when the value cannot be parsed
# ---------------------------------------------------------------------------


def _settings(env):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", env, raising=False)
        return Settings()


@pytest.mark.parametrize(
    ("var", "attr", "fallback"),
    [
        ("DANYAPI_USAGE_MAX_RECORDS", "usage_max_records", 1000),
        ("DANYAPI_TOOLTRACE_MAX_BYTES", "tooltrace_max_bytes", 50 * 1024 * 1024),
        ("DANYAPI_TOOLTRACE_BACKUP_COUNT", "tooltrace_backup_count", 10),
    ],
)
def test_invalid_numeric_setting_falls_back(var, attr, fallback):
    assert getattr(_settings({var: "not-a-number"}), attr) == fallback


# ---------------------------------------------------------------------------
# logging.py: the pre-3.11 fallback for the level-name registry
# ---------------------------------------------------------------------------


def test_known_level_names_falls_back_without_getLevelNamesMapping(monkeypatch):
    monkeypatch.delattr(stdlib_logging, "getLevelNamesMapping", raising=False)
    assert dlog._known_level_names()["INFO"] == stdlib_logging.INFO


# ---------------------------------------------------------------------------
# tokens.py: a bare string inside a content list, and non-dict tool calls
# ---------------------------------------------------------------------------


def test_count_message_tokens_list_with_plain_string_item():
    message = {"content": ["plain text", {"type": "image_url"}]}
    assert count_message_tokens(message) > 3


def test_count_message_tokens_skips_non_dict_tool_call():
    message = {"content": "hi", "tool_calls": ["not-a-dict", {"function": {"name": "Read", "arguments": "{}"}}]}
    assert count_message_tokens(message) > 3


# ---------------------------------------------------------------------------
# usage.py: persistence failures must be swallowed, not raised
# ---------------------------------------------------------------------------


def test_usage_store_write_failure_is_swallowed():
    store = MagicMock()
    store.set = MagicMock(side_effect=RuntimeError("disk full"))
    tracker = UsageTracker(store=store)
    tracker.record("deepseek", "m", 1, 2, 3)  # must not raise
    assert tracker.snapshot()["totals"]["requests"] == 1


def test_usage_store_clear_failure_is_swallowed():
    store = MagicMock()
    store.discard = MagicMock(side_effect=RuntimeError("disk full"))
    tracker = UsageTracker(store=store)
    tracker.reset()  # must not raise
    assert tracker.snapshot()["totals"]["requests"] == 0


# ---------------------------------------------------------------------------
# zai/models.py: an unknown model names the supported ones
# ---------------------------------------------------------------------------


def test_resolve_zai_model_unsupported_lists_alternatives():
    with pytest.raises(ZaiModelError) as excinfo:
        resolve_zai_model("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
    assert "glm-5.3" in str(excinfo.value)


# ---------------------------------------------------------------------------
# zai/api.py: error mapping in collect_non_stream
# ---------------------------------------------------------------------------


def _acc(content="", reasoning="", error=None):
    acc = MagicMock()
    acc.content = content
    acc.reasoning = reasoning
    acc.error = error
    acc.usage = {}
    return acc


@pytest.mark.asyncio
async def test_collect_non_stream_unsupported_model_is_400():
    browser = MagicMock()
    browser.complete = AsyncMock(side_effect=ZaiModelError("nope"))
    with pytest.raises(HTTPException) as excinfo:
        await zai_api.collect_non_stream(browser=browser, prompt="hi", model="bad")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_collect_non_stream_timeout_is_504():
    browser = MagicMock()
    browser.complete = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(HTTPException) as excinfo:
        await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3")
    assert excinfo.value.status_code == 504


@pytest.mark.asyncio
async def test_collect_non_stream_empty_completion_is_502():
    browser = MagicMock()
    browser.complete = AsyncMock(return_value=_acc())
    with pytest.raises(HTTPException) as excinfo:
        await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3")
    assert excinfo.value.status_code == 502
    assert "empty" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_collect_non_stream_tool_mode_falls_back_to_text_with_reasoning():
    browser = MagicMock()
    browser.complete = AsyncMock(return_value=_acc(content="just prose", reasoning="thinking"))
    result = await zai_api.collect_non_stream(browser=browser, prompt="hi", model="glm-5.3", tool_mode=True, tool_schemas={})
    message = result["choices"][0]["message"]
    assert result["choices"][0]["finish_reason"] == "stop"
    assert message["content"] == "just prose"
    assert message["reasoning_content"] == "thinking"


# ---------------------------------------------------------------------------
# qwen/api.py: the trace helper's two uncovered arms
# ---------------------------------------------------------------------------


def test_trace_parse_without_trace_is_noop():
    qwen_api._trace_parse(None, "r1", "content", None)  # must not raise


def test_trace_parse_records_the_issue(monkeypatch):
    captured = {}
    trace = SimpleNamespace(parse=lambda *args, **kwargs: captured.update(kwargs, args=args))
    monkeypatch.setattr(qwen_api, "_classify_trace", lambda content, schemas: ("json_malformed", "bad json"))
    monkeypatch.setattr(qwen_api, "toolemu", SimpleNamespace(parse_tool_calls=lambda content, schemas: None))

    qwen_api._trace_parse(trace, "r1", "garbage", None, session_key="s1", model="m")

    assert captured["result"] == "unparsed:json_malformed"
    assert captured["issue"] == "bad json"


# ---------------------------------------------------------------------------
# accounts.py: ContextIndex trimming and its missing-timestamp guard
# ---------------------------------------------------------------------------


def _ctx_store(tmp_path, monkeypatch, name):
    import danyapi.store as store_mod

    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    return JsonStore(name, "default")


def test_context_index_restore_trims_to_maxsize(tmp_path, monkeypatch):
    store = _ctx_store(tmp_path, monkeypatch, "ctx-trim")
    store.set("s1", ["a"])
    store.set("s2", ["b"])
    store.set("s3", ["c"])

    idx = ContextIndex(1, store=store)

    assert len(idx._seqs) == 1
    assert len(idx._ts) == 1


def test_context_index_trim_guard_leaves_untracked_entries_alone(tmp_path, monkeypatch):
    """With no timestamps recorded, the trim loop must bail out rather than crash."""
    store = _ctx_store(tmp_path, monkeypatch, "ctx-trim-guard")
    idx = ContextIndex(1, store=store)
    idx._seqs["s1"] = ("a",)
    idx._seqs["s2"] = ("b",)

    idx._restore()

    assert len(idx._seqs) == 2


# ---------------------------------------------------------------------------
# api/deepseek.py: the rate-limit hint on the continuation path
# ---------------------------------------------------------------------------

RATE_LIMIT_SSE = (
    "event: ready\n"
    'data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n'
    "\n"
    "event: hint\n"
    'data: {"type":"error","content":"Messages too frequent. Try again later.",'
    '"message":"Messages too frequent. Try again later.","finish_reason":"too_frequent"}\n'
    "\n"
)


class _ContinuationResp:
    def __init__(self, text):
        self._body = text.encode()

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self):
        pass


class _ContinuationAccount:
    def __init__(self):
        self.client = SimpleNamespace(create_pow_challenge=AsyncMock(return_value={}))
        self.pow = SimpleNamespace(make_header=AsyncMock(return_value={}))
        self.label = "acct#0"

    async def wait_turn(self):
        return None

    def note_request_start(self):
        return None

    def recent_request_count(self, window):
        return 0


@pytest.mark.asyncio
async def test_continuation_reports_rate_limit_hint(monkeypatch, caplog):
    responses = [_ContinuationResp(RATE_LIMIT_SSE) for _ in range(ds_mod.MAX_RETRIES + 1)]
    monkeypatch.setattr(ds_mod, "_send_completion", AsyncMock(side_effect=responses))

    with caplog.at_level("WARNING"):
        rec = await ds_mod._collect_continuation(
            _ContinuationAccount(),
            SimpleNamespace(id="c1"),
            None,
            "default",
            False,
            False,
        )

    assert rec is not None
    assert not rec.content
    assert any("rate limit hint" in record.getMessage() for record in caplog.records)
