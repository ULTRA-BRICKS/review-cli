"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations


def _help_topic_config(_api, /) -> str:
    """The `review help config` deep reference (ROADMAP "Topic-based help across the
    ecosystem"): config file path + cascade, the keys, key/auth env vars, and how the
    reviewer board + model selection resolve. Kept in sync with config.py / backends.py
    behavior (help-docs-sync); a flag/behavior change updates this topic in the same commit.

    NOTE: the "LEVELS" section below prints every level straight from the shipped
    `levels.yaml` merged with the user's config (`_levels_help`), so its numbers cannot drift
    from the data."""
    adm = _api.admission.Settings()
    return f"""\
review — configuration reference
================================

CONFIG FILE
  {_api.CONFIG_PATH}
  Optional YAML. Absent / unparseable -> the built-in defaults apply (never an error).
  Keys:
    models:            list[str]  priority roster for `review diff`: the live pool is
                       selected from this full ordered set and the rest are reserve.
                       For just-ask/quorum it remains the flat default panel.
    brainstorm_models: list[str]  default panel for `review brainstorm` (falls back to
                       `models:`, then the built-in defaults). Unreachable backends are
                       dropped gracefully.
    levels:            mapping    per-level overrides of the shipped capability levels
                       (pool, effort, roles, moderators, escalate_to, aliases); see
                       CAPABILITY LEVELS below. default_level: str picks the default level.
    moderator_candidates: list[str]  ordered seat ids tried as the quorum/brainstorm
                       moderator (aliases expand; unavailable seats are skipped; a failing
                       one falls to the next). Not set -> the `moderators` chain of the
                       active level (`--level`, else the default level; the shipped routine
                       chain is Sonnet-class, so a roster-less config never starts on Opus).
                       An explicit --moderator always goes first. A malformed value (empty /
                       not a list of strings) is ignored with a stderr notice.
    visual_models:     list[str]  ordered vision backends for `review visual` and companion
                       visual fan-out. Separate from the text reviewer board; runtime
                       failures skip to the next vision backend.
    board:             list[seat]  override the built-in reviewer board (see BOARD below).
    unpaid_providers:  list[str]   providers whose billing/subscription is currently
                       unavailable; every direct or oc: seat under them is skipped before
                       any backend process/API call. Env equivalent:
                       REVIEW_UNPAID_PROVIDERS=commandcode,fireworks.
    omp_bin:           str         the omp executable omp: seats run instead of the `omp` on
                       PATH. Rarely needed: a wrapper `omp` script's real binary is found
                       before the seat's sandbox replaces HOME (OMP_REAL_BIN, PI_INSTALL_DIR,
                       ~/.local/lib/omp/omp) and handed to it as OMP_REAL_BIN. Env
                       equivalent: REVIEW_OMP_BIN=/path/to/omp.
    omp_accounts:      mapping     which of omp's Anthropic accounts omp:anthropic/* seats may
                       take (`review seats` shows the policy and each account's reason):
                         exclude: [you@example.com]   never used (no token is fetched)
                         max_usage_percent: 90        an account with an `omp usage` window
                                                      at or above it waits for that window's
                                                      reset (a model-tier window, e.g.
                                                      Fable's, counts only for that tier);
                                                      unknown usage does not skip
                         accounts: {{you@example.com: {{max_usage_percent: 95}}}}
                                                      per-account threshold, wins over the
                                                      global one
                       Every account excluded or over its threshold -> the seat fails naming
                       each account and why (`review seats` shows the seat UNUSABLE); an
                       invalid value fails the seat with the problem (never ignored), and so
                       does a policy together with $REVIEW_OMP_CLAUDE_PROFILES (a profile's
                       account is unknown) or with no omp account list to apply it to. While
                       `exclude` is set, an account without a known email is skipped. Env
                       overrides, replacing the config value (which is then not checked):
                       REVIEW_OMP_ACCOUNTS_EXCLUDE=a@x.com,b@y.com (at least one email)
                       replaces `exclude`, REVIEW_OMP_MAX_USAGE_PERCENT=90 (not empty)
                       `max_usage_percent`.
                       `review seats` also prints SEAT ACCOUNTS: per seat its status, the
                       account it would use (lowest usage below the cap), 5h and 7d usage,
                       the cap, the reset time and the reason; a refusal names the rule
                       ("ограничение review-cli (config ...)" vs "лимит провайдера");
                       usage that is unknown or stale is refused conservatively.
    timeout:           int         per-call timeout seconds for `review diff` (replaces the
                       configured/default request timeout; still overrideable
                       per-invocation by --timeout). Review/panel agent CLI backends treat
                       this as an idle/silence timeout with a 20m default floor for normal
                       review runs; REST backends use it as their HTTP request timeout.
                       `review qa` uses its timeout as a
                       wall-clock QA cost cap.
    admission:         mapping     host-wide parallelism budget (headroom, per-seat cost,
                       min/max slots, per-provider limits, ...). See HOST-WIDE ADMISSION
                       below for every key, its REVIEW_ADMISSION_* env var and default.
  Model ids accept the friendly aliases. Generic names follow the current pin
  (`opus` -> claude:claude-opus-5-5, `sonnet` -> claude:claude-sonnet-5-5,
  `fable` -> claude:claude-fable-5-1, `sol` -> codex:gpt-6.1-sol, `luna` -> codex:gpt-6-luna,
  `glm` -> zai:glm-5.3, `cc` -> commandcode, `grok`/`grok47`/`grok-4.7` -> oc:xai/grok-4.7,
  `mimo`/`mimo26` -> omp:opencode-go/mimo-v2.6-pro). Version-numbered aliases keep their
  own release: `opus48` -> claude:claude-opus-4-8, `fable5` -> claude:claude-fable-5,
  `glm52` -> zai:glm-5.2, `grok46`/`grok-4.6` -> oc:xai/grok-4.6,
  `grok45` -> oc:xai/grok-4.5, `mimo25`/`mimo-2.5` -> omp:opencode-go/mimo-v2.5,
  `terra`/`gpt56terra` -> codex:gpt-5.6-terra.

SELECTION CASCADE, by mode:
  review diff           : explicit -m requested models  >  explicit --level/--preset  >
                          config `models:` priority roster  >  config `board:`  >
                          the default level (routine unless `default_level:` says otherwise).
                          With configured `models:`/`board:` metadata, -m narrows that
                          board metadata to only the requested models; without config it is
                          the legacy flat exact panel unless an explicit level supplies
                          metadata/effort for the requested models.
  review visual         : explicit -m  >  `visual_models:`  >  visual defaults.
  review brainstorm     : explicit -m  >  `brainstorm_models:`  >  `models:`  >  defaults.
  review just-ask/quorum: explicit -m  >  `models:`  >  the built-in defaults.
  quorum/brainstorm moderator: --moderator  >  `moderator_candidates:`  >  the `moderators`
                          chain of --level (default: the default level).
  review qa             : IGNORES config `models:` / `brainstorm_models:` / the defaults.
                          It picks ONE write/exec tester: `REVIEW_QA_TESTER=claude|codex`  >
                          a bare `-m claude|codex`  >  the `claude` default. (opencode is not
                          in v1.) The panel/board cascade does not apply to qa.
  Built-in defaults: {", ".join(_api._expand_alias(x) for x in _api.DEFAULT_MODELS)}.
  Visual defaults:  {", ".join(_api._expand_alias(x) for x in _api.VISUAL_MODELS)}.
  See the live default for each subcommand in `review <mode> --help` (the --model line).

CAPABILITY LEVELS (the diff-review default; `review --show-board` prints the board live)
  A level says how much review a task needs: pool size, reasoning effort, a roster of
  role -> priority-ordered seats, the quorum/brainstorm moderator chain and the next
  heavier level. The defaults are DATA in the shipped file
  {_api.Path(_api.__file__).with_name("levels.yaml")} (no model or provider is hard-coded in the
  engine): {_api._levels_help()}. Default: `{_api._default_level_for_help()}`, the lowest sufficient
  level. `review diff --level NAME` (`--preset NAME` is the same option, with the old
  names light/default/heavy as aliases of routine/standard/deep) asks for another level;
  nothing is ever dispatched on a heavier level because a seat of the lighter one is
  unavailable: a missing seat is replaced from the SAME level's reserve (its roster order),
  and an explicit `--level` is the only way up (the pool guard may PROPOSE one).
  Fable is in no shipped level (a ~100% dispatch failure rate on the owner's account); add it to
  a level of your own, or use `-m fable`.

  Override per user in config.yaml `levels:` (review-cli has no project-level config, the
  user config is the one override layer): a mapping inherits from the shipped level and a key overrides one field;
  mappings merge key by key, lists and scalars replace, `null` deletes a key; a new level name
  defines a new level (it needs pool, effort, roles, moderators). Example:
    levels:
      routine:
        effort: medium                    # one field, everything else inherited
        roles:
          correctness: ["claude:claude-opus-5-5"]   # replaces that role's seats only
          tests: null                      # drops the role
        moderators: ["omp:zai/glm-5.3"]
    default_level: standard               # optional: another level as the default
  A seat is a model string or alias, or {{model, name, effort}}. Roles are the REVIEW_ROLES
  keys. A bad level (unknown key, effort, pool, escalate_to, role) is an error naming the key,
  e.g. `levels.routine.effort`. `models:` / `board:` keep working exactly as before and keep
  precedence over the default level (an explicit --level wins over both). paid-provider
  limits (`unpaid_providers:`) and the `omp_accounts:` usage ceiling apply to every level.
  The level used is recorded in run-stats (`level`).

  The listing also prints per-role fallback model ids (bare names; harness auto-resolved). A
  recorded `review diff --task CODE` floors the pool to {_api.SHIP_MIN_ROLES}
  distinct roles so `review task CODE --check` / gh ship can pass from one run; explicit
  `--pool N` still wins (warns if N<{_api.SHIP_MIN_ROLES}). `-m one-model` with `--task` pads
  that model across {_api.SHIP_MIN_ROLES} roles for review, quorum, and brainstorm (quorum and
  brainstorm announce the role slots on stderr, one call each); just-ask never pads — one
  `-m` model is one call. A default (no `-m`) brainstorm `--task` gives its live panel
  max({_api.SHIP_MIN_ROLES}, engines) distinct role slots per round; `--pool 1` opts out.
  `review task CODE --check` reports distinct ROLES and distinct MODELS separately (3 roles
  can be 2 models); the ship floor counts roles, `--min-models N` counts models.
  `--pool N` sizes the selected board (`--pool 0`
  = all available; recorded `--task --pool 0` still pads a same-role config roster
  to the ship floor). Explicit -m never lets config add extra seats; it narrows
  configured metadata when present, else uses the flat exact panel (unless `--task`
  pads a single model). To set the priority
  roster, configure `models:`. To add role/name/effort metadata (or a full board when
  `models:` is absent), set `board:` in config.yaml (a list of {{model, role, name,
  effort}} entries; `name` is the display label, `role` is a key into the built-in role
  lenses, and `effort` is one of minimal/low/medium/high/xhigh/max) or edit DEFAULT_BOARD in
  reviewlib/config.py.

KEYS / AUTH (resolved from the process env first, then the shared .env)
  Shared .env:  ~/.config/review-cli/.env   (override with $GEMINI_ENV_FILE)
  Env vars:
    GEMINI_API_KEY (or GOOGLE_API_KEY)  — gemini seat.
    ZAI_API_KEY    (or ZHIPU_API_KEY)   — z.ai / GLM: the diff-only zai: seat, and omp:zai/* seats when omp stores no zai credential.
    COMMANDCODE_API_KEY                 — commandcode seats (a `user_...` token ONLY).
    OPENROUTER_API_KEY                  — diff-only OpenRouter aggregator seat (`-m openrouter:<model>`).
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN (+ optional ANTHROPIC_BASE_URL) — claude API.
    REVIEW_CLAUDE_MODE=api|cli, REVIEW_<BACKEND>_MODE=api|cli — transport selection.
    REVIEW_UNPAID_PROVIDERS=provider[,provider...] — skip disabled/unpaid providers early.
    REVIEW_IDLE_TIMEOUT_SECONDS=N       — override the review/panel subprocess idle window;
                                          0 disables idle reap and uses wall-clock --timeout.
    --timeout applies to every backend seat. For review/panel subprocess agent CLIs, backend
      stdout/stderr resets the idle timer; a fully silent seat is reaped after the idle
      window. Values under 60s stay exact for tests/probes; otherwise the normal 20m floor
      applies unless REVIEW_IDLE_TIMEOUT_SECONDS is set. QA and vision calls keep wall-clock
      timeout caps. An explicit `review just-ask --timeout N` is a hard wall-clock bound
      instead: seats still running N s after dispatch are killed (whole process group), the
      command prints each seat with why it gave no answer (timeout + last output, quota, no
      output, failed) and exits 1; if it is somehow still alive at N + {_api.COMMAND_TIMEOUT_GRACE_SECONDS} s,
      a watchdog prints the seats still running with their last activity, kills them and
      exits 124.
    REVIEW_HEARTBEAT_SECONDS=N          — interval between "still waiting" heartbeat
                                          lines printed to stderr once a backend call
                                          has SPAWNED (not while still queued on
                                          REVIEW_MAX_CONCURRENCY), for every STREAMED
                                          backend call (_run_streamed — review/panel
                                          seat dispatch) regardless of mode (default
                                          60s; a non-finite value, e.g. nan/inf, falls
                                          back to the default; ANY value <= 0, not just
                                          a literal 0, disables heartbeats entirely; a
                                          positive value below the 0.5s poll
                                          granularity clamps up to it).
    REVIEW_DIFF_MAX_BYTES=N             — max bytes of diff per review PASS (default 300000).
                                          `review diff` NEVER truncates: a diff over N is
                                          split at file boundaries (an oversized file at hunk
                                          boundaries) and the board reviews each part in its
                                          own pass inside one run, the per-seat results merged
                                          under "Part i/n" headings; a single hunk bigger
                                          than N fails loudly (exit 13) naming the file.
                                          brainstorm/quorum/just-ask put the diff in ONE
                                          prompt, so an over-N diff fails loudly (exit 13)
                                          instead. A diff piped on stdin is always sent
                                          whole. <= 0 never splits (sends the whole diff).
                                          See README.md's "Diff size cap" paragraph.
    REVIEW_STDIN_WAIT_SECONDS=N         — idle timeout for a non-tty stdin (default 5). A
                                          verb with no explicit diff source (`git diff |
                                          review diff`) reads stdin to EOF but gives up
                                          once it stays silent for N s (before the first
                                          byte or mid-stream): one stderr line, then the
                                          repo diff as usual. `--staged` / `--diff` never
                                          read stdin (a diff piped in anyway is ignored
                                          with a notice). 0 = only data already waiting.
    REVIEW_SEAT_COOLDOWN_SECONDS=N      — cross-invocation cooldown window for a
                                          chronically-unavailable claude seat (Fable).
                                          opencode also consults + records + clears
                                          this store for a TRUE-SILENCE trip
                                          (review-cli#235, see
                                          REVIEW_TRUE_SILENCE_SECONDS below), AND for
                                          the SAME shared administrative-sentinel/
                                          quota-marker phrases claude's own detection
                                          uses (codex review finding, round 18: an
                                          earlier version of this text wrongly said
                                          that detection "remains claude-only" — it
                                          does not, as of #243's round-12 fix).
                                          opencode-SPECIFIC quota wording that matches
                                          none of the shared phrases is still not
                                          recognised (falls through as a genuine
                                          success) — see review-cli#226.
                                          commandcode/zai HTTP backends have no
                                          cooldown consult/record/clear at all yet.
                                          Unset: the window
                                          ESCALATES per consecutive failure (10min,
                                          30min, 2h, then 8h cap), resetting to 10min
                                          after a success or a 24h quiet period. Set:
                                          that fixed window every time, no escalation;
                                          <= 0 disables cooldowns entirely.
    REVIEW_SEAT_COOLDOWN_FILE=PATH      — override the cooldown store location (default
                                          ~/.config/review-cli/seat-cooldown.json).
    REVIEW_TASKREF_VALIDATE=0           — skip checking --task CODE against the repo's tracker via the
                                          optional agenttools_taskref lib (shape check only; `0`, also
                                          `false`/`no`/`off`; any other value keeps it on). An invented
                                          code exits 2. Repos whose backend the lib lacks (forgejo)
                                          get the shape check only, with a note.
    REVIEW_HEALTH_PREFLIGHT=0           — skip the automatic pre-dispatch seat health check
                                          (`review help health`).
    REVIEW_HEALTH_TTL_SECONDS=N         — how long a successful probe answers for the pre-dispatch
                                          check (default 300; <= 0 disables the check).
    REVIEW_HEALTH_TIMEOUT_SECONDS=N     — per-seat probe timeout (default 5).
    REVIEW_TRIVIAL_DELTA_LINES=N        — pre-commit gate: max changed lines tolerated
                                          against the last reviewed baseline before a
                                          restage forces a fresh full review (default 10);
                                          0 disables the tolerance (exact-hash match only).
                                          Fails CLOSED regardless of line count whenever
                                          either diff contains a `Binary files ... differ`
                                          or `Subproject commit <sha>` line — binary /
                                          submodule-gitlink content is unmeasurable by a
                                          line-count ruler, so it always forces a fresh
                                          full review.
    (pre-commit gate)                   — a commit that finishes an in-progress merge, rebase
                                          (`rebase-merge/`) or cherry-pick skips the
                                          staged-review requirement, with the marker named on
                                          stderr. `git am` (`rebase-apply/`) does not skip.
                                          `review install-commit-hook --print-gate` prints the
                                          composer-stage gate body for rig; `review --version`
                                          prints the installed version.
    REVIEW_TRUE_SILENCE_SECONDS=N       — how many seconds of ZERO output an opencode
                                          seat gets before it is reaped as stuck rather
                                          than silently thinking (default 5min, per-model
                                          overridable in reviewlib/model_behavior.py's
                                          registry; review-cli#235). A trip records a
                                          REVIEW_SEAT_COOLDOWN_SECONDS-governed cooldown
                                          for that model. <= 0 disables the check
                                          entirely (mirrors REVIEW_IDLE_TIMEOUT_SECONDS=0).
                                          Only applies before the call's FIRST byte of
                                          output ever arrives — see review-cli#239 for
                                          the currently-uncovered mid-call-silence case.
                                          Before the first byte, this REPLACES
                                          REVIEW_IDLE_TIMEOUT_SECONDS as the sole reap
                                          authority (not a min() of the two) — an
                                          operator who tightens the idle timeout below
                                          this value does NOT get an earlier pre-output
                                          reap. There is no way to COMBINE the two for
                                          an earlier pre-first-byte cutoff: for any
                                          positive REVIEW_IDLE_TIMEOUT_SECONDS value,
                                          it has NO effect before the first byte, no
                                          matter what it is set to — the only lever
                                          pre-first-byte is lowering
                                          REVIEW_TRUE_SILENCE_SECONDS itself (codex
                                          review finding, review-cli#243 round 15: an
                                          earlier version of this text wrongly implied
                                          setting both env vars together could make a
                                          short idle floor also apply pre-output; it
                                          cannot — see review-cli#254 for the related
                                          deadline-clamp discussion). One EXCEPTION
                                          (codex review finding, round 18):
                                          REVIEW_IDLE_TIMEOUT_SECONDS=0 (the explicit
                                          "disable idle reap entirely" value) also
                                          disables true-silence — it is not just "no
                                          effect", it turns the check OFF, since the
                                          true-silence poll branch only runs at all
                                          when idle reap is enabled.
  codex / opencode / omp carry their own CLI auth (no key here). The Grok/xai board seat
    (GROK_SEAT) is one of these: opencode's own XAI_API_KEY env var, an inline
    options.apiKey in opencode.json, or `opencode providers login` (OAuth) all work.
    review-cli reads the local auth files to PROBE non-empty-ness at startup (deciding
    seat availability) but never logs or transmits the credential value anywhere —
    opencode is what actually sends it, at call time.
  AGENTIC board seats authenticate via the AGENT's own config, not a review-cli env var:
    Kimi/Qwen/DeepSeek (oc:commandcode/...) and Grok (oc:xai/...)
    all go through opencode's OWN provider config (`opencode auth login` /
    `opencode providers login`; ~/.config/opencode/opencode.json) — COMMANDCODE_API_KEY
    and ZAI_API_KEY above do NOT cover them; COMMANDCODE_API_KEY only gates the diff-only
    `-m cc` path. omp seats (omp:<provider>/<model>, incl. MIMO_SEAT
    omp:opencode-go/mimo-v2.6-pro and the GLM seat omp:zai/glm-5.3) authenticate via
    `omp setup` (`omp token <provider>` shows the stored credential); the startup probe
    reads omp's auth db read-only. Exception: omp:zai/* and omp:openrouter/* seats also
    run on ZAI_API_KEY (or ZHIPU_API_KEY) / OPENROUTER_API_KEY from the env or
    ~/.config/review-cli/.env, which review-cli hands the seat (review-cli#646), so
    `-m omp:zai/<model>` is an agentic GLM seat on the same key as `-m glm`.
    `review seats` lists every login it found per harness. Either way a missing binary
    or missing provider auth is caught at
    startup and the seat is skipped gracefully (pool backfills from reserve) rather than
    failing the run.
  z.ai / GLM Coding-Plan endpoint: `glm-5.3` (the flagship) answers on the
    Coding-Plan base URL https://api.z.ai/api/coding/paas/v4 (review's default); the
    standard https://api.z.ai/api/paas/v4 endpoint topped out at `glm-5.1`. Override with
    ZAI_BASE_URL for a standard-plan key; an explicit `zai:<model>` suffix wins over
    ZAI_MODEL, which alone only affects a bare `-m zai` invocation.
  OPENROUTER_API_KEY (an sk-or-v1-... token): diff-only `-m openrouter:<model>`
    aggregator backend (400+ models behind one key, api-only, no CLI mode). A bare
    `-m openrouter` uses OpenRouter's own auto-router, overridable with
    OPENROUTER_MODEL; base URL override is OPENROUTER_BASE_URL (default
    https://openrouter.ai/api/v1). The OPTIONAL OPENROUTER_HTTP_REFERER /
    OPENROUTER_X_TITLE env vars set OpenRouter's leaderboard-attribution headers
    (HTTP-Referer / X-Title) and never affect the review; a value containing a
    control character is silently dropped rather than sent.

HOST-WIDE ADMISSION (parallel reviews up to machine + provider capacity, review-cli#614)
  Every review run (diff/visual/brainstorm/just-ask/quorum/qa) takes a host-wide slot
  before its seats dispatch; there is no fixed slot count and no external lock to add
  (old `flock /tmp/review-cli.lock review ...` wrappers still work, they just serialize
  themselves). Do not hold a lock around `review`: the lock stays held for the whole
  queue wait, so every other caller waits behind that one holder's queue ETA, one after
  another, while the host has free capacity. The admission queue is the only host-wide
  gate. A run is admitted, oldest waiter first (FIFO), only while
    load average(1m) + estimates of runs admitted < ramp_seconds ago + this run's estimate
      <= (1 - headroom) x CPU count, and
    (MemTotal - MemAvailable) + the same estimates <= (1 - headroom) x MemTotal, and
    memory-stall pressure (Linux PSI `some avg10`, /proc/pressure/memory) <=
      max_memory_pressure percent (0 = no pressure check). Swap usage alone is not a
      signal (stale swap pages linger for hours); real shortage shows in MemAvailable/PSI.
  A run's estimate is base + per_seat x seats; per_seat is learned (p75 of the last
  learn_window runs' measured process-tree peak RSS / avg CPU) once 3+ runs exist, else
  the configured defaults. min_slots runs always start; max_slots (0 = none) caps them.
  Each seat call also holds a slot of its provider (anthropic, openai, zai, commandcode,
  xai, opencode-go, google, ...): at most floor(max_concurrent x (1 - headroom)) (>= 1)
  at once and, with rpm set, floor(rpm x (1 - headroom)) starts per minute. A 429 /
  overload reply lowers that provider's limit to floor(current x throttle_backoff) for
  throttle_cooldown_seconds; an exhausted quota (z.ai code 1310 "Weekly/Monthly Limit
  Exhausted" or code 1308 "Usage limit reached for 5 hour", each with "Your limit will
  reset at ...", a CLI "session limit" or "usage credits") is not a throttle and never
  lowers it. A seat still waiting after provider_wait_seconds (0 = wait forever) is skipped as
  unavailable, so failover moves to the next provider/reserve. A run is also held back
  until every provider it needs has room for its planned seats next to the seats of runs
  already running, so admitted runs rarely wait on a provider.
  Leases live in the registry dir as flock-held files: a killed holder is reclaimed at
  once, a lease whose heartbeat is older than stale_lease_seconds stops counting. The
  shared registry.lock is held only for each admission decision; a holder stopped or
  gone for over lock_stale_seconds has its lock replaced (logged loudly), and a waiter
  still blocked after lock_wait_seconds runs without host-wide admission. While
  queued, stderr shows `[review] admission: waiting — queue position P/N, ... ETA ~T`;
  queue time is excluded from the run's stats and from the internal backstop, but not
  from an explicit just-ask --timeout N: a run still queued at N exits 124 without
  starting a seat. A --visual companion is admitted against its full text panel, not
  just the vision call. `review jobs` (or `review jobs --slots [--json]`)
  and bare `review status` list running/waiting reviews, the budget and provider seats,
  plus the last 24h of admissions (admitted, queued, queue wait p50/p90/max, peak running
  at once) from admissions.jsonl in the registry dir.
  config.yaml `admission:` key / env var (env > config > default):
    enabled                   REVIEW_ADMISSION                        default {adm.enabled}
    headroom                  REVIEW_ADMISSION_HEADROOM               default {adm.headroom}
    min_slots                 REVIEW_ADMISSION_MIN_SLOTS              default {adm.min_slots}
    max_slots                 REVIEW_ADMISSION_MAX_SLOTS              default {adm.max_slots} (none)
    cpu_per_seat              REVIEW_ADMISSION_CPU_PER_SEAT           default {adm.cpu_per_seat} cores
    mem_per_seat_mb           REVIEW_ADMISSION_MEM_PER_SEAT_MB        default {adm.mem_per_seat_mb}
    cpu_base                  REVIEW_ADMISSION_CPU_BASE               default {adm.cpu_base} cores
    mem_base_mb               REVIEW_ADMISSION_MEM_BASE_MB            default {adm.mem_base_mb}
    learn                     REVIEW_ADMISSION_LEARN                  default {adm.learn}
    learn_window              REVIEW_ADMISSION_LEARN_WINDOW           default {adm.learn_window} runs
    ramp_seconds              REVIEW_ADMISSION_RAMP_SECONDS           default {adm.ramp_seconds}
    poll_seconds              REVIEW_ADMISSION_POLL_SECONDS           default {adm.poll_seconds}
    stale_lease_seconds       REVIEW_ADMISSION_STALE_SECONDS          default {adm.stale_lease_seconds}
    lock_wait_seconds         REVIEW_ADMISSION_LOCK_WAIT_SECONDS      default {adm.lock_wait_seconds:g}
    lock_stale_seconds        REVIEW_ADMISSION_LOCK_STALE_SECONDS     default {adm.lock_stale_seconds:g}
    provider_max_concurrent   REVIEW_ADMISSION_PROVIDER_MAX           default {adm.provider_max_concurrent} (0 = unlimited)
    provider_wait_seconds     REVIEW_ADMISSION_PROVIDER_WAIT_SECONDS  default {adm.provider_wait_seconds} (0 = wait forever; queue time never counts toward the backstop)
    throttle_backoff          REVIEW_ADMISSION_THROTTLE_BACKOFF       default {adm.throttle_backoff}
    throttle_cooldown_seconds REVIEW_ADMISSION_THROTTLE_COOLDOWN_SECONDS default {adm.throttle_cooldown_seconds}
    max_memory_pressure       REVIEW_ADMISSION_MAX_MEMORY_PRESSURE    default {adm.max_memory_pressure:g} (% PSI some avg10; 0 = off)
    providers                 REVIEW_ADMISSION_PROVIDERS="anthropic=12,zai=4/60" (name=max[/rpm])
    dir                       REVIEW_ADMISSION_DIR                    default $XDG_STATE_HOME/review-cli/admission
  Example:
    admission:
      headroom: 0.2
      max_slots: 12
      providers:
        anthropic: {{max_concurrent: 12}}
        zai: {{max_concurrent: 4, rpm: 60}}
  REVIEW_MAX_CONCURRENCY (default 4) is separate: it caps backend subprocesses INSIDE one
  run.

See also: `review --help` (overview), `review --show-board`, `review <mode> --help`.
"""


