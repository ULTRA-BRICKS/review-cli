"""Admission-aware run statistics, retaining repository identity and role evidence.

The mode's exit status remains its technical-success signal, not a new content
approval classifier. QA is report-only, so its verdict is unknown here and never
silently counts as a passed review. Per-seat usable roles remain independent of
whole-panel success; explicit model/iteration gates keep their existing policy.
"""
from __future__ import annotations


def _print_feedback_nudge(_api, /, run_id: str) -> None:
    if _api.os.environ.get("REVIEW_NO_FEEDBACK_NUDGE"):
        return
    print(f"[review] run recorded (run_id={run_id}). Rate it: "
          f'review feedback {run_id} --useful yes|no [--note "..."] [--ticket ID]',
          file=_api.sys.stderr)


def _run_mode_with_stats(_api, /, mode: str, pool_models: list[str], dispatch,
    models_after, roles_after, usable_roles_after, *, task_code: str | None,
    repo_id: str | None, diff_files: list[str] | None, diff_sha256: str | None,
    needs_slot: bool, direct_seats: bool, later_models: list[str] | None,
    level: str | None) -> int:
    """Queue time is deliberately excluded from duration and subsequent ETAs."""
    slot = _api.contextlib.nullcontext()
    if needs_slot:
        slot = _api.admission.run_slot(mode=mode,
            models=pool_models if direct_seats else _api.admission.routed(pool_models),
            later_models=_api.admission.routed(later_models or []), task_code=task_code)
    with slot:
        return _api._run_admitted_mode_with_stats(mode, pool_models, dispatch,
            models_after, roles_after, usable_roles_after, task_code=task_code,
            repo_id=repo_id, diff_files=diff_files, diff_sha256=diff_sha256, level=level)


def _callback_value(callback):
    """A failed optional telemetry callback must not change the command's result."""
    if callback is None:
        return None
    try:
        return callback() or None
    except Exception:
        return None


def _run_evidence(pool_models, models_after, roles_after, usable_roles_after):
    actual_models = _callback_value(models_after)
    # Model/role lists are index-paired. Never pair planned models with actual roles
    # after the actual-model callback failed or returned no usable roster (#137).
    actual_roles = _callback_value(roles_after) if actual_models is not None else None
    return (actual_models if actual_models is not None else pool_models,
            actual_roles, _callback_value(usable_roles_after))


def _restore_task_environment(_api, task_code, previous):
    if not task_code:
        return
    if previous is None:
        _api.os.environ.pop("REVIEW_TASK_CODE", None)
    else:
        _api.os.environ["REVIEW_TASK_CODE"] = previous


def _persist_run(_api, *, mode, pool_models, callbacks, task_code, started,
                 elapsed, tally, rc, repo_id, diff_files, diff_sha256, level):
    if not tally["ok"] and not tally["fail"]:
        return  # No actual dispatch: do not poison ETAs with a zero-work record.
    models, roles, usable_roles = _run_evidence(pool_models, *callbacks)
    verdict = None if mode == "qa" else rc == 0
    try:
        clean_task = _api.normalize_task_code(task_code)
    except ValueError:
        clean_task = None
    run_id = _api.run_id_for(started.isoformat(), mode, clean_task)
    recorded = _api.record_run(task_code=task_code, mode=mode, models=models,
        duration_seconds=elapsed, ok_count=tally["ok"], fail_count=tally["fail"],
        started=started, passed=verdict, repo_id=repo_id, diff_files=diff_files,
        diff_sha256=diff_sha256, roles=roles, usable_roles=usable_roles,
        prompt_tokens=tally["prompt_tokens"], output_tokens=tally["output_tokens"],
        run_id=run_id, level=level)
    if recorded:
        _api._print_feedback_nudge(run_id)


def _run_admitted_mode_with_stats(_api, /, mode: str, pool_models: list[str],
    dispatch, models_after, roles_after, usable_roles_after, *, task_code: str | None,
    repo_id: str | None, diff_files: list[str] | None, diff_sha256: str | None,
    level: str | None) -> int:
    """Record actual outcomes after dispatch, including exceptional termination."""
    import time

    _api.announce_eta(mode, len(pool_models))
    _api.begin_call_tally()
    started = _api.datetime.now(_api.timezone.utc)
    clock_start = time.monotonic()
    previous = _api.os.environ.get("REVIEW_TASK_CODE")
    if task_code:
        _api.os.environ["REVIEW_TASK_CODE"] = task_code
    rc = None
    try:
        rc = dispatch()
        return rc
    finally:
        _restore_task_environment(_api, task_code, previous)
        elapsed = time.monotonic() - clock_start
        tally = _api.end_call_tally()
        _persist_run(_api, mode=mode, pool_models=pool_models,
            callbacks=(models_after, roles_after, usable_roles_after),
            task_code=task_code, started=started, elapsed=elapsed, tally=tally, rc=rc,
            repo_id=repo_id, diff_files=diff_files, diff_sha256=diff_sha256, level=level)


def _call_with_task_env(_api, /, task_code: str | None, fn):
    clean_task = _api.normalize_task_code(task_code)
    if not clean_task:
        return fn()
    previous = _api.os.environ.get("REVIEW_TASK_CODE")
    _api.os.environ["REVIEW_TASK_CODE"] = clean_task
    try:
        return fn()
    finally:
        _restore_task_environment(_api, clean_task, previous)
