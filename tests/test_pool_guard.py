#!/usr/bin/env python3
"""Unit tests for the pool/model-selection FOOLPROOFING (защита от дурака).

`reviewlib.pool_guard.evaluate_selection` is the pre-dispatch guard that turns a
non-convergent selection into an ACTIONABLE outcome instead of a silent, degenerate
review. Three branches are proven here, all OFFLINE (availability + reason are injected
closures — no backend, no network):

  (1) explicit `-m` narrows the set, some picks are DOWN, and the live subset can't fill
      the requested size  -> a PROPOSAL listing only the fallbacks that CAN converge, each
      with its FULL model list annotated live/down + per-seat reason;
  (2) `--pool N` larger than the live count                    -> propose not overriding
      the pool, showing the default option + live/down annotations;
  (3) even the default board can't muster the minimum panel    -> a TARGETED per-provider
      error enumerating WHY each dead seat is down, plus the current live-model list.

Also proven: the happy path returns PROCEED untouched; codex and Sol collapse to ONE
distinct engine (they share an underlying model); and EVERY printed model list carries
the per-seat live/down+reason annotation (the CTO refinement).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run by __main__.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import ASTRA_SEAT, GROK_SEAT, SOL_SEAT  # noqa: E402
from reviewlib.pool_guard import (  # noqa: E402
    EXIT_UNSATISFIED,
    MIN_CONVERGE_MODELS,
    PROCEED,
    PROPOSE,
    UNSATISFIED,
    Candidate,
    default_distinct_key,
    evaluate_selection,
)


@contextlib.contextmanager
def _codex_home(config: str | None):
    """A throwaway CODEX_HOME whose config.toml holds `config` (None = no file)."""
    old = os.environ.get("CODEX_HOME")
    with tempfile.TemporaryDirectory() as d:
        if config is not None:
            (Path(d) / "config.toml").write_text(config, encoding="utf-8")
        os.environ["CODEX_HOME"] = d
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old


# The codex/Sol collapse below assumes the codex CLI's own default is Sol; pin it per test
# so the result does not depend on the machine's ~/.codex.
_SOL_CONFIG = f'model = "{SOL_SEAT.split(":", 1)[1]}"\n'

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _sol_codex_default():
        with _codex_home(_SOL_CONFIG):
            yield

except ImportError:  # the standalone runner below pins it instead
    pass


# === injected availability harness ==============================================
def _avail(live: set[str]):
    return lambda model: model in live


def _reason(reasons: dict[str, str]):
    """Return a `reason(model)` closure: None when live, else the mapped down-reason."""
    return lambda model: reasons.get(model)


def _cand(label: str, why: str, board, pool_size: int) -> Candidate:
    return Candidate(label=label, why=why, board=tuple(board), pool_size=pool_size)


LIVE_ALL = {
    "claude:claude-opus-5-5",
    "codex",
    "gemini:gemini-2.5-flash",
    "oc:zai/glm-5.3",
    "claude:claude-fable-5-1",
    SOL_SEAT,
}
DOWN_REASONS = {
    "commandcode:moonshotai/Kimi-K3": "commandcode: insufficient credits (provider marked unpaid)",
    "commandcode:zai-org/GLM-5.3": "commandcode: insufficient credits (provider marked unpaid)",
    "gemini:gemini-2.5-flash": "gemini: GEMINI_API_KEY not found",
}
DEFAULT_BOARD_SPEC = [
    ("claude:claude-opus-5-5", "Opus"),
    ("codex", "Codex"),
    ("gemini:gemini-2.5-flash", "Gemini"),
    ("oc:zai/glm-5.3", "GLM"),
    ("commandcode:moonshotai/Kimi-K3", "Kimi-cc"),
    ("commandcode:zai-org/GLM-5.3", "GLM-cc"),
]


# === happy path ==================================================================
def test_selection_that_converges_proceeds():
    """All requested seats live and >= requested size -> PROCEED, no message."""
    board = [("claude:claude-opus-5-5", "Opus"), ("codex", "Codex")]
    d = evaluate_selection(
        user_board=board,
        requested_size=2,
        explicit=True,
        candidates=[],
        available=_avail(LIVE_ALL),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.kind
    assert d.exit_code == 0
    assert d.text == ""


def test_candidates_thunk_not_realized_on_proceed():
    """`candidates` may be a THUNK realized ONLY on the non-converge path — so a converging
    happy-path run never rebuilds the fallback boards (which re-resolve presets and can
    re-emit load_board stderr warnings). The thunk must NOT be called when PROCEED."""
    board = [("claude:claude-opus-5-5", "Opus"), ("codex", "Codex")]

    def _boom():
        raise AssertionError(
            "candidates thunk must not be realized on the PROCEED path"
        )

    d = evaluate_selection(
        user_board=board,
        requested_size=2,
        explicit=True,
        candidates=_boom,
        available=_avail(LIVE_ALL),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.text


def test_default_path_with_enough_live_proceeds():
    """No -m / no --pool: proceeds as long as the board clears the convergence floor,
    even if fewer live than the nominal pool (the pool auto-shrinks)."""
    live = {"claude:claude-opus-5-5", "codex"}  # only 2 of 6 live
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


# === branch 1: explicit -m can't fill the requested size =========================
def test_explicit_narrow_cannot_converge_proposes_only_fitting_options():
    """User -m picks 4 models, 2 are down -> live subset (2) < requested (4). Propose the
    fallbacks that CAN converge; a too-small preset that can't is filtered out."""
    user = [
        ("claude:claude-opus-5-5", "Opus"),
        ("gemini:gemini-2.5-flash", "Gemini"),  # down
        ("commandcode:moonshotai/Kimi-K3", "Kimi-cc"),  # down
        ("codex", "Codex"),
    ]
    default_cand = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    dead_preset = _cand(
        "preset:dead",
        "--preset dead",
        [
            ("gemini:gemini-2.5-flash", "Gemini"),
            ("commandcode:zai-org/GLM-5.3", "GLM-cc"),
        ],
        2,
    )  # both down -> can't converge -> must be hidden
    d = evaluate_selection(
        user_board=user,
        requested_size=4,
        explicit=True,
        candidates=[default_cand, dead_preset],
        available=_avail(LIVE_ALL - {"gemini:gemini-2.5-flash"}),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # the viable default option is offered, the dead preset is filtered out
    assert "default board" in d.text
    assert "preset:dead" not in d.text and "--preset dead" not in d.text
    # per-seat annotation: a down seat shows its reason inline
    assert "insufficient credits" in d.text
    # the always-on current-live-models line is present
    assert "live model" in d.text.lower()


def test_proposal_annotates_every_seat_live_or_down_with_reason():
    """Each model list in a proposal shows per-seat live/down + reason (CTO refinement)."""
    user = [("commandcode:moonshotai/Kimi-K3", "Kimi-cc")]  # single, down
    default_cand = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    d = evaluate_selection(
        user_board=user,
        requested_size=1,
        explicit=True,
        candidates=[default_cand],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    # the default option lists a live seat (Opus) AND a down seat (Kimi-cc) with reason
    assert "Opus" in d.text
    assert "Kimi-cc" in d.text
    assert "insufficient credits" in d.text
    # live seats are marked distinctly from down seats
    assert "claude:claude-opus-5-5" in d.text


# === branch 2: --pool N larger than live count ===================================
def test_pool_larger_than_live_proposes_not_overriding():
    """--pool 6 but only 2 live -> propose NOT overriding the pool; show the default
    board option (which converges) with live/down annotations."""
    live = {"claude:claude-opus-5-5", "codex"}
    default_cand = _cand(
        "default",
        "don't pass --pool; the default pool size runs the live board",
        DEFAULT_BOARD_SPEC,
        4,
    )
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=6,
        explicit=True,
        candidates=[default_cand],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    assert "default" in d.text
    # shows what would actually run + reasons for the down seats
    assert "insufficient credits" in d.text


# === branch 3: default board itself can't converge ===============================
def test_default_cannot_converge_targeted_per_provider_error():
    """Fewer distinct live models than the floor, and NO candidate converges -> a targeted
    per-provider error enumerating each dead seat's reason + the live-model list."""
    live = {"claude:claude-opus-5-5"}  # 1 live < MIN_CONVERGE_MODELS
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[default_cand],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # targeted: enumerate the specific dead providers/reasons
    assert "commandcode" in d.text
    assert "insufficient credits" in d.text
    assert "GEMINI_API_KEY" in d.text
    # still shows the current live list (Opus)
    assert "claude:claude-opus-5-5" in d.text


def test_error_headline_uses_per_board_best_not_union_count():
    """The UNSATISFIED headline count is the BEST any SINGLE board achieves, never the union
    across boards. Two candidate boards that each contribute ONE distinct live model (a
    different one) can't converge (floor 2), but a union count would print the
    self-contradictory 'only 2 … need >= 2'. The headline must say 'at most 1 … need >= 2'."""
    # user board: only Opus live (1 distinct). Two presets, each with exactly one DIFFERENT
    # live model, so the union of live models is 2 but no single board reaches the floor.
    user = [("claude:claude-opus-5-5", "Opus"), ("gemini:gemini-2.5-flash", "Gemini")]
    preset_a = _cand(
        "preset:a",
        "--preset a",
        [
            ("codex", "Codex"),
            ("gemini:gemini-2.5-flash", "Gemini"),  # only Codex live
        ],
        2,
    )
    preset_b = _cand(
        "preset:b",
        "--preset b",
        [
            ("oc:zai/glm-5.3", "GLM"),
            ("commandcode:zai-org/GLM-5.3", "GLM-cc"),  # only GLM live
        ],
        2,
    )
    live = {
        "claude:claude-opus-5-5",
        "codex",
        "oc:zai/glm-5.3",
    }  # 1 per board, 3 in union
    d = evaluate_selection(
        user_board=user,
        requested_size=2,
        explicit=False,
        candidates=[preset_a, preset_b],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.text
    assert "at most 1 distinct live" in d.text, d.text
    # never the self-contradictory union count that equals/exceeds the floor
    assert (
        "at most 2 distinct live" not in d.text
        and "at most 3 distinct live" not in d.text
    ), d.text


def test_error_when_nothing_is_live_lists_no_live_models():
    """Zero live seats -> error path that says there are no live models (never a blank)."""
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[default_cand],
        available=_avail(set()),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # the live-model line must be present even when empty (no silent blank)
    assert "live model" in d.text.lower()


# === codex / sol collapse to one distinct engine =================================
def test_codex_and_sol_collapse_to_one_engine_for_viability():
    """codex and Sol share an underlying model. A fallback made of ONLY codex+Sol — even
    with both live — is ONE distinct engine, so it must NOT be offered as a viable
    (>= floor) proposal; the guard must not present it as a real 2-model panel."""
    assert default_distinct_key("codex") == default_distinct_key(SOL_SEAT)
    user = [
        ("commandcode:moonshotai/Kimi-K3", "Kimi-cc")
    ]  # single dead -> can't converge
    codex_sol_only = _cand(
        "preset:codexish",
        "--preset codexish",
        [
            ("codex", "Codex"),
            (SOL_SEAT, "Sol"),
        ],
        2,
    )  # both LIVE but ONE engine -> distinct_live 1 < floor -> filtered out
    real_default = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    d = evaluate_selection(
        user_board=user,
        requested_size=1,
        explicit=True,
        candidates=[codex_sol_only, real_default],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    assert "codexish" not in d.text  # the one-engine option is filtered out
    assert "default board" in d.text


def test_astra_does_not_collapse_with_codex_or_sol():
    """Opus review finding: the whole point of pinning ASTRA_SEAT onto the board (in
    place of the bare `"codex"` seat that used to collapse with Sol) is that Astra is a
    GENUINELY DISTINCT engine. Assert it directly against the distinct-engine counter
    this module actually uses for viability, rather than trusting it transitively —
    a future change to `_CODEX_SOL_EQUIVALENTS` (e.g. a prefix test instead of exact
    membership) could silently re-collapse Astra into the same engine as Sol/bare codex
    with nothing here to catch it."""
    astra_key = default_distinct_key(ASTRA_SEAT)
    assert astra_key != default_distinct_key(SOL_SEAT)
    assert astra_key != default_distinct_key("codex")
    # `-m codex,sol,astra` must therefore count as 2 distinct engines, not 1 or 3.
    assert (
        len(
            {
                default_distinct_key("codex"),
                default_distinct_key(SOL_SEAT),
                default_distinct_key(ASTRA_SEAT),
            }
        )
        == 2
    )


def test_deliberate_fully_live_narrow_selection_proceeds():
    """A fully-live `-m codex,sol` proceeds (both reachable) — the guard never blocks a
    selection whose every seat is live, even when they dedupe to one engine."""
    user = [("codex", "Codex"), (SOL_SEAT, "Sol")]
    d = evaluate_selection(
        user_board=user,
        requested_size=2,
        explicit=True,
        candidates=[],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


def test_convergence_floor_is_at_least_two():
    """A multi-model panel needs >= 2 distinct live models by default."""
    assert MIN_CONVERGE_MODELS >= 2


def test_single_seat_board_floor_caps_to_board_size():
    """A deliberately configured 1-seat board must RUN its one live model, not demand two
    (the floor caps at the board's distinct total)."""
    d = evaluate_selection(
        user_board=[("codex", "Codex")],
        requested_size=0,
        explicit=False,
        candidates=[],
        available=_avail({"codex"}),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.text


def test_provider_variants_of_one_model_are_one_distinct_engine():
    """The guard must agree with provider-failover on model identity: `zai:glm-5.3`,
    `oc:zai/glm-5.3` and `commandcode:zai-org/GLM-5.3` are ONE engine (cross-PROVIDER
    collapse), and `opus` collapses with its oc: spelling too. This keeps the guard's
    'distinct live models' count consistent with what provider-failover treats as one model.

    Consequence, mirroring codex/sol: an all-live `-m zai:glm-5.3,oc:zai/glm-5.3` is a
    1-engine request, not a 2-model panel. cli._evaluate_pool_or_bail sizes an -m request by
    the DISTINCT-engine count (`len({default_distinct_key(m)...})`), so it passes
    requested_size=1 here — and an all-live selection PROCEEDs (the guard never blocks a
    selection whose every seat is live; see test_deliberate_fully_live_narrow_selection)."""
    k = default_distinct_key
    assert k("zai:glm-5.3") == k("oc:zai/glm-5.3") == k("commandcode:zai-org/GLM-5.3")
    assert k("claude:claude-opus-5-5") == k("oc:anthropic/claude-opus-5-5")
    user = [("zai:glm-5.3", "GLM-a"), ("oc:zai/glm-5.3", "GLM-b")]
    requested = len({k(m) for m, _n in user})  # what cli passes: distinct engines == 1
    assert requested == 1
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=user,
        requested_size=requested,
        explicit=True,
        candidates=[default_cand],
        available=_avail(LIVE_ALL | {"zai:glm-5.3", "oc:zai/glm-5.3"}),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


def test_distinct_key_keeps_other_models_separate():
    """The codex/sol collapse must NOT bleed into unrelated models."""
    keys = {
        default_distinct_key("claude:claude-opus-5-5"),
        default_distinct_key("claude:claude-fable-5-1"),
        default_distinct_key("gemini:gemini-2.5-flash"),
        default_distinct_key("codex"),
    }
    assert len(keys) == 4  # opus, fable, gemini, codex-engine all distinct


# === reuse_eligible (task-cli#139): a role-lensed board's dispatch will pad a single ===
# === live seat across the missing roles, so the convergence floor must not block it ===


def test_reuse_eligible_single_live_seat_proceeds_below_the_normal_floor():
    """The exact production incident: a default `review diff` (non-explicit, so the
    normal floor is MIN_CONVERGE_MODELS=2), only 1 of 6 board seats live (the other 5 in
    session-limit cooldown). Without `reuse_eligible`, this bails UNSATISFIED/PROPOSE
    before dispatch ever gets a chance to pad — the pool_guard's own floor was the thing
    stopping the already-existing reuse mechanism from firing."""
    live = {"codex"}  # only 1 of 6 live
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
        reuse_eligible=True,
    )
    assert d.kind == PROCEED, d.text


def test_reuse_eligible_explicit_pool_below_the_normal_floor_proceeds():
    """Same scarcity, but an EXPLICIT `--pool 3` (no -m) — the normal floor would demand
    3 distinct live models; reuse_eligible=True still proceeds on the single live seat,
    since the real dispatch will pad it across 3 board roles."""
    live = {"codex"}
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=3,
        explicit=True,
        candidates=[],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
        reuse_eligible=True,
    )
    assert d.kind == PROCEED, d.text


def test_reuse_eligible_but_nothing_live_still_unsatisfied():
    """reuse_eligible doesn't manufacture a seat out of nothing: zero live models still
    fails, same as today (there's nothing for the reuse selector to pad WITH)."""
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[],
        available=_avail(set()),
        reason=_reason(DOWN_REASONS),
        reuse_eligible=True,
    )
    assert d.kind == UNSATISFIED, d.text


def test_reuse_eligible_false_keeps_the_prior_strict_floor():
    """The default (`reuse_eligible=False`, matching every existing caller/test above)
    is byte-identical to pre-task-cli#139 behaviour: 1 live seat against the 2-model
    floor still fails to converge."""
    live = {"codex"}
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind != PROCEED, d.kind


def test_reuse_eligible_lets_one_live_model_fill_a_three_role_request():
    """A recorded `--task` light/3-role request with one live engine: without
    reuse_eligible the guard demands 3 distinct engines and bails; with it, one
    live model proceeds so dispatch can pad (review-cli#309)."""
    board = [
        (GROK_SEAT, "Grok"),
        ("claude:claude-opus-5-5", "Opus"),
        ("codex:gpt-6-astra", "Astra"),
    ]
    live = {GROK_SEAT}
    blocked = evaluate_selection(
        user_board=board,
        requested_size=3,
        explicit=True,
        candidates=[],
        available=_avail(live),
        reason=_reason({}),
        reuse_eligible=False,
    )
    assert blocked.kind != PROCEED, blocked.kind
    allowed = evaluate_selection(
        user_board=board,
        requested_size=3,
        explicit=True,
        candidates=[],
        available=_avail(live),
        reason=_reason({}),
        reuse_eligible=True,
    )
    assert allowed.kind == PROCEED, allowed.text


def test_bare_codex_follows_the_cli_default_not_the_pin():
    """review-cli#645: the codex CLI's own default decides what a bare `codex` is. A
    machine still defaulting to the pre-bump gpt-5.6-sol collapses `codex` with
    `gpt56sol`, not with the bumped `sol`; with no `model` it stays its own key. A
    `model = ...` line inside a multi-line string is text, not the setting."""
    from reviewlib.modes import qa

    if qa._toml_loader() is None:  # 3.9/3.10 without tomli: see the fallback test
        return
    with _codex_home('model = "gpt-5.6-sol"\n[profiles.x]\nmodel = "other"\n'):
        assert default_distinct_key("codex") == default_distinct_key("gpt56sol")
        assert default_distinct_key("codex") != default_distinct_key("sol")
    with _codex_home(
        'developer_instructions = """\nmodel = \'gpt-6-astra\'\n"""\n'
        'model = "gpt-6.1-sol"\n'
    ):
        assert default_distinct_key("codex") == default_distinct_key(SOL_SEAT)
        assert default_distinct_key("codex") != default_distinct_key(ASTRA_SEAT)
    with _codex_home('[profiles.x]\nmodel = "gpt-6.1-sol"\n'):
        assert default_distinct_key("codex") == "codex"
    with _codex_home(None):
        assert default_distinct_key("codex") == "codex"


def test_bare_codex_without_a_toml_parser_keeps_the_sol_assumption():
    """On a host with no tomllib/tomli the guard cannot read the codex config, so a bare
    `codex` keeps counting as SOL_SEAT's model, as before review-cli#645."""
    from reviewlib.modes import qa

    real = qa._toml_loader
    qa._toml_loader = lambda: None
    try:
        with _codex_home('model = "gpt-5.6-sol"\n'):
            assert default_distinct_key("codex") == default_distinct_key(SOL_SEAT)
    finally:
        qa._toml_loader = real


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with _codex_home(_SOL_CONFIG):
                    fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
