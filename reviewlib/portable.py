"""POSIX/Windows portability shims shared across reviewlib.

review-cli grew up on macOS/Linux; a handful of POSIX-only primitives (``fcntl.flock``,
process groups via ``os.killpg``/``os.getpgid``, ``os.getuid``, ``O_NOFOLLOW``,
``os.kill(pid, 0)`` as an existence probe) made the package unimportable -- or quietly
wrong -- on Windows. Everything platform-specific goes through this one module so the
call sites stay single-path and the POSIX behaviour is byte-for-byte what it was.

Advisory file lock
------------------
``lock_file(fd)`` / ``unlock_file(fd)`` take and release an EXCLUSIVE lock on an open
file descriptor, with ``fcntl.flock`` semantics:

* POSIX: exactly ``fcntl.flock(fd, LOCK_EX[|LOCK_NB])`` / ``flock(fd, LOCK_UN)`` -- the
  lock belongs to the open file description, so two separate ``open()``s of the same file
  contend even inside one process.
* Windows: ``msvcrt.locking`` on ONE byte at ``_WIN_LOCK_OFFSET`` (far past any real
  content -- Windows byte-range locks are mandatory for I/O through OTHER handles, so the
  lock byte must never overlap data anyone reads or writes; the SQLite "pending byte"
  trick). Windows locks are likewise per-handle, so the contention semantics match
  ``flock``. ``msvcrt``'s own blocking mode gives up after ~10 s; ``blocking=True`` here
  instead retries a non-blocking attempt until it succeeds, i.e. waits indefinitely like
  ``flock(LOCK_EX)``. The fd's file position is saved and restored around the call.

``blocking=False`` raises ``BlockingIOError`` when another holder has the lock (what
``flock(LOCK_EX | LOCK_NB)`` raises), on both platforms. Any other failure is an
``OSError`` -- every caller already degrades on ``OSError`` (or deliberately lets it
propagate). Only exclusive locks are offered: no caller needs a shared lock.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None  # type: ignore[assignment]

# 1 GiB: fits the CRT's 32-bit lock offset, and no lock/sidecar file ever grows this big.
_WIN_LOCK_OFFSET = 0x40000000
# Poll interval while a blocking Windows lock waits for its holder (flock just sleeps in
# the kernel; msvcrt has no unbounded blocking mode).
_WIN_LOCK_POLL_SECONDS = 0.02


def _win_locking(fd: int, mode: int) -> None:
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        _msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def lock_file(fd: int, *, blocking: bool = True) -> None:
    """Take an exclusive advisory lock on open file descriptor ``fd`` (see module doc)."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX if blocking else _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return
    if _msvcrt is None:  # pragma: no cover - neither POSIX nor Windows
        raise OSError(errno.ENOSYS, "no advisory file locking on this platform")
    while True:
        try:
            _win_locking(fd, _msvcrt.LK_NBLCK)
            return
        except OSError as exc:
            # EACCES = held by another handle (EDEADLK is what LK_LOCK reports after its
            # own retries; accept either). Anything else is a real error.
            if exc.errno not in (errno.EACCES, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "file lock is held elsewhere") from exc
        time.sleep(_WIN_LOCK_POLL_SECONDS)


def unlock_file(fd: int) -> None:
    """Release the lock ``lock_file`` took on ``fd``."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is None:  # pragma: no cover
        raise OSError(errno.ENOSYS, "no advisory file locking on this platform")
    _win_locking(fd, _msvcrt.LK_UNLCK)


# ── ownership / open flags ──────────────────────────────────────────────────────────────

# `O_NOFOLLOW`/`O_NONBLOCK` do not exist on Windows. 0 there: symlinks need admin or
# Developer Mode to create, the FIFO-hang they defend against has no Windows analogue,
# and every caller also runs an explicit resolve/S_ISREG check.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def current_uid() -> int:
    """``os.getuid()`` on POSIX. On Windows there are no numeric uids -- ``os.stat``
    reports ``st_uid == 0`` for every file and the temp dir is already per-user -- so 0
    keeps the ``st_uid == current_uid()`` ownership checks meaningful-as-possible and the
    uid-keyed temp paths stable."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


