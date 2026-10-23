#!/usr/bin/env python3
"""A diff over `$REVIEW_DIFF_MAX_BYTES` is SPLIT into passes, never truncated.

History: an uncapped `git diff` once sent a real 6.5MB / 583-file diff whole to every seat
(2026-08 token-burn investigation); the first fix cut it at 300000 bytes with a marker, so
no seat ever saw the tail of a big change and `--staged` stamps / `--commit` checkpoints
were refused. `reviewlib.diff_chunks.split_diff` now cuts at `diff --git` file boundaries
(an oversized file at `@@` hunk boundaries), `review diff` runs the board once per chunk
inside ONE run, and `reviewlib.diff_passes` merges the per-seat answers.

Pinned here:
  * the pure splitter (packing, hunk splitting, the unsplittable hunk, no content loss, a
    >1 MB / 400-file synthetic diff);
  * the merge helpers;
  * `mode_review`'s flat and board paths in-process (every file reaches some seat, the
    prompts say which part they are, the single-pass path is untouched, stamp/checkpoint
    follow the AND of every pass, the unsplittable hunk fails loudly before any seat runs,
    a piped diff is sent whole);
  * brainstorm/quorum/just-ask refusing an over-budget diff instead of cutting it;
  * the REAL `bin/review diff` CLI end to end over a >1 MB staged diff with the fake
    backend, and over an unsplittable hunk.

Runs under pytest and as a plain script (`python3 tests/test_diff_chunking.py`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends  # noqa: E402
from reviewlib import diff_chunks  # noqa: E402
from reviewlib import diff_passes  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.diff_chunks import (  # noqa: E402
    DIFF_MAX_BYTES_DEFAULT,
    EXIT_DIFF_TOO_LARGE,
    DiffTooLargeError,
    DiffUnsplittableError,
    split_diff,
)
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.review import mode_review  # noqa: E402

REVIEW_BIN = REPO_ROOT / "bin" / "review"

_ENV_VARS = (
    "REVIEW_FAKE_BACKEND",
    "REVIEW_FAKE_RECORD_DIR",
    "REVIEW_MARKER",
    "REVIEW_DIFF_MAX_BYTES",
    "HOME",
)


class _EnvSandbox:
    """Snapshot/restore the env this file touches; clears the budget var so a host that
    exports `$REVIEW_DIFF_MAX_BYTES` cannot change a default-budget assertion."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _ENV_VARS}
        for name in (
            "REVIEW_FAKE_BACKEND",
            "REVIEW_FAKE_RECORD_DIR",
            "REVIEW_DIFF_MAX_BYTES",
        ):
            os.environ.pop(name, None)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


# --- synthetic diffs -----------------------------------------------------------------
def file_diff(path: str, hunks: int = 1, lines: int = 3, payload: str = "x") -> str:
    """One `diff --git` section with `hunks` hunks of `lines` added lines each."""
    out = f"diff --git a/{path} b/{path}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}\n"
    for h in range(hunks):
        start = h * 1000 + 1
        out += f"@@ -{start},{lines} +{start},{lines} @@ def ctx{h}\n"
        out += "".join(f"+{payload}{h}_{i}\n" for i in range(lines))
    return out


def many_files(count: int, **kwargs) -> tuple[str, list[str]]:
    paths = [f"src/pkg_{i:04d}/mod.py" for i in range(count)]
    return "".join(file_diff(p, **kwargs) for p in paths), paths


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


# --- the pure splitter ---------------------------------------------------------------
def test_within_budget_diff_is_one_unchanged_chunk():
    diff, paths = many_files(3)
    (chunk,) = split_diff(diff, nbytes(diff))
    assert chunk.text == diff
    assert (chunk.index, chunk.total) == (1, 1)
    assert list(chunk.files) == paths


def test_budget_zero_or_negative_never_splits():
    diff, _ = many_files(50)
    for budget in (0, -1):
        (chunk,) = split_diff(diff, budget)
        assert chunk.text == diff


