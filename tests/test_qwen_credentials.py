from __future__ import annotations

import base64
import json

from danyapi.qwen.accounts import (
    _token_fingerprint,
    _token_issued_at,
    load_effective_token,
    persist_rotated_token,
)
from danyapi.store import JsonStore


def make_jwt(iat: int | None = None, **claims) -> str:
    """A decodable JWT carrying ``iat`` (and whatever else is asked for)."""
    payload = dict(claims)
    if iat is not None:
        payload["iat"] = iat
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{encoded}.s"


def test_load_returns_configured_when_store_empty():
    store = JsonStore("qwen-credentials", None)
    assert load_effective_token("configured", store) == "configured"


def test_load_returns_configured_when_source_differs():
    store = JsonStore("qwen-credentials", None)
    store.set("source", _token_fingerprint("someone-else"))
    store.set("token", "rotated")
    assert load_effective_token("configured", store) == "configured"


def test_load_returns_rotated_for_matching_source():
    store = JsonStore("qwen-credentials", None)
    persist_rotated_token("configured", store, "rotated")
    assert load_effective_token("configured", store) == "rotated"


def test_load_handles_none_store():
    assert load_effective_token("configured", None) == "configured"


def test_persist_handles_none_store():
    persist_rotated_token("configured", None, "rotated")


def test_persist_then_reload_round_trip():
    store = JsonStore("qwen-credentials", None)
    persist_rotated_token("env-token", store, "rotated-token")
    assert store.get(_token_fingerprint("env-token")) == "rotated-token"
    assert load_effective_token("env-token", store) == "rotated-token"


def test_multiple_accounts_persist_independently():
    store = JsonStore("qwen-credentials", None)
    persist_rotated_token("env-token-a", store, "rotated-a")
    persist_rotated_token("env-token-b", store, "rotated-b")
    # A second account's rotation must not clobber the first one's entry.
    assert load_effective_token("env-token-a", store) == "rotated-a"
    assert load_effective_token("env-token-b", store) == "rotated-b"


def test_load_uses_legacy_single_slot():
    store = JsonStore("qwen-credentials", None)
    store.set("source", _token_fingerprint("env-token"))
    store.set("token", "rotated-legacy")
    assert load_effective_token("env-token", store) == "rotated-legacy"


def test_load_ignores_non_string_stored_token():
    store = JsonStore("qwen-credentials", None)
    store.set("source", _token_fingerprint("configured"))
    store.set("token", 123)
    assert load_effective_token("configured", store) == "configured"


def test_token_fingerprint_is_stable_and_distinct():
    assert _token_fingerprint("abc") == _token_fingerprint("abc")
    assert _token_fingerprint("abc") != _token_fingerprint("abd")


# ---------------------------------------------------------------------------
# iat ordering: the store must never be rolled back to a revoked credential
# ---------------------------------------------------------------------------


def test_token_issued_at_reads_iat():
    assert _token_issued_at(make_jwt(iat=1700000000)) == 1700000000.0


def test_token_issued_at_handles_missing_and_garbage():
    assert _token_issued_at(None) is None
    assert _token_issued_at("") is None
    assert _token_issued_at("not-a-jwt") is None
    assert _token_issued_at(make_jwt()) is None
    assert _token_issued_at(make_jwt(iat="later")) is None


def test_persist_keeps_the_newer_stored_credential():
    """The older transport must not resurrect a token the server revoked."""
    store = JsonStore("qwen-credentials", None)
    newer = make_jwt(iat=2000)
    older = make_jwt(iat=1000)
    persist_rotated_token("configured", store, newer)

    persist_rotated_token("configured", store, older)

    assert store.get(_token_fingerprint("configured")) == newer


def test_persist_keeps_an_equally_old_stored_credential():
    store = JsonStore("qwen-credentials", None)
    first = make_jwt(iat=1000)
    second = make_jwt(iat=1000)
    persist_rotated_token("configured", store, first)

    persist_rotated_token("configured", store, second)

    assert store.get(_token_fingerprint("configured")) == first


def test_persist_overwrites_with_a_fresher_credential():
    store = JsonStore("qwen-credentials", None)
    older = make_jwt(iat=1000)
    newer = make_jwt(iat=2000)
    persist_rotated_token("configured", store, older)

    persist_rotated_token("configured", store, newer)

    assert store.get(_token_fingerprint("configured")) == newer


def test_persist_writes_when_iat_is_incomparable():
    """Legacy opaque credentials have no iat: last write wins, as before."""
    store = JsonStore("qwen-credentials", None)
    persist_rotated_token("configured", store, "opaque-old")
    persist_rotated_token("configured", store, "opaque-new")
    assert store.get(_token_fingerprint("configured")) == "opaque-new"


def test_persist_rewrites_the_same_value():
    store = JsonStore("qwen-credentials", None)
    token = make_jwt(iat=1000)
    persist_rotated_token("configured", store, token)
    persist_rotated_token("configured", store, token)
    assert store.get(_token_fingerprint("configured")) == token


def test_persist_ignores_a_non_string_slot():
    store = JsonStore("qwen-credentials", None)
    store.set(_token_fingerprint("configured"), 42)
    persist_rotated_token("configured", store, make_jwt(iat=1000))
    assert load_effective_token("configured", store) == make_jwt(iat=1000)
