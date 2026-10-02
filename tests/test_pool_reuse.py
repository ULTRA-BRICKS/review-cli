#!/usr/bin/env python3
"""Unit tests for model reuse + usage-limit exclusion in board/panel composition
(select_pool_with_reuse / expand_flat_models_with_reuse, reviewlib.config).

Covers, all offline (no model call, no network — `usage_percent` is a plain
in-memory dict lookup, never the real tg-ctl file):
  (a) enough distinct models -> IDENTICAL output to select_pool/the plain list
      (both when usage_percent is None and when nothing is excluded);
  (b) fewer distinct models than roles -> reuse fills every role, cycling
      through the available models, each repeat taking a DIFFERENT board role;
  (c) some models excluded by the 70%-threshold -> the pool shrinks BEFORE
      role-filling, then reuse pads it back up from what remains;
  (d) every model excluded -> falls back to the single least-depleted model,
      repeated for every role/slot, rather than an empty board;
  (e) a model with unknown/unavailable usage data is NOT excluded (fails open).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run
by __main__, and also pytest-discoverable.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    BoardReviewer,
    expand_flat_models_with_reuse,
    select_pool,
    select_pool_and_reserve_with_reuse,
    select_pool_with_reuse,
)

BOARD = [
    BoardReviewer("fable", "architect", "Fable"),
    BoardReviewer("sol", "consistency", "Sol"),
    BoardReviewer("opus", "correctness", "Opus"),
    BoardReviewer("glm-cc", "performance", "GLM-cc"),
    BoardReviewer("kimi", "quality", "Kimi"),
    BoardReviewer("codex", "consistency", "Codex"),
]


def _usage(pcts: dict[str, float]):
    return lambda model: pcts.get(model)


def test_enough_models_no_usage_percent_matches_select_pool():
    reuse = select_pool_with_reuse(BOARD, 4)
    plain = select_pool(BOARD, 4)
    assert reuse == plain
    assert [r.model for r in reuse] == ["fable", "sol", "opus", "glm-cc"]


def test_enough_models_with_usage_percent_nothing_excluded_matches_select_pool():
    usage_percent = _usage({})  # every model unknown -> nothing excluded
    reuse = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    plain = select_pool(BOARD, 4)
    assert reuse == plain


def test_raw_scarcity_without_usage_percent_still_clamps_like_select_pool():
    # `usage_percent=None` is the deliberate opt-OUT of reuse entirely (byte-identical
    # to `select_pool`, see `select_pool_with_reuse`'s docstring step 4) — a caller that
    # hasn't opted into reuse-awareness at all gets none, regardless of raw scarcity vs.
    # hard cooldown. With no `usage_percent` given, only fable and glm-cc are AVAILABLE
    # (2 of 6), and the pool still clamps to 2 — identical to plain `select_pool`.
    available = {"fable", "glm-cc"}
    result = select_pool_with_reuse(BOARD, 4, available=lambda r: r.model in available)
    assert len(result) == 2
    assert [r.model for r in result] == ["fable", "glm-cc"]


def test_raw_scarcity_with_usage_percent_pads_to_requested_pool_task_cli_139():
    # Same raw scarcity (only fable/glm-cc reachable, 2 of 6), but
    # usage_percent IS supplied this time with nothing actually near its
    # limit — this is the HARD-unavailability case (seats excluded from
    # `reachable` itself, e.g. a real session-limit cooldown), not the
    # near-usage-limit case. Before the task-cli#139 fix, `n` collapsed to
    # `len(reachable)` == 2 here, so the padding loop never ran and the pool
    # stayed at 2 even though 4 were requested. The fix sizes `n` against the
    # full board (the real ceiling on how many distinct role slots exist),
    # so the 2 reachable seats now get reused across 2 more board roles.
    available = {"fable", "glm-cc"}
    result = select_pool_with_reuse(
        BOARD, 4, available=lambda r: r.model in available, usage_percent=_usage({})
    )
    assert len(result) == 4
    models = [r.model for r in result]
    roles = [r.role for r in result]
    assert models[:2] == ["fable", "glm-cc"]  # both keep their own role first
    assert len(set(roles)) == 4  # every slot has a DISTINCT role/lens
    assert set(models) == {"fable", "glm-cc"}  # only the 2 reachable models used


def test_hard_cooldown_single_reachable_seat_pads_across_distinct_roles_task_cli_139():
    # The exact production incident (task-cli#139, 2026-09-07): Opus/Sonnet/
    # Fable/GLM are all in session-limit cooldown (`available()` returns
    # False for them, NOT a usage_percent threshold — no usage_percent is
    # passed here at all, isolating this from the already-working
    # near-usage-limit path), only Codex survives. A default `--pool 3`
    # request must still cover 3 DISTINCT board roles by reusing the one
    # live seat, not collapse to a single role.
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("oc:zai/glm-5.2", "quality", "GLM"),
        BoardReviewer("claude:claude-sonnet-5", "quality", "Sonnet"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
    ]
    only_codex = lambda r: r.model == "codex:gpt-5.6-terra"  # noqa: E731
    # usage_percent given but every model comfortably under_limit — proves
    # this is the RAW-unavailability path, not the near-usage-limit one.
    result = select_pool_with_reuse(
        board, 3, available=only_codex, usage_percent=_usage({})
    )
    assert len(result) == 3
    assert {r.model for r in result} == {"codex:gpt-5.6-terra"}
    roles = [r.role for r in result]
    assert len(set(roles)) == 3  # 3 distinct roles, not 1
    assert roles[0] == "consistency"  # codex keeps its own role first


def test_limit_exclusion_shrinks_pool_before_reuse():
    # All 6 board seats are reachable, but sol/opus/kimi/codex are all >=70% used —
    # only fable and glm-cc remain under the threshold. Pool wants 4 seats: reuse
    # must fill the remaining 2 slots by cycling fable/glm-cc again, each under a
    # DIFFERENT (unused) role.
    usage_percent = _usage(
        {"sol": 71, "opus": 95, "kimi": 70, "codex": 88, "fable": 10, "glm-cc": 20}
    )
    result = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    assert len(result) == 4
    models = [r.model for r in result]
    roles = [r.role for r in result]
    assert models == ["fable", "glm-cc", "fable", "glm-cc"]
    assert len(set(roles)) == 4  # every slot has a DISTINCT role/lens
    assert roles[0] == "architect"  # fable keeps its own role first
    assert roles[1] == "performance"  # glm-cc keeps its own role first


def test_real_default_board_duplicate_roles_never_dispatch_byte_identical_seats():
    # DEFAULT_BOARD genuinely repeats several roles: "consistency" (Sol + Astra,
    # pre-existing) and, as of review-cli#382, "performance" (GLM-cc + Terra), "quality"
    # (Kimi + Sonnet), and "security" (Qwen + the z.ai-routed GLM). Excluding everything
    # except the two seats that share ONE of those roles must never pad a second slot
    # onto that SAME role — that would dispatch the same model under the same role/lens
    # twice, a pure-cost duplicate the "N distinct lenses" contract forbids (k3 review
    # finding, review-cli#205 round 3).
    glm_cc = next(r for r in DEFAULT_BOARD if r.model == "commandcode:zai-org/GLM-5.2")
    terra = next(r for r in DEFAULT_BOARD if r.model == "codex:gpt-5.6-terra")
    assert glm_cc.role == "performance" and terra.role == "performance"
    usage_percent = _usage(
        {
            m.model: 99
            for m in DEFAULT_BOARD
            if m.model not in (glm_cc.model, terra.model)
        }
    )
    result = select_pool_with_reuse(list(DEFAULT_BOARD), 4, usage_percent=usage_percent)
    seen = set()
    for r in result:
        key = (r.model, r.role)
        assert key not in seen, f"byte-identical duplicate seat dispatched: {key}"
        seen.add(key)
    # Both under-limit seats (glm_cc, terra) share role "performance" -- padding
    # must borrow roles from ELSEWHERE on the board (architect/correctness/...)
    # for the extra slots, never repeat "performance" onto a second slot for
    # the same model. Plenty of other distinct roles exist on the 12-seat
    # board, so all 4 requested slots fill (this fixture doesn't exercise the
    # separate "roles genuinely exhausted -> stop short" branch).
    assert len(result) == 4


def test_total_exhaustion_falls_back_to_least_depleted_single_model():
    usage_percent = _usage(
        {"fable": 99, "sol": 95, "opus": 91, "glm-cc": 100, "kimi": 88, "codex": 71}
    )
    result = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    assert len(result) == 4
    models = {r.model for r in result}
    assert models == {"codex"}  # codex (71) is the least-depleted of the six
    roles = [r.role for r in result]
    assert len(set(roles)) == 4  # still 4 distinct lenses, just one model


def test_least_depleted_skips_exhausted_family():
    # Every reachable seat is at 100%. The old least-depleted fallback would
    # still pick one (board-priority tie-break) and spawn it. Exhausted
    # families must shrink the pool to empty instead.
    usage_percent = _usage(
        {
            "fable": 100,
            "sol": 100,
            "opus": 100,
            "glm-cc": 100,
            "kimi": 100,
            "codex": 100,
        }
    )
    result = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    assert result == []


def test_unknown_usage_is_not_excluded():
    # opus has NO usage sample (None) — must be treated as "not near limit", never
    # excluded, even though every OTHER seat is deep in its limit.
    usage_percent = _usage({"fable": 99, "sol": 95, "opus": None, "glm-cc": 100})
    result = select_pool_with_reuse(BOARD, 2, usage_percent=usage_percent)
    models = [r.model for r in result]
    assert "opus" in models


def test_explicit_threshold_is_honored():
    usage_percent = _usage({"fable": 50, "sol": 50})
    strict = select_pool_with_reuse(
        BOARD[:2], 2, usage_percent=usage_percent, limit_threshold=40
    )
    # both models are >=40 -> both excluded -> fallback to the single least-depleted
    assert {r.model for r in strict} == {"fable"}  # tie -> first in priority order

    lenient = select_pool_with_reuse(
        BOARD[:2], 2, usage_percent=usage_percent, limit_threshold=60
    )
    assert {r.model for r in lenient} == {"fable", "sol"}


def test_role_less_board_never_reuses():
    # A config `models:` roster with no lens metadata carries role="" for every
    # seat. Reuse would add pure cost (an extra dispatch) with zero added
    # review diversity there, so it must stay a plain shrink instead.
    role_less = [
        BoardReviewer("fable", "", "Fable"),
        BoardReviewer("glm-cc", "", "GLM-cc"),
    ]
    usage_percent = _usage({"fable": 90, "glm-cc": 10})
    result = select_pool_with_reuse(role_less, 4, usage_percent=usage_percent)
    assert len(result) == 1  # excluded fable, kept glm-cc, did NOT pad back to 4
    assert result[0].model == "glm-cc"


def test_unknown_role_board_never_reuses_task_cli_139():
    # Codex review finding, task-cli#139: a board whose `role` strings are all
    # TYPOS/unknown REVIEW_ROLES keys (e.g. a hand-edited config with "typo-a",
    # "typo-b", "typo-c") is functionally role-less — every seat's `role_lens`
    # resolves empty, so every seat falls back to the SAME generic prompt
    # regardless of its `role` label. Before this fix, `any(r.role for r in
    # board)` treated these non-empty-but-invalid strings as real lenses and
    # padded anyway: with only Codex reachable and pool=3, it would have
    # dispatched Codex three times with the IDENTICAL generic prompt while
    # mislabeling the three seats as three distinct roles "typo-a"/"typo-b"/
    # "typo-c" -- pure duplicate cost, zero added review diversity, and a
    # misleading transcript. Must behave exactly like a role-less board: stay
    # at the smaller un-padded size instead of pretending to pad.
    board = [
        BoardReviewer("codex", "typo-a", "Codex"),
        BoardReviewer("gemini", "typo-b", "Gemini"),
        BoardReviewer("claude", "typo-c", "Claude"),
    ]
    for r in board:
        assert r.role_lens == "", (
            "sanity: none of these roles are real REVIEW_ROLES keys"
        )
    only_codex = lambda r: r.model == "codex"  # noqa: E731
    result = select_pool_with_reuse(
        board, 3, available=only_codex, usage_percent=_usage({})
    )
    assert len(result) == 1  # did NOT pad to 3 identical-generic-prompt duplicates
    assert result[0].model == "codex"


def test_mixed_known_and_unknown_roles_pads_only_into_the_known_lens():
    # A board mixing one REAL role (correctness) with typo'd ones: reuse must still
    # fire (the board is NOT functionally role-less -- one seat has a genuine lens),
    # but the padding loop must never hand out a typo'd role as if it were a distinct
    # lens -- it should pad short rather than fabricate a "second distinct role" out
    # of an unknown string.
    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "typo-b", "Gemini"),
        BoardReviewer("claude", "typo-c", "Claude"),
    ]
    only_codex = lambda r: r.model == "codex"  # noqa: E731
    result = select_pool_with_reuse(
        board, 3, available=only_codex, usage_percent=_usage({})
    )
    # No unknown role has a real lens to pad into, and codex's own "correctness" role
    # is already used by the primary slot -- so there is nothing left in role_pool to
    # pad with, and the result stays at the un-padded size of 1.
    assert len(result) == 1
    assert result[0].role == "correctness"


# --- expand_flat_models_with_reuse (quorum/just-ask/brainstorm's flat panel) -------


def test_flat_no_usage_percent_is_identity():
    models = ["fable", "sol", "opus"]
    assert expand_flat_models_with_reuse(models, 3) == models


def test_flat_nothing_excluded_is_identity():
    models = ["fable", "sol", "opus"]
    assert expand_flat_models_with_reuse(models, 3, usage_percent=_usage({})) == models


def test_flat_reuse_pads_back_to_target():
    result = expand_flat_models_with_reuse(
        ["fable", "glm-cc"], 4, usage_percent=_usage({})
    )
    assert result == ["fable", "glm-cc", "fable", "glm-cc"]


def test_flat_excludes_near_limit_then_reuses_remainder():
    models = ["fable", "sol", "opus", "glm-cc"]
    usage_percent = _usage({"sol": 90, "opus": 85, "fable": 5, "glm-cc": 15})
    result = expand_flat_models_with_reuse(models, 4, usage_percent=usage_percent)
    assert result == ["fable", "glm-cc", "fable", "glm-cc"]


def test_flat_total_exhaustion_falls_back_to_single_least_depleted():
    models = ["fable", "sol"]
    usage_percent = _usage({"fable": 99, "sol": 95})
    result = expand_flat_models_with_reuse(models, 3, usage_percent=usage_percent)
    assert result == ["sol", "sol", "sol"]


def test_flat_exhausted_does_not_repeat_100_percent_model():
    models = ["fable", "sol"]
    usage_percent = _usage({"fable": 100, "sol": 100})
    result = expand_flat_models_with_reuse(models, 3, usage_percent=usage_percent)
    assert result == []


# --- select_pool_and_reserve_with_reuse (the board-mode dispatch wiring) -----------


def test_reserve_is_seat_identity_not_model_string():
    # A config board that legitimately lists the SAME model under two DIFFERENT
    # roles is a supported configuration (board_from_models / role metadata) —
    # filtering reserve by `.model` string would wrongly drop the second seat's
    # reserve slot just because the FIRST seat (same model) landed in the pool.
    dup_model_board = [
        BoardReviewer("fable", "architect", "Fable-A"),
        BoardReviewer("sol", "consistency", "Sol"),
        BoardReviewer("fable", "security", "Fable-B"),  # same model, distinct seat
    ]
    pool, reserve = select_pool_and_reserve_with_reuse(dup_model_board, 2)
    assert [r.display for r in pool] == ["Fable-A", "Sol"]
    # Fable-B is a DISTINCT seat object from Fable-A (same model, different
    # role/display) — it must still be reserve-eligible.
    assert [r.display for r in reserve] == ["Fable-B"]


def test_reserve_excludes_near_limit_seat_only_when_it_is_actually_used():
    usage_percent = _usage({"opus": 90})  # opus is near-limit
    pool, reserve = select_pool_and_reserve_with_reuse(
        BOARD, 2, usage_percent=usage_percent
    )
    # pool wants 2: fable (fine) + sol (fine) — opus is skipped for being
    # near-limit, but with 5 OTHER under-limit candidates available no reuse
    # is needed to fill 2 seats.
    assert [r.model for r in pool] == ["fable", "sol"]
    # opus must still be VISIBLE in reserve (coherent policy: a near-limit
    # seat that isn't actually in the pool is a valid last-resort backfill).
    reserve_models = [r.model for r in reserve]
    assert "opus" in reserve_models
    assert set(reserve_models) == {"opus", "glm-cc", "kimi", "codex"}
    # But its ORDER is demoted to the END: opus sits at board position 2
    # (ahead of glm-cc/kimi/codex), yet mid-run failover must try the
    # perfectly healthy overflow seats FIRST, not promote the very
    # near-limit account this feature exists to protect (Fable review
    # finding, review-cli#205 round 5). Board-priority order is preserved
    # WITHIN each group (under-limit seats keep their relative order; here
    # there's only one near-limit seat, so its own relative order is moot).
    assert reserve_models == ["glm-cc", "kimi", "codex", "opus"]


def test_reserve_ordering_preserves_relative_priority_within_each_group():
    # BOARD board-priority order: fable, sol, opus, glm-cc, kimi, codex.
    # pool=1 -> only fable is primary. Reserve holds the other 5: opus and
    # kimi are near-limit; sol, glm-cc, codex are under-limit. Each group
    # must keep its OWN relative board-priority order after the demotion.
    usage_percent = _usage({"opus": 80, "kimi": 90})
    _, reserve = select_pool_and_reserve_with_reuse(
        BOARD, 1, usage_percent=usage_percent
    )
    assert [r.model for r in reserve] == ["sol", "glm-cc", "codex", "opus", "kimi"]


def test_reserve_omits_exhausted_family():
    # An exhausted Codex seat (100%) must not land in reserve: after a primary
    # failure the board path would promote it, and Codex has no backend spawn
    # guard. Near-limit seats stay last-resort; exhausted ones do not.
    usage_percent = _usage(
        {
            "fable": 10,
            "sol": 10,
            "opus": 10,
            "glm-cc": 10,
            "kimi": 10,
            "codex": 100,
        }
    )
    pool, reserve = select_pool_and_reserve_with_reuse(
        BOARD, 2, usage_percent=usage_percent
    )
    assert "codex" not in [r.model for r in pool]
    assert "codex" not in [r.model for r in reserve]


def test_pool_and_reserve_probe_availability_exactly_once():
    calls = []

    def _available(r: BoardReviewer) -> bool:
        calls.append(r.model)
        return True

    select_pool_and_reserve_with_reuse(BOARD, 3, _available)
    # One probe per board seat, not two (pool split + reserve split each
    # re-probing independently would double this).
    assert len(calls) == len(BOARD)


# --- integration: the real _mode_review_board / mode_review dispatch path ---------
# (task-cli#139): a unit test on select_pool_with_reuse alone proves the selector's
# own math, but not that the fix is actually WIRED all the way through the real
# dispatch call site — a future refactor could stop passing usage_percent through, or
# stop calling select_pool_and_reserve_with_reuse, and the unit tests above would stay
# green while the real `review diff` command reverted to the pre-fix behaviour. This
# drives `mode_review` end-to-end (fake backend, no network/subprocess) with a
# hard-cooldown board (only Codex's `available()` returns True) and asserts the run
# that actually DISPATCHED covers 3 distinct roles, not 1.


def test_mode_review_board_pads_hard_cooldown_pool_to_distinct_roles_task_cli_139():
    from reviewlib.backends import ReviewResult
    from reviewlib.modes import review as review_mod

    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("oc:zai/glm-5.2", "quality", "GLM"),
        BoardReviewer("claude:claude-sonnet-5", "quality", "Sonnet"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
    ]

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout=f"checked: ok from {model} — no issues",
            stderr="",
        )

    import reviewlib.panel as panel_mod

    saved_resolve = panel_mod.resolve_backend
    saved_available = review_mod.backend_available
    panel_mod.resolve_backend = lambda _m: _fake_backend
    # Only Codex is reachable — the exact production incident: Opus/Sonnet/Fable/GLM
    # in session-limit cooldown (a hard `available()` False, not a usage_percent
    # threshold).
    review_mod.backend_available = lambda model: model == "codex:gpt-5.6-terra"
    outcome_sink: list = []
    try:
        rc = review_mod.mode_review(
            [],
            "Review this diff.",
            "+x",
            REPO_ROOT,
            5,
            False,
            board=board,
            pool_size=3,
            outcome_sink=outcome_sink,
            usage_percent=_usage({}),  # opted in, nothing near its limit
        )
    finally:
        panel_mod.resolve_backend = saved_resolve
        review_mod.backend_available = saved_available

    assert rc == 0, "a fully-padded pool must not be reported as degraded"
    assert len(outcome_sink) == 1
    outcome = outcome_sink[0]
    assert len(outcome.usable) == 3
    assert set(outcome.usable_models) == {"codex:gpt-5.6-terra"}
    assert len(set(outcome.usable_roles)) == 3  # 3 distinct roles, not 1


def test_light_preset_one_reachable_with_ship_pool_covers_three_roles():
    """Once a recorded `--task` floors light to pool 3, one reachable light-preset
    seat must cover 3 distinct roles (the #309 ship-gate gap). usage_percent is
    required: without it, select_pool_with_reuse clamps to len(reachable)=1."""
    from reviewlib.config import LIGHT_PRESET_BOARD

    board = list(LIGHT_PRESET_BOARD)
    live = board[0].model
    result = select_pool_with_reuse(
        board, 3, available=lambda r: r.model == live, usage_percent=_usage({})
    )
    assert len(set(r.role for r in result)) >= 3, [r.role for r in result]
    assert {r.model for r in result} == {live}


def test_one_seat_explicit_model_board_cannot_pad_without_cloned_roles():
    """board_from_models(['grok']) is 1 seat, so n=min(3, len(board))=1 and
    reuse has no unused role to pad into — the --task path must clone roles
    first (ensure_min_distinct_roles). Pins the ceiling that made `-m grok`
    skip padding even when usage_percent was passed."""
    from reviewlib.config import GROK_SEAT, board_from_models

    board = board_from_models([GROK_SEAT], {})
    assert len(board) == 1
    result = select_pool_with_reuse(board, 3, usage_percent=_usage({}))
    assert len(result) == 1
    assert len({r.role for r in result}) <= 1


def test_ensure_min_distinct_roles_lets_one_model_pad_to_ship_floor():
    from reviewlib.config import (
        GROK_SEAT,
        SHIP_MIN_ROLES,
        board_from_models,
        ensure_min_distinct_roles,
    )

    board = board_from_models([GROK_SEAT], {})
    padded = ensure_min_distinct_roles(board, SHIP_MIN_ROLES)
    assert {r.model for r in padded} == {GROK_SEAT}
    assert len({r.role for r in padded if r.role}) >= SHIP_MIN_ROLES
    result = select_pool_with_reuse(padded, SHIP_MIN_ROLES, usage_percent=_usage({}))
    assert len(set(r.role for r in result)) >= SHIP_MIN_ROLES
    assert {r.model for r in result} == {GROK_SEAT}


def test_ensure_min_distinct_roles_repairs_every_role_less_seat_without_collision():
    """review-cli#309 round-2 review finding: a single-seat repair that blindly
    assigns the source's FIRST known role (with no collision check against roles
    other seats already carry) can duplicate an existing seat's role and waste a
    pool slot -- pushing a genuinely distinct cloned role past a positional
    `pool`/`--pool N` slice, so the SLICED pool ends up short of the requested
    distinct-role floor even though `ensure_min_distinct_roles` returned enough
    seats overall. Every role-less/unknown-role seat must be repaired onto its
    own UNUSED role before any clone is appended."""
    from reviewlib.config import BoardReviewer, ensure_min_distinct_roles

    # Seat 0 is role-less (e.g. a config `models:` entry with no board metadata);
    # seat 1 already carries "correctness" (e.g. Opus). A naive single-seat repair
    # would hand seat 0 the source's first known role too -- also "correctness" --
    # colliding with seat 1 and yielding only 2 distinct roles across 3 seats.
    board = [
        BoardReviewer(model="my-custom", role="", display="Custom", effort="high"),
        BoardReviewer(
            model="claude:claude-opus-4-8",
            role="correctness",
            display="Opus",
            effort="high",
        ),
    ]
    padded = ensure_min_distinct_roles(board, 3)
    sliced = padded[:3]
    distinct = {r.role for r in sliced if r.role}
    assert len(distinct) == 3, (
        [(r.model, r.role) for r in sliced],
        [(r.model, r.role) for r in padded],
    )

    # Two role-less seats: BOTH must be repaired (not just seat 0).
    board2 = [
        BoardReviewer(model="foo", role="", display="Foo", effort="high"),
        BoardReviewer(model="bar", role="", display="Bar", effort="high"),
    ]
    padded2 = ensure_min_distinct_roles(board2, 3)
    sliced2 = padded2[:3]
    distinct2 = {r.role for r in sliced2 if r.role}
    assert len(distinct2) == 3, [(r.model, r.role) for r in padded2]
    assert padded2[0].role != padded2[1].role, [(r.model, r.role) for r in padded2]


def test_ensure_min_distinct_roles_prefix_dedups_duplicate_known_roles():
    """Two seats sharing `correctness` + min_roles=3: the sliced prefix must
    still have 3 distinct roles (the duplicate moves behind the prefix)."""
    from reviewlib.config import BoardReviewer, ensure_min_distinct_roles

    board = [
        BoardReviewer("codex", "correctness", "A"),
        BoardReviewer("gemini", "correctness", "B"),
    ]
    padded = ensure_min_distinct_roles(board, 3)
    assert len({r.role for r in padded[:3] if r.role}) == 3, [
        (r.model, r.role) for r in padded
    ]


def test_recorded_task_pool_size_floors_light_unless_explicit():
    from reviewlib.config import recorded_task_pool_size

    assert recorded_task_pool_size(2, explicit_pool=False, recorded=False) == 2
    assert recorded_task_pool_size(2, explicit_pool=False, recorded=True) == 3
    assert recorded_task_pool_size(1, explicit_pool=True, recorded=True) == 1
    assert recorded_task_pool_size(0, explicit_pool=False, recorded=True) == 0
    assert recorded_task_pool_size(4, explicit_pool=False, recorded=True) == 4


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
