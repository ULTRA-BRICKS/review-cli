"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    BoardConfigError,
    BoardReviewer,
    DEFAULT_BOARD,
    DEFAULT_PROMPT,
    LIGHT_PRESET_BOARD,
    Path,
    REPO_ROOT,
    REVIEW_ROLES,
    ReviewResult,
    SOL_SEAT,
    _AvailabilityPatch,
    _review_mod,
    backends,
    build_board_jobs,
    lens_override_text,
    load_board,
    os,
    sys,
)

def test_every_default_role_has_a_lens():
    for reviewer in DEFAULT_BOARD:
        assert reviewer.role in REVIEW_ROLES, reviewer.role
        assert reviewer.role_lens.strip(), reviewer.role


def test_roles_are_non_overlapping_focus_sentences():
    # Each lens names its own focus word — a cheap guard that they aren't identical.
    focuses = {role: lens.split(":", 1)[0] for role, lens in REVIEW_ROLES.items()}
    assert len(set(focuses.values())) == len(REVIEW_ROLES), focuses


def test_resolved_lens_with_no_cwd_matches_role_lens_byte_for_byte():
    """No `cwd` (the pre-#547 call shape, e.g. a diff-only just-ask seat) must resolve
    BYTE-IDENTICAL to the old `role_lens` -- absence of repo context is absence of
    override, never a crash."""
    for role in REVIEW_ROLES:
        reviewer = BoardReviewer("codex", role, "Codex")
        assert reviewer.resolved_lens() == reviewer.role_lens == REVIEW_ROLES[role]
        assert reviewer.resolved_lens(None) == reviewer.role_lens


def test_resolved_lens_with_no_override_file_matches_role_lens_byte_for_byte():
    """A repo with NO `.review/lenses/` at all must behave exactly as before this
    feature -- acceptance criterion 2 of review-cli#547."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        for role in REVIEW_ROLES:
            reviewer = BoardReviewer("codex", role, "Codex")
            assert reviewer.resolved_lens(cwd) == reviewer.role_lens
        assert lens_override_text("security", cwd) == ""


def test_resolved_lens_extends_not_replaces_the_builtin_lens():
    """An override file's text is APPENDED after the built-in lens (never silently
    replacing it) -- acceptance criterion 1 of review-cli#547, extension over
    replacement is the deliberate, documented safer default (see
    `lens_override_text`'s docstring)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        (lenses / "security.md").write_text(
            "Also check: our payments ledger must never go negative.\n"
        )
        reviewer = BoardReviewer("codex", "security", "Codex")
        resolved = reviewer.resolved_lens(cwd)
        assert resolved.startswith(REVIEW_ROLES["security"])
        assert "payments ledger must never go negative" in resolved
        assert resolved != REVIEW_ROLES["security"]
        # A DIFFERENT role's board seat in the SAME repo is untouched.
        other = BoardReviewer("codex", "correctness", "Codex")
        assert other.resolved_lens(cwd) == REVIEW_ROLES["correctness"]


