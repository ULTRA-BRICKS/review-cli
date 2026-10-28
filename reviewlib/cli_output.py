"""Stdout tee used by the existing CLI output-file contract."""
from __future__ import annotations

import io
from typing import TextIO


class _Tee(io.TextIOBase):
    """A write-through tee: every write goes to BOTH a live stream (the real stdout,
    so the user still sees the review as it streams) AND an in-memory buffer that
    `-o FILE` later persists. We mirror stdout rather than redirecting it so `-o`
    NEVER swallows the on-screen output (the task: "still also print to stdout").
    Only `write`/`flush` are exercised by `print()`; the rest delegates to the live
    stream so the object stays a drop-in `sys.stdout`."""

    def __init__(self, live: TextIO, buffer: io.StringIO) -> None:
        self._live = live
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._live.write(s)

    def flush(self) -> None:
        self._live.flush()

    def isatty(self) -> bool:
        return self._live.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._live, "encoding", "utf-8")
