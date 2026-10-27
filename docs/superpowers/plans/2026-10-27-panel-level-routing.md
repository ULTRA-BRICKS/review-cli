# Panel capability-level routing — #699 / #697

Owner: primary ChatGPT, isolated branch panel-level-routing-20261027 from a93a442.
Scope: close the observed discrepancy between text-panel --list-defaults and dispatch. Existing PR700 and account-cap696 have separate owners; do not edit their worktrees.

## Binding requirements
Choose capability levels, not provider bans. Load model/provider/effort/moderator data from version-controlled levels.yaml plus machine overrides. Explicit -m wins; explicit --level wins over legacy rosters; absent explicit level preserves a configured models/brainstorm_models roster. With no legacy roster use the configured default level. Selected-level candidate pools and effort must reach quorum, brainstorm and just-ask; unavailable seats never trigger an implicit heavier level. Preserve quota, unpaid provider, distinct-role, admission and normal review/commit/ship gates. Never claim that focused tests replace full CI.

## Approach and plan
Use a shared pure panel-selection module; wire list/default help/health and actual CLI dispatch to it. No provider literals or changed authorization checks. Changing user configuration alone is rejected because dispatch ignores the level and can fall back to hard-coded defaults.
1. Add offline regressions exercising real CLI parsing/listing/dispatch with model transport replaced only at the mode boundary. Observe failures on the existing code.
2. Implement shared resolution and propagate selected seats, effort and canonical level; validate availability/pool behavior within the same level. Preserve explicit and legacy behavior.
3. Run focused files serially, exact CI-pinned lint and protected complexity gate. The existing CLI has 9,054 lines and 30 baseline lint findings; extraction must preserve behavior, never weaken thresholds or hide code from analysis.
4. Normal sufficient-tier review, commit, publication and CI only when their gates pass. Coordinate adoption with PR700 owner; no forced merge or live hotpatch.

## Environment and ledger
Disk constrained on Ubuntu and existing heavy server. No new dependencies, heavy suite or denied cleanup workaround. Full CI remains required.
Pre-flight: selection is shared by help, listing, health, actual mode context and statistics; tests must cover all consumers.
No implementation yet. Worktree clean at base a93a442 before this plan.

## Ruling: existing CLI must be split without weakening its gates
The unchanged CLI baseline is 9,054 lines and has 30 protected lint findings; the file-level gate blocks even a small routing edit. Split command responsibilities into ordinary Python modules and keep a typed compatibility facade. Implementation functions receive that facade explicitly so the existing public monkeypatch/dependency seams continue to work. No eval/exec, dynamically hidden source, lint exclusions or changed thresholds. The first generated candidate preserves all 159 original signatures and 157 extracted bodies under an AST equivalence check (only module-global references become explicit facade dependencies). It is not installed. Next split the large dispatch routine into ordered, named stages and simplify existing complex functions with focused regression checks.

Tests already observed: original panel routing had seven failing and two passing cases; the corrected selection/dispatch now passes 15 offline cases covering all three modes. Moderator-only legacy tests were observed queuing in real admission (captured stack); their helper now replaces only the statistics/admission transport, restoring it afterwards, so tests do not block or mutate the owner's real review queue. Production admission is unchanged.
