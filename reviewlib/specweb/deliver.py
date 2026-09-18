"""Deliver a submitted spec-web review into the OWNING AGENT's live tmux pane.

Why this exists: a submitted review used to only sit in the JSON store waiting for a
``watch_submits`` poller — if no process was watching (the daemon is long-lived, launching
agents come and go), the reviewer's comments reached NOBODY. Now every spec (or the daemon
itself) carries an ``--agent <name>`` owner, and on Submit the batch is INJECTED into that
agent's tmux pane as a prompt, the same way ``tg-ctl`` injects inbound Telegram messages
into a live Claude Code session (``[TG from …]``).

The injection recipe is tg-ctl's live-proven one (tg-cli features/tg-ctl/inject.ts):
  * single line  -> ``tmux send-keys -t <pane> -l <text>`` (literal mode, special-char safe);
  * multi-line   -> ``tmux load-buffer -`` + ``tmux paste-buffer -p -d -t <pane>``
                    (bracketed paste — a literal LF submits early in canonical-mode REPLs);
  * then a PACED separate ``send-keys Enter`` after ~500ms — a combined or too-fast Enter is
    dropped by the Ink TUI (the single most common failure mode in tmux-injecting bots).

Discovery runs ``tmux list-panes -a`` with an EXPLICIT UTF-8 locale: under launchd the daemon
inherits no locale and tmux then escapes the TAB separators in ``-F`` output to ``_``,
collapsing every line to one field (the tg-ctl "not in tmux" saga — see tg-cli's PANE_FORMAT
comment). The agent NAME matches a tmux window name first, then a session name (both exact,
then case-insensitive) — e.g. ``--agent ext`` reaches the pane of the tmux session ``ext``.

Everything here is BEST-EFFORT by contract: a failed delivery must never fail the Submit
(the review is already durably in the store); the caller logs the (ok, detail) outcome.

When the tmux hand-off above fails (most commonly: the review was launched from a process
with no tmux/omp session at all, e.g. a ChatGPT-plugin agent — there is simply no pane to
match), ``notify_admin_via_tg`` is a SECOND best-effort channel: it pings the human reviewer
directly on Telegram (mirroring ``reviewlib/cli.py::_spec_web_reply_to_tg``'s tg-invocation
shape) so the submitted comments don't just sit silently in the store until someone thinks
to run ``review spec-web watch <name> --emit-current``. Same contract: never raises, never
allowed to fail the Submit.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Tab-separated so names with spaces survive; tabs are safe under the forced UTF-8 locale.
_PANE_FORMAT = (
    "#{pane_id}\t#{session_name}\t#{window_name}\t#{window_active}\t#{pane_active}"
)

# Pre-Enter gap (tg-ctl's competitor-source-proven pacing for Ink TUIs).
_ENTER_GAP_SECONDS = 0.5

# Clip the reviewer's free text so one batch can't paste an essay into the agent's prompt.
_QUOTE_CLIP = 160
_BODY_CLIP = 1000

# Telegram's hard message cap. The admin fallback embeds format_review_message's
# already-per-comment-clipped output verbatim, but batch size (comment count) is unbounded,
# and every field here (including operator-configured ``agent``/``spec_name``) is escaped
# with `html.escape`, which can expand the string up to 5x (`&` -> `&amp;`). Per-field clips
# below (``_esc_clip``) bound the common case cheaply, but ``_format_admin_fallback_message``
# also applies one final hard clamp to this cap on the ASSEMBLED message — the only way to
# actually guarantee "a pathological name/batch can't push the send over budget on its own",
# since the recovery command's ``spec_name``/``agent`` copies are deliberately NOT
# field-clipped (they must stay byte-for-byte correct for the printed command to work when
# pasted). All these caps are counted in TELEGRAM's own unit (UTF-16 code units — see
# ``_telegram_len``), not Python's code-point ``len()``: astral-plane characters (many emoji,
# common in review comments) are 2 UTF-16 units but 1 Python char, so a code-point count can
# stay "under budget" while the real send is still rejected as oversized.
_TELEGRAM_MESSAGE_CAP = 4096
_FALLBACK_TRUNCATION_MARKER = (
    "\n… (truncated — pull the full batch with the command below)"
)
_FALLBACK_REASON_CLIP = 200
_FALLBACK_BODY_CLIP = 3400
_FALLBACK_AGENT_CLIP = 60
_FALLBACK_SPEC_NAME_CLIP = 100

# `html.escape(..., quote=False)` only ever emits three entities: `&amp;`, `&lt;`, `&gt;`. A
# length-based clip on already-escaped text can land partway through one, leaving a dangling
# `&`/`&a`/`&am`/`&l`/`&g`/etc that is NOT a valid entity — Telegram's HTML parser rejects the
# whole message for it ("can't parse entities"). Matching every letter run after a bare `&`
# (not just the three complete-minus-semicolon spellings) covers every possible cut point;
# a COMPLETE entity always ends in `;`, which this pattern (anchored on end-of-string) never
# touches, so real entities at a clip boundary survive untouched. See ``_clip_escaped``.
_PARTIAL_ENTITY_TAIL = re.compile(r"&[a-zA-Z]*$")


def _telegram_len(s: str) -> int:
    """Telegram counts message length in UTF-16 code units, not Python's code-point
    ``len()``. Astral-plane characters (outside the Basic Multilingual Plane — most emoji)
    are 2 UTF-16 units but a single Python character, so a code-point-based cap can
    undercount and still get a send rejected as over Telegram's real ~4096-unit limit."""
    return len(s.encode("utf-16-le")) // 2


