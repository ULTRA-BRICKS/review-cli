"""Host-wide admission control (reviewlib.admission, review-cli#614).

Covers the admission math (boundary exactly at the headroom), the provider budget and its
429 feedback, settings precedence (env > config > default), FIFO fairness, crash-safe
recovery of a SIGKILLed lease holder, and — with REAL separate processes sharing one
registry directory — that concurrent holders never exceed the budget. Every test points
the registry at its own temp dir, so nothing touches the host's real admission state.

Runs under pytest and standalone (`python tests/test_admission.py`, as smoke.py does).
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import admission, backstop  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.retry import is_throttle  # noqa: E402

A = admission


@contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    admission.reset_for_tests()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        admission.reset_for_tests()


def _cfg(tmp: str, **overrides) -> A.Settings:
    base = dict(dir=Path(tmp), poll_seconds=0.05, learn=False)
    base.update(overrides)
    return A.Settings(**base)


def _host(cpus=10, load1=0.0, total=10_000.0, avail=10_000.0) -> A.HostSnapshot:
    return A.HostSnapshot(cpus, load1, total, avail)


# ── admission math ──────────────────────────────────────────────────────────────────────


def test_cpu_boundary_exactly_at_headroom_admits_and_one_over_refuses():
    cfg = A.Settings(headroom=0.20, min_slots=0)
    host = _host(cpus=10, load1=7.0)  # budget 8.0
    at = A.machine_verdict(cfg, host, [], est_cpu=1.0, est_mem_mb=0, now=0)
    over = A.machine_verdict(cfg, host, [], est_cpu=1.01, est_mem_mb=0, now=0)
    assert at.admit, at
    assert math.isclose(at.cpu_budget, 8.0)
    assert not over.admit and over.reason == "cpu", over


def test_memory_boundary_exactly_at_headroom():
    cfg = A.Settings(headroom=0.20, min_slots=0)
    host = _host(cpus=100, total=10_000, avail=3_000)  # used 7000, budget 8000
    assert A.machine_verdict(cfg, host, [], 0, 1000, now=0).admit
    refused = A.machine_verdict(cfg, host, [], 0, 1000.5, now=0)
    assert not refused.admit and refused.reason == "memory", refused


def test_young_runs_count_until_ramped_into_the_load_average():
    cfg = A.Settings(headroom=0.20, min_slots=0, ramp_seconds=100)
    host = _host(cpus=10, load1=4.0)
    young = [{"admitted_at": 950, "est_cpu": 3.0, "est_mem_mb": 0}]
    old = [{"admitted_at": 800, "est_cpu": 3.0, "est_mem_mb": 0}]
    # 4 + 3 (young, not yet visible in load) + 2 = 9 > 8
    assert not A.machine_verdict(cfg, host, young, 2.0, 0, now=1000).admit
    # an old run is already inside load1 -> 4 + 2 = 6 <= 8
    assert A.machine_verdict(cfg, host, old, 2.0, 0, now=1000).admit


def test_min_slots_bypass_budget_and_max_slots_cap():
    cfg = A.Settings(min_slots=1, max_slots=2)
    host = _host(cpus=1, load1=50.0)  # hopelessly over budget
    assert A.machine_verdict(cfg, host, [], 1, 1, now=0).admit  # 0 < min_slots
    one = [{"admitted_at": 0}]
    assert not A.machine_verdict(cfg, host, one, 1, 1, now=10_000).admit
    roomy = _host(cpus=100)
    two = [{"admitted_at": 0}, {"admitted_at": 0}]
    v = A.machine_verdict(cfg, roomy, two, 0, 0, now=10_000)
    assert not v.admit and "max_slots" in v.reason, v


def test_memory_pressure_above_the_limit_waits_and_at_the_limit_admits():
    cfg = A.Settings(min_slots=0, max_memory_pressure=10.0)
    roomy = dict(cpus=100, load1=0.0, total=10_000.0, avail=10_000.0)
    at = A.HostSnapshot(*roomy.values(), mem_pressure=10.0)
    over = A.HostSnapshot(*roomy.values(), mem_pressure=10.01)
    assert A.machine_verdict(cfg, at, [], 0, 0, now=0).admit
    refused = A.machine_verdict(cfg, over, [], 0, 0, now=0)
    assert not refused.admit and "memory pressure 10.0% > 10%" in refused.reason, (
        refused
    )
    # no PSI on this platform -> no pressure check
    assert A.machine_verdict(
        cfg, A.HostSnapshot(*roomy.values()), [], 0, 0, now=0
    ).admit
    # 0 turns the check off
    off = A.Settings(min_slots=0, max_memory_pressure=0)
    stalled = A.HostSnapshot(*roomy.values(), mem_pressure=95.0)
    assert A.machine_verdict(off, stalled, [], 0, 0, now=0).admit
    # min_slots runs still always start, so a stalled host keeps making progress
    floor = A.Settings(min_slots=1, max_memory_pressure=10.0)
    assert A.machine_verdict(floor, stalled, [], 0, 0, now=0).admit
    assert not A.machine_verdict(
        floor, stalled, [{"admitted_at": 0}], 0, 0, now=1e4
    ).admit


def test_full_swap_alone_never_blocks_but_short_ram_or_stalls_do():
    """Swap usage is deliberately not a signal: swap keeps stale pages for hours (on
    2026-10-15 it sat 100% full with 28 GB MemAvailable and PSI 0, and a swap-% rule
    refused every review). Admission is the only gate for reviews, so a host that is really
    short — little MemAvailable, or tasks stalling on reclaim — must still be refused."""
    cfg = A.Settings(min_slots=0, headroom=0.20, max_memory_pressure=10.0)
    # 48 GB box, 28 GB available, no stalls: admitted whatever swap looks like
    stale_swap = A.HostSnapshot(16, 5.7, 48_000.0, 28_000.0, mem_pressure=0.0)
    assert A.machine_verdict(cfg, stale_swap, [], 0.1, 1_000, now=0).admit
    # RAM really short (used 40 GB > the 38.4 GB budget): refused on memory
    short = A.HostSnapshot(16, 5.7, 48_000.0, 8_000.0, mem_pressure=0.0)
    v = A.machine_verdict(cfg, short, [], 0.1, 1_000, now=0)
    assert not v.admit and v.reason == "memory", v
    # roomy-looking MemAvailable but the kernel already stalls on reclaim: refused
    thrash = A.HostSnapshot(16, 5.7, 48_000.0, 28_000.0, mem_pressure=35.0)
    v = A.machine_verdict(cfg, thrash, [], 0.1, 1_000, now=0)
    assert not v.admit and v.reason.startswith("memory pressure"), v


def test_memory_pressure_probe_reads_psi_some_avg10():
    with tempfile.TemporaryDirectory() as d:
        psi = Path(d) / "memory"
        psi.write_text(
            "some avg10=12.34 avg60=2.21 avg300=1.30 total=275485664\n"
            "full avg10=11.17 avg60=1.95 avg300=1.18 total=231751496\n"
        )
        assert A._memory_pressure(str(psi)) == 12.34
        psi.write_text("garbage\n")
        assert A._memory_pressure(str(psi)) is None
        assert A._memory_pressure(str(Path(d) / "missing")) is None


def test_max_memory_pressure_setting_precedence_and_range():
    load = A.load_settings
    assert load(config={}, env={}).max_memory_pressure == 10.0
    cfg = {"admission": {"max_memory_pressure": 25}}
    assert load(config=cfg, env={}).max_memory_pressure == 25.0
    env = {"REVIEW_ADMISSION_MAX_MEMORY_PRESSURE": "5"}
    assert load(config=cfg, env=env).max_memory_pressure == 5.0
    bad = {"REVIEW_ADMISSION_MAX_MEMORY_PRESSURE": "150"}  # out of range -> config
    assert load(config=cfg, env=bad).max_memory_pressure == 25.0


def test_learned_seat_cost_replaces_config_after_three_runs():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, learn=True, cpu_base=0.0, mem_base_mb=0)
        root = Path(d)
        assert A.seat_cost(cfg, root).source == "config"
        root.mkdir(exist_ok=True)
        with open(root / "costs.jsonl", "w") as fh:
            for rss, cpu in ((300, 0.3), (600, 0.6), (900, 0.9)):
                fh.write(
                    json.dumps({"seats": 3, "peak_rss_mb": rss, "cpu_avg": cpu}) + "\n"
                )
        cost = A.seat_cost(cfg, root)
        assert cost.source.startswith("learned"), cost
        assert math.isclose(cost.mem_mb, 300) and math.isclose(cost.cpu, 0.3), cost
        cpu, mem = A.run_estimate(cfg, cost, seats=2)
        assert math.isclose(cpu, 0.6) and math.isclose(mem, 600)


# ── provider budget ─────────────────────────────────────────────────────────────────────


def test_provider_limit_applies_headroom_with_floor_of_one():
    cfg = A.Settings(
        headroom=0.2,
        providers={
            "zai": A.ProviderLimit(1),
            "free": A.ProviderLimit(0),
            "x": A.ProviderLimit(5, rpm=10),
        },
    )
    assert A.configured_limit(cfg, "anthropic") == 8  # default 10 * 0.8
    assert A.configured_limit(cfg, "zai") == 1
    assert A.configured_limit(cfg, "free") == math.inf
    assert A.configured_limit(cfg, "x") == 4
    assert A.rpm_limit(cfg, "x") == 8
    assert A.rpm_limit(cfg, "anthropic") == math.inf


def test_throttle_halves_limit_until_cooldown_expires():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, throttle_backoff=0.5, throttle_cooldown_seconds=300)
        assert A.record_throttle("anthropic", cfg=cfg, now=1000) == 4
        assert A.record_throttle("anthropic", cfg=cfg, now=1010) == 2
        root = Path(d)

        def limit_at(t):
            state = A._prune_provider_state(A._load_provider_state(root), cfg, t)
            return A.effective_limit(cfg, "anthropic", state)

        assert limit_at(1020) == 2
        assert limit_at(1305) == 2  # first event expired, second still active
        assert limit_at(1311) == 8  # both expired -> configured limit again
        assert limit_at(1020) == 2 and A.effective_limit(cfg, "zai", {}) == 8


def test_is_throttle_only_for_provider_pushback():
    assert is_throttle(ReviewResult("m", "c", 1, "", "HTTP 429 Too Many Requests"))
    assert is_throttle(ReviewResult("m", "c", 1, "", "Error: 529 overloaded_error"))
    assert not is_throttle(ReviewResult("m", "c", 124, "", "timed out"))
    assert not is_throttle(ReviewResult("m", "c", 1, "", "Connection reset by peer"))
    assert not is_throttle(
        ReviewResult("m", "c", 1, "", "429 insufficient credit balance")
    )
    usable = ReviewResult(
        "m", "c", 0, "No findings. The diff looks fine overall. " * 20, ""
    )
    assert not is_throttle(usable)


def test_is_throttle_reads_http_status_from_returncode():
    """Gemini / Claude API results carry the HTTP status in returncode, often with an
    empty body; a 429/529 there is throttling unless the text says quota/billing."""
    assert is_throttle(ReviewResult("gemini", "POST", 429, "", ""))
    assert is_throttle(ReviewResult("claude-api", "POST", 529, "", ""))
    assert not is_throttle(
        ReviewResult("gemini", "POST", 429, "", "insufficient credit balance")
    )
    assert not is_throttle(ReviewResult("gemini", "POST", 500, "", ""))


def test_call_backend_caps_its_timeout_to_the_command_deadline():
    """Queue time counts against `--timeout N`: the call gets only what is left of N."""
    from reviewlib import backends, process

    seen = []

    def backend(model, prompt, diff, cwd, timeout, round_no):
        seen.append(timeout)
        return ReviewResult(model, "c", 0, "ok", "")

    with (
        tempfile.TemporaryDirectory() as d,
        _env(REVIEW_ADMISSION_DIR=d, REVIEW_ADMISSION="0"),
    ):
        process.set_run_deadline(time.monotonic() + 10.2)
        try:
            backends.call_backend(backend, "gemini", "p", "", Path(d), 600)
        finally:
            process.set_run_deadline(None)
        backends.call_backend(backend, "gemini", "p", "", Path(d), 600)
    assert seen == [11, 600], seen


def test_note_result_feeds_429_back_into_the_provider_limit():
    with tempfile.TemporaryDirectory() as d:
        with _env(
            REVIEW_ADMISSION_DIR=d,
            REVIEW_ADMISSION="1",
            REVIEW_ADMISSION_PROVIDERS="zai=10",
        ):
            A.note_result(
                "oc:zai/glm-5.3", ReviewResult("m", "c", 1, "", "rate limit exceeded")
            )
            state = A._load_provider_state(Path(d))
            assert (
                A.effective_limit(A.settings(), "zai", state) == 4
            )  # floor(10*.8) halved


def test_provider_key_maps_transports_to_vendors():
    cases = {
        "claude:claude-opus-5-5": "anthropic",
        "omp:anthropic/claude-sonnet-5-5": "anthropic",
        "oc:anthropic/claude-opus-5-5": "anthropic",
        "fable": "anthropic",
        "codex:gpt-6-luna": "openai",
        "omp:openai-codex/gpt-6-luna": "openai",
        "oc:zai/glm-5.3": "zai",
        "zai:glm-5.3": "zai",
        "glm": "zai",
        "oc:commandcode/moonshotai/Kimi-K3": "commandcode",
        "omp:opencode-go/mimo-v2.6-pro": "opencode-go",
        "gemini": "google",
    }
    with _env(ANTHROPIC_BASE_URL=None):
        for model, want in cases.items():
            assert A.provider_key(model) == want, (model, A.provider_key(model))


def test_claude_through_a_gateway_spends_the_gateway_budget():
    with _env(ANTHROPIC_BASE_URL="https://api.commandcode.ai/provider"):
        assert A.provider_key("claude:claude-opus-5-5") == "commandcode"
        assert A.provider_key("claude") == "commandcode"
        assert A.provider_key("omp:anthropic/claude-sonnet-5-5") == "anthropic"


def test_vision_denied_at_the_command_deadline_is_a_timeout():
    """A vision seat refused because `just-ask --timeout N` passed while it queued must
    surface as timed_out (exit 124), not a generic unavailable backend (review-cli#614)."""
    from reviewlib import process
    from reviewlib.features.visual import vision_client as vc

    @contextmanager
    def denied(_model, **_kw):
        yield "--timeout reached while waiting for a openai slot"

    real = A.seat_slot
    A.seat_slot = denied
    process.set_run_deadline(time.monotonic() - 1)
    try:
        with _env(REVIEW_UNPAID_PROVIDERS=None):
            verdict = vc.call_ai_vision("codex:gpt-5", blocks=[])
    finally:
        process.set_run_deadline(None)
        A.seat_slot = real
    assert verdict.timed_out and verdict.verdict is None, verdict


def test_resumed_brainstorm_takes_a_host_slot():
    from reviewlib import cli, sessions

    sess = sessions.Session(
        session_id="abc123",
        path=Path("/nonexistent"),
        topic="t",
        panel=["codex:gpt-5", "oc:zai/glm-5.3"],
        moderator="",
        min_rounds=1,
        max_rounds=2,
    )
    entered: list[tuple[str, list[str]]] = []
    inside: list[bool] = []

    @contextmanager
    def slot(*, mode, models, **_kw):
        entered.append((mode, list(models)))
        inside.append(True)
        yield
        inside.pop()

    real = (A.run_slot, sessions.find_session, sessions.resume_session)
    A.run_slot = slot
    sessions.find_session = lambda _id: sess
    sessions.resume_session = lambda *_a, **_k: 0 if inside else 99
    try:
        rc = cli.main(["sessions", "--resume", "abc123", "-m", "codex:gpt-5"])
    finally:
        A.run_slot, sessions.find_session, sessions.resume_session = real
    assert rc == 0 and entered and entered[0][0] == "brainstorm", (rc, entered)


def test_saturated_provider_seat_fails_over_after_wait_cap():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(
            d,
            headroom=0.0,
            providers={"zai": A.ProviderLimit(1)},
            provider_wait_seconds=1,
        )
        held = threading.Event()
        release = threading.Event()

        def holder():
            with A.seat_slot(
                "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
            ) as denied:
                assert denied is None
                held.set()
                release.wait(10)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(5)
        started = time.monotonic()
        with A.seat_slot(
            "oc:zai/glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
        ) as denied:
            waited = time.monotonic() - started
        release.set()
        t.join(5)
        assert denied and "saturated" in denied, denied
        assert 0.9 <= waited < 5, waited
        # once the holder is gone the provider admits again
        with A.seat_slot(
            "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
        ) as denied:
            assert denied is None


def test_registry_os_error_mid_wait_fails_open_instead_of_crashing():
    """A plain OSError (EMFILE, registry dir removed) while a seat waits must fail open
    like RegistryBusy does, not escape seat_slot and crash the run (review-cli#614)."""
    import errno

    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, headroom=0.0, providers={"zai": A.ProviderLimit(1)})
        held = threading.Event()
        release = threading.Event()

        def holder():
            with A.seat_slot("zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")):
                held.set()
                release.wait(10)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(5)
        real = A._acquire_registry
        broken = threading.Event()

        def flaky(root, cfg):
            if broken.is_set():
                raise OSError(errno.EMFILE, "Too many open files")
            return real(root, cfg)

        A._acquire_registry = flaky
        timer = threading.Timer(0.5, broken.set)
        timer.start()
        try:
            with A.seat_slot(
                "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
            ) as denied:
                assert denied is None
        finally:
            A._acquire_registry = real
            timer.cancel()
            release.set()
            t.join(5)


