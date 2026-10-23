#!/usr/bin/env python3
"""`review feedback <run_id>` — the CLI-side path onto the overseer annotation store
(`reviewlib.dashboard.store`), added to close the feedback-fill-rate gap: before this,
`feedback`/`conscious`/`links` existed ONLY as a dashboard web-UI action with zero CLI
reminder, and real usage showed a 0% fill rate (`~/.config/review-cli/dashboard.json`
had never once been written).

Most scenarios are driven directly through `reviewlib.cli._feedback_subcommand` (fast,
precise control over `run_id`/env) — one subprocess-driven test at the end pins the
actual `bin/review feedback ...` dispatch wiring (the `_BARE_SUBCOMMANDS` registration),
mirroring `tests/test_stat_subcommand.py`'s own regression for `review stat`.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402

REVIEW = str(REPO_ROOT / "bin" / "review")


class _Isolated:
    """Point both the run-stats store and the dashboard annotation store at fresh
    temp files for one test."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        d = Path(self._dir.name)
        self._saved_stats = os.environ.get("REVIEW_STATS_FILE")
        self._saved_dash = os.environ.get("REVIEW_DASHBOARD_STORE")
        os.environ["REVIEW_STATS_FILE"] = str(d / "run-stats.jsonl")
        os.environ["REVIEW_DASHBOARD_STORE"] = str(d / "dashboard.json")
        return self

    def __exit__(self, *exc):
        for key, saved in (
            ("REVIEW_STATS_FILE", self._saved_stats),
            ("REVIEW_DASHBOARD_STORE", self._saved_dash),
        ):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        self._dir.cleanup()
        return False


