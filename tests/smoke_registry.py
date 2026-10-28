"""Complete smoke inventory, still checked by test_every_unit_file_is_run."""

_GIT_REQUIRED_UNIT_FILES = frozenset(
    {
        "test_cwd.py",
        "test_dashboard.py",
        "test_deploy_sh.py",
        "test_inseat_retry.py",
        "test_install_state.py",
        "test_no_git_repo.py",
        "test_opencode_realrepo.py",
        "test_output_flag.py",
        "test_qa_executor.py",
        "test_qa_mode.py",
        "test_review_gate_template.py",
        "test_review_marker.py",
        "test_review_remote.py",
        "test_review_commit_checkpoint.py",
        "test_review_stamp_delta_tolerance.py",
        "test_run_stats.py",
        "test_staged_diff_honors_c_repo.py",
        "test_stdin_silent_pipe.py",
        "test_taskref.py",
        "test_tui_pty.py",
    }
)


_FRESH_TMP = object()


_UNIT_FILES = [
    ("test_dashboard_service.py", {}),
    ("test_dashboard.py", {}),
    ("test_capacity_check.py", {}),
    ("test_capacity_check_contract.py", {}),
    ("test_streaming.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # review-cli#221: run_board_with_failover arms/clears the wall-clock board deadline
    # at the right moments (the clamp math itself is covered in test_streaming.py). One
    # test intentionally fails a seat, which writes a retry log — REVIEW_LOG_DIR isolates
    # that away from the real log directory.
    ("test_board_deadline_wiring.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_claude_seat_robust.py", {}),
    ("test_workspace_trust.py", {}),
    ("test_moderator.py", {}),
    ("test_cwd.py", {}),
    ("test_shim_bootstrap.py", {}),
    ("test_install_shadow_warning.py", {}),
    # install.sh trusts a clone only when its origin is the real repo by host AND path.
    ("test_install_origin.py", {}),
    # The rig-apply deploy hook (scripts/deploy.sh): safe-FF-pull refusal semantics
    # driven against throwaway origin+clone pairs in temp dirs (review-cli#105).
    ("test_deploy_sh.py", {}),
    ("test_no_git_repo.py", {}),
    ("test_staged_diff_honors_c_repo.py", {}),
    ("test_output_flag.py", {}),
    ("test_opencode_realrepo.py", {}),
    # Versioned per-model true-silence behavior registry (review-cli#235). Pure data
    # lookups + env-override precedence — no I/O, no git.
    ("test_model_behavior.py", {}),
    # review_opencode's true-silence cooldown wiring (review-cli#235): recording AND
    # consulting a cooldown, escalation on repeat trips, dashboard attribution, and the
    # no-cooldown-on-a-genuine-child-exit-125 safeguard (codex/Fable review findings).
    # Isolates its own $REVIEW_SEAT_COOLDOWN_FILE per test — no shared env needed here.
    ("test_true_silence_cooldown_wiring.py", {}),
    # 4 backend wiring checks (review-cli#474): codex/omp/claude_cli/claude_cli_with_images
    # each forward true_silence_timeout=true_silence_timeout_seconds(model) to _run_streamed.
    ("test_true_silence_backend_wiring.py", {}),
    # Backend-agnostic regression test (review-cli#474): _run_streamed itself reaps a
    # real, zero-output subprocess via true_silence_timeout — independent of any
    # single backend's wiring (that's covered separately by the wiring test above).
    ("test_true_silence_hang_regression.py", {}),
    # The omp (Oh My Pi) agentic read-only backend (review-cli#174): routing, the
    # `@payloadfile` launch contract, the offline sqlite auth probe, unpaid gating,
    # board scope label + dashboard attribution. Hermetic — fake _which/_run_streamed
    # and a throwaway OMP_AUTH_DB; no real omp binary, no network.
    ("test_omp_backend.py", {}),
    # omp seats under the sandbox HOME (review-cli#636) and omp's Anthropic account pool
    # (review-cli#637): the wrapper's real binary handed over as OMP_REAL_BIN, a wrapper
    # that cannot start reported unusable before dispatch, agent.db read read-only, one
    # account per attempt, the next one after a 429, the account named in the output and
    # in `review seats`, and the account policy (review-cli#641: exclude list, usage
    # threshold, env overrides, skip reasons). Unit tests patch attributes; the e2e runs
    # bin/review against a fake omp wrapper + fake real binary + fake agent.db in a temp HOME.
    ("test_omp_account_pool.py", {}),
    # Every harness login in `review seats` (review-cli#646: Claude Code, Codex, omp,
    # opencode, review-cli keys; no secret printed) and omp:zai seats on review-cli's
    # .env z.ai key. e2e runs bin/review in a temp HOME with fake claude/omp binaries.
    ("test_harness_accounts.py", {}),
    # review_omp's true-silence cooldown wiring (review-cli#235 omp half): mirrors
    # test_true_silence_cooldown_wiring.py's opencode coverage above -- recording AND
    # consulting an independent "omp" cooldown namespace, plus a real-subprocess,
    # unstubbed end-to-end reap of a genuinely hanging (zero-output) seat. Isolates its
    # own $REVIEW_SEAT_COOLDOWN_FILE per test — no shared env needed here.
    ("test_omp_true_silence_cooldown_wiring.py", {}),
    # The LIVE half of the omp cage: real-omp probes that the write/xdev, user-MCP,
    # and project-config execution holes stay closed (each was verified open pre-fix).
    # Opt-in via REVIEW_OMP_CAGE_LIVE=1 — self-skips in ~1s otherwise (CI stays hermetic).
    ("test_omp_cage_live.py", {}),
    ("test_readonly_agent.py", {}),
    ("test_specweb.py", {}),
    ("test_specweb_daemon.py", {}),
    ("test_claude_api.py", {}),
    ("test_provider_keys.py", {}),
    ("test_reviewer_board.py", {}),
    ("test_effort_run_flag.py", {}),
    # Pool/model-selection foolproofing (the pre-dispatch guard that turns a non-convergent
    # -m/--pool selection into an actionable proposal or a targeted per-provider error). Pure
    # logic, availability+reason injected — no backend, no network.
    ("test_pool_guard.py", {}),
    # The guard's liveness probe must be provider-failover-chain-aware, not just the
    # requested spelling (review of #157). Offline — backends.backend_available /
    # .backend_unavailable_reason / .runtime_provider_marked_unpaid are patched with manual
    # save/restore; no real backend/network.
    ("test_pool_guard_failover_wiring.py", {}),
    # Capability-aware seat resolution from the shared models.yaml manifest (rig-cli#8 consumer
    # side). All offline — writes a fixture manifest to a temp file + points $REVIEW_MODELS_MANIFEST
    # at it; no network, no real agent-tools checkout, no model call.
    ("test_manifest_capability.py", {}),
    # Per-role fallback model lists + harness seat resolution (review-cli#445).
    ("test_role_models.py", {}),
    ("test_review_marker.py", {}),
    ("test_review_remote.py", {}),
    ("test_install_review_remote.py", {}),
    # `review task --export / --import / --import-from`: iterations recorded on another machine (#659).
    ("test_task_export_import.py", {}),
    # The composer-stage review gate driven through REAL git commit/rebase/cherry-pick
    # (review-cli#396). Needs git; runs under pytest itself (see the file's own
    # `if __name__ == "__main__":`) because it uses capsys/monkeypatch/parametrize.
    ("test_review_gate_template.py", {}),
    # `review diff --staged --commit` (the checkpoint-commit feature): the usage gate
    # (--commit requires --staged), the checkpoint gate shared with the marker/stamp
    # (ok/staged/not-piped), the real `git commit` it makes, and the distinct
    # EXIT_COMMIT_FAILED when the commit subprocess itself (e.g. a rejecting hook) fails.
    ("test_review_commit_checkpoint.py", {}),
    # `review diff --json` (review-cli#546): the additive structured findings envelope --
    # extraction/coercion/report/exit-code units plus flat+board mode_review dispatch and
    # _handler wiring, all offline (resolve_backend/panel.resolve_backend swapped for an
    # in-process fake, mirroring test_reviewer_board.py / test_review_commit_checkpoint.py).
    ("test_json_findings.py", {}),
    # The pre-commit gate's trivial-follow-up delta tolerance (review-cli#208): a small
    # restage on top of an already-reviewed baseline is accepted without a fresh full
    # review, a substantive one is still blocked, and a pre-#208 stamp (no companion
    # review-stamp-diff) keeps today's exact-hash-only behavior. Exercises the ACTUAL
    # `_PRECOMMIT` shell script via subprocess against real temp git repos.
    ("test_review_stamp_delta_tolerance.py", {}),
    ("test_failover_pool.py", {}),
    ("test_levels.py", {}),
    ("test_panel_levels.py", {}),
    # Provider-failover: the per-model provider chain + last-working cache + the MID-REVIEW
    # switchover (provider A fails on the call, the model completes via B, board not degraded).
    # Offline — availability/unpaid injected, cache is a throwaway temp file.
    ("test_provider_failover.py", {}),
    # review-cli#594: a dead Claude CLI seat drops to its omp-routed twin (board, just-ask
    # and the --task role slots), is credited once under its own role, and bare
    # `review status` / `review entities` say why a seat cannot run. Offline — executors
    # faked by id; cache, logs, cooldown store and account registry in temp dirs.
    ("test_claude_omp_twin.py", {}),
    # review-cli#596: a Claude CLI login that failed is recorded per account, so bare
    # `review status` shows the seat UNUSABLE and the next call goes to the omp twin
    # without paying the failed call; a later success or a re-login clears it. Offline —
    # Claude CLI and omp faked; registry, cooldown store, cache and logs in temp dirs.
    ("test_claude_login_failure.py", {}),
    # review-cli#587: an explicit `review just-ask --timeout N` is a hard wall-clock bound —
    # a fake seat that never answers is killed with its process group at N, the command
    # names each seat and why it gave no answer, a seat wedged inside review is cut off by
    # the N + grace watchdog, and one -m model is one call. Fake `codex` executables on a
    # private PATH; the real bin/review runs with HOME/logs/stats/caches in temp dirs.
    ("test_just_ask_timeout.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Reuse-aware board/panel composition (review-cli#205): scarce/near-limit models
    # reuse across roles instead of shrinking the panel. Pure algorithm, no I/O.
    ("test_pool_reuse.py", {}),
    # The tg-ctl usage-percent bridge test_pool_reuse.py's consumer relies on — offline
    # (every core assertion passes `samples=`); the few real-file tests manage their own
    # $REVIEW_USAGE_LIMITS_FILE/_DIR save-restore internally, no shared env needed.
    ("test_usage_limits.py", {}),
    # The multi-account `~/.claude-accounts` registry discovery module (review-cli#168):
    # listing, `current` state, oauth email extraction, rotation wrap order,
    # $REVIEW_CLAUDE_ACCOUNTS_ROOT override. Hermetic — every test builds its own throwaway
    # registry under a temp dir; no real ~/.claude-accounts, no network.
    ("test_claude_accounts.py", {}),
    # The account-rotation fallback loops that module feeds (review-cli#168/#170):
    # an exhausted/chronic account is skipped and the next one in rotation order
    # answers, for BOTH the direct claude:* CLI seats and the omp-hosted Claude
    # seat. Patches backends attributes directly (no Popen), isolates its own
    # registry/log/cooldown env — no shared env needed here.
    ("test_account_rotation.py", {}),
    # The operator-facing "panel/board padded" warnings + quorum's <model>#N
    # duplicate-seat labelling (review-cli#205 round 2). Pure functions +
    # mocked run_panel/run_moderator, no real dispatch.
    ("test_reuse_warnings.py", {}),
    # The pre-dispatch "pool came up short" STDOUT notice (2026-08-28): names every
    # board seat unavailable before dispatch whenever the live pool is smaller than
    # requested, so the shrink is visible in the review output itself, not only in
    # stderr/logs. Mostly a pure function with `backend_unavailable_reason`
    # monkeypatched, no real backend/network -- except one call-site wiring test that
    # drives the real mode_review -> run_board_with_failover dispatch path (with a
    # patched-in fake backend), so it gets REVIEW_LOG_DIR isolated like its sibling
    # dispatch-driving tests (test_streaming.py, test_board_deadline_wiring.py) rather
    # than writing to the real log dir if a seat there ever fails (GLM review finding,
    # round 8).
    ("test_pool_shortfall_warning.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Adversarial review-rigor audit fixes (Alex, 2026-08-21): the adversarial base
    # prompt + evidence-for-a-clean-verdict surfacing, the security/tests role
    # blending, and quorum's opt-in --adversarial-check refutation pass. Pure
    # functions + mocked run_panel/run_moderator, no real dispatch.
    ("test_adversarial_review_rigor.py", {}),
    # The concurrency cap drives real _run_streamed subprocesses (which write live logs), so
    # give it a FRESH temp log dir like the other log-touching tests (review-cli#65).
    ("test_concurrency_cap.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Host-wide admission control (review-cli#614): admission math, provider budget + 429
    # feedback, FIFO, SIGKILL recovery and multi-process budget proofs, each test on its own
    # temp registry dir.
    ("test_admission.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_inseat_retry.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_brainstorm_diff.py", {}),
    ("test_brainstorm_dead_panel.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_brainstorm_empty_panel.py", {}),
    ("test_quorum_empty_experts.py", {}),
    ("test_mode_subcommands.py", {}),
    # qa is declared with announce_logs/stats_mode, so its dispatcher path can touch the
    # ETA/log store on the way to the early return — give it a FRESH temp log dir so the
    # run never writes into the dev's default log dir (matches the other log-touching tests).
    ("test_qa_mode.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The Phase-2 executor: prompt/parser/exit units + the MOCKED-tester DoD (the buggy SUT
    # verdicts FAIL, the good one PASS) — all deterministic, no backend. The fake tester
    # streams a fake transcript via the same plumbing, so give it a FRESH temp log dir. The
    # LIVE-backend DoD inside this file is gated on REVIEW_QA_LIVE=1 and skips in CI.
    ("test_qa_executor.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Run-scoped --effort honored by the qa write/exec tester (review-cli#127 harvest):
    # deterministic argv/prompt assertions on the codex/claude tester spawns, no live backend.
    ("test_qa_effort.py", {}),
    # The Phase-3 SUT-env lifecycle: stage-reuse / setup.sh-hook bring-up / health-gate /
    # GUARANTEED teardown (incl. on a throw + via the atexit hook) + config parsing. All
    # deterministic — a stdlib HTTP server for stage/health, shell setup.sh hooks for
    # bring-up/teardown, no docker, no model. The live docker-compose bring-up test inside
    # this file is gated on REVIEW_QA_DOCKER=1 and skips in CI.
    ("test_qa_env.py", {}),
    # The bot Tier-1 HERMETIC harness: the fake-Telegram server + suite parser + classifier
    # units, plus the 2-fixture DoD (the good bot verdicts PASS, the buggy one FAIL with a
    # finding) driven through the real harness (fake server + a real stdlib subprocess bot +
    # inject/capture). Deterministic, no network/token/model; the per-case waits are shrunk via
    # REVIEW_QA_BOT_*_TIMEOUT_S inside the file. Give it a FRESH temp log dir for consistency
    # with the other qa-touching files.
    ("test_qa_bot.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The bot AGENT-SIDE tier: a bridge bot (tg-ctl) driven by the agent's hook client — the suite
    # parser (Ask-question/Expect-card/Tap/Expect-answer), the new sut.bot knobs (ask_command/seed/
    # owner_id/sender_id/ready_file), the harness seam (emit_question/cards/tap/await_answer), and
    # the 2-config DoD (a faithful miniature bridge bot verdicts PASS replaying an answered re-fire,
    # FAILs under SUT_DUP_BUG=1 — the tg-cli#98 duplicate-card class) driven through the REAL harness
    # (fake Telegram + a real subprocess daemon + a real Unix-socket hook client). Deterministic, no
    # network/token/model; waits shrunk via REVIEW_QA_BOT_*_S in the file. Fresh temp log dir for
    # consistency with the other qa-touching files.
    ("test_qa_bot_agent_side.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The web Tier-1 DETERMINISTIC harness: the case-grammar parser + driver (goto/click/fill +
    # DOM assertions) + the Playwright gate + the dev-server health gate, plus the 2-fixture DoD
    # (the good web app verdicts PASS, the buggy one FAIL with a finding) driven through the REAL
    # driver against the fixtures' real HTML. Deterministic, NO browser needed (the driver speaks
    # a small PageDriver protocol an in-memory HTTP-backed fake page implements); the LIVE-browser
    # DoD inside the file is gated on REVIEW_QA_PLAYWRIGHT=1 and SKIPs without Chromium. Give it a
    # FRESH temp log dir for consistency with the other qa-touching files.
    ("test_qa_web.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The ext (VS Code extension) Tier-1 DETERMINISTIC harness: the case-grammar parser + driver
    # (Command/Open + notification/editor-text/webview assertions) + the VS Code gate + the JSON
    # runner protocol, plus the 2-fixture DoD (the good extension verdicts PASS, the buggy one FAIL
    # with a finding) driven through the REAL driver against a fake automation backed by each
    # fixture's behavior.json. Deterministic, NO VS Code needed (the driver speaks a small
    # ExtAutomation protocol an in-memory behavior-backed fake implements); the LIVE-VS-Code DoD
    # inside the file is gated on REVIEW_QA_VSCODE=1 and SKIPs without node + a VS Code binary. Give
    # it a FRESH temp log dir for consistency with the other qa-touching files.
    ("test_qa_ext.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The Tier-2 (LIVE tier): the per-SUT availability GATEs (each naming the exact missing creds),
    # the SKIP-LOUD path, the bot fail-closed safety, the config acceptance of the live driver
    # values, the web/ext skeletons (connect BLOCKS until #82) + the REAL bot driver's connect gate
    # (telethon-absent → controlled BLOCKED, no #82), the dispatch routing a tier:live block to a
    # controlled BLOCKED (never the un-caged executor), and the creds-doc/spec consistency. All
    # deterministic — NO creds, NO Telegram, NO browser, NO VS Code, NO network; heavy deps
    # (telethon/playwright) are stubbed in-process. Fresh log dir for consistency.
    ("test_qa_live.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # The Tier-2 LIVE bot DRIVER + suite runner (the real MTProto path, #82): the driver's connect/
    # send/expect/tap logic against an in-memory fake Telethon client, and the runner's case
    # sequencing + classification (Send/Expect, Expect-silent, the Tap flow, a non-runnable BLOCKED
    # case, a connect-failure BLOCKED transcript) against a scripted fake driver. Deterministic — NO
    # creds, NO telethon, NO Telegram, NO network; the fakes are the transport boundary.
    ("test_qa_live_driver.py", {}),
    ("test_sessions.py", {}),
    ("test_e2e_resume.py", {}),
    ("test_run_stats.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Backend-aware --task validation (review-cli#427). Skips lib-dependent
    # checks when agent-tools is not checked out beside review-cli (CI).
    ("test_taskref.py", {}),
    # Diff-identity binding (review-cli#213): repo/diff mismatch detection for
    # `review task CODE --check`'s self-merge-authority gate.
    ("test_diff_identity.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_forgejo_history.py", {}),
    ("test_backstop.py", {}),
    # External SIGTERM/SIGINT child reaper (review-cli#160) and its `--detach`/`jobs`/
    # `status`/`wait` companion feature. Both spawn real subprocesses (a throwaway
    # backend child + the actual `bin/review` CLI with the deterministic fake backend)
    # but need no network/creds/real model CLIs.
    ("test_signal_reaper.py", {}),
    # review-cli#163 signal-reaper spawn-race regression: the window between a real
    # subprocess.Popen() and _register_child() adding it to _LIVE_CHILDREN used to be
    # lock-free. Drives a genuine _run_streamed call (real log write), so it needs a
    # fresh log dir like the other log-touching tests (review-cli#65).
    ("test_signal_reaper_spawn_race.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_detach_jobs.py", {}),
    # review-cli#613/#516: a silent (or trickling-then-stalled) non-tty stdin must never
    # park `review` before dispatch. Spawns the real `bin/review` with a socketpair on fd 0
    # against a throwaway git repo; needs git like the other repo-driving suites.
    ("test_stdin_silent_pipe.py", {}),
    # Log-dir/log-file sandboxed-write fallback (review-cli#162): a seat must survive an
    # unwritable log location rather than crashing before its backend subprocess spawns.
    ("test_log_dir_fallback.py", {}),
    # review-cli#180: the $REVIEW_CLI_ACTIVE reentrancy guard that stops a codex/claude/
    # opencode backend from re-invoking `review` on the same worktree (each level of
    # recursion re-roots into a fresh OS session via `start_new_session=True`, so the
    # existing process-GROUP kill/backstop machinery structurally cannot bound it — an
    # env var is the one signal that survives that). Exercises main()'s guard wiring, a
    # real nested `bin/review` subprocess, and an end-to-end regression against a
    # stubbed codex binary that attempts self-reinvocation.
    ("test_reentrancy_guard.py", {}),
    # review-cli#180: the codex execpolicy `.rules` guard (`install.
    # install_codex_recursion_guard`) — the closest available equivalent to the claude
    # backend's `--tools ""` / opencode's `bash: deny` for a backend whose core
    # capability IS shell exec. Hermetic install/idempotency/content checks always run;
    # the integration check against codex's own `execpolicy check` engine SKIPs when
    # the `codex` CLI isn't on PATH.
    ("test_codex_recursion_guard_rules.py", {}),
    # From main's review-UX-chain (review-cli#44): help defaults, install hook text / state, topic help.
    ("test_help_defaults.py", {}),
    # review-cli#705: an unreadable config.yaml is a loud error, never silent defaults.
    ("test_config_loud.py", {}),
    ("test_install_hook_text.py", {}),
    ("test_install_state.py", {}),
    ("test_topic_help.py", {}),
    ("test_readme_anchors.py", {}),
    # intent_mentions_tag: multilingual (English + Russian) synonym matching for
    # intent-based module activation (tg#6188). Pure string/regex logic, no
    # ImageMagick/Pillow dependency, so it runs as a plain unit file rather than
    # via the gated visual-verification suite.
    ("test_intent_keywords.py", {}),
    # `review install-hook tg` (the tg pre-send-photo review-visual gate installer): isolated
    # HOME + a fake tg-cli checkout, no git needed.
    ("test_install_hook_tg.py", {}),
    # `review install-commit-hook` delegates to `rig apply` when rig is present (agent-tools#282's
    # shared agenttools_rig_delegate helper). The two delegation cases SKIP when that in-ecosystem
    # helper isn't installed (bare CI); the rig-absent / helper-missing cases always run.
    ("test_install_commit_hook_rig_delegate.py", {}),
    # The token-burn investigation's concrete fixes: the per-pass diff budget
    # (reviewlib.diff_chunks / $REVIEW_DIFF_MAX_BYTES — an over-budget diff is SPLIT per file
    # group and reviewed in several passes inside one run, never truncated; applied at the
    # mode_review dispatch boundary, deliberately NEVER in _git_diff, which would break the
    # --commit checkpoint's integrity check; a piped diff is exempt everywhere) and the
    # cross-invocation Fable cooldown cache (reviewlib.seat_cooldown) and its wiring into
    # review_claude/review_with_images. Both deterministic — no live backends, no network;
    # the diff-chunking e2e drives the real `bin/review diff` over the fake backend with
    # its own temp HOME/log/stats dirs; the seat_cooldown wiring tests redirect
    # $REVIEW_SEAT_COOLDOWN_FILE, $REVIEW_LOG_DIR, and
    # $REVIEW_CLAUDE_MODE (the cooldown-skip path writes a real sidecar log via
    # reviewlib.process.log_dir(), and REVIEW_CLAUDE_MODE=cli keeps the dispatch
    # deterministic on any host).
    ("test_diff_chunking.py", {}),
    ("test_seat_cooldown.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # Availability windows + fast health checks (review-cli#663): reset/Retry-After/credits/
    # auth parsing, store exclusion, board/admission wiring, `review health` end to end
    # against a local HTTP stub. Each test isolates its own cooldown store and log dir.
    ("test_availability.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_health.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    # `review stat`'s data model (reviewlib.dashboard.tokenstats): parsing per-call logs
    # into the per-harness/per-model breakdown, the REST-backend token-text parser, the
    # Fable dispatch/failure report, and byte-proxy stats. Deterministic — synthetic
    # CallLog fixtures, no real log dir, no network, no backend.
    ("test_tokenstats.py", {}),
    # The SQLite call-log archive (review-cli#480): store/seal/load round-trip, durability
    # across process exit, process.py's archive-on-finish write paths, the dashboard and
    # `review stat` read paths under the `days` window, and the migration's proofs. Every
    # test uses its own temp log dir (the archive lives inside it).
    ("test_log_archive.py", {}),
    # `review stat`'s CLI surface (argparse wiring, --since/--days resolution, --json vs
    # text rendering, --harness table filtering). Deterministic, no network/backend.
    ("test_stat_subcommand.py", {}),
    # `review feedback <run_id>` — the CLI path onto the overseer annotation store
    # (review-cli#355's 0% fill-rate fix). Isolates its own $REVIEW_STATS_FILE/
    # $REVIEW_DASHBOARD_STORE per test via its own `_Isolated` context manager, no
    # shared env needed here. Deterministic, no network/backend.
    ("test_feedback_subcommand.py", {}),
    # The persistent call-log cache (reviewlib.dashboard.call_log_cache) that lets a
    # repeat `review stat`/dashboard scan skip re-parsing unchanged log files.
    # Deterministic — synthetic tmpdirs, no real log dir, no network.
    ("test_call_log_cache.py", {}),
    # `_run_streamed`'s Popen stdin kwarg (review-cli#428): the argv-only backend
    # path (opencode/omp: input_text=None) must get stdin=DEVNULL, never a bare
    # None/inherited fd that could block forever reading review-cli's own stdin;
    # the codex/claude path (input_text provided) keeps stdin=PIPE. Verified via
    # a spied subprocess.Popen, no real opencode/omp/codex/claude process.
    ("test_process.py", {}),
    # reviewlib.portable: the one POSIX/Windows advisory-lock helper (flock semantics on
    # both: exclusive across opens/processes, LOCK_NB -> BlockingIOError, blocking waits)
    # plus the pid-probe / uid / Windows tree-kill shims. Real subprocesses, no network.
    ("test_portable.py", {}),
    # The live per-seat TUI observer (review-cli#548): the auto-enable gate (--no-tui /
    # $REVIEW_NO_TUI / both-streams-isatty), BoardReporter's state machine + rendering
    # against a fake stdscr, and the reporter's wiring into run_board_with_failover /
    # run_panel_with_retry (reserve promotion, in-seat retry, provider failover) via the
    # SAME _FakeBackends harness test_failover_pool.py uses. Each wiring test asserts the
    # corresponding raw stderr print is suppressed with a reporter and BYTE-IDENTICAL to
    # before this feature with reporter=None. No real curses/terminal needed here (see
    # test_tui_pty.py for that).
    ("test_tui.py", {}),
    # The REAL-pty half of review-cli#548: spawns the actual `bin/review diff` CLI attached
    # to a real pty (REVIEW_FAKE_BACKEND=1, no network/keys) and asserts the TUI actually
    # activates and paints role/model/status/elapsed, that a broken terminal (TERM=unknown)
    # falls back to the plain non-TUI output within a bounded time instead of hanging, and
    # that --no-tui / $REVIEW_NO_TUI disable it even under a fully-capable real pty. Needs
    # a real git binary (own throwaway repo per test) -- see _GIT_REQUIRED_UNIT_FILES.
    ("test_tui_pty.py", {}),
]


_VISUAL_UNIT_FILES = frozenset(
    {
        "test_cv_gate.py",
        "test_vision_client.py",
        "test_policy_engine.py",
        "test_pipeline.py",
        "test_preclassifier.py",
        "test_visual_compose.py",
        "test_visual_registry.py",
        "test_selection_highlight.py",
        "test_error_text_module.py",
        "test_visual_fanout.py",
    }
)
