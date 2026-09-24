#!/usr/bin/env python3
"""Unit tests for the live curses TUI observer (review-cli#548, reviewlib/tui.py).

Three layers, each covered independently:
  1. The auto-enable GATE (`no_tui_env` / `start_if_enabled`) -- pure env/flag/isatty logic,
     no curses touched at all for the "must stay off" paths (asserted via a poisoned
     `BoardReporter.start` that raises if it is ever reached).
  2. `BoardReporter`'s own state machine + rendering, exercised against a FAKE `stdscr`
     (no real curses/terminal) constructed WITHOUT starting the background render thread
     (`_make_reporter` below mirrors `__init__` minus `Thread.start()`) -- deterministic,
     no timing/threading races, `_draw()` called synchronously.
  3. The OBSERVER WIRING into `panel.run_board_with_failover` / `run_panel_with_retry` --
     a spy reporter recording every call, run through the SAME `_FakeBackends` harness
     tests/test_failover_pool.py already uses for reserve-promotion/failover coverage, so
     these tests exercise the REAL dispatch path (not a re-implementation of it). Each such
     test also asserts the corresponding raw stderr print is SUPPRESSED with a reporter
     present and UNCHANGED (byte-identical) with reporter=None -- the acceptance-criterion
     "a non-interactive invocation is unaffected" guarantee, checked directly rather than
     assumed.

Real-terminal/pty coverage (TUI actually painting a screen, activating under a real pty,
--no-tui/$REVIEW_NO_TUI disabling it under a real pty, and the broken-terminal fallback
timing) lives in tests/test_tui_pty.py -- curses genuinely needs a real terminal device for
that, which this file deliberately does not attempt (test #1 above proves the ACQUISITION
gate never even tries when one isn't available, without needing one to prove it).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run by __main__.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402
import tempfile  # noqa: E402

os.environ.setdefault(
    "REVIEW_PROVIDER_CACHE", str(Path(tempfile.mkdtemp()) / "last-provider.json")
)
from _failover_neutralise import identity_provider_chain  # noqa: E402

import reviewlib.panel as panel  # noqa: E402
import reviewlib.provider_failover as provider_failover  # noqa: E402
import reviewlib.tui as tui  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from test_failover_pool import _FakeBackends  # noqa: E402

try:
    import pytest  # noqa: E402

    @pytest.fixture(autouse=True)
    def _neutralise_provider_failover():
        with identity_provider_chain():
            yield
except ImportError:  # plain-script harness (no pytest) applies it in __main__ instead
    pass


# === layer 1: the auto-enable gate ===============================================
class _Tty(io.StringIO):
    """A stream that answers `isatty()` with a FIXED value -- io.StringIO's own always
    answers False, which is exactly what the "non-interactive" tests want, but the
    "both streams ARE a tty" tests need the opposite without a real terminal."""

    def __init__(self, is_tty: bool):
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class _PoisonedStart:
    """Stand-in for `BoardReporter.start` that FAILS the test if it is ever called -- the
    whole point of the gate is that a disabled/non-interactive path never even attempts
    curses acquisition."""

    def __call__(self, *a, **kw):
        raise AssertionError("BoardReporter.start must not be reached when the TUI is off")


def test_no_tui_env_default_and_falsy_values():
    old = os.environ.pop("REVIEW_NO_TUI", None)
    try:
        for raw in (None, "", "0", "false", "False", "no", "NO", "off", "  0  "):
            if raw is None:
                os.environ.pop("REVIEW_NO_TUI", None)
            else:
                os.environ["REVIEW_NO_TUI"] = raw
            assert tui.no_tui_env() is False, raw
    finally:
        if old is None:
            os.environ.pop("REVIEW_NO_TUI", None)
        else:
            os.environ["REVIEW_NO_TUI"] = old


def test_no_tui_env_truthy_values():
    old = os.environ.pop("REVIEW_NO_TUI", None)
    try:
        for raw in ("1", "true", "TRUE", "yes", "on", "anything-else"):
            os.environ["REVIEW_NO_TUI"] = raw
            assert tui.no_tui_env() is True, raw
    finally:
        if old is None:
            os.environ.pop("REVIEW_NO_TUI", None)
        else:
            os.environ["REVIEW_NO_TUI"] = old


def _with_forced_tty(fn):
    """Run `fn()` with sys.stdout/sys.stderr swapped for FIXED-isatty streams, restoring
    the real ones afterward no matter what."""
    old_out, old_err = sys.stdout, sys.stderr
    try:
        fn()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_start_if_enabled_no_tui_flag_short_circuits_before_curses():
    old_start, old_env = tui.BoardReporter.start, os.environ.pop("REVIEW_NO_TUI", None)
    tui.BoardReporter.start = _PoisonedStart()

    def _run():
        sys.stdout, sys.stderr = _Tty(True), _Tty(True)
        assert tui.start_if_enabled([("architect", "codex")], no_tui_flag=True) is None

    try:
        _with_forced_tty(_run)
    finally:
        tui.BoardReporter.start = old_start
        if old_env is not None:
            os.environ["REVIEW_NO_TUI"] = old_env


def test_start_if_enabled_env_disable_short_circuits_before_curses():
    old_start, old_env = tui.BoardReporter.start, os.environ.get("REVIEW_NO_TUI")
    tui.BoardReporter.start = _PoisonedStart()
    os.environ["REVIEW_NO_TUI"] = "1"

    def _run():
        sys.stdout, sys.stderr = _Tty(True), _Tty(True)
        assert tui.start_if_enabled([("architect", "codex")], no_tui_flag=False) is None

    try:
        _with_forced_tty(_run)
    finally:
        tui.BoardReporter.start = old_start
        if old_env is None:
            os.environ.pop("REVIEW_NO_TUI", None)
        else:
            os.environ["REVIEW_NO_TUI"] = old_env


def test_start_if_enabled_requires_both_streams_tty():
    """A piped/redirected/agent-driven invocation (either stream not a real tty) must
    never even attempt curses acquisition -- this is the exact guarantee that keeps a
    non-interactive caller's behavior unchanged."""
    old_start, old_env = tui.BoardReporter.start, os.environ.pop("REVIEW_NO_TUI", None)
    tui.BoardReporter.start = _PoisonedStart()

    def _run():
        for out_tty, err_tty in ((False, True), (True, False), (False, False)):
            sys.stdout, sys.stderr = _Tty(out_tty), _Tty(err_tty)
            assert (
                tui.start_if_enabled([("architect", "codex")], no_tui_flag=False) is None
            )

    try:
        _with_forced_tty(_run)
    finally:
        tui.BoardReporter.start = old_start
        if old_env is not None:
            os.environ["REVIEW_NO_TUI"] = old_env


