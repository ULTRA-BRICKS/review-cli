"""Config + model selection: defaults, aliases, ~/.config/review-cli/config.yaml.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). The reviewer-board layer (role lenses +
`board:` config, HYP-741) is additive: with no `board:` in config.yaml the legacy
DEFAULT_MODELS panel is untouched.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from pathlib import Path

from .portable import O_NOFOLLOW, O_NONBLOCK
from .usage_limits import DEFAULT_LIMIT_THRESHOLD

DEFAULT_PROMPT = (
    "Review this uncommitted git diff for bugs, regressions, security issues, and "
    "missing tests. Before concluding the diff is fine, actively try to find a way it "
    "is WRONG: walk through concrete failure scenarios (edge cases, concurrent access, "
    "malformed or adversarial input, a caller/integration point you haven't traced) and "
    "genuinely attempt to break the change, rather than skimming it for obviously bad "
    "code. Only report 'no issues' after you have tried specific scenarios and failed "
    "to break it — a skim is not a review.\n\n"
    "Every verdict must show that work, not just an opinion:\n"
    "- A FINDING must cite file:line and describe a concrete failure scenario — the "
    "input or state that triggers it and what goes wrong.\n"
    "- A CLEAN verdict (no findings) must explicitly state which specific failure "
    "modes you checked and why each is ruled out, e.g. 'checked: race condition on "
    "concurrent writes to X — none found, guarded by a lock at Y; checked: injection "
    "via the user-supplied Z — sanitized before use at W'. A bare 'looks good' or 'no "
    "issues found' with no such statement is not an acceptable answer.\n\n"
    "Return only actionable findings (or the clean-verdict statement above). Do not "
    "edit files."
)
# Canonical Kimi seat — the SINGLE source of truth for "the Kimi model the defaults use",
# referenced by BOTH the flat DEFAULT_MODELS panel below and the priority-4 seat of
# DEFAULT_BOARD. It routes through the commandcode gateway, NOT the old
# `oc:fireworks/.../kimi-k2p6-turbo` route, which ran on the suspended Fireworks `glide`
# account and is dead (ROADMAP §7, review-cli#25). One constant so the flat panel and the
# board can never drift back to the dead route — the staleness that motivated this fix
# existed precisely because the Kimi seat was spelled independently in two places and only
# the board was kept current (the flat panel rotted on the dead Fireworks route).
KIMI_SEAT = "commandcode:moonshotai/Kimi-K3"
SOL_SEAT = "codex:gpt-6.1-sol"
# GPT-6-Astra ("Astra") is OpenAI's new flagship codex model — "our most capable model
# for complex, demanding work" per the codex CLI's own model catalog. Explicitly PINNED
# (like SOL_SEAT), rather than left as a bare `"codex"` seat that silently tracks
# whatever ~/.codex/config.toml's own `model =` default happens to be today. The bare
# `"codex"` seat used to sit at DEFAULT_BOARD's priority 5 and, because that config
# default was then `gpt-5.6-sol`, it was accidentally running the SAME model as
# SOL_SEAT (priority 1) — a silent duplicate that gave the board zero extra model
# coverage for a full seat. Pinning to Astra here fixes that: the board gets a genuinely
# distinct model, and this seat no longer drifts if the operator's global codex default
# changes.
ASTRA_SEAT = "codex:gpt-6-astra"
# GPT-6-Luna ("Luna") — a THIRD, distinct codex model on the same already-paid
# OpenAI/Codex account as SOL_SEAT/ASTRA_SEAT. Live `performance` fallback for the
# disabled GLM_COMMANDCODE_SEAT. Pinned explicitly (not bare `"codex"`), same
# anti-duplicate reason as ASTRA_SEAT above. Added as TERRA_SEAT (`gpt-5.6-terra`) in
# review-cli#382; review-cli#645 moved it to Luna, GPT-6's low-cost tier, because the
# Terra line has no GPT-6 successor.
LUNA_SEAT = "codex:gpt-6-luna"
# claude-sonnet-5-5 ("Sonnet") — review-cli#382: a SECOND, distinct Anthropic model on the
# same already-paid Claude account as Opus/FABLE_SEAT. Live `quality` fallback for Kimi
# (commandcode) and the quota-fragile z.ai-GLM seat. `review stat` telemetry (2026-09-05):
# dispatched cleanly 7/7 times historically, zero recorded failures — unlike FABLE_SEAT's
# ~100% dispatch failure rate (see its own entry below).
SONNET_SEAT = "claude:claude-sonnet-5-5"
# OMP-hosted Claude ("Sonnet via omp") -- review-cli#168: the SAME claude-sonnet-5-5 model,
# dispatched through the omp harness (reviewlib.backends.review_omp) instead of the direct
# `claude` CLI -- a genuinely DIFFERENT auth path (omp's own ANTHROPIC_API_KEY/
# ANTHROPIC_OAUTH_TOKEN/profile-scoped agent dir, independent of Claude Code CLI's
# CLAUDE_CONFIG_DIR/keychain), so it survives an outage that takes out every claude:*
# seat's shared account state. Live `quality` fallback for both Kimi (commandcode) AND
# Sonnet (claude: direct) -- see ROLE_FALLBACK_MODELS / DEFAULT_BOARD.
OMP_CLAUDE_SEAT = "omp:anthropic/claude-sonnet-5-5"
# review-cli#fable-seat-reliability (GLM review finding): the single source of truth
# for the Fable seat's model id — every sibling seat (KIMI_SEAT/SOL_SEAT/
# GLM_COMMANDCODE_SEAT above) already has one; the Fable id was the one left as a
# repeated literal across DEFAULT_BOARD's entry, DEFAULT_PRESET_BOARD's exclusion set,
# and HEAVY_PRESET_BOARD's filter (three places that must never independently drift).
FABLE_SEAT = "claude:claude-fable-5-1"

# Canonical GLM-5.3-via-commandcode seat — the SINGLE source of truth for "GLM 5.3 routed
# through the Command Code gateway" (as opposed to the z.ai-subscription route used by the
# lower-priority `omp:zai/glm-5.3` seat). It is the priority-3 board seat, immediately after
# Opus (review-cli#fable-seat-reliability: was priority-4 before Fable's demotion moved
# every subsequent seat up one slot). The wire id is byte-exact against the commandcode gateway
# catalog (`zai-org/GLM-5.3`, from models.dev's commandcode listing — review-cli#645). This is DIFF-ONLY (a stateless keyed-
# HTTP POST through review_commandcode, like Gemini): opencode's `commandcode` provider does
# NOT register this model, so the agentic `oc:commandcode/zai-org/GLM-5.3` form errors — the
# diff-only route is the one that actually reaches it. Read-only by construction (it POSTs
# only the diff; no repo access, no tools, no exec), so it needs no `-s read-only` cage.
GLM_COMMANDCODE_SEAT = "commandcode:zai-org/GLM-5.3"

# Canonical Grok seat — opencode's NATIVE `xai` provider (oauth-authenticated via
# `opencode providers login`). models.dev's xai catalog lists `grok-4.7`. Already
# agentic (`oc:` prefix) — unlike Kimi/GLM-cc/Qwen/DeepSeek, there is no
# diff-only REST fallback because xai is not one of the commandcode/z.ai custom
# gateways `_agentic()` derives from; it is a first-class opencode provider, so
# the seat is spelled directly. Image→text capable: VISUAL_MODELS includes this
# id and `_VISION_MODEL_HINTS` matches the `grok-4.7` substring (not a generic
# `grok`, which would also match grok-3).
GROK_SEAT = "oc:xai/grok-4.7"

# Canonical MiMo seat — OpenCode Go subscription via omp's opencode-go provider
# (review-cli#450). The opencode-go selector is opencode-go/mimo-v2.6-pro (review-cli#645);
# agentic through review_omp. The opencode CLI catalog spelling
# `oc:opencode/mimo-v2.5-free` is NOT DEFAULT-safe (`default_routes_live` False:
# provider `opencode` under `oc:` is not a named backend token and is not in
# `_AGENTIC_ONLY_PROVIDERS`). Explicit `-m oc:opencode/mimo-v2.5-free` still works
# at runtime. Not on VISUAL_MODELS.
MIMO_SEAT = "omp:opencode-go/mimo-v2.6-pro"


def _agentic(seat: str) -> str:
    """Turn a diff-only keyed-HTTP seat (`provider:model`) into its AGENTIC opencode
    form (`oc:provider/model`), so the SAME model runs read-only INSIDE the repo and can
    read any project file — not just the diff in the prompt (review-cli#24).

    opencode registers `commandcode` and `zai` as custom OpenAI-compatible providers
    (`~/.config/opencode/opencode.json`), so the exact model ids the diff-only
    `review_commandcode` / `review_zai` REST backends POST are reachable agentically with
    no new wire id — only the transport changes. resolve_backend routes the `oc:` prefix
    to review_opencode; everything after `oc:` is opencode's `provider/model` selector.

    Deriving the board's agentic seats FROM the diff-only constants keeps ONE source of
    truth for each model id: the flat DEFAULT_MODELS panel (which has no failover/reserve,
    so it stays on the robust key-only commandcode route) and the board's agentic seat can
    never drift to different model ids, and neither can drift back to the dead Fireworks
    route (review-cli#25). The flip is transport-only — a single, reversible rewrite.

    Idempotent + canonical: a seat that is ALREADY agentic is returned in the CANONICAL
    `oc:` spelling — `oc:foo/bar` unchanged, and the `opencode:foo/bar` alias normalized to
    `oc:foo/bar`. Both spellings resolve to review_opencode and run the same
    `opencode -m foo/bar`, which the dashboard attributes to `oc:foo/bar`; canonicalizing
    here keeps the board seat id == the attributed id, so a seat can't split into a
    `no_data` board row plus a separate `oc:` health row (review-cli#24, codex review).
    Wrapping twice can never produce a nonsense `oc:oc/...` id."""
    if seat.startswith("oc:"):
        return seat
    if seat.startswith("opencode:"):
        return "oc:" + seat[len("opencode:") :]
    if ":" not in seat:
        return f"oc:{seat}"
    provider, model = seat.split(":", 1)
    return f"oc:{provider}/{model}"


# Code default keeps a self-sufficient panel (incl. opencode). Personal model
# preferences live in ~/.config/review-cli/config.yaml (keys: models,
# brainstorm_models) and override this — see load_config().
DEFAULT_MODELS = ("codex", "gemini", KIMI_SEAT, GROK_SEAT, MIMO_SEAT)
# Visual review uses a separate priority list from the text reviewer board. The visual
# pipeline selects the first reachable vision-capable backend, so a dead/paywalled Opus is
# skipped automatically and the next vision seat is promoted.
VISUAL_MODELS = (
    "claude:claude-opus-5-5",
    GROK_SEAT,
    "oc:zai/glm-4.5v",
    "oc:commandcode/moonshotai/Kimi-K3",
    "gemini",
)
# Friendly aliases for claude models, expanded in _split_models() and the
# default/config paths, so `-m fable` == `-m claude:claude-fable-5-1`. Generic aliases
# follow the current pin; version-numbered aliases keep naming their own release.
MODEL_ALIASES = {
    # opus is a MODEL, not a backend token — bare `opus` used to fall through
    # `_match_named_backend` to the opencode catch-all (`opencode -m opus`), whose agentic
    # backend infra-fails and blocks review quorums. Pin it to the reliable claude-direct
    # provider. (Provider-failover's DEFAULT_PROVIDER_CHAINS then adds opencode as the
    # FAILOVER alternate, not the default.)
    "opus": "claude:claude-opus-5-5",
    "opus55": "claude:claude-opus-5-5",
    "opus48": "claude:claude-opus-4-8",
    "fable": FABLE_SEAT,
    "fable51": FABLE_SEAT,
    "fable5": "claude:claude-fable-5",
    "sol": SOL_SEAT,
    "gpt61sol": SOL_SEAT,
    "gpt56sol": "codex:gpt-5.6-sol",
    # Astra falls through `_match_named_backend` to the opencode catch-all without this —
    # the same failure class documented on `opus` above. Pin it, like Sol.
    "astra": ASTRA_SEAT,
    "gpt6astra": ASTRA_SEAT,
    # Luna falls through the same opencode catch-all without this — same class as
    # opus/astra above. Pin it too (review-cli#382 pinned Terra, review-cli#645 Luna).
    "luna": LUNA_SEAT,
    "gpt6luna": LUNA_SEAT,
    "terra": "codex:gpt-5.6-terra",
    "gpt56terra": "codex:gpt-5.6-terra",
    "sonnet": SONNET_SEAT,
    "sonnet55": SONNET_SEAT,
    "sonnet5": "claude:claude-sonnet-5",
    "grok": GROK_SEAT,
    "grok47": GROK_SEAT,
    "grok-4.7": GROK_SEAT,
    # `grok46`/`grok45` are the Grok 4.6/4.5 spellings; they do not track GROK_SEAT.
    "grok46": "oc:xai/grok-4.6",
    "grok-4.6": "oc:xai/grok-4.6",
    "grok45": "oc:xai/grok-4.5",
    "mimo": MIMO_SEAT,
    "mimo26": MIMO_SEAT,
    "mimo25": "omp:opencode-go/mimo-v2.5",
    "mimo-2.5": "omp:opencode-go/mimo-v2.5",
    # z.ai (Zhipu / GLM) — OpenAI-compatible keyed HTTP backend. Bare `zai` resolves
    # directly in resolve_backend (env ZAI_MODEL / glm-5.3 default — the newest GLM,
    # reachable on the Coding-Plan endpoint). These aliases pin specific GLM model ids;
    # `glm`/`glm53` point at the newest (glm-5.3), the rest pin older releases.
    "glm": "zai:glm-5.3",
    "glm53": "zai:glm-5.3",
    "glm52": "zai:glm-5.2",
    "glm51": "zai:glm-5.1",
    "glm47": "zai:glm-4.7",
    "glm46": "zai:glm-4.6",
    "glm45": "zai:glm-4.5",
    # commandcode — Command Code's OpenAI-compatible Provider API (keyed HTTP).
    # `cc` is a short hand; the legacy `commoncode`/`common-code` spellings still
    # resolve via resolve_backend, so old configs keep working.
    "commandcode": "commandcode",
    "commoncode": "commandcode",
    "cc": "commandcode",
}
CONFIG_PATH = Path.home() / ".config" / "review-cli" / "config.yaml"

# Short default for the interactive multi-call modes; classic review keeps 1200s.
PANEL_TIMEOUT_DEFAULT = 240
# qa (the agent-as-tester mode) carve-out: a tester run boots a SUT and drives a whole
# suite with an un-caged agent — tens of minutes, NOT the 4-minute chat-panel default. It
# gets its own long per-run default (45 min) and still leans on the <=4h run backstop, so a
# real qa run is never cut off by the short panel cap (review-qa.md §3 timeout carve-out).
QA_TIMEOUT_DEFAULT = 2700
# Moderator priority for --quorum/--brainstorm. opus first now that the headless
# claude backend works reliably (deterministic workspace auto-trust); codex and
# gemini are the fallbacks. pick_moderators() filters this to available backends
# and run_moderator() walks it at run time, so a candidate that passes the cheap
# availability probe but dies at run time (e.g. an Anthropic-disabled model like
# fable) never leaves the panel without a synthesis.
MODERATOR_CANDIDATES = ("claude:claude-opus-5-5", "codex", "gemini")


def _expand_alias(model: str) -> str:
    token = MODEL_ALIASES.get(model.lower(), model)
    from .role_models import seat_for_model

    return seat_for_model(token)


def load_config() -> dict:
    """Read ~/.config/review-cli/config.yaml (keys: models, brainstorm_models, board).
    Returns {} if absent/unparseable so the code default always applies."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- Reviewer board (HYP-741) ---------------------------------------------------
# A "board" is a panel where each reviewer model carries its OWN ROLE/lens. The
# lens is a short instruction appended to DEFAULT_PROMPT so each model focuses on a
# distinct, non-overlapping facet of the diff — instead of every model doing the
# same generic pass. The board is built into the SAME default-review path (one
# PanelJob per reviewer, run in parallel); the opus-first moderator is unchanged.
#
# Role lenses: role id -> a tight sentence appended to the generic prompt. Kept
# non-overlapping so the panel covers the diff broadly without N duplicate reviews.
REVIEW_ROLES = {
    "architect": (
        "Focus specifically on ARCHITECTURE and DESIGN: design coherence, API shape, "
        "abstraction boundaries, separation of concerns, and whether the change fits "
        "the existing structure. Skip low-level style nits."
    ),
    "correctness": (
        "Focus specifically on CORRECTNESS: logic bugs, regressions, edge cases, "
        "null/undefined handling, async/await and race conditions, off-by-one errors, "
        "and incorrect control flow. Skip style and naming."
    ),
    "consistency": (
        "Focus specifically on CROSS-FILE CONSISTENCY: dead or dangling references, "
        "contract drift between caller and callee, mismatched signatures, and whole-repo "
        "coherence. Use the broad context to catch things a single-file view misses."
    ),
    "performance": (
        "Focus specifically on PERFORMANCE: algorithmic complexity, hot paths, "
        "unnecessary allocations, async/concurrency overhead, and N+1 query or loop "
        "patterns. Flag only changes with a real performance impact."
    ),
    "quality": (
        "Focus specifically on CODE QUALITY: readability, naming, duplication, "
        "code smells, and idiomatic use of the language/framework. Suggest concrete, "
        "actionable cleanups."
    ),
    "security": (
        "Focus specifically on SECURITY: injection, broken authn/authz, secret "
        "handling, unsafe deserialization, path traversal, and SSRF. Think like a "
        "security-paranoid reviewer: assume the worst actor and the worst case, and "
        "actively probe every input, trust boundary, and secret-handling path for a "
        "way to break it before concluding the change is safe. Flag exploitable "
        "issues, not theoretical ones."
    ),
    "tests": (
        "Focus specifically on TESTS: missing tests for new behavior, untested branches, "
        "boundary conditions, and error-path coverage. Bring a skeptical-SRE mindset: "
        "distrust anything without a failure plan — what breaks in production, under "
        "concurrent or adversarial load, or at 3am with no one watching? Point at the "
        "exact cases that should be tested but aren't."
    ),
    "contracts": (
        "Focus specifically on PUBLIC API SHAPE and CONTRACTS: exported function/type "
        "signatures, interface design, backward compatibility, breaking changes to "
        "callers, and whether new types/return values are coherent and future-proof. "
        "Skip internal-only refactors with no external surface."
    ),
}


# Per-project lens overrides (review-cli#547): the revmux `.revmux/` pattern applied to
# review-cli's board. A project can check in `.review/lenses/<role>.md` (role = one of
# the REVIEW_ROLES keys above) to teach the board something specific to ITS codebase (a
# payments invariant, a house style rule) without forking/patching this file. The
# override is resolved relative to the REVIEWED repo's root -- the `cwd` a board
# dispatch already carries (see `panel.build_board_job` / `run_board_with_failover`),
# NOT review-cli's own installation directory.
LENS_OVERRIDE_SUBDIR = Path(".review") / "lenses"
# A project override is meant to be a short addendum (a house rule, an invariant
# reminder), not a document -- capped well under the per-pass diff budget
# (`diff_chunks.DIFF_MAX_BYTES_DEFAULT`, 300_000) so a pathologically large file at this
# path can never balloon into a huge chunk of every matching seat's prompt.
LENS_OVERRIDE_MAX_BYTES = 8_192


def lens_override_path(role: str, cwd: Path) -> Path:
    """The checked-in override file a project can drop for `role` under `cwd` (the
    reviewed repo's root). Pure path arithmetic -- existence is NOT checked here, so a
    caller that only wants to LABEL a path (e.g. `--show-board`) doesn't pay for a read
    it isn't going to use."""
    return cwd / LENS_OVERRIDE_SUBDIR / f"{role}.md"


def lens_override_text(role: str, cwd: Path | None) -> str:
    """Read a project's checked-in lens-override file for `role`, or "" when there is
    none to apply.

    Every failure mode below collapses to "" by design, all silent/never-crashing -- a
    malformed or hostile override must never break a review the way a real
    `BoardConfigError` does; this is optional per-project seasoning, not board config:
      * `cwd` is None (no repo context to resolve a project-relative path against, e.g.
        a diff-only seat built with no real reviewed repo) or `role` isn't a real
        REVIEW_ROLES key (an unknown/typo'd role has no built-in lens to extend either
        -- see `BoardReviewer.role_lens`);
      * the file doesn't exist, isn't readable, or isn't valid UTF-8 (a typo'd override
        file is not a review-cli crash);
      * the resolved path escapes `cwd` -- either the leaf file itself is a SYMLINK, or
        an intermediate component (`.review` or `.review/lenses`) is (review-cli#547
        security review finding). `Path.read_text()` follows symlinks unconditionally;
        a git-tracked symlink at this exact path pointing OUTSIDE the repo (e.g.
        `.review/lenses/security.md -> ~/.ssh/id_rsa` or `../../.aws/credentials`)
        would have THIS PROCESS (not a sandboxed agentic seat) read an arbitrary host
        file and ship its bytes to every seat with that role -- including diff-only
        HTTP backends (gemini/commandcode/zai) that otherwise have NO filesystem access
        at all, a brand-new exfiltration channel a cloned repo should never get. Checked
        with `Path.resolve()` + `is_relative_to(cwd.resolve())` up front (catches an
        escaping symlink ANYWHERE in the path, not just the leaf), and the leaf is then
        opened with `O_NOFOLLOW` (belt-and-suspenders against a TOCTOU swap between the
        resolve check and the open) and `O_NONBLOCK` (a FIFO planted at this path would
        otherwise hang a blocking open forever) -- the same pattern `jobs.py`'s
        `_read_fallback_pointer` already uses for a locally predictable path; `S_ISREG`
        rejects anything that slips through as a directory/device/socket;
      * the file is LARGER than `LENS_OVERRIDE_MAX_BYTES` -- read is capped at the
        limit (never loaded whole; `MemoryError` on a pathological file is not an
        `OSError` and would otherwise violate this function's own never-crash
        contract), and the content is silently truncated to that many bytes rather
        than rejected outright (a project override is a short addendum, not a
        payload-sized document, so truncation is the safe conservative behavior, not a
        rejection a project author would need to debug);
      * the file's content is EMPTY or whitespace-only after stripping. Blank-means-
        "no override" (not "override with an empty string") is the safer default here,
        matching how most config-override patterns treat a blank value -- this helper is
        EXTEND-only (see `BoardReviewer.resolved_lens`), so there is no way to write an
        override that deliberately SUPPRESSES the built-in lens; an accidentally-emptied
        file falling back to "no override" (full built-in lens) is strictly safer than
        treating it as "override with nothing," which would look identical to a no-op
        and give the project author no signal that their file stopped doing anything."""
    if cwd is None or not role or role not in REVIEW_ROLES:
        return ""
    path = lens_override_path(role, cwd)
    try:
        resolved_cwd = cwd.resolve()
        if not path.resolve(strict=False).is_relative_to(resolved_cwd):
            return ""
    except OSError:
        return ""
    try:
        fd = os.open(str(path), os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    except OSError:
        return ""
    try:
        # `os.fdopen` takes ownership of `fd` immediately, so every exit from this
        # `with` block (including the early `return` below) closes it -- no leaked
        # descriptor on the rejection path.
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return ""
            raw = fh.read(LENS_OVERRIDE_MAX_BYTES + 1)
    except OSError:
        return ""
    truncated = len(raw) > LENS_OVERRIDE_MAX_BYTES
    capped = raw[:LENS_OVERRIDE_MAX_BYTES]
    try:
        text = capped.decode("utf-8")
    except UnicodeDecodeError:
        if not truncated:
            # The file fit entirely within the cap and still isn't valid UTF-8 (binary
            # garbage, wrong encoding) -- a genuinely invalid file, not a truncation
            # artifact. Stays "no override".
            return ""
        # Truncation landed mid-codepoint (any multibyte UTF-8 text, e.g. an em dash or
        # non-ASCII name): the ONLY invalid bytes are the incomplete trailing sequence
        # (at most 3 bytes) the cap cut off. Dropping just those (not the whole file)
        # keeps the documented "silently truncated, never rejected" contract instead of
        # discarding a valid override over one boundary byte (review finding).
        text = capped.decode("utf-8", errors="ignore")
    return text.strip()


@dataclass(frozen=True)
class BoardReviewer:
    """One seat on the reviewer board: a backend model + its review role.

    `model` is the backend string (e.g. `claude:claude-opus-5-5`,
    `commandcode:deepseek/deepseek-v4-pro`). `role` is a key into REVIEW_ROLES;
    `display` is a short human name for the --show-board listing / result label.
    `role_lens` resolves the BUILT-IN lens text (empty string for a role that isn't a
    REVIEW_ROLES key, so the job falls back to the generic prompt — never a crash);
    `resolved_lens(cwd)` is the same text optionally EXTENDED with a checked-in
    per-project `.review/lenses/<role>.md` override (review-cli#547) -- callers that
    dispatch a real review (`panel.build_board_job`) use `resolved_lens`, not the bare
    property, so a project override actually takes effect.

    This dataclass itself stays permissive on `role` for non-config callers (the
    `--task` role pad, `DEFAULT_BOARD`'s own literals, brainstorm/quorum personas);
    only the config.yaml `board:` parser (`_parse_board_entry`) validates `role`
    against REVIEW_ROLES and rejects an unrecognized one, review-cli#422."""

    model: str
    role: str
    display: str
    effort: str | None = None

    @property
    def role_lens(self) -> str:
        return REVIEW_ROLES.get(self.role, "")

    def resolved_lens(self, cwd: Path | None = None) -> str:
        """`role_lens`, EXTENDED with a checked-in project override for this seat's role
        (review-cli#547), when one is present under `cwd`.

        EXTENDS rather than REPLACES the built-in lens text deliberately: a project
        override exists to add project-specific knowledge (a house rule, a payments
        invariant) ON TOP of review-cli's own coverage for that role, not to silently
        drop the built-in coverage the board was designed around -- a project author who
        wants to ADD "also check X" should never accidentally lose "check CORRECTNESS:
        logic bugs, ..." just by writing a short override file. See `lens_override_text`
        for exactly when an override file counts as absent (missing/unreadable/blank all
        resolve to "no override", never a crash)."""
        base = self.role_lens
        override = lens_override_text(self.role, cwd)
        if not override:
            return base
        if not base:
            return override
        return f"{base}\n\n{override}"


# DEFAULT_BOARD: the raw 14-seat board used as the source of truth for built-in presets
# and for custom/config fallback paths. A plain `review diff` runs the default preset,
# not this tuple directly. The board is ordered by *priority* — strongest WORKING model
# first, weakest (or least-reliable, review-cli#fable-seat-reliability) last — NOT by
# role. Priority drives the FAILOVER pool: the selected board runs the top-N AVAILABLE
# seats, skipping a higher-priority seat whose backend isn't reachable (startup failover),
# and a seat that fails *during* the run is backfilled (mid-run failover). See
# select_pool() / panel.run_board_with_failover().
#
# Each seat still carries its OWN role/lens — the lens is what makes a multi-model panel
# cover the diff broadly instead of N duplicate passes. Priority decides WHO sits; the
# role decides WITH WHAT LENS they review. review-cli#588: the ROLES are what a run owes,
# so both failover steps keep them filled — the startup pool takes distinct roles first
# (`prefer_distinct_roles`), and a failed seat is backfilled by a reserve with the same
# (or another uncovered) role, else a reserve re-lensed onto the missing role, else a
# model that already answered in this run (`panel._choose_backfill`).
#
# To RE-RANK the board, just reorder this tuple (top = highest priority). Model ids are
# byte-exact against the provider catalogs (commandcode gateway /models, z.ai Coding-Plan)
# — do not alter the strings. Each is the TOP available version of its model family
# (fable-5-1, Sol, opus-5-5, GLM-5.3-via-gateway, Kimi-K3, GPT-6-Astra, GPT-6-Luna,
# claude-sonnet-5-5, Qwen3.8-Max, deepseek-v4-pro, Gemini, glm-5.3-via-z.ai).
#
# review-cli#382: TERRA_SEAT (now LUNA_SEAT)/SONNET_SEAT were added as live `performance`/`quality`
# fallbacks (unpaid_providers disabling commandcode left those roles with zero live
# seats — full incident writeup in CHANGELOG.md), and the z.ai-GLM seat was re-lensed
# `quality` -> `security`. Astra's pre-existing `consistency` role (a duplicate of Sol's)
# was DELIBERATELY LEFT UNCHANGED — an earlier draft flipped it to `security`, but that
# left `consistency` with zero live fallback board-wide; see its own entry below. `tests`
# now has MIMO_SEAT as a live paid fallback (review-cli#450); `contracts`/`architect`
# stay thin (no third distinct already-paid model existed to give them one too). Stopgap
# within the existing hardcoded seat-string mechanism, not the
# "roles declare models, harness auto-resolves" redesign tracked as review-cli#364/
# rig-cli#337.
#
# `pool: 2` (the `light` preset a bare `review diff` uses) can never show more than 2
# roles at all — a deliberate cost choice. (The older `--preset heavy` limit — Sol/Opus/
# Astra/Luna with `consistency` twice — is gone since review-cli#588 picks distinct roles
# first; see test_heavy_preset_double_failure_still_beats_pre_382_board.)
#
# AGENTIC BY DEFAULT (review-cli#24): every board seat that CAN read the repo does. The
# claude seats run via the agentic claude CLI; Sol/Astra/Luna via the codex CLI
# (`codex exec -s read-only -C <cwd>`); Kimi/Qwen/DeepSeek through opencode
# (`oc:provider/model`, built by `_agentic()` from the diff-only constant) and z.ai-GLM
# through omp (`omp:zai/...`), so they ALSO
# run read-only inside `-C` and can open ANY project file — not just the diff in the
# prompt. The board has a reserve, so an `oc:` seat that opencode can't reach on a given
# host probes UNAVAILABLE and is backfilled (startup or mid-run failover) — the board
# degrades gracefully rather than blocking. Two seats stay diff-only stateless HTTP calls:
# Gemini (no agentic transport) and the GLM-5.3-via-commandcode seat
# (`GLM_COMMANDCODE_SEAT` — opencode's commandcode provider does not register this GLM id, so
# the agentic form errors; the keyed-HTTP route is the one that reaches it). Both are read-
# only by construction (they POST only the diff). The diff-only `commandcode:`/`zai:` REST
# backends stay available for `-m cc`/`-m glm` and config boards on hosts without opencode;
# the board just prefers the agentic transport when one exists.
#
# The FLAT DEFAULT_MODELS panel deliberately keeps the diff-only commandcode Kimi seat
# (KIMI_SEAT): that panel has NO reserve/failover, so an opencode-less host would silently
# shrink it; the board can absorb that via its reserve, the flat panel cannot. Same model
# id (`_agentic(KIMI_SEAT)`), transport-only difference — one source of truth, no drift.
#
# OPTIONAL HEAVYWEIGHTS (NOT in the default board): add them to a config.yaml `board:`
# list if you want a 1M-context resilience / holistic pass:
#   - { model: "oc:commandcode/MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx, agentic
#   - { model: "oc:commandcode/nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx
DEFAULT_BOARD = (
    # priority 1 — Sol through Codex CLI.
    BoardReviewer(SOL_SEAT, "consistency", "Sol"),
    # priority 2 — Opus 5.5. Also the moderator (MODERATOR_CANDIDATES[0]).
    BoardReviewer("claude:claude-opus-5-5", "correctness", "Opus"),
    # priority 3 — GLM-5.3 via the Command Code gateway (CTO directive: directly under Opus).
    # DIFF-ONLY (keyed HTTP through review_commandcode, like Gemini): opencode's commandcode
    # provider does NOT register `zai-org/GLM-5.3`, so the agentic `oc:commandcode/...` form
    # errors — the diff-only route is the one that actually reaches it. Read-only by
    # construction (POSTs only the diff; no repo/tools/exec). Distinct from the lower-priority
    # `omp:zai/glm-5.3` seat (same model FAMILY, different provider/transport: z.ai vs gateway).
    # ROLE: `performance` — NOT `correctness` (which would duplicate Opus's lens).
    BoardReviewer(GLM_COMMANDCODE_SEAT, "performance", "GLM-cc"),
    # priority 4 — Kimi K3, AGENTIC through opencode (reads the repo). Same model id as
    # the flat panel's KIMI_SEAT (one source of truth via `_agentic`); transport-only diff.
    # Kept at this exact position: both the raw top-4 (Sol/Opus/GLM-cc/Kimi) and
    # DEFAULT_PRESET_BOARD's top-4 (Opus/GLM-cc/Kimi/Astra) need it here to stay
    # 4-distinct-roles — see test_default_pool_roles_are_distinct_no_lens_lost /
    # test_preset_boards_pin_model_order_pool_and_effort. LUNA_SEAT/SONNET_SEAT (below)
    # are placed AFTER Astra, outside both windows, for the same reason.
    BoardReviewer(_agentic(KIMI_SEAT), "quality", "Kimi"),
    # priority 5 — Astra, the agentic codex CLI route (reads the whole repo), pinned via
    # ASTRA_SEAT rather than the bare `"codex"` seat this used to be. ROLE LEFT AS
    # `consistency` (a duplicate of Sol's) — an earlier draft of #382 flipped it to
    # `security`, but that left `consistency` with ZERO live fallback board-wide. Since
    # review-cli#588 the startup pool skips it while Sol covers `consistency` (see
    # test_glm_cc_unavailable_backfills_a_new_lens_not_astras_duplicate), so it acts as
    # Sol's same-role backfill.
    BoardReviewer(ASTRA_SEAT, "consistency", "Astra"),
    # priority 6 — Luna (review-cli#382 added it as Terra): live `performance` fallback,
    # promoted whenever GLM-cc is unavailable.
    BoardReviewer(LUNA_SEAT, "performance", "Luna"),
    # priority 7 — Sonnet (review-cli#382): live `quality` fallback for Kimi — the exact
    # role the 2026-09-05 z.ai-quota incident exposed (see CHANGELOG.md).
    BoardReviewer(SONNET_SEAT, "quality", "Sonnet"),
    # priority 7.5 (review-cli#168) -- Sonnet via omp, a DIFFERENT auth path than the
    # seat right above; live `quality` fallback that survives a Claude-account-wide
    # outage (every claude:* seat sharing one exhausted/paywalled account).
    BoardReviewer(OMP_CLAUDE_SEAT, "quality", "Sonnet-omp"),
    # priority 8 — Qwen3.8-Max, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(_agentic("commandcode:Qwen/Qwen3.8-Max"), "security", "Qwen"),
    # priority 9 — DeepSeek-V4-Pro, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(
        _agentic("commandcode:deepseek/deepseek-v4-pro"), "tests", "DeepSeek"
    ),
    # priority 10 — Gemini. `contracts` has no other live seat (review-cli#382: there was
    # no third distinct already-paid model left to give it without doubling up quota
    # pressure on an account already carrying two board seats — see the block comment
    # above DEFAULT_BOARD) — it stays thin until commandcode/gemini are re-enabled or a
    # new paid seat is added.
    BoardReviewer("gemini", "contracts", "Gemini"),
    # priority 11 — Grok 4.7 via opencode's native `xai` provider (agentic, reads the
    # repo). Placed BEFORE the deliberately-slow z.ai GLM reserve below: this seat's own
    # dogfooding review of its addition (`review diff` seat, round 1) flagged that
    # appending it AFTER GLM meant deep mid-run failover always paid GLM's #65
    # pathological-slowness cost before ever reaching Grok, defeating the point of having
    # a faster reserve. ROLE `security`, not `performance`: after review-cli#382/#386
    # `performance` already has a live paid fallback (Luna) while `security` is held only
    # by Qwen (commandcode — unpaid/disabled machine-wide) and the slow, quota-fragile GLM
    # seat below, so this fast, live, oauth-backed reserve is the lens that was missing.
    BoardReviewer(GROK_SEAT, "security", "Grok"),
    # priority 12 — MiMo V2.6 Pro via omp OpenCode Go (review-cli#450). Live paid `tests`
    # fallback: DeepSeek (priority 9) is the only other tests seat and is commandcode
    # (often unpaid). Same rationale as GROK filling `security`. The
    # `oc:opencode/mimo-v2.5-free` catalog spelling is NOT DEFAULT-safe.
    BoardReviewer(MIMO_SEAT, "tests", "MiMo"),
    # priority 13 — GLM-5.3 (his z.ai subscription, the newest GLM), AGENTIC through
    # omp's `zai` provider (review-cli#645; opencode's `oc:zai/` route before that).
    # DELIBERATELY DEPRIORITIZED to the bottom of the reserve
    # (review-cli#65): this seat is observed to be PATHOLOGICALLY SLOW under load, so
    # promoting it onto the failover critical path stalls the pool's path to a verdict.
    # It stays on the board (still backfills when every faster reserve is also
    # exhausted), but it no longer blocks a fast verdict — Qwen / DeepSeek / Gemini / Grok
    # are promoted before it (review-cli#fable-seat-reliability: above only Fable). To
    # re-rank, move this line up; its position IS its priority. ROLE CHANGED (review-cli#382)
    # from `quality` to `security`: Sonnet now covers `quality` more reliably, freeing this
    # live-but-fragile seat to backfill `security` (whose only other seat, Qwen, is
    # disabled) instead of duplicating Sonnet.
    BoardReviewer("omp:zai/glm-5.3", "security", "GLM"),
    # priority 14 (LAST-RESORT reserve) — Fable 5.1 (Anthropic flagship). DEMOTED from
    # priority 1 to LAST (review-cli#fable-seat-reliability, 2026-08): `review stat`
    # telemetry showed a 97.9-100% dispatch failure rate (chronic session/usage-limit
    # exhaustion on the account it runs through — being priority 1 made it the FIRST
    # seat to exhaust that account's quota on every run), up from 67.7% two weeks
    # earlier and still climbing. STILL TRUE as of 2026-09-05 (review-cli#382 investigation,
    # `review stat --days 14`): 422 dispatch attempts, 100% failure rate, 674 retry/
    # promotion events (session_limit=7, paywall=278, auth=214, other=175) — no
    # improvement, so Fable stays last-resort rather than being promoted to help any of
    # the roles this change targets; a seat that answers ~0% of the time is not a real
    # fallback, it is decoration. At priority 1 this cost a near-certain-doomed real
    # dispatch on literally every default review before a single working seat ever
    # answered. Mirrors the review-cli#65 precedent right above (a seat with a
    # confirmed pathological failure mode is deprioritized to the bottom of the
    # reserve, not removed outright — it still backfills on the rare occasion every
    # other seat is also down). Worse than GLM's "merely slow" profile (Fable is
    # "essentially never answers"), so it sits even later than GLM. The
    # seat_cooldown.py catch-rate bug that let most of these doomed dispatches through
    # despite the existing cooldown cache is fixed separately in this same change
    # (`_chronic_unavailable_reason` — see its docstring); THIS demotion is the
    # complementary fix for the seats that DO still get dispatched (as reserve, now
    # rarely) — at priority 1 a paying seat had zero chance to answer first even with
    # a perfect cooldown catch rate. To re-rank, move this line up; its position IS
    # its priority.
    BoardReviewer(FABLE_SEAT, "architect", "Fable"),
)

# Presets are named, opinionated board+pool bundles for day-to-day review selection.
# They deliberately sit above the raw DEFAULT_BOARD: custom config still owns custom
# boards/models, while `--preset` gives the CLI a predictable canned roster.
DEFAULT_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "high")
    for r in DEFAULT_BOARD
    if r.model not in {FABLE_SEAT, SOL_SEAT}
)
# review-cli#fable-seat-reliability: FABLE_SEAT is excluded here too (mirrors
# `DEFAULT_PRESET_BOARD` above), for the same reason documented on its `DEFAULT_BOARD` entry
# — a ~100% dispatch failure rate makes a "heavy" preset's paying for it pure waste, on top of
# the DEFAULT_PRESET_BOARD gap this closes (that comprehension already excluded Fable; this one
# didn't). GLM review finding: filter FIRST, into a named intermediate, THEN `enumerate` it —
# the earlier version enumerated the unfiltered `DEFAULT_BOARD` and filtered Fable's row out
# afterward (buried inside `enumerate(...)`, easy to misread as a late filter), which only
# produced the correct xhigh/max split because Fable happened to sit LAST; a future re-rank
# moving Fable anywhere else would have silently shifted every later seat's tier by one slot
# with no test catching it (the tuple-order pin would read as a membership change, not a
# tier-boundary bug). The named `_heavy_eligible_seats` intermediate makes both the filter-
# first ordering and the resulting seat-count invariant self-documenting (14 seats as of
# review-cli#168's OMP_CLAUDE_SEAT on top of review-cli#450's MIMO_SEAT, review-cli#165's
# GROK_SEAT and review-cli#382's TERRA_SEAT (now LUNA_SEAT)/SONNET_SEAT — first 4 get `xhigh` effort: Sol, Opus,
# GLM-cc, Kimi (unchanged from before #382 — the Luna/Sonnet seats were inserted at raw
# priority 6-7, AFTER Kimi at priority 4, so they land in the `max` tier, not `xhigh`); the
# rest get `max`).
_heavy_eligible_seats = tuple(r for r in DEFAULT_BOARD if r.model != FABLE_SEAT)
HEAVY_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "xhigh" if i < 4 else "max")
    for i, r in enumerate(_heavy_eligible_seats)
)
LIGHT_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "medium") for r in DEFAULT_PRESET_BOARD
)

