#!/usr/bin/env python3
"""Regression test for the review-cli#163 signal-reaper spawn race.

install_signal_reaper installs a handler that reaps every child registered in
reviewlib.process._LIVE_CHILDREN via kill_live_children(). Every existing
signal-reaper test (test_signal_reaper.py) hand-registers its victim child before
sending the external signal, so none of them exercise the real window inside
_run_streamed between subprocess.Popen() returning and _register_child() adding the
handle to that registry. A signal landing in that real window used to see the
registry without this child and reap nothing, orphaning the just-spawned backend
process -- exactly the gap review-cli#163 flags.

This test drives the actual _run_streamed spawn path on a worker thread (mirroring a
real panel seat) while the main thread stays idle waiting for the external signal --
the same shape review-cli#163 describes. subprocess.Popen is patched to hold the
freshly spawned child open for a short, deterministic window at the exact point
_run_streamed calls it, so the test can reliably land a signal inside the real
register gap instead of depending on raw scheduling luck.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return text
        time.sleep(0.02)
    raise AssertionError("victim never wrote " + str(path))


def _assert_dies(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        raise AssertionError(
            "backend child pid " + str(pid) + " survived the external signal -- "
            "orphaned by the register-after-Popen spawn race (review-cli#163)"
        )


# The victim: installs the reaper, then spawns a real _run_streamed call on a worker
# thread (mirroring a panel seat) while its main thread stays idle -- the shape
# review-cli#163 describes. subprocess.Popen is patched so that right after the real
# Popen() call returns (the backend child now genuinely alive), it writes the newly
# spawned pid out and holds for a window derived from proc_mod._REAP_LOCK_TIMEOUT
# (a quarter of it, floored at 0.5s, so the hold always stays well inside the
# reaper's acquire budget -- and gives the outer test enough reaction time -- no
# matter how that constant is tuned later; a hardcoded hold that merely happened to
# be less than the timeout constant in a different file would silently lose
# regression power if either one changed) before returning control to
# _run_streamed -- reproducing the real gap between spawn and _register_child at a
# size this test can reliably land a signal inside. It also writes a THIRD marker
# the instant the hold ends (window closes), so the outer test can detect a
# scheduling delay that let the window close before the signal was sent and fail
# loudly instead of passing vacuously on a slow/loaded CI runner.
_VICTIM_SRC = (
    "import sys; sys.path.insert(0, %r)\n"
    "import os, subprocess, threading, time\n"
    "from pathlib import Path\n"
    "from reviewlib import process as proc_mod\n"
    "proc_mod.install_signal_reaper()\n"
    "real_popen = subprocess.Popen\n"
    "hold_s = max(0.5, proc_mod._REAP_LOCK_TIMEOUT / 4)\n"
    "def delayed_popen(*a, **kw):\n"
    "    proc = real_popen(*a, **kw)\n"
    "    if kw.get('start_new_session'):\n"
    "        with open(sys.argv[1], 'w') as f:\n"
    "            f.write(str(proc.pid))\n"
    "            f.flush()\n"
    "        time.sleep(hold_s)\n"
    "        with open(sys.argv[3], 'w') as f:\n"
    "            f.write('closed')\n"
    "            f.flush()\n"
    "    return proc\n"
    "subprocess.Popen = delayed_popen\n"
    "def _worker():\n"
    "    proc_mod._run_streamed(\n"
    "        ['sleep', '60'], cwd=Path('.'), backend='race-test', timeout=30\n"
    "    )\n"
    "threading.Thread(target=_worker, daemon=True).start()\n"
    "with open(sys.argv[2], 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "    f.flush()\n"
    "time.sleep(30)\n"
)


def test_signal_during_real_run_streamed_registration_does_not_orphan_child(tmp_path):
    """The regression review-cli#163 flags: a signal landing in the genuine window
    between _run_streamed calling subprocess.Popen() and its _register_child() call
    (not a hand pre-registered stand-in, unlike every prior signal-reaper test) must
    still reap the spawned backend child instead of orphaning it."""
    backend_pid_file = tmp_path / "backend.pid"
    ready_file = tmp_path / "victim.pid"
    window_closed_file = tmp_path / "window_closed"
    victim = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _VICTIM_SRC % str(REPO_ROOT),
            str(backend_pid_file),
            str(ready_file),
            str(window_closed_file),
        ],
    )
    try:
        victim_pid = int(_wait_for_file(ready_file))
        assert victim_pid == victim.pid
        backend_pid = int(_wait_for_file(backend_pid_file))
        assert _pid_alive(backend_pid), "backend child did not start"

        # The window is still open (asserted, not assumed): if it already closed here,
        # the signal below would land AFTER registration and this test would pass
        # without ever exercising the race review-cli#163 is about.
        assert not window_closed_file.exists(), (
            "spawn-race window closed before the signal was sent (CI scheduling "
            "delay) -- this run exercised nothing; rerun or widen the hold"
        )

        # The external signal, delivered while the worker thread is still inside the
        # artificially held-open Popen-to-register window.
        os.kill(victim.pid, signal.SIGTERM)

        victim.wait(timeout=5)
        assert victim.returncode != 0, victim.returncode

        _assert_dies(backend_pid)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)


def test_kill_live_children_falls_back_when_lock_contended():
    """kill_live_children() must never hang: both its real callers (backstop._fire's
    deadman-bounded reap and install_signal_reaper's signal handler) depend on it
    returning promptly even if it can't get _LIVE_CHILDREN_LOCK. Hold the lock from
    another thread past a short lock_timeout and assert the call falls back to an
    unlocked read instead of blocking past that budget -- the mechanism the whole
    fix, and backstop's bounded override, both rely on."""
    from reviewlib import process as proc_mod

    release_event = threading.Event()
    holder_ready = threading.Event()

    def _hold_lock():
        with proc_mod._LIVE_CHILDREN_LOCK:
            holder_ready.set()
            release_event.wait(5)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    try:
        assert holder_ready.wait(5), "holder thread never acquired the lock"
        start = time.monotonic()
        proc_mod.kill_live_children(lock_timeout=0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, (
            f"kill_live_children blocked {elapsed:.2f}s past its 0.1s lock_timeout "
            "instead of falling back to an unlocked read"
        )
    finally:
        release_event.set()
        holder.join(timeout=5)


def test_kill_live_children_reaps_already_registered_child_immediately_despite_lock_contention():
    """review-cli#163 review (GLM): a single acquire-then-kill design meant an
    aggressive external SIGTERM-then-SIGKILL harness landing its SIGKILL while
    kill_live_children() was still waiting on the (now 3.0s default) contended
    lock reaped NOTHING -- not even a backend child registered long before the
    signal, which the pre-#163-era hardcoded 0.5s timeout always reaped well
    within that same window. Runs kill_live_children() with the REAL production
    default lock_timeout (not a short test-only override) while another thread
    holds the lock for the whole call; the already-registered child must still
    die almost immediately via the first, lock-free pass -- NOT only ~3s later
    once the second pass's acquire eventually times out and falls back. A short
    `child.wait(timeout=1.0)` (vs the 3.0s default) is what actually
    distinguishes this from the pre-fix single-pass design, which a
    short-`lock_timeout` test cannot: its own fallback-on-timeout would reap the
    child too, indistinguishably from this fix, once `lock_timeout` elapses."""
    from reviewlib import process as proc_mod

    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        pgid = os.getpgid(child.pid)
        handle = proc_mod._register_child(child, pgid)
        try:
            release_event = threading.Event()
            holder_ready = threading.Event()

            def _hold_lock():
                with proc_mod._LIVE_CHILDREN_LOCK:
                    holder_ready.set()
                    release_event.wait(5)

            holder = threading.Thread(target=_hold_lock, daemon=True)
            holder.start()
            kill_thread = threading.Thread(
                target=proc_mod.kill_live_children, daemon=True
            )
            try:
                assert holder_ready.wait(5), "holder thread never acquired the lock"
                kill_thread.start()
                # Reap via wait(), not _pid_alive() -- this child is OWNED directly
                # by this test process (unlike the victim-spawned grandchild
                # elsewhere in this file), so a SIGKILLed-but-unreaped child is a
                # ZOMBIE: os.kill(pid, 0) keeps succeeding until something calls
                # wait() on it, which would make _pid_alive() report a dead child
                # as still alive.
                try:
                    child.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    raise AssertionError(
                        f"already-registered child pid {child.pid} was not reaped "
                        "within 1s despite running with the 3.0s production "
                        "default lock_timeout while another thread held the lock "
                        "the whole time -- the immediate lock-free pass must not "
                        "wait on lock_timeout at all"
                    ) from None
                assert child.returncode == -signal.SIGKILL, child.returncode
            finally:
                release_event.set()
                kill_thread.join(timeout=5)
                holder.join(timeout=5)
        finally:
            proc_mod._unregister_child(handle)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


# A child that patches kill_live_children with a recorder BEFORE calling backstop._fire
# for real (it unconditionally os._exit()s, so this must run out-of-process) and writes
# out the lock_timeout _fire actually passed.
_FIRE_LOCK_TIMEOUT_RECORDER_SRC = (
    "import sys; sys.path.insert(0, %r)\n"
    "from reviewlib import backstop, process as proc_mod\n"
    "def fake_kill_live_children(*, lock_timeout=None):\n"
    "    with open(sys.argv[1], 'w') as f:\n"
    "        f.write(repr(lock_timeout))\n"
    "proc_mod.kill_live_children = fake_kill_live_children\n"
    "backstop._fire(1, sys.stderr)\n"
)


def test_backstop_fire_passes_its_bounded_lock_timeout(tmp_path):
    """The backstop/signal-reaper timeout collision this fix closes is only guarded
    by a constant-side invariant check -- nothing previously proved `_fire` actually
    PASSES `_BACKSTOP_REAP_LOCK_TIMEOUT` rather than silently falling back to
    `kill_live_children`'s default (which would reintroduce the exact bug: that
    default nearly equals the deadman's own grace window). Runs `_fire` for real in
    a child process (it unconditionally `os._exit`s) with `kill_live_children`
    swapped for a recorder, then asserts the recorded value."""
    from reviewlib.backstop import _BACKSTOP_REAP_LOCK_TIMEOUT

    recorded_file = tmp_path / "lock_timeout.txt"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _FIRE_LOCK_TIMEOUT_RECORDER_SRC % str(REPO_ROOT),
            str(recorded_file),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert recorded_file.exists(), (
        "backstop._fire never reached kill_live_children: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    recorded = float(recorded_file.read_text())
    assert recorded == _BACKSTOP_REAP_LOCK_TIMEOUT, (
        f"backstop._fire passed lock_timeout={recorded!r}, expected "
        f"_BACKSTOP_REAP_LOCK_TIMEOUT={_BACKSTOP_REAP_LOCK_TIMEOUT!r} -- reverting to "
        "kill_live_children()'s default would race the deadman (review-cli#163)"
    )


def test_kill_live_children_same_thread_reentry_is_instant():
    """_LIVE_CHILDREN_LOCK must be an RLock, not a plain Lock: kill_live_children()
    can run on the SAME thread that already holds the lock (a signal handler firing
    while _run_streamed runs synchronously on the main thread, e.g. `--pool 1`). A
    plain Lock would block for the full lock_timeout before falling back to a stale
    read; RLock re-enters instantly. Pin the mechanism directly -- the cross-thread
    tests above would still pass if RLock were reverted to a plain Lock."""
    from reviewlib import process as proc_mod

    with proc_mod._LIVE_CHILDREN_LOCK:
        start = time.monotonic()
        proc_mod.kill_live_children(lock_timeout=2.0)
        elapsed = time.monotonic() - start
    assert elapsed < 0.5, (
        f"same-thread kill_live_children took {elapsed:.2f}s against a 2.0s "
        "lock_timeout -- _LIVE_CHILDREN_LOCK must be an RLock for instant "
        "same-thread re-entry, not a plain Lock"
    )


if __name__ == "__main__":
    import tempfile

    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                        fn(Path(d))
                    else:
                        fn()
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"FAIL {name}: {exc}")
                    failures.append(name)
    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        raise SystemExit(1)
    print("\nAll tests passed.")
