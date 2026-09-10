# Decision Record — Backend/model liveness behavior: adapter shape (review-cli#476)

**Status:** decided (design-only; implementation still gated — see "Implementation gate" below)
**Date:** 2026-09-10
**Method:** Haft workflow (h-frame → h-explore → h-compare → h-decide), executed manually in this
session. **Honesty note on tooling:** `review-cli` was not yet onboarded into Haft v7
(`.haft/` absent). A full `h-onboard` pass (TargetSystemSpec → EnablingSystemSpec → TermMap,
per `skill://h-onboard`) requires interactive per-phase human answers from Alex at ~9 gated
phases and could not be completed unattended in this session. `haft init` plus this decision
record apply the SAME rigor h-frame/h-explore/h-compare/h-decide require (explicit problem
framing, 2 genuinely distinct variants, a fair comparison table, a decision with rationale)
without the full project-onboarding ceremony, which is out of scope for one ticket's redesign.

## Why this record exists

Alex, on review-cli#476 (2026-09-10, verbatim):

> Очень невнятно написанная задача куча какого-то непонятного. А
> _opencode_model_needs_stall_watchdog явный признак того что архитектура абсолютно
> некорректно и требует полный переделки. Когда у нас всё через адаптеры не может
> существовать такой константы. Опять же нужно ныть неконкретный этот случай типа
> убрать конкретно эту константу а как игру по нормальному пересмотреть архитектуру
> сделать про это спеку, haft/superpowers у нас для кого?

His claim is not "this one constant is wrong" — it's that the constant's mere existence, as a
bespoke one-off function living OUTSIDE any registry, is evidence #476 (as written before this
record) never actually decided an architecture; it sketched one BackendProfile shape in one
pass and never asked "does this shape actually retire the smoking gun, and is it the only
candidate shape?". This record answers both questions explicitly.

## Problem frame (h-frame)

**Signal:** `reviewlib/backends.py::_opencode_model_needs_stall_watchdog` (line 741) hardcodes,
in a standalone function with its own env var (`REVIEW_OPENCODE_STALL_MODELS`) and its own
default substring tuple (`("zai/glm",)`), a decision that conceptually belongs to the SAME
question `reviewlib/model_behavior.py`'s `ModelBehavior`/`_REGISTRY` already answers for
`true_silence_timeout_seconds`/`is_known_silent_thinker`: "what does review-cli believe about
this model's output behavior?" Two independent per-model behavior stores for the same class of
question is the actual defect — not the specific substring `"zai/glm"`.

**Constraints (hard):**
- Whatever is decided here does not change review-cli's runtime behavior for any currently
  unlisted model (byte-identical default path).
- Must not duplicate the 3-layer architecture #476 already cites and must not re-solve #474
  (kwarg wiring, already landed) or #472 (fleet capacity check, unrelated).
- Must not regress the 4 currently-wired `true_silence_timeout` call sites (`review_opencode`,
  `review_omp`, `review_codex`, `review_claude_cli`(+images)) or opencode's existing stall/
  cooldown behavior (`tests/test_opencode_realrepo.py`, `tests/test_true_silence_cooldown_wiring.py`,
  `tests/test_true_silence_backend_wiring.py`, `tests/test_true_silence_hang_regression.py`,
  `tests/test_model_behavior.py`).
- Independent of this decision: **implementation stays blocked.** #476's own "DESIGN-ONLY
  GATE" acceptance criterion requires #472 to resolve AND Alex's explicit go-ahead before any
  implementation PR opens. That gate is a process precondition Alex wrote himself, not an
  architectural ambiguity this record can or should override.

**Optimization targets (≤3):**
1. Retire `_opencode_model_needs_stall_watchdog` as a *category* (no bespoke one-off matcher
   function survives outside a registry), not merely rename/relocate the one constant.
2. Minimize call sites touched and net-new abstraction for the concrete, EXISTING need (one
   model, one liveness-mechanism kind: CPU-poll vs true-silence).