PRESET_BOARDS = {
    "default": DEFAULT_PRESET_BOARD,
    "heavy": HEAVY_PRESET_BOARD,
    "light": LIGHT_PRESET_BOARD,
}
PRESET_POOL_SIZES = {
    "default": 4,
    "heavy": 4,
    "light": 2,
}
DEFAULT_PRESET = "light"
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


# DEFAULT_POOL_SIZE: the pool-size fallback ONLY for a custom config `board:` with no
# `pool:` key set (not the bare-`review`-with-no-config-at-all case — that resolves via
# DEFAULT_PRESET's own pool, PRESET_POOL_SIZES[DEFAULT_PRESET] = 2 as of Alex's
# 2026-08-28 "light" default; see preset_pool_size()). The board (DEFAULT_BOARD or a
# config `board:`) is a priority-ordered list; the top N AVAILABLE seats participate and
# the remaining (lower-priority) seats are the RESERVE that backfills a skipped/failed
# seat. The board is NEVER disabled — `--pool` only sizes how many seats run. See
# select_pool() / panel.run_board_with_failover().
DEFAULT_POOL_SIZE = 4

# SYNC: keep equal to reviewlib.stats._DEFAULT_QUORUM_FLOOR. That is the default
# min_roles for `review task CODE --check` / gh ship. Lowering it here would make
# a recorded `review diff --task` run structurally unable to satisfy the ship gate.
SHIP_MIN_ROLES = 3


