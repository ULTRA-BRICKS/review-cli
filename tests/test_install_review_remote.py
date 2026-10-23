#!/usr/bin/env python3
"""`review install-skill` (the Python install path install.sh ends with) links `review-remote`
into ~/.local/bin, so a new machine needs no manual symlink (review-cli#658).

State is reported like every install-* target: `+ wrote/updated` when it linked, `✓ already
configured` on a re-run, `! conflict` (left as-is, non-zero) when something else holds the name.
All HOME state is a temp dir.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reviewlib import install  # noqa: E402
from test_install_state import _capture, _isolated_home, _restore  # noqa: E402


def _script(tmp: str) -> Path:
    path = Path(tmp) / "checkout" / "scripts" / "review-remote"
    path.parent.mkdir(parents=True)
    path.write_text("#!/usr/bin/env python3\n")
    path.chmod(0o755)
    return path


def _install(script: Path | None) -> tuple[int, str]:
    return _capture(
        install.install_agent_skill, "review", "SKILL", "- blurb", link_script=script
    )


def test_install_skill_links_review_remote_and_a_rerun_is_a_noop():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            script = _script(tmp)
            link = Path(tmp) / ".local" / "bin" / "review-remote"
            rc, out = _install(script)
            assert rc == 0, out
            assert link.is_symlink() and link.resolve() == script.resolve(), out
            assert any(
                "+ wrote/updated" in ln and str(link) in ln for ln in out.splitlines()
            ), out
            rc, out = _install(script)
            assert rc == 0 and "nothing to do" in out, out
            assert any(
                "already configured" in ln and str(link) in ln
                for ln in out.splitlines()
            ), out
        finally:
            _restore(saved)


def test_a_link_to_another_script_is_a_conflict_and_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            script = _script(tmp)
            other = Path(tmp) / "other-review-remote"
            other.write_text("x")
            link = Path(tmp) / ".local" / "bin" / "review-remote"
            link.parent.mkdir(parents=True)
            link.symlink_to(other)
            rc, out = _install(script)
            assert rc != 0 and "conflict" in out and "nothing to do" not in out, out
            assert link.resolve() == other.resolve()
        finally:
            _restore(saved)


def test_a_regular_file_in_the_way_is_a_conflict_and_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            script = _script(tmp)
            link = Path(tmp) / ".local" / "bin" / "review-remote"
            link.parent.mkdir(parents=True)
            link.write_text("not ours\n")
            rc, out = _install(script)
            assert rc != 0 and "conflict" in out, out
            assert link.read_text() == "not ours\n" and not link.is_symlink()
        finally:
            _restore(saved)


def test_a_package_install_without_the_script_skips_the_link():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rc, out = _install(None)
            assert rc == 0 and "review-remote" not in out, out
            assert not (Path(tmp) / ".local" / "bin").exists()
        finally:
            _restore(saved)


def test_install_skill_wires_the_checkouts_own_script():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rc, out = _capture(install.install_skill)
            link = Path(tmp) / ".local" / "bin" / "review-remote"
            assert rc == 0, out
            assert link.resolve() == (REPO_ROOT / "scripts" / "review-remote").resolve()
        finally:
            _restore(saved)


def test_a_symlink_loop_in_the_way_is_a_conflict_not_a_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            script = _script(tmp)
            link = Path(tmp) / ".local" / "bin" / "review-remote"
            link.parent.mkdir(parents=True)
            link.symlink_to(link.name)  # review-remote -> review-remote
            rc, out = _install(script)
            assert rc != 0 and "conflict" in out, out
        finally:
            _restore(saved)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
