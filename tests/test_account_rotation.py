#!/usr/bin/env python3
"""Regression tests for review-cli#168/#170's ACCOUNT-ROTATION fallback loops:
`reviewlib.backends._review_claude_cli_with_account_fallback` (direct `claude:*`
CLI seats, rotating `CLAUDE_CONFIG_DIR` across the `~/.claude-accounts` registry) and
`_review_omp_with_account_fallback` (omp-hosted Claude seats, rotating omp
`--profile`).

The bug these pin (Alex's hatch-request incident, 2026-09-09): ONE of this machine's
N registered Claude accounts reporting 100% usage (or returning the chronic
unavailable sentinel) used to block EVERY claude:* seat, even though the other
accounts were fine. The loops must SKIP an exhausted/errored account and try the
next one in `claude_accounts.rotation_order()`'s wrap order.

Hermetic: subprocess-dispatch mocking follows this repo's existing convention
(test_usage_limits.py / test_omp_backend.py — direct attribute save/restore on
`reviewlib.backends`, never a real Popen), env vars are set with save/restore so the
standalone `__main__` runner is as isolated as pytest's conftest fixtures, and the
fake chronic result is pinned against the REAL `_chronic_unavailable_reason`
detector so the loops' genuine gate is what drives rotation. No network, no
subprocess, no real account registry.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

_MODEL = "claude:claude-sonnet-5"
# A wording straight from backends._UNAVAILABLE_MARKERS — the rc=0 administrative
# sentinel shape a paywalled/exhausted Claude account actually returns.
_CHRONIC_STDOUT = "Claude Sonnet 5 is currently unavailable"


def _chronic(model: str, tag: str = "") -> ReviewResult:
    """A result the REAL `_chronic_unavailable_reason` must classify as chronic:
    rc=0, short body carrying the unavailable sentinel, empty stderr."""
    return ReviewResult(
        model=model,
        command="fake",
        returncode=0,
        stdout=f"{tag}{_CHRONIC_STDOUT}",
        stderr="",
    )


def _usable(model: str, text: str = "a real review verdict") -> ReviewResult:
    return ReviewResult(
        model=model, command="fake", returncode=0, stdout=text, stderr=""
    )


@contextlib.contextmanager
def _patched_backends(**attrs):
    """Direct attribute save/restore on reviewlib.backends — the same mocking
    convention test_usage_limits.py and test_omp_backend.py use."""
    saved = {name: getattr(backends, name) for name in attrs}
    for name, value in attrs.items():
        setattr(backends, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(backends, name, value)


@contextlib.contextmanager
def _isolated_env(**overrides: str):
    saved = {key: os.environ.get(key) for key in overrides}
    for key, value in overrides.items():
        os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _hermetic_env() -> dict:
    """Fresh temp dirs for every side-effecting path the loops can touch: an EMPTY
    `~/.claude-accounts` registry (mirrors conftest's `_isolate_claude_accounts_root` for
    the standalone runner), a throwaway log dir (`_usage_limit_skip_result` emits a
    sidecar log), and a throwaway seat-cooldown store (`review_claude` consults it).
    """
    tmp = tempfile.mkdtemp()
    return {
        "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(Path(tmp) / "claude-accounts"),
        "REVIEW_LOG_DIR": tmp,
        "REVIEW_SEAT_COOLDOWN_FILE": str(Path(tmp) / "seat-cooldown.json"),
    }


def _registry_with_emails(*names: str, current: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    for name in names:
        account_dir = root / name
        account_dir.mkdir()
        (account_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": f"{name}@example.com"}}),
            encoding="utf-8",
        )
    if current is not None:
        (root / "current").write_text(current + "\n", encoding="utf-8")
    return root


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


# --- the fake chronic shape must trip the REAL detector ----------------------


def test_fake_results_match_the_real_chronic_detector():
    # Every rotation test below drives the loops' genuine gate with these fakes —
    # pin them against the real classifier so the tests can't silently rotate on a
    # shape the product would never treat as chronic (or skip one it would).
    assert backends._chronic_unavailable_reason(_chronic(_MODEL)) is not None
    assert backends._chronic_unavailable_reason(_usable(_MODEL)) is None


# --- the claude CLI account-rotation loop -------------------------------------

_TWO_CANDIDATES = [("/cfg/one", "one@example.com"), ("/cfg/two", "two@example.com")]


def test_cli_fallback_rotates_past_chronic_account_to_next():
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return _chronic(model) if config_dir == "/cfg/one" else _usable(model)

    with (
        _patched_backends(
            _claude_account_candidates=lambda: list(_TWO_CANDIDATES),
            allow_spawn_for_model=lambda model, account=None: True,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    assert calls == ["/cfg/one", "/cfg/two"], calls
    assert result.stdout == "a real review verdict"


def _run_cli_fallback_with_first_account_returning(first_result):
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return first_result if config_dir == "/cfg/one" else _usable(model)

    with (
        _patched_backends(
            _claude_account_candidates=lambda: list(_TWO_CANDIDATES),
            allow_spawn_for_model=lambda model, account=None: True,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    return calls, result


def test_cli_fallback_rotates_past_logged_out_account_to_next():
    """review-cli#588: on 2026-09-29 the `current` account (account-2) was logged out —
    the claude CLI printed "Failed to authenticate: OAuth session expired and could not
    be refreshed" (exit 1) — while the other registered accounts were fine. Stopping
    there killed every claude:* seat; the next account must be tried."""
    logged_out = backends.ReviewResult(
        _MODEL,
        "claude --print",
        1,
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "",
    )
    calls, result = _run_cli_fallback_with_first_account_returning(logged_out)
    assert calls == ["/cfg/one", "/cfg/two"], calls
    assert result.stdout == "a real review verdict"


def test_cli_fallback_stops_on_a_plain_seat_failure():
    """A non-auth, non-quota failure (a crash, a bad model) is not the account's fault:
    rotating would only burn the other accounts' quota on the same failure."""
    crashed = backends.ReviewResult(_MODEL, "claude --print", 1, "", "segfault")
    calls, result = _run_cli_fallback_with_first_account_returning(crashed)
    assert calls == ["/cfg/one"], calls
    assert result is crashed