**Observation, not optimization (real, but not this decision's job to size):** Phase 3/4 of
#476 (subscription/account-health signal, fallback-first orchestration) will need richer
per-model data than a bool+int. This record decides what ships for the smoking gun; it does
not pre-decide Phase 3/4's shape.

**Acceptance:** a single named data shape for "how one model's liveness behavior is declared,"
with `_opencode_model_needs_stall_watchdog` demonstrably reducible to a registry lookup against
that shape, decided against a real alternative, not asserted from one sketch.

**Blast radius:** `reviewlib/backends.py` (opencode stall-retry path only), `reviewlib/
model_behavior.py`. `reviewlib/process.py::_run_streamed`'s signature is untouched by the
decided variant (see below) — this is itself one of the two variants' distinguishing costs.

**Reversibility:** high. Both candidate variants are additive/data-shaped; no behavior change
for any unlisted model either way.

**Mode:** standard — 2 genuinely different shapes existed, needed comparison, not a coin flip.

## Variants explored (h-explore)

### Variant A — Grow the existing `ModelBehavior` dataclass and `_REGISTRY`

Add two fields to `model_behavior.ModelBehavior` (`needs_stall_watchdog: bool = False`,
`stall_seconds: int | None = None`), add one row to `_REGISTRY` for `"oc:zai/glm-5.2"`, add
one new lookup function pair (`needs_stall_watchdog(model)`, `stall_seconds(model)`) mirroring
the existing `is_known_silent_thinker`/`true_silence_timeout_seconds` shape byte-for-byte
(same env-override precedence, same `<=0`-disables convention). Delete
`_opencode_model_needs_stall_watchdog`, `_opencode_stall_seconds`,
`_OPENCODE_DEFAULT_STALL_MODEL_SUBSTRINGS`, `_OPENCODE_STALL_MODELS_ENV`,
`_OPENCODE_STALL_SECONDS_ENV`, `_OPENCODE_DEFAULT_STALL_SECONDS` from `backends.py` entirely;
their logic moves verbatim into the two new `model_behavior.py` functions.
`_run_opencode_with_stall_retry` changes its 4-line stall-seconds computation to call
`model_behavior.needs_stall_watchdog`/`model_behavior.stall_seconds` instead of the
now-deleted local functions — its control flow (branch to CPU-poll vs true-silence) is
UNCHANGED, only the data source moves.

### Variant B — New `BackendProfile` adapter class (#476's pre-existing pseudocode, as written)

A new `reviewlib/backend_profiles.py` module: `BackendProfile` dataclass with `streaming: bool`,
`liveness_signal: LivenessSignal` (`TOKEN_FLOW | PROCESS_CPU_POLL | API_STATUS_POLL | NONE`),
`subscription_check: SubscriptionCheck | None`, `true_silence_seconds`, `idle_seconds`; a
`LivenessState` enum (`DEAD | STALLED | PROGRESSING`) and `LivenessProbe` protocol extracted
from `_run_streamed`'s existing token-flow tracking and `_opencode_call_stalled`'s CPU-poll
logic; `profile_for(harness, provider, model, account)` most-specific-key lookup; `_run_streamed`
gains a `backend_profile: BackendProfile | None` kwarg it reads instead of separately-computed
`true_silence_timeout`/`liveness_timeout` kwargs; `model_behavior.py`'s registry is folded into
`_REGISTRY` wholesale; `review_opencode` (the sharpest test — it exercises both true-silence AND
stall-watchdog AND cooldown recording) is cut over first.

## Comparison (h-compare)

| Axis | Variant A (extend `ModelBehavior`) | Variant B (`BackendProfile` adapter) |
|---|---|---|
| **Call sites changed today** | 1 real call site (`_run_opencode_with_stall_retry`'s stall-seconds computation); `model_behavior.py` grows 2 fields + 2 functions, no new file. | ≥6: `_run_streamed`'s signature (`process.py`, a load-bearing function called from every backend), plus every one of the 4 already-wired `true_silence_timeout` call sites, plus the opencode stall-retry call site, plus whatever computes `(harness, provider, account)` for `profile_for()`. #476's own text already splits this into two SEPARATE phases (0 and 2) specifically because it is not a small lift. |
| **3rd hypothetical special-case model (e.g. a 2nd opencode route that also 0%-CPU-hangs)** | One new `_REGISTRY` row: `needs_stall_watchdog=True, stall_seconds=N`. Zero new code — literally today's mechanism, unchanged, satisfies Alex's "просто через конфиг" ask immediately. | Also one new registry row — but ONLY after Phase 0 (`LivenessState`/`LivenessProbe` extraction) and Phase 1 (`BackendProfile`/`profile_for`) and Phase 2 (`_run_streamed` cutover) have ALL landed first. Same end-state benefit, much later availability. |
| **Test surface** | Extends `tests/test_model_behavior.py`'s existing table-driven style with 2 new fields/functions; no new test file. | New test files for `LivenessProbe`, `BackendProfile`, `profile_for()` precedence, PLUS a byte-identical-behavior regression suite for the `_run_streamed` cutover — #476's own phase breakdown already lists 3 separate test deliverables (Phase 0/1/2) for this alone. |
| **Migration risk** | Low — one dataclass grows 2 optional fields (default `False`/`None`, so every existing `_REGISTRY` entry and `_DEFAULT_BEHAVIOR` stays valid unchanged); `_run_opencode_with_stall_retry`'s control flow is untouched, only its two input calls are swapped. | Higher — `_run_streamed` is called from every `review_*` backend; `API_STATUS_POLL`/`NONE` liveness-signal variants have **zero current implementation to extract from** (only `TOKEN_FLOW` and `PROCESS_CPU_POLL` exist today), so part of the new enum is speculative code built ahead of a caller, which is exactly the kind of premature-abstraction Alex's complaint (and this repo's own `yagni-kiss-dry`/`smallest-change` conventions) argues against building without a concrete second consumer. |

## Decision (h-decide)

**Chosen: Variant A**, for the concrete fix this ticket's smoking gun demands, right now.

**Rationale:**
1. It satisfies Alex's literal requirement — "when everything goes through adapters, such a
   constant cannot exist" — because `model_behavior.ModelBehavior`/`_REGISTRY` genuinely
   already IS the per-model adapter for behavior data; `needs_stall_watchdog`/`stall_seconds`
   become two more fields on that adapter, not a second parallel mechanism living beside it.
   After this change, zero bespoke per-model matcher functions remain outside the registry.
2. Variant B's extra machinery (`LivenessState`, `LivenessProbe`, `API_STATUS_POLL`,
   `SubscriptionCheck`) is real future work (Phase 3/4 of #476 will need account-health and
   fallback-orchestration data a bool+int cannot carry) — but building it NOW, before Phase 3
   has a concrete second liveness-signal kind to extract a protocol from, is exactly the kind
   of "нужно ныть неконкретный" speculative architecture Alex is objecting to, just in the
   other direction (over-built instead of under-decided).
3. Variant A does not foreclose Variant B. When Phase 3 (account-health) actually lands and
   needs a richer per-(harness,provider,model,account) shape than `ModelBehavior` can express,
   promoting `ModelBehavior` into a fuller profile (or wrapping it in the `BackendProfile` shape
   this ticket already sketches) is a new, then-well-motivated decision — not one to pre-make
   today against a hypothetical.

**What this decides:** the exact shape `_opencode_model_needs_stall_watchdog` is retired into
(on paper — see gate below), and that #476's Phase 0/1/2 (as previously drafted) is REPLACED by
one smaller phase. It does NOT decide Phase 3/4/5's eventual data shape — those remain future
decisions, explicitly deferred, not silently assumed.

## Implementation gate (unchanged by this decision)

review-cli#476's own acceptance criteria still require: (1) review-cli#472 (fleet-wide capacity
crisis) to resolve, AND (2) Alex's explicit go-ahead, before any implementation PR opens against
this ticket — for ANY phase, including the now-decided Phase 1. As of 2026-09-10, #472 is still
OPEN. This decision record is therefore design-only: it names the shape so the eventual
implementation PR has zero architectural ambiguity left to resolve, but does not itself
implement anything.
