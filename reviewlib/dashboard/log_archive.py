"""SQLite log-archive core: compressed, chunked store for review-cli call logs.

WHY: the live log dir (`~/Library/Logs/review-cli`) grows unboundedly (~113k call
logs / 4.4GB observed): every `review stat` and every dashboard cold start re-parses
tens of thousands of small files, and the old derived-data cache
(`.call-log-cache.sqlite3`, 7.3GB) duplicated full bodies without ever shrinking the
source dir. This module is the Phase-1 core of the replacement: per-call records are
packed as JSONL, compressed (zstd when available, else zlib), and stored in ~1000-row
chunks keyed by (day, backend), so a bounded window reads a handful of rows instead
of opening thousands of files.

SCOPE (Phase 1): this module + the parser text-core + `scripts/migrate-log-archive.py`
+ hermetic tests. Writers (`process.py`), readers (`parser.load_sessions`,
`tokenstats.list_call_logs`) and the dashboard server switch over in Phase 2 — nothing
here is on any hot path yet.

DESIGN:
  * `chunks` holds NON-EMPTY calls only (a chunk = one compressed JSONL blob of full
    record dicts, `v: 1`). Trivial EXIT-0 calls (~39k of the real corpus: `ok`, `x`,
    fixture leakage) are scalar rows in `empty_calls` — no body to keep, and
    `classify_call` is exactly reproducible from the 7 stored classification fields
    with `body=''` (all its inputs are scalars except a length-gated body regex that
    only fires on real content; an empty-classified body has none).
  * `staging` buffers live-recorded calls until a partition reaches a full chunk;
    `seal_all` forces everything (migration end, shutdown).
  * `daily_stats` is a maintained rollup per (day, backend): n_calls / n_fail /
    prompt+output tokens / raw+zstd bytes. Recomputed for touched partitions only.
  * `migrated_files` makes migration idempotent (filename -> kind).
  * `meta` holds `schema_version`, `codec`, `last_prune`, `last_migration` receipt.
  * ALL write entry points are best-effort: they catch everything and return False /
    empty on failure — log loss beats a broken review. Proofs are the opposite: they
    return `{'ok': bool, 'detail': {...}}` with NUMBERS, never bare booleans.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import BrainstormLog, CallLog

ARCHIVE_FILENAME = "logs-archive.sqlite3"
SCHEMA_VERSION = "1"

# Chunking thresholds: a chunk holds records up to BOTH limits. Module-level (not
# literals) so tests can monkeypatch small values to force multi-chunk packing.
CHUNK_MAX_RECORDS = 1000
CHUNK_MAX_BYTES = 1_000_000

# Compression levels. Chunk payloads (already-JSONL text) use 6 (speed/size sweet
# spot at 100k-record scale); standalone brainstorm bodies use 9 (few, small, cold).
CHUNK_COMPRESS_LEVEL = 6
BRAINSTORM_COMPRESS_LEVEL = 9

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

_DDL = """
CREATE TABLE chunks(day TEXT NOT NULL, backend TEXT NOT NULL, min_ts TEXT NOT NULL,
  max_ts TEXT NOT NULL, n_calls INTEGER NOT NULL, payload_zstd BLOB NOT NULL,
  PRIMARY KEY(day,backend,min_ts)) WITHOUT ROWID, STRICT;
CREATE INDEX idx_chunks_day_backend ON chunks(day, backend);
CREATE TABLE staging(day TEXT NOT NULL, backend TEXT NOT NULL, ts TEXT NOT NULL,
  record_json TEXT NOT NULL) STRICT;
CREATE INDEX idx_staging_day_backend ON staging(day, backend);
CREATE TABLE empty_calls(day TEXT NOT NULL, ts TEXT NOT NULL, backend TEXT NOT NULL,
  round INTEGER NOT NULL, task TEXT, argv0 TEXT NOT NULL, exit_code INTEGER,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  timed_out INTEGER NOT NULL DEFAULT 0, true_silenced INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL, has_error INTEGER NOT NULL, error_summary TEXT,
  is_paywall INTEGER NOT NULL DEFAULT 0, is_cf_blocked INTEGER NOT NULL DEFAULT 0,
  is_bad_key INTEGER NOT NULL DEFAULT 0, has_real_content INTEGER NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL DEFAULT 0, filename TEXT NOT NULL) STRICT;
CREATE INDEX idx_empty_day_backend ON empty_calls(day, backend);
CREATE TABLE brainstorm(day TEXT NOT NULL, ts TEXT NOT NULL, topic TEXT NOT NULL,
  panel TEXT NOT NULL DEFAULT '', moderator TEXT, task TEXT,
  filename TEXT NOT NULL UNIQUE, body_zstd BLOB NOT NULL) STRICT;
CREATE INDEX idx_brainstorm_day ON brainstorm(day);
CREATE TABLE daily_stats(day TEXT NOT NULL, backend TEXT NOT NULL, n_calls INTEGER NOT NULL,
  n_fail INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  raw_bytes INTEGER NOT NULL, zstd_bytes INTEGER NOT NULL,
  PRIMARY KEY(day,backend)) WITHOUT ROWID, STRICT;
