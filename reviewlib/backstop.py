"""Internal run backstop — the last-resort time bound on a `review` run.

`review` is meant to have NO external timeout: agents must not wrap it in a short
shell `timeout`, because the panel/brainstorm modes are multi-round and only emit
their synthesis at the very end (see stats.py / the SKILL.md advertising). Removing
the external cap, though, means a genuinely wedged run (a backend that hangs past
its own per-call deadline in a way the per-call kill can't reach, a pathological
loop) could in principle run forever. So the run carries ONE internal, last-resort
time bound instead: a watchdog that force-terminates the process if the WHOLE run
exceeds a hard ceiling.

Design:
  * The ceiling is INTERNAL and capped at <= 4h (`MAX_BACKSTOP_SECONDS`). Nothing can
    set it higher — that is the contract the SKILL.md/README advertise ("no external
    timeout; internal <=4h backstop"). `$REVIEW_BACKSTOP_SECONDS` may only LOWER it
    (tests, or a user who wants a tighter last-resort), never raise it past 4h.
  * It is a backstop, not a normal deadline: a healthy run finishes in MINUTES, far
    under the ceiling, and the watchdog is cancelled cleanly on exit. It only ever
    fires for a truly stuck run.
  * On fire it KILL-FIRST reaps the registered backend subprocesses (SIGKILL straight
    to each one's OWN session group — never the CLI's or the caller's group, which
    `review` often shares with its parent), prints a loud actionable line to stderr,
    and `os._exit`s with code 124 — the conventional "timed out" code, mirroring shell
    `timeout`. A deadman timer guarantees that `os._exit` even if the stderr announce
    blocks on a full pipe. `os._exit` is deliberate: a wedged run may be stuck inside a
    C call or a non-daemon thread that a normal `sys.exit`/raise could not unwind. See
    `_fire` for the exact ordering and why nothing on that path may block the exit.
  * `command_deadline` (review-cli#587) reuses the same watchdog for the one user-set
    wall-clock bound: an explicit `review just-ask --timeout N` kills every seat call at
    N (`process.set_run_deadline`) and fires this watchdog, with a seat report instead of
    the generic line, at N + `COMMAND_TIMEOUT_GRACE_SECONDS`.
  * Time a run spends QUEUED for host-wide admission (`exempt_from_backstop`,
    reviewlib.admission) does not count against the run backstop: the old external
    `flock` wrapper queued before the process even started, and a long queue is not a
    wedged run. An explicit `--timeout N` (`command_deadline`) stays a pure wall-clock
    bound that includes any queueing.
"""

from __future__ import annotations

import os
import time
import sys
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

# The hard ceiling. The internal backstop can be LOWERED below this (tests, a tighter
# user preference) but NEVER raised above it — 4h is the advertised maximum.
MAX_BACKSTOP_SECONDS = 4 * 60 * 60  # 14400

# Exit code on a backstop fire — matches shell `timeout`'s 124 so callers/CI that
# special-case a timeout keep working.
BACKSTOP_EXIT_CODE = 124


def backstop_seconds() -> int:
    """The effective internal backstop, in seconds. Always 1..MAX_BACKSTOP_SECONDS.

    Defaults to the 4h ceiling. `$REVIEW_BACKSTOP_SECONDS` may only LOWER it: a value
    above the ceiling is clamped down to 4h (the cap can't be raised), a non-positive
    or unparseable value is ignored (falls back to the ceiling). The floor is 1s so a
    test can force an almost-immediate fire.
    """
    raw = os.environ.get("REVIEW_BACKSTOP_SECONDS")
    if raw is None:
        return MAX_BACKSTOP_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return MAX_BACKSTOP_SECONDS
    if val <= 0:
        return MAX_BACKSTOP_SECONDS
    return min(val, MAX_BACKSTOP_SECONDS)


# Grace (seconds) the deadman gives the orderly path (reap + announce) before it forces
# the exit unconditionally. Short — its only job is to outlast a normal reap, not to let
# the run continue.
_DEADMAN_GRACE_SECONDS = 3

# Real margin _BACKSTOP_REAP_LOCK_TIMEOUT must leave below _DEADMAN_GRACE_SECONDS for
# the kill loop, the qa-teardown sweep, and the announce to still run before the
# deadman's os._exit fires.
_DEADMAN_REAP_MARGIN_SECONDS = 1.0

