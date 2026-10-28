"""Importing run-stats records made on another machine (review-cli#659).

Split out of ``reviewlib.stats`` (review-cli#721). ``import_records`` appends records to
the local store exactly once each: a record already present (field for field, the
``remote_host`` tag aside) is skipped, a line that is not a record is rejected, and the
whole read-check-append runs under a sidecar lock so two imports of the same records
never interleave.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from . import portable
from .stats_identity import _github_issue_task_keys, normalize_task_code
from .stats_store import _strict_records, _task_code_in, stats_path


def _record_identity(record: dict) -> str:
    """Two records are the same iteration when they match field for field, whichever host
    imported them: the import tag is not part of the identity."""
    return json.dumps(
        {k: v for k, v in record.items() if k != "remote_host"},
        sort_keys=True,
        separators=(",", ":"),
    )


def _ends_mid_line(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _write_all(fd: int, data: bytes) -> None:
    """os.write may accept fewer bytes (full disk, quota): keep going until all landed. A
    concurrent `record_run` append (not under the import lock) could still land between two
    partial writes; short writes are rare on local disks and the torn line is then skipped
    by every reader, so the cost is one retry of the import."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _importable_record(line: str, keys: frozenset[str] | None) -> dict | None:
    """The record a non-blank import line carries, or None when the line is not one: not
    JSON, not an object, no ``duration_seconds``, or (with ``keys``) another task's."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or "duration_seconds" not in rec:
        return None
    if keys is not None and not _task_code_in(rec, keys):
        return None
    return rec


def _collect_fresh(
    lines: Iterable[str],
    seen: set[str],
    keys: frozenset[str] | None,
    remote_host: str | None,
) -> tuple[list[str], int, int]:
    """``(new JSONL lines, skipped, rejected)`` for ``lines``; ``seen`` (the identities
    already stored) grows with every record taken, so a repeat inside the batch is skipped."""
    fresh: list[str] = []
    skipped = rejected = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rec = _importable_record(line, keys)
        if rec is None:
            rejected += 1
            continue
        identity = _record_identity(rec)
        if identity in seen:
            skipped += 1
            continue
        seen.add(identity)
        if remote_host and "remote_host" not in rec:
            rec["remote_host"] = remote_host
        fresh.append(json.dumps(rec, separators=(",", ":")))
    return fresh, skipped, rejected


def _append_fresh(path: Path, fresh: list[str]) -> None:
    """Append ``fresh`` (one JSONL line each) to the store, 0600, O_APPEND."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        portable.fchmod(fd, 0o600)
        payload = ("\n".join(fresh) + "\n").encode("utf-8")
        if _ends_mid_line(
            path
        ):  # a torn earlier write must not swallow our first record
            payload = b"\n" + payload
        _write_all(fd, payload)
    finally:
        os.close(fd)


def import_records(
    lines: Iterable[str],
    *,
    remote_host: str | None = None,
    task_code: str | None = None,
) -> dict:
    """Append run-stats records made on another machine to the local store, once each.

    ``lines`` are JSONL as ``review task CODE --export`` prints them. A line that is not a
    record (not JSON, not an object, no ``duration_seconds``) is rejected; a record already in
    the store is skipped, so importing twice never double-counts an iteration. ``remote_host``
    tags records that carry no tag yet; ``task_code`` keeps only that task's records (the rest
    count as rejected). The records keep their ``repo_id`` / ``diff_files``,
    so ``review task --check`` verifies them against the local checkout exactly as it does
    its own. Returns ``{"imported": n, "skipped": n, "rejected": n}``; raises OSError when
    the store cannot be written."""
    try:
        path = stats_path()  # RuntimeError: no HOME / unexpandable ~user
        path.parent.mkdir(parents=True, exist_ok=True)
    except RuntimeError as exc:
        raise OSError(f"no run-stats store: {exc}") from exc
    keys = (
        _github_issue_task_keys(normalize_task_code(task_code)) if task_code else None
    )
    # The read-check-append must not interleave with another import of the same records
    # (a retried `review-remote` import next to `--import-from`): a sidecar lock held across it.
    lock_fd = os.open(str(path) + ".import.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        portable.lock_file(lock_fd, blocking=True)
        seen = {_record_identity(r) for r in _strict_records(path)}
        fresh, skipped, rejected = _collect_fresh(lines, seen, keys, remote_host)
        if fresh:
            _append_fresh(path, fresh)
    finally:
        os.close(lock_fd)  # closing the descriptor releases the lock
    return {"imported": len(fresh), "skipped": skipped, "rejected": rejected}
