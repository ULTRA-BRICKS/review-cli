"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    GROK_SEAT,
    LIGHT_PRESET_BOARD,
    MIMO_SEAT,
    Path,
    REPO_ROOT,
    REVIEW_ROLES,
    ReviewResult,
    _capture_panel_task_dispatch,
    _capture_recorded_review_dispatch,
    _capture_review_with_config,
    backends,
    os,
    sys,
)

def test_show_board_role_less_config_roster_stays_unpadded():
    """review-cli#309 review finding: `--show-board --task CODE` must NOT
    display a padded/repaired board for a role-less config `models:` roster --
    the real dispatch gate only pads a config/default board when it already
    carries at least one real `role_lens` (`ensure_min_distinct_roles` would
    otherwise invent roles the user never configured, see
    test_unknown_role_board_with_one_live_seat_still_bails_end_to_end). A
    role-less 2-seat roster showing 4 padded seats would mislead an agent
    planning off `--show-board`'s output about what `review diff --task CODE`
    actually dispatches."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["totally-unknown-model-1", "totally-unknown-model-2"]
    }
    backends.backend_available = lambda _m: True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--task", "309", "--show-board"])
    finally:
        cli.load_config = old_load_config
        backends.backend_available = old_avail
    assert rc == 0, buf.getvalue()
    out = buf.getvalue()
    assert "(2 seats" in out, out
    assert "2 filled" in out, out
    pool_lines = [ln for ln in out.splitlines() if "[pool" in ln]
    assert len(pool_lines) == 2, out


def test_cli_recorded_task_multi_model_stays_exact_despite_config_roster_present():
    """review-cli#309 round-2 review finding: a multi-model explicit `-m` list
    must stay exact -- never gain extra cloned seats -- even when `--task` is
    given and a config `models:`/`board:` roster is ALSO present (Fable review
    finding, round 2: the prior name/docstring here said "short config
    roster", but the config below has 4 entries -- not short by
    `short_config_roster`'s own `config_roster_len <= SHIP_MIN_ROLES`
    definition, and irrelevant anyway since `short_config_roster` never
    applies once `explicit_models` is non-empty). Only the SINGLE-model `-m`
    case (`task_role_pad`) is allowed to pad; the multi-model contract
    ("explicit -m is honored as-is, never adds extra seats") must hold
    regardless of `--task` or any config roster."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-m", "codex", "-m", "gemini", "-C", str(REPO_ROOT)],
        {"models": ["codex", "gemini", "opus", "astra"]},
    )
    board = captured["board"]
    assert board, captured
    assert len(board) == 2, [(r.model, r.role) for r in board]
    assert {r.model for r in board} == {"codex", "gemini"}, captured
    assert captured["pool_size"] == 2, captured


def test_cli_recorded_task_explicit_pool_one_stays_one_and_warns():
    """Explicit `--pool 1` WITH `--task` still wins, but must warn that this
    run cannot satisfy default `review task --check` / gh ship."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_recorded_review_dispatch(
            [
                "diff",
                "--task",
                "309",
                "--pool",
                "1",
                "-C",
                str(REPO_ROOT),
            ]
        )
    assert captured["rc"] == 0, captured
    assert captured["pool_size"] == 1, captured
    warning = err.getvalue().lower()
    assert "pool 1" in warning or "--pool" in warning, err.getvalue()
    assert "gh ship" in warning or "min_roles" in warning or "ship" in warning, (
        err.getvalue()
    )


def test_cli_recorded_task_pool_zero_single_model_warns():
    """review-cli#309 review finding: `--pool 0` ("all seats") with a single
    `-m` model + `--task` collapses to that ONE model/seat -- genuinely below
    the ship floor, same as an explicit `--pool 1` -- and must warn, not
    silently dispatch an unwarned 1-seat run that can never satisfy `review
    task CODE --check` / gh ship."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_recorded_review_dispatch(
            ["diff", "--task", "309", "--pool", "0", "-m", "grok", "-C", str(REPO_ROOT)]
        )
    assert captured["rc"] == 0, captured
    assert captured["models"] == [GROK_SEAT], captured
    warning = err.getvalue().lower()
    assert "--pool 0" in warning, err.getvalue()
    assert "gh ship" in warning or "min_roles" in warning or "ship" in warning, (
        err.getvalue()
    )


