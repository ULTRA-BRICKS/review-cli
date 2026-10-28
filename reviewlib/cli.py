"""review CLI entry: argparse + dispatch only.

This is the package entry point (`[project.scripts] review = "reviewlib.cli:main"`
and the target of the thin `bin/review` shim). It owns argument parsing, diff
acquisition, model selection, and dispatch to the mode functions. All behaviour
lives in the sibling modules — this file is the thin entry the Stage 0
decomposition was about.
"""

from __future__ import annotations

from .cli_facade import forwarding as _forwarding
from .cli_output import _Tee as _Tee
from . import cli_git_io as _cli_git_io
from . import cli_sessions as _cli_sessions
from . import cli_task_commands as _cli_task_commands
from . import cli_job_commands as _cli_job_commands
from . import cli_accounts as _cli_accounts
from . import cli_task_proof as _cli_task_proof
from . import cli_specweb as _cli_specweb
from . import cli_run_stats as _cli_run_stats
from . import cli_entry_helpers as _cli_entry_helpers
from . import cli_options as _cli_options
from . import cli_help_topics as _cli_help_topics
from . import cli_parsing as _cli_parsing
from . import cli_dispatch as _cli_dispatch
from . import cli_board_output as _cli_board_output
from .cli_constants import (
    EXIT_NOT_A_REPO as EXIT_NOT_A_REPO,
    EXIT_GIT_DIFF_FAILED as EXIT_GIT_DIFF_FAILED,
    EXIT_QA_NO_SUITES as EXIT_QA_NO_SUITES,
    EXIT_QA_NO_ENV as EXIT_QA_NO_ENV,
    EXIT_QA_SUT_BOOT_FAILED as EXIT_QA_SUT_BOOT_FAILED,
    EXIT_QA_ENV_UNHEALTHY as EXIT_QA_ENV_UNHEALTHY,
    EXIT_QA_NOT_IMPLEMENTED as EXIT_QA_NOT_IMPLEMENTED,
    STDIN_WAIT_ENV as STDIN_WAIT_ENV,
    STDIN_WAIT_DEFAULT_SECONDS as STDIN_WAIT_DEFAULT_SECONDS,
    _STDIN_CHUNK as _STDIN_CHUNK,
    _THREADED_WAIT_FLOOR as _THREADED_WAIT_FLOOR,
    _DASHBOARD_HOST_HELP as _DASHBOARD_HOST_HELP,
    _AGENTTOOLS_SERVICE_MISSING_MSG as _AGENTTOOLS_SERVICE_MISSING_MSG,
    _TASK_SESSION_MATCH_WINDOW_SECONDS as _TASK_SESSION_MATCH_WINDOW_SECONDS,
    _HARNESS_ARG_ALIASES as _HARNESS_ARG_ALIASES,
    _USEFUL_CHOICES as _USEFUL_CHOICES,
    _ENTITY_HARNESS_LABELS as _ENTITY_HARNESS_LABELS,
    _SPECWEB_HELP as _SPECWEB_HELP,
    _SPECWEB_LIFECYCLE as _SPECWEB_LIFECYCLE,
    _SPECWEB_MISSING_LIB as _SPECWEB_MISSING_LIB,
    _SPECWEB_AGENT_REQUIRED as _SPECWEB_AGENT_REQUIRED,
    _WAITING_ON as _WAITING_ON,
    _SERVER_SUBCOMMANDS as _SERVER_SUBCOMMANDS,
    _SPECWEB_FAST_ACTIONS as _SPECWEB_FAST_ACTIONS,
    _VALUE_TAKING_OPTS as _VALUE_TAKING_OPTS,
    _LEADING_MODE_VALUE_OPTS as _LEADING_MODE_VALUE_OPTS,
    _LEADING_MODE_FLAG_OPTS as _LEADING_MODE_FLAG_OPTS,
    _LEADING_MODE_INLINE_SHORT_OPTS as _LEADING_MODE_INLINE_SHORT_OPTS,
    REVIEW_CLI_ACTIVE_ENV as REVIEW_CLI_ACTIVE_ENV,
    _SUBCOMMAND_ONLY_FLAGS as _SUBCOMMAND_ONLY_FLAGS,
    _BARE_SUBCOMMANDS as _BARE_SUBCOMMANDS,
)

import argparse as argparse
import contextlib as contextlib
import io as io
import math as math
import os as os
import select as select
import shlex as shlex
import subprocess as subprocess
import sys as sys
import time as time
from datetime import datetime as datetime, timedelta as timedelta, timezone as timezone
from pathlib import Path as Path
from typing import TYPE_CHECKING as TYPE_CHECKING, Callable as Callable, Literal as Literal, TextIO as TextIO

from . import admission as admission
from . import backends as backends
from . import jobs as jobs
from . import portable as portable
from . import usage_limits as usage_limits
from .backends import _which as _which
from .backstop import COMMAND_TIMEOUT_GRACE_SECONDS as COMMAND_TIMEOUT_GRACE_SECONDS, command_deadline as command_deadline, run_backstop as run_backstop
from .process import _open_log_with_fallback as _open_log_with_fallback, install_signal_reaper as install_signal_reaper
from .modes import refuse_oversized_diff as refuse_oversized_diff
from .panel_selection import TEXT_PANEL_MODES as TEXT_PANEL_MODES, panel_default_help as panel_default_help, panel_effort as panel_effort, resolve_panel_selection as resolve_panel_selection, select_level_panel as select_level_panel
from .config import CONFIG_PATH as CONFIG_PATH, DEFAULT_BOARD as DEFAULT_BOARD, DEFAULT_MODELS as DEFAULT_MODELS, DEFAULT_POOL_SIZE as DEFAULT_POOL_SIZE, SHIP_MIN_ROLES as SHIP_MIN_ROLES, DEFAULT_PROMPT as DEFAULT_PROMPT, default_level_name as default_level_name, resolve_level_name as resolve_level_name, resolve_moderator_candidates as resolve_moderator_candidates, PANEL_TIMEOUT_DEFAULT as PANEL_TIMEOUT_DEFAULT, QA_TIMEOUT_DEFAULT as QA_TIMEOUT_DEFAULT, VISUAL_MODELS as VISUAL_MODELS, BoardConfigError as BoardConfigError, BoardReviewer as BoardReviewer, EffortValueError as EffortValueError, _drop_malformed_board_entries as _drop_malformed_board_entries, _expand_alias as _expand_alias, _effective_pool_size as _effective_pool_size, _split_models as _split_models, apply_effort_override as apply_effort_override, board_from_models as board_from_models, ensure_min_distinct_roles as ensure_min_distinct_roles, expand_flat_models_with_reuse as expand_flat_models_with_reuse, lens_override_path as lens_override_path, lens_override_text as lens_override_text, load_board as load_board, load_config as load_config, prefer_distinct_roles as prefer_distinct_roles, parse_effort_flag as parse_effort_flag, preset_board as preset_board, preset_names as preset_names, preset_pool_size as preset_pool_size, recorded_task_pool_size as recorded_task_pool_size, select_pool_and_reserve_with_reuse as select_pool_and_reserve_with_reuse
from .usage_limits import usage_percent_for_model as usage_percent_for_model
from .pool_guard import PROCEED as PROCEED, Candidate as Candidate, default_distinct_key as default_distinct_key, evaluate_selection as evaluate_selection
from ._version import resolve_version as resolve_version
from .install import install_commit_hook as install_commit_hook, install_hook_tg as install_hook_tg, install_skill as install_skill, print_review_gate as print_review_gate
from .modes.brainstorm import mode_brainstorm as mode_brainstorm
from .modes.contract import ModeContext as ModeContext, ModeSpec as ModeSpec
from .modes.just_ask import mode_just_ask as mode_just_ask
from .modes.quorum import mode_quorum as mode_quorum
from .modes.registry import REMOVED_FLAGS as REMOVED_FLAGS, REMOVED_MODE_FLAGS as REMOVED_MODE_FLAGS, REMOVED_SUBCOMMANDS as REMOVED_SUBCOMMANDS, brainstorm_pool as brainstorm_pool, diff_mode as diff_mode, get_mode as get_mode, iter_modes as iter_modes, known_subcommands as known_subcommands
from .modes.review import mode_review as mode_review
from .panel import begin_call_tally as begin_call_tally, end_call_tally as end_call_tally, pick_moderators as pick_moderators
from .process import _run as _run, git_repo_env as git_repo_env, strip_control_sequences as strip_control_sequences
from .retry import max_retry_count as max_retry_count
from .stats import STATS_VERSION as STATS_VERSION, announce_eta as announce_eta, diff_content_hash as diff_content_hash, export_records as stats_export_records, extract_diff_files as extract_diff_files, find_by_run_id as find_by_run_id, fmt_duration as fmt_duration, import_records as stats_import_records, iterations_for_task as iterations_for_task, load_run_records as load_run_records, mode_summary as mode_summary, normalize_repo_remote as normalize_repo_remote, normalize_task_code as normalize_task_code, parse_iso_ts as parse_iso_ts, quorum_check as quorum_check, record_run as record_run, repo_summary as repo_summary, run_id_for as run_id_for, task_summaries as task_summaries
from .taskref import resolve_task_code as resolve_task_code

