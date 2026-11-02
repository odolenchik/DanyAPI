import os

import pytest

from danyapi.config import Settings


@pytest.fixture
def settings_for():
    def _make(env):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "environ", env, raising=False)
            return Settings()

    return _make


def test_defaults(settings_for):
    s = settings_for({})
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.deepseek_tokens == []
    assert s.qwen_tokens == []
    assert s.deepseek_email == ""
    assert s.deepseek_password == ""
    assert s.qwen_email == ""
    assert s.qwen_password == ""
    assert s.timeout == 60.0
    assert s.acquire_timeout is None
    assert s.session_cache_size == 128
    assert s.session_ttl == 3600.0
    assert s.qwen_quota_cooldown == 86400.0
    assert s.log_level == "INFO"
    assert s.log_file == ""
    assert s.log_max_bytes == 10485760
    assert s.log_backup_count == 3
    assert s.cache_dir == ""
    assert s.cache_enabled


def test_invalid_port_falls_back(settings_for):
    assert settings_for({"DANYAPI_PORT": "abc"}).port == 8000
    assert settings_for({"DANYAPI_PORT": "9000"}).port == 9000


def test_invalid_timeout_falls_back(settings_for):
    assert settings_for({"DANYAPI_TIMEOUT": "xyz"}).timeout == 60.0
    assert settings_for({"DANYAPI_TIMEOUT": "12.5"}).timeout == 12.5


def test_acquire_timeout(settings_for):
    assert settings_for({"DANYAPI_ACQUIRE_TIMEOUT": ""}).acquire_timeout is None
    assert settings_for({"DANYAPI_ACQUIRE_TIMEOUT": "bad"}).acquire_timeout is None
    assert settings_for({"DANYAPI_ACQUIRE_TIMEOUT": "3"}).acquire_timeout == 3.0


def test_invalid_cache_size_falls_back(settings_for):
    assert settings_for({"DANYAPI_SESSION_CACHE_SIZE": "bad"}).session_cache_size == 128
    assert settings_for({"DANYAPI_SESSION_CACHE_SIZE": "7"}).session_cache_size == 7


def test_invalid_ttl_falls_back(settings_for):
    assert settings_for({"DANYAPI_SESSION_TTL_SECONDS": "bad"}).session_ttl == 3600.0
    assert settings_for({"DANYAPI_SESSION_TTL_SECONDS": "42"}).session_ttl == 42.0


def test_qwen_quota_cooldown(settings_for):
    assert settings_for({"DANYAPI_QWEN_QUOTA_COOLDOWN": "bad"}).qwen_quota_cooldown == 86400.0
    assert settings_for({"DANYAPI_QWEN_QUOTA_COOLDOWN": "120"}).qwen_quota_cooldown == 120.0


def test_log_level_empty_uses_info(settings_for):
    assert settings_for({"DANYAPI_LOG_LEVEL": ""}).log_level == "INFO"
    assert settings_for({"DANYAPI_LOG_LEVEL": "DEBUG"}).log_level == "DEBUG"


def test_invalid_log_bytes_falls_back(settings_for):
    assert settings_for({"DANYAPI_LOG_MAX_BYTES": "bad"}).log_max_bytes == 10485760
    assert settings_for({"DANYAPI_LOG_MAX_BYTES": "2048"}).log_max_bytes == 2048


def test_invalid_log_backup_falls_back(settings_for):
    assert settings_for({"DANYAPI_LOG_BACKUP_COUNT": "bad"}).log_backup_count == 3
    assert settings_for({"DANYAPI_LOG_BACKUP_COUNT": "5"}).log_backup_count == 5


def test_deepseek_tokens_comma(settings_for):
    s = settings_for({"DEEPSEEK_TOKENS": "a, b , c"})
    assert s.deepseek_tokens == ["a", "b", "c"]