CREATE TABLE migrated_files(filename TEXT PRIMARY KEY, kind TEXT NOT NULL) WITHOUT ROWID, STRICT;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID, STRICT;
"""

# ---- empty-call rule ----------------------------------------------------------
# ANSI escape stripper: a stderr line counts as blank when nothing remains after
# removing CSI sequences + whitespace.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Usage trailer lines are non-content (both spellings review-cli emits).
_USAGE_LINE_RE = re.compile(r"^((prompt|input)_tokens=\d+\s*)?(output_tokens=\d+\s*)?$")
# Trivial-body budget: at most this many surviving lines / chars of stripped text.
_EMPTY_MAX_LINES = 3
_EMPTY_MAX_CHARS = 140


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def is_empty_call(call: "CallLog", size_bytes: int) -> bool:  # noqa: ARG001
    """True when `call` is a trivial EXIT-0 call not worth a chunk payload slot.

    ALL must hold: exit 0, not timed out / true-silenced, completed, no
    paywall/block/auth/error signal, every stderr line blank (after ANSI + whitespace
    stripping), and a trivial body (blank lines, `[review-cli]…` framing lines and
    usage trailer lines dropped; what remains is <= 3 lines and <= 140 stripped
    chars total).

    `size_bytes` is accepted for call-site symmetry with the parser stat tuple (and
    as a future guard rail) — the rule itself is content-based, not size-based.
    """
    if call.exit_code != 0:
        return False
    if call.timed_out or call.true_silenced:
        return False
    if not call.completed:
        return False
    if call.is_paywall or call.is_cf_blocked or call.is_bad_key or call.has_error:
        return False
    for line in call.stderr_lines or []:
        if _strip_ansi(line).strip():
            return False
    surviving: list[str] = []
    for raw_line in (call.body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[review-cli]"):
            continue
        if _USAGE_LINE_RE.match(line):
            continue
        surviving.append(line)
    if len(surviving) > _EMPTY_MAX_LINES:
        return False
    return sum(len(line) for line in surviving) <= _EMPTY_MAX_CHARS


# ---- codec --------------------------------------------------------------------
# Selection is resolved LAZILY (per call, via normal imports) so tests can
# monkeypatch-hide `zstandard`/`compression.zstd` to exercise the zlib fallback, and
# so an install without zstandard still imports this module fine.


def _zstd_compress(raw: bytes, level: int) -> bytes | None:
    try:
        import zstandard as zstd_mod

        return zstd_mod.ZstdCompressor(level=level).compress(raw)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- fall through to the next codec
        pass
    try:
        from compression import zstd as zstd_pkg

        compress_fn = getattr(zstd_pkg, "compress", None)
        if compress_fn is not None:
            try:
                return compress_fn(raw, level=level)
            except TypeError:
                return compress_fn(raw)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- fall through to zlib
        pass
    return None


def _zstd_decompress(blob: bytes) -> bytes:
    try:
        import zstandard as zstd_mod

        return zstd_mod.ZstdDecompressor().decompress(blob)
    except ImportError:
        pass
    from compression import zstd as zstd_pkg  # raises ImportError when absent

    decompress_fn = getattr(zstd_pkg, "decompress", None)
    if decompress_fn is None:
        raise RuntimeError("no zstd decompressor available")
    return decompress_fn(blob)


def compress_blob(raw: bytes, level: int) -> bytes:
    """Compress `raw` with zstd (level honored) when available, else zlib level 9."""
    encoded = _zstd_compress(raw, level)
    if encoded is not None:
        return encoded
    zlevel = level if 0 <= level <= 9 else 9
    return zlib.compress(raw, zlevel)


def decompress_blob(blob: bytes) -> bytes:
    """Decompress sniffing the magic: zstd frame (`28 b5 2f fd`) vs zlib (`78 …`)."""
    if bytes(blob[:4]) == _ZSTD_MAGIC:
        return _zstd_decompress(bytes(blob))
    return zlib.decompress(bytes(blob))


def codec_name() -> str:
    """The encoder `compress_blob` will use on this machine (for reporting)."""
    if _zstd_compress(b"", CHUNK_COMPRESS_LEVEL) is not None:
        return "zstd"
    return "zlib"


# ---- paths + connects -----------------------------------------------------------


def archive_path_for(log_dir: Path | None) -> Path:
    """Resolve the archive db path: `$REVIEW_ARCHIVE_DB` wins; then the given
    `log_dir`, then `$REVIEW_LOG_DIR` (hermetic tests); else the XDG-state default."""
    override = os.environ.get("REVIEW_ARCHIVE_DB")
    if override:
        return Path(override)
    if log_dir is not None:
        return Path(log_dir) / ARCHIVE_FILENAME
    env_dir = os.environ.get("REVIEW_LOG_DIR")
    if env_dir:
        return Path(env_dir) / ARCHIVE_FILENAME
    return Path.home() / ".local" / "state" / "review-cli" / ARCHIVE_FILENAME


def connect(db: Path) -> sqlite3.Connection:
    """Open (creating, 0600) the archive db: WAL + 8K pages, then the DDL."""
    db = Path(db)
    if db.parent and str(db.parent):
        try:
            db.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    conn = sqlite3.connect(str(db), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("PRAGMA page_size=8192")
    except sqlite3.Error:
        pass  # page_size is fixed once the db exists; harmless on reopen
    conn.executescript(_DDL if _is_fresh(conn) else "")
    _ensure_meta_defaults(conn)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(f"{db}{suffix}", 0o600)
        except OSError:
            pass  # best-effort, matches the other swallowed-chmod sites in this repo
    return conn


def _is_fresh(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
    except sqlite3.Error:
        return True
    return row is None


def _ensure_meta_defaults(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(k, v) VALUES ('codec', ?)",
            (codec_name(),),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    try:
        conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        conn.commit()
    except sqlite3.Error:
        pass


# Module-level default connections for the conn-less live API (`record_call` /
# `record_brainstorm` mirror the process-global `log_dir()` style: the writer knows
# no db path). Keyed by resolved path; best-effort throughout.
_default_lock = threading.Lock()
_default_conns: dict[str, sqlite3.Connection | None] = {}


def _default_conn() -> sqlite3.Connection | None:
    path = str(archive_path_for(None))
    with _default_lock:
        if path in _default_conns:
            return _default_conns[path]
        try:
            conn = connect(Path(path))
        except Exception:  # noqa: BLE001 -- never break a review over the archive
            conn = None
        _default_conns[path] = conn
        return conn


def file_logs_enabled() -> bool:
    """Whether the legacy per-file logs are still being written (`$REVIEW_FILE_LOGS`)."""
    return os.environ.get("REVIEW_FILE_LOGS") == "1"


# ---- chunk records ---------------------------------------------------------------


def _day_of(call: "CallLog") -> str:
    return call.started.date().isoformat()


def call_to_record(call: "CallLog") -> dict:
    """The canonical `v: 1` chunk-record dict for a non-empty call (FULL body text)."""
    return {
        "v": 1,
        "filename": call.filename,
        "ts": call.started.isoformat(),
        "day": _day_of(call),
        "backend": call.backend,
        "round": call.round,
        "argv0": call.argv0 or "",
        "task": call.task_code,
        "exit_code": call.exit_code,
        "timed_out": bool(call.timed_out),
        "timeout_secs": call.timeout_secs,
        "true_silenced": bool(call.true_silenced),
        "true_silence_secs": call.true_silence_secs,
        "completed": bool(call.completed),
        "has_error": bool(call.has_error),
        "error_summary": call.error_summary,
        "is_paywall": bool(call.is_paywall),
        "is_cf_blocked": bool(call.is_cf_blocked),
        "is_bad_key": bool(call.is_bad_key),
        "has_real_content": bool(call.has_real_content),
        "size_bytes": call.size_bytes,
        "mtime": call.mtime.isoformat() if call.mtime is not None else None,
        "body": call.body or "",
        "stderr": list(call.stderr_lines or []),
    }


def _call_from_record(rec: dict) -> "CallLog":
    """Rebuild a `CallLog` from a chunk/staging record. Bodies are re-capped for
    display (`_cap_body`/`_cap_stderr_lines`); ALL 7 classification fields are passed
    explicitly so `classify_call` reproduces the file-parse verdict exactly."""
    from .parser import CallLog, _cap_body, _cap_stderr_lines

    mtime = rec.get("mtime")
    return CallLog(
        path=rec.get("filename", ""),
        filename=rec.get("filename", ""),
        started=datetime.fromisoformat(rec["ts"]),
        backend=rec.get("backend", ""),
        round=int(rec.get("round", 0)),
        argv0=rec.get("argv0", ""),
        body=_cap_body(rec.get("body", "")),
        stderr_lines=_cap_stderr_lines(rec.get("stderr", []) or []),
        timed_out=bool(rec.get("timed_out", False)),
        timeout_secs=rec.get("timeout_secs"),
        true_silenced=bool(rec.get("true_silenced", False)),
        true_silence_secs=rec.get("true_silence_secs"),
        exit_code=rec.get("exit_code"),
        task_code=rec.get("task"),
        completed=bool(rec.get("completed", False)),
        has_error=bool(rec.get("has_error", False)),
        error_summary=rec.get("error_summary"),
        is_paywall=bool(rec.get("is_paywall", False)),
        is_cf_blocked=bool(rec.get("is_cf_blocked", False)),
        is_bad_key=bool(rec.get("is_bad_key", False)),
        has_real_content=bool(rec.get("has_real_content", False)),
        size_bytes=int(rec.get("size_bytes", 0)),
        mtime=datetime.fromisoformat(mtime) if mtime else None,
    )


def _empty_row_to_call(row: dict) -> "CallLog":
    """Rebuild a `CallLog` from an `empty_calls` row: `body=''` plus ALL 7
    classification fields explicit (the `classify_call`-exact reproduction)."""
    from .parser import CallLog

    return CallLog(
        path=row["filename"],
        filename=row["filename"],
        started=datetime.fromisoformat(row["ts"]),
        backend=row["backend"],
        round=row["round"],
        argv0=row["argv0"] or "",
        body="",
        stderr_lines=[],
        exit_code=row["exit_code"],
        task_code=row["task"],
        completed=bool(row["completed"]),
        has_error=bool(row["has_error"]),
        error_summary=row["error_summary"],
        is_paywall=bool(row["is_paywall"]),
        is_cf_blocked=bool(row["is_cf_blocked"]),
        is_bad_key=bool(row["is_bad_key"]),
        has_real_content=bool(row["has_real_content"]),
        size_bytes=row["size_bytes"],
        mtime=None,
    )


def _pack_chunks(records: list[dict]) -> list[list[dict]]:
    """Split ordered record dicts into chunks of <= `CHUNK_MAX_RECORDS` records AND
    <= `CHUNK_MAX_BYTES` raw JSONL bytes (byte size measured on the encoded line)."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = 0
    for rec in records:
        line_bytes = len(json.dumps(rec, ensure_ascii=False).encode("utf-8")) + 1
        if current and (
            len(current) >= CHUNK_MAX_RECORDS
            or current_bytes + line_bytes > CHUNK_MAX_BYTES
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(rec)
        current_bytes += line_bytes
    if current:
        chunks.append(current)
    return chunks


def _write_chunk(
    conn: sqlite3.Connection, day: str, backend: str, records: list[dict]
) -> tuple[int, int]:
    """Write one chunk row; returns (raw_bytes, zstd_bytes)."""
    lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    raw = ("\n".join(lines)).encode("utf-8")
    blob = compress_blob(raw, CHUNK_COMPRESS_LEVEL)
    by_ts = sorted(records, key=lambda r: r["ts"])
    conn.execute(
        "INSERT OR REPLACE INTO chunks(day, backend, min_ts, max_ts, n_calls,"
        " payload_zstd) VALUES (?, ?, ?, ?, ?, ?)",
        (day, backend, by_ts[0]["ts"], by_ts[-1]["ts"], len(records), blob),
    )
    return len(raw), len(blob)


def _iter_chunk_records(
    conn: sqlite3.Connection,
    day: str,
    backend: str,
    *,
    day_min: str | None = None,
    day_max: str | None = None,
) -> list[dict]:
    if day_min is not None and day < day_min:
        return []
    if day_max is not None and day > day_max:
        return []
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT payload_zstd FROM chunks WHERE day = ? AND backend = ?"
            " ORDER BY min_ts",
            (day, backend),
        ).fetchall()
    except sqlite3.Error:
        return []
    for (blob,) in rows:
        try:
            raw = decompress_blob(blob)
        except Exception:  # noqa: BLE001 -- a corrupt chunk must not kill the read
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ---- live writes -------------------------------------------------------------------


