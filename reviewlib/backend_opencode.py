"""Backend opencode implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _frontmatter_yaml(_api, /, text: str) -> str | None:
    """The YAML between the opening ``---`` and the next CLOSING ``---``, each on its OWN
    line (the markdown frontmatter convention), or ``None`` if there is no such block.

    Splitting on a LINE-anchored delimiter (not a bare ``---`` substring) avoids tearing
    the YAML on a ``---`` that appears INSIDE a value (e.g. ``description: a---b``), which a
    naive ``str.split('---')`` would mis-cut — turning a legit file into "unparseable" and
    forcing a needless rewrite."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _agent_frontmatter(_api, /, text: str) -> dict | None:
    """Parse the YAML frontmatter of an opencode agent markdown file into a dict.

    Returns ``None`` when there is no line-delimited ``---`` frontmatter block or it does
    not parse to a mapping — the caller treats that as "not a trusted read-only definition"
    and rewrites the canonical one. PyYAML is a hard dependency (it parses config.yaml),
    but the import stays lazy/guarded so a broken install degrades to "rewrite" rather
    than a traceback on the hot review path."""
    block = _api._frontmatter_yaml(text)
    if block is None:
        return None
    try:
        import yaml

        data = yaml.safe_load(block)
    except Exception:  # noqa: BLE001 — any parse/import failure => not trusted
        return None
    return data if isinstance(data, dict) else None


def _permission_value_is_fully_denied(_api, /, value: object) -> bool:
    """True if a single permission VALUE grants nothing.

    opencode accepts both the scalar form (``bash: deny``) and a granular per-pattern map
    (``bash: {"*": deny, "git diff": deny}``). A value denies everything iff it is:
      * the string ``deny``; OR
      * a granular map that has an explicit catch-all ``"*": deny`` AND every other leaf is
        also fully denied.
    The catch-all requirement is the SECURITY-CRITICAL bit (verified against opencode's
    permission docs, review-cli#40): opencode's defaults are PERMISSIVE and rules match by
    "last matching wins", so a granular map WITHOUT a ``"*"`` catch-all leaves every
    UNLISTED command at the default ``allow``. A map like ``bash: {"git status": deny}``
    would otherwise look "all-deny" to a naive scan yet permit arbitrary other shell — the
    exact tampered-agent hole this hardening must close. An empty map, a missing/non-deny
    ``"*"``, a string other than ``deny`` (``allow``/``ask``), or any other type is a GRANT."""
    if isinstance(value, str):
        return value == "deny"
    if isinstance(value, dict):
        if not value:
            return False
        # A granular map must explicitly deny the catch-all (unlisted patterns default to
        # allow otherwise) AND deny every other listed pattern.
        if value.get("*") != "deny":
            return False
        return all(_api._permission_value_is_fully_denied(v) for v in value.values())
    return False


def _agent_is_strictly_readonly(_api, /, text: str) -> bool:
    """True only if the agent definition's permission block is at-least-as-strict as the
    canonical deny-all set — i.e. it grants NO write/exec capability.

    Strict on purpose (a stale/tampered global agent is the threat in review-cli#40):
      * the frontmatter must parse to a mapping with a ``permission`` mapping;
      * EVERY key we require (``_READONLY_AGENT_DENIED_PERMISSIONS``) must be present and
        fully denied — a missing key defaults to opencode's permissive behaviour, so a
        partial block is treated as permissive;
      * EVERY key actually present (including extra capabilities we don't enumerate, e.g.
        a future ``network``) must ALSO be fully denied. A single grant on ANY key — known
        or unknown, scalar or nested — means we reject it and rewrite the canonical def.
    "Fully denied" accepts BOTH the scalar ``deny`` and a granular per-action map whose
    every leaf is ``deny`` (so a legitimately hardened granular config is NOT clobbered).
    Extra keys that are themselves fully denied are ACCEPTED (a user who hardened the agent
    with more denies is at least as safe as canonical — we must not downgrade them by
    rewriting). Anything with a grant, a missing required key, or an unparseable frontmatter
    is rejected so the caller rewrites the canonical safe definition.

    NOTE: this enforces "no capability is granted", NOT "exactly the canonical key set".
    The known-safe set is the FLOOR (all must be denied); extra denies only raise it."""
    front = _api._agent_frontmatter(text)
    if front is None:
        return False
    perms = front.get("permission")
    if not isinstance(perms, dict):
        return False
    # Floor: every capability we know must be present and fully denied (a missing one
    # falls back to opencode's permissive default, so a partial block is permissive).
    if any(name not in perms for name in _api._READONLY_AGENT_DENIED_PERMISSIONS):
        return False
    # And nothing present (extras included) may grant anything: every value must be denied.
    return all(
        _api._permission_value_is_fully_denied(value) for value in perms.values()
    )


