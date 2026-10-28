"""Execute validated CLI modes with admission, deadlines and evidence intact."""
from __future__ import annotations

from .cli_dispatch_state import CONTINUE


def _standalone_visual(_api, state):
    from .features.visual.visual_cli import run_visual_standalone

    slot = _api.contextlib.nullcontext() if state.args.no_ai else _api.admission.run_slot(
        mode="visual", models=state._vision_seat())
    with slot:
        return run_visual_standalone(
            state.args.visual, before=state.args.before, expect=state.args.expect,
            intent=state.args.intent, requested_checks=list(state.args.check),
            models=state.visual_models, no_ai=state.args.no_ai,
            local_model=not state.args.no_local_model and state.config.get("local_model", True) is not False,
            vision_timeout=state.args.vision_timeout, as_json=state.args.json,
            strict=state.args.strict, project=state.args.project or str(state.cwd),
            effort_override=state.effort_override)


def _build_visual_context(_api, state):
    from .features.visual.compose import build_mode_visual_context

    before = _api.Path(state.args.before).expanduser() if state.args.before else None
    return build_mode_visual_context(
        _api.Path(state.args.visual).expanduser(), before=before,
        expect=state.args.expect, intent=state.args.intent,
        models=[] if state.args.no_ai else state.visual_models,
        requested_checks=list(state.args.check), vision_timeout=state.args.vision_timeout,
        require_vision=not state.args.no_ai, effort_override=state.effort_override)


def _visual_refusal(state):
    context = state.visual_ctx
    refusal = 10 if state.args.strict else 1
    if context.prefilter_verdict == "rollback":
        print(f"[review visual] ROLLBACK (pre-filter, mode blocked): {context.prefilter_reason}")
        return 1 if "unreadable" in context.prefilter_reason else refusal
    if context.vision_error:
        print(f"[review visual] UNVERIFIED (mode blocked): {context.vision_error}")
        return 124 if context.vision_timed_out else refusal
    return CONTINUE


def run_visual_companion(_api, state):
    if not state.visual_mode:
        return CONTINUE
    if state.mode.name == "qa":
        print("[review-cli] qa: --visual is not supported (the tester produces its OWN visual proof by driving the SUT). Drop --visual.", file=_api.sys.stderr, flush=True)
        return 2
    if not state.panel_mode and not state.diff.strip():
        return _standalone_visual(_api, state)
    result = state.validate_board()
    if result is not None:
        return result
    if state.just_ask_deadline_seconds is not None:
        state.deadline_armed = _api.admission.hold_in_dispatch(_api.command_deadline(
            state.just_ask_deadline_seconds,
            command=f"review just-ask --timeout {state.just_ask_deadline_seconds}"))
    if not state.args.no_ai:
        _api.admission.hold_run_slot(mode=state.mode.stats_mode, models=state._vision_seat(),
            later_models=_api.admission.routed(state._planned_panel()),
            final_models=_api.admission.routed(state._moderator_seat()), task_code=state.task_code)
    state.visual_ctx = _api._call_with_task_env(state.task_code, lambda: _build_visual_context(_api, state))
    return _visual_refusal(state)


def build_mode_context(_api, state):
    def with_visual_text(text: str) -> str:
        return _api._with_visual(text, state.visual_ctx)

    state.moderators = state._moderators()
    state.ctx = _api.ModeContext(
        args=state.args, models=state.models, diff=state.diff, cwd=state.cwd,
        timeout=state.timeout, with_visual=with_visual_text, visual_ctx=state.visual_ctx,
        moderators=state.moderators,
        effort_override=_api.panel_effort(state.selected_panel, state.effort_override),
        extra={"diff_from_stdin": state.diff_from_stdin, "stamp_diff_hash": state.stamp_diff_hash,
               **({"task_roles": state.panel_task_roles} if state.panel_task_roles else {})})
    return CONTINUE


def _provenance(state):
    return {"task_code": state.task_code, "repo_id": state.repo_id,
            "diff_files": state.diff_files, "diff_sha256": state.diff_sha256}


def dispatch_brainstorm(_api, state):
    if not state.is_brainstorm:
        return CONTINUE
    roles = {}
    if state.panel_task_roles:
        coverage = []
        state.ctx.extra["role_coverage"] = coverage
        roles["models_after"] = lambda: [model for model, _ in coverage]
        roles["roles_after"] = lambda: [role for _, role in coverage]
    planned = list(state.models) if state.panel_task_roles else _api.brainstorm_pool(state.models)
    return _api._run_mode_with_stats(
        state.mode.stats_mode, planned, lambda: state.mode.handler(state.ctx),
        **_provenance(state), level=state.recorded_level, later_models=state.moderators[:1], **roles)


