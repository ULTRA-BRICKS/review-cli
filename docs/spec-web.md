# `review spec-web` — multi-spec web reviewer (a persistent daemon)

**Render any markdown spec server-side and review it like a GitHub PR — as a bidirectional
channel between a human reviewer and the agent that launched it.** Select any text in the
rendered spec → leave a **question** (expects an answer from the spec author) or a **remark**
(feedback that doesn't), anchored to that selection; notes accumulate in a **pending batch**;
one **Submit review** finalizes them **and delivers the structured review back to the
launching agent** (no copy-paste export step). The agent answers a question with
`review spec-web reply <id> <answer>` — the reply shows **in the UI** (distinct agent
styling) and is pushed to **Telegram**. In-progress note text **autosaves to disk** so a page
reload never loses a half-typed comment. Single implicit reviewer (no author field). Reusable
for *any* spec markdown file. Serve it over Tailscale to review from your phone.

**One daemon, every spec.** `review spec-web start` runs ONE persistent daemon (managed by the
shared `agenttools_service` lib, exactly like `review dashboard`) that serves EVERY registered
spec by NAME at `/spec/<name>` — a navigator at `/` lists them all — so a single port /
Tailscale mapping covers all your specs instead of one ad-hoc server per spec. `add` registers
a spec (idempotent by path; the name is a slug of the filename) and the daemon serves it
immediately, no restart. The daemon **live-reloads** an open spec page when the `.md` changes
on disk (SSE): the content swaps in place with **no scroll jump** — even when text above your
viewport changed height — and the changed blocks flash briefly (above or below the fold, never
scrolling to them). `start` is idempotent (an already-running daemon reports itself + what it
serves, exit 0); only an explicit `stop` stops it.

```sh
# daemon lifecycle (shared agenttools_service lib, like `review dashboard`)
review spec-web start --agent ext   # start the daemon in the background (idempotent)
review spec-web status              # running? + registered specs + their /spec/<name> URLs
review spec-web stop                # stop it (the ONLY thing that stops it)
review spec-web run --agent ext     # foreground (this shell), for ad-hoc use
review spec-web enable --agent ext  # OS autostart at login (disable removes it)

# specs
review spec-web add docs/specs/my-spec.md      # register + print its /spec/<name> URL
review spec-web serve docs/specs/my-spec.md --agent ext   # add + BLOCK until the review is submitted
review spec-web docs/specs/my-spec.md --agent ext         # same as `serve` (backward-compatible form)
review spec-web list                           # every registered spec + open-note counts (+ stalled-thread reminders)
review spec-web remove <name>                  # unregister (comments stay on disk)
review spec-web watch <name|path>              # wait for a submit on a registered spec

# pre-load an existing Q&A thread
review spec-web serve docs/specs/my-spec.md --agent ext --seed thread.json

# return to the agent as soon as the reviewer submits (one-shot review handoff)
review spec-web serve docs/specs/my-spec.md --agent ext --exit-on-submit

# the agent answers a reviewer's question (shown in the UI + sent to Telegram)
review spec-web reply <comment-id> "the answer" --spec docs/specs/my-spec.md
```

| Flag | Meaning |
|------|---------|
| `--agent <name>` | the tmux window/session that OWNS the served specs — submitted review batches are injected INTO it (see below). **Required** by the daemon-launching actions (`start`/`run`/`enable`, `serve` and the positional form); also required by `add` when the daemon is DOWN, since `add` auto-starts it. A per-spec `--agent` (on `add`/`serve`) overrides the daemon-wide default for that spec. |
| `--host` | daemon bind host (default `0.0.0.0` — reachable over Tailscale; `127.0.0.1` for loopback-only) |
| `--port` | daemon port (default `7920`, stable across restarts so URLs/Tailscale mappings survive) |
| `--seed FILE` | import an initial review thread from a JSON file before serving |
| `--exit-on-submit` | return after the first Submit (the daemon keeps running; only the watch returns) |
| `--emit-current` | `watch`: re-emit the batch ALREADY in the store before watching — the recovery path when a submit's live delivery failed (a bare `watch` only fires on a *later* submit) |
| `--no-watch` | `serve` / the positional form: register + print the URL but don't block |
| `--open` | open the URL in a browser on startup |

On a host **without** the shared `agenttools_service` lib, the positional form falls back to
the classic single-spec foreground server (ephemeral port, Ctrl-C to stop), so nothing breaks.

**The submit → agent handoff.** `review spec-web` is started by an agent and blocks. When the
reviewer clicks **Submit review**, the server marks the batch submitted in the store and the
launching process prints the **structured review** to stdout framed by a line that is exactly
`<<<REVIEW-SPEC-WEB-SUBMITTED`, then **one line** of compact JSON, then a line that is exactly
`REVIEW-SPEC-WEB-SUBMITTED>>>`. **Parse by taking the single line immediately after the begin
marker** (not by splitting on the end marker) — the JSON is one line and a reviewer's free
text is JSON-escaped on it, so a body that happens to contain the marker substring can never
break the framing. The payload carries every comment's **id** (so the agent can answer a
specific one), `kind`, `status`, `quote`, `section_title`, `body`, and its reply thread, plus
`counts` (questions/remarks/total). By default the server keeps serving after a submit (the
reviewer can keep going and the agent can `reply`), re-emitting a fresh framed payload on each
submit; `--exit-on-submit` returns after the first submit instead. (Like the other persistent
server paths, `--exit-on-submit` ignores `-o FILE` — read the framed payload from stdout.) The
store on disk stays the single source of truth.

