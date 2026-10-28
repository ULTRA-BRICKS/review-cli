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
saying logged in while every call on the account failed). For the omp:anthropic seats
(review-cli#637, `omp_pool_state`): the omp launcher, omp's Anthropic accounts and their
rate-limit blocks from agent.db (read-only), the limits review-cli recorded per account,
and each account's usage windows from `omp usage` (a usage read, not a model call), with
the account policy's skip reason per account (review-cli#641: EXCLUDED / OVER LIMIT). No
model is called and no login is started.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import backends, claude_accounts, omp_accounts, seat_cooldown
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


@dataclass(frozen=True)
class OmpAccountState:
    """One of omp's Anthropic accounts as an omp:anthropic seat would see it
    (review-cli#637): omp's own rate-limit blocks (on `account`), a limit an earlier
    review-cli call hit on it (`limited_until` UNIX time, `limited_reason`), its usage
    windows from `omp usage` (None when omp did not report them), and why the account
    policy (review-cli#641) skips it for every model (`skip_reason`: EXCLUDED or a shared
    window OVER LIMIT) or only for one model tier (`tier_over_limit`)."""

    account: omp_accounts.OmpAccount
    limited_until: float | None
    limited_reason: str | None
    usage: list[omp_accounts.UsageWindow] | None
    skip_reason: str | None = None
    tier_over_limit: tuple[str, ...] = ()

    def is_available(self, now: float) -> bool:
        """Not skipped by the policy, and neither omp's shared-window block nor a
        review-cli limit is active."""
        return (
            self.skip_reason is None
            and self.account.blocked_until_ms(None) is None
            and (self.limited_until is None or self.limited_until <= now)
        )

    def availability(self, now: float) -> str:
        blocked_ms = self.account.blocked_until_ms(None)
        scoped = [
            f"{scope} blocked until {omp_accounts.utc_stamp(until)}"
            for scope, until in self.account.blocks
            if scope
        ]
        parts = [self.skip_reason] if self.skip_reason else []
        if blocked_ms is not None:
            parts.append(
                f"RATE-LIMITED until {omp_accounts.utc_stamp(blocked_ms)} (omp block)"
            )
        if self.limited_until is not None and self.limited_until > now:
            parts.append(
                f"LIMITED until {omp_accounts.utc_stamp(self.limited_until * 1000)} "
                f"(last review-cli call: {self.limited_reason})"
            )
        if not parts:
            parts.append("available")
        return "; ".join(parts + scoped + list(self.tier_over_limit))


@dataclass(frozen=True)
class OmpPoolState:
    """The omp launcher an omp seat would run (or why none can run), the account policy
    (review-cli#641), omp's Anthropic accounts (None when agent.db cannot be read) and
    their `omp usage` windows by lower-cased email (None when omp did not answer)."""

    launcher: omp_accounts.OmpLauncher | None
    launcher_problem: str | None
    db: Path
    accounts: list[OmpAccountState] | None
    policy: omp_accounts.AccountPolicy = omp_accounts.AccountPolicy()
    usage: dict[str, list[omp_accounts.UsageWindow]] | None = None

    def refusal(self, omp_model: str | None, now: float) -> str | None:
        """Why a seat on `omp_model` would get no account from the pool under the policy
        (`omp_accounts.pool_refusal`, the verdict dispatch acts on)."""
        accounts = None if self.accounts is None else [s.account for s in self.accounts]
        return omp_accounts.pool_refusal(
            self.policy, accounts, self.db, self.usage, omp_model, int(now * 1000)
        )[0]


def omp_pool_state(*, now: float | None = None) -> OmpPoolState:
    """Read-only: agent.db (read-only), the cooldown store and `omp usage` (a usage read,
    no model call)."""
    moment = time.time() if now is None else now
    now_ms = int(moment * 1000)
    try:
        launcher, problem = backends._omp_launcher(), None
    except RuntimeError as exc:
        launcher, problem = None, str(exc)
    policy = backends._omp_account_policy()
    db = backends._omp_auth_db()
    accounts = omp_accounts.anthropic_accounts(db, now_ms=now_ms)
    if accounts is None:
        return OmpPoolState(launcher, problem, db, None, policy)
    usage = omp_accounts.usage_by_email(launcher) if launcher and accounts else None
    # A live provider read may finish after the initial snapshot clock. Do not
    # classify its real observation timestamp as future; explicit test clocks stay fixed.
    if now is None:
        moment = time.time()
        now_ms = int(moment * 1000)
    states = []
    for account in accounts:
        cooldown = seat_cooldown.active_cooldown(
            backends._omp_account_cooldown_key(account),
            access_method=backends._OMP_ACCESS_METHOD,
            now=moment,
        )
        windows = omp_accounts.usage_of(usage, account)
        states.append(
            OmpAccountState(
                account=account,
                limited_until=cooldown["until"] if cooldown else None,
                limited_reason=cooldown["reason"] if cooldown else None,
                usage=windows,
                skip_reason=policy.skip_reason(account, windows, None, now_ms),
                tier_over_limit=policy.tier_over_limit(account, windows, now_ms),
            )
        )
    return OmpPoolState(launcher, problem, db, states, policy, usage)


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


def _omp_pool_reason(
    model: str, omp_pool: OmpPoolState | None, now: float
) -> str | None:
    """Why an omp seat on omp's Anthropic accounts gets no account under the account
    policy (review-cli#641), else None (also for every other seat)."""
    if omp_pool is None or backends.resolve_backend(model) is not backends.review_omp:
        return None
    canonical = _expand_alias(model)
    omp_model = canonical.split(":", 1)[1] if ":" in canonical else None
    if not backends._omp_selector_is_anthropic(omp_model):
        return None
    if backends._review_omp_claude_profiles() != [None]:
        return backends._omp_profiles_policy_reason(omp_model)
    return omp_pool.refusal(omp_model, now)


def _reason(
    model: str,
    accounts: list[ClaudeAccount],
    now: float,
    omp_pool: OmpPoolState | None,
) -> str | None:
    reason = backends.backend_unavailable_reason(model) or _cooldown_reason(model, now)
    if reason is None and _runs_through_claude_cli(model):
        reason = _claude_cli_reason(accounts)
    return reason or _omp_pool_reason(model, omp_pool, now)


def _substitute(
    model: str,
    accounts: list[ClaudeAccount],
    now: float,
    omp_pool: OmpPoolState | None,
) -> str | None:
    requested = _expand_alias(model).strip().lower()
    chain = provider_chain(
        model,
        available=lambda c: backends.backend_unavailable_reason(c) is None,
        unpaid=backends.runtime_provider_marked_unpaid,
    )
    for candidate in chain:
        if candidate.strip().lower() == requested:
            continue
        if _reason(candidate, accounts, now, omp_pool) is None:
            return candidate
    return None


def seat_health(
    model: str,
    accounts: list[ClaudeAccount],
    *,
    now: float | None = None,
    omp_pool: OmpPoolState | None = None,
) -> SeatHealth:
    """Why `model`'s seat cannot run right now (None when it can) and, if it cannot, the
    executor its provider failover would hand the seat to (review-cli#594). With
    `omp_pool`, an omp:anthropic seat the account policy leaves no account for is
    unusable too (review-cli#641)."""
    now = time.time() if now is None else now
    reason = _reason(model, accounts, now, omp_pool)
    substitute = _substitute(model, accounts, now, omp_pool) if reason else None
    return SeatHealth(model=model, reason=reason, substitute=substitute)
