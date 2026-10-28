#!/usr/bin/env python3
"""Characterization of `reviewlib.stats` (review-cli#721): the exact observable behavior
of the run-stats store before it was split into modules, pinned as golden vectors.

Each scenario writes a JSONL store, calls one public entry point and compares the result
with `tests/fixtures/stats_vectors/*.json`. The vectors were generated from the ORIGINAL
single-file implementation (`REGEN_STATS_GOLDEN=1 python tests/test_stats_characterization.py`
regenerates them -- only legitimate when behavior is meant to change); the refactor must
reproduce every one byte for byte. The quorum gate is a self-merge-authority check, so its
verdicts, counters, error strings and key SETS are pinned, not just passed/failed.

Plain test_* functions, run by pytest OR the __main__ block. The store is redirected with
$REVIEW_STATS_FILE; the seat-cooldown store is stubbed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import seat_cooldown as _seat_cooldown  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402

VECTOR_DIR = Path(__file__).resolve().parent / "fixtures" / "stats_vectors"
REGEN = os.environ.get("REGEN_STATS_GOLDEN") == "1"

OLD_TS = "2026-07-01T00:00:00+00:00"  # before the role-tracking cutoff
NEW_TS = "2026-09-01T00:00:00+00:00"  # after it
REPO_A = "git.hyperide.ai/ultrabricks/review-cli"
REPO_B = "github.com/other/thing"
ROLES3 = ["correctness", "security", "performance"]
MODELS3 = ["m-a", "m-b", "m-c"]


@contextlib.contextmanager
def _store(lines: list[str] | None):
    """A temp store holding `lines` (None = no file at all)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "run-stats.jsonl"
        if lines is not None:
            path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        old = os.environ.get("REVIEW_STATS_FILE")
        os.environ["REVIEW_STATS_FILE"] = str(path)
        try:
            yield path
        finally:
            if old is None:
                os.environ.pop("REVIEW_STATS_FILE", None)
            else:
                os.environ["REVIEW_STATS_FILE"] = old


def _rec(task="T-1", n=0, **kw) -> dict:
    base = {
        "v": 5,
        "ts": f"2026-09-01T00:{n:02d}:00+00:00",
        "mode": "review",
        "pool_size": 3,
        "models": list(MODELS3),
        "duration_seconds": 10.0 + n,
        "ok_count": 3,
        "fail_count": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "passed": True,
        "task_code": task,
    }
    base.update(kw)
    return base


def _dump(records: list[dict]) -> list[str]:
    return [json.dumps(r, separators=(",", ":")) for r in records]


def _cooling(table: dict[str, dict]):
    def fake(model, access_method="cli", **_kw):
        if access_method != "cli":
            return None
        return table.get(model)

    return mock.patch.object(_seat_cooldown, "active_cooldown", fake)


def _check(records, task="T-1", cooldowns=None, **kw) -> dict:
    with _store(_dump(records)), _cooling(cooldowns or {}):
        return _stats.quorum_check(task, **kw)


def _full(n, **kw) -> list[dict]:
    """`n` passed role-tagged iterations on three distinct models/roles."""
    return [_rec(n=i, roles=list(ROLES3), repo_id=REPO_A, **kw) for i in range(n)]


