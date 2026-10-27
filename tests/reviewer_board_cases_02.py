"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    BoardReviewer,
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    GROK_SEAT,
    KIMI_SEAT,
    MIMO_SEAT,
    Path,
    REPO_ROOT,
    REVIEW_ROLES,
    ReviewResult,
    VISUAL_MODELS,
    _BackendStateSandbox,
    _DEAD_ROUTE_TOKENS,
    _agentic,
    _all_default_model_strings,
    _assert_backend_availability,
    _split_models,
    backends,
    os,
    select_pool,
)

def test_codex_backend_maps_minimal_effort_to_supported_low_cli_config():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="minimal",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert 'model_reasoning_effort="low"' in captured["argv"], captured


def test_codex_backend_maps_max_effort_to_supported_xhigh_cli_config():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="max",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured


def test_claude_backend_preserves_max_effort_for_cli_flag():
    assert backends._claude_reasoning_effort("max") == "max"


def test_opencode_backend_includes_effort_hint_without_spacing_glitch():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    class _GitProc:
        returncode = 0
        stdout = ""
        stderr = ""

    old_which = backends._which
    old_run = backends._run
    old_streamed = backends._run_streamed
    old_ensure = backends._ensure_opencode_readonly_agent
    old_runs_in_repo = backends._opencode_runs_in_repo
    backends._which = lambda name: f"/bin/{name}"
    backends._run = lambda *a, **k: _GitProc()
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None
    backends._opencode_runs_in_repo = lambda _cwd: False

    def _fake_streamed(argv, **kw):
        captured["message"] = argv[-1]
        return _Proc()

    backends._run_streamed = _fake_streamed
    try:
        with _BackendStateSandbox():
            result = backends.review_opencode(
                "oc:commandcode/model",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="xhigh",
            )
    finally:
        backends._which = old_which
        backends._run = old_run
        backends._run_streamed = old_streamed
        backends._ensure_opencode_readonly_agent = old_ensure
        backends._opencode_runs_in_repo = old_runs_in_repo
    assert result.returncode == 0
    assert captured["message"].count("Use highest reasoning effort.") == 1, captured
    assert (
        "Use highest reasoning effort.\n\nYou are running outside"
        in captured["message"]
    ), captured
    assert "effort.Review" not in captured["message"], captured


def test_call_backend_with_opencode_effort_hint_is_not_duplicated():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    class _GitProc:
        returncode = 0
        stdout = ""
        stderr = ""

    old_which = backends._which
    old_run = backends._run
    old_streamed = backends._run_streamed
    old_ensure = backends._ensure_opencode_readonly_agent
    old_runs_in_repo = backends._opencode_runs_in_repo
    backends._which = lambda name: f"/bin/{name}"
    backends._run = lambda *a, **k: _GitProc()
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None
    backends._opencode_runs_in_repo = lambda _cwd: False

    def _fake_streamed(argv, **kw):
        captured["message"] = argv[-1]
        return _Proc()

    backends._run_streamed = _fake_streamed
    try:
        with _BackendStateSandbox():
            result = backends.call_backend(
                backends.review_opencode,
                "oc:commandcode/model",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="xhigh",
            )
    finally:
        backends._which = old_which
        backends._run = old_run
        backends._run_streamed = old_streamed
        backends._ensure_opencode_readonly_agent = old_ensure
        backends._opencode_runs_in_repo = old_runs_in_repo
    assert result.returncode == 0
    assert captured["message"].count("Use highest reasoning effort.") == 1, captured


def test_direct_rest_backend_effort_adds_prompt_hint():
    captured: dict = {}

    old_key = backends._commandcode_key
    old_preflight = backends.provider_preflight_result
    old_request = backends._openai_compatible_request
    backends._commandcode_key = lambda: "fake-key"
    backends.provider_preflight_result = lambda *a, **k: None

    def _fake_request(**kwargs):
        captured.update(kwargs)
        return ReviewResult(
            model=kwargs["model"],
            command="fake-rest",
            returncode=0,
            stdout=kwargs["prompt"],
            stderr="",
        )

    backends._openai_compatible_request = _fake_request
    try:
        with _BackendStateSandbox():
            result = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="high",
            )
    finally:
        backends._commandcode_key = old_key
        backends.provider_preflight_result = old_preflight
        backends._openai_compatible_request = old_request

    assert result.returncode == 0
    assert "Use high reasoning effort." in result.stdout
    assert "Use high reasoning effort." in captured["prompt"]


