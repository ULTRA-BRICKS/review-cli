"""Backend provider implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def _peel_oc_transport(_api, /, lowered: str) -> str | None:
    """The opencode `provider/model` selector under an `oc:`/`opencode:` transport prefix
    of an already-lower-cased id, or None when the id carries no transport prefix. The ONE
    place the prefix list lives — `effective_provider` and `default_routes_live` both peel
    through it, so the two can never disagree on what counts as the agentic transport."""
    for prefix in _api._OC_TRANSPORT_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return None


def _oc_selector_is_well_formed(_api, /, selector: str, provider: str) -> bool:
    """True iff an opencode selector (the part after `oc:`) is the exact `provider/model`
    shape opencode's `-m` wants: a slash, the segment before the FIRST slash canonicalising
    to the effective `provider` (so a colon-contaminated `xai:grok/model` — whose provider
    peels as `xai` on the colon — or `zai:/glm` fails, while `gemini-api/...` still matches
    its canonical `gemini`), no whitespace anywhere, and no empty `/`-separated segment
    (rejects `zai//glm-5.3`, an interior `deepseek//v4` and a trailing `grok-4.5/` alike).

    Whitespace is REJECTED, never canonicalised away (review round 2, GH-165, Codex): the
    selector is forwarded to `opencode -m` verbatim by `review_opencode`, so a
    `oc: xai/grok-4.5` or `oc:xai /grok-4.5` default that this guard stripped into shape
    would pass the anti-rot CI check and then fail at dispatch."""
    if "/" not in selector or any(ch.isspace() for ch in selector):
        return False
    segments = selector.split("/")
    if not all(segments):
        return False
    return _api._canonical_provider(segments[0]) == provider


def effective_provider(_api, /, model: str) -> str:
    """The EFFECTIVE underlying provider of a model id, peeling the `oc:`/`opencode:`
    AGENTIC-transport prefix so the real provider — not "opencode" — is what's checked.

    `oc:commandcode/moonshotai/Kimi-K3` -> `commandcode`; `oc:fireworks/x` ->
    `fireworks`; `zai:glm-5.3` -> `zai`; a bare `codex` -> `codex`. Lower-cased. The #25
    guard needs this because an `oc:` seat is just a transport: the dead-provider check must
    look THROUGH the `oc:` prefix at the provider underneath — otherwise a dead
    `oc:fireworks/...` default reads as the opencode transport and the rot slips past."""
    lowered = model.lower()
    peeled = _api._peel_oc_transport(lowered)
    if peeled is not None:
        lowered = peeled
    if lowered.startswith("fable"):
        return "claude"
    # The provider is the first segment before either a `:` (keyed-HTTP `provider:model`)
    # or a `/` (opencode `provider/model`), whichever comes first.
    for sep in (":", "/"):
        if sep in lowered:
            lowered = lowered.split(sep, 1)[0]
    return _api._canonical_provider(lowered)


def _canonical_provider(_api, /, provider: str) -> str:
    return _api._PROVIDER_ALIASES.get(
        provider.strip().lower(), provider.strip().lower()
    )


def _parse_provider_names(_api, /, raw: object) -> frozenset[str]:
    """Normalize config/env provider lists (`["commandcode"]` or "a,b")."""
    if raw is None:
        return frozenset()
    parts: list[str] = []
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        for item in raw:
            if isinstance(item, str):
                parts.extend(item.split(","))
    return frozenset(_api._canonical_provider(part) for part in parts if part.strip())


def configure_unpaid_providers(_api, /, raw: object) -> None:
    """Set per-process providers whose billing/entitlement is currently unavailable.

    `reviewlib.backends` intentionally does not import the YAML config loader. The CLI calls
    this after `load_config()`; tests and non-CLI callers can use REVIEW_UNPAID_PROVIDERS.
    """
    pass  # Mutable state is owned by the public backend module.
    _api._CONFIG_UNPAID_PROVIDERS = _api._parse_provider_names(raw)


def unpaid_providers(_api, /) -> frozenset[str]:
    """Providers skipped before dispatch because their subscription/billing is unavailable."""
    return _api._CONFIG_UNPAID_PROVIDERS | _api._parse_provider_names(
        _api.os.environ.get(_api._UNPAID_PROVIDERS_ENV)
    )


def provider_marked_unpaid(_api, /, model: str) -> bool:
    """True when `model` routes through a provider disabled by the payment/entitlement list."""
    return _api.effective_provider(model) in _api.unpaid_providers()


def _runtime_unpaid_provider(_api, /, model: str) -> str | None:
    provider = (
        _api._claude_runtime_gateway_provider()
        if _api.resolve_backend(model) is _api.review_claude
        else None
    )
    return _api._matched_unpaid_provider(model, provider)


def runtime_provider_marked_unpaid(_api, /, model: str) -> bool:
    """True when a backend invocation for `model` would use a disabled provider."""
    return _api._runtime_unpaid_provider(model) is not None


def runtime_unpaid_provider_error(_api, /, model: str) -> str:
    return _api.unpaid_provider_error(model, _api._runtime_unpaid_provider(model))


def _matched_unpaid_provider(_api, /, model: str, provider: str | None) -> str | None:
    providers = []
    if provider:
        providers.append(_api._canonical_provider(provider))
    providers.append(_api.effective_provider(model))
    disabled = _api.unpaid_providers()
    for candidate in providers:
        if candidate in disabled:
            return candidate
    return None


def unpaid_provider_error(_api, /, model: str, provider: str | None) -> str:
    provider = _api._matched_unpaid_provider(
        model, provider
    ) or _api.effective_provider(model)
    return (
        f"provider '{provider}' is marked unpaid/disabled "
        f"({_api._UNPAID_PROVIDERS_ENV} or config.yaml unpaid_providers); skipping {model}"
    )


def unpaid_provider_result(
    _api,
    /,
    model: str,
    *,
    backend: str,
    command: str,
    round_no: int,
    started: _shared.datetime | None,
    provider: str | None,
) -> _shared.ReviewResult | None:
    """Return a skipped result for an unpaid provider, emitting the usual live-log sidecar."""
    provider = _api._matched_unpaid_provider(model, provider)
    if provider is None:
        return None
    stderr = _api.unpaid_provider_error(model, provider)
    try:
        _api._emit_rest_log(
            backend,
            command,
            stdout="",
            stderr=stderr,
            returncode=1,
            round_no=round_no,
            started=started,
        )
    except OSError as exc:
        print(
            f"[review-cli] unpaid-provider sidecar log skipped: {exc}",
            file=_api.sys.stderr,
            flush=True,
        )
    return _api.ReviewResult(
        model=model, command=command, returncode=1, stdout="", stderr=stderr
    )


def _provider_preflight_url(_api, /, provider: str) -> str | None:
    if provider == "commandcode":
        base = (
            _api.os.environ.get("COMMANDCODE_BASE_URL")
            or _api.COMMANDCODE_DEFAULT_BASE_URL
        )
    elif provider == "fireworks":
        base = (
            _api.os.environ.get("FIREWORKS_BASE_URL")
            or _api._FIREWORKS_DEFAULT_BASE_URL
        )
    else:
        return None
    return base.rstrip("/") + "/models"


def _oc_auth_provider_key(_api, /, provider: str) -> str | None:
    try:
        data = _api.json.loads(_api._oc_auth_file().read_text())
        value = data.get(provider, {}).get("key", "")
        return value if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _oc_config_provider_key(_api, /, provider: str) -> str | None:
    key, _base = _api._oc_config_provider_credentials(provider)
    return key


def _oc_config_provider_credentials(
    _api, /, provider: str
) -> tuple[str | None, str | None]:
    try:
        data = _api.json.loads(_api._oc_config_file().read_text())
        opts = data.get("provider", {}).get(provider, {}).get("options", {})
        value = opts.get("apiKey", "")
        key = value if isinstance(value, str) and value.strip() else None
        base_value = (
            opts.get("baseURL") or opts.get("baseUrl") or opts.get("base_url") or ""
        )
        base = base_value.strip() if isinstance(base_value, str) else ""
        return key, (base or None)
    except Exception:  # noqa: BLE001
        return None, None


def _oc_env_provider_key(_api, /, provider: str) -> str | None:
    for var in _api._OC_PROVIDER_ENV_VARS.get(provider, ()):
        value = _api.os.environ.get(var, "").strip()
        if value:
            return value
    return None


def _oc_provider_key(_api, /, provider: str) -> str | None:
    return (
        _api._oc_auth_provider_key(provider)
        or _api._oc_config_provider_key(provider)
        or _api._oc_env_provider_key(provider)
    )


def _models_url_from_base(_api, /, base_url: str | None, provider: str) -> str | None:
    if not base_url:
        return _api._provider_preflight_url(provider)
    return base_url.rstrip("/") + "/models"


def _payment_preflight_credentials(
    _api, /, model: str, provider: str
) -> tuple[str | None, str | None]:
    if provider == "commandcode":
        if model.lower().startswith(("oc:", "opencode:")):
            return _opencode_payment_credentials(_api, provider)
        try:
            return _api._commandcode_key(), _api._provider_preflight_url(provider)
        except RuntimeError:
            return None, None
    if provider == "fireworks":
        return _opencode_payment_credentials(_api, provider)
    return None, None


def _payment_preflight_cache_key(
    _api, /, provider: str, url: str, key: str, scope: str
) -> tuple[str, str, str, str]:
    key_hash = _api.hashlib.sha256(key.encode("utf-8")).hexdigest()
    return (provider, url, key_hash, scope)


def _is_payment_preflight_denial(_api, /, code: int | None, body: str) -> bool:
    if code == 402:
        return True
    lowered = body.lower()
    return code in _api._PAYMENT_PREFLIGHT_MARKER_CODES and any(
        marker in lowered for marker in _api._PAYMENT_PREFLIGHT_MARKERS
    )


def _payment_denial_cache_scope(
    _api, /, model: str, code: int | None, body: str
) -> str:
    if code == 402:
        return _api._PAYMENT_PREFLIGHT_PROVIDER_SCOPE
    lowered = body.lower()
    if any(
        marker in lowered for marker in _api._PAYMENT_PREFLIGHT_PROVIDER_WIDE_MARKERS
    ):
        return _api._PAYMENT_PREFLIGHT_PROVIDER_SCOPE
    return model


def _payment_preflight_reason(
    _api, /, provider: str, model: str, code: int | None
) -> str:
    suffix = f"HTTP {code}" if code is not None else "payment/availability denial"
    return f"provider '{provider}' failed payment/availability preflight ({suffix}); skipping {model}"


def _cache_payment_preflight_denial(
    _api, /, model: str, code: int | None, body: str
) -> None:
    provider = _api.effective_provider(model)
    if provider not in _api._PAYMENT_PREFLIGHT_PROVIDERS:
        return
    key, url = _api._payment_preflight_credentials(model, provider)
    if not key or not url:
        return
    cache_key = _api._payment_preflight_cache_key(
        provider, url, key, _api._payment_denial_cache_scope(model, code, body)
    )
    with _api._PAYMENT_PREFLIGHT_CACHE_LOCK:
        _api._PAYMENT_PREFLIGHT_CACHE[cache_key] = (True, code)


def _cached_payment_preflight_result(
    _api,
    /,
    provider: str,
    model: str,
    url: str,
    key: str,
) -> tuple[bool, int | None] | None:
    with _api._PAYMENT_PREFLIGHT_CACHE_LOCK:
        return _api._PAYMENT_PREFLIGHT_CACHE.get(
            _api._payment_preflight_cache_key(provider, url, key, model)
        ) or _api._PAYMENT_PREFLIGHT_CACHE.get(
            _api._payment_preflight_cache_key(provider, url, key)
        )


def cached_payment_preflight_unavailable_reason(_api, /, model: str) -> str | None:
    """Return a cached payment/entitlement denial without performing network I/O."""
    provider = _api.effective_provider(model)
    if provider not in _api._PAYMENT_PREFLIGHT_PROVIDERS:
        return None
    key, url = _api._payment_preflight_credentials(model, provider)
    if not key or not url:
        return None
    cached = _api._cached_payment_preflight_result(provider, model, url, key)
    if cached is None:
        return None
    denied, code = cached
    return _api._payment_preflight_reason(provider, model, code) if denied else None


def _provider_payment_preflight_unavailable_reason(_api, /, model: str) -> str | None:
    """Return a skip reason when a provider key exists but payment/entitlement is unavailable.

    This is deliberately narrow: only providers with known cheap `/models` entitlement probes
    are checked. Network/DNS/transient failures are conservative-allow so an offline probe
    does not make a working provider disappear; explicit payment/auth/disabled responses are
    conservative-deny so they do not launch chat/opencode calls.
    """
    provider = _api.effective_provider(model)
    if provider not in _api._PAYMENT_PREFLIGHT_PROVIDERS:
        return None
    key, url = _api._payment_preflight_credentials(model, provider)
    if not key or not url:
        return None
    cached = _api._cached_payment_preflight_result(provider, model, url, key)
    if cached is not None:
        denied, code = cached
        return _api._payment_preflight_reason(provider, model, code) if denied else None
    req = _api.urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "review-cli/0.1",
        },
    )
    try:
        with _api.urllib.request.urlopen(
            req, timeout=_api._PROVIDER_PREFLIGHT_TIMEOUT_SECONDS
        ):
            return _record_successful_payment_probe(_api, provider, model, url, key)
    except _api.urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if _api._is_payment_preflight_denial(exc.code, body):
            with _api._PAYMENT_PREFLIGHT_CACHE_LOCK:
                _api._PAYMENT_PREFLIGHT_CACHE[
                    _api._payment_preflight_cache_key(provider, url, key)
                ] = (True, exc.code)
            return _api._payment_preflight_reason(provider, model, exc.code)
        return None
    except (_api.urllib.error.URLError, TimeoutError, OSError):
        return None
    except Exception:  # noqa: BLE001
        # Test fakes and unusual urllib failures that are not explicit entitlement
        # denials should not make a provider disappear. Only the HTTP payment/auth
        # shapes above are authoritative enough to skip a seat.
        return None


def provider_preflight_result(
    _api,
    /,
    model: str,
    *,
    backend: str,
    command: str,
    round_no: int,
    started: _shared.datetime | None,
) -> _shared.ReviewResult | None:
    reason = _api._provider_payment_preflight_unavailable_reason(model)
    if reason is None:
        return None
    try:
        _api._emit_rest_log(
            backend,
            command,
            stdout="",
            stderr=reason,
            returncode=1,
            round_no=round_no,
            started=started,
        )
    except OSError as exc:
        print(
            f"[review-cli] provider-preflight sidecar log skipped: {exc}",
            file=_api.sys.stderr,
            flush=True,
        )
    return _api.ReviewResult(
        model=model, command=command, returncode=1, stdout="", stderr=reason
    )


def _oc_auth_file(_api, /) -> _shared.Path:
    """Path to opencode's auth.json.  Injectable via OC_AUTH_FILE for tests."""
    env = _api.os.environ.get(_api._OC_AUTH_FILE_ENV, "").strip()
    if env:
        return _api.Path(env)
    xdg = _api.os.environ.get("XDG_DATA_HOME", "").strip()
    base = _api.Path(xdg) if xdg else _api.Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


