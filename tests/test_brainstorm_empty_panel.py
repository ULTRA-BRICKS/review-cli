#!/usr/bin/env python3
"""mode_brainstorm must fail closed when exhaustion removes every seat.

expand_flat_models_with_reuse can now return []. `panel[slot % len(panel)]`
then raises ZeroDivisionError instead of exiting non-zero. Fail closed
before any slot assignment or moderator, matching mode_quorum.

Plain-script harness (mirrors tests/test_quorum_empty_experts.py).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _brainstorm_env_isolation import with_isolated_brainstorm_paths  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
import reviewlib.modes.brainstorm as bs  # noqa: E402


def test_brainstorm_empty_panel_exits_nonzero_without_zerodivision():
    mod_calls = []
    panel_calls = []

    def fake_panel(*_a, **_k):
        panel_calls.append(1)
        return []

    def fake_moderator(*_a, **_k):
        mod_calls.append(1)
        return ReviewResult(
            model="mod",
            command="mod",
            returncode=0,
            stdout="vacuous synthesis of nothing",
            stderr="",
        )

    saved_panel = bs.run_panel
    saved_mod = bs.run_moderator
    saved_slots = bs.run_role_slots
    bs.run_panel = fake_panel
    bs.run_moderator = fake_moderator
    bs.run_role_slots = fake_panel
    try:
        with redirect_stdout(io.StringIO()):
            rc = with_isolated_brainstorm_paths(
                lambda: bs.mode_brainstorm("topic?", [], REPO_ROOT, 60, ["mod"], 3, 3)
            )
    finally:
        bs.run_panel = saved_panel
        bs.run_moderator = saved_mod
        bs.run_role_slots = saved_slots
    assert rc != 0, rc
    assert mod_calls == [], "moderator ran on an empty brainstorm panel"
    assert panel_calls == [], "persona panel ran on an empty brainstorm panel"


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