if TYPE_CHECKING:
    from agenttools_service import ServiceManager

# `review task --check` identity modes (review-cli#589): repo AND touched-file overlap,
# repository alone (`--verify-repo-only`), or none (`--no-verify-identity`).
IdentityMode = Literal["full", "repo_only", "disabled"]

# Keep the mode-handler names imported off `cli` for legacy import compatibility ONLY
# (some external/legacy callers `from reviewlib.cli import mode_review`). Dispatch goes
# through `modes/registry`, so rebinding `cli.mode_*` has NO effect on the running mode.
# NEW tests/code must patch the handler in its OWN module (e.g.
# `reviewlib.modes.review.mode_review`) or configure the `ModeSpec`, never via `cli`.
# This tuple just keeps the names referenced so the imports aren't flagged unused.
__mode_fns__ = (mode_brainstorm, mode_just_ask, mode_quorum, mode_review)

# Stable, per-class exit codes (structured-exit-codes). The diff-review path REQUIRES a
# git repo; run it outside one and it must fail GRACEFULLY with this distinct code (a
# "wrong place to run this" usage class), NOT a raw traceback / generic crash. Scripts can
# branch on it; it stays stable. 0=success, 2=argparse/usage (argparse's own), 124=backstop
# (reviewlib.backstop). 3 is the not-a-repo class — distinct from argparse-2 so a caller can
# tell "you ran the diff review outside a repo" apart from "you mistyped a flag".
# 4 is the "in a repo, but `git diff` itself failed" class (e.g. a wedged/timed-out git, a
# corrupt index) — distinct from EXIT_NOT_A_REPO (you ARE in a repo) and argparse-2. The
# REQUIRED review path catches the RuntimeError `_git_diff` raises so this never tracebacks.
# qa mode (review-qa.md §4/§6): "qa ran but no test-case suites/cases are authored for the
# target" — a CONTRACT failure (a green qa run with zero authored cases is a lie), distinct
# from a real finding, so CI can tell "you didn't author any suites" apart from "a test
# failed". The qa handler's no-suites gate returns this BEFORE any agent/docker/browser.
#
# Value is 6, NOT the 5 the spec's §6 first proposed: code 5 is already taken at the
# PROCESS-exit level by brainstorm's EXIT_DEAD_PANEL (modes/brainstorm.py), and structured
# exit codes must stay per-class distinct, so a script seeing 5 can't tell "brainstorm dead
# panel" from "qa no suites". qa's own codes therefore start at the next free integer (6);
# the later qa env classes (NO_ENV / ENV_UNHEALTHY / SUT_BOOT_FAILED) continue from 7 in
# Phase 2/3 (the spec's 5/6/7/8 block shifts up by one for the brainstorm collision).
# qa env classes (review-qa.md §6, shifted +1 from the spec's 5/6/7/8 because code 5 is
# brainstorm's EXIT_DEAD_PANEL — qa's own block starts at EXIT_QA_NO_SUITES=6). Phase 2
# lands the executor; the only env class it needs is "could not bring the SUT up at all"
# (the agent emitted VERDICT: BLOCKED) — distinct from a real finding (which is report-only)
# and from no-suites (6), so CI tells "infra broke" apart from "a bug was found". The
# NO_ENV / ENV_UNHEALTHY classes belong to the (later) compose env harness — reserved here
# as 7/9 so the executor's BLOCKED code (8) sits between them in the same coherent block.
# 10 is the pool-selection FOOLPROOFING class (reviewlib.pool_guard.EXIT_UNSATISFIED): the
# resolved review selection could not converge, so the CLI printed a proposal / targeted
# per-provider error instead of dispatching a degenerate panel. Distinct from every class
# above so a script can tell "the review pool couldn't be assembled" apart from a finding,
# a usage error, or a not-a-repo run. The value is owned by pool_guard (single source).

# RETIRED Phase-1 scaffold code. Phase 1 returned 70 from the suites-resolved-but-no-executor
# branch ("not implemented yet"). Phase 2 lands the executor, so that branch is GONE — the
# constant is kept (and pointed at the real executor path) only so a stale caller/import does
# not break; nothing in the live code returns it anymore.

# Codes 11/12 are CLAIMED by `modes/review.py` (EXIT_COMMIT_REQUIRES_STAGED / EXIT_COMMIT_
# FAILED, the `--commit` checkpoint feature) — defined there, not here, because that mode
# module can't import from cli.py without a circular import. Recorded here too so the next
# person picking a new top-level exit code doesn't re-use them.



@_forwarding(_cli_git_io._is_git_repo)
def _is_git_repo(cwd: Path) -> bool:
    return _cli_git_io._is_git_repo(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._fail_not_a_repo)
def _fail_not_a_repo(cwd: Path) -> int:
    return _cli_git_io._fail_not_a_repo(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._fail_git_diff)
def _fail_git_diff(cwd: Path, exc: Exception) -> int:
    return _cli_git_io._fail_git_diff(sys.modules[__name__], cwd, exc)


@_forwarding(_cli_git_io._git_diff)
def _git_diff(cwd: Path, staged: bool) -> str:
    return _cli_git_io._git_diff(sys.modules[__name__], cwd, staged)


@_forwarding(_cli_git_io._stamp_hash_for_staged_diff)
def _stamp_hash_for_staged_diff(cwd: Path) -> str | None:
    return _cli_git_io._stamp_hash_for_staged_diff(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._git_remote_origin_url)
