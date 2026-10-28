#!/usr/bin/env python3
"""`reviewlib.capacity_check` — the `review capacity-check` report (review-cli#472).

`all_exhausted: true` lets `gh ship` pass a review gate without a review, so these tests
pin that it is asserted only on fresh, applicable, typed quota evidence for every
candidate route/account, and that every other state (no evidence, stale evidence, a
missing/failed/malformed source, a non-quota cooldown, a healthy account) keeps it false.

Isolation: harness cards come from `$REVIEW_HARNESS_LIMITS_JSON`, cooldowns from a temp
`$REVIEW_SEAT_COOLDOWN_FILE`, the Claude account registry from a temp
`$REVIEW_CLAUDE_ACCOUNTS_ROOT`; backend liveness/transport are stubbed on `reviewlib.backends`, so
nothing reads this host's real CLIs, keys, quota or cooldown store.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import warnings
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.availability as availability  # noqa: E402
import reviewlib.backends as backends  # noqa: E402
import reviewlib.capacity_check as cc  # noqa: E402
import reviewlib.claude_accounts as accts  # noqa: E402
import reviewlib.cli as cli  # noqa: E402
import reviewlib.seat_cooldown as sc  # noqa: E402
import reviewlib.usage_limits as ul  # noqa: E402
from reviewlib.config import (  # noqa: E402
    BoardConfigError,
    default_level_name,
    resolve_review_board,
)

_REAL_BACKEND_UNAVAILABLE_REASON = backends.backend_unavailable_reason
OPUS = "claude:claude-opus-4-8"
SONNET = "claude:claude-sonnet-5"
OPUS_OMP = "omp:anthropic/claude-opus-4-8"
NOW = time.time()
FIVE_HOURS = 5 * 3600
WEEK = 7 * 86400


def _window(used, *, seconds=FIVE_HOURS, resets_at=None):
    return {
        "name": "5-hour" if seconds == FIVE_HOURS else "weekly",
        "used_percent": used,
        "resets_at": resets_at,
        "window_seconds": seconds,
    }


def _card(provider, account, *windows, status="ok"):
    return {
        "provider": provider,
        "account": account,
        "status": status,
        "windows": list(windows),
    }


def _make_omp_db(path: Path, accounts) -> Path:
    """A fake omp agent.db holding Anthropic OAuth `accounts` as (id, email); with
    `accounts` None no file is created (omp not set up)."""
    if accounts is None:
        return path
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "provider TEXT NOT NULL, credential_type TEXT NOT NULL, data TEXT NOT NULL, "
            "disabled_cause TEXT DEFAULT NULL, identity_key TEXT DEFAULT NULL)"
        )
        for cred_id, email in accounts:
            conn.execute(
                "INSERT INTO auth_credentials (id, provider, credential_type, data, "
                "identity_key) VALUES (?, 'anthropic', 'oauth', '{}', ?)",
                (cred_id, f"email:{email}|org:o{cred_id}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@contextlib.contextmanager
def _env(
    *,
    live=(),
    cards=None,
    harness_raw=None,
    accounts=(),
    claude_mode="cli",
    omp_accounts=None,
):
    """An isolated capacity-check world. `live`: routes whose backend is runnable (every
    other route is reported unavailable, so provider failover drops it as long as one
    route of the model is live). `cards`: harness limit cards (None = `{"cards": []}`).
    `harness_raw`: the raw `$REVIEW_HARNESS_LIMITS_JSON` value instead. `accounts`:
    emails of registered Claude accounts (`account-0`, `account-1`, ...).
    `omp_accounts`: `(credential id, email)` Anthropic accounts in a fake omp agent.db
    (None: no agent.db at all)."""
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "REVIEW_HARNESS_LIMITS_JSON",
            "REVIEW_SEAT_COOLDOWN_FILE",
            accts._ENV_ROOT,
            "REVIEW_CAPACITY_MAX_AGE_SECONDS",
            "REVIEW_SEAT_COOLDOWN_SECONDS",
            "REVIEW_FAKE_BACKEND",
            "OMP_AUTH_DB",
            "REVIEW_OMP_CLAUDE_PROFILES",
            "REVIEW_OMP_ACCOUNTS_EXCLUDE",
            "REVIEW_OMP_MAX_USAGE_PERCENT",
        )
    }
    saved_attrs = {
        name: getattr(backends, name)
        for name in (
            "backend_unavailable_reason",
            "runtime_provider_marked_unpaid",
            "claude_access_method",
        )
    }
    live = set(live)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "accounts"
        root.mkdir()
        for i, email in enumerate(accounts):
            acct = root / f"account-{i}"
            acct.mkdir()
            (acct / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
            )
        os.environ[accts._ENV_ROOT] = str(root)
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ["REVIEW_HARNESS_LIMITS_JSON"] = (
            harness_raw
            if harness_raw is not None
            else json.dumps({"cards": cards or []})
        )
        for k in (
            "REVIEW_CAPACITY_MAX_AGE_SECONDS",
            "REVIEW_SEAT_COOLDOWN_SECONDS",
            "REVIEW_FAKE_BACKEND",
            "REVIEW_OMP_CLAUDE_PROFILES",
            "REVIEW_OMP_ACCOUNTS_EXCLUDE",
            "REVIEW_OMP_MAX_USAGE_PERCENT",
        ):
            os.environ.pop(k, None)
        os.environ["OMP_AUTH_DB"] = str(
            _make_omp_db(Path(d) / "agent.db", omp_accounts)
        )
        backends.backend_unavailable_reason = lambda m, honor_exclusions=True: (
            None if m in live else f"stub: {m} not live"
        )
        backends.runtime_provider_marked_unpaid = lambda m: False
        backends.claude_access_method = lambda: claude_mode
        try:
            yield Path(d)
        finally:
            for name, value in saved_attrs.items():
                setattr(backends, name, value)
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _report(models, *, now=NOW, preset=None):
    return cc.board_capacity_report(
        config={"models": list(models)}, preset=preset, now=now
    )


def _by_route(report):
    out: dict[str, list[dict]] = {}
    for c in report["candidates"]:
        out.setdefault(c["route"], []).append(c)
    return out


def _account_dir(index: int) -> Path:
    return Path(os.environ[accts._ENV_ROOT]) / f"account-{index}"


# ---- the two verified grooming cases (review-cli#472) ------------------------------


def test_database_locked_cooldowns_are_not_quota_exhaustion():
    """Grooming case 1: two cooldowns whose reason is `local database is locked; quota
    unknown` used to produce all_exhausted=true. A cooldown without a typed quota cause
    is not quota evidence."""
    with _env(live={OPUS, "codex"}):
        for model in (OPUS, "codex"):
            sc.record_cooldown(
                model,
                "local database is locked; quota unknown",
                access_method="cli",
                now=NOW - 60,
                ttl_seconds=3600,
            )
        report = _report([OPUS, "codex"])
    assert report["all_exhausted"] is False, report
    opus = _by_route(report)[OPUS][0]
    assert opus["status"] == "unknown" and opus["reason"] == "cooldown_unclassified", (
        opus
    )
    assert report["exhausted"] == []


def test_no_evidence_is_unknown_never_available():
    """Grooming case 2: candidates with no evidence used to be listed as `available`.
    They are `unknown` now, and the old `available` key is gone."""
    with _env(live={OPUS, "codex"}):
        report = _report([OPUS, "codex"])
    assert "available" not in report
    assert report["all_exhausted"] is False
    statuses = {c["route"]: (c["status"], c["reason"]) for c in report["candidates"]}
    assert statuses == {
        OPUS: ("unknown", "no_observation"),
        "codex": ("unknown", "no_observation"),
    }, statuses
    assert report["coverage"]["unknown"] == 2
    assert report["coverage"]["complete"] is False


def test_typed_non_quota_cooldowns_are_unavailable_not_exhausted():
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS,
            "true-silence timeout",
            access_method="cli",
            now=NOW - 60,
            ttl_seconds=3600,
            cause=sc.CAUSE_STALL,
        )
        report = _report([OPUS])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"], cand["cause"]) == (
        "unavailable",
        "cooldown_not_quota",
        "stall",
    ), cand
    assert report["all_exhausted"] is False


# ---- all_exhausted requires every candidate --------------------------------------


def test_zero_candidates_never_all_exhausted():
    """A board that cannot be resolved has no candidates; that is never `all exhausted`."""
    bad = {"board": [{"model": OPUS, "role": "not-a-role"}]}
    with _env(live={OPUS}):
        report = cc.board_capacity_report(config=bad, now=NOW)
    assert report["all_exhausted"] is False
    assert report["candidates"] == [] and report["coverage"]["candidates"] == 0
    assert report["error"] and "BoardConfigError" in report["error"], report["error"]
    assert report["schema_version"] == cc.SCHEMA_VERSION


def test_single_account_quota_cooldown_is_exhaustion_with_its_observation_time():
    recorded = NOW - 120
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS,
            "session limit / usage credits",
            access_method="cli",
            now=recorded,
            ttl_seconds=3600,
            cause=sc.CAUSE_QUOTA,
        )
        report = _report([OPUS])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"], cand["cause"]) == (
        "exhausted",
        "quota_cooldown",
        "quota",
    ), cand
    assert cand["observed_at"] == cc._iso(recorded)
    assert cand["age_seconds"] == 120.0
    assert cand["quota_identity"] == "claude-cli:default"
    assert report["all_exhausted"] is True
    assert report["coverage"]["complete"] is True


def test_mixed_accounts_one_healthy_account_keeps_all_exhausted_false():
    cards = [
        _card("Claude Code", "a@x.com", _window(100)),
        _card("Claude Code", "b@x.com", _window(40)),
    ]
    with _env(live={OPUS}, cards=cards, accounts=("a@x.com", "b@x.com")):
        report = _report([OPUS])
    by_account = {c["account"]: c for c in report["candidates"]}
    assert by_account["a@x.com"]["status"] == "exhausted"
    assert by_account["a@x.com"]["reason"] == "quota_window_exhausted"
    assert by_account["b@x.com"]["status"] == "usable"
    assert by_account["b@x.com"]["reason"] == "usage_below_limit"
    assert by_account["a@x.com"]["quota_identity"] == "claude-cli:a@x.com"
    assert by_account["b@x.com"]["quota_identity"] == "claude-cli:b@x.com"
    assert report["all_exhausted"] is False


def test_every_account_exhausted_is_all_exhausted_and_shares_identity_across_models():
    cards = [
        _card("Claude Code", "a@x.com", _window(100)),
        _card("Claude Code", "b@x.com", _window(20), _window(100, seconds=WEEK)),
    ]
    with _env(live={OPUS, SONNET}, cards=cards, accounts=("a@x.com", "b@x.com")):
        report = _report([OPUS, SONNET])
    assert report["all_exhausted"] is True, report["candidates"]
    assert len(report["candidates"]) == 4
    # Two models on the same account share ONE quota identity, stated explicitly.
    identities = {(c["route"], c["quota_identity"]) for c in report["candidates"]}
    assert (OPUS, "claude-cli:a@x.com") in identities
    assert (SONNET, "claude-cli:a@x.com") in identities
    assert {c["quota_identity_basis"] for c in report["candidates"]} == {"account"}
    assert len(report["exhausted"]) == 4
    assert all(e["model"] in (OPUS, SONNET) for e in report["exhausted"])


def test_one_exhausted_account_does_not_gate_a_distinct_account_without_evidence():
    """Account specificity: a card for account a@ says nothing about account b@."""
    cards = [_card("Claude Code", "a@x.com", _window(100))]
    with _env(live={OPUS}, cards=cards, accounts=("a@x.com", "b@x.com")):
        report = _report([OPUS])
    by_account = {c["account"]: c for c in report["candidates"]}
    assert by_account["a@x.com"]["status"] == "exhausted"
    assert by_account["b@x.com"]["status"] == "unknown"
    assert report["all_exhausted"] is False


def test_route_level_quota_cooldown_is_not_attributed_to_one_of_several_accounts():
    with _env(live={OPUS}, accounts=("a@x.com", "b@x.com")):
        sc.record_cooldown(
            OPUS,
            "session limit / usage credits",
            access_method="cli",
            now=NOW - 60,
            ttl_seconds=3600,
            cause=sc.CAUSE_QUOTA,
        )
        report = _report([OPUS])
    reasons = {c["reason"] for c in report["candidates"]}
    assert reasons == {"cooldown_not_account_scoped"}, report["candidates"]
    assert report["all_exhausted"] is False


def test_availability_window_is_read_like_a_timer_cooldown_review_cli_663():
    """A seat's availability window (`record_window`, access method `window`) carries the
    same typed cause: quota is quota evidence under the same freshness/identity rules,
    every other cause is non-quota evidence."""
    with _env(live={OPUS}):
        sc.record_window(
            OPUS,
            "weekly limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 120,
        )
        cand = _report([OPUS])["candidates"][0]
    assert (cand["status"], cand["reason"], cand["cause"], cand["source"]) == (
        "exhausted",
        "quota_cooldown",
        "quota",
        "seat-cooldown:window",
    ), cand
    assert cand["observed_at"] == cc._iso(NOW - 120)

    for cause in (sc.CAUSE_RATE_LIMIT, sc.CAUSE_AUTH, sc.CAUSE_UNAVAILABLE):
        with _env(live={OPUS}):
            sc.record_window(
                OPUS,
                "blocked",
                until=NOW + 3600,
                cause=cause,
                source="retry-after",
                now=NOW - 60,
            )
            cand = _report([OPUS])["candidates"][0]
        assert (cand["status"], cand["reason"], cand["cause"]) == (
            "unavailable",
            "cooldown_not_quota",
            cause,
        ), cand

    with _env(live={OPUS}, accounts=("a@x.com", "b@x.com")):
        sc.record_window(
            OPUS,
            "weekly limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 60,
        )
        report = _report([OPUS])
    assert {c["reason"] for c in report["candidates"]} == {
        "cooldown_not_account_scoped"
    }
    assert report["all_exhausted"] is False


def test_window_on_a_keyless_backend_and_on_its_provider_key_is_read():
    """codex records no timer cooldown, but `call_backend` writes its window under the
    route; a provider-wide exhaustion is written under `provider:<name>` (review-cli#663)."""
    with _env(live={"codex"}):
        sc.record_window(
            "codex",
            "usage limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 30,
        )
        cand = _report(["codex"])["candidates"][0]
    assert (cand["status"], cand["reason"], cand["source"]) == (
        "exhausted",
        "quota_cooldown",
        "seat-cooldown:window",
    ), cand

    zai = "omp:zai/glm-5.3"
    assert availability.provider_key(zai) == "provider:zai"
    with _env(live={zai}):
        sc.record_window(
            "provider:zai",
            "credits exhausted",
            until=NOW + 3600,
            cause=sc.CAUSE_UNAVAILABLE,
            source="credits",
            now=NOW - 30,
        )
        cand = _report([zai])["candidates"][0]
    assert (cand["status"], cand["reason"], cand["cause"]) == (
        "unavailable",
        "cooldown_not_quota",
        "unavailable",
    ), cand


def test_omp_pool_route_window_counts_only_when_it_is_not_quota():
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        sc.record_window(
            OPUS_OMP,
            "weekly limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 30,
        )
        report = _report([OPUS_OMP])
    assert {c["reason"] for c in report["candidates"]} == {"no_observation"}
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        sc.record_window(
            OPUS_OMP,
            "login rejected",
            until=NOW + 3600,
            cause=sc.CAUSE_AUTH,
            source="credential",
            now=NOW - 30,
        )
        report = _report([OPUS_OMP])
    assert {(c["status"], c["cause"]) for c in report["candidates"]} == {
        ("unavailable", "auth")
    }


def test_omp_pool_route_timer_cooldown_counts_only_when_it_is_not_quota():
    """A stall on the route blocks dispatch for every pool account (`review_omp` checks
    the route cooldown first); a quota one names no single account."""
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        sc.record_cooldown(
            OPUS_OMP,
            "true-silence timeout",
            access_method="omp",
            now=NOW - 30,
            ttl_seconds=3600,
            cause=sc.CAUSE_STALL,
        )
        report = _report([OPUS_OMP])
    assert {(c["status"], c["cause"]) for c in report["candidates"]} == {
        ("unavailable", "stall")
    }
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        sc.record_cooldown(
            OPUS_OMP,
            "session limit",
            access_method="omp",
            now=NOW - 30,
            ttl_seconds=3600,
            cause=sc.CAUSE_QUOTA,
        )
        report = _report([OPUS_OMP])
    assert {c["reason"] for c in report["candidates"]} == {"no_observation"}


def test_real_probe_honors_a_stored_window_unless_asked_not_to():
    """The production `backend_unavailable_reason` reports a windowed seat as unavailable;
    `honor_exclusions=False` is what lets capacity-check read the typed window itself."""
    with _env(live={OPUS}):
        real = _REAL_BACKEND_UNAVAILABLE_REASON
        os.environ.pop("REVIEW_FAKE_BACKEND", None)
        sc.record_window(
            "codex",
            "usage limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=time.time() - 30,
        )
        assert real("codex").startswith("unavailable:")
        assert not (real("codex", honor_exclusions=False) or "").startswith(
            "unavailable:"
        )


def test_freshest_typed_quota_record_decides_not_the_first_found():
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS,
            "session limit",
            access_method="cli",
            now=NOW - 7200,
            ttl_seconds=86400,
            cause=sc.CAUSE_QUOTA,
        )
        sc.record_window(
            OPUS,
            "weekly limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 10,
        )
        cand = _report([OPUS])["candidates"][0]
    assert (cand["status"], cand["reason"], cand["source"]) == (
        "exhausted",
        "quota_cooldown",
        "seat-cooldown:window",
    ), cand
    # an untyped record never masks a typed quota one
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS, "mystery", access_method="cli", now=NOW - 5, ttl_seconds=3600
        )
        sc.record_window(
            OPUS,
            "weekly limit",
            until=NOW + 3600,
            cause=sc.CAUSE_QUOTA,
            source="reset-time",
            now=NOW - 60,
        )
        cand = _report([OPUS])["candidates"][0]
    assert cand["reason"] == "quota_cooldown", cand


