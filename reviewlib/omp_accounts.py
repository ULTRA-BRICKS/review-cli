"""The omp launcher an omp seat runs, and omp's pool of Anthropic accounts.

Two problems for `backends.review_omp` live here (review-cli#636, #637).

LAUNCHER (#636). An omp seat runs with HOME pointed at an empty sandbox dir (cage layer 2,
see the omp comment block in backends.py). The `omp` on this machine's PATH is a bash
wrapper (~/.local/bin/omp) that finds the real binary as `$HOME/.local/lib/omp/omp`, so
under the sandbox HOME every omp seat exited 127 ("real binary not found"). The launcher
is resolved here from the REAL environment, before the sandbox exists, and the seat gets
the real binary's absolute path as `OMP_REAL_BIN`, the wrapper's own override. A wrapper
whose real binary cannot be found is probed once under a sandbox-like HOME; when it fails
there the seat is reported unusable (`launcher_problem`) instead of failing mid-run.

ACCOUNT POOL (#637). omp keeps several Anthropic OAuth logins in its agent.db. Left alone,
omp picks one account per run, and a run that lands on an exhausted account fails with
`429 rate_limit_error` while the other accounts still have quota. review-cli reads the
account list from agent.db (read-only, no token is read), pins each attempt to one account
and moves on to the next one on a limit or login failure. Pinning, verified against omp
18.8.1: `omp token anthropic --account N` prints account N's access token (omp refreshes it
in its own db when needed), and an omp run whose agent dir holds NO stored credential uses
`ANTHROPIC_OAUTH_TOKEN` and nothing else. With stored credentials present, omp ignores
that variable, so a pinned attempt gets a fresh empty agent dir inside the seat sandbox.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# `omp token --account N` and agent.db's `auth_credentials.provider` spell it this way;
# omp's rate-limit blocks for the provider live under this `provider_key`.
PROVIDER = "anthropic"
_BLOCK_PROVIDER_KEY = "anthropic:oauth"

# The wrapper's own override for where the real binary is (see ~/.local/bin/omp), and the
# dir omp's installer and `omp update` write the binary to.
REAL_BIN_ENV = "OMP_REAL_BIN"
_INSTALL_DIR_ENV = "PI_INSTALL_DIR"

_TOKEN_TIMEOUT_SECONDS = 30
_PROBE_TIMEOUT_SECONDS = 20
_USAGE_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class OmpAccount:
    """One enabled Anthropic OAuth login in omp's agent.db. `position` is the 1-based
    index `omp token anthropic --account N` takes: omp lists the enabled OAuth rows of a
    provider by `auth_credentials.id`, and so does `anthropic_accounts`. `blocks` are omp's
    own active rate-limit blocks for this login as (scope, blocked_until_ms); scope "" is
    the shared window, any other scope names a model tier (e.g. "fable")."""

    credential_id: int
    position: int
    email: str | None
    blocks: tuple[tuple[str, int], ...] = ()

    @property
    def label(self) -> str:
        return self.email or f"credential #{self.credential_id}"

    def blocked_until_ms(self, omp_model: str | None) -> int | None:
        """When omp's block for `omp_model` on this account ends, None when not blocked."""
        lowered = (omp_model or "").lower()
        ends = [
            until
            for scope, until in self.blocks
            if not scope or scope.lower() in lowered
        ]
        return max(ends) if ends else None


def _identity_email(identity_key: object) -> str | None:
    """`email:you@example.com|org:<id>` (agent.db's `identity_key`) -> the email. A value
    with whitespace or control characters is not used: the email is printed into review
    output and log headers."""
    if not isinstance(identity_key, str):
        return None
    for part in identity_key.split("|"):
        email = part[len("email:") :] if part.startswith("email:") else ""
        if email and email.isprintable() and not any(ch.isspace() for ch in email):
            return email
    return None


