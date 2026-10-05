import asyncio
import base64
import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from danyapi.qwen.client import (
    QwenClient,
    QwenError,
    QwenSession,
    jwt_claims,
    new_uuid,
    sha256_hex,
    timezone_header,
    token_is_expired,
)


def test_new_uuid():
    value = new_uuid()
    assert str(uuid.UUID(value)) == value


def test_sha256_hex():
    assert sha256_hex("hello") == hashlib.sha256(b"hello").hexdigest()


def test_timezone_header():
    value = timezone_header()
    assert "GMT" in value
    assert __import__("re").search(r"GMT[+-]\d{4}$", value)


def test_session_defaults():
    s = QwenSession(id="c1")
    assert s.title == ""
    assert s.last_response_id is None
    assert s.model is None
    assert s.accumulated_input_tokens == 0
    assert s.accumulated_output_tokens == 0
    assert s.extra == {}


def test_session_full():
    s = QwenSession(id="c1", title="t", last_response_id="r1", model="m", accumulated_input_tokens=5, accumulated_output_tokens=7)
    assert s.accumulated_input_tokens == 5
    assert s.accumulated_output_tokens == 7


def test_error_str_and_attrs():
    err = QwenError(401, "bad token")
    assert err.code == 401
    assert err.message == "bad token"
    assert "401" in str(err)


def test_biz_success_returns_data():
    assert QwenClient._biz({"success": True, "data": {"a": 1}}) == {"a": 1}


def test_biz_success_without_data():
    assert QwenClient._biz({"success": True}) == {}


def test_biz_failure_with_data_code():
    with pytest.raises(QwenError) as excinfo:
        QwenClient._biz({"success": False, "data": {"code": 123, "details": "nope"}})
    assert excinfo.value.code == 123
    assert excinfo.value.message == "nope"


def test_biz_failure_generic():
    with pytest.raises(QwenError) as excinfo:
        QwenClient._biz({"success": False, "code": 500, "details": "oops"})
    assert excinfo.value.code == 500
    assert excinfo.value.message == "oops"


def test_biz_failure_no_code_defaults():
    with pytest.raises(QwenError) as excinfo:
        QwenClient._biz({"success": False})
    assert excinfo.value.code == -1


def test_parse_json_non_json_content_type():
    resp = SimpleNamespace(headers={"content-type": "text/html"}, text="<html></html>")
    with pytest.raises(QwenError) as excinfo:
        QwenClient()._parse_json(resp, "/x")
    assert excinfo.value.code == -1


def test_parse_json_invalid_json():
    resp = SimpleNamespace(headers={"content-type": "application/json"}, text="not json")
    resp.json = MagicMock(side_effect=ValueError("boom"))
    with pytest.raises(QwenError):
        QwenClient()._parse_json(resp, "/x")


def test_parse_json_valid_json():
    resp = SimpleNamespace(headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {"k": 1}})
    assert QwenClient()._parse_json(resp, "/x") == {"k": 1}


def test_request_headers():
    headers = QwenClient._request_headers()
    assert "X-Request-Id" in headers
    assert "Timezone" in headers


def test_request_headers_merge():
    headers = QwenClient._request_headers({"Accept": "text/event-stream"})
    assert headers["Accept"] == "text/event-stream"
    assert "X-Request-Id" in headers


def _client(token=None, **kwargs):
    """Client with a pre-seeded access token so no refresh call is needed.

    ``ensure_access_token`` treats a value without an ``exp`` claim as valid,
    so a plain marker string keeps the network out of these tests.
    """
    client = QwenClient(token=token, **kwargs)
    client._access_token = "access-tok"
    client._apply_access_token("access-tok")
    return client


async def test_credential_is_stored_but_not_used_as_bearer():
    # The env credential is a refresh source, not the bearer token itself.
    client = QwenClient(token="tok")
    assert client.token == "tok"
    assert client._access_token is None
    assert "Authorization" not in client.http.headers
    await client.aclose()


