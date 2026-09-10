"""Task-code backend validation via agenttools_taskref (review-cli#427)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import reviewlib.cli as cli
import reviewlib.taskref as taskref
from reviewlib.stats import normalize_task_code

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_TOOLS_LIB = REPO_ROOT.parents[2] / "agent-tools" / "lib"


def _ensure_agenttools_taskref_on_path() -> None:
    lib_root = str(AGENT_TOOLS_LIB)
    if lib_root not in sys.path:
        sys.path.insert(0, lib_root)


@pytest.fixture
def enable_taskref_validation(monkeypatch):
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "1")
    taskref.validate_task_code_hook = None
    _ensure_agenttools_taskref_on_path()
    yield
    taskref.validate_task_code_hook = None


def test_resolve_task_code_shape_only_when_validation_disabled(monkeypatch):
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "0")
    assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"


def test_resolve_task_code_uses_monkeypatch_hook(monkeypatch):
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "1")

    def _hook(code: str, repo_root: Path) -> str:
        assert code == "607"
        assert repo_root == REPO_ROOT.resolve()
        return "607"

    taskref.validate_task_code_hook = _hook
    assert taskref.resolve_task_code("607", REPO_ROOT) == "607"


def test_resolve_task_code_permissive_when_lib_missing(monkeypatch):
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "1")
    monkeypatch.setattr(taskref, "_taskref_lib", lambda: None)
    assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"


def test_resolve_task_code_rejects_invented_code(enable_taskref_validation):
    with pytest.raises(ValueError, match="task find"):
        taskref.resolve_task_code("TEST-1", REPO_ROOT)


def test_cli_rejects_fabricated_task_code_end_to_end(enable_taskref_validation, capsys):
    rc = cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "invalid --task CODE" in captured.err
    assert "task find" in captured.err.lower() or "github" in captured.err.lower()


def test_help_works_without_agenttools_taskref(monkeypatch, capsys):
    monkeypatch.setenv("REVIEW_TASKREF_VALIDATE", "1")
    monkeypatch.setattr(taskref, "_taskref_lib", lambda: None)
    rc = cli.main(["--help"])
    assert rc == 0
    assert "review" in capsys.readouterr().out.lower()


def test_normalize_task_code_still_shape_only():
    assert normalize_task_code("  427  ") == "427"
    with pytest.raises(ValueError):
        normalize_task_code("has spaces inside")