def test_cli_recorded_task_pool_zero_two_models_warns():
    """review-cli#309 round-2 review finding: the `--pool 0` single-model
    warning above missed a MULTI (but still sub-floor) explicit `-m` list --
    `--pool 0 -m a -m b --task` also collapses to just those 2 models/seats,
    below the ship floor, and must warn the same way."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_recorded_review_dispatch(
            [
                "diff",
                "--task",
                "309",
                "--pool",
                "0",
                "-m",
                "codex",
                "-m",
                "gemini",
                "-C",
                str(REPO_ROOT),
            ]
        )
    assert captured["rc"] == 0, captured
    assert captured["models"] == ["codex", "gemini"], captured
    warning = err.getvalue().lower()
    assert "--pool 0" in warning, err.getvalue()
    assert "gh ship" in warning or "min_roles" in warning or "ship" in warning, (
        err.getvalue()
    )


def test_cli_recorded_task_pool_zero_at_floor_models_does_not_warn():
    """`--pool 0` with exactly SHIP_MIN_ROLES (3) explicit `-m` models already
    meets the floor on its own and must NOT warn."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_recorded_review_dispatch(
            [
                "diff",
                "--task",
                "309",
                "--pool",
                "0",
                "-m",
                "codex",
                "-m",
                "gemini",
                "-m",
                "grok",
                "-C",
                str(REPO_ROOT),
            ]
        )
    assert captured["rc"] == 0, captured
    assert "warning" not in err.getvalue().lower(), err.getvalue()


def test_cli_recorded_task_pool_zero_short_config_roster_warns():
    """review-cli#309 round-3 review finding (pre-commit review): the `--pool 0`
    warning above only covered an explicit `-m` list -- it missed the MIRROR
    case, a short config `models:`/`board:` roster with NO `-m` at all.
    `--pool 0` on a 2-model config roster collapses to just those 2 seats,
    below the ship floor, and must warn the same way."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_review_with_config(
            ["diff", "--task", "309", "--pool", "0", "-C", str(REPO_ROOT)],
            {"models": ["codex", "gemini"]},
        )
    assert captured.get("board"), captured
    warning = err.getvalue().lower()
    assert "--pool 0" in warning, err.getvalue()
    assert "gh ship" in warning or "min_roles" in warning or "ship" in warning, (
        err.getvalue()
    )


def test_cli_recorded_task_pool_zero_at_floor_config_roster_does_not_warn():
    """`--pool 0` with exactly SHIP_MIN_ROLES (3) config `models:` entries (no
    `-m`) already meets the floor on its own and must NOT warn (regression
    guard for the short-config-roster `--pool 0` warning above)."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_review_with_config(
            ["diff", "--task", "309", "--pool", "0", "-C", str(REPO_ROOT)],
            {"models": ["codex", "gemini", "grok"]},
        )
    assert captured.get("board"), captured
    assert "warning" not in err.getvalue().lower(), err.getvalue()


def test_cli_recorded_task_pool_zero_same_role_roster_pads_roles():
    """review-cli#433: `--pool 0` with a 4-seat config roster whose every
    seat shares one role must still pad so the recorded dispatch covers
    SHIP_MIN_ROLES distinct roles. Seat COUNT is already above the floor,
    so the short-roster pad and the `--pool 0` seat-count warning both skip;
    without role-repair the board would dispatch 4 seats / 1 role and fail
    `review task CODE --check` / gh ship with no diagnostic.

    Fails if `ensure_min_distinct_roles` is skipped for `effective_pool_size
    == 0` (or called with min_roles<=0, which is a no-op)."""
    import contextlib
    import io

    from reviewlib.config import SHIP_MIN_ROLES

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_review_with_config(
            ["diff", "--task", "433", "--pool", "0", "-C", str(REPO_ROOT)],
            {
                "board": [
                    {"model": "codex", "role": "correctness", "name": "A"},
                    {"model": "gemini", "role": "correctness", "name": "B"},
                    {"model": "oc:xai/grok-4.7", "role": "correctness", "name": "C"},
                    {
                        "model": "claude:claude-opus-5-5",
                        "role": "correctness",
                        "name": "D",
                    },
                ],
            },
        )
    board = captured.get("board")
    assert board, captured
    distinct = {r.role for r in board if r.role}
    assert len(distinct) >= SHIP_MIN_ROLES, [(r.model, r.role) for r in board]
    warning = err.getvalue().lower()
    assert "--pool 0" in warning, err.getvalue()
    assert "role" in warning, err.getvalue()
    assert "gh ship" in warning or "min_roles" in warning or "ship" in warning, (
        err.getvalue()
    )