async def test_no_token():
    client = QwenClient()
    assert client.token is None
    assert "Authorization" not in client.http.headers
    await client.aclose()


def test_source_cookie_name_for_legacy_token():
    # No ``type`` claim -> legacy session token -> cookie ``token``.
    client = QwenClient(token="not-a-jwt")
    assert client._source_cookie_name == "token"


def test_source_cookie_name_for_refresh_token():
    payload = base64.urlsafe_b64encode(json.dumps({"type": "refresh_token"}).encode()).decode().rstrip("=")
    client = QwenClient(token=f"h.{payload}.s")
    assert client._source_cookie_name == "refresh_token"


def test_jwt_claims_rejects_garbage():
    assert jwt_claims(None) == {}
    assert jwt_claims("") == {}
    assert jwt_claims("not-a-jwt") == {}
    assert jwt_claims("a.b") == {}


def test_jwt_claims_rejects_undecodable_payload():
    assert jwt_claims("a.!!!notbase64!!!.c") == {}


def test_jwt_claims_rejects_non_dict_payload():
    payload = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode().rstrip("=")
    assert jwt_claims(f"a.{payload}.c") == {}


def test_jwt_claims_reads_payload():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 42, "type": "access_token"}).encode()).decode().rstrip("=")
    assert jwt_claims(f"a.{payload}.c") == {"exp": 42, "type": "access_token"}


def test_token_is_expired_without_exp_is_false():
    assert token_is_expired("not-a-jwt") is False
    assert token_is_expired(None) is False


def test_token_is_expired_compares_exp():
    past = int(time.time()) - 10
    future = int(time.time()) + 10_000

    def tok(exp):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"a.{payload}.c"

    assert token_is_expired(tok(past)) is True
    assert token_is_expired(tok(future)) is False
    # A margin pulls an almost-expired token into the expired set.
    assert token_is_expired(tok(int(time.time()) + 30), margin=60) is True


async def test_check_auth_ok():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"success": True})
    client.http.get = AsyncMock(return_value=resp)
    assert await client.check_auth()


async def test_check_auth_non_200():
    client = _client()
    resp = SimpleNamespace(status_code=403)
    resp.json = MagicMock(return_value={"success": False})
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()


async def test_check_auth_profile_without_success_flag():
    # Live /api/v1/auths/ returns the profile with no ``success`` key at all.
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"id": "54fcb681", "email": "a@b.c", "role": "user"})
    client.http.get = AsyncMock(return_value=resp)
    assert await client.check_auth()


async def test_check_auth_nested_profile():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"data": {"id": "54fcb681"}})
    client.http.get = AsyncMock(return_value=resp)
    assert await client.check_auth()


async def test_check_auth_payload_without_identity():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"detail": "not signed in"})
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()


async def test_check_auth_non_dict_payload():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value=["unexpected"])
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()


async def test_check_auth_exception():
    import httpx

    client = _client()
    client.http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert not client.token  # no credential set
    assert not await client.check_auth()


async def test_check_auth_false_when_refresh_fails():
    # With no credential at all, check_auth must report False, not raise.
    client = QwenClient()
    assert not await client.check_auth()