def test_stored_exclusion_is_evidence_not_a_backend_probe_failure():
    """Production `backend_unavailable_reason` reports an excluded seat as unavailable;
    capacity-check must ask without the exclusion so the typed record is what it reads."""
    seen = []
    with _env(live={OPUS}):
        stub = backends.backend_unavailable_reason

        def spy(m, honor_exclusions=True):
            seen.append(honor_exclusions)
            return stub(m, honor_exclusions=honor_exclusions)

        backends.backend_unavailable_reason = spy
        _report([OPUS])
    assert seen and set(seen) == {False}, seen


def test_account_fallbacks_every_failover_route_must_be_exhausted():
    """A seat whose Claude CLI route is exhausted still has its omp twin; with no
    evidence for the twin the fleet is not exhausted. A fresh quota cooldown on the twin
    completes the set."""
    cards = [_card("Claude Code", None, _window(100))]
    with _env(live={OPUS, OPUS_OMP}, cards=cards):
        report = _report([OPUS])
        routes = _by_route(report)
        assert set(routes) == {OPUS, OPUS_OMP}, routes
        assert routes[OPUS][0]["status"] == "exhausted"
        assert routes[OPUS_OMP][0]["status"] == "unknown"
        assert routes[OPUS_OMP][0]["quota_identity"] == f"route:{OPUS_OMP}@omp"
        assert routes[OPUS_OMP][0]["quota_identity_basis"] == "route"
        assert report["all_exhausted"] is False

        sc.record_cooldown(
            OPUS_OMP,
            "session limit / usage credits",
            access_method=backends._OMP_ACCESS_METHOD,
            now=NOW - 30,
            ttl_seconds=3600,
            cause=sc.CAUSE_QUOTA,
        )
        report = _report([OPUS])
    assert report["all_exhausted"] is True, report["candidates"]


