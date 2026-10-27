"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import ServiceManager, argparse, datetime

def _dashboard_help_no_lib(_api, /) -> int:
    """Help for a BARE ``review dashboard`` when the service lib is ABSENT (still launches nothing).

    Mirrors the lib-present help surface — it advertises every lifecycle action so the bare-HELP
    contract (and the smoke test that greps for ``status`` / the action names) holds regardless of
    whether ``agenttools_service`` is installed — while making clear those actions need the lib."""
    print(
        "usage: review dashboard [--host H] [--port N] "
        "{run,start,stop,status,enable,disable}\n\n"
        "review-cli dashboard as a managed service. A bare `review dashboard` prints this help "
        "and launches nothing.\n\n"
        "actions:\n"
        "  run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled\n"
        "  start    start in the BACKGROUND (detached daemon); return immediately\n"
        "  status   is it running? pid / port / url / autostart-enabled\n"
        "  stop     stop the background instance\n"
        "  enable   install OS autostart (launchd / systemd --user / fallback) AND start now\n"
        "  disable  remove OS autostart AND stop\n\n"
        "note: start/status/stop/enable/disable need the shared 'agenttools_service' lib "
        "(pip install -e <agent-tools>/lib/agenttools_daemon "
        "-e <agent-tools>/lib/agenttools_service); `run` works without it."
    )
    return 0


def _dashboard_serve(_api, /, rest: list[str]) -> int:
    """Hidden entry: ``review dashboard __serve`` — run the blocking web server in THIS shell.

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``):
    it calls ``run_dashboard`` directly and so never re-enters the service dispatcher (a bare
    ``run`` action whose argv pointed back at ``review dashboard run`` would fork-bomb). Not
    advertised in help — operators use ``review dashboard run`` (ad-hoc) or ``start`` (managed).

    Binds 127.0.0.1 by default; ``--host 0.0.0.0`` exposes it over Tailscale (mirrors
    ``review spec-web``). Imported lazily so the dashboard's stdlib HTTP stack never loads on
    the hot review path (and a stray import error in dashboard code can't break `review`)."""
    sub = _api.argparse.ArgumentParser(
        prog="review dashboard __serve",
        description="Run the review-cli dashboard web server (blocking).",
    )
    sub.add_argument("--host", default="127.0.0.1", help=_api._DASHBOARD_HOST_HELP)
    sub.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default: a free ephemeral port)",
    )
    sub.add_argument(
        "--no-open", action="store_true", help="do not open a browser window"
    )
    sub.add_argument(
        "--verbose", action="store_true", help="log every HTTP request to stderr"
    )
    ns = sub.parse_args(rest)
    from .dashboard import run_dashboard

    return run_dashboard(
        port=ns.port, host=ns.host, open_browser=not ns.no_open, verbose=ns.verbose
    )


