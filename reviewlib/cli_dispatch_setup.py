"""Parse CLI input and validate policy before any model or visual operation."""
from __future__ import annotations

from .cli_dispatch_state import CONTINUE


def _install_commit_hook(_api, rest):
    if not rest:
        return _api.install_commit_hook()
    if rest == ["--print-gate"]:
        return _api.print_review_gate()
    print("usage: review install-commit-hook [--print-gate]", file=_api.sys.stderr)
    raise SystemExit(2)


def _install_hook(_api, rest):
    if rest == ["tg"]:
        return _api.install_hook_tg()
    print("usage: review install-hook tg", file=_api.sys.stderr)
    return 2


def _trust_module(_api, rest):
    from .features.visual.registry import trust_module

    if not rest:
        print("usage: review trust-module <name> [--project DIR]  (only needed under REVIEW_UNTRUSTED_MODULES=1)", file=_api.sys.stderr)
        return 2
    project = None
    if "--project" in rest[1:]:
        index = rest.index("--project", 1)
        project = _api.Path(rest[index + 1]).expanduser() if index + 1 < len(rest) else None
    return trust_module(rest[0], project=project)


def _register_module(_api, rest):
    from .features.visual.registry import register_module

    if not rest:
        print("usage: review register-module <path-to-manifest>", file=_api.sys.stderr)
        return 2
    return register_module(rest[0])


def _exact_management_command(_api, argv):
    if argv == ["--version"]:
        print(f"review {_api.resolve_version()}")
        return 0
    if argv == ["install-skill"]:
        return _api.install_skill()
    if argv == ["--reviewlib-dir"]:
        import reviewlib

        print(_api.Path(reviewlib.__file__).resolve().parent)
        return 0
    return CONTINUE


def management_commands(_api, state):
    """Management verbs do not enter review policy resolution or model dispatch."""
    raw = _api.sys.argv[1:] if state.argv is None else state.argv
    state.argv = _api._normalize_leading_mode_options(list(raw))
    exact = _exact_management_command(_api, state.argv)
    if exact is not CONTINUE or not state.argv:
        return exact
    verb, rest = state.argv[0], state.argv[1:]
    if verb in ("--help", "-h") and rest and not rest[0].startswith("-"):
        return _api._help_subcommand(rest)
    if verb in ("seats", "doctor"):
        return _api._seats_subcommand(verb, rest)
    special = {"install-commit-hook": _install_commit_hook, "install-hook": _install_hook,
               "trust-module": _trust_module, "register-module": _register_module}
    if verb in special:
        return special[verb](_api, rest)
    commands = {
        "help": _api._help_subcommand, "dashboard": _api._dashboard_subcommand,
        "sessions": _api._sessions_subcommand, "task": _api._task_subcommand,
        "jobs": _api._jobs_subcommand, "status": _api._status_subcommand,
        "wait": _api._wait_subcommand, "stat": _api._stat_subcommand,
        "health": _api._health_subcommand, "entities": _api._entities_subcommand,
        "capacity-check": _api._capacity_check_subcommand,
        "feedback": _api._feedback_subcommand, "spec-web": _api._spec_web,
    }
    handler = commands.get(verb)
    return handler(rest) if handler else CONTINUE


def parse_mode_arguments(_api, state):
    """Reject obsolete syntax before selecting the exact subcommand parser."""
    for reject in (_api._reject_removed_flags, _api._reject_removed_subcommand):
        rc = reject(state.argv)
        if rc is not None:
            return rc
    state.is_subcommand = bool(state.argv) and not state.argv[0].startswith("-") and state.argv[0] in _api.known_subcommands()
    if state.is_subcommand:
        state.mode = _api.get_mode(state.argv[0])
        assert state.mode is not None
        state.rest = state.argv[1:]
        state.parser = _api._build_mode_parser(state.mode)
    else:
        rc = _api._reject_subcommand_only_flag_without_verb(state.argv)
        if rc is not None:
            return rc
        state.mode = _api.diff_mode()
        state.rest = state.argv
        state.parser = _api._build_top_level_parser()
    state.args = state.parser.parse_args(state.rest)
    return CONTINUE


def load_runtime_policy(_api, state):
    """Apply the existing paid-provider, account and transport restrictions."""
    if getattr(state.args, "retry", None) is not None:
        _api.os.environ["REVIEW_RETRY_COUNT"] = str(max(0, min(state.args.retry, _api.max_retry_count())))
    try:
        state.effort_override = _api.parse_effort_flag(getattr(state.args, "effort", None))
    except _api.EffortValueError as exc:
        print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    state.config = _api.load_config()
    _api.backends.configure_unpaid_providers(state.config.get("unpaid_providers"))
    _api.backends.configure_omp_bin(state.config.get("omp_bin"))
    _api.backends.configure_omp_accounts(state.config.get("omp_accounts"))
    _api.account_cap.apply(state.config)
    from .provider_failover import configure_provider_chains

    configure_provider_chains(state.config.get("provider_chains"))
    try:
        _api.preset_names(state.config)
    except _api.BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    return CONTINUE


