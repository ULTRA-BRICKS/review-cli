#!/usr/bin/env python3
"""Record-time task-code/branch-binding guard (reviewlib.cli._task_code_branch_mismatch_warning).

Postmortem (conloca/landing, 2026-09-07): before agent-tools#566/#567 landed, `gh ship`
couldn't derive a review-quorum task code from a bare GitHub issue reference in a repo
tracked as plain GitHub issues (not HYP-<n> Linear tickets). The documented workaround was
to mint a throwaway ticket and run `review diff -C <shared primary checkout> --task <code>`
— reviewing whatever happened to be uncommitted in a SHARED, actively-mutating checkout, not
the PR's own worktree/branch. One resulting ticket (conloca/landing#135) reviewed a
package.json claiming version "0.4.3", a version that never existed on any ref in that
repo's history — direct evidence the diff being recorded had nothing to do with the task
code it was credited to.

This is the SAME diff-identity-pollution class `--check`-time repo/diff mismatch detection
already guards against (reviewlib.stats "Diff-identity binding", see test_diff_identity.py —
PR #151/#131/HYP-858), just at RECORD time instead of check time: nothing previously stopped
`review diff --task <code>` from recording an iteration credited to a task code whose own PR
lives on a completely different branch than the one being reviewed.

These tests exercise `_task_code_branch_mismatch_warning` directly (a fake `gh` prepended to
PATH stands in for the real GitHub CLI — offline, no network/auth needed) plus one end-to-end
pass through the real `bin/review diff` subprocess.

Matching is against GitHub's own `closingIssuesReferences` (the "Closes/Fixes/Resolves #<n>"
keyword relationship), NOT a text search over the PR body — an earlier cut of this guard
matched any standalone "#<n>" mention anywhere in the body, which a codex review on this
guard's own PR caught as a false-refusal hazard: a PR that merely DISCUSSES an issue in
passing (never closes it) would be wrongly treated as that issue's "own PR".
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from reviewlib import cli as _cli  # noqa: E402

REVIEW = str(REPO_ROOT / "bin" / "review")


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "REVIEW_SKIP": "1"}
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)


_LOCAL_REPO_OWNER = "test-owner"
_LOCAL_REPO_NAME = "test-repo"


def _init_repo() -> Path:
    d = Path(tempfile.mkdtemp())
    _git(d, "init", "-q")
    _git(d, "config", "core.hooksPath", "/dev/null")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "commit.gpgsign", "false")
    # An `origin` remote is required for `_local_repo_identity` (the cross-repo closing-
    # reference check, `_closing_ref_is_this_repos_issue`) to resolve THIS repo's own
    # identity — every `_pr()`-built fixture below embeds a matching `repository` in its
    # closing references. Without this, `_local_repo_identity` returns None and every
    # closing reference is untrusted (fail-open), which would make the branch-mismatch
    # guard a silent no-op for this entire suite (caught by this test file's own dogfood
    # run: 3 tests failed with a false "None" verdict, one fell through to a REAL model
    # dispatch since nothing refused).
    _git(d, "remote", "add", "origin", f"git@github.com:{_LOCAL_REPO_OWNER}/{_LOCAL_REPO_NAME}.git")
    (d / "f.txt").write_text("base\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "init", "--no-verify")
    return d


def _closing_ref(issue_number: int, *, owner: str, name: str) -> str:
    return (
        f'{{"number": {issue_number}, '
        f'"repository": {{"owner": {{"login": "{owner}"}}, "name": "{name}"}}}}'
    )


def _pr(
    number: int,
    head: str,
    *,
    closes: list[int] = (),
    body: str = "",
    cross_repo: bool = False,
    closes_owner: str = _LOCAL_REPO_OWNER,
    closes_name: str = _LOCAL_REPO_NAME,
) -> str:
    refs = ", ".join(
        _closing_ref(n, owner=closes_owner, name=closes_name) for n in closes
    )
    cross = "true" if cross_repo else "false"
    return (
        f'{{"number": {number}, "headRefName": "{head}", '
        f'"isCrossRepository": {cross}, '
        f'"closingIssuesReferences": [{refs}], "body": "{body}"}}'
    )


def _fake_gh(bin_dir: Path, prs_json: str, *, exit_code: int = 0) -> None:
    """Write a fake `gh` on `bin_dir` that answers `gh pr list ...` with `prs_json`
    regardless of arguments — enough for this module's single call shape."""
    script = bin_dir / "gh"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{prs_json}\nEOF\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _NoReentrancyGuard:
    """Context manager: strip `REVIEW_CLI_ACTIVE` (reviewlib.cli.REVIEW_CLI_ACTIVE_ENV)
    for an IN-PROCESS `_cli.main()` call. `_run_review_diff`'s subprocess helper already
    does this for its own child's env, but an in-process call runs in THIS interpreter,
    inheriting `os.environ` directly — if the process invoking pytest itself happens to
    be nested inside another `review` run (e.g. this very suite driven by a review-
    backed agent session), the reentrancy guard fires before the branch guard is ever
    reached (codex review finding on this guard's own PR: reproduced 2 failures in a
    review-backed environment, both fixed by clearing this one variable)."""

    def __enter__(self):
        self._saved = os.environ.pop("REVIEW_CLI_ACTIVE", None)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            os.environ["REVIEW_CLI_ACTIVE"] = self._saved
        return False


