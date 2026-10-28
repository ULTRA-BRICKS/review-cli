"""Reading the run-stats JSONL store (review-cli#721 split of ``reviewlib.stats``).

``stats_path`` locates the append-only store; ``_load_records`` is the lenient reader
(junk lines skipped, never raises) and ``_strict_records`` the exact one an import or
export needs (an unreadable store is not an empty one). ``iterations_for_task`` and
``export_records`` are the per-task views.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .stats_identity import _github_issue_task_keys, normalize_task_code


def stats_path() -> Path:
    """Append-only JSONL store of run records.

    Honors ``$REVIEW_STATS_FILE`` (tests / opt-relocation); otherwise lives next
    to the other review-cli config under ``~/.config/review-cli/run-stats.jsonl``.
    The parent dir is created 0700-ish by the OS default; the file itself is
    forced 0600 on first write.
    """
    override = os.environ.get("REVIEW_STATS_FILE")
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".config" / "review-cli" / "run-stats.jsonl"
    return p


def _load_records() -> list[dict]:
    """Read every well-formed JSONL record. Skips junk lines; never raises."""
    out: list[dict] = []
    try:
        raw = stats_path().read_text(
            encoding="utf-8"
        )  # stats_path() may raise RuntimeError
    except Exception:  # noqa: BLE001 — unreadable/unexpandable store -> no history, never crash
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "duration_seconds" in rec:
            out.append(rec)
    return out


def _records() -> list[dict]:
    """The store's records, read through ``reviewlib.stats._load_records`` at call time.

    ``reviewlib.stats`` is the public face of this package, and ``_load_records`` is a
    seam callers rebind there (a test that must prove a reader does NOT re-read the
    store swaps it). Resolving it through that module keeps a rebind effective for every
    reader that moved out of it; the import is function-local because ``reviewlib.stats``
    itself imports this module.
    """
    from . import stats

    return stats._load_records()


def _record_ts_str(record: dict) -> str:
    """The raw ``ts`` sort key for `iterations_for_task` -- NOT related to
    `parse_iso_ts` below despite the similar name (GLM review finding on this
    feature's own PR: the two used to be named `_parse_ts`/`parse_iso_ts`, a
    near-miss for a call-site mix-up since one returns the raw string and the
    other a parsed `datetime | None`). This one exists only to give
    `list.sort(key=...)` a lexicographically-sortable string (ISO-8601
    timestamps sort correctly as plain strings); it never parses anything."""
    ts = record.get("ts")
    return ts if isinstance(ts, str) else ""


def iterations_for_task(task_code: str) -> list[dict]:
    """Return run records for one task, oldest first, annotated with iteration numbers.

    All-digit GitHub-issue codes match both ``105`` and ``#105`` so a check
    under either spelling sees history recorded under the other (review-cli#389).
    """
    try:
        clean = normalize_task_code(task_code)
    except ValueError:
        return []
    if clean is None:
        return []
    keys = _github_issue_task_keys(clean)
    records = [r for r in _records() if r.get("task_code") in keys]
    records.sort(key=_record_ts_str)
    out: list[dict] = []
    for index, record in enumerate(records, start=1):
        item = dict(record)
        item["iteration"] = index
        out.append(item)
    return out


def _task_code_in(record: dict, keys: frozenset[str]) -> bool:
    """A record's task_code is one of `keys` (a hand-edited store may hold a list or object)."""
    code = record.get("task_code")
    return isinstance(code, str) and code in keys


def export_records(task_code: str) -> list[dict]:
    """The raw run-stats records of one task (both spellings of an all-digit code), oldest
    first, without the derived ``iteration`` number: what ``import_records`` takes on another
    machine so the iterations count there without being re-run (review-cli#659). OSError when
    the store cannot be read: an unreadable store is not an empty one."""
    try:
        path = stats_path()
    except RuntimeError as exc:
        raise OSError(f"no run-stats store: {exc}") from exc
    code = normalize_task_code(task_code)
    if code is None:
        return []
    keys = _github_issue_task_keys(code)
    records = [r for r in _strict_records(path) if _task_code_in(r, keys)]
    records.sort(key=_record_ts_str)
    return records


def _strict_records(path: Path) -> list[dict]:
    """Every record of the store, or OSError when it cannot be read exactly. `_load_records`
    answers "no history" for an unreadable store, which is right for a check but would switch
    deduplication off for an import."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        raise OSError(f"{path} is not valid UTF-8: {exc}") from exc
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "duration_seconds" in rec:
            out.append(rec)
    return out
