"""Loop guard: a reply repeating the same lines must be detected."""

from __future__ import annotations

from danyapi.loopguard import LoopGuard

NL = "\n"


def _run(text: str, *, chunk: int = 7, **kwargs) -> tuple[int, int] | None:
    guard = LoopGuard(**kwargs)
    for start in range(0, len(text), chunk):
        hit = guard.feed(text[start : start + chunk])
        if hit is not None:
            return hit
    return None


def test_normal_prose_does_not_trip():
    text = NL.join(f"Paragraph {i} about the code." for i in range(500))
    assert _run(text) is None


def test_normal_code_does_not_trip():
    text = NL.join(f"def f_{i}(x):{NL}    return x + {i}" for i in range(300))
    assert _run(text) is None


def test_table_of_distinct_rows_does_not_trip():
    text = NL.join(f"| item {i} | value {i * 7} |" for i in range(40))
    assert _run(text) is None


def test_single_line_repeat_hits():
    assert _run(NL.join(["same line"] * 50)) == (1, 15)


def test_block_repeat_hits():
    text = NL.join(["second", "third", "fourth"] * 40)
    assert _run(text) == (3, 15)


def test_five_line_block_repeat_hits():
    block = ["a", "b", "c", "d", "e"]
    assert _run(NL.join(block * 20)) == (5, 15)


def test_blank_line_run_hits():
    assert _run(NL * 40) == (1, 21)


def test_below_threshold_does_not_hit():
    assert _run(NL.join(["same line"] * 14)) is None


def test_incomplete_tail_line_is_ignored():
    """The fragment after the last newline is still streaming and must not count."""
    text = NL.join(["same line"] * 30) + NL + "partial"
    guard = LoopGuard()
    hit = None
    for start in range(0, len(text), 7):
        hit = guard.feed(text[start : start + 7])
        if hit is not None:
            break
    assert hit is not None


def test_hit_is_sticky():
    guard = LoopGuard()
    first = guard.feed(NL.join(["x"] * 40))
    assert first is not None
    assert guard.feed("more text" + NL) == first


def test_empty_delta_returns_none():
    guard = LoopGuard()
    assert guard.feed("") is None
    assert guard.hit is None


def test_feed_without_newline_returns_none():
    guard = LoopGuard()
    assert guard.feed("no newline here") is None
    assert guard.hit is None


def test_feed_after_hit_short_circuits():
    guard = LoopGuard()
    guard.feed(NL.join(["x"] * 40))
    before = list(guard._hashes)
    guard.feed(NL.join(["y"] * 40))
    assert guard._hashes == before


def test_max_period_and_repeats_are_clamped():
    guard = LoopGuard(max_period=0, repeats=0)
    assert guard.max_period == 1
    assert guard.repeats == 2


def test_zero_repeats_never_matches_long_block():
    """With repeats clamped to 2, a long block needs at least two copies."""
    text = NL.join(["a", "b", "c"] * 2) + NL
    assert _run(text, max_period=40, repeats=0) == (3, 2)


def test_period_larger_than_window_is_skipped():
    """A block longer than what the window holds must not match."""
    guard = LoopGuard(max_period=2, repeats=15)
    text = NL.join(["a", "b", "c"] * 40)
    hit = None
    for start in range(0, len(text), 7):
        hit = guard.feed(text[start : start + 7])
        if hit is not None:
            break
    assert hit is None


def test_insufficient_hashes_returns_none():
    guard = LoopGuard()
    guard.feed("one" + NL + "two" + NL)
    assert guard.hit is None


def test_non_utf8_surrogate_is_encoded_safely():
    guard = LoopGuard()
    for _ in range(20):
        guard.feed("bad \udcff line" + NL)
    assert guard.hit is not None
