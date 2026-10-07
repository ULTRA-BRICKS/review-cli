"""Claude Code multi-account discovery, for review-cli's ACCOUNT fallback axis.

WHY THIS EXISTS (review-cli#168/#169/#170, Alex's hatch-request incident 2026-09-09):
review-cli's `claude:*` seats and any omp-hosted Anthropic seat authenticate as exactly
ONE Claude Code account. This machine (and any multi-account setup) keeps N separate
personal logins side by side under `~/.claude-accounts/account-N/`, one per-account
`CLAUDE_CONFIG_DIR` each -- the SAME layout harness-cli (harness_cli.discovery) and rig
already read. This module deliberately reuses that ONE existing convention instead of
inventing a review-cli-private registry. `$REVIEW_CLAUDE_ACCOUNTS_ROOT` points review-cli
at a different root (tests use it to build a throwaway registry).

This module DISCOVERS and ORDERS the registry, reads each account's login state
(`account_logged_in`, a local `claude auth status`), and remembers a login that failed on
an account (`record_login_failure`/`login_failure`, review-cli#596) -- it never logs an
account in or out (that is a real interactive OAuth flow: the one-time human step
`CLAUDE_CONFIG_DIR=<account dir> claude auth login`, see `login_command`, populates a new
account slot). An empty/absent registry degrades to a single implicit account (`[]` /
`[(None, None)]` from the callers below) -- exactly today's pre-#168 single-account
behaviour, unchanged.

Accessed via: reviewlib.backends' `review_claude`/`review_omp` account-rotation loop, and
indirectly by `usage_limits`'s `account=` filter (backends resolves the email, usage_limits
only ever sees the resulting string).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import seat_cooldown

_ENV_ROOT = "REVIEW_CLAUDE_ACCOUNTS_ROOT"


def accounts_root() -> Path:
    """The account registry directory -- `$REVIEW_CLAUDE_ACCOUNTS_ROOT` or
    `~/.claude-accounts`. Never created here; callers only read."""
    override = os.environ.get(_ENV_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude-accounts"


def list_accounts(root: Path | None = None) -> list[Path]:
    """Registered `account-*` config dirs, sorted by name: any directory literally named
    `account-*` is auto-discovered, so adding a 4th account needs no review-cli code
    change. `[]` when the registry dir does not exist or is unreadable (the
    single-implicit-account case)."""
    base = root if root is not None else accounts_root()
    try:
        entries = [
            p for p in base.iterdir() if p.is_dir() and p.name.startswith("account-")
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda p: p.name)


def current_account_name(root: Path | None = None) -> str | None:
    """The account named in the registry root's optional `current` file (the account to
    try first), or None when absent/unreadable -- callers then just use registry order
    as-is."""
    base = root if root is not None else accounts_root()
    try:
        saved = (base / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return saved or None


def account_email(account_dir: Path) -> str | None:
    """Best-effort account email for `account_dir`, read from the same place harness-cli
    reads it (`.claude.json`'s `oauthAccount.emailAddress`) -- non-secret metadata only,
    never a credential/token. None when the file is absent, unparsable, or has no email."""
    try:
        data = json.loads((account_dir / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    return email.strip() if isinstance(email, str) and email.strip() else None


def rotation_order(root: Path | None = None) -> list[Path]:
    """Registered account dirs starting at the persisted `current` account, then
    wrapping through the rest in name order -- the exact walk order the account-fallback
    loop in `reviewlib.backends` tries. `[]` when no `account-*` registry exists at all
    (single implicit account; callers treat that the same as today's pre-#168 behaviour)."""
    accounts = list_accounts(root)
    if not accounts:
        return []
    current = current_account_name(root)
    names = [a.name for a in accounts]
    if not current or current not in names:
        return accounts
    start = names.index(current)
    return accounts[start:] + accounts[:start]


def account_logged_in(account_dir: Path) -> bool | None:
    """Whether the Claude CLI considers `account_dir` logged in: `loggedIn` from
    `claude auth status --json` run with CLAUDE_CONFIG_DIR=account_dir (review-cli#594).
    Read-only and local -- it never starts a login and never prints a credential. None
    when it cannot tell (no `claude` binary, a timeout, output that is not that JSON)."""
    binary = shutil.which("claude")
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "auth", "status", "--json"],
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(account_dir)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    logged_in = data.get("loggedIn") if isinstance(data, dict) else None
    return logged_in if isinstance(logged_in, bool) else None


def login_command(account_dir: Path | None) -> str:
    """The one command that logs `account_dir` (None = the single implicit account) back
    in (an interactive OAuth flow the user runs themselves)."""
    if account_dir is None:
        return "claude auth login"
    return f"CLAUDE_CONFIG_DIR={account_dir} claude auth login"


# review-cli#596: on 2026-09-30 `claude auth status` said loggedIn=true for account-0 and
# account-1 while every call on them failed with "Failed to authenticate: OAuth session
# expired and could not be refreshed", so nothing marked the claude:* seats unusable and
# every review paid the failed call before its omp twin answered. A failed login is now
# recorded per ACCOUNT in the seat-cooldown store (its escalating window bounds how long
# the account goes untried). It is dropped by a later successful call on the account, or
# as soon as the account's stored login is rewritten after the failure (a re-login).
_LOGIN_FAILURE_ACCESS_METHOD = "cli"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_MDAT = re.compile(r'"mdat"<timedate>=\S*\s+"(\d{14})Z')
# While a failure stands, `login_failure` runs on every availability probe (each seat,
# each failover candidate, each account); one Keychain read per item per this window.
_KEYCHAIN_STAMP_TTL_SECONDS = 30.0
_keychain_stamps: dict[str, tuple[float, float | None]] = {}


def _login_failure_key(account_dir: Path | None) -> str:
    return f"claude-login:{account_dir if account_dir is not None else 'default'}"


def record_login_failure(account_dir: Path | None, reason: str) -> None:
    """Remember that a call on `account_dir` (None = the single implicit account) failed
    its login with `reason`. Best-effort, never raises."""
    seat_cooldown.record_cooldown(
        _login_failure_key(account_dir),
        reason,
        access_method=_LOGIN_FAILURE_ACCESS_METHOD,
    )


def clear_login_failure(account_dir: Path | None) -> None:
    """Forget `account_dir`'s recorded login failure. Best-effort, never raises."""
    seat_cooldown.clear_cooldown(
        _login_failure_key(account_dir), access_method=_LOGIN_FAILURE_ACCESS_METHOD
    )


def login_failure(account_dir: Path | None) -> dict | None:
    """The recorded login failure still standing for `account_dir` (seat_cooldown's
    `active_cooldown` entry: `reason`, `recorded_at`, `until`, ...), else None.

    Side effect: a login stored after the failure (`_credentials_modified_at`) means the
    account was logged back in, so the record is DROPPED from the store and None
    returned. The record is written after the failed call returned, so anything that
    call itself stored predates it."""
    failure = seat_cooldown.active_cooldown(
        _login_failure_key(account_dir), access_method=_LOGIN_FAILURE_ACCESS_METHOD
    )
    if failure is None:
        return None
    stored_at = _credentials_modified_at(account_dir)
    recorded_at = failure["recorded_at"]
    if stored_at is not None and (recorded_at is None or stored_at > recorded_at):
        clear_login_failure(account_dir)
        return None
    return failure


def _credentials_modified_at(account_dir: Path | None) -> float | None:
    """When the Claude CLI last stored `account_dir`'s login (a `claude auth login`, or a
    token refresh by any session on the account), as a UNIX time; None when it cannot
    tell. Reads metadata only, never the credential: the mtime of
    `<config dir>/.credentials.json` (where the CLI keeps the login outside macOS), and on
    macOS the modification date of the CLI's Keychain item. That item is named
    `Claude Code-credentials`, suffixed with the first 8 hex digits of
    sha256(CLAUDE_CONFIG_DIR) when that is set (the CLI's own naming)."""
    explicit = (
        str(account_dir)
        if account_dir is not None
        else os.environ.get("CLAUDE_CONFIG_DIR") or None
    )
    config_dir = Path(explicit) if explicit else Path.home() / ".claude"
    stamps: list[float] = []
    try:
        stamps.append((config_dir / ".credentials.json").stat().st_mtime)
    except OSError:
        pass
    if sys.platform == "darwin":
        service = _KEYCHAIN_SERVICE
        if explicit:
            service += "-" + hashlib.sha256(explicit.encode()).hexdigest()[:8]
        stamp = _keychain_item_modified_at(service)
        if stamp is not None:
            stamps.append(stamp)
    return max(stamps, default=None)


def _keychain_item_modified_at(service: str) -> float | None:
    """The modification date of the macOS Keychain generic-password item `service`, or
    None, read at most once per `_KEYCHAIN_STAMP_TTL_SECONDS` per item.
    `security find-generic-password` without -g/-w prints the item's attributes only; it
    never reads the secret and never prompts."""
    now = time.monotonic()
    cached = _keychain_stamps.get(service)
    if cached is not None and now - cached[0] < _KEYCHAIN_STAMP_TTL_SECONDS:
        return cached[1]
    stamp = _read_keychain_item_modified_at(service)
    _keychain_stamps[service] = (now, stamp)
    return stamp


def _read_keychain_item_modified_at(service: str) -> float | None:
    binary = shutil.which("security")
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "find-generic-password", "-s", service],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _KEYCHAIN_MDAT.search(proc.stdout) if proc.returncode == 0 else None
    if match is None:
        return None
    stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return stamp.replace(tzinfo=timezone.utc).timestamp()