class _FakeAuthClient:
    """Stands in for the ``auth.qwen.ai`` client inside refresh/login."""

    def __init__(self, get_response=None, post_response=None):
        self.get = AsyncMock(return_value=get_response)
        self.post = AsyncMock(return_value=post_response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_auth(monkeypatch, fake):
    import danyapi.qwen.client as mod

    monkeypatch.setattr(mod, "new_auth_client", lambda timeout: fake)


def _json_resp(payload, status_code=200):
    resp = SimpleNamespace(status_code=status_code, text=json.dumps(payload))
    resp.json = MagicMock(return_value=payload)
    return resp


# ---------------------------------------------------------------------------
# refresh_access_token / ensure_access_token
# ---------------------------------------------------------------------------


async def test_refresh_uses_auth_host_with_timezone_and_origin(monkeypatch):
    access = "a." + base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 900}).encode()).decode().rstrip("=") + ".c"
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": access}}))
    _patch_auth(monkeypatch, fake)

    client = QwenClient(token="cred")
    token = await client.refresh_access_token()

    assert token == access
    assert client._access_token == access
    assert client.http.headers["Authorization"] == f"Bearer {access}"
    url = fake.get.await_args.args[0]
    headers = fake.get.await_args.kwargs["headers"]
    cookies = fake.get.await_args.kwargs["cookies"]
    assert url == "https://auth.qwen.ai/api/v2/auths/refresh"
    # Both headers are mandatory: without timezone the API answers
    # "Invalid request header", without x-request-origin "Missing origin".
    assert headers["Timezone"]
    assert headers["x-request-origin"] == "https://chat.qwen.ai"
    assert headers["version"] == "0.3.12"
    assert cookies == {"token": "cred"}


async def test_refresh_sends_refresh_cookie_for_refresh_token(monkeypatch):
    payload = base64.urlsafe_b64encode(json.dumps({"type": "refresh_token"}).encode()).decode().rstrip("=")
    credential = f"h.{payload}.s"
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "acc"}}))
    _patch_auth(monkeypatch, fake)

    client = QwenClient(token=credential)
    await client.refresh_access_token()

    assert fake.get.await_args.kwargs["cookies"] == {"refresh_token": credential}


async def test_refresh_raises_without_credential():
    client = QwenClient()
    with pytest.raises(QwenError, match="no qwen credential"):
        await client.refresh_access_token()


async def test_refresh_raises_on_non_json(monkeypatch):
    resp = SimpleNamespace(status_code=200, text="<html>", json=MagicMock(side_effect=ValueError))
    _patch_auth(monkeypatch, _FakeAuthClient(get_response=resp))
    client = QwenClient(token="cred")
    with pytest.raises(QwenError, match="invalid JSON"):
        await client.refresh_access_token()