def _maybe_prune(conn: sqlite3.Connection) -> None:
    """Opportunistic `prune_archive` at most once/24h (meta `last_prune`); errors
    swallowed — pruning must never break a recording review."""
    try:
        last = _meta_get(conn, "last_prune")
        now = datetime.now(timezone.utc)
        if last is not None:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < 86400:
                    return
            except ValueError:
                pass
        prune_archive(conn)
        _meta_set(conn, "last_prune", now.isoformat())
    except Exception:  # noqa: BLE001 -- best-effort
        pass


def _insert_empty(conn: sqlite3.Connection, call: "CallLog", day: str) -> None:
    from .tokenstats import extract_usage_tokens

    usage = extract_usage_tokens(call)
    prompt, output = usage if usage is not None else (0, 0)
    conn.execute(
        "INSERT INTO empty_calls(day, ts, backend, round, task, argv0, exit_code,"
        " prompt_tokens, output_tokens, timed_out, true_silenced, completed,"
        " has_error, error_summary, is_paywall, is_cf_blocked, is_bad_key,"
        " has_real_content, size_bytes, filename)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            day,
            call.started.isoformat(),
            call.backend,
            call.round,
            call.task_code,
            call.argv0 or "",
            call.exit_code,
            prompt,
            output,
            int(bool(call.timed_out)),
            int(bool(call.true_silenced)),
            int(bool(call.completed)),
            int(bool(call.has_error)),
            call.error_summary,
            int(bool(call.is_paywall)),
            int(bool(call.is_cf_blocked)),
            int(bool(call.is_bad_key)),
            int(bool(call.has_real_content)),
            call.size_bytes,
            call.filename,
        ),
    )
    conn.commit()


