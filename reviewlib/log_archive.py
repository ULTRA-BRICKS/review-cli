"""SQLite-backed durable archive for finished review-cli call logs (review-cli#480).

WHY: the live log dir grew to ~154k one-file-per-call artifacts / 4.4GB+; every
dashboard cold start and `review stat` paid O(total historical file count) to list,
stat and parse it. Finished call logs now move into ONE SQLite file per log dir
(`<log_dir>/logs-archive.sqlite3`), stored as compressed ~1000-call chunks keyed by
(day, backend), so the directory itself only holds calls that are still streaming.

Lifecycle of one call (see `process._run_streamed` / `process.write_sidecar_log`):

  * While a call streams it still writes its own `<stamp>-<backend>-r<N>.log` file, so
    `tail -f`, the dashboard's live SSE view and the crash-safety of a line-buffered
    file are unchanged.
  * When it finishes, `store_call` commits the full text DURABLY as one row of
    `pending_calls` (one transaction, no in-process buffering — a process that exits
    right after its last call loses nothing), and only then is the file unlinked.
  * `pending_calls` rows of one (day, backend) group are SEALED into a compressed
    `chunks` row once the group reaches `_CHUNK_TARGET_CALLS` calls /
    `_CHUNK_TARGET_RAW_BYTES` raw bytes, or once a later day's call is stored (the day
    is over). Sealing moves rows; it never changes what `load_calls` returns.

The archive lives INSIDE the log dir it belongs to, so `load_sessions(log_dir)` reads
exactly that directory's history (tests with a fresh REVIEW_LOG_DIR are isolated by
construction) and the dashboard's `.log`/`.md`-only directory scan ignores it, like the
call-log parse cache that already lives there.

Brainstorm discussion logs stay on disk: they are the resume substrate of
`review sessions` (reviewlib/sessions.py). The migration copies them into the archive
as a durable backup; the dashboard prefers the on-disk file when both exist.

Every connection is opened per operation (no shared module-level handle): the
dashboard serves requests from many threads and several `review` processes write
concurrently, so writers serialize on SQLite's own lock (`BEGIN IMMEDIATE`, 30s busy
timeout) instead of a process-local mutex. The cost is small: `archive_signature`
(connect + three aggregate queries + close, what the dashboard's 1Hz SSE poll pays)
measured ~0.35ms on a 357-call archive.

No retention: the archive keeps every call, because `load_sessions(days=0)` /
`review task` / `review stat --days 0` promise all history. What it bounds is the
file COUNT the log dir accumulates and the read cost: a windowed read decompresses only
in-window chunks. Total size still grows with history, compressed (a real 356-call dir
went from 5.7MB of files to a 2.2MB archive). Pruning old history is a separate
policy decision, not part of moving logs into the archive.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import zlib
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:
    from .dashboard.parser import BrainstormLog, CallLog

ARCHIVE_FILENAME = "logs-archive.sqlite3"

_CHUNK_TARGET_CALLS = 1000
_CHUNK_TARGET_RAW_BYTES = 1_000_000
# Stdlib zlib only: an optional codec (zstandard) would make an archive written on one
# machine unreadable on another that lacks the module.
_ZLIB_LEVEL = 9
_BUSY_TIMEOUT_SECONDS = 30.0
# Stored in `PRAGMA user_version` once `_DDL` has run; bump when the schema changes.
_SCHEMA_VERSION = 2

# One statement per entry: `sqlite3.Connection.execute` runs exactly one statement.
_DDL = (
    "PRAGMA journal_mode=WAL",
    """CREATE TABLE IF NOT EXISTS chunks (
      day TEXT NOT NULL,
      backend TEXT NOT NULL,
      min_ts TEXT NOT NULL,
      max_ts TEXT NOT NULL,
      max_mtime REAL NOT NULL,
      n_calls INTEGER NOT NULL,
      raw_bytes INTEGER NOT NULL,
      payload BLOB NOT NULL,
      PRIMARY KEY (day, backend, min_ts)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS idx_chunks_max_ts ON chunks(max_ts)",
    """CREATE TABLE IF NOT EXISTS pending_calls (
      filename TEXT PRIMARY KEY,
      day TEXT NOT NULL,
      backend TEXT NOT NULL,
      ts TEXT NOT NULL,
      size INTEGER NOT NULL,
      mtime REAL NOT NULL,
      content BLOB NOT NULL
    ) WITHOUT ROWID""",
    # Covering (size included): `_seal_due` counts and sums a group on every insert.
    "DROP INDEX IF EXISTS idx_pending_group",
    "CREATE INDEX IF NOT EXISTS idx_pending_group_size ON pending_calls(day, backend, size)",
    "CREATE INDEX IF NOT EXISTS idx_pending_ts ON pending_calls(ts)",
    # Every call filename ever archived (pending or sealed): idempotency for a retried
    # archive (a crash between commit and unlink) and the migration's proof A.
    "CREATE TABLE IF NOT EXISTS call_files (filename TEXT PRIMARY KEY) WITHOUT ROWID",
    """CREATE TABLE IF NOT EXISTS brainstorm (
      filename TEXT PRIMARY KEY,
      day TEXT NOT NULL,
      ts TEXT NOT NULL,
      topic TEXT NOT NULL,
      mtime REAL NOT NULL,
      body BLOB NOT NULL
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_ts ON brainstorm(ts)",
)


def archive_db_path(log_dir: Path) -> Path:
    return Path(log_dir) / ARCHIVE_FILENAME


def use_sqlite_logs() -> bool:
    """Whether FINISHED call logs move into the archive (default on). Read paths always
    consult an existing archive regardless, so turning this off never hides history."""
    raw = os.environ.get("REVIEW_LOG_SQLITE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def compress_payload(data: bytes) -> bytes:
    return zlib.compress(data, level=_ZLIB_LEVEL)


def decompress_payload(data: bytes) -> bytes:
    return zlib.decompress(data)


def connect(log_dir: Path, *, create: bool = True) -> sqlite3.Connection | None:
    """A NEW autocommit connection to `log_dir`'s archive. Whoever calls `connect` owns
    and closes the connection; functions that accept `conn=` use the caller's connection
    and leave it open.

    `create=False` is the read path: returns None when no archive exists yet instead of
    creating an empty database in whatever directory a reader was pointed at. The schema
    DDL runs only when `PRAGMA user_version` says this file hasn't been initialised yet,
    so a per-call writer pays one pragma read, not a dozen `IF NOT EXISTS` lookups."""
    path = archive_db_path(log_dir)
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Private like the logs themselves: the archive holds prompts/diffs.
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
    conn = sqlite3.connect(
        str(path),
        timeout=_BUSY_TIMEOUT_SECONDS,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        if (
            create
            and conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION
        ):
            for stmt in _DDL:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def immediate_transaction(db: sqlite3.Connection) -> Iterator[None]:
    """`BEGIN IMMEDIATE` ... `COMMIT`, rolled back on any exception. IMMEDIATE takes the
    write lock up front, so concurrent writers queue on the busy timeout instead of
    failing mid-transaction on a lock upgrade."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _day_of_stamp(stamp: str) -> str:
    """`20260610T120000_000000Z` -> `2026-06-10` (the UTC day of a filename stamp)."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _stamp_of(filename: str) -> str:
    return filename.split("-", 1)[0]


def _seal_group(db: sqlite3.Connection, day: str, backend: str) -> None:
    """Move every pending row of (day, backend) into chunk rows of at most the target
    size. Runs inside the caller's write transaction."""
    rows = db.execute(
        "SELECT filename, size, mtime, content FROM pending_calls "
        "WHERE day = ? AND backend = ? ORDER BY filename",
        (day, backend),
    ).fetchall()
    batch: list[dict[str, Any]] = []
    batch_bytes = 0

    def _emit() -> None:
        raw = json.dumps(batch, ensure_ascii=False).encode("utf-8")
        compressed = compress_payload(raw)
        db.execute(
            "INSERT INTO chunks(day, backend, min_ts, max_ts, max_mtime, n_calls, "
            "raw_bytes, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                backend,
                _stamp_of(batch[0]["filename"]),
                _stamp_of(batch[-1]["filename"]),
                max(e["mtime"] for e in batch),
                len(batch),
                batch_bytes,
                compressed,
            ),
        )

    for filename, size, mtime, blob in rows:
        content = decompress_payload(blob).decode("utf-8")
        batch.append(
            {"filename": filename, "content": content, "mtime": mtime, "size": size}
        )
        batch_bytes += size
        if len(batch) >= _CHUNK_TARGET_CALLS or batch_bytes >= _CHUNK_TARGET_RAW_BYTES:
            _emit()
            batch, batch_bytes = [], 0
    if batch:
        _emit()
    db.execute(
        "DELETE FROM pending_calls WHERE day = ? AND backend = ?", (day, backend)
    )


