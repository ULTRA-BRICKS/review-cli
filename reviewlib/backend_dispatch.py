"""Backend dispatch implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _which(_api, /, name: str) -> str:
    path = _api._which_optional(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def _which_optional(_api, /, name: str) -> str | None:
    """`shutil.which`, but as a backends-local indirection. The claude CLI resolution
    (review-cli#76) needs the present-or-absent form (None on miss, not a raise); routing
    it through this module symbol lets tests patch `backends._which_optional` to simulate
    a host's CLI inventory WITHOUT monkeypatching the stdlib `shutil.which` globally."""
    return _api.shutil.which(name)


def _payload(_api, /, prompt: str, diff: str) -> str:
    out = prompt if not diff.strip() else f"{prompt}\n\n```diff\n{diff}\n```"
    if len(out.encode("utf-8")) > _api._PAYLOAD_WARN_BYTES:
        print(
            f"[review-cli] WARNING: payload is {len(out)} bytes — nearing the ~1 MB "
            "ARG_MAX ceiling; opencode (argv-only) may fail. "
            "Use fewer --max-rounds or a smaller diff.",
            file=_api.sys.stderr,
            flush=True,
        )
    return out


def _claude_api_model(_api, /, model: str) -> str:
    return (
        model.split(":", 1)[1]
        if ":" in model
        else _api.os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
    )


def _claude_api_command(_api, /, model: str) -> str:
    return f"Anthropic API {_api._claude_api_model(model)}"


def _prompt_with_effort(_api, /, prompt: str, effort: str | None) -> str:
    if not effort:
        return prompt
    effort_label = "highest" if effort in {"xhigh", "max"} else effort
    hint = f"Use {effort_label} reasoning effort."
    if hint in prompt:
        return prompt
    return f"{prompt}\n\n{hint}"


def _codex_reasoning_effort(_api, /, effort: str | None) -> str | None:
    if not effort:
        return None
    if effort == "minimal":
        return "low"
    if effort == "max":
        return "xhigh"
    return effort


def _claude_reasoning_effort(_api, /, effort: str | None) -> str | None:
    if not effort:
        return None
    if effort == "minimal":
        return "low"
    return effort


def _opencode_variant(_api, /, effort: str | None) -> str | None:
    """The opencode `--variant` value for a review effort level, or None.

    opencode's `--variant` is "provider-specific reasoning effort" (its help lists
    `high`, `max`, `minimal`) forwarded to whatever provider the seat routes to. review's
    effort vocabulary is a strict superset of those examples, so the level passes straight
    through; opencode + the provider decide how to honour it."""
    if not effort:
        return None
    return effort


def _http_error_text(_api, /, exc: _shared.urllib.error.HTTPError) -> str:
    """An HTTP error's body, plus its `Retry-After` header as a trailing line: the header
    is where a provider says how long to wait, and `availability` reads it from the text."""
    body = exc.read().decode("utf-8", "replace")
    headers = getattr(exc, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    return f"{body}\nRetry-After: {retry_after}" if retry_after else body


def call_backend(
    _api,
    /,
    backend: _shared.Callable[..., _shared.ReviewResult],
    model: str,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    *,
    effort: str | None,
) -> _shared.ReviewResult:
    """Run one seat call while holding a slot of its provider's host-wide budget
    (reviewlib.admission, review-cli#614); a 429/overload reply feeds back into that
    budget. The per-call `timeout` starts only once the slot is held, and is capped to
    the time left of an explicit `--timeout N`: an HTTP backend has no process for the
    command deadline to kill, so queue time plus its call must not outrun N.

    A seat that is excluded (`availability`: its quota/credits/login window has not
    ended) is answered with the cached-skip sentinel BEFORE it takes a provider slot,
    and what a failed call says about when the seat answers again is recorded."""
    excluded = _api.availability_exclusion(model)
    if excluded is not None:
        return _api._availability_skip_result(model, round_no, excluded)
    with _api.admission.seat_slot(model) as denied:
        if denied is not None:
            return _api._admission_skip_result(model, round_no, denied)
        remaining = _api.run_deadline_remaining()
        if remaining is not None:
            timeout = max(1, min(timeout, _api.math.ceil(remaining)))
        result = _api._call_backend_now(
            backend, model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    _api.admission.note_result(model, result)
    _api.note_availability(model, result)
    return result


def _availability_methods(_api, /, model: str) -> tuple[str, ...]:
    """The cooldown-store transports `model` can actually run on, whose older escalating-
    timer entries therefore exclude it: claude's resolved transport, opencode's and omp's
    own bucket, else the CLI/API buckets (the `window` bucket is always checked)."""
    backend = _api.resolve_backend(model)
    if backend is _api.review_claude:
        return (_api.claude_access_method(),)
    if backend is _api.review_opencode:
        return (_api._OPENCODE_ACCESS_METHOD,)
    if backend is _api.review_omp:
        return (_api._OMP_ACCESS_METHOD,)
    return ("cli", "api")


def availability_exclusion(
    _api, /, model: str
) -> _shared.availability.Exclusion | None:
    """Why `model` must not be dispatched right now — a quota/credits/login/rate-limit
    window recorded from an earlier failure (own seat or whole provider) — or None.
    Disabled under the test fake backend and by `$REVIEW_SEAT_COOLDOWN_SECONDS<=0`."""
    if _api._fake_backend_enabled():
        return None
    try:
        return _api.availability.exclusion(
            model,
            methods=_api._availability_methods(model),
            legacy_check=lambda m, access_method: _api._cooldown_after_recovery_probe(
                m, access_method=access_method
            ),
        )
    except Exception:  # noqa: BLE001 — a bookkeeping failure must not block a seat
        return None


def note_availability(_api, /, model: str, result: _shared.ReviewResult) -> None:
    """Feed a finished seat call to `availability.observe`. Never raises."""
    if _api._fake_backend_enabled():
        return
    try:
        from .panel import result_is_usable

        _api.availability.observe(model, result, usable=result_is_usable(result))
    except Exception:  # noqa: BLE001 — see availability.observe
        pass


def _availability_skip_result(
    _api, /, model: str, round_no: int, excluded: _shared.availability.Exclusion
) -> _shared.ReviewResult:
    """The cached-skip sentinel for an excluded seat: same shape as a cooldown skip, so
    failover and the dashboard treat it as unavailable without any call being made."""
    return _api._cooldown_skip_result(
        model,
        round_no,
        {
            "reason": excluded.describe(),
            "remaining_seconds": excluded.remaining_seconds,
        },
        backend=_api.provider_route_name(model),
    )


def _admission_skip_result(
    _api, /, model: str, round_no: int, reason: str
) -> _shared.ReviewResult:
    """Synthetic rc=0 "unavailable" sentinel for a seat whose provider stayed saturated
    past `admission.provider_wait_seconds`, so board/provider failover moves on to the
    next provider or a reserve seat (same shape as `_usage_limit_skip_result`)."""
    marker = _api._UNAVAILABLE_MARKERS[0]
    stdout = f"{model} {marker} ({reason}).\n"
    if len(stdout) > _api._UNAVAILABLE_MAX_LEN:
        stdout = f"{marker} ({reason}).\n"[: _api._UNAVAILABLE_MAX_LEN]
    command = "admission skip (provider saturated)"
    _api._emit_rest_log(
        _api.provider_route_name(model),
        command,
        round_no=round_no,
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    return _api.ReviewResult(
        model=model, command=command, returncode=0, stdout=stdout, stderr=""
    )


def _call_backend_now(
    _api,
    /,
    backend: _shared.Callable[..., _shared.ReviewResult],
    model: str,
    prompt: str,
    diff: str,
    cwd: _shared.Path,
    timeout: int,
    round_no: int,
    *,
    effort: str | None,
) -> _shared.ReviewResult:
    if effort is None:
        return backend(model, prompt, diff, cwd, timeout, round_no)
    try:
        sig = _api.inspect.signature(backend)
    except (TypeError, ValueError):
        accepts_effort = False
    else:
        effort_param = sig.parameters.get("effort")
        accepts_effort = (
            effort_param is not None
            and effort_param.kind != _api.inspect.Parameter.POSITIONAL_ONLY
        ) or any(
            p.kind == _api.inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
    prompt = _api._prompt_with_effort(prompt, effort)
    if accepts_effort:
        return backend(model, prompt, diff, cwd, timeout, round_no, effort=effort)
    return backend(model, prompt, diff, cwd, timeout, round_no)


def review_with_images(
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
) -> _shared.ReviewResult:
    """Run a backend with raw image attachments when that backend safely supports them.

    Unsupported text backends still receive the grounded visual note in the prompt; the
    raw image path is an additive transport for capable backends, not a new hard
    dependency for every panel seat.
    """
    backend = _api.resolve_backend(model)
    if images and backend is _api.review_claude:
        # The API uses separate billing, so subscription admission/cooldowns do
        # not apply. The existing API/text fallback owns its transport checks.
        if _api.claude_access_method() == "api":
            return _api.call_backend(
                backend, model, prompt, diff, cwd, timeout, round_no, effort=effort
            )
        unpaid = _api.unpaid_provider_result(
            model,
            backend="claude",
            command=_api._claude_api_command(model),
            round_no=round_no,
            provider=_api._claude_gateway_provider_from_env(),
        )
        if unpaid is not None:
            return unpaid
        # Mirrors review_claude()'s own cooldown check/record (this branch bypasses
        # review_claude entirely to reach the images-capable CLI transport, so it must
        # opt back into the SAME contract explicitly — kimi review finding: --visual
        # was the one dispatch path that never consulted or recorded seat_cooldown).
        # Always the CLI transport (review_claude_cli_with_images), so it shares the
        # "cli" cooldown bucket with review_claude()'s own CLI branch (review-cli#187) —
        # both really are the same underlying CLI/quota.
        cooldown = _api._cooldown_after_recovery_probe(model, access_method="cli")
        if cooldown is not None:
            return _api._cooldown_skip_result(
                model, round_no, cooldown, backend="claude"
            )
        with _api.admission.seat_slot(model) as denied:
            if denied is not None:
                return _api._admission_skip_result(model, round_no, denied)
            from .backend_claude import review_cli_images_with_accounts

            result = review_cli_images_with_accounts(
                _api, model, prompt, diff, cwd, timeout, round_no, images, effort
            )
        _api.admission.note_result(model, result)
        if result.command.endswith(_api._BUDGET_SKIP_SUFFIX):
            return result
        reason = _api._chronic_unavailable_reason(result)
        if reason is not None:
            _api.record_cooldown(
                model, reason, access_method="cli", cause=_api._chronic_cause(reason)
            )
        elif _api._clears_cooldown_history(result):
            _api.clear_cooldown(model, access_method="cli")
        return result
    return _api.call_backend(
        backend, model, prompt, diff, cwd, timeout, round_no, effort=effort
    )


def _ensure_codex_recursion_guard(_api, /) -> None:
    """Best-effort, once-per-process install of the review-cli#180 execpolicy guard
    (see `install.install_codex_recursion_guard` for the rationale and its
    limitations). Never raises — a failed write must not block a review run; the
    REVIEW_CLI_ACTIVE reentrancy guard (`reviewlib.cli._reject_if_reentrant`) is the
    mechanism that still holds either way. Prints a one-line stderr notice ONLY when
    the file is actually created/rewritten (never on the common no-op "already up to
    date" path) — this writes into the user's `$CODEX_HOME` as a side effect of an
    ordinary review run, so it must not be silent (review-cli#180 review finding,
    glm-5.2)."""
    pass  # Mutable state is owned by the public backend module.
    if _api._codex_recursion_guard_ensured:
        return
    with _api._codex_recursion_guard_lock:
        if _api._codex_recursion_guard_ensured:
            return
        try:
            if _api.install_codex_recursion_guard():
                print(
                    "[review-cli] installed/updated the codex execpolicy recursion "
                    "guard at $CODEX_HOME/rules/ (review-cli#180) — forbids codex "
                    "from shelling out to review/codex/claude/opencode/omp.",
                    file=_api.sys.stderr,
                    flush=True,
                )
        except Exception:  # noqa: BLE001 — must never break a review call
            pass
        _api._codex_recursion_guard_ensured = True


def _cwd_is_git_repo(_api, /, cwd: _shared.Path) -> bool:
    """True when `cwd` is inside a git work tree. codex's `-s read-only -C <cwd>`
    refuses to run ("Not inside a trusted directory and --skip-git-repo-check was
    not specified") for a non-repo cwd (review-cli#168's reported `codex:gpt-5.6-terra`
    failure) -- e.g. review-cli's own omp-cage sandbox dirs, or a plain scratch dir a
    caller points `review visual`/`--diff` at. Any subprocess failure (git missing,
    timeout) is treated as "not a repo", the conservative default that still lets
    codex make its own trust decision rather than review-cli asserting a repo exists."""
    try:
        proc = _api.subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, _api.subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def review_codex(
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
    codex_model = model.split(":", 1)[1] if ":" in model else None
    unpaid = _api.unpaid_provider_result(
        model, backend="codex", command="codex", round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    codex_path = _api._which("codex")
    # Only after the unpaid short-circuit and the PATH check: installing the execpolicy
    # guard writes into $HOME (review-cli#180 review finding, Opus) — a board that lists
    # a codex seat the user has no binary for, or has disabled as unpaid, must not touch
    # their home directory or print a guard-installed notice for a backend that never runs.
    _api._ensure_codex_recursion_guard()
    argv = [codex_path, "exec", "-s", "read-only", "-C", str(cwd), "--ephemeral"]
    if not _api._cwd_is_git_repo(cwd):
        # review-cli#168: codex refuses a non-repo `-C` dir outright otherwise.
        argv.append("--skip-git-repo-check")
    if codex_model:
        argv += ["-m", codex_model]
    codex_effort = _api._codex_reasoning_effort(effort)
    if codex_effort:
        argv += ["-c", f'model_reasoning_effort="{codex_effort}"']
    argv.append("-")
    command = " ".join(argv[:-1]) + " -"
    proc = _api._run_streamed(
        argv,
        cwd=cwd,
        input_text=_api._payload(prompt, diff),
        timeout=timeout,
        backend="codex",
        round_no=round_no,
        announce=_api._ANNOUNCE_LOGS,
        true_silence_timeout=_api.true_silence_timeout_seconds(model),
        header_argv0=f"codex -m {_api._safe_log_header(codex_model)}"
        if codex_model
        else None,
    )
    return _api.ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _fake_backend_enabled(_api, /) -> bool:
    # Normalize case so `False`/`FALSE`/`No` are treated as OFF (not just lowercase forms) —
    # otherwise a case-variant false value would silently route every backend to the fake.
    return _api.os.environ.get("REVIEW_FAKE_BACKEND", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def review_fake(
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
    """Deterministic, network-free stand-in for a real backend (TEST-ONLY).

    Distinguishes the brainstorm prompt shapes by their fixed lead-in text (imported from the
    prompt SOURCE — `modes.brainstorm` — so a reword can never silently misroute the fake):
      * the final SYNTHESIS prompt (SYNTHESIS_PROMPT_MARKER) -> a synthesis body;
      * a MODERATOR round prompt (MODERATOR_PROMPT_LEADIN) -> a summary ending in
        `DECISION: CONTINUE` (so the loop runs to its max cap, then synthesizes — a
        deterministic, always-terminating run with a guaranteed final synthesis);
      * anything else (a persona prompt) -> a short per-round idea.
    The text embeds `model`/`round_no` so each round's content is distinct and a resumed
    run's new rounds are visibly different from the pre-kill ones. `REVIEW_FAKE_DELAY`
    (float seconds, default 0) sleeps per call so an e2e can make a run slow enough to
    reliably SIGTERM it mid-round; the delay still honours `timeout` as an upper bound.
    `REVIEW_FAKE_RECORD_DIR` (a directory) makes every call write one `<ns>-<pid>-<tid>.json`
    file holding the `model`, `prompt` and `diff` it received, so an e2e can assert exactly
    what each seat was sent (e.g. that a chunked diff's passes cover every file)."""
    prompt = _api._prompt_with_effort(prompt, effort)
    record_dir = _api.os.environ.get("REVIEW_FAKE_RECORD_DIR", "").strip()
    if record_dir:
        target = _api.Path(record_dir)
        target.mkdir(parents=True, exist_ok=True)
        (
            target
            / f"{_api.time.time_ns()}-{_api.os.getpid()}-{_api.threading.get_ident()}.json"
        ).write_text(
            _api.json.dumps({"model": model, "prompt": prompt, "diff": diff}),
            encoding="utf-8",
        )

    # Lazy import (not at module top) so this lower backends layer never imports the higher
    # modes layer at import time — only when the env-gated fake is actually exercised.
    from .modes.brainstorm import MODERATOR_PROMPT_LEADIN, SYNTHESIS_PROMPT_MARKER

    delay = 0.0
    try:
        delay = float(_api.os.environ.get("REVIEW_FAKE_DELAY", "0") or "0")
    except ValueError:
        delay = 0.0
    if delay > 0:
        _api.time.sleep(min(delay, max(timeout - 0.1, 0.0)))

    if SYNTHESIS_PROMPT_MARKER in prompt:
        body = (
            f"FINAL SYNTHESIS (fake/{model}). BEST IDEAS (ranked): 1) idea-A 2) idea-B. "
            "TRADEOFFS: A is simpler, B scales better. RECOMMENDATION: ship idea-A first."
        )
    elif MODERATOR_PROMPT_LEADIN in prompt:
        body = (
            f"Moderator summary (fake/{model}, round {round_no}): the panel converged on a "
            "couple of concrete directions.\nDECISION: CONTINUE"
        )
    else:
        body = (
            f"Idea from {model} in round {round_no}: a concrete, deterministic suggestion "
            f"(#{round_no}) the fake backend emits without any network or CLI."
        )
    # Mirror a real REST backend's sidecar live-log so `tail -f` / the dashboard parser behave
    # the same under the fake. A log-dir hiccup must not break the call, but it shouldn't fail
    # SILENTLY either — narrow to OSError (the only thing write_sidecar_log raises on disk) and
    # note it on stderr so a broken log dir is diagnosable.
    try:
        _api.write_sidecar_log(
            backend="fake",
            round_no=round_no,
            argv0=f"fake:{model}",
            returncode=0,
            stdout=body,
            stderr="",
        )
    except OSError as exc:
        print(
            f"[review-cli] fake sidecar log skipped: {exc}",
            file=_api.sys.stderr,
            flush=True,
        )
    return _api.ReviewResult(
        model=model, command=f"fake:{model}", returncode=0, stdout=body, stderr=""
    )


def _match_named_backend(
    _api, /, lowered: str
) -> _shared.Callable[..., _shared.ReviewResult] | None:
    """Match a model string to an EXPLICITLY-recognized backend, or None when nothing
    matches. `lowered` is the already-lowercased model id.

    This is the set of providers the CLI knows by name — every branch here is a deliberate,
    documented route. `resolve_backend` adds a catch-all opencode fallthrough on top (so an
    unknown id still runs *something*). Splitting the match out keeps one source of truth for
    the named routes and keeps `resolve_backend` a one-liner over it."""
    if lowered == "codex" or lowered.startswith("codex:"):
        return _api.review_codex
    if lowered in ("gemini", "gemini-api") or lowered.startswith("gemini:"):
        return _api.review_gemini
    # z.ai (Zhipu / GLM) — OpenAI-compatible keyed HTTP. `zai`/`glm` plus `zai:<model>`
    # (e.g. zai:glm-5.3). `glm:` prefix also routes here.
    if lowered in ("zai", "z.ai", "zhipu", "glm") or lowered.startswith(
        ("zai:", "z.ai:", "glm:", "zhipu:")
    ):
        return _api.review_zai
    # commandcode — Command Code's OpenAI-compatible Provider API (keyed HTTP).
    # The legacy `common-code`/`common_code` spellings still route here as aliases so
    # any pre-rename config keeps working.
    if lowered in (
        "commandcode",
        "command-code",
        "command_code",
        "common-code",
        "commoncode",
        "common_code",
    ) or lowered.startswith(
        (
            "commandcode:",
            "command-code:",
            "command_code:",
            "common-code:",
            "commoncode:",
            "common_code:",
        )
    ):
        return _api.review_commandcode
    # OpenRouter — OpenAI-compatible API aggregator (keyed HTTP). `openrouter` plus
    # `openrouter:<provider>/<model>` (e.g. openrouter:anthropic/claude-3.5-sonnet). Does NOT
    # collide with opencode's `oc:`/`opencode:` prefixes — `openrouter:` shares no prefix with
    # either, so the order relative to the opencode catch-all below is irrelevant.
    if lowered == "openrouter" or lowered.startswith("openrouter:"):
        return _api.review_openrouter
    # Oh My Pi (omp) — agentic read-only CLI seat (review-cli#174). `omp` plus
    # `omp:<provider>/<model>` (e.g. omp:kimi-code/k3); the selector after `omp:` goes
    # to omp's `--model` fuzzy matcher verbatim. `omp:` shares no prefix with `oc:`/
    # `opencode:`, but an explicit branch keeps the seat off the catch-all fallthrough.
    if lowered == "omp" or lowered.startswith("omp:"):
        return _api.review_omp
    # fable runs on the claude CLI. Route any fable form to review_claude defensively, so
    # an unexpanded `fable`/`fable5` can NEVER fall through to the review_opencode
    # default (which would hit fireworks — the wrong provider entirely).
    if (
        lowered in ("claude", "claude-fable-5", "claude-fable-5-1")
        or lowered.startswith("claude:")
        or lowered.startswith("fable")
    ):
        return _api.review_claude
    if lowered.startswith(("oc:", "opencode:")):
        return _api.review_opencode
    return None
