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

- **Startup failover** — the active pool is the **top N AVAILABLE** seats by priority.
  A higher-priority seat whose backend isn't reachable (no key / not on PATH) is
  **skipped** and the next-priority seat is pulled up, so you still start with the
  requested number of working models. (E.g. if Sol is unavailable in the heavy preset,
  the pool starts at Opus.)
- **Mid-run failover** — if an active seat **fails during the review** (backend error,
  timeout, empty output, or an "unavailable" reply such as a paywalled model returning
  *"… is currently unavailable"*), the next-priority **reserve** model is promoted and
  run in its place, repeating until the requested number of working verdicts is produced or the reserve is
  exhausted (then the run degrades gracefully and says so on stderr, exiting non-zero).
- **Account rotation (review-cli#168)** — every `claude:*` seat and the omp-hosted
  Claude seat authenticate as ONE Claude account, so an account-wide outage or paywall
  (or a 100% harness 5-hour/7-day window) used to take out all of them at once. On a
  multi-account host (`~/.claude-accounts/account-*/`, the same registry `claude-rotate`
  already uses; `$CLAUDE_ROTATE_ROOT` overrides the root) the direct `claude:*` dispatch
  now tries each registered account in rotation order — the persisted `current` account
  first, then wrapping through the rest — via a per-call `CLAUDE_CONFIG_DIR`, skipping
  any account whose own harness window is exhausted and falling through to the next on a
  chronic-unavailable result. The omp-hosted Claude seat rotates the same way across
  `$REVIEW_OMP_CLAUDE_PROFILES` (comma-separated, already-logged-in omp profiles). Only
  when EVERY configured account is exhausted does the run emit the synthetic usage-limit
  skip, now naming every account it tried; a host with no registry behaves exactly as
  before.

The role/lens **travels with the seat**: priority decides *who* sits in the pool, the
role decides *with what lens* they review. A promoted reserve brings its own lens, so
the panel still covers a broad set of facets.

`--pool N` overrides the preset default (the top-N available, with the same failover);
`--pool 0` runs **all available** seats in the selected preset/board. Use
`--preset heavy --pool 0` for all 14 heavy-preset built-in seats (the raw 15-seat board,
including last-resort Fable, needs an explicit `board:`/`models:` config). The board is **never
disabled** — `--pool` only sizes the pool.

**review-cli#382:** a `unpaid_providers: [commandcode, gemini]` config listing disables
every commandcode-routed seat (GLM-cc/Kimi/Qwen/DeepSeek) plus Gemini at once, machine-wide,
leaving several roles (`performance`/`quality`/`security`/`tests`/`contracts`) with zero or
one live seat — `quality`'s one live seat hit a real weekly quota exhaustion with nothing
left to promote, hard-blocking a real PR's review-quorum gate. Decision: Terra and Sonnet
were added as genuinely distinct, already-paid `performance`/`quality` fallbacks on accounts
the board already uses, and GLM was re-lensed off its now-redundant `quality` role onto
`security`. `contracts`/`architect` remain single-seat roles (`tests` later gained a second,
paid live seat in MiMo, review-cli#450); Astra's pre-existing `consistency` duplicate of
Sol's lens was deliberately left as is rather than reintroduce a different
single-point-of-failure. See
[review-cli#382](https://github.com/alex-mextner/review-cli/issues/382).

**Agentic by default.** Every board seat that *can* read the repo does. Fable/Opus run via
the agentic claude CLI **when `claude-p` is on PATH** (they fall back to the diff-only
Anthropic API only on a host that lacks the CLI but has an API key), Sol/Astra via the codex
CLI (Astra — GPT-6-Astra, pinned via `codex:gpt-6-astra` — is the #5 board seat as of
review-cli#286, was #6 before Fable's demotion moved every subsequent seat up one slot; it
replaced a bare `"codex"` seat that silently tracked the CLI's own default model),
and Kimi/z.ai-GLM/Qwen/DeepSeek/Grok through opencode (`oc:provider/model`) — all run
read-only *inside* `-C` and can open any project file, not just the diff. Two seats are
always diff-only stateless HTTP calls: **Gemini** (no agentic transport) and the priority-3
**GLM-cc** seat (`commandcode:zai-org/GLM-5.2` — opencode's `commandcode` provider does not
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
paid for one full `claude-p` dispatch on *every* review invocation before falling to the
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

**Diagnostic gap (known, tracked):** `stalled_models` is scoped to the models a task's
recorded runs actually attempted (`record_run`'s `models=`), which on the normal
board/failover dispatch path only ever contains the SEATS THAT SURVIVED — a chronically-
cooling seat that a reserve successfully backfilled never appears there, so the
diagnostic stays silent in exactly the case (a stuck seat masked by working failover)
it exists to surface. Tracked as
[review-cli#227](https://github.com/alex-mextner/review-cli/issues/227).

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
is tracked as [review-cli#226](https://github.com/alex-mextner/review-cli/issues/226),
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
(light) pool of 2 are unaffected (both `<=` the cap). This is a *per-process* cap; a swarm
of separate `review` processes also leans on the per-seat timeout (a stalled seat frees its
slot fast) and on the slow `oc:zai/glm-5.2` seat being **deprioritized to last-resort
reserve** (review-cli#65).

**To re-rank** the board, reorder the priority list (`DEFAULT_BOARD` in
`reviewlib/config.py`, or a `board:` list in `config.yaml`) — the top entry is the
highest priority. The role lens you attach to each model is independent of its priority.

The GLM seat uses **the maintainer's z.ai subscription** (`glm-5.2`, the newest GLM)
through opencode's `zai` provider, so it reviews agentically — not the diff-only z.ai
REST call. It needs that provider configured in opencode (see Auth in the README); the
other `oc:commandcode/…` seats
reach the commandcode gateway the same way. opencode must be installed for the agentic
seats; without it they fall back to the reserve.

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
  seat is attempted. A recorded `--task -m one-model` run pads that model across 3 roles (review, just-ask, quorum, and brainstorm).
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
  - { model: "claude:claude-fable-5",  role: architect, effort: xhigh }
  - { model: "claude:claude-opus-4-8", role: correctness, effort: high }
  - { model: "codex:gpt-6-astra",      role: consistency, name: Astra, effort: high }
  # Agentic via opencode (oc:provider/model) — reads the repo read-only, like the default
  # board (review-cli#24). Use the diff-only `commandcode:`/`zai:` forms only if you want a
  # stateless keyed-HTTP seat that sees just the diff (and needs no opencode install).
  - { model: "oc:commandcode/moonshotai/Kimi-K2.7-Code", role: performance, name: Kimi }
  - { model: "oc:zai/glm-5.2",         role: quality }
  - { model: "oc:commandcode/Qwen/Qwen3.7-Max", role: security, name: Qwen }
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
