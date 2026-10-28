"""Offline reviewer-board cases; collected by test_reviewer_board.py."""
from __future__ import annotations

from reviewer_board_fixtures import (
    ASTRA_SEAT,
    DEFAULT_BOARD,
    GROK_SEAT,
    Path,
    REPO_ROOT,
    SOL_SEAT,
    _capture_recorded_review_dispatch,
    _capture_review_with_config,
    _dispatch_just_ask_capturing_models,
    _show_board_lines,
    backends,
    os,
)

def test_show_board_explicit_model_with_preset_keeps_exact_model_and_preset_effort_metadata():
    # Astra (codex:gpt-6-astra) is a deep-level seat, so -m'ing it exactly is what
    # exercises "explicit model reuses the matching level entry's effort metadata".
    lines = _show_board_lines(4, preset="deep", explicit_models=[ASTRA_SEAT])
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 1, lines
    assert f"{ASTRA_SEAT}  [available]" in seat_lines[0], lines
    assert "effort=xhigh" in seat_lines[0], lines
    assert "source: explicit -m + level:deep" in lines[0], lines


def test_show_board_explicit_models_are_all_live_and_pool_is_ignored():
    lines = _show_board_lines(
        1,
        explicit_models=["codex", "gemini", "missing-provider:model"],
        available={"codex", "gemini"},
    )
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 3, lines
    assert not any(
        "[pool" in ln or "[reserve]" in ln or "[unavail]" in ln for ln in lines
    ), lines
    assert any(
        "missing-provider:model" in ln and "will attempt (no key/CLI)" in ln
        for ln in seat_lines
    ), lines
    assert any("--pool` and reserve failover do not slice" in ln for ln in lines), lines


def test_show_board_startup_failover_skips_unavailable_top_seat():
    """The live pool is the top-N AVAILABLE seats by priority: an unavailable higher
    priority seat is tagged `unavail` and the next available seat fills the pool."""
    # Opus (#1 in the deep level), Sol (#2): Sol unavailable -> the pool of 2 is Opus (#1)
    # + Sonnet (#3); Sol is unavail.
    avail = {r.model for r in DEFAULT_BOARD if r.model != SOL_SEAT}
    seat_lines = [
        ln
        for ln in _show_board_lines(2, available=avail, preset="deep")
        if "[pool" in ln or "[reserve]" in ln or "[unavail]" in ln
    ]
    by_tier = {"pool": [], "reserve": [], "unavail": []}
    for ln in seat_lines:
        for tier in by_tier:
            if f"[{tier}" in ln:
                by_tier[tier].append(ln)
                break
    assert len(by_tier["pool"]) == 2, by_tier["pool"]
    assert "Sol" in by_tier["unavail"][0], by_tier["unavail"]
    assert "Opus" in by_tier["pool"][0]
    assert "Sonnet" in by_tier["pool"][1]


def test_show_board_marks_claude_commandcode_api_gateway_unpaid():
    import contextlib
    import io

    from reviewlib import cli

    saved = {
        k: os.environ.get(k)
        for k in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "REVIEW_CLAUDE_MODE",
            "REVIEW_UNPAID_PROVIDERS",
        )
    }
    old_which = backends._which_optional
    buf = io.StringIO()
    try:
        backends._which_optional = lambda _name: None
        os.environ["ANTHROPIC_API_KEY"] = "user_x"
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.commandcode.ai/provider"
        os.environ.pop("REVIEW_CLAUDE_MODE", None)
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
        cfg = {"board": [{"model": "claude:claude-fable-5-1", "role": "tests"}]}
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(cfg, 1, REPO_ROOT)
    finally:
        backends._which_optional = old_which
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert rc == 0, buf.getvalue()
    lines = buf.getvalue().splitlines()
    row = next(line for line in lines if "claude:claude-fable-5-1" in line)
    assert "SKIPPED (provider unpaid/disabled)" in row, row


def test_show_board_pool_zero_marks_all_seats_pool():
    lines = _show_board_lines(0)
    assert any("live pool = all AVAILABLE seats by priority" in ln for ln in lines), (
        lines
    )
    assert any("runs all AVAILABLE seats" in ln for ln in lines), lines
    assert not any("top 0 AVAILABLE" in ln for ln in lines), lines
    seat_lines = [ln for ln in lines if "[pool" in ln or "[reserve]" in ln]
    assert all("[pool" in ln for ln in seat_lines), seat_lines


