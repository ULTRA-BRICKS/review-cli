#!/usr/bin/env python3
"""Unit tests for per-role fallback model lists and harness seat resolution.

Standalone runner (`python3 tests/test_role_models.py`) and pytest-collectable.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import (  # noqa: E402
    ASTRA_SEAT,
    FABLE_SEAT,
    GLM_COMMANDCODE_SEAT,
    GROK_SEAT,
    KIMI_SEAT,
    REVIEW_ROLES,
    SOL_SEAT,
    SONNET_SEAT,
    TERRA_SEAT,
    _expand_alias,
    load_board,
)
from reviewlib.role_models import (  # noqa: E402
    ROLE_FALLBACK_MODELS,
    bare_model_id,
    format_role_fallback_section,
    seat_for_model,
)

_HARNESS_PREFIXES = ("claude:", "codex:", "oc:", "commandcode:", "zai:")


def test_every_review_role_has_bare_fallback_models():
    for role in REVIEW_ROLES:
        models = ROLE_FALLBACK_MODELS[role]
        assert isinstance(models, tuple), (role, type(models))
        assert len(models) >= 2, (role, models)
        for mid in models:
            assert isinstance(mid, str) and mid.strip(), (role, mid)
            for prefix in _HARNESS_PREFIXES:
                assert not mid.startswith(prefix), (role, mid)


def test_fallback_lists_are_the_published_identity():
    assert ROLE_FALLBACK_MODELS["architect"] == ("claude-fable-5", "claude-opus-4-8")
    assert ROLE_FALLBACK_MODELS["correctness"] == ("claude-opus-4-8", "gpt-6-astra")
    assert ROLE_FALLBACK_MODELS["consistency"] == ("gpt-5.6-sol", "gpt-6-astra")
    assert ROLE_FALLBACK_MODELS["performance"] == ("zai-org/GLM-5.2", "gpt-5.6-terra")
    assert ROLE_FALLBACK_MODELS["quality"] == (
        "moonshotai/Kimi-K2.7-Code",
        "claude-sonnet-5",
    )
    assert ROLE_FALLBACK_MODELS["security"] == (
        "Qwen/Qwen3.7-Max",
        "grok-4.6",
        "glm-5.2",
    )
    assert ROLE_FALLBACK_MODELS["tests"] == (
        "deepseek/deepseek-v4-pro",
        "gpt-5.6-terra",
    )
    assert ROLE_FALLBACK_MODELS["contracts"] == ("gemini", "gpt-6-astra")


def test_seat_for_model_maps_bare_ids_and_passes_prefixed_through():
    assert seat_for_model("gpt-6-astra") == "codex:gpt-6-astra"
    assert seat_for_model("claude-opus-4-8") == "claude:claude-opus-4-8"
    assert seat_for_model("grok-4.6") == "oc:xai/grok-4.6"
    assert seat_for_model("glm-5.2") == "zai:glm-5.2"
    assert seat_for_model("gemini") == "gemini"
    assert seat_for_model("codex:gpt-6-astra") == "codex:gpt-6-astra"
    assert seat_for_model("claude:claude-opus-4-8") == "claude:claude-opus-4-8"


def test_seat_for_model_sync_pins_config_constants():
    assert seat_for_model("gpt-5.6-sol") == SOL_SEAT
    assert seat_for_model("gpt-6-astra") == ASTRA_SEAT
    assert seat_for_model("gpt-5.6-terra") == TERRA_SEAT
    assert seat_for_model("claude-fable-5") == FABLE_SEAT
    assert seat_for_model("claude-sonnet-5") == SONNET_SEAT
    assert seat_for_model("grok-4.6") == GROK_SEAT
    assert seat_for_model("moonshotai/Kimi-K2.7-Code") == KIMI_SEAT
    assert not seat_for_model("moonshotai/Kimi-K2.7-Code").startswith("oc:")
    assert seat_for_model("zai-org/GLM-5.2") == GLM_COMMANDCODE_SEAT


def test_load_board_resolves_bare_model_to_harness_seat():
    board = load_board({"board": [{"model": "gpt-6-astra", "role": "consistency"}]})
    assert board[0].model == "codex:gpt-6-astra"
    prefixed = load_board(
        {"board": [{"model": "claude:claude-opus-4-8", "role": "correctness"}]}
    )
    assert prefixed[0].model == "claude:claude-opus-4-8"


def test_expand_alias_opus_and_bare_opus_id():
    assert _expand_alias("opus") == "claude:claude-opus-4-8"
    assert _expand_alias("claude-opus-4-8") == "claude:claude-opus-4-8"
    assert _expand_alias("glm") == "zai:glm-5.2"
    assert _expand_alias("gemini") == "gemini"
    assert _expand_alias("commandcode") == "commandcode"
    assert _expand_alias("codex") == "codex"


def test_format_role_fallback_section_lists_every_role():
    text = format_role_fallback_section()
    assert "Fallbacks per role" in text
    for role in REVIEW_ROLES:
        assert role in text, role
        for mid in ROLE_FALLBACK_MODELS[role]:
            assert mid in text, (role, mid)


def test_show_board_includes_fallback_section():
    from reviewlib import backends, cli

    saved_avail = backends.backend_available
    saved_load = cli.load_config
    try:
        backends.backend_available = lambda model: True
        cli.load_config = lambda: {}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board({})
        out = buf.getvalue()
    finally:
        backends.backend_available = saved_avail
        cli.load_config = saved_load
    assert rc == 0
    assert "Fallbacks per role" in out
    for role in REVIEW_ROLES:
        assert role in out, role
    assert "claude:claude-opus-4-8" in out
    assert "oc:xai/grok-4.6" in out


def test_bare_model_id_strips_harness_prefix():
    assert (
        bare_model_id("oc:commandcode/moonshotai/Kimi-K2.7-Code")
        == "moonshotai/Kimi-K2.7-Code"
    )
    assert bare_model_id("codex:gpt-5.6-sol") == "gpt-5.6-sol"
    assert bare_model_id("gemini") == "gemini"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
