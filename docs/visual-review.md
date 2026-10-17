# `review visual` — visual verification

**Give it a screenshot; it judges keep / rollback / repair.** `--visual` is image-only
visual verification: pixels in → verdict out. There is **no DOM, no page, no capture** —
the image arrives as a CLI argument (or on a hook's stdin) already rendered. Every check,
including "is this render unstyled or broken", is performed from the **image** — pixel-level
CV heuristics plus an AI-vision model looking at the picture — never by reading a stylesheet.

The pipeline is:

```
cvGate → local cache pre-classifier → AI-vision → policy engine
```

The **model is the witness**; the **deterministic policy engine is the judge**. cvGate is a
fast pixel pre-filter that auto-rejects the unambiguously-broken set (blank/FOUC canvas,
unstyled no-CSS render, error overlay) before any paid call. The local pre-classifier is an
on-device, no-VLM cost-saver that clears the confident-clear cases for free; AI-vision is the
primary judge for everything ambiguous; the policy engine decides the final verdict **outside**
the model (schema validation, CV/model contradiction checks, module vetoes). AI-vision never
self-decides and the local model never overrides it.

![review --visual cases — REPORTS-unstyled (top) vs no-report-styled (bottom)](assets/visual-cases.png)

*What `review visual <image>` reports across real renders. Top row: unstyled / blank / FOUC /
error-overlay renders the detector flags (each one would block a `tg --photo` send). Bottom row:
properly-styled renders it stays quiet on. (The grid's title art is the tool's old standalone
name "styleprobe" in older copies — it is the `--visual` detector.)*

### Standalone and Composable

`review visual <image>` is the canonical standalone verdict pipeline. Add `--diff` or
`--staged` plus `--task CODE` when you want the screenshot judged together with the current
diff. `--visual` remains a composable flag on text modes (`brainstorm` / `quorum` / `just-ask` /
`diff`), so personas / voters / reviewers literally **see** the image as multimodal context:

```bash
# Standalone — pure verdict pipeline on one render (no diff present)
review visual after.png

# Standalone visual plus the working-tree diff as context
review visual after.png --task HYP-742 --diff

# The brainstorm personas see the screenshot and reason about it
review brainstorm "is this layout good?" --task HYP-742 --visual after.png

# Every quorum voter gets the image as shared context
review quorum "ship this UI?" --task HYP-742 --visual after.png

# Diff review with the rendered result attached as evidence
review diff --task HYP-742 --visual after.png
```

When a companion mode is present, Claude CLI seats can receive the raw screenshot
attachment, and every seat receives the grounded visual observation from a companion vision
fan-out. The standalone verdict pipeline (and its exit codes below) fires only in the
mode-less case. If that companion visual fan-out cannot produce a usable grounded
observation, review blocks the companion text mode instead of returning a normal answer that
merely describes cvGate signals. Use `--no-ai` only when you explicitly want the offline
CV-only path.

### Vision backends

Vision runs **through the agent CLIs** — `codex` / `claude` / `opencode` — mirroring exactly
how review's text backends shell out, but with the image attached and the structured verdict
parsed from the CLI output. No provider REST keys for those three. **Gemini is the one
exception**: its CLI is broken, so the Gemini vision call stays on the REST API key
(`GEMINI_API_KEY`), same as review's text Gemini backend. Vision requires a vision-capable
model on whatever backend you pick — review never silently uses a text-only model to "verify"
an image. (For router backends like opencode, that means selecting a vision model explicitly,
e.g. `oc:<provider>/<vision-model>`.) Visual defaults are separate from the text reviewer
board: Opus is tried first, then GROK_SEAT (`oc:xai/grok-4.7`), then vision-capable
fallbacks including GLM vision (`oc:zai/glm-4.5v`). If Opus is unavailable at runtime,
review skips to the next visual backend instead of failing while a fallback exists.
Override with `visual_models:` in
config or `-m` on the command. `--no-local-model` disables the local cache pre-classifier (the
cost-saver) and forces every cvGate pass-through to the paid AI-vision call.

### Modules

Each visual check is an independent, self-selecting **module** that declares *when it
activates*. Built-ins: `style-presence`, `blank-frame`, `error-overlay`, `error-text`. A module contributes a
cheap `cv_check` pre-filter and/or `vision_questions` folded into the multimodal call, plus a
`judge` that can veto a keep. Force-activate one with `--check <name>`; otherwise modules
self-select from `--intent` / `--expect`.

**Per-project modules.** A project ships its own modules via a manifest at
`<project>/.review/visual-modules.json` declaring `{name, entry, activates_on}`. review
discovers, loads, and folds them into the same pipeline. The model is **trust-by-default** —
reviewing your *own* repo, a contributed module loads and runs with zero ceremony. For the rare
untrusted-repo case (an external PR, a cloned stranger's repo) set `REVIEW_UNTRUSTED_MODULES=1`
to re-engage a TOFU quarantine + sha-pin (`review trust-module <name>` to pin). Every load is
recorded to an append-only audit log either way.

The worked example is **`selection-highlight`** (shipped by HyperIDE / hyper-canvas-draft):
`activates_on: ["selection"]`. A HyperCanvas selection frame is a 2px `rgb(59,130,246)` outline
so thin a vision model routinely can't tell whether it's there — so "selection works" proofs
slip through with no frame drawn. The module reuses `bin/frames-check`'s deterministic
colour+shape detector to turn "selection expected but no outline present" into a **hard veto**,
short-circuiting before any vision call.

### `tg --photo` hook

`tg` can run `review visual` as a **pre-send hook** to block an unstyled / broken screenshot
before it reaches Telegram — turning the often-violated "review screenshots before sending" rule
into an enforced mechanism. The hook runs `review visual <png> --json --strict`; a `rollback`
verdict (exit 10) drops the photo, a `keep` lets it through, and no-vision
`human_review` / `unverified` also fail closed under `--strict` so an unverified screenshot
does not silently pass the send gate. See the
`feat-tg-photo-visual-hook` branch and [architecture-visual-verification.md](architecture-visual-verification.md) §7.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | `keep` — the render is acceptable |
| `10` | blocking verdict (`rollback`, or `human_review`/`unverified` when treated as a block) under `--strict` |
| `1` | usage error — no image / unreadable image |
| `124` | vision-call timeout |

Add `--no-ai` to run cvGate only (fast CI smoke / offline), `--json` for the machine verdict
(used by the tg hook), and `--before <img>` for diff-aware judgement.

---

[← Back to README](../README.md)
