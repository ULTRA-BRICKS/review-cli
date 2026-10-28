"""How long a seat, provider or account stays unavailable, learned from the failure itself.

Why this exists
---------------
A quota that ran out used to cost every later review something: the failing seat kept
being dispatched (REST seats had no cooldown at all), kept holding a provider reservation
at admission, and a human had to comment the seat out of config.yaml and remember to put
it back. The provider already says when it will answer again — z.ai's ``429`` code 1310
"Your limit will reset at 2026-10-26 05:58:34", a Claude CLI "session limit · resets
7:30pm", an HTTP ``Retry-After`` — so this module reads that out of the error, records
``unavailable_until`` in the cooldown store (``seat_cooldown``, bucket ``window``) and lets
board selection, admission planning and `review --show-board` exclude exactly that seat
until then.

What is recorded
----------------
``classify`` turns one failed result into an :class:`Unavailability`:

* ``quota`` — a session/weekly/monthly/usage limit. Ends at the reset time the error names;
  with none named, the escalating timer picks the window. An exhaustion that names its reset
  is account-wide for providers that bill one account (z.ai, commandcode, gemini, ...), so
  it is also recorded under ``provider:<name>`` and every seat on that provider is excluded.
  A window of an hour or more is re-probed periodically (``probe_after``): a top-up or a
  mis-read time zone then ends it early instead of costing the rest of the window.
* ``quota`` from credits ("insufficient balance", HTTP 402) — a long window (6 h) with a
  re-probe every 30 minutes; credits come back when someone pays, not on a schedule.
* ``auth`` — HTTP 401, ``invalid_grant``, "not logged in": excluded until the credential
  file that backs the seat changes (``watch``), re-probed hourly for credentials the seat
  reads from somewhere unwatched (an exported key).
* ``rate_limit`` — only when the error says how long to wait (``Retry-After``) and it is
  long enough to matter; an ordinary 429 stays admission's throttle business.

Time zones: an error timestamp with no zone is read as UTC (z.ai prints one). A wrong guess
is bounded — the re-probe above corrects an over-long window and an under-long one costs a
single failed call, after which the same classification re-records the seat.

Nothing here raises: availability bookkeeping must never break a review.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from . import seat_cooldown
from .seat_cooldown import (
    CAUSE_AUTH,
    CAUSE_QUOTA,
    CAUSE_RATE_LIMIT,
    CAUSE_UNAVAILABLE,
    CHRONIC_QUOTA_MARKERS,
    WINDOW_ACCESS_METHOD,
)

PROVIDER_KEY_PREFIX = "provider:"

# Providers whose seats all spend ONE account/key, so an exhaustion that one seat saw ends
# every seat on the provider. Left out on purpose: anthropic and openai, where a Claude CLI
# login, omp's account pool and an API key (or a codex login) are separate accounts.
PROVIDER_WIDE: frozenset[str] = frozenset(
    {
        "zai",
        "commandcode",
        "gemini",
        "xai",
        "opencode-go",
        "openrouter",
        "moonshot",
        "fireworks",
    }
)

CREDITS_WINDOW_SECONDS = 6 * 3600.0
CREDITS_REPROBE_SECONDS = 30 * 60.0
AUTH_WINDOW_SECONDS = 24 * 3600.0
AUTH_REPROBE_SECONDS = 3600.0
SUSPENDED_WINDOW_SECONDS = 6 * 3600.0
SUSPENDED_REPROBE_SECONDS = 30 * 60.0
# A reset further away than this is a mis-parse, not a window (a year in the future).
MAX_WINDOW_SECONDS = 14 * 24 * 3600.0
# A Retry-After shorter than this is a blip the in-seat retry rides out, not an exclusion.
MIN_RETRY_AFTER_EXCLUDE_SECONDS = 120.0
# Re-probe a long exclusion every quarter of its remainder, between these bounds.
_REPROBE_MIN_SECONDS = 15 * 60.0
_REPROBE_MAX_SECONDS = 6 * 3600.0
_LONG_WINDOW_SECONDS = 3600.0

_STDERR_TAIL = 2000  # bytes of stderr read for an error
_SHORT_BODY = 400  # same bound retry.py / backends use for a stdout error channel
_TIMEOUT_EXIT_CODE = (
    124  # retry._TIMEOUT_EXIT_CODE; a killed call, not an account verdict
)


@dataclass(frozen=True)
class Unavailability:
    """What one failure says about when its seat answers again."""

    cause: str
    until: (
        float | None
    )  # epoch seconds; None = the failure named none (escalating timer)
    reason: str
    source: str  # "reset-time" | "retry-after" | "credits" | "credential" | "timer"
    provider_wide: bool = False
    probe_after: float | None = None
    watch: tuple[str, ...] = field(default_factory=tuple)


def format_window(reason: str, until: float) -> str:
    """`<reason> (until <UTC time>)` — how every display shows an unavailable seat."""
    stamp = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    return f"{reason} (until {stamp})"


@dataclass(frozen=True)
class Exclusion:
    """An active exclusion of a seat, from whichever store entry ends last."""

    key: str  # the store key: the seat model, or `provider:<name>`
    scope: str  # "seat" | "provider"
    cause: str | None
    reason: str
    until: float
    source: str | None
    probe_after: float | None

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.until - time.time())

    def probe_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.probe_after is not None and self.probe_after <= now

    def describe(self) -> str:
        """`<reason> until <UTC time>` — what the board, `review health` and the skip
        sentinel show."""
        scope = "provider " if self.scope == "provider" else ""
        return format_window(f"{scope}{self.reason}", self.until)


# ---- parsing --------------------------------------------------------------------------

_ISO_RESET_RE = re.compile(
    r"(?:reset(?:s|_at)?|available again|try again|retry)\D{0,32}?"
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_EPOCH_RESET_RE = re.compile(
    r"(?:reset(?:s)?(?:_at|_time|-at)?|x-ratelimit-reset[\w-]*)[\"'\s:=]{1,6}"
    r"(\d{10}(?:\d{3})?)\b",
    re.IGNORECASE,
)
# The Claude CLI's own wording: "resets 7:30pm (Europe/Moscow)", "resets Oct 18, 5am",
# "resets Oct 18 at 5:00am". A bare clock time means the next such time.
_CLI_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*(?:,|at)?\s*)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\b"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?",
    re.IGNORECASE,
)
# The codex CLI's wording: "try again at Oct 30th, 2026 6:43 PM." — local time, no zone.
_CODEX_RESET_RE = re.compile(
    r"try again at\s+(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4})\s+(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>[ap]m)\b",
    re.IGNORECASE,
)
_MONTHS = {
    name: index + 1
    for index, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        )
    )
}
_RETRY_AFTER_MS_RE = re.compile(r"retry-after-ms\s*[:=]\s*(\d{1,15})\b", re.IGNORECASE)
_RETRY_AFTER_HEADER_RE = re.compile(
    r"retry-after\s*[:=]\s*(?P<v>\d+(?:\.\d+)?|[A-Za-z]{3},[^\n\r\"]{10,40}GMT)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}
_DURATION_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)
_RETRY_IN_RE = re.compile(
    r"(?:retry|try again|resets?|available|wait)\s*(?:after|in|for)\s+"
    r"((?:\d+(?:\.\d+)?\s*[a-z]+[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)

_CREDIT_MARKERS = (
    "insufficient credit",
    "insufficient balance",
    "insufficient funds",
    "insufficient_quota",
    "out of credits",
    "credit balance is too low",
    "credits exhausted",
    "payment required",
    "balance is not enough",
    "account balance",
    "exceeded your current quota",
)
# `401` counts only next to an HTTP/status/error word (or as the exit status); a bare
# "unauthorized" is too common in unrelated tool warnings on an agentic CLI's stderr.
_AUTH_RE = re.compile(
    r"invalid_grant|invalid[_ ]api[_ ]key|incorrect api key"
    r"|\b(?:http|status)[ :=/]{1,3}401\b|\"(?:code|status)\"\s*:\s*\"?401\b|\b401 unauthori[sz]ed"
    r"|not logged in|please run /login|oauth (?:session|token)[^.\n]{0,40}expired"
    r"|token has expired|refresh token[^.\n]{0,40}(?:invalid|expired|revoked)"
    r"|authentication[_ ](?:error|failed)|credentials? (?:are |is )?(?:invalid|expired)",
    re.IGNORECASE,
)
_SUSPENDED_RE = re.compile(
    r"account (?:is |has been )?(?:suspended|deactivated|disabled)"
    r"|temporarily (?:disabled|unavailable) for this account",
    re.IGNORECASE,
)
# Numeric provider codes count only as a JSON/bracketed `code`, never as a bare number a
# stack frame or a port could carry.
_QUOTA_RE = re.compile(
    r"weekly limit|monthly limit|usage limit|limit exhausted|quota (?:exceeded|exhausted"
    r"|reached)|hit your (?:\w+ )?limit|exceed(?:ed)? your (?:\w+ )?(?:limit|quota)"
    r"|(?:\"code\"\s*:\s*\"?|\[)(?:1310|1308)\b",
    re.IGNORECASE,
)


def _error_channel(stdout: str, stderr: str) -> str:
    """stderr plus a SHORT stdout — the channel a failed call reports its error on. A long
    stdout is a review (which may quote these very words), never an error."""
    body = (stdout or "").strip()
    # Only the tail of stderr: an agentic CLI streams its whole transcript (the prompt, the
    # diff under review) there, and the provider's own error is the last thing it prints.
    tail = (stderr or "")[-_STDERR_TAIL:]
    return tail + ("\n" + body if body and len(body) <= _SHORT_BODY else "")


def _zone(name: str | None):
    """The tzinfo for an IANA name in an error, else the local zone."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unknown zone name: fall back to local
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _sane(moment: float, now: float) -> float | None:
    if not math.isfinite(moment) or moment <= now or moment - now > MAX_WINDOW_SECONDS:
        return None
    return moment


