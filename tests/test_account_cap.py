"""The one account usage cap (review-cli#696): `account_usage_cap_percent` in config.yaml,
default 80, applied to every route that spends a subscription-backed Claude account --
omp's account pool AND the direct `claude` CLI seat. Pure policy tests (no omp, no
harness, no clock); the dispatch wiring is covered at the bottom with fake readings."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from reviewlib import account_cap, backends, omp_accounts  # noqa: E402

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc).timestamp()
FIVE_RESET = datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc).timestamp()
SEVEN_RESET = datetime(2026, 10, 30, 22, 59, tzinfo=timezone.utc).timestamp()
MODEL = "claude:claude-sonnet-5-5"


def windows(five, seven, *, live=False):
    """omp usage windows; `live` resets them in the future of the REAL clock (wiring tests
    run through backends, which reads time.time())."""
    five_reset = int((time.time() + 3600) * 1000) if live else int(FIVE_RESET * 1000)
    seven_reset = int((time.time() + 86400) * 1000) if live else int(SEVEN_RESET * 1000)
    return (
        omp_accounts.UsageWindow("Claude 5 Hour", five / 100, five_reset),
        omp_accounts.UsageWindow("Claude 7 Day", seven / 100, seven_reset),
    )


def refusal(cap, email="a@x", *, omp=None, harness=None, model=MODEL):
    return account_cap.claude_refusal(
        model,
        email,
        cap_percent=cap,
        omp_windows=omp,
        harness_percent=harness,
        now=NOW,
        tz=timezone.utc,
    )


# ---- configuration -----------------------------------------------------------------------


def test_default_cap_is_80_percent():
    cap = account_cap.resolve(None, {})
    assert cap.percent == 80 and cap.problems == () and "default" in cap.source


def test_config_value_replaces_the_default():
    cap = account_cap.resolve(65, {})
    assert cap.percent == 65 and "account_usage_cap_percent" in cap.source


def test_env_overrides_config_and_off_disables_it_visibly():
    assert account_cap.resolve(65, {account_cap.ENV: "50"}).percent == 50
    off = account_cap.resolve(65, {account_cap.ENV: "off"})
    assert off.percent is None and off.disabled and not off.problems


@pytest.mark.parametrize("bad", [0, -5, 101, "eighty", True, [80]])
def test_invalid_values_are_a_problem_not_silently_ignored(bad):
    cap = account_cap.resolve(bad, {})
    assert cap.problems and "account_usage_cap_percent" in cap.problems[0]


def test_a_percent_sign_string_is_accepted():
    assert account_cap.resolve("80%", {}).percent == 80


def test_unconfigured_cap_enforces_nothing():
    assert account_cap.current().percent is None
    account_cap.configure(80, {})
    try:
        assert account_cap.current().percent == 80
    finally:
        account_cap.configure_unset()
    assert account_cap.current().percent is None


# ---- the direct claude seat: refusal wording and boundaries ------------------------------


def test_below_the_cap_is_not_refused():
    assert refusal(80, omp=windows(30, 40)) is None


@pytest.mark.parametrize(
    "five,expected_refused", [(79.99, False), (80.0, True), (81.0, True)]
)
def test_cap_boundary_79_99_80_81(five, expected_refused):
    got = refusal(80, omp=windows(five, 10))
    assert (got is not None) is expected_refused


def test_refusal_names_the_rule_key_account_percent_window_and_reset():
    text = refusal(80, "mextner@gmail.com", omp=windows(83, 10))
    assert text == (
        "ограничение review-cli (config account_usage_cap_percent=80): "
        "аккаунт mextner@gmail.com использован на 83% окна 5ч, сброс в 03:30"
    )


def test_seven_day_window_over_the_cap_is_worded_as_weekly():
    text = refusal(80, omp=windows(10, 91))
    assert "91% окна 7д, сброс в 22:59" in text


def test_harness_percent_alone_refuses_without_naming_a_window():
    text = refusal(80, harness=85.0)
    assert "85%" in text and "ограничение review-cli" in text
    assert "наибольшего из окон 5ч/7д" in text


def test_either_source_over_the_cap_refuses():
    assert refusal(80, omp=windows(10, 10), harness=90.0) is not None
    assert refusal(80, omp=windows(90, 10), harness=10.0) is not None


def test_both_sources_below_the_cap_is_fine():
    assert refusal(80, omp=windows(10, 10), harness=40.0) is None


def test_no_reading_at_all_is_refused_conservatively_and_says_so():
    text = refusal(80, "a@x")
    assert "usage неизвестен" in text and "консервативно" in text and "a@x" in text
    assert "account_usage_cap_percent" in text


def test_incomplete_omp_pair_without_a_harness_reading_is_unknown():
    only_five = (
        omp_accounts.UsageWindow("Claude 5 Hour", 0.1, int(FIVE_RESET * 1000)),
    )
    assert "usage неизвестен" in refusal(80, omp=only_five)


def test_a_reset_window_is_not_a_reading():
    stale = (
        omp_accounts.UsageWindow("Claude 5 Hour", 0.95, int((NOW - 60) * 1000)),
        omp_accounts.UsageWindow("Claude 7 Day", 0.10, int(SEVEN_RESET * 1000)),
    )
    assert "usage неизвестен" in refusal(80, omp=stale)


def test_harness_reading_covers_for_an_incomplete_omp_pair():
    only_five = (
        omp_accounts.UsageWindow("Claude 5 Hour", 0.1, int(FIVE_RESET * 1000)),
    )
    assert refusal(80, omp=only_five, harness=30.0) is None


def test_no_cap_means_no_refusal_even_without_a_reading():
    assert refusal(None) is None


def test_the_message_never_calls_the_cap_a_provider_limit():
    for text in (refusal(80, omp=windows(95, 10)), refusal(80)):
        for banned in ("limits exhausted", "лимиты исчерпаны", "quota", "policy hold"):
            assert banned not in text


def test_the_message_carries_no_secret():
    text = refusal(80, "a@x", omp=windows(95, 10))
    assert "token" not in text.lower() and "secret" not in text.lower()


# ---- wiring: the omp pool and the direct claude CLI gate use the same cap -----------------


def test_omp_policy_inherits_the_cap_when_it_sets_no_threshold():
    account_cap.configure(80, {})
    try:
        policy = backends._omp_account_policy()
    finally:
        account_cap.configure_unset()
    assert policy.max_usage_percent == 80
    assert "account_usage_cap_percent" in policy.max_usage_source


def test_omp_policy_own_threshold_wins_over_the_shared_cap(monkeypatch):
    monkeypatch.setattr(backends, "_CONFIG_OMP_ACCOUNTS", {"max_usage_percent": 60})
    account_cap.configure(80, {})
    try:
        policy = backends._omp_account_policy()
    finally:
        account_cap.configure_unset()
    assert policy.max_usage_percent == 60


def test_direct_claude_gate_refuses_before_spawning(monkeypatch):
    account_cap.configure(80, {})
    spawned = []
    monkeypatch.setattr(
        backends, "review_claude_cli", lambda *a, **k: spawned.append(a)
    )
    monkeypatch.setattr(backends, "_claude_account_candidates", lambda: [(None, "a@x")])
    monkeypatch.setattr(backends, "allow_spawn_for_model", lambda *a, **k: True)
    monkeypatch.setattr(
        backends,
        "_claude_usage_readings",
        lambda model, email: (windows(85, 10, live=True), None),
    )
    try:
        result = backends._review_claude_cli_with_account_fallback(
            MODEL, "p", "d", ".", 30, 1, effort=None
        )
    finally:
        account_cap.configure_unset()
    assert spawned == [], "a refused account must not be spawned"
    assert (
        "ограничение review-cli (config account_usage_cap_percent=80)" in result.stdout
    )
    assert "аккаунт a@x использован на 85% окна 5ч" in result.stdout


def test_direct_claude_gate_spawns_when_below_the_cap(monkeypatch):
    account_cap.configure(80, {})
    seen = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""
        model = MODEL
        command = "claude"

    monkeypatch.setattr(
        backends, "review_claude_cli", lambda *a, **k: seen.append(a) or _Result()
    )
    monkeypatch.setattr(backends, "_claude_account_candidates", lambda: [(None, "a@x")])
    monkeypatch.setattr(backends, "allow_spawn_for_model", lambda *a, **k: True)
    monkeypatch.setattr(backends, "_account_should_advance", lambda r: False)
    monkeypatch.setattr(backends, "_remember_claude_login", lambda *a, **k: None)
    monkeypatch.setattr(
        backends,
        "_claude_usage_readings",
        lambda model, email: (windows(30, 10, live=True), None),
    )
    try:
        backends._review_claude_cli_with_account_fallback(
            MODEL, "p", "d", ".", 30, 1, effort=None
        )
    finally:
        account_cap.configure_unset()
    assert len(seen) == 1


def test_direct_claude_gate_rotates_to_the_next_account_when_the_first_is_over_the_cap(
    monkeypatch,
):
    account_cap.configure(80, {})
    seen = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""
        model = MODEL
        command = "claude"

    monkeypatch.setattr(
        backends,
        "review_claude_cli",
        lambda *a, config_dir=None, **k: seen.append(config_dir) or _Result(),
    )
    monkeypatch.setattr(
        backends,
        "_claude_account_candidates",
        lambda: [("/d/a", "a@x"), ("/d/b", "b@x")],
    )
    monkeypatch.setattr(backends, "allow_spawn_for_model", lambda *a, **k: True)
    monkeypatch.setattr(backends, "_account_should_advance", lambda r: False)
    monkeypatch.setattr(backends, "_remember_claude_login", lambda *a, **k: None)
    monkeypatch.setattr(
        backends,
        "_claude_usage_readings",
        lambda model, email: (
            (windows(90, 10, live=True), None)
            if email == "a@x"
            else (windows(10, 10, live=True), None)
        ),
    )
    try:
        backends._review_claude_cli_with_account_fallback(
            MODEL, "p", "d", ".", 30, 1, effort=None
        )
    finally:
        account_cap.configure_unset()
    assert seen == ["/d/b"]


def test_unconfigured_library_use_is_unchanged(monkeypatch):
    account_cap.configure_unset()
    seen = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""
        model = MODEL
        command = "claude"

    monkeypatch.setattr(
        backends, "review_claude_cli", lambda *a, **k: seen.append(a) or _Result()
    )
    monkeypatch.setattr(backends, "_claude_account_candidates", lambda: [(None, None)])
    monkeypatch.setattr(backends, "allow_spawn_for_model", lambda *a, **k: True)
    monkeypatch.setattr(backends, "_account_should_advance", lambda r: False)
    monkeypatch.setattr(backends, "_remember_claude_login", lambda *a, **k: None)
    backends._review_claude_cli_with_account_fallback(
        MODEL, "p", "d", ".", 30, 1, effort=None
    )
    assert len(seen) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
