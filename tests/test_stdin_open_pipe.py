#!/usr/bin/env python3
"""RC-3 residuals: `review diff` must never hang on an OPEN, never-EOF stdin pipe.

The hatch-flood incident shape (review-cli#516): a detached agent process
(hub start application=bash ..., pty=false) inherits a stdin that is a pipe whose
write end nobody ever closes and nobody ever writes to. `_read_stdin_if_piped`
treated "not a tty" as "a diff is being piped in" and called `sys.stdin.read()`,
which blocks until EOF — i.e. forever in that environment. The review then never
printed anything, never recorded a stats iteration, never refreshed the
commit-gate marker, and the agent waited on it indefinitely.

Coverage:
  * unit: `_read_stdin_if_piped` itself, handed a REAL blocking pipe as
    sys.stdin (write end held open, nothing written), must return None within a
    watchdog window instead of blocking. Run in a worker thread so a regression
    fails the assertion (watchdog expiry) rather than wedging the whole suite.
  * end-to-end: the real `bin/review diff` SUBPROCESS with `sleep <big> |`
    stdin in a diff-less repo (no seat dispatch — the stall sits upstream of
    mode dispatch, so a no-diff run still proves the stdin read completed) must
    finish with its normal "No diff to review." exit 1, not hang.
  * persistence: a staged review through the real CLI entry point in a child
    process, with only model dispatch stubbed, must record its stats iteration,
    refresh a stale marker, and stamp the staged diff while stdin stays silent.

Same harness style as tests/test_run_stats.py: plain test_* functions runnable
via pytest or the __main__ block; no live model call is ever made.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

os.environ.setdefault(
    "REVIEW_PROVIDER_CACHE", str(Path(tempfile.mkdtemp()) / "last-provider.json")
)
from _failover_neutralise import identity_provider_chain  # noqa: E402

# Generous for CI: the fixed path returns in milliseconds; only a genuine
# blocking read blows through this.
_WATCHDOG_SECONDS = 5.0


def _blocking_stdin() -> tuple[io.TextIOWrapper, int]:
    """A REAL never-EOF stdin: the read end of an os.pipe whose write end the
    caller must hold open for the duration of the call under test."""
    r_fd, w_fd = os.pipe()
    return io.TextIOWrapper(io.FileIO(r_fd, "r"), encoding="utf-8"), w_fd


def test_stdin_wait_rejects_nonfinite_values():
    """Non-finite configuration cannot bypass the bounded stdin wait."""
    from reviewlib.cli import _STDIN_WAIT_DEFAULT, _stdin_wait_seconds

    old = os.environ.get("REVIEW_STDIN_WAIT_SECONDS")
    try:
        for raw in ("inf", "1e999", "-inf", "nan"):
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = raw
            wait = _stdin_wait_seconds()
            assert math.isfinite(wait) and wait == _STDIN_WAIT_DEFAULT, (
                f"{raw!r} must use finite default {_STDIN_WAIT_DEFAULT}, got {wait}"
            )
    finally:
        if old is None:
            os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
        else:
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = old


def test_stdin_wait_rejects_excessive_finite_values():
    """Huge finite values cannot overflow select or leave stdin waiting for years."""
    from reviewlib.cli import _STDIN_WAIT_DEFAULT, _stdin_wait_seconds

    for raw in ("60.01", "86400", "1e308"):
        with patch.dict(os.environ, {"REVIEW_STDIN_WAIT_SECONDS": raw}):
            wait = _stdin_wait_seconds()
        assert wait == _STDIN_WAIT_DEFAULT, (raw, wait)


def test_stdin_wait_preserves_finite_values_and_zero():
    """Finite negatives still clamp to zero; zero remains an immediate poll."""
    from reviewlib.cli import _stdin_wait_seconds

    old = os.environ.get("REVIEW_STDIN_WAIT_SECONDS")
    try:
        for raw, expected in (("-1", 0.0), ("0", 0.0), ("0.25", 0.25), ("60", 60.0)):
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = raw
            wait = _stdin_wait_seconds()
            assert wait == expected, f"{raw!r}: expected {expected}, got {wait}"
    finally:
        if old is None:
            os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
        else:
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = old


def test_huge_stdin_wait_finishes_on_real_open_pipe():
    """A huge finite setting reaches select safely and exits before the watchdog."""
    stdin, write_fd = _blocking_stdin()
    try:
        result = subprocess.run(
            [sys.executable, "-c", "from reviewlib.cli import _read_stdin_if_piped; assert _read_stdin_if_piped() is None"],
            cwd=REPO_ROOT,
            stdin=stdin,
            capture_output=True,
            text=True,
            env={**os.environ, "REVIEW_STDIN_WAIT_SECONDS": "1e308"},
            timeout=_WATCHDOG_SECONDS,
        )
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    finally:
        os.close(write_fd)
        stdin.close()


def test_partial_open_fifo_returns_data_before_deadline():
    """One ready byte must not turn the bounded wait into a read-until-EOF."""
    for wait in ("0", "0.1"):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "stdin"
            os.mkfifo(fifo)
            read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            write_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(read_fd, True)
            try:
                os.write(write_fd, b"x")
                result = subprocess.run(
                    [sys.executable, "-c", (
                        "import os, time; from reviewlib.cli import _read_stdin_if_piped; "
                        "started = time.monotonic(); "
                        "assert _read_stdin_if_piped() == 'x'; "
                        "assert time.monotonic() - started < 0.75; "
                        "assert os.get_blocking(0)"
                    )],
                    cwd=REPO_ROOT, stdin=read_fd, capture_output=True, text=True,
                    env={**os.environ, "REVIEW_STDIN_WAIT_SECONDS": wait},
                    timeout=_WATCHDOG_SECONDS,
                )
                assert result.returncode == 0, result.stderr
            finally:
                os.close(write_fd)
                os.close(read_fd)


def test_trickling_socket_cannot_extend_total_deadline():
    """Repeated readiness must not reset the deadline after every chunk."""
    script = """