def _oc_config_file(_api, /) -> _shared.Path:
    """Path to opencode's opencode.json.  Injectable via OC_CONFIG_FILE for tests."""
    env = _api.os.environ.get(_api._OC_CONFIG_FILE_ENV, "").strip()
    if env:
        return _api.Path(env)
    xdg = _api.os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = _api.Path(xdg) if xdg else _api.Path.home() / ".config"
    return base / "opencode" / "opencode.json"


def _oc_auth_has_provider(_api, /, provider: str) -> bool:
    """True iff opencode's auth.json carries a usable credential for *provider*.

    Recognised shapes (`opencode providers login`): keyed API auth `{"type": "api",
    "key": ...}` (deepseek/commandcode/zai) and OAuth `{"type": "oauth", "access": ...,
    "refresh": ..., "expires": ...}` (xai — `opencode providers list` shows "xAI | oauth").
    Discriminates by `type`: `key` for "api"; `access` OR `refresh` for "oauth" (opencode
    auto-refreshes an expired `access` from `refresh`). A whitespace-only value is not a
    credential. An unrecognised `type` falls back to "any non-blank string field that is
    not in `_OC_AUTH_METADATA_FIELDS`", so a genuinely new auth shape still counts rather
    than silently dropping the seat; drift INSIDE a known type (opencode renaming `key`)
    hits the strict branch and is NOT covered by the fallback. Never proves the token is
    unexpired — opencode's own auth layer owns expiry/refresh at call time."""

    def _nonblank(value: object) -> bool:
        return isinstance(value, str) and value.strip() != ""

    # Everything that assumes a dict shape stays INSIDE this try: a corrupted auth.json
    # can carry a truthy NON-dict value for a provider (`{"xai": "raw-token"}`, a list, a
    # number) — `.get()`/`.items()` on that raises AttributeError, which must read as "no
    # credential", not abort board selection.
    try:
        entry = _api.json.loads(_api._oc_auth_file().read_text()).get(provider) or {}
        if not isinstance(entry, dict) or not entry:
            return False
        auth_type = entry.get("type")
        if auth_type == "api":
            return _nonblank(entry.get("key"))
        if auth_type == "oauth":
            return _nonblank(entry.get("access")) or _nonblank(entry.get("refresh"))
        return any(
            _nonblank(v)
            for k, v in entry.items()
            if k not in _api._OC_AUTH_METADATA_FIELDS
        )
    except Exception:  # noqa: BLE001
        return False


