"""Pytest-wide test-isolation guard for process-global backend state.

WHY: several suites drive the real `cli._dispatch` (the `--retry`, staged-diff, no-repo,
mode-subcommand paths). `_dispatch` runs `load_config()` +
`backends.configure_unpaid_providers(config['unpaid_providers'])` against the REAL
`~/.config/review-cli/config.yaml`, which sets the process-wide `_CONFIG_UNPAID_PROVIDERS`
(e.g. {commandcode, gemini}). It also lazily fills the payment-preflight cache. Neither is
reset when the test ends, so the leaked "commandcode is unpaid" state silently marked
`oc:commandcode/...` seats DEAD in a LATER suite's availability assertions
(test_reviewer_board), and the same class of leak repeatedly broke the provider-failover /
pool-guard suites. Individual per-test restores are whack-a-mole across 18 `_dispatch`
callers; this autouse fixture snapshots the two globals before every test and restores them
after, so no test can leak backend-availability state into another. It only saves+restores
(never mutates during the test), so it can't hide a real product bug.

Scope: pytest only. The plain-script `__main__` harnesses (which CI's smoke.py also drives)
don't load conftest; the suites that leak via their own `_dispatch` calls additionally
restore inline so the script path is covered too.
"""

from __future__ import annotations

import pytest

import reviewlib.backends as _backends
import reviewlib.provider_failover as _pf
from _brainstorm_env_isolation import _ISOLATED_ENV_KEYS


@pytest.fixture(autouse=True)
def _isolate_backend_global_state():
    saved_unpaid = _backends._CONFIG_UNPAID_PROVIDERS
    saved_preflight = dict(_backends._PAYMENT_PREFLIGHT_CACHE)
    # cli._dispatch also calls provider_failover.configure_provider_chains(config['provider_chains']),
    # mutating the process-wide _CONFIG_OVERRIDES. Snapshot it too so a _dispatch-driving test
    # with a `provider_chains:` config can't leak the override map into a later suite (same leak
    # class this fixture exists to close).
    saved_chain_overrides = dict(_pf._CONFIG_OVERRIDES)
    try:
        yield
    finally:
        _backends._CONFIG_UNPAID_PROVIDERS = saved_unpaid
        _backends._PAYMENT_PREFLIGHT_CACHE.clear()
        _backends._PAYMENT_PREFLIGHT_CACHE.update(saved_preflight)
        _pf._CONFIG_OVERRIDES = saved_chain_overrides


@pytest.fixture(autouse=True)
def _isolate_log_and_stats_paths(tmp_path, monkeypatch):
    """Default EVERY test to a throwaway `$REVIEW_LOG_DIR` / `$REVIEW_STATS_FILE`.

    Root cause of the dashboard's "TOPIC: topic" / M1,M2 junk (Alex, 2026-09-02): most
    suites that drive the REAL `mode_brainstorm` deliberately redirect `REVIEW_LOG_DIR`
    to a throwaway temp dir per-test — but 5 call sites across `test_diff_cap.py` and
    `test_brainstorm_diff.py` called `mode_brainstorm(...)` with NO such override
    (`log_dir()` then falls back to the real per-user path, `~/Library/Logs/review-cli`).
    Every unisolated `pytest` run of this repo's OWN suite (run constantly across the
    many parallel worktrees while iterating — not any external scheduled job) wrote a
    real `{ts}-brainstorm.md` with a literal placeholder topic ("topic") and generic
    "m1"/"m2" model labels straight into the dashboard's live log dir. Those 5 call
    sites now redirect explicitly too (`tests/_brainstorm_env_isolation.py`, imported
    for its `_ISOLATED_ENV_KEYS`, is the single source of truth for which env vars that
    helper AND this fixture isolate — keep them in sync by editing that one list, not
    by hand-editing both places).

    This fixture closes the class, not just the known instances: every test now gets
    an isolated `REVIEW_LOG_DIR`/`REVIEW_STATS_FILE` by default, so a FUTURE test that
    forgets to redirect them can no longer touch the developer's real files. A test that
    still wants its own explicit temp dir (most already do) is unaffected — it just
    overrides this default within its own scope and monkeypatch restores this fixture's
    value afterward, same as it always restored the pre-test value.
    """
    for key in _ISOLATED_ENV_KEYS:
        monkeypatch.setenv(key, str(tmp_path / key.lower()))


