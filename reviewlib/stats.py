"""Per-run statistics + a startup ETA, so agents never short-timeout the tool.

review-cli runs are multi-model / multi-round and take MINUTES. Agents that wrap
the command in a short shell `timeout` kill the run before it can finish (most
painfully a brainstorm, which only produces its synthesis at the very end). To
make the expected duration *visible* up front, every run:

  1. appends a structured stat record to a JSONL store when it finishes, and
  2. prints a one-line ETA to stderr at dispatch, computed from past runs of the
     same shape.

Why a NEW store and not the dashboard's session reconstruction
--------------------------------------------------------------
The dashboard (``reviewlib.dashboard.parser``) reconstructs "sessions" by
time-clustering the per-CALL ``*.log`` files. That reader cannot, by its own
admission, recover (mode, pool_size, real wall-clock) cleanly:

  * MODE is *inferred* from the round shape — a plain ``review`` and a
    ``--just-ask`` are both a single r0 call and indistinguishable; a multi-model
    review and a ``--quorum`` both look like a "panel". The ETA must key on the
    EXACT mode (a brainstorm of 4 is nothing like a plain review of 4), so an
    inferred mode is not good enough.
  * POOL SIZE is "distinct backends seen", which a brainstorm (same backend in
    several persona slots) undercounts.
  * DURATION is a proxy (filename stamp -> file mtime), which the parser itself
    caps and warns can be ballooned by an out-of-band touch.

So this module records the GROUND TRUTH the run already knows — the real mode,
the real pool size (models actually dispatched), and the real wall-clock from a
monotonic clock — into its own append-only JSONL. The dashboard's per-call logs
are untouched and keep serving their richer drill-down; this store serves the ETA.

Privacy: the store holds model NAMES, plus (since v4 — see "Diff-identity
binding" below) a normalized REPO IDENTIFIER (a remote URL with credentials
stripped, or a local absolute path when there is no remote) and the list of FILE
PATHS touched by the reviewed diff. It never holds prompts, diff BODIES, or keys.
It is created 0600 (same posture as the per-call logs, which can hold secrets)
even though it shouldn't carry any.

Diff-identity binding (task-code quorum-pollution fix)
--------------------------------------------------------
The self-merge-authority quorum gate (`quorum_check` / `review task CODE
--check`) counts PASSED iterations keyed purely by TASK CODE STRING. Three real
incidents in one session (2026-08-11) showed that string alone is not enough:
task-code reuse across unrelated repos/PRs/typos let one diff's real reviews
silently count toward a completely different diff's quorum — a wrong-repo
review, a deliberate task-code swap between two PRs sharing a parent ticket, and
years of unrelated cross-repo history piling up under one shared code. In all
three the reviewed content had nothing to do with the diff being merged.

So every new record carries `repo_id` (which repo the review ran in) and
`diff_files` (which files the reviewed diff touched). `quorum_check` can then be
handed the CURRENT repo/diff context and flag/exclude iterations that don't
match — see quorum_check's own docstring for the exact matching rules. This is
INTENTIONALLY NOT a diff content-hash equality check: the whole point of
multiple review iterations is that the code changes between them (findings get
fixed, then re-reviewed), so the diff TEXT is expected to differ round to
round — only the REPO and the FILE SET are expected to stay stable across a
task's legitimate iterations. `diff_sha256` is recorded too (an exact hash of
the reviewed diff text) purely as an additional diagnostic signal for
`--detail`/debugging, not as part of the mismatch-detection gate.

A repository that was renamed or moved hosts stays one `repo_id`: its former
remotes map to the current one through `_REPO_ID_ALIASES`. A caller whose own
diff says nothing about the reviewed change (`gh ship` checking a PR from the
primary checkout) passes the repo alone, with no file set, so only the REPO is
compared (`review task --check --verify-repo-only`, review-cli#589).

Threat-model boundary: this store is a local, self-reported JSONL file — nothing
stops a caller with write access from appending a FRESH record with a fabricated
`repo_id`/`diff_files` that happen to match the current check context (the test
suite does exactly this, deliberately, to simulate history without a real
`review diff` run). This fix closes the "wrong string still matches real but
unrelated history" bug class the 3 incidents actually were — task-code reuse,
typos, and shared-parent-ticket confusion — NOT a cryptographic guarantee against
a fully malicious agent minting a convincing fake record from scratch. Don't
oversell it as the latter in downstream docs/messaging.

Old records (pre-v4) simply lack `repo_id`/`diff_files`/`diff_sha256` — readers
must treat that as "identity unknown, can't verify" (still counted, per the
gate's existing backward-compat contract for missing fields) rather than
crashing or auto-failing them.

Module map (review-cli#721)
---------------------------
This module is the public face and the stable import path; the implementation lives in
focused modules, each re-exported below with its names unchanged:

  * ``stats_identity``        -- diff hash/files, repo identity + aliases, task codes, run id
  * ``stats_store``           -- store location and reading (lenient / strict), per-task views
  * ``stats_writer``          -- ``record_run`` (best-effort O_APPEND 0600 append), STATS_VERSION
  * ``stats_import``          -- ``import_records`` (exactly-once, locked, torn-line safe)
  * ``stats_quorum_evidence`` -- iteration classification and distinct model/role counting
  * ``stats_quorum_model``    -- ``QuorumRequest`` / ``QuorumHistory`` and early-reject payload
  * ``stats_quorum_fallback`` -- the bare-check zero-role fallback and role-tracking gap
  * ``stats_quorum``          -- ``quorum_check`` and its payload assembly
  * ``stats_summaries``       -- ``task_summaries`` / ``mode_summary`` / ``repo_summary`` / lookups

The startup ETA (``estimate_eta`` / ``eta_line`` / ``announce_eta``) and ``fmt_duration``
live here. Names a caller or test rebinds (``_load_records``, ``estimate_eta``) are looked
up on THIS module, so rebinding them here keeps taking effect.
"""

