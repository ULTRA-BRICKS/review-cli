"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    DEFAULT_POOL_SIZE,
    DEFAULT_PRESET_BOARD,
    GROK_SEAT,
    LIGHT_PRESET_BOARD,
    REPO_ROOT,
    SOL_SEAT,
    SONNET_SEAT,
    _capture_default_review_board,
    _capture_review_with_config,
    _review_mod,
    _show_board_lines,
    backends,
    os,
    sys,
)

def test_cli_explicit_models_are_not_sliced_by_pool():
    """When -m is explicit, --pool must not drop requested models from the review panel."""
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
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex", "gemini", "claude:claude-fable-5-1"],
        "board": [
            {"model": "codex", "role": "correctness", "name": "Codex"},
            {"model": "gemini", "role": "tests", "name": "Gemini"},
            {"model": "claude:claude-fable-5-1", "role": "architect", "name": "Fable"},
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "-m",
                "codex",
                "-m",
                "fable51",
                "--pool",
                "1",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert [r.model for r in captured["board"]] == [
            "codex",
            "claude:claude-fable-5-1",
        ], captured
        assert captured["pool_size"] == 2, captured
        assert captured["exact_board"] is True, captured
        assert captured["models"] == ["codex", "claude:claude-fable-5-1"], captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_models_override_preset_and_config_models():
    """-m is exact: neither --preset nor config models may append or reorder seats."""
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
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex", "gemini"],
        "board": [
            {"model": "claude:claude-opus-5-5", "role": "tests", "effort": "low"}
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--preset",
                "heavy",
                "-m",
                "gemini",
                "-m",
                "claude:claude-opus-5-5",
                "--pool",
                "1",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert captured["models"] == ["gemini", "claude:claude-opus-5-5"], captured
        assert [r.model for r in captured["board"]] == [
            "gemini",
            "claude:claude-opus-5-5",
        ], captured
        assert [r.effort for r in captured["board"]] == ["xhigh", "xhigh"], captured
        assert captured["pool_size"] == 2, captured
        assert captured["exact_board"] is True, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_model_not_in_config_board_is_still_requested():
    """Configured board metadata must not drop an explicit model that lacks a board entry."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex"],
        "board": [{"model": "codex", "role": "correctness", "name": "Codex"}],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "-m", "gemini", "-C", str(REPO_ROOT)])
        assert captured["models"] == ["gemini"], captured
        assert {r.model for r in captured["board"]} == {"gemini"}, captured
        assert len({r.role for r in captured["board"] if r.role}) >= 3, captured
        assert captured["exact_board"] is not True, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_models_ignore_malformed_config_board():
    """Explicit -m is an override: a broken config board must not abort the requested run."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {"board": [{"role": "tests"}]}
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["diff", "--task", "TEST-1", "-m", "codex", "-C", str(REPO_ROOT)])
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    assert rc == 0
    assert captured["models"] == ["codex"], captured
    assert {r.model for r in captured["board"]} == {"codex"}, captured
    assert captured["exact_board"] is not True, captured


def test_cli_leading_model_option_before_subcommand_is_honored():
    """Leading -m keeps the same precedence and narrows config models after normalization."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["cwd"] = cwd
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["commandcode:deepseek/deepseek-v4-pro", "codex"]
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["-m", "fable51", "-C", str(REPO_ROOT), "diff", "--task", "TEST-1"])
        assert captured["board"] is not None, captured
        assert {r.model for r in captured["board"]} == {"claude:claude-fable-5-1"}, (
            captured
        )
        assert captured["models"] == ["claude:claude-fable-5-1"], captured
        assert captured["cwd"] == REPO_ROOT, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_leading_model_option_does_not_reorder_management_subcommands():
    """Management subcommands are not review modes, so leading -m remains an argparse error."""
    from reviewlib import cli

    argv = ["-m", "fable5", "dashboard"]
    assert cli._normalize_leading_mode_options(argv) == argv


def test_cli_leading_mode_options_do_not_treat_long_flags_as_short_m():
    """Unknown long options such as --markdown must not be mistaken for glued -m values."""
    from reviewlib import cli

    argv = ["--markdown", "diff"]
    assert cli._normalize_leading_mode_options(argv) == argv


def test_cli_no_board_flag_is_gone():
    """The board can NEVER be disabled: --no-board was removed, so argparse must
    reject it (SystemExit) rather than silently accepting + disabling the board."""
    from reviewlib import cli

    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        raised = False
        try:
            cli.main(["--no-board", "-C", str(REPO_ROOT)])
        except SystemExit:
            raised = True  # argparse rejects the unknown flag
        assert raised, "--no-board must be an unknown flag now (removed)"
    finally:
        sys.stdin = old_stdin


def test_cli_config_models_form_priority_board():
    """A config `models:` list is the priority roster for the diff-review board.

    It must NOT take the legacy flat path: `review diff` still gets a board, `--pool`
    still sizes the active pool, and the remaining configured models are reserve seats.
    Explicit CLI `-m` narrows this configured board to the requested models."""
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
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "models": ["codex", "gemini", "commandcode:deepseek/deepseek-v4-pro"]
    }
    # Force every seat live so the pre-dispatch pool-selection guard (reviewlib.pool_guard)
    # doesn't bail on a host without these backends' keys/CLIs — this test checks BOARD
    # ROUTING, not liveness.
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)])
        assert captured["board"] is not None, captured
        assert [r.model for r in captured["board"]] == [
            "codex",
            "gemini",
            "commandcode:deepseek/deepseek-v4-pro",
        ], captured["board"]
        assert captured["pool_size"] == 2, captured["pool_size"]
        # The flat model list is still passed for compatibility, but the board path wins.
        assert captured["models"] == [
            "codex",
            "gemini",
            "commandcode:deepseek/deepseek-v4-pro",
        ], captured["models"]
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_empty_config_models_does_not_disable_board():
    """An "effectively empty" `models:` — absent, [], or only blank/whitespace
    entries — is NOT a real preference, so it must NOT disable the board NOR feed
    blank model names to the panel (codex P2). The board still runs in every case."""
    from reviewlib import cli

    for empty_models in ([], ["", "  ", "\t"]):
        captured: dict = {}

        def _fake_mode_review(
            models, prompt, diff, cwd, timeout, staged, board=None, **kw
        ):
            captured["board"] = board
            captured["models"] = models
            return 0

        old = _review_mod.mode_review
        _review_mod.mode_review = _fake_mode_review
        old_load_config = cli.load_config
        cli.load_config = lambda em=empty_models: {"models": em}
        old_load_board = cli.load_board
        cli.load_board = lambda _cfg, **_kw: list(DEFAULT_PRESET_BOARD)
        # Force liveness: this checks the board is not DISABLED by an empty models list,
        # not backend availability — keep the pool-selection guard from bailing on a host
        # lacking these backends.
        old_avail = backends.backend_available
        backends.backend_available = lambda _m: True
        old_stdin = sys.stdin
        try:
            os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
            import io

            sys.stdin = io.StringIO("+added line\n")
            cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
            assert captured["board"] is not None, (
                f"{empty_models!r} must not disable the board"
            )
            # And no blank model name leaked into the (unused) flat models list.
            assert all(m.strip() for m in captured["models"]), captured["models"]
        finally:
            _review_mod.mode_review = old
            cli.load_config = old_load_config
            cli.load_board = old_load_board
            backends.backend_available = old_avail
            sys.stdin = old_stdin


def test_cli_all_malformed_board_errors_nonzero():
    """A present-but-all-malformed `board:` makes the CLI exit non-zero with a
    message, not silently run the paid default board."""
    from reviewlib import cli

    called = {"mode_review": False}

    def _fake_mode_review(*_a, **_k):
        called["mode_review"] = True
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping", {"role": "x"}]}
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
        assert rc != 0, rc
        assert called["mode_review"] is False, "must not run the panel on a bad board"
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_standalone_visual_ignores_malformed_board():
    """(codex P2) Standalone `review visual image` (no diff) does NOT use the reviewer
    board, so a present-but-malformed `board:` must NOT block it — board validation runs
    only on the board path, after the standalone pipeline has had its chance."""
    from reviewlib import cli
    import reviewlib.features.visual.visual_cli as visual_cli

    reached = {"standalone": False}

    def _fake_standalone(*_a, **_k):
        reached["standalone"] = True
        return 0

    old_standalone = visual_cli.run_visual_standalone
    visual_cli.run_visual_standalone = _fake_standalone
    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": ["not-a-mapping"]
    }  # malformed board, irrelevant here
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        # No piped diff and the cwd is not inside a diff-producing repo path we control;
        # _git_diff degrades to "" for --visual, so this routes to the standalone pipeline.
        sys.stdin = io.StringIO("")
        rc = cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--visual",
                "/tmp/does-not-exist-zzz.png",
                "-C",
                "/tmp",
            ]
        )
        assert rc == 0, rc
        assert reached["standalone"] is True, (
            "standalone visual must run despite the malformed board"
        )
    finally:
        visual_cli.run_visual_standalone = old_standalone
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_all_malformed_board_fails_before_visual_fanout():
    """(codex P2) On the default-review path, an all-malformed `board:` must error
    BEFORE the --visual fan-out, so a doomed config never spends a paid vision call."""
    from reviewlib import cli
    import reviewlib.features.visual.compose as compose

    fanout_calls = {"n": 0}

    def _fake_build(*_a, **_k):
        fanout_calls["n"] += 1
        raise AssertionError(
            "visual fan-out must not run when the board config is invalid"
        )

    old_mode_review = _review_mod.mode_review
    _review_mod.mode_review = lambda *_a, **_k: 0
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping"]}
    old_build = compose.build_mode_visual_context
    compose.build_mode_visual_context = _fake_build
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--visual",
                "/tmp/does-not-exist-zzz.png",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert rc == 2, rc
        assert fanout_calls["n"] == 0, "fan-out ran before the board was validated"
    finally:
        _review_mod.mode_review = old_mode_review
        cli.load_config = old_load_config
        compose.build_mode_visual_context = old_build
        sys.stdin = old_stdin


def test_cli_default_path_passes_full_preset_board_and_default_pool():
    """No -m/--preset -> the full LIGHT-preset board is passed into mode_review (reserve
    incl.). Bare `review diff --task` floors light's pool 2 to 3 so one recorded
    iteration can satisfy gh ship (review-cli#309). `--show-board` without --task
    still shows pool 2."""
    captured = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)]
    )
    assert captured["board"] is not None, "board should be active by default"
    assert len(captured["board"]) == len(LIGHT_PRESET_BOARD), (
        "full board passed (reserve incl.)"
    )
    assert [r.model for r in captured["board"]] == [r.model for r in LIGHT_PRESET_BOARD]
    assert captured["pool_size"] == 3, captured["pool_size"]


def test_cli_builtin_default_uses_light_preset_board():
    captured = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)]
    )
    assert [r.model for r in captured["board"]] == [r.model for r in LIGHT_PRESET_BOARD]
    assert all(r.effort == "low" for r in captured["board"])
    assert captured["pool_size"] == 3


def test_cli_light_and_heavy_presets_set_board_and_default_pool():
    light = _capture_default_review_board(
        [
            "diff",
            "--task",
            "TEST-1",
            "--preset",
            "light",
            "-C",
            str(REPO_ROOT),
        ]
    )
    assert [r.model for r in light["board"]] == [r.model for r in LIGHT_PRESET_BOARD]
    assert all(r.effort == "low" for r in light["board"])
    assert light["pool_size"] == 3

    heavy = _capture_default_review_board(
        [
            "diff",
            "--task",
            "TEST-1",
            "--preset",
            "heavy",
            "-C",
            str(REPO_ROOT),
        ]
    )
    # review-cli#fable-seat-reliability: Fable is in NO level (a confirmed ~100% dispatch
    # failure rate makes it pure waste at any effort tier) — deep leads with Opus and Sol.
    assert [r.model for r in heavy["board"][:4]] == [
        "claude:claude-opus-5-5",
        SOL_SEAT,
        SONNET_SEAT,
        GROK_SEAT,
    ]
    assert all(r.effort == "xhigh" for r in heavy["board"])
    assert "claude:claude-fable-5-1" not in [r.model for r in heavy["board"]]
    assert heavy["pool_size"] == 4


def test_config_pool_key_sets_default_pool_size():
    """A config `pool:` int becomes the default pool size when no --pool is passed."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)],
        {"models": ["codex", "claude:claude-opus-5-5", "oc:zai/glm-5.3"], "pool": 3},
    )
    assert cap["pool_size"] == 3, cap["pool_size"]


def test_explicit_pool_flag_overrides_config_pool():
    """An explicit `--pool N` wins over the config `pool:` default."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)],
        {"models": ["codex", "claude:claude-opus-5-5", "oc:zai/glm-5.3"], "pool": 3},
    )
    assert cap["pool_size"] == 2, cap["pool_size"]


def test_explicit_preset_overrides_config_pool():
    """An explicit `--preset` sizes the pool from the preset, then a recorded
    `--task` floors light's 2 to 3 — still not the config `pool:` key."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "--preset", "light", "-C", str(REPO_ROOT)],
        {"pool": 5},
    )
    assert cap["pool_size"] == 3, cap["pool_size"]