def test_deepseek_single_token_fallback(settings_for):
    assert settings_for({"DEEPSEEK_TOKEN": "solo"}).deepseek_tokens == ["solo"]


def test_deepseek_tokens_win_over_single(settings_for):
    s = settings_for({"DEEPSEEK_TOKENS": "a,b", "DEEPSEEK_TOKEN": "solo"})
    assert s.deepseek_tokens == ["a", "b"]


def test_qwen_tokens_comma(settings_for):
    assert settings_for({"QWEN_TOKENS": "x , y"}).qwen_tokens == ["x", "y"]


def test_qwen_single_token_fallback(settings_for):
    assert settings_for({"QWEN_TOKEN": "only"}).qwen_tokens == ["only"]


def test_zai_tokens_comma(settings_for):
    assert settings_for({"ZAI_TOKENS": "a , b"}).zai_tokens == ["a", "b"]


def test_zai_single_token_fallback(settings_for):
    assert settings_for({"ZAI_TOKEN": "solo"}).zai_tokens == ["solo"]


def test_zai_disabled_by_default(settings_for):
    assert settings_for({}).zai_enabled is False


def test_zai_enabled_by_tokens(settings_for):
    assert settings_for({"ZAI_TOKENS": "a"}).zai_enabled is True


def test_zai_enabled_by_single_token(settings_for):
    assert settings_for({"ZAI_TOKEN": "solo"}).zai_enabled is True


def test_zai_enabled_without_token(settings_for):
    # profile-only auth: no token, provider turned on explicitly
    assert settings_for({"DANYAPI_ZAI_ENABLED": "1"}).zai_enabled is True
    assert settings_for({"DANYAPI_ZAI_ENABLED": "true"}).zai_enabled is True


def test_zai_disabled_flag_values(settings_for):
    assert settings_for({"DANYAPI_ZAI_ENABLED": "0"}).zai_enabled is False
    assert settings_for({"DANYAPI_ZAI_ENABLED": "off"}).zai_enabled is False
    assert settings_for({"DANYAPI_ZAI_ENABLED": ""}).zai_enabled is False


def test_empty_tokens(settings_for):
    s = settings_for({"DEEPSEEK_TOKENS": " , , ", "QWEN_TOKENS": "", "ZAI_TOKENS": ""})
    assert s.deepseek_tokens == []
    assert s.qwen_tokens == []
    assert s.zai_tokens == []


def test_cache_dir(settings_for):
    assert settings_for({"DANYAPI_CACHE_DIR": "C:/tmp/cache"}).cache_dir == "C:/tmp/cache"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_cache_disabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_CACHE_DISABLED": value}).cache_enabled is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_cache_enabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_CACHE_DISABLED": value}).cache_enabled is True


def test_toolcall_retry_off_by_default(settings_for):
    assert settings_for({}).toolcall_retry is False
    assert settings_for({"DANYAPI_TOOLCALL_RETRY": ""}).toolcall_retry is False
    assert settings_for({"DANYAPI_TOOLCALL_RETRY": "0"}).toolcall_retry is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_toolcall_retry_enabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_TOOLCALL_RETRY": value}).toolcall_retry is True


def test_one_tool_call_per_turn_on_by_default(settings_for):
    assert settings_for({}).one_tool_call_per_turn is True
    assert settings_for({"DANYAPI_ONE_TOOL_CALL_PER_TURN": ""}).one_tool_call_per_turn is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
def test_one_tool_call_per_turn_disabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_ONE_TOOL_CALL_PER_TURN": value}).one_tool_call_per_turn is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_one_tool_call_per_turn_enabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_ONE_TOOL_CALL_PER_TURN": value}).one_tool_call_per_turn is True


def test_degenerate_guard_on_by_default(settings_for):
    assert settings_for({}).degenerate_guard is True
    assert settings_for({"DANYAPI_DEGENERATE_GUARD": ""}).degenerate_guard is True
    assert settings_for({"DANYAPI_DEGENERATE_GUARD": "1"}).degenerate_guard is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_degenerate_guard_disabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_DEGENERATE_GUARD": value}).degenerate_guard is False


