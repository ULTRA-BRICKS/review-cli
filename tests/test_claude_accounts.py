#!/usr/bin/env python3
"""Unit tests for reviewlib.claude_accounts — the multi-account registry discovery
module behind review-cli#168's account-rotation fallback.

Hermetic: every test builds its own throwaway registry under a fresh temp dir (or
points $CLAUDE_ROTATE_ROOT at one with save/restore), so the suite never touches the
developer machine's real ~/.claude-accounts. No network, no subprocess, no real OAuth
state. Under pytest the conftest `_isolate_claude_accounts_root` fixture additionally
defaults $CLAUDE_ROTATE_ROOT to an empty per-test dir; the explicit roots below work
identically in the standalone `__main__` runner (no conftest there).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import claude_accounts  # noqa: E402

_ENV_KEY = "CLAUDE_ROTATE_ROOT"


def _save_env(key: str) -> str | None:
    return os.environ.get(key)


def _restore_env(key: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


def _registry(
    *names: str, current: str | None = None, emails: dict | None = None
) -> Path:
    """A throwaway claude-rotate-layout registry: one dir per name, an optional
    `current` state file, and optional per-account `.claude.json` emails."""
    root = Path(tempfile.mkdtemp())
    for name in names:
        account_dir = root / name
        account_dir.mkdir()
        email = (emails or {}).get(name)
        if email is not None:
            (account_dir / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": email}}),
                encoding="utf-8",
            )
    if current is not None:
        (root / "current").write_text(current + "\n", encoding="utf-8")
    return root


# --- accounts_root ---------------------------------------------------------


def test_accounts_root_defaults_to_home_dot_claude_accounts():
    old = _save_env(_ENV_KEY)
    os.environ.pop(_ENV_KEY, None)
    try:
        assert claude_accounts.accounts_root() == Path.home() / ".claude-accounts"
    finally:
        _restore_env(_ENV_KEY, old)


def test_accounts_root_honors_claude_rotate_root_override():
    old = _save_env(_ENV_KEY)
    os.environ[_ENV_KEY] = "/tmp/some-custom-rotate-root"
    try:
        assert claude_accounts.accounts_root() == Path("/tmp/some-custom-rotate-root")
    finally:
        _restore_env(_ENV_KEY, old)


def test_accounts_root_override_is_stripped_and_expanded():
    old = _save_env(_ENV_KEY)
    os.environ[_ENV_KEY] = "  ~/rotate-root  "
    try:
        assert claude_accounts.accounts_root() == Path.home() / "rotate-root"
    finally:
        _restore_env(_ENV_KEY, old)


def test_accounts_root_blank_override_falls_back_to_default():
    old = _save_env(_ENV_KEY)
    os.environ[_ENV_KEY] = "   "
    try:
        assert claude_accounts.accounts_root() == Path.home() / ".claude-accounts"
    finally:
        _restore_env(_ENV_KEY, old)


# --- list_accounts ---------------------------------------------------------


def test_list_accounts_empty_when_registry_dir_missing():
    missing = Path(tempfile.mkdtemp()) / "no-such-registry"
    assert claude_accounts.list_accounts(missing) == []


def test_list_accounts_discovers_only_account_prefixed_dirs_sorted():
    root = _registry("account-2", "account-1", "account-10")
    # Non-matching entries must be ignored: an unrelated dir, and a FILE that
    # happens to carry the prefix (only directories count).
    (root / "notes").mkdir()
    (root / "account-readme").write_text("not a dir", encoding="utf-8")
    found = claude_accounts.list_accounts(root)
    # Name-string sort (claude-rotate's own ordering), so account-10 sorts
    # between account-1 and account-2 — pinned deliberately.
    assert [p.name for p in found] == ["account-1", "account-10", "account-2"]


def test_list_accounts_unreadable_dir_degrades_to_empty():
    # A registry path that is a FILE, not a dir, raises NotADirectoryError on
    # iterdir — the OSError guard must turn that into the single-implicit-account
    # case, never a traceback.
    not_a_dir = Path(tempfile.mkdtemp()) / "registry-file"
    not_a_dir.write_text("oops", encoding="utf-8")
    assert claude_accounts.list_accounts(not_a_dir) == []


# --- current_account_name ---------------------------------------------------


def test_current_account_name_reads_state_file():
    root = _registry("account-1", current="account-1")
    assert claude_accounts.current_account_name(root) == "account-1"


def test_current_account_name_none_when_state_file_missing_or_blank():
    root = _registry("account-1")
    assert claude_accounts.current_account_name(root) is None
    (root / "current").write_text("   \n", encoding="utf-8")
    assert claude_accounts.current_account_name(root) is None


# --- account_email ----------------------------------------------------------


def test_account_email_reads_oauth_account_email():
    root = _registry("account-1", emails={"account-1": "alex@example.com"})
    assert claude_accounts.account_email(root / "account-1") == "alex@example.com"


def test_account_email_none_when_file_missing_or_unparsable():
    root = _registry("account-1", "account-2", "account-3", "account-4")
    assert claude_accounts.account_email(root / "account-1") is None  # no file
    (root / "account-2" / ".claude.json").write_text("{not json", encoding="utf-8")
    assert claude_accounts.account_email(root / "account-2") is None
    (root / "account-3" / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "   "}}), encoding="utf-8"
    )
    assert claude_accounts.account_email(root / "account-3") is None
    (root / "account-4" / ".claude.json").write_text(
        json.dumps({"hasCustomApiKeyResponses": []}), encoding="utf-8"
    )
    assert claude_accounts.account_email(root / "account-4") is None


# --- rotation_order ----------------------------------------------------------


def test_rotation_order_empty_without_registry():
    missing = Path(tempfile.mkdtemp()) / "no-such-registry"
    assert claude_accounts.rotation_order(missing) == []


def test_rotation_order_starts_at_current_and_wraps_in_name_order():
    root = _registry("account-1", "account-2", "account-3", current="account-2")
    assert [p.name for p in claude_accounts.rotation_order(root)] == [
        "account-2",
        "account-3",
        "account-1",
    ]


def test_rotation_order_without_current_uses_name_order():
    root = _registry("account-2", "account-1", "account-3")
    assert [p.name for p in claude_accounts.rotation_order(root)] == [
        "account-1",
        "account-2",
        "account-3",
    ]


def test_rotation_order_ignores_unknown_current_name():
    # A stale `current` pointing at a since-removed account must not crash or
    # reorder — fall back to plain name order.
    root = _registry("account-1", "account-2", current="account-99")
    assert [p.name for p in claude_accounts.rotation_order(root)] == [
        "account-1",
        "account-2",
    ]


def test_rotation_order_uses_env_root_when_no_explicit_root():
    root = _registry("account-1", "account-2", current="account-2")
    old = _save_env(_ENV_KEY)
    os.environ[_ENV_KEY] = str(root)
    try:
        assert [p.name for p in claude_accounts.rotation_order()] == [
            "account-2",
            "account-1",
        ]
    finally:
        _restore_env(_ENV_KEY, old)


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
