# Optional task codes and durable run association

Feature: https://git.hyperide.ai/ultrabricks/review-cli/issues/1

Review execution must not require a tracker identifier. Explicit `--task CODE` takes
precedence over `REVIEW_TASK_CODE`; omitted values are allowed in diff, just-ask,
quorum, brainstorm, visual and qa modes. Supplied empty, whitespace-only, multi-token,
control-character or overlong codes fail with actionable validation.

## Commands

```sh
review just-ask "What are the tradeoffs?"
review jobs --json
review status 20260914T120000-a1b2c3d4 --json
review attach 20260914T120000-a1b2c3d4 --task PROJECT-42
review task PROJECT-42 --json
```

IDs above are examples. The completion guidance prints THIS run's actual ID; only
`CODE` remains a placeholder. It asks to attach task-related work and suggests
creating a task if needed. Product execution and attachment create no tracker issues.
Guidance goes to stderr, preserving structured JSON stdout (including visual verdicts).
`review attach RUN_ID --task CODE --json` emits association/change metadata.

## Storage and identity

Use the existing jobs store and exact timestamp/random job ID format. Synchronous and
detached runs share `review jobs`, `status`, and `wait`. Management/help commands do
not execute models. Jobs retain original stdout/stderr, completion status/exit code,
start/finish times and the original stats evidence: model/role coverage, partial usable
roles, repository and diff identity. The append-only stats index carries the run ID;
readers resolve association from the job without rewriting the original stats row.
Evidence retained in the job can recover a missing index, scoped to its original stats
store. Call-log headers carry the same run ID so transcript readers resolve attachment.
Historical records without a run ID are not guessed or relabelled.

Attachment is a library operation (`reviewlib.jobs.attach_task`) plus a thin CLI.
It uses the job store's exclusive per-run lock and atomic replacement, with file and
directory fsync. Same-task attachment is idempotent; changing an existing task fails.
Concurrent different-task requests have one winner. Unrelated run/index appends are
not rewritten. Prefixes, paths, missing/malformed IDs, symlinks, mismatched records,
ambiguous primary/fallback records and incomplete output are rejected. A unique fallback-only
record is updated in its existing store; the command never accepts an arbitrary record path.

Completion guidance follows durable output and terminal status. Interrupted processes
retain their job identity and existing unknown-termination reconciliation; a run without
finalized output cannot be attached. A failed run stays failed. Original model verdicts
remain visible even when later output/CLI handling fails; such a run is not credited as
a passed task iteration. Association cannot use the current working tree as evidence.

Role planning is independent of task presence, including the existing single-model
role expansion. This feature does not implement the entire general models/roles design.

## Acceptance checks

Run `python -m pytest -q tests/test_optional_task.py` with pytest >=8,<9. Tests use a
fresh HOME and real CLI handlers/readers, replacing only provider boundaries. Cover
omitted/explicit/environment/invalid task values, role parity, original diff identity,
failed output, JSON, synchronous/detached identity, same/different-task attachment,
concurrent attachment and append, crash before replace, unsafe/ambiguous IDs and
recovery from a missing stats index. Existing stats/role/detached tests remain gates.
