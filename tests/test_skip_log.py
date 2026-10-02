#!/usr/bin/env python3
"""Unit tests for `reviewlib.process.write_skip_log` and its wiring into
`reviewlib.modes.review._mode_review_board`.

WHY this exists (task-cli logging audit, Ticket A, P1): `select_pool_and_reserve_with_reuse`
(reviewlib.config) explicitly documents that a hard-unavailable board seat (no key/CLI, an
active cooldown, an exhausted provider chain) ends up "in NEITHER list" -- it just vanishes
from the run. No backend function is ever invoked for such a seat, so none of the normal
per-call log paths (`_run_streamed`, `write_sidecar_log`) ever fire for it either -- distinct
from the unpaid-provider path (`unpaid_provider_result`), which DOES log because it reaches
inside a backend function before bailing. A completed `review diff` invocation that skipped a
seat left NO trace anywhere on disk for it (`--show-board` is a separate, later, read-only
probe of the same predicate, not a record of what a specific past run actually did).

Offline (no model call, no network — the fake backend below never spawns a subprocess).
Plain-script harness (mirrors tests/test_pool_shortfall_warning.py): each test_* is run by
__main__, and also pytest-discoverable.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import process as process_mod  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes import review as review_mod  # noqa: E402


def _make_tmp_log():
    """A per-test fresh log dir, exported via $REVIEW_LOG_DIR so write_skip_log / log_dir
    point at it. Mirrors test_inseat_retry.py's identical helper. Returns a restore
    callable."""
    saved = os.environ.get("REVIEW_LOG_DIR")
    tmp = tempfile.mkdtemp(prefix="review-skip-log-")
    os.environ["REVIEW_LOG_DIR"] = tmp

    def _restore():
        if saved is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = saved

    return _restore


def _skip_log_files() -> list[Path]:
    return sorted(process_mod.log_dir().glob("*-skip-*.log"))


def test_write_skip_log_writes_a_durable_file_with_model_and_reason():
    restore = _make_tmp_log()
    try:
        before = _skip_log_files()
        assert before == []
        path = process_mod.write_skip_log(
            "omp:kimi-code/k3", reason="omp: harness 5h limit exhausted"
        )
        after = _skip_log_files()
        assert len(after) == 1, after
        assert path == after[0]
        text = path.read_text(encoding="utf-8")
        assert "omp:kimi-code/k3" in text
        assert "omp: harness 5h limit exhausted" in text
        assert "SKIP-EVENT" in text
        # Not mistaken for a real dispatched call — dashboard.parser._CALL_RE requires a
        # trailing `-r<digits>.log`, which this filename never has.
        import re

        assert not re.match(r"^.+-r\d+\.log$", path.name), path.name
    finally:
        restore()


def test_write_skip_log_is_best_effort_on_os_error(monkeypatch):
    """A log we can't write must never raise -- see write_retry_log's identical contract."""
    restore = _make_tmp_log()
    try:

        def _boom(*_a, **_k):
            raise OSError("disk full (test)")

        saved_open = process_mod.os.open
        process_mod.os.open = _boom
        try:
            path = process_mod.write_skip_log("m1", reason="boom")
        finally:
            process_mod.os.open = saved_open
        assert isinstance(path, Path)  # returns a path even though the write failed
    finally:
        restore()


def test_mode_review_board_logs_a_skipped_seat_through_the_real_call_site():
    """Locks the CALL SITE, not just the function: drives the real `mode_review` ->
    `_mode_review_board` path with a 2-seat board (one available, one not) and a fake
    dispatch backend (no network/subprocess), and asserts a `*-skip-*.log` file actually
    lands on disk naming the skipped seat's model + reason -- before this fix, nothing did."""
    restore = _make_tmp_log()
    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("missing-provider:model", "tests", "Missing"),
    ]

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return review_mod.ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout=f"ok from {model}",
            stderr="",
        )

    import reviewlib.panel as panel_mod

    saved_resolve = panel_mod.resolve_backend
    saved_available = review_mod.backend_available
    saved_reason = review_mod.backend_unavailable_reason
    panel_mod.resolve_backend = lambda _m: _fake_backend
    review_mod.backend_available = lambda model: model == "codex"
    review_mod.backend_unavailable_reason = lambda model: (
        None if model == "codex" else "patched unavailable (test, skip-log)"
    )
    try:
        assert _skip_log_files() == []
        buf = io.StringIO()
        with redirect_stdout(buf):
            review_mod.mode_review(
                [],
                "Review this diff.",
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=len(board),
            )
        skip_logs = _skip_log_files()
        assert len(skip_logs) == 1, skip_logs
        text = skip_logs[0].read_text(encoding="utf-8")
        assert "missing-provider:model" in text, text
        assert "patched unavailable (test, skip-log)" in text, text
        # The LIVE codex seat must never itself produce a skip log.
        assert "model=codex" not in text, text
    finally:
        panel_mod.resolve_backend = saved_resolve
        review_mod.backend_available = saved_available
        review_mod.backend_unavailable_reason = saved_reason
        restore()


TESTS = [
    test_write_skip_log_writes_a_durable_file_with_model_and_reason,
    test_mode_review_board_logs_a_skipped_seat_through_the_real_call_site,
]


if __name__ == "__main__":
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    # test_write_skip_log_is_best_effort_on_os_error uses a pytest-style monkeypatch
    # fixture and is exercised only under pytest (see below); it is deliberately not
    # in TESTS/__main__.
    if failures:
        sys.exit(1)
    print("all tests passed")
