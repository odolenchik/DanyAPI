"""Qwen account pacing and anti-bot captcha detection.

Qwen sends no throttle hint the way DeepSeek does: it silently answers with a
captcha (``RGV587_ERROR``) that then sticks for hours. Two properties are
checked here: an account is never driven hard (a burst, a jittered pause, then a
long rest) and a captcha is never retried — it takes the account out of rotation
and is reported as its own state, distinct from a dead token or an exhausted
quota.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import danyapi.qwen.api as qwen_api
from danyapi.accounts import AccountPool, _account_state
from danyapi.qwen.accounts import QwenAccount
from danyapi.qwen.client import QwenClient


def make_account(**kwargs) -> QwenAccount:
    client = QwenClient(token="dummy-token")
    return QwenAccount(0, client, **kwargs)


def test_no_pacing_by_default():
    """Pacing stays off until an interval or a burst rest is configured."""
    account = make_account()
    account.note_request_start(now=100.0)
    assert account.seconds_until_ready(now=100.0) == 0.0


def test_burst_pause_applies_after_burst():
    """The first ``burst`` starts are spaced; the next one waits the long rest."""
    account = make_account(burst=4, min_interval=3.0, burst_pause=360.0)
    for i in range(3):
        account.note_request_start(now=100.0 + i)
        assert account.seconds_until_ready(now=100.0 + i) == pytest.approx(3.0)
    account.note_request_start(now=103.0)
    assert account.seconds_until_ready(now=103.0) == pytest.approx(360.0)


def test_jitter_stays_within_range():
    """Jitter never drops the pause below the base nor exceeds base + jitter."""
    for _ in range(20):
        account = make_account(burst=2, min_interval=3.0, min_interval_jitter=4.0)
        account.note_request_start(now=0.0)
        delay = account.seconds_until_ready(now=0.0)
        assert 3.0 <= delay <= 7.0


def test_wait_turn_sleeps_the_pacing_delay(monkeypatch):
    """``wait_turn`` sleeps the remaining pause instead of returning at once."""
    account = make_account(burst=2, min_interval=3.0, min_interval_jitter=0.0)
    # The deadline is measured against the monotonic clock, so anchor it to now.
    account._cycle_start = time.monotonic()
    account._pacing_delay = 5.0
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(qwen_api.asyncio, "sleep", fake_sleep)
    asyncio.run(account.wait_turn())
    assert slept and 4.9 < slept[0] <= 5.0


def test_recent_request_count():
    account = make_account()
    account.note_request_start(now=100.0)
    account.note_request_start(now=105.0)
    assert account.recent_request_count(10.0, now=106.0) == 2
    assert account.recent_request_count(3.0, now=106.0) == 1


def test_captcha_takes_account_out_and_reports_state():
    """A captcha is its own state: neither a dead token nor a quota cooldown."""
    account = make_account()
    account.mark_captcha(3600.0)
    assert not account.is_available()
    assert _account_state(account) == "captcha"


def test_captcha_cooldown_elapses():
    account = make_account()
    account.mark_captcha(0.0)
    assert account.is_available()


def test_stats_expose_captcha():
    account = make_account()
    account.mark_captcha(3600.0)
    stats = AccountPool([account], label="qwen").stats()
    assert stats["broken_captcha"] == 1
    assert stats["broken_auth"] == 0
    assert stats["captcha_resets_in"] > 0


def test_captcha_markers_detected():
    body = b'{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::SM::x"],"data":{"url":"https://chat.qwen.ai/punish?x5secdata=abc"}}'
    assert qwen_api._is_captcha_text(body)
    assert not qwen_api._is_captcha_text(b'{"choices":[]}')
    assert not qwen_api._is_captcha_text(None)


def test_mark_captcha_helper_drops_session():
    account = make_account()
    pool = AccountPool([account], label="qwen")
    pool.register(0, "s1")
    qwen_api._mark_captcha(account, pool, "s1")
    assert not account.is_available()
    assert pool.account_for_session("s1") is None


# --- remaining edges ---


def test_pacing_delay_property_is_exposed():
    """The last computed pause is readable, which the diagnostics rely on."""
    account = make_account(burst=2, min_interval=3.0, min_interval_jitter=0.0)
    assert account.pacing_delay is None
    account.note_request_start(now=time.monotonic())
    assert account.pacing_delay is not None
    assert account.pacing_delay >= 3.0


def test_burst_property_is_exposed():
    assert make_account(burst=7).burst == 7
    # A nonsense burst is clamped to at least one request.
    assert make_account(burst=0).burst == 1


def test_no_pause_until_the_first_request():
    """Before any request there is no deadline to wait for."""
    account = make_account(burst=4, min_interval=3.0)
    assert account.seconds_until_ready(now=100.0) == 0.0


def test_old_starts_are_dropped_from_the_window():
    """Starts older than the widest window are trimmed so the list stays bounded."""
    account = make_account()
    account.note_request_start(now=0.0)
    account.note_request_start(now=1.0)
    # A start far in the future trims the two ancient entries.
    account.note_request_start(now=1000.0)
    assert account.recent_request_count(10.0, now=1000.0) == 1


# --- browser transport pacing ---


def test_browser_mode_uses_an_even_interval():
    """A browser request is slow already, so it gets a think time, not bursts."""
    account = make_account(browser_interval=20.0, browser_interval_jitter=0.0)
    account.note_request_start(now=100.0)
    assert account.seconds_until_ready(now=100.0) == pytest.approx(20.0)


def test_browser_mode_adds_jitter_only_up_to_its_limit():
    for _ in range(20):
        account = make_account(browser_interval=20.0, browser_interval_jitter=20.0)
        account.note_request_start(now=0.0)
        delay = account.seconds_until_ready(now=0.0)
        assert 20.0 <= delay <= 40.0


def test_browser_mode_has_no_long_burst_rest():
    """The burst rule must not apply: every request gets the same short gap."""
    account = make_account(burst=2, browser_interval=20.0, browser_interval_jitter=0.0)
    for i in range(6):
        account.note_request_start(now=100.0 + i)
        assert account.seconds_until_ready(now=100.0 + i) == pytest.approx(20.0)


def test_browser_mode_without_a_configured_interval_does_not_pace():
    account = make_account(browser_interval=0.0)
    account.note_request_start(now=100.0)
    assert account.seconds_until_ready(now=100.0) == 0.0