def _quorum_scenarios() -> dict[str, tuple]:
    """name -> (records, check kwargs, cooldowns)."""
    out: dict[str, tuple] = {}
    legacy = [_rec(n=i, ts=OLD_TS, roles=None) for i in range(3)]
    for r in legacy:
        r.pop("roles", None)
    roleless_new = [_rec(n=i, mode="quorum") for i in range(3)]
    out["bare_three_roles_one_iter"] = (_full(1), {"min_iter": 3}, {})
    out["bare_two_roles"] = (
        [_rec(roles=["correctness", "security"], repo_id=REPO_A)],
        {"min_iter": 1},
        {},
    )
    out["bare_unknown_roles_not_credited"] = (
        [_rec(roles=["nope", "typo", "also-bad"])],
        {"min_iter": 1},
        {},
    )
    out["bare_legacy_fallback"] = (legacy, {"min_iter": 3}, {})
    out["bare_legacy_fallback_repo_ctx"] = (
        legacy,
        {"min_iter": 3, "repo_id": REPO_A},
        {},
    )
    out["bare_legacy_fallback_diff_ctx"] = (
        legacy,
        {"min_iter": 3, "repo_id": REPO_A, "diff_files": ["a.py"]},
        {},
    )
    out["bare_gap_quorum"] = (roleless_new, {"min_iter": 3}, {})
    out["bare_gap_mixed_legacy_and_quorum"] = (
        legacy[:1] + roleless_new[:1],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_review_roleless"] = (
        [_rec(n=1)],
        {"min_iter": 1, "repo_id": REPO_A},
        {},
    )
    out["bare_gap_review_seats_failed"] = (
        [_rec(n=1, passed=False, ok_count=0, fail_count=3)],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_visual_roleless"] = (
        [_rec(n=1, mode="visual")],
        {"min_iter": 1, "repo_id": REPO_A, "diff_files": ["x.py"]},
        {},
    )
    out["bare_gap_visual_and_review"] = (
        [_rec(n=1, mode="visual"), _rec(n=2)],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_board_and_panel"] = (
        [_rec(n=1), _rec(n=2, mode="quorum")],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_unknown_mode"] = (
        [_rec(n=1, mode="")],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_naive_ts"] = (
        [_rec(n=1, ts="2026-01-01T00:00:00")],
        {"min_iter": 1},
        {},
    )
    out["bare_gap_z_ts_legacy_falls_back"] = (
        [_rec(n=1, ts="2026-01-01T00:00:00Z")],
        {"min_iter": 1},
        {},
    )
    out["bare_failed_role_iteration_blocks_fallback"] = (
        [_rec(n=1, ts=OLD_TS, passed=False, roles=["correctness"]), *legacy],
        {"min_iter": 1},
        {},
    )
    out["bare_all_mismatched_keeps_role_mode"] = (
        [_rec(n=i, repo_id=REPO_B, roles=list(ROLES3)) for i in range(2)],
        {"min_iter": 1, "repo_id": REPO_A},
        {},
    )
    out["bare_no_history_for_task"] = ([_rec(task="OTHER")], {"min_iter": 1}, {})
    out["bare_failed_only"] = (
        [_rec(passed=False, roles=list(ROLES3))],
        {"min_iter": 1},
        {},
    )
    out["bare_unverdicted_roles_pre137"] = (
        [_rec(passed=None, roles=list(ROLES3))],
        {"min_iter": 1},
        {},
    )
    out["roles_usable_roles_credit_failed_run"] = (
        [_rec(passed=False, usable_roles=list(ROLES3), roles=["correctness"])],
        {"min_iter": 1, "min_roles": 3},
        {},
    )
    out["roles_pre137_failed_roles_not_credited"] = (
        [_rec(passed=False, roles=list(ROLES3))],
        {"min_iter": 1, "min_roles": 3},
        {},
    )
    out["roles_explicit_min_roles_only"] = (
        _full(1),
        {"min_iter": 5, "min_roles": 2},
        {},
    )
    out["roles_misaligned_models_roles"] = (
        [_rec(models=["m-a"], roles=["correctness", "security"])],
        {"min_iter": 1, "min_roles": 2},
        {},
    )
    out["both_floors_met"] = (
        _full(3),
        {"min_iter": 3, "min_models": 3, "min_roles": 3},
        {},
    )
    out["both_floors_models_short"] = (
        [_rec(models=["m-a"], roles=list(ROLES3))],
        {"min_iter": 1, "min_models": 3, "min_roles": 3},
        {},
    )
    out["models_only_pass_with_advisory"] = (
        [_rec(n=i) for i in range(3)],
        {"min_iter": 3, "min_models": 3},
        {},
    )
    out["models_only_short_iter"] = (
        [_rec(n=0)],
        {"min_iter": 3, "min_models": 3},
        {},
    )
    out["models_only_suggestion_fires"] = (
        [
            _rec(n=0, models=["m-a", "m-b"], roles=["correctness", "security"]),
            _rec(n=1, models=["m-a", "m-a"], roles=["performance", "consistency"]),
        ],
        {"min_iter": 2, "min_models": 3},
        {},
    )
    out["models_only_suggestion_monoculture_suppressed"] = (
        [
            _rec(n=0, models=["m-a", "m-a", "m-a"], roles=list(ROLES3)),
        ],
        {"min_iter": 1, "min_models": 3},
        {},
    )
    out["models_only_no_history"] = ([], {"min_iter": 1, "min_models": 2}, {})
    out["models_only_failed_models_dont_count"] = (
        [_rec(passed=False), _rec(n=1, models=["m-a"])],
        {"min_iter": 1, "min_models": 2},
        {},
    )
    out["stalled_models_listed"] = (
        [_rec(n=0, models=["m-a", "m-b"], passed=False)],
        {"min_iter": 1, "min_models": 2},
        {
            "m-a": {
                "reason": "usage limit",
                "remaining_seconds": 61.4,
                "fail_count": 2,
            },
            "zzz": {"reason": "unrelated", "remaining_seconds": 5, "fail_count": 1},
        },
    )
    out["identity_mismatch_repo_and_diff"] = (
        [
            _rec(n=0, repo_id=REPO_B, diff_files=["a.py"]),
            _rec(n=1, repo_id=REPO_A, diff_files=["z.py"]),
            _rec(n=2, repo_id=REPO_A, diff_files=["a.py"], roles=list(ROLES3)),
            _rec(n=3),
        ],
        {"min_iter": 3, "min_models": 3, "repo_id": REPO_A, "diff_files": ["a.py"]},
        {},
    )
    out["identity_repo_only_foreign_error"] = (
        [
            _rec(n=0, repo_id=REPO_B, passed=False),
            _rec(n=1, repo_id=REPO_B, roles=list(ROLES3)),
        ],
        {"min_iter": 1, "min_models": 3, "repo_id": REPO_A},
        {},
    )
    out["identity_alias_moved_repo_matches"] = (
        [_rec(repo_id="github.com/alex-mextner/review-cli", roles=list(ROLES3))],
        {"min_iter": 1, "repo_id": REPO_A, "diff_files": ["a.py"]},
        {},
    )
    out["identity_mismatch_details_capped"] = (
        [_rec(n=i % 60, repo_id=REPO_B, diff_files=["q.py"]) for i in range(57)],
        {"min_iter": 1, "min_models": 1, "repo_id": REPO_A, "diff_files": ["a.py"]},
        {},
    )
    out["identity_foreign_details_capped"] = (
        [_rec(n=i % 60, repo_id=REPO_B) for i in range(53)],
        {"min_iter": 1, "min_models": 1, "repo_id": REPO_A},
        {},
    )
    out["identity_no_ctx_counts_everything"] = (
        [_rec(repo_id=REPO_B), _rec(n=1, repo_id=REPO_A)],
        {"min_iter": 2, "min_models": 3},
        {},
    )
    out["twin_hash_code_counted"] = (
        [_rec(task="#105", roles=list(ROLES3))],
        {"min_iter": 1},
        {},
    )
    out["twin_near_miss_not_passed"] = (
        [_rec(task="105", passed=False), _rec(task="#105", n=1, passed=False)],
        {"min_iter": 1, "min_models": 3},
        {},
    )
    out["twin_near_miss_passed_but_mismatched"] = (
        [_rec(task="105", repo_id=REPO_B), _rec(task="#105", n=1, passed=False)],
        {"min_iter": 1, "min_models": 3, "repo_id": REPO_A},
        {},
    )
    out["twin_near_miss_counted"] = (
        [_rec(task="105"), _rec(task="#105", n=1, passed=False)],
        {"min_iter": 5, "min_models": 3},
        {},
    )
    return out