def test_cli_recorded_task_explicit_config_pool_wins_over_short_roster_pad():
    """review-cli#309 round-2 review finding: an explicit config `pool:` default
    must win over the short/exact-config-roster pad the same way an explicit
    CLI `--pool` does -- it already went through `recorded_task_pool_size`'s
    floor, so the roster-size heuristic (meant only for the no-real-preference
    DEFAULT_POOL_SIZE fallback) must not clamp it back down to SHIP_MIN_ROLES."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-C", str(REPO_ROOT)],
        {"models": ["codex", "gemini", "claude:claude-opus-5-5"], "pool": 5},
    )
    assert captured["pool_size"] == 5, captured


def test_cli_recorded_task_explicit_config_pool_wins_over_single_model_pad():
    """review-cli#309 round-2 review finding: an explicit config `pool:` default
    must win over the single-`-m`-model ship-floor pad too -- the model is
    cloned across the CONFIGURED pool size, not clamped to SHIP_MIN_ROLES."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-m", "grok", "-C", str(REPO_ROOT)],
        {"pool": 5},
    )
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} == {GROK_SEAT}, captured
    roles = [r.role for r in board if r.role]
    assert len(set(roles)) == 5, (roles, captured)
    assert captured["pool_size"] == 5, captured


def test_cli_recorded_task_sub_floor_config_pool_still_floors_single_model():
    """review-cli#309 round-2 review finding (Sonnet, pre-commit review): a
    SUB-floor explicit config `pool:` (below SHIP_MIN_ROLES) combined with a
    single `-m` model + `--task` must still floor to SHIP_MIN_ROLES -- it
    goes through `recorded_task_pool_size`'s `max(resolved, SHIP_MIN_ROLES)`
    before either pad guard runs, so skipping the pad guards (since an
    explicit config `pool:` is set) does not leave it silently under-floored."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-m", "grok", "-C", str(REPO_ROOT)],
        {"pool": 1},
    )
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} == {GROK_SEAT}, captured
    roles = [r.role for r in board if r.role]
    assert len(set(roles)) == 3, (roles, captured)
    assert captured["pool_size"] == 3, captured


def test_cli_recorded_task_sub_floor_config_pool_still_floors_short_roster():
    """review-cli#309 round-2 review finding (Sonnet, pre-commit review): a
    SUB-floor explicit config `pool:` combined with a short (2-entry) config
    `models:` roster + `--task` must still floor to SHIP_MIN_ROLES, same
    reasoning as the single-model case above."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-C", str(REPO_ROOT)],
        {"pool": 1, "models": ["codex", "gemini"]},
    )
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} <= {"codex", "gemini"}
    assert len({r.role for r in board if r.role}) == 3, [
        (r.model, r.role) for r in board
    ]
    assert captured["pool_size"] == 3, captured


def test_cli_recorded_task_pool_zero_default_board_does_not_warn():
    """`--pool 0` ("all seats") on a real multi-seat default/config board has
    plenty of distinct roles and must NOT emit the single-model-only warning
    (regression guard for the `--pool 0` single-model warning above)."""
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        captured = _capture_recorded_review_dispatch(
            ["diff", "--task", "309", "--pool", "0", "-C", str(REPO_ROOT)]
        )
    assert captured["rc"] == 0, captured
    assert "warning" not in err.getvalue().lower(), err.getvalue()


