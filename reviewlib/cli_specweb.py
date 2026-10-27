"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import Path, TextIO

def _spec_web(_api, /, argv: list[str]) -> int:
    """Dispatch ``review spec-web`` — daemon lifecycle + spec registration + the reply/watch glue.

    A bare ``review spec-web`` prints HELP and launches nothing. The daemon lifecycle
    (start/status/stop/run/enable/disable) is delegated to the shared ``agenttools_service`` lib
    (see ``reviewlib.specweb.service`` — same pattern as ``review dashboard``); only the
    spec-web-unique surface (add/serve/list/remove/watch/reply and the legacy positional) lives
    here. See ``reviewlib.specweb`` for the full design.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(_api._SPECWEB_HELP)
        return 0
    sub = argv[0]
    if sub == "reply":
        return _api._spec_web_reply(argv[1:])
    if sub == "__serve":
        return _api._spec_web_serve_daemon(argv[1:])
    if sub in _api._SPECWEB_LIFECYCLE:
        return _api._spec_web_lifecycle(sub, argv[1:])
    if sub == "list":
        return _api._spec_web_list(argv[1:])
    if sub == "remove":
        return _api._spec_web_remove(argv[1:])
    if sub == "watch":
        return _api._spec_web_watch(argv[1:])
    if sub == "add":
        return _api._spec_web_add(argv[1:], watch=False)
    if sub == "serve":
        return _api._spec_web_add(argv[1:], watch=True)
    # A legacy positional `review spec-web <spec.md>`: register + serve-in-daemon + watch for the
    # submit (backward-compatible with the old blocking single-spec command).
    return _api._spec_web_add(argv, watch=True)


def _spec_web_host_port(_api, /, rest: list[str], *, defaults: bool):
    """Parse the optional ``--host``/``--port`` (+ ``--agent``) shared by the spec-web daemon
    commands.

    Returns ``(ns, remaining_positionals)``. With ``defaults=True`` the daemon defaults
    (0.0.0.0 / the stable port) are applied; the raw values (possibly None) are used otherwise."""
    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    parser = _api.argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("--host", default=DEFAULT_SPECWEB_HOST if defaults else None)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_SPECWEB_PORT if defaults else None
    )
    parser.add_argument("--agent", default=None)
    return parser.parse_known_args(rest)


def _spec_web_require_agent(_api, /, action: str, agent: str | None) -> int | None:
    """Exit-2 usage error when a daemon-LAUNCHING action lacks ``--agent``, else None.

    The agent names the tmux window/session (e.g. ``--agent ext``) that owns the served
    specs: every submitted review batch is injected into that session (see specweb.deliver),
    so an agentless daemon would silently strand reviews in the store again."""
    if agent:
        return None
    print(
        f"[review spec-web] `{action}` requires --agent <name> — the tmux window/session "
        f"that owns the served specs (submitted reviews are delivered INTO that session).\n"
        f"  use:  review spec-web {action} --agent <name>   (e.g. --agent ext)",
        file=_api.sys.stderr,
    )
    return 2


def _spec_web_manager(_api, /, host: str, port: int | None, agent: str | None):
    """Build the agenttools_service ServiceManager for the spec-web daemon, or None if the lib
    is absent (the caller prints the shared missing-lib guidance and returns exit 4)."""
    try:
        from agenttools_service import ServiceManager
    except ImportError:
        return None
    from .specweb.service import spec_web_service

    return ServiceManager(spec_web_service(host=host, port=port, agent=agent))


def _spec_web_serve_daemon(_api, /, rest: list[str]) -> int:
    """Hidden entry: ``review spec-web __serve`` — the blocking multi-spec daemon (this shell).

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``);
    it calls ``run_daemon`` directly so it never re-enters the service dispatcher. Not advertised
    — operators use ``run`` (ad-hoc) or ``start`` (managed)."""
    ns, _ = _api._spec_web_host_port(rest)
    rc = _api._spec_web_require_agent("__serve", ns.agent)
    if rc is not None:
        return rc
    verbose = "--verbose" in rest
    from .specweb.server import run_daemon

    return run_daemon(host=ns.host, port=ns.port, verbose=verbose, agent=ns.agent)


def _spec_web_lifecycle(_api, /, action: str, rest: list[str]) -> int:
    """Run a daemon lifecycle action (start/status/stop/run/enable/disable) via the shared lib.

    ``status``/``start`` additionally print the registered specs + their name-based URLs (the
    spec-web-specific bit), so the operator immediately sees what the daemon is serving.

    ``start`` is IDEMPOTENT: an already-running daemon is reported (pid + what it serves) and
    exits 0 — a repeated ``start`` in a setup script must not read as a failure. (The shared
    ``run_action`` alone would exit 3 there; that convention is for ``status``/``stop``'s
    "was anything up?" branch, not for a start that got what it asked for.)

    The daemon-LAUNCHING actions (start/run/enable) REQUIRE ``--agent`` — see
    ``_spec_web_require_agent``; status/stop/disable manage the existing instance and don't."""
    ns, _ = _api._spec_web_host_port(rest)
    if action in _api._SPECWEB_AGENT_REQUIRED:
        rc = _api._spec_web_require_agent(action, ns.agent)
        if rc is not None:
            return rc
    mgr = _api._spec_web_manager(ns.host, ns.port, ns.agent)
    if mgr is None:
        print(_api._SPECWEB_MISSING_LIB, file=_api.sys.stderr)
        return 4
    if action == "start":
        st = mgr.status()
        if st.running:
            suffix = f" (pid {st.pid})" if st.pid is not None else ""
            print(f"[review spec-web] already running{suffix} — nothing to start.")
            _api._spec_web_print_registry(ns.host, ns.port)
            return 0
    from agenttools_service import run_action

    rc = run_action(mgr, action)
    if action in ("start", "status"):
        _api._spec_web_print_registry(ns.host, ns.port)
    return rc