def test_lens_override_blank_file_counts_as_no_override():
    """An empty or whitespace-only override file is treated as ABSENT (the safer
    default, documented on `lens_override_text`) -- the seat keeps the full built-in
    lens rather than silently reviewing with a truncated/empty one."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        for blank in ("", "   ", "\n\n  \n"):
            (lenses / "correctness.md").write_text(blank)
            assert lens_override_text("correctness", cwd) == ""
            reviewer = BoardReviewer("codex", "correctness", "Codex")
            assert reviewer.resolved_lens(cwd) == REVIEW_ROLES["correctness"]


def test_lens_override_unknown_role_and_missing_cwd_never_crash():
    """`lens_override_text` never raises: an unknown/typo'd role, a `cwd=None`, and a
    path that exists but is a DIRECTORY (not a file, so `.read_text()` raises
    `IsADirectoryError`, an `OSError` subclass) all collapse to "" rather than
    propagating -- a malformed override must never break a review."""
    import tempfile

    assert lens_override_text("not-a-real-role", None) == ""
    assert lens_override_text("", None) == ""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        assert lens_override_text("security", cwd) == ""  # no .review/ at all
        # A directory sitting where the override FILE should be (a plausible mistake --
        # `mkdir security.md` instead of writing a file) must not crash either.
        bogus = cwd / ".review" / "lenses" / "security.md"
        bogus.mkdir(parents=True)
        assert lens_override_text("security", cwd) == ""


def test_lens_override_refuses_a_leaf_symlink_escaping_the_repo():
    """Security review finding (review-cli#547): a git-tracked symlink AT the override
    path pointing OUTSIDE the reviewed repo (e.g. `.review/lenses/security.md ->
    ~/.ssh/id_rsa`) must never be followed -- `Path.read_text()` follows symlinks
    unconditionally, which would have review-cli's own process read an arbitrary host
    file and ship its bytes to every matching seat's prompt, including diff-only HTTP
    backends that otherwise have NO filesystem access at all."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as outside,
        tempfile.TemporaryDirectory() as repo,
    ):
        secret = Path(outside) / "secret.txt"
        secret.write_text("SUPER_SECRET_API_KEY=xyz\n")
        cwd = Path(repo)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        (lenses / "security.md").symlink_to(secret)
        assert lens_override_text("security", cwd) == ""
        reviewer = BoardReviewer("codex", "security", "Codex")
        assert reviewer.resolved_lens(cwd) == REVIEW_ROLES["security"]


def test_lens_override_refuses_an_intermediate_directory_symlink_escaping_the_repo():
    """Same exfiltration class as the leaf-symlink case, one level up: `.review/lenses`
    (or `.review` itself) checked in as a symlink to an OUTSIDE directory must not let
    an override "inside" that directory be read either -- `os.open(..., O_NOFOLLOW)`
    alone only refuses a symlinked LEAF, so this is covered by the separate
    `Path.resolve() -> is_relative_to(cwd)` containment check."""
    import tempfile

    with (
        tempfile.TemporaryDirectory() as outside,
        tempfile.TemporaryDirectory() as repo,
    ):
        (Path(outside) / "security.md").write_text("Also check: leaked secret.\n")
        cwd = Path(repo)
        review_dir = cwd / ".review"
        review_dir.mkdir()
        (review_dir / "lenses").symlink_to(outside, target_is_directory=True)
        assert lens_override_text("security", cwd) == ""


