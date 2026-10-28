"""Resolve candidates, role coverage and grounding before model execution."""
from __future__ import annotations

from .cli_dispatch_state import CONTINUE


def read_usage_snapshot(_api, state):
    state._usage_snapshot = _api.usage_limits.load_snapshot()
    state._usage_cards = _api.usage_limits.load_harness_cards()

    def usage_percent(model: str) -> float | None:
        return _api.usage_percent_for_model(model, samples=state._usage_snapshot, cards=state._usage_cards)

    state._usage_percent = usage_percent
    state.quorum_auto_roles = []
    state.brainstorm_has_live = False
    state.level_panel_roles = []
    return CONTINUE


def _select_level_candidates(_api, state):
    if state.mode.name != "just-ask":
        state.effective_pool_size = _api.recorded_task_pool_size(
            state.effective_pool_size, explicit_pool=state.args.pool is not None,
            recorded=bool(state.task_code))
    state.models, state.level_panel_roles = _api.select_level_panel(
        state.selected_panel, state.mode.name, state.effective_pool_size,
        _api._chain_aware_available, state._usage_percent)
    if not state.models:
        print("[review-cli] no available seats in the selected level", file=_api.sys.stderr)
        return 1
    return CONTINUE


def _select_legacy_panel(_api, state):
    """Keep a user's exact legacy roster, including when none of its routes work."""
    source = list(state.selected_panel.models)
    available = _api.backends.backend_available if state.is_brainstorm else _api._chain_aware_available
    reachable = [model for model in source if available(model)]
    state.models = reachable or source
    if not reachable or state.mode.name == "just-ask":
        return
    state.brainstorm_has_live = state.is_brainstorm
    state.models = _api.expand_flat_models_with_reuse(
        reachable, len(reachable), usage_percent=state._usage_percent)
    _api._warn_if_panel_padded(state.models)


def select_panel_models(_api, state):
    if state.explicit_models:
        state.models = state.explicit_models
        return CONTINUE
    if state.selected_panel is not None:
        if state.selected_panel.level:
            return _select_level_candidates(_api, state)
        _select_legacy_panel(_api, state)
        return CONTINUE
    # Diff/visual/QA paths retain their existing board/executor policy downstream.
    state.models = state.config_models or [_api._expand_alias(x) for x in _api.DEFAULT_MODELS]
    return CONTINUE


def resolve_visual_timeout(_api, state):
    state.visual_mode = state.args.visual is not None
    state.visual_models = state.explicit_models or state.config_visual_models or [
        _api._expand_alias(x) for x in _api.VISUAL_MODELS]
    configured = state.config.get("timeout")
    if state.args.timeout is not None:
        state.timeout = state.args.timeout
    elif state.mode.name == "qa":
        state.timeout = _api.QA_TIMEOUT_DEFAULT
    elif state.panel_mode:
        state.timeout = _api.PANEL_TIMEOUT_DEFAULT
    elif configured is not None:
        state.timeout = int(configured)
    else:
        state.timeout = 1200
    return CONTINUE


def _pad_panel_roles(_api, state):
    minimum = state.effective_pool_size if state.effective_pool_size > 0 else _api.SHIP_MIN_ROLES
    if state.explicit_models:
        board = _api._board_from_models_lenient(state.explicit_models, state.config)
        padded = _api.ensure_min_distinct_roles(board, minimum)
        state.models = [seat.model for seat in padded]
        state.panel_task_roles = [seat.role for seat in padded]
        print(f"[review-cli] --task with one -m model: {state.mode.name} runs {state.explicit_models[0]} as {len(state.models)} role slots ({', '.join(state.panel_task_roles)}) = {len(state.models)} calls", file=_api.sys.stderr, flush=True)
        return
    roles = list(dict.fromkeys(seat.role for seat in _api.preset_board(None, state.config) if seat.role_lens))
    count = max(_api.SHIP_MIN_ROLES, len(state.models))
    state.models = [state.models[index % len(state.models)] for index in range(count)]
    state.panel_task_roles = [roles[index % len(roles)] for index in range(count)]
    engines = ", ".join(dict.fromkeys(state.models))
    print(f"[review-cli] --task brainstorm: {len(state.models)} role slots per round ({', '.join(state.panel_task_roles)}) on {engines}", file=_api.sys.stderr, flush=True)


def prepare_role_context(_api, state):
    single_model = len(state.explicit_models) == 1 and state.effective_pool_size > 1
    legacy_brainstorm = state.brainstorm_has_live and state.effective_pool_size != 1
    role_mode = state.mode.name in ("review", "quorum", "brainstorm")
    state.task_role_pad = bool(state.task_code) and role_mode and (single_model or legacy_brainstorm)
    if state.task_role_pad and _api._no_deliberate_pool_choice(state.args, state.explicit_preset, state.config):
        state.effective_pool_size = _api.SHIP_MIN_ROLES
    state.panel_task_roles = list(state.level_panel_roles or state.quorum_auto_roles)
    if state.task_role_pad and state.panel_mode:
        _pad_panel_roles(_api, state)
    board_preference = (not state.explicit_models or bool(state.config_models) or
                        state.config_has_board or state.active_preset is not None or state.task_role_pad)
    state.use_board = not state.panel_mode and board_preference
    return CONTINUE


