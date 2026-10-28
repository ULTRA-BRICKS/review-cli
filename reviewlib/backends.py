"""Model backends: codex / gemini / claude / opencode, and backend resolution.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). Each backend builds a TEXT payload and
returns a ReviewResult. `_ANNOUNCE_LOGS` is a module-global toggled on by the
panel modes so streamed calls print their live-log path to stderr.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from .backend_facade import forwarding as _forwarding
from . import backend_api as _backend_api
from . import backend_claude as _backend_claude
from . import backend_cooldown as _backend_cooldown
from . import backend_dispatch as _backend_dispatch
from . import backend_omp as _backend_omp
from . import backend_omp_registry as _backend_omp_registry
from . import backend_opencode as _backend_opencode
from . import backend_provider as _backend_provider
from . import backend_rest as _backend_rest
from typing import Callable, Iterator

from .config import _agentic as _agentic
from . import availability
from .install import install_codex_recursion_guard
from .model_behavior import true_silence_timeout_seconds
from . import account_budget, admission, omp_accounts
from .portable import lock_file, unlock_file
from .process import TIMEOUT_KIND_STALL, _run as _run, _run_streamed as _run_streamed, _safe_log_header as _safe_log_header, git_repo_env, run_deadline_remaining, strip_control_sequences, write_sidecar_log
from .seat_cooldown import (
    CAUSE_AUTH,
    CAUSE_QUOTA,
    CAUSE_STALL,
    CAUSE_UNAVAILABLE,
    CHRONIC_QUOTA_MARKERS,
    active_cooldown,
    clear_cooldown,
    record_cooldown,
)
from .usage_limits import (
    spawn_refuse_reason,
    usage_percent_for_model,
)

GEMINI_ENV_FALLBACKS = (
    Path.home() / ".config" / "review-cli" / ".env",
    Path("/Users/ultra/xp/ExpenseSyncBot/.env"),
)

# When True, streamed backend calls print their live-log path to stderr at start so
# the user knows what to `tail -f`. Enabled by the panel modes (--just-ask/--quorum/
# --brainstorm); the plain single-diff review path keeps stderr quiet.
_ANNOUNCE_LOGS = False


@dataclass(frozen=True)
class ReviewResult:
    model: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    # Real token usage, set ONLY at the REST call sites that parse a provider's own
    # usage payload (gemini/openai-shape/anthropic below). Every other construction
    # site (every CLI/agentic backend, every error path) leaves these at the default
    # 0 — "no usage data for this call", never scraped from stdout text, so an
    # agentic backend that happens to quote a usage-shaped line in its own output
    # (a real cross-contamination case review-cli hit before, see tokenstats.py's
    # `_REST_USAGE_BACKENDS`) can never be misattributed here.
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Set by provider failover (reviewlib.panel.mark_substitution, review-cli#594) when the
    # verdict came from a DIFFERENT executor than the one the seat asked for — e.g. a dead
    # `claude:X` answered by `omp:anthropic/X`: `served_by` is the executor that answered,
    # `substitution` the one-line human account of what failed, why, and who answered.
    # Empty on every direct call.
    served_by: str = ""
    substitution: str = ""
    # The account that answered, for a seat that rotates through several accounts of one
    # provider (omp's Anthropic accounts, review-cli#637): the account, and any that
    # failed before it. Empty when the backend has a single implicit account.
    account: str = ""


# A backend that passes the prompt as argv caps out near ARG_MAX (~1 MB): opencode
# passes the message as argv. Feeding via stdin (claude/codex) removes that ceiling
# for them, but opencode has no stdin path — so warn well before it, loudly not
# silently.
_PAYLOAD_WARN_BYTES = 600_000


# Guards `_ensure_codex_recursion_guard` so the (cheap but non-zero: stat + maybe
# write) install check runs at most ONCE per process, not once per codex seat/round.
_codex_recursion_guard_ensured = False
_codex_recursion_guard_lock = threading.Lock()


# Every capability the read-only reviewer agent MUST deny. This is the security boundary
# the whole agentic-opencode path relies on: opencode may READ project files under this
# agent, but every one of these must be `deny` so it can never mutate the repo, run a
# shell command, or hit the network. This list mirrors opencode's mutating/egress
# permission keys as of the version this CLI targets.
#
# SCOPE / LIMITATION (review-cli#40 review finding): the validator's guarantee is "none of
# THESE keys grants, and nothing PRESENT grants". It canNOT defend against a FUTURE
# opencode capability that (a) is not in this list and (b) defaults to permissive when
# absent — such a key would need to be added here AND to the canonical file. So this set
# must be kept in sync with opencode's permission surface; it is the floor, not a proof
# that opencode exposes nothing else. (opencode has no documented deny-by-default/wildcard
# we can lean on instead, so an explicit, maintained key list is the available mitigation.)
#
# DELIBERATE WRITE/EXEC EXCEPTION — DO NOT clamp qa to this boundary. The `review qa` mode
# (the agent-as-tester, `reviewlib/qa/executor.py`) is review-cli's ONE intentionally
# UN-CAGED agent: a tester MUST run bash + write to bring a SUT up and drive it. It does
# NOT ride this read-only path, does NOT call `_ensure_opencode_readonly_agent`, and spawns
# claude/codex with WRITE/EXEC enabled — by design (review-qa.md §1/§9). Its SUT-MUTATION
# blast radius is fenced by running inside a throwaway `git worktree` of the SUT (see
# `IsolatedSut`) — that bounds writes to a disposable tree, but it is NOT an OS sandbox (an
# un-caged shell can still read elsewhere / hit the network). If a future hardening pass
# tightens the read-only boundary, leave qa's write/exec spawn alone — "restoring" a
# read-only flag there silently neuters the tester.
_READONLY_AGENT_DENIED_PERMISSIONS = (
    "bash",
    "edit",
    "write",
    "webfetch",
    "task",
    "todowrite",
    "websearch",
    "lsp",
    "skill",
)


# The canonical, safe read-only-reviewer agent. SINGLE SOURCE OF TRUTH: the permission
# block is GENERATED from `_READONLY_AGENT_DENIED_PERMISSIONS` (not duplicated as a
# literal), so the validator (which reads that tuple) and the writer can never drift — a
# deny key added to the tuple is both enforced AND written. Both the "create when missing"
# and "rewrite a permissive/tampered one" paths write THIS exact content.
def _build_readonly_agent_markdown() -> str:
    perm_lines = "\n".join(
        f"  {name}: deny" for name in _READONLY_AGENT_DENIED_PERMISSIONS
    )
    return (
        "---\n"
        "description: Read-only code reviewer for diff inspection.\n"
        "mode: primary\n"
        "permission:\n"
        f"{perm_lines}\n"
        "---\n"
        "You are a read-only code reviewer. Do not edit files, write files, run\n"
        "shell commands, or ask questions. Return only actionable findings.\n"
    )


_READONLY_AGENT_MARKDOWN = _build_readonly_agent_markdown()


# Project-local opencode config paths that opencode merges when run with `--dir <cwd>`.
# A reviewed repo that ships ANY of these can REDEFINE the `read-only-reviewer` agent
# (or the global permission rules) and FLIP its deny→allow — verified: a repo-local
# `.opencode/agent/read-only-reviewer.md` with `permission: { write: allow, … }` (or an
# `agent:` block in a root opencode.json/jsonc) wins over our global agent, and neither
# OPENCODE_DISABLE_PROJECT_CONFIG nor OPENCODE_PERMISSION nor an inline OPENCODE_CONFIG
# agent suppresses it. So the read-only guarantee would depend on the repo under review.
# We refuse to run agentically in such a repo and fall back to the isolated temp-dir
# (diff-only) posture instead — the safe default for a potentially adversarial repo.
_OPENCODE_PROJECT_CONFIG_NAMES = (".opencode", "opencode.json", "opencode.jsonc")


# opencode's agentic seats (esp. `oc:zai/glm-5.2`) can STALL — hang at ~0% CPU
# producing ZERO output for the entire call — when the underlying provider's quota is
# exhausted (review-cli#153/#159/#179): no error, no partial answer, just silence until
# killed. That is a different failure signature from a backend that is legitimately
# still thinking (some agentic seats take ~15 minutes between chunks): "never produced
# a single byte" can be detected far sooner than "went quiet after producing output",
# which still needs the generous default idle window.
#
# Alex, 2026-08-14 (design directive): "Некоторые модели могут думать молча но наверно
# 20 минут это перебор. И если известно что модель думает вслух и 5 минут она ничего не
# выдает надо ретраить до 3 раз а потом скипать" — a seat that's expected to stream
# output and produces NOTHING for 5 minutes is a stall signal; retry up to 3 times (a
# transient network hiccup should not permanently sideline a seat), and only after 3
# consecutive stalls skip the seat for the rest of this run (cooldown-cached so the
# NEXT invocation doesn't pay for a doomed dispatch either — review-cli#187 fold-in:
# opencode's cooldown bucket is `access_method="opencode"`, kept separate from a
# model's direct keyed-HTTP route, since the two access methods can fail independently
# even for literally the same underlying model/provider quota).
_OPENCODE_STALL_SECONDS_ENV = "REVIEW_OPENCODE_STALL_SECONDS"
_OPENCODE_DEFAULT_STALL_SECONDS = 300  # 5 minutes, per Alex's explicit spec above
# 3 TOTAL attempts (the initial dispatch + 2 retries), not 3 retries after the first —
# pinned by test_opencode_exhausting_stall_retries_records_a_cooldown. Bounds worst-case
# wall-clock at `_OPENCODE_MAX_STALL_RETRIES * _OPENCODE_DEFAULT_STALL_SECONDS` (~15
# minutes) rather than ~20 for the "3 retries after the first" reading.
_OPENCODE_MAX_STALL_RETRIES = 3
_OPENCODE_ACCESS_METHOD = "opencode"
_OPENCODE_STALL_MODELS_ENV = "REVIEW_OPENCODE_STALL_MODELS"
# The default board also runs Kimi/Qwen/DeepSeek AGENTIC seats through opencode
# (reviewlib/config.py) besides zai/glm — there is no evidence any of THOSE share the
# zero-output hang failure mode, so the watchdog stays scoped to the one confirmed-bad
# route rather than blanket-applied to every opencode dispatch (codex review finding,
# round 2: a blanket liveness bound risks misclassifying another seat's legitimately
# slow start as a stall, retrying it 3x, and wrongly cooling it down).
_OPENCODE_DEFAULT_STALL_MODEL_SUBSTRINGS = ("zai/glm",)


# Oh My Pi (omp) — agentic read-only seat (review-cli#174).
#
# TRANSPORT: omp does NOT read the prompt from stdin (an empty-message error), and passing
# a large prompt+diff as an argv message would hit the same ~1 MB ARG_MAX ceiling opencode
# has. omp message args support `@file` prefixing ("prefix files with @"), so the payload
# is written to a private temp dir (NEVER inside the reviewed repo) and passed as
# `@<path>`, which dodges ARG_MAX entirely (verified live against omp v17).
#
# READ-ONLY CAGE — three layers, each VERIFIED live against omp v17.1.8 (review of #174):
#   1. `--tools read,grep,glob` restricts the built-in tool set to the read-only subset
#      (no bash/edit/write — asked to run a shell command, the caged seat answers it has
#      no bash tool). `--no-extensions` + `--no-skills` + `--no-session` keep the run
#      free of discovered extensions/skills and ephemeral.
#   2. NEUTRAL CWD + SANITIZED HOME + `--add-dir <repo>`: omp discovers and EXECUTES
#      project-shipped code from its launch cwd — a hostile repo's `.mcp.json` spawns its
#      MCP server command and `.omp/tools/*.js` is imported at startup, BOTH despite
#      layer 1 (verified: marker files were created). And user-scope MCP servers
#      (~/.claude.json, cursor/vscode mcp.json, …) stay mounted regardless of cwd — their
#      tools run arbitrary code (verified: node_repl executed JS inside the caged seat).
#      So omp is launched from a fresh empty temp dir (no project discovery) with HOME
#      pointed at an empty subdir of it (no user-scope MCP discovery), while
#      PI_CODING_AGENT_DIR pins omp's real agent dir so provider auth still resolves
#      (verified). The reviewed repo is mounted read-only as a workspace via `--add-dir`
#      — the read/grep/glob tools still open any project file, but nothing in the repo
#      and no user MCP server is ever executed.
#   3. A `--config` overlay disables `fetch` (omp's read tool accepts https URLs, an
#      outbound exfiltration channel for a prompt-injected seat — verified; with
#      `fetch.enabled: false` the same URL read fails), `tools.xdev` (the `xd://` device
#      transport CARRIES the write/edit/bash device tools around `--tools` — verified:
#      without this a "caged" seat created files; with it the seat reports no write
#      tool), and `mcp.enableProjectConfig` (belt-and-braces behind layer 2).
#
# SCOPE / LIMITATION: the cage bounds what the seat can DO (read-only local tools, no
# egress, no project/user code execution) — it does NOT scope READS: the read tool opens
# any absolute path (e.g. ~/.ssh), and file contents can carry prompt injection, same as
# any agentic reviewer; review transcripts are the containment for that (read-only data
# flow), not the sandbox. The model string after `omp:` is passed to omp's `--model`
# fuzzy selector verbatim (`omp:kimi-code/k3` -> `--model kimi-code/k3`).
_OMP_READONLY_TOOLS = "read,grep,glob"

# The per-run `--config` overlay carrying cage layer 3 (see the comment block above).
_OMP_CAGE_OVERLAY = (
    "# review-cli omp read-only seat (review-cli#174): no outbound network via the read\n"
    "# tool's URL path (fetch backs it), no xd:// device transport (it carries\n"
    "# write/edit/bash around --tools), and never execute project-shipped MCP config.\n"
    "fetch:\n"
    "  enabled: false\n"
    "tools:\n"
    "  xdev: false\n"
    "mcp:\n"
    "  enableProjectConfig: false\n"
)

OMP_SUPPORTED_MODES = ("cli",)  # CLI-only — no omp REST transport exists.

# Cooldown-store access-method key for the omp backend (mirrors _OPENCODE_ACCESS_METHOD
# above): omp's read-only agentic seat is a distinct failure domain from the claude CLI
# transport ("cli"), so it gets its own cooldown namespace rather than reusing that one.
_OMP_ACCESS_METHOD = "omp"


# review-cli#598: removing a seat's sandbox must never cost the seat's answer. omp's
# process lifeline (oh-my-pi f7f04f2) starts a supervisor in its OWN session — outside
# the seat's process group, so the group kill never reaches it — that inherits the seat
# env and, the moment the omp host exits, re-execs the CLI as a reaper. Under the
# from-source launcher (`bun src/cli.ts`) that reaper loads its modules through Bun's
# runtime transpiler cache, which lives under $HOME (the sandbox's `home/`) at
# `Library/Caches/bun/@t@`, and it writes fresh `.pile` files there while the cleanup is
# deleting the tree: a one-shot rmtree then fails with ENOTEMPTY (Errno 66 on macOS). The
# reaper is done within tens of milliseconds, so removal retries briefly, then leaves
# the dir to the OS temp cleaner with a warning instead of raising.
_SANDBOX_REMOVE_ATTEMPTS = 3
_SANDBOX_REMOVE_RETRY_SECONDS = 0.2


@contextlib.contextmanager
def _seat_sandbox(prefix: str) -> Iterator[Path]:
    """A fresh private temp dir, removed best-effort on exit (see
    `_remove_sandbox_best_effort`) — unlike `tempfile.TemporaryDirectory`, a failed
    removal never raises over the result computed inside."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        _remove_sandbox_best_effort(path)


