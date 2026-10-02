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
    # commandcode's Kimi seat deliberately stays unmapped (it shares the commandcode
    # gateway account, not kimi's own account -- see usage_limits._AGENT_FAMILY_PREFIXES).
    samples = [_sample("claude", "weekly", 99)]
    assert (
        usage_percent_for_model(
            "commandcode:moonshotai/Kimi-K2.7-Code", samples=samples, now=NOW
        )
        is None
    )
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


def test_zai_prefixes_map_to_zai_family():
    samples = [_sample("zai", "weekly", 73)]
    assert usage_percent_for_model("zai:glm-5.2", samples=samples, now=NOW) == 73
    assert usage_percent_for_model("oc:zai/glm-5.2", samples=samples, now=NOW) == 73
    assert usage_percent_for_model("glm:glm-4.7", samples=samples, now=NOW) == 73


def test_xai_oc_prefix_maps_to_xai_family():
    samples = [_sample("xai", "5-hour", 61)]
    assert usage_percent_for_model("oc:xai/grok-4.6", samples=samples, now=NOW) == 61


def test_kimi_code_omp_prefix_maps_to_kimi_family():
    samples = [_sample("kimi", "weekly", 88)]
    assert usage_percent_for_model("omp:kimi-code/k3", samples=samples, now=NOW) == 88
    # A different omp provider selector does not pick up the kimi family.
    assert (
        usage_percent_for_model("omp:openai/gpt-5.5", samples=samples, now=NOW) is None
    )


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
    assert usage_percent_for_model("claude:claude-opus-4-8", cards=cards) == 100
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
    unknown = [_harness_card("Claude Code", [_window("Session", None, 18000)])]
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
    assert usage_percent_for_model("claude:claude-opus-4-8", cards=cards) == 100
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


def test_backend_unavailable_reason_skips_exhausted_zai_seat():
    """review-cli#skip-dblock-color: `backend_unavailable_reason` now layers
    `spawn_refuse_reason` on top of EVERY backend's own auth/CLI probe, not just
    claude's dispatch -- so a GLM seat whose z.ai account is already 100% exhausted
    is reported unavailable pre-dispatch instead of being tried and failing slowly."""
    from reviewlib import backends

    cards = [_harness_card("z.ai", [_window("Weekly", 100.0, 7 * 86400)])]
    saved_json = os.environ.get("REVIEW_HARNESS_LIMITS_JSON")
    saved_key = os.environ.get("ZAI_API_KEY")
    saved_env_file = os.environ.get("GEMINI_ENV_FILE")
    saved_unpaid = backends._CONFIG_UNPAID_PROVIDERS
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = json.dumps({"cards": cards})
    os.environ["ZAI_API_KEY"] = "test-key-live"
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    backends.configure_unpaid_providers(None)
    try:
        reason = backends.backend_unavailable_reason("zai:glm-5.2")
        assert reason is not None, "exhausted z.ai weekly window still reported live"
        assert "weekly" in reason and "exhausted" in reason, reason
        assert backends.backend_available("zai:glm-5.2") is False
    finally:
        for key, saved in (
            ("REVIEW_HARNESS_LIMITS_JSON", saved_json),
            ("ZAI_API_KEY", saved_key),
            ("GEMINI_ENV_FILE", saved_env_file),
        ):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        backends.configure_unpaid_providers(saved_unpaid)
    # With the key present and NO exhausted card, the same seat is live.
    os.environ["ZAI_API_KEY"] = "test-key-live"
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ["REVIEW_HARNESS_LIMITS_JSON"] = json.dumps({"cards": []})
    backends.configure_unpaid_providers(None)
    try:
        assert backends.backend_unavailable_reason("zai:glm-5.2") is None
    finally:
        for key, saved in (
            ("REVIEW_HARNESS_LIMITS_JSON", saved_json),
            ("ZAI_API_KEY", saved_key),
            ("GEMINI_ENV_FILE", saved_env_file),
        ):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        backends.configure_unpaid_providers(saved_unpaid)


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
        return usage_percent_for_model(model, samples=samples, cards=cards, now=NOW)

    kept = expand_flat_models_with_reuse(roster, 2, usage_percent=samples_only)
    assert "claude:claude-opus-4-8" in kept

    excluded = expand_flat_models_with_reuse(roster, 2, usage_percent=with_cards)
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