_SEARCHED_TASK = {
    "twin_hash_code_counted": "#105",
    "twin_near_miss_not_passed": "105",
    "twin_near_miss_passed_but_mismatched": "#105",
    "twin_near_miss_counted": "#105",
}


def _run_quorum_scenario(name: str, spec: tuple) -> dict:
    records, kwargs, cooldowns = spec
    task = _SEARCHED_TASK.get(name, "T-1")
    return _check(records, task=task, cooldowns=cooldowns, **kwargs)


def _invalid_input_vectors() -> dict[str, dict]:
    out: dict[str, dict] = {}
    cases = {
        "min_iter_zero": {"min_iter": 0},
        "min_models_zero": {"min_iter": 1, "min_models": 0},
        "min_roles_zero": {"min_iter": 1, "min_roles": 0},
        "all_three_bad": {"min_iter": 0, "min_models": 0, "min_roles": -1},
        "bare_min_iter_negative": {"min_iter": -2},
    }
    for name, kw in cases.items():
        out[name] = _check([_rec()], **kw)
    out["bad_task_code_space"] = _check([_rec()], task="bad code", min_iter=1)
    out["bad_task_code_long"] = _check(
        [_rec()], task="x" * 121, min_iter=1, min_models=2
    )
    out["bad_task_code_with_roles"] = _check(
        [_rec()], task="a\tb", min_iter=1, min_roles=2
    )
    out["blank_task_code"] = _check([_rec()], task="   ", min_iter=1)
    return out


def _store_state_vectors() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as td, _cooling({}):
        old = os.environ.get("REVIEW_STATS_FILE")
        try:
            os.environ["REVIEW_STATS_FILE"] = str(Path(td) / "missing.jsonl")
            out["missing_store_bare"] = _stats.quorum_check("T-1", min_iter=1)
            out["missing_store_models"] = _stats.quorum_check(
                "T-1", min_iter=1, min_models=2
            )
            os.environ["REVIEW_STATS_FILE"] = td  # a directory: unreadable
            out["unreadable_store"] = _stats.quorum_check("T-1", min_iter=1)
        finally:
            if old is None:
                os.environ.pop("REVIEW_STATS_FILE", None)
            else:
                os.environ["REVIEW_STATS_FILE"] = old
    with _store(["not json", "", '{"no":"duration"}']), _cooling({}):
        out["junk_only_store"] = _stats.quorum_check("T-1", min_iter=1)
    return out


