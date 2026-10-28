#!/usr/bin/env python3
"""Unit tests for the reviewer board (HYP-741).

A board assigns each reviewer model its OWN role/lens. These tests prove, all
offline (no model call, no network — backends are monkeypatched / forced
unavailable):
  (a) DEFAULT_BOARD matches the directive table byte-exact (model -> role);
  (b) config.yaml `board:` parsing — valid entries kept, an unrecognized `role:`
      raises BoardConfigError immediately (review-cli#422), a malformed model/
      capability entry is skipped, empty/absent -> DEFAULT_BOARD;
  (c) role-lens injection — an available reviewer's PanelJob prompt is
      `base_prompt + lens` and its label is `"<display> [<role>]"`;
  (d) graceful skip — an unavailable reviewer is dropped (not crashed) and surfaced
      in the `skipped` list; an all-unavailable board returns no jobs;
  (e) mode_review board path runs in parallel and returns 0 on success / 1 on a
      failed reviewer, and explicit -m either runs a flat panel (no config) or narrows
      configured board metadata.

Runs as a plain script (mirrors tests/test_provider_keys.py): each `test_*` is
invoked by the __main__ block, no pytest required.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
import reviewlib.modes.review as _review_mod  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    ASTRA_SEAT,
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    GLM_COMMANDCODE_SEAT,
    GROK_SEAT,
    MIMO_SEAT,
    KIMI_SEAT,
    SOL_SEAT,
    SONNET_SEAT,
    LUNA_SEAT,
    REVIEW_ROLES,
    VISUAL_MODELS,
    BoardConfigError,
    BoardReviewer,
    _agentic,
    _split_models,
    board_from_models,
    lens_override_text,
    load_board,
    default_level_name,
    preset_board,
    select_pool,
)
from reviewlib.panel import build_board_jobs  # noqa: E402

DEFAULT_PROMPT = "Review this diff."

# The pre-levels preset constants were data of the shipped levels now; these names keep the
# many board tests below readable (routine = the former `light`, standard = `default`,
# deep = `heavy`).
DEFAULT_PRESET = default_level_name()
LIGHT_PRESET_BOARD = tuple(preset_board("routine"))
DEFAULT_PRESET_BOARD = tuple(preset_board("standard"))
HEAVY_PRESET_BOARD = tuple(preset_board("deep"))


class _BackendStateSandbox:
    def __enter__(self):
        self._saved_unpaid_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
        self._saved_config_unpaid = backends._CONFIG_UNPAID_PROVIDERS
        os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        backends.configure_unpaid_providers(None)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return self

    def __exit__(self, *_exc):
        if self._saved_unpaid_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = self._saved_unpaid_env
        backends.configure_unpaid_providers(self._saved_config_unpaid)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return False


# === DEFAULT_BOARD shape (byte-exact model ids, PRIORITY order from the directive) ==


# === No dead Fireworks/glide provider in the defaults (review-cli#25) ============
# The flat DEFAULT_MODELS panel used to pin `oc:fireworks/.../kimi-k2p6-turbo`, which ran
# on the suspended Fireworks `glide` account — a dead route. These pin that it is gone and
# that the flat panel + the board share ONE canonical Kimi seat (KIMI_SEAT) so they cannot
# drift back to the dead provider.

# The DEAD-ROUTE tokens this fix removed. Matched as exact substrings (not a bare
# "fireworks", which would false-trip on a future legitimately re-enabled Fireworks
# account or an unrelated `commandcode:fireworks/...` route — the ban is on THIS dead
# route, not the provider name forever).
_DEAD_ROUTE_TOKENS = ("oc:fireworks/", "kimi-k2p6-turbo", "glide")


def _all_default_model_strings() -> list[str]:
    """Every model string reachable from the CODE-defined defaults: the flat DEFAULT_MODELS
    panel and every DEFAULT_BOARD seat (the board IS pool + reserve; BoardReviewer carries
    no other model field). The dead-route ban is global across these defaults — a new
    seat/pool must not reintroduce the suspended route anywhere (review-cli#25). It does
    NOT police user-supplied config models, which are out of scope for this fix."""
    return list(DEFAULT_MODELS) + [r.model for r in DEFAULT_BOARD]


# === --pool seat selection (board redesign): default 4, first-N, reserve = rest ==


# === per-project lens overrides (review-cli#547, .review/lenses/<role>.md) =======


# === config.yaml board parsing ==================================================


# === cost-safety: PRESENT-but-all-malformed `board:` ERRORS (does not silently
# fall back to any built-in board). Absent -> default preset; partial -> keep valid. ==


# === role-lens injection into PanelJobs =========================================
class _AvailabilityPatch:
    """Force backend_available to a fixed predicate so the board path is offline."""

    def __init__(self, available_models: set[str]):
        self._available = available_models

    def __enter__(self):
        self._old = backends.backend_available

        def _fake(model: str) -> bool:
            return model in self._available

        backends.backend_available = _fake
        # build_board_jobs imported the name into reviewlib.panel; patch there too.
        import reviewlib.panel as panel

        self._old_panel = panel.backend_available
        panel.backend_available = _fake
        # _mode_review_board also does `from ..backends import backend_available` (a
        # direct binding in reviewlib.modes.review), so the board-pool split there must
        # be patched too — otherwise the "no reviewers available" test silently hits the
        # REAL probe (and now that `codex` is on PATH, a real seat could leak through).
        import reviewlib.modes.review as review_mod

        self._old_review_mod = review_mod.backend_available
        review_mod.backend_available = _fake

        # `_report_pool_shortfall` (review-cli#276) also does
        # `from ..backends import backend_unavailable_reason` -- a seat this patch marks
        # unavailable now gets a SECOND real probe from that function whenever the pool
        # comes up short, undoing the exact hermeticity this class exists to guarantee
        # (k3 review finding, round 4: real `_which()`/PATH/auth-db reads leak through a
        # test that has no real CLI/key configured). Keep it deterministic and consistent
        # with `_fake` above: available -> no reason, unavailable -> a fixed fake reason.
        def _fake_reason(model: str) -> str | None:
            return None if model in self._available else "patched unavailable (test)"

        self._old_review_mod_reason = review_mod.backend_unavailable_reason
        review_mod.backend_unavailable_reason = _fake_reason
        return self

    def __exit__(self, *exc):
        backends.backend_available = self._old
        import reviewlib.panel as panel

        panel.backend_available = self._old_panel
        import reviewlib.modes.review as review_mod

        review_mod.backend_available = self._old_review_mod
        review_mod.backend_unavailable_reason = self._old_review_mod_reason
        return False


# === graceful skip of unavailable reviewers =====================================


# === mode_review board path (parallel run, exit codes) ==========================


# === CLI wiring: explicit -m precedence (no --no-board flag exists) ==============


def _capture_default_review_board(argv: list[str]) -> dict:
    """Run `cli.main(argv)` on the default-review path with the board pinned to
    DEFAULT_BOARD and an empty config, capturing the (full) board and pool_size passed
    into mode_review. All seats forced AVAILABLE so the test is offline + deterministic.
    Shared by the default-pool and --pool wiring tests."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        outcome_sink=None,
        **kw,
    ):
        captured["board"] = board
        captured["pool_size"] = pool_size
        captured["timeout"] = timeout
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    # Pin the board to DEFAULT_BOARD AND stub load_config to an empty dict so the
    # test is independent of the dev machine's ~/.config/review-cli/config.yaml.
    # The true default path has neither -m nor config models.
    old_load_board = cli.load_board

    def _load_board(_cfg, **kw):
        preset = kw.get("preset")
        if preset == "standard":
            return list(DEFAULT_PRESET_BOARD)
        if preset == "routine":
            return list(LIGHT_PRESET_BOARD)
        if preset == "deep":
            return list(HEAVY_PRESET_BOARD)
        return list(DEFAULT_BOARD)

    cli.load_board = _load_board
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    # Force every seat available so the CLI's ETA planned_pool slice is deterministic
    # and never probes a real key/CLI (offline).
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(argv)
    finally:
        _review_mod.mode_review = old
        cli.load_board = old_load_board
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    return captured


