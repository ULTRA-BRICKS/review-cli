"""review CLI entry: argparse + dispatch only.

This is the package entry point (`[project.scripts] review = "reviewlib.cli:main"`
and the target of the thin `bin/review` shim). It owns argument parsing, diff
acquisition, model selection, and dispatch to the mode functions. All behaviour
lives in the sibling modules — this file is the thin entry the Stage 0
decomposition was about.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, TextIO

from . import admission
from . import backends
from . import jobs
from . import portable
from . import usage_limits
from .backends import _which  # re-export for tests/compat  # noqa: F401
from .backstop import COMMAND_TIMEOUT_GRACE_SECONDS, command_deadline, run_backstop
from .process import _open_log_with_fallback, install_signal_reaper
from .config import (
    CONFIG_PATH,
    DEFAULT_PRESET,
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    SHIP_MIN_ROLES,
    DEFAULT_PROMPT,
    MODERATOR_CANDIDATES,
    PANEL_TIMEOUT_DEFAULT,
    QA_TIMEOUT_DEFAULT,
    VISUAL_MODELS,
    BoardConfigError,
    BoardReviewer,
    EffortValueError,
    _drop_malformed_board_entries,
    _expand_alias,
    _effective_pool_size,
    _split_models,
    apply_effort_override,
    board_from_models,
    ensure_min_distinct_roles,
    expand_flat_models_with_reuse,
    lens_override_path,
    lens_override_text,
    load_board,
    load_config,
    prefer_distinct_roles,
    parse_effort_flag,
    preset_board,
    preset_names,
    preset_pool_size,
    recorded_task_pool_size,
    select_pool_and_reserve_with_reuse,
)
from .usage_limits import usage_percent_for_model
from .pool_guard import (
    PROCEED,
    Candidate,
    default_distinct_key,
    evaluate_selection,
)
from .install import install_commit_hook, install_hook_tg, install_skill
from .modes.brainstorm import mode_brainstorm
from .modes.contract import ModeContext, ModeSpec
from .modes.just_ask import mode_just_ask
from .modes.quorum import mode_quorum
from .modes.registry import (
    REMOVED_FLAGS,
    REMOVED_MODE_FLAGS,
    REMOVED_SUBCOMMANDS,
    brainstorm_pool,
    diff_mode,
    get_mode,
    iter_modes,
    known_subcommands,
)
from .modes.review import mode_review
from .panel import begin_call_tally, end_call_tally, pick_moderators
from .process import _run, git_repo_env, strip_control_sequences
from .retry import max_retry_count
from .stats import (
    STATS_VERSION,
    announce_eta,
    diff_content_hash,
    extract_diff_files,
    fmt_duration,
    iterations_for_task,
    normalize_repo_remote,
    normalize_task_code,
    parse_iso_ts,
    quorum_check,
    record_run,
    task_summaries,
)

if TYPE_CHECKING:
    from agenttools_service import ServiceManager

# `review task --check` identity modes (review-cli#589): repo AND touched-file overlap,
# repository alone (`--verify-repo-only`), or none (`--no-verify-identity`).
IdentityMode = Literal["full", "repo_only", "disabled"]

# Keep the mode-handler names imported off `cli` for legacy import compatibility ONLY
# (some external/legacy callers `from reviewlib.cli import mode_review`). Dispatch goes
# through `modes/registry`, so rebinding `cli.mode_*` has NO effect on the running mode.
# NEW tests/code must patch the handler in its OWN module (e.g.
# `reviewlib.modes.review.mode_review`) or configure the `ModeSpec`, never via `cli`.
# This tuple just keeps the names referenced so the imports aren't flagged unused.
__mode_fns__ = (mode_brainstorm, mode_just_ask, mode_quorum, mode_review)

# Stable, per-class exit codes (structured-exit-codes). The diff-review path REQUIRES a
# git repo; run it outside one and it must fail GRACEFULLY with this distinct code (a
# "wrong place to run this" usage class), NOT a raw traceback / generic crash. Scripts can
# branch on it; it stays stable. 0=success, 2=argparse/usage (argparse's own), 124=backstop
# (reviewlib.backstop). 3 is the not-a-repo class — distinct from argparse-2 so a caller can
# tell "you ran the diff review outside a repo" apart from "you mistyped a flag".
EXIT_NOT_A_REPO = 3
# 4 is the "in a repo, but `git diff` itself failed" class (e.g. a wedged/timed-out git, a
# corrupt index) — distinct from EXIT_NOT_A_REPO (you ARE in a repo) and argparse-2. The
# REQUIRED review path catches the RuntimeError `_git_diff` raises so this never tracebacks.
EXIT_GIT_DIFF_FAILED = 4
# qa mode (review-qa.md §4/§6): "qa ran but no test-case suites/cases are authored for the
# target" — a CONTRACT failure (a green qa run with zero authored cases is a lie), distinct
# from a real finding, so CI can tell "you didn't author any suites" apart from "a test
# failed". The qa handler's no-suites gate returns this BEFORE any agent/docker/browser.
#
# Value is 6, NOT the 5 the spec's §6 first proposed: code 5 is already taken at the
# PROCESS-exit level by brainstorm's EXIT_DEAD_PANEL (modes/brainstorm.py), and structured
# exit codes must stay per-class distinct, so a script seeing 5 can't tell "brainstorm dead
# panel" from "qa no suites". qa's own codes therefore start at the next free integer (6);
# the later qa env classes (NO_ENV / ENV_UNHEALTHY / SUT_BOOT_FAILED) continue from 7 in
# Phase 2/3 (the spec's 5/6/7/8 block shifts up by one for the brainstorm collision).
EXIT_QA_NO_SUITES = 6
# qa env classes (review-qa.md §6, shifted +1 from the spec's 5/6/7/8 because code 5 is
# brainstorm's EXIT_DEAD_PANEL — qa's own block starts at EXIT_QA_NO_SUITES=6). Phase 2
# lands the executor; the only env class it needs is "could not bring the SUT up at all"
# (the agent emitted VERDICT: BLOCKED) — distinct from a real finding (which is report-only)
# and from no-suites (6), so CI tells "infra broke" apart from "a bug was found". The
# NO_ENV / ENV_UNHEALTHY classes belong to the (later) compose env harness — reserved here
# as 7/9 so the executor's BLOCKED code (8) sits between them in the same coherent block.
EXIT_QA_NO_ENV = (
    7  # no stage AND no bring-up config for a backend/bot SUT (env harness, later)
)
EXIT_QA_SUT_BOOT_FAILED = (
    8  # the tester could not bring the SUT up at all (VERDICT: BLOCKED)
)
EXIT_QA_ENV_UNHEALTHY = (
    9  # bring-up succeeded but the health gate timed out (env harness, later)
)
# 10 is the pool-selection FOOLPROOFING class (reviewlib.pool_guard.EXIT_UNSATISFIED): the
# resolved review selection could not converge, so the CLI printed a proposal / targeted
# per-provider error instead of dispatching a degenerate panel. Distinct from every class
# above so a script can tell "the review pool couldn't be assembled" apart from a finding,
# a usage error, or a not-a-repo run. The value is owned by pool_guard (single source).

# RETIRED Phase-1 scaffold code. Phase 1 returned 70 from the suites-resolved-but-no-executor
# branch ("not implemented yet"). Phase 2 lands the executor, so that branch is GONE — the
# constant is kept (and pointed at the real executor path) only so a stale caller/import does
# not break; nothing in the live code returns it anymore.
EXIT_QA_NOT_IMPLEMENTED = 70

# Codes 11/12 are CLAIMED by `modes/review.py` (EXIT_COMMIT_REQUIRES_STAGED / EXIT_COMMIT_
# FAILED, the `--commit` checkpoint feature) — defined there, not here, because that mode
# module can't import from cli.py without a circular import. Recorded here too so the next
# person picking a new top-level exit code doesn't re-use them.


def _is_git_repo(cwd: Path) -> bool:
    """Cheap, correct "is `cwd` inside a git work tree?" probe.

    `git rev-parse --is-inside-work-tree` is the canonical, fast check (exit 0 + `true`
    inside a work tree, non-zero outside). The whole point of this probe is to AVOID a raw
    traceback, so every way the spawn itself can blow up is caught and treated as "not a
    repo": OSError (a non-existent / non-directory `cwd` -> FileNotFoundError /
    NotADirectoryError, e.g. a stale `-C /missing/path`) and TimeoutExpired (a wedged `git
    rev-parse` -> `_run` forwards `timeout=` straight to subprocess.run, which raises). `_run`
    is `text=True, stdout=PIPE`, so `proc.stdout` is always a str (never None)."""
    try:
        proc = _run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def _fail_not_a_repo(cwd: Path) -> int:
    """Print the 3-part WHAT/WHY/HOW message for "ran the diff review outside a repo" and
    return the stable EXIT_NOT_A_REPO code. Verb-named because it has a side effect (prints)
    AND returns the code. No traceback — this is an expected user error, not a crash."""
    print(
        f"[review-cli] not in a git repository ({cwd}).\n"
        "  the diff review needs a repo to diff (it reviews your working-tree / staged changes).\n"
        '  fix: run a mode that needs no git — `review just-ask "..." --task CODE` / '
        '`review quorum "..." --task CODE` / `review brainstorm "..." --task CODE` — '
        "or cd into a repo and re-run.",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_NOT_A_REPO


def _fail_git_diff(cwd: Path, exc: Exception) -> int:
    """Print a structured error for "in a git repo, but `git diff` failed" (the REQUIRED
    review path) and return the stable EXIT_GIT_DIFF_FAILED code. `_is_git_repo` passing does
    NOT guarantee `git diff` succeeds (a wedged/timed-out git, a corrupt index), so this is
    the no-traceback floor for that path — an expected runtime failure, not a crash."""
    print(
        f"[review-cli] could not read the git diff in {cwd}.\n"
        f"  git diff failed: {exc}\n"
        "  fix: check the repo is healthy (`git status`), or pipe a diff on stdin "
        "(`git diff | review diff --task CODE`).",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_GIT_DIFF_FAILED


def _git_diff(cwd: Path, staged: bool) -> str:
    """Return the working-tree (or --staged) diff. Raises RuntimeError on ANY failure —
    a non-zero `git diff`, a spawn failure (missing/non-dir `cwd` -> OSError, or a missing
    git binary -> FileNotFoundError), or a wedged git (TimeoutExpired). Normalizing every
    failure to the single RuntimeError type is what lets each OPTIONAL caller (--visual /
    brainstorm / panel --diff|--staged) catch it and degrade to "". The REQUIRED review path
    is gated by `_is_git_repo` first, so the common non-repo case is handled gracefully
    there; a RARE in-repo `git diff` failure (a wedge, a corrupt repo) on that path still
    surfaces as the RuntimeError above — a clean one-line error, not a silent wrong result.

    The diff is anchored to `cwd` TWICE — `git -C <cwd>` AND `env=git_repo_env(cwd)` (FOREIGN
    repo-pinning git vars dropped) — so a `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` leaked from
    a parent pointing at an UNRELATED repo can't silently divert the diff. Without it, `git -C
    /repoB diff --cached` reads the env's repo, not repoB — the review-gate then reviews the
    wrong (or empty) diff (review-cli#71). `git_repo_env` KEEPS the target repo's own hook env
    (a legit pre-commit's GIT_INDEX_FILE/temp `next-index` that scopes `--cached` to the partial
    commit), dropping only env vars that resolve outside `cwd`'s git dir (codex P2 on PR #72).

    `--src-prefix=a/ --dst-prefix=b/` pins the header format regardless of the invoking
    machine's `diff.noprefix`/custom-prefix git config (some machines set
    `diff.noprefix=true` globally, which emits headers with NO a/b prefix at all —
    `diff --git f.txt f.txt` — silently breaking `stats.extract_diff_files`'s
    `diff --git a/<path> b/<path>` parse, found live on this repo's own dev machine).
    Content is unaffected — only the path labels in the header/`---`/`+++` lines."""
    args = [
        "git",
        "-C",
        str(cwd),
        "diff",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ]
    if staged:
        args.append("--cached")
    try:
        proc = _run(args, cwd=cwd, env=git_repo_env(cwd), timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git diff could not run: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return proc.stdout


def _stamp_hash_for_staged_diff(cwd: Path) -> str | None:
    """Sha256 of `git diff --no-ext-diff --cached` with NO `--src-prefix`/
    `--dst-prefix` — i.e. exactly what the pre-commit hook's own independent
    verification computes, whatever the machine's ambient `diff.noprefix`/prefix
    git config happens to produce. None on any failure (best-effort; the review
    still proceeds without the tightened stamp, falling back to
    `_write_review_stamp`'s own re-derive at write time).

    Called ONCE, immediately adjacent to the `_git_diff(cwd, staged=True)` call
    that captures the diff actually sent to the models (reviewlib.install
    "_write_review_stamp" docstring has the full story of why this exists —
    round-5 review finding, k3+Opus: hashing at stamp-WRITE time instead of
    dispatch-CAPTURE time reopened a multi-minute TOCTOU window where a
    concurrent index mutation during the panel run could get silently
    certified as reviewed)."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    import hashlib

    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _git_remote_origin_url(cwd: Path) -> str | None:
    """`git -C cwd remote get-url origin`, or None on ANY failure (no remote, not a
    repo, no git binary, a wedge) — best-effort, this only feeds an identity label,
    never a required path."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _git_toplevel(cwd: Path) -> Path | None:
    """`git -C cwd rev-parse --show-toplevel`, or None on any failure (not a repo,
    no git binary, a wedge)."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def _compute_repo_id(cwd: Path) -> str | None:
    """Best-effort stable identity for the repo at `cwd`, for run-stats diff-identity
    binding (reviewlib.stats "Diff-identity binding") — see that module's docstring for
    WHY this exists (closes 3 real task-code quorum-pollution incidents).

    Prefers the normalized `origin` remote URL (stable across worktrees/clones of the
    SAME repo, unlike a local path). Falls back to the resolved absolute repo root
    for a remote-less repo, prefixed `path:` so a path-based id can never collide with
    a remote-based one. The path fallback SELF-NORMALIZES via `_git_toplevel` rather
    than trusting `cwd` as-is (review finding on this feature's own PR, round 2: every
    CURRENT caller already passes an `_effective_cwd`-resolved toplevel, so this was
    not a live bug today, but the id would silently diverge — `path:/repo/subdir` at
    record time vs `path:/repo` at check time, a spurious `repo_mismatch` — the moment
    any future caller passed a subdirectory, and nothing enforced the invariant this
    function's OWN docstring asserted). Falls back to `cwd` itself only when `cwd` is
    a real directory but git can't resolve its toplevel (a non-repo directory, kept
    for the same "reviewing it as-is" posture `_effective_cwd` already has elsewhere).
    None only when `cwd` isn't even a real directory.
    """
    url = _git_remote_origin_url(cwd)
    if url:
        normalized = normalize_repo_remote(url)
        if normalized:
            return normalized
    toplevel = _git_toplevel(cwd)
    if toplevel is not None:
        return f"path:{toplevel}"
    try:
        if cwd.is_dir():
            return f"path:{cwd}"
    except OSError:
        pass
    return None


def _default_branch_ref(cwd: Path) -> str | None:
    """Best-effort `origin/<default-branch>` ref for `cwd`, or None.

    Tries the recorded `origin/HEAD` symref first (what `git clone` sets up), then
    falls back to probing the two common default-branch names directly — a shallow
    CI checkout or a repo cloned with `--single-branch` may lack `origin/HEAD`
    entirely. Feeds `--check`'s diff-identity file-set comparison for the common
    post-push case (clean working tree, nothing to diff locally); never raises.
    """
    try:
        proc = _run(
            ["git", "-C", str(cwd), "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().replace("refs/remotes/", "")
    for candidate in ("origin/main", "origin/master"):
        try:
            proc = _run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", candidate],
                cwd=cwd,
                env=git_repo_env(cwd),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return candidate
    return None


def _diff_name_only(cwd: Path, ref_args: list[str]) -> list[str] | None:
    """`git diff --name-only <ref_args>` -> sorted touched-file list, or None on ANY
    failure (non-repo, unresolvable ref, a wedge). `--name-only` sidesteps the
    `diff.noprefix` footgun `_git_diff` otherwise has to pin `--src-prefix`/
    `--dst-prefix` against (`extract_diff_files`'s `diff --git a/... b/...` regex
    doesn't even apply here — there IS no `diff --git` header, just bare file
    paths) — a real bug this exact fallback shipped with once already (codex/
    GLM/opus/fable review finding on this feature's own PR: the first cut only
    pinned the prefix on `_git_diff`, not on this fallback's own separate `git
    diff` call, silently disabling file-level matching on any `diff.noprefix=true`
    machine — precisely the class this feature targets). Also far cheaper than a
    full patch body for an identity check: file NAMES only, not hunks."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--name-only", *ref_args],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _current_diff_files_for_check(cwd: Path) -> list[str] | None:
    """Best-effort file list for the diff currently under review at `cwd`, for
    `--check`'s repo/diff mismatch detection (reviewlib.stats "Diff-identity
    binding").

    Returns the UNION of `git diff --name-only HEAD` (local uncommitted changes —
    staged AND unstaged together, the dev-loop case) and the branch's diff against
    its own default branch when one resolves (the post-push `gh ship` case).
    Deliberately a UNION, not first-non-empty-wins: two independent review
    findings (Opus + Fable) on this feature's own PR caught that "HEAD probe wins
    whenever the tree has ANY uncommitted change" lets a single unrelated dirty
    file at check time (a stray edit, a version bump in progress) SHADOW the
    branch's real PR files entirely — the post-push case is exactly when the
    branch diff is what matters, and it's exactly when a stray local edit is most
    likely to be sitting around. Unioning means a legitimate iteration whose files
    overlap the REAL branch diff still verifies even with local dirt present.
    Returns None (skip the file-level check; `repo_id` alone still gates) only
    when NEITHER source resolves to anything — never raises.
    """
    local = set(_diff_name_only(cwd, ["HEAD"]) or [])
    default_ref = _default_branch_ref(cwd)
    branch = (
        set(_diff_name_only(cwd, [f"{default_ref}...HEAD"]) or [])
        if default_ref
        else set()
    )
    union = sorted(local | branch)
    return union or None


STDIN_WAIT_ENV = "REVIEW_STDIN_WAIT_SECONDS"
STDIN_WAIT_DEFAULT_SECONDS = 5.0
_STDIN_CHUNK = 1 << 16


def _stdin_wait_seconds() -> float:
    """How long a non-tty stdin may stay silent before it counts as "nothing piped"
    (`$REVIEW_STDIN_WAIT_SECONDS`, default `STDIN_WAIT_DEFAULT_SECONDS`). A pipe producer
    (`git diff | review diff`) delivers its first bytes or EOF within milliseconds, so a
    few seconds of silence means the writer is a long-lived parent that will never
    write (review-cli#516/#613). Non-numeric / non-finite -> the default; negative -> 0."""
    raw = os.environ.get(STDIN_WAIT_ENV)
    if raw is None or not raw.strip():
        return STDIN_WAIT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return STDIN_WAIT_DEFAULT_SECONDS
    if not math.isfinite(value):
        return STDIN_WAIT_DEFAULT_SECONDS
    return max(value, 0.0)


def _stdin_unusable() -> bool:
    """True when stdin is absent, already closed, or a tty — nothing to read in any case.
    A daemonizer may close the Python-level `sys.stdin` (not just redirect fd 0), and
    `isatty()` on a closed stream raises, so the `closed` check comes first."""
    stdin = sys.stdin
    return stdin is None or getattr(stdin, "closed", False) or stdin.isatty()


def _stdin_fd() -> int | None:
    """fd 0 when stdin is a usable, non-tty stream; None otherwise, including a file-like
    without an fd (a test's StringIO — which cannot block, so the callers read it
    directly)."""
    if _stdin_unusable():
        return None
    try:
        return sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        return None


def _stdin_notice(text: str) -> None:
    print(f"[review-cli] {text}", file=sys.stderr, flush=True)


def _decode_stdin(raw: bytes) -> str:
    """Decode raw stdin bytes with stdin's encoding, always replacing undecodable bytes:
    a stray invalid byte in a piped diff must not raise out of `_dispatch` (the stream's
    own `errors` is normally "strict")."""
    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
    return raw.decode(encoding, "replace")


# Minimum per-chunk wait on the reader-thread path: `REVIEW_STDIN_WAIT_SECONDS=0` means
# "only data already waiting counts", and a thread needs a moment to hand over bytes a
# closed pipe already holds — a 0 s queue wait would race the scheduler and drop them.
_THREADED_WAIT_FLOOR = 0.1


def _read_fd_with_idle_timeout(
    fd: int, idle: float, *, pollable: bool | None = None
) -> bytes | None:
    """Read `fd` to EOF, giving up when it stays silent for `idle` seconds — whether
    before the first byte or in the middle of the stream (a launcher that writes one
    greeting byte and then holds the fd open must not park the read any more than a
    fully silent one). A producer that keeps writing is read to EOF however long the
    whole stream takes. None = gave up (the notice has been printed); bytes = EOF.

    Chunk-source contract (shared by `_polled_chunks` / `_threaded_chunks`): bytes = a
    chunk, b"" = EOF, None = idle timeout, OSError raised = the read itself failed. A
    failed read (ECONNRESET on a socket, say) is NOT EOF: the bytes so far are an
    incomplete diff, so they are discarded with a notice rather than reviewed.

    `select` cannot poll a non-socket fd on Windows (OSError) and refuses an fd past
    FD_SETSIZE (ValueError); such an fd is read by a daemon thread handing chunks over a
    queue, with the SAME idle timeout applied between chunks (`_threaded_chunks`), so no
    platform keeps the unbounded read. `pollable` forces one path (tests)."""
    if pollable is None:
        try:
            select.select([fd], [], [], 0)
            pollable = True
        except (OSError, ValueError):
            pollable = False
    next_chunk = _polled_chunks(fd, idle) if pollable else _threaded_chunks(fd, idle)
    chunks: list[bytes] = []
    while True:
        try:
            chunk = next_chunk()
        except OSError as exc:
            got = sum(len(c) for c in chunks)
            _stdin_notice(
                f"stdin read failed after {got} bytes ({exc.strerror or exc}) — treating"
                " it as no piped input."
            )
            return None
        if chunk is None:
            got = sum(len(c) for c in chunks)
            arrived = f"only {got} bytes" if got else "nothing"
            _stdin_notice(
                f"stdin is not a terminal but {arrived} arrived and it stayed silent for"
                f" {idle:g}s — treating it as no piped input (tune with {STDIN_WAIT_ENV})."
            )
            return None
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _polled_chunks(fd: int, idle: float) -> Callable[[], bytes | None]:
    """Chunk source over `select` (contract in `_read_fd_with_idle_timeout`)."""

    def _next() -> bytes | None:
        ready, _, _ = select.select([fd], [], [], idle)
        if not ready:
            return None
        return os.read(fd, _STDIN_CHUNK)

    return _next


def _threaded_chunks(fd: int, idle: float) -> Callable[[], bytes | None]:
    """Chunk source for an unpollable fd: a daemon thread blocks in `os.read` and hands
    each chunk (or the OSError it hit) over a queue; the caller waits at most `idle`
    (floored at `_THREADED_WAIT_FLOOR`) per chunk. Once the caller gives up, the thread
    stops at its next wakeup instead of draining the stream for the life of the process
    (no unbounded buffer, and the writer sees backpressure)."""
    import queue
    import threading

    handoff: queue.Queue[bytes | OSError] = queue.Queue()
    stop = threading.Event()
    wait = max(idle, _THREADED_WAIT_FLOOR)

    def _drain() -> None:
        while not stop.is_set():
            try:
                item: bytes | OSError = os.read(fd, _STDIN_CHUNK)
            except OSError as exc:
                item = exc
            if stop.is_set():
                return
            handoff.put(item)
            if not isinstance(item, bytes) or not item:
                return

    threading.Thread(target=_drain, name="review-stdin", daemon=True).start()

    def _next() -> bytes | None:
        try:
            item = handoff.get(timeout=wait)
        except queue.Empty:
            stop.set()
            return None
        if isinstance(item, OSError):
            raise item
        return item

    return _next


def _read_stdin_if_piped() -> str | None:
    """Return the diff piped on stdin, or None when stdin is a tty / empty / silent.

    The read is BOUNDED (review-cli#516, #613): an inherited non-tty stdin whose writer
    stays open but never writes (an omp eval kernel's socket, a detached launcher's pipe,
    `pty=false` agent sessions) used to park `sys.stdin.read()` forever — before any
    dispatch, log or heartbeat, while holding the host-wide review lock. Now the fd is
    read with an idle timeout of `_stdin_wait_seconds()` between bytes
    (`_read_fd_with_idle_timeout`); a stall -> one stderr line and "nothing piped"."""
    if _stdin_unusable():
        return None
    fd = _stdin_fd()
    if fd is None:
        # No fd to poll: a file-like substituted for stdin (tests use StringIO). Such an
        # object cannot block on a pipe, so the plain read is safe here.
        try:
            data = sys.stdin.read()
        except OSError:
            return None
        return data if data else None
    raw = _read_fd_with_idle_timeout(fd, _stdin_wait_seconds())
    if not raw:
        return None
    return _decode_stdin(raw)


def _warn_if_stdin_ignored(source_flag: str, what: str) -> None:
    """With an explicit `--staged` / `--diff` source, stdin is never read — but a caller
    who piped a diff anyway (`git show X | review diff --staged`) must hear that it was
    ignored rather than silently get a review of the index. A zero-timeout poll plus a
    one-byte read tells data (warn) apart from EOF (`< /dev/null`, `true |`: silent)
    without ever blocking; the byte is discarded like the rest of the stream."""
    fd = _stdin_fd()
    if fd is None:
        return
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return
        peek = os.read(fd, 1)
    except (OSError, ValueError):
        return
    if peek:
        _stdin_notice(
            f"ignoring piped stdin: {source_flag} names the diff source, so the {what}"
            " diff is reviewed."
        )


def _explicit_diff_source(args: argparse.Namespace) -> tuple[str, str] | None:
    """`(flag, what)` for an explicitly named diff source — `("--staged", "staged")` or
    `("--diff", "working-tree")` — or None when the verb may take a diff from stdin.
    The top-level parser (no subcommand) defines neither flag, hence `getattr`."""
    if getattr(args, "staged", False):
        return "--staged", "staged"
    if getattr(args, "diff", False):
        return "--diff", "working-tree"
    return None


def _effective_cwd(raw: str, *, warn: bool = True) -> Path:
    """Resolve the review cwd, preferring the enclosing git repository root.

    Agents commonly invoke `review` from a scratch / temp directory and forget
    -C, so the diff and the claude workspace silently point at the wrong place
    (often /tmp) and the review is empty or about the wrong code. Resolve to the
    git toplevel when inside a repo (also robust to being run from a subdir), and
    warn loudly when the cwd is not a git repo at all so the mistake is visible
    instead of producing a misleading review. Pass -C <project-root> to be exact.

    `warn=False` suppresses the non-repo "reviewing it as-is" warning for a caller that
    will itself print a clearer message: the review-mode diff path hard-fails via
    `_fail_not_a_repo`, so the "as-is" promise would contradict that hard-fail.
    """
    resolved = Path(raw).expanduser().resolve()
    if resolved.is_dir():
        # This runs on EVERY invocation, BEFORE mode dispatch — including the no-git modes
        # (just-ask / quorum / brainstorm) that must "work anywhere". So the git spawn here
        # must NEVER leak a raw traceback: a missing git binary (OSError -> FileNotFoundError)
        # or a wedged `git rev-parse` (TimeoutExpired) degrades to "review the dir as-is",
        # exactly like a non-repo dir — same defensive catch as `_is_git_repo`. The rev-parse
        # is anchored to `resolved` AND runs with the repo-pinning git env stripped
        # (git_repo_env) so a leaked GIT_DIR/GIT_WORK_TREE can't resolve the toplevel to an
        # UNRELATED repo — the same #71 footgun the diff probe guards against.
        try:
            proc = _run(
                ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                cwd=resolved,
                env=git_repo_env(resolved),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    if warn:
        print(
            f"[review-cli] warning: {resolved} is not inside a git repository; "
            "reviewing it as-is — pass -C <project-root> to point review at your repo.",
            file=sys.stderr,
            flush=True,
        )
    return resolved


# Shared between the `__serve` parser and the managed-service parser so the two surfaces never
# drift on what `--host` means (loopback-only by default; 0.0.0.0 exposes over Tailscale).
_DASHBOARD_HOST_HELP = "interface to bind (default: 127.0.0.1 loopback-only; 0.0.0.0 exposes over Tailscale)"

# Printed (exit 4) when a genuine lifecycle action is requested but the shared service lib is
# absent. Kept as a module constant so the smoke test can grep it and it stays aligned with the
# `pyproject.toml [dashboard]` extra comment that explains the same in-ecosystem dependency.
_AGENTTOOLS_SERVICE_MISSING_MSG = (
    "[review dashboard] the managed-service subcommands need the shared "
    "'agenttools_service' lib, which isn't installed.\n"
    "  why:  run/start/status/stop/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon "
    "-e <agent-tools>/lib/agenttools_service\n"
    "  note: `review dashboard run` still works without it for an ad-hoc foreground server."
)


def _dashboard_help_no_lib() -> int:
    """Help for a BARE ``review dashboard`` when the service lib is ABSENT (still launches nothing).

    Mirrors the lib-present help surface — it advertises every lifecycle action so the bare-HELP
    contract (and the smoke test that greps for ``status`` / the action names) holds regardless of
    whether ``agenttools_service`` is installed — while making clear those actions need the lib."""
    print(
        "usage: review dashboard [--host H] [--port N] "
        "{run,start,stop,status,enable,disable}\n\n"
        "review-cli dashboard as a managed service. A bare `review dashboard` prints this help "
        "and launches nothing.\n\n"
        "actions:\n"
        "  run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled\n"
        "  start    start in the BACKGROUND (detached daemon); return immediately\n"
        "  status   is it running? pid / port / url / autostart-enabled\n"
        "  stop     stop the background instance\n"
        "  enable   install OS autostart (launchd / systemd --user / fallback) AND start now\n"
        "  disable  remove OS autostart AND stop\n\n"
        "note: start/status/stop/enable/disable need the shared 'agenttools_service' lib "
        "(pip install -e <agent-tools>/lib/agenttools_daemon "
        "-e <agent-tools>/lib/agenttools_service); `run` works without it."
    )
    return 0


def _dashboard_serve(rest: list[str]) -> int:
    """Hidden entry: ``review dashboard __serve`` — run the blocking web server in THIS shell.

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``):
    it calls ``run_dashboard`` directly and so never re-enters the service dispatcher (a bare
    ``run`` action whose argv pointed back at ``review dashboard run`` would fork-bomb). Not
    advertised in help — operators use ``review dashboard run`` (ad-hoc) or ``start`` (managed).

    Binds 127.0.0.1 by default; ``--host 0.0.0.0`` exposes it over Tailscale (mirrors
    ``review spec-web``). Imported lazily so the dashboard's stdlib HTTP stack never loads on
    the hot review path (and a stray import error in dashboard code can't break `review`)."""
    sub = argparse.ArgumentParser(
        prog="review dashboard __serve",
        description="Run the review-cli dashboard web server (blocking).",
    )
    sub.add_argument("--host", default="127.0.0.1", help=_DASHBOARD_HOST_HELP)
    sub.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default: a free ephemeral port)",
    )
    sub.add_argument(
        "--no-open", action="store_true", help="do not open a browser window"
    )
    sub.add_argument(
        "--verbose", action="store_true", help="log every HTTP request to stderr"
    )
    ns = sub.parse_args(rest)
    from .dashboard import run_dashboard

    return run_dashboard(
        port=ns.port, host=ns.host, open_browser=not ns.no_open, verbose=ns.verbose
    )


def _dashboard_subcommand(rest: list[str]) -> int:
    """`review dashboard [run|start|status|stop|enable|disable]` — the dashboard as a MANAGED
    service, plus the legacy ad-hoc ``run``.

    The lifecycle subcommands (run/start/status/stop/enable/disable) come from the shared
    ``agenttools_service`` lib (one service-manager for every long-running server in the
    ecosystem — review dashboard, config-web, tg-ctl, future daemons), NOT hand-rolled here:

      run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled.
      start    start in the BACKGROUND (detached daemon); return immediately.
      status   is it running? pid / port / url / autostart-enabled.
      stop     stop the background instance.
      enable   install OS autostart (launchd / systemd --user / no-systemd fallback) AND start now.
      disable  remove OS autostart AND stop.

    A BARE ``review dashboard`` (no action) prints HELP and launches NOTHING. ``--host`` /
    ``--port`` select the managed bind (a stable default port, unlike ad-hoc ``run``'s ephemeral
    one). Everything (the lib, the service descriptor) is imported lazily so the hot ``review``
    path never pays for the dashboard/service stack."""
    # Hidden foreground entry that the service argv points at (see dashboard.service._serve_argv).
    # Kept BEFORE the agenttools_service import so the blocking server still runs even on a host
    # where the shared service lib isn't installed (a detached `start` from a host that HAS the
    # lib can run `__serve` here).
    if rest and rest[0] == "__serve":
        return _dashboard_serve(rest[1:])

    try:
        from agenttools_service import add_service_subcommands, dispatch
    except ImportError:
        # No shared service lib on this host. The bare-HELP and ad-hoc `run` contracts must
        # still hold (they don't depend on the lib): a BARE `review dashboard` (or `--help`)
        # prints help and launches nothing, and `run` still brings up an ad-hoc foreground
        # server so a lib-less operator isn't fully blocked. Only the genuine lifecycle actions
        # (start/status/stop/enable/disable) need the lib — for those, emit an actionable error
        # (structured-exit-codes) instead of a raw ImportError traceback.
        action, action_idx = _dashboard_action_with_index(rest)
        # A BARE `review dashboard` OR a help-only `review dashboard [--help|-h]` (no lifecycle
        # action) prints help and launches nothing — exit 0. The help surface is the SAME
        # whether or not the lib is installed (the smoke test greps it for the action names),
        # so a lib-less operator still sees what the dashboard can do. `--help`/`-h` with no
        # action was previously mis-routed to the missing-lib error (exit 4), breaking the
        # bare-HELP contract — a `--help`/`-h` is NOT itself a lifecycle action.
        if action is None:
            return _dashboard_help_no_lib()
        if action == "run":
            # Ad-hoc foreground server still works without the lib. Drop ONLY the `run` action
            # token (by index, not by value — a value that happens to equal "run" must survive);
            # `_dashboard_serve` parses the rest as server flags (and prints its own --help/exit 0
            # for `run --help`, so the help contract holds there too).
            return _dashboard_serve(rest[:action_idx] + rest[action_idx + 1 :])
        # A genuine lifecycle action (start/status/stop/enable/disable) needs the lib — emit the
        # actionable missing-lib error (structured-exit-codes) instead of a raw ImportError.
        print(_AGENTTOOLS_SERVICE_MISSING_MSG, file=sys.stderr)
        return 4

    from .dashboard.service import DEFAULT_DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT

    parser = argparse.ArgumentParser(
        prog="review dashboard",
        description="review-cli dashboard as a managed service (run/start/status/stop/enable/disable).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_DASHBOARD_HOST, help=_DASHBOARD_HOST_HELP
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f"port to bind the managed dashboard (default: {DEFAULT_DASHBOARD_PORT})",
    )
    subs = parser.add_subparsers(dest="action")
    # Parse FIRST, then build the manager factory over the parsed namespace. The factory closes
    # over `ns`, so `ns` must exist before any action can invoke it; registering the subcommands
    # does not call the factory (it is lazy, per the lib contract), so registration order vs parse
    # is free — but binding `ns` explicitly avoids a fragile forward-reference closure.
    add_service_subcommands(
        subs,
        manager_factory=lambda: _dashboard_manager(ns.host, ns.port),
        service_name="dashboard",
    )
    ns = parser.parse_args(rest)

    def _print_help_ok() -> int:
        parser.print_help()
        return 0

    return dispatch(ns, on_no_subcommand=_print_help_ok)


def _dashboard_manager(host: str, port: int) -> "ServiceManager":
    """Build a ``ServiceManager`` for the dashboard at ``host:port`` (lazy, per the lib's
    ``manager_factory`` contract — constructed only when an action actually runs)."""
    from agenttools_service import ServiceManager

    from .dashboard.service import dashboard_service

    return ServiceManager(dashboard_service(port=port, host=host))


def _sessions_subcommand(rest: list[str]) -> int:
    """`review sessions [-a/--all] [-s/--resume <id>] [--force] [-m … --moderator …]`.

    List or RESUME brainstorm sessions parsed from the on-disk discussion logs. Kept as a
    bare subcommand (like `dashboard`) — it is a MANAGEMENT command over the logs, not a
    fan-out review mode, so it does not go through the mode registry. All session logic
    lives in `reviewlib.sessions` (lib); this handler is thin.

    Default listing (no `-a`) shows recent COMPLETED sessions (a sensible recent subset);
    `-a/--all` adds the dead/interrupted ones (crashed / killed / timed out — no synthesis)
    and lifts the cap. `-s <id>` RESUMES: it reloads the saved transcript and continues the
    brainstorm from `completed_round + 1`, reusing the saved topic / panel / moderator,
    then synthesizes — it does NOT start from scratch.
    """
    from . import sessions as _sessions

    sub = argparse.ArgumentParser(
        prog="review sessions",
        description="List or resume brainstorm sessions (parsed from the discussion logs).",
    )
    sub.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="list ALL sessions incl. dead/interrupted (no synthesis); default lists recent completed",
    )
    sub.add_argument(
        "-s",
        "--resume",
        metavar="ID",
        default=None,
        help="resume the session with this id (short id or unambiguous prefix): continue the round loop and synthesize",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="with --resume on an already-completed session, re-synthesize anyway",
    )
    # Resume reuses the saved panel/moderator by default; -m / --moderator override.
    sub.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help="override the resume panel (repeat or comma-separate); default = the saved session's panel",
    )
    sub.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="repository directory (resume diff/agentic cwd)",
    )
    sub.add_argument(
        "--moderator",
        default=None,
        help="override the resume moderator; default = the saved session's moderator",
    )
    # Grounding diff on resume: the original `--diff`/`--staged` grounding is NOT persisted
    # in the discussion log, so a resumed grounded brainstorm would otherwise continue
    # UNgrounded. These flags re-attach the current working-tree (--diff) or staged
    # (--staged) diff as grounding for the resumed rounds + synthesis (opt-in, like the
    # brainstorm mode's own grounding). Absent -> the resume runs ungrounded.
    sub.add_argument(
        "--diff",
        action="store_true",
        help="re-attach the working-tree diff as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--staged",
        action="store_true",
        help="re-attach the staged diff (git diff --cached) as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"per-call timeout seconds for the resumed rounds (default {PANEL_TIMEOUT_DEFAULT})",
    )
    ns = sub.parse_args(rest)

    if ns.resume:
        return _resume_session_cli(ns)

    sessions = _sessions.list_sessions(include_dead=ns.all)
    if not sessions:
        scope = "" if ns.all else " completed"
        print(f"No{scope} brainstorm sessions found in {_sessions.log_dir()}.")
        if not ns.all:
            print("(pass -a/--all to include dead/interrupted sessions.)")
        return 0
    header = (
        "all sessions (incl. interrupted)" if ns.all else "recent completed sessions"
    )
    print(
        f"Brainstorm {header} — newest first; resume with `review sessions -s <id>`:\n"
    )
    for s in sessions:
        ts = s.timestamp.strftime("%Y-%m-%d %H:%M UTC") if s.timestamp else "?"
        topic = (s.topic[:60] + "…") if len(s.topic) > 61 else (s.topic or "(no topic)")
        print(
            f"  {s.session_id}  [{s.status:<11}]  r{s.completed_rounds}  {ts}  {topic}"
        )
    return 0


def _resume_session_cli(ns: argparse.Namespace) -> int:
    """Resolve the saved session by id and continue its brainstorm. Thin over
    `reviewlib.sessions.resume_session`; resolves the panel/moderator (saved unless
    overridden) and reports the clean errors (unknown id / ambiguous prefix / already
    complete) with actionable messages + meaningful exit codes."""
    from . import backends, sessions as _sessions

    config = load_config()
    backends.configure_unpaid_providers(config.get("unpaid_providers"))
    backends.configure_omp_bin(config.get("omp_bin"))
    backends.configure_omp_accounts(config.get("omp_accounts"))

    try:
        sess = _sessions.find_session(ns.resume)
    except _sessions.AmbiguousSessionError as exc:
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2
    if sess is None:
        print(
            f"[review sessions] no session with id '{ns.resume}'. "
            "Run `review sessions -a` to list available ids.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    cwd = _effective_cwd(ns.cwd)
    # Panel: explicit -m override > the saved session panel (dropping unreachable
    # backends so a vanished key never aborts) > whatever the saved panel was.
    explicit_models = _split_models(ns.model)
    if explicit_models:
        models = explicit_models
    else:
        models = (
            [m for m in sess.panel if backends.backend_available(m)]
            or sess.panel
            or list(DEFAULT_MODELS)
        )
    # Moderator: explicit --moderator override > the saved session moderator > picked.
    # The log records the moderator FALLBACK CHAIN joined with `>` (e.g. `claude:..>codex`),
    # so the saved value must be SPLIT back into candidates — passing the whole `a>b>c`
    # string as one explicit seed would make `pick_moderators` try an invalid single
    # backend id before falling back. Take the FIRST (highest-priority) saved candidate as
    # the explicit seed; pick_moderators rebuilds the rest of the priority order.
    saved_mod = sess.moderator.split(">")[0].strip() if sess.moderator else ""
    mod_seed = ns.moderator or (saved_mod or None)
    if mod_seed:
        mod_seed = _expand_alias(mod_seed)
    moderators = pick_moderators(mod_seed, models)
    timeout = ns.timeout if ns.timeout is not None else PANEL_TIMEOUT_DEFAULT

    # Optional grounding diff for the resumed rounds: --diff / --staged re-attach the
    # current diff (the original grounding is not persisted in the log). Degrades to
    # ungrounded on a non-repo / git failure, exactly like the brainstorm mode.
    diff = ""
    if getattr(ns, "diff", False) or getattr(ns, "staged", False):
        try:
            diff = _git_diff(cwd, ns.staged)
        except RuntimeError:
            diff = ""

    print(
        f"[review sessions] resuming '{sess.session_id}' ({sess.status}, "
        f"{sess.completed_rounds} round(s) done): {sess.topic}",
        file=sys.stderr,
        flush=True,
    )

    # Panel modes announce their live-log paths (the resumed rounds stream to the log).
    backends._ANNOUNCE_LOGS = True
    try:
        # A resumed brainstorm is a full panel run: it takes a host-wide slot like any
        # other brainstorm (review-cli#614).
        with admission.run_slot(
            mode="brainstorm",
            models=admission.routed(brainstorm_pool(models)),
            later_models=admission.routed(moderators[:1]),
        ):
            return _sessions.resume_session(
                sess,
                models=models,
                cwd=cwd,
                timeout=timeout,
                moderators=moderators,
                diff=diff,
                force=ns.force,
            )
    except _sessions.SessionAlreadyCompleteError as exc:
        # A refused resume (already-complete, no --force) did NO requested work. Return the
        # same non-zero code the unknown/ambiguous-id paths use so scripts and hooks can
        # tell a refusal from a real resume — exit 0 here was indistinguishable from success
        # (codex P2: CTO sided with the bot over the prior "intentional" exit-0 choice).
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2


def _task_sessions(task_code: str):
    """Dashboard sessions for a task, oldest first. Best-effort over existing logs."""
    from .dashboard import parser as dparser
    from .process import log_dir

    sessions = [s for s in dparser.load_sessions(log_dir()) if s.task_code == task_code]
    return sorted(sessions, key=lambda s: s.started)


_TASK_SESSION_MATCH_WINDOW_SECONDS = 10 * 60


def _parse_task_record_started(record: dict) -> datetime | None:
    dt = parse_iso_ts(record.get("ts"))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _session_started_utc(session) -> datetime:
    dt = session.started
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _task_record_iteration(record: dict) -> int | None:
    try:
        return int(record["iteration"])
    except (KeyError, TypeError, ValueError):
        return None


def _closest_iteration_for_session(session, iterations: list[dict]) -> int | None:
    session_started = _session_started_utc(session)
    best_iteration: int | None = None
    best_delta: float | None = None
    for record in iterations:
        started = _parse_task_record_started(record)
        iteration = _task_record_iteration(record)
        if started is None or iteration is None:
            continue
        delta = abs((session_started - started).total_seconds())
        if delta > _TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if best_delta is None or delta < best_delta:
            best_iteration = iteration
            best_delta = delta
    return best_iteration


def _session_for_task_iteration(
    record: dict,
    sessions: list,
    all_iterations: list[dict] | None = None,
    used_session_ids: set[str] | None = None,
):
    """Match a stats iteration to the session whose start time is closest to it."""
    started = _parse_task_record_started(record)
    iteration = _task_record_iteration(record)
    if started is None or iteration is None:
        return None
    best = None
    best_delta: float | None = None
    used = used_session_ids or set()
    for session in sessions:
        if session.session_id in used:
            continue
        delta = abs((_session_started_utc(session) - started).total_seconds())
        if delta > _TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if (
            all_iterations
            and _closest_iteration_for_session(session, all_iterations) != iteration
        ):
            continue
        if best_delta is None or delta < best_delta:
            best = session
            best_delta = delta
    return best


def _iteration_for_session(session, iterations: list[dict]) -> int | None:
    return _closest_iteration_for_session(session, iterations)


def _task_subcommand(rest: list[str]) -> int:
    """`review task CODE [--detail N|SESSION] [--json]` — task-scoped review history.

    The run-stats JSONL is the authoritative iteration list; dashboard logs provide
    transcript detail when they are still present. Both stores are private local files.
    """
    if "--task" in rest or any(arg.startswith("--task=") for arg in rest):
        print(
            "[review task] use positional CODE for history lookup: review task CODE. "
            "--task is for recorded review modes.",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(
        prog="review task",
        description="Show review iterations, models, and transcripts for one task code.",
    )
    parser.add_argument("code", nargs="?", help="task/issue code, e.g. HYP-742")
    parser.add_argument(
        "--detail",
        metavar="N|SESSION",
        help="print the full transcript for an iteration number or session id",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 iff the task has enough PASSED recorded iterations across enough "
        "distinct board roles (self-merge-authority gate, default) or distinct "
        "models (when --min-models is explicitly given); fail-closed on "
        "missing/unreadable history or pre-verdict-field records; see "
        "--min-iter/--min-models/--min-roles and `review help task-check` for "
        "the full pass/fail logic (incl. the role-tracking fallback) and the "
        "complete --json field-by-field contract",
    )
    parser.add_argument(
        "--min-iter",
        type=int,
        default=3,
        help="review-bar floor: PASSED recorded iterations (default 3)",
    )
    parser.add_argument(
        "--min-models",
        type=int,
        default=None,
        help="review-bar floor: distinct models among the PASSED iterations. Not "
        "enforced by default (review-cli#246: role-based coverage is the "
        "default instead — see --min-roles). When explicitly given, this floor "
        "is ALWAYS enforced — including alongside --min-roles, where BOTH "
        "floors must then be met (an explicit request is never silently "
        "dropped) — and, when there's real review history to check it against, "
        "a non-blocking advisory is printed noting that role-based coverage is "
        "usually sufficient on its own.",
    )
    parser.add_argument(
        "--min-roles",
        type=int,
        default=None,
        help="review-cli#221: review-bar floor: distinct BOARD ROLES (architect/"
        "correctness/security/…) covered among the PASSED iterations, instead of "
        "distinct model NAMES. This is the DEFAULT check (review-cli#246) when "
        "neither --min-models nor --min-roles is given, at the same numeric "
        "floor --min-models used to default to (3) — pass this flag explicitly "
        "only to override that number. A role filled by the board's "
        "shortage-resilience duplicate-model pad (PR #207: one model reused "
        "onto an otherwise-empty role when too few distinct models are "
        "available) counts once per role, same as any other role, even though "
        "it repeats a model --min-models already counted. Only iterations from "
        "a mode that records per-seat roles (currently: the review-diff/visual "
        "board dispatch) contribute.",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="--check only: repo to verify recorded iterations against "
        "(diff-identity binding — see reviewlib.stats module docstring)",
    )
    identity_flags = parser.add_mutually_exclusive_group()
    identity_flags.add_argument(
        "--no-verify-identity",
        action="store_true",
        help="--check only: skip repo/diff mismatch detection entirely (legacy "
        "behavior — every PASSED iteration counts, exactly as before this gate "
        "existed). Escape hatch for a cwd that can't resolve a repo; NOT a way "
        "around a genuine mismatch finding.",
    )
    identity_flags.add_argument(
        "--verify-repo-only",
        action="store_true",
        help="--check only: verify recorded iterations by repository alone. "
        "Iterations recorded in another repository (even under the '#'-twin "
        "code) are excluded; same-repo iterations count whatever files they "
        "touched. For a caller like `gh ship` that checks a PR from a checkout "
        "whose own diff is unrelated to it (review-cli#589).",
    )
    ns = parser.parse_args(rest)

    if ns.check:
        if not ns.code:
            print("[review task] --check requires a task CODE", file=sys.stderr)
            return 2
        # Both `--min-models` and `--min-roles` default to None (not a resolved int)
        # so the CLI can tell "the user explicitly typed this flag" apart from "left
        # at the default" — and now (review-cli#246) that raw None-or-value is passed
        # straight through to quorum_check(), which owns ALL default-resolution and
        # AND-logic itself (see its docstring): an explicitly given floor is always
        # enforced, and the "neither given" default switches to a role-based check.
        # A floor of 0 would trivially satisfy the bar for a task with ZERO passed
        # iterations (0 >= 0), even one whose every recorded run failed or predates
        # the verdict field -- defeating the fail-closed contract this gate exists
        # for. Only an EXPLICITLY given floor is validated here (mirrors
        # quorum_check's own validation) — the message only names a flag that was
        # actually part of the input, so a plain --min-iter 0 never prints a
        # confusing "--min-models None --min-roles None" (Opus round-2 review
        # finding, still honored under the new sentinel-based design).
        if (
            ns.min_iter < 1
            or (ns.min_models is not None and ns.min_models < 1)
            or (ns.min_roles is not None and ns.min_roles < 1)
        ):
            clauses = [f"--min-iter {ns.min_iter}"]
            if ns.min_models is not None:
                clauses.append(f"--min-models {ns.min_models}")
            if ns.min_roles is not None:
                clauses.append(f"--min-roles {ns.min_roles}")
            print(
                "[review task] --min-iter must be >= 1, and any explicitly given "
                "--min-models/--min-roles must also be >= 1 "
                f"(got {' '.join(clauses)})",
                file=sys.stderr,
            )
            return 2
        identity_mode: IdentityMode = "full"
        if ns.no_verify_identity:
            identity_mode = "disabled"
        elif ns.verify_repo_only:
            identity_mode = "repo_only"
        return _quorum_check_subcommand(
            ns.code,
            ns.min_iter,
            ns.min_models,
            min_roles=ns.min_roles,
            as_json=ns.json,
            cwd_raw=ns.cwd,
            identity_mode=identity_mode,
        )

    if not ns.code:
        summaries = task_summaries()
        if ns.json:
            import json as _json

            print(_json.dumps({"tasks": summaries}, indent=2))
            return 0
        if not summaries:
            print("No task-coded review iterations found.")
            return 0
        print("Review tasks:")
        for item in summaries:
            models = ", ".join(item["models"]) or "-"
            print(
                f"  {item['task_code']}: {item['iterations']} iteration"
                f"{'' if item['iterations'] == 1 else 's'} · models: {models}"
            )
        return 0

    try:
        code = normalize_task_code(ns.code)
    except ValueError as exc:
        print(f"[review task] invalid task code: {exc}", file=sys.stderr)
        return 2
    assert code is not None

    iterations = iterations_for_task(code)
    sessions = _task_sessions(code)

    if ns.detail:
        return _task_detail(code, ns.detail, iterations, sessions, as_json=ns.json)

    if ns.json:
        import json as _json

        payload = {
            "task_code": code,
            "iterations": iterations,
            "sessions": [s.to_summary() for s in sessions],
        }
        print(_json.dumps(payload, indent=2))
        return 0

    if not iterations and not sessions:
        print(f"No review iterations found for task {code}.")
        return 1

    count = len(iterations) if iterations else len(sessions)
    print(f"Task {code}: {count} iteration{'' if count == 1 else 's'}")
    if iterations:
        used_session_ids: set[str] = set()
        for item in iterations:
            idx = int(item["iteration"])
            session = _session_for_task_iteration(
                item, sessions, iterations, used_session_ids
            )
            if session is not None:
                used_session_ids.add(session.session_id)
            models = ", ".join(item.get("models") or []) or "-"
            duration = fmt_duration(float(item.get("duration_seconds") or 0))
            suffix = (
                f" · session {session.session_id}" if session else " · logs not found"
            )
            print(
                f"  iteration {idx}: {item.get('ts', '?')} · {item.get('mode', '?')} "
                f"· pool={item.get('pool_size', 0)} · models: {models} "
                f"· ok={item.get('ok_count', 0)} fail={item.get('fail_count', 0)} "
                f"· {duration}{suffix}"
            )
    else:
        for idx, session in enumerate(sessions, start=1):
            print(
                f"  iteration {idx}: {session.started.isoformat()} · {session.mode} "
                f"· models: {', '.join(session.models) or '-'} · session {session.session_id}"
            )
    print(f"\nDetail: review task {code} --detail <iteration|session_id>")
    return 0


def _jobs_subcommand(rest: list[str]) -> int:
    """`review jobs [--json] [--slots]` — host-wide admission slots, then every recorded
    `--detach` job, newest first.

    Status is the RECONCILED value (`jobs.job_status`): a job whose recorded state is
    still "running" but whose pid is gone is shown as "unknown-terminated" rather than
    a stale "running" that never resolves. The ADMISSION block (review-cli#614) lists
    every review on this host that is running or queued for a slot — detached or not —
    with the host budget and per-provider seat usage. `--slots` prints only that block
    (`--slots --json` as JSON); plain `--json` stays the job list it always was."""

    parser = argparse.ArgumentParser(
        prog="review jobs",
        description="Host-wide admission slots and detached (`--detach`) review jobs.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--slots",
        action="store_true",
        help="show only the host-wide admission slots (running/waiting reviews, budgets)",
    )
    ns = parser.parse_args(rest)

    if ns.slots:
        snap = admission.status_snapshot()
        if ns.json:
            import json as _json

            print(_json.dumps(snap, indent=2))
        else:
            print(admission.render_status(snap))
        return 0

    records = [jobs.job_status(rec["job_id"]) or rec for rec in jobs.list_jobs()]
    records.sort(key=lambda r: r.get("started_at") or 0, reverse=True)

    if ns.json:
        import json as _json

        print(_json.dumps(records, indent=2))
        return 0

    print(admission.render_status(admission.status_snapshot()))
    print()
    if not records:
        print("No detached jobs recorded.")
        return 0

    print("DETACHED JOBS")
    for rec in records:
        started = rec.get("started_at")
        when = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
            if started
            else "?"
        )
        queued = (
            f" admission=waiting#{rec.get('queue_position')}"
            if rec.get("status") == "running" and rec.get("admission") == "waiting"
            else ""
        )
        print(
            f"  {rec.get('job_id')}  {rec.get('status', '?'):<18} "
            f"mode={rec.get('mode', '?')} pid={rec.get('pid', '?')} started={when}{queued}"
        )
    return 0


def _job_exit_code(rec: dict) -> int:
    """The exit code `status`/`wait` report for a job record: 0 while still running,
    the job's OWN recorded `exit_code` once it finished ("done" or "failed" — NOT
    collapsed to a bare 0/1, so a caller scripting against a specific code (2, 10, 124,
    …) sees the real value), or 1 for "unknown-terminated" (the process died before it
    could record one — no exit code exists to report)."""
    status = rec.get("status")
    if status == "running":
        return 0
    if status in ("done", "failed"):
        code = rec.get("exit_code")
        return code if isinstance(code, int) else 1
    return 1  # unknown-terminated, or any unrecognized status


def _seats_subcommand(verb: str, rest: list[str]) -> int:
    """`review seats|doctor [--json]` -- the bare `review status` seat report."""
    parser = argparse.ArgumentParser(
        prog=f"review {verb}",
        description=(
            "Show which board seats can run right now and why the others cannot, each "
            "Claude CLI account's login state, the omp launcher, each omp Anthropic "
            "account's availability and usage, and every harness login on this machine "
            "(Claude Code, Codex, omp, opencode, review-cli's own API keys; no secret is "
            "shown). Read-only, no model is called. Same report as a bare `review status`."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    return _seat_status_report(as_json=ns.json)


def _status_subcommand(rest: list[str]) -> int:
    """`review status <job-id> [--json] [--tail N]` — inspect one detached job.

    Exit code mirrors the job (`_job_exit_code`): 0 while running, the job's OWN
    recorded exit code once it finished, 1 for "unknown-terminated", 2 for an unknown
    job-id (a usage error, like any other bad argument).

    Bare `review status [--json]` (no job id) is the seat-health view instead
    (`_seat_status_report`, review-cli#594): which board seats can run right now and why not."""

    parser = argparse.ArgumentParser(
        prog="review status",
        description=(
            "Show one detached (`--detach`) review job's status; with no job id, show "
            "which board seats can run right now, why the others cannot, and what "
            "each would be substituted with (read-only, no model is called)."
        ),
    )
    parser.add_argument(
        "job_id",
        nargs="?",
        help="job id printed by `--detach` / `review jobs` (omit for seat health)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--tail",
        type=int,
        default=20,
        metavar="N",
        help="lines of log to show (default 20)",
    )
    ns = parser.parse_args(rest)
    if ns.job_id is None:
        return _seat_status_report(as_json=ns.json)

    rec = jobs.job_status(ns.job_id)
    if rec is None:
        print(f"[review status] no such job: {ns.job_id}", file=sys.stderr)
        return 2

    if ns.json:
        import json as _json

        print(_json.dumps(rec, indent=2))
        return _job_exit_code(rec)

    print(f"job {rec['job_id']}: {rec.get('status', '?')}")
    print(f"  mode:   {rec.get('mode', '?')}")
    print(f"  pid:    {rec.get('pid', '?')}")
    print(f"  log:    {rec.get('log_path', '?')}")
    print(f"  result: {rec.get('result_path', '?')}")
    if rec.get("exit_code") is not None:
        print(f"  exit:   {rec['exit_code']}")
    log_path = rec.get("log_path")
    if ns.tail > 0 and log_path:
        lines = jobs.tail_lines(Path(log_path), ns.tail)
        if lines:
            print(f"\n--- last {len(lines)} log line(s) ---")
            for line in lines:
                print(f"  {line}")
    return _job_exit_code(rec)


def _wait_subcommand(rest: list[str]) -> int:
    """`review wait <job-id> [--timeout SECS] [--poll SECS]` — block until a detached
    job finishes (or the wait itself times out).

    For a human terminal or an unbounded background caller — NOT for a subagent with
    its own short foreground cap (that caller should poll `review status` instead;
    blocking here defeats the entire point of `--detach`). Default timeout mirrors the
    internal run backstop's 4h ceiling, since no detached review can legitimately run
    longer than that. Exit code mirrors the job's own recorded exit code
    (`_job_exit_code`) once it finishes — NOT collapsed to a bare 0/1 — so a caller
    scripting against a specific code (2, 10, 124, …) sees the real value; 1 for
    "unknown-terminated" or this wait's own timeout, 2 for an unknown job-id."""
    from .backstop import MAX_BACKSTOP_SECONDS

    parser = argparse.ArgumentParser(
        prog="review wait",
        description="Block until a detached (`--detach`) review job finishes.",
    )
    parser.add_argument("job_id", help="job id printed by `--detach` / `review jobs`")
    parser.add_argument(
        "--timeout",
        type=int,
        default=MAX_BACKSTOP_SECONDS,
        metavar="SECS",
        help=f"give up waiting after this many seconds (default {MAX_BACKSTOP_SECONDS})",
    )
    parser.add_argument(
        "--poll", type=float, default=2.0, metavar="SECS", help="poll interval"
    )
    ns = parser.parse_args(rest)

    deadline = time.monotonic() + max(1, ns.timeout)
    while True:
        rec = jobs.job_status(ns.job_id)
        if rec is None:
            print(f"[review wait] no such job: {ns.job_id}", file=sys.stderr)
            return 2
        if rec.get("status") in jobs.TERMINAL_STATUSES:
            print(f"job {rec['job_id']}: {rec['status']}")
            return _job_exit_code(rec)
        if time.monotonic() >= deadline:
            print(
                f"[review wait] timed out after {ns.timeout}s waiting for {ns.job_id} "
                f"(still {rec.get('status', '?')})",
                file=sys.stderr,
            )
            return 1
        time.sleep(max(0.1, ns.poll))


def _resolve_stat_since(since_arg: str | None, days: int) -> datetime | None | bool:
    """Resolve `--since`/`--days` into a UTC datetime floor (None = all history).
    Returns `False` as a sentinel for an unparseable `--since` — the caller reports it
    as a usage error rather than silently falling back to "all history".

    Opus/kimi review finding: `datetime.fromisoformat` only accepts a trailing `Z`
    (Zulu/UTC) shorthand from Python 3.11 onward, but this project declares
    `requires-python = ">=3.9"` — and every call-log filename this command's own output
    is built from uses exactly that `...Z` stamp format. A user copying a timestamp
    straight out of `review stat`'s own report (or a filename) got a real usage error
    on 3.9/3.10 while the identical value worked on 3.11+. Normalize the shorthand
    ourselves before parsing so the accepted syntax doesn't depend on the interpreter."""
    if since_arg:
        parsed = parse_iso_ts(since_arg)
        if parsed is None:
            return False
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


# glm review finding, round 2: `--harness` filtered on an EXACT match against
# `report["harnesses"]`'s keys, which are the raw `CallLog.backend` string every OTHER
# call-log writer uses (`opencode`, `z.ai`, `commandcode`, ...) — not the short aliases
# `-m`/config actually teach (`-m glm`, `-m cc`, board ids `zai:...`/`oc:...`). So
# `--harness glm`/`zai`/`oc`/`cc` all printed "no calls recorded" while the data sat in
# the report under a different spelling. Sourced ONLY from aliases genuinely resolved
# elsewhere in this codebase — `resolve_backend`'s own alt-spelling matching
# (`zai`/`zhipu`/`glm` -> the z.ai backend, `oc` -> opencode) and `config.MODEL_ALIASES`
# (the `glm*` family, `cc`/`commoncode` -> commandcode) — NOT invented here. `cmd` is
# deliberately absent: despite a stale comment elsewhere once claiming it as a
# commandcode alias, it resolves NOWHERE in `_match_named_backend` or `MODEL_ALIASES` —
# it is not a real alias, so it is not silently accepted here either.
_HARNESS_ARG_ALIASES = {
    "zai": "z.ai",
    "zhipu": "z.ai",
    "glm": "z.ai",
    "glm53": "z.ai",
    "glm52": "z.ai",
    "glm51": "z.ai",
    "glm47": "z.ai",
    "glm46": "z.ai",
    "glm45": "z.ai",
    "oc": "opencode",
    "cc": "commandcode",
    "commoncode": "commandcode",
    "command-code": "commandcode",
    "command_code": "commandcode",
    "common-code": "commandcode",
    "common_code": "commandcode",
}


def _normalize_harness_arg(raw: str) -> str:
    """The `--harness` value, normalized to the exact `CallLog.backend` spelling the
    report's `harnesses` dict is keyed by — see `_HARNESS_ARG_ALIASES` above.

    Opus review finding, round 4: the fallback for a token NOT in the alias dict used
    to return `raw` completely UNCHANGED — but every real backend key is lowercase with
    no surrounding whitespace, so `--harness Codex` (different casing) or `--harness
    "codex "` (trailing space) fell through as literally `"Codex"`/`"codex "`, which
    then never equals the report's `"codex"` key — a false "no calls recorded" for data
    that genuinely exists. The fallback now returns the SAME normalized (stripped,
    lowercased) key the alias lookup itself used, so an exact name matches regardless
    of input casing/whitespace, while a genuinely unrecognized token still produces the
    honest "no calls recorded for harness ..." message, not a silent misroute."""
    key = raw.strip().lower()
    return _HARNESS_ARG_ALIASES.get(key, key)


def _stat_subcommand(rest: list[str]) -> int:
    """`review stat [--days N] [--since ISO] [--top N] [--harness NAME] [--json]` —
    detailed per-harness/per-model usage + health report parsed from the real per-call
    logs (see `reviewlib.dashboard.tokenstats` for the data model and the 2026-08
    token-burn investigation this answers). Default window is the last 7 days — a full
    scan of a long-lived install's log dir (tens of thousands of files) is slow, and
    most token-burn questions are about recent behaviour; `--days 0` scans everything."""
    parser = argparse.ArgumentParser(
        prog="review stat",
        description="Per-harness/per-model usage + health report from the real call logs.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="only calls from the last N days (default 7; <= 0 = all recorded history)",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        default=None,
        help="only calls at/after this ISO-8601 timestamp (overrides --days)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="how many largest calls to list (default 10; clamped to >= 1)",
    )
    parser.add_argument(
        "--harness",
        default=None,
        help="narrow the per-harness BREAKDOWN TABLE to this backend (e.g. codex, "
        "opencode, commandcode, omp, claude, z.ai — also accepts common aliases: "
        "glm/zai/oc/cc) — every other DATA section (models, Fable, retry events, "
        "call count, top oversized calls) stays whole-window in --json; the text "
        "report's Fable/retry/oversized sections show it too, but per-model detail is "
        "--json-only (see `review stat --json`)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    # A non-positive --top has no sensible "top N" meaning, and a negative value would
    # silently slice as "all but the last |N|" (sorted(...)[:-3]) — a confusing result
    # under the "Top N oversized calls" heading (kimi review finding). Clamp instead of
    # erroring: a typo'd --top still gets a useful (if minimal) report.
    if ns.top < 1:
        ns.top = 1

    since = _resolve_stat_since(ns.since, ns.days)
    if since is False:
        print(
            f"[review stat] invalid --since value: {ns.since!r} "
            "(expected ISO-8601, e.g. 2026-08-01T00:00:00+00:00)",
            file=sys.stderr,
        )
        return 2

    from .dashboard.tokenstats import compute_stat_report

    report = compute_stat_report(since=since, top=ns.top)
    # glm review finding, round 2: normalize BEFORE filtering — see
    # `_normalize_harness_arg` for why a raw alias (`glm`, `zai`, `oc`, `cc`) used to
    # always miss even though the report's own footnote teaches it. Displaying the
    # normalized name (not the raw one the user typed) in the empty-result message is
    # deliberate: it shows exactly what was actually searched for, so a mistaken alias
    # mapping is visible, not hidden.
    harness = _normalize_harness_arg(ns.harness) if ns.harness else None
    if harness:
        # Table-only filter (codex review finding): this narrows ONLY the per-harness
        # breakdown row set, not the whole report — models/fable/retry-events/
        # call_count/top_oversized_calls are cross-harness context that stays
        # whole-window on purpose (e.g. seeing where codex's calls rank among the
        # largest overall). See the --harness help text above.
        report["harnesses"] = {
            name: hs for name, hs in report["harnesses"].items() if name == harness
        }
        # Opus review finding, round 3: this stderr note used to print UNCONDITIONALLY,
        # so the text-report path showed the "no calls recorded for harness ..." message
        # TWICE — once here, once again via `_render_stat_harness_table`'s own empty-
        # table message in the printed report body. Harmless but redundant. The `--json`
        # path has no equivalent in-payload message (an empty `harnesses` dict alone
        # doesn't explain WHY), so it's the only path that still needs this stderr note.
        if not report["harnesses"] and ns.json:
            print(
                f"[review stat] no calls recorded for harness {harness!r} in this window.",
                file=sys.stderr,
            )

    if ns.json:
        import json as _json

        print(_json.dumps(report, indent=2))
        return 0
    print(_render_stat_report_text(report, requested_harness=harness))
    return 0


def _capacity_check_subcommand(rest: list[str]) -> int:
    """`review capacity-check [--preset NAME] [--json]` — whether every route and account
    the active review board could dispatch on is confirmed out of quota right now
    (review-cli#472; the evidence predicate is in `reviewlib.capacity_check`'s docstring
    and `review help capacity`). Built for `gh ship`'s review-quorum/external-review
    gates (agent-tools ci/ship/ship.sh), which skip the manual hatch prompt only on
    `all_exhausted: true`. A bare MANAGEMENT subcommand (like stat/task/entities), NOT a
    fan-out mode.

    Always exits 0 in --json mode (the caller gates on `all_exhausted`/`error`, not the
    exit code); the text form exits 1 only when the board could not be resolved."""
    parser = argparse.ArgumentParser(
        prog="review capacity-check",
        description=(
            "Report whether every route/account the active review board could use is "
            "confirmed out of quota. See `review help capacity`."
        ),
    )
    parser.add_argument(
        "--preset",
        default=None,
        help="evaluate this preset's board instead of the config/default board "
        "(the board `review diff --preset NAME` would run)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)

    from .capacity_check import board_capacity_report, render_text

    report = board_capacity_report(preset=ns.preset)
    if ns.json:
        import json as _json

        print(_json.dumps(report, indent=2))
        return 0
    if report.get("error"):
        print(render_text(report), end="", file=sys.stderr)
        return 1
    print(render_text(report), end="")
    return 0


def _render_stat_report_text(
    report: dict, *, requested_harness: str | None = None
) -> str:
    """Assemble the full human-readable `review stat` report from its sections.
    `requested_harness` (the raw `--harness` value, if any) only affects the empty-table
    message — see `_render_stat_harness_table`."""
    sections = [
        _render_stat_header(report),
        _render_stat_harness_table(
            report["harnesses"], requested_harness=requested_harness
        ),
        _render_stat_fable_section(report["fable"]),
        _render_stat_retry_section(report["retry_events_by_kind"]),
        _render_stat_oversized_section(report["top_oversized_calls"]),
        "Note: real token counts exist ONLY for REST-backed calls "
        f"({', '.join(report['tokens_recorded_backends'])}); the agentic CLI harnesses "
        "(oc/opencode, omp, codex, claude in CLI mode) show tokens: not captured (see "
        "below) — bytes are the best available cross-harness proxy today. Each of these "
        "CLIs DOES expose exact usage/cost via its own --json/--format json mode "
        "(verified live); review-cli doesn't invoke them that way yet because it would "
        "replace their readable stdout wholesale, breaking the paywall/auth detection "
        "this report itself relies on — tracked separately, see review-cli"
        "#186. `cc` is not a separate harness — it resolves to `commandcode`, same as "
        "`--harness cc` (see `--harness`'s help text for the full alias list).",
    ]
    return "\n\n".join(sections)


def _render_stat_header(report: dict) -> str:
    window = f"since {report['since']}" if report["since"] else "all recorded history"
    return (
        f"review stat — {report['log_dir']} ({window})\n"
        f"calls: {report['call_count']}   retry/promotion events: {report['retry_event_count']}"
    )


def _render_stat_harness_table(
    harnesses: dict, *, requested_harness: str | None = None
) -> str:
    """One row per backend: call/health counts, byte-proxy distribution, real tokens
    (REST backends only), and the SKILL.md/MEMORY.md context-pollution rate.

    `requested_harness` distinguishes "genuinely nothing in this window" from "calls
    exist, just none for the requested --harness" (kimi review finding: the generic
    message was false/misleading in the latter case — this table can be legitimately
    empty here while `report['call_count']` in the header above is nonzero)."""
    if not harnesses:
        if requested_harness:
            return (
                f"No calls recorded for harness {requested_harness!r} in this window "
                "(other harnesses may still have activity — see the header above)."
            )
        return "No calls recorded in this window."
    from .dashboard.tokenstats import format_bytes

    header = (
        f"{'HARNESS':<14}{'CALLS':>7}{'OK':>6}{'FAIL':>6}{'RUN':>5}  "
        f"{'BYTES':>9}{'AVG':>9}{'P90':>9}{'MAX':>9}  {'TOK(real)':>10}  SKILL.md  MEMORY.md"
    )
    rows = [header, "-" * len(header)]
    for name, hs in harnesses.items():
        tok = (
            f"{hs['tokens_prompt']}/{hs['tokens_output']}" if hs["tokens_real"] else "-"
        )
        calls = hs["calls"] or 1
        skill_pct = f"{100 * hs['skill_md_calls'] / calls:.0f}%"
        mem_pct = f"{100 * hs['memory_md_calls'] / calls:.0f}%"
        rows.append(
            f"{name:<14}{hs['calls']:>7}{hs['ok']:>6}{hs['fail']:>6}{hs['running']:>5}  "
            f"{format_bytes(hs['bytes_total']):>9}{format_bytes(hs['bytes_avg']):>9}"
            f"{format_bytes(hs['bytes_p90']):>9}{format_bytes(hs['bytes_max']):>9}  "
            f"{tok:>10}  {skill_pct:>8}  {mem_pct:>9}"
        )
    title = (
        "Per-harness breakdown (bytes = call-log size, a token PROXY for every "
        "harness; TOK(real) = exact prompt/output tokens, REST backends only):"
    )
    return title + "\n" + "\n".join(rows)


def _render_stat_fable_section(fable: dict) -> str:
    """Surfaces the investigation's headline finding: the Fable seat's dispatch/failure
    rate and WHY it failed (session-limit vs paywall vs auth vs other).

    review-cli#fable-seat-reliability: the label dropped "priority-1" — that demotion
    (DEFAULT_BOARD priority 1 -> last-resort reserve) is the whole point of that
    change, so a report still claiming Fable sits at priority 1 would be actively
    wrong about its own subject."""
    if not fable["dispatch_attempts"] and not fable["retry_events"]:
        return "Fable (board seat): no dispatch attempts recorded in this window."
    rate = (
        f"{fable['failure_rate']:.0%}" if fable["failure_rate"] is not None else "n/a"
    )
    reasons = fable["retry_event_reasons"]
    return (
        "Fable (board seat) pattern:\n"
        f"  dispatch attempts: {fable['dispatch_attempts']}   "
        f"cached-skips: {fable['cached_skips']}   failure rate: {rate}\n"
        f"  retry/promotion events: {fable['retry_events']} "
        f"(session_limit={reasons['session_limit']} paywall={reasons['paywall']} "
        f"auth={reasons['auth']} other={reasons['other']})\n"
        "  note: dispatch_attempts/failure_rate are a LOWER BOUND — a claude CLI-mode "
        "call is only attributable to Fable when its body carries the paywall sentinel; "
        "a successful Fable dispatch or a session-limit-shaped failure is attributed to "
        "Opus instead (see reviewlib.dashboard.tokenstats.compute_fable_report)."
    )


def _render_stat_retry_section(by_kind: dict) -> str:
    if not by_kind:
        return "Retry/promotion events: none recorded in this window."
    parts = " ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items()))
    return f"Retry/promotion events by kind: {parts}"


def _render_stat_oversized_section(top: list[dict]) -> str:
    """The largest calls by log size — the investigation's own outlier-hunting method,
    now a standing report instead of a one-off manual pass."""
    if not top:
        return "Top oversized calls: none recorded in this window."
    from .dashboard.tokenstats import format_bytes

    lines = [f"Top {len(top)} oversized calls:"]
    for i, call in enumerate(top, start=1):
        task = call["task_code"] or "-"
        flags = []
        if call["diff_git_files"]:
            flags.append(f"diff_git_files={call['diff_git_files']}")
        if call["binary_stub_files"]:
            flags.append(f"binary_stub_files={call['binary_stub_files']}")
        if call["skill_md"]:
            flags.append("SKILL.md")
        if call["memory_md"]:
            flags.append("MEMORY.md")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  {i}. {call['backend']:<12} {format_bytes(call['size_bytes']):>9}  "
            f"task={task}{flag_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# review entities — read-only entity/relationship/stats overview (review-cli#487)
# ---------------------------------------------------------------------------

# Maps a resolved backend dispatch function's __name__ (see `backends.resolve_backend`)
# to the harness display name `review stat --harness`/the README already use. The single
# source of truth for "which harness does this model route through" is
# `backends._match_named_backend`; this table only LABELS its outputs for humans instead
# of re-deriving the prefix rules a second time (review-cli#487).
_ENTITY_HARNESS_LABELS = {
    "review_codex": "codex",
    "review_claude": "claude",
    "review_opencode": "opencode",
    "review_omp": "omp",
    "review_commandcode": "commandcode",
    "review_zai": "z.ai",
    "review_gemini": "gemini",
    "review_openrouter": "openrouter",
}


def _entity_harness_for_model(model: str) -> str:
    """The harness display name a board seat's `model` string actually dispatches
    through, via the real `backends.resolve_backend` router -- never a second guess at
    the prefix rules (see `_ENTITY_HARNESS_LABELS`)."""
    fn = backends.resolve_backend(model)
    return _ENTITY_HARNESS_LABELS.get(getattr(fn, "__name__", ""), "unknown")


def _configured_board(config: dict) -> list[BoardReviewer]:
    """The board `review diff` would use from `config` (its `models:` roster, else its
    `board:`/the default board), with the config's unpaid providers and provider chains
    applied so availability and failover read the same as a real run. Raises
    BoardConfigError for a malformed `board:`."""
    backends.configure_unpaid_providers(config.get("unpaid_providers"))
    backends.configure_omp_bin(config.get("omp_bin"))
    backends.configure_omp_accounts(config.get("omp_accounts"))
    from .provider_failover import configure_provider_chains

    configure_provider_chains(config.get("provider_chains"))
    config_models = _split_models(config.get("models") or [])
    return (
        board_from_models(config_models, config)
        if config_models
        else load_board(config)
    )


def _omp_usage_text(windows) -> str:
    """`5 Hour 10% · 7 Day 42%` -- a window at 100% also says when it resets."""
    from .omp_accounts import utc_stamp

    parts = []
    for w in windows:
        text = f"{w.label.removeprefix('Claude ')} {round(w.used_fraction * 100)}%"
        if w.used_fraction >= 1 and w.resets_at_ms is not None:
            text += f" (resets {utc_stamp(w.resets_at_ms)})"
        parts.append(text)
    return " · ".join(parts)


def _omp_pool_json(state) -> dict:
    launcher = state.launcher
    policy = state.policy
    return {
        "launcher": launcher.argv0 if launcher else None,
        "launcher_source": launcher.source if launcher else None,
        "real_bin": launcher.real_bin if launcher else None,
        "launcher_problem": state.launcher_problem,
        "auth_db": str(state.db),
        "account_policy": {
            "exclude": list(policy.exclude),
            "exclude_source": policy.exclude_source or None,
            "max_usage_percent": policy.max_usage_percent,
            "max_usage_source": policy.max_usage_source or None,
            "accounts": {
                email: {"max_usage_percent": percent}
                for email, percent in policy.per_account
            },
            "problems": list(policy.problems),
        },
        "anthropic_accounts": None
        if state.accounts is None
        else [
            {
                "position": s.account.position,
                "credential_id": s.account.credential_id,
                "email": s.account.email,
                "available": s.is_available(time.time()),
                "availability": s.availability(time.time()),
                "skip_reason": s.skip_reason,
                "tier_over_limit": list(s.tier_over_limit),
                "max_usage_percent": policy.threshold_for(s.account.email),
                "omp_blocks": [
                    {"scope": scope, "until_ms": until}
                    for scope, until in s.account.blocks
                ],
                "limited_until": s.limited_until,
                "limited_reason": s.limited_reason,
                "usage": None
                if s.usage is None
                else [
                    {
                        "label": w.label,
                        "used_fraction": w.used_fraction,
                        "resets_at_ms": w.resets_at_ms,
                        "tier": w.tier,
                    }
                    for w in s.usage
                ],
            }
            for s in state.accounts
        ],
    }


def _omp_pool_lines(state) -> list[str]:
    """The `review status` section for omp:anthropic seats (review-cli#636/#637)."""
    lines = [
        "\nOMP ANTHROPIC ACCOUNTS (omp:anthropic/* seats try these one per attempt, "
        "available first; next account on a rate limit or failed login; EXCLUDED and "
        "OVER LIMIT accounts are never tried)"
    ]
    launcher = state.launcher
    if launcher is None:
        lines.append(
            f"  launcher: none, omp: seats cannot start -- {state.launcher_problem}"
        )
    elif launcher.real_bin:
        lines.append(
            f"  launcher: {launcher.argv0} ({launcher.source}; wrapper, real binary "
            f"{launcher.real_bin} passed to seats as OMP_REAL_BIN)"
        )
    else:
        lines.append(f"  launcher: {launcher.argv0} ({launcher.source})")
    policy = state.policy
    lines.append(f"  account policy: {policy.summary()}")
    for problem in policy.problems:
        lines.append(
            f"  INVALID SETTING: {problem} -- omp:anthropic seats use no account "
            "until it is fixed"
        )
    if not state.accounts:
        where = (
            f"no readable omp auth db at {state.db}"
            if state.accounts is None
            else f"none in {state.db}"
        )
        then = (
            "omp:anthropic seats fail: the account policy cannot be applied"
            if policy.restricts
            else "omp picks the account"
        )
        lines.append(f"  {where} -- {then}")
        return lines
    now = time.time()
    email_w = max(len(s.account.label) for s in state.accounts)
    for s in state.accounts:
        usage = (
            "usage unknown (omp usage did not answer)"
            if s.usage is None
            else _omp_usage_text(s.usage) or "no usage windows reported"
        )
        lines.append(
            f"  {s.account.position}  {s.account.label.ljust(email_w)}  "
            f"credential {s.account.credential_id:<3} {s.availability(now)}  [{usage}]"
        )
    return lines


def _seat_status_report(*, as_json: bool) -> int:
    """Bare `review status` (review-cli#594): every board seat as usable or UNUSABLE with
    the reason (unpaid provider, missing CLI/login, an active cooldown, every Claude CLI
    account logged out or failed its last login) and the executor its failover would hand
    the seat to, plus each registered Claude CLI account's login state -- including a
    login failure a call recorded (review-cli#596) -- with the command that logs it back
    in, and the omp launcher plus each omp Anthropic account's availability and usage
    (review-cli#636/#637). `review seats` and `review doctor` print the same report.
    Read-only: no model is called and no login is started."""
    from .seat_health import claude_accounts_state, omp_pool_state, seat_health

    try:
        board = _configured_board(load_config())
    except BoardConfigError as exc:
        print(f"[review status] {exc}", file=sys.stderr, flush=True)
        return 2
    accounts = claude_accounts_state()
    from . import harness_accounts

    logins = harness_accounts.discover()
    omp_pool = omp_pool_state()
    seats = [(r, seat_health(r.model, accounts, omp_pool=omp_pool)) for r in board]

    if as_json:
        import json as _json

        payload = {
            "seats": [
                {
                    "seat": r.model,
                    "display": r.display,
                    "role": r.role,
                    "usable": h.reason is None,
                    "reason": h.reason,
                    "substitute": h.substitute,
                }
                for r, h in seats
            ],
            "claude_accounts": [
                {
                    "name": a.name,
                    "email": a.email,
                    "current": a.current,
                    "logged_in": a.logged_in,
                    "login_failure": a.login_failure,
                    "login_failed_at": a.login_failed_at_utc,
                    "login_command": a.login_command,
                }
                for a in accounts
            ],
            "omp": _omp_pool_json(omp_pool),
            "harness_accounts": harness_accounts.as_json(logins),
            "admission": admission.status_snapshot(),
        }
        print(_json.dumps(payload, indent=2))
        return 0

    lines = [
        "review status -- which board seats can run right now "
        "(read-only: no model is called)\n",
        "SEATS (board order)",
    ]
    name_w = max((len(r.display) for r, _h in seats), default=0)
    model_w = max((len(r.model) for r, _h in seats), default=0)
    for r, h in seats:
        head = f"  {r.display.ljust(name_w)}  {(r.role or 'general'):<12} {r.model.ljust(model_w)}"
        if h.reason is None:
            lines.append(f"{head}  usable")
            continue
        then = (
            f"answers via {h.substitute}" if h.substitute else "no substitute executor"
        )
        lines.append(f"{head}  UNUSABLE: {h.reason} -> {then}")

    lines.append("\nCLAUDE CLI ACCOUNTS (the claude:* seats rotate through these)")
    if not accounts:
        lines.append(
            "  none registered -- single implicit account (see HARNESS ACCOUNTS)"
        )
    for a in accounts:
        marker = "*" if a.current else " "
        if a.logged_in is False:
            state = f"LOGGED OUT -- log it back in: {a.login_command}"
        elif a.login_failure:
            state = (
                f"LOGIN FAILED {a.login_failed_at_utc} ({a.login_failure}) "
                f"-- log it back in: {a.login_command}"
            )
        elif a.logged_in:
            state = "logged in"
        else:
            state = (
                "login state unknown (no `claude` CLI answered `claude auth status`)"
            )
        lines.append(
            f"  {marker} {a.name:<12} {(a.email or '(no email on disk)'):<28} {state}"
        )
    lines.extend(_omp_pool_lines(omp_pool))
    lines.extend(harness_accounts.render(logins))
    lines.append("")
    lines.append(admission.render_status(admission.status_snapshot()))
    print("\n".join(lines))
    return 0


def _entities_subcommand(rest: list[str]) -> int:
    """`review entities [--json]` -- read-only structural overview of review-cli's own
    configured entities (harnesses, providers/vendors, models, accounts, effort/role
    lenses) and how they relate, plus a compact per-harness call-health line from the
    real call logs (last 7 days).

    This is the Phase-0 list-view slice of the fuller interactive entity/relationship
    browser tracked in review-cli#487 -- it prints a structured report once and exits;
    it does not yet support drill-down or live refresh (later phases). Every field comes
    from data review-cli already owns (the resolved board, `role_models`,
    `claude_accounts`, and the real call-log stats `review stat` also reads) plus one
    local `claude auth status` per registered Claude CLI account (review-cli#594: seat
    availability is `seat_health`'s reason, the same one bare `review status` prints).
    Read-only -- no model is called, no key is printed."""
    parser = argparse.ArgumentParser(
        prog="review entities",
        description="Read-only overview of review-cli's harnesses/providers/models/accounts/efforts.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)

    try:
        board = _configured_board(load_config())
    except BoardConfigError as exc:
        print(f"[review entities] {exc}", file=sys.stderr, flush=True)
        return 2

    from .role_models import ROLE_FALLBACK_MODELS
    from .dashboard.tokenstats import compute_stat_report
    from .seat_health import claude_accounts_state, omp_pool_state, seat_health

    account_state = claude_accounts_state()
    # review-cli#641: an omp seat (or a failover target on omp) the omp account policy
    # leaves no account for is not available
    omp_pool = omp_pool_state()

    harnesses: dict[str, dict] = {}
    models: list[dict] = []
    for r in board:
        harness = _entity_harness_for_model(r.model)
        provider = backends.effective_provider(r.model)
        health = seat_health(r.model, account_state, omp_pool=omp_pool)
        entry = harnesses.setdefault(harness, {"providers": set(), "seats": 0})
        entry["providers"].add(provider)
        entry["seats"] += 1
        models.append(
            {
                "seat": r.model,
                "display": r.display,
                "harness": harness,
                "provider": provider,
                "role": r.role,
                "effort": r.effort,
                "available": health.reason is None,
                "reason": health.reason,
                "substitute": health.substitute,
            }
        )

    accounts = [
        {
            "name": a.name,
            "email": a.email,
            "current": a.current,
            "logged_in": a.logged_in,
            "login_failure": a.login_failure,
            "login_failed_at": a.login_failed_at_utc,
            "login_command": a.login_command,
        }
        for a in account_state
    ]

    since = datetime.now(timezone.utc) - timedelta(days=7)
    stat_report = compute_stat_report(since=since, top=1)

    payload = {
        "harnesses": {
            name: {"seats": v["seats"], "providers": sorted(v["providers"])}
            for name, v in sorted(harnesses.items())
        },
        "models": models,
        "accounts": accounts,
        "roles": {role: list(ids) for role, ids in ROLE_FALLBACK_MODELS.items()},
        "stats_since": stat_report["since"],
        "stats_harnesses": stat_report["harnesses"],
    }

    if ns.json:
        import json as _json

        print(_json.dumps(payload, indent=2))
        return 0

    print(_render_entities_report(payload))
    return 0


def _render_entities_report(payload: dict) -> str:
    """Human-readable `review entities` report -- see `_entities_subcommand`."""
    lines = ["review entities -- harnesses, providers, models, accounts, efforts\n"]

    lines.append("HARNESSES")
    for name, info in payload["harnesses"].items():
        providers = ", ".join(info["providers"]) or "-"
        lines.append(
            f"  {name:<12} {info['seats']:>2} seat(s)   providers: {providers}"
        )

    lines.append("\nMODELS (board order)")
    name_w = max((len(m["display"]) for m in payload["models"]), default=0)
    for m in payload["models"]:
        status = "available" if m["available"] else f"unavailable: {m['reason']}"
        if m.get("substitute"):
            status += f" -> answers via {m['substitute']}"
        role = m["role"] or "general"
        effort = m["effort"] or "-"
        lines.append(
            f"  {m['display'].ljust(name_w)}  {m['harness']:<11} {m['provider']:<12} "
            f"role={role:<12} effort={effort:<6} [{status}]"
        )

    lines.append("\nACCOUNTS (Claude multi-account registry)")
    if not payload["accounts"]:
        lines.append("  none registered -- single implicit account")
    else:
        for a in payload["accounts"]:
            marker = "*" if a["current"] else " "
            if a.get("logged_in") is False:
                state = f"  LOGGED OUT -- log it back in: {a['login_command']}"
            elif a.get("login_failure"):
                state = (
                    f"  LOGIN FAILED {a['login_failed_at']} ({a['login_failure']}) "
                    f"-- log it back in: {a['login_command']}"
                )
            else:
                state = ""
            lines.append(
                f"  {marker} {a['name']:<14} {a['email'] or '(no email on disk)'}{state}"
            )

    lines.append("\nEFFORT / ROLE LENSES (fallback chain per role)")
    for role, ids in payload["roles"].items():
        lines.append(f"  {role:<12} {' -> '.join(ids)}")

    since = payload["stats_since"] or "all recorded history"
    lines.append(f"\nCALL STATS (since {since})")
    if not payload["stats_harnesses"]:
        lines.append("  no calls recorded in this window")
    else:
        lines.append(_render_stat_harness_table(payload["stats_harnesses"]))

    lines.append(
        "\nThis is a read-only Phase-0 snapshot (`review entities --json` for the "
        "machine-readable form) -- the full interactive browser (drill-down, live "
        "refresh) is tracked in review-cli#487."
    )
    return "\n".join(lines)


def _resolve_quorum_check_context(
    cwd_raw: str, identity_mode: IdentityMode
) -> tuple[str | None, list[str] | None, str]:
    """Resolve the (repo_id, diff_files, status) check context for `--check`. See
    `_quorum_check_subcommand`.

    `identity_mode` is ``"full"`` (repo AND touched-file overlap, the default),
    ``"repo_only"`` (`--verify-repo-only`: repo alone, so `diff_files` is None), or
    ``"disabled"`` (`--no-verify-identity`).

    `status` is one of ``"ran"`` (repo_id/diff_files are usable), ``"repo_only"``
    (repo_id is usable, no file-set check), ``"disabled"``
    (`--no-verify-identity`), or ``"skipped_unresolvable"`` (`-C`/`cwd_raw` isn't
    even a real directory — `_compute_repo_id` otherwise always has a `path:`
    fallback). This is a MACHINE-READABLE version of the same distinction the
    stderr warnings below already make for a human — review finding (Fable) on
    this feature's own PR: the original cut only had the stderr text, so a
    machine caller like `gh ship` had no JSON field to assert "verification
    actually ran" against; it could only infer it from the ABSENCE of the
    diagnostic keys, which is indistinguishable from "verification ran and this
    task genuinely has zero passed iterations". `_quorum_check_subcommand` writes
    `status` into the result as `"identity_verification"`.
    """
    if identity_mode == "disabled":
        print(
            "[review task] warning: diff-identity verification disabled "
            "(--no-verify-identity) — every PASSED iteration counts regardless of "
            "recorded repo/diff, exactly as before this gate existed",
            file=sys.stderr,
        )
        return None, None, "disabled"
    cwd = _effective_cwd(cwd_raw, warn=False)
    repo_id = _compute_repo_id(cwd)
    if repo_id is None:
        print(
            f"[review task] warning: could not resolve a repo at -C {cwd_raw!r} — "
            "diff-identity verification skipped, falling back to legacy counting "
            "(every PASSED iteration counts regardless of recorded repo/diff)",
            file=sys.stderr,
        )
        return None, None, "skipped_unresolvable"
    if identity_mode == "repo_only":
        return repo_id, None, "repo_only"
    return repo_id, _current_diff_files_for_check(cwd), "ran"


def _print_quorum_model_audit_line(result: dict, stream=None) -> None:
    """Role mode's headline line reports covered ROLES, not models — print a
    secondary line naming which MODELS actually reviewed, so a self-merge-
    authority audit trail never loses that fact just because roles (not models)
    governed the verdict (Fable review finding, review-cli#221). `stream` matches
    the caller's stream choice (stdout for a plain ratio, stderr for an error)."""
    distinct_models = result["distinct_models_passed"]
    models_str = ", ".join(result["models"]) or "-"
    plural_m = "" if distinct_models == 1 else "s"
    print(
        f"  models: {distinct_models} distinct model{plural_m} ({models_str})",
        file=stream,
    )


def _print_quorum_bar_met(result: dict, role_mode: bool, model_mode: bool) -> None:
    """Text-mode 'review bar met' line. `role_mode`/`model_mode` (review-cli#221,
    extended in review-cli#246) select which floor(s) actually governed the
    verdict — the caller derives both from which keys `quorum_check` put in
    `result` (see `_quorum_check_subcommand`), since that reflects the function's
    OWN default-resolution/AND-logic, not the raw CLI flags. Either, or both, can
    be true (review-cli#246: an explicit --min-models alongside --min-roles now
    means BOTH govern)."""
    passed_iter = result["passed_iterations"]
    plural_i = "" if passed_iter == 1 else "s"
    parts = []
    if role_mode:
        distinct = result["distinct_roles_passed"]
        covered_str = ", ".join(result["roles"]) or "-"
        plural_u = "" if distinct == 1 else "s"
        parts.append(f"{distinct} distinct role{plural_u} ({covered_str})")
    if model_mode:
        distinct = result["distinct_models_passed"]
        covered_str = ", ".join(result["models"]) or "-"
        plural_u = "" if distinct == 1 else "s"
        parts.append(f"{distinct} distinct model{plural_u} ({covered_str})")
    print(
        f"review bar met for {result['task_code']}: {passed_iter} passed "
        f"iteration{plural_i} across {' and '.join(parts)}"
    )
    # Fable review finding: role mode's line above drops WHICH MODELS reviewed
    # entirely — for a self-merge-authority audit trail that's the fact worth
    # keeping in the log even when roles (not models) governed the verdict. Only
    # needed when models weren't ALREADY shown in the headline above.
    if role_mode and not model_mode:
        _print_quorum_model_audit_line(result)
    if advisory := result.get("min_models_advisory"):
        print(f"  note: {advisory}")
    # Alex's review-cli#246 follow-up: a bare `--check` that fell back to
    # model-counting because the task has zero role-tagged history looks
    # IDENTICAL to an explicit `--min-models` pass otherwise (same result
    # shape, same printed line above) -- this note is the one visible signal
    # that the bar-met floor came from the fallback, not from a real flag.
    if fallback := result.get("quorum_mode_fallback"):
        print(f"  note: {fallback}")


def _print_quorum_bar_not_met(result: dict, role_mode: bool, model_mode: bool) -> None:
    """Text-mode 'review bar NOT met' report: the ratio/error header, the
    `--min-roles` suggestion hint (review-cli#221, `--min-models`-only mode — see
    `quorum_check`'s `min_roles_suggestion`), and any currently-stalled attempted
    model(s). Everything is read from `result` (populated by `quorum_check`, whose
    own default-resolution/AND-logic decided which floor(s) actually govern — see
    `_print_quorum_bar_met`) rather than from separately-passed CLI values, so this
    never drifts from what actually gated the verdict.

    review-cli#221 round-4 review finding (k3/Fable): the header and every detail
    line below it must land on the SAME stream — a caller capturing only one of
    stdout/stderr must never see a bare header with no detail, or bare detail lines
    with no task-code/NOT-met context. An "error" result (invalid code, unreadable
    store, diff mismatch) reports to stderr; a plain ratio shortfall reports to
    stdout, matching the pre-existing convention."""
    detail_stream = sys.stderr if "error" in result else None
    if "error" in result:
        print(
            f"review bar NOT met for {result['task_code']}: {result['error']}",
            file=detail_stream,
        )
    else:
        passed_iter = result["passed_iterations"]
        clauses = [f"{passed_iter}/{result['min_iter']} passed iterations"]
        if role_mode:
            clauses.append(
                f"{result['distinct_roles_passed']}/{result['min_roles']} "
                "distinct roles"
            )
        if model_mode:
            clauses.append(
                f"{result['distinct_models_passed']}/{result['min_models']} "
                "distinct models"
            )
        print(
            f"review bar NOT met for {result['task_code']}: {', '.join(clauses)}",
            file=detail_stream,
        )
    # Codex round-5 review finding: this line must not be suppressed on every
    # "error" result — a diff-identity MISMATCH error still carries real
    # models/roles data (mismatch exclusion runs AFTER the counts are computed;
    # only `_rejected()`'s genuinely-empty cases — invalid code, unreadable
    # store, zero iterations — have nothing to show), so the audit trail must
    # not go dark there. Gate on there being real data instead of on "error"
    # presence: `distinct_models_passed > 0` is false for every `_rejected()`
    # shape and true whenever there's a meaningful roster to report. UNLIKE the
    # met-path printer, the NOT-met header's model clause above is COUNTS ONLY
    # (no names, matching the pre-#246 convention) even when `model_mode` is
    # True — so this line is the only place model NAMES ever appear here, and
    # must print whenever role_mode is on regardless of model_mode.
    if role_mode and result["distinct_models_passed"] > 0:
        _print_quorum_model_audit_line(result, detail_stream)
    if advisory := result.get("min_models_advisory"):
        print(f"  note: {advisory}", file=detail_stream)
    if fallback := result.get("quorum_mode_fallback"):
        # Alex's review-cli#246 follow-up: same rationale as the met-path note --
        # this is the one visible signal that the ratio above (`N/M distinct
        # models`) came from the zero-role-data fallback, not an explicit
        # `--min-models` flag.
        print(f"  note: {fallback}", file=detail_stream)
    if gap := result.get("role_tracking_gap"):
        # review-cli#252 follow-up: case (b) ("not genuinely old" -- see
        # quorum_check's docstring) never fires alongside quorum_mode_fallback
        # (they're mutually exclusive branches of the same fallback decision),
        # so this is never a duplicate of the note above -- it explains exactly
        # WHY the ordinary "N/M distinct roles" ratio above can't be satisfied by
        # the old model-counting escape hatch, and what to run instead.
        print(f"  hint: {gap}", file=detail_stream)
    if suggestion := result.get("min_roles_suggestion"):
        print(f"  hint: {suggestion}", file=detail_stream)
    # review-cli#221: a bare N/M count (or a bare mismatch-error line) leaves a human
    # no better off than before — name the SPECIFIC attempted model(s) currently
    # cooling down (an unavailable sentinel or a session-limit/usage-credits notice —
    # a plain timeout doesn't record a cooldown, see seat_cooldown.py's docstring) and
    # why, same signal --json already carries in `stalled_models`. Reached on EITHER
    # not-met path (round-3 review
    # finding: an earlier version of this diff returned early on the `error` branch,
    # before ever reaching this loop, even though `stalled_models` can genuinely be
    # populated alongside a mismatch error). Text-mode-only (the --json branch above
    # already returned this data structured) so a plain `review task X --check` run
    # directly by a developer sees it too, not just a `gh ship` caller parsing --json.
    for stalled in result.get("stalled_models", []):
        minutes = max(1, round(stalled["remaining_seconds"] / 60))
        times = stalled["consecutive_failures"]
        plural = "" if times == 1 else "s"
        print(
            f"  stalled: {stalled['model']} ({stalled['reason']}, "
            f"{times} consecutive failure{plural}, ~{minutes}m until retry-eligible)",
            file=detail_stream,
        )


def _quorum_check_subcommand(
    code: str,
    min_iter: int,
    min_models: int | None,
    *,
    min_roles: int | None = None,
    as_json: bool,
    cwd_raw: str = ".",
    identity_mode: IdentityMode = "full",
) -> int:
    """`review task CODE --check [--min-iter N] [--min-models M] [--min-roles R] [-C DIR]`.

    Exit 0 iff CODE has >= min_iter PASSED recorded iterations AND every EXPLICITLY
    given floor among {min_models, min_roles} is met (review-cli#246: an explicit
    floor is never silently outvoted by the other) — or, when NEITHER is given, >=
    the default distinct BOARD ROLES floor (review-cli#246's new default; see
    quorum_check's docstring for the full default-resolution/AND-logic table). Exit 1
    otherwise, including the fail-closed cases (invalid code, unreadable/missing
    stats store, zero records, or a task whose only history predates the verdict
    field) where reviewlib.stats.quorum_check sets an "error" key or simply comes up
    short. See quorum_check's docstring for the "passed, not just dispatched"
    semantics this check enforces.

    `identity_mode` ``"full"`` (the default) resolves `cwd_raw` (`-C`) to a repo id +
    the current diff's touched-file set and hands both to `quorum_check`, so a PASSED
    iteration recorded against a DIFFERENT repo, or a diff sharing no touched file,
    is EXCLUDED from the count rather than silently trusted — see reviewlib.stats
    "Diff-identity binding" for why (closes 3 real quorum-pollution incidents).
    ``"repo_only"`` (`--verify-repo-only`, review-cli#589) hands over the repo id
    alone: `gh ship` checks a PR from a checkout whose own diff has nothing to do
    with the PR, so only the repository can be compared there.
    ``"disabled"`` (`--no-verify-identity`) restores the pre-binding behavior (every
    passed iteration counts, no repo/diff cross-check) — an escape hatch for a cwd
    that can't resolve a repo, not a way around a genuine mismatch finding.
    """
    repo_id, diff_files, verification_status = _resolve_quorum_check_context(
        cwd_raw, identity_mode
    )
    result = quorum_check(
        code,
        min_iter=min_iter,
        min_models=min_models,
        min_roles=min_roles,
        repo_id=repo_id,
        diff_files=diff_files,
    )
    # Machine-readable counterpart to the stderr warnings below: a caller like
    # `gh ship` parsing --json alone (never sees stderr) can assert on this
    # directly instead of inferring "did verification run" from key absence.
    result["identity_verification"] = verification_status
    # This warning goes to stderr in BOTH --json and text mode: --json's structured
    # payload already carries excluded_mismatched_iterations/mismatch_details for a
    # machine reader, but a human watching stderr (e.g. `gh ship`'s own console
    # output) must see the mismatch surfaced too, not just a smaller-than-expected
    # number with no explanation.
    # With no diff compared (`--verify-repo-only`, or a full-mode check whose checkout
    # resolves no diff -- `quorum_check` compares the repository alone either way), the
    # exclusions are other repositories' records only, of any verdict (a failed one
    # still names a review that did not happen here) -- the same count and wording as
    # the JSON `error`. Pointing `gh ship` at --no-verify-identity would be wrong there.
    no_diff_compared = repo_id is not None and diff_files is None
    if no_diff_compared:
        excluded = result.get("excluded_foreign_repo_iterations", 0)
        why = "recorded in another repository (--json lists them in foreign_repo_iterations)"
    else:
        excluded = result.get("excluded_mismatched_iterations", 0)
        why = (
            "recorded repo/diff did not match the code currently being checked "
            "(--json for detail; --no-verify-identity disables this check)"
        )
    if excluded:
        print(
            f"[review task] warning: excluded {excluded} recorded iteration(s) for "
            f"{result['task_code']} — {why}",
            file=sys.stderr,
        )

    # review-cli#389: a failed/empty check whose search key has a same-digits
    # different-'#' twin already IN the store must say so on the refusal itself —
    # both modes (--json's payload already carries searched_key/near_miss_keys for
    # a machine reader, but a human running a plain `review task CODE --check`
    # never opens --json) so the mismatch is diagnosable from the refusal alone,
    # matching the excluded-mismatch warning's same both-modes convention above.
    #
    # Quorum-review round 2 (Grok, flipping a prior CLEAN to NOT CLEAN with a
    # concrete repro): the original cut branched on `excluded` (this task's
    # TOTAL mismatched-iteration count) to pick the wording, but a twin whose
    # ONLY record has `passed=False` never reaches `passed_iterations` at all
    # -- `excluded` stays 0 for it and the "already count above" branch fired
    # anyway, which is false. `near_miss_counted`/`near_miss_ever_passed`
    # (quorum_check, computed against the actual gated/passed populations) are
    # the real signal, so branch on those instead of `excluded`.
    if near_miss := result.get("near_miss_keys"):
        near_miss_list = ", ".join(repr(key) for key in near_miss)
        prefix = "review-cli#389 aliases these spellings when reading iterations, "
        if result.get("near_miss_counted"):
            detail = f"{prefix}so they already count above"
        elif result.get("near_miss_ever_passed"):
            # Passed at least once, but diff-identity verification excluded
            # every one of those as a repo/diff mismatch. `mismatch_details`
            # is --json-only (round-2 review finding: the original text
            # pointed a plain-text reader at a field they have no way to see)
            # -- point at the flag that actually gets them there instead.
            detail = (
                f"{prefix}but repository verification excluded the twin's passed "
                "iteration(s) as another repository's above -- re-run with --json "
                "to see foreign_repo_iterations for confirmation"
                if no_diff_compared
                else f"{prefix}but diff-identity verification excluded the twin's "
                "passed iteration(s) as a repo/diff mismatch above -- re-run "
                "with --json to see mismatch_details for confirmation"
            )
        else:
            detail = (
                f"{prefix}but none of the twin's recorded iteration(s) actually "
                "passed, so they do not count above"
            )
        print(
            f"[review task] note: searched key {result['searched_key']!r} — found "
            f"{near_miss_list} in the store (same digits, different '#'); {detail}",
            file=sys.stderr,
        )

    if as_json:
        import json as _json

        print(_json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    # review-cli#246: derived from the RESULT (which floor(s) quorum_check actually
    # applied, after its own default-resolution), not from the raw min_models/
    # min_roles arguments here — those can be None even when quorum_check ends up
    # enforcing a floor anyway (the "neither given" default substitutes min_roles).
    role_mode = "min_roles" in result
    model_mode = "min_models" in result
    if result["passed"]:
        _print_quorum_bar_met(result, role_mode, model_mode)
        return 0
    _print_quorum_bar_not_met(result, role_mode, model_mode)
    return 1


def _task_detail(
    code: str, selector: str, iterations: list[dict], sessions: list, *, as_json: bool
) -> int:
    session = None
    iteration_no: int | None = None
    if selector.isdigit():
        iteration_no = int(selector)
        if iteration_no <= 0:
            print("[review task] --detail iteration must be >= 1", file=sys.stderr)
            return 2
        if iterations:
            record = next(
                (item for item in iterations if item.get("iteration") == iteration_no),
                None,
            )
            if record is None:
                print(
                    f"No review iteration {iteration_no} found for task {code}.",
                    file=sys.stderr,
                )
                return 1
            session = _session_for_task_iteration(record, sessions, iterations)
        elif iteration_no - 1 < len(sessions):
            session = sessions[iteration_no - 1]
    else:
        session = next((s for s in sessions if s.session_id == selector), None)
        if session is not None:
            iteration_no = (
                _iteration_for_session(session, iterations)
                or sessions.index(session) + 1
            )
    if session is None:
        print(
            f"No conversation logs found for task {code} detail {selector}.",
            file=sys.stderr,
        )
        return 1
    detail = session.to_detail()
    if as_json:
        import json as _json

        print(_json.dumps(detail, indent=2))
        return 0

    label = f"iteration {iteration_no}" if iteration_no is not None else selector
    print(f"# Task {code} {label}")
    print(f"Session: {session.session_id}")
    print(f"Mode: {session.mode}")
    print(f"Started: {session.started.isoformat()}")
    print(f"Models: {', '.join(session.models) or '-'}")
    if session.brainstorm is not None:
        print("\n## Brainstorm transcript")
        print(session.brainstorm.body.rstrip())
    for idx, call in enumerate(session.calls, start=1):
        status = (
            "error" if call.has_error else ("running" if not call.completed else "ok")
        )
        print(
            f"\n## Call {idx}: {call.backend} r{call.round} [{status}] {call.filename}"
        )
        if call.body:
            print(call.body)
        if call.stderr_lines:
            print("\nstderr:")
            print("\n".join(call.stderr_lines))
    return 0


_SPECWEB_HELP = """review spec-web — multi-spec markdown reviewer (a persistent daemon)

DAEMON LIFECYCLE (shared agenttools_service lib, like `review dashboard`):
  review spec-web start --agent A [--host H] [--port N]   start the daemon in the background
  review spec-web status                        is it running? + registered specs + URLs
  review spec-web stop                          stop the daemon
  review spec-web run --agent A                 run the daemon in the foreground (this shell)
  review spec-web enable --agent A | disable    install / remove OS autostart

SPECS (spec-web-specific):
  review spec-web add <spec.md> [--agent A]     register a spec, print its name-based URL
  review spec-web serve <spec.md> --agent A     add + wait for the review (submit -> stdout)
  review spec-web <spec.md> --agent A            same as `serve` (backward-compatible)
  review spec-web list                          list registered specs
  review spec-web remove <name>                 unregister a spec
  review spec-web watch <name|path>             wait for a submit on an already-registered spec
  review spec-web reply <id> <answer> --spec P  the agent answers a reviewer's note

add/serve options: --seed FILE (import a review thread before serving), --open (open the
browser), --exit-on-submit (serve/watch return after the first submit), --no-watch (add:
register into the daemon and return without waiting).

--agent <name> is the tmux window/session that OWNS the served specs (e.g. --agent ext):
every submitted review batch is INJECTED into that live session (tg-ctl-style tmux
injection), so reviews reach the agent even when nothing is watching stdout. The
daemon-launching actions require it; a spec's own --agent (add/serve) overrides the
daemon default for that spec.

The daemon serves EVERY registered spec by name at /spec/<name> on ONE port (navigator at /),
so one port / Tailscale mapping covers all specs instead of a server per spec."""

# The lifecycle actions handled by the shared agenttools_service ServiceManager.
_SPECWEB_LIFECYCLE = frozenset(
    {"start", "status", "stop", "run", "enable", "disable", "__serve"}
)


def _spec_web(argv: list[str]) -> int:
    """Dispatch ``review spec-web`` — daemon lifecycle + spec registration + the reply/watch glue.

    A bare ``review spec-web`` prints HELP and launches nothing. The daemon lifecycle
    (start/status/stop/run/enable/disable) is delegated to the shared ``agenttools_service`` lib
    (see ``reviewlib.specweb.service`` — same pattern as ``review dashboard``); only the
    spec-web-unique surface (add/serve/list/remove/watch/reply and the legacy positional) lives
    here. See ``reviewlib.specweb`` for the full design.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(_SPECWEB_HELP)
        return 0
    sub = argv[0]
    if sub == "reply":
        return _spec_web_reply(argv[1:])
    if sub == "__serve":
        return _spec_web_serve_daemon(argv[1:])
    if sub in _SPECWEB_LIFECYCLE:
        return _spec_web_lifecycle(sub, argv[1:])
    if sub == "list":
        return _spec_web_list(argv[1:])
    if sub == "remove":
        return _spec_web_remove(argv[1:])
    if sub == "watch":
        return _spec_web_watch(argv[1:])
    if sub == "add":
        return _spec_web_add(argv[1:], watch=False)
    if sub == "serve":
        return _spec_web_add(argv[1:], watch=True)
    # A legacy positional `review spec-web <spec.md>`: register + serve-in-daemon + watch for the
    # submit (backward-compatible with the old blocking single-spec command).
    return _spec_web_add(argv, watch=True)


# ---- spec-web: shared agenttools_service message + host/port parsing ---------- #
_SPECWEB_MISSING_LIB = (
    "[review spec-web] the daemon lifecycle needs the shared 'agenttools_service' lib, which "
    "isn't installed.\n"
    "  why:  start/status/stop/run/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy — same as `review dashboard`.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon -e <agent-tools>/lib/agenttools_service\n"
)


def _spec_web_host_port(rest: list[str], *, defaults: bool = True):
    """Parse the optional ``--host``/``--port`` (+ ``--agent``) shared by the spec-web daemon
    commands.

    Returns ``(ns, remaining_positionals)``. With ``defaults=True`` the daemon defaults
    (0.0.0.0 / the stable port) are applied; the raw values (possibly None) are used otherwise."""
    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    parser = argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("--host", default=DEFAULT_SPECWEB_HOST if defaults else None)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_SPECWEB_PORT if defaults else None
    )
    parser.add_argument("--agent", default=None)
    return parser.parse_known_args(rest)


# The daemon-launching actions REQUIRE an owning agent (submitted reviews are DELIVERED into
# that agent's live session — a daemon nobody owns re-creates the "comments reach nobody" bug).
_SPECWEB_AGENT_REQUIRED = frozenset({"start", "run", "enable", "__serve"})


def _spec_web_require_agent(action: str, agent: str | None) -> int | None:
    """Exit-2 usage error when a daemon-LAUNCHING action lacks ``--agent``, else None.

    The agent names the tmux window/session (e.g. ``--agent ext``) that owns the served
    specs: every submitted review batch is injected into that session (see specweb.deliver),
    so an agentless daemon would silently strand reviews in the store again."""
    if agent:
        return None
    print(
        f"[review spec-web] `{action}` requires --agent <name> — the tmux window/session "
        f"that owns the served specs (submitted reviews are delivered INTO that session).\n"
        f"  use:  review spec-web {action} --agent <name>   (e.g. --agent ext)",
        file=sys.stderr,
    )
    return 2


def _spec_web_manager(host: str, port: int | None, agent: str | None = None):
    """Build the agenttools_service ServiceManager for the spec-web daemon, or None if the lib
    is absent (the caller prints the shared missing-lib guidance and returns exit 4)."""
    try:
        from agenttools_service import ServiceManager
    except ImportError:
        return None
    from .specweb.service import spec_web_service

    return ServiceManager(spec_web_service(host=host, port=port, agent=agent))


def _spec_web_serve_daemon(rest: list[str]) -> int:
    """Hidden entry: ``review spec-web __serve`` — the blocking multi-spec daemon (this shell).

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``);
    it calls ``run_daemon`` directly so it never re-enters the service dispatcher. Not advertised
    — operators use ``run`` (ad-hoc) or ``start`` (managed)."""
    ns, _ = _spec_web_host_port(rest)
    rc = _spec_web_require_agent("__serve", ns.agent)
    if rc is not None:
        return rc
    verbose = "--verbose" in rest
    from .specweb.server import run_daemon

    return run_daemon(host=ns.host, port=ns.port, verbose=verbose, agent=ns.agent)


def _spec_web_lifecycle(action: str, rest: list[str]) -> int:
    """Run a daemon lifecycle action (start/status/stop/run/enable/disable) via the shared lib.

    ``status``/``start`` additionally print the registered specs + their name-based URLs (the
    spec-web-specific bit), so the operator immediately sees what the daemon is serving.

    ``start`` is IDEMPOTENT: an already-running daemon is reported (pid + what it serves) and
    exits 0 — a repeated ``start`` in a setup script must not read as a failure. (The shared
    ``run_action`` alone would exit 3 there; that convention is for ``status``/``stop``'s
    "was anything up?" branch, not for a start that got what it asked for.)

    The daemon-LAUNCHING actions (start/run/enable) REQUIRE ``--agent`` — see
    ``_spec_web_require_agent``; status/stop/disable manage the existing instance and don't."""
    ns, _ = _spec_web_host_port(rest)
    if action in _SPECWEB_AGENT_REQUIRED:
        rc = _spec_web_require_agent(action, ns.agent)
        if rc is not None:
            return rc
    mgr = _spec_web_manager(ns.host, ns.port, ns.agent)
    if mgr is None:
        print(_SPECWEB_MISSING_LIB, file=sys.stderr)
        return 4
    if action == "start":
        st = mgr.status()
        if st.running:
            suffix = f" (pid {st.pid})" if st.pid is not None else ""
            print(f"[review spec-web] already running{suffix} — nothing to start.")
            _spec_web_print_registry(ns.host, ns.port)
            return 0
    from agenttools_service import run_action

    rc = run_action(mgr, action)
    if action in ("start", "status"):
        _spec_web_print_registry(ns.host, ns.port)
    return rc


def _clip_text(text: str, limit: int, *, collapse_whitespace: bool = False) -> str:
    """Truncate ``text`` to ``limit`` characters with a trailing ellipsis, stripping any
    trailing whitespace left by the cut. ``collapse_whitespace`` additionally joins the
    text onto one line first (used for the terminal reminder line, where an embedded
    newline would otherwise break the "one line per comment" contract; not used for the
    tg message body, which renders literal newlines fine)."""
    if collapse_whitespace:
        text = " ".join(text.split())
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_WAITING_ON = {"submitted": "an agent reply", "answered": "reviewer resolution"}


def _waiting_on_phrase(status: str | None) -> str:
    """What a stalled comment in ``status`` is waiting on: an agent reply for
    ``submitted``, reviewer resolution for ``answered``. Shared by `_stale_reminder_line`
    and `_stale_summary_for_tg` so the per-comment line and the tg summary can't drift
    into describing the same stall differently. `stale_comments()` only ever produces
    these two statuses (`OPEN_CYCLE_STATUSES`); an unexpected value falls back to
    "reviewer resolution" rather than raising, since this is a display-only formatter."""
    return _WAITING_ON.get(status, "reviewer resolution")


def _stale_reminder_line(stale: dict) -> str:
    """One indented reminder line for a comment `specweb.store.stale_comments` flagged as
    a stalled review-cycle: stuck in ``submitted`` (the agent hasn't replied) or
    ``answered`` (the agent replied, the reviewer hasn't resolved it) well past the
    reminder threshold. Shared verbatim (via `_print_stale_reminders`) by ``spec-web
    list``, ``spec-web start``/``status``, and the ``spec-web reply`` command's stderr
    nudge; the reply command's TG summary (`_stale_summary_for_tg`) uses different
    wording for a multi-comment count, but the same `_waiting_on_phrase`, so at least
    that half can't drift.

    The quote is whitespace-collapsed before clipping: it is the reviewer's verbatim
    selection, which can span a paragraph break, and a literal embedded newline would
    otherwise break this "one line per stalled comment" contract."""
    kind = stale.get("kind") or "remark"
    waiting_on = _waiting_on_phrase(stale.get("status"))
    age = stale.get("age_hours") or 0.0
    raw_quote = stale.get("quote") or stale.get("body") or ""
    clipped = _clip_text(str(raw_quote), 60, collapse_whitespace=True)
    return (
        f"    ⚠ stalled {kind} #{stale.get('id') or '?'} — {age:.1f}h waiting on "
        f'{waiting_on}: "{clipped}"'
    )


def _print_stale_reminders(stale: list[dict], *, stream: TextIO | None = None) -> None:
    """Print one `_stale_reminder_line` per already-filtered stalled comment (the output
    of `specweb.store.stale_comments` / `SpecStore.stale_comments`) — the single loop
    shared by all three surfaces (`spec-web list`, `spec-web start`/`status`, and the
    reply command's stderr nudge) so they can't drift out of sync with each other.
    ``stream=None`` prints to stdout (``print``'s own default); the reply command passes
    ``sys.stderr`` since its stdout is reserved for the "replied to ..." confirmation."""
    for s in stale:
        print(_stale_reminder_line(s), file=stream)


def _comments_for_reminder(spec_path: Path | str) -> list[dict]:
    """This spec's comments, or ``[]`` on any read failure (a deleted/corrupted store —
    the caller is printing an at-a-glance summary, not doing something that should abort
    on a single unreadable spec). Shared by `spec-web list` and `spec-web start`/
    `status`, the two registry-driven callers that both fetch-then-reminder per spec; the
    `spec-web reply` surface already has a live, known-good `SpecStore` in hand and calls
    its `stale_comments()` method directly instead."""
    from .specweb.store import SpecStore

    try:
        return SpecStore(spec_path).all_comments()
    except Exception:  # noqa: BLE001
        return []


def _spec_web_print_registry(host: str, port: int) -> None:
    """Print the navigator URL(s) + every registered spec's name-based URL (loopback +
    Tailscale), plus a reminder line for any comment stuck in an open review cycle past
    the staleness threshold (see ``specweb.store.stale_comments``). ``start``/``status``
    is the OTHER place (besides ``spec-web list``) an operator glances at routinely, so it
    gets the same lazily-computed nudge — never a background poller, just whatever this
    already-running command happens to read."""
    from .specweb import registry
    from .specweb.server import _reachable_urls, daemon_spec_urls
    from .specweb.store import stale_comments

    for url in _reachable_urls(host, port):
        print(f"[review spec-web] navigator {url}")
    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered yet — add one with `review spec-web add <spec.md>`"
        )
        return
    for rec in specs:
        urls = " ".join(daemon_spec_urls(rec["name"], host, port))
        missing = "" if rec["exists"] else "  (file missing)"
        agent = f"  (agent: {rec['agent']})" if rec.get("agent") else ""
        print(f"[review spec-web]   {rec['name']}: {urls}{agent}{missing}")
        comments = _comments_for_reminder(rec["path"])
        _print_stale_reminders(stale_comments(comments))


def _spec_web_add(rest: list[str], *, watch: bool) -> int:
    """``review spec-web add|serve <spec.md>`` (and the legacy positional): ensure the daemon is
    running, register the spec, print its name-based URL. ``serve`` (and the legacy form) then
    BLOCK watching for the submit -> stdout handoff; ``add`` returns immediately.

    Backward-compat: on a host WITHOUT the shared service lib, the legacy positional falls back
    to the classic single-spec foreground server so `review spec-web <spec>` still works."""
    parser = argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("spec")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--seed", metavar="FILE", default=None)
    parser.add_argument("--open", dest="open_browser", action="store_true")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument("--no-watch", dest="no_watch", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    ns, _ = parser.parse_known_args(rest)

    # `serve` / the legacy positional own a review round-trip — they REQUIRE the owning agent
    # (submitted batches are DELIVERED into that session; see _spec_web_require_agent). A bare
    # `add` may omit it (the spec then inherits the daemon's own --agent at delivery time).
    if watch:
        rc = _spec_web_require_agent("serve", ns.agent)
        if rc is not None:
            return rc

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web] spec not found: {spec}", file=sys.stderr)
        return 1
    # Canonicalize once, up front: registry + SpecStore key by the RESOLVED path internally,
    # so this keeps every printed path/URL consistent with what the daemon actually serves.
    spec = spec.resolve()

    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    host = ns.host if ns.host is not None else DEFAULT_SPECWEB_HOST
    # NOT `ns.port or DEFAULT`: `--port 0` is falsy and would silently become the default.
    port = ns.port if ns.port is not None else DEFAULT_SPECWEB_PORT

    mgr = _spec_web_manager(host, port, ns.agent)
    if mgr is None:
        if ns.no_watch or not watch:
            # `add` / `--no-watch` mean "register into the daemon + return fast"; without the
            # service lib there is no daemon to register into — the only lib-less mode is the
            # BLOCKING single-spec server, the opposite of what was asked (and what the
            # backstop classifier assumes). Refuse loudly rather than silently blocking.
            print(_SPECWEB_MISSING_LIB, file=sys.stderr)
            print(
                "[review spec-web] add/--no-watch need the daemon; without the lib the only "
                "mode is the blocking single-spec server (`review spec-web <spec.md>`).",
                file=sys.stderr,
            )
            return 4
        # Lib-less host: preserve the classic single-spec foreground server (full backward-compat).
        print(
            "[review spec-web] shared service lib absent — falling back to a single-spec server.",
            file=sys.stderr,
        )
        return _spec_web_legacy_foreground(spec, ns)

    if ns.seed is not None and _spec_web_seed(spec, ns.seed) != 0:
        return 1

    from .specweb import registry
    from .specweb.server import daemon_spec_urls, watch_submits
    from .specweb.store import SpecStore

    # Baseline the store's last_submit BEFORE the daemon is (maybe) started / the spec is
    # registered: a submit that lands in the ensure-running/register window must count as
    # FRESH for the watch below, not be folded into the baseline and lost (the caller would
    # then wait forever on a review that already happened).
    baseline = SpecStore(spec).last_submit()

    rc = _spec_web_ensure_running(mgr, agent=ns.agent)
    if rc is not None:
        return rc

    name = registry.register(spec, agent=ns.agent)
    urls = daemon_spec_urls(name, host, port)
    print(f"[review spec-web] registered '{name}' -> {spec}")
    for url in urls:
        print(f"[review spec-web] {url}")
    if ns.open_browser and urls:
        _spec_web_open_browser(urls[0])
    if watch and not ns.no_watch:
        print(
            "[review spec-web] waiting for a submit (Ctrl-C to stop; the daemon keeps running)."
        )
        return watch_submits(spec, exit_on_submit=ns.exit_on_submit, baseline=baseline)
    return 0


def _spec_web_ensure_running(mgr, *, agent: str | None = None) -> int | None:
    """Start the daemon if it isn't already up (idempotent); None on success, an exit code on
    refusal. A concurrent start losing the race (AlreadyRunningError) is fine — the daemon is
    up either way. LAUNCHING an agentless daemon is refused (nothing may start without an
    owner — see _spec_web_require_agent); an already-running daemon needs no agent here."""
    st = mgr.status()
    if st.running:
        return None
    rc = _spec_web_require_agent("start", agent)
    if rc is not None:
        print(
            "[review spec-web] the daemon is not running and cannot be auto-started "
            "without --agent.",
            file=sys.stderr,
        )
        return rc
    # AlreadyRunningError lives in agenttools_daemon (the pidfile layer agenttools_service
    # builds on) — it is NOT re-exported by agenttools_service. Imported only on the
    # actually-starting path so a fake manager in tests never needs the lib installed.
    from agenttools_daemon import AlreadyRunningError

    try:
        st = mgr.start()
    except AlreadyRunningError:
        return None
    suffix = f" (pid {st.pid})" if st.pid is not None else ""
    print(f"[review spec-web] daemon started{suffix}.")
    return None


def _spec_web_seed(spec: Path, seed: str) -> int:
    """Import an initial review thread into ``spec``'s store before serving (``--seed``)."""
    import json as _json

    from .specweb.store import SpecStore

    seed_path = Path(seed).expanduser()
    try:
        payload = _json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[review spec-web] cannot read seed {seed_path}: {exc}", file=sys.stderr)
        return 1
    replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
    try:
        result = SpecStore(spec).import_thread(payload, replace=replace)
    except ValueError as exc:
        print(f"[review spec-web] bad seed: {exc}", file=sys.stderr)
        return 1
    print(f"[review spec-web] seeded {result['imported']} comment(s) from {seed_path}")
    return 0


def _spec_web_open_browser(url: str) -> None:
    import threading
    import webbrowser

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Timer(0.4, _open).start()


def _spec_web_legacy_foreground(spec: Path, ns) -> int:
    """The classic single-spec blocking server (fallback when the shared service lib is absent).

    The caller has already enforced ``--agent`` (the watch/serve path requires it up front),
    so submitted batches are tmux-delivered to the owning session here too — the lib-less
    fallback must not regress into store-only submits."""
    from .specweb.server import run_specweb

    return run_specweb(
        spec,
        host=ns.host or "127.0.0.1",
        port=ns.port,
        open_browser=ns.open_browser,
        seed=ns.seed,
        verbose=ns.verbose,
        exit_on_submit=ns.exit_on_submit,
        agent=ns.agent,
    )


def _spec_web_list(rest: list[str]) -> int:
    """``review spec-web list`` — every registered spec with its open-note count, plus a
    reminder line for any comment stuck in an open review cycle (submitted/answered) past
    the staleness threshold (see ``specweb.store.stale_comments``) — otherwise a stalled
    thread is silent until someone happens to open the spec-web UI and notices its age."""
    from .specweb import registry
    from .specweb.store import stale_comments

    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered. Add one with `review spec-web add <spec.md>`."
        )
        return 0
    for rec in specs:
        comments = _comments_for_reminder(rec["path"])
        openc = sum(1 for c in comments if c.get("status") in ("pending", "submitted"))
        flag = "" if rec["exists"] else "  (file missing)"
        print(
            f"  {rec['name']:<28} {openc} open / {len(comments)} total  {rec['path']}{flag}"
        )
        _print_stale_reminders(stale_comments(comments))
    return 0


def _spec_web_remove(rest: list[str]) -> int:
    """``review spec-web remove <name>`` — unregister a spec (its comments are kept on disk)."""
    if not rest:
        print("[review spec-web] usage: review spec-web remove <name>", file=sys.stderr)
        return 2
    from .specweb import registry

    name = rest[0]
    if registry.unregister(name):
        print(
            f"[review spec-web] removed '{name}' (its comment store is left on disk)."
        )
        return 0
    print(f"[review spec-web] no registered spec named '{name}'.", file=sys.stderr)
    return 1


def _spec_web_watch(rest: list[str]) -> int:
    """``review spec-web watch <name|path> [--emit-current] [--exit-on-submit]`` — block until a
    fresh submit.

    ``--emit-current`` first re-emits the batch ALREADY in the store — the recovery path when a
    submit's live tmux delivery failed (a bare ``watch`` only fires on a LATER submit, so it
    would never re-surface the already-submitted batch)."""
    parser = argparse.ArgumentParser(prog="review spec-web watch", add_help=False)
    parser.add_argument("target", help="a registered spec NAME or a spec PATH")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument(
        "--emit-current",
        dest="emit_current",
        action="store_true",
        help="re-emit the batch already in the store before watching (recover a failed live delivery)",
    )
    ns, _ = parser.parse_known_args(rest)
    from .specweb import registry
    from .specweb.server import watch_submits

    spec = registry.resolve(ns.target)
    if spec is None:
        spec = Path(ns.target).expanduser()
    if not spec.is_file():
        print(
            f"[review spec-web watch] no such spec (name or path): {ns.target}",
            file=sys.stderr,
        )
        return 1
    print(f"[review spec-web] watching {spec} for a submit (Ctrl-C to stop).")
    return watch_submits(
        spec, exit_on_submit=ns.exit_on_submit, emit_current=ns.emit_current
    )


def _spec_web_reply(argv: list[str]) -> int:
    """`review spec-web reply <comment-id> <answer> --spec <spec.md>`: the AGENT answers a
    reviewer's question/remark. Threads the reply into the store (so the spec-web UI shows
    it under that comment) and best-effort delivers it to the user via the `tg` CLI.

    The spec is required (the store is keyed per spec): pass it as ``--spec``. The reply is
    stamped with the agent author so the UI styles it distinctly.
    """
    parser = argparse.ArgumentParser(
        prog="review spec-web reply",
        description="Answer a reviewer's spec-web question/remark (shown in the UI + sent to tg).",
    )
    parser.add_argument(
        "comment_id",
        help="the id of the comment/question to answer (from the structured review)",
    )
    parser.add_argument("answer", help="the answer text")
    parser.add_argument(
        "--spec",
        required=True,
        metavar="FILE",
        help="path to the spec markdown file (the store is keyed per spec)",
    )
    parser.add_argument(
        "--no-tg",
        action="store_true",
        help="do not deliver the reply to Telegram (UI only)",
    )
    ns = parser.parse_args(argv)

    from .specweb.store import AGENT_AUTHOR, SpecStore

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web reply] spec not found: {spec}", file=sys.stderr)
        return 1
    answer = (ns.answer or "").strip()
    if not answer:
        print("[review spec-web reply] answer is empty", file=sys.stderr)
        return 2

    store = SpecStore(spec)
    rec = store.add_reply(ns.comment_id, body=answer, author=AGENT_AUTHOR)
    if rec is None:
        print(
            f"[review spec-web reply] unknown comment id: {ns.comment_id}",
            file=sys.stderr,
        )
        return 1
    print(
        f"[review spec-web reply] replied to {ns.comment_id} (now {rec.get('status')}); shown in the spec-web UI.",
        flush=True,
    )

    # Piggyback the proactive reminder on a command that's already touching this spec's
    # store — a reply is exactly the moment an agent is paying attention to this thread,
    # so it's the cheapest place to also flag every OTHER thread still stuck in an open
    # review cycle, with no background poller involved (see specweb.store.stale_comments).
    stale_others = [s for s in store.stale_comments() if s.get("id") != rec.get("id")]
    _print_stale_reminders(stale_others, stream=sys.stderr)

    if not ns.no_tg:
        _spec_web_reply_to_tg(spec, rec, answer, stale_others=stale_others)
    return 0


def _stale_summary_for_tg(stale: list[dict]) -> str:
    """A one-line proactive nudge for the tg reply message: how many OTHER comments on
    this spec are stuck in an open review cycle past the staleness threshold, and how
    long the oldest one has been waiting. Empty when there is nothing stale (the common
    case), so a normal reply's tg message is unchanged — this only adds a block when
    there is actually something to nudge about."""
    if not stale:
        return ""
    # max() by age rather than trusting stale[0]: stale_comments() sorts oldest-first, and
    # the production caller (_spec_web_reply) preserves that via a plain list-comp filter,
    # but nothing enforces it on this function's own signature.
    oldest = max(stale, key=lambda s: s.get("age_hours") or 0.0)
    age = oldest.get("age_hours") or 0.0
    plural = "" if len(stale) == 1 else "s"
    statuses = {s.get("status") for s in stale}
    if len(statuses) <= 1:
        # every stalled comment is waiting on the same thing — the single-phrase form
        # from the oldest applies to the whole group.
        waiting_on = _waiting_on_phrase(oldest.get("status"))
        return (
            f"⚠ {len(stale)} other stalled review thread{plural} on this spec "
            f"(oldest {age:.1f}h, waiting on {waiting_on})."
        )
    # A mix of submitted/answered comments: attributing the OLDEST one's wait-phrase to
    # the whole group would misdescribe the others (e.g. "waiting on reviewer resolution"
    # when a different thread in the same count is actually waiting on an agent reply).
    # Break the count down per status instead of picking one phrase for everything.
    submitted_n = sum(1 for s in stale if s.get("status") == "submitted")
    answered_n = len(stale) - submitted_n
    return (
        f"⚠ {len(stale)} other stalled review thread{plural} on this spec "
        f"({submitted_n} awaiting an agent reply, {answered_n} awaiting reviewer "
        f"resolution; oldest {age:.1f}h)."
    )


def _spec_web_reply_to_tg(
    spec: Path,
    comment: dict,
    answer: str,
    *,
    stale_others: list[dict] | None = None,
) -> None:
    """Best-effort: deliver the agent's reply to the user via the `tg` CLI on PATH. NEVER
    raises — tg being absent/failing must not fail the reply (it is already in the store /
    UI). Logs the outcome.

    ``stale_others`` (this reply's own comment already excluded by the caller) is appended
    as a short proactive-reminder block — the tg message that already tells the reviewer
    "here's my answer" is also the cheapest existing delivery channel to surface OTHER
    threads still stuck in an open review cycle, with zero new infrastructure."""
    import shutil
    import subprocess

    exe = shutil.which("tg")
    if not exe:
        print(
            "[review spec-web reply] tg not on PATH — reply saved to the UI only (no Telegram).",
            flush=True,
        )
        return
    question = (comment.get("body") or "").strip()
    quote = (comment.get("quote") or "").strip()
    kind = comment.get("kind") or "remark"

    # Bound the reviewer's free text so a long multi-paragraph remark can't blow past
    # Telegram's ~4096-char message limit (the answer is always shown in full).
    # Plain-text message (no --format html) so we never have to escape the spec/question
    # free text. tg's own --title/--tag give it structure.
    lines = [f"Spec: {spec.name}"]
    if quote:
        lines.append(f"On: “{_clip_text(quote, 200)}”")
    lines.append(
        f"{'Question' if kind == 'question' else 'Remark'}: {_clip_text(question, 600)}"
    )
    lines.append(f"Agent answer: {_clip_text(answer, 3000)}")
    summary = _stale_summary_for_tg(stale_others or [])
    if summary:
        lines.append("")
        lines.append(summary)
    message = "\n".join(lines)
    try:
        proc = subprocess.run(
            [
                exe,
                "--tag",
                "ANSWER",
                "--title",
                f"Spec-web reply — {spec.name}",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            print("[review spec-web reply] delivered to Telegram via tg.", flush=True)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            print(
                f"[review spec-web reply] tg delivery failed (exit {proc.returncode}): {err}",
                flush=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[review spec-web reply] tg delivery error: {exc}", flush=True)


def _run_mode_with_stats(
    mode: str,
    pool_models: list[str],
    dispatch,
    models_after=None,
    roles_after=None,
    usable_roles_after=None,
    *,
    task_code: str | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
    diff_sha256: str | None = None,
    needs_slot: bool = True,
    direct_seats: bool = False,
    later_models: list[str] | None = None,
) -> int:
    """Wait for a host-wide admission slot (reviewlib.admission, review-cli#614), then run
    `_run_admitted_mode_with_stats`. Queue time is outside the recorded duration, so it
    never inflates future ETAs. `needs_slot=False` (a run that calls no model, e.g. a
    diff review with nothing to review) skips the queue. A text panel reserves the
    providers its failover chains dispatch to; `direct_seats=True` (the qa tester) names
    its executors exactly. `later_models` (a quorum/brainstorm moderator) run after the
    panel and are reserved alongside it."""
    slot = (
        admission.run_slot(
            mode=mode,
            models=pool_models if direct_seats else admission.routed(pool_models),
            later_models=admission.routed(later_models or []),
            task_code=task_code,
        )
        if needs_slot
        else contextlib.nullcontext()
    )
    with slot:
        return _run_admitted_mode_with_stats(
            mode,
            pool_models,
            dispatch,
            models_after,
            roles_after,
            usable_roles_after,
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
        )


def _run_admitted_mode_with_stats(
    mode: str,
    pool_models: list[str],
    dispatch,
    models_after=None,
    roles_after=None,
    usable_roles_after=None,
    *,
    task_code: str | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
    diff_sha256: str | None = None,
) -> int:
    """Announce the ETA, time the run on a monotonic clock, and append a stat record.

    `mode` is the EXACT mode (review/just-ask/quorum/brainstorm) and `pool_models` is
    the list of backends DISPATCHED, used to KEY the up-front ETA (so `pool_size` is
    ground truth — for brainstorm that is the per-round persona slot count, which can
    exceed len(models)), not the dashboard parser's inferred/proxy values. `dispatch` is
    a zero-arg callable that runs the mode and returns its exit code. The per-call ok/fail
    tally is collected via panel.begin/end_call_tally so success/fail counts are real per
    backend call.

    `models_after` (optional) is a zero-arg callable read AFTER the run to get the models
    that ACTUALLY produced verdicts — used by the failover board path, where the final
    pool can differ from the planned one (a skipped/failed seat is backfilled from the
    reserve). When given and non-empty, its list is what lands in the stat record, so the
    recorded `pool_size`/`models` reflect what really ran; the ETA still keys on the
    planned `pool_models` (known up front). Without it, `pool_models` is recorded as-is.

    `roles_after` (optional, review-cli#221) is the role-tracking counterpart: a zero-arg
    callable read AFTER the run for the board ROLE each usable seat covered (currently
    only the board dispatch path supplies one — see `panel.FailoverOutcome.usable_roles`).
    A falsy/erroring result omits the record's `roles` key entirely (see `record_run`'s
    `roles` param) rather than recording an empty list — there is no "planned roles"
    fallback the way `models_after` has `pool_models`, since a role only means something
    tied to a seat that actually produced a verdict. The board dispatch's own
    `roles_after` closure (`_ran_roles` in `_dispatch`) returns every EXPLICIT `-m`
    board seat's role for `exact_board` runs, mirroring `_ran_models`'s identical choice
    there — safe ONLY because an exact board has no reserve, so this function's caller
    (`_dispatch`) can only ever pass a `passed=True` verdict up to `record_run` when
    every one of those seats genuinely produced a verdict (any single failure degrades
    the whole run to a nonzero exit first); a record whose seats didn't ALL succeed is
    always `passed=False` and therefore never counts toward `--min-roles` regardless
    of what this callable returned. `roles` itself is UNCHANGED by review-cli#137 below
    (still index-aligned with `models`, still consumed by the
    `_models_behind_role_coverage` monoculture-suggestion diagnostic) but is NO LONGER
    the sole source `quorum_check`'s `--min-roles` gate credits from — that's now
    `usable_roles_after`.

    `usable_roles_after` (optional, review-cli#137) is the SEAT-granular sibling of
    `roles_after`: a zero-arg callable read AFTER the run for `record_run`'s
    `usable_roles` param (see its docstring), recording seat-level role credit that
    survives a sibling seat's hard failure in the same run. Unlike `_ran_models`/
    `_ran_roles`'s exact-board special case above, this needs NO such case:
    `panel.FailoverOutcome.usable_roles` already means "seats that individually
    succeeded" whether or not a reserve exists (an exact board with no reserve simply
    degrades instead of backfilling), so the same `outcome_sink[0].usable_roles if
    outcome_sink else []` expression covers both the exact and reserve-failover board
    shapes — see `_ran_usable_roles` in `_dispatch`.

    A run that dispatched ZERO backend calls (a clean-tree review with no diff, an
    early usage error) is NOT recorded: it has no real wall-clock to contribute and a
    ~0s record would drag every future ETA for that pool toward zero — defeating the
    whole point. The ETA line is still printed (it costs nothing and warns the agent),
    but only real runs land in the history. Stats failures NEVER affect the run.

    `repo_id`/`diff_files`/`diff_sha256` are the diff-identity fields (reviewlib.stats
    "Diff-identity binding") threaded straight through to `record_run` unchanged — this
    function doesn't compute or interpret them, it just carries them from the caller
    (which already has `cwd`/`diff` in scope) to the stat record.
    """
    import time

    pool_size = len(pool_models)
    announce_eta(mode, pool_size)
    begin_call_tally()
    started = datetime.now(timezone.utc)
    start = time.monotonic()
    old_task_env = os.environ.get("REVIEW_TASK_CODE")
    if task_code:
        os.environ["REVIEW_TASK_CODE"] = task_code
    rc: int | None = None
    try:
        rc = dispatch()
        return rc
    finally:
        if task_code:
            if old_task_env is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task_env
        elapsed = time.monotonic() - start
        tally = end_call_tally()
        ok_count, fail_count = tally["ok"], tally["fail"]
        prompt_tokens = tally["prompt_tokens"]
        output_tokens = tally["output_tokens"]
        recorded_models = pool_models
        models_after_fell_back = False
        if models_after is not None:
            try:
                actual = models_after()
            except Exception:  # noqa: BLE001 — stats must never break the run
                actual = None
            if actual:
                recorded_models = actual
            else:
                models_after_fell_back = True
        recorded_roles = None
        # Round-5/6 review finding (Opus): `models_after` and `roles_after` both read
        # the SAME `outcome_sink[0]` snapshot, and `quorum_check`'s monoculture guard
        # (`_models_behind_role_coverage`) zips `recorded_models` against
        # `recorded_roles` BY INDEX — so if `models_after` ever fell back to the
        # PLANNED `pool_models` (its own error/empty-result path) while `roles_after`
        # still returned the ACTUAL roles, the two lists would no longer describe the
        # same seats in the same order. Force `recorded_roles` to `None` (omitted,
        # not a misleadingly-paired list) whenever `recorded_models` isn't the real,
        # actually-produced roster — there is no "planned roles" fallback that could
        # keep the pairing honest the way `pool_models` does for models alone. The
        # round-5 cut only checked `models_after_fell_back`, which stays False when
        # `models_after` is None entirely (never set) — a FUTURE caller supplying
        # `roles_after` alone (no current one does) would then pair real roles
        # against the merely-PLANNED `pool_models`. Requiring `models_after is not
        # None` too closes that: roles only ever get recorded when there was an
        # ACTUAL-roster callable in the loop, not just a planned one.
        if (
            roles_after is not None
            and models_after is not None
            and not models_after_fell_back
        ):
            try:
                actual_roles = roles_after()
            except Exception:  # noqa: BLE001 — stats must never break the run
                actual_roles = None
            if actual_roles:
                recorded_roles = actual_roles
        recorded_usable_roles = None
        # review-cli#137: unlike `recorded_roles` above, this has NO dependency on
        # `models_after` succeeding/not-falling-back -- `usable_roles` is not
        # index-paired with `models` at all (see record_run's `usable_roles`
        # docstring), it is simply "the roles this run individually satisfied", so
        # there is no pairing invariant to protect here.
        if usable_roles_after is not None:
            try:
                actual_usable_roles = usable_roles_after()
            except Exception:  # noqa: BLE001 — stats must never break the run
                actual_usable_roles = None
            if actual_usable_roles:
                recorded_usable_roles = actual_usable_roles
        # Only record a run that actually dispatched at least one backend call. No
        # dispatch -> nothing real to time -> skip, so no-op invocations never poison
        # the ETA average.
        if ok_count or fail_count:
            # The mode handler's own exit code IS the run's verdict for every TEXT
            # panel mode (review/quorum/just-ask/brainstorm): each already returns 0
            # iff its own success criterion held (every seat produced a usable
            # verdict / the board didn't degrade) and nonzero otherwise — the same
            # signal the CLI exits the process with. Reuse it rather than inventing
            # a parallel verdict pipeline.
            #
            # KNOWN LIMITATION for `review` (diff review) specifically: this rc is
            # "every reviewer call completed technically" (reviewlib/modes/review.py's
            # `ok = all(r.returncode == 0 for r in results)`), NOT "every reviewer
            # approved the diff with no findings" — review-cli's text-panel reviewers
            # print free-text findings with no structured accept/reject grammar to
            # parse (unlike `qa`'s PASS/FAIL/BLOCKED verdict below), so there is no
            # richer existing signal to key on. This still closes the gap the CTO
            # decision (tg#7306 #1) targeted — a run whose backend calls genuinely
            # failed no longer counts — but a reviewer that ran cleanly and printed
            # "found 3 blocking issues" still counts as passed today. Building a real
            # content verdict for review mode is a separate, larger effort (issue
            # #137 tracks whether/how to close this).
            #
            # `qa` is the one mode where exit code is NOT a verdict: it is
            # deliberately REPORT-ONLY (reviewlib/qa/executor.py:verdict_to_exit_code)
            # — a FAIL verdict with real findings still exits 0 unless --strict, so
            # rc==0 would wrongly read as "passed" for a run that found bugs. Rather
            # than mis-record a qa run as passed, its verdict is UNKNOWN here (not
            # threaded through this generic wrapper) and fails closed — it simply
            # never counts toward the quorum gate until a dedicated fix threads qa's
            # parsed PASS/FAIL/BLOCKED verdict through instead of its exit code.
            #
            # brainstorm's exit 0 on a MID-RUN COLLAPSE (some good rounds, then a
            # dead panel, still synthesized — reviewlib/modes/brainstorm.py's
            # "documented partial-success behavior", CTO 2026-06-16) is intentionally
            # still treated as passed here, unlike qa: it is brainstorm's own,
            # deliberately-chosen definition of "produced a usable result", not an
            # accidental decoupling of exit code from verdict the way qa's is — qa
            # has a real PASS/FAIL/BLOCKED grammar that the exit code ignores by
            # design; brainstorm has no such grammar to ignore. Whether a degraded
            # brainstorm (or any non-`review`-mode iteration at all) SHOULD count
            # toward the self-merge-authority quorum is a separate, broader design
            # question tracked in issue #137 — out of scope for this "ran vs passed"
            # fix, which only had to stop mis-recording a KNOWN-not-passed run as
            # passed.
            #
            # PARTIALLY ADDRESSED (review-cli#137, 2026-09-10): the paragraphs above describe a
            # CONTENT-verdict gap (ran cleanly vs. approved-with-no-findings) and a non-review-
            # mode-counting policy question — both remain fully open; this fix touches NEITHER.
            # A different, narrower gap in the same "what should count as a role credit" area is
            # now fixed instead: `ok`/`rc` above is still the WHOLE run's own all-or-nothing
            # verdict (unchanged, still what `passed` reflects), but `quorum_check`'s
            # `--min-roles` gate no longer inherits that all-or-nothing shape for role CREDIT.
            # Previously, an exact `-m modelA,modelB,modelC,...` board (no reserve to backfill a
            # failed seat) recorded per-seat role data that only ever counted when `passed` was
            # True for the WHOLE run — so a single quota-exhausted sibling seat (nonzero
            # returncode) zeroed out role credit for a genuinely successful seat in the SAME run
            # (e.g. Opus succeeding while Codex/GLM/Grok/Sonnet were all exhausted), even though
            # that seat's own verdict was real. See `_ran_usable_roles`/`usable_roles_after`
            # below and `record_run`'s `usable_roles` param / `stats._seat_credited_roles` — role
            # credit is now SEAT-granular: a seat's own role counts toward `--min-roles` whenever
            # THAT SEAT individually produced a usable verdict, regardless of a sibling's hard
            # failure in the same run. `--min-iter`/`--min-models` (and `passed` itself) are
            # UNCHANGED — still the run's own all-or-nothing signal, exactly as this whole
            # comment block already describes.
            #
            # `rc` is None only if `dispatch()` raised before returning; treat that
            # as not-passed (fail-closed) too — a run that never completed is not a
            # pass by any definition.
            if mode == "qa":
                verdict: bool | None = None
            elif rc is None:
                verdict = False
            else:
                verdict = rc == 0
            record_run(
                task_code=task_code,
                mode=mode,
                models=recorded_models,
                duration_seconds=elapsed,
                ok_count=ok_count,
                fail_count=fail_count,
                started=started,
                passed=verdict,
                repo_id=repo_id,
                diff_files=diff_files,
                diff_sha256=diff_sha256,
                roles=recorded_roles,
                usable_roles=recorded_usable_roles,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )


def _call_with_task_env(task_code: str | None, fn):
    """Run a pre-stats helper while exposing the validated task code to log writers."""
    clean_task = normalize_task_code(task_code)
    if not clean_task:
        return fn()
    old_task_env = os.environ.get("REVIEW_TASK_CODE")
    os.environ["REVIEW_TASK_CODE"] = clean_task
    try:
        return fn()
    finally:
        if old_task_env is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task_env


# Subcommands that run a PERSISTENT server until Ctrl-C (`review dashboard`,
# `review spec-web`) — these are intentionally long-lived and must NOT be bounded by
# the run backstop, which would otherwise kill the server after the ceiling (or almost
# immediately under a lowered $REVIEW_BACKSTOP_SECONDS). The backstop is for the
# bounded review/model RUN paths only.
_SERVER_SUBCOMMANDS = frozenset({"dashboard", "spec-web"})


def _is_persistent_server_invocation(argv: list[str]) -> bool:
    """True when argv starts a PERSISTENT server that runs until Ctrl-C and so must bypass the
    `-o` tee + the run backstop. The short-lived management actions are NOT servers — they
    return immediately — so they go through the normal tee/backstop path like any instant
    subcommand:

      * ``spec-web reply …``  — returns immediately, not a server.
      * ``dashboard`` lifecycle actions (``start``/``status``/``stop``/``enable``/``disable``
        and the bare HELP) — return immediately. Only the FOREGROUND blocking server blocks:
        ad-hoc ``dashboard run`` and the hidden ``dashboard __serve`` it dispatches to.
    """
    if not argv or argv[0] not in _SERVER_SUBCOMMANDS:
        return False
    if argv[0] == "spec-web":
        return _spec_web_is_persistent(argv[1:])
    if argv[0] == "dashboard":
        # Only the blocking foreground server is persistent; everything else returns fast.
        # The managed-service parser accepts the global `--host`/`--port` options BEFORE the
        # action (`dashboard --port 7878 run`), so the action is NOT necessarily argv[1] — it
        # is the first NON-OPTION token. Misclassifying `--port N run` as non-persistent would
        # wrap the foreground server in the run backstop and let it be killed. `--host`/`--port`
        # are the only options that take a value here; skip the flag and its argument.
        return _dashboard_action(argv[1:]) in ("run", "__serve")
    return True


# spec-web actions that BLOCK (a persistent server / a submit-watch loop) and so must bypass the
# `-o` tee + run backstop; everything else (lifecycle management, register, list) returns fast.
_SPECWEB_FAST_ACTIONS = frozenset(
    {
        "reply",
        "start",
        "status",
        "stop",
        "enable",
        "disable",
        "add",
        "list",
        "remove",
        "-h",
        "--help",
    }
)


def _spec_web_is_persistent(rest: list[str]) -> bool:
    """True when ``review spec-web <rest>`` blocks (foreground daemon / submit-watch), so it must
    NOT be wrapped in the run backstop. A bare ``spec-web`` (help) and the fast management actions
    return immediately; ``run``/``__serve`` are the blocking daemon; ``watch`` always blocks;
    ``serve`` and the LEGACY positional ``spec-web <path>`` block on the submit-watch unless
    ``--no-watch``."""
    if not rest:
        return False  # bare `review spec-web` prints help + launches nothing
    sub = rest[0]
    if sub in _SPECWEB_FAST_ACTIONS:
        return False
    if sub in ("run", "__serve", "watch"):
        return True  # `watch` has no --no-watch: it exists only to block on the submit
    if sub == "serve":
        return "--no-watch" not in rest
    # A legacy positional `review spec-web <path>` (register + serve-in-daemon + watch, or the
    # lib-less foreground fallback) blocks unless `--no-watch`. `--no-watch` returns fast in
    # BOTH worlds: daemon mode registers + returns; the lib-less fallback REFUSES it (exit 4)
    # rather than silently blocking (see _spec_web_add).
    return "--no-watch" not in rest


def _dashboard_action(rest: list[str]) -> str | None:
    """The dashboard ACTION (run/start/…) from the tokens after ``dashboard``, or ``None``."""
    return _dashboard_action_with_index(rest)[0]


def _dashboard_action_with_index(rest: list[str]) -> tuple[str | None, int]:
    """``(action, index)`` for the dashboard action token, or ``(None, -1)``.

    Skips the global ``--host``/``--port`` options (and their values) that the managed-service
    parser allows before the action, so ``--port 7878 run`` resolves to ``run`` — matching how
    argparse itself parses it (see :func:`_dashboard_subcommand`). The index lets a caller drop
    exactly the action token (not a value that merely equals it)."""
    i = 0
    value_opts = ("--host", "--port")
    while i < len(rest):
        tok = rest[i]
        if tok in value_opts:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            # `--port=7878` or any other valueless flag — skip the single token.
            i += 1
            continue
        return tok, i  # first non-option token == the action
    return None, -1


class _Tee(io.TextIOBase):
    """A write-through tee: every write goes to BOTH a live stream (the real stdout,
    so the user still sees the review as it streams) AND an in-memory buffer that
    `-o FILE` later persists. We mirror stdout rather than redirecting it so `-o`
    NEVER swallows the on-screen output (the task: "still also print to stdout").
    Only `write`/`flush` are exercised by `print()`; the rest delegates to the live
    stream so the object stays a drop-in `sys.stdout`."""

    def __init__(self, live: TextIO, buffer: io.StringIO) -> None:
        self._live = live
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._live.write(s)

    def flush(self) -> None:
        self._live.flush()

    def isatty(self) -> bool:
        return self._live.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._live, "encoding", "utf-8")


# Options that CONSUME the next token as their value (space-separated form). When the
# pre-scan for `-o` sees one of these, the FOLLOWING token is that option's value and
# must be passed through untouched — even if it happens to look like `-o`/`--output`
# (e.g. `review --just-ask --output` where `--output` is the question text, or a
# `--prompt -o…`). This keeps the light pre-scan from stealing another flag's value.
_VALUE_TAKING_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "-o",
        "--output",
        "--prompt",
        "--timeout",
        "--pool",
        "--preset",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--vision-timeout",
        "--project",
        # `--retry N` is a diff-mode-only int option (reviewlib/modes/review.py). It consumes a
        # value, so the `-o` pre-scan must skip its argument — otherwise `--retry -o…`-shaped input
        # would have the retry count mis-read as the output flag.
        "--retry",
        # `review spec-web reply <id> <answer> --spec <path>`: the value after --spec is a spec
        # path that could look like an option (e.g. `--spec -odd-name.md`); list it so the `-o`
        # pre-scan never steals it.
        "--spec",
        # The qa mode's VALUE-taking flags (modes/qa.py): `--suites <glob/dir/file>`,
        # `--kind <shape>`, `--report <path>`, `--max-cases <N>`, plus the Phase-3 env flags
        # `--stage-url <URL>` and `--config <path>`. Their values can look like an option (a path,
        # `-1`), so the `-o` pre-scan must skip each one's argument. `--in-place` / `--keep-env`
        # are boolean flags (no value) and are deliberately NOT listed.
        "--suites",
        "--kind",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
    }
)


def _extract_output_path(argv: list[str]) -> tuple[Path | None, list[str]]:
    """Pull the output flag OUT of argv before dispatch and return (path, remaining).

    Recognized forms: `-o FILE`, `--output FILE`, `--output=FILE`, `-o=FILE`, and the
    glued short `-oFILE`. `-o` is handled OUTSIDE the main argparse surface because the
    capture has to wrap the WHOLE dispatch (every mode prints its final result to
    stdout), and the bare subcommands (install-skill, dashboard, spec-web, …) never
    reach the main parser. A single light pre-scan here makes `-o` work uniformly for
    every path while the parser still advertises it in `--help`.

    Two safeguards keep the pre-scan from misreading another option's value as the
    output flag: (1) scanning STOPS at the first `--` (end-of-options), so a positional
    that starts with `-o` is kept verbatim; (2) a token that is the VALUE of a preceding
    value-taking option (`--just-ask --output`, `--prompt -o…`) is NOT intercepted — it
    is passed through so argparse still receives that option's argument. When the flag
    is absent the remaining list has the SAME contents as the input (a fresh list); a
    bare `-o` with no value is left in the remaining argv so argparse reports the usage
    error instead of a silent swallow."""
    out: Path | None = None
    rest: list[str] = []
    i = 0
    value_for_previous = False
    # `review task CODE --check` is a BOOLEAN flag (no value) — the sole exception to
    # `--check` meaning `--check NAME` (visual module force-activation) everywhere else.
    # Exclude it from the value-taking set for a `task` invocation so this pre-scan
    # doesn't mistake a following `-o FILE` for --check's (nonexistent) value.
    value_taking_opts = (
        _VALUE_TAKING_OPTS - {"--check"}
        if argv and argv[0] == "task"
        else _VALUE_TAKING_OPTS
    )
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # End of options: keep `--` and everything after it untouched.
            rest.extend(argv[i:])
            break
        # If the prior token we recognized as a value-taking option (space form), THIS
        # token is its value — pass it through, never read it as the output flag. Track
        # this as state, not by peeking at argv[i-1], because a *value* can itself equal
        # another value-taking flag string (e.g. `--task --output -o out.md`).
        if value_for_previous:
            rest.append(tok)
            value_for_previous = False
            i += 1
            continue
        if tok in ("-o", "--output"):
            if i + 1 < len(argv):
                out = Path(argv[i + 1]).expanduser()
                i += 2
                continue
            # No value — leave it for argparse to flag (don't silently swallow).
            rest.append(tok)
            i += 1
            continue
        if tok.startswith("--output="):
            out = Path(tok.split("=", 1)[1]).expanduser()
            i += 1
            continue
        if tok.startswith("-o="):
            # `-o=FILE` — accept it (symmetry with `--output=FILE`).
            out = Path(tok.split("=", 1)[1]).expanduser()
            i += 1
            continue
        if tok.startswith("-o") and len(tok) > 2:
            # `-oFILE` (glued short form).
            out = Path(tok[2:]).expanduser()
            i += 1
            continue
        rest.append(tok)
        if tok in value_taking_opts:
            value_for_previous = True
        i += 1
    return out, rest


_LEADING_MODE_VALUE_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "--timeout",
        "--pool",
        "--preset",
    }
)
_LEADING_MODE_FLAG_OPTS = frozenset({"--list-defaults", "--show-board"})
_LEADING_MODE_INLINE_SHORT_OPTS = ("-m", "-C")


def _is_leading_inline_short_option(tok: str) -> bool:
    """True only for glued short global forms like `-mMODEL`, never long flags."""
    return (
        len(tok) > 2
        and tok.startswith("-")
        and not tok.startswith("--")
        and any(
            tok.startswith(opt) and tok != opt
            for opt in _LEADING_MODE_INLINE_SHORT_OPTS
        )
    )


def _normalize_leading_mode_options(argv: list[str]) -> list[str]:
    """Allow truly-global options before a mode verb.

    The mode parser already accepts `review diff -m fable ...`; users also naturally type
    `review -m fable diff ...` because `-m` is advertised as global. Move only recognized
    global options that appear before a known mode verb to just after that verb, leaving
    unknown/management invocations untouched so argparse still reports the right error.
    """
    if not argv or (not argv[0].startswith("-") and argv[0] in known_subcommands()):
        return argv
    moved: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv
        if not tok.startswith("-"):
            if tok in known_subcommands() and moved:
                return [tok, *moved, *argv[i + 1 :]]
            return argv
        if tok in _LEADING_MODE_VALUE_OPTS:
            if i + 1 >= len(argv):
                return argv
            moved.extend([tok, argv[i + 1]])
            i += 2
            continue
        if tok in _LEADING_MODE_FLAG_OPTS:
            moved.append(tok)
            i += 1
            continue
        if any(
            tok.startswith(f"{opt}=")
            for opt in _LEADING_MODE_VALUE_OPTS
            if opt.startswith("--")
        ):
            moved.append(tok)
            i += 1
            continue
        if _is_leading_inline_short_option(tok):
            moved.append(tok)
            i += 1
            continue
        return argv
    return argv


def _write_output_file(path: Path, text: str) -> None:
    """Persist captured stdout to `path` via Python `open(...,"w")` — which bypasses
    the shell entirely, so it NEVER trips zsh `noclobber` the way `review … > FILE`
    does (the bug this flag exists to kill). ANSI escape sequences are stripped so the
    file is clean text even if the live stream was coloured. Parent dirs are created;
    an existing file is overwritten (that is the point). A bad path raises a clear
    OSError that the caller turns into a non-zero exit with an actionable message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strip_control_sequences(text), encoding="utf-8")


# Env var that marks "a `review` invocation is already running in this process tree"
# (review-cli#180). Set for the lifetime of a run by `main()`; checked by
# `_reject_if_reentrant()` before any backend is touched.
REVIEW_CLI_ACTIVE_ENV = "REVIEW_CLI_ACTIVE"


def _reject_if_reentrant(_argv: list[str]) -> int | None:
    """Fail closed if this process is already nested inside a running `review`
    invocation — the review-cli#180 fork-bomb guard.

    A codex/claude/opencode backend can shell out to `review diff` again on the same
    worktree, which spawns another backend, which can do it again — an unbounded
    self-reinvocation loop (live-confirmed 2026-08-11: 40+ live `codex exec` processes
    and 11 `review diff` processes across 4 worktrees, swap at 88.5%, load average
    60+). The existing process-GROUP kill/backstop machinery (`process._run_streamed`,
    `reviewlib.backstop`) cannot bound this: each backend child is spawned with
    `start_new_session=True` so a per-call timeout can kill just that call's tree
    without also taking down the CLI's own group — but that means every recursive
    level re-roots into a BRAND NEW OS session, invisible to a `killpg` rooted at an
    earlier level. An env var is the one signal that survives `exec`/`setsid`
    regardless of how many session boundaries the recursion crosses, so it is checked
    here instead.

    Deliberately NOT reset by `call_backend`/panel concurrency — those run several
    backends in threads WITHIN one process (review-cli#65), not nested `review`
    invocations, so they never trip this.

    VERIFIED (not assumed — re-raised by every review round on this ticket, so
    recording the evidence here): $REVIEW_CLI_ACTIVE is set for a persistent SERVER
    subcommand's (`review dashboard`/`review spec-web`) entire lifetime too (see
    `main()` — it wraps the whole dispatch, server path included). This is safe
    because neither server spawns `review` as a child process during normal request
    handling: `reviewlib/dashboard/server.py` and `reviewlib/specweb/server.py` only
    shell out to `tailscale`/`git`/`tmux`; `reviewlib/dashboard/service.py`'s
    `_review_argv0()` launches the server's OWN foreground process at STARTUP (once,
    from the user's shell — not a request-handling re-invocation) and is itself gated
    against re-entering the service layer (see that module's docstring: "otherwise
    run/start would re-enter the service layer and fork-bomb"). If a future change
    adds a code path where a server-mode process dispatches an actual model review
    while servicing a request, that path should call the mode/panel functions
    directly (in-process, the way `reviewlib.panel`/`reviewlib.modes` are normally
    invoked), not subprocess-invoke `review` on itself.

    Checks PRESENCE (`is not None`), not truthiness: `REVIEW_CLI_ACTIVE= review diff`
    (a shell setting the var to an EMPTY string for the child) must still trip the
    guard — `os.environ.get(...)` would return `""`, which is falsy under a bare
    `if`, and silently let the recursion through (review-cli#180 review finding,
    glm-5.2). This does NOT defend against a shell that removes the var entirely
    (`env -u REVIEW_CLI_ACTIVE review diff`) — no env-var-based guard can, since the
    child then looks identical to a genuinely fresh top-level invocation. That
    residual gap is why this is layered with, not a substitute for, the codex
    execpolicy guard (`install.install_codex_recursion_guard`) and the existing
    process-group timeout/backstop.

    This exact `env -u REVIEW_CLI_ACTIVE` technique is also used DELIBERATELY, not just as an
    attacker's residual gap: `dashboard.service._env_clear_prefix()` (reused by
    `specweb.service._serve_argv`) prepends it to the managed `__serve` child's argv, because
    `run`/`start` are themselves active `review` invocations and the spawned server must NOT
    inherit that activity marker (review-cli#180 review finding, chatgpt-codex-connector, PR
    #279) — a sanctioned use of the same bypass this paragraph documents as unstoppable."""
    if os.environ.get(REVIEW_CLI_ACTIVE_ENV) is not None:
        print(
            "[review-cli] refusing to run: $REVIEW_CLI_ACTIVE is already set, meaning "
            "this process is nested inside another `review` invocation. A backend "
            "(codex/claude/opencode) appears to have re-invoked `review` on the same "
            "worktree — this is the unbounded self-reinvocation loop tracked as "
            "review-cli#180. Refusing to recurse.",
            file=sys.stderr,
        )
        return 1
    return None


def _dispatch_with_backstop(raw: list[str], output_path: str | None) -> int:
    """Resolve and run `raw`, applying the internal backstop timer + `-o` tee.

    Split out of `main()` so the REVIEW_CLI_ACTIVE reentrancy guard (review-cli#180)
    can wrap this ENTIRE dispatch — server subcommand included — in one try/finally at
    the call site, without main() itself growing past a screenful.

    `review` advertises NO external timeout — agents must not wrap it in a short
    shell `timeout` (the panel/brainstorm modes only emit their synthesis at the very
    end). The ONLY time bound is the INTERNAL last-resort backstop, capped at <=4h
    (`reviewlib.backstop`): a watchdog that force-terminates a genuinely wedged run so
    "no external timeout" can never mean "runs forever". A healthy run finishes in
    minutes, far under the ceiling, and the watchdog is cancelled cleanly on return.

    The persistent SERVER subcommands (`dashboard`, `spec-web`) are deliberately
    long-lived (they run until Ctrl-C), so they bypass the backstop entirely — bounding
    them would kill the server at the ceiling, and a lowered env var would kill it almost
    at once (codex P2). Every other path (the review/panel run and the instant
    subcommands) is wrapped.

    This is also where `-o FILE` is handled: the flag is pre-scanned out of argv by the
    caller (so it works for every dispatch path, including the bare subcommands), and
    when present the whole dispatch runs under a stdout TEE whose captured text is
    persisted to FILE via Python — bypassing the shell redirect (and thus zsh
    noclobber). The file is always written; stdout still prints live.
    """
    # The persistent SERVER subcommands stream until Ctrl-C — capturing/teeing their
    # output to a single `-o` file makes no sense (and the file would only be written
    # on shutdown), so `-o` is ignored for them and they bypass both the tee and the
    # backstop exactly as before. `review spec-web reply …` is the EXCEPTION: it is a
    # short-lived command, not the server, so it must NOT bypass — `-o` should work and
    # the backstop should bound it like any other instant subcommand.
    if _is_persistent_server_invocation(raw):
        return _dispatch(raw)

    if output_path is None:
        with run_backstop():
            return _dispatch(raw)

    # `-o FILE`: tee stdout (so the review STILL prints live) and persist the captured
    # text via Python open()/write — which sidesteps zsh `noclobber` (the failure mode
    # this flag fixes). The file is written even on a non-zero exit or empty result (a
    # caller that asked for a file gets one) — but NOT when the dispatch exits EARLY via
    # SystemExit. An argparse usage error or `--help` raises SystemExit before any review
    # ran; writing then would TRUNCATE a pre-existing `-o` target to empty/help-text — a
    # silent data-loss footgun (e.g. `review --bad-flag -o important.md`). So a SystemExit
    # propagates with NO write; the file is touched only when `_dispatch` actually
    # returned (the review path ran).
    captured = io.StringIO()
    real_stdout = sys.stdout
    rc = 1
    completed = False
    try:
        with contextlib.redirect_stdout(_Tee(real_stdout, captured)):
            with run_backstop():
                rc = _dispatch(raw)
                completed = True
    finally:
        # Only persist when the dispatch RAN to a return (completed). On a SystemExit
        # (argparse/--help) or any other propagating exception, skip the write so a
        # pre-existing target is never truncated by an early exit. The write outcome is
        # recorded but NOT returned from `finally` (a `return` there would swallow a
        # propagating exception); the final return below applies it only on a clean run.
        write_error: OSError | None = None
        if completed:
            try:
                _write_output_file(output_path, captured.getvalue())
            except OSError as exc:
                write_error = exc
                print(
                    f"[review-cli] -o: could not write {output_path}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    return 1 if write_error is not None else rc


def main(argv: list[str] | None = None) -> int:
    """Entry point: install the child-reaper, then either detach a background job or
    run the review inline (reentrancy guard + backstop).

    See `_reject_if_reentrant` for the review-cli#180 fork-bomb guard,
    `_dispatch_with_backstop` for the backstop/`-o`-tee mechanics, and
    `_spawn_detached_job` for `--detach` (review-cli#160 companion feature), which
    lets the CALLER stop blocking at all — it spawns the review as a session-detached
    background process and returns almost immediately with a job-id (`review status
    <job-id>` / `review jobs` poll it afterwards).

    This function also finalizes a DETACHED job's terminal status: when
    `$REVIEW_JOB_ID` is set (only true inside a child `_spawn_detached_job` itself
    spawned), the body's return code — or the code of a propagating `SystemExit` — is
    recorded via `reviewlib.jobs.write_job` before this process exits, so `review
    status` never has to guess from a raw exit code alone.
    """
    # Reap live backend children on an EXTERNAL SIGTERM/SIGINT (a caller's `kill <pid>`,
    # a harness timing out this process, or Ctrl-C) — not just an internal backstop
    # fire. See `process.install_signal_reaper` for why this is a distinct gap
    # (review-cli#160): the internal backstop only reaps when ITS OWN watchdog fires
    # from a live interpreter; an external signal's default disposition kills the
    # process before any `finally` runs, orphaning session-isolated backend children.
    install_signal_reaper()
    # A model's review text (→, ✓, CJK) must never crash printing on a cp1251/cp1252 pipe
    # (Windows mintty/Git Bash/redirects); see `portable.ensure_utf8_stdio`.
    portable.ensure_utf8_stdio()

    raw = sys.argv[1:] if argv is None else argv
    # The reentrancy guard runs BEFORE the --detach split (review-cli#180 x #160): a
    # backend nested inside a running `review` must not be able to sidestep the
    # fork-bomb guard by detaching — the spawned child would start from a clean
    # environment (no $REVIEW_CLI_ACTIVE, by design) and recurse freely. It is also
    # the highest-severity failure, so it wins over any usage error below.
    rejected = _reject_if_reentrant(raw)
    if rejected is not None:
        return rejected
    detach, raw = _extract_detach_flag(raw)
    if detach:
        return _spawn_detached_job(raw)

    job_id = os.environ.get("REVIEW_JOB_ID")
    if not job_id:
        return _main_dispatch(raw)

    # Detached-child finalization path: run the SAME body as a normal foreground call,
    # then durably record how it ended — including a SystemExit (argparse usage error,
    # `--help`, or a deliberate `sys.exit`), which otherwise propagates past a plain
    # `return` and would leave the job record stuck at "running" forever.

    rc = 1
    try:
        rc = _main_dispatch(raw)
    except SystemExit as exc:
        code = exc.code
        rc = 0 if code is None else (code if isinstance(code, int) else 1)
        raise
    finally:
        # Best-effort, like every other job-record write in this module: `$REVIEW_JOB_ID`
        # is only ever set by OUR OWN `_spawn_detached_job` in the shipped call graph, but
        # it is still an inherited environment variable — a malformed/tampered value must
        # never crash the finalizer and mask the review's own real result (codex review:
        # `jobs.write_job` raises `InvalidJobId` for anything that isn't a well-formed id).
        try:
            jobs.write_job(
                job_id,
                status="done" if rc == 0 else "failed",
                exit_code=rc,
                finished_at=time.time(),
            )
        except jobs.InvalidJobId:
            print(
                f"[review-cli] $REVIEW_JOB_ID is not a valid job id ({job_id!r}) — "
                "not recording a job status.",
                file=sys.stderr,
            )
        except OSError as exc:
            # A jobs dir that became unwritable AFTER spawn (a sandbox grant that
            # expired mid-review) must not replace the review's own real exit code
            # with a traceback; the record is left "running" and `job_status`'s
            # dead-pid reconciliation reports it as unknown-terminated later.
            print(
                f"[review-cli] could not record the final status of job {job_id}: "
                f"{type(exc).__name__}: {exc} — the review itself finished with exit "
                f"code {rc}.",
                file=sys.stderr,
            )
    return rc


def _extract_detach_flag(argv: list[str]) -> tuple[bool, list[str]]:
    """Pull a bare `--detach` flag out of argv before dispatch (review-cli#160
    companion feature), mirroring `_extract_output_path`'s pre-scan so `--detach` works
    uniformly ahead of every mode's own parser. Boolean, no value: unlike `-o` there is
    no glued/`=`-value form to recognize. Scanning stops at the first `--`
    (end-of-options) and skips the value of any preceding value-taking option, so
    `--prompt --detach` (the flag as literal prompt TEXT) is never misread as the
    switch."""
    out = False
    rest: list[str] = []
    i = 0
    value_for_previous = False
    value_taking_opts = (
        _VALUE_TAKING_OPTS - {"--check"}
        if argv and argv[0] == "task"
        else _VALUE_TAKING_OPTS
    )
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        if value_for_previous:
            rest.append(tok)
            value_for_previous = False
            i += 1
            continue
        if tok == "--detach":
            out = True
            i += 1
            continue
        rest.append(tok)
        if tok in value_taking_opts:
            value_for_previous = True
        i += 1
    return out, rest


def _reviewlib_repo_root() -> str:
    """The directory CONTAINING the `reviewlib` package this process is actually
    running (its parent), suitable for `$PYTHONPATH`. `python -m reviewlib` only finds
    the package if it is importable — true for a `pip install`, but NOT guaranteed for
    the symlink-install path (`bin/review`'s shim inserts the repo root into ITS OWN
    `sys.path`, which a spawned `python -m reviewlib` subprocess does not inherit). Every
    install shape reaches this same line already having imported `reviewlib`
    successfully (we ARE `reviewlib.cli`), so its resolved `__file__` always answers
    correctly regardless of how THIS process found the package."""
    import reviewlib

    return str(Path(reviewlib.__file__).resolve().parent.parent)


def _first_verb(raw: list[str]) -> str | None:
    """The first non-option token of `raw`, skipping the VALUE of any value-taking
    option (`-m claude:x dashboard …` -> "dashboard", not "claude:x") — the same
    value-skipping walk `_extract_detach_flag` does."""
    skip_value = False
    for tok in raw:
        if tok == "--":
            return None
        if skip_value:
            skip_value = False
            continue
        if tok.startswith("-"):
            skip_value = tok in _VALUE_TAKING_OPTS
            continue
        return tok
    return None


def _reject_undetachable(raw: list[str]) -> int | None:
    """The whitelist gate for `--detach` (review round 1, GH-162, Fable #2): only a
    REVIEW MODE verb (`known_subcommands()` — diff/brainstorm/just-ask/quorum/qa/
    visual/…) may be detached. Runs on the already-NORMALIZED argv, so a leading
    global option (`review -m x dashboard run --detach`) can no longer slip a
    server subcommand past the check with `raw[0] == "-m"`. Persistent SERVER
    subcommands (`dashboard`, `spec-web`) keep their own message: ANY of their
    subactions is rejected (not just the blocking foreground server) rather than
    double-daemonized, since they already have a start/stop lifecycle. Management
    verbs (`jobs`/`status`/`wait`/`install-*`/…) are rejected too — detaching them is
    meaningless and would only pollute the jobs list. Returns the exit code to
    return, or None when `raw` is detachable."""
    verb = _first_verb(raw)
    if verb in _SERVER_SUBCOMMANDS:
        print(
            "[review-cli] --detach is not supported for dashboard/spec-web — they "
            "already have their own start/stop lifecycle (`review dashboard start`).",
            file=sys.stderr,
        )
        return 2
    # ONE verb for both checks (review round 2, GH-162, Opus #2 / Sonnet #1): the
    # whitelist used to index `raw[0]` directly, so it only agreed with the server
    # check above when normalization had already moved every leading option behind
    # the verb — a leading value-taking option normalization does not know would have
    # been rejected as "got '-o'" even though the mode itself is detachable.
    if verb is None or verb not in known_subcommands():
        modes = ", ".join(sorted(known_subcommands()))
        print(
            f"[review-cli] --detach requires a review mode as the first argument "
            f"({modes}); got {verb if verb is not None else 'nothing'!r}. Management commands "
            "(jobs/status/wait/install-*) run synchronously and cannot be detached.",
            file=sys.stderr,
        )
        return 2
    return None


def _detached_child_argv(raw: list[str], result_path: Path | None) -> list[str]:
    """`python -m reviewlib <raw>`, with a generated `-o <result_path>` (None when the
    caller passed its own `-o`) placed BEFORE a `--` end-of-options marker — appending
    after `--` would make `-o`/its value land as extra POSITIONALS, corrupting any
    invocation with its own `--` (`review just-ask --task X --detach -- --flag-like
    -question`, codex review). Built structurally from the split, not via index
    arithmetic across the two lists (Sonnet review, round 1)."""
    if result_path is None:
        return [sys.executable, "-m", "reviewlib", *raw]
    idx = raw.index("--") if "--" in raw else len(raw)
    head, tail = raw[:idx], raw[idx:]
    return [sys.executable, "-m", "reviewlib", *head, "-o", str(result_path), *tail]


def _detached_child_env(job_id: str, this_jobs_dir: Path) -> dict[str, str]:
    """The child's environment: `$REVIEW_JOB_ID` (the marker `main()`'s finalizer keys
    on), `$REVIEW_JOBS_DIR` pinned to the SAME resolved jobs dir this process used —
    without this a child that later calls `jobs.jobs_dir()` itself could independently
    recompute the last-resort `mkdtemp()` fallback and land its bookkeeping in a
    DIFFERENT directory than the parent (codex review, review-cli#162 follow-up; a
    no-op when the standard location is writable) — and `$PYTHONPATH` prefixed with
    THIS process's own resolved repo root (`_reviewlib_repo_root`) so a symlink-only
    install still finds the package."""
    env = dict(os.environ)
    env["REVIEW_JOB_ID"] = job_id
    env["REVIEW_JOBS_DIR"] = str(this_jobs_dir)
    repo_root = _reviewlib_repo_root()
    existing_pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{existing_pp}" if existing_pp else repo_root
    )
    return env


def _detached_child_stdin() -> int:
    """What the detached child gets as stdin: the parent's OWN stdin fd passed straight
    through when it is a pipe/file, `DEVNULL` when it is a tty (or absent).

    Review round 1 (GH-162, Fable #1 / Opus #2): the previous design READ stdin to EOF
    in the parent and spooled it to a file for the child. That blocked the parent —
    the very process `--detach` promises returns at once — whenever a caller's stdin
    was a non-tty pipe held OPEN with nothing in it (agent tool wrappers, pre-commit
    hooks: the feature's own audience). The synchronous path reads stdin for every
    verb without an explicit `--staged`/`--diff` source (bounded by
    `$REVIEW_STDIN_WAIT_SECONDS`, see `_read_stdin_if_piped` / `_dispatch`), so the
    only way to keep the child's input IDENTICAL to a
    synchronous run without the parent ever reading is to hand it the same fd: the
    pipe's writer stays open until the CHILD reads to EOF, the parent never touches
    it. A tty stdin is never inherited — a session-detached background process
    reading a tty would stop on SIGTTIN; the synchronous path never reads a tty
    either (`_read_stdin_if_piped`), so `DEVNULL` gives the child the same "no piped
    input" answer."""
    stdin = sys.stdin
    if stdin is None or getattr(stdin, "closed", True) or stdin.isatty():
        return subprocess.DEVNULL
    try:
        return stdin.fileno()
    except (OSError, ValueError):
        return subprocess.DEVNULL


def _spawn_detached_job(raw: list[str]) -> int:
    """Implement `--detach`: spawn a SECOND, full `review` invocation (the same argv,
    minus `--detach`) as a session-detached background process, record it as a job, and
    return almost immediately — the caller never blocks for the review itself.

    Reuses `python -m reviewlib` (rather than resolving a `review` on PATH, which can be
    a live-symlink into a DIFFERENT checkout — see `dashboard/service.py
    _review_argv0`) so the detached child is guaranteed to run the exact code this
    process is running (see `_detached_child_env` for how it finds the package). The
    child is started in its OWN session (`start_new_session=True`) — same isolation
    `_run_streamed` gives backend children — so it outlives this process's exit and is
    bounded the same way (its own `install_signal_reaper` + `run_backstop`, wired the
    moment its `main()` runs). Its stdin is the caller's own (`_detached_child_stdin`),
    so `git diff | review diff --detach` reviews the piped diff exactly like a
    synchronous run — read by the child, never by this process.

    A caller's own `-o FILE` is honored as the job's result file; otherwise a default
    result path under `jobs.jobs_dir()` is used and passed to the child so the review's
    normal `-o` machinery (including the quorum stamp) writes there. The job record is
    written TWICE and never touches `status` after the first write: (1) BEFORE `Popen`,
    with everything except `pid` — this happens-before the child can possibly exist, so
    the child's OWN terminal-status write (in `main`'s finally) can never race a "does
    this job exist yet" gap; (2) immediately after `Popen`, adding ONLY `pid` — no
    `status` key — so even if the child finishes and writes its terminal status BETWEEN
    these two writes, this second write cannot stomp it back to "running" (`jobs.
    write_job`'s read-modify-write would otherwise silently regress a `done`/`failed`
    record for a review fast enough to race its own parent).
    """
    raw = _normalize_leading_mode_options(raw)
    rejected = _reject_undetachable(raw)
    if rejected is not None:
        return rejected

    job_id = jobs.new_job_id()
    # Resolved ONCE and reused for every path below (and propagated to the child via
    # $REVIEW_JOBS_DIR) — `jobs_dir()`'s own last-resort fallback tier mints a brand-new
    # `tempfile.mkdtemp()` directory the FIRST time it's hit in this process, so calling
    # it repeatedly without pinning the result could scatter this one job's
    # result/log paths across different directories (codex review, review-cli#162
    # follow-up).
    this_jobs_dir = jobs.jobs_dir()
    user_output, _ = _extract_output_path(list(raw))
    result_path = (
        user_output
        if user_output is not None
        else this_jobs_dir / f"{job_id}.result.txt"
    )
    log_path = this_jobs_dir / f"{job_id}.log"
    child_argv = _detached_child_argv(raw, None if user_output else result_path)

    # Write the initial record BEFORE Popen (see docstring): the child cannot possibly
    # exist yet, so its own terminal-status write can never race a job that doesn't
    # exist on disk yet.
    jobs.write_job(
        job_id,
        status="running",
        mode=raw[0],
        argv=raw,
        cwd=os.getcwd(),
        log_path=str(log_path),
        result_path=str(result_path),
        started_at=time.time(),
    )

    # The log open goes through the SAME review-cli#162 sandboxed-write fallback
    # `_run_streamed`'s own log open uses — a `--detach` job's bookkeeping file is
    # exactly as likely to hit a sandboxed caller's write deny-list as the review's own
    # transcript log, and a job whose OWN log can't be opened must not silently fail to
    # even start.
    log_fd, log_path = _open_log_with_fallback(log_path)
    try:
        proc = subprocess.Popen(
            child_argv,
            stdin=_detached_child_stdin(),
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env=_detached_child_env(job_id, this_jobs_dir),
            start_new_session=True,
            **portable.new_process_group_kwargs(),  # Windows: CREATE_NEW_PROCESS_GROUP
        )
    finally:
        os.close(log_fd)

    # pid + resolved log_path update: deliberately omits `status` so it can never
    # regress a terminal status the child may have already written (see docstring).
    # `log_path` is re-sent here because `_open_log_with_fallback` above may have
    # replaced it with a fallback path AFTER the initial record (written before the
    # open was even attempted) already recorded the ORIGINAL, possibly-inaccessible
    # one — without this, `review status`/its JSON output/log-tail all pointed at a
    # path the sandboxed caller was never able to write to, even though the process
    # was logging successfully elsewhere (codex review, review-cli#162 follow-up).
    jobs.write_job(job_id, pid=proc.pid, log_path=str(log_path))

    print(f"[review-cli] detached job {job_id} started (pid {proc.pid})")
    print(f"[review-cli]   log:    {log_path}")
    print(f"[review-cli]   result: {result_path}")
    print(f"[review-cli]   check:  review status {job_id}")
    return 0


def _main_dispatch(raw: list[str]) -> int:
    """The body of a normal (non-`--detach`) `main()` call: `-o` handling, the
    reentrancy marker, the backstop, and dispatch. Split out of `main()` so a
    detached child (see `_spawn_detached_job`) can run the identical path while
    `main()` wraps it with job-status finalization."""
    output_path, raw = _extract_output_path(list(raw))
    raw = _normalize_leading_mode_options(raw)

    # A REMOVED flag (--mcp/--ln, or a removed mode flag) OR the removed `review review`
    # SUBCOMMAND verb is a USAGE error — it must behave like argparse's own usage errors
    # w.r.t. `-o`: print the structured error and exit WITHOUT writing the `-o` file.
    # Rejecting it INSIDE `_dispatch` only `return`s 2, which the tee path below treats as
    # "the dispatch completed" and would persist the (empty) captured stdout — truncating a
    # pre-existing `-o` target (codex P1/P2). Reject it here, before the tee is armed, so no
    # write happens. Both are pure argv pre-scans; the later calls in `_dispatch` are then
    # harmless no-ops.
    # _reject_if_reentrant runs FIRST: it is the cheapest check (one os.environ.get)
    # and the highest-severity failure (review-cli#180 review finding, glm-5.2) — a
    # recursive invocation that happens to also carry a removed flag/verb should get
    # the reentrancy diagnostic, not a confusing unrelated usage error.
    for _reject in (
        _reject_if_reentrant,
        _reject_removed_flags,
        _reject_removed_subcommand,
        _reject_subcommand_only_flag_without_verb,
    ):
        rejected = _reject(raw)
        if rejected is not None:
            return rejected

    os.environ[REVIEW_CLI_ACTIVE_ENV] = "1"
    try:
        return _dispatch_with_backstop(raw, output_path)
    finally:
        os.environ.pop(REVIEW_CLI_ACTIVE_ENV, None)


def _model_default_help(mode: ModeSpec | None) -> str:
    """The EFFECTIVE `--model` default for THIS parser's mode, for `--help` (ROADMAP: help
    must show ACTUAL defaults — esp `--model`). The default is MODE-AWARE because the
    runtime selection differs per mode (only the diff review runs the board):

      * diff review (mode.name == "review", and the top-level overview where mode is None):
        a `models:` list in config.yaml -> a priority roster for the failover board,
        else the active reviewer BOARD (run `review --show-board`).
      * brainstorm: a `brainstorm_models:` list -> those, else `models:`, else the built-in
        DEFAULT_MODELS — the board does NOT apply here.
      * just-ask / quorum: a `models:` list -> those, else DEFAULT_MODELS.

    Best-effort: any config read failure degrades to the built-in DEFAULT_MODELS phrasing
    (never raises in --help)."""
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 — --help must never crash on a bad config
        config = {}

    def _fmt(models: list[str]) -> str:
        shown = ", ".join(models[:4]) + (", …" if len(models) > 4 else "")
        # This string is interpolated into an argparse `help=`, where `%` is formatting
        # syntax — an un-escaped `%` in a config model id (e.g. `models: ["bad%model"]`)
        # crashes `review --help` with "badly formed help string". Config values are
        # untrusted input here, so escape `%` -> `%%` (codex review). The static phrasing
        # below has no `%`, so escaping only the config-derived fragment is sufficient.
        return shown.replace("%", "%%")

    try:
        config_models = _split_models(config.get("models") or [])
    except Exception:  # noqa: BLE001
        config_models = []
    default_models = [_expand_alias(x) for x in DEFAULT_MODELS]
    default_visual_models = [_expand_alias(x) for x in VISUAL_MODELS]

    # visual: visual_models > VISUAL_MODELS. It deliberately does NOT inherit the text
    # review board or `models:` by default; a text-only board must not accidentally serve
    # screenshot verification.
    if mode is not None and mode.name == "visual":
        try:
            visual = _split_models(config.get("visual_models") or [])
        except Exception:  # noqa: BLE001
            visual = []
        if visual:
            return f"your config.yaml visual_models: {_fmt(visual)}"
        return f"{_fmt(default_visual_models)} (the visual defaults)"

    # brainstorm: brainstorm_models > models > DEFAULT_MODELS (no board).
    if mode is not None and mode.name == "brainstorm":
        try:
            bs = _split_models(config.get("brainstorm_models") or [])
        except Exception:  # noqa: BLE001
            bs = []
        if bs:
            return f"your config.yaml brainstorm_models: {_fmt(bs)}"
        if config_models:
            return f"your config.yaml models: {_fmt(config_models)}"
        return f"{_fmt(default_models)} (the built-in defaults)"

    # qa is SINGLE-SEAT and does NOT use the panel / config `models:` / DEFAULT_MODELS — it
    # selects ONE write/exec tester (claude default, codex via REVIEW_QA_TESTER / `-m codex`).
    # So its `--model` help must NOT advertise the panel defaults (review finding).
    if mode is not None and mode.name == "qa":
        return "claude (the qa tester; use `-m codex` or REVIEW_QA_TESTER=codex for the codex seat)"

    # just-ask / quorum: models > DEFAULT_MODELS (no board).
    if mode is not None and mode.name not in ("review",):
        if config_models:
            return f"your config.yaml models: {_fmt(config_models)}"
        return f"{_fmt(default_models)} (the built-in defaults)"

    # diff review (and the top-level overview, mode is None): models priority roster >
    # config board > active/default preset board.
    if config_models:
        return f"your config.yaml models priority roster: {_fmt(config_models)}"
    if isinstance(config.get("board"), list) and config.get("board"):
        try:
            board_models = [r.model for r in load_board(config)]
        except Exception:  # noqa: BLE001 — --help must never crash on a bad board
            board_models = []
        if board_models:
            return f"your config.yaml board: {_fmt(board_models)}"
    return (
        f"the {DEFAULT_PRESET!r} preset reviewer board "
        "(run `review --show-board`; see `review help config`)"
    )


def _moderator_default_help() -> str:
    """The EFFECTIVE auto-picked `--moderator` default, for `--help`. The moderator is
    chosen from MODERATOR_CANDIDATES (opus -> codex -> gemini) filtered to availability at
    run time; the help names that priority chain so the default is concrete, not vague.

    Escape `%` -> `%%`: this is interpolated into an argparse `help=`, where `%` is
    formatting syntax. The candidates are hardcoded today (no `%`), but escaping keeps the
    same defensive guarantee as `_model_default_help` so a future `%`-bearing candidate id
    can't crash `review --help` (gemini review)."""
    return " -> ".join(MODERATOR_CANDIDATES).replace("%", "%%")


def _add_global_options(
    parser: argparse.ArgumentParser, *, mode: ModeSpec | None
) -> None:
    """Add the TRULY-GLOBAL options — the only ones the top-level `review --help` should
    list (ROADMAP "Subcommand-only options belong in the subcommand help, not the global
    list"): `-m/--model`, `-C/--cwd`, `-o/--output`, `--timeout`, `--list-defaults`,
    `--show-board`, `--pool`. These apply to every path (the meta flags + every mode), so
    they sit on the top-level parser AND every mode parser.

    Configurable options show their EFFECTIVE default value (ROADMAP "Help must show ACTUAL
    defaults"), resolving the config cascade + the mode where relevant (`--model`)."""
    parser.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help=f"model/backend to run; repeat or comma-separate (default: {_model_default_help(mode)})",
    )
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
    parser.add_argument(
        "--task",
        metavar="CODE",
        default=None,
        help=(
            "task/issue code for this review iteration (required for recorded review "
            "modes; standalone visual without diff is exempt; may also be supplied by "
            "$REVIEW_TASK_CODE)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        default=None,
        help=(
            "write the result to FILE via Python (creates parent dirs, overwrites) "
            "while still printing to stdout. Use this instead of `review … > FILE`, "
            "which fails under zsh noclobber."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "per-call timeout seconds; REST uses wall/request timeout; review/panel "
            "agent CLIs use idle/silence timeout with a 20m floor for values >=60 "
            "(set REVIEW_IDLE_TIMEOUT_SECONDS to shorten); qa/vision keep wall-clock "
            f"caps (requested defaults: review 1200, panel 240, qa {QA_TIMEOUT_DEFAULT}). "
            "An explicit `just-ask --timeout N` is instead a hard wall-clock bound: "
            "every seat still running N s after dispatch is killed, the command lists "
            "each seat and why it gave no answer, exits non-zero, and is force-exited "
            f"(124) at N + {COMMAND_TIMEOUT_GRACE_SECONDS} s at the latest"
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help=(
            "spawn this review as a session-detached background process and return "
            "almost immediately with a job-id (see `review jobs`/`review status`/"
            "`review wait`); not supported on dashboard/spec-web"
        ),
    )
    parser.add_argument(
        "--list-defaults", action="store_true", help="print default models and exit"
    )
    parser.add_argument(
        "--show-board",
        action="store_true",
        help="print the active reviewer board (model -> role, availability) plus per-role fallback model ids and exit",
    )
    if mode is None or mode.name == "review":
        parser.add_argument(
            "--preset",
            choices=preset_names(),
            default=None,
            help=(
                "diff-review preset: light = quick/cheap preflight (pool 2, medium effort; "
                "a recorded `--task` run floors that to 3 distinct roles so gh ship can pass); "
                "default = routine change review (pool 4, high effort, excludes Fable/Sol); "
                "heavy = release/risky-change review (pool 4, highest effort, excludes "
                "Fable — a confirmed near-total dispatch failure rate — but includes Sol). "
                f"If no config board/models are set, review diff uses {DEFAULT_PRESET!r}."
            ),
        )
    parser.add_argument(
        "--pool",
        type=int,
        default=None,
        metavar="N",
        help=(
            "how many of the board's seats to run (default "
            f"{preset_pool_size('default')} for default/heavy, {preset_pool_size('light')} "
            f"for light — a bare `review diff` uses {preset_pool_size(DEFAULT_PRESET)}, the "
            f"{DEFAULT_PRESET!r} preset's pool; a recorded `--task` run floors that to "
            f"{SHIP_MIN_ROLES} so gh ship can pass; {DEFAULT_POOL_SIZE} is only the fallback "
            "for a custom config `models:`/`board:` roster with no `pool:` key set); the "
            "first N seats participate, the rest are kept in reserve. The board is "
            "never off — --pool only sizes it. N<=0 means all seats. An explicit `--pool N` "
            "below 3 with `--task` still wins but warns. Ignored for exact multi-model -m; "
            "a recorded `--task -m` one-model run pads that model across the ship floor. "
            "Recorded `--task --pool 0` still pads a same-role config roster to the ship floor."
        ),
    )
    parser.add_argument(
        "--effort",
        action="append",
        default=[],
        metavar="LEVEL|PROVIDER=LEVEL",
        help=(
            "run-scoped reasoning effort, overriding each seat's config effort for THIS run. "
            "A bare level (minimal/low/medium/high/xhigh/max) applies to every seat; "
            "PROVIDER=LEVEL (e.g. codex=high, opencode=max) overrides one backend route. "
            "Repeat or comma-separate; per-provider wins over the global level. "
            "Reaches the codex, claude, opencode, and omp reasoning-effort levers plus the "
            "screenshot vision call."
        ),
    )
    # NOTE: `--retry` is NOT global — it only applies to the diff REVIEW path (the failover
    # board + the flat `-m` panel), not brainstorm/quorum/just-ask (which call run_panel and
    # never use the retry wrapper). It lives on the diff mode's own option surface
    # (modes/review.py `_add_arguments`), so the top-level help isn't padded with a no-op flag
    # (AGENTS.md: the global list is only truly-global options). codex P1 on #46.


def _add_visual_options(
    parser: argparse.ArgumentParser,
    *,
    include_visual_flag: bool = True,
    json_help: str = "emit the structured visual verdict as JSON",
) -> None:
    """Add the composable `--visual` feature flags as their own argument GROUP. These are
    SUBCOMMAND-scoped, NOT global (ROADMAP): `--visual` rides any subcommand, so they live
    on every MODE parser but must NOT clutter the top-level `review --help`. Grouping them
    makes `review <mode> --help` render them under a clear "visual verification" heading.

    `json_help` (ticket #546) lets a caller override `--json`'s help text WITHOUT adding a
    second, colliding `--json` flag: this group already sits on EVERY mode parser (diff
    review included), so `review diff`'s own `--json` (structured findings, wired in
    `modes/review.py`) reuses this SAME argparse flag rather than defining its own — see
    `_add_mode_options` below, the only caller that passes a non-default value."""
    group_title = (
        "visual verification"
        if not include_visual_flag
        else "visual verification (the composable --visual flag; rides text subcommands)"
    )
    group = parser.add_argument_group(group_title)
    if include_visual_flag:
        group.add_argument(
            "--visual",
            metavar="IMAGE",
            help='image to verify/attach; rides text subcommands (e.g. `review brainstorm "Q" --task CODE --visual IMAGE`; prefer `review visual IMAGE` for standalone)',
        )
    group.add_argument(
        "--before",
        metavar="IMAGE",
        help="baseline image for diff-aware judgement / no-effect bypass",
    )
    group.add_argument(
        "--intent",
        metavar="TEXT",
        help="free-text edit intent (untrusted; may only tighten the contract)",
    )
    group.add_argument(
        "--expect",
        metavar="KIND",
        help="expectation kind: zero-diff|move|resize|style|wrap|insert|delete|text",
    )
    group.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        help="force-activate a visual module by name (repeatable)",
    )
    group.add_argument("--json", action="store_true", help=json_help)
    group.add_argument(
        "--strict",
        action="store_true",
        help="exit 10 on a blocking visual verdict (gate use)",
    )
    group.add_argument(
        "--no-ai",
        action="store_true",
        help="run cvGate only (no vision call) — fast CI smoke / offline",
    )
    group.add_argument(
        "--no-local-model",
        action="store_true",
        help="disable the Stage-2a local pre-classifier (known-good cache cost-saver); flow = cvGate → vision (§3.1a)",
    )
    group.add_argument(
        "--vision-timeout",
        type=int,
        default=60,
        help="per vision-call timeout seconds (default 60)",
    )
    group.add_argument(
        "--project",
        default=None,
        help="project root for per-project visual modules (default --cwd)",
    )


def _add_mode_options(parser: argparse.ArgumentParser, *, mode: ModeSpec) -> None:
    """Add the full surface for an EXPLICIT `review <mode> …` subcommand parser: the global
    options, the diff-source flags, the mode-relevant flags (`--prompt` for the diff review,
    `--moderator` for the panel modes), the composable visual group, and the mode's own
    UNIQUE arguments (its positional question/topic, via `add_arguments`).

    `--diff`/`--staged` and `--prompt`/`--moderator` are scoped to the modes that use them
    (ROADMAP): `--prompt` is the diff review's prompt; `--moderator` steers quorum/brainstorm.
    They are harmless if a mode ignores them, but scoping keeps each `review <mode> --help`
    showing only what that mode actually reads."""
    _add_global_options(parser, mode=mode)

    # --diff / --staged select the diff source. They matter to the diff review (its diff is
    # required) and brainstorm/panel grounding; keep them on every mode parser (a mode that
    # ignores one is harmless) but OFF the top-level overview.
    parser.add_argument(
        "--diff",
        action="store_true",
        help="use the working-tree diff (default for the diff review; optional grounding for brainstorm)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="use the staged diff (git diff --cached) instead of the working-tree diff",
    )

    # --prompt is the DIFF REVIEW's prompt override only.
    if mode.name == "review":
        parser.add_argument(
            "--prompt", default=DEFAULT_PROMPT, help="override the diff-review prompt"
        )
    # --moderator steers the quorum / brainstorm synthesis only.
    if mode.name in ("quorum", "brainstorm"):
        parser.add_argument(
            "--moderator",
            default=None,
            help=f"moderator backend (default: auto-pick, first available of {_moderator_default_help()})",
        )

    # The composable --visual feature rides any subcommand, so its group is on every mode
    # parser (but never the top-level overview). --rounds / --max-rounds are brainstorm-only
    # and added by the brainstorm mode's own add_arguments; they stay in _VALUE_TAKING_OPTS
    # so the mode-agnostic `-o` pre-scan treats them as value-taking.
    # `--json`'s help text is mode-aware (ticket #546): `review diff` (and a `review
    # visual IMAGE --diff/--staged` companion run, which reaches the SAME handler via
    # visual.py's own `_handler = review._handler`) gives the flag a SECOND meaning —
    # a structured findings envelope, wired entirely in modes/review.py — since the
    # pre-existing "visual verdict JSON" behaviour never fires there anyway (it is
    # gated on there being NO diff at all; diff review always has one). Every other
    # mode (quorum/brainstorm/just-ask) keeps the original, unrelated help text.
    json_help = (
        "with a diff in play (review diff, or review visual IMAGE --diff/--staged): "
        "emit a structured findings JSON envelope on stdout instead of the prose "
        "panel -- additive, the prose output is unaffected when this flag is absent. "
        "Each backend is additionally asked to emit one fenced JSON block on top of "
        "its normal prose; findings[] items carry file/line/severity(P0-P3)/"
        "confidence(high|medium|low)/verdict(FINDING|CLEAR)/source (the seat that "
        "raised it), plus summary.expected/reported/degraded (which seats ran / "
        "parsed / failed to parse). Exit 0 = no findings, 1 = findings present, 2 = "
        "no seat's structured response was usable at all -- --commit's own checkpoint "
        "exit codes (11/12/13) still take priority over this 0/1/2 contract when both "
        "flags are given. For a STANDALONE `review visual IMAGE` (no diff), this "
        "instead emits the structured VISUAL verdict, unchanged from before."
        if mode.name in ("review", "visual")
        else "emit the structured visual verdict as JSON"
    )
    _add_visual_options(
        parser, include_visual_flag=(mode.name != "visual"), json_help=json_help
    )

    if mode.add_arguments is not None:
        mode.add_arguments(parser)


# Flags that live ONLY on a subcommand parser (NOT the global top-level parser), per the
# option-scoping. When one of these LEADS a no-subcommand invocation (e.g. the old pre-commit
# `review --staged`, or `review --visual shot.png`), the top-level parser would reject it with
# argparse's opaque "unrecognized arguments", losing the `review diff` migration pointer. The
# pre-parse guard below catches them and emits the friendly pointer instead.
_SUBCOMMAND_ONLY_FLAGS: frozenset[str] = frozenset(
    {
        "--diff",
        "--staged",
        "--prompt",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--json",
        "--strict",
        "--no-ai",
        "--no-local-model",
        "--vision-timeout",
        "--project",
        "--retry",
        "--commit",
        # review diff's live per-seat status TUI off-switch (modes/review.py).
        "--no-tui",
        # quorum's opt-in adversarial refutation pass (modes/quorum.py).
        "--adversarial-check",
        # The qa mode's own flags (modes/qa.py); a verb-less `review --suites …` / `--kind …`
        # etc. must get the friendly "use the subcommand" pointer, not argparse's opaque error.
        # Phase 3 adds the SUT-env flags `--stage-url` / `--config` / `--keep-env`.
        "--suites",
        "--kind",
        "--in-place",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
        "--keep-env",
    }
)

# The BARE management subcommands `_dispatch` handles directly (NOT mode verbs in
# known_subcommands(), and NOT the diff review): they have their OWN flag parsers (e.g.
# `review sessions -s <id> --diff --moderator …`). The verb-less migration guard must leave
# them alone — a `--diff`/`--moderator` after `sessions` belongs to that subparser, not a
# missing `review diff`.
_BARE_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "install-skill",
        "install-commit-hook",
        "install-hook",
        "dashboard",
        "sessions",
        "task",
        "stat",
        "entities",
        "capacity-check",
        "trust-module",
        "register-module",
        "spec-web",
        "jobs",
        "status",
        "wait",
        "seats",
        "doctor",
    }
)


def _reject_subcommand_only_flag_without_verb(argv: list[str]) -> int | None:
    """If a SUBCOMMAND-scoped flag appears with NO recognized subcommand, print the friendly
    `review diff` migration pointer + the usage code (2), instead of letting argparse reject
    it with an opaque "unrecognized arguments" that drops the pointer (codex review). Scans
    up to the first `--` (end-of-options), matching the `=value` form too. Returns 2 when such
    a flag leads a verb-less invocation, else None (a purely-global `review -C <repo>` falls
    through to the help pointer; a recognized mode OR bare management subcommand is left
    alone — those own their flags).

    Self-contained (checks the no-subcommand condition itself) so it is safe to call in
    `main()` BEFORE the `-o` tee is armed — a usage error must NOT write/truncate the `-o`
    file (like the removed-flags guards)."""
    if (
        argv
        and not argv[0].startswith("-")
        and (argv[0] in known_subcommands() or argv[0] in _BARE_SUBCOMMANDS)
    ):
        return None  # a recognized subcommand (mode or management) owns these flags
    for tok in argv:
        if tok == "--":
            break
        if tok.split("=", 1)[0] in _SUBCOMMAND_ONLY_FLAGS:
            use_line = (
                "  use:  review visual IMAGE [--diff] [options]\n"
                if tok.split("=", 1)[0] == "--visual"
                else "  use:  review diff --task CODE [options]   "
                "(e.g. `review diff --staged --task CODE`)\n"
            )
            print(
                "review: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one), and that flag belongs to a "
                "subcommand.\n"
                f"{use_line}"
                "  (run `review --help` for all subcommands)",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return None


def _help_topic_config() -> str:
    """The `review help config` deep reference (ROADMAP "Topic-based help across the
    ecosystem"): config file path + cascade, the keys, key/auth env vars, and how the
    reviewer board + model selection resolve. Kept in sync with config.py / backends.py
    behavior (help-docs-sync); a flag/behavior change updates this topic in the same commit.

    NOTE: the "REVIEWER BOARD + PRESETS" section below derives its stated effort from
    `preset_board(DEFAULT_PRESET)[0].effort` — correct only because DEFAULT_PRESET's board
    has UNIFORM effort across all seats (true for "light"/"default", not for "heavy",
    which mixes xhigh/max). If DEFAULT_PRESET ever becomes "heavy", that line needs a
    real fix, not just re-pointing the same expression."""
    adm = admission.Settings()
    return f"""\
review — configuration reference
================================

CONFIG FILE
  {CONFIG_PATH}
  Optional YAML. Absent / unparseable -> the built-in defaults apply (never an error).
  Keys:
    models:            list[str]  priority roster for `review diff`: the live pool is
                       selected from this full ordered set and the rest are reserve.
                       For just-ask/quorum it remains the flat default panel.
    brainstorm_models: list[str]  default panel for `review brainstorm` (falls back to
                       `models:`, then the built-in defaults). Unreachable backends are
                       dropped gracefully.
    visual_models:     list[str]  ordered vision backends for `review visual` and companion
                       visual fan-out. Separate from the text reviewer board; runtime
                       failures skip to the next vision backend.
    board:             list[seat]  override the built-in reviewer board (see BOARD below).
    unpaid_providers:  list[str]   providers whose billing/subscription is currently
                       unavailable; every direct or oc: seat under them is skipped before
                       any backend process/API call. Env equivalent:
                       REVIEW_UNPAID_PROVIDERS=commandcode,fireworks.
    omp_bin:           str         the omp executable omp: seats run instead of the `omp` on
                       PATH. Rarely needed: a wrapper `omp` script's real binary is found
                       before the seat's sandbox replaces HOME (OMP_REAL_BIN, PI_INSTALL_DIR,
                       ~/.local/lib/omp/omp) and handed to it as OMP_REAL_BIN. Env
                       equivalent: REVIEW_OMP_BIN=/path/to/omp.
    omp_accounts:      mapping     which of omp's Anthropic accounts omp:anthropic/* seats may
                       take (`review seats` shows the policy and each account's reason):
                         exclude: [you@example.com]   never used (no token is fetched)
                         max_usage_percent: 90        an account with an `omp usage` window
                                                      at or above it waits for that window's
                                                      reset (a model-tier window, e.g.
                                                      Fable's, counts only for that tier);
                                                      unknown usage does not skip
                         accounts: {{you@example.com: {{max_usage_percent: 95}}}}
                                                      per-account threshold, wins over the
                                                      global one
                       Every account excluded or over its threshold -> the seat fails naming
                       each account and why (`review seats` shows the seat UNUSABLE); an
                       invalid value fails the seat with the problem (never ignored), and so
                       does a policy together with $REVIEW_OMP_CLAUDE_PROFILES (a profile's
                       account is unknown) or with no omp account list to apply it to. While
                       `exclude` is set, an account without a known email is skipped. Env
                       overrides, replacing the config value (which is then not checked):
                       REVIEW_OMP_ACCOUNTS_EXCLUDE=a@x.com,b@y.com (at least one email)
                       replaces `exclude`, REVIEW_OMP_MAX_USAGE_PERCENT=90 (not empty)
                       `max_usage_percent`.
    timeout:           int         per-call timeout seconds for `review diff` (replaces the
                       configured/default request timeout; still overrideable
                       per-invocation by --timeout). Review/panel agent CLI backends treat
                       this as an idle/silence timeout with a 20m default floor for normal
                       review runs; REST backends use it as their HTTP request timeout.
                       `review qa` uses its timeout as a
                       wall-clock QA cost cap.
    admission:         mapping     host-wide parallelism budget (headroom, per-seat cost,
                       min/max slots, per-provider limits, ...). See HOST-WIDE ADMISSION
                       below for every key, its REVIEW_ADMISSION_* env var and default.
  Model ids accept the friendly aliases. Generic names follow the current pin
  (`opus` -> claude:claude-opus-5-5, `sonnet` -> claude:claude-sonnet-5-5,
  `fable` -> claude:claude-fable-5-1, `sol` -> codex:gpt-6.1-sol, `luna` -> codex:gpt-6-luna,
  `glm` -> zai:glm-5.3, `cc` -> commandcode, `grok`/`grok47`/`grok-4.7` -> oc:xai/grok-4.7,
  `mimo`/`mimo26` -> omp:opencode-go/mimo-v2.6-pro). Version-numbered aliases keep their
  own release: `opus48` -> claude:claude-opus-4-8, `fable5` -> claude:claude-fable-5,
  `glm52` -> zai:glm-5.2, `grok46`/`grok-4.6` -> oc:xai/grok-4.6,
  `grok45` -> oc:xai/grok-4.5, `mimo25`/`mimo-2.5` -> omp:opencode-go/mimo-v2.5,
  `terra`/`gpt56terra` -> codex:gpt-5.6-terra.

SELECTION CASCADE, by mode:
  review diff           : explicit -m requested models  >  explicit --preset  >
                          config `models:` priority roster  >  config `board:`  >
                          default preset.
                          With configured `models:`/`board:` metadata, -m narrows that
                          board metadata to only the requested models; without config it is
                          the legacy flat exact panel unless an explicit preset supplies
                          metadata/effort for the requested models.
  review visual         : explicit -m  >  `visual_models:`  >  visual defaults.
  review brainstorm     : explicit -m  >  `brainstorm_models:`  >  `models:`  >  defaults.
  review just-ask/quorum: explicit -m  >  `models:`  >  the built-in defaults.
  review qa             : IGNORES config `models:` / `brainstorm_models:` / the defaults.
                          It picks ONE write/exec tester: `REVIEW_QA_TESTER=claude|codex`  >
                          a bare `-m claude|codex`  >  the `claude` default. (opencode is not
                          in v1.) The panel/board cascade does not apply to qa.
  Built-in defaults: {", ".join(_expand_alias(x) for x in DEFAULT_MODELS)}.
  Visual defaults:  {", ".join(_expand_alias(x) for x in VISUAL_MODELS)}.
  See the live default for each subcommand in `review <mode> --help` (the --model line).

REVIEWER BOARD + PRESETS (the diff-review default; `review --show-board` prints it live)
  A priority-ordered panel of seats, each model carrying its own
  role/lens. The listing also prints per-role fallback model ids (bare names; harness auto-resolved). A plain `review diff` runs the `{DEFAULT_PRESET}` preset: pool
  {preset_pool_size(DEFAULT_PRESET)}, {preset_board(DEFAULT_PRESET)[0].effort} effort, without
  Fable/Sol. A recorded `review diff --task CODE` floors that pool to {SHIP_MIN_ROLES}
  distinct roles so `review task CODE --check` / gh ship can pass from one run; explicit
  `--pool N` still wins (warns if N<{SHIP_MIN_ROLES}). `-m one-model` with `--task` pads
  that model across {SHIP_MIN_ROLES} roles for review, quorum, and brainstorm (quorum and
  brainstorm announce the role slots on stderr, one call each); just-ask never pads — one
  `-m` model is one call.
  Use `--preset default` for a routine change
  review (pool 4, high effort) or `--preset heavy` for release/risky changes
  (Sol/Opus/GLM-cc/Kimi at highest effort; Fable is excluded from every preset — a
  confirmed ~100% dispatch failure rate, see DEFAULT_BOARD's own comment — and sits
  last-resort in the raw board instead). `--pool N` sizes the selected board (`--pool 0`
  = all available; recorded `--task --pool 0` still pads a same-role config roster
  to the ship floor). Explicit -m never lets config add extra seats; it narrows
  configured metadata when present, else uses the flat exact panel (unless `--task`
  pads a single model). To set the priority
  roster, configure `models:`. To add role/name/effort metadata (or a full board when
  `models:` is absent), set `board:` in config.yaml (a list of {{model, role, name,
  effort}} entries; `name` is the display label, `role` is a key into the built-in role
  lenses, and `effort` is one of minimal/low/medium/high/xhigh/max) or edit DEFAULT_BOARD in
  reviewlib/config.py.

KEYS / AUTH (resolved from the process env first, then the shared .env)
  Shared .env:  ~/.config/review-cli/.env   (override with $GEMINI_ENV_FILE)
  Env vars:
    GEMINI_API_KEY (or GOOGLE_API_KEY)  — gemini seat.
    ZAI_API_KEY    (or ZHIPU_API_KEY)   — z.ai / GLM: the diff-only zai: seat, and omp:zai/* seats when omp stores no zai credential.
    COMMANDCODE_API_KEY                 — commandcode seats (a `user_...` token ONLY).
    OPENROUTER_API_KEY                  — diff-only OpenRouter aggregator seat (`-m openrouter:<model>`).
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN (+ optional ANTHROPIC_BASE_URL) — claude API.
    REVIEW_CLAUDE_MODE=api|cli, REVIEW_<BACKEND>_MODE=api|cli — transport selection.
    REVIEW_UNPAID_PROVIDERS=provider[,provider...] — skip disabled/unpaid providers early.
    REVIEW_IDLE_TIMEOUT_SECONDS=N       — override the review/panel subprocess idle window;
                                          0 disables idle reap and uses wall-clock --timeout.
    --timeout applies to every backend seat. For review/panel subprocess agent CLIs, backend
      stdout/stderr resets the idle timer; a fully silent seat is reaped after the idle
      window. Values under 60s stay exact for tests/probes; otherwise the normal 20m floor
      applies unless REVIEW_IDLE_TIMEOUT_SECONDS is set. QA and vision calls keep wall-clock
      timeout caps. An explicit `review just-ask --timeout N` is a hard wall-clock bound
      instead: seats still running N s after dispatch are killed (whole process group), the
      command prints each seat with why it gave no answer (timeout + last output, quota, no
      output, failed) and exits 1; if it is somehow still alive at N + {COMMAND_TIMEOUT_GRACE_SECONDS} s,
      a watchdog prints the seats still running with their last activity, kills them and
      exits 124.
    REVIEW_HEARTBEAT_SECONDS=N          — interval between "still waiting" heartbeat
                                          lines printed to stderr once a backend call
                                          has SPAWNED (not while still queued on
                                          REVIEW_MAX_CONCURRENCY), for every STREAMED
                                          backend call (_run_streamed — review/panel
                                          seat dispatch) regardless of mode (default
                                          60s; a non-finite value, e.g. nan/inf, falls
                                          back to the default; ANY value <= 0, not just
                                          a literal 0, disables heartbeats entirely; a
                                          positive value below the 0.5s poll
                                          granularity clamps up to it).
    REVIEW_DIFF_MAX_BYTES=N             — dispatch-time diff-size cap (default 300000);
                                          <= 0 disables it. See the bold "Diff size cap"
                                          paragraph in README.md's "Diff review" section.
    REVIEW_STDIN_WAIT_SECONDS=N         — idle timeout for a non-tty stdin (default 5). A
                                          verb with no explicit diff source (`git diff |
                                          review diff`) reads stdin to EOF but gives up
                                          once it stays silent for N s (before the first
                                          byte or mid-stream): one stderr line, then the
                                          repo diff as usual. `--staged` / `--diff` never
                                          read stdin (a diff piped in anyway is ignored
                                          with a notice). 0 = only data already waiting.
    REVIEW_SEAT_COOLDOWN_SECONDS=N      — cross-invocation cooldown window for a
                                          chronically-unavailable claude seat (Fable).
                                          opencode also consults + records + clears
                                          this store for a TRUE-SILENCE trip
                                          (review-cli#235, see
                                          REVIEW_TRUE_SILENCE_SECONDS below), AND for
                                          the SAME shared administrative-sentinel/
                                          quota-marker phrases claude's own detection
                                          uses (codex review finding, round 18: an
                                          earlier version of this text wrongly said
                                          that detection "remains claude-only" — it
                                          does not, as of #243's round-12 fix).
                                          opencode-SPECIFIC quota wording that matches
                                          none of the shared phrases is still not
                                          recognised (falls through as a genuine
                                          success) — see review-cli#226.
                                          commandcode/zai HTTP backends have no
                                          cooldown consult/record/clear at all yet.
                                          Unset: the window
                                          ESCALATES per consecutive failure (10min,
                                          30min, 2h, then 8h cap), resetting to 10min
                                          after a success or a 24h quiet period. Set:
                                          that fixed window every time, no escalation;
                                          <= 0 disables cooldowns entirely.
    REVIEW_SEAT_COOLDOWN_FILE=PATH      — override the cooldown store location (default
                                          ~/.config/review-cli/seat-cooldown.json).
    REVIEW_TRIVIAL_DELTA_LINES=N        — pre-commit gate: max changed lines tolerated
                                          against the last reviewed baseline before a
                                          restage forces a fresh full review (default 10);
                                          0 disables the tolerance (exact-hash match only).
                                          Fails CLOSED regardless of line count whenever
                                          either diff contains a `Binary files ... differ`
                                          or `Subproject commit <sha>` line — binary /
                                          submodule-gitlink content is unmeasurable by a
                                          line-count ruler, so it always forces a fresh
                                          full review.
    REVIEW_TRUE_SILENCE_SECONDS=N       — how many seconds of ZERO output an opencode
                                          seat gets before it is reaped as stuck rather
                                          than silently thinking (default 5min, per-model
                                          overridable in reviewlib/model_behavior.py's
                                          registry; review-cli#235). A trip records a
                                          REVIEW_SEAT_COOLDOWN_SECONDS-governed cooldown
                                          for that model. <= 0 disables the check
                                          entirely (mirrors REVIEW_IDLE_TIMEOUT_SECONDS=0).
                                          Only applies before the call's FIRST byte of
                                          output ever arrives — see review-cli#239 for
                                          the currently-uncovered mid-call-silence case.
                                          Before the first byte, this REPLACES
                                          REVIEW_IDLE_TIMEOUT_SECONDS as the sole reap
                                          authority (not a min() of the two) — an
                                          operator who tightens the idle timeout below
                                          this value does NOT get an earlier pre-output
                                          reap. There is no way to COMBINE the two for
                                          an earlier pre-first-byte cutoff: for any
                                          positive REVIEW_IDLE_TIMEOUT_SECONDS value,
                                          it has NO effect before the first byte, no
                                          matter what it is set to — the only lever
                                          pre-first-byte is lowering
                                          REVIEW_TRUE_SILENCE_SECONDS itself (codex
                                          review finding, review-cli#243 round 15: an
                                          earlier version of this text wrongly implied
                                          setting both env vars together could make a
                                          short idle floor also apply pre-output; it
                                          cannot — see review-cli#254 for the related
                                          deadline-clamp discussion). One EXCEPTION
                                          (codex review finding, round 18):
                                          REVIEW_IDLE_TIMEOUT_SECONDS=0 (the explicit
                                          "disable idle reap entirely" value) also
                                          disables true-silence — it is not just "no
                                          effect", it turns the check OFF, since the
                                          true-silence poll branch only runs at all
                                          when idle reap is enabled.
  codex / opencode / omp carry their own CLI auth (no key here). The Grok/xai board seat
    (GROK_SEAT) is one of these: opencode's own XAI_API_KEY env var, an inline
    options.apiKey in opencode.json, or `opencode providers login` (OAuth) all work.
    review-cli reads the local auth files to PROBE non-empty-ness at startup (deciding
    seat availability) but never logs or transmits the credential value anywhere —
    opencode is what actually sends it, at call time.
  AGENTIC board seats authenticate via the AGENT's own config, not a review-cli env var:
    Kimi/Qwen/DeepSeek (oc:commandcode/...) and Grok (oc:xai/...)
    all go through opencode's OWN provider config (`opencode auth login` /
    `opencode providers login`; ~/.config/opencode/opencode.json) — COMMANDCODE_API_KEY
    and ZAI_API_KEY above do NOT cover them; COMMANDCODE_API_KEY only gates the diff-only
    `-m cc` path. omp seats (omp:<provider>/<model>, incl. MIMO_SEAT
    omp:opencode-go/mimo-v2.6-pro and the GLM seat omp:zai/glm-5.3) authenticate via
    `omp setup` (`omp token <provider>` shows the stored credential); the startup probe
    reads omp's auth db read-only. Exception: omp:zai/* and omp:openrouter/* seats also
    run on ZAI_API_KEY (or ZHIPU_API_KEY) / OPENROUTER_API_KEY from the env or
    ~/.config/review-cli/.env, which review-cli hands the seat (review-cli#646), so
    `-m omp:zai/<model>` is an agentic GLM seat on the same key as `-m glm`.
    `review seats` lists every login it found per harness. Either way a missing binary
    or missing provider auth is caught at
    startup and the seat is skipped gracefully (pool backfills from reserve) rather than
    failing the run.
  z.ai / GLM Coding-Plan endpoint: `glm-5.3` (the flagship) answers on the
    Coding-Plan base URL https://api.z.ai/api/coding/paas/v4 (review's default); the
    standard https://api.z.ai/api/paas/v4 endpoint topped out at `glm-5.1`. Override with
    ZAI_BASE_URL for a standard-plan key; an explicit `zai:<model>` suffix wins over
    ZAI_MODEL, which alone only affects a bare `-m zai` invocation.
  OPENROUTER_API_KEY (an sk-or-v1-... token): diff-only `-m openrouter:<model>`
    aggregator backend (400+ models behind one key, api-only, no CLI mode). A bare
    `-m openrouter` uses OpenRouter's own auto-router, overridable with
    OPENROUTER_MODEL; base URL override is OPENROUTER_BASE_URL (default
    https://openrouter.ai/api/v1). The OPTIONAL OPENROUTER_HTTP_REFERER /
    OPENROUTER_X_TITLE env vars set OpenRouter's leaderboard-attribution headers
    (HTTP-Referer / X-Title) and never affect the review; a value containing a
    control character is silently dropped rather than sent.

HOST-WIDE ADMISSION (parallel reviews up to machine + provider capacity, review-cli#614)
  Every review run (diff/visual/brainstorm/just-ask/quorum/qa) takes a host-wide slot
  before its seats dispatch; there is no fixed slot count and no external lock to add
  (old `flock /tmp/review-cli.lock review ...` wrappers still work, they just serialize
  themselves). A run is admitted, oldest waiter first (FIFO), only while
    load average(1m) + estimates of runs admitted < ramp_seconds ago + this run's estimate
      <= (1 - headroom) x CPU count, and
    (MemTotal - MemAvailable) + the same estimates <= (1 - headroom) x MemTotal, and
    memory-stall pressure (Linux PSI `some avg10`, /proc/pressure/memory) <=
      max_memory_pressure percent (0 = no pressure check). Swap usage alone is not a
      signal (stale swap pages linger for hours); real shortage shows in MemAvailable/PSI.
  A run's estimate is base + per_seat x seats; per_seat is learned (p75 of the last
  learn_window runs' measured process-tree peak RSS / avg CPU) once 3+ runs exist, else
  the configured defaults. min_slots runs always start; max_slots (0 = none) caps them.
  Each seat call also holds a slot of its provider (anthropic, openai, zai, commandcode,
  xai, opencode-go, google, ...): at most floor(max_concurrent x (1 - headroom)) (>= 1)
  at once and, with rpm set, floor(rpm x (1 - headroom)) starts per minute. A 429 /
  overload reply lowers that provider's limit to floor(current x throttle_backoff) for
  throttle_cooldown_seconds. A seat still waiting after provider_wait_seconds (0 = wait
  forever) is skipped as unavailable, so failover moves to the next provider/reserve. A
  run is also held back until every provider it needs has room for its planned seats next
  to the seats of runs already running, so admitted runs rarely wait on a provider.
  Leases live in the registry dir as flock-held files: a killed holder is reclaimed at
  once, a lease whose heartbeat is older than stale_lease_seconds stops counting. The
  shared registry.lock is held only for each admission decision; a holder stopped or
  gone for over lock_stale_seconds has its lock replaced (logged loudly), and a waiter
  still blocked after lock_wait_seconds runs without host-wide admission. While
  queued, stderr shows `[review] admission: waiting — queue position P/N, ... ETA ~T`;
  queue time is excluded from the run's stats and from the internal backstop, but not
  from an explicit just-ask --timeout N: a run still queued at N exits 124 without
  starting a seat. A --visual companion is admitted against its full text panel, not
  just the vision call. `review jobs` (or `review jobs --slots [--json]`)
  and bare `review status` list running/waiting reviews, the budget and provider seats,
  plus the last 24h of admissions (admitted, queued, queue wait p50/p90/max, peak running
  at once) from admissions.jsonl in the registry dir.
  config.yaml `admission:` key / env var (env > config > default):
    enabled                   REVIEW_ADMISSION                        default {adm.enabled}
    headroom                  REVIEW_ADMISSION_HEADROOM               default {adm.headroom}
    min_slots                 REVIEW_ADMISSION_MIN_SLOTS              default {adm.min_slots}
    max_slots                 REVIEW_ADMISSION_MAX_SLOTS              default {adm.max_slots} (none)
    cpu_per_seat              REVIEW_ADMISSION_CPU_PER_SEAT           default {adm.cpu_per_seat} cores
    mem_per_seat_mb           REVIEW_ADMISSION_MEM_PER_SEAT_MB        default {adm.mem_per_seat_mb}
    cpu_base                  REVIEW_ADMISSION_CPU_BASE               default {adm.cpu_base} cores
    mem_base_mb               REVIEW_ADMISSION_MEM_BASE_MB            default {adm.mem_base_mb}
    learn                     REVIEW_ADMISSION_LEARN                  default {adm.learn}
    learn_window              REVIEW_ADMISSION_LEARN_WINDOW           default {adm.learn_window} runs
    ramp_seconds              REVIEW_ADMISSION_RAMP_SECONDS           default {adm.ramp_seconds}
    poll_seconds              REVIEW_ADMISSION_POLL_SECONDS           default {adm.poll_seconds}
    stale_lease_seconds       REVIEW_ADMISSION_STALE_SECONDS          default {adm.stale_lease_seconds}
    lock_wait_seconds         REVIEW_ADMISSION_LOCK_WAIT_SECONDS      default {adm.lock_wait_seconds:g}
    lock_stale_seconds        REVIEW_ADMISSION_LOCK_STALE_SECONDS     default {adm.lock_stale_seconds:g}
    provider_max_concurrent   REVIEW_ADMISSION_PROVIDER_MAX           default {adm.provider_max_concurrent} (0 = unlimited)
    provider_wait_seconds     REVIEW_ADMISSION_PROVIDER_WAIT_SECONDS  default {adm.provider_wait_seconds} (0 = wait forever; queue time never counts toward the backstop)
    throttle_backoff          REVIEW_ADMISSION_THROTTLE_BACKOFF       default {adm.throttle_backoff}
    throttle_cooldown_seconds REVIEW_ADMISSION_THROTTLE_COOLDOWN_SECONDS default {adm.throttle_cooldown_seconds}
    max_memory_pressure       REVIEW_ADMISSION_MAX_MEMORY_PRESSURE    default {adm.max_memory_pressure:g} (% PSI some avg10; 0 = off)
    providers                 REVIEW_ADMISSION_PROVIDERS="anthropic=12,zai=4/60" (name=max[/rpm])
    dir                       REVIEW_ADMISSION_DIR                    default $XDG_STATE_HOME/review-cli/admission
  Example:
    admission:
      headroom: 0.2
      max_slots: 12
      providers:
        anthropic: {{max_concurrent: 12}}
        zai: {{max_concurrent: 4, rpm: 60}}
  REVIEW_MAX_CONCURRENCY (default 4) is separate: it caps backend subprocesses INSIDE one
  run.

See also: `review --help` (overview), `review --show-board`, `review <mode> --help`.
"""


def _help_topic_task_check() -> str:
    """The `review help task-check` deep reference (review-cli#552-PR3): the FULL
    `review task CODE --check [--json]` contract, field by field. Extracted out of the
    `--check` flag's own `help=` string (which stayed a summary + pointer here, matching
    the config/board topics' pattern) because the field-by-field breakdown alone is ~2KB
    -- inline it and `review task --help` buries `--detail`/`--min-iter` under it. Kept
    in sync with reviewlib.stats.quorum_check / `_finalize_quorum_result` (the actual
    payload builder) and `_quorum_check_subcommand` (the CLI wrapper that adds
    `identity_verification`) — a flag/behavior change there updates this topic in the
    same commit (help-docs-sync); `tests/test_run_stats.py`'s
    `test_check_help_text_names_every_json_contract_field_review_cli_552` asserts every
    field name `quorum_check` can actually emit is named somewhere below, so the two
    can't silently drift apart again."""
    return """\
review — `task CODE --check [--json]` contract reference
==========================================================

WHAT --check DECIDES
  Exit 0 iff CODE has enough PASSED recorded iterations across enough distinct board
  roles (self-merge-authority gate, default) or distinct models (when --min-models is
  explicitly given); see --min-iter/--min-models/--min-roles on `review task --help`.
  If the task has ZERO recorded role data at all AND every in-scope (non-mismatched)
  iteration predates role-tracking (PR #246), the default falls back to the old
  distinct-model count instead of a guaranteed fail (see quorum_mode_fallback below). A
  task with at least one iteration recorded AFTER role-tracking became possible but
  reviewed only via quorum/just-ask/brainstorm/qa does NOT get that fallback
  (review-cli#252) — it stays role-based and fails (see role_tracking_gap below). Counts
  only iterations whose run came back clean — a review that ran but failed/degraded does
  not count toward the bar, and pre-verdict-field history never satisfies it either
  (fail-closed).

--JSON FIELDS  (quorum_check()'s full result plus identity_verification — not just the
  printed ratio; a caller like `gh ship` should parse this, not the text-mode ratio line)

  ALWAYS present:
    task_code, passed_iterations, total_iterations, distinct_models_passed, models,
    min_iter, passed, identity_verification (ran / repo_only / disabled /
    skipped_unresolvable — see -C/--verify-repo-only/--no-verify-identity on
    `review task --help`).

  MODEL FLOOR — present whenever a model floor governs, explicitly OR via the
  zero-role-data fallback:
    min_models, min_models_source (explicit / fallback).
    min_models_advisory — ONLY when --min-models was given EXPLICITLY (never on the
    fallback path — the advisory text would be actively wrong there, since role-based
    coverage is exactly what's unavailable when the fallback fires).
    min_roles_suggestion — ONLY on an explicit, unmet --min-models run (--min-roles NOT
    given) whose PASSED iterations actually carry enough role data to have cleared the
    same floor via --min-roles instead — a non-blocking models->roles nudge, not a
    fallback/gap diagnostic (it never co-occurs with quorum_mode_fallback/
    role_tracking_gap below, since both of those require the bare role-based default,
    not an explicit --min-models run).

  ROLE MODE — present whenever role mode governs (the default):
    distinct_roles_passed, roles, min_roles.

  FALLBACK / GAP DIAGNOSTICS — mutually exclusive, present only when the matching case
  above fired (both require the bare role-based default -- neither can co-occur with an
  explicit --min-models run):
    quorum_mode_fallback — the role-based default fell back to model-counting.
    role_tracking_gap — it did NOT fall back and stays role-based, failing on its own
    merits (empty/short `roles`).

  STALLED SEATS — present only when the bar is NOT met:
    stalled_models — list of {model, reason, remaining_seconds,
    consecutive_failures} naming any attempted model currently cooling down.

  IDENTITY VERIFICATION — present only when identity_verification is "ran" or
  "repo_only" (a repo_id resolved; see -C/--verify-repo-only/--no-verify-identity).
  Repository ids are normalized remotes; a repository's former ids (a rename or a
  host move, e.g. sme-archiving-gc-tmp on Forgejo -> sme-archiving-gc on GitHub)
  count as the same repository:
    verified_iterations, unverifiable_iterations, excluded_mismatched_iterations,
    mismatch_details — list of {iteration, reason, recorded_repo_id,
    recorded_diff_files, ts} for excluded PASSED iterations, capped
    (mismatch_details_truncated when capped).
    excluded_foreign_repo_iterations, foreign_repo_iterations — list of {iteration,
    repo_id, task_code, ts, passed} for every iteration of ANY verdict recorded in
    another repository (none of them count toward iterations, models or roles),
    capped (foreign_repo_iterations_truncated when capped).

  NEAR-MISS TWIN KEY — present only when the bar is NOT met AND a same-digits
  different-'#' twin task code exists in the store (review-cli#389):
    searched_key, near_miss_keys, near_miss_counted, near_miss_ever_passed.

  error — present on every fail-closed shape: an invalid task code, an
  unreadable/missing store, zero recorded iterations, an explicit --min-iter /
  --min-models / --min-roles floor below 1, or a bar-not-met denial where some history
  was excluded as mismatched.

See also: `review task --help` (the flag surface), `review help config`.
"""


def _help_topic_runtime() -> str:
    """The `review help runtime` deep reference (help-docs-sync): run stats and the
    startup ETA line printed before a dispatch, plus why `review` must never be
    wrapped in a short external timeout. Content moved here from the README's
    "Quorum" section (review-cli#552 PR1); body prose em dashes were normalized to
    `--` for plain-text help — the title and the literal ETA example line keep the
    real `—` they use on-screen/in `eta_line()`'s actual output, so this stays a
    live, test-checked contract (tests/test_topic_help.py), not just relocated
    prose. The schema-version line below reads `STATS_VERSION` directly so it
    cannot silently go stale the way the README prose once did. Kept in sync with
    `reviewlib/stats.py`'s `record_run` / `announce_eta` / `eta_line` behavior — a
    flag/behavior change there updates this topic in the same commit."""
    return f"""\
review — runtime, run stats & the startup ETA
===============================================

NEVER SHORT-TIMEOUT `review`
  `review` is multi-model and (for the panel modes) multi-round, so it takes MINUTES
  per run. A short shell `timeout` wrapped around it kills the run before its
  synthesis -- a `brainstorm` only emits its final answer at the very end, so a
  short cap yields *nothing* usable.

RUN STATS & THE STARTUP ETA
  To make the expected duration visible up front, every run that actually dispatches
  a backend appends a structured stat record -- task code, mode, pool size (backends
  actually dispatched; for a small-panel brainstorm that is the per-round persona
  slot count), model NAMES (never keys or prompts), the real monotonic wall-clock,
  and ok/fail counts -- to an append-only JSONL store at
  ~/.config/review-cli/run-stats.jsonl (mode 0600; override with $REVIEW_STATS_FILE).

  At dispatch it then prints a one-line ETA to stderr keyed on (mode, pool_size):

    [review] pool=4 (brainstorm) — typically ~6m12s based on 12 past runs of this size; do NOT timeout.

  With no exact history it falls back to pool-size-only across modes, then to a
  "no history yet ... expect MINUTES" line. Read that line and wait at least that
  long. (This store is separate from the dashboard's per-call log reader, whose mode
  is INFERRED and whose duration is an mtime proxy -- this one records the run's
  ground truth.) Task-coded records power `review task CODE` and the dashboard's
  task history.

  The run-stats JSONL schema is versioned (currently v: {STATS_VERSION}). v: 3 added
  an optional passed: bool verdict field (the mode's own success/failure signal, used
  by `review task CODE --check` -- the self-merge-authority gate); v: 4 added
  repo_id/diff_files/diff_sha256 (diff-identity binding); v: 5 added
  prompt_tokens/output_tokens (aggregate REST token usage). Task-coded records also
  add an optional task_code field. A record with no `passed` key has verdict UNKNOWN
  -- either it predates v: 3, or it is a current record from a mode with no verdict
  to thread (`qa` is report-only by design; see `reviewlib/qa/executor.py`) -- never
  crashes a reader, but the quorum gate treats unknown as not-passed either way.

See also: `review --help` (overview), README's "Quorum" section, `review task CODE`.
"""


def _help_topic_capacity() -> str:
    """The `review help capacity` deep reference (help-docs-sync): the
    `review capacity-check` CLI/JSON contract and its evidence predicate (review-cli#472).
    Keep in sync with `reviewlib/capacity_check.py`."""
    from .capacity_check import (
        DEFAULT_MAX_AGE_SECONDS,
        REASONS,
        SCHEMA,
        SCHEMA_VERSION,
    )

    reasons = "\n".join(
        f"    {reason:<28} -> {status}" for reason, status in REASONS.items()
    )
    return f"""\
review capacity-check — is the active review board's whole capacity out of quota?

  review capacity-check                  text report, exit 1 only if the board can't resolve
  review capacity-check --json           JSON report, always exit 0
  review capacity-check --preset heavy   evaluate the board `review diff --preset heavy` runs

`gh ship` (agent-tools ci/ship/ship.sh) skips its manual review-hatch prompt only when
`--json` says `"all_exhausted": true`. Nothing else in the report authorizes anything.

WHAT IS EVALUATED
  The board a no-`-m` `review diff` would run: --preset > config `models:` > config
  `board:` > the default preset. Every seat counts (pool and reserve), each seat model
  expands to the routes its provider failover dispatches (Claude CLI <-> omp twins
  included), a Claude CLI route expands to one candidate per account it rotates
  through, and an omp seat on omp's Anthropic accounts to one per account its pool may
  dispatch on (omp's enabled Anthropic logins minus the `omp_accounts:` policy's
  excluded ones; no agent.db: the route itself). A candidate is one
  (route, account).

THE PREDICATE
  all_exhausted = at least one candidate AND every candidate has status `exhausted`.
  A candidate is `exhausted` only on one of:
    - a harness-cli limit card for ITS quota identity (the Claude or omp Anthropic
      account's own card, matched by email; the codex card) with a 5-hour/weekly
      window at >= 100% that has not reset, read within the freshness window;
    - an active seat cooldown whose typed cause is `quota` (a session-limit /
      usage-credits failure), recorded within the freshness window, on a route whose
      quota identity is the route itself or its only account, or under one omp pool
      account's own `omp-account:` key.
  Checked in this order, first decisive answer wins: backend not runnable
  (unavailable) -> the omp account policy leaves an omp Anthropic seat no account
  (unavailable) -> the account's recorded login failure (unavailable, cause auth) ->
  an active cooldown with a non-quota cause (unavailable) -> harness card (exhausted /
  usable) -> quota cooldown (exhausted) -> unknown.
  Freshness window: {DEFAULT_MAX_AGE_SECONDS}s, override with
  $REVIEW_CAPACITY_MAX_AGE_SECONDS.

  Statuses are separate: `usable` needs a card showing usage below 100% for that
  identity; no evidence is `unknown`, never usable. Typed reasons:
{reasons}

JSON (schema "{SCHEMA}", schema_version {SCHEMA_VERSION})
  schema, schema_version   identify this contract; a consumer refuses other versions
  checked_at               when the report was built (UTC ISO-8601)
  max_age_seconds          the freshness window applied
  configuration            source ("preset:light", "config.yaml (models:)", ...), preset,
                           config_path, config_present, board_fingerprint
                           ("sha256:<16 hex>" over the seats), seats [{{model, role,
                           display, effort}}]; null when the board failed to resolve
  sources                  harness_limits {{status ok|missing|failed|malformed, origin,
                           observed_at, cards, detail}}, seat_cooldown {{status
                           ok|missing|unreadable|malformed, path}}, claude_accounts
                           {{registry, accounts}}
  candidates[]             seat_model, route, backend, access_method, account,
                           quota_identity, quota_identity_basis (account|route), status,
                           reason, cause, source, observed_at (when the evidence was
                           observed), age_seconds, detail
  coverage                 candidates, exhausted, usable, unavailable, unknown, complete
                           (no unknown candidate)
  models, exhausted[]      schema-1 fields kept: distinct seat models; one {{model,
                           source, evidence}} per exhausted candidate
  all_exhausted            the predicate above
  error                    board-resolution error, else null

COMPATIBILITY
  Schema 1 (the unreleased first draft of this command that agent-tools ship.sh was
  written against) had no schema/schema_version and listed every model without
  exhaustion evidence under `available`. That key is gone:
  missing evidence is not availability. A schema-1 consumer reading only
  `all_exhausted` and `exhausted[].model/.evidence/.source` (ship.sh) keeps working, and
  only ever gets a stricter `all_exhausted`.

SOURCE LIMITS (each makes the affected candidates `unknown`, never exhausted)
  - Only Claude Code (CLI) and Codex have quota data (harness-cli cards). opencode,
    omp and the Claude API transport are covered only by a quota cooldown a real call
    on that route recorded. z.ai, Gemini, OpenRouter and CommandCode record no
    cooldowns, so their routes have no evidence path at all: a board using them can
    never read all_exhausted.
  - The cooldown store keys routes, not accounts, so a quota cooldown on a Claude CLI
    route that rotates through several accounts is not attributed to any of them.
  - A cooldown is recorded when the failing call returns; later runs skip the seat
    without re-recording, so it goes stale after the freshness window even if the
    quota is still out.
  - Cooldowns recorded before review-cli#472 carry no typed cause and never count.
  - tg-ctl usage samples carry no account and are not used.
  - omp's own account pool windows (`omp usage`) are not read; an omp Anthropic account
    is judged by its own quota cooldown or its Claude Code card (same email). With
    several pool accounts the route cooldown a pool call also writes names none of them
    and is not read.

See also: `review status` (which seats can run now), `review entities`.
"""


# Deep help TOPICS advertised from the main `review --help` and served by `review help
# <topic>` / `review --help <topic>` (ROADMAP "Topic-based help across the ecosystem").
# topic -> (one-line summary for the main-help listing, zero-arg renderer). Add a topic =
# add an entry here; the main-help pointer + dispatch pick it up with no other edit.
HELP_TOPICS: dict[str, tuple[str, "object"]] = {
    "config": (
        "config file, the model/board selection cascade, keys/auth",
        _help_topic_config,
    ),
    "task-check": (
        "the `task CODE --check --json` self-merge-gate contract, field by field",
        _help_topic_task_check,
    ),
    "runtime": (
        "run stats, the startup ETA line, why never to short-timeout `review`",
        _help_topic_runtime,
    ),
    "capacity": (
        "the `capacity-check --json` quota-outage contract and evidence predicate",
        _help_topic_capacity,
    ),
}


def _subcommand_epilog() -> str:
    topics = "\n".join(
        f"  review help {t:<8} {summary}" for t, (summary, _) in HELP_TOPICS.items()
    )
    return (
        "subcommands:\n"
        + "\n".join(f"  {m.subcommand:<11} {m.summary}" for m in iter_modes())
        + (
            "\n  dashboard   managed web dashboard over review-cli runs (run/start/status/stop/enable/disable)"
            "\n  sessions    list / resume brainstorm sessions (-a all, -s <id> resume)"
            "\n  task        show review iterations and transcripts for one task code"
            "\n  stat        per-harness/per-model usage + health report from the real call logs"
            "\n  capacity-check"
            "\n              is the active board's whole capacity out of quota? (`review help capacity`)"
            "\n  jobs        list detached (--detach) review jobs"
            "\n  status      show one detached job's status, paths, and a log tail (`status <job-id>`);"
            "\n              bare `status`: which board seats can run now, why not, and their substitute"
            "\n  seats       = bare `status`: every seat, Claude CLI account and omp Anthropic account (alias: doctor)"
            "\n  wait        block until a detached job finishes (`wait <job-id>`)"
            "\n  spec-web    multi-spec web reviewer daemon (start/status/stop/add <spec>; also `spec-web <spec>`)"
            "\n  install-skill / install-commit-hook / install-hook tg / register-module"
            "\n\nhelp topics (deep help — `review help <topic>` or `review --help <topic>`):\n"
            + topics
            + "\n  see `review help config` for configuration."
        )
    )


def _build_top_level_parser() -> argparse.ArgumentParser:
    """Build the TOP-LEVEL `review` parser — the overview shown by a bare `review` and by
    `review --help`. It advertises the SUBCOMMAND list (the diff review is `review diff`
    now; a bare `review` no longer runs a diff review — it prints this help) and carries
    only the TRULY GLOBAL options + the board/meta flags (`--list-defaults` / `--show-board`
    / `--pool`). Mode/visual-only flags live on their own subparsers (scoped help)."""
    parser = argparse.ArgumentParser(
        prog="review",
        description=(
            "Run read-only code reviews / AI panels across multiple model backends "
            "(one narrow opt-in exception: `review diff --staged --task CODE --commit` "
            "checkpoints a commit). Everything is a SUBCOMMAND: `review diff` (review the git diff), "
            "`review brainstorm`, `review just-ask`, `review quorum`. A bare `review` "
            "(no subcommand) prints this help — it does NOT run a diff review; use "
            "`review diff` for that."
        ),
        epilog=_subcommand_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Only the truly-global options — NOT the mode/visual-only flags (ROADMAP: those belong
    # in the subcommand help, not the global list). The top-level parser only ever serves the
    # meta flags (--list-defaults / --show-board / --help) + the help fall-through; it never
    # dispatches a review, so it does not need --prompt / --diff / --moderator / the visual
    # group.
    _add_global_options(parser, mode=None)
    return parser


def _build_mode_parser(mode: ModeSpec) -> argparse.ArgumentParser:
    """Build the argparse surface for an EXPLICIT `review <mode> …` subcommand (its prog
    is `review <subcommand>` and it carries the global options + the mode-relevant flags +
    the composable visual group + the mode's own positional)."""
    parser = argparse.ArgumentParser(
        prog=f"review {mode.subcommand}",
        description=mode.summary,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_mode_options(parser, mode=mode)
    return parser


def _reject_removed_flags(argv: list[str]) -> int | None:
    """Reject flags this redesign REMOVED with a clear, actionable error instead of letting
    them silently mis-parse (mode flags) or hit argparse's opaque `unrecognized arguments`
    (the no-replacement flags). Two classes:

      * REMOVED_MODE_FLAGS (`--brainstorm`/`--quorum`/`--just-ask`) → "use the subcommand";
      * REMOVED_FLAGS (`--mcp`/`--ln`) → a 3-part what/why/how-to-fix error (the `--mcp`
        case is the dead review-MCP entrypoint a stale `~/.claude/mcp/mcp.json` still spawns;
        the error tells the user to drop that registration — see structured-exit-codes).

    Returns the stable usage exit code (2) when a removed flag is present, else None. Scans
    only up to the first `--` (end-of-options), so the same string appearing as a positional
    value (e.g. a quote that literally contains '--quorum') is untouched."""
    for tok in argv:
        if tok == "--":
            break
        # `--brainstorm=foo` / `--mcp=foo` form too.
        bare = tok.split("=", 1)[0]
        sub = REMOVED_MODE_FLAGS.get(bare)
        if sub is not None:
            task_hint = (
                " --task CODE" if sub in {"brainstorm", "just-ask", "quorum"} else ""
            )
            print(
                f"review: `{bare}` is no longer a flag — it is now the `{sub}` subcommand.\n"
                f'  use:  review {sub} "<your text>"{task_hint} [options]\n'
                f"  (modes are subcommands now: brainstorm / just-ask / quorum; "
                f"run `review --help`)",
                file=sys.stderr,
                flush=True,
            )
            return 2
        removed = REMOVED_FLAGS.get(bare)
        if removed is not None:
            print(
                f"review: `{bare}` was removed and is no longer accepted.\n"
                f"  why:  {removed.reason}\n"
                f"  fix:  {removed.fix}",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return None


def _reject_removed_subcommand(argv: list[str]) -> int | None:
    """Reject the renamed-away SUBCOMMAND verb `review review` (the diff review is `review
    diff` now) with a one-line `review diff` pointer + the stable usage code (2), else None.
    A pure argv check so it can run in `main()` BEFORE the `-o` tee is armed — a usage error
    must NOT write/truncate the `-o` file (codex P1), exactly like the removed FLAGS. It scans
    past leading global options because `review -m fable review` should get the same helpful
    pointer as `review review`. The later call in `_dispatch` is then a harmless no-op."""
    removed = _leading_removed_subcommand(argv)
    if removed:
        replacement = REMOVED_SUBCOMMANDS[removed]
        print(
            f"review: `review {removed}` is no longer a subcommand — the diff review is "
            f"now `review {replacement}`.\n"
            f"  use:  review {replacement} --task CODE [options]\n"
            f"  (run `review --help` for all subcommands)",
            file=sys.stderr,
            flush=True,
        )
        return 2
    return None


def _leading_removed_subcommand(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return None
        if tok in REMOVED_SUBCOMMANDS:
            return tok
        if tok in _LEADING_MODE_VALUE_OPTS:
            i += 2
            continue
        if any(
            tok.startswith(f"{opt}=")
            for opt in _LEADING_MODE_VALUE_OPTS
            if opt.startswith("--")
        ):
            i += 1
            continue
        if tok in _LEADING_MODE_FLAG_OPTS:
            i += 1
            continue
        if _is_leading_inline_short_option(tok):
            i += 1
            continue
        return None
    return None


def _help_subcommand(rest: list[str]) -> int:
    """`review help [<topic>]` — deep topic help (ROADMAP "Topic-based help across the
    ecosystem"). Bare `review help` lists the topics; `review help <topic>` prints that
    topic. An unknown topic lists the available ones + exits 2 (usage). Also reached as
    `review --help <topic>` (see the main dispatch). Topics live in HELP_TOPICS so adding a
    topic needs no edit here.

    A USAGE error here `raise`s SystemExit(2), it does NOT `return` — like argparse's own
    usage errors and the bare-`review` help path below. `main()`'s `-o` tee only persists the
    captured output on a `return` (a "the dispatch ran" signal); a `return 2` here would let
    that tee TRUNCATE a pre-existing `-o` target to empty on `review help bogus -o file.md`
    (premium merge-gate finding, same data-loss class as #37). SystemExit propagates through
    the tee's `finally` with `completed=False`, so no write happens. The SUCCESS paths (the
    topic listing / a valid topic) still `return 0`: that output is real and SHOULD be teed."""
    if not rest:
        print(
            "review help topics:\n"
            + "\n".join(
                f"  {t:<10} {summary}" for t, (summary, _) in HELP_TOPICS.items()
            )
            + "\n\nRun `review help <topic>` (or `review --help <topic>`) for the full reference."
        )
        return 0
    # Exactly one topic token — extra trailing args are a usage error, not silently dropped
    # (codex review: `review help config --bogus` / `review --help config extra` must NOT
    # exit 0 ignoring the tail).
    if len(rest) > 1:
        print(
            f"review help: takes a single topic, got extra arguments: {' '.join(rest[1:])}\n"
            f"  use:  review help <topic>   (topics: {', '.join(HELP_TOPICS)})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    topic = rest[0]
    entry = HELP_TOPICS.get(topic)
    if entry is None:
        known = ", ".join(HELP_TOPICS)
        print(
            f"review help: unknown topic '{topic}'. Known topics: {known}.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    _summary, render = entry
    text = render()
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _config_default_pool(config: dict) -> int | None:
    """A `pool:` default from config.yaml (a positive int), or None when absent/invalid.

    Lets a personal config pin the default review pool size (e.g. 3 while a provider is
    disabled). A non-positive / non-integer value is ignored (falls back to the preset
    default) rather than erroring — `pool: 0`/`--pool 0` "all seats" stays a CLI-only knob."""
    raw = config.get("pool")
    if isinstance(
        raw, bool
    ):  # bool is an int subclass; a stray `pool: true` is not a size
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def _no_deliberate_pool_choice(args, explicit_preset: bool, config: dict) -> bool:
    """True when nothing -- CLI `--pool`, `--preset`, or a config `pool:` default --
    expressed a deliberate pool-size preference, so a ship-floor pad heuristic is
    free to size the pool itself. (review-cli#309 round-3 review finding: this
    exact condition was duplicated verbatim at the early show-board-facing pad and
    the later dispatch-side pad -- the same duplication-drift pattern that caused
    the original off-by-one bug this PR fixes.)"""
    return (
        args.pool is None
        and not explicit_preset
        and _config_default_pool(config) is None
    )


def _seats_of(board: list) -> tuple[tuple[str, str], ...]:
    """Board seats as (model, display-name) pairs for the pool-selection guard."""
    return tuple((r.model, r.display) for r in board)


def _board_from_models_lenient(
    models: list[str], config: dict, *, preset: str | None = None
) -> list:
    """`board_from_models(models, config, preset=preset)`, but a malformed config
    `board:` entry (`BoardConfigError` -- e.g. review-cli#422's unrecognized `role:`)
    is a WARNING, not a hard failure, when the operator named MODELS directly via
    explicit `-m`: losing that ONE entry's role/name/effort METADATA is recoverable, so
    this retries with the offending `board:` entries dropped rather than aborting an
    explicit `-m` request over unrelated/malformed board metadata. The retry only drops
    entries that actually fail to parse (`_drop_malformed_board_entries`) -- an
    unrelated bad-role entry elsewhere in `board:` must not also erase metadata for an
    entry that DOES match the requested models and IS valid (Fable review finding,
    review-cli#422: `board_from_models` used to wipe the WHOLE config's board metadata,
    including perfectly valid entries, on any single unrelated typo). Shared by every
    explicit-`-m` call site (the --task panel role-pad, `validate_board`, `_show_board`)
    so the fallback message/behavior can't drift between them.

    The sanitized retry is guaranteed to succeed: `_parse_board_entry`'s only raise
    path is `BoardConfigError` on `role:` validation, and the sanitizer drops every
    entry that raises it, so nothing is left for `board_from_models` to reject."""
    try:
        return board_from_models(models, config, preset=preset)
    except BoardConfigError as exc:
        print(
            f"[review-cli] {exc}; ignoring malformed board metadata for explicit -m",
            file=sys.stderr,
            flush=True,
        )
        return board_from_models(
            models, _drop_malformed_board_entries(config), preset=preset
        )


def _default_review_board(
    config: dict, config_models: list[str], config_has_board: bool, default_pool: int
):
    """The board a no-`-m`/no-`--pool` run would use, with its default pool size — the
    precedence the guard offers as the 'drop your override' fallback (config `models:` >
    config `board:` > the default preset). `default_pool` is the pool size a no-override run
    would ACTUALLY use (config `pool:` > preset default), so a proposal's promised size
    matches what re-running would do."""
    if config_models:
        return board_from_models(config_models, config), default_pool
    if config_has_board:
        return load_board(config), default_pool
    return load_board(config, preset=DEFAULT_PRESET), default_pool


def _pool_guard_candidates(
    config: dict,
    config_models: list[str],
    config_has_board: bool,
    default_pool: int,
) -> list[Candidate]:
    """Fallback options the guard proposes when a selection can't converge: the default
    board plus every preset. Boards are built (cheap, no availability probe — the guard
    probes lazily only on the non-converge path); a malformed board is skipped, not fatal.
    A preset identical to the default board is de-duped so the proposal isn't redundant."""
    candidates: list[Candidate] = []
    default_seats: tuple[tuple[str, str], ...] | None = None
    try:
        board, pool = _default_review_board(
            config, config_models, config_has_board, default_pool
        )
        default_seats = _seats_of(board)
        candidates.append(
            Candidate(
                label="default",
                why="drop -m / --pool and run the default board (`review diff`)",
                board=default_seats,
                pool_size=pool,
            )
        )
    except BoardConfigError:
        pass
    for name in preset_names():
        try:
            seats = _seats_of(load_board(config, preset=name))
        except BoardConfigError:
            continue
        if seats == default_seats:
            continue
        candidates.append(
            Candidate(
                label=f"preset:{name}",
                why=f"use the {name!r} preset (`review diff --preset {name}`)",
                board=seats,
                pool_size=preset_pool_size(name),
            )
        )
    return candidates


def _announce_quorum_auto_roles(seats: list) -> None:
    """Operator-facing stderr notice for "which model filled which REVIEW_ROLES
    role" when quorum's default (no `-m`, no config `models:`/`board:`) path
    auto-assigns its own board (see `DEFAULT_BOARD` fallback in `_dispatch`) --
    otherwise nothing surfaces the mapping before the panel itself runs, and an
    operator has no way to tell which live model is speaking for which role."""
    if not seats:
        return
    pairs = ", ".join(f"{r.model} -> {r.role}" for r in seats)
    print(
        f"[review-cli] quorum auto-assigned roles (no -m): {pairs}",
        file=sys.stderr,
        flush=True,
    )


def _warn_if_panel_padded(models: list[str]) -> None:
    """Operator-facing stderr notice for the flat quorum panel, fired iff
    `models` contains a repeat -- the only way a repeat can appear here is
    `expand_flat_models_with_reuse` reusing a model, since its input `src`
    (DEFAULT_MODELS or config `models:`) is already distinct and `-m` never
    reaches this call site (`explicit_models` is handled by an earlier,
    separate branch that bypasses padding entirely). Mirrors the board path's
    existing failover promotion message (reviewlib.panel.run_board_with_failover)
    so the token spend is attributable instead of a silent surprise."""
    if len(set(models)) == len(models):
        return
    counts = {m: models.count(m) for m in dict.fromkeys(models)}
    repeated = ", ".join(f"{m} x{n}" for m, n in counts.items() if n > 1)
    print(
        f"[review-cli] panel padded — reusing {repeated} across multiple seats "
        "(some models near their usage limit)",
        file=sys.stderr,
        flush=True,
    )


def _chain_aware_available(model: str) -> bool:
    """The failover-chain-aware liveness probe: True iff `model` OR any later provider in
    its `reviewlib.provider_failover` chain is reachable and paid.

    This is THE single liveness predicate every pre-dispatch decision about a seat must
    share — the pool guard, the startup pool/reserve SPLIT (`select_pool_and_reserve_with_reuse`,
    both here for the ETA and in `modes.review._mode_review_board` for the REAL dispatch), and
    the ETA's `planned_pool`. Before this fix the guard alone became chain-aware while the split
    stayed on raw `backend_available`, so the guard could approve a pool size the split then
    silently shrank (a two-seat board where one seat's head is down but its failover
    alternate is live: the guard says 'fine, 2 live', the split — still raw — says '1 live',
    and dispatch quietly runs a smaller pool than what was approved). Codex P1 on review of
    #157: 'the chain-aware guard approves seats that board dispatch still removes.'"""
    from .provider_failover import any_provider_available

    return any_provider_available(
        model,
        available=backends.backend_available,
        unpaid=backends.runtime_provider_marked_unpaid,
    )


def _evaluate_pool_or_bail(
    config: dict,
    config_models: list[str],
    config_has_board: bool,
    user_seats: tuple[tuple[str, str], ...],
    explicit_models: list[str],
    pool_arg: int | None,
    default_pool: int,
    reuse_eligible: bool = False,
) -> int | None:
    """Pre-dispatch foolproofing (reviewlib.pool_guard): when the resolved review selection
    (`user_seats` = (model, name) pairs — a board OR the flat `-m` list) can't converge,
    print a proposal / targeted error and return its exit code; otherwise return None to
    proceed. Inert under the fake backend (every seat live -> PROCEED).

    `reuse_eligible` (task-cli#139): pass True ONLY when the real dispatch this call gates
    will actually use `select_pool_and_reserve_with_reuse` — a role-lensed board, including
    a recorded `--task` one-model `-m` pad (review-cli#309). See `evaluate_selection`'s
    own docstring for why this relaxes the convergence floor to "at least 1 live model"
    instead of the distinct-model count."""
    # A config `pool: N` default is a SOFT target (graceful auto-shrink), NOT a hard request:
    # only an EXPLICIT `-m` / `--pool N` sets `explicit=True` so the guard enforces the
    # requested size and proposes when the live subset can't fill it. A bare `review diff`
    # with a config `pool:` still runs the live board (down to the min_converge floor) rather
    # than nagging — an explicit flag is a deliberate ask; a config default is a preference.
    if explicit_models:
        requested, explicit = (
            len({default_distinct_key(m) for m in explicit_models}),
            True,
        )
    elif pool_arg is not None and pool_arg > 0:
        requested, explicit = pool_arg, True
    else:
        requested, explicit = 0, False

    def _guard_reason(model: str) -> str | None:
        # `backend_unavailable_reason` probes only the requested (head) spelling, and — by
        # construction — already agrees with `_chain_aware_available` whenever the head
        # itself is what's down (it embeds the same `runtime_provider_marked_unpaid` check
        # `any_provider_available` uses). The one gap: if it ever returned None while
        # `_chain_aware_available` says the WHOLE chain is down (every provider
        # unavailable/unpaid, a shape only reachable with mismatched injected predicates
        # today, e.g. in a test), the guard would bail with a blank reason. Fail safe with a
        # synthesized one rather than ship a misleading empty message (review of #157).
        reason = backends.backend_unavailable_reason(model)
        if reason is not None or _chain_aware_available(model):
            return reason
        return f"{model}: no provider in its failover chain is currently available"

    decision = evaluate_selection(
        user_board=user_seats,
        requested_size=requested,
        explicit=explicit,
        # THUNK: the fallback boards (default + presets) are re-resolved only on the
        # non-converge path, so a converging happy-path run never rebuilds them (and never
        # re-emits load_board's stderr warnings for a malformed config board — glm review).
        candidates=lambda: _pool_guard_candidates(
            config, config_models, config_has_board, default_pool
        ),
        available=_chain_aware_available,
        reason=_guard_reason,
        reuse_eligible=reuse_eligible,
    )
    if decision.kind == PROCEED:
        return None
    print(decision.text, file=sys.stderr, flush=True)
    return decision.exit_code


def _dispatch(argv: list[str] | None = None) -> int:
    """Resolve and run one `review` invocation. A host-wide run slot taken early in the
    dispatch (`admission.hold_run_slot`, the companion vision fan-out) is released when it
    returns (review-cli#614)."""
    with admission.dispatch_scope():
        return _dispatch_scoped(argv)


def _dispatch_scoped(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_leading_mode_options(list(argv))
    # `review help [<topic>]` — deep topic help. Wired here (a bare management subcommand
    # like `dashboard`), before mode resolution, so it stays off the main argparse surface.
    if argv and argv[0] == "help":
        return _help_subcommand(argv[1:])
    # `review --help <topic>` is an alias for `review help <topic>` (ROADMAP advertises
    # both spellings). Route ANY non-option token after `--help`/`-h` through the topic
    # handler — so a known topic prints, and a TYPO (`review --help confg`) gets the same
    # unknown-topic usage error (exit 2) as `review help confg`, not argparse's normal help
    # (exit 0) (codex review). A bare `--help` (no token) falls through to argparse's help.
    if len(argv) >= 2 and argv[0] in ("--help", "-h") and not argv[1].startswith("-"):
        # Pass ALL tokens after --help (not just argv[1]) so extra trailing args are caught
        # by _help_subcommand's usage check rather than silently dropped (codex review).
        return _help_subcommand(argv[1:])
    if argv == ["install-skill"]:
        return install_skill()
    if argv == ["install-commit-hook"]:
        return install_commit_hook()
    if argv and argv[0] == "install-hook":
        if argv[1:] == ["tg"]:
            return install_hook_tg()
        print("usage: review install-hook tg", file=sys.stderr)
        return 2
    # `review --reviewlib-dir` — print the directory of the `reviewlib` package this `review`
    # actually runs, then exit. Used by the managed-dashboard service to detect the live-symlink
    # trap (the `review` on PATH resolving to a DIFFERENT checkout than the one wiring the
    # service), so autostart never launches the wrong code. Deliberately introspection-only.
    if argv == ["--reviewlib-dir"]:
        import reviewlib

        print(Path(reviewlib.__file__).resolve().parent)
        return 0
    # `review dashboard [--port N] [--no-open]` — local-only web dashboard over the
    # review-cli logs + overseer annotations. Kept as a bare subcommand (like
    # install-skill) so it doesn't bloat the main review argparse surface.
    if argv and argv[0] == "dashboard":
        return _dashboard_subcommand(argv[1:])
    # `review sessions [-a] [-s <id>]` — list / resume brainstorm sessions parsed from
    # the discussion logs. A bare MANAGEMENT subcommand (like dashboard), NOT a fan-out
    # mode, so it is wired here and stays off the main review argparse surface.
    if argv and argv[0] == "sessions":
        return _sessions_subcommand(argv[1:])
    # `review task CODE` — task-scoped run-stat iterations + log transcripts.
    if argv and argv[0] == "task":
        return _task_subcommand(argv[1:])
    # `review jobs` / `review status <job-id>` / `review wait <job-id>` — the
    # `--detach` companion commands (review-cli#160): inspect or block on a
    # session-detached background review started elsewhere. Bare subcommands, same
    # reasoning as `sessions`/`task` above.
    if argv and argv[0] == "jobs":
        return _jobs_subcommand(argv[1:])
    if argv and argv[0] == "status":
        return _status_subcommand(argv[1:])
    if argv and argv[0] == "wait":
        return _wait_subcommand(argv[1:])
    # `review seats` / `review doctor` -- the bare `review status` seat report under the
    # names people reach for when a seat will not run (review-cli#637).
    if argv and argv[0] in ("seats", "doctor"):
        return _seats_subcommand(argv[0], argv[1:])
    # `review stat` — detailed per-harness/per-model token-burn + health report parsed
    # from the real on-disk call logs. A bare MANAGEMENT subcommand (like task/dashboard/
    # sessions), NOT a fan-out mode.
    if argv and argv[0] == "stat":
        return _stat_subcommand(argv[1:])
    # `review entities` — read-only overview of review-cli's own configured
    # harnesses/providers/models/accounts/effort-role lenses. A bare MANAGEMENT
    # subcommand (like stat/task/dashboard/sessions), NOT a fan-out mode (review-cli#487).
    if argv and argv[0] == "entities":
        return _entities_subcommand(argv[1:])
    # `review capacity-check` — report whether every configured review-board seat is
    # confirmed exhausted right now (gh ship's review-quorum/external-review auto-bypass
    # consults this before ever prompting for a manual hatch). A bare MANAGEMENT
    # subcommand (like stat/task/dashboard/sessions), NOT a fan-out mode.
    if argv and argv[0] == "capacity-check":
        return _capacity_check_subcommand(argv[1:])
    # Per-project visual-module subcommands (§6). Kept as bare subcommands (like
    # install-skill) so they don't clutter the main review argparse surface. Project
    # modules load by default (trust-by-default); trust-module only pins under the
    # opt-in REVIEW_UNTRUSTED_MODULES=1 guard (the rare untrusted-repo case).
    if argv and argv[0] == "trust-module":
        from .features.visual.registry import trust_module

        if len(argv) < 2:
            print(
                "usage: review trust-module <name> [--project DIR]  (only needed under REVIEW_UNTRUSTED_MODULES=1)",
                file=sys.stderr,
            )
            return 2
        proj = None
        rest = argv[2:]
        if "--project" in rest:
            i = rest.index("--project")
            proj = Path(rest[i + 1]).expanduser() if i + 1 < len(rest) else None
        return trust_module(argv[1], project=proj)
    if argv and argv[0] == "register-module":
        from .features.visual.registry import register_module

        if len(argv) < 2:
            print("usage: review register-module <path-to-manifest>", file=sys.stderr)
            return 2
        return register_module(argv[1])
    # `review spec-web <spec.md>` — interactive web reviewer for ANY markdown spec.
    # Kept as a bare subcommand (like install-skill / register-module) so it stays off
    # the main review argparse surface; it has its own small flag parser.
    if argv and argv[0] == "spec-web":
        return _spec_web(argv[1:])

    # The removed mode flags (--brainstorm/--quorum/--just-ask) are now subcommands —
    # reject them with a helpful pointer rather than mis-parsing the value (§2).
    rc = _reject_removed_flags(argv)
    if rc is not None:
        return rc

    # The removed SUBCOMMAND verb `review review` (the old stuttering diff review) prints a
    # one-line "use `review diff`" pointer and exits with the usage code — like the removed
    # mode flags. Done BEFORE the help fall-through so a stale `review review …` is
    # diagnosed, not silently turned into a help dump. (Pre-rejected in `main()` before the
    # `-o` tee, so this is a no-op when reached via `main`; it still fires for a direct
    # `_dispatch` call, e.g. in tests.)
    rc = _reject_removed_subcommand(argv)
    if rc is not None:
        return rc

    # --- Subcommand resolution (§2/§4). A recognized leading VERB selects its mode and runs
    # the per-mode parser. ANYTHING else — a bare `review`, `review --flag …` with no verb,
    # an unknown verb — routes to the TOP-LEVEL parser, which serves --help / --list-defaults
    # / --show-board and otherwise prints HELP (a bare `review` no longer runs a diff review:
    # that was the mistake this migration fixes — use `review diff`). ----------------------
    is_subcommand = (
        bool(argv) and not argv[0].startswith("-") and argv[0] in known_subcommands()
    )
    if is_subcommand:
        mode = get_mode(argv[0])
        assert mode is not None  # known_subcommands() guarantees it
        rest = argv[1:]
        parser = _build_mode_parser(mode)
    else:
        # No recognized subcommand. A SUBCOMMAND-scoped flag here (e.g. the old pre-commit
        # `review --staged`, or `review --visual …`) would hit argparse's opaque
        # "unrecognized arguments" on the top-level parser — losing the `review diff`
        # migration pointer. Catch those leading mode/visual-only flags BEFORE argparse and
        # emit the friendly pointer (the friendly path for GLOBAL flags without a verb is the
        # help fall-through below). Then parse the meta flags off the top-level parser; if
        # none short-circuit, fall through to the HELP path (no implicit diff review).
        rc = _reject_subcommand_only_flag_without_verb(argv)
        if rc is not None:
            return rc
        mode = (
            diff_mode()
        )  # only used by the meta-flag handlers (--list-defaults / --show-board)
        rest = argv
        parser = _build_top_level_parser()

    args = parser.parse_args(rest)

    # `--retry N` is wired by EXPORTING $REVIEW_RETRY_COUNT for the rest of this process, so
    # the single in-seat-retry reader (reviewlib.retry.retry_count, called deep in the panel)
    # honours flag, env, and default with ONE precedence rule — the flag wins over a
    # pre-existing env, an unset flag leaves the env (or the built-in default) in force. The
    # board path never has to thread the value through every panel signature. Clamped to the
    # ceiling so a stray `--retry 9999` can't pin a dead seat for minutes.
    if getattr(args, "retry", None) is not None:
        os.environ["REVIEW_RETRY_COUNT"] = str(
            max(0, min(args.retry, max_retry_count()))
        )

    # Run-scoped `--effort` (global flag): parse ONCE into an EffortOverride here so both the
    # board path (applied CLI-side onto the seats) and the flat-panel modes (threaded via
    # ModeContext) resolve against the same value. A bad level is user input for THIS run —
    # fail loudly with exit 2, not the config board's warn-and-drop.
    try:
        effort_override = parse_effort_flag(getattr(args, "effort", None))
    except EffortValueError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2

    config = load_config()
    backends.configure_unpaid_providers(config.get("unpaid_providers"))
    backends.configure_omp_bin(config.get("omp_bin"))
    backends.configure_omp_accounts(config.get("omp_accounts"))
    from .provider_failover import configure_provider_chains

    configure_provider_chains(config.get("provider_chains"))

    # `models:` / `visual_models:` from config, stripped + alias-expanded + blanks dropped
    # (same rule as _split_models for -m). An "effectively empty" list — absent, or only
    # blank/whitespace entries — is NOT a real preference.
    config_models = _split_models(config.get("models") or [])
    config_visual_models = _split_models(config.get("visual_models") or [])
    config_has_board = isinstance(config.get("board"), list) and bool(
        config.get("board")
    )
    explicit_models = _split_models(args.model)
    explicit_preset = getattr(args, "preset", None) is not None
    preset_applies = mode.name == "review"
    if explicit_preset and preset_applies:
        active_preset = args.preset
    elif (
        preset_applies
        and not explicit_models
        and not config_models
        and not config_has_board
    ):
        active_preset = DEFAULT_PRESET
    else:
        active_preset = None
    # Default pool size precedence: explicit `--pool N` > an explicit `--preset` > a
    # config `pool:` default > the preset/built-in default. The config key lets a personal
    # config.yaml pin a smaller board (e.g. 3 while a provider is disabled) without passing
    # `--pool` every call; an explicit flag/preset still wins.
    if args.pool is not None:
        effective_pool_size = args.pool
    elif explicit_preset and preset_applies:
        effective_pool_size = preset_pool_size(active_preset)
    elif _config_default_pool(config) is not None:
        effective_pool_size = _config_default_pool(config)
    else:
        effective_pool_size = preset_pool_size(active_preset)

    if args.list_defaults:
        if explicit_models:
            effective = explicit_models
        elif mode.name == "visual":
            effective = config_visual_models or [
                _expand_alias(x) for x in VISUAL_MODELS
            ]
        elif mode.name == "review" and active_preset is not None:
            try:
                effective = [r.model for r in load_board(config, preset=active_preset)]
            except BoardConfigError as exc:
                print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
                return 2
        elif mode.name == "review" and config_models:
            effective = config_models
        elif mode.name == "review" and config_has_board:
            try:
                effective = [r.model for r in load_board(config)]
            except BoardConfigError as exc:
                print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
                return 2
        elif mode.name == "brainstorm":
            effective = (
                _split_models(config.get("brainstorm_models") or [])
                or config_models
                or [_expand_alias(x) for x in DEFAULT_MODELS]
            )
        else:
            effective = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        print("\n".join(effective))
        return 0

    # Optional `--task` (or $REVIEW_TASK_CODE) floors the pool BEFORE --show-board
    # so `review diff --task CODE --show-board --preset light` displays 3, not 2
    # (review-cli#309). Missing --task is not fatal here — show-board stays light's 2.
    raw_early_task = getattr(args, "task", None) or os.environ.get("REVIEW_TASK_CODE")
    early_task: str | None = None
    if raw_early_task:
        try:
            early_task = normalize_task_code(raw_early_task)
        except ValueError:
            early_task = None
    if early_task and mode.name == "review":
        config_roster_len = (
            len(config_models)
            if config_models
            else len(config.get("board") or [])
            if config_has_board
            else None
        )
        if args.pool is not None:
            # `--pool 0` (or negative) means "all seats" and is normally fine for a
            # multi-seat config/default board (plenty of distinct roles either way).
            # An explicit `-m` list, or a config `models:`/`board:` roster shorter
            # than the ship floor, has no MORE seats than that list/roster itself --
            # "all seats" collapses to exactly that count, genuinely below the ship
            # floor whenever it is fewer than SHIP_MIN_ROLES, same as an explicit
            # low `--pool N` (round-3 review finding: the explicit-`-m` warning
            # above missed the mirror-image case, a short config roster with NO
            # `-m`, e.g. `--pool 0` on a 2-model `models:` config).
            below_floor_count = (
                len(explicit_models)
                if explicit_models
                else config_roster_len
                if config_roster_len is not None
                else None
            )
            pool_zero_below_floor = (
                args.pool <= 0
                and below_floor_count is not None
                and below_floor_count < SHIP_MIN_ROLES
            )
            if 0 < args.pool < SHIP_MIN_ROLES or pool_zero_below_floor:
                subject = (
                    f'--pool {args.pool} ("all seats") with only '
                    f"{below_floor_count} available model/seat(s)"
                    if pool_zero_below_floor
                    else f"--pool {args.pool}"
                )
                print(
                    f"[review-cli] warning: {subject} is below the default ship "
                    f"floor of {SHIP_MIN_ROLES} distinct board roles; this run "
                    "cannot satisfy `review task CODE --check` / gh ship.",
                    file=sys.stderr,
                    flush=True,
                )
            elif args.pool <= 0 and not explicit_models:
                # review-cli#433: seat COUNT can already meet the floor while
                # every seat shares one role. The warning above only checks
                # count, so load the config roster and warn on role diversity.
                # Do not fire on the default/preset board (no config roster)
                # and do not double-warn when the seat-count warning already ran.
                try:
                    roster = (
                        board_from_models(config_models, config)
                        if config_models
                        else load_board(config)
                        if config_has_board
                        else None
                    )
                except BoardConfigError:
                    roster = None
                if roster is not None:
                    # Only a fully-specified roster (every seat has a known
                    # role_lens) can be "all same role". A `models:` list that
                    # only partially matches DEFAULT_BOARD metadata (e.g.
                    # ["codex", "gemini", "grok"] — "codex" has no board seat)
                    # is a seat-count question, not a role-diversity one.
                    known = [r for r in roster if r.role_lens]
                    distinct_roles = len({r.role for r in known})
                    if (
                        known
                        and len(known) == len(roster)
                        and distinct_roles < SHIP_MIN_ROLES
                    ):
                        print(
                            f'[review-cli] warning: --pool 0 ("all seats") with only '
                            f"{distinct_roles} distinct board role(s) is below the "
                            f"default ship floor of {SHIP_MIN_ROLES} distinct board "
                            "roles; this run cannot satisfy `review task CODE "
                            "--check` / gh ship.",
                            file=sys.stderr,
                            flush=True,
                        )
        else:
            effective_pool_size = recorded_task_pool_size(
                effective_pool_size,
                explicit_pool=False,
                recorded=True,
            )
        # A config `models:`/`board:` roster that does not EXCEED the ship floor is,
        # like the single-model `-m` case above, not a real DEFAULT_POOL_SIZE-seat
        # roster -- `recorded_task_pool_size`'s `max(resolved, SHIP_MIN_ROLES)` never
        # LOWERS an already-resolved `effective_pool_size` (DEFAULT_POOL_SIZE 4, the
        # no-preset/no-`pool:`-key fallback that applies here too), so a 1-3 entry
        # roster was silently padded past its OWN size to 4 (review-cli#309 round-2
        # review finding: a strict `< SHIP_MIN_ROLES` missed the EXACTLY-3 case --
        # `3 < 3` is False -- leaving a 3-model roster at pool 4 instead of its own 3).
        short_config_roster = (
            not explicit_models
            and config_roster_len is not None
            and config_roster_len <= SHIP_MIN_ROLES
        )
        if (
            (len(explicit_models) == 1 or short_config_roster)
            and _no_deliberate_pool_choice(args, explicit_preset, config)
            and effective_pool_size > 1
        ):
            # One-model `-m`, or a config roster at or below the ship floor, is not a
            # DEFAULT_POOL_SIZE-seat roster; floor/cap it to SHIP_MIN_ROLES (a no-op
            # when the roster is already exactly that size).
            effective_pool_size = SHIP_MIN_ROLES

    if args.show_board:
        # Resolve cwd up front so the agentic/diff-only labels reflect whether opencode
        # would actually run in a real repo for THIS -C (it's diff-only outside a repo).
        # `len(explicit_models) <= 1` mirrors the dispatch-side `task_role_pad` gate
        # (review-cli#309 round-3 review finding): a multi-model explicit `-m` list
        # stays exact on dispatch (never gains cloned seats), so `--show-board` must
        # not show it padded either -- a padded display for a run that will actually
        # dispatch exact would mislead an agent planning off `--show-board`'s output.
        # `--pool 0` ("all seats") sets effective_pool_size=0, so the `> 1` half of
        # that gate would skip a config/default board that dispatch now repairs
        # (review-cli#433). Pad show-board for `--task --pool 0` with no `-m`, but
        # still not for `--pool 0 -m grok` (dispatch stays exact 1-seat).
        return _show_board(
            config,
            effective_pool_size,
            _effective_cwd(args.cwd),
            preset=active_preset,
            explicit_models=explicit_models,
            pad_task_roles=bool(
                early_task
                and mode.name == "review"
                and (
                    (len(explicit_models) <= 1 and effective_pool_size > 1)
                    or (not explicit_models and effective_pool_size <= 0)
                )
            ),
        )

    # `--staged` / `--diff` name the diff source explicitly (the git index / the working
    # tree), so stdin is never read for them (review-cli#613): `review diff --staged` from
    # a launcher with an inherited, silent non-tty stdin must start reviewing immediately.
    # A diff piped in anyway earns one "ignoring piped stdin" line (`_warn_if_stdin_
    # ignored`, a non-blocking peek). Only a verb with no explicit source reads a piped
    # diff (`git diff | review diff`), and that read is bounded.
    explicit_source = _explicit_diff_source(args)
    if explicit_source is not None:
        _warn_if_stdin_ignored(*explicit_source)
        piped_input = None
    else:
        piped_input = _read_stdin_if_piped()
    task_code: str | None = None
    requires_task_code = is_subcommand and (
        mode.name != "visual"
        or getattr(args, "diff", False)
        or getattr(args, "staged", False)
        or piped_input is not None
    )
    if requires_task_code:
        raw_task = getattr(args, "task", None) or os.environ.get("REVIEW_TASK_CODE")
        try:
            task_code = normalize_task_code(raw_task)
        except ValueError as exc:
            print(
                f"[review-cli] invalid --task CODE: {exc}", file=sys.stderr, flush=True
            )
            return 2
        if task_code is None:
            print(
                "[review-cli] missing required --task CODE for review iteration history.\n"
                "  use: review <mode> --task CODE ...\n"
                "  or set REVIEW_TASK_CODE=CODE for automation.",
                file=sys.stderr,
                flush=True,
            )
            return 2
        args.task = task_code

    # A bare `review` (or `review --flag …` with no verb / an unknown verb) reaches here
    # without a meta flag to serve: print the HELP/usage instead of silently running a diff
    # review. There are three shapes:
    #   * args passed (`review -C <repo>`)            -> usage error (exit 2): point at `review diff`;
    #   * a diff piped on stdin (`git diff | review`)  -> usage error (exit 2): the classic
    #       piped-diff review is `git diff | review diff` now — a bare `review` here used to
    #       run a diff review, so silently exiting 0 would turn it into a no-op SUCCESS that
    #       a script can't detect (codex P1). Fail loud, pointing at `review diff`;
    #   * truly bare (`review`, no args, TTY stdin)    -> print the overview help, exit 0.
    #
    # Raise SystemExit (do NOT `return`): like argparse's own --help / usage errors, a
    # help/usage dump must NOT write the `-o` file — `main()`'s tee only persists on a
    # `return` (a "the dispatch ran" signal), so a `return` here would truncate a
    # pre-existing `-o` target with the help text / an empty buffer. SystemExit propagates
    # through the tee's `finally` with `completed=False`, so no write happens.
    if not is_subcommand:
        piped_diff = (not rest) and (piped_input is not None)
        usage_error = bool(rest) or piped_diff
        parser.print_help(sys.stderr if usage_error else sys.stdout)
        if piped_diff:
            print(
                "\nreview: a diff was piped in but no subcommand given. The diff review is "
                "now `review diff` (a bare `review` no longer runs one). "
                "Run `git diff | review diff --task CODE`.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(2)
        if rest:
            print(
                "\nreview: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one). Run `review diff --task CODE [options]`.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(2)
        raise SystemExit(0)

    if mode.name == "visual" and not args.visual:
        parser.error("the following arguments are required: IMAGE")

    # Suppress the "reviewing it as-is" non-repo warning on the REVIEW-mode required-diff
    # path: there a non-repo hard-fails via `_fail_not_a_repo` (the authoritative message),
    # so the "as-is" promise would contradict it. The no-git modes (panel) and the
    # tolerant `--visual` review (which DOES proceed as-is) keep the warning.
    _review_required = mode.name == "review" and (args.staged or args.visual is None)
    cwd = _effective_cwd(args.cwd, warn=not _review_required)
    is_brainstorm = mode.name == "brainstorm"
    is_visual_subcommand = mode.name == "visual"
    # A "panel mode" is any non-review mode (brainstorm / just-ask / quorum): the diff is
    # OPTIONAL context for it, its calls are long-running (announce live-log paths), and
    # its per-call timeout default is the shorter PANEL_TIMEOUT_DEFAULT.
    panel_mode = mode.name not in ("review", "visual")
    # Precedence: explicit -m > config > code default. Brainstorm prefers
    # config.brainstorm_models and drops unreachable backends gracefully (so a
    # missing GEMINI_API_KEY never aborts the run). Explicit -m is honored as-is.
    # One usage-limit SNAPSHOT for this whole dispatch (board mode takes the same
    # approach in modes/review.py) — every seat/candidate checked below reads the
    # SAME sample set instead of each re-globbing + re-parsing the tg-ctl usage
    # file independently (and possibly disagreeing mid-selection if it changes).
    # Load harness cards once too: passing samples= alone disables the live
    # harness path inside usage_percent_for_model, so composition would ignore
    # an exhausted Claude Code / Codex card reported only by `harness limits`.
    _usage_snapshot = usage_limits.load_snapshot()
    _usage_cards = usage_limits.load_harness_cards()

    def _usage_percent(model: str) -> float | None:
        return usage_percent_for_model(
            model, samples=_usage_snapshot, cards=_usage_cards
        )

    # Populated ONLY on quorum's own default path (no -m, no config
    # `models:`/`board:` override) when the DEFAULT_BOARD fallback below finds
    # live seats -- carries the role each auto-picked model was assigned so it
    # can be merged into `panel_task_roles` further down and threaded through
    # `ctx.extra["task_roles"]` to `mode_quorum`'s real `_seat_assignments`.
    quorum_auto_roles: list[str] = []
    if explicit_models:
        models = explicit_models
    elif is_brainstorm:
        src = (
            _split_models(config.get("brainstorm_models") or [])
            or config_models
            or [_expand_alias(x) for x in DEFAULT_MODELS]
        )
        models = [m for m in src if backends.backend_available(m)]
        if not models:
            models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        else:
            # Reuse-aware panel: pad back up to the AVAILABILITY-filtered count
            # (not `len(src)`) when some of the reachable models are near their
            # usage limit, instead of silently running a smaller panel (Alex,
            # 2026-08-18). Target is `len(models)`, not `len(src)`, so this ONLY
            # compensates usage-limit exclusions -- it must never fight brainstorm's
            # existing "drop unreachable backends gracefully" shrink above.
            models = expand_flat_models_with_reuse(
                models, len(models), usage_percent=_usage_percent
            )
            _warn_if_panel_padded(models)
    else:
        src = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        if mode.name == "quorum":
            # Default (no -m) quorum path: try the SAME role-tagged
            # DEFAULT_BOARD + reuse-aware selector `review diff`'s own board
            # dispatch already uses, so a plain `review quorum "<question>"
            # --task CODE` gets a real REVIEW_ROLES role-to-model mapping
            # instead of falling back to generic literary PERSONAS
            # (`_seat_assignments` in modes/quorum.py). Scoped to quorum's
            # OWN unconfigured default only: a config `models:` roster or a
            # config `board:` is an explicit operator choice and must keep
            # using the flat `src` path below untouched, exactly like
            # today.
            if not config_models and not config_has_board:
                quorum_board_seats, _ = select_pool_and_reserve_with_reuse(
                    list(DEFAULT_BOARD),
                    effective_pool_size,
                    lambda r: _chain_aware_available(r.model),
                    usage_percent=_usage_percent,
                )
                if quorum_board_seats:
                    models = [r.model for r in quorum_board_seats]
                    quorum_auto_roles = [r.role for r in quorum_board_seats]
                    _announce_quorum_auto_roles(quorum_board_seats)
            if not quorum_auto_roles:
                # Scoped to quorum specifically: `review` builds its OWN board
                # from `config_models`/`load_board` (never reads this `models`
                # var) and `visual`/`qa` use their own model lists too — this
                # `else` branch is also their fallthrough, so padding it
                # unconditionally printed a spurious "panel padded" warning for a
                # panel that was never actually dispatched (k3/Fable review
                # finding). `just-ask` is EXCLUDED too, deliberately: it sends the
                # identical prompt to every seat with no per-seat role/lens (see
                # config's role-less-board carve-out for the same reasoning), so a
                # duplicated seat there is pure cost with zero added diversity —
                # unlike quorum, which labels + discloses duplicates to its
                # moderator so a repeated model is at least an INFORMED tradeoff.
                #
                # Availability-filter BEFORE padding (k3 review finding, round 3):
                # `_is_near_limit` fails OPEN on unknown usage data, so an
                # UNREACHABLE model (missing API key/CLI — unknown to
                # usage_percent, hence "not excluded") could otherwise survive
                # padding while a REACHABLE-but-near-limit model gets excluded —
                # e.g. src=[claude:opus@90%, gemini(keyless)] would pad to
                # ["gemini","gemini"], a panel with ZERO live seats, worse than
                # quorum's pre-reuse behavior of just dispatching both and letting
                # the dead one fail per-call. Filtering first keeps padding
                # scoped to seats that can actually answer; the all-unreachable
                # case is handled below (dispatch `src` unfiltered, no exclusion
                # — k3 review finding, round 5).
                #
                # Target `len(reachable)`, NOT `len(src)` (k3/Fable review finding,
                # round 4): padding must compensate ONLY usage-limit exclusions,
                # exactly like the brainstorm branch above — targeting `len(src)`
                # would ALSO duplicate a live model to paper over a plain
                # unreachable one (a real paid call replacing what used to be a
                # free per-call failure), which is a different, unrequested
                # feature and made `_warn_if_panel_padded`'s "near their usage
                # limit" text false in that case.
                #
                # `_chain_aware_available`, NOT raw `backend_available` (k3/Fable
                # review finding, round 6): the raw probe false-negatives on a
                # seat whose HEAD provider is down but has a live failover
                # alternate (e.g. no ZAI_API_KEY but an authenticated `oc:zai`) —
                # exactly the case `_chain_aware_available`'s own docstring exists
                # to cover for every OTHER pre-dispatch liveness decision (the
                # pool guard, the board split, the ETA). Using the raw probe here
                # would silently drop a chain-recoverable model from the quorum
                # panel instead of just letting `run_panel`'s own provider
                # failover (panel.py) route around the down head provider.
                reachable = [m for m in src if _chain_aware_available(m)]
                if reachable:
                    models = expand_flat_models_with_reuse(
                        reachable, len(reachable), usage_percent=_usage_percent
                    )
                    _warn_if_panel_padded(models)
                else:
                    # EVERYTHING reads unreachable -- dispatch `src` UNFILTERED
                    # and skip usage-limit exclusion entirely (k3 review finding,
                    # round 5): the comment above claimed this fallback matches
                    # brainstorm's "identical" one, but it didn't -- brainstorm's
                    # actual all-unreachable fallback (`config_models or
                    # [_expand_alias(x) for x in DEFAULT_MODELS]`, a few lines up)
                    # dispatches the raw list with NO exclusion, letting each dead
                    # seat fail per-call (and its provider-chain failover a real
                    # chance to recover a false-negative availability probe).
                    # Applying exclusion here too would silently drop a near-limit
                    # model in favor of duplicating another EQUALLY DEAD one --
                    # pure noise when nothing is going to answer regardless.
                    models = src
        else:
            # just-ask (visual/qa also fall through here but ignore `models`):
            # drop unreachable defaults so a missing xAI OAuth seat cannot fail
            # the all-usable just-ask exit (review-cli#418). No reuse-padding —
            # identical prompt, no per-seat lens, a duplicate is pure cost (see
            # the quorum carve-out above). All-unreachable dispatches `src`
            # unfiltered so a false-negative probe can still recover per-call.
            if mode.name == "just-ask":
                reachable = [m for m in src if _chain_aware_available(m)]
                models = reachable or src
            else:
                models = src

    visual_mode = args.visual is not None
    if explicit_models:
        visual_models = explicit_models
    else:
        visual_models = config_visual_models or [
            _expand_alias(x) for x in VISUAL_MODELS
        ]
    # Timeout default by mode. qa is the carve-out: it is technically a "panel mode" (non-
    # review), but a tester run boots a SUT and drives a whole suite with an un-caged agent —
    # tens of minutes, not the short PANEL_TIMEOUT_DEFAULT (240s) the chat panels use. Give it
    # its OWN long default (it leans on the <=4h backstop, not a 4-minute cap). Review keeps
    # 1200s; the other panel modes (brainstorm/just-ask/quorum) keep the short panel default.
    # Timeout precedence: --timeout flag > config.yaml `timeout:` > mode default.
    # `config_timeout` lets you set a persistent short default for iterate-review
    # workflows without passing --timeout every time.
    config_timeout = config.get("timeout")
    if args.timeout is not None:
        timeout = args.timeout
    elif mode.name == "qa":
        timeout = QA_TIMEOUT_DEFAULT
    elif panel_mode:
        timeout = PANEL_TIMEOUT_DEFAULT
    elif config_timeout is not None:
        timeout = int(config_timeout)
    else:
        timeout = 1200

    # Reviewer board (HYP-741): the diff-review panel assigns each model its own
    # role/lens and keeps a reserve. Precedence:
    #   explicit -m  >  config `models:` priority roster  >  config/default board.
    # With no configured board/models, explicit -m keeps the legacy flat panel. When a
    # config board/models roster exists, explicit -m NARROWS that configured board to the
    # requested models only, preserving matching role/name metadata but never adding extra
    # configured seats. A configured `models:` list is the full priority-ordered roster from
    # which the active pool + reserve are selected; optional `board:` entries can still
    # provide role/name metadata for those models. The board is NEVER disabled — `--pool N`
    # only sizes how many of its seats run (default 2, the light preset; the rest are
    # reserve). `use_board` is
    # a cheap boolean gate computed now; the actual board resolution + cost-safety validation
    # (and the --pool slice) runs LATER
    # (validate_board, below) — after the standalone-visual path has had its chance to
    # short-circuit, so a malformed `board:` never blocks the board-unrelated standalone
    # `review visual` pipeline (codex P2). It still fires
    # BEFORE the COMPANION visual fan-out, so a doomed config never spends a paid vision
    # call.
    # One explicit `-m` model on a recorded run: clone that model onto unused
    # roles and reuse-pad to the ship floor (review-cli#309, extended in
    # review-cli#433 to quorum / brainstorm). `--pool 1` keeps exact
    # (effective_pool_size > 1 is false) and warns above. Review uses the board
    # path; panel modes pad the flat `models` list below. Never just-ask
    # (review-cli#587): it asks every seat the same question with no role lens,
    # so a pad only multiplied the calls — and the wait — by the role count.
    task_role_pad = (
        bool(task_code)
        and mode.name in ("review", "quorum", "brainstorm")
        and len(explicit_models) == 1
        and effective_pool_size > 1
    )
    if task_role_pad and _no_deliberate_pool_choice(args, explicit_preset, config):
        # Without `--pool`/`--preset`/config `pool:`, a one-model `-m` pad targets
        # the ship floor, not DEFAULT_POOL_SIZE 4 (the no-preset fallback `-m` never
        # consumed).
        effective_pool_size = SHIP_MIN_ROLES

    panel_task_roles: list[str] = []
    if task_role_pad and panel_mode:
        # Never shrink the role list to engine count: one live model still
        # executes every required role via separate role-specific invocations
        # (review-cli#433). Copies of one response do not fill extra slots.
        pad_min = effective_pool_size if effective_pool_size > 0 else SHIP_MIN_ROLES
        # review-cli#422: a typo'd config `board:` role must not crash --task panel
        # dispatch with a raw traceback -- `_board_from_models_lenient` (shared with
        # `validate_board`/`_show_board` below) recovers the same way every other
        # explicit -m call site does.
        board_source = _board_from_models_lenient(explicit_models, config)
        padded_board = ensure_min_distinct_roles(board_source, pad_min)
        models = [r.model for r in padded_board]
        panel_task_roles = [r.role for r in padded_board]
        # review-cli#587: the pad multiplies the calls, so say so up front.
        print(
            f"[review-cli] --task with one -m model: {mode.name} runs "
            f"{explicit_models[0]} as {len(models)} role slots "
            f"({', '.join(panel_task_roles)}) = {len(models)} calls",
            file=sys.stderr,
            flush=True,
        )
    elif quorum_auto_roles:
        # Mutually exclusive with task_role_pad in practice (quorum_auto_roles
        # is only ever populated on the no-`-m` default path, task_role_pad
        # requires exactly one explicit `-m`), but kept as `elif` defensively
        # -- task_role_pad's own role assignment wins if both were somehow true.
        panel_task_roles = quorum_auto_roles

    use_board = not panel_mode and (
        not explicit_models
        or bool(config_models)
        or config_has_board
        or active_preset is not None
        or task_role_pad
    )
    board: list | None = None
    board_validated = False

    def validate_board() -> int | None:
        """Resolve + validate the FULL priority-ordered reviewer board for the default
        review path, once. The board is loaded whole (NOT sliced to --pool here): the
        failover pool path (mode_review) does the startup failover — selecting the top
        configured number of AVAILABLE seats by priority — and keeps the rest as the reserve that
        backfills a seat which fails mid-run. Returns an exit code (2) on an all-malformed
        `board:` config, else None. No-op when the board does not apply (panel mode or
        explicit -m with no configured board/models)."""
        nonlocal board, board_validated
        if board_validated or not use_board:
            return None
        board_validated = True
        try:
            if explicit_models:
                board = _board_from_models_lenient(
                    explicit_models, config, preset=active_preset
                )
            elif active_preset is not None:
                board = load_board(config, preset=active_preset)
            elif config_models:
                board = board_from_models(config_models, config)
            else:
                board = load_board(config)
        except BoardConfigError as exc:
            print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
            return 2
        # Run-scoped `--effort` OVERRIDES each seat's config effort for this run (falls back
        # to the seat value where the flag says nothing). Applied once, here, so the failover
        # board mode_review consumes carries the effective effort — no double application.
        if board is not None:
            board = apply_effort_override(board, effort_override)
            # Recorded `--task` reviews with a short roster (explicit `-m` one-model
            # OR a 1-2 seat config `models:`/`board:`, but NEVER a multi-model
            # explicit `-m` list -- that stays exact/never gains extra seats, same
            # contract as every other explicit -m board) need unused roles cloned
            # on so select_pool_with_reuse can pad to the ship floor (review-cli#309).
            # Gating this on the FULL board's distinct-role count (`< SHIP_MIN_ROLES`)
            # was wrong (review-cli#309 round-4 review finding): `select_pool_with_reuse`
            # slices the first `pool_size` seats POSITIONALLY, so a board that already
            # has >= SHIP_MIN_ROLES distinct roles OVERALL can still slice to fewer if a
            # duplicate-role seat sits ahead of a distinct one in priority order --
            # exactly the case `ensure_min_distinct_roles`'s own prefix-dedup exists to
            # fix. Call it whenever the other conditions hold: it is a safe no-op (returns
            # the same seats in the same order) whenever the first `effective_pool_size`
            # known-role seats are already distinct.
            #
            # `task_role_pad` (a single explicit `-m` model) always repairs/pads --
            # that ONE seat is deliberately cloned across roles regardless of whether it
            # started with a role, the entire point of the single-model pad. A config/
            # default board (not `task_role_pad`) only repairs when it ALREADY carries at
            # least one real `role_lens` -- mirroring the pre-existing `reuse_eligible`
            # safety gate below: a board whose EVERY role is a typo/unknown REVIEW_ROLES
            # key is functionally role-less by user intent and must stay that way rather
            # than have this repair invent real lenses the user never configured
            # (test_unknown_role_board_with_one_live_seat_still_bails_end_to_end); repair
            # is for a SHORT roster missing SOME distinct roles, not for recovering an
            # entirely-role-less/misconfigured one.
            # `--pool 0` ("all seats") sets effective_pool_size=0. Passing 0 into
            # ensure_min_distinct_roles is a no-op (min_roles<=0 returns the board
            # unchanged), and the `> 1` gate skipped the call entirely — a 4-seat
            # all-same-role config roster then dispatched 1 distinct role
            # (review-cli#433). All-seats mode still needs the ship-floor prefix.
            role_repair_min = (
                effective_pool_size if effective_pool_size > 0 else SHIP_MIN_ROLES
            )
            if (
                bool(task_code)
                and mode.name == "review"
                and role_repair_min > 1
                and (
                    task_role_pad
                    or (not explicit_models and any(r.role_lens for r in board))
                )
            ):
                board = ensure_min_distinct_roles(board, role_repair_min)
        return None

    # Panel modes are interactive and long-running, so announce each streamed
    # backend's live-log path to stderr; the plain review path stays quiet. The mode
    # descriptor declares this (announce_logs); brainstorm/just-ask/quorum opt in.
    if mode.announce_logs:
        backends._ANNOUNCE_LOGS = True

    # Diff acquisition. Panel modes treat the diff as optional context. The review mode
    # REQUIRES a diff. With --visual + the review mode, the diff still drives the routing
    # (§3): a present diff → the diff-review companion, an absent diff → the standalone
    # pipeline — so we MUST still try to discover it, but a missing diff / non-repo must
    # degrade to standalone rather than abort.
    diff = piped_input
    # A piped diff is NOT the git index, so it must not satisfy the staged commit gate:
    # the CLI never reads stdin under `--staged` (review-cli#613), but the mode handler
    # is also a library entry point, so it records the provenance and suppresses the
    # stamp/marker for a stdin diff on its own (`diff_from_stdin`).
    diff_from_stdin = diff is not None
    # brainstorm treats the diff as OPTIONAL grounding context even with --staged/--diff,
    # so it must NOT take the hard-fail `needs_diff` path: a non-repo `-C` or a failing
    # `git diff [--cached]` degrades to pure ideation (diff == ""), not an abort. Only the
    # review mode (no --visual) genuinely REQUIRES a diff; --staged on a review still
    # hard-requires it (the pre-commit gate). So brainstorm is excluded from needs_diff
    # and routed through the caught/optional probe below.
    needs_diff = (
        (args.staged or (mode.name == "review" and not visual_mode))
        and not is_brainstorm
        and not is_visual_subcommand
    )
    if diff is None and needs_diff:
        # This path attaches the working-tree / staged diff. Outside a git repo it must NOT
        # raise a raw `git diff` traceback. Two cases:
        #   * REVIEW mode (not panel_mode): the diff is genuinely REQUIRED, so a non-repo is
        #     a user error — fail GRACEFULLY with the 3-part message + stable EXIT_NOT_A_REPO.
        #   * PANEL mode (just-ask / quorum) with --staged: the diff is OPTIONAL context
        #     (diff_policy="none"), so a non-repo degrades to no-context ("") — NOT a hard
        #     error, and never the "run just-ask" message at someone already running it.
        # A piped diff short-circuited above (diff is not None), so the stdin path never
        # reaches here — it works without a repo.
        if not _is_git_repo(cwd):
            if panel_mode:
                diff = ""  # optional context (diff_policy="none") -> degrade, never hard-fail
            else:
                return _fail_not_a_repo(cwd)
        elif panel_mode:
            # In a repo but the diff is OPTIONAL context for a panel mode: a `git diff`
            # failure (e.g. an unborn HEAD with --staged, a partial repo) degrades to
            # no-context, exactly like the `--diff` / brainstorm siblings below — never a
            # raw traceback.
            try:
                diff = _git_diff(cwd, args.staged)
            except RuntimeError:
                diff = ""
        else:
            # REQUIRED path, in a real repo. `_is_git_repo` passing does NOT guarantee `git
            # diff` succeeds (a wedged/timed-out git, a corrupt index -> `_git_diff` raises
            # RuntimeError). The diff is required here, so we can't degrade to "" — but we must
            # still NOT traceback: fail GRACEFULLY with a structured error + stable exit.
            try:
                diff = _git_diff(cwd, args.staged)
            except RuntimeError as exc:
                return _fail_git_diff(cwd, exc)
    elif diff is None and visual_mode and mode.name == "review":
        # --visual riding the review mode: probe the working-tree diff to decide
        # companion-vs-standalone, but tolerate "no diff / not a git repo".
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and is_visual_subcommand and (args.diff or args.staged):
        # `review visual IMAGE` is standalone by default. `--diff` / `--staged` explicitly
        # opt into the companion diff-review path, but still degrade to standalone when no
        # diff can be obtained.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and is_brainstorm:
        # brainstorm picks up the staged (--staged) or working-tree diff as OPTIONAL
        # grounding context so you can brainstorm ABOUT a specific change. The diff is
        # never required: an absent diff / non-repo / git failure degrades to pure
        # ideation (diff == ""). `_read_stdin_if_piped` already returns the diff for a
        # NON-EMPTY pipe (precedence); empty/`/dev/null` stdin reads as None here, so we
        # still probe the working tree — matching every other mode and the documented
        # `review brainstorm "Q" < /dev/null` convention (an empty redirect must NOT
        # suppress grounding). `--diff` is the explicit opt-in spelling of the same probe.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and panel_mode and args.diff:
        # just-ask / quorum: the diff is "none" policy (a question, not a change), so it
        # is NOT auto-grabbed. `--diff` is the explicit OPT-IN to attach the working-tree
        # diff as context (the staged counterpart is the `needs_diff` path above). It
        # degrades gracefully to no-context on a non-repo / git failure.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    diff = diff or ""

    # Diff-identity binding (reviewlib.stats "Diff-identity binding"): captured from
    # the FULL, uncapped diff — computed BEFORE the dispatch-time cap below and before
    # mode_review's own internal capping, so a huge diff's identity is never truncated
    # away. `repo_id` is independent of `diff` (cwd alone), so it's computed even for a
    # diff-less just-ask/quorum/brainstorm run — that still lets --check catch a
    # cross-REPO mismatch (incident #1) even with no file-set signal to add.
    repo_id = _compute_repo_id(cwd)
    diff_files = extract_diff_files(diff)
    diff_sha256 = diff_content_hash(diff) if diff else None

    # Review-stamp integrity (round-5 review finding, k3+Opus, on this same
    # diff-identity feature): the pre-commit gate's stamp must certify the diff
    # actually DISPATCHED to the models, not whatever happens to be staged
    # MINUTES later when the multi-model panel finishes. Captured HERE,
    # immediately adjacent to the `diff` capture above (a millisecond gap, not
    # the minutes-long panel-run gap `_write_review_stamp` used to have when it
    # independently re-ran `git diff --cached` at stamp-WRITE time) — see
    # `reviewlib.install._write_review_stamp`'s docstring for the full story
    # (why it can't just hash `diff` directly: the pre-commit hook's own
    # verification is UNPREFIXED, `diff` is prefixed via `_git_diff`'s
    # `diff.noprefix` fix). Only meaningful for `--staged` (the only case
    # `_write_review_stamp` is ever reached from); None otherwise.
    stamp_diff_hash = _stamp_hash_for_staged_diff(cwd) if args.staged else None

    # Dispatch-time diff cap for the flat PANEL modes (brainstorm/quorum/just-ask) —
    # see reviewlib.backends.cap_diff_for_dispatch's docstring for why this is NOT
    # applied to `review`/`visual` (mode_review owns capping its own two dispatch
    # paths itself, so it can keep the UNCAPPED canonical diff for the --commit
    # checkpoint's integrity check; these three modes have no such requirement).
    # brainstorm in particular auto-probes the diff by DEFAULT (no --diff needed), so
    # without this an oversized diff is sent uncapped to every persona every round —
    # the worst token-burn multiplier the 2026-08 investigation found (codex/kimi
    # review finding on this feature's own PR). A piped diff (`diff_from_stdin`) is
    # exempt, matching mode_review's identical exemption.
    #
    # codex review finding (round 2 on this feature's own PR): each of these three
    # modes ALSO caps at its own dispatch boundary (`cap_diff_for_dispatch` is called
    # again inside mode_brainstorm/mode_quorum/mode_just_ask, so a direct library
    # caller bypassing this CLI layer is still protected). Capping HERE and there both
    # is a genuine double-application: harmless at the DEFAULT cap (the first call's
    # output is already <= cap, so the second is a true no-op), but NOT idempotent
    # when `$REVIEW_DIFF_MAX_BYTES` is set below the truncation marker's own length —
    # the second call then re-truncates the FIRST call's marker text and reports ITS
    # byte count as "the full diff", not the real original diff's size. Rather than
    # remove either capping point (the mode-level one is the ONLY guard for a direct
    # caller; this CLI-level one is what `test_cli_brainstorm_oversized_worktree_diff_
    # is_capped_for_dispatch` pins), thread whether THIS layer already capped it so the
    # mode-level call becomes a genuine no-op for the CLI path instead of a second real
    # application — see `diff_already_capped` in `ModeContext.extra` below.
    diff_already_capped = False
    if (
        diff
        and not diff_from_stdin
        and mode.name in ("brainstorm", "quorum", "just-ask")
    ):
        capped_diff = backends.cap_diff_for_dispatch(diff)
        diff_already_capped = capped_diff != diff
        diff = capped_diff

    # --- --visual composition (§2.1). Build the visual context ONCE; thread it into
    # whichever consumer runs. cvGate fires here regardless of mode (a broken render
    # is flagged before any model call). -----------------------------------------
    visual_ctx = None
    # review-cli#587: an explicit `just-ask --timeout N` is a hard wall-clock bound. Armed
    # once, before the first host-wide admission wait of this dispatch (review-cli#614).
    just_ask_deadline_seconds = (
        timeout if mode.name == "just-ask" and args.timeout is not None else None
    )
    deadline_armed = False

    def _planned_panel() -> list[str]:
        # The text panel this dispatch runs after the companion vision call, so admission
        # reserves it up front: brainstorm persona slots; for the review board its exact
        # `-m` roster, else the same availability-aware, reuse-padded selection the board
        # dispatch makes (unavailable leading seats skipped; a `--task -m <one>` review
        # runs that model as several role seats).
        if is_brainstorm:
            return list(brainstorm_pool(models))
        if panel_mode or not board:
            return list(models)
        if bool(explicit_models) and not task_role_pad:
            return [r.model for r in board]
        pool, _ = select_pool_and_reserve_with_reuse(
            board,
            effective_pool_size,
            lambda r: _chain_aware_available(r.model),
            usage_percent=_usage_percent,
        )
        return [r.model for r in pool]

    def _moderators() -> list[str]:
        # --moderator is scoped to the modes that USE a moderator (quorum / brainstorm); a
        # flat panel like just-ask has no `--moderator` on its parser, so read it
        # defensively. No flag -> None -> pick_moderators falls back to the auto-pick
        # chain (harmless: just-ask ignores the resolved moderators anyway).
        moderator_arg = getattr(args, "moderator", None)
        if moderator_arg:
            moderator_arg = _expand_alias(moderator_arg)
        return pick_moderators(moderator_arg, models) if panel_mode else []

    def _moderator_seat() -> list[str]:
        # The moderator a quorum/brainstorm runs after its panel, reserved with it.
        return _moderators()[:1] if mode.name in ("brainstorm", "quorum") else []

    def _vision_seat() -> list[str]:
        # The model the vision fan-out will actually call first (it skips non-vision and
        # unreachable models), so admission reserves that provider, not visual_models[0].
        # None reachable: the pipeline fails closed without a call, so reserve no seat.
        from .features.visual.compose import _ordered_vision_backends

        return _ordered_vision_backends(list(visual_models or []))[:1]

    if visual_mode:
        # qa does NOT consume --visual: the tester drives a LIVE system and produces its OWN
        # proof (screenshots/logs), so an input image has no place in its prompt. Reject it
        # HERE — before the (paid) vision pipeline runs — rather than letting cli.py spend a
        # vision call and the qa handler silently drop the result (review finding). This is a
        # tiny mode-aware guard, not mode-dispatch surgery.
        if mode.name == "qa":
            print(
                "[review-cli] qa: --visual is not supported (the tester produces its OWN "
                "visual proof by driving the SUT). Drop --visual.",
                file=sys.stderr,
                flush=True,
            )
            return 2

        from .features.visual.compose import build_mode_visual_context

        # STANDALONE: --visual with no companion mode AND no diff → the verdict pipeline.
        # NOT recorded in run-stats / no ETA: this is a single-backend vision pipeline
        # (select_vision_backend picks ONE model, and run_pipeline can return before any
        # vision call), not a multi-model / multi-round text panel — recording the whole
        # candidate list as the "pool" would mis-key its history with a bogus pool_size
        # and duration. The ETA store deliberately covers only the slow text panel modes
        # an agent might wrongly short-timeout (codex P2).
        if not panel_mode and not diff.strip():
            from .features.visual.visual_cli import run_visual_standalone

            # One vision call (none with --no-ai): it still takes a host-wide run slot,
            # as the external flock wrapper it replaces did (review-cli#614).
            slot = (
                contextlib.nullcontext()
                if args.no_ai
                else admission.run_slot(mode="visual", models=_vision_seat())
            )
            with slot:
                return run_visual_standalone(
                    args.visual,
                    before=args.before,
                    expect=args.expect,
                    intent=args.intent,
                    requested_checks=list(args.check),
                    models=visual_models,
                    no_ai=args.no_ai,
                    # Stage-2a cost-saver default ON; --no-local-model OR `local_model: false`
                    # in config.yaml disables it (CLI flag wins over config).
                    local_model=(not args.no_local_model)
                    and (config.get("local_model", True) is not False),
                    vision_timeout=args.vision_timeout,
                    as_json=args.json,
                    strict=args.strict,
                    # Per-project module discovery defaults to the CLI cwd (-C), NOT the
                    # process cwd, so `review visual shot.png -C <repo>` finds
                    # <repo>/.review/visual-modules.json (codex P2).
                    project=args.project or str(cwd),
                    effort_override=effort_override,
                )

        # COMPANION: a mode (or the default diff-review) runs WITH the image as context.
        # Validate the board BEFORE the (potentially paid) vision fan-out so an
        # all-malformed `board:` fails fast and never spends a vision call on a config
        # that is going to error anyway (codex P2). Standalone visual already returned
        # above, so this never touches the board-unrelated standalone path.
        rc = validate_board()
        if rc is not None:
            return rc
        # Stage 2: the image is delivered to a vision model (the per-mode fan-out) unless
        # --no-ai, and the grounded observation is folded into the mode prompt. The vision
        # call already spends host + provider capacity, so the run's host-wide slot is
        # taken HERE and held through the text panel that follows (review-cli#614). An
        # explicit `just-ask --timeout N` is armed first so queue time counts toward N,
        # and the slot reserves the full planned panel so admission is not checked
        # against the 1-seat vision call alone.
        if just_ask_deadline_seconds is not None:
            deadline_armed = admission.hold_in_dispatch(
                command_deadline(
                    just_ask_deadline_seconds,
                    command=f"review just-ask --timeout {just_ask_deadline_seconds}",
                )
            )
        if not args.no_ai:
            admission.hold_run_slot(
                mode=mode.stats_mode,
                models=_vision_seat(),
                later_models=admission.routed(_planned_panel()),
                final_models=admission.routed(_moderator_seat()),
                task_code=task_code,
            )
        visual_ctx = _call_with_task_env(
            task_code,
            lambda: build_mode_visual_context(
                Path(args.visual).expanduser(),
                before=Path(args.before).expanduser() if args.before else None,
                expect=args.expect,
                intent=args.intent,
                models=[] if args.no_ai else visual_models,
                requested_checks=list(args.check),
                vision_timeout=args.vision_timeout,
                require_vision=not args.no_ai,
                effort_override=effort_override,
            ),
        )
        # The cvGate pre-filter BLOCKS the companion run on an unambiguously-broken
        # render (codex P2): a blank/unreadable/error-overlay image must short-circuit
        # the mode, not merely be mentioned in prompt text (else `review --staged
        # --visual blank.png` would run the review and stamp success). Exit 10 under
        # --strict (the gate/hook block code), else a non-zero advisory exit.
        if visual_ctx.prefilter_verdict == "rollback":
            print(
                f"[review visual] ROLLBACK (pre-filter, mode blocked): {visual_ctx.prefilter_reason}"
            )
            # An unreadable/missing image is a USAGE error (exit 1), matching the
            # standalone exit-code map — scripts/hooks rely on the distinction between
            # "unreadable input" (1) and "blocking content verdict under --strict" (10).
            if "unreadable" in visual_ctx.prefilter_reason:
                return 1
            return 10 if args.strict else 1
        if visual_ctx.vision_error:
            print(
                f"[review visual] UNVERIFIED (mode blocked): {visual_ctx.vision_error}"
            )
            if visual_ctx.vision_timed_out:
                return 124
            return 10 if args.strict else 1

    # Build the resolved ModeContext handed to the mode's handler (thin over the lib).
    # `with_visual` folds the --visual companion context into the mode's prompt/topic
    # (identity when there is none). Moderators are resolved for the panel/brainstorm
    # modes; the review handler ignores them.
    def _with_visual_text(text: str) -> str:
        return _with_visual(text, visual_ctx)

    moderators = _moderators()
    ctx = ModeContext(
        args=args,
        models=models,
        diff=diff,
        cwd=cwd,
        timeout=timeout,
        with_visual=_with_visual_text,
        visual_ctx=visual_ctx,
        moderators=moderators,
        effort_override=effort_override,
        extra={
            "diff_from_stdin": diff_from_stdin,
            "diff_already_capped": diff_already_capped,
            "stamp_diff_hash": stamp_diff_hash,
            **({"task_roles": panel_task_roles} if panel_task_roles else {}),
        },
    )

    # The recorded mode is the EXACT mode (a brainstorm of 4 is nothing like a plain
    # review of 4), and `pool_models` is what is ACTUALLY DISPATCHED so pool_size is
    # ground truth. A --visual companion is recorded under its base text mode: the
    # vision context above already ran (cvGate + the bounded <=--vision-timeout call),
    # and the multi-minute cost an agent might wrongly short-timeout is the text panel
    # that follows — which IS inside the wrapper below — so the base-mode key is the
    # honest one (codex P2: don't split history on a tag whose timing we exclude).
    if is_brainstorm:
        # Brainstorm dispatches max(3, len(panel)) persona slots PER ROUND, so the real
        # per-round pool — and the ETA key — is the slot count, not len(models) (codex
        # P2: don't undercount a 1-2 model panel). brainstorm_pool mirrors that.
        bs_pool = brainstorm_pool(models)
        bs_roles_kw = {}
        if panel_task_roles:
            # Persist Roles: N/M complete, not the planned list. A 2/3-alive
            # round can still pass; recording the failed slot would falsely
            # satisfy the ship gate (review-cli#433 P1).
            role_coverage: list[tuple[str, str]] = []
            ctx.extra["role_coverage"] = role_coverage
            bs_roles_kw["models_after"] = lambda: [m for m, _ in role_coverage]
            bs_roles_kw["roles_after"] = lambda: [r for _, r in role_coverage]
        return _run_mode_with_stats(
            mode.stats_mode,
            bs_pool,
            lambda: mode.handler(ctx),
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
            later_models=moderators[:1],
            **bs_roles_kw,
        )

    if mode.name == "qa":
        # qa is SINGLE-SEAT: the handler runs exactly ONE write/exec tester (claude default /
        # codex via REVIEW_QA_TESTER / -m), ignoring --pool and the panel. Record its run-stats
        # / ETA under a pool of ONE — not len(DEFAULT_MODELS) — so the ETA store isn't polluted
        # with a fake multi-model pool size (review finding). The recorded seat is the ACTUAL
        # resolved backend, not a model alias.
        from .qa.executor import resolved_tester_backend

        qa_seat = [resolved_tester_backend(_split_models(args.model or []))]
        return _run_mode_with_stats(
            mode.stats_mode,
            qa_seat,
            lambda: mode.handler(ctx),
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
            direct_seats=True,
        )

    if mode.name not in ("review", "visual"):
        # just-ask / quorum: a flat multi-model panel; pool_size == len(models).
        panel_roles_kw = {}
        if panel_task_roles:
            panel_models = list(models)
            panel_roles = list(panel_task_roles)
            panel_roles_kw["models_after"] = lambda: panel_models
            panel_roles_kw["roles_after"] = lambda: panel_roles

        # review-cli#587: an explicit `just-ask --timeout N` is a hard wall-clock bound —
        # every seat call dies at N, the command at N + the grace. It is armed BEFORE the
        # host-wide admission wait inside `_run_mode_with_stats` (review-cli#614), so time
        # spent queued for a slot counts toward N too. A --visual companion armed it
        # already, before its own admission wait; re-arming would push N back.
        deadline = (
            command_deadline(
                just_ask_deadline_seconds,
                command=f"review just-ask --timeout {just_ask_deadline_seconds}",
            )
            if just_ask_deadline_seconds is not None and not deadline_armed
            else contextlib.nullcontext()
        )
        with deadline:
            return _run_mode_with_stats(
                mode.stats_mode,
                models,
                lambda: mode.handler(ctx),
                task_code=task_code,
                repo_id=repo_id,
                diff_files=diff_files,
                diff_sha256=diff_sha256,
                later_models=moderators[:1] if mode.name == "quorum" else None,
                **panel_roles_kw,
            )

    # The review mode. Validate the board now if it wasn't already (the no-visual path);
    # an all-malformed `board:` exits 2 before the panel runs. The --visual companion
    # context folds into each per-reviewer prompt via args.prompt (handler's with_visual).
    rc = validate_board()
    if rc is not None:
        return rc
    if board:
        # Foolproofing (review mode + a NON-EMPTY diff ONLY): bail with a proposal / targeted
        # per-provider error when the live subset can't satisfy the requested pool size,
        # instead of silently running a degenerate panel (reviewlib.pool_guard). Gated on:
        #   * mode.name == "review" — the proposal/error text is review-specific
        #     (`review diff --preset …`, "review pool"), so it must NOT fire for the other
        #     panel modes (quorum / brainstorm / just-ask / visual) that reach this dispatch;
        #   * a non-empty diff — an EMPTY diff runs NO panel (mode_review short-circuits with
        #     "No diff to review", exit 1), so there is no pool to assemble and the guard must
        #     not preempt that no-op with an EXIT_UNSATISFIED. Inert on the happy path + fake
        #     backend.
        if mode.name == "review" and diff.strip():
            # `preset_pool_size(active_preset)`, NOT `preset_pool_size(DEFAULT_PRESET)`
            # (2 review-round finding, all 3 reviewers, after the light-default change):
            # `active_preset` is `None` for a custom config `models:`/`board:` roster —
            # in that case a no-override run ACTUALLY resolves pool via
            # `preset_pool_size(None) == DEFAULT_POOL_SIZE` (4, cli.py ~4218), not the
            # preset system's own default (2). Passing the literal `DEFAULT_PRESET` here
            # would silently halve the guard's promised pool for every config-roster user,
            # contradicting the very comment/help text this same change added elsewhere.
            guard_rc = _evaluate_pool_or_bail(
                config,
                config_models,
                config_has_board,
                _seats_of(board),
                [] if task_role_pad else explicit_models,
                effective_pool_size if task_role_pad else args.pool,
                _config_default_pool(config) or preset_pool_size(active_preset),
                # Reuse-eligible iff the real dispatch below will actually use
                # `select_pool_and_reserve_with_reuse` on this board AND that selector
                # will actually pad it: never for an exact `-m` board (dispatched
                # `exact_board`, no reuse) EXCEPT a recorded `--task` one-model pad
                # (review-cli#309: `-m grok --task` clones that model across 3 roles
                # and must not demand 3 engines); only when the board carries at least
                # one seat with a REAL, resolvable `role_lens` (checked via `role_lens`,
                # not the raw `role` string — codex review finding, task-cli#139: a board
                # whose only non-empty roles are typos/unknown REVIEW_ROLES keys gains
                # nothing from reuse either, same as a role-less `models:` roster,
                # because every seat would fall back to the identical generic prompt —
                # `select_pool_with_reuse` itself now applies this exact check); and
                # only for a POSITIVE `effective_pool_size` (codex review finding,
                # task-cli#139 round 3): `--pool 0`/no-preset means "all reachable
                # seats", and `select_pool_with_reuse` deliberately does NOT pad for
                # `pool <= 0` (see its own `n` computation) — relaxing the floor there
                # would let a single live seat bypass the 2-model convergence floor for
                # a run that ends up with just that ONE seat/role, zero compensating
                # padding. `review_pool_size` below is `effective_pool_size` in this
                # exact branch (not exact `-m`), so this mirrors what actually
                # gets passed down.
                reuse_eligible=(
                    (not explicit_models or task_role_pad)
                    and any(r.role_lens for r in board)
                    and effective_pool_size > 0
                ),
            )
            if guard_rc is not None:
                return guard_rc
        exact_m = bool(explicit_models) and not task_role_pad
        review_pool_size = len(board) if exact_m else effective_pool_size
        # Failover pool. The PLANNED pool keys the up-front ETA: the top `--pool`
        # AVAILABLE seats by priority (startup failover — the same selection mode_review
        # makes). The RECORDED models come from the failover outcome (the seats that
        # actually produced verdicts, after any mid-run backfill), via outcome_sink.
        if exact_m:
            planned_pool = list(board)
        else:
            # Chain-aware (`_chain_aware_available`), matching the pool guard above AND the
            # REAL dispatch split in `modes.review._mode_review_board` — a raw
            # `backend_available` here would let the ETA plan a smaller pool than the guard
            # just approved (codex P1 on review of #157). Reuse-aware
            # (`select_pool_and_reserve_with_reuse`, NOT the plain `split_pool_reserve`) —
            # matching this branch's own `usage_percent=_usage_percent` dispatch below
            # (task-cli#139 P2, codex review): the real dispatch pads a shrunk/degraded
            # board by reusing a live seat across the missing roles, and the planned ETA
            # must reflect that same padded size, not the smaller unpadded one — otherwise
            # a one-live-seat, `--pool 3` review dispatches 3 padded roles but announces
            # and keys its ETA as pool=1.
            planned_pool, _ = select_pool_and_reserve_with_reuse(
                board,
                review_pool_size,
                lambda r: _chain_aware_available(r.model),
                usage_percent=_usage_percent,
            )
        eta_models = [r.model for r in planned_pool]
        outcome_sink: list = []
        # Live per-seat TUI (review-cli#548): an OPTIONAL observer bolted onto the
        # failover board (reviewlib.tui.BoardReporter), auto-enabled only in a genuine
        # interactive terminal and forced off by --no-tui / $REVIEW_NO_TUI. Scoped to
        # mode.name == "review" (the `diff` subcommand) ONLY -- this same `if board:`
        # branch is also reachable for mode.name == "visual", whose own handler never
        # reads ctx.extra["board_reporter"], so starting curses there would be a pointless
        # screen flash for a mode that can't use it. `diff.strip()` gates it the SAME way
        # the pool-guard/foolproofing check above does -- an empty diff never reaches
        # run_board_with_failover (mode_review short-circuits "No diff to review" first),
        # so a no-op run never even flashes a screen.
        board_reporter = None
        if mode.name == "review" and diff.strip():
            from . import tui as _tui

            # Seeded from `planned_pool` (the SAME ETA-planning selection `eta_models`
            # below already relies on), not a separate call -- review finding on this
            # feature's own PR: `_mode_review_board` computes its OWN pool moments later
            # via a second `select_pool_and_reserve_with_reuse` call, and if backend
            # availability flips in that narrow window the two selections could, in
            # principle, diverge. That divergence risk is pre-existing in this codebase
            # (the ETA plan and the real dispatch are always two separate calls, by
            # design -- see the `planned_pool` comment above), not introduced here; the
            # TUI's only NEW consequence of inheriting it is a stale "queued" row for a
            # seat that was planned but never actually dispatched -- cosmetic-only,
            # never a functional/correctness issue (the review's own outcome is governed
            # by the REAL pool `_mode_review_board` computes, never by this seed list).
            # `args.no_tui` always exists on this path (the `diff` subcommand's own
            # parser registers --no-tui, modes/review.py's `_add_arguments`) -- a bare
            # attribute access, not `getattr(..., False)`, so a future rename/wiring
            # regression here fails loudly instead of silently making the flag a no-op.
            # `r.role or "general"` -- MUST match panel._seat_role's exact normalization
            # (the same coalescing `run_board_with_failover`'s own reporter calls apply to
            # a role-less seat -- review finding on this feature's own PR): seeding this
            # initial row under the RAW (possibly falsy) role while dispatch later keys
            # the same physical seat under the normalized one would split one seat into
            # two table rows (a stale seed row frozen at "queued" forever, plus the real
            # row progressing through running/done under the other key).
            board_reporter = _tui.start_if_enabled(
                [(r.role or "general", r.model) for r in planned_pool],
                no_tui_flag=bool(args.no_tui),
            )
        ctx.extra.update(
            board=board,
            pool_size=review_pool_size,
            outcome_sink=outcome_sink,
            exact_board=exact_m,
            # None for an exact `-m` board (exact_board — usage-limit awareness
            # doesn't apply to a hand-picked, non-failover roster) or when a
            # config/default board is priced out entirely — matches this
            # function's own "explicit -m is honored as-is" precedent above.
            # A recorded `--task` one-model pad opts IN so select_pool_with_reuse
            # can fill the cloned role slots (review-cli#309).
            usage_percent=None if exact_m else _usage_percent,
            board_reporter=board_reporter,
        )

        def _ran_models() -> list[str]:
            if exact_m:
                return [r.model for r in board]
            # The BARE model ids that produced verdicts (a backfilled reserve under its
            # real id), so the stat record keys on what actually ran — not labels.
            return outcome_sink[0].usable_models if outcome_sink else []

        def _ran_roles() -> list[str]:
            # review-cli#221: mirrors `_ran_models` exactly, one board ROLE per usable
            # seat (see `panel.FailoverOutcome.usable_roles`) — including a role filled
            # by the shortage-resilience duplicate-model pad (PR #207), which repeats a
            # `.model` string but always carries its OWN distinct `.role`.
            if exact_m:
                return [r.role for r in board]
            return outcome_sink[0].usable_roles if outcome_sink else []

        def _ran_usable_roles() -> list[str]:
            # review-cli#137: SEAT-granular role credit, independent of the whole
            # run's own `passed`/exit code -- every board seat (exact `-m` OR
            # reserve-failover) that individually produced a usable verdict this
            # run, so a sibling seat's hard failure (quota exhaustion, a dead key,
            # a timeout, ...) never zeroes out a genuinely-successful seat's role
            # credit. Unlike `_ran_models`/`_ran_roles` above, this has NO exact-
            # board special case: `FailoverOutcome.usable_roles` already means
            # "seats that individually succeeded" for BOTH an exact board (no
            # reserve -- see `panel.run_board_with_failover`'s docstring) and a
            # reserve-failover board, so the same expression covers both shapes.
            return outcome_sink[0].usable_roles if outcome_sink else []

        try:
            return _run_mode_with_stats(
                mode.stats_mode,
                eta_models,
                lambda: mode.handler(ctx),
                models_after=_ran_models,
                roles_after=_ran_roles,
                usable_roles_after=_ran_usable_roles,
                task_code=task_code,
                repo_id=repo_id,
                diff_files=diff_files,
                diff_sha256=diff_sha256,
                needs_slot=mode.name != "review" or bool(diff.strip()),
            )
        finally:
            # Unconditional teardown (review-cli#548): the TUI must restore the terminal
            # whether the board run succeeded, failed, or raised -- a caller that never
            # started one (board_reporter is None, the overwhelming common case) pays
            # nothing here.
            if board_reporter is not None:
                board_reporter.close()
    # Flat review path (no board): explicit `-m` with no configured board/models. The
    # foolproofing guard STILL applies here — an explicit selection whose live subset can't
    # converge must not silently run a degenerate flat panel either (the board path isn't the
    # only advertised case). The user's selection IS `models` (the flat `-m` list). Gated to
    # the review mode ONLY (the flat dispatch is shared by quorum / brainstorm / just-ask,
    # whose panels keep their own behaviour and never see review-specific proposal text) AND
    # to a non-empty diff (an empty diff runs no panel — see the board-path note above).
    if mode.name == "review" and diff.strip():
        # See the matching `preset_pool_size(active_preset)` comment above — same
        # config-roster-vs-preset-system distinction applies here.
        guard_rc = _evaluate_pool_or_bail(
            config,
            config_models,
            config_has_board,
            tuple((m, m) for m in models),
            explicit_models,
            args.pool,
            _config_default_pool(config) or preset_pool_size(active_preset),
        )
        if guard_rc is not None:
            return guard_rc
    # ctx.extra has no "board" key, so the handler reads board=None and takes the legacy
    # flat call shape.
    return _run_mode_with_stats(
        mode.stats_mode,
        models,
        lambda: mode.handler(ctx),
        task_code=task_code,
        repo_id=repo_id,
        diff_files=diff_files,
        diff_sha256=diff_sha256,
        needs_slot=mode.name != "review" or bool(diff.strip()),
    )


def _seat_reads_repo(model: str, cwd_is_repo: bool) -> bool:
    """True iff this seat's backend runs AGENTICALLY in the real repo (reads any file),
    False if it only sees the diff embedded in the prompt (a raw keyed-HTTP call).

    Agentic backends (codex, opencode, the claude CLI) run read-only inside `-C` and can
    open project files beyond the diff. Raw-API backends (gemini, z.ai, commandcode, and
    the claude API path) are stateless HTTP calls with no workspace, so they review only
    the diff. This is purely for the `--show-board` label — it never affects routing.

    `cwd_is_repo` matters for opencode: it is agentic ONLY when `-C` is a real git repo
    (it falls back to a diff-only isolated temp dir otherwise), so the label mirrors
    `review_opencode`'s own `_opencode_runs_in_repo(cwd)` check rather than claiming every
    `oc:` seat is agentic regardless of where it would run. The caller resolves this bit
    ONCE (a single `git rev-parse`) and passes it in, so labeling N seats stays O(1)
    subprocesses, not O(N)."""
    backend = backends.resolve_backend(model)
    if backend is backends.review_codex:
        return True
    if backend is backends.review_omp:
        # omp runs read-only (`--tools read,grep,glob`) in the real cwd like codex —
        # agentic regardless of whether `-C` is a git repo.
        return True
    if backend is backends.review_opencode:
        return cwd_is_repo
    if backend is backends.review_claude:
        # claude is agentic ONLY via the CLI path; the API path has no workspace.
        mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
        if mode == "api":
            return False
        if mode == "cli":
            return True
        # Auto-pick mirrors the dispatcher: CLI when the binary is present.
        return backends._have_claude_cli()
    return False


def _show_board(
    config: dict,
    pool_size: int = DEFAULT_POOL_SIZE,
    cwd: Path | None = None,
    *,
    preset: str | None = None,
    explicit_models: list[str] | None = None,
    pad_task_roles: bool = False,
) -> int:
    """Print the active reviewer board as a PRIORITY-ordered failover pool.

    The selected board (explicit preset, else config.yaml `models:`/`board:`, else the
    raw built-in DEFAULT_BOARD) is listed in priority order (strongest first). Each seat
    shows its display name, role, model, backend availability (key/CLI present), and a
    failover TIER:
      * `pool`    — one of the top-`pool_size` AVAILABLE seats that a plain `review`
                    actually runs (startup failover: a higher-priority but UNAVAILABLE
                    seat is skipped and the next available one is pulled into the pool);
      * `reserve` — available, below the pool cut; backfills a pool seat that fails
                    mid-run (mid-run failover);
      * `unavail` — backend not reachable right now; it can't sit in the pool, but a
                    run-time "unavailable" reply still triggers a reserve backfill.
    A seat whose role has an active checked-in `.review/lenses/<role>.md` override
    (review-cli#547) is suffixed `(lens overridden: <path>)`; the resolved cwd (this
    function's `cwd`, defaulting to `Path.cwd()`) is the repo root the override path is
    read relative to.
    Read-only — no model is called, no key is printed."""
    config_models = _split_models(config.get("models") or [])
    exact_models = explicit_models or []
    try:
        if exact_models:
            board = _board_from_models_lenient(exact_models, config, preset=preset)
            source = f"explicit -m{f' + preset:{preset}' if preset else ''}"
        elif preset:
            board = load_board(config, preset=preset)
            source = f"preset:{preset}"
        elif config_models:
            board = board_from_models(config_models, config)
            source = "config.yaml (models:)"
        else:
            board = load_board(config)
            source = (
                "config.yaml (board:)"
                if isinstance(config.get("board"), list) and config.get("board")
                else "default"
            )
    except BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2
    # The LIVE pool/reserve split is by PRIORITY + AVAILABILITY (the same split the
    # failover review path makes), not by raw seat index — an unavailable top seat is
    # skipped so the pool fills from the next available priority. Probe each seat ONCE by
    # index (handles a board with the same model in two seats), then walk the available
    # seats in priority order, tagging the first `pool_filled` `pool` and the rest
    # `reserve`. `pool_filled` is how many of the AVAILABLE seats the pool size selects.
    avail = [backends.backend_available(r.model) for r in board]
    available_count = sum(avail)
    exact_board = bool(exact_models)
    # Mirrors the dispatch-side gate (`validate_board`, above): an explicit `-m`
    # model (`exact_board`) always repairs/pads -- the whole point of the
    # single-model pad. A config/default board (no explicit -m) only repairs
    # when it ALREADY carries at least one real `role_lens`; otherwise this
    # display would show a padded/repaired board for a role-less config that
    # the ACTUAL dispatch would never pad (review-cli#309 review finding:
    # `pad_task_roles` was missing this `any(r.role_lens for r in board)` half
    # of the real gate, so `--show-board --task CODE` could display a
    # repaired board for a run that dispatches unpadded).
    if pad_task_roles and (exact_board or any(r.role_lens for r in board)):
        board = ensure_min_distinct_roles(
            board, pool_size if pool_size > 0 else SHIP_MIN_ROLES
        )
        exact_board = False
        avail = [backends.backend_available(r.model) for r in board]
        available_count = sum(avail)
    pool_filled = (
        len(board) if exact_board else _effective_pool_size(available_count, pool_size)
    )
    # The "sized" marker fires iff the RESOLVED pool differs from the selected preset's
    # (or the config-roster fallback's) own default pool — typically an explicit
    # --pool N that isn't equal to that default. It does NOT fire just because a
    # non-bare --preset was passed (a preset's own resolved pool always equals
    # preset_pool_size(that preset), so that clause alone can never trip it) — review
    # finding, comment corrected to match actual behavior, not aspiration. Comparing
    # against the bare constant DEFAULT_POOL_SIZE (4) unconditionally was wrong once a
    # bare `review --show-board` started resolving the light preset's pool of 2 (Alex,
    # 2026-08-28): every unadorned invocation would misleadingly claim to be "sized".
    # `preset_pool_size(None)` already returns DEFAULT_POOL_SIZE (review finding: a
    # prior `if preset else DEFAULT_POOL_SIZE` ternary duplicated that same fallback).
    bare_default_pool = preset_pool_size(preset)
    sized = " (sized by preset/--pool)" if pool_size != bare_default_pool else ""
    if exact_board:
        print(
            f"Reviewer board ({len(board)} explicit seats, source: {source}; "
            "exact -m run = every LIVE listed seat is attempted, --pool is ignored — but if the "
            "live subset can't fill the request the pre-dispatch guard PROPOSES a fitting "
            "board/preset instead of running a degenerate panel):\n"
        )
    else:
        pool_target = (
            "all AVAILABLE seats" if pool_size <= 0 else f"top {pool_size} AVAILABLE"
        )
        print(
            f"Reviewer board ({len(board)} seats, priority-ordered, source: {source}; "
            f"live pool = {pool_target} by priority, distinct roles first{sized}, "
            f"{pool_filled} filled, the rest reserve — size with --pool N):\n"
        )
    name_w = max((len(r.display) for r in board), default=0)
    role_w = max((len(r.role or "general") for r in board), default=0)
    effort_w = max((len(r.effort or "-") for r in board), default=1)
    # Resolve the repo bit ONCE (a single git rev-parse) for the opencode scope label,
    # rather than per seat in the loop.
    effective_cwd = cwd or Path.cwd()
    cwd_is_repo = backends._opencode_runs_in_repo(effective_cwd)
    # Per-project lens overrides (review-cli#547): resolve ONCE per DISTINCT role (not
    # per seat -- DEFAULT_BOARD alone carries "consistency" and "quality" twice each) so
    # a 15-seat board costs at most 8 file reads (len(REVIEW_ROLES)), not 15. Maps role ->
    # the override file's path RELATIVE to the reviewed repo root, for a short, readable
    # label; a role absent from this dict has no active override (missing/unreadable/
    # blank file, or a role that isn't a real REVIEW_ROLES key -- see
    # `lens_override_text`'s docstring for why each of those counts as "no override").
    overridden_roles: dict[str, Path] = {}
    for r in dict.fromkeys(seat.role for seat in board if seat.role):
        override_text = lens_override_text(r, effective_cwd)
        if override_text:
            overridden_roles[r] = lens_override_path(r, effective_cwd).relative_to(
                effective_cwd
            )
    # review-cli#588: the pool is the AVAILABLE seats in priority order with distinct roles
    # first (`prefer_distinct_roles`), the same pick the dispatch makes — a seat repeating a
    # role an earlier pool seat already covers is listed as reserve.
    pool_ids = {
        id(r)
        for r in prefer_distinct_roles([r for i, r in enumerate(board) if avail[i]])[
            :pool_filled
        ]
    }
    for index, reviewer in enumerate(board):
        available = avail[index]
        if available:
            status = "available"
        elif backends.runtime_provider_marked_unpaid(reviewer.model):
            status = (
                "will attempt (provider unpaid/disabled)"
                if exact_board
                else "SKIPPED (provider unpaid/disabled)"
            )
        else:
            status = (
                "will attempt (no key/CLI)" if exact_board else "SKIPPED (no key/CLI)"
            )
        role = (reviewer.role or "general").ljust(role_w)
        effort = (reviewer.effort or "-").ljust(effort_w)
        if exact_board:
            tier = "explicit"
        elif not available:
            tier = "unavail"
        else:
            tier = "pool   " if id(reviewer) in pool_ids else "reserve"
        prio = f"#{index + 1}"
        scope = (
            "agentic" if _seat_reads_repo(reviewer.model, cwd_is_repo) else "diff-only"
        )
        lens_note = (
            f"  (lens overridden: {overridden_roles[reviewer.role]})"
            if reviewer.role in overridden_roles
            else ""
        )
        print(
            f"  {prio:>3}  [{tier}]  {reviewer.display.ljust(name_w)}  {role}  "
            f"{reviewer.model}  [{status}]  ({scope})  effort={effort}{lens_note}"
        )
    print(
        "\nScope: `agentic` seats (codex / opencode / omp / claude-CLI) run read-only in the "
        "real repo and can read any project file; `diff-only` seats (gemini / z.ai / "
        "commandcode / claude-API) are stateless HTTP calls that see only the diff."
    )
    if exact_board:
        print(
            "\nWith explicit `-m`, review-cli attempts exactly the listed models in order. "
            "`--pool` and reserve failover do not slice or reorder explicit seats."
        )
    elif pool_size <= 0:
        print(
            "\nA plain `review diff` runs all AVAILABLE seats by priority (--pool 0); "
            "a higher-priority seat that is unavailable is skipped, and a seat that "
            "fails mid-run is recorded while the remaining available seats continue. "
            "`--pool N` sizes the pool."
        )
    else:
        print(
            f"\nA plain `review diff` runs the top {pool_size} AVAILABLE seats by "
            f"priority, distinct roles first (--pool {pool_size}); a seat that is "
            f"unavailable (or fails mid-run) is replaced by a seat that keeps its role (a "
            f"same-role reserve, else a reserve re-lensed onto the missing role, else a "
            f"model that already answered) so the pool keeps {pool_size} working reviewers "
            f"on distinct roles. `--pool N` sizes the pool; `--pool 0` runs all available seats."
        )
    if not all(avail):
        print(
            "Unavailable reviewers drop out and are backfilled from the reserve; the "
            "board degrades gracefully only if the reserve is exhausted. The default "
            "agentic seats (`oc:…` Kimi/Qwen/DeepSeek/Grok, `omp:…` GLM/MiMo/Sonnet-omp, "
            "codex, claude) need their CLI on PATH — `oc:` seats need the `opencode` binary "
            "plus its own provider auth "
            "(`opencode auth login`), NOT review-cli's COMMANDCODE_API_KEY/ZAI_API_KEY. "
            "gemini needs GEMINI_API_KEY. (COMMANDCODE_API_KEY powers only the diff-only "
            "`commandcode:` backend for `-m cc`; ZAI_API_KEY powers the diff-only `zai:` "
            "backend for `-m glm` and omp:zai/* seats that have no omp zai login.)"
        )
    if overridden_roles:
        overridden_list = ", ".join(
            f"{role} ({path})" for role, path in overridden_roles.items()
        )
        print(
            "\nProject lens overrides active (review-cli#547): "
            f"{overridden_list}. Each file's text is APPENDED to that role's built-in "
            "lens, never replacing it; delete/empty the file to fall back to the "
            "built-in lens alone."
        )
    from .role_models import format_role_fallback_section

    print(format_role_fallback_section())
    return 0


def _with_visual(text: str, visual_ctx) -> str:
    """Fold the --visual composition context into a mode's prompt/question/topic.

    This is the composition seam (§2.1): the image's described context (and cvGate
    outcome) is appended so the companion mode reasons about the render. The full
    per-call multimodal fan-out (routing each model call through call_ai_vision with
    the image attached) is Stage 2; this is where it plugs in."""
    if visual_ctx is None:
        return text
    return text + visual_ctx.context_note


# Re-export for legacy callers that imported subprocess off the entry module.
__all__ = ["main", "subprocess"]
