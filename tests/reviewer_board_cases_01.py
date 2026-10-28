"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    DEFAULT_PRESET,
    DEFAULT_PRESET_BOARD,
    GLM_COMMANDCODE_SEAT,
    GROK_SEAT,
    HEAVY_PRESET_BOARD,
    LIGHT_PRESET_BOARD,
    LUNA_SEAT,
    MIMO_SEAT,
    REPO_ROOT,
    ReviewResult,
    SOL_SEAT,
    SONNET_SEAT,
    _model_ids,
    backends,
    board_from_models,
    load_board,
    os,
    preset_board,
)

def test_default_board_matches_directive_table():
    # Priority order (failover pool): strongest WORKING model first. Each seat keeps a
    # role/lens, but selection is by PRIORITY + availability, not role order.
    expected = [
        ("codex:gpt-6.1-sol", "consistency", "Sol"),
        ("claude:claude-opus-5-5", "correctness", "Opus"),
        # Seat 3: GLM-5.3 via the Command Code gateway, directly under Opus.
        # DIFF-ONLY keyed HTTP (review_commandcode) — opencode's commandcode provider does
        # not register this GLM id, so the agentic form errors; read-only by construction.
        # Role `performance` (NOT correctness) so it doesn't duplicate Opus's lens.
        ("commandcode:zai-org/GLM-5.3", "performance", "GLM-cc"),
        # Seat 4 is the first reserve and preserves lens diversity when a top seat drops.
        ("oc:commandcode/moonshotai/Kimi-K3", "quality", "Kimi"),
        # Seat 5 is the agentic codex CLI route, pinned to GPT-6-Astra (ASTRA_SEAT) rather
        # than the bare `"codex"` seat this used to be (see config.py for rationale). Role
        # LEFT AS `consistency` (a duplicate of Sol's lens) by review-cli#382 -- an earlier
        # draft flipped it to `security`, but that left `consistency` with ZERO live
        # fallback anywhere on the board; see config.py's comment on this exact seat.
        ("codex:gpt-6-astra", "consistency", "Astra"),
        # Seats 6-7 (review-cli#382): LUNA_SEAT/SONNET_SEAT, live fallbacks for the
        # `performance`/`quality` roles whose only prior seats (GLM-cc/Kimi) are disabled
        # whenever `unpaid_providers` includes `commandcode`. Placed right after Astra so
        # neither the raw top-4 nor DEFAULT_PRESET_BOARD's top-4 window picks up a
        # duplicate role in the common (nothing disabled) case.
        ("codex:gpt-6-luna", "performance", "Luna"),
        ("claude:claude-sonnet-5-5", "quality", "Sonnet"),
        # Seat 7.5 (review-cli#168): OMP_CLAUDE_SEAT, the SAME claude-sonnet-5-5 model
        # dispatched through the omp harness instead of the direct claude CLI -- a
        # DIFFERENT auth path, so it survives an outage that takes out every claude:*
        # seat's shared account state.
        ("omp:anthropic/claude-sonnet-5-5", "quality", "Sonnet-omp"),
        # Seats 4, 8-9 and 11 route through opencode (`oc:`) so they run AGENTICALLY (read the
        # repo read-only), not the diff-only commandcode/z.ai REST call (review-cli#24). Seats
        # 6-7 (Luna/Sonnet, review-cli#382) run via the codex/claude CLIs, not opencode.
        ("oc:commandcode/Qwen/Qwen3.8-Max", "security", "Qwen"),
        ("oc:commandcode/deepseek/deepseek-v4-pro", "tests", "DeepSeek"),
        ("gemini", "contracts", "Gemini"),
        # Seat 11 — Grok 4.7 via opencode's native `xai` provider, agentic (oauth-
        # authenticated, no diff-only fallback route exists for this provider). Sits
        # BEFORE the z.ai GLM last-resort seat so deep mid-run failover reaches a fast
        # reserve before paying GLM's #65 pathological-slowness cost. Role `security`
        # (NOT `performance`, which already has a live paid fallback in Luna) — see the
        # seat's own comment in `reviewlib/config.py`.
        ("oc:xai/grok-4.7", "security", "Grok"),
        # Seat 12 — MiMo V2.6 Pro via omp OpenCode Go (review-cli#450). Live paid `tests`
        # fallback: DeepSeek is the only other tests seat and is commandcode (often
        # unpaid). Same rationale as GROK filling `security`. Sits AFTER Grok, BEFORE
        # the slow z.ai GLM last-resort, Fable stays LAST.
        ("omp:opencode-go/mimo-v2.6-pro", "tests", "MiMo"),
        # GLM-5.3 via omp's `zai` provider (his z.ai subscription), agentic. Distinct
        # from the seat-3 commandcode GLM: same model family, different provider/transport.
        # DEPRIORITIZED to LAST-RESORT reserve (review-cli#65): it is pathologically slow
        # under load, so it is promoted before only Fable — Qwen/DeepSeek/Gemini/Grok go first.
        # review-cli#382: role changed from `quality` (now covered by Sonnet above, which
        # doesn't share this seat's real-world quota fragility) to `security`.
        ("omp:zai/glm-5.3", "security", "GLM"),
        # Fable 5.1 (Anthropic flagship). DEMOTED from priority 1 to the very LAST seat
        # (review-cli#fable-seat-reliability): confirmed 97.9-100% dispatch failure rate
        # (chronic session/usage-limit exhaustion) made priority 1 a near-certain-doomed
        # dispatch on every single default review. Worse than GLM's "merely slow" profile,
        # so it sits even later than GLM — still a reserve seat, not removed outright.
        ("claude:claude-fable-5-1", "architect", "Fable"),
    ]
    got = [(r.model, r.role, r.display) for r in DEFAULT_BOARD]
    assert got == expected, got


