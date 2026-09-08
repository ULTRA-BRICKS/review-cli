#!/usr/bin/env python3
"""mode_quorum must fail closed when exhaustion removes every expert.

expand_flat_models_with_reuse can now return []. Running a moderator on that
empty transcript plus `all(result_is_usable(r) for r in [])` is vacuously
true, so a usable moderator would exit 0 with no expert answering.

Plain-script harness (mirrors tests/test_usage_limits.py).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.backends import ReviewResult  # noqa: E402
import reviewlib.modes.quorum as q_mod  # noqa: E402


def test_quorum_empty_experts_exits_nonzero_before_moderator():
    mod_calls = []

    def fake_panel(jobs, cwd, timeout):
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

    saved_panel = q_mod.run_panel
    saved_mod = q_mod.run_moderator
    q_mod.run_panel = fake_panel
    q_mod.run_moderator = fake_moderator
    try:
        with redirect_stdout(io.StringIO()):
            rc = q_mod.mode_quorum("q?", [], "", REPO_ROOT, 60, ["mod"])
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert rc != 0, rc
    assert mod_calls == [], "moderator ran on an empty expert transcript"


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
