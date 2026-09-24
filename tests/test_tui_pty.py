#!/usr/bin/env python3
"""REAL-pty tests for the live curses TUI (review-cli#548).

curses genuinely needs a real terminal DEVICE (a controlling tty, not a captured pipe) to
initialize a screen at all -- tests/test_tui.py's unit tests already prove the ACQUISITION
gate never even attempts curses outside one, but proving the TUI actually PAINTS a screen,
that --no-tui/$REVIEW_NO_TUI disable it under a genuinely capable terminal, and that a
broken terminal falls back within a bounded time, all need a real pty. This file spawns the
ACTUAL `bin/review diff` CLI as a subprocess attached to one (`pty.openpty()`), with
`REVIEW_FAKE_BACKEND=1` so no network/API keys/real model CLIs are needed (see
tests/test_e2e_resume.py for the same fake-backend-over-a-real-subprocess pattern this
mirrors) and a small `REVIEW_FAKE_DELAY` so the board run takes long enough for at least a
few TUI render ticks (`_REFRESH_SECONDS = 0.3`) to land before it completes.

The pty's master fd MUST be drained continuously while the child runs (a real terminal
emulator is always reading) -- a test that sleeps-then-reads-once can deadlock the child on
a full pty output buffer (reproduced empirically while building this test: an un-drained
master hung a curses child indefinitely on a plain `stdscr.refresh()`). `_run_under_pty`
below runs a dedicated draining thread for exactly this reason.

Self-skips (a real pytest SKIP, or a standalone SKIP line) when `pty`/`curses` aren't
importable (stock Windows CPython) or a working `git` binary isn't on PATH -- mirrors
tests/test_qa_web.py's `_skip` convention.

Plain-script harness (mirrors tests/test_e2e_resume.py): each test_* is run by __main__.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

try:
    import pty as _pty
except ImportError:  # pragma: no cover - POSIX-only stdlib module
    _pty = None

try:
    import curses as _curses  # noqa: F401 - presence check only, never used directly here
except ImportError:  # pragma: no cover
    _curses = None


def _skip(reason: str) -> None:
    """SKIP under pytest (a real pytest skip) or print a SKIP line standalone -- mirrors
    tests/test_qa_web.py's `_skip` convention."""
    try:
        import pytest

        if os.environ.get("PYTEST_CURRENT_TEST"):
            pytest.skip(reason)
    except ImportError:
        pass
    print(f"SKIP (tui pty): {reason}")


def _make_repo(base: Path) -> Path:
    """A throwaway git repo with a real UNSTAGED diff (a tracked file modified after the
    initial commit) -- plain `review diff` (no --staged) picks this up via `git diff`."""
    repo = base / "repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "HOME": str(base),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, env=git_env, check=True)
    (repo / "file.txt").write_text("line one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, env=git_env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, env=git_env, check=True
    )
    (repo / "file.txt").write_text("line one\nline two\n", encoding="utf-8")
    return repo


def _env(*, term: str, fake_delay: float, extra: dict[str, str] | None = None) -> dict:
    home = tempfile.mkdtemp(prefix="review-tui-pty-home-")
    log_dir = tempfile.mkdtemp(prefix="review-tui-pty-logs-")
    env = {
        **os.environ,
        "TERM": term,
        "HOME": home,
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        "REVIEW_FAKE_BACKEND": "1",
        "REVIEW_FAKE_DELAY": str(fake_delay),
        "REVIEW_LOG_DIR": log_dir,
        "REVIEW_STATS_FILE": str(Path(log_dir) / "stats.jsonl"),
        "COLUMNS": "80",
        "LINES": "24",
    }
    env.pop("REVIEW_NO_TUI", None)
    if extra:
        env.update(extra)
    return env


def _run_under_pty(
    argv: list[str], env: dict[str, str], cwd: str, timeout: float = 40.0
) -> tuple[int, bytes, float]:
    """Spawn `argv` attached to a REAL pty, continuously draining the master fd (a real
    terminal emulator is always reading -- an un-drained master can deadlock a curses
    child on a full output buffer, reproduced empirically while building this test).
    Returns (returncode, combined captured bytes, wall-clock seconds)."""
    master_fd, slave_fd = _pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    buf = bytearray()
    stop = threading.Event()

    def _drain() -> None:
        while not stop.is_set():
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=cwd,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    elapsed = time.monotonic() - started
    stop.set()
    drain_thread.join(timeout=2.0)
    try:
        os.close(master_fd)
    except OSError:
        pass
    return proc.returncode, bytes(buf), elapsed


def _preflight_skip() -> str | None:
    if _pty is None:
        return "the `pty` module is not available on this platform"
    if _curses is None:
        return "the `curses` module is not available on this platform"
    if shutil.which("git") is None:
        return "no working `git` binary on PATH"
    return None


