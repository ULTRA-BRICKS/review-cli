"""What a run-stats record describes: repository, diff and task identity.

Pure helpers shared by the writer, the readers and the quorum gate (split out of
``reviewlib.stats``, review-cli#721): the diff hash and file set, the normalized
repository identity (with the rename/move alias table), the task-code grammar and its
GitHub-issue twin spellings, and the deterministic run id. No I/O, no store access.
"""

from __future__ import annotations

import hashlib
import re

_TASK_CODE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,120}$")
# All-digit GitHub-issue codes, optional leading '#'. ASCII digits only: Python's
# ``\d`` is Unicode, which would treat a fullwidth digit as an issue number.
_GITHUB_ISSUE_TASK_RE = re.compile(r"^#?([0-9]+)$")

# `diff --git a/<path> b/<path>` header. Paths containing spaces/special chars are
# C-quoted by git (`"a/weird\tname"`) and NOT unescaped here — extraction degrades
# to a best-effort miss for those rare paths rather than a wrong path, which is
# fine for this module's purpose (file-SET overlap, not an exact manifest).
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
# `git@host:org/repo(.git)` SCP-style SSH remote syntax.
_SCP_STYLE_REMOTE_RE = re.compile(r"^[^@/]+@([^:/]+):(.+)$")


# Former repository identities -> the identity the repository moved to. A repo that
# moved hosts or was renamed keeps its review/quorum history this way instead of
# splitting into two logical repositories (review-cli#589). Keys are lowercase
# normalized ``host/owner/repo`` ids. Record a new move as one more row here; a row
# may point at an id that later moved again (lookups follow the chain).
_REPO_ID_ALIASES: dict[str, str] = {
    # The alex-mextner GitHub mirrors moved to ultrabricks Forgejo.
    "github.com/alex-mextner/agent-tools": "git.hyperide.ai/ultrabricks/agent-tools",
    "github.com/alex-mextner/dev-cli": "git.hyperide.ai/ultrabricks/dev-cli",
    "github.com/alex-mextner/review-cli": "git.hyperide.ai/ultrabricks/review-cli",
    "github.com/alex-mextner/rig-cli": "git.hyperide.ai/ultrabricks/rig-cli",
    "github.com/alex-mextner/task-cli": "git.hyperide.ai/ultrabricks/task-cli",
    "github.com/alex-mextner/tg-cli": "git.hyperide.ai/ultrabricks/tg-cli",
    # sme-archiving-gc lived on Forgejo as sme-archiving-gc-tmp, then moved back to
    # GitHub on 2026-09-29.
    "git.hyperide.ai/ultrabricks/sme-archiving-gc-tmp": (
        "github.com/alex-mextner/sme-archiving-gc"
    ),
}