def test_direct_claude_cli_effort_adds_prompt_hint():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_binary = backends._claude_cli_binary
    old_argv = backends._claude_cli_argv
    old_env = backends._claude_cli_env
    old_trust = backends._ensure_workspace_trusted
    old_run = backends._run_streamed
    backends._claude_cli_binary = lambda: "/bin/claude"
    backends._claude_cli_argv = lambda *_a, **_k: ["/bin/claude"]
    backends._claude_cli_env = lambda: {}
    backends._ensure_workspace_trusted = lambda _cwd: None

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input_text"] = kwargs.get("input_text", "")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_claude_cli(
            "claude:claude-opus-5-5",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="high",
        )
    finally:
        backends._claude_cli_binary = old_binary
        backends._claude_cli_argv = old_argv
        backends._claude_cli_env = old_env
        backends._ensure_workspace_trusted = old_trust
        backends._run_streamed = old_run

    assert result.returncode == 0
    assert "Use high reasoning effort." in captured["input_text"]


def test_visual_models_have_separate_priority_from_review_board():
    """Visual review has its own priority list: Opus first, then GROK_SEAT, then
    vision-capable fallbacks, including a GLM vision model rather than the text-only
    GLM-5.3 seat."""
    assert VISUAL_MODELS[0] == "claude:claude-opus-5-5", VISUAL_MODELS
    assert VISUAL_MODELS[1] == GROK_SEAT, VISUAL_MODELS
    assert "commandcode:zai-org/GLM-5.3" not in VISUAL_MODELS, VISUAL_MODELS
    assert any(
        "glm-4.5v" in model.lower() or "glm-4.6v" in model.lower()
        for model in VISUAL_MODELS
    ), VISUAL_MODELS
    assert all(
        "vision" in model.lower()
        or "kimi" in model.lower()
        or "glm-4." in model.lower()
        or "opus" in model.lower()
        or "grok-4.7" in model.lower()
        or model == "gemini"
        for model in VISUAL_MODELS
    ), VISUAL_MODELS


def test_no_default_model_string_uses_the_dead_route():
    for model in _all_default_model_strings():
        low = model.lower()
        for tok in _DEAD_ROUTE_TOKENS:
            assert tok not in low, f"{model!r} contains dead-route token {tok!r}"


def test_kimi_seat_itself_is_clean():
    low = KIMI_SEAT.lower()
    for tok in _DEAD_ROUTE_TOKENS:
        assert tok not in low, (
            f"KIMI_SEAT {KIMI_SEAT!r} contains dead-route token {tok!r}"
        )


def test_every_default_kimi_entry_is_the_canonical_seat():
    """Any default model string that names Kimi must be EITHER the canonical diff-only seat
    KIMI_SEAT (flat panel) or its agentic opencode form `_agentic(KIMI_SEAT)` (board) — one
    source of truth for the Kimi MODEL ID across both transports (review-cli#24/#25). A
    third hard-coded (and drift-prone) Kimi string can never slip into the flat panel, the
    board, or a reserve seat. Both forms share the same wire model id (the bit after the
    provider), so a future id bump touches ONE constant and both transports follow."""
    kimi_entries = [m for m in _all_default_model_strings() if "kimi" in m.lower()]
    assert kimi_entries, "no Kimi entry found in the defaults at all"
    assert set(kimi_entries) == {KIMI_SEAT, _agentic(KIMI_SEAT)}, kimi_entries


def test_default_models_and_board_share_one_kimi_seat():
    """The flat panel's Kimi entry is KIMI_SEAT (diff-only); the board's Kimi seat is its
    agentic opencode form `_agentic(KIMI_SEAT)` (review-cli#24). Both derive from the SAME
    constant, so a future model-id bump can't update one and leave the other pointing at a
    stale/dead provider (the exact staleness #25 removed). Matched by MODEL string, not the
    display label (which is cosmetic and may be renamed). The board has a reserve so the
    agentic seat can fail over on an opencode-less host; the flat panel has none, which is
    why it keeps the robust key-only commandcode route."""
    assert KIMI_SEAT in DEFAULT_MODELS, DEFAULT_MODELS
    board_models = {r.model for r in DEFAULT_BOARD}
    assert _agentic(KIMI_SEAT) in board_models, board_models
    # And the agentic board seat carries the SAME wire model id as the flat-panel seat
    # (everything after the provider) — transport-only difference, never an id fork.
    assert KIMI_SEAT.split(":", 1)[1] == _agentic(KIMI_SEAT).split("/", 1)[1]


