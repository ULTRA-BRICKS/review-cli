#!/usr/bin/env python3
"""Capability levels (review-cli#697, #695): shipped defaults + user override layer.

Everything is offline: pure resolution/selection code and `cli.main` with a patched
`cli.load_config`; no model is dispatched. Plain `test_*` functions run by the __main__
block (pytest also collects them).
"""

from __future__ import annotations

import contextlib
import copy
import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli, levels, panel  # noqa: E402
from reviewlib.config import (  # noqa: E402
    BoardConfigError,
    default_level_name,
    load_board,
    preset_board,
    preset_names,
    preset_pool_size,
    resolve_moderator_candidates,
    resolve_review_board,
    select_pool_and_reserve_with_reuse,
)

# The owner's live config after the 2026-10-27 correction: Sonnet/Sonnet/GLM routine pool,
# Codex and Opus as medium-effort reserves, Fable last, 80% omp ceiling. It has no `levels:`
# key, so it is the back-compat fixture for "an old-format config behaves as before".
LIVE_CONFIG = {
    "timeout": 300,
    "pool": 3,
    "models": [
        "omp:anthropic/claude-sonnet-5-5",
        "claude:claude-sonnet-5-5",
        "omp:zai/glm-5.3",
        "oc:xai/grok-4.7",
        "omp:opencode-go/mimo-v2.6-pro",
        "omp:openai-codex/gpt-6-luna",
        "codex:gpt-6-luna",
        "claude:claude-opus-5-5",
        "omp:anthropic/claude-opus-5-5",
        "claude:claude-fable-5-1",
    ],
    "unpaid_providers": ["commandcode", "gemini"],
    "omp_accounts": {"exclude": [], "max_usage_percent": 80},
    "board": [
        {
            "model": "omp:anthropic/claude-sonnet-5-5",
            "role": "correctness",
            "name": "Sonnet-omp",
            "effort": "low",
        },
        {
            "model": "claude:claude-sonnet-5-5",
            "role": "consistency",
            "name": "Sonnet",
            "effort": "low",
        },
        {"model": "omp:zai/glm-5.3", "role": "quality", "name": "GLM", "effort": "low"},
        {
            "model": "oc:xai/grok-4.7",
            "role": "security",
            "name": "Grok",
            "effort": "low",
        },
        {
            "model": "omp:opencode-go/mimo-v2.6-pro",
            "role": "tests",
            "name": "MiMo",
            "effort": "low",
        },
        {
            "model": "omp:openai-codex/gpt-6-luna",
            "role": "consistency",
            "name": "Luna-omp",
            "effort": "medium",
        },
        {
            "model": "codex:gpt-6-luna",
            "role": "consistency",
            "name": "Luna",
            "effort": "medium",
        },
        {
            "model": "claude:claude-opus-5-5",
            "role": "correctness",
            "name": "Opus",
            "effort": "medium",
        },
        {
            "model": "omp:anthropic/claude-opus-5-5",
            "role": "correctness",
            "name": "Opus-omp",
            "effort": "medium",
        },
        {
            "model": "claude:claude-fable-5-1",
            "role": "architect",
            "name": "Fable",
            "effort": "medium",
        },
    ],
}

HEAVY_MARKERS = ("fable", "gpt-6.1-sol", "opus", "astra", "codex", "luna")


def _models(board) -> list[str]:
    return [seat.model for seat in board]


def _cli(argv: list[str], config: dict) -> tuple[int, str, str]:
    saved = cli.load_config
    saved_task = os.environ.pop("REVIEW_TASK_CODE", None)
    out, err = io.StringIO(), io.StringIO()
    cli.load_config = lambda: config
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:  # argparse errors exit 2
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        cli.load_config = saved
        if saved_task is not None:
            os.environ["REVIEW_TASK_CODE"] = saved_task
    return rc, out.getvalue(), err.getvalue()


# --- shipped defaults, no user config ------------------------------------------------


def test_shipped_defaults_when_user_config_absent():
    assert preset_names() == ("routine", "standard", "deep")
    assert default_level_name() == "routine"
    assert levels.resolve_levels({}).names() == levels.resolve_levels(None).names()
    assert preset_pool_size("routine") == 2
    assert preset_pool_size("standard") == 4
    assert preset_pool_size("deep") == 4
    assert [s.effort for s in preset_board("routine")] == ["low"] * 5
    assert {s.effort for s in preset_board("standard")} == {"medium"}
    assert {s.effort for s in preset_board("deep")} == {"xhigh"}


def test_default_level_is_the_lowest_sufficient_one():
    assert default_level_name({}) == "routine"
    assert resolve_review_board({})[1:] == ("level:routine", "routine")
    assert preset_board(None) == preset_board("routine")


