#!/usr/bin/env python3
"""Regression: the pool guard's liveness probe must consider provider failover.

codex P2 on review-cli#157 ("Let the guard consider live failover providers"):
`cli._evaluate_pool_or_bail` wired the guard's `available=` straight to
`backends.backend_available(model)` — a raw probe of the REQUESTED spelling only. In an
environment where the requested head provider is unavailable (e.g. no `ZAI_API_KEY`) but a
later provider in the model's failover chain is live (e.g. authenticated `oc:zai`), this
marked the seat DOWN and let the guard propose/exit — even though `provider_chain` would
have picked the live alternate at dispatch time. The guard's liveness check must agree with
what will actually be dispatched.

Offline: `backends.backend_available` / `backends.runtime_provider_marked_unpaid` /
`backends.backend_unavailable_reason` are patched with manual save/restore (not the pytest
`monkeypatch` fixture), so this also runs unchanged under the standalone `__main__` runner
(mirrors tests/test_reviewer_board.py's convention).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
from reviewlib import cli  # noqa: E402
from reviewlib.pool_guard import EXIT_UNSATISFIED  # noqa: E402


class _PatchBackends:
    """Save/restore `backends.backend_available` + `.backend_unavailable_reason` +
    `.runtime_provider_marked_unpaid` around a test body.

    `extra_attrs` (codex review finding, task-cli#139 round 3 -- DRY): additional
    `(obj, attr_name) -> new_value` patches applied/restored the SAME way, for a test
    that needs to control more than just the three backend globals (e.g. `cli.load_config`
    or `cli._read_stdin_if_piped` for a full `_dispatch(...)` end-to-end test) without
    hand-rolling its own save/patch/restore bookkeeping. `env` similarly save/restores
    os.environ entries (value `None` means "unset on exit")."""

    def __init__(
        self,
        *,
        available,
        reason,
        unpaid=lambda _m: False,
        extra_attrs: dict | None = None,
        env: dict | None = None,
    ):
        self._available, self._reason, self._unpaid = available, reason, unpaid
        self._extra_attrs = dict(extra_attrs or {})
        self._env = dict(env or {})

    def __enter__(self):
        self._saved = (
            backends.backend_available,
            backends.backend_unavailable_reason,
            backends.runtime_provider_marked_unpaid,
        )
        backends.backend_available = self._available
        backends.backend_unavailable_reason = self._reason
        backends.runtime_provider_marked_unpaid = self._unpaid
        self._saved_extra = {
            (obj, attr): getattr(obj, attr) for obj, attr in self._extra_attrs
        }
        for (obj, attr), value in self._extra_attrs.items():
            setattr(obj, attr, value)
        self._saved_env = {name: os.environ.get(name) for name in self._env}
        for name, value in self._env.items():
            os.environ[name] = value
        return self

    def __exit__(self, *exc):
        (
            backends.backend_available,
            backends.backend_unavailable_reason,
            backends.runtime_provider_marked_unpaid,
        ) = self._saved
        for (obj, attr), value in self._saved_extra.items():
            setattr(obj, attr, value)
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def test_guard_proceeds_when_only_a_failover_alternate_is_live():
    """glm53 -> zai:glm-5.3 (down, no key) -> oc:zai/glm-5.3 (live). The guard must NOT
    bail just because the head spelling is unavailable."""

    def _available(model: str) -> bool:
        return model == "oc:zai/glm-5.3"

    def _reason(model: str) -> str | None:
        return None if _available(model) else f"{model}: no key configured"

    with _PatchBackends(available=_available, reason=_reason):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            user_seats=(("glm53", "glm53"),),
            explicit_models=["glm53"],
            pool_arg=None,
            default_pool=1,
        )
    assert rc is None, "guard bailed even though a failover alternate is live"


def test_guard_still_bails_when_every_provider_in_the_chain_is_down():
    """Sanity check the other direction: the fix must not make the guard blind — if NO
    provider in the chain is reachable, it still reports down."""

    with _PatchBackends(
        available=lambda _m: False,
        reason=lambda m: f"{m}: no key configured",
    ):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            user_seats=(("glm53", "glm53"),),
            explicit_models=["glm53"],
            pool_arg=None,
            default_pool=1,
        )
    assert rc is not None, "guard proceeded even though every chain provider is down"


def test_guard_synthesizes_a_reason_when_the_head_reason_is_blank():
    """A defensive fallback (codex review of #157, finding 1): if `_guard_available` says
    the WHOLE chain is down but `backend_unavailable_reason` (which only probes the head
    spelling) happens to return None, the guard must still print a real reason, not a blank
    one. This shape isn't reachable through the real `backend_available`/
    `backend_unavailable_reason` pairing (both are backed by the same single source of
    truth), but a defensive synthesis costs nothing on the common path and closes the gap."""

    with _PatchBackends(
        available=lambda _m: False,  # nothing in the chain is reachable
        reason=lambda _m: None,  # ...yet the head-only reason probe is silent
    ):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._evaluate_pool_or_bail(
                config={},
                config_models=[],
                user_seats=(("glm53", "glm53"),),
                explicit_models=["glm53"],
                pool_arg=None,
                default_pool=1,
            )
    assert rc is not None, "guard should have bailed"
    assert (
        "no provider in its failover chain is currently available" in err.getvalue()
    ), err.getvalue()


def test_guard_bails_on_one_live_seat_against_the_real_default_board_without_the_fix():
    """Reproduces task-cli#139's exact CLI-dispatch-level symptom (codex review finding,
    P1): against the REAL `DEFAULT_BOARD` (many distinct roles) with only 1 seat live and
    a `--pool 3` request, the guard call `cli._dispatch` actually makes bails — UNLESS its
    caller passes `reuse_eligible=True`. This is the control: `reuse_eligible` defaults to
    False, so a caller that forgets to compute/pass it (a future refactor regression)
    reverts straight back to the pre-fix bail."""
    from reviewlib.config import DEFAULT_BOARD

    board_seats = tuple((r.model, r.display) for r in DEFAULT_BOARD)

    def _available(model: str) -> bool:
        return model == "codex:gpt-6-luna"

    with _PatchBackends(
        available=_available, reason=lambda m: f"{m}: session-limit cooldown"
    ):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            user_seats=board_seats,
            explicit_models=[],
            pool_arg=3,
            default_pool=4,
        )
    assert rc is not None, "expected the pre-fix strict floor to still bail here"


def test_guard_proceeds_on_one_live_seat_against_the_real_default_board_with_reuse_eligible():
    """The fix, at the exact same CLI-dispatch call site: passing `reuse_eligible=True`
    (what `cli._dispatch`'s board-path branch now computes for a non-`-m`, role-lensed
    board — `not explicit_models and any(r.role for r in board)`) makes the SAME
    scenario above PROCEED, so real dispatch reaches `select_pool_and_reserve_with_reuse`
    and pads Codex across the requested 3 roles instead of exiting 10 first."""
    from reviewlib.config import DEFAULT_BOARD

    board_seats = tuple((r.model, r.display) for r in DEFAULT_BOARD)
    assert any(r.role for r in DEFAULT_BOARD), (
        "sanity: DEFAULT_BOARD carries role metadata"
    )

    def _available(model: str) -> bool:
        return model == "codex:gpt-6-luna"

    with _PatchBackends(
        available=_available, reason=lambda m: f"{m}: session-limit cooldown"
    ):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            user_seats=board_seats,
            explicit_models=[],
            pool_arg=3,
            default_pool=4,
            reuse_eligible=True,
        )
    assert rc is None, "guard bailed even with reuse_eligible=True and 1 live seat"


def test_pool_zero_with_one_live_seat_still_bails_end_to_end():
    """Full `cli._dispatch(["diff", "--pool", "0", ...])`, codex review finding (round
    3): `--pool 0` means "all reachable seats" and `select_pool_with_reuse` deliberately
    never pads for `pool <= 0` (there's no fixed target to pad TOWARDS). Before this
    fix, the board-path branch computed `reuse_eligible = not explicit_models and
    any(r.role for r in board)` with NO check on the effective pool size, so a `--pool 0`
    run with only 1 live seat wrongly bypassed the 2-model convergence floor for a run
    that would have ended up with just that one seat under one role — zero compensating
    padding, a real regression from the reuse fix. This drives the REAL CLI dispatcher
    end to end (a real git repo + a real staged diff + `REVIEW_FAKE_BACKEND` so a
    wrongly-proceeding run wouldn't need real credentials to prove the point) and asserts
    the pre-existing safety floor still bails with EXIT_UNSATISFIED, unchanged from
    before task-cli#139."""

    def _config() -> dict:
        return {
            "board": [
                {"model": "seat-a", "role": "correctness", "name": "A"},
                {"model": "seat-b", "role": "consistency", "name": "B"},
                {"model": "seat-c", "role": "quality", "name": "C"},
            ]
        }

    err = io.StringIO()
    with _PatchBackends(
        available=lambda model: model == "seat-a",
        reason=lambda model: (
            None if model == "seat-a" else f"{model}: session-limit cooldown"
        ),
        extra_attrs={
            (cli, "load_config"): _config,
            (cli, "_read_stdin_if_piped"): lambda: None,
        },
        env={"REVIEW_FAKE_BACKEND": "1"},
    ):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(
                ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "t"], cwd=str(repo), check=True
            )
            (repo / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli._dispatch(
                    [
                        "diff",
                        "--task",
                        "TEST-1",
                        "--staged",
                        "--pool",
                        "0",
                        "-C",
                        str(repo),
                    ]
                )
    assert rc == EXIT_UNSATISFIED, (rc, err.getvalue())


def test_unknown_role_board_with_one_live_seat_still_bails_end_to_end():
    """Full `cli._dispatch(...)`. Originally (codex review finding, round 4) a board
    whose `role` strings are all typos/unknown `REVIEW_ROLES` keys was functionally
    role-less (every seat's `role_lens` resolved empty), and this test proved the
    pool-selection safety floor still bailed with `EXIT_UNSATISFIED` instead of a false
    PROCEED into a paid triple-dispatch of duplicate generic-prompt reviews.

    review-cli#422 moved that failure earlier: `load_board` now raises `BoardConfigError`
    on the FIRST typo'd role (`typo-a`) at board-COMPOSITION time, before the board ever
    reaches the pool guard/`select_pool_with_reuse` at all -- so this config never gets
    the chance to hit `EXIT_UNSATISFIED` anymore. This test now proves the fail-fast
    path instead: the real CLI dispatcher exits 2 (the generic `BoardConfigError` config-
    error code every sibling `except BoardConfigError` handler in cli.py returns) with a
    stderr message naming the bad role, never a raw traceback and never a silent
    triple-dispatch of duplicate generic-prompt reviews."""

    def _config() -> dict:
        return {
            "board": [
                {"model": "seat-a", "role": "typo-a", "name": "A"},
                {"model": "seat-b", "role": "typo-b", "name": "B"},
                {"model": "seat-c", "role": "typo-c", "name": "C"},
            ]
        }

    err = io.StringIO()
    with _PatchBackends(
        available=lambda model: model == "seat-a",
        reason=lambda model: (
            None if model == "seat-a" else f"{model}: session-limit cooldown"
        ),
        extra_attrs={
            (cli, "load_config"): _config,
            (cli, "_read_stdin_if_piped"): lambda: None,
        },
        env={"REVIEW_FAKE_BACKEND": "1"},
    ):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(
                ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "t"], cwd=str(repo), check=True
            )
            (repo / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli._dispatch(
                    [
                        "diff",
                        "--task",
                        "TEST-1",
                        "--staged",
                        "--pool",
                        "3",
                        "-C",
                        str(repo),
                    ]
                )
    assert rc == 2, (rc, err.getvalue())
    assert "typo-a" in err.getvalue(), err.getvalue()


def test_chain_aware_available_agrees_with_the_guard_on_a_head_down_alternate_live_seat():
    """`cli._chain_aware_available` is the ONE liveness predicate the guard, the ETA's
    `planned_pool` split, and the real board dispatch split (`_mode_review_board`) all
    share — codex P1 on review of #157: before this fix only the guard was chain-aware,
    so it could approve a pool size the (raw) split then silently shrank. This proves the
    shared function itself agrees with `any_provider_available` (the guard's own
    liveness check) for the exact head-down/alternate-live shape the P1 was about;
    `test_board_startup_split_includes_a_seat_whose_head_is_down_but_has_a_live_alternate`
    (tests/test_provider_failover.py) proves the REAL dispatch split honors it end to end."""

    def _available(model: str) -> bool:
        return model == "oc:zai/glm-5.3"

    with _PatchBackends(available=_available, reason=lambda _m: None):
        assert cli._chain_aware_available("zai:glm-5.3") is True, (
            "a live failover alternate must count as available"
        )
        assert cli._chain_aware_available("codex") is False, (
            "a single-provider model with no live provider must be unavailable"
        )


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