def test_cli_panel_task_one_model_pads_to_ship_min_roles():
    """review-cli#433: quorum / brainstorm `-m one-model --task` must dispatch
    SHIP_MIN_ROLES distinct roles on that one engine — never shrink the role list
    to engine count. Copies of one response do not count as coverage; each role is
    its own slot. (just-ask never pads — review-cli#587, see
    tests/test_just_ask_timeout.py.)

    Fails if `task_role_pad` stays gated on `mode.name == "review"` (today
    those modes stay a single seat)."""
    from reviewlib.config import SHIP_MIN_ROLES

    for sub, positional in (
        ("quorum", "q"),
        ("brainstorm", "topic"),
    ):
        captured = _capture_panel_task_dispatch(
            [sub, positional, "--task", "433", "-m", "grok", "-C", str(REPO_ROOT)]
        )
        assert captured.get("rc") == 0, (sub, captured)
        models = captured.get("models") or []
        roles = captured.get("roles") or []
        assert set(models) == {GROK_SEAT}, (sub, models, captured)
        assert len(models) >= SHIP_MIN_ROLES, (
            f"{sub} shrank to {len(models)} engine seat(s); "
            f"need {SHIP_MIN_ROLES} role slots",
            models,
        )
        distinct = {r for r in roles if r}
        assert len(distinct) >= SHIP_MIN_ROLES, (
            f"{sub} dispatched {len(distinct)} distinct roles, need {SHIP_MIN_ROLES}",
            roles,
            models,
        )


def test_cli_default_brainstorm_task_pads_live_level_engines_to_ship_floor():
    """Default panels preserve three role lenses, bounded by their chosen level."""
    from reviewlib.config import SHIP_MIN_ROLES, preset_pool_size

    defaults = [seat.model for seat in LIGHT_PRESET_BOARD]
    argv = ["brainstorm", "topic", "--task", "444", "-C", str(REPO_ROOT)]
    for live in (defaults[:1], defaults[:2], defaults):
        captured = _capture_panel_task_dispatch(argv, available=lambda model, live=live: model in live)
        assert captured.get("rc") == 0, captured
        models, roles = captured["models"], captured["roles"]
        assert set(models) <= set(live), (live, models)
        assert len(models) == len(roles) == max(SHIP_MIN_ROLES, preset_pool_size("routine")), captured
        assert len(set(roles)) >= SHIP_MIN_ROLES, captured
    for pool in (1, 2, 4, 6):
        captured = _capture_panel_task_dispatch([*argv, "--pool", str(pool)], available=lambda model: model == defaults[0])
        assert captured.get("rc") == 0, captured
        assert len(captured["models"]) == min(pool, len(defaults)), captured
        assert set(captured["models"]) == {defaults[0]}, captured
        assert len(captured["roles"]) == min(pool, len(defaults)), captured
    all_seats = _capture_panel_task_dispatch([*argv, "--pool", "0"], available=True)
    assert all_seats.get("rc") == 0, all_seats
    assert set(all_seats["models"]) == set(defaults), all_seats
    all_dead = _capture_panel_task_dispatch(argv, available=False)
    assert all_dead.get("rc") == 1 and "models" not in all_dead, all_dead


