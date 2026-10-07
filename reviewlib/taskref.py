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
- the repo declares no backend and its ``origin`` is not a GitHub remote (or there is no
  ``origin``). The lib's zero-config default is ``github-issues`` resolved from ``origin``,
  so a Forgejo-hosted repo without a ``task:`` block would also have every code rejected.

A malformed ``task.yaml`` still goes to the lib, which fails closed on it.

When the lib is **not** installed: **permissive fallback** — shape-only
``normalize_task_code`` (the behavior before #427). This matches how other optional
``agenttools_*`` imports in this repo degrade (``agenttools_rig_delegate``,
``agenttools_service``): a missing lib never crashes ``review --help``; validation runs only
when the lib is present.

Test seam: set ``REVIEW_TASKREF_VALIDATE=0`` to skip backend validation so the suite can keep
synthetic placeholders (``TEST-1``, ``CODE``, …) without hitting ``gh`` or a real tracker
(``tests/conftest.py`` and ``tests/smoke.py`` set it). Tests may also monkeypatch
``validate_task_code_hook``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

from .process import git_repo_env
from .stats import normalize_task_code

# Monkeypatch target for unit tests. When set, called instead of the lib-backed path.
validate_task_code_hook: Callable[[str, Path], str] | None = None

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_GIT_TIMEOUT_S = 5
# SCP-like git remote (`[user@]host:path`, no `://`), e.g. `git@github.com:o/r.git`.
_SCP_LIKE_REMOTE_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):")


def _backend_validation_enabled() -> bool:
    raw = os.environ.get("REVIEW_TASKREF_VALIDATE", "1")
    return str(raw).strip().lower() in _TRUTHY


def _taskref_lib() -> ModuleType | None:
    """Best-effort import of ``agenttools_taskref``; None when not installed."""
    try:
        import agenttools_taskref
    except ImportError:
        return None
    return agenttools_taskref


def taskref_available() -> bool:
    """True when backend validation can run (lib importable and not disabled)."""
    return _backend_validation_enabled() and _taskref_lib() is not None


def _yaml_mapping(path: Path) -> dict | None:
    """``path`` parsed as a YAML mapping; None when missing, unreadable or not a mapping."""
    if not path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable file declares nothing here
        return None
    return data if isinstance(data, dict) else None


def _declared_backend(repo_root: Path) -> str | None:
    """The tracker backend the repo declares, read the way the lib reads it: ``rig.yaml``'s
    ``task.backend``, overridden by ``task.yaml``'s ``backend``. None when neither says."""
    backend = None
    rig = _yaml_mapping(repo_root / "rig.yaml")
    task_block = rig.get("task") if rig else None
    if isinstance(task_block, dict) and "backend" in task_block:
        backend = str(task_block["backend"])
    task_yaml = _yaml_mapping(repo_root / "task.yaml")
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
    """Why the lib cannot judge task codes for ``repo_root``, or None when it can."""
    backend = _declared_backend(repo_root)
    if backend is not None:
        if backend in lib.VALID_BACKENDS:
            return None
        return f"agenttools_taskref has no {backend!r} tracker backend"
    try:
        cfg = lib.load_repo_task_config(repo_root)
    except lib.TaskRefError:
        return None  # malformed task.yaml: lib.validate() fails closed on it
    if cfg.backend != "github-issues" or cfg.github_repo != "auto":
        return None
    host = _origin_host(repo_root)
    if host == "github.com":
        return None
    if host is None:
        return "no task backend configured and no git origin remote"
    return f"no task backend configured and origin ({host}) is not GitHub"


def _validate_with_lib(code: str, repo_root: Path) -> str:
    lib = _taskref_lib()
    if lib is None:
        return code
    reason = _unvalidatable_reason(lib, repo_root)
    if reason is not None:
        print(
            f"[review-cli] task code {code!r} checked for shape only: {reason}",
            file=sys.stderr,
            flush=True,
        )
        return code
    result = lib.validate(code, repo_root)
    if not result.ok:
        raise ValueError(result.reason)
    return code


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
    hook = validate_task_code_hook
    if hook is not None:
        return hook(code, root)
    return _validate_with_lib(code, root)