def test_seat_queued_past_the_command_deadline_never_starts():
    """Deadline at 0.5s, the provider frees up at 1s: the waiting seat must be denied at
    the deadline, not admitted once the slot frees (review-cli#614)."""
    from reviewlib import process

    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, headroom=0.0, providers={"zai": A.ProviderLimit(1)})
        held = threading.Event()

        def holder():
            with A.seat_slot("zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")):
                held.set()
                time.sleep(1.0)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(5)
        started = time.monotonic()
        process.set_run_deadline(started + 0.5)
        try:
            with A.seat_slot(
                "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
            ) as denied:
                waited = time.monotonic() - started
        finally:
            process.set_run_deadline(None)
            t.join(5)
        assert denied and "--timeout reached" in denied, denied
        assert 0.45 <= waited < 0.9, waited


def test_qa_tester_without_provider_capacity_is_blocked_not_failed():
    from reviewlib.qa import executor

    @contextmanager
    def saturated(_seat, **_kw):
        yield "provider openai saturated for 5m (8/8 seats busy) — failing over"

    real = A.seat_slot
    A.seat_slot = saturated
    try:
        with _env(REVIEW_QA_FAKE_TESTER=None):
            proc = executor._dispatch_tester("codex", "p", Path("."), 60, "gpt-5")
        outcome = executor._build_outcome(proc, backend="codex", wall=0.0)
    finally:
        A.seat_slot = real
    assert outcome.verdict == executor.VERDICT_BLOCKED, outcome
    assert "saturated" in outcome.transcript