def _oc_config_has_provider_key(_api, /, provider: str) -> bool:
    """True iff opencode.json carries an inline options.apiKey for *provider*."""
    try:
        data = _api.json.loads(_api._oc_config_file().read_text())
        opts = data.get("provider", {}).get(provider, {}).get("options", {})
        return bool(opts.get("apiKey", ""))
    except Exception:  # noqa: BLE001
        return False


def _oc_env_has_provider_key(_api, /, provider: str) -> bool:
    """True iff a known env var for *provider* is set and non-empty."""
    return any(
        _api.os.environ.get(var, "").strip()
        for var in _api._OC_PROVIDER_ENV_VARS.get(provider, ())
    )


def _oc_provider_auth_available(_api, /, provider: str) -> bool:
    """True iff opencode has credentials for *provider* from any source.

    Checked in order: local (no-key) → auth.json → opencode.json → env vars.
    For unknown providers (not in _OC_PROVIDER_ENV_VARS and not local) we fall
    back to True — opencode may carry them through its own mechanism and we have
    no signal to reject them.
    """
    if provider in _api._OC_LOCAL_PROVIDERS:
        return True
    if _api._oc_auth_has_provider(provider):
        return True
    if _api._oc_config_has_provider_key(provider):
        return True
    if _api._oc_env_has_provider_key(provider):
        return True
    # For a KNOWN provider where every credential source came up empty → unavailable.
    # For an UNKNOWN provider → conservative True (no false negatives).
    return provider not in _api._OC_PROVIDER_ENV_VARS


