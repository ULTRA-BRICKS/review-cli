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

        # the box's review wrote no stamp (not a passing staged run): nothing is installed,
        # so the commit gate keeps blocking an unreviewed diff
        _stub_remote(rr, {})
        meta = {}
        with (
            _env(REVIEW_STATS_FILE=str(stats), REVIEW_MARKER=str(marker)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rr.import_results("box", str(root), "20261022T182746Z-33b75c", meta)
        assert meta["stamp"] is False and not stamp.exists()


def test_importing_the_same_job_twice_never_double_counts_an_iteration():
    # a retried import (or `review task --import` of the same box) must not turn one review
    # into two iterations of the quorum
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        root = (
            _repo_with_staged_change(Path(td) / "repo")
            if (Path(td) / "repo").mkdir() is None
            else None
        )
        stats = Path(td) / "stats.jsonl"
        good = json.dumps({"duration_seconds": 1.0, "task_code": "T", "passed": True})
        _stub_remote(rr, {"stats.jsonl": (good + "\n").encode()})
        for _ in range(2):
            meta: dict = {}
            with (
                _env(REVIEW_STATS_FILE=str(stats), REVIEW_MARKER=str(Path(td) / "m")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rr.import_results("box", str(root), "20261022T182746Z-33b75c", meta)
        assert len(stats.read_text().splitlines()) == 1
        assert meta["imported_records"] == 0


def test_an_unwritable_run_stats_store_is_recorded_on_the_job_and_fails_wait_but_keeps_the_stamp():
    # the box's review finished: the stamp still installs, but the records that would make it
    # count for the quorum did not land, and the caller must be told with a non-zero exit
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
        blocker = Path(td) / "not-a-dir"
        blocker.write_text("x")
        good = json.dumps({"duration_seconds": 1.0, "task_code": "T", "passed": True})
        _stub_remote(
            rr,
            {
                "stats.jsonl": (good + "\n").encode(),
                "review-stamp-diff": f"{head}\nDIFF".encode(),
                "review-stamp)": f"{digest}\n".encode(),
            },
        )
        meta: dict = {}
        err = io.StringIO()
        with (
            _env(
                REVIEW_STATS_FILE=str(blocker / "stats.jsonl"),
                REVIEW_MARKER=str(Path(td) / "m"),
            ),
            contextlib.redirect_stderr(err),
        ):
            rr.import_results("box", str(root), "20261022T182746Z-33b75c", meta)
        stamp = Path(_git(root, "rev-parse", "--git-path", "review-stamp").strip())
        stamp = stamp if stamp.is_absolute() else root / stamp
        assert meta["stamp"] is True and stamp.exists()
        # the box's real exit code survives; the failed import is its own field and fails wait
        meta["remote_rc"] = 0
        assert "import_error" in meta and rr._rc_of(meta) == 1
        assert "NOT imported" in err.getvalue() and "imported 0" not in err.getvalue()


def _main_exit(rr, argv: list[str]) -> tuple[int, str]:
    """Run rr.main() with argv, returning (exit code, stderr); any ssh/git work is a failure."""

    def boom(*_a, **_k):
        raise AssertionError(
            "review-remote touched the box for an invalid command line"
        )

    rr.run = boom
    rr.remote_sh = boom
    saved = sys.argv
    sys.argv = ["review-remote", *argv]
    err = io.StringIO()
    try:
        with _env(REVIEW_REMOTE="box"), contextlib.redirect_stderr(err):
            try:
                code = rr.main()
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.argv = saved
    return code, err.getvalue()


def test_job_subcommands_without_a_job_id_are_usage_errors_not_runs():
    # `review-remote wait` (no id) used to fall through to the mode check; it must say what
    # is missing and never dispatch a run.
    rr = _load()
    for sub in ("wait", "status"):
        code, err = _main_exit(rr, [sub])
        assert code == 2, (sub, code, err)
        assert "job id" in err and sub in err, err
    code, err = _main_exit(rr, ["wait", "20261022T182746Z-33b75c", "extra"])
    assert code == 2 and "job id" in err, err
    code, err = _main_exit(rr, ["nonsense"])
    assert code == 2 and "unsupported mode" in err, err


def test_foreground_run_records_its_pid_so_a_killed_job_does_not_wait_forever():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        jobdir = Path(td)
        seen = {}

        def fake_run(_host, _req, _job, jd, _meta):
            seen["pid"] = (jd / "pid").read_text() if (jd / "pid").exists() else None
            return 0

        rr._run = fake_run
        rr.run("box", {"mode": "diff", "cwd": td, "args": []}, "j", jobdir)
        assert seen["pid"] == str(os.getpid())


def _job_in(rr, td: str, job: str = "20261022T182746Z-33b75c") -> Path:
    rr.STATE_DIR = Path(td)
    d = Path(td) / "jobs" / job
    d.mkdir(parents=True)
    return d


def test_empty_pid_file_is_unknown_not_dead():
    # a child rewriting `pid` empties it for an instant; a poller must not call that a death
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        d = _job_in(rr, td)
        (d / "pid").write_text("")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rr.cmd_status(d.name)
        assert rc == 0 and "running" in out.getvalue() and "DIED" not in out.getvalue()


def test_dead_job_with_pid_file_and_no_result_ends_wait_with_failure():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        d = _job_in(rr, td)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (d / "pid").write_text(str(dead.pid))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rr.cmd_wait(d.name)
            except SystemExit as exc:
                assert exc.code == 1 and "died" in err.getvalue()
            else:
                raise AssertionError("wait returned for a job that never finished")


def test_wait_on_a_finished_foreground_job_prints_its_review_output():
    # foreground jobs keep their output in review.out (only detached ones have out.log)
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        d = _job_in(rr, td)
        (d / "review.out").write_text("the findings\n")
        (d / "meta.json").write_text(json.dumps({"remote_rc": 0}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rr.cmd_wait(d.name)
        assert rc == 0 and "the findings" in out.getvalue()


def test_a_failing_pid_write_still_leaves_a_result_for_wait():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        jobdir = Path(td)

        def no_pid(*_a, **_k):
            raise OSError("disk full")

        rr._write_pid = no_pid
        try:
            rr.run("box", {"mode": "diff", "cwd": td, "args": []}, "j", jobdir)
        except OSError:
            pass
        else:
            raise AssertionError("the OSError was swallowed")
        meta = json.loads((jobdir / "meta.json").read_text())
        assert meta["remote_rc"] == 1 and "disk full" in meta["error"]


def test_concurrent_pid_writers_never_collide_on_a_shared_temp_file():
    # the detached launcher and its child both record the pid; two os.replace calls over one
    # fixed temp path made the loser die with FileNotFoundError
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        code = (
            "import importlib.machinery as m, importlib.util as u, sys, pathlib\n"
            f"l = m.SourceFileLoader('rr', {str(SCRIPT)!r}); mod = u.module_from_spec(u.spec_from_loader('rr', l)); l.exec_module(mod)\n"
            "d = pathlib.Path(sys.argv[1]); base = int(sys.argv[2])\n"
            "for i in range(300):\n"
            "    mod._write_pid(d, base + i)\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(d), str(1000 * (n + 1))],
                stderr=subprocess.PIPE,
            )
            for n in range(6)
        ]
        errs = [p.communicate()[1].decode() for p in procs]
        assert all(p.returncode == 0 for p in procs), errs
        assert rr._read_pid(d) is not None
        assert not list(d.glob("pid.*tmp")) and not (d / "pid.tmp").exists()


def test_status_and_wait_name_the_log_file_the_job_really_has():
    rr = _load()
    with tempfile.TemporaryDirectory() as td:
        d = _job_in(rr, td)
        (d / "review.out").write_text("partial\n")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (d / "pid").write_text(str(dead.pid))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert rr.cmd_status(d.name) == 1
        assert "review.out" in out.getvalue() and "out.log" not in out.getvalue()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rr.cmd_wait(d.name)
            except SystemExit:
                pass
        assert "review.out" in err.getvalue() and "out.log" not in err.getvalue()


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