def _help_topic_task_check(_api, /) -> str:
    """The `review help task-check` deep reference (review-cli#552-PR3): the FULL
    `review task CODE --check [--json]` contract, field by field. Extracted out of the
    `--check` flag's own `help=` string (which stayed a summary + pointer here, matching
    the config/board topics' pattern) because the field-by-field breakdown alone is ~2KB
    -- inline it and `review task --help` buries `--detail`/`--min-iter` under it. Kept
    in sync with reviewlib.stats.quorum_check / `_finalize_quorum_result` (the actual
    payload builder) and `_quorum_check_subcommand` (the CLI wrapper that adds
    `identity_verification`) — a flag/behavior change there updates this topic in the
    same commit (help-docs-sync); `tests/test_run_stats.py`'s
    `test_check_help_text_names_every_json_contract_field_review_cli_552` asserts every
    field name `quorum_check` can actually emit is named somewhere below, so the two
    can't silently drift apart again."""
    return """\
review — `task CODE --check [--json]` contract reference
==========================================================

WHAT --check DECIDES
  Exit 0 iff CODE has enough PASSED recorded iterations across enough distinct board
  roles (self-merge-authority gate, default) or distinct models (when --min-models is
  explicitly given); see --min-iter/--min-models/--min-roles on `review task --help`.
  If the task has ZERO recorded role data at all AND every in-scope (non-mismatched)
  iteration predates role-tracking (PR #246), the default falls back to the old
  distinct-model count instead of a guaranteed fail (see quorum_mode_fallback below). A
  task with at least one iteration recorded AFTER role-tracking became possible but
  reviewed only via quorum/just-ask/brainstorm/qa does NOT get that fallback
  (review-cli#252) — it stays role-based and fails (see role_tracking_gap below). Counts
  only iterations whose run came back clean — a review that ran but failed/degraded does
  not count toward the bar, and pre-verdict-field history never satisfies it either
  (fail-closed).

--JSON FIELDS  (quorum_check()'s full result plus identity_verification — not just the
  printed ratio; a caller like `gh ship` should parse this, not the text-mode ratio line)

  ALWAYS present:
    task_code, passed_iterations, total_iterations, distinct_models_passed, models,
    min_iter, passed, identity_verification (ran / repo_only / disabled /
    skipped_unresolvable — see -C/--verify-repo-only/--no-verify-identity on
    `review task --help`).

  MODEL FLOOR — present whenever a model floor governs, explicitly OR via the
  zero-role-data fallback:
    min_models, min_models_source (explicit / fallback).
    min_models_advisory — ONLY when --min-models was given EXPLICITLY (never on the
    fallback path — the advisory text would be actively wrong there, since role-based
    coverage is exactly what's unavailable when the fallback fires).
    min_roles_suggestion — ONLY on an explicit, unmet --min-models run (--min-roles NOT
    given) whose PASSED iterations actually carry enough role data to have cleared the
    same floor via --min-roles instead — a non-blocking models->roles nudge, not a
    fallback/gap diagnostic (it never co-occurs with quorum_mode_fallback/
    role_tracking_gap below, since both of those require the bare role-based default,
    not an explicit --min-models run).

  ROLE MODE — present whenever role mode governs (the default):
    distinct_roles_passed, roles, min_roles.

  FALLBACK / GAP DIAGNOSTICS — mutually exclusive, present only when the matching case
  above fired (both require the bare role-based default -- neither can co-occur with an
  explicit --min-models run):
    quorum_mode_fallback — the role-based default fell back to model-counting.
    role_tracking_gap — it did NOT fall back and stays role-based, failing on its own
    merits (empty/short `roles`).

  STALLED SEATS — present only when the bar is NOT met:
    stalled_models — list of {model, reason, remaining_seconds,
    consecutive_failures} naming any attempted model currently cooling down.

  IDENTITY VERIFICATION — present only when identity_verification is "ran" or
  "repo_only" (a repo_id resolved; see -C/--verify-repo-only/--no-verify-identity).
  Repository ids are normalized remotes; a repository's former ids (a rename or a
  host move, e.g. sme-archiving-gc-tmp on Forgejo -> sme-archiving-gc on GitHub)
  count as the same repository:
    verified_iterations, unverifiable_iterations, excluded_mismatched_iterations,
    mismatch_details — list of {iteration, reason, recorded_repo_id,
    recorded_diff_files, ts} for excluded PASSED iterations, capped
    (mismatch_details_truncated when capped).
    excluded_foreign_repo_iterations, foreign_repo_iterations — list of {iteration,
    repo_id, task_code, ts, passed} for every iteration of ANY verdict recorded in
    another repository (none of them count toward iterations, models or roles),
    capped (foreign_repo_iterations_truncated when capped).

  NEAR-MISS TWIN KEY — present only when the bar is NOT met AND a same-digits
  different-'#' twin task code exists in the store (review-cli#389):
    searched_key, near_miss_keys, near_miss_counted, near_miss_ever_passed.

  error — present on every fail-closed shape: an invalid task code, an
  unreadable/missing store, zero recorded iterations, an explicit --min-iter /
  --min-models / --min-roles floor below 1, or a bar-not-met denial where some history
  was excluded as mismatched.

See also: `review task --help` (the flag surface), `review help config`.
"""


