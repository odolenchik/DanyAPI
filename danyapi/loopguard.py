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
    """Track streamed text and report a runaway line-block repeat.

    Feed fragments with :meth:`feed`; once a block of 1..``max_period`` lines has
    repeated ``repeats`` times in a row it returns ``(period, count)`` and keeps
    returning that same result on every later call.
    """

    def __init__(self, *, max_period: int = 40, repeats: int = 15) -> None:
        self.max_period = max(1, max_period)
        self.repeats = max(2, repeats)
        self.hit: tuple[int, int] | None = None
        self._hashes: list[bytes] = []
        self._buf = ""

    def feed(self, delta: str) -> tuple[int, int] | None:
        """Feed a streamed fragment and report a loop as soon as one is visible."""
        if self.hit is not None or not delta:
            return self.hit
        self._buf += delta
        if "\n" not in self._buf:
            return None
        parts = self._buf.split("\n")
        # The text after the last newline is a line still being streamed: it is
        # incomplete, so it must never take part in a comparison.
        self._buf = parts.pop()
        for line in parts:
            self._hashes.append(hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).digest())
        self.hit = self._detect()
        return self.hit

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
