"""Task-code tracker validation via agenttools_taskref (review-cli#427).

Covers the three postures of ``reviewlib.taskref.resolve_task_code``:

- lib absent: shape-only fallback, ``review --help`` still works;
- lib present, Forgejo-backed repo: ``#N`` codes are accepted (the lib has no ``forgejo``
  backend, so tracker validation is skipped with a note) and shape errors still reject;
- lib present, a backend it knows: an invented code is rejected, end-to-end through
  ``review diff --task``.

Each posture runs against a stand-in ``agenttools_taskref`` module (always, so CI covers it)
and, when the real lib is importable, against the real lib too. The real lib is found by a
normal import, or under ``$AGENT_TOOLS_LIB`` (the agent-tools checkout's ``lib`` dir, for
runs with a redirected ``HOME`` such as tests/smoke.py).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from reviewlib import cli, taskref
from reviewlib.stats import normalize_task_code

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = "agenttools_taskref"
_FORGEJO_ORIGIN = "https://git.hyperide.ai/ultrabricks/review-cli.git"
_GITHUB_ORIGIN = "https://github.com/alex-mextner/review-cli.git"


class _Skip(Exception):
    pass


@contextmanager
def _env(**values: str | None):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _validation_on():
    """Validation enabled, no hook, stats to a throwaway file."""
    old_hook = taskref.validate_task_code_hook
    taskref.validate_task_code_hook = None
    with tempfile.TemporaryDirectory() as stats_dir, _env(
        REVIEW_TASKREF_VALIDATE="1",
        REVIEW_STATS_FILE=str(Path(stats_dir) / "run-stats.jsonl"),
    ):
        try:
            yield
        finally:
            taskref.validate_task_code_hook = old_hook


@contextmanager
def _lib_module(module: types.ModuleType | None):
    """Install ``module`` as ``agenttools_taskref`` (None = import fails) for the block."""
    missing = object()
    old = sys.modules.get(_LIB, missing)
    sys.modules[_LIB] = module  # type: ignore[assignment]
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop(_LIB, None)
        else:
            sys.modules[_LIB] = old  # type: ignore[assignment]


def _real_lib() -> types.ModuleType:
    extra = os.environ.get("AGENT_TOOLS_LIB")
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)
    try:
        import agenttools_taskref
    except ImportError as exc:
        msg = f"agenttools_taskref not importable: {exc}"
        if os.environ.get("PYTEST_CURRENT_TEST"):
            import pytest

            pytest.skip(msg)
        raise _Skip(msg) from exc
    return agenttools_taskref


class _StubRefError(RuntimeError):
    pass


def _stub_lib(calls: list[str]) -> types.ModuleType:
    """A stand-in with the real lib's public surface and its two-backend limit: its config
    loader refuses a ``forgejo`` rig.yaml like the real one, it accepts only digits
    (github-issues), and it records every ``validate()`` call."""
    mod = types.ModuleType(_LIB)
    mod.VALID_BACKENDS = frozenset({"github-issues", "linear"})
    mod.TaskRefError = _StubRefError

    def load_repo_task_config(repo_root):
        rig = Path(repo_root) / "rig.yaml"
        if rig.is_file() and "forgejo" in rig.read_text(encoding="utf-8"):
            raise _StubRefError("backend must be one of ['github-issues', 'linear']")
        return types.SimpleNamespace(
            backend="github-issues", github_repo="auto", linear_team="", sources=()
        )

    def validate(code, repo_root):
        calls.append(code)
        ok = code.lstrip("#").isdigit()
        reason = "resolved" if ok else f"{code!r} is not real. Use `task find` or `task new`."
        return types.SimpleNamespace(
            ok=ok, reason=reason, canonical=code.lstrip("#"), backend="github-issues"
        )

    mod.load_repo_task_config = load_repo_task_config
    mod.validate = validate
    return mod


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


@contextmanager
def _repo(*, origin: str | None, files: dict[str, str]):
    """A committed throwaway git repo with ``files`` and an optional ``origin`` remote."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(repo, "init", "-q")
        if origin:
            _git(repo, "remote", "add", "origin", origin)
        for name, text in files.items():
            (repo / name).write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(
            repo, "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "--allow-empty", "-m", "init",
        )
        yield repo


