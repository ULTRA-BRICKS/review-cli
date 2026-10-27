"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import ModeSpec, argparse

def _help_model_list(_api, config, key):
    try:
        return _api._split_models(config.get(key) or [])
    except Exception:
        return []


def _help_format_models(models):
    shown = ", ".join(models[:4]) + (", …" if len(models) > 4 else "")
    return shown.replace("%", "%%")


def _review_model_help(_api, config, models):
    if models:
        return f"your config.yaml models priority roster: {_help_format_models(models)}"
    if isinstance(config.get("board"), list) and config.get("board"):
        try:
            board_models = [seat.model for seat in _api.load_board(config)]
        except Exception:
            board_models = []
        if board_models:
            return f"your config.yaml board: {_help_format_models(board_models)}"
    return (f"the {_api._default_level_for_help()!r} level reviewer board "
            "(run `review --show-board`; see `review help config`)")


def _visual_model_help(_api, config):
    models = _help_model_list(_api, config, "visual_models")
    if models:
        return f"your config.yaml visual_models: {_help_format_models(models)}"
    defaults = [_api._expand_alias(model) for model in _api.VISUAL_MODELS]
    return f"{_help_format_models(defaults)} (the visual defaults)"


def _model_default_help(_api, /, mode: ModeSpec | None) -> str:
    """Show the same defaults as listing and dispatch; bad config never breaks help."""
    try:
        config = _api.load_config()
    except Exception:
        config = {}
    models = _help_model_list(_api, config, "models")
    name = mode.name if mode is not None else "review"
    if name in _api.TEXT_PANEL_MODES:
        try:
            return _api.panel_default_help(config, name)
        except (_api.BoardConfigError, TypeError, ValueError):
            return "the configured capability level (see `review help config`)"
    if name == "visual":
        return _visual_model_help(_api, config)
    if name == "qa":
        return "claude (the qa tester; use `-m codex` or REVIEW_QA_TESTER=codex for the codex seat)"
    if name == "review":
        return _review_model_help(_api, config, models)
    if models:
        return f"your config.yaml models: {_help_format_models(models)}"
    defaults = [_api._expand_alias(model) for model in _api.DEFAULT_MODELS]
    return f"{_help_format_models(defaults)} (the built-in defaults)"


def _default_level_for_help(_api, /) -> str:
    """The default level's name for `--help` text; `--help` must never crash on a bad
    config, so a levels error shows the shipped default."""
    try:
        return _api.default_level_name(_api.load_config())
    except Exception:  # noqa: BLE001
        return _api.default_level_name(None)


def _levels_help(_api, /) -> str:
    """One line per level (pool, effort, aliases, next heavier) for `--help`, from the
    shipped + user levels data; a broken user config falls back to the shipped levels."""
    from .levels import resolve_levels

    try:
        level_set = resolve_levels(_api.load_config())
    except Exception:  # noqa: BLE001 — --help must never crash on a bad config
        level_set = resolve_levels(None)
    parts = []
    for lv in level_set.levels.values():
        alias = f", alias {'/'.join(lv.aliases)}" if lv.aliases else ""
        parts.append(f"{lv.name} (pool {lv.pool}, {lv.effort} effort{alias})")
    return "; ".join(parts).replace("%", "%%")


def _level_arg(_api, /, value: str) -> str:
    """argparse `type` of `--level`/`--preset`: the canonical level name (the legacy preset
    names light/default/heavy are level aliases), or an argparse error naming the known levels."""
    try:
        return _api.resolve_level_name(value, _api.load_config())
    except _api.BoardConfigError as exc:
        raise _api.argparse.ArgumentTypeError(str(exc)) from exc


def _moderator_default_help(_api, /) -> str:
    """The EFFECTIVE auto-picked `--moderator` default, for `--help`: the chain
    `config.resolve_moderator_candidates` yields for the user's config (their
    `moderator_candidates:`, else the default level's `moderators`), filtered to
    availability at run time.

    Escape `%` -> `%%`: this is interpolated into an argparse `help=`, where `%` is
    formatting syntax, and config values are untrusted (same guarantee as
    `_model_default_help`). Best-effort: a config read failure shows the shipped chain."""
    try:
        chain, _ = _api.resolve_moderator_candidates(_api.load_config())
    except Exception:  # noqa: BLE001 — --help must never crash on a bad config
        chain, _ = _api.resolve_moderator_candidates({})
    return " -> ".join(chain).replace("%", "%%")


