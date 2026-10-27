"""CLI parsing constants and stable exit codes; policy data stays in levels.yaml."""
from __future__ import annotations

EXIT_NOT_A_REPO = 3

EXIT_GIT_DIFF_FAILED = 4

EXIT_QA_NO_SUITES = 6

EXIT_QA_NO_ENV = (
    7  # no stage AND no bring-up config for a backend/bot SUT (env harness, later)
)

EXIT_QA_SUT_BOOT_FAILED = (
    8  # the tester could not bring the SUT up at all (VERDICT: BLOCKED)
)

EXIT_QA_ENV_UNHEALTHY = (
    9  # bring-up succeeded but the health gate timed out (env harness, later)
)

EXIT_QA_NOT_IMPLEMENTED = 70

STDIN_WAIT_ENV = "REVIEW_STDIN_WAIT_SECONDS"

STDIN_WAIT_DEFAULT_SECONDS = 5.0

_STDIN_CHUNK = 1 << 16

_THREADED_WAIT_FLOOR = 0.1

_DASHBOARD_HOST_HELP = "interface to bind (default: 127.0.0.1 loopback-only; 0.0.0.0 exposes over Tailscale)"

_AGENTTOOLS_SERVICE_MISSING_MSG = (
    "[review dashboard] the managed-service subcommands need the shared "
    "'agenttools_service' lib, which isn't installed.\n"
    "  why:  run/start/status/stop/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon "
    "-e <agent-tools>/lib/agenttools_service\n"
    "  note: `review dashboard run` still works without it for an ad-hoc foreground server."
)

_TASK_SESSION_MATCH_WINDOW_SECONDS = 10 * 60

_HARNESS_ARG_ALIASES = {
    "zai": "z.ai",
    "zhipu": "z.ai",
    "glm": "z.ai",
    "glm53": "z.ai",
    "glm52": "z.ai",
    "glm51": "z.ai",
    "glm47": "z.ai",
    "glm46": "z.ai",
    "glm45": "z.ai",
    "oc": "opencode",
    "cc": "commandcode",
    "commoncode": "commandcode",
    "command-code": "commandcode",
    "command_code": "commandcode",
    "common-code": "commandcode",
    "common_code": "commandcode",
}

_USEFUL_CHOICES = {
    "yes": True,
    "y": True,
    "true": True,
    "no": False,
    "n": False,
    "false": False,
    "unsure": None,
    "unknown": None,
}

_ENTITY_HARNESS_LABELS = {
    "review_codex": "codex",
    "review_claude": "claude",
    "review_opencode": "opencode",
    "review_omp": "omp",
    "review_commandcode": "commandcode",
    "review_zai": "z.ai",
    "review_gemini": "gemini",
    "review_openrouter": "openrouter",
}

_SPECWEB_HELP = """review spec-web — multi-spec markdown reviewer (a persistent daemon)

DAEMON LIFECYCLE (shared agenttools_service lib, like `review dashboard`):
  review spec-web start --agent A [--host H] [--port N]   start the daemon in the background
  review spec-web status                        is it running? + registered specs + URLs
  review spec-web stop                          stop the daemon
  review spec-web run --agent A                 run the daemon in the foreground (this shell)
  review spec-web enable --agent A | disable    install / remove OS autostart

SPECS (spec-web-specific):
  review spec-web add <spec.md> [--agent A]     register a spec, print its name-based URL
  review spec-web serve <spec.md> --agent A     add + wait for the review (submit -> stdout)
  review spec-web <spec.md> --agent A            same as `serve` (backward-compatible)
  review spec-web list                          list registered specs
  review spec-web remove <name>                 unregister a spec
  review spec-web watch <name|path>             wait for a submit on an already-registered spec
  review spec-web reply <id> <answer> --spec P  the agent answers a reviewer's note

add/serve options: --seed FILE (import a review thread before serving), --open (open the
browser), --exit-on-submit (serve/watch return after the first submit), --no-watch (add:
register into the daemon and return without waiting).

--agent <name> is the tmux window/session that OWNS the served specs (e.g. --agent ext):
every submitted review batch is INJECTED into that live session (tg-ctl-style tmux
injection), so reviews reach the agent even when nothing is watching stdout. The
daemon-launching actions require it; a spec's own --agent (add/serve) overrides the
daemon default for that spec.

The daemon serves EVERY registered spec by name at /spec/<name> on ONE port (navigator at /),
so one port / Tailscale mapping covers all specs instead of a server per spec."""