def _git_remote_origin_url(cwd: Path) -> str | None:
    return _cli_git_io._git_remote_origin_url(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._git_toplevel)
def _git_toplevel(cwd: Path) -> Path | None:
    return _cli_git_io._git_toplevel(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._compute_repo_id)
def _compute_repo_id(cwd: Path) -> str | None:
    return _cli_git_io._compute_repo_id(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._default_branch_ref)
def _default_branch_ref(cwd: Path) -> str | None:
    return _cli_git_io._default_branch_ref(sys.modules[__name__], cwd)


@_forwarding(_cli_git_io._diff_name_only)
def _diff_name_only(cwd: Path, ref_args: list[str]) -> list[str] | None:
    return _cli_git_io._diff_name_only(sys.modules[__name__], cwd, ref_args)


@_forwarding(_cli_git_io._current_diff_files_for_check)
def _current_diff_files_for_check(cwd: Path) -> list[str] | None:
    return _cli_git_io._current_diff_files_for_check(sys.modules[__name__], cwd)




@_forwarding(_cli_git_io._stdin_wait_seconds)
def _stdin_wait_seconds() -> float:
    return _cli_git_io._stdin_wait_seconds(sys.modules[__name__])


@_forwarding(_cli_git_io._stdin_unusable)
def _stdin_unusable() -> bool:
    return _cli_git_io._stdin_unusable(sys.modules[__name__])


@_forwarding(_cli_git_io._stdin_fd)
def _stdin_fd() -> int | None:
    return _cli_git_io._stdin_fd(sys.modules[__name__])


@_forwarding(_cli_git_io._stdin_notice)
def _stdin_notice(text: str) -> None:
    return _cli_git_io._stdin_notice(sys.modules[__name__], text)


@_forwarding(_cli_git_io._decode_stdin)
def _decode_stdin(raw: bytes) -> str:
    return _cli_git_io._decode_stdin(sys.modules[__name__], raw)


# Minimum per-chunk wait on the reader-thread path: `REVIEW_STDIN_WAIT_SECONDS=0` means
# "only data already waiting counts", and a thread needs a moment to hand over bytes a
# closed pipe already holds — a 0 s queue wait would race the scheduler and drop them.


@_forwarding(_cli_git_io._read_fd_with_idle_timeout)
def _read_fd_with_idle_timeout(fd: int, idle: float, *, pollable: bool | None=None) -> bytes | None:
    return _cli_git_io._read_fd_with_idle_timeout(sys.modules[__name__], fd, idle, pollable=pollable)


@_forwarding(_cli_git_io._polled_chunks)
def _polled_chunks(fd: int, idle: float) -> Callable[[], bytes | None]:
    return _cli_git_io._polled_chunks(sys.modules[__name__], fd, idle)


@_forwarding(_cli_git_io._threaded_chunks)
def _threaded_chunks(fd: int, idle: float) -> Callable[[], bytes | None]:
    return _cli_git_io._threaded_chunks(sys.modules[__name__], fd, idle)


@_forwarding(_cli_git_io._read_stdin_if_piped)
def _read_stdin_if_piped() -> str | None:
    return _cli_git_io._read_stdin_if_piped(sys.modules[__name__])


@_forwarding(_cli_git_io._warn_if_stdin_ignored)
def _warn_if_stdin_ignored(source_flag: str, what: str) -> None:
    return _cli_git_io._warn_if_stdin_ignored(sys.modules[__name__], source_flag, what)


@_forwarding(_cli_git_io._explicit_diff_source)
def _explicit_diff_source(args: argparse.Namespace) -> tuple[str, str] | None:
    return _cli_git_io._explicit_diff_source(sys.modules[__name__], args)


@_forwarding(_cli_git_io._effective_cwd)
def _effective_cwd(raw: str, *, warn: bool=True) -> Path:
    return _cli_git_io._effective_cwd(sys.modules[__name__], raw, warn=warn)


# Shared between the `__serve` parser and the managed-service parser so the two surfaces never
# drift on what `--host` means (loopback-only by default; 0.0.0.0 exposes over Tailscale).

# Printed (exit 4) when a genuine lifecycle action is requested but the shared service lib is
# absent. Kept as a module constant so the smoke test can grep it and it stays aligned with the
# `pyproject.toml [dashboard]` extra comment that explains the same in-ecosystem dependency.


@_forwarding(_cli_sessions._dashboard_help_no_lib)
def _dashboard_help_no_lib() -> int:
    return _cli_sessions._dashboard_help_no_lib(sys.modules[__name__])


@_forwarding(_cli_sessions._dashboard_serve)
def _dashboard_serve(rest: list[str]) -> int:
    return _cli_sessions._dashboard_serve(sys.modules[__name__], rest)


@_forwarding(_cli_sessions._dashboard_subcommand)
def _dashboard_subcommand(rest: list[str]) -> int:
    return _cli_sessions._dashboard_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_sessions._dashboard_manager)
def _dashboard_manager(host: str, port: int) -> 'ServiceManager':
    return _cli_sessions._dashboard_manager(sys.modules[__name__], host, port)


@_forwarding(_cli_sessions._sessions_subcommand)
def _sessions_subcommand(rest: list[str]) -> int:
    return _cli_sessions._sessions_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_sessions._resume_session_cli)
def _resume_session_cli(ns: argparse.Namespace) -> int:
    return _cli_sessions._resume_session_cli(sys.modules[__name__], ns)


@_forwarding(_cli_sessions._task_sessions)
def _task_sessions(task_code: str):
    return _cli_sessions._task_sessions(sys.modules[__name__], task_code)




@_forwarding(_cli_sessions._parse_task_record_started)
def _parse_task_record_started(record: dict) -> datetime | None:
    return _cli_sessions._parse_task_record_started(sys.modules[__name__], record)


@_forwarding(_cli_sessions._session_started_utc)
def _session_started_utc(session) -> datetime:
    return _cli_sessions._session_started_utc(sys.modules[__name__], session)


@_forwarding(_cli_sessions._task_record_iteration)
def _task_record_iteration(record: dict) -> int | None:
    return _cli_sessions._task_record_iteration(sys.modules[__name__], record)


@_forwarding(_cli_sessions._closest_iteration_for_session)
def _closest_iteration_for_session(session, iterations: list[dict]) -> int | None:
    return _cli_sessions._closest_iteration_for_session(sys.modules[__name__], session, iterations)


@_forwarding(_cli_sessions._session_for_task_iteration)
def _session_for_task_iteration(record: dict, sessions: list, all_iterations: list[dict] | None=None, used_session_ids: set[str] | None=None):
    return _cli_sessions._session_for_task_iteration(sys.modules[__name__], record, sessions, all_iterations, used_session_ids)


@_forwarding(_cli_sessions._iteration_for_session)
def _iteration_for_session(session, iterations: list[dict]) -> int | None:
    return _cli_sessions._iteration_for_session(sys.modules[__name__], session, iterations)


@_forwarding(_cli_sessions._ssh_review)
def _ssh_review(host: str, remote_args: list[str]) -> tuple[int, str, str]:
    return _cli_sessions._ssh_review(sys.modules[__name__], host, remote_args)


@_forwarding(_cli_task_commands._task_transfer)
def _task_transfer(ns: argparse.Namespace) -> int:
    return _cli_task_commands._task_transfer(sys.modules[__name__], ns)


