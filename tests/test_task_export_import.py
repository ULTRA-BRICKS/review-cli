#!/usr/bin/env python3
"""`review task CODE --export` / `review task --import` / `--import-from HOST`: a review
iteration recorded on one machine counts for `review task CODE --check` on another one
(review-cli#659), without losing the repo/diff identity verification.

Plain test_* functions; the stats store is redirected with $REVIEW_STATS_FILE, ssh is stubbed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402

TASK = "GL-343"
REPO = "github.com/conloca/private"
ROLES = ["correctness", "consistency", "performance"]
MODELS = ["claude:claude-opus-5-5", "codex:gpt-6-astra", "codex:gpt-6-luna"]


@contextlib.contextmanager
def _store(path: Path):
    old = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = old


def _record_three_passed_iterations(code: str = TASK) -> None:
    for _ in range(3):
        assert _stats.record_run(
            task_code=code,
            mode="review",
            models=MODELS,
            roles=ROLES,
            duration_seconds=1.0,
            ok_count=3,
            fail_count=0,
            passed=True,
            repo_id=REPO,
            diff_files=["a.ts"],
        )


def _task(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = _cli._task_subcommand(args)
        except SystemExit as exc:  # argparse usage errors
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def test_export_prints_only_that_tasks_raw_records():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "a.jsonl"):
            _record_three_passed_iterations()
            _record_three_passed_iterations("OTHER-1")
            rc, out, _ = _task([TASK, "--export"])
        recs = _lines(out)
        assert rc == 0 and len(recs) == 3
        assert {r["task_code"] for r in recs} == {TASK}
        assert all("iteration" not in r and "duration_seconds" in r for r in recs)


def test_iterations_exported_from_one_store_satisfy_check_in_another():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _, exported, _ = _task([TASK, "--export"])
        dump = Path(td) / "dump.jsonl"
        dump.write_text(exported)
        with _store(Path(td) / "workstation.jsonl"):
            before = _stats.quorum_check(
                TASK, min_iter=3, repo_id=REPO, diff_files=["a.ts"]
            )
            assert before["passed"] is False
            rc, out, _ = _task(["--import", str(dump), "--from-host", "clvm"])
            assert rc == 0 and "imported 3" in out, out
            after = _stats.quorum_check(
                TASK, min_iter=3, repo_id=REPO, diff_files=["a.ts"]
            )
            assert after["passed"] is True and after["distinct_roles_passed"] == 3
            recs = _stats._load_records()
            assert {r["remote_host"] for r in recs} == {"clvm"}


def test_imported_iterations_keep_the_repo_identity_check():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _, exported, _ = _task([TASK, "--export"])
        dump = Path(td) / "dump.jsonl"
        dump.write_text(exported)
        with _store(Path(td) / "workstation.jsonl"):
            _task(["--import", str(dump)])
            wrong = _stats.quorum_check(
                TASK, min_iter=3, repo_id="github.com/someone/else", diff_files=["a.ts"]
            )
        assert wrong["passed"] is False, wrong


def test_import_is_idempotent_and_rejects_non_records():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _, exported, _ = _task([TASK, "--export"])
        dump = Path(td) / "dump.jsonl"
        dump.write_text(exported + "not json\n" + '{"no_duration": 1}\n[1]\n')
        with _store(Path(td) / "workstation.jsonl"):
            rc, out, _ = _task(["--import", str(dump)])
            assert rc == 0 and "imported 3" in out and "rejected 3" in out, out
            rc, out, _ = _task(["--import", str(dump)])
            assert rc == 0 and "imported 0" in out and "skipped 3" in out, out
            assert len(_stats._load_records()) == 3


def test_import_from_host_runs_export_over_ssh_and_imports_it():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _, exported, _ = _task([TASK, "--export"])
        calls = []

        def fake_ssh(host, remote_args):
            calls.append((host, remote_args))
            return 0, exported, ""

        old = _cli._ssh_review
        _cli._ssh_review = fake_ssh
        try:
            with _store(Path(td) / "workstation.jsonl"):
                rc, out, _ = _task([TASK, "--import-from", "ultra@65.21.40.51"])
                recs = _stats._load_records()
        finally:
            _cli._ssh_review = old
        assert rc == 0 and "imported 3" in out, out
        assert calls == [("ultra@65.21.40.51", ["task", TASK, "--export"])]
        assert {r["remote_host"] for r in recs} == {"ultra@65.21.40.51"}


def test_import_from_host_failure_is_an_error_not_a_silent_zero():
    with tempfile.TemporaryDirectory() as td:
        old = _cli._ssh_review
        _cli._ssh_review = lambda host, remote_args: (255, "", "ssh: no route to host")
        try:
            with _store(Path(td) / "ws.jsonl"):
                rc, out, err = _task([TASK, "--import-from", "ultra@nowhere"])
        finally:
            _cli._ssh_review = old
        assert rc != 0 and "no route to host" in err and "imported" not in out


def test_import_and_export_flag_misuse_is_a_usage_error():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "ws.jsonl"):
            assert _task(["--export"])[0] == 2  # needs a CODE
            assert _task([TASK, "--import-from", "h", "--import", "f"])[0] == 2
            assert _task(["--import-from", "h"])[0] == 2  # needs a CODE


def test_import_from_host_end_to_end_through_a_real_ssh_hop():
    """Real subprocesses: a fake `ssh` runs the remote command locally with the BOX's store, the
    real `review task CODE --export` answers, and the workstation store ends up satisfying --check."""
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "review").write_text(
            f'#!/bin/sh\nexec {sys.executable} {REPO_ROOT / "bin" / "review"} "$@"\n'
        )
        # ssh [-o opt]... host command  ->  run command locally against the box store
        (bindir / "ssh").write_text(
            '#!/bin/sh\nwhile [ "$1" = -o ]; do shift 2; done\nshift\n'
            # a real ssh does not forward the nesting guard to the box
            'unset REVIEW_CLI_ACTIVE; REVIEW_STATS_FILE="$BOX_STATS" exec bash -c "$1"\n'
        )
        for f in bindir.iterdir():
            f.chmod(0o755)
        with _store(tmp / "box.jsonl"):
            _record_three_passed_iterations()
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "BOX_STATS": str(tmp / "box.jsonl"),
            "REVIEW_STATS_FILE": str(tmp / "ws.jsonl"),
            "HOME": str(tmp),
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "review"),
                "task",
                TASK,
                "--import-from",
                "box",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "imported 3" in proc.stdout, proc.stdout
        with _store(tmp / "ws.jsonl"):
            ok = _stats.quorum_check(
                TASK, min_iter=3, repo_id=REPO, diff_files=["a.ts"]
            )
        assert ok["passed"] is True, ok


def test_check_combined_with_a_transfer_flag_never_passes_without_running_the_check():
    # `--check --import-from HOST` once imported and returned 0 without checking anything: a
    # ship gate reading that 0 as "quorum met" would merge with no review
    with tempfile.TemporaryDirectory() as td:
        dump = Path(td) / "dump.jsonl"
        dump.write_text("")
        for extra in (["--import", str(dump)], ["--import-from", "h"], ["--export"]):
            for flag in (["--check"], ["--json"], ["--detail", "1"]):
                with _store(Path(td) / "ws.jsonl"):
                    rc, _, err = _task([TASK, *flag, *extra])
                assert rc == 2 and "cannot be combined" in err, (flag, extra, rc, err)


def test_import_takes_no_task_code_instead_of_silently_ignoring_it():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _record_three_passed_iterations("OTHER-1")
            _, exported_a, _ = _task([TASK, "--export"])
            _, exported_b, _ = _task(["OTHER-1", "--export"])
        dump = Path(td) / "dump.jsonl"
        dump.write_text(exported_a + exported_b)
        with _store(Path(td) / "ws.jsonl"):
            rc, _, err = _task([TASK, "--import", str(dump)])
            assert rc == 2 and "takes no task CODE" in err, err
            assert _stats._load_records() == []


def test_ssh_hop_does_not_read_the_callers_stdin():
    # `while read h; do review task X --import-from "$h"; done < hosts` lost every host after the
    # first because ssh slurped the rest of the loop's stdin
    import subprocess

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    old = _cli.subprocess.run
    _cli.subprocess.run = fake_run
    try:
        _cli._ssh_review("box", ["task", TASK, "--export"])
    finally:
        _cli.subprocess.run = old
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL or "-n" in seen["cmd"]


def test_concurrent_imports_of_the_same_records_never_double_count():
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dump = tmp / "dump.jsonl"
        dump.write_text(
            "".join(
                json.dumps({"duration_seconds": 1.0, "task_code": TASK, "n": i}) + "\n"
                for i in range(400)
            )
        )
        go = tmp / "go"
        code = (
            "import sys, time, pathlib\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from reviewlib import stats\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.001)\n"
            "stats.import_records(open(sys.argv[1]).read().splitlines())\n"
        )
        env = {**os.environ, "REVIEW_STATS_FILE": str(tmp / "ws.jsonl")}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(dump), str(go)],
                env=env,
                stderr=subprocess.PIPE,
            )
            for _ in range(6)
        ]
        go.write_text("go")
        errs = [p.communicate(timeout=120)[1].decode() for p in procs]
        assert all(p.returncode == 0 for p in procs), errs
        lines = (tmp / "ws.jsonl").read_text().splitlines()
        assert len(lines) == 400, len(lines)


def test_import_from_host_keeps_only_the_requested_tasks_records():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "box.jsonl"):
            _record_three_passed_iterations()
            _record_three_passed_iterations("OTHER-1")
            _, a, _ = _task([TASK, "--export"])
            _, b, _ = _task(["OTHER-1", "--export"])
        old = _cli._ssh_review
        _cli._ssh_review = lambda host, remote_args: (0, a + b, "")
        try:
            with _store(Path(td) / "ws.jsonl"):
                rc, out, _ = _task([TASK, "--import-from", "box"])
                codes = {r["task_code"] for r in _stats._load_records()}
        finally:
            _cli._ssh_review = old
        assert rc == 0 and "imported 3" in out and "rejected 3" in out, out
        assert codes == {TASK}


def test_an_unreadable_store_fails_the_import_instead_of_disabling_dedup():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "ws.jsonl"
        good = json.dumps({"duration_seconds": 1.0, "task_code": TASK})
        store.write_bytes(good.encode() + b"\n\xff\xfe not utf-8\n")
        with _store(store):
            try:
                _stats.import_records([good])
            except OSError:
                pass
            else:
                raise AssertionError("imported into a store it could not read")
        assert store.read_bytes().count(b"duration_seconds") == 1


def test_a_short_os_write_still_lands_every_record():
    with tempfile.TemporaryDirectory() as td:
        real_write = os.write

        def short_write(fd, data):
            return real_write(
                fd, data[:7]
            )  # a full disk / quota may accept fewer bytes

        lines = [
            json.dumps({"duration_seconds": 1.0, "task_code": TASK, "n": i})
            for i in range(5)
        ]
        with _store(Path(td) / "ws.jsonl"):
            os.write = short_write
            try:
                result = _stats.import_records(lines)
            finally:
                os.write = real_write
            assert result["imported"] == 5
            assert len(_stats._load_records()) == 5


def test_ssh_output_with_invalid_utf8_is_not_a_crash():
    import subprocess

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    old = _cli.subprocess.run
    _cli.subprocess.run = fake_run
    try:
        _cli._ssh_review("box", ["task", TASK, "--export"])
    finally:
        _cli.subprocess.run = old
    assert seen.get("errors") == "replace"


def test_import_into_a_store_that_ends_mid_line_keeps_every_record_parseable():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "ws.jsonl"
        good = json.dumps({"duration_seconds": 1.0, "task_code": TASK, "n": 0})
        store.write_text(
            good + "\n" + '{"duration_seconds": 1.0, "task_co'
        )  # a torn write
        new = json.dumps({"duration_seconds": 2.0, "task_code": TASK, "n": 1})
        with _store(store):
            assert _stats.import_records([new])["imported"] == 1
            assert [r["n"] for r in _stats._load_records()] == [0, 1]


def test_export_from_an_unreadable_store_is_an_error_not_an_empty_success():
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "box.jsonl"
        store.write_bytes(b"\xff\xfe not utf-8\n")
        with _store(store):
            rc, out, err = _task([TASK, "--export"])
        assert rc == 1 and out == "" and "not valid UTF-8" in err, (rc, out, err)


def test_empty_import_from_host_and_stray_from_host_are_usage_errors():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "ws.jsonl"):
            assert _task([TASK, "--import-from", ""])[0] == 2
            assert _task([TASK, "--from-host", "box"])[0] == 2
            assert _task([TASK, "--export", "--from-host", "box"])[0] == 2


def test_import_from_stdin_with_invalid_utf8_is_a_clean_error():
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "REVIEW_STATS_FILE": str(Path(td) / "ws.jsonl")}
        env.pop("REVIEW_CLI_ACTIVE", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "review"),
                "task",
                "--import",
                "-",
            ],
            input=b'\xff{"duration_seconds": 1}\n',
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 1, proc.returncode
        assert b"Traceback" not in proc.stderr and b"cannot read" in proc.stderr, (
            proc.stderr
        )


def test_the_import_with_a_code_hint_names_a_command_that_parses():
    with tempfile.TemporaryDirectory() as td:
        with _store(Path(td) / "ws.jsonl"):
            _, _, err = _task([TASK, "--import", str(Path(td) / "f")])
        assert "review task CODE --import-from HOST" in err, err


def test_a_record_with_an_unhashable_task_code_is_rejected_not_a_crash():
    with tempfile.TemporaryDirectory() as td:
        lines = [
            json.dumps({"duration_seconds": 1.0, "task_code": []}),
            json.dumps({"duration_seconds": 1.0, "task_code": {}}),
            json.dumps({"duration_seconds": 1.0, "task_code": TASK}),
        ]
        with _store(Path(td) / "ws.jsonl"):
            result = _stats.import_records(lines, task_code=TASK)
            assert result == {"imported": 1, "skipped": 0, "rejected": 2}, result
            (Path(td) / "box.jsonl").write_text("\n".join(lines) + "\n")
        with _store(Path(td) / "box.jsonl"):
            assert [r["task_code"] for r in _stats.export_records(TASK)] == [TASK]


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