def recorded_task_pool_size(
    resolved: int,
    *,
    explicit_pool: bool,
    recorded: bool,
    min_roles: int = SHIP_MIN_ROLES,
) -> int:
    """Pool size for a recorded `review diff --task` run.

    A recorded review must cover at least `min_roles` distinct board roles so one
    iteration can satisfy default `review task CODE --check` / gh ship. `--preset
    light` is pool 2; without this floor, re-running light never accumulates a 3rd
    role (the board is priority-ordered). An explicit `--pool N` still wins,
    including N < min_roles and N <= 0 ("all seats"). Unrecorded runs keep
    `resolved` as-is.
    """
    if not recorded or explicit_pool or resolved <= 0:
        return resolved
    return max(resolved, min_roles)


def preset_names() -> tuple[str, ...]:
    return tuple(PRESET_BOARDS)


def preset_pool_size(name: str | None) -> int:
    if not name:
        return DEFAULT_POOL_SIZE
    try:
        return PRESET_POOL_SIZES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown preset {name!r}; expected one of {', '.join(preset_names())}"
        ) from exc


def preset_board(name: str | None) -> list[BoardReviewer]:
    if not name:
        return [replace(r) for r in PRESET_BOARDS[DEFAULT_PRESET]]
    try:
        return [replace(r) for r in PRESET_BOARDS[name]]
    except KeyError as exc:
        raise ValueError(
            f"unknown preset {name!r}; expected one of {', '.join(preset_names())}"
        ) from exc


