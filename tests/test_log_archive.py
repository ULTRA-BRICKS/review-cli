"""SQLite call-log archive (review-cli#480): store + chunk-seal + load round-trip,
durability across process exit, the process.py write paths, the dashboard / `review stat`
read paths (including the `days` window), and the migration's four proofs.

Script-style like the other unit files: `python tests/test_log_archive.py`."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import log_archive as la  # noqa: E402
from reviewlib import log_archive_migrate as mig  # noqa: E402
from reviewlib import process  # noqa: E402
from reviewlib.dashboard import parser as dparser  # noqa: E402
from reviewlib.dashboard import tokenstats  # noqa: E402

BASE = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


@contextmanager
def _env(**env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _chunk_target(n: int):
    old = la._CHUNK_TARGET_CALLS
    la._CHUNK_TARGET_CALLS = n
    try:
        yield
    finally:
        la._CHUNK_TARGET_CALLS = old


def _name(dt: datetime, backend: str = "codex", rnd: int = 0) -> str:
    return f"{dt.strftime('%Y%m%dT%H%M%S_%f')}Z-{backend}-r{rnd}.log"


def _content(
    backend: str = "codex", body: str = "## verdict\nLooks good.\n", exit_code: int = 0
) -> str:
    return f"[review-cli] {backend}: {backend} (args redacted)\n{body}[review-cli] EXIT {exit_code}\n"


def _store(ld: Path, dt: datetime, **kw) -> str:
    backend = kw.pop("backend", "codex")
    name = _name(dt, backend)
    la.store_call(
        ld,
        name,
        _content(backend, **kw),
        mtime=(dt + timedelta(seconds=30)).timestamp(),
    )
    return name


def _write_file(ld: Path, dt: datetime, backend: str = "codex", **kw) -> Path:
    p = ld / _name(dt, backend)
    p.write_text(_content(backend, **kw), encoding="utf-8")
    ts = (dt + timedelta(seconds=30)).timestamp()
    os.utime(p, (ts, ts))
    return p


def _session_calls(ld: Path, **kw) -> list[str]:
    return sorted(c.filename for s in dparser.load_sessions(ld, **kw) for c in s.calls)


def test_roundtrip_store_seal_load():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        name = _store(ld, BASE)
        pending = la.load_calls(ld)
        assert [c.filename for c in pending] == [name]
        assert pending[0].exit_code == 0 and pending[0].backend == "codex"
        la.seal_all(ld)
        with closing(la.connect(ld)) as conn:
            counts = la.archive_counts(conn)
        assert counts["chunks"] == 1 and counts["chunk_calls"] == 1, counts
        assert counts["pending_calls"] == 0, counts
        sealed = la.load_calls(ld)
        assert [c.filename for c in sealed] == [name]
        assert sealed[0].body == pending[0].body
        assert sealed[0].mtime == pending[0].mtime


def test_store_is_durable_across_process_exit():
    """The pre-fix archive buffered calls in a process-local dict that was never flushed,
    so every call of a short-lived `review` process was lost on exit."""
    with tempfile.TemporaryDirectory() as d:
        code = (
            "import sys; from pathlib import Path; from reviewlib import log_archive as la; "
            f"la.store_call(Path(sys.argv[1]), {_name(BASE)!r}, {_content()!r}, mtime=1.0)"
        )
        subprocess.run(
            [sys.executable, "-c", code, d],
            check=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        assert [c.filename for c in la.load_calls(Path(d))] == [_name(BASE)]


def test_groups_seal_at_target_and_on_day_rollover():
    with tempfile.TemporaryDirectory() as d, _chunk_target(2):
        ld = Path(d)
        for i in range(3):
            _store(ld, BASE + timedelta(minutes=i))
        with closing(la.connect(ld)) as conn:
            c = la.archive_counts(conn)
        assert (c["chunks"], c["chunk_calls"], c["pending_calls"]) == (1, 2, 1), c
        _store(ld, BASE + timedelta(days=1))  # next day: day 1's open group is sealed
        with closing(la.connect(ld)) as conn:
            c = la.archive_counts(conn)
        assert (c["chunks"], c["chunk_calls"], c["pending_calls"]) == (2, 3, 1), c
        assert len(la.load_calls(ld)) == 4


def test_tiny_failed_call_keeps_its_content():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _store(ld, BASE, body="[stderr] 429 Too Many Requests\n", exit_code=1)
        la.seal_all(ld)
        (call,) = la.load_calls(ld)
        assert call.exit_code == 1
        assert "429 Too Many Requests" in "\n".join(call.stderr_lines)


def test_store_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        name = _name(BASE)
        assert la.store_call(ld, name, _content(), mtime=1.0) is True
        assert la.store_call(ld, name, _content(), mtime=1.0) is False
        assert len(la.load_calls(ld)) == 1
        with closing(la.connect(ld)) as conn:
            assert la.archive_counts(conn)["pending_calls"] == 1


def test_load_sessions_reads_archive_and_honors_days_window():
    """Archived calls obey `days` exactly like files: same cutoff, anchored on the newest
    stamp across BOTH sources (clamped to now)."""
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        old_archived = _store(ld, BASE - timedelta(days=10))
        recent_archived = _store(ld, BASE, backend="gemini")
        old_file = _write_file(ld, BASE - timedelta(days=9), backend="opencode").name
        recent_file = _write_file(ld, BASE - timedelta(hours=1), backend="omp").name

        assert _session_calls(ld, days=7) == sorted([recent_archived, recent_file])
        assert _session_calls(ld, days=0) == sorted(
            [old_archived, recent_archived, old_file, recent_file]
        )
        # The archive's newest stamp anchors the window even though it is newer than every
        # file: a 30-minute window keeps only the newest archived call, not the file an hour
        # older (a file-only anchor would have kept that file and dropped the archive row).
        assert _session_calls(ld, days=30 / 1440) == [recent_archived]


def test_archive_window_skips_old_chunks_without_decompressing():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _store(ld, BASE - timedelta(days=10))
        _store(ld, BASE)
        la.seal_all(ld)
        calls_seen = []
        real = la.decompress_payload

        def spy(data):
            calls_seen.append(1)
            return real(data)

        la._PARSE_MEMO.clear()
        la.decompress_payload = spy
        try:
            got = la.load_calls(
                ld, since=(BASE - timedelta(days=7)).strftime("%Y%m%dT%H%M%S")
            )
        finally:
            la.decompress_payload = real
        assert [c.filename for c in got] == [_name(BASE)]
        assert len(calls_seen) == 1, (
            "the out-of-window day's chunk must not be decompressed"
        )


def test_parse_memo_is_bounded_to_the_latest_windowed_load():
    """The memo keeps the dashboard window's rows across an unbounded (`days=0`) load,
    and never grows to hold out-of-window history."""
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _store(ld, BASE - timedelta(days=10))
        _store(ld, BASE)
        la.seal_all(ld)
        recent = (BASE - timedelta(days=7)).strftime("%Y%m%dT%H%M%S")
        la.load_calls(ld, since=recent)
        la.load_calls(ld)  # unbounded: reuses, doesn't store
        decoded = []
        real = la.decompress_payload
        la.decompress_payload = lambda data: decoded.append(1) or real(data)
        try:
            assert len(la.load_calls(ld, since=recent)) == 1
            assert decoded == [], "the window's memoised chunk was decompressed again"
            assert len(la.load_calls(ld)) == 2
            assert len(decoded) == 1, "only the out-of-window chunk is decoded"
        finally:
            la.decompress_payload = real
        memo = la._PARSE_MEMO[str(la.archive_db_path(ld))]
        assert [k[0] for k in memo] == ["chunk"] and len(memo) == 1, memo.keys()


def test_undecodable_chunk_is_skipped_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _store(ld, BASE - timedelta(days=1))
        good = _store(ld, BASE, backend="gemini")
        la.seal_all(ld)
        with closing(la.connect(ld)) as conn:
            conn.execute(
                "UPDATE chunks SET payload = ? WHERE backend = 'codex'", (b"garbage",)
            )
        la._PARSE_MEMO.clear()
        assert [c.filename for c in la.load_calls(ld)] == [good]
        assert _session_calls(ld) == [good]


def test_a_non_call_name_is_never_archived_or_deleted():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_LOG_DIR=d, REVIEW_LOG_SQLITE="1"),
    ):
        ld = Path(d)
        try:
            la.store_call(ld, "notes.log", "x", mtime=1.0)
        except ValueError:
            pass
        else:
            raise AssertionError("store_call accepted a non-call-log name")
        stray = ld / "notes.log"
        stray.write_text("keep me\n", encoding="utf-8")
        process._archive_finished_log(stray)
        assert stray.read_text(encoding="utf-8") == "keep me\n"


def test_sidecar_name_collision_keeps_the_later_call():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_LOG_DIR=d, REVIEW_LOG_SQLITE="1"),
    ):
        ld = Path(d)
        kw = dict(round_no=1, argv0="z.ai:glm", returncode=0, started=BASE)
        first = process.write_sidecar_log("z.ai", stdout="first", **kw)
        second = process.write_sidecar_log("z.ai", stdout="second", **kw)
        assert first == second and second.exists()
        (call,) = [c for s in dparser.load_sessions(ld) for c in s.calls]
        assert "second" in call.body


def test_load_sessions_dedupes_a_name_on_disk_and_in_archive():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        p = _write_file(ld, BASE)
        la.store_call(
            ld, p.name, p.read_text(encoding="utf-8"), mtime=p.stat().st_mtime
        )
        assert _session_calls(ld) == [p.name]
        assert _session_calls(ld, days=7) == [p.name]


def test_signature_moves_on_archive_only_write():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_file(ld, BASE)
        before = dparser._dir_signature(ld)
        _store(ld, BASE + timedelta(minutes=5), backend="gemini")
        assert dparser._dir_signature(ld) != before


def test_run_streamed_archives_the_finished_log_and_unlinks_it():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_LOG_DIR=d, REVIEW_LOG_SQLITE="1"),
    ):
        ld = Path(d)
        result = process._run_streamed(
            [sys.executable, "-c", "print('archived-body-line')"],
            cwd=REPO_ROOT,
            timeout=60,
            backend="codex",
        )
        assert result.returncode == 0
        assert not list(ld.glob("*.log")), "the finished call's file must be gone"
        (call,) = la.load_calls(ld)
        assert call.backend == "codex" and call.exit_code == 0
        assert "archived-body-line" in call.body
        sessions = dparser.load_sessions(ld, days=7)
        assert [c.filename for s in sessions for c in s.calls] == [call.filename]


def test_run_streamed_keeps_the_file_when_archival_is_off():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_LOG_DIR=d, REVIEW_LOG_SQLITE="0"),
    ):
        ld = Path(d)
        process._run_streamed(
            [sys.executable, "-c", "print('x')"],
            cwd=REPO_ROOT,
            timeout=60,
            backend="codex",
        )
        assert len(list(ld.glob("*.log"))) == 1
        assert not la.archive_db_path(ld).exists()


def test_sidecar_log_goes_straight_to_the_archive():
    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_LOG_DIR=d, REVIEW_LOG_SQLITE="1"),
    ):
        ld = Path(d)
        path = process.write_sidecar_log(
            "z.ai",
            round_no=1,
            argv0="z.ai:glm",
            returncode=0,
            stdout="ok\nprompt_tokens=10 output_tokens=5",
            started=BASE,
        )
        assert not path.exists()
        (call,) = la.load_calls(ld)
        assert call.filename == path.name
        assert tokenstats.extract_usage_tokens(call) == (10, 5)


def test_review_stat_lists_archived_calls_with_since():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _store(ld, BASE - timedelta(days=10))
        recent = _store(ld, BASE)
        on_disk = _write_file(ld, BASE - timedelta(minutes=5), backend="omp").name
        got = [
            c.filename
            for c in tokenstats.list_call_logs(ld, since=BASE - timedelta(days=1))
        ]
        assert got == [on_disk, recent]
        assert len(tokenstats.list_call_logs(ld)) == 3


def test_migration_proofs_pass_and_only_archived_calls_are_deleted():
    with tempfile.TemporaryDirectory() as d, _chunk_target(2):
        ld = Path(d)
        for i in range(3):
            _write_file(ld, BASE + timedelta(minutes=i))
        _write_file(
            ld,
            BASE + timedelta(days=1),
            backend="z.ai",
            body="ok\nprompt_tokens=7 output_tokens=3\n",
        )
        brainstorm = ld / f"{BASE.strftime('%Y%m%dT%H%M%S_%f')}Z-brainstorm.md"
        brainstorm.write_text(
            "# Brainstorm: auth\npanel=codex moderator=codex rounds>=1 max=3\n",
            encoding="utf-8",
        )
        junk = ld / "faketest-junk.log"
        junk.write_text("x", encoding="utf-8")
        retry = ld / "20260610T120000_000000Z-retry-codex.log"
        retry.write_text("retry event\n", encoding="utf-8")
        before = _session_calls(ld)

        with closing(la.connect(ld)) as conn:
            mig.migrate_directory(ld, conn=conn)
            proofs = mig.run_proofs(ld, conn)
        assert proofs["ok"], proofs
        assert proofs["a_counts"]["parseable_files"] == 4
        assert proofs["b_chunks"]["checked"] == 4
        assert proofs["d_brainstorms"] == [
            {"file": brainstorm.name, "found": True, "ok": True}
        ]

        assert mig.main(["--log-dir", d, "--skip-migrate"]) == 0
        assert junk.exists(), "files that aren't archived call logs are never deleted"
        assert not list(ld.glob("*-r0.log")), "archived call files are deleted"
        assert brainstorm.exists(), "brainstorms stay on disk for `review sessions`"
        assert retry.exists(), "non-call logs are untouched"
        assert _session_calls(ld) == before
        # Re-running is a no-op: everything is already archived.
        with closing(la.connect(ld)) as conn:
            stats = mig.migrate_directory(ld, conn=conn)
        assert stats["call_files"] == 0


def test_migration_leaves_a_possibly_streaming_call_alone():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        done = _write_file(ld, BASE)
        live = _write_file(ld, BASE + timedelta(minutes=1), backend="omp")
        os.utime(live)  # modified just now: the call may still be streaming
        assert mig.main(["--log-dir", d]) == 0
        assert not done.exists()
        assert live.exists(), "an in-flight call's file must never be archived+deleted"
        assert [c.filename for c in la.load_calls(ld)] == [done.name]


def test_proof_b_flags_a_name_recorded_without_content_and_a_changed_file():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        ok_file = _write_file(ld, BASE)
        orphan = _write_file(ld, BASE + timedelta(minutes=1))
        with closing(la.connect(ld)) as conn:
            la.store_call(
                ld,
                ok_file.name,
                ok_file.read_text(encoding="utf-8"),
                mtime=1.0,
                conn=conn,
            )
            assert mig.proof_b(ld, conn)["ok"]
            # recorded in call_files but absent from chunks and pending_calls
            conn.execute("INSERT INTO call_files(filename) VALUES (?)", (orphan.name,))
            res = mig.proof_b(ld, conn)
            assert not res["ok"] and res["mismatches"] == [orphan.name], res
            conn.execute("DELETE FROM call_files WHERE filename = ?", (orphan.name,))
            ok_file.write_text("changed\n", encoding="utf-8")
            res = mig.proof_b(ld, conn)
            assert not res["ok"] and res["mismatches"] == [ok_file.name], res


def test_connect_upgrades_a_v1_archive_to_the_covering_group_index():
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        with closing(la.connect(ld)) as conn:
            conn.execute("DROP INDEX idx_pending_group_size")
            conn.execute(
                "CREATE INDEX idx_pending_group ON pending_calls(day, backend)"
            )
            conn.execute("PRAGMA user_version=1")
        with closing(la.connect(ld)) as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            assert (
                "idx_pending_group_size" in names and "idx_pending_group" not in names
            ), names
            assert (
                conn.execute("PRAGMA user_version").fetchone()[0] == la._SCHEMA_VERSION
            )


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
