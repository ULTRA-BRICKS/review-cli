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
    `usage --json`, and `-p` runs that answer, or fail with omp's real 429 line for a
    token listed in $FAKE_OMP_LIMITED. Every call is appended to $FAKE_OMP_LOG."""
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
