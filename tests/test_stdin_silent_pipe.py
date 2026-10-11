#!/usr/bin/env python3
"""Regression tests for review-cli#516 / #613: `review` must never park forever on a
non-tty stdin whose writer stays open and never reaches EOF.

The hang shape seen live: a review launched from a long-lived parent (an omp eval
kernel, a detached `pty=false` agent session, a pre-commit hook) inherits a unix socket
or pipe as fd 0. `sys.stdin.read()` ran unconditionally before any dispatch, so the
process sat in `unix_stream_data_wait` for 42+ minutes holding the host-wide review
lock, with 34 other reviews queued behind it (#613).

Contracts, exercised against the REAL `bin/review` subprocess with a REAL socketpair /
pipe on fd 0 (an in-process StringIO cannot reproduce a blocking read):

  * `review diff --staged` / `review diff --diff` (explicit git sources) do NOT read
    stdin at all — they complete immediately even with `REVIEW_STDIN_WAIT_SECONDS` set
    absurdly high; a diff piped in anyway is ignored with one stderr line.
  * A verb with no explicit source (`review diff`) reads stdin with an idle timeout,
    says so once on stderr, and falls back to the working-tree diff — for a fully silent
    writer AND for one that trickles a few bytes and then stalls.
  * The ordinary `git diff | review diff` pipe (data promptly, EOF at producer exit)
    is still consumed verbatim; `REVIEW_STDIN_WAIT_SECONDS=0` still accepts data that
    is already waiting.

Plus unit coverage of the read primitive and the env knob parsing.
"""

from __future__ import annotations

import io
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

# A cold interpreter + the fake backend finish in ~1 s; the "forever" the bug produced
# is what the budget guards against. The subprocess timeout is a hard backstop so a
# regression fails instead of wedging the suite.
_WALL_LIMIT = 30
_SUBPROCESS_TIMEOUT = 2 * _WALL_LIMIT

# Fragments of the two stderr notices `reviewlib.cli` prints (`_read_fd_with_idle_timeout`,
# `_warn_if_stdin_ignored`), kept in one place so a copy edit is a one-line test change.
SILENT_NOTICE = "treating it as no piped input"
IGNORED_NOTICE = "ignoring piped stdin"

PIPED_DIFF = (
    "diff --git a/piped.txt b/piped.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/piped.txt\n"
    "@@ -0,0 +1 @@\n"
    "+only via stdin\n"
)