def test_default_brainstorm_task_one_live_engine_meets_ship_floor_check():
    """review-cli#444 end to end: a default `brainstorm --task` with ONE live
    DEFAULT_MODELS engine prompts every slot with its REVIEW_ROLES lens, records
    SHIP_MIN_ROLES completed roles, and `quorum_check(min_roles=SHIP_MIN_ROLES)`
    -- the gate `review task CODE --check` / gh ship applies -- passes from that
    single run. Only the model boundary (slot dispatch + moderator) is faked."""
    import contextlib
    import io
    import json
    import tempfile

    from reviewlib import cli
    from reviewlib import panel as p
    from reviewlib import stats as _stats
    from reviewlib.config import SHIP_MIN_ROLES
    from reviewlib.modes import brainstorm as bs_mod

    live = LIGHT_PRESET_BOARD[0].model
    prompted: list[tuple[str, str]] = []

    def _fake_slots(jobs, cwd, timeout, roles=None):
        role_list = list(roles or [])
        results = []
        for i, job in enumerate(jobs):
            role = role_list[i] if i < len(role_list) else ""
            assert role and REVIEW_ROLES[role] in job.prompt, (role, job.prompt)
            prompted.append((job.model, role))
            result = ReviewResult(
                model=job.label or job.model,
                command="fake",
                returncode=0,
                stdout="ideas",
                stderr="",
            )
            results.append(result)
            p._tally_result(0)
        return results

    def _fake_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        p._tally_result(0)
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary\nDECISION: STOP",
            stderr="",
        )

    env_keys = (
        "REVIEW_TASK_CODE",
        "GEMINI_ENV_FILE",
        "REVIEW_FAKE_BACKEND",
        "REVIEW_LOG_DIR",
        "REVIEW_STATS_FILE",
        "REVIEW_USAGE_LIMITS_DIR",
        "REVIEW_HARNESS_LIMITS_JSON",
    )
    saved_env = {k: os.environ.get(k) for k in env_keys}
    saved_slots, saved_mod = bs_mod.run_role_slots, bs_mod.run_moderator
    saved_load = cli.load_config
    saved_avail = backends.backend_available
    saved_chain = cli._chain_aware_available
    saved_stdin = sys.stdin
    saved_diff = cli._git_diff
    cli._git_diff = lambda *args, **kwargs: ""
    bs_mod.run_role_slots = _fake_slots
    bs_mod.run_moderator = _fake_moderator
    cli.load_config = lambda: {}
    backends.backend_available = lambda m: m == live
    cli._chain_aware_available = lambda m: m == live
    sys.stdin = io.StringIO("")
    os.environ.pop("REVIEW_TASK_CODE", None)
    os.environ.pop("REVIEW_FAKE_BACKEND", None)
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ["REVIEW_USAGE_LIMITS_DIR"] = "/nonexistent/review-cli/.usage"
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = "{}"
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
                        "444",
                        "--rounds",
                        "3",
                        "--max-rounds",
                        "3",
                        "-C",
                        str(REPO_ROOT),
                    ]
                )
            assert rc == 0, rc
            recs = [
                json.loads(ln)
                for ln in stats_path.read_text().splitlines()
                if ln.strip()
            ]
            assert recs, "expected a recorded brainstorm iteration"
            rec = recs[-1]
            assert rec.get("passed") is True, rec
            assert len(set(rec.get("roles") or [])) >= SHIP_MIN_ROLES, rec
            result = _stats.quorum_check(
                rec["task_code"], min_iter=1, min_roles=SHIP_MIN_ROLES
            )
    finally:
        bs_mod.run_role_slots = saved_slots
        bs_mod.run_moderator = saved_mod
        cli.load_config = saved_load
        backends.backend_available = saved_avail
        cli._chain_aware_available = saved_chain
        sys.stdin = saved_stdin
        cli._git_diff = saved_diff
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert {m for m, _ in prompted} == {live}, prompted
    assert result["passed"] is True, result
    assert result["distinct_roles_passed"] >= SHIP_MIN_ROLES, result


def test_cli_panel_task_pad_recovers_from_typo_role_in_config_board():
    """review-cli#422 CLI-level regression: `board_from_models(explicit_models, config)`
    inside the --task panel role-pad path (just-ask/quorum/brainstorm, one explicit -m)
    used to call straight into the now-validating `_parse_board_entry` with NO
    try/except -- a config `board:` entry naming the SAME model under a typo'd role
    crashed the whole CLI dispatch with an unhandled BoardConfigError traceback instead
    of the friendly [review-cli] error every sibling board_from_models call site prints.
    The fix mirrors the sibling explicit -m fallback: warn to stderr naming the bad
    role, ignore the malformed board metadata, and still pad the ship floor."""
    cfg = {"board": [{"model": "grok", "role": "not-a-real-role"}]}
    captured = _capture_panel_task_dispatch(
        ["quorum", "q", "--task", "422", "-m", "grok", "-C", str(REPO_ROOT)],
        config=cfg,
    )
    assert captured.get("rc") == 0, captured  # no crash, dispatch still completes
    models = captured.get("models") or []
    roles = captured.get("roles") or []
    assert set(models) == {GROK_SEAT}, captured
    assert len(roles) == len(models) and all(roles), captured
    stderr = captured.get("stderr") or ""
    assert "not-a-real-role" in stderr, stderr
    assert "ignoring malformed board metadata" in stderr, stderr