def _read_vectors(name: str) -> dict:
    return json.loads((VECTOR_DIR / name).read_text(encoding="utf-8"))


def _compare_or_write(name: str, actual: dict) -> None:
    tmp_root = re.escape(tempfile.gettempdir().rstrip("/"))
    scrubbed = re.sub(
        tmp_root + r"/tmp\w+", "<TMP>", json.dumps(actual, sort_keys=True)
    )
    if REGEN:
        VECTOR_DIR.mkdir(parents=True, exist_ok=True)
        (VECTOR_DIR / name).write_text(
            json.dumps(json.loads(scrubbed), indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    # Canonical JSON text, not `==` on parsed values: `True == 1` in Python, but a
    # consumer of the --check --json payload tells a boolean from a number.
    expected = _read_vectors(name)
    assert sorted(expected) == sorted(json.loads(scrubbed)), name
    for key, want in expected.items():
        got = json.loads(scrubbed)[key]
        assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True), (
            key,
            got,
            want,
        )


def test_quorum_check_vectors_match_original_implementation():
    actual = {
        name: _run_quorum_scenario(name, spec)
        for name, spec in _quorum_scenarios().items()
    }
    _compare_or_write("quorum_scenarios.json", actual)


def test_quorum_check_rejections_match_original_implementation():
    _compare_or_write("quorum_rejections.json", _invalid_input_vectors())


def test_quorum_check_store_states_match_original_implementation():
    _compare_or_write("quorum_store_states.json", _store_state_vectors())


def test_quorum_check_result_key_sets_are_pinned():
    """The key SET per scenario is part of the --check --json contract."""
    actual = {
        name: sorted(_run_quorum_scenario(name, spec))
        for name, spec in _quorum_scenarios().items()
    }
    _compare_or_write("quorum_key_sets.json", actual)


# --- identity helpers ---------------------------------------------------------------------


def _identity_vectors() -> dict:
    remotes = [
        "https://x-access-token:ghp_abc@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "ssh://git@Host.COM:22/org/repo",
        "ssh://git@host.com:2222/org/repo",
        "https://github.com/alex-mextner/review-cli/",
        "git@git.hyperide.ai:ultrabricks/sme-archiving-gc-tmp.git",
        "",
        "   ",
        "nonsense",
        "https://host/",
        "https://host",
        "/local/path/repo",
    ]
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "diff --git a/old.py b/new.py\n"
        "diff --git a/gone.py b/dev/null\n"
        'diff --git a/"weird" b/"weird"\n'
    )
    codes = ["105", "#105", "HYP-742", "#HYP-742", " X ", "", None, "１０５"]
    twins = {
        c: _stats._github_issue_twin(c)
        for c in ["105", "#105", "abc", "#abc", "１０５"]
    }
    keys = {
        c: sorted(_stats._github_issue_task_keys(c)) for c in ["105", "#105", "abc"]
    }
    return {
        "remotes": {r: _stats.normalize_repo_remote(r) for r in remotes},
        "files": _stats.extract_diff_files(diff),
        "files_empty": _stats.extract_diff_files(""),
        "files_none": _stats.extract_diff_files(None),
        "hash": _stats.diff_content_hash("héllo\udcff"),
        "task_codes": {str(c): _normalize_or_error(c) for c in codes},
        "twins": twins,
        "keys": keys,
        "run_id": _stats.run_id_for("2026-01-01T00:00:00+00:00", "review", "T-1"),
        "run_id_no_task": _stats.run_id_for("2026-01-01T00:00:00+00:00", "qa", None),
        "durations": [
            _stats.fmt_duration(s) for s in (-5, 0, 0.4, 47, 372, 3780, 90061)
        ],
        "parse_ts": {
            str(t): _iso(t)
            for t in (
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00+02:00",
                "x",
                "",
                None,
                5,
            )
        },
        "comparable": [
            _stats._comparable_repo_id("git@github.com:alex-mextner/rig-cli.git"),
            _stats._comparable_repo_id("path:/tmp/x"),
        ],
        "recorded": [
            _stats._recorded_repo_id({"repo_id": "git@github.com:org/repo.git"}),
            _stats._recorded_repo_id({"repo_id": ""}),
            _stats._recorded_repo_id({"repo_id": 5}),
            _stats._recorded_repo_id({}),
        ],
    }


def _normalize_or_error(code):
    try:
        return _stats.normalize_task_code(code)
    except ValueError as exc:
        return f"ValueError: {exc}"


