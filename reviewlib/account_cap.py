"""The one account usage cap (review-cli#696): never spend a subscription-backed Claude
account beyond a configured share of its allowance, whichever route dispatches the call.

`account_usage_cap_percent` in config.yaml (default 80; `$REVIEW_ACCOUNT_USAGE_CAP_PERCENT`
overrides it, `off` disables it and says so) is enforced before any paid dispatch of:

  * omp's Anthropic account pool -- `backends._omp_account_policy` makes it the account
    policy's usage threshold unless `omp_accounts.max_usage_percent` / a per-account entry
    sets one (those stay the more specific word);
  * the direct `claude` CLI seat -- `claude_refusal`, called per registered account before
    the CLI is spawned. The reading is the matching account's: omp's `omp usage` windows by
    email and the harness card for that account. Either one at or over the cap refuses;
    with no reading at all the account is refused CONSERVATIVELY and the message says so,
    because an account whose usage cannot be confirmed is not assumed to be below the cap.

A refusal always says it is review-cli's own rule (`ограничение review-cli (config KEY=N)`)
with the account, the observed percent and window and, when known, the reset time; it never
claims the provider ran out of quota, and a provider 429 is worded elsewhere
(`seat_budget.PROVIDER_LIMITED`). A refused call spawns nothing and records no cooldown.

Unconfigured (library callers, unit tests) nothing is enforced; the CLI configures it from
the config on every run (`cli_accounts._apply_runtime_config`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo

from . import omp_accounts, seat_budget

CONFIG_KEY = "account_usage_cap_percent"
ENV = "REVIEW_ACCOUNT_USAGE_CAP_PERCENT"
DEFAULT_PERCENT = 80.0
_OFF_WORDS = frozenset({"off", "none", "disabled", "false"})
_LABEL_KEYS = {"5 hour": "5h", "7 day": "7d"}


@dataclass(frozen=True)
class Cap:
    """The effective cap. `percent` None means nothing is enforced (`disabled`: the owner
    switched it off; otherwise the cap was never configured). `problems` are invalid
    settings; the CLI refuses to run while one exists."""

    percent: float | None
    source: str = "unconfigured"
    problems: tuple[str, ...] = ()
    disabled: bool = False


_UNCONFIGURED = Cap(None)
_CURRENT: Cap = _UNCONFIGURED


def _number(value: object, where: str) -> tuple[float | None, str | None]:
    text = value.strip().removesuffix("%").strip() if isinstance(value, str) else value
    if isinstance(text, str):
        try:
            text = float(text)
        except ValueError:
            text = None
    if (
        isinstance(text, bool)
        or not isinstance(text, (int, float))
        or not 0 < text <= 100
    ):
        return None, f"{where} must be a number above 0 and at most 100, got {value!r}"
    return float(text), None


def resolve(raw: object, env: Mapping[str, str]) -> Cap:
    """The cap from config value `raw` (None: not set -> the 80% default) and the env."""
    env_value = (env.get(ENV) or "").strip()
    if env_value:
        if env_value.lower() in _OFF_WORDS:
            return Cap(None, f"${ENV}={env_value}", disabled=True)
        percent, problem = _number(env_value, f"${ENV}")
        source = f"${ENV}"
    elif raw is None:
        percent, problem, source = DEFAULT_PERCENT, None, f"default {CONFIG_KEY}"
    else:
        percent, problem = _number(raw, f"config.yaml {CONFIG_KEY}")
        source = f"config.yaml {CONFIG_KEY}"
    if problem:
        return Cap(None, source, (problem,))
    return Cap(percent, source)


def configure(raw: object, env: Mapping[str, str] | None = None) -> Cap:
    """Set the per-process cap from the config value (the CLI does this on every run)."""
    global _CURRENT
    _CURRENT = resolve(raw, os.environ if env is None else env)
    return _CURRENT


def configure_unset() -> None:
    global _CURRENT
    _CURRENT = _UNCONFIGURED


def current() -> Cap:
    return _CURRENT


def apply(config: Mapping[str, object]) -> Cap:
    """Configure the cap from a loaded config.yaml mapping. An invalid value stops the run
    (ConfigError -> exit 2): a cap nobody can trust must not silently become 'no cap'."""
    cap = configure(config.get(CONFIG_KEY))
    if cap.problems:
        from .config import ConfigError

        raise ConfigError("; ".join(cap.problems))
    return cap


def default_claude_email() -> str | None:
    """The email of the implicit `claude` CLI login (`~/.claude.json` oauthAccount: the
    same non-secret metadata `claude_accounts.account_email` reads), None when unknown."""
    from pathlib import Path

    from . import claude_accounts

    return claude_accounts.account_email(Path.home())


def omp_windows_to_budget(
    windows: list | tuple | None, omp_model: str | None
) -> tuple[seat_budget.Window, ...] | None:
    """`omp usage` windows as `seat_budget` windows: the shared 5-hour/7-day pair plus any
    model-tier window `omp_model` uses (Fable's counts for Fable models only)."""
    if windows is None:
        return None
    out = []
    for w in windows:
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


def _unknown_text(name: str, cap_percent: float) -> str:
    return (
        f"usage неизвестен для аккаунта {name}: ни omp usage, ни harness не дали показаний "
        f"5ч/7д; консервативно не используется (ограничение review-cli, config "
        f"{CONFIG_KEY}={seat_budget.percent_text(cap_percent)})"
    )


def claude_refusal(
    model: str,
    email: str | None,
    *,
    cap_percent: float | None,
    omp_windows: list | tuple | None,
    harness_percent: float | None,
    now: float,
    tz: tzinfo | None = None,
) -> str | None:
    """Why the direct claude CLI seat must not spend `email`'s account now (review-cli's own
    cap, or usage that cannot be confirmed), else None. `omp_windows` are that account's
    `omp usage` windows, `harness_percent` the harness card's highest window for it."""
    if cap_percent is None:
        return None
    name = email or "claude (аккаунт по умолчанию)"
    confirmed = False
    budget_windows = omp_windows_to_budget(omp_windows, model.split(":", 1)[-1])
    if budget_windows is not None:
        verdict = seat_budget.judge(
            seat_budget.AccountReading(name=name, windows=budget_windows, read_at=now),
            cap_percent=cap_percent,
            cap_key=CONFIG_KEY,
            now=now,
            tz=tz,
        )
        if verdict.state == seat_budget.OVER_CAP:
            return verdict.reason
        confirmed = verdict.state == seat_budget.OK
    if harness_percent is not None:
        if harness_percent >= cap_percent:
            return (
                f"ограничение review-cli (config {CONFIG_KEY}="
                f"{seat_budget.percent_text(cap_percent)}): аккаунт {name} использован на "
                f"{seat_budget.percent_text(harness_percent)}% (по harness, наибольшего из "
                "окон 5ч/7д)"
            )
        confirmed = True
    return None if confirmed else _unknown_text(name, cap_percent)
