"""Resolve review-cli's version from the SINGLE source of truth (pyproject `[project] version`).

What `review --version` prints (review-cli#396 — every other ecosystem CLI has one; agents and
rig's freshness checks expect it). The version must never be a hardcoded literal that drifts
from the packaged metadata: a stale `--version` is a useless freshness signal.

Two resolution paths, in order, so it works both installed and from a live checkout:

1. **Live checkout** — when `pyproject.toml` is readable at the repo root (parent of this
   `reviewlib/` package), parse `[project] version` directly from it. This is the ALWAYS-FRESH
   path for a source checkout, including the `install.sh` symlink install and an editable
   `pip install -e .` (whose in-tree egg-info lags behind a pyproject bump, and
   `importlib.metadata` would read THAT stale copy).
2. **Installed dist** — when no readable `pyproject.toml` is found (a pip/pipx wheel install
   outside any checkout), fall back to `importlib.metadata.version("hyper-review")`.

Stdlib-only. Deliberately no `tomllib` (absent on Python 3.9/3.10, still in the supported
matrix); a targeted regex over the `[project]` table is enough for one scalar key.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

# The distribution name declared in pyproject `[project] name` — importlib.metadata keys on
# this, NOT on the import package (`reviewlib`) or the script name (`review`).
_DIST_NAME = "hyper-review"

# Last-resort sentinel: only surfaces if review is neither installed NOR runnable from a
# checkout that carries a parseable pyproject — a genuinely broken deployment.
_UNKNOWN = "0.0.0+unknown"

# TOML allows leading whitespace before a header/key and a trailing `# comment` after a
# header, so every pattern tolerates both — a valid-but-unusual layout must not silently
# push `--version` onto the stale installed-metadata path.
_PROJECT_TABLE_RE = re.compile(r"^[ \t]*\[project\][ \t]*(#.*)?$", re.MULTILINE)
# A real TOML table header (`[t]`, or an array-of-tables `[[t]]`) starts with `[`/`[[` and a name char — NOT any line starting
# with `[`, which would end `[project]` early on a multi-line array continuation line.
_NEXT_TABLE_RE = re.compile(r"^[ \t]*\[{1,2}[ \t]*[A-Za-z]", re.MULTILINE)
_VERSION_RE = re.compile(r"""^[ \t]*version[ \t]*=[ \t]*['"]([^'"]+)['"]""", re.MULTILINE)
# Only trust a pyproject whose `[project] name` matches the dist name, so an adjacent
# host-project pyproject (e.g. `pip install --target ./vendor`) is never mistaken for ours.
_NAME_RE = re.compile(r"""^[ \t]*name[ \t]*=[ \t]*['"]([^'"]+)['"]""", re.MULTILINE)


def parse_project_version(text: str, *, require_name: str | None = None) -> str | None:
    """Extract `[project] version` from pyproject TOML text, or None if absent/mismatched.

    Scopes the `version =` / `name =` matches to the `[project]` table body (header to the
    next real table header) so a key in another table can never be picked up."""
    # CRLF checkouts (Windows `core.autocrlf`): `$` in MULTILINE matches only before `\n`.
    text = text.replace("\r\n", "\n")
    start = _PROJECT_TABLE_RE.search(text)
    if start is None:
        return None
    body_start = start.end()
    next_table = _NEXT_TABLE_RE.search(text, body_start)
    body = text[body_start : next_table.start() if next_table else len(text)]
    if require_name is not None:
        name_match = _NAME_RE.search(body)
        if name_match is None or name_match.group(1) != require_name:
            return None
    match = _VERSION_RE.search(body)
    return match.group(1) if match else None


def _version_from_pyproject() -> str | None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_project_version(text, require_name=_DIST_NAME)


def resolve_version() -> str:
    """review-cli's version: live checkout pyproject first, then installed dist metadata.
    Never raises; returns the `0.0.0+unknown` sentinel only when neither is readable."""
    pyproject_ver = _version_from_pyproject()
    if pyproject_ver is not None:
        return pyproject_ver
    try:
        return _dist_version(_DIST_NAME)
    except PackageNotFoundError:
        return _UNKNOWN
