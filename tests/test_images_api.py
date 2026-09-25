import asyncio
import base64
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import danyapi.api.openai as openai_mod
from danyapi.accounts import AccountPoolBusy
from danyapi.api.openai import app


def _png_bytes(width=4, height=4):
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(1, 2, 3))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeImageResponse:
    def __init__(self, content):
        self.status_code = 200
        self.content = content


class FakeHttpClient:
    def __init__(self, content):
        self._content = content
        self.requested: list[str] = []

    async def get(self, url):
        self.requested.append(url)
        return FakeImageResponse(self._content)


def _pool(session_id="s1"):
    pool = MagicMock()
    account = MagicMock()
    account.sem = asyncio.Semaphore(1)
    pool.acquire = AsyncMock(return_value=(account, session_id))
    return pool


@pytest.fixture(autouse=True)
def clean_state():
    saved = (
        getattr(app.state, "qwen_pool", None),
        getattr(app.state, "http_client", None),
    )
    app.state.qwen_pool = None
    if hasattr(app.state, "http_client"):
        delattr(app.state, "http_client")
    yield
    app.state.qwen_pool = saved[0]
    if saved[1] is None:
        if hasattr(app.state, "http_client"):
            delattr(app.state, "http_client")
    else:
        app.state.http_client = saved[1]


def test_generations_without_pool_returns_503():
    client = TestClient(app)
    resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
    client.close()
    assert resp.status_code == 503


def test_edits_without_pool_returns_503():
    client = TestClient(app)
    resp = client.post("/v1/images/edits", files={"image": ("a.png", b"xx", "image/png")})
    client.close()
    assert resp.status_code == 503


def test_variations_without_pool_returns_503():
    client = TestClient(app)
    resp = client.post("/v1/images/variations", files={"image": ("a.png", b"xx", "image/png")})
    client.close()
    assert resp.status_code == 503


async def test_generations_url_response():
    app.state.qwen_pool = _pool()
    with patch.object(
        openai_mod.qwen_api,
        "collect_image",
        AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/a.png"], "revised_prompt": "cat", "usage": {"total_tokens": 3}, "session_id": "s1"}),
    ):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"] == [{"url": "https://cdn.qwenlm.ai/a.png"}]
    assert data["session_id"] == "s1"
    assert data["usage"] == {"total_tokens": 3}


async def test_generations_b64_json():
    app.state.qwen_pool = _pool()
    fake_client = FakeHttpClient(_png_bytes())
    with (
        patch.object(
            openai_mod.qwen_api,
            "collect_image",
            AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/a.png"], "revised_prompt": "", "usage": None, "session_id": "s1"}),
        ),
        patch.object(openai_mod, "_image_http_client", lambda: fake_client),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/generations",
            json={"model": "qwen-image-gen", "prompt": "a cat", "response_format": "b64_json"},
        )
        client.close()
    assert resp.status_code == 200
    item = resp.json()["data"][0]
    assert base64.b64decode(item["b64_json"]) == _png_bytes()


async def test_generations_resizes_when_size_given():
    app.state.qwen_pool = _pool()
    fake_client = FakeHttpClient(_png_bytes(64, 64))
    with (
        patch.object(
            openai_mod.qwen_api,
            "collect_image",
            AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/a.png"], "revised_prompt": "", "usage": None, "session_id": "s1"}),
        ),
        patch.object(openai_mod, "_image_http_client", lambda: fake_client),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/generations",
            json={"model": "qwen-image-gen", "prompt": "a cat", "size": "32x16"},
        )
        client.close()
    assert resp.status_code == 200
    from PIL import Image

    payload = base64.b64decode(resp.json()["data"][0]["b64_json"])
    with Image.open(BytesIO(payload)) as img:
        assert img.size == (32, 16)