# ── processes ───────────────────────────────────────────────────────────────────────────


def new_process_group_kwargs() -> dict:
    """Extra ``Popen`` kwargs that isolate a child the way ``start_new_session=True`` does
    on POSIX: on Windows, ``CREATE_NEW_PROCESS_GROUP`` (the console's Ctrl-C no longer
    hits the child directly; the caller reaps it via ``kill_process_tree``). POSIX callers
    already pass ``start_new_session=True`` themselves, so this is empty there."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


# SIGKILL has no Windows definition; the Windows paths below never send signals anyway.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def kill_process_tree(pid: int, *, timeout: float = 10.0) -> None:
    """Windows-only forced kill of ``pid`` AND its descendants (``taskkill /T /F``), the
    stand-in for ``os.killpg(pgid, SIGKILL)``. Best-effort, never raises. Windows tracks
    descendants by parent pid, so this must run while the direct child is still alive to
    reach grandchildren -- callers then ``proc.kill()`` the direct child regardless."""
    systemroot = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    taskkill = os.path.join(systemroot, "System32", "taskkill.exe")
    if not os.path.exists(taskkill):
        taskkill = "taskkill"
    try:
        subprocess.run(
            [taskkill, "/T", "/F", "/PID", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 - best-effort reaping must never raise
        pass


def pid_alive(pid: int) -> bool:
    """True iff ``pid`` names a live process. POSIX: the ``kill(pid, 0)`` probe (a pid we
    may not signal still counts as alive). Windows: ``os.kill(pid, 0)`` would TERMINATE
    the process there, so query it via ``OpenProcess`` + ``GetExitCodeProcess``."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Exists but not ours to query (another user's / protected process) -> alive,
        # mirroring the POSIX PermissionError branch; anything else -> no such process.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# ── text encoding ───────────────────────────────────────────────────────────────────────


def _is_utf8(encoding: str | None) -> bool:
    return (encoding or "").lower().replace("-", "").replace("_", "") in {"utf8", "utf8sig"}


def ensure_utf8_stdio() -> None:
    """Re-encode stdout/stderr (and, on Windows, stdin) as UTF-8 with ``errors="replace"``.

    Python encodes a non-console stream with the OS ANSI codepage -- on Windows ``cp1251``/
    ``cp1252`` for a pipe, a redirect, or a mintty/Cygwin/Git Bash terminal -- which cannot
    encode the ``→``/``✓``/CJK a model's review routinely contains, so printing the panel died
    with ``UnicodeEncodeError``; a piped UTF-8 diff read through ``cp1251`` stdin was mangled
    (byte 0x98, e.g. Cyrillic "И", does not even decode). Same fix rig-cli applies
    (``riglib.stdio``). Idempotent; streams already in UTF-8 or that cannot be reconfigured
    (test doubles) are left alone."""
    streams = [sys.stdout, sys.stderr] + ([sys.stdin] if IS_WINDOWS else [])
    for stream in streams:
        if stream is None or _is_utf8(getattr(stream, "encoding", None)):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


# `subprocess.run(text=True)` decodes with the ANSI codepage on Windows, where a UTF-8
# `git diff` containing an undecodable byte made the reader thread die and `stdout` come
# back None (a silently EMPTY diff). Git emits UTF-8; decode it as such there. POSIX keeps
# the locale default (UTF-8 in practice).
def fchmod(fd: int, mode: int) -> None:
    """``os.fchmod`` where the OS has it. Windows before Python 3.13 has none; there the mode
    passed to ``os.open`` at creation is what applies."""
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


TEXT_SUBPROCESS_KWARGS: dict = {"encoding": "utf-8", "errors": "replace"} if IS_WINDOWS else {}