def _clip_escaped(escaped: str, limit: int, marker: str = "…") -> str:
    """Clip an ALREADY html-escaped string to ``limit`` Telegram (UTF-16) units without
    landing inside an entity (see ``_PARTIAL_ENTITY_TAIL``'s docstring) and without the
    appended ``marker`` itself pushing the result back over ``limit``. Shared by every
    clip site in this module so the entity-boundary fix lives in exactly one place.
    Every caller here passes a ``marker`` far shorter than its ``limit`` (60-4096 units vs
    a 0-1 unit marker, or the truncation marker paired with a correspondingly raised
    limit — see the body-clip call site), so a ``limit`` too small to fit ``marker`` is
    always a caller bug, not reachable input; raised loudly (a plain ``if``, not `assert`
    — assertions are stripped under `python -O`, and this module's "never raises, never
    hangs" fallback contract can't afford a silently-vanished guard turning into an
    infinite trim loop on a negative budget) rather than silently clamped."""
    marker_units = _telegram_len(marker)
    if limit < marker_units:
        raise ValueError(f"limit {limit} smaller than marker {marker!r}")
    if _telegram_len(escaped) <= limit:
        return escaped
    budget = limit - marker_units
    # A code-point slice can only OVER-shoot `limit` UTF-16 units (each Python char is
    # 1-2 units), never under-shoot `limit` code points — the loop below trims the exact
    # surplus down to `budget` regardless of which direction this seed missed by. `cut`
    # is bounded by the `while cut` guard even if `budget` were somehow negative (belt and
    # braces alongside the raise above — the loop itself must never be able to spin).
    cut = escaped[:limit]
    while cut and _telegram_len(cut) > budget:
        cut = cut[:-1]
    # rstrip BEFORE stripping a dangling entity tail: trailing whitespace after a
    # partial entity (e.g. "...&am\n") would otherwise block the `$`-anchored entity
    # regex, then get stripped itself afterward, re-exposing the same dangling "&am".
    cut = _PARTIAL_ENTITY_TAIL.sub("", cut.rstrip())
    return cut + marker


def _subproc_env() -> dict:
    """A guaranteed-UTF-8 env for tmux calls (launchd inherits no locale; see module doc)."""
    import os

    env = dict(os.environ)
    env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def _tmux(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env=_subproc_env(),
    )


def list_panes() -> list[dict]:
    """Every pane on the tmux server as {pane_id, session, window, window_active, pane_active}.

    tmux absent / no server / bad output -> [] (never raises): delivery then degrades to
    "no pane found", which the caller logs."""
    try:
        out = _tmux(["list-panes", "-a", "-F", _PANE_FORMAT])
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows: list[dict] = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue  # a mangled line (wrong locale / odd name) is dropped, not crashed on
        rows.append(
            {
                "pane_id": parts[0],
                "session": parts[1],
                "window": parts[2],
                "window_active": parts[3] == "1",
                "pane_active": parts[4] == "1",
            }
        )
    return rows


