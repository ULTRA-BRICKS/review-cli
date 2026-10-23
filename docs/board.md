# Reviewer board — full mechanics

The main [README](../README.md#reviewer-board) covers what the reviewer board is, the preset
pool sizes, and the seat/role table. This doc covers the full failover, retry, cooldown,
account-rotation, and configuration-precedence mechanics behind it.

## Priority-ordered failover pool

The raw built-in board is a **priority-ordered** list of 15 models — strongest WORKING
model first — and a plain `review diff` runs the `light` preset (Alex, 2026-08-28): a
**pool of 2** without Fable/Sol, at medium effort — cheap by default for routine
pre-commit checks. Use `--preset default` for a routine change review at a pool of 4,
high effort, and `--preset heavy` for release/risky changes: Sol, Opus, GLM-cc, and Kimi at
`xhigh` effort, with the remaining board seats as `max`-effort reserve. Fable is excluded
from every preset — `review stat` telemetry showed a confirmed ~97.9-100% dispatch failure
rate (chronic session/usage-limit exhaustion on the account it runs through), so it is
demoted to the raw board's LAST seat instead (see the seat table in the README) — a
last-resort reserve, not a routinely-dispatched pool seat, in any preset. The pool is chosen
by **priority + availability**, with three layers so the run keeps the requested
number of working reviewers even when models drop:

- **Startup failover** — the active pool is the **top N AVAILABLE** seats by priority,
  **distinct roles first** (review-cli#588): a seat whose role an earlier pool seat already
  covers waits in the reserve while another available seat brings a new role. A
  higher-priority seat whose backend isn't reachable (no key / not on PATH) is
  **skipped** and the next seat is pulled up, so you still start with the requested
  number of working models. (E.g. if Sol is unavailable in the heavy preset, the pool
  starts at Opus.) If only repeat-role seats are left to fill a slot, the seat reviews
  under a board role the pool lacks (`board re-lensed` on stderr).
- **Mid-run failover** — if an active seat **fails during the review** (backend error,
  timeout, empty output, or an "unavailable" reply such as a paywalled model returning
  *"… is currently unavailable"*), its **role** is kept filled (review-cli#588): a
  reserve seat with the same role runs in its place, else a reserve seat whose own role
  is still uncovered, else the next reserve re-lensed onto the missing role, and once the
  reserve is empty a model that already returned a usable verdict in this run is
  dispatched again under the missing role (a real call with that role's lens). This
  repeats until the requested number of working verdicts is produced. If no healthy
  model is left, the run degrades, prints a `ROLE SHORTFALL` notice on stdout naming
  each missing role and why its seats failed, and exits non-zero.
- **Account rotation (review-cli#168)** — every `claude:*` seat and the omp-hosted
  Claude seat authenticate as ONE Claude account, so an account-wide outage or paywall
  (or a 100% harness 5-hour/7-day window) used to take out all of them at once. On a
  multi-account host (the per-account `CLAUDE_CONFIG_DIR` layout under `~/.claude-accounts/account-*/`;
  `$REVIEW_CLAUDE_ACCOUNTS_ROOT` overrides the root) the direct `claude:*` dispatch
  now tries each registered account in rotation order — the persisted `current` account
  first, then wrapping through the rest — via a per-call `CLAUDE_CONFIG_DIR`, skipping
  any account whose own harness window is exhausted and falling through to the next on a
  chronic-unavailable result or a failed login (logged out / OAuth expired,
  review-cli#588). The omp-hosted Claude seats (`omp:anthropic/*`) rotate through omp's
  own Anthropic accounts in `~/.omp/agent/agent.db`, one account per attempt, to the next
  on a 429 rate limit or a failed login, and name the account that answered
  (review-cli#637); `$REVIEW_OMP_CLAUDE_PROFILES` (comma-separated, already-logged-in omp
  profiles) replaces that with a profile rotation when set. Only
  when EVERY configured account is exhausted does the run emit the synthetic usage-limit
  skip, now naming every account it tried; a host with no registry behaves exactly as
  before.
- **omp account policy (review-cli#641)** — config.yaml `omp_accounts:` limits which of
  omp's Anthropic accounts the `omp:anthropic/*` seats take: `exclude:` emails are never
  used, and an account with an `omp usage` window at or above `max_usage_percent` (global,
  or per account under `accounts: {email: {max_usage_percent: N}}`) waits for that
  window's reset; `$REVIEW_OMP_ACCOUNTS_EXCLUDE` / `$REVIEW_OMP_MAX_USAGE_PERCENT`
  override. With every account excluded or over its threshold the seat fails naming each
  account and why; an invalid setting, a policy with `$REVIEW_OMP_CLAUDE_PROFILES`, or a
  policy with no account list to apply it to fails it too, and while an exclude list is
  set an account without a known email is skipped. `review seats` shows the seat
  UNUSABLE, the policy and each account's skip reason.
- **Claude CLI -> omp twin (review-cli#594)** — every `claude:claude-X` seat has
  `omp:anthropic/claude-X` (the same model through omp's own, separately logged-in
  Anthropic accounts) as its next executor, and the reverse. When the Claude CLI fails on
  every registered account (login expired, session or quota limit, an active cooldown),
  the SAME seat answers through omp under its own role, in `review diff`'s board and in
  just-ask / quorum / brainstorm. The result carries a `substituted:` line naming what
  failed, why, and who answered, and the ship quorum credits that role once, to the omp
  model. The Claude CLI stays first: omp is never cached as the seat's last-working
  provider, so the next run tries the Claude CLI again. Bare `review status` lists each
  seat as usable or UNUSABLE with the reason and its substitute, and each registered Claude
  CLI account as logged in or LOGGED OUT with the `CLAUDE_CONFIG_DIR=… claude auth login`
  command that fixes it.
- **A failed Claude CLI login is remembered per account (review-cli#596)** — `claude auth
  status` can say logged in while every call on the account fails ("OAuth session expired
  and could not be refreshed"). When a call fails its login, that account is recorded as
  LOGIN FAILED (the seat-cooldown store's escalating 10m/30m/2h/8h window) and is not
  tried again while another account answers. Once every account has failed its login the
  `claude:*` seats are unusable: a board or panel hands them straight to the omp twin
  without paying the failed call first, and bare `review status` shows the seat UNUSABLE
  with the failure, the accounts it hit and their `CLAUDE_CONFIG_DIR=… claude auth login`
  commands. The record is dropped by a later successful call on the account, or as soon as
  the account is logged back in (its stored login — the macOS Keychain item or
  `.credentials.json` — rewritten after the failure). `REVIEW_SEAT_COOLDOWN_SECONDS=0`
  ignores it, like every other cooldown.

Each seat carries its own configured role/lens: priority decides *who* sits in the pool,
the role decides *with what lens* they review. The run owes the pool's roles, so a
replacement keeps a missing lens covered rather than repeating one the pool already has.

`--pool N` overrides the preset default (the top-N available, with the same failover);
`--pool 0` runs **all available** seats in the selected preset/board. Use
`--preset heavy --pool 0` for all 14 heavy-preset built-in seats (the raw 15-seat board,
including last-resort Fable, needs an explicit `board:`/`models:` config). The board is **never
disabled** — `--pool` only sizes the pool.

**review-cli#382:** a `unpaid_providers: [commandcode, gemini]` config listing disables
every commandcode-routed seat (GLM-cc/Kimi/Qwen/DeepSeek) plus Gemini at once, machine-wide,
leaving several roles (`performance`/`quality`/`security`/`tests`/`contracts`) with zero or
one live seat — `quality`'s one live seat hit a real weekly quota exhaustion with nothing
left to promote, hard-blocking a real PR's review-quorum gate. Decision: Terra (Luna since review-cli#645) and Sonnet
were added as genuinely distinct, already-paid `performance`/`quality` fallbacks on accounts
the board already uses, and GLM was re-lensed off its now-redundant `quality` role onto
`security`. `contracts`/`architect` remain single-seat roles (`tests` later gained a second,
paid live seat in MiMo, review-cli#450); Astra's pre-existing `consistency` duplicate of
Sol's lens was deliberately left as is rather than reintroduce a different
single-point-of-failure. See
[review-cli#382](https://git.hyperide.ai/ultrabricks/review-cli/issues/382).

**Agentic by default.** Every board seat that *can* read the repo does. Fable/Opus run via
the agentic claude CLI **when `claude` is on PATH** (they fall back to the diff-only
Anthropic API only on a host that lacks the CLI but has an API key), Sol/Astra via the codex
CLI (Astra — GPT-6-Astra, pinned via `codex:gpt-6-astra` — is the #5 board seat as of
review-cli#286, was #6 before Fable's demotion moved every subsequent seat up one slot; it
replaced a bare `"codex"` seat that silently tracked the CLI's own default model),
and Kimi/Qwen/DeepSeek/Grok through opencode (`oc:provider/model`) and z.ai-GLM through omp (`omp:zai/glm-5.3`) — all run
read-only *inside* `-C` and can open any project file, not just the diff. Two seats are
always diff-only stateless HTTP calls: **Gemini** (no agentic transport) and the priority-3
**GLM-cc** seat (`commandcode:zai-org/GLM-5.3` — opencode's `commandcode` provider does not
register this GLM id, so the agentic form errors; the keyed-HTTP route is the one that
reaches it). Both are read-only by construction (they POST only the diff).
`review --show-board` shows each seat's live
`agentic`/`diff-only` scope for the current host. The board has
a reserve, so an `oc:` seat that opencode can't reach is backfilled rather than blocking:
a missing opencode **binary** is detected at startup (the seat probes unavailable and the
pool fills from the next reserve); missing opencode **provider auth** for known providers is
also a startup skip. If a provider key exists but the account is not entitled/billed, list the
provider in `unpaid_providers:` (or set `REVIEW_UNPAID_PROVIDERS`) and every direct or `oc:`
seat under it is skipped before any model process/API call. (The diff-only
`commandcode:`/`zai:` keyed-HTTP backends are still there for explicit `-m cc`/`-m glm` and
config boards on hosts without opencode.)

**In-seat retry before reserve-replace.** A seat that fails is first re-tried *on the same
model* when the failure looks **transient** — a `429` rate-limit, a `529`/5xx overload, a
provider timeout, an "overloaded"/"service unavailable" notice — with exponential backoff +
jitter, before any reserve is promoted. The same model usually answers on the next try a
moment later, so a brief throttle spike no longer burns a reserve seat. A **seat-fatal**
failure (auth / bad model / `501` not-implemented / a refusal) is **never** retried — no
retry can fix it — so it falls straight to the reserve. The retry budget is configurable via
`--retry N` (or `$REVIEW_RETRY_COUNT`; default 2, `0` disables it). Every retry and every
reserve promotion is recorded **durably** in the run-log dir (not just stderr), so a
post-mortem or the dashboard can reconstruct exactly how a seat recovered or fell over.

**Cross-invocation cooldown for a chronically-unavailable claude seat.** In-seat retry only
sees one process's worth of history, so a Fable seat that is out of session quota still
paid for one full `claude` dispatch on *every* review invocation before falling to the
reserve — real evidence: 4,322 of 6,383 recorded runs dispatched Fable and it failed, most
with an explicit session-limit notice. Once a dispatch comes back with that notice or the
administrative "... is currently unavailable" sentinel, `reviewlib.seat_cooldown` records a
cooldown (`$REVIEW_SEAT_COOLDOWN_SECONDS` pins a fixed window for FUTURE writes and
disables escalation going forward — it does not retroactively shorten an already-
escalated entry's `until`, only `<= 0` (the documented un-stick hatch, disabling
cooldowns entirely) does that; store path overridable via `$REVIEW_SEAT_COOLDOWN_FILE`)
so the *next* invocation within that window skips the real
dispatch and returns the same sentinel shape immediately — every downstream consumer
(failover, the dashboard's paywall classification) sees it exactly as it would a live
paywall response. **Escalating window (review-cli#221):** left unset, the window starts at
10 minutes and climbs per CONSECUTIVE failure (30min, 2h, then an 8-hour cap) — a seat that
keeps getting re-dispatched and re-failing across many separate `review` invocations
(minutes apart, so a flat 10-minute window kept lapsing before the next check) gets pushed
out of the active pool long enough for `run_board_with_failover`'s reserve-promotion to
substitute a different model instead. A genuine SUCCESSFUL dispatch clears the escalation
immediately; absent that, a 24-hour gap since the last failure also resets it to 10 minutes.
`review task CODE --check` (both text and `--json`) names any currently-stalled attempted
model plus why, instead of leaving an operator to guess from a bare pass/fail count.
Deliberately narrow: only these two chronic signals start a cooldown, never a bare
auth/bad-model seat-fatal (which can be a transient misconfiguration a human just fixed).
An explicit `$REVIEW_SEAT_COOLDOWN_SECONDS` override always resets `fail_count` to 1 on
its next write, the same as a genuine success — it's a human asking for a specific window,
which takes priority over the escalation history the way any explicit request does.

**Availability windows and fast health checks (review-cli#663).** The escalating timer above
guesses; most failures say exactly when the seat answers again. `reviewlib.availability`
reads that out of every failed seat call and records `unavailable_until` in the same store
(bucket `window`): a quota with a named reset (z.ai `429` code 1310 "will reset at
2026-10-26 05:58:34", zone-less = UTC; the Claude CLI's "resets 7:30pm (Europe/Moscow)";
an epoch `x-ratelimit-reset`; a `Retry-After`), credits exhausted (6 h, re-probed every
30 min), a suspended account (6 h, re-probed every 30 min), a rejected login (until the
credential file behind the seat changes, re-probed hourly) and a rate limit that names a
wait of two minutes or more. A quota that names its reset on a provider that bills one
account (z.ai, commandcode, gemini, xai, opencode-go, openrouter, moonshot, fireworks) is also
recorded as `provider:<name>`, so every seat on it is excluded; Anthropic and OpenAI stay per
seat because a Claude CLI login, omp's account pool and an API key are separate accounts.
A window of an hour or more is re-probed every quarter of its remainder (15 min to 6 h), so a
top-up or a mis-read time zone ends it early. An excluded seat is out of board selection
and out of admission planning — it holds no run slot and no provider reservation, which is
what used to serialize every review behind a dead provider (review-cli#660) — and
dispatching it returns a skip sentinel without a call. `review --show-board` lists it as
`SKIPPED (unavailable: <reason> (until <UTC time>))`, `review jobs` shows an excluded provider as
`EXCLUDED` instead of `THROTTLED`. No config edit is needed when a quota runs out or comes back.

`review health` probes every seat of the board in parallel with the cheapest call its
backend offers (the seat is asked to "Reply with exactly: OK" over no diff; 5 s timeout, 15 s for the CLI seats claude/codex/omp/opencode),
records what each failure says, and prints `ok` / `unavailable` (with `until`) / `unknown`
(no answer in time — a slow CLI is not a dead one). `--json`, `--seat`, `--preset`,
`--cached`, `--no-probe` and `--host H` (the same check on another machine over ssh) are
described by `review help health`. `review diff|quorum|brainstorm|just-ask` run the same
check before asking for a slot (results cached for `$REVIEW_HEALTH_TTL_SECONDS`, default
300 s, so back-to-back reviews do not probe again; `$REVIEW_HEALTH_PREFLIGHT=0` turns it off), so a dead seat never costs a review a slot.

**Diagnostic gap (known, tracked):** `stalled_models` is scoped to the models a task's
recorded runs actually attempted (`record_run`'s `models=`), which on the normal
board/failover dispatch path only ever contains the SEATS THAT SURVIVED — a chronically-
cooling seat that a reserve successfully backfilled never appears there, so the
diagnostic stays silent in exactly the case (a stuck seat masked by working failover)
it exists to surface. Tracked as
[review-cli#227](https://git.hyperide.ai/ultrabricks/review-cli/issues/227).

**Scope gap (known, tracked):** the FULL check/record/clear trio — including the
administrative-sentinel/quota-marker body detection that decides a call is
chronically-unavailable in the first place — is wired into only the two claude
dispatch paths (`review_with_images`, `review_claude` in `backends.py`).
`review_opencode` (review-cli#235) additionally consults + records + clears this same
store, but ONLY for its own narrower TRUE-SILENCE failure mode (see the
`REVIEW_TRUE_SILENCE_SECONDS` entry in `review help config`'s KEYS/AUTH section,
which also documents that the agentic Kimi/Qwen/DeepSeek/GLM/Grok board reviewers'
own auth is opencode's, not review-cli's, env vars):
a seat that never produces a single byte of output within `REVIEW_TRUE_SILENCE_SECONDS`
is reaped and cooled down the same way a chronically-unavailable claude seat is.
`review_opencode`'s success path now ALSO reuses the SAME shared chronic-unavailable/
quota-marker body detector `review_claude`'s clear-gate uses (`_chronic_unavailable_reason`
— not actually claude-specific in implementation, it just wasn't wired into any other
call site before this) — a rc=0 body matching one of those known marker phrases is
recorded as a chronic failure rather than wrongly clearing true-silence escalation
history. This narrows, but does not fully close, the gap: an opencode-specific quota
wording that matches NONE of the shared marker phrases is treated as a genuine success
and DOES clear the cooldown (any non-empty rc=0 body defaults to "recovered" unless it
matches a known chronic sentinel) — the same behavior `review_claude`'s identical
sibling pattern has always had for an unrecognised claude quota wording, not a new gap
this feature introduces. `review_codex` and the `commandcode:`/`zai:` HTTP backends
have no cooldown consult/record/clear at all. Extending the full trio (including
genuinely opencode-specific body detection, so an unrecognised wording is correctly
classified as chronic rather than defaulting to "success") to the non-claude backends
is tracked as [review-cli#226](https://git.hyperide.ai/ultrabricks/review-cli/issues/226),
not done here.

**Pool/spawn consults live `harness limits` 5-hour and 7-day windows.** Board composition and Claude dispatch skip a family whose more-restrictive 5-hour or 7-day window is already at 100% (`harness limits --all --json`, falling back to tg-ctl samples when harness has no card for that family). This is not a concurrent-agent cap; `$REVIEW_MAX_CONCURRENCY` remains the memory semaphore below.

**Memory-aware concurrency cap.** Each heavy seat (codex / claude / opencode) spawns a fat
model-runner subprocess, and a `review` runs its whole pool in parallel — so a high `--pool`
or a cascade of reserve backfills can fan out into enough concurrent agent subprocesses to
**OOM-kill** a seat mid-review under load. A process-wide cap bounds how many heavy backend
subprocesses run at once (default **4**, overridable via `$REVIEW_MAX_CONCURRENCY`; `<= 0`
disables it, and a value above **64** is clamped to that ceiling so a typo can't pin an
absurd number of children). A seat over the cap simply **waits** for a slot — it is never
dropped, and its per-call timeout starts only once it actually spawns, so queueing on the
cap can't falsely time it out. The common single-seat gate (`--pool 1`) and the default
(light) pool of 2 are unaffected (both `<=` the cap). This is a *per-process* cap; ACROSS
processes, host-wide admission (next paragraph) bounds how many reviews and seats run.

**Host-wide admission (review-cli#614).** Reviews run in parallel up to what the machine
and the AI providers can sustain, keeping a safety margin (`headroom`, default 20%). There
is no fixed slot count and nothing to wrap `review` in — an old
`flock -o /tmp/review-cli.lock review ...` wrapper still works but only serializes its own
callers.

- **Machine budget (per run).** A run starts only if `load average(1m) + estimates of runs
  admitted in the last ramp_seconds + its own estimate <= (1 - headroom) × CPUs`, and the
  same for RAM (`MemTotal − MemAvailable`). Runs older than `ramp_seconds` are already in
  the load average and MemAvailable, so only young runs are added from their estimate. A
  run's estimate is `base + per_seat × seats`; `per_seat` starts at the configured
  `cpu_per_seat`/`mem_per_seat_mb` and, once 3 runs have been measured, becomes the p75 of
  the last `learn_window` runs' measured process-tree peak RSS and average CPU (each run
  samples its own tree while it holds its slot and appends to `costs.jsonl`). It also waits
  while the kernel's memory-stall pressure (Linux PSI `some avg10` in
  `/proc/pressure/memory`) is above `max_memory_pressure` percent (default 10, 0 = off):
  MemAvailable can look roomy while the host is already reclaiming, as on 2026-10-10/11.
  Swap usage on its own is deliberately not a signal: swap keeps stale pages for hours (on
  2026-10-15 it sat 100% full with 28 GB MemAvailable and PSI 0); a host that is really
  short shows it in MemAvailable or PSI, and both refuse.
  `min_slots` runs always start; `max_slots` (0 = none) caps them.
- **Provider budget (per seat call).** Each seat holds a slot of its provider (`anthropic`
  for `claude:`/`omp:anthropic/`/fable, `openai` for codex, `zai`, `commandcode`, `xai`,
  `opencode-go`, ...): `floor(max_concurrent × (1 − headroom))` (≥ 1) at once (default
  `max_concurrent` 10) and, with `rpm`, `floor(rpm × (1 − headroom))` starts per minute. A
  429/overload reply (`retry.is_throttle`) lowers that provider's limit to
  `floor(current × throttle_backoff)` for `throttle_cooldown_seconds`; repeated throttles
  keep lowering it. A seat still waiting after `provider_wait_seconds` returns the rc=0
  unavailable sentinel, so the failover above moves it to the next provider in its chain or
  to a reserve seat. The per-call timeout starts only once the slot is held. To keep that
  wait rare, a run is admitted only while every provider it needs has room for its planned
  seats next to the seats committed to runs already running.
- **Queue.** Waiters are admitted strictly first-come-first-served (a global sequence number;
  only the oldest live waiter may take a freed slot). While queued, stderr shows
  `[review] admission: waiting — queue position 2/5, 4 running; host budget full on cpu
  (...); ETA ~6m`. The wait counts neither toward the run's recorded duration nor toward the
  internal 4h backstop. An explicit `just-ask --timeout N` stays wall-clock: a run still
  queued at N exits 124 without starting a seat. A `--visual` companion run (vision call,
  then the text panel) is admitted against its full panel up front. `review jobs`
  (also `review jobs --slots [--json]`, bare `review status`) lists every running and
  waiting review host-wide, the budget, the per-seat estimate, and each provider's seats in
  use vs. its current limit; a detached job that is still queued shows `admission=waiting#P`.
  Every admitted run appends its queue wait and the number of reviews running once it
  started to `admissions.jsonl`; `review jobs` summarizes the last 24h from it (admitted,
  queued, wait p50/p90/max, peak running at once — JSON key `recent`). The ledger keeps the
  newest 5000 records (the busiest day so far had ~500 reviews); if a trim ever cuts into
  the 24h window, `recent.complete` is false and the text line says so.
- **Crash safety.** Each lease is `<name>.lock` (flock held by its owner for life) plus
  `<name>.json` in `$XDG_STATE_HOME/review-cli/admission`. The kernel releases a killed
  holder's flock, so the next scan deletes its lease; a lease whose heartbeat is older than
  `stale_lease_seconds` (a stopped/wedged owner) stops counting. The shared `registry.lock`
  is held only for each admission decision and taken with a bounded non-blocking wait; its
  holder records its pid and start time in it. A holder that is stopped (SIGSTOP) or gone
  after `lock_stale_seconds` (default 30) has the lock file replaced by a fresh one, with a
  loud `STALE registry.lock` warning; if that holder resumes, its next registry write sees
  the replaced lock and redoes its decision instead of writing it. A still-running holder
  is never broken; a waiter blocked past `lock_wait_seconds` (default 60) runs without
  host-wide admission, unless an explicit `just-ask --timeout N` passes first (then it
  stops at N without starting a seat).

Every key, its `REVIEW_ADMISSION_*` env var and default (env > config > default) is listed in
`review help config` (HOST-WIDE ADMISSION); `REVIEW_ADMISSION=0` turns admission off.

**To re-rank** the board, reorder the priority list (`DEFAULT_BOARD` in
`reviewlib/config.py`, or a `board:` list in `config.yaml`) — the top entry is the
highest priority. The role lens you attach to each model is independent of its priority.

The GLM seat uses **the maintainer's z.ai subscription** (`glm-5.3`, the newest GLM)
through omp's `zai` provider (`omp:zai/glm-5.3`, review-cli#645), so it reviews
agentically — not the diff-only z.ai REST call. It needs omp installed with z.ai
credentials; the `oc:commandcode/…` seats reach the commandcode gateway through opencode
(see Auth in the README). A missing harness or credential makes the seat probe
unavailable, and the reserve backfills it.

```bash
review --show-board        # active default preset board (light); add --preset heavy to show Sol (Fable excluded)
export REVIEW_TASK_CODE=HYP-742
review diff                # recorded `--task` (via REVIEW_TASK_CODE): light floored to 3 roles
review diff --pool 0       # run all available light-preset seats
review diff --preset default --pool 0  # run all 13 default-preset built-in seats (Fable/Sol excluded)
review diff --preset heavy --pool 0  # run all 14 heavy-preset built-in seats (Fable excluded)
review diff --pool 2       # explicit --pool 2 still wins (warns under --task: below ship floor 3)
review diff --retry 4      # up to 4 in-seat retries on a transient failure before the reserve
review diff --retry 0      # disable in-seat retry (straight to reserve-replace, legacy)
review diff -m grok        # with --task: pad grok across 3 roles; without --task / multi -m: exact
```

## Board vs. models precedence

The diff-review board is always the failover mechanism unless you pass exact CLI models
without any configured `models:`/`board:` metadata.
Precedence:

```
explicit -m requested models   >   explicit --preset   >   `models:` priority roster   >
configured `board:`   >   default preset (light)
```

- A `models:` list in `config.yaml` is the **full priority roster** for `review diff`:
  the first available models fill the live pool, and lower-priority models are reserve
  backfill. It does not disable board/failover.
- Explicit `-m` on the CLI never lets config add extra seats. With no configured
  `models:`/`board:` it is the legacy flat exact panel; with config present it narrows
  the configured board metadata to only the requested models. The board can otherwise
  never be disabled — there is no `--no-board` flag. Use `--pool N` to size the failover
  pool (default 2 for a bare unrecorded `review diff` — the light preset, Alex 2026-08-28 —
  or 4 if you pass `--preset default`; a recorded `--task` run floors light to 3 distinct
  roles so `review task CODE --check` / gh ship can pass from one run; `--pool 0` runs all
  available seats in the selected preset/board (recorded `--task --pool 0` still pads a
  same-role config roster to the ship floor); `--preset heavy --pool 0` currently covers
  all 14 heavy-preset built-ins — Fable is excluded from every preset, see "Priority-ordered
  failover pool" above). `--pool`
  does not reduce an explicit multi-model `-m` list: every requested `-m`
  seat is attempted. A recorded `--task -m one-model` run pads that model across 3 roles (review, quorum, and brainstorm); just-ask never pads — one `-m` model is one call (review-cli#587).
- An "effectively empty" `models:` (absent, `[]`, or only blank entries) is **not** a
  roster — the configured/default board applies.

Override the board itself in `config.yaml` with a `board:` list — each entry is a
`{model, role}` mapping (optional `name:` for the label, optional `effort:` for the
seat's reasoning-effort hint). Supported effort values are `minimal`, `low`, `medium`,
`high`, `xhigh`, and `max`; `xhigh` and `max` mean "highest" in prompts. Native backend
settings are normalized to the closest supported value where the provider exposes one.
When `models:` is present,
`board:` supplies role/name/effort metadata for matching models; priority still comes from
`models:`. When `models:` is absent, `board:` is the full priority-ordered board.

An entry is checked model/capability FIRST: one with no usable `model:` or resolvable
`capability:` is skipped with a warning (the valid entries are kept) and its `role:` is
never inspected. Only an entry that DOES resolve to a real seat then has its `role:`
validated: an unknown value (not a key in the built-in role list) is a hard error naming
the bad value and the valid roles (review-cli#422) — fix the typo, or omit `role:` to use
the generic prompt intentionally. With **no**
`models:` or `board:` configured, the CLI uses the default preset (light as of
2026-08-28); `--preset default` uses the same 13 seats at high effort, and `--preset
heavy` uses the 14-seat `HEAVY_PRESET_BOARD` (review-cli#fable-seat-reliability: Fable
excluded, see "Priority-ordered failover pool" above) — the raw 15-seat board (including last-resort
Fable) is only reached by an EXPLICIT, non-empty `board:`/`models:` listing naming the
seats you want; an absent or empty `board:`/`models:` falls back to the same 13-seat
`LIGHT_PRESET_BOARD` (no Fable, no Sol; same model order as `DEFAULT_PRESET_BOARD`, at
medium instead of high effort) that the default preset now resolves to. A
`board:` that is **present but has no usable entry at all** is a hard error (non-zero
exit) — it never silently falls back to the paid default board.

```yaml
# Priority order: the first 4 reachable models are the live pool; the rest are the
# reserve that backfills a skipped/failed seat.
board:
  - { model: "claude:claude-fable-5-1", role: architect, effort: xhigh }
  - { model: "claude:claude-opus-5-5", role: correctness, effort: high }
  - { model: "codex:gpt-6-astra",      role: consistency, name: Astra, effort: high }
  # Agentic via opencode (oc:provider/model) — reads the repo read-only, like the default
  # board (review-cli#24). Use the diff-only `commandcode:`/`zai:` forms only if you want a
  # stateless keyed-HTTP seat that sees just the diff (and needs no opencode install).
  - { model: "oc:commandcode/moonshotai/Kimi-K3", role: performance, name: Kimi }
  - { model: "oc:zai/glm-5.3",         role: quality }
  - { model: "oc:commandcode/Qwen/Qwen3.8-Max", role: security, name: Qwen }
```

**Optional heavyweight seats** (NOT enabled by default — the default/light preset stays at
13, heavy stays at 14). Add
either to your `board:` list for an extra 1M-context resilience / holistic-senior
pass; both run agentically through opencode's commandcode provider (needs opencode +
`opencode auth login`, like the default `oc:` seats):

```yaml
board:
  # ... the 13 built-in seats ...
  - { model: "oc:commandcode/MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx — resilience
  - { model: "oc:commandcode/nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx — holistic senior
```

Known roles: `architect`, `correctness`, `consistency`, `performance`, `quality`,
`security`, `tests`, `contracts`.

---

[← Back to README](../README.md)