def test_default_models_routes_kimi_through_commandcode():
    """The FLAT panel's Kimi seat goes through the commandcode gateway (the live account),
    not the dead `oc:fireworks` opencode route — and `_split_models` round-trips the whole
    flat panel unchanged (no alias rewrite / no drop), the normalization the review path
    applies before dispatch. The flat panel stays diff-only on purpose: it has no
    reserve/failover, so it keeps the key-only route that needs no opencode install (#24)."""
    assert KIMI_SEAT.startswith("commandcode:"), KIMI_SEAT
    assert DEFAULT_MODELS == ("codex", "gemini", KIMI_SEAT, GROK_SEAT, MIMO_SEAT), (
        DEFAULT_MODELS
    )
    assert _split_models(list(DEFAULT_MODELS)) == [
        "codex",
        "gemini",
        KIMI_SEAT,
        GROK_SEAT,
        MIMO_SEAT,
    ]


def test_every_default_model_routes_live():
    """Every entry in the flat DEFAULT_MODELS panel AND every DEFAULT_BOARD seat must pass
    `backends.default_routes_live` (review-cli#25): the id BOTH takes an explicitly-named
    backend route AND its underlying provider is not known-dead.

    This is the anti-rot guard. The original #25 bug was a default
    `oc:fireworks/.../kimi-k2p6-turbo` on the suspended `glide` account: resolve_backend has
    a permissive opencode catch-all, so a stale default degrades SILENTLY at runtime rather
    than erroring. `default_routes_live` closes that by checking the provider UNDER any
    `oc:`/`opencode:` transport (not the wrapper): that provider must name a real backend
    (`_match_named_backend`, so a typo'd provider — flat `comandcode:...` OR agentic
    `oc:comandcode/...` — fails, since the bare `comandcode` names nothing) AND must not be
    in the dead-provider denylist (the forward-looking half for a once-live provider that
    later dies). A future stale default trips this in CI instead of rotting at runtime. It
    checks named ROUTING of the under-transport provider + the dead-provider denylist, not
    live network reachability — a probe would need keys and can't run in CI."""
    for model in _all_default_model_strings():
        assert backends.default_routes_live(model), (
            f"default model {model!r} is not safe to ship: its under-transport provider "
            f"{backends.effective_provider(model)!r} either names no backend (would rot via "
            f"the opencode catch-all) or is in the dead-provider denylist "
            f"(this is the #25 silent-rot guard)"
        )


