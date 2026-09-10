#!/usr/bin/env python3
"""Backend-agnostic regression test for review-cli#474: 'review diff hangs
indefinitely' when a codex/claude-cli/omp-routed model goes truly silent.

#474 wired true_silence_timeout=true_silence_timeout_seconds(model) into
review_codex, review_claude_cli, and review_omp's _run_streamed calls (see
tests/test_true_silence_backend_wiring.py for the per-backend wiring proof).
This file proves the OTHER half of the fix: the underlying primitive those
three call sites now all forward the kwarg to,
`reviewlib.process._run_streamed` itself, actually reaps a real,
zero-output-producing subprocess quickly when true_silence_timeout is set —
independent of any particular backend. A regression here (e.g. the
true-silence poll silently stops firing) would reintroduce the exact
13-30+ minute hang reported live, on every backend that wires the kwarg,
even though each backend's own wiring test would still pass (they mock
_run_streamed and never exercise a real child process).

Runs directly against `reviewlib.process._run_streamed` (not a backend, not
a mock) with a real subprocess that sleeps far longer than the test's
overall timeout/idle_floor but writes nothing — must return in seconds, not
the full sleep duration.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.process as process  # noqa: E402


def test_run_streamed_reaps_a_zero_output_subprocess_via_true_silence_timeout():
    """A child that sleeps for a long time and never writes a single byte must be
    reaped by true_silence_timeout well before the (much longer) idle_floor or
    overall timeout budgets given here — proving _run_streamed's true-silence path
    itself works, independent of which backend calls it."""
    # Sleeps far longer than the true_silence_timeout below and produces ZERO stdout
    # or stderr bytes — the exact shape reported live (mimo via omp, 0% CPU, no output).
    argv = [sys.executable, "-c", "import time\ntime.sleep(120)\n"]

    started = time.monotonic()
    result = process._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=60,  # generous overall wall-clock budget
        idle_floor=60,  # generous idle floor — true-silence must fire well before it
        backend="fake-hang-regression",
        round_no=1,
        true_silence_timeout=1,  # short: the reap under test
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 125, (
        f"a never-talking child must be reaped with returncode 125, got "
        f"{result.returncode}: {result.stdout!r} {result.stderr!r}"
    )
    assert result.true_silenced is True, (
        "true_silence_timeout was set on a real zero-output subprocess but "
        ".true_silenced was not set — the true-silence reap did not fire"
    )
    assert elapsed < 20, (
        f"reap took {elapsed:.2f}s — far longer than the 1s true_silence_timeout "
        "budget, meaning the child ran toward its full sleep/idle_floor instead of "
        "being reaped early (the exact #474 hang)"
    )


if __name__ == "__main__":
    test_run_streamed_reaps_a_zero_output_subprocess_via_true_silence_timeout()
    print("ok")