def diff_content_hash(diff_text: str) -> str:
    """Sha256 hex digest of the exact reviewed diff text (diagnostic-only; NOT part
    of the mismatch-detection gate — see the module docstring)."""
    return hashlib.sha256(
        diff_text.encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def extract_diff_files(diff_text: str) -> list[str]:
    """Sorted, deduped file paths touched by a unified git diff's ``a/``/``b/`` sides.

    Best-effort: pure regex over ``diff --git a/<path> b/<path>`` headers, no full
    diff parse. Both sides are collected (not just the post-image) so a rename or a
    delete (``b/`` side ``/dev/null``) still credits the pre-image path. Returns []
    for an empty/header-less diff — never raises.
    """
    files: set[str] = set()
    for a_path, b_path in _DIFF_GIT_HEADER_RE.findall(diff_text or ""):
        for p in (a_path, b_path):
            if p and p != "/dev/null":
                files.add(p)
    return sorted(files)


def normalize_repo_remote(url: str) -> str | None:
    """Normalize a git remote URL to a host/org/repo identity string, or None if
    ``url`` is empty/unparseable.

    Strips credentials, protocol, a trailing ``.git``, and trailing slashes so the
    SAME remote reached over https vs ssh, with or without an embedded token,
    normalizes to one identical id — e.g. ``https://x-access-token:ghp_abc@
    github.com/org/repo.git`` and ``git@github.com:org/repo.git`` both become
    ``github.com/org/repo``. Also lowercases the host (DNS hostnames are
    case-insensitive; ``GitHub.com`` and ``github.com`` are the same remote) and
    drops an explicit default SSH port (``:22``) so ``ssh://git@host:22/org/repo``
    matches the portless ``git@host:org/repo`` form (codex/fable review findings on
    this feature's own PR — both were real "same repo, spurious repo_mismatch"
    false-positive gaps in the ORIGINAL cut of this function; a NON-default port is
    intentionally NOT stripped since it plausibly names a different remote). A
    former identity listed in ``_REPO_ID_ALIASES`` maps to the repository's current
    one, so history recorded before a rename or host move still matches. This
    is an identity key for cross-repo mismatch detection, not a URL a caller should
    try to clone from.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    scp = _SCP_STYLE_REMOTE_RE.match(raw)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        # Strip a protocol scheme (https://, git://, ssh://, http://) if present.
        no_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
        # Drop embedded credentials (user[:pass]@host/...).
        no_creds = re.sub(r"^[^/@]+@", "", no_scheme)
        parts = no_creds.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        host, path = parts
    host = host.strip().rstrip("/").lower()
    host = re.sub(r":22$", "", host)  # default SSH port -> same remote as portless
    path = path.strip().strip("/")
    path = path.removesuffix(".git")
    if not host or not path:
        return None
    identity = f"{host}/{path}"
    # Follow a chain of moves to the current id. Keys and values are lowercase, so
    # an aliased id comes back lowercase; `seen` stops a cyclic table.
    seen: set[str] = set()
    while (key := identity.lower()) in _REPO_ID_ALIASES and key not in seen:
        seen.add(key)
        identity = _REPO_ID_ALIASES[key]
    return identity


def _comparable_repo_id(repo_id: str) -> str:
    """``repo_id`` normalized for comparison. An id that isn't a parseable remote
    (an opaque local ``path:`` identity) is kept as-is, so it stays distinct."""
    return normalize_repo_remote(repo_id) or repo_id


def _recorded_repo_id(item: dict) -> str | None:
    """The iteration's recorded repository as ``_comparable_repo_id`` gives it, or
    None when the record carries no ``repo_id`` (identity unverifiable)."""
    item_repo = item.get("repo_id")
    if not isinstance(item_repo, str) or not item_repo:
        return None
    return _comparable_repo_id(item_repo)


def normalize_task_code(value: str | None) -> str | None:
    """Return a safe task code for stats/log metadata, or None when absent.

    Task codes are identifiers, not prose: one non-whitespace token, no control
    characters. Preserve case because external trackers may be case-sensitive.
    A leading ``#`` is kept as written (``#105`` stays ``#105``); GitHub-issue
    all-digit codes are aliased at READ time — see ``_github_issue_task_keys``.
    """
    if value is None:
        return None
    code = str(value).strip()
    if not code:
        return None
    if not _TASK_CODE_RE.match(code):
        raise ValueError(
            "task code must be one non-whitespace token, max 120 characters"
        )
    return code


def _github_issue_twin(code: str) -> str | None:
    """Other spelling of an all-digit GitHub-issue task code, else None.

    ``105`` ↔ ``#105``. Non-numeric codes (``HYP-742``) have no twin —
    ``#HYP-742`` is a different key. Used by the dual-key READ path so history
    recorded under either spelling is visible to the other (review-cli#389).
    Does not rewrite records.
    """
    match = _GITHUB_ISSUE_TASK_RE.fullmatch(code)
    if match is None:
        return None
    digits = match.group(1)
    hashed = f"#{digits}"
    return digits if code.startswith("#") else hashed


def _github_issue_task_keys(code: str) -> frozenset[str]:
    """Lookup keys for one task code: itself, plus the ``#`` twin if any."""
    twin = _github_issue_twin(code)
    if twin is None:
        return frozenset({code})
    return frozenset({code, twin})


def run_id_for(ts: str, mode: str, task_code: str | None) -> str:
    """Short, deterministic id for one run-stats record, used to key overseer
    feedback (``review feedback <run_id> ...`` / ``reviewlib.dashboard.store``).

    Deterministic from (ts, mode, task_code) rather than random so a caller who
    already has those three fields (e.g. re-deriving the id for an OLD record
    that predates this field) can recompute the exact same id without re-reading
    the record itself. Collisions are not a security concern here (see
    ``record_run``'s own threat-model note) — worst case two runs that started at
    the exact same microsecond with the same mode/task share an id, which just
    means feedback left under one also shows up for the other.

    16 hex chars (64 bits), not fewer: a review finding on the original 10-hex-char
    (40-bit) cut found a ~36% birthday-collision chance at a realistic one-million-run
    history -- silently misattributing feedback between two unrelated runs. 64 bits
    needs billions of runs before that risk becomes non-negligible, while staying
    short enough to read/type/paste from the end-of-run nudge.
    """
    digest = hashlib.sha256(f"{ts}|{mode}|{task_code or ''}".encode()).hexdigest()
    return digest[:16]
