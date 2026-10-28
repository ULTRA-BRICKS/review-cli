"""The configured account budget ceiling (review-cli#696): review-cli never spends a
subscription-backed Claude account beyond a configured share of its allowance, whichever
route dispatches the call.

WHAT: one typed setting, `account_budget:` in config.yaml merged over the version-controlled
defaults in the shipped `levels.yaml` (80%, strict). `max_usage_percent` is the ceiling for
every budgeted provider, `providers` replaces it per provider (only `anthropic` is budgeted:
the claude CLI seats and omp's `anthropic` account pool share one subscription), `strict`
decides what unconfirmable usage means and `max_age_seconds` how old a harness read may be.

WHERE it is enforced (every executable paid dispatch of a budgeted provider, before any
process is spawned or token fetched):
  * the direct claude CLI seat, per registered account (`usage_limits.spawn_refuse_reason`,
    called from `backends._review_claude_cli_with_account_fallback` and the images path);
  * omp's Anthropic account pool, per account (`omp_accounts.AccountPolicy`, which folds
    this budget into the #641 account policy; the stricter of the two thresholds wins);
  * a reserve seat promoted by failover or board selection: it dispatches through the same
    two functions, so there is no second entry.

WHAT a hold means. A 5-hour or 7-day window of the SAME account at or above the ceiling
holds it, compared unrounded (79.99 is under 80, 80.00 is held). With `strict` (the shipped
default) usage that cannot be confirmed holds it too: no usage card or `omp usage` entry for
that account, a read older than `max_age_seconds`, a failed source, or a missing 5-hour or
7-day window. A window that has already reset is not a reading. Usage of another account is
never mixed in; a model tier's own window (Fable's) counts for that tier's models only and
never completes the shared 5-hour/7-day pair. The refusal says "Blocked by review-cli
configured N% budget ceiling", names the account redacted (`redact_account`), the observed
percent and window and, when known, the reset time, and says it is a review-cli policy hold:
it never claims the provider ran out of quota. A held call spawns nothing and records no
cooldown (the next dispatch re-reads the usage).

Unconfigured (`current()` is None, e.g. library callers and tests) nothing is enforced; the
CLI configures it from the shipped defaults and the user's config on every run. Raising
`max_usage_percent`, setting `strict: false` or `enabled: false` in config.yaml is the owner's
deliberate, visible override (`enabled: false` is announced on stderr once per run; a hold
names the ceiling in its message; `review seats` shows only the omp pool's budget state in
its policy summary, not the direct-Claude budget).
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from . import levels

CONFIG_KEY = "account_budget"
KNOWN_PROVIDERS = ("anthropic",)
PROVIDER_ANTHROPIC = "anthropic"
_KEYS = ("enabled", "max_usage_percent", "providers", "strict", "max_age_seconds")
_WHERE = "config.yaml account_budget"

WINDOW_5H = "5-hour"
WINDOW_7D = "weekly"
WINDOWS = (WINDOW_5H, WINDOW_7D)
_WINDOW_TEXT = {WINDOW_5H: "5-hour", WINDOW_7D: "7-day"}

BLOCKED_PREFIX = "Blocked by review-cli configured"
_POLICY_NOTE = "review-cli policy hold, not a provider error"


@dataclass(frozen=True)
class AccountBudget:
    """The effective budget. `problems` are invalid settings: while one exists every
    budgeted dispatch is held, because the intended ceiling cannot be trusted."""

    max_usage_percent: float
    providers: tuple[tuple[str, float], ...] = ()
    strict: bool = True
    max_age_seconds: float = 1800.0
    problems: tuple[str, ...] = ()
    source: str = "shipped default"
    enabled: bool = True

    def ceiling_for(self, provider: str) -> float:
        for name, percent in self.providers:
            if name == provider:
                return percent
        return self.max_usage_percent

    def summary(self) -> str:
        overrides = "".join(
            f", {name} {percent_text(percent)}%" for name, percent in self.providers
        )
        mode = "strict" if self.strict else "unknown usage is let through"
        return (
            f"budget ceiling {percent_text(self.max_usage_percent)}%{overrides}, "
            f"{mode} ({self.source})"
        )


@dataclass(frozen=True)
class Reading:
    """One usage window of one account. `shared` is False for a model tier's own window
    (it never completes the 5-hour/7-day pair)."""

    window: str
    percent: float
    resets_at: float | None = None
    shared: bool = True


def percent_text(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def redact_account(identifier: str | None) -> str:
    """An account name safe for logs: `i***@gmail.com` for an email (first character and
    domain kept), the first character plus `***` for any other identifier, `the default
    account` when there is none."""
    if not identifier or not identifier.strip():
        return "the default account"
    name = identifier.strip().rstrip("/").rsplit("/", 1)[-1]
    local, at, domain = name.partition("@")
    head = (local[:1] or "?") + "***"
    return f"{head}@{domain}" if at and domain else head


def is_budget_hold(text: str | None) -> bool:
    return bool(text) and text.startswith(BLOCKED_PREFIX)


def _stamp(epoch_seconds: float | None) -> str:
    from datetime import datetime, timezone

    if epoch_seconds is None:
        return "reset time unknown"
    stamp = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return f"resets {stamp.strftime('%Y-%m-%d %H:%MZ')}"


def _account_note(account: str | None, named: bool) -> str:
    return f"; account {redact_account(account)}" if named else ""


def hold(
    budget: AccountBudget,
    provider: str,
    readings: list[Reading],
    *,
    now: float,
    account: str | None = None,
    name_account: bool = True,
    gap: str | None = None,
) -> str | None:
    """None when `provider`'s account may be used, else why not (a sentence starting with
    `BLOCKED_PREFIX`). `readings` are the account's windows; `gap` says why they could not
    be read at all (a failed source, an account the source does not list). `name_account`
    False leaves the account out of the sentence for a caller that prefixes its own
    redacted label."""
    named = name_account
    if budget.problems:
        return (
            f"{BLOCKED_PREFIX} budget ceiling: invalid account_budget settings "
            f"({'; '.join(budget.problems)}); fix them{_account_note(account, named)}. "
            f"{_POLICY_NOTE} (fail closed)"
        )
    ceiling = budget.ceiling_for(provider)
    live = [r for r in readings if r.resets_at is None or r.resets_at > now]
    over = [r for r in live if r.percent >= ceiling]
    if over:
        worst = max(over, key=lambda r: r.percent)
        label = _WINDOW_TEXT.get(worst.window, worst.window)
        tier = "" if worst.shared else " (model tier)"
        return (
            f"{BLOCKED_PREFIX} {percent_text(ceiling)}% budget ceiling: {label} window"
            f"{tier} at {percent_text(worst.percent)}% ({_stamp(worst.resets_at)})"
            f"{_account_note(account, named)}. {_POLICY_NOTE}"
        )
    if not budget.strict:
        return None
    missing = _missing_shared_windows(live)
    if gap is None and not missing:
        return None
    why = gap or f"no current {' or '.join(missing)} window reading for this account"
    return (
        f"{BLOCKED_PREFIX} {percent_text(ceiling)}% budget ceiling: cannot confirm this "
        f"account's 5-hour and 7-day usage ({why}); failing closed"
        f"{_account_note(account, named)}. {_POLICY_NOTE}"
    )


def _missing_shared_windows(readings: list[Reading]) -> list[str]:
    present = {r.window for r in readings if r.shared}
    return [
        _WINDOW_TEXT.get(window, window) for window in WINDOWS if window not in present
    ]


def _problem(where: str, value: object, what: str) -> str:
    return f"{where} must be {what}, got {value!r}"


def _percent(value: object, where: str) -> tuple[float | None, str | None]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 100
    ):
        return None, _problem(where, value, "a number above 0 and at most 100")
    return float(value), None


def _unknown_keys(raw: dict, problems: list[str]) -> None:
    unknown = sorted(str(key) for key in raw if key not in _KEYS)
    if unknown:
        problems.append(
            f"{_WHERE}: unknown key {', '.join(unknown)} (known: {', '.join(_KEYS)})"
        )


def _percent_override(raw: dict, default: float, problems: list[str]) -> float:
    if raw.get("max_usage_percent") is None:
        return default
    value, problem = _percent(raw["max_usage_percent"], f"{_WHERE}.max_usage_percent")
    if problem:
        problems.append(problem)
        return default
    return value


def _boolean_override(raw: dict, key: str, default: bool, problems: list[str]) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    problems.append(_problem(f"{_WHERE}.{key}", value, "true or false"))
    return default


def _age_override(raw: dict, default: float, problems: list[str]) -> float:
    value = raw.get("max_age_seconds")
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        problems.append(
            _problem(f"{_WHERE}.max_age_seconds", value, "a number of seconds above 0")
        )
        return default
    return float(value)


def _provider_overrides(raw: object, problems: list[str]) -> dict[str, float]:
    providers: dict[str, float] = {}
    if raw is None:
        return providers
    if not isinstance(raw, dict):
        problems.append(
            _problem(f"{_WHERE}.providers", raw, "a mapping of provider -> percent")
        )
        return providers
    for name, percent in raw.items():
        where = f"{_WHERE}.providers.{name}"
        if name not in KNOWN_PROVIDERS:
            problems.append(
                f"{where}: unknown provider (budgeted: {', '.join(KNOWN_PROVIDERS)})"
            )
            continue
        value, problem = _percent(percent, where)
        if problem:
            problems.append(problem)
        else:
            providers[name] = value
    return providers


def resolve(raw: object) -> AccountBudget:
    """The shipped defaults with the user's `account_budget:` value `raw` (None when
    absent) merged over them: a scalar replaces, `providers` merges per provider. Invalid
    values are collected in `problems`, never silently dropped."""
    shipped = levels.shipped_section(CONFIG_KEY)
    if not isinstance(shipped, dict):
        raise levels.LevelError(
            f"shipped levels file {levels.SHIPPED_LEVELS_PATH} has no "
            f"`{CONFIG_KEY}:` mapping"
        )
    problems: list[str] = []
    max_percent, problem = _percent(
        shipped.get("max_usage_percent"), f"shipped {CONFIG_KEY}.max_usage_percent"
    )
    if problem:
        raise levels.LevelError(problem)
    providers: dict[str, float] = {}
    strict = bool(shipped.get("strict", True))
    max_age = float(shipped.get("max_age_seconds", 1800))
    source = "shipped default"
    enabled = True
    if raw is not None and not isinstance(raw, dict):
        problems.append(_problem(_WHERE, raw, "a mapping"))
    elif isinstance(raw, dict):
        source = _WHERE
        _unknown_keys(raw, problems)
        max_percent = _percent_override(raw, max_percent, problems)
        strict = _boolean_override(raw, "strict", strict, problems)
        enabled = _boolean_override(raw, "enabled", enabled, problems)
        max_age = _age_override(raw, max_age, problems)
        providers = _provider_overrides(raw.get("providers"), problems)
    return AccountBudget(
        max_usage_percent=max_percent,
        providers=tuple(sorted(providers.items())),
        strict=strict,
        max_age_seconds=max_age,
        problems=tuple(problems),
        source=source,
        enabled=enabled,
    )


_CONFIGURED: AccountBudget | None = None
_DISABLED_ANNOUNCED = False


def configure(raw: object) -> None:
    """Set the per-process budget from config.yaml's `account_budget:` value `raw`
    (None: the shipped defaults). Called by the CLI on every run. `enabled: false` is the
    owner's deliberate override: nothing is enforced, and that is said once on stderr."""
    global _CONFIGURED, _DISABLED_ANNOUNCED
    budget = resolve(raw)
    if budget.enabled:
        _CONFIGURED = budget
        return
    _CONFIGURED = None
    if not _DISABLED_ANNOUNCED:
        _DISABLED_ANNOUNCED = True
        print(
            f"[review-cli] account budget ceiling is OFF ({_WHERE}.enabled: false): "
            "Claude accounts are used up to their provider limits",
            file=sys.stderr,
            flush=True,
        )


def unconfigure() -> None:
    """No budget is enforced (library callers and tests that predate the budget)."""
    global _CONFIGURED
    _CONFIGURED = None


def current() -> AccountBudget | None:
    return _CONFIGURED