def test_invalid_config_pool_falls_back_to_preset_default():
    """A non-positive / non-int config `pool:` is ignored (falls back to the preset —
    DEFAULT_PRESET is "light", then `--task` floors that 2 to 3)."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)],
        {"pool": 0},  # 0 = "all" is a CLI-only knob; as a config default it's ignored
    )
    assert cap["pool_size"] == 3, cap["pool_size"]


def test_cli_explicit_preset_overrides_config_board():
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
        captured["board"] = board
        captured["pool_size"] = pool_size
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "board": [
            {
                "model": "gemini",
                "role": "tests",
                "name": "Configured",
                "effort": "medium",
            }
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            ["diff", "--task", "TEST-1", "--preset", "heavy", "-C", str(REPO_ROOT)]
        )
        # review-cli#fable-seat-reliability: Fable is in no level.
        assert [r.model for r in captured["board"][:4]] == [
            "claude:claude-opus-5-5",
            SOL_SEAT,
            SONNET_SEAT,
            GROK_SEAT,
        ], captured
        assert captured["board"][0].effort == "xhigh", captured
        assert captured["pool_size"] == 4, captured
        assert "claude:claude-fable-5-1" not in [r.model for r in captured["board"]]
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_pool_flag_threads_pool_size():
    """--pool N threads N as pool_size into mode_review (the full board still flows so
    the reserve is available); --pool 0 = all seats."""
    cap2 = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)]
    )
    assert cap2["pool_size"] == 2, cap2["pool_size"]
    assert len(cap2["board"]) == len(LIGHT_PRESET_BOARD), "full board still passed"
    cap_all = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "--pool", "0", "-C", str(REPO_ROOT)]
    )
    assert cap_all["pool_size"] == 0, cap_all["pool_size"]


def test_cli_leading_pool_and_timeout_options_before_subcommand_are_honored():
    """Value-taking global flags before the mode verb must land on the mode parser."""
    captured = _capture_default_review_board(
        [
            "--pool",
            "2",
            "--timeout",
            "77",
            "diff",
            "--task",
            "TEST-1",
            "-C",
            str(REPO_ROOT),
        ]
    )
    assert captured["pool_size"] == 2, captured
    assert captured["timeout"] == 77, captured


def test_cli_leading_meta_flags_before_subcommand_are_honored():
    """Zero-arg global flags before the mode verb must land on the mode parser too."""
    from reviewlib import cli

    assert cli._normalize_leading_mode_options(["--list-defaults", "diff"]) == [
        "diff",
        "--list-defaults",
    ]
    captured: dict = {}
    old_show_board = cli._show_board
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    cli._show_board = lambda config, pool_size, cwd=None, **kw: (
        captured.update(
            {
                "pool_size": pool_size,
                "cwd": cwd,
                "preset": kw.get("preset"),
                "explicit_models": kw.get("explicit_models"),
            }
        )
        or 0
    )
    try:
        rc = cli.main(
            [
                "--show-board",
                "--pool",
                "2",
                "-m",
                "codex",
                "diff",
                "--preset",
                "heavy",
                "-C",
                str(REPO_ROOT),
            ]
        )
    finally:
        cli._show_board = old_show_board
        cli.load_config = old_load_config
    assert rc == 0
    assert captured["pool_size"] == 2, captured
    assert captured["cwd"] == REPO_ROOT, captured
    assert captured["preset"] == "deep", captured
    assert captured["explicit_models"] == ["codex"], captured


def test_show_board_honors_pool_flag_tagging():
    """`--show-board --pool N` (all seats available) must tag the top N priority seats
    `pool`, the rest `reserve`."""
    seat_lines = [
        ln for ln in _show_board_lines(2) if "[pool" in ln or "[reserve]" in ln
    ]
    assert len(seat_lines) == len(LIGHT_PRESET_BOARD)
    pool_lines = [ln for ln in seat_lines if "[pool" in ln]
    reserve_lines = [ln for ln in seat_lines if "[reserve]" in ln]
    assert len(pool_lines) == 2, pool_lines
    assert len(reserve_lines) == len(LIGHT_PRESET_BOARD) - 2, reserve_lines
    # The default level is routine: its first two seats are the pool.
    assert "Sonnet-omp" in pool_lines[0]
    assert "Sonnet " in pool_lines[1]


def test_show_board_explicit_model_is_exact():
    lines = _show_board_lines(4, explicit_models=["codex"])
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 1, lines
    assert "codex  [available]" in seat_lines[0], lines
    assert "effort=-" in seat_lines[0], lines
    assert "source: explicit -m" in lines[0], lines
    assert "exact -m run = every LIVE listed seat is attempted" in lines[0], lines
    assert any("--pool` and reserve failover do not slice" in ln for ln in lines), lines
    assert not any("claude:claude-opus-5-5" in ln for ln in seat_lines), lines