def _normalize_effort(value: object) -> str | None:
    effort = value.strip().lower() if isinstance(value, str) and value.strip() else None
    if effort is None:
        return None
    if effort not in EFFORT_LEVELS:
        print(
            f"[review-cli] config board entry has unsupported effort {effort!r}; "
            f"expected one of {', '.join(sorted(EFFORT_LEVELS))}; ignoring effort",
            file=sys.stderr,
            flush=True,
        )
        return None
    return effort


class EffortValueError(ValueError):
    """A `--effort` value is not a recognised level. Unlike `_normalize_effort`
    (which warns + drops a bad CONFIG board entry so a stale file never crashes a
    run), a bad CLI `--effort` is user input for THIS run and must fail loudly."""


def _require_effort(value: str) -> str:
    """Strict effort parse for run-scoped CLI input: raise on anything not in
    EFFORT_LEVELS. Shares the level vocabulary with `_normalize_effort`."""
    level = value.strip().lower()
    if level not in EFFORT_LEVELS:
        raise EffortValueError(
            f"unsupported effort {value!r}; expected one of {', '.join(sorted(EFFORT_LEVELS))}"
        )
    return level


def _effort_provider_key(model_or_provider: str) -> str:
    """Canonical backend-route key a `--effort` override is matched on.

    Reuses `backends.provider_route_name` (the SAME resolution `review` uses to pick a
    seat's backend) so both a `--effort <provider>=<level>` token and a seat's model id
    collapse to the same route name — `codex`, `claude`, `opencode` (incl. every `oc:`/
    unknown seat), `omp`, `gemini`, `commandcode`, `zai`, `openrouter`. Imported lazily to keep
    `config` import-light and cycle-free (backends never imports config)."""
    from . import backends

    return backends.provider_route_name(model_or_provider)


