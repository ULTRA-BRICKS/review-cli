"""Optional live curses TUI for `review diff`'s board dispatch (review-cli#548).

Why this exists
----------------
`review diff`'s failover board (panel.run_board_with_failover) is normally a silent,
multi-minute wait followed by one block of prose findings -- a per-call log file plus an
occasional stderr heartbeat line (process._emit_wait_heartbeat) is the only sign of life.
docs/compare-revmux.md (review-cli#546) noted that umputun's revmux shows a live terminal
table of its supervised claude/codex subprocesses; this module is review-cli's equivalent
for the ONE audience who benefits from it -- a human running `review diff` directly in a
real terminal. An agent/CI caller (piped, redirected, or just not a tty) sees NO behavior
change at all: see `start_if_enabled`'s gate below.

Scope, deliberately narrow
---------------------------
This is an OBSERVER bolted onto the EXISTING board-dispatch machinery
(`panel.run_board_with_failover` / `panel.run_panel_with_retry` / `retry.run_seat_with_retry`),
not a rewrite of it: every public method here is a cheap, non-blocking callback those
functions call when a seat's status changes or when a retry/failover/promotion notice would
otherwise be a raw `print(..., file=sys.stderr)` -- which, unlike a normal non-interactive run,
WOULD corrupt an active curses screen (stdout and stderr point at the SAME real tty during an
interactive run). When no `BoardReporter` is passed to those functions (the default, `None`),
their behavior is BYTE-IDENTICAL to before this feature existed: the same stderr prints, the
same per-call log files, the same heartbeat lines. See each call site's own comment for
exactly what it does instead when a reporter IS active.

What this deliberately does NOT cover: the "still waiting" heartbeat
(`process._emit_wait_heartbeat`) is deep inside the generic per-call streamed-subprocess
plumbing shared by EVERY mode (not just the diff-review board), reached through every single
backend function in backends.py. Routing it through this reporter too would mean threading a
callback through all of that -- a genuinely different, much larger change than "hook an
observer into the board-dispatch/failover machinery", which is the ticket's explicit,
narrower scope. In practice this is a narrow, self-healing gap: a stray heartbeat line only
appears once a seat has gone >60s with no output at all (most seats stream something well
before then), and the render loop below repaints the WHOLE screen every `_REFRESH_SECONDS`
regardless of new events -- so any such stray line is overdrawn within a fraction of a second
of appearing, not left as lasting corruption.

Auto-enable gate (`start_if_enabled`)
--------------------------------------
Enabled ONLY when ALL of:
  * not explicitly disabled (`--no-tui` / `$REVIEW_NO_TUI`, see `no_tui_env`);
  * `sys.stdout.isatty()` AND `sys.stderr.isatty()` are both true (a piped/redirected/
    agent-driven invocation must see zero behavior change -- matching AGENTS.md's discipline
    that automation's behavior never depends on incidental interactivity);
  * `curses.initscr()` can actually acquire a REAL, cursor-addressable screen within a
    bounded handshake (a few seconds) -- see `BoardReporter.start`'s docstring for exactly
    what "real" means and why a bare `initscr()` success is not sufficient proof of that.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Sequence

try:
    import curses
except ImportError:
    curses = None  # type: ignore[assignment]

NO_TUI_ENV = "REVIEW_NO_TUI"


def no_tui_env() -> bool:
    """Truthy-env convention MATCHED from `reviewlib.qa.live_tier._flag_enabled` (the
    closest existing "boolean env var" idiom in this codebase -- a grep for a prior
    `REVIEW_NO_*` env var found no exact precedent to match, so this mirrors the nearest
    established one instead of inventing a new convention): anything but "", "0", "false",
    "no", "off" (case-insensitive, after stripping) counts as set. $REVIEW_NO_TUI is a
    DISABLE switch (the inverse sense of the flags `_flag_enabled` guards), so "set at all,
    to a truthy value" means "force the TUI off" -- the same sense as `--no-tui`'s
    `action="store_true"`."""
    return os.environ.get(NO_TUI_ENV, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRYING = "retrying"
STATUS_PROMOTED = "promoted-from-reserve"
STATUS_DONE = "done"
STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRYING, STATUS_PROMOTED, STATUS_DONE)

_REFRESH_SECONDS = 0.3
_MAX_LOG_LINES = 200


@dataclass
class _SeatRow:
    role: str
    model: str
    status: str
    started: float
    ok: bool | None = None
    # Set once, on the transition to STATUS_DONE -- without it, `_draw` would keep
    # computing `now - started` for an already-finished seat, so its ELAPSED column
    # would report the WHOLE RUN's wall-clock (every other seat still running) instead
    # of that seat's own duration (review finding on this feature's own PR).
    finished: float | None = None


def _safe_endwin() -> None:
    """Best-effort `curses.endwin()`. A caller tearing down after a FAILED/partial
    `initscr()` must never raise past this -- the whole point of a graceful fallback is that
    the fallback path itself cannot also blow up."""
    if curses is None:
        return
    try:
        curses.endwin()
    except Exception:  # noqa: BLE001 - teardown must never raise
        pass


class BoardReporter:
    """Live curses observer for one `review diff` board run.

    Construct via `BoardReporter.start(...)`, never directly -- the constructor assumes an
    already-initialized `stdscr` and does no curses setup of its own. Every public method
    (`seat_status` / `event` / `close`) is safe to call from ANY thread: they only mutate
    lock-protected state. The actual curses drawing happens exclusively on this object's OWN
    background render thread -- curses is not safe for concurrent calls from more than one
    thread, so every real `addstr`/`refresh` call funnels through `_render_loop`, and
    `seat_status`/`event` (called from board-dispatch worker threads) never touch curses
    directly."""

    def __init__(self, stdscr: "curses.window", seats: Sequence[tuple[str, str]]) -> None:
        self._stdscr = stdscr
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], _SeatRow] = {}
        self._log: deque[str] = deque(maxlen=_MAX_LOG_LINES)
        self._closed = False
        self._stop = threading.Event()
        now = time.monotonic()
        for role, model in seats:
            self._rows[(role, model)] = _SeatRow(
                role=role, model=model, status=STATUS_QUEUED, started=now
            )
        self._thread = threading.Thread(
            target=self._render_loop, name="review-tui-render", daemon=True
        )
        self._thread.start()

    @classmethod
    def start(
        cls, seats: Sequence[tuple[str, str]], *, timeout: float = 3.0
    ) -> "BoardReporter | None":
        """Bounded, try/except-wrapped curses acquisition. Returns None (never raises, never
        blocks past `timeout`) for every failure mode:
          * curses absent on this platform (stock Windows CPython);
          * `initscr()` raising -- verified empirically: TERM=unknown / an empty TERM / a
            bogus made-up terminal name all raise `curses.error` immediately (NOT a hang) --
            a genuinely missing/broken terminfo entry;
          * a terminal that lacks real cursor-addressing (`cup`) -- verified empirically:
            TERM=dumb makes `initscr()` SUCCEED, but every subsequent full-screen redraw then
            degrades into a scroll of blank-padded lines filling the whole screen (ncurses'
            fallback for a terminal it can't cursor-address), which is strictly WORSE than
            the heartbeat this feature replaces -- so a missing `cup` capability is treated as
            a startup failure too, not just an `initscr()` exception;
          * a defensive case, never reproduced against a real terminal in testing here, kept
            because the ticket explicitly asks for it: an `initscr()` call that never returns
            at all (a wedged terminal driver). The acquisition runs on a daemon thread and
            this call gives up after `timeout` seconds rather than blocking the caller. A
            timed-out attempt leaves that daemon thread running in the background; if it
            eventually completes, nothing consumes the result -- an unresponsive driver is
            not something this process can safely recover from the outside, so "give up
            cleanly and fall back" is the whole contract here, not "reclaim the thread too"."""
        if curses is None:
            return None
        result: dict[str, object] = {}
        ready = threading.Event()
        # `abandon_lock` makes the "did the caller already give up?" check ATOMIC with
        # publishing `result["stdscr"]` -- without it, `_init` could observe "not
        # abandoned yet" microseconds before `start()` times out and marks it so,
        # publish anyway, and leak noecho()/cbreak() on the real terminal with nobody
        # left to call endwin() (review finding on this feature's own PR, tightened
        # after an initial unlocked version left exactly that gap open). A residual,
        # much narrower window remains between `_init` releasing this lock and its own
        # `ready.set()` in `finally` -- closing that fully would need an OS-level
        # cancellation primitive Python's `threading.Event` doesn't offer; at that point
        # the whole curses handshake already completed within the bound, so this is
        # deep into "genuinely defensive, not reproduced against a real terminal" territory.
        abandon_lock = threading.Lock()
        abandoned = False

        def _init() -> None:
            try:
                stdscr = curses.initscr()
                if not curses.tigetstr("cup"):
                    raise curses.error(
                        "terminal has no cursor-addressing capability (cup) -- not a real "
                        "full-screen terminal"
                    )
                curses.noecho()
                curses.cbreak()
                stdscr.keypad(True)
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                with abandon_lock:
                    if abandoned:
                        _safe_endwin()
                    else:
                        result["stdscr"] = stdscr
            except Exception as exc:  # noqa: BLE001 - ANY curses/terminfo failure -> fallback
                result["error"] = exc
                _safe_endwin()
            finally:
                ready.set()

        threading.Thread(target=_init, name="review-tui-init", daemon=True).start()
        if not ready.wait(timeout) or "stdscr" not in result:
            with abandon_lock:
                abandoned = True
            return None
        try:
            return cls(result["stdscr"], seats)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a display glitch must never break the review
            _safe_endwin()
            return None

    def close(self) -> None:
        """Tear down and restore the terminal. Idempotent -- the CLI calls this in a
        `finally`, unconditionally, regardless of how the board run ended."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=2.0)
        _safe_endwin()

    def seat_status(
        self, role: str, model: str, status: str, *, ok: bool | None = None
    ) -> None:
        """Record a seat's lifecycle transition. `(role, model)` identifies the table row --
        stable across a seat's whole life EXCEPT a reserve promotion, which creates a
        genuinely NEW row (a different reviewer, possibly under a different role, taking
        over for the seat that failed) rather than mutating the failed seat's own row (whose
        `done` update already landed first).

        `status` MUST be one of `STATUSES` -- checked here (not just documented) so a
        typo'd/renamed status string fails LOUDLY at the call site instead of silently
        rendering as a frozen, bogus row (`_render_loop`'s own top-level `except Exception:
        pass` would otherwise hide it as a blank screen with no diagnostic at all)."""
        if status not in STATUSES:
            raise ValueError(f"unknown seat status {status!r}; expected one of {STATUSES}")
        with self._lock:
            key = (role, model)
            row = self._rows.get(key)
            if row is None:
                self._rows[key] = _SeatRow(
                    role=role, model=model, status=status, started=time.monotonic(), ok=ok
                )
                return
            row.status = status
            if ok is not None:
                row.ok = ok
            if status == STATUS_DONE and row.finished is None:
                row.finished = time.monotonic()

    def event(self, message: str) -> None:
        """Append one line to the scrolling retry/failover event log."""
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _render_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._draw()
            except Exception:  # noqa: BLE001 - a display glitch must never end the review
                pass
            self._stop.wait(_REFRESH_SECONDS)

    def _draw(self) -> None:
        stdscr = self._stdscr
        with self._lock:
            rows = list(self._rows.values())
            log_lines = list(self._log)
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        now = time.monotonic()

        def put(y: int, x: int, text: str) -> None:
            if y < 0 or y >= max_y or x >= max_x:
                return
            try:
                stdscr.addnstr(y, x, text, max(0, max_x - x - 1))
            except curses.error:
                pass

        put(0, 0, f"review-cli - live board ({len(rows)} seat(s))")
        put(1, 0, f"{'ROLE':<14} {'MODEL':<26} {'STATUS':<22} {'ELAPSED':>8}")
        y = 2
        for row in rows:
            if y >= max_y - 1:
                break
            elapsed = (row.finished if row.finished is not None else now) - row.started
            status_text = row.status
            if row.status == STATUS_DONE and row.ok is False:
                status_text = f"{row.status} (failed)"
            put(
                y,
                0,
                f"{row.role[:14]:<14} {row.model[:26]:<26} {status_text[:22]:<22} "
                f"{elapsed:>7.1f}s",
            )
            y += 1
        y += 1
        if y < max_y:
            put(y, 0, "-- events --")
            y += 1
        tail = log_lines[-(max_y - y):] if max_y > y else []
        for line in tail:
            if y >= max_y:
                break
            put(y, 0, line)
            y += 1
        try:
            stdscr.refresh()
        except curses.error:
            pass


def start_if_enabled(
    seats: Sequence[tuple[str, str]], *, no_tui_flag: bool
) -> "BoardReporter | None":
    """The one entry point the CLI's board wiring needs: combines the auto-enable gate
    (`--no-tui` / `$REVIEW_NO_TUI`, both-streams-isatty) with the bounded curses
    acquisition. Never raises; returns None for every "don't show a TUI" reason, so a
    caller can always do `reporter = start_if_enabled(...)` unconditionally and treat
    `None` as "run exactly as before this feature existed"."""
    if no_tui_flag or no_tui_env():
        return None
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        return None
    return BoardReporter.start(seats)