def test_default_routes_live_catches_dead_and_typo_defaults_through_the_transport():
    """`default_routes_live` is not a no-op: it REJECTS the dead default #25 removed AND a
    typo'd provider — in BOTH the flat and the agentic `oc:` form, because the check is on
    the provider UNDER any transport, not on the `oc:` wrapper.

    Holes closed:
      * the dead `oc:fireworks/.../kimi-k2p6-turbo`: `effective_provider` peels to
        `fireworks`, which names no backend (and is in `_DEAD_PROVIDERS`), so it is rejected;
      * a typo'd provider in the FLAT form (`comandcode:...`) — the bare `comandcode` names
        no backend, rejected;
      * a typo'd provider in the AGENTIC form (`oc:comandcode/...`) — the `oc:` transport
        does NOT mask the typo, because the check peels to `comandcode`, which names no
        backend, rejected. This is the agentic hole the first cut missed (codex review);
      * a bogus bare id (`totally-bogus-model-xyz`) — names no backend, rejected.
    The live commandcode Kimi seat passes via BOTH transports."""
    dead = "oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo"
    assert backends.effective_provider(dead) == "fireworks"
    assert "fireworks" in backends._DEAD_PROVIDERS
    assert backends.default_routes_live(dead) is False
    # Typo'd provider — caught in the flat form AND in BOTH agentic spellings (`oc:` and the
    # `opencode:` alias). The agentic case is the one a name-only `_match_named_backend(model)`
    # check would have missed; both transports are checked for symmetry.
    assert backends.default_routes_live("comandcode:moonshotai/Kimi-K3") is False
    assert backends.default_routes_live("oc:comandcode/moonshotai/Kimi-K3") is False
    assert (
        backends.default_routes_live("opencode:comandcode/moonshotai/Kimi-K3") is False
    )
    # A stale default whose id names no route at all.
    assert backends.default_routes_live("totally-bogus-model-xyz") is False
    # A bare-alias provider + model suffix that resolve_backend does NOT match on the full
    # id (it accepts gemini only as the bare `gemini-api` or a `gemini:` prefix, never the
    # `gemini-api:` form) and so falls through to opencode at runtime. The guard must agree:
    # checking the COLLAPSED provider token would wrongly bless it, so the guard validates
    # the FULL id route (codex review of #49).
    assert (
        backends.resolve_backend("gemini-api:gemini-2.5-flash")
        is backends.review_opencode
    )
    assert backends.default_routes_live("gemini-api:gemini-2.5-flash") is False
    # ...but the spellings resolve_backend DOES match on the full id still pass.
    assert backends.default_routes_live("gemini:gemini-2.5-flash") is True
    assert backends.default_routes_live("zai:glm-5.3") is True
    # Mixed case: the guard lowercases exactly like resolve_backend, so a mixed-case id gets
    # the SAME verdict as its lowercase form (the guard must mirror the dispatcher, codex #49).
    assert backends.resolve_backend("Codex") is backends.resolve_backend("codex")
    assert backends.default_routes_live("Codex") is True
    assert backends.default_routes_live("OC:Commandcode/moonshotai/Kimi-K3") is True
    assert backends.default_routes_live("OC:Fireworks/x/y") is False  # dead, mixed case
    # Intentional flat-vs-agentic asymmetry: `oc:gemini-api/model` passes because the agentic
    # opencode transport DOES route an arbitrary `provider/model` (gemini-api is a real
    # opencode provider), whereas the flat keyed-HTTP `gemini-api:model` does not — they have
    # different runtime routes, and the guard tracks each (not a bug).
    assert (
        backends.resolve_backend("oc:gemini-api/gemini-2.5-flash")
        is backends.review_opencode
    )
    assert backends.default_routes_live("oc:gemini-api/gemini-2.5-flash") is True
    # The live defaults pass, via every transport spelling (flat, `oc:`, `opencode:`).
    assert backends.default_routes_live(KIMI_SEAT) is True
    assert backends.default_routes_live(_agentic(KIMI_SEAT)) is True
    assert backends.default_routes_live("opencode:zai/glm-5.3") is True
    assert backends.effective_provider(KIMI_SEAT) == "commandcode"
    assert backends.effective_provider(_agentic(KIMI_SEAT)) == "commandcode"


def test_effective_provider_peels_transport_and_splits_on_first_separator():
    """`effective_provider` peels the `oc:`/`opencode:` AGENTIC prefix, then takes the first
    segment before `:` or `/` — so the provider UNDER an agentic seat is what's checked, not
    the literal "opencode" transport. Covers the spellings the defaults actually use."""
    assert backends.effective_provider("codex") == "codex"
    assert (
        backends.effective_provider("commandcode:moonshotai/Kimi-K3") == "commandcode"
    )
    assert (
        backends.effective_provider("oc:commandcode/moonshotai/Kimi-K3")
        == "commandcode"
    )
    assert backends.effective_provider("opencode:zai/glm-5.3") == "zai"
    assert backends.effective_provider("zai:glm-5.3") == "zai"
    assert backends.effective_provider("oc:fireworks/x/y") == "fireworks"


def test_dead_provider_denylist_is_load_bearing_in_the_guard():
    """The `_DEAD_PROVIDERS` denylist actually CHANGES the verdict — it is not dead weight
    behind the named-route check. With `codex` forced into the denylist, `default_routes_live`
    rejects it even though `_match_named_backend('codex')` is non-None (a live named route).
    This exercises the denylist branch directly, so it can't silently stop mattering.

    Patches the module global manually with try/finally (NOT the pytest `monkeypatch`
    fixture): this file's `test_*` functions also run argument-less under the standalone
    `__main__` runner, where a fixture parameter would be an unfilled positional and the
    test would error. Same save/restore pattern the other tests here use."""
    assert backends._match_named_backend("codex") is not None  # codex IS a named route
    assert backends.default_routes_live("codex") is True  # ...and live by default
    saved = backends._DEAD_PROVIDERS
    backends._DEAD_PROVIDERS = frozenset({"codex"})
    try:
        assert (
            backends.default_routes_live("codex") is False
        )  # denylist flips the verdict
    finally:
        backends._DEAD_PROVIDERS = saved
    # Restored — the guard is back to its real verdict for the rest of the suite.
    assert backends.default_routes_live("codex") is True


