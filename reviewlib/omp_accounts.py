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

ACCOUNT POLICY (#641). config.yaml `omp_accounts:` (or its env overrides) names accounts a
seat must never take and a usage threshold: an account whose `omp usage` window is at or
above it waits for that window's reset (`AccountPolicy`).
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import account_budget

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
        ends = [
            until
            for scope, until in self.blocks
            if tier_applies(scope or None, omp_model)
        ]
        return max(ends) if ends else None


def tier_applies(tier: str | None, omp_model: str | None) -> bool:
    """True when a block or usage window of model tier `tier` (None: shared by every
    model) counts for `omp_model`: the tier name (e.g. "fable") occurs in the model id."""
    return tier is None or tier.lower() in (omp_model or "").lower()


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
        blocks = _active_credential_blocks(conn, now)
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
    """One usage window from `omp usage`. `tier` names the model tier a scoped window
    counts (e.g. "fable" for `Claude 7 Day (Fable)`); None for a window every model of the
    account shares."""

    label: str
    used_fraction: float
    resets_at_ms: int | None
    tier: str | None = None
    observed_at_ms: int | None = None
    observation_key: str | None = None


_USAGE_WINDOW_LABELS = (
    (re.compile(r"\b5\s*hours?\b", re.IGNORECASE), account_budget.WINDOW_5H),
    (re.compile(r"\b7\s*days?\b", re.IGNORECASE), account_budget.WINDOW_7D),
)


def usage_window_name(label: str) -> str | None:
    """`account_budget.WINDOW_5H` / `WINDOW_7D` for an `omp usage` limit labelled like
    `Claude 5 Hour` / `Claude 7 Day (Fable)`, None for any other window."""
    for pattern, name in _USAGE_WINDOW_LABELS:
        if pattern.search(label):
            return name
    return None


def usage_by_email(launcher: OmpLauncher) -> dict[str, list[UsageWindow]] | None:
    """Each Anthropic account's usage windows from `omp usage --json --provider
    anthropic`, keyed by the lower-cased account email (look one up with `usage_of`), or
    None when omp did not answer. A network read of
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
    for report_index, report in enumerate(reports if isinstance(reports, list) else []):
        if not isinstance(report, dict) or report.get("provider") != PROVIDER:
            continue
        metadata = report.get("metadata")
        email = metadata.get("email") if isinstance(metadata, dict) else None
        if not isinstance(email, str):
            continue
        windows = _report_usage_windows(report, str(report_index))
        # a second report for the same email adds its windows instead of replacing them
        out.setdefault(email.lower(), []).extend(windows)
    return out


def utc_stamp(epoch_ms: float) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%MZ"
    )


# ---- the account policy (#641) ----------------------------------------------------------

EXCLUDE_ENV = "REVIEW_OMP_ACCOUNTS_EXCLUDE"
MAX_USAGE_ENV = "REVIEW_OMP_MAX_USAGE_PERCENT"
_CONFIG_WHERE = "config.yaml omp_accounts"
_POLICY_KEYS = ("accounts", "exclude", "max_usage_percent")


@dataclass(frozen=True)
class AccountPolicy:
    """Which of omp's Anthropic accounts an omp:anthropic seat may take. `exclude` holds
    lower-cased emails that are never taken. An account with a usage window at or above
    its threshold (percent: its `per_account` entry, else `max_usage_percent`) waits for
    that window's reset; unknown usage never skips. `problems` are invalid settings: while
    one exists the pool is not used, because the intended exclusions cannot be trusted."""

    exclude: tuple[str, ...] = ()
    max_usage_percent: float | None = None
    per_account: tuple[tuple[str, float], ...] = ()
    problems: tuple[str, ...] = ()
    exclude_source: str = ""
    max_usage_source: str = ""
    budget: account_budget.AccountBudget | None = None

    @property
    def needs_usage(self) -> bool:
        return (
            self.max_usage_percent is not None
            or bool(self.per_account)
            or self.budget is not None
        )

    @property
    def restricts(self) -> bool:
        """True when the policy can refuse an account (or is invalid): omp must then not
        pick an account on its own. A strict budget refuses an account whose usage cannot
        be confirmed; a lenient one only refuses an account it can see over the ceiling."""
        return (
            bool(self.exclude)
            or self.max_usage_percent is not None
            or bool(self.per_account)
            or bool(self.problems)
            or (self.budget is not None and self.budget.strict)
        )

    def threshold_for(self, email: str | None) -> float | None:
        """The percent at which `email`'s account waits: its `per_account` entry, else the
        global `max_usage_percent`, lowered to the account budget's ceiling when one is
        configured (the stricter of the two wins); None when neither sets one."""
        key = (email or "").lower()
        configured = self.max_usage_percent
        for name, percent in self.per_account:
            if name == key:
                configured = percent
                break
        if self.budget is None:
            return configured
        ceiling = self.budget.ceiling_for(PROVIDER)
        return ceiling if configured is None else min(configured, ceiling)

    def is_excluded(self, account: OmpAccount) -> bool:
        return bool(account.email) and account.email.lower() in self.exclude

    def _over_limit(
        self, account: OmpAccount, usage: list[UsageWindow] | None, now_ms: int
    ) -> list[tuple[UsageWindow, float]]:
        """(window, threshold) for each of `account`'s windows, any tier, at or above its
        threshold that has not reset. The fraction is compared unrounded: 89.99% is
        under a 90% threshold."""
        threshold = self.threshold_for(account.email)
        if threshold is None or not usage:
            return []
        return [
            (w, threshold)
            for w in usage
            if w.used_fraction >= threshold / 100
            and (w.resets_at_ms is None or w.resets_at_ms > now_ms)
        ]

    def _budget_hold(
        self,
        account: OmpAccount,
        usage: list[UsageWindow] | None,
        omp_model: str | None,
        now_ms: int,
    ) -> str | None:
        """The account budget's hold (review-cli#696) for a seat on `omp_model` taking
        `account`, else None. The text leaves the account out: the caller names it,
        redacted. Only this account's own windows are read (`usage_of` matches by email);
        a tier window counts for that tier's models and never completes the 5-hour/7-day
        pair."""
        if self.budget is None:
            return None
        readings = [
            account_budget.Reading(
                window=name,
                percent=w.used_fraction * 100,
                resets_at=None if w.resets_at_ms is None else w.resets_at_ms / 1000,
                shared=w.tier is None,
            )
            for w in usage or []
            if (name := usage_window_name(w.label)) and tier_applies(w.tier, omp_model)
        ]
        gap = None
        if not account.email:
            gap = "the account's email is unknown, so omp usage cannot be matched to it"
        elif not usage:
            gap = "omp usage lists no windows for this account (or did not answer)"
        else:
            gap = _budget_observation_gap(self.budget, usage, omp_model, now_ms)
            if gap is not None:
                readings = []  # Cached/ambiguous data is not a current quota observation.
        return account_budget.hold(
            self.budget,
            PROVIDER,
            readings,
            now=now_ms / 1000,
            name_account=False,
            gap=gap,
        )

    def skip_reason(
        self,
        account: OmpAccount,
        usage: list[UsageWindow] | None,
        omp_model: str | None,
        now_ms: int,
    ) -> str | None:
        """Why a seat on `omp_model` must not take `account` now, else None. An account
        without a known email cannot be checked against an exclude list, so it is skipped
        while one is set. A tier window (e.g. Fable's, tier "fable") counts only for that
        tier's models (`tier_applies`, as omp's own tier blocks do); `omp_model=None`
        counts the shared windows only (`tier_over_limit` lists the tier ones)."""
        if self.exclude and not account.email:
            return f"EMAIL UNKNOWN (cannot be checked against {self.exclude_source})"
        if self.is_excluded(account):
            return f"EXCLUDED ({self.exclude_source})"
        held = self._budget_hold(account, usage, omp_model, now_ms)
        if held is not None:
            return held
        over = [
            _window_text(w, threshold)
            for w, threshold in self._over_limit(account, usage, now_ms)
            if tier_applies(w.tier, omp_model)
        ]
        return "OVER LIMIT (" + "; ".join(over) + ")" if over else None

    def tier_over_limit(
        self, account: OmpAccount, usage: list[UsageWindow] | None, now_ms: int
    ) -> tuple[str, ...]:
        """`<tier> OVER LIMIT (...)` for each tier window at or above the threshold: the
        account is skipped for that tier's models only. Empty for an excluded account."""
        if self.is_excluded(account):
            return ()
        return tuple(
            f"{w.tier} OVER LIMIT ({_window_text(w, threshold)})"
            for w, threshold in self._over_limit(account, usage, now_ms)
            if w.tier
        )

    def summary(self) -> str:
        """One line: the exclude list and thresholds with where each came from."""
        parts = [
            f"exclude {', '.join(self.exclude)} ({self.exclude_source})"
            if self.exclude
            else "exclude none"
        ]
        parts.append(
            f"max usage {_percent_text(self.max_usage_percent)}% ({self.max_usage_source})"
            if self.max_usage_percent is not None
            else "max usage none (accounts are used up to their limit)"
        )
        if self.budget is not None:
            parts.append(self.budget.summary())
        if self.per_account:
            parts.append(
                "per account "
                + ", ".join(f"{e} {_percent_text(p)}%" for e, p in self.per_account)
                + f" ({_CONFIG_WHERE}.accounts)"
            )
        return " · ".join(parts)


def _window_text(window: UsageWindow, threshold: float) -> str:
    resets = (
        f"resets {utc_stamp(window.resets_at_ms)}"
        if window.resets_at_ms is not None
        else "reset time unknown"
    )
    return (
        f"{window.label.removeprefix('Claude ')} "
        f"{_percent_text(window.used_fraction * 100)}% "
        f">= {_percent_text(threshold)}%, {resets}"
    )


def _percent_text(value: float) -> str:
    rounded = round(value, 1)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


def _percent(
    value: object, where: str, *, allow_text: bool = False
) -> tuple[float | None, str | None]:
    """(percent, None) for a number in (0, 100], else (None, the problem). A numeric
    string is accepted only with `allow_text` (an env value)."""
    number: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str) and allow_text:
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is None or not math.isfinite(number) or not 0 < number <= 100:
        return None, f"{where} must be a number above 0 and at most 100, got {value!r}"
    return number, None