def _ensure_opencode_readonly_agent(
    _api, /, _project: _shared.Path, _oc_model: str
) -> None:
    """Guarantee the GLOBAL read-only-reviewer opencode agent is the safe deny-all def.

    SECURITY (review-cli#40): a pre-existing global agent file is NOT trusted blindly. A
    stale or tampered ``read-only-reviewer.md`` that ALLOWS bash/edit/write/etc would
    silently give the "read-only" reviewer write/exec capability on the user's repo,
    defeating the guarantee the whole agentic-opencode path leans on. So we VALIDATE an
    existing file and REWRITE the canonical safe definition (loudly) if it is not strictly
    read-only — never run agentically against a permissive agent. Idempotent: a file that
    already matches the canonical deny-all set is left untouched (no spurious writes)."""
    agent = (
        _api.Path.home() / ".config" / "opencode" / "agents" / "read-only-reviewer.md"
    )
    if agent.is_file():
        try:
            existing = agent.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if _api._agent_is_strictly_readonly(existing):
            return  # already the trusted deny-all definition
        print(
            f"[review-cli] opencode: the global read-only-reviewer agent at {agent} is "
            "not strictly read-only (its permissions grant or omit a deny for a write/exec "
            "capability); rewriting it to the canonical deny-all definition for safety.",
            file=_api.sys.stderr,
            flush=True,
        )
    agent.parent.mkdir(parents=True, exist_ok=True)
    _api._atomic_write_text(agent, _api._READONLY_AGENT_MARKDOWN)


