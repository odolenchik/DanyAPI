"""Coverage for danyapi/api/openai.py.

Targets the lifespan branch where usage tracking is off, the model listing
with provider-specific entries, provider/thinking resolution, and the z.ai
chat route (including its streaming and error arms).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import danyapi.api.openai as openai_mod
from danyapi.api.openai import app, settings

# ---------------------------------------------------------------------------
# lifespan: usage tracking disabled
# ---------------------------------------------------------------------------


async def test_lifespan_with_usage_tracking_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deepseek_tokens", [])
    monkeypatch.setattr(settings, "deepseek_email", "")
    monkeypatch.setattr(settings, "qwen_tokens", [])
    monkeypatch.setattr(settings, "qwen_email", "")
    # z.ai needs no credentials, so it keeps the app bootable without tokens.
    monkeypatch.setattr(settings, "zai_enabled", True)
    monkeypatch.setattr(settings, "zai_tokens", [])
    monkeypatch.setattr(settings, "usage_enabled", False)

    async with app.router.lifespan_context(app):
        assert app.state.usage is None


# ---------------------------------------------------------------------------
# _usage_summary: a broken tracker must not take /health down
# ---------------------------------------------------------------------------


def test_usage_summary_returns_none_when_tracker_missing(monkeypatch):
    monkeypatch.setattr(app.state, "usage", None, raising=False)
    assert openai_mod._usage_summary() is None


def test_usage_summary_swallows_tracker_errors(monkeypatch):
    tracker = MagicMock()
    tracker.snapshot = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(app.state, "usage", tracker, raising=False)
    assert openai_mod._usage_summary() is None


# ---------------------------------------------------------------------------
# /v1/models: provider-specific entries
# ---------------------------------------------------------------------------


async def test_list_models_includes_qwen_and_zai_entries(monkeypatch):
    monkeypatch.setattr(app.state, "qwen_models", [{"id": "qwen3.8-max", "owned_by": "qwen"}], raising=False)
    monkeypatch.setattr(
        app.state,
        "zai_models",
        [{"id": "glm-5.3", "owned_by": "zai", "name": "GLM-5.3", "model_type": "chat"}],
        raising=False,
    )

    payload = await openai_mod.list_models()

    ids = {entry["id"] for entry in payload["data"]}
    assert {"qwen3.8-max", "glm-5.3"} <= ids
    zai = next(entry for entry in payload["data"] if entry["id"] == "glm-5.3")
    assert zai["owned_by"] == "zai"
    assert zai["name"] == "GLM-5.3"


# ---------------------------------------------------------------------------
# provider / thinking resolution
# ---------------------------------------------------------------------------


def test_resolve_provider_routes_glm_to_zai():
    assert openai_mod._resolve_provider("glm-5.3") == "zai"
    assert openai_mod._resolve_provider("zai-something") == "zai"


def test_resolve_thinking_uses_reasoning_effort_when_thinking_absent():
    on = SimpleNamespace(thinking=None, reasoning_effort="high")
    off = SimpleNamespace(thinking=None, reasoning_effort="off")
    assert openai_mod._resolve_thinking(on, default=False) is True
    assert openai_mod._resolve_thinking(off, default=True) is False


# ---------------------------------------------------------------------------
# _chat_completions_zai
# ---------------------------------------------------------------------------


def _zai_request(stream=False):
    return openai_mod.ChatCompletionRequest(model="glm-5.3", messages=[{"role": "user", "content": "hi"}], stream=stream)


async def test_chat_completions_routes_glm_to_zai(monkeypatch):
    handler = AsyncMock(return_value={"choices": []})
    monkeypatch.setattr(openai_mod, "_chat_completions_zai", handler)

    result = await openai_mod.chat_completions(_zai_request())

    handler.assert_awaited_once()
    assert result == {"choices": []}


async def test_zai_route_requires_a_browser(monkeypatch):
    monkeypatch.setattr(app.state, "zai_browser", None, raising=False)
    with pytest.raises(HTTPException) as excinfo:
        await openai_mod._chat_completions_zai(_zai_request())
    assert excinfo.value.status_code == 503


async def test_zai_route_rejects_a_bad_prompt(monkeypatch):
    monkeypatch.setattr(app.state, "zai_browser", MagicMock(), raising=False)
    monkeypatch.setattr(openai_mod.toolemu, "build_prompt", MagicMock(side_effect=ValueError("no messages")))
    with pytest.raises(HTTPException) as excinfo:
        await openai_mod._chat_completions_zai(_zai_request())
    assert excinfo.value.status_code == 400


async def test_zai_route_non_stream_attaches_request_id(monkeypatch):
    monkeypatch.setattr(app.state, "zai_browser", MagicMock(), raising=False)
    monkeypatch.setattr(
        openai_mod.zai_api,
        "collect_non_stream",
        AsyncMock(return_value={"choices": [{"message": {"content": "hi"}}]}),
    )

    result = await openai_mod._chat_completions_zai(_zai_request())

    assert result["choices"][0]["message"]["content"] == "hi"
    assert isinstance(result["x_request_id"], str)
    assert result["x_request_id"]


async def test_zai_route_stream_wraps_the_generator(monkeypatch):
    monkeypatch.setattr(app.state, "zai_browser", MagicMock(), raising=False)

    async def _gen(**kwargs):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(openai_mod.zai_api, "stream_openai", _gen)

    response = await openai_mod._chat_completions_zai(_zai_request(stream=True))

    assert response.media_type == "text/event-stream"
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "[DONE]" in body
