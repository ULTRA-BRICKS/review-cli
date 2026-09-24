#!/usr/bin/env python3
"""Guardrails for README.md's own internal structure (review-cli#552 PR5).

1. Every same-file `](#...)` anchor link inside README.md must resolve to a real
   heading in the same file, via GitHub's heading-slug algorithm: lowercase; drop
   every character outside [\\w space hyphen] (Unicode-aware, so accented/non-Latin
   letters and underscores survive like GitHub's real slugger); turn each remaining
   space into a literal `-`, NOT collapsed, so a punctuation run flanked by spaces on
   both sides yields a DOUBLE hyphen (e.g. "Subcommands & flags" ->
   "subcommands--flags"). This is the exact class of bug review-cli#551 fixed (a
   `[Flags](#flags)` link pointing at a heading whose real slug was
   `#subcommands--flags`). The algorithm is ground-truthed below against the three
   anchors #551's own fix commit (1745e45) is known to use.

2. A byte-size ratchet on README.md so it cannot silently re-bloat toward its
   pre-#552 ~141KB/~2001-line size. See SIZE_CEILING_BYTES below for the exact
   number and why it isn't the ticket's aspirational ~30KB.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

# Bytes, not lines: bytes are what actually determines how many tokens an agent
# burns loading the whole file (the ticket's own cost argument), so they're the more
# direct ratchet target than a line count that can hide bloat in long paragraphs.
# NOT the ticket brainstorm's aspirational ~30KB: PR5's scope (condensing Auth,
# install-state prose, Agent workflows; leaving the help-docs-sync'd flags cheat
# sheet alone) lands README around ~90KB, still carrying a full onboarding guide, a
# mode-by-mode reference, and every safety-critical callout verbatim (PR5 exempts
# those from cutting). This ceiling gives real headroom above that actual size while
# staying strictly below the pre-PR5 merged 99856-byte baseline, so it still traps a
# future regression without failing on this PR's own diff. A future safety-critical
# paragraph that needs more room is a deliberate human call: bump this constant.
SIZE_CEILING_BYTES = 97_000

_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_ANCHOR_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")


def _gfm_slug(heading_text: str) -> str:
    """GitHub-Flavored-Markdown heading slug: lowercase, drop every character
    outside [\\w space hyphen] (Unicode word chars, so underscores and non-ASCII
    letters survive, matching GitHub's real slugger and reviewlib/specweb/render.py's
    own approximation of it), then replace each remaining space with a literal '-'
    one-for-one (consecutive spaces -> consecutive hyphens, never collapsed)."""
    slug = heading_text.lower()
    slug = re.sub(r"[^\w -]", "", slug, flags=re.UNICODE)
    return slug.replace(" ", "-")


def _iter_non_fenced_lines(text: str):
    """Yield (lineno, line) pairs, skipping fenced code blocks (``` or ~~~, any
    length >= 3): bash `# comment` lines and literal `](#...)` samples inside a fence
    must never be mistaken for real ATX headings or live anchor links. A fence only
    closes on a matching-or-longer run of the SAME character it opened with (a
    4-backtick fence wrapping a 3-backtick example does not close early)."""
    fence_char: str | None = None
    fence_len = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _FENCE_OPEN_RE.match(line)
        if m:
            marker = m.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
                continue
            if marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
                continue
        if fence_char is not None:
            continue
        yield lineno, line


def _heading_slugs(text: str) -> set[str]:
    """Every ATX heading's slug, with GitHub's duplicate-slug disambiguation
    applied (first occurrence bare, repeats get -1, -2, ...)."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for _lineno, line in _iter_non_fenced_lines(text):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base = _gfm_slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.add(base if n == 0 else f"{base}-{n}")
    return slugs


@lru_cache(maxsize=1)
def _readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_gfm_slug_matches_known_real_anchors_from_the_551_fix():
    """Ground-truth the slug algorithm itself against the exact anchors review-cli's
    own #551 fix commit (1745e45) is known to use, before trusting it to judge the
    rest of the file."""
    assert _gfm_slug("Subcommands & flags") == "subcommands--flags"
    assert (
        _gfm_slug("QA — agent-as-tester (`review qa`)")
        == "qa--agent-as-tester-review-qa"
    )
    assert _gfm_slug("Diff review (`review diff`)") == "diff-review-review-diff"


def test_every_same_file_anchor_link_resolves_to_a_real_heading():
    """Catches exactly the review-cli#551 class of bug: a `](#dead-anchor)` link
    inside README.md that does not match any real heading's GFM slug."""
    text = _readme_text()
    real_slugs = _heading_slugs(text)
    assert real_slugs, "no ATX headings parsed out of README.md -- parser is broken"

    dead: list[tuple[int, str]] = []
    for lineno, line in _iter_non_fenced_lines(text):
        for match in _ANCHOR_LINK_RE.finditer(line):
            anchor = match.group(1)
            if anchor not in real_slugs:
                dead.append((lineno, anchor))

    assert not dead, (
        "README.md links to same-file anchor(s) that don't match any real heading "
        f"slug (the review-cli#551 class of bug): {dead}"
    )


def test_known_stable_anchors_are_present():
    """Pins the inbound anchors ticket #552 named as load-bearing: at minimum
    #how-review-compares, #quick-start, #subcommands--flags. Each required slug is
    computed from the file's OWN current heading text (copied below) run through the
    same `_gfm_slug` the ground-truth test above already verified against real
    GitHub anchors -- never a hand-computed/guessed slug string -- so a heading
    reword that keeps the same wording here still passes, and the guard is against
    slug-computation drift, not against intentionally updating this test's copy of
    the heading text on a deliberate rename."""
    slugs = _heading_slugs(_readme_text())

    assert _gfm_slug("How review compares") in slugs
    assert _gfm_slug("Quick start") in slugs
    assert _gfm_slug("Subcommands & flags") in slugs

    # The other two anchors identified during the #552 brainstorm as load-bearing:
    # the ralphex/revmux comparison subsections.
    ralphex_heading = "review vs ralphex — the whole loop vs the review primitive"
    revmux_heading = (
        "review vs revmux — panel + decision tool vs supervised multi-agent process"
    )
    assert _gfm_slug(ralphex_heading) in slugs, (
        "expected review-vs-ralphex heading text has drifted from the brainstorm's "
        f"tracked anchor -- update this test's ground-truth string: {ralphex_heading!r}"
    )
    assert _gfm_slug(revmux_heading) in slugs, (
        "expected review-vs-revmux heading text has drifted from the brainstorm's "
        f"tracked anchor -- update this test's ground-truth string: {revmux_heading!r}"
    )


def test_readme_size_ratchet():
    """README.md must not silently re-bloat. See SIZE_CEILING_BYTES above for the
    ceiling rationale. It's a deliberate constant to bump by hand when a genuinely
    safety-critical addition needs the room, not something this test should widen on
    its own."""
    size = README_PATH.stat().st_size
    assert size < SIZE_CEILING_BYTES, (
        f"README.md is {size} bytes, at or over the {SIZE_CEILING_BYTES}-byte "
        "ratchet ceiling (review-cli#552 PR5). If a new paragraph is genuinely "
        "safety-critical and must grow the file past this, bump "
        "SIZE_CEILING_BYTES in tests/test_readme_anchors.py by hand -- that's a "
        "deliberate human call, not something this test should silently allow."
    )


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok      {name}")
            except Exception as exc:  # noqa: BLE001 - surface any failure, not just AssertionError
                failures += 1
                print(f"FAILED  {name}: {exc}")
    sys.exit(1 if failures else 0)
