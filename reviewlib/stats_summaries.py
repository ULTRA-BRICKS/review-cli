"""Aggregates over the run-stats history (review-cli#721 split of ``reviewlib.stats``).

``task_summaries`` / ``mode_summary`` / ``repo_summary`` are the groupings behind ``review
task`` and ``review stat``; ``find_by_run_id`` and ``load_run_records`` are the lookups
beside them. Every reader takes its records through ``stats_store._records`` so a rebound
``reviewlib.stats._load_records`` still reaches them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .stats_identity import _comparable_repo_id, run_id_for
from .stats_store import _records


def _add_duration(group: dict, record: dict) -> None:
    """Add the record's ``duration_seconds`` to the group's when it is a number."""
    dur = record.get("duration_seconds")
    if isinstance(dur, (int, float)):
        group["duration_seconds"] += float(dur)


def _add_counts(group: dict, record: dict, keys: tuple[str, ...]) -> None:
    """Add each integer ``keys`` value of the record to the group's counter."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, int):
            group[key] += value


def _empty_task_group(code: str) -> dict:
    return {
        "task_code": code,
        "iterations": 0,
        "models": [],
        "modes": set(),
        "first_ts": None,
        "last_ts": None,
        "duration_seconds": 0.0,
        "ok_count": 0,
        "fail_count": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
    }


def _widen_ts_range(group: dict, ts: object) -> None:
    """Stretch the group's ``first_ts``/``last_ts`` to cover a string ``ts``."""
    if not isinstance(ts, str):
        return
    if group["first_ts"] is None or ts < group["first_ts"]:
        group["first_ts"] = ts
    if group["last_ts"] is None or ts > group["last_ts"]:
        group["last_ts"] = ts


def _add_task_record(group: dict, record: dict) -> None:
    group["iterations"] += 1
    _widen_ts_range(group, record.get("ts"))
    mode = record.get("mode")
    if isinstance(mode, str) and mode:
        group["modes"].add(mode)
    for model in record.get("models") or []:
        if isinstance(model, str) and model not in group["models"]:
            group["models"].append(model)
    _add_duration(group, record)
    _add_counts(
        group, record, ("ok_count", "fail_count", "prompt_tokens", "output_tokens")
    )


def task_summaries() -> list[dict]:
    """Aggregate run-stats by task code, newest task first."""
    groups: dict[str, dict] = {}
    for record in _records():
        code = record.get("task_code")
        if not isinstance(code, str) or not code:
            continue
        _add_task_record(groups.setdefault(code, _empty_task_group(code)), record)
    summaries = []
    for group in groups.values():
        item = dict(group)
        item["modes"] = sorted(item["modes"])
        item["duration_seconds"] = round(item["duration_seconds"], 3)
        summaries.append(item)
    summaries.sort(key=lambda item: item.get("last_ts") or "", reverse=True)
    return summaries


def find_by_run_id(run_id: str) -> dict | None:
    """Look up one run-stats record by its `run_id` (the id `review feedback` takes as
    its positional argument). Returns the FIRST match. A record written before this
    field existed has no `run_id` key at all -- for THOSE, recompute it on the fly via
    `run_id_for(ts, mode, task_code)` (the exact same deterministic formula
    `record_run` uses for a record with no explicit `run_id` passed in), honoring
    `run_id_for`'s own docstring promise that a caller who knows those three fields
    can always recompute a pre-existing record's id. Records that DO carry `run_id`
    are matched directly without recomputing (cheaper, and correct even for the rare
    caller-pinned id that wouldn't match the deterministic formula). Ties across two
    DIFFERENT records are astronomically unlikely (see `run_id_for`'s docstring); this
    returns the first match either way."""
    for record in _records():
        if "run_id" in record:
            if record["run_id"] == run_id:
                return record
            continue
        ts = record.get("ts")
        mode = record.get("mode")
        if (
            isinstance(ts, str)
            and isinstance(mode, str)
            and run_id_for(ts, mode, record.get("task_code")) == run_id
        ):
            return record
    return None


def _record_ts_ge(record: dict, since: datetime | None) -> bool:
    if since is None:
        return True
    ts = record.get("ts")
    if not isinstance(ts, str):
        return False
    # Review finding (Codex): `datetime.fromisoformat` only accepts a trailing `Z`
    # (Zulu/UTC) shorthand from Python 3.11 onward, but this project declares
    # `requires-python = ">=3.9"` -- `cli._resolve_stat_since` already normalizes
    # this exact shorthand for `--since`'s OWN input for that reason; this reader
    # needs the identical normalization for a record's `ts`, since no CURRENT
    # producer writes `Z` (`record_run`'s `.isoformat()` always emits `+00:00`) but a
    # hand-written/future/external record legitimately could.
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= since