def _oc_provider_from_model(_api, /, model: str) -> str | None:
    """Extract the underlying provider from an oc:/opencode: model string.

    Returns None for a bare 'opencode' (no provider-scoped prefix — binary check only).
    'oc:anthropic/claude-3-5-sonnet' → 'anthropic';
    'opencode:deepseek/deepseek-v3' → 'deepseek';
    'opencode' → None.
    """
    lowered = model.lower()
    if not lowered.startswith(("oc:", "opencode:")):
        return None
    return _api.effective_provider(lowered)


def default_routes_live(_api, /, model: str) -> bool:
    """True iff `model` is safe to ship as a DEFAULT (flat panel or board seat): the FULL id
    takes a real named route AND its effective provider is not known-dead.

    This is the #25 anti-rot guard's single check. It validates the WHOLE id against the
    SAME matcher the dispatcher uses, not just the collapsed provider token (codex review):
      * `_match_named_backend(model)` must be non-None — the full id resolves to a named
        backend, NOT `resolve_backend`'s opencode catch-all. This is the real test: it
        mirrors what `resolve_backend` does (minus the fallthrough), so a default whose full
        id only the catch-all would accept fails. This is why checking the COLLAPSED provider
        is not enough: `gemini-api:gemini-2.5-flash` collapses to the named token
        `gemini-api`, but `resolve_backend` matches gemini only as the bare `gemini-api` or a
        `gemini:` prefix — the `gemini-api:` form falls through to opencode, so the full-id
        check is what catches it (the silent-rot #25 is about).
      * For an `oc:`/`opencode:` AGENTIC id the matcher returns `review_opencode` for ANY
        under-provider, so the full-id check alone can't see a dead/typo'd provider beneath
        the transport. So the provider UNDER the transport (`effective_provider`) must ALSO
        name a backend (rejects `oc:comandcode/...`) — checked only when a transport prefix
        is present, since for a flat id the full-id check already covers it.
      * `effective_provider(model)` must not be in `_DEAD_PROVIDERS` — the forward-looking
        half: when a CURRENTLY named provider (e.g. `commandcode`) is suspended, add it here
        and its defaults trip immediately, without also editing `_match_named_backend`.
    It checks real named routing + a curated dead-provider denylist, NOT live network
    reachability — a probe would need keys and can't run in CI.

    CONTRACT + scope: enforced ONLY by the CI guard test
    (`test_every_default_model_routes_live`), not at runtime — `resolve_backend` still sends
    an unknown id to opencode (back-compat). So the guard is the single line of defense, and
    it constrains DEFAULTS to ids `_match_named_backend` resolves to a named backend: an
    opencode-only provider valid at runtime but not a named branch gets a (false) `False`
    here. That is intentional — every shipped default routes through a named provider today,
    and adding one on a new provider means giving it a named branch (or, for a provider that
    is legitimately agentic-only with no bare/colon-form route — e.g. xai/GROK_SEAT — adding
    it to `_AGENTIC_ONLY_PROVIDERS` instead of a bare-token branch that would expose a
    broken invocation shape). Every `oc:` id, exempt or not, must also be a well-formed
    `provider/model` selector (`_oc_selector_is_well_formed`)."""
    # Lowercase ONCE and use it for every check, so the guard mirrors `resolve_backend`
    # (which dispatches on `model.lower()`) exactly — a mixed-case id can't pass the guard
    # while routing differently at runtime (codex review of #49).
    lowered = model.lower()
    if _api._match_named_backend(lowered) is None:
        return False
    provider = _api.effective_provider(lowered)
    selector = _api._peel_oc_transport(lowered)
    if selector is not None:
        # Agentic transport: the full id matched only the opencode branch. Every `oc:` id
        # — named provider or not — must be the exact `provider/model` selector opencode's
        # CLI wants (an `oc:zai/` or `oc:zai//glm-5.3` default would otherwise pass the
        # named-provider half and rot at runtime), AND the provider under the transport
        # must itself be a named backend or a documented agentic-only one.
        if not _api._oc_selector_is_well_formed(selector, provider):
            return False
        if (
            _api._match_named_backend(provider) is None
            and provider not in _api._AGENTIC_ONLY_PROVIDERS
        ):
            return False
    return provider not in _api._DEAD_PROVIDERS