def _run_feedback(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = _cli._feedback_subcommand(argv)
    return rc, out.getvalue(), err.getvalue()


def test_bare_read_with_nothing_recorded_shows_defaults_and_hint():
    with _Isolated():
        rc, out, err = _run_feedback(["some-run-id"])
        assert rc == 0
        assert "current annotation for run some-run-id" in out
        assert "'feedback': None" in out and "'useful': None" in out
        assert "nothing recorded yet" in err


def test_bare_read_after_a_ticket_only_write_does_not_show_contradictory_hint():
    """Review finding (Opus): the "nothing recorded yet" hint used to be gated only
    on feedback/useful being None -- a run annotated with ONLY a --ticket (feedback
    and useful both still None) got the real ticket link printed on stdout AND a
    contradictory "nothing recorded yet" on stderr in the same read. Must key off
    whether the record has EVER been written (`updated is None`), not off which
    specific fields happen to be set."""
    with _Isolated():
        _run_feedback(["r-ticket-only", "--ticket", "HYP-1"])
        rc, out, err = _run_feedback(["r-ticket-only"])
        assert rc == 0
        assert "'tickets': ['HYP-1']" in out
        assert "nothing recorded yet" not in err


def test_write_note_and_useful_then_read_back():
    with _Isolated():
        _stats.record_run(
            task_code="HYP-1",
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        rid = _stats._load_records()[-1]["run_id"]

        rc, out, _ = _run_feedback([rid, "--note", "nice catch", "--useful", "yes"])
        assert rc == 0
        assert "recorded for run" in out
        rc2, out2, _ = _run_feedback([rid])
        assert rc2 == 0
        assert "'feedback': 'nice catch'" in out2
        assert "'useful': True" in out2


def test_json_output_shape():
    with _Isolated():
        rc, out, _ = _run_feedback(["r1", "--note", "x", "--json"])
        assert rc == 0
        payload = json.loads(out)
        assert payload["run_id"] == "r1"
        assert payload["feedback"] == "x"
        assert payload["useful"] is None
        assert payload["links"] == {"prs": [], "tickets": []}


def test_useful_choice_variants_map_to_expected_booleans():
    with _Isolated():
        for flag, expected in (
            ("yes", True),
            ("y", True),
            ("true", True),
            ("no", False),
            ("n", False),
            ("false", False),
            ("unsure", None),
            ("unknown", None),
        ):
            rid = f"run-{flag}"
            _run_feedback([rid, "--useful", flag])
            _, out, _ = _run_feedback([rid, "--json"])
            assert json.loads(out)["useful"] is expected, flag


def test_task_code_shaped_like_a_ticket_is_auto_linked():
    with _Isolated():
        _stats.record_run(
            task_code="HYP-742",
            mode="quorum",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        rid = _stats._load_records()[-1]["run_id"]
        rc, out, _ = _run_feedback([rid, "--note", "auto-link check"])
        assert rc == 0
        assert "'tickets': ['HYP-742']" in out


def test_explicit_ticket_flag_is_not_overridden_by_auto_link():
    with _Isolated():
        _stats.record_run(
            task_code="HYP-742",
            mode="quorum",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        rid = _stats._load_records()[-1]["run_id"]
        rc, out, _ = _run_feedback([rid, "--ticket", "HYP-999"])
        assert rc == 0
        assert "'tickets': ['HYP-999']" in out
        assert "HYP-742" not in out.split("tickets")[-1]


def test_task_code_not_ticket_shaped_is_not_auto_linked():
    with _Isolated():
        _stats.record_run(
            task_code="fix-typo",
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        rid = _stats._load_records()[-1]["run_id"]
        rc, out, _ = _run_feedback([rid, "--note", "x"])
        assert rc == 0
        assert "'tickets': []" in out


def test_invalid_ticket_format_rejected_with_exit_2():
    with _Isolated():
        rc, _, err = _run_feedback(["r2", "--ticket", "not-a-ticket"])
        assert rc == 2
        assert "invalid ticket reference" in err


def test_invalid_pr_format_rejected_with_exit_2():
    with _Isolated():
        rc, _, err = _run_feedback(["r3", "--pr", "not-a-pr"])
        assert rc == 2
        assert "invalid PR reference" in err


def test_invalid_pr_aborts_before_useful_or_note_is_written():
    """Review finding: --useful/--note used to be written BEFORE --ticket/--pr were
    validated, so `--useful yes --pr garbage` persisted `useful=yes` and then failed
    with rc 2 -- reading as "nothing happened" when something had. Must be
    all-or-nothing: an invalid --pr aborts before ANY store write."""
    with _Isolated():
        rc, _, err = _run_feedback(
            ["r4", "--useful", "yes", "--note", "should not land", "--pr", "garbage"]
        )
        assert rc == 2
        assert "invalid PR reference" in err
        _, out, _ = _run_feedback(["r4"])
        assert "'feedback': None" in out
        assert "'useful': None" in out


def test_unknown_run_id_still_records_with_a_warning():
    with _Isolated():
        rc, out, err = _run_feedback(["totally-unknown-id", "--note", "x"])
        assert rc == 0
        assert "not found in run-stats.jsonl" in err
        assert "recorded for run totally-unknown-id" in out


def test_cli_dispatches_feedback_through_bin_review_subprocess():
    """Pins the actual `_BARE_SUBCOMMANDS` registration (mirrors
    tests/test_stat_subcommand.py's identical regression for `review stat`): a
    `feedback` verb with subcommand-scoped flags must dispatch normally, not fall
    through to the removed-bare-review migration pointer."""
    with tempfile.TemporaryDirectory() as fake_home:
        env = dict(os.environ)
        env["HOME"] = fake_home
        env["XDG_CONFIG_HOME"] = str(Path(fake_home) / ".config")
        proc = subprocess.run(
            [sys.executable, REVIEW, "feedback", "r1", "--note", "x", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "no subcommand given" not in proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["feedback"] == "x"


def test_note_that_looks_like_an_output_flag_is_not_stolen_by_the_prescan():
    """Review finding: `--note` was missing from `_VALUE_TAKING_OPTS`, so
    `_extract_output_path` read `--note '-o drops the report'` as `-o <path>` and left
    a valueless `--note` behind (usage error). The note must survive verbatim."""
    with tempfile.TemporaryDirectory() as fake_home:
        env = dict(os.environ)
        env["HOME"] = fake_home
        env["XDG_CONFIG_HOME"] = str(Path(fake_home) / ".config")
        proc = subprocess.run(
            [
                sys.executable,
                REVIEW,
                "feedback",
                "r2",
                "--note",
                "-o drops the report text",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert json.loads(proc.stdout)["feedback"] == "-o drops the report text"


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