def _make_repo(tmp: Path) -> Path:
    """A throwaway git repo with one staged (a.txt) AND one unstaged (b.txt) change."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    (repo / "b.txt").write_text("tree\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    (repo / "b.txt").write_text("tree edited\n", encoding="utf-8")
    return repo


def _diff_files(tmp: Path) -> list[str]:
    """`diff_files` of the single run-stats record the review wrote (the fake backend
    does not echo the diff, so the stats record is how we see WHICH diff was reviewed)."""
    lines = (tmp / "logs" / "stats.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines if ln.strip()]
    assert len(records) == 1, records
    return records[0].get("diff_files") or []


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["REVIEW_FAKE_BACKEND"] = "1"
    env["REVIEW_LOG_DIR"] = str(tmp / "logs")
    env["REVIEW_STATS_FILE"] = str(tmp / "logs" / "stats.jsonl")
    env["REVIEW_JOBS_DIR"] = str(tmp / "jobs")
    env["REVIEW_TASK_CODE"] = "TEST-613"
    env["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    env.pop("REVIEW_STDIN_WAIT_SECONDS", None)
    env.update(extra)
    return env


class _Run:
    def __init__(self, proc: subprocess.CompletedProcess[bytes], wall: float):
        self.returncode = proc.returncode
        self.stderr = proc.stderr.decode("utf-8", "replace")
        self.wall = wall


def _run_with_socket_stdin(
    args: list[str], env: dict[str, str], *, write: bytes = b"", close: bool = False
) -> _Run:
    """Spawn `review ARGS` with fd 0 = one end of a socketpair. The other end stays in
    this test: `write` goes out right after spawn, then the end is closed (`close=True`,
    a producer that exits) or HELD OPEN and silent for the child's whole life — the
    exact wchan=unix_stream_data_wait shape from #613."""
    held_open, theirs = socket.socketpair()
    try:
        started = time.monotonic()
        with subprocess.Popen(
            [sys.executable, str(REVIEW_BIN), *args],
            stdin=theirs.fileno(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            theirs.close()
            if write:
                held_open.sendall(write)
            if close:
                held_open.close()
            try:
                out, err = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise AssertionError(
                    f"review hung for {_SUBPROCESS_TIMEOUT}s on stdin: {args}"
                )
        completed = subprocess.CompletedProcess(args, proc.returncode, out, err)
        return _Run(completed, time.monotonic() - started)
    finally:
        if not close:
            held_open.close()


def _diff_args(repo: Path, *extra: str) -> list[str]:
    return [
        "diff",
        *extra,
        "-C",
        str(repo),
        "-m",
        "claude:claude-opus-4-8",
        "--no-tui",
    ]


def _assert_explicit_source_skips_stdin(flag: str, expected_files: list[str]) -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "result.md"
        run = _run_with_socket_stdin(
            _diff_args(repo, flag, "-o", str(out)),
            _env(tmp, REVIEW_STDIN_WAIT_SECONDS="3600"),
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert run.wall < _WALL_LIMIT, (
            f"{flag} took {run.wall:.1f}s with a silent stdin: {run.stderr}"
        )
        assert SILENT_NOTICE not in run.stderr, (
            f"{flag} must not even poll stdin:\n" + run.stderr
        )
        assert IGNORED_NOTICE not in run.stderr, run.stderr
        assert out.exists(), "the review never produced its -o result"
        assert _diff_files(tmp) == expected_files


def test_staged_review_with_silent_socket_stdin_never_reads_stdin():
    """`review diff --staged` + a silent open socket on fd 0 completes promptly. The
    wait knob is an hour to prove the explicit-source path does not poll stdin at all
    (a bounded poll would still burn the full window here)."""
    _assert_explicit_source_skips_stdin("--staged", ["a.txt"])


def test_diff_flag_review_with_silent_socket_stdin_never_reads_stdin():
    """Same for `--diff` (the working-tree spelling of an explicit source)."""
    _assert_explicit_source_skips_stdin("--diff", ["b.txt"])


def test_staged_review_ignores_a_piped_diff_and_says_so():
    """`printf DIFF | review diff --staged`: the explicit source wins — the INDEX is
    reviewed, not the pipe — and the caller is told on stderr that stdin was ignored."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "staged.md"
        run = _run_with_socket_stdin(
            _diff_args(repo, "--staged", "-o", str(out)),
            _env(tmp),
            write=PIPED_DIFF.encode("utf-8"),
            close=True,
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert IGNORED_NOTICE in run.stderr, run.stderr
        assert "--staged" in run.stderr, run.stderr
        assert _diff_files(tmp) == ["a.txt"], "the index must be reviewed, not the pipe"


def test_staged_review_with_eof_stdin_is_quiet():
    """`review diff --staged < /dev/null` (and `true | …`): EOF is not piped data, so no
    "ignoring piped stdin" line — hooks and wrappers that redirect stdin stay quiet."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        run = _run_with_socket_stdin(
            _diff_args(repo, "--staged", "-o", str(tmp / "o.md")),
            _env(tmp),
            close=True,
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert IGNORED_NOTICE not in run.stderr, run.stderr
        assert SILENT_NOTICE not in run.stderr, run.stderr


def test_unsourced_review_with_silent_socket_stdin_falls_back_after_bounded_wait():
    """`review diff` (no explicit source) + a silent open socket: wait the configured
    window, print the one-line notice, then review the working-tree diff."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "tree.md"
        run = _run_with_socket_stdin(
            _diff_args(repo, "-o", str(out)),
            _env(tmp, REVIEW_STDIN_WAIT_SECONDS="1"),
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert 1.0 <= run.wall < _WALL_LIMIT, f"wall {run.wall:.1f}s: {run.stderr}"
        assert SILENT_NOTICE in run.stderr, run.stderr
        assert "nothing arrived" in run.stderr, run.stderr
        assert "REVIEW_STDIN_WAIT_SECONDS" in run.stderr, run.stderr
        assert out.exists(), "the review never produced its -o result"
        assert _diff_files(tmp) == ["b.txt"], "the working-tree diff must be reviewed"


def test_unsourced_review_with_trickling_then_stalled_stdin_falls_back():
    """A launcher that writes a greeting byte or two and then holds the fd open is the
    #613 shape with one byte of data: the first `select` is satisfied, so the bound must
    cover the whole read (idle timeout), not just the first byte."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "tree.md"
        run = _run_with_socket_stdin(
            _diff_args(repo, "-o", str(out)),
            _env(tmp, REVIEW_STDIN_WAIT_SECONDS="1"),
            write=b"ready\n",
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert 1.0 <= run.wall < _WALL_LIMIT, f"wall {run.wall:.1f}s: {run.stderr}"
        assert SILENT_NOTICE in run.stderr, run.stderr
        assert "only 6 bytes arrived" in run.stderr, run.stderr
        assert _diff_files(tmp) == ["b.txt"], (
            "a stalled partial stdin must not be reviewed as the diff"
        )


def test_piped_diff_is_still_consumed_verbatim():
    """`printf DIFF | review diff` — data at once, EOF when the producer exits — must
    still review the PIPED diff (the stdin-wins precedence without an explicit source is
    unchanged), with neither notice printed."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "piped.md"
        run = _run_with_socket_stdin(
            _diff_args(repo, "-o", str(out)),
            _env(tmp, REVIEW_STDIN_WAIT_SECONDS="1"),
            write=PIPED_DIFF.encode("utf-8"),
            close=True,
        )
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert SILENT_NOTICE not in run.stderr, run.stderr
        assert IGNORED_NOTICE not in run.stderr, run.stderr
        assert out.exists(), "the review never produced its -o result"
        assert _diff_files(tmp) == ["piped.txt"], "the piped diff must win over the tree"


def test_zero_wait_still_accepts_data_already_waiting():
    """`REVIEW_STDIN_WAIT_SECONDS=0` = "only data already waiting counts": a producer that
    has finished before review starts is still consumed."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        out = tmp / "piped.md"
        proc = subprocess.run(
            [sys.executable, str(REVIEW_BIN), *_diff_args(repo, "-o", str(out))],
            input=PIPED_DIFF.encode("utf-8"),
            env=_env(tmp, REVIEW_STDIN_WAIT_SECONDS="0"),
            check=False,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 0, (proc.returncode, stderr)
        assert _diff_files(tmp) == ["piped.txt"], stderr


def _call_bounded(fn, *args, budget: float = 10.0, **kwargs):
    """Run `fn(*args, **kwargs)` on a thread and FAIL (not hang) if it outlives
    `budget` — the shape the in-process tests guard against is an unbounded blocking
    read."""
    box: dict = {}

    def _run() -> None:
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(budget)
    assert not worker.is_alive(), f"blocked for more than {budget}s"
    if "error" in box:
        raise box["error"]
    return box["result"]


# `_read_fd_with_idle_timeout` has two chunk sources: `select` (POSIX pipes/sockets) and
# the reader-thread fallback for fds `select` cannot poll (Windows pipes, fd >= FD_SETSIZE).
# Both must honour the same idle-timeout contract, so every primitive test runs twice.
_CHUNK_SOURCES = (True, False)


def test_read_fd_with_idle_timeout_silent_pipe_returns_none():
    from contextlib import redirect_stderr

    from reviewlib import cli

    for pollable in _CHUNK_SOURCES:
        r, w = os.pipe()
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                result = _call_bounded(cli._read_fd_with_idle_timeout, r, 0.2, pollable=pollable)
        finally:
            os.close(w)
            os.close(r)
        assert result is None, pollable
        assert err.getvalue().count(SILENT_NOTICE) == 1, (pollable, err.getvalue())


def test_read_fd_with_idle_timeout_trickle_then_stall_returns_none():
    from contextlib import redirect_stderr

    from reviewlib import cli

    for pollable in _CHUNK_SOURCES:
        r, w = os.pipe()
        err = io.StringIO()
        try:
            os.write(w, b"abc")
            with redirect_stderr(err):
                result = _call_bounded(cli._read_fd_with_idle_timeout, r, 0.2, pollable=pollable)
        finally:
            os.close(w)
            os.close(r)
        assert result is None, pollable
        assert "only 3 bytes arrived" in err.getvalue(), (pollable, err.getvalue())


def test_read_fd_with_idle_timeout_reads_a_slow_but_live_producer_to_eof():
    """The window is an IDLE timeout: a producer that keeps writing (each gap shorter
    than the window) is read to EOF even when the whole stream takes longer than it."""
    from reviewlib import cli

    for pollable in _CHUNK_SOURCES:
        r, w = os.pipe()

        def _produce(w=w) -> None:
            for i in range(4):
                time.sleep(0.1)
                os.write(w, f"part{i}\n".encode())
            os.close(w)

        threading.Thread(target=_produce, daemon=True).start()
        try:
            result = _call_bounded(cli._read_fd_with_idle_timeout, r, 0.3, pollable=pollable)
        finally:
            os.close(r)
        assert result == b"part0\npart1\npart2\npart3\n", (pollable, result)


def test_read_error_after_a_chunk_discards_the_partial_diff():
    """A socket that delivers part of a diff and then resets (ECONNRESET) is a FAILED
    read, not EOF: the prefix must not be reviewed as if it were the whole diff."""
    from contextlib import redirect_stderr

    from reviewlib import cli

    for pollable in _CHUNK_SOURCES:
        ours, theirs = socket.socketpair()
        err = io.StringIO()
        try:
            ours.sendall(b"diff --git a/x b/x\n")
            # Linux AF_UNIX: closing a stream socket that still holds UNREAD inbound data
            # resets the peer — its next read fails with ECONNRESET instead of EOF.
            theirs.sendall(b"unread")
            with redirect_stderr(err):
                result = _call_bounded(
                    _read_after_reset, cli, theirs.fileno(), ours, pollable=pollable
                )
        finally:
            theirs.close()
        assert result is None, (pollable, result)
        assert "stdin read failed after 19 bytes" in err.getvalue(), (
            pollable,
            err.getvalue(),
        )


def _read_after_reset(cli, fd: int, writer: socket.socket, *, pollable: bool):
    """Reset the writer side a moment after the reader has consumed the first chunk."""

    def _reset() -> None:
        time.sleep(0.15)
        writer.close()

    threading.Thread(target=_reset, daemon=True).start()
    return cli._read_fd_with_idle_timeout(fd, 2.0, pollable=pollable)


def test_threaded_zero_wait_still_reads_a_preloaded_closed_pipe():
    """`REVIEW_STDIN_WAIT_SECONDS=0` on the thread fallback: bytes a finished producer
    left in the pipe must be handed over despite thread scheduling (the floor wait),
    so the documented "data already waiting counts" holds on both chunk sources."""
    from reviewlib import cli

    for _ in range(20):
        r, w = os.pipe()
        os.write(w, b"preloaded")
        os.close(w)
        try:
            result = _call_bounded(
                cli._read_fd_with_idle_timeout, r, 0.0, pollable=False
            )
        finally:
            os.close(r)
        assert result == b"preloaded", result


def test_threaded_reader_stops_draining_after_giving_up():
    """Once the thread fallback gives up, its reader must not keep swallowing the
    stream for the life of the process: a writer that trickles forever would otherwise
    grow an unbounded buffer with no backpressure. After the give-up, at most one more
    chunk is consumed (the read the thread was already blocked in); everything after
    that stays in the pipe."""
    from contextlib import redirect_stderr

    from reviewlib import cli

    r, w = os.pipe()
    try:
        with redirect_stderr(io.StringIO()):
            result = _call_bounded(
                cli._read_fd_with_idle_timeout, r, 0.2, pollable=False
            )
            assert result is None
        os.write(w, b"wakeup")  # the blocked read returns; the thread sees stop and exits
        time.sleep(0.2)
        os.write(w, b"stays")
        time.sleep(0.2)
        ready, _, _ = select.select([r], [], [], 0)
        assert ready, "the stopped reader must leave later data in the pipe"
        assert os.read(r, 64) == b"stays"
    finally:
        os.close(w)
        os.close(r)


def test_read_stdin_if_piped_with_silent_pipe_fd_returns_none_in_process():
    """The CLI-level function against a real pipe fd held open with no writer."""
    from contextlib import redirect_stderr

    from reviewlib import cli

    r, w = os.pipe()
    saved_stdin = sys.stdin
    saved_env = os.environ.get("REVIEW_STDIN_WAIT_SECONDS")
    os.environ["REVIEW_STDIN_WAIT_SECONDS"] = "0.2"
    err = io.StringIO()
    try:
        sys.stdin = os.fdopen(r, "r")
        with redirect_stderr(err):
            assert _call_bounded(cli._read_stdin_if_piped) is None
    finally:
        sys.stdin.close()
        sys.stdin = saved_stdin
        os.close(w)
        if saved_env is None:
            os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
        else:
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = saved_env
    assert err.getvalue().count(SILENT_NOTICE) == 1, err.getvalue()


def test_closed_stdin_object_is_no_piped_input():
    """A daemonizer that closes the Python-level `sys.stdin` (not just fd 0): neither
    helper may raise `ValueError: I/O operation on closed file` out of dispatch."""
    from reviewlib import cli

    r, w = os.pipe()
    saved_stdin = sys.stdin
    closed = os.fdopen(r, "r")
    closed.close()
    try:
        sys.stdin = closed
        assert cli._read_stdin_if_piped() is None
        cli._warn_if_stdin_ignored("--staged", "staged")  # must not raise
    finally:
        sys.stdin = saved_stdin
        os.close(w)


def test_invalid_utf8_on_stdin_is_replaced_not_raised():
    """`printf '\\xff bad' | review diff`: a stray undecodable byte must not escape as a
    UnicodeDecodeError (sys.stdin's own errors mode is normally "strict")."""
    from reviewlib import cli

    r, w = os.pipe()
    saved_stdin = sys.stdin
    try:
        os.write(w, b"diff --git a/x b/x\n+\xff bad\n")
        os.close(w)
        sys.stdin = os.fdopen(r, "r", encoding="utf-8", errors="strict")
        text = _call_bounded(cli._read_stdin_if_piped)
    finally:
        sys.stdin.close()
        sys.stdin = saved_stdin
    assert text is not None and text.startswith("diff --git a/x b/x\n+\ufffd bad"), text


def test_stdin_wait_seconds_env_parsing():
    from reviewlib import cli

    saved = os.environ.get("REVIEW_STDIN_WAIT_SECONDS")
    cases = {
        None: cli.STDIN_WAIT_DEFAULT_SECONDS,
        "": cli.STDIN_WAIT_DEFAULT_SECONDS,
        "abc": cli.STDIN_WAIT_DEFAULT_SECONDS,
        "nan": cli.STDIN_WAIT_DEFAULT_SECONDS,
        "inf": cli.STDIN_WAIT_DEFAULT_SECONDS,
        "-inf": cli.STDIN_WAIT_DEFAULT_SECONDS,
        "1e3": 1000.0,
        "2.5": 2.5,
        "0": 0.0,
        "-3": 0.0,
    }
    try:
        for raw, expected in cases.items():
            if raw is None:
                os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
            else:
                os.environ["REVIEW_STDIN_WAIT_SECONDS"] = raw
            assert cli._stdin_wait_seconds() == expected, (raw, expected)
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
        else:
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