def test_every_named_provider_bare_token_is_recognized():
    """For an AGENTIC `oc:`/`opencode:` default, `default_routes_live` checks the bare
    provider token under the transport (`_match_named_backend(effective_provider(model))`,
    e.g. `commandcode`/`zai` from `oc:commandcode/...`). That only works if every named
    provider's resolve_backend branch matches the bare token, not just the `provider:`-prefixed
    form — otherwise a legitimate agentic default on a bare-token-unmatched provider would get
    a false `False`.

    This pins that contract across ALL named providers the defaults use: each bare token
    must resolve to its backend. If a future provider is added with a branch that matches
    only `startswith('newprov:')`, an `oc:newprov/...` default (and this test) goes red — the
    fix is to make the branch accept the bare token too, so the #25 guard keeps working."""
    bare_to_backend = {
        "codex": backends.review_codex,
        "gemini": backends.review_gemini,
        "gemini-api": backends.review_gemini,
        "zai": backends.review_zai,
        "z.ai": backends.review_zai,
        "zhipu": backends.review_zai,
        "glm": backends.review_zai,
        "commandcode": backends.review_commandcode,
        "claude": backends.review_claude,
        "fable": backends.review_claude,
    }
    for token, backend in bare_to_backend.items():
        assert backends._match_named_backend(token) is backend, token
    # And the live agentic z.ai/GLM board seat passes the full guard (not just
    # effective_provider) — the bare-token contract is what makes that work.
    assert backends.default_routes_live("oc:zai/glm-5.3") is True


def test_agentic_helper_rewrites_provider_seat_idempotent_and_canonical():
    """`_agentic` flips a diff-only `provider:model` seat to its agentic `oc:provider/model`
    opencode form, and a colonless seat (e.g. `codex`) to `oc:<seat>`. It is IDEMPOTENT
    and CANONICAL: an already-agentic seat is returned in the `oc:` spelling — `oc:foo/bar`
    unchanged, and the `opencode:foo/bar` ALIAS normalized to `oc:foo/bar`. Both resolve to
    review_opencode and run the same `opencode -m foo/bar`, which the dashboard attributes
    to `oc:foo/bar`; canonicalizing keeps the board seat id == the attributed id so a seat
    never splits into a `no_data` board row plus a separate `oc:` health row (review-cli#24,
    codex review). Double-wrapping can't produce a nonsense `oc:oc/...` id."""
    assert (
        _agentic("commandcode:moonshotai/Kimi-K3")
        == "oc:commandcode/moonshotai/Kimi-K3"
    )
    assert _agentic("zai:glm-5.3") == "oc:zai/glm-5.3"
    assert _agentic("codex") == "oc:codex"
    # Idempotent: wrapping an already-`oc:` seat is a no-op (no `oc:oc/...`).
    assert _agentic("oc:zai/glm-5.3") == "oc:zai/glm-5.3"
    assert (
        _agentic(_agentic("commandcode:Qwen/Qwen3.8-Max"))
        == "oc:commandcode/Qwen/Qwen3.8-Max"
    )
    # Canonical: the `opencode:` alias is normalized to the canonical `oc:` spelling, so it
    # matches the dashboard's `oc:`-prefixed attribution of the same opencode run.
    assert _agentic("opencode:foo/bar") == "oc:foo/bar"
    assert _agentic(_agentic("opencode:foo/bar")) == "oc:foo/bar"


def test_default_pool_size_is_four():
    assert DEFAULT_POOL_SIZE == 4, DEFAULT_POOL_SIZE