def test_rpm_cap_holds_starts_per_minute():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(
            d,
            headroom=0.0,
            providers={"p": A.ProviderLimit(0, rpm=2)},
            provider_wait_seconds=1,
        )
        sink = open(os.devnull, "w")
        for _ in range(2):
            with A.seat_slot("p:m", cfg=cfg, stream=sink) as denied:
                assert denied is None
        with A.seat_slot("p:m", cfg=cfg, stream=sink) as denied:
            assert denied and "saturated" in denied, denied


# ── settings ────────────────────────────────────────────────────────────────────────────


def test_run_waits_until_its_providers_have_room_for_its_seats():
    cfg = A.Settings(
        headroom=0.2, min_slots=1, providers={"anthropic": A.ProviderLimit(5)}
    )
    need = A.seats_by_provider(
        ["claude:claude-opus-5-5", "omp:anthropic/claude-sonnet-5-5", "zai:glm"]
    )
    assert need == {"anthropic": 2, "zai": 1}
    running = [{"provider_seats": {"anthropic": 2}}]  # limit floor(5*.8) = 4
    assert A.provider_verdict(cfg, {}, running, need) == ""  # 2 + 2 <= 4
    running.append({"provider_seats": {"anthropic": 1}})
    assert "provider anthropic full (3/4" in A.provider_verdict(cfg, {}, running, need)
    # a run needing more than the whole limit reserves the limit instead of waiting forever
    assert A.provider_verdict(cfg, {}, [{"provider_seats": {}}], {"anthropic": 9}) == ""
    # throttled provider: committed seats are checked against the LOWERED limit
    throttled = {"anthropic": {"throttles": [{"ts": 0, "limit": 2}], "starts": []}}
    assert "full" in A.provider_verdict(
        cfg, throttled, [{"provider_seats": {"anthropic": 1}}], need
    )
    # below min_slots nothing blocks
    assert A.provider_verdict(cfg, throttled, [], {"anthropic": 99}) == ""


def test_help_config_documents_every_admission_setting():
    from reviewlib.cli import _help_topic_config

    text = _help_topic_config()
    assert "HOST-WIDE ADMISSION" in text
    for key, (env_name, _parse, _valid) in A._FIELDS.items():
        assert key in text and env_name in text, (key, env_name)
    for env_name in (A.ENV_PROVIDERS, A.ENV_DIR):
        assert env_name in text, env_name


def test_settings_precedence_env_over_config_over_default():
    config = {
        "admission": {
            "headroom": 0.3,
            "max_slots": 5,
            "poll_seconds": 2,
            "providers": {"anthropic": {"max_concurrent": 20, "rpm": 100}, "zai": 3},
        }
    }
    env = {
        "REVIEW_ADMISSION_HEADROOM": "0.1",
        "REVIEW_ADMISSION_MAX_SLOTS": "not-a-number",  # invalid -> config value wins
        "REVIEW_ADMISSION_PROVIDERS": "zai=6/30,command-code=2",
        "REVIEW_ADMISSION_DIR": "/tmp/x-adm",
    }
    s = A.load_settings(config=config, env=env)
    assert s.headroom == 0.1  # env > config
    assert s.max_slots == 5  # config (env invalid)
    assert s.poll_seconds == 2.0  # config
    assert s.min_slots == 1  # default
    assert s.provider_limit("anthropic") == A.ProviderLimit(20, 100)
    assert s.provider_limit("zai") == A.ProviderLimit(
        6, 30
    )  # env entry overrides config
    assert s.provider_limit("commandcode") == A.ProviderLimit(
        2, 0
    )  # alias canonicalized
    assert s.dir == Path("/tmp/x-adm")
    d = A.load_settings(config={}, env={})
    assert (d.headroom, d.enabled, d.max_slots) == (0.20, True, 0)
    assert not A.load_settings(config={"admission": {"enabled": False}}, env={}).enabled
    assert A.load_settings(
        config={"admission": {"enabled": False}}, env={"REVIEW_ADMISSION": "1"}
    ).enabled
    assert (
        A.load_settings(config={"admission": {"headroom": 1.5}}, env={}).headroom
        == 0.20
    )


# ── backstop exemption ──────────────────────────────────────────────────────────────────


def test_admission_wait_does_not_count_against_the_run_backstop():
    fired: list[int] = []
    real = backstop._fire
    backstop._fire = lambda secs, out, announce=None: fired.append(secs)
    try:
        with backstop.run_backstop(1):
            with backstop.exempt_from_backstop():
                time.sleep(0.6)
            time.sleep(0.8)  # 1.4s wall, 0.8s counted
        time.sleep(0.4)
        assert fired == [], fired
        with backstop.run_backstop(1):
            time.sleep(1.4)
        assert fired == [1], fired
        fired.clear()
        with backstop.run_backstop(1, exempt_waits=False):  # --timeout N: wall clock
            with backstop.exempt_from_backstop():
                time.sleep(1.3)
        assert fired == [1], fired
    finally:
        backstop._fire = real


def test_overlapping_waits_are_credited_once():
    """Two seats waiting in parallel for 0.5s exempt 0.5s, not 1.0s: with 1.2s of real
    work after them the 1s backstop must still fire."""
    fired: list[int] = []
    real = backstop._fire
    backstop._fire = lambda secs, out, announce=None: fired.append(secs)
    try:
        with backstop.run_backstop(1):
            waiters = [
                threading.Thread(target=lambda: _exempt_sleep(0.5)) for _ in range(2)
            ]
            for t in waiters:
                t.start()
            for t in waiters:
                t.join()
            time.sleep(1.2)
        assert fired == [1], fired
    finally:
        backstop._fire = real


def _exempt_sleep(seconds: float) -> None:
    with backstop.exempt_from_backstop():
        time.sleep(seconds)