# ---- omp's Anthropic account pool (review-cli#637, #641) ----------------------------

OMP_POOL = ((2, "a@x.com"), (3, "b@x.com"))
QUOTA_REASON = "session limit / usage credits"


def _record_omp_account_quota(credential_id, *, cause=sc.CAUSE_QUOTA, age=30):
    sc.record_cooldown(
        f"omp-account:anthropic:{credential_id}",
        QUOTA_REASON,
        access_method=backends._OMP_ACCESS_METHOD,
        now=NOW - age,
        ttl_seconds=3600,
        cause=cause,
    )


def _record_omp_route_quota():
    """What a pool call also writes: the failing account's limit under the ROUTE key."""
    sc.record_cooldown(
        OPUS_OMP,
        QUOTA_REASON,
        access_method=backends._OMP_ACCESS_METHOD,
        now=NOW - 30,
        ttl_seconds=3600,
        cause=sc.CAUSE_QUOTA,
    )


def test_omp_pool_expands_to_one_candidate_per_account_and_attributes_quota_to_it():
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        _record_omp_account_quota(2)
        report = _report([OPUS_OMP])
    by_account = {c["account"]: c for c in report["candidates"]}
    assert set(by_account) == {"a@x.com", "b@x.com"}, report["candidates"]
    assert by_account["a@x.com"]["status"] == "exhausted"
    assert by_account["a@x.com"]["reason"] == "quota_cooldown"
    assert by_account["a@x.com"]["quota_identity"] == "omp-anthropic:a@x.com"
    assert by_account["a@x.com"]["quota_identity_basis"] == "account"
    # one limited account never gates the healthy one
    assert by_account["b@x.com"]["status"] == "unknown"
    assert report["all_exhausted"] is False