def _is_email_token(value: object) -> bool:
    """A configured account email: printable, no whitespace (it is printed into status
    output, so control characters are refused)."""
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def _config_exclude(listed: object) -> tuple[tuple[str, ...], str | None]:
    where = f"{_CONFIG_WHERE}.exclude"
    if isinstance(listed, list) and all(
        isinstance(e, str) and _is_email_token(e.strip()) for e in listed
    ):
        return tuple(dict.fromkeys(e.strip().lower() for e in listed)), None
    return (), f"{where} must be a list of account emails, got {listed!r}"


def _config_per_account(accounts: object) -> tuple[dict[str, float], list[str]]:
    if not isinstance(accounts, dict):
        return {}, [
            f"{_CONFIG_WHERE}.accounts must map an email to settings, got {accounts!r}"
        ]
    out: dict[str, float] = {}
    problems: list[str] = []
    for email, entry in accounts.items():
        if not isinstance(email, str) or not _is_email_token(email.strip()):
            problems.append(f"{_CONFIG_WHERE}.accounts: {email!r} is not an email")
            continue
        where = f"{_CONFIG_WHERE}.accounts.{email.strip()}"
        if not isinstance(entry, dict) or set(entry) != {"max_usage_percent"}:
            problems.append(f"{where} must be {{max_usage_percent: N}}, got {entry!r}")
            continue
        percent, problem = _percent(
            entry["max_usage_percent"], f"{where}.max_usage_percent"
        )
        if problem:
            problems.append(problem)
        else:
            out[email.strip().lower()] = percent
    return out, problems