class _PathPrepend:
    """Context manager: temporarily prepend a directory to $PATH for THIS process (the
    guard's `gh`/`git` calls inherit os.environ via `_run(..., env=None)`)."""

    def __init__(self, bin_dir: Path) -> None:
        self._bin_dir = bin_dir
        self._saved: str | None = None

    def __enter__(self):
        self._saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self._bin_dir}{os.pathsep}{self._saved}"
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            os.environ["PATH"] = self._saved
        return False


def test_non_issue_shaped_code_skips_check_without_needing_gh():
    """A HYP-931-shaped (or any non-`#<n>`) task code is out of scope for this guard —
    it must return None immediately, with no `gh` on PATH at all (would crash if it tried)."""
    d = _init_repo()
    assert _cli._task_code_branch_mismatch_warning(d, "HYP-931") is None
    assert _cli._task_code_branch_mismatch_warning(d, "SME-ROADMAP-NOTE") is None


def test_non_ascii_digit_codes_are_out_of_scope():
    """`str.isdigit()` accepts non-ASCII numerals (fullwidth, Arabic-Indic, superscript)
    that `gh ship`'s own bash-regex derivation (`[0-9]+`) never would — a codex review
    finding on this guard's own PR. `_is_bare_issue_task_code` must reject all of these,
    not just the ASCII shape it's meant for."""
    d = _init_repo()
    assert _cli._task_code_branch_mismatch_warning(d, "#１２３") is None  # fullwidth "123"
    assert _cli._task_code_branch_mismatch_warning(d, "#١٢٣") is None  # Arabic-Indic "123"
    assert _cli._task_code_branch_mismatch_warning(d, "#²") is None  # superscript "2", not int()-able
    assert not _cli._is_bare_issue_task_code("#１２３")
    assert _cli._is_bare_issue_task_code("#141")


