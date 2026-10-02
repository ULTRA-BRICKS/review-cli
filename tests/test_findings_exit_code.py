#!/usr/bin/env python3
"""RC-6 residuals: `review diff` must exit nonzero when a HEALTHY seat reports
findings (review-cli#517), without conflating seat transport errors.

Observed live in the hatch-flood session: a review printed real
correctness/security findings yet exited 0, because the mode's exit code maps
only SEAT HEALTH (`ok = all(result_is_usable(...))`): a body full of findings is
a "usable verdict". Automation keying on the exit code sailed straight past the
findings.

New contract pinned here (flat `-m` path and the board path alike):
  * every seat healthy + every body reads CLEAN             -> exit 0 (unchanged)
  * every seat healthy + >=1 usable body reports FINDINGS   -> exit
    EXIT_FINDINGS_REPORTED (14, stable/documented), plus a one-line stderr notice
  * any seat unhealthy (transport/degrade)                  -> exit 1 (unchanged;
    an errored seat is NOT a finding; existing partial-failure semantics intact)
  * the stamp/checkpoint writes are keyed on seat health exactly as before —
    findings remain informational for the gate; only the exit class changes.

"Reports findings" follows DEFAULT_PROMPT's own verdict contract: a FINDING
cites file:line before any `checked:` marker in its clause; a citation following
`checked:` is ruled out. A CLEAN verdict carries a `checked:` statement or a clean
phrase ("no issues", "LGTM", ...). A body with
neither signal (a contract-violating skim) fails CLOSED into the findings class.

Same harness style as tests/test_run_stats.py: backends stubbed by module-global
reassignment, stats store redirected to a temp file, no live model call.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

os.environ.setdefault(
    "REVIEW_PROVIDER_CACHE", str(Path(tempfile.mkdtemp()) / "last-provider.json")
)
from _failover_neutralise import identity_provider_chain  # noqa: E402

from reviewlib import backends as _backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib import cli as _cli  # noqa: E402
from reviewlib import panel as _panel  # noqa: E402
from reviewlib.config import DEFAULT_PROMPT, BoardReviewer  # noqa: E402
from reviewlib.modes.contract import ModeContext  # noqa: E402
from reviewlib.modes import review as _review_mode  # noqa: E402

TASK = "RC6-FINDINGS"

FINDING_BODY = (
    "P1 correctness: reviewlib/foo.py:42 — the retry loop re-reads the index "
    "after a hook mutated it, so the reviewed diff is not the committed one. "
    "Trigger: stage, run review, let a formatting hook restage mid-run."
)
CLEAN_BODY = (
    "checked: race on concurrent writes to the cache — none found, guarded by "
    "the cache flock at reviewlib/bar.py:10 — ruled out; checked: injection via user input — "
    "sanitized before use. No issues."
)
BARE_CLEAN_BODY = "No issues found."
# Neither a finding citation nor a clean signal: DEFAULT_PROMPT's contract says
# this shape is not an acceptable answer — it must fail CLOSED (findings class).
SKIM_BODY = "I read the diff and it seems reasonable overall."


def _stub(bodies_by_model: dict[str, str], fail: set[str] | None = None) -> None:
    """Swap resolve_backend in every dispatching namespace; per-model bodies."""
    fail = fail or set()

    def backend(m, prompt, diff, cwd, timeout, round_no=0, effort=None):
        if m in fail:
            return ReviewResult(
                model=m,
                command=f"stub {m}",
                returncode=1,
                stdout="",
                stderr="Usage limit: quota exhausted",
                prompt_tokens=0,
                output_tokens=0,
            )
        return ReviewResult(
            model=m,
            command=f"stub {m}",
            returncode=0,
            stdout=bodies_by_model.get(m, CLEAN_BODY),
            stderr="",
            prompt_tokens=1,
            output_tokens=1,
        )

    def resolver(model: str):
        return backend

    global_saved = (
        _panel.resolve_backend,
        _review_mode.resolve_backend,
        _backends.backend_available,
        _panel.backend_available,
        _review_mode.backend_available,
    )
    _panel.resolve_backend = resolver
    _review_mode.resolve_backend = resolver
    _backends.backend_available = lambda _m: True
    _panel.backend_available = lambda _m: True
    _review_mode.backend_available = lambda _m: True
    return global_saved


def _restore(saved) -> None:
    (
        _panel.resolve_backend,
        _review_mode.resolve_backend,
        _backends.backend_available,
        _panel.backend_available,
        _review_mode.backend_available,
    ) = saved


class _Env:
    """Temp stats/marker/log redirection for one test."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self._old = {
            k: os.environ.get(k)
            for k in ("REVIEW_STATS_FILE", "REVIEW_MARKER", "REVIEW_LOG_DIR")
        }
        os.environ["REVIEW_STATS_FILE"] = str(base / "stats.jsonl")
        os.environ["REVIEW_MARKER"] = str(base / "marker")
        os.environ["REVIEW_LOG_DIR"] = str(base / "logs")
        # A fast bounded stdin wait so a stray non-tty stdin can never stall a test.
        self._old_wait = os.environ.get("REVIEW_STDIN_WAIT_SECONDS")
        os.environ["REVIEW_STDIN_WAIT_SECONDS"] = "1"
        return self

    def __exit__(self, *exc):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._old_wait is None:
            os.environ.pop("REVIEW_STDIN_WAIT_SECONDS", None)
        else:
            os.environ["REVIEW_STDIN_WAIT_SECONDS"] = self._old_wait
        self._dir.cleanup()
        return False


