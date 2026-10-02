"""The shared contract behind review-cli's two heavy-seat concurrency caps.

`process.max_concurrency()` (`$REVIEW_MAX_CONCURRENCY`, per process) and
`machine_slots.max_machine_concurrency()` (`$REVIEW_MACHINE_MAX_CONCURRENCY`, machine-wide)
read their cap the same way and announce a queued seat the same way; this module is the
single source for both so the two caps cannot silently drift apart (review findings on
review-cli#592):

  * `capped_env_int` — one env reader, ONE ceiling: a missing/blank/non-integer value
    falls back to the default, `<= 0` disables the cap (returned as 0), anything above
    `CAP_CEILING` clamps down so a typo'd env can't pin an absurd number of model children.
  * `waiting_marker` / `WAITING_MARKER_RE` — the pre-header line `process._run_streamed`
    writes to a seat's per-call log while it waits on either cap, and the regex the
    dashboard parser uses to recognise it. Both are built from the same phrase pieces: a
    producer that changes the wording changes the parser with it, instead of silently
    turning the marker into body text and losing the header's argv0/task attribution.
    (Historical logs on disk carry the per-process form; the regex must keep matching it.)
"""

from __future__ import annotations

import os
import re

CAP_CEILING = 64

_PHRASE_BEFORE_SCOPE = "waiting for a "
_SCOPE_MACHINE_WIDE = "machine-wide "
_PHRASE_AFTER_SCOPE = "concurrency slot (cap "
_PHRASE_END = ")"

WAITING_MARKER_RE = re.compile(
    r"^\[review-cli\] .+?: "
    + re.escape(_PHRASE_BEFORE_SCOPE)
    + "(?:"
    + re.escape(_SCOPE_MACHINE_WIDE)
    + ")?"
    + re.escape(_PHRASE_AFTER_SCOPE)
    + r"\d+"
    + re.escape(_PHRASE_END)
    + r"\s*$"
)


def capped_env_int(name: str, *, default: int) -> int:
    """Read the cap `name` from the environment; see the module docstring for the
    fallback/disable/clamp contract. `default` must be > 0: 0 is the "disabled" return, so a
    zero default would be indistinguishable from an explicit `<= 0`."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return 0
    return min(value, CAP_CEILING)


def waiting_marker(backend: str, cap: int, *, machine_wide: bool) -> str:
    """The queue-marker log line (with trailing newline) for a seat parked on a cap."""
    scope = _SCOPE_MACHINE_WIDE if machine_wide else ""
    return (
        f"[review-cli] {backend}: {_PHRASE_BEFORE_SCOPE}{scope}"
        f"{_PHRASE_AFTER_SCOPE}{cap}{_PHRASE_END}\n"
    )