def _record_call_impl(conn: sqlite3.Connection, call: "CallLog") -> bool:
    day = _day_of(call)
    if is_empty_call(call, call.size_bytes):
        _insert_empty(conn, call, day)
    else:
        rec = call_to_record(call)
        conn.execute(
            "INSERT INTO staging(day, backend, ts, record_json) VALUES (?, ?, ?, ?)",
            (day, call.backend, call.started.isoformat(), json.dumps(rec)),
        )
        conn.commit()
        # NOTE: `migrated_files` is migration-only — a live-recorded call is NOT
        # marked there (a later `migrate_all` over a dir still holding the source
        # file parses the file itself; archive/file merge dedupes by filename).
        seal_partition(conn, day, call.backend)
    _recompute_daily_stats(conn, day, call.backend)
    _maybe_prune(conn)
    return True


def record_call(call: "CallLog") -> bool:
    """Archive one live call: empty → `empty_calls` row, else a `staging` row plus
    opportunistic `seal_partition`. Best-effort: never raises, False on any failure."""
    try:
        conn = _default_conn()
        if conn is None:
            return False
        return _record_call_impl(conn, call)
    except Exception:  # noqa: BLE001 -- log loss beats a broken review
        return False


def record_brainstorm(bs: "BrainstormLog") -> bool:
    """Upsert one brainstorm discussion (body compressed level 9). Best-effort."""
    try:
        conn = _default_conn()
        if conn is None:
            return False
        day = bs.started.date().isoformat()
        blob = compress_blob(bs.body.encode("utf-8"), BRAINSTORM_COMPRESS_LEVEL)
        conn.execute(
            "INSERT INTO brainstorm(day, ts, topic, panel, moderator, task,"
            " filename, body_zstd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(filename) DO UPDATE SET day=excluded.day, ts=excluded.ts,"
            " topic=excluded.topic, panel=excluded.panel,"
            " moderator=excluded.moderator, task=excluded.task,"
            " body_zstd=excluded.body_zstd",
            (
                day,
                bs.started.isoformat(),
                bs.topic or "",
                ",".join(bs.panel or []),
                bs.moderator,
                bs.task_code,
                bs.filename,
                blob,
            ),
        )
        conn.commit()
        _maybe_prune(conn)
        return True
    except Exception:  # noqa: BLE001 -- log loss beats a broken review
        return False


# ---- sealing -------------------------------------------------------------------------


def seal_partition(
    conn: sqlite3.Connection, day: str, backend: str, *, force: bool = False
) -> int:
    """Move staged rows for (day, backend) into chunks. Without `force`, only seals
    when the staged pile reaches a full chunk (>= max records OR >= max bytes);
    with `force` (migration end), seals everything. Returns chunks written."""
    try:
        rows = conn.execute(
            "SELECT rowid, ts, record_json FROM staging WHERE day = ? AND backend = ?"
            " ORDER BY ts",
            (day, backend),
        ).fetchall()
    except sqlite3.Error:
        return 0
    if not rows:
        return 0
    total_bytes = sum(len(r[2].encode("utf-8")) for r in rows)
    if not force and len(rows) < CHUNK_MAX_RECORDS and total_bytes < CHUNK_MAX_BYTES:
        return 0
    records: list[dict] = []
    for _, _, payload in rows:
        try:
            records.append(json.loads(payload))
        except ValueError:
            continue
    if not records:
        return 0
    records.sort(key=lambda r: r.get("ts", ""))
    written = 0
    try:
        for part in _pack_chunks(records):
            _write_chunk(conn, day, backend, part)
            written += 1
        conn.execute(
            "DELETE FROM staging WHERE day = ? AND backend = ?", (day, backend)
        )
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0
    _recompute_daily_stats(conn, day, backend)
    return written


def seal_all(conn: sqlite3.Connection) -> int:
    """Force-seal every staged partition. Returns total chunks written."""
    try:
        parts = conn.execute("SELECT DISTINCT day, backend FROM staging").fetchall()
    except sqlite3.Error:
        return 0
    return sum(seal_partition(conn, day, backend, force=True) for day, backend in parts)


# ---- daily stats -----------------------------------------------------------------------