def test_whole_files_are_packed_greedily_at_file_boundaries():
    diff, paths = many_files(7)
    one = nbytes(file_diff(paths[0]))
    chunks = split_diff(diff, one * 3 + 5)
    assert [len(c.files) for c in chunks] == [3, 3, 1]
    assert all(c.size <= one * 3 + 5 for c in chunks)
    assert [c.index for c in chunks] == [1, 2, 3] and {c.total for c in chunks} == {3}
    # Every file lands in exactly one chunk, in diff order, byte-for-byte.
    assert "".join(c.text for c in chunks) == diff
    assert [p for c in chunks for p in c.files] == paths


def test_oversized_file_is_split_at_hunk_boundaries_with_repeated_header():
    big = file_diff("src/huge.py", hunks=24, lines=5)
    small_before = file_diff("a.py")
    small_after = file_diff("z.py")
    diff = small_before + big + small_after
    budget = 900
    assert nbytes(big) > budget
    chunks = split_diff(diff, budget)
    assert all(c.size <= budget for c in chunks), [c.size for c in chunks]
    pieces = [p for c in chunks for p in c.pieces if p.path == "src/huge.py"]
    assert len(pieces) > 1
    header = "diff --git a/src/huge.py b/src/huge.py\nindex 111..222 100644\n--- a/src/huge.py\n+++ b/src/huge.py\n"
    for number, piece in enumerate(pieces, start=1):
        assert piece.header == header
        assert piece.text.startswith(header)
        assert f"[chunk {number}/{len(pieces)} of src/huge.py]" in piece.text
    # No content loss: the pieces' bodies are exactly the original hunk text, in order.
    original_hunks = big[len(header) :]
    assert "".join(p.body for p in pieces) == original_hunks
    # The small files around it are untouched whole sections.
    assert chunks[0].text.startswith(small_before)
    assert chunks[-1].text.endswith(small_after)


def test_unsplittable_hunk_raises_naming_file_and_sizes():
    diff = file_diff("ok.py") + file_diff("src/giant.py", hunks=1, lines=400)
    try:
        split_diff(diff, 2000)
    except DiffUnsplittableError as exc:
        message = str(exc)
        assert exc.path == "src/giant.py"
        assert "src/giant.py" in message
        assert "hunk 1 of 1" in message
        assert "2000 bytes" in message and "REVIEW_DIFF_MAX_BYTES" in message
        assert exc.needed > 2000
    else:
        raise AssertionError("an unsplittable hunk must raise")


def test_header_only_section_over_budget_is_unsplittable():
    diff = file_diff("a.py") + (
        "diff --git a/b.bin b/b.bin\nnew file mode 100644\n"
        + "index 0000000..1111111\n"
        + "GIT binary patch\n"
        + "literal 1\n" * 500
    )
    try:
        split_diff(diff, 300)
    except DiffUnsplittableError as exc:
        assert exc.path == "b.bin"
    else:
        raise AssertionError("an oversized header-only section must raise")


def test_text_before_the_first_file_header_is_kept():
    preamble = "commit 1234\nAuthor: x\n\n"
    diff = preamble + file_diff("a.py") + file_diff("b.py")
    chunks = split_diff(diff, nbytes(file_diff("a.py")) + nbytes(preamble) - 1)
    assert "".join(c.text for c in chunks) == diff
    assert chunks[0].text.startswith(preamble)


def test_carriage_returns_inside_a_hunk_do_not_create_phantom_hunks():
    # A "\r@@" in the middle of a line is NOT a hunk header (only "\n@@" is), so the
    # two real hunks stay two pieces and nothing is lost.
    filler = "".join(f"+filler line {i} " + "z" * 40 + "\n" for i in range(20))
    body = "diff --git a/w.txt b/w.txt\n--- a/w.txt\n+++ b/w.txt\n"
    body += "@@ -1,2 +1,2 @@\n+a\r@@ -9 +9 @@\n+b\n" + filler
    body += "@@ -50,1 +50,1 @@\n+c\n" + filler
    (chunk,) = split_diff(body, 0)
    assert "".join(p.body for p in chunk.pieces) == body[body.index("@@") :]
    pieces = split_diff(body, nbytes(body) - 1)
    assert "".join(p.body for c in pieces for p in c.pieces) == body[body.index("@@") :]
    assert sum(len(c.pieces) for c in pieces) == 2  # exactly the two real hunks