def _dashboard_subcommand(_api, /, rest: list[str]) -> int:
    """`review dashboard [run|start|status|stop|enable|disable]` — the dashboard as a MANAGED
    service, plus the legacy ad-hoc ``run``.

    The lifecycle subcommands (run/start/status/stop/enable/disable) come from the shared
    ``agenttools_service`` lib (one service-manager for every long-running server in the
    ecosystem — review dashboard, config-web, tg-ctl, future daemons), NOT hand-rolled here:

      run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled.
      start    start in the BACKGROUND (detached daemon); return immediately.
      status   is it running? pid / port / url / autostart-enabled.
      stop     stop the background instance.
      enable   install OS autostart (launchd / systemd --user / no-systemd fallback) AND start now.
      disable  remove OS autostart AND stop.

    A BARE ``review dashboard`` (no action) prints HELP and launches NOTHING. ``--host`` /
    ``--port`` select the managed bind (a stable default port, unlike ad-hoc ``run``'s ephemeral
    one). Everything (the lib, the service descriptor) is imported lazily so the hot ``review``
    path never pays for the dashboard/service stack."""
    # Hidden foreground entry that the service argv points at (see dashboard.service._serve_argv).
    # Kept BEFORE the agenttools_service import so the blocking server still runs even on a host
    # where the shared service lib isn't installed (a detached `start` from a host that HAS the
    # lib can run `__serve` here).
    if rest and rest[0] == "__serve":
        return _api._dashboard_serve(rest[1:])

    try:
        from agenttools_service import add_service_subcommands, dispatch
    except ImportError:
        # No shared service lib on this host. The bare-HELP and ad-hoc `run` contracts must
        # still hold (they don't depend on the lib): a BARE `review dashboard` (or `--help`)
        # prints help and launches nothing, and `run` still brings up an ad-hoc foreground
        # server so a lib-less operator isn't fully blocked. Only the genuine lifecycle actions
        # (start/status/stop/enable/disable) need the lib — for those, emit an actionable error
        # (structured-exit-codes) instead of a raw ImportError traceback.
        action, action_idx = _api._dashboard_action_with_index(rest)
        # A BARE `review dashboard` OR a help-only `review dashboard [--help|-h]` (no lifecycle
        # action) prints help and launches nothing — exit 0. The help surface is the SAME
        # whether or not the lib is installed (the smoke test greps it for the action names),
        # so a lib-less operator still sees what the dashboard can do. `--help`/`-h` with no
        # action was previously mis-routed to the missing-lib error (exit 4), breaking the
        # bare-HELP contract — a `--help`/`-h` is NOT itself a lifecycle action.
        if action is None:
            return _api._dashboard_help_no_lib()
        if action == "run":
            # Ad-hoc foreground server still works without the lib. Drop ONLY the `run` action
            # token (by index, not by value — a value that happens to equal "run" must survive);
            # `_dashboard_serve` parses the rest as server flags (and prints its own --help/exit 0
            # for `run --help`, so the help contract holds there too).
            return _api._dashboard_serve(rest[:action_idx] + rest[action_idx + 1 :])
        # A genuine lifecycle action (start/status/stop/enable/disable) needs the lib — emit the
        # actionable missing-lib error (structured-exit-codes) instead of a raw ImportError.
        print(_api._AGENTTOOLS_SERVICE_MISSING_MSG, file=_api.sys.stderr)
        return 4

    from .dashboard.service import DEFAULT_DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT

    parser = _api.argparse.ArgumentParser(
        prog="review dashboard",
        description="review-cli dashboard as a managed service (run/start/status/stop/enable/disable).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_DASHBOARD_HOST, help=_api._DASHBOARD_HOST_HELP
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f"port to bind the managed dashboard (default: {DEFAULT_DASHBOARD_PORT})",
    )
    subs = parser.add_subparsers(dest="action")
    # Parse FIRST, then build the manager factory over the parsed namespace. The factory closes
    # over `ns`, so `ns` must exist before any action can invoke it; registering the subcommands
    # does not call the factory (it is lazy, per the lib contract), so registration order vs parse
    # is free — but binding `ns` explicitly avoids a fragile forward-reference closure.
    add_service_subcommands(
        subs,
        manager_factory=lambda: _api._dashboard_manager(ns.host, ns.port),
        service_name="dashboard",
    )
    ns = parser.parse_args(rest)

    def _print_help_ok() -> int:
        parser.print_help()
        return 0

    return dispatch(ns, on_no_subcommand=_print_help_ok)


def _dashboard_manager(_api, /, host: str, port: int) -> 'ServiceManager':
    """Build a ``ServiceManager`` for the dashboard at ``host:port`` (lazy, per the lib's
    ``manager_factory`` contract — constructed only when an action actually runs)."""
    from agenttools_service import ServiceManager

    from .dashboard.service import dashboard_service

    return ServiceManager(dashboard_service(port=port, host=host))


