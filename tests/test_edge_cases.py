import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import danyapi.config as cfg
import danyapi.store as store_mod
from danyapi.deepseek.sse import SSEEvent
from danyapi.deepseek.stream import MessageReconstructor, _set_path
from danyapi.qwen.accounts import QwenAccount
from danyapi.qwen.client import QwenClient
from danyapi.qwen.stream import QwenStreamReconstructor


def _settings(env):
    with patch.dict("os.environ", env, clear=True):
        return cfg.Settings()


def test_dotenv_missing_fallback():
    import sys

    orig_settings = cfg.settings
    orig_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "dotenv":
            raise ImportError("no dotenv")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        importlib.reload(cfg)
    assert cfg.load_dotenv is cfg._noop_load_dotenv
    assert not cfg._noop_load_dotenv()
    importlib.reload(cfg)
    cfg.settings = orig_settings
    assert sys.modules["danyapi.config"].settings is orig_settings


def test_human_delay_min_parse():
    assert _settings({"DANYAPI_HUMAN_DELAY_MIN": "bad"}).human_delay_min == 0.5
    assert _settings({"DANYAPI_HUMAN_DELAY_MIN": "1.5"}).human_delay_min == 1.5


def test_human_delay_max_parse():
    assert _settings({"DANYAPI_HUMAN_DELAY_MAX": "bad"}).human_delay_max == 3.0
    assert _settings({"DANYAPI_HUMAN_DELAY_MAX": "7"}).human_delay_max == 7.0


def test_min_request_interval_parse():
    # Default is 4 s when unset, and bad input falls back to the same default.
    assert _settings({}).min_request_interval == 4.0
    assert _settings({"DANYAPI_MIN_REQUEST_INTERVAL": "bad"}).min_request_interval == 4.0
    assert _settings({"DANYAPI_MIN_REQUEST_INTERVAL": "2.5"}).min_request_interval == 2.5
    # Explicitly disabling pacing and negative input both clamp to 0.
    assert _settings({"DANYAPI_MIN_REQUEST_INTERVAL": "0"}).min_request_interval == 0.0
    assert _settings({"DANYAPI_MIN_REQUEST_INTERVAL": "-5"}).min_request_interval == 0.0


def test_request_burst_parse():
    assert _settings({}).request_burst == 4
    assert _settings({"DANYAPI_REQUEST_BURST": "bad"}).request_burst == 4
    assert _settings({"DANYAPI_REQUEST_BURST": "8"}).request_burst == 8
    # Zero and negative values clamp to a single request per cycle.
    assert _settings({"DANYAPI_REQUEST_BURST": "0"}).request_burst == 1
    assert _settings({"DANYAPI_REQUEST_BURST": "-3"}).request_burst == 1


def test_min_start_gap_parse():
    assert _settings({}).min_start_gap == 0.0
    assert _settings({"DANYAPI_MIN_START_GAP": "bad"}).min_start_gap == 0.0
    assert _settings({"DANYAPI_MIN_START_GAP": "2.5"}).min_start_gap == 2.5
    # Negative values clamp to zero (disabled).
    assert _settings({"DANYAPI_MIN_START_GAP": "-1"}).min_start_gap == 0.0


def test_min_end_gap_parse():
    # Default is 5 s when unset, and bad input falls back to the same default.
    assert _settings({}).min_end_gap == 5.0
    assert _settings({"DANYAPI_MIN_END_GAP": "bad"}).min_end_gap == 5.0
    assert _settings({"DANYAPI_MIN_END_GAP": "2.5"}).min_end_gap == 2.5
    # Explicitly disabling and negative input both clamp to 0.
    assert _settings({"DANYAPI_MIN_END_GAP": "0"}).min_end_gap == 0.0
    assert _settings({"DANYAPI_MIN_END_GAP": "-1"}).min_end_gap == 0.0


def test_cache_root_mkdir_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    blocker = Path(tmp_path) / "blocker"
    blocker.write_text("file", encoding="utf-8")
    store_mod.settings.cache_dir = str(blocker)
    root = store_mod.cache_root()
    assert root == blocker


def test_load_skips_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    store = store_mod.JsonStore("n", None)
    store._load()
    assert store._data == {}


def test_write_error_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    store = store_mod.JsonStore("w", "default")
    with patch("danyapi.store.os.replace", side_effect=OSError("denied")):
        store.set("k", "v")
    assert store.get("k") == "v"


def test_mark_broken_and_label():
    acct = QwenAccount(3, MagicMock(spec=QwenClient))
    assert not acct.broken
    assert acct.label == "qwen-acct#3"
    acct.mark_broken()
    assert acct.broken
    acct.mark_broken()
    assert acct.broken


def test_mark_quota_exhausted_blocks_then_recovers():
    acct = QwenAccount(0, MagicMock(spec=QwenClient))
    assert acct.is_available()
    acct.mark_quota_exhausted(3600.0)
    assert acct.broken
    assert not acct.is_available()
    # Simulate the cooldown elapsing: a past deadline lets the account return.
    acct.quota_until = 1.0
    assert acct.is_available()
    assert not acct.broken
    assert acct.quota_until == 0.0


