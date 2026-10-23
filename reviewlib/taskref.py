"""Backend-aware ``--task CODE`` validation via ``agenttools_taskref`` (review-cli#427).

When the shared ``agenttools_taskref`` lib is installed, ``resolve_task_code`` confirms a
candidate code against the repo's tracker before it is exported to ``$REVIEW_TASK_CODE`` or
recorded in ``run-stats.jsonl``. A rejected code raises ``ValueError`` carrying the lib's own
reason (with its ``task find`` / ``task new`` guidance). An accepted code is returned as the
caller wrote it (after shape normalization), not as the lib's canonical form: run-stats keys
and ``review task CODE --check`` lookups keep matching the spelling ``gh ship`` passes.

The lib knows two backends, ``github-issues`` and ``linear``. Where it cannot judge a code,
tracker validation is skipped and the code gets the shape-only check, with a one-line note
on stderr saying why:

- the repo declares a backend the lib lacks (``rig.yaml`` ``task.backend: forgejo``, as this
  repo does; Forgejo codes look like ``#608``). The lib would reject every code there with
  "backend must be one of ...";
- the lib's ``github-issues`` backend resolves the repo from ``origin`` (its zero-config
  default, ``github.repo: auto``) and ``origin`` is not a GitHub remote, or there is none.
  The lib would reject every code there, so a Forgejo-hosted repo without a ``task:``
  block would have every code rejected too;
- GitHub gave no verdict: the lib checks GitHub codes with ``gh api`` and, when ``gh`` is
  missing, failing, unauthenticated or offline, answers ``ok=False`` with a "could not
  verify" message (matched by its known prefixes, ``_UNVERIFIED_PREFIXES``). Every other
  ``ok=False`` (bad shape, a 404, unknown wording) is a rejection;
- the lib raises, or returns something without ``ok``, against its contract.

A malformed ``task.yaml`` still goes to the lib, which fails closed on it.

When the lib is **not** installed: **permissive fallback** — shape-only
``normalize_task_code`` (the behavior before #427). This matches how other optional
``agenttools_*`` imports in this repo degrade (``agenttools_rig_delegate``,
``agenttools_service``): a missing lib never crashes ``review --help``; validation runs only
when the lib is present.

Opt-out: ``REVIEW_TASKREF_VALIDATE=0`` (also ``false``/``no``/``off``) skips backend validation
and leaves the shape check. The suite uses it so synthetic placeholders (``TEST-1``, ``CODE``, …)
never reach ``gh`` or a real tracker (``tests/conftest.py`` and ``tests/smoke.py`` set it).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

from .process import git_repo_env
from .stats import normalize_task_code

_FALSY = frozenset({"0", "false", "no", "off"})
_GIT_TIMEOUT_S = 5
# SCP-like git remote (`[user@]host:path`, no `://`), e.g. `git@github.com:o/r.git`.
_SCP_LIKE_REMOTE_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):")
# How the lib's github-issues answers start when `gh` could not give a verdict (missing,
# failing, unauthenticated, offline): `_check_github_issue`'s exists=None messages. Any
# other ok=False answer (bad shape, a 404, wording this list doesn't know) is a rejection.
_UNVERIFIED_PREFIXES = ("gh CLI not found", "gh api call failed", "could not verify")


def _backend_validation_enabled() -> bool:
    """On unless ``REVIEW_TASKREF_VALIDATE`` is an explicit off value: a typo in the knob
    must not silently disable the check."""
    return os.environ.get("REVIEW_TASKREF_VALIDATE", "1").strip().lower() not in _FALSY


def _taskref_lib() -> ModuleType | None:
    """Best-effort import of ``agenttools_taskref``; None when not installed."""
    try:
        import agenttools_taskref
    except ImportError:
        return None
    return agenttools_taskref


def _yaml_mapping(path: Path) -> dict | None:
    """``path`` parsed as a YAML mapping (an empty file is an empty mapping, as in the lib);
    None when missing, unreadable or not a mapping."""
    if not path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable file declares nothing here
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def _declared_backend(repo_root: Path) -> str | None:
    """The tracker backend the repo declares, read the way the lib reads it: ``rig.yaml``'s
    ``task.backend``, overridden by ``task.yaml``'s ``backend``. None when neither says.
    Only consulted to name a backend the lib's own config loader refused."""
    backend = None
    rig = _yaml_mapping(repo_root / "rig.yaml")
    task_block = rig.get("task") if rig else None
    if isinstance(task_block, dict) and "backend" in task_block:
        backend = str(task_block["backend"])
    task_path = repo_root / "task.yaml"
    task_yaml = _yaml_mapping(task_path)
    if task_yaml is None and task_path.is_file():
        return None  # malformed task.yaml: the lib's refusal is a config error, not a backend gap
    if task_yaml and "backend" in task_yaml:
        backend = str(task_yaml["backend"])
    return backend


