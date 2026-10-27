"""Pure text-panel selection shared by CLI listing, health and actual dispatch.

Model/provider choices come from level data or an explicit legacy roster, never
from a second hard-coded panel default. Availability cannot raise the level.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import (
    BoardReviewer,
    EffortOverride,
    _split_models,
    default_level_name,
    preset_board,
    resolve_level_name,
    select_pool_and_reserve_with_reuse,
)
from .levels import resolve_levels

TEXT_PANEL_MODES = frozenset({"quorum", "brainstorm", "just-ask"})


@dataclass(frozen=True)
class PanelSelection:
    """Configured candidates before runtime availability and pool sizing."""

    models: tuple[str, ...]
    level: str | None = None
    board: tuple[BoardReviewer, ...] = ()
    default_effort: str | None = None
    source: str = "models:"


def _legacy_models(config: dict, mode: str) -> tuple[list[str], str]:
    if mode == "brainstorm":
        models = _split_models(config.get("brainstorm_models") or [])
        if models:
            return models, "brainstorm_models:"
    return _split_models(config.get("models") or []), "models:"


def resolve_panel_selection(
    config: dict,
    mode: str,
    *,
    level: str | None = None,
    explicit_models: list[str] | None = None,
) -> PanelSelection:
    """Explicit models > explicit level > legacy roster > configured default level.

    Preserve legacy panel rosters unless a level is requested. A configured board
    alone did not define a legacy flat panel; the default level now supplies its
    candidates instead of the old provider-specific DEFAULT_MODELS constant.
    """
    if mode not in TEXT_PANEL_MODES:
        raise ValueError(f"not a text panel: {mode}")
    chosen = resolve_level_name(level, config) if level else None
    explicit = _split_models(explicit_models or [])
    if explicit and not chosen:
        return PanelSelection(tuple(explicit), source="explicit -m")
    legacy, source = _legacy_models(config, mode)
    if legacy and not chosen:
        return PanelSelection(tuple(legacy), source=source)
    chosen = chosen or default_level_name(config)
    board = tuple(preset_board(chosen, config))
    effort = resolve_levels(config).get(chosen).effort
    models = tuple(explicit) if explicit else tuple(seat.model for seat in board)
    return PanelSelection(models, chosen, board, effort, f"level:{chosen}")


@dataclass(frozen=True)
class PanelEffort:
    """Level seat effort with run-scoped provider/global overrides applied last."""

    selection: PanelSelection
    override: EffortOverride | None

    def effort_for(self, model: str) -> str | None:
        if self.override is not None:
            explicit = self.override.effort_for(model)
            if explicit is not None:
                return explicit
        for seat in self.selection.board:
            if seat.model == model:
                return seat.effort
        # Moderators or an explicit -m outside the roster inherit the level effort.
        return self.selection.default_effort


def panel_effort(
    selection: PanelSelection | None, override: EffortOverride | None
) -> EffortOverride | PanelEffort | None:
    if selection is None or selection.level is None:
        return override
    return PanelEffort(selection, override)


def select_level_panel(
    selection: PanelSelection,
    mode: str,
    pool: int,
    available: Callable[[str], bool],
    usage_percent: Callable[[str], float | None],
) -> tuple[list[str], list[str]]:
    """Choose only this level's candidates; never synthesize a heavier fallback."""
    if mode == "just-ask":
        # Every seat gets the same question: repeating a model buys no extra lens.
        reachable = list(dict.fromkeys(model for model in selection.models if available(model)))
        return (reachable[:pool] if pool > 0 else reachable), []
    selected, _reserve = select_pool_and_reserve_with_reuse(
        list(selection.board), pool,
        available=lambda seat: available(seat.model),
        usage_percent=usage_percent,
    )
    return [seat.model for seat in selected], [seat.role for seat in selected]


def panel_default_help(config: dict, mode: str) -> str:
    selected = resolve_panel_selection(config, mode)
    shown = ", ".join(selected.models[:4]) + (", …" if len(selected.models) > 4 else "")
    shown = shown.replace("%", "%%")
    if selected.level:
        return f"the {selected.level!r} level: {shown} (override with --level)"
    return f"your config.yaml {selected.source} {shown} (an explicit --level overrides this)"