def _iso(value):
    parsed = _stats.parse_iso_ts(value)
    return None if parsed is None else parsed.isoformat()


def test_identity_helpers_match_original_implementation():
    _compare_or_write("identity_helpers.json", _identity_vectors())


def _classify_vectors() -> dict:
    cases = {
        "no_repo": {},
        "repo_mismatch": {"repo_id": REPO_B, "diff_files": ["a.py"]},
        "diff_mismatch": {"repo_id": REPO_A, "diff_files": ["z.py"]},
        "verified_overlap": {"repo_id": REPO_A, "diff_files": ["a.py", "z.py"]},
        "verified_no_files": {"repo_id": REPO_A},
        "verified_empty_files": {"repo_id": REPO_A, "diff_files": []},
        "files_not_list": {"repo_id": REPO_A, "diff_files": "a.py"},
    }
    out = {}
    for name, item in cases.items():
        for ctx_name, files in (("none", None), ("a", ["a.py"]), ("empty", [])):
            cur = _stats._current_files_frozenset(files)
            out[f"{name}/{ctx_name}"] = list(
                _stats._classify_iteration_identity(item, REPO_A, cur)
            )
    return out


def test_classify_iteration_identity_matches_original_implementation():
    _compare_or_write("classify_identity.json", _classify_vectors())


def _role_helper_vectors() -> dict:
    items = [
        {
            "models": ["a", "b", "a", 3],
            "roles": ["correctness", "security", "bogus", 7],
        },
        {"models": ["c"], "roles": ["performance"], "passed": True},
        {"models": None, "roles": None},
        {"usable_roles": ["consistency"], "passed": False, "roles": ["correctness"]},
        {"passed": True, "roles": ["security"]},
        {"passed": False, "roles": ["security"]},
        {"usable_roles": None, "passed": True, "roles": ["security"]},
        {"roles": []},
    ]
    return {
        "models": _stats._distinct_models(items),
        "roles": _stats._distinct_roles(items),
        "credited": _stats._seat_credited_roles(items),
        "any_role": [
            _stats._has_any_role_data(items),
            _stats._has_any_role_data(items[2:3]),
            _stats._has_any_role_data(items[7:]),
        ],
        "behind": _stats._models_behind_role_coverage(items),
        "predate": [
            _stats._iteration_predates_role_tracking(i)
            for i in (
                {"ts": OLD_TS},
                {"ts": NEW_TS},
                {"ts": _stats._ROLE_TRACKING_CUTOFF.isoformat()},
                {"ts": "2026-01-01T00:00:00"},
                {"ts": "2026-01-01T00:00:00Z"},
                {},
            )
        ],
        "all_predate": [
            _stats._all_iterations_predate_role_tracking([]),
            _stats._all_iterations_predate_role_tracking([{"ts": OLD_TS}]),
            _stats._all_iterations_predate_role_tracking(
                [{"ts": OLD_TS}, {"ts": NEW_TS}]
            ),
        ],
    }


def test_role_helpers_match_original_implementation():
    _compare_or_write("role_helpers.json", _role_helper_vectors())


def _bucket_vectors() -> dict:
    items = [
        {"iteration": 1, "repo_id": REPO_A, "diff_files": ["a.py"], "ts": "t1"},
        {"iteration": 2, "repo_id": REPO_B, "diff_files": ["a.py"], "ts": "t2"},
        {"iteration": 3, "repo_id": REPO_A, "diff_files": ["z.py"], "ts": "t3"},
        {"iteration": 4, "ts": "t4"},
    ]
    out = {}
    for label, repo, files in (
        ("none", None, None),
        ("repo", REPO_A, None),
        ("diff", REPO_A, ["a.py"]),
    ):
        verified, mismatched, unverifiable = (
            _stats._sort_passed_iterations_into_buckets(items, repo, files)
        )
        out[f"buckets/{label}"] = [verified, mismatched, unverifiable]
        out[f"excluding/{label}"] = _stats._iterations_excluding_mismatched(
            items, repo, files
        )
    out["foreign"] = [list(_stats._foreign_repo_iterations(items, REPO_A))]
    many = [{"iteration": i, "repo_id": REPO_B} for i in range(60)]
    details, count = _stats._foreign_repo_iterations(many, REPO_A)
    out["foreign_many"] = [len(details), count]
    return out


def test_identity_bucketing_matches_original_implementation():
    _compare_or_write("identity_buckets.json", _bucket_vectors())


# --- writer ---------------------------------------------------------------------------------


