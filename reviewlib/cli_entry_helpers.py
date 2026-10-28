"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import ConfigError

if TYPE_CHECKING:
    from .cli import Path

def _is_persistent_server_invocation(_api, /, argv: list[str]) -> bool:
    """True when argv starts a PERSISTENT server that runs until Ctrl-C and so must bypass the
    `-o` tee + the run backstop. The short-lived management actions are NOT servers — they
    return immediately — so they go through the normal tee/backstop path like any instant
    subcommand:

      * ``spec-web reply …``  — returns immediately, not a server.
      * ``dashboard`` lifecycle actions (``start``/``status``/``stop``/``enable``/``disable``
        and the bare HELP) — return immediately. Only the FOREGROUND blocking server blocks:
        ad-hoc ``dashboard run`` and the hidden ``dashboard __serve`` it dispatches to.
    """
    if not argv or argv[0] not in _api._SERVER_SUBCOMMANDS:
        return False
    if argv[0] == "spec-web":
        return _api._spec_web_is_persistent(argv[1:])
    if argv[0] == "dashboard":
        # Only the blocking foreground server is persistent; everything else returns fast.
        # The managed-service parser accepts the global `--host`/`--port` options BEFORE the
        # action (`dashboard --port 7878 run`), so the action is NOT necessarily argv[1] — it
        # is the first NON-OPTION token. Misclassifying `--port N run` as non-persistent would
        # wrap the foreground server in the run backstop and let it be killed. `--host`/`--port`
        # are the only options that take a value here; skip the flag and its argument.
        return _api._dashboard_action(argv[1:]) in ("run", "__serve")
    return True


def _spec_web_is_persistent(_api, /, rest: list[str]) -> bool:
    """True when ``review spec-web <rest>`` blocks (foreground daemon / submit-watch), so it must
    NOT be wrapped in the run backstop. A bare ``spec-web`` (help) and the fast management actions
    return immediately; ``run``/``__serve`` are the blocking daemon; ``watch`` always blocks;
    ``serve`` and the LEGACY positional ``spec-web <path>`` block on the submit-watch unless
    ``--no-watch``."""
    if not rest:
        return False  # bare `review spec-web` prints help + launches nothing
    sub = rest[0]
    if sub in _api._SPECWEB_FAST_ACTIONS:
        return False
    if sub in ("run", "__serve", "watch"):
        return True  # `watch` has no --no-watch: it exists only to block on the submit
    if sub == "serve":
        return "--no-watch" not in rest
    # A legacy positional `review spec-web <path>` (register + serve-in-daemon + watch, or the
    # lib-less foreground fallback) blocks unless `--no-watch`. `--no-watch` returns fast in
    # BOTH worlds: daemon mode registers + returns; the lib-less fallback REFUSES it (exit 4)
    # rather than silently blocking (see _spec_web_add).
    return "--no-watch" not in rest


def _dashboard_action(_api, /, rest: list[str]) -> str | None:
    """The dashboard ACTION (run/start/…) from the tokens after ``dashboard``, or ``None``."""
    return _api._dashboard_action_with_index(rest)[0]


def _dashboard_action_with_index(_api, /, rest: list[str]) -> tuple[str | None, int]:
    """``(action, index)`` for the dashboard action token, or ``(None, -1)``.

    Skips the global ``--host``/``--port`` options (and their values) that the managed-service
    parser allows before the action, so ``--port 7878 run`` resolves to ``run`` — matching how
    argparse itself parses it (see :func:`_dashboard_subcommand`). The index lets a caller drop
    exactly the action token (not a value that merely equals it)."""
    i = 0
    value_opts = ("--host", "--port")
    while i < len(rest):
        tok = rest[i]
        if tok in value_opts:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            # `--port=7878` or any other valueless flag — skip the single token.
            i += 1
            continue
        return tok, i  # first non-option token == the action
    return None, -1