def parse_reset_time(text: str, *, now: float) -> float | None:
    """The epoch time an error says the limit resets, else None. Understands ISO
    timestamps ("reset at 2026-10-26 05:58:34", zone-less = UTC), epoch seconds/ms
    (`x-ratelimit-reset: 1760000000`) and the Claude CLI's "resets 7:30pm (Europe/Moscow)"
    / "resets Oct 18, 5am", and codex's "try again at Oct 30th, 2026 6:43 PM" (host local
    zone). A time already past, or implausibly far away, is not a reset."""
    match = _ISO_RESET_RE.search(text)
    if match:
        year, month, day, hour, minute, second, zone = match.groups()
        try:
            moment = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
            )
        except ValueError:
            moment = None
        if moment is not None:
            tz = timezone.utc
            if zone and zone.upper() not in ("Z", "UTC", "GMT"):
                sign = 1 if zone[0] == "+" else -1
                digits = zone[1:].replace(":", "")
                tz = timezone(
                    sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
                )
            found = _sane(moment.replace(tzinfo=tz).timestamp(), now)
            if found is not None:
                return found
    match = _EPOCH_RESET_RE.search(text)
    if match:
        raw = int(match.group(1))
        found = _sane(raw / 1000.0 if len(match.group(1)) == 13 else float(raw), now)
        if found is not None:
            return found
    match = _CLI_RESET_RE.search(text)
    if match:
        return _cli_reset(match, now)
    match = _CODEX_RESET_RE.search(text)
    if match:
        return _codex_reset(match, now)
    return None


