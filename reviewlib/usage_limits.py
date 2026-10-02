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

Accessed via: board/panel composition (`usage_percent_for_model`) and the
claude pre-dispatch spawn gate (`allow_spawn_for_model` in backends.py).
Tests inject cards via `cards=` / `runner=` / `$REVIEW_HARNESS_LIMITS_JSON`
so they never hit the network or the real `harness` binary.
"""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

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

# The single named threshold this whole feature uses by default (board
# composition, flat-panel padding, and this module's own helpers all import
# this ONE constant rather than each hardcoding "70.0" separately).
DEFAULT_LIMIT_THRESHOLD = 70.0

# Model-string prefix -> the tg-ctl `agent` family sharing its account quota.
# Extend this map as tg-ctl learns to track more agent families (see the module
# docstring's "coverage is PARTIAL" note). `codex:gpt-...` seats (e.g. SOL_SEAT
# in config.py) route through the SAME codex account as the bare `"codex"`
# seat, so they share the prefix mapping too.
#
# zai/xai/kimi-code (review-cli#skip-dblock-color): `backend_unavailable_reason`
# now checks `spawn_refuse_reason` for EVERY backend (not just claude's own
# dispatch), so a GLM/Grok/Kimi seat whose harness-tracked window is already
# 100% exhausted is pre-emptively skipped instead of dispatched-then-failed.
# `oc:zai/glm-5.2` (opencode transport, provider_failover.DEFAULT_PROVIDER_CHAINS)
# shares the SAME z.ai account as the direct `zai:glm-5.2` keyed-HTTP seat, so
# both prefixes map to the "zai" family. `oc:xai/...` (GROK_SEAT) is xai's ONLY
# route (review-cli has no diff-only xai backend -- see backends._AGENTIC_ONLY_PROVIDERS).
# `omp:kimi-code/...` is the sole kimi-code route (KIMI_SEAT itself routes through
# the commandcode gateway account, NOT kimi's own account, so it deliberately
# stays unmapped here).
_AGENT_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude:", "claude"),
    ("codex:", "codex"),
    ("zai:", "zai"),
    ("z.ai:", "zai"),
    ("zhipu:", "zai"),
    ("glm:", "zai"),
    ("oc:zai/", "zai"),
    ("opencode:zai/", "zai"),
    ("oc:xai/", "xai"),
    ("opencode:xai/", "xai"),
    ("omp:kimi-code/", "kimi"),
)
# Bare (non-prefixed) model ids routed through a specific agent's own account.
_AGENT_FAMILY_EXACT: dict[str, str] = {
    "codex": "codex",
    "zai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "glm": "zai",
}

# harness-cli LimitCard.provider -> the same family ids as above. Provider strings
# verified live via `harness limits --all --json` (2026-09-09): "z.ai", "x.ai",
# "kimi" (lower-cased below by `_family_from_provider`).
_HARNESS_PROVIDER_FAMILY = {
    "claude code": "claude",
    "codex": "codex",
    "z.ai": "zai",
    "x.ai": "xai",
    "kimi": "kimi",
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


def load_harness_cards(*, runner=None) -> list[dict]:
    """Cards from `$REVIEW_HARNESS_LIMITS_JSON` or `harness limits --all --json`.

    The env fixture is a JSON object (`{"cards":[...]}`), a JSON array of
    cards, or a path to either. `runner`, when given, is a zero-arg callable
    that returns that same JSON text — tests inject it so this never touches
    the real binary. Missing harness / unusable JSON / any error -> []."""
    try:
        raw = os.environ.get(_ENV_HARNESS_JSON)
        if raw is not None and raw.strip():
            text = raw.strip()
            if not text.startswith("{") and not text.startswith("["):
                text = Path(text).expanduser().read_text(encoding="utf-8")
        else:
            run = runner if runner is not None else _run_harness_limits_json
            text = run()
        data = json.loads(text)
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
        if isinstance(data, dict):
            cards = data.get("cards")
            if isinstance(cards, list):
                return [c for c in cards if isinstance(c, dict)]
        return []
    except Exception:  # noqa: BLE001 -- fail-open
        return []


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


def _window_kind(window_seconds: object) -> str | None:
    seconds = _finite_percent(window_seconds)
    if seconds is None or seconds <= 0:
        return None
    return _WINDOW_SECONDS_TO_NAME.get(seconds)


def _family_from_provider(provider: object) -> str | None:
    if not isinstance(provider, str):
        return None
    return _HARNESS_PROVIDER_FAMILY.get(provider.strip().lower())


def _percent_from_harness_cards(cards: list[dict], family: str) -> float | None:
    """Max used_percent across this family's 5-hour and 7-day windows.

    Cards with status != "ok" are skipped. Windows with used_percent None
    (or non-finite) are skipped. Identification is by window_seconds, never
    the English name."""
    best: float | None = None
    for card in cards:
        if not isinstance(card, dict):
            continue
        if card.get("status") != "ok":
            continue
        if _family_from_provider(card.get("provider")) != family:
            continue
        windows = card.get("windows")
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, dict):
                continue
            if _window_kind(window.get("window_seconds")) is None:
                continue
            pct = _finite_percent(window.get("used_percent"))
            if pct is None:
                continue
            if best is None or pct > best:
                best = pct
    return best


def _exhausted_window_names(cards: list[dict], family: str) -> list[str]:
    """5-hour / weekly names whose used_percent is >= 100 for `family`."""
    seen: dict[str, float] = {}
    for card in cards:
        if not isinstance(card, dict) or card.get("status") != "ok":
            continue
        if _family_from_provider(card.get("provider")) != family:
            continue
        windows = card.get("windows")
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, dict):
                continue
            kind = _window_kind(window.get("window_seconds"))
            if kind is None:
                continue
            pct = _finite_percent(window.get("used_percent"))
            if pct is None:
                continue
            current = seen.get(kind)
            if current is None or pct > current:
                seen[kind] = pct
    return [name for name, pct in seen.items() if pct >= 100.0]


def _percent_from_tgctl_samples(
    family: str, live_samples: list[dict], now: float | None
) -> float | None:
    if not live_samples:
        return None
    now_ms = (now if now is not None else time.time()) * 1000
    cutoff_ms = now_ms - _max_age_seconds() * 1000
    freshest_by_limit: dict[str, dict] = {}
    for sample in live_samples:
        if not isinstance(sample, dict):
            continue
        limit_name = sample.get("limitName")
        if sample.get("agent") != family or limit_name not in _RATE_LIMIT_NAMES:
            continue
        sampled_at = sample.get("sampledAt")
        if not isinstance(sampled_at, (int, float)):
            continue
        current = freshest_by_limit.get(limit_name)
        if current is None or sampled_at > current["sampledAt"]:
            freshest_by_limit[limit_name] = sample

    best: float | None = None
    for sample in freshest_by_limit.values():
        sampled_at = sample["sampledAt"]
        if sampled_at < cutoff_ms:
            continue
        reset_at = sample.get("resetAt")
        if isinstance(reset_at, (int, float)) and reset_at < now_ms:
            continue  # this sample's own window has already reset
        percent = sample.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        if best is None or percent > best:
            best = float(percent)
    return best


def usage_percent_for_model(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
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
    """
    family = _agent_family(model)
    if family is None:
        return None

    # Existing tests pass samples= and must not hit harness. Live path
    # (neither samples nor cards) prefers harness, then tg-ctl fallback.
    if cards is not None or samples is None:
        live_cards = cards if cards is not None else load_harness_cards(runner=runner)
        harness_pct = _percent_from_harness_cards(live_cards, family)
        if harness_pct is not None:
            return harness_pct
        if cards is not None and samples is None:
            return None

    live_samples = samples if samples is not None else load_snapshot()
    return _percent_from_tgctl_samples(family, live_samples, now)