def _output_option_value(argv, index):
    """Recognize output syntax only after the caller has excluded option values."""
    token = argv[index]
    if token in ("-o", "--output"):
        return (argv[index + 1], 2) if index + 1 < len(argv) else (None, 0)
    if token.startswith(("--output=", "-o=")):
        return token.split("=", 1)[1], 1
    if token.startswith("-o") and len(token) > 2:
        return token[2:], 1
    return None, 0


def _extract_output_path(_api, /, argv: list[str]) -> tuple[Path | None, list[str]]:
    """Pull the output flag OUT of argv before dispatch and return (path, remaining).

    Recognized forms: `-o FILE`, `--output FILE`, `--output=FILE`, `-o=FILE`, and the
    glued short `-oFILE`. `-o` is handled OUTSIDE the main argparse surface because the
    capture has to wrap the WHOLE dispatch (every mode prints its final result to
    stdout), and the bare subcommands (install-skill, dashboard, spec-web, …) never
    reach the main parser. A single light pre-scan here makes `-o` work uniformly for
    every path while the parser still advertises it in `--help`.

    Two safeguards keep the pre-scan from misreading another option's value as the
    output flag: (1) scanning STOPS at the first `--` (end-of-options), so a positional
    that starts with `-o` is kept verbatim; (2) a token that is the VALUE of a preceding
    value-taking option (`--just-ask --output`, `--prompt -o…`) is NOT intercepted — it
    is passed through so argparse still receives that option's argument. When the flag
    is absent the remaining list has the SAME contents as the input (a fresh list); a
    bare `-o` with no value is left in the remaining argv so argparse reports the usage
    error instead of a silent swallow."""
    out: Path | None = None
    rest: list[str] = []
    i = 0
    value_for_previous = False
    # `review task CODE --check` is a BOOLEAN flag (no value) — the sole exception to
    # `--check` meaning `--check NAME` (visual module force-activation) everywhere else.
    # Exclude it from the value-taking set for a `task` invocation so this pre-scan
    # doesn't mistake a following `-o FILE` for --check's (nonexistent) value.
    value_taking_opts = (
        _api._VALUE_TAKING_OPTS - {"--check"}
        if argv and argv[0] == "task"
        else _api._VALUE_TAKING_OPTS
    )
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # End of options: keep `--` and everything after it untouched.
            rest.extend(argv[i:])
            break
        # If the prior token we recognized as a value-taking option (space form), THIS
        # token is its value — pass it through, never read it as the output flag. Track
        # this as state, not by peeking at argv[i-1], because a *value* can itself equal
        # another value-taking flag string (e.g. `--task --output -o out.md`).
        if value_for_previous:
            rest.append(tok)
            value_for_previous = False
            i += 1
            continue
        output_value, consumed = _output_option_value(argv, i)
        if consumed:
            out = _api.Path(output_value).expanduser()
            i += consumed
            continue
        rest.append(tok)
        if tok in value_taking_opts:
            value_for_previous = True
        i += 1
    return out, rest


def _is_leading_inline_short_option(_api, /, tok: str) -> bool:
    """True only for glued short global forms like `-mMODEL`, never long flags."""
    return (
        len(tok) > 2
        and tok.startswith("-")
        and not tok.startswith("--")
        and any(
            tok.startswith(opt) and tok != opt
            for opt in _api._LEADING_MODE_INLINE_SHORT_OPTS
        )
    )


def _leading_option_width(_api, token):
    """Return recognized global option width, without consuming its value."""
    if token in _api._LEADING_MODE_VALUE_OPTS:
        return 2
    if token in _api._LEADING_MODE_FLAG_OPTS:
        return 1
    prefixes = tuple(f"{option}=" for option in _api._LEADING_MODE_VALUE_OPTS if option.startswith("--"))
    if token.startswith(prefixes) or _api._is_leading_inline_short_option(token):
        return 1
    return 0