def test_show_board_deep_level_displays_effort_values():
    """Fable is in no level (a confirmed ~100% dispatch failure rate), so it has no row
    here; Sol and Astra both show the deep level's xhigh."""
    lines = _show_board_lines(4, preset="deep")
    assert not any("claude:claude-fable-5-1" in ln for ln in lines), lines
    sol = next(ln for ln in lines if SOL_SEAT in ln)
    # Match the DISPLAY name ("Astra"), not the bare model string — "codex" is also a
    # substring of both Sol's AND Astra's model ids (`codex:gpt-6.1-sol`,
    # `codex:gpt-6-astra`), which would false-match here.
    astra = next(ln for ln in lines if "Astra" in ln)
    assert "effort=xhigh" in sol, sol
    assert "effort=xhigh" in astra, astra


def test_show_board_tags_by_seat_not_model_for_duplicate_models():
    """A board with the SAME model in two seats must tag each per-SEAT (by object
    identity / priority position), so a duplicate model in the reserve isn't mislabeled
    `pool`. Pool of 1 (all available) -> seat #1 pool, the rest reserve."""
    lines = _show_board_lines(1, board_models=["codex", "codex", "gemini"])
    seat_lines = [ln for ln in lines if "[pool" in ln or "[reserve]" in ln]
    assert len(seat_lines) == 3
    assert "[pool" in seat_lines[0]
    assert "[reserve]" in seat_lines[1], seat_lines[1]  # the duplicate codex, by seat
    assert "[reserve]" in seat_lines[2]


def test_show_board_marks_seat_with_active_project_lens_override():
    """review-cli#547 acceptance criterion 3: `--show-board` must show when a seat's
    lens has been overridden by a project file. Only the seat(s) whose ROLE has an
    active `.review/lenses/<role>.md` are annotated; every other seat is untouched."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        lenses = cwd / ".review" / "lenses"
        lenses.mkdir(parents=True)
        (lenses / "correctness.md").write_text("Also check: our house style rule.")
        lines = _show_board_lines(2, board_models=["codex", "gemini"], cwd=cwd)
    role_lines = [ln for ln in lines if "[pool" in ln or "[reserve]" in ln]
    # board_models seats are all tagged role="tests" by the _show_board_lines helper,
    # so with a correctness.md override present NEITHER seat here is annotated.
    assert role_lines, lines
    assert not any("lens overridden" in ln for ln in role_lines), lines
    footer = "\n".join(lines)
    assert "Project lens overrides active" not in footer, footer

    with tempfile.TemporaryDirectory() as tmp2:
        cwd2 = Path(tmp2)
        lenses2 = cwd2 / ".review" / "lenses"
        lenses2.mkdir(parents=True)
        (lenses2 / "tests.md").write_text("Also check: our house style rule.")
        lines2 = _show_board_lines(2, board_models=["codex", "gemini"], cwd=cwd2)
    seat_lines2 = [ln for ln in lines2 if "[pool" in ln or "[reserve]" in ln]
    assert len(seat_lines2) == 2
    assert all(
        "lens overridden: .review/lenses/tests.md" in ln for ln in seat_lines2
    ), lines2
    assert any("Project lens overrides active" in ln for ln in lines2), lines2


def test_show_board_with_no_override_files_matches_pre_547_output():
    """review-cli#547 acceptance criterion 2: a repo with no `.review/lenses/` at all
    must print BYTE-IDENTICAL `--show-board` output to a repo where that directory was
    never introduced as a concept -- proven by comparing two independent empty repos."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        lines_a = _show_board_lines(
            2, board_models=["codex", "gemini"], cwd=Path(tmp_a)
        )
        lines_b = _show_board_lines(
            2, board_models=["codex", "gemini"], cwd=Path(tmp_b)
        )
    assert lines_a == lines_b
    assert not any("lens overridden" in ln for ln in lines_a)
    assert not any("Project lens overrides active" in ln for ln in lines_a)


