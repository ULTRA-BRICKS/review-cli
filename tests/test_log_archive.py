"""Targeted tests for SQLite log archive storage."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reviewlib.dashboard.parser import parse_call_log_text
from reviewlib.log_archive import (
    archive_counts,
    archived_filenames,
    compress_payload,
    connect,
    decompress_payload,
    flush_pending_chunks,
    load_calls_from_archive,
    payload_filenames,
    prune_faketest_archive,
    query_daily_stats,
    reset_connection_for_tests,
    rotate,
    store_brainstorm,
    store_call_raw,
)


@pytest.fixture()
def archive_env(tmp_path, monkeypatch):
    db = tmp_path / "archive.sqlite3"
    monkeypatch.setenv("REVIEW_LOG_ARCHIVE", str(db))
    monkeypatch.setenv("REVIEW_LOG_SQLITE", "1")
    reset_connection_for_tests()
    connect()
    yield db
    reset_connection_for_tests()


def test_roundtrip_call_log(archive_env):
    content = (
        "[review-cli] codex: codex -m gpt (args redacted)\n"
        "## codex [ok]\nLooks good.\n"
        "[review-cli] EXIT 0\n"
    )
    started = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    store_call_raw(backend="codex", round_no=0, started=started, content=content)
    flush_pending_chunks()
    calls = load_calls_from_archive()
    assert len(calls) == 1
    assert calls[0].backend == "codex"
    assert calls[0].exit_code == 0


def test_empty_call_skips_transcript(archive_env):
    content = "[review-cli] codex: codex (args redacted)\n[review-cli] EXIT 0\n"
    started = datetime(2026, 6, 10, 12, 1, 0, tzinfo=timezone.utc)
    store_call_raw(backend="codex", round_no=0, started=started, content=content)
    counts = archive_counts()
    assert counts["empty_calls"] == 1
    assert counts["chunk_calls"] == 0


def test_brainstorm_roundtrip(archive_env):
    body = "# Brainstorm: auth\npanel=codex moderator=codex rounds>=1 max=3\n"
    started = datetime(2026, 6, 10, 13, 0, 0, tzinfo=timezone.utc)
    store_brainstorm(started=started, topic="auth", body=body)
    raw = decompress_payload(compress_payload(body.encode("utf-8"), level=9)).decode("utf-8")
    assert raw == body


def test_migration_proofs_on_fixture(tmp_path, archive_env, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    started = datetime(2026, 6, 11, 10, 0, 0, tzinfo=timezone.utc)
    name = started.strftime("%Y%m%dT%H%M%S_%fZ") + "-codex-r0.log"
    content = (
        "[review-cli] codex: codex (args redacted)\n"
        "prompt_tokens=10 output_tokens=5\n"
        "[review-cli] EXIT 0\n"
    )
    (log_dir / name).write_text(content, encoding="utf-8")
    (log_dir / "faketest-junk.log").write_text("x", encoding="utf-8")
    monkeypatch.setenv("REVIEW_LOG_DIR", str(log_dir))
    from reviewlib.log_archive_migrate import delete_faketest_logs, migrate_directory, proof_a

    n, freed = delete_faketest_logs(log_dir)
    assert n == 1
    conn = connect()
    migrate_directory(log_dir, conn=conn)
    flush_pending_chunks(conn)
    pa = proof_a(log_dir, conn)
    assert pa["match"] is True


def test_archived_filenames_uses_payload_when_call_files_stale(archive_env):
    content = (
        "[review-cli] codex: codex (args redacted)\n"
        "## codex [ok]\nLooks good.\n"
        "[review-cli] EXIT 0\n"
    )
    started = datetime(2026, 6, 12, 14, 0, 0, tzinfo=timezone.utc)
    store_call_raw(backend="codex", round_no=0, started=started, content=content)
    flush_pending_chunks()
    conn = connect()
    conn.execute("DELETE FROM call_files")
    conn.commit()
    assert archived_filenames(conn) == payload_filenames(conn)
    rows = conn.execute("SELECT COUNT(*) FROM call_files").fetchall()
    assert rows[0][0] == 1

def test_rotate_does_not_delete_by_default(archive_env):
    conn = connect()
    old_day = "2000-01-01"
    conn.execute(
        """
        INSERT INTO chunks(day, backend, min_ts, max_ts, n_calls, payload_zstd)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (old_day, "codex", "20000101T000000_000000Z", "20000101T000000_000000Z", 1, b"x"),
    )
    conn.execute(
        """
        INSERT INTO daily_stats(day, backend, n_calls, n_fail, prompt_tokens,
                                output_tokens, raw_bytes, zstd_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (old_day, "codex", 1, 0, 0, 0, 10, 1),
    )
    conn.commit()
    n_chunks, n_stats = rotate(conn)
    assert n_chunks == 0
    assert n_stats == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 1


def test_store_call_skips_faketest(archive_env):
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    content = "[review-cli] faketest: x (args redacted)\nbody\n[review-cli] EXIT 0\n"
    filename = store_call_raw(
        backend="faketest",
        round_no=0,
        started=started,
        content=content,
    )
    flush_pending_chunks()
    counts = archive_counts()
    assert counts["chunks"] == 0
    assert counts["empty_calls"] == 0
    assert counts["call_meta"] == 0
    assert filename.startswith("20260102")


def test_prune_faketest_removes_archive_rows(archive_env):
    import json

    conn = connect()
    started = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    good = "[review-cli] codex: codex (args redacted)\nok\n[review-cli] EXIT 0\n"
    store_call_raw(backend="codex", round_no=0, started=started, content=good, conn=conn)
    flush_pending_chunks(conn)
    fake_name = "20260201T120001_000000Z-faketest-r0.log"
    fake_content = "[review-cli] faketest: x (args redacted)\nnoise\n[review-cli] EXIT 0\n"
    payload = json.dumps(
        [
            {
                "filename": fake_name,
                "content": fake_content,
                "mtime": started.timestamp(),
                "size": len(fake_content),
            }
        ]
    ).encode("utf-8")
    blob = compress_payload(payload)
    conn.execute(
        """
        INSERT INTO chunks(day, backend, min_ts, max_ts, n_calls, payload_zstd)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("2026-02-01", "faketest", "20260201T120001_000000Z", "20260201T120001_000000Z", 1, blob),
    )
    conn.execute("INSERT OR IGNORE INTO call_files(filename) VALUES (?)", (fake_name,))
    conn.commit()
    report = prune_faketest_archive(conn)
    assert report.filenames_removed >= 1
    rows = conn.execute(
        "SELECT filename FROM call_files WHERE filename LIKE 'faketest%'"
    ).fetchall()
    assert rows == []
    remaining = load_calls_from_archive(conn)
    assert len(remaining) == 1
    assert remaining[0].backend == "codex"


