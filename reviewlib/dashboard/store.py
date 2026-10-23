"""The ONLY new persistence layer for the dashboard.

review-cli's log artifacts are read-only history; they do not capture the things the
overseer (the human lead) wants to attach AFTER a run:

  * ``feedback``   — free-text overseer feedback on a session;
  * ``conscious``  — a per-session boolean the overseer toggles to flag a session as
                     "conscious" (deliberately reviewed / acted on — the Tasks panel);
  * ``useful``     — a lightweight tri-state (None/True/False) usefulness rating,
                     orthogonal to ``conscious`` (a run can be reviewed but useless, or
                     useful but never consciously reviewed);
  * ``links``      — PR/ticket associations ({"prs": [...], "tickets": [...]}), which
                     the overseer attaches in the UI and which auto-detection seeds from
                     a run's cwd/branch where possible.

Stored as a single JSON file under the config dir (``~/.config/review-cli/dashboard.json``,
overridable via ``$REVIEW_DASHBOARD_STORE`` for tests), keyed EITHER by the dashboard's
own deterministic session id from ``parser._session_id_for`` (set via the web UI) OR by
a ``"run-<run_id>"`` key from ``reviewlib.stats.run_id_for`` (set via ``review
feedback <run_id>`` from the CLI — see review-cli's feedback-fill-rate fix) — this
module treats both as opaque string keys and doesn't care which scheme produced one, so
annotations stay pinned even as logs age out and re-cluster. Writes are atomic (temp
file + os.replace) and the file is created 0600 (it may hold review notes).

KNOWN GAP (review-cli#357, filed from code review on the feedback-fill-rate fix): a
``"run-<run_id>"`` entry is currently INVISIBLE in the dashboard's own per-session
views (``server._merge_annotation`` looks up a session's annotation by its OWN
``sess-<timestamp>`` id, which a `run-<run_id>` key never matches) — the run-stats
store and the call-log-derived session model have no join between them today. It DOES
still count toward the aggregate `feedback_count`/`conscious_count` on `/api/stats`
(``server.py`` sums over every entry in the store regardless of key scheme), so the
fill-rate signal this fix exists to create is real and visible there; only the
per-run drill-down in the dashboard UI is the open gap, tracked separately rather than
folded into this already-shipped change.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import portable

_LOCK = threading.RLock()

# Cross-process locking (review-cli feedback-fill-rate fix, review finding): before
# `review feedback` existed, this store had exactly ONE writer — the single long-lived
# dashboard server — so an in-process-only `threading.RLock` around each
# load-modify-write was already enough (a single process can't race itself). `review
# feedback` makes this store a PER-INVOCATION multi-process writer for the first time
# (two agents running `review feedback` concurrently, or one racing the web UI's own
# write), which the in-process lock alone cannot serialize. See `_locked`'s own
# docstring for the mechanics — mirrors `reviewlib.seat_cooldown._locked` (trimmed:
# this is a lower-stakes annotation cache, not a review-authority-gating cooldown),
# ported rather than shared, following that module's own precedent of duplicating
# `reviewlib.specweb.store.SpecStore._guard` instead of importing it.
_LOCK_TOTAL_DEADLINE_SECONDS = 2.0
_FLOCK_RETRY_INTERVAL_SECONDS = 0.02
_FLOCK_SUB_BUDGET_SECONDS = 0.15

# A HYP-style ticket or a bare PR number. Loose on purpose — the overseer is trusted,
# this only guards against absurd input, not adversaries (local-only server).
_TICKET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_PR_RE = re.compile(r"^#?\d+$")

# Sentinel distinguishing "leave this field alone" from an explicit `None` -- which
# IS a valid value for both `feedback` (clears it) and `useful` (marks "unrated").
# `apply_feedback` needs a way to tell the two apart that a bare `None` default can't.
UNSET = object()


def store_path() -> Path:
    override = os.environ.get("REVIEW_DASHBOARD_STORE")
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".config" / "review-cli" / "dashboard.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def _locked():
    """Exclusive advisory lock serializing the read-modify-write pair every
    ``set_*``/``add_link``/``remove_link`` does: acquire the in-process `_LOCK` first
    (serializes same-process threads unconditionally), then a cross-process
    `reviewlib.portable.lock_file` (`fcntl.flock` on POSIX, `msvcrt.locking` on
    Windows) on a dedicated
    `.<name>.lock` sidecar file (never `store_path()` itself — it may not exist yet on
    a first-ever write, and `_write_store`'s `os.replace` swaps the underlying inode
    out from under any fd opened on the real path directly). Degrades progressively —
    to `_LOCK`-only, then to fully unlocked — rather than ever blocking indefinitely or
    aborting a write: a failure to lock must never turn into a failure to record
    feedback. Bounded by `_LOCK_TOTAL_DEADLINE_SECONDS` total, with the flock retry on
    its own smaller `_FLOCK_SUB_BUDGET_SECONDS` sub-budget so a thread holding `_LOCK`
    releases it back to other same-process threads quickly on flock contention instead
    of holding it hostage for the cross-process half's sake (same reasoning as
    `reviewlib.seat_cooldown._locked`, which this mirrors in miniature)."""
    deadline = time.monotonic() + _LOCK_TOTAL_DEADLINE_SECONDS
    got_lock = _LOCK.acquire(timeout=max(0.0, deadline - time.monotonic()))
    try:
        if not got_lock:
            yield
            return
        p = store_path()
        lock_path = p.parent / f".{p.name}.lock"
        fd = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            flock_deadline = min(deadline, time.monotonic() + _FLOCK_SUB_BUDGET_SECONDS)
            while True:
                try:
                    portable.lock_file(fd, blocking=False)
                    break
                except BlockingIOError:
                    if time.monotonic() >= flock_deadline:
                        _fd_to_close, fd = fd, None
                        try:
                            os.close(_fd_to_close)
                        except OSError:
                            pass
                        break
                    time.sleep(_FLOCK_RETRY_INTERVAL_SECONDS)
        except OSError:
            if fd is not None:
                _fd_to_close, fd = fd, None
                try:
                    os.close(_fd_to_close)
                except OSError:
                    pass
        try:
            yield
        finally:
            if fd is not None:
                try:
                    portable.unlock_file(fd)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        if got_lock:
            _LOCK.release()


def _empty() -> dict:
    return {"version": 1, "sessions": {}}


def load_store() -> dict:
    p = store_path()
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if (
        not isinstance(data, dict)
        or "sessions" not in data
        or not isinstance(data["sessions"], dict)
    ):
        return _empty()
    data.setdefault("version", 1)
    return data


def _write_store(data: dict) -> None:
    p = store_path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".dashboard.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_annotation() -> dict:
    return {
        "feedback": None,
        "conscious": False,
        # `useful` (review-cli feedback-fill-rate fix): a lightweight, orthogonal-to-
        # `conscious` usefulness signal ("was this run actually worth the tokens it
        # burned") the overseer/`review feedback` sets alongside free-text feedback.
        # `conscious` means "deliberately reviewed/acted on" — a run can be conscious
        # but useless, or useful but never consciously reviewed — so this is its own
        # tri-state field: `None` (never rated), `True`, `False`.
        "useful": None,
        "links": {"prs": [], "tickets": []},
        "updated": None,
    }


def _session_record(data: dict, session_id: str) -> dict:
    return data["sessions"].setdefault(session_id, _default_annotation())


def get_annotation(session_id: str) -> dict:
    """Return the overseer annotation for a session (defaults if none stored)."""
    with _locked():
        data = load_store()
        rec = data["sessions"].get(session_id)
    if rec is None:
        return _default_annotation()
    # Normalize shape defensively (older/hand-edited files).
    rec.setdefault("feedback", None)
    rec.setdefault("conscious", False)
    rec.setdefault("useful", None)
    links = rec.setdefault("links", {"prs": [], "tickets": []})
    links.setdefault("prs", [])
    links.setdefault("tickets", [])
    rec.setdefault("updated", None)
    return rec


def all_annotations() -> dict:
    with _locked():
        return dict(load_store()["sessions"])


def set_feedback(session_id: str, text: str | None) -> dict:
    with _locked():
        data = load_store()
        rec = _session_record(data, session_id)
        rec["feedback"] = (text or "").strip() or None
        rec["updated"] = _now()
        _write_store(data)
        return rec


def apply_feedback(
    session_id: str,
    *,
    feedback=UNSET,
    useful=UNSET,
    ticket: str | None = None,
    pr: str | None = None,
) -> dict:
    """Apply any combination of feedback/useful/ticket-link/pr-link to a session in a
    SINGLE load-modify-write cycle — one `_locked()` critical section for the whole
    call, not up to four separate ones. Review finding (review-cli#355, round 2):
    `review feedback`'s caller-side pre-validation (`validate_ticket`/`validate_pr`)
    makes a bad `--ticket`/`--pr` abort before ANY write, but the writes themselves —
    feedback, useful, then the two link appends — were still four independent store
    round-trips. An I/O failure or a concurrent writer landing between two of them
    could leave a genuinely partial annotation, contradicting the "all-or-nothing"
    contract for the parts that DO get validated up front. `ticket`/
    `pr` here MUST already be validated by the caller (this function does not call
    `validate_ticket`/`validate_pr` itself, and does not dedup against pre-existing
    entries beyond the same `in` check `add_link` uses) — it exists to compose their
    effects into one atomic write, not to replace their own validation. `feedback`/
    `useful` default to the `UNSET` sentinel (not `None`, a valid value for both) so
    "not given" and "explicitly cleared/unrated" stay distinguishable."""
    with _locked():
        data = load_store()
        rec = _session_record(data, session_id)
        if feedback is not UNSET:
            rec["feedback"] = (feedback or "").strip() or None
        if useful is not UNSET:
            rec["useful"] = useful if useful is None else bool(useful)
        if ticket is not None:
            npr_ticket = _norm_ticket(ticket) or ticket
            if npr_ticket not in rec["links"]["tickets"]:
                rec["links"]["tickets"].append(npr_ticket)
        if pr is not None:
            npr = _norm_pr(pr) or pr
            if npr not in rec["links"]["prs"]:
                rec["links"]["prs"].append(npr)
        rec["updated"] = _now()
        _write_store(data)
        return rec


def looks_like_ticket(value: str) -> bool:
    """True when `value` matches the same ticket-id shape `add_link`/`_norm_ticket`
    validate (e.g. ``HYP-742``). Public wrapper around `_TICKET_RE` for callers
    (`review feedback`'s task_code auto-link) that need to check the shape WITHOUT
    reaching into this module's private regex directly."""
    return bool(_TICKET_RE.match(value))


def set_conscious(session_id: str, conscious: bool) -> dict:
    with _locked():
        data = load_store()
        rec = _session_record(data, session_id)
        rec["conscious"] = bool(conscious)
        rec["updated"] = _now()
        _write_store(data)
        return rec


def _norm_pr(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if _PR_RE.match(value):
        return value if value.startswith("#") else "#" + value
    return None


def _norm_ticket(value: str) -> str | None:
    value = value.strip().upper()
    if _TICKET_RE.match(value):
        return value
    return None


def validate_pr(value: str) -> str:
    """Normalize + validate a PR reference (`123` or `#123`); raise ``ValueError``
    (with a message naming the expected shape) instead of returning `None` on bad
    input. Public so a caller (`review feedback`) can validate BEFORE writing
    anything else, rather than discovering an invalid `--pr` only after a `--note`/
    `--useful` write has already landed."""
    npr = _norm_pr(value)
    if npr is None:
        raise ValueError(f"invalid PR reference: {value!r} (expected '#123' or '123')")
    return npr


def validate_ticket(value: str) -> str:
    """Normalize + validate a ticket id (e.g. ``HYP-742``); raise ``ValueError`` (with
    a message naming the expected shape) instead of returning `None` on bad input.
    See `validate_pr` for why this is public."""
    nt = _norm_ticket(value)
    if nt is None:
        raise ValueError(
            f"invalid ticket reference: {value!r} (expected e.g. 'HYP-742')"
        )
    return nt


def add_link(
    session_id: str, *, pr: str | None = None, ticket: str | None = None
) -> dict:
    """Attach a PR (#123) and/or a ticket (HYP-742) to a session. Dedups.

    Both are validated BEFORE the lock is taken / anything is written — a bad
    `ticket` no longer leaves a good `pr` (or vice versa) written behind a raised
    exception; the whole call is all-or-nothing."""
    npr = validate_pr(pr) if pr is not None else None
    nt = validate_ticket(ticket) if ticket is not None else None
    with _locked():
        data = load_store()
        rec = _session_record(data, session_id)
        links = rec["links"]
        if npr is not None and npr not in links["prs"]:
            links["prs"].append(npr)
        if nt is not None and nt not in links["tickets"]:
            links["tickets"].append(nt)
        rec["updated"] = _now()
        _write_store(data)
        return rec


def remove_link(
    session_id: str, *, pr: str | None = None, ticket: str | None = None
) -> dict:
    with _locked():
        data = load_store()
        rec = _session_record(data, session_id)
        links = rec["links"]
        if pr is not None:
            npr = _norm_pr(pr) or pr
            links["prs"] = [x for x in links["prs"] if x != npr]
        if ticket is not None:
            nt = _norm_ticket(ticket) or ticket.strip().upper()
            links["tickets"] = [x for x in links["tickets"] if x != nt]
        rec["updated"] = _now()
        _write_store(data)
        return rec
