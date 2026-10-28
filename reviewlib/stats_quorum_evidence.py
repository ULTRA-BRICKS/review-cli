"""Iteration evidence the quorum gate counts (review-cli#721 split of ``reviewlib.stats``).

Per-iteration classification and counting, no store access: diff-identity buckets
(verified / mismatched / unverifiable, foreign-repo detail), the distinct-model and
board-role counters (seat-granular credit, valid ``REVIEW_ROLES`` only), and the
role-tracking cutoff that separates genuinely pre-role history from history that merely
never chose a role-recording mode. ``reviewlib.stats.quorum_check`` composes them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import REVIEW_ROLES
from .stats_identity import _comparable_repo_id, _recorded_repo_id

# Cap on `quorum_check`'s `mismatch_details` AND `foreign_repo_iterations` lists
# (the COUNTS in `excluded_mismatched_iterations` / `excluded_foreign_repo_iterations`
# are always the true totals, uncapped) -- a task with thousands of polluted
# iterations (the HYP-858 shape this feature targets) must not balloon
# `--check --json` into a multi-MB payload.
_MISMATCH_DETAILS_CAP = 50


def _classify_iteration_identity(
    item: dict, repo_id: str, current_files: frozenset[str]
) -> tuple[str, str | None]:
    """Classify one iteration against the CURRENT check context.

    Despite the name of its main caller (``_sort_passed_iterations_into_buckets``),
    this function itself never reads ``item.get("passed")`` — it purely compares
    recorded repo/diff identity, so it is safe to reuse on iterations of ANY
    verdict (see ``_iterations_excluding_mismatched``, which does exactly that for
    the bare-check zero-role fallback's "has any role data" check).

    ``current_files`` is a pre-built ``frozenset`` (hoisted ONCE by each caller
    via ``_current_files_frozenset``, not rebuilt per iteration — a caller's loop
    can run once per PASSED iteration (``_sort_passed_iterations_into_buckets``)
    or once per iteration of ANY verdict (``_iterations_excluding_mismatched``),
    either of which, for the exact "years of history" pollution shape this
    feature targets, can be thousands; review finding on this feature's own PR).

    Returns ``(bucket, reason)``:
      * ``"verified"`` — the iteration's recorded repo matches ``repo_id``, and
        either it has no recorded file set, ``current_files`` (the current
        context) is empty, or the two file sets share at least one file.
        ``reason`` is None.
      * ``"mismatched"`` — the recorded repo differs (``reason="repo_mismatch"``),
        or the repo matches but BOTH file sets are non-empty and share NO file at
        all (``reason="diff_mismatch"``) — this is the pattern all three real
        incidents shared: iterations reviewing manifestly different content
        counting toward an unrelated task's quorum.
      * ``"unverifiable"`` — the iteration predates diff-identity recording (no
        ``repo_id`` on the record); can't be verified either way, ``reason`` None.
    """
    recorded_repo = _recorded_repo_id(item)
    if recorded_repo is None:
        return "unverifiable", None
    if recorded_repo != _comparable_repo_id(repo_id):
        return "mismatched", "repo_mismatch"
    item_files = item.get("diff_files")
    if (
        current_files
        and isinstance(item_files, list)
        and item_files
        and current_files.isdisjoint(item_files)
    ):
        return "mismatched", "diff_mismatch"
    return "verified", None


def _current_files_frozenset(diff_files: list[str] | None) -> frozenset[str]:
    """Shared ``diff_files -> frozenset`` construction for diff-identity
    classification (``frozenset(diff_files or ())``) — used by every caller of
    `_classify_iteration_identity` so independent call sites can never diverge on
    how a ``None``/absent ``diff_files`` is treated (Fable review finding: two
    call sites each inlining this expression separately risked silent drift if
    one were ever edited without the other)."""
    return frozenset(diff_files or ())


def _sort_passed_iterations_into_buckets(
    passed_iterations: list[dict], repo_id: str | None, diff_files: list[str] | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split PASSED iterations into (verified, mismatched_detail_dicts, unverifiable).

    When ``repo_id`` is None (the caller supplied no check context — e.g. a direct
    library call with no cwd to resolve), verification is skipped entirely: every
    passed iteration is "verified" as-is, matching this function's pre-diff-identity
    behavior exactly (no new keys, no filtering) for backward compatibility.
    """
    if repo_id is None:
        return list(passed_iterations), [], []
    current_files = _current_files_frozenset(diff_files)
    verified: list[dict] = []
    mismatched: list[dict] = []
    unverifiable: list[dict] = []
    for item in passed_iterations:
        bucket, reason = _classify_iteration_identity(item, repo_id, current_files)
        if bucket == "verified":
            verified.append(item)
        elif bucket == "mismatched":
            # recorded_diff_files is the actual evidence for a "diff_mismatch" (the
            # repo already matched by definition in that case, so recorded_repo_id
            # alone tells an operator nothing about WHY it was excluded — review
            # finding on this feature's own PR).
            mismatched.append(
                {
                    "iteration": item.get("iteration"),
                    "reason": reason,
                    "recorded_repo_id": item.get("repo_id"),
                    "recorded_diff_files": item.get("diff_files"),
                    "ts": item.get("ts"),
                }
            )
        else:
            unverifiable.append(item)
    return verified, mismatched, unverifiable


def _distinct_models(items: list[dict]) -> list[str]:
    """Sorted, deduped model list across a set of run-stats iterations."""
    models: list[str] = []
    for item in items:
        for model in item.get("models") or []:
            if isinstance(model, str) and model not in models:
                models.append(model)
    models.sort()
    return models


def _distinct_roles(items: list[dict]) -> list[str]:
    """Sorted, deduped board-role list across a set of run-stats iterations
    (review-cli#221 --min-roles). Mirrors ``_distinct_models``, reading the
    ``roles`` key instead of ``models`` — with one deliberate difference: only
    roles that are a real ``REVIEW_ROLES`` key count.

    An iteration with no recorded ``roles`` key (a mode with no role concept, or
    history that predates this field) contributes nothing — it neither helps nor
    hurts a ``--min-roles`` count, same as a pre-v4 record and ``repo_id`` in the
    diff-identity check.

    Codex review finding: an unrecognized/typo'd role string can still reach a
    recorded run's ``roles`` list from a path this counter can't assume is
    validated — a quorum/brainstorm PERSONA name (deliberately NOT a
    ``REVIEW_ROLES`` key), or history logged before review-cli#422 made an
    unrecognized config ``board:`` role a hard ``BoardConfigError`` at
    composition time (``config._parse_board_entry``) instead of a degraded
    generic-prompt seat. Counting either case here would let N seats that all
    reviewed under the SAME generic prompt (or under N non-lens personas)
    satisfy a ``--min-roles N`` self-merge-authority gate as if they were N
    genuinely distinct facets — exactly the "distinct lenses" contract this
    gate exists to enforce. Filtering to ``REVIEW_ROLES`` keeps the count
    honest: only a role that actually carries its own lens text can ever count
    as covered."""
    roles: list[str] = []
    for item in items:
        for role in item.get("roles") or []:
            if isinstance(role, str) and role in REVIEW_ROLES and role not in roles:
                roles.append(role)
    roles.sort()
    return roles


def _seat_credited_roles(items: list[dict]) -> list[str]:
    """Sorted, deduped board-role list crediting each SEAT's OWN role independently
    of the record's overall run ``passed`` (review-cli#137).

    Reads the seat-granular ``usable_roles`` key (the roles of seats that
    INDIVIDUALLY produced a usable verdict this run — see ``record_run``'s
    ``usable_roles`` param) ahead of the legacy run-granular ``roles`` key. Falls
    back to ``roles``, but ONLY when both (a) the record predates ``usable_roles``
    (a pre-review-cli#137 record never wrote it) AND (b) the record's own
    ``passed`` is True — a pre-#137 record never separated per-seat credit from
    the run's own verdict, so a pre-#137 FAILED record's ``roles`` cannot be
    trusted for partial credit: an exact `-m` board with no reserve used to record
    every PLANNED seat's role there whenever the run passed, and record nothing
    reliable per-seat when it didn't.

    Mirrors ``_distinct_roles`` otherwise: only a real ``REVIEW_ROLES`` key
    counts; an iteration with neither key contributes nothing (same fail-closed-
    by-omission convention)."""
    roles: list[str] = []
    for item in items:
        if "usable_roles" in item:
            item_roles = item.get("usable_roles") or []
        elif item.get("passed") is True:
            item_roles = item.get("roles") or []
        else:
            item_roles = []
        for role in item_roles:
            if isinstance(role, str) and role in REVIEW_ROLES and role not in roles:
                roles.append(role)
    roles.sort()
    return roles


def _has_any_role_data(items: list[dict]) -> bool:
    """True iff at least one item recorded ANY role data (a non-empty ``roles``
    list) -- used by `quorum_check`'s bare-check zero-role fallback (Alex's
    review-cli#246 follow-up, see the module-level constant `_DEFAULT_QUORUM_FLOOR`
    usage below) to tell "genuinely never recorded a role" apart from "recorded
    some roles, just not enough to meet the floor". Only the former falls back to
    the old model-counting default -- the latter must fail on its own merits via
    role-counting, exactly like PR #246 left it.

    Deliberately checks for a non-empty ``roles`` LIST, not whether it contains a
    valid ``REVIEW_ROLES`` name (unlike `_distinct_roles`, which is stricter
    because it decides what counts TOWARD the floor). An unrecognized/typo'd role
    string is still evidence the task WAS reviewed through a role-recording mode
    (the normal ``review diff`` board), which is the only fact this check needs --
    whether that role earns credit is a separate question `_distinct_roles`
    already answers correctly. Truthiness (not merely ``is not None``) is
    deliberate too (round-review finding, Opus/Fable): `record_run` can in
    principle persist ``"roles": []`` for a caller that supplies matching empty
    ``models``/``roles`` lists (its own length-match guard permits ``0 == 0``) --
    though no CURRENT producer's own truthy-result guard (see cli.py's
    `_run_mode_with_stats`) ever exercises that path in practice, an empty list
    carries no actual role information and must not count as "role data present"
    either way; treating it as sufficient would re-create the exact
    guaranteed-fail-with-no-escape this feature exists to close."""
    return any(item.get("roles") for item in items)


# The exact instant role-tracking became POSSIBLE: PR #246 (commit
# 7a8b64f2eb00178acddbbc325a9c1cf8c53b0dc8), merged 2026-08-21T10:52:25Z, is the
# commit that added `record_run`'s `roles` param and the "roles" key to the JSONL
# record shape in the first place -- no record written before this instant could
# carry role data no matter which review mode produced it, because the code to
# write one didn't exist yet. This is a real git-log fact, not a guess: `git log
# -S'"roles"' -- reviewlib/stats.py` shows the "roles" key was introduced in that
# one commit, nothing earlier -- cross-checked (Opus round-review finding on
# this feature's own PR, worried an EARLIER PR could have written role data
# first) two ways: (1) `git log --all --oneline --reverse -- reviewlib/stats.py`
# lists every commit that ever touched this file, in order, and 7a8b64f is the
# only one before this PR's own that shows up in the `-S'"roles"'` search
# above; (2) "review-cli#221", referenced throughout this module's comments as
# where the role concept originated, is a GitHub ISSUE (closed, a *different*
# topic -- gate tolerance for a flaky model), not a merged PR that could have
# landed code -- the actual "roles" implementation landed entirely in #246.
#
# Used by `_all_iterations_predate_role_tracking` (review-cli#252 follow-up,
# Alex's explicit request) to tell a GENUINELY pre-role-tracking task -- role data
# literally could not have existed at the time it was reviewed -- apart from a
# task that simply never CHOSE a role-recording mode despite having the option.
# See `quorum_check`'s "Bare-check zero-role fallback" docstring section for why
# this distinction matters: only the former may silently reuse the old
# model-counting default; the latter must not, or the role-based gate becomes
# permanently optional for anyone who reviews via quorum/just-ask/brainstorm/qa
# instead of `review diff`.
#
# KNOWN LIMITATION (Fable round-review finding on this feature's own PR): this
# is a WALL-CLOCK cutoff keyed on when PR #246 merged in THIS repository's own
# history, not a per-record marker of which code version actually wrote it. A
# hypothetical separate deployment of review-cli that upgrades past commit
# 7a8b64f some time after 2026-08-21T10:52:25Z would have PRE-#246-code-written
# history carrying POST-cutoff timestamps -- misclassified as case (b) even
# though it's genuinely case (a). This fails CLOSED (denies the free pass
# rather than granting one it shouldn't), so it's an availability annoyance for
# that scenario, not a correctness hole -- and review-cli today has exactly one
# real deployment (this repo, upgraded via `git pull`/worktree, not a
# separately-versioned distributed package), so the scenario is currently
# theoretical. A more robust discriminator would be a per-record schema/format-
# version marker written by `record_run` itself (there is no such field today
# -- checked via `git log -S` before choosing this design, per Alex's explicit
# instruction to verify rather than invent one) rather than comparing against
# an upstream merge instant; worth revisiting if review-cli ever ships as a
# distributed package with staggered upgrades.
#
# The constant below is verified, not eyeballed (Fable round-review finding on
# this feature's own PR: confirm it matches the commit's OWN timestamp, since a
# GitHub "merged at" can in principle differ from author/committer date after a
# rebase). `git show -s --format='%aI%n%cI' 7a8b64f` gives AuthorDate =
# CommitDate = 2026-08-21T13:52:25+03:00, which converts to
# 2026-08-21T10:52:25Z -- exactly `gh pr view 246 --json mergedAt` and exactly
# the literal below. `7a8b64f` has a single parent (not a 2-parent merge
# commit), so its own timestamp is what actually landed on `main`.
_ROLE_TRACKING_CUTOFF = datetime(2026, 8, 21, 10, 52, 25, tzinfo=timezone.utc)


def parse_iso_ts(ts: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` if `ts` isn't a non-empty string or
    fails to parse. Normalizes a trailing ``Z`` (Zulu/UTC shorthand) to ``+00:00``
    first — ``datetime.fromisoformat`` only accepts ``Z`` directly from Python 3.11
    onward, but this project declares ``requires-python = ">=3.9"``.

    GLM review finding on this feature's own PR: this exact fix (strip-trailing-Z,
    then `fromisoformat`) used to be copied independently at THREE call sites across
    two modules, and two of them had already drifted apart (`cli.py`'s
    `_parse_task_record_started` used `.replace("Z", "+00:00")` — rewrites ANY `Z`
    anywhere in the string, not just a trailing one — while the other two used the
    suffix-strip form here). One shared helper, one fix surface.

    Deliberately does NOT default a naive (tz-less) result to UTC or otherwise
    interpret the parsed value — that's a caller decision (some callers reject naive
    timestamps outright as unverifiable, others assume UTC for a field this module's
    own writer always emits tz-aware), so every caller still inspects `.tzinfo`
    itself before trusting the result."""
    if not isinstance(ts, str) or not ts:
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _iteration_predates_role_tracking(item: dict) -> bool:
    """True iff ``item``'s own ``ts`` is a parseable, timezone-aware timestamp
    strictly before ``_ROLE_TRACKING_CUTOFF`` (PR #246's merge instant).

    Fail-closed on anything that can't be verified: a missing, malformed, or
    naive (no UTC offset) ``ts`` does NOT count as "provably old" -- an
    unparseable/ambiguous date proves nothing either way, so it can never help
    grant the legacy fallback via `_all_iterations_predate_role_tracking` below.
    Every producer writes ``ts`` as ``datetime.now(timezone.utc).isoformat()``
    (see `record_run`'s single caller, `cli.py`'s `_run_mode_with_stats`), which
    always parses back timezone-aware -- and this is not merely the CURRENT
    behavior: `git log -S'datetime.now(timezone.utc)).isoformat()' --
    reviewlib/stats.py` shows this exact line has been unchanged since
    `record_run`'s original introduction (commit 4f3bb98), the entire history of
    this store. Fable round-review finding on this feature's own PR asked this
    to be verified (not merely assumed) precisely because case (a)'s whole
    population is historical -- confirmed, so the naive/malformed/missing
    branch below exists only for a hypothetical externally-written or corrupted
    record, never a real gap in this repo's own history.

    "Provably"/"genuinely" old here means "recorded, via this store's existing
    self-reported ``ts`` field, as predating the cutoff" -- the SAME trust model
    every other field in this JSONL store already relies on (`record_run`'s
    ``started`` param, and therefore ``ts``, is caller-supplied; a write-access
    caller could in principle backdate one, exactly as this repo's own test
    suite deliberately does via `started=LEGACY_TS`). This is not new attack
    surface for the self-merge-authority gate -- everything the store records
    (models, verdicts, roles) already carries this same level of trust -- so
    "provably" is used in the sense of "verified against the record's own
    claim," not "cryptographically unforgeable" (Fable round-review finding on
    this feature's own PR).

    Normalizes a trailing ``Z`` (Zulu/UTC shorthand) to ``+00:00`` before
    parsing -- ``datetime.fromisoformat`` only accepts ``Z`` directly from
    Python 3.11 onward, but this project declares ``requires-python = ">=3.9"``.
    Without this, a genuinely pre-cutoff record using ``Z`` (a valid,
    unambiguous UTC marker -- unlike an offsetless naive string, which stays
    fail-closed below) would raise on 3.9/3.10 and get wrongly denied case (a)'s
    fallback. Delegates that normalization to `parse_iso_ts` (shared with
    `_resolve_stat_since` and `_parse_task_record_started` in `cli.py`, which used to
    each carry their own independent copy of this exact fix -- GLM review finding on
    this feature's own PR) because `record_run` itself never emits ``Z``
    (`datetime.isoformat()` on a UTC-aware value always writes ``+00:00``), so this
    guards only against externally-written or historical records in an unverified
    format, not anything this module's own writer produces (codex round-review
    finding on this feature's own PR).
    """
    parsed = parse_iso_ts(item.get("ts"))
    if parsed is None or parsed.tzinfo is None:
        return False
    return parsed < _ROLE_TRACKING_CUTOFF


def _all_iterations_predate_role_tracking(items: list[dict]) -> bool:
    """True iff EVERY item in ``items`` provably predates role-tracking (case (a)
    in `quorum_check`'s "Bare-check zero-role fallback" docstring section) --
    i.e. this task's entire in-scope history could not possibly carry role data,
    so the old model-counting fallback is the best this gate can ever do for it.

    An empty ``items`` returns False, not True -- this function's one caller
    (`quorum_check`'s bare-check zero-role fallback) only ever calls it on an
    already-nonempty ``role_data_population``; "no history at all" is a
    different, pre-existing fail-closed case (`_finalize_quorum_result`'s
    zero-iterations branch), not "all history is old"."""
    return bool(items) and all(_iteration_predates_role_tracking(i) for i in items)


def _iterations_excluding_mismatched(
    iterations: list[dict], repo_id: str | None, diff_files: list[str] | None
) -> list[dict]:
    """All ``iterations`` (of ANY verdict -- passed, failed, or unverdicted) except
    those diff-identity-MISMATCHED against the current check context.

    Used ONLY by the bare-check zero-role fallback's "has this task ever recorded
    role data" check (`_has_any_role_data`) -- deliberately a DIFFERENT population
    than `gate_iterations` (which is PASSED-only). A role-tagged iteration that
    FAILED, or was never verdicted, is still real evidence the task went through a
    role-recording review mode; excluding it from the "any role data" check (round-
    review finding, Opus/Fable, independently) would let the fallback fire for a
    task that genuinely has role-recording history, just none of it happened to
    pass -- contradicting this feature's own documented "even ONE role-tagged
    iteration is unaffected" guarantee and, in the worst case, silently letting
    model-counting decide a self-merge-authority gate that role coverage was
    supposed to govern.

    Mismatched iterations ARE still excluded, though (unlike the "include every
    verdict" relaxation above) -- for the OPPOSITE reason `gate_iterations`
    excludes them: a role-tagged iteration recorded against a genuinely DIFFERENT
    repo/diff (the exact task-code-reuse/collision pollution
    `_sort_passed_iterations_into_buckets` exists to guard against) has nothing to
    do with THIS check's task, and letting it force the CURRENT, unrelated check
    into role-mode (where it would then see zero roles and fail) would silently
    reopen that same cross-repo pollution bug class through a new angle -- the
    counting MODE, not the iteration count itself.

    When ``repo_id`` is None (no check context given -- a direct library call that
    omits it, exactly as elsewhere in this module), no filtering is applied: every
    iteration is returned as-is, matching the "verification is opt-in" convention
    used throughout diff-identity binding.

    Its caller passes only the NON-passed remainder of a task's iterations, not
    the full list -- the PASSED-and-non-mismatched subset is exactly
    ``gate_iterations`` (already computed by `_sort_passed_iterations_into_buckets`
    moments earlier with identical inputs), so re-classifying it here would be
    pure duplicate work on the exact "thousands of iterations" history shape this
    module's docstrings call out as performance-relevant (GLM/Fable review
    finding, raised across two rounds)."""
    if repo_id is None:
        return list(iterations)
    current_files = _current_files_frozenset(diff_files)
    return [
        item
        for item in iterations
        if _classify_iteration_identity(item, repo_id, current_files)[0] != "mismatched"
    ]


def _foreign_repo_iterations(
    iterations: list[dict], repo_id: str
) -> tuple[list[dict], int]:
    """``(details, count)`` for iterations of ANY verdict recorded in a different
    repository; ``details`` stops at ``_MISMATCH_DETAILS_CAP``, ``count`` does not.

    Every one of them is excluded from the gate: passed ones from the iteration and
    model counts, all of them from role credit. ``mismatch_details`` lists only the
    passed ones, so a caller like ``gh ship`` reads this list to say which foreign
    reviews it dropped (review-cli#589). ``task_code`` is the spelling as stored, so
    a digit twin (``464`` recorded for ``#464``) shows up as such. A record without
    ``repo_id`` is unverifiable (see ``_classify_iteration_identity``), not foreign.
    """
    current_id = _comparable_repo_id(repo_id)
    details: list[dict] = []
    count = 0
    for item in iterations:
        recorded_repo = _recorded_repo_id(item)
        if recorded_repo is None or recorded_repo == current_id:
            continue
        count += 1
        if len(details) < _MISMATCH_DETAILS_CAP:
            details.append(
                {
                    "iteration": item.get("iteration"),
                    "repo_id": item.get("repo_id"),
                    "task_code": item.get("task_code"),
                    "ts": item.get("ts"),
                    "passed": item.get("passed"),
                }
            )
    return details, count


def _models_behind_role_coverage(items: list[dict]) -> list[str]:
    """Distinct models that actually EARNED a VALID (``REVIEW_ROLES``) role — the
    population the ``--min-roles`` monoculture guard in ``quorum_check``'s
    suggestion must check diversity against.

    Round-3 review finding (Fable): the guard originally checked
    ``len(models) >= 2`` across EVERY passed iteration, including role-LESS ones
    (quorum/just-ask/brainstorm). That let a role-less iteration on a second model
    satisfy "2 distinct models" while 100% of the counted ROLE coverage still came
    from a single other model — exactly the monoculture shape the guard exists to
    block, just laundered through an unrelated iteration.

    Round-4 review finding (Codex), sharper still: scoping to "this ITERATION
    contributed >=1 valid role" is not enough EITHER — a single multi-seat record
    (e.g. one `review diff` call recording every board seat's ``models``/``roles``
    together) can mix a valid-role seat with an unknown-role seat, and crediting
    every model in that record (not just the one that earned the valid role) lets
    the unknown-role seat's model launder in as "diverse" too. ``models``/``roles``
    ARE index-aligned per seat for every current producer (`panel.
    FailoverOutcome.usable_models`/`usable_roles`, and the CLI's `_ran_models`/
    `_ran_roles` explicit-board branches both iterate the SAME seat list in the
    SAME order — see their docstrings), so ``zip`` pairs each model with the SAME
    seat's own role and credits ONLY a model whose OWN role is valid — never a
    model that merely shares a record with a valid-role seat. A record from a
    hypothetical future producer that doesn't honor this alignment just
    under-credits (``zip`` stops at the shorter list) rather than over-crediting —
    the fail-closed direction for a self-merge-authority gate."""
    models: list[str] = []
    for item in items:
        item_models = item.get("models") or []
        item_roles = item.get("roles") or []
        for model, role in zip(item_models, item_roles):
            if (
                isinstance(model, str)
                and isinstance(role, str)
                and role in REVIEW_ROLES
                and model not in models
            ):
                models.append(model)
    models.sort()
    return models