def test_zero_matching_prs_skips_check():
    d = _init_repo()
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[]")
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_multiple_matching_prs_is_ambiguous_and_skips_check():
    d = _init_repo()
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(
        bin_dir,
        "[" + _pr(1, "a", closes=[141]) + ", " + _pr(2, "b", closes=[141]) + "]",
    )
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_matching_pr_on_current_branch_is_clean():
    d = _init_repo()
    _git(d, "checkout", "-qb", "fix/slide-reveal")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_detached_head_does_not_false_refuse():
    """A detached-HEAD checkout (common for a CI/review worktree pinned at the PR's own
    commit SHA rather than its branch) must be treated as 'branch unknown', not as a
    branch literally named 'HEAD' — `git rev-parse --abbrev-ref HEAD` prints the literal
    string 'HEAD' when detached. Before this fix, that string was compared against the
    matching PR's real head-ref name ('fix/slide-reveal') and NEVER matched, so a
    detached checkout sitting at the exact right commit was FALSELY refused (codex
    review finding on this guard's own PR)."""
    d = _init_repo()
    _git(d, "checkout", "-q", "--detach")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_gh_repo_env_var_does_not_redirect_the_lookup():
    """`GH_REPO` (and its GitHub-Actions alias `GITHUB_REPOSITORY`) override `gh`'s own
    repository auto-detection regardless of `-C`/cwd — `git_repo_env` only strips GIT_*
    vars, so an ambient `GH_REPO` would otherwise make the PR lookup silently answer for
    a totally different repository than the one being reviewed (codex review finding on
    this guard's own PR). The fake `gh` script here ignores its args entirely and would
    return a match either way, so this test's only job is confirming the guard still
    calls it at all with GH_REPO set — a regression that swallowed the env var (or a
    crash) would be the failure mode this guards against; the meaningful proof lives in
    the code's explicit `gh_env.pop(\"GH_REPO\"...)` scrub, verified by inspecting the
    env passed to `gh` here."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    script = bin_dir / "gh"
    captured_env_file = Path(tempfile.mkdtemp()) / "gh_repo_seen.txt"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "${{GH_REPO:-UNSET}}" > "{captured_env_file}"\n'
        "cat <<'EOF'\n"
        f'[{_pr(144, "fix/slide-reveal", closes=[141])}]\n'
        "EOF\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    old_gh_repo = os.environ.get("GH_REPO")
    os.environ["GH_REPO"] = "some-other-org/some-other-repo"
    try:
        with _PathPrepend(bin_dir):
            warning = _cli._task_code_branch_mismatch_warning(d, "#141")
    finally:
        if old_gh_repo is None:
            os.environ.pop("GH_REPO", None)
        else:
            os.environ["GH_REPO"] = old_gh_repo
    assert warning is not None  # unrelated-work != fix/slide-reveal, refused as expected
    assert captured_env_file.read_text().strip() == "UNSET", (
        "GH_REPO leaked into the gh pr list call — it should have been scrubbed"
    )


def test_matching_pr_on_different_branch_warns():
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        warning = _cli._task_code_branch_mismatch_warning(d, "#141")
    assert warning is not None
    assert "#141" in warning
    assert "fix/slide-reveal" in warning
    assert "unrelated-work" in warning
    assert "check out 'fix/slide-reveal'" in warning


def test_matching_pr_on_different_branch_with_existing_worktree_names_it():
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    wt = Path(tempfile.mkdtemp()) / "wt"
    _git(d, "worktree", "add", "-q", "-b", "fix/slide-reveal", str(wt))
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        warning = _cli._task_code_branch_mismatch_warning(d, "#141")
    assert warning is not None
    # Compare resolved paths: macOS's /tmp -> /private/tmp symlink means `git worktree
    # list` (which reports the canonical path) and Python's raw tempfile path can differ
    # in string form while naming the same directory.
    resolved_wt = str(wt.resolve())
    assert resolved_wt in warning, warning
    assert f"-C {resolved_wt}" in warning, warning


def test_incidental_body_mention_does_not_count_as_the_issues_own_pr():
    """A PR whose BODY happens to mention '#141' in passing (e.g. "see the discussion in
    #141") but does NOT close it (empty closingIssuesReferences) must not be treated as
    #141's own PR — the exact false-refusal shape a codex review caught on this guard's
    first cut, which matched any standalone body mention."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(
        bin_dir,
        "[" + _pr(200, "some-other-branch", closes=[], body="see the discussion in #141") + "]",
    )
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_fork_pr_with_same_named_local_branch_does_not_false_confirm():
    """A fork PR's headRefName ('fix/login') can collide with an UNRELATED local branch
    of the same name in this repo. Before excluding cross-repository matches, that
    equality would have falsely CONFIRMED the local branch as 'the issue's own PR
    branch' even though the real PR content lives in someone else's fork — a codex
    review finding on this guard's own PR. With the fork PR excluded from the match
    set, the lookup drops to zero matches and the guard fails open (skips), rather than
    either falsely confirming OR falsely refusing an unrelated local branch."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "fix/login")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(
        bin_dir,
        "[" + _pr(144, "fix/login", closes=[141], cross_repo=True) + "]",
    )
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_cross_repo_closing_reference_with_same_number_does_not_false_confirm():
    """GitHub explicitly supports CROSS-repository closing keywords ('Fixes other-org/
    other-repo#141'). A local PR that closes issue #141 in a DIFFERENT repository must
    NOT be mistaken for THIS repo's own #141 just because the number matches — a codex
    review finding on this guard's own PR. With the foreign reference excluded, the
    lookup drops to zero matches and fails open, rather than falsely confirming (or
    falsely refusing) the local branch based on a same-numbered but unrelated issue."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(
        bin_dir,
        "["
        + _pr(
            144,
            "fix/something-else",
            closes=[141],
            closes_owner="some-other-org",
            closes_name="some-other-repo",
        )
        + "]",
    )
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_truncated_pr_page_is_untrustworthy_and_skips_check():
    """A full page of `_PR_LOOKUP_LIMIT` results might be hiding the real match past the
    page boundary — treating that as a confident "zero matches" would let a wrong-branch
    review through silently (a codex review finding on this guard's own PR)."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    prs = ", ".join(_pr(n, f"branch-{n}", closes=[]) for n in range(_cli._PR_LOOKUP_LIMIT))
    _fake_gh(bin_dir, f"[{prs}]")
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


def test_gh_failure_fails_open():
    """A `gh` that errors (missing auth, no network) must never turn into a false refusal —
    the guard is advisory infrastructure, not a hard dependency."""
    d = _init_repo()
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "not json", exit_code=1)
    with _PathPrepend(bin_dir):
        assert _cli._task_code_branch_mismatch_warning(d, "#141") is None


# Every provider-credential env var this codebase reads (reviewlib/backends.py). Stripped
# from every subprocess `review` invocation these tests spawn: none of these tests should
# EVER reach a real model dispatch (the refusal tests exit before dispatch; the
# --no-verify-task-branch bypass test intentionally reaches PAST the branch guard, and
# without this would fall through to real, credit-spending API calls using whatever
# provider key happens to be set in the developer/CI environment — the isolated `HOME`
# below only isolates CLI-session-based auth (codex/claude/opencode OAuth state), not a
# literal API key inherited via `dict(os.environ)`).
_PROVIDER_API_KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "COMMANDCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_AI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
)


def _run_review_diff(
    cwd: Path, task: str, *, extra: list[str] | None = None, stdin: str = ""
):
    with tempfile.TemporaryDirectory() as fake_home:
        env = dict(os.environ)
        env["HOME"] = fake_home
        env["XDG_CONFIG_HOME"] = str(Path(fake_home) / ".config")
        # Strip REVIEW_CLI_ACTIVE (reviewlib.cli.REVIEW_CLI_ACTIVE_ENV): a bare `dict(
        # os.environ)` copy leaks it in from a parent `review` process this test itself
        # might be running under (e.g. this very test suite invoked via a review-backed
        # agent session), which trips the reentrancy refusal BEFORE the branch guard is
        # ever reached — a codex review finding on this guard's own PR ("the bypass test
        # masks that failure": both the required-refusal and the --no-verify-task-branch
        # tests need to reach the actual branch-guard code path, not exit early on an
        # unrelated reentrancy guard).
        env.pop("REVIEW_CLI_ACTIVE", None)
        for var in _PROVIDER_API_KEY_ENV_VARS:
            env.pop(var, None)
        return subprocess.run(
            [sys.executable, REVIEW, "diff", "--task", task, *(extra or [])],
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


def test_branch_switch_between_check_and_diff_capture_is_caught():
    """The TOCTOU window a codex review flagged on this guard's own PR: the initial
    check can pass (current branch matches the issue's PR), but if something switches
    the checkout to a DIFFERENT branch before the diff is actually read, the diff that
    gets recorded is no longer the one the check verified. A fake `gh` that itself
    performs the branch switch (as a stand-in for a concurrent process doing so) proves
    the post-diff-capture re-check catches it."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "fix/slide-reveal")
    (d / "f.txt").write_text("base\nchanged\n")
    bin_dir = Path(tempfile.mkdtemp())
    script = bin_dir / "gh"
    # First check (pre-diff-capture): still on fix/slide-reveal, matches -> passes.
    # THEN, as a side effect of the `gh` call itself, switch the checkout to a
    # different branch -- simulating a concurrent process racing this review.
    script.write_text(
        "#!/bin/sh\n"
        f"git -C '{d}' checkout -q -b raced-away-branch\n"
        "cat <<'EOF'\n"
        f'[{_pr(144, "fix/slide-reveal", closes=[141])}]\n'
        "EOF\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(d, "#141")
    assert proc.returncode == _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "branch changed" in proc.stderr, proc.stderr
    assert "raced-away-branch" in proc.stderr, proc.stderr


def test_branch_detaching_mid_run_is_caught_not_silently_skipped():
    """Losing a previously-known branch entirely (going detached, or a git failure at
    exactly the wrong moment) between the two checks must ALSO refuse — a codex review
    finding on this guard's own PR: an earlier cut only compared two non-None samples,
    so known-branch -> detached mid-run silently passed even though the diff could
    easily have come from an unrelated commit during that window."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "fix/slide-reveal")
    (d / "f.txt").write_text("base\nchanged\n")
    bin_dir = Path(tempfile.mkdtemp())
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f"git -C '{d}' checkout -q --detach\n"
        "cat <<'EOF'\n"
        f'[{_pr(144, "fix/slide-reveal", closes=[141])}]\n'
        "EOF\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(d, "#141")
    assert proc.returncode == _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "branch changed" in proc.stderr, proc.stderr
    assert "undeterminable" in proc.stderr, proc.stderr


def test_visual_companion_on_clean_tree_is_not_blocked_by_the_branch_guard():
    """`review diff --visual shot.png` on a CLEAN tree intentionally falls back to a
    STANDALONE visual review with no diff at all (Sec.3 routing) — it will never record
    anything under the task code. A codex review finding on this guard's own PR: an
    earlier cut ran the branch guard before diff acquisition, so a mismatched branch
    with a valid image and an empty diff refused (exit 14) a review that was never
    going to touch the task code's history in the first place."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    img = Path(tempfile.mkdtemp()) / "shot.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(
            d, "#141", extra=["--visual", str(img), "--no-ai", "--strict"]
        )
    assert proc.returncode != _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "branch changed" not in proc.stdout + proc.stderr
    assert "fix/slide-reveal" not in proc.stdout + proc.stderr


def test_visual_companion_with_a_real_diff_is_still_guarded():
    """The sibling of the clean-tree case above: `--visual shot.png` with a REAL
    unstaged diff present is a COMPANION review that DOES record under the task code
    (Sec.3 routing) — round 2 of a codex review on this guard's own PR caught an
    earlier fix that exempted `--visual` unconditionally, reopening the exact
    vulnerability being closed for this combination specifically. Must still refuse on
    a branch mismatch."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    (d / "f.txt").write_text("base\nchanged\n")  # a real diff -> companion mode
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    img = Path(tempfile.mkdtemp()) / "shot.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(
            d, "#141", extra=["--visual", str(img), "--no-ai", "--strict"]
        )
    assert proc.returncode == _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "fix/slide-reveal" in proc.stderr, proc.stderr


_FAKE_DIFF = (
    "diff --git a/f.txt b/f.txt\n"
    "index 111..222 100644\n"
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -1 +1 @@\n"
    "-base\n"
    "+piped content unrelated to this branch\n"
)


def test_piped_diff_on_correct_branch_still_refuses():
    """The branch check alone proves CWD is on the right branch, but says NOTHING about
    what a CALLER piped in on stdin — `git diff <anything> | review diff --task '#141'`
    bypasses `_git_diff` entirely, so an unrelated diff could still get recorded under a
    real task code even while sitting on the exact right branch (codex review finding on
    this guard's own PR). Verified here on the CORRECT branch specifically — the
    scenario the plain branch check would have called 'clean'."""
    d = _init_repo()
    _git(d, "checkout", "-qb", "fix/slide-reveal")  # the CORRECT branch for #141
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(d, "#141", stdin=_FAKE_DIFF)
    assert proc.returncode == _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "piped" in proc.stderr.lower(), proc.stderr


class _FakeStdin(io.StringIO):
    """A non-TTY stdin stand-in: `_read_stdin_if_piped` checks `isatty()` (False here,
    same as a real pipe) then `.read()`s the content."""

    def isatty(self) -> bool:
        return False


def test_piped_diff_non_issue_task_code_is_unaffected():
    """The piped-diff refusal is scoped to bare `#<n>` issue codes only
    (`_is_bare_issue_task_code`) — a HYP-931-shaped or descriptive code piping a diff is
    normal, long-supported usage (`git diff | review diff --task CODE`) and must not be
    newly refused by this guard. In-process + stubbed backend (same rationale as
    `test_no_verify_task_branch_flag_bypasses_the_refusal`): a real subprocess dispatch
    here would hit the real model backends since nothing refuses this run early."""
    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    log = tempfile.mkdtemp()
    stats_file = Path(tempfile.mkdtemp()) / "run-stats.jsonl"
    restore = _with_backend_stub(_stub_resolve_backend(0))
    old_stdin = sys.stdin
    sys.stdin = _FakeStdin(_FAKE_DIFF)
    os.environ["REVIEW_LOG_DIR"] = log
    os.environ["REVIEW_STATS_FILE"] = str(stats_file)
    try:
        err = io.StringIO()
        with redirect_stderr(err), _NoReentrancyGuard():
            rc = _cli.main(["diff", "--task", "HYP-931", "-C", str(d), "-m", "codex"])
    finally:
        sys.stdin = old_stdin
        restore()
        os.environ.pop("REVIEW_LOG_DIR", None)
        os.environ.pop("REVIEW_STATS_FILE", None)

    assert rc == 0, (rc, err.getvalue())
    assert "refusing to record a PIPED diff" not in err.getvalue(), err.getvalue()


def test_diff_subcommand_refuses_on_branch_mismatch_end_to_end():
    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    (d / "f.txt").write_text("base\nchanged\n")  # give it a real diff to reach the guard
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")
    with _PathPrepend(bin_dir):
        proc = _run_review_diff(d, "#141")
    assert proc.returncode == _cli.EXIT_TASK_BRANCH_MISMATCH, (
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    assert "fix/slide-reveal" in proc.stderr, proc.stderr
    assert "Traceback" not in (proc.stdout + proc.stderr)


def test_no_verify_task_branch_flag_bypasses_the_refusal():
    """Deliberately IN-PROCESS with STUBBED backends (`test_run_stats`'s own
    `_with_backend_stub`/`_stub_resolve_backend` convention — see
    test_diff_identity.py::test_cli_review_run_records_repo_id_and_diff_files for the
    identical pattern), NOT a real subprocess dispatch: an earlier cut of this test ran
    the real `bin/review diff` subprocess and, even with common provider API keys
    stripped, one seat still resolved a real, credit-spending backend (a CLI-session-
    based provider unaffected by the stripped env vars) — a codex review finding on this
    guard's own PR. Stubbing the backend resolver guarantees zero real model calls
    regardless of the host's ambient credentials, while still proving the CLI reaches
    PAST the branch guard and into real dispatch: both stub seats' canned output shows
    up (proof the mode's own dispatch loop ran each one), not just a bare exit code
    (a weak `!= EXIT_TASK_BRANCH_MISMATCH`-only check can't tell "bypass worked" from "a
    different guard/refusal fired first and happened to share a different exit code").
    Deliberately NOT asserting on `run-stats.jsonl` here: that store's write path has
    its own environment-dependent quirks unrelated to this guard (reproduced hanging /
    silently-skipped writes outside this suite's own pytest fixtures) — the stub
    seats' actual output landing on stdout is the direct, reliable signal that dispatch
    happened."""
    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    d = _init_repo()
    _git(d, "checkout", "-qb", "unrelated-work")
    (d / "f.txt").write_text("base\nchanged\n")
    bin_dir = Path(tempfile.mkdtemp())
    _fake_gh(bin_dir, "[" + _pr(144, "fix/slide-reveal", closes=[141]) + "]")

    with _PathPrepend(bin_dir):
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        stats_file = Path(tempfile.mkdtemp()) / "run-stats.jsonl"
        os.environ["REVIEW_LOG_DIR"] = log
        os.environ["REVIEW_STATS_FILE"] = str(stats_file)
        try:
            err, out = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out), _NoReentrancyGuard():
                rc = _cli.main(
                    [
                        "diff",
                        "--task",
                        "#141",
                        "-C",
                        str(d),
                        "-m",
                        "codex,gemini",
                        "--no-verify-task-branch",
                    ]
                )
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_STATS_FILE", None)

    assert rc == 0, (rc, err.getvalue())
    assert "fix/slide-reveal" not in err.getvalue(), err.getvalue()
    assert "output from codex" in out.getvalue(), out.getvalue()
    assert "output from gemini" in out.getvalue(), out.getvalue()
    assert "review-cli#180" not in err.getvalue(), err.getvalue()


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
