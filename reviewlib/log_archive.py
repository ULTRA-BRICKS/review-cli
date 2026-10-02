"""SQLite-backed durable storage for review-cli call and brainstorm logs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dashboard.parser import BrainstormLog, CallLog

try:
    import zstandard as _zstd  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _zstd = None

_CHUNK_TARGET_CALLS = 1000
_CHUNK_TARGET_RAW_BYTES = 1_000_000
_EMPTY_MAX_BYTES = 300
_CHUNK_ZSTD_LEVEL = 6
_BRAINSTORM_ZSTD_LEVEL = 9
_ZLIB_LEVEL = 9

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA page_size=8192;

CREATE TABLE IF NOT EXISTS chunks (
  day TEXT NOT NULL,
  backend TEXT NOT NULL,
  min_ts TEXT NOT NULL,
  max_ts TEXT NOT NULL,
  n_calls INTEGER NOT NULL,
  payload_zstd BLOB NOT NULL,
  PRIMARY KEY (day, backend, min_ts)
) WITHOUT ROWID, STRICT;

CREATE TABLE IF NOT EXISTS brainstorm (
  day TEXT NOT NULL,
  ts TEXT NOT NULL,
  topic TEXT NOT NULL,
  body_zstd BLOB NOT NULL,
  PRIMARY KEY (day, ts)
) WITHOUT ROWID, STRICT;

CREATE TABLE IF NOT EXISTS daily_stats (
  day TEXT NOT NULL,
  backend TEXT NOT NULL,
  n_calls INTEGER NOT NULL,
  n_fail INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  raw_bytes INTEGER NOT NULL,
  zstd_bytes INTEGER NOT NULL,
  PRIMARY KEY (day, backend)
) WITHOUT ROWID, STRICT;

CREATE TABLE IF NOT EXISTS empty_calls (
  day TEXT NOT NULL,
  ts TEXT NOT NULL,
  backend TEXT NOT NULL,
  round INTEGER NOT NULL,
  task TEXT,
  exit INTEGER,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  is_empty INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (day, ts, backend, round)
) WITHOUT ROWID, STRICT;

CREATE INDEX IF NOT EXISTS idx_chunks_day ON chunks(day);
CREATE INDEX IF NOT EXISTS idx_chunks_backend ON chunks(backend);
CREATE INDEX IF NOT EXISTS idx_brainstorm_day ON brainstorm(day);
CREATE INDEX IF NOT EXISTS idx_empty_calls_day ON empty_calls(day);
CREATE INDEX IF NOT EXISTS idx_empty_calls_backend ON empty_calls(backend);
CREATE TABLE IF NOT EXISTS call_files (
  filename TEXT PRIMARY KEY
) WITHOUT ROWID, STRICT;

CREATE TABLE IF NOT EXISTS call_meta (
  filename       TEXT PRIMARY KEY,
  started_unix   INTEGER NOT NULL,
  day            TEXT NOT NULL,
  backend        TEXT NOT NULL,
  round          INTEGER NOT NULL,
  task           TEXT,
  exit           INTEGER,
  prompt_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  raw_bytes      INTEGER NOT NULL DEFAULT 0,
  is_empty       INTEGER NOT NULL DEFAULT 0,
  has_error      INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID, STRICT;

CREATE INDEX IF NOT EXISTS idx_daily_stats_day ON daily_stats(day);
CREATE INDEX IF NOT EXISTS idx_call_meta_started ON call_meta(started_unix);
CREATE INDEX IF NOT EXISTS idx_call_meta_day ON call_meta(day);
CREATE INDEX IF NOT EXISTS idx_call_meta_backend ON call_meta(backend);
"""

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_CONN_PATH: Path | None = None


def archive_db_path() -> Path:
    override = os.environ.get("REVIEW_LOG_ARCHIVE", "").strip()
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = (
        Path(state)
        if state and os.path.isabs(state)
        else (Path.home() / ".local" / "state")
    )
    return root / "review-cli" / "logs-archive.sqlite3"