def test_default_board_is_priority_ordered():
    """The CTO's priority sketch (strongest-WORKING-model first): Sol, Opus,
    GLM-5.3-via-commandcode, Kimi, Astra, Luna, Sonnet, Sonnet-omp, Qwen, DeepSeek,
    Gemini, GLM-5.3-via-z.ai, Fable. Re-ranking = reordering
    DEFAULT_BOARD; this pins the order. Seats 4 and 9-10 are AGENTIC opencode (`oc:`)
    routes (review-cli#24) and the z.ai GLM seat is the agentic omp route (review-cli#645);
    the commandcode GLM at #3 is diff-only keyed HTTP. Luna/Sonnet
    (review-cli#382) are live fallbacks for performance/quality, inserted right after Astra.
    Sonnet-omp (review-cli#168) is the SAME Sonnet model via a different auth path,
    right after Sonnet. The z.ai GLM seat is DEPRIORITIZED to next-to-last (review-cli#65)
    — pathologically slow under load. Fable is LAST (review-cli#fable-seat-reliability) —
    a confirmed ~100% dispatch failure rate, worse than merely slow."""
    assert [r.model for r in DEFAULT_BOARD] == [
        "codex:gpt-6.1-sol",
        "claude:claude-opus-5-5",
        "commandcode:zai-org/GLM-5.3",
        "oc:commandcode/moonshotai/Kimi-K3",
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


def test_glm_commandcode_seat_sits_directly_under_opus():
    """The CTO directive: GLM 5.3 via commandcode must sit IMMEDIATELY after Opus, so the
    pool tries Opus first, then this GLM. Pin the adjacency by index so a future re-rank that
    pulls them apart trips here. Uses the canonical constant (one source of truth)."""
    models = [r.model for r in DEFAULT_BOARD]
    opus_i = models.index("claude:claude-opus-5-5")
    glm_i = models.index(GLM_COMMANDCODE_SEAT)
    assert glm_i == opus_i + 1, (opus_i, glm_i, models)
    # Opus sits immediately after Sol (priority 1), so GLM is priority 3 (index 2).
    assert glm_i == 2, glm_i


def test_fable_seat_is_last_resort_reserve():
    """review-cli#fable-seat-reliability: Fable is the LAST seat on the board (the
    lowest-priority reserve), demoted from priority 1 after a confirmed 97.9-100%
    dispatch failure rate — see `test_default_board_matches_directive_table`'s comment
    for the full rationale. Pinned by index (not just membership) so a future re-rank
    that accidentally moves it back up trips here."""
    models = [r.model for r in DEFAULT_BOARD]
    assert models[-1] == "claude:claude-fable-5-1", models
    fable = next(r for r in DEFAULT_BOARD if r.model == "claude:claude-fable-5-1")
    assert fable.display == "Fable"
    assert fable.role == "architect"


def test_glm_commandcode_seat_is_the_canonical_constant():
    """The seat's model string is GLM_COMMANDCODE_SEAT byte-exact, and it is the
    commandcode gateway id (`zai-org/GLM-5.3`), not the z.ai-route `glm-5.3` id."""
    assert GLM_COMMANDCODE_SEAT == "commandcode:zai-org/GLM-5.3"
    seat = next(r for r in DEFAULT_BOARD if r.model == GLM_COMMANDCODE_SEAT)
    assert seat.display == "GLM-cc"


def test_grok_seat_is_the_canonical_constant_and_resolves_to_opencode():
    """GROK_SEAT is byte-exact "oc:xai/grok-4.7" (pinned so the seat can't silently
    drift to a different spelling), sits on the board as "Grok", and resolves through
    review_opencode end-to-end (not the bare/colon xai forms, which are deliberately
    NOT routed anywhere — see test_bare_and_colon_xai_forms_are_not_routed)."""
    assert GROK_SEAT == "oc:xai/grok-4.7"
    seat = next(r for r in DEFAULT_BOARD if r.model == GROK_SEAT)
    assert seat.display == "Grok"
    # `security`, not `performance` (review round 1 of #167, Fable): after review-cli#386
    # `performance` already has a live paid fallback (Luna) while `security` is held
    # only by Qwen (unpaid/disabled) and the slow z.ai GLM seat.
    assert seat.role == "security"
    assert backends.resolve_backend(GROK_SEAT) is backends.review_opencode
    assert backends.default_routes_live(GROK_SEAT) is True
    assert backends.effective_provider(GROK_SEAT) == "xai"


def test_grok_aliases_resolve_to_the_canonical_seat():
    """`grok` / `grok47` / `grok-4.7` expand to GROK_SEAT; `grok46`/`grok-4.6` and
    `grok45` stay pinned to their own releases and do not track GROK_SEAT."""
    from reviewlib.config import MODEL_ALIASES

    assert MODEL_ALIASES["grok"] == GROK_SEAT
    assert MODEL_ALIASES["grok47"] == GROK_SEAT
    assert MODEL_ALIASES["grok-4.7"] == GROK_SEAT
    assert MODEL_ALIASES["grok46"] == "oc:xai/grok-4.6"
    assert MODEL_ALIASES["grok-4.6"] == "oc:xai/grok-4.6"
    assert MODEL_ALIASES["grok45"] == "oc:xai/grok-4.5"
    assert MODEL_ALIASES["grok45"] != GROK_SEAT


def test_mimo_seat_is_the_canonical_constant_and_resolves_to_omp():
    """MIMO_SEAT is byte-exact "omp:opencode-go/mimo-v2.6-pro" (pinned so the seat can't
    silently drift to a different spelling), sits on the board as "MiMo" with role
    `tests`, and resolves through review_omp (OpenCode Go via omp). DeepSeek is the
    only other tests seat and is commandcode (often unpaid); MIMO is the live paid
    tests fallback — same rationale as GROK filling security."""
    assert MIMO_SEAT == "omp:opencode-go/mimo-v2.6-pro"
    seat = next(r for r in DEFAULT_BOARD if r.model == MIMO_SEAT)
    assert seat.display == "MiMo"
    assert seat.role == "tests"
    assert backends.resolve_backend(MIMO_SEAT) is backends.review_omp
    assert backends.default_routes_live(MIMO_SEAT) is True
    assert backends.effective_provider(MIMO_SEAT) == "omp"


def test_mimo_aliases_resolve_to_the_canonical_seat():
    """`mimo` / `mimo26` expand to MIMO_SEAT; `mimo25` / `mimo-2.5` stay on MiMo 2.5."""
    from reviewlib.config import MODEL_ALIASES

    assert MODEL_ALIASES["mimo"] == MIMO_SEAT
    assert MODEL_ALIASES["mimo26"] == MIMO_SEAT
    assert MODEL_ALIASES["mimo25"] == "omp:opencode-go/mimo-v2.5"
    assert MODEL_ALIASES["mimo-2.5"] == "omp:opencode-go/mimo-v2.5"


def test_oc_mimo_catalog_id_is_not_default_safe():
    """`oc:opencode/mimo-v2.5-free` is LIVE in `opencode models` and resolve_backend
    routes it to review_opencode, but default_routes_live is False: the provider
    `opencode` under `oc:` is not a named backend token and is not in
    `_AGENTIC_ONLY_PROVIDERS` (currently {xai}). Do not ship it as a DEFAULT;
    explicit `-m oc:opencode/mimo-v2.5-free` still works at runtime. MIMO_SEAT uses
    the omp OpenCode Go spelling instead."""
    oc_id = "oc:opencode/mimo-v2.5-free"
    assert backends.resolve_backend(oc_id) is backends.review_opencode
    assert backends.default_routes_live(oc_id) is False


def test_mimo_seat_is_on_default_models():
    assert MIMO_SEAT in DEFAULT_MODELS


def test_bare_and_colon_xai_forms_are_not_named_routes():
    """Bare "xai" and colon-form "xai:<model>" are deliberately UNMATCHED by
    _match_named_backend (review of #166): "xai:grok-4.5" would hand opencode the wrong
    spelling ("grok-4.5" instead of "xai/grok-4.5" — review_opencode peels only the first
    colon), and bare "xai" has no default-model resolution at all — unlike "zai"/
    "commandcode", which DO have a bare-form default.

    This does NOT mean these forms are unreachable end-to-end: resolve_backend()'s
    existing generic catch-all (any string _match_named_backend rejects still falls
    through to review_opencode, for back-compat with arbitrary provider/model selectors)
    still sends both forms to opencode with that broken argv — see
    test_bare_and_colon_xai_forms_still_hit_the_generic_catch_all. What THIS test proves
    is narrower and is the actual point of the change: neither form is a NAMED, documented
    route, so neither can be mistaken for the supported seat spelling, and (the next test)
    neither can ship as a DEFAULT via the #25 anti-rot guard. Only "oc:xai/<model>" is a
    valid, working, DOCUMENTED invocation."""
    assert backends._match_named_backend("xai") is None
    assert backends._match_named_backend("xai:grok-4.5") is None
    # But the guard still accepts GROK_SEAT as a safe default (agentic-only exception).
    assert backends.default_routes_live(GROK_SEAT) is True


def test_bare_and_colon_xai_forms_still_hit_the_generic_catch_all():
    """Honesty check (review of #166 round 3): resolve_backend("xai") and
    resolve_backend("xai:grok-4.5") are NOT specially blocked by this change — they still
    resolve to review_opencode via the PRE-EXISTING generic catch-all
    (`_match_named_backend(model.lower()) or review_opencode`), which sends ANY
    unrecognized model id to opencode for back-compat with arbitrary
    `provider/model` selectors. A user who types `-m xai` or `-m xai:grok-4.5` still gets
    a real (broken) opencode invocation — the exact same generic risk as typing any other
    unrecognized provider string, e.g. `-m definitely-not-a-provider`. This change never
    attempted to close that general gap; it only keeps bare/colon xai forms OUT of the
    NAMED-route surface (_match_named_backend) and the DEFAULT-shipping surface
    (default_routes_live), so nothing about this diff could accidentally promote a broken
    invocation shape to a supported default."""
    assert backends.resolve_backend("xai") is backends.review_opencode
    assert backends.resolve_backend("xai:grok-4.5") is backends.review_opencode
    # Same generic behavior for an unrelated bogus provider string — not xai-specific.
    assert (
        backends.resolve_backend("definitely-not-a-provider")
        is backends.review_opencode
    )


def test_bare_slash_form_reaches_opencode_correctly_but_is_not_the_default_safe_spelling():
    """Codex review of #166 round 7: a bare "xai/grok-4.5" (no "oc:" prefix, no colon) is
    DIFFERENT from the "xai"/"xai:grok-4.5" forms above — review_opencode splits on the
    first ":" and passes the remainder unchanged when there is none, so this form actually
    constructs the CORRECT opencode invocation ("opencode -m xai/grok-4.5") via the same
    generic catch-all. It is not documented as invalid; it's just not the canonical,
    DEFAULT-safe GROK_SEAT spelling, so default_routes_live correctly still rejects it as a
    default (it isn't a NAMED route — only oc:/opencode:-prefixed or otherwise explicitly
    matched ids are)."""
    assert backends.resolve_backend("xai/grok-4.5") is backends.review_opencode
    assert backends.default_routes_live("xai/grok-4.5") is False


def test_bare_and_colon_xai_forms_fail_the_default_routes_live_guard():
    """Bare "xai" and colon-form "xai:<model>" are not just unmatched by
    _match_named_backend — they must ALSO fail default_routes_live itself (not merely
    the matcher), so nobody could ship either spelling as a DEFAULT and have the #25
    anti-rot guard wave it through via the new _AGENTIC_ONLY_PROVIDERS exemption. The
    exemption is scoped to the under-transport check (a provider peeled from an oc:/
    opencode: prefix), not to these bare/colon top-level ids."""
    assert backends.default_routes_live("xai") is False
    assert backends.default_routes_live("xai:grok-4.5") is False


def test_agentic_only_exemption_does_not_widen_to_unknown_providers():
    """_AGENTIC_ONLY_PROVIDERS is a narrow, closed set — an unrelated typo'd or unknown
    provider under the oc:/opencode: transport must still fail default_routes_live, so a
    regression that widens the exemption (or inverts the guard's "and") trips this test
    immediately."""
    assert "nosuchprovider" not in backends._AGENTIC_ONLY_PROVIDERS
    assert backends.default_routes_live("oc:nosuchprovider/model") is False
    assert backends.default_routes_live("oc:comandcode/some-model") is False


def test_agentic_only_exemption_requires_a_real_model_selector():
    """The _AGENTIC_ONLY_PROVIDERS exemption must not wave through a malformed default
    with no model segment (review of #166 round 5): opencode's CLI wants
    `-m provider/model`, so `oc:xai` (no slash at all), `oc:xai:` (colon-form, not the
    agentic slash form), and `oc:xai/` (slash present but no model after it) are all
    invalid invocations that must fail default_routes_live, not merely happen to name a
    known agentic-only provider. Only a real `provider/model` selector — the documented
    GROK_SEAT form — passes."""
    assert backends.default_routes_live("oc:xai") is False
    assert backends.default_routes_live("oc:xai:") is False
    assert backends.default_routes_live("oc:xai/") is False
    assert backends.default_routes_live("oc:xai/ ") is False
    assert backends.default_routes_live(GROK_SEAT) is True


def test_agentic_only_exemption_rejects_colon_contaminated_selectors():
    """A naive "any slash + nonblank suffix" check (review of #166 round 5) let
    colon-contaminated forms slip through, because effective_provider() peels on the
    FIRST `:` OR `/` (whichever comes first) — so `oc:xai:grok/model` peels to provider
    "xai" while the raw peeled string still contains a `/` further along. The guard must
    require the segment BEFORE the first `/` to be EXACTLY the provider (no leftover
    colon), and reject a double-slash (empty inner segment, model starting with `/`)."""
    assert backends.default_routes_live("oc:xai:grok/model") is False
    assert backends.default_routes_live("oc:xai:/grok") is False
    assert backends.default_routes_live("oc:xai//grok") is False


def test_selector_well_formedness_applies_to_named_providers_too():
    """The `provider/model` well-formedness check is one helper applied to EVERY
    `oc:`/`opencode:` id before the provider check (review round 1 of #167, Fable/Sonnet):
    previously it ran only inside the `_AGENTIC_ONLY_PROVIDERS` branch, so a malformed
    default on a NAMED provider — blank model, double slash, colon contamination, no
    slash — sailed past the #25 anti-rot guard and would rot at runtime."""
    assert backends.default_routes_live("oc:zai/glm-5.3") is True
    assert backends.default_routes_live("oc:zai/") is False
    assert backends.default_routes_live("oc:zai//glm-5.3") is False
    assert backends.default_routes_live("oc:zai:glm/x") is False
    assert backends.default_routes_live("oc:commandcode") is False
    assert backends.default_routes_live("opencode:commandcode/deepseek/deepseek-v4-pro")
    assert backends.default_routes_live("opencode:commandcode//deepseek") is False


def test_transport_peel_is_shared_by_provider_and_guard():
    """`effective_provider` and `default_routes_live` peel the agentic transport through
    ONE helper (`_peel_oc_transport`, over `_OC_TRANSPORT_PREFIXES`), so the two can never
    disagree on what counts as the transport prefix."""
    assert backends._OC_TRANSPORT_PREFIXES == ("oc:", "opencode:")
    assert backends._peel_oc_transport("oc:xai/grok-4.5") == "xai/grok-4.5"
    assert backends._peel_oc_transport("opencode:zai/glm-5.3") == "zai/glm-5.3"
    assert backends._peel_oc_transport("zai:glm-5.3") is None
    # Only ONE prefix is peeled: a doubled transport is not a valid selector.
    assert backends._peel_oc_transport("opencode:oc:xai/grok-4.5") == "oc:xai/grok-4.5"
    assert backends.default_routes_live("opencode:oc:xai/grok-4.5") is False
    assert backends._oc_selector_is_well_formed("xai/grok-4.5", "xai") is True
    assert backends._oc_selector_is_well_formed("xai:grok/model", "xai") is False
    assert backends._oc_selector_is_well_formed("xai//grok", "xai") is False
    assert backends._oc_selector_is_well_formed("xai", "xai") is False


def test_oc_selector_rejects_whitespace_and_empty_segments():
    """Review round 2 (GH-165), Codex: `_canonical_provider` strips its argument, so a
    whitespace-contaminated selector (`oc: xai/grok-4.5`, `oc:xai /grok-4.5`, a tab, a
    trailing space on the model) used to pass the guard — yet `review_opencode` hands
    the selector to `opencode -m` untrimmed, so such a default would clear the anti-rot
    CI check and fail at dispatch. Whitespace is rejected outright, never canonicalised
    away; and an EMPTY segment anywhere (interior `deepseek//v4`, trailing `grok-4.5/`)
    is malformed too, not just the one right after the provider."""
    for selector in (
        " xai/grok-4.5",
        "xai /grok-4.5",
        "xai\t/grok-4.5",
        "xai/grok-4.5 ",
    ):
        assert backends._oc_selector_is_well_formed(selector, "xai") is False, selector
        assert backends.default_routes_live(f"oc:{selector}") is False, selector
    for selector in ("xai/grok-4.5/", "commandcode/deepseek//v4"):
        assert (
            backends._oc_selector_is_well_formed(selector, selector.split("/")[0])
            is False
        )
    assert backends.default_routes_live("oc: xai/grok-4.5") is False
    assert backends.default_routes_live("opencode:xai\t/grok-4.5") is False
    assert (
        backends.default_routes_live("oc:commandcode/deepseek/deepseek-v4-pro") is True
    )


def test_glm_commandcode_seat_routes_readonly_through_review_commandcode():
    """The seat resolves to review_commandcode (a stateless keyed-HTTP REST backend that
    POSTs ONLY the diff — read-only by construction, no repo/tools/exec), NOT to an agentic
    opencode/codex/claude transport. This is what makes it caged without an `-s read-only`
    flag: a REST POST has no workspace to write to."""
    assert backends.resolve_backend(GLM_COMMANDCODE_SEAT) is backends.review_commandcode
    # It passes the #25 default-routing guard (named backend, provider not dead).
    assert backends.default_routes_live(GLM_COMMANDCODE_SEAT) is True
    assert backends.effective_provider(GLM_COMMANDCODE_SEAT) == "commandcode"


def test_glm_commandcode_seat_degrades_gracefully_without_a_key():
    """When COMMANDCODE_API_KEY is absent the seat must NOT report available (so the pool
    backfills it from the reserve) — graceful degradation, never a hard failure. Mirrors how
    every other key-gated backend behaves when its credential is missing.

    Patches the module globals manually with try/finally (NOT the pytest `monkeypatch`
    fixture), so the test runs unchanged under the standalone `__main__` runner where a
    fixture parameter would be an unfilled positional arg."""
    import reviewlib.backends as b

    saved_resolve = b._resolve_key
    saved_mode = os.environ.pop("REVIEW_COMMANDCODE_MODE", None)
    saved_fake = os.environ.pop("REVIEW_FAKE_BACKEND", None)
    # Also drop COMMANDCODE_API_KEY from the live env for the duration: a dev/CI host that
    # exports the real key would otherwise let any os.environ-direct read inside
    # backend_available see it and make this a false-green (review of #57). Stubbing
    # _resolve_key alone only covers the resolver path; popping the env var makes the
    # "no key anywhere" precondition explicit and resolver-independent.
    saved_key = os.environ.pop("COMMANDCODE_API_KEY", None)
    # Force the key resolver to find nothing (no env, no shared .env file).
    b._resolve_key = lambda *a, **k: ""
    try:
        assert backends.backend_available(GLM_COMMANDCODE_SEAT) is False
    finally:
        b._resolve_key = saved_resolve
        if saved_mode is not None:
            os.environ["REVIEW_COMMANDCODE_MODE"] = saved_mode
        if saved_fake is not None:
            os.environ["REVIEW_FAKE_BACKEND"] = saved_fake
        if saved_key is not None:
            os.environ["COMMANDCODE_API_KEY"] = saved_key


def test_glm_commandcode_seat_carries_performance_not_a_duplicate_role():
    """GLM-cc must NOT reuse Opus's `correctness` lens — that would duplicate a role in the
    default top-4 pool and silently drop the `performance` lens from a plain `review diff`
    (review of #57). It carries `performance`, so inserting this seat is a priority change,
    not a coverage loss."""
    glmcc = next(r for r in DEFAULT_BOARD if r.model == GLM_COMMANDCODE_SEAT)
    opus = next(r for r in DEFAULT_BOARD if r.model == "claude:claude-opus-5-5")
    assert glmcc.role == "performance", glmcc.role
    assert glmcc.role != opus.role, (glmcc.role, opus.role)


def test_default_pool_roles_are_distinct_no_lens_lost():
    """The default top-4 pool (DEFAULT_POOL_SIZE seats) must have FOUR DISTINCT roles, so a
    plain `review diff` always covers four non-overlapping lenses — no seat wasted on a
    duplicate lens. Coverage is consistency/correctness/performance/quality now
    (review-cli#fable-seat-reliability: Fable's `architect` lens moved to the LAST seat
    along with Fable itself; Kimi's `quality` lens fills the vacated top-4 slot)."""
    pool = [r for r in DEFAULT_BOARD[:DEFAULT_POOL_SIZE]]
    roles = [r.role for r in pool]
    assert len(set(roles)) == len(roles), f"duplicate role in default pool: {roles}"
    assert set(roles) == {"consistency", "correctness", "performance", "quality"}, roles


def test_default_board_has_fifteen_seats():
    # review-cli#382: LUNA_SEAT and SONNET_SEAT added as live fallbacks (10 -> 12);
    # GROK_SEAT (review-cli#165) makes it 13; MIMO_SEAT (review-cli#450) makes it 14;
    # OMP_CLAUDE_SEAT (review-cli#168) makes it 15.
    assert len(DEFAULT_BOARD) == 15, len(DEFAULT_BOARD)


def test_preset_boards_pin_model_order_pool_and_effort():
    # The built-in boards are the capability levels of reviewlib/levels.yaml (routine ==
    # the former `light`, standard == `default`, deep == `heavy`); a bare `review diff` runs
    # the lowest sufficient one, routine. Their full contract is in tests/test_levels.py.
    assert DEFAULT_PRESET == "routine"
    assert all(r.effort == "medium" for r in DEFAULT_PRESET_BOARD)
    assert SOL_SEAT not in _model_ids(DEFAULT_PRESET_BOARD)
    assert [r.model for r in DEFAULT_PRESET_BOARD[:4]] == [
        "claude:claude-opus-5-5",
        "omp:openai-codex/gpt-6-luna",
        "omp:zai/glm-5.3",
        GROK_SEAT,
    ]
    assert len({r.role for r in DEFAULT_PRESET_BOARD[:4]}) == 4

    assert all(r.effort == "low" for r in LIGHT_PRESET_BOARD)
    assert [r.model for r in LIGHT_PRESET_BOARD[:3]] == [
        "omp:anthropic/claude-sonnet-5-5",
        SONNET_SEAT,
        "omp:zai/glm-5.3",
    ]

    # Fable is in NO level (review-cli#fable-seat-reliability): a level must not pay for a
    # seat with a confirmed ~100% dispatch failure rate unless the user adds it.
    for board in (LIGHT_PRESET_BOARD, DEFAULT_PRESET_BOARD, HEAVY_PRESET_BOARD):
        assert "claude:claude-fable-5-1" not in _model_ids(board)
    assert [(r.model, r.effort) for r in HEAVY_PRESET_BOARD[:4]] == [
        ("claude:claude-opus-5-5", "xhigh"),
        (SOL_SEAT, "xhigh"),
        (SONNET_SEAT, "xhigh"),
        (GROK_SEAT, "xhigh"),
    ]
    assert all(r.effort == "xhigh" for r in HEAVY_PRESET_BOARD)

    # Grok and MiMo (the `security` / `tests` lenses) are in every level.
    for board in (LIGHT_PRESET_BOARD, DEFAULT_PRESET_BOARD, HEAVY_PRESET_BOARD):
        assert GROK_SEAT in _model_ids(board)
        assert MIMO_SEAT in _model_ids(board)
    assert LUNA_SEAT in _model_ids(HEAVY_PRESET_BOARD)
    assert SONNET_SEAT in _model_ids(HEAVY_PRESET_BOARD)


def test_package_facade_exports_preset_surface():
    import reviewlib

    assert reviewlib.default_level_name() == DEFAULT_PRESET
    assert reviewlib.SOL_SEAT == SOL_SEAT
    # review-cli#382 (codex review finding, round 3): LUNA_SEAT/SONNET_SEAT must be
    # re-exported through the facade too, like every other seat constant (SOL_SEAT/
    # ASTRA_SEAT/FABLE_SEAT above) -- a caller doing `from reviewlib import LUNA_SEAT`
    # got an ImportError before this was added to reviewlib/__init__.py.
    assert reviewlib.LUNA_SEAT == LUNA_SEAT
    assert reviewlib.SONNET_SEAT == SONNET_SEAT
    assert reviewlib.preset_names() == ("routine", "standard", "deep")
    assert reviewlib.preset_pool_size("light") == 2
    # preset_board(None) resolves via the default level (routine == the former `light`).
    assert [r.model for r in reviewlib.preset_board(None)] == [
        r.model for r in LIGHT_PRESET_BOARD
    ]
    assert [r.model for r in preset_board(None)] == [
        r.model for r in LIGHT_PRESET_BOARD
    ]
    assert preset_board(None)[0] == LIGHT_PRESET_BOARD[0]
    assert preset_board(None)[0] is not LIGHT_PRESET_BOARD[0]
    # Review finding (4 review rounds, multiple reviewers): preset_pool_size(None) must
    # STAY at DEFAULT_POOL_SIZE (4), deliberately NOT tracking the default level the way
    # preset_board(None) does — cli.py's pool guard (`preset_pool_size(active_preset)`)
    # relies on this exact disagreement to give a config `models:`/`board:` roster (no
    # explicit `pool:`) its correct no-override pool of 4, not the routine level's 2. A
    # future "consistency" cleanup that makes these agree would silently re-break that
    # path with the whole suite otherwise green — this assertion is the guard against it.
    assert reviewlib.preset_pool_size(None) == DEFAULT_POOL_SIZE == 4


def test_explicit_preset_board_overrides_config_board():
    cfg = {"board": [{"model": "gemini", "role": "tests", "effort": "medium"}]}
    board = load_board(cfg, preset="heavy")
    assert [r.model for r in board[:4]] == [
        "claude:claude-opus-5-5",
        SOL_SEAT,
        SONNET_SEAT,
        GROK_SEAT,
    ]
    assert all(r.effort == "xhigh" for r in board[:4])
    assert all(r.effort == "xhigh" for r in board[4:])
    assert "claude:claude-fable-5-1" not in [r.model for r in board]


def test_explicit_models_with_preset_preserve_config_metadata_and_use_preset_effort():
    cfg = {
        "board": [
            {
                "model": "gemini",
                "role": "tests",
                "name": "Configured Gemini",
                "effort": "medium",
            },
        ]
    }
    board = board_from_models(["gemini"], cfg, preset="heavy")

    assert len(board) == 1
    assert board[0].model == "gemini"
    assert board[0].role == "tests"
    assert board[0].display == "Configured Gemini"
    assert board[0].effort == "xhigh"


def test_explicit_models_with_preset_apply_preset_effort_to_out_of_preset_seat():
    board = board_from_models(
        ["claude:claude-fable-5-1", "custom:model"], {}, preset="light"
    )

    assert [r.model for r in board] == ["claude:claude-fable-5-1", "custom:model"]
    assert board[0].role == "architect"
    assert board[0].effort == "low"
    assert board[1].role == ""
    assert board[1].effort == "low"


def test_bare_codex_explicit_model_no_longer_matches_board_metadata_after_astra_pin():
    """Opus review finding: before ASTRA_SEAT replaced the bare `"codex"` DEFAULT_BOARD
    seat, `-m codex` matched that seat byte-for-byte and inherited its role/display/preset
    effort. Now that the seat is pinned to `codex:gpt-6-astra`, a bare `-m codex` matches NO
    board/preset entry, so it falls back to the SAME generic unmatched-model path any
    unrecognised model already gets (role="", a display derived from the model string, and
    `board_from_models`'s `preset_default_effort` -- the first NON-None `effort` among the
    preset's own seats, in priority order).

    Deliberately uses `preset="heavy"` (NOT the `preset="light"` that
    `test_explicit_models_with_preset_apply_preset_effort_to_out_of_preset_seat` uses),
    because "heavy" grades effort per seat (xhigh for the top 4, max after) while "light"
    gives every seat a uniform "medium" -- so this test's expected "xhigh" (heavy's own
    first-seat effort, Sol's) and that other test's expected "medium" (light's uniform
    effort) are BOTH correct outputs of the identical fallback code path, just fed a
    different preset; they are not in tension. This pins that the metadata loss for a bare
    `-m codex` is a deliberate, non-crashing behavior change, not a regression."""
    board = board_from_models(["codex"], {}, preset="heavy")

    assert len(board) == 1
    assert board[0].model == "codex"
    assert board[0].role == ""
    assert board[0].display == "codex"
    # Preset default effort (the FIRST seat's effort in priority order, i.e. Sol's xhigh)
    # -- NOT the "max" effort the old DEFAULT_BOARD seat #5 used to carry.
    assert board[0].effort == "xhigh"


def test_explicit_models_with_preset_prevent_config_effort_downgrade_for_out_of_preset_seat():
    cfg = {
        "board": [
            {
                "model": "custom:model",
                "role": "tests",
                "name": "Custom",
                "effort": "low",
            },
        ]
    }
    board = board_from_models(["custom:model"], cfg, preset="heavy")

    assert len(board) == 1
    assert board[0].model == "custom:model"
    assert board[0].role == "tests"
    assert board[0].display == "Custom"
    assert board[0].effort == "xhigh"


def test_config_board_unsupported_effort_is_ignored():
    board = load_board(
        {"board": [{"model": "codex", "role": "tests", "effort": 'bad"value'}]}
    )
    assert board[0].effort is None


def test_unknown_preset_name_fails_fast_for_api_callers():
    try:
        load_board({}, preset="bogus")
    except ValueError as exc:
        assert "unknown level" in str(exc)
    else:
        raise AssertionError("unknown preset should not fall back to DEFAULT_BOARD")

    from reviewlib.config import preset_pool_size

    try:
        preset_pool_size("bogus")
    except ValueError as exc:
        assert "unknown level" in str(exc)
    else:
        raise AssertionError("unknown preset should not fall back to DEFAULT_POOL_SIZE")


def test_call_backend_does_not_break_legacy_custom_backend_without_effort():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _legacy,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="high",
    )
    assert result.returncode == 0
    assert result.command == "legacy"
    assert "Use high reasoning effort." in result.stdout


