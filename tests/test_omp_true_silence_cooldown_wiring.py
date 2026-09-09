"""Wiring test: `review_omp` must record a seat_cooldown entry when `_run_streamed`
reaps a seat for TRUE SILENCE (the explicit `.true_silenced` attribute -- see
reviewlib.process's true_silence_timeout, reviewlib.model_behavior's per-model
threshold), and must NOT record one for an ordinary successful call -- mirroring
review_opencode's own wiring, exercised via tests/test_true_silence_cooldown_wiring.py
(review-cli#235's opencode half). This is the omp half of the same issue: omp's
`access_method="omp"` cooldown namespace is independent of opencode's, so a true-
silence trip on one backend must never cool down the other.

Mirrors tests/test_true_silence_cooldown_wiring.py's `_with_store` isolation and
tests/test_omp_backend.py's `_capture_omp`-style mocking for the stubbed-dispatch
tests, and tests/test_streaming.py::test_true_silence_kills_a_never_talking_child_
before_the_idle_floor's real-subprocess pattern for the end-to-end acceptance test
(d): a genuinely hanging (zero-output) child process, run through the REAL, unstubbed
`reviewlib.process._run_streamed`, must be reaped well within the wall-clock budget
and reported as a clean failure -- never left hanging indefinitely -- and the
cooldown side of the fix must also fire.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402
from reviewlib import seat_cooldown as sc  # noqa: E402

MODEL = "omp:test-provider/test-model"


def _with_store(fn):
    """Isolates BOTH the cooldown store AND the dashboard log dir for every test
    (mirrors tests/test_true_silence_cooldown_wiring.py's own `_with_store`): the
    cooldown-skip path writes a REAL sidecar log via `process.log_dir()`, so without
    isolating `REVIEW_LOG_DIR` too, a test that reaches a cooldown skip would inject a
    synthetic event into a developer's real dashboard log dir."""
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as log_dir:
        saved_store = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        saved_log_dir = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        os.environ["REVIEW_LOG_DIR"] = log_dir
        try:
            return fn()
        finally:
            for key, saved in (
                ("REVIEW_SEAT_COOLDOWN_FILE", saved_store),
                ("REVIEW_SEAT_COOLDOWN_SECONDS", saved_ttl),
                ("REVIEW_LOG_DIR", saved_log_dir),
            ):
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


class _FakeProc:
    """Mirrors the real `_run_streamed` return shape: `.true_silenced` is the
    authoritative signal, independent of `.returncode`."""

    def __init__(
        self,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        *,
        true_silenced: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.true_silenced = true_silenced


@contextlib.contextmanager
def _stub_run_streamed(returncode: int, *, true_silenced: bool):
    """Patch `_run_streamed` to return a fixed fake, and `_which` so the call reaches
    `_run_streamed` deterministically without a real omp binary (mirrors
    tests/test_omp_backend.py's `_capture_omp`). `captured["calls"]` counts real
    `_run_streamed` invocations -- a test can assert it stayed at 0 when a cooldown
    should have short-circuited the dispatch entirely."""
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    captured: dict[str, object] = {"calls": 0, "true_silence_timeout": None}

    def _fake(*args, **kwargs):
        captured["calls"] += 1
        captured["true_silence_timeout"] = kwargs.get("true_silence_timeout")
        return _FakeProc(
            returncode,
            stdout="partial or full output",
            stderr="",
            true_silenced=true_silenced,
        )

    review_backends._run_streamed = _fake  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        yield captured
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which


# ---------------------------------------------------------------------------
# (a) wiring test
# ---------------------------------------------------------------------------


def test_true_silence_records_a_cooldown():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="omp") is None
        with _stub_run_streamed(125, true_silenced=True) as captured:
            result = review_backends.review_omp(MODEL, "prompt", "diff", Path("."), 60)
        assert result.returncode == 125
        assert sc.active_cooldown(MODEL, access_method="omp") is not None

        from reviewlib import model_behavior

        assert captured[
            "true_silence_timeout"
        ] == model_behavior.true_silence_timeout_seconds(MODEL)

    _with_store(_run)


# ---------------------------------------------------------------------------
# (b) the cooldown is actually consulted on the next call
# ---------------------------------------------------------------------------


def test_a_recorded_cooldown_is_actually_consulted_on_the_next_call():
    """After a true-silence trip, a second `review_omp` call for the SAME model must
    skip the real dispatch entirely (0 further `_run_streamed` calls) and return the
    synthetic cooldown-skip sentinel instead."""

    def _run():
        with _stub_run_streamed(125, true_silenced=True) as first_call:
            first = review_backends.review_omp(MODEL, "prompt", "diff", Path("."), 60)
        assert first.returncode == 125
        assert first_call["calls"] == 1
        assert sc.active_cooldown(MODEL, access_method="omp") is not None

        with _stub_run_streamed(0, true_silenced=False) as second_call:
            second = review_backends.review_omp(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert second_call["calls"] == 0, (
            "review_omp dispatched a real call despite an active cooldown"
        )
        assert second.returncode == 0  # the synthetic cooldown-skip sentinel

    _with_store(_run)


# ---------------------------------------------------------------------------
# (c) negative control -- an ordinary success must not record a cooldown
# ---------------------------------------------------------------------------


def test_ordinary_success_does_not_record_a_cooldown():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="omp") is None
        with _stub_run_streamed(0, true_silenced=False):
            result = review_backends.review_omp(MODEL, "prompt", "diff", Path("."), 60)
        assert result.returncode == 0
        assert sc.active_cooldown(MODEL, access_method="omp") is None

    _with_store(_run)


# ---------------------------------------------------------------------------
# (d) acceptance criterion: a REAL, unstubbed, genuinely hanging child process
# ---------------------------------------------------------------------------

_NEVER_TALKING_SCRIPT = "import time\ntime.sleep(60)\n"  # never prints a single byte


@contextlib.contextmanager
def _fake_omp_binary():
    """A REAL executable script that ignores every argv it is given, sleeps for 60s,
    and never writes a single byte to stdout/stderr -- the same "never talking"
    failure shape as tests/test_streaming.py's real-subprocess true-silence test, but
    reached by driving the REAL `review_omp` end-to-end (not `_run_streamed`
    directly). Patching `_which("omp")` to return this script's path (rather than
    `sys.executable`) means the full real omp argv (`-p --no-session --tools ...`) is
    passed to the script and simply ignored, so review_omp's own argv-construction
    code runs completely for real; only the omp binary itself is swapped out."""
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "fake-omp"
        script.write_text(f"#!{sys.executable}\n{_NEVER_TALKING_SCRIPT}", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        orig_which = review_backends._which
        review_backends._which = (
            lambda name: str(script) if name == "omp" else orig_which(name)
        )
        try:
            yield
        finally:
            review_backends._which = orig_which


def test_a_genuinely_hanging_omp_seat_is_reaped_and_cooled_down_end_to_end():
    """THE ACCEPTANCE-CRITERION TEST: drives `review_omp` down a real subprocess argv
    (the REAL, unstubbed `_run_streamed`) that never writes a single byte, with a tiny
    true-silence budget forced via $REVIEW_TRUE_SILENCE_SECONDS. Proves all three
    legs of the fix fire together: (i) review_omp returns within a few seconds, not
    60s -- termination fired, not an indefinite hang; (ii) the result is a CLEAN
    failure (rc 125, a TRUE-SILENCE marker), not an exception or a hang; (iii) the
    omp cooldown is now active -- the cooldown side of the fix also fired."""

    def _run():
        saved = os.environ.get("REVIEW_TRUE_SILENCE_SECONDS")
        os.environ["REVIEW_TRUE_SILENCE_SECONDS"] = "1"
        try:
            assert sc.active_cooldown(MODEL, access_method="omp") is None
            with _fake_omp_binary():
                started = time.monotonic()
                result = review_backends.review_omp(
                    MODEL, "prompt", "diff", Path("."), 30
                )
                elapsed = time.monotonic() - started
        finally:
            if saved is None:
                os.environ.pop("REVIEW_TRUE_SILENCE_SECONDS", None)
            else:
                os.environ["REVIEW_TRUE_SILENCE_SECONDS"] = saved

        assert elapsed < 20, (
            f"a never-talking omp seat took {elapsed:.2f}s -- true-silence reap did "
            "not fire in time"
        )
        assert result.returncode == 125, (
            f"a never-talking omp seat must be reaped with rc 125, got "
            f"{result.returncode}: {result.stdout!r}"
        )
        assert "TRUE-SILENCE" in result.stdout
        assert sc.active_cooldown(MODEL, access_method="omp") is not None, (
            "the true-silence reap did not record an omp cooldown"
        )

    _with_store(_run)


if __name__ == "__main__":
    failures = 0
    tests = [
        obj
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