def test_degenerate_threshold_default_and_parse(settings_for):
    assert settings_for({}).degenerate_threshold == 20
    assert settings_for({"DANYAPI_DEGENERATE_THRESHOLD": "35"}).degenerate_threshold == 35
    # Non-numeric falls back to the default; below-minimum is clamped to 2.
    assert settings_for({"DANYAPI_DEGENERATE_THRESHOLD": "abc"}).degenerate_threshold == 20
    assert settings_for({"DANYAPI_DEGENERATE_THRESHOLD": "0"}).degenerate_threshold == 2


def test_thinking_heuristic_off_by_default(settings_for):
    assert settings_for({}).thinking_heuristic is False
    assert settings_for({"DANYAPI_THINKING_HEURISTIC": ""}).thinking_heuristic is False
    assert settings_for({"DANYAPI_THINKING_HEURISTIC": "0"}).thinking_heuristic is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_thinking_heuristic_enabled_variants(settings_for, value):
    assert settings_for({"DANYAPI_THINKING_HEURISTIC": value}).thinking_heuristic is True


def test_thinking_thresholds_have_defaults(settings_for):
    s = settings_for({})
    assert s.thinking_min_prompt_chars == 2000
    assert s.thinking_min_messages == 8


def test_thinking_thresholds_read_env(settings_for):
    s = settings_for({"DANYAPI_THINKING_MIN_PROMPT_CHARS": "500", "DANYAPI_THINKING_MIN_MESSAGES": "3"})
    assert s.thinking_min_prompt_chars == 500
    assert s.thinking_min_messages == 3


def test_thinking_thresholds_fall_back_on_bad_values(settings_for):
    s = settings_for({"DANYAPI_THINKING_MIN_PROMPT_CHARS": "abc", "DANYAPI_THINKING_MIN_MESSAGES": "xyz"})
    assert s.thinking_min_prompt_chars == 2000
    assert s.thinking_min_messages == 8


def test_thinking_thresholds_floor_at_one(settings_for):
    s = settings_for({"DANYAPI_THINKING_MIN_PROMPT_CHARS": "0", "DANYAPI_THINKING_MIN_MESSAGES": "-5"})
    assert s.thinking_min_prompt_chars == 1
    assert s.thinking_min_messages == 1


def test_thinking_score_defaults(settings_for):
    s = settings_for({})
    assert s.thinking_strong_weight == 2
    assert s.thinking_weak_weight == 1
    assert s.thinking_score_threshold == 2


def test_thinking_score_read_env(settings_for):
    s = settings_for(
        {
            "DANYAPI_THINKING_STRONG_WEIGHT": "5",
            "DANYAPI_THINKING_WEAK_WEIGHT": "3",
            "DANYAPI_THINKING_SCORE_THRESHOLD": "7",
        }
    )
    assert s.thinking_strong_weight == 5
    assert s.thinking_weak_weight == 3
    assert s.thinking_score_threshold == 7


def test_thinking_score_falls_back_on_bad_values(settings_for):
    s = settings_for(
        {
            "DANYAPI_THINKING_STRONG_WEIGHT": "abc",
            "DANYAPI_THINKING_WEAK_WEIGHT": "",
            "DANYAPI_THINKING_SCORE_THRESHOLD": "1.5",
        }
    )
    assert s.thinking_strong_weight == 2
    assert s.thinking_weak_weight == 1
    assert s.thinking_score_threshold == 2


def test_thinking_score_floors_at_one(settings_for):
    s = settings_for(
        {
            "DANYAPI_THINKING_STRONG_WEIGHT": "0",
            "DANYAPI_THINKING_WEAK_WEIGHT": "-3",
            "DANYAPI_THINKING_SCORE_THRESHOLD": "0",
        }
    )
    assert s.thinking_strong_weight == 1
    assert s.thinking_weak_weight == 1
    assert s.thinking_score_threshold == 1


