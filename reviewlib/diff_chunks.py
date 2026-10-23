"""Split an oversized unified diff into per-pass chunks — never truncate it.

A diff over the per-pass budget (`$REVIEW_DIFF_MAX_BYTES`, default 300000 bytes) used
to be cut off with a marker, so no seat ever saw the tail of a big change. It is now
SPLIT: the board reviews the diff in several passes and every byte of the original
lands in exactly one pass.

`split_diff` is pure (only `diff_budget_bytes` reads the env):

  * the diff is cut at `diff --git` file boundaries and whole files are packed greedily,
    in diff order, into chunks of at most `budget_bytes`;
  * a single file larger than the budget is cut at hunk (`@@`) boundaries; each piece
    repeats the file header and carries a visible `[chunk i/n of <path>]` note;
  * only when ONE hunk (or one header-only section) cannot fit a chunk is the diff
    unsplittable, and `DiffUnsplittableError` names the file, the sizes and the budget.

Nothing here ever drops bytes: for every file, the pieces' bodies concatenate back to
the original hunk text (see `tests/test_diff_chunking.py`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DIFF_MAX_BYTES_DEFAULT = 300_000
# Exit code of a review refused BEFORE any seat ran because its diff cannot be reviewed
# whole: `review diff` when one hunk exceeds the budget (DiffUnsplittableError), and the
# single-prompt modes (brainstorm/quorum/just-ask) when the diff is over budget
# (DiffTooLargeError).
EXIT_DIFF_TOO_LARGE = 13

_FILE_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@", re.MULTILINE)
_HEADER_PATHS_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
# Pseudo-path for text before the first `diff --git` line (a `git show` preamble, a
# non-git diff): it is packed like a file so no byte is ever dropped.
_PREAMBLE_PATH = "<preamble>"


class DiffUnsplittableError(ValueError):
    """One hunk (or one header-only section) is larger than the per-pass budget, so the
    diff cannot be split into passes without cutting inside a hunk. The message names the
    file, the sizes and the way out; callers print it and exit non-zero."""

    def __init__(
        self, path: str, what: str, needed: int, budget: int, detail: str = ""
    ) -> None:
        self.path = path
        self.what = what
        self.needed = needed
        self.budget = budget
        super().__init__(
            f"cannot split the diff: {what} needs {needed} bytes"
            f"{detail} but the per-pass budget is {budget} bytes "
            "($REVIEW_DIFF_MAX_BYTES) — a hunk is never cut. Raise "
            "$REVIEW_DIFF_MAX_BYTES above that size (or set it to 0 to send the whole "
            "diff in one pass), or shrink/exclude that file from the review."
        )


class DiffTooLargeError(ValueError):
    """A single-prompt mode (brainstorm / quorum / just-ask) was handed a diff over the
    per-prompt budget. Those modes embed the diff in ONE prompt, so there is no pass to
    spread it over — and a diff is never silently cut."""

    def __init__(self, mode: str, size: int, files: int, budget: int) -> None:
        self.mode = mode
        self.size = size
        self.budget = budget
        super().__init__(
            f"`review {mode}` puts the diff into a single prompt, but the diff is "
            f"{size} bytes across {files} file(s) and the per-prompt budget is {budget} "
            "bytes ($REVIEW_DIFF_MAX_BYTES) — review-cli never truncates a diff silently. "
            f"Pipe a scoped diff in (`git diff -- <path> | review {mode} ...`; a piped "
            "diff is sent whole), raise $REVIEW_DIFF_MAX_BYTES (0 sends the whole diff), "
            "or use `review diff`, which reviews a large diff in several passes."
        )


def require_single_prompt_diff(diff: str, mode: str) -> None:
    """Raise `DiffTooLargeError` when `diff` is over the budget; a no-op otherwise (a
    budget <= 0 disables the check). For the modes that cannot split a diff over passes."""
    budget = diff_budget_bytes()
    size = _size(diff)
    if budget > 0 and size > budget:
        raise DiffTooLargeError(mode, size, len(_FILE_HEADER_RE.findall(diff)), budget)


def diff_budget_bytes() -> int:
    """Max bytes of diff per pass, read at CALL time so an env override applies. A
    missing/blank/non-integer `$REVIEW_DIFF_MAX_BYTES` falls back to the default; a value
    <= 0 means "send the whole diff in one pass, never split"."""
    raw = os.environ.get("REVIEW_DIFF_MAX_BYTES")
    if raw is None or not raw.strip():
        return DIFF_MAX_BYTES_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return DIFF_MAX_BYTES_DEFAULT


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class DiffPiece:
    """One packable unit: a whole file's diff, or a hunk-range piece of an oversized one.

    `text` is exactly what is sent. For a whole file `text == header + body`, the original
    section byte-for-byte; for one piece of a split file `text == header + note + body`
    where `note` is the visible `[chunk i/n of <path>]` line."""

    path: str
    header: str
    body: str
    text: str
    size: int
    index: int = 1
    total: int = 1

    @property
    def label(self) -> str:
        """How the part note lists this piece: the path, plus `i/n` for a split file."""
        if self.total == 1:
            return self.path
        return f"{self.path} [chunk {self.index}/{self.total}]"


@dataclass(frozen=True)
class DiffChunk:
    """The diff text of ONE pass plus what the seat is told about it."""

    index: int
    total: int
    text: str
    pieces: tuple[DiffPiece, ...]

    @property
    def size(self) -> int:
        return _size(self.text)

    @property
    def files(self) -> tuple[str, ...]:
        """Distinct file paths in this chunk, in diff order."""
        return tuple(dict.fromkeys(piece.path for piece in self.pieces))

    def prompt_note(self) -> str:
        """The paragraph appended to the review prompt of a MULTI-pass run, so a seat
        reviews this part on its own terms instead of flagging the files it cannot see."""
        listing = ", ".join(piece.label for piece in self.pieces)
        others = self.total - 1
        return (
            f"Part {self.index} of {self.total} of the change. This pass's diff holds "
            f"ONLY these files: {listing}. The other {others} part"
            f"{'s' if others != 1 else ''} of the change "
            f"{'are' if others != 1 else 'is'} reviewed in separate passes — do not flag "
            "a file, test or caller as missing just because it is not in this part, and "
            "do not assume it is unchanged."
        )


def _section_path(section: str) -> str:
    first_line = section.split("\n", 1)[0]
    match = _HEADER_PATHS_RE.match(first_line)
    if match:
        return match.group(2)
    return first_line[len("diff --git ") :].strip() or _PREAMBLE_PATH


def _sections(diff: str) -> list[tuple[str, str]]:
    """`(path, text)` per file section; the texts concatenate back to `diff` exactly."""
    starts = [m.start() for m in _FILE_HEADER_RE.finditer(diff)]
    if not starts:
        return [(_PREAMBLE_PATH, diff)] if diff else []
    sections: list[tuple[str, str]] = []
    if starts[0] > 0:
        sections.append((_PREAMBLE_PATH, diff[: starts[0]]))
    bounds = starts + [len(diff)]
    for begin, end in zip(bounds, bounds[1:]):
        text = diff[begin:end]
        sections.append((_section_path(text), text))
    return sections


def _split_hunks(section: str) -> tuple[str, list[str]]:
    """`(header, hunks)`: everything before the first `@@` line, then each hunk."""
    starts = [m.start() for m in _HUNK_RE.finditer(section)]
    if not starts:
        return section, []
    bounds = starts + [len(section)]
    return section[: starts[0]], [
        section[begin:end] for begin, end in zip(bounds, bounds[1:])
    ]


def _whole_file_piece(path: str, text: str) -> DiffPiece:
    header, hunks = _split_hunks(text)
    return DiffPiece(path, header, "".join(hunks), text, _size(text))


def _note(
    *, path: str, index: int, total: int, first: int, last: int, hunks: int
) -> str:
    return (
        f"[chunk {index}/{total} of {path}] hunks {first}-{last} of {hunks} "
        "(file header repeated in every chunk)\n"
    )


def _split_oversized_file(path: str, section: str, budget: int) -> list[DiffPiece]:
    header, hunks = _split_hunks(section)
    if not hunks:
        raise DiffUnsplittableError(
            path,
            f"{path} (a diff section with no hunks to split at)",
            _size(section),
            budget,
        )
    # Worst-case note width (every number as wide as the hunk count) so the final
    # `i/n` can never push a piece over the budget.
    count = len(hunks)
    worst_note = _size(
        _note(path=path, index=count, total=count, first=count, last=count, hunks=count)
    )
    room = budget - _size(header) - worst_note
    groups: list[list[int]] = []
    used = 0
    for number, hunk in enumerate(hunks, start=1):
        need = _size(hunk)
        if need > room:
            overhead = _size(header) + worst_note
            raise DiffUnsplittableError(
                path,
                f"hunk {number} of {len(hunks)} in {path}",
                need + overhead,
                budget,
                f" (the hunk is {need} bytes, plus {overhead} bytes of file header and "
                "chunk note repeated in every piece)",
            )
        if not groups or used + need > room:
            groups.append([])
            used = 0
        groups[-1].append(number)
        used += need
    pieces: list[DiffPiece] = []
    for index, numbers in enumerate(groups, start=1):
        body = "".join(hunks[n - 1] for n in numbers)
        text = (
            header
            + _note(
                path=path,
                index=index,
                total=len(groups),
                first=numbers[0],
                last=numbers[-1],
                hunks=len(hunks),
            )
            + body
        )
        pieces.append(
            DiffPiece(path, header, body, text, _size(text), index, len(groups))
        )
    return pieces


def split_diff(diff: str, budget_bytes: int) -> list[DiffChunk]:
    """Split `diff` into chunks of at most `budget_bytes` bytes each (see module docs).

    `budget_bytes <= 0`, or a diff already within the budget, yields ONE chunk holding the
    diff unchanged. Raises `DiffUnsplittableError` when a single hunk cannot fit."""
    sections = _sections(diff)
    if budget_bytes <= 0 or _size(diff) <= budget_bytes:
        pieces = tuple(_whole_file_piece(path, text) for path, text in sections)
        return [DiffChunk(1, 1, diff, pieces)]
    atoms: list[DiffPiece] = []
    for path, text in sections:
        size = _size(text)
        if size <= budget_bytes:
            atoms.append(_whole_file_piece(path, text))
        else:
            atoms.extend(_split_oversized_file(path, text, budget_bytes))
    packed: list[list[DiffPiece]] = []
    used = 0
    for atom in atoms:
        if not packed or used + atom.size > budget_bytes:
            packed.append([])
            used = 0
        packed[-1].append(atom)
        used += atom.size
    total = len(packed)
    return [
        DiffChunk(index, total, "".join(piece.text for piece in group), tuple(group))
        for index, group in enumerate(packed, start=1)
    ]