@_forwarding(_cli_task_commands._task_subcommand)
def _task_subcommand(rest: list[str]) -> int:
    return _cli_task_commands._task_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._jobs_subcommand)
def _jobs_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._jobs_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._job_exit_code)
def _job_exit_code(rec: dict) -> int:
    return _cli_job_commands._job_exit_code(sys.modules[__name__], rec)


@_forwarding(_cli_job_commands._seats_subcommand)
def _seats_subcommand(verb: str, rest: list[str]) -> int:
    return _cli_job_commands._seats_subcommand(sys.modules[__name__], verb, rest)


@_forwarding(_cli_job_commands._status_subcommand)
def _status_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._status_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._wait_subcommand)
def _wait_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._wait_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._resolve_stat_since)
def _resolve_stat_since(since_arg: str | None, days: int) -> datetime | None | bool:
    return _cli_job_commands._resolve_stat_since(sys.modules[__name__], since_arg, days)


# glm review finding, round 2: `--harness` filtered on an EXACT match against
# `report["harnesses"]`'s keys, which are the raw `CallLog.backend` string every OTHER
# call-log writer uses (`opencode`, `z.ai`, `commandcode`, ...) — not the short aliases
# `-m`/config actually teach (`-m glm`, `-m cc`, board ids `zai:...`/`oc:...`). So
# `--harness glm`/`zai`/`oc`/`cc` all printed "no calls recorded" while the data sat in
# the report under a different spelling. Sourced ONLY from aliases genuinely resolved
# elsewhere in this codebase — `resolve_backend`'s own alt-spelling matching
# (`zai`/`zhipu`/`glm` -> the z.ai backend, `oc` -> opencode) and `config.MODEL_ALIASES`
# (the `glm*` family, `cc`/`commoncode` -> commandcode) — NOT invented here. `cmd` is
# deliberately absent: despite a stale comment elsewhere once claiming it as a
# commandcode alias, it resolves NOWHERE in `_match_named_backend` or `MODEL_ALIASES` —
# it is not a real alias, so it is not silently accepted here either.


@_forwarding(_cli_job_commands._normalize_harness_arg)
def _normalize_harness_arg(raw: str) -> str:
    return _cli_job_commands._normalize_harness_arg(sys.modules[__name__], raw)




@_forwarding(_cli_job_commands._feedback_subcommand)
def _feedback_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._feedback_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._stat_subcommand)
def _stat_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._stat_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._capacity_check_subcommand)
def _capacity_check_subcommand(rest: list[str]) -> int:
    return _cli_job_commands._capacity_check_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_job_commands._render_stat_report_text)
def _render_stat_report_text(report: dict, *, requested_harness: str | None=None) -> str:
    return _cli_job_commands._render_stat_report_text(sys.modules[__name__], report, requested_harness=requested_harness)


@_forwarding(_cli_job_commands._render_stat_header)
def _render_stat_header(report: dict) -> str:
    return _cli_job_commands._render_stat_header(sys.modules[__name__], report)


@_forwarding(_cli_job_commands._render_stat_harness_table)
def _render_stat_harness_table(harnesses: dict, *, requested_harness: str | None=None) -> str:
    return _cli_job_commands._render_stat_harness_table(sys.modules[__name__], harnesses, requested_harness=requested_harness)


@_forwarding(_cli_job_commands._render_stat_fable_section)
def _render_stat_fable_section(fable: dict) -> str:
    return _cli_job_commands._render_stat_fable_section(sys.modules[__name__], fable)


@_forwarding(_cli_job_commands._render_stat_retry_section)
def _render_stat_retry_section(by_kind: dict) -> str:
    return _cli_job_commands._render_stat_retry_section(sys.modules[__name__], by_kind)


@_forwarding(_cli_job_commands._render_stat_mode_section)
def _render_stat_mode_section(by_mode: list[dict]) -> str:
    return _cli_job_commands._render_stat_mode_section(sys.modules[__name__], by_mode)


@_forwarding(_cli_job_commands._render_stat_repo_section)
def _render_stat_repo_section(by_repo: list[dict]) -> str:
    return _cli_job_commands._render_stat_repo_section(sys.modules[__name__], by_repo)


@_forwarding(_cli_job_commands._render_stat_oversized_section)
def _render_stat_oversized_section(top: list[dict]) -> str:
    return _cli_job_commands._render_stat_oversized_section(sys.modules[__name__], top)


# ---------------------------------------------------------------------------
# review entities — read-only entity/relationship/stats overview (review-cli#487)
# ---------------------------------------------------------------------------

# Maps a resolved backend dispatch function's __name__ (see `backends.resolve_backend`)
# to the harness display name `review stat --harness`/the README already use. The single
# source of truth for "which harness does this model route through" is
# `backends._match_named_backend`; this table only LABELS its outputs for humans instead
# of re-deriving the prefix rules a second time (review-cli#487).


@_forwarding(_cli_accounts._entity_harness_for_model)
def _entity_harness_for_model(model: str) -> str:
    return _cli_accounts._entity_harness_for_model(sys.modules[__name__], model)


@_forwarding(_cli_accounts._apply_runtime_config)
def _apply_runtime_config(config: dict) -> None:
    return _cli_accounts._apply_runtime_config(sys.modules[__name__], config)


@_forwarding(_cli_accounts._configured_board)
def _configured_board(config: dict) -> list[BoardReviewer]:
    return _cli_accounts._configured_board(sys.modules[__name__], config)


@_forwarding(_cli_accounts._omp_usage_text)
def _omp_usage_text(windows) -> str:
    return _cli_accounts._omp_usage_text(sys.modules[__name__], windows)


@_forwarding(_cli_accounts._omp_pool_json)
def _omp_pool_json(state) -> dict:
    return _cli_accounts._omp_pool_json(sys.modules[__name__], state)


@_forwarding(_cli_accounts._omp_pool_lines)
def _omp_pool_lines(state) -> list[str]:
    return _cli_accounts._omp_pool_lines(sys.modules[__name__], state)


@_forwarding(_cli_accounts._seat_status_report)
def _seat_status_report(*, as_json: bool) -> int:
    return _cli_accounts._seat_status_report(sys.modules[__name__], as_json=as_json)


@_forwarding(_cli_accounts._health_seats)
def _health_seats(config: dict, mode_name: str, preset: str | None, explicit_models: list[str]) -> list[str]:
    return _cli_accounts._health_seats(sys.modules[__name__], config, mode_name, preset, explicit_models)


@_forwarding(_cli_accounts._health_subcommand)
def _health_subcommand(rest: list[str]) -> int:
    return _cli_accounts._health_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_accounts._local_health_report)
def _local_health_report(ns, timeout: float, live: bool) -> dict:
    return _cli_accounts._local_health_report(sys.modules[__name__], ns, timeout, live)


@_forwarding(_cli_accounts._entities_subcommand)
def _entities_subcommand(rest: list[str]) -> int:
    return _cli_accounts._entities_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_accounts._render_entities_report)
def _render_entities_report(payload: dict) -> str:
    return _cli_accounts._render_entities_report(sys.modules[__name__], payload)


@_forwarding(_cli_task_proof._resolve_quorum_check_context)
def _resolve_quorum_check_context(cwd_raw: str, identity_mode: IdentityMode) -> tuple[str | None, list[str] | None, str]:
    return _cli_task_proof._resolve_quorum_check_context(sys.modules[__name__], cwd_raw, identity_mode)


