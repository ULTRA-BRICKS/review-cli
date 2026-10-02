# review-cli log archive — perpetual compressed store

> **Status:** spec (implementation in progress).  
> **Source:** Alex, session «Куда делось место на диске» (2026-09-09), Telegram follow-ups.  
> **Repo:** `review-cli` (`/Users/ultra/xp/.ship-main/review-cli`).  
> **Primary modules:** `reviewlib/log_archive.py`, `reviewlib/log_archive_migrate.py`, `reviewlib/dashboard/tokenstats.py`, `reviewlib/process.py`.

> **Review:** submitted 2026-09-09 (`spec-web`, 6 questions + 8 remarks). Responses incorporated below. Delivery to tmux `hyperide` failed; agent pulled via `review spec-web watch … --emit-current`.

## 1. Problem

`~/Library/Logs/review-cli` grew to **~201k files / ~18 GB** of flat `.log` files. A one-shot migration moved **113 321** call logs into `~/.local/state/review-cli/logs-archive.sqlite3` (~928 MB zstd-compressed chunks). The parse cache `~/.cache/review-cli/.call-log-cache.sqlite3` (~14 GB) was removed — it is rebuildable and unnecessary in sqlite write mode.

After migration the archive is the **only durable copy** of full call transcripts. Two design mistakes remain in code:

| Constant / function | Current behaviour | Risk |
| --- | --- | --- |
| `_CHUNKS_RETAIN_DAYS = 7` + `rotate()` | `DELETE FROM chunks WHERE day < ?` | **Irreversible loss** of full transcripts older than 7 days |
| `_DAILY_STATS_RETAIN_DAYS = 90` + `rotate()` | `DELETE FROM daily_stats WHERE day < ?` | Loss of long-term aggregates for `review stat` (~100 B/row — no space win) |

`rotate()` is **not called automatically today** (verified: only defined in `log_archive.py`). Data is intact (chunks 2026-06-13…2026-09-09). The constants are still dangerous if someone wires `rotate()` into a cron or startup path.

**Separate from archive:** test harness artefacts (`faketest*`, `faketree*`) must **not** enter durable storage:

- **Write path:** `store_call_raw` / `store_brainstorm` **must not persist** `faketest*` / `faketree*` (same rule as migration ingest). Harness noise must never become history.
- **One-shot cleanup:** migration already **skips** on ingest; `prune_faketest_archive` still required for rows that predate the write-path skip.
- **Optional TTL safety net:** future `REVIEW_LOG_ARCHIVE_TEST_TTL_HOURS=1` may auto-purge faketest/faketree rows younger than N hours only — production leaves unset.

## 2. Goal

Turn the sqlite archive into a **perpetual compressed store**:

```
WRITE (store_call_raw / store_brainstorm)
  → zstd payload in SQLite
  → KEEP FOREVER (no age-based DELETE)

READ paths
  → dashboard / task detail: decompress chunks on demand (today)
  → review stat --days N: daily_stats + call_meta (phase 1), not full chunk scan
```

**Non-negotiable (Alex):**

1. **Preserve statistics and logs for all time** — aggregates and compressed transcripts stay.
2. **Delete test runs** (`faketest`, `faketree`, harness self-test noise) after proof they carry no production signal.
3. **Empty / stub calls** — no full transcript body; keep metadata + `is_empty=1` (already implemented via `empty_calls`).
4. **No silent data loss** — age-based rotation is forbidden unless an explicit env override + dry-run + audit log.

## 3. Storage model (current + target)

### 3.1 Tables (today)

| Table | Role | Retention target |
| --- | --- | --- |
| `chunks` | zstd JSON array of `{filename, content, mtime, size}` batched by `(day, backend)` | **Forever** |
| `daily_stats` | Aggregates per `(day, backend)`: calls, fails, tokens, bytes | **Forever** |
| `empty_calls` | Stub calls: no body, tokens/exit/task preserved | **Forever** |
| `brainstorm` | zstd brainstorm bodies | **Forever** |
| `call_files` | Filename index (dedup / migration proofs) | **Forever** |
| `call_meta` | **NEW** — one row per call (see §4); indexed metadata | **Forever** |

### 3.2 Why `call_meta` is its own table

