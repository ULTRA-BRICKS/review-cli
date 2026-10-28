"""Writing run records to the run-stats JSONL store (review-cli#721 split of ``reviewlib.stats``).

``record_run`` builds one record (``_run_record``: the always-present fields plus the
optional verdict / diff-identity / level / role fields, each present only when the caller
supplied it) and appends it best-effort (``_append_record``): never raises, O_APPEND so
concurrent runs cannot clobber each other's whole lines, 0600 on every write, a bool
result. Where the store lives and how it is read back is ``reviewlib.stats_store``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import portable
from .stats_identity import normalize_task_code, run_id_for
from .stats_store import stats_path

# Schema version for the JSONL records. Bump if the record shape changes
# incompatibly; readers tolerate unknown/missing fields and skip junk lines.
#
# v3 adds "passed": bool — the run's VERDICT (did it come back clean), keyed off
# the mode handler's own exit code (0 = every seat produced a usable verdict /
# board not degraded, nonzero = failure/degraded — see `_run_mode_with_stats` in
# cli.py). A record with no "passed" key has verdict UNKNOWN — either it predates
# v3, or it is a CURRENT record from a mode with no verdict to thread (e.g. `qa`,
# report-only by design). Readers must treat "no key" as unknown either way, never
# crash, and — for the quorum gate specifically — fail-closed (unknown counts as
# not-passed; see quorum_check).
#
# v4 adds "repo_id" (normalized repo identity) and "diff_files" (sorted list of
# file paths the reviewed diff touched), plus "diff_sha256" (an exact hash of the
# reviewed diff text, diagnostic-only). See the module docstring's "Diff-identity
# binding" section. A record with none of these predates v4 (or ran in a context
# with no resolvable repo/diff) — readers must treat that as identity UNKNOWN,
# not as a mismatch.
#
# v5 adds "prompt_tokens"/"output_tokens": int — aggregate REAL token usage summed
# across every backend call in the run (panel.py's call tally, fed from
# `ReviewResult.prompt_tokens`/`output_tokens`, which backends.py sets ONLY at the
# REST call sites that parse a provider's own usage payload — gemini/OpenAI-shape
# z.ai|commandcode|openrouter/anthropic). A CLI/agentic backend (codex, opencode,
# omp, claude in CLI mode) never sets these, so it contributes 0, same as any error
# path that never got a successful usage payload. 0 therefore means "no REST usage
# data for this run" — NOT necessarily "zero tokens spent" — and a record with no
# "prompt_tokens"/"output_tokens" key at all predates v4; both cases must be read
# the same way (unknown/absent), never treated as a confirmed zero-token run.
#
# Relationship to reviewlib.dashboard.tokenstats (#194): that module is a SEPARATE,
# complementary source — it derives token counts by re-parsing the per-call `.log`
# files on disk (`extract_usage_tokens`, scoped to `_REST_USAGE_BACKENDS` to avoid a
# real cross-contamination bug it already fixed) for the DETAILED per-model/
# per-harness `review stat` report. This module's `prompt_tokens`/`output_tokens`
# instead come from the SAME run's in-memory `ReviewResult` fields, aggregated once
# per run for the lighter-weight ETA/quorum-gate store. Both trace back to the same
# underlying provider usage payload, so they are two different aggregation
# granularities over one source of truth, not two independent measurements — but
# nothing in either module actively verifies the two stay in agreement; a scoping
# divergence between `_REST_USAGE_BACKENDS` and what actually sets `ReviewResult`'s
# token fields would drift silently. Worth a reconciliation check if this ever
# needs to be load-bearing rather than directional.
STATS_VERSION = 5


def _verdict_fields(clean_task: str | None, passed: bool | None) -> dict:
    """``task_code`` / ``passed``, each only when known (``None`` = omitted, not null)."""
    fields: dict = {}
    if clean_task is not None:
        fields["task_code"] = clean_task
    if passed is not None:
        fields["passed"] = bool(passed)
    return fields


def _diff_identity_fields(
    repo_id: str | None, diff_files: list[str] | None, diff_sha256: str | None
) -> dict:
    """The v4 diff-identity fields, each only when known. ``diff_files`` may be ``[]``
    (a real run with no diff), which is recorded, unlike ``None``."""
    fields: dict = {}
    if repo_id is not None:
        fields["repo_id"] = repo_id
    if diff_files is not None:
        fields["diff_files"] = list(diff_files)
    if diff_sha256 is not None:
        fields["diff_sha256"] = diff_sha256
    return fields


def _role_fields(
    models: list[str], roles: list[str] | None, usable_roles: list[str] | None
) -> dict:
    """``roles`` / ``usable_roles``, each only when supplied. A ``roles`` list whose
    length differs from ``models`` is dropped whole, not partially written."""
    fields: dict = {}
    if roles is not None and len(roles) == len(models):
        # Round-7 review finding (Fable): `_distinct_roles` (the ENFORCING counter
        # behind --min-roles) has no reference to `models` at all -- it just iterates
        # `roles` independently, unlike the advisory `_models_behind_role_coverage`,
        # which zips the two and so fails closed (under- never over-credits) on a
        # length mismatch. That makes the enforcement path fail OPEN if a future/
        # buggy/adversarial producer ever wrote more roles than models (e.g.
        # `models: ["codex"], roles: ["architect", "security"]`), inflating role
        # coverage from a single seat -- backwards for a self-merge-authority gate.
        # No CURRENT producer can write mismatched lengths (`panel.
        # FailoverOutcome.usable_models`/`usable_roles` are appended in lockstep;
        # `_ran_models`/`_ran_roles` build both from the same seat list in the same
        # order), so this is defense-in-depth at the one write chokepoint, not a
        # fix for a reachable bug: silently omit `roles` (not partially write it)
        # rather than persist a shape whose length lies about which seats it
        # actually describes.
        fields["roles"] = list(roles)
    if usable_roles is not None:
        # review-cli#137: no `models`-length alignment requirement (unlike `roles`
        # above) -- see `record_run`'s `usable_roles` docstring paragraph for why.
        fields["usable_roles"] = list(usable_roles)
    return fields


def _run_record(
    *,
    task_code: str | None,
    mode: str,
    models: list[str],
    duration_seconds: float,
    ok_count: int,
    fail_count: int,
    started: datetime | None,
    passed: bool | None,
    repo_id: str | None,
    diff_files: list[str] | None,
    diff_sha256: str | None,
    roles: list[str] | None,
    usable_roles: list[str] | None,
    prompt_tokens: int,
    output_tokens: int,
    run_id: str | None,
    level: str | None,
) -> dict:
    """The record ``record_run`` appends: the fields every run has, then the optional ones
    in their fixed order (verdict, diff identity, level, roles)."""
    try:
        clean_task = normalize_task_code(task_code)
    except ValueError:
        clean_task = None
    record = {
        "v": STATS_VERSION,
        "ts": (started or datetime.now(timezone.utc)).isoformat(),
        "mode": mode,
        "pool_size": len(models),
        "models": list(models),
        "duration_seconds": round(float(duration_seconds), 3),
        "ok_count": int(ok_count),
        "fail_count": int(fail_count),
        "prompt_tokens": int(prompt_tokens),
        "output_tokens": int(output_tokens),
    }
    # `run_id` keys overseer feedback (``review feedback <run_id>``, review-cli#feedback
    # -fill-rate) -- deterministic from (ts, mode, task_code) via `run_id_for` when the
    # caller doesn't supply one, so every record gets a stable id even from callers that
    # predate this field. The CLI's `_run_mode_with_stats` computes it itself BEFORE
    # calling `record_run` (same three inputs) so the id it prints in the completion
    # nudge is guaranteed to match what lands in the record, rather than depending on
    # the return value (which stays a plain bool to avoid breaking the existing
    # `record_run(...) is True/False` contract).
    record["run_id"] = run_id or run_id_for(record["ts"], mode, clean_task)
    record.update(_verdict_fields(clean_task, passed))
    record.update(_diff_identity_fields(repo_id, diff_files, diff_sha256))
    if level is not None:
        record["level"] = level
    record.update(_role_fields(models, roles, usable_roles))
    return record


def _append_record(record: dict) -> bool:
    """Append one JSON line to the store; False (never an exception) when it cannot."""
    try:
        p = stats_path()  # may raise RuntimeError on an unexpandable ~user / no HOME
        p.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND keeps concurrent runs from clobbering each other (each writes a
        # whole line). 0600 because we mirror the per-call-log privacy posture.
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # O_CREAT's 0600 only applies when WE create the file; a run-stats.jsonl that
            # already exists (or a $REVIEW_STATS_FILE the user pre-created) could carry
            # broader perms and keep them forever. fchmod on every write so the 0600
            # privacy guarantee holds for pre-existing files too.
            portable.fchmod(fd, 0o600)
            os.write(
                fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            )
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 — stats are best-effort; never abort a finished run
        # stats are best-effort. Called from a finally in the CLI, so a stats-only
        # failure (unwritable dir, an unexpandable $REVIEW_STATS_FILE that makes
        # stats_path() raise, ...) must NEVER turn an otherwise-completed review into
        # a crash.
        return False


def record_run(
    *,
    task_code: str | None = None,
    mode: str,
    models: list[str],
    duration_seconds: float,
    ok_count: int,
    fail_count: int,
    started: datetime | None = None,
    passed: bool | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
    diff_sha256: str | None = None,
    roles: list[str] | None = None,
    usable_roles: list[str] | None = None,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    run_id: str | None = None,
    level: str | None = None,
) -> bool:
    """Append one run record to the JSONL store. Best-effort: never raises.

    ``models`` is the list ACTUALLY dispatched (so ``pool_size`` reflects reality,
    not what was requested but skipped). ``duration_seconds`` must be the REAL
    wall-clock the caller timed with a monotonic clock. Returns True on a
    successful append, False if anything went wrong (unwritable dir, etc.) — the
    run must never fail because stats couldn't be persisted.

    ``roles`` (review-cli#221) is the board ROLE each USABLE seat that produced a
    verdict was reviewing under, for callers that carry per-seat role info
    (currently: the review-diff/visual board dispatch, whose seats each carry a
    ``REVIEW_ROLES`` key — see ``panel.FailoverOutcome.usable_roles``). It IS
    index-aligned with ``models`` for every current producer (``usable_models``/
    ``usable_roles`` are appended in the same loop iteration in
    ``panel.run_board_with_failover``; the CLI's explicit-board branches build
    both from the same seat list in the same order — see ``cli._ran_models``/
    ``_ran_roles``), and ``quorum_check``'s monoculture guard
    (``_models_behind_role_coverage``) relies on that alignment to credit only a
    model whose OWN role is valid, not merely one sharing a record with a
    valid-role seat. ``record_run`` itself DOES enforce this at write time (see
    the ``len(roles) == len(models)`` check below — a mismatched-length ``roles``
    is dropped, not partially written), so every record THIS module writes is
    aligned; the ``zip``-truncates-gracefully fallback in
    ``_models_behind_role_coverage`` matters only for pre-existing/externally-
    written records this function's own guard predates, which can still supply a
    mismatched pair (``zip`` truncates to the shorter list, under- never over-
    crediting — the fail-closed direction) — it just won't get full role credit.
    ``None``
    (the default) means "this mode/caller has no role concept" and the key is
    omitted entirely — same "unknown, not written as an empty list" convention as
    ``passed``/``repo_id`` — so a ``--min-roles`` gate can tell "never recorded a
    role" apart from "recorded zero roles". Modes with no board (quorum/just-ask/
    brainstorm/qa) never pass this.

    ``usable_roles`` (review-cli#137) is the SEAT-GRANULAR counterpart of ``roles``:
    the board ROLE of every seat that INDIVIDUALLY produced a usable verdict this
    run (``panel.FailoverOutcome.usable_roles`` — the SAME source ``roles`` reads
    for the reserve-failover board path), recorded regardless of whether a
    SIBLING seat in the same run hard-failed (quota exhaustion, a dead key, a
    timeout, …) and made the run's own ``passed`` False. This is what closes
    review-cli#137's adjacent seat-crediting gap: before it existed, an exact
    ``-m modelA,modelB,...`` board (no reserve) recorded ``roles`` for every
    PLANNED seat only when the run fully passed, and ``quorum_check`` only ever
    read ``roles`` from ``passed is True`` records — so ONE hard-failed seat
    zeroed role credit for the entire iteration, including a seat that genuinely
    produced a verdict. ``usable_roles`` has NO ``models``-length-alignment
    requirement (unlike ``roles``) — it is not paired seat-by-seat with a
    parallel ``models`` list, it is simply "the roles this run actually
    satisfied". ``None`` (the default) means "no seat-granular credit to report"
    and the key is omitted entirely, same convention as ``roles``/``passed``.

    ``level`` is the capability level the run used (review-cli#697: the diff review's level
    board, or the level an explicit ``--level`` picked for a moderator chain). ``None`` — a
    config roster, a mode with no level, every record written before levels existed — omits
    the key, same "unknown, not written as null" convention as ``passed``.

    ``passed`` is the run's VERDICT — the caller's own success/failure criterion
    (e.g. the mode handler's exit code), NOT ``ok_count``/``fail_count`` (those are
    per-BACKEND-CALL technical tallies, unrelated to whether the review came back
    clean). ``None`` means "caller has no verdict to report" and the field is
    omitted from the record entirely, so a reader can tell "unknown" apart from an
    explicit False. Every pre-v3 record looks like this (the field didn't exist
    yet), but so can a CURRENT v3 record from a mode with no verdict to thread
    (e.g. ``qa``, which is report-only — see the ``mode == "qa"`` branch in
    ``cli.py``'s ``_run_mode_with_stats``): a missing ``passed`` key means "verdict
    unknown", not specifically "written before v3".

    ``repo_id``/``diff_files``/``diff_sha256`` are the diff-identity fields (v4 —
    see the module docstring's "Diff-identity binding" section), each omitted from
    the record when ``None`` (same "unknown, not written as null" convention as
    ``passed``/``task_code``). ``diff_files`` MAY be an empty list (a real run with
    no diff, e.g. a diff-less ``just-ask``) — that is recorded as ``[]``, distinct
    from omitting the key entirely.

    ``prompt_tokens``/``output_tokens`` are aggregate REAL token counts for the run
    (see the v5 STATS_VERSION comment above) — default 0, which means "no REST usage
    data", not a confirmed zero-token run.

    ``run_id`` lets a caller pin the exact id that will be written (see the `run_id`
    field assignment below); ``None`` (the default) derives one from
    ``(ts, mode, task_code)`` via ``run_id_for``.
    """
    record = _run_record(
        task_code=task_code,
        mode=mode,
        models=models,
        duration_seconds=duration_seconds,
        ok_count=ok_count,
        fail_count=fail_count,
        started=started,
        passed=passed,
        repo_id=repo_id,
        diff_files=diff_files,
        diff_sha256=diff_sha256,
        roles=roles,
        usable_roles=usable_roles,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        run_id=run_id,
        level=level,
    )
    return _append_record(record)
