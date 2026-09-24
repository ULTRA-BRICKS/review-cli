#!/usr/bin/env python3
"""Tests for `review diff --json` (review-cli#546): the additive structured findings
output.

Covers, all offline (no model call, no network -- backends are monkeypatched the SAME
way tests/test_reviewer_board.py / tests/test_review_commit_checkpoint.py already do:
`review_mode.resolve_backend`/`panel.resolve_backend` swapped for an in-process fake):

  (a) `_extract_json_findings_block` -- the fenced-block / whole-text extraction, and
      its deliberate refusal to scan free prose for a balanced `{...}` (see the
      function's own docstring for why that would be UNSAFE here, unlike
      `features/visual/vision_client._extract_first_json_object`).
  (b) `_coerce_finding` -- per-item normalization: valid items pass through untouched;
      an invalid/missing severity, confidence, or verdict falls back to the documented
      default rather than dropping the finding; a non-dict item is dropped.
  (c) `_seat_json_findings` -- the per-seat (findings, reported) split: an unusable
      seat or an unparseable response is DEGRADED; a parsed-but-empty findings list is
      a genuine reported clean pass.
  (d) `_build_json_report` -- merges multiple seats into the top-level
      findings/summary.expected/reported/degraded shape, tagging each finding with its
      seat's `source`.
  (e) `_json_exit_code` -- the 0 (clean)/1 (finding present)/2 (nothing reported) exit
      contract, DISTINCT from the prose path's seat-health-only `0 if ok else 1`.
  (f) `_with_json_findings_request` -- purely additive prompt suffix.
  (g) End-to-end through `mode_review` (flat AND board dispatch paths) with
      `json_output=True`: printed stdout is valid JSON with the right shape and exit
      code for a clean pass, a finding-present pass, an all-degraded pass, and a
      mixed pass; a `--json`-absent call still prints the UNCHANGED prose format.
  (h) `_handler` end-to-end: `ctx.args.json=True` appends the findings-request suffix
      to the prompt actually dispatched and threads `json_output` through to
      `mode_review`.

Runs as a plain script (mirrors tests/test_reviewer_board.py): each `test_*` is
invoked by the __main__ block below, no pytest required.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.panel as panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import DEFAULT_PROMPT, BoardReviewer  # noqa: E402
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.contract import ModeContext  # noqa: E402
from reviewlib.modes.review import (  # noqa: E402
    _build_json_report,
    _coerce_finding,
    _extract_json_findings_block,
    _JSON_FINDINGS_FENCE_TAG,
    _json_exit_code,
    _seat_json_findings,
    _with_json_findings_request,
    mode_review,
)


def _usable(model: str, stdout: str) -> ReviewResult:
    return ReviewResult(
        model=model, command="fake", returncode=0, stdout=stdout, stderr=""
    )


def _unusable(model: str) -> ReviewResult:
    return ReviewResult(
        model=model, command="fake", returncode=1, stdout="", stderr="boom"
    )


def _fenced(payload: dict, *, tag: str = _JSON_FINDINGS_FENCE_TAG) -> str:
    return f"```{tag}\n{json.dumps(payload)}\n```"


# === (a) _extract_json_findings_block =====================================
def test_extract_json_findings_block_parses_fenced_review_json_tag():
    body = (
        "Some prose review first.\n\n" + _fenced({"findings": []}) + "\ntrailing text"
    )
    assert _extract_json_findings_block(body) == {"findings": []}


def test_extract_json_findings_block_falls_back_to_plain_json_fence():
    body = (
        "prose\n```json\n" + json.dumps({"findings": [{"verdict": "CLEAR"}]}) + "\n```"
    )
    assert _extract_json_findings_block(body) == {"findings": [{"verdict": "CLEAR"}]}


def test_extract_json_findings_block_falls_back_to_whole_text():
    body = json.dumps({"findings": []})
    assert _extract_json_findings_block(body) == {"findings": []}


def test_extract_json_findings_block_returns_none_on_garbage():
    assert _extract_json_findings_block("just some prose, no JSON at all") is None
    assert _extract_json_findings_block("") is None


def test_extract_json_findings_block_skips_non_object_fence_and_finds_next():
    # A response that fences a bare list first (not the requested object shape),
    # then the real object -- the extractor must not stop at the first PARSEABLE
    # fence, only the first one that is a dict.
    body = "```json\n[1, 2, 3]\n```\nmore prose\n" + _fenced(
        {"findings": [{"verdict": "FINDING"}]}
    )
    assert _extract_json_findings_block(body) == {"findings": [{"verdict": "FINDING"}]}


def test_extract_json_findings_block_does_not_scan_prose_braces():
    # A diff snippet quoted in the prose review contains braces of its own; with no
    # fenced block at all, this must NOT be mistaken for the requested structure
    # (the function deliberately has no free-prose brace-scanning fallback).
    body = "The diff adds `const cfg = {retries: 3};` which looks fine."
    assert _extract_json_findings_block(body) is None


# === (b) _coerce_finding ====================================================
def test_coerce_finding_passes_through_valid_item():
    raw = {
        "file": "reviewlib/cli.py",
        "line": 42,
        "severity": "P1",
        "confidence": "high",
        "verdict": "FINDING",
    }
    assert _coerce_finding(raw) == raw


def test_coerce_finding_defaults_invalid_severity_confidence_verdict():
    raw = {
        "file": None,
        "line": None,
        "severity": "urgent",
        "confidence": "certain",
        "verdict": "maybe",
    }
    coerced = _coerce_finding(raw)
    assert coerced == {
        "file": None,
        "line": None,
        "severity": "P2",
        "confidence": "medium",
        "verdict": "FINDING",
    }


def test_coerce_finding_drops_non_dict_item():
    assert _coerce_finding("not a dict") is None
    assert _coerce_finding(None) is None
    assert _coerce_finding(42) is None


def test_coerce_finding_coerces_line_to_int_or_none():
    assert _coerce_finding({"line": "17", "verdict": "FINDING"})["line"] == 17
    assert (
        _coerce_finding({"line": "not-a-number", "verdict": "FINDING"})["line"] is None
    )
    assert _coerce_finding({"verdict": "FINDING"})["line"] is None
    assert _coerce_finding({"file": 123, "verdict": "FINDING"})["file"] is None


# === (c) _seat_json_findings ================================================
def test_seat_json_findings_degrades_unusable_result():
    findings, reported = _seat_json_findings(_unusable("m1"))
    assert findings == []
    assert reported is False


def test_seat_json_findings_degrades_unparseable_stdout():
    findings, reported = _seat_json_findings(_usable("m1", "no structured block here"))
    assert findings == []
    assert reported is False


def test_seat_json_findings_degrades_missing_findings_key():
    findings, reported = _seat_json_findings(_usable("m1", _fenced({"other": []})))
    assert findings == []
    assert reported is False


def test_seat_json_findings_reports_empty_clean_pass():
    findings, reported = _seat_json_findings(_usable("m1", _fenced({"findings": []})))
    assert findings == []
    assert reported is True


def test_seat_json_findings_reports_real_findings():
    payload = {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "P0",
                "confidence": "high",
                "verdict": "FINDING",
            },
            {
                "file": None,
                "line": None,
                "severity": "P3",
                "confidence": "low",
                "verdict": "CLEAR",
            },
        ]
    }
    findings, reported = _seat_json_findings(_usable("m1", _fenced(payload)))
    assert reported is True
    assert len(findings) == 2
    assert findings[0]["verdict"] == "FINDING"
    assert findings[1]["verdict"] == "CLEAR"


# === (d) _build_json_report =================================================
def test_build_json_report_merges_seats_and_tags_source():
    clean = _usable("Opus [correctness]", _fenced({"findings": []}))
    finder = _usable(
        "Sol [performance]",
        _fenced(
            {
                "findings": [
                    {
                        "file": "x.py",
                        "line": 3,
                        "severity": "P1",
                        "confidence": "high",
                        "verdict": "FINDING",
                    }
                ]
            }
        ),
    )
    degraded = _unusable("Fable [security]")
    report = _build_json_report([clean, finder, degraded])
    assert report["summary"]["expected"] == [
        "Opus [correctness]",
        "Sol [performance]",
        "Fable [security]",
    ]
    assert report["summary"]["reported"] == ["Opus [correctness]", "Sol [performance]"]
    assert report["summary"]["degraded"] == ["Fable [security]"]
    assert report["findings"] == [
        {
            "file": "x.py",
            "line": 3,
            "severity": "P1",
            "confidence": "high",
            "verdict": "FINDING",
            "source": "Sol [performance]",
        }
    ]


# === (e) _json_exit_code ====================================================
def test_json_exit_code_zero_when_reported_and_clean():
    report = _build_json_report([_usable("m1", _fenced({"findings": []}))])
    assert _json_exit_code(report) == 0


def test_json_exit_code_one_when_finding_present():
    payload = _fenced(
        {"findings": [{"verdict": "FINDING", "severity": "P2", "confidence": "medium"}]}
    )
    report = _build_json_report([_usable("m1", payload)])
    assert _json_exit_code(report) == 1


def test_json_exit_code_two_when_nothing_reported():
    report = _build_json_report([_unusable("m1"), _usable("m2", "no block")])
    assert _json_exit_code(report) == 2


def test_json_exit_code_one_when_mixed_reported_and_degraded_but_a_finding_exists():
    finder = _usable(
        "m1",
        _fenced(
            {
                "findings": [
                    {"verdict": "FINDING", "severity": "P0", "confidence": "high"}
                ]
            }
        ),
    )
    report = _build_json_report([finder, _unusable("m2")])
    assert report["summary"]["reported"] == ["m1"]
    assert report["summary"]["degraded"] == ["m2"]
    assert _json_exit_code(report) == 1


# === (f) _with_json_findings_request ========================================
def test_with_json_findings_request_is_additive_suffix():
    base = "Review this diff."
    result = _with_json_findings_request(base)
    assert result.startswith(base)
    assert result != base
    assert _JSON_FINDINGS_FENCE_TAG in result
    assert "findings" in result


# === (g) mode_review end-to-end, flat path ==================================
def test_mode_review_json_output_flat_path_clean_pass_exit_0():
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return _usable(model, _fenced({"findings": []}))

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(
                ["m1"],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                json_output=True,
            )
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 0, rc
    payload = json.loads(out.getvalue())
    assert payload == {
        "findings": [],
        "summary": {"expected": ["m1"], "reported": ["m1"], "degraded": []},
    }


def test_mode_review_json_output_flat_path_finding_present_exit_1():
    finding = {
        "file": "reviewlib/x.py",
        "line": 10,
        "severity": "P1",
        "confidence": "high",
        "verdict": "FINDING",
    }

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return _usable(model, _fenced({"findings": [finding]}))

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(
                ["m1"],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                json_output=True,
            )
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 1, rc
    payload = json.loads(out.getvalue())
    assert payload["findings"] == [{**finding, "source": "m1"}]
    assert payload["summary"]["reported"] == ["m1"]


def test_mode_review_json_output_flat_path_all_degraded_exit_2():
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return _unusable(model)

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(
                ["m1"],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                json_output=True,
            )
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 2, rc
    payload = json.loads(out.getvalue())
    assert payload["summary"]["reported"] == []
    assert payload["summary"]["degraded"] == ["m1"]
    assert payload["findings"] == []


def test_mode_review_json_output_flat_path_mixed_seats_deterministic_order():
    finding = {
        "file": "a.py",
        "line": 1,
        "severity": "P0",
        "confidence": "high",
        "verdict": "FINDING",
    }

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        if model == "m1":
            return _usable(model, _fenced({"findings": [finding]}))
        return _unusable(model)

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(
                ["m1", "m2"],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                json_output=True,
            )
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 1, rc
    payload = json.loads(out.getvalue())
    # Deterministic model-list order (not thread-completion order) -- built from
    # `by_model[model] for model in models`, see _build_json_report's call site.
    assert payload["summary"]["expected"] == ["m1", "m2"]
    assert payload["summary"]["reported"] == ["m1"]
    assert payload["summary"]["degraded"] == ["m2"]


def test_mode_review_json_output_no_diff_returns_exit_2():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mode_review(
            ["m1"], DEFAULT_PROMPT, "   ", REPO_ROOT, 5, staged=False, json_output=True
        )
    assert rc == 2, rc
    assert "No diff to review" in err.getvalue()


def test_mode_review_no_diff_without_json_flag_still_returns_exit_1():
    # Regression: the pre-existing prose contract (exit 1 for "no diff") must be
    # completely unaffected when --json is absent.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mode_review(["m1"], DEFAULT_PROMPT, "   ", REPO_ROOT, 5, staged=False)
    assert rc == 1, rc


def test_mode_review_no_json_flag_prose_output_unchanged():
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return _usable(model, "looks fine, checked: nothing to report")

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(["m1"], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, staged=False)
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 0, rc
    printed = out.getvalue()
    assert printed.startswith("## m1 [ok]")
    with_json_would_look_like = None
    try:
        with_json_would_look_like = json.loads(printed)
    except json.JSONDecodeError:
        pass
    assert with_json_would_look_like is None, "prose output must never parse as JSON"


# === (g) mode_review end-to-end, board path =================================
def test_mode_review_board_json_output_finding_present_with_degraded_seat():
    board = [
        BoardReviewer(model="fake-codex", role="correctness", display="Codex"),
        BoardReviewer(model="fake-gemini", role="security", display="Gemini"),
    ]
    finding = {
        "file": "b.py",
        "line": 5,
        "severity": "P2",
        "confidence": "medium",
        "verdict": "FINDING",
    }

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        if model == "fake-codex":
            return _usable(model, _fenced({"findings": [finding]}))
        return _unusable(model)

    old_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                board=board,
                pool_size=2,
                exact_board=True,
                json_output=True,
            )
    finally:
        panel.resolve_backend = old_resolve
    assert rc == 1, rc
    payload = json.loads(out.getvalue())
    assert payload["summary"]["reported"] == ["Codex [correctness]"]
    assert payload["summary"]["degraded"] == ["Gemini [security]"]
    assert payload["findings"] == [{**finding, "source": "Codex [correctness]"}]


# === (h) _handler end-to-end =================================================
def test_handler_json_flag_appends_findings_request_and_threads_json_output():
    captured_prompts = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        captured_prompts.append(prompt)
        return _usable(model, _fenced({"findings": []}))

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        ctx = ModeContext(
            args=argparse.Namespace(
                prompt=DEFAULT_PROMPT, staged=False, commit=False, json=True
            ),
            models=["m1"],
            diff="+x",
            cwd=REPO_ROOT,
            timeout=5,
            with_visual=lambda text: text,
            extra={},
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = review_mode._handler(ctx)
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 0, rc
    assert len(captured_prompts) == 1
    assert captured_prompts[0].startswith(DEFAULT_PROMPT)
    assert _JSON_FINDINGS_FENCE_TAG in captured_prompts[0]
    payload = json.loads(out.getvalue())
    assert payload == {
        "findings": [],
        "summary": {"expected": ["m1"], "reported": ["m1"], "degraded": []},
    }


def test_handler_without_json_flag_leaves_prompt_and_output_untouched():
    captured_prompts = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        captured_prompts.append(prompt)
        return _usable(model, "plain prose review, checked: nothing to report")

    old_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda _m: _fake_backend
    try:
        ctx = ModeContext(
            args=argparse.Namespace(prompt=DEFAULT_PROMPT, staged=False, commit=False),
            models=["m1"],
            diff="+x",
            cwd=REPO_ROOT,
            timeout=5,
            with_visual=lambda text: text,
            extra={},
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = review_mode._handler(ctx)
    finally:
        review_mode.resolve_backend = old_resolve
    assert rc == 0, rc
    assert captured_prompts == [DEFAULT_PROMPT]
    assert out.getvalue().startswith("## m1 [ok]")


# === regression tests for the multi-model self-review findings (review-cli#546) =====
def test_extract_json_findings_block_prefers_review_json_tag_over_earlier_generic_json_dict():
    # GLM/Grok review finding: an earlier, incidentally-quoted ```json dict (e.g. the
    # diff itself, or an injected decoy) must never shadow the backend's OWN
    # ```review-json block just because it parses first and is a dict.
    decoy = "```json\n" + json.dumps({"findings": []}) + "\n```"
    real = _fenced(
        {
            "findings": [
                {
                    "file": "app.py",
                    "line": 10,
                    "severity": "P0",
                    "confidence": "high",
                    "verdict": "FINDING",
                }
            ]
        }
    )
    body = "prose quoting an example:\n" + decoy + "\nreal answer:\n" + real
    result = _extract_json_findings_block(body)
    assert result["findings"][0]["severity"] == "P0"


def test_extract_json_findings_block_prefers_last_findings_bearing_review_json_block():
    # Same-tag variant of the same attack: an earlier "planted clean" review-json
    # block must not outrank the backend's real, LATER one (the prompt asks for
    # exactly one block, emitted last, once the prose review is complete).
    planted_clean = _fenced({"findings": []})
    real = _fenced({"findings": [{"verdict": "FINDING", "severity": "P0"}]})
    body = planted_clean + "\nmore prose\n" + real
    result = _extract_json_findings_block(body)
    assert result["findings"] == [{"verdict": "FINDING", "severity": "P0"}]


def test_coerce_finding_survives_json_infinity_overflow():
    # Grok review finding: json.loads accepts non-RFC Infinity/huge-float-overflow by
    # default; int(line) on those raises OverflowError, which used to escape
    # _coerce_finding's "never raise" promise and crash the whole --json run.
    assert _coerce_finding({"line": float("inf"), "verdict": "FINDING"})["line"] is None
    assert _coerce_finding({"line": 1e309, "verdict": "FINDING"})["line"] is None


def test_seat_json_findings_treats_null_findings_as_reported_clean():
    # Opus-omp review finding: a top-level `"findings": null` is a plausible
    # "nothing to report" rendering, not a shape violation -- must NOT degrade.
    findings, reported = _seat_json_findings(_usable("m1", _fenced({"findings": None})))
    assert findings == []
    assert reported is True


def test_seat_json_findings_degrades_when_every_item_fails_coercion():
    # GLM review finding: a NON-EMPTY findings array whose every item is garbage
    # (e.g. row-style tuples instead of objects) is a false clean if reported=True
    # with findings=[] -- the model was clearly trying to report something.
    payload = _fenced(
        {"findings": [["a.py", 1, "P1", "high", "FINDING"], "not a dict either"]}
    )
    findings, reported = _seat_json_findings(_usable("m1", payload))
    assert findings == []
    assert reported is False


def test_mode_review_board_json_output_stdout_stays_pure_json_under_pool_shortfall():
    # P1 review finding: _report_pool_shortfall's operator notice is deliberately
    # STDOUT (for the prose path); under --json it must move to stderr so
    # json.loads(stdout) still succeeds when the board pool comes up short.
    board = [
        BoardReviewer(model="codex", role="correctness", display="Codex"),
        BoardReviewer(model="fake-b", role="security", display="B"),
        BoardReviewer(model="fake-c", role="quality", display="C"),
    ]

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return _usable(model, _fenced({"findings": []}))

    old_resolve = panel.resolve_backend
    old_avail = review_mode.backend_available
    old_unpaid = review_mode.runtime_provider_marked_unpaid
    old_reason = review_mode.backend_unavailable_reason
    panel.resolve_backend = lambda _m: _fake_backend
    review_mode.backend_available = lambda model: model == "codex"
    review_mode.runtime_provider_marked_unpaid = lambda _m: False
    review_mode.backend_unavailable_reason = lambda model: (
        None if model == "codex" else "patched unavailable (test)"
    )
    try:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                staged=False,
                board=board,
                pool_size=3,
                json_output=True,
            )
    finally:
        panel.resolve_backend = old_resolve
        review_mode.backend_available = old_avail
        review_mode.runtime_provider_marked_unpaid = old_unpaid
        review_mode.backend_unavailable_reason = old_reason
    payload = json.loads(
        out.getvalue()
    )  # must not raise -- stdout is ONLY the envelope
    assert payload["summary"]["reported"] == ["Codex [correctness]"]
    assert "pool came up short" in err.getvalue()
    assert rc == 0, rc


if __name__ == "__main__":
    failures = []
    tests = [
        (name, fn) for name, fn in list(globals().items()) if name.startswith("test_")
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)}/{len(tests)} tests failed: {failures}")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
