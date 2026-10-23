"""`review capacity-check`: is EVERY route the active review board could use confirmed out
of quota right now? (review-cli#472)

Built for `gh ship`'s review-quorum / external-review gates (agent-tools ci/ship/ship.sh,
agent-tools#705): ship skips its manual hatch prompt only when this report says
`all_exhausted: true`. That assertion lets a review gate be passed without a review, so it
is made only on affirmative, fresh, applicable quota evidence for every candidate, and
everything else — no data, stale data, a failed source, a cooldown for some other reason,
an account that is merely logged out — keeps it false.

Candidates. The board is resolved exactly as a no-`-m` `review diff` resolves it
(`config.resolve_review_board`: `--preset` > config `models:` > config `board:` > the
default preset), every seat counts (pool and reserve alike: failover can promote any of
them), and each distinct seat model expands to the routes its provider failover would
dispatch (`provider_failover.provider_chain`, Claude CLI <-> omp twins included). A
Claude CLI route expands further to one candidate per account it rotates through
(`backends._claude_account_candidates`), and an omp seat on omp's Anthropic accounts
(`backends._review_omp_with_anthropic_pool`) to one per account the pool may dispatch on
(`omp_accounts.anthropic_accounts`, minus the account policy's excluded ones). One
candidate = one (route, account).

Evidence, per candidate, in this order (first decisive answer wins):
  1. the route's backend is not runnable (`backends.backend_unavailable_reason`: no
     CLI/key, unpaid provider, every Claude account's login failed) -> `unavailable`;
  1b. the omp account policy (review-cli#641) leaves an omp Anthropic seat no account
     (every one excluded, or invalid settings: dispatch refuses it) -> `unavailable`;
  2. the account's recorded login failure (`claude_accounts.login_failure`) ->
     `unavailable` (cause `auth`);
  3. an active route cooldown whose typed cause is not quota (`unavailable`, `stall`,
     `auth`) -> `unavailable`;
  4. harness-cli limit cards (`usage_limits.load_harness_report`) for the route's quota
     identity: a Claude CLI or omp Anthropic account's own card (matched by email), or the
     codex card(s).
     A 5-hour/weekly window at >= 100% that has not reset -> `exhausted`; every window
     below 100% on the one applicable card -> `usable`;
  5. an active cooldown with cause `quota`, recorded within the freshness window, on a
     route whose quota identity is the route itself or its only account, or under one omp
     pool account's own `omp-account:` key -> `exhausted`. On a multi-account Claude CLI
     route it is NOT attributable to any one account (the store keys routes, not
     accounts) -> `unknown`; with several omp pool accounts the route-level quota
     record names none of them and is not read (a non-quota one blocks them all; with
     one account the route record is that account's). The cooldowns of steps 3 and 5 are
     the timer cooldowns AND the availability windows (review-cli#663, the `window`
     access method, keyed by the route and by `provider:<name>`); the stored exclusion
     itself is not a step-1 probe failure (`honor_exclusions=False`);
  6. otherwise `unknown`, with the typed reason saying what was missing.

`all_exhausted` = at least one candidate AND every candidate `exhausted`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (
    availability,
    backends,
    claude_accounts,
    omp_accounts,
    seat_cooldown,
    usage_limits,
)
from .config import CONFIG_PATH, _agentic, load_config, resolve_review_board
from .provider_failover import configure_provider_chains, provider_chain

SCHEMA = "review-cli/capacity-check"
SCHEMA_VERSION = 2

# Older observations than this are not evidence of the CURRENT quota state. A cooldown is
# recorded once, when the failing call returns, and later runs skip the seat without
# re-recording it, so an old cooldown says the quota WAS out, not that it still is.
DEFAULT_MAX_AGE_SECONDS = 30 * 60
_ENV_MAX_AGE = "REVIEW_CAPACITY_MAX_AGE_SECONDS"

STATUS_EXHAUSTED = "exhausted"
STATUS_USABLE = "usable"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_EXHAUSTED, STATUS_USABLE, STATUS_UNAVAILABLE, STATUS_UNKNOWN)

# Typed reasons -> the status each one implies. The JSON carries the reason; consumers
# can rely on this mapping (pinned by tests/test_capacity_check.py).
REASONS: dict[str, str] = {
    "quota_window_exhausted": STATUS_EXHAUSTED,
    "quota_cooldown": STATUS_EXHAUSTED,
    "usage_below_limit": STATUS_USABLE,
    "backend_unavailable": STATUS_UNAVAILABLE,
    "login_failed": STATUS_UNAVAILABLE,
    "cooldown_not_quota": STATUS_UNAVAILABLE,
    "account_policy_refusal": STATUS_UNAVAILABLE,
    "cooldown_unclassified": STATUS_UNKNOWN,
    "cooldown_not_account_scoped": STATUS_UNKNOWN,
    "stale_observation": STATUS_UNKNOWN,
    "harness_card_not_ok": STATUS_UNKNOWN,
    "ambiguous_account": STATUS_UNKNOWN,
    "no_window_data": STATUS_UNKNOWN,
    "no_observation": STATUS_UNKNOWN,
}


def max_age_seconds() -> float:
    raw = os.environ.get(_ENV_MAX_AGE)
    try:
        value = float(raw) if raw is not None and raw.strip() else None
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return float(DEFAULT_MAX_AGE_SECONDS)
    return value


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> float | None:
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


def _verdict(
    reason: str,
    *,
    source: str,
    detail: str,
    observed_at: float | None = None,
    cause: str | None = None,
) -> dict:
    return {
        "status": REASONS[reason],
        "reason": reason,
        "cause": cause,
        "source": source,
        "observed_at": observed_at,
        "detail": detail,
    }


# --- harness cards ------------------------------------------------------------------


def _card_verdict(card: dict, observed_at: float, now: float) -> dict:
    """What one harness limit card proves about its account right now."""
    who = f"{card.get('provider')} {card.get('account') or '(no account)'}"
    if card.get("status") != "ok":
        return _verdict(
            "harness_card_not_ok",
            source="harness-limits",
            detail=f"{who}: card status {card.get('status')!r}",
            observed_at=observed_at,
        )
    exhausted: list[str] = []
    reset_since: list[str] = []
    below: list[float] = []
    windows = card.get("windows")
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        kind = usage_limits._window_kind(window.get("window_seconds"))
        pct = usage_limits._finite_percent(window.get("used_percent"))
        if kind is None or pct is None:
            continue
        if pct < 100.0:
            below.append(pct)
            continue
        resets_at = _parse_time(window.get("resets_at"))
        if resets_at is not None and resets_at <= now:
            reset_since.append(f"{kind} (reset at {_iso(resets_at)})")
        else:
            until = f", resets {_iso(resets_at)}" if resets_at is not None else ""
            exhausted.append(f"{kind} {pct:g}%{until}")
    if exhausted:
        return _verdict(
            "quota_window_exhausted",
            source="harness-limits",
            detail=f"{who}: {', '.join(exhausted)}",
            observed_at=observed_at,
            cause=seat_cooldown.CAUSE_QUOTA,
        )
    if reset_since:
        return _verdict(
            "stale_observation",
            source="harness-limits",
            detail=f"{who}: 100% window has reset since: {', '.join(reset_since)}",
            observed_at=observed_at,
        )
    if below:
        return _verdict(
            "usage_below_limit",
            source="harness-limits",
            detail=f"{who}: highest window {max(below):g}%",
            observed_at=observed_at,
        )
    return _verdict(
        "no_window_data",
        source="harness-limits",
        detail=f"{who}: no 5-hour/weekly window with a usage percent",
        observed_at=observed_at,
    )


def _cards_verdict(
    cards: list[dict], observed_at: float, now: float, max_age: float
) -> dict | None:
    """The verdict for a quota identity covered by `cards` (None when no card applies).
    Several cards with no way to tell which one the route uses: exhausted only when all
    of them are; otherwise ambiguous."""
    if not cards:
        return None
    if now - observed_at > max_age:
        return _verdict(
            "stale_observation",
            source="harness-limits",
            detail=f"harness limits read {int(now - observed_at)}s ago (> {max_age:g}s)",
            observed_at=observed_at,
        )
    verdicts = [_card_verdict(card, observed_at, now) for card in cards]
    if len(verdicts) == 1:
        return verdicts[0]
    if all(v["reason"] == "quota_window_exhausted" for v in verdicts):
        merged = dict(verdicts[0])
        merged["detail"] = "; ".join(v["detail"] for v in verdicts)
        return merged
    return _verdict(
        "ambiguous_account",
        source="harness-limits",
        detail=(
            f"{len(cards)} harness cards and no way to tell which account this route "
            "uses: " + "; ".join(v["detail"] for v in verdicts)
        ),
        observed_at=observed_at,
    )


def _family_cards(cards: list[dict], family: str) -> list[dict]:
    return [
        c
        for c in cards
        if usage_limits._family_from_provider(c.get("provider")) == family
    ]


# --- routes and candidates ------------------------------------------------------------


def _route_access(route: str) -> tuple[str | None, str | None]:
    """`(cooldown key, access method)` the route's dispatch records cooldowns under, or
    `(None, None)` for a backend that records none (codex, gemini, z.ai, ...)."""
    backend = backends.resolve_backend(route)
    if backend is backends.review_claude:
        return route, backends.claude_access_method()
    if backend is backends.review_opencode:
        return _agentic(route), backends._OPENCODE_ACCESS_METHOD
    if backend is backends.review_omp:
        return route, backends._OMP_ACCESS_METHOD
    return None, None


def _omp_pool(
    route: str, now: float
) -> tuple[list[omp_accounts.OmpAccount], str | None]:
    """`(accounts, refusal)` for an omp seat on `route` as `backends.review_omp` resolves
    them: the omp Anthropic accounts its pool may dispatch on, or why the account policy
    (review-cli#641) leaves none. `([], None)` for any other route, and for an omp
    Anthropic seat with no enabled account and no policy (omp picks its own credential).
    `omp usage` is not read: an account a usage threshold would skip still counts, which
    only widens the candidate set the report needs exhausted evidence for (conservative:
    it can keep `all_exhausted` false, never make it true)."""
    omp_model = route.split(":", 1)[1] if ":" in route else None
    if not backends._omp_selector_is_anthropic(omp_model):
        return [], None
    if backends._review_omp_claude_profiles() != [None]:
        return [], backends._omp_profiles_policy_reason(omp_model)
    db = backends._omp_auth_db()
    refusal, eligible, _skipped = omp_accounts.pool_refusal(
        backends._omp_account_policy(),
        omp_accounts.anthropic_accounts(db),
        db,
        None,
        omp_model,
        int(now * 1000),
    )
    return eligible, refusal


def _route_candidates(route: str, cards: list[dict], now: float) -> list[dict]:
    """One candidate identity per account the route can dispatch on."""
    backend = backends.resolve_backend(route)
    key, method = _route_access(route)
    base = {
        "route": route,
        "backend": backends.provider_route_name(route),
        "access_method": method,
        "cooldown_keys": [] if key is None else [key],
        # where `call_backend` records the seat's availability window: the route itself,
        # and `provider:<name>` when the provider's whole account is exhausted
        "window_keys": [route, *filter(None, [availability.provider_key(route)])],
        # a route that rotates through several accounts: a quota record on the route
        # names none of them, a non-quota one blocks all of them
        "window_quota_unattributable": False,
        "route_cooldown_keys": [],
        # why dispatch refuses the route outright (omp account policy), else None
        "refusal": None,
        # only a Claude CLI account has a `claude_accounts` login to check
        "checks_cli_login": False,
    }
    if backend is backends.review_claude and method == "cli":
        accounts = backends._claude_account_candidates()
        multi = len(accounts) > 1
        out = []
        for config_dir, email in accounts:
            name = email or (Path(config_dir).name if config_dir else None)
            out.append(
                {
                    **base,
                    "account": name,
                    "account_dir": config_dir,
                    "account_email": email,
                    "quota_identity": f"claude-cli:{name or 'default'}",
                    "quota_identity_basis": "account",
                    "harness_family": "claude",
                    "route_accounts": len(accounts),
                    "multi_account": multi,
                    "checks_cli_login": True,
                }
            )
        return out
    pool, refusal = (
        _omp_pool(route, now) if backend is backends.review_omp else ([], None)
    )
    if pool:
        # `_review_omp_with_anthropic_pool` records each limit under the account's own
        # cooldown key (a chronic one also under the route key), so that key's evidence
        # is attributable to the account alone. With ONE account the route key is its
        # evidence too; with several it names none of them and is not read.
        out = []
        for account in pool:
            label = account.label
            keys = [backends._omp_account_cooldown_key(account)]
            if len(pool) == 1 and key is not None:
                keys.append(key)
            out.append(
                {
                    **base,
                    "cooldown_keys": keys,
                    "window_quota_unattributable": len(pool) > 1,
                    "route_cooldown_keys": (
                        [key] if len(pool) > 1 and key is not None else []
                    ),
                    "account": label,
                    "account_dir": None,
                    "account_email": account.email,
                    "quota_identity": f"omp-anthropic:{label}",
                    "quota_identity_basis": "account",
                    # harness-cli's Claude Code card of the same login (by email)
                    "harness_family": "claude" if account.email else None,
                    "route_accounts": len(pool),
                    # False: the account's own cooldown key IS attributable; the card
                    # filter keys on `account_email` (see `_candidate_verdict`)
                    "multi_account": False,
                }
            )
        return out
    if backend is backends.review_codex:
        codex_cards = _family_cards(cards, "codex")
        named = sorted({str(c["account"]) for c in codex_cards if c.get("account")})
        account = named[0] if len(named) == 1 else None
        return [
            {
                **base,
                "account": account,
                "account_dir": None,
                "account_email": None,
                "quota_identity": f"codex:{account or 'default'}",
                "quota_identity_basis": "account",
                "harness_family": "codex",
                "route_accounts": 1,
                "multi_account": False,
            }
        ]
    return [
        {
            **base,
            "account": None,
            "account_dir": None,
            "account_email": None,
            "quota_identity": f"route:{route}" + (f"@{method}" if method else ""),
            "quota_identity_basis": "route",
            "harness_family": None,
            "route_accounts": 1,
            "refusal": refusal,
            "multi_account": False,
        }
    ]


def _recorded_at_key(item: tuple[str | None, dict]) -> float:
    recorded_at = item[1]["recorded_at"]
    return -math.inf if recorded_at is None else recorded_at


def _pick_cooldown(
    found: list[tuple[str | None, dict]], default_method: str | None
) -> tuple[str | None, dict | None]:
    """The one record that decides a candidate: the freshest typed non-quota record first
    (a login or stall record is never masked by quota evidence), else the freshest typed
    quota record, else an untyped one (which never counts as quota)."""
    blocking = [
        item
        for item in found
        if item[1]["cause"] not in (None, seat_cooldown.CAUSE_QUOTA)
    ]
    if blocking:
        return max(blocking, key=_recorded_at_key)
    quota = [item for item in found if item[1]["cause"] == seat_cooldown.CAUSE_QUOTA]
    if quota:
        return max(quota, key=_recorded_at_key)
    return found[0] if found else (default_method, None)


def _candidate_verdict(
    cand: dict,
    *,
    unavailable_reason: str | None,
    harness: dict,
    now: float,
    max_age: float,
) -> dict:
    if unavailable_reason is not None:
        return _verdict(
            "backend_unavailable", source="backend-probe", detail=unavailable_reason
        )
    if cand["refusal"]:
        return _verdict(
            "account_policy_refusal",
            source="omp-account-policy",
            detail=cand["refusal"],
        )
    if cand["checks_cli_login"]:
        account_dir = Path(cand["account_dir"]) if cand["account_dir"] else None
        failure = claude_accounts.login_failure(account_dir)
        if failure is not None:
            return _verdict(
                "login_failed",
                source="claude-accounts",
                detail=failure["reason"],
                observed_at=failure["recorded_at"],
                cause=seat_cooldown.CAUSE_AUTH,
            )
    # A candidate can have two keys (an omp pool's only account: its own, then the
    # route's). A typed non-quota cooldown on either outranks quota evidence, so a
    # login/stall record is never masked by an older quota one.
    # The seat's availability WINDOW (review-cli#663: an exclusion until the time the
    # failure itself named) is stored under the transport-independent `window` method with
    # the same typed cause, keyed by the seat's route (and `provider:<name>` for a
    # provider-wide failure). It is read like a timer cooldown: a quota window is quota
    # evidence under the same freshness and account-identity rules, any other cause (auth,
    # unavailable, rate_limit, stall) is non-quota evidence. A route that rotates through
    # several accounts records its window on the route, so only its non-quota windows
    # (which block every account) count there; a quota window names no single account.
    found = [
        (cand["access_method"], entry)
        for key in cand["cooldown_keys"]
        if (
            entry := seat_cooldown.active_cooldown(
                key, access_method=cand["access_method"], now=now
            )
        )
        is not None
    ]
    for key in cand["route_cooldown_keys"]:
        entry = seat_cooldown.active_cooldown(
            key, access_method=cand["access_method"], now=now
        )
        if entry is not None and entry["cause"] not in (
            None,
            seat_cooldown.CAUSE_QUOTA,
        ):
            found.append((cand["access_method"], entry))
    for key in cand["window_keys"]:
        entry = seat_cooldown.active_cooldown(
            key, access_method=seat_cooldown.WINDOW_ACCESS_METHOD, now=now
        )
        if entry is None:
            continue
        if cand["window_quota_unattributable"] and entry["cause"] in (
            None,
            seat_cooldown.CAUSE_QUOTA,
        ):
            continue
        found.append((seat_cooldown.WINDOW_ACCESS_METHOD, entry))
    method, cooldown = _pick_cooldown(found, cand["access_method"])
    source = f"seat-cooldown:{method}"
    if cooldown is not None and cooldown["cause"] not in (
        None,
        seat_cooldown.CAUSE_QUOTA,
    ):
        return _verdict(
            "cooldown_not_quota",
            source=source,
            detail=cooldown["reason"],
            observed_at=cooldown["recorded_at"],
            cause=cooldown["cause"],
        )

    harness_verdict = None
    if cand["harness_family"] is not None and harness["status"] == "ok":
        cards = _family_cards(harness["cards"], cand["harness_family"])
        if cand["multi_account"] or cand["account_email"]:
            email = (cand["account_email"] or "").strip().lower()
            cards = [
                c for c in cards if email and usage_limits._card_account(c) == email
            ]
        harness_verdict = _cards_verdict(cards, harness["observed_at"], now, max_age)
        if harness_verdict is not None and harness_verdict["status"] in (
            STATUS_EXHAUSTED,
            STATUS_USABLE,
        ):
            return harness_verdict

    if cooldown is not None and cooldown["cause"] == seat_cooldown.CAUSE_QUOTA:
        recorded_at = cooldown["recorded_at"]
        if cand["multi_account"]:
            return _verdict(
                "cooldown_not_account_scoped",
                source=source,
                detail=(
                    f"{cooldown['reason']} — recorded for the route, which rotates "
                    f"through {cand['route_accounts']} accounts; not attributable to "
                    "this one"
                ),
                observed_at=recorded_at,
                cause=seat_cooldown.CAUSE_QUOTA,
            )
        if recorded_at is None or now - recorded_at > max_age:
            age = (
                "unknown age"
                if recorded_at is None
                else f"{int(now - recorded_at)}s old"
            )
            return _verdict(
                "stale_observation",
                source=source,
                detail=f"{cooldown['reason']} ({age}, > {max_age:g}s)",
                observed_at=recorded_at,
                cause=seat_cooldown.CAUSE_QUOTA,
            )
        return _verdict(
            "quota_cooldown",
            source=source,
            detail=cooldown["reason"],
            observed_at=recorded_at,
            cause=seat_cooldown.CAUSE_QUOTA,
        )
    if cooldown is not None:
        return _verdict(
            "cooldown_unclassified",
            source=source,
            detail=f"{cooldown['reason']} (recorded without a typed cause)",
            observed_at=cooldown["recorded_at"],
        )
    if harness_verdict is not None:
        return harness_verdict
    if cand["harness_family"] is None:
        detail = "no quota source covers this route"
    elif harness["status"] != "ok":
        detail = f"harness limits source {harness['status']}"
        if harness.get("detail"):
            detail += f": {harness['detail']}"
    else:
        detail = f"no harness limit card for {cand['quota_identity']}"
    return _verdict("no_observation", source="none", detail=detail)


# --- report ---------------------------------------------------------------------------


def _configuration(board: list, source: str, preset: str | None) -> dict:
    seats = [
        {"model": r.model, "role": r.role, "display": r.display, "effort": r.effort}
        for r in board
    ]
    digest = hashlib.sha256(
        json.dumps(seats, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "source": source,
        "preset": preset,
        "config_path": str(CONFIG_PATH),
        "config_present": CONFIG_PATH.exists(),
        "board_fingerprint": f"sha256:{digest[:16]}",
        "seats": seats,
    }


def _empty_report(now: float, max_age: float, error: str) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "checked_at": _iso(now),
        "max_age_seconds": max_age,
        "configuration": None,
        "sources": {},
        "candidates": [],
        "coverage": {
            "candidates": 0,
            **{status: 0 for status in STATUSES},
            "complete": False,
        },
        "models": [],
        "exhausted": [],
        "all_exhausted": False,
        "error": error,
    }


def board_capacity_report(
    *,
    config: dict | None = None,
    preset: str | None = None,
    now: float | None = None,
) -> dict:
    """The versioned capacity report (schema `SCHEMA` v`SCHEMA_VERSION`) for the board a
    no-`-m` `review diff [--preset NAME]` would run. Never raises: a board that cannot be
    resolved is an empty report with `error` set and `all_exhausted: false`."""
    now = time.time() if now is None else now
    max_age = max_age_seconds()
    try:
        config = load_config() if config is None else config
        backends.configure_unpaid_providers(config.get("unpaid_providers"))
        backends.configure_omp_bin(config.get("omp_bin"))
        backends.configure_omp_accounts(config.get("omp_accounts"))
        configure_provider_chains(config.get("provider_chains"))
        board, source, used_preset = resolve_review_board(config, preset=preset)
    except Exception as exc:  # noqa: BLE001 - reported in the JSON, never raised
        return _empty_report(now, max_age, f"{type(exc).__name__}: {exc}")

    harness = usage_limits.load_harness_report()
    unavailable: dict[str, str | None] = {}

    def _unavailable(route: str) -> str | None:
        if route not in unavailable:
            unavailable[route] = backends.backend_unavailable_reason(
                route, honor_exclusions=False
            )
        return unavailable[route]

    models = list(dict.fromkeys(r.model for r in board))
    candidates: list[dict] = []
    for model in models:
        routes = provider_chain(
            model,
            available=lambda c: _unavailable(c) is None,
            unpaid=backends.runtime_provider_marked_unpaid,
        )
        for route in routes:
            for cand in _route_candidates(route, harness["cards"], now):
                verdict = _candidate_verdict(
                    cand,
                    unavailable_reason=_unavailable(route),
                    harness=harness,
                    now=now,
                    max_age=max_age,
                )
                observed_at = verdict.pop("observed_at")
                candidates.append(
                    {
                        "seat_model": model,
                        "route": cand["route"],
                        "backend": cand["backend"],
                        "access_method": cand["access_method"],
                        "account": cand["account"],
                        "quota_identity": cand["quota_identity"],
                        "quota_identity_basis": cand["quota_identity_basis"],
                        **verdict,
                        "observed_at": _iso(observed_at),
                        "age_seconds": (
                            None if observed_at is None else round(now - observed_at, 1)
                        ),
                    }
                )

    counts = {status: 0 for status in STATUSES}
    for cand in candidates:
        counts[cand["status"]] += 1
    total = len(candidates)
    all_exhausted = total > 0 and counts[STATUS_EXHAUSTED] == total
    accounts = claude_accounts.list_accounts()
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "checked_at": _iso(now),
        "max_age_seconds": max_age,
        "configuration": _configuration(board, source, used_preset),
        "sources": {
            "harness_limits": {
                "status": harness["status"],
                "origin": harness["origin"],
                "observed_at": _iso(harness["observed_at"]),
                "cards": len(harness["cards"]),
                "detail": harness["detail"],
            },
            "seat_cooldown": {
                "status": seat_cooldown.store_status(),
                "path": str(seat_cooldown.cooldown_path()),
            },
            "claude_accounts": {
                "registry": str(claude_accounts.accounts_root()),
                "accounts": len(accounts),
            },
        },
        "candidates": candidates,
        "coverage": {
            "candidates": total,
            **counts,
            "complete": total > 0 and counts[STATUS_UNKNOWN] == 0,
        },
        # Schema v1 fields, kept for consumers that read only these (agent-tools ship.sh
        # reads `all_exhausted` and `exhausted[].model/.evidence/.source`).
        "models": models,
        "exhausted": [
            {
                "model": c["seat_model"],
                "source": c["source"],
                "evidence": (
                    f"{c['route']}"
                    + (f" [{c['account']}]" if c["account"] else "")
                    + f": {c['detail']}"
                ),
            }
            for c in candidates
            if c["status"] == STATUS_EXHAUSTED
        ],
        "all_exhausted": all_exhausted,
        "error": None,
    }


def render_text(report: dict) -> str:
    """Human-readable form of `board_capacity_report`'s result."""
    if report.get("error"):
        return f"[review capacity-check] board resolution failed: {report['error']}\n"
    conf = report["configuration"]
    cov = report["coverage"]
    lines = [
        f"[review capacity-check] board {conf['source']} ({conf['board_fingerprint']}), "
        f"{len(report['models'])} model(s), {cov['candidates']} candidate route/account(s)",
        "  sources: "
        + ", ".join(
            f"{name}={info['status']}"
            for name, info in report["sources"].items()
            if "status" in info
        ),
    ]
    for status in STATUSES:
        rows = [c for c in report["candidates"] if c["status"] == status]
        if not rows:
            continue
        lines.append(f"{status}:")
        for c in rows:
            account = f" [{c['account']}]" if c["account"] else ""
            seen = f", observed {c['observed_at']}" if c["observed_at"] else ""
            lines.append(
                f"  - {c['route']}{account} ({c['reason']}{seen}): {c['detail']}"
            )
    lines.append(f"all_exhausted={'true' if report['all_exhausted'] else 'false'}")
    return "\n".join(lines) + "\n"
