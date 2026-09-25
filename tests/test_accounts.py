import asyncio
import time
from unittest.mock import MagicMock

import pytest

from danyapi.accounts import AccountPool, AccountPoolBusy, ContextIndex, DeepSeekAccount, account_lock
from danyapi.deepseek.client import DeepSeekClient
from danyapi.retry import START_BUFFER_SEC
from danyapi.store import JsonStore


def make_acct(i):
    client = MagicMock(spec=DeepSeekClient)
    client.index = i
    return DeepSeekAccount(i, client)


async def test_no_accounts_raises():
    pool = AccountPool([])
    with pytest.raises(RuntimeError):
        await pool.acquire(None)


async def test_session_affinity():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    pool.register(1, "sess-abc")
    acct, sid = await pool.acquire("sess-abc")
    assert acct is a1
    assert sid == "sess-abc"
    acct, sid = await pool.acquire("sess-zzz")
    assert sid is None


async def test_broken_account_releases_session():
    a0, a1 = make_acct(0), make_acct(1)
    a0.broken = True
    pool = AccountPool([a0, a1])
    pool.register(0, "sess-broken")
    acct, sid = await pool.acquire("sess-broken")
    assert acct is a1
    assert sid is None
    assert "sess-broken" not in pool._by_session


async def test_round_robin_frees_first():
    a0, a1, a2 = make_acct(0), make_acct(1), make_acct(2)
    pool = AccountPool([a0, a1, a2])
    await a1.sem.acquire()
    await asyncio.sleep(0)
    assert a1.sem.locked()
    acct, _ = await pool.acquire(None)
    assert acct is a0
    await a0.sem.acquire()
    await asyncio.sleep(0)
    acct, _ = await pool.acquire(None)
    assert acct is a2
    await a2.sem.acquire()
    await asyncio.sleep(0)
    acct, _ = await pool.acquire(None)
    assert acct is not None
    a1.sem.release()
    a0.sem.release()
    a2.sem.release()


async def test_acquire_waits_for_free_account():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    await a0.sem.acquire()
    await a1.sem.acquire()

    async def acquire():
        return await pool.acquire(None, max_wait=5)

    task = asyncio.create_task(acquire())
    await asyncio.sleep(0.1)
    assert not task.done()
    a1.sem.release()
    acct, sid = await asyncio.wait_for(task, timeout=2)
    assert acct is a1
    assert sid is None
    a0.sem.release()


async def test_acquire_times_out_when_busy():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    await a0.sem.acquire()
    await a1.sem.acquire()
    with pytest.raises(AccountPoolBusy):
        await pool.acquire(None, max_wait=0.2)
    a0.sem.release()
    a1.sem.release()


async def test_session_affinity_waits_for_its_account():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    pool.register(1, "sess-x")
    await a1.sem.acquire()

    async def acquire():
        return await pool.acquire("sess-x", max_wait=5)

    task = asyncio.create_task(acquire())
    await asyncio.sleep(0.1)
    assert not task.done()
    a1.sem.release()
    acct, sid = await asyncio.wait_for(task, timeout=2)
    assert acct is a1
    assert sid == "sess-x"


async def test_account_lock_no_timeout():
    sem = asyncio.Semaphore(1)
    async with account_lock(sem):
        assert sem.locked()
    assert not sem.locked()


async def test_account_lock_timeout_free():
    sem = asyncio.Semaphore(1)
    async with account_lock(sem, 1.0):
        assert sem.locked()
    assert not sem.locked()


async def test_account_lock_timeout_busy():
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    try:
        with pytest.raises(AccountPoolBusy):
            async with account_lock(sem, 0.05):
                pass
    finally:
        sem.release()


async def test_account_lock_marks_request_end_on_release():
    """Releasing the lock is what tells the account a request has finished."""
    sem = asyncio.Semaphore(1)
    account = MagicMock()
    async with account_lock(sem, 1.0, account):
        account.note_request_end.assert_not_called()
    account.note_request_end.assert_called_once_with()


async def test_account_lock_without_account_still_works():
    """The account argument is optional; qwen call sites pass no account."""
    sem = asyncio.Semaphore(1)
    async with account_lock(sem, 1.0):
        pass
    assert not sem.locked()


async def test_account_lock_marks_request_end_on_exception():
    sem = asyncio.Semaphore(1)
    account = MagicMock()
    with pytest.raises(RuntimeError):
        async with account_lock(sem, 1.0, account):
            raise RuntimeError("boom")
    account.note_request_end.assert_called_once_with()