def _writer_vectors() -> dict:
    started = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    out: dict = {}
    with _store(None) as path:
        ok = _stats.record_run(
            task_code=" T-9 ",
            mode="review",
            models=["m-a", "m-b"],
            duration_seconds=1.23456,
            ok_count=2,
            fail_count=1,
            started=started,
            passed=True,
            repo_id=REPO_A,
            diff_files=["b.py", "a.py"],
            diff_sha256="ff",
            roles=["correctness", "security"],
            usable_roles=["correctness"],
            prompt_tokens=7,
            output_tokens=9,
            level="L2",
        )
        out["ok"] = ok
        out["mode_bits"] = stat.S_IMODE(path.stat().st_mode)
        out["lines"] = path.read_text(encoding="utf-8").splitlines()
        _stats.record_run(
            task_code="bad task",
            mode="qa",
            models=["m-a"],
            duration_seconds=0,
            ok_count=0,
            fail_count=0,
            started=started,
            roles=["x", "y"],
            usable_roles=[],
            diff_files=[],
            run_id="pinned",
        )
        _stats.record_run(
            task_code=None,
            mode="just-ask",
            models=[],
            duration_seconds=2,
            ok_count=1,
            fail_count=0,
            started=started,
            roles=[],
            passed=False,
        )
        out["lines_after"] = path.read_text(encoding="utf-8").splitlines()
        path.chmod(0o644)
        _stats.record_run(
            mode="review",
            models=["m"],
            duration_seconds=1,
            ok_count=1,
            fail_count=0,
            started=started,
        )
        out["mode_bits_after_rewrite"] = stat.S_IMODE(path.stat().st_mode)
    return out


def test_record_run_matches_original_implementation():
    _compare_or_write("writer.json", _writer_vectors())


def test_record_run_is_best_effort_and_returns_bool():
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "file"
        blocker.write_text("x")
        old = os.environ.get("REVIEW_STATS_FILE")
        try:
            os.environ["REVIEW_STATS_FILE"] = str(blocker / "child" / "stats.jsonl")
            res = _stats.record_run(
                mode="review",
                models=["m"],
                duration_seconds=1,
                ok_count=1,
                fail_count=0,
            )
            assert res is False
            os.environ["REVIEW_STATS_FILE"] = "~nosuchuser_721/x.jsonl"
            res = _stats.record_run(
                mode="review",
                models=["m"],
                duration_seconds=1,
                ok_count=1,
                fail_count=0,
            )
            assert res is False
        finally:
            if old is None:
                os.environ.pop("REVIEW_STATS_FILE", None)
            else:
                os.environ["REVIEW_STATS_FILE"] = old


def test_record_run_appends_with_o_append_so_concurrent_lines_survive():
    """Every write opens O_APPEND: a second writer between two of ours is not clobbered."""
    flags_seen: list[int] = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        flags_seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    with _store(None), mock.patch.object(os, "open", spy):
        assert _stats.record_run(
            mode="review", models=["m"], duration_seconds=1, ok_count=1, fail_count=0
        )
    assert flags_seen and flags_seen[0] & os.O_APPEND and flags_seen[0] & os.O_CREAT


def test_stats_path_honors_override_and_default():
    with _store(None) as path:
        assert _stats.stats_path() == path
    old = os.environ.pop("REVIEW_STATS_FILE", None)
    try:
        assert (
            _stats.stats_path()
            == Path.home() / ".config" / "review-cli" / "run-stats.jsonl"
        )
    finally:
        if old is not None:
            os.environ["REVIEW_STATS_FILE"] = old


# --- readers / import / export --------------------------------------------------------------


def _reader_vectors() -> dict:
    records = [
        _rec(task="105", n=3, run_id="r3"),
        _rec(task="#105", n=1, run_id="r1"),
        _rec(task="OTHER", n=2),
        {k: v for k, v in _rec(task="T-1", n=4).items() if k != "run_id"},
    ]
    lines = _dump(records) + ["junk", "", "[1]", '{"no": "duration_seconds"}']
    out: dict = {}
    with _store(lines):
        out["load"] = _stats._load_records()
        out["public_load"] = _stats.load_run_records()
        out["iterations_105"] = _stats.iterations_for_task("105")
        out["iterations_hash"] = _stats.iterations_for_task("#105")
        out["iterations_bad"] = _stats.iterations_for_task("bad code")
        out["iterations_blank"] = _stats.iterations_for_task("  ")
        out["export"] = _stats.export_records("105")
        out["export_blank"] = _stats.export_records("")
        out["find_pinned"] = _stats.find_by_run_id("r1")
        out["find_missing"] = _stats.find_by_run_id("nope")
        derived = _stats.run_id_for(records[3]["ts"], "review", "T-1")
        out["find_derived"] = _stats.find_by_run_id(derived)
    with _store(_dump([_rec(task={"x": 1}, n=5)])):
        out["unhashable_task_export"] = _stats.export_records("105")
    with _store(None):
        out["load_missing"] = _stats._load_records()
        out["find_in_missing"] = _stats.find_by_run_id("x")
        try:
            _stats.export_records("T-1")
            out["export_missing"] = "no error"
        except OSError:
            out["export_missing"] = "OSError"
    return out