def _forgejo_repo():
    return _repo(origin=_FORGEJO_ORIGIN, files={"rig.yaml": "task:\n  backend: forgejo\n"})


def _review_diff(code: str, repo: Path) -> tuple[int, str]:
    """``review diff --task CODE -C repo`` in-process; (rc, stdout+stderr). The repos here
    have no changes, so a code that passes validation ends at "No diff to review"."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["diff", "--task", code, "-C", str(repo)])
    return rc, out.getvalue() + err.getvalue()


# ── lib absent ──────────────────────────────────────────────────────────────────────────────


def test_lib_absent_is_shape_only():
    with _validation_on(), _lib_module(None):
        assert taskref._taskref_lib() is None
        assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"
        assert taskref.resolve_task_code("#608", REPO_ROOT) == "#608"
        try:
            taskref.resolve_task_code("has spaces inside", REPO_ROOT)
        except ValueError as exc:
            assert "one non-whitespace token" in str(exc)
        else:
            raise AssertionError("expected the shape check to reject")


def test_lib_absent_help_works():
    with _validation_on(), _lib_module(None):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                rc = cli.main(["--help"])
            except SystemExit as exc:
                rc = exc.code
        assert rc in (0, None)
        assert "review" in out.getvalue().lower()


def test_lib_absent_review_diff_accepts_any_shaped_code():
    with _validation_on(), _lib_module(None), _forgejo_repo() as repo:
        rc, text = _review_diff("TEST-1", repo)
        assert "invalid --task CODE" not in text, text
        assert "No diff to review" in text, text
        assert rc == 1


# ── lib present, Forgejo-backed repo ────────────────────────────────────────────────────────


def test_stub_lib_forgejo_code_accepted_without_calling_validate():
    calls: list[str] = []
    err = io.StringIO()
    with _validation_on(), _lib_module(_stub_lib(calls)), _forgejo_repo() as repo, redirect_stderr(
        err
    ):
        assert taskref.resolve_task_code("#608", repo) == "#608"
    assert calls == []
    assert "shape only" in err.getvalue() and "'forgejo'" in err.getvalue()


def test_stub_lib_forgejo_shape_error_still_rejects():
    with _validation_on(), _lib_module(_stub_lib([])), _forgejo_repo() as repo:
        try:
            taskref.resolve_task_code("has spaces inside", repo)
        except ValueError as exc:
            assert "one non-whitespace token" in str(exc)
        else:
            raise AssertionError("expected the shape check to reject")


def test_stub_lib_unconfigured_non_github_origin_is_shape_only():
    """No ``task:`` block and a Forgejo origin: the lib's github default can't resolve, so
    it would reject every code. Same for a repo with no origin at all."""
    for origin in (_FORGEJO_ORIGIN, None):
        calls: list[str] = []
        with _validation_on(), _lib_module(_stub_lib(calls)), _repo(
            origin=origin, files={}
        ) as repo, redirect_stderr(io.StringIO()):
            assert taskref.resolve_task_code("#608", repo) == "#608"
        assert calls == [], origin


def test_real_lib_forgejo_code_accepted():
    lib = _real_lib()
    with _validation_on(), _lib_module(lib), _forgejo_repo() as repo:
        # The lib itself rejects every code on a forgejo repo — the reason for the skip.
        assert not lib.validate("#608", repo).ok
        with redirect_stderr(io.StringIO()):
            assert taskref.resolve_task_code("#608", repo) == "#608"
            assert taskref.resolve_task_code("608", repo) == "608"
        rc, text = _review_diff("#608", repo)
        assert "invalid --task CODE" not in text, text
        assert "checked for shape only" in text, text
        assert "No diff to review" in text, text
        assert rc == 1


def test_real_lib_this_repo_accepts_its_forgejo_codes():
    """review-cli itself is Forgejo-backed (rig.yaml task.backend: forgejo, #585)."""
    lib = _real_lib()
    with _validation_on(), _lib_module(lib), redirect_stderr(io.StringIO()):
        assert taskref.resolve_task_code("#427", REPO_ROOT) == "#427"


def test_real_lib_github_backend_with_forgejo_origin_is_shape_only():
    """A declared ``github-issues`` backend that resolves its repo from a Forgejo origin:
    the lib alone rejects every code ("cannot resolve github repo")."""
    lib = _real_lib()
    with _validation_on(), _lib_module(lib), _repo(
        origin=_FORGEJO_ORIGIN, files={"task.yaml": "backend: github-issues\n"}
    ) as repo:
        assert not lib.validate("#608", repo).ok
        err = io.StringIO()
        with redirect_stderr(err):
            assert taskref.resolve_task_code("#608", repo) == "#608"
        assert "origin (git.hyperide.ai) is not GitHub" in err.getvalue()


def test_stub_lib_raising_degrades_to_shape_only():
    lib = _stub_lib([])

    def _boom(code, repo_root):
        raise RuntimeError("tracker exploded")

    lib.validate = _boom
    err = io.StringIO()
    with _validation_on(), _lib_module(lib), _repo(origin=_GITHUB_ORIGIN, files={}) as repo:
        with redirect_stderr(err):
            assert taskref.resolve_task_code("#608", repo) == "#608"
        try:
            taskref.resolve_task_code("has spaces inside", repo)
        except ValueError as exc:
            assert "one non-whitespace token" in str(exc)
        else:
            raise AssertionError("expected the shape check to reject")
    assert "agenttools_taskref failed: RuntimeError: tracker exploded" in err.getvalue()


def _expect_rejection(code: str, repo: Path, needle: str) -> None:
    try:
        taskref.resolve_task_code(code, repo)
    except ValueError as exc:
        assert needle in str(exc), exc
    else:
        raise AssertionError(f"expected {code!r} to be rejected")


def test_stub_lib_github_no_verdict_is_shape_only_everything_else_rejects():
    """Only the lib's "could not verify" answers (gh missing/offline) get the shape check.
    An ``ok=False`` with an empty reason, unknown wording, or no ``backend`` is a
    rejection; an answer without ``ok`` breaks the contract and gets the shape check."""
    lib = _stub_lib([])

    def _gh(ok, reason, **extra):
        return types.SimpleNamespace(ok=ok, reason=reason, backend="github-issues", **extra)

    answers = {
        "#601": _gh(False, "gh CLI not found on PATH; cannot verify GitHub issue/PR numbers"),
        "#602": _gh(False, "gh api call failed: PermissionError: denied"),
        "#603": _gh(False, "could not verify via gh api (HTTP 502)"),
        "#604": object(),
        "#605": _gh(False, ""),
        "#606": _gh(False, "#606 does not exist; run `task new` to create it"),
        "#607": types.SimpleNamespace(ok=False, reason="could not verify via gh api (x)"),
    }
    lib.validate = lambda code, repo_root: answers[code]
    err = io.StringIO()
    with _validation_on(), _lib_module(lib), _repo(
        origin=_GITHUB_ORIGIN, files={}
    ) as repo, redirect_stderr(err):
        for code in ("#601", "#602", "#603", "#604"):
            assert taskref.resolve_task_code(code, repo) == code
        _expect_rejection("#605", repo, "rejected by agenttools_taskref")
        _expect_rejection("#606", repo, "does not exist")
        _expect_rejection("#607", repo, "could not verify")  # no backend: fail closed
    assert err.getvalue().count("GitHub gave no verdict") == 3
    assert "agenttools_taskref failed: AttributeError" in err.getvalue()


def test_real_lib_github_verdicts_via_fake_gh():
    """The real lib against a fake ``gh`` (no network): missing ``gh`` and a non-404 error
    get the shape check; a 404 and an invented slug are rejected with guidance."""
    import shutil

    lib = _real_lib()
    git = shutil.which("git")
    assert git
    with tempfile.TemporaryDirectory() as bindir, _validation_on(), _lib_module(lib), _repo(
        origin=_GITHUB_ORIGIN, files={}
    ) as repo, _env(PATH=bindir):
        os.symlink(git, Path(bindir) / "git")
        gh = Path(bindir) / "gh"

        def _fake_gh(stderr: str) -> None:
            gh.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit 1\n", encoding="utf-8")
            gh.chmod(0o755)

        err = io.StringIO()
        with redirect_stderr(err):
            assert taskref.resolve_task_code("#608", repo) == "#608"  # no gh at all
            _fake_gh("HTTP 502: Bad Gateway")
            assert taskref.resolve_task_code("#609", repo) == "#609"
        assert "GitHub gave no verdict: gh CLI not found" in err.getvalue()
        assert "GitHub gave no verdict: could not verify via gh api" in err.getvalue()
        _fake_gh("HTTP 404: Not Found (https://api.github.com/repos/x/y/issues/99999)")
        _expect_rejection("#99999", repo, "does not exist")
        _expect_rejection("ambiguous-create-dedup", repo, "task find")


def test_stub_lib_taskref_error_from_validate_fails_closed():
    lib = _stub_lib([])

    def _malformed(code, repo_root):
        raise _StubRefError("cannot read/parse task.yaml")

    lib.validate = _malformed
    with _validation_on(), _lib_module(lib), _repo(origin=_GITHUB_ORIGIN, files={}) as repo:
        try:
            taskref.resolve_task_code("#608", repo)
        except ValueError as exc:
            assert "cannot read/parse task.yaml" in str(exc)
        else:
            raise AssertionError("expected a malformed config to fail closed")


def test_real_lib_malformed_task_yaml_fails_closed():
    lib = _real_lib()
    with _validation_on(), _lib_module(lib), _repo(
        origin=_GITHUB_ORIGIN, files={"task.yaml": "backend: [unclosed\n"}
    ) as repo:
        try:
            taskref.resolve_task_code("#608", repo)
        except ValueError as exc:
            assert "task.yaml" in str(exc), exc
        else:
            raise AssertionError("expected a malformed task.yaml to fail closed")


# ── lib present, an invalid code ────────────────────────────────────────────────────────────


def test_stub_lib_invalid_code_rejected_end_to_end():
    calls: list[str] = []
    with _validation_on(), _lib_module(_stub_lib(calls)), _repo(
        origin=_GITHUB_ORIGIN, files={}
    ) as repo:
        try:
            taskref.resolve_task_code("ambiguous-create-dedup", repo)
        except ValueError as exc:
            assert "task find" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        assert taskref.resolve_task_code("#427", repo) == "#427"  # as written, not canonical
        rc, text = _review_diff("ambiguous-create-dedup", repo)
        assert rc == 2
        assert "invalid --task CODE" in text and "task find" in text, text
    assert calls == ["ambiguous-create-dedup", "#427", "ambiguous-create-dedup"]


def test_real_lib_invented_code_rejected_end_to_end():
    """A free-text slug — the invented-code shape agent-tools#607 found in run-stats — is
    rejected by the lib's shape check before any `gh` call, so no network is needed."""
    _real_lib()
    task_yaml = "backend: github-issues\ngithub:\n  repo: alex-mextner/review-cli\n"
    with _validation_on(), _repo(origin=_GITHUB_ORIGIN, files={"task.yaml": task_yaml}) as repo:
        try:
            taskref.resolve_task_code("ambiguous-create-dedup", repo)
        except ValueError as exc:
            assert "not a GitHub issue/PR reference" in str(exc)
            assert "task find" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        rc, text = _review_diff("ambiguous-create-dedup", repo)
        assert rc == 2
        assert "invalid --task CODE" in text and "task find" in text, text


def test_real_lib_linear_wrong_team_rejected():
    _real_lib()
    task_yaml = "backend: linear\nlinear:\n  team: HYP\n"
    with _validation_on(), _repo(origin=None, files={"task.yaml": task_yaml}) as repo:
        assert taskref.resolve_task_code("HYP-742", repo) == "HYP-742"
        try:
            taskref.resolve_task_code("TEST-1", repo)
        except ValueError as exc:
            assert "team prefix" in str(exc)
        else:
            raise AssertionError("expected ValueError")


# ── test seam ───────────────────────────────────────────────────────────────────────────────


def test_validation_disabled_is_shape_only():
    calls: list[str] = []
    with _validation_on(), _lib_module(_stub_lib(calls)), _env(REVIEW_TASKREF_VALIDATE="0"):
        assert taskref.resolve_task_code("TEST-1", REPO_ROOT) == "TEST-1"
    assert calls == []


def test_hook_replaces_lib():
    def _hook(code: str, repo_root: Path) -> str:
        assert code == "607"
        assert repo_root == REPO_ROOT.resolve()
        return "607"

    with _validation_on():
        taskref.validate_task_code_hook = _hook
        assert taskref.resolve_task_code("607", REPO_ROOT) == "607"


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
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s), {skipped} skipped")
    sys.exit(1 if failures else 0)