def dispatch_qa(_api, state):
    if state.mode.name != "qa":
        return CONTINUE
    from .qa.executor import resolved_tester_backend

    seat = [resolved_tester_backend(_api._split_models(state.args.model or []))]
    return _api._run_mode_with_stats(state.mode.stats_mode, seat,
        lambda: state.mode.handler(state.ctx), **_provenance(state), direct_seats=True)


def dispatch_text_panel(_api, state):
    if state.mode.name in ("review", "visual"):
        return CONTINUE
    roles = {}
    if state.panel_task_roles:
        models, role_names = list(state.models), list(state.panel_task_roles)
        roles["models_after"] = lambda: models
        roles["roles_after"] = lambda: role_names
    deadline = _api.contextlib.nullcontext()
    if state.just_ask_deadline_seconds is not None and not state.deadline_armed:
        deadline = _api.command_deadline(state.just_ask_deadline_seconds,
            command=f"review just-ask --timeout {state.just_ask_deadline_seconds}")
    with deadline:
        return _api._run_mode_with_stats(state.mode.stats_mode, state.models,
            lambda: state.mode.handler(state.ctx), **_provenance(state), level=state.recorded_level,
            later_models=state.moderators[:1] if state.mode.name == "quorum" else None, **roles)


def validate_review_board(_api, state):
    result = state.validate_board()
    return result if result is not None else CONTINUE


def _review_pool_guard(_api, state):
    if state.mode.name != "review" or not state.diff.strip():
        return None
    reuse = (not state.explicit_models or state.task_role_pad) and any(
        seat.role_lens for seat in state.board) and state.effective_pool_size > 0
    return _api._evaluate_pool_or_bail(
        state.config, state.config_models, _api._seats_of(state.board),
        [] if state.task_role_pad else state.explicit_models,
        state.effective_pool_size if state.task_role_pad else state.args.pool,
        _api._config_default_pool(state.config) or _api.preset_pool_size(state.active_preset, state.config),
        reuse_eligible=reuse)


def _prepare_board_run(_api, state):
    state.exact_m = bool(state.explicit_models) and not state.task_role_pad
    state.review_pool_size = len(state.board) if state.exact_m else state.effective_pool_size
    if state.exact_m:
        planned = list(state.board)
    else:
        planned, _ = _api.select_pool_and_reserve_with_reuse(state.board, state.review_pool_size,
            lambda seat: _api._chain_aware_available(seat.model), usage_percent=state._usage_percent)
    state.eta_models = [seat.model for seat in planned]
    state.outcome_sink = []
    state.board_reporter = None
    if state.mode.name == "review" and state.diff.strip():
        from . import tui

        state.board_reporter = tui.start_if_enabled(
            [(seat.role or "general", seat.model) for seat in planned], no_tui_flag=bool(state.args.no_tui))
    state.ctx.extra.update(board=state.board, pool_size=state.review_pool_size,
        outcome_sink=state.outcome_sink, exact_board=state.exact_m,
        usage_percent=None if state.exact_m else state._usage_percent, board_reporter=state.board_reporter)


def _board_outcome_callbacks(state):
    def models():
        if state.exact_m:
            return [seat.model for seat in state.board]
        return state.outcome_sink[0].usable_models if state.outcome_sink else []

    def roles():
        if state.exact_m:
            return [seat.role for seat in state.board]
        return state.outcome_sink[0].usable_roles if state.outcome_sink else []

    def usable_roles():
        return state.outcome_sink[0].usable_roles if state.outcome_sink else []

    return {"models_after": models, "roles_after": roles, "usable_roles_after": usable_roles}


def dispatch_review_board(_api, state):
    if not state.board:
        return CONTINUE
    result = _review_pool_guard(_api, state)
    if result is not None:
        return result
    _prepare_board_run(_api, state)
    try:
        return _api._run_mode_with_stats(state.mode.stats_mode, state.eta_models,
            lambda: state.mode.handler(state.ctx), **_provenance(state),
            **_board_outcome_callbacks(state), level=state.recorded_level,
            needs_slot=state.mode.name != "review" or bool(state.diff.strip()))
    finally:
        if state.board_reporter is not None:
            state.board_reporter.close()


def dispatch_flat_review(_api, state):
    if state.mode.name == "review" and state.diff.strip():
        result = _api._evaluate_pool_or_bail(state.config, state.config_models,
            tuple((model, model) for model in state.models), state.explicit_models,
            state.args.pool, _api._config_default_pool(state.config) or _api.preset_pool_size(state.active_preset, state.config))
        if result is not None:
            return result
    return _api._run_mode_with_stats(state.mode.stats_mode, state.models,
        lambda: state.mode.handler(state.ctx), **_provenance(state), level=state.recorded_level,
        needs_slot=state.mode.name != "review" or bool(state.diff.strip()))