def _staged_repo() -> tempfile.TemporaryDirectory:
    d = tempfile.TemporaryDirectory()
    repo = Path(d.name)
    env = {**os.environ, "REVIEW_SKIP": "1"}

    def run(*argv: str) -> None:
        subprocess.run(argv, cwd=str(repo), check=True, env=env, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "core.hooksPath", "/dev/null")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("base\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "chore: init findings-exit scratch repo")
    (repo / "f.txt").write_text("base\nchanged\n")
    run("git", "add", "-A")
    return d


def _run_flat(repo: str, models: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = _cli.main(["diff", "--task", TASK, "-C", repo, "-m", models, "--staged"])
    return rc, err.getvalue()


def test_flat_healthy_seat_with_finding_exits_findings_code():
    """RED for review-cli#517: a healthy seat whose body cites a finding must not
    exit 0. Expected after the fix: EXIT_FINDINGS_REPORTED (14)."""
    saved = _stub({"stubseat": FINDING_BODY})
    d = _staged_repo()
    try:
        with _Env():
            rc, err = _run_flat(d.name, "stubseat")
        assert rc == 14, (
            f"healthy seat reporting a file:line finding must exit 14 "
            f"(EXIT_FINDINGS_REPORTED), got {rc}; stderr:\n{err}"
        )
        assert "stubseat" in err and "finding" in err.lower(), err
    finally:
        _restore(saved)
        d.cleanup()


def test_host_port_urls_are_not_file_citations():
    """Network endpoints cannot turn a clean verdict into a cited finding."""
    for url in (
        "example.com:443", "api.example.org:8080", "localhost:8000",
        "api.example.ai:443", "example.me:443", "example.xyz:8080",
        "api.example.tech:443", "example.arbitrarytld:1234", "API.Example.XyZ:443",
        "api-2.example.future:443", "192.168.1.20:9000",
        "127.0.0.1:8080", "https://example.com:443", "http://host/file.py:42",
        "ftp://host/src/worker:42", "//example.com:443",
        "custom+transport://file.py:42", "https://file.py:42",
        "https://api.example.arbitrarytld:443/src/file.py:42",
    ):
        assert not _panel._clause_has_citation(url), url
        assert not _panel.body_reports_findings(f"No issues at {url}"), url
        assert _panel.clean_verdict_missing_evidence(f"No issues at {url}"), url
    for citation in (
        "./file.py:42", "reviewlib/foo.py:42", "Makefile:42", "Dockerfile:7",
        "README:1", "src/worker:42", "bin/review:42", "src/example.ai:443",
        "./example.xyz:42", "foo_bar.py:42", "file.py:42",
        "app.test.ts:42", "settings.json:42", "pyproject.toml:42",
        "config.yaml:42", ".env:42", "main.go:42", "view.swift:42",
    ):
        assert _panel._clause_has_citation(citation), citation
        assert _panel.body_reports_findings(f"No issues found. {citation} is unsafe"), citation
        assert not _panel.clean_verdict_missing_evidence(f"No issues at {citation}"), citation
        assert _panel.body_reports_findings(f"{citation} is unsafe; checked: unrelated"), citation


def test_citation_extension_uses_final_suffix_and_preserves_slash_paths():
    """Bare names use a case-insensitive suffix; slash paths need no known suffix."""
    for citation in (
        "example.xyz.py:42", "EXAMPLE.PY:42", "config.YAML:42",
        "src/example.unknown:42", "src/example.123:42", "src/example.com:443",
    ):
        assert _panel._clause_has_citation(citation), citation
        assert _panel.body_reports_findings(f"No issues. {citation} is unsafe"), citation
        assert not _panel.clean_verdict_missing_evidence(f"No issues at {citation}"), citation
    for endpoint in ("example.py.xyz:42", "EXAMPLE.PY.XYZ:42", "example.123:42"):
        assert not _panel._clause_has_citation(endpoint), endpoint
        assert not _panel.body_reports_findings(f"No issues at {endpoint}"), endpoint
        assert _panel.clean_verdict_missing_evidence(f"No issues at {endpoint}"), endpoint


def test_urls_do_not_expose_file_citations_and_config_is_a_file():
    for body in (
        "No issues at https://example.com?next=file.py:42",
        "No issues at https://example.com#file.py:42",
        "No issues at //example.com?next=src/worker:42",
    ):
        assert not _panel.body_reports_findings(body), body
        assert _panel.clean_verdict_missing_evidence(body), body
    citation = "settings.config:42"
    assert _panel._clause_has_citation(citation)
    assert _panel.body_reports_findings(f"No issues found. {citation} is unsafe")
    assert not _panel.clean_verdict_missing_evidence(f"No issues at {citation}")


def test_url_semicolons_do_not_expose_file_citations():
    for url in (
        "https://example.com?next=ok;file.py:42",
        "https://example.com#ok;settings.config:42",
        "//example.com?next=ok;file.py:42",
        "//example.com#ok;settings.config:42",
    ):
        body = f"No issues at {url}"
        assert not _panel.body_reports_findings(body), body
        assert _panel.clean_verdict_missing_evidence(body), body
        assert not _panel._clause_has_citation(url), url

        finding = f"{body} checked: URL inspected; file.py:42 is unsafe"
        assert _panel.body_reports_findings(finding), finding
        assert not _panel.clean_verdict_missing_evidence(finding), finding

        checked = f"{body}; checked: file.py:42 is safe"
        assert not _panel.body_reports_findings(checked), checked
        assert not _panel.clean_verdict_missing_evidence(checked), checked


def test_checked_url_does_not_hide_following_clause_finding():
    body = "checked: redirect https://example.com; P1: settings.config:42 is unsafe"
    assert _panel.body_reports_findings(body)
    assert not _panel.clean_verdict_missing_evidence(body)


def test_checked_clause_does_not_hide_same_line_finding():
    """A ruled-out check cannot launder a separate same-line finding."""
    for body in (
        "checked: concurrent writes are locked; " + FINDING_BODY,
        FINDING_BODY + "; checked: concurrent writes are locked",
        "checked: lock at reviewlib/bar.py:10 rules out races; " + FINDING_BODY,
    ):
        assert _panel.body_reports_findings(body), body
    assert not _panel.body_reports_findings(
        "checked: lock at reviewlib/bar.py:10 rules out races; "
        "checked: reviewlib/foo.py:42 validates input"
    )


def test_checked_marker_before_same_clause_citation_is_clean():
    """A checked marker explicitly rules out the citation that follows it."""
    assert not _panel.body_reports_findings("checked: reviewlib/bar.py:10 is ruled out")
    for citation in ("reviewlib/foo.py:42", "Makefile:42", "Dockerfile:7", "README:1", "config/Makefile:9"):
        body = f"checked: locking at {citation} prevents concurrent writes — ruled out"
        assert not _panel.body_reports_findings(body), body
    for body in ("checked: unrelated", "No issues, checked: locking", CLEAN_BODY):
        assert not _panel.body_reports_findings(body), body


def test_citation_before_same_clause_checked_marker_remains_finding():
    """A later unrelated check cannot rule out an earlier citation."""
    assert _panel.body_reports_findings(
        "P1: reviewlib/foo.py:42 is unsafe, checked: unrelated"
    )
    for citation in ("reviewlib/foo.py:42", "Makefile:42", "Dockerfile:7", "README:1", "config/Makefile:9"):
        for suffix in ("", ", checked: unrelated locking is safe"):
            body = f"P1: {citation} is unsafe{suffix}"
            assert _panel.body_reports_findings(body), body


def test_visual_diff_builtin_prompt_retains_default_classification_and_composition():
    """The visual CLI default retains the findings contract after image composition."""
    from unittest.mock import patch
    from reviewlib.features.visual.compose import VisualComposition

    prompts = []
    output, errors = io.StringIO(), io.StringIO()

    def backend(model, prompt, diff, cwd, timeout, round_no=0, images=None, effort=None):
        prompts.append(prompt)
        return ReviewResult(model=model, command="stub", returncode=0,
                            stdout=FINDING_BODY, stderr="")

    composition = VisualComposition("keep", "", "\n[visual context]", Path("shot.png"))
    saved = _stub({"stubseat": FINDING_BODY})
    try:
        with _Env(), identity_provider_chain(), redirect_stdout(output), redirect_stderr(errors), \
                patch("reviewlib.cli._git_diff", return_value="+changed"), \
                patch("reviewlib.cli._read_stdin_if_piped", return_value=None), \
                patch("reviewlib.features.visual.compose.build_mode_visual_context",
                      return_value=composition), \
                patch.object(_panel, "resolve_backend", return_value=backend), \
                patch.object(_panel, "review_with_images", side_effect=backend), \
                patch.object(_review_mode, "resolve_backend", return_value=backend):
            rc = _cli.main(["visual", "shot.png", "--diff", "--task", TASK,
                            "-C", str(REPO_ROOT), "-m", "stubseat"])
        assert prompts and all("[visual context]" in prompt for prompt in prompts), (rc, output.getvalue(), errors.getvalue())
        assert rc == 14, f"visual built-in prompt must classify citation findings, got {rc}"
    finally:
        _restore(saved)


def test_extensionless_file_citations_count_without_prose_false_positives():
    """DEFAULT_PROMPT's file:line grammar includes conventional extensionless files."""
    for citation in ("Makefile:42", "Dockerfile:7", "README:1", "config/Makefile:9"):
        assert _panel.body_reports_findings(
            f"No issues found. P1: {citation} has an unsafe default."
        ), citation
    for prose in ("line:42", "version:7", "status:1", "FooMakefile:42", "README:1suffix", "12:30", "P1:42", "https://example.com:443"):
        assert not _panel.body_reports_findings(f"No issues found. {prose}"), prose


def test_custom_prompt_prefixed_with_default_is_not_classified_as_default_contract():
    """Prompt provenance, not text prefix, selects the DEFAULT_PROMPT findings contract."""
    saved = _stub({"stubseat": FINDING_BODY})
    d = _staged_repo()
    try:
        out, err = io.StringIO(), io.StringIO()
        custom_prompt = DEFAULT_PROMPT + "\nReturn only APPROVED or BLOCKED."
        with _Env(), redirect_stdout(out), redirect_stderr(err):
            rc = _cli.main(
                [
                    "diff",
                    "--task",
                    TASK,
                    "-C",
                    d.name,
                    "-m",
                    "stubseat",
                    "--staged",
                    "--prompt",
                    custom_prompt,
                ]
            )
        assert rc == 0, (
            "a genuinely custom prompt must not classify a file:line body under "
            f"the DEFAULT_PROMPT contract; got {rc}\nstdout:\n{out.getvalue()}\nstderr:\n{err.getvalue()}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_cli_equal_form_prompt_is_explicitly_custom():
    """`--prompt=...` must disable the DEFAULT_PROMPT-only findings contract."""
    saved = _stub({"stubseat": FINDING_BODY})
    d = _staged_repo()
    try:
        out, err = io.StringIO(), io.StringIO()
        custom_prompt = DEFAULT_PROMPT + "\nReturn only APPROVED or BLOCKED."
        with _Env(), redirect_stdout(out), redirect_stderr(err):
            rc = _cli.main(
                [
                    "diff",
                    "--task",
                    TASK,
                    "-C",
                    d.name,
                    "-m",
                    "stubseat",
                    "--staged",
                    f"--prompt={custom_prompt}",
                ]
            )
        assert rc == 0, (
            "`--prompt=...` is an explicit custom prompt even when it starts with "
            f"DEFAULT_PROMPT; got {rc}\nstdout:\n{out.getvalue()}\nstderr:\n{err.getvalue()}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_cli_explicit_exact_default_text_is_custom():
    """Supplying the default text explicitly still selects a custom contract."""
    saved = _stub({"stubseat": FINDING_BODY})
    d = _staged_repo()
    try:
        for override in (["--prompt", DEFAULT_PROMPT], ["--prompt=" + DEFAULT_PROMPT]):
            with _Env(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = _cli.main([
                    "diff", "--task", TASK, "-C", d.name, "-m", "stubseat", "--staged",
                    *override,
                ])
            assert rc == 0, (override[0], rc)
    finally:
        _restore(saved)
        d.cleanup()


def test_review_handler_threads_prompt_provenance_to_mode_review():
    """The handler must pass provenance separately from the composed prompt text."""
    captured: dict[str, object] = {}
    saved = _review_mode.mode_review

    def _fake_mode_review(*args, **kwargs):
        captured["prompt"] = args[1]
        captured["prompt_is_default"] = kwargs.get("prompt_is_default")
        return 0

    _review_mode.mode_review = _fake_mode_review
    try:
        ctx = ModeContext(
            args=type(
                "Args", (), {"prompt": DEFAULT_PROMPT, "staged": False, "commit": False}
            )(),
            models=["stubseat"],
            diff="+x",
            cwd=REPO_ROOT,
            timeout=5,
            with_visual=lambda text: text + "\n[visual]",
            prompt_is_default=False,
        )
        assert _review_mode._handler(ctx) == 0
    finally:
        _review_mode.mode_review = saved
    assert captured == {
        "prompt": DEFAULT_PROMPT + "\n[visual]",
        "prompt_is_default": False,
    }


def test_handler_classifies_only_explicit_default_provenance_in_flat_and_board():
    """Visual composition retains provenance; custom copies of the default do not."""
    saved = _stub({"stubseat": FINDING_BODY})
    try:
        for board in (None, [BoardReviewer("stubseat", "correctness", "One")]):
            for is_default in (False, True):
                for suffix in ("", "\nReturn only APPROVED or BLOCKED."):
                    with _Env(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        ctx = ModeContext(
                            args=type("Args", (), {"prompt": DEFAULT_PROMPT + suffix,
                                                  "staged": False, "commit": False})(),
                            models=["stubseat"], diff="+changed", cwd=REPO_ROOT, timeout=5,
                            with_visual=lambda text: text + "\n[visual context]",
                            prompt_is_default=is_default,
                            extra={"board": board, "exact_board": True},
                        )
                        rc = _review_mode._handler(ctx)
                    assert rc == (14 if is_default else 0), (board, is_default, suffix, rc)
    finally:
        _restore(saved)


def test_direct_default_prompt_infers_contract_when_provenance_is_omitted():
    """Legacy flat/board callers retain default and visual-composed contracts."""
    from types import SimpleNamespace

    prompts = (
        DEFAULT_PROMPT,
        _cli._with_visual(
            DEFAULT_PROMPT, SimpleNamespace(context_note="\n[visual context]")
        ),
    )
    for body, expected in (
        (FINDING_BODY, 14),
        (CLEAN_BODY, 0),
        ("P1: reviewlib/foo.py:42 is unsafe; checked: unrelated locking", 14),
    ):
        saved = _stub({"stubseat": body})
        try:
            for board in (None, [BoardReviewer("stubseat", "correctness", "One")]):
                for prompt in prompts:
                    with _Env(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        rc = _review_mode.mode_review(
                            ["stubseat"], prompt, "+changed", REPO_ROOT, 5,
                            staged=False, board=board, exact_board=True,
                        )
                    assert rc == expected, (board, prompt, body, rc)
        finally:
            _restore(saved)


def test_flat_all_clean_verdicts_still_exit_zero():
    """The other side of the contract: honest clean verdicts — a `checked:`
    statement, a bare clean phrase, and even a long evidence-bearing clean body —
    must keep exiting 0."""
    saved = _stub(
        {
            "seatclean": CLEAN_BODY,
            "seatbare": BARE_CLEAN_BODY,
            "seatlong": CLEAN_BODY * 30,
        }
    )
    d = _staged_repo()
    try:
        with _Env():
            rc, err = _run_flat(d.name, "seatclean,seatbare,seatlong")
        assert rc == 0, f"all-clean review must stay exit 0, got {rc}; stderr:\n{err}"
    finally:
        _restore(saved)
        d.cleanup()


def test_long_checked_body_does_not_hide_later_cited_finding():
    """Finding evidence beyond the warning heuristic's cap still blocks a pass."""
    body = (
        CLEAN_BODY
        + "\n"
        + "x" * _panel._EVIDENCE_SCAN_MAX_LEN
        + "\nP1: reviewlib/panel.py:332 drops cited findings from long verdicts."
    )
    assert len(body) > _panel._EVIDENCE_SCAN_MAX_LEN
    assert _panel.body_reports_findings(body), "long cited finding classified clean"


def test_long_checked_body_with_finding_exits_fourteen():
    """The mode must propagate a late citation after a long clean section."""
    body = CLEAN_BODY * 30 + "\nP1: reviewlib/panel.py:332 drops cited findings."
    assert len(body) > _panel._EVIDENCE_SCAN_MAX_LEN
    saved = _stub({"seatlong": body})
    d = _staged_repo()
    try:
        with _Env():
            rc, _err = _run_flat(d.name, "seatlong")
        assert rc == 14, f"long cited finding must exit 14, got {rc}"
    finally:
        _restore(saved)
        d.cleanup()


def test_long_unbroken_tokens_preserve_clean_and_fail_closed_verdicts():
    """A token longer than the warning cap must not hide its citation suffix."""
    for token in ("x", "x."):
        blob = token * (_panel._EVIDENCE_SCAN_MAX_LEN * 3)
        assert not _panel.body_reports_findings("No issues.\n" + blob)
        assert _panel.body_reports_findings(blob)
        assert _panel.body_reports_findings("No issues.\n" + blob + ".py:332")
        assert not _panel.body_reports_findings("checked: " + blob + ".py:332")


def test_flat_contract_violating_skim_fails_closed():
    """A body with neither a finding citation nor a clean-verdict signal violates
    DEFAULT_PROMPT's contract; it must fail CLOSED into the findings class, not
    pass as clean."""
    saved = _stub({"skimmer": SKIM_BODY})
    d = _staged_repo()
    try:
        with _Env():
            rc, _err = _run_flat(d.name, "skimmer")
        assert rc == 14, f"skim body must fail closed (14), got {rc}"
    finally:
        _restore(saved)
        d.cleanup()


def test_cli_custom_prompt_approved_exits_zero():
    """A replacement prompt defines its own verdict contract."""
    saved = _stub({"stubseat": "APPROVED"})
    d = _staged_repo()
    try:
        with _Env():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = _cli.main(
                    [
                        "diff",
                        "--task",
                        TASK,
                        "-C",
                        d.name,
                        "-m",
                        "stubseat",
                        "--staged",
                        "--prompt",
                        "Answer APPROVED if safe",
                    ]
                )
        assert rc == 0, (
            f"custom APPROVED verdict must exit 0, got {rc}; stderr:\n{err.getvalue()}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_board_custom_prompt_approved_exits_zero():
    """Board dispatch also preserves a replacement prompt's verdict contract."""
    saved = _stub({"stubseat": "APPROVED"})
    d = _staged_repo()
    try:
        with _Env():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = _review_mode.mode_review(
                    ["stubseat"],
                    "Answer APPROVED if safe",
                    "diff --git a/f.txt b/f.txt\n+changed\n",
                    Path(d.name),
                    5,
                    staged=False,
                    board=[BoardReviewer("stubseat", "correctness", "One")],
                    exact_board=True,
                )
        assert rc == 0, (
            f"custom board APPROVED verdict must exit 0, got {rc}; stderr:\n{err.getvalue()}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_flat_custom_prompt_approved_exits_zero():
    """Flat dispatch preserves the custom contract without a reviewer board."""
    saved = _stub({"stubseat": "APPROVED"})
    d = _staged_repo()
    try:
        with _Env():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = _review_mode.mode_review(
                    ["stubseat"],
                    "Answer APPROVED if safe",
                    "diff --git a/f.txt b/f.txt\n+changed\n",
                    Path(d.name),
                    5,
                    staged=False,
                )
        assert rc == 0, (
            f"custom flat APPROVED verdict must exit 0, got {rc}; stderr:\n{err.getvalue()}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_flat_degraded_seat_keeps_exit_one_even_with_findings():
    """Transport errors are NOT findings: a dead seat next to a
    findings-reporting healthy seat keeps the exact existing degraded semantics
    (exit 1), and the healthy seat's findings are still surfaced on stderr."""
    saved = _stub(
        {"healthy": FINDING_BODY},
        fail={"deadseat"},
    )
    d = _staged_repo()
    try:
        with _Env():
            rc, err = _run_flat(d.name, "healthy,deadseat")
        assert rc == 1, (
            f"a degraded run keeps its exit-1 partial-failure semantics even "
            f"when a healthy seat reports findings; got {rc}"
        )
    finally:
        _restore(saved)
        d.cleanup()


def test_flat_finding_still_stamps_and_notices():
    """Findings remain informational for the commit gate: a staged review with
    findings still writes the marker/stamp (seat health keyed), while the
    stderr notice names the reporting seat."""
    saved = _stub({"stubseat": FINDING_BODY})
    d = _staged_repo()
    try:
        with _Env():
            rc, err = _run_flat(d.name, "stubseat")
            marker = Path(os.environ["REVIEW_MARKER"])
            assert rc == 14, rc
            assert marker.exists(), (
                "a staged healthy review keeps writing the commit-gate marker "
                "even when findings are reported (gate semantics unchanged)"
            )
            assert "stubseat" in err, err
    finally:
        _restore(saved)
        d.cleanup()


def test_board_healthy_seat_with_finding_exits_findings_code():
    """The board (default dispatch) path gets the same contract: an exact board
    whose every seat is healthy, one reporting a finding, exits 14 instead of
    lying with 0."""
    board = [
        BoardReviewer("seatone", "correctness", "One"),
        BoardReviewer("seattwo", "quality", "Two"),
    ]
    saved = _stub(
        {
            "seatone": FINDING_BODY,
            "seattwo": CLEAN_BODY,
        }
    )
    d = _staged_repo()
    try:
        with _Env():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = _review_mode.mode_review(
                    ["seatone", "seattwo"],
                    _review_mode.DEFAULT_PROMPT,
                    "diff --git a/f.txt b/f.txt\n+changed\n",
                    Path(d.name),
                    5,
                    staged=False,
                    board=board,
                    exact_board=True,
                    prompt_is_default=True,
                )
        assert rc == 14, (
            f"board path: healthy board with one finding seat must exit 14, "
            f"got {rc}; stderr:\n{err.getvalue()}"
        )
        # The notice names the reporting seat as the output renders it (the
        # board path's result headers carry the display label).
        assert "One" in err.getvalue(), err.getvalue()
    finally:
        _restore(saved)
        d.cleanup()


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with identity_provider_chain():
                    fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
