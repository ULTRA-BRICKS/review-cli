"""Backend rest implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _read_env_key(_api, /, env_file: _shared.Path, var: str) -> str | None:
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except (OSError, ValueError):  # unreadable, or not UTF-8 (UnicodeDecodeError)
        return None
    return None


def _resolve_key(_api, /, env_names: tuple, fallback_var: str) -> str | None:
    """Resolve a provider API key from the environment, then the same fallback files
    review already reads (GEMINI_ENV_FILE override, else GEMINI_ENV_FALLBACKS — the
    shared `~/.config/review-cli/.env` and the personal env). Returns None if unset.
    See `resolve_key_source` for the precedence."""
    found = _api.resolve_key_source(env_names, fallback_var)
    return found[1] if found is not None else None


def _env_key_files(_api, /) -> tuple[_shared.Path, ...]:
    env_file = _api.os.environ.get("GEMINI_ENV_FILE")
    return (_api.Path(env_file),) if env_file else _api.GEMINI_ENV_FALLBACKS


def resolve_key_source(
    _api, /, env_names: tuple, fallback_var: str
) -> tuple[str, str, str] | None:
    """(variable name, key, where: "env" or the .env path) for the key `_resolve_key`
    returns, or None if unset.

    Precedence is KEY-NAME-FIRST, not path-first: env var beats every file, and among
    the files the canonical/primary key name wins over an alias REGARDLESS of which
    .env file each lives in. So `COMMANDCODE_API_KEY` in a later fallback file beats
    `DEEPSEEK_API_KEY` in an earlier one — the precedence a caller reading the env-var
    order would expect. (A path-first loop would let an earlier file's alias shadow a
    later file's primary key, a surprising and non-deterministic ordering.)

    REUSE (§6.4 / CTO D9): vision providers piggyback on the SAME config surface review
    already uses for Gemini — no new per-provider egress config is invented."""
    # env var wins over any file, in declared order (primary name first).
    for name in env_names:
        value = _api.os.environ.get(name, "").strip()
        if value:
            return (name, value, "env")
    paths = _api._env_key_files()
    # File lookup is name-priority-first: fallback_var (the canonical/primary name),
    # then the remaining accepted aliases, each checked across ALL .env files before
    # moving to the next name. This keeps key-name precedence deterministic and
    # independent of file ordering, mirroring the env-var lookup above.
    file_vars = (fallback_var, *(n for n in env_names if n != fallback_var))
    for var in file_vars:
        for path in paths:
            key = _api._read_env_key(path, var)
            if key:
                return (var, key, str(path))
    return None


def _gemini_key(_api, /) -> str:
    key = _api._resolve_key(("GEMINI_API_KEY", "GOOGLE_API_KEY"), "GEMINI_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "GEMINI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def review_gemini(
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
    gemini_model = (
        model.split(":", 1)[1]
        if ":" in model
        else _api.os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    )
    command = f"Gemini API {gemini_model}"
    # Gemini is a REST backend — it never goes through `_run_streamed`, so it must emit
    # its own per-call sidecar log or the dashboard parser (which reads ONLY `.log`
    # files) would not see it at all: models undercounted, Gemini-only runs invisible
    # (HYP-742 finding 2). The sidecar carries the explicit EXIT status (finding 4) and
    # is stamped with the call's START time (captured now), so a slow call's duration
    # and session clustering stay correct (codex P2).
    #
    # KEY RESOLUTION IS INSIDE the try: a missing GEMINI_API_KEY is the COMMON failure
    # (gemini is in DEFAULT_MODELS), and `_gemini_key()` raising before the logged path
    # would leave that auth failure invisible — `run_panel` would turn it into an
    # internal 127 with no `.log` (codex P2). Now the auth failure emits a sidecar too.
    started = _api.datetime.now(_api.timezone.utc)
    unpaid = _api.unpaid_provider_result(
        model, backend="gemini", command=command, round_no=round_no, started=started
    )
    if unpaid is not None:
        return unpaid
    try:
        key = _api._gemini_key()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": _api._payload(prompt, diff)}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
        }
        data = _api.json.dumps(body).encode("utf-8")
        req = _api.urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        with _api.urllib.request.urlopen(req, timeout=timeout) as response:
            payload = _api.json.loads(response.read().decode("utf-8"))
        # Shape-guarded walk: `{"candidates": []}` (a filtered/blocked response) used to
        # raise IndexError here and surface as a generic failure with the spent tokens
        # lost; it is now the same fail-closed empty-content result as any other
        # candidate-less 2xx, with usage still parsed below.
        text = _gemini_response_text(payload)
        usage = _api._usage_dict(payload, "usageMetadata")
        # Empty success is a failure, exactly as on the OpenAI-compatible and Anthropic
        # paths: a 2xx with no candidate text has nothing to review with, and a bare
        # `prompt_tokens=...` footer would otherwise pass `result_is_usable()` as
        # review content and consume a seat instead of failing over. The tokens the
        # call SPENT still ride on the failed result for the per-attempt tally.
        rc = 0 if text.strip() else 1
        prompt_tokens, output_tokens = _api._validated_usage_pair(
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            completed=rc == 0,
        )
        if rc == 0:
            stdout = (
                text.strip()
                + f"\n\nprompt_tokens={prompt_tokens} output_tokens={output_tokens}\n"
            )
            stderr = ""
        else:
            stdout = ""
            stderr = f"Gemini API returned no candidate text: {_api.json.dumps(payload)[:500]}"
        _api._emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            started=started,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        return _api.ReviewResult(
            model=model,
            command=command,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
    except _api.urllib.error.HTTPError as exc:
        body_text = _api._http_error_text(exc)
        rc = exc.code or 1
        _api._emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=rc,
            stdout="",
            stderr=body_text,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=rc, stdout="", stderr=body_text
        )
    except Exception as exc:  # noqa: BLE001
        # Anything other than an HTTPError: a missing API key (RuntimeError from
        # `_gemini_key()`), a network/DNS/socket-timeout error (URLError), or a malformed
        # JSON response (ValueError). Without this the call would raise out of run_panel
        # as an "internal" 127 with NO `.log`, so a failed Gemini run would stay invisible
        # to the dashboard (codex P2). Emit a failure sidecar and return a normal non-zero
        # ReviewResult instead of raising.
        err = f"{type(exc).__name__}: {exc}"
        # A urlopen timeout (socket timeout, or a URLError wrapping one) must be recorded
        # as a TIMEOUT, not a generic error, so the dashboard's timeout metric stays
        # consistent with the subprocess backends (codex P2). rc 124 = the timeout code.
        if _api._is_timeout_error(exc):
            _api._emit_rest_log(
                "gemini",
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return _api.ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        _api._emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )


def _is_timeout_error(_api, /, exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a socket/network timeout.

    `urlopen(..., timeout=N)` surfaces a timeout as `socket.timeout` (== `TimeoutError`
    on 3.10+) directly, or as a `urllib.error.URLError` whose `.reason` is that timeout."""
    if isinstance(exc, (_api.socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (_api.socket.timeout, TimeoutError))


def _emit_rest_log(
    _api,
    /,
    backend: str,
    argv0: str,
    *,
    round_no: int,
    returncode: int,
    stdout: str,
    stderr: str,
    started: _shared.datetime | None,
    timed_out: bool,
    timeout_secs: int | None,
    prompt_tokens: int,
    output_tokens: int,
) -> None:
    """Best-effort sidecar log for a NON-subprocess (REST) backend run.

    Mirrors what `_run_streamed` writes for subprocess backends so the dashboard parser
    counts the run. ``backend`` is the canonical backend name (gemini, z.ai, commandcode)
    — it becomes the `{stamp}-{backend}-r{n}.log` filename segment the parser reads, so
    EACH REST backend is attributed to itself in the dashboard, not lumped under a single
    hardcoded name (HYP-742: z.ai/commandcode runs were invisible because this hardcoded
    "gemini"). Logging must never take down a review: a read-only log dir or write error
    is swallowed (the backend already produced its result). ``started`` is the call's
    START time so the dashboard reports an honest duration (codex P2); ``timed_out``
    records a TIMEOUT marker so a REST timeout counts as a timeout, not a generic error.
    ``prompt_tokens``/``output_tokens`` MUST be the same usage reading the returned
    `ReviewResult` carries, on every path that has one -- the dashboard Metrics tab reads
    usage only from this log (review-cli#369: a billed no-text failure used to log none)."""
    try:
        _api.write_sidecar_log(
            backend,
            round_no=round_no,
            argv0=argv0,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            started=started,
            timed_out=timed_out,
            timeout_secs=timeout_secs,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
    except Exception:  # noqa: BLE001 - logging is best-effort; it must NEVER change the
        # review outcome. Beyond OSError (read-only / full log dir), a provider can return
        # text with an unpaired surrogate that makes write_sidecar_log raise
        # UnicodeEncodeError (a ValueError) — and since this is called on the SUCCESS path
        # of the REST backends, an unswallowed error would flip a successful ReviewResult
        # into a failure (codex P3). Swallow everything; a missing sidecar only loses a log.
        pass


def _mode_env_var(_api, /, name: str) -> str:
    """The env var that forces a backend's transport mode, e.g. commandcode ->
    REVIEW_COMMANDCODE_MODE. Mirrors PR #8's REVIEW_CLAUDE_MODE naming so the
    whole family is discoverable from one rule."""
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in name.upper())
    return f"REVIEW_{sanitized}_MODE"


def resolve_backend_mode(
    _api, /, name: str, supported: tuple[str, ...], default: str
) -> str:
    """Resolve a backend's transport mode from REVIEW_<NAME>_MODE, validated against
    `supported`. Returns the forced mode if set and supported, else `default`.

    Raises RuntimeError on an explicitly-forced mode the backend does not support —
    that is a user configuration error (e.g. REVIEW_COMMANDCODE_MODE=cli when no
    commandcode CLI exists) and must surface loudly, not silently run the api path.
    An empty/unset value selects `default` (the backend's own auto-pick)."""
    forced = _api.os.environ.get(_api._mode_env_var(name), "").strip().lower()
    if not forced:
        return default
    if forced not in supported:
        raise RuntimeError(
            f"{_api._mode_env_var(name)}={forced!r} is not a supported mode for the "
            f"'{name}' backend (supported: {', '.join(supported)})"
        )
    return forced


def _parse_openai_choice(_api, /, payload: object) -> str:
    """Pull assistant text out of an OpenAI-compatible response, tolerating any shape.

    A provider can return a 2xx body that is valid JSON but NOT the expected object
    (e.g. `[]`, `{"choices":[null]}`, `{"choices":[{"message":[]}]}`). Each access is
    type-guarded so a wrong shape yields "" instead of raising AttributeError/
    TypeError/IndexError out of the backend (those would crash the whole run).

    REASONING MODELS (e.g. z.ai glm-5.3) return `message.content` (the final answer)
    PLUS `message.reasoning_content` (the chain of thought). We read `content` for the
    review text. When `content` is empty/missing but `reasoning_content` is present —
    a reasoning model with a low output-token budget can spend it all on reasoning and
    emit no final answer — we fall back to the reasoning text (prefixed so the reader
    knows what they're getting) rather than fail-closed as "empty output". A non-string
    reasoning field is ignored (type-guarded), never a crash."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return "[reasoning_content — no final answer returned]\n\n" + reasoning
    return content if isinstance(content, str) else ""


def _valid_token_count(_api, /, count: object) -> bool:
    """A real token count: a non-negative int (and not a bool, which is an int subclass)."""
    return isinstance(count, int) and not isinstance(count, bool) and count >= 0


def _usage_dict(_api, /, payload: object, key: str) -> dict:
    """The provider's usage object under `key` in a JSON response, or `{}` when the
    response or the usage entry is not a dict -- so every REST path reads usage through
    one shape guard instead of re-deriving it."""
    usage = payload.get(key) if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else {}


def _validated_usage_pair(
    _api, /, prompt: object, output: object, *, completed: bool
) -> tuple[int, int]:
    """The ONE validity rule for a provider usage reading, shared by every REST path
    (OpenAI-compatible, Anthropic, Gemini) so `run-stats.jsonl` and `review stat`'s
    log-derived `tokenstats` can never disagree on what counts as a measurement.

    All-or-nothing: a `usage` object with only one valid field is a partial reading,
    and defaulting the missing field to 0 independently would persist `(prompt, 0)` as
    a real total. 0/0 is the one value every reader already treats as "usage unknown",
    so anything short of a complete pair collapses to it.

    `completed` = the response carried real assistant content. A completed call can
    never genuinely have zero prompt OR zero output tokens, so either field being 0 is
    the same "absent/defaulted" signal `tokenstats.extract_usage_tokens` already
    rejects, and collapses to 0/0 too. A NON-completed call (a 2xx with no content,
    which fails closed) legitimately has output 0 while still having SPENT its prompt
    tokens, so that pair is kept as parsed -- provider failover tallies every dispatch
    attempt, and dropping it would lose real spend from the run record.
    """
    if not completed and output is None:
        # A provider that produced no content commonly OMITS the output field rather
        # than writing an explicit 0 (Gemini blocked responses, some OpenAI-compatible
        # gateways). That is the same "nothing generated" reading, so coalesce it --
        # otherwise the prompt tokens the call spent are lost on exactly the shape
        # this branch exists for.
        output = 0
    if not (isinstance(prompt, int) and isinstance(output, int)):
        return 0, 0
    if not (_api._valid_token_count(prompt) and _api._valid_token_count(output)):
        return 0, 0
    if completed and (prompt == 0 or output == 0):
        return 0, 0
    return prompt, output


def _parse_openai_usage(
    _api, /, payload: object, *, completed: bool
) -> tuple[int, int]:
    """Return (prompt_tokens, output_tokens) from an OpenAI-shape response via
    `_validated_usage_pair`'s rule; 0/0 ("unknown") on any wrong or partial shape."""
    usage = _api._usage_dict(payload, "usage")
    return _api._validated_usage_pair(
        usage.get("prompt_tokens"), usage.get("completion_tokens"), completed=completed
    )


def _openai_compatible_request(
    _api,
    /,
    *,
    model: str,
    api_model: str,
    label: str,
    base_url: str,
    key: str,
    prompt: str,
    diff: str,
    timeout: int,
    backend: str,
    round_no: int,
    extra_body: dict | None,
    extra_headers: dict | None,
) -> _shared.ReviewResult:
    """POST an OpenAI-compatible chat/completions request and return a ReviewResult.

    `model` is the REQUESTED backend string (e.g. `zai`, `commandcode:deepseek/deepseek-v4-flash`)
    and is preserved in ReviewResult.model — mode_review keys results by the requested
    string, so substituting the resolved provider id here would KeyError. `api_model`
    is the resolved provider model id sent on the wire (e.g. glm-4.6, deepseek/deepseek-v4-flash).

    `backend` is the canonical sidecar name (e.g. `z.ai`, `commandcode`) used for the
    dashboard `.log` file, and `round_no` is threaded from the panel. Like review_gemini,
    these REST backends never go through `_run_streamed`, so they MUST emit their own
    sidecar log on EVERY return path or the dashboard parser (which reads only `.log`
    files) never sees the run — z.ai/commandcode runs were invisible and models were
    undercounted (HYP-742). The sidecar is stamped with the call START time so a slow
    call's duration and session clustering stay correct (codex P2); a socket timeout is
    recorded as a TIMEOUT, not a generic error, keeping the timeout metric consistent.

    `extra_body` merges provider-specific request fields into the body while keeping the
    shared OpenAI wire shape generic. z.ai/commandcode/openrouter all pass None today; the
    hook stays for any future provider that needs a non-standard field.

    `extra_headers` merges provider-specific HTTP headers onto the shared
    Content-Type/Authorization pair (used by OpenRouter for its OPTIONAL leaderboard
    attribution headers HTTP-Referer / X-Title). It can never override the credential:
    any case-insensitive `authorization`/`content-type` key in `extra_headers` is dropped
    BEFORE the canonical pair is written, so a stray (even lower-cased) Authorization can't
    shadow the real bearer key — the defense is explicit, not a reliance on urllib's header
    capitalization. (Note: urllib normalizes header NAMES on the wire, e.g. `HTTP-Referer`
    → `Http-referer`; HTTP header names are case-insensitive so this is cosmetic.)
    This helper does NOT sanitize `extra_headers` VALUES — a value with a CR/LF or a
    non-latin-1 char would make http.client.putheader raise mid-send (caught here as a
    malformed-request ValueError, but a wasted call). The caller is responsible for passing
    safe values; OpenRouter's wrapper validates via `_header_value_is_safe` and drops bad ones.

    `base_url` is the endpoint root (e.g. https://api.z.ai/api/paas/v4); the
    /chat/completions suffix is appended here so callers pass the same value users
    would set in any OpenAI-compatible client. EVERY failure mode maps to a non-zero
    returncode with the error on stderr — HTTP status errors, connection refused /
    DNS / socket timeouts (URLError, OSError, TimeoutError), malformed JSON
    (JSONDecodeError), and valid-but-wrong-shape JSON (type-guarded parse) — so the
    panel treats a failed call as a dead backend rather than crashing the whole run."""
    url = base_url.rstrip("/") + "/chat/completions"
    command = f"{label} API {api_model}"
    started = _api.datetime.now(_api.timezone.utc)
    body = {
        "model": api_model,
        "messages": [{"role": "user", "content": _api._payload(prompt, diff)}],
        "stream": False,
    }
    if extra_body:
        body.update(extra_body)
    data = _api.json.dumps(body).encode("utf-8")
    # Drop any caller-supplied credential/content-type header (case-insensitively) BEFORE
    # writing the canonical pair, so a stray `extra_headers` entry — even a lower-cased
    # `authorization` that would be a DISTINCT dict key — can never shadow the real bearer
    # key or content type. The canonical pair is then the only authority for those names.
    _reserved = {"authorization", "content-type"}
    headers = {
        str(k): str(v)
        for k, v in (extra_headers or {}).items()
        if str(k).lower() not in _reserved
    }
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "review-cli/0.1")
    req = _api.urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with _api.urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        payload = _api.json.loads(raw)
        text = _api._parse_openai_choice(payload)
        # Usage is parsed BEFORE the empty-content check: a 2xx with a valid `usage`
        # but no assistant content still SPENT its prompt tokens, and provider
        # failover tallies every dispatch attempt -- the failed result must carry
        # them or they vanish from `run-stats.jsonl` (see `_validated_usage_pair`).
        prompt_tokens, output_tokens = _api._parse_openai_usage(
            payload, completed=bool(text.strip())
        )
        if not text.strip():
            # A 2xx whose body carries NO assistant content (`[]`, `{"choices":[null]}`,
            # `{"error":...}` with HTTP 200, an empty completion) is NOT a successful
            # review — it has nothing to review with. Returning rc=0 here would let
            # mode_review write a "reviewed" stamp and satisfy the commit gate with an
            # empty result. Fail-closed: map it to a non-zero dead-backend result.
            stderr = f"{label} API returned no assistant content: {raw[:500]}"
            _api._emit_rest_log(
                backend,
                command,
                round_no=round_no,
                returncode=1,
                stdout="",
                stderr=stderr,
                started=started,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
            return _api.ReviewResult(
                model=model,
                command=command,
                returncode=1,
                stdout="",
                stderr=stderr,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
        stdout = text.strip() + (
            f"\n\nprompt_tokens={prompt_tokens} output_tokens={output_tokens}\n"
        )
        _api._emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=0,
            stdout=stdout,
            stderr="",
            started=started,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        return _api.ReviewResult(
            model=model,
            command=command,
            returncode=0,
            stdout=stdout,
            stderr="",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
    except _api.urllib.error.HTTPError as exc:
        body_text = _api._http_error_text(exc)
        rc = exc.code or 1
        if _api._is_payment_preflight_denial(rc, body_text):
            _api._cache_payment_preflight_denial(model, rc, body_text)
        _api._emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=rc,
            stdout="",
            stderr=body_text,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=rc, stdout="", stderr=body_text
        )
    except (_api.urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connection refused / DNS failure / socket timeout — no HTTP status. URLError
        # and TimeoutError are both OSError subclasses; the wide catch normalises any
        # transport-level failure to a dead-backend result instead of crashing. A socket
        # timeout is logged as a TIMEOUT (rc 124) to match the subprocess + gemini metric.
        err = f"{label} API request failed: {exc}"
        if _api._is_timeout_error(exc):
            _api._emit_rest_log(
                backend,
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return _api.ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        _api._emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )
    except (_api.json.JSONDecodeError, ValueError) as exc:
        # 2xx with a non-JSON / truncated body — the provider returned garbage. Treat
        # it as a failed call rather than letting the decode error escape.
        err = f"{label} API returned a malformed response: {exc}"
        _api._emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )


def _gemini_response_text(payload):
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    first = candidates[0] if isinstance(candidates, list) and candidates else {}
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    parts = parts if isinstance(parts, list) else []
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )
