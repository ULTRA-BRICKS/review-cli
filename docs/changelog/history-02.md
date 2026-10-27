# Historical release notes

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
  passed-but-mismatched case used to point a plain-text reader at `mismatch_details`,
  a `--json`-only field they have no way to see; it now says to re-run with `--json`
  instead. Also documents `searched_key`/`near_miss_keys`/`near_miss_counted`/
  `near_miss_ever_passed` in the `--check --json` schema (README) — undocumented
  since `near_miss_keys` first shipped — and adds `--check --json` regression tests
  covering all three near-miss shapes (never passed, gated-pass, passed-but-mismatched).

## 0.36.3 — 2026-09-08

- **Fix: `review task --check` treats `105` and `#105` as the same GitHub-issue
  task key (review-cli#389).** Iterations recorded under either spelling are
  visible to the other, so `gh ship` looking up `#105` from a `Closes #105`
  trailer no longer reports 0/3 when the reviews ran as `--task 105`. Write
  path keeps the spelling that was passed (old records are not rewritten).
  Non-numeric codes (`HYP-742`) are not aliased. A failed check's result dict
  includes `searched_key` and `near_miss_keys` (same digits, different `#`)
  when the other spelling exists in the store.

## 0.36.2 — 2026-09-08

- **`review task CODE --check` zero-role fallback — only for GENUINELY old
  history, never a permanent way to dodge the role-based gate (#252
  follow-up).** The zero-role-data fallback below (#252) didn't distinguish
  two different situations: (a) a task whose entire history genuinely predates
  role-tracking (PR #246's merge) — nothing more can be done, the fallback is
  correct; vs. (b) a task with at least one iteration recorded AFTER
  role-tracking became possible, that simply never happened to use a
  role-tracking `review diff` board (reviewed only via `quorum`/`just-ask`/
  `brainstorm`/`qa` instead). Silently granting case (a)'s free pass to case
  (b) too would let ANY task dodge the role-based gate forever, permanently,
  just by never choosing a role-tracking review mode — not the one-time
  migration accommodation for old history the fallback was meant to be. Now:
  the fallback only fires when EVERY recorded iteration provably predates
  role-tracking (compared against PR #246's merge commit/timestamp). A task
  with at least one post-cutoff, role-less iteration stays role-based (fails on
  its own merits, same as an ordinary short-of-floor denial) and gains a
  `role_tracking_gap` key (plus a text-mode `hint:` line) naming exactly what's
  missing and what to run instead (`review diff --task CODE` through a
  role-tracking board) — distinct from `quorum_mode_fallback`, which is now
  reserved for case (a) only. **Non-monotonic gate, by design:** a task
  currently passing via the model-counting fallback can flip to failing the
  moment one more role-less `quorum`/`just-ask`/`brainstorm`/`qa` review is
  recorded against it — doing additional review work can move the gate from
  pass to fail. That's the intended incentive (stop taking the role-blind
  shortcut once role-tracking is available), but it's the concrete form of the
  breaking change here, and the shape an automated `gh ship` pipeline could
  trip on if it re-checks a task after recording a fresh, role-less review.
  This is any-VERDICT, same as case (a)'s population — a role-less quorum/
  just-ask attempt that FAILED (produced no usable review at all) still trips
  it, not only a passing one, which is the sharper shape a retry loop is most
  likely to hit (record a failed attempt → re-check → now blocked for a new
  reason).

- **`review task CODE --check` bare default — fall back to model-counting when
  a task has zero recorded role data (follow-up to #246).** The role-based
  default #246 introduced (below) has a real gap: a task whose ENTIRE review
  history predates role-tracking, or comes only from `review quorum`/
  `review just-ask`/`review brainstorm`/`review qa` (modes that never record
  per-seat roles at all), has ZERO role-tagged iterations — and the bare-check
  default was treating that identically to "recorded some roles, just short of
  the floor," a guaranteed hard fail even when the task genuinely has 3+
  distinct MODELS in its history that would have satisfied the pre-#246
  model-counting default. Now: in the bare-check path only (neither
  `--min-models` nor `--min-roles` given), once real history is loaded and
  found to carry NO role data whatsoever — checked across every recorded
  iteration REGARDLESS OF VERDICT (a role-tagged review that failed still
  counts as role data), excluding only iterations diff-identity-mismatched
  against a genuinely different repo/diff — the default silently swaps to the
  old model-counting check at the same numeric floor, and the JSON payload
  gains a `quorum_mode_fallback` key (plus a text-mode `note:` line) explaining
  why, PLUS a `min_models_source: "fallback" | "explicit"` key — so a caller can
  tell this apart from a genuine explicit `--min-models` request, which
  behaves exactly as before (`"min_models" in payload` alone is no longer
  sufficient to detect an explicit request; `min_models_source` is the direct,
  positive discriminator). A task whose ENTIRE history is diff-identity-
  mismatched against the current repo/diff (e.g. a task-code collision with an
  unrelated project) is treated the same as "no effective history for this
  check" and does NOT trigger the fallback either — it keeps the pre-existing
  role-based fail-closed shape, rather than a misleadingly-worded
  model-counting shape. A task with even ONE role-tagged iteration of any
  verdict, for the current repo/diff, is unaffected: it stays role-based and
  must pass or fail on that basis alone, never falling back just because the
  role floor isn't met.

## 0.36.1 — 2026-09-08

- **Fix: dashboard cache-invalidation scan no longer scales with total historical log
  corpus size (task-cli#151).** On a long-lived install (147k+ files / 18GB observed
  live), `_log_dir_fingerprint` (the `/api/runs`/`/api/stats` invalidation check, run
  under `_SessionCache`'s lock on every request) and `_snapshot_logs` (the SSE
  watcher's continuous 1Hz poll) both `os.scandir`+`.stat()`-ed the ENTIRE log
  directory on every call — measured ~3.9s cold / ~0.5-0.7s warm-cache, and almost
  certainly the real explanation for the dashboard "sustaining 77% CPU continuously"
  even with no requests in flight. Unifies this with the parser's own `_dir_signature`
  onto one shared `_scan_log_dir_entries`, which skips `.stat()` for any file whose
  filename-embedded stamp is older than a 6h safety-margined window — safe because
  `.log`/`.md` artifacts are write-once by their originating writer process, so a file
  that old can never move the signature again. A stat-free `total_count` separately
  catches create/delete of files outside the window. Directly measured against the
  real 147k-file directory after the fix: a steady ~0.35-0.4s per scan regardless of
  cache state. Follow-ups filed: task-cli#155 (`review dashboard status` false
  "stopped" report, root-caused to a legacy launchd unit outside this repo) and
  task-cli#156 (the log directory's total lack of retention/rotation).

## 0.36.0 — 2026-09-07

- **GROK_SEAT is Grok 4.6 via opencode's native xAI OAuth (`oc:xai/grok-4.6`).**
  Aliases `grok` / `grok46` / `grok-4.6` expand to it; `grok45` stays pinned to
  `oc:xai/grok-4.5`. The seat remains priority-11 on the diff board and is also
  on `DEFAULT_MODELS` (just-ask / quorum / brainstorm) and `VISUAL_MODELS`
  (after Opus). Vision matching uses the `grok-4.6` substring, not a generic
  `grok` (which would also match grok-3). `--moderator grok` alias-expands
  before `pick_moderators`; grok is not an auto-moderator candidate. Bare/colon
  `xai` forms remain unnamed routes. Unavailable hosts drop the seat at the
  probe. (review-cli#415)
- **just-ask drops unreachable built-in/config defaults** so a missing xAI OAuth
  (or any other down seat) cannot fail the all-usable exit while other defaults
  answered. Explicit `-m` is still honored as-is; if every default is down the
  unfiltered src still dispatches (same all-unreachable fallback as quorum).
  (review-cli#418)

## 0.35.12 — 2026-09-07

- **Fix: role-reuse padding now fires for a HARD-unavailable board (session-limit
  cooldown), not only for a model merely near its usage limit (task-cli#139).**
  `select_pool_with_reuse` (`reviewlib/config.py`) lets one live model cover multiple
  board roles when other role-owning seats can't run — built 2026-08-18 so a quorum gate
  stays satisfiable by role coverage, not raw model count. It worked for the case it was
  built for (some seats near their `usage_percent` threshold) but silently never fired for
  the far more common real-world case: seats fully unreachable (cooldown, missing
  key/CLI). The bug: its target pool size clamped to `len(reachable)` seats, which
  collapsed to 1 whenever only one seat was reachable, leaving nothing for the
  role-padding loop to pad. Fixed by sizing the reuse-aware target against the full board
  instead (`min(pool, len(board))`) whenever `usage_percent` is given and `pool` is a
  concrete positive request; `usage_percent=None` and `--pool 0` ("all seats") keep the
  exact prior behaviour. Three more gaps found by review while fixing this, all fixed in
  the same PR: (1) the CLI's own pre-dispatch pool guard (`reviewlib/pool_guard.py`) was
  bailing with exit 10 BEFORE dispatch ever reached the fixed selector, so the selector
  fix alone was unreachable from a real `review diff` run — added a `reuse_eligible` gate
  (only for a non-`-m`, role-lensed board, and only for a positive effective pool size,
  since `select_pool_with_reuse` never pads for `--pool 0`); (2) the planned-ETA pool
  computation used the non-reuse-aware `split_pool_reserve`, so the announced ETA pool
  size didn't match what actually dispatched — now uses
  `select_pool_and_reserve_with_reuse`; (3) the "board padded" stderr notice hardcoded
  "too few distinct models were under their usage limit" as the cause, which is now
  sometimes false (a hard-cooldown pad has nothing to do with a usage threshold) — reworded
  generically. Known follow-up (not fixed here, tracked separately): `--show-board`'s
  preview display still reports the pre-padding pool composition for this scenario, so it
  can under-report what a real `review diff` would actually dispatch.
## 0.35.11 — 2026-09-07

- **New default board seat: Grok 4.5 via opencode's native `xai` provider
  (`GROK_SEAT = "oc:xai/grok-4.5"`).** Added as priority-11 in `DEFAULT_BOARD`
  (`reviewlib/config.py`), on-by-default — it participates in every normal
  `review diff` run exactly like the existing seats, no opt-in flag required.
  (`review quorum`/`review brainstorm` use the separate flat `DEFAULT_MODELS`
  panel, not the board, so Grok does not participate there — only `review diff`
  is board-based.) Agentic (reads the repo read-only, like the other `oc:`
  seats); unlike Kimi/GLM/Qwen/DeepSeek there is no diff-only REST fallback for
  this provider, and unlike zai/commandcode it has no bare/colon-form default-
  model resolution either, so a bare `-m xai` or colon-form `-m xai:<model>`
  would hand opencode a broken invocation shape (opencode wants `provider/model`,
  not `provider:model`, and bare `xai` has no default model) — same as typing
  any other unrecognized provider string, since `resolve_backend`'s existing
  generic catch-all still sends an unmatched id to opencode; this change does
  not add new routing for these forms and does not close that pre-existing,
  general catch-all gap (out of scope here). Rather than giving
  `_match_named_backend` a bare-token branch that would make these broken
  forms look like a deliberately-supported route, `default_routes_live`'s
  under-transport check
  gained a documented `_AGENTIC_ONLY_PROVIDERS` exemption set
  (`reviewlib/backends.py`) so the #25 anti-rot guard still accepts `GROK_SEAT`
  as a safe DEFAULT without `_match_named_backend`/`default_routes_live`
  treating bare/colon `xai` as valid ids. Fixed a real bug found while wiring this up:
  `_oc_auth_has_provider` only recognized opencode's keyed-API auth.json shape
  (`{"type": "api", "key": ...}`) and silently reported an OAuth-authenticated
  provider (xai's `opencode providers login` flow, `{"type": "oauth", "access":
  ..., "refresh": ...}`) as having no credential — the new Grok seat would have
  reported unavailable on this exact host despite `opencode run -m xai/grok-4.5`
  working. Now discriminates by the entry's own `type`: `key` for `"api"` (deepseek/
  commandcode/zai), `access` OR `refresh` for `"oauth"` (opencode auto-refreshes an
  expired `access` from `refresh`, so a refresh-only entry still counts as usable), and
  a field-name-agnostic fallback (excluding the purely-metadata `type`/`expires` fields)
  for any unrecognized future auth type — so a genuinely new auth shape doesn't silently
  drop its seat either. All dict-assuming logic stays inside the fail-closed exception
  boundary, so a corrupted non-dict entry reports no credential instead of raising and
  aborting board selection. Placed directly ABOVE the deliberately-
  slow z.ai GLM last-resort seat (review-cli#65), not below it: the seat's own
  first dogfooding pass (`review diff` against this change) flagged that
  appending it after GLM meant deep mid-run failover always paid GLM's
  pathological-slowness cost before ever reaching Grok. New `grok`/`grok45`
  aliases in `MODEL_ALIASES`. Board/doc tests, README, and the embedded
  install-skill text updated for the 13-seat raw board (11-seat light/default presets,
  12-seat heavy preset — Grok joins every preset since only Fable/Sol are excluded). Filed review-cli#166
  (out of scope here) for a pre-existing, unrelated gap this review surfaced:
  `_oc_config_has_provider_key` accepts a whitespace-only or non-string inline
  `options.apiKey` as a valid credential for ANY provider, not just xai.
  - Review round 1 (Sonnet/Fable) follow-ups: the `oc:`/`opencode:` transport peel
    is one helper (`_peel_oc_transport`) shared by `effective_provider` and
    `default_routes_live`, and the `provider/model` selector well-formedness check
    (`_oc_selector_is_well_formed`: slash required, non-blank model, no colon
    contamination, no whitespace, no empty `/`-segment) now runs for EVERY `oc:` id before the provider
    check — a malformed `oc:zai/` or `oc:zai//glm-5.2` default on a NAMED provider
    no longer slips past the #25 guard; `_AGENTIC_ONLY_PROVIDERS` is a pure
    membership exemption. Grok's role is `security`, not `performance`: after
    review-cli#386 `performance` already had a live paid fallback (Terra) while
    `security` was held only by Qwen (unpaid/disabled) and the slow, quota-fragile
    GLM seat. `_oc_auth_has_provider`'s docstring trimmed to its invariants.

## 0.35.10 — 2026-09-07

- **`--detach` hardening from its first review round (review-cli#162 follow-up).**
  The spawning parent no longer reads stdin to EOF itself (a non-tty stdin held open
  with nothing in it — agent tool wrappers, pre-commit hooks — used to block the very
  call that promises to return at once); the caller's stdin fd is handed straight to
  the detached child, which reads it exactly as a synchronous run would. Leading
  global options are normalized BEFORE the detachable-mode check, and that check is a
  whitelist of review modes — `review -m x dashboard run --detach` can no longer sneak
  past the dashboard/spec-web rejection, and `review jobs/wait/install-skill --detach`
  is rejected instead of spawning a meaningless background job. A dead pid observed on
  a "running" record is now written back as `unknown-terminated` once, so a later pid
  reuse (or a reboot) can never flip the job back to "running" and make `review wait`
  block; a brand-new record with no pid yet stays "running" for a short spawn grace
  instead of being misreported as terminated by a concurrent `review jobs`. The
  finalizer's own job-status write is best-effort: a jobs dir that became unwritable
  after spawn no longer turns a finished review into a traceback exit, and one
  schema-malformed `<id>.json` (`{}`, `[]`, a bare string) no longer crashes
  `review jobs` — it is skipped like a corrupt file.

- **A sandboxed caller's unwritable log location no longer kills the whole seat
  (review-cli#162).** `log_dir()` (`~/Library/Logs/review-cli` on macOS,
  `$XDG_STATE_HOME/review-cli/logs` elsewhere) and the per-call log file it opens are
  both outside most agent-harness sandbox allow-lists, which commonly permit writes
  only under the system temp dir. Before this fix, a denied `mkdir` or file `open`
  raised an uncaught `PermissionError`/`OSError` straight out of `_run_streamed`
  *before the backend subprocess was even spawned* — silently killing that seat
  (observed live as a Fable/claude-p seat dying with "Operation not permitted"; every
  seat shares the identical code path, so this was never actually fable-specific —
  fable is simply the slowest seat, ~15 minutes, and so the most likely to still be
  running if a time-scoped sandbox grant is the trigger). `log_dir()` and the new
  `_open_log_with_fallback()` now catch the failure, print a loud stderr line naming
  the fix (disable the sandbox, or set `$REVIEW_LOG_DIR` to an allowed path), and
  retry under a writable temp-dir fallback instead of crashing the seat. Covered by
  `tests/test_log_dir_fallback.py`, including an end-to-end `_run_streamed` call
  against a deliberately unwritable (chmod 0) log directory that still runs the real
  backend subprocess and returns its result.

- **External SIGTERM/SIGINT now reap live backend children — no more orphaned reviewer
  subprocesses (review-cli#160, duplicate #159).** A `review` process killed or timed out
  from OUTSIDE (an agent's `kill <pid>`, a harness timeout, Ctrl-C) left its spawned
  backend children (`claude`/`codex`/`opencode` model-runners, and their own
  descendants) running — Python's default SIGTERM disposition terminates the process
  with no `finally`/`atexit`, and each backend child runs in its own session
  (`start_new_session=True`, so the internal per-call reap can bound its whole process
  tree) which also means an external signal to `review` never reaches it. Orphans were
  observed alive 3.5h+ after their run had already exited. `reviewlib.process.
  install_signal_reaper()` (wired at the top of `cli.main`) now reaps every registered
  live child (`kill_live_children()`, the same reap the internal 4h backstop already
  used) before re-delivering the same signal to the process, so it still dies with the
  correct exit status. Covered by `tests/test_signal_reaper.py`, which sends a REAL
  external SIGTERM/SIGINT to a child process holding a REAL spawned backend and asserts
  the backend is dead afterward.

- **New `--detach` / `review jobs` / `review status` / `review wait` — background
  review jobs (review-cli#160 companion).** A caller with its own short foreground cap
  (a capped subagent shell tool, a tight pre-commit budget) previously had no supported
  way to avoid blocking for a whole review other than wrapping it in an external
  timeout — explicitly the wrong move (`review` only emits panel/brainstorm synthesis
  at the very end). `--detach` spawns the identical review as a session-detached
  background process (`python -m reviewlib`, its own session) and returns almost
  immediately with a job-id; `review jobs` lists recorded jobs, `review status
  <job-id>` shows status/paths/a log tail, and `review wait <job-id>` blocks until
  done (for an unbounded caller, not a capped one). The detached run reuses the exact
  same code path as a synchronous run — backstop, the new signal reaper above, `-o`/
  quorum-stamp output — so its result is valid for `gh ship`'s review-quorum gate.
  Covered by `tests/test_detach_jobs.py` (a real `bin/review` subprocess with the
  deterministic fake backend): immediate return, running -> done transition, the
  detached result matching a synchronous run byte-for-byte, `wait` returning the job's
  exit code, and a killed detached job reconciling to `unknown-terminated` instead of
  reporting "running" forever.

## 0.35.9 — 2026-09-06

- **Opencode zai/glm stall watchdog + cooldown keyed by access method
  (review-cli#153/#159/#179).** Opencode's `oc:zai/glm-5.2` agentic seat hangs
  at ~0% CPU with zero output for the entire call when z.ai quota is
  exhausted — no error, no partial answer, just silence until the idle
  timeout eventually kills it after up to 20 minutes. A call from a
  watchdog-scoped model (default: any model matching `zai/glm`, overridable
  via `$REVIEW_OPENCODE_STALL_MODELS`) that produces zero output for
  `$REVIEW_OPENCODE_STALL_SECONDS` (default 5 minutes) is now retried up to 3
  total attempts; if every attempt stalls, the seat is cooled down so the next
  `review` invocation skips the doomed dispatch entirely instead of paying for
  another multi-minute stuck attempt.
  - Folds in #187: the `seat_cooldown` store is now keyed by `(model,
    access_method)` instead of `model` alone — `record_cooldown`/
    `active_cooldown`/`clear_cooldown` all take a REQUIRED `access_method`
    keyword, so a cooldown recorded from one transport (e.g. claude's CLI)
    never shadows a different, independently-healthy one (claude's API route,
    or opencode's separate route to the same underlying model/quota).
  - For a watchdog-scoped model the liveness bound is the SOLE owner of the
    "zero output since spawn" signal — `true_silence_timeout` is not forwarded
    for it (review round 1, Opus + Fable: the registry's true-silence value for
    zai/glm equals the 300s stall default, so "retry 3x then cool down" vs
    "cool down on the first silence" was decided by poll-loop check order).
    Unwatched opencode seats keep the true-silence wiring unchanged, and the
    liveness check now keys off the same `got_output` latch as true-silence.
  - A stall whose liveness bound was CLAMPED below the requested window by the
    board deadline / idle clamp (`stall_bound_clamped` on the `_run_streamed`
    result) is an honest bounded failure, not the quota-exhaustion signature:
    no retry, no cooldown (review round 2, Fable — under deadline pressure a
    merely slow-to-first-byte seat would otherwise be benched). `seat_cooldown`
    now owns the access-method enumeration (`ACCESS_METHODS`) that
    `quorum_check`'s stalled-model probe iterates.

## 0.35.8 — 2026-09-06

- **Real token counts now reach `run-stats.jsonl` (review-cli#195).** `record_run()` (the
  ETA/quorum-gate JSONL store) never recorded token usage even though the REST backends
  already parse it from every provider response. `ReviewResult` now carries
  `prompt_tokens`/`output_tokens` (set only at the REST construction sites), the panel's
  call tally sums them per run across EVERY dispatch attempt (an in-seat retry or a
  provider failover no longer undercounts), the flat `-m` review path tallies at all (it
  previously always recorded 0/0), and `record_run`/`task_summaries` persist and aggregate
  them. STATS_VERSION 4 → 5; older records without the keys read as "usage unknown", not
  zero. Two Codex review findings folded in: a 2xx with a valid `usage` but no assistant
  content still fails closed but keeps the prompt tokens it spent on the failed result;
  and a `usage` object with only one valid field collapses to 0/0 ("unknown") instead of
  persisting a half-real `(prompt, 0)` pair — the same validity rule `review stat`'s
  `tokenstats` applies to successful calls. The two stores still differ by design on
  FAILED-but-billed attempts: `run-stats.jsonl` counts the prompt tokens an empty-content
  failure spent (it aggregates every dispatch attempt), while `review stat` reads only
  successful calls' log footers and reports those as unknown. Gemini now also fails
  closed on a 2xx with no candidate text (it used to return rc=0 with only the token
  footer, which passed `result_is_usable()` as review content).

## 0.35.7 — 2026-09-06

- **Pre-commit gate: trivial-follow-up delta tolerance (review-cli#208).** The
  `review install-commit-hook` gate used to require the staged diff's sha256 to match
  the last reviewed one EXACTLY — any restage, even a one-line follow-up fix during an
  iterative review-fix cycle, forced a brand-new full multi-model review round (observed:
  9 rounds for one ticket). `review diff --staged` now also writes a `review-stamp-diff`
  companion holding the reviewed diff's raw text; on a hash miss the gate re-diffs the
  current staged diff against it and allows the commit — without dispatching a fresh
  review — when the line-level delta is within `$REVIEW_TRIVIAL_DELTA_LINES` (default
  10; `0` restores the old exact-hash-only behavior). Fails CLOSED (falls through to a
  full review requirement) on binary-file or submodule-gitlink content, which a line
  count can't measure, and excludes diff-generation metadata (`index` lines, shifted `@@`
  hunk headers) from the count so a genuine one-line edit in a file with existing hunks
  doesn't get miscounted as a large change. The baseline only ever advances via a real
  `review diff --staged` pass, so drift is always measured from the last genuine review.
  The count is `max(insertions, removals)` between the reviewed diff and the current one,
  not a raw `+`/`-` line total — a modified line shows up as a `-old`/`+new` pair in a
  unified diff, so a raw total would silently count every genuinely-edited line as 2,
  making `REVIEW_TRIVIAL_DELTA_LINES=N` roughly half as tolerant as documented. The
  metadata-exclusion patterns are anchored to git's actual header shapes, not bare
  prefixes — a removed source line that itself starts with `-- ` (SQL/Lua/Haskell
  comments) can't be misread as a `--- ` file header and silently undercounted — and a
  mode change or rename/copy line anywhere in EITHER diff — the reviewed baseline or the
  current staged one, whether or not that file also carries a real content hunk — fails
  CLOSED to a full review instead of reading as a free, unboundedly-sized trivial change
  (a rename/mode-change that also touches real content costs one full review it might not
  strictly have needed; that's the accepted conservative trade).

## 0.35.6 — 2026-09-06

- **The `quality`/`performance`/`security` review-board roles get a genuinely live
  fallback instead of depending on a single disabled/quota-fragile seat (review-cli#382).**
  `unpaid_providers: [commandcode, gemini]` disables every commandcode-routed seat
  (GLM-cc/Kimi/Qwen/DeepSeek) plus Gemini at once, machine-wide. That left `performance`/
  `quality`/`security`/`tests` with zero or exactly one live seat each, and `quality`'s one
  live seat (the z.ai-routed GLM) hit a real weekly quota exhaustion with nothing left to
  promote — hard-blocking a real PR's review-quorum gate with no path forward except a
  multi-day wait or a manual hatch bypass. Two new `DEFAULT_BOARD` seats — `TERRA_SEAT`
  (`codex:gpt-5.6-terra`) and `SONNET_SEAT` (`claude:claude-sonnet-5`) — are distinct,
  already-paid models on the same OpenAI/Codex and Anthropic accounts Sol/Astra and
  Opus/Fable already use (no new provider account), added as live fallbacks for
  `performance` and `quality`; the z.ai-routed GLM seat is re-lensed off its now-redundant
  `quality` role onto `security`, which had none. `tests` (DeepSeek-only), `contracts`
  (Gemini-only), and `architect` (Fable-only — see Fable's ~100% dispatch failure rate
  below, still true as of this investigation) remain thin: there was no third distinct
  already-paid model to give every starved role its own seat without doubling up one
  account's quota pressure on a single review run, the same failure class this change
  fixes for Fable. An earlier draft of this change also re-lensed Astra off its
  pre-existing (duplicate-of-Sol) `consistency` role onto `security`, but a second review
  round caught that this left `consistency` with zero live fallback anywhere on the
  board — the exact single-point-of-failure class this change exists to fix, just
  relocated — so Astra's role is left unchanged. This is a stopgap within the existing
  harness-prefixed seat-string mechanism, not the "roles declare models, harness/provider
  auto-resolve" redesign tracked separately as review-cli#364 (this repo's own copy) and
  rig-cli#337 (the umbrella tracking it across the harness ecosystem).

## 0.35.5 — 2026-09-05

- **`review diff` board mode: print a visible STDOUT notice when the pool comes up
  short of the requested size, naming every excluded seat + its unavailability
  reason.** Before this, a seat unavailable BEFORE dispatch (missing CLI binary,
  no auth, unpaid provider, …) that also had no healthy reserve to backfill it
  silently shrank the pool with zero signal anywhere a human or agent would
  actually see it — the existing `board: degraded` warning only covers a
  different, rarer case (reserve exhausted mid-dispatch) and only prints to
  stderr, which report-pasting workflows routinely drop. Root-caused 2026-08-28:
  a review round reported as "reviewed via codex + Fable" gave no indication
  that Opus/GLM were configured but missing. `_report_pool_shortfall` in
  `reviewlib/modes/review.py`.

## 0.35.4 — 2026-09-05

- **Stop the codex-backend self-reinvocation loop (review-cli#180).** A codex reviewer
  could re-invoke `review diff` on the same worktree as a plain shell command — codex's
  only safety mechanism, `-s read-only`, restricts filesystem/network access but not
  shell/exec, unlike the claude/opencode backends' explicit tool denial. This caused a
  real incident (2026-08-11): 40+ live `codex exec` processes, 11 `review diff` processes
  across 4 worktrees, swap at 88.5%, load average 60+.
  - Primary defense: a `$REVIEW_CLI_ACTIVE` reentrancy guard, set for the lifetime of a
    run and checked first in `main()`, refuses immediately on a recursive invocation. An
    env var survives `exec`/`setsid` regardless of how a backend child re-roots its
    process group, so it is the one signal that reaches every descendant.
  - Defense-in-depth: an idempotently-installed codex execpolicy `.rules` file forbids
    `review`/`codex`/`claude`/`opencode`/`omp` as shell commands.
  - The managed dashboard/spec-web `run`/`start` servers are themselves full `review`
    invocations and would otherwise trip their own reentrancy guard on launch; their
    `__serve` child argv and the installed-console-script probe now strip
    `$REVIEW_CLI_ACTIVE` before exec'ing, via a hardcoded `/usr/bin/env -u` prefix (never
    a PATH-resolved `env`, to avoid a poisoned-PATH escalation into a persisted autostart
    unit).

## 0.35.3 — 2026-09-05

- **`review diff` now says WHY a passing review did not satisfy the commit gate
  (review-cli#350).** The gate marker that agent-tools'
  `require-review-before-commit` hook stats is written by exactly one shape of
  run — a PASSING `review diff --staged` whose diff came from the real index.
  Every other shape (unstaged, a diff piped on stdin, a diff truncated for
  dispatch) used to skip writing it SILENTLY, so a caller saw a green review, a
  stale marker and a blocked commit with no explanation. Such a run now prints
  one stderr line naming the failed condition and the remediation for THAT
  condition (the truncated case is told to split the change, not to re-run
  `--staged`, which would truncate again), and warns off hand-creating the
  marker. The same now holds for the one run that DOES try to write the marker
  and loses it (an unwritable `$REVIEW_MARKER`): the write stays best-effort and
  never fails the review, but it is no longer swallowed in silence, because
  "green review, stale marker, no reason anywhere" is exactly the state that
  sends a caller reaching for the forged marker. The diff-shape half of the gate
  predicate now lives in exactly one place (`_commit_gate_skip_reason`), so a
  future condition added to it cannot skip the warning by drift, and its reasons
  are ordered so a truncated diff is told to be split rather than sent around a
  re-run loop that truncates again. For that ordering to fire at all, the
  dispatch-cap helper now reports the plain fact "the cap cut this diff" instead
  of a `--staged`-only version of it: an unstaged oversized run used to arrive as
  "not truncated", take the not-staged branch, and get sent around exactly that
  loop. Nothing about WHEN the marker is written changed — an unstaged run was
  refused before and is refused now, only the reason it gives is right. The
  same silence is gone from the two writes themselves: an unwritable session
  marker, a marker path that is not a regular file (pointing it at a directory
  used to "succeed" — `touch` just bumps the directory's mtime), and a
  `.git/review-stamp` that could not be written are each reported with the file
  and the fix that belongs to them. The marker and the stamp feed DIFFERENT
  gates — agent-tools' hook stats the marker, the local git pre-commit hook
  verifies the stamp — so losing either one blocks a commit on its own, and each
  gets its own line rather than a merged one that would send the caller to fix
  the wrong file. The `review` skill blurb and SKILL.md now
  name `review diff --staged --task CODE` as the pre-commit invocation.
  Every rerun command the notice (and the `--commit` refusals) print carries a
  `--task <CODE>` placeholder — a recorded review mode requires a task code, so a
  remediation without one failed at the first keystroke (Codex finding on #359).


## 0.35.3 — 2026-09-05

- **`DEFAULT_BOARD` (the `review diff` reviewer board): replace the redundant
  bare-`"codex"` #5 seat with GPT-6-Astra ("Astra"), pinned explicitly.** The
  bare seat tracked `~/.codex/config.toml`'s own default model, which today is
  `gpt-5.6-sol` — the SAME model the #1 Sol seat (`SOL_SEAT`) already pins, so
  the board's #5 slot was silently reviewing with Sol twice instead of adding
  real model coverage. `ASTRA_SEAT = "codex:gpt-6-astra"` (OpenAI's new
  flagship codex model) is now the #5 `DEFAULT_BOARD` seat, keeping the same
  `consistency` role/lens. Scoped to the board only — the separate flat
  `DEFAULT_MODELS` panel (used by `--quorum`/`--brainstorm`) still keeps its
  own bare `codex` entry unchanged, by design.

## 0.35.3 — 2026-09-05

- **Dashboard: stop tabs from lying about an empty state during a background reload
  (review-cli#362).** Every runs-reading panel falls back to `(state.runs || [])`, but
  `state.runs` is briefly `null` whenever an SSE activity event or a write calls
  `invalidate()`. Switching tabs during that window rendered a false "no errors / no
  sessions" panel — reproduced live on a busy install where the Errors tab claimed "No
  errors yet" while Overview/Metrics (rendered moments earlier) reported 42k+ recorded
  errors. `render()` now shows a loading placeholder instead, except for the three panels
  (Stats, Models & roles, Metrics) that read only `state.stats` and can safely keep
  showing their still-valid snapshot — unless `state.stats` itself hasn't loaded yet
  either (the initial-boot window). A failed load now surfaces the actual error (or a
  toast, if data was already showing) instead of an eternal, misleading "Loading…".
- **Dashboard: fix `fmtDur()` rendering a nonsense "19m 60s" (and, one branch over,
  "60.0s") instead of rolling over correctly** — seen live on the Metrics tab's Duration
  max card. Rounding the seconds remainder in isolation could land on 60; fixed by
  rounding to one decimal before branching and decomposing minutes/seconds from the
  original value.

## 0.35.2 — 2026-09-01

- **Dashboard: fix the review-cli#326 memory balloon — every streamed call
  body/stderr was retained uncapped forever, hitting 32.8GB RSS on the real
  ~132k-log install.** Body and stderr are now capped (head+tail, sized to
  bound worst-case memory regardless of Unicode/script mix). Classification
  (`completed`/`has_error`/`is_paywall`/`is_cf_blocked`/`is_bad_key`/
  `has_real_content`) is computed once from the FULL untruncated text at
  parse time and stored, so a marker or the empty-vs-real-verdict split
  surviving only in a truncated part of a huge log is still classified
  correctly. `top_oversized_calls`/`compute_harness_stats` still scanning
  the now-capped body for `SKILL.md`/`MEMORY.md`/diff markers is a known,
  lower-severity follow-up (review-cli#335).