def anthropic_accounts(
    db: Path, *, now_ms: int | None = None
) -> list[OmpAccount] | None:
    """Every enabled Anthropic OAuth account in omp's agent.db `db`, in omp's own order,
    with its active rate-limit blocks. None when the db cannot be read. The db is opened
    read-only through a `file:` URI, so this can never create or change it, and only the
    id and identity columns are selected, never the credential data."""
    if not db.is_file():
        return None
    now = (
        now_ms
        if now_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        query = (
            "SELECT id, {identity} FROM auth_credentials WHERE provider = ? "
            "AND credential_type = 'oauth' AND disabled_cause IS NULL ORDER BY id"
        )
        try:
            rows = conn.execute(
                query.format(identity="identity_key"), (PROVIDER,)
            ).fetchall()
        except sqlite3.OperationalError:
            # an older agent.db without the identity_key column: ids only
            rows = conn.execute(query.format(identity="NULL"), (PROVIDER,)).fetchall()
        blocks: dict[int, list[tuple[str, int]]] = {}
        try:
            for cred_id, scope, until in conn.execute(
                "SELECT credential_id, block_scope, MAX(blocked_until_ms) "
                "FROM auth_credential_blocks WHERE provider_key = ? AND blocked_until_ms > ? "
                "GROUP BY credential_id, block_scope",
                (_BLOCK_PROVIDER_KEY, now),
            ):
                until_ms = _finite_int(until)
                if isinstance(cred_id, int) and until_ms is not None:
                    label = scope if isinstance(scope, str) else ""
                    blocks.setdefault(cred_id, []).append((label, until_ms))
        except sqlite3.OperationalError:
            pass  # no block table in this omp version: nothing is blocked
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    accounts = []
    for row in rows:
        cred_id = row[0]
        if not isinstance(cred_id, int) or isinstance(cred_id, bool):
            continue
        accounts.append(
            OmpAccount(
                credential_id=cred_id,
                position=len(accounts) + 1,
                email=_identity_email(row[1]),
                blocks=tuple(sorted(blocks.get(cred_id, ()))),
            )
        )
    return accounts


def dispatch_order(
    accounts: list[OmpAccount], omp_model: str | None, limited_until: dict[int, float]
) -> list[OmpAccount]:
    """The order to try `accounts` in: those neither blocked by omp nor recorded as
    limited by review-cli (`limited_until`: credential id -> epoch seconds) first, in omp's
    order; the limited ones after them, soonest-free first. Limited accounts are still
    tried last: a block can be stale, and nothing else can answer once the rest failed."""
    free: list[OmpAccount] = []
    limited: list[tuple[float, int, OmpAccount]] = []
    for account in accounts:
        blocked_ms = account.blocked_until_ms(omp_model)
        until = max(
            blocked_ms / 1000 if blocked_ms is not None else 0.0,
            limited_until.get(account.credential_id, 0.0),
        )
        if until:
            limited.append((until, account.position, account))
        else:
            free.append(account)
    return free + [account for _until, _pos, account in sorted(limited)]


# A failed attempt that means "this account cannot answer now, another may": a rate or
# usage limit (omp relays Anthropic's `429 {"type":"error","error":{"type":
# "rate_limit_error",...}} retry-after-ms=N`) or a quota notice. Scanned only on a
# non-zero exit, on stderr plus a short stdout, so a long review that quotes these words
# is never read as a limit.
_LIMIT_RE = re.compile(
    r"rate_limit_error|\b429\b|rate limit|usage limit|weekly limit|session limit"
    r"|hit your (?:\w+ )?limit|exceed your account",
    re.IGNORECASE,
)
_RETRY_AFTER_MS_RE = re.compile(r"retry-after-ms=(\d{1,15})\b")
_SHORT_STDOUT = 400


def _error_channel(stdout: str, stderr: str) -> str:
    body = (stdout or "").strip()
    return (stderr or "") + ("\n" + body if body and len(body) <= _SHORT_STDOUT else "")


def limit_failure(returncode: int, stdout: str, stderr: str) -> str | None:
    """The output line showing an attempt hit this account's rate or usage limit, else
    None."""
    if returncode == 0:
        return None
    for line in _error_channel(stdout, stderr).splitlines():
        if _LIMIT_RE.search(line):
            return line.strip()[:200]
    return None


def retry_after_seconds(stdout: str, stderr: str) -> float | None:
    """omp's `retry-after-ms=N` hint on a limit failure, in seconds."""
    match = _RETRY_AFTER_MS_RE.search(_error_channel(stdout, stderr))
    return int(match.group(1)) / 1000 if match else None


# ---- the launcher ---------------------------------------------------------------------


@dataclass(frozen=True)
class OmpLauncher:
    """`argv0` is what a seat execs (the `omp` on PATH, or an explicit override);
    `real_bin` the native binary behind a wrapper script, exported to the seat as
    `OMP_REAL_BIN` so the wrapper finds it under the sandbox HOME; `source` names where
    `argv0` came from, for messages."""

    argv0: str
    real_bin: str | None
    source: str

    @property
    def token_bin(self) -> str:
        """The binary that runs `omp token` / `omp usage` outside the sandbox: the real
        binary when known (no wrapper scope for a one-second helper call)."""
        return self.real_bin or self.argv0

    def seat_env(self) -> dict[str, str]:
        if not self.real_bin:
            return {}
        return {
            REAL_BIN_ENV: self.real_bin,
            _INSTALL_DIR_ENV: str(Path(self.real_bin).parent),
        }


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _is_script(path: Path) -> bool:
    """True for a `#!` script (a wrapper), False for a native binary or an unreadable path."""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _real_bin_candidates() -> list[Path]:
    """Where a wrapper's real binary is, in order: the wrapper's own `OMP_REAL_BIN`
    override, omp's install dir (`PI_INSTALL_DIR`), then the omp installer's default
    `~/.local/lib/omp/omp` under the REAL home."""
    out = []
    real_bin = os.environ.get(REAL_BIN_ENV, "").strip()
    if real_bin:
        out.append(Path(os.path.expanduser(real_bin)))
    install_dir = os.environ.get(_INSTALL_DIR_ENV, "").strip()
    if install_dir:
        out.append(Path(os.path.expanduser(install_dir)) / "omp")
    out.append(Path.home() / ".local" / "lib" / "omp" / "omp")
    return out


def launcher_for(argv0: str, *, source: str) -> OmpLauncher:
    """The launcher for the `omp` at `argv0`: a native binary runs as is; for a wrapper
    script the first real-binary candidate that is an executable file other than the
    wrapper itself is recorded as `real_bin`."""
    path = Path(argv0)
    real: str | None = None
    if _is_script(path):
        wrapper = path.resolve()
        for candidate in _real_bin_candidates():
            if _is_executable_file(candidate) and candidate.resolve() != wrapper:
                real = str(candidate.resolve())
                break
    return OmpLauncher(argv0=argv0, real_bin=real, source=source)


_PROBE_CACHE: dict[tuple[str, int], str | None] = {}
# A wrapper that finds things through the home dir: `$HOME`, `${HOME...}` or `~/`.
_HOME_REFERENCE = re.compile(rb"\$\{?HOME\b|~/")


def _uses_home(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return bool(_HOME_REFERENCE.search(fh.read(65536)))
    except OSError:
        return False


def _finite_int(value: object) -> int | None:
    """`value` as an int when it is a finite number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) else None


def _printable(text: str) -> str:
    """`text` without control characters: helper output is printed into review output."""
    return "".join(ch for ch in text if ch.isprintable())


def launcher_problem(launcher: OmpLauncher) -> str | None:
    """Why `launcher` cannot start an omp seat, else None. An explicit override that is not
    an executable file is a problem. A wrapper script that refers to the home dir is run
    once (`--version`, cached per wrapper mtime) as a seat would run it: HOME set to an
    empty temp dir, with the real binary handed over as OMP_REAL_BIN when one was found; a
    failure there is the problem, with the wrapper's own message. A script that never
    mentions HOME cannot break on the sandbox HOME and is not run."""
    path = Path(launcher.argv0)
    if launcher.source != "PATH" and not _is_executable_file(path):
        return f"omp: {launcher.source} {launcher.argv0} is not an executable file"
    if not _is_script(path) or not _uses_home(path):
        return None
    try:
        stamp = path.stat().st_mtime_ns
    except OSError as exc:
        return f"omp: cannot read the launcher {launcher.argv0}: {exc}"
    key = (launcher.argv0, stamp, launcher.real_bin or "")
    if key not in _PROBE_CACHE:
        _PROBE_CACHE[key] = _probe_wrapper(launcher)
    return _PROBE_CACHE[key]


def _probe_wrapper(launcher: OmpLauncher) -> str | None:
    argv0 = launcher.argv0
    with tempfile.TemporaryDirectory(prefix="review-cli-omp-probe-") as home:
        env = {
            **os.environ,
            "HOME": home,
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        }
        env.pop(REAL_BIN_ENV, None)
        env.update(launcher.seat_env())
        try:
            proc = subprocess.run(
                [argv0, "--version"],
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"omp: the launcher {argv0} does not start under a sandboxed HOME ({exc})"
    if proc.returncode == 0:
        return None
    detail = next(
        (
            line.strip()
            for line in (proc.stderr or proc.stdout or "").splitlines()
            if line.strip()
        ),
        "no output",
    )
    return (
        f"omp: the launcher {argv0} exits {proc.returncode} under the seat's sandboxed HOME "
        f"({_printable(detail)[:200]}); set REVIEW_OMP_BIN or config.yaml omp_bin to the "
        "real omp binary"
    )


# ---- omp helper calls (outside the sandbox) ---------------------------------------------


def access_token(launcher: OmpLauncher, account: OmpAccount) -> tuple[str | None, str]:
    """(token, "") for `account`, or (None, why not). Runs `omp token anthropic --account
    N` in the real environment, so omp refreshes an expired token in its own db. The token
    is never logged or put on a command line."""
    argv = [launcher.token_bin, "token", PROVIDER, "--account", str(account.position)]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_TOKEN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            None,
            f"`omp token {PROVIDER} --account {account.position}` failed: {exc}",
        )
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    token = lines[-1] if lines else ""
    if proc.returncode != 0 or not token or any(ch.isspace() for ch in token):
        detail = next(
            (line.strip() for line in (proc.stderr or "").splitlines() if line.strip()),
            f"exit {proc.returncode}, no token printed",
        )
        return (
            None,
            f"`omp token {PROVIDER} --account {account.position}`: "
            f"{_printable(detail)[:200]}",
        )
    return token, ""


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_fraction: float
    resets_at_ms: int | None


def usage_by_email(launcher: OmpLauncher) -> dict[str, list[UsageWindow]] | None:
    """Each Anthropic account's usage windows from `omp usage --json --provider
    anthropic`, keyed by account email, or None when omp did not answer. A network read of
    the provider's usage endpoint (omp caches it); no model is called."""
    import json  # noqa: PLC0415

    try:
        proc = subprocess.run(
            [launcher.token_bin, "usage", "--json", "--provider", PROVIDER],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_USAGE_TIMEOUT_SECONDS,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, list[UsageWindow]] = {}
    reports = data.get("reports")
    for report in reports if isinstance(reports, list) else []:
        if not isinstance(report, dict) or report.get("provider") != PROVIDER:
            continue
        metadata = report.get("metadata")
        email = metadata.get("email") if isinstance(metadata, dict) else None
        if not isinstance(email, str):
            continue
        windows = []
        limits = report.get("limits")
        for limit in limits if isinstance(limits, list) else []:
            if not isinstance(limit, dict):
                continue
            amount, window = limit.get("amount"), limit.get("window")
            used = amount.get("usedFraction") if isinstance(amount, dict) else None
            if _finite_int(used) is None:
                continue
            resets = window.get("resetsAt") if isinstance(window, dict) else None
            windows.append(
                UsageWindow(
                    label=str(limit.get("label") or limit.get("id") or "?"),
                    used_fraction=float(used),
                    resets_at_ms=_finite_int(resets),
                )
            )
        out[email] = windows
    return out


def utc_stamp(epoch_ms: float) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%MZ"
    )