def load_run_records() -> list[dict]:
    """Public read of the whole run-stats history, so a caller needing several
    aggregates (`review stat`'s by_mode + by_repo_top) parses the JSONL once and hands
    the list to each `*_summary(records=...)`."""
    return _records()


def _empty_mode_group(mode: str) -> dict:
    return {
        "mode": mode,
        "runs": 0,
        "duration_seconds": 0.0,
        "ok_count": 0,
        "fail_count": 0,
        "pool_size_total": 0,
    }


def _add_mode_record(group: dict, record: dict) -> None:
    group["runs"] += 1
    _add_duration(group, record)
    _add_counts(group, record, ("ok_count", "fail_count"))


def _mode_row(group: dict) -> dict:
    """The reported row for one mode group: rounded duration, mean pool size, fail rate."""
    item = dict(group)
    item["duration_seconds"] = round(item["duration_seconds"], 3)
    item["avg_pool_size"] = (
        round(item["pool_size_total"] / item["runs"], 1) if item["runs"] else 0.0
    )
    calls = item["ok_count"] + item["fail_count"]
    item["fail_rate"] = round(item["fail_count"] / calls, 3) if calls else None
    del item["pool_size_total"]
    return item


def mode_summary(
    since: datetime | None = None, records: list[dict] | None = None
) -> list[dict]:
    """Aggregate run-stats by MODE (review/quorum/brainstorm/just-ask/qa) — answers
    "which mode is actually burning the time/calls", which the per-harness/per-model
    `review stat` report (sourced from the call logs, not this store) never joins
    against. One row per mode, busiest (by total duration) first. `records` is a
    pre-loaded `load_run_records()` result (default: read the store).
    """
    groups: dict[str, dict] = {}
    for record in _records() if records is None else records:
        if not _record_ts_ge(record, since):
            continue
        mode = record.get("mode")
        if not isinstance(mode, str) or not mode:
            continue
        group = groups.setdefault(mode, _empty_mode_group(mode))
        _add_mode_record(group, record)
        pool = record.get("pool_size")
        if isinstance(pool, int):
            group["pool_size_total"] += pool
    summaries = [_mode_row(group) for group in groups.values()]
    summaries.sort(key=lambda item: item["duration_seconds"], reverse=True)
    return summaries


def repo_summary(
    since: datetime | None = None,
    top: int = 10,
    records: list[dict] | None = None,
) -> list[dict]:
    """Aggregate run-stats by `repo_id` (review-cli#195/#213's diff-identity field) —
    answers "which repo is actually generating the volume". Only ~26% of historical
    records carry `repo_id` at all (pre-v4, or a diff-less mode) — those are excluded
    here rather than lumped into a misleading "unknown" bucket, same convention as
    `quorum_check`'s identity handling. Busiest (by run count) `top` repos first.

    `top <= 0` returns EVERY repo, uncapped — the same "<=0 means no cap" convention
    `_resolve_stat_since` already uses for `--days`. The CLI's own `--top` clamps to
    `>= 1` before calling this (review-cli#355 review finding: this function's OWN
    default contract is "the caller decides whether 0 means capped-to-nothing or
    unbounded" — clamping here too would silently take away the "give me all of them"
    use a direct library caller might want). `records` is a pre-loaded
    `load_run_records()` result (default: read the store)."""
    groups: dict[str, dict] = {}
    for record in _records() if records is None else records:
        if not _record_ts_ge(record, since):
            continue
        repo_id = record.get("repo_id")
        if not isinstance(repo_id, str) or not repo_id:
            continue
        # A moved/renamed repo (`_REPO_ID_ALIASES`) counts under its current id, the
        # same identity `quorum_check` compares on, instead of splitting in two.
        repo_id = _comparable_repo_id(repo_id)
        group = groups.setdefault(
            repo_id,
            {"repo_id": repo_id, "runs": 0, "duration_seconds": 0.0, "fail_count": 0},
        )
        group["runs"] += 1
        dur = record.get("duration_seconds")
        if isinstance(dur, (int, float)):
            group["duration_seconds"] += float(dur)
        fail = record.get("fail_count")
        if isinstance(fail, int):
            group["fail_count"] += fail
    summaries = []
    for group in groups.values():
        item = dict(group)
        item["duration_seconds"] = round(item["duration_seconds"], 3)
        summaries.append(item)
    summaries.sort(key=lambda item: item["runs"], reverse=True)
    if top > 0:
        summaries = summaries[:top]
    return summaries