# NOTE: `_anthropic_key` / `_openai_key` were added in 30163c5 ONLY for the REST vision
# path. That path is gone (vision now invokes the agent CLIs — claude/codex — which carry
# their own auth, exactly like review's TEXT backends). Gemini stays the one key-based
# vision exception via `_gemini_key`. The two orphaned helpers are removed with the REST
# adapters; `_resolve_key` stays — `_gemini_key` still uses it.


# --- Backend transport mode (api | cli) ----------------------------------------
# A backend can run as a REST `api` call, a `cli` subprocess, or both. The claude
# backend (PR #8) established the selector: a per-backend `REVIEW_<NAME>_MODE` env
# var forces one variant (else the backend auto-picks). This generalises that ONE
# mechanism so every backend declares its supported modes and resolves the forced
# mode the same way — no second, parallel selector. commandcode and z.ai are
# api-only (no commandcode/z.ai CLI exists); claude supports both; codex/opencode
# are cli-only. `resolve_backend_mode` is the single entry point: it reads
# `REVIEW_<NAME>_MODE`, validates it against the backend's `supported` modes, and
# returns the chosen mode (or `default` when unset). A forced mode the backend does
# NOT support is a hard, explicit error — never a silent fall-through to the wrong
# transport (e.g. forcing `cli` on commandcode must fail loudly, not POST anyway).


# --- OpenAI-compatible keyed HTTP backends (z.ai / commandcode) -----------------
# Both z.ai (Zhipu / GLM) and commandcode expose an OpenAI-compatible
# /chat/completions API. Unlike review_gemini's bespoke `contents`/`parts` shape,
# these speak the standard OpenAI request body ({"model", "messages":[{role,content}]}
# + Authorization: Bearer). The two backends share one request builder so the wire
# shape stays identical; only the endpoint, key, and default model differ.
#
# Both are API-ONLY: no z.ai or commandcode CLI exists on PATH, so a forced
# `cli` mode is rejected by resolve_backend_mode rather than silently POSTing.


# z.ai (Zhipu / GLM) — OpenAI-compatible /chat/completions, Bearer-keyed.
# DEFAULT base is the GLM Coding Plan endpoint (/api/coding/paas/v4): when glm-5.2 was
# the default, only that endpoint served the flagship (the STANDARD /api/paas/v4 endpoint
# topped out at glm-5.1 on its /models catalog). The default is now glm-5.3, which
# answered on the Coding-Plan endpoint on 2026-10-17 (review-cli#645). A standard-plan
# user overrides the base via ZAI_BASE_URL=https://api.z.ai/api/paas/v4
# (and picks a model their plan serves, e.g. ZAI_MODEL=glm-5.1). The GLM-5 line is
# REASONING models — they return message.reasoning_content alongside message.content;
# _parse_openai_choice reads content and falls back to reasoning_content if content
# is empty (a low output budget can leave only the reasoning).
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5.3"


ZAI_SUPPORTED_MODES = ("api",)  # z.ai is REST-only; no z.ai CLI exists.


# commandcode — Command Code's OpenAI-compatible Provider API (API-only).
# The CTO confirmed: "Command code cli нет, есть api key" — there is no commandcode
# CLI, only an API key (format `user_...`). So commandcode is a keyed HTTP backend
# (same OpenAI wire shape as z.ai), NOT a subprocess. Endpoint verified from the
# Command Code Provider API docs (https://commandcode.ai/docs/provider-api):
#   base  https://api.commandcode.ai/provider/v1
#   POST  /chat/completions   (OpenAI/OSS models — what this backend speaks)
#   POST  /messages           (Anthropic models — use review_claude_api instead)
#   auth  Authorization: Bearer $COMMANDCODE_API_KEY   (the same CLI key)
# On /chat/completions the model id is provider-prefixed (e.g. deepseek/deepseek-v4-
# flash); a Claude id there 400s and is directed to /messages, so Anthropic models
# must go through the claude backend (REVIEW_CLAUDE_MODE=api), not here.
# Base URL + model are overridable via COMMANDCODE_BASE_URL / COMMANDCODE_MODEL.
COMMANDCODE_DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
# Default to an OpenAI-shape (provider-prefixed) model the /chat/completions path
# serves. DeepSeek V4 Flash is a cheap, capable default for diff review; override
# with COMMANDCODE_MODEL or a `commandcode:<model>` suffix for anything else.
COMMANDCODE_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
COMMANDCODE_SUPPORTED_MODES = ("api",)  # API-only — no commandcode CLI exists.

