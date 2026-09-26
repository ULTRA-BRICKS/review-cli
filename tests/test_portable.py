#!/usr/bin/env python3
"""Unit tests for reviewlib.portable — the POSIX/Windows shims.

The advisory lock must keep `fcntl.flock` semantics on BOTH platforms (every
cross-process critical section in jobs/seat_cooldown/specweb/backends relies on
them): exclusive between separate opens (even inside one process), a
non-blocking attempt raises BlockingIOError while held, a blocking attempt
waits for the release, and on Windows (where byte-range locks are mandatory)
the lock byte never interferes with ordinary reads/writes of the file. Runs on
Linux CI (flock path) and on Windows (msvcrt path) unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import portable  # noqa: E402

_CHILD_LOCK = (
    "import os, sys;"
    f"sys.path.insert(0, {str(REPO_ROOT)!r});"
    "from reviewlib import portable;"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600);"
    "blocking = sys.argv[2] == 'block';"
    "\ntry:\n"
    "    portable.lock_file(fd, blocking=blocking)\n"
    "except BlockingIOError:\n"
    "    print('busy'); sys.exit(0)\n"
    "print('got'); portable.unlock_file(fd); os.close(fd)\n"
)


def _child(lock_path: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_LOCK, str(lock_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_lock_is_exclusive_between_two_opens_in_one_process():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        fd1 = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fd2 = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            portable.lock_file(fd1)
            try:
                portable.lock_file(fd2, blocking=False)
                raise AssertionError("second open acquired a held lock")
            except BlockingIOError:
                pass
            portable.unlock_file(fd1)
            portable.lock_file(fd2, blocking=False)  # free now
            portable.unlock_file(fd2)
        finally:
            os.close(fd1)
            os.close(fd2)


def test_lock_is_exclusive_across_processes_and_blocking_waits():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.lock"
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            portable.lock_file(fd)
            out, err = _child(path, "try").communicate(timeout=60)
            assert out.strip() == "busy", (out, err)

            waiter = _child(path, "block")
            time.sleep(1.0)
            assert waiter.poll() is None, "blocking lock did not wait for the holder"
            portable.unlock_file(fd)
            out, err = waiter.communicate(timeout=60)
            assert waiter.returncode == 0 and out.strip() == "got", (out, err)
        finally:
            os.close(fd)


def test_lock_keeps_file_position_and_does_not_block_data_io():
    """Windows byte-range locks are MANDATORY for other handles' I/O — the lock byte
    must sit outside the data so a reader through another handle is never refused."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "data.json")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"hello")
            portable.lock_file(fd)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 5
            with open(path, "rb") as other:
                assert other.read() == b"hello"
            with open(path, "ab") as other:
                other.write(b" world")
            portable.unlock_file(fd)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 5
        finally:
            os.close(fd)
        with open(path, "rb") as fh:
            assert fh.read() == b"hello world"


def test_pid_alive():
    assert portable.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    assert not portable.pid_alive(proc.pid)


def test_current_uid():
    if hasattr(os, "getuid"):
        assert portable.current_uid() == os.getuid()
    else:
        assert portable.current_uid() == 0


def test_kill_process_tree_reaps_grandchildren_on_windows():
    if not portable.IS_WINDOWS:
        return  # POSIX uses os.killpg on the child's own session (see process._kill_tree)
    grandchild = "import time; time.sleep(120)"
    parent = (
        "import subprocess, sys, time;"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}]);"
        "print(p.pid, flush=True); time.sleep(120)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent],
        stdout=subprocess.PIPE,
        text=True,
        **portable.new_process_group_kwargs(),
    )
    try:
        gpid = int(proc.stdout.readline().strip())
        assert portable.pid_alive(gpid)
        portable.kill_process_tree(proc.pid)
        proc.wait(timeout=30)
        deadline = time.monotonic() + 10
        while portable.pid_alive(gpid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not portable.pid_alive(gpid), "grandchild survived the tree kill"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()



def test_ensure_utf8_stdio_prints_non_codepage_text_to_a_pipe():
    """Windows encodes a piped stdout with the ANSI codepage (cp1251/cp1252), which cannot
    encode an arrow; after ensure_utf8_stdio the child must print it as UTF-8, not crash."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from reviewlib import portable; portable.ensure_utf8_stdio();"
        "print(chr(0x2192) + chr(0x418))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (chr(0x2192) + chr(0x418)).encode("utf-8")


def test_run_decodes_utf8_subprocess_output():
    """`process._run` (the `git diff` path) must decode UTF-8 output — on Windows the ANSI
    codepage made byte 0x98 (Cyrillic "И" = D0 98) kill the reader thread and return None."""
    from reviewlib import process

    text = chr(0x418) + chr(0x2192)
    code = f"import sys; sys.stdout.buffer.write({text.encode('utf-8')!r})"
    proc = process._run([sys.executable, "-c", code], cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0 and proc.stdout == text

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