from __future__ import annotations

import sys

from .stats_identity import (
    _DIFF_GIT_HEADER_RE,
    _GITHUB_ISSUE_TASK_RE,
    _REPO_ID_ALIASES,
    _SCP_STYLE_REMOTE_RE,
    _TASK_CODE_RE,
    _comparable_repo_id,
    _github_issue_task_keys,
    _github_issue_twin,
    _recorded_repo_id,
    diff_content_hash,
    extract_diff_files,
    normalize_repo_remote,
    normalize_task_code,
    run_id_for,
)
from .stats_import import (
    _ends_mid_line,
    _record_identity,
    _write_all,
    import_records,
)
from .stats_quorum import _finalize_quorum_result, quorum_check
from .stats_quorum_evidence import (
    _MISMATCH_DETAILS_CAP,
    _ROLE_TRACKING_CUTOFF,
    _all_iterations_predate_role_tracking,
    _classify_iteration_identity,
    _current_files_frozenset,
    _distinct_models,
    _distinct_roles,
    _foreign_repo_iterations,
    _has_any_role_data,
    _iteration_predates_role_tracking,
    _iterations_excluding_mismatched,
    _models_behind_role_coverage,
    _seat_credited_roles,
    _sort_passed_iterations_into_buckets,
    parse_iso_ts,
)
from .stats_quorum_model import _DEFAULT_QUORUM_FLOOR, _store_unreadable_error
from .stats_store import (
    _load_records,
    _record_ts_str,
    _strict_records,
    _task_code_in,
    export_records,
    iterations_for_task,
    stats_path,
)
from .stats_summaries import (
    _record_ts_ge,
    find_by_run_id,
    load_run_records,
    mode_summary,
    repo_summary,
    task_summaries,
)
from .stats_writer import STATS_VERSION, record_run