# NOTE (HYP-741): the earlier `common-code` placeholder injected DeepSeek's
# `{"thinking":{"type":"disabled"}}` field to reproduce the legacy non-thinking
# `deepseek-chat` default. That field is DeepSeek-API-specific and is NOT sent here:
# the default transport is the Command Code GATEWAY (api.commandcode.ai), which need
# not accept an unknown body field — sending it risks a 400. A user who points
# COMMANDCODE_BASE_URL at the raw DeepSeek endpoint and wants the non-thinking mode
# can pass it explicitly via the model id; we don't inject a provider-specific field
# behind their back onto a gateway. `_openai_compatible_request`'s generic
# `extra_body` hook stays available for any future provider that needs it.


# OpenRouter — an OpenAI-compatible API AGGREGATOR (https://openrouter.ai) that fronts 400+
# models across providers (anthropic/openai/google/meta/...) behind ONE key and ONE
# OpenAI-shaped /chat/completions endpoint. So it is a keyed-HTTP backend exactly like z.ai
# and commandcode, NOT a subprocess (no OpenRouter CLI exists — API-only).
#   base   https://openrouter.ai/api/v1
#   POST   /chat/completions      (OpenAI request/response shape)
#   auth   Authorization: Bearer $OPENROUTER_API_KEY   (keys look like sk-or-v1-...)
# The wire model id is the OpenRouter slug `<provider>/<model>` (e.g. anthropic/claude-3.5-
# sonnet, openai/gpt-4o), optionally with a `:variant` suffix (`:free`, `:beta`, `:nitro`).
# A bare `openrouter` seat defaults to OpenRouter's OWN auto-router (`openrouter/auto`),
# which picks a suitable model server-side — a never-stale default, since the concrete pick
# is OpenRouter's job, not a literal we'd have to bump. Override the model with an
# `openrouter:<slug>` seat suffix or OPENROUTER_MODEL, and the base with OPENROUTER_BASE_URL.
OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_SUPPORTED_MODES = ("api",)  # API-only — no OpenRouter CLI exists.


# A non-default User-Agent: some Anthropic-compatible gateways (e.g. CommandCode,
# behind Cloudflare) 403 the bare urllib UA with "error code: 1010".
_ANTHROPIC_UA = "review-cli (anthropic-compatible client)"
_CLAUDE_REVIEW_SYSTEM = (
    "You are running inside review-cli in a headless read-only diff review. "
    "Do not use tools, inspect files, ask for permissions, or plan tool work. "
    "Answer only from the prompt and diff supplied by the user."
)
_CLAUDE_IMAGE_REVIEW_SYSTEM = (
    "You are running inside review-cli in a headless read-only visual diff review. "
    "Use the Read tool only for image file paths explicitly listed in the RAW VISUAL "
    "ATTACHMENT section. Do not inspect the source repository or any other local files. "
    "Answer from the prompt, diff, and those attached images only."
)


# review-cli#588: a Claude login that is gone (logged out, OAuth expired/revoked) on ONE
# registered account. The account loops below move on to the next account for this, the
# same as for a chronic quota result — on 2026-09-29 the `current` account (account-2) was
# logged out while account-0/1 were fine, and stopping at the first account killed every
# claude:* seat. Only matched on a non-zero exit, so a real review mentioning these words
# is never read as an auth failure.
_ACCOUNT_AUTH_FAILURE = re.compile(
    r"failed to authenticate|oauth (?:session|token) (?:has )?expired|not logged in"
    r"|please run /login|invalid api key|authentication_error|log in again",
    re.IGNORECASE,
)


# The rc=0 administrative sentinel shapes ("Claude Fable 5 is currently unavailable.
# Learn more: ..."). This is now the SINGLE canonical source — panel.py imports these
# two names FROM backends (this direction only; backends never imports panel, so no
# circularity), instead of each module keeping its own copy.
#
# glm/Opus review finding (2026-08 seat-cooldown feature, round 2): a PRIOR version of
# this module kept its own private one-marker literal ("is currently unavailable") that
# the comment claimed "must stay in sync with panel.py's _UNAVAILABLE_MARKERS" — but it
# recognised only 1 of panel's 4 marker phrases ("is temporarily unavailable", "model is
# unavailable", "currently not available" were invisible here). Every OTHER consumer of
# those 4 markers (`panel.result_is_usable`, `retry.classify_failure` via
# `_is_rc0_sentinel`, the dashboard's HEALTH_PAYWALL) already treats all four as
# equally chronic — a Fable response using any of the other three wordings was
# therefore correctly failover-replaced and classified paywall EVERYWHERE except here,
# so `record_cooldown` silently never fired for it and the seat kept paying for a real
# dispatch on every single invocation for exactly the failure shape this feature exists
# to stop. Defining the tuple once, here, and having every consumer import it makes a
# future wording drift impossible instead of merely commented-against.
_UNAVAILABLE_MARKERS = (
    "is currently unavailable",
    "is temporarily unavailable",
    "model is unavailable",
    "currently not available",
)
# The session/usage/weekly-limit quota markers live in `reviewlib.seat_cooldown`
# (single source shared with the dashboard's retry-event classifier).
_CHRONIC_QUOTA_MARKERS = CHRONIC_QUOTA_MARKERS
# The two reasons `_chronic_unavailable_reason` returns, and the typed cooldown cause each
# one proves (review-cli#472): a session-limit/usage-credits notice is the account's quota
# running out; the administrative sentinel only says the model is unavailable, which is
# not proof of quota exhaustion.
_QUOTA_COOLDOWN_REASON = "session limit / usage credits"
_SENTINEL_COOLDOWN_REASON = "unavailable sentinel"
_CHRONIC_REASON_CAUSE = {
    _QUOTA_COOLDOWN_REASON: CAUSE_QUOTA,
    _SENTINEL_COOLDOWN_REASON: CAUSE_UNAVAILABLE,
}


# The administrative sentinel is only trustworthy when the WHOLE body is short (a
# one-liner notice) — a real, long review that happens to mention availability must
# never be cached as a chronic failure. Shared by panel.result_is_usable and
# retry._is_rc0_sentinel too (both import this name), so all three agree on the bound.
_UNAVAILABLE_MAX_LEN = 400
# Marks the command of a skip caused by the configured account budget (review-cli#696): it is
# a policy decision re-made from fresh usage on every dispatch, so it must never be recorded
# as a seat cooldown (`review_claude`).
_BUDGET_SKIP_SUFFIX = ": account budget hold"


# seat_cooldown's TTL is a blind timer -- once a quota-exhaustion failure escalates to
# the 8h cap (seat_cooldown._ESCALATION_SCHEDULE), the seat stays excluded for the full
# window even if the 5-hour/weekly usage window that actually caused it resets in
# minutes. The only existing way to un-stick it sooner was the documented
# `$REVIEW_SEAT_COOLDOWN_SECONDS<=0` hatch -- a human has to notice and manually export
# it (in practice: re-running `review` after manually clearing/waiting). `usage_limits`
# already collects the live signal that answers "has it actually recovered" (the same
# harness-card/tg-ctl snapshot `spawn_refuse_reason` reads for a seat with NO
# cooldown) -- `_cooldown_after_recovery_probe` below consults it BEFORE honoring a
# cooldown, so a seat that recovered early is silently re-included on the very next
# dispatch instead of waiting out the clock. No daemon, no background polling: a plain
# check-before-use, run once per dispatch decision, at the exact point `active_cooldown`
# was already being read.


