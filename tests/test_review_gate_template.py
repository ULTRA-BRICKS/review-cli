"""The composer-stage review gate (`install.REVIEW_GATE`, review-cli#396) driven through REAL
`git commit` / `rebase --continue` / `cherry-pick --continue` with the gate installed as the
repo's `core.hooksPath` pre-commit — the way rig wires it on every machine.

Two things these tests pin that the `_PRECOMMIT`-only suite (`test_review_stamp_delta_tolerance`)
cannot:

* The gate rig actually installs (`review install-commit-hook --print-gate`) carries the
  trivial-delta tolerance AND the merge/rebase/cherry-pick skip. Before #396 the installed body
  was a hand-ported copy in agent-tools that had neither, so a one-line restage after a stamped
  review demanded a fresh full review and every conflict-resolving merge commit needed
  `--no-verify`.
* The in-progress markers are resolved with `git rev-parse --git-path`, so the skip also works
  in a LINKED worktree (where `MERGE_HEAD` lives under `.git/worktrees/<name>/`, not `.git/`).

Repos are created under `<repo>/.test-repos/` (gitignored), NOT pytest's `tmp_path`: the gate
deliberately exempts repos under the OS temp dirs (`$TMPDIR`, `/tmp`, `/var/folders`, ...), and
macOS's `tmp_path` lives in `/private/var/folders`, where every assertion below would pass
vacuously. Same convention as agent-tools' `tests/test_global_review_gate.py`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from reviewlib import install  # noqa: E402
from reviewlib import _version as _version_mod  # noqa: E402
from reviewlib._version import parse_project_version  # noqa: E402
from reviewlib.cli import main  # noqa: E402

_SCRATCH_ROOT = _ROOT / ".test-repos"
_BASE_LINES = [f"line{i}" for i in range(20)]


def _env(home: Path) -> dict[str, str]:
    """A subprocess env that cannot see the developer's real git config, review knobs, or a
    leaked repo pin — the gate under test must be the ONLY hook in play."""
    env = dict(os.environ)
    for name in (
        "REVIEW_SKIP",
        "REVIEW_TRIVIAL_DELTA_LINES",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["GIT_CONFIG_GLOBAL"] = str(home / "empty-gitconfig")
    env["GIT_CONFIG_SYSTEM"] = str(home / "empty-gitconfig")
    env["GIT_EDITOR"] = "true"
    return env


def _git(
    repo: Path, *args: str, env: dict[str, str], check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=check
    )


def _install_gate(hooks_dir: Path, body: str) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text(body, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


@contextmanager
def _gated_repo(body: str = install.REVIEW_GATE):
    """A real repo OUTSIDE the temp-dir exemption whose `core.hooksPath` pre-commit IS `body`.
    Yields (repo, env). Every setup commit goes through `--no-verify` (git's blanket bypass)
    so only the commit under test exercises the gate."""
    base = _SCRATCH_ROOT / uuid.uuid4().hex
    home = base / "home"
    home.mkdir(parents=True)
    repo = base / "repo"
    repo.mkdir()
    env = _env(home)
    try:
        _git(repo, "init", "-q", "-b", "main", env=env)
        _git(repo, "config", "user.email", "t@t", env=env)
        _git(repo, "config", "user.name", "t", env=env)
        _git(repo, "config", "commit.gpgsign", "false", env=env)
        _install_gate(base / "hooks", body)
        _git(repo, "config", "core.hooksPath", str(base / "hooks"), env=env)
        yield repo, env
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write(repo: Path, name: str, lines: list[str]) -> None:
    (repo / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_commit(
    repo: Path, env: dict[str, str], name: str, lines: list[str], msg: str
) -> str:
    _write(repo, name, lines)
    _git(repo, "add", name, env=env)
    _git(repo, "commit", "-q", "--no-verify", "-m", msg, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env).stdout.strip()


def _staged_diff(repo: Path, env: dict[str, str]) -> str:
    return _git(repo, "diff", "--no-ext-diff", "--cached", env=env).stdout


def _stamp_staged(repo: Path, env: dict[str, str]) -> None:
    """What a passing `review diff --staged` leaves behind: the hash stamp + the reviewed
    diff text the trivial-delta block measures drift against."""
    install._write_review_stamp(repo, _staged_diff(repo, env))


def _conflicting_branches(repo: Path, env: dict[str, str]) -> None:
    """main and `feature` both rewrite line5 of a.py differently on top of a shared base."""
    _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
    _git(repo, "checkout", "-q", "-b", "feature", env=env)
    _setup_commit(
        repo,
        env,
        "a.py",
        [ln if ln != "line5" else "feature5" for ln in _BASE_LINES],
        "feature edit",
    )
    _git(repo, "checkout", "-q", "main", env=env)
    _setup_commit(
        repo,
        env,
        "a.py",
        [ln if ln != "line5" else "main5" for ln in _BASE_LINES],
        "main edit",
    )


def _resolve_conflict(repo: Path, env: dict[str, str]) -> None:
    _write(repo, "a.py", [ln if ln != "line5" else "resolved5" for ln in _BASE_LINES])
    _git(repo, "add", "a.py", env=env)


# --- the printed body ---------------------------------------------------------------------


def test_print_gate_prints_the_composer_stage_body(capsys):
    assert main(["install-commit-hook", "--print-gate"]) == 0
    out = capsys.readouterr().out
    assert out == install.REVIEW_GATE
    assert out.startswith("#!/bin/sh\n")


def test_both_gate_bodies_share_the_trivial_delta_and_replay_blocks():
    for body in (install.REVIEW_GATE, install._PRECOMMIT):
        assert install._TRIVIAL_DELTA_BLOCK in body
        assert install._REPLAY_SKIP_BLOCK in body
    # composer-stage specifics: no self-service bypass, temp + docs exemptions kept
    assert '[ -n "$REVIEW_SKIP" ] && exit 0' not in install.REVIEW_GATE
    assert '[ -n "$REVIEW_SKIP" ] && exit 0' in install._PRECOMMIT
    assert "is_docs_path" in install.REVIEW_GATE
    assert "/var/folders/*" in install.REVIEW_GATE


@pytest.mark.parametrize("argv", [["--bogus"], ["--print-gate", "extra"], ["extra"]])
def test_install_commit_hook_rejects_unknown_args(capsys, argv):
    with pytest.raises(SystemExit) as exc:
        main(["install-commit-hook", *argv])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "usage: review install-commit-hook" in captured.err
    assert captured.out == ""


def test_review_version_prints_the_pyproject_version(capsys):
    assert (
        _version_mod._DIST_NAME == "hyper-review"
    )  # the published PyPI dist (review-cli#582)
    assert main(["--version"]) == 0
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = parse_project_version(pyproject, require_name=_version_mod._DIST_NAME)
    assert expected
    assert capsys.readouterr().out == f"review {expected}\n"


def test_print_gate_subprocess_contract_matches_rig_consumer(tmp_path):
    """rig-cli riglib/review_gate.py runs `<review> install-commit-hook --print-gate` with no
    stdin, a PATH/HOME-only env and a neutral cwd, and accepts the body only when exit == 0
    and stdout starts with `#!`. Anything else must exit non-zero so rig falls back.
    LD_LIBRARY_PATH is kept only so a shared-libpython interpreter (CI's setup-python) can
    start; it is part of the interpreter's environment, not of the contract."""
    env = {
        k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LD_LIBRARY_PATH")
    }
    review = str(_ROOT / "bin" / "review")
    ok = subprocess.run(
        [review, "install-commit-hook", "--print-gate"],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.decode("utf-8") == install.REVIEW_GATE
    assert ok.stdout.startswith(b"#!")
    bad = subprocess.run(
        [review, "install-commit-hook", "--print-gate", "extra"],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert bad.returncode != 0
    assert bad.stdout == b""


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nname = "review-cli"\nversion = "1.2.3"\n',
        '[build-system]\nrequires = ["x"]\n\n[project]  # metadata\n  name = "review-cli"\n  version = "1.2.3"\n[tool.ruff]\nversion = "9.9.9"\n',
        "[project]\nname = 'review-cli'\ndeps = [\n  \"[bracketed]\",\n]\nversion = '1.2.3'\n",
        '[project]\nname = "review-cli"\nversion = "1.2.3"\n[[tool.x.source]]\nversion = "9.9.9"\n',
        '[project]\r\nname = "review-cli"\r\nversion = "1.2.3"\r\n',
    ],
)
def test_parse_project_version_tolerates_valid_toml_layouts(text):
    assert parse_project_version(text, require_name="review-cli") == "1.2.3"


def test_parse_project_version_rejects_missing_table_or_foreign_name():
    assert parse_project_version('[tool.x]\nversion = "1.0"\n') is None
    assert (
        parse_project_version(
            '[project]\nname = "other"\nversion = "1.0"\n', require_name="review-cli"
        )
        is None
    )
    assert (
        parse_project_version('[project]\nversion = "1.0"\n', require_name="review-cli")
        is None
    )
    assert parse_project_version('[project]\nversion = "1.0"\n') == "1.0"
    # an array-of-tables header ends `[project]`: its `version` key is not ours
    assert (
        parse_project_version(
            '[project]\nname = "review-cli"\n[[tool.x.source]]\nversion = "9.9.9"\n'
        )
        is None
    )


def test_resolve_version_falls_back_to_dist_metadata_then_sentinel(monkeypatch):
    from reviewlib import _version

    monkeypatch.setattr(_version, "_version_from_pyproject", lambda: None)
    monkeypatch.setattr(_version, "_dist_version", lambda name: "7.7.7")
    assert _version.resolve_version() == "7.7.7"

    def _missing(name):
        raise _version.PackageNotFoundError(name)

    monkeypatch.setattr(_version, "_dist_version", _missing)
    assert _version.resolve_version() == "0.0.0+unknown"


# --- live commits through the installed gate ----------------------------------------------


def test_unstamped_code_commit_is_blocked():
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["unreviewed"])
        _git(repo, "add", "a.py", env=env)
        p = _git(repo, "commit", "-q", "-m", "x", env=env, check=False)
        assert p.returncode != 0
        assert "have not been reviewed" in p.stderr


def test_one_line_restage_after_stamped_review_commits_via_trivial_delta():
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["reviewed addition"])
        _git(repo, "add", "a.py", env=env)
        _stamp_staged(repo, env)
        # the review fix: one more line, restaged — a different hash, no fresh review
        _write(repo, "a.py", _BASE_LINES + ["reviewed addition", "one-line follow-up"])
        _git(repo, "add", "a.py", env=env)
        p = _git(repo, "commit", "-q", "-m", "follow-up", env=env, check=False)
        assert p.returncode == 0, p.stderr
        assert "follow-up" in _git(repo, "log", "-1", "--format=%s", env=env).stdout