# Everything this module exported before the split, public and private: callers import the
# public names, and tests rebind or read the private ones here (see the module docstring).
__all__ = [
    "STATS_VERSION",
    "_DEFAULT_QUORUM_FLOOR",
    "_DIFF_GIT_HEADER_RE",
    "_GITHUB_ISSUE_TASK_RE",
    "_MISMATCH_DETAILS_CAP",
    "_REPO_ID_ALIASES",
    "_ROLE_TRACKING_CUTOFF",
    "_SCP_STYLE_REMOTE_RE",
    "_TASK_CODE_RE",
    "_all_iterations_predate_role_tracking",
    "_classify_iteration_identity",
    "_comparable_repo_id",
    "_current_files_frozenset",
    "_distinct_models",
    "_distinct_roles",
    "_ends_mid_line",
    "_finalize_quorum_result",
    "_foreign_repo_iterations",
    "_github_issue_task_keys",
    "_github_issue_twin",
    "_has_any_role_data",
    "_iteration_predates_role_tracking",
    "_iterations_excluding_mismatched",
    "_load_records",
    "_models_behind_role_coverage",
    "_record_identity",
    "_record_ts_ge",
    "_record_ts_str",
    "_recorded_repo_id",
    "_seat_credited_roles",
    "_sort_passed_iterations_into_buckets",
    "_store_unreadable_error",
    "_strict_records",
    "_task_code_in",
    "_write_all",
    "announce_eta",
    "diff_content_hash",
    "estimate_eta",
    "eta_line",
    "export_records",
    "extract_diff_files",
    "find_by_run_id",
    "fmt_duration",
    "import_records",
    "iterations_for_task",
    "load_run_records",
    "mode_summary",
    "normalize_repo_remote",
    "normalize_task_code",
    "parse_iso_ts",
    "quorum_check",
    "record_run",
    "repo_summary",
    "run_id_for",
    "stats_path",
    "task_summaries",
]


def fmt_duration(seconds: float) -> str:
    """Compact human duration: ``6m12s``, ``47s``, ``1h03m``. Always >= ``0s``."""
    total = round(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def estimate_eta(mode: str, pool_size: int) -> dict | None:
    """Average past wall-clock for a run of this shape.

    Keyed PRIMARILY on (mode, pool_size) — a brainstorm of 4 is nothing like a
    plain review of 4 — then falls back to pool_size alone (any mode), then to
    None when there is no usable history at all. Returns a dict
    ``{"avg_seconds", "samples", "basis"}`` where ``basis`` is ``"mode+pool"`` or
    ``"pool"``; None means "no history". Never raises — an unreadable store yields
    None and the caller prints the no-history line.
    """
    records = _load_records()
    if not records:
        return None

    def _avg(matching: list[dict]) -> float | None:
        durs = [
            float(r["duration_seconds"])
            for r in matching
            if isinstance(r.get("duration_seconds"), (int, float))
            and r["duration_seconds"] >= 0
        ]
        return (sum(durs) / len(durs)) if durs else None

    exact = [
        r for r in records if r.get("mode") == mode and r.get("pool_size") == pool_size
    ]
    avg = _avg(exact)
    if avg is not None:
        return {"avg_seconds": avg, "samples": len(exact), "basis": "mode+pool"}

    by_pool = [r for r in records if r.get("pool_size") == pool_size]
    avg = _avg(by_pool)
    if avg is not None:
        return {"avg_seconds": avg, "samples": len(by_pool), "basis": "pool"}

    return None


def eta_line(mode: str, pool_size: int) -> str:
    """One concise stderr line shown at dispatch. Always returns a string.

    With history: ``[review] pool=4 (brainstorm) — typically ~6m12s based on 12
    past runs of this size; do NOT timeout.`` Without history (or an unreadable
    store): a no-data line that still warns about the multi-round / minutes-long
    nature, so the agent never short-timeouts a first-of-its-kind run either.
    """
    try:
        eta = estimate_eta(mode, pool_size)
    except Exception:  # noqa: BLE001 — stats must never block a run
        eta = None
    if eta is None:
        return (
            f"[review] pool={pool_size} ({mode}) — no history yet for this size; "
            "this is multi-model / multi-round, expect MINUTES. Do NOT timeout."
        )
    avg = fmt_duration(eta["avg_seconds"])
    n = eta["samples"]
    plural = "run" if n == 1 else "runs"
    if eta["basis"] == "mode+pool":
        basis = f"based on {n} past {plural} of this size"
    else:
        basis = f"based on {n} past {plural} of pool={pool_size} (any mode)"
    return f"[review] pool={pool_size} ({mode}) — typically ~{avg} {basis}; do NOT timeout."


def announce_eta(mode: str, pool_size: int, stream=None) -> None:
    """Print the ETA line to stderr (or ``stream``) at dispatch. Never raises."""
    try:
        print(eta_line(mode, pool_size), file=stream or sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — never let the announcement abort a run
        return
