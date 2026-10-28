"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import datetime

def _jobs_subcommand(_api, /, rest: list[str]) -> int:
    """`review jobs [--json] [--slots]` — host-wide admission slots, then every recorded
    `--detach` job, newest first.

    Status is the RECONCILED value (`jobs.job_status`): a job whose recorded state is
    still "running" but whose pid is gone is shown as "unknown-terminated" rather than
    a stale "running" that never resolves. The ADMISSION block (review-cli#614) lists
    every review on this host that is running or queued for a slot — detached or not —
    with the host budget and per-provider seat usage. `--slots` prints only that block
    (`--slots --json` as JSON); plain `--json` stays the job list it always was."""

    parser = _api.argparse.ArgumentParser(
        prog="review jobs",
        description="Host-wide admission slots and detached (`--detach`) review jobs.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--slots",
        action="store_true",
        help="show only the host-wide admission slots (running/waiting reviews, budgets)",
    )
    ns = parser.parse_args(rest)

    if ns.slots:
        snap = _api.admission.status_snapshot()
        if ns.json:
            import json as _json

            print(_json.dumps(snap, indent=2))
        else:
            print(_api.admission.render_status(snap))
        return 0

    records = [_api.jobs.job_status(rec["job_id"]) or rec for rec in _api.jobs.list_jobs()]
    records.sort(key=lambda r: r.get("started_at") or 0, reverse=True)

    if ns.json:
        import json as _json

        print(_json.dumps(records, indent=2))
        return 0

    print(_api.admission.render_status(_api.admission.status_snapshot()))
    print()
    if not records:
        print("No detached jobs recorded.")
        return 0

    print("DETACHED JOBS")
    for rec in records:
        started = rec.get("started_at")
        when = (
            _api.time.strftime("%Y-%m-%d %H:%M:%S", _api.time.localtime(started))
            if started
            else "?"
        )
        queued = (
            f" admission=waiting#{rec.get('queue_position')}"
            if rec.get("status") == "running" and rec.get("admission") == "waiting"
            else ""
        )
        print(
            f"  {rec.get('job_id')}  {rec.get('status', '?'):<18} "
            f"mode={rec.get('mode', '?')} pid={rec.get('pid', '?')} started={when}{queued}"
        )
    return 0


def _job_exit_code(_api, /, rec: dict) -> int:
    """The exit code `status`/`wait` report for a job record: 0 while still running,
    the job's OWN recorded `exit_code` once it finished ("done" or "failed" — NOT
    collapsed to a bare 0/1, so a caller scripting against a specific code (2, 10, 124,
    …) sees the real value), or 1 for "unknown-terminated" (the process died before it
    could record one — no exit code exists to report)."""
    status = rec.get("status")
    if status == "running":
        return 0
    if status in ("done", "failed"):
        code = rec.get("exit_code")
        return code if isinstance(code, int) else 1
    return 1  # unknown-terminated, or any unrecognized status