# Byte-exact match for the numeric codes retry.py's `_TRANSIENT_PATTERNS` regex
# targets (`\b429\b`, `\b5(?:0[0234]|2[0-4]|29)\b`): 429, 500/502/503/504, 520-524,
# 529 — deliberately NOT 501 (retry.py classifies that SEAT_FATAL, not transient) or
# any other 5xx. Used by `_looks_transient` to check `ReviewResult.returncode`
# directly (the literal HTTP status for `review_claude_api`'s `HTTPError` path,
# `rc = exc.code or 1`) rather than re-deriving it from a regex on `str(rc)`.
_TRANSIENT_HTTP_STATUSES = frozenset(
    {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)

# Byte-exact match for the numeric codes retry.py's `_SEAT_FATAL_PATTERNS` regex
# targets AS BARE STATUS CODES (`\b401\b`, `\b403\b`, `\b501\b`) — mirrors
# `_TRANSIENT_HTTP_STATUSES`'s same rationale, for the seat-fatal side. k3 review
# finding (review-cli#fable-seat-reliability, round 8): `_is_seat_fatal` is a TEXT-only
# check, so a gateway that conveys the status ONLY via `ReviewResult.returncode` (not
# as digits anywhere in the body) — concrete repro: `returncode=401, stderr='{"error":
# {"message": "model is currently unavailable to this account"}}'` — never matches ANY
# `_SEAT_FATAL_PATTERNS` text pattern, so it falls through as "not seat-fatal" and gets
# cached as an hours-long chronic cooldown for what is really just a bad key. Checked
# directly against `returncode`, the same way `_TRANSIENT_HTTP_STATUSES` already is.
_SEAT_FATAL_HTTP_STATUSES = frozenset({401, 403, 501})


# The exact flag set `_ensure_workspace_trusted` seeds. An entry holding ONLY these (a subset is
# fine) is one review created and nothing else enriched — safe to reap. Any EXTRA key means a
# real claude session touched it, so we leave it.
_REVIEW_SEEDED_TRUST_KEYS = frozenset(
    {"hasTrustDialogAccepted", "hasCompletedProjectOnboarding"}
)


@lru_cache(maxsize=8)
def _claude_cli_supports_effort(binary: str) -> bool:
    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    help_text = f"{proc.stdout}\n{proc.stderr}"
    return "--effort" in help_text


# --- Fake backend (TEST-ONLY, env-gated) -------------------------------------------
# `REVIEW_FAKE_BACKEND=1` replaces EVERY real backend with a deterministic in-process
# responder — NO network, NO CLI subprocess, NO API key. It exists so the e2e tests
# (tests/test_e2e_resume.py) can spawn the REAL `review` CLI as a subprocess, kill it
# mid-run, and resume it, exercising the genuine cli.py -> modes/brainstorm -> panel
# path while ONLY the leaf model call is faked. OFF by default: when the env var is unset
# `resolve_backend` returns the real backends unchanged, so production behaviour is
# untouched (the e2e/round-trip unit tests prove this — they don't set the var).
#
# It is wired at `resolve_backend` (the single chokepoint every run_panel / run_moderator
# call funnels through), so the fake is the ONLY fake in the whole stack — argument
# parsing, mode dispatch, the round loop, the discussion-log writer, the parser, and the
# resume seeding all run for real.


# Providers known to be DEAD — a default model id must never route through one. Right now
# that is just `fireworks`: the old `oc:fireworks/.../kimi-k2p6-turbo` default ran on the
# suspended `glide` account (review-cli#25). This is a DENYLIST, not an allowlist, on
# purpose: the #25 guard pairs it with a real-routing check (`_match_named_backend` must
# recognize the id — see `default_routes_live`), so the two halves are "the id actually
# routes to a named backend" AND "its underlying provider is not a known-dead one". A
# denylist can't go stale the way a hand-maintained live-allowlist would (every NEW live
# provider would have to be remembered there); when another provider dies, add it here.
_DEAD_PROVIDERS = frozenset({"fireworks"})

# Providers reachable ONLY through opencode's agentic `oc:`/`opencode:` transport: no
# diff-only REST backend exists for them, and (unlike zai/commandcode) opencode has no
# bare/colon-form default-model resolution for them, so `_match_named_backend` must NOT
# grow a bare-token branch (a `-m xai` / `-m xai:<model>` route would hand opencode the
# wrong selector). `default_routes_live` treats membership here as the ONLY thing this set
# decides: the under-transport provider counts as a named route. Selector well-formedness
# is checked for EVERY `oc:` id by `_oc_selector_is_well_formed`, not here.
_AGENTIC_ONLY_PROVIDERS = frozenset({"xai"})


_OC_TRANSPORT_PREFIXES = ("oc:", "opencode:")


# ---------------------------------------------------------------------------
# opencode per-provider auth probe helpers (review-cli#94)
# ---------------------------------------------------------------------------

# Env var names that override the default opencode file paths in tests.
_OC_AUTH_FILE_ENV = "OC_AUTH_FILE"
_OC_CONFIG_FILE_ENV = "OC_CONFIG_FILE"

# Providers that run locally and need no API key.
_OC_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio"})

# Env vars for KNOWN providers.  Kept deliberately minimal — only providers
# whose env var name is well-established and unlikely to change.  Unknown
# providers fall through to a conservative True (opencode may handle them).
_OC_PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    # commandcode is an opencode custom provider in this ecosystem. Deliberately do NOT
    # treat review-cli's COMMANDCODE_API_KEY as opencode auth: that key powers the direct
    # diff-only `commandcode:` REST backend, while `oc:commandcode/...` must be configured in
    # opencode itself. Listing it here with no env aliases makes missing opencode provider
    # auth a startup skip instead of a long opencode failure.
    "commandcode": (),
    "fireworks": ("FIREWORKS_API_KEY",),
    # z.ai is likewise an opencode provider for `oc:zai/...`; REVIEW/ZAI_API_KEY powers
    # only the direct diff-only `zai:` REST backend and must not make opencode seats live.
    "zai": (),
    "google": ("GOOGLE_AI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
}

_UNPAID_PROVIDERS_ENV = "REVIEW_UNPAID_PROVIDERS"
_CONFIG_UNPAID_PROVIDERS: frozenset[str] = frozenset()
_PROVIDER_ALIASES = {
    "command-code": "commandcode",
    "command_code": "commandcode",
    "common-code": "commandcode",
    "commoncode": "commandcode",
    "common_code": "commandcode",
    "cc": "commandcode",
    "gemini-api": "gemini",
    "claude-fable-5": "claude",
    "claude-fable-5-1": "claude",
    "z.ai": "zai",
    "zhipu": "zai",
    "glm": "zai",
}


_PAYMENT_PREFLIGHT_PROVIDERS = frozenset({"commandcode", "fireworks"})
_PAYMENT_PREFLIGHT_MARKER_CODES = frozenset({400, 401, 403})
_PAYMENT_PREFLIGHT_MARKERS = (
    "account disabled",
    "account suspended",
    "insufficient balance",
    "insufficient credit",
    "insufficient credits",
    "payment required",
    "subscription required",
    "unpaid",
)
_PAYMENT_PREFLIGHT_PROVIDER_WIDE_MARKERS = (
    "account disabled",
    "account suspended",
    "insufficient balance",
    "insufficient credit",
    "insufficient credits",
    "payment required",
    "unpaid",
)
_FIREWORKS_DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
_PROVIDER_PREFLIGHT_TIMEOUT_SECONDS = 2.0
_PAYMENT_PREFLIGHT_PROVIDER_SCOPE = "*"
_PAYMENT_PREFLIGHT_CACHE: dict[tuple[str, str, str, str], tuple[bool, int | None]] = {}
_PAYMENT_PREFLIGHT_CACHE_LOCK = threading.Lock()


# opencode auth.json fields that are descriptive metadata, never credential material.
# Excluded from `_oc_auth_has_provider`'s unknown-auth-type fallback so a future entry
# with only a label/account and no token is not read as a credential. The two KNOWN shapes
# ("api"/"oauth") are matched by exact field name and never reach that fallback; this
# denylist cannot be exhaustive against an auth shape that does not exist yet.
_OC_AUTH_METADATA_FIELDS = frozenset(
    {"type", "expires", "account", "note", "notes", "url", "email", "label", "name"}
)


# review-cli#636: the omp an omp seat runs, when not the `omp` on PATH: $REVIEW_OMP_BIN,
# else config.yaml `omp_bin:` (set once by the CLI after load_config, like
# `configure_unpaid_providers`).
_OMP_BIN_ENV = "REVIEW_OMP_BIN"
_CONFIG_OMP_BIN: str | None = None


# review-cli#641: config.yaml `omp_accounts:` (exclude list + usage thresholds for omp's
# Anthropic accounts), set once by the CLI after load_config like `omp_bin:`; the env
# overrides are read on every use (omp_accounts.account_policy).
_CONFIG_OMP_ACCOUNTS: object = None


# Env var that overrides the omp auth db path in tests (mirrors OC_AUTH_FILE for the
# opencode probe), so the availability tests never touch the real ~/.omp/agent/agent.db.
_OMP_AUTH_DB_ENV = "OMP_AUTH_DB"
# Forward-compatible omp seat pin: when set, omp should resolve only this
# `auth_credentials.id` for openai-codex (not yet read by omp v18.1.16, but
# review-cli passes it so future omp builds can honor per-credential fallback).
_OMP_CREDENTIAL_ID_ENV = "OMP_CREDENTIAL_ID"


# In-process memo for the omp auth probe (glm review of #174): `--show-board` and the
# pool guard probe EVERY omp seat, and a naive probe re-opens the same sqlite db per
# seat. The memo is keyed on the db's (main, -wal) mtimes — WAL writes (a mid-process
# `omp` login/logout) may not touch the main db file — so a credential change is still
# picked up on the very next probe, and one stamp miss hydrates ALL live providers in a
# single read, so N omp seats genuinely cost ONE db read per board pass. A FAILED probe
# (locked/corrupt db) is NOT cached — the next probe retries instead of serving a stale
# "unauthenticated" until the db file happens to change.
_OMP_AUTH_CACHE: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


# review-cli#646: omp also authenticates these providers from an API key in its env
# (`omp --help` lists ZAI_API_KEY / OPENROUTER_API_KEY). review-cli usually holds such a
# key in its own ~/.config/review-cli/.env rather than in the process env, so an
# omp:zai/... seat looked unauthenticated while the key sat on the machine. Provider ->
# (the env var omp reads, the names review-cli resolves it from, primary first).
_OMP_ENV_KEY_PROVIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "zai": ("ZAI_API_KEY", ("ZAI_API_KEY", "ZHIPU_API_KEY")),
    "openrouter": ("OPENROUTER_API_KEY", ("OPENROUTER_API_KEY",)),
}


# omp_env_key memo: board passes probe every omp seat repeatedly, so the .env files are
# re-read only when the process env values or a file's mtime change.
_OMP_ENV_KEY_CACHE: dict[tuple, tuple[str, str] | None] = {}



@_forwarding(_backend_dispatch._which)
def _which(name: str) -> str:
    return _backend_dispatch._which(sys.modules[__name__], name)


@_forwarding(_backend_dispatch._which_optional)
def _which_optional(name: str) -> str | None:
    return _backend_dispatch._which_optional(sys.modules[__name__], name)


@_forwarding(_backend_dispatch._payload)
def _payload(prompt: str, diff: str='') -> str:
    return _backend_dispatch._payload(sys.modules[__name__], prompt, diff)


@_forwarding(_backend_dispatch._claude_api_model)
def _claude_api_model(model: str) -> str:
    return _backend_dispatch._claude_api_model(sys.modules[__name__], model)


@_forwarding(_backend_dispatch._claude_api_command)
def _claude_api_command(model: str) -> str:
    return _backend_dispatch._claude_api_command(sys.modules[__name__], model)


@_forwarding(_backend_dispatch._prompt_with_effort)
def _prompt_with_effort(prompt: str, effort: str | None) -> str:
    return _backend_dispatch._prompt_with_effort(sys.modules[__name__], prompt, effort)


@_forwarding(_backend_dispatch._codex_reasoning_effort)
def _codex_reasoning_effort(effort: str | None) -> str | None:
    return _backend_dispatch._codex_reasoning_effort(sys.modules[__name__], effort)