def test_lens_override_rejects_a_fifo_without_hanging():
    """A FIFO planted at the override path (not a symlink, so `O_NOFOLLOW` alone
    wouldn't stop it) must be rejected via `O_NONBLOCK` + `S_ISREG` rather than hang a
    blocking open forever -- the same class of bug `jobs.py`'s
    `_read_fallback_pointer` already guards against for a comparable predictable
    path. This test itself would hang were the guard missing, so its own PASS is the
    proof."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        os.mkfifo(lenses / "security.md")
        assert lens_override_text("security", cwd) == ""


def test_lens_override_caps_read_size_instead_of_loading_the_whole_file():
    """A pathologically large override file must never be loaded whole (`MemoryError`
    is not an `OSError`, so an uncapped `read_text()` would violate this function's own
    never-crash contract) -- content beyond `LENS_OVERRIDE_MAX_BYTES` is silently
    truncated, never rejected outright (a project override is a short addendum, not a
    payload-sized document)."""
    import tempfile

    from reviewlib.config import LENS_OVERRIDE_MAX_BYTES

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        (lenses / "security.md").write_text("A" * (LENS_OVERRIDE_MAX_BYTES * 3))
        result = lens_override_text("security", cwd)
        assert 0 < len(result) <= LENS_OVERRIDE_MAX_BYTES, len(result)


def test_build_board_job_prompt_carries_project_lens_override():
    """`panel.build_board_job` is the function `panel.py` actually calls to assemble a
    dispatched seat's prompt (review-cli#547's real integration point) -- prove the
    override reaches the FINAL prompt, not just the config-layer helper."""
    import tempfile

    from reviewlib.panel import build_board_job

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        (lenses / "tests.md").write_text(
            "Also check: golden-file fixtures are updated."
        )
        reviewer = BoardReviewer("codex", "tests", "Codex")
        job_with_override = build_board_job(reviewer, DEFAULT_PROMPT, "+x", cwd=cwd)
        job_without = build_board_job(reviewer, DEFAULT_PROMPT, "+x")
        assert "golden-file fixtures are updated" in job_with_override.prompt
        assert "golden-file fixtures are updated" not in job_without.prompt
        assert job_without.prompt == f"{DEFAULT_PROMPT}\n\n{REVIEW_ROLES['tests']}"


def test_no_board_key_falls_back_to_default():
    assert [r.model for r in load_board({})] == [r.model for r in LIGHT_PRESET_BOARD]
    assert [r.model for r in load_board({"models": ["codex"]})] == [
        r.model for r in LIGHT_PRESET_BOARD
    ]


def test_empty_or_wrong_typed_board_falls_back_to_default():
    for bad in ([], {"board": []}, {"board": "codex"}, {"board": 42}):
        cfg = bad if isinstance(bad, dict) else {"board": bad}
        assert len(load_board(cfg)) == len(LIGHT_PRESET_BOARD), cfg


def test_board_config_overrides_default():
    cfg = {
        "board": [
            {"model": "codex", "role": "correctness"},
            {"model": "gemini", "role": "security", "name": "G"},
        ]
    }
    board = load_board(cfg)
    assert len(board) == 2
    assert board[0].model == "codex" and board[0].role == "correctness"
    # Explicit name honored; default name derived from the model tail otherwise.
    assert board[1].display == "G"
    assert board[0].display == "codex"


def test_board_alias_in_model_is_expanded():
    # `glm46` is an alias for `zai:glm-4.6` (config._expand_alias).
    board = load_board({"board": [{"model": "glm46", "role": "tests"}]})
    assert board[0].model == "zai:glm-4.6", board[0].model


def test_unknown_role_fails_fast_with_clear_error():
    """review-cli#422: an unrecognized `role:` string used to be silently kept with the
    generic prompt (a stderr warning easy to miss) -- on a mixed board with more than one
    typo'd role, every such seat collapsed onto the SAME generic prompt, so "N roles
    reviewed" quietly became "N duplicate generic passes". Board composition now rejects
    it outright, naming both the bad value and the valid REVIEW_ROLES enum."""
    try:
        load_board({"board": [{"model": "codex", "role": "made-up-role"}]})
    except BoardConfigError as exc:
        message = str(exc)
        assert "made-up-role" in message, message
        for known_role in REVIEW_ROLES:
            assert known_role in message, message
    else:
        raise AssertionError("unknown role should raise BoardConfigError, not degrade")


def test_non_string_role_fails_fast_same_as_unknown_string():
    """review-cli#422: a non-string, non-omitted `role:` (a YAML type-confusion typo --
    e.g. an unquoted `role: true` parsed as a bool, or `role: 123`) is the SAME class of
    config bug as an unrecognized string. It must raise BoardConfigError too, not silently
    coerce to the empty/general role and degrade with no warning."""
    for bad_role in (True, 123, ["security"], {"k": "v"}):
        try:
            load_board({"board": [{"model": "codex", "role": bad_role}]})
        except BoardConfigError as exc:
            assert repr(bad_role) in str(exc), (bad_role, exc)
        else:
            raise AssertionError(
                f"non-string role {bad_role!r} should raise BoardConfigError, not degrade"
            )


def test_entry_with_unusable_model_skips_before_role_is_checked():
    """review-cli#422 precedence: `_parse_board_entry` resolves model/capability FIRST --
    an entry with no usable `model:`/`capability:` is skipped (with a warning) before its
    `role:` is ever inspected, even when that role is ALSO an unrecognized string. Only an
    entry that DOES resolve to a real seat has its role validated; a board with one other
    valid entry proves the bad-model+bad-role entry was silently skipped, not fatal."""
    cfg = {
        "board": [
            {"model": "   ", "role": "not-a-real-role"},  # blank model: skipped first
            {"model": "codex", "role": "correctness"},  # the only valid entry
        ]
    }
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex"], [r.model for r in board]


def test_mixed_board_with_one_bad_role_fails_fast_not_silently_duplicated():
    """review-cli#422 regression: a board with several VALID distinct-role seats plus ONE
    typo'd role must fail the whole board load with a clear error naming the bad seat --
    not silently generate a duplicate generic prompt for just that one seat while the rest
    of the board dispatches normally."""
    cfg = {
        "board": [
            {"model": "codex", "role": "correctness"},
            {"model": "gemini", "role": "securty"},  # typo: should be "security"
            {"model": "claude:claude-opus-5-5", "role": "performance"},
        ]
    }
    try:
        load_board(cfg)
    except BoardConfigError as exc:
        message = str(exc)
        assert "gemini" in message, message
        assert "securty" in message, message
        # "security" is one of the alphabetically-listed valid roles in the message
        # (the error lists the whole enum, it does not guess a typo-correction).
        assert "security" in message, message
    else:
        raise AssertionError(
            "a mixed board with one bad role must fail fast, not silently keep the "
            "typo'd seat on a duplicate generic prompt"
        )


def test_bad_entries_are_skipped_not_crashed():
    cfg = {
        "board": [
            "not-a-mapping",
            {"role": "correctness"},  # missing model
            {"model": "   "},  # blank model
            {"model": "codex", "role": "correctness"},  # the only valid one
        ]
    }
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex"], [r.model for r in board]


def test_role_omitted_is_general_with_generic_prompt():
    board = load_board({"board": [{"model": "codex"}]})
    assert board[0].role == ""
    assert board[0].role_lens == ""


def test_present_board_all_malformed_raises_not_silent_default():
    """A non-empty `board:` whose entries are ALL malformed must ERROR, NOT
    silently run a built-in board."""
    cfgs = [
        {"board": ["not-a-mapping", "still-not"]},  # no mappings at all
        {"board": [{"role": "correctness"}, {"role": "security"}]},  # all missing model
        {"board": [{"model": "   "}, {"model": ""}]},  # all blank models
        {"board": [{"model": 42}, "x", {"no": "model"}]},  # mixed garbage, none usable
    ]
    for cfg in cfgs:
        try:
            load_board(cfg)
        except BoardConfigError:
            pass  # expected
        else:
            raise AssertionError(f"expected BoardConfigError for {cfg!r}")


def test_absent_board_key_uses_default_not_error():
    """Absent `board:` -> the default level's board (no error): no preference was expressed."""
    assert [r.model for r in load_board({})] == [r.model for r in LIGHT_PRESET_BOARD]
    # An explicitly empty list is "no preference" too -> default, not an error.
    assert [r.model for r in load_board({"board": []})] == [
        r.model for r in LIGHT_PRESET_BOARD
    ]