def test_readers_match_original_implementation():
    _compare_or_write("readers.json", _reader_vectors())


def test_export_of_undecodable_store_raises_oserror():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.jsonl"
        path.write_bytes(b"\xff\xfe\x00bad\n")
        old = os.environ.get("REVIEW_STATS_FILE")
        os.environ["REVIEW_STATS_FILE"] = str(path)
        try:
            for call in (
                lambda: _stats.export_records("T-1"),
                lambda: _stats.import_records(["{}"]),
            ):
                try:
                    call()
                except OSError:
                    continue
                raise AssertionError("expected OSError for an undecodable store")
        finally:
            if old is None:
                os.environ.pop("REVIEW_STATS_FILE", None)
            else:
                os.environ["REVIEW_STATS_FILE"] = old


def _import_vectors() -> dict:
    base = _rec(task="T-1", n=1)
    tagged = dict(_rec(task="T-1", n=2), remote_host="old-host")
    incoming = [
        json.dumps(base),
        json.dumps(tagged),
        json.dumps(_rec(task="T-2", n=3)),
        json.dumps(_rec(task="105", n=4)),
        "not json",
        "[1]",
        json.dumps({"x": 1}),
        "",
        json.dumps(dict(base)),
        json.dumps(dict(base, remote_host="other-host")),
    ]
    out: dict = {}
    with _store(None) as path:
        out["first"] = _stats.import_records(incoming, remote_host="box")
        out["first_lines"] = path.read_text(encoding="utf-8").splitlines()
        out["mode_bits"] = stat.S_IMODE(path.stat().st_mode)
        out["again"] = _stats.import_records(incoming, remote_host="box")
        out["lock_exists"] = Path(str(path) + ".import.lock").exists()
    with _store(None) as path:
        out["filtered"] = _stats.import_records(incoming, task_code="#105")
        out["filtered_lines"] = path.read_text(encoding="utf-8").splitlines()
    with _store(None) as path:
        path.write_text(
            json.dumps(_rec(task="T-0", n=0)), encoding="utf-8"
        )  # torn: no \n
        out["torn"] = _stats.import_records([json.dumps(base)])
        out["torn_lines"] = path.read_text(encoding="utf-8").splitlines()
    with _store(None):
        out["empty"] = _stats.import_records([])
    return out


def test_import_records_matches_original_implementation():
    _compare_or_write("import.json", _import_vectors())


def test_import_without_home_raises_oserror():
    old = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "~nosuchuser_721/x.jsonl"
    try:
        for call in (
            lambda: _stats.import_records([]),
            lambda: _stats.export_records("T-1"),
        ):
            try:
                call()
            except OSError as exc:
                assert "no run-stats store" in str(exc), exc
                continue
            raise AssertionError("expected OSError")
    finally:
        if old is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = old


# --- summaries and ETA ----------------------------------------------------------------------


def _summary_vectors() -> dict:
    records = [
        _rec(
            task="A",
            n=1,
            mode="review",
            repo_id="git@github.com:alex-mextner/dev-cli.git",
        ),
        _rec(
            task="A", n=2, mode="quorum", prompt_tokens=5, output_tokens=6, fail_count=2
        ),
        _rec(task="B", n=3, mode="review", repo_id=REPO_B, ts="2026-09-02T00:00:00Z"),
        _rec(task="B", n=4, mode="brainstorm", ts="2026-08-01T00:00:00"),
        _rec(task=None, n=5, mode="qa", duration_seconds="x", ok_count="y"),
        _rec(task="C", n=6, mode="", models=["m-z", "m-z", 3], pool_size="x"),
        _rec(task="C", n=7, mode="review", repo_id="", fail_count=1, ok_count=0),
        _rec(task="C", n=8, mode=5, repo_id=7),
    ]
    since = datetime(2026, 9, 1, 0, 3, tzinfo=timezone.utc)
    out: dict = {}
    with _store(_dump(records)):
        out["tasks"] = _stats.task_summaries()
        out["modes"] = _stats.mode_summary()
        out["modes_since"] = _stats.mode_summary(since=since)
        out["repos"] = _stats.repo_summary()
        out["repos_top1"] = _stats.repo_summary(top=1)
        out["repos_top0"] = _stats.repo_summary(top=0, since=since)
        loaded = _stats.load_run_records()
        out["modes_records"] = _stats.mode_summary(records=loaded[:3])
        out["repos_records"] = _stats.repo_summary(records=loaded[:3])
        out["ts_ge"] = [
            _stats._record_ts_ge(r, since)
            for r in (
                {"ts": NEW_TS},
                {"ts": OLD_TS},
                {"ts": "2026-09-02T00:00:00Z"},
                {"ts": "2026-09-02T00:00:00"},
                {"ts": 5},
                {},
                {"ts": "bad"},
            )
        ] + [_stats._record_ts_ge({}, None)]
    with _store(None):
        out["tasks_missing"] = _stats.task_summaries()
        out["modes_missing"] = _stats.mode_summary()
        out["repos_missing"] = _stats.repo_summary()
    return out