def _clip_text(_api, /, text: str, limit: int, *, collapse_whitespace: bool) -> str:
    """Truncate ``text`` to ``limit`` characters with a trailing ellipsis, stripping any
    trailing whitespace left by the cut. ``collapse_whitespace`` additionally joins the
    text onto one line first (used for the terminal reminder line, where an embedded
    newline would otherwise break the "one line per comment" contract; not used for the
    tg message body, which renders literal newlines fine)."""
    if collapse_whitespace:
        text = " ".join(text.split())
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _waiting_on_phrase(_api, /, status: str | None) -> str:
    """What a stalled comment in ``status`` is waiting on: an agent reply for
    ``submitted``, reviewer resolution for ``answered``. Shared by `_stale_reminder_line`
    and `_stale_summary_for_tg` so the per-comment line and the tg summary can't drift
    into describing the same stall differently. `stale_comments()` only ever produces
    these two statuses (`OPEN_CYCLE_STATUSES`); an unexpected value falls back to
    "reviewer resolution" rather than raising, since this is a display-only formatter."""
    return _api._WAITING_ON.get(status, "reviewer resolution")


def _stale_reminder_line(_api, /, stale: dict) -> str:
    """One indented reminder line for a comment `specweb.store.stale_comments` flagged as
    a stalled review-cycle: stuck in ``submitted`` (the agent hasn't replied) or
    ``answered`` (the agent replied, the reviewer hasn't resolved it) well past the
    reminder threshold. Shared verbatim (via `_print_stale_reminders`) by ``spec-web
    list``, ``spec-web start``/``status``, and the ``spec-web reply`` command's stderr
    nudge; the reply command's TG summary (`_stale_summary_for_tg`) uses different
    wording for a multi-comment count, but the same `_waiting_on_phrase`, so at least
    that half can't drift.

    The quote is whitespace-collapsed before clipping: it is the reviewer's verbatim
    selection, which can span a paragraph break, and a literal embedded newline would
    otherwise break this "one line per stalled comment" contract."""
    kind = stale.get("kind") or "remark"
    waiting_on = _api._waiting_on_phrase(stale.get("status"))
    age = stale.get("age_hours") or 0.0
    raw_quote = stale.get("quote") or stale.get("body") or ""
    clipped = _api._clip_text(str(raw_quote), 60, collapse_whitespace=True)
    return (
        f"    ⚠ stalled {kind} #{stale.get('id') or '?'} — {age:.1f}h waiting on "
        f'{waiting_on}: "{clipped}"'
    )