# Known route keys a `--effort <provider>=<level>` token may target (for the error message
# when a token is unrecognised). Derived once from the backend routes.
_EFFORT_PROVIDER_KEYS = (
    "codex",
    "claude",
    "gemini",
    "opencode",
    "commandcode",
    "zai",
    "openrouter",
)


def _require_provider_key(provider: str) -> str:
    """Validate a user-supplied `--effort <provider>=...` token and return its canonical
    route key. A typo (`claud`, `codexx`) that would otherwise fall through to the opencode
    catch-all raises EffortValueError instead of silently overriding the opencode route."""
    from . import backends

    if not backends.is_known_backend_token(provider):
        raise EffortValueError(
            f"unknown --effort provider {provider!r}; expected one of {', '.join(_EFFORT_PROVIDER_KEYS)}"
        )
    return backends.provider_route_name(provider)


@dataclass(frozen=True)
class EffortOverride:
    """A run-scoped `--effort` override resolved once from the CLI.

    `default` is the global level (a bare `--effort high`); `by_provider` maps a backend
    route key (see `_effort_provider_key`) to a level (`--effort codex=high`). It OVERRIDES
    the per-seat config `effort` for a run and falls back to the seat value when it has
    nothing to say — the resolution seam is `resolve`."""

    default: str | None = None
    by_provider: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate + canonicalize provider keys to backend-route names, so a DIRECT
        # construction (`EffortOverride(by_provider={"oc": "high"})`) matches the same route
        # `effort_for` resolves a model to, and a typo'd key (`claud`) fails loudly instead
        # of silently landing on the opencode catch-all. parse_effort_flag already does this;
        # this keeps programmatic callers coherent. The result is frozen behind a
        # MappingProxyType so `frozen=True` is not defeated by an in-place dict mutation.
        # Frozen dataclass → object.__setattr__.
        canonical = {
            _require_provider_key(key): value for key, value in self.by_provider.items()
        }
        object.__setattr__(self, "by_provider", MappingProxyType(canonical))

    @property
    def is_empty(self) -> bool:
        return self.default is None and not self.by_provider

    def effort_for(self, model_or_provider: str) -> str | None:
        """The run-scoped level for a model/provider: the per-provider override when set,
        else the global default, else None (nothing to override — caller keeps the seat)."""
        if self.is_empty:
            return None
        key = _effort_provider_key(model_or_provider)
        if key in self.by_provider:
            return self.by_provider[key]
        return self.default

    def resolve(self, model: str, seat_effort: str | None) -> str | None:
        """The effective effort for a seat: `effort_for(provider) or seat_effort`. The
        run-scoped flag WINS when present; otherwise the per-seat config value stands."""
        return self.effort_for(model) or seat_effort


def parse_effort_flag(values: list[str] | None) -> EffortOverride:
    """Parse repeated/comma-joined `--effort` tokens into an EffortOverride.

    A bare token (`high`) sets the global default; a `provider=level` token
    (`codex=high`, `opencode=max`) sets a per-provider override, its provider normalised
    to the same route key seats resolve to. Raises EffortValueError on an unknown level."""
    default: str | None = None
    by_provider: dict[str, str] = {}
    for raw in values or []:
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            if "=" in token:
                provider, _, level = token.partition("=")
                provider = provider.strip()
                if not provider:
                    raise EffortValueError(
                        f"malformed --effort token {token!r}; expected provider=level"
                    )
                by_provider[_require_provider_key(provider)] = _require_effort(level)
            else:
                default = _require_effort(token)
    return EffortOverride(default=default, by_provider=by_provider)


def apply_effort_override(
    board: list[BoardReviewer],
    override: EffortOverride | None,
) -> list[BoardReviewer]:
    """Return a board with each seat's effort resolved through the run-scoped override
    (`override.resolve(model, seat_effort)`). Identity-returns the board when the override
    is empty, so the per-seat config effort stands untouched with no `--effort`. Always
    returns a FRESH list (the seats are frozen/immutable, so the copy is safe to hand on)."""
    if override is None or override.is_empty:
        return list(board)
    return [replace(r, effort=override.resolve(r.model, r.effort)) for r in board]


def _always_available(_reviewer: BoardReviewer) -> bool:
    """Default availability predicate: every seat counts as available (the legacy
    'no failover here, availability is checked downstream' behaviour)."""
    return True


def _effective_pool_size(seat_count: int, pool: int) -> int:
    """How many seats a `--pool N` request actually selects from `seat_count` available
    seats: `pool <= 0` means ALL, a `pool` larger than the count is clamped to the count,
    otherwise exactly `pool`. The single source of truth for select_pool +
    split_pool_reserve so the two can never drift."""
    if pool <= 0 or pool >= seat_count:
        return seat_count
    return pool


def prefer_distinct_roles(seats: list[BoardReviewer]) -> list[BoardReviewer]:
    """`seats` (priority order) with every seat that REPEATS an already-taken lensed role
    moved behind the seats that bring a new one; each group keeps its priority order.

    review-cli#588: a pure priority slice put Codex-omp AND Codex (both `consistency`)
    into a 3-seat pool, and failover then dropped consistency AND security in the same
    run. Priority still decides WHO sits among seats of the same role; the pool just
    stops spending a slot on a lens it already has while another lens is available.
    A seat without a real lens (`role_lens` empty: role-less `models:` roster, unknown
    role) is never treated as a duplicate — there is no lens to dedupe on."""
    first: list[BoardReviewer] = []
    repeats: list[BoardReviewer] = []
    taken: set[str] = set()
    for seat in seats:
        if seat.role_lens:
            if seat.role in taken:
                repeats.append(seat)
                continue
            taken.add(seat.role)
        first.append(seat)
    return first + repeats


def select_pool(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
) -> list[BoardReviewer]:
    """Pick the top `pool` AVAILABLE seats by priority, distinct roles first; the rest
    are the reserve.

    The board is EXPECTED to be priority-ordered (strongest first) — that order IS the
    priority. Startup failover: a higher-priority seat whose backend is NOT reachable
    (per `available`) is SKIPPED and the next-priority seat pulled up, so the run still
    starts with `pool` WORKING seats when enough reachable seats exist. A reachable seat
    whose lensed role an earlier seat already covers yields to the next seat with a new
    role (`prefer_distinct_roles`, review-cli#588). The default `available` treats every
    seat as reachable.

    The selection is deterministic. `pool <= 0` means "all (available) seats", and a
    `pool` larger than the available count is clamped — a caller can never ask for more
    seats than exist. An empty board stays empty. Returns a NEW list, never a view."""
    seats = [r for r in board if available(r)]
    return _pick_distinct(seats, _effective_pool_size(len(seats), pool))