def _seal_due(db: sqlite3.Connection, day: str, backend: str) -> None:
    """After inserting a call of (day, backend): seal every group of an EARLIER day (that
    day is over) and this call's own group if it reached the chunk target. Indexed
    lookups only (`idx_pending_group_size`), no scan of every pending group."""
    for old_day, old_backend in db.execute(
        "SELECT DISTINCT day, backend FROM pending_calls WHERE day < ?", (day,)
    ).fetchall():
        _seal_group(db, old_day, old_backend)
    n, total = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM pending_calls "
        "WHERE day = ? AND backend = ?",
        (day, backend),
    ).fetchone()
    if n >= _CHUNK_TARGET_CALLS or total >= _CHUNK_TARGET_RAW_BYTES:
        _seal_group(db, day, backend)


def _insert_call(
    db: sqlite3.Connection, filename: str, content: str, mtime: float
) -> bool:
    """Insert one finished call into `pending_calls`, then seal whatever became due.
    False if `filename` is already archived; ValueError if it is not a call-log name.
    Runs inside the caller's transaction."""
    from .dashboard.parser import _CALL_RE

    m = _CALL_RE.match(filename)
    if m is None:
        raise ValueError(f"not a call-log filename: {filename!r}")
    if db.execute(
        "SELECT 1 FROM call_files WHERE filename = ?", (filename,)
    ).fetchone():
        return False
    stamp = _stamp_of(filename)
    day = _day_of_stamp(stamp)
    backend = m.group(3)
    data = content.encode("utf-8")
    db.execute("INSERT INTO call_files(filename) VALUES (?)", (filename,))
    db.execute(
        "INSERT INTO pending_calls(filename, day, backend, ts, size, mtime, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (filename, day, backend, stamp, len(data), mtime, compress_payload(data)),
    )
    _seal_due(db, day, backend)
    return True