def test_omp_pool_every_account_with_quota_evidence_is_all_exhausted():
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        _record_omp_account_quota(2)
        _record_omp_account_quota(3)
        report = _report([OPUS_OMP])
    assert report["all_exhausted"] is True, report["candidates"]


def test_omp_pool_route_cooldown_is_not_attributed_to_any_account():
    """A pool call also records the failing account's limit under the ROUTE key; that
    entry names no account, so it proves nothing about either."""
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        _record_omp_route_quota()
        report = _report([OPUS_OMP])
    assert {c["status"] for c in report["candidates"]} == {"unknown"}
    assert report["all_exhausted"] is False


def test_omp_pool_login_failure_is_unavailable_not_exhausted():
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
        _record_omp_account_quota(2)
        _record_omp_account_quota(3, cause=sc.CAUSE_AUTH)
        report = _report([OPUS_OMP])
    by_account = {c["account"]: c for c in report["candidates"]}
    assert by_account["b@x.com"]["status"] == "unavailable"
    assert by_account["b@x.com"]["cause"] == "auth"
    assert report["all_exhausted"] is False


def test_omp_pool_account_is_matched_to_its_harness_card_by_email():
    cards = [
        _card("Claude Code", "a@x.com", _window(100)),
        _card("Claude Code", "b@x.com", _window(100)),
    ]
    with _env(live={OPUS_OMP}, cards=cards, omp_accounts=OMP_POOL):
        report = _report([OPUS_OMP])
    assert report["all_exhausted"] is True, report["candidates"]
    assert {c["reason"] for c in report["candidates"]} == {"quota_window_exhausted"}


