# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

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
