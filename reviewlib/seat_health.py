"""Why a board seat cannot run right now, and which executor would answer for it.

review-cli#594: on 2026-09-29/30 every `claude:*` seat died ("OAuth session expired",
"You've hit your session limit") while `review --show-board` / `review entities` still
called them available (the `claude` binary was on PATH) and `review status` could only
inspect detached jobs. Bare `review status` and `review entities` render this module.

Read-only, and only from what review-cli already knows: the backend availability probe
(missing CLI/key/login, unpaid provider, a Claude CLI login that failed on every account),
the seat cooldown store (a recorded quota, limit or silence cooldown), and — for a seat
that runs through the Claude CLI — each registered `~/.claude-accounts` account's login
state (`claude_accounts.account_logged_in`, a local `claude auth status`) and its recorded
login failure (`claude_accounts.login_failure`, review-cli#596: `claude auth status` kept
saying logged in while every call on the account failed). No model is called and no login
is started.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import backends, claude_accounts, seat_cooldown
from .config import _expand_alias
from .provider_failover import provider_chain

# Every transport the cooldown store keys a seat under (seat_cooldown.ACCESS_METHODS does
# not list omp's own bucket yet — review-cli#483).
_COOLDOWN_METHODS = tuple(
    dict.fromkeys((*seat_cooldown.ACCESS_METHODS, backends._OMP_ACCESS_METHOD))
)


@dataclass(frozen=True)
class ClaudeAccount:
    """One registered Claude CLI account, whether the CLI considers it logged in
    (`logged_in` None = could not tell), and the login failure a call on it recorded
    (`login_failure` None = none standing; `login_failed_at` its UNIX time)."""

    name: str
    path: Path
    email: str | None
    current: bool
    logged_in: bool | None
    login_failure: str | None = None
    login_failed_at: float | None = None

    @property
    def login_command(self) -> str:
        return claude_accounts.login_command(self.path)

    @property
    def login_failed_at_utc(self) -> str | None:
        """When the recorded login failure happened, or None without one; a failure
        whose store entry lost its time reads as "at an unknown time"."""
        if self.login_failure is None:
            return None
        if self.login_failed_at is None:
            return "at an unknown time"
        at = datetime.fromtimestamp(self.login_failed_at, tz=timezone.utc)
        return at.strftime("%Y-%m-%d %H:%MZ")


@dataclass(frozen=True)
class SeatHealth:
    """`reason` is None when the seat's own executor can run; otherwise why it cannot, and
    `substitute` is the next executor in its failover chain that can (None if none)."""

    model: str
    reason: str | None
    substitute: str | None


def claude_accounts_state() -> list[ClaudeAccount]:
    """The Claude CLI account registry in name order, each with its login state and any
    standing recorded login failure."""
    current = claude_accounts.current_account_name()
    accounts = []
    for path in claude_accounts.list_accounts():
        failure = claude_accounts.login_failure(path)
        accounts.append(
            ClaudeAccount(
                name=path.name,
                path=path,
                email=claude_accounts.account_email(path),
                current=path.name == current,
                logged_in=claude_accounts.account_logged_in(path),
                login_failure=failure["reason"] if failure else None,
                login_failed_at=failure["recorded_at"] if failure else None,
            )
        )
    return accounts


def _cooldown_reason(model: str, now: float) -> str | None:
    for method in _COOLDOWN_METHODS:
        cooldown = seat_cooldown.active_cooldown(model, access_method=method, now=now)
        if cooldown is not None:
            until = datetime.fromtimestamp(cooldown["until"], tz=timezone.utc)
            return (
                f"cooling down until {until.strftime('%Y-%m-%d %H:%MZ')} "
                f"({cooldown['reason']})"
            )
    return None


def _runs_through_claude_cli(model: str) -> bool:
    return (
        backends.resolve_backend(model) is backends.review_claude
        and backends.claude_access_method() == "cli"
    )


def _claude_cli_reason(accounts: list[ClaudeAccount]) -> str | None:
    """A reason only when EVERY registered account is known to be unusable — logged out,
    or its last login failed — since the claude seats rotate through the registry and
    one live account keeps them usable. Wider than `backends._claude_cli_login_reason`
    (what dispatch routes on), which reads only recorded failures because it runs on
    every availability probe and cannot afford a `claude auth status` per account; a
    logged-out account gets its recorded failure from its first call ("Not logged in")."""
    if not accounts or any(
        a.logged_in is not False and a.login_failure is None for a in accounts
    ):
        return None
    if all(a.logged_in is False for a in accounts):
        names = ", ".join(
            f"{a.name} ({a.email or 'no email on disk'})" for a in accounts
        )
        return f"every registered Claude CLI account is logged out: {names}"
    states = ", ".join(
        f"{a.name} ({'logged out' if a.logged_in is False else a.login_failure})"
        for a in accounts
    )
    return f"every registered Claude CLI account is logged out or failed its login: {states}"


def _reason(model: str, accounts: list[ClaudeAccount], now: float) -> str | None:
    reason = backends.backend_unavailable_reason(model) or _cooldown_reason(model, now)
    if reason is None and _runs_through_claude_cli(model):
        reason = _claude_cli_reason(accounts)
    return reason


def _substitute(model: str, accounts: list[ClaudeAccount], now: float) -> str | None:
    requested = _expand_alias(model).strip().lower()
    chain = provider_chain(
        model,
        available=lambda c: backends.backend_unavailable_reason(c) is None,
        unpaid=backends.runtime_provider_marked_unpaid,
    )
    for candidate in chain:
        if candidate.strip().lower() == requested:
            continue
        if _reason(candidate, accounts, now) is None:
            return candidate
    return None


def seat_health(
    model: str, accounts: list[ClaudeAccount], *, now: float | None = None
) -> SeatHealth:
    """Why `model`'s seat cannot run right now (None when it can) and, if it cannot, the
    executor its provider failover would hand the seat to (review-cli#594)."""
    now = time.time() if now is None else now
    reason = _reason(model, accounts, now)
    substitute = _substitute(model, accounts, now) if reason else None
    return SeatHealth(model=model, reason=reason, substitute=substitute)
