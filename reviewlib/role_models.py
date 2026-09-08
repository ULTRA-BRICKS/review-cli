"""Per-role fallback model lists and harness seat resolution.

What: each REVIEW_ROLES key publishes an ordered tuple of bare model ids —
the identity a role falls back through — and those ids (plus a config
`model:` value) resolve onto a harness-prefixed seat.

Accessed via: `review --show-board` (footer from format_role_fallback_section);
config._expand_alias (seat_for_model after the MODEL_ALIASES lookup);
panel.run_board_with_failover and config.select_pool / split_pool_reserve
(next_role_fallback on seat fail/unavailable, before a different-role reserve).

Assumptions: roles name bare model ids; the harness prefix is derived once
via BOARD_MODEL_SEATS. Unknown ids are returned unchanged — never guessed.
next_role_fallback only walks the list when the failed model is ON it;
a custom seat of that role still falls through to reserve.

Past bugs: leftover of review-cli#364 + 175-ask 168/170 — roles still named
harness seats, and a bare id fell through to the opencode catch-all.
Listing-only after review-cli#449: the lists printed but dispatch still
popped the next-priority reserve (Grok/security -> DeepSeek/tests).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

ROLE_FALLBACK_MODELS: dict[str, tuple[str, ...]] = {
    "architect": ("claude-fable-5", "claude-opus-4-8"),
    "correctness": ("claude-opus-4-8", "gpt-6-astra"),
    "consistency": ("gpt-5.6-sol", "gpt-6-astra"),
    "performance": ("zai-org/GLM-5.2", "gpt-5.6-terra"),
    "quality": ("moonshotai/Kimi-K2.7-Code", "claude-sonnet-5"),
    "security": ("Qwen/Qwen3.7-Max", "grok-4.6", "glm-5.2"),
    "tests": ("deepseek/deepseek-v4-pro", "gpt-5.6-terra"),
    "contracts": ("gemini", "gpt-6-astra"),
}


def _board_model_seats() -> dict[str, str]:
    """Bare model id -> harness-prefixed seat. Lazy-imports config constants
    so this module can load without a circular import of config.py.
    """
    from . import config as c

    return {
        "claude-fable-5": c.FABLE_SEAT,
        "claude-opus-4-8": "claude:claude-opus-4-8",
        "gpt-6-astra": c.ASTRA_SEAT,
        "gpt-5.6-sol": c.SOL_SEAT,
        "zai-org/GLM-5.2": c.GLM_COMMANDCODE_SEAT,
        "gpt-5.6-terra": c.TERRA_SEAT,
        "moonshotai/Kimi-K2.7-Code": c.KIMI_SEAT,
        "claude-sonnet-5": c.SONNET_SEAT,
        "Qwen/Qwen3.7-Max": "oc:commandcode/Qwen/Qwen3.7-Max",
        "grok-4.6": c.GROK_SEAT,
        "glm-5.2": "zai:glm-5.2",
        "deepseek/deepseek-v4-pro": "oc:commandcode/deepseek/deepseek-v4-pro",
        "gemini": "gemini",
    }


def seat_for_model(token: str) -> str:
    """Resolve a bare model id onto its harness-prefixed seat.

    Already-prefixed seats (`claude:…`, `codex:…`, `oc:…`, …) pass through
    unchanged. Unknown bare ids are returned as-is — do not guess.
    """
    if ":" in token:
        return token
    return _board_model_seats().get(token, token)


def bare_model_id(token: str) -> str:
    """Strip a harness prefix, leaving the model id a role list would name.

    `oc:provider/model` drops both the `oc:` transport and the provider
    segment (`oc:commandcode/moonshotai/Kimi-K2.7-Code` ->
    `moonshotai/Kimi-K2.7-Code`; `oc:xai/grok-4.6` -> `grok-4.6`).
    Other `harness:id` forms drop only the harness (`codex:gpt-5.6-sol` ->
    `gpt-5.6-sol`). A token with no colon is already bare.
    """
    if ":" not in token:
        return token
    prefix, rest = token.split(":", 1)
    if prefix in ("oc", "opencode") and "/" in rest:
        return rest.split("/", 1)[1]
    return rest


def _identity_keys(token: str) -> frozenset[str]:
    """Seat, bare id, and original token — so used_models can name any form."""
    seat = seat_for_model(token)
    return frozenset({token, seat, bare_model_id(token), bare_model_id(seat)})


def next_role_fallback(failed, used_models: Iterable[str] = ()):
    """Next same-role seat after `failed`, or None if the role list is exhausted.

    Walks ROLE_FALLBACK_MODELS[failed.role] in order, resolves each id via
    seat_for_model, and skips `failed.model` plus anything in `used_models`.
    Returns replace(failed, model=next_seat) so role and effort stay put.

    If the failed model is not on that role's list (a custom m-pool seat),
    returns None immediately so the caller can pop the next-priority reserve.
    """
    models = ROLE_FALLBACK_MODELS.get(getattr(failed, "role", ""), ())
    if not models:
        return None
    failed_keys = _identity_keys(failed.model)
    if not any(_identity_keys(mid) & failed_keys for mid in models):
        return None
    used_keys = set(failed_keys)
    for token in used_models:
        used_keys |= _identity_keys(token)
    for mid in models:
        seat = seat_for_model(mid)
        if _identity_keys(seat) & used_keys:
            continue
        return replace(failed, model=seat)
    return None


def format_role_fallback_section() -> str:
    """Footer for `--show-board`: every role and the bare ids it falls back through."""
    lines = ["", "Fallbacks per role (bare model ids; harness auto-resolved):"]
    for role, models in ROLE_FALLBACK_MODELS.items():
        lines.append(f"  {role}: {', '.join(models)}")
    return "\n".join(lines)
