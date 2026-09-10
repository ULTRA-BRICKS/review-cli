# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## 0.37.8 — 2026-09-10

- **feat(specweb): version history panel (#484)**

## 0.37.6 — 2026-09-10

- **Feat(dashboard): interactive Stats timeline + per-review-dimension breakdown
  (review-cli#343).** Replaces the old count-sorted per-day list with a horizontal
  time-axis chart (day/week/month toggle, click-through drill-in) and surfaces
  `compute_stats()`'s `by_dimension` breakdown (ok/error/ok_rate per board lens).

## 0.37.5 — 2026-09-10

- **New `review entities` bare subcommand — read-only harness/provider/model/
  account/effort-lens overview (review-cli#487, Phase 0).** Prints a
  structural snapshot of review-cli's own configured entities: which
  harnesses (codex/claude/opencode/omp/commandcode/z.ai/gemini/openrouter)
  the resolved board actually routes seats through (via the same
  `backends.resolve_backend` router used at dispatch time, not a re-derived
  guess), each seat's provider/role/effort/availability, the registered
  Claude multi-account roster (`reviewlib.claude_accounts`), the
  `ROLE_FALLBACK_MODELS` effort/role fallback chains, and a compact
  per-harness call-health summary for the last 7 days reusing `review
  stat`'s own report builder. `--json` emits the machine-readable form.
  Entirely read-only and offline — no new external call, no key printed —
  and reuses only data review-cli already resolves for a normal run. This
  is the Phase 0 (static list view) slice of the fuller interactive
  entity/relationship browser (drill-down, live refresh) tracked in
  review-cli#487; later phases add the TUI itself.

## 0.37.2 — 2026-09-09

- **Feat: omp-hosted Claude board seat + account-level fallback across
  registered Claude accounts (review-cli#168).** New `OMP_CLAUDE_SEAT`
  (`omp:anthropic/claude-sonnet-5`, display "Sonnet-omp") joins
  `DEFAULT_BOARD` at priority 7.5 with the `quality` role, directly after
  the direct-CLI Sonnet seat — the SAME claude-sonnet-5 model dispatched
  through the omp harness, a genuinely different auth path (omp's own
  credentials/profile-scoped agent dir, independent of the Claude Code
  CLI's `CLAUDE_CONFIG_DIR`/keychain), so it survives an outage that takes
  out every `claude:*` seat sharing one account. Raw board 14 → 15 seats
  (default/light 12 → 13, heavy 13 → 14); `ROLE_FALLBACK_MODELS["quality"]`
  gains `claude-sonnet-5-omp` as its third fallback.
- **Account rotation (review-cli#168/#170).** The trigger: a
  Claude-account-wide outage or paywall — or a 100% harness 5-hour/7-day
  window — hitting every `claude:*` seat that shares one account. The new
  `reviewlib/claude_accounts` module discovers the registry `claude-rotate`
  and harness-cli already use (`~/.claude-accounts/account-*/`, the same
  `$CLAUDE_ROTATE_ROOT` override name claude-rotate honors, the `current`
  state file) and yields the wrap-around rotation order starting at the
  current account. `review_claude`'s CLI transport rotates
  `CLAUDE_CONFIG_DIR` per dispatch (a fresh per-call env dict, never a
  global `os.environ` mutation — seats dispatch concurrently); the
  omp-hosted Claude seat rotates `--profile` across the comma-separated
  `$REVIEW_OMP_CLAUDE_PROFILES`. An account whose own harness window is
  exhausted is skipped pre-dispatch; a chronically unavailable result falls
  through to the next account; only when EVERY configured account is
  exhausted does the run return the synthetic usage-limit skip, now naming
  every account it tried. A host with no registry gets exactly one implicit
  candidate — byte-identical to the pre-#168 behaviour.
- **Usage-limit `account=` scoping axis (review-cli#170).**
  `usage_limits`' `usage_percent_for_model` / `allow_spawn_for_model` /
  `spawn_refuse_reason` accept a keyword-only `account` (an email,
  normalized case/whitespace-insensitively): only harness cards for THAT
  account gate the result — the exact fix for one registered account's 100%
  card blocking seats actually running as another. `account=None` keeps the
  old aggregate-across-every-account behaviour for every existing caller.
- **Fix: codex seats run from a non-repo `-C` dir (review-cli#168).**
  `codex exec -s read-only -C <dir>` refuses outright ("Not inside a
  trusted directory and --skip-git-repo-check was not specified") when the
  target isn't a git work tree (omp-cage sandbox dirs, plain scratch dirs).
  `review_codex` now probes `git rev-parse --is-inside-work-tree` and
  appends `--skip-git-repo-check` only for a non-repo cwd, leaving codex's
  own trust decision untouched inside real repos.

## 0.37.5 — 2026-09-10

- **Fix: role credit for `review` mode is now seat-granular per run
  (review-cli#137).** A new `usable_roles` field on `record_run` (read from
  `panel.FailoverOutcome.usable_roles` via a new `usable_roles_after` callable
  threaded through `cli._run_mode_with_stats`) records the board role of every
  seat that INDIVIDUALLY produced a usable verdict this run, independent of
  the run's own `passed`. `quorum_check`'s `--min-roles` gate now credits from
  a new `_seat_credited_roles` helper reading `usable_roles` (falling back to
  the legacy passed-only `roles` reading for pre-fix records), so a hard-failed
  sibling seat (e.g. a quota-exhausted provider returning nonzero) no longer
  zeroes out role credit for a genuinely-successful seat in the same run —
  concretely, an exact `-m modelA,modelB,...` board with Opus succeeding while
  Codex/GLM/Grok/Sonnet were all exhausted can now credit Opus's role toward
  `--min-roles` even though the run as a whole still fails. `--min-iter` /
  `--min-models` / `passed` are unchanged — still the run's own all-or-nothing
  verdict. This does NOT resolve review-cli#137's own two open acceptance
  criteria (whether non-review modes like brainstorm/quorum/just-ask should
  count toward the gate at all, and review mode's free-text content-verdict
  parsing gap) — both remain open.

## 0.37.1 — 2026-09-08

- **New default board seat: MiMo 2.5 via omp OpenCode Go (`MIMO_SEAT`,
  review-cli#450).** Canonical id `omp:opencode-go/mimo-v2.5` (live
  `omp models find mimo-v2.5` selector). Aliases `mimo` / `mimo25` /
  `mimo-2.5`. Role `tests` (DeepSeek is the only other tests seat and is
  commandcode, often unpaid — same rationale as GROK filling `security`).
  Inserted after Grok (priority 12), before z.ai GLM; Fable stays last
  (raw board 13 → 14; light/default 11 → 12; heavy 12 → 13). Added to
  `DEFAULT_MODELS`. The opencode CLI catalog spelling
  `oc:opencode/mimo-v2.5-free` is documented as not DEFAULT-safe
  (`default_routes_live` False: provider `opencode` under `oc:` is not a
  named backend token and is not in `_AGENTIC_ONLY_PROVIDERS`); explicit
  `-m oc:opencode/mimo-v2.5-free` still works at runtime. Not added to
  `VISUAL_MODELS` or `_AGENTIC_ONLY_PROVIDERS`.

## 0.37.0 — 2026-09-08

- **Feat: refuse seat spawn when a family's live harness 5-hour or 7-day
  window is exhausted (review-cli#437).** Pool composition and Claude
  dispatch now prefer `harness limits --all --json` for Claude Code / Codex
  usage, falling back to tg-ctl samples for families harness did not report.
  5-hour / 7-day windows are identified by `window_seconds` (18000 / 604800),
  not English labels. When the more-restrictive of those windows is at 100%,
  `allow_spawn_for_model` is false and `review_claude` returns a synthetic
  sentinel without Popen; `select_pool_with_reuse` no longer picks a 100%
  family as the least-depleted fallback (the pool shrinks instead). Missing
  harness / unusable JSON / a non-ok card still fails open. Does not add a
  fake max-7 concurrent-agent cap; `$REVIEW_MAX_CONCURRENCY` stays a memory
  semaphore and `DEFAULT_LIMIT_THRESHOLD` stays 70.

## 0.36.9 — 2026-09-08

- **Board roles name fallback models; `--show-board` lists them (review-cli#445).**
  Each `REVIEW_ROLES` key now publishes a `ROLE_FALLBACK_MODELS` tuple of bare
  model ids (no harness prefix). `review --show-board` still prints harness-prefixed
  seat lines and adds a Fallbacks-per-role footer of those bare names. A config
  `model: gpt-6-astra` (or a bare id through `_expand_alias`) auto-resolves to its
  harness seat (`codex:gpt-6-astra`); already-prefixed seats and existing aliases
  (`opus`, `glm`, `gemini`, `commandcode`, `codex`) are unchanged. Does not walk
  these lists at dispatch and does not reorder `DEFAULT_BOARD`.

## 0.36.8 — 2026-09-08

- **Fix: `review task CODE --check` passes when one padded iteration covers the
  role floor (C12 / review-cli#433 follow-up).** A 1-model pad records one
  iteration with 3 board roles. `quorum_check` still ANDed default `min_iter=3`,
  so `passed` was false and agents fired `ship-review-quorum` hatch even though
  role coverage (the ship predicate, agent-tools#644) was met. When `min_roles`
  is set (bare `--check` default or explicit), role coverage waives `min_iter`.
  Model-only mode still requires `min_iter`. Explicit `--min-models` still ANDs.
  A zero-iteration record still fails.

## 0.36.7 — 2026-09-08

- **Fix: recorded `review diff --task CODE --pool 0` still pads a same-role config
  roster to the ship floor (review-cli#433).** `--pool 0` ("all seats") sets the
  effective pool size to 0. Role-repair (`ensure_min_distinct_roles`) was gated on
  `effective_pool_size > 1` and is a no-op for `min_roles <= 0`, so a 4-seat config
  roster whose every seat shared one role skipped both the short-roster pad and the
  seat-count `--pool 0` warning, then dispatched 1 distinct role — failing
  `review task CODE --check` / gh ship with no diagnostic. All-seats recorded
  reviews now repair to `SHIP_MIN_ROLES` distinct roles (same pattern as
  `--show-board`). A sibling warning fires when seat COUNT already meets the floor
  but the config roster's distinct known roles do not. Does not pad `--pool 0 -m
  one-model` (stays exact 1-seat) or `--pool 1`, and does not lower
  `SHIP_MIN_ROLES` / `_DEFAULT_QUORUM_FLOOR`.
- **Fix: recorded just-ask / quorum / brainstorm `-m one-model --task` pads that
  model across the ship floor (review-cli#433).** `task_role_pad` was gated on
  `mode.name == "review"`, so a panel run with one live engine stayed a single
  seat and could not satisfy `review task CODE --check` / gh ship. One available
  model now executes every required role via separate role-specific invocations
  (never shrinking the role list to engine count, never copying one response into
  several slots). Failed slots are retried once; the run reports
  `Roles: N/M complete`. Does not lower `SHIP_MIN_ROLES`.
- **Fix: padded quorum/brainstorm `--task` seats prompt the actual REVIEW_ROLES
  lens and brainstorm records only completed roles (review-cli#433).** One-model
  padding passed `roles` to `run_role_slots` for retry/reporting while expert
  and brainstorm prompts still used the unrelated PERSONAS rotation, so
  architect/correctness/security were recorded without ever being asked.
  Brainstorm's `roles_after` also persisted the planned list, so a 2/3-alive
  round that passed synthesis could satisfy the ship gate for a failed slot.
  Each padded role's lens is now in that seat's prompt; brainstorm persists
  `Roles: N/M complete`, not the plan.

## 0.36.6 — 2026-09-08

- **Fix: a recorded `review diff --task CODE` now covers the ship min_roles floor even
  under `--preset light` and a single `-m` model (review-cli#309).** `review task CODE
  --check` / gh ship default to 3 distinct board roles. Light is pool 2 and the board is
  priority-ordered, so re-running `review diff --task CODE --preset light` never
  accumulated a 3rd role; `-m grok` dispatched an exact 1-seat board and skipped
  role-padding. Recorded reviews now take `max(resolved pool, 3)` unless `--pool N` is
  explicit (N<3 still wins but warns that the run cannot satisfy default `--check` /
  gh ship). Role-padding is on (`usage_percent` passed into
  `select_pool_and_reserve_with_reuse`). `-m one-model` with `--task` clones that model
  onto unused board roles and sets `reuse_eligible` so the pool guard does not demand 2
  engines. A short config `models:`/`board:` roster is padded the same way. Without
  `--task`, light stays pool 2 and `-m` stays exact. Does not lower
  `_DEFAULT_QUORUM_FLOOR`, change `DEFAULT_PRESET`, or pad to the full 11-seat board.
  **Review-finding follow-ups (same PR):** (1) `--show-board`'s display-only pad gate
  now checks `any(r.role_lens for r in board)` for a config/default board too, matching
  the real dispatch gate exactly — a role-less config roster no longer shows a padded
  board for a run that would actually dispatch unpadded. (2) A short config
  `models:`/`board:` roster (fewer entries than the ship floor) now pads to the
  ship floor (`SHIP_MIN_ROLES`, 3) instead of over-padding to the unrelated
  `DEFAULT_POOL_SIZE` fallback (4) — this doc's own "pad to 3" claim above is now
  actually what happens for that shape; the existing test tightened from `>= 3` to
  the exact count. (3) A new end-to-end test drives a real `review diff --task CODE
  -m <model>` dispatch through `review task CODE --check` (not just `quorum_check()`
  in isolation), proving the role pad actually clears the same gate `gh ship` runs.
  **Round-2 review-finding follow-ups (same PR):** (4) The short-config-roster pad
  gate's `config_roster_len < SHIP_MIN_ROLES` comparison was itself off by one: an
  EXACTLY-3-entry `models:`/`board:` roster (already at the ship floor) still fell
  through to the unrelated `DEFAULT_POOL_SIZE` (4) fallback, silently cloning an
  unnecessary 4th seat — one extra real paid model dispatch on every recorded review
  using a 3-model roster. Fixed to `<= SHIP_MIN_ROLES` so a roster that already meets
  the floor stays at its own size. (5) `--pool 0` ("all seats") with an explicit `-m`
  list under the ship floor (a single model, or 2+ models) collapsed to just those
  model(s)/seat(s) — genuinely below the floor — without the warning an equivalent
  explicit low `--pool N` already gives; both now warn identically. (6) An explicit
  config `pool:` default could be silently overridden back down to `SHIP_MIN_ROLES`
  by the short-roster and single-model pads above (a pre-commit `review diff
  --staged` finding on this same PR) even when the config value already satisfied
  the ship floor on its own; both pads now defer to an explicit config `pool:` the
  same way they already deferred to an explicit CLI `--pool`. (7) Added a regression
  test pinning that the two independently re-derived task-code normalizations
  (`early_task` and `task_code`) always agree, and a test asserting `--pool N >=
  SHIP_MIN_ROLES` still wins over the single-model pad. (8) Added a test asserting
  `SHIP_MIN_ROLES` stays equal to `reviewlib.stats._DEFAULT_QUORUM_FLOOR`, enforcing
  the pre-existing "SYNC: keep equal to" comment. (9) Renamed a misleadingly-named
  test (`..._multi_model_stays_exact_with_short_config_roster`, whose config
  fixture had 4 entries — not short) to describe what it actually asserts.
  (10) A second `review diff --staged` pass on the round-2 diff itself confirmed
  the config-`pool:`-versus-pad interaction is safe for SUB-floor config values too
  (e.g. `pool: 1` + a single `-m` model still floors to 3 via
  `recorded_task_pool_size`, which runs before either pad guard) and added
  regression tests pinning that; renamed `explicit_models_all_seats` to
  `pool_zero_below_floor_models` (the prior name backwards-described what it
  checks); and collapsed the two near-duplicate `--pool` warning `print` blocks
  into one. (11) A third `review diff --staged` pass found the mirror-image gap
  to (5): `--pool 0` on a short config `models:`/`board:` roster with NO `-m`
  (e.g. a 2-model config) also collapsed below the floor with no warning — the
  warning now checks whichever of the explicit `-m` count or the config roster
  count actually bounds the "all seats" collapse. Also extracted the
  `args.pool is None and not explicit_preset and _config_default_pool(config)
  is None` guard (duplicated verbatim at both pad sites — the same
  duplication-drift pattern that caused the original bug) into one
  `_no_deliberate_pool_choice()` helper. (Flagged but out of scope for this PR:
  an explicit multi-model `-m` list — regardless of `--pool` value, pre-dating
  this PR entirely — takes the flat non-board dispatch path and records no role
  data, so `review task CODE --check`'s role-based floor is not actually
  satisfied by model count alone for that path; tracked as a separate
  follow-up.)

## 0.36.5 — 2026-09-08

- **Fix: opencode/omp backend seats no longer inherit review-cli's own stdin, which
  could hang the seat forever (review-cli#428).** `_run_streamed`'s Popen call passed
  `stdin=None` (inherit) whenever `input_text` was `None` — the case for `review_opencode`
  and `review_omp`, both argv-only backends that never pass `input_text`. review-cli
  routinely runs as a subprocess of an orchestrating agent whose own stdin is an open,
  non-EOF pipe/tty; if the opencode/omp CLI reads stdin at all (a common non-TTY CLI
  pattern), the inherited fd let it block indefinitely at 0% CPU with zero output.
  Observed live 2026-09-07: an `opencode run` subprocess hung in a stdin-read syscall and
  had to be killed and relaunched manually with stdin from `/dev/null`. This is very
  likely the actual root cause behind some of the "opencode/oc:zai stalls with zero
  output" cases previously band-aided only by the true-silence stall watchdog + cooldown,
  which detects the symptom and cools the seat down as though the provider's quota were
  exhausted. `_run_streamed` now always passes `stdin=subprocess.DEVNULL` for the
  `input_text is None` path — never a bare/inherited `None` — while the codex/claude path
  (`input_text` provided) keeps `stdin=subprocess.PIPE` unchanged.

## 0.36.5 — 2026-09-08

- **Fix: `review task CODE --check`'s `#389` near-miss key diagnostic now
  prints in plain-text mode too, not just `--json`.** `quorum_check`'s
  `searched_key`/`near_miss_keys` fields (added alongside the `105`/`#105`
  read-time aliasing) previously reached only the `--json` payload — a human
  running a bare `review task CODE --check` never saw the note, even though
  the mismatch is supposed to be diagnosable from the refusal alone. Both
  modes now print `[review task] note: searched key '#105' — found '105' in
  the store (same digits, different '#')…` on a failed/empty check whenever
  the other spelling exists in the store.
- **Fix (review-cli#389 follow-up, quorum-review finding): the near-miss note above
  no longer claims a twin's iteration "already counts above" when it never actually
  did.** The note's wording used to branch on `excluded_mismatched_iterations` (a
  count of diff-identity mismatches) alone — a twin whose only record has
  `passed=False` never reaches that count at all, so the "already count above" claim
  printed regardless and was simply false for that shape (Grok review flipped a prior
  CLEAN verdict to NOT CLEAN with a concrete `#105`/`105` repro). `quorum_check` now
  exposes the real signal — `near_miss_counted`/`near_miss_ever_passed`, checked
  against the actual gated population — and the CLI picks accurate wording for each of
  the three cases (counted, passed-but-mismatched, never passed). Also: the
⚠ 1 unresolved conflict detected
- ours = HEAD
- theirs = bade2cc (chore: bump version to 0.37.7)
- base = parent of bade2cc (chore: bump version to 0.37.7)
NOTICE: Inspect a block by reading `conflict://<N>` (add `/ours` / `/theirs` / `/base` to render a single side). Resolve with `write({ path: "conflict://<N>", content })`, or bulk-resolve every registered conflict with `write({ path: "conflict://*", content })`. Writes replace ONLY the marker block (markers + all sides) — never repeat the lines before/after it; they stay in place.
`content` shorthand: a line that is exactly `@ours` / `@theirs` / `@base` / `@both` expands to that recorded section. `@both` is ours-then-theirs with no separator — only for additive conflicts where each side adds something different; NEVER for competing edits of the same lines (pick a side or write the combined text). Lines that are not a token pass through verbatim, so `"// keep both\n@ours\n@theirs"` literally writes the comment, then ours, then theirs.
Per-id bulk: `write({ path: "conflict://*", content: "1: @ours\n2: @theirs\n…" })` resolves each listed id with that side in ONE call — the cheapest way through many pick-one conflicts; unlisted ids stay registered.
Resolve each block faithfully: keep one side (`@ours`/`@theirs`), or combine them when both intents apply — never invent content beyond the recorded sides, and never stack both sides of competing edits. Resolve several conflicts in a single turn by issuing multiple `write` calls at once; ids stay valid as earlier blocks are resolved.

──── #2  L6-40 ────
<<< ours
## 0.37.6 — 2026-09-10

- **Feat(dashboard): interactive Stats timeline + per-review-dimension breakdown
  (review-cli#343).** Replaces the old count-sorted per-day list with a horizontal
  time-axis chart (day/week/month toggle, click-through drill-in) and surfaces
  `compute_stats()`'s `by_dimension` breakdown (ok/error/ok_rate per board lens).
… (20 more lines)
=== base
(empty)
>>> theirs
## 0.37.7 — 2026-09-10

- **feat(specweb): version history panel (#484)**



[Showing lines 1-300 of 1761. Use :301 to continue]