def test_select_pool_default_picks_first_four_seats():
    """Default pool (no availability predicate) = the FIRST 4 seats by priority of the
    14-seat board (the rest are reserve). The pool now leads with Sol, Opus, then the
    GLM-5.3-via-commandcode seat, then Kimi (review-cli#fable-seat-reliability: Fable
    moved from priority 1 to the very last reserve seat — see
    `test_default_board_matches_directive_table`'s comment for the full rationale)."""
    pool = select_pool(list(DEFAULT_BOARD), DEFAULT_POOL_SIZE)
    assert len(pool) == 4
    assert [r.model for r in pool] == [r.model for r in DEFAULT_BOARD[:4]]
    assert [r.model for r in pool] == [
        "codex:gpt-6.1-sol",
        "claude:claude-opus-5-5",
        "commandcode:zai-org/GLM-5.3",
        "oc:commandcode/moonshotai/Kimi-K3",
    ]
    # The reserve is exactly the remainder (priority order): Astra, Luna/Sonnet
    # (review-cli#382 live fallbacks), Sonnet-omp (review-cli#168, same model different
    # auth path), the remaining opencode routes (review-cli#24), the diff-only Gemini,
    # Grok (agentic via opencode's native xai provider, directly BEFORE MiMo/the slow
    # GLM seat so failover reaches it first), MiMo (review-cli#450, live paid `tests`
    # fallback via omp OpenCode Go), the slow z.ai GLM seat (LAST-RESORT, review-cli#65
    # deprioritization), and finally Fable (LAST-RESORT, ~100% failure rate).
    reserve = [r.model for r in DEFAULT_BOARD[4:]]
    assert reserve == [
        "codex:gpt-6-astra",
        "codex:gpt-6-luna",
        "claude:claude-sonnet-5-5",
        "omp:anthropic/claude-sonnet-5-5",
        "oc:commandcode/Qwen/Qwen3.8-Max",
        "oc:commandcode/deepseek/deepseek-v4-pro",
        "gemini",
        "oc:xai/grok-4.7",
        "omp:opencode-go/mimo-v2.6-pro",
        "omp:zai/glm-5.3",
        "claude:claude-fable-5-1",
    ]


def test_select_pool_zero_or_negative_means_all_seats():
    for n in (0, -1, -8):
        assert [r.model for r in select_pool(list(DEFAULT_BOARD), n)] == [
            r.model for r in DEFAULT_BOARD
        ]


def test_select_pool_larger_than_board_is_clamped():
    assert len(select_pool(list(DEFAULT_BOARD), 99)) == len(DEFAULT_BOARD)


def test_select_pool_boundary_at_and_below_full_size():
    """Exact boundary: pool == len(board) -> all (the `pool >= len` short-circuit);
    pool == len(board) - 1 -> all but ONE seat, kept in priority order (GLM finding 11).
    review-cli#588: the seat left out is the lowest-priority one repeating a role an
    earlier seat covers (the z.ai GLM `security` seat), not Fable, whose `architect`
    lens no other seat has."""
    n = len(DEFAULT_BOARD)
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), n)] == [
        r.model for r in DEFAULT_BOARD
    ]
    dropped = "omp:zai/glm-5.3"
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), n - 1)] == [
        r.model for r in DEFAULT_BOARD if r.model != dropped
    ]


def test_select_pool_picks_first_n_in_order():
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), 2)] == [
        r.model for r in DEFAULT_BOARD[:2]
    ]
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), 1)] == [
        DEFAULT_BOARD[0].model
    ]


def test_select_pool_empty_board_stays_empty():
    assert select_pool([], 4) == []


def test_select_pool_does_not_mutate_input():
    """select_pool must return a NEW list, leaving the caller's board untouched — a
    future refactor returning a slice-view would silently mutate it (GLM finding 1)."""
    src = list(DEFAULT_BOARD)
    src_snapshot = list(src)
    out = select_pool(src, 4)
    out.append(BoardReviewer("x", "tests", "X"))  # mutate the result
    assert src == src_snapshot, "select_pool mutated its input board"
    # And the "all" branch (pool<=0) is a copy too, not the same object.
    assert select_pool(src, 0) is not src


