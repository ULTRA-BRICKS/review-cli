"""Best-effort cross-provider usage-limit awareness for board/panel composition.

review-cli itself has no visibility into a provider's account-level rate/usage
window at DISPATCH time -- it only ever learns a seat is exhausted AFTER a call
fails (see reviewlib.retry / reviewlib.seat_cooldown, both reactive). Live
``harness limits --all --json`` (harness-cli) is the preferred source for
Claude Code / Codex 5-hour and 7-day windows. tg-cli's tg-ctl daemon still
persists samples to ``~/.config/tg-cli/tg-ctl.<chat_id>.usage-latest.json``;
this module keeps that file as a fallback for families harness did not report.

Coverage is PARTIAL by construction: only the agent families mapped below
(currently ``claude`` and ``codex``) have real percentage data. Every other
provider (zai/glm, gemini, commandcode-routed opencode seats, openrouter,
...) has no comparable local signal yet -- ``usage_percent_for_model`` returns
None for those, which callers MUST treat as "not excluded" (fail OPEN, never
fail closed on missing data). Tracked as a known gap in review-cli#205.

Fails open throughout: a missing harness binary, unusable JSON, a card whose
``status != "ok"``, a window with ``used_percent is None``, a missing/corrupt
tg-ctl file, an unmapped model, or any parse error all resolve to None /
allow spawn, so a review can never be blocked by this module being wrong,
absent, or out of date.

ONE exception, the configured account budget (review-cli#696, `reviewlib.account_budget`):
when it is configured, `spawn_refuse_reason` / `allow_spawn_for_model` hold a `claude:`
dispatch at the budget ceiling and, with `strict`, fail CLOSED when that account's fresh
5-hour and 7-day usage cannot be read from a harness card (never the tg-ctl file: it has no
account field and may be a day old). Board composition above still fails open.

A SECOND, ORTHOGONAL scoping axis: `account` (review-cli#168, Alex's
hatch-request incident 2026-09-09). Every function below now accepts an
optional keyword-only `account` (an email, normalized case/whitespace-
insensitively). When given, only harness cards carrying that SAME account
gate the result -- a card for a DIFFERENT registered Claude account (see
`reviewlib.claude_accounts`) never gates a dispatch for THIS one. This is
the exact fix for the bug where ANY ONE of this machine's N registered
Claude Code accounts reporting 100% blocked EVERY `claude:*` seat regardless
of which account was actually in use. `account=None` (every existing caller)
is 100% backward compatible: the old aggregate-across-every-account MAX
behaviour, unchanged.

Accessed via: board/panel composition (`usage_percent_for_model`) and the
claude pre-dispatch spawn gate (`spawn_refuse_reason` in backends.py).
Tests inject cards via `cards=` / `runner=` / `$REVIEW_HARNESS_LIMITS_JSON`
so they never hit the network or the real `harness` binary.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import account_budget

# A sample older than this is not trusted as "current" -- tg-ctl only writes a
# fresh sample when the harness itself reports one (on hook fire / limit check),
# so a stale file (the user hasn't run that harness in a while) must not
# silently exclude a model based on ancient data. Overridable for tests via
# $REVIEW_USAGE_MAX_AGE_SECONDS.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60  # 24h
_ENV_MAX_AGE = "REVIEW_USAGE_MAX_AGE_SECONDS"
_ENV_FILE = "REVIEW_USAGE_LIMITS_FILE"
_ENV_DIR = "REVIEW_USAGE_LIMITS_DIR"
_ENV_HARNESS_JSON = "REVIEW_HARNESS_LIMITS_JSON"

# The rate/usage-WINDOW limit names this module cares about. "context window" is
# a per-call SIZE limit, not an account usage budget that runs out over time, so
# it is deliberately excluded from the near-limit check. Harness JSON is keyed
# by window_seconds, not these English labels.
_RATE_LIMIT_NAMES = frozenset({"5-hour", "weekly"})
_FIVE_HOUR_SECONDS = 5 * 3600
_SEVEN_DAY_SECONDS = 7 * 86400
_WINDOW_SECONDS_TO_NAME = {
    float(_FIVE_HOUR_SECONDS): "5-hour",
    float(_SEVEN_DAY_SECONDS): "weekly",
}

# A model tier's own allowance, named like `Weekly (Fable)`: it counts for that tier's models
# only, never for the shared 5-hour/7-day pair the account budget needs.
_TIER_IN_NAME = re.compile(r"\((fable|opus|sonnet|haiku)\b[^)]*\)", re.IGNORECASE)

# The single named threshold this whole feature uses by default (board
# composition, flat-panel padding, and this module's own helpers all import
# this ONE constant rather than each hardcoding "70.0" separately).
DEFAULT_LIMIT_THRESHOLD = 70.0

# Model-string prefix -> the tg-ctl `agent` family sharing its account quota.
# Extend this map as tg-ctl learns to track more agent families (see the module
# docstring's "coverage is PARTIAL" note). `codex:gpt-...` seats (e.g. SOL_SEAT
# in config.py) route through the SAME codex account as the bare `"codex"`
# seat, so they share the prefix mapping too.
_AGENT_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude:", "claude"),
    ("codex:", "codex"),
)
# Bare (non-prefixed) model ids routed through a specific agent's own account.
_AGENT_FAMILY_EXACT: dict[str, str] = {"codex": "codex"}

# harness-cli LimitCard.provider -> the same family ids as above.
_HARNESS_PROVIDER_FAMILY = {
    "claude code": "claude",
    "codex": "codex",
}


def _max_age_seconds() -> float:
    raw = os.environ.get(_ENV_MAX_AGE)
    if not raw or not raw.strip():
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_AGE_SECONDS


def _usage_file_paths() -> list[Path]:
    override = os.environ.get(_ENV_FILE)
    if override:
        return [Path(override).expanduser()]
    # $REVIEW_USAGE_LIMITS_DIR overrides the DIRECTORY the glob searches (tests
    # drive the multi-file merge path against a throwaway dir); the real
    # default is tg-cli's own config directory.
    base = os.environ.get(_ENV_DIR)
    directory = Path(base).expanduser() if base else Path.home() / ".config" / "tg-cli"
    pattern = str(directory / "tg-ctl.*.usage-latest.json")
    return [Path(p) for p in glob.glob(pattern)]


def load_snapshot() -> list[dict]:
    """Every usage sample from EVERY tg-ctl usage-latest.json this host has,
    merged. Multiple chat ids each get their own file; a single "freshest file
    wins" pick would silently drop a whole agent family's data if that chat's
    file happens to be older (e.g. a `claude`-only daemon writes often while a
    separate chat's `codex` samples sit in a less-recently-touched file). Each
    sample already carries its own `sampledAt` and staleness is enforced
    per-sample by the caller, so merging everything is strictly safe and more
    complete than picking one file. Never raises -- any failure reads as "no
    samples", matching this module's fail-open contract.

    Call this ONCE per board/panel composition and reuse the result (pass it
    as `usage_percent_for_model`'s `samples=`) instead of letting every seat
    re-glob + re-read + re-parse the same files independently."""
    merged: list[dict] = []
    try:
        for path in _usage_file_paths():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            samples = data.get("samples") if isinstance(data, dict) else None
            if isinstance(samples, list):
                merged.extend(samples)
    except Exception:  # noqa: BLE001 -- best-effort, read-only signal
        return []
    return merged


def _run_harness_limits_json() -> str:
    binary = shutil.which("harness")
    if not binary:
        raise FileNotFoundError("harness")
    proc = subprocess.run(
        [binary, "limits", "--all", "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "harness limits failed")
    return proc.stdout


def load_harness_report(*, runner=None) -> dict:
    """`{"status", "cards", "observed_at", "origin", "detail"}` for the harness limits
    source, never raising (review-cli#472: a capacity report must tell a missing source
    from a failed or malformed one, which `load_harness_cards`' plain `[]` cannot).

    `status`: "ok", "missing" (no `harness` binary), "failed" (non-zero exit, timeout,
    unreadable fixture file) or "malformed" (not JSON, or no card list). `observed_at` is
    when the cards were read (UNIX time): the call time for a live `harness` run or an
    inline `$REVIEW_HARNESS_LIMITS_JSON` value, the file's mtime for a fixture path.
    `origin` is "harness", "env" or "env-file"."""
    observed_at = time.time()
    origin = "harness"
    try:
        raw = os.environ.get(_ENV_HARNESS_JSON)
        if raw is not None and raw.strip():
            text = raw.strip()
            origin = "env"
            if not text.startswith("{") and not text.startswith("["):
                path = Path(text).expanduser()
                origin = "env-file"
                observed_at = path.stat().st_mtime
                text = path.read_text(encoding="utf-8")
        else:
            run = runner if runner is not None else _run_harness_limits_json
            text = run()
    except FileNotFoundError as exc:
        status = "missing" if origin == "harness" else "failed"
        return _harness_report(status, [], observed_at, origin, str(exc))
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        return _harness_report("failed", [], observed_at, origin, str(exc))
    return _decode_harness_report(text, observed_at, origin)


def _harness_report(
    status: str, cards: list[dict], observed_at: float, origin: str, detail
) -> dict:
    return {
        "status": status,
        "cards": cards,
        "observed_at": observed_at,
        "origin": origin,
        "detail": detail,
    }


def load_harness_cards(*, runner=None) -> list[dict]:
    """Cards from `$REVIEW_HARNESS_LIMITS_JSON` or `harness limits --all --json`.

    The env fixture is a JSON object (`{"cards":[...]}`), a JSON array of
    cards, or a path to either. `runner`, when given, is a zero-arg callable
    that returns that same JSON text — tests inject it so this never touches
    the real binary. Missing harness / unusable JSON / any error -> []
    (`load_harness_report` says which)."""
    return load_harness_report(runner=runner)["cards"]


def _agent_family(model: str) -> str | None:
    if model in _AGENT_FAMILY_EXACT:
        return _AGENT_FAMILY_EXACT[model]
    for prefix, family in _AGENT_FAMILY_PREFIXES:
        if model.startswith(prefix):
            return family
    return None


def _finite_percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = float(value)
    if not math.isfinite(pct):
        return None
    return pct


def parse_time(value: object) -> float | None:
    """UNIX seconds from an epoch number (seconds or milliseconds) or an ISO-8601
    string; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if not math.isfinite(ts):
            return None
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _window_kind(window_seconds: object) -> str | None:
    seconds = _finite_percent(window_seconds)
    if seconds is None or seconds <= 0:
        return None
    return _WINDOW_SECONDS_TO_NAME.get(seconds)


def _family_from_provider(provider: object) -> str | None:
    if not isinstance(provider, str):
        return None
    return _HARNESS_PROVIDER_FAMILY.get(provider.strip().lower())


def _card_account(card: dict) -> str | None:
    """Normalized (casefolded, stripped) `card["account"]`, or None when the
    harness didn't report one. Callers normalize their own `account=` kwarg
    the same way before comparing, so "Alex@X.com" and "alex@x.com " match."""
    value = card.get("account")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _percent_from_harness_cards(
    cards: list[dict], family: str, account: str | None = None
) -> float | None:
    """Maximum finite percentage for the same family's optionally scoped account."""
    return max(
        (percent for _, percent in _known_window_percentages(cards, family, account)),
        default=None,
    )


def _exhausted_window_names(
    cards: list[dict], family: str, account: str | None = None
) -> list[str]:
    """Preserve source order while collecting exhausted shared-window names."""
    seen: dict[str, float] = {}
    for kind, percent in _known_window_percentages(cards, family, account):
        previous = seen.get(kind)
        if previous is None or percent > previous:
            seen[kind] = percent
    return [name for name, percent in seen.items() if percent >= 100.0]


def _percent_from_tgctl_samples(
    family: str, live_samples: list[dict], now: float | None
) -> float | None:
    if not live_samples:
        return None
    now_ms = (now if now is not None else time.time()) * 1000
    cutoff_ms = now_ms - _max_age_seconds() * 1000
    best: float | None = None
    for sample in _freshest_tgctl_samples(family, live_samples).values():
        percent = _current_tgctl_percent(sample, cutoff_ms, now_ms)
        if percent is not None and (best is None or percent > best):
            best = float(percent)
    return best


def usage_percent_for_model(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
    account: str | None = None,
) -> float | None:
    """The highest current rate-limit-window usage percent (0-100) for
    `model`'s underlying account, or None when unknown (unmapped provider, no
    matching harness card or tg-ctl sample, or the freshest matching sample is
    older than the trust window). Callers MUST treat None as "not excluded" --
    never as 0 or 100.

    Prefer live harness JSON when available; fall back to tg-ctl samples for a
    family harness did not report. `samples`, when given without `cards`, is
    used INSTEAD of reading either live source -- the existing unit tests drive
    this so the tg-ctl algorithm has zero filesystem/env dependency. `cards`
    and `runner` inject harness JSON the same way for the live-limits path.

    `account`, when given, scopes the harness-card path to that ONE account
    (review-cli#168; see `_percent_from_harness_cards`). The tg-ctl samples
    fallback has no per-account field in its schema -- KNOWN PARTIAL
    COVERAGE: that path stays unscoped regardless of `account`.
    """
    family = _agent_family(model)
    if family is None:
        return None

    # Existing tests pass samples= and must not hit harness. Live path
    # (neither samples nor cards) prefers harness, then tg-ctl fallback.
    if cards is not None or samples is None:
        live_cards = cards if cards is not None else load_harness_cards(runner=runner)
        harness_pct = _percent_from_harness_cards(live_cards, family, account)
        if harness_pct is not None:
            return harness_pct
        if cards is not None and samples is None:
            return None

    live_samples = samples if samples is not None else load_snapshot()
    return _percent_from_tgctl_samples(family, live_samples, now)


def _tier_of(window: dict) -> str | None:
    """The model tier a harness window is scoped to (`tier` field, or a `(Fable)`-style
    suffix of its name), None for a window every model of the account shares."""
    tier = window.get("tier")
    if isinstance(tier, str) and tier.strip():
        return tier.strip().lower()
    match = _TIER_IN_NAME.search(str(window.get("name") or ""))
    return match.group(1).lower() if match else None


def _claude_budget_readings(
    cards: list[dict], model: str, account: str | None
) -> tuple[list[account_budget.Reading], str | None]:
    """Keep account selection separate from validating that account's window data."""
    mine, gap = _claude_budget_cards(cards, account)
    if gap is not None:
        return [], gap
    readings: list[account_budget.Reading] = []
    for card in mine:
        windows = card.get("windows")
        for window in windows if isinstance(windows, list) else []:
            reading = _budget_window_reading(window, model)
            if reading is not None:
                readings.append(reading)
    return readings, None


def _budget_refusal(
    model: str,
    budget: account_budget.AccountBudget,
    report: dict,
    *,
    now: float,
    account: str | None,
) -> str | None:
    """Why the configured account budget (review-cli#696) holds a `claude:` dispatch on
    `account` (an email; None: the single implicit account) given the harness `report`
    (`load_harness_report`), else None. A report that failed, or is older than the
    budget's `max_age_seconds`, proves nothing about the account."""
    gap = None
    readings: list[account_budget.Reading] = []
    if report["status"] != "ok":
        gap = f"harness limits source {report['status']}"
    elif now - report["observed_at"] > budget.max_age_seconds:
        gap = (
            f"harness limits were read {int(now - report['observed_at'])}s ago "
            f"(> {budget.max_age_seconds:g}s)"
        )
    else:
        readings, gap = _claude_budget_readings(report["cards"], model, account)
    return account_budget.hold(
        budget,
        account_budget.PROVIDER_ANTHROPIC,
        readings,
        now=now,
        account=account,
        gap=gap,
    )


def _legacy_refuse_reason(
    model: str,
    *,
    samples: list[dict] | None,
    cards: list[dict] | None,
    now: float | None,
    runner,
    account: str | None,
) -> str | None:
    pct = usage_percent_for_model(
        model, samples=samples, cards=cards, now=now, runner=runner, account=account
    )
    if pct is None or pct < 100.0:
        return None
    family = _agent_family(model)
    if family is None:
        return None
    live_cards = cards if cards is not None else load_harness_cards(runner=runner)
    names = _exhausted_window_names(live_cards, family, account)
    if not names and samples is not None:
        names.extend(_exhausted_sample_names(family, samples))
    if not names:
        names = ["5-hour"]
    # Preserve 5-hour before weekly for a stable reason string.
    ordered = [n for n in ("5-hour", "weekly") if n in names]
    joined = " and ".join(ordered)
    return f"harness {joined} window exhausted (100%)"


def spawn_refuse_reason(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
    account: str | None = None,
) -> str | None:
    """Why a spawn of `model` must not happen, else None (allowed): the configured account
    budget's hold (review-cli#696, `account_budget`: starts with "Blocked by review-cli
    configured") for a `claude:` model, else the exhausted 5-hour/7-day window (100%).
    `account` scopes to one registered account (review-cli#168). One harness read serves
    both checks."""
    budget = account_budget.current()
    if budget is not None and _agent_family(model) == "claude":
        at = time.time() if now is None else now
        if cards is None:
            report = load_harness_report(runner=runner)
            cards = report["cards"]
            if samples is None:
                samples = load_snapshot()
        else:
            report = _harness_report("ok", cards, at, "injected", None)
        held = _budget_refusal(model, budget, report, now=at, account=account)
        if held is not None:
            return held
    return _legacy_refuse_reason(
        model, samples=samples, cards=cards, now=now, runner=runner, account=account
    )


def allow_spawn_for_model(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
    account: str | None = None,
) -> bool:
    """True unless `spawn_refuse_reason` has a reason: the configured account budget
    holds a `claude:` dispatch, or its more-restrictive 5-hour or 7-day window is at or
    above 100%. Without a configured budget missing data fails open (True). `account`
    scopes the check to one registered account (review-cli#168). This is the DISPATCH
    gate: board composition and panel padding read `usage_percent_for_model` (a near-limit
    heuristic that fails open) and never call it."""
    return (
        spawn_refuse_reason(
            model, samples=samples, cards=cards, now=now, runner=runner, account=account
        )
        is None
    )


def _family_cards(cards, family, account):
    for card in cards:
        if not isinstance(card, dict) or card.get("status") != "ok":
            continue
        if _family_from_provider(card.get("provider")) != family:
            continue
        if account is not None and _card_account(card) != account:
            continue
        yield card


def _known_window_percentages(cards, family, account):
    for card in _family_cards(cards, family, account):
        windows = card.get("windows")
        if not isinstance(windows, list):
            continue
        for window in windows:
            value = _window_percentage(window)
            if value is not None:
                yield value


def _window_percentage(window):
    if not isinstance(window, dict):
        return None
    kind = _window_kind(window.get("window_seconds"))
    percent = _finite_percent(window.get("used_percent"))
    if kind is None or percent is None:
        return None
    return kind, percent


def _freshest_tgctl_samples(family, samples):
    freshest: dict[str, dict] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        name = sample.get("limitName")
        if sample.get("agent") != family or name not in _RATE_LIMIT_NAMES:
            continue
        at = sample.get("sampledAt")
        if not isinstance(at, (int, float)):
            continue
        previous = freshest.get(name)
        if previous is None or at > previous["sampledAt"]:
            freshest[name] = sample
    return freshest


def _current_tgctl_percent(sample, cutoff_ms, now_ms):
    if sample["sampledAt"] < cutoff_ms:
        return None
    reset = sample.get("resetAt")
    if isinstance(reset, (int, float)) and reset < now_ms:
        return None
    percent = sample.get("percent")
    return percent if isinstance(percent, (int, float)) else None


def _claude_budget_cards(cards, account):
    ok = list(_family_cards(cards, "claude", None))
    wanted = _card_account({"account": account})
    if wanted is not None:
        mine = [card for card in ok if _card_account(card) == wanted]
        if mine:
            return mine, None
        others = " (only other accounts report)" if ok else ""
        return [], f"harness has no usage card for this account{others}"
    if not ok:
        return [], "harness reports no Claude usage card"
    if len({_card_account(card) for card in ok}) > 1:
        return (
            [],
            f"{len(ok)} Claude accounts report usage and this dispatch is not tied to one of them",
        )
    return ok, None


def _budget_window_reading(window, model):
    value = _window_percentage(window)
    if value is None:
        return None
    kind, percent = value
    tier = _tier_of(window)
    if tier is not None and tier not in model.lower():
        return None
    return account_budget.Reading(
        window=kind,
        percent=percent,
        resets_at=parse_time(window.get("resets_at")),
        shared=tier is None,
    )


def _exhausted_sample_names(family, samples):
    names = []
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("agent") != family:
            continue
        name = sample.get("limitName")
        percent = _finite_percent(sample.get("percent"))
        if name in _RATE_LIMIT_NAMES and percent is not None and percent >= 100.0:
            names.append(str(name))
    return names


def _decode_harness_report(text, observed_at, origin):
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        return _harness_report("malformed", [], observed_at, origin, str(exc))
    cards = data.get("cards") if isinstance(data, dict) else data
    if not isinstance(cards, list):
        return _harness_report(
            "malformed", [], observed_at, origin, "no card list in harness JSON"
        )
    return _harness_report(
        "ok",
        [card for card in cards if isinstance(card, dict)],
        observed_at,
        origin,
        None,
    )
