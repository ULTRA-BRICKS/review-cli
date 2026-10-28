# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## 0.66.6 — 2026-10-28

- Require a fresh, complete same-account five-hour and weekly observation before
  either the direct Claude route or the OMP pool can spend below its configured cap.
  Preserve the provider timestamp through caches; refuse missing, future, stale,
  conflicting and mixed partial reports. Low harness summaries no longer manufacture
  permission, and account displays use the same observation evidence. Related:
  [review-cli#720](https://git.hyperide.ai/ultrabricks/review-cli/issues/720).
- Preserve deliberate cap overrides, tier-specific limits and the exact 80% boundary.
  Fix the live snapshot clock so a completed usage read is not compared with a time
  captured before it started. No auth, rate-limit or provider cooldown is cleared.

## 0.66.5 — 2026-10-28

- **Fix(budget): an account-cap hold starts no seat cooldown**
  ([review-cli#696](https://git.hyperide.ai/ultrabricks/review-cli/issues/696)). Found by the
  live check of 0.66.4: a direct claude seat refused by review-cli's own cap was recorded as a
  10-minute `unavailable sentinel` cooldown, so `review seats` kept showing the seat UNUSABLE
  after the cap or the usage moved. The hold now carries its own result label (`account-cap
  hold`) that the cooldown classifier ignores, while failover still sees the seat as unusable
  and a provider's real quota notice still cools it down.

## 0.66.4 — 2026-10-28

- **Feat(budget): one account usage cap (default 80%) on every Claude route, direct CLI included**
  ([review-cli#696](https://git.hyperide.ai/ultrabricks/review-cli/issues/696)).
  New config key `account_usage_cap_percent` (default 80, `$REVIEW_ACCOUNT_USAGE_CAP_PERCENT`
  overrides, `off` disables). It is the usage threshold of omp's Anthropic account pool unless
  `omp_accounts.max_usage_percent` / a per-account entry sets a more specific one, and it now
  also gates the direct `claude` CLI seat per registered account before the CLI is spawned,
  using that account's `omp usage` windows (by email) and harness card; either at or over the
  cap refuses, and an account with no reading at all is refused conservatively. The refusal
  reads `ограничение review-cli (config account_usage_cap_percent=80): аккаунт X использован
  на 83% окна 5ч, сброс в HH:MM`, never a provider-quota claim, and records no cooldown. An
  invalid value stops the command (exit 2). `review seats` shows the direct claude seat's
  account, usage and verdict against the cap. Behavior change: with no `omp_accounts:` setting
  the omp pool is now capped at 80% (before: used up to the provider limit).

## 0.66.3 — 2026-10-28

- **Feat(omp): omp:anthropic seats take the lowest-usage account first**
  ([review-cli#709](https://git.hyperide.ai/ultrabricks/review-cli/issues/709)).
  Dispatch used omp's account order, so the first free account was spent up to the cap while
  emptier ones idled. Free accounts (neither blocked by omp nor recorded as limited) are now
  tried by their highest live 5h/7d window, lowest first, so spend spreads and rotates as
  readings move; a model tier's window (Fable's) counts only for that tier's models, a window
  that already reset is not a reading, a tie or an account without a reading keeps omp's order,
  and blocked/limited accounts still go last. `omp usage` is now read once per dispatch whenever
  more than one account could answer (before: only with a usage threshold); if it does not
  answer, omp's order applies. `review seats` ranks the same way.

## 0.66.2 — 2026-10-28

- **Feat(seats): per-seat account, usage, cap, reset and reason**
  ([review-cli#707](https://git.hyperide.ai/ultrabricks/review-cli/issues/707)).
  `review seats` prints a SEAT ACCOUNTS section: for every seat its status
  (`usable` / `refused` / `unusable`), the account it would use (the omp Anthropic account
  with the lowest 5-hour/7-day usage below the cap, for `omp:anthropic/*` seats and for
  `claude:*` seats answering through omp), that account's 5h and 7d usage, the cap, the
  reset time and the reason; `--json` carries the same as `seats[].account`. A refusal
  names its rule: `ограничение review-cli (config <key>=80): аккаунт X использован на 83%
  окна 5ч, сброс в HH:MM; другой аккаунт доступен: Y`, `все аккаунты выше порога ...`, or
  `лимит провайдера (omp block / 429) ... это не ограничение review-cli` for an account the
  provider rate-limited; unknown, incomplete or stale usage is refused conservatively and
  says so. Seats on an executor with no usage reading say `usage неизвестен` instead of 0%.

## 0.66.1 — 2026-10-28

- **Fix(config): an unreadable config.yaml is a loud error, not silent defaults**
  ([review-cli#705](https://git.hyperide.ai/ultrabricks/review-cli/issues/705)).
  `load_config()` returned `{}` on any exception, so under an interpreter without PyYAML
  (Homebrew `python3` here) the whole `~/.config/review-cli/config.yaml` -- the roster,
  `omp_accounts` and its usage ceiling -- was ignored while `review seats` looked normal.
  A config file that exists but cannot be loaded (PyYAML missing, a syntax error with its
  line, a non-mapping top level, an unreadable path) now stops the command with exit 2
  and one message naming the file, the cause and the fix. An absent or empty file still
  means defaults; `--help` never reads the config strictly.

## 0.66.0 — 2026-10-27

- Apply capability levels consistently to `diff`, `just-ask`, `quorum` and
  `brainstorm`, including their displayed candidates, pool, role lenses, effort,
  moderator chain and statistics. Explicit model/pool/effort choices still win;
  legacy configured panel rosters remain compatible unless a level is selected.
  Missing seats never silently select a higher level. Refs #697, #699.
- Split the oversized CLI into ordinary command and dispatch modules while
  preserving its public forwarding signatures, monkeypatch seams, source
  introspection, quota/admission controls and review evidence contracts.
- Preserve all reviewer-board cases in collected test modules, update superseded
  panel-default assertions, and isolate test-only admission and grounding state.
- See [capability-level routing](docs/capability-level-routing.md) for precedence,
  migration, verification and the unchanged role/account safeguards.

## 0.65.0 — 2026-10-27

- **Feat(levels): config-driven capability levels with shipped defaults and user overrides;
  moderator chain from the level**
  ([review-cli#697](https://git.hyperide.ai/ultrabricks/review-cli/issues/697),
  [review-cli#695](https://git.hyperide.ai/ultrabricks/review-cli/issues/695)). The preset
  boards, pool sizes, efforts and the quorum/brainstorm moderator chain were Python constants
  (`PRESET_BOARDS`, `PRESET_POOL_SIZES`, `DEFAULT_PRESET`, `MODERATOR_CANDIDATES`); they are now
  DATA in the shipped `reviewlib/levels.yaml` (levels `routine` = old `light`, `standard` =
  `default`, `deep` = `heavy`: pool, effort, role -> priority-ordered seats, moderators,
  `escalate_to`), merged with the user's `levels:` / `default_level:` config (mappings merge,
  lists and scalars replace, `null` deletes; a bad key, effort, pool or role is an error naming
  the key). The default is the lowest sufficient level, `routine` (Sonnet-class seats, low
  effort); `--level NAME` selects another and `--preset` stays as its alias (light/default/heavy
  still work). A missing seat is replaced from the same level's roster only: nothing escalates
  to a heavier level unless asked. Opus and Codex are in `standard`/`deep`; Fable is in no
  shipped level. Quorum/brainstorm take `--level` for the moderator chain, `moderator_candidates:`
  overrides it and an explicit `--moderator` still wins. run-stats records `level` (additive).
  Behavior changes: a config-less `review diff` now runs `routine` instead of the old light
  board; `capacity-check` JSON `configuration.source` reads `level:<name>` and `preset` holds the
  canonical level name. Configs without `levels:` behave as before.

## 0.64.1 — 2026-10-28

- **Fix(availability): codex weekly-limit reset time is parsed**
  ([review-cli#693](https://git.hyperide.ai/ultrabricks/review-cli/issues/693)).
  `try again at Oct 30th, 2026 6:43 PM` (host local zone) now sets the seat cooldown to
  that instant (source `reset-time`) instead of a guessed 8 h timer, so `review seats`
  shows the real recovery time and the seat is not re-dispatched days early.

## 0.64.0 — 2026-10-24

- **Fix(brainstorm): a default `brainstorm --task` can meet the ship floor**
  ([review-cli#610](https://git.hyperide.ai/ultrabricks/review-cli/issues/610), part of
  [review-cli#444](https://git.hyperide.ai/ultrabricks/review-cli/issues/444)).
  Without `-m`, brainstorm ran literary personas and recorded no roles, so
  `review task CODE --check` could not count it toward the 3 distinct roles, whether one
  engine was live or five. Its live panel now gets role slots: one or two live engines
  run 3 distinct roles (each role a separate call, as brainstorm already dispatched 3
  slots per round), a wider panel gets one distinct role per engine, and only completed
  slots are recorded. Dead engines are never cloned. `--pool 1` keeps the persona panel.
  Quorum already assigns default roles, and just-ask still never pads.


Older releases: [complete archive](docs/changelog.md).