async def test_generations_download_failure_returns_url():
    app.state.qwen_pool = _pool()

    class FailingClient:
        async def get(self, url):
            return type("R", (), {"status_code": 404, "content": b""})()

    with (
        patch.object(
            openai_mod.qwen_api,
            "collect_image",
            AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/a.png"], "revised_prompt": "", "usage": None, "session_id": "s1"}),
        ),
        patch.object(openai_mod, "_image_http_client", FailingClient),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/generations",
            json={"model": "qwen-image-gen", "prompt": "a cat", "response_format": "b64_json"},
        )
        client.close()
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"url": "https://cdn.qwenlm.ai/a.png"}]


def test_generations_invalid_size_returns_400():
    app.state.qwen_pool = _pool()
    client = TestClient(app)
    resp = client.post(
        "/v1/images/generations",
        json={"model": "qwen-image-gen", "prompt": "a cat", "size": "bogus"},
    )
    client.close()
    assert resp.status_code == 400


async def test_generations_busy_returns_429():
    pool = _pool()
    pool.acquire = AsyncMock(side_effect=AccountPoolBusy())
    app.state.qwen_pool = pool
    client = TestClient(app)
    resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
    client.close()
    assert resp.status_code == 429


async def test_generations_no_urls_returns_502():
    app.state.qwen_pool = _pool()
    with patch.object(
        openai_mod.qwen_api,
        "collect_image",
        AsyncMock(return_value={"image_urls": [], "revised_prompt": "", "usage": None, "session_id": "s1"}),
    ):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 502


async def test_edits_forwards_markdown_prompt():
    app.state.qwen_pool = _pool()
    collected = AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/e.png"], "revised_prompt": "", "usage": None, "session_id": "s1"})
    with patch.object(openai_mod.qwen_api, "collect_image", collected):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/edits",
            files={"image": ("a.png", _png_bytes(), "image/png")},
            data={"prompt": "make it blue"},
        )
        client.close()
    assert resp.status_code == 200
    prompt = collected.call_args.kwargs["prompt"]
    assert prompt.startswith("make it blue")
    assert "![image](data:image/png;base64," in prompt


async def test_edits_with_mask():
    app.state.qwen_pool = _pool()
    collected = AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/e.png"], "revised_prompt": "", "usage": None, "session_id": "s1"})
    with patch.object(openai_mod.qwen_api, "collect_image", collected):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/edits",
            files={
                "image": ("a.png", _png_bytes(), "image/png"),
                "mask": ("m.png", _png_bytes(), "image/png"),
            },
            data={"prompt": "fix"},
        )
        client.close()
    assert resp.status_code == 200
    assert collected.call_args.kwargs["prompt"].count("![image](data:image/png;base64,") == 2


async def test_variations_uses_image_as_prompt():
    app.state.qwen_pool = _pool()
    collected = AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/v.png"], "revised_prompt": "", "usage": None, "session_id": "s1"})
    with patch.object(openai_mod.qwen_api, "collect_image", collected):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/variations",
            files={"image": ("a.png", _png_bytes(), "image/png")},
        )
        client.close()
    assert resp.status_code == 200
    assert collected.call_args.kwargs["prompt"].startswith("![image](data:image/png;base64,")


async def test_edits_upload_too_large_returns_413():
    app.state.qwen_pool = _pool()
    oversized = b"x" * (openai_mod.MAX_FILE_SIZE + 1)
    client = TestClient(app)
    resp = client.post("/v1/images/edits", files={"image": ("a.png", oversized, "image/png")})
    client.close()
    assert resp.status_code == 413


async def test_image_pool_missing_raises_503():
    with pytest.raises(openai_mod.HTTPException) as excinfo:
        await openai_mod._image_pool(MagicMock())
    assert excinfo.value.status_code == 503