def _help_topic_health(_api, /) -> str:
    """The `review help health` deep reference (help-docs-sync): availability windows and
    the `review health` command. Keep in sync with `reviewlib/availability.py` and
    `reviewlib/health.py`."""
    from . import availability, health

    wide = ", ".join(sorted(availability.PROVIDER_WIDE))
    return f"""\
review health — which seats answer right now, and for how long a dead one stays out

  review health                         probe every seat of the active board (parallel, {health.DEFAULT_TIMEOUT_SECONDS:g}s each)
  review health --seat omp:zai/glm-5.3  probe one seat (repeatable); --preset NAME for a preset's board
  review health --json                  machine-readable report (schema "{health.SCHEMA}", version {health.SCHEMA_VERSION})
  review health --cached                let a probe newer than $REVIEW_HEALTH_TTL_SECONDS answer
  review health --no-probe              store + static checks only; calls no model
  review health --host home --host clvm run the same check on other machines over ssh (their
                                        own logins and stores; `--host local` includes this one)

Exit 0 when at least one seat is ok, 1 when none is (`--no-probe` always 0). Statuses: `ok` answered; `unavailable`
excluded (the reason and the end time are shown); `unknown` did not answer in time or failed
transiently — a slow CLI is not a dead one, so it is left alone.

AVAILABILITY WINDOWS (what an unavailable seat is excluded for, and until when)
  Every failed seat call is read for when the seat answers again, and recorded in
  ~/.config/review-cli/seat-cooldown.json (bucket `window`):
    quota        the reset time the error names — z.ai `429` code 1310 "will reset at
                 2026-10-26 05:58:34" (zone-less = UTC), the Claude CLI's "resets 7:30pm
                 (Europe/Moscow)" / "resets Oct 18, 5am", an epoch `x-ratelimit-reset`, or a
                 `Retry-After`; none named -> the escalating timer (10 min .. 8 h)
    credits      "insufficient balance", HTTP 402: 6 h, re-probed every 30 min (a top-up ends it)
    auth         HTTP 401, invalid_grant, "not logged in": until the credential file behind the
                 seat changes (~/.claude*/.credentials.json, ~/.codex/auth.json, omp's agent.db,
                 opencode's auth.json, ~/.config/review-cli/.env), re-probed hourly
    rate_limit   only when the error names a wait of 2 minutes or more
  A window of an hour or more is re-probed every quarter of its remainder (15 min .. 6 h): a
  top-up or a mis-read time zone ends it early. A quota that names its reset is account-wide for
  providers that bill one account ({wide}), so every seat on that provider is excluded
  (`provider:<name>` in the store); anthropic and openai are per seat (separate logins).

An excluded seat is skipped by board selection, admission planning (it holds no slot and no
provider reservation) and dispatch (a skip sentinel, no call). `review --show-board` lists it
as SKIPPED with the reason and `until`; `review jobs` shows an excluded provider as EXCLUDED
with its end time; `review seats` shows the cooldown. `REVIEW_SEAT_COOLDOWN_SECONDS=0` disables
the whole store (one-off escape hatch); delete an entry from seat-cooldown.json to end it by hand.

PRE-DISPATCH CHECK
  `review diff|quorum|brainstorm|just-ask` probe the seats the run may use before asking for
  a slot. A probe newer than $REVIEW_HEALTH_TTL_SECONDS (default {health.DEFAULT_TTL_SECONDS:g}s) is reused, so
  back-to-back reviews do not probe again. It prints one stderr line per seat found
  unavailable and blocks at most the probe timeout (${health._ENV_TIMEOUT}, default
  {health.DEFAULT_TIMEOUT_SECONDS:g}s; {health.AGENTIC_TIMEOUT_SECONDS:g}s for the CLI seats claude, codex, omp, opencode).
  Off with $REVIEW_HEALTH_PREFLIGHT=0.

A probe asks the seat's own backend "Reply with exactly: OK" over no diff. It takes no admission
slot and writes no review log or run-stats record.

See also: `review help config`, `review --show-board`, `review jobs`, `review seats`.
"""