def account_policy(
    raw: object,
    env: Mapping[str, str],
    budget: account_budget.AccountBudget | None = None,
) -> AccountPolicy:
    """Resolve configuration and environment overrides without validating replaced fields."""
    problems: list[str] = []
    env_exclude, env_max = _policy_environment(env, problems)
    exclude, max_percent, per_account, exclude_source, max_source = (
        _policy_config_values(raw, env_exclude, env_max, problems)
    )
    if env_exclude:
        exclude, exclude_source = env_exclude, f"${EXCLUDE_ENV}"
    if env_max:
        max_percent, problem = _percent(env_max, f"${MAX_USAGE_ENV}", allow_text=True)
        if problem:
            problems.append(problem)
        else:
            max_source = f"${MAX_USAGE_ENV}"
    return AccountPolicy(
        exclude=exclude,
        max_usage_percent=max_percent,
        per_account=tuple(sorted(per_account.items())),
        problems=tuple(problems),
        exclude_source=exclude_source,
        max_usage_source=max_source,
        budget=budget,
    )


def usage_of(
    usage: dict[str, list[UsageWindow]] | None, account: OmpAccount
) -> list[UsageWindow] | None:
    """`account`'s windows from a `usage_by_email` map (keys are lower-cased emails)."""
    if usage is None or not account.email:
        return None
    return usage.get(account.email.lower())