def store_call(
    log_dir: Path,
    filename: str,
    content: str,
    *,
    mtime: float,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Durably archive one FINISHED call log (committed before this returns). Returns
    False when `filename` was already archived (idempotent: the archive holds it either
    way). Raises ValueError for a non-call-log name and sqlite3/OS errors on storage
    failure — the caller keeps the on-disk file then, so nothing is lost."""
    own = conn is None
    db = conn if conn is not None else connect(log_dir)
    assert db is not None
    try:
        with immediate_transaction(db):
            return _insert_call(db, filename, content, mtime)
    finally:
        if own:
            db.close()


def seal_all(log_dir: Path, *, conn: sqlite3.Connection | None = None) -> None:
    """Seal every pending group now, whatever its size or day."""
    own = conn is None
    db = conn if conn is not None else connect(log_dir)
    assert db is not None
    try:
        with immediate_transaction(db):
            for day, backend in db.execute(
                "SELECT DISTINCT day, backend FROM pending_calls"
            ).fetchall():
                _seal_group(db, day, backend)
    finally:
        if own:
            db.close()


def store_brainstorm(
    log_dir: Path,
    filename: str,
    body: str,
    *,
    topic: str,
    mtime: float,
    conn: sqlite3.Connection | None = None,
) -> None:
    own = conn is None
    db = conn if conn is not None else connect(log_dir)
    assert db is not None
    stamp = _stamp_of(filename)
    try:
        db.execute(
            """
            INSERT INTO brainstorm(filename, day, ts, topic, mtime, body)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET
              topic = excluded.topic,
              mtime = excluded.mtime,
              body = excluded.body
            """,
            (
                filename,
                _day_of_stamp(stamp),
                stamp,
                topic,
                mtime,
                compress_payload(body.encode("utf-8")),
            ),
        )
    finally:
        if own:
            db.close()


# ---- read side ---------------------------------------------------------------------------
# Sealed chunks and pending rows are immutable once committed, so their parsed CallLogs are
# memoised per archive path: a warm dashboard reload re-parses only rows it has never seen
# (the archive analogue of `call_log_cache` for on-disk files). The memo holds exactly the
# rows of the latest WINDOWED load (the dashboard's `days` view, the same set its session
# cache already keeps alive), so it is bounded by that window, not by history. An
# unbounded load (`since=None`: `review task`, a feedback POST's existence check) reuses
# the memo but never stores into it, so it neither evicts the window's rows nor pins all
# of history in memory. A chunk that can't be decoded is skipped with a stderr warning
# rather than failing every reader of the directory.
_PARSE_MEMO: dict[str, dict[tuple, list[CallLog]]] = {}
_PARSE_MEMO_LOCK = threading.Lock()


def _undecodable(what: str, exc: Exception) -> list:
    print(
        f"[review-cli] log archive: skipping undecodable {what} ({exc})",
        file=sys.stderr,
    )
    return []


def _window_clause(column: str, since: str | None) -> tuple[str, tuple]:
    if since is None:
        return "", ()
    return f" WHERE {column} >= ?", (since,)


def _in_window(filename: str, since: str | None) -> bool:
    # Same predicate as `_scan_log_dir_entries`'s `_windowed`: compare the 15-char
    # `YYYYMMDDTHHMMSS` prefix against the cutoff string.
    return since is None or filename[: len(since)] >= since


def _memoised(
    memo: dict[tuple, list[CallLog]],
    seen: dict[tuple, list[CallLog]],
    key: tuple,
    produce: Callable[[], list[CallLog]],
) -> list[CallLog]:
    # `memo` is this load's private snapshot of `_PARSE_MEMO`; no lock needed here.
    hit = memo.get(key)
    if hit is None:
        hit = produce()
    seen[key] = hit
    return hit


def load_calls(log_dir: Path, since: str | None = None) -> list[CallLog]:
    """Every archived call whose filename stamp is at or after `since` (a
    `YYYYMMDDTHHMMSS` cutoff; None = all history). Chunks entirely before the cutoff are
    never read or decompressed."""
    from .dashboard.parser import parse_call_log_text

    path = archive_db_path(log_dir)
    try:
        conn = connect(log_dir, create=False)
    except sqlite3.Error:
        return []
    if conn is None:
        return []

    def _parse(
        filename: str, content: str, size: int, mtime: float | None
    ) -> list[CallLog]:
        call = parse_call_log_text(
            filename,
            content,
            size=size,
            mtime=datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else None,
            path=f"{path}#{filename}",
        )
        return [call] if call is not None else []

    with _PARSE_MEMO_LOCK:
        memo = dict(_PARSE_MEMO.get(str(path), {}))
    seen: dict[tuple, list[CallLog]] = {}
    calls: list[CallLog] = []
    with closing(conn):
        try:
            # One read transaction = one WAL snapshot for every query below, so a group
            # sealed concurrently (pending rows -> chunk) is seen entirely on one side.
            conn.execute("BEGIN")
            where, args = _window_clause("max_ts", since)
            chunk_keys = conn.execute(
                f"SELECT day, backend, min_ts FROM chunks{where}", args
            ).fetchall()
            for day, backend, min_ts in chunk_keys:

                def _parse_chunk(
                    day=day, backend=backend, min_ts=min_ts
                ) -> list[CallLog]:
                    (blob,) = conn.execute(
                        "SELECT payload FROM chunks WHERE day = ? AND backend = ? "
                        "AND min_ts = ?",
                        (day, backend, min_ts),
                    ).fetchone()
                    try:
                        entries = json.loads(decompress_payload(blob).decode("utf-8"))
                    except (zlib.error, ValueError) as exc:
                        return _undecodable(f"chunk {day}/{backend}/{min_ts}", exc)
                    out: list[CallLog] = []
                    for e in entries:
                        out.extend(
                            _parse(e["filename"], e["content"], e["size"], e["mtime"])
                        )
                    return out

                for call in _memoised(
                    memo, seen, ("chunk", day, backend, min_ts), _parse_chunk
                ):
                    if _in_window(call.filename, since):
                        calls.append(call)
            where, args = _window_clause("ts", since)
            pending = conn.execute(
                f"SELECT filename FROM pending_calls{where}", args
            ).fetchall()
            for (filename,) in pending:

                def _parse_pending(filename=filename) -> list[CallLog]:
                    size, mtime, blob = conn.execute(
                        "SELECT size, mtime, content FROM pending_calls WHERE filename = ?",
                        (filename,),
                    ).fetchone()
                    try:
                        content = decompress_payload(blob).decode("utf-8")
                    except (zlib.error, ValueError) as exc:
                        return _undecodable(f"call {filename}", exc)
                    return _parse(filename, content, size, mtime)

                calls.extend(
                    _memoised(memo, seen, ("pending", filename), _parse_pending)
                )
            conn.execute("COMMIT")
        except sqlite3.Error:
            return calls
    if since is not None:
        with _PARSE_MEMO_LOCK:
            _PARSE_MEMO[str(path)] = seen
    return calls


def load_brainstorms(log_dir: Path, since: str | None = None) -> list[BrainstormLog]:
    from .dashboard.parser import parse_brainstorm_log_text

    try:
        conn = connect(log_dir, create=False)
    except sqlite3.Error:
        return []
    if conn is None:
        return []
    path = archive_db_path(log_dir)
    out: list[BrainstormLog] = []
    with closing(conn):
        try:
            where, args = _window_clause("ts", since)
            rows = conn.execute(
                f"SELECT filename, mtime, body FROM brainstorm{where}", args
            ).fetchall()
        except sqlite3.Error:
            return []
    for filename, mtime, blob in rows:
        try:
            body = decompress_payload(blob).decode("utf-8", errors="replace")
        except zlib.error as exc:
            _undecodable(f"brainstorm {filename}", exc)
            continue
        item = parse_brainstorm_log_text(
            filename,
            body,
            mtime=datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else None,
            path=f"{path}#{filename}",
        )
        if item is not None:
            out.append(item)
    return out


def newest_stamp(log_dir: Path) -> str | None:
    """The newest filename stamp of any archived call or brainstorm, or None. Feeds the
    dashboard window anchor so archived history and on-disk files share one cutoff."""
    try:
        conn = connect(log_dir, create=False)
    except sqlite3.Error:
        return None
    if conn is None:
        return None
    with closing(conn):
        try:
            row = conn.execute(
                "SELECT MAX(s) FROM ("
                " SELECT MAX(max_ts) AS s FROM chunks"
                " UNION ALL SELECT MAX(ts) FROM pending_calls"
                " UNION ALL SELECT MAX(ts) FROM brainstorm)"
            ).fetchone()
        except sqlite3.Error:
            return None
    return row[0] if row else None


def archive_signature(log_dir: Path) -> tuple[int, float]:
    """(archived call + brainstorm count, newest archived mtime): cheap aggregate queries,
    no payload reads. (0, 0.0) when there is no archive. The archive is append-only, so
    any new row moves the count."""
    try:
        conn = connect(log_dir, create=False)
    except sqlite3.Error:
        return (0, 0.0)
    if conn is None:
        return (0, 0.0)
    with closing(conn):
        try:
            n_chunk, m_chunk = conn.execute(
                "SELECT COALESCE(SUM(n_calls), 0), COALESCE(MAX(max_mtime), 0) FROM chunks"
            ).fetchone()
            n_pending, m_pending = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(mtime), 0) FROM pending_calls"
            ).fetchone()
            n_brain, m_brain = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(mtime), 0) FROM brainstorm"
            ).fetchone()
        except sqlite3.Error:
            return (0, 0.0)
    return (
        int(n_chunk) + int(n_pending) + int(n_brain),
        float(max(m_chunk, m_pending, m_brain)),
    )


# ---- migration / verification helpers -----------------------------------------------------


def iter_archived_entries(conn: sqlite3.Connection, day: str | None = None):
    """Yield `(filename, content, mtime, size)` for every archived call (sealed and
    pending), optionally restricted to one UTC `day`."""
    where = " WHERE day = ?" if day is not None else ""
    args: tuple = (day,) if day is not None else ()
    for (blob,) in conn.execute(f"SELECT payload FROM chunks{where}", args).fetchall():
        for e in json.loads(decompress_payload(blob).decode("utf-8")):
            yield e["filename"], e["content"], e["mtime"], e["size"]
    for filename, size, mtime, blob in conn.execute(
        f"SELECT filename, size, mtime, content FROM pending_calls{where}", args
    ).fetchall():
        yield filename, decompress_payload(blob).decode("utf-8"), mtime, size


def archived_filenames(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT filename FROM call_files")}


def archive_counts(conn: sqlite3.Connection) -> dict[str, int]:
    (n_chunks, n_chunk_calls) = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n_calls), 0) FROM chunks"
    ).fetchone()
    (n_pending,) = conn.execute("SELECT COUNT(*) FROM pending_calls").fetchone()
    (n_brain,) = conn.execute("SELECT COUNT(*) FROM brainstorm").fetchone()
    return {
        "chunks": int(n_chunks),
        "chunk_calls": int(n_chunk_calls),
        "pending_calls": int(n_pending),
        "brainstorm": int(n_brain),
    }


def archive_db_size(log_dir: Path) -> int:
    path = archive_db_path(log_dir)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            total += side.stat().st_size
    return total