**Submit → agent delivery (`--agent`).** The stdout handoff above only reaches an agent that is
actively watching the process's stdout — but the daemon is long-lived and the agents that
launch specs come and go, so a submitted batch used to sit in the store with nobody reading it.
The required `--agent <name>` fixes that: on every Submit, the batch is **injected into that
agent's live tmux session** as a prompt — the same mechanism `tg-ctl` uses to inject inbound
Telegram messages (`[TG from …]`) into a running session. `<name>` matches a tmux **window**
name first, then a **session** name (e.g. `--agent ext` reaches the pane of the `ext` session).
Each comment arrives on one line (`[SPEC-WEB comment on <spec> §<section>] "<quote>" — <body>
(question, id <id>)`) with a trailing pointer to `review spec-web reply <id> "<answer>" --spec
<path>`. Delivery is **best-effort**: the batch is already durably in the store, so a missing
tmux session never fails the Submit — the failure is logged and reported to the reviewer in the
UI, and the agent can still pull it with `review spec-web watch`. A daemon-wide `--agent` (on
`start`/`run`) is the default; a per-spec `--agent` (on `add`/`serve`) overrides it for that spec.

**The agent reply → UI + Telegram.** `review spec-web reply <comment-id> <answer> --spec <spec>`
threads the agent's answer under that comment in the store (stamped with the `agent` author so
the UI styles it distinctly), and **also** delivers it to Telegram via the `tg` CLI on `PATH`
(best-effort — a missing/failing `tg` never fails the reply; it logs and continues; `--no-tg`
skips it). An open spec-web page **polls** the comments API, so the agent's reply appears under
the question without a manual refresh.

