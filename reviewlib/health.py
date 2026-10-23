"""Fast health checks: is each seat answering right now, learned before a review pays for it.

`review health` (and the automatic pre-dispatch check) probes seats in parallel with the
cheapest call each backend offers — the seat's own backend asked to "Reply with exactly: OK"
over no diff, bounded by a short timeout (5 s) — and feeds the outcome to the same store
the real dispatches write (`availability`, `seat_cooldown`): a seat that answers has its
stale exclusion dropped; a seat that fails with a quota/credits/login error is excluded
until the time the error names; a seat that did not answer in time is reported `unknown`
and left alone (a slow CLI is not a dead one).

Results are cached with a TTL (`seat-health.json` next to the cooldown store) so the
pre-dispatch check of many reviews started close together probes each seat once. A seat
already excluded by a window is not probed — except when its `probe_after` has come (a long
quota window or a credits exhaustion is re-tried periodically, so a top-up ends it early).

A probe never takes an admission slot, writes no review log and no run-stats record: it
runs the backend function directly with logs redirected to a scratch directory.

Nothing here raises into a review: `preflight` swallows every failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import availability, seat_cooldown

PROBE_PROMPT = "Reply with exactly: OK\nDo not use any tools, do not read any files, add nothing else."
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_TTL_SECONDS = 300.0
# A seat that did not answer in time is re-probed sooner than one that did.
UNKNOWN_TTL_SECONDS = 60.0
# Agentic CLI seats (claude/codex/omp/opencode) need several seconds just to start.
AGENTIC_TIMEOUT_SECONDS = 15.0
# A due re-probe that got no answer is tried again this much later.
REPROBE_RETRY_SECONDS = 15 * 60.0

_ENV_PREFLIGHT = "REVIEW_HEALTH_PREFLIGHT"
_ENV_TTL = "REVIEW_HEALTH_TTL_SECONDS"
_ENV_TIMEOUT = "REVIEW_HEALTH_TIMEOUT_SECONDS"
SCHEMA = "review-health"
SCHEMA_VERSION = 1

OK = "ok"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"


@dataclass
class SeatProbe:
    """One seat's health. `until` is set only for an unavailable seat with a known end;
    `probed` says whether a live call was made now (else the answer is the store or the
    cache), `cached` that the answer is a still-fresh earlier probe."""

    seat: str
    status: str
    detail: str
    until: float | None = None
    cause: str | None = None
    scope: str | None = None
    probed: bool = False
    cached: bool = False
    fresh_window: bool = False  # this probe itself recorded a new window
    elapsed_ms: int | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("fresh_window")
        data["until_iso"] = (
            datetime.fromtimestamp(self.until, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if self.until is not None
            else None
        )
        return data


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value == value and value != float("inf") else default


def default_timeout() -> float:
    return max(1.0, _env_float(_ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS))


def default_ttl() -> float:
    return _env_float(_ENV_TTL, DEFAULT_TTL_SECONDS)


def preflight_enabled() -> bool:
    raw = os.environ.get(_ENV_PREFLIGHT, "").strip().lower()
    return raw not in ("0", "off", "false", "no") and default_ttl() > 0


# ---- TTL cache ------------------------------------------------------------------------


def cache_path() -> Path:
    return seat_cooldown.cooldown_path().with_name("seat-health.json")


def _load_cache() -> dict:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(updates: dict) -> None:
    """Merge `updates` into the cache file (atomic replace; a racing writer can lose an
    entry, which only costs one more probe)."""
    try:
        path = cache_path()
        data = _load_cache()
        data.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".seat-health.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 — a cache we could not write costs a probe, no more
        pass


def _fresh_cached(seat: str, cache: dict, now: float, ttl: float) -> SeatProbe | None:
    entry = cache.get(seat)
    if not isinstance(entry, dict):
        return None
    at, status = entry.get("at"), entry.get("status")
    if type(at) not in (int, float) or status not in (OK, UNKNOWN):
        return None
    limit = ttl if status == OK else min(ttl, UNKNOWN_TTL_SECONDS)
    if not 0 <= now - at <= limit:
        return None
    detail = entry.get("detail") if isinstance(entry.get("detail"), str) else ""
    return SeatProbe(seat, status, detail, cached=True)


# ---- one live probe -------------------------------------------------------------------


def _live_probe(seat: str, timeout: float) -> SeatProbe:
    """Run the seat's backend once over no diff and classify the outcome. Records the
    result in the availability store. Never raises."""
    from . import backends
    from .panel import result_is_usable

    started = time.monotonic()

    def done(status: str, detail: str, **kw) -> SeatProbe:
        return SeatProbe(
            seat,
            status,
            detail,
            probed=True,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            **kw,
        )

    try:
        backend = backends.resolve_backend(seat)
        with tempfile.TemporaryDirectory(prefix="review-health-") as scratch:
            result = backends._call_backend_now(
                backend, seat, PROBE_PROMPT, "", Path(scratch), max(1, int(timeout)), 0
            )
    except Exception as exc:  # noqa: BLE001 — a probe that crashed says nothing about the seat
        return done(UNKNOWN, f"probe error: {type(exc).__name__}: {exc}"[:200])

    if result_is_usable(result):
        availability.observe(seat, result, usable=True)
        _clear_legacy(seat)
        return done(OK, "answered")
    if result.returncode == 124:
        return done(UNKNOWN, f"no answer within {int(timeout)}s")
    found = availability.observe(seat, result, usable=False)
    if found is not None:
        stored = backends.availability_exclusion(seat)
        until = (
            stored.until
            if stored is not None
            else (found.until if found.until is not None else time.time())
        )
        return done(
            UNAVAILABLE,
            availability.format_window(found.reason, until),
            until=until,
            cause=found.cause,
            fresh_window=True,
        )
    excluded = backends.availability_exclusion(seat)
    if excluded is not None:
        # an older escalating-timer entry (or the backend's own cooldown) explains it
        return done(
            UNAVAILABLE,
            excluded.describe(),
            until=excluded.until,
            cause=excluded.cause,
            scope=excluded.scope,
        )
    # No window explains it and it is not transient: report it, but do not exclude on the
    # strength of a probe alone — a probe runs with an empty diff outside any repo, so a
    # failure it cannot classify may be its own setup, not the seat.
    return done(
        UNKNOWN, f"probe failed: {_first_line(result) or f'exit {result.returncode}'}"
    )


def _first_line(result) -> str:
    for text in (result.stderr, result.stdout):
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()[:160]
    return ""


def _recorded_new_window(probe: SeatProbe, started: float) -> bool:
    """True when this probe itself recorded a fresh window (its re-probe is then scheduled by
    that record) rather than leaving the old, due one in place."""
    return probe.fresh_window


def _defer_reprobe(seat: str, when: float) -> None:
    """Push the re-probe of `seat`'s window (and its provider's) out to `when`."""
    seat_cooldown.defer_probe(seat, when)
    key = availability.provider_key(seat)
    if key is not None:
        seat_cooldown.defer_probe(key, when)


def _clear_legacy(seat: str) -> None:
    """A seat that just answered is not in any older escalating-timer cooldown either."""
    from . import backends

    for method in backends._availability_methods(seat):
        seat_cooldown.clear_cooldown(seat, access_method=method)


def timeout_for(seat: str, explicit: float | None) -> float:
    """The probe timeout for `seat`: `explicit` when given, else the HTTP default, raised to
    the agentic default for a CLI-backed seat (claude / codex / omp / opencode start a
    process whose cold start alone is 3-8 s)."""
    if explicit is not None:
        return max(1.0, explicit)
    base = default_timeout()
    try:
        from . import backends

        backend = backends.resolve_backend(seat)
        if backend in (
            backends.review_claude,
            backends.review_codex,
            backends.review_omp,
            backends.review_opencode,
        ):
            return max(base, AGENTIC_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — the default timeout is always safe
        pass
    return base


# ---- batch ----------------------------------------------------------------------------


def probe_seats(
    seats: list[str],
    *,
    timeout: float | None = None,
    ttl: float | None = None,
    live: bool = True,
    now: float | None = None,
) -> list[SeatProbe]:
    """Health of every distinct seat, in input order. `ttl` > 0 lets a fresh cached probe
    answer; `live=False` reports only what the store and static checks know (no calls).
    `timeout` None = automatic: 5 s per HTTP seat, 15 s per agentic CLI seat (their cold
    start alone is several seconds). Live probes run in parallel, so the batch returns
    within the longest timeout + a second or two whatever the backends do."""
    from . import backends

    ttl = 0.0 if ttl is None else ttl
    now = time.time() if now is None else now
    ordered = list(dict.fromkeys(seats))
    cache = _load_cache() if ttl > 0 else {}
    results: dict[str, SeatProbe] = {}
    to_probe: list[str] = []
    due: set[str] = set()
    for seat in ordered:
        excluded = backends.availability_exclusion(seat)
        static = (
            None if excluded is not None else backends.backend_unavailable_reason(seat)
        )
        if excluded is not None and not excluded.probe_due(now):
            results[seat] = SeatProbe(
                seat,
                UNAVAILABLE,
                excluded.describe(),
                until=excluded.until,
                cause=excluded.cause,
                scope=excluded.scope,
            )
        elif static is not None:
            results[seat] = SeatProbe(seat, UNAVAILABLE, static)
        elif not live:
            results[seat] = SeatProbe(seat, UNKNOWN, "not probed (--no-probe)")
        else:
            cached = _fresh_cached(seat, cache, now, ttl) if excluded is None else None
            if cached is not None:
                results[seat] = cached
            else:
                if excluded is not None:
                    # a due re-probe: the backend itself refuses a seat with an older
                    # timer entry, so drop that one (a failed probe records it again)
                    _clear_legacy(seat)
                    due.add(seat)
                to_probe.append(seat)
    if to_probe:
        results.update(
            _probe_parallel(to_probe, {s: timeout_for(s, timeout) for s in to_probe})
        )
        # A due re-probe that got no answer must not come due again at every preflight:
        # push it out so the next try is a quarter hour away.
        for seat in to_probe:
            if seat in due and not _recorded_new_window(results[seat], now):
                _defer_reprobe(seat, now + REPROBE_RETRY_SECONDS)
    return [results[s] for s in ordered]


def _probe_parallel(
    seats: list[str], timeouts: dict[str, float]
) -> dict[str, SeatProbe]:
    """Probe `seats` concurrently on daemon threads (a hung backend cannot hold the process
    open) and collect what finished within the deadline; the rest are `unknown`."""
    out: dict[str, SeatProbe] = {}
    lock = threading.Lock()

    def run(seat: str) -> None:
        probe = _live_probe(seat, timeouts[seat])
        with lock:
            out[seat] = probe

    # Probe logs are scratch: the dashboard must not list a health check as a review call.
    scratch_logs = tempfile.TemporaryDirectory(prefix="review-health-logs-")
    previous_log_dir = os.environ.get("REVIEW_LOG_DIR")
    os.environ["REVIEW_LOG_DIR"] = scratch_logs.name
    try:
        threads = [
            threading.Thread(target=run, args=(seat,), daemon=True) for seat in seats
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + max(timeouts.values()) + 2.0
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
    finally:
        if previous_log_dir is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = previous_log_dir
    now = time.time()
    with lock:
        finished = dict(out)
    for seat in seats:
        finished.setdefault(
            seat,
            SeatProbe(
                seat,
                UNKNOWN,
                f"no answer within {int(timeouts[seat])}s",
                probed=True,
            ),
        )
    _store_cache(
        {
            seat: {"at": now, "status": p.status, "detail": p.detail}
            for seat, p in finished.items()
            if p.status in (OK, UNKNOWN)
        }
    )
    # the scratch dir may still be written by a hung thread; best-effort removal
    try:
        scratch_logs.cleanup()
    except OSError:
        pass
    return finished


def preflight(seats: list[str]) -> list[SeatProbe]:
    """The automatic check before a review dispatches: probe the seats the run may use
    (cached for `$REVIEW_HEALTH_TTL_SECONDS`, default 300 s), so a dead seat is in the
    availability store — hence out of board selection and admission planning — before the
    run asks for a slot. One stderr line names a seat found unavailable. Disabled by
    `$REVIEW_HEALTH_PREFLIGHT=0`, under the fake test backend, and for no seats. Never
    raises and never blocks longer than the probe timeout."""
    try:
        from . import backends

        if not seats or not preflight_enabled() or backends._fake_backend_enabled():
            return []
        results = probe_seats(seats, ttl=default_ttl())
        fresh = [r for r in results if r.probed and r.status == UNAVAILABLE]
        for r in fresh:
            print(
                f"[review-cli] health: {r.seat} unavailable — {r.detail}",
                file=sys.stderr,
                flush=True,
            )
        return results
    except Exception:  # noqa: BLE001 — the check is an optimisation, never a gate
        return []


# ---- rendering ------------------------------------------------------------------------


def report(results: list[SeatProbe], *, host: str, timeout: float, live: bool) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "host": host,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe_timeout_seconds": timeout if timeout is not None else default_timeout(),
        "agentic_timeout_seconds": None
        if timeout is not None
        else AGENTIC_TIMEOUT_SECONDS,
        "live": live,
        "seats": [r.to_json() for r in results],
    }


def render_text(rep: dict) -> str:
    seats = rep["seats"]
    width = max((len(s["seat"]) for s in seats), default=0)
    mode = "live probes" if rep.get("live") else "store only"
    lines = [
        f"Seat health @{rep['host']} ({mode}, timeout {rep['probe_timeout_seconds']:g}s"
        + (
            f", {rep['agentic_timeout_seconds']:g}s for CLI seats"
            if rep.get("agentic_timeout_seconds")
            else ""
        )
        + ", "
        f"{rep['checked_at']}):"
    ]
    for s in seats:
        label = {OK: "ok         ", UNAVAILABLE: "UNAVAILABLE", UNKNOWN: "unknown    "}[
            s["status"]
        ]
        tail = s["detail"]
        if s.get("elapsed_ms") is not None:
            tail += f" [{s['elapsed_ms'] / 1000:.1f}s]"
        elif s.get("cached"):
            tail += " [cached]"
        lines.append(f"  {label}  {s['seat'].ljust(width)}  {tail}")
    ok = sum(1 for s in seats if s["status"] == OK)
    lines.append(
        f"{ok}/{len(seats)} ok, "
        f"{sum(1 for s in seats if s['status'] == UNAVAILABLE)} unavailable, "
        f"{sum(1 for s in seats if s['status'] == UNKNOWN)} unknown"
    )
    return "\n".join(lines)


# ---- remote hosts ---------------------------------------------------------------------

# A non-interactive ssh shell often lacks the user's PATH; the same preamble review-remote
# uses to find `review` on the box.
_REMOTE_PATH = 'export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"'


def run_remote(host: str, args: list[str], *, timeout: float) -> dict:
    """`review health --json <args>` on `host` over ssh; its report, or a report-shaped
    error. The remote host probes with its own logins and updates its own store."""
    import shlex

    cmd = f"{_REMOTE_PATH}; review health --json " + " ".join(
        shlex.quote(a) for a in args
    )
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "--",
                host,
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"host": host, "error": f"ssh failed: {exc}"}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {
            "host": host,
            "error": f"no JSON from `review health` (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:300]}",
        }
    data["host"] = host
    return data
