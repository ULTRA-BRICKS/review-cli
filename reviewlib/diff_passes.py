"""Combine the per-pass results of a multi-pass (chunked-diff) review into ONE report.

`review diff` on a diff over the per-pass budget runs the same board once per chunk
(`reviewlib.diff_chunks.split_diff`) inside ONE run. These helpers fold the passes back
together: one report per seat (its parts under `### Part i/n` headings) and one
`FailoverOutcome` for the run-stats record, so the run still counts as a single
iteration with all its seats. With a single pass the merge helpers return their input
unchanged — the within-budget path is byte-identical to a review without chunking.
"""

from __future__ import annotations

from dataclasses import replace

from .backends import ReviewResult
from .diff_chunks import DiffChunk
from .panel import (
    MISSING_EVIDENCE_WARNING,
    FailoverOutcome,
    clean_verdict_missing_evidence,
)


def with_part_note(prompt: str, chunk: DiffChunk) -> str:
    """The review prompt for `chunk`'s pass: unchanged for a single-pass run, else the
    prompt followed by the "Part i of n" note."""
    if chunk.total == 1:
        return prompt
    return f"{prompt}\n\n{chunk.prompt_note()}"


def describe_plan(chunks: list[DiffChunk], budget: int, total_bytes: int) -> str:
    """The multi-line notice naming every pass and the files it carries — printed once
    before dispatch so the split is never silent."""
    lines = [
        (
            f"diff is {total_bytes} bytes, over the {budget}-byte per-pass budget "
            f"($REVIEW_DIFF_MAX_BYTES): reviewing it in {len(chunks)} passes — every "
            "file is reviewed, none is cut."
        )
    ]
    for chunk in chunks:
        files = ", ".join(piece.label for piece in chunk.pieces)
        lines.append(
            f"  part {chunk.index}/{chunk.total}: {len(chunk.files)} file(s), "
            f"{chunk.size} bytes: {files}"
        )
    return "\n".join(lines)


def _distinct(values: list[str]) -> list[str]:
    return [value for value in dict.fromkeys(values) if value]


def _merge_seat(
    parts: list[tuple[DiffChunk, ReviewResult]], *, check_evidence: bool
) -> ReviewResult:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for chunk, result in parts:
        heading = f"### Part {chunk.index}/{chunk.total} ({len(chunk.files)} file(s))"
        body = result.stdout.strip()
        section = [heading, body or "(no output from this part)"]
        if body and check_evidence and clean_verdict_missing_evidence(body):
            section.append(MISSING_EVIDENCE_WARNING)
        stdout_parts.append("\n\n".join(section))
        if result.stderr.strip():
            stderr_parts.append(f"{heading}\n{result.stderr.strip()}")
    results = [result for _chunk, result in parts]
    return ReviewResult(
        model=results[0].model,
        command="; ".join(_distinct([r.command for r in results])),
        returncode=next((r.returncode for r in results if r.returncode != 0), 0),
        stdout="\n\n".join(stdout_parts),
        stderr="\n\n".join(stderr_parts),
        prompt_tokens=sum(r.prompt_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        served_by=next((r.served_by for r in results if r.served_by), ""),
        substitution="; ".join(_distinct([r.substitution for r in results])),
        account="; ".join(_distinct([r.account for r in results])),
    )


def merge_passes(
    passes: list[list[ReviewResult]],
    chunks: list[DiffChunk],
    *,
    check_evidence: bool,
) -> list[ReviewResult]:
    """One ReviewResult per seat label (first-appearance order) across `passes`, whose
    stdout carries every pass's answer under a `### Part i/n` heading.

    A single pass is returned as-is. For several passes the missing-evidence warning is
    already inlined per part, so the caller renders the merged results with
    `check_evidence=False`."""
    if len(passes) == 1:
        return passes[0]
    by_label: dict[str, list[tuple[DiffChunk, ReviewResult]]] = {}
    for chunk, results in zip(chunks, passes, strict=True):
        for result in results:
            by_label.setdefault(result.model, []).append((chunk, result))
    return [
        _merge_seat(parts, check_evidence=check_evidence) for parts in by_label.values()
    ]


def merge_outcomes(outcomes: list[FailoverOutcome]) -> FailoverOutcome:
    """Fold the per-pass board outcomes into the single outcome the run-stats record
    reads (`usable_models`/`usable_roles` listing each seat once, however many passes it
    answered). `degraded` is true when ANY pass was degraded."""
    if len(outcomes) == 1:
        return outcomes[0]
    seats: dict[tuple[str, str | None], None] = {}
    for outcome in outcomes:
        roles: list[str | None] = (
            list(outcome.usable_roles)
            if len(outcome.usable_roles) == len(outcome.usable_models)
            else [None] * len(outcome.usable_models)
        )
        for model, role in zip(outcome.usable_models, roles, strict=True):
            seats.setdefault((model, role), None)
    role_failures: dict[str, list[str]] = {}
    for outcome in outcomes:
        for role, lines in outcome.role_failures.items():
            known = role_failures.setdefault(role, [])
            known.extend(line for line in lines if line not in known)
    return replace(
        outcomes[0],
        results=[r for outcome in outcomes for r in outcome.results],
        usable=[r for outcome in outcomes for r in outcome.usable],
        target=max(outcome.target for outcome in outcomes),
        degraded=any(outcome.degraded for outcome in outcomes),
        usable_models=[model for model, _role in seats],
        usable_roles=[]
        if any(role is None for _model, role in seats)
        else [role for _model, role in seats if role is not None],
        role_target=max(outcome.role_target for outcome in outcomes),
        missing_roles=list(
            dict.fromkeys(role for o in outcomes for role in o.missing_roles)
        ),
        role_failures=role_failures,
    )