**Stalled review-cycle reminders.** A comment stuck in `submitted` (awaiting an agent reply) or
`answered` (awaiting reviewer resolution) for more than 24h (override via
`$REVIEW_SPECWEB_STALE_HOURS`) is flagged with a `⚠ stalled ...` line — lazily, whenever an
already-running command reads the store, never via a background poller. `review spec-web list`
and `review spec-web status`/`start` print one line per stalled comment for every spec; `review
spec-web reply` prints the same for every OTHER stalled comment on that spec to stderr, and
appends a short summary to its Telegram delivery (e.g. "2 other stalled review threads on this
spec (oldest 36.2h, waiting on an agent reply)."), skipped when nothing else is stale.

**Drafts (reload-safe).** While you type a note, the composer **autosaves** the in-progress
text to the server (debounced, persisted to disk) under a per-slot key (the new-note composer
and each edit-in-progress have their own slot). On page load the most recent draft is **restored
into the composer**, so an accidental reload mid-typing continues exactly where you left off.
Saving the note (or clearing the box) drops its draft.

**Layout.** Desktop (≥900px) = two panes side-by-side (spec left, comments right, a
draggable divider). Mobile (<900px) = comments as a bottom sheet under the spec. The
comments panel collapses to just its header bar (which carries a **count badge** so added
notes are visible while collapsed) and re-expands from that same bar. Following an internal
cross-reference link shows a **← Back** control that returns to the prior scroll position.

**Rendering.** Markdown → HTML is rendered server-side with the GitHub heading-slug scheme,
so the spec's own internal links (`[§9.4](#94-…)`) resolve. Figures referenced as
`./assets/fig-*.svg|png` are served as real HTTP resources at `/asset/<name>` (never
inlined). Tables size columns to their content and scroll horizontally on narrow screens
rather than crushing columns.

**Comments.** A note stores the selected quote, the containing section id, char offsets, its
**kind** (`question` | `remark`, default `remark`), the body, created-at, a status
(`pending`/`submitted`/`answered`/`resolved`), and a thread of replies. (An `author` field
is still persisted for import/export round-tripping but is not shown in the UI.) The kind is
surfaced with a coloured chip + icon; each note can be edited (`/api/comments/<id>/edit`). On
reload, each note re-anchors by locating its quote within its section and highlighting it; a
quote that can't be re-found shows in the sidebar as **unanchored** (never a crash).

**Persistence.** One JSON file per spec at `~/.config/review-cli/spec-web/<sha1-of-abspath>.json`
(mode `0600`), surviving restarts. It holds the comments, the in-progress composer **drafts**
(per slot), and the `last_submit` batch the launching agent reads back. The daemon's spec
**registry** (`registry.json`, name → path) lives in the same directory — and the comment
stores are keyed by spec PATH, not by name, so specs reviewed under the old one-server-per-spec
mode keep their full history when the daemon takes over. Override the directory with
`$REVIEW_SPECWEB_DIR`. Override the stalled-review-cycle reminder threshold (default 24h) with
`$REVIEW_SPECWEB_STALE_HOURS`.

**Security.** Reads (spec, assets, comments, **drafts**) are open — same model as comments
(the in-progress composer text is readable by anyone who can reach the port; there are no
secrets in a spec review); only figures the markdown *references* are served (an unrelated
file in the assets dir 404s), SVGs are served with a `sandbox` CSP so a directly-opened one
can't run script, and symlinked assets that escape the assets dir are refused. Writes (post
comment / reply / submit / import / draft autosave) are origin-guarded against both CSRF and
DNS rebinding: a write requires (1) the request's **Host** to be in the allowlist —
**loopback + this machine's Tailscale identity (discovered at runtime via `tailscale status`,
never hardcoded) + `$REVIEW_SPECWEB_ALLOWED_HOSTS`** (comma-separated) — and (2) the
**Origin/Referer** to match that Host (classic CSRF check), plus
`Content-Type: application/json` and a body-size cap. The Host allowlist is what stops a
rebound attacker hostname from posting same-origin. So to accept writes from a phone over
Tailscale, the Tailscale name/IP must be discovered or listed in
`$REVIEW_SPECWEB_ALLOWED_HOSTS` (e.g. `REVIEW_SPECWEB_ALLOWED_HOSTS=ultras-mbp.tailbfe8ea.ts.net,100.123.113.82`).
Note the daemon binds `0.0.0.0` (not loopback-only) by default (see `--host` above to restrict
it to `127.0.0.1`) — the Host allowlist is what makes that default safe.

**Seed / import JSON shape** (`comments` is the only required key):

```json
{
  "comments": [
    {
      "quote": "the selected text",
      "body": "the question or comment",
      "section_id": "94-...",
      "section_title": "§9.4 ...",
      "kind": "question",
      "author": "alex",
      "status": "submitted",
      "batch": "2026-06-14T10:00:00+00:00",
      "replies": [ { "author": "claude", "body": "the answer" } ]
    }
  ]
}
```

Missing `id`/`created` are generated; unknown keys are ignored. Add `"replace": true` at the
top level to discard existing comments before importing.

Routes: `GET /` (SPA shell), `GET /static/<app.css|app.js>`, `GET /asset/<name>`,
`GET /api/spec`, `GET /api/comments`, `GET /api/drafts`, `POST /api/comments`,
`POST /api/comments/<id>/{reply,edit,status,delete}`, `POST /api/drafts/<slot>`,
`POST /api/submit`, `POST /api/import`. (The old `GET /api/export` is removed — Submit now
delivers the structured review to the launching agent.)

---

[← Back to README](../README.md)