`chunks` stores **batched zstd JSON arrays** (~1000 calls per row). Per-call fields (`task`, `exit`, tokens, `started`) live inside compressed blobs — you cannot `WHERE task = ?` or `ORDER BY started` without decompressing every chunk.

| Alternative | Why rejected |
| --- | --- |
| Index on `chunks` columns | No per-call columns at SQL layer |
| Scan `empty_calls` only | Covers stub calls, not full transcripts |
| Rely on `daily_stats` only | Day/backend aggregates — no per-call drill-down |
| Denormalize into chunk JSON | Still requires full decompress for queries |

`call_meta` is a **normalized per-call row table** with btree indexes (`idx_call_meta_started`, etc.), not a redundant duplicate of `call_files`.

### 3.3 What is cache vs archive

| Artifact | Cache? | Safe to delete? |
| --- | --- | --- |
| `~/.cache/review-cli/.call-log-cache.sqlite3` | **Yes** — parsed `CallLog` JSON | **Yes** (rebuild from archive; slow) |
| `chunks` | **No** — primary transcript store post-migration | **Never** by age |
| `daily_stats` | **No** — long-term metrics | **Never** by age |
| Flat `~/Library/Logs/review-cli/*.log` post-migration | Legacy source | Deleted only after migration proofs pass |

## 4. `call_meta` schema (phase 1)

Per-call index so `review stat` and session clustering do not decompress every chunk.

```sql
CREATE TABLE IF NOT EXISTS call_meta (
  filename       TEXT PRIMARY KEY,
  started_unix   INTEGER NOT NULL,   -- UTC microseconds since epoch
  day            TEXT NOT NULL,      -- 'YYYY-MM-DD' partition key, matches chunks/daily_stats
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

CREATE INDEX IF NOT EXISTS idx_call_meta_started ON call_meta(started_unix);
CREATE INDEX IF NOT EXISTS idx_call_meta_day ON call_meta(day);
CREATE INDEX IF NOT EXISTS idx_call_meta_backend ON call_meta(backend);
```

**Why `started_unix INTEGER`, not TEXT?**

- In Python, `CallLog.started` is `datetime` (`parser.py`).
- Existing archive uses TEXT `_ts_key` / `_day_key` for chunk keys and filenames — keep those for compatibility.
- **New** `call_meta` uses integer epoch for `ORDER BY`, range filters (`review stat --days 7`), and session gap math (90s window). `day TEXT` remains the partition key aligned with `daily_stats`.

**Write path:** `store_call_raw` and `_flush_pending` insert/update `call_meta` for every call (including rows that land in `empty_calls`).

**Backfill:** `log_archive_migrate.py` gains `--backfill-call-meta` (idempotent `INSERT OR IGNORE` from existing chunks + `empty_calls`).

## 5. `rotate()` — redefine, do not delete history

### 5.1 Forbidden (default)

- `DELETE FROM chunks …` by age
- `DELETE FROM daily_stats …` by age
- `DELETE FROM empty_calls` / `brainstorm` by age

### 5.2 Allowed operations

Rename or split into explicit functions:

| Function | Purpose |
| --- | --- |
| `prune_faketest(conn)` | Remove `faketest*` / `faketree*` from `call_files`, `call_meta`, `empty_calls`, and any chunk entries referencing those filenames; recompute affected `daily_stats` rows or run targeted rebuild for touched days |
| `vacuum_archive(conn)` | `VACUUM` / `PRAGMA optimize` after large deletes — **no row loss** |
| `merge_small_chunks(conn)` | Optional compaction: combine undersized chunks same `(day, backend)` — **lossless** |

### 5.3 Optional escape hatch (off by default)

Env `REVIEW_LOG_ARCHIVE_RETAIN_DAYS_CHUNKS=0` (0 = forever). If set to N>0, `rotate()` may delete chunks older than N days **only** when both:

1. Env explicitly set (not default), and  
2. `--dry-run` preview printed counts, and  
3. Caller passes `confirm=True`.

**Semantics (module docstring, CLI `--help`, retention stdout):**

| Value | Meaning |
| --- | --- |
| unset or `0` | **Forever** — never delete chunks by age (production default) |
| `N > 0` | Dev/test escape hatch — may delete chunks older than N days when guards below pass |

In tests, `REVIEW_LOG_ARCHIVE_RETAIN_DAYS_CHUNKS=0` means **never** (same as production), not "delete everything". Tests needing bounded history must set explicit `N > 0`.

