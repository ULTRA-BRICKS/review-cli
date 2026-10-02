"""Machine-wide cap on heavy backend subprocesses, shared across ALL `review` processes.

Reached from `process._run_streamed` right after the per-process `REVIEW_MAX_CONCURRENCY`
semaphore and before `Popen`: a seat holds one slot for the lifetime of its model-runner
child and gives it back once the child is reaped.

WHY a second cap (review-cli#592, sysmon incident 20260929T195657-live): the per-process
semaphore in `process.py` bounds one `review` invocation's fan-out, and its own comment
says so — "a swarm of N separate `review` processes is N independent caps". That swarm is
the normal case on an agent workstation: several agent sessions each run `review diff`,
and one agent fanned 8 worktrees into 8 concurrent reviews from a single shell script.
10 reviews x 3 seats = ~30 fat agent CLIs at once, load 39 on 14 CPUs, swap 76%, and the
same overload class kernel-panicked the machine earlier that day. Nothing in review-cli
serialized across processes.

HOW: a pool of N lock files (`slot-0.lock` .. `slot-{N-1}.lock`) in one directory every
review process of this user resolves identically. A seat takes a slot with a NON-blocking
exclusive lock (`portable.lock_file(fd, blocking=False)` — `flock` on POSIX); when none is
free it sleeps briefly and tries again, forever, logging one WAITING marker first. The
lock lives on the open file description, so:
  * a slot is released by the KERNEL when its holder exits or is killed — there is no
    stale-lock state to detect or clean, which is exactly why this is not the `mkdir
    /tmp/heavy-run.lock` convention (an `rm -rf` in a trap is skipped by SIGKILL);
  * two threads of ONE process contend the same slot file too, so this pool also bounds
    in-process fan-out and the per-process semaphore stays the only in-process fast path.

Degradation: any OSError building the directory (a sandbox denying the temp root, a
foreign-owned squat at the path) prints a stderr warning and returns None — the seat
then runs UNCAPPED machine-wide, exactly the pre-#592 behaviour. That matches every other
best-effort lock in this package (`jobs.py`, `seat_cooldown.py`): a coordination failure
must never turn into "review doesn't run at all".

Invariants:
  * The slot directory is the same path for every process of this uid on the machine
    regardless of `$TMPDIR` (POSIX: `/tmp/review-cli-slots-<uid>`), or the pool is not
    machine-wide. `$REVIEW_MACHINE_SLOT_DIR` overrides it — tests point it at a temp dir
    so a test run never steals slots from a real review on the same box.
  * Every process must resolve the SAME cap: the cap is the number of slot files a process
    contends (`slot-0` .. `slot-{cap-1}`), so a process running with a larger
    `$REVIEW_MACHINE_MAX_CONCURRENCY` sees extra slots the others never take, and the
    effective bound becomes the largest cap in use, not one machine-wide number. Set the
    override in the login environment (every review process inherits it), never per
    invocation; the cap is deliberately NOT a per-project config value for this reason.
  * `acquire()` blocks BEFORE spawn only; the per-call timeout clock starts at `Popen`
    (same contract as the per-process cap), so a queued seat is never falsely timed out.
  * A held slot is released exactly once: `MachineSlot.release()` is idempotent.
"""

from __future__ import annotations

import os
import random
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO

from . import portable
from .concurrency_caps import capped_env_int, waiting_marker

_ENV_CAP = "REVIEW_MACHINE_MAX_CONCURRENCY"
_ENV_DIR = "REVIEW_MACHINE_SLOT_DIR"
# 6 = two default light-preset boards (3 seats each) fully in flight on a 14-CPU / 24 GB
# workstation; a third review queues. Each seat is a 150-900 MB agent CLI, so 6 keeps the
# worst case near 5 GB instead of the ~15 GB that pushed the box into swap.
DEFAULT_CAP = 6
# Poll cadence while every slot is taken. Jittered so N queued seats don't wake in
# lock-step and all lose the same race to the same freed slot.
_POLL_MIN_SECONDS = 0.5
_POLL_MAX_SECONDS = 1.5


def max_machine_concurrency() -> int:
    """The machine-wide cap from $REVIEW_MACHINE_MAX_CONCURRENCY (`concurrency_caps.capped_env_int`
    contract: missing/garbage → DEFAULT_CAP, <= 0 disables, above the shared ceiling clamps)."""
    return capped_env_int(_ENV_CAP, default=DEFAULT_CAP)


