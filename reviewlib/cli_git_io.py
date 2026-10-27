"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import Callable, Path, argparse

def _is_git_repo(_api, /, cwd: Path) -> bool:
    """Cheap, correct "is `cwd` inside a git work tree?" probe.

    `git rev-parse --is-inside-work-tree` is the canonical, fast check (exit 0 + `true`
    inside a work tree, non-zero outside). The whole point of this probe is to AVOID a raw
    traceback, so every way the spawn itself can blow up is caught and treated as "not a
    repo": OSError (a non-existent / non-directory `cwd` -> FileNotFoundError /
    NotADirectoryError, e.g. a stale `-C /missing/path`) and TimeoutExpired (a wedged `git
    rev-parse` -> `_run` forwards `timeout=` straight to subprocess.run, which raises). `_run`
    is `text=True, stdout=PIPE`, so `proc.stdout` is always a str (never None)."""
    try:
        proc = _api._run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def _fail_not_a_repo(_api, /, cwd: Path) -> int:
    """Print the 3-part WHAT/WHY/HOW message for "ran the diff review outside a repo" and
    return the stable EXIT_NOT_A_REPO code. Verb-named because it has a side effect (prints)
    AND returns the code. No traceback — this is an expected user error, not a crash."""
    print(
        f"[review-cli] not in a git repository ({cwd}).\n"
        "  the diff review needs a repo to diff (it reviews your working-tree / staged changes).\n"
        '  fix: run a mode that needs no git — `review just-ask "..." --task CODE` / '
        '`review quorum "..." --task CODE` / `review brainstorm "..." --task CODE` — '
        "or cd into a repo and re-run.",
        file=_api.sys.stderr,
        flush=True,
    )
    return _api.EXIT_NOT_A_REPO


def _fail_git_diff(_api, /, cwd: Path, exc: Exception) -> int:
    """Print a structured error for "in a git repo, but `git diff` failed" (the REQUIRED
    review path) and return the stable EXIT_GIT_DIFF_FAILED code. `_is_git_repo` passing does
    NOT guarantee `git diff` succeeds (a wedged/timed-out git, a corrupt index), so this is
    the no-traceback floor for that path — an expected runtime failure, not a crash."""
    print(
        f"[review-cli] could not read the git diff in {cwd}.\n"
        f"  git diff failed: {exc}\n"
        "  fix: check the repo is healthy (`git status`), or pipe a diff on stdin "
        "(`git diff | review diff --task CODE`).",
        file=_api.sys.stderr,
        flush=True,
    )
    return _api.EXIT_GIT_DIFF_FAILED


