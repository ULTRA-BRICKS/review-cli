"""Availability windows (reviewlib.availability, review-cli#663): how long a seat/provider
stays unavailable, read from the failure itself.

Covers the parsing (z.ai code 1310 reset, the Claude CLI's "resets 7:30pm", Retry-After,
credits, auth), what is NOT a window (a blip, a timeout, a long review quoting the words),
the store (explicit window, provider-wide scope, credential-change expiry, recovery clear),
and the wiring: dispatch skips an excluded seat without calling it or taking a provider
slot, board selection / admission planning drop it, and three runs are admitted together
while a provider is excluded (the serialization of review-cli#660).

Runs under pytest and standalone (`python tests/test_availability.py`, as smoke.py does).
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import admission, availability, backends, seat_cooldown  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

ZAI_1310 = (
    '{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted. '
    'Your limit will reset at 2026-10-26 05:58:34"}}'
)
RESET_UTC = datetime(2026, 10, 26, 5, 58, 34, tzinfo=timezone.utc).timestamp()
# A reset three days from the real clock, for tests that classify against `time.time()`
# (a fixed date would pass and then fail once the clock went beyond it).
_LIVE_RESET = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
LIVE_1310 = ZAI_1310.replace(
    "2026-10-26 05:58:34", _LIVE_RESET.strftime("%Y-%m-%d %H:%M:%S")
)
LIVE_STAMP = _LIVE_RESET.strftime("%Y-%m-%d %H:%MZ")
NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@contextlib.contextmanager
def _env(**values):
    """Isolated cooldown store + log dir (+ any extra env) for one test."""
    with tempfile.TemporaryDirectory() as tmp:
        values = {
            "REVIEW_SEAT_COOLDOWN_FILE": str(Path(tmp) / "seat-cooldown.json"),
            "REVIEW_LOG_DIR": str(Path(tmp) / "logs"),
            "REVIEW_SEAT_COOLDOWN_SECONDS": None,
            "REVIEW_FAKE_BACKEND": None,
            # the host's own commandcode key must not make a zai seat fail over to it
            "REVIEW_UNPAID_PROVIDERS": "commandcode,gemini",
            **values,
        }
        saved = {k: os.environ.get(k) for k in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        try:
            yield Path(tmp)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


# ── parsing ─────────────────────────────────────────────────────────────────────────────


def test_zai_1310_names_its_reset_and_is_provider_wide():
    found = availability.classify("", ZAI_1310, 429, now=NOW)
    assert found is not None
    assert found.cause == "quota" and found.source == "reset-time"
    assert found.until == RESET_UTC  # a zone-less timestamp is UTC
    assert found.provider_wide
    assert "reset at 2026-10-26 05:58:34" in found.reason
    assert found.probe_after is not None and NOW < found.probe_after < RESET_UTC


def test_omp_relayed_error_reads_the_same():
    relayed = "[1310][Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-10-26 05:58:34][2026102320303504c1741e1ebd4cac]"
    found = availability.classify("", relayed, 1, now=NOW)
    assert found is not None and found.until == RESET_UTC
    assert found.reason.startswith("quota exhausted: Weekly/Monthly Limit Exhausted")


def test_explicit_offset_in_the_timestamp_is_honoured():
    text = "limit reached, resets at 2026-10-26T13:58:34+08:00"
    assert availability.parse_reset_time(text, now=NOW) == RESET_UTC


def test_claude_cli_clock_time_means_the_next_such_time_in_its_zone():
    os.environ.pop("TZ", None)
    text = "You've hit your session limit · resets 7:30pm (Europe/Moscow)"
    until = availability.parse_reset_time(text, now=NOW)  # NOW = 12:00Z = 15:00 Moscow
    assert until == datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc).timestamp()
    # already past today -> tomorrow
    late = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc).timestamp()
    assert availability.parse_reset_time(text, now=late) == until + 86400


def test_claude_cli_weekly_limit_with_a_date():
    text = "You've hit your weekly limit · resets Oct 28, 5am (UTC)"
    until = availability.parse_reset_time(text, now=NOW)
    assert until == datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc).timestamp()


@contextlib.contextmanager
def _local_zone(name):
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


CODEX_WEEKLY = (
    "You've hit your usage limit. Upgrade to Pro or try again at Oct 30th, 2026 6:43 PM."
)


def test_codex_weekly_limit_is_read_in_the_local_zone():
    with _local_zone("Europe/Belgrade"):  # CET on Oct 30: UTC+1
        until = availability.parse_reset_time(CODEX_WEEKLY, now=NOW)
        found = availability.classify("", CODEX_WEEKLY, 1, now=NOW)
    assert until == datetime(2026, 10, 30, 17, 43, tzinfo=timezone.utc).timestamp()
    assert found is not None and found.source == "reset-time" and found.until == until


def _codex_reset_with_wall_clock(text, wall_clock):
    from unittest.mock import patch

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall_clock if tz is None else wall_clock.astimezone(tz)

    with patch.object(availability, "datetime", Clock):
        return availability.parse_reset_time(text, now=wall_clock.timestamp())


def test_codex_reset_uses_target_offset_after_autumn_transition():
    with _local_zone("Europe/Belgrade"):
        found = _codex_reset_with_wall_clock(
            CODEX_WEEKLY, datetime(2026, 10, 24, 12)
        )
    assert found == datetime(2026, 10, 30, 17, 43, tzinfo=timezone.utc).timestamp()


def test_codex_reset_uses_target_offset_after_spring_transition():
    with _local_zone("Europe/Belgrade"):
        found = _codex_reset_with_wall_clock(
            "try again at Mar 30th, 2026 6:43 PM", datetime(2026, 3, 27, 12)
        )
    assert found == datetime(2026, 3, 30, 16, 43, tzinfo=timezone.utc).timestamp()


def test_codex_reset_ordinals_and_midnight_noon_edges():
    with _local_zone("UTC"):
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (11, "th"), (30, "th")):
            text = f"try again at Oct {day}{suffix}, 2026 6:43 PM."
            want = datetime(2026, 10, day, 18, 43, tzinfo=timezone.utc).timestamp()
            now = want - 86400
            assert availability.parse_reset_time(text, now=now) == want
        for clock, hour in (("12:00 AM", 0), ("12:00 PM", 12)):
            text = f"try again at Oct 25th, 2026 {clock}"
            want = datetime(2026, 10, 25, hour, 0, tzinfo=timezone.utc).timestamp()
            assert availability.parse_reset_time(text, now=NOW) == want


def test_codex_reset_in_the_past_or_malformed_is_not_a_window():
    with _local_zone("UTC"):
        assert (
            availability.parse_reset_time("try again at Oct 1st, 2026 6:43 PM", now=NOW)
            is None
        )
        for bad in (
            "try again at Foo 30th, 2026 6:43 PM",
            "try again at Oct 32nd, 2026 6:43 PM",
            "try again at Oct 30th, 2026 13:43 PM",
            "try again at Jan 1st, 0001 1:00 AM",
            "try again at Dec 31st, 9999 11:00 PM",
        ):
            assert availability.parse_reset_time(bad, now=NOW) is None
            found = availability.classify("", "usage limit; " + bad, 1, now=NOW)
            assert found is not None and found.source == "timer"


def test_epoch_reset_header_seconds_and_millis():
    seconds = int(NOW) + 7200
    assert (
        availability.parse_reset_time(f"x-ratelimit-reset: {seconds}", now=NOW)
        == seconds
    )
    millis = (int(NOW) + 7200) * 1000
    assert (
        availability.parse_reset_time(f'"reset_at": {millis}', now=NOW) == millis / 1000
    )


def test_a_past_or_absurd_reset_is_not_a_reset():
    assert (
        availability.parse_reset_time("resets at 2026-10-01 00:00:00", now=NOW) is None
    )
    assert (
        availability.parse_reset_time("resets at 2031-10-01 00:00:00", now=NOW) is None
    )


def test_retry_after_in_all_its_spellings():
    p = availability.parse_retry_after
    assert p("429\nRetry-After: 900", now=NOW) == 900
    assert p("retry-after-ms=45000 something", now=NOW) == 45
    assert p("please retry after 30 seconds", now=NOW) == 30
    assert p("try again in 2h 30m", now=NOW) == 9000
    http_date = "Retry-After: Fri, 23 Oct 2026 12:10:00 GMT"
    assert p(http_date, now=NOW) == 600
    assert p("nothing to see", now=NOW) is None


def test_long_retry_after_is_a_rate_limit_window_short_one_is_not():
    long_wait = availability.classify(
        "", "429 Too Many Requests\nRetry-After: 900", 429, now=NOW
    )
    assert long_wait is not None
    assert long_wait.cause == "rate_limit" and long_wait.until == NOW + 900
    assert (
        availability.classify("", "429 Too Many Requests\nRetry-After: 5", 429, now=NOW)
        is None
    )
    assert availability.classify("", "429 rate limit exceeded", 429, now=NOW) is None


def test_credits_get_a_long_window_and_a_periodic_reprobe():
    found = availability.classify(
        "", "Error: insufficient balance, please recharge", 402, now=NOW
    )
    assert found is not None
    assert found.cause == "quota" and found.source == "credits"
    assert found.until == NOW + availability.CREDITS_WINDOW_SECONDS
    assert found.probe_after == NOW + availability.CREDITS_REPROBE_SECONDS


def test_auth_failure_is_tied_to_the_credential_files():
    found = availability.classify(
        "", "invalid_grant: refresh token revoked", 1, now=NOW, watch=("/x/auth.json",)
    )
    assert found is not None and found.cause == "auth"
    assert found.watch == ("/x/auth.json",) and found.source == "credential"
    http401 = availability.classify("", "", 401, now=NOW)
    # no body at all: nothing to read, no window
    assert http401 is None
    assert availability.classify("", "HTTP 401 Unauthorized", 401, now=NOW) is not None


def test_a_suspended_provider_account_is_a_provider_wide_window_with_frequent_reprobe():
    """Fireworks went down while its account was suspended: nothing names an end, so the
    seat is held out and re-tried often instead of being hand-edited out of config."""
    found = availability.classify(
        "", '{"error":{"message":"Account suspended: contact support"}}', 412, now=NOW
    )
    assert found is not None
    assert found.cause == "unavailable" and found.source == "suspended"
    assert found.provider_wide
    assert found.until == NOW + availability.SUSPENDED_WINDOW_SECONDS
    assert found.probe_after == NOW + availability.SUSPENDED_REPROBE_SECONDS
    with _env():
        availability.record("omp:fireworks/kimi-k3", found, now=NOW)
        other = availability.exclusion("oc:fireworks/other", now=NOW + 60)
        assert other is not None and other.scope == "provider"


def test_a_quota_body_is_quota_even_with_a_401_status_line():
    found = availability.classify("", "401\n" + ZAI_1310, 401, now=NOW)
    assert found is not None and found.cause == "quota"


def test_session_limit_without_a_time_falls_back_to_the_escalating_timer():
    found = availability.classify("", "You've hit your session limit", 1, now=NOW)
    assert found is not None and found.until is None and found.source == "timer"


def test_things_that_are_not_windows():
    # a killed call says nothing about the account
    assert (
        availability.classify("", "usage credits ... session limit", 124, now=NOW)
        is None
    )
    # a long successful review that merely quotes the words
    review = "The README mentions the session limit and 401 handling. " * 30
    assert availability.classify(review, "", 0, now=NOW) is None
    # an ordinary connection failure
    assert availability.classify("", "connection reset by peer", 1, now=NOW) is None


# ── the store ───────────────────────────────────────────────────────────────────────────


def _record_zai(model="omp:zai/glm-5.3", now=None):
    found = availability.classify("", ZAI_1310, 429, now=now or time.time() - 5)
    assert found is not None
    availability.record(model, found, now=now or time.time() - 5)


def test_a_short_named_wait_is_a_throttle_and_noise_is_not_a_code():
    # z.ai concurrency / OpenAI per-minute limits name a short wait: no exclusion at all
    short = "Rate limit reached for requests. Please try again in 2.002s"
    assert availability.classify("", short, 429, now=NOW) is None
    assert (
        availability.classify(
            "", "Quota exceeded for metric. Please retry in 21s", 429, now=NOW
        )
        is None
    )
    # a bare 401 / 1310 inside a stack frame or a port is not a provider error
    assert (
        availability.classify("", "at cli.js:401:12\nTypeError: x", 1, now=NOW) is None
    )
    assert availability.classify("", "listening on :1310 failed", 1, now=NOW) is None
    assert availability.classify("", "warning: tool unauthorized", 1, now=NOW) is None


def test_only_the_tail_of_stderr_is_read():
    """An agentic CLI streams the diff under review to stderr; a quota notice quoted in
    that diff (far from the end) must not exclude the seat (the real error is last)."""
    transcript = ZAI_1310 + "\n" + "tool output line\n" * 400
    assert availability.classify("", transcript, 1, now=NOW) is None
    assert availability.classify("", "x\n" * 400 + ZAI_1310, 429, now=NOW) is not None
    assert availability.classify("", "Error in app.ts:401", 1, now=NOW) is None
    assert availability.classify("", "HTTP 401 Unauthorized", 1, now=NOW) is not None


def test_a_recorded_window_excludes_the_seat_until_its_end_then_lapses():
    with _env():
        found = availability.classify("", ZAI_1310, 429, now=NOW)
        availability.record("zai:glm-5.3", found, now=NOW)
        inside = availability.exclusion("zai:glm-5.3", now=NOW + 3600)
        assert inside is not None and inside.until == RESET_UTC
        assert inside.scope == "seat" and "2026-10-26 05:58Z" in inside.describe()
        assert availability.exclusion("zai:glm-5.3", now=RESET_UTC + 1) is None


def test_zai_exhaustion_excludes_every_seat_on_the_provider_but_not_other_providers():
    with _env():
        found = availability.classify("", ZAI_1310, 429, now=NOW)
        availability.record("omp:zai/glm-5.3", found, now=NOW)
        # a different seat on the same provider account is excluded through provider:zai
        via_provider = availability.exclusion("zai:glm-5.3", now=NOW + 60)
        assert via_provider is not None and via_provider.scope == "provider"
        assert via_provider.key == "provider:zai"
        assert availability.exclusion("oc:zai/glm-5.3", now=NOW + 60) is not None
        # another gateway and another vendor are untouched
        assert (
            availability.exclusion("commandcode:zai-org/GLM-5.3", now=NOW + 60) is None
        )
        assert availability.exclusion("claude:claude-opus-5-5", now=NOW + 60) is None
        assert set(availability.provider_exclusions(now=NOW + 60)) == {"zai"}


def test_anthropic_seats_are_never_provider_wide():
    with _env():
        found = availability.classify(
            "", "You've hit your weekly limit · resets Oct 28, 5am (UTC)", 1, now=NOW
        )
        availability.record("claude:claude-opus-5-5", found, now=NOW)
        assert (
            availability.exclusion("claude:claude-opus-5-5", now=NOW + 60) is not None
        )
        # omp's Anthropic pool is a separate login: not excluded by the CLI's limit
        assert (
            availability.exclusion("omp:anthropic/claude-opus-5-5", now=NOW + 60)
            is None
        )


def test_an_auth_window_ends_when_the_credential_file_changes():
    with _env() as tmp:
        cred = tmp / "auth.json"
        cred.write_text("{}")
        old = time.time() - 600
        os.utime(cred, (old, old))
        found = availability.classify(
            "", "invalid_grant", 1, now=time.time(), watch=(str(cred),)
        )
        availability.record("codex:gpt-6-luna", found)
        assert availability.exclusion("codex:gpt-6-luna") is not None
        cred.write_text('{"fresh": true}')  # a re-login rewrites the file
        # coarse filesystem timestamps (CI overlay fs) can tie with the record's clock
        later = time.time() + 5
        os.utime(cred, (later, later))
        assert availability.exclusion("codex:gpt-6-luna") is None


def test_a_probe_due_marker_survives_and_the_latest_end_wins():
    with _env():
        found = availability.classify("", ZAI_1310, 429, now=NOW)
        availability.record("zai:glm-5.3", found, now=NOW)
        seat_cooldown.record_window(
            "zai:glm-5.3",
            "older timer",
            until=NOW + 60,
            cause="quota",
            source="timer",
            now=NOW,
            access_method="api",
        )
        excl = availability.exclusion("zai:glm-5.3", methods=("api",), now=NOW + 30)
        assert excl is not None and excl.until == RESET_UTC
        assert excl.probe_after is not None and not excl.probe_due(NOW + 30)
        assert excl.probe_due(excl.probe_after + 1)


def test_a_usable_answer_drops_the_window_and_the_provider_window():
    with _env():
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("omp:zai/glm-5.3", found)
        ok = ReviewResult("omp:zai/glm-5.3", "c", 0, "A real review verdict.", "")
        availability.observe("omp:zai/glm-5.3", ok, usable=True)
        assert availability.exclusion("omp:zai/glm-5.3") is None
        assert availability.exclusion("zai:glm-5.3") is None


def test_the_disable_hatch_turns_the_whole_store_off():
    with _env(REVIEW_SEAT_COOLDOWN_SECONDS="0"):
        found = availability.classify("", ZAI_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)
        assert availability.exclusion("zai:glm-5.3") is None


# ── wiring: dispatch, board, admission ──────────────────────────────────────────────────


def _failing_backend(calls):
    def backend(model, prompt, diff, cwd, timeout, round_no=0):
        calls.append(model)
        return ReviewResult(model, "POST", 429, "", LIVE_1310)

    return backend


def test_call_backend_records_the_window_then_skips_without_calling():
    with _env(ZAI_API_KEY="test-key"):
        calls: list[str] = []
        backend = _failing_backend(calls)
        first = backends.call_backend(backend, "zai:glm-5.3", "p", "d", Path("."), 5)
        assert first.returncode == 429 and calls == ["zai:glm-5.3"]
        second = backends.call_backend(backend, "zai:glm-5.3", "p", "d", Path("."), 5)
        assert calls == ["zai:glm-5.3"], "an excluded seat must not be called again"
        assert second.returncode == 0 and "unavailable" in second.stdout.lower()
        assert LIVE_STAMP in second.stdout
        # another seat on the same provider is skipped too, with no call
        other = backends.call_backend(backend, "oc:zai/glm-5.3", "p", "d", Path("."), 5)
        assert calls == ["zai:glm-5.3"] and "unavailable" in other.stdout.lower()


def test_a_skipped_seat_takes_no_provider_slot():
    with _env(ZAI_API_KEY="test-key"):
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)

        def boom(*_a, **_k):
            raise AssertionError("seat_slot must not be entered for an excluded seat")

        real = admission.seat_slot
        admission.seat_slot = boom
        try:
            out = backends.call_backend(
                _failing_backend([]), "zai:glm-5.3", "p", "d", Path("."), 5
            )
        finally:
            admission.seat_slot = real
        assert "unavailable" in out.stdout.lower()


def test_backend_unavailable_reason_names_the_window():
    with _env(ZAI_API_KEY="test-key"):
        assert backends.backend_available("zai:glm-5.3")
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("omp:zai/glm-5.3", found)
        reason = backends.backend_unavailable_reason("zai:glm-5.3")
        assert reason is not None and reason.startswith("unavailable:")
        assert LIVE_STAMP in reason and not backends.backend_available("zai:glm-5.3")


def test_admission_plans_no_seat_for_an_excluded_provider():
    with _env(ZAI_API_KEY="test-key"):
        seats = ["zai:glm-5.3", "claude:claude-opus-5-5"]
        assert "zai:glm-5.3" in admission.routed(seats)
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)
        assert "zai:glm-5.3" not in admission.routed(seats)


_ADMIT_SCRIPT = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from reviewlib import admission
tmp, models, idx = Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]
cfg = admission.Settings(dir=tmp / "admission", poll_seconds=0.05, learn=False,
                         min_slots=0, providers={{"zai": admission.ProviderLimit(1)}})
host = admission.HostSnapshot(10, 0.0, 10_000.0, 10_000.0)
with admission.run_slot(mode="review", models=models, cfg=cfg, probe=lambda: host,
                        stream=open(os.devnull, "w")):
    (tmp / ("in-" + idx)).write_text("x")
    deadline = time.time() + 3
    while time.time() < deadline:
        if len(list(tmp.glob("in-*"))) == 3:
            print("TOGETHER")
            sys.exit(0)
        time.sleep(0.05)
    print("ALONE")
"""


