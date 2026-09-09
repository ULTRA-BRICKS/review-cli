"""Per-role fallback model lists and harness seat resolution.

What: each REVIEW_ROLES key publishes an ordered tuple of bare model ids —
the identity a role falls back through — and those ids (plus a config
`model:` value) resolve onto a harness-prefixed seat.

Accessed via: `review --show-board` (footer from format_role_fallback_section)
and config._expand_alias (seat_for_model after the MODEL_ALIASES lookup).

Assumptions: roles name bare model ids; the harness prefix is derived once
via BOARD_MODEL_SEATS. Unknown ids are returned unchanged — never guessed.

Past bugs: leftover of review-cli#364 + 175-ask 168/170 — roles still named
harness seats, and a bare id fell through to the opencode catch-all.
"""

from __future__ import annotations

ROLE_FALLBACK_MODELS: dict[str, tuple[str, ...]] = {
    "architect": ("claude-fable-5", "claude-opus-4-8"),
    "correctness": ("claude-opus-4-8", "gpt-6-astra"),
    "consistency": ("gpt-5.6-sol", "gpt-6-astra"),
    "performance": ("zai-org/GLM-5.2", "gpt-5.6-terra"),
    # review-cli#168: "claude-sonnet-5-omp" is a THIRD quality fallback, the SAME
    # claude-sonnet-5 model dispatched through the omp harness instead of the direct
    # claude CLI -- a genuinely different auth path, so it survives a Claude-account-
    # wide outage that would otherwise take out both Kimi's commandcode reserve AND
    # the direct Sonnet seat.
    "quality": ("moonshotai/Kimi-K2.7-Code", "claude-sonnet-5", "claude-sonnet-5-omp"),
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
        # review-cli#168: "claude-sonnet-5-omp" is a review-cli-INVENTED display id
        # (not a stripped-prefix derivation of OMP_CLAUDE_SEAT -- see bare_model_id's
        # special case below), distinguishing the omp-hosted seat from the direct
        # claude-sonnet-5 CLI seat above it in this same map.
        "claude-sonnet-5-omp": c.OMP_CLAUDE_SEAT,
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

    review-cli#168: `OMP_CLAUDE_SEAT` ("omp:anthropic/claude-sonnet-5") is a
    SPECIAL CASE, checked before the generic split -- the generic omp branch would
    otherwise strip it to "anthropic/claude-sonnet-5" (dropping the transport but
    keeping the provider segment, since "omp" is not in the oc/opencode branch
    above). "claude-sonnet-5-omp" is review-cli's own INVENTED display id (see
    `_board_model_seats`), not a stripped-prefix derivation, so it needs an
    explicit mapping rather than falling out of the split.
    """
    from . import config as c

    if token == c.OMP_CLAUDE_SEAT:
        return "claude-sonnet-5-omp"
    if ":" not in token:
        return token
    prefix, rest = token.split(":", 1)
    if prefix in ("oc", "opencode") and "/" in rest:
        return rest.split("/", 1)[1]
    return rest


def format_role_fallback_section() -> str:
    """Footer for `--show-board`: every role and the bare ids it falls back through."""
    lines = ["", "Fallbacks per role (bare model ids; harness auto-resolved):"]
    for role, models in ROLE_FALLBACK_MODELS.items():
        lines.append(f"  {role}: {', '.join(models)}")
    return "\n".join(lines)
