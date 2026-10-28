# Capability-level routing

`review diff`, `review just-ask`, `review quorum` and `review brainstorm` use the
same capability-level configuration. Concrete models, providers, role priorities,
pool size, effort and moderators live in packaged `reviewlib/levels.yaml`, with
machine-specific overrides in `~/.config/review-cli/config.yaml`.

## Precedence

An explicit `-m` remains an exact model choice. Otherwise an explicit `--level`
selects the level's roster instead of a legacy `models:` or `brainstorm_models:`
list. Without `--level`, an existing legacy list remains compatible; when there
is no such list, `default_level` selects the configured default. `--preset` is an
alias, including the existing `light`, `default` and `heavy` names.

`--pool`, `--effort` and `--moderator` are explicit per-run overrides. Without
those flags, the selected level supplies their values. A recorded panel normally
preserves the distinct-role floor. An explicit smaller pool is still allowed to
run, but cannot falsely satisfy a larger required review quorum.

```sh
review quorum 'Check the proposed transaction boundary' --task TASK-123 --level routine
review brainstorm 'Compare the two migration options' --task TASK-123 --level standard
review just-ask 'Explain this failure' --task TASK-123 --level routine --pool 1
review diff --staged --task TASK-123 --level standard
```

Choose the least costly sufficient level. Selecting a deeper level is an explicit
escalation, not an availability fallback: record the concrete risk or unsuccessful
cheaper approach in the task. Provider or model names alone are not reasons to
forbid a route or choose an expensive one. Codex and Opus remain configurable in
the standard/deep data; an explicit model can also be requested directly.

## User overrides

Mappings inherit unchanged keys, lists/scalars replace their field, and `null`
removes a key according to the level loader's validated contract. For example:

```yaml
default_level: routine
levels:
  routine:
    pool: 3
    effort: medium
omp_accounts:
  exclude: []
  max_usage_percent: 80
```

The example changes policy fields without binding the routing implementation to
any model vendor. Role/model and moderator overrides use the same schema as the
shipped YAML. Unknown levels, roles, fields or effort values fail with an
explanation rather than silently choosing another tier.

## Availability and cost safeguards

A missing or exhausted seat can be replaced only by candidates inside the chosen
level. A usable engine can fill distinct role lenses when the existing role-pool
policy permits it. That is multiple roles, not multiple distinct models.
`just-ask` does not create duplicate calls to the same engine merely to ask the
same question again. When no selected-level route is available, the command
refuses before model dispatch instead of trying an unrequested expensive roster.

Existing per-account shared-window quota checks, unpaid-provider restrictions,
account exclusions, admission controls and review/ship gates still apply. A direct
CLI route in cooldown does not imply another already-authorized route has no
quota. No cooldown or safety marker should be cleared to force a run.

## Migration and proof

Legacy lists intentionally retain their behavior. To adopt automatic levels on a
machine, preserve its prior config, move necessary concrete choices into validated
`levels:` overrides, and remove only the superseded legacy panel lists. Preserve
account ceilings, excluded providers and visual-specific settings. Do this only
after installing the version that supports level-aware panels, not while an old
binary is still in use.

Before a paid call, compare the read-only inventory for each relevant command:

```sh
review quorum question --level routine --list-defaults
review brainstorm topic --level routine --list-defaults
review just-ask question --level routine --list-defaults
review diff --level routine --list-defaults
```

CLI listing/help/health and actual model dispatch resolve through the shared
selection logic. Statistics retain the canonical selected level, the actual
models and completed role coverage. A model-selection test is not evidence of a
successful real review; a local test result is not green CI or an installed
release.

## CLI organization

`reviewlib/cli.py` remains the public compatibility facade. Ordinary command
modules own parsing, task/history display, account diagnostics and output. The
ordered dispatch stages separate parsing, policy selection, grounding, admission
and execution. They receive the facade explicitly, retaining public monkeypatch
seams and call signatures while avoiding a single oversized command function.
All modules remain subject to the normal lint, test, review and ship controls.
