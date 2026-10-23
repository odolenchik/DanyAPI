from __future__ import annotations

import os
from pathlib import Path


def _noop_load_dotenv(*args, **kwargs) -> bool:
    return False


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a float env var, falling back to ``default`` when unset or bad.

    Non-numeric input never raises: an unusable value keeps the default so a
    typo cannot take the server down. Values below ``minimum`` are clamped.
    """
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


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
        # How long a Qwen account is dropped from rotation after it reports its
        # daily quota is used up. Qwen resets the image quota daily, so the
        # default cooldown is one day; the account returns on its own after it.
        raw_quota = os.environ.get("DANYAPI_QWEN_QUOTA_COOLDOWN", "86400")
        try:
            self.qwen_quota_cooldown = float(raw_quota)
        except ValueError:
            self.qwen_quota_cooldown = 86400.0

        # Pacing for Qwen accounts. Unlike DeepSeek, Qwen has no per-account
        # throttle hint to react to: it punishes a client silently with a
        # captcha (``RGV587_ERROR``), which then sticks to the account for
        # hours. The only defence is to never drive an account hard in the
        # first place, so requests go in short bursts with a jittered pause
        # between them and a long cooldown after each burst.
        #
        # ``qwen_burst`` requests may start back-to-back; each start is spaced
        # by ``qwen_min_interval`` seconds plus jitter, and after the burst the
        # account rests for ``qwen_burst_pause`` seconds plus jitter. 0 disables
        # pacing entirely.
        self.qwen_burst = self._int_env("DANYAPI_QWEN_BURST", 4, minimum=1)
        self.qwen_min_interval = _float_env("DANYAPI_QWEN_MIN_INTERVAL", 3.0)
        self.qwen_min_interval_jitter = _float_env("DANYAPI_QWEN_MIN_INTERVAL_JITTER", 4.0)
        self.qwen_burst_pause = _float_env("DANYAPI_QWEN_BURST_PAUSE", 360.0)
        self.qwen_burst_pause_jitter = _float_env("DANYAPI_QWEN_BURST_PAUSE_JITTER", 240.0)
        # Pacing for the browser transport, which has a different shape: one
        # request already takes 30-70 s because the page is typed into and the
        # answer is rendered, so a burst rule would make the pipeline starve.
        # What a human does between two messages is a short think time, and that
        # is exactly what is applied here - an even jittered gap before every
        # request, no long rests.
        self.qwen_browser_interval = _float_env("DANYAPI_QWEN_BROWSER_INTERVAL", 20.0)
        self.qwen_browser_interval_jitter = _float_env("DANYAPI_QWEN_BROWSER_INTERVAL_JITTER", 20.0)
        # How long an account rests after Qwen answers with an anti-bot captcha.
        # Only a human can clear the challenge, so the account stays out of
        # rotation; retrying just feeds the risk score. It returns on its own
        # once the cooldown elapses, in case the captcha was solved manually.
        self.qwen_captcha_cooldown = _float_env("DANYAPI_QWEN_CAPTCHA_COOLDOWN", 3600.0, minimum=60.0)

        # Image generation through a real browser. Qwen's anti-bot wall rejects
        # every plain HTTP image request, so the image path drives the actual web
        # UI: the SPA signs the request and the browser carries the state the WAF
        # bound to it. Text keeps using the fast HTTP client.
        self.qwen_browser = os.environ.get("DANYAPI_QWEN_BROWSER", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        # How long one browser request may take. Image generation is slower than
        # a text call: the prompt is typed, rendered and only then answered.
        self.qwen_browser_timeout = _float_env("DANYAPI_QWEN_BROWSER_TIMEOUT", 240.0, minimum=30.0)
        # How long to keep the window open for a human to clear a captcha. 0
        # fails fast and reports the captcha instead of waiting.
        self.qwen_captcha_wait = _float_env("DANYAPI_QWEN_CAPTCHA_WAIT", 0.0)
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

        # Retry a tool-mode reply whose tool call failed to parse at all. Off by
        # default: for tool_choice="auto" an unparsed reply is usually ordinary
        # prose, and every retry costs an upstream request (PoW + tokens). When
        # enabled, only replies the classifier judged json_malformed are retried.
        self.toolcall_retry = os.environ.get("DANYAPI_TOOLCALL_RETRY", "").strip().lower() in ("1", "true", "yes", "on")

        # Abort a DeepSeek stream that has collapsed into runaway repetition of
        # the empty DSML ``calls`` container (a long multi-call turn can lose EOS
        # and repeat it hundreds of times until the output limit). The valid call
        # payload, if any, is already in the buffer, so the tail is pure waste.
        # On by default: the signal is structural (dozens of bare containers),
        # not probabilistic, so a legitimate answer is never truncated. Set to 0
        # to only log the condition without aborting.
        self.degenerate_guard = os.environ.get("DANYAPI_DEGENERATE_GUARD", "1").strip().lower() not in ("0", "false", "no", "off")
        self.degenerate_threshold = self._int_env("DANYAPI_DEGENERATE_THRESHOLD", 20, minimum=2)

        # Stop a turn that keeps emitting the SAME tool call round after round.
        # The degenerate guard above only sees a runaway inside one reply; a model
        # that has lost the thread instead re-asks the identical call every round
        # until the context runs out (14 and 10 consecutive turns in two logged
        # sessions), which also burns the account's rate limit. On by default:
        # five identical calls in a row is never a legitimate sequence.
        self.repeat_guard = os.environ.get("DANYAPI_REPEAT_GUARD", "1").strip().lower() not in ("0", "false", "no", "off")
        self.repeat_threshold = self._int_env("DANYAPI_REPEAT_THRESHOLD", 5, minimum=2)

        # Per-turn thinking: when the client sends neither `thinking` nor
        # `reasoning_effort`, decide the flag from the turn itself instead of the
        # provider default. Off by default so existing clients keep their exact
        # behavior; opt in with DANYAPI_THINKING_HEURISTIC=1.
        self.thinking_heuristic = os.environ.get("DANYAPI_THINKING_HEURISTIC", "").strip().lower() in ("1", "true", "yes", "on")
        # A user turn at or above this many characters counts as hard.
        try:
            self.thinking_min_prompt_chars = max(1, int(os.environ.get("DANYAPI_THINKING_MIN_PROMPT_CHARS", "2000")))
        except ValueError:
            self.thinking_min_prompt_chars = 2000
        # A conversation with at least this many messages counts as hard.
        try:
            self.thinking_min_messages = max(1, int(os.environ.get("DANYAPI_THINKING_MIN_MESSAGES", "8")))
        except ValueError:
            self.thinking_min_messages = 8
        # Hard-turn signals add up to a score; reaching the threshold turns
        # thinking on. A strong signal (long prompt, error/difficulty cue)
        # clears it alone; weak ones (long history, automatic tool round) must
        # combine. Tunable so the balance can be adjusted without a code change.
        self.thinking_strong_weight = self._int_env("DANYAPI_THINKING_STRONG_WEIGHT", 2, minimum=1)
        self.thinking_weak_weight = self._int_env("DANYAPI_THINKING_WEAK_WEIGHT", 1, minimum=1)
        self.thinking_score_threshold = self._int_env("DANYAPI_THINKING_SCORE_THRESHOLD", 2, minimum=1)

        # Reviving DeepSeek accounts marked broken. A token is marked broken when
        # an auth probe fails, but that can mean only the network was down. A
        # background loop re-checks broken accounts: normally every
        # DANYAPI_DEEPSEEK_REVIVE_INTERVAL seconds; an account broken for longer
        # than DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER is re-checked at the slower
        # DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL cadence.
        self.deepseek_revive_interval = _float_env("DANYAPI_DEEPSEEK_REVIVE_INTERVAL", 900.0, minimum=60.0)
        self.deepseek_revive_slow_interval = _float_env("DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL", 1800.0, minimum=60.0)
        self.deepseek_revive_slow_after = _float_env("DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER", 7200.0, minimum=60.0)

    @staticmethod
    def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
        """Read an integer env var, falling back to ``default`` when unset or bad.

        Non-numeric input never raises: an unusable value keeps the default so
        a typo cannot take the server down. Values below ``minimum`` are clamped.
        """
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return max(minimum, value)


settings = Settings()