def test_cli_fallback_skips_exhausted_account_without_dispatching_it():
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return _usable(model)

    def fake_allow(model, account=None):
        return account != "one@example.com"

    with (
        _patched_backends(
            _claude_account_candidates=lambda: list(_TWO_CANDIDATES),
            allow_spawn_for_model=fake_allow,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    # The usage-exhausted account must never even be dispatched — its seat is
    # skipped pre-Popen, and the next account in rotation order answers instead.
    assert calls == ["/cfg/two"], calls
    assert result.stdout == "a real review verdict"


def test_cli_fallback_all_accounts_exhausted_returns_skip_naming_every_account():
    calls = []

    def fake_cli(*_a, **_k):
        calls.append(1)
        return _usable(_MODEL)

    with (
        _patched_backends(
            _claude_account_candidates=lambda: list(_TWO_CANDIDATES),
            allow_spawn_for_model=lambda model, account=None: False,
            spawn_refuse_reason=lambda model, account=None: (
                "harness 5-hour window exhausted (100%)"
            ),
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    assert calls == [], "an exhausted account was still dispatched"
    assert result.returncode == 0
    assert "usage-limit skip" in result.command
    assert "all 2 configured accounts" in result.stdout, result.stdout
    assert "one@example.com" in result.stdout and "two@example.com" in result.stdout


def test_cli_fallback_every_account_chronic_returns_the_last_result():
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return _chronic(model, tag=f"{config_dir}: ")

    with (
        _patched_backends(
            _claude_account_candidates=lambda: list(_TWO_CANDIDATES),
            allow_spawn_for_model=lambda model, account=None: True,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    assert calls == ["/cfg/one", "/cfg/two"], calls
    assert result.stdout.startswith("/cfg/two: "), result.stdout


def test_cli_fallback_single_implicit_account_dispatches_once_with_none_config_dir():
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return _usable(model)

    with (
        _patched_backends(
            _claude_account_candidates=lambda: [(None, None)],
            allow_spawn_for_model=lambda model, account=None: True,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**_hermetic_env()),
    ):
        result = backends._review_claude_cli_with_account_fallback(
            _MODEL, "prompt", "diff", Path("."), 60, 1, effort=None
        )
    # No ~/.claude-accounts registry -> exactly one (None, None) candidate, one
    # dispatch, config_dir untouched: byte-identical to the pre-#168 behaviour.
    assert calls == [None], calls
    assert result.stdout == "a real review verdict"


# --- _claude_account_candidates against a real (throwaway) registry ----------


def test_claude_account_candidates_empty_registry_is_single_implicit_account():
    with _isolated_env(**_hermetic_env()):
        assert backends._claude_account_candidates() == [(None, None)]


def test_claude_account_candidates_follow_rotation_order_with_emails():
    root = _registry_with_emails(
        "account-1", "account-2", "account-3", current="account-2"
    )
    env = _hermetic_env()
    env["REVIEW_CLAUDE_ACCOUNTS_ROOT"] = str(root)
    with _isolated_env(**env):
        candidates = backends._claude_account_candidates()
    assert candidates == [
        (str(root / "account-2"), "account-2@example.com"),
        (str(root / "account-3"), "account-3@example.com"),
        (str(root / "account-1"), "account-1@example.com"),
    ], candidates


# --- the full review_claude path: the review-cli#170 regression ----------------


def test_review_claude_advances_to_next_account_when_current_is_exhausted():
    """The incident shape, end to end through the REAL review_claude: the CURRENT
    account's 5-hour harness window is at 100%, the second registered account is
    fine. Pre-#168 the single aggregate gate returned a usage-limit skip without
    Popen; now the current account is skipped and the seat dispatches as the NEXT
    account in rotation order, with ITS CLAUDE_CONFIG_DIR."""
    root = _registry_with_emails("account-1", "account-2", current="account-1")
    cards = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 5 * 3600)],
            account="account-1@example.com",
        )
    ]
    calls = []

    def fake_cli(
        model, prompt, diff, cwd, timeout, round_no, *, effort=None, config_dir=None
    ):
        calls.append(config_dir)
        return _usable(model)

    env = _hermetic_env()
    env.update(
        {
            "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(root),
            "REVIEW_HARNESS_LIMITS_JSON": json.dumps({"cards": cards}),
            "REVIEW_CLAUDE_MODE": "cli",
            "REVIEW_USAGE_LIMITS_FILE": str(root / "missing-usage.json"),
        }
    )
    with (
        _patched_backends(
            unpaid_provider_result=lambda *_a, **_k: None,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**env),
    ):
        result = backends.review_claude(_MODEL, "prompt", "diff", Path("."), 60)
    assert calls == [str(root / "account-2")], calls
    assert result.stdout == "a real review verdict"
    assert "usage-limit skip" not in result.command


def test_review_claude_skips_every_seat_shape_only_when_all_accounts_exhausted():
    """Same end-to-end path, but BOTH registered accounts are at 100%: rotation has
    nothing left to try, so the synthetic usage-limit skip (no Popen) is still the
    correct outcome — and it must say so for every account."""
    root = _registry_with_emails("account-1", "account-2", current="account-1")
    cards = [
        _harness_card(
            "Claude Code",
            [_window("Session", 100.0, 5 * 3600)],
            account="account-1@example.com",
        ),
        _harness_card(
            "Claude Code",
            [_window("weekly", 100.0, 7 * 24 * 3600)],
            account="account-2@example.com",
        ),
    ]
    calls = []

    def fake_cli(*_a, **_k):
        calls.append(1)
        return _usable(_MODEL)

    env = _hermetic_env()
    env.update(
        {
            "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(root),
            "REVIEW_HARNESS_LIMITS_JSON": json.dumps({"cards": cards}),
            "REVIEW_CLAUDE_MODE": "cli",
            "REVIEW_USAGE_LIMITS_FILE": str(root / "missing-usage.json"),
        }
    )
    with (
        _patched_backends(
            unpaid_provider_result=lambda *_a, **_k: None,
            review_claude_cli=fake_cli,
        ),
        _isolated_env(**env),
    ):
        result = backends.review_claude(_MODEL, "prompt", "diff", Path("."), 60)
    assert calls == [], "an exhausted account was still dispatched"
    assert result.returncode == 0
    assert "usage-limit skip" in result.command
    assert "account-1@example.com" in result.stdout
    assert "account-2@example.com" in result.stdout


# --- the omp-hosted Claude profile-rotation loop -----------------------------

_OMP_MODEL = "omp:anthropic/claude-sonnet-5"


def test_omp_fallback_rotates_profiles_past_chronic_result():
    tried = []

    def fake_once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile
    ):
        tried.append(profile)
        return _chronic(model) if profile == "main" else _usable(model)

    with _patched_backends(
        _review_omp_claude_profiles=lambda: ["main", "alt"],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_account_fallback(
            _OMP_MODEL,
            "anthropic/claude-sonnet-5",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == ["main", "alt"], tried
    assert result.stdout == "a real review verdict"


def test_omp_fallback_every_profile_chronic_returns_the_last_result():
    tried = []

    def fake_once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile
    ):
        tried.append(profile)
        return _chronic(model, tag=f"{profile}: ")

    with _patched_backends(
        _review_omp_claude_profiles=lambda: ["main", "alt"],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_account_fallback(
            _OMP_MODEL,
            "anthropic/claude-sonnet-5",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == ["main", "alt"], tried
    assert result.stdout.startswith("alt: "), result.stdout


def test_omp_fallback_default_profile_list_is_single_none():
    tried = []

    def fake_once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile
    ):
        tried.append(profile)
        return _usable(model)

    with _patched_backends(
        _review_omp_claude_profiles=lambda: [None],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_account_fallback(
            _OMP_MODEL,
            "anthropic/claude-sonnet-5",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == [None], tried
    assert result.stdout == "a real review verdict"


# --- the pure helpers behind the omp loop -------------------------------------


def test_omp_model_hosts_claude_detection():
    assert backends._omp_model_hosts_claude("anthropic/claude-sonnet-5") is True
    assert backends._omp_model_hosts_claude("Anthropic/Claude-Opus-4-8") is True
    assert backends._omp_model_hosts_claude("some-proxy/claude-x") is True
    assert backends._omp_model_hosts_claude(None) is False
    assert backends._omp_model_hosts_claude("") is False
    assert backends._omp_model_hosts_claude("opencode-go/mimo-v2.5") is False
    assert backends._omp_model_hosts_claude("kimi-code/k3-256k") is False


def test_review_omp_claude_profiles_env_parsing():
    key = "REVIEW_OMP_CLAUDE_PROFILES"
    saved = os.environ.get(key)
    try:
        os.environ.pop(key, None)
        assert backends._review_omp_claude_profiles() == [None]
        os.environ[key] = "   "
        assert backends._review_omp_claude_profiles() == [None]
        os.environ[key] = "main, alt ,,  third "
        assert backends._review_omp_claude_profiles() == ["main", "alt", "third"]
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


# --- the omp-hosted openai-codex credential-rotation loop --------------------

_OMP_CODEX_MODEL = "omp:openai-codex/gpt-5.6-terra"


def test_omp_codex_fallback_rotates_credentials_past_chronic_result():
    tried = []

    def fake_once(
        model,
        omp_model,
        prompt,
        diff,
        cwd,
        timeout,
        round_no,
        effort,
        profile,
        credential_id=None,
    ):
        tried.append(credential_id)
        return _chronic(model) if credential_id == 12 else _usable(model)

    with _patched_backends(
        _review_omp_openai_codex_credentials=lambda: [12, 15],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_codex_credential_fallback(
            _OMP_CODEX_MODEL,
            "openai-codex/gpt-5.6-terra",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == [12, 15], tried
    assert result.stdout == "a real review verdict"


def test_omp_codex_fallback_every_credential_chronic_returns_the_last_result():
    tried = []

    def fake_once(
        model,
        omp_model,
        prompt,
        diff,
        cwd,
        timeout,
        round_no,
        effort,
        profile,
        credential_id=None,
    ):
        tried.append(credential_id)
        return _chronic(model, tag=f"{credential_id}: ")

    with _patched_backends(
        _review_omp_openai_codex_credentials=lambda: [12, 15],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_codex_credential_fallback(
            _OMP_CODEX_MODEL,
            "openai-codex/gpt-5.6-terra",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == [12, 15], tried
    assert result.stdout.startswith("15: "), result.stdout


def test_omp_codex_fallback_single_credential_dispatches_once_without_pin():
    tried = []

    def fake_once(
        model,
        omp_model,
        prompt,
        diff,
        cwd,
        timeout,
        round_no,
        effort,
        profile,
        credential_id=None,
    ):
        tried.append(credential_id)
        return _usable(model)

    with _patched_backends(
        _review_omp_openai_codex_credentials=lambda: [12],
        _review_omp_once=fake_once,
    ):
        result = backends._review_omp_with_codex_credential_fallback(
            _OMP_CODEX_MODEL,
            "openai-codex/gpt-5.6-terra",
            "prompt",
            "diff",
            Path("."),
            60,
            1,
            None,
        )
    assert tried == [None], tried
    assert result.stdout == "a real review verdict"


def test_omp_model_hosts_openai_codex_detection():
    assert backends._omp_model_hosts_openai_codex("openai-codex/gpt-5.6-terra") is True
    assert backends._omp_model_hosts_openai_codex("OpenAI-Codex/gpt-5.6-terra") is True
    assert backends._omp_model_hosts_openai_codex(None) is False
    assert backends._omp_model_hosts_openai_codex("") is False
    assert backends._omp_model_hosts_openai_codex("anthropic/claude-sonnet-5") is False
    assert backends._omp_model_hosts_openai_codex("kimi-code/k3-256k") is False


def test_review_omp_openai_codex_credentials_reads_auth_db():
    db = Path(tempfile.mkdtemp()) / "agent.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, "
        "credential_type TEXT NOT NULL, data TEXT NOT NULL, disabled_cause TEXT)"
    )
    conn.execute(
        "INSERT INTO auth_credentials (id, provider, credential_type, data, disabled_cause) "
        "VALUES (3, 'openai-codex', 'oauth', '{}', NULL)"
    )
    conn.execute(
        "INSERT INTO auth_credentials (id, provider, credential_type, data, disabled_cause) "
        "VALUES (7, 'openai-codex', 'oauth', '{}', 'revoked')"
    )
    conn.execute(
        "INSERT INTO auth_credentials (id, provider, credential_type, data, disabled_cause) "
        "VALUES (9, 'openai-codex', 'oauth', '{}', NULL)"
    )
    conn.commit()
    conn.close()
    key = backends._OMP_AUTH_DB_ENV
    saved = os.environ.get(key)
    try:
        os.environ[key] = str(db)
        assert backends._review_omp_openai_codex_credentials() == [3, 9]
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


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
