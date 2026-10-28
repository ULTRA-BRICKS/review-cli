"""The bare-check zero-role fallback and its role-tracking-gap explanation (review-cli#721).

Split out of ``reviewlib.stats.quorum_check`` (see its docstring, "Bare-check zero-role
fallback" and "Genuinely-old vs. merely-mode-avoiding"). A bare check (neither floor
given) is role-based at ``_DEFAULT_QUORUM_FLOOR``. When the task has NO role data anywhere
in its non-mismatched history, one of two things is true:

  (a) every iteration provably predates role-tracking -- nothing can ever be done about
      that history, so the gate falls back to the pre-#246 model-counting default; or
  (b) at least one iteration does not -- the task simply never chose a role-recording
      mode. That is NOT given the free pass (it would let a task dodge the role gate
      forever by always reviewing through quorum/brainstorm); the gate stays role-based,
      fails on its own merits, and ``role_tracking_gap`` names the fix.

``resolve_floors`` returns the floors in force plus whichever note applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stats_quorum_evidence import (
    _ROLE_TRACKING_CUTOFF,
    _all_iterations_predate_role_tracking,
    _has_any_role_data,
    _iteration_predates_role_tracking,
    _iterations_excluding_mismatched,
)
from .stats_quorum_model import _DEFAULT_QUORUM_FLOOR, QuorumHistory, QuorumRequest

# `mode="review"` is the run-stats identity of the `review diff` SUBCOMMAND (see
# reviewlib/modes/review.py's `ModeSpec` -- the verb moved to `diff`, the stats key stayed
# "review"), so it is rendered as `review diff`, never the removed `review review`.
# `visual` has the same board-vs-flat duality as `review` (cli.py gates the board path on
# `mode.name not in ("review", "visual")`): a role-less run of either is a missing-board-
# config problem, unlike the role-blind PANEL modes (quorum/just-ask/brainstorm/qa) that
# can NEVER record roles whatever the board config.
_BOARD_CAPABLE_MODES = ("review", "visual")


@dataclass(frozen=True)
class FloorResolution:
    """The floors in force after the history is known, plus the note explaining a swap or
    a gap (both None for an ordinary check)."""

    min_models: int | None
    min_roles: int | None
    mode_fallback: str | None = None
    tracking_gap: str | None = None


def role_data_population(request: QuorumRequest, history: QuorumHistory) -> list[dict]:
    """The iterations whose role data decides the fallback, or [] when it cannot apply.

    Only the true bare-check path is eligible (an explicit floor is never second-guessed)
    and only for a readable store whose PASSED history carries no role. The population is
    ALL verdicts minus diff-identity-MISMATCHED ones: a role-tagged iteration that FAILED
    still proves the task went through role-recording review (so the role floor must fail
    on its own merits), while a role-tagged iteration for a genuinely different repo/diff
    must not force THIS check into role mode (that would reopen the cross-repo pollution
    the identity binding closes, through the counting mode). Passed iterations are reused
    from ``gate_iterations``; only the failed/unverdicted remainder is freshly classified.
    Empty also for "no history at all" and "every iteration mismatched": those keep the
    plain role-based shape instead of a misleading "no role-tagged history" note.
    """
    if not request.bare_default or history.store_error is not None:
        return []
    if _has_any_role_data(history.gate_iterations):
        return []
    non_passed = [item for item in history.iterations if item.get("passed") is not True]
    return history.gate_iterations + _iterations_excluding_mismatched(
        non_passed, request.repo_id, request.diff_files
    )


def _scope_phrase(request: QuorumRequest) -> str:
    """What the check compared against, for messages. Empty without a check context (a
    direct library caller has nothing to claim); repo-only mode compared no diff."""
    if request.repo_id is None:
        return ""
    if request.diff_files is None:
        return " against the current repo"
    return " against the current repo/diff"


def _legacy_fallback_note(request: QuorumRequest, clean: str | None, scope: str) -> str:
    """The ``quorum_mode_fallback`` message for case (a). The "excluding mismatched"
    qualifier is only claimed when a check context made that exclusion real."""
    in_scope_note = ""
    if request.repo_id is not None:
        in_scope_note = (
            " -- excluding any recorded for a different repository --"
            if request.diff_files is None
            else " -- excluding any recorded for a different repo/diff --"
        )
    return (
        f"no role-tagged review history recorded for {clean}{scope}; fell "
        "back to the pre-review-cli#246 model-counting default "
        f"(min_models={_DEFAULT_QUORUM_FLOOR}) since there is no role data "
        "to count against the new role-based default (every in-scope "
        f"iteration{in_scope_note} predates role-tracking)"
    )


def _modes_phrase(modes_used: list[str]) -> str:
    """The modes of the iterations behind the gap, as runnable-command names."""
    display_modes = [
        "review diff" if m == "review" else f"review {m}" for m in modes_used
    ]
    return ", ".join(f"`{m}`" for m in display_modes) or "an unverified mode"


def _rerun_hint(clean: str | None, board_capable_used: list[str]) -> str:
    """How to re-run a board-capable review. `review visual` REQUIRES an IMAGE positional
    the gate cannot know, so it is described, not printed as a copy-pasteable command."""
    if board_capable_used == ["review"]:
        return f"`review diff --task {clean}`"
    if board_capable_used == ["visual"]:
        return (
            f"`review visual IMAGE --task {clean}` (IMAGE is the "
            "screenshot you're reviewing)"
        )
    return (
        f"`review diff --task {clean}` (or `review visual "
        f"IMAGE --task {clean}` for screenshot review)"
    )


def _board_fix(
    clean: str | None, board_capable_used: list[str], not_legacy: list[dict]
) -> str:
    """The remedy for role-less board-capable runs. A record with ``ok_count == 0 and
    fail_count > 0`` means EITHER every configured seat failed OR no board was configured
    and a flat `-m` dispatch failed -- the record cannot say which, so the message hedges
    both readings instead of sending a boardless operator to fix seat health."""
    rerun_hint = _rerun_hint(clean, board_capable_used)
    board_seats_failed = any(
        item.get("mode") in _BOARD_CAPABLE_MODES
        and item.get("ok_count") == 0
        and (item.get("fail_count") or 0) > 0
        for item in not_legacy
    )
    if board_seats_failed:
        return (
            "either configure a reviewer board for this repo (see "
            "`review --show-board` / config.yaml's `board:` entry) or, "
            "if one is already configured, restore its seats to health "
            "(dead API keys, missing CLI auth, and an unpaid provider "
            f"are the usual causes) -- then re-run {rerun_hint} -- this "
            "run recorded zero usable seats, which happens either way "
            "and records no per-seat roles"
        )
    return (
        "configure a reviewer board for this repo (see `review "
        "--show-board` / config.yaml's `board:` entry) and re-run "
        f"{rerun_hint} through it -- a role-less run with no board "
        "config records no per-seat roles"
    )


def _diff_fix(clean: str | None, modes_used: list[str]) -> str:
    """The remedy when no board-capable mode is involved. With NO identifiable mode (a
    corrupted record with a blank `mode`) it does not claim which modes never record roles."""
    if modes_used:
        return (
            f"run `review diff --task {clean}` through a role-tracking "
            "board (quorum/just-ask/brainstorm/qa never record roles at "
            "all)"
        )
    return f"run `review diff --task {clean}` through a role-tracking board"


def _gap_fix(clean: str | None, modes_used: list[str], not_legacy: list[dict]) -> str:
    """The remedy named in the gap message, by which kinds of modes are involved."""
    board_capable_used = sorted(m for m in modes_used if m in _BOARD_CAPABLE_MODES)
    has_panel = any(m not in _BOARD_CAPABLE_MODES for m in modes_used)
    if not board_capable_used:
        return _diff_fix(clean, modes_used)
    board_fix = _board_fix(clean, board_capable_used, not_legacy)
    if not has_panel:
        return board_fix
    # Deliberately generic ("the other" rather than naming quorum/just-ask/brainstorm/qa):
    # a future/unrecognized mode is also not board-capable, and a hardcoded list would
    # misdescribe it.
    return (
        f"{board_fix}; the other iteration(s) need a "
        "role-tracking `review diff` run instead regardless of "
        "board config (quorum/just-ask/brainstorm/qa never "
        "record roles at all)"
    )


def _role_tracking_gap(
    clean: str | None, population: list[dict], scope: str, min_roles: int | None
) -> str:
    """The ``role_tracking_gap`` message for case (b).

    Built from ONLY the iterations that caused case (b) -- those NOT provably predating
    the cutoff -- not the whole population, which can mix legacy and non-legacy ones;
    counting or naming the legacy ones would overstate the gap.
    """
    not_legacy = [
        item for item in population if not _iteration_predates_role_tracking(item)
    ]
    modes_used = sorted(
        {m for item in not_legacy if isinstance(m := item.get("mode"), str) and m}
    )
    # Derived from `_ROLE_TRACKING_CUTOFF` itself so a corrected constant never leaves
    # this message quoting a stale date.
    cutoff_date = _ROLE_TRACKING_CUTOFF.date().isoformat()
    fix_it = _gap_fix(clean, modes_used, not_legacy)
    return (
        f"{clean} has {len(not_legacy)} review iteration(s) not confirmed "
        f"to predate role-tracking (review-cli#246, merged {cutoff_date}) "
        f"-- either recorded after it became available, or with a timestamp "
        f"that can't be verified as older{scope} -- yet none carry role "
        f"data: they ran under {_modes_phrase(modes_used)} without recording per-seat "
        "roles. This is NOT eligible for the legacy model-counting "
        "fallback (that only covers history verifiably predating "
        f"role-tracking entirely): {fix_it} to record real role "
        f"coverage -- {min_roles} distinct role(s) are required."
    )


def resolve_floors(request: QuorumRequest, history: QuorumHistory) -> FloorResolution:
    """The floors in force for this history (see the module docstring): the request's own,
    except that case (a) swaps the role floor for the model-counting default."""
    population = role_data_population(request, history)
    if not population or _has_any_role_data(population):
        return FloorResolution(request.min_models, request.min_roles)
    scope = _scope_phrase(request)
    if _all_iterations_predate_role_tracking(population):
        return FloorResolution(
            min_models=_DEFAULT_QUORUM_FLOOR,
            min_roles=None,
            mode_fallback=_legacy_fallback_note(request, history.clean, scope),
        )
    return FloorResolution(
        request.min_models,
        request.min_roles,
        tracking_gap=_role_tracking_gap(
            history.clean, population, scope, request.min_roles
        ),
    )