def test_show_board_uses_config_models_as_priority_roster():
    """`review --show-board` must reflect config `models:` as the active priority roster,
    not silently show the unrelated built-in board."""
    import contextlib
    import io

    from reviewlib import cli

    cfg = {"models": ["codex", "gemini", "commandcode:deepseek/deepseek-v4-pro"]}
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(cfg, 2)
    finally:
        backends.backend_available = old_avail
    assert rc == 0, rc
    out = buf.getvalue()
    assert "source: config.yaml (models:)" in out, out
    seat_lines = [ln for ln in out.splitlines() if "[pool" in ln or "[reserve]" in ln]
    assert [
        "codex" in seat_lines[0],
        "gemini" in seat_lines[1],
        "commandcode:deepseek/deepseek-v4-pro" in seat_lines[2],
    ] == [True, True, True], seat_lines
    assert "[pool" in seat_lines[0] and "[pool" in seat_lines[1], seat_lines
    assert "[reserve]" in seat_lines[2], seat_lines


def test_cli_list_defaults_reports_normalized_config_models(capfd=None):
    """(codex P3) `--list-defaults` must report the SAME normalized models the review
    path uses: comma-joined entries split, blanks dropped, aliases expanded."""
    import io
    import contextlib

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["codex, fable51", "  ", ""]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    # "codex, fable51" splits into two; blanks dropped; fable51 -> claude:claude-fable-5-1.
    assert out == ["codex", "claude:claude-fable-5-1"], out