@pytest.fixture(autouse=True)
def _isolate_claude_accounts_root(tmp_path, monkeypatch):
    """Default EVERY test to a throwaway `$REVIEW_CLAUDE_ACCOUNTS_ROOT` (review-cli#168).

    `reviewlib.claude_accounts.accounts_root()` falls back to this DEVELOPER MACHINE's
    real `~/.claude-accounts/account-0/1/2` registry when the env var is unset. Without
    this fixture, any suite that drives the real account-rotation loop (`review_claude`'s
    `_claude_account_candidates`) would pick up whatever accounts happen to be registered
    on the machine running the tests -- non-deterministic, and a real credential-bearing
    dir the test suite has no business touching. Point every test at an empty, unique
    `tmp_path` subdir instead; a test that wants a populated registry builds one under
    this same dir explicitly.
    """
    monkeypatch.setenv("REVIEW_CLAUDE_ACCOUNTS_ROOT", str(tmp_path / "claude-accounts"))


@pytest.fixture(autouse=True)
def _isolate_omp_accounts(tmp_path, monkeypatch):
    """Default EVERY test to a missing omp agent.db, no omp launcher overrides and no
    omp account policy (review-cli#636/#637/#641). An omp:anthropic seat reads its account
    list from the auth db and fetches each account's token with `omp token`; against this
    developer machine's real ~/.omp/agent/agent.db a suite would run real token commands
    and depend on which accounts are logged in here. A test that wants accounts builds its
    own db and points `OMP_AUTH_DB` at it."""
    monkeypatch.setenv("OMP_AUTH_DB", str(tmp_path / "omp-agent" / "agent.db"))
    for key in (
        "REVIEW_OMP_BIN",
        "OMP_REAL_BIN",
        "PI_INSTALL_DIR",
        "REVIEW_OMP_ACCOUNTS_EXCLUDE",
        "REVIEW_OMP_MAX_USAGE_PERCENT",
    ):
        monkeypatch.delenv(key, raising=False)
    saved = _backends._CONFIG_OMP_BIN, _backends._CONFIG_OMP_ACCOUNTS
    _backends.configure_omp_accounts(None)
    yield
    _backends._CONFIG_OMP_BIN, _backends._CONFIG_OMP_ACCOUNTS = saved


@pytest.fixture(autouse=True)
def _isolate_seat_cooldown(tmp_path, monkeypatch):
    """Default EVERY test to a throwaway seat-cooldown store (review-cli#663). A failed seat
    call now records an availability window (and a skipped seat is read back from it), so
    a suite that drives `backends.call_backend` against a failing stub must not write into
    — or be excluded by — the developer's real ~/.config/review-cli/seat-cooldown.json."""
    monkeypatch.setenv(
        "REVIEW_SEAT_COOLDOWN_FILE", str(tmp_path / "seat-cooldown.json")
    )
    # The automatic pre-dispatch health check would probe the stub CLIs/servers a test
    # drives and consume their scripted answers; tests/test_health.py turns it on itself.
    monkeypatch.setenv("REVIEW_HEALTH_PREFLIGHT", "0")


@pytest.fixture(autouse=True)
def _isolate_admission(tmp_path, monkeypatch):
    """Default EVERY test to host-wide admission OFF, with a throwaway registry dir
    (review-cli#614). Admission reads the REAL host load/memory and coordinates through a
    shared registry, so a suite left on it would queue behind reviews actually running on
    the developer's machine. tests/test_admission.py turns it on explicitly."""
    from reviewlib import admission

    monkeypatch.setenv("REVIEW_ADMISSION", "0")
    monkeypatch.setenv("REVIEW_ADMISSION_DIR", str(tmp_path / "admission"))
    admission.reset_for_tests()
    yield
    admission.reset_for_tests()


@pytest.fixture(autouse=True)
def _disable_taskref_backend_validation(monkeypatch):
    """Default every test to permissive task-code shape checks (review-cli#427).

    Backend validation via ``agenttools_taskref`` is opt-in under pytest so the
    existing synthetic placeholders (``TEST-1``, ``CODE``, …) keep working without
    mocking ``gh``. Tests that exercise validation set ``REVIEW_TASKREF_VALIDATE=1``
    explicitly.
    """
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "0")