@_forwarding(_cli_task_proof._print_quorum_model_audit_line)
def _print_quorum_model_audit_line(result: dict, stream=None) -> None:
    return _cli_task_proof._print_quorum_model_audit_line(sys.modules[__name__], result, stream)


@_forwarding(_cli_task_proof._print_quorum_bar_met)
def _print_quorum_bar_met(result: dict, role_mode: bool, model_mode: bool) -> None:
    return _cli_task_proof._print_quorum_bar_met(sys.modules[__name__], result, role_mode, model_mode)


@_forwarding(_cli_task_proof._print_quorum_bar_not_met)
def _print_quorum_bar_not_met(result: dict, role_mode: bool, model_mode: bool) -> None:
    return _cli_task_proof._print_quorum_bar_not_met(sys.modules[__name__], result, role_mode, model_mode)


@_forwarding(_cli_task_proof._quorum_check_subcommand)
def _quorum_check_subcommand(code: str, min_iter: int, min_models: int | None, *, min_roles: int | None=None, as_json: bool, cwd_raw: str='.', identity_mode: IdentityMode='full') -> int:
    return _cli_task_proof._quorum_check_subcommand(sys.modules[__name__], code, min_iter, min_models, min_roles=min_roles, as_json=as_json, cwd_raw=cwd_raw, identity_mode=identity_mode)


@_forwarding(_cli_task_proof._task_detail)
def _task_detail(code: str, selector: str, iterations: list[dict], sessions: list, *, as_json: bool) -> int:
    return _cli_task_proof._task_detail(sys.modules[__name__], code, selector, iterations, sessions, as_json=as_json)



# The lifecycle actions handled by the shared agenttools_service ServiceManager.


@_forwarding(_cli_specweb._spec_web)
def _spec_web(argv: list[str]) -> int:
    return _cli_specweb._spec_web(sys.modules[__name__], argv)


# ---- spec-web: shared agenttools_service message + host/port parsing ---------- #


@_forwarding(_cli_specweb._spec_web_host_port)
def _spec_web_host_port(rest: list[str], *, defaults: bool=True):
    return _cli_specweb._spec_web_host_port(sys.modules[__name__], rest, defaults=defaults)


# The daemon-launching actions REQUIRE an owning agent (submitted reviews are DELIVERED into
# that agent's live session — a daemon nobody owns re-creates the "comments reach nobody" bug).


@_forwarding(_cli_specweb._spec_web_require_agent)
def _spec_web_require_agent(action: str, agent: str | None) -> int | None:
    return _cli_specweb._spec_web_require_agent(sys.modules[__name__], action, agent)


@_forwarding(_cli_specweb._spec_web_manager)
def _spec_web_manager(host: str, port: int | None, agent: str | None=None):
    return _cli_specweb._spec_web_manager(sys.modules[__name__], host, port, agent)


@_forwarding(_cli_specweb._spec_web_serve_daemon)
def _spec_web_serve_daemon(rest: list[str]) -> int:
    return _cli_specweb._spec_web_serve_daemon(sys.modules[__name__], rest)


@_forwarding(_cli_specweb._spec_web_lifecycle)
def _spec_web_lifecycle(action: str, rest: list[str]) -> int:
    return _cli_specweb._spec_web_lifecycle(sys.modules[__name__], action, rest)


@_forwarding(_cli_specweb._clip_text)
def _clip_text(text: str, limit: int, *, collapse_whitespace: bool=False) -> str:
    return _cli_specweb._clip_text(sys.modules[__name__], text, limit, collapse_whitespace=collapse_whitespace)




@_forwarding(_cli_specweb._waiting_on_phrase)
def _waiting_on_phrase(status: str | None) -> str:
    return _cli_specweb._waiting_on_phrase(sys.modules[__name__], status)


@_forwarding(_cli_specweb._stale_reminder_line)
def _stale_reminder_line(stale: dict) -> str:
    return _cli_specweb._stale_reminder_line(sys.modules[__name__], stale)


@_forwarding(_cli_specweb._print_stale_reminders)
def _print_stale_reminders(stale: list[dict], *, stream: TextIO | None=None) -> None:
    return _cli_specweb._print_stale_reminders(sys.modules[__name__], stale, stream=stream)


@_forwarding(_cli_specweb._comments_for_reminder)
def _comments_for_reminder(spec_path: Path | str) -> list[dict]:
    return _cli_specweb._comments_for_reminder(sys.modules[__name__], spec_path)


@_forwarding(_cli_specweb._spec_web_print_registry)
def _spec_web_print_registry(host: str, port: int) -> None:
    return _cli_specweb._spec_web_print_registry(sys.modules[__name__], host, port)


@_forwarding(_cli_specweb._spec_web_add)
def _spec_web_add(rest: list[str], *, watch: bool) -> int:
    return _cli_specweb._spec_web_add(sys.modules[__name__], rest, watch=watch)


@_forwarding(_cli_specweb._spec_web_ensure_running)
def _spec_web_ensure_running(mgr, *, agent: str | None=None) -> int | None:
    return _cli_specweb._spec_web_ensure_running(sys.modules[__name__], mgr, agent=agent)


@_forwarding(_cli_specweb._spec_web_seed)
def _spec_web_seed(spec: Path, seed: str) -> int:
    return _cli_specweb._spec_web_seed(sys.modules[__name__], spec, seed)


@_forwarding(_cli_specweb._spec_web_open_browser)
def _spec_web_open_browser(url: str) -> None:
    return _cli_specweb._spec_web_open_browser(sys.modules[__name__], url)


@_forwarding(_cli_specweb._spec_web_legacy_foreground)
def _spec_web_legacy_foreground(spec: Path, ns) -> int:
    return _cli_specweb._spec_web_legacy_foreground(sys.modules[__name__], spec, ns)


@_forwarding(_cli_specweb._spec_web_list)
def _spec_web_list(rest: list[str]) -> int:
    return _cli_specweb._spec_web_list(sys.modules[__name__], rest)


@_forwarding(_cli_specweb._spec_web_remove)
def _spec_web_remove(rest: list[str]) -> int:
    return _cli_specweb._spec_web_remove(sys.modules[__name__], rest)


@_forwarding(_cli_specweb._spec_web_watch)
def _spec_web_watch(rest: list[str]) -> int:
    return _cli_specweb._spec_web_watch(sys.modules[__name__], rest)


@_forwarding(_cli_specweb._spec_web_reply)
def _spec_web_reply(argv: list[str]) -> int:
    return _cli_specweb._spec_web_reply(sys.modules[__name__], argv)


@_forwarding(_cli_specweb._stale_summary_for_tg)
def _stale_summary_for_tg(stale: list[dict]) -> str:
    return _cli_specweb._stale_summary_for_tg(sys.modules[__name__], stale)


@_forwarding(_cli_specweb._spec_web_reply_to_tg)
def _spec_web_reply_to_tg(spec: Path, comment: dict, answer: str, *, stale_others: list[dict] | None=None) -> None:
    return _cli_specweb._spec_web_reply_to_tg(sys.modules[__name__], spec, comment, answer, stale_others=stale_others)


@_forwarding(_cli_run_stats._print_feedback_nudge)
def _print_feedback_nudge(run_id: str) -> None:
    return _cli_run_stats._print_feedback_nudge(sys.modules[__name__], run_id)