def _git_diff(_api, /, cwd: Path, staged: bool) -> str:
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
        proc = _api._run(args, cwd=cwd, env=_api.git_repo_env(cwd), timeout=120)
    except (OSError, _api.subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git diff could not run: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return proc.stdout


def _stamp_hash_for_staged_diff(_api, /, cwd: Path) -> str | None:
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
        proc = _api._run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    import hashlib

    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _git_remote_origin_url(_api, /, cwd: Path) -> str | None:
    """`git -C cwd remote get-url origin`, or None on ANY failure (no remote, not a
    repo, no git binary, a wedge) — best-effort, this only feeds an identity label,
    never a required path."""
    try:
        proc = _api._run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _git_toplevel(_api, /, cwd: Path) -> Path | None:
    """`git -C cwd rev-parse --show-toplevel`, or None on any failure (not a repo,
    no git binary, a wedge)."""
    try:
        proc = _api._run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _api.Path(proc.stdout.strip())


def _compute_repo_id(_api, /, cwd: Path) -> str | None:
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
    url = _api._git_remote_origin_url(cwd)
    if url:
        normalized = _api.normalize_repo_remote(url)
        if normalized:
            return normalized
    toplevel = _api._git_toplevel(cwd)
    if toplevel is not None:
        return f"path:{toplevel}"
    try:
        if cwd.is_dir():
            return f"path:{cwd}"
    except OSError:
        pass
    return None


def _default_branch_ref(_api, /, cwd: Path) -> str | None:
    """Best-effort `origin/<default-branch>` ref for `cwd`, or None.

    Tries the recorded `origin/HEAD` symref first (what `git clone` sets up), then
    falls back to probing the two common default-branch names directly — a shallow
    CI checkout or a repo cloned with `--single-branch` may lack `origin/HEAD`
    entirely. Feeds `--check`'s diff-identity file-set comparison for the common
    post-push case (clean working tree, nothing to diff locally); never raises.
    """
    try:
        proc = _api._run(
            ["git", "-C", str(cwd), "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().replace("refs/remotes/", "")
    for candidate in ("origin/main", "origin/master"):
        try:
            proc = _api._run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", candidate],
                cwd=cwd,
                env=_api.git_repo_env(cwd),
                timeout=10,
            )
        except (OSError, _api.subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return candidate
    return None


def _diff_name_only(_api, /, cwd: Path, ref_args: list[str]) -> list[str] | None:
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
        proc = _api._run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--name-only", *ref_args],
            cwd=cwd,
            env=_api.git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, _api.subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _current_diff_files_for_check(_api, /, cwd: Path) -> list[str] | None:
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
    local = set(_api._diff_name_only(cwd, ["HEAD"]) or [])
    default_ref = _api._default_branch_ref(cwd)
    branch = (
        set(_api._diff_name_only(cwd, [f"{default_ref}...HEAD"]) or [])
        if default_ref
        else set()
    )
    union = sorted(local | branch)
    return union or None


def _stdin_wait_seconds(_api, /) -> float:
    """How long a non-tty stdin may stay silent before it counts as "nothing piped"
    (`$REVIEW_STDIN_WAIT_SECONDS`, default `STDIN_WAIT_DEFAULT_SECONDS`). A pipe producer
    (`git diff | review diff`) delivers its first bytes or EOF within milliseconds, so a
    few seconds of silence means the writer is a long-lived parent that will never
    write (review-cli#516/#613). Non-numeric / non-finite -> the default; negative -> 0."""
    raw = _api.os.environ.get(_api.STDIN_WAIT_ENV)
    if raw is None or not raw.strip():
        return _api.STDIN_WAIT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _api.STDIN_WAIT_DEFAULT_SECONDS
    if not _api.math.isfinite(value):
        return _api.STDIN_WAIT_DEFAULT_SECONDS
    return max(value, 0.0)


def _stdin_unusable(_api, /) -> bool:
    """True when stdin is absent, already closed, or a tty — nothing to read in any case.
    A daemonizer may close the Python-level `sys.stdin` (not just redirect fd 0), and
    `isatty()` on a closed stream raises, so the `closed` check comes first."""
    stdin = _api.sys.stdin
    return stdin is None or getattr(stdin, "closed", False) or stdin.isatty()


def _stdin_fd(_api, /) -> int | None:
    """fd 0 when stdin is a usable, non-tty stream; None otherwise, including a file-like
    without an fd (a test's StringIO — which cannot block, so the callers read it
    directly)."""
    if _api._stdin_unusable():
        return None
    try:
        return _api.sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        return None


def _stdin_notice(_api, /, text: str) -> None:
    print(f"[review-cli] {text}", file=_api.sys.stderr, flush=True)


def _decode_stdin(_api, /, raw: bytes) -> str:
    """Decode raw stdin bytes with stdin's encoding, always replacing undecodable bytes:
    a stray invalid byte in a piped diff must not raise out of `_dispatch` (the stream's
    own `errors` is normally "strict")."""
    encoding = getattr(_api.sys.stdin, "encoding", None) or "utf-8"
    return raw.decode(encoding, "replace")


def _chunk_reader(_api, fd, idle, pollable):
    if pollable is None:
        try:
            _api.select.select([fd], [], [], 0)
            pollable = True
        except (OSError, ValueError):
            pollable = False
    return _api._polled_chunks(fd, idle) if pollable else _api._threaded_chunks(fd, idle)


def _read_fd_with_idle_timeout(_api, /, fd: int, idle: float, *, pollable: bool | None) -> bytes | None:
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
    next_chunk = _chunk_reader(_api, fd, idle, pollable)
    chunks: list[bytes] = []
    total_bytes = 0
    while True:
        try:
            chunk = next_chunk()
        except OSError as exc:
            got = total_bytes
            _api._stdin_notice(
                f"stdin read failed after {got} bytes ({exc.strerror or exc}) — treating"
                " it as no piped input."
            )
            return None
        if chunk is None:
            got = total_bytes
            arrived = f"only {got} bytes" if got else "nothing"
            _api._stdin_notice(
                f"stdin is not a terminal but {arrived} arrived and it stayed silent for"
                f" {idle:g}s — treating it as no piped input (tune with {_api.STDIN_WAIT_ENV})."
            )
            return None
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total_bytes += len(chunk)


def _polled_chunks(_api, /, fd: int, idle: float) -> Callable[[], bytes | None]:
    """Chunk source over `select` (contract in `_read_fd_with_idle_timeout`)."""

    def _next() -> bytes | None:
        ready, _, _ = _api.select.select([fd], [], [], idle)
        if not ready:
            return None
        return _api.os.read(fd, _api._STDIN_CHUNK)

    return _next


def _threaded_chunks(_api, /, fd: int, idle: float) -> Callable[[], bytes | None]:
    """Chunk source for an unpollable fd: a daemon thread blocks in `os.read` and hands
    each chunk (or the OSError it hit) over a queue; the caller waits at most `idle`
    (floored at `_THREADED_WAIT_FLOOR`) per chunk. Once the caller gives up, the thread
    stops at its next wakeup instead of draining the stream for the life of the process
    (no unbounded buffer, and the writer sees backpressure)."""
    import queue
    import threading

    handoff: queue.Queue[bytes | OSError] = queue.Queue()
    stop = threading.Event()
    wait = max(idle, _api._THREADED_WAIT_FLOOR)

    def _drain() -> None:
        while not stop.is_set():
            try:
                item: bytes | OSError = _api.os.read(fd, _api._STDIN_CHUNK)
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


def _read_stdin_if_piped(_api, /) -> str | None:
    """Return the diff piped on stdin, or None when stdin is a tty / empty / silent.

    The read is BOUNDED (review-cli#516, #613): an inherited non-tty stdin whose writer
    stays open but never writes (an omp eval kernel's socket, a detached launcher's pipe,
    `pty=false` agent sessions) used to park `sys.stdin.read()` forever — before any
    dispatch, log or heartbeat, while holding the host-wide review lock. Now the fd is
    read with an idle timeout of `_stdin_wait_seconds()` between bytes
    (`_read_fd_with_idle_timeout`); a stall -> one stderr line and "nothing piped"."""
    if _api._stdin_unusable():
        return None
    fd = _api._stdin_fd()
    if fd is None:
        # No fd to poll: a file-like substituted for stdin (tests use StringIO). Such an
        # object cannot block on a pipe, so the plain read is safe here.
        try:
            data = _api.sys.stdin.read()
        except OSError:
            return None
        return data if data else None
    raw = _api._read_fd_with_idle_timeout(fd, _api._stdin_wait_seconds())
    if not raw:
        return None
    return _api._decode_stdin(raw)


def _warn_if_stdin_ignored(_api, /, source_flag: str, what: str) -> None:
    """With an explicit `--staged` / `--diff` source, stdin is never read — but a caller
    who piped a diff anyway (`git show X | review diff --staged`) must hear that it was
    ignored rather than silently get a review of the index. A zero-timeout poll plus a
    one-byte read tells data (warn) apart from EOF (`< /dev/null`, `true |`: silent)
    without ever blocking; the byte is discarded like the rest of the stream."""
    fd = _api._stdin_fd()
    if fd is None:
        return
    try:
        ready, _, _ = _api.select.select([fd], [], [], 0)
        if not ready:
            return
        peek = _api.os.read(fd, 1)
    except (OSError, ValueError):
        return
    if peek:
        _api._stdin_notice(
            f"ignoring piped stdin: {source_flag} names the diff source, so the {what}"
            " diff is reviewed."
        )


def _explicit_diff_source(_api, /, args: argparse.Namespace) -> tuple[str, str] | None:
    """`(flag, what)` for an explicitly named diff source — `("--staged", "staged")` or
    `("--diff", "working-tree")` — or None when the verb may take a diff from stdin.
    The top-level parser (no subcommand) defines neither flag, hence `getattr`."""
    if getattr(args, "staged", False):
        return "--staged", "staged"
    if getattr(args, "diff", False):
        return "--diff", "working-tree"
    return None


def _effective_cwd(_api, /, raw: str, *, warn: bool) -> Path:
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
    resolved = _api.Path(raw).expanduser().resolve()
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
            proc = _api._run(
                ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                cwd=resolved,
                env=_api.git_repo_env(resolved),
                timeout=10,
            )
        except (OSError, _api.subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return _api.Path(proc.stdout.strip())
    if warn:
        print(
            f"[review-cli] warning: {resolved} is not inside a git repository; "
            "reviewing it as-is — pass -C <project-root> to point review at your repo.",
            file=_api.sys.stderr,
            flush=True,
        )
    return resolved
