"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import Candidate, ModeSpec, argparse

def _subcommand_epilog(_api, /) -> str:
    topics = "\n".join(
        f"  review help {t:<8} {summary}" for t, (summary, _) in _api.HELP_TOPICS.items()
    )
    return (
        "subcommands:\n"
        + "\n".join(f"  {m.subcommand:<11} {m.summary}" for m in _api.iter_modes())
        + (
            "\n  dashboard   managed web dashboard over review-cli runs (run/start/status/stop/enable/disable)"
            "\n  sessions    list / resume brainstorm sessions (-a all, -s <id> resume)"
            "\n  task        show review iterations and transcripts for one task code"
            "\n  stat        per-harness/per-model usage + health report from the real call logs"
            "\n  capacity-check"
            "\n              is the active board's whole capacity out of quota? (`review help capacity`)"
            "\n  health      probe every seat now (5s, parallel), record when unavailable seats answer again"
            "\n  jobs        list detached (--detach) review jobs"
            "\n  status      show one detached job's status, paths, and a log tail (`status <job-id>`);"
            "\n              bare `status`: which board seats can run now, why not, and their substitute"
            "\n  seats       = bare `status`: every seat, Claude CLI account and omp Anthropic account (alias: doctor)"
            "\n  wait        block until a detached job finishes (`wait <job-id>`)"
            "\n  feedback    attach overseer feedback/usefulness/ticket-link to a recorded run (run_id from the end-of-run nudge)"
            "\n  spec-web    multi-spec web reviewer daemon (start/status/stop/add <spec>; also `spec-web <spec>`)"
            "\n  install-skill / install-commit-hook [--print-gate] / install-hook tg / register-module"
            "\n  --version   print the review-cli version (checkout pyproject, else installed metadata)"
            "\n\nhelp topics (deep help — `review help <topic>` or `review --help <topic>`):\n"
            + topics
            + "\n  see `review help config` for configuration."
        )
    )


def _build_top_level_parser(_api, /) -> argparse.ArgumentParser:
    """Build the TOP-LEVEL `review` parser — the overview shown by a bare `review` and by
    `review --help`. It advertises the SUBCOMMAND list (the diff review is `review diff`
    now; a bare `review` no longer runs a diff review — it prints this help) and carries
    only the TRULY GLOBAL options + the board/meta flags (`--list-defaults` / `--show-board`
    / `--pool`). Mode/visual-only flags live on their own subparsers (scoped help)."""
    parser = _api.argparse.ArgumentParser(
        prog="review",
        description=(
            "Run read-only code reviews / AI panels across multiple model backends "
            "(one narrow opt-in exception: `review diff --staged --task CODE --commit` "
            "checkpoints a commit). Everything is a SUBCOMMAND: `review diff` (review the git diff), "
            "`review brainstorm`, `review just-ask`, `review quorum`. A bare `review` "
            "(no subcommand) prints this help — it does NOT run a diff review; use "
            "`review diff` for that."
        ),
        epilog=_api._subcommand_epilog(),
        formatter_class=_api.argparse.RawDescriptionHelpFormatter,
    )
    # Only the truly-global options — NOT the mode/visual-only flags (ROADMAP: those belong
    # in the subcommand help, not the global list). The top-level parser only ever serves the
    # meta flags (--list-defaults / --show-board / --help) + the help fall-through; it never
    # dispatches a review, so it does not need --prompt / --diff / --moderator / the visual
    # group.
    _api._add_global_options(parser, mode=None)
    return parser


def _build_mode_parser(_api, /, mode: ModeSpec) -> argparse.ArgumentParser:
    """Build the argparse surface for an EXPLICIT `review <mode> …` subcommand (its prog
    is `review <subcommand>` and it carries the global options + the mode-relevant flags +
    the composable visual group + the mode's own positional)."""
    parser = _api.argparse.ArgumentParser(
        prog=f"review {mode.subcommand}",
        description=mode.summary,
        formatter_class=_api.argparse.RawDescriptionHelpFormatter,
    )
    _api._add_mode_options(parser, mode=mode)
    return parser


