#!/usr/bin/env python3
"""One-shot migration: legacy log files -> SQLite archive."""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from reviewlib.dashboard.parser import _BRAINSTORM_RE, _CALL_RE, parse_brainstorm_log, parse_call_log
from reviewlib.dashboard.tokenstats import extract_usage_tokens
from reviewlib.log_archive import (
    archived_filenames,
    _ChunkEntry,
    _PendingChunk,
    _day_key,
    _flush_pending,
    _update_daily_stats,
    archive_counts,
    archive_db_path,
    archive_db_size,
    connect,
    decompress_payload,
    flush_pending_chunks,
    is_empty_stub,
    reset_connection_for_tests,
    store_brainstorm,
)


def _is_faketest_name(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("faketest") or lower.startswith("faketree")


def delete_faketest_logs(log_dir: Path) -> tuple[int, int]:
    freed = 0
    count = 0
    for entry in log_dir.iterdir():
        if not entry.is_file() or not _is_faketest_name(entry.name):
            continue
        try:
            freed += entry.stat().st_size
            entry.unlink()
            count += 1
        except OSError:
            pass
    return count, freed


def _tokens(call) -> tuple[int, int]:
    usage = extract_usage_tokens(call)
    return usage if usage is not None else (0, 0)


def migrate_directory(log_dir: Path, *, conn) -> dict[str, int]:
    pending: dict[tuple[str, str], _PendingChunk] = {}
    already = archived_filenames(conn)
    stats = {"call_files": 0, "empty": 0, "brainstorm": 0, "skipped_retry": 0, "skipped_existing": 0}
    entries: list[tuple[str, Path, str]] = []
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if _is_faketest_name(name):
            continue
        if _CALL_RE.match(name):
            entries.append(("call", entry, name))
        elif _BRAINSTORM_RE.match(name):
            entries.append(("brainstorm", entry, name))
        elif name.endswith(".log"):
            stats["skipped_retry"] += 1

    def sort_key(item):
        _, _, name = item
        m = _CALL_RE.match(name) or _BRAINSTORM_RE.match(name)
        return (m.group(1), m.group(2)) if m else ("", "")

    entries.sort(key=sort_key)
    for kind, path, name in entries:
        if kind == "brainstorm":
            if name in already:
                stats["skipped_existing"] += 1
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            parsed = parse_brainstorm_log(path)
            topic = parsed.topic if parsed else ""
            started = parsed.started if parsed else datetime.now(timezone.utc)
            store_brainstorm(started=started, topic=topic, body=raw, conn=conn)
            stats["brainstorm"] += 1
            already.add(name)
            continue
        if name in already:
            stats["skipped_existing"] += 1
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        call = parse_call_log(path)
        if call is None:
            continue
        stats["call_files"] += 1
        day = _day_key(call.started)
        backend = call.backend
        if is_empty_stub(raw, size, call):
            pt, ot = _tokens(call)
            conn.execute(
                """INSERT OR REPLACE INTO empty_calls
                  (day, ts, backend, round, task, exit, prompt_tokens, output_tokens, is_empty)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (day, name.split("-", 1)[0], backend, call.round, call.task_code, call.exit_code, pt, ot),
            )
            n_fail = 1 if call.exit_code not in (0, None) else 0
            _update_daily_stats(conn, day, backend, n_calls=1, n_fail=n_fail, prompt_tokens=pt, output_tokens=ot, raw_bytes=size, zstd_bytes=0)
            conn.execute("INSERT OR IGNORE INTO call_files(filename) VALUES (?)", (name,))
            already.add(name)
            stats["empty"] += 1
            continue
        key = (day, backend)
        buf = pending.get(key)
        if buf is None:
            buf = _PendingChunk(day=day, backend=backend, entries=[])
            pending[key] = buf
        if buf.append(_ChunkEntry(filename=name, content=raw, mtime=path.stat().st_mtime, size=size)):
            _flush_pending(conn, buf)
            pending.pop(key, None)
    for buf in pending.values():
        _flush_pending(conn, buf)
    conn.commit()
    return stats


def proof_a(log_dir: Path, conn) -> dict:
    archived = archived_filenames(conn)
    parseable: list[str] = []
    for entry in log_dir.iterdir():
        if not entry.is_file() or not _CALL_RE.match(entry.name) or _is_faketest_name(entry.name):
            continue
        if parse_call_log(entry) is not None:
            parseable.append(entry.name)
    missing = set(parseable) - archived
    counts = archive_counts(conn)
    return {
        "parseable_files": len(parseable),
        "archive_chunk_calls": counts["chunk_calls"],
        "archive_empty_calls": counts["empty_calls"],
        "archive_total": len(archived),
        "missing_from_archive": len(missing),
        "match": len(missing) == 0,
    }


def proof_b(log_dir: Path, conn, n: int = 5) -> list[dict]:
    from reviewlib.log_archive import load_chunk_entries

    chunks = load_chunk_entries(conn)
    if not chunks:
        return []
    sample = random.sample(chunks, min(n, len(chunks)))
    out = []
    for chunk in sample:
        entries = chunk["entries"]
        picks = random.sample(entries, min(3, len(entries)))
        mismatches = 0
        checked = 0
        for entry in picks:
            checked += 1
            src = log_dir / entry["filename"]
            if src.exists() and src.read_text(encoding="utf-8", errors="replace") != entry["content"]:
                mismatches += 1
        out.append(
            {
                "day": chunk["day"],
                "backend": chunk["backend"],
                "n_calls": chunk["n_calls"],
                "spot_entries_checked": checked,
                "mismatches": mismatches,
                "roundtrip_ok": mismatches == 0,
            }
        )
    return out


def proof_c(log_dir: Path, conn, n: int = 3) -> list[dict]:
    days = sorted(
        {
            _day_key(call.started)
            for call in (parse_call_log(p) for p in log_dir.glob("*.log"))
            if call is not None and not _is_faketest_name(call.filename)
        }
    )
    if not days:
        return []
    sample_days = random.sample(days, min(n, len(days)))
    results = []
    for day in sample_days:
        parsed_prompt = parsed_output = parsed_calls = 0
        for path in log_dir.glob("*.log"):
            if not _CALL_RE.match(path.name) or _is_faketest_name(path.name):
                continue
            call = parse_call_log(path)
            if call is None or _day_key(call.started) != day:
                continue
            parsed_calls += 1
            pt, ot = _tokens(call)
            parsed_prompt += pt
            parsed_output += ot
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(n_calls),0) FROM daily_stats WHERE day=?",
            (day,),
        ).fetchone()
        results.append(
            {
                "day": day,
                "files_prompt_tokens": parsed_prompt,
                "files_output_tokens": parsed_output,
                "files_calls": parsed_calls,
                "stats_prompt_tokens": int(row[0]),
                "stats_output_tokens": int(row[1]),
                "stats_calls": int(row[2]),
                "match": parsed_prompt == int(row[0]) and parsed_output == int(row[1]) and parsed_calls == int(row[2]),
            }
        )
    return results


def proof_d(log_dir: Path, conn, n: int = 2) -> list[dict]:
    paths = [p for p in log_dir.glob("*-brainstorm.md") if _BRAINSTORM_RE.match(p.name) and not _is_faketest_name(p.name)]
    if not paths:
        return []
    sample = random.sample(paths, min(n, len(paths)))
    out = []
    for path in sample:
        raw = path.read_text(encoding="utf-8", errors="replace")
        ts = path.name.replace("-brainstorm.md", "")
        row = conn.execute("SELECT body_zstd FROM brainstorm WHERE ts=?", (ts,)).fetchone()
        if row is None:
            out.append({"file": path.name, "found": False, "match": False})
            continue
        body = decompress_payload(row[0]).decode("utf-8", errors="replace")
        out.append({"file": path.name, "found": True, "match": raw == body})
    return out


def delete_migrated_sources(log_dir: Path) -> tuple[int, int]:
    removed = freed = 0
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if _is_faketest_name(name):
            continue
        if _CALL_RE.match(name) or _BRAINSTORM_RE.match(name):
            try:
                freed += entry.stat().st_size
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed, freed


def source_bytes(log_dir: Path) -> int:
    total = 0
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        if _CALL_RE.match(entry.name) or _BRAINSTORM_RE.match(entry.name):
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate review-cli logs to SQLite")
    parser.add_argument("--log-dir", default=os.environ.get("REVIEW_LOG_DIR", str(Path.home() / "Library/Logs/review-cli")))
    parser.add_argument("--archive", default="")
    parser.add_argument("--skip-delete", action="store_true")
    parser.add_argument("--skip-migrate", action="store_true")
    ns = parser.parse_args(argv)
    log_dir = Path(ns.log_dir)
    if ns.archive:
        os.environ["REVIEW_LOG_ARCHIVE"] = ns.archive
    reset_connection_for_tests()
    conn = connect()
    fake_n, fake_freed = delete_faketest_logs(log_dir)
    print(f"faketest deleted: count={fake_n} freed_bytes={fake_freed}")
    if not ns.skip_migrate:
        stats = migrate_directory(log_dir, conn=conn)
        flush_pending_chunks(conn)
        print(f"migrate: {stats}")
    pa = proof_a(log_dir, conn)
    pb = proof_b(log_dir, conn)
    pc = proof_c(log_dir, conn)
    pd = proof_d(log_dir, conn)
    print(f"proof_a: {pa}")
    print(f"proof_b: {pb}")
    print(f"proof_c: {pc}")
    print(f"proof_d: {pd}")
    ok = pa["match"] and all(x.get("roundtrip_ok", False) for x in pb) and all(x.get("match", False) for x in pc) and all(x.get("match", False) for x in pd)
    src = source_bytes(log_dir)
    db = archive_db_size()
    print(f"source_call_brainstorm_bytes={src} archive_db_bytes={db} db={archive_db_path()}")
    if ok and not ns.skip_delete:
        removed, freed = delete_migrated_sources(log_dir)
        print(f"deleted_sources: count={removed} freed_bytes={freed}")
    elif not ok:
        print("proofs FAILED — source .log/.md files kept", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