def test_glm_seat_routes_agentically_via_omp_zai_provider():
    """The GLM seat must run AGENTICALLY (review-cli#24), so it reads the repo read-only
    instead of the diff-only z.ai REST call. Since review-cli#645 it is omp's `zai`
    provider: resolve_backend(omp:zai/glm-5.3) -> review_omp, and the omp model selector
    (after `omp:`) is `zai/glm-5.3` — his z.ai subscription, glm-5.3 the newest GLM, the
    same id the diff-only `zai:glm-5.3` route POSTs."""
    seat = next(r for r in DEFAULT_BOARD if r.display == "GLM")
    assert seat.model == "omp:zai/glm-5.3", seat.model
    assert backends.resolve_backend(seat.model) is backends.review_omp
    # The omp model selector (after stripping `omp:`) is the z.ai provider/model pair.
    assert seat.model.split(":", 1)[1] == "zai/" + backends.ZAI_DEFAULT_MODEL


def test_all_repo_capable_default_seats_are_agentic():
    """review-cli#24 acceptance, as amended by the GLM-5.3-via-commandcode seat: every
    DEFAULT_BOARD seat that HAS an agentic transport uses it (the codex CLI, opencode, the
    claude CLI, or omp) — not a stateless diff-only REST/keyed-HTTP call. Exactly TWO seats
    stay diff-only, each because NO agentic transport reaches them:
      * Gemini — a workspace-less REST API, no agentic CLI/opencode provider; and
      * GLM-cc (`commandcode:zai-org/GLM-5.3`, priority 3) — opencode's `commandcode`
        provider does NOT register this GLM id, so `oc:commandcode/zai-org/GLM-5.3` errors;
        the keyed-HTTP route is the only one that reaches it (verified live).

    This still pins the agentic-by-default contract for the seats that CAN be agentic:
    Kimi/Qwen/DeepSeek go through opencode (`oc:`), Sol/Astra through the codex CLI,
    the two Anthropic seats through claude, MiMo and z.ai-GLM through omp (review-cli#450 — omp's
    read-only three-layer cage mounts the repo, so it is agentic, not diff-only, same as
    the codex/opencode/claude CLI transports). A future edit that silently reverts one of
    THOSE to the diff-only REST route fails here. (claude is agentic via its CLI path;
    resolve_backend returns review_claude for both, and the board's claude seats run the
    CLI on a normal host.)"""
    agentic = {
        backends.review_codex,
        backends.review_opencode,
        backends.review_claude,
        backends.review_omp,
    }
    # The seats that are legitimately diff-only (no agentic transport exists), keyed by the
    # backend they MUST route to. Any OTHER seat falling to a diff-only backend is the
    # silent regression #24 guards against.
    allowed_diff_only = {
        "Gemini": backends.review_gemini,
        "GLM-cc": backends.review_commandcode,
    }
    for seat in DEFAULT_BOARD:
        backend = backends.resolve_backend(seat.model)
        if seat.display in allowed_diff_only:
            assert backend is allowed_diff_only[seat.display], seat.model
            continue
        assert backend in agentic, f"{seat.display} ({seat.model}) is not agentic"
    # Belt-and-suspenders: the ONLY diff-only commandcode/z.ai REST seat on the default board
    # is the deliberate priority-3 GLM-cc one; every other seat is agentic (or Gemini). A
    # second keyed-HTTP commandcode/z.ai seat slipping in is the regression to catch.
    diff_only_backends = {backends.review_commandcode, backends.review_zai}
    rest_seats = [
        s.display
        for s in DEFAULT_BOARD
        if backends.resolve_backend(s.model) in diff_only_backends
    ]
    assert rest_seats == ["GLM-cc"], rest_seats


