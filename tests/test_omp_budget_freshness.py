"""Strict OMP budgets must use the provider observation time, not read time."""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reviewlib import account_budget, omp_accounts  # noqa: E402

NOW = 2_000_000_000.0
ACCOUNT = omp_accounts.OmpAccount(
    credential_id=1, position=1, email="fixture@example.test", blocks=()
)


def _report(observed, windows=("5h", "7d")):
    report = {
        "provider": "anthropic",
        "metadata": {"email": ACCOUNT.email, "accountId": "fixture-id"},
        "limits": [
            {
                "id": f"anthropic:{name}",
                "label": "Claude 5 Hour" if name == "5h" else "Claude 7 Day",
                "scope": {"shared": True, "windowId": name},
                "window": {"id": name, "resetsAt": (NOW + 5000) * 1000},
                "amount": {"usedFraction": 0.2},
            }
            for name in windows
        ],
    }
    if observed is not None:
        report["fetchedAt"] = observed * 1000
    return report


def _decision(reports):
    completed = subprocess.CompletedProcess([], 0, json.dumps({"reports": reports}), "")
    with patch.object(omp_accounts.subprocess, "run", return_value=completed):
        usage = omp_accounts.usage_by_email(SimpleNamespace(token_bin="fixture-omp"))
    policy = omp_accounts.account_policy(
        None, {}, budget=account_budget.AccountBudget(max_usage_percent=80)
    )
    return policy.skip_reason(
        ACCOUNT,
        omp_accounts.usage_of(usage, ACCOUNT),
        "anthropic/sonnet",
        int(NOW * 1000),
    )


def test_fresh_same_account_pair_is_admitted():
    assert _decision([_report(NOW - 5)]) is None


def test_stale_provider_observation_is_not_refreshed_by_reading_it():
    reason = _decision([_report(NOW - 3600)])
    assert account_budget.is_budget_hold(reason), reason
    assert "stale" in reason.lower(), reason


def test_missing_provider_observation_time_is_unknown():
    reason = _decision([_report(None)])
    assert account_budget.is_budget_hold(reason), reason


def test_future_provider_observation_is_not_fresh():
    reason = _decision([_report(NOW + 3600)])
    assert account_budget.is_budget_hold(reason), reason


def test_partial_reports_do_not_supply_each_others_shared_window():
    reason = _decision([_report(NOW - 5, ("5h",)), _report(NOW - 10, ("7d",))])
    assert account_budget.is_budget_hold(reason), reason


if __name__ == "__main__":
    failed = 0
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except AssertionError as error:
                failed += 1
                print(f"FAIL {name}: {error}")
    raise SystemExit(bool(failed))