def _atomic_write_text(_api, /, path: _shared.Path, text: str) -> None:
    """Write `text` to `path` ATOMICALLY: a concurrent reader sees either the old file or
    the complete new one, never a truncated mid-write file.

    The default board runs SEVERAL agentic opencode seats in parallel, each calling
    `_ensure_opencode_readonly_agent`. A plain `write_text` (truncate-then-write) lets one
    seat's opencode read this agent while another seat is rewriting it — the reader could
    get an empty/partial markdown, lose the frontmatter, and fall back to opencode's
    PERMISSIVE defaults (bash/edit/write). Writing to a temp file in the SAME directory and
    `os.replace`-ing it in is atomic on POSIX, so the read-only guarantee holds under
    concurrency. The temp file is cleaned up on any write failure."""
    fd, tmp_name = _api.tempfile.mkstemp(
        prefix=".read-only-reviewer.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = _api.Path(tmp_name)
    try:
        with _api.os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _api.os.replace(tmp, path)  # atomic on POSIX: reader never sees a partial file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _opencode_has_project_config(_api, /, cwd: _shared.Path) -> bool:
    """True if `cwd` ships its OWN opencode config that could redefine the read-only
    agent / weaken its permissions (see _OPENCODE_PROJECT_CONFIG_NAMES). When True we do
    NOT run opencode agentically there — the sandbox can't be trusted."""
    return any((cwd / name).exists() for name in _api._OPENCODE_PROJECT_CONFIG_NAMES)


def _opencode_runs_in_repo(_api, /, cwd: _shared.Path) -> bool:
    """True iff `cwd` is a git repo we can let opencode read AGENTICALLY AND SAFELY.

    Mirrors review's own cwd resolution: the panel/agentic backends (codex/claude)
    run in the REAL repo read-only and read the whole tree; opencode now does the
    same so the api-only seats routed through it (deepseek/kimi/qwen/glm via `oc:`)
    are AGENTIC too — they can open any project file, not just the diff in the prompt.

    Two gates, both required:
      * it must be a git work tree (a non-repo `cwd` — e.g. `--just-ask` from /tmp —
        has nothing to read, so we fall back rather than roam a scratch dir);
      * it must NOT ship its own opencode project config (.opencode/ or opencode.json
        /jsonc), which could OVERRIDE our read-only agent and re-enable write/bash —
        a real privilege-escalation path on an untrusted repo. Such a repo falls back
        to the isolated temp-dir posture (read-only is enforced by the global agent in
        a clean dir, where no project config can touch it)."""
    if not cwd.is_dir():
        return False
    # This is reached by `--show-board` (a meta flag that must work anywhere) and the
    # opencode-seat scope label, so a missing git binary (OSError -> FileNotFoundError) or a
    # wedged `git rev-parse` (TimeoutExpired) must degrade to "not a repo" (False), never a
    # raw traceback — same defensive catch as cli._is_git_repo.
    try:
        proc = _api._run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return False
    if not (proc.returncode == 0 and proc.stdout.strip() == "true"):
        return False
    if _api._opencode_has_project_config(cwd):
        print(
            f"[review-cli] opencode: {cwd} ships its own opencode config "
            "(.opencode/ or opencode.json) which could override the read-only agent; "
            "running diff-only in an isolated dir for safety.",
            file=_api.sys.stderr,
            flush=True,
        )
        return False
    return True


def _opencode_model_needs_stall_watchdog(_api, /, model: str) -> bool:
    """True iff `model` matches a route known to hang at 0% CPU with zero output on
    quota exhaustion (review-cli#153/#159/#179 — confirmed so far only for zai/glm-5.2
    via opencode). Overridable via $REVIEW_OPENCODE_STALL_MODELS (comma-separated
    case-insensitive substrings) if a future model needs the same treatment."""
    raw = _api.os.environ.get(_api._OPENCODE_STALL_MODELS_ENV)
    substrings = (
        tuple(s.strip() for s in raw.split(",") if s.strip())
        if raw is not None and raw.strip()
        else _api._OPENCODE_DEFAULT_STALL_MODEL_SUBSTRINGS
    )
    model_lower = model.lower()
    return any(s.lower() in model_lower for s in substrings)


def _opencode_stall_seconds(_api, /) -> int | None:
    """Seconds of TOTAL silence (zero output since spawn) before an opencode seat is
    declared stalled, distinct from (and much shorter than) the generic idle timeout.
    None disables the check. Overridable via $REVIEW_OPENCODE_STALL_SECONDS for
    tests/tuning; a value <= 0 disables it, mirroring $REVIEW_IDLE_TIMEOUT_SECONDS's own
    convention."""
    raw = _api.os.environ.get(_api._OPENCODE_STALL_SECONDS_ENV)
    if raw is not None and raw.strip():
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None:
            return value if value > 0 else None
    return _api._OPENCODE_DEFAULT_STALL_SECONDS


def _opencode_call_stalled(
    _api, /, proc: _shared.subprocess.CompletedProcess[str]
) -> bool:
    """True iff `proc` is a `_run_streamed` result that timed out via the STALL
    (liveness) path specifically — not a normal idle timeout, not a real error.

    Reads the `timeout_kind` attribute `_run_streamed` stamps on its result (process.py)
    rather than text-scraping `stdout` for a marker substring (codex review finding: a
    substring search is both forgeable — a reviewed model could itself emit matching
    prose — and, after `_run_streamed`'s own idle/liveness clamping, the marker's exact
    embedded seconds value may not match what this caller requested anyway).

    A stall whose bound was CLAMPED below the requested window (`stall_bound_clamped`,
    stamped by `_run_streamed` when the board-deadline / idle clamp shortened it) is
    NOT a stall in this sense (round-2 review finding, Fable): under deadline pressure
    a merely slow-to-first-byte seat would otherwise rack up three sub-minute "stalls"
    and be benched for the next invocation on a signal that says nothing about quota.
    Such a result is returned as an honest bounded failure — no retry, no cooldown."""
    return (
        proc.returncode == 124
        and getattr(proc, "timeout_kind", None) == _api.TIMEOUT_KIND_STALL
        and not getattr(proc, "stall_bound_clamped", False)
    )


def _run_opencode_with_stall_retry(
    _api,
    /,
    argv: list[str],
    *,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    header_argv0: str,
    model: str,
    true_silence_timeout: int | None,
) -> _shared.subprocess.CompletedProcess[str]:
    """Run an opencode agentic seat, retrying up to `_OPENCODE_MAX_STALL_RETRIES` times
    if it stalls (see the module-level comment above for the full rationale). After the
    final attempt is ALSO a stall, records a cooldown for `(model, "opencode")` so the
    NEXT `review` invocation skips the real dispatch entirely, and returns the last
    (stalled) result to the caller — this run's pool still gets a bounded, honest
    failure instead of hanging.

    Composition with the outer in-seat retry (`reviewlib.retry`, capped at
    `_MAX_TIMEOUT_RETRIES=1` for an rc=124 result): if every attempt here stalls, the
    cooldown is already recorded by the time this returns, so the outer layer's ONE
    permitted retry re-enters `review_opencode`, hits the fresh cooldown at the top of
    that function, and short-circuits to the (fast, rc=0) cooldown-skip sentinel instead
    of spending another `_OPENCODE_MAX_STALL_RETRIES` attempts — the same self-limiting
    composition `review_claude`'s existing cooldown cache already relies on.

    Only applies the stall bound to `model`s matching
    `_opencode_model_needs_stall_watchdog` — every OTHER opencode seat dispatches
    exactly as before this feature (no liveness bound, no retry, no cooldown).

    For a WATCHED model the liveness watchdog is the SOLE owner of the "zero output
    since spawn" signal: `true_silence_timeout` is NOT forwarded to `_run_streamed`
    for it (round-1 review finding, Opus + Fable: both detectors fire on the same
    signal but with opposite policies — liveness retries up to 3x then cools down,
    true-silence cools down on the FIRST trip with no retry — and the registry's
    true-silence value for zai/glm equals the 300s stall default, so which policy
    applied was decided by poll-loop check order, not by design; a user raising
    $REVIEW_OPENCODE_STALL_SECONDS above the registry value would silently have
    flipped the seat to "cooldown on first silence"). Unwatched models keep the
    true-silence wiring exactly as before."""
    stall_seconds = (
        _api._opencode_stall_seconds()
        if _api._opencode_model_needs_stall_watchdog(model)
        else None
    )
    effective_true_silence = None if stall_seconds is not None else true_silence_timeout
    proc = None
    for attempt in range(1, _api._OPENCODE_MAX_STALL_RETRIES + 1):
        proc = _api._run_streamed(
            argv,
            cwd=cwd,
            timeout=timeout,
            backend="opencode",
            round_no=round_no,
            announce=_api._ANNOUNCE_LOGS,
            header_argv0=header_argv0,
            liveness_timeout=stall_seconds,
            true_silence_timeout=effective_true_silence,
        )
        if not _api._opencode_call_stalled(proc):
            return proc
        if attempt >= _api._OPENCODE_MAX_STALL_RETRIES:
            _api.record_cooldown(
                model,
                f"opencode stalled with no output after {attempt} attempts",
                access_method=_api._OPENCODE_ACCESS_METHOD,
                cause=_api.CAUSE_STALL,
            )
    assert proc is not None  # loop runs >= 1 time (_OPENCODE_MAX_STALL_RETRIES >= 1)
    return proc


def _record_true_silence_if_needed(
    _api, /, model: str, proc, *, access_method: str
) -> None:
    """After a `_run_streamed` call (codex review finding, round 13: originally only
    opencode called this — review-cli#235 now also wires `review_omp` through the
    same path; the remaining backends #235 still tracks are not done yet): if it was
    reaped for producing ZERO output (`proc.true_silenced` — see
    process._run_streamed's
    true_silence_timeout), record a cooldown so the NEXT invocation skips this seat
    for a while instead of paying for another multi-minute stuck dispatch (Alex's
    request 2026-08-19/20). A no-op for every other outcome (ordinary success,
    ordinary idle-timeout, or a real error) — those are unrelated to true-silence and
    already handled by their own existing paths. `access_method` (required, no
    default — every caller must name its own cooldown namespace explicitly) keys the
    cooldown-store entry, mirroring `_cooldown_skip_result`'s own no-default
    reasoning: opencode and omp are independent failure domains and must never share
    one cooldown bucket.

    Branches on the explicit `.true_silenced` attribute, NOT `proc.returncode == 125`
    (round-2 review finding, codex + Fable): 125 is a real exit code some CLIs/
    wrappers legitimately use on their own (`timeout(1)`'s "the wrapper itself
    failed", docker run, git-bisect skip) — a child that happens to genuinely exit
    125 by itself, even after producing full real output, must never be misdiagnosed
    as a true-silence reap and wrongly benched. `getattr(..., False)` tolerates a
    plain mock/fake CompletedProcess in tests that doesn't set the attribute at all.

    Deliberately does NOT pass an explicit `ttl_seconds` to `record_cooldown` (codex
    review finding, round 1): an explicit TTL makes `record_cooldown` treat every
    occurrence as `fail_count = 1` (see its docstring), which would silently DISABLE
    the escalating cooldown schedule this repo already ships (review-cli#230) for a
    model that keeps going true-silent repeatedly. Letting the reason string alone
    drive an un-parameterized `record_cooldown` call means it escalates exactly like
    every other cooldown-worthy failure.

    codex review finding, round 3: a genuine success ALSO clears any true-silence
    cooldown history, mirroring `clear_cooldown`'s own documented contract (it names
    this as one of backends.py's two intended call sites) and review_claude's sibling
    pattern above. Without this, a seat that recovers after one true-silence reap,
    then goes true-silent again later (still within the 24h escalation-reset window),
    would wrongly resume at `fail_count=2` (30 minutes) instead of resetting to
    `fail_count=1` (10 minutes) — the same "never actually recovers" shape #221 fixed
    for the ordinary chronic-failure path.

    codex review finding, round 12 (P1): a bare `returncode == 0 and stdout.strip()`
    treated ANY non-empty rc=0 body as recovery — including a chronic-unavailable-
    shaped sentinel body (the same 4 marker phrases `review_claude`'s sibling clear
    already guards against via `_chronic_unavailable_reason`). `_chronic_unavailable_
    reason` is NOT actually claude-specific in implementation — it only inspects the
    generic `ReviewResult`-shaped `.stdout`/`.returncode`/`.stderr`, it is simply not
    wired into any non-claude call site yet — so reusing it here, mirroring
    `review_claude_with_images`'s exact pattern above, closes the false-clear for any
    body matching that SHARED marker vocabulary, using zero new detection logic. This
    narrows, but does not fully close, review-cli#226 (codex review finding, round 17:
    an earlier version of this docstring wrongly claimed the unrecognised case is a
    no-op — it is NOT): an opencode-specific quota wording that matches NONE of the 4
    shared marker phrases falls through past `reason is None` to the `elif` below,
    which DOES clear the cooldown (any non-empty rc=0 body is treated as recovery
    unless it matches a known chronic sentinel) — the exact same behavior
    `review_claude`'s identical sibling pattern has always had for an unrecognised
    claude quota wording, not a new gap this diff introduces. Building genuine
    opencode-specific quota-body detection (so an unrecognised wording is correctly
    classified as chronic rather than defaulting to "success") remains #226's job.

    Opus review finding, round 14 (doc accuracy): `_chronic_unavailable_reason` runs
    on EVERY non-true-silenced outcome, not just the rc=0 sentinel case — a non-zero
    exit whose stderr/short-stdout matches `_CHRONIC_QUOTA_MARKERS` ("session limit" /
    "usage-credits" / "usage credits") ALSO records a cooldown here, mirroring
    `review_claude`'s identical handling of that same signal. The narrower framing
    above ("closes the false-clear ... rc=0 body") describes the round-12 FIX's
    motivating scenario, not the full scope of what this call now covers."""
    if getattr(proc, "true_silenced", False):
        _api.record_cooldown(
            model,
            "true-silence timeout",
            access_method=access_method,
            cause=_api.CAUSE_STALL,
        )
        return
    reason = _api._chronic_unavailable_reason(proc)
    if reason is not None:
        _api.record_cooldown(
            model,
            reason,
            access_method=access_method,
            cause=_api._chronic_cause(reason),
        )
    elif proc.returncode == 0 and proc.stdout.strip():
        _api.clear_cooldown(model, access_method=access_method)


def review_opencode(
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
    prompt = _api._prompt_with_effort(prompt, effort)
    oc_model = model.split(":", 1)[1] if ":" in model else model
    # codex review finding, review-cli#243 round 22: `resolve_backend` routes BOTH the
    # `oc:` and `opencode:` prefixes here (config.py's own `_agentic` docstring calls
    # this pair "both spellings" deliberately), but the cooldown store and
    # model_behavior registry are keyed by the raw, un-normalized `model` string —
    # `oc:zai/glm-5.3` and `opencode:zai/glm-5.3` would build SEPARATE cooldown/
    # behavior histories for what the board/dashboard treat as the SAME canonical
    # seat. `_agentic` is idempotent+canonical for an already-agentic id (returns
    # `oc:...` unchanged, rewrites `opencode:...` to it) — reused here rather than
    # duplicating that normalization, so a true-silence trip via either spelling
    # protects the next dispatch regardless of which spelling it uses.
    #
    # codex review finding, round 26 (P1 — supersedes the round-23/24 notes this
    # replaced): `canonical_model` must NEVER leak into a RETURNED `ReviewResult.model`
    # — only into internal cooldown-store / model_behavior-registry KEYS. Rounds 23-24
    # made the returned `.model` canonical on every exit path (dispatch, skip, unpaid,
    # preflight) to fix an inconsistency BETWEEN those paths — but that broke a
    # load-bearing invariant a caller several layers up already depends on:
    # `reviewlib/modes/review.py`'s flat-diff path builds `by_model = {result.model:
    # result for result in results}` and then looks up `by_model[model]` for each
    # ORIGINAL requested model string. `review diff -m opencode:zai/glm-5.3` would
    # dispatch with `models = ["opencode:zai/glm-5.3"]`, get back a result whose
    # `.model` was rewritten to `"oc:zai/glm-5.3"`, and KeyError on the lookup —
    # crashing a basic, real CLI invocation. The correct fix keeps EVERY returned
    # `ReviewResult.model` as the caller's own `model` string, unchanged — `canonical_
    # model` is scoped to internal lookups only (`active_cooldown`, `record_cooldown`/
    # `clear_cooldown` via `_record_true_silence_if_needed`, `true_silence_timeout_
    # seconds`), which never leak their key back into anything a caller inspects.
    #
    # codex review finding, round 25 (deferred, tracked #259): `_agentic` and
    # `oc_model` above parse a catch-all colon-form id (no `oc:`/`opencode:` prefix
    # — `resolve_backend`'s documented back-compat fallback for an unrecognized
    # model) DIFFERENTLY -- `oc_model` drops everything before the first colon
    # (silently discarding the provider segment), `_agentic` keeps it as a provider/
    # model pair. This is a real, narrow edge case (not the standard `oc:`/
    # `opencode:` path this fix covers), and fixing it means deciding the correct
    # provider-parsing contract for that pre-existing catch-all derivation — a
    # product/design question, not a mechanical one — so it's tracked, not fixed here.
    canonical_model = _api._agentic(model)
    unpaid = _api.unpaid_provider_result(
        model, backend="opencode", command=f"opencode -m {oc_model}", round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    # review-cli#153/#159/#179 fold-in of #187's cooldown design: checked BEFORE
    # `_which`/preflight (codex review finding — a cached-unavailable seat must skip
    # ALL real work, including the preflight network probe, not just the final
    # dispatch) so a cooling-down seat costs nothing beyond this cheap store read.
    # codex review finding (round 1): recording a true-silence cooldown is useless if
    # nothing ever CHECKS it before the next dispatch — mirrors review_claude's own
    # cooldown-consult pattern, so a seat that just went true-silent is actually
    # skipped (a synthetic sentinel, same shape failover already knows how to handle)
    # instead of paying for another multi-minute stuck attempt on every invocation.
    # `canonical_model` (not `model`) for the lookup — see the module comment above
    # about internal-lookup-only uses of `canonical_model`.
    cooldown = _api._cooldown_after_recovery_probe(
        canonical_model, access_method=_api._OPENCODE_ACCESS_METHOD
    )
    if cooldown is not None:
        # `backend="opencode"` (codex review finding, round 2): without this the skip
        # was hard-coded to "claude" in the sidecar log/dashboard, mislabeling every
        # opencode cooldown skip as a Claude/Fable event and hiding the real `oc:*`
        # seat's health. `model` (not `canonical_model`) here too — round 26.
        return _api._cooldown_skip_result(model, round_no, cooldown, backend="opencode")
    _api._which("opencode")
    preflight = _api.provider_preflight_result(
        model, backend="opencode", command=f"opencode -m {oc_model}", round_no=round_no
    )
    if preflight is not None:
        return preflight
    _api._ensure_opencode_readonly_agent(cwd, oc_model)

    if _api._opencode_runs_in_repo(cwd):
        command = f"opencode run --agent read-only-reviewer --dir {cwd} -m {oc_model} <prompt-with-diff>"
        # AGENTIC, read-only: run opencode in the REAL repo (like review_codex's
        # `codex exec -s read-only -C <cwd>`), so it can READ any project file — not
        # just the diff embedded in the prompt. SAFETY is enforced by the
        # read-only-reviewer agent: it DENIES bash/edit/write/webfetch/etc, so opencode
        # may open files but can NEVER mutate the user's repo, run a command, or hit the
        # network. The diff is still handed in the message as the review FOCUS; the repo
        # is the surrounding context the model can pull in on demand.
        message = (
            f"{prompt}\n\nYou are reviewing a real git repository in READ-ONLY mode: "
            "read any project file you need with your read tools, but do not edit, "
            "write, or run commands. "
            + (
                f"Focus on this diff:\n\n```diff\n{diff}\n```"
                if diff.strip()
                else "Answer based on the repository and the prompt."
            )
        )
        argv = [
            _api._which("opencode"),
            "run",
            "--agent",
            "read-only-reviewer",
            "--dir",
            str(cwd),
            "-m",
            oc_model,
        ]
        variant = _api._opencode_variant(effort)
        if variant:
            argv += ["--variant", variant]
        argv.append(message)
        proc = _api._run_opencode_with_stall_retry(
            argv,
            cwd=cwd,
            timeout=timeout,
            round_no=round_no,
            header_argv0=f"opencode -m {_api._safe_log_header(oc_model)}",
            model=canonical_model,
            true_silence_timeout=_api.true_silence_timeout_seconds(canonical_model),
        )
        _api._record_true_silence_if_needed(
            canonical_model, proc, access_method=_api._OPENCODE_ACCESS_METHOD
        )
        return _api.ReviewResult(
            model=model,
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # FALLBACK: cwd is not a git repo (e.g. a panel `--just-ask` run from a scratch
    # dir) — there is nothing to read, so keep the old isolated empty-temp-dir posture
    # and review the diff/prompt alone.
    command = (
        f"opencode run --agent read-only-reviewer -m {oc_model} <prompt-with-diff>"
    )
    with _api.tempfile.TemporaryDirectory(prefix="review-cli-opencode-") as tmp_raw:
        tmp = _api.Path(tmp_raw)
        # Strip the repo-pinning git env: a leaked GIT_DIR/GIT_WORK_TREE would make `git init`
        # operate on the LEAKED repo instead of this isolated temp dir, defeating the read-only
        # sandbox (review-cli#71). `tmp` is a fresh empty dir (not yet a repo), so git_repo_env
        # resolves no target git dir and drops every set var — exactly the isolation wanted.
        _api._run(
            ["git", "init", "-q"], cwd=tmp, env=_api.git_repo_env(tmp), timeout=30
        )
        if diff.strip():
            message = (
                f"{prompt}\n\nYou are running outside the source repo; do not edit files. "
                f"Review this diff:\n\n```diff\n{diff}\n```"
            )
        else:
            message = (
                f"{prompt}\n\nYou are running outside any source repo; do not edit files "
                "or run shell commands. Answer based on the prompt alone."
            )
        argv = [
            _api._which("opencode"),
            "run",
            "--agent",
            "read-only-reviewer",
            "-m",
            oc_model,
        ]
        variant = _api._opencode_variant(effort)
        if variant:
            argv += ["--variant", variant]
        argv.append(message)
        proc = _api._run_opencode_with_stall_retry(
            argv,
            cwd=tmp,
            timeout=timeout,
            round_no=round_no,
            header_argv0=f"opencode -m {_api._safe_log_header(oc_model)}",
            model=canonical_model,
            true_silence_timeout=_api.true_silence_timeout_seconds(canonical_model),
        )
    _api._record_true_silence_if_needed(
        canonical_model, proc, access_method=_api._OPENCODE_ACCESS_METHOD
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
