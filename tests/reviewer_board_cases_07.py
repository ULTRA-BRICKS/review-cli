"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    Path,
    REPO_ROOT,
    REVIEW_ROLES,
    ReviewResult,
    backends,
    os,
    sys,
)

def test_quorum_and_brainstorm_padded_roles_put_review_lenses_in_prompts():
    """review-cli#433 P1: a one-model quorum/brainstorm pad records
    architect/correctness/security only if each seat was actually prompted
    with that REVIEW_ROLES lens. PERSONAS rotation is a different set of
    lenses — recording the board roles without injecting them is a ship-gate
    lie."""
    import contextlib
    import io
    import tempfile

    from reviewlib.modes import brainstorm as bs_mod
    from reviewlib.modes import quorum as q_mod

    roles = ["architect", "correctness", "security"]
    models = ["m1", "m1", "m1"]

    def _usable(job):
        return ReviewResult(
            model=job.label or job.model,
            command="fake",
            returncode=0,
            stdout="ok",
            stderr="",
        )

    captured: dict = {}

    def _capture_slots(jobs, cwd, timeout, roles=None):
        captured["jobs"] = list(jobs)
        captured["roles"] = list(roles or [])
        return [_usable(j) for j in jobs]

    def _fake_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="DECISION: STOP\n1. QUORUM\nNone.\n2. DISAGREEMENT / NO QUORUM\nNone.\n3. ABSTAINED\nNone.",
            stderr="",
        )

    saved_q_slots, saved_q_mod = q_mod.run_role_slots, q_mod.run_moderator
    q_mod.run_role_slots = _capture_slots
    q_mod.run_moderator = _fake_moderator
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = q_mod.mode_quorum(
                "should we ship?",
                models,
                "",
                REPO_ROOT,
                5,
                ["mod"],
                roles=roles,
            )
    finally:
        q_mod.run_role_slots = saved_q_slots
        q_mod.run_moderator = saved_q_mod
    assert rc == 0, rc
    q_jobs = captured["jobs"]
    assert len(q_jobs) == 3, [j.label for j in q_jobs]
    for role, job in zip(roles, q_jobs, strict=True):
        assert REVIEW_ROLES[role] in job.prompt, (
            f"quorum seat recorded as {role!r} was never asked to apply that lens",
            job.prompt,
        )

    captured.clear()
    saved_b_slots, saved_b_mod = bs_mod.run_role_slots, bs_mod.run_moderator
    bs_mod.run_role_slots = _capture_slots
    bs_mod.run_moderator = _fake_moderator
    saved_log = os.environ.get("REVIEW_LOG_DIR")
    saved_stats = os.environ.get("REVIEW_STATS_FILE")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["REVIEW_LOG_DIR"] = tmp
            os.environ["REVIEW_STATS_FILE"] = str(Path(tmp) / "run-stats.jsonl")
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc = bs_mod.mode_brainstorm(
                    "topic",
                    models,
                    REPO_ROOT,
                    5,
                    ["mod"],
                    3,
                    3,
                    roles=roles,
                )
    finally:
        bs_mod.run_role_slots = saved_b_slots
        bs_mod.run_moderator = saved_b_mod
        if saved_log is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = saved_log
        if saved_stats is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved_stats
    assert rc == 0, rc
    b_jobs = captured["jobs"]
    assert len(b_jobs) == 3, [j.label for j in b_jobs]
    for role, job in zip(roles, b_jobs, strict=True):
        assert REVIEW_ROLES[role] in job.prompt, (
            f"brainstorm slot recorded as {role!r} was never asked to apply that lens",
            job.prompt,
        )


def test_brainstorm_records_only_usable_completed_roles():
    """review-cli#433 P1: a 2/3 brainstorm round is still alive, and a successful
    synthesis makes the iteration pass. Persist the completed roles (Roles: 2/3
    complete), not the planned list — a failed slot after retry must not satisfy
    the ship gate."""
    import contextlib
    import io
    import json
    import tempfile

    from reviewlib import cli
    from reviewlib import panel as p
    from reviewlib.modes import brainstorm as bs_mod

    failed_role = "security"

    def _fake_slots(jobs, cwd, timeout, roles=None):
        role_list = list(roles or [])
        results = []
        for i, job in enumerate(jobs):
            role = role_list[i] if i < len(role_list) else ""
            ok = role != failed_role
            result = ReviewResult(
                model=job.label or job.model,
                command="fake",
                returncode=0 if ok else 124,
                stdout="ideas" if ok else "",
                stderr="" if ok else "timeout",
            )
            results.append(result)
            p._tally_result(result.returncode)
        print(p.report_roles_complete(role_list, results), file=sys.stderr, flush=True)
        return results

    def _fake_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        p._tally_result(0)
        body = (
            "DECISION: STOP"
            if "The brainstorm is complete." in prompt
            else "summary\nDECISION: STOP"
        )
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout=body,
            stderr="",
        )

    saved_slots, saved_mod = bs_mod.run_role_slots, bs_mod.run_moderator
    saved_load = cli.load_config
    saved_avail = backends.backend_available
    saved_stdin = sys.stdin
    saved_diff = cli._git_diff
    cli._git_diff = lambda *args, **kwargs: ""
    old_task = os.environ.get("REVIEW_TASK_CODE")
    old_env = os.environ.get("GEMINI_ENV_FILE")
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    old_log = os.environ.get("REVIEW_LOG_DIR")
    old_stats = os.environ.get("REVIEW_STATS_FILE")
    bs_mod.run_role_slots = _fake_slots
    bs_mod.run_moderator = _fake_moderator
    cli.load_config = lambda: {}
    backends.backend_available = lambda _m: True
    sys.stdin = io.StringIO("")
    os.environ.pop("REVIEW_TASK_CODE", None)
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ.pop("REVIEW_FAKE_BACKEND", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "run-stats.jsonl"
            os.environ["REVIEW_LOG_DIR"] = tmp
            os.environ["REVIEW_STATS_FILE"] = str(stats_path)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc = cli.main(
                    [
                        "brainstorm",
                        "topic",
                        "--task",
                        "433",
                        "-m",
                        "grok",
                        "--rounds",
                        "3",
                        "--max-rounds",
                        "3",
                        "-C",
                        str(REPO_ROOT),
                    ]
                )
            assert rc == 0, rc
            assert stats_path.exists(), "expected a recorded brainstorm iteration"
            recs = [
                json.loads(ln)
                for ln in stats_path.read_text().splitlines()
                if ln.strip()
            ]
            assert recs, recs
            rec = recs[-1]
            assert rec.get("passed") is True, rec
            recorded = rec.get("roles") or []
            assert failed_role not in recorded, (
                "failed slot was persisted as covered",
                recorded,
                rec,
            )
            assert recorded, rec
    finally:
        bs_mod.run_role_slots = saved_slots
        bs_mod.run_moderator = saved_mod
        cli.load_config = saved_load
        backends.backend_available = saved_avail
        sys.stdin = saved_stdin
        cli._git_diff = saved_diff
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake
        if old_log is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = old_log
        if old_stats is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = old_stats
