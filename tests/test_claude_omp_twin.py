#!/usr/bin/env python3
"""A Claude CLI seat that cannot run drops to the SAME model through omp (review-cli#594).

The incident (2026-09-29/30, this repo's own run logs): every `claude:*` seat died on
"Failed to authenticate: OAuth session expired" / "You've hit your session limit" while
omp's separately logged-in Anthropic accounts kept answering the same models — yet the
board never routed a Claude seat through omp, just-ask had no failover at all, and
`review status` could not say why a seat was dead. These tests pin the fix:

  * a dead `claude:<id>` executor is followed by `omp:anthropic/<id>` for the SAME seat,
    in `review diff`'s board and in the panel modes (just-ask / quorum / brainstorm, via
    their shared runners), and the printed result says what was substituted and why;
  * the substituted seat is credited once, under its own role, with the model that
    actually answered;
  * the Claude CLI stays the first executor on the next run (omp is never cached as the
    seat's last-working provider);
  * bare `review status` and `review entities` name why a seat cannot run and what it
    would be substituted with, and show logged-out Claude CLI accounts with the login
    command.

Offline: executors are faked by id (no model call, no subprocess), the provider cache,
retry logs, cooldown store and Claude account registry all live in temp dirs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as _backends  # noqa: E402
import reviewlib.claude_accounts as _accounts  # noqa: E402
import reviewlib.cli as _cli  # noqa: E402
import reviewlib.panel as _panel  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes.just_ask import mode_just_ask  # noqa: E402
from reviewlib.panel import (  # noqa: E402
    build_board_job,
    completed_role_coverage,
    format_result,
    result_is_usable,
    run_board_with_failover,
    run_role_slots,
)
from reviewlib.provider_failover import provider_chain  # noqa: E402
from reviewlib.seat_cooldown import record_cooldown  # noqa: E402

_OPUS_CLI = "claude:claude-opus-4-8"
_OPUS_OMP = "omp:anthropic/claude-opus-4-8"
_SONNET_CLI = "claude:claude-sonnet-5"
_SONNET_OMP = "omp:anthropic/claude-sonnet-5"
_AUTH_EXPIRED = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)
_SESSION_LIMIT = "You've hit your session limit · resets 5:40am (Europe/Belgrade)"


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


class _FakeExecutors:
    """Every executor is reachable and paid; each call answers from `behaviour` keyed on
    the CONCRETE executor id (default: a real-looking answer). Records dispatch order."""

    def __init__(self, behaviour: dict[str, tuple[int, str]]):
        self.behaviour = behaviour
        self.dispatched: list[str] = []
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "last-provider.json"
        self._stack = contextlib.ExitStack()

    def _resolve(self, _model):
        def _backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
            self.dispatched.append(model)
            rc, out = self.behaviour.get(model, (0, f"real answer from {model}"))
            return _backends.ReviewResult(
                model=model,
                command=f"fake {model}",
                returncode=rc,
                stdout=out,
                stderr="",
            )

        return _backend

    def __enter__(self):
        self._stack.enter_context(
            _patched(
                _panel,
                resolve_backend=self._resolve,
                backend_available=lambda _m: True,
            )
        )
        self._stack.enter_context(
            _patched(_backends, runtime_provider_marked_unpaid=lambda _m: False)
        )
        self._stack.enter_context(
            _env(
                REVIEW_PROVIDER_CACHE=str(self.cache),
                REVIEW_RETRY_COUNT="0",
                REVIEW_LOG_DIR=str(self.tmp / "logs"),
            )
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def test_board_claude_seat_with_expired_login_answers_through_omp_under_its_own_role():
    pool = [
        BoardReviewer(_OPUS_CLI, "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
    ]
    reserve = [BoardReviewer(_OPUS_OMP, "correctness", "Opus-omp")]
    with _FakeExecutors({_OPUS_CLI: (1, _AUTH_EXPIRED)}) as fx:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            outcome = run_board_with_failover(
                pool, reserve, "Review this.", "+x", REPO_ROOT, 5
            )

    assert not outcome.degraded, outcome
    # The dead Claude CLI ran once, then the SAME seat ran through omp; the Opus-omp
    # reserve seat was never promoted (the seat did not fail), so omp ran exactly once.
    assert fx.dispatched.count(_OPUS_CLI) == 1, fx.dispatched
    assert fx.dispatched.count(_OPUS_OMP) == 1, fx.dispatched
    # Quorum credit: each role exactly once; correctness is credited to the executor that
    # actually answered, not to the dead Claude CLI.
    assert sorted(outcome.usable_roles) == ["consistency", "correctness"], outcome
    opus = outcome.usable_roles.index("correctness")
    assert outcome.usable_models[opus] == _OPUS_OMP, outcome.usable_models
    rendered = format_result(outcome.usable[opus])
    for needle in (_OPUS_CLI, _OPUS_OMP, "OAuth session expired"):
        assert needle in rendered, rendered
    assert "OAuth session expired" in err.getvalue(), err.getvalue()


def test_claude_cli_stays_the_first_executor_on_the_next_run():
    seat = [BoardReviewer(_OPUS_CLI, "correctness", "Opus")]
    with _FakeExecutors({_OPUS_CLI: (1, _SESSION_LIMIT)}) as fx:
        with contextlib.redirect_stderr(io.StringIO()):
            run_board_with_failover(seat, [], "Review this.", "+x", REPO_ROOT, 5)
            run_board_with_failover(seat, [], "Review this.", "+x", REPO_ROOT, 5)
    # Run 2 tries the Claude CLI again first (it may have recovered), then omp.
    assert fx.dispatched == [_OPUS_CLI, _OPUS_OMP, _OPUS_CLI, _OPUS_OMP], fx.dispatched


def test_just_ask_answers_through_the_omp_twin_and_prints_the_substitution():
    with _FakeExecutors(
        {_SONNET_CLI: (1, _SESSION_LIMIT), _SONNET_OMP: (0, "Answer: 42.")}
    ) as fx:
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = mode_just_ask("What is 6*7?", [_SONNET_CLI], "", REPO_ROOT, 5)
    printed = out.getvalue()
    assert rc == 0, printed
    assert fx.dispatched == [_SONNET_CLI, _SONNET_OMP], fx.dispatched
    for needle in ("Answer: 42.", _SONNET_CLI, _SONNET_OMP, "session limit"):
        assert needle in printed, printed


def test_a_last_working_cache_reorder_is_not_reported_as_a_substitution():
    """Nothing failed and the seat's own executor is still in its chain: the cache just put
    the alternate first. That is routing, so no `substituted:` line and the seat keeps its
    own model for the quorum."""
    seat = [BoardReviewer("zai:glm-5.2", "quality", "GLM")]
    with _FakeExecutors({}) as fx:
        fx.cache.write_text(json.dumps({"glm-5.2": "oc:zai/glm-5.2"}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            outcome = run_board_with_failover(seat, [], "Review this.", "+x", REPO_ROOT, 5)
    assert fx.dispatched == ["oc:zai/glm-5.2"], fx.dispatched
    assert "substituted" not in format_result(outcome.usable[0])
    assert outcome.usable_models == ["zai:glm-5.2"], outcome.usable_models


def test_task_role_slots_each_count_once_when_every_claude_slot_is_substituted():
    """The `--task` role-slot runner (just-ask / quorum / brainstorm padding one model
    across the ship floor): every slot answers through omp, every role counts once."""
    roles = ["correctness", "quality", "security"]
    jobs = [
        build_board_job(BoardReviewer(_OPUS_CLI, role, _OPUS_CLI), "Q?", "")
        for role in roles
    ]
    with _FakeExecutors({_OPUS_CLI: (1, _AUTH_EXPIRED)}) as fx:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            results = run_role_slots(jobs, REPO_ROOT, 5, roles=roles)
    assert all(result_is_usable(r) for r in results), results
    assert "Roles: 3/3 complete" in err.getvalue(), err.getvalue()
    assert [
        role for _m, role in completed_role_coverage(roles, results, [_OPUS_CLI] * 3)
    ] == roles
    assert fx.dispatched.count(_OPUS_OMP) == 3, fx.dispatched


def test_every_claude_cli_model_falls_to_its_omp_twin_before_other_routes():
    cache = Path(tempfile.mkdtemp()) / "last-provider.json"

    def chain(model):
        return provider_chain(
            model, available=lambda _m: True, unpaid=lambda _m: False, cache_path=cache
        )

    assert chain(_SONNET_CLI) == [_SONNET_CLI, _SONNET_OMP]
    assert chain("claude:claude-fable-5") == [
        "claude:claude-fable-5",
        "omp:anthropic/claude-fable-5",
    ]
    # omp (a proven multi-account route) comes before the opencode anthropic route.
    assert chain(_OPUS_CLI)[:2] == [_OPUS_CLI, _OPUS_OMP]


# --- review status / review entities ------------------------------------------------------


@contextlib.contextmanager
def _seat_health_world():
    """A board of four seats on a machine where both registered Claude CLI accounts are
    logged out, Grok is cooling down after running out of credits, commandcode is unpaid
    and omp is healthy."""
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "claude-accounts"
    for name, email in (("account-0", "a@example.com"), ("account-1", "b@example.com")):
        (root / name).mkdir(parents=True)
        (root / name / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8"
        )
    (root / "current").write_text("account-1", encoding="utf-8")
    config = {
        "models": [
            _OPUS_CLI,
            "oc:xai/grok-4.6",
            "commandcode:deepseek/deepseek-v4-pro",
            _SONNET_OMP,
        ],
        "unpaid_providers": ["commandcode"],
    }

    def _reason(model: str) -> str | None:
        if model.startswith("commandcode:"):
            return "commandcode: provider marked unpaid"
        return None

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            _env(
                CLAUDE_ROTATE_ROOT=str(root),
                REVIEW_SEAT_COOLDOWN_FILE=str(tmp / "cooldown.json"),
                REVIEW_PROVIDER_CACHE=str(tmp / "last-provider.json"),
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_CLAUDE_MODE=None,
                REVIEW_SEAT_COOLDOWN_SECONDS=None,
            )
        )
        stack.enter_context(_patched(_cli, load_config=lambda: config))
        stack.enter_context(_patched(_accounts, account_logged_in=lambda _dir: False))
        stack.enter_context(
            _patched(
                _backends,
                backend_unavailable_reason=_reason,
                _CONFIG_UNPAID_PROVIDERS=_backends._CONFIG_UNPAID_PROVIDERS,
            )
        )
        record_cooldown(
            "oc:xai/grok-4.6",
            "Error: You have run out of credits",
            access_method="opencode",
        )
        yield root


def _line_with(text: str, needle: str) -> str:
    lines = [line for line in text.splitlines() if needle in line]
    assert lines, f"no line mentions {needle!r} in:\n{text}"
    return lines[0]


def test_bare_review_status_names_why_each_seat_cannot_run_and_its_substitute():
    with _seat_health_world() as root:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = _cli._status_subcommand([])
    text = out.getvalue()
    assert rc == 0, text
    opus = _line_with(text, _OPUS_CLI)
    assert "every registered Claude CLI account is logged out" in opus, opus
    assert _OPUS_OMP in opus, opus
    grok = _line_with(text, "oc:xai/grok-4.6")
    assert "cooling down" in grok and "run out of credits" in grok, grok
    assert "unpaid" in _line_with(text, "commandcode:deepseek/deepseek-v4-pro")
    assert "usable" in _line_with(text, _SONNET_OMP)
    for name in ("account-0", "account-1"):
        account = _line_with(text, f"CLAUDE_CONFIG_DIR={root / name} claude auth login")
        assert name in account and "logged out" in account.lower(), account


def test_review_entities_json_carries_the_same_seat_reasons_and_account_state():
    with _seat_health_world():
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = _cli._entities_subcommand(["--json"])
    payload = json.loads(out.getvalue())
    assert rc == 0
    seats = {m["seat"]: m for m in payload["models"]}
    assert seats[_OPUS_CLI]["available"] is False, seats[_OPUS_CLI]
    assert "logged out" in seats[_OPUS_CLI]["reason"], seats[_OPUS_CLI]
    assert seats[_OPUS_CLI]["substitute"] == _OPUS_OMP, seats[_OPUS_CLI]
    assert (
        seats[_SONNET_OMP]["available"] is True and seats[_SONNET_OMP]["reason"] is None
    )
    assert [a["logged_in"] for a in payload["accounts"]] == [False, False], payload[
        "accounts"
    ]


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