def use_sqlite_logs() -> bool:
    raw = os.environ.get("REVIEW_LOG_SQLITE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}




def is_faketest_name(name: str) -> bool:
    lower = name.lower()
    if lower.startswith("faketest") or lower.startswith("faketree"):
        return True
    return "-faketest-" in lower or "-faketree-" in lower


def _chunks_retain_days() -> int:
    raw = os.environ.get("REVIEW_LOG_ARCHIVE_RETAIN_DAYS_CHUNKS", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _started_unix_micro(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)

def _day_key(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _ts_key(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def compress_payload(data: bytes, *, level: int = _CHUNK_ZSTD_LEVEL) -> bytes:
    if _zstd is not None:
        return _zstd.ZstdCompressor(level=level).compress(data)
    return zlib.compress(data, level=_ZLIB_LEVEL)


def decompress_payload(data: bytes) -> bytes:
    if _zstd is not None:
        try:
            return _zstd.ZstdDecompressor().decompress(data)
        except _zstd.ZstdError:
            pass
    return zlib.decompress(data)


def connect(*, create: bool = True) -> sqlite3.Connection:
    global _CONN, _CONN_PATH
    path = archive_db_path()
    with _LOCK:
        if _CONN is not None and _CONN_PATH == path:
            return _CONN
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        path.parent.mkdir(parents=True, exist_ok=True)
        if create and not path.exists():
            path.touch(mode=0o600)
        conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _CONN = conn
        _CONN_PATH = path
        return conn


def reset_connection_for_tests() -> None:
    global _CONN, _CONN_PATH
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None
        _CONN_PATH = None


def archive_signature() -> tuple[int, float, int]:
    try:
        conn = connect()
    except sqlite3.Error:
        return (0, 0.0, 0)
    with _LOCK:
        try:
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            n_empty = conn.execute("SELECT COUNT(*) FROM empty_calls").fetchone()[0]
            row = conn.execute("SELECT MAX(max_ts) FROM chunks").fetchone()
            max_ts = row[0] or ""
            max_val = float(
                max_ts.replace("_", "").replace("T", "").replace("Z", "")[:20] or 0
            )
            return (int(n_chunks), max_val, int(n_empty))
        except sqlite3.Error:
            return (0, 0.0, 0)


def rotate(
    conn: sqlite3.Connection | None = None,
    *,
    dry_run: bool = False,
    confirm: bool = False,
) -> tuple[int, int]:
    """Delete aged chunks only when REVIEW_LOG_ARCHIVE_RETAIN_DAYS_CHUNKS > 0 and confirm=True.

    daily_stats and other tables are never deleted by age. Default retain=0 means forever.
    """
    retain = _chunks_retain_days()
    if retain <= 0 or not confirm:
        return 0, 0
    db = conn or connect()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retain)
    ).strftime("%Y-%m-%d")
    with _LOCK:
        if dry_run:
            n_chunks = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE day < ?", (cutoff,)
            ).fetchone()[0]
            return int(n_chunks), 0
        cur = db.execute("DELETE FROM chunks WHERE day < ?", (cutoff,))
        n_chunks = cur.rowcount
        db.commit()
    return n_chunks, 0


def vacuum_archive(conn: sqlite3.Connection | None = None) -> None:
    db = conn or connect()
    with _LOCK:
        db.execute("VACUUM")
        db.execute("PRAGMA optimize")
        db.commit()


@dataclass
class _ChunkEntry:
    filename: str
    content: str
    mtime: float | None
    size: int


@dataclass
class _PendingChunk:
    day: str
    backend: str
    entries: list[_ChunkEntry]
    raw_bytes: int = 0

    def append(self, entry: _ChunkEntry) -> bool:
        self.entries.append(entry)
        self.raw_bytes += entry.size
        return (
            len(self.entries) >= _CHUNK_TARGET_CALLS
            or self.raw_bytes >= _CHUNK_TARGET_RAW_BYTES
        )


def _call_filename(stamp: str, backend: str, round_no: int) -> str:
    safe = (
        "".join(c if (c.isalnum() or c in "-_.") else "_" for c in backend) or "backend"
    )
    return f"{stamp}-{safe}-r{round_no}.log"


def _tokens_for_call(call: CallLog) -> tuple[int, int]:
    from .dashboard.tokenstats import extract_usage_tokens

    usage = extract_usage_tokens(call)
    if usage is None:
        return 0, 0
    return usage


def is_empty_stub(raw: str, size: int, call: CallLog | None) -> bool:
    if size > _EMPTY_MAX_BYTES:
        return False
    if call is None:
        return True
    return not bool(call.has_real_content)




def _upsert_call_meta(
    conn: sqlite3.Connection,
    *,
    filename: str,
    started: datetime,
    day: str,
    backend: str,
    round_no: int,
    task: str | None,
    exit_code: int | None,
    prompt_tokens: int,
    output_tokens: int,
    raw_bytes: int,
    is_empty: bool,
    has_error: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO call_meta(
          filename, started_unix, day, backend, round, task, exit,
          prompt_tokens, output_tokens, raw_bytes, is_empty, has_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
          started_unix = excluded.started_unix,
          day = excluded.day,
          backend = excluded.backend,
          round = excluded.round,
          task = excluded.task,
          exit = excluded.exit,
          prompt_tokens = excluded.prompt_tokens,
          output_tokens = excluded.output_tokens,
          raw_bytes = excluded.raw_bytes,
          is_empty = excluded.is_empty,
          has_error = excluded.has_error
        """,
        (
            filename,
            _started_unix_micro(started),
            day,
            backend,
            round_no,
            task,
            exit_code,
            prompt_tokens,
            output_tokens,
            raw_bytes,
            1 if is_empty else 0,
            1 if has_error else 0,
        ),
    )

def _update_daily_stats(
    conn: sqlite3.Connection,
    day: str,
    backend: str,
    *,
    n_calls: int,
    n_fail: int,
    prompt_tokens: int,
    output_tokens: int,
    raw_bytes: int,
    zstd_bytes: int,
) -> None:
    conn.execute(
        """
        INSERT INTO daily_stats(day, backend, n_calls, n_fail, prompt_tokens,
                                output_tokens, raw_bytes, zstd_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, backend) DO UPDATE SET
          n_calls = n_calls + excluded.n_calls,
          n_fail = n_fail + excluded.n_fail,
          prompt_tokens = prompt_tokens + excluded.prompt_tokens,
          output_tokens = output_tokens + excluded.output_tokens,
          raw_bytes = raw_bytes + excluded.raw_bytes,
          zstd_bytes = zstd_bytes + excluded.zstd_bytes
        """,
        (
            day,
            backend,
            n_calls,
            n_fail,
            prompt_tokens,
            output_tokens,
            raw_bytes,
            zstd_bytes,
        ),
    )


def _flush_pending(conn: sqlite3.Connection, pending: _PendingChunk) -> None:
    if not pending.entries:
        return
    payload_obj = [
        {
            "filename": e.filename,
            "content": e.content,
            "mtime": e.mtime,
            "size": e.size,
        }
        for e in pending.entries
    ]
    raw = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    compressed = compress_payload(raw, level=_CHUNK_ZSTD_LEVEL)
    min_ts = pending.entries[0].filename.split("-", 1)[0]
    max_ts = pending.entries[-1].filename.split("-", 1)[0]
    conn.execute(
        """
        INSERT OR REPLACE INTO chunks(day, backend, min_ts, max_ts, n_calls, payload_zstd)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            pending.day,
            pending.backend,
            min_ts,
            max_ts,
            len(pending.entries),
            compressed,
        ),
    )
    for entry in pending.entries:
        conn.execute(
            "INSERT OR IGNORE INTO call_files(filename) VALUES (?)",
            (entry.filename,),
        )
    n_fail = 0
    prompt_total = 0
    output_total = 0
    for entry in pending.entries:
        from .dashboard.parser import parse_call_log_text

        call = parse_call_log_text(entry.filename, entry.content, size=entry.size)
        if call is None:
            continue
        pt, ot = _tokens_for_call(call)
        prompt_total += pt
        output_total += ot
        if call.exit_code not in (0, None):
            n_fail += 1
        _upsert_call_meta(
            conn,
            filename=entry.filename,
            started=call.started,
            day=pending.day,
            backend=pending.backend,
            round_no=call.round,
            task=call.task_code,
            exit_code=call.exit_code,
            prompt_tokens=pt,
            output_tokens=ot,
            raw_bytes=entry.size,
            is_empty=False,
            has_error=bool(call.has_error),
        )
    _update_daily_stats(
        conn,
        pending.day,
        pending.backend,
        n_calls=len(pending.entries),
        n_fail=n_fail,
        prompt_tokens=prompt_total,
        output_tokens=output_total,
        raw_bytes=pending.raw_bytes,
        zstd_bytes=len(compressed),
    )


_PENDING: dict[tuple[str, str], _PendingChunk] = {}


def flush_pending_chunks(conn: sqlite3.Connection | None = None) -> None:
    db = conn or connect()
    with _LOCK:
        for key in list(_PENDING.keys()):
            pending = _PENDING.pop(key, None)
            if pending is not None:
                _flush_pending(db, pending)
        db.commit()


def store_call_raw(
    *,
    backend: str,
    round_no: int,
    started: datetime,
    content: str,
    mtime: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    from .dashboard.parser import parse_call_log_text

    stamp = _ts_key(started)
    filename = _call_filename(stamp, backend, round_no)
    if is_faketest_name(filename) or is_faketest_name(backend):
        return filename

    db = conn or connect()
    day = _day_key(started)
    raw_bytes = len(content.encode("utf-8"))
    call = parse_call_log_text(
        filename,
        content,
        size=raw_bytes,
        mtime=mtime,
    )
    with _LOCK:
        if is_empty_stub(content, raw_bytes, call):
            pt, ot = _tokens_for_call(call) if call else (0, 0)
            exit_code = call.exit_code if call else None
            task = call.task_code if call else None
            db.execute("INSERT OR IGNORE INTO call_files(filename) VALUES (?)", (filename,))
            db.execute(
                """
                INSERT OR REPLACE INTO empty_calls
                  (day, ts, backend, round, task, exit, prompt_tokens, output_tokens, is_empty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (day, stamp, backend, round_no, task, exit_code, pt, ot),
            )
            n_fail = 1 if exit_code not in (0, None) else 0
            _update_daily_stats(
                db,
                day,
                backend,
                n_calls=1,
                n_fail=n_fail,
                prompt_tokens=pt,
                output_tokens=ot,
                raw_bytes=raw_bytes,
                zstd_bytes=0,
            )
            _upsert_call_meta(
                db,
                filename=filename,
                started=started,
                day=day,
                backend=backend,
                round_no=round_no,
                task=task,
                exit_code=exit_code,
                prompt_tokens=pt,
                output_tokens=ot,
                raw_bytes=raw_bytes,
                is_empty=True,
                has_error=bool(call and call.has_error),
            )
            db.commit()
            return filename

        entry = _ChunkEntry(
            filename=filename,
            content=content,
            mtime=(mtime or started).timestamp() if mtime or started else None,
            size=raw_bytes,
        )
        key = (day, backend)
        pending = _PENDING.get(key)
        if pending is None:
            pending = _PendingChunk(day=day, backend=backend, entries=[])
            _PENDING[key] = pending
        if pending.append(entry):
            _flush_pending(db, pending)
            _PENDING.pop(key, None)
        db.commit()
    return filename


def store_brainstorm(
    *,
    started: datetime,
    topic: str,
    body: str,
    conn: sqlite3.Connection | None = None,
) -> str:
    if is_faketest_name(topic):
        return f"{_ts_key(started)}-brainstorm.md"

    db = conn or connect()
    day = _day_key(started)
    ts = _ts_key(started)
    compressed = compress_payload(body.encode("utf-8"), level=_BRAINSTORM_ZSTD_LEVEL)
    with _LOCK:
        db.execute(
            """
            INSERT INTO brainstorm(day, ts, topic, body_zstd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day, ts) DO UPDATE SET
              topic = excluded.topic,
              body_zstd = excluded.body_zstd
            """,
            (day, ts, topic, compressed),
        )
        db.commit()
    return f"{ts}-brainstorm.md"


def query_daily_stats(
    conn: sqlite3.Connection | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    db = conn or connect()
    with _LOCK:
        if since is not None:
            cutoff = _day_key(since)
            rows = db.execute(
                """
                SELECT day, backend, n_calls, n_fail, prompt_tokens,
                       output_tokens, raw_bytes, zstd_bytes
                FROM daily_stats
                WHERE day >= ?
                ORDER BY day, backend
                """,
                (cutoff,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT day, backend, n_calls, n_fail, prompt_tokens,
                       output_tokens, raw_bytes, zstd_bytes
                FROM daily_stats
                ORDER BY day, backend
                """
            ).fetchall()
    return [
        {
            "day": row[0],
            "backend": row[1],
            "n_calls": int(row[2]),
            "n_fail": int(row[3]),
            "prompt_tokens": int(row[4]),
            "output_tokens": int(row[5]),
            "raw_bytes": int(row[6]),
            "zstd_bytes": int(row[7]),
        }
        for row in rows
    ]


def load_calls_from_archive_since(
    since: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[CallLog]:
    from .dashboard.parser import parse_call_log_text

    db = conn or connect()
    calls: list[CallLog] = []
    cutoff = _day_key(since) if since is not None else None
    with _LOCK:
        if cutoff is not None:
            rows = db.execute(
                """
                SELECT day, backend, min_ts, max_ts, n_calls, payload_zstd
                FROM chunks
                WHERE day >= ?
                """,
                (cutoff,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT day, backend, min_ts, max_ts, n_calls, payload_zstd FROM chunks"
            ).fetchall()
    for _day, _backend, _min_ts, _max_ts, _n_calls, blob in rows:
        raw = decompress_payload(blob)
        entries = json.loads(raw.decode("utf-8"))
        for entry in entries:
            call = parse_call_log_text(
                entry["filename"],
                entry["content"],
                size=int(entry.get("size") or 0),
                mtime=(
                    datetime.fromtimestamp(entry["mtime"], tz=timezone.utc)
                    if entry.get("mtime")
                    else None
                ),
            )
            if call is not None:
                if since is not None and call.started < since:
                    continue
                calls.append(call)
    return calls


@dataclass
class PruneReport:
    filenames_removed: int = 0
    chunks_touched: int = 0
    empty_calls_removed: int = 0
    bytes_freed_estimate: int = 0


def _rebuild_daily_stats_for_day(conn: sqlite3.Connection, day: str) -> None:
    conn.execute("DELETE FROM daily_stats WHERE day = ?", (day,))
    rows = conn.execute(
        "SELECT backend, payload_zstd FROM chunks WHERE day = ?", (day,)
    ).fetchall()
    for backend, blob in rows:
        raw = decompress_payload(blob)
        entries = json.loads(raw.decode("utf-8"))
        raw_bytes = 0
        n_fail = 0
        prompt_total = 0
        output_total = 0
        for entry in entries:
            from .dashboard.parser import parse_call_log_text

            size = int(entry.get("size") or 0)
            raw_bytes += size
            call = parse_call_log_text(entry["filename"], entry["content"], size=size)
            if call is None:
                continue
            pt, ot = _tokens_for_call(call)
            prompt_total += pt
            output_total += ot
            if call.exit_code not in (0, None):
                n_fail += 1
        _update_daily_stats(
            conn,
            day,
            backend,
            n_calls=len(entries),
            n_fail=n_fail,
            prompt_tokens=prompt_total,
            output_tokens=output_total,
            raw_bytes=raw_bytes,
            zstd_bytes=len(blob),
        )
    empty_rows = conn.execute(
        """
        SELECT backend, exit, prompt_tokens, output_tokens
        FROM empty_calls
        WHERE day = ?
        """,
        (day,),
    ).fetchall()
    if empty_rows:
        by_backend: dict[str, dict[str, int]] = {}
        for backend, exit_code, pt, ot in empty_rows:
            bucket = by_backend.setdefault(
                backend,
                {
                    "n_calls": 0,
                    "n_fail": 0,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "raw_bytes": 0,
                },
            )
            bucket["n_calls"] += 1
            if exit_code not in (0, None):
                bucket["n_fail"] += 1
            bucket["prompt_tokens"] += int(pt)
            bucket["output_tokens"] += int(ot)
        for backend, bucket in by_backend.items():
            _update_daily_stats(
                conn,
                day,
                backend,
                n_calls=bucket["n_calls"],
                n_fail=bucket["n_fail"],
                prompt_tokens=bucket["prompt_tokens"],
                output_tokens=bucket["output_tokens"],
                raw_bytes=bucket["raw_bytes"],
                zstd_bytes=0,
            )


def prune_faketest_archive(
    conn: sqlite3.Connection | None = None,
    *,
    dry_run: bool = False,
) -> PruneReport:
    db = conn or connect()
    report = PruneReport()
    affected_days: set[str] = set()
    with _LOCK:
        chunk_rows = db.execute(
            "SELECT day, backend, min_ts, max_ts, n_calls, payload_zstd FROM chunks"
        ).fetchall()
        for day, backend, min_ts, max_ts, n_calls, blob in chunk_rows:
            raw = decompress_payload(blob)
            entries = json.loads(raw.decode("utf-8"))
            kept = [
                e
                for e in entries
                if not is_faketest_name(e["filename"]) and not is_faketest_name(backend)
            ]
            removed = len(entries) - len(kept)
            if removed == 0:
                continue
            report.filenames_removed += removed
            report.chunks_touched += 1
            report.bytes_freed_estimate += len(raw)
            affected_days.add(day)
            if dry_run:
                continue
            if not kept:
                db.execute(
                    "DELETE FROM chunks WHERE day = ? AND backend = ? AND min_ts = ?",
                    (day, backend, min_ts),
                )
            else:
                payload_obj = kept
                new_raw = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
                compressed = compress_payload(new_raw, level=_CHUNK_ZSTD_LEVEL)
                new_min = kept[0]["filename"].split("-", 1)[0]
                new_max = kept[-1]["filename"].split("-", 1)[0]
                db.execute(
                    """
                    UPDATE chunks
                    SET min_ts = ?, max_ts = ?, n_calls = ?, payload_zstd = ?
                    WHERE day = ? AND backend = ? AND min_ts = ?
                    """,
                    (
                        new_min,
                        new_max,
                        len(kept),
                        compressed,
                        day,
                        backend,
                        min_ts,
                    ),
                )
        empty_rows = db.execute(
            "SELECT day, ts, backend, round FROM empty_calls"
        ).fetchall()
        for day, ts, backend, rnd in empty_rows:
            fname = _empty_call_filename(ts, backend, rnd)
            if not is_faketest_name(fname) and not is_faketest_name(backend):
                continue
            report.filenames_removed += 1
            report.empty_calls_removed += 1
            affected_days.add(day)
            if dry_run:
                continue
            db.execute(
                "DELETE FROM empty_calls WHERE day = ? AND ts = ? AND backend = ? AND round = ?",
                (day, ts, backend, rnd),
            )
        if not dry_run:
            for table in ("call_meta", "call_files"):
                rows = db.execute(f"SELECT filename FROM {table}").fetchall()
                for (fname,) in rows:
                    if is_faketest_name(fname):
                        db.execute(
                            f"DELETE FROM {table} WHERE filename = ?",
                            (fname,),
                        )
            for day in affected_days:
                _rebuild_daily_stats_for_day(db, day)
            db.commit()
    return report


def backfill_call_meta(conn: sqlite3.Connection | None = None) -> int:
    from .dashboard.parser import parse_call_log_text

    db = conn or connect()
    inserted = 0
    with _LOCK:
        for chunk in load_chunk_entries(db):
            day = chunk["day"]
            backend = chunk["backend"]
            for entry in chunk["entries"]:
                call = parse_call_log_text(
                    entry["filename"],
                    entry["content"],
                    size=int(entry.get("size") or 0),
                )
                if call is None:
                    continue
                pt, ot = _tokens_for_call(call)
                cur = db.execute(
                    """
                    INSERT OR IGNORE INTO call_meta(
                      filename, started_unix, day, backend, round, task, exit,
                      prompt_tokens, output_tokens, raw_bytes, is_empty, has_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        entry["filename"],
                        _started_unix_micro(call.started),
                        day,
                        backend,
                        call.round,
                        call.task_code,
                        call.exit_code,
                        pt,
                        ot,
                        int(entry.get("size") or 0),
                        1 if call.has_error else 0,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
        rows = db.execute(
            """
            SELECT day, ts, backend, round, task, exit, prompt_tokens, output_tokens
            FROM empty_calls
            """
        ).fetchall()
        for day, ts, backend, rnd, task, exit_code, pt, ot in rows:
            filename = _empty_call_filename(ts, backend, rnd)
            stamp_part = ts.replace("_", "").replace("T", "").replace("Z", "")
            started = datetime.strptime(stamp_part[:20], "%Y%m%d%H%M%S%f").replace(
                tzinfo=timezone.utc
            )
            has_error = exit_code not in (0, None)
            cur = db.execute(
                """
                INSERT OR IGNORE INTO call_meta(
                  filename, started_unix, day, backend, round, task, exit,
                  prompt_tokens, output_tokens, raw_bytes, is_empty, has_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
                """,
                (
                    filename,
                    _started_unix_micro(started),
                    day,
                    backend,
                    rnd,
                    task,
                    exit_code,
                    pt,
                    ot,
                    1 if has_error else 0,
                ),
            )
            if cur.rowcount:
                inserted += 1
        db.commit()
    return inserted



def load_chunk_entries(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    db = conn or connect()
    out: list[dict[str, Any]] = []
    with _LOCK:
        rows = db.execute(
            "SELECT day, backend, min_ts, max_ts, n_calls, payload_zstd FROM chunks"
        ).fetchall()
    for day, backend, min_ts, max_ts, n_calls, blob in rows:
        raw = decompress_payload(blob)
        entries = json.loads(raw.decode("utf-8"))
        out.append(
            {
                "day": day,
                "backend": backend,
                "min_ts": min_ts,
                "max_ts": max_ts,
                "n_calls": n_calls,
                "entries": entries,
            }
        )
    return out


def load_calls_from_archive(
    conn: sqlite3.Connection | None = None,
) -> list[CallLog]:
    from .dashboard.parser import parse_call_log_text

    db = conn or connect()
    calls: list[CallLog] = []
    chunks = load_chunk_entries(db)
    for chunk in chunks:
        for entry in chunk["entries"]:
            call = parse_call_log_text(
                entry["filename"],
                entry["content"],
                size=int(entry.get("size") or 0),
                mtime=(
                    datetime.fromtimestamp(entry["mtime"], tz=timezone.utc)
                    if entry.get("mtime")
                    else None
                ),
            )
            if call is not None:
                calls.append(call)
    return calls


def load_brainstorms_from_archive(
    conn: sqlite3.Connection | None = None,
) -> list[BrainstormLog]:
    from .dashboard.parser import parse_brainstorm_log_text

    db = conn or connect()
    out: list[BrainstormLog] = []
    with _LOCK:
        rows = db.execute("SELECT ts, body_zstd FROM brainstorm").fetchall()
    for ts, blob in rows:
        body = decompress_payload(blob).decode("utf-8", errors="replace")
        filename = f"{ts}-brainstorm.md"
        item = parse_brainstorm_log_text(filename, body)
        if item is not None:
            out.append(item)
    return out


def _empty_call_filename(ts: str, backend: str, rnd: int) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in backend) or "backend"
    return f"{ts}-{safe}-r{rnd}.log"


def payload_filenames(conn: sqlite3.Connection | None = None) -> set[str]:
    """Authoritative archived call filenames from chunk payloads and empty_calls."""
    db = conn or connect()
    names: set[str] = set()
    for chunk in load_chunk_entries(db):
        for entry in chunk["entries"]:
            names.add(entry["filename"])
    with _LOCK:
        rows = db.execute("SELECT ts, backend, round FROM empty_calls").fetchall()
    for ts, backend, rnd in rows:
        names.add(_empty_call_filename(ts, backend, rnd))
    return names


def rebuild_call_files_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM call_files")
    for name in payload_filenames(conn):
        conn.execute(
            "INSERT OR IGNORE INTO call_files(filename) VALUES (?)",
            (name,),
        )
    conn.commit()


def archived_filenames(conn: sqlite3.Connection | None = None) -> set[str]:
    db = conn or connect()
    authoritative = payload_filenames(db)
    with _LOCK:
        indexed = {
            row[0] for row in db.execute("SELECT filename FROM call_files").fetchall()
        }
    if indexed == authoritative:
        return authoritative
    rebuild_call_files_index(db)
    return authoritative



def archive_counts(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    db = conn or connect()
    with _LOCK:
        n_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_calls = db.execute("SELECT COALESCE(SUM(n_calls), 0) FROM chunks").fetchone()[
            0
        ]
        n_empty = db.execute("SELECT COUNT(*) FROM empty_calls").fetchone()[0]
        n_brain = db.execute("SELECT COUNT(*) FROM brainstorm").fetchone()[0]
        n_meta = db.execute("SELECT COUNT(*) FROM call_meta").fetchone()[0]
    return {
        "chunks": int(n_chunks),
        "chunk_calls": int(n_calls),
        "empty_calls": int(n_empty),
        "brainstorm": int(n_brain),
        "call_meta": int(n_meta),
    }


def archive_db_size() -> int:
    path = archive_db_path()
    if not path.exists():
        return 0
    total = path.stat().st_size
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            total += side.stat().st_size
    return total
