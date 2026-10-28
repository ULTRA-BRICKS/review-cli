"""Which account a seat would use, and exactly why an account is refused (review-cli#707).

Pure policy, no I/O: callers pass each account's usage windows, the configured cap, the
name of the config key that sets it and "now". A verdict says one of five things and its
`reason` is the sentence `review seats` prints:

  * `ok`                -- every shared window is below the cap;
  * `over_cap`          -- review-cli's OWN configured ceiling holds the account
                           ("ограничение review-cli (config KEY=80): аккаунт X использован на
                           83% окна 5ч, сброс в HH:MM"); never worded as a provider limit;
  * `provider_limited`  -- the PROVIDER (a 429, an omp block) holds it; says so and that it is
                           not review-cli's rule;
  * `usage_unknown`     -- no usage reading, or the 5-hour/7-day pair is incomplete;
  * `usage_stale`       -- the reading is older than allowed or a window already reset.
The last two are refused conservatively and say so: an unconfirmable account is never
assumed to be below the cap.

`pick` ranks the accounts that are `ok` by their highest shared window (lowest first, ties
keep the caller's order) so spend spreads to the emptiest account and rotates as readings
move, and annotates every refused verdict with the account that is available instead.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, tzinfo

OK = "ok"
OVER_CAP = "over_cap"
PROVIDER_LIMITED = "provider_limited"
USAGE_UNKNOWN = "usage_unknown"
USAGE_STALE = "usage_stale"

_WINDOW_TEXT = {"5h": "окна 5ч", "7d": "окна 7д"}
_SHARED_PAIR = ("5h", "7d")


@dataclass(frozen=True)
class Window:
    """One usage window of one account, `used_pct` in 0..100. `key` is "5h" or "7d" for
    the windows every model shares; a model tier's own window uses any other key and never
    completes the shared pair."""

    key: str
    used_pct: float
    resets_at: float | None = None


@dataclass(frozen=True)
class AccountReading:
    """What is known about one account. `windows` is None when usage could not be read;
    `read_at` (epoch seconds) is None when the reading's age is unknown. `name` is the
    account's display name (an email or label), never a credential."""

    name: str
    windows: tuple[Window, ...] | None
    read_at: float | None = None
    provider_block_until: float | None = None
    provider_block_reason: str | None = None
    cap_percent: float | None = None
    cap_key: str | None = None


@dataclass(frozen=True)
class AccountVerdict:
    name: str
    state: str
    five_hour_pct: float | None
    seven_day_pct: float | None
    cap_pct: float | None
    reset_at: float | None
    reason: str
    cap_key: str = ""
    worst_pct: float | None = None

    @property
    def usable(self) -> bool:
        return self.state == OK

    @property
    def peak_pct(self) -> float:
        """The highest window that counts for ranking: the shared pair and any model-tier
        window the seat's model uses (`worst_pct`, set once the windows were judged)."""
        if self.worst_pct is not None:
            return self.worst_pct
        values = [v for v in (self.five_hour_pct, self.seven_day_pct) if v is not None]
        return max(values) if values else 0.0


@dataclass(frozen=True)
class Selection:
    chosen: AccountVerdict | None
    verdicts: tuple[AccountVerdict, ...]
    summary: str


