"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import IdentityMode

def _resolve_quorum_check_context(_api, /, cwd_raw: str, identity_mode: IdentityMode) -> tuple[str | None, list[str] | None, str]:
    """Resolve the (repo_id, diff_files, status) check context for `--check`. See
    `_quorum_check_subcommand`.

    `identity_mode` is ``"full"`` (repo AND touched-file overlap, the default),
    ``"repo_only"`` (`--verify-repo-only`: repo alone, so `diff_files` is None), or
    ``"disabled"`` (`--no-verify-identity`).

    `status` is one of ``"ran"`` (repo_id/diff_files are usable), ``"repo_only"``
    (repo_id is usable, no file-set check), ``"disabled"``
    (`--no-verify-identity`), or ``"skipped_unresolvable"`` (`-C`/`cwd_raw` isn't
    even a real directory — `_compute_repo_id` otherwise always has a `path:`
    fallback). This is a MACHINE-READABLE version of the same distinction the
    stderr warnings below already make for a human — review finding (Fable) on
    this feature's own PR: the original cut only had the stderr text, so a
    machine caller like `gh ship` had no JSON field to assert "verification
    actually ran" against; it could only infer it from the ABSENCE of the
    diagnostic keys, which is indistinguishable from "verification ran and this
    task genuinely has zero passed iterations". `_quorum_check_subcommand` writes
    `status` into the result as `"identity_verification"`.
    """
    if identity_mode == "disabled":
        print(
            "[review task] warning: diff-identity verification disabled "
            "(--no-verify-identity) — every PASSED iteration counts regardless of "
            "recorded repo/diff, exactly as before this gate existed",
            file=_api.sys.stderr,
        )
        return None, None, "disabled"
    cwd = _api._effective_cwd(cwd_raw, warn=False)
    repo_id = _api._compute_repo_id(cwd)
    if repo_id is None:
        print(
            f"[review task] warning: could not resolve a repo at -C {cwd_raw!r} — "
            "diff-identity verification skipped, falling back to legacy counting "
            "(every PASSED iteration counts regardless of recorded repo/diff)",
            file=_api.sys.stderr,
        )
        return None, None, "skipped_unresolvable"
    if identity_mode == "repo_only":
        return repo_id, None, "repo_only"
    return repo_id, _api._current_diff_files_for_check(cwd), "ran"


def _print_quorum_model_audit_line(_api, /, result: dict, stream) -> None:
    """Role mode's headline line reports covered ROLES, not models — print a
    secondary line naming which MODELS actually reviewed, so a self-merge-
    authority audit trail never loses that fact just because roles (not models)
    governed the verdict (Fable review finding, review-cli#221). `stream` matches
    the caller's stream choice (stdout for a plain ratio, stderr for an error)."""
    distinct_models = result["distinct_models_passed"]
    models_str = ", ".join(result["models"]) or "-"
    plural_m = "" if distinct_models == 1 else "s"
    print(
        f"  models: {distinct_models} distinct model{plural_m} ({models_str})",
        file=stream,
    )