Document in module docstring: **production installs must leave retain days at 0.**

## 6. Test data — do not write, and prune if present

**Policy (Alex, review 2026-09-09):** harness artefacts must **not** enter durable logs. Cleaning up after the fact is not enough — the write path skips them; anything that slips in is short-lived.

**Detection** (existing in `log_archive_migrate._is_faketest_name`):

```python
name.lower().startswith("faketest") or name.lower().startswith("faketree")
```

**Write path (target):** `store_call_raw` / `store_brainstorm` return early (no DB insert) when `_is_faketest_name(filename)`.

**Scope:**

1. Flat log dir: `delete_faketest_logs()` — already in migrate `main()`; legacy only.
2. Archive: `prune_faketest_archive(conn) -> PruneReport` removes rows that predate the write-path skip.
3. CLI: `python -m reviewlib.log_archive_migrate --prune-faketest-only` (no re-migrate).

**Acceptance:** after prune, `SELECT COUNT(*) FROM call_files WHERE filename LIKE 'faketest%' OR filename LIKE 'faketree%'` → **0**, and no chunk payload still contains those filenames.

**Proof before delete:** print sample count + bytes freed; require `--yes` for mutating run.

## 7. `review stat` phase 1

Today `review stat` scans/decompresses call logs. Target:

| Window | Source |
| --- | --- |
| `--days 0` (all time) | `daily_stats` SUM grouped by backend; token totals from same table |
| `--days N` | `daily_stats WHERE day >= cutoff` |
| Per-call drill-down | `call_meta` for listing; one matching chunk decompressed on demand — **phase 2** |

**What "per-call drill-down" means:** today `review stat` prints backend totals. Drill-down = list individual calls in a window (time, task, exit, tokens) and open one call's full transcript. Phase 1 only needs day-level totals from `daily_stats`; drill-down needs `call_meta` (§4) and is deferred to phase 2.

**What "selective chunk decompress" means:** a chunk holds up to ~1000 calls in one zstd blob. To render **one** call: read `call_meta` → locate `(day, backend, min_ts)` → decompress **that chunk only**. Today `load_calls_from_archive()` decompresses **all** chunks (fine for dashboard, too slow for stat over 100k+ calls). Phase 1 stat avoids chunks entirely; phase 2 drill-down uses selective decompress.

**Constraint:** phase 1 must match migration proof C (per-day token/call totals) on a sample of days.

**Out of scope for phase 1:** rebuilding `daily_stats` from chunks on every stat invocation; per-call listing / transcript open.

## 8. Write path defaults (done / verify)

| Item | Expected state |
| --- | --- |
| `REVIEW_LOG_SQLITE` default | `1` (sqlite write on) |
| `store_call_raw` | Default path in `process.py` |
| Flat `.log` creation | **Removed** — sqlite-only write path; no `REVIEW_LOG_SQLITE=0` flat fallback |
| `~/.bunfig.toml` preload | `REVIEW_LOG_SQLITE=1` for Bun-driven review (sqlite default) |

## 9. Migration status (2026-09-09)

Completed on Alex's machine:

- Migrate 113k+ calls → archive sqlite (~928 MB)
- Delete flat sources after proofs A–D
- Remove 14 GB call-log-cache
- `rig worktree gc` freed ~101 GiB worktrees (separate from log archive); gc now also deletes local branches and `origin` remotes by default (`--no-delete-branch` to skip; protected: main/master/develop/trunk)

**Still TODO (this spec):**

- [ ] Remove dangerous default retention from `rotate()`
- [ ] Add `call_meta` + backfill
- [ ] `prune_faketest_archive` + verify 0 faketest rows
- [ ] `review stat` reads `daily_stats` for `--days`
- [ ] Tests: retention forever, faketest prune, call_meta roundtrip, stat parity

## 10. Tests

| Test | Asserts |
| --- | --- |
| `test_rotate_does_not_delete_by_default` | Seed chunks/stats with old `day`; `rotate()` → row counts unchanged |
| `test_prune_faketest_removes_archive_rows` | Insert faketest filename in chunk payload; prune → gone from all tables |
| `test_call_meta_backfill` | Migrate fixture dir; every `call_files` row has `call_meta` |
| `test_stat_uses_daily_stats` | Mock archive with known `daily_stats`; `review stat --days 7` matches without reading chunks |
| Existing `test_log_archive.py` | Still pass; empty stub → `empty_calls` not `chunks` |