def _capture_review_with_config(argv: list[str], config: dict) -> dict:
    """Run the default-review path with a CUSTOM config (all seats live), capturing the
    pool_size passed into mode_review. For config `pool:` default-size wiring tests."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        outcome_sink=None,
        **kw,
    ):
        captured["board"] = board
        captured["pool_size"] = pool_size
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: config
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(argv)
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    return captured


def _show_board_lines(
    pool_size: int,
    board_models: list[str] | None = None,
    available: set[str] | None = None,
    *,
    preset: str | None = None,
    explicit_models: list[str] | None = None,
    cwd: Path | None = None,
) -> list[str]:
    """Run cli._show_board(config, pool_size) and return its stdout lines. When
    board_models is given, a config `board:` of those models is used (each role tests).
    `available` forces the availability probe to a fixed set (None = all available) so
    the tag split is deterministic + offline. `cwd`, when given, is the reviewed repo
    root to resolve `.review/lenses/<role>.md` project overrides against (review-cli#547);
    omitted, `_show_board` resolves its own `Path.cwd()`."""
    import contextlib
    import io

    from reviewlib import cli

    cfg: dict = {}
    if board_models is not None:
        cfg = {"board": [{"model": m, "role": "tests"} for m in board_models]}
    old_avail = backends.backend_available
    backends.backend_available = lambda m: (
        True if available is None else (m in available)
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(
                cfg,
                pool_size,
                cwd,
                preset=preset,
                explicit_models=explicit_models,
            )
    finally:
        backends.backend_available = old_avail
    assert rc == 0, rc
    return buf.getvalue().splitlines()


def _dispatch_just_ask_capturing_models(*, extra_argv=None):
    """Run `review just-ask` against built-in DEFAULT_MODELS, capturing the
    panel the handler actually received. Offline: handler + availability
    stubs, no backend spawn."""
    import contextlib
    import io

    from reviewlib import cli
    from reviewlib.modes import just_ask as just_ask_mod

    captured: dict = {}

    def fake_just_ask(question, models, *a, **k):
        captured["models"] = list(models)
        return 0

    saved_ask = just_ask_mod.mode_just_ask
    saved_cfg = cli.load_config
    saved_stdin = cli._read_stdin_if_piped
    saved_chain = cli._chain_aware_available
    old_task = os.environ.get("REVIEW_TASK_CODE")
    old_env = os.environ.get("GEMINI_ENV_FILE")
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    try:
        just_ask_mod.mode_just_ask = fake_just_ask
        cli.load_config = lambda: {}
        cli._read_stdin_if_piped = lambda: None
        cli._chain_aware_available = lambda model: backends.backend_available(model)
        os.environ["REVIEW_TASK_CODE"] = "TEST-418"
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        os.environ.pop("REVIEW_FAKE_BACKEND", None)
        argv = ["just-ask", "q", "--task", "TEST-418", "-C", str(REPO_ROOT)]
        if extra_argv:
            argv.extend(extra_argv)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            captured["rc"] = cli.main(argv)
    finally:
        just_ask_mod.mode_just_ask = saved_ask
        cli.load_config = saved_cfg
        cli._read_stdin_if_piped = saved_stdin
        cli._chain_aware_available = saved_chain
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake
    return captured


# --- review-cli#309: recorded --task reviews must satisfy the ship min_roles floor ---


def _capture_recorded_review_dispatch(argv: list[str]) -> dict:
    """Run `cli.main(argv)` with empty config, capturing board-path kwargs.

    Offline: every seat is available and mode_review is stubbed so no model is
    called. Independent of the machine's ~/.config/review-cli/config.yaml.
    """
    import io

    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        **kw,
    ):
        captured["models"] = models
        captured["board"] = board
        captured["pool_size"] = pool_size
        captured["exact_board"] = kw.get("exact_board")
        captured["usage_percent"] = kw.get("usage_percent")
        return 0

    old_mode = _review_mod.mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    old_stdin = sys.stdin
    _review_mod.mode_review = _fake_mode_review
    cli.load_config = lambda: {}
    backends.backend_available = lambda _m: True
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        sys.stdin = io.StringIO("+added line\n")
        captured["rc"] = cli.main(argv)
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    return captured


def _capture_panel_task_dispatch(
    argv: list[str],
    *,
    config: dict | None = None,
    available: bool | Callable[[str], bool] = True,
) -> dict:
    """Run `cli.main(argv)` for a panel mode, capturing the ModeContext the
    handler actually received. Offline: empty config by default (or `config`
    when given), every backend live by default (or dead everywhere when
    `available=False`, for exercising an all-unreachable fallback; a
    `model -> bool` callable marks only some engines live), handler
    stubbed so no model is called.

    Also monkeypatches `cli._chain_aware_available` (not just
    `backends.backend_available`): the real chain-aware probe walks each
    seat's `provider_failover` chain and consults
    `backends.runtime_provider_marked_unpaid`, which is NOT patched here —
    leaving it live means a seat's liveness can depend on this machine's
    actual paid-provider bookkeeping, defeating the offline determinism the
    other two patches exist for. Every quorum-default-board caller (the
    `select_pool_and_reserve_with_reuse(DEFAULT_BOARD, ...)` path) probes
    seats via `cli._chain_aware_available`, so it must be forced live too.

    Also neutralizes real usage-limit data (`REVIEW_USAGE_LIMITS_DIR` ->
    a nonexistent path, `REVIEW_HARNESS_LIMITS_JSON` -> `"{}"`): `_dispatch`
    calls the real `usage_limits.load_snapshot()`/`load_harness_cards()`,
    which on a host with real tg-ctl usage data could mark DEFAULT_BOARD
    seats near their limit and make `select_pool_and_reserve_with_reuse`
    collapse to fewer distinct seats than expected -- flaky depending on
    this machine's actual usage bookkeeping (Fable/GLM review finding).

    Fails closed if the CLI never reaches the handler (`rc` set, models/roles
    absent) so a usage-error path cannot silently look like a 0-role pad.
    """
    import contextlib
    import io
    from dataclasses import replace

    from reviewlib import cli
    from reviewlib.modes import registry as reg

    captured: dict = {}
    sub = argv[0]

    def _fake_handler(ctx):
        captured["models"] = list(ctx.models)
        captured["roles"] = list(ctx.extra.get("task_roles") or [])
        return 0

    orig_modes = reg.MODES
    orig_load = cli.load_config
    orig_avail = backends.backend_available
    orig_chain_avail = cli._chain_aware_available
    orig_stdin = sys.stdin
    saved_diff = cli._git_diff
    cli._git_diff = lambda *args, **kwargs: ""
    old_task = os.environ.get("REVIEW_TASK_CODE")
    old_env = os.environ.get("GEMINI_ENV_FILE")
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    old_usage_dir = os.environ.get("REVIEW_USAGE_LIMITS_DIR")
    old_harness_json = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    reg.MODES = tuple(
        replace(m, handler=_fake_handler) if m.subcommand == sub else m
        for m in orig_modes
    )
    cli.load_config = lambda: dict(config) if config is not None else {}
    is_live = available if callable(available) else (lambda _m: available)
    backends.backend_available = is_live
    cli._chain_aware_available = is_live
    sys.stdin = io.StringIO("")
    os.environ.pop("REVIEW_TASK_CODE", None)
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ.pop("REVIEW_FAKE_BACKEND", None)
    os.environ["REVIEW_USAGE_LIMITS_DIR"] = "/nonexistent/review-cli/.usage"
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = "{}"
    try:
        stderr_buf = io.StringIO()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr_buf),
        ):
            captured["rc"] = cli.main(argv)
        captured["stderr"] = stderr_buf.getvalue()
    finally:
        reg.MODES = orig_modes
        cli.load_config = orig_load
        backends.backend_available = orig_avail
        cli._chain_aware_available = orig_chain_avail
        sys.stdin = orig_stdin
        cli._git_diff = saved_diff
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake
        if old_usage_dir is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_DIR", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_DIR"] = old_usage_dir
        if old_harness_json is None:
            os.environ.pop("REVIEW_HARNESS_LIMITS_JSON", None)
        else:
            os.environ["REVIEW_HARNESS_LIMITS_JSON"] = old_harness_json
    return captured




def _model_ids(board):
    return [reviewer.model for reviewer in board]


def _assert_backend_availability(seats, expected):
    for seat in seats:
        assert backends.backend_available(seat) is expected, seat

__all__ = ['ASTRA_SEAT', 'BoardConfigError', 'BoardReviewer', 'Callable', 'DEFAULT_BOARD', 'DEFAULT_MODELS', 'DEFAULT_POOL_SIZE', 'DEFAULT_PRESET', 'DEFAULT_PRESET_BOARD', 'DEFAULT_PROMPT', 'GLM_COMMANDCODE_SEAT', 'GROK_SEAT', 'HEAVY_PRESET_BOARD', 'KIMI_SEAT', 'LIGHT_PRESET_BOARD', 'LUNA_SEAT', 'MIMO_SEAT', 'Path', 'REPO_ROOT', 'REVIEW_ROLES', 'ReviewResult', 'SOL_SEAT', 'SONNET_SEAT', 'VISUAL_MODELS', '_AvailabilityPatch', '_BackendStateSandbox', '_DEAD_ROUTE_TOKENS', '_agentic', '_all_default_model_strings', '_assert_backend_availability', '_capture_default_review_board', '_capture_panel_task_dispatch', '_capture_recorded_review_dispatch', '_capture_review_with_config', '_dispatch_just_ask_capturing_models', '_model_ids', '_review_mod', '_show_board_lines', '_split_models', 'backends', 'board_from_models', 'build_board_jobs', 'default_level_name', 'lens_override_text', 'load_board', 'os', 'preset_board', 'select_pool', 'sys']