def resolve_backend(_api, /, model: str) -> _shared.Callable[..., _shared.ReviewResult]:
    # TEST-ONLY: with REVIEW_FAKE_BACKEND set, every model routes to the deterministic
    # in-process fake (no network/CLI). Unset (the default) -> real backends, unchanged.
    if _api._fake_backend_enabled():
        return _api.review_fake
    # Catch-all: an UNRECOGNIZED id still runs via opencode (back-compat with arbitrary
    # `provider/model` selectors). The #25 default-model guard (`default_routes_live`)
    # instead checks each default's under-transport provider is a named backend and not in
    # the dead-provider denylist, so a stale default can't ride this fallthrough silently.
    return _api._match_named_backend(model.lower()) or _api.review_opencode


def provider_route_name(_api, /, model: str) -> str:
    """Canonical backend-route name for a model id, reusing `resolve_backend`.

    Maps a model string to the name of the backend it actually runs on — `codex`,
    `claude`, `gemini`, `opencode` (incl. every `oc:`/unknown-fallthrough seat),
    `commandcode`, `zai`, `openrouter`. This is the route KEY a run-scoped `--effort`
    override matches on (config.EffortOverride) and the same mapping the visual path uses
    (`features/visual/vision_client._route_name`)."""
    return {
        _api.review_claude: "claude",
        _api.review_gemini: "gemini",
        _api.review_codex: "codex",
        _api.review_opencode: "opencode",
        _api.review_omp: "omp",
        _api.review_commandcode: "commandcode",
        _api.review_zai: "zai",
        _api.review_openrouter: "openrouter",
    }.get(_api.resolve_backend(model), "opencode")