def test_start_returns_none_when_curses_absent_on_platform():
    """Stock-Windows-CPython simulation: `reviewlib.tui.curses` is None (the module-level
    ImportError fallback) -- `start`/`start_if_enabled` must return None, never raise,
    even with both streams reporting as a real tty."""
    old_curses, old_env = tui.curses, os.environ.pop("REVIEW_NO_TUI", None)
    tui.curses = None

    def _run():
        sys.stdout, sys.stderr = _Tty(True), _Tty(True)
        assert tui.start_if_enabled([("architect", "codex")], no_tui_flag=False) is None
        assert tui.BoardReporter.start([("architect", "codex")]) is None

    try:
        _with_forced_tty(_run)
    finally:
        tui.curses = old_curses
        if old_env is not None:
            os.environ["REVIEW_NO_TUI"] = old_env


def test_start_returns_none_promptly_without_a_real_terminal():
    """The genuine (not simulated) acquisition path: this test process's own stdout/stderr
    are NORMALLY not a real terminal (piped/captured under pytest/smoke.py, exactly like
    every CI/agent invocation) -- `BoardReporter.start` must return None quickly (well
    inside its own bounded timeout), never hang, never raise. This is the SAME code path
    a broken/dumb pty exercises (tests/test_tui_pty.py covers that shape under a REAL
    pty); this is the hermetic, no-pty-needed half of that guarantee.

    Does NOT assert `result is None` unconditionally: this file's own documented harness
    also supports `python tests/test_tui.py` run BY HAND from a real terminal, where
    acquisition legitimately succeeds -- asserting None there would be a false failure
    (review finding on this feature's own PR). Either way the reporter is torn down before
    returning, so a real success here can never leak the terminal into noecho/cbreak or
    leave a render thread drawing over the rest of this file's PASS/FAIL output."""
    started = time.monotonic()
    result = tui.BoardReporter.start([("architect", "codex")], timeout=3.0)
    elapsed = time.monotonic() - started
    if result is not None:
        result.close()
    else:
        assert elapsed < 2.0, f"acquisition took {elapsed:.2f}s outside a real terminal"