def test_call_meta_written_on_store(archive_env):
    conn = connect()
    started = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    content = "[review-cli] codex: codex (args redacted)\nbody\n[review-cli] EXIT 0\n"
    store_call_raw(backend="codex", round_no=0, started=started, content=content, conn=conn)
    flush_pending_chunks(conn)
    row = conn.execute(
        "SELECT backend, round, is_empty FROM call_meta"
    ).fetchone()
    assert row is not None
    assert row[0] == "codex"
    assert row[1] == 0
    assert row[2] == 0


def test_query_daily_stats_aggregate(archive_env):
    conn = connect()
    day = "2026-04-01"
    conn.execute(
        """
        INSERT INTO daily_stats(day, backend, n_calls, n_fail, prompt_tokens,
                                output_tokens, raw_bytes, zstd_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (day, "codex", 2, 1, 100, 50, 2000, 400),
    )
    conn.execute(
        """
        INSERT INTO daily_stats(day, backend, n_calls, n_fail, prompt_tokens,
                                output_tokens, raw_bytes, zstd_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (day, "omp", 3, 0, 0, 0, 3000, 500),
    )
    conn.commit()
    rows = query_daily_stats(conn, since=datetime(2026, 4, 1, tzinfo=timezone.utc))
    by_backend = {r["backend"]: r for r in rows}
    assert by_backend["codex"]["n_calls"] == 2
    assert by_backend["codex"]["n_fail"] == 1
    assert by_backend["omp"]["n_calls"] == 3
    assert sum(r["n_calls"] for r in rows) == 5