def test_substantive_restage_after_stamped_review_is_still_blocked():
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["reviewed addition"])
        _git(repo, "add", "a.py", env=env)
        _stamp_staged(repo, env)
        _write(
            repo,
            "a.py",
            _BASE_LINES + ["reviewed addition"] + [f"unreviewed{i}" for i in range(11)],
        )
        _git(repo, "add", "a.py", env=env)
        p = _git(repo, "commit", "-q", "-m", "big", env=env, check=False)
        assert p.returncode != 0
        assert "have not been reviewed" in p.stderr


def test_failed_staged_diff_generation_does_not_accept_a_partial_diff_hash():
    """A failing textconv on a later-sorted file makes `git diff --cached` exit non-zero after
    printing the already-reviewed file's diff; hashing that partial output would match the
    stamp and wave the unreviewed second file through."""
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["reviewed addition"])
        _git(repo, "add", "a.py", env=env)
        _stamp_staged(repo, env)
        _git(repo, "config", "diff.broken.textconv", "false", env=env)
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("z.txt diff=broken\n", encoding="utf-8")
        _write(repo, "z.txt", ["unreviewed"])
        _git(repo, "add", "z.txt", env=env)
        p = _git(repo, "commit", "-q", "-m", "x", env=env, check=False)
        assert p.returncode != 0
        assert (
            "refusing to skip review" in p.stderr
            or "have not been reviewed" in p.stderr
        )