def test_partial_malformed_board_keeps_valid_entries_no_error():
    """SOME valid + SOME malformed -> keep the valid ones, warn on bad (no error)."""
    cfg = {
        "board": [
            "not-a-mapping",
            {"role": "correctness"},  # missing model -> skipped
            {"model": "codex", "role": "correctness"},  # valid
            {"model": "gemini", "role": "security"},  # valid
        ]
    }
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex", "gemini"], [r.model for r in board]


def test_build_board_jobs_injects_lens_label_and_carries_effort_metadata():
    board = [BoardReviewer("codex", "performance", "Codex", "high")]
    with _AvailabilityPatch({"codex"}):
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert skipped == []
    assert len(jobs) == 1
    job = jobs[0]
    assert job.model == "codex"
    assert job.diff == "+x"
    assert job.label == "Codex [performance]"
    assert job.effort == "high"
    # The lens is appended to the base prompt; effort is dispatched at backend-call time.
    assert job.prompt.startswith(DEFAULT_PROMPT + "\n\n")
    assert "Use high reasoning effort." not in job.prompt
    assert REVIEW_ROLES["performance"] in job.prompt


def test_panel_job_keeps_round_no_as_sixth_positional_argument():
    from reviewlib.panel import PanelJob

    job = PanelJob("codex", "prompt", "+x", "Codex", (), 3)
    assert job.round_no == 3
    assert job.effort is None

    effort_job = PanelJob("codex", "prompt", "+x", "Codex", (), 3, "high")
    assert effort_job.round_no == 3
    assert effort_job.effort == "high"