def _help_topic_runtime(_api, /) -> str:
    """The `review help runtime` deep reference (help-docs-sync): run stats and the
    startup ETA line printed before a dispatch, plus why `review` must never be
    wrapped in a short external timeout. Content moved here from the README's
    "Quorum" section (review-cli#552 PR1); body prose em dashes were normalized to
    `--` for plain-text help — the title and the literal ETA example line keep the
    real `—` they use on-screen/in `eta_line()`'s actual output, so this stays a
    live, test-checked contract (tests/test_topic_help.py), not just relocated
    prose. The schema-version line below reads `STATS_VERSION` directly so it
    cannot silently go stale the way the README prose once did. Kept in sync with
    `reviewlib/stats.py`'s `record_run` / `announce_eta` / `eta_line` behavior — a
    flag/behavior change there updates this topic in the same commit."""
    return f"""\
review — runtime, run stats & the startup ETA
===============================================

NEVER SHORT-TIMEOUT `review`
  `review` is multi-model and (for the panel modes) multi-round, so it takes MINUTES
  per run. A short shell `timeout` wrapped around it kills the run before its
  synthesis -- a `brainstorm` only emits its final answer at the very end, so a
  short cap yields *nothing* usable.

RUN STATS & THE STARTUP ETA
  To make the expected duration visible up front, every run that actually dispatches
  a backend appends a structured stat record -- task code, mode, pool size (backends
  actually dispatched; for a small-panel brainstorm that is the per-round persona
  slot count), model NAMES (never keys or prompts), the real monotonic wall-clock,
  and ok/fail counts -- to an append-only JSONL store at
  ~/.config/review-cli/run-stats.jsonl (mode 0600; override with $REVIEW_STATS_FILE).

  At dispatch it then prints a one-line ETA to stderr keyed on (mode, pool_size):

    [review] pool=4 (brainstorm) — typically ~6m12s based on 12 past runs of this size; do NOT timeout.

  With no exact history it falls back to pool-size-only across modes, then to a
  "no history yet ... expect MINUTES" line. Read that line and wait at least that
  long. (This store is separate from the dashboard's per-call log reader, whose mode
  is INFERRED and whose duration is an mtime proxy -- this one records the run's
  ground truth.) Task-coded records power `review task CODE` and the dashboard's
  task history.

  The run-stats JSONL schema is versioned (currently v: {_api.STATS_VERSION}). v: 3 added
  an optional passed: bool verdict field (the mode's own success/failure signal, used
  by `review task CODE --check` -- the self-merge-authority gate); v: 4 added
  repo_id/diff_files/diff_sha256 (diff-identity binding); v: 5 added
  prompt_tokens/output_tokens (aggregate REST token usage). Task-coded records also
  add an optional task_code field. A record with no `passed` key has verdict UNKNOWN
  -- either it predates v: 3, or it is a current record from a mode with no verdict
  to thread (`qa` is report-only by design; see `reviewlib/qa/executor.py`) -- never
  crashes a reader, but the quorum gate treats unknown as not-passed either way.

RUN FEEDBACK & `review stat`
  Every new record also carries a run_id (16 hex chars, deterministic from ts/mode/
  task_code). A recorded run ends with a one-line stderr reminder naming it;
  REVIEW_NO_FEEDBACK_NUDGE=1 silences it for scripted/CI callers. Rate the run with:

    review feedback <run_id> [--useful yes|no|unsure] [--note TEXT] [--ticket ID] [--pr N] [--json]

  It writes (in one locked load-modify-write) into the same annotation store the
  dashboard reads (~/.config/review-cli/dashboard.json). A task_code that looks like a
  ticket is auto-linked. `review stat` joins run-stats.jsonl's mode/repo attribution:
  by_mode / by_repo_top sections, plus a FAIL%B column (bytes_fail/bytes_fail_pct in
  --json) -- the share of a harness's bytes spent on calls that failed anyway.
  `review stat --json` shapes: by_mode = [{{mode, runs, duration_seconds, ok_count,
  fail_count, avg_pool_size, fail_rate: number|null}}] (busiest by duration first);
  by_repo_top = [{{repo_id, runs, duration_seconds, fail_count}}] (busiest by runs
  first; only records with a repo_id, a moved repo counted under its current id).
  `review feedback <run_id>` with no write flag is a read; --useful also accepts
  y/n/true/false/unknown.

See also: `review --help` (overview), README's "Quorum" section, `review task CODE`.
"""


