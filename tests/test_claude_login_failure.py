#!/usr/bin/env python3
"""A Claude CLI login that fails is remembered per account (review-cli#596).

The incident (2026-09-30, found during review-cli#594's live proof): `claude auth status`
said `loggedIn: true` for account-0 and account-1 while every call on them failed with
"Failed to authenticate: OAuth session expired and could not be refreshed". Nothing
recorded that failure, so bare `review status` kept calling the `claude:*` seats usable
and every review paid the failed Claude CLI call again before its omp twin answered.
These tests pin the fix:

  * after a claude:* call fails its login on every registered account, bare
    `review status` shows the seat UNUSABLE, naming the login failure, the accounts it
    hit and their login commands, and the omp substitute;
  * the next call goes straight to the omp twin without calling the Claude CLI, and an
    account whose login failed is not tried again while another account answers;
  * a later successful call on the seat makes it usable again;
  * logging the account back in (its stored login rewritten after the failure) clears
    the failure without waiting for a timer or paying a call.

Offline: the Claude CLI and omp are faked (no model call, no subprocess); the account
registry, cooldown store, provider cache and logs live in temp dirs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as _backends  # noqa: E402
import reviewlib.claude_accounts as _accounts  # noqa: E402
import reviewlib.cli as _cli  # noqa: E402
import reviewlib.panel as _panel  # noqa: E402
from reviewlib.modes.just_ask import mode_just_ask  # noqa: E402

_OPUS_CLI = "claude:claude-opus-4-8"
_OPUS_OMP = "omp:anthropic/claude-opus-4-8"
_AUTH_EXPIRED = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)
_ANSWER = "Answer: 42."


@contextlib.contextmanager
def _env(**values: str | None):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _patched(obj, **attrs):
    saved = {name: getattr(obj, name) for name in attrs}
    try:
        for name, value in attrs.items():
            setattr(obj, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(obj, name, value)


class _World:
    """Registered Claude CLI accounts whose `claude auth status` says logged in and whose
    login was stored an hour ago (`.credentials.json`, as a real account has); the Claude
    CLI answers per account from `cli` (account name -> (rc, stdout)); omp answers every
    call (or is unreachable when `twin_available` is False)."""

    def __init__(self, names, *, current, cli, twin_available=True):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "claude-accounts"
        an_hour_ago = time.time() - 3600
        for name in names:
            (self.root / name).mkdir(parents=True)
            (self.root / name / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": f"{name}@example.com"}}),
                encoding="utf-8",
            )
            credentials = self.root / name / ".credentials.json"
            credentials.write_text("{}", encoding="utf-8")
            os.utime(credentials, (an_hour_ago, an_hour_ago))
        (self.root / "current").write_text(current, encoding="utf-8")
        self.cli = dict(cli)
        self.twin_available = twin_available
        self.cli_calls: list[str] = []
        self.omp_calls: list[str] = []
        self._stack = contextlib.ExitStack()

    def account(self, name: str) -> Path:
        return self.root / name

    def _fake_cli(
        self,
        model,
        prompt,
        diff,
        cwd,
        timeout,
        round_no,
        *,
        effort=None,
        config_dir=None,
    ):
        name = Path(config_dir).name
        self.cli_calls.append(name)
        rc, out = self.cli[name]
        return _backends.ReviewResult(
            model=model, command="claude --print", returncode=rc, stdout=out, stderr=""
        )

    def _fake_omp(self, model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        self.omp_calls.append(model)
        return _backends.ReviewResult(
            model=model, command=f"omp {model}", returncode=0, stdout=_ANSWER, stderr=""
        )

    def _resolve(self, model):
        return self._fake_omp if model.startswith("omp:") else _backends.review_claude

    def __enter__(self):
        real_reason = _backends.backend_unavailable_reason

        def _reason(model):
            if model.startswith("omp:"):
                return None if self.twin_available else "omp: not installed"
            return real_reason(model)

        stack = self._stack
        stack.enter_context(
            _env(
                CLAUDE_ROTATE_ROOT=str(self.root),
                REVIEW_SEAT_COOLDOWN_FILE=str(self.tmp / "seat-cooldown.json"),
                REVIEW_PROVIDER_CACHE=str(self.tmp / "last-provider.json"),
                REVIEW_LOG_DIR=str(self.tmp / "logs"),
                REVIEW_STATS_FILE=str(self.tmp / "run-stats.jsonl"),
                REVIEW_HARNESS_LIMITS_JSON=json.dumps({"cards": []}),
                REVIEW_USAGE_LIMITS_FILE=str(self.tmp / "missing-usage.json"),
                REVIEW_CLAUDE_MODE="cli",
                REVIEW_RETRY_COUNT="0",
                REVIEW_FAKE_BACKEND=None,
                REVIEW_SEAT_COOLDOWN_SECONDS=None,
                CLAUDE_CONFIG_DIR=None,
            )
        )
        stack.enter_context(
            _patched(
                _backends,
                review_claude_cli=self._fake_cli,
                unpaid_provider_result=lambda *_a, **_k: None,
                runtime_provider_marked_unpaid=lambda _m: False,
                _have_claude_cli=lambda: True,
                backend_unavailable_reason=_reason,
                _CONFIG_UNPAID_PROVIDERS=_backends._CONFIG_UNPAID_PROVIDERS,
            )
        )
        stack.enter_context(_patched(_panel, resolve_backend=self._resolve))
        stack.enter_context(_patched(_accounts, account_logged_in=lambda _dir: True))
        stack.enter_context(_patched(_cli, load_config=lambda: {"models": [_OPUS_CLI]}))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def call_claude_seat(self) -> _backends.ReviewResult:
        return _backends.review_claude(_OPUS_CLI, "prompt", "", REPO_ROOT, 5)

    def just_ask(self) -> tuple[int, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = mode_just_ask("What is 6*7?", [_OPUS_CLI], "", REPO_ROOT, 5)
        return rc, out.getvalue()

    def status(self) -> str:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = _cli._status_subcommand([])
        assert rc == 0, out.getvalue()
        return out.getvalue()


def _line_with(text: str, needle: str) -> str:
    lines = [line for line in text.splitlines() if needle in line]
    assert lines, f"no line mentions {needle!r} in:\n{text}"
    return lines[0]


def test_status_shows_the_seat_unusable_after_every_account_failed_its_login():
    with _World(
        ("account-0", "account-1"),
        current="account-0",
        cli={"account-0": (1, _AUTH_EXPIRED), "account-1": (1, _AUTH_EXPIRED)},
    ) as world:
        world.call_claude_seat()
        assert world.cli_calls == ["account-0", "account-1"], world.cli_calls
        text = world.status()
    seat = _line_with(text, _OPUS_CLI)
    assert "UNUSABLE" in seat, seat
    for needle in ("OAuth session expired", "account-0", "account-1", _OPUS_OMP):
        assert needle in seat, (needle, seat)
    for name in ("account-0", "account-1"):
        line = _line_with(text, f"{name}@example.com")
        assert "LOGIN FAILED" in line and "OAuth session expired" in line, line
        assert f"CLAUDE_CONFIG_DIR={world.account(name)} claude auth login" in line, (
            line
        )


def test_the_next_call_goes_straight_to_the_omp_twin_without_calling_the_claude_cli():
    with _World(
        ("account-0", "account-1"),
        current="account-1",
        cli={"account-0": (1, _AUTH_EXPIRED), "account-1": (1, _AUTH_EXPIRED)},
    ) as world:
        first_rc, _first = world.just_ask()
        assert world.cli_calls == ["account-1", "account-0"], world.cli_calls
        rc, printed = world.just_ask()
    assert first_rc == 0 and rc == 0, printed
    assert world.cli_calls == ["account-1", "account-0"], world.cli_calls
    assert world.omp_calls == [_OPUS_OMP, _OPUS_OMP], world.omp_calls
    substitution = _line_with(printed, f"{_OPUS_CLI} -> {_OPUS_OMP}")
    assert "OAuth session expired" in substitution, substitution
    assert _ANSWER in printed, printed


def test_an_account_whose_login_failed_is_not_tried_again_while_another_answers():
    with _World(
        ("account-0", "account-1"),
        current="account-1",
        cli={"account-0": (0, _ANSWER), "account-1": (1, _AUTH_EXPIRED)},
    ) as world:
        first = world.call_claude_seat()
        second = world.call_claude_seat()
        text = world.status()
    assert first.stdout == _ANSWER and second.stdout == _ANSWER
    assert world.cli_calls == ["account-1", "account-0", "account-0"], world.cli_calls
    assert "usable" in _line_with(text, _OPUS_CLI)
    failed = _line_with(text, "account-1@example.com")
    assert "LOGIN FAILED" in failed, failed
    assert f"CLAUDE_CONFIG_DIR={world.account('account-1')} claude auth login" in failed
    working = _line_with(text, "account-0@example.com")
    assert "LOGIN FAILED" not in working, working


def test_a_later_successful_call_on_the_seat_makes_it_usable_again():
    """With no omp twin to hand the seat to, the Claude CLI is still the only way to get
    an answer, so it is tried; once it answers, the seat is usable again."""
    with _World(
        ("account-0",),
        current="account-0",
        cli={"account-0": (1, _AUTH_EXPIRED)},
        twin_available=False,
    ) as world:
        world.call_claude_seat()
        assert "UNUSABLE" in _line_with(world.status(), _OPUS_CLI)
        world.cli["account-0"] = (0, _ANSWER)
        rc, printed = world.just_ask()
        text = world.status()
    assert rc == 0, printed
    assert world.cli_calls == ["account-0", "account-0"], world.cli_calls
    seat = _line_with(text, _OPUS_CLI)
    assert "UNUSABLE" not in seat and "usable" in seat, seat
    assert "LOGIN FAILED" not in _line_with(text, "account-0@example.com")


def test_logging_the_account_back_in_clears_the_failure_without_a_call():
    with _World(
        ("account-0",),
        current="account-0",
        cli={"account-0": (1, _AUTH_EXPIRED)},
    ) as world:
        world.call_claude_seat()
        assert "UNUSABLE" in _line_with(world.status(), _OPUS_CLI)
        # What `CLAUDE_CONFIG_DIR=<dir> claude auth login` leaves behind where the CLI
        # keeps its login in a file: the stored credentials rewritten after the failure.
        credentials = world.account("account-0") / ".credentials.json"
        credentials.write_text("{}", encoding="utf-8")
        later = time.time() + 5
        os.utime(credentials, (later, later))
        text = world.status()
        world.cli["account-0"] = (0, _ANSWER)
        rc, _printed = world.just_ask()
    assert "usable" in _line_with(text, _OPUS_CLI) and "UNUSABLE" not in text, text
    assert rc == 0
    assert world.cli_calls == ["account-0", "account-0"], world.cli_calls
    assert world.omp_calls == [], world.omp_calls


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