def _recompute_daily_stats(conn: sqlite3.Connection, day: str, backend: str) -> None:
    """Recompute one `daily_stats` row from chunks + staging + empty rows.

    n_fail uses the `compute_harness_stats` split (completed AND
    `classify_call != HEALTH_OK`); token sums reuse `extract_usage_tokens` on the
    reconstructed calls. `raw_bytes`/`zstd_bytes` cover chunk+staging payloads (empty
    calls are scalar rows with no payload); empty calls DO count in n_calls/n_fail.
    Best-effort: never raises.
    """
    try:
        from .parser import HEALTH_OK, classify_call
        from .tokenstats import extract_usage_tokens

        n_calls = 0
        n_fail = 0
        prompt = 0
        output = 0
        raw_bytes = 0
        zstd_bytes = 0
        for rec in _iter_chunk_records(conn, day, backend):
            call = _call_from_record(rec)
            n_calls += 1
            if call.completed and classify_call(call) != HEALTH_OK:
                n_fail += 1
            usage = extract_usage_tokens(call)
            if usage is not None:
                prompt += usage[0]
                output += usage[1]
        try:
            blobs = conn.execute(
                "SELECT length(payload_zstd) FROM chunks WHERE day = ? AND backend = ?",
                (day, backend),
            ).fetchall()
            zstd_bytes += sum(b[0] or 0 for b in blobs)
            raws = conn.execute(
                "SELECT payload_zstd FROM chunks WHERE day = ? AND backend = ?",
                (day, backend),
            ).fetchall()
            for (blob,) in raws:
                try:
                    raw_bytes += len(decompress_blob(blob))
                except Exception:  # noqa: BLE001 -- corrupt chunk; stats skip it
                    pass
        except sqlite3.Error:
            pass
        try:
            staged = conn.execute(
                "SELECT record_json FROM staging WHERE day = ? AND backend = ?",
                (day, backend),
            ).fetchall()
        except sqlite3.Error:
            staged = []
        for (payload,) in staged:
            raw_bytes += len(payload.encode("utf-8"))
            try:
                call = _call_from_record(json.loads(payload))
            except ValueError:
                continue
            n_calls += 1
            if call.completed and classify_call(call) != HEALTH_OK:
                n_fail += 1
            usage = extract_usage_tokens(call)
            if usage is not None:
                prompt += usage[0]
                output += usage[1]
        try:
            empty_rows = conn.execute(
                "SELECT prompt_tokens, output_tokens, completed, has_error,"
                " is_paywall, is_cf_blocked, is_bad_key, has_real_content,"
                " timed_out, true_silenced, exit_code"
                " FROM empty_calls WHERE day = ? AND backend = ?",
                (day, backend),
            ).fetchall()
        except sqlite3.Error:
            empty_rows = []
        for r in empty_rows:
            n_calls += 1
            prompt += r[0] or 0
            output += r[1] or 0
            # Empty rows are EXIT-0, completed, error-free by construction, so they
            # classify HEALTH_OK/HEALTH_EMPTY — never a fail. Recomputed structurally
            # instead of trusting that: completed AND (any fail signal).
            if r[2] and (r[3] or r[4] or r[5] or r[6]):
                n_fail += 1
        conn.execute(
            "INSERT INTO daily_stats(day, backend, n_calls, n_fail, prompt_tokens,"
            " output_tokens, raw_bytes, zstd_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(day, backend) DO UPDATE SET n_calls=excluded.n_calls,"
            " n_fail=excluded.n_fail, prompt_tokens=excluded.prompt_tokens,"
            " output_tokens=excluded.output_tokens, raw_bytes=excluded.raw_bytes,"
            " zstd_bytes=excluded.zstd_bytes",
            (day, backend, n_calls, n_fail, prompt, output, raw_bytes, zstd_bytes),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 -- stats are derived data, never fatal
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


# ---- pruning -----------------------------------------------------------------------------


def prune_archive(
    conn: sqlite3.Connection,
    chunks_days: int = 7,
    stats_days: int = 90,
    brainstorm_days: int = 90,
) -> dict:
    """DELETE rows older than the per-table day cutoffs (UTC today minus N days).
    Returns per-table deleted counts. Empty/staging rows age with chunks."""
    from datetime import date, timedelta

    today = date.today()
    out = {
        "chunks": 0,
        "staging": 0,
        "empty_calls": 0,
        "daily_stats": 0,
        "brainstorm": 0,
    }
    cuts = {
        "chunks": (today - timedelta(days=chunks_days)).isoformat(),
        "staging": (today - timedelta(days=chunks_days)).isoformat(),
        "empty_calls": (today - timedelta(days=chunks_days)).isoformat(),
        "daily_stats": (today - timedelta(days=stats_days)).isoformat(),
        "brainstorm": (today - timedelta(days=brainstorm_days)).isoformat(),
    }
    for table, cutoff in cuts.items():
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
            out[table] = cur.rowcount or 0
        except sqlite3.Error:
            out[table] = 0
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return out


# ---- readers -------------------------------------------------------------------------------


def iter_calls(
    conn: sqlite3.Connection,
    day_min: str | None = None,
    day_max: str | None = None,
):
    """Yield every archived `CallLog` (chunks + staging + empty rows), oldest first.

    Empty rows rebuild with `body=''` and all 7 classification fields explicit, so
    `classify_call` reproduces the file-parse verdict exactly.
    """
    calls: list[CallLog] = []
    try:
        parts = conn.execute(
            "SELECT DISTINCT day, backend FROM chunks"
            " UNION SELECT DISTINCT day, backend FROM staging"
            " UNION SELECT DISTINCT day, backend FROM empty_calls"
        ).fetchall()
    except sqlite3.Error:
        return
    for day, backend in sorted(parts):
        if day_min is not None and day < day_min:
            continue
        if day_max is not None and day > day_max:
            continue
        for rec in _iter_chunk_records(conn, day, backend):
            try:
                calls.append(_call_from_record(rec))
            except (ValueError, KeyError):
                continue
        try:
            staged = conn.execute(
                "SELECT record_json FROM staging WHERE day = ? AND backend = ?"
                " ORDER BY ts",
                (day, backend),
            ).fetchall()
        except sqlite3.Error:
            staged = []
        for (payload,) in staged:
            try:
                calls.append(_call_from_record(json.loads(payload)))
            except (ValueError, KeyError):
                continue
        try:
            cur = conn.execute(
                "SELECT day, ts, backend, round, task, argv0, exit_code,"
                " prompt_tokens, output_tokens, timed_out, true_silenced,"
                " completed, has_error, error_summary, is_paywall, is_cf_blocked,"
                " is_bad_key, has_real_content, size_bytes, filename"
                " FROM empty_calls WHERE day = ? AND backend = ? ORDER BY ts",
                (day, backend),
            )
            cols = [d[0] for d in cur.description]
            empty_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error:
            empty_rows = []
        for row in empty_rows:
            try:
                calls.append(_empty_row_to_call(row))
            except (ValueError, KeyError):
                continue
    calls.sort(key=lambda c: (c.started, c.filename))
    yield from calls


def archived_filenames(conn: sqlite3.Connection) -> set[str]:
    """Every call/brainstorm filename the archive holds (chunk payloads decompressed
    + staging JSON + empty/brainstorm filename columns). Used to dedupe the file scan
    so a log shadowed by an archive row is read once."""
    names: set[str] = set()
    try:
        blobs = conn.execute("SELECT payload_zstd FROM chunks").fetchall()
    except sqlite3.Error:
        blobs = []
    for (blob,) in blobs:
        try:
            raw = decompress_blob(blob)
        except Exception:  # noqa: BLE001
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("filename"):
                names.add(rec["filename"])
    for table, col in (
        ("staging", "record_json"),
        ("empty_calls", "filename"),
        ("brainstorm", "filename"),
        ("migrated_files", "filename"),
    ):
        try:
            rows = conn.execute(f"SELECT {col} FROM {table}").fetchall()
        except sqlite3.Error:
            continue
        for (value,) in rows:
            if table == "staging":
                try:
                    rec = json.loads(value)
                except ValueError:
                    continue
                if rec.get("filename"):
                    names.add(rec["filename"])
            elif value:
                names.add(value)
    return names


def iter_brainstorms(conn: sqlite3.Connection):
    """Yield every archived `BrainstormLog`, oldest first."""
    from .parser import BrainstormLog, _parse_stamp

    try:
        cur = conn.execute("SELECT * FROM brainstorm ORDER BY ts, filename")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return
    for row in rows:
        try:
            body = decompress_blob(row["body_zstd"]).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        try:
            started = datetime.fromisoformat(row["ts"])
        except ValueError:
            m = re.match(r"^(\d{8}T\d{6})_(\d+)Z-", row["filename"])
            if not m:
                continue
            try:
                started = _parse_stamp(m.group(1), m.group(2))
            except ValueError:
                continue
        panel = [p for p in (row["panel"] or "").split(",") if p]
        yield BrainstormLog(
            path=row["filename"],
            filename=row["filename"],
            started=started,
            topic=row["topic"] or "",
            panel=panel,
            moderator=row["moderator"],
            rounds=[],
            body=body,
            task_code=row["task"],
            mtime=None,
        )


# ---- migration ---------------------------------------------------------------------------------

_FAKE_MARKERS = ("faketest-", "faketree-")
_RETRY_MARKER = "-retry-"


def migrate_file(conn: sqlite3.Connection, path: Path) -> str:
    """Migrate one file; returns 'chunk' | 'empty' | 'brainstorm' | 'retry-skip' |
    'other-skip' | 'fake-skip' | 'unparseable'. Records `migrated_files` (migration
    idempotency). Non-empty calls buffer into `staging` (seal via `seal_all`)."""
    from .parser import _BRAINSTORM_RE, _CALL_RE, parse_brainstorm_log

    name = path.name
    kind: str
    if any(marker in name for marker in _FAKE_MARKERS):
        kind = "fake-skip"
    elif _RETRY_MARKER in name or name.startswith("qa-"):
        kind = "retry-skip" if _RETRY_MARKER in name else "other-skip"
    elif _BRAINSTORM_RE.match(name):
        bs = parse_brainstorm_log(path)
        if bs is None:
            kind = "unparseable"
        else:
            blob = compress_blob(bs.body.encode("utf-8"), BRAINSTORM_COMPRESS_LEVEL)
            day = bs.started.date().isoformat()
            conn.execute(
                "INSERT INTO brainstorm(day, ts, topic, panel, moderator, task,"
                " filename, body_zstd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(filename) DO UPDATE SET day=excluded.day,"
                " ts=excluded.ts, topic=excluded.topic, panel=excluded.panel,"
                " moderator=excluded.moderator, task=excluded.task,"
                " body_zstd=excluded.body_zstd",
                (
                    day,
                    bs.started.isoformat(),
                    bs.topic or "",
                    ",".join(bs.panel or []),
                    bs.moderator,
                    bs.task_code,
                    bs.filename,
                    blob,
                ),
            )
            kind = "brainstorm"
    elif _CALL_RE.match(name):
        # FULL text (cap=False): the archive keeps the complete verdict, not the
        # dashboard's memory-capped display slice; classification is identical.
        from .parser import parse_call_text

        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        try:
            st = path.stat()
            size = st.st_size
            file_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        except OSError:
            size = len(raw.encode("utf-8"))
            file_mtime = None
        call = parse_call_text(
            name,
            raw,
            path_str=str(path),
            size_bytes=size,
            mtime=file_mtime,
            cap=False,
        )
        if call is None:
            kind = "unparseable"
        elif is_empty_call(call, size):
            _insert_empty(conn, call, call.started.date().isoformat())
            kind = "empty"
        else:
            rec = call_to_record(call)
            conn.execute(
                "INSERT INTO staging(day, backend, ts, record_json)"
                " VALUES (?, ?, ?, ?)",
                (
                    rec["day"],
                    rec["backend"],
                    rec["ts"],
                    json.dumps(rec),
                ),
            )
            kind = "chunk"
    else:
        kind = "other-skip"
    try:
        conn.execute(
            "INSERT OR IGNORE INTO migrated_files(filename, kind) VALUES (?, ?)",
            (name, kind),
        )
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    return kind


def migrate_all(log_dir: Path, db: Path, *, limit: int | None = None) -> dict:
    """Bulk-migrate `log_dir` into `db`: parse each file with the REAL parser,
    buffer non-empty records per (day, backend) partition IN MEMORY only up to chunk
    scale (flushed to `chunks` as full chunks form, so RSS stays bounded on a 4GB
    corpus), executemany the scalar rows, seal, recompute touched `daily_stats`
    partitions, write the meta receipt. Idempotent via `migrated_files`. Returns
    per-kind counts + bytes."""
    from .parser import _BRAINSTORM_RE, _CALL_RE, parse_brainstorm_log, parse_call_text

    log_dir = Path(log_dir)
    conn = connect(Path(db))
    try:
        migrated = {
            r[0] for r in conn.execute("SELECT filename FROM migrated_files").fetchall()
        }
    except sqlite3.Error:
        migrated = set()
    counts = {
        "chunk": 0,
        "empty": 0,
        "brainstorm": 0,
        "retry-skip": 0,
        "other-skip": 0,
        "fake-skip": 0,
        "unparseable": 0,
    }
    # Partition buffers: (day, backend) -> list of (ts, record_json, raw_len).
    # Flushed to a chunk row as soon as EITHER threshold is reached, so memory stays
    # ~ (#live partitions x chunk scale) instead of the whole corpus.
    buffers: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    touched: set[tuple[str, str]] = set()
    empty_batch: list[tuple] = []
    bs_batch: list[tuple] = []
    mig_batch: list[tuple] = []
    chunks_written = 0
    body_bytes = 0

    def flush_partition(key: tuple[str, str]) -> None:
        nonlocal chunks_written
        buf = buffers.get(key)
        if not buf:
            return
        day, backend = key
        records = [json.loads(payload) for _, payload, _ in sorted(buf)]
        for part in _pack_chunks(records):
            raw, _ = _write_chunk(conn, day, backend, part)
            chunks_written += 1
        buffers[key] = []
        touched.add(key)

    def flush_empty() -> None:
        if not empty_batch:
            return
        conn.executemany(
            "INSERT INTO empty_calls(day, ts, backend, round, task, argv0, exit_code,"
            " prompt_tokens, output_tokens, timed_out, true_silenced, completed,"
            " has_error, error_summary, is_paywall, is_cf_blocked, is_bad_key,"
            " has_real_content, size_bytes, filename)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            empty_batch,
        )
        empty_batch.clear()

    def flush_bs() -> None:
        if not bs_batch:
            return
        conn.executemany(
            "INSERT INTO brainstorm(day, ts, topic, panel, moderator, task,"
            " filename, body_zstd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(filename) DO UPDATE SET day=excluded.day, ts=excluded.ts,"
            " topic=excluded.topic, panel=excluded.panel,"
            " moderator=excluded.moderator, task=excluded.task,"
            " body_zstd=excluded.body_zstd",
            bs_batch,
        )
        bs_batch.clear()

    def flush_mig() -> None:
        if not mig_batch:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO migrated_files(filename, kind) VALUES (?, ?)",
            mig_batch,
        )
        mig_batch.clear()

    from .tokenstats import extract_usage_tokens

    try:
        files = sorted(p for p in log_dir.iterdir() if p.is_file())
    except OSError:
        files = []
    processed = 0
    for path in files:
        if limit is not None and processed >= limit:
            break
        name = path.name
        if name in migrated or name == ARCHIVE_FILENAME or name == db.name:
            continue
        processed += 1
        if any(marker in name for marker in _FAKE_MARKERS):
            counts["fake-skip"] += 1
            mig_batch.append((name, "fake-skip"))
        elif _RETRY_MARKER in name:
            counts["retry-skip"] += 1
            mig_batch.append((name, "retry-skip"))
        elif _BRAINSTORM_RE.match(name):
            bs = parse_brainstorm_log(path)
            if bs is None:
                counts["unparseable"] += 1
                mig_batch.append((name, "unparseable"))
            else:
                blob = compress_blob(bs.body.encode("utf-8"), BRAINSTORM_COMPRESS_LEVEL)
                bs_batch.append(
                    (
                        bs.started.date().isoformat(),
                        bs.started.isoformat(),
                        bs.topic or "",
                        ",".join(bs.panel or []),
                        bs.moderator,
                        bs.task_code,
                        bs.filename,
                        blob,
                    )
                )
                body_bytes += len(bs.body.encode("utf-8"))
                counts["brainstorm"] += 1
                mig_batch.append((name, "brainstorm"))
        elif _CALL_RE.match(name):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            try:
                st = path.stat()
                size = st.st_size
                file_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            except OSError:
                size = len(raw.encode("utf-8"))
                file_mtime = None
            call = parse_call_text(
                name,
                raw,
                path_str=str(path),
                size_bytes=size,
                mtime=file_mtime,
                cap=False,
            )
            if call is None:
                counts["unparseable"] += 1
                mig_batch.append((name, "unparseable"))
            elif is_empty_call(call, size):
                usage = extract_usage_tokens(call)
                prompt, output = usage if usage is not None else (0, 0)
                empty_batch.append(
                    (
                        call.started.date().isoformat(),
                        call.started.isoformat(),
                        call.backend,
                        call.round,
                        call.task_code,
                        call.argv0 or "",
                        call.exit_code,
                        prompt,
                        output,
                        int(bool(call.timed_out)),
                        int(bool(call.true_silenced)),
                        int(bool(call.completed)),
                        int(bool(call.has_error)),
                        call.error_summary,
                        int(bool(call.is_paywall)),
                        int(bool(call.is_cf_blocked)),
                        int(bool(call.is_bad_key)),
                        int(bool(call.has_real_content)),
                        call.size_bytes,
                        call.filename,
                    )
                )
                touched.add((call.started.date().isoformat(), call.backend))
                counts["empty"] += 1
                mig_batch.append((name, "empty"))
            else:
                rec = call_to_record(call)
                payload = json.dumps(rec)
                key = (rec["day"], rec["backend"])
                buffers.setdefault(key, []).append(
                    (rec["ts"], payload, len(payload.encode("utf-8")))
                )
                body_bytes += len((rec["body"] or "").encode("utf-8"))
                buf = buffers[key]
                if (
                    len(buf) >= CHUNK_MAX_RECORDS
                    or sum(b[2] for b in buf) >= CHUNK_MAX_BYTES
                ):
                    flush_partition(key)
                counts["chunk"] += 1
                mig_batch.append((name, "chunk"))
        else:
            counts["other-skip"] += 1
            mig_batch.append((name, "other-skip"))
        if len(mig_batch) >= 2000:
            flush_empty()
            flush_bs()
            flush_mig()
            conn.commit()
    for key in list(buffers):
        flush_partition(key)
    flush_empty()
    flush_bs()
    flush_mig()
    conn.commit()
    seal_all(conn)  # any stragglers (none expected: bulk writes chunks directly)
    for day, backend in sorted(touched):
        _recompute_daily_stats(conn, day, backend)
    receipt = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "log_dir": str(log_dir),
        "codec": codec_name(),
        "chunks_written": chunks_written,
        "body_bytes": body_bytes,
        "counts": counts,
    }
    _meta_set(conn, "last_migration", json.dumps(receipt))
    result = dict(counts)
    result.update(
        {
            "chunks_written": chunks_written,
            "body_bytes": body_bytes,
            "db": str(db),
        }
    )
    return result