def test_call_backend_does_not_pass_effort_when_signature_uninspectable():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    old_signature = backends.inspect.signature
    backends.inspect.signature = lambda _backend: (_ for _ in ()).throw(
        ValueError("no signature")
    )
    try:
        result = backends.call_backend(
            _legacy,
            "custom",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="high",
        )
    finally:
        backends.inspect.signature = old_signature
    assert result.returncode == 0
    assert result.command == "legacy"


def test_call_backend_does_not_pass_effort_as_keyword_to_positional_only_param():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0, effort=None, /):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _legacy,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="high",
    )
    assert result.returncode == 0
    assert result.command == "legacy"


def test_call_backend_passes_effort_to_var_keyword_backend_once():
    captured: dict = {}

    def _backend(model, prompt, diff, cwd, timeout, round_no=0, **kwargs):
        captured["effort"] = kwargs.get("effort")
        return ReviewResult(
            model=model, command="kwargs", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _backend,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="xhigh",
    )

    assert result.returncode == 0
    assert result.command == "kwargs"
    assert captured["effort"] == "xhigh"
    assert result.stdout.count("Use highest reasoning effort.") == 1


def test_codex_backend_threads_effort_to_cli_config():
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
        captured["header_argv0"] = kw.get("header_argv0")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            SOL_SEAT,
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="xhigh",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert "-c" in captured["argv"], captured
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured
    assert captured["header_argv0"] == "codex -m gpt-6.1-sol", captured


def test_codex_backend_sanitizes_model_selector_in_log_header():
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
        captured["header_argv0"] = kw.get("header_argv0")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex:gpt-6.1-sol\n\u0085\u2028\u2029[review-cli] forged: codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    for ch in ("\n", "\u0085", "\u2028", "\u2029"):
        assert ch not in captured["header_argv0"], captured
    assert (
        captured["header_argv0"] == "codex -m gpt-6.1-sol????[review-cli] forged: codex"
    ), captured
    assert any("\n" in arg for arg in captured["argv"]), captured
