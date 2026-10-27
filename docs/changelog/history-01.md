# Historical release notes

## 0.63.0 — 2026-10-24

- **Feat(gate): one review-gate template, merge/rebase/cherry-pick skip, `review --version`**
  ([review-cli#396](https://git.hyperide.ai/ultrabricks/review-cli/issues/396)). review-cli
  now owns both gate bodies; `review install-commit-hook --print-gate` prints the
  composer-stage gate rig installs (rig-cli#382: exit 0 and a `#!` body on stdout, a usage
  error and non-zero exit for any other arguments). A commit that finishes a merge, a
  `rebase-merge/` rebase or a cherry-pick skips the staged-review requirement with a logged
  reason, including in linked worktrees; `git am` state (`rebase-apply/`) does not. The
  rig-installed gate also gains the trivial-delta tolerance. `review --version` reads
  `[project] version` from a checkout's pyproject, then the installed `hyper-review`
  metadata. `TMPDIR=/`, or a `TMPDIR` that is `$HOME` or an ancestor of it, no longer widens
  the temp-repo exemption to every path; the gate refuses to skip review when it cannot
  hash the staged diff.

## 0.62.0 — 2026-10-23

- **Feat(install): `review install-skill` links `scripts/review-remote` into `~/.local/bin`**
  ([review-cli#658](https://git.hyperide.ai/ultrabricks/review-cli/issues/658)), reported like every
  install target (`+ wrote/updated`, `✓ already configured`, `! conflict` when another file holds the
  name); a package install without the script skips it. Lane vs default provider state: the
  review-remote lane has its own admission dir, so seat leases and the 429 state are NOT shared with the
  box's default lane (documented in docs/review-remote.md).

## 0.61.0 — 2026-10-23

- **Feat(task): a recording run checks `--task CODE` against the repo's tracker**
  ([review-cli#427](https://git.hyperide.ai/ultrabricks/review-cli/issues/427)). With the
  agent-tools `agenttools_taskref` lib installed, `review <mode> --task CODE` (or
  `$REVIEW_TASK_CODE`) asks it whether the code is real before recording the run, and exits 2
  on an invented code with the lib's `task find` / `task new` guidance. The code is recorded
  as written, not rewritten to the lib's canonical form. The lib knows GitHub issues and
  Linear only:
  - a repo that declares another backend (`rig.yaml` `task.backend: forgejo`, as this one
    does) gets the shape check only, and stderr says why. Without this, the lib rejects
    every code there, `#608` included;
  - so does a GitHub-issues repo that takes its repo from `origin` (the lib's default)
    when `origin` is not on github.com or is missing, a run where the lib raises, and a
    GitHub number `gh` gave no verdict on (missing, unauthenticated or offline: the lib's
    "could not verify" answers). A bad shape, a 404 or any other answer is still refused;
  - without the lib, every run gets the shape check only, as before;
  - `review task CODE` and `review task CODE --check` read history and keep the shape check;
  - `REVIEW_TASKREF_VALIDATE=0` turns tracker validation off; `tests/conftest.py` and
    `tests/smoke.py` set it so the suites' synthetic codes stay valid.

## 0.60.0 — 2026-10-23

- **Feat(task): `review task CODE --export`, `--import FILE|-` and `--import-from USER@HOST`
  move review iterations between machines** ([review-cli#659](https://git.hyperide.ai/ultrabricks/review-cli/issues/659)).
  Reviews recorded on the build box were invisible to `review task CODE --check` (and so to
  `gh ship`) on the workstation. `--import-from` runs `review task CODE --export` over ssh and
  appends the records here; each keeps its `repo_id` / `diff_files`, so `--check` still verifies
  it against the local checkout, and a record already in the store is skipped (importing twice
  never double-counts, also when two imports run at once). `review-remote` imports through the
  same function, so a retried import is idempotent too. A transfer flag cannot be combined with
  `--check`/`--json`/`--detail` (a gate never exits 0 without running), and `--import` takes no CODE.
- **Docs: the admission queue is the only host-wide review gate** (same issue). A lock held
  around `review` stays held for the whole queue wait, so every caller behind it waits one
  queue ETA after another while the host has capacity; `review help config` and docs/board.md now say so.

## 0.59.0 — 2026-10-23

- **Fix(admission): z.ai's 5-hour plan quota (HTTP 429, code 1308 "Usage limit reached for 5 hour.
  Your limit will reset at ...") is no longer counted as a throttle** ([review-cli#660](https://git.hyperide.ai/ultrabricks/review-cli/issues/660)).
  The weekly/monthly shape (code 1310) already was not; the 5-hour wording matched no chronic
  marker, so every run's z.ai seat still halved the provider limit and renewed the cooldown for
  a limit that backing off cannot clear. A quota notice that names its own reset ("limit
  exhausted/reached ... Your limit will reset") is now recognised in `retry`; the seat's exclusion
  until that reset stays with `availability`, so a 5-hour quota is not stretched into the seat
  cooldown's up-to-8-hour escalation.


## 0.58.0 — 2026-10-23

- **Feat: `scripts/review-remote` — run `review diff|just-ask|quorum|brainstorm` on a remote box**
  (`REVIEW_REMOTE=user@host`). Ships the exact repo state (HEAD via a bare mirror + staged /
  unstaged / untracked deltas, refused unless diff hashes match), runs the box's own `review`
  with per-job stats/marker files, then imports the box's run-stats records, commit-gate marker
  and diff-scoped review-stamp so `review task --check`, `gh ship` and the pre-commit gates work
  unchanged. See README "`scripts/review-remote`". ([review-cli#656](https://git.hyperide.ai/ultrabricks/review-cli/issues/656))
  `review-remote wait`/`status` without exactly one job id is a usage error (never a second run), and a
  foreground job records its pid so `wait` on a killed job ends instead of spinning.

## 0.57.0 — 2026-10-23

- **Feat(logs): finished call logs move into a SQLite archive inside the log dir**
  ([review-cli#480](https://git.hyperide.ai/ultrabricks/review-cli/issues/480)). A long-lived
  log dir held one file per call (~154k files / 4.4GB observed), and the dashboard and
  `review stat` listed and parsed all of them. Now:
  - a call still streams to its own `.log` file (`tail -f`, the dashboard's live view and
    crash safety are unchanged). When it finishes, its full text is committed to
    `<log dir>/logs-archive.sqlite3` and the file is deleted. A REST backend's sidecar log
    is written to the archive directly. Rows are compressed into chunks of up to 1000 calls
    or 1MB per (day, backend). `REVIEW_LOG_SQLITE=0` keeps the old file-per-call behavior.
    If archival fails, the file stays on disk;
  - the dashboard (`/api/runs`, `/api/stats`, live updates), `review task` and `review stat`
    read archived calls together with on-disk files. The `days` window covers both sources
    and is anchored on the newest stamp from either one. Chunks older than the window are
    not decompressed;
  - `python -m reviewlib.log_archive_migrate [--log-dir DIR] [--skip-delete]` archives an
    existing log dir, then runs four proofs: every call is archived, sampled entries are
    byte-identical, per-day token and call totals match, and brainstorm copies are
    identical. Call files are deleted only when all four pass. Brainstorm `.md` files are
    kept because `review sessions` resumes from them.

## 0.56.0 — 2026-10-23

- **Add `review feedback <run_id>` and close the overseer-feedback 0% fill-rate gap.**
  Real usage showed `~/.config/review-cli/dashboard.json` had never once been written —
  `feedback`/`conscious`/`links` existed only as a dashboard web-UI action with no CLI
  path and no reminder anywhere, so nobody ever used it. Every recorded run now prints a
  one-line stderr reminder (`[review] run recorded (run_id=...). Rate it: review
  feedback <run_id> --useful yes|no [...]`), and `review feedback` writes into the same
  annotation store the web UI reads. Adds a `run_id` field (deterministic from
  `ts`/`mode`/`task_code`) to every new run-stats.jsonl record, a tri-state `useful`
  rating alongside the existing `conscious`/`links` fields, and auto-links a run's
  `task_code` as a ticket when it already looks like one. `REVIEW_NO_FEEDBACK_NUDGE=1`
  silences the reminder for scripted/CI callers. The annotation store now holds a
  cross-process advisory lock (`review feedback` made it a multi-process writer for
  the first time — it used to have exactly one, the long-lived dashboard server),
  mirroring `reviewlib.seat_cooldown`'s degrade-rather-than-hang design: it never
  blocks a write indefinitely, degrading to in-process-only after a small bounded
  wait on a contended peer (an accepted, narrow lost-update window under sustained
  contention — same tradeoff `seat_cooldown` already makes). `--ticket`/`--pr`/
  `--note`/`--useful` writes are validated up front, then applied in a SINGLE
  load-modify-write (`store.apply_feedback`) rather than up to four separate ones —
  a real all-or-nothing write, not just validated-then-hopefully-uninterrupted.
  `run_id` is a 64-bit (16 hex char) digest — long enough that a collision needs
  billions of runs to become likely. `/api/stats` gains a `useful_count` alongside
  the existing `conscious_count`/`feedback_count`, so a usefulness-only annotation
  (no free-text note) still shows up in the dashboard's fill-rate signal, and the
  Overview panel shows it as a `rated` card. The store's cross-process lock goes through
  `reviewlib.portable` (`fcntl` on POSIX, `msvcrt` on Windows), and
  `--note`/`--useful`/`--ticket`/`--pr` are registered with the `-o` pre-scan so a note
  starting with `-o ...` is not read as an output path.
- **`review stat`: join run-stats.jsonl's mode/repo attribution and a fail-bytes
  column, closing gaps found in a 2026-09 token-overspend investigation ([review-cli#355](https://git.hyperide.ai/ultrabricks/review-cli/issues/355)).**
  New `by_mode`/`by_repo_top` sections (busiest mode/repo first, from `reviewlib.stats`'
  ground-truth store — the pre-existing report only ever broke usage down by
  harness/model, never by which MODE or REPO was generating it) and a `FAIL%B` column
  on the per-harness table (`bytes_fail`/`bytes_fail_pct` in `--json`) showing what
  share of a harness's bytes were spent on calls that failed anyway — the
  retry-inflation cost signal.
- Known gap, tracked separately ([review-cli#357](https://git.hyperide.ai/ultrabricks/review-cli/issues/357)): a `review feedback` annotation is
  invisible in the dashboard's own per-session drill-down (it looks up by a different,
  call-log-derived session id) — it does still count toward the dashboard's aggregate
  `feedback_count`/`conscious_count`.

## 0.55.0 — 2026-10-23

- **Feat(dashboard): the Metrics tab shows real token usage and $ cost**
  ([review-cli#344](https://git.hyperide.ai/ultrabricks/review-cli/issues/344)), instead of
  a permanent "not recorded" empty state: calls with and without a usage reading, total
  input/output tokens, total cost, and a per-model table. Each model row carries an explicit
  state: a dollar figure, `+ N unpriced`, `unpriced` (real tokens, no verified price) or
  `not tracked` (CLI-mode backends, which report no usage). An unknown cost is never shown
  as $0. Only claude API-mode calls to `https://api.anthropic.com` get a $ figure, priced
  from the same verified table as rig-cli's `riglib/usage.py`, which now includes the
  default Opus seat `claude-opus-4-8` and resolves `-YYYYMMDD` snapshot ids.
- **Fix(dashboard): large reviews no longer under-report tokens and cost**
  ([review-cli#369](https://git.hyperide.ai/ultrabricks/review-cli/issues/369)). A REST
  call that the provider billed but that returned no review text (common on large prompts)
  wrote no usage to its call log, so the Metrics totals silently dropped its spend while
  `run-stats.jsonl` counted it. REST call logs now end with a
  `[review-cli] USAGE prompt_tokens=N output_tokens=N` line before the EXIT footer on every
  call with a provider reading; the dashboard and `review stat` read it, falling back to
  the old stdout usage line for older logs. The call-log cache re-parses once (parser
  version 3).

## 0.54.0 — 2026-10-23

- **Feat(availability): a dead seat or provider is excluded until the time its own error
  names, and `review health` finds dead seats before a review pays for them**
  ([review-cli#663](https://git.hyperide.ai/ultrabricks/review-cli/issues/663), closes the
  rest of [#660](https://git.hyperide.ai/ultrabricks/review-cli/issues/660)). New
  `reviewlib/availability.py` reads every failed seat call: a quota with a named reset (z.ai
  `429` code 1310 `will reset at <time>`, the Claude CLI's `resets 7:30pm (Europe/Moscow)`, an
  epoch `x-ratelimit-reset`, `Retry-After`), exhausted credits, a suspended account, a rejected
  login (ends when the credential file changes) and a long rate limit, and records
  `unavailable_until` in `seat-cooldown.json` (bucket `window`, with `cause`, `source`,
  `watch` and `probe_after`). A provider-wide exhaustion (z.ai, commandcode, gemini, xai,
  opencode-go, openrouter, moonshot, fireworks) also records `provider:<name>` and excludes every
  seat on it; long windows are re-probed periodically. The exclusion is read by
  `backend_unavailable_reason` (board selection, `review --show-board`: `SKIPPED (unavailable:
  <reason> (until <time>))`), `admission.routed` (an excluded seat holds no run slot and no
  provider reservation, so reviews are no longer serialized behind a dead provider), and
  `call_backend` (a skip sentinel, no call, no provider slot). `review jobs` shows an excluded
  provider as `EXCLUDED` instead of `THROTTLED`. REST seats (z.ai, commandcode, gemini,
  openrouter, codex) now feed the store too; HTTP `Retry-After` headers are kept in the error text.
- **Feat(health): `review health [--seat M] [--preset P] [--host H] [--json] [--cached]
  [--no-probe] [--timeout S]`.** Probes every seat in parallel with the cheapest call its
  backend offers (5 s timeout; no admission slot, no review log, no run-stats record), updates
  the store (a seat that answers drops its stale window; one that does not time out is `unknown`,
  not excluded) and caches results (`$REVIEW_HEALTH_TTL_SECONDS`, default 300 s). The same check
  runs automatically before `review diff|quorum|brainstorm|just-ask` ask for a slot
  (`$REVIEW_HEALTH_PREFLIGHT=0` disables it). `--host H` runs it on another machine over ssh.
  `review help health`, README and docs/board.md describe it.
- `review capacity-check` reads a seat's availability windows (its route and, for a
  provider-wide exhaustion, `provider:<name>`) like timer cooldowns and no longer treats a
  stored exclusion as a backend-probe failure: a `quota` window is quota evidence under the
  same freshness and account-identity rules (the freshest typed record wins), any other
  cause (`auth`, `unavailable`, `rate_limit`, `stall`) makes the seat `unavailable`.

## 0.53.0 — 2026-10-23

- **Feat(diff): an over-budget diff is split into passes, never truncated**
  ([review-cli#662](https://git.hyperide.ai/ultrabricks/review-cli/issues/662)). A diff over
  `$REVIEW_DIFF_MAX_BYTES` (default 300000, now "max bytes of diff per pass") used to be cut
  at the cap with a marker, so no seat saw the tail of a big change and `--staged` stamps /
  `--commit` checkpoints were refused. `review diff` now splits it (`reviewlib/diff_chunks.py`):
  at `diff --git` file boundaries, whole files packed greedily in diff order; a single file
  over the budget at `@@` hunk boundaries, its header repeated in every piece with a visible
  `[chunk i/n of <path>]` line. The board runs once per part, one after another inside ONE
  run (one admission slot, one run-stats record and `--task` iteration with all its seats),
  each prompt saying `Part i of n of the change` and listing its files so seats do not flag
  files they cannot see. `reviewlib/diff_passes.py` merges the answers into one report per
  seat under `### Part i/n` headings; with `--json`, `findings[]` holds every pass's findings
  and a seat is `reported` only when every pass of it parsed. The split is announced on stderr
  with each part's files. `--staged` stamps and `--commit` checkpoints are now valid for a
  split diff (every file was reviewed) and still withheld unless EVERY pass produced its
  usable verdicts. A diff within the budget is dispatched exactly as before.
  - **Loud failure, never a silent cut.** Only one HUNK (or one header-only file section)
    bigger than the budget is unsplittable: `review diff` exits `13` before any seat runs,
    naming the file, the hunk number and the sizes. `$REVIEW_DIFF_MAX_BYTES=0` (or any value
    `<= 0`) never splits and sends the whole diff.
  - **brainstorm / quorum / just-ask** put the diff in one prompt, so an over-budget diff now
    fails loudly (exit `13`, naming sizes and the way out) instead of grounding on a fraction
    of it — on a big dirty tree pipe a scoped diff in (`git diff -- <path> | review brainstorm
    ...`) or raise the budget. A diff piped on stdin is never split or refused, in any mode.
  - **Removed:** the dispatch-time truncation function and its marker, the over-cap
    stderr warning, the `EXIT_COMMIT_DIFF_TRUNCATED` code (exit `13` now means "diff cannot
    be reviewed whole"), and the `diff_already_capped` mode-context key.
  - `REVIEW_FAKE_RECORD_DIR` (test-only, with `REVIEW_FAKE_BACKEND`) records the prompt and
    diff each fake seat call received.

## 0.52.1 — 2026-10-23

- **Fix(admission): an exhausted provider quota is no longer counted as a throttle**
  ([review-cli#660](https://git.hyperide.ai/ultrabricks/review-cli/issues/660)). z.ai answers
  a used-up weekly quota with HTTP 429 and `Weekly/Monthly Limit Exhausted. Your limit will
  reset at <time>` (code 1310). `is_throttle` / `text_is_throttle` read that as provider
  pushback, so every run's zai seat halved the provider's concurrent-seat limit to 1 and
  renewed the 300 s cooldown. Each run reserves a zai seat, so the host admitted one review at
  a time (one running, 15 waiting, load 2.1 of 9.6) until the quota reset. A failure carrying a
  seat cooldown's chronic-quota marker (`CHRONIC_QUOTA_MARKERS`: the weekly/monthly reset
  notice, "session limit", "usage credits") now never feeds the admission limit; the marker
  list is shared with the seat cooldown. Genuine 429 / 529 pushback still throttles. Excluding
  a quota-exhausted provider from the board plan up front stays open on #660.

## 0.52.0 — 2026-10-23

- **Feat(dashboard): the Metrics tab shows real token usage and $ cost**
  ([review-cli#344](https://git.hyperide.ai/ultrabricks/review-cli/issues/344)), instead of
  a permanent "not recorded" empty state: calls with and without a usage reading, total
  input/output tokens, total cost, and a per-model table. Each model row carries an explicit
  state: a dollar figure, `+ N unpriced`, `unpriced` (real tokens, no verified price) or
  `not tracked` (CLI-mode backends, which report no usage). An unknown cost is never shown
  as $0. Only claude API-mode calls to `https://api.anthropic.com` get a $ figure, priced
  from the same verified table as rig-cli's `riglib/usage.py`, which now includes the
  default Opus seat `claude-opus-4-8` and resolves `-YYYYMMDD` snapshot ids.
- **Fix(dashboard): large reviews no longer under-report tokens and cost**
  ([review-cli#369](https://git.hyperide.ai/ultrabricks/review-cli/issues/369)). A REST
  call that the provider billed but that returned no review text (common on large prompts)
  wrote no usage to its call log, so the Metrics totals silently dropped its spend while
  `run-stats.jsonl` counted it. REST call logs now end with a
  `[review-cli] USAGE prompt_tokens=N output_tokens=N` line before the EXIT footer on every
  call with a provider reading; the dashboard and `review stat` read it, falling back to
  the old stdout usage line for older logs. The call-log cache re-parses once (parser
  version 3).

## 0.52.0 — 2026-10-23

- **New: `review capacity-check [--preset NAME] [--json]` — is the active board's whole
  capacity out of quota?** ([review-cli#472](https://git.hyperide.ai/ultrabricks/review-cli/issues/472)).
  `gh ship` (agent-tools#705) skips its manual review-hatch prompt when this says
  `all_exhausted: true`, so it is asserted only on evidence:
  - every seat of the board a no-`-m` `review diff` would run counts, expanded to each
    provider-failover route (Claude CLI ↔ omp twins included) and each registered Claude
    CLI account; each (route, account) is `exhausted`, `usable`, `unavailable` or
    `unknown`, with a typed reason, its evidence and when that evidence was observed;
  - `exhausted` needs a harness-cli card at 100% for that account's own quota (not reset
    yet), or a cooldown whose typed cause is `quota` recorded within the last 30 minutes
    (`$REVIEW_CAPACITY_MAX_AGE_SECONDS`) on a route that is its own quota identity. No
    evidence is `unknown`, never usable;
  - the JSON is versioned (`schema_version: 2`) and names the evaluated configuration
    (source, preset, board fingerprint), each source's status, every candidate and the
    coverage. `review help capacity` documents every field and what each source can't see.
- **Seat cooldowns record a typed cause** (`quota`, `unavailable`, `stall`, `auth`) next
  to their reason text; `active_cooldown` returns it. Cooldowns recorded by earlier
  versions have none and never count as quota evidence.
- **omp's Anthropic account pool is account-scoped in the report.** An omp seat on omp's
  Anthropic accounts (review-cli#637) rotates through several logins and records each
  limit under that account's own cooldown key, so `capacity-check` evaluates one candidate
  per pool account (policy-excluded accounts dropped; its Claude Code card matched by
  email) and, with several accounts, never reads the route-level cooldown a pool call
  also writes as proof for any one of them. Every cooldown the omp pool records now
  carries its cause: a rate or usage limit is `quota`, a failed login is `auth`.

## 0.51.1 — 2026-10-23

- **Dashboard: session-detail no longer reads as an unbroken wall of stderr,
  adds Round N grouping, and surfaces board role + a recovered prompt per
  call ([review-cli#345](https://git.hyperide.ai/ultrabricks/review-cli/issues/345)).** codex genuinely streams its whole transcript
  (including its own echo of the sent prompt) to real OS stderr and only the
  terse final answer to stdout — review-cli's capture was already correct,
  but the UI gave stdout no visual priority, so a healthy call read as
  "everything is an error." Session-detail now splits each call into a
  prominent "Result (stdout)" block and a collapsible "verbose transcript
  (stderr, N lines)" section (auto-expanded only on a real error or when a
  call produced no stdout at all). The Calls list groups under "Round N"
  headers whenever a session genuinely carries more than one round
  (brainstorm/quorum); a single-round session (the common diff-review shape)
  still renders flat rather than a fabricated "Round 0" header. Each call now
  also shows its board role/lens (e.g. `correctness`, `consistency`), joined
  from the same board metadata the Errors tab already uses (a Claude seat that
  ran on its `omp:anthropic/…` twin keeps the seat's role), and — when a
  subprocess backend echoed it into its own transcript — its recovered
  prompt, with an honest "no prompt recovered" note when it wasn't (REST
  backends never carry one, by design; prompts are still not persisted as
  their own field). A related finding — the two example sessions used to
  investigate this turned out to be test-suite artifacts leaked into the
  real log dir, not genuine runs — is tracked separately (review-cli#346).

## 0.51.0 — 2026-10-17

- **Feat(models): bump board and alias pins to the newest models**
  ([review-cli#645](https://git.hyperide.ai/ultrabricks/review-cli/issues/645)). The board,
  flat default panel, visual list, moderator, role fallbacks, provider-failover chains and
  generic aliases now name the current release of each model family:

  | old | new |
  |---|---|
  | `claude:claude-opus-4-8` | `claude:claude-opus-5-5` |
  | `claude:claude-sonnet-5` (and `omp:anthropic/claude-sonnet-5`) | `claude:claude-sonnet-5-5` (and `omp:anthropic/claude-sonnet-5-5`) |
  | `claude:claude-fable-5` | `claude:claude-fable-5-1` |
  | `codex:gpt-5.6-sol` | `codex:gpt-6.1-sol` |
  | `codex:gpt-5.6-terra` (`TERRA_SEAT`, "Terra") | `codex:gpt-6-luna` (`LUNA_SEAT`, "Luna"; the Terra line has no GPT-6 successor, Luna is GPT-6's low-cost tier) |
  | `codex:gpt-6-astra` | unchanged |
  | `zai:glm-5.2` (`glm`, z.ai default model) | `zai:glm-5.3` |
  | `commandcode:zai-org/GLM-5.2` | `commandcode:zai-org/GLM-5.3` |
  | `oc:xai/grok-4.6` | `oc:xai/grok-4.7` (still on `VISUAL_MODELS`) |
  | `omp:opencode-go/mimo-v2.5` | `omp:opencode-go/mimo-v2.6-pro` |
  | `commandcode:moonshotai/Kimi-K2.7-Code` | `commandcode:moonshotai/Kimi-K3` |
  | `commandcode:Qwen/Qwen3.7-Max` | `commandcode:Qwen/Qwen3.8-Max` |
  | `deepseek/deepseek-v4-pro` | unchanged |
  | board GLM seat `oc:zai/glm-5.2` | `omp:zai/glm-5.3` (omp's z.ai route) |

  Generic aliases (`opus`, `sonnet`, `fable`, `sol`, `grok`, `mimo`, `glm`) follow the new
  pins; new pinned aliases `opus55`, `sonnet55`, `fable51`, `gpt61sol`, `luna`/`gpt6luna`,
  `grok47`/`grok-4.7`, `mimo26`, `glm53`. Version-numbered aliases keep their own release:
  `opus48`, `sonnet5`, `fable5`, `gpt56sol`, `terra`/`gpt56terra`, `grok46`/`grok-4.6`,
  `mimo25`/`mimo-2.5` and `glm52` still name the old models. The `glm-5.3` failover chain
  is `zai:` → `oc:zai/` → `commandcode:` → `omp:zai/`. The dashboard keeps its labels for
  the old ids (historical call logs) and adds the new ones; `reviewlib.TERRA_SEAT` is
  renamed `reviewlib.LUNA_SEAT`. Evidence: omp's Anthropic route answered
  `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-fable-5-1`, codex answered
  `gpt-6.1-sol` and `gpt-6-luna`, and z.ai answered `glm-5.3` on 2026-10-17. The
  `grok-4.7`, `mimo-v2.6-pro`, `Kimi-K3`, `Qwen3.8-Max` and `zai-org/GLM-5.3` ids come
  from [models.dev](https://models.dev/api.json) (no xai / opencode-go / commandcode login
  on the probing machine).
- **Docs(help): ZAI_API_KEY also feeds omp:zai seats.** The `--show-board` footer and the
  help config section no longer say ZAI_API_KEY only powers the diff-only `zai:` seat.

## 0.50.0 — 2026-10-17

- **Feat(seats): `review seats` lists every harness login on the machine, and omp:zai
  seats run on review-cli's z.ai key**
  ([review-cli#646](https://git.hyperide.ai/ultrabricks/review-cli/issues/646)). A new
  HARNESS ACCOUNTS section in `review seats` / `status` / `doctor` (`harness_accounts` in
  `--json`) shows, per harness, who is logged in and where it was found, never a secret:
  Claude Code (the default config dir's email, plan and login state from `claude auth
  status`; a registered `~/.claude-accounts` set is pointed at its own section), Codex
  (`$CODEX_HOME/auth.json`: the ChatGPT login's email and plan, or an API key; an
  id_token that does not decode to an email or a blank key is not counted), omp (every
  provider in agent.db with its usable/disabled credential count, plus providers served
  by a key review-cli passes), opencode (the auth.json, opencode.json `options.apiKey` and
  env sources the `oc:` probe accepts) and review-cli's own API keys (the variable and
  file it resolves from, and whether its seats' provider is marked unpaid). An
  `omp:zai/*` or `omp:openrouter/*` seat with no credential stored in omp now gets
  `ZAI_API_KEY` (or `ZHIPU_API_KEY`) / `OPENROUTER_API_KEY` from the env or
  `~/.config/review-cli/.env`, and its availability probe counts that key, so GLM runs
  agentically through omp on the key that until now only reached the diff-only `zai:`
  seat. A `.env` that is not valid UTF-8 now reads as "no key" instead of raising.

## 0.49.0 — 2026-10-16

- **Feat(omp): exclude omp Anthropic accounts and cap their usage**
  ([review-cli#641](https://git.hyperide.ai/ultrabricks/review-cli/issues/641)). New
  config.yaml key `omp_accounts:` for the `omp:anthropic/*` seats' account pool:
  `exclude: [email, ...]` accounts are never used (no token is fetched for them), and an
  account with an `omp usage` window at or above `max_usage_percent` (global, or per
  account under `accounts: {email: {max_usage_percent: N}}`, which wins) is skipped until
  that window resets; a model-tier window (Fable's 7-day) counts only for that tier, and
  unknown usage never skips. Env overrides: `$REVIEW_OMP_ACCOUNTS_EXCLUDE` (comma-separated)
  and `$REVIEW_OMP_MAX_USAGE_PERCENT`; a config value an env override replaces is not
  checked. Emails match case-insensitively. When every account is excluded or over its
  threshold, the seat fails without a model call and names each account and why, and
  `review seats` shows the seat UNUSABLE with that reason. An invalid value fails the seat
  with the problem instead of being ignored; an unreadable or empty agent.db with a policy
  set, or a policy together with `$REVIEW_OMP_CLAUDE_PROFILES` (a profile's account is
  unknown), fails rather than letting an excluded account answer, and while an exclude
  list is set an account without a known email is skipped. `review seats` / `status` /
  `doctor` (text and `--json`) show the policy and each account's `EXCLUDED` /
  `OVER LIMIT (window, % >= threshold, resets at)` reason; the answer's `account:` line
  lists the accounts the policy skipped.

## 0.48.0 — 2026-10-16

- **Fix(omp): omp seats start inside the seat sandbox without `OMP_REAL_BIN`**
  ([review-cli#636](https://git.hyperide.ai/ultrabricks/review-cli/issues/636)). An omp
  seat runs with HOME set to an empty sandbox dir, and the `omp` wrapper script on PATH
  found its real binary as `$HOME/.local/lib/omp/omp`, so every omp seat exited 127. The
  launcher is now resolved from the real environment before the sandbox exists: for a
  wrapper script the real binary (`$OMP_REAL_BIN`, `$PI_INSTALL_DIR/omp`, then
  `~/.local/lib/omp/omp`) is handed to the seat as `OMP_REAL_BIN`. A wrapper that refers
  to HOME and still fails under a sandboxed HOME makes omp seats unusable before dispatch
  (`SKIPPED` on `--show-board`, the wrapper's own message in `review status`) instead of an
  exit 127 mid-run. `$REVIEW_OMP_BIN` / config.yaml `omp_bin:` override the omp a seat runs.
- **Feat(omp): `omp:anthropic/*` seats rotate through omp's Anthropic accounts**
  ([review-cli#637](https://git.hyperide.ai/ultrabricks/review-cli/issues/637)). The seat
  reads the enabled Anthropic OAuth accounts from omp's agent.db (read-only; ids and
  identities only) and pins each attempt to one of them: `omp token anthropic --account N`
  for the token, passed as `ANTHROPIC_OAUTH_TOKEN` to an omp run whose agent dir is empty
  (omp 18.8.1 ignores that variable while stored credentials exist). Accounts omp has
  rate-limited (`auth_credential_blocks`) or review-cli saw limited (cooldown store, for
  omp's `retry-after-ms`) go last; a 429 rate limit or a failed login moves on to the next
  account. The output names the account that answered (`> [review-cli] account: ...`) and
  the per-call log header carries `account=<email>`. Bare `review status` and the new
  aliases `review seats` / `review doctor` list the omp launcher and each account's
  availability (omp blocks, review-cli cooldowns, `omp usage` windows). A pinned run also
  never reads the real agent dir's `mcp.json`.

## 0.47.0 — 2026-10-15

- **Feat(admission): wait on memory-stall pressure and record every run's queue wait**
  ([review-cli#633](https://git.hyperide.ai/ultrabricks/review-cli/issues/633), follow-up of
  [#614](https://git.hyperide.ai/ultrabricks/review-cli/issues/614)). A run now also waits
  while Linux PSI `some avg10` (`/proc/pressure/memory`) is above `max_memory_pressure`
  percent (default 10, 0 = off; `admission.max_memory_pressure` /
  `REVIEW_ADMISSION_MAX_MEMORY_PRESSURE`; the first `min_slots` runs still always start) —
  the same signal the agent-tools
  heavy-op-memory-gate hook refuses on, so that hook can leave reviews to this queue. Each
  admitted run appends its queue wait and the number of reviews running once it started to
  `admissions.jsonl`; `review jobs` / `review jobs --slots [--json]` show the last 24h
  (admitted, queued, wait p50/p90/max, peak running at once; JSON key `recent`) and the
  current memory pressure. Swap usage alone stays deliberately unweighted: stale swap pages
  linger for hours (100% full next to 28 GB MemAvailable and PSI 0 on 2026-10-15); real
  shortage shows in MemAvailable or PSI, and both refuse.

## 0.46.0 — 2026-10-10

- **Feat(admission): run reviews in parallel up to machine and provider capacity with a
  20% margin** ([review-cli#614](https://git.hyperide.ai/ultrabricks/review-cli/issues/614)).
  Replaces the host-wide single `flock` / fixed 3-slot wrapper with an admission controller
  inside `review`. A run starts (FIFO) only while projected host CPU (load average + young
  runs' estimates + its own) and RAM stay within `(1 - headroom)` of capacity; the per-run
  estimate is learned from measured runs. Each seat call holds a slot of its provider
  (`floor(max_concurrent × (1 - headroom))`, optional requests/minute); a 429/overload reply
  halves that provider's limit for a cooldown, and a seat saturated past
  `provider_wait_seconds` fails over like an unavailable seat. Leases are flock-held files
  (a killed holder is reclaimed at once, a stale heartbeat stops counting; a process stopped
  or gone while holding the shared `registry.lock` past `lock_stale_seconds` gets its lock
  replaced, and no waiter blocks on it past `lock_wait_seconds`); stderr shows queue
  position + ETA; `review jobs` / `review jobs --slots [--json]` / bare `review status` show
  running and waiting reviews and provider seats. Every parameter is configurable via
  `admission:` in config.yaml and `REVIEW_ADMISSION_*` env (`review help config`). Queue time
  is excluded from run stats and from the internal backstop, but counts toward an explicit
  `just-ask --timeout N` (a run still queued at N exits 124); a `--visual` companion run is
  admitted against its full text panel. Callers still wrapping `review` in
  `flock -o /tmp/review-cli.lock` keep working.

## 0.45.2 — 2026-10-10

- **Fix(cli): never hang on a silent non-tty stdin**
  ([review-cli#613](https://git.hyperide.ai/ultrabricks/review-cli/issues/613), dup of
  [#516](https://git.hyperide.ai/ultrabricks/review-cli/issues/516)). `sys.stdin.read()`
  ran unconditionally before any dispatch, so a review inheriting an open-but-silent
  socket/pipe as stdin (an omp eval kernel, a detached `pty=false` session, a hook) sat
  forever in `unix_stream_data_wait` while holding the host-wide review lock — one such
  run starved 34 queued reviews. Now `--staged` / `--diff` never read stdin at all (a diff
  piped in anyway is ignored with a stderr notice), and a verb with no explicit diff source
  reads stdin with an idle timeout of `$REVIEW_STDIN_WAIT_SECONDS` (default 5; a stall
  before the first byte or mid-stream) before falling back to the repo diff with one
  stderr line. `git diff | review diff` is unchanged. Regression:
  `tests/test_stdin_silent_pipe.py`.

## 0.45.1 — 2026-10-08

- **Fix(review): durable skip log for board seats dropped before dispatch**
  ([review-cli#469](https://git.hyperide.ai/ultrabricks/review-cli/issues/469)). A seat
  removed from both the pool and the reserve (backend unavailable, or a live seat at
  ≥100% usage) never reached a backend call, so no per-call log was written. Every such
  seat now gets a `write_skip_log` entry with its reason (`usage N% (quota exhausted)`
  for quota drops), even when seat reuse padded the pool back to size.

## 0.45.0 — 2026-10-08

- **Fix(seat cooldown): recognise the "Weekly/Monthly Limit Exhausted. Your limit will reset
  at <ts>" quota notice** ([review-cli#339](https://git.hyperide.ai/ultrabricks/review-cli/issues/339)).
  The chronic-quota markers lacked this phrase, so a seat returning it was retried on every
  call instead of cooling down, and the dashboard counted the retry as "other" instead of
  "session_limit". The marker is anchored on "...limit exhausted. your limit will reset", so
  transient wording such as "429 rate limit exhausted; retry after 5 seconds" still does not
  cache a chronic cooldown. The backend and the dashboard now read one tuple,
  `reviewlib.seat_cooldown.CHRONIC_QUOTA_MARKERS`, instead of two copies that had drifted
  (review-cli#342).

## 0.44.0 — 2026-10-01

- **Breaking: the `claude-p` wrapper is retired; the claude seat runs plain `claude --print`.**
  `claude --print` works on the Claude subscription again, so review no longer has a
  `claude-p` fallback. Every `claude:` seat and the `review qa` claude tester now need the
  real `claude` binary on PATH; with it missing the seat fails with a clear
  `claude not found on PATH` error instead of looking for `claude-p`. The `claude-p`
  model/provider alias is gone (use `claude` / `claude:<model>`). `review qa` now spawns
  `claude --print --permission-mode bypassPermissions --allowedTools … --output-format json`
  (no `--cwd`/`--timeout-sec`; the working directory is the spawn cwd and the wall timeout
  is review's own).
- **Breaking: `CLAUDE_ROTATE_ROOT` is renamed `REVIEW_CLAUDE_ACCOUNTS_ROOT`.** It overrides
  the per-account `CLAUDE_CONFIG_DIR` registry root (default `~/.claude-accounts`); the
  `claude-rotate` launcher that used to own that layout is gone, but the
  `~/.claude-accounts/account-N` directories and review's multi-account rotation over them
  are unchanged. Set the new name if you pointed the old one at a custom root.

## 0.43.0 — 2026-09-30

- **Fix(task --check): `--verify-repo-only`, and review history that survives a repository
  move** ([review-cli#589](https://git.hyperide.ai/ultrabricks/review-cli/issues/589)).
  `gh ship` used `--no-verify-identity`, so its quorum counted every repository's
  iterations for a task code: `#464` in sme-archiving-gc passed on rig-cli and review-cli
  reviews stored as `464`. Now:
  - `review task CODE --check --verify-repo-only -C <repo>` compares the repository alone.
    Iterations recorded in another repository no longer count toward iterations, models or
    roles, whatever spelling of the code they used. Same-repository iterations count even
    when their files don't overlap the checkout's own diff, which is unrelated to the PR
    when `gh ship` runs from the primary checkout. `identity_verification` reads
    `repo_only`. `gh ship` switches to it in agent-tools#824;
  - `--check --json` names the iterations dropped for being from another repository in
    `foreign_repo_iterations` (`repo_id`, `task_code`, `ts`, `passed`). The list stops at
    50; `excluded_foreign_repo_iterations` holds the full count and
    `foreign_repo_iterations_truncated` marks a cut list. When no diff is compared
    (`--verify-repo-only`, or a clean checkout), a refusal's `error` and stderr warning
    both count these records, failed ones included;
  - a repository's former remotes are one table of old id → current id
    (`_REPO_ID_ALIASES` in `reviewlib/stats.py`), which now also maps
    `git.hyperide.ai/ultrabricks/sme-archiving-gc-tmp` to
    `github.com/alex-mextner/sme-archiving-gc`. The checkout's reviews from its Forgejo
    period count again after its origin moved back to GitHub.

## 0.42.1 — 2026-09-30

- **Fix(omp): an omp seat that answered keeps its answer when its scratch dir can't be
  deleted** ([review-cli#598](https://git.hyperide.ai/ultrabricks/review-cli/issues/598)).
  Since 2026-09-30 09:05 UTC every omp seat on this Mac answered and was then reported as
  failed with `[Errno 66] Directory not empty: '…/home/Library/Caches/bun/@t@'`, so the
  review threw the answer away and paid for a second call through the claude CLI twin.
  The cause: omp run from source (`bun src/cli.ts`) starts a helper process in its own
  session that outlives the omp call by a few tens of milliseconds, and that helper writes
  Bun cache files into the seat's scratch home while review was deleting it. Now review
  retries the delete briefly, and if the dir still can't be removed it prints
  `[review-cli] warning: could not remove seat sandbox <path> (…)`, leaves the dir to the
  OS temp cleaner, and keeps the seat's answer.

## 0.42.0 — 2026-09-30

- **Fix(just-ask): `--timeout N` is a hard wall-clock bound, and one `-m` model is one call**
  ([review-cli#587](https://git.hyperide.ai/ultrabricks/review-cli/issues/587)). On
  2026-09-29 a just-ask proofread printed nothing for 25 minutes, and a retry with
  `-m claude:claude-opus-4-8 --timeout 300` was still running after 600 s. `--timeout` was
  an idle window with a 20-minute floor that any output reset, a single `-m` model with
  `--task` ran as 3 role slots, and failover and retries each got the full window. Now,
  with an explicit `review just-ask --timeout N`:
  - every seat call still running N seconds after dispatch is killed with its whole
    process group, and no failover call starts after that;
  - the command prints each seat it waited on and why it gave no answer — `timeout` (with
    the seat's last output line), `quota`, `no output` or `failed` — and exits 1;
  - if the command is still alive at N + 30 s (a seat wedged where no per-call kill
    reaches), a watchdog prints each seat still running with its last output and how long
    ago it came, kills every seat process group, and exits 124.
  just-ask no longer pads one `-m` model into role slots: it asks every seat the same
  question with no role lens, so one `-m` model is one call. quorum and brainstorm still
  pad (their seats answer under role lenses) and now say so on stderr: `--task with one -m
  model: quorum runs <model> as 3 role slots (…) = 3 calls`. Without `--timeout`, just-ask
  keeps the idle window; `review diff` and the board are unchanged.

## 0.41.0 — 2026-09-30

- **Fix(board): a failed Claude CLI login is remembered, so `review status` says the seat is
  unusable and the next call goes straight to omp**
  ([review-cli#596](https://git.hyperide.ai/ultrabricks/review-cli/issues/596)). On
  2026-09-30 `claude auth status` said `loggedIn: true` for account-0 and account-1 while
  every call on them failed with "Failed to authenticate: OAuth session expired and could
  not be refreshed". Nothing recorded that, so bare `review status` kept calling the
  `claude:*` seats usable and every review paid the failed Claude CLI call again before its
  omp twin answered. Now:
  - a call whose login fails records LOGIN FAILED for that Claude account (the failure
    line, in the seat-cooldown store with its escalating 10m/30m/2h/8h window), and the
    account is not tried again while another registered account answers;
  - once every account has failed its login, the `claude:*` seats count as unusable: a
    board, `-m` review or just-ask / quorum / brainstorm hands them straight to their
    `omp:anthropic/` twin without calling the Claude CLI first, and the `substituted:` line
    names the login failure and the `CLAUDE_CONFIG_DIR=… claude auth login` commands;
  - bare `review status [--json]` and `review entities` show the seat UNUSABLE with the
    failure, the accounts it hit and the omp substitute, and each such account as
    `LOGIN FAILED <time> (<failure>) -- log it back in: <command>`;
  - the record is dropped by a later successful call on the account, or as soon as the
    account is logged back in: its stored login (the macOS Keychain item's modification
    date, or `.credentials.json`'s mtime; the secret itself is never read) rewritten after
    the failure. With no omp twin reachable, the Claude CLI is still tried, since nothing
    else can answer.

## 0.40.0 — 2026-09-30

- **Fix(board): a dead Claude CLI seat answers through omp, and `review status` says why a
  seat cannot run**
  ([review-cli#594](https://git.hyperide.ai/ultrabricks/review-cli/issues/594)). On
  2026-09-29/30 every `claude:*` seat failed ("Failed to authenticate: OAuth session
  expired", "You've hit your session limit") while omp's own Anthropic accounts kept
  answering the same models; reviews degraded and agents had to hand-pick
  `-m omp:anthropic/…`. Now:
  - every `claude:claude-X` seat falls to `omp:anthropic/claude-X` (and the reverse)
    before any other route, in `review diff`'s board, its flat `-m` path, and just-ask /
    quorum / brainstorm, which previously had no provider failover at all;
  - the result prints `substituted: claude:… -> omp:… (claude:… unusable: <why>)`, and
    each switch notice on stderr names why the previous executor failed;
  - the substituted seat counts once under its own role for the ship quorum, recorded as
    the omp model that actually answered;
  - the Claude CLI stays the first executor: omp is never stored as its last-working
    provider, so the next run tries the Claude CLI again once it recovers;
  - bare `review status [--json]` (no job id) lists every board seat as usable or
    UNUSABLE with the reason (unpaid provider, missing CLI or login, active cooldown and
    its end time, every Claude CLI account logged out) and the executor it would be
    substituted with, plus each registered Claude CLI account as logged in or LOGGED OUT
    with the `CLAUDE_CONFIG_DIR=… claude auth login` command. `review entities` shows the
    same reasons and account states.

## 0.39.0 — 2026-09-29

- **Fix(board): a review keeps every role filled when seats fail**
  ([review-cli#588](https://git.hyperide.ai/ultrabricks/review-cli/issues/588)). On
  2026-09-29, 29 of 142 review runs covered fewer than 3 distinct roles, and in the week
  before, 190 runs ended as correctness + quality + quality. Failover promoted the
  next-priority reserve with whatever lens it carried, so consistency and security
  disappeared whenever Codex or Grok were down. Now:
  - the startup pool takes distinct roles first. A seat whose role an earlier pool seat
    already covers waits in the reserve. If such a seat must still fill a slot, it reviews
    under a board role the pool lacks (`board re-lensed` on stderr). `--show-board` marks
    the same pool;
  - a failed seat is backfilled by a reserve seat with the same role, else a reserve seat
    whose own role is still uncovered, else the next reserve seat re-lensed onto the missing
    role. Once the reserve is empty, a model that already returned a usable verdict in this
    run is dispatched again under the missing role (a real call with that lens). A reserve
    seat whose model already failed in this run is skipped. An exact `-m` board is not
    padded this way: a failed explicit seat stays failed (review-cli#137);
  - a run that still covers fewer distinct roles than it owes (min of the pool size and the
    board's distinct lensed roles) prints a `ROLE SHORTFALL` notice on stdout. The notice
    names each missing role and why its seats failed, and the run exits non-zero;
  - the Claude CLI account rotation now also moves on to the next registered account when
    a login is gone ("Failed to authenticate: OAuth session expired", "not logged in"). A
    logged-out `current` account used to stop the rotation and take down every `claude:*`
    seat;
  - the dashboard's "planned fallback" hint names the next seat with the same role.

## 0.38.0 — 2026-09-26

- **Build: published on PyPI as `hyper-review`** (the command stays `review`; `review-cli` on
  PyPI is an unrelated project). The README installs with uv or pipx from PyPI, the
  git.hyperide.ai one-liner or a clone, and tells people with the pre-rename install to remove
  it first. `install.sh` recognises the repo origin by host and path (a lookalike host no longer
  passes for the canonical repo), pulls from the origin it validated, and installs the clone
  editable, so `git pull` updates the tool.
- **Fix(dashboard): a create/delete in the same directory-mtime tick is no longer hidden.** The
  log-dir name-list cache is used only once the directory has been quiet for 2 s — Linux's
  coarse mtime clock let a deleted old log keep a stale session alive.
- **Fix(windows):** `review qa --report` no longer crashes on Python < 3.13 (the third
  `os.fchmod` site now goes through `portable.fchmod`), and the repo's `.git` dir is resolved
  with UTF-8 decoding like every other git read (a non-ASCII path crashed the diff review, or
  mis-matched and stripped the repo's own `GIT_*` env).
- Correction to 0.37.14's "POSIX behaviour is unchanged": stdout/stderr that are not UTF-8 are
  re-encoded as UTF-8 (`errors="replace"`) on POSIX too — a latin-1 terminal now receives UTF-8
  instead of a crash on a character it cannot encode.

## 0.37.14 — 2026-09-25

- **Fix: `review` runs natively on Windows.** `import fcntl` (POSIX-only) made the package
  unimportable there. All cross-process advisory locks (job records, seat cooldowns, spec-web
  store/registry, the `~/.claude.json` trust lock) now go through ONE helper,
  `reviewlib.portable.lock_file`/`unlock_file`: exactly `fcntl.flock` on POSIX, and
  `msvcrt.locking` on a lock byte far past any data on Windows with the same semantics
  (exclusive between opens/processes, non-blocking -> `BlockingIOError`, blocking waits
  indefinitely). Other POSIX-only spots on the `diff`/`task --check`/`just-ask` path are
  guarded in the same module: backend children start in their own process group
  (`CREATE_NEW_PROCESS_GROUP`) and are tree-killed with `taskkill /T /F`; `os.getuid`,
  `O_NOFOLLOW`/`O_NONBLOCK`, `os.getpgid`/`killpg`/`SIGKILL` and `os.fchmod` (Python < 3.13)
  have fallbacks; job liveness no longer uses `os.kill(pid, 0)`, which TERMINATES the process
  on Windows. stdout/stderr (and stdin on Windows) are re-encoded as UTF-8 with
  `errors="replace"` (a model's `→` crashed printing on a cp1251 pipe), `git diff` output is
  decoded as UTF-8 on Windows (a Cyrillic "И" silently produced an EMPTY diff), and the
  workspace-trust key uses claude's forward-slash form there. POSIX behaviour is unchanged.

## 0.37.13 — 2026-09-24

- **Feat(review diff): additive `--json` structured findings output (review-cli#546).**
  `review diff --json` (and a `review visual IMAGE --diff/--staged` companion run, which
  shares the same handler) emits a machine-parseable envelope on stdout instead of the
  prose panel — a flat `findings` array (`file`/`line`/`severity`/`confidence`/`verdict`/
  `source`) plus `summary.expected`/`reported`/`degraded`. Each backend is additionally
  asked to emit one fenced JSON block on top of its normal prose review; a seat whose
  block can't be parsed (or that failed outright) is reported `degraded` rather than
  crashing the run. Exit `0` = no findings, `1` = findings present, `2` = no seat's
  structured response was usable at all — a contract scoped entirely to `--json`, distinct
  from the prose path's existing seat-health-only `0`/`1` exit. Reuses the `--json` flag
  already present on every mode parser (added for the standalone `review visual` verdict)
  rather than a second, colliding flag — that flag was a pure no-op for diff review before
  (its old branch only fires with no diff at all). Purely additive: the prose default is
  byte-identical when the flag is absent.

## 0.37.12 — 2026-09-24

- **Feat(board): checked-in per-project lens overrides via `.review/lenses/<role>.md`
  (review-cli#547).** The revmux `.revmux/` pattern applied to review-cli's board: a
  project can now drop `.review/lenses/<role>.md` (role = any `REVIEW_ROLES` key) at its
  repo root to teach a board role something specific to its own codebase (a payments
  invariant, a house style rule) without forking/patching `reviewlib/config.py`. New
  `BoardReviewer.resolved_lens(cwd)` (config.py) reads the file relative to the reviewed
  repo's root and, when present and non-blank, APPENDS it after the role's built-in
  `REVIEW_ROLES` lens — extension, never silent replacement, so an override can never
  accidentally drop the built-in coverage the board was designed around. A
  missing/unreadable/blank-after-strip file all resolve to "no override" (never a crash);
  blank-means-absent is the deliberate safer default so an accidentally-emptied file falls
  back to the full built-in lens rather than a silent no-op. `panel.build_board_job` (and
  `build_board_jobs`) now take an optional `cwd` and call `resolved_lens` instead of the
  bare `role_lens`; `run_board_with_failover` (the real diff-review dispatch path) and
  `mode_just_ask`'s task-role-padded path both thread their own `cwd` through, so a repo
  with no `.review/lenses/` at all dispatches byte-identical prompts to before this
  change. `review --show-board` now suffixes any seat whose role has an active override
  with `(lens overridden: .review/lenses/<role>.md)` and prints a summary footer line
  naming every overridden role. Hardened against a self-review finding: the override read
  refuses a symlink anywhere in `.review/lenses/<role>.md`'s path (`Path.resolve()` +
  `is_relative_to(cwd)`, plus `O_NOFOLLOW`/`O_NONBLOCK` on the open) so a git-tracked
  symlink pointing outside the repo can't have review-cli's own process read an arbitrary
  host file and ship it to every matching seat — including diff-only HTTP backends that
  otherwise have no filesystem access at all — and caps the read at
  `LENS_OVERRIDE_MAX_BYTES` (8 KiB) so neither a FIFO nor a pathologically large file can
  hang or `MemoryError` the process.

## 0.37.11 — 2026-09-18

- **Fix(config): board composition rejects an unrecognized `role:` string instead of
  silently duplicating the generic prompt (review-cli#422).** A `board:` entry (literal
  `model:` or `capability:`-resolved) whose `role` isn't a `REVIEW_ROLES` key used to be
  kept with a stderr warning and fall back to the generic review prompt — harmless for one
  typo, but a mixed board with SEVERAL typo'd roles collapsed every one of those seats onto
  the exact same generic prompt, silently turning "N roles reviewed" into "N duplicate
  generic passes". `_parse_board_entry` now raises `BoardConfigError` immediately, naming
  the bad value and the full valid-role enum, the same cost-safety stance `load_board`
  already takes for an all-malformed board. An omitted role (`role: ""` / no `role` key) is
  unaffected — still the intentional generic-prompt seat, no error. Also fixes a regression
  the new validation exposed: the `--task` single-`-m` panel role-pad path
  (just-ask/quorum/brainstorm) called `board_from_models` with no `BoardConfigError` guard,
  so a typo'd config `board:` role would have crashed the whole CLI dispatch with a raw
  traceback instead of the friendly `[review-cli]` warning every sibling call site prints
  — now wrapped to match, warning and retrying with the malformed board metadata ignored
  (the fix is one shared `_board_from_models_lenient` helper, reused by all three explicit
  `-m` call sites). A non-string `role:` (e.g. an unquoted `role: true`/`role: no` parsed
  by YAML as a bool) is rejected the same way a typo'd string is, instead of silently
  coercing to the empty/general role.

## 0.37.10 — 2026-09-10

- **Perf: dashboard hot path no longer scales with the full log corpus (review-cli#447).**
  PR #426 skipped `.stat()` for old files but every fingerprint/SSE still `os.scandir`'d
  all ~154k names, and `load_sessions` still parsed every artifact — `GET /api/runs?gap=90`
  hung. `_scan_log_dir_entries` now reuses the candidate name list when the directory
  mtime is unchanged (still `.stat()`s the windowed subset so a live-streaming `.log`
  stays visible; listing cache ≠ TTL debounce; cache is published only when pre-scan
  and post-scan dir mtimes match). `load_sessions()` stays all-history by default so
  `review task` and write-side existence checks keep old transcripts; dashboard GET
  `/api/runs` and `/api/stats` pass `days=7` (matching `review stat --days`; `?days=`
  override, `days<=0` = all history). Measured 2026-09-08 against `~/Library/Logs/review-cli`
  (154810 entries, 148100 `.log`, 6706 `.md`):
  - `_scan_log_dir_entries` (6h window): 0.55–0.75s every call → 0.44s first / **0.12s**
    cached (name list reused; 5085 windowed / 154806 total).
  - `load_sessions` default 7-day window: **4.3s** (2870 sessions / 10939 calls) instead
    of hanging over the full corpus.

## 0.37.9 — 2026-09-10

- **Feat(specweb):** version history + compare view (review-cli#484 item 2).

## 0.37.8 — 2026-09-10

- **Feat(specweb):** anchor-change tracking, auto-height panel, agent hints, resolve/delete icons (#484).

## 0.37.7 — 2026-09-10

- **Feat(backends): omp openai-codex multi-credential fallback (review-cli#491).**
  Mirror the omp-hosted Claude profile rotation loop for openai-codex seats: read
  active credential ids from omp agent.db, try each in order on chronic exhaustion,
  and pin attempts via `OMP_CREDENTIAL_ID` when multiple credentials exist.
  Single-credential hosts keep the pre-change dispatch path.

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