def _print_stale_reminders(_api, /, stale: list[dict], *, stream: TextIO | None) -> None:
    """Print one `_stale_reminder_line` per already-filtered stalled comment (the output
    of `specweb.store.stale_comments` / `SpecStore.stale_comments`) — the single loop
    shared by all three surfaces (`spec-web list`, `spec-web start`/`status`, and the
    reply command's stderr nudge) so they can't drift out of sync with each other.
    ``stream=None`` prints to stdout (``print``'s own default); the reply command passes
    ``sys.stderr`` since its stdout is reserved for the "replied to ..." confirmation."""
    for s in stale:
        print(_api._stale_reminder_line(s), file=stream)


def _comments_for_reminder(_api, /, spec_path: Path | str) -> list[dict]:
    """This spec's comments, or ``[]`` on any read failure (a deleted/corrupted store —
    the caller is printing an at-a-glance summary, not doing something that should abort
    on a single unreadable spec). Shared by `spec-web list` and `spec-web start`/
    `status`, the two registry-driven callers that both fetch-then-reminder per spec; the
    `spec-web reply` surface already has a live, known-good `SpecStore` in hand and calls
    its `stale_comments()` method directly instead."""
    from .specweb.store import SpecStore

    try:
        return SpecStore(spec_path).all_comments()
    except Exception:  # noqa: BLE001
        return []


def _spec_web_print_registry(_api, /, host: str, port: int) -> None:
    """Print the navigator URL(s) + every registered spec's name-based URL (loopback +
    Tailscale), plus a reminder line for any comment stuck in an open review cycle past
    the staleness threshold (see ``specweb.store.stale_comments``). ``start``/``status``
    is the OTHER place (besides ``spec-web list``) an operator glances at routinely, so it
    gets the same lazily-computed nudge — never a background poller, just whatever this
    already-running command happens to read."""
    from .specweb import registry
    from .specweb.server import _reachable_urls, daemon_spec_urls
    from .specweb.store import stale_comments

    for url in _reachable_urls(host, port):
        print(f"[review spec-web] navigator {url}")
    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered yet — add one with `review spec-web add <spec.md>`"
        )
        return
    for rec in specs:
        urls = " ".join(daemon_spec_urls(rec["name"], host, port))
        missing = "" if rec["exists"] else "  (file missing)"
        agent = f"  (agent: {rec['agent']})" if rec.get("agent") else ""
        print(f"[review spec-web]   {rec['name']}: {urls}{agent}{missing}")
        comments = _api._comments_for_reminder(rec["path"])
        _api._print_stale_reminders(stale_comments(comments))