def test_omp_pool_excluded_account_is_not_a_candidate():
    with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL + ((4, "c@x.com"),)):
        os.environ["REVIEW_OMP_ACCOUNTS_EXCLUDE"] = "c@x.com"
        _record_omp_account_quota(2)
        _record_omp_account_quota(3)
        report = _report([OPUS_OMP])
    assert {c["account"] for c in report["candidates"]} == {"a@x.com", "b@x.com"}
    assert report["all_exhausted"] is True, report["candidates"]


def test_omp_pool_refused_by_the_account_policy_is_unavailable_not_exhausted():
    """Dispatch refuses the seat outright (every account excluded, or invalid settings):
    a stale route-level quota cooldown from an earlier pool call proves nothing now."""
    for setting, value in (
        ("REVIEW_OMP_ACCOUNTS_EXCLUDE", "a@x.com,b@x.com"),
        ("REVIEW_OMP_MAX_USAGE_PERCENT", "not-a-number"),
    ):
        with _env(live={OPUS_OMP}, omp_accounts=OMP_POOL):
            os.environ[setting] = value
            _record_omp_route_quota()
            _record_omp_account_quota(2)
            report = _report([OPUS_OMP])
        (cand,) = report["candidates"]
        assert cand["status"] == "unavailable", (setting, cand)
        assert cand["reason"] == "account_policy_refusal", (setting, cand)
        assert cand["source"] == "omp-account-policy"
        assert report["all_exhausted"] is False