@_forwarding(_cli_run_stats._run_mode_with_stats)
def _run_mode_with_stats(mode: str, pool_models: list[str], dispatch, models_after=None, roles_after=None, usable_roles_after=None, *, task_code: str | None=None, repo_id: str | None=None, diff_files: list[str] | None=None, diff_sha256: str | None=None, needs_slot: bool=True, direct_seats: bool=False, later_models: list[str] | None=None, level: str | None=None) -> int:
    return _cli_run_stats._run_mode_with_stats(sys.modules[__name__], mode, pool_models, dispatch, models_after, roles_after, usable_roles_after, task_code=task_code, repo_id=repo_id, diff_files=diff_files, diff_sha256=diff_sha256, needs_slot=needs_slot, direct_seats=direct_seats, later_models=later_models, level=level)


@_forwarding(_cli_run_stats._run_admitted_mode_with_stats)
def _run_admitted_mode_with_stats(mode: str, pool_models: list[str], dispatch, models_after=None, roles_after=None, usable_roles_after=None, *, task_code: str | None=None, repo_id: str | None=None, diff_files: list[str] | None=None, diff_sha256: str | None=None, level: str | None=None) -> int:
    return _cli_run_stats._run_admitted_mode_with_stats(sys.modules[__name__], mode, pool_models, dispatch, models_after, roles_after, usable_roles_after, task_code=task_code, repo_id=repo_id, diff_files=diff_files, diff_sha256=diff_sha256, level=level)


@_forwarding(_cli_run_stats._call_with_task_env)
def _call_with_task_env(task_code: str | None, fn):
    return _cli_run_stats._call_with_task_env(sys.modules[__name__], task_code, fn)


# Subcommands that run a PERSISTENT server until Ctrl-C (`review dashboard`,
# `review spec-web`) — these are intentionally long-lived and must NOT be bounded by
# the run backstop, which would otherwise kill the server after the ceiling (or almost
# immediately under a lowered $REVIEW_BACKSTOP_SECONDS). The backstop is for the
# bounded review/model RUN paths only.


@_forwarding(_cli_entry_helpers._is_persistent_server_invocation)
def _is_persistent_server_invocation(argv: list[str]) -> bool:
    return _cli_entry_helpers._is_persistent_server_invocation(sys.modules[__name__], argv)


# spec-web actions that BLOCK (a persistent server / a submit-watch loop) and so must bypass the
# `-o` tee + run backstop; everything else (lifecycle management, register, list) returns fast.


@_forwarding(_cli_entry_helpers._spec_web_is_persistent)
def _spec_web_is_persistent(rest: list[str]) -> bool:
    return _cli_entry_helpers._spec_web_is_persistent(sys.modules[__name__], rest)


@_forwarding(_cli_entry_helpers._dashboard_action)
def _dashboard_action(rest: list[str]) -> str | None:
    return _cli_entry_helpers._dashboard_action(sys.modules[__name__], rest)


@_forwarding(_cli_entry_helpers._dashboard_action_with_index)
def _dashboard_action_with_index(rest: list[str]) -> tuple[str | None, int]:
    return _cli_entry_helpers._dashboard_action_with_index(sys.modules[__name__], rest)




# Options that CONSUME the next token as their value (space-separated form). When the
# pre-scan for `-o` sees one of these, the FOLLOWING token is that option's value and
# must be passed through untouched — even if it happens to look like `-o`/`--output`
# (e.g. `review --just-ask --output` where `--output` is the question text, or a
# `--prompt -o…`). This keeps the light pre-scan from stealing another flag's value.


@_forwarding(_cli_entry_helpers._extract_output_path)
def _extract_output_path(argv: list[str]) -> tuple[Path | None, list[str]]:
    return _cli_entry_helpers._extract_output_path(sys.modules[__name__], argv)




@_forwarding(_cli_entry_helpers._is_leading_inline_short_option)
def _is_leading_inline_short_option(tok: str) -> bool:
    return _cli_entry_helpers._is_leading_inline_short_option(sys.modules[__name__], tok)


@_forwarding(_cli_entry_helpers._normalize_leading_mode_options)
def _normalize_leading_mode_options(argv: list[str]) -> list[str]:
    return _cli_entry_helpers._normalize_leading_mode_options(sys.modules[__name__], argv)


@_forwarding(_cli_entry_helpers._write_output_file)
def _write_output_file(path: Path, text: str) -> None:
    return _cli_entry_helpers._write_output_file(sys.modules[__name__], path, text)


# Env var that marks "a `review` invocation is already running in this process tree"
# (review-cli#180). Set for the lifetime of a run by `main()`; checked by
# `_reject_if_reentrant()` before any backend is touched.


@_forwarding(_cli_entry_helpers._reject_if_reentrant)
def _reject_if_reentrant(_argv: list[str]) -> int | None:
    return _cli_entry_helpers._reject_if_reentrant(sys.modules[__name__], _argv)


@_forwarding(_cli_entry_helpers._dispatch_with_backstop)
def _dispatch_with_backstop(raw: list[str], output_path: str | None) -> int:
    return _cli_entry_helpers._dispatch_with_backstop(sys.modules[__name__], raw, output_path)


def main(argv: list[str] | None = None) -> int:
    """Entry point: install the child-reaper, then either detach a background job or
    run the review inline (reentrancy guard + backstop).

    See `_reject_if_reentrant` for the review-cli#180 fork-bomb guard,
    `_dispatch_with_backstop` for the backstop/`-o`-tee mechanics, and
    `_spawn_detached_job` for `--detach` (review-cli#160 companion feature), which
    lets the CALLER stop blocking at all — it spawns the review as a session-detached
    background process and returns almost immediately with a job-id (`review status
    <job-id>` / `review jobs` poll it afterwards).

    This function also finalizes a DETACHED job's terminal status: when
    `$REVIEW_JOB_ID` is set (only true inside a child `_spawn_detached_job` itself
    spawned), the body's return code — or the code of a propagating `SystemExit` — is
    recorded via `reviewlib.jobs.write_job` before this process exits, so `review
    status` never has to guess from a raw exit code alone.
    """
    # Reap live backend children on an EXTERNAL SIGTERM/SIGINT (a caller's `kill <pid>`,
    # a harness timing out this process, or Ctrl-C) — not just an internal backstop
    # fire. See `process.install_signal_reaper` for why this is a distinct gap
    # (review-cli#160): the internal backstop only reaps when ITS OWN watchdog fires
    # from a live interpreter; an external signal's default disposition kills the
    # process before any `finally` runs, orphaning session-isolated backend children.
    install_signal_reaper()
    # A model's review text (→, ✓, CJK) must never crash printing on a cp1251/cp1252 pipe
    # (Windows mintty/Git Bash/redirects); see `portable.ensure_utf8_stdio`.
    portable.ensure_utf8_stdio()

    raw = sys.argv[1:] if argv is None else argv
    # The reentrancy guard runs BEFORE the --detach split (review-cli#180 x #160): a
    # backend nested inside a running `review` must not be able to sidestep the
    # fork-bomb guard by detaching — the spawned child would start from a clean
    # environment (no $REVIEW_CLI_ACTIVE, by design) and recurse freely. It is also
    # the highest-severity failure, so it wins over any usage error below.
    rejected = _reject_if_reentrant(raw)
    if rejected is not None:
        return rejected
    detach, raw = _extract_detach_flag(raw)
    if detach:
        return _spawn_detached_job(raw)

    job_id = os.environ.get("REVIEW_JOB_ID")
    if not job_id:
        return _main_dispatch(raw)

    # Detached-child finalization path: run the SAME body as a normal foreground call,
    # then durably record how it ended — including a SystemExit (argparse usage error,
    # `--help`, or a deliberate `sys.exit`), which otherwise propagates past a plain
    # `return` and would leave the job record stuck at "running" forever.

    rc = 1
    try:
        rc = _main_dispatch(raw)
    except SystemExit as exc:
        code = exc.code
        rc = 0 if code is None else (code if isinstance(code, int) else 1)
        raise
    finally:
        # Best-effort, like every other job-record write in this module: `$REVIEW_JOB_ID`
        # is only ever set by OUR OWN `_spawn_detached_job` in the shipped call graph, but
        # it is still an inherited environment variable — a malformed/tampered value must
        # never crash the finalizer and mask the review's own real result (codex review:
        # `jobs.write_job` raises `InvalidJobId` for anything that isn't a well-formed id).
        try:
            jobs.write_job(
                job_id,
                status="done" if rc == 0 else "failed",
                exit_code=rc,
                finished_at=time.time(),
            )
        except jobs.InvalidJobId:
            print(
                f"[review-cli] $REVIEW_JOB_ID is not a valid job id ({job_id!r}) — "
                "not recording a job status.",
                file=sys.stderr,
            )
        except OSError as exc:
            # A jobs dir that became unwritable AFTER spawn (a sandbox grant that
            # expired mid-review) must not replace the review's own real exit code
            # with a traceback; the record is left "running" and `job_status`'s
            # dead-pid reconciliation reports it as unknown-terminated later.
            print(
                f"[review-cli] could not record the final status of job {job_id}: "
                f"{type(exc).__name__}: {exc} — the review itself finished with exit "
                f"code {rc}.",
                file=sys.stderr,
            )
    return rc