def _spec_web_add(_api, /, rest: list[str], *, watch: bool) -> int:
    """``review spec-web add|serve <spec.md>`` (and the legacy positional): ensure the daemon is
    running, register the spec, print its name-based URL. ``serve`` (and the legacy form) then
    BLOCK watching for the submit -> stdout handoff; ``add`` returns immediately.

    Backward-compat: on a host WITHOUT the shared service lib, the legacy positional falls back
    to the classic single-spec foreground server so `review spec-web <spec>` still works."""
    parser = _api.argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("spec")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--seed", metavar="FILE", default=None)
    parser.add_argument("--open", dest="open_browser", action="store_true")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument("--no-watch", dest="no_watch", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    ns, _ = parser.parse_known_args(rest)

    # `serve` / the legacy positional own a review round-trip — they REQUIRE the owning agent
    # (submitted batches are DELIVERED into that session; see _spec_web_require_agent). A bare
    # `add` may omit it (the spec then inherits the daemon's own --agent at delivery time).
    if watch:
        rc = _api._spec_web_require_agent("serve", ns.agent)
        if rc is not None:
            return rc

    spec = _api.Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web] spec not found: {spec}", file=_api.sys.stderr)
        return 1
    # Canonicalize once, up front: registry + SpecStore key by the RESOLVED path internally,
    # so this keeps every printed path/URL consistent with what the daemon actually serves.
    spec = spec.resolve()

    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    host = ns.host if ns.host is not None else DEFAULT_SPECWEB_HOST
    # NOT `ns.port or DEFAULT`: `--port 0` is falsy and would silently become the default.
    port = ns.port if ns.port is not None else DEFAULT_SPECWEB_PORT

    mgr = _api._spec_web_manager(host, port, ns.agent)
    if mgr is None:
        return _spec_without_service(_api, ns, spec, watch)

    if ns.seed is not None and _api._spec_web_seed(spec, ns.seed) != 0:
        return 1

    from .specweb import registry
    from .specweb.server import daemon_spec_urls, watch_submits
    from .specweb.store import SpecStore

    # Baseline the store's last_submit BEFORE the daemon is (maybe) started / the spec is
    # registered: a submit that lands in the ensure-running/register window must count as
    # FRESH for the watch below, not be folded into the baseline and lost (the caller would
    # then wait forever on a review that already happened).
    baseline = SpecStore(spec).last_submit()

    rc = _api._spec_web_ensure_running(mgr, agent=ns.agent)
    if rc is not None:
        return rc

    name = registry.register(spec, agent=ns.agent)
    urls = daemon_spec_urls(name, host, port)
    print(f"[review spec-web] registered '{name}' -> {spec}")
    for url in urls:
        print(f"[review spec-web] {url}")
    if ns.open_browser and urls:
        _api._spec_web_open_browser(urls[0])
    if watch and not ns.no_watch:
        print(
            "[review spec-web] waiting for a submit (Ctrl-C to stop; the daemon keeps running)."
        )
        return watch_submits(spec, exit_on_submit=ns.exit_on_submit, baseline=baseline)
    return 0


def _spec_web_ensure_running(_api, /, mgr, *, agent: str | None) -> int | None:
    """Start the daemon if it isn't already up (idempotent); None on success, an exit code on
    refusal. A concurrent start losing the race (AlreadyRunningError) is fine — the daemon is
    up either way. LAUNCHING an agentless daemon is refused (nothing may start without an
    owner — see _spec_web_require_agent); an already-running daemon needs no agent here."""
    st = mgr.status()
    if st.running:
        return None
    rc = _api._spec_web_require_agent("start", agent)
    if rc is not None:
        print(
            "[review spec-web] the daemon is not running and cannot be auto-started "
            "without --agent.",
            file=_api.sys.stderr,
        )
        return rc
    # AlreadyRunningError lives in agenttools_daemon (the pidfile layer agenttools_service
    # builds on) — it is NOT re-exported by agenttools_service. Imported only on the
    # actually-starting path so a fake manager in tests never needs the lib installed.
    from agenttools_daemon import AlreadyRunningError

    try:
        st = mgr.start()
    except AlreadyRunningError:
        return None
    suffix = f" (pid {st.pid})" if st.pid is not None else ""
    print(f"[review spec-web] daemon started{suffix}.")
    return None