@_forwarding(_backend_dispatch._claude_reasoning_effort)
def _claude_reasoning_effort(effort: str | None) -> str | None:
    return _backend_dispatch._claude_reasoning_effort(sys.modules[__name__], effort)


@_forwarding(_backend_dispatch._opencode_variant)
def _opencode_variant(effort: str | None) -> str | None:
    return _backend_dispatch._opencode_variant(sys.modules[__name__], effort)


@_forwarding(_backend_dispatch._http_error_text)
def _http_error_text(exc: urllib.error.HTTPError) -> str:
    return _backend_dispatch._http_error_text(sys.modules[__name__], exc)


@_forwarding(_backend_dispatch.call_backend)
def call_backend(backend: Callable[..., ReviewResult], model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, *, effort: str | None=None) -> ReviewResult:
    return _backend_dispatch.call_backend(sys.modules[__name__], backend, model, prompt, diff, cwd, timeout, round_no, effort=effort)


@_forwarding(_backend_dispatch._availability_methods)
def _availability_methods(model: str) -> tuple[str, ...]:
    return _backend_dispatch._availability_methods(sys.modules[__name__], model)


@_forwarding(_backend_dispatch.availability_exclusion)
def availability_exclusion(model: str) -> availability.Exclusion | None:
    return _backend_dispatch.availability_exclusion(sys.modules[__name__], model)


@_forwarding(_backend_dispatch.note_availability)
def note_availability(model: str, result: ReviewResult) -> None:
    return _backend_dispatch.note_availability(sys.modules[__name__], model, result)


@_forwarding(_backend_dispatch._availability_skip_result)
def _availability_skip_result(model: str, round_no: int, excluded: availability.Exclusion) -> ReviewResult:
    return _backend_dispatch._availability_skip_result(sys.modules[__name__], model, round_no, excluded)


@_forwarding(_backend_dispatch._admission_skip_result)
def _admission_skip_result(model: str, round_no: int, reason: str) -> ReviewResult:
    return _backend_dispatch._admission_skip_result(sys.modules[__name__], model, round_no, reason)


@_forwarding(_backend_dispatch._call_backend_now)
def _call_backend_now(backend: Callable[..., ReviewResult], model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, *, effort: str | None=None) -> ReviewResult:
    return _backend_dispatch._call_backend_now(sys.modules[__name__], backend, model, prompt, diff, cwd, timeout, round_no, effort=effort)


@_forwarding(_backend_dispatch.review_with_images)
def review_with_images(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, images: tuple[Path, ...]=(), effort: str | None=None) -> ReviewResult:
    return _backend_dispatch.review_with_images(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, images, effort)


@_forwarding(_backend_dispatch._ensure_codex_recursion_guard)
def _ensure_codex_recursion_guard() -> None:
    return _backend_dispatch._ensure_codex_recursion_guard(sys.modules[__name__])


@_forwarding(_backend_dispatch._cwd_is_git_repo)
def _cwd_is_git_repo(cwd: Path) -> bool:
    return _backend_dispatch._cwd_is_git_repo(sys.modules[__name__], cwd)