def is_known_backend_token(_api, /, token: str) -> bool:
    """Whether a bare token names a backend review RECOGNISES — as opposed to falling
    through `resolve_backend`'s opencode catch-all. opencode itself (`opencode`/`oc`, or an
    `opencode:`/`oc:` prefix) counts as recognised; an unknown word does NOT.

    Used to validate a user-supplied `--effort <provider>=<level>` token so a typo
    (`claud=high`) fails loudly instead of silently landing on the opencode route."""
    lowered = token.strip().lower()
    if lowered in ("opencode", "oc") or lowered.startswith(("opencode:", "oc:")):
        return True
    return _api._match_named_backend(lowered) is not None


def backend_available(_api, /, model: str) -> bool:
    """Cheap availability probe so moderator selection never picks a dead backend.

    Thin boolean over `backend_unavailable_reason` (the single source of truth for BOTH
    "is it live?" and "why not?") so the two can never drift."""
    return _api.backend_unavailable_reason(model) is None


def backend_unavailable_reason(
    _api, /, model: str, *, honor_exclusions: bool
) -> str | None:
    """Human-readable reason `model`'s backend is NOT live right now, or None if it is.

    `honor_exclusions=False` skips the stored availability exclusion (a quota/credit/login
    window or escalating cooldown): `review capacity-check` reads those records itself, with
    their typed cause, so a quota exclusion is evidence there rather than a probe failure.

    Mirrors `backend_available`'s probe exactly but surfaces WHY a seat is dead so the
    pool-selection guard (reviewlib.pool_guard) can annotate every printed model list with
    per-seat health (live / down + reason). Never raises: a probe RuntimeError (missing
    key/CLI) becomes its message; unpaid/paywalled providers get their dedicated reason."""
    # TEST-ONLY: under the fake backend EVERY model is reachable (it is faked in-process,
    # no key/CLI needed). Without this the panel/moderator selection would prune the faked
    # models as "unavailable" on a host lacking the real CLIs (e.g. CI), defeating the e2e.
    if _api._fake_backend_enabled():
        return None
    if _api.runtime_provider_marked_unpaid(model):
        return _api.runtime_unpaid_provider_error(model)
    preflight = _api.cached_payment_preflight_unavailable_reason(model)
    if preflight is not None:
        return preflight
    excluded = _api.availability_exclusion(model) if honor_exclusions else None
    if excluded is not None:
        return f"unavailable: {excluded.describe()}"
    backend = _api.resolve_backend(model)
    try:
        if backend is _api.review_gemini:
            _api._gemini_key()
            return None
        if backend is _api.review_zai:
            # Honor a forced mode: REVIEW_ZAI_MODE=cli makes review_zai a dead
            # backend, so it must NOT report available (resolve_backend_mode raises
            # on the unsupported mode → caught below → reason). Codex P2.
            _api.resolve_backend_mode("zai", _api.ZAI_SUPPORTED_MODES, "api")
            _api._zai_key()
            return None
        if backend is _api.review_commandcode:
            # Same as z.ai: a forced REVIEW_COMMANDCODE_MODE=cli is unrunnable, so the
            # probe must reflect that instead of selecting a backend that only fails.
            _api.resolve_backend_mode(
                "commandcode", _api.COMMANDCODE_SUPPORTED_MODES, "api"
            )
            _api._commandcode_key()
            return None
        if backend is _api.review_openrouter:
            # Same api-only contract: a forced REVIEW_OPENROUTER_MODE=cli is unrunnable, so
            # the probe reports unavailable rather than selecting a backend that only fails.
            _api.resolve_backend_mode(
                "openrouter", _api.OPENROUTER_SUPPORTED_MODES, "api"
            )
            _api._openrouter_key()
            return None
        if backend is _api.review_codex:
            _api._which("codex")
            return None
        if backend is _api.review_claude:
            return _claude_transport_reason(_api, model)
        if backend is _api.review_opencode:
            return _opencode_transport_reason(_api, model)
        if backend is _api.review_omp:
            return _omp_transport_reason(_api, model)
    except RuntimeError as exc:
        return str(exc)
    return "backend unavailable"