# === layer 2: BoardReporter's state machine + rendering (fake stdscr, no real curses) ====
class _FakeStdscr:
    """Records every `addnstr` call `_draw()` makes; `erase()`/`refresh()` are no-ops that
    just count invocations. Enough surface for `_draw()` to run unmodified."""

    def __init__(self, height: int = 24, width: int = 80):
        self.height = height
        self.width = width
        self.calls: list[tuple[int, int, str]] = []
        self.erase_count = 0
        self.refresh_count = 0

    def erase(self) -> None:
        self.erase_count += 1
        self.calls = []

    def getmaxyx(self):
        return (self.height, self.width)

    def addnstr(self, y: int, x: int, text: str, n: int) -> None:
        self.calls.append((y, x, text[: max(0, n)]))

    def refresh(self) -> None:
        self.refresh_count += 1


def _make_reporter(stdscr, seats) -> tui.BoardReporter:
    """Construct a BoardReporter with the SAME state `__init__` sets up, but WITHOUT
    starting the background render thread -- deterministic tests call `_draw()` directly
    instead of racing a 0.3s timer. Reaches into private state deliberately (there is no
    other way to unit-test the render output without a real terminal); mirrors the
    whitebox style tests/test_failover_pool.py already uses via `panel.resolve_backend`."""
    r = tui.BoardReporter.__new__(tui.BoardReporter)
    r._stdscr = stdscr
    r._lock = threading.Lock()
    r._rows = {}
    r._log = deque(maxlen=tui._MAX_LOG_LINES)
    r._closed = False
    r._stop = threading.Event()
    now = time.monotonic()
    for role, model in seats:
        r._rows[(role, model)] = tui._SeatRow(
            role=role, model=model, status=tui.STATUS_QUEUED, started=now
        )
    r._thread = None  # never started; close() below must tolerate this
    return r


def test_seat_status_creates_and_updates_rows():
    r = _make_reporter(_FakeStdscr(), [("architect", "codex")])
    assert r._rows[("architect", "codex")].status == tui.STATUS_QUEUED
    r.seat_status("architect", "codex", tui.STATUS_RUNNING)
    assert r._rows[("architect", "codex")].status == tui.STATUS_RUNNING
    # A promoted reserve is a genuinely NEW row, not a mutation of an existing one.
    r.seat_status("security", "claude", tui.STATUS_PROMOTED)
    assert ("security", "claude") in r._rows
    assert r._rows[("security", "claude")].status == tui.STATUS_PROMOTED
    r.seat_status("architect", "codex", tui.STATUS_DONE, ok=False)
    row = r._rows[("architect", "codex")]
    assert row.status == tui.STATUS_DONE
    assert row.ok is False


def test_event_log_is_bounded_and_ordered():
    r = _make_reporter(_FakeStdscr(), [])
    for i in range(tui._MAX_LOG_LINES + 10):
        r.event(f"line-{i}")
    assert len(r._log) == tui._MAX_LOG_LINES
    # The oldest lines were evicted; the most recent ones survive in order.
    assert list(r._log)[-1].endswith(f"line-{tui._MAX_LOG_LINES + 9}")
    assert list(r._log)[0].endswith("line-10")