def test_run_panel_codex_uses_native_effort_and_text_prompt_hint():
    from reviewlib import backends
    from reviewlib.panel import build_board_job, run_panel

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
        captured["input_text"] = kw["input_text"]
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        job = build_board_job(
            BoardReviewer(SOL_SEAT, "consistency", "Sol", "xhigh"), DEFAULT_PROMPT, "+x"
        )
        results = run_panel([job], REPO_ROOT, 5)
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert results[0].returncode == 0
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured
    assert "Use highest reasoning effort." in captured["input_text"], captured


def test_build_board_jobs_unknown_role_uses_generic_prompt():
    board = [BoardReviewer("codex", "", "Codex")]
    with _AvailabilityPatch({"codex"}):
        jobs, _ = build_board_jobs(board, DEFAULT_PROMPT, "")
    assert jobs[0].prompt == DEFAULT_PROMPT  # no lens appended
    assert jobs[0].label == "Codex [general]"


def test_unavailable_reviewers_are_skipped_not_crashed():
    board = list(DEFAULT_BOARD)
    reachable = {"gemini", "omp:zai/glm-5.3"}  # only two reachable (GLM is now agentic)
    with _AvailabilityPatch(reachable):
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert {j.model for j in jobs} == reachable
    assert {r.model for r in skipped} == {
        r.model for r in board if r.model not in reachable
    }
    assert len(jobs) + len(skipped) == len(board)


def test_all_unavailable_board_returns_no_jobs():
    with _AvailabilityPatch(set()):
        jobs, skipped = build_board_jobs(list(DEFAULT_BOARD), DEFAULT_PROMPT, "+x")
    assert jobs == []
    assert len(skipped) == len(DEFAULT_BOARD)


def test_mode_review_board_runs_and_succeeds():
    from reviewlib.modes import review as review_mod

    calls: list[tuple[str, str]] = []

    # round_no is the 6th positional arg run_panel passes to every backend (HYP-742
    # dashboard threading); a board reviewer fake must accept it or run_panel turns the
    # TypeError into an internal 127.
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append((model, prompt))
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    old_resolve = review_mod.resolve_backend
    review_mod.resolve_backend = lambda _m: _fake_backend
    # also patch the one build_board_jobs uses (reviewlib.panel.resolve_backend)
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex", "gemini"}):
            rc = review_mod.mode_review(
                [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board
            )
    finally:
        review_mod.resolve_backend = old_resolve
        panel.resolve_backend = old_panel_resolve
    assert rc == 0, rc
    # Each reviewer got a role-lensed prompt.
    prompts = {model: prompt for model, prompt in calls}
    assert REVIEW_ROLES["correctness"] in prompts["codex"]
    assert REVIEW_ROLES["consistency"] in prompts["gemini"]


def test_mode_review_board_reuses_the_healthy_seat_and_fails_only_when_none_is_left():
    """review-cli#588: on a (non-exact) board, a failed seat's role is re-run on the seat
    that already answered, so the run succeeds; with no healthy seat at all it fails."""
    from reviewlib.modes import review as review_mod

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    import reviewlib.panel as panel

    def _run(healthy: set[str]) -> int:
        def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
            rc = 0 if model in healthy else 1
            return ReviewResult(
                model=model, command="fake", returncode=rc, stdout="x", stderr="boom"
            )

        old_panel_resolve = panel.resolve_backend
        panel.resolve_backend = lambda _m: _fake_backend
        try:
            with _AvailabilityPatch({"codex", "gemini"}):
                return review_mod.mode_review(
                    [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board
                )
        finally:
            panel.resolve_backend = old_panel_resolve

    assert _run({"codex"}) == 0
    assert _run(set()) == 1


def test_mode_review_board_backfills_preflight_skip_from_reserve():
    from reviewlib.modes import review as review_mod

    calls: list[str] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append(model)
        if model == "commandcode:deepseek/deepseek-v4-pro":
            return ReviewResult(
                model=model,
                command="commandcode API deepseek/deepseek-v4-pro",
                returncode=1,
                stdout="",
                stderr="provider 'commandcode' failed payment/availability preflight (HTTP 402); skipping",
            )
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("commandcode:deepseek/deepseek-v4-pro", "tests", "CommandCode"),
        BoardReviewer("codex", "correctness", "Codex"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"commandcode:deepseek/deepseek-v4-pro", "codex"}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
            )
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 0, rc
    assert calls == ["commandcode:deepseek/deepseek-v4-pro", "codex"], calls


def test_mode_review_failover_threads_effort_to_pool_and_reserve():
    from reviewlib.modes import review as review_mod

    calls: list[tuple[str, str | None, str]] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append((model, effort, prompt))
        if model == "claude:claude-fable-5-1":
            return ReviewResult(
                model=model,
                command="fake",
                returncode=1,
                stdout="",
                stderr="authentication failed",
            )
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("claude:claude-fable-5-1", "architect", "Fable", effort="xhigh"),
        BoardReviewer(SOL_SEAT, "consistency", "Sol", effort="medium"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"claude:claude-fable-5-1", SOL_SEAT}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
            )
    finally:
        panel.resolve_backend = old_panel_resolve

    assert rc == 0, rc
    assert [model for model, _effort, _prompt in calls] == [
        "claude:claude-fable-5-1",
        SOL_SEAT,
    ]
    assert [effort for _model, effort, _prompt in calls] == ["xhigh", "medium"]
    assert "Use highest reasoning effort." in calls[0][2]
    assert "Use medium reasoning effort." in calls[1][2]