async def test_account_lock_exception_releases():
    sem = asyncio.Semaphore(1)
    with pytest.raises(ValueError):
        async with account_lock(sem, 1.0):
            raise ValueError("boom")
    assert not sem.locked()


async def test_account_lock_end_marks_hold_back_the_next_start():
    """Full cycle: the lock release is what sets the end-gap deadline.

    This is the integration point the fix hinges on — the unit tests on
    ``note_request_end`` and ``account_lock`` are only meaningful if passing the
    account to the lock actually makes the next request wait.

    ``instant_sleep`` in conftest disables real sleeping, so the wait is checked
    via ``seconds_until_ready()`` rather than by letting the clock run.
    """
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_end_gap=1.0)

    async with account_lock(acct.sem, 1.0, acct):
        acct.note_request_start()
    # The release happened just now, so the end-gap deadline is still in the future.
    assert acct.seconds_until_ready() > 0.0


async def test_account_lock_without_account_does_not_record_end():
    """Omitting the account leaves it unaware the request finished."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_end_gap=1.0)
    async with account_lock(acct.sem, 1.0):
        acct.note_request_start()
    assert acct.seconds_until_ready() == 0.0


@pytest.fixture
def ctx_store(tmp_path, monkeypatch):
    import danyapi.store as store_mod

    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))
    return JsonStore("ctx-persist", "default")


def test_restore_from_store(ctx_store):
    ctx_store.set("s1", ["a"])
    ctx_store.set("s2", ["b"])
    idx = ContextIndex(16, store=ctx_store)
    assert idx.lookup(("a",)) == "s1"
    assert idx.lookup(("b",)) == "s2"


def test_index_persists(ctx_store):
    idx = ContextIndex(16, store=ctx_store)
    idx.index("s1", ("a",))
    idx2 = ContextIndex(16, store=JsonStore("ctx-persist", "default"))
    assert idx2.lookup(("a",)) == "s1"


def test_eviction_discards_from_store(ctx_store):
    idx = ContextIndex(2, store=ctx_store)
    idx.index("s1", ("a",))
    idx.index("s2", ("b",))
    idx.index("s3", ("c",))
    assert "s1" not in ctx_store


def test_forget_discards_from_store(ctx_store):
    idx = ContextIndex(16, store=ctx_store)
    idx.index("s1", ("a",))
    idx.forget("s1")
    assert "s1" not in ctx_store
    assert idx.lookup(("a",)) is None


def test_expired_discards_from_store(ctx_store):
    idx = ContextIndex(16, ttl=0.1, store=ctx_store)
    idx.index("s1", ("a",))
    idx._ts["s1"] = time.monotonic() - 10
    assert idx.lookup(("a",)) is None
    assert "s1" not in ctx_store


def test_index_touches_superset():
    idx = ContextIndex(16)
    idx.index("s1", ("a", "b"))
    idx.index("s1", ("a",))
    assert idx.lookup(("a", "b", "c")) == "s1"


def test_index_supersedes_shorter():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    idx.index("s1", ("a", "b", "c"))
    assert idx.lookup(("a", "b", "c")) == "s1"


def test_empty_index_counts_miss():
    idx = ContextIndex(16)
    assert idx.lookup(("a",)) is None
    assert idx.misses == 1


def test_min_maxsize():
    assert ContextIndex(0)._maxsize == 1
    assert ContextIndex(-5)._maxsize == 1


def test_restore_skips_garbage(ctx_store):
    ctx_store.set("s1", ["a"])
    ctx_store.set("bad", 42)
    ctx_store.set("", ["x"])
    idx = ContextIndex(16, store=ctx_store)
    assert idx.lookup(("a",)) == "s1"
    assert idx.lookup(("x",)) is None


def test_exact_match_reuses():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    assert idx.lookup(("a",)) == "s1"


def test_continuation_matches_prefix():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    idx.index("s2", ("x",))
    assert idx.lookup(("a", "b")) == "s1"
    assert idx.lookup(("x", "y", "z")) == "s2"


def test_prefers_longest_prefix():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    idx.index("s2", ("a", "b"))
    assert idx.lookup(("a", "b")) == "s2"
    assert idx.lookup(("a", "b", "c")) == "s2"


def test_no_match():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    assert idx.lookup(("c",)) is None
    assert idx.lookup(()) is None


def test_eviction():
    idx = ContextIndex(2)
    idx.index("s1", ("a",))
    idx.index("s2", ("b",))
    idx.index("s3", ("c",))
    assert idx.lookup(("a",)) is None
    assert idx.lookup(("b",)) == "s2"


def test_recency_tiebreak():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    idx.index("s2", ("a",))
    assert idx.lookup(("a",)) == "s2"


def test_stats_counters():
    idx = ContextIndex(16)
    idx.index("s1", ("a",))
    assert idx.lookup(("x",)) is None
    assert idx.lookup(("a",)) == "s1"
    assert idx.hits == 1
    assert idx.misses == 1


@pytest.fixture
def pool_store(tmp_path, monkeypatch):
    import danyapi.store as store_mod

    monkeypatch.setattr(store_mod.settings, "cache_dir", str(tmp_path))


def test_register_persists_affinity(pool_store):
    store = JsonStore("aff", "default")
    AccountPool([make_acct(0)], affinity_store=store).register(0, "s1")
    pool2 = AccountPool([make_acct(0)], affinity_store=JsonStore("aff", "default"))
    assert pool2.account_for_session("s1") is not None


def test_register_ttl_cleanup():
    pool = AccountPool([make_acct(0)], ttl=0.05)
    for i in range(4097):
        pool._by_session[f"s{i}"] = (0, time.monotonic())
    pool._by_session["stale"] = (0, time.monotonic() - 10)
    pool.register(0, "fresh")
    assert "stale" not in pool._by_session
    assert "fresh" in pool._by_session


def test_account_for_session_expired():
    pool = AccountPool([make_acct(0)], ttl=0.05)
    pool.register(0, "s1")
    pool._by_session["s1"] = (0, time.monotonic() - 10)
    assert pool.account_for_session("s1") is None
    assert "s1" not in pool._by_session


def test_account_for_session_broken():
    a0 = make_acct(0)
    a0.broken = True
    pool = AccountPool([a0])
    pool.register(0, "s1")
    assert pool.account_for_session("s1") is None
    assert "s1" not in pool._by_session


class _QuotaAccount:
    """Account stub whose availability is driven by an explicit flag."""

    def __init__(self, index: int, available: bool = True) -> None:
        self.index = index
        self.sem = asyncio.Semaphore(1)
        self._available = available

    def is_available(self) -> bool:
        return self._available


async def test_pool_skips_quota_cooling_account():
    a0, a1 = _QuotaAccount(0, available=False), _QuotaAccount(1)
    pool = AccountPool([a0, a1])
    assert a0 not in pool.healthy
    acct, sid = await pool.acquire(None)
    assert acct is a1
    assert sid is None


async def test_pool_returns_account_after_cooldown():
    a0 = _QuotaAccount(0, available=False)
    pool = AccountPool([a0])
    assert pool.healthy == []
    a0._available = True  # cooldown elapsed
    assert pool.healthy == [a0]


def test_stats():
    pool = AccountPool([make_acct(0), make_acct(1)])
    pool.register(0, "s1")
    pool.index_context("s1", ("a",))
    stats = pool.stats()
    assert stats["accounts"] == 2
    assert stats["session_affinities"] == 1
    assert stats["context_entries"] == 1
    assert stats["context_cache_size"] == 128


def test_context_forget():
    pool = AccountPool([make_acct(0)])
    pool.index_context("s1", ("a",))
    assert pool.resolve_context(("a",)) == "s1"
    pool.forget_context("s1")
    assert pool.resolve_context(("a",)) is None


async def test_acquire_no_healthy():
    a0 = make_acct(0)
    a0.broken = True
    pool = AccountPool([a0])
    with pytest.raises(RuntimeError):
        await pool.acquire(None)


def test_mark_broken_idempotent():
    a0 = make_acct(0)
    a0.mark_broken()
    a0.mark_broken()
    assert a0.broken
    assert a0.label == "acct#0"


def test_resolve_routes_to_owner():
    a0, a1 = make_acct(0), make_acct(1)
    pool = AccountPool([a0, a1])
    pool.register(1, "sess-a")
    pool.index_context("sess-a", ("u1",))
    assert pool.resolve_context(("u1",)) == "sess-a"


def test_forget_removes_mapping():
    pool = AccountPool([make_acct(0)])
    pool.register(0, "s1")
    assert pool.account_for_session("s1").index == 0
    pool.forget("s1")
    assert pool.account_for_session("s1") is None


def test_restore_skips_empty_sequence(ctx_store):
    ctx_store.set("s1", [])
    ctx_store.set("s2", ["a"])
    ctx_store.set("", ["x"])
    ctx_store.set("s3", 42)
    idx = ContextIndex(16, store=ctx_store)
    assert idx.lookup(("a",)) == "s2"


def test_lookup_skips_longer_sequences():
    idx = ContextIndex(16)
    idx.index("s1", ("a", "b"))
    assert idx.lookup(("a",)) is None


def test_index_ignores_empty():
    idx = ContextIndex(16)
    idx.index("", ("a",))
    idx.index("s1", ())
    assert len(idx._seqs) == 0


def test_register_ttl_cleanup_with_store(pool_store):
    store = JsonStore("aff2", "default")
    pool = AccountPool([make_acct(0)], ttl=0.05, affinity_store=store)
    for i in range(4097):
        pool._by_session[f"s{i}"] = (0, time.monotonic())
    pool._by_session["stale"] = (0, time.monotonic() - 10)
    pool.register(0, "fresh")
    assert "stale" not in store


def test_forget_discards_affinity(pool_store):
    store = JsonStore("aff3", "default")
    pool = AccountPool([make_acct(0)], affinity_store=store)
    pool.register(0, "s1")
    assert "s1" in store
    pool.forget("s1")
    assert "s1" not in store


def test_account_for_session_expired_keeps_durable_affinity(pool_store):
    # TTL only drops the in-memory entry; the durable mapping survives so a
    # resumed session still routes to the account that owns its chat.
    store = JsonStore("aff4", "default")
    pool = AccountPool([make_acct(0)], ttl=0.05, affinity_store=store)
    pool.register(0, "s1")
    pool._by_session["s1"] = (0, time.monotonic() - 10)
    assert pool.account_for_session("s1") is not None
    assert "s1" in store


def test_account_for_session_expired_without_store_returns_none(pool_store):
    pool = AccountPool([make_acct(0)], ttl=0.05)
    pool.register(0, "s1")
    pool._by_session["s1"] = (0, time.monotonic() - 10)
    assert pool.account_for_session("s1") is None


def test_account_for_session_reads_durable_affinity_after_restart(pool_store):
    # A fresh pool (simulating a restart) has an empty in-memory map but the
    # persisted affinity routes the session back to its owner account.
    store = JsonStore("aff7", "default")
    AccountPool([make_acct(0)], affinity_store=store).register(0, "s1")
    pool2 = AccountPool([make_acct(0)], affinity_store=JsonStore("aff7", "default"))
    pool2._by_session.clear()
    acct = pool2.account_for_session("s1")
    assert acct is not None
    assert acct.index == 0


def test_account_for_session_broken_discards(pool_store):
    store = JsonStore("aff6", "default")
    a0 = make_acct(0)
    a0.broken = True
    pool = AccountPool([a0], affinity_store=store)
    pool.register(0, "s1")
    assert pool.account_for_session("s1") is None
    assert "s1" not in store


def test_restore_affinities_variants(pool_store):
    store = JsonStore("aff5", "default")
    store.set("good", 0)
    store.set("as-list", [0])
    store.set("bad-bool", True)
    store.set("bad-str", "x")
    store.set("out-of-range", 5)
    store.set("", 0)
    pool = AccountPool([make_acct(0)], affinity_store=store)
    assert pool.account_for_session("good") is not None
    assert pool.account_for_session("as-list") is not None
    assert pool.account_for_session("bad-bool") is None
    assert pool.account_for_session("bad-str") is None
    assert pool.account_for_session("out-of-range") is None


def test_note_request_start_trims_old_entries():
    acct = make_acct(0)
    acct.note_request_start(now=0.0)
    acct.note_request_start(now=1.0)
    assert acct.recent_request_count(10.0, now=1.0) == 2
    # Everything older than the widest window is dropped from the buffer.
    acct.note_request_start(now=START_BUFFER_SEC + 5.0)
    assert len(acct._starts) == 1


def test_recent_request_count_windows():
    acct = make_acct(0)
    now = 100.0
    acct.note_request_start(now=now - 50.0)
    acct.note_request_start(now=now - 20.0)
    acct.note_request_start(now=now - 5.0)
    assert acct.recent_request_count(10.0, now=now) == 1
    assert acct.recent_request_count(30.0, now=now) == 2
    assert acct.recent_request_count(60.0, now=now) == 3


def test_recent_request_count_trims_buffer_directly():
    acct = make_acct(0)
    now = 1000.0
    # Push entries straight into the buffer so recent_request_count does the
    # trimming itself (note_request_start would have pruned them already).
    acct._starts.extend([now - START_BUFFER_SEC - 10.0, now - 5.0])
    assert acct.recent_request_count(30.0, now=now) == 1
    assert len(acct._starts) == 1


def test_recent_request_count_empty():
    acct = make_acct(0)
    assert acct.recent_request_count(30.0, now=0.0) == 0


def test_pacing_disabled_by_default():
    acct = make_acct(0)
    assert acct.min_interval == 0.0
    acct.note_request_start(now=100.0)
    assert acct.seconds_until_ready(now=100.0) == 0.0


def test_pacing_sets_deadline_from_previous_start():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_start(now=100.0)
    # 4 s after the previous start the account is ready again.
    assert acct.seconds_until_ready(now=100.0) == 4.0
    assert acct.seconds_until_ready(now=102.0) == 2.0
    assert acct.seconds_until_ready(now=104.0) == 0.0
    assert acct.seconds_until_ready(now=110.0) == 0.0


def test_pacing_deadline_ignores_request_duration():
    """A slow request must not leave a wait behind it.

    The deadline is derived from the previous *start*, not its completion, so a
    request that ran longer than the interval is immediately ready to send again.
    """
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_start(now=100.0)
    # Request took 10 s; wall clock is now well past the deadline.
    assert acct.seconds_until_ready(now=110.0) == 0.0


def test_pacing_deadline_never_moves_backwards():
    """Repeated starts in the same cycle keep the interval applied.

    With ``burst=1`` every start is its own cycle, so the deadline is always
    ``last_start + min_interval`` — a second start cannot shorten the wait.
    """
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_start(now=100.0)
    acct.note_request_start(now=101.0)
    assert acct.seconds_until_ready(now=101.0) == 4.0
    assert acct.seconds_until_ready(now=105.0) == 0.0


def test_min_end_gap_waits_after_the_previous_request_finished():
    """A chained long request must still leave a gap after the stream closes.

    Start-to-start pacing alone is satisfied here: the previous request started
    100 s ago, far more than any interval. The wait has to come from the moment
    the previous request *ended*.
    """
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, min_end_gap=5.0)
    acct.note_request_start(now=100.0)
    # The request ran for 80 s, so by the time it ends the start deadline is long past.
    acct.note_request_end(now=180.0)
    assert acct.seconds_until_ready(now=180.0) == 5.0
    assert acct.seconds_until_ready(now=183.0) == 2.0
    assert acct.seconds_until_ready(now=185.0) == 0.0


def test_min_end_gap_uses_the_later_of_the_two_deadlines():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=8.0, burst=1, min_end_gap=3.0)
    acct.note_request_start(now=100.0)
    acct.note_request_end(now=101.0)
    # Start deadline (108) is later than the end deadline (104).
    assert acct.seconds_until_ready(now=101.0) == 7.0


def test_min_end_gap_disabled_by_default():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_end(now=180.0)
    # No pacing configured at all -> ready immediately.
    assert acct.seconds_until_ready(now=180.0) == 0.0


def test_min_end_gap_only_pacing():
    """min_end_gap works on its own, with start pacing disabled."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_end_gap=5.0)
    acct.note_request_end(now=200.0)
    assert acct.seconds_until_ready(now=200.0) == 5.0
    assert acct.seconds_until_ready(now=204.0) == 1.0
    assert acct.seconds_until_ready(now=205.0) == 0.0