def test_draw_renders_expected_columns_and_events():
    stdscr = _FakeStdscr()
    r = _make_reporter(
        stdscr, [("architect", "codex"), ("security", "claude:claude-opus-4-8")]
    )
    r.seat_status("architect", "codex", tui.STATUS_RUNNING)
    r.seat_status("security", "claude:claude-opus-4-8", tui.STATUS_RETRYING)
    r.event("[promote] board: codex failed -- promoting reserve X")
    r._draw()
    rendered = "\n".join(text for _y, _x, text in stdscr.calls)
    for expected in (
        "live board",
        "ROLE",
        "MODEL",
        "STATUS",
        "ELAPSED",
        "architect",
        "codex",
        tui.STATUS_RUNNING,
        "security",
        tui.STATUS_RETRYING,
        "-- events --",
        "promoting reserve",
    ):
        assert expected in rendered, f"{expected!r} missing from rendered frame:\n{rendered}"
    assert stdscr.erase_count == 1
    assert stdscr.refresh_count == 1


def test_draw_marks_a_failed_done_seat_distinctly():
    stdscr = _FakeStdscr()
    r = _make_reporter(stdscr, [("architect", "codex")])
    r.seat_status("architect", "codex", tui.STATUS_DONE, ok=False)
    r._draw()
    rendered = "\n".join(text for _y, _x, text in stdscr.calls)
    assert "done (failed)" in rendered


def test_close_is_idempotent_and_tolerates_no_thread():
    r = _make_reporter(_FakeStdscr(), [("architect", "codex")])
    r._thread = threading.Thread(target=lambda: None)
    r._thread.start()
    r.close()
    r.close()
    assert r._closed is True


# === layer 3: observer wiring into the REAL board-dispatch/failover machinery ============
class _SpyReporter:
    """Records every call in order; used to assert the reporter contract panel.py/retry.py
    actually drive, without any curses/rendering involved."""

    def __init__(self):
        self.statuses: list[tuple[str, str, str, bool | None]] = []
        self.events: list[str] = []

    def seat_status(self, role, model, status, *, ok=None):
        self.statuses.append((role, model, status, ok))

    def event(self, message):
        self.events.append(message)


def _capture_stderr(fn):
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        fn()
        return sys.stderr.getvalue()
    finally:
        sys.stderr = old


def test_reserve_promotion_routes_to_reporter_and_suppresses_stderr():
    board = [
        BoardReviewer(model="m-pool", role="architect", display="Pool"),
        BoardReviewer(model="m-reserve", role="security", display="Reserve"),
    ]
    pool, reserve = board[:1], board[1:]
    spy = _SpyReporter()
    with _FakeBackends({"m-pool": (1, "boom")}):
        stderr_out = _capture_stderr(
            lambda: panel.run_board_with_failover(
                pool, reserve, "prompt", "+x", REPO_ROOT, 5, reporter=spy
            )
        )
    assert "promoting reserve" not in stderr_out, stderr_out
    assert any(
        role == "architect" and model == "m-pool" and status == "done" and ok is False
        for role, model, status, ok in spy.statuses
    ), spy.statuses
    assert any(
        role == "security" and model == "m-reserve" and status == "promoted-from-reserve"
        for role, model, status, _ok in spy.statuses
    ), spy.statuses
    assert any(
        role == "security" and model == "m-reserve" and status == "done" and ok is True
        for role, model, status, ok in spy.statuses
    ), spy.statuses
    assert any("promoting reserve" in msg for msg in spy.events), spy.events


def test_reserve_promotion_without_reporter_is_byte_identical_to_before():
    board = [
        BoardReviewer(model="m-pool", role="architect", display="Pool"),
        BoardReviewer(model="m-reserve", role="security", display="Reserve"),
    ]
    pool, reserve = board[:1], board[1:]
    with _FakeBackends({"m-pool": (1, "boom")}):
        stderr_out = _capture_stderr(
            lambda: panel.run_board_with_failover(
                pool, reserve, "prompt", "+x", REPO_ROOT, 5
            )
        )
    # run_panel relabels ReviewResult.model to the board LABEL ("<display> [<role>]"), so
    # the promotion line names "Pool [architect]", not the bare model id "m-pool".
    assert (
        "[review-cli] board: Pool [architect] failed \u2014 promoting reserve Reserve "
        "[security] (m-reserve)" in stderr_out
    ), stderr_out


