"""omp:anthropic seats take the free account with the LOWEST usage first, not omp's list
order (review-cli#709): spend spreads to the emptiest account and rotates as readings move.
Pure ordering tests plus an end-to-end dispatch with a fake omp and fake `omp usage`."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from reviewlib import omp_accounts  # noqa: E402
from test_omp_account_pool import _MODEL, _SELECTOR, _world  # noqa: E402

NOW_MS = 1_000_000_000_000
LATER_MS = NOW_MS + 3_600_000
EARLIER_MS = NOW_MS - 60_000


def acct(position, email, blocks=()):
    return omp_accounts.OmpAccount(position + 10, position, email, tuple(blocks))


def windows(five, seven, *, resets=LATER_MS, fable=None):
    out = [
        omp_accounts.UsageWindow("Claude 5 Hour", five / 100, resets),
        omp_accounts.UsageWindow("Claude 7 Day", seven / 100, resets),
    ]
    if fable is not None:
        out.append(
            omp_accounts.UsageWindow(
                "Claude 7 Day (Fable)", fable / 100, resets, "fable"
            )
        )
    return out


def order(accounts, usage, model=_SELECTOR, limited=None):
    ordered = omp_accounts.dispatch_order(
        accounts, model, limited or {}, usage=usage, now_ms=NOW_MS
    )
    return [a.email for a in ordered]


def test_lowest_usage_account_goes_first_not_the_first_in_omp_order():
    accounts = [acct(1, "a@x"), acct(2, "b@x"), acct(3, "c@x")]
    usage = {"a@x": windows(60, 50), "b@x": windows(5, 29), "c@x": windows(30, 10)}
    assert order(accounts, usage) == ["b@x", "c@x", "a@x"]


def test_account_ranks_by_its_highest_window():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    usage = {"a@x": windows(5, 79), "b@x": windows(40, 40)}
    assert order(accounts, usage) == ["b@x", "a@x"]


def test_rotation_follows_the_readings():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    assert order(accounts, {"a@x": windows(10, 10), "b@x": windows(20, 20)}) == [
        "a@x",
        "b@x",
    ]
    assert order(accounts, {"a@x": windows(30, 10), "b@x": windows(20, 20)}) == [
        "b@x",
        "a@x",
    ]


def test_tie_keeps_omp_order():
    accounts = [acct(1, "a@x"), acct(2, "b@x"), acct(3, "c@x")]
    usage = {e: windows(10, 10) for e in ("a@x", "b@x", "c@x")}
    assert order(accounts, usage) == ["a@x", "b@x", "c@x"]


def test_unknown_usage_sorts_after_known_and_keeps_omp_order_among_unknown():
    accounts = [acct(1, "a@x"), acct(2, "b@x"), acct(3, "c@x"), acct(4, None)]
    usage = {"b@x": windows(90, 90)}
    assert order(accounts, usage) == ["b@x", "a@x", "c@x", None]


def test_stale_windows_that_already_reset_are_not_a_reading():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    usage = {"a@x": windows(95, 95, resets=EARLIER_MS), "b@x": windows(50, 50)}
    assert order(accounts, usage) == ["b@x", "a@x"]  # a has no live reading -> unknown


def test_a_reset_window_does_not_count_but_a_live_one_still_does():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    stale_hot = [
        omp_accounts.UsageWindow("Claude 5 Hour", 0.99, EARLIER_MS),
        omp_accounts.UsageWindow("Claude 7 Day", 0.20, LATER_MS),
    ]
    assert order(accounts, {"a@x": stale_hot, "b@x": windows(30, 30)}) == ["a@x", "b@x"]


def test_tier_window_counts_only_for_that_tiers_models():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    usage = {"a@x": windows(10, 10, fable=70), "b@x": windows(30, 30, fable=1)}
    assert order(accounts, usage, model="claude-sonnet-5-5") == ["a@x", "b@x"]
    assert order(accounts, usage, model="claude-fable-5-1") == ["b@x", "a@x"]


def test_blocked_and_limited_accounts_stay_last_whatever_their_usage():
    accounts = [
        acct(1, "a@x", blocks=[("", 9_000_000_000_000)]),
        acct(2, "b@x"),
        acct(3, "c@x"),
    ]
    usage = {"a@x": windows(1, 1), "b@x": windows(50, 50), "c@x": windows(60, 60)}
    assert order(accounts, usage) == ["b@x", "c@x", "a@x"]
    limited = {accounts[1].credential_id: 5_000.0}
    assert order(accounts, usage, limited=limited) == ["c@x", "b@x", "a@x"]


def test_without_a_usage_map_the_order_is_omps_order_as_before():
    accounts = [acct(1, "a@x"), acct(2, "b@x")]
    assert order(accounts, None) == ["a@x", "b@x"]


def _usage_json(**pairs):
    resets = int(time.time() * 1000) + 3_600_000
    return json.dumps(
        {
            "reports": [
                {
                    "provider": "anthropic",
                    "metadata": {"email": f"{name}@example.com"},
                    "fetchedAt": int(time.time() * 1000),
                    "limits": [
                        {
                            "label": label,
                            "amount": {"usedFraction": fraction},
                            "window": {"resetsAt": resets},
                        }
                        for label, fraction in (
                            ("Claude 5 Hour", five),
                            ("Claude 7 Day", seven),
                        )
                    ],
                }
                for name, (five, seven) in pairs.items()
            ]
        }
    )


def _first_token(world, usage, **extra_env):
    ask = world.review(
        "just-ask",
        "Say hi",
        "--task",
        "709",
        "-m",
        _MODEL,
        "--timeout",
        "60",
        FAKE_OMP_USAGE=usage,
        **extra_env,
    )
    assert ask.returncode == 0, ask.stdout + ask.stderr
    runs = [c for c in world.calls() if c["kind"] == "run"]
    return runs[0]["token"]


def test_dispatch_uses_the_emptiest_account_even_without_any_policy():
    with _world() as world:
        assert _first_token(world, _usage_json(a=(0.6, 0.5), b=(0.1, 0.2))) == "tok-2"


def test_dispatch_rotates_to_the_other_account_when_readings_flip():
    with _world() as world:
        assert _first_token(world, _usage_json(a=(0.1, 0.2), b=(0.6, 0.5))) == "tok-1"


def test_explicit_disabled_cap_without_usage_keeps_omps_order():
    with _world() as world:
        assert (
            _first_token(world, "not json", REVIEW_ACCOUNT_USAGE_CAP_PERCENT="off")
            == "tok-1"
        )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
