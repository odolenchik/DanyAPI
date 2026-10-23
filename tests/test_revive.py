"""Tests for the broken-account revive sweep and loop (danyapi.api.openai)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from danyapi.api.openai import _revive_broken_accounts, _revive_sweep
from danyapi.deepseek.client import AuthCheck


class ReviveAccount:
    def __init__(self, index=0):
        self.index = index
        self.broken = False
        self.broken_at = None
        self.last_revive_at = None
        self.client = MagicMock()
        self.client.check_auth = AsyncMock()

    def is_available(self):
        return not self.broken

    def mark_broken(self):
        self.broken = True

    def revive(self):
        self.broken = False
        self.broken_at = None
        self.last_revive_at = None

    @property
    def label(self):
        return f"acct#{self.index}"


def _pool(*accounts):
    return SimpleNamespace(accounts=list(accounts))


async def test_sweep_revives_on_ok():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.OK)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert acct.is_available()
    acct.client.check_auth.assert_awaited_once()


async def test_sweep_keeps_broken_on_rejected():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.REJECTED)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert not acct.is_available()


async def test_sweep_keeps_broken_on_unreachable():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.UNREACHABLE)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert not acct.is_available()


async def test_sweep_swallows_probe_exception():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.client.check_auth = AsyncMock(side_effect=RuntimeError("boom"))

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert not acct.is_available()
    assert acct.last_revive_at == 1000.0


async def test_sweep_skips_healthy_accounts():
    healthy = ReviveAccount()
    await _revive_sweep(_pool(healthy), fast=10, slow=50, slow_after=100, now=1000.0)
    healthy.client.check_auth.assert_not_awaited()


async def test_sweep_uses_fast_cadence_for_recent_break():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 990.0
    acct.last_revive_at = 985.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.OK)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert acct.is_available()


async def test_sweep_respects_slow_cadence_after_long_outage():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.last_revive_at = 950.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.OK)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)
    acct.client.check_auth.assert_awaited_once()

    acct.client.check_auth.reset_mock()
    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1020.0)
    acct.client.check_auth.assert_not_awaited()


async def test_loop_runs_and_can_be_cancelled():
    acct = ReviveAccount()
    acct.mark_broken()
    acct.broken_at = 0.0
    acct.client.check_auth = AsyncMock(return_value=AuthCheck.REJECTED)
    task = asyncio.create_task(_revive_broken_accounts(_pool(acct), fast=0.01, slow=0.02, slow_after=0.05))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert acct.client.check_auth.await_count >= 1


# ---------------------------------------------------------------------------
# Qwen accounts: ``check_auth`` answers with a plain bool, not ``AuthCheck``
# ---------------------------------------------------------------------------


class QwenReviveAccount:
    def __init__(self, index=0):
        self.index = index
        self.broken = False
        self.broken_at = None
        self.last_revive_at = None
        self.client = MagicMock()
        self.client.check_auth = AsyncMock()

    def is_available(self):
        return not self.broken

    def mark_broken(self):
        self.broken = True
        self.broken_at = 0.0

    def revive(self):
        self.broken = False
        self.broken_at = None
        self.last_revive_at = None

    @property
    def label(self):
        return f"qwen-acct#{self.index}"


async def test_sweep_revives_a_qwen_account_on_true():
    acct = QwenReviveAccount()
    acct.mark_broken()
    acct.client.check_auth = AsyncMock(return_value=True)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert acct.is_available()
    acct.client.check_auth.assert_awaited_once()


async def test_sweep_keeps_a_qwen_account_broken_on_false():
    acct = QwenReviveAccount()
    acct.mark_broken()
    acct.client.check_auth = AsyncMock(return_value=False)

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert not acct.is_available()


async def test_sweep_revives_the_account_whose_credential_was_repaired():
    """The production case: the store now holds a working rotated token.

    ``QwenClient.check_auth`` re-reads the store before refreshing, so once the
    browser transport has written the rotated credential it answers True and the
    account returns to rotation instead of staying out until a restart.
    """
    acct = QwenReviveAccount()
    acct.mark_broken()
    acct.client.check_auth = AsyncMock(side_effect=[False, True])

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1000.0)
    assert not acct.is_available()

    await _revive_sweep(_pool(acct), fast=10, slow=50, slow_after=100, now=1020.0)
    assert acct.is_available()


async def test_sweep_serves_both_pools_transport_agnostically():
    ds_acct = ReviveAccount()
    ds_acct.mark_broken()
    ds_acct.broken_at = 0.0
    ds_acct.client.check_auth = AsyncMock(return_value=AuthCheck.OK)
    qwen_acct = QwenReviveAccount()
    qwen_acct.mark_broken()
    qwen_acct.client.check_auth = AsyncMock(return_value=True)

    await _revive_sweep(_pool(ds_acct, qwen_acct), fast=10, slow=50, slow_after=100, now=1000.0)

    assert ds_acct.is_available()
    assert qwen_acct.is_available()


async def test_qwen_revive_loop_runs_and_can_be_cancelled():
    acct = QwenReviveAccount()
    acct.mark_broken()
    acct.client.check_auth = AsyncMock(return_value=False)
    task = asyncio.create_task(_revive_broken_accounts(_pool(acct), fast=0.01, slow=0.02, slow_after=0.05))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert acct.client.check_auth.await_count >= 1
