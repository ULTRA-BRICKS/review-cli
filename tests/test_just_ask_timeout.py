#!/usr/bin/env python3
"""`review just-ask --timeout N` is a hard wall-clock bound on the whole command
(review-cli#587).

The incident (2026-09-29): a just-ask proofread printed nothing for 25 minutes, and a
retry with `-m claude:claude-opus-4-8 --timeout 300` was still running after 600 s.
`--timeout` was an idle/silence window with a 20-minute floor, reset by any output, and
it multiplied across the seat's role slots, provider failover and retries. These tests
pin the fix:

  * a seat that never answers is killed at the N-second deadline (its whole process
    group, grandchildren included), and the command exits non-zero within N plus the
    30 s grace, printing each seat it waited on and why it gave no answer;
  * a mixed panel (one seat out of quota, one silent) names both reasons;
  * one explicit `-m` model makes exactly one call (no role-slot padding);
  * a seat wedged inside review itself (where no per-call kill reaches) is still cut
    off at N + grace by the watchdog, which prints the seats still running and their
    last activity, reaps the children and exits 124.

The seats are fake `codex` executables on a private PATH (a shell script that prints
progress or nothing and sleeps, with a sleeping grandchild in its process group); the
real `bin/review` runs as a subprocess with HOME, logs, stats, cooldown and provider
caches in a temp dir. No model is called.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# The only variables taken from the runner's environment: what a relocated interpreter
# needs to load at all (a CI Python needs its LD_LIBRARY_PATH). Everything else —
# review state, seat logins, forge tokens — stays out of the child.
_LOADER_ENV = (
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "SYSTEMROOT",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

# The requested `--timeout` (small, so the suite stays fast) and the documented grace
# the whole command may take beyond it.
_TIMEOUT = 3
_GRACE = 30
_QUOTA_LINE = "You've hit your session limit · resets 5:40am (Europe/Belgrade)"


class _World:
    """A temp HOME + private PATH holding fake seat executables; every pid a fake seat
    starts is appended to `pidfile` so the test can prove none outlives the command."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.home = root / "home"
        self.work = root / "work"
        self.pidfile = root / "pids"
        self.calls = root / "calls"
        for d in (self.bin, self.home, self.work):
            d.mkdir(parents=True)

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def env(self) -> dict[str, str]:
        state = self.root / "state"
        return {
            **{k: os.environ[k] for k in _LOADER_ENV if k in os.environ},
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "CODEX_HOME": str(self.home / ".codex"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "REVIEW_LOG_DIR": str(state / "logs"),
            "REVIEW_STATS_FILE": str(state / "stats.jsonl"),
            "REVIEW_SEAT_COOLDOWN_FILE": str(state / "cooldown.json"),
            # the automatic pre-dispatch seat probe would add a call to the stub seat
            # these tests count (review-cli#663)
            "REVIEW_HEALTH_PREFLIGHT": "0",
            "REVIEW_PROVIDER_CACHE": str(state / "last-provider.json"),
            "REVIEW_USAGE_LIMITS_DIR": str(state / "usage"),
            "REVIEW_JOBS_DIR": str(state / "jobs"),
            "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(state / "claude-accounts"),
        }

    def pids(self) -> list[int]:
        try:
            text = self.pidfile.read_text(encoding="utf-8")
        except OSError:
            return []
        return [int(tok) for tok in text.split() if tok.strip().isdigit()]

    def alive(self) -> list[int]:
        """Pids from `pidfile` still running once the kernel/launchd had a moment to
        reap them (an orphaned grandchild is reaped asynchronously by init)."""
        deadline = time.monotonic() + 5
        while True:
            alive = []
            for pid in self.pids():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    pass
                alive.append(pid)
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.1)

    def reap_leftovers(self) -> None:
        for pid in self.pids():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


@contextlib.contextmanager
def _world():
    with tempfile.TemporaryDirectory(prefix="review-587-") as d:
        world = _World(Path(d))
        try:
            yield world
        finally:
            world.reap_leftovers()


# A seat that keeps printing progress but never answers: output resets an idle timer,
# so only a real wall-clock bound can stop it.
_CHATTY_HANG = """\
    echo "$$" >> {pidfile}
    sleep 1000 &
    echo "$!" >> {pidfile}
    while :; do echo "thinking about it..."; sleep 0.5; done
"""

# A seat that says nothing at all and never exits.
_SILENT_HANG = """\
    echo "$$" >> {pidfile}
    sleep 1000 &
    echo "$!" >> {pidfile}
    exec sleep 1000
"""


def _run_review(
    world: _World, *args: str, limit: float
) -> tuple[subprocess.CompletedProcess, float]:
    """Run the real `bin/review` in `world`; fail (never hang) if it outlives `limit`."""
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(REVIEW_BIN), *args],
        cwd=str(world.work),
        env=world.env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        world.reap_leftovers()
        out, err = proc.communicate()
        raise AssertionError(
            f"`review {' '.join(args)}` was still running after {limit:.0f}s "
            f"(--timeout {_TIMEOUT} + {_GRACE}s grace)\nstdout:\n{out}\nstderr:\n{err}"
        ) from None
    elapsed = time.monotonic() - started
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), elapsed


