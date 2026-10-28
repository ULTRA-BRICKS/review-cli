"""Offline CLI regressions for capability-level routing in every text panel (#699)."""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewlib import cli  # noqa: E402
from reviewlib.config import preset_board  # noqa: E402

MODES = ("just-ask", "quorum", "brainstorm")
MODE_FUNCTIONS = {
    "just-ask": ("reviewlib.modes.just_ask", "mode_just_ask"),
    "quorum": ("reviewlib.modes.quorum", "mode_quorum"),
    "brainstorm": ("reviewlib.modes.brainstorm", "mode_brainstorm"),
}
SMALL = ["test:small-a", "test:small-b", "test:small-c"]
CONFIG = {
    "models": ["test:legacy-expensive"],
    "brainstorm_models": ["test:legacy-brainstorm"],
    "levels": {
        "test-small": {
            "pool": 3,
            "effort": "medium",
            "roles": {
                "correctness": [SMALL[0]],
                "consistency": [{"model": SMALL[1], "effort": "low"}],
                "quality": [SMALL[2]],
            },
            "moderators": ["test:small-moderator"],
            "escalate_to": "deep",
        }
    },
}


def invoke(mode, config, flags=(), *, dispatch=False, available=None):
    """Run actual CLI selection with a synthetic mode transport, never a live model."""
    out, err, captured = io.StringIO(), io.StringIO(), {}
    available = available or (lambda model: True)

    def handler(*args, **kwargs):
        captured["models"] = list(args[1])
        override = kwargs.get("effort_override")
        captured["efforts"] = [
            override.effort_for(model) if override else None for model in args[1]
        ]
        return 0

    def run_stats(_mode, _models, run, *args, **kwargs):
        captured["level"] = kwargs.get("level")
        return run()

    with tempfile.TemporaryDirectory(prefix="panel-level-test-") as home:
        env = {
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "REVIEW_STATS_FILE": str(Path(home) / "stats.jsonl"),
            "REVIEW_TASKREF_VALIDATE": "0",
            "REVIEW_TASK_CODE": "",
            "REVIEW_HEALTH_PREFLIGHT": "0",
            "REVIEW_ADMISSION_DIR": str(Path(home) / "admission"),
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch.object(cli, "load_config", lambda: copy.deepcopy(config)))
            stack.enter_context(patch.object(cli, "_read_stdin_if_piped", lambda: ""))
            stack.enter_context(patch.object(cli.backends, "backend_available", available))
            stack.enter_context(patch.object(cli, "_chain_aware_available", available))
            stack.enter_context(patch.object(cli.usage_limits, "load_snapshot", lambda: []))
            stack.enter_context(patch.object(cli.usage_limits, "load_harness_cards", lambda: []))
            stack.enter_context(patch.object(cli, "usage_percent_for_model", lambda *a, **kw: 0))
            stack.enter_context(patch.object(cli, "_run_mode_with_stats", run_stats))
            module, name = MODE_FUNCTIONS[mode]
            stack.enter_context(patch.object(importlib.import_module(module), name, handler))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            argv = [mode, "synthetic routing check", "--task", "PANEL-699", *flags]
            if not dispatch:
                argv.append("--list-defaults")
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code
    return rc, out.getvalue(), err.getvalue(), captured


def test_explicit_level_overrides_legacy_roster_in_all_panel_listings():
    for mode in MODES:
        rc, out, err, _ = invoke(mode, CONFIG, ["--level", "test-small"])
        assert rc == 0, (mode, err)
        assert out.splitlines() == SMALL, (mode, out)


def test_default_panel_roster_comes_from_configured_level():
    cfg = {"default_level": "test-small", "levels": CONFIG["levels"]}
    for mode in MODES:
        rc, out, err, _ = invoke(mode, cfg)
        assert rc == 0, (mode, err)
        assert out.splitlines() == SMALL, (mode, out)


def test_empty_config_panel_roster_comes_from_shipped_default_level():
    expected = [seat.model for seat in preset_board("routine", {})]
    for mode in MODES:
        rc, out, err, _ = invoke(mode, {})
        assert rc == 0, (mode, err)
        assert out.splitlines() == expected, (mode, out)


def test_explicit_models_win_over_a_selected_level():
    for mode in MODES:
        rc, out, err, _ = invoke(mode, CONFIG, ["--level", "test-small", "-m", "test:chosen"])
        assert rc == 0, (mode, err)
        assert out.splitlines() == ["test:chosen"], (mode, out)