def _normalize_leading_mode_options(_api, /, argv: list[str]) -> list[str]:
    """Allow truly-global options before a mode verb.

    The mode parser already accepts `review diff -m fable ...`; users also naturally type
    `review -m fable diff ...` because `-m` is advertised as global. Move only recognized
    global options that appear before a known mode verb to just after that verb, leaving
    unknown/management invocations untouched so argparse still reports the right error.
    """
    if not argv or (not argv[0].startswith("-") and argv[0] in _api.known_subcommands()):
        return argv
    moved: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv
        if not tok.startswith("-"):
            if tok in _api.known_subcommands() and moved:
                return [tok, *moved, *argv[i + 1 :]]
            return argv
        width = _leading_option_width(_api, tok)
        if not width or i + width > len(argv):
            return argv
        moved.extend(argv[i:i + width])
        i += width
    return argv


def _write_output_file(_api, /, path: Path, text: str) -> None:
    """Persist captured stdout to `path` via Python `open(...,"w")` — which bypasses
    the shell entirely, so it NEVER trips zsh `noclobber` the way `review … > FILE`
    does (the bug this flag exists to kill). ANSI escape sequences are stripped so the
    file is clean text even if the live stream was coloured. Parent dirs are created;
    an existing file is overwritten (that is the point). A bad path raises a clear
    OSError that the caller turns into a non-zero exit with an actionable message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_api.strip_control_sequences(text), encoding="utf-8")


def _reject_if_reentrant(_api, /, _argv: list[str]) -> int | None:
    """Fail closed if this process is already nested inside a running `review`
    invocation — the review-cli#180 fork-bomb guard.

    A codex/claude/opencode backend can shell out to `review diff` again on the same
    worktree, which spawns another backend, which can do it again — an unbounded
    self-reinvocation loop (live-confirmed 2026-08-11: 40+ live `codex exec` processes
    and 11 `review diff` processes across 4 worktrees, swap at 88.5%, load average
    60+). The existing process-GROUP kill/backstop machinery (`process._run_streamed`,
    `reviewlib.backstop`) cannot bound this: each backend child is spawned with
    `start_new_session=True` so a per-call timeout can kill just that call's tree
    without also taking down the CLI's own group — but that means every recursive
    level re-roots into a BRAND NEW OS session, invisible to a `killpg` rooted at an
    earlier level. An env var is the one signal that survives `exec`/`setsid`
    regardless of how many session boundaries the recursion crosses, so it is checked
    here instead.

    Deliberately NOT reset by `call_backend`/panel concurrency — those run several
    backends in threads WITHIN one process (review-cli#65), not nested `review`
    invocations, so they never trip this.

    VERIFIED (not assumed — re-raised by every review round on this ticket, so
    recording the evidence here): $REVIEW_CLI_ACTIVE is set for a persistent SERVER
    subcommand's (`review dashboard`/`review spec-web`) entire lifetime too (see
    `main()` — it wraps the whole dispatch, server path included). This is safe
    because neither server spawns `review` as a child process during normal request
    handling: `reviewlib/dashboard/server.py` and `reviewlib/specweb/server.py` only
    shell out to `tailscale`/`git`/`tmux`; `reviewlib/dashboard/service.py`'s
    `_review_argv0()` launches the server's OWN foreground process at STARTUP (once,
    from the user's shell — not a request-handling re-invocation) and is itself gated
    against re-entering the service layer (see that module's docstring: "otherwise
    run/start would re-enter the service layer and fork-bomb"). If a future change
    adds a code path where a server-mode process dispatches an actual model review
    while servicing a request, that path should call the mode/panel functions
    directly (in-process, the way `reviewlib.panel`/`reviewlib.modes` are normally
    invoked), not subprocess-invoke `review` on itself.

    Checks PRESENCE (`is not None`), not truthiness: `REVIEW_CLI_ACTIVE= review diff`
    (a shell setting the var to an EMPTY string for the child) must still trip the
    guard — `os.environ.get(...)` would return `""`, which is falsy under a bare
    `if`, and silently let the recursion through (review-cli#180 review finding,
    glm-5.2). This does NOT defend against a shell that removes the var entirely
    (`env -u REVIEW_CLI_ACTIVE review diff`) — no env-var-based guard can, since the
    child then looks identical to a genuinely fresh top-level invocation. That
    residual gap is why this is layered with, not a substitute for, the codex
    execpolicy guard (`install.install_codex_recursion_guard`) and the existing
    process-group timeout/backstop.

    This exact `env -u REVIEW_CLI_ACTIVE` technique is also used DELIBERATELY, not just as an
    attacker's residual gap: `dashboard.service._env_clear_prefix()` (reused by
    `specweb.service._serve_argv`) prepends it to the managed `__serve` child's argv, because
    `run`/`start` are themselves active `review` invocations and the spawned server must NOT
    inherit that activity marker (review-cli#180 review finding, chatgpt-codex-connector, PR
    #279) — a sanctioned use of the same bypass this paragraph documents as unstoppable."""
    if _api.os.environ.get(_api.REVIEW_CLI_ACTIVE_ENV) is not None:
        print(
            "[review-cli] refusing to run: $REVIEW_CLI_ACTIVE is already set, meaning "
            "this process is nested inside another `review` invocation. A backend "
            "(codex/claude/opencode) appears to have re-invoked `review` on the same "
            "worktree — this is the unbounded self-reinvocation loop tracked as "
            "review-cli#180. Refusing to recurse.",
            file=_api.sys.stderr,
        )
        return 1
    return None


def _dispatch_with_backstop(_api, /, raw: list[str], output_path: str | None) -> int:
    """Resolve and run `raw`, applying the internal backstop timer + `-o` tee.

    Split out of `main()` so the REVIEW_CLI_ACTIVE reentrancy guard (review-cli#180)
    can wrap this ENTIRE dispatch — server subcommand included — in one try/finally at
    the call site, without main() itself growing past a screenful.

    `review` advertises NO external timeout — agents must not wrap it in a short
    shell `timeout` (the panel/brainstorm modes only emit their synthesis at the very
    end). The ONLY time bound is the INTERNAL last-resort backstop, capped at <=4h
    (`reviewlib.backstop`): a watchdog that force-terminates a genuinely wedged run so
    "no external timeout" can never mean "runs forever". A healthy run finishes in
    minutes, far under the ceiling, and the watchdog is cancelled cleanly on return.

    The persistent SERVER subcommands (`dashboard`, `spec-web`) are deliberately
    long-lived (they run until Ctrl-C), so they bypass the backstop entirely — bounding
    them would kill the server at the ceiling, and a lowered env var would kill it almost
    at once (codex P2). Every other path (the review/panel run and the instant
    subcommands) is wrapped.

    This is also where `-o FILE` is handled: the flag is pre-scanned out of argv by the
    caller (so it works for every dispatch path, including the bare subcommands), and
    when present the whole dispatch runs under a stdout TEE whose captured text is
    persisted to FILE via Python — bypassing the shell redirect (and thus zsh
    noclobber). The file is always written; stdout still prints live.
    """
    # The persistent SERVER subcommands stream until Ctrl-C — capturing/teeing their
    # output to a single `-o` file makes no sense (and the file would only be written
    # on shutdown), so `-o` is ignored for them and they bypass both the tee and the
    # backstop exactly as before. `review spec-web reply …` is the EXCEPTION: it is a
    # short-lived command, not the server, so it must NOT bypass — `-o` should work and
    # the backstop should bound it like any other instant subcommand.
    if _api._is_persistent_server_invocation(raw):
        return _api._dispatch(raw)

    if output_path is None:
        with _api.run_backstop():
            return _api._dispatch(raw)

    # `-o FILE`: tee stdout (so the review STILL prints live) and persist the captured
    # text via Python open()/write — which sidesteps zsh `noclobber` (the failure mode
    # this flag fixes). The file is written even on a non-zero exit or empty result (a
    # caller that asked for a file gets one) — but NOT when the dispatch exits EARLY via
    # SystemExit. An argparse usage error or `--help` raises SystemExit before any review
    # ran; writing then would TRUNCATE a pre-existing `-o` target to empty/help-text — a
    # silent data-loss footgun (e.g. `review --bad-flag -o important.md`). So a SystemExit
    # propagates with NO write; the file is touched only when `_dispatch` actually
    # returned (the review path ran).
    captured = _api.io.StringIO()
    real_stdout = _api.sys.stdout
    rc = 1
    completed = False
    try:
        with _api.contextlib.redirect_stdout(_api._Tee(real_stdout, captured)):
            with _api.run_backstop():
                rc = _api._dispatch(raw)
                completed = True
    finally:
        # Only persist when the dispatch RAN to a return (completed). On a SystemExit
        # (argparse/--help) or any other propagating exception, skip the write so a
        # pre-existing target is never truncated by an early exit. The write outcome is
        # recorded but NOT returned from `finally` (a `return` there would swallow a
        # propagating exception); the final return below applies it only on a clean run.
        write_error: OSError | None = None
        if completed:
            try:
                _api._write_output_file(output_path, captured.getvalue())
            except OSError as exc:
                write_error = exc
                print(
                    f"[review-cli] -o: could not write {output_path}: {exc}",
                    file=_api.sys.stderr,
                    flush=True,
                )
    return 1 if write_error is not None else rc


def _extract_detach_flag(_api, /, argv: list[str]) -> tuple[bool, list[str]]:
    """Pull a bare `--detach` flag out of argv before dispatch (review-cli#160
    companion feature), mirroring `_extract_output_path`'s pre-scan so `--detach` works
    uniformly ahead of every mode's own parser. Boolean, no value: unlike `-o` there is
    no glued/`=`-value form to recognize. Scanning stops at the first `--`
    (end-of-options) and skips the value of any preceding value-taking option, so
    `--prompt --detach` (the flag as literal prompt TEXT) is never misread as the
    switch."""
    out = False
    rest: list[str] = []
    i = 0
    value_for_previous = False
    value_taking_opts = (
        _api._VALUE_TAKING_OPTS - {"--check"}
        if argv and argv[0] == "task"
        else _api._VALUE_TAKING_OPTS
    )
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        if value_for_previous:
            rest.append(tok)
            value_for_previous = False
            i += 1
            continue
        if tok == "--detach":
            out = True
            i += 1
            continue
        rest.append(tok)
        if tok in value_taking_opts:
            value_for_previous = True
        i += 1
    return out, rest


def _reviewlib_repo_root(_api, /) -> str:
    """The directory CONTAINING the `reviewlib` package this process is actually
    running (its parent), suitable for `$PYTHONPATH`. `python -m reviewlib` only finds
    the package if it is importable — true for a `pip install`, but NOT guaranteed for
    the symlink-install path (`bin/review`'s shim inserts the repo root into ITS OWN
    `sys.path`, which a spawned `python -m reviewlib` subprocess does not inherit). Every
    install shape reaches this same line already having imported `reviewlib`
    successfully (we ARE `reviewlib.cli`), so its resolved `__file__` always answers
    correctly regardless of how THIS process found the package."""
    import reviewlib

    return str(_api.Path(reviewlib.__file__).resolve().parent.parent)


def _first_verb(_api, /, raw: list[str]) -> str | None:
    """The first non-option token of `raw`, skipping the VALUE of any value-taking
    option (`-m claude:x dashboard …` -> "dashboard", not "claude:x") — the same
    value-skipping walk `_extract_detach_flag` does."""
    skip_value = False
    for tok in raw:
        if tok == "--":
            return None
        if skip_value:
            skip_value = False
            continue
        if tok.startswith("-"):
            skip_value = tok in _api._VALUE_TAKING_OPTS
            continue
        return tok
    return None


def _reject_undetachable(_api, /, raw: list[str]) -> int | None:
    """The whitelist gate for `--detach` (review round 1, GH-162, Fable #2): only a
    REVIEW MODE verb (`known_subcommands()` — diff/brainstorm/just-ask/quorum/qa/
    visual/…) may be detached. Runs on the already-NORMALIZED argv, so a leading
    global option (`review -m x dashboard run --detach`) can no longer slip a
    server subcommand past the check with `raw[0] == "-m"`. Persistent SERVER
    subcommands (`dashboard`, `spec-web`) keep their own message: ANY of their
    subactions is rejected (not just the blocking foreground server) rather than
    double-daemonized, since they already have a start/stop lifecycle. Management
    verbs (`jobs`/`status`/`wait`/`install-*`/…) are rejected too — detaching them is
    meaningless and would only pollute the jobs list. Returns the exit code to
    return, or None when `raw` is detachable."""
    verb = _api._first_verb(raw)
    if verb in _api._SERVER_SUBCOMMANDS:
        print(
            "[review-cli] --detach is not supported for dashboard/spec-web — they "
            "already have their own start/stop lifecycle (`review dashboard start`).",
            file=_api.sys.stderr,
        )
        return 2
    # ONE verb for both checks (review round 2, GH-162, Opus #2 / Sonnet #1): the
    # whitelist used to index `raw[0]` directly, so it only agreed with the server
    # check above when normalization had already moved every leading option behind
    # the verb — a leading value-taking option normalization does not know would have
    # been rejected as "got '-o'" even though the mode itself is detachable.
    if verb is None or verb not in _api.known_subcommands():
        modes = ", ".join(sorted(_api.known_subcommands()))
        print(
            f"[review-cli] --detach requires a review mode as the first argument "
            f"({modes}); got {verb if verb is not None else 'nothing'!r}. Management commands "
            "(jobs/status/wait/install-*) run synchronously and cannot be detached.",
            file=_api.sys.stderr,
        )
        return 2
    return None


def _detached_child_argv(_api, /, raw: list[str], result_path: Path | None) -> list[str]:
    """`python -m reviewlib <raw>`, with a generated `-o <result_path>` (None when the
    caller passed its own `-o`) placed BEFORE a `--` end-of-options marker — appending
    after `--` would make `-o`/its value land as extra POSITIONALS, corrupting any
    invocation with its own `--` (`review just-ask --task X --detach -- --flag-like
    -question`, codex review). Built structurally from the split, not via index
    arithmetic across the two lists (Sonnet review, round 1)."""
    if result_path is None:
        return [_api.sys.executable, "-m", "reviewlib", *raw]
    idx = raw.index("--") if "--" in raw else len(raw)
    head, tail = raw[:idx], raw[idx:]
    return [_api.sys.executable, "-m", "reviewlib", *head, "-o", str(result_path), *tail]


def _detached_child_env(_api, /, job_id: str, this_jobs_dir: Path) -> dict[str, str]:
    """The child's environment: `$REVIEW_JOB_ID` (the marker `main()`'s finalizer keys
    on), `$REVIEW_JOBS_DIR` pinned to the SAME resolved jobs dir this process used —
    without this a child that later calls `jobs.jobs_dir()` itself could independently
    recompute the last-resort `mkdtemp()` fallback and land its bookkeeping in a
    DIFFERENT directory than the parent (codex review, review-cli#162 follow-up; a
    no-op when the standard location is writable) — and `$PYTHONPATH` prefixed with
    THIS process's own resolved repo root (`_reviewlib_repo_root`) so a symlink-only
    install still finds the package."""
    env = dict(_api.os.environ)
    env["REVIEW_JOB_ID"] = job_id
    env["REVIEW_JOBS_DIR"] = str(this_jobs_dir)
    repo_root = _api._reviewlib_repo_root()
    existing_pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{_api.os.pathsep}{existing_pp}" if existing_pp else repo_root
    )
    return env