async def test_image_fetch_exception_falls_back_to_url():
    app.state.qwen_pool = _pool()

    class ExplodingClient:
        async def get(self, url):
            raise RuntimeError("connection refused")

    with (
        patch.object(
            openai_mod.qwen_api,
            "collect_image",
            AsyncMock(return_value={"image_urls": ["https://cdn.qwenlm.ai/x.png"], "revised_prompt": "", "usage": None, "session_id": "s1"}),
        ),
        patch.object(openai_mod, "_image_http_client", ExplodingClient),
    ):
        client = TestClient(app)
        resp = client.post(
            "/v1/images/generations",
            json={"model": "qwen-image-gen", "prompt": "a cat", "response_format": "b64_json"},
        )
        client.close()
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"url": "https://cdn.qwenlm.ai/x.png"}]


async def test_generations_failover_to_next_account_on_quota():
    pool = _pool()
    acct_a, acct_b = MagicMock(), MagicMock()
    acct_a.sem = asyncio.Semaphore(1)
    acct_b.sem = asyncio.Semaphore(1)
    pool.accounts = [acct_a, acct_b]
    pool.acquire = AsyncMock(side_effect=[(acct_a, "s1"), (acct_b, "s1")])
    app.state.qwen_pool = pool
    ok = {"image_urls": ["https://cdn.qwenlm.ai/b.png"], "revised_prompt": "", "usage": None, "session_id": "s1"}
    collected = AsyncMock(side_effect=[openai_mod.qwen_api.QuotaExhausted("quota exhausted"), ok])
    with patch.object(openai_mod.qwen_api, "collect_image", collected):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"url": "https://cdn.qwenlm.ai/b.png"}]
    assert collected.await_count == 2


async def test_generations_all_accounts_quota_returns_429():
    pool = _pool()
    acct_a, acct_b = MagicMock(), MagicMock()
    acct_a.sem = asyncio.Semaphore(1)
    acct_b.sem = asyncio.Semaphore(1)
    pool.accounts = [acct_a, acct_b]
    pool.acquire = AsyncMock(side_effect=[(acct_a, "s1"), (acct_b, "s1")])
    app.state.qwen_pool = pool
    with patch.object(
        openai_mod.qwen_api,
        "collect_image",
        AsyncMock(side_effect=openai_mod.qwen_api.QuotaExhausted("daily image generation quota exhausted")),
    ):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 429
    assert "quota" in resp.json()["detail"]


async def test_generations_no_data_with_revised_prompt_returns_502_with_text():
    app.state.qwen_pool = _pool()
    with patch.object(
        openai_mod.qwen_api,
        "collect_image",
        AsyncMock(return_value={"image_urls": [], "revised_prompt": "quota notice", "usage": None, "session_id": "s1"}),
    ):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 502
    assert "quota notice" in resp.json()["detail"]


async def test_generations_busy_during_fetch_returns_429():
    app.state.qwen_pool = _pool()
    with patch.object(
        openai_mod.qwen_api,
        "collect_image",
        AsyncMock(side_effect=AccountPoolBusy()),
    ):
        client = TestClient(app)
        resp = client.post("/v1/images/generations", json={"model": "qwen-image-gen", "prompt": "a cat"})
        client.close()
    assert resp.status_code == 429


def test_image_edit_req_includes_mask():
    result = openai_mod._image_edit_req("do it", "IMAGE", "MASK")
    assert result == "do it\nIMAGE\nMASK"


def test_image_edit_req_empty_prompt():
    result = openai_mod._image_edit_req("   ", "IMAGE", None)
    assert result == "IMAGE"


async def test_b64encode_large_payload_uses_thread():
    payload = b"x" * (openai_mod._ASYNC_B64_THRESHOLD + 1)
    encoded = await openai_mod._b64encode(payload)
    assert base64.b64decode(encoded) == payload


async def test_lifespan_closes_http_client(monkeypatch):
    closed = {"done": False}

    class FakeHttpClient:
        async def aclose(self):
            closed["done"] = True

    app.state.http_client = FakeHttpClient()
    try:
        async with openai_mod.lifespan(app):
            pass
    finally:
        if hasattr(app.state, "http_client"):
            delattr(app.state, "http_client")
    assert closed["done"]
