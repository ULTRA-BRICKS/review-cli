"""Per-account verdicts and the account pick for `review seats` (review-cli#707).

The pure policy: given each account's usage windows, the configured cap and "now", say
which account a seat would use (the one with the lowest usage that is below the cap) and,
for a refused one, EXACTLY which rule refused it -- review-cli's own cap, a provider rate
limit, or usage review-cli cannot confirm. Nothing here reads omp, a config file or a
clock; the callers pass those in.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import seat_budget as sb  # noqa: E402

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc).timestamp()
FIVE_H_RESET = datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc).timestamp()
SEVEN_D_RESET = datetime(2026, 10, 30, 22, 59, tzinfo=timezone.utc).timestamp()
CAP_KEY = "account_budget.max_usage_percent"


def reading(
    name,
    five=10.0,
    seven=20.0,
    *,
    read_at=NOW - 60,
    block_until=None,
    block_reason=None,
):
    return sb.AccountReading(
        name=name,
        windows=None
        if five is None and seven is None
        else tuple(
            w
            for w in (
                None if five is None else sb.Window("5h", five, FIVE_H_RESET),
                None if seven is None else sb.Window("7d", seven, SEVEN_D_RESET),
            )
            if w is not None
        ),
        read_at=read_at,
        provider_block_until=block_until,
        provider_block_reason=block_reason,
    )


def judge(r, cap=80.0, **kw):
    return sb.judge(r, cap_percent=cap, cap_key=CAP_KEY, now=NOW, tz=timezone.utc, **kw)


def test_below_the_cap_is_ok():
    v = judge(reading("a", five=30, seven=40))
    assert v.state == sb.OK and v.five_hour_pct == 30 and v.seven_day_pct == 40
    assert v.cap_pct == 80


def test_cap_boundary_79_99_ok_80_and_81_held():
    assert judge(reading("a", five=79.99)).state == sb.OK
    assert judge(reading("a", five=80.0)).state == sb.OVER_CAP
    assert judge(reading("a", five=81.0)).state == sb.OVER_CAP


def test_either_window_over_the_cap_holds_the_account():
    assert judge(reading("a", five=10, seven=83)).state == sb.OVER_CAP
    assert judge(reading("a", five=83, seven=10)).state == sb.OVER_CAP


def test_cap_refusal_names_the_rule_account_percent_window_and_reset():
    v = judge(reading("mextner@gmail.com", five=83, seven=20))
    assert v.reason == (
        "ограничение review-cli (config account_budget.max_usage_percent=80): "
        "аккаунт mextner@gmail.com использован на 83% окна 5ч, сброс в 03:30"
    )


def test_seven_day_window_is_worded_as_weekly():
    v = judge(reading("a", five=10, seven=91))
    assert "91% окна 7д, сброс в 22:59" in v.reason


def test_cap_at_exactly_100_percent_still_reads_as_the_review_cli_cap_without_provider_signal():
    v = judge(reading("a", five=100, seven=10))
    assert v.state == sb.OVER_CAP
    assert "лимиты исчерпаны" not in v.reason


def test_provider_block_is_a_provider_limit_not_the_cap():
    until = datetime(2026, 10, 28, 1, 40, tzinfo=timezone.utc).timestamp()
    v = judge(
        reading("a", five=100, seven=30, block_until=until, block_reason="omp block")
    )
    assert v.state == sb.PROVIDER_LIMITED
    assert "ограничение review-cli (config" not in v.reason
    assert "не ограничение review-cli" in v.reason
    assert "01:40" in v.reason and "omp block" in v.reason


def test_expired_provider_block_is_ignored():
    v = judge(
        reading("a", five=10, seven=10, block_until=NOW - 5, block_reason="omp block")
    )
    assert v.state == sb.OK


def test_missing_usage_is_refused_conservatively_and_says_so():
    v = judge(reading("a", five=None, seven=None))
    assert v.state == sb.USAGE_UNKNOWN
    assert "usage неизвестен" in v.reason and "консервативно" in v.reason


def test_a_missing_window_of_the_pair_is_unknown_not_zero():
    assert judge(reading("a", five=10, seven=None)).state == sb.USAGE_UNKNOWN
    assert judge(reading("a", five=None, seven=10)).state == sb.USAGE_UNKNOWN


def test_stale_reading_is_refused_conservatively():
    v = judge(reading("a", five=10, seven=10, read_at=NOW - 3600), max_age_seconds=1800)
    assert v.state == sb.USAGE_STALE
    assert "устарел" in v.reason


def test_unknown_age_counts_as_stale_when_an_age_limit_is_set():
    v = judge(reading("a", five=10, seven=10, read_at=None), max_age_seconds=1800)
    assert v.state == sb.USAGE_STALE


def test_window_that_already_reset_is_not_a_reading():
    r = sb.AccountReading(
        name="a",
        windows=(sb.Window("5h", 95, NOW - 600), sb.Window("7d", 10, SEVEN_D_RESET)),
        read_at=NOW - 60,
    )
    assert judge(r).state == sb.USAGE_STALE


def test_pick_takes_the_lowest_usage_account_below_the_cap():
    sel = sb.pick(
        [reading("a", 50, 60), reading("b", 5, 29), reading("c", 30, 10)],
        cap_percent=80,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert sel.chosen.name == "b"
    assert [v.name for v in sel.verdicts] == ["a", "b", "c"]


def test_pick_rotates_away_from_an_account_that_crosses_the_cap():
    readings = [reading("a", 79.99, 10), reading("b", 40, 10)]
    first = sb.pick(readings, cap_percent=80, cap_key=CAP_KEY, now=NOW, tz=timezone.utc)
    assert first.chosen.name == "b"  # lower of the two maxima
    readings = [reading("a", 80, 10), reading("b", 40, 10)]
    second = sb.pick(
        readings, cap_percent=80, cap_key=CAP_KEY, now=NOW, tz=timezone.utc
    )
    assert second.chosen.name == "b"
    assert "другой аккаунт доступен: b" in second.verdicts[0].reason


def test_pick_ties_keep_input_order():
    sel = sb.pick(
        [reading("a", 10, 10), reading("b", 10, 10)],
        cap_percent=80,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert sel.chosen.name == "a"


def test_pick_none_available_when_every_account_is_over_the_cap():
    sel = sb.pick(
        [reading("a", 85, 10), reading("b", 10, 90)],
        cap_percent=80,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert sel.chosen is None
    assert (
        sel.summary
        == "все аккаунты выше порога ограничения review-cli (config account_budget.max_usage_percent=80)"
    )
    assert all("другой аккаунт доступен" not in v.reason for v in sel.verdicts)


def test_pick_none_available_mixed_reasons_are_all_listed():
    until = datetime(2026, 10, 28, 1, 40, tzinfo=timezone.utc).timestamp()
    sel = sb.pick(
        [
            reading("a", 85, 10),
            reading("b", 100, 10, block_until=until, block_reason="omp block"),
            reading("c", None, None),
        ],
        cap_percent=80,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert sel.chosen is None
    assert "все аккаунты выше порога" not in sel.summary
    assert "нет подходящего аккаунта" in sel.summary
    for fragment in ("ограничение review-cli", "лимит провайдера", "usage неизвестен"):
        assert fragment in sel.summary


def test_a_tier_window_the_seat_model_uses_counts_against_the_cap():
    r = sb.AccountReading(
        name="a",
        windows=(
            sb.Window("5h", 10, FIVE_H_RESET),
            sb.Window("7d", 10, SEVEN_D_RESET),
            sb.Window("7d-fable", 88, SEVEN_D_RESET),
        ),
        read_at=NOW - 60,
    )
    v = judge(r)
    assert v.state == sb.OVER_CAP and "окна 7d-fable" in v.reason


def test_a_tier_window_that_already_reset_is_ignored():
    r = sb.AccountReading(
        name="a",
        windows=(
            sb.Window("5h", 10, FIVE_H_RESET),
            sb.Window("7d", 10, SEVEN_D_RESET),
            sb.Window("7d-fable", 88, NOW - 5),
        ),
        read_at=NOW - 60,
    )
    assert judge(r).state == sb.OK


def test_pick_ranks_by_the_tier_window_too_so_a_busy_tier_account_is_not_the_emptiest():
    def with_tier(name, shared, tier):
        return sb.AccountReading(
            name=name,
            windows=(
                sb.Window("5h", shared, FIVE_H_RESET),
                sb.Window("7d", shared, SEVEN_D_RESET),
                sb.Window("7d-fable", tier, SEVEN_D_RESET),
            ),
            read_at=NOW - 60,
        )

    sel = sb.pick(
        [with_tier("busy-tier", 10, 75), with_tier("calm", 40, 5)],
        cap_percent=80,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert sel.chosen.name == "calm"


def test_pick_without_accounts_says_so():
    sel = sb.pick([], cap_percent=80, cap_key=CAP_KEY, now=NOW, tz=timezone.utc)
    assert sel.chosen is None and "нет аккаунтов" in sel.summary


def test_no_cap_configured_ranks_by_usage_and_never_holds_on_usage():
    v = sb.judge(
        reading("a", 99, 99),
        cap_percent=None,
        cap_key=CAP_KEY,
        now=NOW,
        tz=timezone.utc,
    )
    assert v.state == sb.OK and v.cap_pct is None


def test_verdicts_never_carry_secrets():
    v = judge(reading("a@example.com", 90, 10))
    text = repr(v)
    assert "token" not in text.lower() and "secret" not in text.lower()


if __name__ == "__main__":
    # smoke.py runs each unit file as a script; delegate to pytest (tests/conftest.py isolation applies).
    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
