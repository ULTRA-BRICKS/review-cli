"""`review seats` per-seat account view (review-cli#707): which account a seat would use,
its usage, the cap, the reset and the reason, from a fake omp pool and fake usage."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import omp_accounts, seat_accounts  # noqa: E402
from reviewlib.seat_health import OmpAccountState, OmpPoolState, SeatHealth  # noqa: E402

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc).timestamp()
NOW_MS = int(NOW * 1000)
FIVE_RESET_MS = int(
    datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc).timestamp() * 1000
)
SEVEN_RESET_MS = int(
    datetime(2026, 10, 30, 22, 59, tzinfo=timezone.utc).timestamp() * 1000
)


def windows(five, seven, fable=None):
    out = [
        omp_accounts.UsageWindow(
            "Claude 5 Hour", five / 100, FIVE_RESET_MS, observed_at_ms=NOW_MS
        ),
        omp_accounts.UsageWindow(
            "Claude 7 Day", seven / 100, SEVEN_RESET_MS, observed_at_ms=NOW_MS
        ),
    ]
    if fable is not None:
        out.append(
            omp_accounts.UsageWindow(
                "Claude 7 Day (Fable)", fable / 100, SEVEN_RESET_MS, "fable"
            )
        )
    return out


def state(
    position,
    email,
    five,
    seven,
    *,
    fable=None,
    blocks=(),
    usage="windows",
    limited_until=None,
):
    account = omp_accounts.OmpAccount(
        credential_id=position + 10,
        position=position,
        email=email,
        blocks=tuple(blocks),
    )
    return OmpAccountState(
        account=account,
        limited_until=limited_until,
        limited_reason="429 rate_limit_error" if limited_until else None,
        usage=None if usage is None else windows(five, seven, fable),
    )


def pool(states, *, cap=80.0, per_account=(), exclude=()):
    policy = omp_accounts.AccountPolicy(
        exclude=tuple(exclude),
        max_usage_percent=cap,
        per_account=tuple(per_account),
        exclude_source="config.yaml omp_accounts.exclude",
        max_usage_source="config.yaml omp_accounts.max_usage_percent",
    )
    return OmpPoolState(None, None, Path("/nonexistent/agent.db"), list(states), policy)


def view(model, omp_pool, health=None, **kw):
    health = health or SeatHealth(model, None, None)
    return seat_accounts.seat_view(
        model, health, omp_pool, now=NOW, tz=timezone.utc, **kw
    )


SONNET = "omp:anthropic/claude-sonnet-5-5"


def test_omp_seat_takes_the_lowest_usage_account_below_the_cap():
    v = view(
        SONNET,
        pool(
            [state(1, "a@x", 50, 60), state(2, "b@x", 5, 29), state(3, "c@x", 30, 10)]
        ),
    )
    assert v.status == "usable" and v.account == "b@x"
    assert (v.five_hour_pct, v.seven_day_pct, v.cap_pct) == (5, 29, 80)
    assert v.reason == ""


def test_cap_boundary_79_80_81_on_the_seat():
    assert view(SONNET, pool([state(1, "a@x", 79.0, 10)])).status == "usable"
    assert view(SONNET, pool([state(1, "a@x", 80.0, 10)])).status == "refused"
    assert view(SONNET, pool([state(1, "a@x", 81.0, 10)])).status == "refused"


def test_refusal_names_the_rule_account_percent_reset_and_the_other_account():
    v = view(SONNET, pool([state(1, "a@x", 83, 10), state(2, "b@x", 20, 10)]))
    assert v.status == "usable" and v.account == "b@x"
    assert len(v.refused_accounts) == 1
    text = v.refused_accounts[0]
    assert "ограничение review-cli (config omp_accounts.max_usage_percent=80)" in text
    assert "аккаунт a@x использован на 83% окна 5ч, сброс в 03:30" in text
    assert text.endswith("другой аккаунт доступен: b@x")


def test_all_accounts_over_the_cap_is_a_refused_seat_with_the_closest_account_shown():
    v = view(SONNET, pool([state(1, "a@x", 92, 10), state(2, "b@x", 83, 10)]))
    assert v.status == "refused"
    assert v.account == "b@x" and v.five_hour_pct == 83
    assert v.reason.startswith("все аккаунты выше порога ограничения review-cli")
    assert "аккаунт b@x использован на 83% окна 5ч, сброс в 03:30" in v.reason
    assert "config omp_accounts.max_usage_percent=80" in v.reason
    assert "лимиты исчерпаны" not in v.reason and "limits exhausted" not in v.reason


def test_provider_block_is_worded_as_provider_limit_not_the_cap():
    until_ms = int(
        datetime(2026, 10, 28, 1, 40, tzinfo=timezone.utc).timestamp() * 1000
    )
    v = view(SONNET, pool([state(1, "a@x", 100, 10, blocks=[("", until_ms)])]))
    assert v.status == "refused"
    assert "лимит провайдера" in v.reason and "не ограничение review-cli" in v.reason
    assert "01:40" in v.reason


def test_recorded_review_cli_429_is_a_provider_limit():
    v = view(SONNET, pool([state(1, "a@x", 10, 10, limited_until=NOW + 600)]))
    assert v.status == "refused" and "лимит провайдера" in v.reason


def test_unknown_usage_is_refused_conservatively_and_says_so():
    v = view(SONNET, pool([state(1, "a@x", 0, 0, usage=None)]))
    assert v.status == "refused"
    assert "usage неизвестен" in v.reason and "консервативно" in v.reason
    assert v.five_hour_pct is None


def test_account_without_email_cannot_be_matched_to_usage():
    v = view(SONNET, pool([state(1, None, 10, 10)]))
    assert v.status == "refused" and "usage неизвестен" in v.reason


def test_excluded_account_is_never_chosen_even_with_the_lowest_usage():
    v = view(
        SONNET, pool([state(1, "a@x", 1, 1), state(2, "b@x", 50, 50)], exclude=["a@x"])
    )
    assert v.account == "b@x"


def test_per_account_cap_overrides_the_global_cap_and_names_its_key():
    p = pool([state(1, "a@x", 70, 10)], per_account=[("a@x", 60.0)])
    v = view(SONNET, p)
    assert v.status == "refused" and v.cap_pct == 60
    assert "config omp_accounts.accounts.a@x.max_usage_percent=60" in v.reason


def test_fable_tier_window_counts_only_for_fable_models():
    p = pool([state(1, "a@x", 10, 10, fable=95)])
    assert view(SONNET, p).status == "usable"
    assert view("omp:anthropic/claude-fable-5-1", p).status == "refused"


def test_claude_seat_answering_through_omp_shows_the_omp_account():
    health = SeatHealth("claude:claude-sonnet-5-5", "quota exhausted", SONNET)
    v = view("claude:claude-sonnet-5-5", pool([state(1, "a@x", 10, 10)]), health)
    assert v.account == "a@x" and v.executor == "omp account pool"


def test_seat_without_a_usage_reading_says_usage_unknown_not_zero():
    v = view("zai:glm-5.3", pool([state(1, "a@x", 10, 10)]))
    assert v.status == "usable" and v.account is None
    assert v.five_hour_pct is None and v.executor == "no usage reading"
    assert "usage неизвестен" in v.reason


def test_claude_cli_seat_without_omp_substitute_says_usage_unknown():
    v = view("claude:claude-sonnet-5-5", pool([state(1, "a@x", 10, 10)]))
    assert v.executor == "claude CLI login" and v.five_hour_pct is None


def test_unusable_seat_keeps_its_own_reason():
    health = SeatHealth("codex:gpt-6-luna", "session limit / usage credits", None)
    v = view("codex:gpt-6-luna", pool([]), health)
    assert v.status == "unusable" and "session limit" in v.reason


def test_json_is_serialisable_and_has_no_secret_fields():
    v = view(SONNET, pool([state(1, "a@x", 10, 10)]))
    payload = v.as_json()
    text = json.dumps(payload)
    assert payload["account"] == "a@x" and payload["cap_percent"] == 80
    assert "token" not in text.lower() and "credential" not in text.lower()


def test_table_line_shows_all_columns():
    v = view(SONNET, pool([state(2, "b@x", 5, 29)]))
    line = seat_accounts.render_line("Sonnet-omp", v, tz=timezone.utc)
    for fragment in ("Sonnet-omp", "usable", "b@x", "5h 5%", "7d 29%", "cap 80%"):
        assert fragment in line


def test_table_line_for_refusal_shows_reset_and_reason():
    v = view(SONNET, pool([state(1, "a@x", 83, 10)]))
    line = seat_accounts.render_line("Sonnet-omp", v, tz=timezone.utc)
    assert (
        "refused" in line and "сброс 03:30" in line and "ограничение review-cli" in line
    )


def _claude_cli_pool(five, seven):
    p = pool([state(1, "a@x", 10, 10)])
    usage = {"a@x": windows(five, seven)}
    return OmpPoolState(p.launcher, None, p.db, p.accounts, p.policy, usage)


def _with_cap(monkeypatch, cap=80):
    from reviewlib import account_cap

    account_cap.configure(cap, {})
    monkeypatch.setattr(account_cap, "default_claude_email", lambda: "a@x")
    return account_cap


def test_direct_claude_seat_shows_its_account_usage_and_the_cap(monkeypatch):
    cap = _with_cap(monkeypatch)
    try:
        v = view("claude:claude-sonnet-5-5", _claude_cli_pool(30, 40))
    finally:
        cap.configure_unset()
    assert v.status == "usable" and v.executor == "claude CLI login"
    assert v.account == "a@x" and (v.five_hour_pct, v.seven_day_pct, v.cap_pct) == (
        30,
        40,
        80,
    )


def test_direct_claude_seat_over_the_cap_is_refused_with_the_rule(monkeypatch):
    cap = _with_cap(monkeypatch)
    try:
        v = view("claude:claude-sonnet-5-5", _claude_cli_pool(83, 40))
    finally:
        cap.configure_unset()
    assert v.status == "refused"
    assert v.reason.startswith(
        "ограничение review-cli (config account_usage_cap_percent=80)"
    )
    assert "аккаунт a@x использован на 83% окна 5ч, сброс в 03:30" in v.reason


def test_direct_claude_seat_without_any_reading_is_refused_conservatively(monkeypatch):
    cap = _with_cap(monkeypatch)
    try:
        v = view("claude:claude-sonnet-5-5", pool([state(1, "a@x", 10, 10)]))
    finally:
        cap.configure_unset()
    assert (
        v.status == "refused"
        and "usage неизвестен" in v.reason
        and "консервативно" in v.reason
    )


if __name__ == "__main__":
    # smoke.py runs each unit file as a script; delegate to pytest (tests/conftest.py isolation applies).
    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
