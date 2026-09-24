# Review contracts: runtime reliability and review interfaces

Status: proposed implementation specification; documentation only.
Date: 2026-09-10.
Parent: [rig-cli#514](https://git.hyperide.ai/ultrabricks/rig-cli/issues/514).
Epics: [runtime #492](https://git.hyperide.ai/ultrabricks/review-cli/issues/492), [entity browser #487](https://git.hyperide.ai/ultrabricks/review-cli/issues/487), [versioned spec review #484](https://git.hyperide.ai/ultrabricks/review-cli/issues/484).

## Ownership and evidence

Review-cli owns review board composition, role-preserving execution/fallback, review-call evidence and its review interfaces. Harness-cli owns provider/account discovery, factual health observations and harness protocols. Agent-tools owns the ship-policy consumer. Telegram delivery and rig provisioning remain outside review-cli.

This grooming inspected issue bodies, selected comments, current source and PR metadata. It did not run the complete suite or contact live model accounts. A previous PR's reported test count is historical evidence, not a new run.

Verified baselines: [PR #488](https://git.hyperide.ai/ultrabricks/review-cli/pulls/488) delivered Phase-0 entities (merge `7552c3e68a99c00aade236128c2b0ffe7ae3ebb5`). [PR #485](https://git.hyperide.ai/ultrabricks/review-cli/pulls/485) remains open at inspected head `620d3eddf851bba9e19f4dbcc83b7579b7e2e877`; its anchor/comment UX must not be called shipped on main. The inspected `reviewlib/specweb/store.py` persists comments/replies/drafts and documents client-side reanchoring, not an audited revision-linked anchor chain.

## Review runtime

### Capacity evidence, not a blanket bypass

[#472](https://git.hyperide.ai/ultrabricks/review-cli/issues/472) produces the capacity report consumed by [agent-tools#705](https://git.hyperide.ai/ultrabricks/agent-tools/issues/705). Define the evaluated set from actual eligible review routes, including configured fallback candidates. Resolve each route to its account identity and report incomplete mappings explicitly.

The versioned result must include the inspected configuration/context identity, observation time, freshness/coverage diagnostics and per-candidate reasons. `all_exhausted=true` requires ALL of the following:

1. At least one eligible candidate exists.
2. Every eligible candidate is mapped and covered by applicable, fresh evidence.
3. Every such candidate is affirmatively exhausted under the documented predicate.
4. No collection/parse/schema error leaves an unknown candidate.

| Input condition | all_exhausted |
| --- | --- |
| All mapped candidates affirmatively exhausted with fresh evidence | true |
| One known usable candidate | false |
| Zero candidates | false, with empty-board diagnostic |
| Unknown, stale, unsupported or unmapped candidate | false, with incomplete-evidence diagnostic |
| Missing CLI, invalid schema, malformed JSON or failed collection | no affirmative proof; consumer refuses the exception |

Authentication failure, billing blockage, local database lock, network failure and a temporary rate window are not interchangeable with quota exhaustion. Record their distinct causes. A cooldown is an execution policy observation, not automatically proof of provider exhaustion.

Normal fallback may attempt an unknown-health candidate under policy. A protective-gate exception may not treat unknown as affirmative evidence. This distinction avoids conflating availability-oriented retry policy with authorization.

[#479](https://git.hyperide.ai/ultrabricks/review-cli/issues/479) owns account-specific selection: an exhausted account must not block an unrelated healthy account routed through the same provider/model. Conversely, routes sharing a genuine credential/quota account need explicit shared identity. Do not infer chosen account from a nearby timestamp when concurrent calls make that join ambiguous.

### Adapter architecture and approval gate

[#476](https://git.hyperide.ai/ultrabricks/review-cli/issues/476) remains DESIGN ONLY until the capacity crisis in #472 / agent-tools#705 is resolved AND Alex explicitly approves implementation. This document and its PR are not that approval.

Retain the existing decision as a historical, narrower proposal: [.haft decision record](https://git.hyperide.ai/ultrabricks/review-cli/src/branch/haft/476-backend-liveness-decision/.haft/decisions/476-backend-liveness-adapter-architecture.md). Moving `_opencode_model_needs_stall_watchdog` into a Python registry can consolidate data, but it does not by itself satisfy universal adapter ownership, config-only onboarding or meaningful progress/health detection.

The reviewed architecture must distinguish:

- Protocol adapter: capabilities and observable signals of a harness/provider transport.
- Route/account: the actual execution and credential context.
- Declarative behavior profile: known model/output characteristics using existing supported mechanisms.
- Execution state machine: dispatch, progress, stall suspicion, retry/fallback, cancellation and terminal outcome.

| Option | Benefit | Unresolved cost |
| --- | --- | --- |
| Extend the existing model registry only | Small migration for known timeout settings | Cannot alone own new signal kinds, live account state or generic protocol capabilities |
| Replace everything with a new parallel profile engine | A clean abstraction on paper | Duplicates established process, cooldown and configuration mechanisms; high migration risk |
| Normalize adapter observations and profiles through existing execution mechanisms | Keeps one enforcement path while separating facts and policy | Requires a real contract, migration map and negative conformance tests |

Recommended direction for review is the third option, reusing existing registry/process/cooldown code where appropriate. Final shapes and any promotion/replacement decision remain gated. Do not implement a new parallel backend stack merely to follow a name in this document.

Required observations distinguish emitted text, thinking/progress evidence, local process activity, transport state, account/quota facts and unknown. Silence or low CPU alone is not proof that a remote model is dead. Time limits are bounded-execution safeguards, not an account-health diagnosis.

The current narrow true-silence wiring in [#474](https://git.hyperide.ai/ultrabricks/review-cli/issues/474) remains credited. Contract tests must additionally distinguish no output, a stall after initial output, and silent but evidenced progress. Every supported route passes through the common policy seam; a new route with existing capabilities can be declared in configuration without a model-name branch in the dispatcher. A genuinely new protocol still needs an adapter, and this distinction must be stated honestly in onboarding proof.

Use the public [harness inventory/health contract](https://git.hyperide.ai/ultrabricks/harness-cli/issues/26), not imports of individual provider internals. Failed collection must not automatically become unauthenticated; unsupported collection must not set authenticated=true.

### Fallback, hangs and evidence

[#460](https://git.hyperide.ai/ultrabricks/review-cli/issues/460) owns actionable failure classification and viable fallback; #479 owns the account-specific slice. Preserve the requested role/effort constraints when selecting alternatives. Retry budgets and terminal reasons must be explicit; do not declare a successful review when no valid review completed.

[#486](https://git.hyperide.ai/ultrabricks/review-cli/issues/486) requires a reproduction and identification of the blocked process. `UV_PROJECT_ENVIRONMENT` is a hypothesis to test, not an established cause. Tests must prove resource cleanup and terminal reporting, not merely select a different large timeout. [#483](https://git.hyperide.ai/ultrabricks/review-cli/issues/483) adds missing OMP diagnostic coverage through the same route taxonomy.

Attempt-level evidence is split into existing tasks:

| Task | Required record |
| --- | --- |
| [#469](https://git.hyperide.ai/ultrabricks/review-cli/issues/469) | Skipped candidate with reason before dispatch |
| [#467](https://git.hyperide.ai/ultrabricks/review-cli/issues/467) | Every failed/stalled attempt and retry, not only the final one |
| [#468](https://git.hyperide.ai/ultrabricks/review-cli/issues/468) | Cancellation/termination with provenance; an uncatchable process death requires external reconciliation, not a fictional final write |
| [#465](https://git.hyperide.ai/ultrabricks/review-cli/issues/465) | Actual account/route attribution or explicit unknown |
| [#480](https://git.hyperide.ai/ultrabricks/review-cli/issues/480) | Durable compressed log storage and verified migration |

Correlate run, task, seat, attempt, route/account, source revision, timestamps and terminal reason. Redact credentials. For #480, select storage/compression using representative read/write and compatibility tests. Migration must verify counts/content and downstream statistics/session/brainstorm parity before retiring old data. Interruptions, repeat migration and rollback must preserve the originals. No destructive cleanup is authorized by grooming.

## Entity browser

The requested surface is a keyboard TUI, not another static list. [#487](https://git.hyperide.ai/ultrabricks/review-cli/issues/487) credits the merged Phase-0 text/JSON report and now tracks two remaining implementation children.

[#495](https://git.hyperide.ai/ultrabricks/review-cli/issues/495) extends one shared read model using board/role/statistics plus the public harness inventory contract. Stable IDs distinguish harness, provider, vendor, account, model/version and role/effort relationships. Preserve absent/stale/unsupported source diagnostics. A historical metric with no reliable account join is not assigned to the most recently active account.

[#496](https://git.hyperide.ai/ultrabricks/review-cli/issues/496) builds keyboard list/detail/stats navigation over that same model. Record the optional UI dependency decision and preserve lightweight help/import behavior. Non-TTY and explicit no-TUI mode return the readable report; JSON contains no terminal decoration. Support small terminals, focus visibility, filter/drill-down, refresh without overlapping queries and selection retention by stable ID. Display refresh/error age rather than freezing apparently live data. Provide a dashboard deep-link for the inspected context.

Acceptance includes interaction/PTY tests and durable screenshots for navigation, resize, refresh, incomplete live data and clean exit. The optional account-rotation action from the original Phase 3 requires a separate explicit decision and confirmation-gated task; it is neither implemented nor required implicitly by the read-only UI slice.

## Versioned spec review

[#484](https://git.hyperide.ai/ultrabricks/review-cli/issues/484) preserves five original outcomes: anchor-change auditing, version history/compare, content-fit comments, agent-directed reply hints, and consistent Resolve/Delete icons. The existing open PR #485 is a narrower UX baseline to review and reuse, not proof that all five outcomes shipped.

### Revision model — #493

[#493](https://git.hyperide.ai/ultrabricks/review-cli/issues/493) owns captured revisions and comparison. A revision stores stable identity, content hash, observed timestamp, content and relevant source metadata. mtime may trigger an inspection but is not sufficient identity: same-mtime content changes and duplicate saves need correct behavior. State explicitly that unobserved intermediate edits are not guaranteed to be recoverable.

Integrate snapshots with existing per-spec persistence without losing comments/replies/drafts. Atomic replacement alone is not concurrent read-modify-write protection: preserve/review the store's locking contract on supported platforms and make any weaker platform guarantees explicit. Test two writers, server restart and interrupted writes.

Compare git history with application snapshots as alternatives. Application snapshots cover uncommitted/out-of-repository edits and are the recommended baseline for the existing request; git history can be an additional source, not a mandatory dependency. Freeze API names/schema during implementation. Required operations list/fetch captured versions and compare any two selected captured revisions or a captured revision with live content, without mutating the live file.

Retention is bounded, with a reviewed policy for revisions referenced by comments/anchor events. A missing old revision is labeled unavailable, never silently substituted. Content-hash deduplication avoids repeated identical versions. The UI must keep a selected historical comparison stable during live/SSE updates and work with empty history, long documents and narrow viewports.

### Anchor history — #494

[#494](https://git.hyperide.ai/ultrabricks/review-cli/issues/494) depends on #493. Persist append-only events with comment ID, previous/next revision, old/new section and offset/text, observation time, resolution method and status/confidence. Do not overwrite the original quote with a heuristic candidate.

Exact, suggested, ambiguous and unresolved states are distinct. Repeated quotes, heading rename/move, deletions and multiple successive edits must not create a false-confirmed reanchor. A best-effort candidate may be displayed as a suggestion; confirmation is a separate state. Event processing must be idempotent across duplicate file-change notifications and restart.

The UI shows the old/new context and diff, with a visible unresolved fallback. Browser and persistence tests prove a multi-revision history after restart, not merely one successful render against current text.

### Existing comment UX acceptance

Complete PR #485's content-height panel behavior on desktop while preserving long-list scrolling and narrow-screen usability. Open unanswered questions show an agent-addressed command for the existing reply mechanism; remarks may invite an explanation. Hints disappear only when the relevant agent reply exists. Resolve and Delete use consistent muted checkmark/cross icons and remain understandable controls.

Capture durable browser evidence at the accepted revision. The old `/tmp/specweb-version-demo/...` links and checked 'not implemented' wording are contradictory historical claims, not completion evidence. Do not close #484 on a cosmetic PR or on merging this specification.

## Closure contract

Each task separates source implemented, merged, tested, integrated and live accepted. Preserve narrower completed work and its merge evidence. Unknown or external-quota-blocked verification remains open with a reason. Cross-repository contracts require producer and consumer tests; documentation alone does not satisfy runtime or UI outcomes.
