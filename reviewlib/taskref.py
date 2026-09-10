"""Backend-aware ``--task CODE`` validation via ``agenttools_taskref`` (review-cli#427).

When the shared ``agenttools_taskref`` lib is installed, ``resolve_task_code`` confirms a
candidate code against the repo's tracker backend (GitHub issues / Linear team prefix)
before it is exported to ``$REVIEW_TASK_CODE`` or recorded in ``run-stats.jsonl``.

When the lib is **not** installed: **permissive fallback** — shape-only
``normalize_task_code`` (today's behavior). This matches how other optional
``agenttools_*`` imports in this repo degrade (``agenttools_rig_delegate``,
``agenttools_service``): missing lib never crashes ``review --help``; validation runs
only when the lib is present.

Test seam: set ``REVIEW_TASKREF_VALIDATE=0`` to skip backend validation so the suite can
keep synthetic placeholders (``TEST-1``, ``CODE``, …) without hitting ``gh`` or a real
tracker. Tests may also monkeypatch ``validate_task_code_hook``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .stats import normalize_task_code

if TYPE_CHECKING:
    from agenttools_taskref import ValidationResult

# Monkeypatch target for unit tests. When set, called instead of the lib-backed path.
validate_task_code_hook: Callable[[str, Path], str] | None = None

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _backend_validation_enabled() -> bool:
    raw = os.environ.get("REVIEW_TASKREF_VALIDATE", "1")
    return str(raw).strip().lower() in _TRUTHY


def _taskref_lib():
    """Best-effort import of ``agenttools_taskref``; None when not installed."""
    try:
        import agenttools_taskref
    except ImportError:
        return None
    return agenttools_taskref


def taskref_available() -> bool:
    """True when backend validation can run (lib importable and not disabled)."""
    return _backend_validation_enabled() and _taskref_lib() is not None


def _validate_with_lib(code: str, repo_root: Path) -> str:
    lib = _taskref_lib()
    if lib is None:
        return code
    result: ValidationResult = lib.validate(code, repo_root)
    if not result.ok:
        raise ValueError(result.reason)
    return result.canonical or code


def resolve_task_code(
    raw: str | None,
    repo_root: Path | str | None = None,
) -> str | None:
    """Normalize ``raw`` and optionally validate against ``repo_root``.

    Returns the canonical task code, ``None`` when absent/blank, or raises
    ``ValueError`` with the library's task-find/task-new guidance on rejection.
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