def match_agent_pane(rows: list[dict], agent: str) -> str | None:
    """The pane id for ``agent`` from a pane listing (pure — unit-testable).

    Match order: window NAME exact -> session name exact -> both case-insensitive. A window
    match prefers that window's ACTIVE pane; a session match requires the session's active
    window's active pane (the pane the agent is actually attached to)."""

    def _pick(pred) -> str | None:
        matched = [r for r in rows if pred(r)]
        if not matched:
            return None
        active = [r for r in matched if r["pane_active"]]
        return (active[0] if active else matched[0])["pane_id"]

    for fold in (False, True):
        want = agent.lower() if fold else agent
        name_of = (lambda v: v.lower()) if fold else (lambda v: v)
        pane = _pick(lambda r: name_of(r["window"]) == want)
        if pane:
            return pane
        pane = _pick(lambda r: name_of(r["session"]) == want and r["window_active"])
        if pane:
            return pane
    return None


def find_agent_pane(agent: str) -> str | None:
    return match_agent_pane(list_panes(), agent)


def _clip(text: str, limit: int) -> str:
    text = " ".join(
        (text or "").split()
    )  # collapse newlines/runs: each comment is ONE line
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc_clip(text: str, limit: int) -> str:
    """`_clip`, but ``limit`` bounds the ESCAPED text `tg` actually receives. `html.escape`
    can expand `&`/`<`/`>` up to 5x, so the text must be escaped BEFORE it is clipped — the
    reverse order lets an otherwise-small, special-char-heavy field blow straight past
    ``limit`` once escaped. Entity-boundary-safe via ``_clip_escaped``."""
    escaped = html.escape(" ".join((text or "").split()), quote=False)
    return _clip_escaped(escaped, limit)


def format_review_message(
    spec_name: str, spec_path: Path | str, review: dict, batch: str | None
) -> str:
    """The prompt injected into the agent's pane for ONE submitted batch (pure).

    Only THIS batch's comments are included (older batches were delivered on their own
    submit). Shape (one line per comment, the format the CTO specified):

        [SPEC-WEB review on <name> — N comment(s) submitted]
        [SPEC-WEB comment on <name> §<section>] "<quote>" — <body> (question, id <id>)
        [SPEC-WEB reply with: review spec-web reply <id> "<answer>" --spec <path>]

    Plain text ONLY — this is injected directly into a tmux pane as a prompt, so it must
    stay free of HTML entities; ``_format_admin_fallback_message`` is what HTML-escapes a
    copy of this output for the separate Telegram channel, which has a different threat
    model (see its docstring)."""
    comments = [
        c
        for c in (review.get("comments") or [])
        if batch is None or c.get("batch") == batch
    ]
    lines = [f"[SPEC-WEB review on {spec_name} — {len(comments)} comment(s) submitted]"]
    for c in comments:
        section = (c.get("section_title") or c.get("section_id") or "").strip()
        at = f" §{section}" if section else ""
        quote = _clip(c.get("quote") or "", _QUOTE_CLIP)
        quoted = f' "{quote}" —' if quote else ""
        kind = c.get("kind") or "remark"
        lines.append(
            f"[SPEC-WEB comment on {spec_name}{at}]{quoted} {_clip(c.get('body') or '', _BODY_CLIP)}"
            f" ({kind}, id {c.get('id', '?')})"
        )
    lines.append(
        f'[SPEC-WEB reply with: review spec-web reply <id> "<answer>" --spec {spec_path}]'
    )
    return "\n".join(lines)


def inject_text(pane_id: str, text: str) -> tuple[bool, str]:
    """Inject ``text`` + a paced Enter into a tmux pane (tg-ctl's recipe). (ok, detail)."""
    try:
        if "\n" in text:
            if _tmux(["load-buffer", "-"], stdin=text).returncode != 0:
                return False, "tmux load-buffer failed"
            if _tmux(["paste-buffer", "-p", "-d", "-t", pane_id]).returncode != 0:
                return False, "tmux paste-buffer failed"
        else:
            if _tmux(["send-keys", "-t", pane_id, "-l", text]).returncode != 0:
                return False, "tmux send-keys failed"
        time.sleep(_ENTER_GAP_SECONDS)
        if _tmux(["send-keys", "-t", pane_id, "Enter"]).returncode != 0:
            return False, "tmux send-keys Enter failed"
        return True, f"injected into pane {pane_id}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"tmux error: {exc}"