def test_min_end_gap_negative_is_clamped():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_end_gap=-3.0)
    acct.note_request_end(now=200.0)
    assert acct.seconds_until_ready(now=200.0) == 0.0


def test_last_end_gap_reports_end_to_start_delay():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient))
    assert acct.last_end_gap is None
    acct.note_request_start(now=100.0)
    # No end recorded yet -> still unknown, never a fake value.
    assert acct.last_end_gap is None
    acct.note_request_end(now=160.0)
    acct.note_request_start(now=163.0)
    assert acct.last_end_gap == 3.0


def test_last_end_gap_is_not_updated_by_end_alone():
    """The gap is measured end->start, so a second end cannot move it."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient))
    acct.note_request_end(now=10.0)
    acct.note_request_start(now=14.0)
    assert acct.last_end_gap == 4.0
    acct.note_request_end(now=99.0)
    assert acct.last_end_gap == 4.0


def test_burst_allows_back_to_back_starts_without_wait():
    """Up to ``burst`` requests go out immediately; only then does the gap apply."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=4)
    for i in range(4):
        assert acct.seconds_until_ready(now=float(i)) == 0.0
        acct.note_request_start(now=float(i))
    # The burst is spent, so the next start waits 4 s after the LAST start (t=3).
    assert acct.seconds_until_ready(now=3.0) == 4.0
    assert acct.seconds_until_ready(now=5.0) == 2.0
    assert acct.seconds_until_ready(now=7.0) == 0.0