import io, socket, sys, threading, time
from reviewlib.cli import _read_stdin_if_piped
reader, writer = socket.socketpair()
sys.stdin = io.TextIOWrapper(reader.makefile('rb'), encoding='utf-8')
def produce():
    for _ in range(100):
        writer.sendall(b'x')
        time.sleep(0.02)
threading.Thread(target=produce, daemon=True).start()
started = time.monotonic()
data = _read_stdin_if_piped()
elapsed = time.monotonic() - started
assert data and set(data) == {'x'}, data
assert 0.08 <= elapsed < 0.5, elapsed
assert len(data) < 100, len(data)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT,
        capture_output=True, text=True,
        env={**os.environ, "REVIEW_STDIN_WAIT_SECONDS": "0.1"},
        timeout=_WATCHDOG_SECONDS,
    )
    assert result.returncode == 0, result.stderr


def test_read_stdin_if_piped_does_not_block_on_open_pipe():
    """The unit-level red test for review-cli#516: a non-tty stdin with no data
    and no EOF must resolve to `None` (no piped diff) within the watchdog, not
    block on read()."""
    from reviewlib.cli import _read_stdin_if_piped

    fake_stdin, w_fd = _blocking_stdin()
    old_stdin = sys.stdin
    outcome: dict = {}

    def _call():
        sys.stdin = fake_stdin
        try:
            outcome["value"] = _read_stdin_if_piped()
        except BaseException as exc:  # noqa: BLE001 - record, never wedge the suite
            outcome["error"] = exc
        finally:
            sys.stdin = old_stdin

    worker = threading.Thread(target=_call, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(_WATCHDOG_SECONDS)
    try:
        assert not worker.is_alive(), (
            "_read_stdin_if_piped blocked for more than "
            f"{_WATCHDOG_SECONDS}s on an open, never-EOF stdin pipe "
            "(review-cli#516: this is the hatch-flood detached-agent hang)"
        )
        assert "error" not in outcome, outcome["error"]
        assert outcome.get("value") is None, (
            f"an open pipe with no data is not a piped diff; expected None, "
            f"got {outcome.get('value')!r}"
        )
        elapsed = time.monotonic() - started
        assert elapsed < _WATCHDOG_SECONDS, elapsed
    finally:
        os.close(w_fd)
        try:
            fake_stdin.close()
        except OSError:
            pass


def test_read_stdin_if_piped_select_failure_does_not_read_open_fifo():
    """A failed readiness check must never direct-read a silent, open FIFO."""
    from reviewlib.cli import _read_stdin_if_piped

    stdin, write_fd = _blocking_stdin()
    try:
        for error in (OSError("select failed"), ValueError("invalid fd"), ImportError("no select")):
            with (
                patch.object(sys, "stdin", stdin),
                patch("select.select", side_effect=error) as select_mock,
                patch.object(stdin, "read", side_effect=AssertionError("read on open FIFO")) as read_mock,
            ):
                started = time.monotonic()
                assert _read_stdin_if_piped() is None
                assert time.monotonic() - started < _WATCHDOG_SECONDS
                select_mock.assert_called_once()
                read_mock.assert_not_called()
    finally:
        os.close(write_fd)
        stdin.close()


def test_read_stdin_if_piped_preserves_regular_file_and_devnull():
    """Regular-file and /dev/null redirects do not depend on select working."""
    from reviewlib.cli import _read_stdin_if_piped

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdin:
        stdin.write("diff --git a/f b/f\n+hello\n")
        stdin.seek(0)
        with patch.object(sys, "stdin", stdin), patch("select.select", side_effect=OSError) as select_mock:
            assert _read_stdin_if_piped() == "diff --git a/f b/f\n+hello\n"
            select_mock.assert_not_called()
    with open(os.devnull, encoding="utf-8") as stdin:
        with patch.object(sys, "stdin", stdin), patch("select.select", side_effect=OSError) as select_mock:
            assert _read_stdin_if_piped() is None
            select_mock.assert_not_called()


def test_read_stdin_if_piped_still_reads_a_real_pipe():
    """Guard the other side of the contract: an actually-piped diff (data, then
    EOF when the producer exits) is still consumed verbatim. `os.pipe` with the
    write end closed after writing is exactly the `git diff | review` shape."""
    from reviewlib.cli import _read_stdin_if_piped

    r_fd, w_fd = os.pipe()
    os.write(w_fd, b"diff --git a/f b/f\n+hello\n")
    os.close(w_fd)
    fake_stdin = io.TextIOWrapper(io.FileIO(r_fd, "r"), encoding="utf-8")
    old_stdin = sys.stdin
    sys.stdin = fake_stdin
    try:
        value = _read_stdin_if_piped()
    finally:
        sys.stdin = old_stdin
        try:
            fake_stdin.close()
        except OSError:
            pass
    assert value == "diff --git a/f b/f\n+hello\n", value


def _diffless_repo() -> tempfile.TemporaryDirectory:
    d = tempfile.TemporaryDirectory()
    repo = Path(d.name)
    env = {**os.environ, "REVIEW_SKIP": "1"}

    def run(*argv: str) -> None:
        subprocess.run(argv, cwd=str(repo), check=True, env=env, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "core.hooksPath", "/dev/null")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("base\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "chore: init diffless scratch repo")
    return d


def test_review_diff_subprocess_survives_open_stdin_pipe():
    """End-to-end red test for review-cli#516: the real binary, launched the way
    a detached pty=false agent launches it (stdin = an open pipe that never
    delivers data or EOF), must complete. A diff-less repo keeps this seat-free:
    the stall sits in cli.py's pre-dispatch stdin read, so the run reaching its
    normal `No diff to review.` exit 1 proves the stdin read returned."""
    d = _diffless_repo()
    env = {
        **os.environ,
        "REVIEW_SKIP": "1",
        "REVIEW_STATS_FILE": str(Path(d.name) / "stats.jsonl"),
        "REVIEW_MARKER": str(Path(d.name) / "marker"),
        "REVIEW_LOG_DIR": str(Path(d.name) / "logs"),
        # Shrink every per-call/backstop cap so a regression cannot make the
        # watchdog kill look like a slow success.
        "REVIEW_IDLE_TIMEOUT_SECONDS": "2",
    }
    try:
        holder = subprocess.Popen(
            ["sleep", "300"], stdout=subprocess.PIPE
        )  # holds the pipe's write end open; never writes, never EOFs
        try:
            proc = subprocess.run(
                [
                    str(REPO_ROOT / "bin" / "review"),
                    "diff",
                    "--task",
                    "RC3-OPENPIPE",
                    "-C",
                    d.name,
                ],
                stdin=holder.stdout,  # a pipe whose write end `sleep` holds open
                capture_output=True,
                text=True,
                timeout=_WATCHDOG_SECONDS * 4,
                env=env,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        assert "No diff to review." in proc.stderr, proc.stderr
        assert proc.returncode == 1, proc.returncode
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "bin/review diff hung on an open never-EOF stdin pipe "
            "(review-cli#516: detached-agent startup hang)"
        ) from None
    finally:
        d.cleanup()


def test_staged_review_persists_with_open_stdin_pipe():
    """Exercise stdin fallback, dispatch, stats and gate writes in one process."""
    with _diffless_repo() as repo_name:
        repo = Path(repo_name)
        (repo / "f.txt").write_text("base\nchanged\n")
        subprocess.run(
            ["git", "add", "f.txt"], cwd=repo, check=True, capture_output=True,
            timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--cached"], cwd=repo,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
        marker = repo / "marker"
        marker.touch()
        os.utime(marker, (1, 1))
        env = {
            **os.environ,
            "REVIEW_STATS_FILE": str(repo / "stats.jsonl"),
            "REVIEW_MARKER": str(marker),
            "REVIEW_LOG_DIR": str(repo / "logs"),
            "REVIEW_PROVIDER_CACHE": str(repo / "provider.json"),
            "REVIEW_STDIN_WAIT_SECONDS": "0.1",
        }
        # A child interpreter lets us replace only the model boundary while
        # retaining real stdin handling, CLI dispatch and persistence writes.
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
sys.path.insert(0, str(Path(sys.path[0]) / 'tests'))
from reviewlib import backends, cli, panel
from reviewlib.modes import review
from _failover_neutralise import identity_provider_chain

def backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
    (cwd / 'received.diff').write_text(diff)
    return backends.ReviewResult(
        model=model, command='stub', returncode=0,
        stdout='checked: staged f.txt change preserves the base line; no issues.',
        stderr='', prompt_tokens=1, output_tokens=1,
    )

for module in (backends, panel, review):
    module.resolve_backend = lambda model: backend
    module.backend_available = lambda model: True
with identity_provider_chain():
    raise SystemExit(cli.main(sys.argv[1:]))
"""
        stdin, write_fd = _blocking_stdin()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script, str(REPO_ROOT), "diff", "--task",
                 "RC3-OPENPIPE", "-C", repo_name, "--staged", "-m", "stubseat"],
                stdin=stdin, capture_output=True, text=True, env=env,
                timeout=_WATCHDOG_SECONDS * 4,
            )
        finally:
            os.close(write_fd)
            stdin.close()
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        received = (repo / "received.diff").read_text()
        assert "+changed" in received and "f.txt" in received, received
        records = [json.loads(line) for line in (repo / "stats.jsonl").read_text().splitlines()]
        assert len(records) == 1, records
        record = records[0]
        assert record["task_code"] == "RC3-OPENPIPE", record
        assert record["mode"] == "review" and record["passed"] is True, record
        assert set(record["models"]) == {"stubseat"}, record
        assert record["pool_size"] == len(record["models"]), record
        assert record["diff_files"] == ["f.txt"], record
        assert marker.is_file() and marker.stat().st_mtime > 1, marker.stat()
        assert (repo / ".git" / "review-stamp").read_text().strip() == hashlib.sha256(
            staged.encode("utf-8")
        ).hexdigest()


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with identity_provider_chain():
                    fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