def deliver_review(
    *,
    agent: str,
    spec_name: str,
    spec_path: Path | str,
    review: dict,
    batch: str | None,
) -> tuple[bool, str]:
    """Deliver one submitted batch to ``agent``'s pane. Best-effort, never raises."""
    try:
        pane = find_agent_pane(agent)
        if pane is None:
            return False, f"no tmux window/session named '{agent}'"
        ok, detail = inject_text(
            pane, format_review_message(spec_name, spec_path, review, batch)
        )
        return ok, detail
    except Exception as exc:  # noqa: BLE001 — delivery must never break the submit
        return False, f"{type(exc).__name__}: {exc}"


def _format_admin_fallback_message(
    *,
    agent: str,
    spec_name: str,
    spec_path: Path | str,
    review: dict,
    batch: str | None,
    reason: str,
) -> str:
    """The Telegram message sent to the admin when tmux hand-off to ``agent`` failed (pure).

    Reuses ``format_review_message``'s batch-scoped per-comment lines verbatim instead of
    re-deriving the clip/format logic — only the framing around it differs: a human reads
    this on their phone, not an agent's prompt, so it opens with WHY delivery failed and
    closes with the watch/emit-current recovery command (the same one already shown in the
    browser alert, ``static/app.js``). ``reason`` is bounded (``_FALLBACK_REASON_CLIP``) and
    the embedded batch block is bounded (``_FALLBACK_BODY_CLIP``) — per-comment clipping in
    ``format_review_message`` does not bound the number of comments in a batch, and an
    unbounded message just makes `tg` reject the whole send instead of rescuing it. ``agent``
    is clipped in the reason line too (an operator-configured tmux/session name, but still
    unbounded input) — the recovery command's own ``spec_name`` copy is NOT clipped: it must
    stay byte-for-byte correct for the printed command to actually work when pasted.

    Every field is HTML-escaped (``_esc_clip``, escape-THEN-clip — see its docstring) and
    ``notify_admin_via_tg`` sends this with an EXPLICIT ``--format html``. This is NOT
    optional/defensive dressing: `tg` auto-upgrades a message to HTML parse mode whenever the
    body merely CONTAINS a recognized Telegram tag name (tg-cli
    ``features/render/html.ts::detectHtmlTags``, wired into the send path at
    ``tg:userHtml = fmt === "html" || detectHtmlTags(...)`` and confirmed by tg-cli's own
    ``tests/html-auto-detect.test.ts`` E2E suite — e.g. a body containing literal ``<code>``
    or ``<b>`` sends with ``parse_mode=HTML`` even with no ``--format`` flag). Every comment's
    ``body``/``quote``/``section_title`` here is untrusted reviewer free text that plausibly
    contains exactly those tag names (code review comments routinely mention ``<code>``,
    ``<a href>``, HTML/JSX snippets, …) — if we relied on tg's content-based autodetection
    instead of an explicit flag, ONE such comment would flip the WHOLE message (including the
    unescaped literal ``<id>``/``<answer>`` framing below, which is not a real Telegram tag)
    into HTML parse mode, and Telegram's server-side entity parser would then reject the
    entire send ("can't parse entities") — silently defeating the exact rescue path this
    function exists to provide. Explicitly forcing ``--format html`` makes the parse mode
    deterministic regardless of comment content; escaping first is then required so our own
    literal `<`/`&` characters read as literal text under real HTML parsing rather than as
    (invalid) tags — tg does not re-escape an already-HTML-formatted body it was explicitly
    told is HTML (tg-cli's own "explicit --format html is not double-processed" guarantee).

    The recovery line's ``agent``/``spec_name`` copies skip the per-field clip (byte-for-byte
    correctness for the pasted command), so nothing above actually bounds the ASSEMBLED
    message on its own — the final hard clamp to ``_TELEGRAM_MESSAGE_CAP`` below (via
    ``_clip_escaped``, also entity-boundary-safe) is what makes the "can't blow the budget"
    guarantee true regardless of field lengths; it only ever bites in the pathological case
    (a very long agent/spec name), trading the pasted command's byte-for-byte accuracy for
    the fallback actually being delivered."""
    body = html.escape(
        format_review_message(spec_name, spec_path, review, batch), quote=False
    )
    if _telegram_len(body) > _FALLBACK_BODY_CLIP:
        # Raise the limit by the marker's own width so `_FALLBACK_BODY_CLIP` still bounds
        # the CONTENT (matching this constant's original meaning), while routing the
        # "trim + append marker without a dangling entity" step through the one shared
        # mechanism instead of re-implementing it by hand at this call site.
        body = _clip_escaped(
            body,
            _FALLBACK_BODY_CLIP + _telegram_len(_FALLBACK_TRUNCATION_MARKER),
            marker=_FALLBACK_TRUNCATION_MARKER,
        )
    lines = [
        f'Spec-web review delivery to agent "{_esc_clip(agent, _FALLBACK_AGENT_CLIP)}" '
        f"failed: {_esc_clip(reason, _FALLBACK_REASON_CLIP)}",
        f"Spec: {_esc_clip(spec_name, _FALLBACK_SPEC_NAME_CLIP)}",
        "",
        body,
        "",
        f"Pull it when '{html.escape(agent, quote=False)}' is back: review spec-web watch "
        f"{html.escape(spec_name, quote=False)} --emit-current",
    ]
    return _clip_escaped("\n".join(lines), _TELEGRAM_MESSAGE_CAP)