def allow_spawn_for_model(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
) -> bool:
    """True unless this family's more-restrictive 5-hour or 7-day window is
    at or above 100%. Missing data fails open (True)."""
    pct = usage_percent_for_model(
        model, samples=samples, cards=cards, now=now, runner=runner
    )
    return pct is None or pct < 100.0


def spawn_refuse_reason(
    model: str,
    *,
    samples: list[dict] | None = None,
    cards: list[dict] | None = None,
    now: float | None = None,
    runner=None,
) -> str | None:
    """Human-readable reason naming the exhausted 5-hour/7-day window, or
    None when spawn is allowed."""
    if allow_spawn_for_model(
        model, samples=samples, cards=cards, now=now, runner=runner
    ):
        return None
    family = _agent_family(model)
    if family is None:
        return None
    live_cards = cards if cards is not None else load_harness_cards(runner=runner)
    names = _exhausted_window_names(live_cards, family)
    if not names and samples is not None:
        # tg-ctl samples use limitName already.
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            if sample.get("agent") != family:
                continue
            limit_name = sample.get("limitName")
            pct = _finite_percent(sample.get("percent"))
            if limit_name in _RATE_LIMIT_NAMES and pct is not None and pct >= 100.0:
                names.append(str(limit_name))
    if not names:
        names = ["5-hour"]
    # Preserve 5-hour before weekly for a stable reason string.
    ordered = [n for n in ("5-hour", "weekly") if n in names]
    joined = " and ".join(ordered)
    return f"harness {joined} window exhausted (100%)"
