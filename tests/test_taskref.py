"""Task-code backend validation via agenttools_taskref (review-cli#427)."""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import reviewlib.cli as cli
import reviewlib.taskref as taskref
from reviewlib.stats import normalize_task_code

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_TOOLS_LIB = REPO_ROOT.parents[2] / "agent-tools" / "lib"


class _Skip(Exception):
    pass


def _ensure_agenttools_taskref_on_path() -> None:
    if not (AGENT_TOOLS_LIB / "agenttools_taskref").is_dir():
        raise _Skip("agenttools_taskref checkout not present")
    lib_root = str(AGENT_TOOLS_LIB)
    if lib_root not in sys.path:
        sys.path.insert(0, lib_root)
    try:
        import agenttools_taskref  # noqa: F401
    except ImportError as exc:
        raise _Skip(f"agenttools_taskref not importable: {exc}") from exc


def test_resolve_task_code_shape_only_when_validation_disabled():
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    os.environ["REVIEW_TASKREF_VALIDATE"] = "0"
    try:
        assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"
    finally:
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_resolve_task_code_uses_monkeypatch_hook():
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    os.environ["REVIEW_TASKREF_VALIDATE"] = "1"
    old_hook = taskref.validate_task_code_hook

    def _hook(code: str, repo_root: Path) -> str:
        assert code == "607"
        assert repo_root == REPO_ROOT.resolve()
        return "607"

    taskref.validate_task_code_hook = _hook
    try:
        assert taskref.resolve_task_code("607", REPO_ROOT) == "607"
    finally:
        taskref.validate_task_code_hook = old_hook
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_resolve_task_code_permissive_when_lib_missing():
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    os.environ["REVIEW_TASKREF_VALIDATE"] = "1"
    old_lib = taskref._taskref_lib
    taskref._taskref_lib = lambda: None
    try:
        assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"
    finally:
        taskref._taskref_lib = old_lib
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_resolve_task_code_rejects_invented_code():
    _ensure_agenttools_taskref_on_path()
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    old_hook = taskref.validate_task_code_hook
    os.environ["REVIEW_TASKREF_VALIDATE"] = "1"
    taskref.validate_task_code_hook = None
    try:
        try:
            taskref.resolve_task_code("TEST-1", REPO_ROOT)
        except ValueError as exc:
            assert "task find" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        taskref.validate_task_code_hook = old_hook
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_cli_rejects_fabricated_task_code_end_to_end():
    _ensure_agenttools_taskref_on_path()
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    old_hook = taskref.validate_task_code_hook
    os.environ["REVIEW_TASKREF_VALIDATE"] = "1"
    taskref.validate_task_code_hook = None
    try:
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
        assert rc == 2
        assert "invalid --task CODE" in err.getvalue()
        lowered = err.getvalue().lower()
        assert "task find" in lowered or "github" in lowered
    finally:
        taskref.validate_task_code_hook = old_hook
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_help_works_without_agenttools_taskref():
    old = os.environ.get("REVIEW_TASKREF_VALIDATE")
    old_lib = taskref._taskref_lib
    os.environ["REVIEW_TASKREF_VALIDATE"] = "1"
    taskref._taskref_lib = lambda: None
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                rc = cli.main(["--help"])
            except SystemExit as exc:
                rc = exc.code
        assert rc in (0, None)
        assert "review" in out.getvalue().lower()
    finally:
        taskref._taskref_lib = old_lib
        if old is None:
            os.environ.pop("REVIEW_TASKREF_VALIDATE", None)
        else:
            os.environ["REVIEW_TASKREF_VALIDATE"] = old


def test_normalize_task_code_still_shape_only():
    assert normalize_task_code("  427  ") == "427"
    try:
        normalize_task_code("has spaces inside")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


if __name__ == "__main__":
    failures = 0
    skipped = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except SystemExit as exc:
                failures += 1
                print(f"ERROR {name}: unexpected SystemExit({exc.code})")
            except _Skip as exc:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(
        f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s), {skipped} skipped"
    )
    sys.exit(1 if failures else 0)