def test_400_file_over_1mb_diff_covers_every_file_within_budget():
    diff, paths = many_files(400, hunks=2, lines=50, payload="synthetic line of text ")
    assert nbytes(diff) > 1_000_000, nbytes(diff)
    chunks = split_diff(diff, DIFF_MAX_BYTES_DEFAULT)
    assert len(chunks) >= 4
    assert all(c.size <= DIFF_MAX_BYTES_DEFAULT for c in chunks)
    seen = [p for c in chunks for p in c.files]
    assert seen == paths, "every file in exactly one chunk, in diff order"
    assert all(path in "".join(c.text for c in chunks) for path in paths)
    assert "".join(c.text for c in chunks) == diff
    for chunk in chunks:
        for path in chunk.files:
            assert f"diff --git a/{path} b/{path}" in chunk.text


def test_budget_env_parsing():
    with _EnvSandbox():
        assert diff_chunks.diff_budget_bytes() == DIFF_MAX_BYTES_DEFAULT == 300_000
        for raw, expected in (
            ("12345", 12345),
            ("0", 0),
            ("-7", -7),
            ("  ", DIFF_MAX_BYTES_DEFAULT),
            ("lots", DIFF_MAX_BYTES_DEFAULT),
        ):
            os.environ["REVIEW_DIFF_MAX_BYTES"] = raw
            assert diff_chunks.diff_budget_bytes() == expected, raw


def test_prompt_note_names_the_part_and_its_files():
    diff, paths = many_files(6)
    chunks = split_diff(diff, nbytes(file_diff(paths[0])) * 2 + 5)
    note = chunks[1].prompt_note()
    assert "Part 2 of 3 of the change" in note
    assert all(p in note for p in chunks[1].files)
    assert not any(p in note for p in chunks[0].files)
    assert "separate passes" in note
    assert "other 2 parts" in note


def test_single_prompt_guard():
    diff, _ = many_files(50)
    with _EnvSandbox():
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
        try:
            diff_chunks.require_single_prompt_diff(diff, "quorum")
        except DiffTooLargeError as exc:
            assert "50 file(s)" in str(exc) and "1000 bytes" in str(exc)
            assert "never truncates" in str(exc)
        else:
            raise AssertionError("over-budget diff must raise")
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "0"
        diff_chunks.require_single_prompt_diff(diff, "quorum")  # disabled: no raise


# --- merge helpers -------------------------------------------------------------------
def _res(model, stdout="ok", rc=0, stderr="", tokens=0):
    return ReviewResult(
        model=model,
        command=f"cmd:{model}",
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        prompt_tokens=tokens,
    )


def test_merge_passes_one_report_per_seat_with_part_headings():
    diff, paths = many_files(4)
    chunks = split_diff(diff, nbytes(file_diff(paths[0])) * 2 + 5)
    passes = [
        [_res("A", "finding a.py:1", tokens=3), _res("B", "b1")],
        [_res("A", "finding z.py:2", tokens=4), _res("B", "b2", rc=1, stderr="boom")],
    ]
    merged = diff_passes.merge_passes(passes, chunks, check_evidence=False)
    assert [r.model for r in merged] == ["A", "B"]
    a, b = merged
    assert "### Part 1/2" in a.stdout and "### Part 2/2" in a.stdout
    assert a.stdout.index("a.py:1") < a.stdout.index("z.py:2")
    assert a.returncode == 0 and a.prompt_tokens == 7
    assert b.returncode == 1, "one failed part makes the merged seat failed"
    assert "boom" in b.stderr and "Part 2/2" in b.stderr


def test_merge_passes_single_pass_is_returned_untouched():
    diff, _ = many_files(2)
    (chunk,) = split_diff(diff, 0)
    passes = [[_res("A")]]
    assert diff_passes.merge_passes(passes, [chunk], check_evidence=True) is passes[0]
    assert diff_passes.with_part_note("prompt", chunk) == "prompt"


