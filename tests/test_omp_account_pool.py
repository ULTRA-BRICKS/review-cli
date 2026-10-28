#!/usr/bin/env python3
"""omp seats inside the sandbox (review-cli#636) and omp's Anthropic account pool
(review-cli#637).

#636: an omp seat runs with HOME set to an empty sandbox dir. This machine's `omp` on
PATH is a wrapper script that finds the real binary as `$HOME/.local/lib/omp/omp`, so
every omp seat exited 127. review-cli now resolves the real binary from the real
environment and hands it to the seat as OMP_REAL_BIN; a wrapper that still cannot start
under a sandboxed HOME makes the seat unusable before dispatch (SKIPPED on the board),
not an exit 127 mid-run.

#637: omp keeps several Anthropic OAuth accounts in agent.db. An omp:anthropic seat now
tries them one at a time (agent.db read read-only, token from `omp token anthropic
--account N`, the attempt pinned by an empty agent dir plus ANTHROPIC_OAUTH_TOKEN), moves
on after a 429 rate limit, names the account that answered, and `review seats` lists each
account's availability.

Unit tests patch module attributes (no real omp). The end-to-end tests run the real
`bin/review` against a fake `omp`: a wrapper script on a private PATH with the same
layout as ~/.local/bin/omp, a fake real binary under the temp HOME's
`.local/lib/omp/omp`, and a fake agent.db. No model is called.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends, omp_accounts, seat_cooldown  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.panel import format_result  # noqa: E402

_MODEL = "omp:anthropic/claude-sonnet-5"
_SELECTOR = "anthropic/claude-sonnet-5"
_LIMIT_STDERR = (
    'Working...\n429 {"type":"error","error":{"type":"rate_limit_error","message":'
    '"This request would exceed your account\'s rate limit. Please try again later."},'
    '"request_id":"req_1"} retry-after-ms=3600000'
)
_LOADER_ENV = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")


def _make_agent_db(path: Path, *, blocks: list[tuple[int, str, int]] = ()) -> Path:
    """A fake omp agent.db: Anthropic OAuth accounts a@ (id 2) and b@ (id 3), a disabled
    Anthropic account (id 4), an openai-codex account (id 5), an Anthropic API key
    (id 7), and omp rate-limit blocks (credential_id, scope, blocked_until_ms)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "provider TEXT NOT NULL, credential_type TEXT NOT NULL, data TEXT NOT NULL, "
            "disabled_cause TEXT DEFAULT NULL, identity_key TEXT DEFAULT NULL)"
        )
        conn.execute(
            "CREATE TABLE auth_credential_blocks (credential_id INTEGER NOT NULL, "
            "provider_key TEXT NOT NULL, block_scope TEXT NOT NULL DEFAULT '', "
            "blocked_until_ms INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        rows = [
            (2, "anthropic", "oauth", None, "email:a@example.com|org:o1"),
            (3, "anthropic", "oauth", None, "email:b@example.com|org:o2"),
            (4, "anthropic", "oauth", "revoked", "email:c@example.com|org:o3"),
            (5, "openai-codex", "oauth", None, "email:a@example.com|acct:x"),
            (7, "anthropic", "api_key", None, None),
        ]
        for cred_id, provider, kind, disabled, identity in rows:
            conn.execute(
                "INSERT INTO auth_credentials (id, provider, credential_type, data, "
                'disabled_cause, identity_key) VALUES (?, ?, ?, \'{"access":"secret"}\', ?, ?)',
                (cred_id, provider, kind, disabled, identity),
            )
        for cred_id, scope, until in blocks:
            conn.execute(
                "INSERT INTO auth_credential_blocks VALUES (?, 'anthropic:oauth', ?, ?, 0)",
                (cred_id, scope, until),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@contextlib.contextmanager
def _env(**values: str | None):
    saved = {k: os.environ.get(k) for k in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _patched(module, **attrs):
    saved = {name: getattr(module, name) for name in attrs}
    for name, value in attrs.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


@contextlib.contextmanager
def _pool_world(blocks: list[tuple[int, str, int]] = ()):
    """A fake agent.db + cooldown store; the launcher resolves to a fake native path."""
    with tempfile.TemporaryDirectory(prefix="review-637-") as d:
        root = Path(d)
        db = _make_agent_db(root / "agent.db", blocks=list(blocks))
        with (
            _env(
                OMP_AUTH_DB=str(db),
                REVIEW_SEAT_COOLDOWN_FILE=str(root / "cooldown.json"),
                REVIEW_SEAT_COOLDOWN_SECONDS=None,
                REVIEW_OMP_CLAUDE_PROFILES=None,
                REVIEW_OMP_BIN=None,
                OMP_REAL_BIN=None,
                REVIEW_OMP_ACCOUNTS_EXCLUDE=None,
                REVIEW_OMP_MAX_USAGE_PERCENT=None,
                REVIEW_LOG_DIR=str(root / "logs"),
            ),
            _patched(backends, _which=lambda name: f"/fake/bin/{name}"),
        ):
            yield root


def _limited(model: str) -> ReviewResult:
    return ReviewResult(
        model=model, command="omp", returncode=1, stdout="", stderr=_LIMIT_STDERR
    )


def _answer(model: str, text: str) -> ReviewResult:
    return ReviewResult(
        model=model, command="omp", returncode=0, stdout=text, stderr=""
    )


# ---- agent.db discovery -----------------------------------------------------------------


def test_anthropic_accounts_lists_enabled_oauth_rows_in_omp_order_with_blocks():
    now_ms = 1_000_000
    with tempfile.TemporaryDirectory() as d:
        db = _make_agent_db(
            Path(d) / "agent.db",
            blocks=[
                (3, "", now_ms + 60_000),
                (2, "", now_ms - 1),
                (2, "fable", now_ms + 5),
            ],
        )
        before = db.read_bytes()
        accounts = omp_accounts.anthropic_accounts(db, now_ms=now_ms)
        assert db.read_bytes() == before, "the read-only probe changed agent.db"
    assert [(a.credential_id, a.position, a.email) for a in accounts] == [
        (2, 1, "a@example.com"),
        (3, 2, "b@example.com"),
    ]
    a, b = accounts
    assert a.blocks == (("fable", now_ms + 5),), "an expired block must not count"
    assert a.blocked_until_ms("anthropic/claude-sonnet-5") is None
    assert a.blocked_until_ms("anthropic/claude-fable-5") == now_ms + 5
    assert b.blocked_until_ms("anthropic/claude-sonnet-5") == now_ms + 60_000


def test_anthropic_accounts_missing_or_corrupt_db_is_none():
    with tempfile.TemporaryDirectory() as d:
        assert omp_accounts.anthropic_accounts(Path(d) / "absent.db") is None
        junk = Path(d) / "junk.db"
        junk.write_text("not sqlite", encoding="utf-8")
        assert omp_accounts.anthropic_accounts(junk) is None


def test_dispatch_order_puts_blocked_and_limited_accounts_last():
    a = omp_accounts.OmpAccount(2, 1, "a@example.com", (("", 9_000_000),))
    b = omp_accounts.OmpAccount(3, 2, "b@example.com")
    c = omp_accounts.OmpAccount(5, 3, "c@example.com")
    d = omp_accounts.OmpAccount(6, 4, "d@example.com")
    order = omp_accounts.dispatch_order([a, b, c, d], _SELECTOR, {3: 5_000.0})
    assert [x.email for x in order] == [
        "c@example.com",
        "d@example.com",
        "b@example.com",  # review-cli saw it limited until t=5000 s
        "a@example.com",  # omp blocked it until t=9000 s
    ]


def test_limit_failure_reads_the_real_omp_429_line_only_on_a_failed_run():
    line = omp_accounts.limit_failure(1, "", _LIMIT_STDERR)
    assert line is not None and "rate_limit_error" in line
    assert omp_accounts.retry_after_seconds("", _LIMIT_STDERR) == 3600.0
    assert omp_accounts.limit_failure(0, "", _LIMIT_STDERR) is None
    review = "A real review that quotes a 429 rate limit handler. " * 20
    assert omp_accounts.limit_failure(1, review, "") is None, (
        "a long body is not scanned"
    )


# ---- the pool loop ----------------------------------------------------------------------


def _fake_once(calls: list[tuple[int | None, str | None]], limited_ids: set[int]):
    def once(
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
        anthropic_account=None,
        oauth_token=None,
        launcher=None,
    ):
        cred = anthropic_account.credential_id if anthropic_account else None
        calls.append((cred, oauth_token))
        if cred in limited_ids:
            return _limited(model)
        return _answer(model, f"answer from {cred}")

    return once


def _fake_token(fetched: list[int]):
    def token(launcher, account):
        fetched.append(account.position)
        return f"tok-{account.credential_id}", ""

    return token


def test_pool_moves_to_the_next_account_after_a_rate_limit_and_names_who_answered():
    calls: list = []
    fetched: list = []
    with (
        _pool_world(),
        _patched(backends, _review_omp_once=_fake_once(calls, {2})),
        _patched(omp_accounts, access_token=_fake_token(fetched)),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
        cooled = seat_cooldown.active_cooldown(
            "omp-account:anthropic:2", access_method=backends._OMP_ACCESS_METHOD
        )
        first_fetched = list(fetched)
        # the next call starts on the account that answered, not the limited one
        calls.clear()
        backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
        second_order = [cred for cred, _tok in calls]
    assert result.returncode == 0 and result.stdout == "answer from 3"
    assert first_fetched == [1, 2], first_fetched
    assert result.account.startswith("b@example.com (omp anthropic account 2 of 2)")
    assert "failed before it: a@example.com: 429" in result.account
    assert cooled is not None and 3500 < cooled["remaining_seconds"] <= 3600
    assert second_order == [3], second_order
    rendered = format_result(result)
    assert (
        "> [review-cli] account: b@example.com (omp anthropic account 2 of 2)"
        in rendered
    )


def test_pool_pins_each_attempt_to_the_token_of_its_account():
    calls: list = []
    with (
        _pool_world(),
        _patched(backends, _review_omp_once=_fake_once(calls, {2})),
        _patched(omp_accounts, access_token=_fake_token([])),
    ):
        backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert calls == [(2, "tok-2"), (3, "tok-3")], calls


def test_pool_every_account_limited_returns_the_last_failure_with_a_summary():
    calls: list = []
    with (
        _pool_world(),
        _patched(backends, _review_omp_once=_fake_once(calls, {2, 3})),
        _patched(omp_accounts, access_token=_fake_token([])),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert [cred for cred, _ in calls] == [2, 3]
    assert result.returncode == 1
    assert "every omp anthropic account failed (2)" in result.stderr
    assert result.account.startswith("none answered")


def test_pool_skips_an_account_whose_token_cannot_be_fetched():
    calls: list = []

    def token(launcher, account):
        if account.credential_id == 2:
            return None, "`omp token anthropic --account 1`: refresh failed"
        return "tok-3", ""

    with (
        _pool_world(),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=token),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert calls == [(3, "tok-3")]
    assert result.stdout == "answer from 3"
    assert (
        "a@example.com: `omp token anthropic --account 1`: refresh failed"
        in result.account
    )


def test_pool_starts_with_accounts_omp_has_not_blocked():
    calls: list = []
    far = int(time.time() * 1000) + 3_600_000
    with (
        _pool_world(blocks=[(2, "", far)]),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=_fake_token([])),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert [cred for cred, _ in calls] == [3]
    assert result.account == "b@example.com (omp anthropic account 2 of 2)"


def test_non_anthropic_claude_selectors_and_explicit_profiles_keep_the_old_path():
    assert backends._omp_selector_is_anthropic("anthropic/claude-opus-4-8")
    assert backends._omp_selector_is_anthropic("claude-sonnet-5")
    assert not backends._omp_selector_is_anthropic("some-proxy/claude-x")
    assert not backends._omp_selector_is_anthropic("kimi-code/k3")
    tried = []

    def once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile, **kw
    ):
        tried.append((profile, kw.get("oauth_token")))
        return _answer(model, "ok")

    with (
        _pool_world(),
        _env(REVIEW_OMP_CLAUDE_PROFILES="main"),
        _patched(backends, _review_omp_once=once),
    ):
        backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert tried == [("main", None)], tried


def test_no_accounts_in_agent_db_dispatches_once_unpinned():
    tried = []

    def once(
        model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile, **kw
    ):
        tried.append(kw.get("oauth_token"))
        return _answer(model, "ok")

    with _pool_world() as root:
        with (
            _env(OMP_AUTH_DB=str(root / "absent.db")),
            _patched(backends, _review_omp_once=once),
        ):
            result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert tried == [None] and result.account == ""


# ---- the seat env of a pinned attempt -----------------------------------------------


class _Capture:
    def __init__(self):
        self.env = None
        self.argv = None
        self.header = None
        self.agent_dir_entries = None

    def __call__(
        self,
        argv,
        cwd,
        timeout,
        backend,
        round_no=0,
        announce=False,
        header_argv0=None,
        env=None,
        true_silence_timeout=None,
    ):
        self.argv, self.env, self.header = list(argv), dict(env), header_argv0
        agent = Path(env["PI_CODING_AGENT_DIR"])
        self.agent_dir_entries = (
            sorted(p.name for p in agent.iterdir()) if agent.is_dir() else None
        )

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


def test_pinned_attempt_runs_with_an_empty_agent_dir_and_the_account_token():
    cap = _Capture()
    account = omp_accounts.OmpAccount(3, 2, "b@example.com")
    with _pool_world(), _env(ANTHROPIC_OAUTH_TOKEN="leaked-from-parent"):
        with _patched(backends, _run_streamed=cap):
            backends._review_omp_once(
                _MODEL,
                _SELECTOR,
                "p",
                "d",
                Path("."),
                60,
                0,
                None,
                None,
                anthropic_account=account,
                oauth_token="tok-3",
            )
            unpinned = _Capture()
            backends._run_streamed = unpinned
            backends._review_omp_once(
                _MODEL, _SELECTOR, "p", "d", Path("."), 60, 0, None, None
            )
    assert cap.env["ANTHROPIC_OAUTH_TOKEN"] == "tok-3"
    assert cap.agent_dir_entries == [], "a pinned attempt must see no stored credential"
    assert Path(cap.env["PI_CODING_AGENT_DIR"]).parent == Path(cap.env["HOME"]).parent
    assert cap.header == f"omp -m {_SELECTOR} account=b@example.com"
    assert "tok-3" not in " ".join(cap.argv), (
        "the token must never be on the command line"
    )
    assert "ANTHROPIC_OAUTH_TOKEN" not in unpinned.env, (
        "a parent token must not leak in"
    )
    assert unpinned.env["PI_CODING_AGENT_DIR"] == str(backends._omp_agent_dir())


# ---- the launcher (#636) ------------------------------------------------------------


_WRAPPER = """\
#!/bin/sh
# same layout as ~/.local/bin/omp: the real binary is found through $HOME
REAL_OMP="${OMP_REAL_BIN:-$HOME/.local/lib/omp/omp}"
if [ ! -x "$REAL_OMP" ]; then
    echo "omp wrapper: real binary not found at $REAL_OMP" >&2
    exit 127
fi
exec "$REAL_OMP" "$@"
"""


def _fake_real_omp(path: Path) -> Path:
    """A fake real omp: `--version`, `token anthropic --account N` (prints tok-N),
    `usage --json` ($FAKE_OMP_USAGE when set, else a fixed report), and `-p` runs that
    answer, or fail with omp's real 429 line for a token listed in $FAKE_OMP_LIMITED.
    Every call is appended to $FAKE_OMP_LOG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""\
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            def record(**kw):
                with open(os.environ["FAKE_OMP_LOG"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(kw) + "\\n")
            if args[:1] == ["--version"]:
                print("omp/0.0.0-fake")
                sys.exit(0)
            if args[:2] == ["token", "anthropic"]:
                n = args[args.index("--account") + 1]
                record(kind="token", account=n, home=os.environ.get("HOME"))
                print("tok-" + n)
                sys.exit(0)
            if args[:1] == ["usage"] and os.environ.get("FAKE_OMP_USAGE"):
                print(os.environ["FAKE_OMP_USAGE"])
                sys.exit(0)
            if args[:1] == ["usage"]:
                print(json.dumps({{"reports": [
                    {{"provider": "anthropic", "metadata": {{"email": "a@example.com"}},
                      "limits": [{{"label": "Claude 7 Day", "amount": {{"usedFraction": 1.0}},
                                  "window": {{"resetsAt": 1792292399734}}}}]}},
                    {{"provider": "anthropic", "metadata": {{"email": "b@example.com"}},
                      "limits": [{{"label": "Claude 5 Hour", "amount": {{"usedFraction": 0.1}},
                                  "window": {{}}}}]}}]}}))
                sys.exit(0)
            if "-p" in args:
                token = os.environ.get("ANTHROPIC_OAUTH_TOKEN")
                agent = Path(os.environ["PI_CODING_AGENT_DIR"])
                record(kind="run", token=token, home=os.environ["HOME"],
                       real_bin=os.environ.get("OMP_REAL_BIN"),
                       agent_dir=sorted(p.name for p in agent.iterdir()))
                if token in os.environ.get("FAKE_OMP_LIMITED", "").split(","):
                    sys.stderr.write({_LIMIT_STDERR!r} + "\\n")
                    sys.exit(1)
                print("ANSWER served with " + str(token))
                sys.exit(0)
            sys.exit(2)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class _World:
    """A temp HOME with the fake real omp at .local/lib/omp/omp, the wrapper on a private
    PATH, a fake agent.db and every review-cli state path in the temp dir."""

    def __init__(self, root: Path, *, install_real: bool = True):
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.work = root / "work"
        for d in (self.home, self.bin, self.work):
            d.mkdir(parents=True)
        self.real = self.home / ".local" / "lib" / "omp" / "omp"
        if install_real:
            _fake_real_omp(self.real)
        wrapper = self.bin / "omp"
        wrapper.write_text(_WRAPPER, encoding="utf-8")
        wrapper.chmod(0o755)
        self.db = _make_agent_db(root / "agent" / "agent.db")
        self.log = root / "omp-calls.jsonl"

    def env(self, **extra: str) -> dict[str, str]:
        state = self.root / "state"
        return {
            **{k: os.environ[k] for k in _LOADER_ENV if k in os.environ},
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "OMP_AUTH_DB": str(self.db),
            "OMP_NO_SCOPE": "1",
            "FAKE_OMP_LOG": str(self.log),
            "REVIEW_ADMISSION": "0",
            "REVIEW_HEALTH_PREFLIGHT": "0",
            "REVIEW_ADMISSION_DIR": str(state / "admission"),
            "REVIEW_LOG_DIR": str(state / "logs"),
            "REVIEW_STATS_FILE": str(state / "stats.jsonl"),
            "REVIEW_SEAT_COOLDOWN_FILE": str(state / "cooldown.json"),
            "REVIEW_PROVIDER_CACHE": str(state / "last-provider.json"),
            "REVIEW_USAGE_LIMITS_DIR": str(state / "usage"),
            "REVIEW_JOBS_DIR": str(state / "jobs"),
            "REVIEW_CLAUDE_ACCOUNTS_ROOT": str(state / "claude-accounts"),
            **extra,
        }

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def review(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REVIEW_BIN), *args],
            cwd=str(self.work),
            env=self.env(**extra),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )


@contextlib.contextmanager
def _world(**kw):
    with tempfile.TemporaryDirectory(prefix="review-636-") as d:
        yield _World(Path(d), **kw)


def test_wrapper_launcher_gets_the_real_binary_from_the_real_home():
    with (
        _world() as world,
        _env(HOME=str(world.home), OMP_REAL_BIN=None, PI_INSTALL_DIR=None),
    ):
        launcher = omp_accounts.launcher_for(str(world.bin / "omp"), source="PATH")
        assert launcher.real_bin == str(world.real.resolve())
        assert launcher.seat_env() == {
            "OMP_REAL_BIN": str(world.real.resolve()),
            "PI_INSTALL_DIR": str(world.real.resolve().parent),
        }
        assert omp_accounts.launcher_problem(launcher) is None
        native = omp_accounts.launcher_for(str(world.real), source="PATH")
        assert native.real_bin is None, "a native binary runs as is"


def test_wrapper_with_no_real_binary_is_unusable_before_dispatch():
    with (
        _world(install_real=False) as world,
        _env(
            HOME=str(world.home),
            OMP_REAL_BIN=None,
            PI_INSTALL_DIR=None,
            REVIEW_OMP_BIN=None,
        ),
        _patched(backends, _which=lambda name: str(world.bin / name)),
    ):
        reason = backends.backend_unavailable_reason(_MODEL)
    assert (
        reason is not None and "exits 127 under the seat's sandboxed HOME" in reason
    ), reason
    assert "real binary not found" in reason


def test_explicit_omp_bin_override_must_be_executable():
    with _env(REVIEW_OMP_BIN="/nonexistent/omp"):
        try:
            backends._omp_launcher()
        except RuntimeError as exc:
            assert "$REVIEW_OMP_BIN /nonexistent/omp is not an executable file" in str(
                exc
            )
        else:
            raise AssertionError("a missing REVIEW_OMP_BIN must make omp unusable")


def test_config_omp_bin_is_used_and_env_override_wins():
    with tempfile.TemporaryDirectory() as d:
        config_bin = _fake_real_omp(Path(d) / "config-omp")
        env_bin = _fake_real_omp(Path(d) / "env-omp")
        saved = backends._CONFIG_OMP_BIN
        try:
            backends.configure_omp_bin(str(config_bin))
            with _env(REVIEW_OMP_BIN=None):
                launcher = backends._omp_launcher()
                assert (launcher.argv0, launcher.source) == (
                    str(config_bin),
                    "config.yaml omp_bin",
                )
            with _env(REVIEW_OMP_BIN=str(env_bin)):
                assert backends._omp_launcher().argv0 == str(env_bin)
            backends.configure_omp_bin(42)
            assert backends._CONFIG_OMP_BIN is None, "a non-path value unsets the key"
        finally:
            backends._CONFIG_OMP_BIN = saved


def test_identity_with_control_characters_is_not_printed():
    assert omp_accounts._identity_email("email:a@b.com|org:x") == "a@b.com"
    assert (
        omp_accounts._identity_email("email:a@b.com\n> [review-cli] ok|org:x") is None
    )
    assert omp_accounts.OmpAccount(9, 1, None).label == "credential #9"


def test_malformed_omp_usage_json_reads_as_unknown_usage_not_a_crash():
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / "omp"
        fake.write_text(
            "#!/bin/sh\n"
            'echo \'{"reports":[{"provider":"anthropic","metadata":"malformed"},'
            '{"provider":"anthropic","metadata":{"email":"a@b.c"},"limits":"x"},'
            '{"provider":"anthropic","metadata":{"email":"d@e.f"},'
            '"limits":[1,{"amount":"x"},{"amount":{"usedFraction":true}}]}]}\'\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        launcher = omp_accounts.OmpLauncher(
            argv0=str(fake), real_bin=None, source="PATH"
        )
        assert omp_accounts.usage_by_email(launcher) == {"a@b.c": [], "d@e.f": []}


def test_malformed_block_rows_and_huge_retry_after_do_not_crash():
    with tempfile.TemporaryDirectory() as d:
        db = _make_agent_db(Path(d) / "agent.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO auth_credential_blocks VALUES (2, 'anthropic:oauth', 1, 'x', 0)"
        )
        conn.execute(
            "INSERT INTO auth_credential_blocks VALUES (3, 'anthropic:oauth', 7, 9e999, 0)"
        )
        conn.commit()
        conn.close()
        accounts = omp_accounts.anthropic_accounts(db, now_ms=0)
    assert [a.blocks for a in accounts] == [(), ()], accounts
    huge = "429 rate_limit_error retry-after-ms=" + "9" * 5000
    assert omp_accounts.retry_after_seconds("", huge) is None
    assert omp_accounts.limit_failure(1, "", huge) is not None


def test_wrapper_that_ignores_omp_real_bin_is_unusable_even_with_a_real_binary():
    with (
        _world() as world,
        _env(HOME=str(world.home), OMP_REAL_BIN=None, PI_INSTALL_DIR=None),
    ):
        wrapper = world.bin / "omp"
        wrapper.write_text(
            '#!/bin/sh\nREAL="$HOME/.local/lib/omp/omp"\n[ -x "$REAL" ] || exit 127\n'
            'exec "$REAL" "$@"\n',
            encoding="utf-8",
        )
        launcher = omp_accounts.launcher_for(str(wrapper), source="PATH")
        assert launcher.real_bin is not None
        problem = omp_accounts.launcher_problem(launcher)
    assert problem is not None and "exits 127" in problem, problem


# ---- end to end: the real bin/review against the fake omp ----------------------------


def test_e2e_just_ask_rotates_past_a_limited_account_through_the_wrapper():
    with _world() as world:
        proc = world.review(
            "just-ask",
            "Say hi",
            "--task",
            "637",
            "-m",
            _MODEL,
            "--timeout",
            "60",
            FAKE_OMP_LIMITED="tok-1",
            # a is the emptiest account, so it is tried first (review-cli#709), is limited
            # and the seat rotates on to b
            FAKE_OMP_USAGE=_usage_report(a=0.1, b=0.5),
        )
        calls = world.calls()
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "ANSWER served with tok-2" in proc.stdout, out
    assert (
        "account: b@example.com (omp anthropic account 2 of 2); failed before it: "
        "a@example.com: 429" in proc.stdout
    ), out
    runs = [c for c in calls if c["kind"] == "run"]
    assert [r["token"] for r in runs] == ["tok-1", "tok-2"], calls
    for run in runs:
        assert run["home"] != str(world.home), (
            "the seat must run under the sandbox HOME"
        )
        assert run["real_bin"] == str(world.real.resolve()), (
            "OMP_REAL_BIN not handed over"
        )
        assert run["agent_dir"] == [], "a pinned attempt must see no stored credential"
    tokens = [c for c in calls if c["kind"] == "token"]
    assert [t["account"] for t in tokens] == ["1", "2"]
    assert all(t["home"] == str(world.home) for t in tokens), (
        "token fetch runs in the real HOME"
    )


def test_e2e_seats_lists_launcher_and_each_account_availability():
    far = int(time.time() * 1000) + 86_400_000
    with _world() as world:
        _make_agent_db(world.root / "blocked" / "agent.db", blocks=[(2, "", far)])
        proc = world.review(
            "seats", OMP_AUTH_DB=str(world.root / "blocked" / "agent.db")
        )
        as_json = world.review(
            "doctor", "--json", OMP_AUTH_DB=str(world.root / "blocked" / "agent.db")
        )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = proc.stdout
    assert "OMP ANTHROPIC ACCOUNTS" in text, text
    assert f"launcher: {world.bin / 'omp'} (PATH; wrapper, real binary" in text, text
    a_line = next(line for line in text.splitlines() if "a@example.com" in line)
    b_line = next(line for line in text.splitlines() if "b@example.com" in line)
    assert "RATE-LIMITED until" in a_line and "7 Day 100% (resets" in a_line, a_line
    assert "available" in b_line and "5 Hour 10%" in b_line, b_line
    assert "c@example.com" not in text, "a disabled account is not in the pool"
    payload = json.loads(as_json.stdout)
    accounts = payload["omp"]["anthropic_accounts"]
    assert [(a["email"], a["available"]) for a in accounts] == [
        ("a@example.com", False),
        ("b@example.com", True),
    ]


def test_e2e_show_board_marks_omp_seats_skipped_when_the_wrapper_cannot_start():
    with _world(install_real=False) as world:
        proc = world.review("--show-board")
    line = next((row for row in proc.stdout.splitlines() if _MODEL in row), None)
    assert line is not None, proc.stdout + proc.stderr
    assert "SKIPPED" in line, line


# ---- the account policy: exclude list + usage threshold (#641) ----------------------


@contextlib.contextmanager
def _omp_accounts_config(raw):
    """config.yaml `omp_accounts:` = `raw` for the duration (as the CLI would set it)."""
    saved = backends._CONFIG_OMP_ACCOUNTS
    backends.configure_omp_accounts(raw)
    try:
        yield
    finally:
        backends.configure_omp_accounts(saved)


def _window(label: str, used: float, *, in_ms: int | None = 3_600_000, tier=None):
    resets = None if in_ms is None else int(time.time() * 1000) + in_ms
    return omp_accounts.UsageWindow(label, used, resets, tier)


def _usage(per_email: dict):
    reads: list[int] = []

    def usage(launcher):
        reads.append(1)
        return per_email

    usage.reads = reads
    return usage


def test_account_policy_reads_config_and_env_overrides_win():
    raw = {
        "exclude": ["A@Example.com"],
        "max_usage_percent": 90,
        "accounts": {"b@example.com": {"max_usage_percent": 95}},
    }
    policy = omp_accounts.account_policy(raw, {})
    assert policy.problems == ()
    assert policy.exclude == ("a@example.com",)
    assert policy.exclude_source == "config.yaml omp_accounts.exclude"
    assert policy.threshold_for("B@example.com") == 95, "per-account wins over global"
    assert policy.threshold_for("c@example.com") == 90
    env = {
        omp_accounts.EXCLUDE_ENV: " x@y.z , ,X@y.z",
        omp_accounts.MAX_USAGE_ENV: "80",
    }
    overridden = omp_accounts.account_policy(raw, env)
    assert overridden.exclude == ("x@y.z",)
    assert overridden.exclude_source == "$REVIEW_OMP_ACCOUNTS_EXCLUDE"
    assert overridden.max_usage_percent == 80
    assert overridden.max_usage_source == "$REVIEW_OMP_MAX_USAGE_PERCENT"
    assert overridden.threshold_for("b@example.com") == 95
    blank = omp_accounts.account_policy(
        raw, {omp_accounts.EXCLUDE_ENV: " ", omp_accounts.MAX_USAGE_ENV: ""}
    )
    assert blank.exclude == ("a@example.com",) and blank.max_usage_percent == 90
    assert not omp_accounts.account_policy(None, {}).restricts


def test_account_policy_reports_every_invalid_value():
    raw = {
        "exclude": "a@example.com",
        "max_usage_percent": "90%",
        "accounts": {"b@example.com": {"max_usage_percent": 0}, "c@x": 95},
        "max_usage": 90,
    }
    policy = omp_accounts.account_policy(raw, {})
    text = "\n".join(policy.problems)
    assert len(policy.problems) == 5, text
    for needle in (
        "unknown key max_usage",
        "omp_accounts.exclude must be a list",
        "omp_accounts.max_usage_percent must be a number",
        "accounts.b@example.com.max_usage_percent must be a number",
        "accounts.c@x must be {max_usage_percent: N}",
    ):
        assert needle in text, (needle, text)
    assert policy.exclude == () and policy.max_usage_percent is None
    assert policy.restricts, "an invalid policy must never let omp pick an account"
    # an env override replaces the config value, so the config value is not judged; an
    # invalid env value is reported itself
    env = {omp_accounts.EXCLUDE_ENV: "z@z.z", omp_accounts.MAX_USAGE_ENV: "ninety"}
    overridden = omp_accounts.account_policy(raw, env)
    text = "\n".join(overridden.problems)
    assert len(overridden.problems) == 4, text
    assert (
        "omp_accounts.exclude" not in text
        and "omp_accounts.max_usage_percent" not in text
    )
    assert "$REVIEW_OMP_MAX_USAGE_PERCENT must be a number" in text, text
    assert overridden.exclude == ("z@z.z",)
    valid_env = omp_accounts.account_policy(
        {"exclude": "a@x", "max_usage_percent": "90"},
        {omp_accounts.EXCLUDE_ENV: "b@x", omp_accounts.MAX_USAGE_ENV: "80"},
    )
    assert valid_env.problems == () and valid_env.max_usage_percent == 80
    for bad in (True, 101, -5, float("nan"), [90], "90"):
        assert omp_accounts.account_policy({"max_usage_percent": bad}, {}).problems
    assert omp_accounts.account_policy(["a@x"], {}).problems


def test_env_exclude_without_an_email_keeps_the_config_list():
    raw = {"exclude": ["secret@example.com"]}
    for value in (",", " , ", " "):
        policy = omp_accounts.account_policy(raw, {omp_accounts.EXCLUDE_ENV: value})
        assert policy.exclude == ("secret@example.com",), (value, policy)
        assert policy.exclude_source == "config.yaml omp_accounts.exclude"


def test_skip_reason_threshold_reset_tier_and_unknown_usage():
    now_ms = int(time.time() * 1000)
    a = omp_accounts.OmpAccount(2, 1, "a@example.com")
    b = omp_accounts.OmpAccount(3, 2, "b@example.com")
    policy = omp_accounts.account_policy(
        {
            "exclude": ["c@example.com"],
            "max_usage_percent": 90,
            "accounts": {"b@example.com": {"max_usage_percent": 95}},
        },
        {},
    )
    over = policy.skip_reason(a, [_window("Claude 5 Hour", 0.9)], _SELECTOR, now_ms)
    assert over is not None and over.startswith(
        "OVER LIMIT (5 Hour 90% >= 90%, resets "
    )
    assert (
        policy.skip_reason(a, [_window("Claude 5 Hour", 0.89)], _SELECTOR, now_ms)
        is None
    )
    # compared unrounded: just under the threshold is under it
    assert (
        policy.skip_reason(
            a, [_window("Claude 5 Hour", 0.899999999)], _SELECTOR, now_ms
        )
        is None
    )
    at_29 = omp_accounts.account_policy({"max_usage_percent": 29}, {})
    assert at_29.skip_reason(a, [_window("Claude 5 Hour", 0.29)], None, now_ms)
    reset = [_window("Claude 7 Day", 1.0, in_ms=-1000)]
    assert policy.skip_reason(a, reset, _SELECTOR, now_ms) is None, (
        "window already reset"
    )
    unknown_reset = policy.skip_reason(
        a, [_window("Claude 7 Day", 0.95, in_ms=None)], None, now_ms
    )
    assert unknown_reset is not None and "reset time unknown" in unknown_reset
    assert policy.skip_reason(a, None, _SELECTOR, now_ms) is None, "unknown usage"
    assert (
        policy.skip_reason(b, [_window("Claude 5 Hour", 0.92)], _SELECTOR, now_ms)
        is None
    )
    fable = [_window("Claude 7 Day (Fable)", 0.97, tier="fable")]
    assert policy.skip_reason(a, fable, _SELECTOR, now_ms) is None
    assert policy.skip_reason(a, fable, "anthropic/claude-fable-5", now_ms) is not None
    assert policy.tier_over_limit(a, fable, now_ms) == (
        f"fable OVER LIMIT (7 Day (Fable) 97% >= 90%, resets "
        f"{omp_accounts.utc_stamp(fable[0].resets_at_ms)})",
    )
    c = omp_accounts.OmpAccount(5, 3, "C@example.com")
    assert policy.skip_reason(c, None, _SELECTOR, now_ms) == (
        "EXCLUDED (config.yaml omp_accounts.exclude)"
    )
    assert policy.tier_over_limit(c, fable, now_ms) == ()


def test_usage_reads_tiers_and_matches_emails_case_insensitively():
    resets = int(time.time() * 1000) + 3_600_000
    report = {
        "reports": [
            {
                "provider": "anthropic",
                "metadata": {"email": "User@Example.com"},
                "limits": [
                    {
                        "label": "Claude 7 Day (Fable)",
                        "scope": {"windowId": "7d", "tier": "fable"},
                        "amount": {"usedFraction": 0.2},
                        "window": {"resetsAt": resets},
                    },
                    {
                        "label": "Claude 5 Hour",
                        "scope": "malformed",
                        "amount": {"usedFraction": 0.95},
                        "window": {"resetsAt": resets},
                    },
                ],
            }
        ]
    }
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / "omp"
        fake.write_text(f"#!/bin/sh\necho '{json.dumps(report)}'\n", encoding="utf-8")
        fake.chmod(0o755)
        launcher = omp_accounts.OmpLauncher(
            argv0=str(fake), real_bin=None, source="PATH"
        )
        usage = omp_accounts.usage_by_email(launcher)
    assert usage == {
        "user@example.com": [
            omp_accounts.UsageWindow("Claude 7 Day (Fable)", 0.2, resets, "fable"),
            omp_accounts.UsageWindow("Claude 5 Hour", 0.95, resets, None),
        ]
    }, usage
    account = omp_accounts.OmpAccount(2, 1, "USER@example.COM")
    policy = omp_accounts.account_policy({"max_usage_percent": 90}, {})
    refusal, eligible, skipped = omp_accounts.pool_refusal(
        policy, [account], Path("/x"), usage, _SELECTOR, int(time.time() * 1000)
    )
    assert eligible == [] and "OVER LIMIT (5 Hour 95% >= 90%" in skipped[0], skipped
    assert refusal is not None and refusal.startswith(
        "no omp anthropic account may be used (1): USER@example.COM: OVER LIMIT"
    ), refusal


def test_policy_fails_closed_without_emails_or_without_accounts():
    now_ms = int(time.time() * 1000)
    nameless = omp_accounts.OmpAccount(7, 1, None)
    excluding = omp_accounts.account_policy({"exclude": ["a@example.com"]}, {})
    capping = omp_accounts.account_policy({"max_usage_percent": 90}, {})
    assert excluding.skip_reason(nameless, None, _SELECTOR, now_ms) == (
        "EMAIL UNKNOWN (cannot be checked against config.yaml omp_accounts.exclude)"
    )
    assert capping.skip_reason(nameless, None, _SELECTOR, now_ms) is None
    db = Path("/x/agent.db")
    refusal, eligible, _ = omp_accounts.pool_refusal(
        excluding, [], db, None, _SELECTOR, now_ms
    )
    assert eligible == [] and refusal is not None
    assert "no omp anthropic OAuth account in /x/agent.db" in refusal, refusal
    empty = omp_accounts.account_policy(None, {})
    assert omp_accounts.pool_refusal(empty, [], db, None, _SELECTOR, now_ms) == (
        None,
        [],
        [],
    )


def test_policy_emails_with_control_characters_are_refused():
    forged = "ok@example.com\x1b[2JFORGED"
    config = omp_accounts.account_policy(
        {"exclude": [forged], "accounts": {forged: {"max_usage_percent": 90}}}, {}
    )
    env = omp_accounts.account_policy(
        None, {omp_accounts.EXCLUDE_ENV: f"a@example.com,{forged}"}
    )
    for policy in (config, env):
        assert policy.problems, policy
        assert "\x1b" not in "".join(policy.problems), policy.problems
        assert "\x1b" not in policy.summary()
    assert len(config.problems) == 2 and config.exclude == ()
    assert env.exclude == ("a@example.com",)


def test_duplicate_usage_reports_for_one_email_keep_every_window():
    report = {
        "reports": [
            {
                "provider": "anthropic",
                "metadata": {"email": "a@example.com"},
                "limits": [
                    {"label": "Claude 5 Hour", "amount": {"usedFraction": 0.95}}
                ],
            },
            {
                "provider": "anthropic",
                "metadata": {"email": "A@example.com"},
                "limits": [{"label": "Claude 7 Day", "amount": {"usedFraction": 0.1}}],
            },
        ]
    }
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / "omp"
        fake.write_text(f"#!/bin/sh\necho '{json.dumps(report)}'\n", encoding="utf-8")
        fake.chmod(0o755)
        launcher = omp_accounts.OmpLauncher(
            argv0=str(fake), real_bin=None, source="PATH"
        )
        usage = omp_accounts.usage_by_email(launcher)
    assert [w.label for w in usage["a@example.com"]] == [
        "Claude 5 Hour",
        "Claude 7 Day",
    ]


def test_pool_never_fetches_a_token_for_an_excluded_account():
    calls: list = []
    fetched: list = []
    usage = _usage({})
    with (
        _pool_world(),
        _omp_accounts_config({"exclude": ["a@example.com"]}),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=_fake_token(fetched), usage_by_email=usage),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert fetched == [2] and calls == [(3, "tok-3")], (fetched, calls)
    assert result.returncode == 0 and result.stdout == "answer from 3"
    assert result.account == (
        "b@example.com (omp anthropic account 2 of 2); skipped: a@example.com: "
        "EXCLUDED (config.yaml omp_accounts.exclude)"
    ), result.account
    assert usage.reads == [], "no threshold -> no usage read"


def test_pool_skips_an_account_over_its_threshold_reading_usage_once():
    calls: list = []
    fetched: list = []
    usage = _usage(
        {
            "a@example.com": [_window("Claude 5 Hour", 0.95)],
            "b@example.com": [_window("Claude 5 Hour", 0.1)],
        }
    )
    with (
        _pool_world(),
        _omp_accounts_config({"max_usage_percent": 90}),
        _patched(backends, _review_omp_once=_fake_once(calls, {3})),
        _patched(omp_accounts, access_token=_fake_token(fetched), usage_by_email=usage),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    # b is limited (429) and a is over its threshold: a is still never tried
    assert fetched == [2] and calls == [(3, "tok-3")], (fetched, calls)
    assert usage.reads == [1], usage.reads
    assert result.returncode == 1
    assert (
        "every omp anthropic account failed (1): b@example.com: 429" in result.account
    )
    assert "skipped: a@example.com: OVER LIMIT (5 Hour 95% >= 90%" in result.account


def test_pool_per_account_threshold_wins_and_env_beats_config():
    calls: list = []
    usage = _usage(
        {
            "a@example.com": [_window("Claude 7 Day", 0.92)],
            "b@example.com": [_window("Claude 7 Day", 0.5)],
        }
    )
    config = {
        "exclude": ["b@example.com"],
        "max_usage_percent": 90,
        "accounts": {"a@example.com": {"max_usage_percent": 95}},
    }
    with (
        _pool_world(),
        _omp_accounts_config(config),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=_fake_token([]), usage_by_email=usage),
    ):
        first = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
        first_calls = list(calls)
        calls.clear()
        with _env(
            REVIEW_OMP_ACCOUNTS_EXCLUDE="a@example.com",
            REVIEW_OMP_MAX_USAGE_PERCENT="40",
        ):
            second = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert first_calls == [(2, "tok-2")], "a@ at 92% is under its own 95% threshold"
    assert first.returncode == 0
    # env: exclude a@ instead of b@, and 40% puts b@ (50%) over the global threshold
    assert calls == [], calls
    assert second.returncode == 1
    assert "a@example.com: EXCLUDED ($REVIEW_OMP_ACCOUNTS_EXCLUDE)" in second.stderr
    assert "b@example.com: OVER LIMIT (7 Day 50% >= 40%" in second.stderr


def test_pool_with_a_threshold_and_unknown_usage_skips_nothing():
    calls: list = []
    usage = _usage(None)
    with (
        _pool_world(),
        _omp_accounts_config({"max_usage_percent": 90}),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=_fake_token([]), usage_by_email=usage),
    ):
        result = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert usage.reads == [1] and calls == [(2, "tok-2")], (usage.reads, calls)
    assert result.account == "a@example.com (omp anthropic account 1 of 2)"


def test_status_pool_refusal_counts_a_tier_window_only_for_that_tier():
    from reviewlib import seat_health

    now = time.time()
    a = omp_accounts.OmpAccount(2, 1, "a@example.com")
    b = omp_accounts.OmpAccount(3, 2, "b@example.com")
    policy = omp_accounts.account_policy(
        {"max_usage_percent": 90, "exclude": ["b@example.com"]}, {}
    )
    usage = {"a@example.com": [_window("Claude 7 Day (Fable)", 0.95, tier="fable")]}
    states = [
        seat_health.OmpAccountState(x, None, None, omp_accounts.usage_of(usage, x))
        for x in (a, b)
    ]
    pool = seat_health.OmpPoolState(
        None, "no launcher", Path("/x"), states, policy, usage
    )
    with _env(REVIEW_OMP_CLAUDE_PROFILES=None):
        sonnet = seat_health._omp_pool_reason(
            "omp:anthropic/claude-sonnet-5", pool, now
        )
        fable = seat_health._omp_pool_reason("omp:anthropic/claude-fable-5", pool, now)
        kimi = seat_health._omp_pool_reason("omp:kimi-code/k3", pool, now)
    assert sonnet is None and kimi is None, (sonnet, kimi)
    assert fable is not None and fable.startswith(
        "no omp anthropic account may be used (2): a@example.com: OVER LIMIT "
        "(7 Day (Fable) 95% >= 90%"
    ), fable


def test_pool_refuses_when_every_account_is_skipped_or_the_settings_are_invalid():
    fetched: list = []
    calls: list = []
    usage = _usage({})
    with (
        _pool_world(),
        _patched(backends, _review_omp_once=_fake_once(calls, set())),
        _patched(omp_accounts, access_token=_fake_token(fetched), usage_by_email=usage),
    ):
        with _omp_accounts_config(
            {"exclude": ["a@example.com", "b@example.com"], "max_usage_percent": 90}
        ):
            every = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
            with _env(REVIEW_OMP_CLAUDE_PROFILES="main"):
                profiles = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
        with _omp_accounts_config({"exclude": "a@example.com"}):
            invalid = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
        with (
            _omp_accounts_config({"exclude": ["a@example.com"]}),
            _env(OMP_AUTH_DB="/nonexistent/agent.db"),
        ):
            unreadable = backends.review_omp(_MODEL, "prompt", "diff", Path("."), 60)
    assert fetched == [] and calls == [], (fetched, calls)
    assert usage.reads == [], "every account excluded -> no usage read"
    assert every.returncode == 1 and every.account.startswith("none answered")
    assert (
        "no omp anthropic account may be used (2): a@example.com: EXCLUDED "
        "(config.yaml omp_accounts.exclude); b@example.com: EXCLUDED" in every.stderr
    ), every.stderr
    assert profiles.returncode == 1
    assert (
        "cannot be applied to $REVIEW_OMP_CLAUDE_PROFILES profiles" in profiles.stderr
    )
    assert invalid.returncode == 1
    assert "omp_accounts.exclude must be a list of account emails" in invalid.stderr
    assert unreadable.returncode == 1
    assert "no readable omp auth db at /nonexistent/agent.db" in unreadable.stderr


def _usage_report(**used: float) -> str:
    resets = int(time.time() * 1000) + 3_600_000
    return json.dumps(
        {
            "reports": [
                {
                    "provider": "anthropic",
                    "metadata": {"email": f"{name}@example.com"},
                    "limits": [
                        {
                            "label": "Claude 5 Hour",
                            "amount": {"usedFraction": fraction},
                            "window": {"resetsAt": resets},
                        }
                    ],
                }
                for name, fraction in used.items()
            ]
        }
    )


def _write_config(world, text: str) -> None:
    path = world.home / ".config" / "review-cli" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_e2e_config_excludes_an_account_and_seats_shows_the_reasons():
    with _world() as world:
        _write_config(
            world,
            "omp_accounts:\n  exclude: [a@example.com]\n  max_usage_percent: 90\n",
        )
        usage = _usage_report(a=0.95, b=0.1)
        ask = world.review(
            "just-ask",
            "Say hi",
            "--task",
            "641",
            "-m",
            _MODEL,
            "--timeout",
            "60",
            FAKE_OMP_USAGE=usage,
        )
        calls = world.calls()
        seats = world.review("seats", FAKE_OMP_USAGE=usage)
        as_json = world.review("doctor", "--json", FAKE_OMP_USAGE=usage)
    out = ask.stdout + ask.stderr
    assert ask.returncode == 0, out
    assert "ANSWER served with tok-2" in ask.stdout, out
    assert (
        "account: b@example.com (omp anthropic account 2 of 2); skipped: a@example.com: "
        "EXCLUDED (config.yaml omp_accounts.exclude)" in ask.stdout
    ), out
    assert [c["account"] for c in calls if c["kind"] == "token"] == ["2"], calls
    assert [c["token"] for c in calls if c["kind"] == "run"] == ["tok-2"], calls
    text = seats.stdout
    assert seats.returncode == 0, text + seats.stderr
    assert (
        "account policy: exclude a@example.com (config.yaml omp_accounts.exclude) · "
        "max usage 90% (config.yaml omp_accounts.max_usage_percent)" in text
    ), text
    a_line = next(line for line in text.splitlines() if "  a@example.com" in line)
    b_line = next(line for line in text.splitlines() if "  b@example.com" in line)
    assert "EXCLUDED (config.yaml omp_accounts.exclude)" in a_line, a_line
    assert "available" in b_line, b_line
    payload = json.loads(as_json.stdout)["omp"]
    assert payload["account_policy"]["exclude"] == ["a@example.com"]
    assert payload["account_policy"]["max_usage_percent"] == 90
    accounts = {a["email"]: a for a in payload["anthropic_accounts"]}
    assert accounts["a@example.com"]["skip_reason"].startswith("EXCLUDED")
    assert accounts["a@example.com"]["available"] is False
    assert accounts["b@example.com"]["skip_reason"] is None


def test_e2e_every_account_skipped_fails_the_seat_and_seats_shows_over_limit():
    with _world() as world:
        _write_config(
            world,
            f'models:\n  - "{_MODEL}"\nomp_accounts:\n  max_usage_percent: 90\n',
        )
        usage = _usage_report(a=0.95, b=0.1)
        env = {"FAKE_OMP_USAGE": usage, "REVIEW_OMP_ACCOUNTS_EXCLUDE": "b@example.com"}
        ask = world.review(
            "just-ask",
            "Say hi",
            "--task",
            "641",
            "-m",
            _MODEL,
            "--timeout",
            "60",
            **env,
        )
        calls = world.calls()
        seats = world.review("seats", **env)
        invalid = world.review("seats", REVIEW_OMP_MAX_USAGE_PERCENT="150")
    out = ask.stdout + ask.stderr
    assert ask.returncode != 0, out
    assert (
        "no omp anthropic account may be used (2): a@example.com: OVER LIMIT" in out
    ), out
    assert "b@example.com: EXCLUDED ($REVIEW_OMP_ACCOUNTS_EXCLUDE)" in out, out
    assert [c for c in calls if c["kind"] in ("token", "run")] == [], calls
    text = seats.stdout
    seat_line = next(line for line in text.splitlines() if _MODEL in line)
    assert (
        "UNUSABLE: no omp anthropic account may be used (2): a@example.com: OVER LIMIT"
        in seat_line
    ), seat_line
    a_line = next(line for line in text.splitlines() if "  a@example.com" in line)
    assert "OVER LIMIT (5 Hour 95% >= 90%, resets " in a_line, a_line
    assert "exclude b@example.com ($REVIEW_OMP_ACCOUNTS_EXCLUDE)" in text, text
    invalid_seat = next(line for line in invalid.stdout.splitlines() if _MODEL in line)
    assert "UNUSABLE: invalid omp account settings" in invalid_seat, invalid_seat
    assert (
        "INVALID SETTING: $REVIEW_OMP_MAX_USAGE_PERCENT must be a number above 0 and at "
        "most 100, got '150'" in invalid.stdout
    ), invalid.stdout


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