## 11. Resolved / deferred decisions

### 11.1 `started_unix` unit — **resolved: microseconds**

Matches log filename `%f` precision and `CallLog.started` microsecond resolution. Reviewer agreed.

### 11.2 Chunk merge — **deferred to v2**

**What it is:** `merge_small_chunks(conn)` combines multiple undersized `chunks` rows sharing `(day, backend)` into one larger zstd blob, then deletes old rows. Purely structural — no transcript loss, fewer SQLite rows.

**Why defer:** archive is ~1 GB after 113k calls; merge is an optimization when row count or file size hurts (`>2 GB` or slow `SELECT` on `chunks`). Not required for perpetual retention or stat phase 1.

### 11.3 `daily_stats` corruption / rebuild — **ship manual rebuild CLI**

**What can go wrong:** a bug in `_update_daily_stats` (or partial migration) could double-count calls, assign wrong `day`, or drop fails. **Aggregations at risk:** per `(day, backend)` — `n_calls`, `n_fail`, `prompt_tokens`, `output_tokens`, `raw_bytes`, `zstd_bytes`.

**"Ship"** = ship as part of `review-cli`, not a one-off script.

| Option | Pros | Cons |
| --- | --- | --- |
| **A. `review archive rebuild-stats --from-chunks` (manual)** | Auditable, dry-run diff, no false auto-heal | Slow (full chunk scan), operator must run |
| B. Background checksum + auto-rebuild | Self-healing | Complex, risk of rebuild during writes |
| C. Never rebuild | Zero code | Silent wrong stats forever |

**Decision:** **A** — manual CLI with `--dry-run` showing per-day diffs; never auto-run on startup.

### 11.4 Cross-machine archive — **documented, no sync**

**What it means:** each machine has its own `~/.local/state/review-cli/logs-archive.sqlite3` (or `$XDG_STATE_HOME/review-cli/...`). There is **no** built-in merge/sync between laptop and desktop in v1.

**Backup expectations (document in README / module docstring):**

- Treat the sqlite file (+ `-wal`/`-shm` if copying live) as the backup unit.
- Time Machine / restic / manual `cp` is the operator's responsibility.
- Copying one machine's archive onto another **overwrites** — no automatic dedup/merge.

**Out of scope:** replicated archive, cloud sync, multi-writer sqlite.

## 12. Out of scope

- Docker container log rotation on host (separate disk investigation).
- opencode `opencode.db` growth (separate tool).
- Encrypting archive at rest.

---

## Appendix A — architecture

```
┌─────────────────┐     store_call_raw      ┌──────────────────────────────┐
│ review backends │ ───────────────────────►│ logs-archive.sqlite3         │
│ (process.py)    │                         │  ├─ call_meta (index)        │
└─────────────────┘                         │  ├─ chunks (zstd transcripts)│
                                            │  ├─ empty_calls (stubs)      │
┌─────────────────┐     read                │  ├─ daily_stats (aggregates) │
│ review stat     │ ◄───────────────────────│  └─ brainstorm               │
│ dashboard/task  │                         └──────────────────────────────┘
└─────────────────┘
        │
        │  legacy migration only (one-shot)
        ▼
 ~/Library/Logs/review-cli/*.log  (import then delete)
```

## Appendix B — reviewer checklist (2026-09-09 submit)

- [x] Agree: no age-based DELETE on `chunks` / `daily_stats` by default
- [x] Agree: `call_meta.started_unix` as INTEGER epoch (microseconds)
- [x] Agree: faketest prune mandatory before calling migration "done"
- [x] Agree: write path must skip faketest/faketree (not only prune)
- [x] Phase 1 `review stat` from `daily_stats` sufficient for `--days N` / `--days 0` totals

**Phase 1 stat explained:** `review stat --days 7` will `SELECT SUM(...) FROM daily_stats WHERE day >= cutoff` grouped by `backend` — same numbers migration proof C verified per day, without reading `chunks`. Per-call listing and transcript open are phase 2 (`call_meta` + selective decompress).

[Showing lines 1-300 of 302. Use :301 to continue]