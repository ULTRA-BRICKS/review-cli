#!/usr/bin/env python3
"""Unit tests for reviewlib.process's subprocess wiring.

review-cli#428: `_run_streamed`'s Popen call used `stdin=None` whenever
`input_text` was None (the argv-only backends: opencode, omp). A bare `None`
means "don't redirect" — the child INHERITS review-cli's own real stdin fd.
review-cli routinely runs as a subprocess of an orchestrating agent whose own
stdin is an open, non-EOF pipe/tty; if the child CLI reads stdin at all (a
common non-TTY pattern), it blocks forever at 0% CPU with zero output. Fixed
by using `subprocess.DEVNULL` for that path so the child's stdin is always
closed/empty regardless of what review-cli's own stdin is doing, while the
`input_text is not None` path (codex/claude) keeps `stdin=PIPE` unchanged.

Verified via a mocked/spied `subprocess.Popen` (never a real opencode/omp
process) — see `tests/test_streaming.py`/`tests/test_concurrency_cap.py` for
the real-subprocess integration coverage of `_run_streamed`'s other
behaviour; this file is scoped to the one Popen kwarg review-cli#428 fixed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.process as process  # noqa: E402


def _fake_popen_result(argv, **kwargs):
    """A minimal Popen stand-in: exits immediately with empty output, capturing
    the kwargs it was called with so the test can assert on them."""
    proc = mock.Mock()
    proc.pid = 4242
    proc.poll.return_value = 0
    proc.returncode = 0
    proc.stdout = mock.Mock()
    proc.stdout.fileno.return_value = -1
    proc.stderr = mock.Mock()
    proc.stderr.fileno.return_value = -1
    proc.stdin = None
    proc.wait.return_value = 0
    return proc


def test_run_streamed_uses_devnull_stdin_when_no_input_text():
    """The argv-only backend path (opencode/omp: input_text=None) must never
    pass a bare `None`/inherited stdin to Popen — only DEVNULL."""
    argv = [sys.executable, "-c", "print('x')"]
    captured = {}

    real_popen = subprocess.Popen

    def _spy_popen(*args, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        return real_popen(*args, **kwargs)

    with mock.patch.object(process.subprocess, "Popen", side_effect=_spy_popen):
        process._run_streamed(
            argv,
            cwd=REPO_ROOT,
            input_text=None,
            timeout=10,
            backend="faketest-devnull",
            round_no=0,
        )

    assert "stdin" in captured, "Popen was never called"
    assert captured["stdin"] is subprocess.DEVNULL, (
        f"expected stdin=subprocess.DEVNULL for input_text=None, got {captured['stdin']!r} "
        "-- a bare None/inherited stdin lets the child read review-cli's own stdin "
        "fd and hang forever if it never reaches EOF (review-cli#428)"
    )
    assert captured["stdin"] is not None, (
        "stdin must never be bare None (inherited) for the argv-only backend path"
    )


def test_run_streamed_uses_pipe_stdin_when_input_text_provided():
    """The codex/claude path (input_text provided) must keep stdin=PIPE,
    unchanged by the review-cli#428 fix."""
    argv = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    captured = {}

    real_popen = subprocess.Popen

    def _spy_popen(*args, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        return real_popen(*args, **kwargs)

    with mock.patch.object(process.subprocess, "Popen", side_effect=_spy_popen):
        process._run_streamed(
            argv,
            cwd=REPO_ROOT,
            input_text="hello",
            timeout=10,
            backend="faketest-pipe",
            round_no=0,
        )

    assert captured.get("stdin") is subprocess.PIPE, (
        f"expected stdin=subprocess.PIPE for input_text provided, got {captured.get('stdin')!r}"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