def test_deepseek_revive_defaults(settings_for):
    s = settings_for({})
    assert s.deepseek_revive_interval == 900.0
    assert s.deepseek_revive_slow_interval == 1800.0
    assert s.deepseek_revive_slow_after == 7200.0


def test_deepseek_revive_read_env(settings_for):
    s = settings_for(
        {
            "DANYAPI_DEEPSEEK_REVIVE_INTERVAL": "300",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL": "600",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER": "1200",
        }
    )
    assert s.deepseek_revive_interval == 300.0
    assert s.deepseek_revive_slow_interval == 600.0
    assert s.deepseek_revive_slow_after == 1200.0


def test_deepseek_revive_bad_values_fall_back(settings_for):
    s = settings_for(
        {
            "DANYAPI_DEEPSEEK_REVIVE_INTERVAL": "abc",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL": "",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER": "1.5.2",
        }
    )
    assert s.deepseek_revive_interval == 900.0
    assert s.deepseek_revive_slow_interval == 1800.0
    assert s.deepseek_revive_slow_after == 7200.0


def test_deepseek_revive_clamps_to_minimum(settings_for):
    s = settings_for(
        {
            "DANYAPI_DEEPSEEK_REVIVE_INTERVAL": "0",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL": "-5",
            "DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER": "1",
        }
    )
    assert s.deepseek_revive_interval == 60.0
    assert s.deepseek_revive_slow_interval == 60.0
    assert s.deepseek_revive_slow_after == 60.0


def test_qwen_revive_defaults(settings_for):
    s = settings_for({})
    assert s.qwen_revive_interval == 900.0
    assert s.qwen_revive_slow_interval == 1800.0
    assert s.qwen_revive_slow_after == 7200.0


def test_qwen_revive_read_env(settings_for):
    s = settings_for(
        {
            "DANYAPI_QWEN_REVIVE_INTERVAL": "120",
            "DANYAPI_QWEN_REVIVE_SLOW_INTERVAL": "600",
            "DANYAPI_QWEN_REVIVE_SLOW_AFTER": "3600",
        }
    )
    assert s.qwen_revive_interval == 120.0
    assert s.qwen_revive_slow_interval == 600.0
    assert s.qwen_revive_slow_after == 3600.0


def test_qwen_revive_bad_values_fall_back(settings_for):
    s = settings_for(
        {
            "DANYAPI_QWEN_REVIVE_INTERVAL": "abc",
            "DANYAPI_QWEN_REVIVE_SLOW_INTERVAL": "",
            "DANYAPI_QWEN_REVIVE_SLOW_AFTER": "1.5.2",
        }
    )
    assert s.qwen_revive_interval == 900.0
    assert s.qwen_revive_slow_interval == 1800.0
    assert s.qwen_revive_slow_after == 7200.0


def test_qwen_revive_clamps_to_minimum(settings_for):
    s = settings_for(
        {
            "DANYAPI_QWEN_REVIVE_INTERVAL": "0",
            "DANYAPI_QWEN_REVIVE_SLOW_INTERVAL": "-5",
            "DANYAPI_QWEN_REVIVE_SLOW_AFTER": "1",
        }
    )
    assert s.qwen_revive_interval == 60.0
    assert s.qwen_revive_slow_interval == 60.0
    assert s.qwen_revive_slow_after == 60.0


def test_qwen_revive_is_independent_of_deepseek(settings_for):
    s = settings_for(
        {
            "DANYAPI_DEEPSEEK_REVIVE_INTERVAL": "300",
            "DANYAPI_QWEN_REVIVE_INTERVAL": "120",
        }
    )
    assert s.deepseek_revive_interval == 300.0
    assert s.qwen_revive_interval == 120.0
