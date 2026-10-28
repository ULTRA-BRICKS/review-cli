"""The self-merge-authority quorum gate (review-cli#721 split of ``reviewlib.stats``).

``quorum_check`` answers "does this task have enough passed, identity-verified review
iterations across enough distinct models / board roles". It is a thin pipeline over the
pieces beside it: ``stats_quorum_model`` (the request, the task's bucketed history, the
early-reject payload), ``stats_quorum_fallback`` (the bare-check zero-role fallback and the
role-tracking-gap note), ``stats_quorum_evidence`` (per-iteration classification and
counting). Fail-closed throughout: an unverdicted iteration never counts, an unreadable
store never passes, an invalid request is rejected before the store is read.

The ``result[...]`` keys of the ``--check --json`` payload are assigned in ``quorum_check``
and ``_finalize_quorum_result`` themselves (not in helpers): the contract test that keeps
``review help task-check`` honest discovers the field names by parsing those two sources.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import seat_cooldown as _seat_cooldown
from .stats_identity import _github_issue_twin
from .stats_quorum_evidence import (
    _MISMATCH_DETAILS_CAP,
    _distinct_models,
    _distinct_roles,
    _foreign_repo_iterations,
    _iterations_excluding_mismatched,
    _models_behind_role_coverage,
    _seat_credited_roles,
)
from .stats_quorum_fallback import FloorResolution, resolve_floors
from .stats_quorum_model import (
    QuorumHistory,
    QuorumRequest,
    build_request,
    check_inputs,
    load_history,
    rejected_result,
)

_MIN_MODELS_ADVISORY = (
    "an explicit min_models floor is set; role-based coverage (min_roles) is "
    "generally sufficient and this explicit floor may not be needed unless "
    "you have a specific reason to require distinct models"
)


@dataclass(frozen=True)
class _Gate:
    """What the history earns against the floors in force."""

    models: list[str]
    roles: list[str] | None
    met: bool


def _evaluate_gate(
    request: QuorumRequest, history: QuorumHistory, floors: FloorResolution
) -> _Gate:
    """Count the history against the floors in force.

    Models are counted over the PASSED, identity-surviving iterations. Role credit
    (review-cli#137) is SEAT-granular, not run-granular: it is computed across every
    non-diff-identity-mismatched iteration of ANY verdict, reading the seat-level
    ``usable_roles`` key (falling back to the legacy passed-only ``roles`` for records that
    predate it). Each floor gates independently when given and is vacuously satisfied when
    not (review-cli#246), so an explicit floor can never be silently outvoted by the other.
    Role coverage waives ``min_iter`` whenever a role floor is in force: a 1-model pad is one
    iteration covering 3 roles, and ANDing ``min_iter=3`` made ``passed`` false and fired
    the ship-review-quorum hatch. Model-only mode still requires ``min_iter``, and zero
    iterations still fail (empty roles).
    """
    models = _distinct_models(history.gate_iterations)
    roles = None
    if floors.min_roles is not None:
        roles = _seat_credited_roles(
            _iterations_excluding_mismatched(
                history.iterations, request.repo_id, request.diff_files
            )
        )
    iter_ok = len(history.gate_iterations) >= request.min_iter
    models_ok = floors.min_models is None or len(models) >= floors.min_models
    roles_ok = floors.min_roles is None or (
        roles is not None and len(roles) >= floors.min_roles
    )
    met = models_ok and roles_ok and (iter_ok or floors.min_roles is not None)
    return _Gate(models=models, roles=roles, met=met)


def _advisory_applies(request: QuorumRequest, history: QuorumHistory) -> bool:
    """Whether the non-blocking ``min_models_advisory`` is surfaced (review-cli#246).

    Only for an EXPLICIT ``min_models`` (the pre-substitution flag: the bare-check fallback
    also leaves a model floor set, but there nothing was explicit and role coverage is
    exactly what is unavailable), and only when the gate was evaluated against real history:
    an unreadable store or zero recorded iterations turn into an error result, and a nudge
    that the floor may not be needed would contradict it.
    """
    return (
        request.min_models_explicit
        and history.store_error is None
        and bool(history.iterations)
    )


def _min_roles_suggestion(
    request: QuorumRequest, history: QuorumHistory, floors: FloorResolution, gate: _Gate
) -> str | None:
    """Point a short model-diversity denial at ``--min-roles`` only when it would pass.

    review-cli#221: when ``--min-models`` governs (no role floor), the bar is missed
    because model diversity itself is short, and the roles ``--min-roles {min_models}``
    would see already cover the floor, suggest switching: a model reused onto several roles
    (PR #207) counts per role there although ``_distinct_models`` collapsed it. Never a
    hint that sends the caller into a second denial (round-1 finding): the iteration floor
    must already be met. Never for a monoculture (round-2/3 findings): at least 2 distinct
    models must stand behind the ROLE-BEARING iterations (``_models_behind_role_coverage``),
    not merely among all passed ones, or one model could self-authorize a merge through an
    unrelated role-less iteration. Gated on the explicit flag, not on ``min_models`` being
    set, because the bare-check fallback also leaves that true and no role data exists there.
    """
    if (
        floors.min_roles is not None
        or not request.min_models_explicit
        or gate.met
        or history.store_error is not None
        or not history.iterations
    ):
        return None
    min_models = request.min_models
    if min_models is None:
        return None
    potential_roles = _distinct_roles(history.gate_iterations)
    role_bearing_models = _models_behind_role_coverage(history.gate_iterations)
    if (
        len(role_bearing_models) >= 2
        and len(gate.models) < min_models
        and len(history.gate_iterations) >= request.min_iter
        and len(potential_roles) >= min_models
    ):
        return (
            f"{min_models} distinct models required, only {len(gate.models)} found. If "
            "review passes duplicated a model onto multiple roles (see PR #207), "
            f"replace --min-models {min_models} with --min-roles {min_models} "
            "(review-cli#246: keeping both now requires BOTH to pass), which "
            f"covers {len(potential_roles)} distinct roles rather than unique "
            "model names."
        )
    return None


def _active_cooldown_any_method(model: str) -> dict | None:
    """The first active cooldown of ``model`` under any known access method, else None.

    The store is keyed by (model, access_method) (review-cli#153/#159/#179) and this
    diagnostic does not know which transport a model dispatches through (claude's cli/api
    split, opencode's own bucket), so it probes them all. A model realistically cools down
    under one method at a time, so "first found" and "most severe" coincide in practice.
    """
    for access_method in _seat_cooldown.ACCESS_METHODS:
        cd = _seat_cooldown.active_cooldown(model, access_method=access_method)
        if cd is not None:
            return cd
    return None


def _stalled_models(iterations: list[dict], passed: bool) -> list[dict]:
    """Models this task attempted that are CURRENTLY cooling down (review-cli#221), when the
    bar is not met: an unavailable sentinel or a session-limit/usage-credits notice (a plain
    timeout starts no cooldown). A bare "2/3 models" count leaves an operator guessing which
    seat is the problem. Scoped to models the task code actually tried (ALL recorded
    iterations, not just passed ones) so an unrelated seat is never listed; sourced from
    ``seat_cooldown``, the same live signal dispatch itself checks. Independent of
    diff-identity, so it is computed whether or not a check context was given.

    One cooldown-store read per attempted model: negligible for a CLI check, but a real N+1
    if this ever runs in a loop over many task codes (a batch ``active_cooldowns(models)`` in
    ``seat_cooldown`` would fix it).
    """
    if passed:
        return []
    stalled = []
    for model in _distinct_models(iterations):
        cd = _active_cooldown_any_method(model)
        if cd is not None:
            stalled.append(
                {
                    "model": model,
                    "reason": cd["reason"],
                    "remaining_seconds": round(cd["remaining_seconds"]),
                    "consecutive_failures": cd["fail_count"],
                }
            )
    return stalled


def _near_miss_twin(
    result: dict, clean: str | None, iterations: list[dict]
) -> str | None:
    """The other spelling of an all-digit GitHub-issue task code that has recorded
    iterations, for a failed check (review-cli#389). The dual-key READ already counted
    those records; this names the leftover spelling split on the refusal payload."""
    if result["passed"] or not clean:
        return None
    twin = _github_issue_twin(clean)
    if twin is not None and any(item.get("task_code") == twin for item in iterations):
        return twin
    return None


def _has_task_code(items: list[dict], code: str) -> bool:
    return any(item.get("task_code") == code for item in items)


def _exclusion_error(excluded: int, clean: str | None, foreign_only: bool) -> str:
    """The human-facing error for a bar that was not met after some history was excluded."""
    why = (
        "recorded in another repository (see foreign_repo_iterations)"
        if foreign_only
        else "recorded repo/diff did not match the code currently being checked "
        "(see mismatch_details)"
    )
    return f"{excluded} recorded iteration(s) for {clean} were excluded: {why}"


def _finalize_quorum_result(
    result: dict, *, request: QuorumRequest, history: QuorumHistory
) -> None:
    """Complete an in-progress quorum result: the diff-identity diagnostics (only when a
    check context was given -- omitted entirely otherwise, so a caller that never opts in
    sees the exact pre-v4 shape), then the terminal ``passed``/``error`` keys in fail-closed
    priority order: an unreadable store, then zero history, then (only if the bar still
    isn't met) surfacing that some history was excluded as mismatched -- mutates ``result``
    in place.

    The COUNTS (``excluded_*``) are always the true totals; only the detail LISTS are capped
    at ``_MISMATCH_DETAILS_CAP``, so a task with thousands of polluted iterations (the
    HYP-858 shape this feature targets) cannot balloon ``--check --json`` into a multi-MB
    payload -- the count is enough for a machine gate and a human debugging has ``--detail``.

    Deliberately does NOT special-case the review-cli#252 follow-up's
    ``role_tracking_gap`` (case (b), "not genuinely old" -- see `quorum_check`'s
    docstring): that message is surfaced as its own additive ``result`` key and a
    printed note/hint line, the same treatment ``quorum_mode_fallback`` itself
    already gets, rather than promoted into this ``error`` slot -- promoting it
    would silently move the primary NOT-met line from stdout to stderr for the
    ordinary "task genuinely doesn't have enough role-tracked reviews yet" shape
    (no different from an ordinary "2/3 distinct roles" shortfall, which also
    never becomes an "error" here), churning stream expectations no other
    shortfall-only denial disturbs."""
    mismatched = history.mismatched
    foreign_total = None
    if request.repo_id is not None:
        result["verified_iterations"] = len(history.verified)
        result["unverifiable_iterations"] = len(history.unverifiable)
        result["excluded_mismatched_iterations"] = len(mismatched)
        result["mismatch_details"] = mismatched[:_MISMATCH_DETAILS_CAP]
        if len(mismatched) > _MISMATCH_DETAILS_CAP:
            result["mismatch_details_truncated"] = True
        foreign, foreign_total = _foreign_repo_iterations(
            history.iterations, request.repo_id
        )
        result["excluded_foreign_repo_iterations"] = foreign_total
        result["foreign_repo_iterations"] = foreign
        if foreign_total > len(foreign):
            result["foreign_repo_iterations_truncated"] = True
    # No diff compared (``diff_files is None``; review-cli#589): the only exclusions are
    # other repositories' records of ANY verdict -- the population `foreign_repo_iterations`
    # names and the stderr warning counts.
    foreign_count = foreign_total if request.diff_files is None else None
    excluded = len(mismatched) if foreign_count is None else foreign_count
    if history.store_error is not None:
        result["passed"] = False
        result["error"] = history.store_error
    elif not history.iterations:
        result["passed"] = False
        result["error"] = f"no recorded review iterations for {history.clean}"
    elif excluded and not result["passed"]:
        # Bar not met AND some history was excluded -- surface WHY in the human-facing
        # error too (not just the diagnostic keys), so an operator staring at "0/3
        # iterations" understands it isn't simply "never reviewed".
        result["error"] = _exclusion_error(
            excluded, history.clean, foreign_count is not None
        )


def quorum_check(
    task_code: str,
    *,
    min_iter: int,
    min_models: int | None = None,
    min_roles: int | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
) -> dict:
    """Compute the quorum verdict for one task: N PASSED iterations across M distinct
    models (self-merge-authority gate; CTO decision tg#7306 #1) and/or across M distinct
    BOARD ROLES (review-cli#221) — the role floor governs whenever ``min_roles`` is
    either explicitly given OR defaulted-in (review-cli#246, see below); see the
    explicit-vs-default semantics below for exactly when each floor governs.

    Only iterations whose record has ``passed is True`` count toward ``min_iter``, and
    ``min_models`` is the distinct-model count among those SAME passed iterations — a
    model that only ever failed/degraded does not help satisfy model diversity either.
    A record with no ``passed`` key is verdict UNKNOWN (either pre-STATS_VERSION-3
    history, or a current record from a mode with no verdict to thread, e.g. `qa`) and
    is deliberately treated as not-passed: unverdicted iterations — old OR current —
    can never satisfy this gate, only verdict-tagged passed runs can. This is
    fail-closed by design, not a bug — see record_run's ``passed`` param.

    FIXED (review-cli#137; was the exact-board seat-crediting gap Alex hit concretely —
    Opus review OK, Codex/GLM/Grok/Sonnet quota-exhausted, unable to record any passing
    iteration): role credit for ``min_roles`` below is now SEAT-granular, not run-
    granular. A single hard-failed sibling seat (nonzero returncode from a quota-
    exhausted/dead-key/timed-out provider) no longer zeroes out role credit for a
    genuinely-successful seat in the SAME run — the role floor is computed from
    ``_seat_credited_roles`` (see its own docstring), which reads the seat-granular
    ``usable_roles`` key across every non-diff-identity-mismatched iteration of ANY
    verdict, falling back to the legacy passed-only ``roles`` reading only for records
    written before this fix existed. ``min_iter``/``min_models``/``passed`` themselves
    are UNCHANGED — still the run's own all-or-nothing verdict, exactly as described
    above.

    ``repo_id``/``diff_files`` (both optional) are the CURRENT check context — "what
    repo/diff are we deciding whether to merge right now" — and enable diff-identity
    verification (v4, see the module docstring): a passed iteration whose OWN recorded
    ``repo_id`` differs, or whose recorded ``diff_files`` shares no file with the
    current ``diff_files``, is EXCLUDED from ``passed_iterations``/``models`` rather
    than silently counted (this is what closes the three real quorum-pollution
    incidents this field exists for). Iterations with no recorded identity (pre-v4,
    or a run with no resolvable repo) are "unverifiable" and still count, preserving
    the old behavior for history that predates this field. When ``repo_id`` is None
    (the caller has no check context to give — direct library callers that omit it,
    exactly as before this parameter existed), NO verification is attempted: no
    mismatch/unverifiable keys, every passed iteration counts. Passing a check context
    is what turns the gate on. ``repo_id`` with ``diff_files=None`` checks the
    repository alone (the CLI's ``--verify-repo-only``, review-cli#589): same-repo
    iterations count whatever files they touched, other repositories' do not, and
    ``foreign_repo_iterations`` names every one excluded that way, of any verdict.
    (review-cli#221: independently of ``repo_id``, a
    ``stalled_models`` key is added whenever the bar ISN'T met and an attempted model
    is currently cooling down — this is the one exception to "identical to pre-v4
    shape when passing," since it doesn't depend on diff-identity at all.)

    ``min_roles`` (optional, review-cli#221) switches the SECOND half of the gate from
    counting distinct model-name strings to counting distinct board ROLES credited
    SEAT-granularly (review-cli#137 — see ``_seat_credited_roles``) across every non-
    diff-identity-mismatched iteration, regardless of that iteration's own ``passed``
    — a role filled by the board's shortage-resilience duplicate-
    model pad (a model reused onto an otherwise-empty role slot, see
    ``config.select_pool_with_reuse`` / PR #207) counts once per role there, exactly
    like any other role, instead of collapsing into the model string ``_distinct_models``
    already counted. Only a role that is an actual ``REVIEW_ROLES`` key counts (an
    unknown/typo'd role string is kept for its own diagnostics but earns no lens
    credit). Iterations from a mode with no role concept (quorum / just-ask /
    brainstorm / qa — only the review-diff/visual board dispatch records per-seat
    roles today) contribute no roles, same fail-closed-by-omission treatment as an
    unverdicted iteration.

    ``min_models`` and ``min_roles`` semantics (review-cli#246, fixing a review-cli#221
    round-3 finding — see below): both parameters default to ``None``, which means
    "the caller did not ask for this floor" — NOT "the caller asked for a 0/low value".
    This distinction is what lets the function tell an explicit request apart from a
    default, the same way the CLI's own argparse layer already could (see
    ``reviewlib.cli``'s ``--check`` handling) but couldn't PASS DOWN before this fix,
    since a plain ``int`` default here was indistinguishable from an explicit int of
    the same value.

      * Only ``min_models`` explicitly given (``min_roles`` omitted): reproduces the
        exact pre-#221 PASS/FAIL semantics — a strict distinct-model-name count
        decides ``passed`` (no ``roles``/``distinct_roles_passed``/``min_roles`` keys
        in the result). The result SHAPE gains one new key here (review-cli#246's
        ``min_models_advisory``, see below) — the gate's decision logic is unchanged,
        the payload is not byte-identical.
      * Only ``min_roles`` explicitly given (``min_models`` omitted): reproduces the
        pre-#246 ``--min-roles`` pass/fail behavior — distinct BOARD ROLES decide
        ``passed``. ``distinct_models_passed``/``models`` are still computed/reported
        for visibility (no ``min_models`` key itself, since no floor was requested).
      * BOTH explicitly given: an explicitly requested floor is now ALWAYS enforced —
        ``passed`` requires the model floor AND the role floor (Alex's policy: "if a
        model limit is explicitly set, it must work"). When ``min_roles`` is set
        (explicitly or by the bare-check default below), role coverage waives
        ``min_iter``: one padded iteration covering N roles is enough. ``min_iter``
        still gates model-only mode (``min_roles is None``). This closes the
        round-3 finding below.
      * NEITHER given (the true default — no flags at all): the default switches to a
        ROLE-based check at ``_DEFAULT_QUORUM_FLOOR`` (the SAME numeric value
        ``--min-models`` used to default to), not a model-count check — Alex's
        direction that role-based counting is the new default everywhere, with no
        default model-count floor; an explicit model-count request is still always
        honored per the bullet above. FALLBACK (Alex's review-cli#246 follow-up,
        NARROWED in review-cli#252 — see "Bare-check zero-role fallback" and
        "Genuinely-old vs. merely-mode-avoiding" below): this role-based default
        only applies when the task has AT LEAST ONE role-tagged iteration
        ANYWHERE in its non-mismatched history (any verdict — passed, failed, or
        unverdicted); a task with genuinely ZERO role-tagged iterations, ALL of
        it recorded as predating role-tracking, falls back to the OLD
        model-counting default instead of a guaranteed hard fail. A task with
        zero role-tagged iterations that is NOT provably old (case (b) below)
        does NOT get that fallback — it stays role-based and fails, with a
        `role_tracking_gap` explanation instead.

    Bare-check zero-role fallback (review-cli#246 follow-up; Alex's explicit
    request after shipping the role-based default above): a task whose ENTIRE
    review history predates role-tracking (pre-#246), or whose history comes only
    from ``review quorum``/``review just-ask``/``review brainstorm``/``review qa``
    (modes that never record roles at all — see `record_run`'s ``roles`` param
    docstring), has ZERO role-tagged iterations. Treating that identically to
    "recorded some roles, just short of the floor" is a hard, undeserved fail even
    when the task genuinely has 3+ distinct MODELS in its history that would have
    satisfied the pre-#246 model-counting default. So: in the bare-check default
    path ONLY (neither ``min_models`` nor ``min_roles`` explicitly given), whenever
    the store is readable AND the task has at least one recorded iteration of ANY
    kind (same ``store_error is None and iterations`` guard the sibling
    ``min_models_advisory``/``min_roles_suggestion`` already use — a task with NO
    history at all keeps the pre-existing role-based fail-closed shape; "never
    reviewed" and "reviewed, but never through a role-recording mode" are
    different problems), the task's history is checked for role data — every
    recorded iteration REGARDLESS OF VERDICT (passed, failed, or unverdicted), but
    EXCLUDING any diff-identity-MISMATCHED iteration (a role-tagged review of a
    genuinely different repo/diff under a reused task code must not force THIS
    check into role-mode — see ``_iterations_excluding_mismatched``). This is
    deliberately a WIDER population than ``gate_iterations`` (PASSED-only): a
    role-tagged iteration that FAILED is still real evidence the task went through
    a role-recording review mode, and must not be invisible to this check just
    because it didn't pass. The fallback fires ONLY when this non-mismatched
    population is itself non-empty, carries no role data, AND every iteration in
    it is recorded as predating role-tracking (case (a) — see "Genuinely-old vs.
    merely-mode-avoiding" below for the case (b) exception, where at least one
    iteration is NOT provably old and the fallback must NOT fire). A task whose
    ENTIRE history is diff-identity-mismatched (e.g. a task-code collision with a
    genuinely different repo) is treated the same as "no effective history for
    this check" and keeps the pre-existing role-based fail-closed shape, rather
    than swapping to a model-counting shape with a message that would falsely
    claim no role data exists at all. When it DOES fire, the default silently
    swaps from role-counting to the old model-counting check at the same numeric
    floor — ``result`` ends up with a ``min_models``/``distinct_models_passed``
    shape (no ``min_roles``/``roles`` keys) exactly as if ``--min-models`` had been
    given explicitly, PLUS a ``quorum_mode_fallback`` key explaining why AND a
    ``min_models_source`` key set to ``"fallback"`` (vs. ``"explicit"`` for a
    genuine ``--min-models`` request) — ``"min_models" in result`` alone is NOT a
    reliable "was this explicit" signal anymore; ``min_models_source`` is the
    positive discriminator (``quorum_mode_fallback``'s presence/absence works too,
    but is a secondary/derived signal).
    A task with even ONE role-tagged iteration of ANY verdict in its
    non-mismatched history — regardless of whether the role floor is actually
    met — never falls back: it stays role-based and must pass or fail on that
    basis alone, same as PR #246 left it. This fallback decision is INDEPENDENT
    of ``min_models_explicit``/``min_roles_explicit`` (both stay False in the
    result) and never fires when either floor is explicitly given.

    Genuinely-old vs. merely-mode-avoiding (review-cli#252 follow-up; Alex's
    explicit request after PR #252 shipped the fallback above with no such
    distinction): "zero role-tagged iterations" was being treated as one single
    case, but it actually covers two very different situations, and only one of
    them deserves the free pass above:

      * Case (a), genuinely old — EVERY iteration in ``role_data_population``
        provably predates ``_ROLE_TRACKING_CUTOFF`` (PR #246's merge instant, the
        exact moment a ``roles`` key first became possible to write at all — see
        `_all_iterations_predate_role_tracking`). Nothing more can ever be done
        about this specific history; falling back to model-counting is correct
        and permanent. This is the ONLY case that reaches the behavior described
        above (``quorum_mode_fallback`` fires, ``min_models``/``min_roles`` swap).
      * Case (b), not genuinely old — at least one iteration in
        ``role_data_population`` ran AFTER role-tracking became possible, yet it
        used a role-blind mode (``quorum``/``just-ask``/``brainstorm``/``qa``)
        instead of the role-tracking ``review diff``. Silently granting the same
        model-counting free pass here would let ANY task dodge the role-based
        gate forever, permanently, just by always choosing quorum/brainstorm over
        diff mode — not a one-time migration accommodation for old history, an
        indefinite escape hatch. So case (b) does NOT swap counting mode:
        ``min_models``/``min_roles`` are left exactly as the bare-check default
        set them (role-based, at ``_DEFAULT_QUORUM_FLOOR``), which fails on its
        own merits (``roles`` is empty, same "N/M distinct roles" ratio a task
        with genuinely insufficient — but present — role coverage would print).
        What's ADDED is ``result["role_tracking_gap"]``, a positive discriminator
        (same convention as ``quorum_mode_fallback``) naming exactly what's
        missing and how to fix it — "run ``review diff --task CODE`` instead of
        quorum/just-ask/brainstorm" — printed as an extra note/hint line
        alongside the ratio, not a replacement for it: this is deliberately the
        SAME stdout/stderr treatment as an ordinary role-count shortfall (never
        promoted to ``result["error"]``), so it doesn't quietly move where an
        otherwise-identical "not enough role-tracked reviews yet" denial prints
        just because this specific variant has a name for why.

      Design note on WHERE this lives: a ``--check`` invocation is a fast,
      side-effect-free status query (typically called from an automated ship
      pipeline) — auto-triggering a brand-new multi-model review run as a side
      effect of asking "is the bar met" would violate that contract and could
      turn a status check into a multi-minute, backend-call-spending operation
      the caller never asked for. So this function only ever produces a precise
      diagnosis; actually running the missing role-tracked review is left to the
      caller (a human, or ``gh ship``'s own retry loop) once the
      ``role_tracking_gap`` message tells them what to run.

    FIXED (review-cli#246; was a KNOWN, DELIBERATE trade-off per a Fable round-3 review
    finding on review-cli#221): a caller passing BOTH floors explicitly — e.g.
    ``min_models=5, min_roles=1`` — used to silently let ``min_roles`` govern alone,
    so a task with only 1 distinct model could get ``passed: True`` even though the
    caller explicitly asked for 5. The AND logic above closes that: an explicit
    ``min_models`` can no longer be silently outvoted by ``min_roles`` (or the reverse).

    Whenever ``min_models`` is explicitly given AND there is real history to evaluate
    it against (the store is readable AND at least one iteration is recorded for the
    task — met, not-met, and a diff-identity-mismatch denial all qualify), the result
    also carries a non-blocking ``min_models_advisory`` key (review-cli#246) — Alex's
    policy allows enforcing an explicit model floor while still nudging the caller
    that role-based coverage is usually enough on its own; the advisory never affects
    ``passed``. It is NOT added to any of the fail-closed shapes below that carry NO
    real history (invalid task code, unreadable store, zero recorded iterations, or a
    floor-validation failure) — matching the pre-existing ``min_roles_suggestion``
    convention exactly (same ``store_error is None and iterations`` guard).

    Fail-closed (independent of the above): an invalid task code, an unreadable/missing
    store, or zero recorded iterations for the code all yield ``passed: False`` plus an
    ``"error"`` key explaining why — the caller must never treat "no data" as "quorum
    met". A ``min_iter`` floor below 1, or an explicitly given ``min_models``/
    ``min_roles`` below 1, is also rejected the same way — 0 would trivially satisfy a
    floor via ``0 >= 0`` even for a task with zero passed iterations, undermining the
    whole point of this gate. Validated HERE (not only in the CLI wrapper) so a direct
    library caller can't bypass the floor either.
    """
    request = build_request(
        task_code, min_iter, min_models, min_roles, repo_id, diff_files
    )
    clean, error = check_inputs(request)
    if error is not None:
        return rejected_result(request, error)
    history = load_history(clean, request)
    floors = resolve_floors(request, history)
    gate = _evaluate_gate(request, history, floors)
    result = {
        "task_code": clean,
        "passed_iterations": len(history.gate_iterations),
        "total_iterations": len(history.iterations),
        "distinct_models_passed": len(gate.models),
        "models": gate.models,
        "min_iter": min_iter,
        "passed": gate.met,
    }
    if floors.min_models is not None:
        result["min_models"] = floors.min_models
        # A direct, POSITIVE discriminator (Fable review): an explicit request and the
        # bare-check fallback produce the identical `min_models` shape, and telling them
        # apart by the ABSENCE of `quorum_mode_fallback` is easy to get wrong.
        result["min_models_source"] = (
            "fallback" if floors.mode_fallback is not None else "explicit"
        )
    if floors.mode_fallback is not None:
        # Distinguishes the silent bare-check swap-to-model-counting from a genuine
        # explicit `--min-models` request. Present ONLY when the fallback fired.
        result["quorum_mode_fallback"] = floors.mode_fallback
    if floors.tracking_gap is not None:
        # review-cli#252 follow-up: the positive, machine-readable discriminator for case
        # (b) -- a caller like `gh ship` parsing --json keys on its presence. Never
        # promoted to `result["error"]` (see `_finalize_quorum_result`).
        result["role_tracking_gap"] = floors.tracking_gap
    if gate.roles is not None:
        result["distinct_roles_passed"] = len(gate.roles)
        result["roles"] = gate.roles
        result["min_roles"] = floors.min_roles
    if _advisory_applies(request, history):
        result["min_models_advisory"] = _MIN_MODELS_ADVISORY
    suggestion = _min_roles_suggestion(request, history, floors, gate)
    if suggestion is not None:
        result["min_roles_suggestion"] = suggestion
    stalled = _stalled_models(history.iterations, result["passed"])
    if stalled:
        result["stalled_models"] = stalled
    _finalize_quorum_result(result, request=request, history=history)
    twin = _near_miss_twin(result, clean, history.iterations)
    if twin is not None:
        # A failed check names the key searched and the near-miss spelling. `iterations`
        # is the UNFILTERED dual-key read, so a twin sitting in it proves nothing about
        # counting: expose whether the twin has a record in `gate_iterations` (the
        # population the quorum math used) and, separately, whether it ever passed (a
        # passed-but-mismatched twin gets different wording in `_quorum_check_subcommand`).
        # `passed_iterations` minus `gate_iterations` is exactly `mismatched` by
        # construction (the bucket sort partitions every passed iteration into exactly one
        # of verified/mismatched/unverifiable), which makes that wording a provable claim.
        result["searched_key"] = clean
        result["near_miss_keys"] = [twin]
        result["near_miss_counted"] = _has_task_code(history.gate_iterations, twin)
        result["near_miss_ever_passed"] = _has_task_code(
            history.passed_iterations, twin
        )
    return result
