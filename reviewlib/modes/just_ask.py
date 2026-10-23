"""just-ask: multi-model answer to a plain question (diff optional).

`review just-ask "<question>"` — a single-shot panel question. Originally the
`--just-ask` flag (Stage 0 decomposition); now a first-class SUBCOMMAND backed by the
self-describing `MODE` descriptor at the bottom of this file (see `modes/contract.py`).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from . import refuse_oversized_diff
from ..panel import (
    PanelJob,
    format_result,
    recount_round_by_usability,
    result_is_usable,
    run_panel,
    seat_no_answer_reason,
)
from ..process import run_deadline_expired
from . import _diff_context_block, _run_effort, _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..backends import ReviewResult
    from ..config import EffortOverride


def mode_just_ask(
    question: str,
    models: list[str],
    diff: str,
    cwd: Path,
    timeout: int,
    visual_images: tuple[Path, ...] = (),
    effort_override: "EffortOverride | None" = None,
    diff_from_stdin: bool = False,
) -> int:
    # The diff is embedded in ONE prompt per seat, so an over-budget diff
    # (`$REVIEW_DIFF_MAX_BYTES`) is refused loudly rather than cut — a piped diff
    # (`diff_from_stdin`) was scoped on purpose by the caller and is sent whole. Checked
    # here too (not only in cli.py) so a direct library caller is held to the same rule.
    if not diff_from_stdin:
        refusal = refuse_oversized_diff(diff, "just-ask")
        if refusal is not None:
            return refusal
    prompt = (
        "Answer this question directly and concisely. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(diff)
    )
    jobs = [
        PanelJob(
            model=model,
            prompt=prompt,
            diff="",
            images=visual_images,
            effort=_run_effort(effort_override, model),
        )
        for model in models
    ]
    results = run_panel(jobs, cwd, timeout)
    # glm-5.2 review finding (2026-08 seat-cooldown feature): `run_panel`'s own
    # auto-tally counts a cached-cooldown sentinel (rc=0, non-empty "unavailable"
    # body) as `ok` in run-stats — the same distortion brainstorm's
    # `recount_round_by_usability` call already fixes for its own rounds. Applied
    # here too so just-ask's run-stats never disagree with the `result_is_usable`
    # verdict this function's own return value already uses below.
    recount_round_by_usability(results)
    print("\n\n---\n\n".join(format_result(r) for r in results))
    # codex review finding (2026-08 seat-cooldown feature): a cached-cooldown-skip
    # result deliberately mirrors a live "is currently unavailable" sentinel — rc=0,
    # non-empty body — so a plain `returncode == 0` check reports SUCCESS for `review
    # just-ask -m fable` during a cooldown window even though no seat produced a real
    # answer. `result_is_usable` is the same predicate `mode_review`'s flat/board paths
    # already use for exactly this reason.
    if all(result_is_usable(r) for r in results):
        return 0
    print(_no_answer_report(results, timeout))
    return 1


def _no_answer_report(results: list["ReviewResult"], timeout: int) -> str:
    """Every seat just-ask waited on, and why each one without a usable answer gave
    none — timeout (with its last output), quota, no output, failed (review-cli#587)."""
    missing = sum(1 for r in results if not result_is_usable(r))
    head = (
        "no seat answered"
        if missing == len(results)
        else f"{missing} of {len(results)} seats gave no answer"
    )
    if run_deadline_expired():
        head += f" within --timeout {timeout}s"
    lines = [f"\n[review-cli] just-ask: {head}. Seats waited on:"]
    for r in results:
        why = "answered" if result_is_usable(r) else seat_no_answer_reason(r)
        lines.append(f"  - {r.model}: {why}")
    return "\n".join(lines)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to ask every backend")


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    if images:
        return mode_just_ask(
            ctx.with_visual(ctx.args.question),
            ctx.models,
            ctx.diff,
            ctx.cwd,
            ctx.timeout,
            images,
            effort_override=ctx.effort_override,
            diff_from_stdin=diff_from_stdin,
        )
    return mode_just_ask(
        ctx.with_visual(ctx.args.question),
        ctx.models,
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        effort_override=ctx.effort_override,
        diff_from_stdin=diff_from_stdin,
    )


MODE = ModeSpec(
    name="just-ask",
    subcommand="just-ask",
    diff_policy="none",
    stats_mode="just-ask",
    summary="single-shot panel question (diff optional)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
    aliases=("ask",),
)
