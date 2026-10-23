#!/usr/bin/env python3
"""One-shot migration: a log dir's legacy per-call `.log` files -> its SQLite archive.

    python -m reviewlib.log_archive_migrate [--log-dir DIR] [--skip-delete]

Archives every parseable call log (idempotent: names already archived are skipped),
copies every brainstorm discussion log as a durable backup, then runs four proofs:

  A. counts   — every parseable call file is in the archive;
  B. chunks   — sampled archived entries are byte-identical to their source files;
  C. day tokens — for sampled days, the tokens and call count parsed from the files equal
                those parsed from their archived copies;
  D. brainstorms — sampled brainstorm bodies are byte-identical in the archive.

Only when all four pass are the archived CALL files deleted. Brainstorm `.md` files are
never deleted: `review sessions` resumes from them (reviewlib/sessions.py).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from reviewlib.dashboard.parser import (
    _BRAINSTORM_RE,
    _CALL_RE,
    _MAX_CALL_WALL,
    parse_brainstorm_log,
    parse_call_log,
    parse_call_log_text,
)
from reviewlib.dashboard.tokenstats import extract_usage_tokens
from reviewlib.log_archive import (
    _day_of_stamp,
    _insert_call,
    _stamp_of,
    archive_counts,
    archive_db_path,
    archive_db_size,
    archived_filenames,
    connect,
    decompress_payload,
    immediate_transaction,
    iter_archived_entries,
    store_brainstorm,
)

# Commit every this many files: one transaction per file is needlessly slow for a
# 150k-file corpus, one transaction for all of it holds the write lock for minutes.
_MIGRATE_BATCH = 500


def _tokens(call) -> tuple[int, int]:
    usage = extract_usage_tokens(call)
    return usage if usage is not None else (0, 0)


# One cutoff for the whole run: migration, proofs and deletion must agree on which files
# count as finished, or a file crossing the threshold mid-run would fail proof A.
_FINISHED_BEFORE = time.time() - _MAX_CALL_WALL.total_seconds()


def _call_files(log_dir: Path) -> list[Path]:
    """FINISHED call logs, oldest first (stamp order = archive seal order). A file
    modified within `_MAX_CALL_WALL` of this run's start may still be streaming:
    archiving and then deleting it would cut off the rest of that call, so every step
    here (migration, proofs, deletion) leaves it alone; the call archives itself when it
    finishes."""
    fresh_after = _FINISHED_BEFORE
    return sorted(
        (
            p
            for p in log_dir.iterdir()
            if p.is_file()
            and _CALL_RE.match(p.name)
            and p.stat().st_mtime < fresh_after
        ),
        key=lambda p: p.name,
    )


def _brainstorm_files(log_dir: Path) -> list[Path]:
    return sorted(
        p for p in log_dir.iterdir() if p.is_file() and _BRAINSTORM_RE.match(p.name)
    )


def migrate_directory(log_dir: Path, *, conn) -> dict[str, int]:
    stats = {"call_files": 0, "brainstorm": 0, "skipped_existing": 0, "unparseable": 0}
    files = _call_files(log_dir)
    for start in range(0, len(files), _MIGRATE_BATCH):
        with immediate_transaction(conn):
            for path in files[start : start + _MIGRATE_BATCH]:
                if parse_call_log(path) is None:
                    stats["unparseable"] += 1
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                # Files arrive in stamp order, so `_insert_call`'s day-rollover sealing
                # closes each earlier day exactly once.
                if not _insert_call(conn, path.name, content, path.stat().st_mtime):
                    stats["skipped_existing"] += 1
                    continue
                stats["call_files"] += 1
    for path in _brainstorm_files(log_dir):
        parsed = parse_brainstorm_log(path)
        store_brainstorm(
            log_dir,
            path.name,
            path.read_text(encoding="utf-8", errors="replace"),
            topic=parsed.topic if parsed else "",
            mtime=path.stat().st_mtime,
            conn=conn,
        )
        stats["brainstorm"] += 1
    return stats


def proof_a(log_dir: Path, conn) -> dict:
    archived = archived_filenames(conn)
    parseable = [p.name for p in _call_files(log_dir) if parse_call_log(p) is not None]
    missing = set(parseable) - archived
    counts = archive_counts(conn)
    return {
        "parseable_files": len(parseable),
        "archive_chunk_calls": counts["chunk_calls"],
        "archive_pending_calls": counts["pending_calls"],
        "archive_total": len(archived),
        "missing_from_archive": len(missing),
        "ok": not missing,
    }


def proof_b(log_dir: Path, conn, n: int = 20) -> dict:
    # Sample names first (cheap), then keep only the sampled entries' contents.
    on_disk = [name for name in archived_filenames(conn) if (log_dir / name).exists()]
    wanted = set(random.sample(on_disk, min(n, len(on_disk))))
    seen: set[str] = set()
    mismatches: list[str] = []
    if wanted:
        for filename, content, _mtime, _size in iter_archived_entries(conn):
            if filename not in wanted:
                continue
            seen.add(filename)
            if (log_dir / filename).read_text(
                encoding="utf-8", errors="replace"
            ) != content:
                mismatches.append(filename)
    # A sampled name recorded in call_files but absent from every chunk/pending row is a
    # defect, not a pass.
    mismatches.extend(sorted(wanted - seen))
    return {"checked": len(seen), "mismatches": mismatches, "ok": not mismatches}


def proof_c(log_dir: Path, conn, n: int = 3) -> list[dict]:
    files = [p for p in _call_files(log_dir)]
    days = sorted({_day_of_stamp(_stamp_of(p.name)) for p in files})
    results = []
    for day in random.sample(days, min(n, len(days))):
        archived = {
            filename: (content, size)
            for filename, content, _mtime, size in iter_archived_entries(conn, day=day)
        }
        file_tokens = [0, 0, 0]
        copy_tokens = [0, 0, 0]
        for path in files:
            if _day_of_stamp(_stamp_of(path.name)) != day:
                continue
            call = parse_call_log(path)
            if call is None:
                continue
            pt, ot = _tokens(call)
            file_tokens = [file_tokens[0] + pt, file_tokens[1] + ot, file_tokens[2] + 1]
            if path.name in archived:
                content, size = archived[path.name]
                copy = parse_call_log_text(path.name, content, size=size)
                cpt, cot = _tokens(copy) if copy is not None else (0, 0)
                copy_tokens = [
                    copy_tokens[0] + cpt,
                    copy_tokens[1] + cot,
                    copy_tokens[2] + 1,
                ]
        results.append(
            {
                "day": day,
                "files": file_tokens,
                "archived_copies": copy_tokens,
                "ok": file_tokens == copy_tokens,
            }
        )
    return results


def proof_d(log_dir: Path, conn, n: int = 5) -> list[dict]:
    paths = _brainstorm_files(log_dir)
    out = []
    for path in random.sample(paths, min(n, len(paths))):
        row = conn.execute(
            "SELECT body FROM brainstorm WHERE filename = ?", (path.name,)
        ).fetchone()
        if row is None:
            out.append({"file": path.name, "found": False, "ok": False})
            continue
        body = decompress_payload(row[0]).decode("utf-8", errors="replace")
        raw = path.read_text(encoding="utf-8", errors="replace")
        out.append({"file": path.name, "found": True, "ok": raw == body})
    return out


def delete_archived_call_files(log_dir: Path, conn) -> tuple[int, int]:
    """Delete call files that are provably in the archive. Never touches brainstorms."""
    archived = archived_filenames(conn)
    removed = freed = 0
    for path in _call_files(log_dir):
        if path.name not in archived:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


def source_bytes(log_dir: Path) -> int:
    total = 0
    for path in _call_files(log_dir) + _brainstorm_files(log_dir):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def run_proofs(log_dir: Path, conn) -> dict:
    proofs = {
        "a_counts": proof_a(log_dir, conn),
        "b_chunks": proof_b(log_dir, conn),
        "c_day_tokens": proof_c(log_dir, conn),
        "d_brainstorms": proof_d(log_dir, conn),
    }
    proofs["ok"] = (
        proofs["a_counts"]["ok"]
        and proofs["b_chunks"]["ok"]
        and all(x["ok"] for x in proofs["c_day_tokens"])
        and all(x["ok"] for x in proofs["d_brainstorms"])
    )
    return proofs


def main(argv: list[str] | None = None) -> int:
    from reviewlib.process import log_dir as default_log_dir

    parser = argparse.ArgumentParser(
        prog="python -m reviewlib.log_archive_migrate",
        description="Migrate a review-cli log dir's call logs into its SQLite archive",
    )
    parser.add_argument("--log-dir", default=None, help="default: review's own log dir")
    parser.add_argument(
        "--skip-delete",
        action="store_true",
        help="keep the source files even if proofs pass",
    )
    parser.add_argument(
        "--skip-migrate", action="store_true", help="only run the proofs"
    )
    ns = parser.parse_args(argv)
    log_dir = Path(ns.log_dir) if ns.log_dir else default_log_dir()
    conn = connect(log_dir)
    assert conn is not None
    try:
        if not ns.skip_migrate:
            print(f"migrate: {migrate_directory(log_dir, conn=conn)}")
        proofs = run_proofs(log_dir, conn)
        for key in ("a_counts", "b_chunks", "c_day_tokens", "d_brainstorms"):
            print(f"proof {key}: {proofs[key]}")
        print(
            f"source_bytes={source_bytes(log_dir)} archive_bytes={archive_db_size(log_dir)} "
            f"archive={archive_db_path(log_dir)}"
        )
        if not proofs["ok"]:
            print("proofs FAILED — source files kept", file=sys.stderr)
            return 1
        if not ns.skip_delete:
            removed, freed = delete_archived_call_files(log_dir, conn)
            print(f"deleted archived call files: count={removed} freed_bytes={freed}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