def _help_topic_capacity(_api, /) -> str:
    """The `review help capacity` deep reference (help-docs-sync): the
    `review capacity-check` CLI/JSON contract and its evidence predicate (review-cli#472).
    Keep in sync with `reviewlib/capacity_check.py`."""
    from .capacity_check import (
        DEFAULT_MAX_AGE_SECONDS,
        REASONS,
        SCHEMA,
        SCHEMA_VERSION,
    )

    reasons = "\n".join(
        f"    {reason:<28} -> {status}" for reason, status in REASONS.items()
    )
    return f"""\
review capacity-check — is the active review board's whole capacity out of quota?

  review capacity-check                  text report, exit 1 only if the board can't resolve
  review capacity-check --json           JSON report, always exit 0
  review capacity-check --preset heavy   evaluate the board `review diff --preset heavy` runs

`gh ship` (agent-tools ci/ship/ship.sh) skips its manual review-hatch prompt only when
`--json` says `"all_exhausted": true`. Nothing else in the report authorizes anything.

WHAT IS EVALUATED
  The board a no-`-m` `review diff` would run: --preset > config `models:` > config
  `board:` > the default preset. Every seat counts (pool and reserve), each seat model
  expands to the routes its provider failover dispatches (Claude CLI <-> omp twins
  included), a Claude CLI route expands to one candidate per account it rotates
  through, and an omp seat on omp's Anthropic accounts to one per account its pool may
  dispatch on (omp's enabled Anthropic logins minus the `omp_accounts:` policy's
  excluded ones; no agent.db: the route itself). A candidate is one
  (route, account).

THE PREDICATE
  all_exhausted = at least one candidate AND every candidate has status `exhausted`.
  A candidate is `exhausted` only on one of:
    - a harness-cli limit card for ITS quota identity (the Claude or omp Anthropic
      account's own card, matched by email; the codex card) with a 5-hour/weekly
      window at >= 100% that has not reset, read within the freshness window;
    - an active seat cooldown whose typed cause is `quota` (a session-limit /
      usage-credits failure; a timer cooldown or an availability window), recorded
      within the freshness window, on a route whose quota identity is the route itself
      or its only account, or under one omp pool account's own `omp-account:` key.
      A window is read under the route and, for a provider-wide exhaustion,
      `provider:<name>` (z.ai, Gemini, ...).
  Checked in this order, first decisive answer wins: backend not runnable
  (unavailable) -> the omp account policy leaves an omp Anthropic seat no account
  (unavailable) -> the account's recorded login failure (unavailable, cause auth) ->
  an active cooldown with a non-quota cause (unavailable) -> harness card (exhausted /
  usable) -> quota cooldown (exhausted) -> unknown.
  Freshness window: {DEFAULT_MAX_AGE_SECONDS}s, override with
  $REVIEW_CAPACITY_MAX_AGE_SECONDS.

  Statuses are separate: `usable` needs a card showing usage below 100% for that
  identity; no evidence is `unknown`, never usable. Typed reasons:
{reasons}

JSON (schema "{SCHEMA}", schema_version {SCHEMA_VERSION})
  schema, schema_version   identify this contract; a consumer refuses other versions
  checked_at               when the report was built (UTC ISO-8601)
  max_age_seconds          the freshness window applied
  configuration            source ("level:routine", "config.yaml (models:)", ...), preset,
                           config_path, config_present, board_fingerprint
                           ("sha256:<16 hex>" over the seats), seats [{{model, role,
                           display, effort}}]; null when the board failed to resolve
  sources                  harness_limits {{status ok|missing|failed|malformed, origin,
                           observed_at, cards, detail}}, seat_cooldown {{status
                           ok|missing|unreadable|malformed, path}}, claude_accounts
                           {{registry, accounts}}
  candidates[]             seat_model, route, backend, access_method, account,
                           quota_identity, quota_identity_basis (account|route), status,
                           reason, cause, source, observed_at (when the evidence was
                           observed), age_seconds, detail
  coverage                 candidates, exhausted, usable, unavailable, unknown, complete
                           (no unknown candidate)
  models, exhausted[]      schema-1 fields kept: distinct seat models; one {{model,
                           source, evidence}} per exhausted candidate
  all_exhausted            the predicate above
  error                    board-resolution error, else null

COMPATIBILITY
  Schema 1 (the unreleased first draft of this command that agent-tools ship.sh was
  written against) had no schema/schema_version and listed every model without
  exhaustion evidence under `available`. That key is gone:
  missing evidence is not availability. A schema-1 consumer reading only
  `all_exhausted` and `exhausted[].model/.evidence/.source` (ship.sh) keeps working, and
  only ever gets a stricter `all_exhausted`.

SOURCE LIMITS (each makes the affected candidates `unknown`, never exhausted)
  - Only Claude Code (CLI) and Codex have quota data (harness-cli cards). opencode,
    omp and the Claude API transport are covered only by a quota cooldown a real call
    on that route recorded. z.ai, Gemini, OpenRouter and CommandCode record no timer
    cooldowns; their only evidence path is an availability window (a quota/credits
    failure naming its reset), read under the route or `provider:<name>`.
  - The cooldown store keys routes, not accounts, so a quota cooldown on a Claude CLI
    route that rotates through several accounts is not attributed to any of them.
  - A cooldown is recorded when the failing call returns; later runs skip the seat
    without re-recording, so it goes stale after the freshness window even if the
    quota is still out.
  - Cooldowns recorded before review-cli#472 carry no typed cause and never count.
  - tg-ctl usage samples carry no account and are not used.
  - omp's own account pool windows (`omp usage`) are not read; an omp Anthropic account
    is judged by its own quota cooldown or its Claude Code card (same email). With
    several pool accounts the route's quota cooldown or window names none of them and is
    not read as quota evidence; a non-quota record on the route (auth, stall, unavailable)
    blocks every account and makes each one `unavailable`.

See also: `review status` (which seats can run now), `review entities`.
"""
