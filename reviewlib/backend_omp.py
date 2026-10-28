"""Backend omp implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _omp_model_hosts_claude(_api, /, omp_model: str | None) -> bool:
    """True when the `omp:<selector>` model string names an Anthropic/Claude model
    (review-cli#168's omp-engine account axis) -- e.g. `anthropic/claude-sonnet-5-5` or
    any selector containing "claude"."""
    if not omp_model:
        return False
    lowered = omp_model.lower()
    return "claude" in lowered or lowered.startswith("anthropic/")


def _review_omp_claude_profiles(_api, /) -> list[str | None]:
    """Ordered omp `--profile` names to try when the hosted model is Claude/Anthropic
    (review-cli#168's omp-engine account axis). `$REVIEW_OMP_CLAUDE_PROFILES` is a
    comma-separated list of already-logged-in omp profile names (each populated by a
    one-time interactive `omp --profile <name>` OAuth login -- review-cli cannot perform
    that itself). Unset/empty -> `[None]`, today's single-default-profile behaviour,
    unchanged."""
    raw = _api.os.environ.get("REVIEW_OMP_CLAUDE_PROFILES", "").strip()
    if not raw:
        return [None]
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or [None]


def _omp_model_hosts_openai_codex(_api, /, omp_model: str | None) -> bool:
    """True when the `omp:<selector>` model string names an openai-codex model
    (review-cli#168 omp-engine account axis) -- e.g. `openai-codex/gpt-6-luna`."""
    if not omp_model:
        return False
    return omp_model.lower().startswith("openai-codex/")


def _review_omp_openai_codex_credentials(_api, /) -> list[int | None]:
    """Ordered openai-codex credential ids from omp's auth db for this process.

    Reads `auth_credentials` rows for provider `openai-codex` with no
    `disabled_cause`, ordered by `id` (same ordering `omp token --list` uses).
    Probe failure or zero rows -> `[None]` (single implicit dispatch, unchanged).
    """
    db = _api._omp_auth_db()
    try:
        import sqlite3  # noqa: PLC0415
        from urllib.parse import quote  # noqa: PLC0415

        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = conn.execute(
                "SELECT id FROM auth_credentials "
                "WHERE provider = 'openai-codex' AND disabled_cause IS NULL "
                "ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable db => implicit single identity
        return [None]
    ids = [
        row[0]
        for row in rows
        if isinstance(row[0], int) and not isinstance(row[0], bool)
    ]
    return ids if ids else [None]


def _review_omp_with_codex_credential_fallback(
    _api, /, model, omp_model, prompt, diff, cwd, timeout, round_no, effort
) -> _shared.ReviewResult:
    """Try each openai-codex credential in order until one produces a non-chronic
    result, or return the last (worst) result when every credential is exhausted.

    Pins each attempt via `OMP_CREDENTIAL_ID` in the seat env when omp supports
    it (forward-compatible); a single stored credential keeps today's behaviour
    (no pin env, one dispatch)."""
    candidates = _api._review_omp_openai_codex_credentials()
    if len(candidates) <= 1:
        # Single (or zero) stored credential: identical to pre-fallback dispatch —
        # do not pin via OMP_CREDENTIAL_ID so omp's native selector stays unchanged.
        return _api._review_omp_once(
            model,
            omp_model,
            prompt,
            diff,
            cwd,
            timeout,
            round_no,
            effort,
            profile=None,
        )
    last_result = None
    for credential_id in candidates:
        if not isinstance(credential_id, int):
            continue
        result = _api._review_omp_once(
            model,
            omp_model,
            prompt,
            diff,
            cwd,
            timeout,
            round_no,
            effort,
            profile=None,
            credential_id=credential_id,
        )
        if _api._chronic_unavailable_reason(result) is None:
            return result
        last_result = result
    return last_result


def _review_omp_with_account_fallback(
    _api, /, model, omp_model, prompt, diff, cwd, timeout, round_no, effort
) -> _shared.ReviewResult:
    """Try each configured omp Claude profile in order until one produces a
    non-chronic result, or return the last (worst) result when every profile is
    exhausted -- the omp-hosted mirror of `_review_claude_cli_with_account_fallback`."""
    candidates = _api._review_omp_claude_profiles()
    last_result = None
    for profile in candidates:
        result = _api._review_omp_once(
            model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile
        )
        if not _api._account_should_advance(result):
            return result
        last_result = result
    return last_result


def _omp_selector_is_anthropic(_api, /, omp_model: str | None) -> bool:
    """True when omp would serve `omp_model` from its `anthropic` provider: an
    `anthropic/<model>` selector, or an unqualified Claude selector. A Claude model under
    another provider (`some-proxy/claude-x`) is not served by the Anthropic accounts."""
    if not omp_model or not _api._omp_model_hosts_claude(omp_model):
        return False
    head, sep, _rest = omp_model.partition("/")
    return not sep or head.lower() == _api.omp_accounts.PROVIDER


def _omp_account_cooldown_key(_api, /, account: _shared.omp_accounts.OmpAccount) -> str:
    """The cooldown-store key a limited omp Anthropic account is recorded under."""
    return f"omp-account:{_api.omp_accounts.PROVIDER}:{account.credential_id}"


def _omp_accounts_limited_until(
    _api,
    /,
    accounts: list[_shared.omp_accounts.OmpAccount],
) -> dict[int, float]:
    """credential id -> epoch seconds until which an earlier call saw that account
    limited (the cooldown store, so it holds across review-cli processes)."""
    out: dict[int, float] = {}
    for account in accounts:
        entry = _api.active_cooldown(
            _api._omp_account_cooldown_key(account),
            access_method=_api._OMP_ACCESS_METHOD,
        )
        if entry is not None:
            out[account.credential_id] = float(entry["until"])
    return out


def _omp_account_failure(
    _api, /, result: _shared.ReviewResult
) -> tuple[str, str | None] | None:
    """Why `result` means the account that ran it cannot answer now, as (the failure
    line, its typed cooldown cause), else None -- the next account is then tried. A
    rate/usage limit or a chronic quota notice is `quota`, an administrative
    "unavailable" sentinel is `unavailable`, a failed login is `auth`."""
    limit = _api.omp_accounts.limit_failure(
        result.returncode, result.stdout, result.stderr
    )
    if limit is not None:
        return limit, _api.CAUSE_QUOTA
    if _api._account_should_advance(result):
        chronic = _api._chronic_unavailable_reason(result)
        if chronic is not None:
            return chronic, _api._chronic_cause(chronic)
        login = _api._login_failure_line(result)
        if login is not None:
            return login, _api.CAUSE_AUTH
    return None


def _omp_pool_refusal(
    _api, /, model: str, round_no: int, reason: str
) -> _shared.ReviewResult:
    """The seat's result when the account policy leaves no omp Anthropic account it may
    take: no model is called, and the reason names every account and why."""
    command = f"omp {_api.omp_accounts.PROVIDER} account pool"
    stderr = f"[review-cli] {reason}"
    _api._emit_rest_log(
        "omp", command, round_no=round_no, returncode=1, stdout="", stderr=stderr
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=1,
        stdout="",
        stderr=stderr,
        account=f"none answered -- {reason}",
    )


def _review_omp_with_anthropic_pool(
    _api, /, model, omp_model, prompt, diff, cwd, timeout, round_no, effort
) -> _shared.ReviewResult:
    """Run an Anthropic-hosted omp seat on omp's own Anthropic accounts, one account per
    attempt (review-cli#637, see reviewlib.omp_accounts): accounts omp has not blocked and
    review-cli has not seen limited go first; a limit, quota or login failure moves on to
    the next account. The account policy (review-cli#641, config.yaml `omp_accounts:`)
    removes excluded accounts and accounts at or over their usage threshold before any
    token is fetched. The result's `account` names the account that answered, any that
    failed before it and any the policy skipped. No account in agent.db -> one unpinned
    run, omp picks."""
    policy = _api._omp_account_policy()
    db = _api._omp_auth_db()
    accounts = None if policy.problems else _api.omp_accounts.anthropic_accounts(db)
    # usage is read once per dispatch, and only when a non-excluded account could be
    # over a threshold
    usage = _pool_usage_snapshot(_api, accounts, policy)
    refusal, eligible, skipped = _api.omp_accounts.pool_refusal(
        policy, accounts, db, usage, omp_model, int(_api.time.time() * 1000)
    )
    if refusal is not None:
        return _api._omp_pool_refusal(model, round_no, refusal)
    if not accounts:
        return _api._review_omp_once(
            model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile=None
        )
    launcher = _api._omp_launcher()
    total = len(accounts)
    ordered = _api.omp_accounts.dispatch_order(
        eligible, omp_model, _api._omp_accounts_limited_until(eligible)
    )
    skipped_note = f"; skipped: {'; '.join(skipped)}" if skipped else ""
    failed: list[str] = []
    last_result: _api.ReviewResult | None = None
    for account in ordered:
        served = (
            f"{account.label} (omp {_api.omp_accounts.PROVIDER} account "
            f"{account.position} of {total})"
        )
        token, error = _api.omp_accounts.access_token(launcher, account)
        if token is None:
            failed.append(f"{account.label}: {error}")
            last_result = _api.ReviewResult(
                model=model,
                command=f"omp token {_api.omp_accounts.PROVIDER} --account {account.position}",
                returncode=1,
                stdout="",
                stderr=f"[review-cli] omp account {served}: {error}",
            )
            continue
        result = _api._review_omp_once(
            model,
            omp_model,
            prompt,
            diff,
            cwd,
            timeout,
            round_no,
            effort,
            profile=None,
            anthropic_account=account,
            oauth_token=token,
            launcher=launcher,
        )
        outcome = _api._omp_account_failure(result)
        key = _api._omp_account_cooldown_key(account)
        if outcome is None:
            if result.returncode == 0:
                _api.clear_cooldown(key, access_method=_api._OMP_ACCESS_METHOD)
            note = served + (
                f"; failed before it: {'; '.join(failed)}" if failed else ""
            )
            return _api.dataclasses.replace(result, account=note + skipped_note)
        failure, cause = outcome
        _api.record_cooldown(
            key,
            failure,
            access_method=_api._OMP_ACCESS_METHOD,
            ttl_seconds=_api.omp_accounts.retry_after_seconds(
                result.stdout, result.stderr
            ),
            cause=cause,
        )
        failed.append(f"{account.label}: {failure}")
        last_result = result
    assert (
        last_result is not None
    )  # `ordered` is every eligible account, and there is at least one
    # counts the accounts tried; the ones the policy skipped are listed after them
    summary = (
        f"every omp {_api.omp_accounts.PROVIDER} account failed ({len(ordered)}): "
        f"{'; '.join(failed)}{skipped_note}"
    )
    return _api.dataclasses.replace(
        last_result,
        stderr=f"{last_result.stderr.rstrip()}\n[review-cli] {summary}".lstrip(),
        account=f"none answered -- {summary}",
    )


def review_omp(
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
    omp_model = model.split(":", 1)[1] if ":" in model else None
    command = (
        f"omp -p --no-session --tools {_api._OMP_READONLY_TOOLS}"
        + (f" --model {omp_model}" if omp_model else "")
        + " @<payloadfile>"
    )
    unpaid = _api.unpaid_provider_result(
        model, backend="omp", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    # review-cli#235: mirrors review_opencode's own cooldown-consult pattern (checked
    # BEFORE resolve_backend_mode/_which/preflight) so a cooling-down omp seat skips ALL
    # real work, not just the final dispatch. omp model ids (e.g. "omp:kimi-code/k3")
    # are already the canonical cooldown-store key -- no alias-normalization step is
    # needed here the way review_opencode's oc:/opencode: spelling pair requires.
    cooldown = _api._cooldown_after_recovery_probe(
        model, access_method=_api._OMP_ACCESS_METHOD
    )
    if cooldown is not None:
        return _api._cooldown_skip_result(model, round_no, cooldown, backend="omp")
    # CLI-only: a forced REVIEW_OMP_MODE=api is a config error — surface it as a
    # dead-backend result (mirrors review_zai's forced-mode path), never silently run.
    try:
        _api.resolve_backend_mode("omp", _api.OMP_SUPPORTED_MODES, "cli")
    except RuntimeError as exc:
        _api._emit_rest_log(
            "omp", command, round_no=round_no, returncode=1, stdout="", stderr=str(exc)
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    # review-cli#637: an omp seat on omp's `anthropic` provider runs on omp's Anthropic
    # accounts one at a time (`_review_omp_with_anthropic_pool`). review-cli#168's
    # explicit `$REVIEW_OMP_CLAUDE_PROFILES` (separately logged-in omp profiles) still
    # wins when set, unless an account policy (review-cli#641) is set: a profile's
    # account is unknown, so it could be an excluded one. Every other omp-hosted model
    # (Kimi, MiMo, ...) dispatches once.
    if _api._omp_model_hosts_claude(omp_model):
        refusal = _api._omp_profiles_policy_reason(omp_model)
        if refusal is not None:
            return _api._omp_pool_refusal(model, round_no, refusal)
        if _api._review_omp_claude_profiles() == [
            None
        ] and _api._omp_selector_is_anthropic(omp_model):
            return _api._review_omp_with_anthropic_pool(
                model, omp_model, prompt, diff, cwd, timeout, round_no, effort
            )
        return _api._review_omp_with_account_fallback(
            model, omp_model, prompt, diff, cwd, timeout, round_no, effort
        )
    if _api._omp_model_hosts_openai_codex(omp_model):
        return _api._review_omp_with_codex_credential_fallback(
            model, omp_model, prompt, diff, cwd, timeout, round_no, effort
        )
    return _api._review_omp_once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile=None
    )


def _remove_sandbox_best_effort(_api, /, path: _shared.Path) -> None:
    """Delete `path`, retrying while a process outside the seat still writes into it;
    warn on stderr (never raise) when it cannot be fully removed."""
    reason = ""
    for attempt in range(_api._SANDBOX_REMOVE_ATTEMPTS):
        if attempt:
            _api.time.sleep(_api._SANDBOX_REMOVE_RETRY_SECONDS)
        try:
            _api.shutil.rmtree(path)
        except OSError as exc:
            reason = str(exc)
        else:
            reason = "recreated after removal"
        if not _api.os.path.lexists(path):
            return
    print(
        f"[review-cli] warning: could not remove seat sandbox {path} ({reason}); left "
        "for the OS temp cleaner, the seat's answer is kept",
        file=_api.sys.stderr,
        flush=True,
    )


def _review_omp_once(
    _api,
    /,
    model: str,
    omp_model: str | None,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    effort: str | None,
    profile: str | None,
    credential_id: int | None,
    anthropic_account: _shared.omp_accounts.OmpAccount | None,
    oauth_token: str | None,
    launcher: _shared.omp_accounts.OmpLauncher | None,
) -> _shared.ReviewResult:
    """The actual omp dispatch (sandbox/cage/argv/env), extracted verbatim from
    `review_omp` (review-cli#168) so the outer function can pick which omp `--profile`
    or openai-codex credential to use before calling in — see `_omp_agent_dir`'s
    `profile` parameter and `_OMP_CREDENTIAL_ID_ENV`. With `oauth_token` (and the
    `anthropic_account` it belongs to, review-cli#637) the run is pinned to that one
    Anthropic account: its agent dir is an empty dir inside the sandbox, so omp has no
    stored credential to pick and uses `ANTHROPIC_OAUTH_TOKEN` (see
    reviewlib.omp_accounts)."""
    launcher = launcher or _api._omp_launcher()
    command = (
        f"omp -p --no-session --tools {_api._OMP_READONLY_TOOLS}"
        + (f" --model {omp_model}" if omp_model else "")
        + " @<payloadfile>"
    )
    # AGENTIC, read-only: omp can READ any project file (mounted via `--add-dir`) — not
    # just the diff embedded in the prompt — while the three-layer cage (see the module
    # comment above) makes mutation, exec, and egress impossible.
    message = (
        f"{prompt}\n\nYou are reviewing a real git repository mounted at `{cwd}` in "
        "READ-ONLY mode: read any project file under that directory with your read "
        "tools, but do not edit, write, run commands, or access the network. "
        + (
            f"Focus on this diff:\n\n```diff\n{diff}\n```"
            # `not diff.isspace()` tests emptiness WITHOUT the full-size copy
            # `.strip()` would allocate just for truthiness.
            if diff and not diff.isspace()
            else "Answer based on the repository and the prompt."
        )
    )
    argv = [
        launcher.argv0,
        "-p",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--tools",
        _api._OMP_READONLY_TOOLS,
        "--add-dir",
        str(cwd),
    ]
    if omp_model:
        argv += ["--model", omp_model]
    if effort:
        # review's effort vocabulary (minimal/low/medium/high/xhigh/max) is a strict
        # subset of omp's `--thinking` levels (those plus off/auto) — pass through.
        argv += ["--thinking", effort]
    # The sandbox dir is the NEUTRAL launch cwd AND the seat's HOME (cage layer 2): a
    # fresh empty temp dir with no repo root above it, so omp's project discovery
    # (`.mcp.json`, `.omp/tools`, `.claude/tools`) finds nothing, and its user-scope MCP
    # discovery (~/.claude.json etc.) sees an empty home. PI_CODING_AGENT_DIR pins the
    # REAL agent dir so provider auth still resolves; OMP_PROFILE is dropped so nothing
    # re-derives profile paths from the fake HOME (its profile-scoped settings are user
    # workflow, not reviewer context, and the auth db is pinned explicitly). NOTE: this
    # layer assumes omp resolves `~` via the HOME env var (not getpwuid) — verified
    # live: with HOME redirected, the seat has no user-scope MCP tools
    # (tests/test_omp_cage_live.py). The dir also holds payload + overlay.
    with _api._seat_sandbox("review-cli-omp-") as box:
        home = box / "home"
        home.mkdir()
        payload = box / "payload.md"
        payload.write_text(message, encoding="utf-8")
        cage = box / "cage.yml"
        cage.write_text(_api._OMP_CAGE_OVERLAY, encoding="utf-8")
        argv += ["--config", str(cage), f"@{payload}"]
        env = {
            k: v
            for k, v in _api.os.environ.items()
            if k
            not in (
                "HOME",
                "XDG_CONFIG_HOME",
                "PI_CODING_AGENT_DIR",
                "OMP_PROFILE",
                "ANTHROPIC_OAUTH_TOKEN",
                _api._OMP_CREDENTIAL_ID_ENV,
            )
        }
        # Sanitized XDG_CONFIG_HOME too — user-scope discovery that keys off XDG
        # instead of HOME must not escape the cage either (review of #174).
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        # review-cli#636: a wrapper launcher finds its real binary through this, not
        # through the sandbox HOME.
        env.update(launcher.seat_env())
        if oauth_token is not None:
            agent_dir = box / "agent"
            agent_dir.mkdir()
            env["PI_CODING_AGENT_DIR"] = str(agent_dir)
            env["ANTHROPIC_OAUTH_TOKEN"] = oauth_token
        else:
            env["PI_CODING_AGENT_DIR"] = str(_api._omp_agent_dir(profile=profile))
        if credential_id is not None:
            env[_api._OMP_CREDENTIAL_ID_ENV] = str(credential_id)
        # review-cli#646: an env-key provider (z.ai) with no credential stored in the
        # agent dir this seat runs on gets the key the availability probe resolved
        # (process env first, then review-cli's .env; a blank primary var loses to a set
        # alias). A stored credential is left to omp.
        _add_omp_provider_key(_api, env, model, profile, oauth_token)
        header = f"omp -m {_api._safe_log_header(omp_model)}" if omp_model else None
        if header and anthropic_account is not None:
            # the account this call ran on, in the per-call log (review-cli#465/#637)
            header += f" account={_api._safe_log_header(anthropic_account.label)}"
        proc = _api._run_streamed(
            argv,
            cwd=box,
            env=env,
            timeout=timeout,
            backend="omp",
            round_no=round_no,
            announce=_api._ANNOUNCE_LOGS,
            header_argv0=header,
            true_silence_timeout=_api.true_silence_timeout_seconds(model),
        )
    _api._record_true_silence_if_needed(
        model, proc, access_method=_api._OMP_ACCESS_METHOD
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _pool_usage_snapshot(_api, accounts, policy):
    if (
        accounts
        and policy.needs_usage
        and any(not policy.is_excluded(account) for account in accounts)
    ):
        return _api.omp_accounts.usage_by_email(_api._omp_launcher())
    return None


def _add_omp_provider_key(_api, env, model, profile, oauth_token):
    provider = _api._omp_provider_from_model(model)
    if oauth_token is not None:
        stored = False
    elif profile is not None:
        live = _api._omp_auth_probe(_api._omp_agent_dir(profile=profile) / "agent.db")
        stored = provider in (live or ())
    else:
        stored = _api._omp_stored_auth(provider)
    key = None if stored else _api.omp_env_key(provider)
    if key is not None:
        env[key[0]] = key[1]
