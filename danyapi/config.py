from __future__ import annotations

import os
from pathlib import Path


def _noop_load_dotenv(*args, **kwargs) -> bool:
    return False


try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = _noop_load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)


class Settings:
    def __init__(self) -> None:
        self.host = os.environ.get("DANYAPI_HOST", "0.0.0.0")
        try:
            self.port = int(os.environ.get("DANYAPI_PORT", "8000"))
        except ValueError:
            self.port = 8000
        tokens = [t.strip() for t in os.environ.get("DEEPSEEK_TOKENS", "").split(",") if t.strip()]
        single = os.environ.get("DEEPSEEK_TOKEN", "").strip()
        if not tokens and single:
            tokens = [single]
        self.deepseek_tokens = tokens
        self.deepseek_email = os.environ.get("DEEPSEEK_EMAIL", "")
        self.deepseek_password = os.environ.get("DEEPSEEK_PASSWORD", "")
        qwen_tokens = [t.strip() for t in os.environ.get("QWEN_TOKENS", "").split(",") if t.strip()]
        qwen_single = os.environ.get("QWEN_TOKEN", "").strip()
        if not qwen_tokens and qwen_single:
            qwen_tokens = [qwen_single]
        self.qwen_tokens = qwen_tokens
        self.qwen_email = os.environ.get("QWEN_EMAIL", "")
        self.qwen_password = os.environ.get("QWEN_PASSWORD", "")
        zai_tokens = [t.strip() for t in os.environ.get("ZAI_TOKENS", "").split(",") if t.strip()]
        zai_single = os.environ.get("ZAI_TOKEN", "").strip()
        if not zai_tokens and zai_single:
            zai_tokens = [zai_single]
        self.zai_tokens = zai_tokens
        self.zai_email = os.environ.get("ZAI_EMAIL", "")
        self.zai_password = os.environ.get("ZAI_PASSWORD", "")
        # The z.ai provider authenticates through a persistent Chrome profile by
        # default, so it needs no token at all. Tokens are optional (injected as
        # a cookie when present); DANYAPI_ZAI_ENABLED turns the provider on for
        # profile-only setups.
        self.zai_enabled = bool(zai_tokens) or os.environ.get("DANYAPI_ZAI_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        try:
            self.timeout = float(os.environ.get("DANYAPI_TIMEOUT", "60"))
        except ValueError:
            self.timeout = 60.0
        raw_acquire = os.environ.get("DANYAPI_ACQUIRE_TIMEOUT", "")
        try:
            self.acquire_timeout = float(raw_acquire) if raw_acquire else None
        except ValueError:
            self.acquire_timeout = None
        raw_cache = os.environ.get("DANYAPI_SESSION_CACHE_SIZE", "128")
        try:
            self.session_cache_size = int(raw_cache)
        except ValueError:
            self.session_cache_size = 128
        raw_ttl = os.environ.get("DANYAPI_SESSION_TTL_SECONDS", "3600")
        try:
            self.session_ttl = float(raw_ttl)
        except ValueError:
            self.session_ttl = 3600.0
        raw_level = os.environ.get("DANYAPI_LOG_LEVEL", "INFO").strip()
        self.log_level = raw_level or "INFO"
        self.log_file = os.environ.get("DANYAPI_LOG_FILE", "").strip()
        try:
            self.log_max_bytes = int(os.environ.get("DANYAPI_LOG_MAX_BYTES", "10485760"))
        except ValueError:
            self.log_max_bytes = 10 * 1024 * 1024
        try:
            self.log_backup_count = int(os.environ.get("DANYAPI_LOG_BACKUP_COUNT", "3"))
        except ValueError:
            self.log_backup_count = 3
        self.cache_dir = os.environ.get("DANYAPI_CACHE_DIR", "").strip()
        self.cache_enabled = os.environ.get("DANYAPI_CACHE_DISABLED", "").strip().lower() not in ("1", "true", "yes", "on")
        try:
            self.human_delay_min = float(os.environ.get("DANYAPI_HUMAN_DELAY_MIN", "0.5"))
        except ValueError:
            self.human_delay_min = 0.5
        try:
            self.human_delay_max = float(os.environ.get("DANYAPI_HUMAN_DELAY_MAX", "3.0"))
        except ValueError:
            self.human_delay_max = 3.0

        # Minimum spacing between consecutive requests to one account. Guards
        # against DeepSeek's per-account throttle, which triggers on short bursts
        # of small requests. 0 disables pacing entirely.
        try:
            self.min_request_interval = max(0.0, float(os.environ.get("DANYAPI_MIN_REQUEST_INTERVAL", "4.0")))
        except ValueError:
            self.min_request_interval = 4.0

        # How many requests may start back-to-back before min_request_interval
        # applies: burst=4 means "4 requests, then one 4 s gap, then 4 more".
        # 1 degenerates into a plain minimum interval.
        try:
            self.request_burst = max(1, int(os.environ.get("DANYAPI_REQUEST_BURST", "4")))
        except ValueError:
            self.request_burst = 4

        # Minimum seconds between consecutive isolated (single) request starts on
        # the same account. Guards against frequent single-shot requests with
        # small gaps that individually look harmless but together trip throttling.
        # 0 disables.
        try:
            self.min_start_gap = max(0.0, float(os.environ.get("DANYAPI_MIN_START_GAP", "0")))
        except ValueError:
            self.min_start_gap = 0.0

        # Minimum seconds between the END of one request and the START of the
        # next on the same account. Start-to-start pacing misses this: a slow
        # request (~80 s) makes consecutive starts look far apart while the
        # account is re-hit seconds after each stream closes, which is exactly
        # what trips DeepSeek's "Messages too frequent" throttle. 0 disables.
        try:
            self.min_end_gap = max(0.0, float(os.environ.get("DANYAPI_MIN_END_GAP", "5.0")))
        except ValueError:
            self.min_end_gap = 5.0

        # Token usage accounting: totals, per-model/provider/user breakdowns and
        # a bounded recent-request list, persisted through JsonStore.
        self.usage_enabled = os.environ.get("DANYAPI_USAGE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
        try:
            self.usage_max_records = max(1, int(os.environ.get("DANYAPI_USAGE_MAX_RECORDS", "1000")))
        except ValueError:
            self.usage_max_records = 1000

        # Diagnostic NDJSON tool-call trace log (empty path = disabled).
        default_trace_dir = str(Path(self.log_file).parent) if self.log_file else "."
        self.tooltrace_file = os.environ.get("DANYAPI_TOOLTRACE_FILE", f"{default_trace_dir}/tooltrace.ndjson").strip()
        try:
            self.tooltrace_max_bytes = int(os.environ.get("DANYAPI_TOOLTRACE_MAX_BYTES", str(50 * 1024 * 1024)))
        except ValueError:
            self.tooltrace_max_bytes = 50 * 1024 * 1024
        try:
            self.tooltrace_backup_count = int(os.environ.get("DANYAPI_TOOLTRACE_BACKUP_COUNT", "10"))
        except ValueError:
            self.tooltrace_backup_count = 10


settings = Settings()