def _detached_child_stdin(_api, /) -> int:
    """What the detached child gets as stdin: the parent's OWN stdin fd passed straight
    through when it is a pipe/file, `DEVNULL` when it is a tty (or absent).

    Review round 1 (GH-162, Fable #1 / Opus #2): the previous design READ stdin to EOF
    in the parent and spooled it to a file for the child. That blocked the parent —
    the very process `--detach` promises returns at once — whenever a caller's stdin
    was a non-tty pipe held OPEN with nothing in it (agent tool wrappers, pre-commit
    hooks: the feature's own audience). The synchronous path reads stdin for every
    verb without an explicit `--staged`/`--diff` source (bounded by
    `$REVIEW_STDIN_WAIT_SECONDS`, see `_read_stdin_if_piped` / `_dispatch`), so the
    only way to keep the child's input IDENTICAL to a
    synchronous run without the parent ever reading is to hand it the same fd: the
    pipe's writer stays open until the CHILD reads to EOF, the parent never touches
    it. A tty stdin is never inherited — a session-detached background process
    reading a tty would stop on SIGTTIN; the synchronous path never reads a tty
    either (`_read_stdin_if_piped`), so `DEVNULL` gives the child the same "no piped
    input" answer."""
    stdin = _api.sys.stdin
    if stdin is None or getattr(stdin, "closed", True) or stdin.isatty():
        return _api.subprocess.DEVNULL
    try:
        return stdin.fileno()
    except (OSError, ValueError):
        return _api.subprocess.DEVNULL