def test_merge_conflict_commit_is_skipped_with_a_logged_reason():
    with _gated_repo() as (repo, env):
        _conflicting_branches(repo, env)
        assert _git(repo, "merge", "feature", env=env, check=False).returncode != 0
        _resolve_conflict(repo, env)
        p = _git(repo, "commit", "-q", "-m", "merge feature", env=env, check=False)
        assert p.returncode == 0, p.stderr
        assert (
            "MERGE_HEAD present" in p.stderr
            and "skipping the staged-review requirement" in p.stderr
        )
        assert len(_git(repo, "log", "-1", "--format=%P", env=env).stdout.split()) == 2
        # the marker is gone with the merge: an ordinary unreviewed change is blocked again
        _write(repo, "a.py", _BASE_LINES + ["after merge"])
        _git(repo, "add", "a.py", env=env)
        blocked = _git(repo, "commit", "-q", "-m", "x", env=env, check=False)
        assert blocked.returncode != 0 and "have not been reviewed" in blocked.stderr


def _assert_unreviewed_commit_is_blocked(repo: Path, env: dict[str, str]) -> None:
    """After a replay finished, the markers must be gone: an ordinary unreviewed change is
    blocked again (a lingering marker would turn the skip into a permanent bypass)."""
    _write(repo, "b.py", ["unreviewed after replay"])
    _git(repo, "add", "b.py", env=env)
    blocked = _git(repo, "commit", "-q", "-m", "x", env=env, check=False)
    assert blocked.returncode != 0 and "have not been reviewed" in blocked.stderr