def _spec_web_seed(_api, /, spec: Path, seed: str) -> int:
    """Import an initial review thread into ``spec``'s store before serving (``--seed``)."""
    import json as _json

    from .specweb.store import SpecStore

    seed_path = _api.Path(seed).expanduser()
    try:
        payload = _json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[review spec-web] cannot read seed {seed_path}: {exc}", file=_api.sys.stderr)
        return 1
    replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
    try:
        result = SpecStore(spec).import_thread(payload, replace=replace)
    except ValueError as exc:
        print(f"[review spec-web] bad seed: {exc}", file=_api.sys.stderr)
        return 1
    print(f"[review spec-web] seeded {result['imported']} comment(s) from {seed_path}")
    return 0


def _spec_web_open_browser(_api, /, url: str) -> None:
    import threading
    import webbrowser

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Timer(0.4, _open).start()


def _spec_web_legacy_foreground(_api, /, spec: Path, ns) -> int:
    """The classic single-spec blocking server (fallback when the shared service lib is absent).

    The caller has already enforced ``--agent`` (the watch/serve path requires it up front),
    so submitted batches are tmux-delivered to the owning session here too — the lib-less
    fallback must not regress into store-only submits."""
    from .specweb.server import run_specweb

    return run_specweb(
        spec,
        host=ns.host or "127.0.0.1",
        port=ns.port,
        open_browser=ns.open_browser,
        seed=ns.seed,
        verbose=ns.verbose,
        exit_on_submit=ns.exit_on_submit,
        agent=ns.agent,
    )


def _spec_web_list(_api, /, rest: list[str]) -> int:
    """``review spec-web list`` — every registered spec with its open-note count, plus a
    reminder line for any comment stuck in an open review cycle (submitted/answered) past
    the staleness threshold (see ``specweb.store.stale_comments``) — otherwise a stalled
    thread is silent until someone happens to open the spec-web UI and notices its age."""
    from .specweb import registry
    from .specweb.store import stale_comments

    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered. Add one with `review spec-web add <spec.md>`."
        )
        return 0
    for rec in specs:
        comments = _api._comments_for_reminder(rec["path"])
        openc = sum(1 for c in comments if c.get("status") in ("pending", "submitted"))
        flag = "" if rec["exists"] else "  (file missing)"
        print(
            f"  {rec['name']:<28} {openc} open / {len(comments)} total  {rec['path']}{flag}"
        )
        _api._print_stale_reminders(stale_comments(comments))
    return 0


def _spec_web_remove(_api, /, rest: list[str]) -> int:
    """``review spec-web remove <name>`` — unregister a spec (its comments are kept on disk)."""
    if not rest:
        print("[review spec-web] usage: review spec-web remove <name>", file=_api.sys.stderr)
        return 2
    from .specweb import registry

    name = rest[0]
    if registry.unregister(name):
        print(
            f"[review spec-web] removed '{name}' (its comment store is left on disk)."
        )
        return 0
    print(f"[review spec-web] no registered spec named '{name}'.", file=_api.sys.stderr)
    return 1


def _spec_web_watch(_api, /, rest: list[str]) -> int:
    """``review spec-web watch <name|path> [--emit-current] [--exit-on-submit]`` — block until a
    fresh submit.

    ``--emit-current`` first re-emits the batch ALREADY in the store — the recovery path when a
    submit's live tmux delivery failed (a bare ``watch`` only fires on a LATER submit, so it
    would never re-surface the already-submitted batch)."""
    parser = _api.argparse.ArgumentParser(prog="review spec-web watch", add_help=False)
    parser.add_argument("target", help="a registered spec NAME or a spec PATH")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument(
        "--emit-current",
        dest="emit_current",
        action="store_true",
        help="re-emit the batch already in the store before watching (recover a failed live delivery)",
    )
    ns, _ = parser.parse_known_args(rest)
    from .specweb import registry
    from .specweb.server import watch_submits

    spec = registry.resolve(ns.target)
    if spec is None:
        spec = _api.Path(ns.target).expanduser()
    if not spec.is_file():
        print(
            f"[review spec-web watch] no such spec (name or path): {ns.target}",
            file=_api.sys.stderr,
        )
        return 1
    print(f"[review spec-web] watching {spec} for a submit (Ctrl-C to stop).")
    return watch_submits(
        spec, exit_on_submit=ns.exit_on_submit, emit_current=ns.emit_current
    )