def _resolve_board(_api, state):
    if state.explicit_models:
        return _api._board_from_models_lenient(state.explicit_models, state.config, preset=state.active_preset)
    if state.active_preset is not None:
        return _api.load_board(state.config, preset=state.active_preset)
    if state.config_models:
        return _api.board_from_models(state.config_models, state.config)
    return _api.load_board(state.config)


def _repair_recorded_board_roles(_api, state):
    if state.board is None:
        return
    state.board = _api.apply_effort_override(state.board, state.effort_override)
    minimum = state.effective_pool_size if state.effective_pool_size > 0 else _api.SHIP_MIN_ROLES
    if not state.task_code or state.mode.name != "review" or minimum <= 1:
        return
    repair = state.task_role_pad or (not state.explicit_models and any(seat.role_lens for seat in state.board))
    if repair:
        state.board = _api.ensure_min_distinct_roles(state.board, minimum)


def define_board_validation(_api, state):
    """Keep lazy validation: early commands and text panels do not load a diff board."""
    state.board = None
    state.board_validated = False

    def validate_board() -> int | None:
        if state.board_validated or not state.use_board:
            return None
        state.board_validated = True
        try:
            state.board = _resolve_board(_api, state)
        except _api.BoardConfigError as exc:
            print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
            return 2
        _repair_recorded_board_roles(_api, state)
        return None

    state.validate_board = validate_board
    if state.mode.announce_logs:
        _api.backends._ANNOUNCE_LOGS = True
    return CONTINUE


def _read_diff(_api, state, *, required):
    try:
        state.diff = _api._git_diff(state.cwd, state.args.staged)
    except RuntimeError as exc:
        if required:
            return _api._fail_git_diff(state.cwd, exc)
        state.diff = ""
    return CONTINUE


def _optional_diff_requested(state):
    if state.is_brainstorm:
        return True
    if state.mode.name == "review":
        return state.visual_mode
    if state.is_visual_subcommand:
        return state.args.diff or state.args.staged
    return state.panel_mode and state.args.diff


def _required_grounding_diff(_api, state):
    if _api._is_git_repo(state.cwd):
        return _read_diff(_api, state, required=not state.panel_mode)
    if not state.panel_mode:
        return _api._fail_not_a_repo(state.cwd)
    state.diff = ""
    return CONTINUE


def acquire_grounding_diff(_api, state):
    state.diff = state.piped_input
    state.diff_from_stdin = state.diff is not None
    required_mode = state.args.staged or (state.mode.name == "review" and not state.visual_mode)
    state.needs_diff = required_mode and not state.is_brainstorm and not state.is_visual_subcommand
    if state.diff is not None:
        return CONTINUE
    if state.needs_diff:
        result = _required_grounding_diff(_api, state)
        if result is not CONTINUE:
            return result
    elif _optional_diff_requested(state):
        _read_diff(_api, state, required=False)
    state.diff = state.diff or ""
    return CONTINUE


def capture_provenance(_api, state):
    state.repo_id = _api._compute_repo_id(state.cwd)
    state.diff_files = _api.extract_diff_files(state.diff)
    state.diff_sha256 = _api.diff_content_hash(state.diff) if state.diff else None
    state.stamp_diff_hash = _api._stamp_hash_for_staged_diff(state.cwd) if state.args.staged else None
    if state.diff and not state.diff_from_stdin and state.mode.name in ("brainstorm", "quorum", "just-ask"):
        refusal = _api.refuse_oversized_diff(state.diff, state.mode.name)
        if refusal is not None:
            return refusal
    state.visual_ctx = None
    state.just_ask_deadline_seconds = state.timeout if state.mode.name == "just-ask" and state.args.timeout is not None else None
    state.deadline_armed = False
    return CONTINUE


def plan_companion_seats(_api, state):
    def planned_panel() -> list[str]:
        if state.is_brainstorm:
            return list(state.models) if state.panel_task_roles else list(_api.brainstorm_pool(state.models))
        if state.panel_mode or not state.board:
            return list(state.models)
        if state.explicit_models and not state.task_role_pad:
            return [seat.model for seat in state.board]
        pool, _ = _api.select_pool_and_reserve_with_reuse(state.board, state.effective_pool_size,
            lambda seat: _api._chain_aware_available(seat.model), usage_percent=state._usage_percent)
        return [seat.model for seat in pool]

    def moderators() -> list[str]:
        explicit = getattr(state.args, "moderator", None)
        if explicit:
            explicit = _api._expand_alias(explicit)
        if not state.panel_mode:
            return []
        return _api.pick_moderators(explicit, state.models,
            _api._moderator_candidates(state.config, getattr(state.args, "level", None)))

    def moderator_seat() -> list[str]:
        return moderators()[:1] if state.mode.name in ("brainstorm", "quorum") else []

    def vision_seat() -> list[str]:
        from .features.visual.compose import _ordered_vision_backends

        return _ordered_vision_backends(list(state.visual_models or []))[:1]

    state._planned_panel = planned_panel
    state._moderators = moderators
    state._moderator_seat = moderator_seat
    state._vision_seat = vision_seat
    return CONTINUE
