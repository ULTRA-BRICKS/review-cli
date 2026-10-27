"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import BoardReviewer

def _entity_harness_for_model(_api, /, model: str) -> str:
    """The harness display name a board seat's `model` string actually dispatches
    through, via the real `backends.resolve_backend` router -- never a second guess at
    the prefix rules (see `_ENTITY_HARNESS_LABELS`)."""
    fn = _api.backends.resolve_backend(model)
    return _api._ENTITY_HARNESS_LABELS.get(getattr(fn, "__name__", ""), "unknown")


def _apply_runtime_config(_api, /, config: dict) -> None:
    """Apply the config's unpaid providers, omp settings and provider chains so
    availability and failover read the same as a real run."""
    _api.backends.configure_unpaid_providers(config.get("unpaid_providers"))
    _api.backends.configure_omp_bin(config.get("omp_bin"))
    _api.backends.configure_omp_accounts(config.get("omp_accounts"))
    from .provider_failover import configure_provider_chains

    configure_provider_chains(config.get("provider_chains"))


def _configured_board(_api, /, config: dict) -> list[BoardReviewer]:
    """The board `review diff` would use from `config` (its `models:` roster, else its
    `board:`/the default board), with the config's unpaid providers and provider chains
    applied so availability and failover read the same as a real run. Raises
    BoardConfigError for a malformed `board:`."""
    _api._apply_runtime_config(config)
    config_models = _api._split_models(config.get("models") or [])
    return (
        _api.board_from_models(config_models, config)
        if config_models
        else _api.load_board(config)
    )


def _omp_usage_text(_api, /, windows) -> str:
    """`5 Hour 10% · 7 Day 42%` -- a window at 100% also says when it resets."""
    from .omp_accounts import utc_stamp

    parts = []
    for w in windows:
        text = f"{w.label.removeprefix('Claude ')} {round(w.used_fraction * 100)}%"
        if w.used_fraction >= 1 and w.resets_at_ms is not None:
            text += f" (resets {utc_stamp(w.resets_at_ms)})"
        parts.append(text)
    return " · ".join(parts)


def _omp_account_json(_api, account_state, policy):
    s = account_state
    usage = None if s.usage is None else [
        {"label": window.label, "used_fraction": window.used_fraction,
         "resets_at_ms": window.resets_at_ms, "tier": window.tier}
        for window in s.usage
    ]
    return {
        "position": s.account.position, "credential_id": s.account.credential_id,
        "email": s.account.email, "available": s.is_available(_api.time.time()),
        "availability": s.availability(_api.time.time()), "skip_reason": s.skip_reason,
        "tier_over_limit": list(s.tier_over_limit),
        "max_usage_percent": policy.threshold_for(s.account.email),
        "omp_blocks": [{"scope": scope, "until_ms": until} for scope, until in s.account.blocks],
        "limited_until": s.limited_until, "limited_reason": s.limited_reason, "usage": usage,
    }


def _omp_pool_json(_api, /, state) -> dict:
    launcher = state.launcher
    policy = state.policy
    return {
        "launcher": launcher.argv0 if launcher else None,
        "launcher_source": launcher.source if launcher else None,
        "real_bin": launcher.real_bin if launcher else None,
        "launcher_problem": state.launcher_problem,
        "auth_db": str(state.db),
        "account_policy": {
            "exclude": list(policy.exclude),
            "exclude_source": policy.exclude_source or None,
            "max_usage_percent": policy.max_usage_percent,
            "max_usage_source": policy.max_usage_source or None,
            "accounts": {
                email: {"max_usage_percent": percent}
                for email, percent in policy.per_account
            },
            "problems": list(policy.problems),
        },
        "anthropic_accounts": None
        if state.accounts is None
        else [_omp_account_json(_api, s, policy) for s in state.accounts],
    }


