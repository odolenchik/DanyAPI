from __future__ import annotations

from danyapi.qwen.accounts import (
    _token_fingerprint,
    load_effective_token,
    persist_rotated_token,
)
from danyapi.store import JsonStore


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