def test_legacy_preset_names_are_level_aliases():
    for old, new in (("light", "routine"), ("default", "standard"), ("heavy", "deep")):
        assert preset_board(old) == preset_board(new), old
        assert preset_pool_size(old) == preset_pool_size(new), old
        assert levels.resolve_levels().get(old).name == new


def test_routine_board_is_three_distinct_roles_on_cheap_seats():
    board = preset_board("routine")
    assert [s.role for s in board[:3]] == ["correctness", "consistency", "quality"]
    assert _models(board[:3]) == [
        "omp:anthropic/claude-sonnet-5-5",
        "claude:claude-sonnet-5-5",
        "omp:zai/glm-5.3",
    ]


def test_no_heavy_seat_or_effort_in_routine_and_standard_defaults():
    # Fable is in no shipped level; Sol and xhigh/max only in deep; Opus/Codex are
    # available in standard and deep but are not routine seats.
    for name in ("routine", "standard"):
        for seat in preset_board(name):
            assert "fable" not in seat.model and "sol" not in seat.model, (name, seat)
            assert seat.effort not in {"xhigh", "max"}, (name, seat)
    for name in preset_names():
        assert not any("fable" in s.model for s in preset_board(name)), name
    routine = " ".join(_models(preset_board("routine"))).lower()
    assert not any(m in routine for m in ("opus", "codex", "astra", "luna")), routine
    standard = " ".join(_models(preset_board("standard"))).lower()
    assert "claude-opus-5-5" in standard and "codex:gpt-6-luna" in standard, standard
    assert any("gpt-6.1-sol" in s.model for s in preset_board("deep"))


# --- inheritance and per-field override ----------------------------------------------


def test_user_override_changes_one_field_and_inherits_the_rest():
    cfg = {"levels": {"routine": {"effort": "medium"}}}
    assert {s.effort for s in preset_board("routine", cfg)} == {"medium"}
    assert _models(preset_board("routine", cfg)) == _models(preset_board("routine"))
    assert preset_pool_size("routine", cfg) == 2
    cfg = {"levels": {"standard": {"pool": 3}}}
    assert preset_pool_size("standard", cfg) == 3
    assert _models(preset_board("standard", cfg)) == _models(preset_board("standard"))


def test_user_override_replaces_one_roles_seats_only():
    cfg = {
        "levels": {"routine": {"roles": {"correctness": ["claude:claude-opus-5-5"]}}}
    }
    board = preset_board("routine", cfg)
    assert board[0].model == "claude:claude-opus-5-5" and board[0].role == "correctness"
    assert _models(board[1:]) == _models(preset_board("routine")[1:])


def test_user_override_removes_a_role_with_null_and_adds_a_seat_by_alias():
    cfg = {
        "levels": {
            "routine": {"roles": {"tests": None, "security": ["grok", "sonnet"]}}
        }
    }
    board = preset_board("routine", cfg)
    assert "tests" not in {s.role for s in board}
    assert [s.model for s in board if s.role == "security"] == [
        "oc:xai/grok-4.7",
        "claude:claude-sonnet-5-5",
    ]


def test_user_can_define_a_new_level_and_make_it_the_default():
    cfg = {
        "default_level": "mine",
        "levels": {
            "mine": {
                "pool": 1,
                "effort": "high",
                "roles": {"correctness": ["claude:claude-sonnet-5-5"]},
                "moderators": ["claude:claude-sonnet-5-5"],
            }
        },
    }
    assert default_level_name(cfg) == "mine"
    assert preset_names(cfg) == ("routine", "standard", "deep", "mine")
    assert _models(preset_board(None, cfg)) == ["claude:claude-sonnet-5-5"]
    assert resolve_review_board(cfg)[1:] == ("level:mine", "mine")


def test_user_alias_resolves_to_its_level():
    cfg = {"levels": {"standard": {"aliases": ["default", "risky"]}}}
    assert levels.resolve_levels(cfg).get("risky").name == "standard"


# --- validation: clear errors naming the key -----------------------------------------


def _raises_naming(fn, *needles):
    try:
        fn()
    except BoardConfigError as exc:
        text = str(exc)
        for needle in needles:
            assert needle in text, (needle, text)
        return
    raise AssertionError("expected BoardConfigError")


def test_unknown_level_name_is_a_clear_error():
    _raises_naming(lambda: preset_board("nope"), "nope", "routine", "standard", "deep")
    _raises_naming(lambda: preset_pool_size("nope"), "nope")