def _omp_pool_lines(_api, /, state) -> list[str]:
    """The `review status` section for omp:anthropic seats (review-cli#636/#637)."""
    lines = [
        "\nOMP ANTHROPIC ACCOUNTS (omp:anthropic/* seats try these one per attempt, "
        "available first; next account on a rate limit or failed login; EXCLUDED and "
        "OVER LIMIT accounts are never tried)"
    ]
    launcher = state.launcher
    if launcher is None:
        lines.append(
            f"  launcher: none, omp: seats cannot start -- {state.launcher_problem}"
        )
    elif launcher.real_bin:
        lines.append(
            f"  launcher: {launcher.argv0} ({launcher.source}; wrapper, real binary "
            f"{launcher.real_bin} passed to seats as OMP_REAL_BIN)"
        )
    else:
        lines.append(f"  launcher: {launcher.argv0} ({launcher.source})")
    policy = state.policy
    lines.append(f"  account policy: {policy.summary()}")
    for problem in policy.problems:
        lines.append(
            f"  INVALID SETTING: {problem} -- omp:anthropic seats use no account "
            "until it is fixed"
        )
    if not state.accounts:
        where = (
            f"no readable omp auth db at {state.db}"
            if state.accounts is None
            else f"none in {state.db}"
        )
        then = (
            "omp:anthropic seats fail: the account policy cannot be applied"
            if policy.restricts
            else "omp picks the account"
        )
        lines.append(f"  {where} -- {then}")
        return lines
    now = _api.time.time()
    email_w = max(len(s.account.label) for s in state.accounts)
    for s in state.accounts:
        usage = (
            "usage unknown (omp usage did not answer)"
            if s.usage is None
            else _api._omp_usage_text(s.usage) or "no usage windows reported"
        )
        lines.append(
            f"  {s.account.position}  {s.account.label.ljust(email_w)}  "
            f"credential {s.account.credential_id:<3} {s.availability(now)}  [{usage}]"
        )
    return lines


def _seat_status_line(reviewer, health, name_width, model_width):
    head = f"  {reviewer.display.ljust(name_width)}  {(reviewer.role or 'general'):<12} {reviewer.model.ljust(model_width)}"
    if health.reason is None:
        return f"{head}  usable"
    then = f"answers via {health.substitute}" if health.substitute else "no substitute executor"
    return f"{head}  UNUSABLE: {health.reason} -> {then}"


def _account_login_line(account):
    marker = "*" if account.current else " "
    if account.logged_in is False:
        status = f"LOGGED OUT -- log it back in: {account.login_command}"
    elif account.login_failure:
        status = f"LOGIN FAILED {account.login_failed_at_utc} ({account.login_failure}) -- log it back in: {account.login_command}"
    elif account.logged_in:
        status = "logged in"
    else:
        status = "login state unknown (no `claude` CLI answered `claude auth status`)"
    return f"  {marker} {account.name:<12} {(account.email or '(no email on disk)'):<28} {status}"