def test_omp_single_account_pool_reads_that_accounts_own_cooldown():
    """One enabled omp account still dispatches through the pool, which records its limit
    under the account's key: that evidence must not be missed."""
    with _env(live={OPUS_OMP}, omp_accounts=((2, "a@x.com"),)):
        _record_omp_account_quota(2)
        report = _report([OPUS_OMP])
    (cand,) = report["candidates"]
    assert cand["account"] == "a@x.com" and cand["status"] == "exhausted", cand
    assert report["all_exhausted"] is True


def test_omp_single_account_typed_stall_on_the_route_outranks_quota_on_the_account():
    with _env(live={OPUS_OMP}, omp_accounts=((2, "a@x.com"),)):
        _record_omp_account_quota(2)
        sc.record_cooldown(
            OPUS_OMP,
            "true-silence timeout",
            access_method=backends._OMP_ACCESS_METHOD,
            now=NOW - 10,
            ttl_seconds=3600,
            cause=sc.CAUSE_STALL,
        )
        report = _report([OPUS_OMP])
    (cand,) = report["candidates"]
    assert cand["status"] == "unavailable" and cand["cause"] == "stall", cand
    assert report["all_exhausted"] is False


def test_omp_single_account_route_keeps_route_level_evidence():
    """One account (or no agent.db): the route IS the account, so the route's own quota
    cooldown stays attributable."""
    for pool in (None, ((2, "a@x.com"),)):
        with _env(live={OPUS_OMP}, omp_accounts=pool):
            _record_omp_route_quota()
            report = _report([OPUS_OMP])
        assert report["all_exhausted"] is True, (pool, report["candidates"])


def test_omp_account_failures_carry_their_cooldown_cause():
    def result(stderr, returncode=1):
        return backends.ReviewResult(
            model=OPUS_OMP,
            command="omp",
            returncode=returncode,
            stdout="",
            stderr=stderr,
        )

    limit = backends._omp_account_failure(
        result("429 rate_limit_error retry-after-ms=5000")
    )
    assert limit is not None and limit[1] == sc.CAUSE_QUOTA
    chronic = backends._omp_account_failure(result("You've hit your session limit"))
    assert chronic is not None and chronic[1] == sc.CAUSE_QUOTA
    login = backends._omp_account_failure(result("OAuth session expired, log in again"))
    assert login is not None and login[1] == sc.CAUSE_AUTH
    sentinel = backends._omp_account_failure(
        result("Claude Fable 5 is currently unavailable.")
    )
    assert sentinel is not None and sentinel[1] == sc.CAUSE_UNAVAILABLE
    assert backends._omp_account_failure(result("", returncode=0)) is None


def test_login_failed_account_is_unavailable_not_exhausted():
    cards = [
        _card("Claude Code", "a@x.com", _window(100)),
        _card("Claude Code", "b@x.com", _window(100)),
    ]
    with _env(live={OPUS}, cards=cards, accounts=("a@x.com", "b@x.com")):
        accts.record_login_failure(_account_dir(1), "OAuth session expired")
        report = _report([OPUS])
    by_account = {c["account"]: c for c in report["candidates"]}
    assert by_account["b@x.com"]["status"] == "unavailable"
    assert by_account["b@x.com"]["reason"] == "login_failed"
    assert by_account["b@x.com"]["cause"] == "auth"
    assert report["all_exhausted"] is False


def test_unrunnable_backend_is_unavailable():
    with _env(live=()):
        report = _report(["codex"])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"]) == ("unavailable", "backend_unavailable")
    assert report["all_exhausted"] is False


# ---- freshness --------------------------------------------------------------------


def test_stale_quota_cooldown_is_unknown():
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS,
            "session limit / usage credits",
            access_method="cli",
            now=NOW - 2 * 3600,
            ttl_seconds=8 * 3600,
            cause=sc.CAUSE_QUOTA,
        )
        report = _report([OPUS])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"]) == ("unknown", "stale_observation"), cand
    assert report["all_exhausted"] is False


def test_max_age_override_is_honored():
    with _env(live={OPUS}):
        sc.record_cooldown(
            OPUS,
            "session limit / usage credits",
            access_method="cli",
            now=NOW - 2 * 3600,
            ttl_seconds=8 * 3600,
            cause=sc.CAUSE_QUOTA,
        )
        os.environ["REVIEW_CAPACITY_MAX_AGE_SECONDS"] = str(3 * 3600)
        report = _report([OPUS])
    assert report["max_age_seconds"] == 3 * 3600
    assert report["all_exhausted"] is True


def test_stale_harness_file_is_unknown():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "limits.json"
        path.write_text(
            json.dumps({"cards": [_card("Codex", "c@x.com", _window(100))]}),
            encoding="utf-8",
        )
        old = NOW - 3 * 3600
        os.utime(path, (old, old))
        with _env(live={"codex"}, harness_raw=str(path)):
            report = _report(["codex"])
    assert report["sources"]["harness_limits"]["origin"] == "env-file"
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"]) == ("unknown", "stale_observation"), cand
    assert report["all_exhausted"] is False


