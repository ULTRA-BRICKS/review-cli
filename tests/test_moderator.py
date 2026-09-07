#!/usr/bin/env python3
"""Unit tests for moderator selection + runtime fallback (panel.py).

pick_moderators() builds the ordered candidate list (explicit, then available
MODERATOR_CANDIDATES, then panel[0]); run_moderator() walks it at run time and
falls back to the next candidate when one fails (non-zero exit OR empty output).
So a moderator that passes the cheap availability probe but dies at run time
(e.g. an Anthropic-disabled model) never leaves the panel without a synthesis.

Same harness style as tests/test_streaming.py: plain test_* functions run by the
__main__ block; backends are stubbed by reassigning panel module globals.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import panel as _panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402


def _result(model: str, rc: int = 0, out: str = "ok") -> ReviewResult:
    return ReviewResult(
        model=model, command=f"cmd {model}", returncode=rc, stdout=out, stderr=""
    )


def _with_stubs(*, available=None, runner=None):
    """Swap panel.backend_available / panel.run_single, returning a restore fn."""
    saved_avail = _panel.backend_available
    saved_run = _panel.run_single
    if available is not None:
        _panel.backend_available = available
    if runner is not None:
        _panel.run_single = runner

    def restore():
        _panel.backend_available = saved_avail
        _panel.run_single = saved_run

    return restore


def test_explicit_is_first_then_priority_then_panel():
    restore = _with_stubs(available=lambda m: True)
    try:
        got = _panel.pick_moderators("oc:custom", ["codex", "gemini"])
        # explicit first, then MODERATOR_CANDIDATES (opus, codex, gemini), then panel[0]
        assert got[0] == "oc:custom", got
        assert "claude:claude-opus-4-8" in got
        assert "codex" in got and "gemini" in got
    finally:
        restore()


def test_opus_is_default_first_when_no_explicit():
    restore = _with_stubs(available=lambda m: True)
    try:
        got = _panel.pick_moderators(None, ["codex", "gemini"])
        assert got[0] == "claude:claude-opus-4-8", got
    finally:
        restore()


def test_unavailable_candidates_are_filtered():
    # opus binary missing -> drops out; codex/gemini remain in priority order
    restore = _with_stubs(available=lambda m: m != "claude:claude-opus-4-8")
    try:
        got = _panel.pick_moderators(None, ["oc:x"])
        assert "claude:claude-opus-4-8" not in got, got
        assert got[0] == "codex", got
    finally:
        restore()


def test_dedup_preserves_order():
    restore = _with_stubs(available=lambda m: True)
    try:
        # explicit == panel[0] == a candidate: must appear once, first
        got = _panel.pick_moderators("codex", ["codex"])
        assert got.count("codex") == 1, got
        assert got[0] == "codex", got
    finally:
        restore()


def test_run_moderator_returns_first_success_without_fallback():
    calls = []

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        calls.append(model)
        return _result(model, rc=0, out="good")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(
            ["claude:claude-opus-4-8", "codex"], "p", Path("."), 5
        )
        assert res.returncode == 0 and res.model == "claude:claude-opus-4-8"
        assert calls == ["claude:claude-opus-4-8"], calls  # never touched the fallback
    finally:
        restore()


def test_run_moderator_falls_back_on_nonzero_exit():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        if model == "claude:claude-opus-4-8":
            return _result(
                model, rc=124, out=""
            )  # timeout-like failure (dead moderator)
        return _result(model, rc=0, out="recovered")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(
            ["claude:claude-opus-4-8", "codex"], "p", Path("."), 5
        )
        assert res.returncode == 0 and res.model == "codex", res
        assert res.stdout == "recovered"
    finally:
        restore()


def test_run_moderator_falls_back_on_empty_output():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        if model == "codex":
            return _result(model, rc=0, out="   ")  # exit 0 but no real content
        return _result(model, rc=0, out="real answer")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["codex", "gemini"], "p", Path("."), 5)
        assert res.model == "gemini" and res.stdout == "real answer", res
    finally:
        restore()


def test_run_moderator_falls_back_on_rc0_unavailable_sentinel():
    """codex review finding (2026-08 seat-cooldown feature): a cached-cooldown-skip
    result is rc=0 with NON-EMPTY stdout (the "is currently unavailable" cache-hit
    notice) — the same shape a genuine short moderator answer has. Before this fix,
    `_run_moderator_inner`'s bare `rc==0 and non-empty` check accepted it as a real
    summary, so a cooling-down FIRST candidate silently blocked fallback to a healthy
    one, and the cache-hit notice could be reported as the actual moderator synthesis.
    Pins that this sentinel shape now falls through to the next candidate exactly like
    a genuine failure."""

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        if model == "claude:claude-fable-5":
            return _result(
                model,
                rc=0,
                out=f"{model} is currently unavailable (cached: session limit; skip "
                "expires in 300s — reviewlib.seat_cooldown).",
            )
        return _result(model, rc=0, out="a real moderator summary")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(
            ["claude:claude-fable-5", "codex"], "p", Path("."), 5
        )
        assert res.model == "codex" and res.stdout == "a real moderator summary", res
    finally:
        restore()


def test_run_moderator_returns_last_when_all_fail():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        return _result(model, rc=1, out="")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["a", "b", "c"], "p", Path("."), 5)
        # all failed -> caller still gets a result (the last attempt) to surface
        assert res.model == "c" and res.returncode == 1, res
    finally:
        restore()


def test_run_moderator_all_empty_success_is_reported_as_failure():
    # every candidate exits 0 but with whitespace-only output: must NOT be
    # reported as success, or quorum/brainstorm claim a synthesis that isn't there
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        return _result(model, rc=0, out="   ")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["a", "b"], "p", Path("."), 5)
        assert res.returncode != 0, res
    finally:
        restore()


def test_run_moderator_accepts_single_string():
    # a lone moderator string must be treated as one candidate, not iterated char-by-char
    calls = []

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        calls.append(model)
        return _result(model, rc=0, out="ok")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator("codex", "p", Path("."), 5)
        assert calls == ["codex"], calls
        assert res.model == "codex" and res.returncode == 0
    finally:
        restore()


def test_cli_moderator_grok_alias_expands_before_pick_moderators():
    """`--moderator grok` expands to GROK_SEAT before pick_moderators. grok is not
    an auto-moderator candidate (MODERATOR_CANDIDATES stays opus/codex/gemini)."""
    from contextlib import redirect_stderr, redirect_stdout

    from reviewlib import cli
    from reviewlib.config import GROK_SEAT, MODERATOR_CANDIDATES
    from reviewlib.modes import quorum as quorum_mod

    assert GROK_SEAT not in MODERATOR_CANDIDATES
    assert "grok" not in MODERATOR_CANDIDATES
    captured: dict = {}

    def fake_quorum(question, models, diff, cwd, timeout, moderators, *a, **k):
        captured["moderators"] = list(moderators)
        return 0

    saved_q = quorum_mod.mode_quorum
    saved_cfg = cli.load_config
    saved_stdin = cli._read_stdin_if_piped
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    old_task = os.environ.get("REVIEW_TASK_CODE")
    old_env = os.environ.get("GEMINI_ENV_FILE")
    try:
        quorum_mod.mode_quorum = fake_quorum
        cli.load_config = lambda: {"models": ["codex"]}
        cli._read_stdin_if_piped = lambda: None
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        os.environ["REVIEW_TASK_CODE"] = "TEST-415"
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(
                [
                    "quorum",
                    "q",
                    "--task",
                    "TEST-415",
                    "--moderator",
                    "grok",
                    "-m",
                    "codex",
                    "-C",
                    str(REPO_ROOT),
                ]
            )
        assert rc == 0, rc
        assert captured["moderators"], captured
        assert captured["moderators"][0] == GROK_SEAT, captured
    finally:
        quorum_mod.mode_quorum = saved_q
        cli.load_config = saved_cfg
        cli._read_stdin_if_piped = saved_stdin
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env


def test_sessions_resume_moderator_grok_alias_expands():
    """`review sessions -s … --moderator grok` expands to GROK_SEAT before
    pick_moderators, same as the quorum/brainstorm flag."""
    from types import SimpleNamespace

    from reviewlib import cli
    from reviewlib.config import GROK_SEAT
    import reviewlib.sessions as sessions

    captured: dict = {}
    real_pick = cli.pick_moderators

    def fake_pick(explicit, panel):
        captured["explicit"] = explicit
        return real_pick(explicit, panel)

    class FakeSess:
        session_id = "s1"
        status = "open"
        completed_rounds = 1
        topic = "t"
        panel = ["codex"]
        moderator = "codex"

    saved_pick = cli.pick_moderators
    saved_find = sessions.find_session
    saved_resume = sessions.resume_session
    saved_cfg = cli.load_config
    try:
        cli.pick_moderators = fake_pick
        cli.load_config = lambda: {}
        sessions.find_session = lambda _id: FakeSess()
        sessions.resume_session = lambda *a, **k: 0
        ns = SimpleNamespace(
            resume="s1",
            model=["codex"],
            moderator="grok",
            cwd=str(REPO_ROOT),
            timeout=1,
            force=False,
            diff=False,
            staged=False,
        )
        rc = cli._resume_session_cli(ns)
        assert rc == 0, rc
        assert captured["explicit"] == GROK_SEAT, captured
    finally:
        cli.pick_moderators = saved_pick
        sessions.find_session = saved_find
        sessions.resume_session = saved_resume
        cli.load_config = saved_cfg


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