def _print_quorum_bar_met(_api, /, result: dict, role_mode: bool, model_mode: bool) -> None:
    """Text-mode 'review bar met' line. `role_mode`/`model_mode` (review-cli#221,
    extended in review-cli#246) select which floor(s) actually governed the
    verdict — the caller derives both from which keys `quorum_check` put in
    `result` (see `_quorum_check_subcommand`), since that reflects the function's
    OWN default-resolution/AND-logic, not the raw CLI flags. Either, or both, can
    be true (review-cli#246: an explicit --min-models alongside --min-roles now
    means BOTH govern)."""
    passed_iter = result["passed_iterations"]
    plural_i = "" if passed_iter == 1 else "s"
    parts = []
    if role_mode:
        distinct = result["distinct_roles_passed"]
        covered_str = ", ".join(result["roles"]) or "-"
        plural_u = "" if distinct == 1 else "s"
        parts.append(f"{distinct} distinct role{plural_u} ({covered_str})")
    if model_mode:
        distinct = result["distinct_models_passed"]
        covered_str = ", ".join(result["models"]) or "-"
        plural_u = "" if distinct == 1 else "s"
        parts.append(f"{distinct} distinct model{plural_u} ({covered_str})")
    print(
        f"review bar met for {result['task_code']}: {passed_iter} passed "
        f"iteration{plural_i} across {' and '.join(parts)}"
    )
    # Fable review finding: role mode's line above drops WHICH MODELS reviewed
    # entirely — for a self-merge-authority audit trail that's the fact worth
    # keeping in the log even when roles (not models) governed the verdict. Only
    # needed when models weren't ALREADY shown in the headline above.
    if role_mode and not model_mode:
        _api._print_quorum_model_audit_line(result)
    if advisory := result.get("min_models_advisory"):
        print(f"  note: {advisory}")
    # Alex's review-cli#246 follow-up: a bare `--check` that fell back to
    # model-counting because the task has zero role-tagged history looks
    # IDENTICAL to an explicit `--min-models` pass otherwise (same result
    # shape, same printed line above) -- this note is the one visible signal
    # that the bar-met floor came from the fallback, not from a real flag.
    if fallback := result.get("quorum_mode_fallback"):
        print(f"  note: {fallback}")


def _quorum_shortfall_header(result, role_mode, model_mode):
    if "error" in result:
        return f"review bar NOT met for {result['task_code']}: {result['error']}"
    clauses = [f"{result['passed_iterations']}/{result['min_iter']} passed iterations"]
    if role_mode:
        clauses.append(f"{result['distinct_roles_passed']}/{result['min_roles']} distinct roles")
    if model_mode:
        clauses.append(f"{result['distinct_models_passed']}/{result['min_models']} distinct models")
    return f"review bar NOT met for {result['task_code']}: {', '.join(clauses)}"


def _stalled_model_line(stalled):
    minutes = max(1, round(stalled["remaining_seconds"] / 60))
    times = stalled["consecutive_failures"]
    plural = "" if times == 1 else "s"
    return (f"  stalled: {stalled['model']} ({stalled['reason']}, "
            f"{times} consecutive failure{plural}, ~{minutes}m until retry-eligible)")


def _print_quorum_bar_not_met(_api, /, result: dict, role_mode: bool, model_mode: bool) -> None:
    """Keep header, audit, hints and stall details on the same result-dependent stream."""
    stream = _api.sys.stderr if "error" in result else None
    print(_quorum_shortfall_header(result, role_mode, model_mode), file=stream)
    if role_mode and result["distinct_models_passed"] > 0:
        _api._print_quorum_model_audit_line(result, stream)
    notes = (("min_models_advisory", "note"), ("quorum_mode_fallback", "note"),
             ("role_tracking_gap", "hint"), ("min_roles_suggestion", "hint"))
    for key, label in notes:
        if result.get(key):
            print(f"  {label}: {result[key]}", file=stream)
    for stalled in result.get("stalled_models", []):
        print(_stalled_model_line(stalled), file=stream)