def test_mode_review_board_with_no_available_reviewers_returns_1():
    from reviewlib.modes import review as review_mod

    with _AvailabilityPatch(set()):
        rc = review_mod.mode_review(
            [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=list(DEFAULT_BOARD)
        )
    assert rc == 1, rc


def test_mode_review_exact_board_runs_unavailable_explicit_seat_and_fails():
    """Explicit -m with board metadata is exact: unavailable requested seats must not vanish."""
    from reviewlib.modes import review as review_mod

    calls: list[str] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append(model)
        rc = 0 if model == "codex" else 1
        return ReviewResult(
            model=model,
            command="fake",
            returncode=rc,
            stdout="ok" if rc == 0 else "",
            stderr="missing",
        )

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("missing-provider:model", "tests", "Missing"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex"}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=len(board),
                exact_board=True,
            )
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 1, rc
    assert set(calls) == {"codex", "missing-provider:model"}, calls


def test_cli_explicit_models_disable_board():
    """An explicit one-model `-m` WITH `--task` pads that model across ship-floor
    roles (review-cli#309) rather than a flat exact 1-seat panel."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["exact_board"] = kw.get("exact_board")
        captured["pool_size"] = kw.get("pool_size")
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    # Avoid touching a real config file / git diff: feed the diff via stdin and
    # point the env file at nothing so no provider key resolves.
    old_stdin = sys.stdin
    # ROUTING test, not a pool-assembly test. The piped diff is non-empty, so on a
    # backend-less host the review-mode pool guard would bail (exit 10) before the
    # stubbed handler runs; force every seat live via the fake backend seam.
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "-m", "codex", "-C", str(REPO_ROOT)])
        assert captured["board"] is not None, captured
        assert {r.model for r in captured["board"]} == {"codex"}, captured
        assert len({r.role for r in captured["board"] if r.role}) >= 3, captured
        assert captured["exact_board"] is not True, captured
        assert captured["models"] == ["codex"], captured["models"]
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        sys.stdin = old_stdin
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake


def test_cli_explicit_models_override_config_models_and_board():
    """-m narrows configured board/model preferences to the requested models only."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["commandcode:deepseek/deepseek-v4-pro", "codex"],
        "board": [
            {
                "model": "commandcode:deepseek/deepseek-v4-pro",
                "role": "tests",
                "name": "DeepSeek-cc",
            },
            {"model": "codex", "role": "consistency", "name": "Codex"},
            {"model": "claude:claude-fable-5-1", "role": "architect", "name": "Fable"},
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "-m", "fable51", "-C", str(REPO_ROOT)])
        assert captured["board"] is not None, captured
        assert {r.model for r in captured["board"]} == {"claude:claude-fable-5-1"}, (
            captured
        )
        first = captured["board"][0]
        assert (first.model, first.role, first.display) == (
            "claude:claude-fable-5-1",
            "architect",
            "Fable",
        ), captured
        assert len({r.role for r in captured["board"] if r.role}) >= 3, captured
        assert captured["models"] == ["claude:claude-fable-5-1"], captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
