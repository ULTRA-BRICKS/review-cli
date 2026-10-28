"""Backend omp registry implementation, with explicit public dependencies."""

from __future__ import annotations
from . import backends as _shared


def configure_omp_bin(_api, /, raw: object) -> None:
    """Set the per-process config.yaml `omp_bin:` value (a path; anything else unsets it)."""
    pass  # Mutable state is owned by the public backend module.
    _api._CONFIG_OMP_BIN = raw.strip() if isinstance(raw, str) and raw.strip() else None


def configure_omp_accounts(_api, /, raw: object) -> None:
    """Set the per-process config.yaml `omp_accounts:` value (validated on use)."""
    pass  # Mutable state is owned by the public backend module.
    _api._CONFIG_OMP_ACCOUNTS = raw


def _omp_account_policy(_api, /) -> _shared.omp_accounts.AccountPolicy:
    return _api.omp_accounts.account_policy(
        _api._CONFIG_OMP_ACCOUNTS, _api.os.environ, _api.account_budget.current()
    )


def _omp_profiles_policy_reason(_api, /, omp_model: str | None) -> str | None:
    """Why an `omp_model` seat on omp's Anthropic accounts must not run through
    `$REVIEW_OMP_CLAUDE_PROFILES`: an account policy is set, and a profile's account is
    not known. None when profiles are not set or no policy applies."""
    if _api._review_omp_claude_profiles() == [
        None
    ] or not _api._omp_selector_is_anthropic(omp_model):
        return None
    return _api.omp_accounts.profiles_reason(_api._omp_account_policy())


def _omp_launcher(_api, /) -> _shared.omp_accounts.OmpLauncher:
    """The omp launcher an omp seat runs, resolved from the REAL environment before the
    seat's sandbox exists (reviewlib.omp_accounts). Raises RuntimeError when there is no
    omp or it cannot start under the sandbox HOME, so the seat is reported unusable."""
    explicit, source = (
        _api.os.environ.get(_api._OMP_BIN_ENV, "").strip(),
        f"${_api._OMP_BIN_ENV}",
    )
    if not explicit and _api._CONFIG_OMP_BIN:
        explicit, source = _api._CONFIG_OMP_BIN, "config.yaml omp_bin"
    if explicit:
        argv0 = _api.os.path.abspath(_api.os.path.expanduser(explicit))
    else:
        argv0, source = _api._which("omp"), "PATH"
    launcher = _api.omp_accounts.launcher_for(argv0, source=source)
    problem = _api.omp_accounts.launcher_problem(launcher)
    if problem is not None:
        raise RuntimeError(problem)
    return launcher


def _omp_agent_dir(_api, /, profile: str | None) -> _shared.Path:
    """omp's agent storage dir for THIS process (verified against omp v17):
    `PI_CODING_AGENT_DIR` replaces it outright; else `OMP_PROFILE` (= `--profile`)
    isolates state under ~/.omp/profiles/<name>/agent; else the default ~/.omp/agent.
    Shared by the auth probe and the seat launch — the seat runs with a SANITIZED HOME
    and pins this explicitly so auth still resolves there.

    `profile`, when given explicitly, takes priority over BOTH env vars -- the caller
    is requesting a NAMED omp profile's agent dir directly (review-cli#168's
    omp-Claude-account rotation: `_review_omp_claude_profiles`/
    `$REVIEW_OMP_CLAUDE_PROFILES`), skipping the env-var precedence entirely. Every
    existing call site passes nothing, so `profile is None` stays byte-identical to
    before this parameter existed."""
    if profile is not None:
        return _api.Path.home() / ".omp" / "profiles" / profile / "agent"
    agent_dir = _api.os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        # Absolutize (after expanding any `~`): a RELATIVE override would resolve
        # against a different cwd in the seat's sandbox than in the probe's process
        # (review of #174).
        return _api.Path(_api.os.path.abspath(_api.os.path.expanduser(agent_dir)))
    env_profile = _api.os.environ.get("OMP_PROFILE", "").strip()
    if env_profile:
        return _api.Path.home() / ".omp" / "profiles" / env_profile / "agent"
    return _api.Path.home() / ".omp" / "agent"


def _omp_auth_db(_api, /) -> _shared.Path:
    """The auth db omp ITSELF would use for this process (`_omp_agent_dir` + agent.db;
    codex review of #174 — probing any OTHER db would misreport a runnable
    authenticated CLI as unauthenticated). The OMP_AUTH_DB test override wins first."""
    override = _api.os.environ.get(_api._OMP_AUTH_DB_ENV, "").strip()
    if override:
        return _api.Path(override)
    return _api._omp_agent_dir() / "agent.db"


