#!/usr/bin/env python3
"""Producer/consumer contract: `review capacity-check --json` (this repo, review-cli#472)
read by agent-tools ci/ship/ship.sh's `_review_capacity_all_exhausted` (agent-tools#705).

The consumer is the real shell function, vendored verbatim in
tests/fixtures/agent_tools_ship_capacity_consumer.sh, run under bash + jq against reports
this repo's own `board_capacity_report` builds. It prints `1<TAB>evidence` only when it
would skip the manual hatch prompt, `0` otherwise. A second test fails when a local
agent-tools checkout's live copy of the function has drifted from the vendored one
(`$AGENT_TOOLS_DIR`, else ~/dev/agent-tools or a sibling `agent-tools` checkout).

Needs bash and jq; without them the shell half self-skips loudly (the producer half of
the contract is still pinned by tests/test_capacity_check.py).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import reviewlib.capacity_check as cc  # noqa: E402
import reviewlib.seat_cooldown as sc  # noqa: E402
import test_capacity_check as base  # noqa: E402

FIXTURE = HERE / "fixtures" / "agent_tools_ship_capacity_consumer.sh"
FUNC = "_review_capacity_all_exhausted"


class _Skip(Exception):
    pass


def _function_text(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{FUNC}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _need_shell() -> None:
    if not (shutil.which("bash") and shutil.which("jq")):
        raise _Skip("bash and jq are required to run the agent-tools consumer")


def _consume(report: dict | None, *, review_script: str | None = None) -> str:
    """Run the vendored consumer with a `review` on PATH that prints `report` as its
    `capacity-check --json` output (or runs `review_script`)."""
    with tempfile.TemporaryDirectory() as d:
        bindir = Path(d)
        review = bindir / "review"
        if review_script is None:
            payload = bindir / "report.json"
            payload.write_text(json.dumps(report), encoding="utf-8")
            review_script = (
                "#!/usr/bin/env bash\n"
                f'[ "$1" = capacity-check ] && exec cat "{payload}"\n'
                "exit 2\n"
            )
        review.write_text(review_script, encoding="utf-8")
        review.chmod(review.stat().st_mode | stat.S_IXUSR)
        jq_dir = str(Path(shutil.which("jq")).parent)
        env = {
            "PATH": os.pathsep.join([str(bindir), jq_dir, "/usr/bin", "/bin"]),
            "HOME": d,
        }
        proc = subprocess.run(
            ["bash", "-c", f'source "{FIXTURE}"; {FUNC}'],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip("\n")


def _quota_cooldown(model: str, access_method: str, *, age: float = 30) -> None:
    sc.record_cooldown(
        model,
        "session limit / usage credits",
        access_method=access_method,
        now=base.NOW - age,
        ttl_seconds=3600,
        cause=sc.CAUSE_QUOTA,
    )


def test_consumer_accepts_only_a_fully_covered_fresh_quota_outage():
    _need_shell()
    cards = [base._card("Codex", "c@x.com", base._window(100))]
    with base._env(live={base.OPUS, "codex"}, cards=cards):
        _quota_cooldown(base.OPUS, "cli")
        report = base._report([base.OPUS, "codex"])
    assert report["all_exhausted"] is True, report["candidates"]
    out = _consume(report)
    assert out.startswith("1\t"), out
    assert f"{base.OPUS}: {base.OPUS}: session limit / usage credits" in out, out
    assert "codex: codex [c@x.com]: Codex c@x.com: 5-hour 100%" in out, out


def test_consumer_refuses_incomplete_evidence():
    """agent-tools#705's refusal cases, each built by the real producer: zero candidates,
    one healthy account, one unknown account, one stale observation, a local database
    failure recorded as a cooldown, a non-quota (stall) cooldown, a missing harness
    source and a malformed one."""
    _need_shell()
    cases: dict[str, dict] = {}
    bad_board = {"board": [{"model": base.OPUS, "role": "not-a-role"}]}
    with base._env(live={base.OPUS}):
        cases["zero candidates"] = cc.board_capacity_report(
            config=bad_board, now=base.NOW
        )
    two = ("a@x.com", "b@x.com")
    healthy = [
        base._card("Claude Code", "a@x.com", base._window(100)),
        base._card("Claude Code", "b@x.com", base._window(30)),
    ]
    with base._env(live={base.OPUS}, cards=healthy, accounts=two):
        cases["one healthy account"] = base._report([base.OPUS])
    with base._env(
        live={base.OPUS},
        cards=[base._card("Claude Code", "a@x.com", base._window(100))],
        accounts=two,
    ):
        cases["one unknown account"] = base._report([base.OPUS])
    with base._env(live={base.OPUS}):
        _quota_cooldown(base.OPUS, "cli", age=3 * 3600)
        cases["stale observation"] = base._report([base.OPUS])
    with base._env(live={base.OPUS}):
        sc.record_cooldown(
            base.OPUS,
            "local database is locked; quota unknown",
            access_method="cli",
            now=base.NOW - 30,
            ttl_seconds=3600,
        )
        cases["local database failure"] = base._report([base.OPUS])
    with base._env(live={base.OPUS}):
        sc.record_cooldown(
            base.OPUS,
            "true-silence timeout",
            access_method="cli",
            now=base.NOW - 30,
            ttl_seconds=3600,
            cause=sc.CAUSE_STALL,
        )
        cases["stalled call"] = base._report([base.OPUS])
    with base._env(live={"codex"}, harness_raw="{not json"):
        cases["malformed harness result"] = base._report(["codex"])
    with base._env(live={"codex"}, cards=[]):
        cases["no harness card"] = base._report(["codex"])
    for name, report in cases.items():
        assert report["all_exhausted"] is False, (name, report["candidates"])
        assert _consume(report) == "0", name


def test_consumer_refuses_a_failed_or_garbled_producer():
    _need_shell()
    assert _consume(None, review_script="#!/usr/bin/env bash\nexit 1\n") == "0"
    assert _consume(None, review_script="#!/usr/bin/env bash\necho '{oops'\n") == "0"
    assert (
        _consume(
            None,
            review_script='#!/usr/bin/env bash\necho \'{"all_exhausted":"true"}\'\n',
        )
        == "0"
    )


def test_consumer_against_the_real_cli_with_no_evidence():
    """End to end through `bin/review`: an isolated HOME, every backend live (fake
    backend), no harness card and no cooldown -> the consumer gets a real report and
    refuses."""
    _need_shell()
    with tempfile.TemporaryDirectory() as home:
        cards = Path(home) / "cards.json"
        cards.write_text('{"cards": []}', encoding="utf-8")
        script = (
            "#!/usr/bin/env bash\n"
            f'export HOME="{home}"\n'
            "export REVIEW_FAKE_BACKEND=1\n"
            f'export REVIEW_HARNESS_LIMITS_JSON="{cards}"\n'
            f'export REVIEW_SEAT_COOLDOWN_FILE="{home}/cooldown.json"\n'
            f'export PYTHONPATH="{REPO_ROOT}"\n'
            f'exec "{sys.executable}" "{REPO_ROOT / "bin" / "review"}" "$@"\n'
        )
        assert _consume(None, review_script=script) == "0"
        direct = Path(home) / "review"
        direct.write_text(script, encoding="utf-8")
        direct.chmod(direct.stat().st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            [str(direct), "capacity-check", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    report = json.loads(proc.stdout)
    assert report["schema_version"] == cc.SCHEMA_VERSION
    assert report["coverage"]["candidates"] > 0
    assert report["all_exhausted"] is False


def _agent_tools_ship_sh() -> Path | None:
    candidates = []
    if os.environ.get("AGENT_TOOLS_DIR"):
        candidates.append(Path(os.environ["AGENT_TOOLS_DIR"]))
    candidates += [
        Path.home() / "dev" / "agent-tools",
        REPO_ROOT.parent / "agent-tools",
    ]
    for root in candidates:
        ship = root / "ci" / "ship" / "ship.sh"
        if ship.is_file():
            return ship
    return None


def test_vendored_consumer_matches_live_agent_tools():
    ship = _agent_tools_ship_sh()
    if ship is None:
        raise _Skip("no agent-tools checkout (set AGENT_TOOLS_DIR) to compare against")
    ship_text = ship.read_text(encoding="utf-8")
    if f"{FUNC}() {{" not in ship_text:
        raise _Skip(
            f"{ship} no longer defines {FUNC}: agent-tools ported the consumer to "
            "ci/ship/shiplib/quorum_skipci.py (same fields: all_exhausted, "
            "exhausted[].model/.evidence/.source), so there is no shell copy to diff"
        )
    live = _function_text(ship_text)
    vendored = _function_text(FIXTURE.read_text(encoding="utf-8"))
    assert live == vendored, (
        f"{ship}'s {FUNC} drifted from the vendored copy; re-run this contract against "
        "the new consumer and update tests/fixtures/agent_tools_ship_capacity_consumer.sh"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except _Skip as exc:
                print(f"SKIP {name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