def test_cli_list_defaults_reports_config_board_without_models():
    """A board-only config must report the same reviewer ids the diff path will run."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
            {"model": "codex", "role": "consistency", "name": "Codex"},
        ]
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["gemini", "codex"], out


def test_cli_list_defaults_prefers_config_models_over_config_board():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "models": ["codex", "gemini"],
        "board": [
            {"model": "claude:claude-opus-5-5", "role": "correctness", "name": "Opus"},
        ],
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["codex", "gemini"], out


def test_panel_list_defaults_ignore_config_board():
    import contextlib
    import io

    from reviewlib import cli
    from reviewlib.config import preset_board

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
        ]
    }
    try:
        for argv in (
            ["just-ask", "q", "--list-defaults"],
            ["quorum", "q", "--list-defaults"],
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(argv)
            assert rc == 0, (argv, rc)
            assert buf.getvalue().strip().splitlines() == [
                seat.model for seat in preset_board("routine")
            ], argv
    finally:
        cli.load_config = old_load_config


def test_brainstorm_list_defaults_prefers_brainstorm_models_and_ignores_config_board():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "brainstorm_models": ["gemini"],
        "models": ["codex"],
        "board": [
            {"model": "claude:claude-opus-5-5", "role": "correctness", "name": "Opus"},
        ],
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["brainstorm", "topic", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["gemini"], out


def test_cli_list_defaults_all_malformed_board_errors_cleanly():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping", {"role": "tests"}]}
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["--list-defaults"])
    finally:
        cli.load_config = old_load_config
    assert rc == 2, rc
    assert stdout.getvalue() == ""
    assert "config.yaml `board:` has 2 entries but none is usable" in stderr.getvalue()


def test_diff_model_help_reports_config_board_without_models():
    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
            {"model": "codex", "role": "consistency", "name": "Codex"},
        ]
    }
    try:
        help_text = cli._build_mode_parser(cli.diff_mode()).format_help()
    finally:
        cli.load_config = old_load_config
    assert "your config.yaml board: gemini, codex" in help_text, help_text


def test_cli_list_defaults_empty_config_uses_code_defaults():
    """An effectively-empty config `models:` -> --list-defaults shows the default level."""
    import io
    import contextlib

    from reviewlib import cli
    from reviewlib.config import preset_board

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["", "  "]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == [r.model for r in preset_board()], out


def test_cli_panel_list_defaults_do_not_use_diff_preset():
    import io
    import contextlib

    from reviewlib import cli
    from reviewlib.config import preset_board

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["brainstorm", "topic", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == [seat.model for seat in preset_board("routine")], out


def test_cli_level_flag_selects_every_text_panel_but_not_visual():
    """The chosen level controls the panel, not only its moderator (#699)."""
    import contextlib
    import io
    from unittest.mock import patch
    from reviewlib import cli
    from reviewlib.config import preset_board

    expected = [seat.model for seat in preset_board("deep")]
    with patch.object(cli, "load_config", lambda: {}):
        for mode in ("just-ask", "quorum", "brainstorm"):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.main([mode, "topic", "--level", "deep", "--list-defaults"])
            assert rc == 0
            assert stdout.getvalue().strip().splitlines() == expected


def test_cli_list_defaults_explicit_models_win_over_preset():
    import io
    import contextlib

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(
                ["diff", "--preset", "heavy", "-m", "gemini,codex", "--list-defaults"]
            )
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == ["gemini", "codex"], out


def test_just_ask_default_panel_drops_unavailable_grok_seat():
    """review-cli#418: adding GROK_SEAT to DEFAULT_MODELS must not fail a
    default `just-ask` on a host without xAI OAuth. Other reachable defaults
    still dispatch; the all-usable exit stays 0."""
    from reviewlib.config import preset_board, preset_pool_size

    assert GROK_SEAT in [seat.model for seat in preset_board("routine")]
    live = [seat.model for seat in preset_board("routine") if seat.model != GROK_SEAT][:preset_pool_size("routine")]
    saved_avail = backends.backend_available
    saved_unpaid = backends.runtime_provider_marked_unpaid
    try:
        backends.backend_available = lambda model: model != GROK_SEAT
        backends.runtime_provider_marked_unpaid = lambda _m: False
        cap = _dispatch_just_ask_capturing_models()
    finally:
        backends.backend_available = saved_avail
        backends.runtime_provider_marked_unpaid = saved_unpaid
    assert cap["rc"] == 0, cap
    assert cap["models"] == live, cap


def test_just_ask_explicit_dash_m_still_dispatches_unavailable_grok():
    """Explicit `-m grok` is honored as-is even when the probe says down —
    the operator asked for that seat — and, with `--task`, it stays ONE seat:
    just-ask never pads a single `-m` model into role slots (review-cli#587)."""
    saved_avail = backends.backend_available
    saved_unpaid = backends.runtime_provider_marked_unpaid
    try:
        backends.backend_available = lambda _model: False
        backends.runtime_provider_marked_unpaid = lambda _m: False
        cap = _dispatch_just_ask_capturing_models(extra_argv=["-m", "grok"])
    finally:
        backends.backend_available = saved_avail
        backends.runtime_provider_marked_unpaid = saved_unpaid
    assert cap["rc"] == 0, cap
    assert cap["models"] == [GROK_SEAT], cap


def test_just_ask_all_unreachable_defaults_refuse_before_dispatch():
    """An exhausted selected level must not dispatch unverified or heavier seats."""

    saved_avail = backends.backend_available
    saved_unpaid = backends.runtime_provider_marked_unpaid
    try:
        backends.backend_available = lambda _model: False
        backends.runtime_provider_marked_unpaid = lambda _m: False
        cap = _dispatch_just_ask_capturing_models()
    finally:
        backends.backend_available = saved_avail
        backends.runtime_provider_marked_unpaid = saved_unpaid
    assert cap["rc"] == 1, cap
    assert "models" not in cap, cap


def test_cli_recorded_task_floors_light_pool_to_ship_min_roles():
    """`--task` + `--preset light` must run pool >= 3 so one recorded iteration
    can satisfy default `review task CODE --check` / gh ship (min_roles=3).
    Light without --task stays 2 (see show-board pin below)."""
    captured = _capture_recorded_review_dispatch(
        [
            "diff",
            "--task",
            "309",
            "--preset",
            "light",
            "-C",
            str(REPO_ROOT),
        ]
    )
    assert captured["rc"] == 0, captured
    assert captured["pool_size"] == 3, captured
    assert captured["usage_percent"] is not None, captured
    assert captured.get("exact_board") is not True, captured


def test_cli_recorded_task_single_model_pads_across_ship_min_roles():
    """`-m grok` WITH `--task` must pad that one model across >=3 distinct
    board roles (select_pool_with_reuse), not dispatch an exact 1-seat board."""
    captured = _capture_recorded_review_dispatch(
        ["diff", "--task", "309", "-m", "grok", "-C", str(REPO_ROOT)]
    )
    assert captured["rc"] == 0, captured
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} == {GROK_SEAT}, captured
    roles = [r.role for r in board if r.role]
    assert len(set(roles)) >= 3, (roles, captured)
    assert captured["pool_size"] == 3, captured
    assert captured["exact_board"] is not True, captured
    assert captured["usage_percent"] is not None, captured


