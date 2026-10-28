"""Strict account admission: provider observations, not a fresh wrapper timestamp."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reviewlib import account_cap, backends, omp_accounts  # noqa: E402

NOW = 2_000_000_000.0
NOW_MS = int(NOW * 1000)
EMAIL = "qa@example.invalid"
MODEL = "claude:claude-sonnet-5-5"


def report(*, age_ms=0, five=10.0, seven=20.0, email=EMAIL, shared=True):
    limits = []
    for label, percent in (("Claude 5 Hour", five), ("Claude 7 Day", seven)):
        if percent is not None:
            limits.append(
                {
                    "label": label,
                    "amount": {"usedFraction": percent / 100},
                    "window": {"resetsAt": NOW_MS + 86_400_000},
                    "scope": {"shared": shared},
                }
            )
    result = {"provider": "anthropic", "metadata": {"email": email}, "limits": limits}
    if age_ms is not None:
        result["fetchedAt"] = NOW_MS - age_ms
    return result


def parsed(monkeypatch, reports):
    response = SimpleNamespace(returncode=0, stdout=json.dumps({"reports": reports}))
    monkeypatch.setattr(omp_accounts.subprocess, "run", lambda *a, **k: response)
    return omp_accounts.usage_by_email(SimpleNamespace(token_bin="never-spawned"))


def direct(windows, harness=None):
    return account_cap.claude_refusal(
        MODEL,
        EMAIL,
        cap_percent=80,
        omp_windows=windows,
        harness_percent=harness,
        now=NOW,
    )


def pooled(windows):
    policy = omp_accounts.account_policy({"max_usage_percent": 80}, {})
    return policy.skip_reason(
        omp_accounts.OmpAccount(1, 1, EMAIL),
        windows,
        "anthropic/claude-sonnet-5-5",
        NOW_MS,
    )


@pytest.mark.parametrize("age_ms", [86_400_000, -60_000, None])
def test_source_age_is_not_replaced_with_the_current_command_time(monkeypatch, age_ms):
    windows = parsed(monkeypatch, [report(age_ms=age_ms)])[EMAIL]
    assert direct(windows) is not None
    assert pooled(windows) is not None


@pytest.mark.parametrize(
    "five,seven,blocked",
    [
        (79.99, 20, False),
        (80, 20, True),
        (20, 80, True),
        (20, 79.99, False),
        (100, 20, True),
    ],
)
def test_fresh_complete_pair_keeps_exact_boundary(monkeypatch, five, seven, blocked):
    windows = parsed(monkeypatch, [report(five=five, seven=seven)])[EMAIL]
    assert (direct(windows) is not None) is blocked
    assert (pooled(windows) is not None) is blocked


def test_two_partial_reports_cannot_manufacture_a_complete_observation(monkeypatch):
    windows = parsed(monkeypatch, [report(seven=None), report(five=None)])[EMAIL]
    assert direct(windows) is not None
    assert pooled(windows) is not None


def test_newer_incomplete_reading_cannot_borrow_an_older_window(monkeypatch):
    windows = parsed(monkeypatch, [report(age_ms=1000), report(seven=None)])[EMAIL]
    assert direct(windows) is not None
    assert pooled(windows) is not None


def test_missing_usage_never_authorizes_the_omp_pool():
    assert pooled(None) is not None


@pytest.mark.parametrize("harness", [0, 10, float("nan"), -1])
def test_a_scalar_harness_summary_is_not_proof_of_both_windows(harness):
    assert direct(None, harness) is not None


def test_known_over_cap_harness_summary_still_vetoes_a_fresh_pair(monkeypatch):
    windows = parsed(monkeypatch, [report()])[EMAIL]
    assert direct(windows, 90) is not None


def test_non_shared_limits_cannot_substitute_for_shared_windows(monkeypatch):
    windows = parsed(monkeypatch, [report(shared=False)])[EMAIL]
    assert direct(windows) is not None
    assert pooled(windows) is not None


def test_other_account_never_supplies_the_missing_observation(monkeypatch):
    readings = parsed(monkeypatch, [report(email="someone-else@example.invalid")])
    assert direct(readings.get(EMAIL)) is not None
    assert pooled(readings.get(EMAIL)) is not None


def test_refused_stale_observation_never_starts_the_cli(monkeypatch):
    windows = parsed(monkeypatch, [report(age_ms=86_400_000)])[EMAIL]
    started = []
    monkeypatch.setattr(backends.time, "time", lambda: NOW)
    monkeypatch.setattr(backends, "_claude_account_candidates", lambda: [(None, EMAIL)])
    monkeypatch.setattr(backends, "_claude_usage_readings", lambda *a: (windows, None))
    monkeypatch.setattr(backends, "allow_spawn_for_model", lambda *a, **k: True)
    monkeypatch.setattr(
        backends,
        "review_claude_cli",
        lambda *a, **k: (
            started.append(a)
            or SimpleNamespace(
                returncode=0, stdout="ok", stderr="", model=MODEL, command="stub"
            )
        ),
    )
    account_cap.configure(80, {})
    try:
        backends._review_claude_cli_with_account_fallback(
            MODEL, "p", "d", ".", 30, 1, effort=None
        )
    finally:
        account_cap.configure_unset()
    assert not started


def test_pool_snapshot_checks_source_time_after_the_usage_read(monkeypatch):
    from reviewlib import seat_health

    windows = parsed(monkeypatch, [report(age_ms=-2000)])[EMAIL]
    clock = iter([NOW, NOW + 3])
    monkeypatch.setattr(seat_health.time, "time", lambda: next(clock))
    monkeypatch.setattr(backends, "_omp_launcher", lambda: object())
    monkeypatch.setattr(
        backends,
        "_omp_account_policy",
        lambda: omp_accounts.account_policy({"max_usage_percent": 80}, {}),
    )
    monkeypatch.setattr(backends, "_omp_auth_db", lambda: Path("/offline"))
    monkeypatch.setattr(
        omp_accounts,
        "anthropic_accounts",
        lambda *a, **k: [omp_accounts.OmpAccount(1, 1, EMAIL)],
    )
    monkeypatch.setattr(omp_accounts, "usage_by_email", lambda *a: {EMAIL: windows})
    monkeypatch.setattr(
        seat_health.seat_cooldown, "active_cooldown", lambda *a, **k: None
    )
    result = seat_health.omp_pool_state()
    assert result.accounts[0].skip_reason is None


@pytest.mark.parametrize("seat", [MODEL, "omp:anthropic/claude-sonnet-5-5"])
def test_seat_display_does_not_claim_stale_usage_is_available(monkeypatch, seat):
    from reviewlib import seat_accounts, seat_health

    windows = parsed(monkeypatch, [report(age_ms=86_400_000)])[EMAIL]
    account = omp_accounts.OmpAccount(1, 1, EMAIL)
    state = seat_health.OmpAccountState(account, None, None, windows)
    policy = omp_accounts.account_policy({"max_usage_percent": 80}, {})
    pool = seat_health.OmpPoolState(
        None, None, Path("/offline"), [state], policy, {EMAIL: windows}
    )
    monkeypatch.setattr(account_cap, "default_claude_email", lambda: EMAIL)
    account_cap.configure(80, {})
    try:
        view = seat_accounts.seat_view(
            seat, seat_health.SeatHealth(seat, None, None), pool, now=NOW
        )
    finally:
        account_cap.configure_unset()
    assert view.status != seat_accounts.USABLE


def test_unscoped_non_shared_window_is_not_a_global_quota(monkeypatch):
    item = report()
    item["limits"].append(
        {
            "label": "Claude 5 Hour",
            "amount": {"usedFraction": 1.0},
            "window": {"resetsAt": NOW_MS + 3600000},
            "scope": {"shared": False},
        }
    )
    windows = parsed(monkeypatch, [item])[EMAIL]
    assert direct(windows) is None
    assert pooled(windows) is None


@pytest.mark.parametrize("age_ms,blocked", [(1800000, False), (1800001, True)])
def test_observation_age_boundary(monkeypatch, age_ms, blocked):
    windows = parsed(monkeypatch, [report(age_ms=age_ms)])[EMAIL]
    assert (direct(windows) is not None) is blocked
    assert (pooled(windows) is not None) is blocked


@pytest.mark.parametrize("source_time", [0, -1])
def test_non_positive_provider_time_is_not_replaced(monkeypatch, source_time):
    payload = report()
    payload["fetchedAt"] = source_time
    windows = parsed(monkeypatch, [payload])[EMAIL]
    assert direct(windows) is not None
    assert pooled(windows) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
