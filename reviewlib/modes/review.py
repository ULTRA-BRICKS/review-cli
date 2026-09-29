"""diff review: run the diff across every selected backend in parallel.

`review diff …` is the diff review. A bare `review` prints top-level help, and the
old `review review …` spelling is a usage error that points here. **--diff/diff is
the default** (the mode REQUIRES a diff, as the pre-commit path always has).
Originally the flag-less default of `bin/review:main()` (Stage 0 decomposition);
now a first-class SUBCOMMAND backed by the `MODE` descriptor at the bottom of this
file (see `modes/contract.py`).

The reviewer-board path (HYP-741) is layered on top: when a board is passed, each
reviewer gets its own role-lens prompt + label, but the parallel run, result
formatting, and staged-stamp behaviour are otherwise identical.

The failover pool (priority + availability) is layered on the board path: the board
is a PRIORITY-ordered list, the active pool is the top-N AVAILABLE seats (startup
failover), and a seat that fails DURING the run is replaced by the next-priority
reserve (mid-run failover) so the run still yields N working verdicts when possible.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dataclasses import replace

from ..backends import (
    ReviewResult,
    backend_available,
    backend_unavailable_reason,
    call_backend,
    cap_diff_for_dispatch,
    resolve_backend,
    review_with_images,
    runtime_provider_marked_unpaid,
)
from ..config import (
    DEFAULT_POOL_SIZE,
    DEFAULT_PROMPT,
    BoardReviewer,
    _effective_pool_size,
    apply_effort_override,
    select_pool_and_reserve_with_reuse,
)
from ..install import _touch_review_marker, _write_review_stamp
from ..panel import (
    FailoverOutcome,
    _tally_ok,
    _tally_tokens,
    format_result,
    result_is_usable,
    run_board_with_failover,
)
from ..process import _run, git_repo_env
from ..retry import retry_default, run_seat_with_retry
from . import _visual_images
from .contract import ModeContext, ModeSpec

# Stable, per-class exit codes for the `--commit` checkpoint feature (structured-exit-codes),
# continuing the numbering discipline started in cli.py (EXIT_NOT_A_REPO=3 … EXIT_QA_ENV_
# UNHEALTHY=9) and features/visual/policy_engine.py (EXIT_BLOCK_STRICT=10). 11/12 are the next
# free integers — distinct from every class above so a script/hook can tell "you misused
# --commit" apart from "the checkpoint commit itself failed" apart from any other exit class.
#
# --commit REQUIRES --staged: a checkpoint commits the reviewed STAGED diff, and there is no
# such thing as checkpointing an unstaged/piped diff. This is a hard usage ERROR, not a silent
# no-op and not an implicit `--staged` (a smaller surprise is still a surprise) and NEVER a
# fallback to `git commit -a` (which would sweep in unrelated unstaged changes — exactly the
# "sweep everything" accident class this whole feature exists to prevent).
EXIT_COMMIT_REQUIRES_STAGED = 11
# The review itself succeeded (`ok`) and the checkpoint gate was satisfied (staged, not
# piped), but the actual `git commit` subprocess FAILED (a commit-msg/pre-commit hook
# rejected it, "nothing to commit", or any other nonzero). The user explicitly asked for a
# checkpoint guarantee and did not get one — that must be a visible, distinct failure, never
# silently swallowed as if the review itself failed (which already has its own 0/1 result).
EXIT_COMMIT_FAILED = 12
# codex review finding: the stamp/checkpoint always certify against the UNCAPPED
# canonical diff (by design), while every backend actually saw only the CAPPED,
# possibly-truncated copy — so a truncated staged review used to still pass the
# commit-hook gate / create a checkpoint that CLAIMS the full diff was reviewed when
# only a partial view was. Automation invoking `review diff --staged --commit`
# non-interactively never sees the stderr warning, so a warning alone does not protect
# it. A truncated staged diff now REFUSES to stamp or checkpoint (this exit code for
# `--commit`; the plain `--staged` stamp is skipped the same way, silently, matching how
# it already skips silently on `ok=False`) — the gate exists specifically to certify
# "the full diff was reviewed", and a partial review must never certify as one.
EXIT_COMMIT_DIFF_TRUNCATED = 13


# ---------------------------------------------------------------------------
# `review diff --json` (review-cli#546): additive structured findings output.
# ---------------------------------------------------------------------------
#
# review-cli's panel is PROSE by design (config.DEFAULT_PROMPT above) -- every
# backend returns free text, never a structured findings list. Scraping file/
# line/severity/confidence back OUT of that prose after the fact is unreliable:
# DEFAULT_PROMPT's own "FINDING must cite file:line" / "checked: ..." contract is
# a convention for a HUMAN reader, not a machine grammar a parser can trust (see
# docs/compare-revmux.md, ticket #546's motivation -- revmux gets a trustworthy
# JSON contract because it tells its finders to emit one, not by re-parsing their
# prose). So --json does not attempt prose extraction at all (design option (a)
# rejected): it asks every backend to ALSO emit one fenced JSON block, ON TOP of
# its normal prose review, then parses each seat's block independently after
# dispatch. A seat that fails to emit a parseable block is reported as a
# DEGRADED source in the merged summary rather than aborting the run -- one
# malformed seat must never take five working ones down with it. The prose
# review itself is completely untouched either way: `format_result` never runs
# when --json is set (see mode_review's flat path / _mode_review_board below),
# but the PROMPT each backend receives is the only thing --json changes, and only
# by APPENDING this instruction -- DEFAULT_PROMPT's existing contract is never
# replaced, so a --json run still gets the same adversarial prose review; it is
# just not what gets printed to stdout for that invocation.
_JSON_FINDINGS_FENCE_TAG = "review-json"

_JSON_FINDINGS_PROMPT_SUFFIX = (
    "\n\nAdditionally, once the review above is complete, emit ONE fenced code "
    f"block tagged ```{_JSON_FINDINGS_FENCE_TAG} containing a single JSON object "
    'with exactly this shape: {"findings": [{"file": "<path relative to the repo '
    'root, or null for a CLEAR entry with no single file>", "line": <int or '
    'null>, "severity": "P0"|"P1"|"P2"|"P3" (P0 = blocking, P3 = nit), '
    '"confidence": "high"|"medium"|"low", "verdict": "FINDING"|"CLEAR"}]}. Use '
    '"FINDING" for a real issue -- cite the SAME file:line you already gave '
    'above -- and "CLEAR" for one of the specific failure modes you checked and '
    'ruled out (the "checked: ..." statements above), so the array is never '
    "suspiciously empty on a genuinely clean pass. Emit the block even when "
    "findings is an empty list. The fenced block must contain ONLY that JSON "
    "object -- no other text inside it."
)


def _with_json_findings_request(prompt: str) -> str:
    """Append the structured-findings instruction to `prompt` for a `--json` run.

    Purely additive (ticket #546's acceptance criteria: existing prose callers
    unaffected): DEFAULT_PROMPT's own contract is untouched, so a backend that
    ignores this suffix entirely still returns the SAME prose a non---json caller
    would get -- `format_result`'s render never changes shape. Only `_handler`
    calls this (gated on `ctx.args.json`), building the prompt actually sent to
    backends -- never the `--prompt` OVERRIDE text `--help` shows the user."""
    return prompt + _JSON_FINDINGS_PROMPT_SUFFIX


# Matches the exact tag `--json` asks for (`review-json`). Kept separate from the
# generic ```json fallback below (GLM/Grok review findings, review-cli#546): the
# reviewed diff is UNTRUSTED and routinely quoted back by the model (a file's own
# example config, a docs snippet) inside its normal prose -- an EARLIER, incidental
# ```json fence with no relation to the requested schema must never outrank the
# backend's OWN structured answer just because it happens to parse as a dict first.
_JSON_FINDINGS_TAGGED_FENCE_RE = re.compile(
    r"```" + re.escape(_JSON_FINDINGS_FENCE_TAG) + r"\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Fallback ONLY when no `review-json`-tagged fence exists at all: some backends
# ignore a custom fence tag and use the generic Markdown convention instead.
_JSON_GENERIC_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _best_json_object_candidate(matches: list[re.Match]) -> dict | None:
    """Among fenced-block regex MATCHES (in document order), the best JSON object
    candidate: the LAST match that parses to a dict carrying a `findings` LIST, or
    -- only when nothing does -- the last match that merely parses to a dict.

    Preferring the LAST match (not the first) matches
    `_JSON_FINDINGS_PROMPT_SUFFIX`'s own instruction ("once the review above is
    complete, emit ONE fenced block") -- the genuine structured answer is always
    the backend's FINAL fenced block of the matched tag, never an earlier one.
    This is a deliberate hardening (GLM/Grok review findings, review-cli#546): a
    diff (or an indirectly-prompt-injected reply) that plants an EARLIER fenced
    block shaped like `{"findings": []}` must never shadow the backend's real,
    later answer -- neither by silently forcing a false-clean gate pass (an
    earlier empty `findings` list winning) nor by silently discarding a real
    finding (an earlier findings-less dict winning and marking the seat DEGRADED
    before the real block is ever inspected). Preferring findings-bearing
    candidates over bare dicts, and the LAST of either group, closes both
    directions at once: a later REAL block always wins over an earlier PLANTED
    or merely-quoted one of the same tag."""
    best_plain: dict | None = None
    best_with_findings: dict | None = None
    for match in matches:
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(parsed, dict):
            continue
        best_plain = parsed
        if isinstance(parsed.get("findings"), list):
            best_with_findings = parsed
    return best_with_findings if best_with_findings is not None else best_plain


def _extract_json_findings_block(stdout: str) -> dict | None:
    """Best-effort extraction of the structured findings object `--json` asked
    every backend to emit (see `_JSON_FINDINGS_PROMPT_SUFFIX`). Tries, in order:
    (1) every ```review-json-tagged fence (the exact tag requested) -- see
    `_best_json_object_candidate` for how the best one among several is picked;
    (2) only when NO review-json-tagged fence exists at all, every plain ```json
    fence (some backends ignore a custom tag); (3) the WHOLE response as JSON (a
    `--prompt` override, or a model that skipped prose entirely, may return just
    the object with no fence at all).

    Returns None -- NEVER raises -- on any parse failure; the caller counts that
    seat as DEGRADED rather than crashing the run (ticket #546: "a backend whose
    response fails to parse as valid JSON should be reported as a degraded/
    unparseable source ... not crash the whole command"). Deliberately does NOT
    fall back to `features/visual/vision_client._extract_first_json_object`'s
    scan-for-the-first-balanced-`{...}`-anywhere-in-the-text heuristic: that
    scanner suits the vision pipeline, whose whole response is typically ONLY the
    structured answer with little surrounding prose, but every --json backend
    here ALSO returns a FULL adversarial prose diff review first -- one that
    routinely quotes braces of its own (a diff snippet, a JS/JSON example from
    the code under review) BEFORE the requested fenced block. Scanning that much
    free prose for the first balanced object would parse VALID but WRONG JSON far
    more often than in the vision case, silently fabricating a findings list the
    model never reported as structured at all. A clean miss (None) is safer than
    a false-positive parse."""
    candidate = _best_json_object_candidate(
        list(_JSON_FINDINGS_TAGGED_FENCE_RE.finditer(stdout))
    )
    if candidate is not None:
        return candidate
    candidate = _best_json_object_candidate(
        list(_JSON_GENERIC_FENCE_RE.finditer(stdout))
    )
    if candidate is not None:
        return candidate
    try:
        parsed = json.loads(stdout.strip())
    except (json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


# The enums `--json` requests (see `_JSON_FINDINGS_PROMPT_SUFFIX`). Severity
# reuses `qa`'s existing P0..P3 convention (reviewlib/qa/executor.py's
# `_FINDING_RE`/`max_severity`) rather than inventing a second severity
# vocabulary in the same codebase; confidence/verdict have no existing
# review-cli precedent to match, so they are new, minimal enums.
_FINDING_SEVERITIES = ("P0", "P1", "P2", "P3")
_FINDING_CONFIDENCES = ("high", "medium", "low")
_FINDING_VERDICTS = ("FINDING", "CLEAR")


def _coerce_finding(raw: object) -> dict | None:
    """Normalize one raw findings-array item into the stable `--json` shape, or
    None to DROP it -- never raise; one malformed item must not crash a whole
    seat's otherwise-usable response.

    `severity`/`confidence` fall back to a documented middle value ("P2"/
    "medium") when missing or spelled outside the requested enum, rather than
    being dropped: a wrong/missing severity TAG does not change whether the item
    IS a finding, only how it should be triaged, so silently discarding a real
    finding over a formatting slip would lose strictly more information than a
    mislabeled severity costs (the same fail-toward-surfacing trade-off
    `features/visual/vision_client._safe_confidence` documents for an analogous
    untrusted-model-output coercion). `verdict` gets the same treatment for the
    same reason, defaulting to "FINDING" rather than being dropped or silently
    downgraded to "CLEAR": an item the model bothered to emit at all is far more
    likely a genuine finding it mislabeled than a ruled-out check it correctly
    labeled wrong -- the more dangerous direction here is a LOST finding, not an
    extra one a caller has to triage."""
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity", "")).strip().upper()
    if severity not in _FINDING_SEVERITIES:
        severity = "P2"
    confidence = str(raw.get("confidence", "")).strip().lower()
    if confidence not in _FINDING_CONFIDENCES:
        confidence = "medium"
    verdict = str(raw.get("verdict", "")).strip().upper()
    if verdict not in _FINDING_VERDICTS:
        verdict = "FINDING"
    file = raw.get("file")
    file = str(file) if isinstance(file, str) and file.strip() else None
    line = raw.get("line")
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError, OverflowError):
        line = None
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "confidence": confidence,
        "verdict": verdict,
    }


def _seat_json_findings(result: ReviewResult) -> tuple[list[dict], bool]:
    """`(findings, reported)` for one seat's `--json` response.

    `reported` is False (the seat is DEGRADED) when: the seat produced no usable
    output at ALL (`result_is_usable` -- the SAME predicate the prose path's `ok`
    gate and the board's own mid-run failover already use, so a seat --json marks
    degraded here is exactly a seat the prose path would already call unusable);
    its structured block failed to parse/extract at all
    (`_extract_json_findings_block` returned None); the parsed object has no
    `findings` KEY at all (a genuine shape violation); `findings` is present but
    neither a list nor `null`; or `findings` is a NON-EMPTY list whose every item
    failed `_coerce_finding` (Opus/GLM review findings, review-cli#546: an
    all-unusable-items array reads as "the model was clearly trying to report
    something and every item was garbage", not a genuine clean pass -- silently
    reporting `reported=True, findings=[]` there would be a false clean, the exact
    class of failure this module elsewhere fails TOWARD surfacing rather than
    away from).

    A `findings: null` (a plausible "nothing to report" rendering a model might
    emit despite the prompt asking for `[]`) is normalized to an empty list
    rather than degrading the seat -- unlike an ABSENT `findings` key entirely,
    which stays a genuine shape violation. A seat that parsed fine and reported a
    genuinely EMPTY `findings` list (explicit `[]` or `null`) is `reported=True`
    with `findings=[]` -- a clean, structured pass is not a parse failure."""
    if not result_is_usable(result):
        return [], False
    parsed = _extract_json_findings_block(result.stdout)
    if parsed is None or "findings" not in parsed:
        return [], False
    raw_findings = parsed["findings"]
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        return [], False
    findings: list[dict] = []
    for raw in raw_findings:
        coerced = _coerce_finding(raw)
        if coerced is not None:
            findings.append(coerced)
    if raw_findings and not findings:
        return [], False
    return findings, True


def _build_json_report(results: list[ReviewResult]) -> dict:
    """The `--json` payload for already-dispatched `results` (run order) -- the
    ONLY --json rendering path for both the flat and board dispatch paths below.
    Pure post-processing over the SAME ReviewResults the prose path already
    formats: --json costs no extra backend calls of its own, and never runs
    during dispatch.

    Top-level shape (ticket #546 -- deliberately review-cli's OWN schema, not a
    copy of revmux's `sources`/`open_questions`/`stats` fields; see
    docs/compare-revmux.md, which this mirrors only in SPIRIT: a stable
    machine-parseable contract, not a byte-for-byte one):
      findings -- flat array of every FINDING/CLEAR item every REPORTED seat
        returned, each additionally tagged `source` = that seat's label. Board
        mode: "<display> [<role>]" (e.g. "Opus [correctness]"); flat `-m` mode:
        the bare model string. Both ALREADY are `result.model` by the time this
        runs -- see `panel.build_board_job` (the board's label) and this
        module's flat dispatch path (which re-tags each result with its
        originally-REQUESTED model string via `replace(final, model=model)` in
        `_flat_seat_with_provider_failover`) -- so no extra bookkeeping is
        threaded through dispatch just to build this field.
      summary.expected -- every seat that actually ran, in run order (mirrors
        the prose path's own "every seat that ran, successes and failed seats
        that triggered a backfill", see `_mode_review_board`'s docstring).
      summary.reported -- the subset whose structured block parsed
        (`_seat_json_findings`'s `reported=True`).
      summary.degraded -- the subset that did not (backend failure OR an
        unparseable/missing findings block) -- ticket #546: "reported as a
        degraded/unparseable source ... not crash the whole command"."""
    findings: list[dict] = []
    expected: list[str] = []
    reported: list[str] = []
    degraded: list[str] = []
    for result in results:
        expected.append(result.model)
        seat_findings, ok = _seat_json_findings(result)
        (reported if ok else degraded).append(result.model)
        for item in seat_findings:
            findings.append({**item, "source": result.model})
    return {
        "findings": findings,
        "summary": {
            "expected": expected,
            "reported": reported,
            "degraded": degraded,
        },
    }


def _json_exit_code(report: dict) -> int:
    """The `--json`-path exit contract (ticket #546), DISTINCT from the prose
    path's `0 if ok else 1` (seat-HEALTH only -- see the bottom of `mode_review`
    and `_mode_review_board`):
      0 -- the run produced at least one REPORTED seat, and zero FINDING-verdict
           entries among them. The acceptance criterion's "above the existing
           severity/confidence floor" is the FINDING/CLEAR line itself, not a
           NEW numeric severity/confidence cutoff -- review-cli has no
           pre-existing severity/confidence bar anywhere else to inherit one
           from (checked: quorum's --min-iter/--min-models/--min-roles gate
           iteration/model/ROLE counts, not per-finding severity; the visual
           pipeline's confidence float gates a SCREENSHOT verdict, not a diff
           finding) -- so introducing a specific numeric cutoff here would be a
           new, undocumented design choice with no existing convention to match,
           not a mirror of one. severity/confidence are still reported on every
           finding for a caller that wants to filter further itself.
      1 -- at least one FINDING-verdict entry among the reported seats.
      2 -- tool error: not ONE seat reported (every dispatched seat either
           failed outright or returned an unparseable block) -- the run
           produced NO structured signal at all, which is a materially
           different claim than "reviewed and clean" and must not silently
           collapse into exit 0.
    This 2 is scoped to `--json`'s own contract (only ever returned from
    mode_review's/`_mode_review_board`'s --json branch) and does not reuse
    argparse's pre-existing "2 == usage error" meaning (cli.py's own comment on
    its exit-code block) -- by the time this runs, argparse has already finished
    parsing successfully; the two can never collide on the same invocation."""
    if not report["summary"]["reported"]:
        return 2
    if any(finding["verdict"] == "FINDING" for finding in report["findings"]):
        return 1
    return 0


def _print_json_report(results: list[ReviewResult]) -> dict:
    """Build + print the `--json` payload for `results` in ONE place -- both
    `mode_review`'s flat path and `_mode_review_board` call this instead of
    repeating the build/print pair (review-cli#546, code-quality review finding),
    and both reuse the RETURNED report for `_json_exit_code` rather than
    rebuilding/re-parsing it a second time."""
    report = _build_json_report(results)
    print(json.dumps(report, indent=2))
    return report


def _predispatch_error_exit(json_output: bool) -> int:
    """The exit code for a PRE-DISPATCH usage/availability failure (no diff to
    review / no reviewers available) -- shared by `mode_review`'s flat path and
    `_mode_review_board` so the 1-vs-2 mapping can't drift between the two
    (review-cli#546, code-quality review finding: it was duplicated verbatim).
    The prose path keeps its pre-existing 1 (unchanged from every prior
    release); `--json` gets its own tool-error code (2) per `_json_exit_code`'s
    contract -- a caller parsing --json needs to tell "nothing ran" apart from
    "ran and found nothing"."""
    return 2 if json_output else 1


if TYPE_CHECKING:
    from ..config import EffortOverride
    from ..tui import BoardReporter


def _flat_seat_with_provider_failover(
    model: str, dispatch: Callable[[str], ReviewResult]
) -> ReviewResult:
    """The flat `-m` path's per-seat runner: SAME provider-failover cascade as the board
    path (reviewlib.panel's `_seat`), applied to a single flat seat.

    Each provider in `model`'s chain first gets the in-seat transient retry
    (`run_seat_with_retry`); a still-unusable result FAILS OVER to the chain's next provider,
    continuing the review — the seat only reports failure once every provider is exhausted.
    The provider that produces a usable verdict is cached as last-working (tried first next
    run); a total failure rotates the cache. Returned results always carry `model` (the
    caller-facing name), never the concrete provider spelling, so callers keying results by
    the ORIGINAL requested model (as `mode_review`'s flat path does) still find them.

    `dispatch` is called with the CONCRETE provider spelling for each attempt (not the
    original `model`), so a per-provider `--effort <route>=<level>` override
    (`_seat_effort`/`EffortOverride.effort_for`, keyed on the backend ROUTE) resolves against
    whichever provider is ACTUALLY running at that attempt — e.g. after `zai:glm-5.2` fails
    over to `oc:zai/glm-5.2`, an `--effort opencode=high` override applies (the seat now
    genuinely runs the opencode route) while a `--effort zai=high` override no longer does.
    This is intentional (raised as a 'please confirm' item on review of #157): a route-keyed
    override exists to size whichever backend executes, not the originally-requested route.
    See test_flat_failover_effort_follows_the_actually_dispatched_provider_route.

    `dispatch` is wrapped per-attempt so a RAISE (not just a nonzero `ReviewResult`) from
    one provider still fails over to the next, instead of aborting the whole seat and
    skipping the rest of the chain. The board path gets this for free because `run_panel`'s
    `_run_job` already catches every exception and normalizes it to a failed `ReviewResult`
    before its provider-failover loop (`panel._seat`) ever sees it; the flat path's
    `dispatch` has no such wrapper, so this cascade must do it itself (codex review of
    #157). See test_flat_m_failover_survives_a_provider_that_raises_instead_of_returning.

    The last-working-cache WRITE (`remember_working_provider`/`forget_working_provider`) is
    gated the SAME way `provider_chain`'s cache-reorder READ is
    (`is_default_provider_selection`): an explicit alternate pin (`-m
    commandcode:zai-org/GLM-5.2`) succeeding/failing must not train/clear the shared
    logical-key cache entry a later BARE alias request (`-m glm52`) reads (review of #157:
    'cache write isn't gated the way the read is')."""
    from ..provider_failover import (
        forget_working_provider,
        is_default_provider_selection,
        provider_chain,
        remember_working_provider,
    )

    def _safe_dispatch(provider_model: str) -> ReviewResult:
        try:
            result = dispatch(provider_model)
        except Exception as exc:  # noqa: BLE001 - normalize so failover can continue
            result = ReviewResult(
                model=provider_model,
                command="internal",
                returncode=127,
                stdout="",
                stderr=str(exc),
            )
        # Tally tokens for EVERY real dispatch attempt, not just the seat's final
        # outcome: `run_seat_with_retry` below can invoke this multiple times for
        # ONE provider (in-seat retry) and the chain loop invokes it again per
        # provider on failover -- each call is a real backend round-trip that may
        # have spent real tokens (codex review finding: tallying only the final
        # result undercounted a seat that needed a retry or a provider failover).
        _tally_tokens(result)
        return result

    chain = provider_chain(
        model, available=backend_available, unpaid=runtime_provider_marked_unpaid
    )
    cache_eligible = len(chain) > 1 and is_default_provider_selection(model)
    last: ReviewResult | None = None
    for idx, provider_model in enumerate(chain):
        result = run_seat_with_retry(
            model, lambda pm=provider_model: _safe_dispatch(pm)
        )
        if result_is_usable(result):
            if cache_eligible:
                remember_working_provider(model, provider_model)
            return replace(result, model=model)
        last = result
        if idx + 1 < len(chain):
            print(
                f"[review-cli] seat {model}: provider {provider_model} failed — "
                f"switching to {chain[idx + 1]} (review continues)",
                file=sys.stderr,
                flush=True,
            )
    if cache_eligible:
        forget_working_provider(model)
    final = last if last is not None else _safe_dispatch(model)
    return replace(final, model=model)


def mode_review(
    models: list[str],
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    staged: bool,
    board: list[BoardReviewer] | None = None,
    pool_size: int = DEFAULT_POOL_SIZE,
    outcome_sink: list[FailoverOutcome] | None = None,
    diff_from_stdin: bool = False,
    visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
    effort_override: "EffortOverride | None" = None,
    commit: bool = False,
    usage_percent: Callable[[str], float | None] | None = None,
    stamp_diff_hash: str | None = None,
    json_output: bool = False,
    reporter: "BoardReporter | None" = None,
) -> int:
    # --commit REQUIRES --staged (see the exit-code block above). Checked BEFORE the (paid)
    # panel dispatch, not after — a usage mistake should fail fast, not after burning a full
    # multi-model review the checkpoint could never have used anyway.
    if commit and not staged:
        # Close the TUI (if active) before printing -- this usage-error return happens
        # BEFORE `_mode_review_board` ever runs (which is where the reporter normally gets
        # closed, right after dispatch), and a message printed while curses' alternate
        # screen is still up (smcup/rmcup, toggled automatically by initscr()/endwin() on a
        # capable terminal) is invisible the instant the screen is later restored — see
        # `_mode_review_board`'s docstring for the full explanation. cli.py's own cleanup
        # closes it too, defensively; close() is idempotent.
        if reporter is not None:
            reporter.close()
        print(
            "[review-cli] --commit requires --staged: a checkpoint commits the reviewed "
            "STAGED diff, and there is no such thing as checkpointing an unstaged/piped "
            f"diff.\n  fix: add --staged (`{_RERUN_STAGED_COMMIT}`), or drop --commit.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_COMMIT_REQUIRES_STAGED

    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return _predispatch_error_exit(json_output)

    if board:
        # Resolve the run-scoped effort onto the seats HERE, so a direct lib/MCP caller that
        # passes an un-applied board + effort_override gets the override too — not only the CLI
        # path (which pre-applies). apply_effort_override is idempotent (re-resolving an already
        # resolved seat yields the same effort), so the CLI's earlier apply stays a no-op.
        board = apply_effort_override(board, effort_override)
        return _mode_review_board(
            board,
            prompt,
            diff,
            cwd,
            timeout,
            staged,
            pool_size,
            outcome_sink,
            diff_from_stdin=diff_from_stdin,
            visual_images=visual_images,
            exact_board=exact_board,
            commit=commit,
            usage_percent=usage_percent,
            stamp_diff_hash=stamp_diff_hash,
            json_output=json_output,
            reporter=reporter,
        )

    # The flat `-m` / config-`models:` path: each seat runs in parallel AND now gets BOTH
    # in-seat retry on a transient failure (`--retry` / $REVIEW_RETRY_COUNT) AND provider
    # failover (reviewlib.provider_failover) — not just the board path (codex P2 on
    # review-cli#157: "Apply provider failover to flat -m reviews"). Each provider first gets
    # the same-provider retry; a still-unusable result switches this SAME model to its NEXT
    # provider, so the seat only fails once every provider is exhausted.
    results: list[ReviewResult] = []

    def _seat_effort(model: str) -> str | None:
        # The flat `-m` path has no BoardReviewer, so the run-scoped `--effort` is the only
        # effort source. None (no flag) keeps the dispatch byte-identical to before.
        return (
            effort_override.effort_for(model) if effort_override is not None else None
        )

    # Capped copy for backend dispatch ONLY — never for the stamp/checkpoint `diff`
    # below (see cap_diff_for_dispatch's docstring: capping the canonical diff broke
    # the --commit checkpoint's integrity check, codex P1 finding). A PIPED diff
    # (`diff_from_stdin`) is exempt from the cap — the user already explicitly, and
    # deliberately, scoped what they piped in; capping it too would silently truncate
    # an intentional `git diff | review diff ...` (a second codex P1 finding on the
    # same PR: the cap originally ignored this flag even though the stamp/checkpoint
    # calls right below already receive it).
    dispatch_diff = diff if diff_from_stdin else cap_diff_for_dispatch(diff)
    dispatch_diff_truncated = _warn_if_dispatch_diff_truncated(
        diff, dispatch_diff, staged
    )

    def _dispatch(model: str) -> ReviewResult:
        backend = resolve_backend(model)
        effort = _seat_effort(model)
        if visual_images:
            if effort is None:
                return review_with_images(
                    model, prompt, dispatch_diff, cwd, timeout, 0, visual_images
                )
            return review_with_images(
                model,
                prompt,
                dispatch_diff,
                cwd,
                timeout,
                0,
                visual_images,
                effort=effort,
            )
        if effort is None:
            return backend(model, prompt, dispatch_diff, cwd, timeout)
        return call_backend(
            backend, model, prompt, dispatch_diff, cwd, timeout, effort=effort
        )

    def _run_seat_with_failover(model: str) -> ReviewResult:
        return _flat_seat_with_provider_failover(model, _dispatch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {
            pool.submit(_run_seat_with_failover, model): model for model in models
        }
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                results.append(
                    ReviewResult(
                        model=model,
                        command="internal",
                        returncode=127,
                        stdout="",
                        stderr=str(exc),
                    )
                )
            # One tally per logical seat (its FINAL outcome after any retry). No-op outside a
            # CLI-driven run; this flat path runs its own executor, so it tallies here.
            # glm-5.2 review finding (2026-08 seat-cooldown feature): this used to tally by
            # bare returncode, so a cached-cooldown sentinel (rc=0, non-empty "unavailable"
            # body) recorded as `ok` in run-stats — the same distortion `run_moderator`'s
            # `_tally_ok(result_is_usable(...))` already fixed. `result_is_usable` is the
            # SAME predicate this function's own `ok` determination uses below, so a
            # cooling-down seat can no longer report differently to run-stats than it does
            # to the review's own pass/fail result.
            _tally_ok(result_is_usable(results[-1]))
            # Token tallying happens PER DISPATCH ATTEMPT inside `_safe_dispatch`
            # above (codex review finding: tallying only the seat's final result
            # here undercounted a seat that needed an in-seat retry or a provider
            # failover -- each such attempt is a separate real backend call). Do
            # NOT also tally `results[-1]` here -- it was already counted once as
            # the last `_safe_dispatch` call that produced it; tallying it again
            # would double-count that one attempt.

    by_model = {result.model: result for result in results}
    # `report` stays None on the prose path (never read there); the --json path
    # builds+prints it here via `_print_json_report` so the SAME merged payload
    # backs both stdout and the exit-code decision below (`_json_exit_code`) --
    # never rebuilt/re-parsed a second time for the return value.
    report: dict | None = None
    if json_output:
        report = _print_json_report([by_model[model] for model in models])
    else:
        # check_evidence=prompt.startswith(DEFAULT_PROMPT) (k3 review finding, round 6;
        # `.startswith` not `==`, k3 finding round 7 — see below): only the diff-review
        # render paths (here and the board path below) apply the missing-evidence
        # WARNING at all — DEFAULT_PROMPT's "checked: ..." contract is a diff-review-only
        # ask, so quorum/just-ask/brainstorm's own format_result calls stay at the
        # default False (glm-cc review finding, round 1). But even within diff-review,
        # `--prompt` lets a caller REPLACE DEFAULT_PROMPT entirely (e.g. `--prompt
        # 'Answer APPROVED if safe; otherwise list blockers.'`) — a custom prompt never
        # asked for the "checked: ..." phrasing, so a compliant one-word "APPROVED"
        # response must not get a warning claiming it omitted evidence it was never
        # asked to provide. An exact `==` check (round 6's first cut) silently broke on
        # `--visual`: `_with_visual` APPENDS the image's context note to whatever prompt
        # is in effect (`text + visual_ctx.context_note`, cli.py's `_with_visual`), so
        # `review diff --visual shot.png` with no `--prompt` dispatches
        # `DEFAULT_PROMPT + context_note` — a DIFFERENT string from `DEFAULT_PROMPT`,
        # even though the model still received DEFAULT_PROMPT's full "checked: ..."
        # contract verbatim. `==` silently disabled the WARNING on every visual diff
        # review, exactly the composable path this feature needs to keep covering.
        # `.startswith` recognizes DEFAULT_PROMPT as an unmodified PREFIX (true whether
        # visual appended nothing, or appended a context note) while still resolving to
        # False for a genuinely custom `--prompt` (with or without `--visual`), which by
        # construction never starts with DEFAULT_PROMPT's text. Computed HERE, inside
        # the prose branch only (code-quality review finding, review-cli#546): the
        # --json branch above never calls format_result, so this was dead weight there.
        check_evidence = prompt.startswith(DEFAULT_PROMPT)
        print(
            "\n\n---\n\n".join(
                format_result(by_model[model], check_evidence=check_evidence)
                for model in models
            )
        )
    # Opus review finding: this used to check ONLY `returncode == 0`, not
    # `result_is_usable`. A live rc=0 "is currently unavailable" sentinel (the same
    # shape `_cooldown_skip_result` deliberately mirrors, and the board path already
    # guards against via `result_is_usable` in `run_board_with_failover`) would
    # therefore read as `ok=True` on the FLAT path whenever provider failover exhausts
    # without a working alternate — e.g. a single-seat `-m fable` review with no
    # failover chain left. That is not a hypothetical: `_flat_seat_with_provider_
    # failover` already returns the CHAIN'S FINAL (possibly still-unusable) result
    # unchanged once every provider is exhausted (`final = last if last is not None
    # else ...`); this cooldown feature makes an rc=0 unusable result far more common
    # than the rare live-paywall case that could already trigger this pre-existing gap.
    # A checkpoint/stamp must never certify a diff that produced ZERO real review
    # content — `result_is_usable` is the same predicate the board path already uses
    # for exactly this reason.
    #
    # ACCEPTED LIMITATION (glm review finding, this feature's own PR): `result_is_usable`
    # marks any rc=0 body <= 400 chars containing a marker phrase like "is currently
    # unavailable" as unusable, with no check for WHERE in the body it appears — so a
    # genuine terse verdict that happens to QUOTE that phrase (e.g. reviewing this exact
    # code, "the cooldown check only fires on 'is currently unavailable'; add the other
    # markers") reads as `ok=False` and withholds the stamp/checkpoint. This is not new
    # here — the board path (`run_board_with_failover`) already carried the identical
    # heuristic before this diff; this change only extends the SAME accepted trade-off
    # to the flat path for consistency. Narrowing it (e.g. only matching the marker on
    # its own line) risks a false NEGATIVE — letting a real cache-hit sentinel through
    # as "usable" — which is the more dangerous direction for a commit-gate certifying
    # a review actually happened. Left as-is deliberately; not a regression to fix here.
    ok = all(result_is_usable(result) for result in results)
    _stamp_if_staged_commit_review(
        ok,
        staged,
        diff_from_stdin,
        cwd,
        diff,
        dispatch_diff_truncated,
        stamp_diff_hash=stamp_diff_hash,
    )
    override = _checkpoint_if_requested(
        commit, ok, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
    if override is not None:
        return override
    if json_output:
        return _json_exit_code(report)
    return 0 if ok else 1


def _warn_if_dispatch_diff_truncated(
    diff: str, dispatch_diff: str, staged: bool, reporter: "BoardReporter | None" = None
) -> bool:
    """Print a visible stderr warning whenever the dispatch cap actually truncated the
    diff, and return WHETHER IT DID (codex review finding, round 5, then round 6): the
    stamp/checkpoint always certify against the UNCAPPED canonical `diff` (by design —
    see cap_diff_for_dispatch's docstring), so `review diff --staged` on an oversized
    staged diff used to still pass the commit-hook gate ("the staged index was
    reviewed") even though every seat actually saw only the first
    $REVIEW_DIFF_MAX_BYTES plus a truncation note. A stderr warning alone does NOT
    protect automation (a non-interactive `review diff --staged --commit` never sees it)
    — the return value here lets the caller REFUSE to stamp/checkpoint on a truncated
    staged diff, not just warn about it (see EXIT_COMMIT_DIFF_TRUNCATED).

    This return used to be STAGED-SCOPED — `False` for an unstaged truncated run, on the
    reasoning that such a run has no commit-gate certification to refuse. That became
    wrong the moment `_commit_gate_skip_reason` started using the same value to pick
    WHICH remediation to print (codex finding, iteration 4): an unstaged oversized run
    arrived as `truncated=False`, fell through to the not-staged branch, and was told to
    "stage and re-run `--staged`" — which truncates again, the exact looping advice that
    branch ordering exists to prevent. The value is now the plain fact "the cap cut this
    diff", and staged-scoping happens where it belongs, in the callers that act on it:
    the stamp path refuses an unstaged run anyway (it is not `staged`), and the
    checkpoint path is only ever reached under `--staged` (`--commit` without `--staged`
    is rejected before dispatch, EXIT_COMMIT_REQUIRES_STAGED). Only the WORDING of the
    warning below is still staged-specific.

    Opus review finding (this feature's own PR): an unstaged, over-cap `review diff`
    used to truncate completely SILENTLY to the user — the only signal was the marker
    embedded in the payload handed to the backend, which never reaches the user's own
    stdout. Not printing a warning here just because there's no gate to refuse left a
    real, common case (a plain `review diff` on a huge change) reviewing a partial diff
    with zero visible notice. Both branches below share the same byte-count facts; only
    the framing (commit-gate consequence vs none) and the git command hint differ.

    `reporter` (review-cli#548's optional live TUI observer, None for the flat path,
    which never has one) routes this warning into the TUI's event log instead of a raw
    stderr print when active -- this is exactly the safety warning the feature's own
    docstring says used to be silently lost, so it must survive an active curses screen
    too (review finding on this feature's own PR: printed directly, it would vanish the
    instant the alt-screen later restores, recreating the very incident this function
    exists to prevent, for the one audience -- an interactive human -- most likely to be
    reviewing a large change by hand)."""
    truncated = dispatch_diff != diff
    if not truncated:
        return False
    full_bytes = len(diff.encode("utf-8"))
    reviewed_bytes = len(dispatch_diff.encode("utf-8"))
    if staged:
        note = (
            "WARNING: the staged diff exceeded $REVIEW_DIFF_MAX_BYTES and "
            "was TRUNCATED for every seat — this review covers only a PARTIAL view of "
            "the staged change, not the full diff, so no commit-gate stamp/checkpoint "
            f"will be created. Full diff: {full_bytes} bytes; reviewed: "
            f"{reviewed_bytes} bytes. Scope the review "
            "(`git diff --cached -- <path>`) or raise the cap to review the full change."
        )
        if reporter is not None:
            reporter.event(f"[warn] {note}")
        else:
            print(f"[review-cli] {note}", file=sys.stderr, flush=True)
        return True
    note = (
        "WARNING: the diff exceeded $REVIEW_DIFF_MAX_BYTES and was "
        "TRUNCATED for every seat — this review covers only a PARTIAL view of the "
        f"change. Full diff: {full_bytes} bytes; reviewed: {reviewed_bytes} bytes. "
        "Scope the review (`git diff -- <path>`) or raise $REVIEW_DIFF_MAX_BYTES to "
        "review the full change."
    )
    if reporter is not None:
        reporter.event(f"[warn] {note}")
    else:
        print(f"[review-cli] {note}", file=sys.stderr, flush=True)
    return True


def _warn_if_board_reused(
    board: list[BoardReviewer], pool: list[BoardReviewer]
) -> None:
    """Operator-facing stderr notice when `select_pool_with_reuse` actually
    padded the pool with a repeated model -- NOT inferred from "does `pool`
    contain a duplicate model" (a config board MAY legitimately list the same
    model under two distinct roles as two separate seats, which would
    false-positive that check — Fable/k3 review finding). The accurate
    signal: `select_pool_with_reuse`'s padding entries are always fresh
    `replace()` copies (see its docstring), so their `id()` is never one of
    `board`'s own seat objects — an ORIGINAL board seat repeated verbatim in
    `pool` is a legitimate duplicate-model board, not reuse.

    Mirrors the existing failover promotion message (`run_board_with_failover`'s
    stderr line) so the duplicated dispatch cost is attributable, not silent.

    The parenthetical cause is deliberately GENERIC ("too few distinct models were
    reachable"), not "...were under their usage limit" (task-cli#139, codex review
    finding): `select_pool_with_reuse` pads for TWO distinct reasons — a model near its
    `usage_percent` threshold (the case this message was originally written for) AND, as
    of task-cli#139, a model that is hard-unavailable altogether (a session-limit
    cooldown, a missing key/CLI — nothing to do with usage limits). This function has no
    visibility into WHICH reason applied (that distinction lives in
    `select_pool_with_reuse`'s internal `under_limit` split, not exposed to callers), so
    claiming "usage limit" unconditionally was actively misleading for the hard-cooldown
    case — a report saying "reused because near its usage limit" while every OTHER seat
    is actually in cooldown, not merely expensive."""
    board_ids = {id(r) for r in board}
    copies = [r for r in pool if id(r) not in board_ids]
    if not copies:
        return
    pool_models = [r.model for r in pool]
    # review-cli#588: a copy whose model appears only once in the pool is not model
    # reuse — it is a repeated-role seat moved onto a lens the pool lacked.
    relensed = [r for r in copies if pool_models.count(r.model) == 1]
    reused_models = [r.model for r in copies if pool_models.count(r.model) > 1]
    if relensed:
        moved = ", ".join(f"{r.display} [{r.role}]" for r in relensed)
        print(
            f"[review-cli] board re-lensed — {moved} (its configured role was already "
            "covered by a higher-priority seat, so it reviews a lens the pool lacked)",
            file=sys.stderr,
            flush=True,
        )
    if not reused_models:
        return
    counts = {m: reused_models.count(m) for m in dict.fromkeys(reused_models)}
    repeated = ", ".join(f"{m} x{n}" for m, n in counts.items())
    print(
        f"[review-cli] board padded — reusing {repeated} across extra roles "
        "(too few distinct models were reachable/under their usage limit)",
        file=sys.stderr,
        flush=True,
    )


def _report_role_shortfall(outcome, *, json_output: bool = False) -> None:
    """Loud notice when a board run ends with fewer distinct lensed roles than it owed
    (`FailoverOutcome.missing_roles`, review-cli#588): names every missing role and, for
    each, the seats that tried it and why they failed. STDOUT like `_report_pool_shortfall`
    (an agent piping the review into a PR/TG report keeps stdout and drops stderr — a lens
    that silently vanished is exactly what this must not hide); stderr under --json so the
    JSON envelope stays parseable."""
    missing = list(getattr(outcome, "missing_roles", None) or [])
    if not missing:
        return
    covered = [r for r in dict.fromkeys(outcome.usable_roles) if r]
    failures = getattr(outcome, "role_failures", None) or {}
    lines = [
        f"[review-cli] ROLE SHORTFALL: {len(covered)} of {outcome.role_target} required "
        f"roles covered ({', '.join(covered) or 'none'}); missing: {', '.join(missing)}. "
        "No healthy seat was left to take them — this review is INCOMPLETE."
    ]
    for role in missing:
        tried = failures.get(role) or []
        if tried:
            for line in tried:
                lines.append(f"  - {role}: {line}")
        else:
            lines.append(f"  - {role}: no live seat on the board carries this lens")
    print("\n".join(lines), file=sys.stderr if json_output else sys.stdout, flush=True)


def _report_pool_shortfall(
    board: list[BoardReviewer],
    pool: list[BoardReviewer],
    *,
    reserve: list[BoardReviewer],
    pool_size: int,
    json_output: bool = False,
) -> None:
    """`pool`/`reserve` are keyword-only past `pool` on purpose (GLM review finding,
    round 5): both are same-typed `list[BoardReviewer]` sitting side by side, and a
    future caller writing `(board, reserve, pool, pool_size)` type-checks cleanly while
    silently inverting the semantics -- with a typical short pool, `len(reserve) >=
    requested` would suppress the notice entirely, the exact silent-shrink bug this
    function exists to catch. Forcing `reserve=` at the call site makes that transposition
    a TypeError instead of a silent regression.

    STDOUT (not stderr) notice naming every board seat that is unavailable, whenever
    the live pool comes up short of the requested `pool_size`. This is the pre-dispatch
    counterpart to `outcome.degraded`'s existing notice: that one only fires for the
    rarer reserve-exhausted-DURING-dispatch case, and only to stderr. A pool that never
    even reached `pool_size` because seats were unavailable BEFORE dispatch had NO
    notice anywhere -- an agent piping review output into a PR/TG report keeps stdout
    and drops stderr, so this silent shrink was invisible to every downstream reader of
    the review (Alex, 2026-08-28: a report said "reviewed via codex + Fable" with zero
    mention that Opus/GLM were configured but missing -- two different seats, two
    different root causes, both silent).

    A healthy seat sitting in `reserve` (not unavailable, just outranked by priority or
    already covering another role) is NOT reported -- unused reserve capacity is normal,
    not a gap. Only board seats absent from BOTH `pool` and `reserve` (i.e. genuinely
    unreachable per `backend_unavailable_reason`) count as the gap.

    `pool_size` is the RAW requested size. The selector itself clamps it via its own
    `_effective_pool_size` call: `split_pool_reserve` (and `select_pool_with_reuse` when
    `usage_percent` is None) against `len(reachable)` -- the LIVE seat count; as of
    task-cli#139, `select_pool_with_reuse`'s reuse-aware path (a role-lensed board,
    `usage_percent` given, a positive `pool`) instead clamps against `len(board)` -- the
    padding loop can fill roles beyond what's merely reachable. Either way this is a
    TIGHTER clamp than this notice needs: an operator asking for `--pool 8` against a 4-seat board
    legitimately gets a 4-seat live pool with nothing unavailable. Reporting against the
    raw, unclamped `pool_size` in that case prints an arithmetically nonsensical
    "requested 8, only 3 live -- 1 unavailable" (k3 review finding, round 1: 8-3=5
    unaccounted-for vs. 1 named seat, because 4 of the 8 never existed on the board). So
    this notice re-clamps separately, against the LOOSER `min(pool_size, len(board))` --
    what the board could ever actually deliver, board size rather than live-seat count --
    not the raw ask (GLM review finding, round 2: this is deliberately NOT the same
    clamp the selector applies; syncing the two would silence the exact
    `2-of-6-seats-down --pool 5` case this notice exists to catch).

    `pool_size <= 0` means "run every available seat" (`--pool 0`/no preset) -- there is
    still a real target here, `len(board)`, so this must NOT be treated the same as "no
    shortfall concept applies" (k3 review finding, round 2: a `--pool 0` run against a
    10-seat board with 2 seats down would otherwise print nothing, reproducing the exact
    incident this notice exists to kill under the "all seats" configuration).

    KNOWN LIMITATIONS (tracked as review-cli#277, not fixed here -- this notice is
    strictly more visible than the prior silence, but none of the gaps below block
    that): (1) the reason shown is RE-PROBED via `backend_unavailable_reason` on the
    RAW board spelling rather than the actual reason the selector excluded the seat
    for; for a seat truly excluded by the chain-aware availability probe (which
    alias-expands) this is usually still accurate (the chain probe is strictly
    weaker-or-equal to this re-probe for a CONCRETE spelling, so a seat that failed the
    former usually fails the latter too, with a real reason). Narrow exception (k3
    review finding, round 4): a bare-ALIAS board seat (e.g. `glm52`, not `oc:zai/glm-5.2`)
    re-probes the unexpanded alias, which can miss an unpaid-provider mark or resolve to
    a different backend than the chain actually tried -- "reason unknown" (or a
    misleading reason) is reachable for that specific shape, not fully dead code. (2) a
    seat excluded for being NEAR its usage limit (the `usage_percent`-based reuse path in
    config.py) is technically reachable, so the selector pads the pool back up with a
    duplicate seat instead of leaving a gap -- `len(pool) >= requested` then true, and
    this function returns before ever inspecting that seat, so the near-limit exclusion
    is invisible here, not merely mislabeled (Fable review finding, round 2). As of
    task-cli#139, the SAME limitation equally covers a HARD-unavailable seat that got
    padded over rather than excluded for being near a usage limit (`select_pool_with_reuse`
    now reuses a reachable model across roles when other seats are cooldown/down too, not
    only when they're merely near their usage threshold) — a fully-padded pool silences
    this notice exactly the same way whether the other seats were near their limit or
    genuinely unreachable; `_warn_if_board_reused`'s stderr line is the only signal that
    ANY padding happened at all in either case (codex review finding, task-cli#139). This
    is strictly the PADDING-SUCCEEDS case (the function never reaches the code below); the
    padding-FAILS case (a role-less or too-few-role board can't pad at all, so `pool`
    stays short with the near-limit/unavailable/role-excluded seats sitting healthy in
    `reserve`) IS handled below -- see the `reserve` line in the printed notice (Codex
    review finding, PR #278). (3) This notice prints at pre-dispatch time, before whatever
    produces a "reviewed via X + Y" summary runs -- an agent that only pastes the final
    summary (not the full terminal output) still misses it."""
    # `_effective_pool_size` (config.py) is the single source of truth for this clamp
    # (its own docstring: "so the two [select_pool + split_pool_reserve] can never
    # drift") -- re-deriving the rule inline here would let a future change to that
    # helper silently desync the notice's "requested N" from what the selector actually
    # targeted (GLM review finding, round 8).
    requested = _effective_pool_size(len(board), pool_size)
    if len(pool) >= requested:
        return
    live_ids = {id(r) for r in pool} | {id(r) for r in reserve}
    missing = [r for r in board if id(r) not in live_ids]
    # No early `if not missing: return` here (Codex review finding, PR #278): `board`
    # is exactly `pool | reserve | missing` (disjoint), and `requested <= len(board)`
    # by construction of `_effective_pool_size` above -- so having reached this line
    # (`len(pool) < requested`), `missing` and `reserve` can never BOTH be empty. A
    # role-less (or too-few-distinct-roles) board can shrink `pool` below `requested`
    # via `select_pool_with_reuse`'s role-padding cutoff with EVERY seat reachable --
    # `missing` empty, `reserve` holding the unused-but-healthy seats -- and the old
    # guard silently returned here, reproducing the exact "zero signal" bug this
    # notice exists to kill, just via a role-limit cause instead of an availability one.
    lines = [
        f"[review-cli] pool came up short: requested {requested} seats "
        f"(board has {len(board)}), only {len(pool)} live"
        + (f" -- {len(missing)} board seat(s) unavailable:" if missing else ":")
    ]
    for r in missing:
        reason = backend_unavailable_reason(r.model) or "unavailable (reason unknown)"
        label = r.display or r.model
        lines.append(f"  - {label} ({r.model}): {reason}")
    if reserve:
        # Codex review finding, PR #278: printing ONLY `missing` here made a role/usage
        # -driven shrink (healthy seats sitting unused in `reserve` because the board
        # had too few distinct roles to pad into, or because they're near their usage
        # limit) look like it was fully explained by whatever unavailable seat happened
        # to also be on the board -- e.g. "1 selected + 3 healthy reserve + 1 unavailable"
        # printed as "only 1 live -- 1 unavailable", falsely pinning a 3-seat gap on one
        # seat. State the reserve count as its own fact instead of folding it into the
        # unavailable-seat blame.
        lines.append(
            f"  ({len(reserve)} more board seat(s) are reachable but sitting in "
            "reserve -- excluded from the pool by role/usage-based selection, not "
            "availability)"
        )
    # flush=True (GLM review finding, round 5): every sibling operator notice in this
    # file flushes (_warn_if_board_reused, the degraded warning, the provider-switch
    # notice) -- without it, a piped/teed run (`review diff | tee report.md`) block-
    # buffers this print, and a run killed mid-dispatch loses it entirely, silently
    # reproducing the exact "zero signal" incident this notice exists to fix.
    # Under --json this notice would print BEFORE the JSON envelope and break
    # `json.loads(stdout)` for the exact degraded-pool case a machine consumer most
    # needs reliable signal for (P1 review finding, review-cli#546) -- route it to
    # stderr there instead, matching every OTHER operator notice in this module.
    print("\n".join(lines), file=sys.stderr if json_output else sys.stdout, flush=True)


# The one pre-commit invocation every gate remediation points at. Spelled ONCE: every
# recorded review mode requires `--task CODE`, so a rerun command without a task-code
# placeholder fails at the first keystroke whenever $REVIEW_TASK_CODE is unset (Codex
# finding on #359) — five copies drifting apart is how that regresses one string at a time.
_RERUN_STAGED = "review diff --task <CODE> --staged -C <repo>"
# Its `--commit` sibling, for the `--commit` usage and checkpoint refusals (all of which
# already run inside the repo, so no `-C <repo>`).
_RERUN_STAGED_COMMIT = "review diff --task <CODE> --staged --commit"


# Why an OK review did not satisfy the commit gate — and what to do about it.
#
# This is the single source of truth for the DIFF-SHAPE half of the gate predicate:
# `_stamp_if_staged_commit_review` does not re-spell those conditions, it asks here and stamps
# iff the answer is None. An earlier draft carried the predicate twice (the `if` plus this
# function) with a comment promising they would not drift; a review pointed out that a future
# fourth clause added to the `if` alone would fall through to "no reason" and skip the warning
# SILENTLY — which is the very bug this code exists to kill. One copy, no promise needed.
#
# Two conditions deliberately live in the CALLER instead, because neither is a property of the
# diff's shape (iteration-2 accuracy finding — the earlier "SINGLE source of truth for the gate
# predicate" claim was overstated by exactly these two):
#   * `ok`   — a FAILED review is not this function's business; its own failure output is the
#              louder reason, and a gate line on top of it points at the wrong problem.
#   * the marker WRITE actually succeeding — only knowable after attempting it.
# Both still route their user-visible outcome through `_warn_commit_gate_not_satisfied` (or, for
# `ok=False`, through the review's own failure output), so no path stays silent.
#
# The gate is satisfied by exactly ONE shape of review, and every other shape used to leave the
# marker alone SILENTLY — which reads, from the caller's side, as "my review passed, so why is
# my commit still blocked?". A real detached agent burned ~40 minutes on that gap: it ran
# `review diff` (unstaged), saw every seat pass, found the marker stale, followed the gate's own
# README advice to `touch` the marker path by hand, and its headless permission policy
# auto-rejected the write to that external directory and killed the run. Naming the reason on
# stderr turns that into a one-line fix.
#
# The REMEDIATION is per-reason, not one shared suffix: "re-run it `--staged`" is right for an
# unstaged or piped run and WRONG for a truncated one (re-running truncates again — advice that
# loops is how an agent burns another 40 minutes).


def _commit_gate_skip_reason(
    staged: bool, diff_from_stdin: bool, truncated: bool
) -> tuple[str, str] | None:
    """`(reason, remediation)` for the first condition this review fails, or None when it
    satisfies the gate. Callers must only consult it for an OK review — a FAILED review has
    its own, louder reason (the failure itself) and is not this function's business."""
    # ORDER MATTERS, and `truncated` deliberately comes FIRST (Fable review finding on
    # iteration 2). Only ONE reason is reported — the first failing condition — so the
    # order decides which remediation a compound failure gets. `staged`/`diff_from_stdin`
    # are fixed by re-running differently; `truncated` is NOT: it persists across every
    # re-run of the same oversized diff. Checking `staged` first would tell an unstaged,
    # oversized run to "stage and re-run `--staged`", it would truncate again, and only
    # THEN would it hear "split the change" — one wasted multi-minute review round, which
    # is precisely the looping-advice failure this whole notice exists to prevent.
    if truncated:
        return (
            "the diff was truncated for dispatch, so no seat saw all of it",
            "split the change into smaller staged commits (or raise $REVIEW_DIFF_MAX_BYTES) "
            f"and review each part with `{_RERUN_STAGED}` — re-running this "
            "same diff truncates again, and piping a smaller one in still fails the "
            "index-provenance check",
        )
    if not staged:
        return (
            "the review was not `--staged` (the gate certifies the staged index)",
            f"stage what you are about to commit and re-run `{_RERUN_STAGED}`",
        )
    if diff_from_stdin:
        return (
            "the diff was piped on stdin, not read from `git diff --cached`",
            f"re-run `{_RERUN_STAGED}` and let it read the index itself",
        )
    return None


# The two artifacts a gate-eligible review writes, and the consequence of losing each.
# They are NOT interchangeable: agent-tools' `require-review-before-commit` hook stats the
# session MARKER, while the local git pre-commit hook verifies the diff-scoped REVIEW-STAMP
# against the diff being committed. A first version of this warning hard-coded the marker
# into its frame and let the stamp branch borrow it, which produced a line that opened
# "commit-gate marker not written" for a run whose marker HAD been written and then sent
# the reader to check $REVIEW_MARKER while the broken file was the stamp (Fable finding,
# iteration 7). Naming the artifact is the whole point of this warning; getting it wrong is
# worse than staying silent.
# Each entry is (artifact, consequence, closing warning). The CLOSING warning differs too,
# and not decoratively: the marker is mtime-stat'd, so a hand-made one is indistinguishable
# from a real one and forging it is the whole hazard. The stamp carries the hash of the
# reviewed diff, so hand-making a USEFUL one means computing the hash of the very diff you
# are about to commit — the forgery advice does not transfer, and printing it there would
# be one more sentence pointing at the wrong problem (Fable finding, iteration 8).
_GATE_MARKER = (
    "commit-gate marker",
    "A pre-commit review gate keyed on that marker (e.g. agent-tools' "
    "require-review-before-commit) will not count this run:",
    "Never create it by hand — a completed staged review is what writes it, so a "
    "hand-made one certifies a review that did not happen.",
)
# The SKIP path (unstaged / piped / truncated) writes NEITHER artifact, so naming only one
# of them invites the reader with the other gate installed to conclude the line is not
# about them (Fable finding, iteration 12).
_GATE_BOTH = (
    "commit-gate marker and diff-scoped review-stamp",
    "Neither pre-commit review gate — agent-tools' require-review-before-commit, which "
    "stats the marker, nor the git hook that verifies the stamp — will accept this run:",
    "Never create either by hand — a completed staged review is what writes them, so a "
    "hand-made one certifies a review that did not happen.",
)
_GATE_STAMP = (
    "diff-scoped review-stamp",
    "A pre-commit git hook that verifies the reviewed diff against that stamp (the one "
    "`review install-commit-hook` places) will reject the commit:",
    "Re-running the review is the fix — the stamp records the hash of the diff that was "
    "actually reviewed, so there is nothing useful to write there by hand.",
)


def _warn_commit_gate_not_satisfied(
    reason: str,
    remediation: str,
    *,
    artifact_and_consequence: tuple[str, str, str] = _GATE_MARKER,
) -> None:
    """One stderr line: which artifact is missing, why, and the fix for THAT reason.

    States only what review-cli can observe — that it did not write the marker. It does
    not claim a commit "will be blocked": whether a gate is installed at all, whether a
    commit is even pending, and whether an earlier staged run left a still-fresh marker
    are all outside this process's knowledge. It does warn off the hand-`touch`
    workaround, which is review-cli's own contract to state: the marker means "review-cli
    ran and passed", so a hand-written one certifies a review that never happened.

    UNCONDITIONAL, and that is a deliberate trade-off rather than an oversight — three
    review seats raised it across two iterations, so the reasoning is recorded here. The
    obvious alternative is to stay quiet unless a gate is plausibly installed, proxied by
    "$REVIEW_MARKER is set" or "the marker path already exists". Rejected: both proxies
    are wrong in the direction that costs the most. The marker exists only AFTER a first
    passing staged run, so on a fresh machine — a new checkout, a fresh container, a
    detached agent's first task, exactly where nobody has the context to diagnose a
    blocked commit — the proxy says "no gate" and the line disappears at the one moment
    it is worth the most. The cost of being wrong the other way is one hedged stderr line
    on a review that passed. The accepted price is that plain `review diff` (the default,
    most common shape) always ends with this line, so it must stay one line and stay
    hedged; if it ever grows into a paragraph, revisit the trade-off, not the wording."""
    artifact, consequence, closing = artifact_and_consequence
    print(
        f"[review-cli] {artifact} not written — {reason}. {consequence} "
        f"{remediation}. {closing}",
        file=sys.stderr,
        flush=True,
    )


def _stamp_if_staged_commit_review(
    ok: bool,
    staged: bool,
    diff_from_stdin: bool,
    cwd: Path,
    diff: str,
    truncated: bool,
    *,
    stamp_diff_hash: str | None = None,
) -> None:
    """Write the diff-scoped review-stamp and touch the session marker iff this review
    genuinely satisfies the staged commit gate. Shared by the flat and board paths so the
    gate condition stays in ONE place — and the condition itself lives in
    `_commit_gate_skip_reason`, which this function only consults.

    Conditions (all required; `ok` is checked here, the rest in `_commit_gate_skip_reason`):
      * `ok`             — the review actually passed (every seat usable / not degraded).
      * `staged`         — `--staged`; an unstaged/working-tree review is not the gate.
      * NOT `diff_from_stdin` — the diff came from `git diff --cached`, not piped stdin.
        `printf ... | review --staged` reviews ARBITRARY stdin, not the index, so it must
        not satisfy the commit gate. The stamp is diff-scoped (the hook re-derives the
        cached diff and compares), but the marker is mtime-only and would otherwise be
        forgeable this way — so both are gated on real-index provenance.
      * NOT `truncated` — codex review finding: the stamp certifies the UNCAPPED
        canonical `diff`, but every seat only saw the CAPPED, truncated copy when this
        is True — so a truncated staged review no longer satisfies the pre-commit hook
        gate it would otherwise pass. (`_warn_if_dispatch_diff_truncated` has already
        printed the loud stderr warning explaining the truncation itself before this
        function is ever called; the gate line below adds only the gate consequence.)

    When the review is `ok` but one of the other conditions fails, this NAMES the failed
    condition on stderr (`_warn_commit_gate_not_satisfied`) instead of skipping silently
    — see `_commit_gate_skip_reason` for the incident that motivated it. The same holds
    for the one branch that DOES attempt the write and loses: a gate-eligible run whose
    marker write fails is reported too, so "the review passed but the marker is stale" is
    never an unexplained state. A FAILED review (`ok=False`) still skips silently: its own
    failure output is the reason, and a gate line on top of it would just be noise.

    `truncated` has NO default (Fable review finding): a defaulted `False` would be
    fail-OPEN — a future call site that simply forgets to pass it would silently
    certify a truncated review again, exactly the bug this parameter exists to prevent.
    Every current caller already computes and threads it explicitly.

    `stamp_diff_hash` (round-5 review finding, k3+Opus): the pre-commit-hook-compatible
    hash captured by the CLI at DISPATCH time (adjacent to `diff`'s own capture), passed
    straight through to `_write_review_stamp` so the stamp certifies what the models
    actually reviewed rather than whatever `git diff --cached` returns MINUTES later at
    stamp-write time (a real TOCTOU window: a concurrent index mutation during the panel
    run could otherwise get silently certified as reviewed). None for any non-CLI caller
    of `mode_review` — `_write_review_stamp` falls back to its own independent re-derive.
    """
    if not ok:
        return  # the review's own failure output already says why; no gate line on top of it.
    skipped = _commit_gate_skip_reason(staged, diff_from_stdin, truncated)
    if skipped is not None:
        _warn_commit_gate_not_satisfied(*skipped, artifact_and_consequence=_GATE_BOTH)
        return
    stamp_error = _write_review_stamp(cwd, diff, stamp_diff_hash=stamp_diff_hash)
    marker_error = _touch_review_marker()
    if stamp_error is None and marker_error is None:
        return
    # The gate-ELIGIBLE run whose own writes failed (iteration-2 review finding, raised
    # independently by three seats; extended to the stamp on iteration 6). Both writes are
    # best-effort by design — neither an unwritable REVIEW_MARKER nor an unwritable
    # `.git/review-stamp` may fail a good review — but swallowing a failure silently
    # rebuilds the original trap one level down: the review passes, every doc says a
    # passing staged run satisfies the gate, it does not, and the caller concludes the
    # gate is broken and forges the marker by hand.
    #
    # The two feed DIFFERENT gates and so are reported separately: agent-tools'
    # `require-review-before-commit` stats the marker, while the local git pre-commit hook
    # verifies the diff-scoped stamp. Losing either one blocks a commit on its own, and
    # each has its own remediation, so a merged "something failed" line would send the
    # caller to fix the wrong file half the time.
    if marker_error is not None:
        _warn_commit_gate_not_satisfied(
            f"the marker file could not be written ({marker_error})",
            "point $REVIEW_MARKER at a writable path (or fix the permissions on the "
            f"default one) and re-run `{_RERUN_STAGED}`",
        )
    if stamp_error is not None:
        _warn_commit_gate_not_satisfied(
            f"the write failed ({stamp_error})",
            "check that `.git/review-stamp` (`git rev-parse --git-path review-stamp`) is "
            f"writable, then re-run `{_RERUN_STAGED}`",
            artifact_and_consequence=_GATE_STAMP,
        )


_CHECKPOINT_COMMIT_MESSAGE = "chore: checkpoint via review diff --staged --commit"

# Matches `git commit`'s own stdout summary line — `[branch abc1234] message`, for the
# repo's very first commit `[branch (root-commit) abc1234] message`, or in detached HEAD
# `[detached HEAD abc1234] message` (codex P2 on review-cli#120: "detached HEAD" is TWO
# words where every other ref-description is one token, so it needs its own alternative —
# tried first, or `\S+` would greedily match just "detached" and the rest wouldn't line
# up). Git prints this using the SHA it just created, BEFORE running the post-commit hook
# — see `_parse_new_commit_sha` for why that ordering is exactly what makes this reliable,
# and why `_parse_new_commit_sha` still cross-checks the match rather than trusting it
# blind (a hook can print its OWN stdout into the same captured stream).
_COMMIT_SUMMARY_RE = re.compile(
    r"^\[(?:detached HEAD|\S+)(?:\s+\(root-commit\))?\s+([0-9a-fA-F]+)\]", re.MULTILINE
)


def _current_staged_diff(cwd: Path) -> str | None:
    """Re-derive the CURRENT staged diff, anchored `-C <cwd>` AND pinned via
    `git_repo_env(cwd)` — the exact same invocation shape as `cli._git_diff` (review-
    cli#71/#72), so a foreign GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE can't divert the
    comparison to the wrong repo. Returns None on ANY failure (spawn error, timeout,
    non-zero git diff) — the caller treats "can't verify" the same as "verification
    failed": don't commit."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_new_commit_sha(cwd: Path, commit_stdout: str) -> str | None:
    """Extract the SHA `git commit` reports it just created, from ITS OWN stdout summary
    line — never `git rev-parse HEAD` read after the fact (codex P1 on review-cli#120):
    a post-commit hook, or a concurrent session sharing the checkout, can advance HEAD
    past our commit before we get to read it, and a naive `rev-parse HEAD` would then
    verify/undo THAT later commit instead of ours — up to and including resetting away a
    completely unrelated commit that just happened to land in the window. Git prints the
    summary line using the commit it just made, BEFORE invoking the post-commit hook, so
    it always names OUR commit regardless of what happens afterward.

    `commit_stdout` is the WHOLE captured stream, which also carries anything a
    pre-commit/commit-msg hook printed before git's own summary line (codex P2 on
    review-cli#120: a hook that happens to print something bracket-shaped could win a
    naive "first regex match"). So every candidate match is checked against reality: it
    must resolve to a real object (`rev-parse --verify`) whose commit message is EXACTLY
    `_CHECKPOINT_COMMIT_MESSAGE` — a coincidental bracket-shaped hook line practically
    never also names a real object with that exact message. Returns the first candidate
    that checks out, or None if none do — the caller treats that as "can't verify,"
    refusing to touch history rather than guess."""
    for m in _COMMIT_SUMMARY_RE.finditer(commit_stdout):
        try:
            proc = _run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", m.group(1)],
                cwd=cwd,
                env=git_repo_env(cwd),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        sha = proc.stdout.strip()
        try:
            subject_proc = _run(
                ["git", "-C", str(cwd), "log", "-1", "--format=%s", sha],
                cwd=cwd,
                env=git_repo_env(cwd),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if (
            subject_proc.returncode == 0
            and subject_proc.stdout.strip() == _CHECKPOINT_COMMIT_MESSAGE
        ):
            return sha
    return None


def _resolve_parent(cwd: Path, sha: str) -> tuple[str, str | None]:
    """Resolve `sha`'s parent, distinguishing a POSITIVELY CONFIRMED root commit (zero
    parents) from a lookup that merely FAILED (git error, corrupt repo, permissions) —
    conflating the two (codex P2 on review-cli#120) let a transient failure masquerade as
    "root commit", and the caller (`_undo_checkpoint_commit`) would then `update-ref -d
    HEAD` — un-borning the WHOLE branch — for a commit that might not be root at all.

    Returns `("root", None)` only when `git rev-list --parents` positively reports zero
    parents, `("parent", <sha>)` for exactly one, or `("unknown", None)` for anything
    else (lookup failure, a merge commit with 2+ parents — this feature only ever makes
    plain commits, so that shape itself means "not the commit we think it is"). Callers
    MUST refuse to act on "unknown" rather than guess."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "rev-list", "--parents", "-n", "1", sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", None
    if proc.returncode != 0:
        return "unknown", None
    parts = proc.stdout.split()
    if len(parts) == 1:
        return "root", None
    if len(parts) == 2:
        return "parent", parts[1]
    return "unknown", None


def _empty_tree_sha(cwd: Path) -> str | None:
    """This repo's empty-tree object hash, used to diff a root commit (no parent)
    against "nothing" — computed via `git mktree` on empty input rather than the
    hardcoded SHA-1 constant `4b825dc642...` (codex P2 on review-cli#120: that constant
    is wrong in a `--object-format=sha256` repo, where it would make every root-commit
    checkpoint mismatch and undo a perfectly good commit). `mktree` writes using
    whatever object format THIS repo is configured for, so it is correct either way."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "mktree"],
            cwd=cwd,
            env=git_repo_env(cwd),
            input_text="",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _commit_diff(cwd: Path, sha: str) -> str | None:
    """The diff `sha` actually introduces (against its parent, or this repo's empty tree
    for a CONFIRMED root commit) — used to verify AFTER the fact that a just-made commit
    contains exactly the reviewed diff and nothing a hook smuggled in. None on any
    failure OR on an unresolvable parent status (the caller treats "can't verify" as
    "verification failed": don't trust the commit, but also don't guess at an undo)."""
    status, parent = _resolve_parent(cwd, sha)
    if status == "unknown":
        return None
    base = _empty_tree_sha(cwd) if status == "root" else parent
    if base is None:
        return None
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", base, sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _undo_checkpoint_commit(cwd: Path, sha: str) -> bool:
    """Undo a just-made checkpoint commit WITHOUT touching the index/working tree — so
    whatever a hook staged is left exactly as-is for the user to inspect, same spirit as
    `git reset --soft HEAD~1` (documented as the safe undo for this feature). A CONFIRMED
    root commit has no parent to reset to, so it's un-born instead.

    Uses `git update-ref <ref> <new> <old>` — a compare-and-swap, not a blind `git reset
    --soft`/`update-ref -d` (codex P1 on review-cli#120): those act on whatever HEAD
    currently is, and if HEAD has ALREADY moved past `sha` (a post-commit hook, or a
    concurrent session, making its own commit on top), a blind reset would silently drag
    that later, unrelated commit's ref-value back too — discarding it as if it never
    happened. The CAS only succeeds when the ref's CURRENT value is exactly `sha`;
    otherwise it fails and nothing is touched, which is exactly the outcome we want when
    we can no longer be sure `sha` is still what HEAD points to.

    Returns True iff an undo actually ran. Returns False — touching NOTHING — when the
    parent status is "unknown" (refusing to guess whether it's root — codex P2 on
    review-cli#120) OR when the CAS itself was rejected (HEAD moved). The caller surfaces
    either case as a distinct "could not safely undo" message."""
    status, parent = _resolve_parent(cwd, sha)
    if status == "unknown":
        return False
    if status == "root":
        proc = _run(
            ["git", "-C", str(cwd), "update-ref", "-d", "HEAD", sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    else:
        proc = _run(
            ["git", "-C", str(cwd), "update-ref", "HEAD", parent, sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    return proc.returncode == 0


def _verify_checkpoint_matches_review(cwd: Path, sha: str, reviewed_diff: str) -> str:
    """Post-commit gate (codex review finding on this feature's own PR, review-cli#120):
    a pre-commit hook that auto-formats/lint-`--fix`es and RE-STAGES files, then exits 0,
    lets `git commit` succeed — but the tree it commits is read from the index AS IT
    STANDS when the hook finishes, which can now hold MORE than `reviewed_diff`. The
    TOCTOU guard in `_checkpoint_commit` only catches drift BEFORE `git commit` runs; it
    cannot catch a hook that mutates the index AS A SIDE EFFECT of that same call, since
    the tree snapshot is taken after the hook exits, not before.

    So this re-diffs `sha` (the commit `git commit` JUST created — resolved by the
    caller via `_parse_new_commit_sha`, NOT a fresh `rev-parse HEAD`) against its parent
    and compares it to `reviewed_diff`. A mismatch means the hook smuggled in
    extra/different content — the commit is undone (`_undo_checkpoint_commit`,
    index/worktree left alone so nothing is lost) and a non-empty detail string is
    returned. Empty string means verified clean.
    """
    committed_diff = _commit_diff(cwd, sha)
    if committed_diff is not None and committed_diff == reviewed_diff:
        return ""
    if not _undo_checkpoint_commit(cwd, sha):
        return (
            "the commit at "
            + sha
            + " may hold more than the reviewed diff, but it could "
            "NOT be safely undone — either its parentage couldn't be positively confirmed, "
            "or HEAD has already moved past it (a post-commit hook or a concurrent session "
            "made another commit), so undoing would risk touching history that isn't ours. "
            "Nothing was changed; inspect `git show "
            + sha
            + "` / `git log -1` and clean up "
            "manually if it holds more than the reviewed diff."
        )
    return (
        "a pre-commit hook modified the index during the commit (e.g. auto-format/"
        "lint --fix re-staging files) — the resulting commit would have held more than "
        "the reviewed diff, so it was undone (index/working tree were left untouched). "
        "Inspect `git status`/`git diff --cached`, review the hook's changes separately, "
        "then re-run the checkpoint."
    )


def _checkpoint_commit(cwd: Path, reviewed_diff: str) -> tuple[bool, str]:
    """Create the real checkpoint commit of the currently-staged index.

    Anchored `-C <cwd>` AND a pinned `git_repo_env(cwd)` (review-cli#71/#72, the same
    reasoning as `cli._git_diff`): a leaked GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE pointing
    at a FOREIGN repo must not divert this commit to the wrong tree.

    TOCTOU guard (codex review finding on this feature's own PR): a review is
    multi-minute / multi-model, leaving a window in which another process or session
    sharing the same checkout could stage additional changes. Before committing, this
    re-reads the CURRENT staged diff (`_current_staged_diff`) and refuses to checkpoint
    if it no longer matches `reviewed_diff` — otherwise `--commit` could silently commit
    unreviewed/unrelated staged work, which is exactly the "sweep in someone else's
    changes" accident class this whole feature exists to prevent.

    A SECOND guard runs AFTER the commit (`_verify_checkpoint_matches_review`): the first
    guard cannot see a pre-commit hook that mutates the index as a side effect of the
    commit it is itself gating (codex P1 on review-cli#120) — only re-diffing the
    finished commit catches that.

    This is a REAL `git commit` — it runs the repo's own commit-msg/pre-commit hooks
    (lint/typecheck/tests, conventional-commit format) and can genuinely fail on a red
    tree or a malformed message. It never passes `--no-verify`: bypassing that gate is
    exactly the kind of accident this feature exists to prevent, not reproduce under a
    new flag. Returns `(succeeded, detail)` — `detail` is empty on success, else a short
    explanation (drift detected, spawn error, hook mismatch, or git's own stderr/stdout)
    for the caller to report.
    """
    current_diff = _current_staged_diff(cwd)
    if current_diff is None:
        return (
            False,
            "could not re-read the staged diff to verify it is still the reviewed one",
        )
    if current_diff != reviewed_diff:
        return False, (
            "the staged diff changed since the review ran (another process/session may "
            "have touched the index) — refusing to checkpoint a diff that was never reviewed"
        )
    try:
        proc = _run(
            ["git", "-C", str(cwd), "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git commit could not run: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "git commit failed"
    new_sha = _parse_new_commit_sha(cwd, proc.stdout)
    if new_sha is None:
        return False, (
            "the commit succeeded but its SHA could not be parsed from git's own output "
            "to verify it — nothing was undone; inspect `git log -1` manually"
        )
    mismatch_detail = _verify_checkpoint_matches_review(cwd, new_sha, reviewed_diff)
    if mismatch_detail:
        return False, mismatch_detail
    return True, ""


def _checkpoint_if_requested(
    commit: bool,
    ok: bool,
    diff_from_stdin: bool,
    cwd: Path,
    diff: str,
    truncated: bool,
) -> int | None:
    """Create the `--commit` checkpoint commit after a review, if requested.

    Called only after `mode_review` has already validated commit-implies-staged up
    front, so `staged` is always True here by construction — the remaining gate mirrors
    `_stamp_if_staged_commit_review`'s other conditions (`ok`, not `diff_from_stdin`,
    not `truncated`).

    Precisely: this CHECKPOINTS the reviewed staged diff after the review completes —
    it does NOT mean "commits when the review passes". `ok` means the pool produced
    usable verdicts, not that the diff is clean; a review that reports open findings
    still has `ok=True` and still gets checkpointed (findings are informational, the
    same as the existing `--staged` stamp gate). That is intended, not a bug.

    Returns `None` to leave the review's own 0/1 result standing (covers: `--commit`
    not requested; the gate not satisfied — the review failed, the diff was piped, or
    the diff was truncated for dispatch — in which case a note is printed but the
    review's own result is untouched; or the checkpoint commit itself succeeded).
    Returns `EXIT_COMMIT_FAILED` to OVERRIDE an otherwise-successful review's exit code
    when the review passed, the gate was satisfied, but the `git commit` subprocess
    itself failed. Returns `EXIT_COMMIT_DIFF_TRUNCATED` when the staged diff exceeded
    the dispatch cap (codex review finding): a checkpoint is supposed to certify the
    FULL reviewed diff, but every seat only saw the truncated copy — a stderr warning
    alone does not protect non-interactive automation from silently checkpointing a
    partial review, so this REFUSES rather than checkpoints. Either override is a
    visible, distinct failure, never silently swallowed as the review's own 0/1 result.

    `truncated` has NO default (Fable review finding): a defaulted `False` would be
    fail-OPEN — a future call site that simply forgets to pass it would silently
    checkpoint a truncated review again. Every current caller already computes and
    threads it explicitly.
    """
    if not commit:
        return None
    if not ok:
        print(
            "[review-cli] --commit: the review did not succeed — no checkpoint created.",
            file=sys.stderr,
            flush=True,
        )
        return None
    if diff_from_stdin:
        print(
            "[review-cli] --commit: the diff was piped on stdin, not the git index — no "
            "checkpoint created (a pipe reviews arbitrary input; --commit checkpoints the "
            "staged index).",
            file=sys.stderr,
            flush=True,
        )
        return None
    if truncated:
        print(
            "[review-cli] --commit: the staged diff exceeded $REVIEW_DIFF_MAX_BYTES and "
            "was TRUNCATED for dispatch — no checkpoint created (a checkpoint must certify "
            "the FULL reviewed diff, and every seat here only saw a partial view). Scope "
            "the review (`git diff --cached -- <path>`) or raise $REVIEW_DIFF_MAX_BYTES, "
            f"then re-run `{_RERUN_STAGED_COMMIT}`.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_COMMIT_DIFF_TRUNCATED
    succeeded, detail = _checkpoint_commit(cwd, diff)
    if succeeded:
        print(
            "[review-cli] --commit: checkpoint commit created.",
            file=sys.stderr,
            flush=True,
        )
        return None
    print(
        "[review-cli] --commit: the review succeeded but the checkpoint commit itself "
        f"FAILED — no checkpoint was created.\n  git commit failed: {detail}\n"
        "  fix: check the repo's commit-msg/pre-commit hooks (lint/typecheck/tests may be "
        f"blocking it), then re-run `{_RERUN_STAGED_COMMIT}`.",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_COMMIT_FAILED


def _mode_review_board(
    board: list[BoardReviewer],
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    staged: bool,
    pool_size: int,
    outcome_sink: list[FailoverOutcome] | None,
    *,
    diff_from_stdin: bool = False,
    visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
    commit: bool = False,
    usage_percent: Callable[[str], float | None] | None = None,
    stamp_diff_hash: str | None = None,
    json_output: bool = False,
    reporter: "BoardReporter | None" = None,
) -> int:
    """Board path: a priority-ordered FAILOVER pool of role-lensed reviewers.

    `board` is the FULL priority-ordered board (highest priority first). The active
    pool is the top-`pool_size` AVAILABLE seats by priority, distinct roles first
    (startup failover — an unreachable seat is skipped, a repeat-role seat waits in the
    reserve; review-cli#588); the remaining available seats are the RESERVE. The pool
    runs in parallel, and any seat that fails DURING the run (backend error, timeout,
    empty or "unavailable" output) is replaced by a seat that keeps its role
    (`panel._choose_backfill`) until `pool_size` usable verdicts are produced or nothing
    healthy is left (then it degrades, loudly).

    Output lists every seat that ran (successes and the failed seats that triggered a
    backfill, so the whole story is visible). Exit 0 (and the staged stamp) iff the pool
    refilled to `pool_size` usable verdicts on its owed distinct roles — i.e. NOT
    degraded. A failed-then-replaced seat is an expected, HANDLED failover event, not a
    review failure: the run still delivered the target number of working verdicts, so it
    must succeed (otherwise the paywalled-Fable case would make every `review --staged`
    fail despite a full, healthy pool). Only a genuine seat or role shortfall is a
    failure, and a role shortfall is printed as a `ROLE SHORTFALL` notice
    (`_report_role_shortfall`). `outcome_sink`, when given, receives the FailoverOutcome
    so the CLI can report the models that actually ran.

    `usage_percent`, when given, enables reuse-aware pool composition (Alex,
    2026-08-18) — the CALLER (cli.py's `_dispatch`) builds ONE tg-ctl usage
    snapshot per dispatch and passes the resulting closure down, rather than
    this function loading it itself. Defaults to None: NO usage-limit
    awareness, byte-identical to the pre-reuse `split_pool_reserve` selection
    — this keeps every existing/direct caller of `_mode_review_board` (tests
    included) hermetic by default; it never touches the real
    ~/.config/tg-cli/ file unless the caller explicitly opts in (k3/Fable
    review finding: an unconditional internal load broke test hermeticity for
    board-path tests that assert exact dispatched model sets).

    `reporter` (review-cli#548's optional live TUI observer, None by default) is forwarded
    to `run_board_with_failover` unchanged, then CLOSED the moment dispatch is done (or
    immediately if there is no pool at all) -- BEFORE the prose findings print below, not
    deferred to the CLI's own cleanup. This ordering matters: curses' initscr()/endwin()
    toggle the terminal's ALTERNATE screen buffer on a capable terminal (verified
    empirically -- TERM=xterm emits smcup/rmcup automatically), so anything printed while
    the alt-screen is still active becomes invisible the instant endwin() restores the
    main buffer. Every stderr notice in this function after the dispatch call is therefore
    a plain, unconditional print -- by construction, the reporter is always already closed
    by the time any of them can fire, so there is no "route to the reporter instead" branch
    left to write here (unlike `run_board_with_failover`'s own promotion notice, which
    fires WHILE the screen is still up). With `reporter=None` (every existing caller,
    unchanged), nothing here differs from before this parameter existed."""
    if exact_board:
        pool, reserve = list(board), []
    else:
        # Chain-aware, matching the pool guard (cli._chain_aware_available) — a raw
        # `backend_available` here would silently shrink the pool the guard just approved:
        # a seat whose head provider is down but has a live failover alternate is REAL live
        # (provider_chain would route around it at dispatch time), so the startup split must
        # agree (codex P1 on review of #157: 'the chain-aware guard approves seats that
        # board dispatch still removes').
        from ..provider_failover import any_provider_available

        def _chain_aware_available(r: BoardReviewer) -> bool:
            return any_provider_available(
                r.model,
                available=backend_available,
                unpaid=runtime_provider_marked_unpaid,
            )

        # Reuse-aware pool + reserve, both derived from ONE `_chain_aware_available`
        # pass (see select_pool_and_reserve_with_reuse's docstring): fills every
        # role slot even when fewer than `pool_size` DISTINCT models are
        # reachable/under their usage limit, by repeating an already-available
        # model across the remaining roles instead of shrinking the board
        # (Alex, 2026-08-18). `usage_percent` is None by default (see this
        # function's docstring) -- the caller opts in explicitly.
        pool, reserve = select_pool_and_reserve_with_reuse(
            board,
            pool_size,
            _chain_aware_available,
            usage_percent=usage_percent,
        )
        _warn_if_board_reused(board, pool)
        _report_pool_shortfall(
            board, pool, reserve=reserve, pool_size=pool_size, json_output=json_output
        )
    if not pool:
        # Close the TUI (if active) BEFORE printing -- curses' initscr()/endwin() toggle
        # the terminal's ALTERNATE screen buffer on capable terminals (verified
        # empirically: TERM=xterm emits smcup/rmcup automatically), so anything printed
        # while the alt-screen is still active is invisible the moment endwin() restores
        # the main buffer, TUI or not. Every message after this point is a normal print;
        # the reporter is never touched again in this function.
        if reporter is not None:
            reporter.close()
        print(
            "[review-cli] board: no reviewers are available — configure at least one "
            "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
            "PATH).",
            file=sys.stderr,
            flush=True,
        )
        return _predispatch_error_exit(json_output)

    # Capped copy for backend dispatch ONLY — `diff` itself (used below for the
    # stamp/checkpoint) stays uncapped (see cap_diff_for_dispatch's docstring). Same
    # stdin exemption as the flat path above.
    dispatch_diff = diff if diff_from_stdin else cap_diff_for_dispatch(diff)
    dispatch_diff_truncated = _warn_if_dispatch_diff_truncated(
        diff, dispatch_diff, staged, reporter
    )
    outcome = run_board_with_failover(
        pool,
        reserve,
        prompt,
        dispatch_diff,
        cwd,
        timeout,
        visual_images,
        reporter=reporter,
        reuse_healthy=not exact_board,
    )
    # Close the TUI (if active) THE MOMENT dispatch is done -- see the `if not pool:`
    # comment above for why this must happen before the prose findings print below, not
    # deferred to the CLI's own cleanup (cli.py still closes it too, defensively, for a
    # path that raises before reaching here -- close() is idempotent).
    if reporter is not None:
        reporter.close()
    if outcome_sink is not None:
        outcome_sink.append(outcome)

    # `report` stays None on the prose path (never read there); the --json path
    # builds+prints it here via `_print_json_report` so the SAME merged payload
    # backs both stdout and the exit-code decision below (`_json_exit_code`) --
    # never rebuilt/re-parsed a second time for the return value.
    report: dict | None = None
    if json_output:
        report = _print_json_report(outcome.results)
    else:
        # check_evidence=prompt.startswith(DEFAULT_PROMPT) — see the flat path's
        # identical note above (k3 review finding, rounds 6-7). Computed HERE,
        # inside the prose branch only (code-quality review finding,
        # review-cli#546): the --json branch above never calls format_result.
        check_evidence = prompt.startswith(DEFAULT_PROMPT)
        print(
            "\n\n---\n\n".join(
                format_result(r, check_evidence=check_evidence) for r in outcome.results
            )
        )

    if outcome.degraded and len(outcome.usable) < outcome.target:
        # The reporter is already closed by this point (see above) -- a plain print,
        # same as every non-TUI run, always correct here regardless of how this run
        # started.
        print(
            f"[review-cli] board: degraded — only {len(outcome.usable)} of "
            f"{outcome.target} seats produced a usable verdict (reserve exhausted).",
            file=sys.stderr,
            flush=True,
        )
    _report_role_shortfall(outcome, json_output=json_output)

    # Success gate: the pool refilled to its target of usable verdicts AND of distinct
    # roles (NOT degraded). A failed-then-replaced seat is handled, so it must not block
    # — only a real seat or role shortfall does (review-cli#588).
    ok = not outcome.degraded
    _stamp_if_staged_commit_review(
        ok,
        staged,
        diff_from_stdin,
        cwd,
        diff,
        dispatch_diff_truncated,
        stamp_diff_hash=stamp_diff_hash,
    )
    override = _checkpoint_if_requested(
        commit, ok, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
    if override is not None:
        return override
    if json_output:
        return _json_exit_code(report)
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Diff-mode-only options. `--retry` lives HERE, not on the global surface: in-seat retry
    applies only to the diff review path (the failover board + the flat `-m` panel), NOT to
    brainstorm/quorum/just-ask (which call run_panel and never use the retry wrapper). Keeping
    it off the top-level help avoids advertising a no-op flag outside diff review (AGENTS.md:
    the global list is only truly-global options; codex P1 on #46). The other shared options
    (-m / -C / --pool / --prompt / --staged / --visual …) come from the CLI's global surface."""
    parser.add_argument(
        "--retry",
        type=int,
        default=None,
        metavar="N",
        help=(
            "in-seat retries on a TRANSIENT failure (429/529/5xx/timeout/overloaded) before "
            "falling to the reserve (default from $REVIEW_RETRY_COUNT, else "
            f"{retry_default()}); applies to the failover board AND the flat -m panel. A "
            "SEAT-FATAL failure (auth/bad-model/501/refusal) is never retried. 0 disables it."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "checkpoint the reviewed diff with a real `git commit` after the review "
            "completes — REQUIRES --staged (errors otherwise, exit "
            f"{EXIT_COMMIT_REQUIRES_STAGED}). This checkpoints the reviewed staged diff, "
            "it does NOT mean 'commits only when the review passes': a review with open "
            "findings still gets checkpointed (same as the existing --staged stamp gate) "
            "— findings are informational. Runs the repo's own commit-msg/pre-commit "
            "hooks; a hook rejection fails the checkpoint distinctly (exit "
            f"{EXIT_COMMIT_FAILED}), never bypassed with --no-verify. Undo a bad "
            "checkpoint with `git reset --soft HEAD~1` (safe — leaves untracked/foreign "
            "files alone); never `git reset --hard` mid-review-cycle (wipes uncommitted "
            "work, including anyone else's, in a shared checkout)."
        ),
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help=(
            "disable the live per-seat status TUI (review-cli#548) even in a real "
            "interactive terminal -- same effect as $REVIEW_NO_TUI=1. The board falls "
            "back to the existing per-call log files + heartbeat lines. The TUI "
            "auto-enables ONLY when stdout AND stderr are both a real tty and curses can "
            "start a real screen; a piped/redirected/agent-driven invocation is "
            "unaffected either way, --no-tui or not."
        ),
    )


def _handler(ctx: ModeContext) -> int:
    """Thin over `mode_review`: the CLI resolves the board / pool / outcome_sink and
    passes them through `ctx.extra` (only the failover-board path needs them).

    The board path passes `board` + `pool_size` + `outcome_sink`; the flat path
    (explicit -m with no configured board/models) passes board=None and NOTHING else —
    the call shape there is identical to the pre-redesign default review (a board=None
    call with no pool_size/outcome_sink), so consumers/stubs of the flat path stay
    compatible."""
    board = ctx.extra.get("board")
    # A diff piped on stdin (vs read from `git diff --cached`) must not satisfy the staged
    # commit gate even under --staged — see _stamp_if_staged_commit_review.
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    commit = bool(getattr(ctx.args, "commit", False))
    # `--json` (ticket #546): reuses the SAME flag every mode parser already carries
    # (added by cli._add_visual_options for the composable --visual verdict JSON) —
    # for `review diff` (and a `review visual IMAGE --diff/--staged` companion run,
    # which reaches this SAME handler via visual.py's own `_handler = review._handler`)
    # that flag was previously a pure no-op: the standalone visual-verdict JSON branch
    # in cli.py only fires when there is NO diff at all, and diff review always has
    # one. Giving it a second, diff-review-scoped meaning here does not collide with
    # that standalone path (mutually exclusive by construction) or with quorum/
    # brainstorm/just-ask (which never reach this handler).
    json_output = bool(getattr(ctx.args, "json", False))
    # Captured by the CLI immediately adjacent to `ctx.diff`'s own capture (a
    # millisecond gap, not the multi-minute panel-run gap re-deriving it at
    # stamp-WRITE time would have) — see `install._write_review_stamp`'s
    # docstring. None for any non-CLI caller of `mode_review` (falls back to
    # that function's own independent re-derive).
    stamp_diff_hash = ctx.extra.get("stamp_diff_hash")
    prompt = ctx.with_visual(ctx.args.prompt)
    if json_output:
        prompt = _with_json_findings_request(prompt)
    base = (
        ctx.models,
        prompt,
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        ctx.args.staged,
    )
    if board is None:
        return mode_review(
            *base,
            board=None,
            diff_from_stdin=diff_from_stdin,
            visual_images=_visual_images(ctx),
            effort_override=ctx.effort_override,
            commit=commit,
            stamp_diff_hash=stamp_diff_hash,
            json_output=json_output,
        )
    return mode_review(
        *base,
        board=board,
        pool_size=ctx.extra.get("pool_size", DEFAULT_POOL_SIZE),
        outcome_sink=ctx.extra.get("outcome_sink"),
        diff_from_stdin=diff_from_stdin,
        visual_images=_visual_images(ctx),
        exact_board=bool(ctx.extra.get("exact_board", False)),
        commit=commit,
        stamp_diff_hash=stamp_diff_hash,
        usage_percent=ctx.extra.get("usage_percent"),
        json_output=json_output,
        reporter=ctx.extra.get("board_reporter"),
    )


MODE = ModeSpec(
    name="review",
    # The diff-review SUBCOMMAND is `diff` (renamed from the stuttering `review review`).
    # The stable mode `name` / `stats_mode` stay "review" (the run-stats key and the
    # handler dispatch identity are unchanged); only the user-facing verb moved.
    subcommand="diff",
    diff_policy="require",
    stats_mode="review",
    summary="diff review across the reviewer board (requires a diff; --staged --commit checkpoints progress)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=False,
)