def _codex_reset(match: re.Match, now: float) -> float | None:
    month = _MONTHS.get(match["mon"][:3].lower())
    hour, minute = int(match["h"]), int(match["m"])
    if month is None or not 1 <= hour <= 12 or minute > 59:
        return None
    try:
        moment = datetime(
            int(match["year"]),
            month,
            int(match["day"]),
            hour % 12 + (12 if match["ap"].lower() == "pm" else 0),
            minute,
            tzinfo=_zone(None),
        )
    except ValueError:
        return None
    return _sane(moment.timestamp(), now)


def _cli_reset(match: re.Match, now: float) -> float | None:
    hour = int(match["h"]) % 12 + (12 if match["ap"].lower() == "pm" else 0)
    minute = int(match["m"] or 0)
    if not 0 <= minute < 60:
        return None
    tz = _zone(match["tz"])
    local_now = datetime.fromtimestamp(now, tz)
    if match["mon"]:
        month = _MONTHS.get(match["mon"][:3].lower())
        if month is None:
            return None
        try:
            moment = local_now.replace(
                month=month,
                day=int(match["day"]),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if moment.timestamp() <= now:
            moment = moment.replace(year=moment.year + 1)
    else:
        moment = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.timestamp() <= now:
            moment += timedelta(days=1)
    return _sane(moment.timestamp(), now)


def parse_retry_after(text: str, *, now: float) -> float | None:
    """Seconds an error says to wait: `retry-after-ms=N`, a `Retry-After` header (seconds
    or an HTTP date), "retry after 30 seconds", "try again in 2h 30m". None when absent."""
    match = _RETRY_AFTER_MS_RE.search(text)
    if match:
        return int(match.group(1)) / 1000.0
    match = _RETRY_AFTER_HEADER_RE.search(text)
    if match:
        value = match["v"]
        if value[0].isdigit():
            return float(value)
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            return None
    match = _RETRY_IN_RE.search(text)
    if match:
        total = 0.0
        found = False
        for amount, unit in _DURATION_PART_RE.findall(match.group(1)):
            total += float(amount) * _UNIT_SECONDS[unit.lower()]
            found = True
        return total if found else None
    return None


_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
# omp relays a provider error as `[<code>][<message>][<request id>]`
_BRACKET_MESSAGE_RE = re.compile(r"\[\d{3,5}\]\[([^\]]+)\]")


def _short(text: str, limit: int = 160) -> str:
    """The first non-blank line, reduced to the provider's own message when it is a JSON
    error body or omp's `[code][message][id]` relay."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    message = _JSON_MESSAGE_RE.search(line) or _BRACKET_MESSAGE_RE.search(line)
    return (message.group(1) if message else line)[:limit]


def classify(
    stdout: str,
    stderr: str,
    returncode: int,
    *,
    now: float | None = None,
    watch: tuple[str, ...] = (),
) -> Unavailability | None:
    """What one FAILED call says about its seat's availability, else None.

    Only an error channel is read (stderr, plus a stdout short enough to be a notice), and
    a plain success never classifies: a long review that quotes "session limit" must not
    exclude its own seat. `watch` is the credential files an auth failure is tied to."""
    now = time.time() if now is None else now
    channel = _error_channel(stdout, stderr)
    if not channel.strip():
        return None
    if returncode == 0 and len((stdout or "").strip()) > _SHORT_BODY:
        return None
    if returncode == _TIMEOUT_EXIT_CODE:
        return None  # a killed call says nothing about the account
    lowered = channel.lower()

    # Quota first: a "weekly limit exhausted" body is not an auth failure even when its
    # status line says 401/429, and an explicit reset time is the most precise answer.
    is_quota = (
        any(marker in lowered for marker in CHRONIC_QUOTA_MARKERS)
        or _QUOTA_RE.search(channel) is not None
    )
    is_credits = returncode == 402 or any(m in lowered for m in _CREDIT_MARKERS)
    if is_quota:
        reset = parse_reset_time(channel, now=now)
        if reset is not None:
            return Unavailability(
                CAUSE_QUOTA,
                reset,
                f"quota exhausted: {_short(_snippet(channel, 'reset'))}",
                "reset-time",
                provider_wide=True,
                probe_after=_reprobe_at(now, reset),
            )
        wait = parse_retry_after(channel, now=now)
        if wait is not None and wait >= MIN_RETRY_AFTER_EXCLUDE_SECONDS:
            return Unavailability(
                CAUSE_QUOTA,
                now + min(wait, MAX_WINDOW_SECONDS),
                f"quota exhausted, retry after {int(wait)}s",
                "retry-after",
                probe_after=_reprobe_at(now, now + wait),
            )
        if wait is not None:
            return None  # a short, named wait is a throttle, not an exclusion
        if not is_credits:
            return Unavailability(
                CAUSE_QUOTA, None, "session limit / usage credits", "timer"
            )
    if is_credits:
        return Unavailability(
            CAUSE_QUOTA,
            now + CREDITS_WINDOW_SECONDS,
            f"credits exhausted: {_short(channel, 100)}",
            "credits",
            provider_wide=True,
            probe_after=now + CREDITS_REPROBE_SECONDS,
        )
    if _SUSPENDED_RE.search(channel):
        # the provider turned the account/service off (Fireworks "account suspended"): it
        # names no end, so hold the seat out for a long window and re-probe it often
        return Unavailability(
            CAUSE_UNAVAILABLE,
            now + SUSPENDED_WINDOW_SECONDS,
            f"account suspended: {_short(channel, 100)}",
            "suspended",
            provider_wide=True,
            probe_after=now + SUSPENDED_REPROBE_SECONDS,
        )
    if returncode == 401 or _AUTH_RE.search(channel):
        return Unavailability(
            CAUSE_AUTH,
            now + AUTH_WINDOW_SECONDS,
            f"credentials rejected: {_short(channel, 100)}",
            "credential",
            probe_after=now + AUTH_REPROBE_SECONDS,
            watch=watch,
        )
    wait = parse_retry_after(channel, now=now)
    if wait is not None and wait >= MIN_RETRY_AFTER_EXCLUDE_SECONDS:
        reset = now + min(wait, MAX_WINDOW_SECONDS)
        return Unavailability(
            CAUSE_RATE_LIMIT,
            reset,
            f"rate limited, retry after {int(wait)}s",
            "retry-after",
            probe_after=_reprobe_at(now, reset),
        )
    return None


def _snippet(text: str, around: str) -> str:
    """The line of `text` that names `around` (the reset), for a readable reason."""
    for line in text.splitlines():
        if around in line.lower():
            return line
    return text


def _reprobe_at(now: float, until: float) -> float | None:
    """When a cheap probe may try an excluded seat again: a quarter of the way to the end of
    a long window, bounded. Short windows just expire."""
    remaining = until - now
    if remaining < _LONG_WINDOW_SECONDS:
        return None
    return now + min(_REPROBE_MAX_SECONDS, max(_REPROBE_MIN_SECONDS, remaining / 4))


# ---- store: record / read -------------------------------------------------------------


def provider_key(model: str) -> str | None:
    """`provider:<name>` for a seat on a provider-wide account, else None."""
    try:
        from .admission import provider_key as _provider_key

        name = _provider_key(model)
    except Exception:  # noqa: BLE001 — planning helper must never break a call
        return None
    return PROVIDER_KEY_PREFIX + name if name in PROVIDER_WIDE else None


def credential_files(model: str) -> tuple[str, ...]:
    """Files whose modification means the seat's login/key was replaced — the `watch` of an
    auth exclusion. Paths only; contents are never read."""
    try:
        from . import backends, claude_accounts

        backend = backends.resolve_backend(model)
        home = Path.home()
        if backend is backends.review_claude:
            found = [home / ".claude" / ".credentials.json"]
            if os.environ.get("CLAUDE_CONFIG_DIR"):
                found.append(
                    Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
                )
            root = claude_accounts.accounts_root()
            if root.is_dir():
                found.extend(sorted(root.glob("*/.credentials.json")))
            found.extend(backends._env_key_files())
            return tuple(str(p) for p in found)
        if backend is backends.review_codex:
            codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
            return (str(codex_home / "auth.json"),)
        if backend is backends.review_omp:
            return (str(backends._omp_auth_db()),)
        if backend is backends.review_opencode:
            return (str(backends._oc_auth_file()),)
        return tuple(str(p) for p in backends._env_key_files())
    except Exception:  # noqa: BLE001 — no watch list just means a timed re-probe
        return ()


def record(
    model: str, found: Unavailability, *, now: float | None = None
) -> Unavailability:
    """Persist `found` for `model` (and its provider when the exhaustion is account-wide).
    Returns `found` for chaining. Best-effort."""
    now = time.time() if now is None else now
    seat_cooldown.record_window(
        model,
        found.reason,
        until=found.until,
        cause=found.cause,
        source=found.source,
        now=now,
        watch=list(found.watch),
        probe_after=found.probe_after,
    )
    if found.provider_wide and found.source in ("reset-time", "credits", "suspended"):
        key = provider_key(model)
        if key is not None:
            seat_cooldown.record_window(
                key,
                found.reason,
                until=found.until,
                cause=found.cause,
                source=found.source,
                now=now,
                probe_after=found.probe_after,
            )
    return found


def observe(
    model: str,
    result,
    *,
    usable: bool,
    now: float | None = None,
) -> Unavailability | None:
    """Learn from one finished call: record a classified failure, or — when the call
    produced a usable answer — drop the window entries the success disproves. `usable` is
    the caller's verdict (`panel.result_is_usable`). Never raises."""
    try:
        now = time.time() if now is None else now
        if usable:
            _clear(model)
            return None
        found = classify(
            result.stdout,
            result.stderr,
            result.returncode,
            now=now,
            watch=credential_files(model),
        )
        if found is None:
            return None
        return record(model, found, now=now)
    except Exception:  # noqa: BLE001 — bookkeeping must never break a review
        return None


def _clear(model: str) -> None:
    if seat_cooldown.active_cooldown(model, access_method=WINDOW_ACCESS_METHOD):
        seat_cooldown.clear_cooldown(model, access_method=WINDOW_ACCESS_METHOD)
    key = provider_key(model)
    if key is not None and seat_cooldown.active_cooldown(
        key, access_method=WINDOW_ACCESS_METHOD
    ):
        seat_cooldown.clear_cooldown(key, access_method=WINDOW_ACCESS_METHOD)


def _from_view(key: str, scope: str, view: dict) -> Exclusion:
    return Exclusion(
        key=key,
        scope=scope,
        cause=view["cause"],
        reason=view["reason"],
        until=view["until"],
        source=view["source"],
        probe_after=view["probe_after"],
    )


def exclusion(
    model: str,
    *,
    methods: tuple[str, ...] = (),
    now: float | None = None,
    legacy_check: Callable[..., dict | None] | None = None,
) -> Exclusion | None:
    """The exclusion keeping `model` from running now, or None. Looks at the seat's own
    window entry, its provider's, and the older escalating-timer entries under `methods`
    (the transports the seat can run on; `legacy_check` lets the backend run its
    usage-recovery probe first). The entry that ends last wins, since the seat is excluded
    until the last one lapses."""
    now = time.time() if now is None else now
    check = legacy_check or seat_cooldown.active_cooldown
    found: list[Exclusion] = []
    view = seat_cooldown.active_cooldown(
        model, access_method=WINDOW_ACCESS_METHOD, now=now
    )
    if view is not None:
        found.append(_from_view(model, "seat", view))
    pkey = provider_key(model)
    if pkey is not None:
        view = seat_cooldown.active_cooldown(
            pkey, access_method=WINDOW_ACCESS_METHOD, now=now
        )
        if view is not None:
            found.append(_from_view(pkey, "provider", view))
    for method in methods:
        view = check(model, access_method=method)
        if view is not None:
            found.append(
                Exclusion(
                    key=model,
                    scope="seat",
                    cause=view.get("cause"),
                    reason=view["reason"],
                    until=view["until"],
                    source=None,
                    probe_after=None,
                )
            )
    if not found:
        return None
    last = max(found, key=lambda e: e.until)
    # a re-probe may be due on any of the entries, whichever one ends last
    probes = [e.probe_after for e in found if e.probe_after is not None]
    if probes and last.probe_after is None:
        last = dataclasses.replace(last, probe_after=min(probes))
    return last


def provider_exclusions(*, now: float | None = None) -> dict[str, Exclusion]:
    """Active provider-wide exclusions by provider name — what `review jobs` shows in place
    of "THROTTLED"."""
    out: dict[str, Exclusion] = {}
    for key, method, view in seat_cooldown.iter_active(now=now):
        if method == WINDOW_ACCESS_METHOD and key.startswith(PROVIDER_KEY_PREFIX):
            out[key[len(PROVIDER_KEY_PREFIX) :]] = _from_view(key, "provider", view)
    return out