def test_commit_inside_a_stopped_rebase_is_skipped_with_a_logged_reason():
    """`git rebase --continue` itself commits with hooks OFF (git's sequencer passes `-n`), so
    the gate never sees it; what it DOES see is the manual `git commit` an agent runs inside
    a stopped rebase after resolving the conflict — that is the rebase-merge/ case."""
    with _gated_repo() as (repo, env):
        _conflicting_branches(repo, env)
        _git(repo, "checkout", "-q", "feature", env=env)
        assert _git(repo, "rebase", "main", env=env, check=False).returncode != 0
        _resolve_conflict(repo, env)
        p = _git(
            repo, "commit", "-q", "-m", "feature edit (rebased)", env=env, check=False
        )
        assert p.returncode == 0, p.stderr
        assert "rebase-merge/ present" in p.stderr
        assert _git(repo, "rebase", "--continue", env=env, check=False).returncode == 0
        assert (
            _git(repo, "log", "-1", "--format=%s", env=env).stdout.strip()
            == "feature edit (rebased)"
        )
        _assert_unreviewed_commit_is_blocked(repo, env)


def test_lingering_rebase_head_after_a_finished_rebase_does_not_skip_the_gate():
    """git (2.50 verified) leaves `REBASE_HEAD` behind after a conflict-resolved rebase
    completes. The skip keys on the rebase STATE DIRECTORY, so that leftover ref must not
    exempt the next ordinary commit."""
    with _gated_repo() as (repo, env):
        _conflicting_branches(repo, env)
        _git(repo, "checkout", "-q", "feature", env=env)
        assert _git(repo, "rebase", "main", env=env, check=False).returncode != 0
        _resolve_conflict(repo, env)
        assert _git(repo, "rebase", "--continue", env=env, check=False).returncode == 0
        rebase_head = Path(
            _git(repo, "rev-parse", "--git-path", "REBASE_HEAD", env=env).stdout.strip()
        )
        if not rebase_head.is_absolute():
            rebase_head = repo / rebase_head
        if not rebase_head.exists():
            rebase_head.write_text(
                "0" * 40 + "\n", encoding="utf-8"
            )  # emulate the leftover
        _assert_unreviewed_commit_is_blocked(repo, env)


def test_cherry_pick_continue_is_skipped_with_a_logged_reason():
    with _gated_repo() as (repo, env):
        _conflicting_branches(repo, env)
        feature_sha = _git(repo, "rev-parse", "feature", env=env).stdout.strip()
        assert (
            _git(repo, "cherry-pick", feature_sha, env=env, check=False).returncode != 0
        )
        _resolve_conflict(repo, env)
        p = _git(repo, "cherry-pick", "--continue", env=env, check=False)
        assert p.returncode == 0, p.stderr
        assert "CHERRY_PICK_HEAD present" in p.stderr
        assert (
            _git(repo, "log", "-1", "--format=%s", env=env).stdout.strip()
            == "feature edit"
        )
        _assert_unreviewed_commit_is_blocked(repo, env)


def test_tmpdir_root_does_not_exempt_every_repo():
    """`TMPDIR=/` strips to an empty temp root; the exemption arm must not become `/*`."""
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["unreviewed"])
        _git(repo, "add", "a.py", env=env)
        p = _git(
            repo, "commit", "-q", "-m", "x", env={**env, "TMPDIR": "/"}, check=False
        )
        assert p.returncode != 0
        assert "have not been reviewed" in p.stderr