def _quorum_check_subcommand(_api, /, code: str, min_iter: int, min_models: int | None, *, min_roles: int | None, as_json: bool, cwd_raw: str, identity_mode: IdentityMode) -> int:
    """`review task CODE --check [--min-iter N] [--min-models M] [--min-roles R] [-C DIR]`.

    Exit 0 iff CODE has >= min_iter PASSED recorded iterations AND every EXPLICITLY
    given floor among {min_models, min_roles} is met (review-cli#246: an explicit
    floor is never silently outvoted by the other) — or, when NEITHER is given, >=
    the default distinct BOARD ROLES floor (review-cli#246's new default; see
    quorum_check's docstring for the full default-resolution/AND-logic table). Exit 1
    otherwise, including the fail-closed cases (invalid code, unreadable/missing
    stats store, zero records, or a task whose only history predates the verdict
    field) where reviewlib.stats.quorum_check sets an "error" key or simply comes up
    short. See quorum_check's docstring for the "passed, not just dispatched"
    semantics this check enforces.

    `identity_mode` ``"full"`` (the default) resolves `cwd_raw` (`-C`) to a repo id +
    the current diff's touched-file set and hands both to `quorum_check`, so a PASSED
    iteration recorded against a DIFFERENT repo, or a diff sharing no touched file,
    is EXCLUDED from the count rather than silently trusted — see reviewlib.stats
    "Diff-identity binding" for why (closes 3 real quorum-pollution incidents).
    ``"repo_only"`` (`--verify-repo-only`, review-cli#589) hands over the repo id
    alone: `gh ship` checks a PR from a checkout whose own diff has nothing to do
    with the PR, so only the repository can be compared there.
    ``"disabled"`` (`--no-verify-identity`) restores the pre-binding behavior (every
    passed iteration counts, no repo/diff cross-check) — an escape hatch for a cwd
    that can't resolve a repo, not a way around a genuine mismatch finding.
    """
    repo_id, diff_files, verification_status = _api._resolve_quorum_check_context(
        cwd_raw, identity_mode
    )
    result = _api.quorum_check(
        code,
        min_iter=min_iter,
        min_models=min_models,
        min_roles=min_roles,
        repo_id=repo_id,
        diff_files=diff_files,
    )
    # Machine-readable counterpart to the stderr warnings below: a caller like
    # `gh ship` parsing --json alone (never sees stderr) can assert on this
    # directly instead of inferring "did verification run" from key absence.
    result["identity_verification"] = verification_status
    # This warning goes to stderr in BOTH --json and text mode: --json's structured
    # payload already carries excluded_mismatched_iterations/mismatch_details for a
    # machine reader, but a human watching stderr (e.g. `gh ship`'s own console
    # output) must see the mismatch surfaced too, not just a smaller-than-expected
    # number with no explanation.
    # With no diff compared (`--verify-repo-only`, or a full-mode check whose checkout
    # resolves no diff -- `quorum_check` compares the repository alone either way), the
    # exclusions are other repositories' records only, of any verdict (a failed one
    # still names a review that did not happen here) -- the same count and wording as
    # the JSON `error`. Pointing `gh ship` at --no-verify-identity would be wrong there.
    no_diff_compared = repo_id is not None and diff_files is None
    if no_diff_compared:
        excluded = result.get("excluded_foreign_repo_iterations", 0)
        why = "recorded in another repository (--json lists them in foreign_repo_iterations)"
    else:
        excluded = result.get("excluded_mismatched_iterations", 0)
        why = (
            "recorded repo/diff did not match the code currently being checked "
            "(--json for detail; --no-verify-identity disables this check)"
        )
    if excluded:
        print(
            f"[review task] warning: excluded {excluded} recorded iteration(s) for "
            f"{result['task_code']} — {why}",
            file=_api.sys.stderr,
        )

    # review-cli#389: a failed/empty check whose search key has a same-digits
    # different-'#' twin already IN the store must say so on the refusal itself —
    # both modes (--json's payload already carries searched_key/near_miss_keys for
    # a machine reader, but a human running a plain `review task CODE --check`
    # never opens --json) so the mismatch is diagnosable from the refusal alone,
    # matching the excluded-mismatch warning's same both-modes convention above.
    #
    # Quorum-review round 2 (Grok, flipping a prior CLEAN to NOT CLEAN with a
    # concrete repro): the original cut branched on `excluded` (this task's
    # TOTAL mismatched-iteration count) to pick the wording, but a twin whose
    # ONLY record has `passed=False` never reaches `passed_iterations` at all
    # -- `excluded` stays 0 for it and the "already count above" branch fired
    # anyway, which is false. `near_miss_counted`/`near_miss_ever_passed`
    # (quorum_check, computed against the actual gated/passed populations) are
    # the real signal, so branch on those instead of `excluded`.
    _print_near_miss_explanation(_api, result, no_diff_compared)

    if as_json:
        import json as _json

        print(_json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    # review-cli#246: derived from the RESULT (which floor(s) quorum_check actually
    # applied, after its own default-resolution), not from the raw min_models/
    # min_roles arguments here — those can be None even when quorum_check ends up
    # enforcing a floor anyway (the "neither given" default substitutes min_roles).
    role_mode = "min_roles" in result
    model_mode = "min_models" in result
    if result["passed"]:
        _api._print_quorum_bar_met(result, role_mode, model_mode)
        return 0
    _api._print_quorum_bar_not_met(result, role_mode, model_mode)
    return 1


def _numeric_detail_session(_api, code, selector, iterations, sessions):
    number = int(selector)
    if number <= 0:
        print("[review task] --detail iteration must be >= 1", file=_api.sys.stderr)
        return None, number, 2
    if iterations:
        record = next((item for item in iterations if item.get("iteration") == number), None)
        if record is None:
            print(f"No review iteration {number} found for task {code}.", file=_api.sys.stderr)
            return None, number, 1
        return _api._session_for_task_iteration(record, sessions, iterations), number, 0
    session = sessions[number - 1] if number - 1 < len(sessions) else None
    return session, number, 0


def _resolve_detail_session(_api, code, selector, iterations, sessions):
    if selector.isdigit():
        return _numeric_detail_session(_api, code, selector, iterations, sessions)
    session = next((item for item in sessions if item.session_id == selector), None)
    number = None
    if session is not None:
        number = _api._iteration_for_session(session, iterations) or sessions.index(session) + 1
    return session, number, 0


def _print_detail_call(index, call):
    status = "error" if call.has_error else ("running" if not call.completed else "ok")
    print(f"\n## Call {index}: {call.backend} r{call.round} [{status}] {call.filename}")
    if call.body:
        print(call.body)
    if call.stderr_lines:
        print("\nstderr:")
        print("\n".join(call.stderr_lines))


def _task_detail(_api, /, code: str, selector: str, iterations: list[dict], sessions: list, *, as_json: bool) -> int:
    session, number, error = _resolve_detail_session(_api, code, selector, iterations, sessions)
    if error:
        return error
    if session is None:
        print(f"No conversation logs found for task {code} detail {selector}.", file=_api.sys.stderr)
        return 1
    detail = session.to_detail()
    if as_json:
        import json
        print(json.dumps(detail, indent=2))
        return 0
    label = f"iteration {number}" if number is not None else selector
    print(f"# Task {code} {label}")
    print(f"Session: {session.session_id}")
    print(f"Mode: {session.mode}")
    print(f"Started: {session.started.isoformat()}")
    print(f"Models: {', '.join(session.models) or '-'}")
    if session.brainstorm is not None:
        print("\n## Brainstorm transcript")
        print(session.brainstorm.body.rstrip())
    for index, call in enumerate(session.calls, start=1):
        _print_detail_call(index, call)
    return 0


def _print_near_miss_explanation(_api, result, no_diff_compared):
    if near_miss := result.get("near_miss_keys"):
        near_miss_list = ", ".join(repr(key) for key in near_miss)
        prefix = "review-cli#389 aliases these spellings when reading iterations, "
        if result.get("near_miss_counted"):
            detail = f"{prefix}so they already count above"
        elif result.get("near_miss_ever_passed"):
            # Passed at least once, but diff-identity verification excluded
            # every one of those as a repo/diff mismatch. `mismatch_details`
            # is --json-only (round-2 review finding: the original text
            # pointed a plain-text reader at a field they have no way to see)
            # -- point at the flag that actually gets them there instead.
            detail = (
                f"{prefix}but repository verification excluded the twin's passed "
                "iteration(s) as another repository's above -- re-run with --json "
                "to see foreign_repo_iterations for confirmation"
                if no_diff_compared
                else f"{prefix}but diff-identity verification excluded the twin's "
                "passed iteration(s) as a repo/diff mismatch above -- re-run "
                "with --json to see mismatch_details for confirmation"
            )
        else:
            detail = (
                f"{prefix}but none of the twin's recorded iteration(s) actually "
                "passed, so they do not count above"
            )
        print(
            f"[review task] note: searched key {result['searched_key']!r} — found "
            f"{near_miss_list} in the store (same digits, different '#'); {detail}",
            file=_api.sys.stderr,
        )
