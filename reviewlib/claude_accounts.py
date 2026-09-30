"""Claude Code multi-account discovery, for review-cli's ACCOUNT fallback axis.

WHY THIS EXISTS (review-cli#168/#169/#170, Alex's hatch-request incident 2026-09-09):
review-cli's `claude:*` seats and any omp-hosted Anthropic seat authenticate as exactly
ONE Claude Code account. This machine (and any multi-account setup) keeps N separate
personal logins side by side under `~/.claude-accounts/account-N/` -- the SAME layout the
`claude-rotate` launcher (~/.files/bin/claude-rotate) and harness-cli
(harness_cli.discovery) already use. This module deliberately reuses that ONE existing
convention instead of inventing a third, review-cli-private registry: `CLAUDE_ROTATE_ROOT`
is the exact env override name claude-rotate itself honors, so pointing one at a custom
root points the other there too.

This module only DISCOVERS and ORDERS the registry and reads each account's login state
(`account_logged_in`, a local `claude auth status`) -- it never logs an account in or out
(that is a real interactive OAuth flow; see claude-rotate's own "rotate login", which is
the one-time human step that populates a new account slot). An empty/absent registry
degrades to a single implicit account (`[]` / `[(None, None)]` from the callers below) --
exactly today's pre-#168 single-account behaviour, unchanged.

Accessed via: reviewlib.backends' `review_claude`/`review_omp` account-rotation loop, and
indirectly by `usage_limits`'s `account=` filter (backends resolves the email, usage_limits
only ever sees the resulting string).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_ENV_ROOT = "CLAUDE_ROTATE_ROOT"


def accounts_root() -> Path:
    """The account registry directory -- `$CLAUDE_ROTATE_ROOT` (claude-rotate's own
    override name) or `~/.claude-accounts`. Never created here; callers only read."""
    override = os.environ.get(_ENV_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude-accounts"


def list_accounts(root: Path | None = None) -> list[Path]:
    """Registered `account-*` config dirs, sorted by name. Mirrors claude-rotate's own
    `list_accounts`: any directory literally named `account-*` is auto-discovered, so
    adding a 4th account needs no review-cli code change. `[]` when the registry dir
    does not exist or is unreadable (the single-implicit-account case)."""
    base = root if root is not None else accounts_root()
    try:
        entries = [
            p for p in base.iterdir() if p.is_dir() and p.name.startswith("account-")
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda p: p.name)


def current_account_name(root: Path | None = None) -> str | None:
    """The persisted `current` account name (claude-rotate's state file), or None when
    absent/unreadable -- callers then just use registry order as-is."""
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


def login_command(account_dir: Path) -> str:
    """The one command that logs `account_dir` back in (an interactive OAuth flow the
    user runs themselves)."""
    return f"CLAUDE_CONFIG_DIR={account_dir} claude auth login"