def test_pool_seats_marked_queued_then_running_then_done():
    board = [
        BoardReviewer(model="m-a", role="architect", display="A"),
        BoardReviewer(model="m-b", role="security", display="B"),
    ]
    spy = _SpyReporter()
    with _FakeBackends():
        panel.run_board_with_failover(board, [], "prompt", "+x", REPO_ROOT, 5, reporter=spy)
    seen = [(role, model, status) for role, model, status, _ok in spy.statuses]
    assert ("architect", "m-a", tui.STATUS_QUEUED) in seen
    assert ("architect", "m-a", tui.STATUS_RUNNING) in seen
    assert ("architect", "m-a", tui.STATUS_DONE) in seen
    idx = [i for i, s in enumerate(seen) if s[:2] == ("architect", "m-a")]
    statuses_in_order = [seen[i][2] for i in idx]
    assert statuses_in_order.index(tui.STATUS_QUEUED) < statuses_in_order.index(
        tui.STATUS_RUNNING
    )
    assert statuses_in_order.index(tui.STATUS_RUNNING) < statuses_in_order.index(
        tui.STATUS_DONE
    )


class _SequenceBackend:
    """panel.resolve_backend stub: a scripted (rc, body) per call for ONE model; extra
    calls beyond the script repeat its last entry. Lets a single seat fail transiently
    once, then recover -- something `_FakeBackends` (fixed per-model behaviour) can't do."""

    def __init__(self, model: str, sequence: list[tuple[int, str]]):
        self.model = model
        self.sequence = list(sequence)
        self.calls = 0

    def __enter__(self):
        self._old = panel.resolve_backend

        def _resolve(_model: str):
            def _backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
                idx = min(self.calls, len(self.sequence) - 1)
                rc, body = self.sequence[idx]
                self.calls += 1
                return ReviewResult(
                    model=model, command="fake", returncode=rc, stdout=body, stderr=""
                )

            return _backend

        panel.resolve_backend = _resolve
        return self

    def __exit__(self, *exc):
        panel.resolve_backend = self._old
        return False


def test_in_seat_retry_routes_to_reporter_and_suppresses_stderr():
    job = panel.PanelJob(model="m", prompt="p", role="architect", label="M [architect]")
    spy = _SpyReporter()
    with _SequenceBackend("m", [(1, "429 too many requests"), (0, "a real finding")]):
        stderr_out = _capture_stderr(
            lambda: panel.run_panel_with_retry([job], REPO_ROOT, 5, reporter=spy)
        )
    assert "transient failure" not in stderr_out, stderr_out
    assert "recovered on retry" not in stderr_out, stderr_out
    assert any(
        role == "architect" and model == "m" and status == tui.STATUS_RETRYING
        for role, model, status, _ok in spy.statuses
    ), spy.statuses
    assert any("[retry]" in msg for msg in spy.events), spy.events
    assert any("recovered" in msg for msg in spy.events), spy.events


def test_seat_role_key_consistent_between_board_and_retry_reporter_calls():
    """Regression (review finding on this feature's own PR): a role-less BoardReviewer
    (role="") that hits an in-seat retry must key EVERY reporter.seat_status call for
    that seat the SAME way. run_board_with_failover's own calls (queued/running/done)
    and run_panel_with_retry's retry-callback calls used to coalesce a falsy role
    differently, splitting one physical seat into TWO table rows (a stuck "running" row
    under one key, a phantom "retrying"/"done" row under the other)."""
    board = [BoardReviewer(model="m", role="", display="M")]
    spy = _SpyReporter()
    with _SequenceBackend("m", [(1, "429 too many requests"), (0, "a real finding")]):
        panel.run_board_with_failover(
            board, [], "prompt", "+x", REPO_ROOT, 5, reporter=spy
        )
    seen_roles = {role for role, model, _status, _ok in spy.statuses if model == "m"}
    assert seen_roles == {"general"}, (
        f"seat 'm' split across role keys {seen_roles} instead of one consistent row"
    )


def test_in_seat_retry_without_reporter_is_byte_identical_to_before():
    job = panel.PanelJob(model="m", prompt="p", role="architect", label="M [architect]")
    with _SequenceBackend("m", [(1, "429 too many requests"), (0, "a real finding")]):
        stderr_out = _capture_stderr(
            lambda: panel.run_panel_with_retry([job], REPO_ROOT, 5)
        )
    assert "transient failure" in stderr_out, stderr_out
    assert "recovered on retry" in stderr_out, stderr_out