def slot_dir() -> Path:
    """Where the slot lock files live. $REVIEW_MACHINE_SLOT_DIR wins; else a uid-keyed dir
    under `/tmp` on POSIX (NOT `tempfile.gettempdir()`: macOS gives every login session its
    own `$TMPDIR`, and a launchd/cron-spawned review would then sit in a different pool than
    the terminal sessions it must share the machine with). Windows has no `/tmp`, so it
    uses the per-user temp root, which is already machine-wide for that user."""
    override = os.environ.get(_ENV_DIR, "").strip()
    if override:
        return Path(override)
    root = Path(tempfile.gettempdir()) if portable.IS_WINDOWS else Path("/tmp")
    return root / f"review-cli-slots-{portable.current_uid()}"


def _ensure_slot_dir(path: Path) -> None:
    """Create `path` (0700) if missing; an existing one must be a real directory owned by
    us (its mode is left as is — the uid check is the guard). Raises OSError otherwise: a
    foreign-owned or symlinked squat at the predictable `/tmp` path must not become the
    pool (its owner could hold every slot forever)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a directory")
    if st.st_uid != portable.current_uid():
        raise OSError(
            f"{path} is owned by uid {st.st_uid}, not {portable.current_uid()}"
        )


class MachineSlot:
    """A held machine-wide slot: the open, locked fd of one `slot-N.lock` file."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def release(self) -> None:
        """Give the slot back. Idempotent: a second call is a no-op. Closing the fd drops
        the lock even if the explicit unlock failed, so a slot can never leak past this."""
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            portable.unlock_file(fd)
        except OSError:
            pass
        os.close(fd)


def _try_slot(path: Path) -> MachineSlot | None:
    """Try ONE slot file without blocking. None when another holder has it."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | portable.O_NOFOLLOW, 0o600)
    try:
        portable.lock_file(fd, blocking=False)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return MachineSlot(fd)


def try_acquire(cap: int, directory: Path) -> MachineSlot | None:
    """One non-blocking pass over the pool, starting at a random slot so queued seats
    spread over the free ones instead of all racing `slot-0`. None when all are taken.
    OSError propagates (the caller degrades to uncapped)."""
    start = random.randrange(cap)
    for step in range(cap):
        index = (start + step) % cap
        slot = _try_slot(directory / f"slot-{index}.lock")
        if slot is not None:
            return slot
    return None


def _warn_uncapped(backend: str, detail: str) -> None:
    print(
        f"[review-cli] {backend}: {detail} — running without the machine-wide "
        "concurrency cap.",
        file=sys.stderr,
        flush=True,
    )


def acquire(backend: str, log_fh: TextIO) -> MachineSlot | None:
    """Block until a machine-wide slot is free and return it; None when the cap is
    disabled or the pool can't be used (uncapped, a stderr warning per seat).

    `log_fh` is the seat's per-call log: exactly one WAITING marker is written there the
    first time the pool is full, in the same shape as the per-process cap's marker so the
    dashboard parser (`_WAITING_RE`) attributes the queue time correctly."""
    cap = max_machine_concurrency()
    if cap <= 0:
        return None
    directory = slot_dir()
    try:
        _ensure_slot_dir(directory)
    except OSError as exc:
        _warn_uncapped(
            backend,
            f"machine-wide concurrency pool unavailable at {directory} ({exc}); set "
            f"{_ENV_DIR} to a writable directory to restore it",
        )
        return None
    # The pool exists and is ours from here on: an OSError below is a slot-file open/lock
    # failure (EMFILE, a vanished directory), not a setup problem, so it gets its own
    # message rather than the "set REVIEW_MACHINE_SLOT_DIR" advice above.
    try:
        waiting = False
        while True:
            slot = try_acquire(cap, directory)
            if slot is not None:
                return slot
            if not waiting:
                # Exactly one marker, written only when the pool is genuinely full.
                log_fh.write(waiting_marker(backend, cap, machine_wide=True))
                log_fh.flush()
                waiting = True
            time.sleep(random.uniform(_POLL_MIN_SECONDS, _POLL_MAX_SECONDS))
    except OSError as exc:
        _warn_uncapped(
            backend,
            f"machine-wide concurrency pool at {directory} failed while "
            f"acquiring a slot ({exc})",
        )
        return None