def _omp_provider_from_model(_api, /, model: str) -> str | None:
    """Extract the provider an `omp:` seat authenticates against.

    Returns None for a bare 'omp' (binary check + any usable credential is enough).
    'omp:kimi-code/k3' → 'kimi-code'; 'omp:openai/gpt-5.5' → 'openai'.
    """
    lowered = model.lower()
    if not lowered.startswith("omp:"):
        return None
    selector = lowered[len("omp:") :]
    return selector.split("/", 1)[0] or None


def _omp_db_stamp(_api, /, db: _shared.Path) -> tuple[int, int] | None:
    """The (main, -wal) mtime pair for the auth db, or None when the db does not exist."""
    try:
        main = db.stat().st_mtime_ns
    except OSError:
        return None
    try:
        wal = db.with_name(db.name + "-wal").stat().st_mtime_ns
    except OSError:
        wal = 0
    return (main, wal)


def _omp_auth_probe(_api, /, db: _shared.Path) -> frozenset[str] | None:
    """One OFFLINE read of omp's auth db: the set of providers with a NON-disabled
    credential row (`auth_credentials.disabled_cause` marks dead ones), or None when the
    probe itself failed (missing/corrupt db, schema drift, a locked file, a python build
    without sqlite3) — callers treat None as 'unauthenticated' but must NOT cache it.
    omp keeps OAuth/API credentials in `auth_credentials` keyed by `provider`. The db is
    opened READ-ONLY via a file: URI so the probe can never create or mutate it. The
    imports stay inside the try (lazy AND failure-proof; repeat imports are a
    sys.modules lookup, so this costs nothing per probe)."""
    try:
        import sqlite3  # noqa: PLC0415
        from urllib.parse import quote  # noqa: PLC0415

        # Read-only offline probe: a long lock wait is pointless here, and a short one
        # bounds the worst case to N seats x 0.5s on a contended db (glm review of #174).
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = conn.execute(
                "SELECT DISTINCT provider FROM auth_credentials WHERE disabled_cause IS NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — any sqlite/schema failure reads as unauthenticated
        return None
    return frozenset(row[0] for row in rows if isinstance(row[0], str))


def _mtime_ns(_api, /, path: _shared.Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def omp_env_key(_api, /, provider: str | None) -> tuple[str, str] | None:
    """(env var, key) review-cli hands an omp seat on `provider`, resolved from the env
    or review-cli's .env files; None for a provider omp has no env key for, or no key."""
    spec = _api._OMP_ENV_KEY_PROVIDERS.get(provider or "")
    if spec is None:
        return None
    var, names = spec
    files = _api._env_key_files()
    stamp = (
        var,
        tuple(_api.os.environ.get(n) for n in names),
        tuple((str(p), _api._mtime_ns(p)) for p in files),
    )
    if stamp not in _api._OMP_ENV_KEY_CACHE:
        key = _api._resolve_key(names, var)
        _api._OMP_ENV_KEY_CACHE[stamp] = (var, key) if key else None
    return _api._OMP_ENV_KEY_CACHE[stamp]


def _omp_auth_available(_api, /, provider: str | None) -> bool:
    """Cheap OFFLINE omp auth probe: True iff the seat's provider has a usable
    credential (or ANY provider does, for a bare `omp` seat) — a stored one, else for an
    env-key provider a key review-cli passes the seat (`omp_env_key`, which reads the
    small .env only when no stored credential answered)."""
    if _api._omp_stored_auth(provider):
        return True
    return provider is not None and _api.omp_env_key(provider) is not None


def _omp_stored_auth(_api, /, provider: str | None) -> bool:
    """True iff omp's auth db holds a usable credential for `provider` (any provider
    for None). One stamp miss hydrates every live provider in a single db read
    (`_OMP_AUTH_CACHE`), so probing N omp seats costs one db read per board pass; see
    `_omp_auth_probe` for the read itself."""
    db = _api._omp_auth_db()
    stamp = _api._omp_db_stamp(db)
    if stamp is None:
        return False
    key = str(db)
    entry = _api._OMP_AUTH_CACHE.get(key)
    if entry is None or entry[0] != stamp:
        live = _api._omp_auth_probe(db)
        if live is None:
            return False  # probe failure — deliberately NOT cached (retry next time)
        entry = (stamp, live)
        _api._OMP_AUTH_CACHE[key] = entry
    providers = entry[1]
    return bool(providers) if provider is None else provider in providers