def _spec_web_reply(_api, /, argv: list[str]) -> int:
    """`review spec-web reply <comment-id> <answer> --spec <spec.md>`: the AGENT answers a
    reviewer's question/remark. Threads the reply into the store (so the spec-web UI shows
    it under that comment) and best-effort delivers it to the user via the `tg` CLI.

    The spec is required (the store is keyed per spec): pass it as ``--spec``. The reply is
    stamped with the agent author so the UI styles it distinctly.
    """
    parser = _api.argparse.ArgumentParser(
        prog="review spec-web reply",
        description="Answer a reviewer's spec-web question/remark (shown in the UI + sent to tg).",
    )
    parser.add_argument(
        "comment_id",
        help="the id of the comment/question to answer (from the structured review)",
    )
    parser.add_argument("answer", help="the answer text")
    parser.add_argument(
        "--spec",
        required=True,
        metavar="FILE",
        help="path to the spec markdown file (the store is keyed per spec)",
    )
    parser.add_argument(
        "--no-tg",
        action="store_true",
        help="do not deliver the reply to Telegram (UI only)",
    )
    ns = parser.parse_args(argv)

    from .specweb.store import AGENT_AUTHOR, SpecStore

    spec = _api.Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web reply] spec not found: {spec}", file=_api.sys.stderr)
        return 1
    answer = (ns.answer or "").strip()
    if not answer:
        print("[review spec-web reply] answer is empty", file=_api.sys.stderr)
        return 2

    store = SpecStore(spec)
    rec = store.add_reply(ns.comment_id, body=answer, author=AGENT_AUTHOR)
    if rec is None:
        print(
            f"[review spec-web reply] unknown comment id: {ns.comment_id}",
            file=_api.sys.stderr,
        )
        return 1
    print(
        f"[review spec-web reply] replied to {ns.comment_id} (now {rec.get('status')}); shown in the spec-web UI.",
        flush=True,
    )

    # Piggyback the proactive reminder on a command that's already touching this spec's
    # store — a reply is exactly the moment an agent is paying attention to this thread,
    # so it's the cheapest place to also flag every OTHER thread still stuck in an open
    # review cycle, with no background poller involved (see specweb.store.stale_comments).
    stale_others = [s for s in store.stale_comments() if s.get("id") != rec.get("id")]
    _api._print_stale_reminders(stale_others, stream=_api.sys.stderr)

    if not ns.no_tg:
        _api._spec_web_reply_to_tg(spec, rec, answer, stale_others=stale_others)
    return 0


def _stale_summary_for_tg(_api, /, stale: list[dict]) -> str:
    """A one-line proactive nudge for the tg reply message: how many OTHER comments on
    this spec are stuck in an open review cycle past the staleness threshold, and how
    long the oldest one has been waiting. Empty when there is nothing stale (the common
    case), so a normal reply's tg message is unchanged — this only adds a block when
    there is actually something to nudge about."""
    if not stale:
        return ""
    # max() by age rather than trusting stale[0]: stale_comments() sorts oldest-first, and
    # the production caller (_spec_web_reply) preserves that via a plain list-comp filter,
    # but nothing enforces it on this function's own signature.
    oldest = max(stale, key=lambda s: s.get("age_hours") or 0.0)
    age = oldest.get("age_hours") or 0.0
    plural = "" if len(stale) == 1 else "s"
    statuses = {s.get("status") for s in stale}
    if len(statuses) <= 1:
        # every stalled comment is waiting on the same thing — the single-phrase form
        # from the oldest applies to the whole group.
        waiting_on = _api._waiting_on_phrase(oldest.get("status"))
        return (
            f"⚠ {len(stale)} other stalled review thread{plural} on this spec "
            f"(oldest {age:.1f}h, waiting on {waiting_on})."
        )
    # A mix of submitted/answered comments: attributing the OLDEST one's wait-phrase to
    # the whole group would misdescribe the others (e.g. "waiting on reviewer resolution"
    # when a different thread in the same count is actually waiting on an agent reply).
    # Break the count down per status instead of picking one phrase for everything.
    submitted_n = sum(1 for s in stale if s.get("status") == "submitted")
    answered_n = len(stale) - submitted_n
    return (
        f"⚠ {len(stale)} other stalled review thread{plural} on this spec "
        f"({submitted_n} awaiting an agent reply, {answered_n} awaiting reviewer "
        f"resolution; oldest {age:.1f}h)."
    )


