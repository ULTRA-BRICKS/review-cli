"""Backend cooldown implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _chronic_cause(_api, /, reason: str) -> str | None:
    """The typed cooldown cause for a `_chronic_unavailable_reason` result."""
    return _api._CHRONIC_REASON_CAUSE.get(reason)


def _cooldown_after_recovery_probe(
    _api, /, model: str, *, access_method: str
) -> dict | None:
    """`active_cooldown(model, access_method=access_method)`, but first gives a
    quota-cooled-down seat a chance to prove it has ALREADY recovered instead of
    blindly waiting out the full (possibly hours-long, escalated) cooldown window.

    Narrow on purpose: only a cooldown recorded for a QUOTA reason
    (`_CHRONIC_QUOTA_MARKERS` -- session/usage-credit limits and the weekly/monthly
    "limit exhausted. your limit will reset" notice)
    is probed. A true-silence timeout, a bare "unavailable sentinel", or any other
    chronic reason is NOT modeled by the usage-limit percent at all -- a healthy quota
    reading says nothing about whether an unrelated hang/notice has actually cleared,
    so probing those would be a false recovery, not a real one. Those reasons keep
    relying purely on the existing TTL.

    The probe (`usage_percent_for_model`) must return a CONCRETE percent below 100 to
    count as recovered -- `None` ("no data": no harness-cli installed, no tg-ctl
    sample, an unmapped provider) is explicitly NOT treated as recovered. Fail-open
    there would silently defeat the whole cooldown feature for every user without
    harness-cli installed (their very first re-check would always read "recovered"
    despite having no evidence at all) -- "unknown" must stay unknown; only a real
    reading proves recovery. A recovered seat's cooldown is cleared via
    `clear_cooldown` (not merely ignored for this one call) so the NEXT probe/report
    also sees it as healthy, matching `clear_cooldown`'s existing genuine-success
    contract elsewhere in this module.

    Also narrow on the ACCESS METHOD: `access_method="api"` is never probed either.
    `usage_percent_for_model` measures the Claude Code CLI subscription window, not
    the separate paid Anthropic API account -- a healthy CLI reading is no evidence
    the API transport's own problem (a bad key, a billing issue) has cleared.

    KNOWN ACCEPTED TRADE-OFF (review finding, tracked in review-cli#530 for a future
    pass, not fixed here to keep this change minimal): a `clear_cooldown` recovery
    also wipes the seat's escalation `fail_count` history (same as any other
    genuine-success clear in this module), and a tg-ctl fallback reading from
    `usage_percent_for_model` may be up to `usage_limits.DEFAULT_MAX_AGE_SECONDS`
    (24h) stale -- a stale-but-still-"fresh" sample could clear a chronic seat's
    escalation history early. This inherits the SAME trust window
    `spawn_refuse_reason` already uses to gate real dispatch for a seat with no
    cooldown at all; it is not a new risk this probe introduces, just an existing
    one now also reachable through the escalation path."""
    cooldown = _api.active_cooldown(model, access_method=access_method)
    if cooldown is None:
        return None
    if not any(
        marker in cooldown["reason"].lower() for marker in _api._CHRONIC_QUOTA_MARKERS
    ):
        return cooldown
    # access_method="api" is never probed -- see the docstring above ("Also narrow
    # on the ACCESS METHOD") for why. Keeps the existing TTL-only behavior, same
    # as any other reason this probe doesn't model.
    if access_method == "api":
        return cooldown
    pct = _api.usage_percent_for_model(model)
    if pct is not None and pct < 100.0:
        _api.clear_cooldown(model, access_method=access_method)
        return None
    return cooldown


def _looks_transient(_api, /, haystack: str, returncode: int) -> bool:
    """True when `haystack` (the error channel — GLM review finding, round 3: this is
    now ALSO called from the rc=0 branch, stderr-only there, not just rc!=0) ALSO
    carries retry.py's own TRANSIENT signal (a process timeout, a 429/5xx status, or
    a rate-limit/overload/service-unavailable
    phrase) — used to keep `_chronic_unavailable_reason`'s rc!=0 `_UNAVAILABLE_MARKERS`
    check (review-cli#fable-seat-reliability) from mis-caching a genuinely transient
    blip as a chronic, hours-long cooldown (see that function's docstring for the
    concrete 503/529 scenario this guards against).

    Codex review finding: a process TIMEOUT (`returncode == 124`, `retry._TIMEOUT_
    EXIT_CODE`) is retry.py's own FIRST, UNCONDITIONAL transient check — `classify_
    failure` returns RETRYABLE for rc=124 before it ever looks at the body text
    (retry.py:264). Without checking it here too, a timeout whose PARTIAL captured
    output happens to contain "is currently unavailable" (plausible: a killed
    subprocess's last buffered line) would be cached as chronic instead — retry.py
    would still want to retry it, but the cooldown would short-circuit that retry on
    every subsequent call. Checked directly, unconditionally, matching retry.py's own
    precedence (before the numeric-status/text checks below).

    `returncode` is ALSO checked directly for HTTP statuses (not just scanned as
    text): `review_claude_api` sets it to the literal HTTP status on an `HTTPError`
    (`rc = exc.code or 1`), so a real 503/529 reaches here as `returncode=503`/`529`,
    not a generic `1` — a plain numeric check is a more precise signal than hoping the
    status also appears in the body text. `haystack` (the SAME string the caller
    already built from stderr + a short stdout) covers the CLI-transport case, where
    the status is conveyed only in prose.

    k3 review finding: `retry.classify_failure` checks `_SEAT_FATAL_PATTERNS` BEFORE
    `_TRANSIENT_PATTERNS` — "a SEAT-FATAL channel wins over an incidental transient-
    looking substring" (retry.py's own docstring). Without mirroring that precedence
    here, a channel like `"401 Unauthorized: this account is temporarily unavailable"`
    would have `_looks_transient` return True (the transient wording matches) even
    though retry.py itself calls this SEAT_FATAL (401 wins there) — the worst outcome
    of both: no in-seat retry (retry.py's own call) AND no cooldown cache (this
    guard's call), so the seat pays a full doomed dispatch on literally every
    subsequent invocation. Checking `_SEAT_FATAL_PATTERNS` first, and returning False
    (never transient) on a match, restores the fatal-wins precedence: a seat-fatal
    channel HAS a chronic-worthy cooldown decision to make (deferred to the caller's
    own marker checks), never gets misrouted into "looks transient, withhold."

    Lazy import: `reviewlib.retry` imports `ReviewResult` from THIS module and
    `result_is_usable` from `panel` at its own module top level, so an eager
    module-level import here would be a circular import — mirrors `panel.py`'s
    existing lazy import of `reviewlib.retry` for the identical reason. Reusing
    retry.py's own pattern tuples (rather than second, hand-copied lists) means the
    two modules can never independently drift on what counts as transient/fatal."""
    from .retry import (
        _TIMEOUT_EXIT_CODE,
        _TRANSIENT_PATTERNS,
    )  # lazy: breaks the backends<->retry cycle

    if returncode == _TIMEOUT_EXIT_CODE:
        return True
    if _api._is_seat_fatal(haystack, returncode):
        return False
    if returncode in _api._TRANSIENT_HTTP_STATUSES:
        return True
    return any(p.search(haystack) for p in _TRANSIENT_PATTERNS)


def _is_seat_fatal(_api, /, haystack: str, returncode: int) -> bool:
    """True when `haystack` (the error channel — GLM review finding, round 3: this
    is now ALSO called from the rc=0 branch, stderr-only there, not just rc!=0)
    matches retry.py's own SEAT_FATAL vocabulary (auth failures, 401, refusals,
    ...). Shared by `_looks_transient` (a seat-fatal
    match wins over an incidental transient-looking substring, mirroring retry.py's
    own precedence) AND by `_chronic_unavailable_reason`'s marker checks directly.

    Codex review finding (review-cli#286): before this helper existed, a seat-fatal
    channel that also happened to contain the administrative-unavailable wording
    (concrete repro: `returncode=1, stderr="401 Unauthorized: model is currently
    unavailable to this account"`) fell through `_looks_transient`'s False return
    (correctly: an auth failure is not transient) straight into the unavailable-
    marker check below, and got cached as an hours-long chronic cooldown. That
    directly contradicts seat_cooldown.py's own documented contract that auth and
    other seat-fatal failures must never be cached — an auth failure resolves the
    moment credentials are fixed, not after a timer; caching it either strands the
    seat for the full escalation window after the fix, or (if never fixed) wastes a
    dispatch on the SAME account-level issue every window regardless. A seat-fatal
    channel is deferred entirely to retry.py's own no-retry verdict; it is never a
    cooldown-worthy CHRONIC reason from this module's cache.

    k3 review finding (round 8): `returncode` is ALSO checked directly against
    `_SEAT_FATAL_HTTP_STATUSES` (401/403/501) — not just scanned as text — mirroring
    `_looks_transient`'s identical numeric-vs-text split for the transient side. A
    gateway that conveys the status ONLY via the exit code (no digits anywhere in the
    body) would otherwise never match any text pattern and be misread as "not
    seat-fatal", reaching the marker check and getting wrongly cached."""
    if returncode in _api._SEAT_FATAL_HTTP_STATUSES:
        return True
    from .retry import _SEAT_FATAL_PATTERNS  # lazy: breaks the backends<->retry cycle

    return any(p.search(haystack) for p in _SEAT_FATAL_PATTERNS)


def _chronic_unavailable_reason(_api, /, result: _shared.ReviewResult) -> str | None:
    """Keep transient/fatal errors out of administrative and quota cooldowns.

    Successful long reviews are never scanned. Short rc0 sentinels consult stderr
    only for transient/fatal evidence. Failed calls inspect unbounded stderr plus
    bounded stdout: narrow quota markers win, while generic unavailable markers
    require non-transient/non-fatal and individually bounded channels.
    """
    body = (result.stdout or "").strip()
    if result.returncode == 0:
        return _successful_sentinel_reason(_api, result, body)
    from .retry import _TIMEOUT_EXIT_CODE

    if result.returncode == _TIMEOUT_EXIT_CODE:
        return None
    stderr = (result.stderr or "").strip()
    error_channel = stderr.lower()
    if body and len(body) <= _api._UNAVAILABLE_MAX_LEN:
        error_channel += "\n" + body.lower()
    if any(marker in error_channel for marker in _api._CHRONIC_QUOTA_MARKERS):
        return _api._QUOTA_COOLDOWN_REASON
    if _api._looks_transient(error_channel, result.returncode) or _api._is_seat_fatal(
        error_channel, result.returncode
    ):
        return None
    return _bounded_sentinel_reason(_api, stderr, body)


def _clears_cooldown_history(_api, /, result: _shared.ReviewResult) -> bool:
    """True when `result` is genuine RECOVERY evidence — safe grounds to wipe any
    escalated cooldown history via `clear_cooldown` (see its own docstring). Shared
    by both `record_cooldown`/`clear_cooldown` call sites (the main `review_claude`
    dispatch and the `--visual` images path) so the two can never independently
    drift on what counts as "genuine success".

    review-cli#221: `reason is None` (from `_chronic_unavailable_reason`) alone
    isn't enough here, since it also covers a non-chronic FAILURE (returncode != 0)
    that must NOT be treated as recovery evidence. Round-4 review finding (k3):
    `returncode == 0` alone isn't "genuine success" either — panel.py's own
    `result_is_usable` treats rc=0 with an EMPTY body as a failure shape too ("a
    silently-disabled model often returns rc=0 with nothing"). Without this check,
    a seat oscillating between chronic-quota failures (escalates) and empty-rc0
    responses (clears) would never actually escalate — pinned at the 10-minute
    window forever, the exact chronically-broken shape this feature exists to push
    out of the pool.

    Codex review finding (review-cli#286, round 3): a bare `returncode == 0 and
    stdout.strip()` ALSO wrongly clears history for the rc=0 transient-stderr
    exception `_chronic_unavailable_reason` now recognises — a result whose stdout
    IS the unavailable sentinel, but whose stderr independently looks transient
    (so the cache guard withholds a chronic verdict for it this particular call).
    That is NOT genuine recovery evidence; it is the SAME "still unavailable"
    sentinel, just not cached this time. `result_is_usable` (panel.py) already
    excludes exactly this shape (rc=0 + a short body matching `_UNAVAILABLE_
    MARKERS` returns False), so reusing it here instead of a second, ad hoc
    truthiness check closes the gap without duplicating sentinel-detection logic a
    third time."""
    from .panel import result_is_usable  # lazy: panel imports FROM backends at its
    # own module top level, so an eager import here would be a circular import.

    return result_is_usable(result)


def _bounded_cooldown_skip_body(
    _api, /, model: str, reason: str, remaining: int
) -> str:
    """Build the cooldown-skip stdout, guaranteed <= `_UNAVAILABLE_MAX_LEN` chars so
    `panel.result_is_usable`'s length-gated marker scan (and `retry._is_rc0_sentinel`'s
    identical bound) always recognises it as the sentinel it deliberately mirrors.

    glm review finding: the previous, unbounded f-string could exceed the bound — the
    `reason` seat_cooldown persists can be up to `_REASON_MAX_LEN=200` chars, and
    `model` is the raw, unvalidated seat string from `-m`/a board config entry, with no
    length limit of its own. Past the bound, `result_is_usable` stops scanning for
    markers at all and returns `True` — silently demoting a cached skip (that never ran
    a real review) to a "successful" rc=0 verdict that satisfies the flat path's `ok`
    and the `--commit`/`--staged` gate, exactly the certification failure this whole
    sentinel-mirroring design exists to prevent.

    `reason` (diagnostic only) is truncated first; `model` (the seat identifier a human
    actually needs to read in run-stats) only as a last resort. The marker phrase itself
    (`_UNAVAILABLE_MARKERS[0]`) is NEVER truncated — it's the one substring every
    downstream consumer actually keys on, so truncating it would defeat recognition
    entirely rather than just look slightly odd.

    Opus review finding, round 4: `remaining` used to be embedded as-is, treated as
    part of the FIXED template overhead the `model`/`reason` budgets are computed
    against — but it is not actually bounded anywhere. `_ttl_seconds()` rejects a
    non-finite `$REVIEW_SEAT_COOLDOWN_SECONDS` (NaN/inf), but a pathologically large yet
    still-FINITE value (e.g. approaching float64's ~1.8e308 ceiling) survives that guard
    and renders as a many-hundred-digit `remaining`, which alone can exceed
    `_UNAVAILABLE_MAX_LEN` — at which point even truncating `model` to nothing (the
    function's own last resort) cannot bring the body back under the bound, silently
    breaking the "guaranteed <= max_len" promise this docstring makes and letting
    `result_is_usable` treat the skip as a real result. Clamping `remaining` to a sane
    ceiling up front (a cooldown lasting >31 years is meaningless to display precisely
    anyway) keeps it a true fixed-width part of the overhead, restoring the guarantee
    unconditionally rather than only for realistic TTLs."""
    marker = _api._UNAVAILABLE_MARKERS[0]
    # 999,999,999s (~31.7 years) — always <= 9 digits, comfortably below any TTL a real
    # operator would configure; a display value beyond this is meaningless precision,
    # not information worth spending the length budget on.
    remaining = min(remaining, 999_999_999)

    def _build(m: str, r: str) -> str:
        return (
            f"{m} {marker} (cached: {r}; skip expires in {remaining}s — "
            "reviewlib.seat_cooldown).\n"
        )

    body = _build(model, reason)
    if len(body) <= _api._UNAVAILABLE_MAX_LEN:
        return body
    overhead = len(_build(model, ""))
    reason_budget = max(0, _api._UNAVAILABLE_MAX_LEN - overhead)
    body = _build(model, reason[:reason_budget])
    if len(body) <= _api._UNAVAILABLE_MAX_LEN:
        return body
    overhead_no_model = len(_build("", ""))
    model_budget = max(0, _api._UNAVAILABLE_MAX_LEN - overhead_no_model)
    return _build(model[:model_budget], "")


def _usage_limit_skip_result(
    _api, /, model: str, round_no: int, reason: str, *, backend: str
) -> _shared.ReviewResult:
    """Synthetic skip when a family's 5-hour or 7-day window is exhausted.

    Mirrors `_cooldown_skip_result`'s sentinel shape (rc=0 + unavailable
    marker) so failover/dashboard treat it as unusable, without Popen."""
    marker = _api._UNAVAILABLE_MARKERS[0]
    stdout = f"{model} {marker} ({reason}).\n"
    if len(stdout) > _api._UNAVAILABLE_MAX_LEN:
        stdout = f"{marker} ({reason}).\n"[: _api._UNAVAILABLE_MAX_LEN]
    command = f"usage-limit skip ({backend})"
    if _api.account_budget.is_budget_hold(reason):
        command += _api._BUDGET_SKIP_SUFFIX
    _api._emit_rest_log(
        backend, command, round_no=round_no, returncode=0, stdout=stdout, stderr=""
    )
    return _api.ReviewResult(
        model=model, command=command, returncode=0, stdout=stdout, stderr=""
    )


def _cooldown_skip_result(
    _api, /, model: str, round_no: int, cooldown: dict, *, backend: str
) -> _shared.ReviewResult:
    """Build the synthetic ReviewResult for a cached-cooldown skip, and log it via the
    same REST sidecar path REST backends use — so the dashboard still sees the call (as
    a skipped/paywall-shaped one) instead of the seat silently vanishing from a session.

    ``backend`` attributes the skip to the ACTUAL seat that cooled down (codex review
    finding, round 2: an earlier version of this function hard-coded "claude" for every
    caller, so an opencode true-silence cooldown skip was logged/dashboarded as a
    Claude/Fable event — corrupting attribution and hiding the real `oc:*` seat).
    Required (no default) as of round 3 (Fable review finding): a default of "claude"
    would silently reintroduce the exact same misattribution class for the NEXT
    backend that grows a cooldown gate (review-cli#235 plans five more) if its author
    simply forgets the keyword — every call site must now name its own backend
    explicitly, enforced at call time, not by convention.

    The command label deliberately does NOT say "Anthropic API" (kimi review finding:
    that string is `_claude_api_command`'s REST-transport label and would mislabel a
    CLI-mode seat's skip as an API call in the sidecar log/dashboard, misleading a
    post-mortem) — the skip never chose a transport at all, it short-circuited before
    either.

    codex review finding, round 4: attributing to the BACKEND alone isn't enough for
    opencode -- `model_id_for_call` (reviewlib/dashboard/parser.py) only resolves an
    opencode call to its real board seat (`oc:<provider/model>`) via an `-m <model>`
    token in argv0, same shape `review_opencode`'s own real dispatch already writes
    (`header_argv0=f"opencode -m {oc_model}"`). Without it, a true-silence cooldown
    skip fell through to the generic `opencode` bucket -- present in the log, but
    invisible on the specific seat's health/error row, defeating the round-2 fix's
    whole point. `-m <model>` must be followed by whitespace (the parser's regex is
    greedy non-whitespace-run), so the parenthetical note goes AFTER the model token,
    not glued to it. If a future backend growing its own cooldown gate (review-cli#235) resolves
    the same way (omp does, per parser.py's comment), extend this the same way."""
    if backend == "opencode":
        # Opus review finding, round 5: matches the real dispatch's own header
        # construction exactly (`header_argv0=f"opencode -m {_safe_log_header(oc_model)}"`
        # in review_opencode) — cosmetic today for a normal `provider/model` selector, but
        # keeps the skip's parser-facing header byte-for-byte consistent with the real
        # call in case a model selector ever needs sanitizing.
        oc_model = model.split(":", 1)[1] if ":" in model else model
        command = f"opencode -m {_api._safe_log_header(oc_model)} (seat-cooldown skip)"
    else:
        command = f"seat-cooldown skip ({backend})"
    remaining = int(cooldown["remaining_seconds"])
    stdout = _api._bounded_cooldown_skip_body(model, cooldown["reason"], remaining)
    _api._emit_rest_log(
        backend, command, round_no=round_no, returncode=0, stdout=stdout, stderr=""
    )
    return _api.ReviewResult(
        model=model, command=command, returncode=0, stdout=stdout, stderr=""
    )


def _successful_sentinel_reason(_api, result, body):
    if not body or len(body) > _api._UNAVAILABLE_MAX_LEN:
        return None
    if not any(marker in body.lower() for marker in _api._UNAVAILABLE_MARKERS):
        return None
    stderr = (result.stderr or "").strip()
    if stderr and (
        _api._looks_transient(stderr.lower(), result.returncode)
        or _api._is_seat_fatal(stderr.lower(), result.returncode)
    ):
        return None
    return _api._SENTINEL_COOLDOWN_REASON


def _bounded_sentinel_reason(_api, stderr, body):
    channel = "\n".join(
        part
        for part in (stderr, body)
        if part and len(part) <= _api._UNAVAILABLE_MAX_LEN
    ).lower()
    return (
        _api._SENTINEL_COOLDOWN_REASON
        if any(marker in channel for marker in _api._UNAVAILABLE_MARKERS)
        else None
    )
