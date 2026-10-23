"""Fast seat health checks (reviewlib.health, `review health`, review-cli#663).

Unit level: every probe outcome (answered, quota with a named reset, no answer in time,
transient failure, unexplained failure) lands in the right status and the right store entry;
a seat already excluded is not probed until its re-probe is due; the TTL cache answers a
second probe; probes run in parallel and the batch is bounded by the timeout; the
pre-dispatch check is switchable and quiet when all is well.

End to end: the real `bin/review health --json` / `--show-board` against a local HTTP stub
that answers like z.ai does when the weekly quota is gone (HTTP 429, code 1310, a reset
time), and then like a healthy provider.

Runs under pytest and standalone (`python tests/test_health.py`, as smoke.py does).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import availability, backends, health  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

_LIVE_RESET = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
ZAI_1310 = (
    '{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted. '
    f'Your limit will reset at {_LIVE_RESET:%Y-%m-%d %H:%M:%S}"}}}}'
)
UNTIL_STAMP = f"{_LIVE_RESET:%Y-%m-%d %H:%MZ}"
UNTIL_ISO = f"{_LIVE_RESET:%Y-%m-%dT%H:%M:%SZ}"
KEYS = {
    "ZAI_API_KEY": "k",
    "COMMANDCODE_API_KEY": "k",
    "GEMINI_API_KEY": "k",
    "OPENROUTER_API_KEY": "k",
}


@contextlib.contextmanager
def _env(**values):
    with tempfile.TemporaryDirectory() as tmp:
        values = {
            "REVIEW_SEAT_COOLDOWN_FILE": str(Path(tmp) / "seat-cooldown.json"),
            "REVIEW_LOG_DIR": str(Path(tmp) / "logs"),
            "REVIEW_SEAT_COOLDOWN_SECONDS": None,
            "REVIEW_FAKE_BACKEND": None,
            "REVIEW_UNPAID_PROVIDERS": None,
            "REVIEW_HEALTH_PREFLIGHT": None,
            "REVIEW_HEALTH_TTL_SECONDS": None,
            # no host key file may stand in for an unset key
            "GEMINI_ENV_FILE": str(Path(tmp) / "no-such.env"),
            **KEYS,
            **values,
        }
        saved = {k: os.environ.get(k) for k in values}
        saved_unpaid = backends._CONFIG_UNPAID_PROVIDERS
        backends.configure_unpaid_providers(None)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        try:
            yield Path(tmp)
        finally:
            backends._CONFIG_UNPAID_PROVIDERS = saved_unpaid
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextlib.contextmanager
def _stub_backends(calls: list[str], *, slow_seconds: float = 4.0, delay: float = 0.0):
    """Replace the keyed-HTTP backends with deterministic stubs (no network)."""

    def zai(model, prompt, diff, cwd, timeout, round_no=0, **_):
        calls.append(model)
        return ReviewResult(model, "POST", 429, "", ZAI_1310)

    def commandcode(model, prompt, diff, cwd, timeout, round_no=0, **_):
        calls.append(model)
        time.sleep(delay)
        return ReviewResult(model, "POST", 0, "OK", "")

    def gemini(model, prompt, diff, cwd, timeout, round_no=0, **_):
        calls.append(model)
        time.sleep(slow_seconds)
        return ReviewResult(model, "POST", 0, "OK", "")

    def openrouter(model, prompt, diff, cwd, timeout, round_no=0, **_):
        calls.append(model)
        if model.endswith("transient"):
            return ReviewResult(model, "POST", 503, "", "503 Service Unavailable")
        return ReviewResult(model, "POST", 1, "", "boom: the gateway fell over")

    saved = (
        backends.review_zai,
        backends.review_commandcode,
        backends.review_gemini,
        backends.review_openrouter,
    )
    backends.review_zai = zai
    backends.review_commandcode = commandcode
    backends.review_gemini = gemini
    backends.review_openrouter = openrouter
    try:
        yield
    finally:
        (
            backends.review_zai,
            backends.review_commandcode,
            backends.review_gemini,
            backends.review_openrouter,
        ) = saved


def _by_seat(results):
    return {r.seat: r for r in results}


def test_every_probe_outcome_gets_its_status_and_store_entry():
    with _env(), _stub_backends([]):
        seats = [
            "zai:glm-5.3",
            "commandcode:m/ok",
            "gemini:slow",
            "openrouter:x/boom",
            "openrouter:x/transient",
        ]
        got = _by_seat(health.probe_seats(seats, timeout=1))
        assert got["commandcode:m/ok"].status == health.OK
        quota = got["zai:glm-5.3"]
        assert quota.status == health.UNAVAILABLE and quota.cause == "quota"
        assert quota.until is not None
        assert quota.to_json()["until_iso"] == UNTIL_ISO
        assert got["gemini:slow"].status == health.UNKNOWN, (
            "a slow seat is not a dead one"
        )
        assert got["openrouter:x/transient"].status == health.UNKNOWN
        boom = got["openrouter:x/boom"]
        # an unexplained failure is reported, never excluded on a probe's say-so alone
        assert boom.status == health.UNKNOWN and "gateway fell over" in boom.detail
        # the store now excludes exactly the dead ones
        assert availability.exclusion("zai:glm-5.3") is not None
        assert availability.exclusion("openrouter:x/boom") is None
        assert availability.exclusion("commandcode:m/ok") is None
        assert availability.exclusion("gemini:slow") is None
        assert availability.exclusion("openrouter:x/transient") is None


def test_probes_run_in_parallel_and_the_batch_is_bounded_by_the_timeout():
    with _env(), _stub_backends([], delay=1.5):
        seats = [f"commandcode:m/s{i}" for i in range(6)]
        started = time.monotonic()
        got = health.probe_seats(seats, timeout=5)
        elapsed = time.monotonic() - started
        assert all(r.status == health.OK for r in got), got
        assert elapsed < 4.0, f"6 probes of 1.5s ran serially ({elapsed:.1f}s)"
    with _env(), _stub_backends([], slow_seconds=30):
        started = time.monotonic()
        got = health.probe_seats(["gemini:slow"], timeout=1)
        assert time.monotonic() - started < 5
        assert got[0].status == health.UNKNOWN


def test_an_excluded_seat_is_not_probed_until_its_reprobe_is_due():
    with _env(), _stub_backends(calls := []):
        now = time.time()
        found = availability.classify("", ZAI_1310, 429, now=now)
        availability.record("zai:glm-5.3", found, now=now)
        first = health.probe_seats(["zai:glm-5.3"], timeout=1, now=now + 60)
        assert calls == [] and first[0].status == health.UNAVAILABLE
        assert f"until {UNTIL_STAMP}" in first[0].detail
        # the re-probe comes due: the seat is tried again, and a success ends the window
        due = found.probe_after + 1

        def healthy(model, prompt, diff, cwd, timeout, round_no=0, **_):
            calls.append(model)
            return ReviewResult(model, "POST", 0, "OK", "")

        saved = backends.review_zai
        backends.review_zai = healthy
        try:
            again = health.probe_seats(["zai:glm-5.3"], timeout=1, now=due)
        finally:
            backends.review_zai = saved
        assert calls == ["zai:glm-5.3"] and again[0].status == health.OK
        assert availability.exclusion("zai:glm-5.3") is None
        assert availability.exclusion("oc:zai/glm-5.3") is None, (
            "provider window dropped too"
        )


def test_ttl_cache_answers_a_second_probe_and_no_probe_calls_nothing():
    with _env(), _stub_backends(calls := []):
        seats = ["commandcode:m/ok"]
        health.probe_seats(seats, timeout=1, ttl=300)
        assert calls == ["commandcode:m/ok"]
        again = health.probe_seats(seats, timeout=1, ttl=300)
        assert calls == ["commandcode:m/ok"], "a fresh probe must answer from the cache"
        assert again[0].cached and again[0].status == health.OK
        health.probe_seats(seats, timeout=1, ttl=0)
        assert len(calls) == 2, "ttl 0 always probes"
        offline = health.probe_seats(seats, timeout=1, live=False)
        assert len(calls) == 2 and offline[0].status == health.UNKNOWN


def test_a_statically_unavailable_seat_is_reported_without_a_probe():
    with (
        _env(ZAI_API_KEY=None, REVIEW_UNPAID_PROVIDERS="gemini"),
        _stub_backends(calls := []),
    ):
        os.environ.pop("ZAI_API_KEY", None)
        # no key file fallback either
        got = health.probe_seats(["zai:glm-5.3", "gemini:slow"], timeout=1)
        assert calls == []
        assert all(r.status == health.UNAVAILABLE for r in got), got


def test_preflight_probes_prints_only_newly_dead_seats_and_can_be_switched_off():
    with _env(), _stub_backends(calls := []):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            results = health.preflight(["zai:glm-5.3", "commandcode:m/ok"])
        assert len(results) == 2
        lines = [
            ln
            for ln in err.getvalue().splitlines()
            if ln.startswith("[review-cli] health:")
        ]
        assert len(lines) == 1 and "zai:glm-5.3 unavailable" in lines[0], err.getvalue()
        # the next run's check is quiet: the dead seat is already known, the live one cached
        err = io.StringIO()
        before = len(calls)
        with contextlib.redirect_stderr(err):
            health.preflight(["zai:glm-5.3", "commandcode:m/ok"])
        assert err.getvalue() == "" and len(calls) == before
    with _env(REVIEW_HEALTH_PREFLIGHT="0"), _stub_backends(calls := []):
        assert health.preflight(["commandcode:m/ok"]) == [] and calls == []
    with _env(REVIEW_HEALTH_TTL_SECONDS="0"), _stub_backends(calls := []):
        assert health.preflight(["commandcode:m/ok"]) == [] and calls == []
    with _env(REVIEW_FAKE_BACKEND="1"), _stub_backends(calls := []):
        assert health.preflight(["commandcode:m/ok"]) == [] and calls == []


def test_probe_logs_and_run_stats_stay_out_of_the_review_logs():
    with _env() as tmp, _stub_backends([]):
        health.probe_seats(["commandcode:m/ok"], timeout=1)
        assert not (tmp / "logs").exists() or not any((tmp / "logs").iterdir())
        assert os.environ["REVIEW_LOG_DIR"] == str(tmp / "logs"), "env must be restored"


def test_render_text_and_report_shape():
    with _env(), _stub_backends([]):
        results = health.probe_seats(["zai:glm-5.3", "commandcode:m/ok"], timeout=1)
        rep = health.report(results, host="box", timeout=1, live=True)
        assert rep["schema"] == "review-health" and rep["host"] == "box"
        text = health.render_text(rep)
        assert "ok" in text and "UNAVAILABLE" in text and "1/2 ok" in text
        assert f"until {UNTIL_STAMP}" in text


# ── end to end through the real CLI ─────────────────────────────────────────────────────


class _ZaiStub(BaseHTTPRequestHandler):
    exhausted = True
    seen = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        type(self).seen += 1
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if type(self).exhausted:
            body = ZAI_1310.encode()
            self.send_response(429)
        else:
            body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST  # noqa: N815 — a payment preflight GET gets the same answer


def _review(*args, env, timeout=60):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "review"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_cli_health_records_the_quota_window_then_board_and_dispatch_skip_the_seat():
    server = HTTPServer(("127.0.0.1", 0), _ZaiStub)
    _ZaiStub.exhausted = True
    _ZaiStub.seen = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".state"),
                "REVIEW_SEAT_COOLDOWN_FILE": str(home / "seat-cooldown.json"),
                "REVIEW_LOG_DIR": str(home / "logs"),
                "REVIEW_STATS_FILE": str(home / "run-stats.jsonl"),
                "REVIEW_ADMISSION_DIR": str(home / "admission"),
                "REVIEW_UNPAID_PROVIDERS": "",
                "ZAI_API_KEY": "k",
                "ZAI_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            }
            env.pop("REVIEW_FAKE_BACKEND", None)
            probe = _review("health", "--seat", "zai:glm-5.3", "--json", env=env)
            assert probe.returncode == 1, probe.stdout + probe.stderr  # no seat is ok
            rep = json.loads(probe.stdout)
            seat = rep["seats"][0]
            assert seat["status"] == "unavailable" and seat["cause"] == "quota"
            assert seat["until_iso"] == UNTIL_ISO, seat
            assert seat["probed"] is True
            calls_after_probe = _ZaiStub.seen
            assert calls_after_probe >= 1

            # the store, not a re-probe, answers now
            again = _review(
                "health", "--seat", "zai:glm-5.3", "--no-probe", "--json", env=env
            )
            assert json.loads(again.stdout)["seats"][0]["status"] == "unavailable"
            assert _ZaiStub.seen == calls_after_probe

            board = _review("--show-board", "-m", "zai:glm-5.3", env=env)
            line = next(
                ln
                for ln in board.stdout.splitlines()
                if "zai:glm-5.3" in ln and "#" in ln
            )
            assert "SKIPPED (unavailable:" in line and f"until {UNTIL_STAMP}" in line, (
                line
            )

            # dispatching it asks no provider: a skip sentinel, no call
            skipped = _review(
                "just-ask", "ping", "-m", "zai:glm-5.3", "--task", "T-1", env=env
            )
            assert _ZaiStub.seen == calls_after_probe, "an excluded seat was called"
            assert "unavailable" in (skipped.stdout + skipped.stderr).lower()

            # quota restored on the provider side: a due re-probe ends the window early
            store = json.loads((home / "seat-cooldown.json").read_text())
            for entry in (
                store["zai:glm-5.3"]["window"],
                store["provider:zai"]["window"],
            ):
                entry["probe_after"] = time.time() - 1
            (home / "seat-cooldown.json").write_text(json.dumps(store))
            _ZaiStub.exhausted = False
            healed = _review("health", "--seat", "zai:glm-5.3", "--json", env=env)
            assert healed.returncode == 0, healed.stdout + healed.stderr
            assert json.loads(healed.stdout)["seats"][0]["status"] == "ok"
            board = _review("--show-board", "-m", "zai:glm-5.3", env=env)
            line = next(
                ln
                for ln in board.stdout.splitlines()
                if "zai:glm-5.3" in ln and "#" in ln
            )
            assert "[available]" in line, line
    finally:
        server.shutdown()


def test_dispatch_runs_the_preflight_and_names_a_dead_seat_before_the_run():
    """The pre-dispatch check is wired into the real CLI: `review just-ask -m <seat>` probes
    the seat first and prints a health line when it finds its quota gone."""
    server = HTTPServer(("127.0.0.1", 0), _ZaiStub)
    _ZaiStub.exhausted = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".state"),
                "REVIEW_SEAT_COOLDOWN_FILE": str(home / "seat-cooldown.json"),
                "REVIEW_LOG_DIR": str(home / "logs"),
                "REVIEW_STATS_FILE": str(home / "run-stats.jsonl"),
                "REVIEW_ADMISSION_DIR": str(home / "admission"),
                "REVIEW_UNPAID_PROVIDERS": "",
                "REVIEW_HEALTH_PREFLIGHT": "1",
                "ZAI_API_KEY": "k",
                "ZAI_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            }
            env.pop("REVIEW_FAKE_BACKEND", None)
            out = _review(
                "just-ask", "ping", "-m", "zai:glm-5.3", "--task", "T-2", env=env
            )
            assert "[review-cli] health: zai:glm-5.3 unavailable" in out.stderr, (
                out.stderr
            )
            assert UNTIL_STAMP in out.stderr
    finally:
        server.shutdown()


def test_cli_health_json_over_a_remote_host_uses_ssh():
    """`--host H` runs `review health --json` on H: a fake `ssh` on PATH plays the host."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        fake = bindir / "ssh"
        fake.write_text(
            "#!/bin/sh\n"
            'echo \'{"schema":"review-health","schema_version":1,"host":"boxy",'
            '"probe_timeout_seconds":5,"live":true,"checked_at":"2026-10-23T12:00:00Z",'
            '"seats":[{"seat":"claude:claude-opus-5-5","status":"ok","detail":"answered",'
            '"probed":true,"cached":false,"elapsed_ms":900,"until":null,"until_iso":null,'
            '"cause":null,"scope":null}]}\'\n'
        )
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": tmp}
        env.pop("REVIEW_FAKE_BACKEND", None)
        out = _review(
            "health",
            "--host",
            "boxy",
            "--seat",
            "claude:claude-opus-5-5",
            "--json",
            env=env,
        )
        assert out.returncode == 0, out.stdout + out.stderr
        rep = json.loads(out.stdout)
        assert rep["host"] == "boxy" and rep["seats"][0]["status"] == "ok"
        text = _review("health", "--host", "boxy", env=env)
        assert (
            "Seat health @boxy" in text.stdout
            and "claude:claude-opus-5-5" in text.stdout
        )


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
