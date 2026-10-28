"""The per-seat account view of `review seats` (review-cli#707): for each board seat the
account it would run on, that account's 5-hour and 7-day usage, the cap, the reset time
and, when the seat is refused, the exact rule.

Built from what `review seats` already read (`seat_health.OmpPoolState`, no new network
call) through the pure policy in `seat_budget`. Only omp's Anthropic accounts carry a usage
reading today, so a seat on any other executor says "usage неизвестен" instead of showing
a made-up zero. No credential is read or shown: accounts are named by email/label only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo

from . import omp_accounts, seat_budget
from .seat_health import OmpAccountState, OmpPoolState, SeatHealth

EXECUTOR_OMP = "omp account pool"
EXECUTOR_CLAUDE_CLI = "claude CLI login"
EXECUTOR_NONE = "no usage reading"

USABLE = "usable"
REFUSED = "refused"
UNUSABLE = "unusable"

_OMP_ANTHROPIC = "omp:anthropic/"
_NO_READING = (
    "usage неизвестен: у этого исполнителя нет читаемого usage, "
    "ограничение review-cli к нему не применимо"
)
_LABEL_KEYS = {"5 hour": "5h", "7 day": "7d"}
_GLOBAL_CAP_KEY = "omp_accounts.max_usage_percent"


@dataclass(frozen=True)
class SeatAccountView:
    seat: str
    status: str
    executor: str
    account: str | None
    five_hour_pct: float | None
    seven_day_pct: float | None
    cap_pct: float | None
    reset_at: float | None
    reason: str
    refused_accounts: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "status": self.status,
            "executor": self.executor,
            "account": self.account,
            "five_hour_percent": self.five_hour_pct,
            "seven_day_percent": self.seven_day_pct,
            "cap_percent": self.cap_pct,
            "reset_at": self.reset_at,
            "reason": self.reason or None,
            "refused_accounts": list(self.refused_accounts),
        }


def _omp_model(model: str, health: SeatHealth) -> str | None:
    """The omp model id whose account pool answers `model`, else None."""
    if model.startswith(_OMP_ANTHROPIC):
        return model.removeprefix(_OMP_ANTHROPIC)
    substitute = health.substitute or ""
    if model.startswith("claude:") and substitute.startswith(_OMP_ANTHROPIC):
        return substitute.removeprefix(_OMP_ANTHROPIC)
    return None


def _cap(pool: OmpPoolState, email: str | None) -> tuple[float | None, str]:
    policy = pool.policy
    key = (email or "").lower()
    for name, percent in policy.per_account:
        if name == key:
            return percent, f"omp_accounts.accounts.{name}.max_usage_percent"
    source = policy.max_usage_source.removeprefix("config.yaml ")
    return policy.max_usage_percent, source or _GLOBAL_CAP_KEY


def _windows(
    state: OmpAccountState, omp_model: str
) -> tuple[seat_budget.Window, ...] | None:
    if state.usage is None or not state.account.email:
        return None
    out = []
    for w in state.usage:
        if w.tier and not omp_accounts.tier_applies(w.tier, omp_model):
            continue
        base = w.label.removeprefix("Claude ").split("(")[0].strip().lower()
        key = _LABEL_KEYS.get(base, base)
        if w.tier:
            key = f"{key}-{w.tier}"
        resets = None if w.resets_at_ms is None else w.resets_at_ms / 1000
        # round away binary float noise (0.29 * 100 = 28.999...) so 29% reads as 29
        out.append(seat_budget.Window(key, round(w.used_fraction * 100, 6), resets))
    return tuple(out)


def _provider_block(state: OmpAccountState, omp_model: str, now: float):
    ends = []
    reasons = []
    blocked_ms = state.account.blocked_until_ms(omp_model)
    if blocked_ms is not None:
        ends.append(blocked_ms / 1000)
        reasons.append("omp block")
    if state.limited_until is not None and state.limited_until > now:
        ends.append(state.limited_until)
        reasons.append("429 на последнем вызове review-cli")
    if not ends:
        return None, None
    return max(ends), ", ".join(reasons)


def _reading(state: OmpAccountState, pool: OmpPoolState, omp_model: str, now: float):
    cap, key = _cap(pool, state.account.email)
    block_until, block_reason = _provider_block(state, omp_model, now)
    return seat_budget.AccountReading(
        name=state.account.label,
        windows=_windows(state, omp_model),
        read_at=now,
        provider_block_until=block_until,
        provider_block_reason=block_reason,
        cap_percent=cap,
        cap_key=key,
    )


def _closest(verdicts) -> seat_budget.AccountVerdict:
    """The refused account nearest to usable: a known reading first, lowest usage."""
    return min(
        verdicts,
        key=lambda v: (
            v.state in (seat_budget.USAGE_UNKNOWN, seat_budget.USAGE_STALE),
            v.peak_pct,
        ),
    )


def _pool_problem(pool: OmpPoolState) -> str | None:
    if pool.policy.problems:
        return (
            "неверная настройка omp_accounts: "
            + "; ".join(pool.policy.problems)
            + " -- аккаунты не используются, пока она не исправлена"
        )
    if not pool.accounts:
        where = (
            "список аккаунтов omp нечитаем"
            if pool.accounts is None
            else "в omp нет аккаунтов anthropic"
        )
        return f"нет аккаунтов: {where}"
    return None


def _no_account(seat: str, executor: str, status: str, reason: str) -> SeatAccountView:
    return SeatAccountView(seat, status, executor, None, None, None, None, None, reason)


def _verdict_view(seat, status, verdict, reason, refused=()) -> SeatAccountView:
    return SeatAccountView(
        seat,
        status,
        EXECUTOR_OMP,
        verdict.name,
        verdict.five_hour_pct,
        verdict.seven_day_pct,
        verdict.cap_pct,
        verdict.reset_at,
        reason,
        tuple(refused),
    )


def _pool_view(
    seat: str, health: SeatHealth, pool: OmpPoolState, omp_model: str, now: float, tz
) -> SeatAccountView:
    problem = _pool_problem(pool)
    if problem is not None:
        return _no_account(seat, EXECUTOR_OMP, REFUSED, problem)
    candidates = [s for s in pool.accounts if not pool.policy.is_excluded(s.account)]
    if not candidates:
        return _no_account(
            seat,
            EXECUTOR_OMP,
            REFUSED,
            "все аккаунты omp исключены настройкой omp_accounts.exclude",
        )
    selection = seat_budget.pick(
        [_reading(s, pool, omp_model, now) for s in candidates],
        cap_percent=pool.policy.max_usage_percent,
        cap_key=_GLOBAL_CAP_KEY,
        now=now,
        tz=tz,
    )
    refused = tuple(v.reason for v in selection.verdicts if not v.usable)
    if selection.chosen is None:
        closest = _closest(selection.verdicts)
        reason = selection.summary
        if reason.startswith("все аккаунты выше порога"):
            reason = f"{reason}; ближайший к порогу: {closest.reason}"
        return _verdict_view(seat, REFUSED, closest, reason, refused)
    if health.reason is not None and seat.startswith(_OMP_ANTHROPIC):
        return _verdict_view(seat, UNUSABLE, selection.chosen, health.reason, refused)
    return _verdict_view(seat, USABLE, selection.chosen, "", refused)


def seat_view(
    seat: str,
    health: SeatHealth,
    pool: OmpPoolState,
    *,
    now: float,
    tz: tzinfo | None = None,
) -> SeatAccountView:
    omp_model = _omp_model(seat, health)
    if omp_model is not None:
        return _pool_view(seat, health, pool, omp_model, now, tz)
    executor = EXECUTOR_CLAUDE_CLI if seat.startswith("claude:") else EXECUTOR_NONE
    if health.reason is not None and health.substitute is None:
        return _no_account(seat, executor, UNUSABLE, health.reason)
    return _no_account(seat, executor, USABLE, _NO_READING)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{seat_budget.percent_text(value)}%"


def render_line(display: str, view: SeatAccountView, tz: tzinfo | None = None) -> str:
    reset = (
        "-"
        if view.reset_at is None
        else f"сброс {seat_budget.clock_text(view.reset_at, tz)}"
    )
    cap = "-" if view.cap_pct is None else f"{seat_budget.percent_text(view.cap_pct)}%"
    head = (
        f"  {display:<12} {view.status:<8} {(view.account or '-'):<24} "
        f"5h {_pct(view.five_hour_pct)} · 7d {_pct(view.seven_day_pct)} · cap {cap} · {reset}"
    )
    return f"{head} · {view.reason}" if view.reason else head