def test_cli_recorded_task_explicit_pool_above_floor_wins_over_single_model_pad():
    """review-cli#309 review finding: an explicit `--pool N` with N >= SHIP_MIN_ROLES
    combined with a single `-m` model and `--task` must still WIN over the
    single-model pad's own SHIP_MIN_ROLES target (documented in this diff's own
    `--help` text: "`--pool N` still wins"). The model is cloned across N
    distinct roles (task_role_pad's cloning still applies), not silently
    clamped back down to 3."""
    captured = _capture_recorded_review_dispatch(
        ["diff", "--task", "309", "--pool", "5", "-m", "grok", "-C", str(REPO_ROOT)]
    )
    assert captured["rc"] == 0, captured
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} == {GROK_SEAT}, captured
    roles = [r.role for r in board if r.role]
    assert len(set(roles)) == 5, (roles, captured)
    assert captured["pool_size"] == 5, captured


def test_show_board_light_preset_keeps_pool_two_without_task():
    """`--show-board` is not a recorded review: light stays pool 2.
    Isolated from `$REVIEW_TASK_CODE` so a session env cannot floor this pin."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    old_task = os.environ.get("REVIEW_TASK_CODE")
    cli.load_config = lambda: {}
    os.environ.pop("REVIEW_TASK_CODE", None)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--show-board", "--preset", "light"])
    finally:
        cli.load_config = old_load_config
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
    assert rc == 0, buf.getvalue()
    out = buf.getvalue()
    assert "top 2 AVAILABLE" in out, out


def test_show_board_with_task_floors_light_pool_to_three():
    """`review diff --task CODE --show-board --preset light` must display pool 3."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(
                [
                    "diff",
                    "--task",
                    "309",
                    "--show-board",
                    "--preset",
                    "light",
                ]
            )
    finally:
        cli.load_config = old_load_config
    assert rc == 0, buf.getvalue()
    out = buf.getvalue()
    assert "top 3 AVAILABLE" in out, out


def test_cli_recorded_task_short_config_roster_pads_to_ship_min_roles():
    """A 1-2 model config `models:` roster WITH `--task` must clone unused roles
    so the board has EXACTLY SHIP_MIN_ROLES (3) distinct lenses -- not the
    unrelated DEFAULT_POOL_SIZE (4) fallback (review-cli#309 review finding:
    `>= 3` alone did not catch an over-pad to 4, contradicting CHANGELOG's own
    "pad to 3" claim for this exact case)."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-C", str(REPO_ROOT)],
        {"models": ["codex", "gemini"]},
    )
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} <= {"codex", "gemini"}
    assert len({r.role for r in board if r.role}) == 3, [
        (r.model, r.role) for r in board
    ]
    assert captured["pool_size"] == 3, captured


def test_cli_recorded_task_exact_ship_min_roster_stays_at_own_size():
    """review-cli#309 round-2 review finding: a config `models:` roster with
    EXACTLY SHIP_MIN_ROLES (3) entries WITH `--task` must stay at pool 3 -- a
    strict `config_roster_len < SHIP_MIN_ROLES` comparison let `3 < 3` fall
    through to the unrelated DEFAULT_POOL_SIZE (4) fallback, synthesizing an
    unnecessary 4th cloned seat (one extra real paid model dispatch) even
    though the roster already met the ship floor on its own."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-C", str(REPO_ROOT)],
        {"models": ["codex", "gemini", "claude:claude-opus-5-5"]},
    )
    board = captured["board"]
    assert board, captured
    assert {r.model for r in board} <= {"codex", "gemini", "claude:claude-opus-5-5"}
    assert len(board) == 3, [(r.model, r.role) for r in board]
    assert captured["pool_size"] == 3, captured