def _sessions_subcommand(_api, /, rest: list[str]) -> int:
    """`review sessions [-a/--all] [-s/--resume <id>] [--force] [-m … --moderator …]`.

    List or RESUME brainstorm sessions parsed from the on-disk discussion logs. Kept as a
    bare subcommand (like `dashboard`) — it is a MANAGEMENT command over the logs, not a
    fan-out review mode, so it does not go through the mode registry. All session logic
    lives in `reviewlib.sessions` (lib); this handler is thin.

    Default listing (no `-a`) shows recent COMPLETED sessions (a sensible recent subset);
    `-a/--all` adds the dead/interrupted ones (crashed / killed / timed out — no synthesis)
    and lifts the cap. `-s <id>` RESUMES: it reloads the saved transcript and continues the
    brainstorm from `completed_round + 1`, reusing the saved topic / panel / moderator,
    then synthesizes — it does NOT start from scratch.
    """
    from . import sessions as _sessions

    sub = _api.argparse.ArgumentParser(
        prog="review sessions",
        description="List or resume brainstorm sessions (parsed from the discussion logs).",
    )
    sub.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="list ALL sessions incl. dead/interrupted (no synthesis); default lists recent completed",
    )
    sub.add_argument(
        "-s",
        "--resume",
        metavar="ID",
        default=None,
        help="resume the session with this id (short id or unambiguous prefix): continue the round loop and synthesize",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="with --resume on an already-completed session, re-synthesize anyway",
    )
    # Resume reuses the saved panel/moderator by default; -m / --moderator override.
    sub.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help="override the resume panel (repeat or comma-separate); default = the saved session's panel",
    )
    sub.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="repository directory (resume diff/agentic cwd)",
    )
    sub.add_argument(
        "--moderator",
        default=None,
        help="override the resume moderator; default = the saved session's moderator",
    )
    # Grounding diff on resume: the original `--diff`/`--staged` grounding is NOT persisted
    # in the discussion log, so a resumed grounded brainstorm would otherwise continue
    # UNgrounded. These flags re-attach the current working-tree (--diff) or staged
    # (--staged) diff as grounding for the resumed rounds + synthesis (opt-in, like the
    # brainstorm mode's own grounding). Absent -> the resume runs ungrounded.
    sub.add_argument(
        "--diff",
        action="store_true",
        help="re-attach the working-tree diff as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--staged",
        action="store_true",
        help="re-attach the staged diff (git diff --cached) as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"per-call timeout seconds for the resumed rounds (default {_api.PANEL_TIMEOUT_DEFAULT})",
    )
    ns = sub.parse_args(rest)

    if ns.resume:
        return _api._resume_session_cli(ns)

    sessions = _sessions.list_sessions(include_dead=ns.all)
    if not sessions:
        scope = "" if ns.all else " completed"
        print(f"No{scope} brainstorm sessions found in {_sessions.log_dir()}.")
        if not ns.all:
            print("(pass -a/--all to include dead/interrupted sessions.)")
        return 0
    header = (
        "all sessions (incl. interrupted)" if ns.all else "recent completed sessions"
    )
    print(
        f"Brainstorm {header} — newest first; resume with `review sessions -s <id>`:\n"
    )
    for s in sessions:
        ts = s.timestamp.strftime("%Y-%m-%d %H:%M UTC") if s.timestamp else "?"
        topic = (s.topic[:60] + "…") if len(s.topic) > 61 else (s.topic or "(no topic)")
        print(
            f"  {s.session_id}  [{s.status:<11}]  r{s.completed_rounds}  {ts}  {topic}"
        )
    return 0


def _resume_session_cli(_api, /, ns: argparse.Namespace) -> int:
    """Resolve the saved session by id and continue its brainstorm. Thin over
    `reviewlib.sessions.resume_session`; resolves the panel/moderator (saved unless
    overridden) and reports the clean errors (unknown id / ambiguous prefix / already
    complete) with actionable messages + meaningful exit codes."""
    from . import backends, sessions as _sessions

    config = _api.load_config()
    backends.configure_unpaid_providers(config.get("unpaid_providers"))
    backends.configure_omp_bin(config.get("omp_bin"))
    backends.configure_omp_accounts(config.get("omp_accounts"))

    try:
        sess = _sessions.find_session(ns.resume)
    except _sessions.AmbiguousSessionError as exc:
        print(f"[review sessions] {exc}", file=_api.sys.stderr, flush=True)
        return 2
    if sess is None:
        print(
            f"[review sessions] no session with id '{ns.resume}'. "
            "Run `review sessions -a` to list available ids.",
            file=_api.sys.stderr,
            flush=True,
        )
        return 2

    cwd = _api._effective_cwd(ns.cwd)
    # Panel: explicit -m override > the saved session panel (dropping unreachable
    # backends so a vanished key never aborts) > whatever the saved panel was.
    models, moderators, timeout = _resume_selection(_api, ns, sess, config)

    # Optional grounding diff for the resumed rounds: --diff / --staged re-attach the
    # current diff (the original grounding is not persisted in the log). Degrades to
    # ungrounded on a non-repo / git failure, exactly like the brainstorm mode.
    diff = ""
    if getattr(ns, "diff", False) or getattr(ns, "staged", False):
        try:
            diff = _api._git_diff(cwd, ns.staged)
        except RuntimeError:
            diff = ""

    print(
        f"[review sessions] resuming '{sess.session_id}' ({sess.status}, "
        f"{sess.completed_rounds} round(s) done): {sess.topic}",
        file=_api.sys.stderr,
        flush=True,
    )

    # Panel modes announce their live-log paths (the resumed rounds stream to the log).
    backends._ANNOUNCE_LOGS = True
    try:
        # A resumed brainstorm is a full panel run: it takes a host-wide slot like any
        # other brainstorm (review-cli#614).
        with _api.admission.run_slot(
            mode="brainstorm",
            models=_api.admission.routed(_api.brainstorm_pool(models)),
            later_models=_api.admission.routed(moderators[:1]),
        ):
            return _sessions.resume_session(
                sess,
                models=models,
                cwd=cwd,
                timeout=timeout,
                moderators=moderators,
                diff=diff,
                force=ns.force,
            )
    except _sessions.SessionAlreadyCompleteError as exc:
        # A refused resume (already-complete, no --force) did NO requested work. Return the
        # same non-zero code the unknown/ambiguous-id paths use so scripts and hooks can
        # tell a refusal from a real resume — exit 0 here was indistinguishable from success
        # (codex P2: CTO sided with the bot over the prior "intentional" exit-0 choice).
        print(f"[review sessions] {exc}", file=_api.sys.stderr, flush=True)
        return 2