def _review_level(_api, state):
    if state.mode.name != "review":
        return None
    if state.explicit_preset:
        return state.args.level
    if state.explicit_models or state.config_models or state.config_has_board:
        return None
    return _api.default_level_name(state.config)


def _pool_size(_api, state):
    if state.args.pool is not None:
        return state.args.pool
    configured = _api._config_default_pool(state.config)
    panel_level = state.selected_panel.level if state.selected_panel else None
    chosen = panel_level or state.active_preset
    if state.explicit_preset and chosen is not None:
        return _api.preset_pool_size(chosen, state.config)
    if configured is not None:
        return configured
    return _api.preset_pool_size(chosen, state.config)


def select_capability(_api, state):
    """One resolution supplies diagnostic output, actual dispatch and statistics."""
    state.config_models = _api._split_models(state.config.get("models") or [])
    state.config_visual_models = _api._split_models(state.config.get("visual_models") or [])
    state.config_has_board = isinstance(state.config.get("board"), list) and bool(state.config.get("board"))
    state.explicit_models = _api._split_models(state.args.model)
    state.explicit_preset = getattr(state.args, "level", None) is not None
    state.selected_panel = None
    if state.mode.name in _api.TEXT_PANEL_MODES:
        try:
            state.selected_panel = _api.resolve_panel_selection(state.config, state.mode.name,
                level=getattr(state.args, "level", None), explicit_models=state.explicit_models)
        except _api.BoardConfigError as exc:
            print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
            return 2
    state.active_preset = _review_level(_api, state)
    panel_level = state.selected_panel.level if state.selected_panel else None
    state.recorded_level = panel_level or state.active_preset or getattr(state.args, "level", None)
    state.effective_pool_size = _pool_size(_api, state)
    return CONTINUE


def _diagnostic_models(_api, state):
    if state.explicit_models:
        return state.explicit_models
    if state.selected_panel is not None:
        return list(state.selected_panel.models)
    if state.mode.name == "visual":
        return state.config_visual_models or [_api._expand_alias(x) for x in _api.VISUAL_MODELS]
    if state.mode.name == "review":
        return _diagnostic_review_models(_api, state)
    return state.config_models or [_api._expand_alias(x) for x in _api.DEFAULT_MODELS]


def _diagnostic_review_models(_api, state):
    if state.active_preset is not None:
        return [seat.model for seat in _api.load_board(state.config, preset=state.active_preset)]
    if state.config_models:
        return state.config_models
    if state.config_has_board:
        return [seat.model for seat in _api.load_board(state.config)]
    return [_api._expand_alias(model) for model in _api.DEFAULT_MODELS]


def list_configured_models(_api, state):
    if not state.args.list_defaults:
        return CONTINUE
    try:
        print("\n".join(_diagnostic_models(_api, state)))
    except _api.BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    return 0


def normalize_early_task(_api, state):
    state.raw_early_task = getattr(state.args, "task", None) or _api.os.environ.get("REVIEW_TASK_CODE")
    state.early_task = None
    if state.raw_early_task:
        try:
            state.early_task = _api.normalize_task_code(state.raw_early_task)
        except ValueError:
            pass
    return CONTINUE


def _configured_roster_length(state):
    if state.config_models:
        return len(state.config_models)
    if state.config_has_board:
        return len(state.config.get("board") or [])
    return None


def _warn_uniform_roles(_api, state):
    try:
        if state.config_models:
            roster = _api.board_from_models(state.config_models, state.config)
        elif state.config_has_board:
            roster = _api.load_board(state.config)
        else:
            return
    except _api.BoardConfigError:
        return
    known = [r for r in roster if r.role_lens]
    count = len({r.role for r in known})
    if known and len(known) == len(roster) and count < _api.SHIP_MIN_ROLES:
        print(f'[review-cli] warning: --pool 0 ("all seats") with only {count} distinct board role(s) is below the default ship floor of {_api.SHIP_MIN_ROLES} distinct board roles; this run cannot satisfy `review task CODE --check` / gh ship.', file=_api.sys.stderr, flush=True)


def _warn_explicit_pool(_api, state):
    count = len(state.explicit_models) if state.explicit_models else state.config_roster_len
    zero_below = state.args.pool <= 0 and count is not None and count < _api.SHIP_MIN_ROLES
    if 0 < state.args.pool < _api.SHIP_MIN_ROLES or zero_below:
        subject = f'--pool {state.args.pool} ("all seats") with only {count} available model/seat(s)' if zero_below else f"--pool {state.args.pool}"
        print(f"[review-cli] warning: {subject} is below the default ship floor of {_api.SHIP_MIN_ROLES} distinct board roles; this run cannot satisfy `review task CODE --check` / gh ship.", file=_api.sys.stderr, flush=True)
    elif state.args.pool <= 0 and not state.explicit_models:
        _warn_uniform_roles(_api, state)