def test_invalid_effort_names_the_key():
    _raises_naming(
        lambda: preset_board(
            "routine", {"levels": {"routine": {"effort": "ludicrous"}}}
        ),
        "levels.routine.effort",
        "ludicrous",
    )
    _raises_naming(
        lambda: preset_board(
            "routine",
            {
                "levels": {
                    "routine": {"roles": {"tests": [{"model": "x", "effort": "wild"}]}}
                }
            },
        ),
        "levels.routine.roles.tests[0].effort",
    )


def test_unknown_level_key_pool_escalation_and_default_are_rejected():
    _raises_naming(
        lambda: preset_board("routine", {"levels": {"routine": {"effrot": "low"}}}),
        "levels.routine.effrot",
    )
    _raises_naming(
        lambda: preset_board("routine", {"levels": {"routine": {"pool": 0}}}),
        "levels.routine.pool",
    )
    _raises_naming(
        lambda: preset_board(
            "routine", {"levels": {"routine": {"escalate_to": "huge"}}}
        ),
        "levels.routine.escalate_to",
        "huge",
    )
    _raises_naming(
        lambda: default_level_name({"default_level": "huge"}), "default_level", "huge"
    )
    _raises_naming(lambda: preset_board("routine", {"levels": ["x"]}), "levels")
    _raises_naming(
        lambda: preset_board("mine", {"levels": {"mine": {"effort": "low"}}}),
        "levels.mine.pool",
    )


def test_unknown_role_in_a_level_is_rejected_like_a_board_role():
    cfg = {"levels": {"routine": {"roles": {"vibes": ["claude:claude-sonnet-5-5"]}}}}
    _raises_naming(
        lambda: preset_board("routine", cfg), "levels.routine.roles.vibes", "vibes"
    )


def test_cli_rejects_an_unknown_level_with_exit_2():
    rc, out, err = _cli(["diff", "--show-board", "--level", "nope"], {})
    assert rc == 2 and "nope" in err + out, (rc, out, err)
    rc, out, err = _cli(
        ["diff", "--show-board"], {"levels": {"routine": {"effort": "wild"}}}
    )
    assert rc == 2 and "levels.routine.effort" in err + out, (rc, out, err)


# --- CLI: --level, --preset alias, default -------------------------------------------


def test_cli_level_and_preset_alias_select_the_same_board():
    outs = {}
    for flag, value in (
        ("--level", "standard"),
        ("--preset", "default"),
        ("--level", "default"),
    ):
        rc, out, err = _cli(["diff", "--show-board", flag, value], {})
        assert rc == 0, (flag, value, out, err)
        outs[(flag, value)] = out
    assert len(set(outs.values())) == 1, outs
    assert "level:standard" in next(iter(outs.values()))


def test_cli_default_level_is_routine_and_help_lists_levels():
    rc, out, err = _cli(["diff", "--show-board"], {})
    assert rc == 0, (out, err)
    assert "level:routine" in out and "top 2 AVAILABLE" in out, out
    rc, out, _ = _cli(["diff", "--help"], {})
    assert rc == 0
    for needle in ("--level", "--preset", "routine", "standard", "deep"):
        assert needle in out, needle


def test_cli_list_defaults_shows_the_default_level_roster():
    rc, out, err = _cli(["--list-defaults"], {})
    assert rc == 0, (out, err)
    assert out.split()[:3] == [
        "omp:anthropic/claude-sonnet-5-5",
        "claude:claude-sonnet-5-5",
        "omp:zai/glm-5.3",
    ], out


# --- fallback stays inside the level, never a heavier one ----------------------------


def _fallback_universe(level_name: str, down: set[str]):
    board = preset_board(level_name)
    pool, reserve = select_pool_and_reserve_with_reuse(
        board, preset_pool_size(level_name), lambda seat: seat.model not in down
    )
    return board, pool, reserve


def test_unavailable_routine_seat_is_replaced_from_the_routine_reserve_only():
    routine = preset_board("routine")
    heavier = {m for n in ("standard", "deep") for m in _models(preset_board(n))} - set(
        _models(routine)
    )
    for down_count in range(len(routine) + 1):
        down = set(_models(routine)[:down_count])
        _, pool, reserve = _fallback_universe("routine", down)
        for seat in pool + reserve:
            assert seat.model in _models(routine), seat
            assert seat.model not in heavier, seat
            assert seat.model not in down, seat
    # Even with every routine seat down nothing from standard/deep is picked up.
    _, pool, reserve = _fallback_universe("routine", set(_models(routine)))
    assert pool == [] and reserve == []