def _pick_distinct(seats: list[BoardReviewer], n: int) -> list[BoardReviewer]:
    """The `n` seats `prefer_distinct_roles` ranks first, returned in their original
    (priority) order — the pool's membership changes, its dispatch order does not."""
    picked_ids = {id(r) for r in prefer_distinct_roles(seats)[:n]}
    return [r for r in seats if id(r) in picked_ids]


def split_pool_reserve(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
) -> tuple[list[BoardReviewer], list[BoardReviewer]]:
    """Split the AVAILABLE board into (pool, reserve) for failover.

    `pool` = the seats `select_pool` picks (priority, distinct roles first).
    `reserve` = the remaining available seats, in board priority order, which back-fill
    a pool seat that fails mid-run. Unavailable seats are in NEITHER list (they can't
    run). The two lists are disjoint and together hold every available seat. The
    `board` is EXPECTED to be priority-ordered (its order is the priority)."""
    seats = [r for r in board if available(r)]
    picked = _pick_distinct(seats, _effective_pool_size(len(seats), pool))
    picked_ids = {id(r) for r in picked}
    return picked, [r for r in seats if id(r) not in picked_ids]


def _is_near_limit(
    model: str,
    usage_percent: Callable[[str], float | None],
    threshold: float,
) -> bool:
    pct = usage_percent(model)
    return pct is not None and pct >= threshold


def _least_depleted_index(
    models: list[str], usage_percent: Callable[[str], float | None]
) -> int:
    """Index of the model with the LOWEST usage percent (None reads as 0 — the
    most-available case — but this is only ever called on a set where every
    known percent is already >= a threshold, per its callers, so a None here
    would mean "not really near-limit" and callers filter those out first)."""
    return min(
        range(len(models)),
        key=lambda i: (usage_percent(models[i]) or 0.0, i),
    )


def select_pool_with_reuse(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
    usage_percent: Callable[[str], float | None] | None = None,
    limit_threshold: float = DEFAULT_LIMIT_THRESHOLD,
) -> list[BoardReviewer]:
    """Like `select_pool`, but reuses already-available models across the
    remaining role slots instead of shrinking the board when the usage-aware
    candidate pool is smaller than the target size.

    Selection, in order:
      1. `reachable` = seats passing `available` (the same distinct-model
         liveness check `select_pool` uses).
      2. If `usage_percent` is given, split `reachable` into seats whose model
         is BELOW `limit_threshold` percent used (`under_limit` — None counts
         as "not excluded", fail-open when usage data is unavailable for a
         model/provider) and seats at/above it.
      3. `candidates` = `under_limit` if non-empty; otherwise (every reachable
         seat is near its limit) the SINGLE seat with the lowest usage
         percentage that is still below 100% (ties broken by board priority).
         A family at >=100% is never selected; if every reachable seat is
         exhausted the pool shrinks to empty rather than spawning it.
      4. Target size `n`:
           - `usage_percent is None` -> `_effective_pool_size(len(reachable),
             pool)`, IDENTICAL to `select_pool`'s clamp (this function is a
             byte-identical drop-in for a caller that hasn't opted into
             reuse at all: zero behaviour change).
           - `usage_percent` given (a caller that HAS opted in) -> for
             `pool > 0`, `min(pool, len(board))` — capped by the size of the
             FULL board (the real ceiling on how many distinct role slots
             exist to pad into), not by `len(reachable)`. Capping by
             `len(reachable)` was the task-cli#139 bug: with only 1 reachable
             seat and a `pool` of 3+, `n` collapsed to 1 before the padding
             loop in step 6 ever got a slot to fill, so a hard-unavailable
             board (session-limit cooldown, not merely near a usage
             threshold) never got padded even though the mechanism exists
             for exactly this. `pool <= 0` ("all") keeps clamping to
             `len(reachable)` — "all" has no fixed target size to pad
             towards, so reuse has nothing to pad here (unaffected: this is
             the same value the near-usage-limit path already used).
      5. The first `min(n, len(candidates))` slots come from `candidates` in
         priority order with distinct roles first (`prefer_distinct_roles`,
         review-cli#588); a slot that still repeats an earlier slot's lensed role
         is re-lensed onto a lensed board role no slot covers yet
         (`_relens_repeated_roles`). With `usage_percent` None there is no
         re-lensing — the pick equals `select_pool`'s.
      6. Any remaining slots (`n - len(candidates)`) are filled by cycling back
         through `candidates`' models, each one taking the NEXT board role not
         yet used by an earlier slot (in board priority order) — so a shrunk
         model pool still reviews the diff under `n` distinct lenses instead of
         fewer."""
    return _select_pool_with_reuse(
        board, pool, available, usage_percent, limit_threshold
    )[0]


def _relens_repeated_roles(
    picked: list[BoardReviewer], board: list[BoardReviewer]
) -> list[BoardReviewer]:
    """`picked` with each seat that repeats an earlier seat's lensed role moved onto a
    lensed board role nobody in `picked` covers yet (board priority order), as a fresh
    `replace()` copy. A repeat stays as-is once the board has no unused lensed role left.

    review-cli#588: `prefer_distinct_roles` already puts every new-role seat first, so a
    repeat only lands in the pool when the reachable seats carry fewer distinct roles than
    the pool size — e.g. Opus + Codex-omp + Codex reachable, the last two both
    `consistency`. The slot is still worth dispatching; it should review a lens the pool
    lacks rather than the same lens twice."""
    seen: set[str] = set()
    repeats: list[int] = []
    for index, seat in enumerate(picked):
        if not seat.role_lens:
            continue
        if seat.role in seen:
            repeats.append(index)
        else:
            seen.add(seat.role)
    if not repeats:
        return list(picked)
    free_roles = [
        role
        for role in dict.fromkeys(r.role for r in board if r.role_lens)
        if role not in seen
    ]
    out = list(picked)
    for index, role in zip(repeats, free_roles):
        out[index] = replace(out[index], role=role)
    return out


def _select_pool_with_reuse(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool],
    usage_percent: Callable[[str], float | None] | None,
    limit_threshold: float,
) -> tuple[list[BoardReviewer], list[BoardReviewer]]:
    """`select_pool_with_reuse`'s body, also returning the ORIGINAL board seats the pool
    consumed. Re-lensed and padded seats are fresh `replace()` copies, so the reserve
    split can't recover their originals by identity from the pool alone."""
    reachable = [r for r in board if available(r)]
    if not reachable:
        return [], []

    if usage_percent is None:
        n = _effective_pool_size(len(reachable), pool)
        picked = _pick_distinct(reachable, n)
        return picked, list(picked)

    # Reuse is opted into (usage_percent given): size `n` against the FULL
    # board when a concrete pool is requested, not just what's reachable —
    # see step 4 above (task-cli#139).
    if pool > 0:
        n = _effective_pool_size(len(board), pool)
    else:
        n = len(reachable)

    under_limit = [
        r
        for r in reachable
        if not _is_near_limit(r.model, usage_percent, limit_threshold)
    ]
    if under_limit:
        candidates = under_limit
    else:
        not_exhausted = [
            r for r in reachable if (usage_percent(r.model) or 0.0) < 100.0
        ]
        if not not_exhausted:
            return [], []
        idx = _least_depleted_index([r.model for r in not_exhausted], usage_percent)
        candidates = [not_exhausted[idx]]

    picked = _pick_distinct(candidates, min(n, len(candidates)))
    primary = _relens_repeated_roles(picked, board)
    extra_needed = n - len(primary)
    if extra_needed <= 0:
        return primary, picked

    if not any(r.role_lens for r in board):
        # A role-less OR every-role-unknown board (every seat's `role_lens` resolves
        # empty — either role="" outright, e.g. a config `models:` roster with no lens
        # metadata, OR a role string that isn't a REVIEW_ROLES key, e.g. a typo) has no
        # distinct LENSES to gain from reuse: every seat would fall back to the same
        # generic prompt regardless of its `role` label, so duplicating a model here is
        # pure cost with zero added review diversity — checked via `role_lens`, not the
        # raw `role` string, so a typo'd/unknown role doesn't masquerade as a real lens
        # (codex review finding, task-cli#139). Leave the pool at its (smaller) primary
        # size instead.
        return primary, picked

    used_roles = {r.role for r in primary}
    # Every DISTINCT board role not yet used by `primary`, in board priority order,
    # deduped order-preservingly — restricted to roles with a REAL `role_lens` (codex
    # review finding, task-cli#139): a typo'd/unknown role resolves to the SAME generic
    # prompt as every other unknown role, so counting it as its own "distinct" padding
    # slot would dispatch byte-identical generic-prompt duplicates while mislabeling
    # them as separate lenses — exactly the cost the dedup two lines below this comment
    # already guards against for KNOWN duplicate roles. A real board can carry the SAME
    # known role twice (DEFAULT_BOARD has "consistency" and "quality" each on two seats)
    # — without the dedup, two padded extras could land on that same role, dispatching
    # two BYTE-IDENTICAL prompts (same model AND same lens) in parallel: pure duplicate
    # cost, contradicting this function's own "N distinct lenses" guarantee (k3 review
    # finding, review-cli#205 round 3).
    role_pool: list[str] = []
    for r in board:
        if r.role_lens and r.role not in used_roles and r.role not in role_pool:
            role_pool.append(r.role)

    extra: list[BoardReviewer] = []
    for i in range(extra_needed):
        if i >= len(role_pool):
            # Every distinct board role is already spoken for — stop padding
            # SHORT rather than emit a model+role pair already in the pool.
            # A smaller-than-`n` pool (still all-distinct lenses) beats a
            # full-size pool with a wasted duplicate dispatch.
            break
        extra.append(replace(candidates[i % len(candidates)], role=role_pool[i]))
    return primary + extra, picked


