"""Validate the provenance of a complete shared account-usage observation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

MAX_OBSERVATION_AGE_MS = 30 * 60 * 1000


# Structural protocol avoids a dependency on the pool implementation; both admission
# and display consume the same source evidence without an account-module import cycle.
class WindowEvidence(Protocol):
    label: str
    used_fraction: float
    resets_at_ms: int | None
    tier: str | None
    observed_at_ms: int | None
    shared: bool


def shared_key(window: WindowEvidence) -> str | None:
    if window.tier or not window.shared:
        return None
    label = window.label.lower().removeprefix("claude ").strip()
    return {
        "5 hour": "5h",
        "5h": "5h",
        "anthropic:5h": "5h",
        "7 day": "7d",
        "7d": "7d",
        "anthropic:7d": "7d",
    }.get(label)


def _shared_pair(windows):
    pair = {}
    for window in windows or ():
        key = shared_key(window)
        if key is None:
            continue
        if key in pair:
            return pair, "duplicate shared usage window"
        pair[key] = window
    if set(pair) != {"5h", "7d"}:
        return pair, "a complete shared 5-hour and 7-day observation is missing"
    return pair, None


def _time_problem(pair, now_ms):
    observed = [window.observed_at_ms for window in pair.values()]
    if any(stamp is None for stamp in observed):
        return "the provider observation timestamp is missing"
    if len(set(observed)) != 1:
        return "shared windows come from different observations"
    stamp = observed[0]
    if stamp > now_ms:
        return "the provider observation timestamp is in the future"
    if now_ms - stamp > MAX_OBSERVATION_AGE_MS:
        return "the provider observation is older than 30 minutes"
    return None


def _window_problem(pair, now_ms):
    for window in pair.values():
        if not math.isfinite(window.used_fraction) or window.used_fraction < 0:
            return "shared usage is invalid"
        if window.resets_at_ms is not None and window.resets_at_ms <= now_ms:
            return "a shared window reset after this observation"
    return None


def observation_problem(
    windows: Sequence[WindowEvidence] | None, now_ms: int
) -> str | None:
    """Unknown evidence refuses admission; never fill gaps from another observation."""
    pair, problem = _shared_pair(windows)
    if problem is not None:
        return problem
    return _time_problem(pair, now_ms) or _window_problem(pair, now_ms)


def confirmed_time(
    windows: Sequence[WindowEvidence] | None, now_ms: int
) -> float | None:
    """Original provider time in seconds, only for a coherent current shared pair."""
    if observation_problem(windows, now_ms) is not None:
        return None
    return next(w.observed_at_ms / 1000 for w in windows if shared_key(w) is not None)