def test_summaries_match_original_implementation():
    _compare_or_write("summaries.json", _summary_vectors())


def _eta_vectors() -> dict:
    records = [
        _rec(n=1, mode="review", pool_size=4, duration_seconds=100.0),
        _rec(n=2, mode="review", pool_size=4, duration_seconds=200.0),
        _rec(n=3, mode="quorum", pool_size=4, duration_seconds=30.0),
        _rec(n=4, mode="qa", pool_size=2, duration_seconds=-5),
        _rec(n=5, mode="qa", pool_size=2, duration_seconds="x"),
    ]
    out: dict = {}
    with _store(_dump(records)):
        out["exact"] = _stats.estimate_eta("review", 4)
        out["pool_only"] = _stats.estimate_eta("brainstorm", 4)
        out["none"] = _stats.estimate_eta("review", 9)
        out["bad_durations"] = _stats.estimate_eta("qa", 2)
        out["line_exact"] = _stats.eta_line("review", 4)
        out["line_pool"] = _stats.eta_line("brainstorm", 4)
        out["line_none"] = _stats.eta_line("review", 9)
        out["line_single"] = _stats.eta_line("quorum", 4)
        buf = io.StringIO()
        _stats.announce_eta("review", 4, stream=buf)
        out["announce"] = buf.getvalue()
    with _store(None):
        out["empty_store"] = _stats.estimate_eta("review", 4)
        out["empty_line"] = _stats.eta_line("review", 4)
    return out


def test_eta_matches_original_implementation():
    _compare_or_write("eta.json", _eta_vectors())


def test_eta_line_survives_a_failing_estimate_and_announce_never_raises():
    with mock.patch.object(_stats, "estimate_eta", side_effect=RuntimeError("boom")):
        line = _stats.eta_line("review", 3)
        assert "no history yet" in line, line

    class Broken:
        def write(self, _text):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    _stats.announce_eta("review", 3, stream=Broken())


def test_public_surface_is_unchanged():
    names = [
        "STATS_VERSION",
        "announce_eta",
        "diff_content_hash",
        "estimate_eta",
        "eta_line",
        "export_records",
        "extract_diff_files",
        "find_by_run_id",
        "fmt_duration",
        "import_records",
        "iterations_for_task",
        "load_run_records",
        "mode_summary",
        "normalize_repo_remote",
        "normalize_task_code",
        "parse_iso_ts",
        "quorum_check",
        "record_run",
        "repo_summary",
        "run_id_for",
        "stats_path",
        "task_summaries",
        "_load_records",
        "_DEFAULT_QUORUM_FLOOR",
        "_MISMATCH_DETAILS_CAP",
        "_REPO_ID_ALIASES",
        "_ROLE_TRACKING_CUTOFF",
        "_finalize_quorum_result",
        "_classify_iteration_identity",
        "_record_ts_ge",
        "_all_iterations_predate_role_tracking",
    ]
    missing = [n for n in names if not hasattr(_stats, n)]
    assert not missing, missing
    assert _stats.STATS_VERSION == 5


def test_patched_load_records_seam_is_honored_by_readers():
    """Rebinding `stats._load_records` (the seam existing tests use) reaches every reader."""
    sentinel = [_rec(task="SEAM", n=1, run_id="seam-1", repo_id=REPO_A)]
    with _store(None), mock.patch.object(_stats, "_load_records", lambda: sentinel):
        assert _stats.load_run_records() == sentinel
        assert _stats.iterations_for_task("SEAM")[0]["run_id"] == "seam-1"
        assert _stats.find_by_run_id("seam-1") == sentinel[0]
        assert _stats.task_summaries()[0]["task_code"] == "SEAM"
        assert _stats.mode_summary()[0]["mode"] == "review"
        assert _stats.repo_summary()[0]["repo_id"] == REPO_A
        assert _stats.estimate_eta("review", 3)["samples"] == 1


if __name__ == "__main__":
    failures = 0
    for fn_name, fn in list(globals().items()):
        if fn_name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {fn_name}")
            except (AssertionError, KeyError, TypeError, ValueError, OSError) as exc:
                failures += 1
                print(f"FAIL {fn_name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