# ---- proofs --------------------------------------------------------------------------------------


def proof_counts(
    conn: sqlite3.Connection, file_call_count: int, file_brainstorm_count: int
) -> dict:
    """Compare archived totals vs the file census: archived calls =
    `SUM(chunks.n_calls) + COUNT(empty_calls)` (staging is transient — sealed before
    proofs run), archived brainstorms = `COUNT(brainstorm)`."""
    try:
        chunk_calls = (
            conn.execute("SELECT COALESCE(SUM(n_calls), 0) FROM chunks").fetchone()[0]
            or 0
        )
        empty_calls = conn.execute("SELECT COUNT(*) FROM empty_calls").fetchone()[0]
        staged = conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0]
        n_brainstorm = conn.execute("SELECT COUNT(*) FROM brainstorm").fetchone()[0]
    except sqlite3.Error:
        chunk_calls, empty_calls, staged, n_brainstorm = 0, 0, 0, 0
    archived_calls = chunk_calls + empty_calls
    detail = {
        "file_calls": file_call_count,
        "chunk_calls": chunk_calls,
        "staged_calls": staged,
        "empty_calls": empty_calls,
        "archived_calls": archived_calls,
        "file_brainstorms": file_brainstorm_count,
        "archived_brainstorms": n_brainstorm,
    }
    ok = archived_calls == file_call_count and n_brainstorm == file_brainstorm_count
    return {"ok": ok, "detail": detail}