def test_level_boards_never_reach_outside_their_own_roster_when_a_pool_seat_is_down():
    for name in preset_names():
        board = preset_board(name)
        first = _models(board)[0]
        _, pool, reserve = _fallback_universe(name, {first})
        assert {s.model for s in pool + reserve} <= set(_models(board)) - {first}, name


def test_escalation_is_data_for_humans_not_a_fallback_path():
    ls = levels.resolve_levels()
    assert [ls.get(n).escalate_to for n in ("routine", "standard", "deep")] == [
        "standard",
        "deep",
        None,
    ]
    # The only path to a heavier level is a proposal the pool guard prints for a human to
    # act on; a candidate is data, never something the dispatch runs by itself.
    proposals = cli._pool_guard_candidates({}, [], 2)
    labels = [c.label for c in proposals]
    assert "level:standard" in labels and "level:deep" in labels, labels
    assert all("--level" in c.why or "default" in c.why for c in proposals), proposals


# --- moderators ----------------------------------------------------------------------


def test_moderator_chain_comes_from_the_level_and_shipped_default_is_cheap():
    chain, notice = resolve_moderator_candidates({})
    assert chain == (
        "claude:claude-sonnet-5-5",
        "omp:anthropic/claude-sonnet-5-5",
        "omp:zai/glm-5.3",
    ), chain
    assert notice is None
    chain, _ = resolve_moderator_candidates({}, "standard")
    assert chain == (
        "claude:claude-opus-5-5",
        "codex",
        "gemini",
    )  # the pre-levels default
    chain, _ = resolve_moderator_candidates({}, "heavy")  # alias of deep
    assert chain[0] == "claude:claude-opus-5-5"


def test_user_level_override_changes_the_moderator_chain():
    cfg = {"levels": {"routine": {"moderators": ["omp:zai/glm-5.3"]}}}
    chain, _ = resolve_moderator_candidates(cfg)
    assert chain == ("omp:zai/glm-5.3",), chain


def test_moderator_candidates_key_beats_the_level_chain():
    cfg = {
        "moderator_candidates": ["sonnet, omp:zai/glm-5.3", "claude:claude-sonnet-5-5"]
    }
    chain, notice = resolve_moderator_candidates(cfg, "deep")
    assert chain == ("claude:claude-sonnet-5-5", "omp:zai/glm-5.3"), chain
    assert notice is None


def test_malformed_moderator_candidates_falls_to_the_level_with_a_notice():
    for bad in ([], [""], "sonnet", [3]):
        chain, notice = resolve_moderator_candidates({"moderator_candidates": bad})
        assert chain[0] == "claude:claude-sonnet-5-5", (bad, chain)
        assert notice and "moderator_candidates" in notice, (bad, notice)


def test_pick_moderators_explicit_wins_then_the_level_chain():
    saved = panel.backend_available
    panel.backend_available = lambda m: True
    try:
        chain, _ = resolve_moderator_candidates({})
        got = panel.pick_moderators("oc:custom", ["x"], chain)
        assert got[0] == "oc:custom" and got[1:4] == list(chain), got
        got = panel.pick_moderators(None, ["x"], chain)
        assert got[:3] == list(chain), got
    finally:
        panel.backend_available = saved


def _quorum_moderators(config: dict, extra: list[str]) -> list[str]:
    from reviewlib.modes import quorum as quorum_mod

    captured: dict = {}

    def fake_quorum(question, models, diff, cwd, timeout, moderators, *a, **k):
        captured["moderators"] = list(moderators)
        return 0

    saved = (quorum_mod.mode_quorum, cli._read_stdin_if_piped,
             panel.backend_available, cli._run_mode_with_stats)
    quorum_mod.mode_quorum = fake_quorum
    cli._read_stdin_if_piped = lambda: None
    panel.backend_available = lambda m: True
    # This tests moderator selection, not live host admission or persistent stats.
    # A fake transport must not queue behind the owner's real reviews.
    cli._run_mode_with_stats = lambda mode, models, run, *a, **k: run()
    env = {k: os.environ.get(k) for k in ("REVIEW_FAKE_BACKEND", "GEMINI_ENV_FILE")}
    os.environ["REVIEW_FAKE_BACKEND"] = "1"
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    try:
        rc, out, err = _cli(
            ["quorum", "q", "--task", "TEST-697", "-m", "codex", "-C", str(REPO_ROOT)]
            + extra,
            config,
        )
        assert rc == 0, (rc, out, err)
        return captured["moderators"]
    finally:
        (quorum_mod.mode_quorum, cli._read_stdin_if_piped,
         panel.backend_available, cli._run_mode_with_stats) = saved
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_quorum_cli_moderator_follows_level_key_flag_and_explicit_moderator():
    sonnet = "claude:claude-sonnet-5-5"
    assert (
        _quorum_moderators({}, [])[0] == sonnet
    )  # routine default: no heavy moderator
    assert _quorum_moderators(LIVE_CONFIG, [])[0] == sonnet  # old-format config, same
    assert (
        _quorum_moderators({}, ["--level", "standard"])[0] == "claude:claude-opus-5-5"
    )
    assert _quorum_moderators({}, ["--preset", "heavy"])[0] == "claude:claude-opus-5-5"
    assert _quorum_moderators({"moderator_candidates": ["omp:zai/glm-5.3"]}, [])[0] == (
        "omp:zai/glm-5.3"
    )
    mods = _quorum_moderators({}, ["--moderator", "omp:zai/glm-5.3"])
    assert mods[0] == "omp:zai/glm-5.3" and sonnet in mods, mods