def _line_with(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line containing {needle!r} in:\n{text}")


def test_a_seat_that_never_answers_is_cut_off_at_the_timeout_with_its_process_group():
    with _world() as world:
        world.fake("codex", _CHATTY_HANG.format(pidfile=world.pidfile))
        result, elapsed = _run_review(
            world,
            "just-ask",
            "--task",
            "587",
            "-m",
            "codex",
            "--timeout",
            str(_TIMEOUT),
            "reply with the single word ok",
            limit=_TIMEOUT + _GRACE,
        )
        text = result.stdout + result.stderr
        assert result.returncode != 0, text
        assert elapsed < _TIMEOUT + _GRACE, (elapsed, text)
        assert "no seat answered" in result.stdout, text
        seat = _line_with(result.stdout, "- codex:")
        assert "timeout" in seat, seat
        assert "thinking about it..." in seat, seat  # its last activity
        assert len(world.pids()) >= 2, world.pids()  # the seat and its grandchild ran
        assert world.alive() == [], f"leftover seat processes: {world.alive()}"


def test_the_report_names_each_seat_and_why_it_gave_no_answer():
    quota_or_silent = f"""\
        case " $* " in
          *" -m gpt-out-of-quota "*)
            echo "{_QUOTA_LINE}" >&2
            exit 1 ;;
        esac
    """ + _SILENT_HANG.format(pidfile="{pidfile}")
    with _world() as world:
        world.fake("codex", quota_or_silent.format(pidfile=world.pidfile))
        result, elapsed = _run_review(
            world,
            "just-ask",
            "--task",
            "587",
            "-m",
            "codex:gpt-out-of-quota",
            "-m",
            "codex",
            "--timeout",
            str(_TIMEOUT),
            "reply ok",
            limit=_TIMEOUT + _GRACE,
        )
        text = result.stdout + result.stderr
        assert result.returncode != 0, text
        assert elapsed < _TIMEOUT + _GRACE, (elapsed, text)
        assert "no seat answered" in result.stdout, text
        quota = _line_with(result.stdout, "- codex:gpt-out-of-quota:")
        assert "quota" in quota and "session limit" in quota, quota
        silent = _line_with(result.stdout, "- codex: ")
        assert "timeout" in silent and "no output" in silent, silent
        assert world.alive() == [], f"leftover seat processes: {world.alive()}"


def test_one_explicit_model_makes_exactly_one_call():
    with _world() as world:
        world.fake("codex", f'echo "$*" >> {world.calls}\necho ok\n')
        result, _ = _run_review(
            world,
            "just-ask",
            "--task",
            "587",
            "-m",
            "codex",
            "--timeout",
            str(_TIMEOUT),
            "reply ok",
            limit=_TIMEOUT + _GRACE,
        )
        text = result.stdout + result.stderr
        assert result.returncode == 0, text
        calls = world.calls.read_text(encoding="utf-8").splitlines()
        assert len(calls) == 1, (calls, text)


# Driver for the last line of defence: the seat wedges INSIDE review (the shape of a
# REST call that ignores its own timeout), so no per-call subprocess kill can reach it.
# It also leaves a registered child running, which the watchdog must reap. The 30 s
# grace is shrunk to 2 s by patching the module constant, only to keep the suite fast.
_WEDGE_DRIVER = """\
import os, subprocess, sys, time
sys.path.insert(0, {repo!r})
from reviewlib import backends, backstop, cli, process

backstop.COMMAND_TIMEOUT_GRACE_SECONDS = 2

def wedged(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
    child = subprocess.Popen(
        ["sleep", "1000"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.register_external_child(child, os.getpgid(child.pid))
    with open({pidfile!r}, "a") as fh:
        fh.write(f"{{child.pid}}\\n")
    time.sleep(1000)

backends.review_codex = wedged
sys.exit(cli.main(["just-ask", "--task", "587", "-m", "codex", "--timeout", "{timeout}", "q"]))
"""


def test_a_seat_wedged_inside_review_is_cut_off_by_the_watchdog_at_timeout_plus_grace():
    grace = 2
    with _world() as world:
        world.fake("codex", _SILENT_HANG.format(pidfile=world.pidfile))
        driver = world.root / "driver.py"
        driver.write_text(
            _WEDGE_DRIVER.format(
                repo=str(REPO_ROOT), pidfile=str(world.pidfile), timeout=_TIMEOUT
            ),
            encoding="utf-8",
        )
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            cwd=str(world.work),
            env=world.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        limit = _TIMEOUT + grace + 15
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            world.reap_leftovers()
            out, err = proc.communicate()
            raise AssertionError(
                f"a wedged seat kept the command alive past {limit}s\n{out}\n{err}"
            ) from None
        elapsed = time.monotonic() - started
        text = out + err
        assert proc.returncode == 124, (proc.returncode, text)
        assert elapsed < _TIMEOUT + grace + 10, (elapsed, text)
        seat = _line_with(err, "- codex:")
        assert "still running" in seat and "no output yet" in seat, seat
        assert world.pids(), "the wedged seat never started its child"
        assert world.alive() == [], f"leftover seat processes: {world.alive()}"


def test_the_20_minute_idle_floor_does_not_outlive_the_command_deadline():
    """The reported case: `--timeout 300` is a >= 60 s value, which the idle window
    raised to a 20-minute floor. Under a command deadline the call is still killed
    when the deadline passes, whatever its idle window."""
    from reviewlib import process

    with _world() as world:
        world.fake("codex", _SILENT_HANG.format(pidfile=world.pidfile))
        saved_log_dir = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = str(world.root / "logs")
        process.set_run_deadline(time.monotonic() + 2)
        started = time.monotonic()
        try:
            result = process._run_streamed(
                [str(world.bin / "codex")], cwd=world.work, timeout=300, backend="codex"
            )
        finally:
            process.set_run_deadline(None)
            if saved_log_dir is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = saved_log_dir
        elapsed = time.monotonic() - started
        assert result.returncode == 124, result
        assert result.timeout_kind == process.TIMEOUT_KIND_DEADLINE, result.timeout_kind
        assert elapsed < 10, elapsed
        assert world.alive() == [], f"leftover seat processes: {world.alive()}"


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
