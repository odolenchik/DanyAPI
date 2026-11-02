"""Detect a reply that collapsed into repeating the same lines.

A looping upstream never closes the stream, and httpx's read timeout resets on
every byte, so the proxy can sit on such a reply forever. The degenerate guard in
`danyapi/api/streaming.py` covers one narrow shape (a runaway of bare tool-call
containers) and only in tool mode; this one looks at the reply's structure
instead, so it works for any mode and any repeated text.

The signal is a block of lines repeated back to back. Block length runs from one
line (the common case: a single markup opener line over and over) up to
``max_period`` lines, which also catches cycles that span several distinct lines.
A legitimate answer does not repeat the same block ``repeats`` times in a row, so
the check costs a handful of digest comparisons per finished line.
"""

from __future__ import annotations

import hashlib


class LoopGuard:
    """Track streamed text and report a runaway repeat.

    Line-level detection covers a block of 1..``max_period`` lines repeated
    ``repeats`` times in a row. It needs newlines, though, so a model that loops
    inside a single line (a marker, a phrase, a run of characters with no break)
    would stream forever unnoticed. A second, character-level pass covers that
    shape: a block of 1..``char_max_period`` characters repeated
    ``char_repeats`` times, provided the run covers at least ``char_min_chars``
    characters so ordinary repetition (a rule of dashes, a short indent) never
    trips it.

    Feed fragments with :meth:`feed`; once either pass sees a loop it returns
    ``(period, count)`` and keeps returning that same result on every later call.
    """

    def __init__(
        self,
        *,
        max_period: int = 40,
        repeats: int = 15,
        char_max_period: int = 200,
        char_repeats: int = 15,
        char_min_chars: int = 2000,
    ) -> None:
        self.max_period = max(1, max_period)
        self.repeats = max(2, repeats)
        self.char_max_period = max(1, char_max_period)
        self.char_repeats = max(2, char_repeats)
        self.char_min_chars = max(1, char_min_chars)
        self.hit: tuple[int, int] | None = None
        self._hashes: list[bytes] = []
        self._buf = ""
        # Character pass: keep only the tail a repeat could still be visible in,
        # and run the scan in steps so its cost stays amortised per fed byte.
        self._char_window = max(self.char_min_chars, self.char_max_period * self.char_repeats)
        self._char_buf = ""
        self._char_step = max(1, self._char_window // 8)
        self._char_total = 0
        self._char_last_check = 0

    def feed(self, delta: str) -> tuple[int, int] | None:
        """Feed a streamed fragment and report a loop as soon as one is visible."""
        if self.hit is not None or not delta:
            return self.hit
        self._buf += delta
        if "\n" in self._buf:
            parts = self._buf.split("\n")
            # The text after the last newline is a line still being streamed: it
            # is incomplete, so it must never take part in a comparison.
            self._buf = parts.pop()
            for line in parts:
                self._hashes.append(hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).digest())
            self.hit = self._detect()
        if self.hit is None:
            self.hit = self._track_chars(delta)
        return self.hit

    def _track_chars(self, delta: str) -> tuple[int, int] | None:
        self._char_total += len(delta)
        self._char_buf += delta
        if len(self._char_buf) > self._char_window:
            self._char_buf = self._char_buf[-self._char_window :]
        if self._char_total - self._char_last_check < self._char_step:
            return None
        self._char_last_check = self._char_total
        return self._detect_chars()

    def _detect_chars(self) -> tuple[int, int] | None:
        buf = self._char_buf
        size = len(buf)
        for period in range(1, self.char_max_period + 1):
            need = period * self.char_repeats
            if need > size:
                break
            if need < self.char_min_chars:
                continue
            block = buf[size - period :]
            count = 1
            index = size - period
            while index - period >= 0 and buf[index - period : index] == block:
                count += 1
                index -= period
                if count >= self.char_repeats:
                    return period, count
        return None

    def _detect(self) -> tuple[int, int] | None:
        window = self.max_period * self.repeats
        hashes = self._hashes[-window:]
        if len(hashes) < self.repeats:
            return None
        for period in range(1, min(self.max_period, len(hashes) // self.repeats) + 1):
            block = hashes[-period:]
            count = 0
            index = len(hashes)
            while index - period >= 0 and hashes[index - period : index] == block:
                count += 1
                index -= period
            if count >= self.repeats:
                return period, count
        return None