def pool_refusal(
    policy: AccountPolicy,
    accounts: list[OmpAccount] | None,
    db: Path,
    usage: dict[str, list[UsageWindow]] | None,
    omp_model: str | None,
    now_ms: int,
) -> tuple[str | None, list[OmpAccount], list[str]]:
    """The account policy's verdict for a seat on `omp_model` over `accounts` (None: the
    auth db `db` was unreadable): (why no account may be used, else None; the accounts
    the seat may take; `<account>: <reason>` for each one it may not). With no refusal
    and no accounts, omp picks the credential itself, which only an empty policy allows.
    Shared by dispatch and `review seats` so both give the same reason."""
    if policy.problems:
        return _invalid_reason(policy), [], []
    if not accounts:
        if not policy.restricts:
            return None, [], []
        where = (
            f"no readable omp auth db at {db}"
            if accounts is None
            else f"no omp {PROVIDER} OAuth account in {db} (omp would pick its own "
            "credential)"
        )
        return (
            f"cannot apply omp account settings ({policy.summary()}): {where}",
            [],
            [],
        )
    eligible: list[OmpAccount] = []
    skipped: list[str] = []
    for account in accounts:
        why = policy.skip_reason(account, usage_of(usage, account), omp_model, now_ms)
        if why is None:
            eligible.append(account)
            continue
        # a budget hold is printed into review output and logs: the account is redacted
        label = (
            account_budget.redact_account(account.email)
            if account.email and account_budget.is_budget_hold(why)
            else account.label
        )
        skipped.append(f"{label}: {why}")
    if not eligible:
        reason = (
            f"no omp {PROVIDER} account may be used ({len(accounts)}): "
            + "; ".join(skipped)
        )
        return reason, [], skipped
    return None, eligible, skipped


def _invalid_reason(policy: AccountPolicy) -> str:
    return (
        f"invalid omp account settings, no omp {PROVIDER} account is used until they "
        f"are fixed: {'; '.join(policy.problems)}"
    )


def profiles_reason(policy: AccountPolicy) -> str | None:
    """Why `$REVIEW_OMP_CLAUDE_PROFILES` profiles must not serve an omp:anthropic seat
    under `policy` (a profile's account is not known), None when the policy is empty."""
    if policy.problems:
        return _invalid_reason(policy)
    if policy.budget is not None and policy.budget.strict:
        return account_budget.hold(
            policy.budget,
            PROVIDER,
            [],
            now=0.0,
            name_account=False,
            gap=(
                "the account behind a $REVIEW_OMP_CLAUDE_PROFILES profile is not known, "
                "so its usage cannot be checked; unset the profiles"
            ),
        )
    if not policy.restricts:
        return None
    return (
        f"omp account settings ({policy.summary()}) cannot be applied to "
        "$REVIEW_OMP_CLAUDE_PROFILES profiles (their accounts are not known); unset one "
        "of them"
    )


def _active_credential_blocks(conn, now):
    blocks: dict[int, list[tuple[str, int]]] = {}
    try:
        rows = conn.execute(
            "SELECT credential_id, block_scope, MAX(blocked_until_ms) "
            "FROM auth_credential_blocks WHERE provider_key = ? AND blocked_until_ms > ? "
            "GROUP BY credential_id, block_scope",
            (_BLOCK_PROVIDER_KEY, now),
        )
        for credential, scope, until in rows:
            until_ms = _finite_int(until)
            if isinstance(credential, int) and until_ms is not None:
                label = scope if isinstance(scope, str) else ""
                blocks.setdefault(credential, []).append((label, until_ms))
    except sqlite3.OperationalError:
        pass  # Older schemas have no block table.
    return blocks