def test_install_skill_text_documents_agentic_default_board():
    """The embedded skill text `review install-skill` writes into agent harnesses must
    reflect the agentic default board (review-cli#24, codex review) — otherwise agents keep
    the stale diff-only mental model. It must mention the `oc:` default seats AND clarify
    that the keyed-HTTP commandcode/z.ai backends back only the explicit `-m cc`/`-m glm`
    paths, not the default board."""
    from reviewlib.install import SKILL_MD

    low = SKILL_MD.lower()
    assert "oc:commandcode/" in SKILL_MD, (
        "skill text must name the agentic oc: default seats"
    )
    assert "omp:zai/glm-5.3" in SKILL_MD, SKILL_MD[:0]
    # The keyed-HTTP section must be scoped to explicit `-m cc`/`-m glm`, not the default board.
    assert "diff-only" in low and "default board" in low
    assert "selected preset/board" in low
    assert "--preset heavy --pool 0" in SKILL_MD
    # review-cli#fable-seat-reliability: the heavy preset excludes Fable (~100%
    # dispatch failure rate). review-cli#382 added LUNA_SEAT/SONNET_SEAT (11 built-ins),
    # review-cli#165 added GROK_SEAT (12), and review-cli#450 added MIMO_SEAT, so --pool 0
    # now covers 13 built-ins. Normalized whitespace (GLM review finding): matching the
    # raw string coupled this assertion to exactly where install.py's paragraph happens
    # to line-wrap — a future reflow of that markdown with no content change would break
    # it for no real reason.
    assert "covers all 13 heavy-preset-built-ins" in " ".join(SKILL_MD.split())
    """review-cli#24 contract (codex review): the agentic `oc:` board seats authenticate via
    opencode's OWN provider config — NOT review-cli's `COMMANDCODE_API_KEY`/`ZAI_API_KEY`.
    So their availability gates on the `opencode` BINARY plus opencode's OWN provider
    config — NOT review-cli's `COMMANDCODE_API_KEY`/`ZAI_API_KEY`. A host whose keys live only
    in review-cli's `.env` (and never configured opencode's `commandcode` provider) must NOT
    have an `oc:commandcode/...` seat falsely reported available on key-presence alone."""
    oc_seats = [r.model for r in DEFAULT_BOARD if r.model.startswith("oc:")]
    assert oc_seats, "expected agentic oc: seats on the default board"

    saved_which = backends._which
    # Snapshot EVERY env var we mutate (incl. GEMINI_ENV_FILE, which we overwrite below) so
    # a dev/CI environment that already sets any of them is restored exactly — otherwise
    # later tests become order-dependent on this one's teardown (codex review).
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "COMMANDCODE_API_KEY",
            "ZAI_API_KEY",
            "ZHIPU_API_KEY",
            "GEMINI_ENV_FILE",
            "OC_AUTH_FILE",
            "OC_CONFIG_FILE",
        )
    }
    try:
        # Scrub review-cli's commandcode/z.ai keys AND point the env-file at nothing, so the
        # only things that could make a seat available are opencode itself and opencode auth.
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            auth_file = tmp / "auth.json"
            config_file = tmp / "opencode.json"
            auth_file.write_text("{}", encoding="utf-8")
            config_file.write_text('{"provider": {}}', encoding="utf-8")
            os.environ["OC_AUTH_FILE"] = str(auth_file)
            os.environ["OC_CONFIG_FILE"] = str(config_file)

            # opencode present but commandcode provider auth missing -> commandcode seats
            # are skipped at startup. Other oc providers keep their existing semantics.
            backends._which = lambda name: f"/fake/bin/{name}"
            commandcode_seats = [
                seat for seat in oc_seats if seat.startswith("oc:commandcode/")
            ]
            assert commandcode_seats, oc_seats
            _assert_backend_availability(commandcode_seats, False)

            # opencode provider auth present -> commandcode oc seats are available, still
            # without consulting review-cli's COMMANDCODE_API_KEY.
            config_file.write_text(
                '{"provider": {"commandcode": {"options": {"apiKey": "user_opencode"}}}}',
                encoding="utf-8",
            )
            _assert_backend_availability(commandcode_seats, True)

            # opencode absent -> unavailable (so the board backfills), even if review-cli
            # keys existed they would not rescue an `oc:` seat — it needs the opencode binary.
            def _no_opencode(name: str) -> str:
                if name == "opencode":
                    raise RuntimeError("opencode not found")
                return f"/fake/bin/{name}"

            backends._which = _no_opencode
            _assert_backend_availability(oc_seats, False)
    finally:
        backends._which = saved_which
        # Restore each mutated var to its exact prior state (set it back, or remove it if it
        # was unset before) — never leave a stale override for the next test.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_contracts_role_has_a_lens():
    """The contracts lens still exists and is non-overlapping (public-API/backward-compat),
    regardless of which priority seat carries it now."""
    assert "contracts" in REVIEW_ROLES
    lens = REVIEW_ROLES["contracts"].lower()
    assert "public api" in lens or "api shape" in lens, lens
    assert "backward" in lens or "compat" in lens, lens
    # Some default seat carries the contracts lens.
    assert any(r.role == "contracts" for r in DEFAULT_BOARD)