def test_wait_spanning_the_deadline_is_credited_once():
    """Budget 1s; 0.5s of work, a 1s wait across the original deadline, then work: the
    watchdog must fire at ~2.0s (1s budget + 1s wait), not later (review-cli#614)."""
    fired: list[float] = []
    real = backstop._fire
    recheck = backstop._EXEMPT_RECHECK_SECONDS
    backstop._EXEMPT_RECHECK_SECONDS = 0.1
    start = time.monotonic()
    backstop._fire = lambda secs, out, announce=None: fired.append(
        time.monotonic() - start
    )
    try:
        with backstop.run_backstop(1):
            time.sleep(0.5)
            _exempt_sleep(1.0)
            time.sleep(1.2)
        assert len(fired) == 1 and 1.9 <= fired[0] < 2.3, fired
    finally:
        backstop._fire = real
        backstop._EXEMPT_RECHECK_SECONDS = recheck


def test_backend_alias_spellings_share_one_provider_budget():
    for model, want in {
        "gemini-api:gemini-2.5-flash": "google",
        "common-code:x": "commandcode",
        "common_code:x": "commandcode",
        "z.ai:glm-5.3": "zai",
    }.items():
        assert A.provider_key(model) == want, (model, A.provider_key(model))


def test_slot_held_for_companion_vision_grows_for_the_panel_and_ends_with_dispatch():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        sink = open(os.devnull, "w")
        with _env(
            REVIEW_ADMISSION="1", REVIEW_ADMISSION_DIR=d, REVIEW_ADMISSION_LEARN="0"
        ):
            with A.dispatch_scope():
                A.hold_run_slot(mode="review", models=["claude:claude-opus-5-5"])
                leases = list(Path(d).glob("run-*.json"))
                assert len(leases) == 1
                with A.run_slot(
                    mode="review",
                    models=["claude:claude-opus-5-5", "oc:zai/glm-5.3", "codex:gpt"],
                    cfg=cfg,
                    stream=sink,
                ):
                    data = json.loads(leases[0].read_text())
                    assert data["state"] == "running" and data["seats"] == 3, data
                    assert data["provider_seats"] == {
                        "anthropic": 1,
                        "zai": 1,
                        "openai": 1,
                    }
                assert leases[0].exists()  # still held until the dispatch ends
            assert not list(Path(d).glob("run-*")), list(Path(d).iterdir())


# ── cross-process ───────────────────────────────────────────────────────────────────────

_CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    sys.path.insert(0, {root!r})
    from reviewlib import admission
    log, name, hold, kind = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    cfg = admission.load_settings(config={{}})
    host = os.environ.get("FAKE_HOST")
    probe = admission.probe_host
    if host:
        h = json.loads(host)
        probe = lambda: admission.HostSnapshot(h["cpus"], h["load1"], h["total"], h["avail"])
    def note(event):
        with open(log, "a") as fh:
            fh.write(f"{{event}} {{name}} {{time.time():.6f}}\\n")
    if kind == "run":
        with admission.run_slot(mode="review", models=["fake"], cfg=cfg, probe=probe):
            note("start"); time.sleep(hold); note("end")
    elif kind.startswith("companion"):
        # companion<N>: a 1-seat vision call, then an N-seat text panel in the same run
        panel = ["fake"] * int(kind[len("companion"):])
        with admission.run_slot(
            mode="review", models=["fake"], later_models=panel, cfg=cfg, probe=probe
        ):
            note("start"); time.sleep(hold); note("end")
    else:
        with admission.seat_slot(kind, cfg=cfg) as denied:
            assert denied is None, denied
            note("start"); time.sleep(hold); note("end")
    """
).format(root=str(REPO_ROOT))


def _spawn(tmp: str, log: Path, name: str, hold: float, kind: str = "run", **env):
    script = Path(tmp) / "child.py"
    if not script.exists():
        script.write_text(_CHILD)
    full = dict(os.environ)
    full.update(
        REVIEW_ADMISSION="1",
        REVIEW_ADMISSION_DIR=str(Path(tmp) / "reg"),
        REVIEW_ADMISSION_POLL_SECONDS="0.05",
        REVIEW_ADMISSION_LEARN="0",
    )
    full.update({k: str(v) for k, v in env.items()})
    return subprocess.Popen(
        [sys.executable, str(script), str(log), name, str(hold), kind],
        env=full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _events(log: Path) -> list[tuple[float, str, str]]:
    out = []
    for line in log.read_text().splitlines():
        event, name, ts = line.split()
        out.append((float(ts), event, name))
    return sorted(out)


def _peak(events) -> int:
    live = peak = 0
    for _ts, event, _name in events:
        live += 1 if event == "start" else -1
        peak = max(peak, live)
    return peak


def _wait_all(procs, timeout=60):
    for p in procs:
        out, err = p.communicate(timeout=timeout)
        assert p.returncode == 0, (p.returncode, out, err)


def test_concurrent_processes_never_exceed_machine_budget():
    """8 processes, each estimated at 2 cpu, on a fake 10-cpu idle host with 20% headroom
    (8 cpu budget): at most 4 may run at once — and 4 do."""
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        host = json.dumps({"cpus": 10, "load1": 0.0, "total": 1e6, "avail": 1e6})
        procs = [
            _spawn(
                d,
                log,
                f"r{i}",
                1.0,
                FAKE_HOST=host,
                REVIEW_ADMISSION_MIN_SLOTS="0",
                REVIEW_ADMISSION_CPU_PER_SEAT="2",
                REVIEW_ADMISSION_CPU_BASE="0",
                REVIEW_ADMISSION_RAMP_SECONDS="600",
            )
            for i in range(8)
        ]
        _wait_all(procs)
        events = _events(log)
        assert len(events) == 16
        assert _peak(events) == 4, events


def test_concurrent_processes_never_exceed_provider_limit():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        procs = [
            _spawn(
                d,
                log,
                f"s{i}",
                0.6,
                kind="zai:glm-5.3",
                REVIEW_ADMISSION_PROVIDERS="zai=3",
            )
            for i in range(6)
        ]
        _wait_all(procs)
        events = _events(log)
        assert len(events) == 12
        assert _peak(events) == 2, events  # floor(3 * 0.8)


def test_waiters_are_admitted_fifo():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        env = dict(REVIEW_ADMISSION_MAX_SLOTS="1")
        first = _spawn(d, log, "a", 1.5, **env)
        while not log.exists():
            time.sleep(0.02)
        later = []
        for name in ("b", "c", "d"):
            later.append(_spawn(d, log, name, 0.1, **env))
            time.sleep(0.3)  # enqueue strictly in this order
        _wait_all([first, *later])
        starts = [name for _ts, event, name in _events(log) if event == "start"]
        assert starts == ["a", "b", "c", "d"], starts


def test_admitted_runs_record_queue_wait_and_review_jobs_summarizes_it():
    """Every admitted run appends its queue wait and the reviews running once it started to
    admissions.jsonl; the status block summarizes the last 24h (review-cli#633)."""
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        env = dict(
            REVIEW_ADMISSION_MAX_SLOTS="2",
            FAKE_HOST=json.dumps(
                {"cpus": 64, "load1": 0.0, "total": 1e6, "avail": 1e6}
            ),
        )
        # unequal holds: a ends first and c takes its slot while b is still running
        first = [
            _spawn(d, log, name, hold, **env) for name, hold in (("a", 2.5), ("b", 6.0))
        ]
        while not log.exists() or len(log.read_text().splitlines()) < 2:
            time.sleep(0.02)
        third = _spawn(d, log, "c", 0.1, **env)  # both slots taken: c queues ~2 s
        _wait_all([*first, third])
        reg = Path(d) / "reg"
        ledger = [
            json.loads(line)
            for line in (reg / "admissions.jsonl").read_text().splitlines()
        ]
        assert [r["running"] for r in ledger] == [1, 2, 2], ledger
        assert [r["queued_seconds"] >= 1.0 for r in ledger] == [False, False, True], (
            ledger
        )
        assert all(r["mode"] == "review" and r["seats"] == 1 for r in ledger), ledger
        # an old record, garbled lines and non-finite numbers never count and never
        # break the summary (json.loads accepts NaN/Infinity)
        now = time.time()
        with open(reg / "admissions.jsonl", "a") as fh:
            fh.write(
                '{"ts": 0, "queued_seconds": 999, "running": 9}\nnot json\n{"ts": "x"}\n'
                f'{{"ts": {now}, "queued_seconds": Infinity, "running": 1}}\n'
                f'{{"ts": {now}, "queued_seconds": NaN, "running": 1}}\n'
                f'{{"ts": {now}, "queued_seconds": 1, "running": Infinity}}\n'
                f'{{"ts": {now + 3600}, "queued_seconds": 500, "running": 7}}\n'
            )
        snap = A.status_snapshot(_cfg(str(reg)), probe=lambda: _host())
        recent = snap["recent"]
        assert recent["admitted"] == 3 and recent["queued"] == 1, recent
        assert recent["peak_running"] == 2, recent
        assert recent["wait_max_seconds"] >= 1.0 > recent["wait_p50_seconds"], recent
        text = A.render_status(snap)
        assert "last 24h: 3 admitted, 1 queued (wait p50 0s" in text, text
        assert "peak 2 running at once" in text, text


def test_ledger_is_count_capped_and_the_summary_says_when_it_was_trimmed():
    """The ledger keeps a bounded number of records (constant cost per admission under
    registry.lock); a window it no longer fully covers is reported, never passed off as
    the whole 24h."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "costs.jsonl"
        for ts in (0, 100, 100, 100, 100):  # 5 lines > 2 x keep triggers a trim
            A._append_ledger(path, {"ts": ts}, keep=2)
        assert [i["ts"] for i in A._load_jsonl(path)] == [100, 100]
        now = time.time()
        rec = lambda ts: {"ts": ts, "queued_seconds": 0, "running": 1}  # noqa: E731
        saved = A._ADMISSIONS_KEEP
        A._ADMISSIONS_KEEP = 3
        try:
            ledger = Path(d) / "admissions.jsonl"
            ledger.write_text(
                "".join(json.dumps(rec(now - 10)) + "\n" for _ in range(3))
            )
            trimmed = A.recent_admissions(Path(d), now)
            assert trimmed["admitted"] == 3 and trimmed["complete"] is False, trimmed
            snap = {"enabled": True, "headroom": 0.2, "dir": d, "recent": trimmed}
            snap["host"] = {"cpus": 1, "load1": 0, "cpu_budget": 0.8}
            assert "ledger trimmed" in A.render_status(snap)
            with open(
                ledger, "a"
            ) as fh:  # a record older than the window: fully covered
                fh.write(json.dumps(rec(now - 2 * 24 * 3600)) + "\n")
            assert A.recent_admissions(Path(d), now)["complete"] is True
        finally:
            A._ADMISSIONS_KEEP = saved


def test_status_shows_memory_pressure_against_its_limit():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, max_memory_pressure=10.0)
        host = A.HostSnapshot(16, 1.0, 48_000.0, 20_000.0, mem_pressure=1.5)
        snap = A.status_snapshot(cfg, probe=lambda: host)
        assert snap["host"]["mem_pressure"] == 1.5
        assert snap["host"]["max_memory_pressure"] == 10.0
        assert "memory pressure 1.5%/10%" in A.render_status(snap)
        assert snap["recent"] == {
            "window_hours": 24.0,
            "admitted": 0,
            "queued": 0,
            "peak_running": 0,
            "complete": True,
        }, snap["recent"]
        # the documented `recent` key is there even before any run made the registry dir,
        # and with admission switched off
        empty = snap["recent"]
        missing = A.status_snapshot(_cfg(str(Path(d) / "none")), probe=lambda: host)
        assert missing["recent"] == empty, missing
        off = A.status_snapshot(_cfg(d, enabled=False), probe=lambda: host)
        assert off["recent"] == empty, off


def test_review_diff_runs_queue_for_a_host_slot_end_to_end():
    """Two real `review diff` processes (fake backend) with one host slot: the second
    queues (stderr says so) and starts only after the first finished; both succeed."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            [
                "git",
                "-c",
                "user.email=t@e",
                "-c",
                "user.name=T",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "i",
            ],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        (repo / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        env = dict(
            os.environ,
            REVIEW_FAKE_BACKEND="1",
            REVIEW_FAKE_DELAY="1.5",
            REVIEW_LOG_DIR=str(tmp / "logs"),
            REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
            REVIEW_JOBS_DIR=str(tmp / "jobs"),
            REVIEW_TASK_CODE="TEST-614",
            REVIEW_ADMISSION="1",
            REVIEW_ADMISSION_DIR=str(tmp / "reg"),
            REVIEW_ADMISSION_MAX_SLOTS="1",
            REVIEW_ADMISSION_POLL_SECONDS="0.1",
        )
        args = [
            sys.executable,
            str(REPO_ROOT / "bin" / "review"),
            "diff",
            "--staged",
            "-C",
            str(repo),
            "-m",
            "claude:claude-opus-5-5",
            "--no-tui",
        ]
        first = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not list(
            (tmp / "reg").glob("seat-*.json")
        ):
            time.sleep(0.05)
        assert list((tmp / "reg").glob("seat-*.json")), (
            "first run never took a provider seat"
        )
        second = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        _o1, e1 = first.communicate(timeout=120)
        _o2, e2 = second.communicate(timeout=120)
        assert first.returncode == 0, e1
        assert second.returncode == 0, e2
        assert "admission: waiting — queue position 1/1" in e2, e2
        assert "admission: waiting" not in e1, e1
        assert not list((tmp / "reg").glob("run-*")) and not list(
            (tmp / "reg").glob("seat-*")
        )


def test_queue_line_reports_position_and_eta():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        env = dict(REVIEW_ADMISSION_MAX_SLOTS="1")
        first = _spawn(d, log, "a", 1.0, **env)
        while not log.exists():
            time.sleep(0.02)
        second = _spawn(d, log, "b", 0.1, **env)
        first.communicate(timeout=30)
        _out, err = second.communicate(timeout=30)
        assert second.returncode == 0, err
        assert "queue position 1/1" in err and "ETA ~" in err, err
        assert "admitted after" in err, err


def test_sigkilled_holder_is_reclaimed():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        holder = _spawn(d, log, "dead", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        while not log.exists():
            time.sleep(0.02)
        holder.send_signal(signal.SIGKILL)
        holder.wait(10)
        reg = Path(d) / "reg"
        assert list(reg.glob("run-*.json")), (
            "the killed holder's lease is still on disk"
        )
        cfg = _cfg(str(reg), max_slots=1)
        started = time.monotonic()
        with A.run_slot(
            mode="review", models=["fake"], cfg=cfg, stream=open(os.devnull, "w")
        ):
            waited = time.monotonic() - started
            live = [p.name for p in reg.glob("run-*.json")]
        assert waited < 2, waited
        assert len(live) == 1 and str(os.getpid()) in live[0], live
        assert not list(reg.glob("run-*")), list(reg.iterdir())


def test_stale_heartbeat_lease_stops_counting():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, max_slots=1, stale_lease_seconds=5)
        root = Path(d)
        with A._registry_locked(root):
            wedged = A.Lease(
                root, "run", A._next_seq(root), {"state": "running", "admitted_at": 0}
            )
        data = json.loads(wedged.json_path.read_text())
        data["heartbeat"] = time.time() - 60  # owner alive (lock held) but frozen
        wedged.json_path.write_text(json.dumps(data))
        try:
            started = time.monotonic()
            with A.run_slot(
                mode="review", models=["fake"], cfg=cfg, stream=open(os.devnull, "w")
            ):
                pass
            assert time.monotonic() - started < 2
            snap = A.status_snapshot(cfg, probe=lambda: _host())
            assert [r["state"] for r in snap["runs"]] == ["stale"], snap["runs"]
        finally:
            wedged.release()


def test_review_jobs_shows_running_and_waiting_slots():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        env = dict(REVIEW_ADMISSION_MAX_SLOTS="1")
        first = _spawn(d, log, "a", 2.0, **env)
        while not log.exists():
            time.sleep(0.02)
        second = _spawn(d, log, "b", 0.1, **env)
        time.sleep(0.5)
        full = dict(
            os.environ, REVIEW_ADMISSION="1", REVIEW_ADMISSION_DIR=str(Path(d) / "reg")
        )
        full["REVIEW_JOBS_DIR"] = str(Path(d) / "jobs")
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "review"),
                "jobs",
                "--slots",
                "--json",
            ],
            env=full,
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "review"), "jobs"],
            env=full,
            capture_output=True,
            text=True,
            timeout=30,
        )
        _wait_all([first, second])
        assert proc.returncode == 0, proc.stderr
        states = sorted(r["state"] for r in json.loads(proc.stdout)["runs"])
        assert states == ["running", "waiting"], proc.stdout
        assert "1 running, 1 waiting" in text.stdout, text.stdout
        assert "waiting #1" in text.stdout, text.stdout