def test_tmpdir_ancestor_of_home_does_not_exempt_the_repo():
    """`TMPDIR` pointing at an ancestor of $HOME must not exempt every repo beneath it."""
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        _write(repo, "a.py", _BASE_LINES + ["unreviewed"])
        _git(repo, "add", "a.py", env=env)
        broad = str(repo.parent)
        p = _git(
            repo,
            "commit",
            "-q",
            "-m",
            "x",
            env={**env, "TMPDIR": broad, "HOME": str(repo)},
            check=False,
        )
        assert p.returncode != 0
        assert "have not been reviewed" in p.stderr


def test_merge_skip_resolves_the_marker_inside_a_linked_worktree():
    """`MERGE_HEAD` of a linked worktree lives under `.git/worktrees/<name>/`; a literal
    `.git/MERGE_HEAD` check would miss it and keep blocking the worktree-based flow."""
    with _gated_repo() as (repo, env):
        _conflicting_branches(repo, env)
        wt = repo.parent / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt), "main", env=env)
        assert _git(wt, "merge", "feature", env=env, check=False).returncode != 0
        assert not (
            repo / ".git" / "MERGE_HEAD"
        ).exists()  # it is the worktree's, not main's
        _resolve_conflict(wt, env)
        p = _git(wt, "commit", "-q", "-m", "merge in worktree", env=env, check=False)
        assert p.returncode == 0, p.stderr
        assert "MERGE_HEAD present" in p.stderr


def test_direct_precommit_template_also_skips_a_merge_commit():
    """The self-contained `_PRECOMMIT` (no-rig machines) shares the replay block."""
    with _gated_repo(install._PRECOMMIT) as (repo, env):
        _conflicting_branches(repo, env)
        assert _git(repo, "merge", "feature", env=env, check=False).returncode != 0
        _resolve_conflict(repo, env)
        p = _git(repo, "commit", "-q", "-m", "merge feature", env=env, check=False)
        assert p.returncode == 0, p.stderr
        assert "MERGE_HEAD present" in p.stderr


def test_abandoned_git_am_state_directory_does_not_skip_the_gate():
    """`rebase-apply/` is `git am`'s state dir (third-party patches) and lingers when an
    `am` is abandoned: it must not switch the gate off for later commits."""
    with _gated_repo() as (repo, env):
        _setup_commit(repo, env, "a.py", _BASE_LINES, "base")
        git_dir = _git(repo, "rev-parse", "--absolute-git-dir", env=env).stdout.strip()
        (Path(git_dir) / "rebase-apply").mkdir()
        _write(repo, "a.py", _BASE_LINES + ["unreviewed"] * 11)
        _git(repo, "add", "a.py", env=env)
        p = _git(repo, "commit", "-q", "-m", "x", env=env, check=False)
        assert p.returncode != 0 and "have not been reviewed" in p.stderr


@pytest.fixture(autouse=True, scope="module")
def _scratch_root_is_not_temp_exempt():
    """Guard the guard: if `.test-repos` ever lands under a temp-exempt path the live tests
    above pass vacuously. Fail loudly instead."""
    root = str(_SCRATCH_ROOT)
    # mirror the gate: an empty/`/` TMPDIR falls back to /tmp semantics, never to a `/` prefix
    tmp = (os.environ.get("TMPDIR") or "/tmp").rstrip("/")
    prefixes = [
        "/tmp/",
        "/var/tmp/",
        "/var/folders/",
        "/private/tmp/",
        "/private/var/folders/",
    ]
    if tmp:
        prefixes.insert(0, tmp + "/")
    for prefix in prefixes:
        assert not root.startswith(prefix), (
            f"{root} is inside the gate's temp exemption ({prefix})"
        )
    yield


if __name__ == "__main__":
    # Plain-script harness (tests/smoke.py's run_unit runs every _UNIT_FILES entry as
    # `python3 <file>`, not through pytest). Unlike the simpler "call every test_* function
    # directly" convention used elsewhere in this suite (see test_pool_guard.py /
    # test_review_marker.py), this file leans on real pytest machinery — capsys, monkeypatch,
    # and @pytest.mark.parametrize — that the manual loop cannot replicate, so hand off to
    # pytest itself instead of reimplementing fixture/parametrize support by hand.
    raise SystemExit(pytest.main([__file__, "-q"]))
