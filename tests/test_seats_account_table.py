"""End to end: `review seats` prints one account row per seat and `--json` carries the same
fields (review-cli#707). Fake omp binary, fake agent.db, fake `omp usage`; no model call."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from test_omp_account_pool import _MODEL, _world  # noqa: E402


def _usage(**pairs):
    """name -> (five_hour_fraction, seven_day_fraction)."""
    resets = int(time.time() * 1000) + 3_600_000
    return json.dumps(
        {
            "reports": [
                {
                    "provider": "anthropic",
                    "metadata": {"email": f"{name}@example.com"},
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


def _config(world, text):
    path = world.home / ".config" / "review-cli" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_CONFIG = f'models:\n  - "{_MODEL}"\nomp_accounts:\n  max_usage_percent: 80\n'


def _seat_row(text):
    """The first seat row of the SEAT ACCOUNTS section (the section's header is line 0)."""
    section = text.split("SEAT ACCOUNTS", 1)[1].split("\n\n", 1)[0]
    return section.splitlines()[1]


def test_seats_shows_the_lowest_usage_account_with_usage_cap_and_reset():
    with _world() as world:
        _config(world, _CONFIG)
        out = world.review(
            "seats", FAKE_OMP_USAGE=_usage(a=(0.60, 0.50), b=(0.05, 0.29))
        )
    assert out.returncode == 0, out.stdout + out.stderr
    row = _seat_row(out.stdout)
    assert "usable" in row and "b@example.com" in row, row
    assert "5h 5%" in row and "7d 29%" in row and "cap 80%" in row and "сброс" in row, (
        row
    )


def test_seats_json_has_the_account_object_per_seat():
    with _world() as world:
        _config(world, _CONFIG)
        out = world.review(
            "seats", "--json", FAKE_OMP_USAGE=_usage(a=(0.10, 0.20), b=(0.30, 0.40))
        )
    assert out.returncode == 0, out.stdout + out.stderr
    seat = json.loads(out.stdout)["seats"][0]["account"]
    assert seat["status"] == "usable" and seat["account"] == "a@example.com"
    assert seat["five_hour_percent"] == 10 and seat["seven_day_percent"] == 20
    assert seat["cap_percent"] == 80 and seat["reset_at"] is not None


def test_seats_says_all_accounts_are_over_the_threshold_and_names_the_rule():
    with _world() as world:
        _config(world, _CONFIG)
        out = world.review("seats", FAKE_OMP_USAGE=_usage(a=(0.92, 0.1), b=(0.83, 0.1)))
    assert out.returncode == 0, out.stdout + out.stderr
    section = out.stdout.split("SEAT ACCOUNTS", 1)[1].split("\n\n", 1)[0]
    assert "refused" in section
    assert "все аккаунты выше порога ограничения review-cli" in section
    assert "config omp_accounts.max_usage_percent=80" in section
    assert "аккаунт b@example.com использован на 83% окна 5ч" in section
    assert (
        "лимиты исчерпаны" not in out.stdout
        and "limits exhausted" not in out.stdout.lower()
    )


def test_seats_unknown_usage_is_conservative():
    with _world() as world:
        _config(world, _CONFIG)
        out = world.review("seats", FAKE_OMP_USAGE="not json")
    assert out.returncode == 0, out.stdout + out.stderr
    section = out.stdout.split("SEAT ACCOUNTS", 1)[1].split("\n\n", 1)[0]
    assert "usage неизвестен" in section and "консервативно" in section


if __name__ == "__main__":
    # smoke.py runs each unit file as a script; delegate to pytest (tests/conftest.py isolation applies).
    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