def select_pool_and_reserve_with_reuse(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
    usage_percent: Callable[[str], float | None] | None = None,
    limit_threshold: float = DEFAULT_LIMIT_THRESHOLD,
) -> tuple[list[BoardReviewer], list[BoardReviewer]]:
    """`(pool_seats, reserve_seats)` for the board dispatch path — the reuse-aware
    counterpart to `split_pool_reserve`, extracted as its own testable function
    (Fable/k3 review finding: this exact pool/reserve seat-identity construction
    had zero coverage before).

    `available` is probed EXACTLY ONCE (both `pool_seats` and `reserve_seats`
    are derived from the same `reachable` snapshot) — calling `available`
    separately for each risks it disagreeing with itself between calls when
    it wraps something non-deterministic (e.g. a live chain-aware liveness
    probe), and wastes a non-free probe.

    `reserve_seats` = every reachable seat NOT one of `pool_seats`'s ORIGINAL
    picks, matched by object IDENTITY (not `.model` string) so a config board
    that legitimately lists the same model under two distinct roles never
    loses one seat's reserve slot just because the OTHER seat with the same
    model landed in the pool. A seat's reserve MEMBERSHIP does not depend on
    its raw board position: a seat excluded from the pool for being near its
    usage limit is just as eligible a last-resort backfill as one that simply
    didn't fit under `pool`. Seats at or above 100% are omitted from reserve
    entirely — promoting an exhausted Codex family after a primary failure
    would spawn it, and Codex has no backend-level spawn guard. Its reserve
    ORDER does, however, get demoted: under-limit overflow seats are offered
    FIRST (each keeps its relative board-priority order), near-limit seats
    LAST (same) — otherwise a near-limit seat sitting at a higher board
    position than a perfectly healthy overflow seat would be promoted to the
    pool FIRST on mid-run failover, defeating the reason it was excluded to
    begin with (Fable review finding, review-cli#205 round 5)."""
    reachable = [r for r in board if available(r)]
    reachable_ids = {id(r) for r in reachable}
    pool_seats, picked = _select_pool_with_reuse(
        board,
        pool,
        lambda r: id(r) in reachable_ids,
        usage_percent,
        limit_threshold,
    )
    consumed_ids = {id(r) for r in picked} & reachable_ids
    unused = [r for r in reachable if id(r) not in consumed_ids]
    if usage_percent is None:
        reserve_seats = unused
    else:
        # Same <100 check as the primary-pool least-depleted fallback: a
        # family at 100% must not be promoted after a primary failure
        # (Codex has no backend spawn guard). Unknown usage (None) stays.
        unused = [r for r in unused if (usage_percent(r.model) or 0.0) < 100.0]
        # Stable sort: seats keep their relative board-priority order WITHIN
        # each group (under-limit first, near-limit last) — this is a
        # DEMOTION of near-limit seats, not a full re-rank.
        reserve_seats = sorted(
            unused,
            key=lambda r: _is_near_limit(r.model, usage_percent, limit_threshold),
        )
    return pool_seats, reserve_seats


def expand_flat_models_with_reuse(
    models: list[str],
    target: int,
    usage_percent: Callable[[str], float | None] | None = None,
    limit_threshold: float = DEFAULT_LIMIT_THRESHOLD,
) -> list[str]:
    """Reuse-aware padding for a FLAT model list (quorum / just-ask / brainstorm
    — no per-seat role/board concept). Excludes any model at/above
    `limit_threshold` percent of its usage window; if that empties the list,
    falls back to the SINGLE least-depleted model still below 100% (repeated
    for every slot). A model at >=100% is never selected; if every model is
    exhausted the result is empty rather than spawning it. Returns `target`
    entries (or `len(models)` when `target <= 0`) when any candidate remains,
    cycling through the surviving models in their given order.

    A no-op (returns `models` unchanged) when `usage_percent` is None, `models`
    is empty, or nothing is excluded and `target <= len(models)` — so existing
    callers see zero behaviour change until a model actually IS near its
    limit."""
    if usage_percent is None or not models:
        return list(models)

    n = target if target > 0 else len(models)
    under_limit = [
        m for m in models if not _is_near_limit(m, usage_percent, limit_threshold)
    ]
    if under_limit:
        pool_models = under_limit
    else:
        not_exhausted = [m for m in models if (usage_percent(m) or 0.0) < 100.0]
        if not not_exhausted:
            return []
        idx = _least_depleted_index(not_exhausted, usage_percent)
        pool_models = [not_exhausted[idx]]

    return [pool_models[i % len(pool_models)] for i in range(n)]


def _display_name(model: str) -> str:
    """A short, human-friendly label for a board reviewer derived from its model
    string — used when config.yaml omits an explicit name. Takes the last path
    segment of the provider id (so `commandcode:deepseek/deepseek-v4-pro` -> the
    provider tail, `claude:claude-opus-5-5` -> `claude-opus-5-5`)."""
    tail = model.split(":", 1)[1] if ":" in model else model
    return tail.rsplit("/", 1)[-1]


class BoardConfigError(ValueError):
    """A `board:` key was present and non-empty in config.yaml but contained NO
    usable reviewer (every entry malformed). Raised instead of silently falling
    back to the paid DEFAULT_BOARD — a user who deliberately configured a board
    must get a clear error, not an unexpected paid default-board run (cost-safety)."""


def _resolve_capability_model(spec: str) -> str | None:
    """Resolve a board entry's `capability:` value to a concrete review-cli seat from the
    shared `models.yaml` manifest, or `None` when it can't be resolved (no manifest on this
    host / unknown capability / role with no review-cli route).

    Two forms, both ADDITIVE to the literal `model:` form:
      * `capability: role:reasoning` (or any `roles:` key — architect/vision/code/fast) ->
        the manifest's `roles:` map resolves the symbolic lens to a concrete pinned id;
      * `capability: vision` (a bare capability tag) -> the strongest manifest entry carrying
        that tag.

    The manifest is the ecosystem source of truth (rig-cli#8); when it isn't reachable this
    returns `None` and the caller skips the entry with a warning — the board keeps working off
    its literal-`model:` entries / the hardcoded DEFAULT_BOARD, never a crash."""
    from . import manifest as _manifest

    spec = spec.strip()
    if spec.startswith("role:"):
        return _manifest.resolve_role(spec[len("role:") :].strip())
    return _manifest.resolve_capability(spec)


def _matching_role_name(spec: str, manifest_mod) -> str | None:
    """The CANONICAL manifest role name a bare `capability:` value names (case-insensitive), or
    `None` when it isn't a role name. A `role:`-prefixed spec is not a bare-capability collision,
    so it returns None. Returns the manifest's own casing so a hint/lookup never dead-ends on a
    case mismatch (`Vision` -> `vision`)."""
    if spec.startswith("role:"):
        return None
    lowered = spec.lower()
    for name in manifest_mod.role_names():
        if name.lower() == lowered:
            return name
    return None


def _capability_fail_reason(capability: str) -> str:
    """A clear reason string for an unresolved `capability:` entry. The common footgun is a
    BARE value that is a ROLE name (`reasoning`/`architect`/`fast`) rather than a capability
    TAG — the two namespaces overlap, so `capability: reasoning` scans tags (not the `roles:`
    map) and `capability: architect` resolves to nothing. Detect that and hint `role:<x>`."""
    from . import manifest as _manifest

    spec = capability.strip()
    # Only hint `role:<x>` when the value is PURELY a role name — NOT a real capability tag.
    # `vision`/`reasoning`/`code` are BOTH tags and roles; if `capability: vision` failed it's
    # because no model carries the tag (a manifest gap), so claiming "vision is a ROLE name, not
    # a tag" would be factually wrong. A pure role (`architect`/`fast`) genuinely isn't a tag.
    if spec.lower() not in _manifest.KNOWN_CAPABILITIES:
        role_match = _matching_role_name(spec, _manifest)
        if role_match is not None:
            # Suggest the role's CANONICAL casing (the manifest key), so the hint never dead-ends
            # on a case mismatch (`capability: Architect` -> hint `role:architect`, which resolves).
            return (
                f"capability {spec!r} is a ROLE name, not a capability tag — write "
                f"'capability: role:{role_match}' to use the manifest's role map"
            )
    return (
        f"capability {spec!r} could not be resolved from the model manifest "
        "(unknown capability tag / no model carries the tag / unknown role / no manifest "
        "reachable — is agent-tools lib/contracts/models.yaml present?)"
    )


def _resolve_entry_model(entry: dict) -> str | None:
    """The concrete seat string for one board entry, from EITHER a literal `model:` or a
    manifest-resolved `capability:` (mutually exclusive; `model:` wins if both are present).
    Returns `None` (with a warning) when neither yields a usable seat, so the caller skips
    the entry — the board degrades, never crashes."""
    model = entry.get("model")
    if isinstance(model, str) and model.strip():
        return _expand_alias(model.strip())
    capability = entry.get("capability")
    if isinstance(capability, str) and capability.strip():
        resolved = _resolve_capability_model(capability)
        if resolved is None:
            print(
                f"[review-cli] board entry ignored ({_capability_fail_reason(capability)}): "
                f"{entry!r}",
                file=sys.stderr,
                flush=True,
            )
            return None
        # Run the resolved seat through the SAME alias normalization the literal `model:` path
        # uses, so an equivalent value (e.g. a manifest id that happens to be an alias) can't
        # behave differently between the two paths.
        return _expand_alias(resolved)
    print(
        f"[review-cli] board entry ignored (missing 'model' or 'capability'): {entry!r}",
        file=sys.stderr,
        flush=True,
    )
    return None


def _parse_board_entry(entry: object) -> BoardReviewer | None:
    """Parse ONE config `board:` entry into a BoardReviewer, or `None` (with a warning) when
    it's unusable so `load_board` skips it. A seat comes from a literal `model:` or a
    capability-resolved `capability:` (rig-cli#8).

    An unrecognized (non-empty) `role:` string is a HARD error (`BoardConfigError`), not a
    skip-with-warning: review-cli#422. It used to be kept with a stderr warning and silently
    degraded to the generic review prompt (role_lens == "") — harmless for one typo, but on a
    board with MULTIPLE unrecognized roles every one of those seats collapsed onto the exact
    same generic prompt, so "N roles reviewed" quietly became "N duplicate generic passes"
    with no signal beyond an easy-to-miss stderr line. A typo'd role is a config bug the
    operator should fix, not a board that degrades and keeps running — same cost-safety
    stance as the ALL-entries-malformed case in `load_board` below, just triggered by one bad
    `role:` instead of a whole board of them. An OMITTED role (`role: ""` / no `role` key) is
    unaffected — that is the intentional 'general, no lens' seat exercised by
    `test_role_omitted_is_general_with_generic_prompt`."""
    if not isinstance(entry, dict):
        print(
            f"[review-cli] board entry ignored (not a mapping): {entry!r}",
            file=sys.stderr,
            flush=True,
        )
        return None
    model = _resolve_entry_model(entry)
    if model is None:
        return None
    raw_role = entry.get("role")
    if raw_role is not None and not isinstance(raw_role, str):
        # A non-string, non-omitted `role:` (e.g. an unquoted `role: true`/`role: no`
        # parsed by YAML as a bool, or `role: 123`) is the SAME class of config typo as
        # an unrecognized string -- catch it here too, or it would silently coerce to ""
        # and degrade to the generic prompt with no warning, defeating review-cli#422's
        # fail-fast contract via a type-confusion typo instead of a spelling one.
        raise BoardConfigError(
            f"config.yaml board reviewer {model!r} has a non-string role {raw_role!r} "
            f"({type(raw_role).__name__}) — valid roles are: "
            f"{', '.join(sorted(REVIEW_ROLES))}. Quote the role value, or omit it to use "
            "the generic review prompt."
        )
    role = raw_role.strip() if isinstance(raw_role, str) else ""
    if role and role not in REVIEW_ROLES:
        raise BoardConfigError(
            f"config.yaml board reviewer {model!r} has unknown role {role!r} — "
            f"valid roles are: {', '.join(sorted(REVIEW_ROLES))}. Fix the role, or "
            "omit it to use the generic review prompt."
        )
    name = entry.get("name")
    display = (
        name.strip() if isinstance(name, str) and name.strip() else _display_name(model)
    )
    effort = _normalize_effort(entry.get("effort"))
    return BoardReviewer(model=model, role=role, display=display, effort=effort)