# kill_live_children()'s lock-acquire budget when called from HERE, not its default
# (process._REAP_LOCK_TIMEOUT, sized for install_signal_reaper's handler, which has no
# competing deadline). That default would consume nearly this whole grace window,
# leaving no time for the kill loop + the pending-qa-teardown sweep + the announce
# before the deadman's os._exit fires — the two constants landing on the same value
# (3.0 / 3) could zero out a backstop-fired reap entirely (review-cli#163 follow-up).
# Capped well under the grace instead; the checked margin below is the actual
# contract, not just this comment. An `assert` alone would be stripped under
# `python -O`, silently dropping the one guard against this exact regression.
_BACKSTOP_REAP_LOCK_TIMEOUT = 0.5
if _BACKSTOP_REAP_LOCK_TIMEOUT > _DEADMAN_GRACE_SECONDS - _DEADMAN_REAP_MARGIN_SECONDS:
    raise ValueError(
        "_BACKSTOP_REAP_LOCK_TIMEOUT must leave _DEADMAN_REAP_MARGIN_SECONDS of "
        "real margin before the deadman's os._exit for the kill loop, the "
        "qa-teardown sweep, and the announce"
    )


def _fire(seconds: int, stream, announce: Callable[[], str] | None = None) -> None:
    """Watchdog body: GUARANTEE a hard 124 exit, reaping backend children on the way out.

    `os._exit` (not `sys.exit`/raise) is deliberate: a wedged run may be stuck inside a
    C call or a non-daemon thread that a normal unwind could not interrupt — `os._exit`
    is the unconditional guarantee that the run cannot survive this.

    NOTHING here may be allowed to BLOCK the exit. In particular the announcement writes
    to stderr, which can be a PIPE a non-draining parent has let fill — a flushed `print`
    on a full pipe blocks forever (codex P2). So before any potentially-blocking work we
    arm a DEADMAN: a daemon timer that force-exits 124 after a short grace no matter what.
    Then, in order: reap the registered backend subprocesses, announce (best-effort), and
    `os._exit`. If the reap or the announce blocks, the deadman still terminates the
    process — the backstop's guarantee holds.

    It does NOT signal its own / the caller's process group. `review` is frequently
    invoked as a CHILD that shares a process group with its caller (an agent's shell, a
    CI step), so a `killpg(getpgrp())` would take the CALLER down with it. It reaps ONLY
    the registered backend subprocesses (`process.kill_live_children`): each runs in its
    OWN session (`start_new_session=True`), so killing those groups bounds the actual
    work without touching the caller's group.
    """
    # Deadman FIRST: a daemon timer that hard-exits even if everything below wedges
    # (e.g. the announce blocks on a full stderr pipe, or a reap hangs). os._exit in a
    # timer thread terminates the whole process.
    deadman = threading.Timer(
        _DEADMAN_GRACE_SECONDS, os._exit, args=(BACKSTOP_EXIT_CODE,)
    )
    deadman.daemon = True
    deadman.start()
    # A caller-supplied announcement describes the run as it stood at the fire — so it
    # is built BEFORE the reap below changes that (command_deadline's seat report).
    message = (
        f"[review] INTERNAL BACKSTOP fired after {seconds}s "
        f"(hard ceiling {MAX_BACKSTOP_SECONDS}s / 4h) — the run was force-terminated. "
        "This is a last-resort bound, not a normal timeout; a healthy run finishes "
        "in minutes. If you see this, a backend wedged past its per-call deadline."
    )
    if announce is not None:
        try:
            message = announce()
        except Exception:  # noqa: BLE001 — fall back to the generic line, never block the exit
            pass
    # Reap live backend subprocesses (their own session groups) so a wedged run's backends
    # don't get orphaned. Bounded to _BACKSTOP_REAP_LOCK_TIMEOUT (see that constant,
    # above), not kill_live_children's default -- the deadman above still covers a
    # pathological hang.
    try:
        from .process import kill_live_children

        kill_live_children(lock_timeout=_BACKSTOP_REAP_LOCK_TIMEOUT)
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # Reap any pending qa SUT env (hook/compose) BEFORE the announce. os._exit below BYPASSES
    # atexit, so without this a wedged qa run leaks the daemonized SUT env the backstop's
    # subprocess-group SIGKILL cannot reach (codex P2). Order matters: RESOURCE SAFETY (don't
    # leak the env) ranks ABOVE observability (printing the line). The announce can BLOCK on a
    # full, undrained stderr pipe — and a full pipe is one of the wedge scenarios — so if it
    # ran first and blocked, the deadman would force-exit mid-print and this sweep would never
    # run, leaking the very env it exists to reap. Sweeping first guarantees the env is reaped
    # whenever the sweep itself completes within the deadman grace. Costs of this call: it is
    # idempotent, never raises, and a no-op when nothing is pending — but it is NOT free for a
    # plain review run that never imported qa.env, where it triggers a one-time module import at
    # the worst moment (the deadman bounds even that). It is also NOT instantaneous: each
    # teardown is a synchronous `down` spawn bounded by its own timeout, so a long sweep can be
    # cut off mid-flight by the deadman (acceptable last-resort; a re-run's atexit hook or a
    # manual `down` reaps the remainder).
    try:
        from .qa.env import sweep_pending_teardowns

        sweep_pending_teardowns()
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # If THIS process is a detached `--detach` job's child (review-cli#160 companion;
    # `$REVIEW_JOB_ID` is only set by `_spawn_detached_job`'s child env), record its
    # terminal status BEFORE the `os._exit` below — that exit bypasses every `finally`/
    # `atexit`, including `cli.main`'s own job-finalization wrapper, so without this a
    # backstop-killed detached job would never get its OWN terminal status/exit code
    # recorded; `review status`/`wait` would only ever see it reconciled to the generic
    # "unknown-terminated" once the pid disappears, losing the specific 124 (codex
    # review). Best-effort like every other step here: a job record we can't write must
    # never block the guaranteed exit.
    try:
        job_id = os.environ.get("REVIEW_JOB_ID")
        if job_id:
            from . import jobs

            jobs.write_job(
                job_id,
                status="failed",
                exit_code=BACKSTOP_EXIT_CODE,
                finished_at=time.time(),
            )
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # Announce LAST among the fallible steps: a full undrained stderr pipe could block this
    # print, but children + the qa env are already reaped and the deadman guarantees the exit.
    try:
        print(message, file=stream, flush=True)
    except Exception:  # noqa: BLE001 — never let the announcement block the exit
        pass
    os._exit(BACKSTOP_EXIT_CODE)


# Admission-queue time exempt from the run backstop (see the module doc). `_exempt_total`
# is the wall-clock seconds during which AT LEAST ONE exempt wait was in progress (the
# union of the intervals: parallel seats waiting at once count once, not once per seat);
# `_exempt_active` counts waits in progress and `_exempt_since` is when the current busy
# stretch began. A watchdog that comes due re-arms for its budget minus the non-exempt
# time used so far, and re-checks every `_EXEMPT_RECHECK_SECONDS` while a wait is open.
_exempt_lock = threading.Lock()
_exempt_total = 0.0
_exempt_active = 0
_exempt_since = 0.0
_EXEMPT_RECHECK_SECONDS = 60.0


@contextmanager
def exempt_from_backstop() -> Iterator[None]:
    """Time spent inside this block does not count against `run_backstop`."""
    global _exempt_total, _exempt_active, _exempt_since
    with _exempt_lock:
        if _exempt_active == 0:
            _exempt_since = time.monotonic()
        _exempt_active += 1
    try:
        yield
    finally:
        with _exempt_lock:
            _exempt_active -= 1
            if _exempt_active == 0:
                _exempt_total += time.monotonic() - _exempt_since


def _exempt_seconds() -> tuple[float, bool]:
    """Exempt seconds so far (including an open wait) and whether one is open now."""
    with _exempt_lock:
        open_part = time.monotonic() - _exempt_since if _exempt_active else 0.0
        return _exempt_total + open_part, _exempt_active > 0


