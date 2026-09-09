#!/usr/bin/env python3
"""Targeted tests for SQLite log archive storage."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.dashboard.parser import parse_call_log_text
from reviewlib.log_archive import (
    archive_counts,
    compress_payload,
    connect,
    decompress_payload,
    flush_pending_chunks,
    load_calls_from_archive,
    reset_connection_for_tests,
    store_brainstorm,
    store_call_raw,
)


def _archive_env():
    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "archive.sqlite3"
    os.environ["REVIEW_LOG_ARCHIVE"] = str(db)
    os.environ["REVIEW_LOG_SQLITE"] = "1"
    reset_connection_for_tests()
    connect()
    return db


def _cleanup_archive_env() -> None:
    reset_connection_for_tests()
    os.environ.pop("REVIEW_LOG_ARCHIVE", None)


def test_roundtrip_call_log():
    _archive_env()
    try:
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
    finally:
        _cleanup_archive_env()


def test_empty_call_skips_transcript():
    _archive_env()
    try:
        content = "[review-cli] codex: codex (args redacted)\n[review-cli] EXIT 0\n"
        started = datetime(2026, 6, 10, 12, 1, 0, tzinfo=timezone.utc)
        store_call_raw(backend="codex", round_no=0, started=started, content=content)
        counts = archive_counts()
        assert counts["empty_calls"] == 1
        assert counts["chunk_calls"] == 0
    finally:
        _cleanup_archive_env()


def test_brainstorm_roundtrip():
    _archive_env()
    try:
        body = "# Brainstorm: auth\npanel=codex moderator=codex rounds>=1 max=3\n"
        started = datetime(2026, 6, 10, 13, 0, 0, tzinfo=timezone.utc)
        store_brainstorm(started=started, topic="auth", body=body)
        raw = decompress_payload(compress_payload(body.encode("utf-8"), level=9)).decode("utf-8")
        assert raw == body
    finally:
        _cleanup_archive_env()


def test_migration_proofs_on_fixture():
    tmp = tempfile.mkdtemp()
    log_dir = Path(tmp) / "logs"
    log_dir.mkdir()
    db = Path(tmp) / "archive.sqlite3"
    os.environ["REVIEW_LOG_ARCHIVE"] = str(db)
    os.environ["REVIEW_LOG_SQLITE"] = "1"
    os.environ["REVIEW_LOG_DIR"] = str(log_dir)
    reset_connection_for_tests()
    connect()
    try:
        started = datetime(2026, 6, 11, 10, 0, 0, tzinfo=timezone.utc)
        name = started.strftime("%Y%m%dT%H%M%S_%fZ") + "-codex-r0.log"
        content = (
            "[review-cli] codex: codex (args redacted)\n"
            "prompt_tokens=10 output_tokens=5\n"
            "[review-cli] EXIT 0\n"
        )
        (log_dir / name).write_text(content, encoding="utf-8")
        (log_dir / "faketest-junk.log").write_text("x", encoding="utf-8")
        from reviewlib.log_archive_migrate import delete_faketest_logs, migrate_directory, proof_a

        n, freed = delete_faketest_logs(log_dir)
        assert n == 1
        conn = connect()
        migrate_directory(log_dir, conn=conn)
        flush_pending_chunks(conn)
        pa = proof_a(log_dir, conn)
        assert pa["match"] is True
    finally:
        _cleanup_archive_env()
        os.environ.pop("REVIEW_LOG_DIR", None)


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
