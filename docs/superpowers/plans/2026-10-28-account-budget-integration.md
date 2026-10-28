# Account budget integration and backend decomposition

**Goal:** Deliver issue696's same-account 80% ceiling on direct Claude text/images, OMP and promoted review seats, on the capability-level CLI from PR700.
**Architecture:** Retain the already-reviewed budget data model and its explicit configuration. Port configuration calls into the split CLI. Divide the oversized backend implementation by transport/responsibility using ordinary functions and explicit forwarding. Preserve the public module, signatures, monkeypatch boundaries, lazy imports and transport/account/cooldown semantics.
**Spec:** Ultra's renewed delivery request and issue696 acceptance criteria; original staged implementation is preserved unchanged in the 696-account-cap worktree and a hashed patch.

## Constraints
- No provider/model bans, threshold relaxation, lint exclusions, review bypass or runtime hotpatch.
- Three role reviews; all shared account windows must be fresh and below the configured ceiling. Unknown usage fails closed under strict mode; no synthetic provider-exhaustion claims.
- Preserve the existing default-disabled library context, CLI activation, explicit owner overrides, paid-provider restrictions and distinct account identities.
- Keep decorated cache/context-manager functions and initialization ordering compatible. Do not implement runtime exec, global injection or synthesized function objects.
- Ordinary source modules below the configured file limits; cognitive/cyclomatic limits15/20 remain enforced.
- One heavy suite on the existing server, bounded temporary directories and no denied cleanup workarounds.

## Tasks and evidence
1. Resolve the two integration conflicts preserving PR700's smoke registry. Port runtime configuration and config help to split modules. Run the existing budget tests individually and retain baseline results.
2. Add compatibility regressions for public backend signatures, introspection, monkeypatched dependency calls and cache/context-manager behavior. Run before decomposition to establish the contract.
3. Move cohesive backend functions into dispatch, opencode, OMP, REST, Claude, cooldown and provider-policy modules. Explicit wrappers pass the public dependency surface; evaluate defaults and cached decorators exactly once. Re-run the contract and existing backend tests.
4. Extract small decision helpers only where the enforced complexity gate fails. Test each before/after behavior; preserve error precedence, no-spawn holds and source freshness.
5. Run pinned Ruff, normal rig lint, complete server smoke, normal review/commit/PR CI and task acceptance. Ship with gh ship only after all required checks. Install, configure and demonstrate effective budgets/levels using non-spawning fixtures and actual read-only usage evidence.

## Review focus
Budget-policy holds must not poison cooldown; images must use the same account whose usage was checked; stale/missing/incomplete/foreign windows cannot complete a shared pair; explicit overrides remain visible; facade mocks must reach real dispatch code without silently changing provider selection.