def test_lenient_board_retry_keeps_matching_entry_metadata_despite_unrelated_bad_role():
    """review-cli#422 follow-up (Fable review finding): `board_from_models` parses
    EVERY `board:` entry to build its model->metadata lookup, not just entries for the
    caller's requested models -- so a single unrelated entry with a typo'd role used to
    make `_board_from_models_lenient`'s retry wipe metadata for the WHOLE config board,
    including a different entry that both matches the requested `-m` model and is
    completely valid. The retry must drop only the entry that actually fails to parse,
    not every entry in the list."""
    import contextlib
    import io

    from reviewlib.cli import _board_from_models_lenient

    cfg = {
        "board": [
            {"model": "grok", "role": "correctness", "name": "Grok Custom"},
            {"model": "gemini", "role": "not-a-real-role"},  # unrelated typo
        ]
    }
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        board = _board_from_models_lenient([GROK_SEAT], cfg)
    assert len(board) == 1, board
    # the matching, valid entry's role/name metadata survived the retry --
    # it was NOT wiped just because an unrelated entry elsewhere was malformed.
    assert board[0].role == "correctness", board[0]
    assert board[0].display == "Grok Custom", board[0]
    assert "not-a-real-role" in stderr.getvalue(), stderr.getvalue()


def test_drop_malformed_board_entries_never_leaves_the_retry_unsafe():
    """`_drop_malformed_board_entries`'s own branches, unit-tested directly against
    the invariant `_board_from_models_lenient`'s retry depends on: `board_from_models`
    never raises `BoardConfigError` for anything OTHER than an individual entry that
    fails `_parse_board_entry` -- a non-dict `config`, and a `board:` that isn't a list
    at all, are both already silently tolerated by `board_from_models` itself (it only
    iterates `board:` when `isinstance(raw_board, list)`), so the sanitizer passing
    them through unchanged can never hand `board_from_models` something that raises."""
    from reviewlib.cli import _board_from_models_lenient
    from reviewlib.config import _drop_malformed_board_entries, board_from_models

    assert _drop_malformed_board_entries(None) == {}
    assert _drop_malformed_board_entries("not-a-dict") == {}
    # `board:` present but not a list -- passed through unchanged, and confirmed
    # (not just asserted) that `board_from_models` tolerates it without raising.
    non_list_cfg = {"board": "oops-not-a-list", "models": ["grok"]}
    assert _drop_malformed_board_entries(non_list_cfg) == non_list_cfg
    board_from_models(["grok"], non_list_cfg)  # must not raise
    # every entry malformed -> sanitized down to an empty list, still tolerated.
    all_bad_cfg = {
        "board": [
            {"model": "grok", "role": "typo-a"},
            {"model": "gemini", "role": "typo-b"},
        ]
    }
    sanitized = _drop_malformed_board_entries(all_bad_cfg)
    assert sanitized["board"] == [], sanitized
    board_from_models(["grok"], sanitized)  # must not raise
    # end to end: the lenient retry survives a config whose `board:` is malformed at
    # the CONTAINER level, not just at the per-entry level.
    board = _board_from_models_lenient(["grok"], {"board": "oops"})
    assert len(board) == 1, board


def test_quorum_no_dash_m_auto_assigns_distinct_roles():
    """review-cli quorum default-role-assignment fix: `review quorum "<question>"
    --task CODE` with NO `-m` and NO config `models:`/`board:` override used to
    dispatch a real model panel but leave `ctx.extra["task_roles"]` empty --
    the only path that ever produced REVIEW_ROLES labels for quorum was
    `task_role_pad`, gated on exactly one explicit `-m` model. Every OTHER
    quorum invocation (this one included) fell back through the flat
    `config_models or DEFAULT_MODELS` branch, which carries no role metadata,
    so `_seat_assignments()` (reviewlib/modes/quorum.py) silently cycled
    generic literary PERSONAS instead of real review roles.

    The fix selects from the SAME role-tagged `DEFAULT_BOARD` +
    `select_pool_and_reserve_with_reuse` that `review diff`'s own board
    dispatch already uses, scoped to quorum's unconfigured default only, and
    threads the picked roles into `ctx.extra["task_roles"]` via
    `panel_task_roles`. Proves: more than one seat, one role per seat, every
    role non-empty, roles are DISTINCT (not one role repeated), models are
    DISTINCT (not one model cloned across every seat), and the operator-facing
    `_announce_quorum_auto_roles` stderr notice fires with the real mapping."""
    captured = _capture_panel_task_dispatch(
        ["quorum", "test question", "--task", "SOMECODE", "-C", str(REPO_ROOT)]
    )
    assert captured.get("rc") == 0, captured
    models = captured.get("models") or []
    roles = captured.get("roles") or []
    assert len(models) > 1, (models, captured)
    assert len(roles) == len(models), (roles, models)
    assert all(roles), (roles, models)
    assert len(set(roles)) > 1, (roles, models)
    assert len(set(models)) > 1, (roles, models)
    assert models == [seat.model for seat in LIGHT_PRESET_BOARD[:3]], captured
    assert roles == [seat.role for seat in LIGHT_PRESET_BOARD[:3]], captured


