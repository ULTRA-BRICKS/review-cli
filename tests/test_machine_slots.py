#!/usr/bin/env python3
"""Tests for the machine-wide heavy-seat cap (`reviewlib.machine_slots`, review-cli#592).

The per-process cap (test_concurrency_cap.py) is blind to a swarm of SEPARATE `review`
processes — the incident shape: 10 concurrent `review diff` invocations from several agent
sessions, ~30 model children at once. These tests drive real separate Python processes,
each running `process._run_streamed` against a fake backend that records its own liveness
window on disk, and assert on that on-disk evidence:
  (a) the cap HOLDS across processes with the per-process cap disabled (nothing else
      serializes), and every queued seat still runs;
  (b) a slot held by a process that is SIGKILLed is freed by the kernel — a seat that was
      blocked on the full pool (with its WAITING marker written) then proceeds;
  (c) a disabled cap (<= 0) never touches the pool;
  (d) an unusable pool directory degrades to uncapped with one stderr warning;
  (e) `max_machine_concurrency()` reads/clamps $REVIEW_MACHINE_MAX_CONCURRENCY.

Standalone-runnable (`python tests/test_machine_slots.py`, what smoke.py drives) as well as
under pytest, so no pytest fixtures: env is set/restored by hand per test.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import reviewlib.concurrency_caps as concurrency_caps  # noqa: E402
import reviewlib.machine_slots as machine_slots  # noqa: E402
from test_concurrency_cap import _liveness_argv, _peak_concurrency  # noqa: E402


@contextlib.contextmanager
def _env(**values: str | None):
    """Set env vars for the body (None = unset), restore exactly afterward."""
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _seat_process_env(slot_dir: Path, cap: str, log_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        REVIEW_MACHINE_SLOT_DIR=str(slot_dir),
        REVIEW_MACHINE_MAX_CONCURRENCY=cap,
        # Per-process cap OFF: only the machine-wide pool may serialize anything here.
        REVIEW_MAX_CONCURRENCY="0",
        REVIEW_LOG_DIR=str(log_dir),
        PYTHONPATH=str(REPO_ROOT),
    )
    return env


def _spawn_seat_process(
    record_dir: str, env: dict[str, str], hold: float
) -> subprocess.Popen:
    """One SEPARATE review-like process: runs `_run_streamed` on the liveness fake backend."""
    code = (
        "import reviewlib.process as p\n"
        f"p._run_streamed({_liveness_argv(record_dir, hold=hold)!r}, cwd={str(REPO_ROOT)!r},"
        " timeout=30, backend='xproc', round_no=0)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(REPO_ROOT))


# === (a) the cap holds ACROSS processes ==========================================
def test_cap_bounds_children_across_separate_processes():
    """Cap 2, five separate review processes dispatched at once: never more than two fake
    backends alive at the same instant, and all five ran to completion."""
    tmp = Path(tempfile.mkdtemp())
    record_dir = tempfile.mkdtemp()
    env = _seat_process_env(tmp / "slots", "2", tmp / "logs")
    procs = [_spawn_seat_process(record_dir, env, hold=0.8) for _ in range(5)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    events = Path(record_dir) / "events"
    assert len(list(events.glob("*.end"))) == 5, sorted(
        p.name for p in events.glob("*")
    )
    assert _peak_concurrency(events) <= 2, _peak_concurrency(events)


# === (b) a killed holder's slot is freed by the kernel ============================
def _holder_process(slot_dir: Path) -> subprocess.Popen:
    """A separate process that takes the sole slot, prints `held`, and sleeps until killed."""
    code = (
        "import io, time, reviewlib.machine_slots as m\n"
        "slot = m.acquire('holder', io.StringIO())\n"
        "assert slot is not None\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    env = dict(
        os.environ,
        REVIEW_MACHINE_SLOT_DIR=str(slot_dir),
        REVIEW_MACHINE_MAX_CONCURRENCY="1",
        PYTHONPATH=str(REPO_ROOT),
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )


def test_sigkilled_holder_frees_slot_for_a_waiting_seat():
    """Cap 1. Another process holds the only slot; `acquire` here writes its WAITING marker
    and blocks. SIGKILL the holder (no cleanup code runs) — the blocked acquire completes.
    This is the property that makes the pool safe without any stale-lock reaping."""
    slot_dir = Path(tempfile.mkdtemp()) / "slots"
    holder = _holder_process(slot_dir)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert machine_slots.try_acquire(1, slot_dir) is None

        log = io.StringIO()
        got: dict[str, machine_slots.MachineSlot | None] = {}
        with _env(
            REVIEW_MACHINE_SLOT_DIR=str(slot_dir), REVIEW_MACHINE_MAX_CONCURRENCY="1"
        ):
            waiter = threading.Thread(
                target=lambda: got.__setitem__(
                    "slot", machine_slots.acquire("waiter", log)
                ),
            )
            waiter.start()
            deadline = time.monotonic() + 10
            while (
                "waiting for a machine-wide concurrency slot (cap 1)"
                not in log.getvalue()
            ):
                assert time.monotonic() < deadline, log.getvalue()
                time.sleep(0.05)
            assert waiter.is_alive(), "acquire must block while the holder is alive"

            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)
            waiter.join(timeout=15)
        assert not waiter.is_alive(), "acquire stayed blocked after the holder died"
        slot = got["slot"]
        assert slot is not None
        slot.release()
        # Released for real: a fresh non-blocking try succeeds immediately.
        again = machine_slots.try_acquire(1, slot_dir)
        assert again is not None
        again.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


# === (c) disabled cap never touches the pool =====================================
def test_disabled_cap_returns_none_without_creating_pool():
    slot_dir = Path(tempfile.mkdtemp()) / "never-created"
    with _env(
        REVIEW_MACHINE_SLOT_DIR=str(slot_dir), REVIEW_MACHINE_MAX_CONCURRENCY="0"
    ):
        assert machine_slots.acquire("x", io.StringIO()) is None
    assert not slot_dir.exists()


# === (d) an unusable pool degrades to uncapped, loudly ===========================
def test_unusable_pool_dir_degrades_to_uncapped_with_warning():
    blocker = Path(tempfile.mkdtemp()) / "a-file"
    blocker.write_text("")
    err = io.StringIO()
    # mkdir under a regular FILE fails — the pool can't be set up.
    with _env(
        REVIEW_MACHINE_SLOT_DIR=str(blocker / "slots"),
        REVIEW_MACHINE_MAX_CONCURRENCY="2",
    ):
        with contextlib.redirect_stderr(err):
            assert machine_slots.acquire("seat", io.StringIO()) is None
    assert "machine-wide concurrency pool unavailable" in err.getvalue(), err.getvalue()
    assert "REVIEW_MACHINE_SLOT_DIR" in err.getvalue()


# === (e) env reader ==============================================================
def test_max_machine_concurrency_reader():
    for raw, expected in (
        (None, machine_slots.DEFAULT_CAP),
        ("garbage", machine_slots.DEFAULT_CAP),
        ("-3", 0),
        ("100000", concurrency_caps.CAP_CEILING),
    ):
        with _env(REVIEW_MACHINE_MAX_CONCURRENCY=raw):
            assert machine_slots.max_machine_concurrency() == expected, raw


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