def test_merge_passes_inlines_the_missing_evidence_warning_per_part():
    diff, paths = many_files(2)
    chunks = split_diff(diff, nbytes(file_diff(paths[0])) + 5)
    passes = [[_res("A", "Looks good.")], [_res("A", "Wrong thing at x.py:3 — bug")]]
    (merged,) = diff_passes.merge_passes(passes, chunks, check_evidence=True)
    assert merged.stdout.count("WARNING: this reads as a clean verdict") == 1
    first_part = merged.stdout.split("### Part 2/2")[0]
    assert "WARNING: this reads as a clean verdict" in first_part


def test_merge_outcomes_is_one_outcome_with_each_seat_once():
    from reviewlib.panel import FailoverOutcome

    def outcome(degraded, models):
        results = [_res(m) for m in models]
        return FailoverOutcome(
            results=results,
            usable=results,
            target=2,
            degraded=degraded,
            usable_models=list(models),
            usable_roles=["correctness", "security"][: len(models)],
        )

    merged = diff_passes.merge_outcomes(
        [outcome(False, ["m1", "m2"]), outcome(True, ["m1", "m2"])]
    )
    assert merged.degraded is True, "any degraded pass degrades the run"
    assert merged.usable_models == ["m1", "m2"]
    assert merged.usable_roles == ["correctness", "security"]
    assert len(merged.results) == 4


# --- mode_review, in process ---------------------------------------------------------
class _Calls:
    """Thread-safe record of what every seat call received."""

    def __init__(self, fail_on_call: int | None = None, stdout: str = "ok"):
        import threading

        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.fail_on_call = fail_on_call
        self.stdout = stdout

    def backend(self, model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        with self.lock:
            self.records.append({"model": model, "prompt": prompt, "diff": diff})
            number = len(self.records)
        if self.fail_on_call is not None and number >= self.fail_on_call:
            return ReviewResult(
                model=model, command="fake", returncode=1, stdout="", stderr="boom"
            )
        return ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout=self.stdout,
            stderr="",
        )

    def covered_paths(self, paths):
        text = "".join(r["diff"] for r in self.records)
        return [p for p in paths if f"diff --git a/{p} b/{p}" in text]


@contextlib.contextmanager
def _flat_backend(calls: _Calls):
    saved = review_mode.resolve_backend
    review_mode.resolve_backend = lambda model: calls.backend
    try:
        yield
    finally:
        review_mode.resolve_backend = saved


@contextlib.contextmanager
def _board_backend(calls: _Calls):
    import reviewlib.panel as panel

    saved = (
        backends.backend_available,
        panel.backend_available,
        review_mode.backend_available,
        panel.resolve_backend,
    )
    backends.backend_available = lambda model: True
    panel.backend_available = lambda model: True
    review_mode.backend_available = lambda model: True
    panel.resolve_backend = lambda model: calls.backend
    try:
        yield
    finally:
        (
            backends.backend_available,
            panel.backend_available,
            review_mode.backend_available,
            panel.resolve_backend,
        ) = saved