@_forwarding(_cli_entry_helpers._extract_detach_flag)
def _extract_detach_flag(argv: list[str]) -> tuple[bool, list[str]]:
    return _cli_entry_helpers._extract_detach_flag(sys.modules[__name__], argv)


@_forwarding(_cli_entry_helpers._reviewlib_repo_root)
def _reviewlib_repo_root() -> str:
    return _cli_entry_helpers._reviewlib_repo_root(sys.modules[__name__])


@_forwarding(_cli_entry_helpers._first_verb)
def _first_verb(raw: list[str]) -> str | None:
    return _cli_entry_helpers._first_verb(sys.modules[__name__], raw)


@_forwarding(_cli_entry_helpers._reject_undetachable)
def _reject_undetachable(raw: list[str]) -> int | None:
    return _cli_entry_helpers._reject_undetachable(sys.modules[__name__], raw)


@_forwarding(_cli_entry_helpers._detached_child_argv)
def _detached_child_argv(raw: list[str], result_path: Path | None) -> list[str]:
    return _cli_entry_helpers._detached_child_argv(sys.modules[__name__], raw, result_path)


@_forwarding(_cli_entry_helpers._detached_child_env)
def _detached_child_env(job_id: str, this_jobs_dir: Path) -> dict[str, str]:
    return _cli_entry_helpers._detached_child_env(sys.modules[__name__], job_id, this_jobs_dir)


@_forwarding(_cli_entry_helpers._detached_child_stdin)
def _detached_child_stdin() -> int:
    return _cli_entry_helpers._detached_child_stdin(sys.modules[__name__])


@_forwarding(_cli_entry_helpers._spawn_detached_job)
def _spawn_detached_job(raw: list[str]) -> int:
    return _cli_entry_helpers._spawn_detached_job(sys.modules[__name__], raw)


@_forwarding(_cli_entry_helpers._main_dispatch)
def _main_dispatch(raw: list[str]) -> int:
    return _cli_entry_helpers._main_dispatch(sys.modules[__name__], raw)


@_forwarding(_cli_options._model_default_help)
def _model_default_help(mode: ModeSpec | None) -> str:
    return _cli_options._model_default_help(sys.modules[__name__], mode)


@_forwarding(_cli_options._default_level_for_help)
def _default_level_for_help() -> str:
    return _cli_options._default_level_for_help(sys.modules[__name__])


@_forwarding(_cli_options._levels_help)
def _levels_help() -> str:
    return _cli_options._levels_help(sys.modules[__name__])


@_forwarding(_cli_options._level_arg)
def _level_arg(value: str) -> str:
    return _cli_options._level_arg(sys.modules[__name__], value)


@_forwarding(_cli_options._moderator_default_help)
def _moderator_default_help() -> str:
    return _cli_options._moderator_default_help(sys.modules[__name__])


_MODERATOR_NOTICES_SHOWN: set[str] = set()


@_forwarding(_cli_options._moderator_candidates)
def _moderator_candidates(config: dict, level: str | None) -> tuple[str, ...]:
    return _cli_options._moderator_candidates(sys.modules[__name__], config, level)


@_forwarding(_cli_options._add_global_options)
def _add_global_options(parser: argparse.ArgumentParser, *, mode: ModeSpec | None) -> None:
    return _cli_options._add_global_options(sys.modules[__name__], parser, mode=mode)
    # NOTE: `--retry` is NOT global — it only applies to the diff REVIEW path (the failover
    # board + the flat `-m` panel), not brainstorm/quorum/just-ask (which call run_panel and
    # never use the retry wrapper). It lives on the diff mode's own option surface
    # (modes/review.py `_add_arguments`), so the top-level help isn't padded with a no-op flag
    # (AGENTS.md: the global list is only truly-global options). codex P1 on #46.


@_forwarding(_cli_options._add_visual_options)
def _add_visual_options(parser: argparse.ArgumentParser, *, include_visual_flag: bool=True, json_help: str='emit the structured visual verdict as JSON') -> None:
    return _cli_options._add_visual_options(sys.modules[__name__], parser, include_visual_flag=include_visual_flag, json_help=json_help)


@_forwarding(_cli_options._add_mode_options)
def _add_mode_options(parser: argparse.ArgumentParser, *, mode: ModeSpec) -> None:
    return _cli_options._add_mode_options(sys.modules[__name__], parser, mode=mode)


# Flags that live ONLY on a subcommand parser (NOT the global top-level parser), per the
# option-scoping. When one of these LEADS a no-subcommand invocation (e.g. the old pre-commit
# `review --staged`, or `review --visual shot.png`), the top-level parser would reject it with
# argparse's opaque "unrecognized arguments", losing the `review diff` migration pointer. The
# pre-parse guard below catches them and emits the friendly pointer instead.

# The BARE management subcommands `_dispatch` handles directly (NOT mode verbs in
# known_subcommands(), and NOT the diff review): they have their OWN flag parsers (e.g.
# `review sessions -s <id> --diff --moderator …`). The verb-less migration guard must leave
# them alone — a `--diff`/`--moderator` after `sessions` belongs to that subparser, not a
# missing `review diff`.


@_forwarding(_cli_options._reject_subcommand_only_flag_without_verb)
def _reject_subcommand_only_flag_without_verb(argv: list[str]) -> int | None:
    return _cli_options._reject_subcommand_only_flag_without_verb(sys.modules[__name__], argv)


@_forwarding(_cli_help_topics._help_topic_config)
def _help_topic_config() -> str:
    return _cli_help_topics._help_topic_config(sys.modules[__name__])