def _report_usage_windows(report, observation_key):
    windows = []
    limits = report.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        window = _usage_window(
            limit, _finite_int(report.get("fetchedAt")), observation_key
        )
        if window is not None:
            windows.append(window)
    return windows


def _usage_window(limit, observed_at_ms, observation_key):
    if not isinstance(limit, dict):
        return None
    amount, window = limit.get("amount"), limit.get("window")
    used = amount.get("usedFraction") if isinstance(amount, dict) else None
    if _finite_int(used) is None:
        return None
    resets = window.get("resetsAt") if isinstance(window, dict) else None
    scope = limit.get("scope")
    tier = scope.get("tier") if isinstance(scope, dict) else None
    return UsageWindow(
        label=str(limit.get("label") or limit.get("id") or "?"),
        used_fraction=float(used),
        resets_at_ms=_finite_int(resets),
        tier=tier if isinstance(tier, str) and tier else None,
        observed_at_ms=observed_at_ms,
        observation_key=observation_key,
    )


def _policy_environment(env, problems):
    tokens = [
        value.strip()
        for value in (env.get(EXCLUDE_ENV) or "").split(",")
        if value.strip()
    ]
    exclude = tuple(
        dict.fromkeys(value.lower() for value in tokens if _is_email_token(value))
    )
    problems.extend(
        f"${EXCLUDE_ENV}: {value!r} is not an email"
        for value in tokens
        if not _is_email_token(value)
    )
    return exclude, (env.get(MAX_USAGE_ENV) or "").strip()


def _policy_config_values(raw, env_exclude, env_max, problems):
    if not isinstance(raw, dict):
        if raw is not None:
            problems.append(f"{_CONFIG_WHERE} must be a mapping, got {raw!r}")
        return (), None, {}, "", ""
    unknown = sorted(str(key) for key in raw if key not in _POLICY_KEYS)
    if unknown:
        problems.append(
            f"{_CONFIG_WHERE}: unknown key {', '.join(unknown)} (known: {', '.join(_POLICY_KEYS)})"
        )
    exclude, exclude_source = _policy_exclude_value(
        raw.get("exclude"), env_exclude, problems
    )
    maximum, max_source = _policy_max_value(
        raw.get("max_usage_percent"), env_max, problems
    )
    per_account = {}
    if raw.get("accounts") is not None:
        per_account, account_problems = _config_per_account(raw["accounts"])
        problems.extend(account_problems)
    return exclude, maximum, per_account, exclude_source, max_source


def _policy_exclude_value(raw, overridden, problems):
    if raw is None or overridden:
        return (), ""
    exclude, problem = _config_exclude(raw)
    if problem:
        problems.append(problem)
        return exclude, ""
    return exclude, f"{_CONFIG_WHERE}.exclude"


def _policy_max_value(raw, overridden, problems):
    if raw is None or overridden:
        return None, ""
    maximum, problem = _percent(raw, f"{_CONFIG_WHERE}.max_usage_percent")
    if problem:
        problems.append(problem)
        return maximum, ""
    return maximum, f"{_CONFIG_WHERE}.max_usage_percent"


def _budget_observation_gap(budget, windows, model, now_ms):
    """Validate original provider timestamps and complete pairs within one report.

    Reading the cached `omp usage` response does not refresh its `fetchedAt`.
    Five seconds of clock skew tolerates sampling and rendering in one operation.
    Distinct partial reports never manufacture a complete shared-window pair.
    """
    groups = {}
    for window in windows:
        if not tier_applies(window.tier, model):
            continue
        observed = window.observed_at_ms
        if observed is None or observed <= 0:
            return "provider observation time is unknown"
        age = now_ms - observed
        if age < -5000:
            return "provider observation time is in the future"
        if age > budget.max_age_seconds * 1000:
            return f"provider observation is stale ({int(age / 1000)}s old)"
        groups.setdefault(window.observation_key, []).append(window)
    for group in groups.values():
        if not _observation_has_shared_pair(group, now_ms):
            return (
                "one provider observation does not contain both current shared windows"
            )
    return None


def _observation_has_shared_pair(windows, now_ms):
    names = {
        usage_window_name(window.label)
        for window in windows
        if window.tier is None
        and (window.resets_at_ms is None or window.resets_at_ms > now_ms)
    }
    return {account_budget.WINDOW_5H, account_budget.WINDOW_7D} <= names