def test_provider_failover_routes_to_reporter_and_suppresses_stderr():
    job = panel.PanelJob(
        model="fake:model", prompt="p", role="architect", label="Fake [architect]"
    )
    spy = _SpyReporter()
    old_chain = provider_failover.provider_chain
    old_is_default = provider_failover.is_default_provider_selection
    old_remember = provider_failover.remember_working_provider
    old_forget = provider_failover.forget_working_provider
    provider_failover.provider_chain = lambda model, **kw: ["fake:model", "fake:model-alt"]
    provider_failover.is_default_provider_selection = lambda model, overrides=None: False
    provider_failover.remember_working_provider = lambda *a, **kw: None
    provider_failover.forget_working_provider = lambda *a, **kw: None
    try:
        with _FakeBackends(
            {"fake:model": (1, "authentication failed"), "fake:model-alt": (0, "ok")}
        ):
            stderr_out = _capture_stderr(
                lambda: panel.run_panel_with_retry([job], REPO_ROOT, 5, reporter=spy)
            )
    finally:
        provider_failover.provider_chain = old_chain
        provider_failover.is_default_provider_selection = old_is_default
        provider_failover.remember_working_provider = old_remember
        provider_failover.forget_working_provider = old_forget
    assert "provider fake:model failed" not in stderr_out, stderr_out
    assert any("[failover]" in msg for msg in spy.events), spy.events


def test_provider_failover_without_reporter_is_byte_identical_to_before():
    job = panel.PanelJob(
        model="fake:model", prompt="p", role="architect", label="Fake [architect]"
    )
    old_chain = provider_failover.provider_chain
    old_is_default = provider_failover.is_default_provider_selection
    old_remember = provider_failover.remember_working_provider
    old_forget = provider_failover.forget_working_provider
    provider_failover.provider_chain = lambda model, **kw: ["fake:model", "fake:model-alt"]
    provider_failover.is_default_provider_selection = lambda model, overrides=None: False
    provider_failover.remember_working_provider = lambda *a, **kw: None
    provider_failover.forget_working_provider = lambda *a, **kw: None
    try:
        with _FakeBackends(
            {"fake:model": (1, "authentication failed"), "fake:model-alt": (0, "ok")}
        ):
            stderr_out = _capture_stderr(
                lambda: panel.run_panel_with_retry([job], REPO_ROOT, 5)
            )
    finally:
        provider_failover.provider_chain = old_chain
        provider_failover.is_default_provider_selection = old_is_default
        provider_failover.remember_working_provider = old_remember
        provider_failover.forget_working_provider = old_forget
    assert (
        "seat Fake [architect]: provider fake:model failed \u2014 switching to "
        "fake:model-alt" in stderr_out
    ), stderr_out


_TESTS = [
    test_no_tui_env_default_and_falsy_values,
    test_no_tui_env_truthy_values,
    test_start_if_enabled_no_tui_flag_short_circuits_before_curses,
    test_start_if_enabled_env_disable_short_circuits_before_curses,
    test_start_if_enabled_requires_both_streams_tty,
    test_start_returns_none_when_curses_absent_on_platform,
    test_start_returns_none_promptly_without_a_real_terminal,
    test_seat_status_creates_and_updates_rows,
    test_event_log_is_bounded_and_ordered,
    test_draw_renders_expected_columns_and_events,
    test_draw_marks_a_failed_done_seat_distinctly,
    test_close_is_idempotent_and_tolerates_no_thread,
    test_reserve_promotion_routes_to_reporter_and_suppresses_stderr,
    test_reserve_promotion_without_reporter_is_byte_identical_to_before,
    test_pool_seats_marked_queued_then_running_then_done,
    test_seat_role_key_consistent_between_board_and_retry_reporter_calls,
    test_in_seat_retry_routes_to_reporter_and_suppresses_stderr,
    test_in_seat_retry_without_reporter_is_byte_identical_to_before,
    test_provider_failover_routes_to_reporter_and_suppresses_stderr,
    test_provider_failover_without_reporter_is_byte_identical_to_before,
]


if __name__ == "__main__":
    failures = 0
    for fn in _TESTS:
        with identity_provider_chain():
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
