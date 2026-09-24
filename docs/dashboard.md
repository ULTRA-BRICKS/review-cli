# `review dashboard` — local web dashboard (managed service)

The dashboard is a **managed service**: it gets the same lifecycle subcommands every
long-running agent-tools server shares (run / start / status / stop / enable / disable),
from the reusable `agenttools_service` lib — review-cli does not hand-roll pidfiles or
autostart units.

```bash
review dashboard            # bare = HELP (prints the actions, launches NOTHING)
review dashboard run        # run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled
review dashboard start      # start in the BACKGROUND (detached daemon); returns immediately
review dashboard status     # is it running? pid / port / url / autostart-enabled
review dashboard stop       # stop the background instance
review dashboard enable     # install OS autostart (launchd / systemd --user / fallback) AND start now
review dashboard disable    # remove OS autostart AND stop
review dashboard --port 8765 run   # global --host/--port apply to any action
```

A bare `review dashboard` (no action) prints HELP and launches nothing. On `run`/`start`
it hints how to `enable` autostart at login. `run` binds an ephemeral port; the managed
`start`/`enable` bind a stable default port (so `status`'s url and a later restart land on
the same address).

A single-page web app over every review-cli run, built on the Python **stdlib** HTTP
server (no extra deps) and a **vanilla-JS SPA** (no npm/build step — assets ship in the
package). It binds **127.0.0.1 only** by default — the logs persist prompts/diffs that may
carry secrets — so it is not exposed on the network unless you pass `--host 0.0.0.0` (e.g.
to reach it over Tailscale).

**Dependency:** the lifecycle subcommands need the shared `agenttools_service` lib (an
in-ecosystem dependency, the `[dashboard]` extra). It is not yet on PyPI; until then install
it editable from the agent-tools checkout (`pip install -e <agent-tools>/lib/agenttools_daemon
-e <agent-tools>/lib/agenttools_service`). Without it, `run` still works for an ad-hoc
foreground server and a bare `review dashboard` still prints help; only the managed
start/status/stop/enable/disable actions emit an actionable error (exit 4).

**Supported autostart matrix:** macOS → launchd LaunchAgent; Linux → systemd `--user`
unit (with a no-systemd fallback); other OSes → no autostart (`enable` still starts the
service now and warns it will not survive reboot). See `agent-tools/lib/agenttools_service`
for the full matrix.

**Data sources (read-only):** review-cli does not emit a structured run record, so the
dashboard reads the real on-disk artifacts in `log_dir()` — the per-call streamed logs
(`{stamp}-{backend}-r{n}.log`) and the brainstorm discussion logs (`{stamp}-brainstorm.md`).
Subprocess backends (codex/claude/opencode) write these live; REST backends (gemini, z.ai,
commandcode) emit an equivalent sidecar log on every call — each under its OWN backend name,
so every backend is counted and attributed correctly. Calls are time-clustered into
**sessions** (review-cli emits no run id; a session = a burst of calls separated by a gap),
and the mode (review / panel / brainstorm) is inferred from the call/round shape. Task
metadata from `--task CODE` is parsed from those logs, so a dashboard session carries the
same task code as the CLI stat record.

**Panels:** Chat logs (per-run transcripts), Stats (runs over time, by task/mode/model/role),
Models & roles, Metrics (durations, success/fail rates), Overseer feedback, Modes, Errors,
Tasks (task-coded review history plus the **conscious** session marker), Prompts, and PR +
ticket links. A task badge filters runs by code and the task view shows iterations, models,
modes, related sessions, and drill-down links to the full conversations.

**The overseer's annotations** — free-text feedback, the per-session **conscious** flag,
and PR/ticket associations — are the only NEW persistence: a small atomic JSON store at
`~/.config/review-cli/dashboard.json` (override with `$REVIEW_DASHBOARD_STORE`), keyed by
the deterministic session id so annotations stay pinned as logs age out. The server exposes
small local-only JSON endpoints (`GET /api/runs|stats|runs/<id>`, `POST .../feedback|conscious|links`).

> Token/cost and an explicit run id are **not recorded** by review-core today; those panels
> show a graceful empty-state noting what review-core would need to log, rather than faking data.

---

[← Back to README](../README.md)