def percent_text(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def clock_text(epoch: float, tz: tzinfo | None = None) -> str:
    return datetime.fromtimestamp(epoch, tz).strftime("%H:%M")


def _shared(reading: AccountReading, key: str) -> Window | None:
    for window in reading.windows or ():
        if window.key == key:
            return window
    return None


def _verdict(
    reading: AccountReading,
    state: str,
    reason: str,
    cap_percent: float | None,
    reset_at: float | None = None,
) -> AccountVerdict:
    five, seven = _shared(reading, "5h"), _shared(reading, "7d")
    return AccountVerdict(
        name=reading.name,
        state=state,
        five_hour_pct=None if five is None else five.used_pct,
        seven_day_pct=None if seven is None else seven.used_pct,
        cap_pct=cap_percent,
        reset_at=reset_at
        if reset_at is not None
        else (None if five is None else five.resets_at),
        reason=reason,
    )


def _provider_limited(
    reading: AccountReading, cap_percent: float | None, now: float, tz: tzinfo | None
) -> AccountVerdict | None:
    until = reading.provider_block_until
    if until is None or until <= now:
        return None
    why = reading.provider_block_reason or "лимит провайдера"
    return _verdict(
        reading,
        PROVIDER_LIMITED,
        f"лимит провайдера ({why}) до {clock_text(until, tz)}: аккаунт "
        f"{reading.name} отклонён самим провайдером, это не ограничение review-cli",
        cap_percent,
        reset_at=until,
    )


def _unconfirmed(
    reading: AccountReading,
    cap_percent: float | None,
    now: float,
    max_age_seconds: float | None,
) -> tuple[str, str] | None:
    """(state, reason) when the shared 5-hour/7-day pair cannot be trusted, else None."""
    pair = [_shared(reading, key) for key in _SHARED_PAIR]
    if reading.windows is None or any(window is None for window in pair):
        return (
            USAGE_UNKNOWN,
            f"usage неизвестен для аккаунта {reading.name} (нет окна 5ч и 7д); "
            "консервативно не используется",
        )
    if any(window.resets_at is not None and window.resets_at <= now for window in pair):
        return (
            USAGE_STALE,
            f"usage аккаунта {reading.name} устарел (окно уже сброшено, показания "
            "не обновлены); консервативно не используется",
        )
    if max_age_seconds is not None and (
        reading.read_at is None or now - reading.read_at > max_age_seconds
    ):
        return (
            USAGE_STALE,
            f"usage аккаунта {reading.name} устарел (старше {max_age_seconds:g} с или "
            "возраст неизвестен); консервативно не используется",
        )
    return None


def judge(
    reading: AccountReading,
    *,
    cap_percent: float | None,
    cap_key: str,
    now: float,
    tz: tzinfo | None = None,
    max_age_seconds: float | None = None,
) -> AccountVerdict:
    """The verdict for one account. `cap_percent` None means no ceiling is configured:
    usage never holds the account (only a provider limit does)."""
    if reading.cap_key is not None:
        cap_percent, cap_key = reading.cap_percent, reading.cap_key
    limited = _provider_limited(reading, cap_percent, now, tz)
    if limited is not None:
        return limited
    if cap_percent is None:
        return _verdict(reading, OK, "", cap_percent)
    unconfirmed = _unconfirmed(reading, cap_percent, now, max_age_seconds)
    if unconfirmed is not None:
        return _verdict(reading, unconfirmed[0], unconfirmed[1], cap_percent)
    counted = [
        w
        for w in reading.windows or ()
        if w.key not in _SHARED_PAIR and (w.resets_at is None or w.resets_at > now)
    ]
    counted += [_shared(reading, key) for key in _SHARED_PAIR]
    worst = max(counted, key=lambda w: w.used_pct)
    if worst.used_pct >= cap_percent:
        text = _WINDOW_TEXT.get(worst.key, f"окна {worst.key}")
        reset = (
            f", сброс в {clock_text(worst.resets_at, tz)}" if worst.resets_at else ""
        )
        return replace(
            _verdict(
                reading,
                OVER_CAP,
                f"ограничение review-cli (config {cap_key}={percent_text(cap_percent)}): "
                f"аккаунт {reading.name} использован на {percent_text(worst.used_pct)}% "
                f"{text}{reset}",
                cap_percent,
                reset_at=worst.resets_at,
            ),
            cap_key=cap_key,
        )
    return replace(_verdict(reading, OK, "", cap_percent), worst_pct=worst.used_pct)


def _summary(verdicts: list[AccountVerdict]) -> str:
    if not verdicts:
        return "нет аккаунтов: список аккаунтов пуст"
    if all(v.state == OVER_CAP for v in verdicts):
        rules = sorted(
            {f"{v.cap_key}={percent_text(v.cap_pct or 0)}" for v in verdicts}
        )
        return (
            "все аккаунты выше порога ограничения review-cli "
            f"(config {', '.join(rules)})"
        )
    return "нет подходящего аккаунта: " + "; ".join(v.reason for v in verdicts)


def pick(
    readings: list[AccountReading],
    *,
    cap_percent: float | None,
    cap_key: str,
    now: float,
    tz: tzinfo | None = None,
    max_age_seconds: float | None = None,
) -> Selection:
    """Judge every account and choose the `ok` one with the lowest highest-window usage.
    Refused verdicts name the chosen account as the alternative."""
    judged = [
        judge(
            r,
            cap_percent=cap_percent,
            cap_key=cap_key,
            now=now,
            tz=tz,
            max_age_seconds=max_age_seconds,
        )
        for r in readings
    ]
    usable = [v for v in judged if v.usable]
    if not usable:
        return Selection(None, tuple(judged), _summary(judged))
    chosen = min(
        usable, key=lambda v: v.peak_pct
    )  # min is stable: ties keep input order
    annotated = tuple(
        v
        if v.usable
        else replace(v, reason=f"{v.reason}; другой аккаунт доступен: {chosen.name}")
        for v in judged
    )
    return Selection(
        next(v for v in annotated if v.name == chosen.name),
        annotated,
        f"выбран аккаунт {chosen.name} (наименьшее использование {percent_text(chosen.peak_pct)}%)",
    )