_SPECWEB_LIFECYCLE = frozenset(
    {"start", "status", "stop", "run", "enable", "disable", "__serve"}
)

_SPECWEB_MISSING_LIB = (
    "[review spec-web] the daemon lifecycle needs the shared 'agenttools_service' lib, which "
    "isn't installed.\n"
    "  why:  start/status/stop/run/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy — same as `review dashboard`.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon -e <agent-tools>/lib/agenttools_service\n"
)

_SPECWEB_AGENT_REQUIRED = frozenset({"start", "run", "enable", "__serve"})

_WAITING_ON = {"submitted": "an agent reply", "answered": "reviewer resolution"}

_SERVER_SUBCOMMANDS = frozenset({"dashboard", "spec-web"})

_SPECWEB_FAST_ACTIONS = frozenset(
    {
        "reply",
        "start",
        "status",
        "stop",
        "enable",
        "disable",
        "add",
        "list",
        "remove",
        "-h",
        "--help",
    }
)

_VALUE_TAKING_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "-o",
        "--output",
        "--prompt",
        "--timeout",
        "--pool",
        "--preset",
        "--level",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--vision-timeout",
        "--project",
        # `--retry N` is a diff-mode-only int option (reviewlib/modes/review.py). It consumes a
        # value, so the `-o` pre-scan must skip its argument — otherwise `--retry -o…`-shaped input
        # would have the retry count mis-read as the output flag.
        "--retry",
        # `review feedback <run_id>`'s value-taking flags: `--note` is free text that can start
        # with `-` (e.g. `--note '-o drops the report text'`), so the `-o` pre-scan must skip
        # each one's argument instead of stealing it as an output path.
        "--note",
        "--useful",
        "--ticket",
        "--pr",
        # `review spec-web reply <id> <answer> --spec <path>`: the value after --spec is a spec
        # path that could look like an option (e.g. `--spec -odd-name.md`); list it so the `-o`
        # pre-scan never steals it.
        "--spec",
        # The qa mode's VALUE-taking flags (modes/qa.py): `--suites <glob/dir/file>`,
        # `--kind <shape>`, `--report <path>`, `--max-cases <N>`, plus the Phase-3 env flags
        # `--stage-url <URL>` and `--config <path>`. Their values can look like an option (a path,
        # `-1`), so the `-o` pre-scan must skip each one's argument. `--in-place` / `--keep-env`
        # are boolean flags (no value) and are deliberately NOT listed.
        "--suites",
        "--kind",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
    }
)

_LEADING_MODE_VALUE_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "--timeout",
        "--pool",
        "--preset",
        "--level",
    }
)

_LEADING_MODE_FLAG_OPTS = frozenset({"--list-defaults", "--show-board"})

_LEADING_MODE_INLINE_SHORT_OPTS = ("-m", "-C")

REVIEW_CLI_ACTIVE_ENV = "REVIEW_CLI_ACTIVE"

_SUBCOMMAND_ONLY_FLAGS: frozenset[str] = frozenset(
    {
        "--diff",
        "--staged",
        "--prompt",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--json",
        "--strict",
        "--no-ai",
        "--no-local-model",
        "--vision-timeout",
        "--project",
        "--retry",
        "--commit",
        # review diff's live per-seat status TUI off-switch (modes/review.py).
        "--no-tui",
        # quorum's opt-in adversarial refutation pass (modes/quorum.py).
        "--adversarial-check",
        # The qa mode's own flags (modes/qa.py); a verb-less `review --suites …` / `--kind …`
        # etc. must get the friendly "use the subcommand" pointer, not argparse's opaque error.
        # Phase 3 adds the SUT-env flags `--stage-url` / `--config` / `--keep-env`.
        "--suites",
        "--kind",
        "--in-place",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
        "--keep-env",
    }
)

_BARE_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "install-skill",
        "install-commit-hook",
        "install-hook",
        "dashboard",
        "sessions",
        "task",
        "stat",
        "entities",
        "capacity-check",
        "health",
        "feedback",
        "trust-module",
        "register-module",
        "spec-web",
        "jobs",
        "status",
        "wait",
        "seats",
        "doctor",
    }
)