def _moderator_candidates(_api, /, config: dict, level: str | None) -> tuple[str, ...]:
    """The moderator chain for this run: `moderator_candidates:` > the level's chain
    (`level` None = the default level); a malformed `moderator_candidates:` value is
    reported once on stderr, then ignored."""
    chain, notice = _api.resolve_moderator_candidates(config, level)
    if notice and notice not in _api._MODERATOR_NOTICES_SHOWN:
        _api._MODERATOR_NOTICES_SHOWN.add(notice)
        print(f"[review] {notice}", file=_api.sys.stderr, flush=True)
    return chain


def _add_global_options(_api, /, parser: argparse.ArgumentParser, *, mode: ModeSpec | None) -> None:
    """Add the TRULY-GLOBAL options — the only ones the top-level `review --help` should
    list (ROADMAP "Subcommand-only options belong in the subcommand help, not the global
    list"): `-m/--model`, `-C/--cwd`, `-o/--output`, `--timeout`, `--list-defaults`,
    `--show-board`, `--pool`. These apply to every path (the meta flags + every mode), so
    they sit on the top-level parser AND every mode parser.

    Configurable options show their EFFECTIVE default value (ROADMAP "Help must show ACTUAL
    defaults"), resolving the config cascade + the mode where relevant (`--model`)."""
    parser.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help=f"model/backend to run; repeat or comma-separate (default: {_api._model_default_help(mode)})",
    )
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
    parser.add_argument(
        "--task",
        metavar="CODE",
        default=None,
        help=(
            "task/issue code for this review iteration (required for recorded review "
            "modes; standalone visual without diff is exempt; may also be supplied by "
            "$REVIEW_TASK_CODE)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        default=None,
        help=(
            "write the result to FILE via Python (creates parent dirs, overwrites) "
            "while still printing to stdout. Use this instead of `review … > FILE`, "
            "which fails under zsh noclobber."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "per-call timeout seconds; REST uses wall/request timeout; review/panel "
            "agent CLIs use idle/silence timeout with a 20m floor for values >=60 "
            "(set REVIEW_IDLE_TIMEOUT_SECONDS to shorten); qa/vision keep wall-clock "
            f"caps (requested defaults: review 1200, panel 240, qa {_api.QA_TIMEOUT_DEFAULT}). "
            "An explicit `just-ask --timeout N` is instead a hard wall-clock bound: "
            "every seat still running N s after dispatch is killed, the command lists "
            "each seat and why it gave no answer, exits non-zero, and is force-exited "
            f"(124) at N + {_api.COMMAND_TIMEOUT_GRACE_SECONDS} s at the latest"
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help=(
            "spawn this review as a session-detached background process and return "
            "almost immediately with a job-id (see `review jobs`/`review status`/"
            "`review wait`); not supported on dashboard/spec-web"
        ),
    )
    parser.add_argument(
        "--list-defaults", action="store_true", help="print default models and exit"
    )
    parser.add_argument(
        "--show-board",
        action="store_true",
        help="print the active reviewer board (model -> role, availability) plus per-role fallback model ids and exit",
    )
    if mode is None or mode.name == "review" or mode.name in _api.TEXT_PANEL_MODES:
        review_level = mode is None or mode.name == "review"
        parser.add_argument(
            "--level",
            "--preset",
            dest="level",
            type=_api._level_arg,
            default=None,
            metavar="LEVEL",
            help=(
                "capability level: how much review this task needs; the lowest sufficient "
                "level is the default and a heavier one is used only when you ask for it "
                "(never because a seat is unavailable). `--preset` is the same option "
                f"under its older name (light/default/heavy = routine/standard/deep). "
                f"Levels: {_api._levels_help()}. "
                + (
                    "Selects the board, pool and effort of this diff review "
                    "(a recorded `--task` run floors the pool to 3 distinct roles so gh ship "
                    "can pass). "
                    if review_level
                    else "Selects this panel's models, pool, effort and moderator chain "
                    "(explicit `-m`, `--pool`, `--effort` and `--moderator` still win). "
                )
                + f"Default: {_api._default_level_for_help()!r}, definitions in the shipped "
                "reviewlib/levels.yaml plus your config `levels:`."
            ),
        )
    parser.add_argument(
        "--pool",
        type=int,
        default=None,
        metavar="N",
        help=(
            "how many of the board's seats to run (default: the level's pool, see --level "
            f"({_api._levels_help()}); a recorded `--task` run floors that to "
            f"{_api.SHIP_MIN_ROLES} so gh ship can pass; {_api.DEFAULT_POOL_SIZE} is only the fallback "
            "for a custom config `models:`/`board:` roster with no `pool:` key set); the "
            "first N seats participate, the rest are kept in reserve. The board is "
            "never off — --pool only sizes it. N<=0 means all seats. An explicit `--pool N` "
            "below 3 with `--task` still wins but warns. Ignored for exact multi-model -m; "
            "a recorded `--task -m` one-model run pads that model across the ship floor; "
            "a default brainstorm `--task` gives its live panel ship-floor role slots. "
            "Recorded `--task --pool 0` still pads a same-role config roster to the ship floor."
        ),
    )
    parser.add_argument(
        "--effort",
        action="append",
        default=[],
        metavar="LEVEL|PROVIDER=LEVEL",
        help=(
            "run-scoped reasoning effort, overriding each seat's config effort for THIS run. "
            "A bare level (minimal/low/medium/high/xhigh/max) applies to every seat; "
            "PROVIDER=LEVEL (e.g. codex=high, opencode=max) overrides one backend route. "
            "Repeat or comma-separate; per-provider wins over the global level. "
            "Reaches the codex, claude, opencode, and omp reasoning-effort levers plus the "
            "screenshot vision call."
        ),
    )


def _add_visual_options(_api, /, parser: argparse.ArgumentParser, *, include_visual_flag: bool, json_help: str) -> None:
    """Add the composable `--visual` feature flags as their own argument GROUP. These are
    SUBCOMMAND-scoped, NOT global (ROADMAP): `--visual` rides any subcommand, so they live
    on every MODE parser but must NOT clutter the top-level `review --help`. Grouping them
    makes `review <mode> --help` render them under a clear "visual verification" heading.

    `json_help` (ticket #546) lets a caller override `--json`'s help text WITHOUT adding a
    second, colliding `--json` flag: this group already sits on EVERY mode parser (diff
    review included), so `review diff`'s own `--json` (structured findings, wired in
    `modes/review.py`) reuses this SAME argparse flag rather than defining its own — see
    `_add_mode_options` below, the only caller that passes a non-default value."""
    group_title = (
        "visual verification"
        if not include_visual_flag
        else "visual verification (the composable --visual flag; rides text subcommands)"
    )
    group = parser.add_argument_group(group_title)
    if include_visual_flag:
        group.add_argument(
            "--visual",
            metavar="IMAGE",
            help='image to verify/attach; rides text subcommands (e.g. `review brainstorm "Q" --task CODE --visual IMAGE`; prefer `review visual IMAGE` for standalone)',
        )
    group.add_argument(
        "--before",
        metavar="IMAGE",
        help="baseline image for diff-aware judgement / no-effect bypass",
    )
    group.add_argument(
        "--intent",
        metavar="TEXT",
        help="free-text edit intent (untrusted; may only tighten the contract)",
    )
    group.add_argument(
        "--expect",
        metavar="KIND",
        help="expectation kind: zero-diff|move|resize|style|wrap|insert|delete|text",
    )
    group.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        help="force-activate a visual module by name (repeatable)",
    )
    group.add_argument("--json", action="store_true", help=json_help)
    group.add_argument(
        "--strict",
        action="store_true",
        help="exit 10 on a blocking visual verdict (gate use)",
    )
    group.add_argument(
        "--no-ai",
        action="store_true",
        help="run cvGate only (no vision call) — fast CI smoke / offline",
    )
    group.add_argument(
        "--no-local-model",
        action="store_true",
        help="disable the Stage-2a local pre-classifier (known-good cache cost-saver); flow = cvGate → vision (§3.1a)",
    )
    group.add_argument(
        "--vision-timeout",
        type=int,
        default=60,
        help="per vision-call timeout seconds (default 60)",
    )
    group.add_argument(
        "--project",
        default=None,
        help="project root for per-project visual modules (default --cwd)",
    )


def _add_mode_options(_api, /, parser: argparse.ArgumentParser, *, mode: ModeSpec) -> None:
    """Add the full surface for an EXPLICIT `review <mode> …` subcommand parser: the global
    options, the diff-source flags, the mode-relevant flags (`--prompt` for the diff review,
    `--moderator` for the panel modes), the composable visual group, and the mode's own
    UNIQUE arguments (its positional question/topic, via `add_arguments`).

    `--diff`/`--staged` and `--prompt`/`--moderator` are scoped to the modes that use them
    (ROADMAP): `--prompt` is the diff review's prompt; `--moderator` steers quorum/brainstorm.
    They are harmless if a mode ignores them, but scoping keeps each `review <mode> --help`
    showing only what that mode actually reads."""
    _api._add_global_options(parser, mode=mode)

    # --diff / --staged select the diff source. They matter to the diff review (its diff is
    # required) and brainstorm/panel grounding; keep them on every mode parser (a mode that
    # ignores one is harmless) but OFF the top-level overview.
    parser.add_argument(
        "--diff",
        action="store_true",
        help="use the working-tree diff (default for the diff review; optional grounding for brainstorm)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="use the staged diff (git diff --cached) instead of the working-tree diff",
    )

    # --prompt is the DIFF REVIEW's prompt override only.
    if mode.name == "review":
        parser.add_argument(
            "--prompt", default=_api.DEFAULT_PROMPT, help="override the diff-review prompt"
        )
    # --moderator steers the quorum / brainstorm synthesis only.
    if mode.name in ("quorum", "brainstorm"):
        parser.add_argument(
            "--moderator",
            default=None,
            help=f"moderator backend (default: auto-pick, first available of {_api._moderator_default_help()})",
        )

    # The composable --visual feature rides any subcommand, so its group is on every mode
    # parser (but never the top-level overview). --rounds / --max-rounds are brainstorm-only
    # and added by the brainstorm mode's own add_arguments; they stay in _VALUE_TAKING_OPTS
    # so the mode-agnostic `-o` pre-scan treats them as value-taking.
    # `--json`'s help text is mode-aware (ticket #546): `review diff` (and a `review
    # visual IMAGE --diff/--staged` companion run, which reaches the SAME handler via
    # visual.py's own `_handler = review._handler`) gives the flag a SECOND meaning —
    # a structured findings envelope, wired entirely in modes/review.py — since the
    # pre-existing "visual verdict JSON" behaviour never fires there anyway (it is
    # gated on there being NO diff at all; diff review always has one). Every other
    # mode (quorum/brainstorm/just-ask) keeps the original, unrelated help text.
    json_help = (
        "with a diff in play (review diff, or review visual IMAGE --diff/--staged): "
        "emit a structured findings JSON envelope on stdout instead of the prose "
        "panel -- additive, the prose output is unaffected when this flag is absent. "
        "Each backend is additionally asked to emit one fenced JSON block on top of "
        "its normal prose; findings[] items carry file/line/severity(P0-P3)/"
        "confidence(high|medium|low)/verdict(FINDING|CLEAR)/source (the seat that "
        "raised it), plus summary.expected/reported/degraded (which seats ran / "
        "parsed / failed to parse). Exit 0 = no findings, 1 = findings present, 2 = "
        "no seat's structured response was usable at all -- --commit's own checkpoint "
        "exit codes (11/12), and 13 (a diff with a hunk too big to split into passes; "
        "nothing ran), still take priority over this 0/1/2 contract. A diff over "
        "$REVIEW_DIFF_MAX_BYTES is reviewed in several passes: findings[] then holds "
        "every pass's findings and a seat counts as reported only when every pass of "
        "it parsed. For a STANDALONE `review visual IMAGE` (no diff), this "
        "instead emits the structured VISUAL verdict, unchanged from before."
        if mode.name in ("review", "visual")
        else "emit the structured visual verdict as JSON"
    )
    _api._add_visual_options(
        parser, include_visual_flag=(mode.name != "visual"), json_help=json_help
    )

    if mode.add_arguments is not None:
        mode.add_arguments(parser)


def _reject_subcommand_only_flag_without_verb(_api, /, argv: list[str]) -> int | None:
    """If a SUBCOMMAND-scoped flag appears with NO recognized subcommand, print the friendly
    `review diff` migration pointer + the usage code (2), instead of letting argparse reject
    it with an opaque "unrecognized arguments" that drops the pointer (codex review). Scans
    up to the first `--` (end-of-options), matching the `=value` form too. Returns 2 when such
    a flag leads a verb-less invocation, else None (a purely-global `review -C <repo>` falls
    through to the help pointer; a recognized mode OR bare management subcommand is left
    alone — those own their flags).

    Self-contained (checks the no-subcommand condition itself) so it is safe to call in
    `main()` BEFORE the `-o` tee is armed — a usage error must NOT write/truncate the `-o`
    file (like the removed-flags guards)."""
    if (
        argv
        and not argv[0].startswith("-")
        and (argv[0] in _api.known_subcommands() or argv[0] in _api._BARE_SUBCOMMANDS)
    ):
        return None  # a recognized subcommand (mode or management) owns these flags
    for tok in argv:
        if tok == "--":
            break
        if tok.split("=", 1)[0] in _api._SUBCOMMAND_ONLY_FLAGS:
            use_line = (
                "  use:  review visual IMAGE [--diff] [options]\n"
                if tok.split("=", 1)[0] == "--visual"
                else "  use:  review diff --task CODE [options]   "
                "(e.g. `review diff --staged --task CODE`)\n"
            )
            print(
                "review: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one), and that flag belongs to a "
                "subcommand.\n"
                f"{use_line}"
                "  (run `review --help` for all subcommands)",
                file=_api.sys.stderr,
                flush=True,
            )
            return 2
    return None