def _admit_three_together(models: list[str], tmp: Path) -> bool:
    """Start three REAL processes needing `models` at once; True iff all three hold a run
    slot at the same time (each waits, inside its slot, for the others to arrive)."""
    import json
    import subprocess

    tmp.mkdir(parents=True, exist_ok=True)
    script = _ADMIT_SCRIPT.format(repo=str(REPO_ROOT))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp), json.dumps(models), str(i)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(3)
    ]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    return outs == ["TOGETHER"] * 3


def test_three_runs_are_admitted_together_while_the_provider_is_excluded():
    """review-cli#660: a dead provider used to hold one seat reservation per run, so with
    its limit at 1 every run waited for the previous one."""
    with _env(ZAI_API_KEY="test-key") as tmp:
        seats = ["zai:glm-5.3"]
        control = _admit_three_together(admission.routed(seats), tmp / "a")
        assert not control, (
            "control: a live zai provider at limit 1 serializes the runs"
        )
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)
        assert admission.routed(seats) == []
        assert _admit_three_together(admission.routed(seats), tmp / "b")


def test_show_board_lists_the_excluded_seat_with_reason_and_until():
    from reviewlib import cli

    with _env(ZAI_API_KEY="test-key"):
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli._show_board(
                {"models": ["zai:glm-5.3", "claude:claude-opus-5-5"]},
                preset=None,
                explicit_models=[],
            )
        text = out.getvalue()
        assert rc == 0
        zai_line = next(ln for ln in text.splitlines() if "zai:glm-5.3" in ln)
        assert "SKIPPED (unavailable:" in zai_line and "until " + LIVE_STAMP in zai_line
        assert "[unavail]" in zai_line


def test_jobs_status_shows_the_provider_as_excluded_not_throttled():
    with _env(ZAI_API_KEY="test-key") as tmp:
        found = availability.classify("", LIVE_1310, 429, now=time.time())
        availability.record("zai:glm-5.3", found)
        (tmp / "admission").mkdir()
        cfg = admission.Settings(dir=tmp / "admission", learn=False)
        snap = admission.status_snapshot(
            cfg, probe=lambda: admission.HostSnapshot(4, 0.0, 8000.0, 8000.0)
        )
        text = admission.render_status(snap)
        zai = next(ln for ln in text.splitlines() if ln.strip().startswith("zai"))
        assert "EXCLUDED" in zai and "THROTTLED" not in zai and LIVE_STAMP in zai


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