@_forwarding(_cli_help_topics._help_topic_task_check)
def _help_topic_task_check() -> str:
    return _cli_help_topics._help_topic_task_check(sys.modules[__name__])


@_forwarding(_cli_help_topics._help_topic_health)
def _help_topic_health() -> str:
    return _cli_help_topics._help_topic_health(sys.modules[__name__])


@_forwarding(_cli_help_topics._help_topic_runtime)
def _help_topic_runtime() -> str:
    return _cli_help_topics._help_topic_runtime(sys.modules[__name__])


@_forwarding(_cli_help_topics._help_topic_capacity)
def _help_topic_capacity() -> str:
    return _cli_help_topics._help_topic_capacity(sys.modules[__name__])


# Deep help TOPICS advertised from the main `review --help` and served by `review help
# <topic>` / `review --help <topic>` (ROADMAP "Topic-based help across the ecosystem").
# topic -> (one-line summary for the main-help listing, zero-arg renderer). Add a topic =
# add an entry here; the main-help pointer + dispatch pick it up with no other edit.
HELP_TOPICS: dict[str, tuple[str, "object"]] = {
    "config": (
        "config file, the model/board selection cascade, keys/auth",
        _help_topic_config,
    ),
    "task-check": (
        "the `task CODE --check --json` self-merge-gate contract, field by field",
        _help_topic_task_check,
    ),
    "runtime": (
        "run stats, the startup ETA line, why never to short-timeout `review`",
        _help_topic_runtime,
    ),
    "capacity": (
        "the `capacity-check --json` quota-outage contract and evidence predicate",
        _help_topic_capacity,
    ),
    "health": (
        "seat health checks and how long an unavailable seat stays excluded",
        _help_topic_health,
    ),
}


@_forwarding(_cli_parsing._subcommand_epilog)
def _subcommand_epilog() -> str:
    return _cli_parsing._subcommand_epilog(sys.modules[__name__])


@_forwarding(_cli_parsing._build_top_level_parser)
def _build_top_level_parser() -> argparse.ArgumentParser:
    return _cli_parsing._build_top_level_parser(sys.modules[__name__])


@_forwarding(_cli_parsing._build_mode_parser)
def _build_mode_parser(mode: ModeSpec) -> argparse.ArgumentParser:
    return _cli_parsing._build_mode_parser(sys.modules[__name__], mode)


@_forwarding(_cli_parsing._reject_removed_flags)
def _reject_removed_flags(argv: list[str]) -> int | None:
    return _cli_parsing._reject_removed_flags(sys.modules[__name__], argv)


@_forwarding(_cli_parsing._reject_removed_subcommand)
def _reject_removed_subcommand(argv: list[str]) -> int | None:
    return _cli_parsing._reject_removed_subcommand(sys.modules[__name__], argv)


@_forwarding(_cli_parsing._leading_removed_subcommand)
def _leading_removed_subcommand(argv: list[str]) -> str | None:
    return _cli_parsing._leading_removed_subcommand(sys.modules[__name__], argv)


@_forwarding(_cli_parsing._help_subcommand)
def _help_subcommand(rest: list[str]) -> int:
    return _cli_parsing._help_subcommand(sys.modules[__name__], rest)


@_forwarding(_cli_parsing._config_default_pool)
def _config_default_pool(config: dict) -> int | None:
    return _cli_parsing._config_default_pool(sys.modules[__name__], config)


@_forwarding(_cli_parsing._no_deliberate_pool_choice)
def _no_deliberate_pool_choice(args, explicit_preset: bool, config: dict) -> bool:
    return _cli_parsing._no_deliberate_pool_choice(sys.modules[__name__], args, explicit_preset, config)


@_forwarding(_cli_parsing._seats_of)
def _seats_of(board: list) -> tuple[tuple[str, str], ...]:
    return _cli_parsing._seats_of(sys.modules[__name__], board)


@_forwarding(_cli_parsing._board_from_models_lenient)
def _board_from_models_lenient(models: list[str], config: dict, *, preset: str | None=None) -> list:
    return _cli_parsing._board_from_models_lenient(sys.modules[__name__], models, config, preset=preset)


@_forwarding(_cli_parsing._default_review_board)
def _default_review_board(config: dict, config_models: list[str], default_pool: int):
    return _cli_parsing._default_review_board(sys.modules[__name__], config, config_models, default_pool)


@_forwarding(_cli_parsing._pool_guard_candidates)
def _pool_guard_candidates(config: dict, config_models: list[str], default_pool: int) -> list[Candidate]:
    return _cli_parsing._pool_guard_candidates(sys.modules[__name__], config, config_models, default_pool)


@_forwarding(_cli_parsing._announce_quorum_auto_roles)
def _announce_quorum_auto_roles(seats: list) -> None:
    return _cli_parsing._announce_quorum_auto_roles(sys.modules[__name__], seats)


@_forwarding(_cli_parsing._warn_if_panel_padded)
def _warn_if_panel_padded(models: list[str]) -> None:
    return _cli_parsing._warn_if_panel_padded(sys.modules[__name__], models)


@_forwarding(_cli_parsing._chain_aware_available)
def _chain_aware_available(model: str) -> bool:
    return _cli_parsing._chain_aware_available(sys.modules[__name__], model)


@_forwarding(_cli_parsing._evaluate_pool_or_bail)
def _evaluate_pool_or_bail(config: dict, config_models: list[str], user_seats: tuple[tuple[str, str], ...], explicit_models: list[str], pool_arg: int | None, default_pool: int, reuse_eligible: bool=False) -> int | None:
    return _cli_parsing._evaluate_pool_or_bail(sys.modules[__name__], config, config_models, user_seats, explicit_models, pool_arg, default_pool, reuse_eligible)


def _dispatch(argv: list[str] | None = None) -> int:
    """Resolve and run one `review` invocation. A host-wide run slot taken early in the
    dispatch (`admission.hold_run_slot`, the companion vision fan-out) is released when it
    returns (review-cli#614)."""
    with admission.dispatch_scope():
        return _dispatch_scoped(argv)


@_forwarding(_cli_dispatch._dispatch_scoped)
def _dispatch_scoped(argv: list[str] | None=None) -> int:
    return _cli_dispatch._dispatch_scoped(sys.modules[__name__], argv)


@_forwarding(_cli_board_output._seat_reads_repo)
def _seat_reads_repo(model: str, cwd_is_repo: bool) -> bool:
    return _cli_board_output._seat_reads_repo(sys.modules[__name__], model, cwd_is_repo)


@_forwarding(_cli_board_output._show_board)
def _show_board(config: dict, pool_size: int=DEFAULT_POOL_SIZE, cwd: Path | None=None, *, preset: str | None=None, explicit_models: list[str] | None=None, pad_task_roles: bool=False) -> int:
    return _cli_board_output._show_board(sys.modules[__name__], config, pool_size, cwd, preset=preset, explicit_models=explicit_models, pad_task_roles=pad_task_roles)


@_forwarding(_cli_board_output._with_visual)
def _with_visual(text: str, visual_ctx) -> str:
    return _cli_board_output._with_visual(sys.modules[__name__], text, visual_ctx)


# Re-export for legacy callers that imported subprocess off the entry module.
__all__ = ["main", "subprocess", "stats_export_records", "stats_import_records"]