def _origin_host(repo_root: Path) -> str | None:
    """Hostname of ``repo_root``'s git ``origin`` remote; None when there is none."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=git_repo_env(repo_root),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return None
    scp = None if "://" in url else _SCP_LIKE_REMOTE_RE.match(url)
    host = scp.group("host") if scp else urlparse(url).hostname
    return host.lower() if host else None


def _unvalidatable_reason(lib: ModuleType, repo_root: Path) -> str | None:
    """Why the lib cannot judge task codes for ``repo_root``, or None when it can.

    The lib's own ``load_repo_task_config`` decides the backend. Its ``TaskRefError`` covers
    both a backend it lacks and a malformed config; only the first is skipped here, the
    second goes on to ``lib.validate()``, which fails closed on it."""
    try:
        cfg = lib.load_repo_task_config(repo_root)
    except lib.TaskRefError:
        backend = _declared_backend(repo_root)
        if backend is not None and backend not in lib.VALID_BACKENDS:
            return f"agenttools_taskref has no {backend!r} tracker backend"
        return None
    if cfg.backend != "github-issues" or cfg.github_repo != "auto":
        return None
    host = _origin_host(repo_root)
    if host == "github.com":
        return None
    if host is None:
        return "github-issues backend resolves its repo from origin, and there is no origin"
    return f"github-issues backend resolves its repo from origin, and origin ({host}) is not GitHub"


def _shape_only(code: str, reason: str) -> str:
    print(
        f"[review-cli] task code {code!r} checked for shape only: {reason}",
        file=sys.stderr,
        flush=True,
    )
    return code


def _validate_with_lib(code: str, repo_root: Path) -> str:
    lib = _taskref_lib()
    if lib is None:
        return code
    try:
        reason = _unvalidatable_reason(lib, repo_root)
        if reason is not None:
            return _shape_only(code, reason)
        result = lib.validate(code, repo_root)
        if result.ok:
            return code
    except lib.TaskRefError as exc:
        # validate() turns a malformed config into ok=False itself; should a later lib
        # version raise it instead, it still means "malformed config": fail closed.
        raise ValueError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - optional lib whose contract is never-raise
        return _shape_only(
            code, f"agenttools_taskref failed: {type(exc).__name__}: {exc}"
        )
    # ok=False from here on: a rejection unless it is GitHub's "could not verify". Read
    # with getattr so an odd result object still fails closed rather than open.
    rejection = str(getattr(result, "reason", None) or "rejected by agenttools_taskref")
    if getattr(result, "backend", None) == "github-issues" and rejection.startswith(
        _UNVERIFIED_PREFIXES
    ):
        return _shape_only(code, f"GitHub gave no verdict: {rejection}")
    raise ValueError(rejection)


def resolve_task_code(
    raw: str | None,
    repo_root: Path | str | None = None,
) -> str | None:
    """Normalize ``raw`` and validate it against ``repo_root``'s tracker when possible.

    Returns the normalized task code, ``None`` when absent/blank, or raises ``ValueError``
    (a shape error, or the library's task-find/task-new guidance on rejection).
    """
    code = normalize_task_code(raw)
    if code is None:
        return None
    if not _backend_validation_enabled():
        return code
    root = Path(repo_root).resolve() if repo_root is not None else Path.cwd()
    return _validate_with_lib(code, root)
