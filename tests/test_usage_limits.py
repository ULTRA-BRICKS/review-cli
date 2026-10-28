#!/usr/bin/env python3
"""Unit tests for reviewlib.usage_limits — usage-percent + spawn gate for
board/panel reuse-and-exclusion (see test_pool_reuse.py for the consumer).

tg-ctl path: tests drive `usage_percent_for_model` via `samples=` (a plain
list of dicts, matching tg-ctl's `usage-latest.json` shape) — no live
~/.config/tg-cli file. `test_reads_real_usage_file` /
`test_merges_samples_across_multiple_chat_files` prove the file-reading
path against throwaway files.

Harness path: tests inject cards via `cards=` / `runner=` /
`$REVIEW_HARNESS_LIMITS_JSON` so they never hit the network or the real
`harness` binary.

Plain-script harness (mirrors tests/test_moderator.py): each test_* is run
by __main__, and also pytest-discoverable.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402

from reviewlib.usage_limits import (  # noqa: E402
    allow_spawn_for_model,
    load_snapshot,
    usage_percent_for_model,
)

# A fixed epoch anchored to real wall-clock time at import time (NOT a hardcoded
# constant) — every test that needs a "sample taken right now" derives it from
# this, so none of them silently go stale as real time passes (codex/Fable
# review finding: a hardcoded NOW + no now= at the call site is a time bomb).
import time as _time  # noqa: E402

NOW = _time.time()


def _sample(
    agent: str, limit_name: str, percent: float, age_seconds: float = 0.0
) -> dict:
    return {
        "agent": agent,
        "limitName": limit_name,
        "percent": percent,
        "resetAt": None,
        "sampledAt": (NOW - age_seconds) * 1000,
    }



def _window(name, used_percent, window_seconds):
    return {
        "name": name,
        "used_percent": used_percent,
        "resets_at": None,
        "window_seconds": window_seconds,
    }


def _harness_card(provider, windows, *, status="ok", account="a@b"):
    return {
        "provider": provider,
        "account": account,
        "status": status,
        "windows": windows,
    }


def test_unmapped_provider_is_unknown():
    samples = [_sample("claude", "weekly", 99)]
    assert usage_percent_for_model("zai:glm-5.2", samples=samples, now=NOW) is None
    assert usage_percent_for_model("gemini", samples=samples, now=NOW) is None


def test_claude_prefix_maps_to_claude_family():
    samples = [_sample("claude", "weekly", 82)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 82
    )
    assert (
        usage_percent_for_model("claude:claude-fable-5", samples=samples, now=NOW) == 82
    )


def test_bare_codex_maps_to_codex_family():
    samples = [_sample("codex", "5-hour", 55)]
    assert usage_percent_for_model("codex", samples=samples, now=NOW) == 55


def test_context_window_limit_is_ignored():
    # "context window" is a per-call size limit, not a rate/usage budget — it must
    # never feed the near-limit check even if it happens to read high.
    samples = [_sample("claude", "context window", 95), _sample("claude", "weekly", 10)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 10
    )


def test_max_of_5hour_and_weekly_wins():
    samples = [_sample("claude", "5-hour", 20), _sample("claude", "weekly", 88)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 88
    )


def test_stale_sample_is_ignored():
    stale = _sample("claude", "weekly", 99, age_seconds=48 * 3600)
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=[stale], now=NOW)
        is None
    )


def test_freshest_sample_wins_over_higher_but_stale_duplicate():
    # load_snapshot() merges multiple tg-ctl chat files -- two samples for
    # the SAME (agent, limitName) key can both survive the 24h trust window.
    # The FRESHER one must win, even though the STALER one reads higher.
    stale_high = _sample("claude", "weekly", 90, age_seconds=3600)
    fresh_low = _sample("claude", "weekly", 15, age_seconds=60)
    samples = [stale_high, fresh_low]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 15
    )
    # order-independent
    assert (
        usage_percent_for_model(
            "claude:claude-opus-4-8", samples=list(reversed(samples)), now=NOW
        )
        == 15
    )


def test_reset_at_in_the_past_is_ignored_even_within_trust_window():
    # A "5-hour" sample taken 6 hours ago whose OWN resetAt already passed is
    # describing a window that no longer applies, regardless of the 24h
    # sampledAt trust window.
    expired = _sample("claude", "5-hour", 95, age_seconds=6 * 3600)
    expired["resetAt"] = (NOW - 3600) * 1000  # reset already happened
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=[expired], now=NOW)
        is None
    )


def test_reset_at_in_the_future_is_honored():
    still_active = _sample("claude", "5-hour", 95)
    still_active["resetAt"] = (NOW + 3600) * 1000  # resets an hour from now
    assert (
        usage_percent_for_model(
            "claude:claude-opus-4-8", samples=[still_active], now=NOW
        )
        == 95
    )


def test_codex_prefixed_seat_maps_to_codex_family():
    # e.g. config.SOL_SEAT = "codex:gpt-5.6-sol" -- shares the codex account
    # quota with the bare "codex" seat.
    samples = [_sample("codex", "weekly", 77)]
    assert usage_percent_for_model("codex:gpt-5.6-sol", samples=samples, now=NOW) == 77


def test_malformed_samples_never_raise():
    junk = [
        "not a dict",
        {"agent": "claude"},  # missing limitName/percent/sampledAt
        {
            "agent": "claude",
            "limitName": "weekly",
            "percent": "not-a-number",
            "sampledAt": NOW * 1000,
        },
        None,
    ]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=junk, now=NOW) is None
    )


def test_reads_real_usage_file(tmp_path=None):
    tmp_dir = Path(tempfile.mkdtemp())
    usage_file = tmp_dir / "usage-latest.json"
    usage_file.write_text(
        json.dumps(
            {
                "version": 1,
                "samples": [_sample("codex", "weekly", 91)],
            }
        ),
        encoding="utf-8",
    )
    old_override = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    old_harness = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = str(usage_file)
    # Empty harness cards: this test is the tg-ctl file path, so harness
    # must not report a family (live `harness limits` on this machine would).
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = '{"cards":[]}'
    try:
        assert usage_percent_for_model("codex") == 91
    finally:
        if old_override is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_override
        if old_harness is None:
            os.environ.pop("REVIEW_HARNESS_LIMITS_JSON", None)
        else:
            os.environ["REVIEW_HARNESS_LIMITS_JSON"] = old_harness


def test_missing_file_is_unknown():
    old_override = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    old_harness = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = "/nonexistent/path/usage-latest.json"
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = '{"cards":[]}'
    try:
        assert usage_percent_for_model("codex") is None
    finally:
        if old_override is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_override
        if old_harness is None:
            os.environ.pop("REVIEW_HARNESS_LIMITS_JSON", None)
        else:
            os.environ["REVIEW_HARNESS_LIMITS_JSON"] = old_harness


def test_merges_samples_across_multiple_chat_files():
    # Two different tg-ctl chat ids each get their own usage-latest.json; one
    # tracks only claude, the other only codex. A "pick the freshest file"
    # strategy would silently drop whichever family's file is less recently
    # touched — load_snapshot() must MERGE both instead.
    tmp_dir = Path(tempfile.mkdtemp())
    (tmp_dir / "tg-ctl.111.usage-latest.json").write_text(
        json.dumps({"version": 1, "samples": [_sample("claude", "weekly", 40)]}),
        encoding="utf-8",
    )
    (tmp_dir / "tg-ctl.222.usage-latest.json").write_text(
        json.dumps({"version": 1, "samples": [_sample("codex", "weekly", 60)]}),
        encoding="utf-8",
    )
    old_dir = os.environ.get("REVIEW_USAGE_LIMITS_DIR")
    old_file = os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
    os.environ["REVIEW_USAGE_LIMITS_DIR"] = str(tmp_dir)
    try:
        snapshot = load_snapshot()
        assert (
            usage_percent_for_model("claude:claude-opus-4-8", samples=snapshot, now=NOW)
            == 40
        )
        assert usage_percent_for_model("codex", samples=snapshot, now=NOW) == 60
    finally:
        if old_dir is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_DIR", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_DIR"] = old_dir
        if old_file is not None:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_file


def test_harness_session_100_refuses_claude_spawn():
    cards = [
        _harness_card(
            "Claude Code",
            [
                _window("Session", 100.0, 18000),
                _window("Weekly", 81.0, 604800),
            ],
        )
    ]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", cards=cards) == 100
    )
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=cards) is False


def test_harness_weekly_max_still_allows_spawn():
    cards = [
        _harness_card(
            "Claude Code",
            [
                _window("Session", 20.0, 18000),
                _window("Weekly", 88.0, 604800),
            ],
        )
    ]
    assert usage_percent_for_model("claude:claude-opus-4-8", cards=cards) == 88
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=cards) is True


def test_harness_missing_card_fails_open():
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=[]) is True
    missing = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 18000)],
            status="missing_creds",
        )
    ]
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=missing) is True
    unknown = [
        _harness_card("Claude Code", [_window("Session", None, 18000)])
    ]
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=unknown) is True


def test_harness_identifies_windows_by_seconds_not_name():
    cards = [
        _harness_card(
            "Claude Code",
            [
                _window("Unexpected session label", 100.0, 5 * 3600),
                _window("Unexpected weekly label", 10.0, 7 * 86400),
            ],
        )
    ]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", cards=cards) == 100
    )
    assert allow_spawn_for_model("claude:claude-opus-4-8", cards=cards) is False


def test_harness_runner_inject_never_hits_binary():
    payload = json.dumps(
        {
            "cards": [
                _harness_card(
                    "Claude Code",
                    [_window("Session", 100.0, 18000)],
                )
            ]
        }
    )

    def runner():
        return payload

    assert usage_percent_for_model("claude:claude-opus-4-8", runner=runner) == 100
    assert allow_spawn_for_model("claude:claude-opus-4-8", runner=runner) is False


def test_exhausted_5h_does_not_popen_claude():
    from reviewlib import backends
    from reviewlib.backends import ReviewResult

    cards = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 5 * 3600)],
        )
    ]
    calls = []

    def fake_cli(*_a, **_k):
        calls.append(1)
        return ReviewResult(
            model="claude:claude-opus-4-8",
            command="claude",
            returncode=0,
            stdout="should not run",
            stderr="",
        )

    saved_cli = backends.review_claude_cli
    saved_unpaid = backends.unpaid_provider_result
    saved_json = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    saved_logs = os.environ.get("REVIEW_LOG_DIR")
    saved_mode = os.environ.get("REVIEW_CLAUDE_MODE")
    saved_file = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    tmp = tempfile.mkdtemp()
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = json.dumps({"cards": cards})
    os.environ["REVIEW_LOG_DIR"] = tmp
    os.environ["REVIEW_CLAUDE_MODE"] = "cli"
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = str(Path(tmp) / "missing.json")
    backends.review_claude_cli = fake_cli
    backends.unpaid_provider_result = lambda *_a, **_k: None
    try:
        result = backends.review_claude(
            "claude:claude-opus-4-8", "prompt", "diff", Path("."), 60
        )
    finally:
        backends.review_claude_cli = saved_cli
        backends.unpaid_provider_result = saved_unpaid
        if saved_json is None:
            os.environ.pop("REVIEW_HARNESS_LIMITS_JSON", None)
        else:
            os.environ["REVIEW_HARNESS_LIMITS_JSON"] = saved_json
        if saved_logs is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = saved_logs
        if saved_mode is None:
            os.environ.pop("REVIEW_CLAUDE_MODE", None)
        else:
            os.environ["REVIEW_CLAUDE_MODE"] = saved_mode
        if saved_file is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = saved_file
    assert calls == [], "exhausted 5-hour window still dispatched claude"
    assert result.returncode == 0
    assert "5-hour" in result.stdout or "5h" in result.stdout


def test_composition_closure_with_session_100_excludes_claude_family():
    # Production _dispatch must pass cards= (not samples= alone, which this
    # branch treats as disabling live harness). A 100% Session card excludes
    # the claude family even when tg-ctl samples would keep it; no network.
    from reviewlib.config import expand_flat_models_with_reuse

    samples = [_sample("claude", "weekly", 10)]
    cards = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 18000)],
        )
    ]
    roster = ["claude:claude-opus-4-8", "gemini"]

    def samples_only(model: str) -> float | None:
        return usage_percent_for_model(model, samples=samples, now=NOW)

    def with_cards(model: str) -> float | None:
        return usage_percent_for_model(
            model, samples=samples, cards=cards, now=NOW
        )

    kept = expand_flat_models_with_reuse(
        roster, 2, usage_percent=samples_only
    )
    assert "claude:claude-opus-4-8" in kept

    excluded = expand_flat_models_with_reuse(
        roster, 2, usage_percent=with_cards
    )
    assert "claude:claude-opus-4-8" not in excluded
    assert excluded  # gemini remains


def test_api_mode_does_not_skip_on_claude_code_quota():
    # REVIEW_CLAUDE_MODE=api must not return the usage-limit sentinel solely
    # because a Claude Code harness card is at 100% — that card measures the
    # CLI subscription, not the paid API account.
    from reviewlib import backends
    from reviewlib.backends import ReviewResult

    cards = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 5 * 3600)],
        )
    ]
    api_calls = []

    def fake_api(*_a, **_k):
        api_calls.append(1)
        return ReviewResult(
            model="claude:claude-opus-4-8",
            command="api",
            returncode=0,
            stdout="API ran",
            stderr="",
        )

    saved_api = backends.review_claude_api
    saved_unpaid = backends.unpaid_provider_result
    saved_json = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    saved_logs = os.environ.get("REVIEW_LOG_DIR")
    saved_mode = os.environ.get("REVIEW_CLAUDE_MODE")
    saved_file = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    saved_cd = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
    tmp = tempfile.mkdtemp()
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = json.dumps({"cards": cards})
    os.environ["REVIEW_LOG_DIR"] = tmp
    os.environ["REVIEW_CLAUDE_MODE"] = "api"
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = str(Path(tmp) / "missing.json")
    os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(tmp) / "cooldown.json")
    backends.review_claude_api = fake_api
    backends.unpaid_provider_result = lambda *_a, **_k: None
    try:
        result = backends.review_claude(
            "claude:claude-opus-4-8", "prompt", "diff", Path("."), 60
        )
    finally:
        backends.review_claude_api = saved_api
        backends.unpaid_provider_result = saved_unpaid
        for key, saved in (
            ("REVIEW_HARNESS_LIMITS_JSON", saved_json),
            ("REVIEW_LOG_DIR", saved_logs),
            ("REVIEW_CLAUDE_MODE", saved_mode),
            ("REVIEW_USAGE_LIMITS_FILE", saved_file),
            ("REVIEW_SEAT_COOLDOWN_FILE", saved_cd),
        ):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
    assert api_calls == [1], "API transport skipped because Claude Code was at 100%"
    assert result.stdout == "API ran"
    assert "usage-limit skip" not in result.command


# ---- the configured account budget on the direct claude CLI seat (review-cli#696) ----------

import contextlib  # noqa: E402
import stat  # noqa: E402

from reviewlib import account_budget, backends  # noqa: E402
from reviewlib.usage_limits import spawn_refuse_reason  # noqa: E402

_SONNET = "claude:claude-sonnet-5-5"
_FIVE_H, _SEVEN_D = 5 * 3600, 7 * 86400
_BLOCKED = "Blocked by review-cli configured 80% budget ceiling"


@contextlib.contextmanager
def _budget(raw=None):
    """The account budget as the CLI configures it (shipped defaults + `raw`)."""
    account_budget.configure(raw)
    try:
        yield
    finally:
        account_budget.unconfigure()


@contextlib.contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _both(five, seven, *, account="a@example.com", resets=None, extra=()):
    """One Claude Code card for `account` with its 5-hour and 7-day windows."""
    windows = [
        {**_window("Session", five, _FIVE_H), "resets_at": resets},
        {**_window("Weekly", seven, _SEVEN_D), "resets_at": resets},
        *extra,
    ]
    return _harness_card("Claude Code", windows, account=account)


def _refusal(cards, account="a@example.com", model=_SONNET, **kw):
    return spawn_refuse_reason(model, cards=cards, account=account, **kw)


def test_budget_79_99_is_allowed_80_and_100_are_blocked_in_either_window():
    with _budget():
        assert _refusal([_both(79.99, 79.99)]) is None
        for five, seven in ((80.0, 10), (10, 80.0), (100.0, 100.0), (99.5, 0)):
            held = _refusal([_both(five, seven)])
            assert held is not None and held.startswith(_BLOCKED), (five, seven, held)
    with _budget():
        assert "5-hour window at 80%" in _refusal([_both(80.0, 10)])
        assert "7-day window at 80%" in _refusal([_both(10, 80.0)])


def test_budget_ceiling_is_data_not_a_literal_in_the_dispatch_code():
    # the shipped default lives in levels.yaml; config overrides it, per provider too
    assert account_budget.resolve(None).max_usage_percent == 80
    with _budget({"max_usage_percent": 90}):
        assert _refusal([_both(85, 85)]) is None
        assert "90% budget ceiling" in _refusal([_both(90, 10)])
    with _budget({"max_usage_percent": 90, "providers": {"anthropic": 50}}):
        held = _refusal([_both(60, 10)])
        assert held is not None and "50% budget ceiling" in held, held


def test_budget_unconfigured_keeps_the_legacy_100_percent_gate():
    account_budget.unconfigure()
    assert _refusal([_both(95, 95)]) is None
    assert _refusal([_both(100, 5)]) == "harness 5-hour window exhausted (100%)"


def test_budget_missing_failed_and_empty_sources_fail_closed_with_the_reason():
    def missing():
        raise FileNotFoundError("harness")

    def failed():
        raise RuntimeError("boom")

    with _budget(), _env(REVIEW_HARNESS_LIMITS_JSON=None):
        for runner, why in (
            (missing, "harness limits source missing"),
            (failed, "harness limits source failed"),
            (lambda: "not json", "harness limits source malformed"),
        ):
            held = spawn_refuse_reason(_SONNET, runner=runner, account="a@example.com")
            assert held is not None and held.startswith(_BLOCKED), held
            assert why in held and "failing closed" in held, held
        no_card = spawn_refuse_reason(
            _SONNET, runner=lambda: json.dumps({"cards": []}), account="a@example.com"
        )
        assert "no usage card for this account" in no_card, no_card


def test_budget_stale_harness_read_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "limits.json"
        path.write_text(json.dumps({"cards": [_both(5, 5)]}), encoding="utf-8")
        old = _time.time() - 3 * 3600
        os.utime(path, (old, old))
        with _budget(), _env(REVIEW_HARNESS_LIMITS_JSON=str(path)):
            held = spawn_refuse_reason(_SONNET, account="a@example.com")
            assert held is not None and "were read" in held and "ago" in held, held
        os.utime(path, None)
        with _budget(), _env(REVIEW_HARNESS_LIMITS_JSON=str(path)):
            assert spawn_refuse_reason(_SONNET, account="a@example.com") is None


def test_budget_needs_both_the_5_hour_and_the_7_day_reading():
    only_5h = _harness_card(
        "Claude Code", [_window("Session", 5, _FIVE_H)], account="a@example.com"
    )
    only_7d = _harness_card(
        "Claude Code", [_window("Weekly", 5, _SEVEN_D)], account="a@example.com"
    )
    unreadable = _both(None, 5)
    with _budget():
        for card, missing in (
            (only_5h, "7-day"),
            (only_7d, "5-hour"),
            (unreadable, "5-hour"),
        ):
            held = _refusal([card])
            assert held is not None and f"no current {missing}" in held, held
    with _budget({"strict": False}):
        assert _refusal([only_5h]) is None, "lenient: unknown usage is let through"
        assert _refusal([]) is None
        assert _BLOCKED in _refusal([_both(85, 1)]), "lenient still holds a known 85%"


def test_budget_never_mixes_another_accounts_usage():
    cards = [
        _both(5, 5, account="a@example.com"),
        _both(95, 95, account="b@example.com"),
    ]
    with _budget():
        assert _refusal(cards, account="a@example.com") is None
        assert _refusal(cards, account="A@Example.com ") is None
        assert _BLOCKED in _refusal(cards, account="b@example.com")
        other = _refusal(cards, account="c@example.com")
        assert "no usage card for this account (only other accounts report)" in other
        # an implicit account cannot be matched to either card: fail closed, not "a is fine"
        implicit = _refusal(cards, account=None)
        assert "2 Claude accounts report usage" in implicit, implicit
        # a lone card is the implicit account's
        assert _refusal([_both(5, 5)], account=None) is None
        assert _BLOCKED in _refusal([_both(90, 5)], account=None)


def test_budget_a_window_that_already_reset_is_not_a_reading():
    past, future = NOW - 60, NOW + 3600
    with _budget():
        held = _refusal([_both(90, 10, resets=past)])
        assert held is not None and "cannot confirm" in held, held
        held = _refusal([_both(90, 10, resets=future)], now=NOW)
        assert held is not None and "5-hour window at 90%" in held, held
        assert "resets 20" in held, "a known reset time is reported"


def test_budget_tier_window_counts_for_its_tier_and_never_completes_the_pair():
    fable = {**_window("Weekly (Fable)", 97, _SEVEN_D), "resets_at": None}
    only_fable_7d = _harness_card(
        "Claude Code",
        [_window("Session", 5, _FIVE_H), fable],
        account="a@example.com",
    )
    with _budget():
        sonnet = _refusal([only_fable_7d])
        assert sonnet is not None and "no current 7-day" in sonnet, (
            "Fable's window must not stand in for the shared 7-day reading"
        )
        both = _both(5, 5, extra=[fable])
        assert _refusal([both], model=_SONNET) is None, "Fable usage is not Sonnet's"
        fable_seat = _refusal([both], model="claude:claude-fable-5-1")
        assert fable_seat is not None and "(model tier) at 97%" in fable_seat, (
            fable_seat
        )


def test_budget_wording_redacts_the_account_and_never_claims_provider_quota():
    secret = "invntrm@gmail.com"
    with _budget():
        for cards in (
            [_both(85, 1, account=secret)],
            [_both(1, 1, account="other@x.y")],
        ):
            held = _refusal(cards, account=secret)
            assert held.startswith(_BLOCKED), held
            assert secret not in held and "invntrm" not in held, held
            assert "i***@gmail.com" in held and "review-cli policy hold" in held, held
            lowered = held.lower()
            for claim in (
                "quota exhausted",
                "exhausted",
                "session limit",
                "usage limit",
            ):
                assert claim not in lowered, (claim, held)
    assert account_budget.redact_account(None) == "the default account"
    assert account_budget.redact_account("/home/u/.claude-accounts/work") == "w***"
    assert account_budget.redact_account("Alex@Example.com") == "A***@Example.com"


def test_budget_settings_are_validated_and_invalid_ones_hold_everything():
    bad = {"max_usage_percent": 0, "strict": "yes", "providers": {"gemini": 50}, "x": 1}
    problems = account_budget.resolve(bad).problems
    assert len(problems) == 4, problems
    with _budget(bad):
        held = _refusal([_both(1, 1)])
        assert held is not None and "invalid account_budget settings" in held, held
    with _budget({"max_usage_percent": 101}):
        assert "above 0 and at most 100" in _refusal([_both(1, 1)])
    assert account_budget.resolve({"enabled": False}).enabled is False


def test_budget_gates_allow_spawn_for_model_too_but_only_at_dispatch():
    # `allow_spawn_for_model` is the dispatch gate: held at 80, allowed at 79.99, and a
    # missing source fails closed; board composition reads usage_percent_for_model, which
    # still fails open (an unknown account is "not near its limit", never excluded).
    with _budget():
        assert allow_spawn_for_model(
            _SONNET, cards=[_both(79.99, 79.99)], account="a@example.com"
        )
        assert not allow_spawn_for_model(
            _SONNET, cards=[_both(80.0, 1)], account="a@example.com"
        )
        assert not allow_spawn_for_model(_SONNET, cards=[], account="a@example.com")
        assert usage_percent_for_model(_SONNET, cards=[]) is None


def test_budget_lenient_keeps_the_legacy_gate_when_the_harness_read_failed():
    def failed():
        raise RuntimeError("boom")

    exhausted = [_sample("claude", "5-hour", 100.0)]
    with _budget({"strict": False}), _env(REVIEW_HARNESS_LIMITS_JSON=None):
        none = spawn_refuse_reason(_SONNET, runner=failed, samples=[], now=NOW)
        assert none is None, "lenient: no usable data lets the dispatch through"
        legacy = spawn_refuse_reason(_SONNET, runner=failed, samples=exhausted, now=NOW)
        assert legacy == "harness 5-hour window exhausted (100%)", legacy


_FAKE_CLAUDE = """#!/bin/sh
echo "$CLAUDE_CONFIG_DIR" >> "$FAKE_CLAUDE_LOG"
cat > /dev/null
echo "## fake [ok]"
echo "the claude CLI answered"
"""


@contextlib.contextmanager
def _claude_world(cards, accounts=()):
    """A temp HOME with a fake `claude` on PATH (it logs every spawn), an optional
    ~/.claude-accounts registry (`accounts`: names; the email is `<name>@example.com`),
    the harness cards as the live limits source and private cooldown/log state."""
    with tempfile.TemporaryDirectory(prefix="budget-696-") as tmp:
        root = Path(tmp)
        (root / "bin").mkdir()
        claude = root / "bin" / "claude"
        claude.write_text(_FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        registry = root / "accounts"
        registry.mkdir()
        for name in accounts:
            account_dir = registry / f"account-{name}"
            account_dir.mkdir()
            (account_dir / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": f"{name}@example.com"}}),
                encoding="utf-8",
            )
        if accounts:
            (registry / "current").write_text(
                f"account-{accounts[0]}\n", encoding="utf-8"
            )
        spawn_log = root / "spawns.log"
        env = {
            "HOME": str(root),
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "FAKE_CLAUDE_LOG": str(spawn_log),
            "REVIEW_CLAUDE_MODE": "cli",
            "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(registry),
            "REVIEW_HARNESS_LIMITS_JSON": json.dumps({"cards": cards}),
            "REVIEW_USAGE_LIMITS_FILE": str(root / "missing-usage.json"),
            "REVIEW_LOG_DIR": str(root / "logs"),
            "REVIEW_STATS_FILE": str(root / "stats.jsonl"),
            "REVIEW_SEAT_COOLDOWN_FILE": str(root / "cooldown.json"),
            "REVIEW_ADMISSION": "0",
        }
        try:
            with _env(**env):
                yield root, spawn_log
        finally:
            # the temp dir goes away with this block: keep what the fake claude logged
            _SPAWN_SNAPSHOTS[spawn_log] = _read_spawns(spawn_log)


_SPAWN_SNAPSHOTS: dict[Path, list[str]] = {}


def _read_spawns(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _spawns(log: Path) -> list[str]:
    """What the fake claude logged: live while the world exists, then the snapshot."""
    return _read_spawns(log) if log.exists() else _SPAWN_SNAPSHOTS.get(log, [])


def test_budget_images_transport_runs_on_the_current_registered_account():
    # two accounts report usage: the images path names the registry's current one instead
    # of failing closed as "not tied to one of them"
    cards = [
        _both(10, 10, account="one@example.com"),
        _both(95, 95, account="two@example.com"),
    ]
    with _claude_world(cards, accounts=("one", "two")) as (root, spawn_log), _budget():
        shot = root / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        result = backends.review_with_images(
            _SONNET, "prompt", "diff", root, 60, images=(shot,)
        )
        spawned = _spawns(spawn_log)
    assert "the claude CLI answered" in result.stdout, result.stdout
    assert [Path(p).name for p in spawned] == ["account-one"], spawned


def test_budget_explicit_off_wins_over_other_invalid_keys():
    account_budget.configure({"enabled": False, "max_usage_percent": 0})
    try:
        assert account_budget.current() is None
    finally:
        account_budget.unconfigure()


def test_budget_held_claude_cli_is_not_spawned_and_records_no_cooldown():
    secret = "invntrm@example.com"
    cards = [_both(85, 10, account=secret)]
    with _claude_world(cards, accounts=("invntrm",)) as (root, spawn_log), _budget():
        result = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
        again = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
        assert not (root / "cooldown.json").exists() or _SONNET not in (
            root / "cooldown.json"
        ).read_text(encoding="utf-8"), "a policy hold must not become a cooldown"
    assert _spawns(spawn_log) == [], "a held seat must not start the claude process"
    for outcome in (result, again):
        assert outcome.returncode == 0
        assert _BLOCKED in outcome.stdout and "5-hour window at 85%" in outcome.stdout
        assert "i***@example.com" in outcome.stdout, outcome.stdout
        assert secret not in outcome.stdout + outcome.command + outcome.stderr
        assert outcome.command.endswith(": account budget hold"), outcome.command


def test_budget_allows_79_99_and_spawns_the_cli_for_the_clear_account():
    cards = [_both(79.99, 20, account="work@example.com")]
    with _claude_world(cards, accounts=("work",)) as (root, spawn_log), _budget():
        result = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
    assert "the claude CLI answered" in result.stdout, result.stdout
    assert len(_spawns(spawn_log)) == 1


def test_budget_pool_swap_dispatches_on_the_account_under_the_ceiling():
    cards = [
        _both(85, 85, account="one@example.com"),
        _both(30, 40, account="two@example.com"),
    ]
    with _claude_world(cards, accounts=("one", "two")) as (root, spawn_log), _budget():
        result = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
        spawned = _spawns(spawn_log)
    assert "the claude CLI answered" in result.stdout, result.stdout
    assert [Path(p).name for p in spawned] == ["account-two"], spawned


def test_budget_blocks_every_account_when_each_is_over_or_unknown():
    cards = [_both(85, 85, account="one@example.com")]  # two@ has no card at all
    with _claude_world(cards, accounts=("one", "two")) as (root, spawn_log), _budget():
        result = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
    assert _spawns(spawn_log) == []
    assert _BLOCKED in result.stdout and "all 2 configured accounts" in result.stdout
    assert (
        "one@example.com" not in result.stdout
        and "two@example.com" not in result.stdout
    )


def test_budget_missing_harness_cards_hold_the_cli_seat_but_not_the_api_transport():
    with _claude_world([], accounts=()) as (root, spawn_log), _budget():
        held = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
        assert _spawns(spawn_log) == []
        assert "harness reports no Claude usage card" in held.stdout, held.stdout
    # the paid Anthropic API account is not the subscription the budget governs
    with _claude_world([], accounts=()) as (root, _spawn_log), _budget():
        calls = []

        def fake_api(*_a, **_k):
            calls.append(1)
            return backends.ReviewResult(
                model=_SONNET, command="api", returncode=0, stdout="API ran", stderr=""
            )

        saved_api, saved_unpaid = (
            backends.review_claude_api,
            backends.unpaid_provider_result,
        )
        backends.review_claude_api = fake_api
        backends.unpaid_provider_result = lambda *_a, **_k: None
        try:
            with _env(REVIEW_CLAUDE_MODE="api"):
                api = backends.review_claude(_SONNET, "prompt", "diff", root, 60)
        finally:
            backends.review_claude_api = saved_api
            backends.unpaid_provider_result = saved_unpaid
    assert calls == [1] and api.stdout == "API ran"


def test_budget_holds_the_images_transport_and_a_promoted_reserve_seat_too():
    cards = [_both(90, 10, account="a@example.com")]
    with _claude_world(cards, accounts=("a",)) as (root, spawn_log), _budget():
        shot = root / "shot.png"
        shot.write_bytes(b"\x89PNG")
        with_image = backends.review_with_images(
            _SONNET, "prompt", "diff", root, 60, images=(shot,)
        )
        # failover/board promotion dispatches a reserve seat through call_backend
        promoted = backends.call_backend(
            backends.resolve_backend(_SONNET), _SONNET, "prompt", "diff", root, 60
        )
    assert _spawns(spawn_log) == []
    for outcome in (with_image, promoted):
        assert _BLOCKED in outcome.stdout, outcome.stdout


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