async def test_refresh_raises_when_success_false(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": False, "data": {"code": "Unauthorized", "details": "nope"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    with pytest.raises(QwenError):
        await client.refresh_access_token()


async def test_refresh_raises_when_access_token_missing(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    with pytest.raises(QwenError, match="no access_token"):
        await client.refresh_access_token()


async def test_ensure_access_token_reuses_valid_token(monkeypatch):
    fake = _FakeAuthClient()
    _patch_auth(monkeypatch, fake)
    client = _client()
    assert await client.ensure_access_token() == "access-tok"
    fake.get.assert_not_awaited()


async def test_ensure_access_token_refreshes_when_expired(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    exp = int(time.time()) - 10
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    client._access_token = f"a.{payload}.c"
    assert await client.ensure_access_token() == "fresh"
    fake.get.assert_awaited_once()


async def test_ensure_access_token_refreshes_when_missing(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    assert await client.ensure_access_token() == "fresh"


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def test_login_uses_auth_host_and_adopts_refresh_token(monkeypatch):
    fake = _FakeAuthClient(post_response=_json_resp({"success": True, "data": {"refresh_token": "rt", "access_token": "at"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient()
    credential = await client.login("a@b.c", "pw")
    assert credential == "rt"
    assert client.token == "rt"
    assert client._access_token == "at"
    assert fake.post.await_args.args[0] == "https://auth.qwen.ai/api/v2/auths/signin"


async def test_login_falls_back_to_legacy_token_field(monkeypatch):
    fake = _FakeAuthClient(post_response=_json_resp({"success": True, "data": {"token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient()
    credential = await client.login("a@b.c", "pw")
    assert credential == "fresh"
    assert client._access_token is None


async def test_login_no_token_raises(monkeypatch):
    fake = _FakeAuthClient(post_response=_json_resp({"success": True, "data": {}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient()
    with pytest.raises(QwenError):
        await client.login("a@b.c", "pw")


async def test_login_invalid_json_raises(monkeypatch):
    resp = SimpleNamespace(status_code=200, text="<html>", json=MagicMock(side_effect=ValueError))
    _patch_auth(monkeypatch, _FakeAuthClient(post_response=resp))
    client = QwenClient()
    with pytest.raises(QwenError, match="invalid JSON"):
        await client.login("a@b.c", "pw")


# ---------------------------------------------------------------------------
# JSON API methods (use _send, so they need a seeded access token)
# ---------------------------------------------------------------------------


async def test_fetch_models():
    client = _client()
    resp = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {"data": [{"id": "m1"}]}})
    client.http.request = AsyncMock(return_value=resp)
    models = await client.fetch_models()
    assert models == [{"id": "m1"}]


async def test_send_refreshes_once_on_401(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = _client(token="cred")
    ok = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    ok.json = MagicMock(return_value={"success": True, "data": {"data": [{"id": "m1"}]}})
    unauth = SimpleNamespace(status_code=401, headers={"content-type": "application/json"})
    client.http.request = AsyncMock(side_effect=[unauth, ok])
    assert await client.fetch_models() == [{"id": "m1"}]
    fake.get.assert_awaited_once()
    assert client.http.request.await_count == 2


async def test_create_chat():
    client = _client()
    resp = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {"id": "chat1"}})
    client.http.request = AsyncMock(return_value=resp)
    assert await client.create_chat("qwen3.8-max") == "chat1"


async def test_create_chat_no_id():
    client = _client()
    resp = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {}})
    client.http.request = AsyncMock(return_value=resp)
    with pytest.raises(QwenError):
        await client.create_chat("qwen3.8-max")


async def test_completion_builds_request():
    client = _client()
    sent = SimpleNamespace(status_code=200)
    client.http.build_request = MagicMock(return_value=object())
    client.http.send = AsyncMock(return_value=sent)
    result = await client.completion("chat1", "hi", "p1", "m", thinking=True, search=True)
    assert result is sent
    _, kwargs = client.http.build_request.call_args
    assert kwargs["json"]["chatId"] == "chat1"
    assert kwargs["json"]["model"] == "m"


async def test_completion_retries_once_on_401(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = _client(token="cred")
    unauth = SimpleNamespace(status_code=401, aclose=AsyncMock())
    ok = SimpleNamespace(status_code=200)
    client.http.build_request = MagicMock(return_value=object())
    client.http.send = AsyncMock(side_effect=[unauth, ok])
    result = await client.completion("chat1", "hi", "p1", "m")
    assert result is ok
    unauth.aclose.assert_awaited_once()
    fake.get.assert_awaited_once()


async def test_completion_puts_image_size_at_top_level():
    """Aspect ratio goes at the TOP level of the body, not into extra.meta.

    Verified live: size in extra.meta is ignored by Qwen (2048x2048 square),
    while the same value at the top level yields the requested ratio.
    """
    client = _client()
    sent = SimpleNamespace(status_code=200)
    client.http.build_request = MagicMock(return_value=object())
    client.http.send = AsyncMock(return_value=sent)

    await client.completion(
        "chat1",
        "hi",
        "p1",
        "m",
        chat_type="t2i",
        image_model="qwen-image-3.0-pro",
        image_size="2:3",
    )

    _, kwargs = client.http.build_request.call_args
    body = kwargs["json"]
    assert body["size"] == "2:3"
    meta = body["messages"][0]["extra"]["meta"]
    assert "size" not in meta
    assert meta["model"] == "qwen-image-3.0-pro"


async def test_completion_omits_size_when_unset():
    client = _client()
    sent = SimpleNamespace(status_code=200)
    client.http.build_request = MagicMock(return_value=object())
    client.http.send = AsyncMock(return_value=sent)

    await client.completion("chat1", "hi", "p1", "m")

    _, kwargs = client.http.build_request.call_args
    assert "size" not in kwargs["json"]


async def test_stop_stream():
    client = _client()
    resp = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {}})
    client.http.request = AsyncMock(return_value=resp)
    await client.stop_stream("chat1", "r1")
    client.http.request.assert_awaited_once()


async def test_fetch_chat_hits_the_chat_endpoint():
    client = _client()
    resp = SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    resp.json = MagicMock(return_value={"success": True, "data": {"id": "chat1"}})
    client.http.request = AsyncMock(return_value=resp)
    assert await client.fetch_chat("chat1") == {"id": "chat1"}
    assert client.http.request.await_args.args[1] == "/api/v2/chats/chat1"


async def test_new_auth_client_targets_no_base_url():
    # The factory exists so tests can substitute it, but it is still real code:
    # a bare client with no chat base_url, cookies or Authorization header.
    import danyapi.qwen.client as mod

    client = mod.new_auth_client(5.0)
    try:
        assert str(client.base_url) == ""
        assert "Authorization" not in client.headers
    finally:
        await client.aclose()


async def test_ensure_access_token_rechecks_after_waiting_for_lock(monkeypatch):
    # A second coroutine can refresh while the first waits on the lock; the
    # inner re-check must then hand back that token instead of refreshing again.
    fake = _FakeAuthClient()
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")

    await client._refresh_lock.acquire()
    try:
        task = asyncio.create_task(client.ensure_access_token())
        await asyncio.sleep(0)  # let the task hit the lock
        client._access_token = "refreshed-elsewhere"
    finally:
        client._refresh_lock.release()

    assert await task == "refreshed-elsewhere"
    fake.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# last_auth_error
# ---------------------------------------------------------------------------


async def test_refresh_records_auth_error(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": False, "data": {"code": "Unauthorized", "details": "Token is revoked, please login again."}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    with pytest.raises(QwenError):
        await client.refresh_access_token()
    assert client.last_auth_error
    assert "Token is revoked" in client.last_auth_error
    await client.aclose()


async def test_refresh_records_transport_error(monkeypatch):
    fake = _FakeAuthClient()
    fake.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    with pytest.raises(httpx.HTTPError):
        await client.refresh_access_token()
    assert client.last_auth_error
    assert "transport error" in client.last_auth_error
    await client.aclose()


async def test_refresh_clears_auth_error_on_success(monkeypatch):
    fake = _FakeAuthClient(get_response=_json_resp({"success": True, "data": {"access_token": "fresh"}}))
    _patch_auth(monkeypatch, fake)
    client = QwenClient(token="cred")
    client._last_error = "stale"
    assert await client.refresh_access_token() == "fresh"
    assert client.last_auth_error is None
    await client.aclose()


async def test_check_auth_records_status_reason():
    client = _client()
    resp = SimpleNamespace(status_code=403)
    resp.json = MagicMock(return_value={"success": False})
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()
    assert client.last_auth_error
    assert "403" in client.last_auth_error
    await client.aclose()


async def test_check_auth_records_missing_identity_reason():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"detail": "not signed in"})
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()
    assert client.last_auth_error
    await client.aclose()


async def test_check_auth_records_transport_error():
    client = _client()
    client.http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert not await client.check_auth()
    assert client.last_auth_error
    assert "ConnectError" in client.last_auth_error
    await client.aclose()


async def test_check_auth_records_json_error():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(side_effect=ValueError("bad json"))
    client.http.get = AsyncMock(return_value=resp)
    assert not await client.check_auth()
    assert client.last_auth_error
    assert "ValueError" in client.last_auth_error
    await client.aclose()


async def test_check_auth_clears_reason_on_success():
    client = _client()
    resp = SimpleNamespace(status_code=200)
    resp.json = MagicMock(return_value={"success": True})
    client.http.get = AsyncMock(return_value=resp)
    client._last_error = "old"
    assert await client.check_auth()
    assert client.last_auth_error is None
    await client.aclose()
