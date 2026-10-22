#!/usr/bin/env python3
"""scripts/review-remote: argument splitting, the dirty-state payload it ships, and the
import step that decides which box artifacts (records / marker / stamp) reach the local gates.

Runs as a plain script (no pytest needed, like the other files under the CI runner)::

    python3 tests/test_review_remote.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review-remote"


def _load():
    loader = importlib.machinery.SourceFileLoader("review_remote", str(SCRIPT))
    spec = importlib.util.spec_from_loader("review_remote", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@contextlib.contextmanager
def _env(**kw: str):
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _repo_with_staged_change(root: Path) -> Path:
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "a.txt").write_text("one staged\n")
    _git(root, "add", "a.txt")
    return root


def test_parse_args_splits_local_flags_from_forwarded_ones():
    rr = _load()
    req = rr.parse_args(
        [
            "diff",
            "--staged",
            "--task",
            "X-1",
            "-C",
            "/tmp/x",
            "-o",
            "out.md",
            "--detach",
            "--pool",
            "3",
        ]
    )
    assert req["mode"] == "diff"
    assert req["args"] == ["--staged", "--task", "X-1", "--pool", "3"]
    assert req["cwd"] == "/tmp/x"
    assert req["output"] == "out.md"
    assert req["detach"] is True


def test_parse_args_equals_and_glued_forms():
    rr = _load()
    req = rr.parse_args(["quorum", "Q?", "--cwd=/tmp/x", "--output=o.md"])
    assert (req["args"], req["cwd"], req["output"]) == (["Q?"], "/tmp/x", "o.md")
    req = rr.parse_args(["diff", "-C/tmp/y", "-oout.md", "--staged"])
    assert (req["args"], req["cwd"], req["output"]) == (
        ["--staged"],
        "/tmp/y",
        "out.md",
    )
    req = rr.parse_args(["diff", "-o=out.md", "-C=/tmp/z"])
    assert (req["cwd"], req["output"]) == ("/tmp/z", "out.md")


def test_parse_args_stops_at_double_dash_and_skips_option_values():
    rr = _load()
    req = rr.parse_args(
        ["just-ask", "--task", "T", "--", "-ollama vs vllm?", "--detach"]
    )
    assert req["args"] == ["--task", "T", "--", "-ollama vs vllm?", "--detach"]
    assert req["output"] is None and req["detach"] is False
    # the value of a value-taking option is never read as a flag
    req = rr.parse_args(["diff", "--prompt", "--detach", "--staged"])
    assert req["args"] == ["--prompt", "--detach", "--staged"]
    assert req["detach"] is False


def test_parse_args_expands_abbreviations_and_flags_unsupported_options():
    rr = _load()
    req = rr.parse_args(["diff", "--staged", "--comm", "--out=o.md", "--cw", "/tmp/q"])
    assert req["refused"] == ["--commit"]
    assert req["output"] == "o.md" and req["cwd"] == "/tmp/q"
    req = rr.parse_args(["diff", "--visual", "/tmp/shot.png", "--before=/tmp/b.png"])
    assert req["refused"] == ["--visual", "--before"]
    assert rr.parse_args(["diff", "--staged"])["refused"] == []


def test_payload_carries_staged_unstaged_untracked_and_matching_hashes():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        root = _repo_with_staged_change(Path(td))
        (root / "b.txt").write_text("two\n")
        _git(root, "add", "b.txt")
        _git(root, "commit", "-q", "-m", "b", "--only", "b.txt")
        (root / "b.txt").write_text("two unstaged\n")
        (root / "new.txt").write_text("untracked\n")
        os.mkfifo(root / "pipe")  # a FIFO must be skipped, never read (would hang)

        data, cached_h, work_h = rr.build_payload(str(root))

        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            names = tar.getnames()
            staged = tar.extractfile("staged.patch").read().decode()
            unstaged = tar.extractfile("unstaged.patch").read().decode()
        assert "untracked/new.txt" in names and "untracked/pipe" not in names
        assert "one staged" in staged and "two unstaged" not in staged
        assert "two unstaged" in unstaged and "one staged" not in unstaged
        assert (
            cached_h
            == hashlib.sha256(
                _git(root, "diff", "--no-ext-diff", "--cached").encode()
            ).hexdigest()
        )
        assert (
            work_h
            == hashlib.sha256(
                _git(root, "diff", "--no-ext-diff", "HEAD").encode()
            ).hexdigest()
        )


def test_job_dir_rejects_path_traversal():
    rr = _load()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            rr.job_dir("../../etc")
    except SystemExit:
        pass
    else:
        raise AssertionError("path traversal accepted")
    assert rr.job_dir("20261022T182746Z-33b75c").name == "20261022T182746Z-33b75c"


def test_rc_of_never_reports_success_for_a_job_without_a_result():
    rr = _load()
    assert rr._rc_of({"remote_rc": None}) == 1
    assert rr._rc_of({}) == 1
    assert rr._rc_of({"remote_rc": 0}) == 0
    assert rr._rc_of({"remote_rc": 130}) == 130


def _stub_remote(rr, outputs: dict[str, bytes]):
    """Replace remote_sh: answer by the first key that appears in the script text."""

    def fake(_host, script, *, check=True):
        for key, out in outputs.items():
            if key in script:
                return out
        return b""

    rr.remote_sh = fake


def test_import_installs_records_marker_and_stamp_only_when_they_match():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        root = (
            _repo_with_staged_change(Path(td) / "repo")
            if (Path(td) / "repo").mkdir() is None
            else None
        )
        head = _git(root, "rev-parse", "HEAD").strip()
        digest = hashlib.sha256(
            _git(root, "diff", "--no-ext-diff", "--cached").encode()
        ).hexdigest()
        stats = Path(td) / "stats.jsonl"
        marker = Path(td) / "marker"
        good = json.dumps({"duration_seconds": 1.0, "task_code": "T", "passed": True})
        _stub_remote(
            rr,
            {
                "stats.jsonl": (
                    good + "\nnot json\n" + '{"no_duration": 1}\n'
                ).encode(),
                "test -f": b"yes\n",
                "review-stamp-diff": f"{head}\nDIFF".encode(),
                "review-stamp)": f"{digest}\n".encode(),
            },
        )
        meta: dict = {}
        with (
            _env(REVIEW_STATS_FILE=str(stats), REVIEW_MARKER=str(marker)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rr.import_results("box", str(root), "20261022T182746Z-33b75c", meta)
        recs = [json.loads(line) for line in stats.read_text().splitlines()]
        assert (
            len(recs) == 1
            and recs[0]["remote_host"] == "box"
            and recs[0]["task_code"] == "T"
        )
        assert meta["marker"] is True and marker.is_file()
        stamp = Path(_git(root, "rev-parse", "--git-path", "review-stamp").strip())
        stamp = stamp if stamp.is_absolute() else root / stamp
        assert meta["stamp"] is True and stamp.read_text().strip() == digest

        # stale stamp (index changed during the review) and no marker written by the box
        stamp.unlink()
        marker.unlink()
        _stub_remote(
            rr,
            {
                "review-stamp)": b"0" * 64 + b"\n",
                "review-stamp-diff": f"{head}\nX".encode(),
            },
        )
        meta = {}
        with (
            _env(REVIEW_STATS_FILE=str(stats), REVIEW_MARKER=str(marker)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rr.import_results("box", str(root), "20261022T182746Z-33b75c", meta)
        assert meta["stamp"] is False and not stamp.exists()
        assert meta["marker"] is False and not marker.exists()
        assert meta["imported_records"] == 0


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