def _reject_removed_flags(_api, /, argv: list[str]) -> int | None:
    """Reject flags this redesign REMOVED with a clear, actionable error instead of letting
    them silently mis-parse (mode flags) or hit argparse's opaque `unrecognized arguments`
    (the no-replacement flags). Two classes:

      * REMOVED_MODE_FLAGS (`--brainstorm`/`--quorum`/`--just-ask`) → "use the subcommand";
      * REMOVED_FLAGS (`--mcp`/`--ln`) → a 3-part what/why/how-to-fix error (the `--mcp`
        case is the dead review-MCP entrypoint a stale `~/.claude/mcp/mcp.json` still spawns;
        the error tells the user to drop that registration — see structured-exit-codes).

    Returns the stable usage exit code (2) when a removed flag is present, else None. Scans
    only up to the first `--` (end-of-options), so the same string appearing as a positional
    value (e.g. a quote that literally contains '--quorum') is untouched."""
    for tok in argv:
        if tok == "--":
            break
        # `--brainstorm=foo` / `--mcp=foo` form too.
        bare = tok.split("=", 1)[0]
        sub = _api.REMOVED_MODE_FLAGS.get(bare)
        if sub is not None:
            task_hint = (
                " --task CODE" if sub in {"brainstorm", "just-ask", "quorum"} else ""
            )
            print(
                f"review: `{bare}` is no longer a flag — it is now the `{sub}` subcommand.\n"
                f'  use:  review {sub} "<your text>"{task_hint} [options]\n'
                f"  (modes are subcommands now: brainstorm / just-ask / quorum; "
                f"run `review --help`)",
                file=_api.sys.stderr,
                flush=True,
            )
            return 2
        removed = _api.REMOVED_FLAGS.get(bare)
        if removed is not None:
            print(
                f"review: `{bare}` was removed and is no longer accepted.\n"
                f"  why:  {removed.reason}\n"
                f"  fix:  {removed.fix}",
                file=_api.sys.stderr,
                flush=True,
            )
            return 2
    return None


def _reject_removed_subcommand(_api, /, argv: list[str]) -> int | None:
    """Reject the renamed-away SUBCOMMAND verb `review review` (the diff review is `review
    diff` now) with a one-line `review diff` pointer + the stable usage code (2), else None.
    A pure argv check so it can run in `main()` BEFORE the `-o` tee is armed — a usage error
    must NOT write/truncate the `-o` file (codex P1), exactly like the removed FLAGS. It scans
    past leading global options because `review -m fable review` should get the same helpful
    pointer as `review review`. The later call in `_dispatch` is then a harmless no-op."""
    removed = _api._leading_removed_subcommand(argv)
    if removed:
        replacement = _api.REMOVED_SUBCOMMANDS[removed]
        print(
            f"review: `review {removed}` is no longer a subcommand — the diff review is "
            f"now `review {replacement}`.\n"
            f"  use:  review {replacement} --task CODE [options]\n"
            f"  (run `review --help` for all subcommands)",
            file=_api.sys.stderr,
            flush=True,
        )
        return 2
    return None