def test_tui_activates_under_real_pty():
    """review diff auto-shows the live board (role/model/status/elapsed columns) in a real
    interactive terminal -- acceptance criterion #1, verified against a real pty."""
    reason = _preflight_skip()
    if reason:
        _skip(reason)
        return
    with tempfile.TemporaryDirectory() as d:
        repo = _make_repo(Path(d))
        rc, output, elapsed = _run_under_pty(
            [
                sys.executable, str(REVIEW_BIN), "diff",
                "--task", "TEST-548-A", "--pool", "2",
            ],
            _env(term="xterm", fake_delay=1.2),
            cwd=str(repo),
        )
    text = output.decode("utf-8", "replace")
    assert rc == 0, f"rc={rc}\n{text[:4000]}"
    for marker in ("live board", "ROLE", "MODEL", "STATUS", "ELAPSED"):
        assert marker in text, f"{marker!r} missing from pty output:\n{text[:4000]}"
    # The underlying review still completed for real (the fake backend's deterministic
    # body), independent of whether the TUI happened to be drawing over it.
    assert "a concrete, deterministic suggestion" in text, text[:4000]
    assert elapsed < 25.0, f"took {elapsed:.2f}s"


def test_broken_terminal_falls_back_within_bounded_time():
    """A broken terminal (curses unable to start a real screen -- TERM=unknown has no
    terminfo entry, verified to raise curses.error immediately rather than hang) falls back
    to the non-TUI behavior within a bounded timeout instead of silently dropping every
    heartbeat/retry notice -- acceptance criterion #4."""
    reason = _preflight_skip()
    if reason:
        _skip(reason)
        return
    with tempfile.TemporaryDirectory() as d:
        repo = _make_repo(Path(d))
        rc, output, elapsed = _run_under_pty(
            [
                sys.executable, str(REVIEW_BIN), "diff",
                "--task", "TEST-548-B", "--pool", "2",
            ],
            _env(term="unknown", fake_delay=0.8),
            cwd=str(repo),
        )
    text = output.decode("utf-8", "replace")
    assert rc == 0, f"rc={rc}\n{text[:4000]}"
    for marker in ("live board", "ROLE", "ELAPSED"):
        assert marker not in text, f"unexpected TUI marker {marker!r} in:\n{text[:4000]}"
    assert "a concrete, deterministic suggestion" in text, text[:4000]
    # Bounded, not hanging: comfortably above the fake delay, comfortably below the pty
    # runner's own 40s hard timeout (which would have killed the process, not let it exit
    # cleanly at rc=0).
    assert elapsed < 15.0, f"fallback took too long: {elapsed:.2f}s"


def test_no_tui_flag_disables_under_real_pty():
    """--no-tui disables the display even under a fully-capable real pty -- acceptance
    criterion #3 (the flag half)."""
    reason = _preflight_skip()
    if reason:
        _skip(reason)
        return
    with tempfile.TemporaryDirectory() as d:
        repo = _make_repo(Path(d))
        rc, output, elapsed = _run_under_pty(
            [
                sys.executable, str(REVIEW_BIN), "diff",
                "--task", "TEST-548-C", "--pool", "2", "--no-tui",
            ],
            _env(term="xterm", fake_delay=0.8),
            cwd=str(repo),
        )
    text = output.decode("utf-8", "replace")
    assert rc == 0, f"rc={rc}\n{text[:4000]}"
    for marker in ("live board", "ROLE", "ELAPSED"):
        assert marker not in text, f"unexpected TUI marker {marker!r} in:\n{text[:4000]}"
    assert "a concrete, deterministic suggestion" in text, text[:4000]
    assert elapsed < 20.0, elapsed


def test_no_tui_env_disables_under_real_pty():
    """$REVIEW_NO_TUI=1 disables the display even under a fully-capable real pty --
    acceptance criterion #3 (the env-var half)."""
    reason = _preflight_skip()
    if reason:
        _skip(reason)
        return
    with tempfile.TemporaryDirectory() as d:
        repo = _make_repo(Path(d))
        rc, output, elapsed = _run_under_pty(
            [
                sys.executable, str(REVIEW_BIN), "diff",
                "--task", "TEST-548-D", "--pool", "2",
            ],
            _env(term="xterm", fake_delay=0.8, extra={"REVIEW_NO_TUI": "1"}),
            cwd=str(repo),
        )
    text = output.decode("utf-8", "replace")
    assert rc == 0, f"rc={rc}\n{text[:4000]}"
    for marker in ("live board", "ROLE", "ELAPSED"):
        assert marker not in text, f"unexpected TUI marker {marker!r} in:\n{text[:4000]}"
    assert "a concrete, deterministic suggestion" in text, text[:4000]
    assert elapsed < 20.0, elapsed


_TESTS = [
    test_tui_activates_under_real_pty,
    test_broken_terminal_falls_back_within_bounded_time,
    test_no_tui_flag_disables_under_real_pty,
    test_no_tui_env_disables_under_real_pty,
]


if __name__ == "__main__":
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failures:
        print(f"{failures} failure(s)")
        sys.exit(1)
    print("all tests passed")