def test_window_that_already_reset_is_not_exhaustion():
    cards = [_card("Codex", "c@x.com", _window(100, resets_at=NOW - 60))]
    with _env(live={"codex"}, cards=cards):
        report = _report(["codex"])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"]) == ("unknown", "stale_observation"), cand


def test_codex_card_exhausted_until_reset():
    reset = "2099-01-01T00:00:00Z"
    cards = [_card("Codex", "c@x.com", _window(100, seconds=WEEK, resets_at=reset))]
    with _env(live={"codex"}, cards=cards):
        report = _report(["codex"])
    cand = report["candidates"][0]
    assert cand["status"] == "exhausted" and cand["quota_identity"] == "codex:c@x.com"
    assert "2099-01-01" in cand["detail"]
    assert report["all_exhausted"] is True
    assert report["exhausted"][0]["model"] == "codex"
    assert report["exhausted"][0]["source"] == "harness-limits"


def test_two_codex_cards_with_one_healthy_are_ambiguous():
    cards = [
        _card("Codex", "c@x.com", _window(100)),
        _card("Codex", "d@x.com", _window(10)),
    ]
    with _env(live={"codex"}, cards=cards):
        report = _report(["codex"])
    assert report["candidates"][0]["reason"] == "ambiguous_account"
    assert report["all_exhausted"] is False


# ---- malformed results / missing sources -------------------------------------------


def test_malformed_harness_json_is_reported_and_unknown():
    with _env(live={"codex"}, harness_raw="{not json"):
        report = _report(["codex"])
    assert report["sources"]["harness_limits"]["status"] == "malformed"
    cand = report["candidates"][0]
    assert cand["status"] == "unknown" and "malformed" in cand["detail"], cand
    assert report["all_exhausted"] is False


def test_harness_card_not_ok_is_unknown():
    cards = [_card("Claude Code", "a@x.com", status="missing_creds")]
    with _env(live={OPUS}, cards=cards, accounts=("a@x.com",)):
        report = _report([OPUS])
    cand = report["candidates"][0]
    assert (cand["status"], cand["reason"]) == ("unknown", "harness_card_not_ok"), cand


def test_missing_harness_binary_is_reported_and_unknown():
    saved = ul._run_harness_limits_json

    def _missing():
        raise FileNotFoundError("harness")

    ul._run_harness_limits_json = _missing
    try:
        with _env(live={"codex"}):
            os.environ.pop("REVIEW_HARNESS_LIMITS_JSON", None)
            report = _report(["codex"])
    finally:
        ul._run_harness_limits_json = saved
    assert report["sources"]["harness_limits"]["status"] == "missing"
    cand = report["candidates"][0]
    assert cand["reason"] == "no_observation" and "missing" in cand["detail"], cand
    assert report["all_exhausted"] is False