def _leading_removed_subcommand(_api, /, argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return None
        if tok in _api.REMOVED_SUBCOMMANDS:
            return tok
        from .cli_entry_helpers import _leading_option_width

        width = _leading_option_width(_api, tok)
        if not width:
            return None
        i += width
    return None


def _help_subcommand(_api, /, rest: list[str]) -> int:
    """`review help [<topic>]` — deep topic help (ROADMAP "Topic-based help across the
    ecosystem"). Bare `review help` lists the topics; `review help <topic>` prints that
    topic. An unknown topic lists the available ones + exits 2 (usage). Also reached as
    `review --help <topic>` (see the main dispatch). Topics live in HELP_TOPICS so adding a
    topic needs no edit here.

    A USAGE error here `raise`s SystemExit(2), it does NOT `return` — like argparse's own
    usage errors and the bare-`review` help path below. `main()`'s `-o` tee only persists the
    captured output on a `return` (a "the dispatch ran" signal); a `return 2` here would let
    that tee TRUNCATE a pre-existing `-o` target to empty on `review help bogus -o file.md`
    (premium merge-gate finding, same data-loss class as #37). SystemExit propagates through
    the tee's `finally` with `completed=False`, so no write happens. The SUCCESS paths (the
    topic listing / a valid topic) still `return 0`: that output is real and SHOULD be teed."""
    if not rest:
        print(
            "review help topics:\n"
            + "\n".join(
                f"  {t:<10} {summary}" for t, (summary, _) in _api.HELP_TOPICS.items()
            )
            + "\n\nRun `review help <topic>` (or `review --help <topic>`) for the full reference."
        )
        return 0
    # Exactly one topic token — extra trailing args are a usage error, not silently dropped
    # (codex review: `review help config --bogus` / `review --help config extra` must NOT
    # exit 0 ignoring the tail).
    if len(rest) > 1:
        print(
            f"review help: takes a single topic, got extra arguments: {' '.join(rest[1:])}\n"
            f"  use:  review help <topic>   (topics: {', '.join(_api.HELP_TOPICS)})",
            file=_api.sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    topic = rest[0]
    entry = _api.HELP_TOPICS.get(topic)
    if entry is None:
        known = ", ".join(_api.HELP_TOPICS)
        print(
            f"review help: unknown topic '{topic}'. Known topics: {known}.",
            file=_api.sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    _summary, render = entry
    text = render()
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _config_default_pool(_api, /, config: dict) -> int | None:
    """A `pool:` default from config.yaml (a positive int), or None when absent/invalid.

    Lets a personal config pin the default review pool size (e.g. 3 while a provider is
    disabled). A non-positive / non-integer value is ignored (falls back to the preset
    default) rather than erroring — `pool: 0`/`--pool 0` "all seats" stays a CLI-only knob."""
    raw = config.get("pool")
    if isinstance(
        raw, bool
    ):  # bool is an int subclass; a stray `pool: true` is not a size
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def _no_deliberate_pool_choice(_api, /, args, explicit_preset: bool, config: dict) -> bool:
    """True when nothing -- CLI `--pool`, `--preset`, or a config `pool:` default --
    expressed a deliberate pool-size preference, so a ship-floor pad heuristic is
    free to size the pool itself. (review-cli#309 round-3 review finding: this
    exact condition was duplicated verbatim at the early show-board-facing pad and
    the later dispatch-side pad -- the same duplication-drift pattern that caused
    the original off-by-one bug this PR fixes.)"""
    return (
        args.pool is None
        and not explicit_preset
        and _api._config_default_pool(config) is None
    )


def _seats_of(_api, /, board: list) -> tuple[tuple[str, str], ...]:
    """Board seats as (model, display-name) pairs for the pool-selection guard."""
    return tuple((r.model, r.display) for r in board)


def _board_from_models_lenient(_api, /, models: list[str], config: dict, *, preset: str | None) -> list:
    """`board_from_models(models, config, preset=preset)`, but a malformed config
    `board:` entry (`BoardConfigError` -- e.g. review-cli#422's unrecognized `role:`)
    is a WARNING, not a hard failure, when the operator named MODELS directly via
    explicit `-m`: losing that ONE entry's role/name/effort METADATA is recoverable, so
    this retries with the offending `board:` entries dropped rather than aborting an
    explicit `-m` request over unrelated/malformed board metadata. The retry only drops
    entries that actually fail to parse (`_drop_malformed_board_entries`) -- an
    unrelated bad-role entry elsewhere in `board:` must not also erase metadata for an
    entry that DOES match the requested models and IS valid (Fable review finding,
    review-cli#422: `board_from_models` used to wipe the WHOLE config's board metadata,
    including perfectly valid entries, on any single unrelated typo). Shared by every
    explicit-`-m` call site (the --task panel role-pad, `validate_board`, `_show_board`)
    so the fallback message/behavior can't drift between them.

    The sanitized retry is guaranteed to succeed: `_parse_board_entry`'s only raise
    path is `BoardConfigError` on `role:` validation, and the sanitizer drops every
    entry that raises it, so nothing is left for `board_from_models` to reject."""
    try:
        return _api.board_from_models(models, config, preset=preset)
    except _api.BoardConfigError as exc:
        print(
            f"[review-cli] {exc}; ignoring malformed board metadata for explicit -m",
            file=_api.sys.stderr,
            flush=True,
        )
        return _api.board_from_models(
            models, _api._drop_malformed_board_entries(config), preset=preset
        )


def _default_review_board(_api, /, config: dict, config_models: list[str], default_pool: int):
    """The board a no-`-m`/no-`--pool` run would use, with its default pool size — the
    precedence the guard offers as the 'drop your override' fallback (config `models:` >
    config `board:` > the default level). `default_pool` is the pool size a no-override run
    would ACTUALLY use (config `pool:` > level default), so a proposal's promised size
    matches what re-running would do."""
    if config_models:
        return _api.board_from_models(config_models, config), default_pool
    return _api.load_board(config), default_pool  # a config `board:`, else the default level


def _pool_guard_candidates(_api, /, config: dict, config_models: list[str], default_pool: int) -> list[Candidate]:
    """Fallback options the guard proposes when a selection can't converge: the default
    board plus every preset. Boards are built (cheap, no availability probe — the guard
    probes lazily only on the non-converge path); a malformed board is skipped, not fatal.
    A preset identical to the default board is de-duped so the proposal isn't redundant."""
    candidates: list[Candidate] = []
    default_seats: tuple[tuple[str, str], ...] | None = None
    try:
        board, pool = _api._default_review_board(config, config_models, default_pool)
        default_seats = _api._seats_of(board)
        candidates.append(
            _api.Candidate(
                label="default",
                why="drop -m / --pool and run the default board (`review diff`)",
                board=default_seats,
                pool_size=pool,
            )
        )
    except _api.BoardConfigError:
        pass
    for name in _api.preset_names(config):
        try:
            seats = _api._seats_of(_api.load_board(config, preset=name))
        except _api.BoardConfigError:
            continue
        if seats == default_seats:
            continue
        candidates.append(
            _api.Candidate(
                label=f"level:{name}",
                why=f"use the {name!r} level (`review diff --level {name}`)",
                board=seats,
                pool_size=_api.preset_pool_size(name, config),
            )
        )
    return candidates


def _announce_quorum_auto_roles(_api, /, seats: list) -> None:
    """Operator-facing stderr notice for "which model filled which REVIEW_ROLES
    role" when quorum's default (no `-m`, no config `models:`/`board:`) path
    auto-assigns its own board (see `DEFAULT_BOARD` fallback in `_dispatch`) --
    otherwise nothing surfaces the mapping before the panel itself runs, and an
    operator has no way to tell which live model is speaking for which role."""
    if not seats:
        return
    pairs = ", ".join(f"{r.model} -> {r.role}" for r in seats)
    print(
        f"[review-cli] quorum auto-assigned roles (no -m): {pairs}",
        file=_api.sys.stderr,
        flush=True,
    )


def _warn_if_panel_padded(_api, /, models: list[str]) -> None:
    """Operator-facing stderr notice for the flat quorum panel, fired iff
    `models` contains a repeat -- the only way a repeat can appear here is
    `expand_flat_models_with_reuse` reusing a model, since its input `src`
    (DEFAULT_MODELS or config `models:`) is already distinct and `-m` never
    reaches this call site (`explicit_models` is handled by an earlier,
    separate branch that bypasses padding entirely). Mirrors the board path's
    existing failover promotion message (reviewlib.panel.run_board_with_failover)
    so the token spend is attributable instead of a silent surprise."""
    if len(set(models)) == len(models):
        return
    counts = {m: models.count(m) for m in dict.fromkeys(models)}
    repeated = ", ".join(f"{m} x{n}" for m, n in counts.items() if n > 1)
    print(
        f"[review-cli] panel padded — reusing {repeated} across multiple seats "
        "(some models near their usage limit)",
        file=_api.sys.stderr,
        flush=True,
    )


def _chain_aware_available(_api, /, model: str) -> bool:
    """The failover-chain-aware liveness probe: True iff `model` OR any later provider in
    its `reviewlib.provider_failover` chain is reachable and paid.

    This is THE single liveness predicate every pre-dispatch decision about a seat must
    share — the pool guard, the startup pool/reserve SPLIT (`select_pool_and_reserve_with_reuse`,
    both here for the ETA and in `modes.review._mode_review_board` for the REAL dispatch), and
    the ETA's `planned_pool`. Before this fix the guard alone became chain-aware while the split
    stayed on raw `backend_available`, so the guard could approve a pool size the split then
    silently shrank (a two-seat board where one seat's head is down but its failover
    alternate is live: the guard says 'fine, 2 live', the split — still raw — says '1 live',
    and dispatch quietly runs a smaller pool than what was approved). Codex P1 on review of
    #157: 'the chain-aware guard approves seats that board dispatch still removes.'"""
    from .provider_failover import any_provider_available

    return any_provider_available(
        model,
        available=_api.backends.backend_available,
        unpaid=_api.backends.runtime_provider_marked_unpaid,
    )


def _evaluate_pool_or_bail(_api, /, config: dict, config_models: list[str], user_seats: tuple[tuple[str, str], ...], explicit_models: list[str], pool_arg: int | None, default_pool: int, reuse_eligible: bool) -> int | None:
    """Pre-dispatch foolproofing (reviewlib.pool_guard): when the resolved review selection
    (`user_seats` = (model, name) pairs — a board OR the flat `-m` list) can't converge,
    print a proposal / targeted error and return its exit code; otherwise return None to
    proceed. Inert under the fake backend (every seat live -> PROCEED).

    `reuse_eligible` (task-cli#139): pass True ONLY when the real dispatch this call gates
    will actually use `select_pool_and_reserve_with_reuse` — a role-lensed board, including
    a recorded `--task` one-model `-m` pad (review-cli#309). See `evaluate_selection`'s
    own docstring for why this relaxes the convergence floor to "at least 1 live model"
    instead of the distinct-model count."""
    # A config `pool: N` default is a SOFT target (graceful auto-shrink), NOT a hard request:
    # only an EXPLICIT `-m` / `--pool N` sets `explicit=True` so the guard enforces the
    # requested size and proposes when the live subset can't fill it. A bare `review diff`
    # with a config `pool:` still runs the live board (down to the min_converge floor) rather
    # than nagging — an explicit flag is a deliberate ask; a config default is a preference.
    if explicit_models:
        requested, explicit = (
            len({_api.default_distinct_key(m) for m in explicit_models}),
            True,
        )
    elif pool_arg is not None and pool_arg > 0:
        requested, explicit = pool_arg, True
    else:
        requested, explicit = 0, False

    def _guard_reason(model: str) -> str | None:
        # `backend_unavailable_reason` probes only the requested (head) spelling, and — by
        # construction — already agrees with `_chain_aware_available` whenever the head
        # itself is what's down (it embeds the same `runtime_provider_marked_unpaid` check
        # `any_provider_available` uses). The one gap: if it ever returned None while
        # `_chain_aware_available` says the WHOLE chain is down (every provider
        # unavailable/unpaid, a shape only reachable with mismatched injected predicates
        # today, e.g. in a test), the guard would bail with a blank reason. Fail safe with a
        # synthesized one rather than ship a misleading empty message (review of #157).
        reason = _api.backends.backend_unavailable_reason(model)
        if reason is not None or _api._chain_aware_available(model):
            return reason
        return f"{model}: no provider in its failover chain is currently available"

    decision = _api.evaluate_selection(
        user_board=user_seats,
        requested_size=requested,
        explicit=explicit,
        # THUNK: the fallback boards (default + presets) are re-resolved only on the
        # non-converge path, so a converging happy-path run never rebuilds them (and never
        # re-emits load_board's stderr warnings for a malformed config board — glm review).
        candidates=lambda: _api._pool_guard_candidates(config, config_models, default_pool),
        available=_api._chain_aware_available,
        reason=_guard_reason,
        reuse_eligible=reuse_eligible,
    )
    if decision.kind == _api.PROCEED:
        return None
    print(decision.text, file=_api.sys.stderr, flush=True)
    return decision.exit_code
