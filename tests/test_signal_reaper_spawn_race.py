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
    raise AssertionError('victim never wrote ' + str(path))


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
            'backend child pid ' + str(pid) + ' survived the external signal -- '
            'orphaned by the register-after-Popen spawn race (review-cli#163)'
        )


# The victim: installs the reaper, then spawns a real _run_streamed call on a worker
# thread (mirroring a panel seat) while its main thread stays idle -- the shape
# review-cli#163 describes. subprocess.Popen is patched so that right after the real
# Popen() call returns (the backend child now genuinely alive), it writes the newly
# spawned pid out and holds for a short, deterministic window before returning
# control to _run_streamed -- reproducing the real gap between spawn and
# _register_child at a size this test can reliably land a signal inside.
_VICTIM_SRC = 'import sys; sys.path.insert(0, %r)\nimport os, subprocess, threading, time\nfrom pathlib import Path\nfrom reviewlib import process as proc_mod\nproc_mod.install_signal_reaper()\nreal_popen = subprocess.Popen\ndef delayed_popen(*a, **kw):\n    proc = real_popen(*a, **kw)\n    if kw.get("start_new_session"):\n        with open(sys.argv[1], "w") as f:\n            f.write(str(proc.pid))\n            f.flush()\n        time.sleep(0.3)\n    return proc\nsubprocess.Popen = delayed_popen\ndef _worker():\n    proc_mod._run_streamed(["sleep", "60"], cwd=Path("."), backend="race-test", timeout=30)\nthreading.Thread(target=_worker, daemon=True).start()\nwith open(sys.argv[2], "w") as f:\n    f.write(str(os.getpid()))\n    f.flush()\ntime.sleep(30)\n'


def test_signal_during_real_run_streamed_registration_does_not_orphan_child(tmp_path):
    """The regression review-cli#163 flags: a signal landing in the genuine window
    between _run_streamed calling subprocess.Popen() and its _register_child() call
    (not a hand pre-registered stand-in, unlike every prior signal-reaper test) must
    still reap the spawned backend child instead of orphaning it."""
    backend_pid_file = tmp_path / 'backend.pid'
    ready_file = tmp_path / 'victim.pid'
    victim = subprocess.Popen(
        [
            sys.executable,
            '-c',
            _VICTIM_SRC % str(REPO_ROOT),
            str(backend_pid_file),
            str(ready_file),
        ],
    )
    try:
        victim_pid = int(_wait_for_file(ready_file))
        assert victim_pid == victim.pid
        backend_pid = int(_wait_for_file(backend_pid_file))
        assert _pid_alive(backend_pid), 'backend child did not start'

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


if __name__ == '__main__':
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_signal_during_real_run_streamed_registration_does_not_orphan_child(Path(d))
    print('PASS')