def test_is_available_expired_cooldown_resets():
    acct = QwenAccount(0, MagicMock(spec=QwenClient))
    acct.mark_quota_exhausted(3600.0)
    acct.quota_until = 1.0  # far in the past on the monotonic clock
    assert acct.is_available()
    assert acct.quota_until == 0.0


def test_mark_broken_stays_unavailable_forever():
    acct = QwenAccount(0, MagicMock(spec=QwenClient))
    acct.mark_broken()
    # No quota deadline is set, so an auth-broken account never returns.
    assert acct.quota_until == 0.0
    assert not acct.is_available()


def test_sticky_image_chat_memory_only():
    acct = QwenAccount(0, MagicMock(spec=QwenClient))
    assert acct.sticky_image_chat("qwen-image-2.0-pro") is None
    acct.set_sticky_image_chat("qwen-image-2.0-pro", "chat-a")
    assert acct.sticky_image_chat("qwen-image-2.0-pro") == "chat-a"
    # Different model keeps its own pinned chat.
    assert acct.sticky_image_chat("qwen-image-3.0-pro") is None
    acct.set_sticky_image_chat("qwen-image-3.0-pro", "chat-b")
    assert acct.sticky_image_chat("qwen-image-2.0-pro") == "chat-a"
    assert acct.sticky_image_chat("qwen-image-3.0-pro") == "chat-b"
    acct.forget_sticky_image_chat("qwen-image-2.0-pro")
    assert acct.sticky_image_chat("qwen-image-2.0-pro") is None


def test_sticky_image_chat_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    store = store_mod.JsonStore("qwen-sticky", "default")
    acct = QwenAccount(0, MagicMock(spec=QwenClient), sticky_store=store)
    acct.set_sticky_image_chat("qwen-image-2.0-pro", "chat-x")
    # A fresh account (restart) reads the pinned chat back from disk.
    acct2 = QwenAccount(0, MagicMock(spec=QwenClient), sticky_store=store_mod.JsonStore("qwen-sticky", "default"))
    assert acct2.sticky_image_chat("qwen-image-2.0-pro") == "chat-x"
    acct2.forget_sticky_image_chat("qwen-image-2.0-pro")
    acct3 = QwenAccount(0, MagicMock(spec=QwenClient), sticky_store=store_mod.JsonStore("qwen-sticky", "default"))
    assert acct3.sticky_image_chat("qwen-image-2.0-pro") is None


def test_sticky_image_chat_ignores_empty_and_bad_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    store = store_mod.JsonStore("qwen-sticky-bad", "default")
    store.set("0:qwen-image-2.0-pro", 123)
    acct = QwenAccount(0, MagicMock(spec=QwenClient), sticky_store=store)
    assert acct.sticky_image_chat("qwen-image-2.0-pro") is None
    acct.set_sticky_image_chat("qwen-image-2.0-pro", "")
    assert acct.sticky_image_chat("qwen-image-2.0-pro") is None


def test_set_path_through_scalar():
    target = {"a": [5]}
    _set_path(target, ["a", "0", "b"], 1)
    assert target == {"a": [5]}


def test_append_negative_index_out_of_range():
    rec = MessageReconstructor()
    rec.message = {"fragments": [{"content": "x"}]}
    rec.handle(SSEEvent(None, {"p": "response/fragments/-5/content", "o": "APPEND", "v": "y"}))
    assert rec.message["fragments"][0]["content"] == "x"


def test_append_replaces_non_str_value():
    rec = MessageReconstructor()
    rec.message = {"fragments": [{"content": 5}]}
    rec.handle(SSEEvent(None, {"p": "response/fragments/0/content", "o": "APPEND", "v": "y"}))
    assert rec.message["fragments"][0]["content"] == "y"


def test_ready_non_dict():
    rec = MessageReconstructor()
    rec.handle(SSEEvent("ready", "garbage"))
    assert rec.response_message_id is None


def test_delta_ignored_event():
    rec = MessageReconstructor()
    rec.handle(SSEEvent("close", {"v": 1}))
    assert rec.message == {}


def test_handle_non_dict_data():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, "text"))
    assert rec.content == ""


def test_handle_error_non_dict():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, {"error": "plain"}))
    assert rec.error is not None
    assert rec.error["code"] == "Internal_Server_Error"
    assert rec.error["details"] == "plain"


def test_handle_choices_empty():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, {"choices": []}))
    assert rec.content == ""


def test_handle_delta_not_dict():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, {"choices": [{"delta": "x"}]}))
    assert rec.content == ""


def test_handle_unknown_phase():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, {"choices": [{"delta": {"phase": "other", "content": "x"}}]}))
    assert rec.content == ""


def test_handle_error_dict():
    rec = QwenStreamReconstructor()
    rec.handle(SSEEvent(None, {"error": {"code": "c", "details": "d"}}))
    assert rec.error == {"code": "c", "details": "d"}


def test_guard_executes_main():
    import runpy

    import danyapi.__main__ as main_mod

    with patch.object(main_mod.uvicorn, "run") as run:
        runpy.run_module("danyapi.__main__", run_name="__main__")
        run.assert_called_once()