def _task_sessions(_api, /, task_code: str):
    """Dashboard sessions for a task, oldest first. Best-effort over existing logs."""
    from .dashboard import parser as dparser
    from .process import log_dir

    sessions = [s for s in dparser.load_sessions(log_dir()) if s.task_code == task_code]
    return sorted(sessions, key=lambda s: s.started)


def _parse_task_record_started(_api, /, record: dict) -> datetime | None:
    dt = _api.parse_iso_ts(record.get("ts"))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_api.timezone.utc)
    return dt.astimezone(_api.timezone.utc)


def _session_started_utc(_api, /, session) -> datetime:
    dt = session.started
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_api.timezone.utc)
    return dt.astimezone(_api.timezone.utc)


def _task_record_iteration(_api, /, record: dict) -> int | None:
    try:
        return int(record["iteration"])
    except (KeyError, TypeError, ValueError):
        return None


def _closest_iteration_for_session(_api, /, session, iterations: list[dict]) -> int | None:
    session_started = _api._session_started_utc(session)
    best_iteration: int | None = None
    best_delta: float | None = None
    for record in iterations:
        started = _api._parse_task_record_started(record)
        iteration = _api._task_record_iteration(record)
        if started is None or iteration is None:
            continue
        delta = abs((session_started - started).total_seconds())
        if delta > _api._TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if best_delta is None or delta < best_delta:
            best_iteration = iteration
            best_delta = delta
    return best_iteration


def _session_for_task_iteration(_api, /, record: dict, sessions: list, all_iterations: list[dict] | None, used_session_ids: set[str] | None):
    """Match a stats iteration to the session whose start time is closest to it."""
    started = _api._parse_task_record_started(record)
    iteration = _api._task_record_iteration(record)
    if started is None or iteration is None:
        return None
    best = None
    best_delta: float | None = None
    used = used_session_ids or set()
    for session in sessions:
        if session.session_id in used:
            continue
        delta = abs((_api._session_started_utc(session) - started).total_seconds())
        if delta > _api._TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if (
            all_iterations
            and _api._closest_iteration_for_session(session, all_iterations) != iteration
        ):
            continue
        if best_delta is None or delta < best_delta:
            best = session
            best_delta = delta
    return best


def _iteration_for_session(_api, /, session, iterations: list[dict]) -> int | None:
    return _api._closest_iteration_for_session(session, iterations)


def _ssh_review(_api, /, host: str, remote_args: list[str]) -> tuple[int, str, str]:
    """Run `review <remote_args>` on `host` over a non-interactive ssh; (rc, stdout, stderr)."""
    cmd = (
        'export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"; exec review '
        + " ".join(_api.shlex.quote(a) for a in remote_args)
    )
    try:
        proc = _api.subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                host,
                cmd,
            ],
            stdin=_api.subprocess.DEVNULL,  # ssh must not slurp the caller's stdin (host lists)
            capture_output=True,
            text=True,
            errors="replace",  # a login banner / locale error is not always UTF-8
            timeout=120,
            check=False,
        )
    except (OSError, _api.subprocess.TimeoutExpired) as exc:
        return 255, "", f"ssh {host} failed: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _resume_selection(_api, ns, sess, config):
    from . import backends
    explicit_models = _api._split_models(ns.model)
    if explicit_models:
        models = explicit_models
    else:
        models = (
            [m for m in sess.panel if backends.backend_available(m)]
            or sess.panel
            or list(_api.DEFAULT_MODELS)
        )
    # Moderator: explicit --moderator override > the saved session moderator > picked.
    # The log records the moderator FALLBACK CHAIN joined with `>` (e.g. `claude:..>codex`),
    # so the saved value must be SPLIT back into candidates — passing the whole `a>b>c`
    # string as one explicit seed would make `pick_moderators` try an invalid single
    # backend id before falling back. Take the FIRST (highest-priority) saved candidate as
    # the explicit seed; pick_moderators rebuilds the rest of the priority order.
    saved_mod = sess.moderator.split(">")[0].strip() if sess.moderator else ""
    mod_seed = ns.moderator or (saved_mod or None)
    if mod_seed:
        mod_seed = _api._expand_alias(mod_seed)
    moderators = _api.pick_moderators(mod_seed, models, _api._moderator_candidates(config, None))
    timeout = ns.timeout if ns.timeout is not None else _api.PANEL_TIMEOUT_DEFAULT
    return models, moderators, timeout