def test_quorum_no_dash_m_with_config_models_skips_auto_board():
    """The quorum-default-board auto-role path is scoped strictly to quorum's
    OWN unconfigured default (`if not config_models and not config_has_board`
    in `_dispatch`) -- an operator-configured `models:` roster is a deliberate
    choice and must keep dispatching through the pre-existing flat
    `config_models` path with NO role metadata, exactly like before this fix.
    Regression coverage for exactly the scoping condition the new branch's
    own comment calls out; nothing else pinned that a future edit dropping
    the guard would go unnoticed."""
    captured = _capture_panel_task_dispatch(
        ["quorum", "test question", "--task", "SOMECODE", "-C", str(REPO_ROOT)],
        config={"models": ["grok", "gemini", "mimo"]},
    )
    assert captured.get("rc") == 0, captured
    models = captured.get("models") or []
    roles = captured.get("roles") or []
    assert models, captured
    assert set(models) <= {GROK_SEAT, "gemini", MIMO_SEAT}, models
    assert roles == [], (
        "config `models:` must bypass the DEFAULT_BOARD auto-role path",
        roles,
        models,
    )


def test_quorum_board_only_config_uses_default_panel_level():
    """A diff-board-only override must not inject models into a text panel."""
    captured = _capture_panel_task_dispatch(
        ["quorum", "test question", "--task", "SOMECODE", "-C", str(REPO_ROOT)],
        config={"board": [{"model": "grok", "role": "architect"}]},
    )
    assert captured.get("rc") == 0, captured
    assert captured["models"] == [seat.model for seat in LIGHT_PRESET_BOARD[:3]], captured
    assert captured["roles"] == [seat.role for seat in LIGHT_PRESET_BOARD[:3]], captured


def test_quorum_all_unreachable_level_refuses_before_dispatch():
    """Unavailable selected-level seats do not fall through to a heavier roster."""
    captured = _capture_panel_task_dispatch(
        ["quorum", "test question", "--task", "SOMECODE", "-C", str(REPO_ROOT)], available=False)
    assert captured.get("rc") == 1, captured
    assert "models" not in captured and "roles" not in captured, captured


def test_run_panel_retry_unusable_retries_failed_slots_only():
    """A failed role slot is retried once; usable slots are not re-dispatched.
    Copies do not add coverage. Report is `Roles: N/M complete`."""
    from reviewlib import panel as p
    from reviewlib.backends import ReviewResult

    calls: list[str] = []

    def _fake_call(backend, model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append(model)
        if model == "b" and calls.count("b") == 1:
            return ReviewResult(
                model=model, command="fake", returncode=124, stdout="", stderr="timeout"
            )
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    jobs = [
        p.PanelJob(model="a", prompt="pa"),
        p.PanelJob(model="b", prompt="pb"),
        p.PanelJob(model="c", prompt="pc"),
    ]
    old_call = p.call_backend
    old_resolve = p.resolve_backend
    p.call_backend = _fake_call
    p.resolve_backend = lambda m: m
    try:
        results = p.run_panel_retry_unusable(jobs, Path("."), 10)
    finally:
        p.call_backend = old_call
        p.resolve_backend = old_resolve
    assert [r.returncode for r in results] == [0, 0, 0], results
    assert calls.count("a") == 1 and calls.count("c") == 1, calls
    assert calls.count("b") == 2, calls
    roles = ["architect", "correctness", "security"]
    assert p.report_roles_complete(roles, results) == "Roles: 3/3 complete"
    failed = ReviewResult(
        model="b [correctness]", command="fake", returncode=124, stdout="", stderr="t"
    )
    mixed = [
        ReviewResult(model="a", command="fake", returncode=0, stdout="ok", stderr=""),
        failed,
        ReviewResult(model="c", command="fake", returncode=0, stdout="ok", stderr=""),
    ]
    assert (
        p.report_roles_complete(roles, mixed)
        == "Roles: 2/3 complete; correctness timed out"
    )