def _scalar_record_mismatch(rec: dict, call: "CallLog") -> list[str]:
    """Field-by-field diff of a chunk record vs a fresh file re-parse (FULL body)."""
    mismatches: list[str] = []
    expected = call_to_record(call)
    for key in (
        "filename",
        "ts",
        "day",
        "backend",
        "round",
        "argv0",
        "task",
        "exit_code",
        "timed_out",
        "timeout_secs",
        "true_silenced",
        "true_silence_secs",
        "completed",
        "has_error",
        "error_summary",
        "is_paywall",
        "is_cf_blocked",
        "is_bad_key",
        "has_real_content",
        "size_bytes",
        "mtime",
    ):
        if rec.get(key) != expected.get(key):
            mismatches.append(
                f"{key}: archive={rec.get(key)!r} file={expected.get(key)!r}"
            )
    if rec.get("body", "") != (call.body or ""):
        mismatches.append(
            f"body: len archive={len(rec.get('body', ''))} file={len(call.body or '')}"
        )
    if list(rec.get("stderr", []) or []) != list(call.stderr_lines or []):
        mismatches.append("stderr: lists differ")
    return mismatches


def proof_chunks_diff(
    conn: sqlite3.Connection, log_dir: Path, k: int = 5, seed: int = 0
) -> dict:
    """Random `k` chunks → every record → source file must exist → re-parse (FULL
    text) → compare ALL scalar fields + full body. Mismatches listed, never bare."""
    from .parser import parse_call_text

    log_dir = Path(log_dir)
    try:
        chunks = conn.execute(
            "SELECT day, backend, min_ts, n_calls FROM chunks"
        ).fetchall()
    except sqlite3.Error:
        chunks = []
    rng = random.Random(seed)
    picked = rng.sample(chunks, min(k, len(chunks))) if chunks else []
    records_checked = 0
    mismatches: list[str] = []
    for day, backend, min_ts, _n in picked:
        try:
            blob = conn.execute(
                "SELECT payload_zstd FROM chunks WHERE day = ? AND backend = ?"
                " AND min_ts = ?",
                (day, backend, min_ts),
            ).fetchone()[0]
            raw = decompress_blob(blob)
        except Exception as exc:  # noqa: BLE001
            mismatches.append(f"chunk {day}/{backend}/{min_ts}: unreadable ({exc})")
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                mismatches.append(f"chunk {day}/{backend}/{min_ts}: corrupt JSONL line")
                continue
            records_checked += 1
            src = log_dir / rec.get("filename", "")
            if not src.is_file():
                mismatches.append(f"{rec.get('filename')}: source file missing")
                continue
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                mismatches.append(f"{rec.get('filename')}: unreadable ({exc})")
                continue
            call = parse_call_text(rec["filename"], text, cap=False)
            if call is None:
                mismatches.append(f"{rec.get('filename')}: no longer parses")
                continue
            for m in _scalar_record_mismatch(rec, call):
                mismatches.append(f"{rec.get('filename')}: {m}")
    detail = {
        "chunks_total": len(chunks),
        "chunks_checked": len(picked),
        "records_checked": records_checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
    }
    return {"ok": not mismatches, "detail": detail}