def test_malformed_cooldown_store_and_corrupt_cause_never_count():
    with _env(live={OPUS}) as d:
        store = d / "seat-cooldown.json"
        store.write_text("{broken", encoding="utf-8")
        report = _report([OPUS])
        assert report["sources"]["seat_cooldown"]["status"] == "malformed"
        assert report["all_exhausted"] is False
        store.write_text(
            json.dumps(
                {
                    OPUS: {
                        "cli": {
                            "until": NOW + 3600,
                            "reason": "session limit / usage credits",
                            "recorded_at": NOW - 10,
                            "fail_count": 1,
                            "cause": ["quota"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        report = _report([OPUS])
    assert report["sources"]["seat_cooldown"]["status"] == "ok"
    assert report["candidates"][0]["reason"] == "cooldown_unclassified"
    assert report["all_exhausted"] is False


# ---- the versioned contract ------------------------------------------------------


def test_report_schema_is_versioned_and_complete():
    cards = [_card("Codex", "c@x.com", _window(100))]
    with _env(live={"codex"}, cards=cards):
        report = _report(["codex"], preset=None)
    assert report["schema"] == "review-cli/capacity-check"
    assert report["schema_version"] == 2
    assert set(report) == {
        "schema",
        "schema_version",
        "checked_at",
        "max_age_seconds",
        "configuration",
        "sources",
        "candidates",
        "coverage",
        "models",
        "exhausted",
        "all_exhausted",
        "error",
    }
    conf = report["configuration"]
    assert conf["source"] == "config.yaml (models:)" and conf["preset"] is None
    assert conf["board_fingerprint"].startswith("sha256:")
    assert conf["seats"][0]["model"] == "codex"
    assert set(report["sources"]) == {
        "harness_limits",
        "seat_cooldown",
        "claude_accounts",
    }
    assert set(report["candidates"][0]) == {
        "seat_model",
        "route",
        "backend",
        "access_method",
        "account",
        "quota_identity",
        "quota_identity_basis",
        "status",
        "reason",
        "cause",
        "source",
        "observed_at",
        "age_seconds",
        "detail",
    }
    assert set(report["coverage"]) == {
        "candidates",
        "exhausted",
        "usable",
        "unavailable",
        "unknown",
        "complete",
    }
    assert set(cc.REASONS.values()) == set(cc.STATUSES)
    json.dumps(report)  # serializable as-is


def test_report_names_the_evaluated_configuration():
    with _env(live={OPUS, "codex"}):
        light = cc.board_capacity_report(config={}, now=NOW)
        heavy = cc.board_capacity_report(config={}, preset="heavy", now=NOW)
        models = _report([OPUS])
    assert light["configuration"]["source"] == f"level:{default_level_name()}"
    assert heavy["configuration"]["source"] == "level:deep"
    assert heavy["configuration"]["preset"] == "deep"
    assert (
        light["configuration"]["board_fingerprint"]
        != heavy["configuration"]["board_fingerprint"]
    )
    assert models["configuration"]["source"] == "config.yaml (models:)"


def test_resolve_review_board_precedence():
    board_cfg = {"board": [{"model": "codex", "role": "correctness"}]}
    _, source, preset = resolve_review_board(board_cfg)
    assert (source, preset) == ("config.yaml (board:)", None)
    both = {**board_cfg, "models": [OPUS]}
    board, source, _ = resolve_review_board(both)
    assert source == "config.yaml (models:)" and board[0].model == OPUS
    _, source, preset = resolve_review_board(both, preset="heavy")
    assert (source, preset) == ("level:deep", "deep")
    _, source, preset = resolve_review_board({})
    assert (source, preset) == (f"level:{default_level_name()}", default_level_name())
    try:
        resolve_review_board({"board": [{"model": OPUS, "role": "nope"}]})
    except BoardConfigError:
        pass
    else:
        raise AssertionError("unknown role must raise BoardConfigError")


# ---- typed cooldown causes ------------------------------------------------------


def test_cooldown_cause_round_trips_and_rejects_unknown_values():
    with _env():
        sc.record_cooldown("m", "x", access_method="cli", ttl_seconds=60, cause="quota")
        assert sc.active_cooldown("m", access_method="cli")["cause"] == "quota"
        sc.record_cooldown(
            "n", "x", access_method="cli", ttl_seconds=60, cause="db-locked"
        )
        assert sc.active_cooldown("n", access_method="cli")["cause"] is None
        sc.record_cooldown("o", "x", access_method="cli", ttl_seconds=60)
        assert sc.active_cooldown("o", access_method="cli")["cause"] is None


def test_chronic_reasons_map_to_typed_causes():
    assert backends._chronic_cause(backends._QUOTA_COOLDOWN_REASON) == sc.CAUSE_QUOTA
    assert (
        backends._chronic_cause(backends._SENTINEL_COOLDOWN_REASON)
        == sc.CAUSE_UNAVAILABLE
    )
    quota = backends.ReviewResult(
        model=OPUS,
        command=[],
        returncode=1,
        stdout="",
        stderr="You've hit your session limit · resets 7pm",
    )
    assert (
        backends._chronic_cause(backends._chronic_unavailable_reason(quota)) == "quota"
    )
    sentinel = backends.ReviewResult(
        model=OPUS,
        command=[],
        returncode=0,
        stdout="Claude Fable 5 is currently unavailable.",
        stderr="",
    )
    assert (
        backends._chronic_cause(backends._chronic_unavailable_reason(sentinel))
        == "unavailable"
    )


def test_login_failures_are_recorded_with_the_auth_cause():
    with _env(accounts=("a@x.com",)):
        accts.record_login_failure(_account_dir(0), "OAuth session expired")
        failure = accts.login_failure(_account_dir(0))
    assert failure is not None and failure["cause"] == sc.CAUSE_AUTH


def test_every_production_cooldown_recorder_passes_a_cause():
    """A recorder without `cause=` would silently write unclassified cooldowns; pin that
    every `record_cooldown(...)` call in reviewlib names one."""
    missing = []
    for path in (REPO_ROOT / "reviewlib").rglob("*.py"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # other modules' escapes
            tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "record_cooldown":
                continue
            if not any(kw.arg == "cause" for kw in node.keywords):
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not missing, f"record_cooldown without cause=: {missing}"


# ---- CLI -------------------------------------------------------------------------


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli._dispatch(argv)
    return rc, buf.getvalue()


def test_cli_capacity_check_json_prints_the_versioned_report():
    with _env(live={"codex"}):
        rc, out = _run_cli(["capacity-check", "--json", "--preset", "heavy"])
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["schema_version"] == cc.SCHEMA_VERSION
    assert payload["configuration"]["source"] == "level:deep"
    assert payload["all_exhausted"] is False


def test_cli_capacity_check_text():
    with _env(live={"codex"}):
        rc, out = _run_cli(["capacity-check", "--preset", "heavy"])
    assert rc == 0, out
    assert "level:deep" in out and "all_exhausted=false" in out, out


def test_help_capacity_topic_documents_the_contract():
    rc, out = _run_cli(["help", "capacity"])
    assert rc == 0
    for needle in (
        "schema_version 2",
        "all_exhausted = at least one candidate AND every candidate",
        "REVIEW_CAPACITY_MAX_AGE_SECONDS",
        "SOURCE LIMITS",
        "COMPATIBILITY",
        "cooldown_not_account_scoped",
    ):
        assert needle in out, needle


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