def test_cli_recorded_task_duplicate_role_roster_prefix_has_distinct_roles():
    """Two config seats sharing one role + `--task` must not ship a pool prefix
    with only 2 distinct lenses (review-cli#309). Duplicate-role seats move
    behind a distinct-role prefix."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "-C", str(REPO_ROOT)],
        {
            "board": [
                {"model": "codex", "role": "correctness", "name": "A"},
                {"model": "gemini", "role": "correctness", "name": "B"},
            ],
        },
    )
    board = captured["board"]
    n = captured["pool_size"]
    assert n >= 3, captured
    prefix = board[:n]
    assert len({r.role for r in prefix if r.role}) >= 3, [
        (r.model, r.role) for r in prefix
    ]


def test_cli_recorded_task_full_board_distinct_count_still_pads_duplicate_prefix():
    """review-cli#309 round-4 review finding: a board with >= SHIP_MIN_ROLES distinct
    roles OVERALL can still slice to FEWER distinct roles in the pool-sized PREFIX if a
    duplicate-role seat sits ahead of a distinct one in priority order. Gating the
    `ensure_min_distinct_roles` call on the full board's distinct-role count (rather
    than calling it unconditionally, since it is a safe no-op when the prefix is
    already fine) missed exactly this shape: 4 seats, 3 distinct roles overall
    (correctness/security/contracts), but seat #2 duplicates seat #1's role, so a
    naive positional pool-3 slice only covers 2 distinct roles."""
    captured = _capture_review_with_config(
        ["diff", "--task", "309", "--pool", "3", "-C", str(REPO_ROOT)],
        {
            "pool": 3,
            "board": [
                {
                    "model": "claude:claude-opus-5-5",
                    "role": "correctness",
                    "name": "Opus",
                },
                {
                    "model": "claude:claude-sonnet-5-5",
                    "role": "correctness",
                    "name": "Sonnet",
                },
                {"model": "oc:xai/grok-4.7", "role": "security", "name": "Grok"},
                {"model": "gemini", "role": "contracts", "name": "Gemini"},
            ],
        },
    )
    board = captured["board"]
    n = captured["pool_size"]
    assert n == 3, captured
    prefix = board[:n]
    assert len({r.role for r in prefix if r.role}) == 3, [
        (r.model, r.role) for r in prefix
    ]


def test_show_board_task_single_model_shows_padded_roles():
    """`review diff --task CODE -m grok --show-board` describes the same 3-role
    pad the run would dispatch, not a 1-seat exact board."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {}
    backends.backend_available = lambda _m: True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--task", "309", "-m", "grok", "--show-board"])
    finally:
        cli.load_config = old_load_config
        backends.backend_available = old_avail
    assert rc == 0, buf.getvalue()
    out = buf.getvalue()
    assert "[explicit]" not in out, out
    assert "top 3 AVAILABLE" in out, out
    pool_lines = [ln for ln in out.splitlines() if "[pool" in ln]
    assert len(pool_lines) >= 3, out


def test_early_task_and_task_code_pool_floor_decisions_always_agree():
    """review-cli#309 review finding: `early_task` (the pre-`--show-board`
    pool-floor computation, ~cli.py line 4946) and `task_code` (the real
    dispatch's `task_role_pad` gate, ~cli.py line 5034) are two independently
    re-derived `normalize_task_code(getattr(args, "task", None) or
    os.environ.get("REVIEW_TASK_CODE"))` calls with no mutation of either
    source in between -- they can never disagree on whether a recorded run
    pads to the ship floor. Pin that a single-model `-m` + `--task` run
    reports the SAME pool size whether observed via `--show-board`
    (early_task path) or the real dispatch (task_code path)."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {}
    backends.backend_available = lambda _m: True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--task", "309", "-m", "grok", "--show-board"])
    finally:
        cli.load_config = old_load_config
        backends.backend_available = old_avail
    assert rc == 0, buf.getvalue()
    show_board_pool = len([ln for ln in buf.getvalue().splitlines() if "[pool" in ln])

    captured = _capture_recorded_review_dispatch(
        ["diff", "--task", "309", "-m", "grok", "-C", str(REPO_ROOT)]
    )
    assert captured["rc"] == 0, captured
    assert captured["pool_size"] == show_board_pool, (captured, show_board_pool)