def test_burst_counter_resets_after_the_gap():
    """Serving the gap starts a fresh burst of ``burst`` requests."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=2)
    acct.note_request_start(now=0.0)
    acct.note_request_start(now=1.0)
    assert acct.seconds_until_ready(now=1.0) == 4.0  # next start at 5.0
    acct.note_request_start(now=5.0)  # gap served -> new cycle
    assert acct.seconds_until_ready(now=5.0) == 0.0  # second request of the new burst


def test_burst_one_is_a_plain_interval():
    """burst=1 disables the burst behaviour entirely."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=1)
    assert acct.burst == 1
    acct.note_request_start(now=0.0)
    assert acct.seconds_until_ready(now=0.0) == 4.0


def test_burst_negative_is_clamped_to_one():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=0)
    assert acct.burst == 1


def test_pacing_deadline_advances_when_starts_respect_the_wait():
    """Steady-state behaviour: each start lands exactly one interval after the last."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_start(now=100.0)
    assert acct.seconds_until_ready(now=100.0) == 4.0
    # wait_turn() would have slept until 104; the next start lands there.
    acct.note_request_start(now=104.0)
    assert acct.seconds_until_ready(now=104.0) == 4.0


def test_min_interval_negative_is_clamped():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=-5.0)
    assert acct.min_interval == 0.0
    assert acct.seconds_until_ready(now=0.0) == 0.0


def test_min_start_gap_disabled_by_default():
    """Without min_start_gap, isolated requests pass without delay."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=2)
    acct.note_request_start(now=100.0)
    # Second isolated request — no gap enforced (min_start_gap=0).
    assert acct.seconds_until_ready(now=100.5) == 0.0


