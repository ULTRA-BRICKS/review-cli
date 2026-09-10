"""Wiring test: every backend that calls `_run_streamed` must forward a
`true_silence_timeout=` kwarg computed by `model_behavior.true_silence_timeout_seconds`
(review-cli#474).

`review_opencode` already forwards it (`effective_true_silence`); the codex, omp, and
both claude-cli call sites used to call `_run_streamed` WITHOUT the kwarg, so a stall
with zero output hung for the full 13-30min seat timeout at 0% CPU instead of being
reaped at the true-silence cutoff. The mechanism itself
(`reviewlib/process.py::_run_streamed`) is exhaustively tested in
tests/test_streaming.py — this file pins only the WIRING: each backend must pass the
kwarg, and the passed value must equal the real registry function's return for the
SAME full model-identifier string the backend received (not a hardcoded stand-in).

Mock harness style mirrors tests/test_opencode_realrepo.py (`_capture_codex`),
tests/test_omp_backend.py (`_capture_omp`), and tests/test_streaming.py
(`_run_claude_cli_capture`): patch the backend boundary (`_run_streamed` + the binary
resolver), capture the kwargs, restore every patched global in a `finally`.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402
from reviewlib import model_behavior  # noqa: E402


class _CapturedStreamed:
    """Stand-in for `_run_streamed`: every backend call site passes `argv` positionally
    and everything else by keyword, so `**kwargs` captures the full set — including a
    newly added kwarg — without a signature change (mirrors test_streaming's
    `fake_run_streamed`)."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.kwargs: dict | None = None

    def __call__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


@contextlib.contextmanager
def _no_true_silence_env_override():
    """$REVIEW_TRUE_SILENCE_SECONDS<=0 makes `true_silence_timeout_seconds` return None
    for EVERY model — a correctly-wired backend would then pass None and the equality
    assertion below would fail for the wrong reason. Hermetic tests must not depend on
    the developer's shell env (mirrors the env save/restore discipline of
    test_true_silence_cooldown_wiring's `_with_store`)."""
    saved = os.environ.pop("REVIEW_TRUE_SILENCE_SECONDS", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["REVIEW_TRUE_SILENCE_SECONDS"] = saved


@contextlib.contextmanager
def _capture_codex():
    """Hermetic capture of review_codex's launch (no real `codex` binary needed): patch
    `_run_streamed` to record kwargs and `_which` to a fake path. Single restore point
    (mirrors test_opencode_realrepo.py's `_capture_codex`)."""
    cap = _CapturedStreamed()
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        yield cap
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which


@contextlib.contextmanager
def _capture_omp():
    """Patch `_run_streamed` (capture) and `_which` (hermetic — no real omp binary
    needed) for one test, restoring both afterward (mirrors test_omp_backend.py's
    `_capture_omp`)."""
    cap = _CapturedStreamed()
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        yield cap
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which


def _assert_wired(cap: _CapturedStreamed, model: str) -> None:
    """The single assertion every backend test shares: the kwarg is PRESENT, non-None,
    and equal to the real registry function's return for the same model string — a
    hardcoded stand-in (or the split `codex_model`/`omp_model`/`claude_model` local,
    which the registry is NOT keyed by) fails this."""
    expected = model_behavior.true_silence_timeout_seconds(model)
    assert expected is not None, model
    assert cap.kwargs is not None, "backend never reached _run_streamed"
    actual = cap.kwargs.get("true_silence_timeout")
    assert actual is not None, f"true_silence_timeout missing from {cap.kwargs}"
    assert actual == expected, (actual, expected)


def test_review_codex_passes_true_silence_timeout():
    with _capture_codex() as cap, _no_true_silence_env_override():
        with tempfile.TemporaryDirectory() as d:
            review_backends.review_codex("codex:gpt-5.5", "prompt", "diff", Path(d), 60)
    _assert_wired(cap, "codex:gpt-5.5")


def test_review_omp_passes_true_silence_timeout():
    with _capture_omp() as cap, _no_true_silence_env_override():
        with tempfile.TemporaryDirectory() as d:
            review_backends.review_omp(
                "omp:kimi-code/k3", "prompt", "diff", Path(d), 60
            )
    _assert_wired(cap, "omp:kimi-code/k3")


def test_review_claude_cli_passes_true_silence_timeout():
    cap = _CapturedStreamed()
    old_which = review_backends._which_optional
    old_run_streamed = review_backends._run_streamed
    old_trust = review_backends._ensure_workspace_trusted
    try:
        review_backends._which_optional = (  # type: ignore[assignment]
            lambda name: "/bin/claude" if name == "claude" else None
        )
        review_backends._run_streamed = cap  # type: ignore[assignment]
        # Stub auto-trust: it writes ~/.claude.json; this unit test must not touch the
        # real developer/CI config (mirrors test_streaming._run_claude_cli_capture).
        review_backends._ensure_workspace_trusted = lambda _cwd: None  # type: ignore[assignment]
        with _no_true_silence_env_override():
            review_backends.review_claude_cli(
                "claude:opus", "prompt", "diff", REPO_ROOT, 10
            )
    finally:
        review_backends._which_optional = old_which
        review_backends._run_streamed = old_run_streamed
        review_backends._ensure_workspace_trusted = old_trust
    _assert_wired(cap, "claude:opus")


def test_review_claude_cli_with_images_passes_true_silence_timeout():
    cap = _CapturedStreamed()
    old_which = review_backends._which_optional
    old_run_streamed = review_backends._run_streamed
    old_trust = review_backends._ensure_workspace_trusted
    old_untrust = review_backends._remove_workspace_trust
    try:
        review_backends._which_optional = (  # type: ignore[assignment]
            lambda name: "/bin/claude" if name == "claude" else None
        )
        review_backends._run_streamed = cap  # type: ignore[assignment]
        review_backends._ensure_workspace_trusted = lambda _cwd: None  # type: ignore[assignment]
        review_backends._remove_workspace_trust = lambda _cwd: None  # type: ignore[assignment]
        with _no_true_silence_env_override():
            with tempfile.TemporaryDirectory() as d:
                # `_stage_panel_images` only checks Path(image).is_file() — any bytes
                # do. A non-empty `staged` list is REQUIRED: with no images the
                # function delegates to review_claude_cli and never reaches its own
                # `_run_streamed` call, so the capture would silently assert on the
                # fallback path instead of the with-images one.
                fake_image = Path(d) / "panel.png"
                fake_image.write_bytes(b"\x89PNG\r\n\x1a\n not-a-real-image")
                review_backends.review_claude_cli_with_images(
                    "claude:opus", "prompt", "diff", Path(d), 10, images=(fake_image,)
                )
    finally:
        review_backends._which_optional = old_which
        review_backends._run_streamed = old_run_streamed
        review_backends._ensure_workspace_trusted = old_trust
        review_backends._remove_workspace_trust = old_untrust
    _assert_wired(cap, "claude:opus")


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
