"""Ordered CLI dispatch, preserving admission and every original early return."""
from __future__ import annotations

from . import cli_dispatch_setup, cli_dispatch_select, cli_dispatch_execute
from .cli_dispatch_state import CONTINUE, DispatchState

STAGES = (
    cli_dispatch_setup.management_commands,
    cli_dispatch_setup.parse_mode_arguments,
    cli_dispatch_setup.load_runtime_policy,
    cli_dispatch_setup.select_capability,
    cli_dispatch_setup.list_configured_models,
    cli_dispatch_setup.normalize_early_task,
    cli_dispatch_setup.check_review_role_floor,
    cli_dispatch_setup.show_review_board,
    cli_dispatch_setup.validate_task_identity,
    cli_dispatch_setup.validate_mode_context,
    cli_dispatch_select.read_usage_snapshot,
    cli_dispatch_select.select_panel_models,
    cli_dispatch_select.resolve_visual_timeout,
    cli_dispatch_select.prepare_role_context,
    cli_dispatch_select.define_board_validation,
    cli_dispatch_select.acquire_grounding_diff,
    cli_dispatch_select.capture_provenance,
    cli_dispatch_select.plan_companion_seats,
    cli_dispatch_execute.run_visual_companion,
    cli_dispatch_execute.build_mode_context,
    cli_dispatch_execute.dispatch_brainstorm,
    cli_dispatch_execute.dispatch_qa,
    cli_dispatch_execute.dispatch_text_panel,
    cli_dispatch_execute.validate_review_board,
    cli_dispatch_execute.dispatch_review_board,
    cli_dispatch_execute.dispatch_flat_review,
)


def _dispatch_scoped(_api, /, argv=None) -> int:
    state = DispatchState(argv=argv)
    for stage in STAGES:
        result = stage(_api, state)
        if result is not CONTINUE:
            return result
    raise RuntimeError("dispatch pipeline produced no terminal result")
