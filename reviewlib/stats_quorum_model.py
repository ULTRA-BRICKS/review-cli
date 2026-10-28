"""The inputs and the history one quorum check works from (review-cli#721 split of ``reviewlib.stats``).

``QuorumRequest`` is what the caller asked for with the bare-check default floor already
applied (and a record of which floors were EXPLICIT, since an explicit floor is never
second-guessed); ``QuorumHistory`` is the task's recorded iterations split into the
buckets the gate counts. ``rejected_result`` is the early-reject payload for an invalid
request. The gate itself is ``reviewlib.stats_quorum.quorum_check``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stats_identity import normalize_task_code
from .stats_quorum_evidence import _sort_passed_iterations_into_buckets
from .stats_store import iterations_for_task, stats_path

_DEFAULT_QUORUM_FLOOR = 3  # the "at least 3 distinct opinions" floor -- shared by
# --min-models' historical default AND --min-roles' new default below (review-cli#246).


@dataclass(frozen=True)
class QuorumRequest:
    """One ``quorum_check`` call, normalized.

    ``min_models`` is exactly as the caller gave it. ``min_roles`` is the floor in force:
    the caller's, or ``_DEFAULT_QUORUM_FLOOR`` for a bare check (neither floor given), which
    the history may swap back to model counting (see ``stats_quorum_fallback``).
    ``*_explicit`` are captured BEFORE any default is substituted: a floor the caller
    typed is never second-guessed, and a message must not claim one the caller did not.
    """

    task_code: str
    min_iter: int
    min_models: int | None
    min_roles: int | None
    min_models_explicit: bool
    min_roles_explicit: bool
    bare_default: bool
    repo_id: str | None
    diff_files: list[str] | None


@dataclass(frozen=True)
class QuorumHistory:
    """A task's recorded iterations and how the gate counts them.

    ``passed_iterations`` are the ``passed is True`` ones (an unverdicted record is
    fail-closed not-passed); ``gate_iterations`` are those that survive diff-identity
    verification (``verified`` + ``unverifiable``); ``mismatched`` are the excluded ones,
    as detail dicts. ``store_error`` is set when the store cannot be read at all.
    """

    clean: str | None
    store_error: str | None
    iterations: list[dict]
    passed_iterations: list[dict]
    verified: list[dict]
    mismatched: list[dict]
    unverifiable: list[dict]
    gate_iterations: list[dict]


def build_request(
    task_code: str,
    min_iter: int,
    min_models: int | None,
    min_roles: int | None,
    repo_id: str | None,
    diff_files: list[str] | None,
) -> QuorumRequest:
    """Normalize a call: capture which floors were explicit, then apply the bare default.

    Neither floor given -> role-based coverage at ``_DEFAULT_QUORUM_FLOOR`` (Alex,
    review-cli#246): the same "at least 3 distinct opinions" intent ``--min-models`` used to
    default to, counted by board role. TENTATIVE: the history can swap it back to model
    counting for a task with no role data that provably predates role-tracking.
    """
    min_models_explicit = min_models is not None
    min_roles_explicit = min_roles is not None
    bare_default = not min_models_explicit and not min_roles_explicit
    return QuorumRequest(
        task_code=task_code,
        min_iter=min_iter,
        min_models=min_models,
        min_roles=_DEFAULT_QUORUM_FLOOR if bare_default else min_roles,
        min_models_explicit=min_models_explicit,
        min_roles_explicit=min_roles_explicit,
        bare_default=bare_default,
        repo_id=repo_id,
        diff_files=diff_files,
    )


def _floor_violation(request: QuorumRequest) -> str | None:
    """The error for a floor below 1, or None. Only floors that were part of the input
    are named (Opus round-3 finding): a bare ``min_iter`` violation must not print
    ``min_models=None``, and the default ``min_roles`` must not be claimed as typed."""
    if (
        request.min_iter >= 1
        and (request.min_models is None or request.min_models >= 1)
        and (request.min_roles is None or request.min_roles >= 1)
    ):
        return None
    clauses = [f"min_iter={request.min_iter}"]
    if request.min_models is not None:
        clauses.append(f"min_models={request.min_models}")
    if request.min_roles_explicit:
        clauses.append(f"min_roles={request.min_roles}")
    return (
        "min_iter must be >= 1, and min_models/min_roles (when explicitly given) "
        f"must also be >= 1 (got {' '.join(clauses)})"
    )


def check_inputs(request: QuorumRequest) -> tuple[str | None, str | None]:
    """``(clean task code, None)`` for a valid request, ``(None, error)`` for an invalid
    one. A blank task code is valid and comes back as ``(None, None)``."""
    error = _floor_violation(request)
    if error is not None:
        return None, error
    try:
        return normalize_task_code(request.task_code), None
    except ValueError as exc:
        return None, f"invalid task code: {exc}"


def rejected_result(request: QuorumRequest, error: str) -> dict:
    """The payload for a request rejected before the store was read."""
    result: dict = {
        "task_code": request.task_code,
        "passed_iterations": 0,
        "total_iterations": 0,
        "distinct_models_passed": 0,
        "models": [],
        "min_iter": request.min_iter,
        "passed": False,
        "error": error,
    }
    if request.min_models is not None:
        result["min_models"] = request.min_models
        # `min_models_source` must accompany `min_models` on EVERY shape that carries it
        # (Opus/Fable round-review finding). Derived from the explicit flag rather than
        # hardcoded: a rejection happens before any fallback, so it is always "explicit"
        # today, and deriving keeps that an enforced fact if the order ever changes.
        result["min_models_source"] = (
            "explicit" if request.min_models_explicit else "fallback"
        )
    if request.min_roles is not None:
        result["distinct_roles_passed"] = 0
        result["roles"] = []
        result["min_roles"] = request.min_roles
    return result


def _store_unreadable_error() -> str | None:
    """Return an error string if the stats store cannot be read, else None.

    Distinguishes "store missing/unreadable" from "store readable but the task has
    zero records" so quorum_check can report a more useful message than a bare
    empty result in both cases. Never raises.
    """
    try:
        stats_path().read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — reporting only, never abort a check
        return f"stats store unreadable: {exc}"
    return None


def load_history(clean: str | None, request: QuorumRequest) -> QuorumHistory:
    """Read the task's iterations and bucket the passed ones against the check context."""
    store_error = _store_unreadable_error()
    iterations = iterations_for_task(clean) if clean else []
    # Fail-closed: a record with no "passed" key (pre-v3, verdict unknown) is NOT
    # passed -- `is True` (not truthy) so it never accidentally matches None/missing.
    passed_iterations = [item for item in iterations if item.get("passed") is True]
    verified, mismatched, unverifiable = _sort_passed_iterations_into_buckets(
        passed_iterations, request.repo_id, request.diff_files
    )
    # Gate-worthy = verified (identity checked out) + unverifiable (no identity to
    # check, so unchanged from pre-v4 behavior) -- mismatched iterations are excluded.
    return QuorumHistory(
        clean=clean,
        store_error=store_error,
        iterations=iterations,
        passed_iterations=passed_iterations,
        verified=verified,
        mismatched=mismatched,
        unverifiable=unverifiable,
        gate_iterations=verified + unverifiable,
    )