def _spawn_detached_job(_api, /, raw: list[str]) -> int:
    """Implement `--detach`: spawn a SECOND, full `review` invocation (the same argv,
    minus `--detach`) as a session-detached background process, record it as a job, and
    return almost immediately — the caller never blocks for the review itself.

    Reuses `python -m reviewlib` (rather than resolving a `review` on PATH, which can be
    a live-symlink into a DIFFERENT checkout — see `dashboard/service.py
    _review_argv0`) so the detached child is guaranteed to run the exact code this
    process is running (see `_detached_child_env` for how it finds the package). The
    child is started in its OWN session (`start_new_session=True`) — same isolation
    `_run_streamed` gives backend children — so it outlives this process's exit and is
    bounded the same way (its own `install_signal_reaper` + `run_backstop`, wired the
    moment its `main()` runs). Its stdin is the caller's own (`_detached_child_stdin`),
    so `git diff | review diff --detach` reviews the piped diff exactly like a
    synchronous run — read by the child, never by this process.

    A caller's own `-o FILE` is honored as the job's result file; otherwise a default
    result path under `jobs.jobs_dir()` is used and passed to the child so the review's
    normal `-o` machinery (including the quorum stamp) writes there. The job record is
    written TWICE and never touches `status` after the first write: (1) BEFORE `Popen`,
    with everything except `pid` — this happens-before the child can possibly exist, so
    the child's OWN terminal-status write (in `main`'s finally) can never race a "does
    this job exist yet" gap; (2) immediately after `Popen`, adding ONLY `pid` — no
    `status` key — so even if the child finishes and writes its terminal status BETWEEN
    these two writes, this second write cannot stomp it back to "running" (`jobs.
    write_job`'s read-modify-write would otherwise silently regress a `done`/`failed`
    record for a review fast enough to race its own parent).
    """
    raw = _api._normalize_leading_mode_options(raw)
    rejected = _api._reject_undetachable(raw)
    if rejected is not None:
        return rejected

    job_id = _api.jobs.new_job_id()
    # Resolved ONCE and reused for every path below (and propagated to the child via
    # $REVIEW_JOBS_DIR) — `jobs_dir()`'s own last-resort fallback tier mints a brand-new
    # `tempfile.mkdtemp()` directory the FIRST time it's hit in this process, so calling
    # it repeatedly without pinning the result could scatter this one job's
    # result/log paths across different directories (codex review, review-cli#162
    # follow-up).
    this_jobs_dir = _api.jobs.jobs_dir()
    user_output, _ = _api._extract_output_path(list(raw))
    result_path = (
        user_output
        if user_output is not None
        else this_jobs_dir / f"{job_id}.result.txt"
    )
    log_path = this_jobs_dir / f"{job_id}.log"
    child_argv = _api._detached_child_argv(raw, None if user_output else result_path)

    # Write the initial record BEFORE Popen (see docstring): the child cannot possibly
    # exist yet, so its own terminal-status write can never race a job that doesn't
    # exist on disk yet.
    _api.jobs.write_job(
        job_id,
        status="running",
        mode=raw[0],
        argv=raw,
        cwd=_api.os.getcwd(),
        log_path=str(log_path),
        result_path=str(result_path),
        started_at=_api.time.time(),
    )

    # The log open goes through the SAME review-cli#162 sandboxed-write fallback
    # `_run_streamed`'s own log open uses — a `--detach` job's bookkeeping file is
    # exactly as likely to hit a sandboxed caller's write deny-list as the review's own
    # transcript log, and a job whose OWN log can't be opened must not silently fail to
    # even start.
    log_fd, log_path = _api._open_log_with_fallback(log_path)
    try:
        proc = _api.subprocess.Popen(
            child_argv,
            stdin=_api._detached_child_stdin(),
            stdout=log_fd,
            stderr=_api.subprocess.STDOUT,
            env=_api._detached_child_env(job_id, this_jobs_dir),
            start_new_session=True,
            **_api.portable.new_process_group_kwargs(),  # Windows: CREATE_NEW_PROCESS_GROUP
        )
    finally:
        _api.os.close(log_fd)

    # pid + resolved log_path update: deliberately omits `status` so it can never
    # regress a terminal status the child may have already written (see docstring).
    # `log_path` is re-sent here because `_open_log_with_fallback` above may have
    # replaced it with a fallback path AFTER the initial record (written before the
    # open was even attempted) already recorded the ORIGINAL, possibly-inaccessible
    # one — without this, `review status`/its JSON output/log-tail all pointed at a
    # path the sandboxed caller was never able to write to, even though the process
    # was logging successfully elsewhere (codex review, review-cli#162 follow-up).
    _api.jobs.write_job(job_id, pid=proc.pid, log_path=str(log_path))

    print(f"[review-cli] detached job {job_id} started (pid {proc.pid})")
    print(f"[review-cli]   log:    {log_path}")
    print(f"[review-cli]   result: {result_path}")
    print(f"[review-cli]   check:  review status {job_id}")
    return 0