def _companion_lease(reg: Path, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in reg.glob("run-*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("state") == "waiting":
                return data
        time.sleep(0.05)
    raise AssertionError(f"no waiting run lease in {reg}")


def test_companion_run_is_admitted_only_when_the_full_panel_fits():
    """A companion run's 1-seat vision call must not get it admitted: the 3-seat panel
    that follows is what the host budget is checked against (review-cli#614 P1)."""
    host = json.dumps({"cpus": 10, "load1": 0.0, "total": 100_000, "avail": 100_000})
    env = dict(
        FAKE_HOST=host,  # 10 cpus, 20% headroom -> 8 cpu budget
        REVIEW_ADMISSION_MIN_SLOTS="0",
        REVIEW_ADMISSION_CPU_PER_SEAT="3",
        REVIEW_ADMISSION_CPU_BASE="0",
    )
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.log"
        big = _spawn(d, log, "big", 0.1, "companion3", **env)  # 9 cpu > 8
        try:
            lease = _companion_lease(Path(d) / "reg")
            assert lease["seats"] == 3, lease
            time.sleep(1.0)
            assert big.poll() is None and not log.exists(), "admitted on 1 seat"
        finally:
            big.kill()
            big.communicate(timeout=10)
        fits = _spawn(d, log, "fits", 0.1, "companion2", **env)  # 6 cpu <= 8
        _wait_all([fits], timeout=30)
        assert [e for _ts, e, _n in _events(log)] == ["start", "end"]


def test_just_ask_visual_timeout_bounds_the_queue_wait():
    """`review just-ask --visual img --timeout N` stuck behind a full queue exits 124 at
    ~N (not N + grace, not when the holder ends), and its waiting lease already
    reserves the whole 3-seat panel, not the 1-seat vision call (review-cli#614 P1s)."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import visual_fixtures as vf

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        log = tmp / "events.log"
        holder = _spawn(d, log, "holder", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            assert log.exists(), "holder never admitted"
            env = dict(
                os.environ,
                REVIEW_FAKE_BACKEND="1",
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
                REVIEW_JOBS_DIR=str(tmp / "jobs"),
                REVIEW_ADMISSION="1",
                REVIEW_ADMISSION_DIR=str(tmp / "reg"),
                REVIEW_ADMISSION_MAX_SLOTS="1",
                REVIEW_ADMISSION_POLL_SECONDS="0.1",
                REVIEW_ADMISSION_LEARN="0",
            )
            image = vf.styled_render(tmp / "shot.png")
            started = time.monotonic()
            asker = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO_ROOT / "bin" / "review"),
                    "just-ask",
                    "Reply ok",
                    "--task",
                    "TEST-614",
                    "--visual",
                    str(image),
                    "--timeout",
                    "3",
                    "-m",
                    "claude:claude-opus-5-5,codex:gpt-5,oc:zai/glm-5.3",
                    "-C",
                    str(REPO_ROOT),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            lease = _companion_lease(tmp / "reg")
            _out, err = asker.communicate(timeout=60)
            elapsed = time.monotonic() - started
        finally:
            holder.kill()
            holder.communicate(timeout=10)
        assert lease["seats"] == 3, lease
        assert asker.returncode == 124, (asker.returncode, err)
        assert "--timeout reached" in err and "no seat was started" in err, err
        assert elapsed < 3 + 10, elapsed  # far below N + 30 s grace / the 120 s holder


def test_empty_diff_review_does_not_queue_for_a_host_slot():
    """`review diff` with nothing to review calls no model: it must report "No diff to
    review" at once, not after waiting behind a full host (review-cli#614)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        log = tmp / "events.log"
        holder = _spawn(d, log, "holder", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            assert log.exists(), "holder never admitted"
            env = dict(
                os.environ,
                REVIEW_FAKE_BACKEND="1",
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
                REVIEW_JOBS_DIR=str(tmp / "jobs"),
                REVIEW_ADMISSION="1",
                REVIEW_ADMISSION_DIR=str(tmp / "reg"),
                REVIEW_ADMISSION_MAX_SLOTS="1",
                REVIEW_ADMISSION_POLL_SECONDS="0.1",
                REVIEW_ADMISSION_LEARN="0",
            )
            try:
                reviewer = subprocess.run(
                    [
                        sys.executable,
                        str(REPO_ROOT / "bin" / "review"),
                        "diff",
                        "--staged",
                        "--task",
                        "TEST-614",
                        "--no-tui",
                        "-C",
                        str(repo),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(
                    f"empty-diff review queued for a slot: {exc}"
                ) from None
        finally:
            holder.kill()
            holder.communicate(timeout=10)
        assert "No diff to review" in reviewer.stderr, reviewer.stderr
        assert "queue position" not in reviewer.stderr, reviewer.stderr


def test_unqualified_omp_claude_selector_spends_the_anthropic_budget():
    """`omp:claude-sonnet-5-5` runs through omp's Anthropic accounts like
    `omp:anthropic/claude-sonnet-5-5`, so both spend one budget (review-cli#614)."""
    assert A.provider_key("omp:claude-sonnet-5-5") == "anthropic"
    assert A.provider_key("omp:anthropic/claude-sonnet-5-5") == "anthropic"
    assert A.provider_key("omp:kimi-code/k3") == "kimi-code"


def test_garbled_provider_state_never_crashes_admission_or_status():
    """A hand-edited or half-written providers.json is ignored entry by entry instead of
    crashing every seat, every run and `review jobs` (int(inf), 'junk'.get, ...)."""
    cfg = A.Settings(provider_max_concurrent=0)
    state = {"zai": {"throttles": [{"ts": 1}, {"limit": "x"}, "junk", {"limit": 3}]}}
    assert A.effective_limit(cfg, "zai", state) == 3
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, providers={"zai": A.ProviderLimit(1)})
        now = time.time()
        (Path(d) / "providers.json").write_text(
            json.dumps(
                {
                    "zai": {
                        "throttles": ["junk", {"ts": "x"}, {"ts": now}],
                        "starts": ["y", None],
                    },
                    "openai": "junk",
                    "xai": {"throttles": "junk", "starts": 5},
                }
            )
        )
        snap = A.status_snapshot(cfg, probe=lambda: _host())
        assert "zai" in snap["providers"], snap["providers"]
        with A.run_slot(
            mode="review", models=["zai:glm-5.3"], cfg=cfg, stream=open(os.devnull, "w")
        ):
            with A.seat_slot(
                "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
            ) as denied:
                assert denied is None, denied


def test_run_reserves_the_provider_its_failover_chain_dispatches_to():
    """`zai:glm-5.3` with z.ai unavailable dispatches to its CommandCode fallback, so the
    run reserves commandcode, not the provider it will never call (review-cli#614)."""
    from reviewlib import backends

    real_available = backends.backend_available
    real_unpaid = backends.runtime_provider_marked_unpaid
    backends.backend_available = lambda m: A.provider_key(m) != "zai"
    backends.runtime_provider_marked_unpaid = lambda m: False
    try:
        assert A.seats_by_provider(A.routed(["zai:glm-5.3"])) == {"commandcode": 1}
    finally:
        backends.backend_available = real_available
        backends.runtime_provider_marked_unpaid = real_unpaid
    assert A.routed(["fake", "fake"]) == ["fake", "fake"]


def test_text_panels_reserve_their_route_and_moderator_but_qa_its_tester():
    """A text panel reserves the providers its failover chains dispatch to, plus the
    quorum/brainstorm moderator that runs after it; the qa tester is spawned directly, so
    it reserves exactly its own executor (review-cli#614)."""
    from reviewlib import cli

    seen: list[tuple[list[str], list[str]]] = []

    @contextmanager
    def fake_slot(*, mode, models, later_models=None, task_code=None, **_kw):
        seen.append((list(models), list(later_models or [])))
        yield

    real_slot, real_routed = A.run_slot, A.routed
    A.run_slot = fake_slot
    A.routed = lambda models: ["codex:gpt-5" for _ in models]
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            _env(REVIEW_STATS_FILE=str(Path(d) / "stats.jsonl")),
        ):
            cli._run_mode_with_stats("qa", ["claude"], lambda: 0, direct_seats=True)
            cli._run_mode_with_stats(
                "quorum", ["claude"], lambda: 0, later_models=["claude"]
            )
    finally:
        A.run_slot, A.routed = real_slot, real_routed
    assert seen == [
        (["claude"], []),
        (["codex:gpt-5"], ["codex:gpt-5"]),
    ], seen


def test_moderator_after_a_companion_grows_the_held_lease():
    """A run already holding its slot (companion vision first) grows the lease by the
    peak of its panel and the moderator that follows (review-cli#614)."""
    with (
        tempfile.TemporaryDirectory() as d,
        _env(
            REVIEW_ADMISSION="1",
            REVIEW_ADMISSION_DIR=d,
            REVIEW_ADMISSION_LEARN="0",
            REVIEW_ADMISSION_POLL_SECONDS="0.05",
        ),
    ):
        with A.dispatch_scope():
            A.hold_run_slot(mode="quorum", models=["codex:gpt-5"])
            with A.run_slot(
                mode="quorum",
                models=["codex:gpt-5", "codex:gpt-5"],
                later_models=["omp:anthropic/claude-opus-5-5"],
            ):
                lease = json.loads(next(Path(d).glob("run-*.json")).read_text())
        assert lease["provider_seats"].get("openai") == 2, lease
        assert lease["provider_seats"].get("anthropic") == 1, lease
        assert not list(Path(d).glob("run-*.json")), "lease not released"


def test_rpm_start_is_stamped_after_a_contended_registry_lock():
    """With the registry lock held for a while, a seat's rpm start is the moment it was
    admitted, not the moment it began waiting (or rpm=1 would allow two calls within a
    minute)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cfg = _cfg(d, headroom=0.0, providers={"zai": A.ProviderLimit(0, rpm=1)})
        held, release = threading.Event(), threading.Event()

        def hold():
            with A._registry_locked(root, cfg):
                held.set()
                release.wait(10)

        with A._registry_locked(root, cfg):
            pass  # create the registry
        t = threading.Thread(target=hold)
        t.start()
        assert held.wait(5)
        threading.Timer(1.0, release.set).start()
        with A.seat_slot("zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")) as d0:
            admitted = time.time()
        t.join(5)
        assert d0 is None, d0
        starts = json.loads((root / "providers.json").read_text())["zai"]["starts"]
        assert admitted - starts[0] < 0.5, (admitted, starts)


def test_vision_call_gets_only_what_is_left_of_the_command_deadline():
    """`just-ask --visual --timeout N` arms N before the admission wait: the vision call
    (Gemini's HTTP request has no other deadline) gets only what is left, and none once
    N has passed (review-cli#614)."""
    from reviewlib import process
    from reviewlib.features.visual import vision_client as vc

    seen: list[int] = []

    def fake_gemini(model, body, timeout_s):
        seen.append(timeout_s)
        return vc.VisionVerdict(available=True, verdict=None, error="x", backend=model)

    real = vc._call_gemini
    vc._call_gemini = fake_gemini
    try:
        process.set_run_deadline(time.monotonic() + 5)
        vc._dispatch_vision("gemini", "gemini:x", "sys", [], {}, 60, None)
        process.set_run_deadline(time.monotonic() - 1)
        late = vc._dispatch_vision("gemini", "gemini:x", "sys", [], {}, 60, None)
    finally:
        process.set_run_deadline(None)
        vc._call_gemini = real
    assert seen == [5], seen
    assert late.timed_out and late.verdict is None, late


def test_review_visual_companion_reserves_the_padded_role_board():
    """`review diff --staged --task X -m <one model> --visual img` runs that model as 3
    role seats; its queued lease must reserve 3 seats, not 1 (review-cli#614)."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import visual_fixtures as vf

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        log = tmp / "events.log"
        holder = _spawn(d, log, "holder", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        reviewer = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            assert log.exists(), "holder never admitted"
            env = dict(
                os.environ,
                REVIEW_FAKE_BACKEND="1",
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
                REVIEW_JOBS_DIR=str(tmp / "jobs"),
                REVIEW_ADMISSION="1",
                REVIEW_ADMISSION_DIR=str(tmp / "reg"),
                REVIEW_ADMISSION_MAX_SLOTS="1",
                REVIEW_ADMISSION_POLL_SECONDS="0.1",
                REVIEW_ADMISSION_LEARN="0",
            )
            reviewer = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO_ROOT / "bin" / "review"),
                    "diff",
                    "--staged",
                    "--task",
                    "TEST-614",
                    "-m",
                    "codex:gpt-5",
                    "--visual",
                    str(vf.styled_render(tmp / "shot.png")),
                    "--no-tui",
                    "-C",
                    str(repo),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            lease = _companion_lease(tmp / "reg", timeout=30)
        finally:
            if reviewer is not None:
                reviewer.kill()
                reviewer.communicate(timeout=10)
            holder.kill()
            holder.communicate(timeout=10)
        assert lease["seats"] == 3 and lease["provider_seats"] == {"openai": 3}, lease


_REVIEW_WITHOUT_OPENAI = textwrap.dedent(
    """
    import runpy, sys
    sys.path.insert(0, {root!r})
    from reviewlib import admission, backends
    real = backends.backend_available
    backends.backend_available = (
        lambda m: admission.provider_key(m) != "openai" and real(m)
    )
    sys.argv = ["review", *sys.argv[1:]]
    runpy.run_path({review!r}, run_name="__main__")
    """
).format(root=str(REPO_ROOT), review=str(REPO_ROOT / "bin" / "review"))


def test_review_visual_companion_reserves_the_pool_dispatch_will_select():
    """With the two top board entries unavailable, `review diff --pool 2 --visual` runs
    the next two AVAILABLE seats; admission must reserve those providers, not the
    unavailable leading entries' (review-cli#614)."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import visual_fixtures as vf

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "a.txt").write_text("hello\n")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        home = tmp / "home"
        (home / ".config" / "review-cli").mkdir(parents=True)
        (home / ".config" / "review-cli" / "config.yaml").write_text(
            textwrap.dedent(
                """
                board:
                  - { model: "codex:gpt-5", role: correctness }
                  - { model: "codex:gpt-5-mini", role: consistency }
                  - { model: "zai:glm-5.3", role: quality }
                  - { model: "oc:xai/grok-4.7", role: security }
                """
            )
        )
        log = tmp / "events.log"
        holder = _spawn(d, log, "holder", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        reviewer = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            assert log.exists(), "holder never admitted"
            env = dict(
                os.environ,
                HOME=str(home),
                REVIEW_FAKE_BACKEND="1",
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
                REVIEW_JOBS_DIR=str(tmp / "jobs"),
                REVIEW_ADMISSION="1",
                REVIEW_ADMISSION_DIR=str(tmp / "reg"),
                REVIEW_ADMISSION_MAX_SLOTS="1",
                REVIEW_ADMISSION_POLL_SECONDS="0.1",
                REVIEW_ADMISSION_LEARN="0",
            )
            reviewer = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _REVIEW_WITHOUT_OPENAI,
                    "diff",
                    "--staged",
                    "--task",
                    "TEST-614",
                    "--pool",
                    "2",
                    "--visual",
                    str(vf.styled_render(tmp / "shot.png")),
                    "--no-tui",
                    "-C",
                    str(repo),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            lease = _companion_lease(tmp / "reg", timeout=30)
        finally:
            if reviewer is not None:
                reviewer.kill()
                reviewer.communicate(timeout=10)
            holder.kill()
            holder.communicate(timeout=10)
        seats = lease["provider_seats"]
        assert lease["seats"] == 2, lease
        assert seats.get("zai") == 1 and seats.get("xai") == 1, lease
        assert "openai" not in seats, lease


def test_standalone_visual_reserves_the_backend_vision_actually_calls():
    """`review visual img -m zai:glm-5.3,codex:gpt-5`: GLM cannot see images, so the one
    vision call goes to codex; admission must reserve openai, not zai (review-cli#614)."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import visual_fixtures as vf

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "codex").write_text("#!/bin/sh\nexit 0\n")
        (bindir / "codex").chmod(0o755)
        log = tmp / "events.log"
        holder = _spawn(d, log, "holder", 120, REVIEW_ADMISSION_MAX_SLOTS="1")
        viewer = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            assert log.exists(), "holder never admitted"
            env = dict(
                os.environ,
                PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                REVIEW_LOG_DIR=str(tmp / "logs"),
                REVIEW_STATS_FILE=str(tmp / "stats.jsonl"),
                REVIEW_JOBS_DIR=str(tmp / "jobs"),
                REVIEW_ADMISSION="1",
                REVIEW_ADMISSION_DIR=str(tmp / "reg"),
                REVIEW_ADMISSION_MAX_SLOTS="1",
                REVIEW_ADMISSION_POLL_SECONDS="0.1",
                REVIEW_ADMISSION_LEARN="0",
            )
            env.pop("REVIEW_UNPAID_PROVIDERS", None)
            viewer = subprocess.Popen(
                [
                    sys.executable,
                    str(REPO_ROOT / "bin" / "review"),
                    "visual",
                    str(vf.styled_render(tmp / "shot.png")),
                    "-m",
                    "zai:glm-5.3,codex:gpt-5",
                    "-C",
                    str(tmp),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            lease = _companion_lease(tmp / "reg", timeout=30)
        finally:
            if viewer is not None:
                viewer.kill()
                viewer.communicate(timeout=10)
            holder.kill()
            holder.communicate(timeout=10)
        assert lease["provider_seats"] == {"openai": 1}, lease


_LOCK_HOLDER = textwrap.dedent(
    """
    import os, signal, sys, time
    sys.path.insert(0, {root!r})
    from reviewlib import admission as A
    with A._registry_locked(A.registry_dir(A.settings())):
        print("HELD", flush=True)
        if sys.argv[1] == "stop":
            os.kill(os.getpid(), signal.SIGSTOP)
        else:
            time.sleep(120)
    """
).format(root=str(REPO_ROOT))

_ADMIT_ONE = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {root!r})
    from reviewlib import admission as A
    t = time.monotonic()
    with A.run_slot(mode="review", models=["fake"]):
        print(f"ADMITTED {{time.monotonic() - t:.2f}}", flush=True)
    """
).format(root=str(REPO_ROOT))


def _proc_state(pid: int) -> str:
    with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
        return fh.read().rsplit(")", 1)[1].split()[0]


def _registry_lock_scenario(mode: str, **env) -> tuple[str, str, int, int]:
    """A child takes registry.lock and then either SIGSTOPs itself (`stop`) or keeps
    running while holding it (`busy`); a second process then asks for a run slot.
    Returns the second process's (stdout, stderr) and registry.lock's inode before/after.
    """
    with tempfile.TemporaryDirectory() as d:
        reg = Path(d) / "reg"
        full = dict(os.environ)
        full.update(
            REVIEW_ADMISSION="1",
            REVIEW_ADMISSION_DIR=str(reg),
            REVIEW_ADMISSION_POLL_SECONDS="0.05",
            REVIEW_ADMISSION_LEARN="0",
        )
        full.update({k: str(v) for k, v in env.items()})
        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER, mode],
            env=full,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"
            if mode == "stop":
                deadline = time.monotonic() + 10
                while _proc_state(holder.pid) != "T":
                    assert time.monotonic() < deadline, "holder never stopped"
                    time.sleep(0.02)
            inode_before = (reg / "registry.lock").stat().st_ino
            waiter = subprocess.run(
                [sys.executable, "-c", _ADMIT_ONE],
                env=full,
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            inode_after = (reg / "registry.lock").stat().st_ino
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"a review stayed stuck behind a {mode} registry.lock holder: {exc}"
            ) from None
        finally:
            holder.kill()  # SIGKILL also ends a stopped process
            holder.communicate(timeout=10)
        assert waiter.returncode == 0, (waiter.stdout, waiter.stderr)
        return waiter.stdout, waiter.stderr, inode_before, inode_after


def test_stopped_registry_lock_holder_does_not_stall_admission():
    """#619: a process SIGSTOPped while holding registry.lock must not stall every review
    on the host: past lock_stale_seconds the lock is broken (new inode) and the run is
    admitted normally, with a loud warning."""
    if not sys.platform.startswith("linux"):
        return
    out, err, before, after = _registry_lock_scenario(
        "stop",
        REVIEW_ADMISSION_LOCK_STALE_SECONDS="1",
        REVIEW_ADMISSION_LOCK_WAIT_SECONDS="10",
    )
    assert out.startswith("ADMITTED "), (out, err)
    assert float(out.split()[1]) < 8, out
    assert "STALE registry.lock" in err and "is stopped" in err, err
    assert before != after, "the stale lock was not replaced by a fresh inode"


def test_running_registry_lock_holder_is_never_broken_but_wait_is_bounded():
    """A holder that is still running is never broken (it might be mid-decision); the
    waiter gives up after lock_wait_seconds and runs without host-wide admission."""
    if not sys.platform.startswith("linux"):
        return
    out, err, before, after = _registry_lock_scenario(
        "busy",
        REVIEW_ADMISSION_LOCK_STALE_SECONDS="1",
        REVIEW_ADMISSION_LOCK_WAIT_SECONDS="2",
    )
    assert out.startswith("ADMITTED "), (out, err)
    assert float(out.split()[1]) < 8, out
    assert "running without host-wide admission" in err, err
    assert before == after, "a running holder's lock was broken"


def test_resumed_holder_of_a_replaced_registry_lock_drops_its_decision():
    """A holder stopped past lock_stale_seconds had its lock replaced; when it resumes,
    its next registry write raises RegistryLost instead of racing the new holder."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cfg = _cfg(d)
        with A._registry_locked(root, cfg):
            assert A._next_seq(root) == 1
            fresh = root / "fresh"
            fresh.write_text("")
            os.replace(fresh, root / "registry.lock")  # what a breaker does
            try:
                A._next_seq(root)
            except A.RegistryLost:
                pass
            else:
                raise AssertionError("wrote through a lock that was taken over")
        assert (root / "seq").read_text() == "1"
        with A._registry_locked(root, cfg):  # the new lock works normally
            assert A._next_seq(root) == 2


def test_registry_contention_respects_the_command_deadline():
    """A contended registry.lock must not hold a `just-ask --timeout N` past N: a seat is
    denied at the deadline instead of failing open into a paid call, and a run exits 124
    without starting a seat."""
    from reviewlib import process

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cfg = _cfg(d, lock_wait_seconds=30, lock_stale_seconds=30)
        held, done = threading.Event(), threading.Event()

        def hold():
            with A._registry_locked(root, cfg):
                held.set()
                done.wait(20)

        t = threading.Thread(target=hold)
        t.start()
        assert held.wait(5)
        try:
            started = time.monotonic()
            process.set_run_deadline(started + 0.5)
            with A.seat_slot(
                "zai:glm-5.3", cfg=cfg, stream=open(os.devnull, "w")
            ) as denied:
                waited = time.monotonic() - started
            assert denied and "--timeout reached" in denied, denied
            assert waited < 3, waited
            process.set_run_deadline(time.monotonic() + 0.5)
            try:
                with A.run_slot(
                    mode="review",
                    models=["fake"],
                    cfg=cfg,
                    stream=open(os.devnull, "w"),
                ):
                    raise AssertionError("a run started after the command deadline")
            except SystemExit as exc:
                assert exc.code == backstop.BACKSTOP_EXIT_CODE, exc.code
        finally:
            process.set_run_deadline(None)
            done.set()
            t.join(5)


def test_no_home_directory_runs_without_admission_instead_of_crashing():
    """No $XDG_STATE_HOME, no $HOME and no passwd entry: Path.home() raises RuntimeError;
    admission must fail open like any other unavailable registry."""
    real_home = Path.home

    def no_home(cls=None):
        raise RuntimeError("Could not determine home directory")

    with _env(XDG_STATE_HOME=None):
        Path.home = classmethod(no_home)
        try:
            cfg = A.Settings(learn=False)
            with A.run_slot(
                mode="review", models=["fake"], cfg=cfg, stream=open(os.devnull, "w")
            ):
                pass
            with A.seat_slot("zai:glm-5.3", cfg=cfg) as denied:
                assert denied is None, denied
            snap = A.status_snapshot(cfg, probe=lambda: _host())
            assert snap["runs"] == [] and snap["dir"].startswith("unavailable"), snap
        finally:
            Path.home = real_home


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