def _opencode_payment_credentials(_api, provider):
    configured, base = _api._oc_config_provider_credentials(provider)
    authenticated = _api._oc_auth_provider_key(provider)
    if authenticated:
        return authenticated, _api._models_url_from_base(base, provider)
    if configured:
        return configured, _api._models_url_from_base(base, provider)
    return _api._oc_env_provider_key(provider), _api._models_url_from_base(
        base, provider
    )


def _record_successful_payment_probe(_api, provider, model, url, key):
    with _api._PAYMENT_PREFLIGHT_CACHE_LOCK:
        provider_key = _api._payment_preflight_cache_key(provider, url, key)
        cached = _api._PAYMENT_PREFLIGHT_CACHE.get(
            _api._payment_preflight_cache_key(provider, url, key, model)
        ) or _api._PAYMENT_PREFLIGHT_CACHE.get(provider_key)
        if cached is not None and cached[0] is True:
            return _api._payment_preflight_reason(provider, model, cached[1])
        if cached is None or cached[0] is False:
            _api._PAYMENT_PREFLIGHT_CACHE[provider_key] = (False, None)
    return None


def _claude_transport_reason(_api, model):
    mode = _api.os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    if mode == "api":
        return (
            None
            if _api._claude_api_available_for_model(model)
            else "claude: no API key for this model (REVIEW_CLAUDE_MODE=api)"
        )
    if mode == "cli":
        return (
            _api._claude_cli_login_reason()
            if _api._have_claude_cli()
            else "claude: CLI not found on PATH (REVIEW_CLAUDE_MODE=cli)"
        )
    if _api._have_claude_cli():
        return _api._claude_cli_login_reason()
    if _api._claude_api_available_for_model(model):
        return None
    return "claude: no `claude` CLI on PATH and no API key"


def _opencode_transport_reason(_api, model):
    _api._which("opencode")
    provider = _api._oc_provider_from_model(model)
    if provider is None or _api._oc_provider_auth_available(provider):
        return None
    return (
        f"opencode: provider {provider!r} not authenticated (run `opencode auth login`)"
    )


def _omp_transport_reason(_api, model):
    _api.resolve_backend_mode("omp", _api.OMP_SUPPORTED_MODES, "cli")
    _api._omp_launcher()
    provider = _api._omp_provider_from_model(model)
    if _api._omp_selector_is_anthropic(
        model.split(":", 1)[1] if ":" in model else None
    ):
        provider = _api.omp_accounts.PROVIDER
    if _api._omp_auth_available(provider):
        return None
    if provider is None:
        return "omp: no usable credentials in ~/.omp/agent/agent.db (run `omp setup`)"
    return f"omp: provider {provider!r} not authenticated (run `omp setup`; check `omp token {provider}`)"
