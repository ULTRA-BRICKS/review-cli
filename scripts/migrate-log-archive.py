#!/usr/bin/env python3
"""One-shot migration: pack a review-cli log dir into the SQLite log archive.

Reads every `*-r{n}.log` / `*-brainstorm.md` in `--log-dir` with the REAL parser,
writes compressed chunks + empty-call rows + brainstorm rows + `daily_stats` into
`--db`, then runs all four proofs (counts, chunk diff, day tokens, brainstorms).

`--delete-migrated` unlinks converted call+brainstorm files ONLY after 4/4 proofs
PASS, otherwise exits 3 and deletes nothing. Retry (`*-retry-*.log`), `qa-*.md`
and any other files are NEVER touched. `--delete-cache` removes the derived
`.call-log-cache.sqlite3*` + `.call-log-cache.pkl` files and reports bytes.

Usage:
  scripts/migrate-log-archive.py --log-dir DIR --db FILE [--delete-migrated]
      [--delete-cache] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.dashboard import log_archive as la  # noqa: E402

EXIT_PROOFS_FAILED = 3


def _census(log_dir: Path) -> tuple[int, int, list[str]]:
    """Count migratable call logs + brainstorm mds (the proof denominators)."""
    from reviewlib.dashboard.parser import _BRAINSTORM_RE, _CALL_RE

    calls = 0
    brainstorms = 0
    for path in sorted(log_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if any(m in name for m in ("faketest-", "faketree-", "-retry-")):
            continue
        if _CALL_RE.match(name):
            calls += 1
        elif _BRAINSTORM_RE.match(name):
            brainstorms += 1
    try:
        days = sorted(
            {r[0] for r in la.connect(Path("--none--")).execute("SELECT 1").fetchall()}
        )
    except Exception:  # noqa: BLE001
        days = []
    return calls, brainstorms, days


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log-dir", required=True, type=Path)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--delete-migrated", action="store_true")
    ap.add_argument("--delete-cache", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    log_dir: Path = args.log_dir
    db: Path = args.db
    if not log_dir.is_dir():
        print(f"error: log dir not found: {log_dir}", file=sys.stderr)
        return 2

    print(f"codec: {la.codec_name()}")
    print(f"log dir: {log_dir}")
    print(f"db: {db}")

    result = la.migrate_all(log_dir, db, limit=args.limit)
    print("migrated:")
    for key in (
        "chunk",
        "empty",
        "brainstorm",
        "retry-skip",
        "other-skip",
        "fake-skip",
        "unparseable",
    ):
        print(f"  {key}: {result.get(key, 0)}")
    print(f"  chunks_written: {result['chunks_written']}")
    print(f"  body_bytes: {result['body_bytes']}")
    try:
        print(f"  db_bytes: {db.stat().st_size}")
    except OSError:
        pass

    conn = la.connect(db)
    file_calls, file_brainstorms, _ = _census(log_dir)
    try:
        days = [
            r[0]
            for r in conn.execute("SELECT DISTINCT day FROM daily_stats").fetchall()
        ]
    except Exception:  # noqa: BLE001
        days = []

    proofs = [
        ("counts", la.proof_counts(conn, file_calls, file_brainstorms)),
        ("chunks_diff", la.proof_chunks_diff(conn, log_dir)),
        ("day_tokens", la.proof_day_tokens(conn, log_dir, days)),
        ("brainstorms", la.proof_brainstorms(conn, log_dir)),
    ]
    all_ok = True
    for name, proof in proofs:
        status = "PASS" if proof["ok"] else "FAIL"
        all_ok = all_ok and proof["ok"]
        print(f"proof {name}: {status}")
        print(f"  {json.dumps(proof['detail'], indent=2, default=str)}")

    if args.delete_cache:
        freed = 0
        removed: list[str] = []
        for name in (
            ".call-log-cache.sqlite3",
            ".call-log-cache.sqlite3-wal",
            ".call-log-cache.sqlite3-shm",
            ".call-log-cache.pkl",
        ):
            target = log_dir / name
            try:
                freed += target.stat().st_size
                target.unlink()
                removed.append(name)
            except OSError:
                pass
        print(f"cache: removed {removed} ({freed} bytes)")

    if args.delete_migrated:
        if not all_ok:
            print("NOT deleting: proofs did not all pass", file=sys.stderr)
            return EXIT_PROOFS_FAILED
        if args.limit is not None:
            print("NOT deleting: --limit was set (partial migration)", file=sys.stderr)
            return EXIT_PROOFS_FAILED
        from reviewlib.dashboard.parser import _BRAINSTORM_RE, _CALL_RE

        deleted = 0
        freed = 0
        archived = la.archived_filenames(conn)
        for path in sorted(log_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if not (_CALL_RE.match(name) or _BRAINSTORM_RE.match(name)):
                continue
            if name not in archived:
                continue
            try:
                freed += path.stat().st_size
                path.unlink()
                deleted += 1
            except OSError as exc:
                print(f"warning: could not delete {name}: {exc}", file=sys.stderr)
        print(f"deleted {deleted} migrated files ({freed} bytes)")

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