def _main_dispatch(_api, /, raw: list[str]) -> int:
    """`_main_dispatch_unguarded` with a config.yaml that exists but cannot be loaded
    (PyYAML missing, a syntax error) turned into one stderr message and exit 2
    (review-cli#705): never a traceback, never a silent fall back to the defaults."""
    try:
        return _main_dispatch_unguarded(_api, raw)
    except ConfigError as exc:
        print(f"[review] {exc}", file=_api.sys.stderr, flush=True)
        return 2


def _main_dispatch_unguarded(_api, /, raw: list[str]) -> int:
    """The body of a normal (non-`--detach`) `main()` call: `-o` handling, the
    reentrancy marker, the backstop, and dispatch. Split out of `main()` so a
    detached child (see `_spawn_detached_job`) can run the identical path while
    `main()` wraps it with job-status finalization."""
    output_path, raw = _api._extract_output_path(list(raw))
    raw = _api._normalize_leading_mode_options(raw)

    # A REMOVED flag (--mcp/--ln, or a removed mode flag) OR the removed `review review`
    # SUBCOMMAND verb is a USAGE error — it must behave like argparse's own usage errors
    # w.r.t. `-o`: print the structured error and exit WITHOUT writing the `-o` file.
    # Rejecting it INSIDE `_dispatch` only `return`s 2, which the tee path below treats as
    # "the dispatch completed" and would persist the (empty) captured stdout — truncating a
    # pre-existing `-o` target (codex P1/P2). Reject it here, before the tee is armed, so no
    # write happens. Both are pure argv pre-scans; the later calls in `_dispatch` are then
    # harmless no-ops.
    # _reject_if_reentrant runs FIRST: it is the cheapest check (one os.environ.get)
    # and the highest-severity failure (review-cli#180 review finding, glm-5.2) — a
    # recursive invocation that happens to also carry a removed flag/verb should get
    # the reentrancy diagnostic, not a confusing unrelated usage error.
    for _reject in (
        _api._reject_if_reentrant,
        _api._reject_removed_flags,
        _api._reject_removed_subcommand,
        _api._reject_subcommand_only_flag_without_verb,
    ):
        rejected = _reject(raw)
        if rejected is not None:
            return rejected

    _api.os.environ[_api.REVIEW_CLI_ACTIVE_ENV] = "1"
    try:
        return _api._dispatch_with_backstop(raw, output_path)
    finally:
        _api.os.environ.pop(_api.REVIEW_CLI_ACTIVE_ENV, None)
