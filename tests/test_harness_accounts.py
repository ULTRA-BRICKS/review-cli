"""review-cli#646: `review seats` lists every harness login on the machine, and an omp
seat on an env-key provider (z.ai) runs on the key review-cli holds in its own .env.

Hermetic: a temp HOME with a fake `claude` (answers `auth status --json`), a fake `omp`,
a Codex auth.json, an opencode auth.json, a throwaway omp agent.db and a review-cli .env
-- no real login, harness or network is touched.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends, claude_accounts, harness_accounts  # noqa: E402

REVIEW_BIN = REPO_ROOT / "bin" / "review"
ZAI_SECRET = "zai-test-secret-646"
CODEX_SECRET = "codex-access-secret-646"
OPENCODE_SECRET = "fw-test-secret-646"


@contextlib.contextmanager
def _env(**values: str | None):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def _agent_db(path: Path, rows: list[tuple[str, str | None]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "provider TEXT NOT NULL, credential_type TEXT NOT NULL, data TEXT NOT NULL, "
            "disabled_cause TEXT DEFAULT NULL)"
        )
        for provider, disabled in rows:
            conn.execute(
                "INSERT INTO auth_credentials (provider, credential_type, data, "
                "disabled_cause) VALUES (?, 'oauth', '{\"access\": \"omp-secret\"}', ?)",
                (provider, disabled),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _jwt(claims: dict) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class _Machine:
    """A temp HOME holding one login per harness."""

    def __init__(self, root: Path, *, claude_logged_in: bool = True):
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.work = root / "work"
        for d in (self.home, self.bin, self.work):
            d.mkdir(parents=True)
        claude = self.bin / "claude"
        status = {
            "loggedIn": claude_logged_in,
            "email": "cc@example.com",
            "subscriptionType": "max",
            "configDirectory": str(self.home / ".claude"),
        }
        claude.write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "auth status" ]; then\n'
            f"  echo '{json.dumps(status)}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        claude.chmod(0o755)
        omp = self.bin / "omp"
        omp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        omp.chmod(0o755)
        self.codex_home = root / "codex"
        self.codex_home.mkdir()
        (self.codex_home / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": _jwt(
                            {
                                "email": "codex@example.com",
                                "https://api.openai.com/auth": {
                                    "chatgpt_plan_type": "pro"
                                },
                            }
                        ),
                        "access_token": CODEX_SECRET,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.data_home = root / "data"
        (self.data_home / "opencode").mkdir(parents=True)
        (self.data_home / "opencode" / "auth.json").write_text(
            json.dumps({"fireworks-ai": {"type": "api", "key": OPENCODE_SECRET}}),
            encoding="utf-8",
        )
        self.db = _agent_db(
            root / "agent" / "agent.db",
            [("anthropic", None), ("anthropic", None), ("openai-codex", "revoked")],
        )
        self.env_file = root / "review.env"
        self.env_file.write_text(f"ZAI_API_KEY={ZAI_SECRET}\n", encoding="utf-8")
        config = self.home / ".config" / "review-cli" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'models:\n  - "omp:zai/glm-5.3"\n  - "omp:openai-codex/gpt-6.1-sol"\n',
            encoding="utf-8",
        )

    def env(self) -> dict[str, str]:
        state = self.root / "state"
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.data_home),
            "CODEX_HOME": str(self.codex_home),
            "OMP_AUTH_DB": str(self.db),
            "GEMINI_ENV_FILE": str(self.env_file),
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
        }
        # CI's tool-cache Python links libpython from LD_LIBRARY_PATH
        if os.environ.get("LD_LIBRARY_PATH"):
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        return env

    def review(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REVIEW_BIN), *args],
            cwd=str(self.work),
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )


@contextlib.contextmanager
def _machine(**kw):
    with tempfile.TemporaryDirectory(prefix="review-646-") as d:
        yield _Machine(Path(d), **kw)


def _no_secret(text: str) -> None:
    for secret in (ZAI_SECRET, CODEX_SECRET, OPENCODE_SECRET, "omp-secret"):
        assert secret not in text, f"secret {secret!r} leaked"


def test_e2e_seats_lists_every_harness_login_without_secrets():
    with _machine() as m:
        text_run = m.review("seats")
        json_run = m.review("seats", "--json")
    assert text_run.returncode == 0, text_run.stdout + text_run.stderr
    text = text_run.stdout
    section = text[text.index("HARNESS ACCOUNTS") :]
    rows = {
        tuple(line.split()[:2]): line
        for line in section.splitlines()[1:]
        if line.strip()
    }
    assert "cc@example.com" in rows[("claude-code", "anthropic")], section
    assert "max, logged in" in rows[("claude-code", "anthropic")], section
    assert "codex@example.com" in rows[("codex", "openai")], section
    assert "ChatGPT pro" in rows[("codex", "openai")], section
    assert "2 credentials" in rows[("omp", "anthropic")], section
    assert "0 credentials" in rows[("omp", "openai-codex")], section
    assert "1 disabled" in rows[("omp", "openai-codex")], section
    assert "passes ZAI_API_KEY" in rows[("omp", "zai")], section
    assert "API key" in rows[("opencode", "fireworks-ai")], section
    assert "ZAI_API_KEY" in rows[("review-cli", "zai")], section
    _no_secret(text)

    assert json_run.returncode == 0, json_run.stderr
    _no_secret(json_run.stdout)
    payload = json.loads(json_run.stdout)
    by_key = {(a["harness"], a["provider"]): a for a in payload["harness_accounts"]}
    assert by_key[("codex", "openai")]["account"] == "codex@example.com"
    assert by_key[("claude-code", "anthropic")]["account"] == "cc@example.com"
    seats = {s["seat"]: s for s in payload["seats"]}
    assert seats["omp:zai/glm-5.3"]["usable"] is True, seats
    assert seats["omp:openai-codex/gpt-6.1-sol"]["usable"] is False, seats


def test_logged_out_claude_code_says_how_to_log_in():
    with _machine(claude_logged_in=False) as m:
        proc = m.review("seats")
    line = next(
        line
        for line in proc.stdout.splitlines()
        if line.strip().startswith("claude-code")
    )
    assert "(none)" in line and "claude auth login" in line, line


def test_registered_claude_accounts_are_pointed_at_not_probed_twice():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "account-0").mkdir()
        (root / "account-1").mkdir()
        calls = []
        with (
            _env(REVIEW_CLAUDE_ACCOUNTS_ROOT=str(root)),
            _patched(claude_accounts, auth_status=lambda d: calls.append(d)),
        ):
            rows = harness_accounts._claude_code()
    assert calls == []
    assert rows[0].account == "2 registered" and "CLAUDE CLI ACCOUNTS" in rows[0].detail


def test_codex_without_login_and_with_api_key():
    with tempfile.TemporaryDirectory() as d:
        with _env(CODEX_HOME=d):
            assert harness_accounts._codex()[0].account == "(none)"
            (Path(d) / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-secret"}), encoding="utf-8"
            )
            rows = harness_accounts._codex()
    assert [(r.account, r.detail.endswith("auth.json")) for r in rows] == [
        ("API key", True)
    ]
    assert "sk-secret" not in repr(rows)


def test_malformed_codex_auth_is_not_a_login():
    with tempfile.TemporaryDirectory() as d, _env(CODEX_HOME=d):
        auth = Path(d) / "auth.json"
        for data in (
            {"tokens": {"id_token": "invalid"}},
            {"tokens": {"id_token": "a.e30.b"}},
            {"OPENAI_API_KEY": "   "},
            {"OPENAI_API_KEY": 42},
        ):
            auth.write_text(json.dumps(data), encoding="utf-8")
            rows = harness_accounts._codex()
            assert [r.account for r in rows] == ["(none)"], (data, rows)


def test_omp_env_key_comes_from_review_cli_env_file():
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_text(f"ZHIPU_API_KEY={ZAI_SECRET}\n", encoding="utf-8")
        with _env(GEMINI_ENV_FILE=str(env_file), ZAI_API_KEY=None, ZHIPU_API_KEY=None):
            assert backends.omp_env_key("zai") == ("ZAI_API_KEY", ZAI_SECRET)
            assert backends.omp_env_key("kimi-code") is None
            assert backends.omp_env_key(None) is None
        with _env(
            GEMINI_ENV_FILE=str(Path(d) / "missing.env"),
            ZAI_API_KEY=None,
            ZHIPU_API_KEY=None,
        ):
            assert backends.omp_env_key("zai") is None


def test_opencode_rows_match_what_the_oc_probe_accepts():
    with tempfile.TemporaryDirectory() as d:
        auth = Path(d) / "auth.json"
        config = Path(d) / "opencode.json"
        auth.write_text(
            json.dumps(
                {
                    "openai": {"type": ["api"], "key": "k-secret"},
                    "xai": {"type": "oauth", "refresh": "r-secret"},
                    "deepseek": {"type": "api", "key": "   "},
                    "broken": "raw",
                }
            ),
            encoding="utf-8",
        )
        config.write_text(
            json.dumps({"provider": {"groq": {"options": {"apiKey": "g-secret"}}}}),
            encoding="utf-8",
        )
        with _env(
            OC_AUTH_FILE=str(auth),
            OC_CONFIG_FILE=str(config),
            **{
                v: None
                for names in backends._OC_PROVIDER_ENV_VARS.values()
                for v in names
            },
        ):
            rows = harness_accounts._opencode()
    got = {(r.provider, r.account) for r in rows}
    assert got == {
        ("openai", "credential"),
        ("xai", "OAuth login"),
        ("groq", "API key"),
    }, got
    for secret in ("k-secret", "r-secret", "g-secret"):
        assert secret not in repr(rows)


def test_omp_seat_gets_the_alias_key_when_the_primary_var_is_blank():
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(argv, cwd, timeout, backend, **kw):
        captured["env"] = kw.get("env")
        return _Proc()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with (
            _env(
                GEMINI_ENV_FILE=str(root / "missing.env"),
                ZAI_API_KEY="   ",
                ZHIPU_API_KEY="alias-key",
                OMP_AUTH_DB=str(_agent_db(root / "agent.db", [])),
                REVIEW_SEAT_COOLDOWN_FILE=str(root / "cooldown.json"),
            ),
            _patched(
                backends,
                _which=lambda name: f"/fake/bin/{name}",
                _run_streamed=run,
                _omp_launcher=lambda: backends.omp_accounts.OmpLauncher(
                    argv0="/fake/bin/omp", real_bin=None, source="PATH"
                ),
            ),
        ):
            assert backends.backend_unavailable_reason("omp:zai/glm-5.3") is None
            backends.review_omp("omp:zai/glm-5.3", "q", "", root, 10)
    assert captured["env"]["ZAI_API_KEY"] == "alias-key"


def test_omp_zai_seat_is_usable_and_gets_the_key():
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(argv, cwd, timeout, backend, **kw):
        captured["env"] = kw.get("env")
        return _Proc()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env_file = root / ".env"
        env_file.write_text(f"ZAI_API_KEY={ZAI_SECRET}\n", encoding="utf-8")
        db = _agent_db(root / "agent.db", [("anthropic", None)])
        with (
            _env(
                GEMINI_ENV_FILE=str(env_file),
                ZAI_API_KEY=None,
                ZHIPU_API_KEY=None,
                OMP_AUTH_DB=str(db),
                REVIEW_SEAT_COOLDOWN_FILE=str(root / "cooldown.json"),
                REVIEW_OMP_BIN=None,
            ),
            _patched(
                backends,
                _which=lambda name: f"/fake/bin/{name}",
                _run_streamed=run,
                _omp_launcher=lambda: backends.omp_accounts.OmpLauncher(
                    argv0="/fake/bin/omp", real_bin=None, source="PATH"
                ),
            ),
        ):
            assert backends.backend_unavailable_reason("omp:zai/glm-5.3") is None
            # a provider with neither a stored credential nor an env key stays unusable
            assert backends.backend_unavailable_reason("omp:xai/grok-4.7") is not None
            backends.review_omp("omp:zai/glm-5.3", "q", "", root, 10)
            assert captured["env"]["ZAI_API_KEY"] == ZAI_SECRET
            # a key already in the process env is what the seat gets
            with _env(ZAI_API_KEY="from-process-env"):
                backends.review_omp("omp:zai/glm-5.3", "q", "", root, 10)
            assert captured["env"]["ZAI_API_KEY"] == "from-process-env"
            # seats on other providers are not handed the z.ai key
            backends.review_omp("omp:kimi-code/k3", "q", "", root, 10)
            assert "ZAI_API_KEY" not in captured["env"]


def test_stored_zai_credential_wins_over_the_env_file_key():
    captured = {}
    reads = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(argv, cwd, timeout, backend, **kw):
        captured["env"] = kw.get("env")
        return _Proc()

    real_resolve = backends._resolve_key

    def counting_resolve(names, var):
        reads.append(var)
        return real_resolve(names, var)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env_file = root / ".env"
        env_file.write_text(f"ZAI_API_KEY={ZAI_SECRET}\n", encoding="utf-8")
        with (
            _env(
                GEMINI_ENV_FILE=str(env_file),
                ZAI_API_KEY=None,
                ZHIPU_API_KEY=None,
                OMP_AUTH_DB=str(_agent_db(root / "agent.db", [("zai", None)])),
                REVIEW_SEAT_COOLDOWN_FILE=str(root / "cooldown.json"),
            ),
            _patched(
                backends,
                _which=lambda name: f"/fake/bin/{name}",
                _run_streamed=run,
                _resolve_key=counting_resolve,
                _omp_launcher=lambda: backends.omp_accounts.OmpLauncher(
                    argv0="/fake/bin/omp", real_bin=None, source="PATH"
                ),
            ),
        ):
            for _ in range(3):
                assert backends.backend_unavailable_reason("omp:zai/glm-5.3") is None
            assert reads == [], "a stored credential answers without reading the .env"
            backends.review_omp("omp:zai/glm-5.3", "q", "", root, 10)
    assert "ZAI_API_KEY" not in captured["env"], "omp keeps its stored credential"


def test_profile_seat_forwards_the_key_unless_that_profile_stores_one():
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(argv, cwd, timeout, backend, **kw):
        captured["env"] = kw.get("env")
        return _Proc()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env_file = root / ".env"
        env_file.write_text("OPENROUTER_API_KEY=or-key\n", encoding="utf-8")
        profile_db = root / "home" / ".omp" / "profiles" / "work" / "agent" / "agent.db"
        with (
            _env(
                HOME=str(root / "home"),
                GEMINI_ENV_FILE=str(env_file),
                OPENROUTER_API_KEY=None,
                OMP_AUTH_DB=str(_agent_db(root / "default.db", [("openrouter", None)])),
                PI_CODING_AGENT_DIR=None,
                OMP_PROFILE=None,
            ),
            _patched(
                backends,
                _run_streamed=run,
                _omp_launcher=lambda: backends.omp_accounts.OmpLauncher(
                    argv0="/fake/bin/omp", real_bin=None, source="PATH"
                ),
            ),
        ):
            once = backends._review_omp_once
            args = ("omp:openrouter/x", "openrouter/x", "q", "", root, 10, 0, None)
            _agent_db(profile_db, [("anthropic", None)])
            once(*args, profile="work")
            assert captured["env"].get("OPENROUTER_API_KEY") == "or-key"
            profile_db.unlink()
            _agent_db(profile_db, [("openrouter", None)])
            once(*args, profile="work")
            assert "OPENROUTER_API_KEY" not in captured["env"]


def test_omp_inventory_lists_the_configured_claude_profiles():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        home = root / "home"
        for name in ("work", "personal"):
            _agent_db(
                home / ".omp" / "profiles" / name / "agent" / "agent.db",
                [("anthropic", None)],
            )
        with _env(
            HOME=str(home),
            OMP_AUTH_DB=str(_agent_db(root / "default.db", [])),
            PI_CODING_AGENT_DIR=None,
            OMP_PROFILE=None,
            REVIEW_OMP_CLAUDE_PROFILES="work,personal",
            GEMINI_ENV_FILE="/nonexistent/review-cli/.env",
            ZAI_API_KEY=None,
            ZHIPU_API_KEY=None,
            OPENROUTER_API_KEY=None,
        ):
            rows = harness_accounts._omp()
    assert [
        (r.provider, r.account, r.detail.endswith(f"(profile {p})"))
        for r, p in zip(rows, ("work", "personal"))
    ] == [
        ("anthropic", "1 credential", True),
        ("anthropic", "1 credential", True),
    ], rows


_NO_KEYS = {
    v: None for _p, _t, names in harness_accounts._REVIEW_CLI_KEYS for v in names
}


def test_review_cli_anthropic_token_from_env_file_is_listed():
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_text("ANTHROPIC_AUTH_TOKEN=tok-secret\n", encoding="utf-8")
        with _env(GEMINI_ENV_FILE=str(env_file), **_NO_KEYS):
            rows = harness_accounts._review_cli_keys()
    assert [(r.provider, r.account) for r in rows] == [("anthropic", "API token")]
    assert f"ANTHROPIC_AUTH_TOKEN from {env_file}" in rows[0].detail, rows
    assert "tok-secret" not in repr(rows)


def test_review_cli_key_row_names_the_source_the_resolver_uses():
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_text("ZAI_API_KEY=file-key\n", encoding="utf-8")
        with _env(GEMINI_ENV_FILE=str(env_file), **{**_NO_KEYS, "ZHIPU_API_KEY": "e"}):
            rows = harness_accounts._review_cli_keys()
    assert [r.detail for r in rows] == ["ZHIPU_API_KEY from the env"], rows


def test_unparsable_env_file_does_not_break_the_report():
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / ".env"
        env_file.write_bytes(b"ZAI_API_KEY=\xff\xfe\n")
        with _env(GEMINI_ENV_FILE=str(env_file), **_NO_KEYS):
            assert harness_accounts._review_cli_keys() == []
            assert backends.omp_env_key("zai") is None


def test_unpaid_note_follows_the_seat_provider_dispatch_checks():
    keys = {
        **_NO_KEYS,
        "GEMINI_ENV_FILE": "/nonexistent/review-cli/.env",
        "ANTHROPIC_API_KEY": "k",
    }
    with _env(**keys, REVIEW_UNPAID_PROVIDERS="claude"):
        assert "marked unpaid" in harness_accounts._review_cli_keys()[0].detail
    with _env(**keys, REVIEW_UNPAID_PROVIDERS="anthropic"):
        assert "marked unpaid" not in harness_accounts._review_cli_keys()[0].detail


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