def _run_mode_review(**kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mode_review(**kwargs)
    return rc, out.getvalue(), err.getvalue()


_BOARD = [
    BoardReviewer("codex", "correctness", "Codex"),
    BoardReviewer("claude", "security", "Claude"),
]


def test_flat_path_chunks_an_oversized_diff_and_every_file_reaches_a_seat():
    diff, paths = many_files(12, lines=20)
    budget = nbytes(file_diff(paths[0], lines=20)) * 4 + 10
    calls = _Calls()
    with _EnvSandbox(), _flat_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(budget)
        rc, out, err = _run_mode_review(
            models=["m1", "m2"],
            prompt="PROMPT",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
        )
    assert rc == 0, (rc, err)
    assert len(calls.records) == 2 * 3, "2 seats x 3 passes"
    assert calls.covered_paths(paths) == paths
    for record in calls.records:
        assert len(record["diff"].encode()) <= budget
        assert record["prompt"].startswith("PROMPT\n\nPart ")
        assert "of 3 of the change" in record["prompt"]
    for part in (1, 2, 3):
        assert f"Part {part} of 3" in "".join(r["prompt"] for r in calls.records)
    # One report per seat, its three parts under headings; the split announced on stderr.
    assert out.count("## m1 [ok]") == 1 and out.count("## m2 [ok]") == 1
    assert "### Part 1/3" in out and "### Part 3/3" in out
    assert "reviewing it in 3 passes" in err
    for path in paths:
        assert path in err, "the announcement lists every part's files"
    assert "truncated" not in err + out


def test_within_budget_diff_is_dispatched_exactly_as_before():
    diff, _ = many_files(3)
    calls = _Calls()
    with _EnvSandbox(), _flat_backend(calls):
        rc, out, err = _run_mode_review(
            models=["m1"],
            prompt="PROMPT",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
        )
    assert rc == 0
    (record,) = calls.records
    assert record["prompt"] == "PROMPT", "no part note on a single pass"
    assert record["diff"] == diff
    assert "### Part" not in out and "passes" not in err


def test_piped_diff_is_sent_whole_even_over_budget():
    diff, _ = many_files(30)
    calls = _Calls()
    with _EnvSandbox(), _flat_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "500"
        rc, _out, err = _run_mode_review(
            models=["m1"],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            diff_from_stdin=True,
        )
    assert rc == 0
    (record,) = calls.records
    assert record["diff"] == diff and record["prompt"] == "P"
    assert "passes" not in err


def test_budget_zero_sends_the_whole_diff_in_one_pass():
    diff, _ = many_files(30)
    calls = _Calls()
    with _EnvSandbox(), _flat_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "0"
        rc, *_ = _run_mode_review(
            models=["m1"],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
        )
    assert rc == 0 and len(calls.records) == 1 and calls.records[0]["diff"] == diff


def test_unsplittable_hunk_fails_loudly_before_any_seat_runs():
    diff = file_diff("small.py") + file_diff("src/giant.py", lines=500)
    calls = _Calls()
    with _EnvSandbox(), _flat_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "3000"
        rc, out, err = _run_mode_review(
            models=["m1"],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
        )
    assert rc == EXIT_DIFF_TOO_LARGE == 13
    assert calls.records == [], (
        "no seat may run on a diff that cannot be reviewed whole"
    )
    assert "src/giant.py" in err and "3000 bytes" in err and "hunk 1 of 1" in err
    assert out == ""


def test_board_path_chunks_runs_each_pass_in_one_run_and_reports_one_outcome():
    diff, paths = many_files(10, lines=20)
    budget = nbytes(file_diff(paths[0], lines=20)) * 5 + 10
    calls = _Calls()
    sink: list = []
    with _EnvSandbox(), _board_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(budget)
        rc, out, err = _run_mode_review(
            models=[],
            prompt="PROMPT",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            board=_BOARD,
            pool_size=2,
            outcome_sink=sink,
        )
    assert rc == 0, (rc, err)
    assert len(calls.records) == 2 * 2, "2 seats x 2 passes"
    assert calls.covered_paths(paths) == paths
    assert all(len(r["diff"].encode()) <= budget for r in calls.records)
    assert len(sink) == 1, "the passes are ONE run: exactly one outcome for the stats"
    assert sink[0].degraded is False and sink[0].usable_models == ["codex", "claude"]
    assert (
        out.count("## Codex [correctness]") == 1
        and out.count("## Claude [security]") == 1
    )
    assert "### Part 1/2" in out and "### Part 2/2" in out


def test_board_path_single_pass_is_unchanged():
    diff, _ = many_files(3)
    calls = _Calls()
    with _EnvSandbox(), _board_backend(calls):
        rc, out, _err = _run_mode_review(
            models=[],
            prompt="PROMPT",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            board=_BOARD,
            pool_size=2,
        )
    assert rc == 0
    assert len(calls.records) == 2
    assert all(
        r["diff"] == diff and r["prompt"].startswith("PROMPT") for r in calls.records
    )
    assert all("Part " not in r["prompt"] for r in calls.records)
    assert "### Part" not in out


def test_board_path_unsplittable_hunk_fails_loudly():
    diff = file_diff("src/giant.py", lines=500)
    calls = _Calls()
    with _EnvSandbox(), _board_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "3000"
        rc, _out, err = _run_mode_review(
            models=[],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            board=_BOARD,
            pool_size=2,
        )
    assert rc == EXIT_DIFF_TOO_LARGE
    assert calls.records == []
    assert "src/giant.py" in err


def _findings_block(path: str) -> str:
    payload = {
        "findings": [
            {
                "file": path,
                "line": 1,
                "severity": "P2",
                "confidence": "high",
                "verdict": "FINDING",
            }
        ]
    }
    return "review text\n```review-json\n" + json.dumps(payload) + "\n```\n"


def test_json_output_merges_every_passes_findings():
    diff, paths = many_files(6, lines=20)
    budget = nbytes(file_diff(paths[0], lines=20)) * 2 + 10
    calls = _Calls(stdout=_findings_block("src/x.py"))
    with _EnvSandbox(), _flat_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(budget)
        rc, out, _err = _run_mode_review(
            models=["m1"],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            json_output=True,
        )
    report = json.loads(out)
    assert len(calls.records) == 3
    assert len(report["findings"]) == 3, "one finding per pass, none dropped"
    assert all(f["source"] == "m1" for f in report["findings"])
    assert report["summary"] == {
        "expected": ["m1"],
        "reported": ["m1"],
        "degraded": [],
    }
    assert rc == 1, "FINDING verdicts present"


def test_json_output_marks_a_seat_degraded_when_any_pass_failed_to_parse():
    passes = [
        [_res("m1", _findings_block("a.py"))],
        [_res("m1", "no structured block here")],
    ]
    report = review_mode._build_json_report_passes(passes)
    assert (
        report["summary"]["degraded"] == ["m1"] and report["summary"]["reported"] == []
    )
    assert len(report["findings"]) == 1
    single = review_mode._build_json_report_passes([passes[0]])
    assert single == review_mode._build_json_report(passes[0])


# --- stamp / checkpoint ---------------------------------------------------------------
def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"], cwd=path, check=True
    )