def test_malformed_moderator_candidates_notice_reaches_stderr_once():
    cli._MODERATOR_NOTICES_SHOWN.clear()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        a = cli._moderator_candidates({"moderator_candidates": []}, None)
        b = cli._moderator_candidates({"moderator_candidates": []}, None)
    assert a == b and err.getvalue().count("moderator_candidates") == 1, err.getvalue()


def test_moderator_help_names_the_level_chain():
    saved = cli.load_config
    try:
        cli.load_config = lambda: {}
        text = cli._moderator_default_help()
        assert "claude:claude-sonnet-5-5" in text and "claude-opus" not in text, text
        cli.load_config = lambda: {
            "levels": {"routine": {"moderators": ["omp:zai/glm-5.3"]}}
        }
        assert cli._moderator_default_help() == "omp:zai/glm-5.3"
    finally:
        cli.load_config = saved


# --- back-compat with an old-format config; ceiling and unpaid untouched --------------


def test_old_format_config_without_levels_behaves_as_before():
    board, source, preset = resolve_review_board(copy.deepcopy(LIVE_CONFIG))
    assert preset is None and source == "config.yaml (models:)", (source, preset)
    assert _models(board) == LIVE_CONFIG["models"]
    assert [(s.display, s.role, s.effort) for s in board[:3]] == [
        ("Sonnet-omp", "correctness", "low"),
        ("Sonnet", "consistency", "low"),
        ("GLM", "quality", "low"),
    ]
    by_model = {s.model: s for s in board}
    assert by_model["claude:claude-opus-5-5"].effort == "medium"
    assert by_model["codex:gpt-6-luna"].effort == "medium"
    assert by_model["omp:anthropic/claude-opus-5-5"].role == "correctness"
    # pool: 3 is the active pool: Sonnet, Sonnet, GLM on three distinct roles.
    pool, reserve = select_pool_and_reserve_with_reuse(board, LIVE_CONFIG["pool"])
    assert [s.role for s in pool] == ["correctness", "consistency", "quality"], pool
    reserve_models = _models(reserve)
    assert (
        "claude:claude-opus-5-5" in reserve_models
        and "codex:gpt-6-luna" in reserve_models
    )
    assert LIVE_CONFIG["omp_accounts"] == {"exclude": [], "max_usage_percent": 80}


def test_old_format_board_only_config_still_loads_its_own_board():
    cfg = {"board": LIVE_CONFIG["board"][:3]}
    assert _models(load_board(cfg)) == LIVE_CONFIG["models"][:3]
    assert resolve_review_board(cfg)[1] == "config.yaml (board:)"


def test_levels_key_does_not_displace_a_configured_models_roster():
    cfg = {
        "models": ["claude:claude-sonnet-5-5"],
        "levels": {"routine": {"effort": "high"}},
    }
    board, source, preset = resolve_review_board(cfg)
    assert source == "config.yaml (models:)" and preset is None, (source, preset)
    assert _models(board) == ["claude:claude-sonnet-5-5"]
    # an explicit level still wins over the roster, as an explicit --preset did
    board, source, preset = resolve_review_board(cfg, preset="routine")
    assert source == "level:routine" and preset == "routine"
    assert {s.effort for s in board} == {"high"}


def test_unpaid_providers_and_ceiling_config_are_not_consumed_by_levels():
    cfg = copy.deepcopy(LIVE_CONFIG)
    before = copy.deepcopy(cfg)
    resolve_review_board(cfg)
    resolve_moderator_candidates(cfg)
    levels.resolve_levels(cfg)
    assert cfg == before
    from reviewlib import backends

    backends.configure_unpaid_providers(cfg["unpaid_providers"])
    try:
        assert backends.unpaid_providers() >= {"commandcode", "gemini"}
    finally:
        backends.configure_unpaid_providers(None)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