def test_legacy_roster_is_preserved_without_a_level_flag():
    for mode in MODES:
        rc, out, err, _ = invoke(mode, CONFIG)
        assert rc == 0, (mode, err)
        expected = CONFIG["brainstorm_models"] if mode == "brainstorm" else CONFIG["models"]
        assert out.splitlines() == expected, (mode, out)


def test_unknown_panel_level_is_rejected_before_dispatch():
    for mode in MODES:
        rc, out, err, captured = invoke(mode, CONFIG, ["--level", "misspelled"])
        assert rc == 2, (mode, rc, out, err)
        assert not captured, captured


def test_actual_dispatch_uses_level_roster_effort_and_stats():
    for mode in MODES:
        rc, out, err, got = invoke(mode, CONFIG, ["--level", "test-small"], dispatch=True)
        assert rc == 0, (mode, rc, out, err)
        assert got["models"] == SMALL, (mode, got)
        assert got["efforts"] == ["medium", "low", "medium"], (mode, got)
        assert got["level"] == "test-small", (mode, got)


def test_explicit_effort_wins_over_level_seat_effort():
    for mode in MODES:
        rc, out, err, got = invoke(
            mode, CONFIG, ["--level", "test-small", "--effort", "high"], dispatch=True
        )
        assert rc == 0, (mode, rc, out, err)
        assert got["models"] == SMALL, (mode, got)
        assert got["efforts"] == ["high"] * 3, (mode, got)


def test_fallback_never_escapes_selected_level_when_legacy_is_available():
    # Legacy expensive seat remains available; only the chosen level's middle seat works.
    def available(model):
        return model in {SMALL[1], "test:legacy-expensive", "test:legacy-brainstorm", "test:small-moderator"}

    for mode in MODES:
        rc, out, err, got = invoke(mode, CONFIG, ["--level", "test-small"], dispatch=True, available=available)
        assert rc == 0, (mode, rc, out, err)
        assert got["models"] and set(got["models"]) == {SMALL[1]}, (mode, got)


def test_health_candidates_match_the_same_configured_panel_source():
    for mode in MODES:
        assert cli._health_seats(CONFIG, mode, "test-small", []) == SMALL
        assert cli._health_seats(CONFIG, mode, "test-small", ["test:chosen"]) == ["test:chosen"]


def test_default_help_names_the_configured_capability_level():
    cfg = {"default_level": "test-small", "levels": CONFIG["levels"]}
    with patch.object(cli, "load_config", lambda: cfg):
        for mode in MODES:
            help_text = cli._model_default_help(cli.get_mode(mode))
            assert "test-small" in help_text and SMALL[0] in help_text, help_text


def test_default_dispatch_records_the_canonical_level():
    cfg = {"default_level": "test-small", "levels": CONFIG["levels"]}
    for mode in MODES:
        rc, out, err, got = invoke(mode, cfg, dispatch=True)
        assert rc == 0, (mode, rc, out, err)
        assert got["models"] == SMALL and got["level"] == "test-small", (mode, got)


def test_exhausted_level_never_calls_the_available_legacy_roster():
    for mode in MODES:
        rc, out, err, got = invoke(
            mode, CONFIG, ["--level", "test-small"], dispatch=True,
            available=lambda model: model.startswith("test:legacy"),
        )
        assert rc != 0, (mode, out, err)
        assert not got, (mode, got)
        assert "selected level" in err, (mode, err)


def test_just_ask_never_duplicates_an_engine_or_runs_all_reserve_seats():
    cfg = copy.deepcopy(CONFIG)
    cfg["levels"]["test-small"]["roles"]["correctness"] = [SMALL[0], "test:reserve"]
    rc, out, err, got = invoke("just-ask", cfg, ["--level", "test-small", "--pool", "2"], dispatch=True)
    assert rc == 0, (out, err)
    assert got["models"] == SMALL[:2], got


def test_level_seat_effort_reaches_the_real_panel_job_constructor():
    from reviewlib.modes import just_ask
    from reviewlib.panel_selection import panel_effort, resolve_panel_selection

    selected = resolve_panel_selection(CONFIG, "just-ask", level="test-small")
    captured = []

    def transport(jobs, _cwd, _timeout):
        captured.extend((job.model, job.effort) for job in jobs)
        return []

    with patch.object(just_ask, "run_panel", transport):
        rc = just_ask.mode_just_ask(
            "synthetic", SMALL, "", Path.cwd(), 10,
            effort_override=panel_effort(selected, None),
        )
    assert rc == 0
    assert captured == list(zip(SMALL, ["medium", "low", "medium"])), captured


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(bool(failures))
