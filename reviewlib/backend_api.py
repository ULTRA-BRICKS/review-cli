"""Backend api implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _zai_key(_api, /) -> str:
    key = _api._resolve_key(("ZAI_API_KEY", "ZHIPU_API_KEY"), "ZAI_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "ZAI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def review_zai(
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
    # api-only: a forced REVIEW_ZAI_MODE=cli is a config error, surfaced as a
    # dead-backend result instead of silently running the api path.
    # `round_no` is accepted (and forwarded) so the panel's uniform 6-arg dispatch
    # (panel.py: backend(model, prompt, diff, cwd, timeout, round_no)) does not raise
    # for these post-HYP-741 REST backends and the dashboard attributes the run to the
    # right brainstorm round.
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log — like review_gemini, these are real (failed) run attempts and must be
    # visible in the dashboard, never raise out of run_panel as an invisible internal 127.
    zai_model = (
        model.split(":", 1)[1]
        if ":" in model
        else _api.os.environ.get("ZAI_MODEL", _api.ZAI_DEFAULT_MODEL)
    )
    command = f"z.ai API {zai_model}"
    unpaid = _api.unpaid_provider_result(
        model, backend="z.ai", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        _api.resolve_backend_mode("zai", _api.ZAI_SUPPORTED_MODES, "api")
        key = _api._zai_key()
    except RuntimeError as exc:
        _api._emit_rest_log(
            "z.ai", command, round_no=round_no, returncode=1, stdout="", stderr=str(exc)
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    base_url = _api.os.environ.get("ZAI_BASE_URL", _api.ZAI_DEFAULT_BASE_URL)
    return _api._openai_compatible_request(
        model=model,
        api_model=zai_model,
        label="z.ai",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="z.ai",
        round_no=round_no,
    )


def _commandcode_key(_api, /) -> str:
    # ONLY COMMANDCODE_API_KEY. The key is a Command Code `user_...` token, NOT a
    # DeepSeek key — accepting DEEPSEEK_API_KEY here would silently POST a DeepSeek
    # credential to api.commandcode.ai (a different host), which both fails to auth
    # AND leaks the key cross-provider. So no alias key names: the canonical name is
    # the only one that resolves. (Codex P1, HYP-741.)
    key = _api._resolve_key(("COMMANDCODE_API_KEY",), "COMMANDCODE_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "COMMANDCODE_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def review_commandcode(
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
    # API-only: a forced REVIEW_COMMANDCODE_MODE=cli is a config error (there is no
    # commandcode CLI), surfaced as a dead-backend result, never a silent api POST.
    # `round_no` is accepted so the panel's uniform 6-arg dispatch does not raise for
    # this post-HYP-741 REST backend (see review_zai for the rationale).
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log (see review_zai) — a failed run must be visible in the dashboard, never
    # an invisible internal 127 raised out of run_panel.
    has_suffix = ":" in model
    env_model = _api.os.environ.get("COMMANDCODE_MODEL")
    cc_model = (
        model.split(":", 1)[1]
        if has_suffix
        else (env_model or _api.COMMANDCODE_DEFAULT_MODEL)
    )
    command = f"commandcode API {cc_model}"
    unpaid = _api.unpaid_provider_result(
        model, backend="commandcode", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        _api.resolve_backend_mode(
            "commandcode", _api.COMMANDCODE_SUPPORTED_MODES, "api"
        )
        key = _api._commandcode_key()
    except RuntimeError as exc:
        _api._emit_rest_log(
            "commandcode",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    preflight = _api.provider_preflight_result(
        model, backend="commandcode", command=command, round_no=round_no
    )
    if preflight is not None:
        return preflight
    base_url = (
        _api.os.environ.get("COMMANDCODE_BASE_URL") or _api.COMMANDCODE_DEFAULT_BASE_URL
    )
    return _api._openai_compatible_request(
        model=model,
        api_model=cc_model,
        label="commandcode",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="commandcode",
        round_no=round_no,
    )


def _openrouter_key(_api, /) -> str:
    # ONLY OPENROUTER_API_KEY. Like commandcode's `user_...` token, an OpenRouter key is a
    # provider-specific `sk-or-v1-...` credential; accepting a foreign alias (e.g. a raw
    # OpenAI/Anthropic key) would silently POST that credential to openrouter.ai — a wrong
    # host and a cross-provider key leak. So the canonical name is the only one that resolves.
    key = _api._resolve_key(("OPENROUTER_API_KEY",), "OPENROUTER_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "OPENROUTER_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def _openrouter_extra_headers(_api, /) -> dict:
    """OpenRouter's OPTIONAL leaderboard-attribution headers, built from env. Empty when
    unset, so the backend works with nothing configured (the headers only affect how the
    app appears on openrouter.ai rankings — never the review result). HTTP-Referer / X-Title
    are the documented header names; the env vars mirror them for discoverability.

    A value containing a control character (CR/LF/etc.) is DROPPED, not sent: http.client's
    putheader would raise a ValueError on a newline mid-send, turning a stray env value into
    a crash AND it would be a header-injection vector. Dropping the bad value keeps the
    backend on its "any failure is a graceful dead-backend, never a raw exception" contract
    and refuses to smuggle attacker/typo CRLF into the request."""
    headers: dict[str, str] = {}
    referer = _api.os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    if referer and _api._header_value_is_safe(referer):
        headers["HTTP-Referer"] = referer
    title = _api.os.environ.get("OPENROUTER_X_TITLE", "").strip()
    if title and _api._header_value_is_safe(title):
        headers["X-Title"] = title
    return headers


def _header_value_is_safe(_api, /, value: str) -> bool:
    """True iff `value` is safe to send as an HTTP header value, on two counts:

    * it is encodable as **latin-1** — http.client encodes header values as latin-1, so a
      non-encodable char (emoji, CJK, …) would raise UnicodeEncodeError mid-send; and
    * it carries **no control character** — C0 (0x00-0x1F, except tab), DEL (0x7F), or C1
      (0x80-0x9F) — which could break HTTP header framing (CR/LF injection) or be mangled.

    A value failing either is DROPPED by the caller, keeping an optional attribution header
    from ever crashing or corrupting the request. Printable ASCII + printable latin-1
    (e.g. accented letters, 0xA0-0xFF) + tab are allowed."""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return not any(
        (ord(ch) < 0x20 and ch != "\t") or 0x7F <= ord(ch) <= 0x9F for ch in value
    )


def review_openrouter(
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
    # API-only: a forced REVIEW_OPENROUTER_MODE=cli is a config error (no OpenRouter CLI),
    # surfaced as a dead-backend result, never a silent api POST. `round_no` is accepted so
    # the panel's uniform 6-arg dispatch does not raise (see review_zai for the rationale).
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log so a failed run is visible in the dashboard, not an invisible internal 127.
    # `split(":", 1)[1]` strips ONLY the `openrouter:` prefix, preserving both the slug's
    # `/` AND any trailing `:variant` colon (e.g. openrouter:anthropic/claude-3.5-sonnet:beta
    # → anthropic/claude-3.5-sonnet:beta). The suffix is `.strip()`ed and used ONLY when
    # non-empty: a bare `openrouter`, an EMPTY suffix (`openrouter:` / `openrouter: `) all fall
    # back to OPENROUTER_MODEL, then the auto-router default — never POST `"model": ""` (a
    # guaranteed 400) just because a stray colon was present.
    # Both env overrides are `.strip()`ed and used only when non-empty, symmetric with the
    # suffix discipline above: a whitespace-only OPENROUTER_MODEL / OPENROUTER_BASE_URL must
    # NOT win (it would POST `"model": "   "` / build a broken URL — the same 400 the empty
    # suffix guards against), so it falls through to the next source / the default.
    suffix = model.split(":", 1)[1].strip() if ":" in model else ""
    env_model = _api.os.environ.get("OPENROUTER_MODEL", "").strip()
    or_model = suffix or env_model or _api.OPENROUTER_DEFAULT_MODEL
    command = f"openrouter API {or_model}"
    unpaid = _api.unpaid_provider_result(
        model, backend="openrouter", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        _api.resolve_backend_mode("openrouter", _api.OPENROUTER_SUPPORTED_MODES, "api")
        key = _api._openrouter_key()
    except RuntimeError as exc:
        _api._emit_rest_log(
            "openrouter",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    base_url = (
        _api.os.environ.get("OPENROUTER_BASE_URL", "").strip()
        or _api.OPENROUTER_DEFAULT_BASE_URL
    )
    return _api._openai_compatible_request(
        model=model,
        api_model=or_model,
        label="openrouter",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="openrouter",
        round_no=round_no,
        extra_headers=_api._openrouter_extra_headers(),
    )


def _anthropic_api_config(_api, /) -> dict | None:
    """Resolve Anthropic-compatible API config (auth header + base url), or None.

    Mirrors the Anthropic SDK / claude CLI env surface so the SAME vars drive
    both review's API backend and any local claude CLI: ANTHROPIC_API_KEY (sent
    as ``x-api-key``) or ANTHROPIC_AUTH_TOKEN (sent as ``Authorization: Bearer``,
    for gateways / OAuth), plus ANTHROPIC_BASE_URL (default api.anthropic.com,
    point it at e.g. CommandCode). Keys also fall back to review's shared env
    files via _resolve_key. Returns None when no key is set, so the dispatcher
    can fall back to the CLI backend."""
    apikey = _api._resolve_key(("ANTHROPIC_API_KEY",), "ANTHROPIC_API_KEY")
    if apikey:
        auth = ("x-api-key", apikey)
    else:
        token = _api._resolve_key(("ANTHROPIC_AUTH_TOKEN",), "ANTHROPIC_AUTH_TOKEN")
        if not token:
            return None
        auth = ("Authorization", f"Bearer {token}")
    base = _api._anthropic_base_url_from_env() or "https://api.anthropic.com"
    return {"base": base, "auth": auth}


def _anthropic_base_url_from_env(_api, /) -> str | None:
    base = _api.os.environ.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    return base or None


def _base_url_hostname(_api, /, base_url: str) -> str:
    parsed = _api.urllib.parse.urlparse(base_url)
    if parsed.hostname:
        return parsed.hostname.lower()
    parsed = _api.urllib.parse.urlparse(f"//{base_url}")
    return (parsed.hostname or "").lower()


def _anthropic_gateway_provider(_api, /, base_url: str) -> str | None:
    host = _api._base_url_hostname(base_url)
    if host == "api.commandcode.ai":
        return "commandcode"
    return None


def review_claude_api(
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
    """Anthropic Messages API backend — works WITHOUT the claude CLI (needs only a
    key). POSTs to ``{ANTHROPIC_BASE_URL}/v1/messages``; the default base is
    Anthropic, but any Anthropic-compatible gateway works (e.g. CommandCode via
    ANTHROPIC_BASE_URL). ``cwd`` is unused — the API has no workspace, which is
    exactly why this variant runs where the CLI cannot.

    Like the other REST backends, this never goes through `_run_streamed`, so it emits
    its own dashboard sidecar log (under the canonical ``claude`` backend name, same as
    the CLI variant) on EVERY return path with ``round_no`` threaded — else a claude
    API-mode run would be invisible to the dashboard and missing from stats / brainstorm
    round attribution (codex P2)."""
    prompt = _api._prompt_with_effort(prompt, effort)
    claude_model = _api._claude_api_model(model)
    cfg = _api._anthropic_api_config()
    command = f"Anthropic API {claude_model}"
    started = _api.datetime.now(_api.timezone.utc)
    gateway_provider = (
        _api._anthropic_gateway_provider(cfg["base"]) if cfg is not None else None
    )
    unpaid = _api.unpaid_provider_result(
        model,
        backend="claude",
        command=command,
        round_no=round_no,
        started=started,
        provider=gateway_provider,
    )
    if unpaid is not None:
        return unpaid
    if cfg is None:
        stderr = (
            "claude API mode: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN configured"
        )
        _api._emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=stderr,
            started=started,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=stderr
        )
    try:
        max_tokens = int(_api.os.environ.get("ANTHROPIC_MAX_TOKENS", "16000"))
    except ValueError:
        max_tokens = 16000
    body = {
        "model": claude_model,
        "max_tokens": max_tokens,
        "system": _api._CLAUDE_REVIEW_SYSTEM,
        "messages": [{"role": "user", "content": _api._payload(prompt, diff)}],
    }
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": _api._ANTHROPIC_UA,
        cfg["auth"][0]: cfg["auth"][1],
    }
    data = _api.json.dumps(body).encode("utf-8")
    url = cfg["base"] + "/v1/messages"
    req = _api.urllib.request.Request(url, data=data, method="POST", headers=headers)
    command = f"Anthropic API {claude_model} @ {cfg['base']}"
    try:
        with _api.urllib.request.urlopen(req, timeout=timeout) as response:
            payload = _api.json.loads(response.read().decode("utf-8"))
        parts = payload.get("content", []) if isinstance(payload, dict) else []
        text = "".join(
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        )
        usage = _api._usage_dict(payload, "usage")
        # Empty success is a failure for the panel/moderator fallback path.
        rc = 0 if text.strip() else 1
        prompt_tokens, output_tokens = _api._validated_usage_pair(
            usage.get("input_tokens"), usage.get("output_tokens"), completed=rc == 0
        )
        # A footer-only stdout on the empty-content failure would read as review
        # content to `result_is_usable()`; keep the failed result's stdout empty
        # like the other REST paths (tokens still ride on the result fields).
        stdout = (
            text.strip()
            + f"\n\ninput_tokens={prompt_tokens} output_tokens={output_tokens}\n"
            if rc == 0
            else ""
        )
        _api._emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=rc,
            stdout=stdout,
            stderr="",
            started=started,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        return _api.ReviewResult(
            model=model,
            command=command,
            returncode=rc,
            stdout=stdout,
            stderr="",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
    except _api.urllib.error.HTTPError as exc:
        body_text = _api._http_error_text(exc)
        rc = exc.code or 1
        _api._emit_rest_log(
            "claude",
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
    except _api.urllib.error.URLError as exc:
        return _claude_api_transport_error(
            _api, model, command, round_no, started, timeout, exc, malformed=False
        )
    except (ValueError, OSError) as exc:
        return _claude_api_transport_error(
            _api, model, command, round_no, started, timeout, exc, malformed=True
        )


def _claude_api_transport_error(
    _api, model, command, round_no, started, timeout, exc, *, malformed
):
    timed_out = _api._is_timeout_error(exc)
    if timed_out:
        error = str(exc)
        _api._emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=124,
            stdout="",
            stderr=error,
            started=started,
            timed_out=True,
            timeout_secs=timeout,
        )
        return _api.ReviewResult(
            model=model, command=command, returncode=124, stdout="", stderr=error
        )
    error = (
        f"claude API: malformed or unreadable response: {exc}"
        if malformed
        else str(exc)
    )
    _api._emit_rest_log(
        "claude",
        command,
        round_no=round_no,
        returncode=1,
        stdout="",
        stderr=error,
        started=started,
    )
    return _api.ReviewResult(
        model=model, command=command, returncode=1, stdout="", stderr=error
    )
