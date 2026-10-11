from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("danyapi.qwen")

BASE_URL = "https://chat.qwen.ai"
# Frontend 0.3.12 moved authentication off chat.qwen.ai onto its own host, and
# its endpoints live outside /api/v1 (the bundle calls this ``nonV1APi``).
AUTH_URL = "https://auth.qwen.ai"

WEB_VERSION = "0.3.12"

# Refresh the access token this many seconds before it actually expires, so a
# request that is about to start never races the deadline.
REFRESH_MARGIN_SEC = 60.0

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "source": "web",
    "version": WEB_VERSION,
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def new_uuid() -> str:
    return str(uuid.uuid4())


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timezone_header() -> str:
    now = datetime.datetime.now().astimezone()
    offset = now.strftime("%z")
    return f"{now.strftime('%a %b %d %Y %H:%M:%S')} GMT{offset}"


def jwt_claims(token: str | None) -> dict:
    """Decode a JWT payload without verifying the signature.

    Used only to read ``type``/``exp`` metadata for the refresh flow, never for
    trust decisions. Returns ``{}`` for anything that is not a decodable JWT.
    """
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def token_is_expired(token: str | None, margin: float = 0.0) -> bool:
    """True when the token's ``exp`` is in the past (or within ``margin``)."""
    exp = jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return time.time() >= float(exp) - margin


def _token_fingerprint(token: str | None) -> str:
    """Short, non-secret label for a credential (type + jti + expiry).

    Lets a rotated or revoked token be told apart in logs without printing the
    secret itself: the ``jti`` changes when the server hands out a replacement,
    while ``exp`` only moves when the lifetime itself changes.
    """
    claims = jwt_claims(token)
    jti = claims.get("jti")
    kind = claims.get("type") or "?"
    short_jti = str(jti)[:8] if jti else "-"
    exp = claims.get("exp") or "-"
    return f"type={kind} jti={short_jti} exp={exp}"


def new_auth_client(timeout: float) -> httpx.AsyncClient:
    """Build the client used against ``auth.qwen.ai``.

    Auth lives on its own host and must not inherit the chat client's base_url,
    cookies or Authorization header, so it gets a fresh client. Kept as a
    module-level factory so tests can substitute a fake.
    """
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


@dataclass
class QwenSession:
    id: str
    title: str = ""
    last_response_id: str | None = None
    model: str | None = None
    chat_type: str = "t2t"
    accumulated_input_tokens: int = 0
    accumulated_output_tokens: int = 0
    reattached: bool = False
    extra: dict = field(default_factory=dict)


class QwenError(Exception):
    def __init__(self, code: int | str, message: str) -> None:
        super().__init__(f"Qwen error {code}: {message}")
        self.code = code
        self.message = message


class QwenClient:
    def __init__(
        self,
        token: str | None = None,
        timeout: float = 60.0,
        token_persist: Callable[[str], None] | None = None,
    ) -> None:
        # ``token`` is the credential from the environment. With frontend
        # 0.3.12 it may be either a ``refresh_token`` (preferred) or a legacy
        # session token; both are exchanged for a short-lived access token via
        # ``auth.qwen.ai``. The raw value is kept because the refresh flow needs
        # it on every call.
        self.token = token
        # Called when the refresh endpoint hands back a *new* refresh token
        # (server-side rotation). Without persisting it the rotated credential is
        # lost on the next restart and the old one is eventually revoked.
        self._token_persist = token_persist
        self._access_token: str | None = None
        # Reason the most recent auth attempt failed (None after a success), so
        # startup can report *why* a credential was skipped.
        self._last_error: str | None = None
        self._refresh_lock = asyncio.Lock()
        headers = {
            "User-Agent": USER_AGENT,
            **COMMON_HEADERS,
        }
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    @property
    def _source_cookie_name(self) -> str:
        """Cookie the refresh endpoint expects for the configured credential.

        A ``refresh_token`` must be sent as the ``refresh_token`` cookie; a
        legacy session token only works as ``token``. Verified live: mixing the
        two names is rejected.
        """
        if jwt_claims(self.token).get("type") == "refresh_token":
            return "refresh_token"
        return "token"

    def _apply_access_token(self, access_token: str) -> None:
        self._access_token = access_token
        self.http.headers["Authorization"] = f"Bearer {access_token}"
        self.http.cookies.set("token", access_token, domain="chat.qwen.ai", path="/")

    @property
    def last_auth_error(self) -> str | None:
        """Human-readable reason the last auth attempt failed (None on success).

        ``refresh_access_token`` and ``check_auth`` record the failure here so
        startup can say why a credential was rejected instead of a bare
        "invalid/expired".
        """
        return self._last_error

    async def refresh_access_token(self) -> str:
        """Exchange the configured credential for a fresh access token.

        Calls ``GET https://auth.qwen.ai/api/v2/auths/refresh``. The ``timezone``
        header is mandatory there (its absence yields ``Invalid request header``)
        and the request must look like it comes from chat.qwen.ai, hence
        ``x-request-origin``.
        """
        if not self.token:
            self._last_error = "no qwen credential configured"
            raise QwenError(-1, "no qwen credential configured")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL + "/",
            "source": "web",
            "version": WEB_VERSION,
            "x-request-origin": BASE_URL,
            "Timezone": timezone_header(),
            "X-Request-Id": new_uuid(),
        }
        cookies = {self._source_cookie_name: self.token}
        before = _token_fingerprint(self.token)
        resp: httpx.Response | None = None
        try:
            async with new_auth_client(self.http.timeout) as client:
                resp = await client.get(f"{AUTH_URL}/api/v2/auths/refresh", headers=headers, cookies=cookies)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise QwenError(-1, f"invalid JSON from auth refresh: {resp.text[:200]}") from exc
            biz = self._biz(payload)
            access_token = biz.get("access_token")
            if not access_token:
                raise QwenError(-1, "auth refresh returned no access_token")
        except QwenError as exc:
            # The auth host answers HTTP 200 even for a refusal and carries the
            # real outcome in ``x-actual-status-code``; log both so a rejected
            # credential can be told from a transport failure at a glance.
            status = getattr(resp, "status_code", "?")
            resp_headers = getattr(resp, "headers", None) or {}
            actual_status = resp_headers.get("x-actual-status-code", "?")
            self._last_error = f"{exc.code}: {exc.message}"
            log.warning(
                "qwen refresh refused (%s): credential=%s http=%s actual=%s detail=%s",
                exc.code,
                before,
                status,
                actual_status,
                exc.message,
            )
            raise
        except httpx.HTTPError as exc:
            self._last_error = f"transport error: {exc}"
            log.warning("qwen refresh transport error: credential=%s error=%s", before, exc)
            raise
        self._adopt_refresh_token(biz.get("refresh_token"))
        self._apply_access_token(access_token)
        self._last_error = None
        log.info(
            "qwen access token refreshed (expires in %ss) credential=%s",
            jwt_claims(access_token).get("exp", 0) - int(time.time()),
            before,
        )
        return access_token

    def _adopt_refresh_token(self, new_token: object) -> None:
        """Adopt a rotated refresh token handed back by the refresh endpoint.

        Qwen rotates the ``refresh_token`` server-side: every successful refresh
        may return a *new* credential while the one we sent keeps working only
        for a grace window. Keeping the old value in memory and on disk means the
        rotated one is dropped, and once the grace window closes every further
        refresh is refused with ``Token is revoked`` — which is exactly the
        ~5 h token lifetime seen in production. Persisting the new value (via the
        callback) is what makes the credential outlive a restart.
        """
        if not isinstance(new_token, str) or not new_token or new_token == self.token:
            return
        log.warning(
            "qwen refresh_token was ROTATED by the server (old=%s new=%s); adopting the new value",
            _token_fingerprint(self.token),
            _token_fingerprint(new_token),
        )
        self.token = new_token
        if self._token_persist is not None:
            try:
                self._token_persist(new_token)
            except Exception as exc:  # persistence must never break the refresh
                log.warning("qwen refresh token persist failed: %s", exc)

    async def ensure_access_token(self) -> str:
        """Return a usable access token, refreshing when missing or near expiry."""
        if self._access_token and not token_is_expired(self._access_token, REFRESH_MARGIN_SEC):
            return self._access_token
        async with self._refresh_lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if self._access_token and not token_is_expired(self._access_token, REFRESH_MARGIN_SEC):
                return self._access_token
            return await self.refresh_access_token()

    @staticmethod
    def _request_headers(headers: dict | None = None) -> dict:
        base = {
            "X-Request-Id": new_uuid(),
            "Timezone": timezone_header(),
        }
        if headers:
            base.update(headers)
        return base

    @staticmethod
    def _biz(payload: dict) -> dict:
        if not payload.get("success", False):
            data = payload.get("data")
            if isinstance(data, dict) and data.get("code"):
                raise QwenError(data["code"], data.get("details") or data.get("message") or "")
            raise QwenError(payload.get("code") or -1, payload.get("details") or payload.get("message") or "request failed")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _parse_json(self, resp: httpx.Response, path: str) -> dict:
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            raise QwenError(-1, f"unexpected non-JSON response from {path}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise QwenError(-1, f"invalid JSON from {path}") from exc
        return self._biz(payload)

    async def _send(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """Send a JSON API request with a valid access token.

        Refreshes proactively when the token is missing or near expiry, and once
        reactively on 401: a token can be revoked server-side well before its
        ``exp``, so expiry alone is not a reliable signal.
        """
        await self.ensure_access_token()
        resp = await self.http.request(method, path, json=json_body, params=params, headers=self._request_headers(headers))
        if resp.status_code == 401:
            await self.refresh_access_token()
            resp = await self.http.request(method, path, json=json_body, params=params, headers=self._request_headers(headers))
        return resp

    async def _post(self, path: str, json_body: dict | None = None, params: dict | None = None, headers: dict | None = None) -> dict:
        resp = await self._send("POST", path, json_body=json_body, params=params, headers=headers)
        return self._parse_json(resp, path)

    async def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> dict:
        resp = await self._send("GET", path, params=params, headers=headers)
        return self._parse_json(resp, path)

    async def check_auth(self) -> bool:
        """Report whether the configured credential still yields a session.

        With 0.3.12 the credential is a refresh source, so this proves it is
        usable by actually minting an access token from it; a stored access
        token is never taken at face value. On failure the reason is recorded in
        ``last_auth_error`` so startup can report it.
        """
        try:
            await self.ensure_access_token()
            resp = await self.http.get("/api/v1/auths/", headers=self._request_headers())
            if resp.status_code != 200:
                self._last_error = f"/api/v1/auths/ returned {resp.status_code}"
                return False
            payload = resp.json()
            if not isinstance(payload, dict):
                self._last_error = "/api/v1/auths/ returned a non-object payload"
                return False
            nested = payload.get("data")
            if payload.get("success") is True or (isinstance(nested, dict) and nested.get("id")) or payload.get("id"):
                self._last_error = None
                return True
            self._last_error = "auth profile carries no identity"
            return False
        except QwenError as exc:
            self._last_error = f"{exc.code}: {exc.message}"
            return False
        except (httpx.HTTPError, ValueError) as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    async def login(self, email: str, password: str) -> str:
        """Sign in with credentials and adopt the returned session credential.

        Note: the sign-in response shape for the 0.3.12 auth host could not be
        verified live (no throwaway account), so both the access and refresh
        fields are accepted and the credential is then exercised through the
        normal refresh path before anything relies on it.
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL + "/",
            "source": "web",
            "version": WEB_VERSION,
            "x-request-origin": BASE_URL,
            "Timezone": timezone_header(),
            "X-Request-Id": new_uuid(),
        }
        async with new_auth_client(self.http.timeout) as client:
            resp = await client.post(
                f"{AUTH_URL}/api/v2/auths/signin",
                json={"email": email, "password": sha256_hex(password)},
                headers=headers,
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise QwenError(-1, f"invalid JSON from signin: {resp.text[:200]}") from exc
        biz = self._biz(payload)
        credential = biz.get("refresh_token") or biz.get("access_token") or biz.get("token") or (biz.get("user") or {}).get("token")
        if not credential:
            raise QwenError(-1, "login failed: no token in response")
        self.token = credential
        self._access_token = biz.get("access_token") or None
        if self._access_token:
            self._apply_access_token(self._access_token)
        return credential

    async def fetch_models(self) -> list[dict]:
        biz = await self._get("/api/v2/models/")
        models = biz.get("data")
        return models if isinstance(models, list) else []

    async def fetch_chat(self, chat_id: str) -> dict:
        """Fetch a chat's metadata and history.

        The response carries ``chat_type`` (``t2t``/``t2i``) plus
        ``chat.history`` and ``chat.messages``; ``history.currentId`` is the tail
        message id to continue from. Used to reattach a chat whose local registry
        entry was lost.
        """
        return await self._get(f"/api/v2/chats/{chat_id}")

    async def create_chat(self, model: str, chat_mode: str = "normal", chat_type: str = "t2t") -> str:
        started = time.monotonic()
        body = {
            "chatId": "",
            "models": [model],
            "project_id": "",
            "timestamp": int(datetime.datetime.now().timestamp() * 1000),
            "chat_type": chat_type,
            "chat_mode": chat_mode,
        }
        biz = await self._post("/api/v2/chats/new", body)
        chat_id = biz.get("id")
        if not chat_id:
            raise QwenError(-1, "create chat failed: no chat id in response")
        log.info("qwen create chat OK (%.0fms)", (time.monotonic() - started) * 1000)
        return chat_id

    async def completion(
        self,
        chat_session_id: str,
        prompt: str,
        parent_message_id: str | None,
        model: str,
        thinking: bool = False,
        search: bool = False,
        chat_type: str = "t2t",
        image_model: str | None = None,
        image_size: str | None = None,
    ) -> httpx.Response:
        log.debug("qwen completion start session=%s model=%s chat_type=%s", chat_session_id, model, chat_type)
        ts = int(datetime.datetime.now().timestamp())
        user_fid = new_uuid()
        response_fid = new_uuid()
        feature_config = {
            "thinking_enabled": thinking,
            "output_schema": "phase",
            "research_mode": "normal",
            "auto_thinking": thinking,
            "thinking_mode": "Auto" if thinking else "Manual",
            "thinking_format": "summary",
            "auto_search": search,
        }
        meta: dict = {"subChatType": chat_type}
        if image_model:
            meta["model"] = image_model
        message = {
            "id": None,
            "fid": user_fid,
            "parentId": parent_message_id,
            "childrenIds": [response_fid],
            "role": "user",
            "content": prompt,
            "user_action": "chat",
            "files": [],
            "timestamp": ts,
            "models": [model],
            "model": "",
            "chat_type": chat_type,
            "feature_config": feature_config,
            "extra": {"meta": meta},
            "sub_chat_type": chat_type,
            "parent_id": parent_message_id,
        }
        body = {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chatId": chat_session_id,
            "parentId": parent_message_id or "",
            "chat_id": chat_session_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": parent_message_id,
            "messages": [message],
            "timestamp": ts,
        }
        # Qwen reads the image aspect ratio from the TOP level of the body, not
        # from extra.meta: verified live - size at the top level makes 2:3 give
        # 1696x2528, while the same value in meta.size is ignored (2048x2048).
        if image_size:
            body["size"] = image_size
        headers = {
            "Accept": "text/event-stream",
            "X-Accel-Buffering": "no",
            "Referer": f"{BASE_URL}/c/{chat_session_id}",
        }
        await self.ensure_access_token()
        req = self.http.build_request(
            "POST",
            "/api/v2/chat/completions",
            params={"chat_id": chat_session_id},
            json=body,
            headers=self._request_headers(headers),
        )
        resp = await self.http.send(req, stream=True)
        if resp.status_code == 401:
            # A revoked-but-not-yet-expired token only shows up here; refresh and
            # retry once so the caller sees a normal stream instead of a 401.
            await resp.aclose()
            await self.refresh_access_token()
            req = self.http.build_request(
                "POST",
                "/api/v2/chat/completions",
                params={"chat_id": chat_session_id},
                json=body,
                headers=self._request_headers(headers),
            )
            resp = await self.http.send(req, stream=True)
        return resp

    async def stop_stream(self, chat_session_id: str, response_id: str | None) -> None:
        await self._post(
            "/api/v2/chat/completions/stop",
            {"chat_id": chat_session_id, "response_id": response_id},
        )
