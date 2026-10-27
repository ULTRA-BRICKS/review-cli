"""Read-only reviewer-board formatting, using the same configured seats as dispatch."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import Path


def _seat_reads_repo(_api, /, model: str, cwd_is_repo: bool) -> bool:
    backend = _api.backends.resolve_backend(model)
    if backend in (_api.backends.review_codex, _api.backends.review_omp):
        return True
    if backend is _api.backends.review_opencode:
        return cwd_is_repo
    if backend is not _api.backends.review_claude:
        return False
    mode = _api.os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    if mode == "api":
        return False
    if mode == "cli":
        return True
    return _api.backends._have_claude_cli()


def _display_board_source(_api, config, preset, exact_models):
    if exact_models:
        board = _api._board_from_models_lenient(exact_models, config, preset=preset)
        suffix = f" + level:{preset}" if preset else ""
        return board, f"explicit -m{suffix}"
    if preset:
        return _api.load_board(config, preset=preset), f"level:{preset}"
    configured = _api._split_models(config.get("models") or [])
    if configured:
        return _api.board_from_models(configured, config), "config.yaml (models:)"
    source = "config.yaml (board:)" if isinstance(config.get("board"), list) and config.get("board") else "default"
    return _api.load_board(config), source


def _display_pool(_api, board, pool_size, exact_board, pad_task_roles):
    available = [_api.backends.backend_available(seat.model) for seat in board]
    if pad_task_roles and (exact_board or any(seat.role_lens for seat in board)):
        board = _api.ensure_min_distinct_roles(board, pool_size if pool_size > 0 else _api.SHIP_MIN_ROLES)
        exact_board = False
        available = [_api.backends.backend_available(seat.model) for seat in board]
    filled = len(board) if exact_board else _api._effective_pool_size(sum(available), pool_size)
    return board, available, exact_board, filled


def _print_board_heading(board, source, pool_size, filled, exact_board, sized):
    if exact_board:
        print(f"Reviewer board ({len(board)} explicit seats, source: {source}; "
              "exact -m run = every LIVE listed seat is attempted, --pool is ignored — but if the "
              "live subset can't fill the request the pre-dispatch guard PROPOSES a fitting "
              "board/preset instead of running a degenerate panel):\n")
        return
    target = "all AVAILABLE seats" if pool_size <= 0 else f"top {pool_size} AVAILABLE"
    print(f"Reviewer board ({len(board)} seats, priority-ordered, source: {source}; "
          f"live pool = {target} by priority, distinct roles first{sized}, "
          f"{filled} filled, the rest reserve — size with --pool N):\n")


def _active_lens_overrides(_api, board, cwd):
    overrides = {}
    for role in dict.fromkeys(seat.role for seat in board if seat.role):
        if _api.lens_override_text(role, cwd):
            overrides[role] = _api.lens_override_path(role, cwd).relative_to(cwd)
    return overrides


def _availability_label(_api, reviewer, available, exact_board):
    if available:
        return "available"
    excluded = _api.backends.availability_exclusion(reviewer.model)
    if excluded is not None:
        return f"SKIPPED (unavailable: {excluded.describe()})"
    prefix = "will attempt" if exact_board else "SKIPPED"
    if _api.backends.runtime_provider_marked_unpaid(reviewer.model):
        return f"{prefix} (provider unpaid/disabled)"
    return f"{prefix} (no key/CLI)"


def _board_row(_api, index, reviewer, *, available, exact, pool_ids, widths, cwd_is_repo, overrides):
    name_width, role_width, effort_width = widths
    status = _availability_label(_api, reviewer, available, exact)
    role = (reviewer.role or "general").ljust(role_width)
    effort = (reviewer.effort or "-").ljust(effort_width)
    if exact:
        tier = "explicit"
    elif not available:
        tier = "unavail"
    else:
        tier = "pool   " if id(reviewer) in pool_ids else "reserve"
    scope = "agentic" if _api._seat_reads_repo(reviewer.model, cwd_is_repo) else "diff-only"
    note = f"  (lens overridden: {overrides[reviewer.role]})" if reviewer.role in overrides else ""
    priority = f"#{index + 1}"
    return (f"  {priority:>3}  [{tier}]  {reviewer.display.ljust(name_width)}  {role}  "
            f"{reviewer.model}  [{status}]  ({scope})  effort={effort}{note}")


def _print_pool_explanation(exact, pool_size):
    print("\nScope: `agentic` seats (codex / opencode / omp / claude-CLI) run read-only in the "
          "real repo and can read any project file; `diff-only` seats (gemini / z.ai / "
          "commandcode / claude-API) are stateless HTTP calls that see only the diff.")
    if exact:
        print("\nWith explicit `-m`, review-cli attempts exactly the listed models in order. "
              "`--pool` and reserve failover do not slice or reorder explicit seats.")
    elif pool_size <= 0:
        print("\nA plain `review diff` runs all AVAILABLE seats by priority (--pool 0); "
              "a higher-priority seat that is unavailable is skipped, and a seat that "
              "fails mid-run is recorded while the remaining available seats continue. "
              "`--pool N` sizes the pool.")
    else:
        print(f"\nA plain `review diff` runs the top {pool_size} AVAILABLE seats by "
              f"priority, distinct roles first (--pool {pool_size}); a seat that is "
              "unavailable (or fails mid-run) is replaced by a seat that keeps its role (a "
              "same-role reserve, else a reserve re-lensed onto the missing role, else a "
              f"model that already answered) so the pool keeps {pool_size} working reviewers "
              "on distinct roles. `--pool N` sizes the pool; `--pool 0` runs all available seats.")


def _print_board_notes(available, overridden_roles, preset):
    if not all(available):
        print("Unavailable reviewers drop out and are backfilled from the reserve; the "
              "board degrades gracefully only if the reserve is exhausted. The default "
              "agentic seats (`oc:…` Kimi/Qwen/DeepSeek/Grok, `omp:…` GLM/MiMo/Sonnet-omp, "
              "codex, claude) need their CLI on PATH — `oc:` seats need the `opencode` binary "
              "plus its own provider auth "
              "(`opencode auth login`), NOT review-cli's COMMANDCODE_API_KEY/ZAI_API_KEY. "
              "gemini needs GEMINI_API_KEY. (COMMANDCODE_API_KEY powers only the diff-only "
              "`commandcode:` backend for `-m cc`; ZAI_API_KEY powers the diff-only `zai:` "
              "backend for `-m glm` and omp:zai/* seats that have no omp zai login.)")
    if overridden_roles:
        shown = ", ".join(f"{role} ({path})" for role, path in overridden_roles.items())
        print("\nProject lens overrides active (review-cli#547): "
              f"{shown}. Each file's text is APPENDED to that role's built-in "
              "lens, never replacing it; delete/empty the file to fall back to the built-in lens alone.")
    if preset:
        print(f"\nFallback is restricted to level:{preset}; higher capability levels require an explicit choice.")
    else:
        from .role_models import format_role_fallback_section

        print(format_role_fallback_section())


def _show_board(_api, /, config: dict, pool_size: int, cwd: Path | None, *, preset: str | None, explicit_models: list[str] | None, pad_task_roles: bool) -> int:
    """Display configured priority, real availability and task-role padding, read-only."""
    exact_models = explicit_models or []
    try:
        board, source = _display_board_source(_api, config, preset, exact_models)
    except _api.BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    board, available, exact, filled = _display_pool(_api, board, pool_size, bool(exact_models), pad_task_roles)
    sized = " (sized by preset/--pool)" if pool_size != _api.preset_pool_size(preset, config) else ""
    _print_board_heading(board, source, pool_size, filled, exact, sized)
    widths = (max((len(seat.display) for seat in board), default=0),
              max((len(seat.role or "general") for seat in board), default=0),
              max((len(seat.effort or "-") for seat in board), default=1))
    effective_cwd = cwd or _api.Path.cwd()
    cwd_is_repo = _api.backends._opencode_runs_in_repo(effective_cwd)
    overrides = _active_lens_overrides(_api, board, effective_cwd)
    reachable = [seat for index, seat in enumerate(board) if available[index]]
    pool_ids = {id(seat) for seat in _api.prefer_distinct_roles(reachable)[:filled]}
    for index, reviewer in enumerate(board):
        print(_board_row(_api, index, reviewer, available=available[index], exact=exact,
                        pool_ids=pool_ids, widths=widths, cwd_is_repo=cwd_is_repo, overrides=overrides))
    _print_pool_explanation(exact, pool_size)
    _print_board_notes(available, overrides, preset)
    return 0


def _with_visual(_api, /, text: str, visual_ctx) -> str:
    if visual_ctx is None:
        return text
    return text + visual_ctx.context_note