def _seats_subcommand(_api, /, verb: str, rest: list[str]) -> int:
    """`review seats|doctor [--json]` -- the bare `review status` seat report."""
    parser = _api.argparse.ArgumentParser(
        prog=f"review {verb}",
        description=(
            "Show which board seats can run right now and why the others cannot, each "
            "Claude CLI account's login state, the omp launcher, each omp Anthropic "
            "account's availability and usage, and every harness login on this machine "
            "(Claude Code, Codex, omp, opencode, review-cli's own API keys; no secret is "
            "shown). Read-only, no model is called. Same report as a bare `review status`."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    return _api._seat_status_report(as_json=ns.json)


def _status_subcommand(_api, /, rest: list[str]) -> int:
    """`review status <job-id> [--json] [--tail N]` — inspect one detached job.

    Exit code mirrors the job (`_job_exit_code`): 0 while running, the job's OWN
    recorded exit code once it finished, 1 for "unknown-terminated", 2 for an unknown
    job-id (a usage error, like any other bad argument).

    Bare `review status [--json]` (no job id) is the seat-health view instead
    (`_seat_status_report`, review-cli#594): which board seats can run right now and why not."""

    parser = _api.argparse.ArgumentParser(
        prog="review status",
        description=(
            "Show one detached (`--detach`) review job's status; with no job id, show "
            "which board seats can run right now, why the others cannot, and what "
            "each would be substituted with (read-only, no model is called)."
        ),
    )
    parser.add_argument(
        "job_id",
        nargs="?",
        help="job id printed by `--detach` / `review jobs` (omit for seat health)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--tail",
        type=int,
        default=20,
        metavar="N",
        help="lines of log to show (default 20)",
    )
    ns = parser.parse_args(rest)
    if ns.job_id is None:
        return _api._seat_status_report(as_json=ns.json)

    rec = _api.jobs.job_status(ns.job_id)
    if rec is None:
        print(f"[review status] no such job: {ns.job_id}", file=_api.sys.stderr)
        return 2

    if ns.json:
        import json as _json

        print(_json.dumps(rec, indent=2))
        return _api._job_exit_code(rec)

    print(f"job {rec['job_id']}: {rec.get('status', '?')}")
    print(f"  mode:   {rec.get('mode', '?')}")
    print(f"  pid:    {rec.get('pid', '?')}")
    print(f"  log:    {rec.get('log_path', '?')}")
    print(f"  result: {rec.get('result_path', '?')}")
    if rec.get("exit_code") is not None:
        print(f"  exit:   {rec['exit_code']}")
    log_path = rec.get("log_path")
    if ns.tail > 0 and log_path:
        lines = _api.jobs.tail_lines(_api.Path(log_path), ns.tail)
        if lines:
            print(f"\n--- last {len(lines)} log line(s) ---")
            for line in lines:
                print(f"  {line}")
    return _api._job_exit_code(rec)


def _wait_subcommand(_api, /, rest: list[str]) -> int:
    """`review wait <job-id> [--timeout SECS] [--poll SECS]` — block until a detached
    job finishes (or the wait itself times out).

    For a human terminal or an unbounded background caller — NOT for a subagent with
    its own short foreground cap (that caller should poll `review status` instead;
    blocking here defeats the entire point of `--detach`). Default timeout mirrors the
    internal run backstop's 4h ceiling, since no detached review can legitimately run
    longer than that. Exit code mirrors the job's own recorded exit code
    (`_job_exit_code`) once it finishes — NOT collapsed to a bare 0/1 — so a caller
    scripting against a specific code (2, 10, 124, …) sees the real value; 1 for
    "unknown-terminated" or this wait's own timeout, 2 for an unknown job-id."""
    from .backstop import MAX_BACKSTOP_SECONDS

    parser = _api.argparse.ArgumentParser(
        prog="review wait",
        description="Block until a detached (`--detach`) review job finishes.",
    )
    parser.add_argument("job_id", help="job id printed by `--detach` / `review jobs`")
    parser.add_argument(
        "--timeout",
        type=int,
        default=MAX_BACKSTOP_SECONDS,
        metavar="SECS",
        help=f"give up waiting after this many seconds (default {MAX_BACKSTOP_SECONDS})",
    )
    parser.add_argument(
        "--poll", type=float, default=2.0, metavar="SECS", help="poll interval"
    )
    ns = parser.parse_args(rest)

    deadline = _api.time.monotonic() + max(1, ns.timeout)
    while True:
        rec = _api.jobs.job_status(ns.job_id)
        if rec is None:
            print(f"[review wait] no such job: {ns.job_id}", file=_api.sys.stderr)
            return 2
        if rec.get("status") in _api.jobs.TERMINAL_STATUSES:
            print(f"job {rec['job_id']}: {rec['status']}")
            return _api._job_exit_code(rec)
        if _api.time.monotonic() >= deadline:
            print(
                f"[review wait] timed out after {ns.timeout}s waiting for {ns.job_id} "
                f"(still {rec.get('status', '?')})",
                file=_api.sys.stderr,
            )
            return 1
        _api.time.sleep(max(0.1, ns.poll))


def _resolve_stat_since(_api, /, since_arg: str | None, days: int) -> datetime | None | bool:
    """Resolve `--since`/`--days` into a UTC datetime floor (None = all history).
    Returns `False` as a sentinel for an unparseable `--since` — the caller reports it
    as a usage error rather than silently falling back to "all history".

    Opus/kimi review finding: `datetime.fromisoformat` only accepts a trailing `Z`
    (Zulu/UTC) shorthand from Python 3.11 onward, but this project declares
    `requires-python = ">=3.9"` — and every call-log filename this command's own output
    is built from uses exactly that `...Z` stamp format. A user copying a timestamp
    straight out of `review stat`'s own report (or a filename) got a real usage error
    on 3.9/3.10 while the identical value worked on 3.11+. Normalize the shorthand
    ourselves before parsing so the accepted syntax doesn't depend on the interpreter."""
    if since_arg:
        parsed = _api.parse_iso_ts(since_arg)
        if parsed is None:
            return False
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_api.timezone.utc)
    if days <= 0:
        return None
    return _api.datetime.now(_api.timezone.utc) - _api.timedelta(days=days)


def _normalize_harness_arg(_api, /, raw: str) -> str:
    """The `--harness` value, normalized to the exact `CallLog.backend` spelling the
    report's `harnesses` dict is keyed by — see `_HARNESS_ARG_ALIASES` above.

    Opus review finding, round 4: the fallback for a token NOT in the alias dict used
    to return `raw` completely UNCHANGED — but every real backend key is lowercase with
    no surrounding whitespace, so `--harness Codex` (different casing) or `--harness
    "codex "` (trailing space) fell through as literally `"Codex"`/`"codex "`, which
    then never equals the report's `"codex"` key — a false "no calls recorded" for data
    that genuinely exists. The fallback now returns the SAME normalized (stripped,
    lowercased) key the alias lookup itself used, so an exact name matches regardless
    of input casing/whitespace, while a genuinely unrecognized token still produces the
    honest "no calls recorded for harness ..." message, not a silent misroute."""
    key = raw.strip().lower()
    return _api._HARNESS_ARG_ALIASES.get(key, key)


def _feedback_subcommand(_api, /, rest: list[str]) -> int:
    """`review feedback <run_id> [--note TEXT] [--useful yes|no|unsure] [--ticket ID]
    [--pr N] [--json]` — the CLI-side path onto the overseer annotation store
    (`reviewlib.dashboard.store`), keyed under `"run-<run_id>"`. `<run_id>` is the id
    printed by `_print_feedback_nudge` at the end of every recorded run (also visible
    in `review task`/`review stat --json` records under the `run_id` field).

    This exists because the ONLY prior path to `feedback`/`conscious`/`links` was the
    dashboard web UI (`POST /api/runs/<id>/feedback`) — real usage showed a 0% fill
    rate (`~/.config/review-cli/dashboard.json` had never once been written) because
    nothing in the CLI path ever told a caller the feature existed. Giving none of
    `--note`/`--useful`/`--ticket`/`--pr` turns this into a READ instead of a write —
    it prints whatever is already on file (defaults if nothing was ever recorded) so a
    caller can check a run's annotation without risking an accidental write.

    If the looked-up run-stats record's `task_code` already looks like a ticket ID
    (the same `HYP-742`-shaped pattern `--ticket` itself validates) and no explicit
    `--ticket` was given, it is auto-linked — the ticket the run was FOR is already
    known, no reason to make the caller retype it."""
    from .dashboard import store as dashboard_store

    parser = _api.argparse.ArgumentParser(
        prog="review feedback",
        description="Attach overseer feedback/usefulness/links to a recorded run.",
    )
    parser.add_argument("run_id", help="the run_id printed at the end of a review run")
    parser.add_argument("--note", default=None, help="free-text feedback")
    parser.add_argument(
        "--useful",
        choices=sorted(_api._USEFUL_CHOICES),
        default=None,
        help="was this run worth the tokens it burned",
    )
    parser.add_argument("--ticket", default=None, help="ticket id, e.g. HYP-742")
    parser.add_argument("--pr", default=None, help="PR number, e.g. 123 or #123")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    nothing_to_write = (
        ns.note is None and ns.useful is None and ns.ticket is None and ns.pr is None
    )

    record = _api.find_by_run_id(ns.run_id)
    if record is None:
        print(
            f"[review feedback] warning: run_id {ns.run_id!r} not found in "
            "run-stats.jsonl (recording the annotation anyway — it may be a stale/"
            "hand-typed id, or predate this field).",
            file=_api.sys.stderr,
        )

    key = f"run-{ns.run_id}"
    if not nothing_to_write:
        invalid = _write_feedback(_api, dashboard_store, ns, record, key)
        if invalid is not None:
            return invalid

    rec = dashboard_store.get_annotation(key)
    if ns.json:
        import json as _json

        print(_json.dumps({"run_id": ns.run_id, **rec}, indent=2))
        return 0
    context = ""
    if record is not None:
        context = f" ({record.get('mode', '?')}, task={record.get('task_code', '-')})"
    if nothing_to_write:
        # No --note/--useful/--ticket/--pr given: a plain `review feedback <id>` is a
        # READ, not a write — show whatever is on file (empty defaults if nothing was
        # ever recorded) instead of erroring on a caller who just wants to check.
        print(
            f"[review feedback] current annotation for run {ns.run_id}{context}: {rec}"
        )
        # Review finding (Opus): gating this hint on feedback/useful alone was wrong
        # -- a run annotated with ONLY a --ticket/--pr (feedback/useful both still
        # None) got a contradictory "nothing recorded yet" printed right next to its
        # real ticket/PR links. `updated` is set by every write path (set_feedback/
        # apply_feedback/add_link all touch it), so `None` means genuinely never written.
        if rec["updated"] is None:
            print(
                "  (nothing recorded yet — add --note/--useful/--ticket/--pr to set it)",
                file=_api.sys.stderr,
            )
    else:
        print(f"[review feedback] recorded for run {ns.run_id}{context}: {rec}")
    return 0


def _stat_subcommand(_api, /, rest: list[str]) -> int:
    """`review stat [--days N] [--since ISO] [--top N] [--harness NAME] [--json]` —
    detailed per-harness/per-model usage + health report parsed from the real per-call
    logs (see `reviewlib.dashboard.tokenstats` for the data model and the 2026-08
    token-burn investigation this answers). Default window is the last 7 days — a full
    scan of a long-lived install's log dir (tens of thousands of files) is slow, and
    most token-burn questions are about recent behaviour; `--days 0` scans everything."""
    parser = _api.argparse.ArgumentParser(
        prog="review stat",
        description="Per-harness/per-model usage + health report from the real call logs.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="only calls from the last N days (default 7; <= 0 = all recorded history)",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        default=None,
        help="only calls at/after this ISO-8601 timestamp (overrides --days)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="how many largest calls to list (default 10; clamped to >= 1) — also "
        "caps the by_repo_top row count",
    )
    parser.add_argument(
        "--harness",
        default=None,
        help="narrow the per-harness BREAKDOWN TABLE to this backend (e.g. codex, "
        "opencode, commandcode, omp, claude, z.ai — also accepts common aliases: "
        "glm/zai/oc/cc) — every other DATA section (models, Fable, retry events, "
        "call count, top oversized calls) stays whole-window in --json; the text "
        "report's Fable/retry/oversized sections show it too, but per-model detail is "
        "--json-only (see `review stat --json`)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    # A non-positive --top has no sensible "top N" meaning, and a negative value would
    # silently slice as "all but the last |N|" (sorted(...)[:-3]) — a confusing result
    # under the "Top N oversized calls" heading (kimi review finding). Clamp instead of
    # erroring: a typo'd --top still gets a useful (if minimal) report.
    if ns.top < 1:
        ns.top = 1

    since = _api._resolve_stat_since(ns.since, ns.days)
    if since is False:
        print(
            f"[review stat] invalid --since value: {ns.since!r} "
            "(expected ISO-8601, e.g. 2026-08-01T00:00:00+00:00)",
            file=_api.sys.stderr,
        )
        return 2

    from .dashboard.tokenstats import compute_stat_report

    report = compute_stat_report(since=since, top=ns.top)
    # `by_mode`/`by_repo_top` join in `reviewlib.stats`'s run-stats.jsonl (mode/task/
    # repo attribution — GROUND TRUTH, not inferred, see that module's docstring) on
    # top of the call-log-only report above (deliberately NOT computed inside
    # `compute_stat_report`/`tokenstats.py`, which documents itself as a call-log-only
    # data model — mixing the two stores in one function would blur which section
    # answers which question). Overspend-investigation finding (2026-09): the
    # pre-existing report had per-harness/per-model breakdowns but nothing answering
    # "which MODE (review/quorum/brainstorm/just-ask) or which REPO is actually
    # generating the volume" — this closes that gap.
    # One read of the run-stats history feeds both sections (it is re-parsed in full
    # per `_load_records` call, which grows with history).
    run_records = _api.load_run_records()
    report["by_mode"] = _api.mode_summary(since=since, records=run_records)
    report["by_repo_top"] = _api.repo_summary(since=since, top=ns.top, records=run_records)
    # glm review finding, round 2: normalize BEFORE filtering — see
    # `_normalize_harness_arg` for why a raw alias (`glm`, `zai`, `oc`, `cc`) used to
    # always miss even though the report's own footnote teaches it. Displaying the
    # normalized name (not the raw one the user typed) in the empty-result message is
    # deliberate: it shows exactly what was actually searched for, so a mistaken alias
    # mapping is visible, not hidden.
    harness = _api._normalize_harness_arg(ns.harness) if ns.harness else None
    if harness:
        # Table-only filter (codex review finding): this narrows ONLY the per-harness
        # breakdown row set, not the whole report — models/fable/retry-events/
        # call_count/top_oversized_calls are cross-harness context that stays
        # whole-window on purpose (e.g. seeing where codex's calls rank among the
        # largest overall). See the --harness help text above.
        report["harnesses"] = {
            name: hs for name, hs in report["harnesses"].items() if name == harness
        }
        # Opus review finding, round 3: this stderr note used to print UNCONDITIONALLY,
        # so the text-report path showed the "no calls recorded for harness ..." message
        # TWICE — once here, once again via `_render_stat_harness_table`'s own empty-
        # table message in the printed report body. Harmless but redundant. The `--json`
        # path has no equivalent in-payload message (an empty `harnesses` dict alone
        # doesn't explain WHY), so it's the only path that still needs this stderr note.
        if not report["harnesses"] and ns.json:
            print(
                f"[review stat] no calls recorded for harness {harness!r} in this window.",
                file=_api.sys.stderr,
            )

    if ns.json:
        import json as _json

        print(_json.dumps(report, indent=2))
        return 0
    print(_api._render_stat_report_text(report, requested_harness=harness))
    return 0


def _capacity_check_subcommand(_api, /, rest: list[str]) -> int:
    """`review capacity-check [--preset NAME] [--json]` — whether every route and account
    the active review board could dispatch on is confirmed out of quota right now
    (review-cli#472; the evidence predicate is in `reviewlib.capacity_check`'s docstring
    and `review help capacity`). Built for `gh ship`'s review-quorum/external-review
    gates (agent-tools ci/ship/ship.sh), which skip the manual hatch prompt only on
    `all_exhausted: true`. A bare MANAGEMENT subcommand (like stat/task/entities), NOT a
    fan-out mode.

    Always exits 0 in --json mode (the caller gates on `all_exhausted`/`error`, not the
    exit code); the text form exits 1 only when the board could not be resolved."""
    parser = _api.argparse.ArgumentParser(
        prog="review capacity-check",
        description=(
            "Report whether every route/account the active review board could use is "
            "confirmed out of quota. See `review help capacity`."
        ),
    )
    parser.add_argument(
        "--level",
        "--preset",
        dest="preset",
        type=_api._level_arg,
        default=None,
        help="evaluate this level's board instead of the config/default board "
        "(the board `review diff --level NAME` would run; `--preset` is the older spelling)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)

    from .capacity_check import board_capacity_report, render_text

    report = board_capacity_report(preset=ns.preset)
    if ns.json:
        import json as _json

        print(_json.dumps(report, indent=2))
        return 0
    if report.get("error"):
        print(render_text(report), end="", file=_api.sys.stderr)
        return 1
    print(render_text(report), end="")
    return 0


def _render_stat_report_text(_api, /, report: dict, *, requested_harness: str | None) -> str:
    """Assemble the full human-readable `review stat` report from its sections.
    `requested_harness` (the raw `--harness` value, if any) only affects the empty-table
    message — see `_render_stat_harness_table`."""
    sections = [
        _api._render_stat_header(report),
        _api._render_stat_harness_table(
            report["harnesses"], requested_harness=requested_harness
        ),
        _api._render_stat_fable_section(report["fable"]),
        _api._render_stat_retry_section(report["retry_events_by_kind"]),
        _api._render_stat_mode_section(report["by_mode"]),
        _api._render_stat_repo_section(report["by_repo_top"]),
        _api._render_stat_oversized_section(report["top_oversized_calls"]),
        "Note: real token counts exist ONLY for REST-backed calls "
        f"({', '.join(report['tokens_recorded_backends'])}); the agentic CLI harnesses "
        "(oc/opencode, omp, codex, claude in CLI mode) show tokens: not captured (see "
        "below) — bytes are the best available cross-harness proxy today. Each of these "
        "CLIs DOES expose exact usage/cost via its own --json/--format json mode "
        "(verified live); review-cli doesn't invoke them that way yet because it would "
        "replace their readable stdout wholesale, breaking the paywall/auth detection "
        "this report itself relies on — tracked separately, see review-cli"
        "#186. `cc` is not a separate harness — it resolves to `commandcode`, same as "
        "`--harness cc` (see `--harness`'s help text for the full alias list).",
    ]
    return "\n\n".join(sections)


def _render_stat_header(_api, /, report: dict) -> str:
    window = f"since {report['since']}" if report["since"] else "all recorded history"
    return (
        f"review stat — {report['log_dir']} ({window})\n"
        f"calls: {report['call_count']}   retry/promotion events: {report['retry_event_count']}"
    )


def _render_stat_harness_table(_api, /, harnesses: dict, *, requested_harness: str | None) -> str:
    """One row per backend: call/health counts, byte-proxy distribution, real tokens
    (REST backends only), and the SKILL.md/MEMORY.md context-pollution rate.

    `requested_harness` distinguishes "genuinely nothing in this window" from "calls
    exist, just none for the requested --harness" (kimi review finding: the generic
    message was false/misleading in the latter case — this table can be legitimately
    empty here while `report['call_count']` in the header above is nonzero)."""
    if not harnesses:
        if requested_harness:
            return (
                f"No calls recorded for harness {requested_harness!r} in this window "
                "(other harnesses may still have activity — see the header above)."
            )
        return "No calls recorded in this window."
    from .dashboard.tokenstats import format_bytes

    header = (
        f"{'HARNESS':<14}{'CALLS':>7}{'OK':>6}{'FAIL':>6}{'RUN':>5}  "
        f"{'BYTES':>9}{'FAIL%B':>7}{'AVG':>9}{'P90':>9}{'MAX':>9}  {'TOK(real)':>10}  SKILL.md  MEMORY.md"
    )
    rows = [header, "-" * len(header)]
    for name, hs in harnesses.items():
        tok = (
            f"{hs['tokens_prompt']}/{hs['tokens_output']}" if hs["tokens_real"] else "-"
        )
        calls = hs["calls"] or 1
        skill_pct = f"{100 * hs['skill_md_calls'] / calls:.0f}%"
        mem_pct = f"{100 * hs['memory_md_calls'] / calls:.0f}%"
        fail_b_pct = f"{hs['bytes_fail_pct']:.0f}%"
        rows.append(
            f"{name:<14}{hs['calls']:>7}{hs['ok']:>6}{hs['fail']:>6}{hs['running']:>5}  "
            f"{format_bytes(hs['bytes_total']):>9}{fail_b_pct:>7}{format_bytes(hs['bytes_avg']):>9}"
            f"{format_bytes(hs['bytes_p90']):>9}{format_bytes(hs['bytes_max']):>9}  "
            f"{tok:>10}  {skill_pct:>8}  {mem_pct:>9}"
        )
    title = (
        "Per-harness breakdown (bytes = call-log size, a token PROXY for every "
        "harness; FAIL%B = share of that harness's total bytes spent on calls that "
        "failed anyway — the retry-inflation cost signal; TOK(real) = exact "
        "prompt/output tokens, REST backends only):"
    )
    return title + "\n" + "\n".join(rows)


def _render_stat_fable_section(_api, /, fable: dict) -> str:
    """Surfaces the investigation's headline finding: the Fable seat's dispatch/failure
    rate and WHY it failed (session-limit vs paywall vs auth vs other).

    review-cli#fable-seat-reliability: the label dropped "priority-1" — that demotion
    (DEFAULT_BOARD priority 1 -> last-resort reserve) is the whole point of that
    change, so a report still claiming Fable sits at priority 1 would be actively
    wrong about its own subject."""
    if not fable["dispatch_attempts"] and not fable["retry_events"]:
        return "Fable (board seat): no dispatch attempts recorded in this window."
    rate = (
        f"{fable['failure_rate']:.0%}" if fable["failure_rate"] is not None else "n/a"
    )
    reasons = fable["retry_event_reasons"]
    return (
        "Fable (board seat) pattern:\n"
        f"  dispatch attempts: {fable['dispatch_attempts']}   "
        f"cached-skips: {fable['cached_skips']}   failure rate: {rate}\n"
        f"  retry/promotion events: {fable['retry_events']} "
        f"(session_limit={reasons['session_limit']} paywall={reasons['paywall']} "
        f"auth={reasons['auth']} other={reasons['other']})\n"
        "  note: dispatch_attempts/failure_rate are a LOWER BOUND — a claude CLI-mode "
        "call is only attributable to Fable when its body carries the paywall sentinel; "
        "a successful Fable dispatch or a session-limit-shaped failure is attributed to "
        "Opus instead (see reviewlib.dashboard.tokenstats.compute_fable_report)."
    )


def _render_stat_retry_section(_api, /, by_kind: dict) -> str:
    if not by_kind:
        return "Retry/promotion events: none recorded in this window."
    parts = " ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items()))
    return f"Retry/promotion events by kind: {parts}"


def _render_stat_mode_section(_api, /, by_mode: list[dict]) -> str:
    """`reviewlib.stats.mode_summary` rendered — busiest MODE first (by total real
    wall-clock, not call-log bytes: this section is sourced from run-stats.jsonl, the
    ground-truth store, not the call logs the rest of this report reads)."""
    if not by_mode:
        return "By mode: no run-stats.jsonl history in this window."
    header = f"{'MODE':<12}{'RUNS':>6}{'DURATION':>10}{'AVG_POOL':>9}{'FAIL%':>7}"
    rows = [header, "-" * len(header)]
    for item in by_mode:
        fail_pct = (
            f"{100 * item['fail_rate']:.0f}%" if item["fail_rate"] is not None else "-"
        )
        rows.append(
            f"{item['mode']:<12}{item['runs']:>6}"
            f"{_api.fmt_duration(item['duration_seconds']):>10}"
            f"{item['avg_pool_size']:>9}{fail_pct:>7}"
        )
    return "By mode (from run-stats.jsonl):\n" + "\n".join(rows)


def _render_stat_repo_section(_api, /, by_repo: list[dict]) -> str:
    """`reviewlib.stats.repo_summary` rendered — busiest repo first, by run count.
    Only records with a resolvable `repo_id` are counted (see that function's
    docstring for why the rest are excluded rather than lumped as "unknown")."""
    if not by_repo:
        return "By repo: no repo-identified run-stats.jsonl history in this window."
    header = f"{'REPO':<50}{'RUNS':>6}{'DURATION':>10}{'FAIL':>6}"
    rows = [header, "-" * len(header)]
    for item in by_repo:
        repo = item["repo_id"]
        if len(repo) > 48:
            repo = "…" + repo[-47:]
        rows.append(
            f"{repo:<50}{item['runs']:>6}"
            f"{_api.fmt_duration(item['duration_seconds']):>10}{item['fail_count']:>6}"
        )
    return "By repo, top by run count (from run-stats.jsonl):\n" + "\n".join(rows)


def _render_stat_oversized_section(_api, /, top: list[dict]) -> str:
    """The largest calls by log size — the investigation's own outlier-hunting method,
    now a standing report instead of a one-off manual pass."""
    if not top:
        return "Top oversized calls: none recorded in this window."
    from .dashboard.tokenstats import format_bytes

    lines = [f"Top {len(top)} oversized calls:"]
    for i, call in enumerate(top, start=1):
        task = call["task_code"] or "-"
        flags = []
        if call["diff_git_files"]:
            flags.append(f"diff_git_files={call['diff_git_files']}")
        if call["binary_stub_files"]:
            flags.append(f"binary_stub_files={call['binary_stub_files']}")
        if call["skill_md"]:
            flags.append("SKILL.md")
        if call["memory_md"]:
            flags.append("MEMORY.md")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  {i}. {call['backend']:<12} {format_bytes(call['size_bytes']):>9}  "
            f"task={task}{flag_str}"
        )
    return "\n".join(lines)


def _write_feedback(_api, dashboard_store, ns, record, key):
    try:
        if ns.ticket is not None:
            dashboard_store.validate_ticket(ns.ticket)
        if ns.pr is not None:
            dashboard_store.validate_pr(ns.pr)
    except ValueError as exc:
        print(f"[review feedback] {exc}", file=_api.sys.stderr)
        return 2

    ticket_to_link = ns.ticket
    if ticket_to_link is None and record is not None:
        task_code = record.get("task_code")
        if isinstance(task_code, str) and dashboard_store.looks_like_ticket(
            task_code
        ):
            ticket_to_link = task_code

    # Review finding, round 2: applying note/useful/ticket/pr as up to FOUR
    # separate load-modify-write cycles (even after validating shapes up front)
    # still let an I/O failure or a concurrent writer land BETWEEN two of them,
    # leaving a genuinely partial annotation. `apply_feedback` does the whole
    # thing as ONE locked critical section — real all-or-nothing, not just
    # validated-then-hopefully-uninterrupted.
    dashboard_store.apply_feedback(
        key,
        feedback=ns.note if ns.note is not None else dashboard_store.UNSET,
        useful=(
            _api._USEFUL_CHOICES[ns.useful]
            if ns.useful is not None
            else dashboard_store.UNSET
        ),
        ticket=ticket_to_link,
        pr=ns.pr,
    )
    return None