def _seat_status_report(_api, /, *, as_json: bool) -> int:
    """Bare `review status` (review-cli#594): every board seat as usable or UNUSABLE with
    the reason (unpaid provider, missing CLI/login, an active cooldown, every Claude CLI
    account logged out or failed its last login) and the executor its failover would hand
    the seat to, plus each registered Claude CLI account's login state -- including a
    login failure a call recorded (review-cli#596) -- with the command that logs it back
    in, and the omp launcher plus each omp Anthropic account's availability and usage
    (review-cli#636/#637). `review seats` and `review doctor` print the same report.
    Read-only: no model is called and no login is started."""
    from .seat_health import claude_accounts_state, omp_pool_state, seat_health

    try:
        board = _api._configured_board(_api.load_config())
    except _api.BoardConfigError as exc:
        print(f"[review status] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    accounts = claude_accounts_state()
    from . import harness_accounts

    logins = harness_accounts.discover()
    omp_pool = omp_pool_state()
    seats = [(r, seat_health(r.model, accounts, omp_pool=omp_pool)) for r in board]

    if as_json:
        import json as _json

        payload = {
            "seats": [
                {
                    "seat": r.model,
                    "display": r.display,
                    "role": r.role,
                    "usable": h.reason is None,
                    "reason": h.reason,
                    "substitute": h.substitute,
                }
                for r, h in seats
            ],
            "claude_accounts": [
                {
                    "name": a.name,
                    "email": a.email,
                    "current": a.current,
                    "logged_in": a.logged_in,
                    "login_failure": a.login_failure,
                    "login_failed_at": a.login_failed_at_utc,
                    "login_command": a.login_command,
                }
                for a in accounts
            ],
            "omp": _api._omp_pool_json(omp_pool),
            "harness_accounts": harness_accounts.as_json(logins),
            "admission": _api.admission.status_snapshot(),
        }
        print(_json.dumps(payload, indent=2))
        return 0

    lines = [
        "review status -- which board seats can run right now "
        "(read-only: no model is called)\n",
        "SEATS (board order)",
    ]
    name_w = max((len(r.display) for r, _h in seats), default=0)
    model_w = max((len(r.model) for r, _h in seats), default=0)
    lines.extend(_seat_status_line(r, h, name_w, model_w) for r, h in seats)

    lines.append("\nCLAUDE CLI ACCOUNTS (the claude:* seats rotate through these)")
    if not accounts:
        lines.append(
            "  none registered -- single implicit account (see HARNESS ACCOUNTS)"
        )
    lines.extend(_account_login_line(account) for account in accounts)
    lines.extend(_api._omp_pool_lines(omp_pool))
    lines.extend(harness_accounts.render(logins))
    lines.append("")
    lines.append(_api.admission.render_status(_api.admission.status_snapshot()))
    print("\n".join(lines))
    return 0


def _health_seats(_api, /, config: dict, mode_name: str, preset: str | None, explicit_models: list[str]) -> list[str]:
    """The seats a run of `mode_name` may use, for the health check: the explicit `-m`
    list, else the diff review's board (the preset's, or the configured one), else a panel
    mode's configured/default model list. Raises BoardConfigError for a malformed board."""
    if mode_name in _api.TEXT_PANEL_MODES:
        return list(_api.resolve_panel_selection(
            config, mode_name, level=preset, explicit_models=explicit_models,
        ).models)
    if explicit_models:
        return list(explicit_models)
    if mode_name == "review":
        board = (
            _api.load_board(config, preset=preset) if preset else _api._configured_board(config)
        )
        return [r.model for r in board]
    src = (
        _api._split_models(config.get("brainstorm_models") or [])
        if mode_name == "brainstorm"
        else []
    ) or _api._split_models(config.get("models") or [])
    return list(src or [_api._expand_alias(x) for x in _api.DEFAULT_MODELS])


def _health_subcommand(_api, /, rest: list[str]) -> int:
    """`review health [--seat X]... [--preset P] [--host H]... [--json] [--timeout S]
    [--cached] [--no-probe]` -- probe every seat of the active board in parallel with the
    cheapest call it offers (default 5 s timeout), record what each failure says about
    when the seat answers again, and print the result (`review help health`). Exit 0 when
    at least one seat is ok, 1 when none is. `--host H` runs the same check on another
    machine over ssh (its own logins, its own store); repeat it for several hosts, and use
    `--host local` to include this one."""
    from . import health

    parser = _api.argparse.ArgumentParser(
        prog="review health",
        description="Probe review seats now and record when each unavailable seat "
        "answers again. See `review help health`.",
    )
    parser.add_argument(
        "--seat",
        "-m",
        action="append",
        default=[],
        metavar="MODEL",
        help="seat to check (repeatable); default: every seat of the active board",
    )
    parser.add_argument(
        "--level",
        "--preset",
        dest="preset",
        type=_api._level_arg,
        default=None,
        help="check this level's board instead (`--preset` is the older spelling)",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="HOST",
        help="run the check on HOST over ssh (repeatable; `local` = this machine)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="S",
        help=f"per-seat probe timeout (default {health.DEFAULT_TIMEOUT_SECONDS:g}s, "
        f"${health._ENV_TIMEOUT})",
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help=f"let a probe newer than ${health._ENV_TTL} "
        f"({health.DEFAULT_TTL_SECONDS:g}s) answer instead of probing again",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="report only what the store and static checks know; call no model",
    )
    ns = parser.parse_args(rest)
    timeout = ns.timeout  # None = automatic per seat (`health.timeout_for`)
    live = not ns.no_probe
    bad_host = next((h for h in ns.host if h.startswith("-")), None)
    if bad_host:
        print(f"[review health] invalid --host {bad_host!r}", file=_api.sys.stderr)
        return 2

    remote_args = _remote_health_arguments(ns, timeout, live)
    remote_hosts = [host for host in ns.host if host != "local"]
    reports = _collect_health_reports(_api, health, ns, timeout, live, remote_hosts, remote_args)
    if reports and reports[0].get("error") and not remote_hosts:
        print(f"[review health] {reports[0]['error']}", file=_api.sys.stderr, flush=True)
        return 2

    _print_health_reports(health, reports, ns.json)
    if not live:
        return 0  # nothing was probed: the exit status has no health to report
    ok = any(s["status"] == health.OK for rep in reports for s in rep.get("seats", []))
    return 0 if ok else 1


def _remote_health_arguments(ns, timeout, live):
    args = [item for seat in ns.seat for item in ("--seat", seat)]
    if ns.preset:
        args += ["--level", ns.preset]
    if timeout is not None:
        args += ["--timeout", f"{timeout:g}"]
    if ns.cached:
        args.append("--cached")
    if not live:
        args.append("--no-probe")
    return args


def _collect_health_reports(_api, health, ns, timeout, live, remote_hosts, remote_args):
    if not remote_hosts:
        return [_api._local_health_report(ns, timeout, live)]
    from concurrent.futures import ThreadPoolExecutor

    reports = []
    with ThreadPoolExecutor(max_workers=len(remote_hosts)) as pool:
        futures = [pool.submit(health.run_remote, host, remote_args,
                              timeout=timeout or health.AGENTIC_TIMEOUT_SECONDS)
                   for host in remote_hosts]
        if not ns.host or "local" in ns.host:
            reports.append(_api._local_health_report(ns, timeout, live))
        reports.extend(future.result() for future in futures)
    return reports


def _local_health_report(_api, /, ns, timeout: float, live: bool) -> dict:
    """This machine's `review health` report (or a report-shaped `error`)."""
    import socket

    from . import health

    config = _api.load_config()
    _api._apply_runtime_config(config)
    host = socket.gethostname()
    try:
        seats = _api._health_seats(
            config, "review", ns.preset, [_api._expand_alias(s) for s in ns.seat]
        )
    except _api.BoardConfigError as exc:
        return {"host": host, "error": str(exc)}
    results = health.probe_seats(
        seats,
        timeout=timeout,
        ttl=health.default_ttl() if ns.cached else 0.0,
        live=live,
    )
    return health.report(results, host=host, timeout=timeout, live=live)


def _entities_subcommand(_api, /, rest: list[str]) -> int:
    """`review entities [--json]` -- read-only structural overview of review-cli's own
    configured entities (harnesses, providers/vendors, models, accounts, effort/role
    lenses) and how they relate, plus a compact per-harness call-health line from the
    real call logs (last 7 days).

    This is the Phase-0 list-view slice of the fuller interactive entity/relationship
    browser tracked in review-cli#487 -- it prints a structured report once and exits;
    it does not yet support drill-down or live refresh (later phases). Every field comes
    from data review-cli already owns (the resolved board, `role_models`,
    `claude_accounts`, and the real call-log stats `review stat` also reads) plus one
    local `claude auth status` per registered Claude CLI account (review-cli#594: seat
    availability is `seat_health`'s reason, the same one bare `review status` prints).
    Read-only -- no model is called, no key is printed."""
    parser = _api.argparse.ArgumentParser(
        prog="review entities",
        description="Read-only overview of review-cli's harnesses/providers/models/accounts/efforts.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)

    try:
        board = _api._configured_board(_api.load_config())
    except _api.BoardConfigError as exc:
        print(f"[review entities] {exc}", file=_api.sys.stderr, flush=True)
        return 2

    from .role_models import ROLE_FALLBACK_MODELS
    from .dashboard.tokenstats import compute_stat_report
    from .seat_health import claude_accounts_state, omp_pool_state, seat_health

    account_state = claude_accounts_state()
    # review-cli#641: an omp seat (or a failover target on omp) the omp account policy
    # leaves no account for is not available
    omp_pool = omp_pool_state()

    harnesses: dict[str, dict] = {}
    models: list[dict] = []
    for r in board:
        harness = _api._entity_harness_for_model(r.model)
        provider = _api.backends.effective_provider(r.model)
        health = seat_health(r.model, account_state, omp_pool=omp_pool)
        entry = harnesses.setdefault(harness, {"providers": set(), "seats": 0})
        entry["providers"].add(provider)
        entry["seats"] += 1
        models.append(
            {
                "seat": r.model,
                "display": r.display,
                "harness": harness,
                "provider": provider,
                "role": r.role,
                "effort": r.effort,
                "available": health.reason is None,
                "reason": health.reason,
                "substitute": health.substitute,
            }
        )

    accounts = [
        {
            "name": a.name,
            "email": a.email,
            "current": a.current,
            "logged_in": a.logged_in,
            "login_failure": a.login_failure,
            "login_failed_at": a.login_failed_at_utc,
            "login_command": a.login_command,
        }
        for a in account_state
    ]

    since = _api.datetime.now(_api.timezone.utc) - _api.timedelta(days=7)
    stat_report = compute_stat_report(since=since, top=1)

    payload = {
        "harnesses": {
            name: {"seats": v["seats"], "providers": sorted(v["providers"])}
            for name, v in sorted(harnesses.items())
        },
        "models": models,
        "accounts": accounts,
        "roles": {role: list(ids) for role, ids in ROLE_FALLBACK_MODELS.items()},
        "stats_since": stat_report["since"],
        "stats_harnesses": stat_report["harnesses"],
    }

    if ns.json:
        import json as _json

        print(_json.dumps(payload, indent=2))
        return 0

    print(_api._render_entities_report(payload))
    return 0


def _entity_model_line(model, name_width):
    status = "available" if model["available"] else f"unavailable: {model['reason']}"
    if model.get("substitute"):
        status += f" -> answers via {model['substitute']}"
    role, effort = model["role"] or "general", model["effort"] or "-"
    return (f"  {model['display'].ljust(name_width)}  {model['harness']:<11} {model['provider']:<12} "
            f"role={role:<12} effort={effort:<6} [{status}]")


def _entity_account_line(account):
    marker = "*" if account["current"] else " "
    if account.get("logged_in") is False:
        status = f"  LOGGED OUT -- log it back in: {account['login_command']}"
    elif account.get("login_failure"):
        status = (f"  LOGIN FAILED {account['login_failed_at']} ({account['login_failure']}) "
                  f"-- log it back in: {account['login_command']}")
    else:
        status = ""
    return f"  {marker} {account['name']:<14} {account['email'] or '(no email on disk)'}{status}"


def _render_entities_report(_api, /, payload: dict) -> str:
    lines = ["review entities -- harnesses, providers, models, accounts, efforts\n", "HARNESSES"]
    for name, info in payload["harnesses"].items():
        providers = ", ".join(info["providers"]) or "-"
        lines.append(f"  {name:<12} {info['seats']:>2} seat(s)   providers: {providers}")
    lines.append("\nMODELS (board order)")
    name_width = max((len(model["display"]) for model in payload["models"]), default=0)
    lines.extend(_entity_model_line(model, name_width) for model in payload["models"])
    lines.append("\nACCOUNTS (Claude multi-account registry)")
    if not payload["accounts"]:
        lines.append("  none registered -- single implicit account")
    lines.extend(_entity_account_line(account) for account in payload["accounts"])
    lines.append("\nEFFORT / ROLE LENSES (fallback chain per role)")
    lines.extend(f"  {role:<12} {' -> '.join(ids)}" for role, ids in payload["roles"].items())
    since = payload["stats_since"] or "all recorded history"
    lines.append(f"\nCALL STATS (since {since})")
    if payload["stats_harnesses"]:
        lines.append(_api._render_stat_harness_table(payload["stats_harnesses"]))
    else:
        lines.append("  no calls recorded in this window")
    lines.append("\nThis is a read-only Phase-0 snapshot (`review entities --json` for the "
                 "machine-readable form) -- the full interactive browser (drill-down, live "
                 "refresh) is tracked in review-cli#487.")
    return "\n".join(lines)


def _print_health_reports(health, reports, as_json):
    if as_json:
        import json
        payload = reports[0] if len(reports) == 1 else {"hosts": reports}
        print(json.dumps(payload, indent=2))
        return
    for report in reports:
        if report.get("error"):
            print(f"Seat health @{report['host']}: {report['error']}")
        else:
            print(health.render_text(report))