def _stage_many(repo: Path, count: int, lines: int = 60) -> list[str]:
    paths = []
    for i in range(count):
        rel = f"pkg/mod_{i:03d}.py"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(
            "".join(f"def f{i}_{j}():\n    return {j}\n" for j in range(lines)),
            encoding="utf-8",
        )
        paths.append(rel)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return paths


def _staged_diff(repo: Path) -> str:
    return subprocess.run(
        ["git", "diff", "--cached"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit_count(repo: Path) -> int:
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return int(proc.stdout.strip()) if proc.returncode == 0 else 0


def test_split_staged_diff_is_stamped_and_checkpointed():
    calls = _Calls()
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        paths = _stage_many(repo, 8)
        diff = _staged_diff(repo)
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(nbytes(diff) // 3)
        with _flat_backend(calls):
            rc, _out, err = _run_mode_review(
                models=["m1"],
                prompt="P",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        assert rc == 0, (rc, err)
        assert len(calls.records) >= 3
        assert calls.covered_paths(paths) == paths
        assert _commit_count(repo) == 1, "the checkpoint commit must be created"
        assert (tmp / "last-review").exists(), "the gate marker must be written"
        assert "truncated" not in err and "no checkpoint created" not in err, err


def test_a_failed_pass_withholds_the_stamp_and_the_checkpoint():
    calls = _Calls(fail_on_call=2)
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        _stage_many(repo, 8)
        diff = _staged_diff(repo)
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(nbytes(diff) // 3)
        with _flat_backend(calls):
            rc, _out, err = _run_mode_review(
                models=["m1"],
                prompt="P",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        assert rc == 1, (rc, err)
        assert _commit_count(repo) == 0, (
            "a pass without a usable verdict: no checkpoint"
        )
        assert not (tmp / "last-review").exists(), "and no gate marker"
        assert "no checkpoint created" in err


def test_board_path_failed_pass_degrades_the_run():
    diff, _ = many_files(8, lines=20)
    budget = nbytes(file_diff("src/pkg_0000/mod.py", lines=20)) * 4 + 10
    # Every call from the 3rd on fails: pass 1 (2 seats) is fine, pass 2 has no seat left.
    calls = _Calls(fail_on_call=3)
    sink: list = []
    with _EnvSandbox(), _board_backend(calls):
        os.environ["REVIEW_DIFF_MAX_BYTES"] = str(budget)
        rc, _out, _err = _run_mode_review(
            models=[],
            prompt="P",
            diff=diff,
            cwd=REPO_ROOT,
            timeout=30,
            staged=False,
            board=_BOARD,
            pool_size=2,
            exact_board=True,
            outcome_sink=sink,
        )
    assert rc == 1
    assert sink[0].degraded is True


# --- single-prompt modes --------------------------------------------------------------
def test_text_modes_refuse_an_over_budget_diff_instead_of_cutting_it():
    from reviewlib.modes import brainstorm as bs_mod
    from reviewlib.modes import just_ask as ja_mod
    from reviewlib.modes import quorum as q_mod

    diff, _ = many_files(40)
    ran: list = []

    def _spy_panel(jobs, cwd, timeout):
        ran.append(jobs)
        return []

    saved = (ja_mod.run_panel, q_mod.run_panel, bs_mod.run_panel)
    ja_mod.run_panel = q_mod.run_panel = bs_mod.run_panel = _spy_panel
    try:
        with _EnvSandbox():
            os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
            calls = {
                "just-ask": lambda: ja_mod.mode_just_ask(
                    "q", ["m1"], diff, REPO_ROOT, 5
                ),
                "quorum": lambda: q_mod.mode_quorum(
                    "q", ["m1"], diff, REPO_ROOT, 5, ["mod"]
                ),
                "brainstorm": lambda: bs_mod.mode_brainstorm(
                    "t", ["m1"], REPO_ROOT, 5, ["mod"], 1, 1, diff=diff
                ),
            }
            for name, call in calls.items():
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = call()
                assert rc == EXIT_DIFF_TOO_LARGE, (name, rc)
                assert "40 file(s)" in err.getvalue(), (name, err.getvalue())
                assert "1000 bytes" in err.getvalue(), (name, err.getvalue())
                assert "review diff" in err.getvalue(), (name, err.getvalue())
    finally:
        ja_mod.run_panel, q_mod.run_panel, bs_mod.run_panel = saved
    assert ran == [], "no seat may be dispatched on a refused diff"


def test_just_ask_sends_a_piped_diff_whole_and_a_fitting_diff_unchanged():
    from reviewlib.modes import just_ask as ja_mod

    diff, _ = many_files(40)
    captured: dict = {}

    def _fake_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [_res(j.model) for j in jobs]

    saved = ja_mod.run_panel
    ja_mod.run_panel = _fake_panel
    try:
        with _EnvSandbox():
            os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
            rc = ja_mod.mode_just_ask(
                "q", ["m1"], diff, REPO_ROOT, 5, diff_from_stdin=True
            )
            assert rc == 0 and diff in captured["prompt"]
            os.environ.pop("REVIEW_DIFF_MAX_BYTES")
            rc = ja_mod.mode_just_ask("q", ["m1"], diff, REPO_ROOT, 5)
            assert rc == 0 and diff in captured["prompt"]
    finally:
        ja_mod.run_panel = saved


def test_git_diff_source_is_never_cut():
    """`cli._git_diff` returns the canonical diff the stamp/checkpoint re-derive; it must
    never shorten it, whatever its size."""
    from reviewlib import cli

    big = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert nbytes(big) > DIFF_MAX_BYTES_DEFAULT
    saved = cli._run
    cli._run = lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout=big, stderr=""
    )
    try:
        assert cli._git_diff(REPO_ROOT, staged=False) == big
    finally:
        cli._run = saved


# --- the real CLI, end to end ---------------------------------------------------------
def _cli_env(tmp: Path, budget: str | None) -> dict:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp / "home"),
        REVIEW_FAKE_BACKEND="1",
        REVIEW_FAKE_RECORD_DIR=str(tmp / "calls"),
        REVIEW_LOG_DIR=str(tmp / "logs"),
        REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
        REVIEW_MARKER=str(tmp / "marker"),
    )
    env.pop("REVIEW_DIFF_MAX_BYTES", None)
    if budget is not None:
        env["REVIEW_DIFF_MAX_BYTES"] = budget
    (tmp / "home").mkdir(exist_ok=True)
    return env


def _run_cli(tmp: Path, repo: Path, *args: str, budget: str | None = None):
    return subprocess.run(
        [
            sys.executable,
            str(REVIEW_BIN),
            "diff",
            "--staged",
            "--task",
            "CHUNK-1",
            "--no-tui",
            "-C",
            str(repo),
            *args,
        ],
        env=_cli_env(tmp, budget),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _recorded_calls(tmp: Path) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((tmp / "calls").glob("*.json"))
    ]


def test_e2e_cli_reviews_a_1mb_staged_diff_in_passes_covering_every_file():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        paths = _stage_many(repo, 400, lines=90)
        staged = _staged_diff(repo)
        assert nbytes(staged) > 1_000_000, nbytes(staged)

        proc = _run_cli(tmp, repo)  # the DEFAULT 300000-byte budget
        assert proc.returncode == 0, (proc.returncode, proc.stderr[-2000:])

        calls = _recorded_calls(tmp)
        union = "".join(c["diff"] for c in calls)
        missing = [p for p in paths if f"diff --git a/{p} b/{p}" not in union]
        assert missing == [], f"files no seat ever saw: {missing[:5]}"
        assert all(nbytes(c["diff"]) <= DIFF_MAX_BYTES_DEFAULT for c in calls)
        parts = {
            int(c["prompt"].split("Part ", 1)[1].split(" of ", 1)[0]) for c in calls
        }
        total = int(calls[0]["prompt"].split(" of ", 1)[1].split(" ", 1)[0])
        assert total >= 4 and parts == set(range(1, total + 1))
        assert len(calls) == 3 * total, "3 board seats x every pass"
        for part in range(1, total + 1):
            assert f"### Part {part}/{total}" in proc.stdout
        assert f"reviewing it in {total} passes" in proc.stderr
        assert "truncated" not in proc.stdout + proc.stderr

        # ONE run: one stats record carrying every seat and the whole diff's file list;
        # the staged gate is satisfied because every pass was usable.
        records = [
            json.loads(line)
            for line in (tmp / "stats.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 1, records
        assert records[0]["task_code"] == "CHUNK-1" and records[0]["passed"] is True
        assert sorted(records[0]["diff_files"]) == sorted(paths)
        assert (tmp / "marker").exists(), "a fully reviewed staged diff is stamped"
        assert (repo / ".git" / "review-stamp").exists()


def test_e2e_cli_fails_loudly_on_an_unsplittable_hunk():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "small.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "giant.txt").write_text(
            "".join(f"line {i} " + "y" * 90 + "\n" for i in range(4000)),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        assert nbytes(_staged_diff(repo)) > DIFF_MAX_BYTES_DEFAULT

        proc = _run_cli(tmp, repo)
        assert proc.returncode == EXIT_DIFF_TOO_LARGE, (
            proc.returncode,
            proc.stderr[-2000:],
        )
        assert "giant.txt" in proc.stderr
        assert "300000 bytes" in proc.stderr and "hunk 1 of 1" in proc.stderr
        assert not (tmp / "calls").exists() or _recorded_calls(tmp) == [], (
            "no seat may run when the diff cannot be reviewed whole"
        )
        assert not (tmp / "marker").exists()

        # The way out the message names works: a budget above the hunk reviews it whole.
        ok = _run_cli(tmp, repo, budget="0")
        assert ok.returncode == 0, (ok.returncode, ok.stderr[-2000:])
        assert any("giant.txt" in c["diff"] for c in _recorded_calls(tmp))


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
