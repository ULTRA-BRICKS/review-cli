"""Every login review-cli's seats could run on, per harness, for `review seats`.

WHY THIS EXISTS (review-cli#646, Alex tg#22608: "review-cli должен находить все акки в
cc, codex, omp и других харнесах"): `review seats` listed only registered
`~/.claude-accounts` dirs and omp's Anthropic accounts. The Claude Code login a plain
`claude:*` seat uses, the Codex CLI login, omp's non-Anthropic credentials, opencode's
providers and the API keys in review-cli's own .env were invisible, so a seat reported
"not authenticated" left the user digging through five config dirs to find out whether
an account existed at all.

Read-only: each source is a local file read or `claude auth status` (a local status
call, no model). Only identities and kinds are reported -- an email, a plan, "API key",
a credential count -- never a token or key. Paths follow each harness's own overrides
($CLAUDE_CONFIG_DIR via the claude CLI, $CODEX_HOME, omp's agent dir, $XDG_DATA_HOME).

Accessed via: reviewlib.cli's seat report (`review seats|status|doctor`, text + --json).
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from . import backends, claude_accounts


@dataclass(frozen=True)
class HarnessAccount:
    """One login a harness holds. `account` is who (an email) or what ("API key",
    "2 credentials"); `detail` is the plan / login state / where it was found."""

    harness: str
    provider: str
    account: str
    detail: str


# review-cli's own API keys (the diff-only REST backends; z.ai's also feeds omp:zai
# seats, review-cli#646): (provider, the backend token whose seats use it, the names
# `_resolve_key` accepts for it, primary first) — one entry per separate resolution.
_REVIEW_CLI_KEYS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("anthropic", "claude", ("ANTHROPIC_API_KEY",)),
    ("anthropic", "claude", ("ANTHROPIC_AUTH_TOKEN",)),
    ("gemini", "gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("zai", "zai", ("ZAI_API_KEY", "ZHIPU_API_KEY")),
    ("commandcode", "commandcode", ("COMMANDCODE_API_KEY",)),
    ("openrouter", "openrouter", ("OPENROUTER_API_KEY",)),
)


def _claude_code() -> list[HarnessAccount]:
    registry = claude_accounts.list_accounts()
    if registry:
        # each one's login state is already in the CLAUDE CLI ACCOUNTS section
        return [
            HarnessAccount(
                "claude-code",
                "anthropic",
                f"{len(registry)} registered",
                f"{claude_accounts.accounts_root()}; listed under CLAUDE CLI ACCOUNTS",
            )
        ]
    status = claude_accounts.auth_status(None)
    if status is None:
        return [
            HarnessAccount(
                "claude-code",
                "anthropic",
                "(unknown)",
                "`claude auth status` did not answer (no claude CLI?)",
            )
        ]
    directory = status.get("configDirectory") or "default config dir"
    if status.get("loggedIn") is not True:
        return [
            HarnessAccount(
                "claude-code",
                "anthropic",
                "(none)",
                f"logged out ({directory}) -- log in: claude auth login",
            )
        ]
    email = status.get("email")
    plan = status.get("subscriptionType") or status.get("authMethod") or "?"
    return [
        HarnessAccount(
            "claude-code",
            "anthropic",
            email if isinstance(email, str) and email else "(no email)",
            f"{plan}, logged in ({directory})",
        )
    ]


def _jwt_claims(token: object) -> dict:
    """The payload of an unverified JWT (identity claims only), {} when not one."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _codex() -> list[HarnessAccount]:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    path = home / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return [
            HarnessAccount("codex", "openai", "(none)", f"no login ({path} missing)")
        ]
    except ValueError:
        return [HarnessAccount("codex", "openai", "(unknown)", f"{path} is not JSON")]
    if not isinstance(data, dict):
        return [
            HarnessAccount("codex", "openai", "(unknown)", f"{path} is not an object")
        ]
    found = []
    tokens = data.get("tokens")
    claims = _jwt_claims(tokens.get("id_token")) if isinstance(tokens, dict) else {}
    email = claims.get("email")
    # only an id_token that decodes to an identity counts; "invalid" is not a login
    if isinstance(email, str) and email:
        auth = claims.get("https://api.openai.com/auth")
        plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
        found.append(
            HarnessAccount(
                "codex", "openai", email, f"ChatGPT {plan or 'login'} ({path})"
            )
        )
    key = data.get("OPENAI_API_KEY")
    if isinstance(key, str) and key.strip():
        found.append(HarnessAccount("codex", "openai", "API key", str(path)))
    return found or [HarnessAccount("codex", "openai", "(none)", f"no login in {path}")]