@_forwarding(_backend_dispatch.review_codex)
def review_codex(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_dispatch.review_codex(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_opencode._frontmatter_yaml)
def _frontmatter_yaml(text: str) -> str | None:
    return _backend_opencode._frontmatter_yaml(sys.modules[__name__], text)


@_forwarding(_backend_opencode._agent_frontmatter)
def _agent_frontmatter(text: str) -> dict | None:
    return _backend_opencode._agent_frontmatter(sys.modules[__name__], text)


@_forwarding(_backend_opencode._permission_value_is_fully_denied)
def _permission_value_is_fully_denied(value: object) -> bool:
    return _backend_opencode._permission_value_is_fully_denied(sys.modules[__name__], value)


@_forwarding(_backend_opencode._agent_is_strictly_readonly)
def _agent_is_strictly_readonly(text: str) -> bool:
    return _backend_opencode._agent_is_strictly_readonly(sys.modules[__name__], text)


@_forwarding(_backend_opencode._ensure_opencode_readonly_agent)
def _ensure_opencode_readonly_agent(_project: Path, _oc_model: str) -> None:
    return _backend_opencode._ensure_opencode_readonly_agent(sys.modules[__name__], _project, _oc_model)


@_forwarding(_backend_opencode._atomic_write_text)
def _atomic_write_text(path: Path, text: str) -> None:
    return _backend_opencode._atomic_write_text(sys.modules[__name__], path, text)


@_forwarding(_backend_opencode._opencode_has_project_config)
def _opencode_has_project_config(cwd: Path) -> bool:
    return _backend_opencode._opencode_has_project_config(sys.modules[__name__], cwd)


@_forwarding(_backend_opencode._opencode_runs_in_repo)
def _opencode_runs_in_repo(cwd: Path) -> bool:
    return _backend_opencode._opencode_runs_in_repo(sys.modules[__name__], cwd)


@_forwarding(_backend_opencode._opencode_model_needs_stall_watchdog)
def _opencode_model_needs_stall_watchdog(model: str) -> bool:
    return _backend_opencode._opencode_model_needs_stall_watchdog(sys.modules[__name__], model)


@_forwarding(_backend_opencode._opencode_stall_seconds)
def _opencode_stall_seconds() -> int | None:
    return _backend_opencode._opencode_stall_seconds(sys.modules[__name__])


@_forwarding(_backend_opencode._opencode_call_stalled)
def _opencode_call_stalled(proc: subprocess.CompletedProcess[str]) -> bool:
    return _backend_opencode._opencode_call_stalled(sys.modules[__name__], proc)


@_forwarding(_backend_opencode._run_opencode_with_stall_retry)
def _run_opencode_with_stall_retry(argv: list[str], *, cwd: Path, timeout: int, round_no: int, header_argv0: str, model: str, true_silence_timeout: int | None=None) -> subprocess.CompletedProcess[str]:
    return _backend_opencode._run_opencode_with_stall_retry(sys.modules[__name__], argv, cwd=cwd, timeout=timeout, round_no=round_no, header_argv0=header_argv0, model=model, true_silence_timeout=true_silence_timeout)


@_forwarding(_backend_opencode._record_true_silence_if_needed)
def _record_true_silence_if_needed(model: str, proc, *, access_method: str) -> None:
    return _backend_opencode._record_true_silence_if_needed(sys.modules[__name__], model, proc, access_method=access_method)


@_forwarding(_backend_opencode.review_opencode)
def review_opencode(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_opencode.review_opencode(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_omp._omp_model_hosts_claude)
def _omp_model_hosts_claude(omp_model: str | None) -> bool:
    return _backend_omp._omp_model_hosts_claude(sys.modules[__name__], omp_model)


@_forwarding(_backend_omp._review_omp_claude_profiles)
def _review_omp_claude_profiles() -> list[str | None]:
    return _backend_omp._review_omp_claude_profiles(sys.modules[__name__])


@_forwarding(_backend_omp._omp_model_hosts_openai_codex)
def _omp_model_hosts_openai_codex(omp_model: str | None) -> bool:
    return _backend_omp._omp_model_hosts_openai_codex(sys.modules[__name__], omp_model)


@_forwarding(_backend_omp._review_omp_openai_codex_credentials)
def _review_omp_openai_codex_credentials() -> list[int | None]:
    return _backend_omp._review_omp_openai_codex_credentials(sys.modules[__name__])


@_forwarding(_backend_omp._review_omp_with_codex_credential_fallback)
def _review_omp_with_codex_credential_fallback(model, omp_model, prompt, diff, cwd, timeout, round_no, effort) -> ReviewResult:
    return _backend_omp._review_omp_with_codex_credential_fallback(sys.modules[__name__], model, omp_model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_omp._review_omp_with_account_fallback)
def _review_omp_with_account_fallback(model, omp_model, prompt, diff, cwd, timeout, round_no, effort) -> ReviewResult:
    return _backend_omp._review_omp_with_account_fallback(sys.modules[__name__], model, omp_model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_omp._omp_selector_is_anthropic)
def _omp_selector_is_anthropic(omp_model: str | None) -> bool:
    return _backend_omp._omp_selector_is_anthropic(sys.modules[__name__], omp_model)


@_forwarding(_backend_omp._omp_account_cooldown_key)
def _omp_account_cooldown_key(account: omp_accounts.OmpAccount) -> str:
    return _backend_omp._omp_account_cooldown_key(sys.modules[__name__], account)


@_forwarding(_backend_omp._omp_accounts_limited_until)
def _omp_accounts_limited_until(accounts: list[omp_accounts.OmpAccount]) -> dict[int, float]:
    return _backend_omp._omp_accounts_limited_until(sys.modules[__name__], accounts)


@_forwarding(_backend_omp._omp_account_failure)
def _omp_account_failure(result: ReviewResult) -> tuple[str, str | None] | None:
    return _backend_omp._omp_account_failure(sys.modules[__name__], result)


@_forwarding(_backend_omp._omp_pool_refusal)
def _omp_pool_refusal(model: str, round_no: int, reason: str) -> ReviewResult:
    return _backend_omp._omp_pool_refusal(sys.modules[__name__], model, round_no, reason)


@_forwarding(_backend_omp._review_omp_with_anthropic_pool)
def _review_omp_with_anthropic_pool(model, omp_model, prompt, diff, cwd, timeout, round_no, effort) -> ReviewResult:
    return _backend_omp._review_omp_with_anthropic_pool(sys.modules[__name__], model, omp_model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_omp.review_omp)
def review_omp(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_omp.review_omp(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_omp._remove_sandbox_best_effort)
def _remove_sandbox_best_effort(path: Path) -> None:
    return _backend_omp._remove_sandbox_best_effort(sys.modules[__name__], path)


@_forwarding(_backend_omp._review_omp_once)
def _review_omp_once(model: str, omp_model: str | None, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int, effort: str | None, profile: str | None, credential_id: int | None=None, anthropic_account: omp_accounts.OmpAccount | None=None, oauth_token: str | None=None, launcher: omp_accounts.OmpLauncher | None=None) -> ReviewResult:
    return _backend_omp._review_omp_once(sys.modules[__name__], model, omp_model, prompt, diff, cwd, timeout, round_no, effort, profile, credential_id, anthropic_account, oauth_token, launcher)


@_forwarding(_backend_rest._read_env_key)
def _read_env_key(env_file: Path, var: str='GEMINI_API_KEY') -> str | None:
    return _backend_rest._read_env_key(sys.modules[__name__], env_file, var)


@_forwarding(_backend_rest._resolve_key)
def _resolve_key(env_names: tuple, fallback_var: str) -> str | None:
    return _backend_rest._resolve_key(sys.modules[__name__], env_names, fallback_var)


@_forwarding(_backend_rest._env_key_files)
def _env_key_files() -> tuple[Path, ...]:
    return _backend_rest._env_key_files(sys.modules[__name__])


@_forwarding(_backend_rest.resolve_key_source)
def resolve_key_source(env_names: tuple, fallback_var: str) -> tuple[str, str, str] | None:
    return _backend_rest.resolve_key_source(sys.modules[__name__], env_names, fallback_var)


@_forwarding(_backend_rest._gemini_key)
def _gemini_key() -> str:
    return _backend_rest._gemini_key(sys.modules[__name__])


@_forwarding(_backend_rest.review_gemini)
def review_gemini(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_rest.review_gemini(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_rest._is_timeout_error)
def _is_timeout_error(exc: BaseException) -> bool:
    return _backend_rest._is_timeout_error(sys.modules[__name__], exc)


@_forwarding(_backend_rest._emit_rest_log)
def _emit_rest_log(backend: str, argv0: str, *, round_no: int, returncode: int, stdout: str, stderr: str, started: datetime | None=None, timed_out: bool=False, timeout_secs: int | None=None, prompt_tokens: int=0, output_tokens: int=0) -> None:
    return _backend_rest._emit_rest_log(sys.modules[__name__], backend, argv0, round_no=round_no, returncode=returncode, stdout=stdout, stderr=stderr, started=started, timed_out=timed_out, timeout_secs=timeout_secs, prompt_tokens=prompt_tokens, output_tokens=output_tokens)


@_forwarding(_backend_rest._mode_env_var)
def _mode_env_var(name: str) -> str:
    return _backend_rest._mode_env_var(sys.modules[__name__], name)


@_forwarding(_backend_rest.resolve_backend_mode)
def resolve_backend_mode(name: str, supported: tuple[str, ...], default: str) -> str:
    return _backend_rest.resolve_backend_mode(sys.modules[__name__], name, supported, default)


@_forwarding(_backend_rest._parse_openai_choice)
def _parse_openai_choice(payload: object) -> str:
    return _backend_rest._parse_openai_choice(sys.modules[__name__], payload)


@_forwarding(_backend_rest._valid_token_count)
def _valid_token_count(count: object) -> bool:
    return _backend_rest._valid_token_count(sys.modules[__name__], count)


@_forwarding(_backend_rest._usage_dict)
def _usage_dict(payload: object, key: str) -> dict:
    return _backend_rest._usage_dict(sys.modules[__name__], payload, key)


@_forwarding(_backend_rest._validated_usage_pair)
def _validated_usage_pair(prompt: object, output: object, *, completed: bool) -> tuple[int, int]:
    return _backend_rest._validated_usage_pair(sys.modules[__name__], prompt, output, completed=completed)


@_forwarding(_backend_rest._parse_openai_usage)
def _parse_openai_usage(payload: object, *, completed: bool=True) -> tuple[int, int]:
    return _backend_rest._parse_openai_usage(sys.modules[__name__], payload, completed=completed)


@_forwarding(_backend_rest._openai_compatible_request)
def _openai_compatible_request(*, model: str, api_model: str, label: str, base_url: str, key: str, prompt: str, diff: str, timeout: int, backend: str, round_no: int=0, extra_body: dict | None=None, extra_headers: dict | None=None) -> ReviewResult:
    return _backend_rest._openai_compatible_request(sys.modules[__name__], model=model, api_model=api_model, label=label, base_url=base_url, key=key, prompt=prompt, diff=diff, timeout=timeout, backend=backend, round_no=round_no, extra_body=extra_body, extra_headers=extra_headers)


@_forwarding(_backend_api._zai_key)
def _zai_key() -> str:
    return _backend_api._zai_key(sys.modules[__name__])


@_forwarding(_backend_api.review_zai)
def review_zai(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_api.review_zai(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_api._commandcode_key)
def _commandcode_key() -> str:
    return _backend_api._commandcode_key(sys.modules[__name__])


@_forwarding(_backend_api.review_commandcode)
def review_commandcode(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_api.review_commandcode(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_api._openrouter_key)
def _openrouter_key() -> str:
    return _backend_api._openrouter_key(sys.modules[__name__])


@_forwarding(_backend_api._openrouter_extra_headers)
def _openrouter_extra_headers() -> dict:
    return _backend_api._openrouter_extra_headers(sys.modules[__name__])


@_forwarding(_backend_api._header_value_is_safe)
def _header_value_is_safe(value: str) -> bool:
    return _backend_api._header_value_is_safe(sys.modules[__name__], value)


@_forwarding(_backend_api.review_openrouter)
def review_openrouter(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_api.review_openrouter(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_api._anthropic_api_config)
def _anthropic_api_config() -> dict | None:
    return _backend_api._anthropic_api_config(sys.modules[__name__])


@_forwarding(_backend_api._anthropic_base_url_from_env)
def _anthropic_base_url_from_env() -> str | None:
    return _backend_api._anthropic_base_url_from_env(sys.modules[__name__])


@_forwarding(_backend_api._base_url_hostname)
def _base_url_hostname(base_url: str) -> str:
    return _backend_api._base_url_hostname(sys.modules[__name__], base_url)


@_forwarding(_backend_api._anthropic_gateway_provider)
def _anthropic_gateway_provider(base_url: str) -> str | None:
    return _backend_api._anthropic_gateway_provider(sys.modules[__name__], base_url)


@_forwarding(_backend_api.review_claude_api)
def review_claude_api(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_api.review_claude_api(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_claude._have_claude_cli)
def _have_claude_cli() -> bool:
    return _backend_claude._have_claude_cli(sys.modules[__name__])


@_forwarding(_backend_claude._claude_api_available_for_model)
def _claude_api_available_for_model(model: str) -> bool:
    return _backend_claude._claude_api_available_for_model(sys.modules[__name__], model)


@_forwarding(_backend_claude._claude_gateway_provider_from_env)
def _claude_gateway_provider_from_env() -> str | None:
    return _backend_claude._claude_gateway_provider_from_env(sys.modules[__name__])


@_forwarding(_backend_claude._claude_runtime_gateway_provider)
def _claude_runtime_gateway_provider() -> str | None:
    return _backend_claude._claude_runtime_gateway_provider(sys.modules[__name__])


@_forwarding(_backend_claude._login_failure_line)
def _login_failure_line(result: ReviewResult) -> str | None:
    return _backend_claude._login_failure_line(sys.modules[__name__], result)


@_forwarding(_backend_claude._account_should_advance)
def _account_should_advance(result: ReviewResult) -> bool:
    return _backend_claude._account_should_advance(sys.modules[__name__], result)


@_forwarding(_backend_claude._claude_account_dir)
def _claude_account_dir(config_dir: str | None) -> Path | None:
    return _backend_claude._claude_account_dir(sys.modules[__name__], config_dir)


@_forwarding(_backend_claude._remember_claude_login)
def _remember_claude_login(config_dir: str | None, result: ReviewResult) -> None:
    return _backend_claude._remember_claude_login(sys.modules[__name__], config_dir, result)


@_forwarding(_backend_claude._claude_cli_login_reason)
def _claude_cli_login_reason() -> str | None:
    return _backend_claude._claude_cli_login_reason(sys.modules[__name__])


@_forwarding(_backend_claude._claude_account_candidates)
def _claude_account_candidates() -> list[tuple[str | None, str | None]]:
    return _backend_claude._claude_account_candidates(sys.modules[__name__])


@_forwarding(_backend_claude._redacted_claude_account)
def _redacted_claude_account(email: str | None, config_dir: str | None) -> str:
    return _backend_claude._redacted_claude_account(sys.modules[__name__], email, config_dir)


@_forwarding(_backend_claude._review_claude_cli_with_account_fallback)
def _review_claude_cli_with_account_fallback(model, prompt, diff, cwd, timeout, round_no, *, effort) -> ReviewResult:
    return _backend_claude._review_claude_cli_with_account_fallback(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort=effort)


@_forwarding(_backend_claude.claude_access_method)
def claude_access_method() -> str:
    return _backend_claude.claude_access_method(sys.modules[__name__])


@_forwarding(_backend_claude.review_claude)
def review_claude(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_claude.review_claude(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_cooldown._chronic_cause)
def _chronic_cause(reason: str) -> str | None:
    return _backend_cooldown._chronic_cause(sys.modules[__name__], reason)


@_forwarding(_backend_cooldown._cooldown_after_recovery_probe)
def _cooldown_after_recovery_probe(model: str, *, access_method: str) -> dict | None:
    return _backend_cooldown._cooldown_after_recovery_probe(sys.modules[__name__], model, access_method=access_method)


@_forwarding(_backend_cooldown._looks_transient)
def _looks_transient(haystack: str, returncode: int) -> bool:
    return _backend_cooldown._looks_transient(sys.modules[__name__], haystack, returncode)


@_forwarding(_backend_cooldown._is_seat_fatal)
def _is_seat_fatal(haystack: str, returncode: int) -> bool:
    return _backend_cooldown._is_seat_fatal(sys.modules[__name__], haystack, returncode)


@_forwarding(_backend_cooldown._chronic_unavailable_reason)
def _chronic_unavailable_reason(result: ReviewResult) -> str | None:
    return _backend_cooldown._chronic_unavailable_reason(sys.modules[__name__], result)


@_forwarding(_backend_cooldown._clears_cooldown_history)
def _clears_cooldown_history(result: ReviewResult) -> bool:
    return _backend_cooldown._clears_cooldown_history(sys.modules[__name__], result)


@_forwarding(_backend_cooldown._bounded_cooldown_skip_body)
def _bounded_cooldown_skip_body(model: str, reason: str, remaining: int) -> str:
    return _backend_cooldown._bounded_cooldown_skip_body(sys.modules[__name__], model, reason, remaining)


@_forwarding(_backend_cooldown._usage_limit_skip_result)
def _usage_limit_skip_result(model: str, round_no: int, reason: str, *, backend: str) -> ReviewResult:
    return _backend_cooldown._usage_limit_skip_result(sys.modules[__name__], model, round_no, reason, backend=backend)


@_forwarding(_backend_cooldown._cooldown_skip_result)
def _cooldown_skip_result(model: str, round_no: int, cooldown: dict, *, backend: str) -> ReviewResult:
    return _backend_cooldown._cooldown_skip_result(sys.modules[__name__], model, round_no, cooldown, backend=backend)


@_forwarding(_backend_claude._claude_project_key)
def _claude_project_key(cwd: Path) -> str:
    return _backend_claude._claude_project_key(sys.modules[__name__], cwd)


@_forwarding(_backend_claude._open_trust_lock)
def _open_trust_lock(cfg: Path):
    return _backend_claude._open_trust_lock(sys.modules[__name__], cfg)


@_forwarding(_backend_claude._release_trust_lock)
def _release_trust_lock(lock) -> None:
    return _backend_claude._release_trust_lock(sys.modules[__name__], lock)


@_forwarding(_backend_claude._ensure_workspace_trusted)
def _ensure_workspace_trusted(cwd: Path) -> None:
    return _backend_claude._ensure_workspace_trusted(sys.modules[__name__], cwd)


@_forwarding(_backend_claude._is_review_seeded_trust_entry)
def _is_review_seeded_trust_entry(entry: object) -> bool:
    return _backend_claude._is_review_seeded_trust_entry(sys.modules[__name__], entry)


@_forwarding(_backend_claude._remove_workspace_trust)
def _remove_workspace_trust(cwd: Path) -> None:
    return _backend_claude._remove_workspace_trust(sys.modules[__name__], cwd)


@_forwarding(_backend_claude._claude_cli_binary)
def _claude_cli_binary() -> str:
    return _backend_claude._claude_cli_binary(sys.modules[__name__])


@_forwarding(_backend_claude._claude_cli_argv)
def _claude_cli_argv(binary: str, model: str | None, *, image_dir: Path | None=None, effort: str | None=None) -> list[str]:
    return _backend_claude._claude_cli_argv(sys.modules[__name__], binary, model, image_dir=image_dir, effort=effort)


@_forwarding(_backend_claude._stage_panel_images)
def _stage_panel_images(images: tuple[Path, ...], tmp: Path) -> list[Path]:
    return _backend_claude._stage_panel_images(sys.modules[__name__], images, tmp)


@_forwarding(_backend_claude._prompt_with_panel_images)
def _prompt_with_panel_images(prompt: str, images: list[Path]) -> str:
    return _backend_claude._prompt_with_panel_images(sys.modules[__name__], prompt, images)


@_forwarding(_backend_claude._claude_cli_env)
def _claude_cli_env() -> dict[str, str]:
    return _backend_claude._claude_cli_env(sys.modules[__name__])


@_forwarding(_backend_claude._claude_cli_log_header)
def _claude_cli_log_header(claude_model: str | None) -> str | None:
    return _backend_claude._claude_cli_log_header(sys.modules[__name__], claude_model)


@_forwarding(_backend_claude.review_claude_cli)
def review_claude_cli(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None, config_dir: str | None=None) -> ReviewResult:
    return _backend_claude.review_claude_cli(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort, config_dir)


@_forwarding(_backend_claude._claude_cli_env_for)
def _claude_cli_env_for(config_dir: str | None) -> dict[str, str]:
    return _backend_claude._claude_cli_env_for(sys.modules[__name__], config_dir)


@_forwarding(_backend_claude.review_claude_cli_with_images)
def review_claude_cli_with_images(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, images: tuple[Path, ...]=(), effort: str | None=None, config_dir: str | None=None) -> ReviewResult:
    return _backend_claude.review_claude_cli_with_images(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, images, effort, config_dir)


@_forwarding(_backend_dispatch._fake_backend_enabled)
def _fake_backend_enabled() -> bool:
    return _backend_dispatch._fake_backend_enabled(sys.modules[__name__])


@_forwarding(_backend_dispatch.review_fake)
def review_fake(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int=0, effort: str | None=None) -> ReviewResult:
    return _backend_dispatch.review_fake(sys.modules[__name__], model, prompt, diff, cwd, timeout, round_no, effort)


@_forwarding(_backend_dispatch._match_named_backend)
def _match_named_backend(lowered: str) -> Callable[..., ReviewResult] | None:
    return _backend_dispatch._match_named_backend(sys.modules[__name__], lowered)


@_forwarding(_backend_provider._peel_oc_transport)
def _peel_oc_transport(lowered: str) -> str | None:
    return _backend_provider._peel_oc_transport(sys.modules[__name__], lowered)


@_forwarding(_backend_provider._oc_selector_is_well_formed)
def _oc_selector_is_well_formed(selector: str, provider: str) -> bool:
    return _backend_provider._oc_selector_is_well_formed(sys.modules[__name__], selector, provider)


@_forwarding(_backend_provider.effective_provider)
def effective_provider(model: str) -> str:
    return _backend_provider.effective_provider(sys.modules[__name__], model)


@_forwarding(_backend_provider._canonical_provider)
def _canonical_provider(provider: str) -> str:
    return _backend_provider._canonical_provider(sys.modules[__name__], provider)


@_forwarding(_backend_provider._parse_provider_names)
def _parse_provider_names(raw: object) -> frozenset[str]:
    return _backend_provider._parse_provider_names(sys.modules[__name__], raw)


@_forwarding(_backend_provider.configure_unpaid_providers)
def configure_unpaid_providers(raw: object) -> None:
    return _backend_provider.configure_unpaid_providers(sys.modules[__name__], raw)


@_forwarding(_backend_provider.unpaid_providers)
def unpaid_providers() -> frozenset[str]:
    return _backend_provider.unpaid_providers(sys.modules[__name__])


@_forwarding(_backend_provider.provider_marked_unpaid)
def provider_marked_unpaid(model: str) -> bool:
    return _backend_provider.provider_marked_unpaid(sys.modules[__name__], model)


@_forwarding(_backend_provider._runtime_unpaid_provider)
def _runtime_unpaid_provider(model: str) -> str | None:
    return _backend_provider._runtime_unpaid_provider(sys.modules[__name__], model)


@_forwarding(_backend_provider.runtime_provider_marked_unpaid)
def runtime_provider_marked_unpaid(model: str) -> bool:
    return _backend_provider.runtime_provider_marked_unpaid(sys.modules[__name__], model)


@_forwarding(_backend_provider.runtime_unpaid_provider_error)
def runtime_unpaid_provider_error(model: str) -> str:
    return _backend_provider.runtime_unpaid_provider_error(sys.modules[__name__], model)


@_forwarding(_backend_provider._matched_unpaid_provider)
def _matched_unpaid_provider(model: str, provider: str | None=None) -> str | None:
    return _backend_provider._matched_unpaid_provider(sys.modules[__name__], model, provider)


@_forwarding(_backend_provider.unpaid_provider_error)
def unpaid_provider_error(model: str, provider: str | None=None) -> str:
    return _backend_provider.unpaid_provider_error(sys.modules[__name__], model, provider)


@_forwarding(_backend_provider.unpaid_provider_result)
def unpaid_provider_result(model: str, *, backend: str, command: str, round_no: int=0, started: datetime | None=None, provider: str | None=None) -> ReviewResult | None:
    return _backend_provider.unpaid_provider_result(sys.modules[__name__], model, backend=backend, command=command, round_no=round_no, started=started, provider=provider)


@_forwarding(_backend_provider._provider_preflight_url)
def _provider_preflight_url(provider: str) -> str | None:
    return _backend_provider._provider_preflight_url(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_auth_provider_key)
def _oc_auth_provider_key(provider: str) -> str | None:
    return _backend_provider._oc_auth_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_config_provider_key)
def _oc_config_provider_key(provider: str) -> str | None:
    return _backend_provider._oc_config_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_config_provider_credentials)
def _oc_config_provider_credentials(provider: str) -> tuple[str | None, str | None]:
    return _backend_provider._oc_config_provider_credentials(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_env_provider_key)
def _oc_env_provider_key(provider: str) -> str | None:
    return _backend_provider._oc_env_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_provider_key)
def _oc_provider_key(provider: str) -> str | None:
    return _backend_provider._oc_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._models_url_from_base)
def _models_url_from_base(base_url: str | None, provider: str) -> str | None:
    return _backend_provider._models_url_from_base(sys.modules[__name__], base_url, provider)


@_forwarding(_backend_provider._payment_preflight_credentials)
def _payment_preflight_credentials(model: str, provider: str) -> tuple[str | None, str | None]:
    return _backend_provider._payment_preflight_credentials(sys.modules[__name__], model, provider)


@_forwarding(_backend_provider._payment_preflight_cache_key)
def _payment_preflight_cache_key(provider: str, url: str, key: str, scope: str=_PAYMENT_PREFLIGHT_PROVIDER_SCOPE) -> tuple[str, str, str, str]:
    return _backend_provider._payment_preflight_cache_key(sys.modules[__name__], provider, url, key, scope)


@_forwarding(_backend_provider._is_payment_preflight_denial)
def _is_payment_preflight_denial(code: int | None, body: str) -> bool:
    return _backend_provider._is_payment_preflight_denial(sys.modules[__name__], code, body)


@_forwarding(_backend_provider._payment_denial_cache_scope)
def _payment_denial_cache_scope(model: str, code: int | None, body: str) -> str:
    return _backend_provider._payment_denial_cache_scope(sys.modules[__name__], model, code, body)


@_forwarding(_backend_provider._payment_preflight_reason)
def _payment_preflight_reason(provider: str, model: str, code: int | None) -> str:
    return _backend_provider._payment_preflight_reason(sys.modules[__name__], provider, model, code)


@_forwarding(_backend_provider._cache_payment_preflight_denial)
def _cache_payment_preflight_denial(model: str, code: int | None, body: str='') -> None:
    return _backend_provider._cache_payment_preflight_denial(sys.modules[__name__], model, code, body)


@_forwarding(_backend_provider._cached_payment_preflight_result)
def _cached_payment_preflight_result(provider: str, model: str, url: str, key: str) -> tuple[bool, int | None] | None:
    return _backend_provider._cached_payment_preflight_result(sys.modules[__name__], provider, model, url, key)


@_forwarding(_backend_provider.cached_payment_preflight_unavailable_reason)
def cached_payment_preflight_unavailable_reason(model: str) -> str | None:
    return _backend_provider.cached_payment_preflight_unavailable_reason(sys.modules[__name__], model)


@_forwarding(_backend_provider._provider_payment_preflight_unavailable_reason)
def _provider_payment_preflight_unavailable_reason(model: str) -> str | None:
    return _backend_provider._provider_payment_preflight_unavailable_reason(sys.modules[__name__], model)


@_forwarding(_backend_provider.provider_preflight_result)
def provider_preflight_result(model: str, *, backend: str, command: str, round_no: int=0, started: datetime | None=None) -> ReviewResult | None:
    return _backend_provider.provider_preflight_result(sys.modules[__name__], model, backend=backend, command=command, round_no=round_no, started=started)


@_forwarding(_backend_provider._oc_auth_file)
def _oc_auth_file() -> Path:
    return _backend_provider._oc_auth_file(sys.modules[__name__])


@_forwarding(_backend_provider._oc_config_file)
def _oc_config_file() -> Path:
    return _backend_provider._oc_config_file(sys.modules[__name__])


@_forwarding(_backend_provider._oc_auth_has_provider)
def _oc_auth_has_provider(provider: str) -> bool:
    return _backend_provider._oc_auth_has_provider(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_config_has_provider_key)
def _oc_config_has_provider_key(provider: str) -> bool:
    return _backend_provider._oc_config_has_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_env_has_provider_key)
def _oc_env_has_provider_key(provider: str) -> bool:
    return _backend_provider._oc_env_has_provider_key(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_provider_auth_available)
def _oc_provider_auth_available(provider: str) -> bool:
    return _backend_provider._oc_provider_auth_available(sys.modules[__name__], provider)


@_forwarding(_backend_omp_registry.configure_omp_bin)
def configure_omp_bin(raw: object) -> None:
    return _backend_omp_registry.configure_omp_bin(sys.modules[__name__], raw)


@_forwarding(_backend_omp_registry.configure_omp_accounts)
def configure_omp_accounts(raw: object) -> None:
    return _backend_omp_registry.configure_omp_accounts(sys.modules[__name__], raw)


@_forwarding(_backend_omp_registry._omp_account_policy)
def _omp_account_policy() -> omp_accounts.AccountPolicy:
    return _backend_omp_registry._omp_account_policy(sys.modules[__name__])


@_forwarding(_backend_omp_registry._omp_profiles_policy_reason)
def _omp_profiles_policy_reason(omp_model: str | None) -> str | None:
    return _backend_omp_registry._omp_profiles_policy_reason(sys.modules[__name__], omp_model)


@_forwarding(_backend_omp_registry._omp_launcher)
def _omp_launcher() -> omp_accounts.OmpLauncher:
    return _backend_omp_registry._omp_launcher(sys.modules[__name__])


@_forwarding(_backend_omp_registry._omp_agent_dir)
def _omp_agent_dir(profile: str | None=None) -> Path:
    return _backend_omp_registry._omp_agent_dir(sys.modules[__name__], profile)


@_forwarding(_backend_omp_registry._omp_auth_db)
def _omp_auth_db() -> Path:
    return _backend_omp_registry._omp_auth_db(sys.modules[__name__])


@_forwarding(_backend_omp_registry._omp_provider_from_model)
def _omp_provider_from_model(model: str) -> str | None:
    return _backend_omp_registry._omp_provider_from_model(sys.modules[__name__], model)


@_forwarding(_backend_omp_registry._omp_db_stamp)
def _omp_db_stamp(db: Path) -> tuple[int, int] | None:
    return _backend_omp_registry._omp_db_stamp(sys.modules[__name__], db)


@_forwarding(_backend_omp_registry._omp_auth_probe)
def _omp_auth_probe(db: Path) -> frozenset[str] | None:
    return _backend_omp_registry._omp_auth_probe(sys.modules[__name__], db)


@_forwarding(_backend_omp_registry._mtime_ns)
def _mtime_ns(path: Path) -> int | None:
    return _backend_omp_registry._mtime_ns(sys.modules[__name__], path)


@_forwarding(_backend_omp_registry.omp_env_key)
def omp_env_key(provider: str | None) -> tuple[str, str] | None:
    return _backend_omp_registry.omp_env_key(sys.modules[__name__], provider)


@_forwarding(_backend_omp_registry._omp_auth_available)
def _omp_auth_available(provider: str | None) -> bool:
    return _backend_omp_registry._omp_auth_available(sys.modules[__name__], provider)


@_forwarding(_backend_omp_registry._omp_stored_auth)
def _omp_stored_auth(provider: str | None) -> bool:
    return _backend_omp_registry._omp_stored_auth(sys.modules[__name__], provider)


@_forwarding(_backend_provider._oc_provider_from_model)
def _oc_provider_from_model(model: str) -> str | None:
    return _backend_provider._oc_provider_from_model(sys.modules[__name__], model)


@_forwarding(_backend_provider.default_routes_live)
def default_routes_live(model: str) -> bool:
    return _backend_provider.default_routes_live(sys.modules[__name__], model)


@_forwarding(_backend_provider.resolve_backend)
def resolve_backend(model: str) -> Callable[..., ReviewResult]:
    return _backend_provider.resolve_backend(sys.modules[__name__], model)


@_forwarding(_backend_provider.provider_route_name)
def provider_route_name(model: str) -> str:
    return _backend_provider.provider_route_name(sys.modules[__name__], model)


@_forwarding(_backend_provider.is_known_backend_token)
def is_known_backend_token(token: str) -> bool:
    return _backend_provider.is_known_backend_token(sys.modules[__name__], token)


@_forwarding(_backend_provider.backend_available)
def backend_available(model: str) -> bool:
    return _backend_provider.backend_available(sys.modules[__name__], model)


@_forwarding(_backend_provider.backend_unavailable_reason)
def backend_unavailable_reason(model: str, *, honor_exclusions: bool=True) -> str | None:
    return _backend_provider.backend_unavailable_reason(sys.modules[__name__], model, honor_exclusions=honor_exclusions)


# Preserve the existing wildcard/public import surface explicitly.
__all__ = [
    'CAUSE_AUTH',
    'CAUSE_QUOTA',
    'CAUSE_STALL',
    'CAUSE_UNAVAILABLE',
    'CHRONIC_QUOTA_MARKERS',
    'COMMANDCODE_DEFAULT_BASE_URL',
    'COMMANDCODE_DEFAULT_MODEL',
    'COMMANDCODE_SUPPORTED_MODES',
    'Callable',
    'GEMINI_ENV_FALLBACKS',
    'Iterator',
    'OMP_SUPPORTED_MODES',
    'OPENROUTER_DEFAULT_BASE_URL',
    'OPENROUTER_DEFAULT_MODEL',
    'OPENROUTER_SUPPORTED_MODES',
    'Path',
    'ReviewResult',
    'TIMEOUT_KIND_STALL',
    'ZAI_DEFAULT_BASE_URL',
    'ZAI_DEFAULT_MODEL',
    'ZAI_SUPPORTED_MODES',
    'account_budget',
    'active_cooldown',
    'admission',
    'annotations',
    'availability',
    'availability_exclusion',
    'backend_available',
    'backend_unavailable_reason',
    'cached_payment_preflight_unavailable_reason',
    'call_backend',
    'claude_access_method',
    'clear_cooldown',
    'configure_omp_accounts',
    'configure_omp_bin',
    'configure_unpaid_providers',
    'contextlib',
    'dataclass',
    'dataclasses',
    'datetime',
    'default_routes_live',
    'effective_provider',
    'git_repo_env',
    'hashlib',
    'inspect',
    'install_codex_recursion_guard',
    'is_known_backend_token',
    'json',
    'lock_file',
    'lru_cache',
    'math',
    'note_availability',
    'omp_accounts',
    'omp_env_key',
    'os',
    'provider_marked_unpaid',
    'provider_preflight_result',
    'provider_route_name',
    're',
    'record_cooldown',
    'resolve_backend',
    'resolve_backend_mode',
    'resolve_key_source',
    'review_claude',
    'review_claude_api',
    'review_claude_cli',
    'review_claude_cli_with_images',
    'review_codex',
    'review_commandcode',
    'review_fake',
    'review_gemini',
    'review_omp',
    'review_opencode',
    'review_openrouter',
    'review_with_images',
    'review_zai',
    'run_deadline_remaining',
    'runtime_provider_marked_unpaid',
    'runtime_unpaid_provider_error',
    'shutil',
    'socket',
    'spawn_refuse_reason',
    'strip_control_sequences',
    'subprocess',
    'sys',
    'tempfile',
    'threading',
    'time',
    'timezone',
    'true_silence_timeout_seconds',
    'unlock_file',
    'unpaid_provider_error',
    'unpaid_provider_result',
    'unpaid_providers',
    'urllib',
    'usage_percent_for_model',
    'write_sidecar_log',
]