def check_review_role_floor(_api, state):
    if not state.early_task or state.mode.name != "review":
        return CONTINUE
    state.config_roster_len = _configured_roster_length(state)
    if state.args.pool is not None:
        _warn_explicit_pool(_api, state)
    else:
        state.effective_pool_size = _api.recorded_task_pool_size(state.effective_pool_size, explicit_pool=False, recorded=True)
    short = not state.explicit_models and state.config_roster_len is not None and state.config_roster_len <= _api.SHIP_MIN_ROLES
    if (len(state.explicit_models) == 1 or short) and _api._no_deliberate_pool_choice(state.args, state.explicit_preset, state.config) and state.effective_pool_size > 1:
        state.effective_pool_size = _api.SHIP_MIN_ROLES
    return CONTINUE


def show_review_board(_api, state):
    if not state.args.show_board:
        return CONTINUE
    panel_level = state.selected_panel.level if state.selected_panel else None
    pad = bool(state.early_task and state.mode.name == "review" and
               (len(state.explicit_models) <= 1 and state.effective_pool_size > 1 or
                not state.explicit_models and state.effective_pool_size <= 0))
    return _api._show_board(state.config, state.effective_pool_size,
        _api._effective_cwd(state.args.cwd), preset=panel_level or state.active_preset,
        explicit_models=state.explicit_models, pad_task_roles=pad)


def validate_task_identity(_api, state):
    explicit = _api._explicit_diff_source(state.args)
    if explicit is not None:
        _api._warn_if_stdin_ignored(*explicit)
        state.piped_input = None
    else:
        state.piped_input = _api._read_stdin_if_piped()
    state.task_code = None
    needs_task = state.is_subcommand and (state.mode.name != "visual" or
        getattr(state.args, "diff", False) or getattr(state.args, "staged", False) or state.piped_input is not None)
    if not needs_task:
        return CONTINUE
    raw = getattr(state.args, "task", None) or _api.os.environ.get("REVIEW_TASK_CODE")
    repo = _api._effective_cwd(state.args.cwd, warn=False) if raw else None
    try:
        state.task_code = _api.resolve_task_code(raw, repo)
    except ValueError as exc:
        print(f"[review-cli] invalid --task CODE: {exc}", file=_api.sys.stderr, flush=True)
        return 2
    if state.task_code is None:
        print("[review-cli] missing required --task CODE for review iteration history.\n  use: review <mode> --task CODE ...\n  or set REVIEW_TASK_CODE=CODE for automation.", file=_api.sys.stderr, flush=True)
        return 2
    state.args.task = state.task_code
    return CONTINUE


def _bare_invocation(_api, state):
    piped = not state.rest and state.piped_input is not None
    state.parser.print_help(_api.sys.stderr if state.rest or piped else _api.sys.stdout)
    if piped:
        print("\nreview: a diff was piped in but no subcommand given. The diff review is now `review diff` (a bare `review` no longer runs one). Run `git diff | review diff --task CODE`.", file=_api.sys.stderr, flush=True)
        raise SystemExit(2)
    if state.rest:
        print("\nreview: no subcommand given. The diff review is now `review diff` (a bare `review` no longer runs one). Run `review diff --task CODE [options]`.", file=_api.sys.stderr, flush=True)
        raise SystemExit(2)
    raise SystemExit(0)


def _preflight(_api, state):
    if state.mode.name not in ("review", "quorum", "brainstorm", "just-ask"):
        return
    from . import health

    if not health.preflight_enabled():
        return
    try:
        _api._apply_runtime_config(state.config)
        selected = state.selected_panel.level if state.selected_panel else state.active_preset
        health.preflight(_api._health_seats(state.config, state.mode.name, selected, state.explicit_models))
    except Exception:
        pass  # Existing advisory preflight; dispatch/account admission stays authoritative.


def validate_mode_context(_api, state):
    if not state.is_subcommand:
        return _bare_invocation(_api, state)
    if state.mode.name == "visual" and not state.args.visual:
        state.parser.error("the following arguments are required: IMAGE")
    _preflight(_api, state)
    required = state.mode.name == "review" and (state.args.staged or state.args.visual is None)
    state.cwd = _api._effective_cwd(state.args.cwd, warn=not required)
    state.is_brainstorm = state.mode.name == "brainstorm"
    state.is_visual_subcommand = state.mode.name == "visual"
    state.panel_mode = state.mode.name not in ("review", "visual")
    return CONTINUE