def _spec_web_reply_to_tg(_api, /, spec: Path, comment: dict, answer: str, *, stale_others: list[dict] | None) -> None:
    """Best-effort: deliver the agent's reply to the user via the `tg` CLI on PATH. NEVER
    raises — tg being absent/failing must not fail the reply (it is already in the store /
    UI). Logs the outcome.

    ``stale_others`` (this reply's own comment already excluded by the caller) is appended
    as a short proactive-reminder block — the tg message that already tells the reviewer
    "here's my answer" is also the cheapest existing delivery channel to surface OTHER
    threads still stuck in an open review cycle, with zero new infrastructure."""
    import shutil
    import subprocess

    exe = shutil.which("tg")
    if not exe:
        print(
            "[review spec-web reply] tg not on PATH — reply saved to the UI only (no Telegram).",
            flush=True,
        )
        return
    question = (comment.get("body") or "").strip()
    quote = (comment.get("quote") or "").strip()
    kind = comment.get("kind") or "remark"

    # Bound the reviewer's free text so a long multi-paragraph remark can't blow past
    # Telegram's ~4096-char message limit (the answer is always shown in full).
    # Plain-text message (no --format html) so we never have to escape the spec/question
    # free text. tg's own --title/--tag give it structure.
    lines = [f"Spec: {spec.name}"]
    if quote:
        lines.append(f"On: “{_api._clip_text(quote, 200)}”")
    lines.append(
        f"{'Question' if kind == 'question' else 'Remark'}: {_api._clip_text(question, 600)}"
    )
    lines.append(f"Agent answer: {_api._clip_text(answer, 3000)}")
    summary = _api._stale_summary_for_tg(stale_others or [])
    if summary:
        lines.append("")
        lines.append(summary)
    message = "\n".join(lines)
    try:
        proc = subprocess.run(
            [
                exe,
                "--tag",
                "ANSWER",
                "--title",
                f"Spec-web reply — {spec.name}",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            print("[review spec-web reply] delivered to Telegram via tg.", flush=True)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            print(
                f"[review spec-web reply] tg delivery failed (exit {proc.returncode}): {err}",
                flush=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[review spec-web reply] tg delivery error: {exc}", flush=True)


def _spec_without_service(_api, ns, spec, watch):
    if ns.no_watch or not watch:
        # `add` / `--no-watch` mean "register into the daemon + return fast"; without the
        # service lib there is no daemon to register into — the only lib-less mode is the
        # BLOCKING single-spec server, the opposite of what was asked (and what the
        # backstop classifier assumes). Refuse loudly rather than silently blocking.
        print(_api._SPECWEB_MISSING_LIB, file=_api.sys.stderr)
        print(
            "[review spec-web] add/--no-watch need the daemon; without the lib the only "
            "mode is the blocking single-spec server (`review spec-web <spec.md>`).",
            file=_api.sys.stderr,
        )
        return 4
    # Lib-less host: preserve the classic single-spec foreground server (full backward-compat).
    print(
        "[review spec-web] shared service lib absent — falling back to a single-spec server.",
        file=_api.sys.stderr,
    )
    return _api._spec_web_legacy_foreground(spec, ns)