def test_min_start_gap_enforces_delay_between_isolated_requests():
    """Isolated requests must respect min_start_gap even before burst is exhausted."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=2, min_start_gap=2.0)
    acct.note_request_start(now=100.0)
    # Only 0.5s elapsed — need to wait until 102.0.
    assert acct.seconds_until_ready(now=100.5) == 1.5
    # Exactly at deadline — ready.
    assert acct.seconds_until_ready(now=102.0) == 0.0


def test_min_start_gap_applies_after_burst_uses_min_interval():
    """Post-burst requests use min_interval, not min_start_gap."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=8.0, burst=2, min_start_gap=1.0)
    # First request starts at t=0 (no prior start to wait for).
    acct.note_request_start(now=0.0)  # _since_gap: 0→1
    # Before the second request: _since_gap(1) < burst(2) → in-burst, uses min_start_gap.
    assert acct.seconds_until_ready(now=0.5) == 0.5  # wait until t=1
    acct.note_request_start(now=1.0)  # _since_gap: 1→2 (== burst)
    # Before the third request: _since_gap(2) >= burst(2) → post-burst, uses min_interval.
    assert acct.seconds_until_ready(now=1.0) == 8.0  # wait until t=9
    assert acct.seconds_until_ready(now=9.0) == 0.0


def test_min_start_gap_only_no_burst_pacing():
    """min_start_gap works independently when min_interval is disabled."""
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=0.0, burst=4, min_start_gap=3.0)
    acct.note_request_start(now=100.0)
    assert acct.seconds_until_ready(now=100.0) == 3.0
    assert acct.seconds_until_ready(now=102.0) == 1.0
    assert acct.seconds_until_ready(now=103.0) == 0.0


def test_min_start_gap_negative_is_clamped():
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0, burst=2, min_start_gap=-1.0)
    # Clamped to 0 → behaves as if not set.
    acct.note_request_start(now=100.0)
    assert acct.seconds_until_ready(now=100.5) == 0.0


async def test_wait_turn_sleeps_for_remaining_interval(monkeypatch):
    acct = DeepSeekAccount(0, MagicMock(spec=DeepSeekClient), min_interval=4.0)
    acct.note_request_start(now=time.monotonic())
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await acct.wait_turn()
    assert len(slept) == 1
    assert 0 < slept[0] <= 4.0


async def test_wait_turn_no_sleep_when_disabled(monkeypatch):
    acct = make_acct(0)
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await acct.wait_turn()
    assert slept == []