def notify_admin_via_tg(
    *,
    agent: str,
    spec_name: str,
    spec_path: Path | str,
    review: dict,
    batch: str | None,
    reason: str,
) -> tuple[bool, str]:
    """Fallback delivery when the tmux hand-off to ``agent`` failed (e.g. no tmux/omp session
    exists at all — a ChatGPT-plugin-launched agent). Pings the human reviewer directly via
    the `tg` CLI on PATH, mirroring ``reviewlib/cli.py::_spec_web_reply_to_tg``'s invocation
    shape, with three deliberate departures from it:
      * ``--tag report`` — tg's `--tag` accepts ONLY the lowercase-english words
        answer/decision/problem/report (see tg-cli features/render/tag.ts); this is a status
        report about a failed hand-off, not a reply or an escalation.
      * ``--format html`` — deterministic HTML parse mode regardless of what a reviewer's
        comment happens to contain, paired with escaping the whole message
        (``_format_admin_fallback_message``'s docstring has the full "why", including the
        confirmed tg-cli autodetection behavior this defends against). ``--title`` is left
        unescaped: tg always treats ``--title`` as a plain field and escapes it internally
        regardless of ``--format`` (tg-cli ``features/cli/dict-gate.ts``: "The --title is
        always a plain field (the header renderer escapes it)").
      * ``--no-feature auto-attach --no-feature screenshot-guard`` — the embedded batch text
        is untrusted reviewer free text. Left default-on, `auto-attach` would upload any
        filesystem path a comment happens to mention (tg-cli's recursive-attach resolves
        bare filenames under the daemon's cwd/worktree too), and `screenshot-guard` would
        hard-block the whole send if a comment merely mentions the word "screenshot" —
        neither is an acceptable side effect of a best-effort notification fallback.
    Best-effort by the same contract as the rest of this module: NEVER raises — a bug
    in the fallback must not break `/api/submit` either. (ok, detail)."""
    try:
        exe = shutil.which("tg")
        if not exe:
            return False, "tg not on PATH"
        message = _format_admin_fallback_message(
            agent=agent,
            spec_name=spec_name,
            spec_path=spec_path,
            review=review,
            batch=batch,
            reason=reason,
        )
        try:
            proc = subprocess.run(
                [
                    exe,
                    "--tag",
                    "report",
                    "--format",
                    "html",
                    "--no-feature",
                    "auto-attach",
                    "--no-feature",
                    "screenshot-guard",
                    "--title",
                    f"Spec-web review needs '{_clip(agent, _FALLBACK_AGENT_CLIP)}' — "
                    f"{_clip(spec_name, _FALLBACK_SPEC_NAME_CLIP)}",
                    message,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=_subproc_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"tg error: {type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"tg exit {proc.returncode}: {err}"
        return True, "notified admin via tg"
    except Exception as exc:  # noqa: BLE001 — fallback must never break the submit
        return False, f"{type(exc).__name__}: {exc}"
