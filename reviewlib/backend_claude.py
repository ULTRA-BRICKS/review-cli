"""Backend claude implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _have_claude_cli(_api, /) -> bool:
    # A host with the `claude` binary has a working CLI seat, so the dispatcher must not
    # route it to the paid API.
    return bool(_api._which_optional("claude"))


def _claude_api_available_for_model(_api, /, model: str) -> bool:
    cfg = _api._anthropic_api_config()
    if cfg is None:
        return False
    provider = _api._anthropic_gateway_provider(cfg["base"])
    return _api._matched_unpaid_provider(model, provider) is None


def _claude_gateway_provider_from_env(_api, /) -> str | None:
    base = _api._anthropic_base_url_from_env()
    return _api._anthropic_gateway_provider(base) if base is not None else None


def _claude_runtime_gateway_provider(_api, /) -> str | None:
    # The Claude CLI child intentionally inherits ANTHROPIC_* vars, so an
    # Anthropic-compatible gateway can be the runtime provider for both API and CLI paths.
    return _api._claude_gateway_provider_from_env()


def _login_failure_line(_api, /, result: _shared.ReviewResult) -> str | None:
    """The output line saying this account's login failed (`_ACCOUNT_AUTH_FAILURE` on a
    non-zero exit that is not a chronic quota result), else None."""
    if result.returncode == 0 or _api._chronic_unavailable_reason(result) is not None:
        return None
    for line in f"{result.stdout or ''}\n{result.stderr or ''}".splitlines():
        if _api._ACCOUNT_AUTH_FAILURE.search(line):
            return line.strip()
    return None


def _account_should_advance(_api, /, result: _shared.ReviewResult) -> bool:
    """True when the next registered account should be tried after `result`: a chronic
    quota/unavailable result (`_chronic_unavailable_reason`) or a failed login on this
    account (`_login_failure_line`)."""
    if _api._chronic_unavailable_reason(result) is not None:
        return True
    return _api._login_failure_line(result) is not None


def _claude_account_dir(_api, /, config_dir: str | None) -> _shared.Path | None:
    """The account a candidate's CLAUDE_CONFIG_DIR names (None = the single implicit
    account): the key its recorded login failure lives under."""
    return _api.Path(config_dir) if config_dir else None


def _remember_claude_login(
    _api, /, config_dir: str | None, result: _shared.ReviewResult
) -> None:
    """After a real call on account `config_dir`, record its failed login or clear a
    recorded one once the account answers (review-cli#596)."""
    from . import claude_accounts as _accts

    account = _api._claude_account_dir(config_dir)
    failure = _api._login_failure_line(result)
    if failure is not None:
        _accts.record_login_failure(account, failure)
    elif _api._clears_cooldown_history(result):
        _accts.clear_login_failure(account)


def _claude_cli_login_reason(_api, /) -> str | None:
    """Why the Claude CLI transport cannot run right now: every account it rotates
    through has a standing recorded login failure (review-cli#596), with the command
    that logs each back in; None while any account has none. Reads the cooldown store
    and, for a recorded failure, when that account's login was last stored (a
    `security` Keychain attribute read on macOS, cached briefly) -- no `claude`
    process, no model call. Side effect: a failure whose account was logged back in is
    dropped (`claude_accounts.login_failure`). `review status` shows a wider verdict
    (`seat_health._claude_cli_reason` also counts accounts `claude auth status` reports
    logged out), which this probe cannot afford on every availability check."""
    from . import claude_accounts as _accts

    accounts: list[_api.Path | None] = [*_accts.rotation_order()] or [None]
    names_by_reason: dict[str, list[str]] = {}
    for account in accounts:
        failure = _accts.login_failure(account)
        if failure is None:
            return None
        name = account.name if account is not None else "the default account"
        names_by_reason.setdefault(failure["reason"], []).append(name)
    what = "; ".join(
        f"{', '.join(names)}: {reason}" for reason, names in names_by_reason.items()
    )
    commands = ", ".join(_accts.login_command(a) for a in accounts)
    return (
        f"Claude CLI login failed on every account ({what}) -- log back in: {commands}"
    )


def _claude_account_candidates(_api, /) -> list[tuple[str | None, str | None]]:
    """Ordered (CLAUDE_CONFIG_DIR, email) pairs review_claude's CLI transport tries,
    current account first (reviewlib.claude_accounts.rotation_order). A single
    `(None, None)` when no `~/.claude-accounts` registry exists -- the single implicit
    account, IDENTICAL to review-cli's pre-#168 behaviour."""
    from . import claude_accounts as _accts

    order = _accts.rotation_order()
    if not order:
        return [(None, None)]
    return [(str(d), _accts.account_email(d)) for d in order]


def _redacted_claude_account(_api, /, email: str | None, config_dir: str | None) -> str:
    """A registered Claude account named for output and logs without its email
    (review-cli#696)."""
    return _api.account_budget.redact_account(email or config_dir)


def _review_claude_cli_with_account_fallback(
    _api, /, model, prompt, diff, cwd, timeout, round_no, *, effort
) -> _shared.ReviewResult:
    """Try each registered Claude account (CLAUDE_CONFIG_DIR) in rotation order, current
    first, until one produces a result that is neither chronic nor a failed login
    (`_account_should_advance`) or every account is exhausted
    (review-cli#168/#170: the OLD gate blocked every claude:* seat when ANY ONE
    configured account reported 100% usage, even when the account actually in use was
    fine). A host with no `~/.claude-accounts` registry gets exactly one (None, None)
    candidate and behaves byte-identically to before this function existed."""
    from . import claude_accounts as _accts

    candidates = _api._claude_account_candidates()
    # review-cli#596: an account whose login already failed is not paid for again while
    # another account may still answer. When every account's login has failed they are
    # all tried: nothing else can answer this call (with a reachable omp twin the seat
    # never gets here -- `backend_unavailable_reason` already handed it over), and a
    # success is what clears the record.
    fresh, login_failed = _prioritized_claude_accounts(_api, _accts, candidates)
    last_result = None
    skipped = [
        f"{_api._redacted_claude_account(email, config_dir)} (login failed)"
        for config_dir, email in login_failed
    ]
    refusals: list[str] = []
    for config_dir, email in fresh:
        refusal = _api.spawn_refuse_reason(model, account=email)
        if refusal is not None:
            refusals.append(refusal)
            skipped.append(_api._redacted_claude_account(email, config_dir))
            continue
        result = _api.review_claude_cli(
            model,
            prompt,
            diff,
            cwd,
            timeout,
            round_no,
            effort=effort,
            config_dir=config_dir,
        )
        _api._remember_claude_login(config_dir, result)
        if not _api._account_should_advance(result):
            return result
        last_result = result
        skipped.append(_api._redacted_claude_account(email, config_dir))
    if last_result is not None:
        return last_result
    # a budget hold anywhere keeps the result marked as one (no cooldown): _BUDGET_SKIP_SUFFIX
    reason = next(
        (r for r in refusals if _api.account_budget.is_budget_hold(r)),
        refusals[0] if refusals else "harness 5-hour window exhausted (100%)",
    )
    if len(candidates) > 1:
        reason = f"{reason} on all {len(candidates)} configured accounts ({', '.join(skipped)})"
    return _api._usage_limit_skip_result(model, round_no, reason, backend="claude")


def claude_access_method(_api, /) -> str:
    """Which transport `review_claude` dispatches through right now: "api" or "cli".

    REVIEW_CLAUDE_MODE forces it; otherwise the CLI when the claude binary is present, and
    the API only when there is no binary but a key IS configured (see `review_claude`)."""
    mode = _api.os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    use_api = mode == "api" or (
        mode != "cli"
        and not _api._have_claude_cli()
        and _api._anthropic_api_config() is not None
    )
    return "api" if use_api else "cli"


def review_claude(
    _api,
    /,
    model: str,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    effort: str | None,
) -> _shared.ReviewResult:
    """Dispatch the claude/opus backend between the API and CLI variants.

    REVIEW_CLAUDE_MODE forces it: ``api`` (HTTP, no claude binary needed) or ``cli``
    (``claude --print``). With no override the choice is automatic and conservative:
    prefer the CLI when the claude binary is present (subscription, no API cost,
    and reliable now that workspace trust is deterministic), and fall back to the
    API only when there is no claude binary but a key IS configured — i.e. don't
    silently switch a working CLI host to the paid API just because a key happens
    to be in the environment. Set REVIEW_CLAUDE_MODE=api to force the API.

    ``round_no`` is threaded from the panel into BOTH variants so the per-call sidecar
    log lands in the right brainstorm round (the dashboard parser keys on it) — the CLI
    variant via _run_streamed, the API variant via its own _emit_rest_log sidecar.

    Before dispatching, checks ``reviewlib.seat_cooldown`` for a CACHED chronic-
    unavailable verdict from an earlier process (Fable's session-limit/paywall pattern —
    see that module's docstring) and, if cooling down, returns a synthetic sentinel
    WITHOUT spawning the real CLI/API call. After a genuine dispatch, a result matching
    that same chronic shape records a fresh cooldown for the NEXT invocation."""
    unpaid = _api.unpaid_provider_result(
        model,
        backend="claude",
        command=_api._claude_api_command(model),
        round_no=round_no,
        provider=_api._claude_runtime_gateway_provider(),
    )
    if unpaid is not None:
        return unpaid
    # Resolve the transport BEFORE checking the cooldown (review-cli#187): a cooldown
    # recorded from one transport must not shadow the other, independently-healthy one
    # for the same model, so the store is keyed by (model, access_method) and the
    # access_method must be known before the lookup, not after.
    access_method = _api.claude_access_method()
    use_api = access_method == "api"
    cooldown = _api._cooldown_after_recovery_probe(model, access_method=access_method)
    if cooldown is not None:
        return _api._cooldown_skip_result(model, round_no, cooldown, backend="claude")
    # Claude Code 5h/7d cards measure the CLI subscription, not the paid API
    # account. Gating the API transport on that card would suppress a valid
    # Anthropic-compatible call when local Claude Code is exhausted.
    result = (
        _api.review_claude_api(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
        if use_api
        else _api._review_claude_cli_with_account_fallback(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    )
    if result.command.endswith(_api._BUDGET_SKIP_SUFFIX):
        return result
    reason = _api._chronic_unavailable_reason(result)
    if reason is not None:
        _api.record_cooldown(
            model,
            reason,
            access_method=access_method,
            cause=_api._chronic_cause(reason),
        )
    elif _api._clears_cooldown_history(result):
        _api.clear_cooldown(model, access_method=access_method)
    return result


def _claude_project_key(_api, /, cwd: _shared.Path) -> str:
    """The ``~/.claude.json`` ``projects`` key claude uses for ``cwd``: the RESOLVED real
    path (claude canonicalises cwd — e.g. /tmp -> /private/tmp, symlinked worktrees). On
    Windows claude writes it with forward slashes (``C:/Users/...``), so a native
    backslash path would never match its entry."""
    key = _api.os.path.realpath(str(cwd))
    if _api.os.name == "nt":
        key = key.replace("\\", "/")
    return key


def _open_trust_lock(_api, /, cfg: _shared.Path):
    """Open + exclusively lock the sidecar ``.claude.json.review-trust.lock`` that
    serialises review's own read-modify-writes of ``~/.claude.json`` (the data file itself
    is swapped by ``os.replace``, so a lock held on it wouldn't survive the swap). Returns
    the locked file object, or None when the lock is unavailable → the caller proceeds
    unsynchronised, best-effort."""
    lock = None
    try:
        lock = open(cfg.with_name(".claude.json.review-trust.lock"), "w")
        _api.lock_file(lock.fileno())
    except OSError:
        if lock is not None:
            lock.close()
        return None
    return lock


def _release_trust_lock(_api, /, lock) -> None:
    if lock is None:
        return
    try:
        _api.unlock_file(lock.fileno())
    except OSError:
        pass
    finally:
        lock.close()


def _ensure_workspace_trusted(_api, /, cwd: _shared.Path) -> None:
    """Pre-accept Claude Code's workspace trust for ``cwd`` in ~/.claude.json.

    The headless seat must never stop on Claude Code's "Quick safety check / Do you
    trust this folder?" gate in an untrusted directory (a fresh git worktree, a
    not-yet-opened repo). `claude --print` skips that dialog on its own (per
    `claude --help`); seeding the trust flag directly keeps the directory trusted
    regardless of that print-mode behaviour, so the deterministic ``dontAsk`` path
    just works.

    This is a DELIBERATE, persistent edit to the user's global config (review is
    configured to auto-trust the directory it reviews): a later interactive
    claude session in the same dir inherits the trust. It is scoped to the trust
    flag (+ onboarding) only — no permissions or tools are granted.

    Best-effort and conservative: skips the write when already trusted (the
    common case → no write at all), only touches the one project entry, writes
    via a same-dir temp + atomic ``os.replace`` (a concurrent reader never sees a
    half-write), and serialises review's own writers with an flock. Any error
    (missing / locked / non-JSON config) silently degrades to the old behaviour.
    """
    cfg = _api.Path.home() / ".claude.json"
    if not cfg.exists():
        return  # best-effort: never fabricate a config the user didn't have
    # claude keys workspace trust by the RESOLVED real path (it canonicalises
    # cwd — e.g. /tmp -> /private/tmp, symlinked worktrees), so a raw-path key
    # would silently miss and the prompt would still fire.
    key = _api._claude_project_key(cwd)
    # Serialise the read-modify-write against other review processes with an
    # advisory flock on a sidecar lock file (the data file itself is swapped by
    # os.replace, so a lock held on it wouldn't survive the swap). This can't
    # synchronise against `claude` — it doesn't lock — but the skip-when-already-
    # trusted guard means we only ever write on the FIRST review in a fresh dir,
    # keeping that residual window tiny.
    lock = _api._open_trust_lock(cfg)
    try:
        data = _api.json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — leave the shared config untouched
        entry = entry if isinstance(entry, dict) else {}
        entry["hasTrustDialogAccepted"] = True
        # Force (not setdefault): a prior blocked/headless attempt can leave a
        # partial entry with hasCompletedProjectOnboarding=false, which would
        # still let claude block on the onboarding gate. Seed both to true.
        entry["hasCompletedProjectOnboarding"] = True
        projects[key] = entry
        tmp = None
        try:
            fd, tmp = _api.tempfile.mkstemp(
                dir=str(cfg.parent), prefix=".claude.", suffix=".tmp"
            )
            with _api.os.fdopen(fd, "w", encoding="utf-8") as f:
                _api.json.dump(data, f, indent=2)
            _api.os.replace(
                tmp, cfg
            )  # atomic: a concurrent reader never sees a half-write
        except Exception:
            if tmp is not None:
                try:
                    _api.os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return  # malformed / unreadable config → degrade to the old behaviour
    finally:
        _api._release_trust_lock(lock)


def _is_review_seeded_trust_entry(_api, /, entry: object) -> bool:
    """True iff ``entry`` looks like an entry review SEEDED and nothing else enriched: a dict
    whose keys are a subset of the trust/onboarding flags review writes. A larger entry (extra
    keys from a real interactive session) returns False so we never delete a user's real data."""
    return isinstance(entry, dict) and set(entry).issubset(
        _api._REVIEW_SEEDED_TRUST_KEYS
    )


def _remove_workspace_trust(_api, /, cwd: _shared.Path) -> None:
    """Remove a previously-seeded workspace-trust entry for ``cwd`` from ~/.claude.json.

    The inverse of ``_ensure_workspace_trusted``: qa seeds a trust entry for each EPHEMERAL
    throwaway worktree it spawns the claude tester in (a fresh worktree is untrusted and the
    headless gate blocks on it). The worktree is deleted on exit, but its persisted
    ``projects[<realpath>]`` trust entry is NOT — so over many default qa runs ~/.claude.json
    accumulates trusted ``/tmp/review-qa-wt-*`` paths that no longer exist (review-cli#60). Call
    this after the run to drop the one entry, keyed by the SAME realpath the seed used.

    Best-effort and conservative (mirrors the seed): only ever removes the ONE project key, uses
    the same flock + atomic os.replace, and silently degrades on any error. It removes the entry
    ONLY when it still holds just the flags review seeds — if a later real interactive claude
    session enriched it, the entry is LEFT intact so we reap only what we created."""
    cfg = _api.Path.home() / ".claude.json"
    if not cfg.exists():
        return
    key = _api._claude_project_key(cwd)
    lock = _api._open_trust_lock(cfg)
    try:
        data = _api.json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        projects = data.get("projects")
        if not isinstance(projects, dict) or key not in projects:
            return  # nothing seeded for this path — nothing to reap
        if not _api._is_review_seeded_trust_entry(projects.get(key)):
            return  # the entry grew beyond what review seeds — leave it untouched
        del projects[key]
        tmp = None
        try:
            fd, tmp = _api.tempfile.mkstemp(
                dir=str(cfg.parent), prefix=".claude.", suffix=".tmp"
            )
            with _api.os.fdopen(fd, "w", encoding="utf-8") as f:
                _api.json.dump(data, f, indent=2)
            _api.os.replace(tmp, cfg)
        except Exception:
            if tmp is not None:
                try:
                    _api.os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return
    finally:
        _api._release_trust_lock(lock)


def _claude_cli_binary(_api, /) -> str:
    """Resolve the `claude` CLI binary (driven via `claude --print`, see
    `_claude_cli_argv`). Raises a clear "claude not found on PATH" error when absent."""
    return _api._which("claude")


def _claude_cli_argv(
    _api,
    /,
    binary: str,
    model: str | None,
    *,
    image_dir: _shared.Path | None,
    effort: str | None,
) -> list[str]:
    """Build the `claude --print` argv.

    It runs headless and reads the prompt from STDIN (never `-p <payload>` argv: a
    brainstorm round embeds the whole prior transcript and a diff can be huge, which as
    a command-line argument blows past ARG_MAX → execve E2BIG). Read-only is enforced by
    an EMPTY tool allowlist (`--tools ""` = all built-in tools off) unless a visual
    panel job needs scoped image reads, in which case it gets `--tools Read` plus
    `--add-dir <staged-image-dir>`. That keeps normal text review locked down while
    allowing the explicit screenshot attachment path to read only its temp image files.

    cwd is the Popen cwd (claude has no --cwd); there is no --timeout-sec (review-cli's
    own _run_streamed timeout governs the call)."""
    tools = "Read" if image_dir is not None else ""
    system_prompt = (
        _api._CLAUDE_IMAGE_REVIEW_SYSTEM
        if image_dir is not None
        else _api._CLAUDE_REVIEW_SYSTEM
    )
    argv = [
        binary,
        "--print",
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--tools",
        tools,
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--safe-mode",
        "--append-system-prompt",
        system_prompt,
    ]
    if image_dir is not None:
        argv += ["--add-dir", str(image_dir)]
    claude_effort = _api._claude_reasoning_effort(effort)
    if claude_effort and _api._claude_cli_supports_effort(binary):
        argv += ["--effort", claude_effort]
    if model:
        argv += ["--model", model]
    return argv


def _stage_panel_images(
    _api, /, images: tuple[_shared.Path, ...], tmp: _shared.Path
) -> list[_shared.Path]:
    paths: list[_api.Path] = []
    for idx, image in enumerate(images):
        src = _api.Path(image)
        if not src.is_file():
            continue
        suffix = src.suffix if src.suffix else ".png"
        dst = tmp / f"{idx}-panel-image{suffix}"
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            continue
        paths.append(dst)
    return paths


def _prompt_with_panel_images(_api, /, prompt: str, images: list[_shared.Path]) -> str:
    if not images:
        return prompt
    refs = "\n".join(f"- @{path}" for path in images)
    return (
        f"{prompt}\n\n"
        "=== RAW VISUAL ATTACHMENT ===\n"
        "Inspect these screenshot image files directly before answering; do not rely only "
        "on any textual visual summary already in the prompt.\n"
        f"{refs}"
    )


def _claude_cli_env(_api, /) -> dict[str, str]:
    """Child env that keeps the claude CLI from emitting decorative terminal control
    sequences into the captured pipe. `TERM=dumb` + `NO_COLOR=1` disable colour/cursor
    rendering; `CI=1` nudges any progress UI off. Inherits the rest of the environment
    (auth, PATH, ANTHROPIC_* gateway vars) so the subscription / gateway path is intact."""
    env = dict(_api.os.environ)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    return env


def _claude_cli_log_header(_api, /, claude_model: str | None) -> str | None:
    """`claude --model <id>` for the call log, so the dashboard attributes the call to the
    model that ran (review-cli#645: `-m opus48` and the default Opus share the binary)."""
    return (
        f"claude --model {_api._safe_log_header(claude_model)}"
        if claude_model
        else None
    )


def review_claude_cli(
    _api,
    /,
    model: str,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    effort: str | None,
    config_dir: str | None,
) -> _shared.ReviewResult:
    prompt = _api._prompt_with_effort(prompt, effort)
    claude_model = model.split(":", 1)[1] if ":" in model else None
    # Resolve the binary BEFORE touching trust so a missing CLI raises here, not after
    # _ensure_workspace_trusted has already mutated ~/.claude.json.
    binary = _api._claude_cli_binary()
    # Pre-accept workspace trust for cwd: the headless run cannot answer the
    # interactive "Do you trust this folder?" gate (see _ensure_workspace_trusted).
    _api._ensure_workspace_trusted(cwd)
    argv = _api._claude_cli_argv(binary, claude_model, effort=effort)
    # A fresh per-call dict (`_claude_cli_env()` already returns `dict(os.environ)`, a
    # new object every call) -- account rotation (review-cli#168) selects a registered
    # `~/.claude-accounts/account-N` dir via CLAUDE_CONFIG_DIR here. NEVER mutate global
    # `os.environ`: seats dispatch concurrently across threads (reviewlib/process.py's
    # ThreadPoolExecutor), so a global mutation here would be a real, hard-to-reproduce
    # cross-seat race.
    env = _api._claude_cli_env()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    proc = _api._run_streamed(
        argv,
        cwd=cwd,
        input_text=_api._payload(prompt, diff),
        env=env,
        timeout=timeout,
        backend="claude",
        round_no=round_no,
        announce=_api._ANNOUNCE_LOGS,
        true_silence_timeout=_api.true_silence_timeout_seconds(model),
        header_argv0=_api._claude_cli_log_header(claude_model),
    )
    # Belt-and-suspenders: strip any terminal control noise the CLI still leaked into the
    # pipe (a stray escape sequence) so it can never corrupt the parsed
    # `## <model> [ok]/[needs-changes]` verdict (review-cli#76).
    stdout = _api.strip_control_sequences(proc.stdout)
    stderr = _api.strip_control_sequences(proc.stderr)
    # A redacted, human-readable command line for the result header (no prompt/diff).
    command = (
        "claude --print --output-format text --permission-mode dontAsk --tools '' "
        "--strict-mcp-config --disable-slash-commands --safe-mode "
        "--append-system-prompt <read-only-review>  (prompt via stdin)"
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _claude_cli_env_for(_api, /, config_dir: str | None) -> dict[str, str]:
    """A fresh claude CLI env, pinned to the registered account dir when there is one."""
    env = _api._claude_cli_env()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def review_claude_cli_with_images(
    _api,
    /,
    model: str,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    images: tuple[_shared.Path, ...],
    effort: str | None,
    config_dir: str | None,
) -> _shared.ReviewResult:
    prompt = _api._prompt_with_effort(prompt, effort)
    claude_model = model.split(":", 1)[1] if ":" in model else None
    if _api.os.environ.get(_api._mode_env_var("claude"), "").strip().lower() == "api":
        return _api.review_claude(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    binary = _api._which_optional("claude")
    if not binary:
        return _api.review_claude(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    with _api.tempfile.TemporaryDirectory(
        prefix="review-cli-claude-images-"
    ) as tmp_raw:
        tmp = _api.Path(tmp_raw)
        staged = _api._stage_panel_images(images, tmp)
        if not staged:
            return _api.review_claude_cli(
                model, prompt, diff, cwd, timeout, round_no, effort=effort
            )
        _api._ensure_workspace_trusted(tmp)
        argv = _api._claude_cli_argv(binary, claude_model, image_dir=tmp, effort=effort)
        proc = _api._run_streamed(
            argv,
            cwd=tmp,
            input_text=_api._payload(
                _api._prompt_with_panel_images(prompt, staged), diff
            ),
            env=_api._claude_cli_env_for(config_dir),
            timeout=timeout,
            backend="claude",
            round_no=round_no,
            announce=_api._ANNOUNCE_LOGS,
            true_silence_timeout=_api.true_silence_timeout_seconds(model),
            header_argv0=_api._claude_cli_log_header(claude_model),
        )
    # The TemporaryDirectory context deleted tmp; remove the trust entry that
    # _ensure_workspace_trusted seeded so dead /tmp/review-cli-claude-images-* paths
    # don't accumulate in ~/.claude.json. (P2 fix — review-cli#98 thread.)
    _api._remove_workspace_trust(tmp)
    stdout = _api.strip_control_sequences(proc.stdout)
    stderr = _api.strip_control_sequences(proc.stderr)
    command = (
        "claude --print --output-format text --permission-mode dontAsk --tools Read "
        "--strict-mcp-config --disable-slash-commands --safe-mode --add-dir <image-dir> "
        "--append-system-prompt <read-only-review>  (prompt via stdin, image @refs)"
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _prioritized_claude_accounts(_api, accounts_module, candidates):
    fresh = []
    failed = []
    for candidate in candidates:
        failure = accounts_module.login_failure(_api._claude_account_dir(candidate[0]))
        (fresh if failure is None else failed).append(candidate)
    return (fresh, failed) if fresh else (failed, [])