def _drop_malformed_board_entries(config: dict | None) -> dict:
    """A shallow copy of `config` with only the `board:` entries that pass
    `_parse_board_entry` retained -- an entry that raises `BoardConfigError` (an
    unrecognized/non-string `role:`, review-cli#422) is dropped individually instead of
    the caller discarding metadata for every OTHER entry too.

    Used by the CLI's `_board_from_models_lenient` retry path: an operator naming
    models directly via explicit `-m` only cares about metadata for THOSE models: one
    unrelated, unrecognized-role entry sitting elsewhere in the same `board:` list must
    not wipe out role/name/effort metadata for an entry that DOES match and IS valid.
    Non-role parse failures (no usable `model`/`capability`) are already tolerated by
    `_parse_board_entry` returning `None` rather than raising, so those pass straight
    through unaffected; only entries that would actually raise are dropped here."""
    if not isinstance(config, dict):
        return {}
    raw_board = config.get("board")
    if not isinstance(raw_board, list):
        return {**config}
    sanitized = []
    for entry in raw_board:
        try:
            _parse_board_entry(entry)  # only care whether this raises, not the result
        except BoardConfigError:
            continue
        sanitized.append(entry)
    return {**config, "board": sanitized}


def load_board(
    config: dict | None = None, *, preset: str | None = None
) -> list[BoardReviewer]:
    """Resolve the active reviewer board.

    A `board:` key in config.yaml (a list of `{model | capability, role[, name]}`
    mappings) overrides the built-in default preset. A seat names its model EITHER
    literally (`model: claude:claude-opus-5-5`) OR by CAPABILITY resolved from the
    shared `models.yaml` manifest (`capability: vision` / `capability: role:reasoning`,
    rig-cli#8) — the latter degrades to "skip this entry with a warning" when the
    manifest isn't reachable, so a manifest-less host still runs its literal seats.
    Validation degrades gracefully but is cost-safe — it never silently substitutes
    the paid default board for a board the user explicitly configured:
      * an ABSENT / non-list / empty `board:`  -> fall back to DEFAULT_PRESET_BOARD
        (no preference expressed, the safe default is intended);
      * a PRESENT non-empty `board:` with SOME valid entries -> keep the valid
        ones; each bad entry is skipped with a warning;
      * an entry with neither a usable `model` nor a resolvable `capability` ->
        skipped with a warning;
      * an unknown, non-empty `role` -> raises BoardConfigError immediately (review-cli#422:
        it used to be kept with the generic prompt and a warning, letting a mixed board with
        several typo'd roles quietly collapse onto duplicate generic-prompt seats). An
        OMITTED role (`""` / no `role` key) is unaffected — still the generic prompt, no
        error, no warning;
      * a PRESENT non-empty `board:` whose entries are ALL malformed (no usable
        reviewer survives) -> raise BoardConfigError. This is NOT a silent
        fall-back to DEFAULT_PRESET_BOARD: the user asked for a specific board and got
        nothing parseable, so erroring loudly beats secretly running the paid
        default board."""
    config = load_config() if config is None else config
    if preset:
        return preset_board(preset)
    raw = config.get("board")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_PRESET_BOARD)
    board: list[BoardReviewer] = []
    for entry in raw:
        reviewer = _parse_board_entry(entry)
        if reviewer is not None:
            board.append(reviewer)
    # NOTE: duplicate models across seats are INTENTIONALLY allowed (the show-board path tags
    # per-SEAT, not per-model — a duplicate model in the reserve must read `reserve`, not
    # `pool`; see test_show_board_tags_by_seat_not_model_for_duplicate_models). Capability
    # resolution can make a collision likelier, but de-duping here would override that
    # deliberate, tested behavior — so a board with two identical seats is kept as-is.
    if not board:
        # `board:` was present and non-empty but nothing parsed. Do NOT fall back
        # to the paid DEFAULT_BOARD — error loudly so the user fixes the config.
        raise BoardConfigError(
            f"config.yaml `board:` has {len(raw)} entr"
            f"{'y' if len(raw) == 1 else 'ies'} but none is usable (every entry "
            "is malformed — not a mapping, or missing a 'model' / a resolvable "
            "'capability'). Fix the board entries, or remove the `board:` key to "
            "use the default reviewer board."
        )
    return board


def board_from_models(
    models: list[str],
    config: dict | None = None,
    *,
    preset: str | None = None,
) -> list[BoardReviewer]:
    """Build a priority reviewer board from a config `models:` roster.

    `models:` owns the seat order. A matching config `board:` entry, selected preset, or
    built-in DEFAULT_BOARD seat supplies role/name metadata; unknown models stay usable
    with the generic review lens and a display name derived from the model id. When a
    preset is explicit, it overlays config metadata so preset effort cannot be downgraded
    by a saved board entry.
    """
    metadata: dict[str, BoardReviewer] = {
        reviewer.model: reviewer for reviewer in DEFAULT_BOARD
    }
    raw_board = (config or {}).get("board") if isinstance(config, dict) else None
    if isinstance(raw_board, list):
        for entry in raw_board:
            reviewer = _parse_board_entry(entry)
            if reviewer is not None:
                metadata[reviewer.model] = reviewer
    preset_effort_by_model: dict[str, str | None] = {}
    preset_default_effort: str | None = None
    if preset:
        preset_reviewers = preset_board(preset)
        preset_effort_by_model = {
            reviewer.model: reviewer.effort for reviewer in preset_reviewers
        }
        preset_default_effort = next(
            (reviewer.effort for reviewer in preset_reviewers if reviewer.effort), None
        )
        for reviewer in preset_reviewers:
            base = metadata.get(reviewer.model)
            if base is None:
                metadata[reviewer.model] = reviewer
            else:
                metadata[reviewer.model] = BoardReviewer(
                    model=base.model,
                    role=base.role,
                    display=base.display,
                    effort=reviewer.effort,
                )

    board: list[BoardReviewer] = []
    for model in models:
        reviewer = metadata.get(model)
        preset_model_effort = (
            preset_effort_by_model.get(model, preset_default_effort) if preset else None
        )
        if reviewer is None:
            board.append(
                BoardReviewer(
                    model=model,
                    role="",
                    display=_display_name(model),
                    effort=preset_model_effort,
                )
            )
        else:
            board.append(
                BoardReviewer(
                    model=model,
                    role=reviewer.role,
                    display=reviewer.display,
                    effort=preset_model_effort if preset else reviewer.effort,
                )
            )
    return board


def ensure_min_distinct_roles(
    board: list[BoardReviewer],
    min_roles: int,
    *,
    role_source: Sequence[BoardReviewer] | None = None,
) -> list[BoardReviewer]:
    """Return a board whose first `min_roles` known-role seats have DISTINCT
    roles, so a positional `--pool N` / `select_pool_with_reuse` prefix covers
    the ship floor. Duplicate-role seats are kept AFTER that prefix (reserve).
    Role-less seats are repaired onto unused source roles. Never grows the
    distinct-role prefix past `min_roles` (does not pad to the full board).
    """
    if not board or min_roles <= 0:
        return list(board)
    source = (
        list(role_source) if role_source is not None else list(DEFAULT_PRESET_BOARD)
    )
    out = [replace(r) for r in board]
    donor = out[0]
    source_roles = [r.role for r in source if r.role_lens]

    used_now = {s.role for s in out if s.role_lens}
    repair_idx = 0
    for i, seat in enumerate(out):
        if seat.role_lens:
            continue
        while repair_idx < len(source_roles) and source_roles[repair_idx] in used_now:
            repair_idx += 1
        if repair_idx >= len(source_roles):
            break
        new_role = source_roles[repair_idx]
        repair_idx += 1
        out[i] = replace(seat, role=new_role)
        used_now.add(new_role)

    prefix: list[BoardReviewer] = []
    used: set[str] = set()
    leftovers: list[BoardReviewer] = []
    for seat in out:
        if seat.role_lens and seat.role not in used and len(prefix) < min_roles:
            prefix.append(seat)
            used.add(seat.role)
        else:
            leftovers.append(seat)

    for role in source_roles:
        if len(prefix) >= min_roles:
            break
        if role in used:
            continue
        prefix.append(replace(donor, role=role))
        used.add(role)
    return prefix + leftovers


def _split_models(raw: list[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(_expand_alias(part))
    return values


def resolve_review_board(
    config: dict, *, preset: str | None = None
) -> tuple[list[BoardReviewer], str, str | None]:
    """`(board, source, preset)` for a `review diff` run with no `-m`: an explicit
    `preset` > config `models:` > config `board:` > DEFAULT_PRESET, the precedence the
    CLI's dispatch uses. `source` names which one won (`preset:<name>`,
    `config.yaml (models:)`, `config.yaml (board:)`); `preset` is the preset whose board
    it is, or None for a config roster. Raises BoardConfigError like `load_board`."""
    if preset:
        return load_board(config, preset=preset), f"preset:{preset}", preset
    config_models = _split_models(config.get("models") or [])
    if config_models:
        return board_from_models(config_models, config), "config.yaml (models:)", None
    if isinstance(config.get("board"), list) and config.get("board"):
        return load_board(config), "config.yaml (board:)", None
    return (
        load_board(config, preset=DEFAULT_PRESET),
        f"preset:{DEFAULT_PRESET}",
        DEFAULT_PRESET,
    )