@contextmanager
def run_backstop(
    seconds: int | None = None,
    *,
    stream=None,
    announce: Callable[[], str] | None = None,
    exempt_waits: bool = True,
) -> Iterator[None]:
    """Arm the internal run backstop for the duration of the `with` block.

    `seconds` defaults to `backstop_seconds()` (the clamped 4h-or-less ceiling). A
    daemon `threading.Timer` fires `_fire` if the block has not exited by then; on a
    normal exit the timer is cancelled so it never fires. Daemon so it can never keep
    the process alive on its own. Re-entrant-safe enough for the single top-level
    `main()` use it is built for, plus one nested `command_deadline`. `announce`, if
    given, builds the line printed on fire instead of the generic backstop notice.
    With `exempt_waits` (the default) the deadline is pushed back by every second the
    block spends in `exempt_from_backstop` (host-wide admission queueing).
    """
    secs = (
        backstop_seconds()
        if seconds is None
        else max(1, min(int(seconds), MAX_BACKSTOP_SECONDS))
    )
    out = stream if stream is not None else sys.stderr
    guard = threading.Lock()
    started = time.monotonic()
    state: dict = {
        "timer": None,
        "done": False,
        "exempt_at_start": _exempt_seconds()[0],
    }

    def _arm(delay: float) -> None:
        timer = threading.Timer(delay, _due)
        timer.daemon = True
        state["timer"] = timer
        timer.start()

    def _due() -> None:
        with guard:
            if state["done"]:
                return
            if exempt_waits:
                exempt, waiting = _exempt_seconds()
                used = time.monotonic() - started - (exempt - state["exempt_at_start"])
                remaining = secs - used
                if remaining > 0:
                    # `used` is frozen while a wait is open, so the budget cannot run
                    # out then: recheck on a fixed period instead of re-arming for a
                    # possibly tiny `remaining` over and over.
                    _arm(_EXEMPT_RECHECK_SECONDS if waiting else remaining)
                    return
        _fire(secs, out, announce)

    with guard:
        _arm(secs)
    try:
        yield
    finally:
        with guard:
            state["done"] = True
            state["timer"].cancel()


# review-cli#587: grace the whole command gets past an explicit `review just-ask
# --timeout N`. Every seat call is killed at N (`process.set_run_deadline`); the grace
# covers the kill, the report and exit, and if the command is still alive at N + grace
# (a seat wedged where no per-call kill reaches) the watchdog force-exits it.
COMMAND_TIMEOUT_GRACE_SECONDS = 30


@contextmanager
def command_deadline(seconds: int, *, command: str, stream=None) -> Iterator[None]:
    """Make `seconds` a hard wall-clock bound on the `with` block (review-cli#587).

    Arms `process.set_run_deadline(now + seconds)`, which kills every backend call
    still running at that moment and refuses to start new ones, and a watchdog at
    `seconds + COMMAND_TIMEOUT_GRACE_SECONDS` that — should the block still not have
    returned — prints `command`, each seat still running with its last activity and
    each finished seat's outcome, reaps every registered child process group and exits
    `BACKSTOP_EXIT_CODE` (124)."""
    from .process import set_run_deadline

    grace = COMMAND_TIMEOUT_GRACE_SECONDS
    started = time.monotonic()
    set_run_deadline(started + seconds)
    try:
        with run_backstop(
            seconds + grace,
            stream=stream,
            announce=lambda: _seat_report(command, seconds, grace, started),
            exempt_waits=False,
        ):
            yield
    finally:
        set_run_deadline(None)


def _seat_report(command: str, seconds: int, grace: int, started: float) -> str:
    """The watchdog's announcement: what was still running at `seconds + grace`."""
    from .process import seat_calls

    now = time.monotonic()
    lines = [
        f"[review] {command}: still running {now - started:.0f}s after start "
        f"(--timeout {seconds}s + {grace}s grace) — killed every seat process group "
        f"and exited {BACKSTOP_EXIT_CODE}. Seats waited on:"
    ]
    for call in seat_calls():
        ran = (call.finished if call.finished is not None else now) - call.started
        if call.finished is None:
            activity = (
                f'last output {now - call.last_output:.0f}s ago: "{call.last_line}"'
                if call.last_output is not None
                else "no output yet"
            )
            lines.append(f"  - {call.seat}: still running after {ran:.0f}s; {activity}")
        else:
            lines.append(
                f"  - {call.seat}: finished after {ran:.0f}s; {call.outcome or 'no answer'}"
            )
    if len(lines) == 1:
        lines.append("  (no seat call had started)")
    return "\n".join(lines)