def _omp_credentials(db: Path) -> list[tuple[str, int, int]] | None:
    """(provider, usable, disabled) credential counts in omp's agent.db; None unreadable."""
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = conn.execute(
                "SELECT provider, SUM(disabled_cause IS NULL), SUM(disabled_cause IS NOT NULL)"
                " FROM auth_credentials GROUP BY provider ORDER BY provider"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return [(str(p), int(u or 0), int(d or 0)) for p, u, d in rows]


def _omp_db_rows(db: Path, label: str) -> tuple[list[HarnessAccount], set[str]]:
    """One omp auth db's rows (`label` names the profile) and its usable providers."""
    rows = _omp_credentials(db) if db.exists() else None
    if rows is None:
        return [
            HarnessAccount(
                "omp", "-", "(unknown)", f"no readable auth db at {db}{label}"
            )
        ], set()
    found = []
    for provider, usable, disabled in rows:
        detail = f"{db}{label}" + (f", {disabled} disabled" if disabled else "")
        if provider == "anthropic" and not label:
            detail += "; accounts listed under OMP ANTHROPIC ACCOUNTS"
        found.append(
            HarnessAccount(
                "omp",
                provider,
                f"{usable} credential" + ("" if usable == 1 else "s"),
                detail,
            )
        )
    return found, {provider for provider, usable, _d in rows if usable}


def _omp() -> list[HarnessAccount]:
    found, stored = _omp_db_rows(backends._omp_auth_db(), "")
    # the omp profiles $REVIEW_OMP_CLAUDE_PROFILES sends omp Claude seats through
    for profile in backends._review_omp_claude_profiles():
        if profile is not None:
            db = backends._omp_agent_dir(profile=profile) / "agent.db"
            found.extend(_omp_db_rows(db, f" (profile {profile})")[0])
    for provider, (var, _names) in backends._OMP_ENV_KEY_PROVIDERS.items():
        if provider not in stored and backends.omp_env_key(provider) is not None:
            found.append(
                HarnessAccount(
                    "omp",
                    provider,
                    "API key",
                    f"review-cli passes {var} to omp:{provider}/* seats",
                )
            )
    return found


def _json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _opencode() -> list[HarnessAccount]:
    """opencode's credentials from the same three sources (and with the same per-entry
    checks) `backends._oc_provider_auth_available` uses for an `oc:` seat: auth.json,
    an inline `options.apiKey` in opencode.json, and known env vars."""
    auth_path = backends._oc_auth_file()
    config_path = backends._oc_config_file()
    found = []
    for provider, entry in sorted(_json_object(auth_path).items()):
        if not backends._oc_auth_has_provider(provider):
            continue
        kind = entry.get("type") if isinstance(entry, dict) else None
        labels = {"api": "API key", "oauth": "OAuth login"}
        account = (
            labels.get(kind, "credential") if isinstance(kind, str) else "credential"
        )
        found.append(HarnessAccount("opencode", provider, account, str(auth_path)))
    providers = _json_object(config_path).get("provider")
    for provider in sorted(providers) if isinstance(providers, dict) else ():
        if backends._oc_config_has_provider_key(provider):
            found.append(
                HarnessAccount(
                    "opencode", provider, "API key", f"{config_path} options.apiKey"
                )
            )
    for provider, names in sorted(backends._OC_PROVIDER_ENV_VARS.items()):
        var = next((n for n in names if os.environ.get(n, "").strip()), None)
        if var is not None:
            found.append(HarnessAccount("opencode", provider, "API key", f"env {var}"))
    return found or [
        HarnessAccount("opencode", "-", "(none)", f"no providers ({auth_path})")
    ]


def _review_cli_keys() -> list[HarnessAccount]:
    found = []
    for provider, token, names in _REVIEW_CLI_KEYS:
        source = backends.resolve_key_source(names, names[0])
        if source is None:
            continue
        var, _key, where = source
        detail = f"{var} from {'the env' if where == 'env' else where}"
        if backends.provider_marked_unpaid(token):
            detail += f"; provider marked unpaid, its {token}: seats are skipped"
        account = "API token" if var.endswith("_TOKEN") else "API key"
        found.append(HarnessAccount("review-cli", provider, account, detail))
    return found


def discover() -> list[HarnessAccount]:
    """Every login on this machine, harness by harness (see the module docstring)."""
    return [
        *_claude_code(),
        *_codex(),
        *_omp(),
        *_opencode(),
        *_review_cli_keys(),
    ]


def as_json(accounts: list[HarnessAccount]) -> list[dict]:
    return [asdict(a) for a in accounts]


def render(accounts: list[HarnessAccount]) -> list[str]:
    """The `review seats` section."""
    lines = [
        "\nHARNESS ACCOUNTS (every login review-cli's seats can use on this machine; "
        "no secret is shown)"
    ]
    harness_w = max((len(a.harness) for a in accounts), default=0)
    provider_w = max((len(a.provider) for a in accounts), default=0)
    account_w = max((len(a.account) for a in accounts), default=0)
    for a in accounts:
        lines.append(
            f"  {a.harness.ljust(harness_w)}  {a.provider.ljust(provider_w)}  "
            f"{a.account.ljust(account_w)}  {a.detail}"
        )
    return lines