def proof_day_tokens(conn: sqlite3.Connection, log_dir: Path, days: list[str]) -> dict:
    """Per (day, backend): file-variant recompute (direct `parse_call_log`, no cache)
    of n_calls / n_fail / prompt / output vs the `daily_stats` row."""
    from .parser import HEALTH_OK, _CALL_RE, classify_call, parse_call_log
    from .tokenstats import extract_usage_tokens

    log_dir = Path(log_dir)
    # Group files by (day, backend) straight from the filename (no reads yet).
    groups: dict[tuple[str, str], list[Path]] = {}
    try:
        files = sorted(p for p in log_dir.iterdir() if p.is_file())
    except OSError:
        files = []
    for path in files:
        m = _CALL_RE.match(path.name)
        if not m:
            continue
        if any(marker in path.name for marker in _FAKE_MARKERS):
            continue
        if _RETRY_MARKER in path.name:
            continue
        try:
            from .parser import _parse_stamp

            stamp = _parse_stamp(m.group(1), m.group(2))
        except ValueError:
            continue
        day = stamp.date().isoformat()
        if day not in days:
            continue
        groups.setdefault((day, m.group(3)), []).append(path)
    partitions_checked = 0
    mismatches: list[str] = []
    for (day, backend), paths in sorted(groups.items()):
        n_calls = 0
        n_fail = 0
        prompt = 0
        output = 0
        for path in paths:
            call = parse_call_log(path)  # direct, no cache — the file variant
            if call is None:
                continue
            # Only NON-EMPTY file calls have chunk/staging rows; empty ones land in
            # `empty_calls` — but `daily_stats` covers both, so count both here, and
            # skip files the migrator would not archive at all.
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if is_empty_call(call, size):
                usage = extract_usage_tokens(call)
                n_calls += 1
                if usage is not None:
                    prompt += usage[0]
                    output += usage[1]
                continue
            n_calls += 1
            if call.completed and classify_call(call) != HEALTH_OK:
                n_fail += 1
            usage = extract_usage_tokens(call)
            if usage is not None:
                prompt += usage[0]
                output += usage[1]
        try:
            row = conn.execute(
                "SELECT n_calls, n_fail, prompt_tokens, output_tokens"
                " FROM daily_stats WHERE day = ? AND backend = ?",
                (day, backend),
            ).fetchone()
        except sqlite3.Error:
            row = None
        partitions_checked += 1
        if row is None:
            mismatches.append(f"{day}/{backend}: no daily_stats row")
            continue
        expected = (n_calls, n_fail, prompt, output)
        if tuple(row) != expected:
            mismatches.append(f"{day}/{backend}: stats={tuple(row)} files={expected}")
    detail = {
        "days": days,
        "partitions_checked": partitions_checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
    }
    return {"ok": not mismatches, "detail": detail}


def proof_brainstorms(
    conn: sqlite3.Connection, log_dir: Path, k: int = 2, seed: int = 0
) -> dict:
    """Random `k` archived brainstorms: raw file bytes == decompressed body bytes."""
    log_dir = Path(log_dir)
    try:
        rows = conn.execute("SELECT filename, body_zstd FROM brainstorm").fetchall()
    except sqlite3.Error:
        rows = []
    rng = random.Random(seed)
    picked = rng.sample(rows, min(k, len(rows))) if rows else []
    mismatches: list[str] = []
    for filename, blob in picked:
        src = log_dir / filename
        if not src.is_file():
            mismatches.append(f"{filename}: source file missing")
            continue
        try:
            raw = src.read_bytes()
        except OSError as exc:
            mismatches.append(f"{filename}: unreadable ({exc})")
            continue
        try:
            body = decompress_blob(blob)
        except Exception as exc:  # noqa: BLE001
            mismatches.append(f"{filename}: undecompressible ({exc})")
            continue
        if body != raw:
            mismatches.append(
                f"{filename}: bytes differ (archive={len(body)} file={len(raw)})"
            )
    detail = {
        "brainstorms_total": len(rows),
        "checked": len(picked),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
    }
    return {"ok": not mismatches, "detail": detail}
