"""Host-wide admission control: run as many reviews as the machine and the AI providers
can sustain, keeping a safety margin (review-cli#614).

Two budgets, one shared lease registry:

* MACHINE budget (per review RUN). A run is admitted only if, after admitting it, the
  projected host CPU and RAM stay within ``(1 - headroom)`` of capacity::

      cpu_projected = loadavg_1m + sum(est_cpu of runs admitted < ramp_seconds ago) + est_cpu(new)
      mem_projected = (MemTotal - MemAvailable) + sum(est_mem of young runs) + est_mem(new)
      admit iff cpu_projected <= (1 - headroom) * cpu_count
            and mem_projected <= (1 - headroom) * MemTotal

  The load average and MemAvailable already contain every run that has been going for a
  while, so only "young" runs (admitted within ``ramp_seconds``, whose seats have not shown
  up in the 1-minute load average yet) are added on top from their estimate. A run's
  estimate is ``base + per_seat * seats``; ``per_seat`` comes from the config defaults until
  ``learn`` has at least 3 measured runs, then from the 75th percentile of the last
  ``learn_window`` runs (each run samples its own process tree's RSS and CPU time while it
  holds its lease and appends the result to ``costs.jsonl``). The run also waits while the
  kernel reports memory-stall pressure (PSI ``some avg10`` in ``/proc/pressure/memory``)
  above ``max_memory_pressure`` percent: MemAvailable can look roomy while the host is
  already reclaiming (the 2026-10-10/11 thrash). ``min_slots`` runs are always
  admitted regardless of the machine budget (so a host busy with other work still makes
  progress) and ``max_slots`` (0 = none) is a hard ceiling on concurrent runs. Where
  ``/proc/meminfo`` / ``/proc/pressure`` do not exist (macOS) only the CPU budget applies.

  Each admitted run appends its queue wait to ``admissions.jsonl``, which ``review jobs``
  summarizes (queued runs, wait percentiles, peak concurrency) for the last 24 hours.

* PROVIDER budget (per model SEAT call). Each seat call holds a slot of its provider
  (``anthropic``, ``openai``, ``zai``, ``commandcode``, ...). The effective concurrency is
  ``max(1, floor(max_concurrent * (1 - headroom)))``; an optional ``rpm`` caps call starts
  per rolling minute the same way. A 429 / overload reply from a provider lowers its
  effective limit to ``floor(current * throttle_backoff)`` for ``throttle_cooldown_seconds``
  (repeated throttles keep lowering it; it recovers when the events age out). A seat that
  waits longer than ``provider_wait_seconds`` (0 = no cap) gives up with an rc=0
  "unavailable" sentinel, which the board's existing failover turns into the next provider
  in the chain or a reserve seat. So that admitted runs rarely hit that wait, a run is also
  admitted only while every provider it needs has room for its planned seats next to the
  seats committed to running runs (``provider_verdict``).

COORDINATION. Every waiting or running run and seat is a lease in one shared directory:
``<name>.lock`` (an exclusive ``flock`` the owner holds for the lease's whole life) plus
``<name>.json`` (its state, atomically replaced). The kernel drops the flock the instant the
owner dies (SIGKILL included), so any process that can take a lease's lock knows the owner
is gone and deletes both files — crash-safe without trusting pids. A lease whose heartbeat
is older than ``stale_lease_seconds`` (a stopped or wedged owner) no longer counts against
the budget. All admission decisions run under ``registry.lock``; queue order is a global
sequence number, and only the oldest live waiter (per budget) may be admitted — strict FIFO.
``registry.lock`` is held only for those short decisions and taken with a bounded,
non-blocking wait; its holder writes its pid and start time into it. A holder that has
held it past ``lock_stale_seconds`` while stopped (SIGSTOP / ``T`` state) or no longer
running gets the lock file replaced by a fresh inode (logged loudly); a holder that is
still running is never broken, and a waiter that outlasts ``lock_wait_seconds`` proceeds
without host-wide admission instead of stalling every review on the host.

Every parameter is configurable: ``admission:`` in config.yaml and ``REVIEW_ADMISSION_*``
env vars (env > config > built-in default). See ``review help config``.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sys
import threading
import time
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .portable import lock_file, pid_alive, unlock_file

# ── settings ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderLimit:
    max_concurrent: int  # 0 = unlimited
    rpm: int = 0  # 0 = no requests-per-minute cap


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    headroom: float = 0.20
    min_slots: int = 1
    max_slots: int = 0
    cpu_per_seat: float = 0.25
    mem_per_seat_mb: int = 450
    cpu_base: float = 0.1
    mem_base_mb: int = 80
    learn: bool = True
    learn_window: int = 20
    ramp_seconds: int = 120
    poll_seconds: float = 5.0
    stale_lease_seconds: int = 900
    lock_wait_seconds: float = 60.0
    lock_stale_seconds: float = 30.0
    provider_max_concurrent: int = 10
    provider_wait_seconds: int = 600
    throttle_backoff: float = 0.5
    throttle_cooldown_seconds: int = 300
    max_memory_pressure: float = 10.0  # PSI `some avg10` percent; 0 = off
    providers: dict = field(default_factory=dict)  # name -> ProviderLimit
    dir: Path | None = None

    def provider_limit(self, provider: str) -> ProviderLimit:
        return self.providers.get(provider) or ProviderLimit(
            self.provider_max_concurrent
        )


# key -> (env var, parser, validator)
def _parse_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def _parse_int(raw: object) -> int:
    if isinstance(raw, bool):
        raise ValueError("boolean is not an integer")
    return int(str(raw).strip())


def _parse_float(raw: object) -> float:
    if isinstance(raw, bool):
        raise ValueError("boolean is not a number")
    value = float(str(raw).strip())
    if math.isnan(value) or math.isinf(value):
        raise ValueError("not a finite number")
    return value


_FIELDS: dict[str, tuple[str, Callable[[object], object], Callable[[object], bool]]] = {
    "enabled": ("REVIEW_ADMISSION", _parse_bool, lambda v: True),
    "headroom": ("REVIEW_ADMISSION_HEADROOM", _parse_float, lambda v: 0.0 <= v < 1.0),
    "min_slots": ("REVIEW_ADMISSION_MIN_SLOTS", _parse_int, lambda v: v >= 0),
    "max_slots": ("REVIEW_ADMISSION_MAX_SLOTS", _parse_int, lambda v: v >= 0),
    "cpu_per_seat": ("REVIEW_ADMISSION_CPU_PER_SEAT", _parse_float, lambda v: v >= 0),
    "mem_per_seat_mb": (
        "REVIEW_ADMISSION_MEM_PER_SEAT_MB",
        _parse_int,
        lambda v: v >= 0,
    ),
    "cpu_base": ("REVIEW_ADMISSION_CPU_BASE", _parse_float, lambda v: v >= 0),
    "mem_base_mb": ("REVIEW_ADMISSION_MEM_BASE_MB", _parse_int, lambda v: v >= 0),
    "learn": ("REVIEW_ADMISSION_LEARN", _parse_bool, lambda v: True),
    "learn_window": ("REVIEW_ADMISSION_LEARN_WINDOW", _parse_int, lambda v: v >= 1),
    "ramp_seconds": ("REVIEW_ADMISSION_RAMP_SECONDS", _parse_int, lambda v: v >= 0),
    "poll_seconds": ("REVIEW_ADMISSION_POLL_SECONDS", _parse_float, lambda v: v > 0),
    "stale_lease_seconds": (
        "REVIEW_ADMISSION_STALE_SECONDS",
        _parse_int,
        lambda v: v > 0,
    ),
    "lock_wait_seconds": (
        "REVIEW_ADMISSION_LOCK_WAIT_SECONDS",
        _parse_float,
        lambda v: v > 0,
    ),
    "lock_stale_seconds": (
        "REVIEW_ADMISSION_LOCK_STALE_SECONDS",
        _parse_float,
        lambda v: v > 0,
    ),
    "provider_max_concurrent": (
        "REVIEW_ADMISSION_PROVIDER_MAX",
        _parse_int,
        lambda v: v >= 0,
    ),
    "provider_wait_seconds": (
        "REVIEW_ADMISSION_PROVIDER_WAIT_SECONDS",
        _parse_int,
        lambda v: v >= 0,
    ),
    "throttle_backoff": (
        "REVIEW_ADMISSION_THROTTLE_BACKOFF",
        _parse_float,
        lambda v: 0.0 < v < 1.0,
    ),
    "throttle_cooldown_seconds": (
        "REVIEW_ADMISSION_THROTTLE_COOLDOWN_SECONDS",
        _parse_int,
        lambda v: v >= 0,
    ),
    "max_memory_pressure": (
        "REVIEW_ADMISSION_MAX_MEMORY_PRESSURE",
        _parse_float,
        lambda v: 0.0 <= v <= 100.0,
    ),
}
ENV_DIR = "REVIEW_ADMISSION_DIR"
ENV_PROVIDERS = "REVIEW_ADMISSION_PROVIDERS"


def _warn(msg: str) -> None:
    try:
        print(f"[review] admission: {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a warning must never break a run
        pass


def _parse_provider_entry(raw: object) -> ProviderLimit:
    """A provider limit from config (`{max_concurrent: 8, rpm: 60}` or a bare int) or env
    (`8` or `8/60`)."""
    if isinstance(raw, dict):
        max_c = _parse_int(raw.get("max_concurrent", 0))
        rpm = _parse_int(raw.get("rpm", 0) or 0)
    elif isinstance(raw, str) and "/" in raw:
        left, right = raw.split("/", 1)
        max_c, rpm = _parse_int(left), _parse_int(right)
    else:
        max_c, rpm = _parse_int(raw), 0
    if max_c < 0 or rpm < 0:
        raise ValueError("provider limits must be >= 0")
    return ProviderLimit(max_c, rpm)


def _parse_providers(raw: object, source: str) -> dict:
    out: dict[str, ProviderLimit] = {}
    if raw is None:
        return out
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            if "=" not in part:
                _warn(f"ignoring {source} entry {part.strip()!r} (want name=max[/rpm])")
                continue
            name, value = part.split("=", 1)
            items.append((name, value))
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        _warn(f"ignoring {source}: expected a mapping")
        return out
    for name, value in items:
        key = canonical_provider(str(name))
        try:
            out[key] = _parse_provider_entry(value)
        except (TypeError, ValueError):
            _warn(f"ignoring {source} limit for {name!r}: {value!r}")
    return out


def load_settings(config: dict | None = None, env: dict | None = None) -> Settings:
    """Resolve the admission settings: env var > config.yaml `admission:` > default.

    An invalid value at one layer is reported on stderr and that layer is skipped (the
    next layer, ultimately the built-in default, applies) — a typo in a VALUE never
    disables the controller or crashes a run. A config.yaml that cannot be loaded at all
    (no PyYAML, a syntax error) is different: `config.ConfigError` propagates to the CLI,
    which stops with exit 2 (review-cli#705)."""
    if config is None:
        from .config import load_config

        config = load_config()
    env = os.environ if env is None else env
    block = config.get("admission") if isinstance(config, dict) else None
    if block is not None and not isinstance(block, dict):
        _warn("config `admission:` is not a mapping; ignoring it")
        block = None
    block = block or {}
    values: dict = {}
    for key, (env_name, parse, valid) in _FIELDS.items():
        for source, raw in (
            (env_name, env.get(env_name)),
            (f"admission.{key}", block.get(key)),
        ):
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            try:
                value = parse(raw)
            except (TypeError, ValueError):
                _warn(f"ignoring invalid {source}={raw!r}")
                continue
            if not valid(value):
                _warn(f"ignoring out-of-range {source}={raw!r}")
                continue
            values[key] = value
            break
    providers = _parse_providers(block.get("providers"), "admission.providers")
    providers.update(_parse_providers(env.get(ENV_PROVIDERS), ENV_PROVIDERS))
    values["providers"] = providers
    raw_dir = env.get(ENV_DIR) or block.get("dir")
    if raw_dir:
        values["dir"] = Path(os.path.expanduser(str(raw_dir)))
    return Settings(**values)


_SETTINGS: Settings | None = None
_SETTINGS_LOCK = threading.Lock()


def settings() -> Settings:
    """The process-wide settings, resolved once (config + env) and cached."""
    global _SETTINGS
    with _SETTINGS_LOCK:
        if _SETTINGS is None:
            _SETTINGS = load_settings()
        return _SETTINGS


def reset_for_tests() -> None:
    """Drop cached settings so a test can change env/config and re-resolve."""
    global _SETTINGS
    with _SETTINGS_LOCK:
        _SETTINGS = None


def registry_dir(cfg: Settings) -> Path:
    """The shared registry dir. OSError when there is no state dir to put it in, so every
    caller's fail-open "registry unavailable" path applies."""
    if cfg.dir is not None:
        return cfg.dir
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        try:
            base = str(Path.home() / ".local" / "state")
        except RuntimeError as exc:  # no $HOME and no passwd entry
            raise OSError(errno.ENOENT, f"no state directory: {exc}") from exc
    return Path(base) / "review-cli" / "admission"


# ── providers ───────────────────────────────────────────────────────────────────────────

# The account/vendor whose limits a seat consumes. Several transports share one vendor
# (the claude CLI, `omp:anthropic/...` and `oc:anthropic/...` all spend Anthropic
# capacity), so they share one budget.
_PROVIDER_FAMILY = {
    "claude": "anthropic",
    "fable": "anthropic",
    "codex": "openai",
    "openai-codex": "openai",
    "gemini": "google",
    "command-code": "commandcode",
    "command_code": "commandcode",
    "cc": "commandcode",
    "glm": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
}


def canonical_provider(name: str) -> str:
    """One budget key per vendor, accepting every spelling `resolve_backend` accepts
    (`gemini-api`, `common-code`, `z.ai`, ...)."""
    from .backends import _canonical_provider

    key = _canonical_provider(name)
    return _PROVIDER_FAMILY.get(key, key)


def provider_key(model: str) -> str:
    """The provider budget a seat's model spends: `omp:anthropic/x` -> anthropic,
    `claude:claude-opus-5-5` -> anthropic (or the Anthropic-compatible gateway
    ANTHROPIC_BASE_URL points at, e.g. commandcode), `oc:zai/glm-5.3` -> zai,
    `codex:gpt` -> openai, `omp:opencode-go/mimo` -> opencode-go."""
    lowered = model.strip().lower()
    for prefix in ("omp:", "oc:", "opencode:"):
        if lowered.startswith(prefix):
            rest = lowered[len(prefix) :]
            if prefix == "omp:" and "/" not in rest:
                # an unqualified omp selector (`omp:claude-sonnet-5-5`) is served by the
                # same account pool its qualified spelling names
                from .backends import _omp_model_hosts_claude

                if _omp_model_hosts_claude(rest):
                    return "anthropic"
            head = rest.split("/", 1)[0].split(":", 1)[0]
            return canonical_provider(head)
    if lowered.startswith("fable"):
        return "anthropic"
    for sep in (":", "/"):
        if sep in lowered:
            lowered = lowered.split(sep, 1)[0]
    if lowered == "claude":
        # the claude backend (API and CLI) runs on whatever gateway ANTHROPIC_BASE_URL names
        from .backends import _claude_runtime_gateway_provider

        return canonical_provider(_claude_runtime_gateway_provider() or "claude")
    return canonical_provider(lowered)


# ── host probes ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostSnapshot:
    cpus: int
    load1: float
    mem_total_mb: float | None  # None when the platform does not expose it
    mem_available_mb: float | None
    mem_pressure: float | None = None  # PSI `some avg10` percent; None = not exposed


def probe_host() -> HostSnapshot:
    cpus = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        load1 = 0.0
    total = avail = None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) / 1024
    except OSError:
        pass
    if total is None or avail is None:
        total = avail = None
    return HostSnapshot(cpus, load1, total, avail, _memory_pressure())


def _memory_pressure(path: str = "/proc/pressure/memory") -> float | None:
    """PSI `some avg10` of /proc/pressure/memory: the percent of the last 10 s in which some
    task stalled on memory reclaim. None without PSI (macOS, CONFIG_PSI=n)."""
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("some "):
                    for part in line.split()[1:]:
                        if part.startswith("avg10="):
                            return float(part.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return None


_PAGE_MB = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096) / (
    1024 * 1024
)
_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _children(pid: int) -> list[int]:
    out: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return out
    for tid in tasks:
        try:
            with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                out.extend(int(x) for x in fh.read().split())
        except (OSError, ValueError):
            continue
    return out


def sample_tree(root: int) -> tuple[float, float] | None:
    """(RSS MiB, CPU seconds incl. reaped children) of `root` and its live descendants, or
    None where /proc is unavailable."""
    if not os.path.isdir(f"/proc/{root}"):
        return None
    rss = cpu = 0.0
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
                rss += int(fh.read().split()[1]) * _PAGE_MB
        except (OSError, IndexError, ValueError):
            continue
        cpu += sum(int(x) for x in fields[11:15]) / _TICKS
        stack.extend(_children(pid))
    return rss, cpu


# ── registry ────────────────────────────────────────────────────────────────────────────

_SEQ_FILE = "seq"
_LOCK_FILE = "registry.lock"
_BREAK_FILE = "registry.break.lock"
_PROVIDERS_FILE = "providers.json"
_COSTS_FILE = "costs.jsonl"
_COSTS_KEEP = 200
_ADMISSIONS_FILE = "admissions.jsonl"
_ADMISSIONS_KEEP = 5000  # busiest day so far: ~500 reviews
_ADMISSIONS_WINDOW_SECONDS = 24 * 3600
_LOCK_BACKOFF_MIN = 0.01
_LOCK_BACKOFF_MAX = 0.25


class RegistryBusy(OSError):
    """registry.lock stayed held past ``lock_wait_seconds`` (or the command deadline) by a
    holder that could not be proven stale. An OSError, so every caller's "registry
    unavailable" path applies."""


class RegistryLost(OSError):
    """The registry.lock this thread holds was replaced as stale (this process was stopped
    past ``lock_stale_seconds``): the decision in progress must be dropped and retried."""


# The registry.lock fd the current thread holds, for `_fence`.
_REGISTRY_HELD = threading.local()


def _fence(root: Path) -> None:
    """Call right before a write made under registry.lock. A holder that was stopped past
    `lock_stale_seconds` had its lock replaced and may now share the registry with the new
    holder; this makes it notice on resume and drop its decision instead of writing it.
    (A stop between this check and the write itself stays possible — flock can't fence
    a process that is no longer on the inode everyone else locks.)"""
    fd = getattr(_REGISTRY_HELD, "fd", None)
    if fd is not None and not _same_file(fd, root / _LOCK_FILE):
        raise RegistryLost(
            errno.ESTALE,
            "registry.lock was replaced as stale while this process held it (it was "
            "stopped past lock_stale_seconds); redoing the admission decision",
        )


def _exit_if_deadline_passed(out, what: str) -> None:
    """An explicit `just-ask --timeout N` that passed: exit 124 before any seat starts."""
    from .backstop import BACKSTOP_EXIT_CODE
    from .process import run_deadline_expired

    if run_deadline_expired():
        print(
            f"[review] admission: --timeout reached while {what}; no seat was started",
            file=out,
            flush=True,
        )
        raise SystemExit(BACKSTOP_EXIT_CODE)


def _proc_state(pid: int) -> str | None:
    """`pid`'s one-letter process state ("T"/"t" = stopped/traced), "" when it is alive
    but its state is unknown (no /proc), None when it is not running."""
    if pid <= 0:
        return None
    if not os.path.isdir("/proc/self"):
        return "" if pid_alive(pid) else None
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        return ""
    return None if state in ("Z", "X") else state


def _read_holder(path: Path) -> tuple[int, float, int] | None:
    """(pid, since, inode) recorded in the lock file at `path`, or None."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        ino = os.fstat(fd).st_ino
        data = json.loads(os.read(fd, 256) or b"null")
        return int(data["pid"]), float(data["since"]), ino
    except (OSError, ValueError, TypeError, KeyError):
        return None
    finally:
        os.close(fd)


def _same_file(fd: int, path: Path) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _break_stale_lock(
    root: Path, cfg: Settings, holder: tuple[int, float, int]
) -> bool:
    """Replace registry.lock with a fresh inode iff its recorded holder has held it past
    `lock_stale_seconds` and is stopped or gone. True when the lock was replaced."""
    pid, since, _ino = holder
    held_for = time.time() - since
    if held_for < cfg.lock_stale_seconds:
        return False
    state = _proc_state(pid)
    if state is None:
        why = "is no longer running"
    elif state in ("T", "t"):
        why = "is stopped"
    else:
        return False  # still running: it may be mid-decision, never break it
    path = root / _LOCK_FILE
    # Breakers take their own lock, so two waiters can't each install a fresh lock file
    # and both believe they hold registry.lock.
    bfd = os.open(root / _BREAK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            lock_file(bfd, blocking=False)
        except OSError:
            return False
        try:
            if _read_holder(path) != holder:
                return False  # released, re-taken or already broken meanwhile
            fresh = root / f".{_LOCK_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
            os.close(os.open(fresh, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600))
            os.replace(fresh, path)
        except OSError:
            return False
        finally:
            unlock_file(bfd)
    finally:
        os.close(bfd)
    _warn(
        f"STALE registry.lock: holder pid {pid} {why} and has held it for "
        f"{held_for:.0f}s (lock_stale_seconds={cfg.lock_stale_seconds:g}); replaced the "
        f"lock file so admission can continue — investigate pid {pid}"
    )
    return True


def _acquire_registry(root: Path, cfg: Settings) -> int:
    """An fd holding registry.lock with this process recorded as holder. Non-blocking
    attempts with backoff; breaks a provably stale lock; RegistryBusy past
    `lock_wait_seconds` or an armed command deadline (`just-ask --timeout N`)."""
    from .process import run_deadline_remaining

    path = root / _LOCK_FILE
    started = time.monotonic()
    pause = _LOCK_BACKOFF_MIN
    warned = False
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_file(fd, blocking=False)
        except BlockingIOError:
            os.close(fd)
        except BaseException:
            os.close(fd)
            raise
        else:
            if _same_file(fd, path):  # not a file a breaker replaced since our open
                try:
                    record = json.dumps({"pid": os.getpid(), "since": time.time()})
                    os.ftruncate(fd, 0)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, record.encode())
                except BaseException:
                    unlock_file(fd)
                    os.close(fd)
                    raise
                return fd
            unlock_file(fd)
            os.close(fd)
            continue
        holder = _read_holder(path)
        if holder is not None and _break_stale_lock(root, cfg, holder):
            pause = _LOCK_BACKOFF_MIN
            continue
        waited = time.monotonic() - started
        if waited >= cfg.lock_stale_seconds and not warned:
            who = f"pid {holder[0]}" if holder else "an unrecorded holder"
            _warn(
                f"registry.lock held by {who} for over {waited:.0f}s and it is still "
                f"running; waiting up to lock_wait_seconds={cfg.lock_wait_seconds:g}"
            )
            warned = True
        remaining = run_deadline_remaining()
        if remaining is not None and remaining <= 0:
            raise RegistryBusy(
                errno.ETIMEDOUT, "command deadline reached waiting for registry.lock"
            )
        if waited >= cfg.lock_wait_seconds:
            who = f"pid {holder[0]}" if holder else "an unrecorded holder"
            raise RegistryBusy(
                errno.ETIMEDOUT,
                f"registry.lock held by {who} for over {waited:.0f}s "
                f"(lock_wait_seconds={cfg.lock_wait_seconds:g})",
            )
        time.sleep(pause if remaining is None else max(0.0, min(pause, remaining)))
        pause = min(pause * 2, _LOCK_BACKOFF_MAX)


@contextmanager
def _registry_locked(root: Path, cfg: Settings | None = None) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = _acquire_registry(root, cfg or settings())
    _REGISTRY_HELD.fd = fd
    try:
        yield
    finally:
        _REGISTRY_HELD.fd = None
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            unlock_file(fd)
        finally:
            os.close(fd)


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _next_seq(root: Path) -> int:
    """Next global queue ticket. Caller holds the registry lock."""
    path = root / _SEQ_FILE
    try:
        current = int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        current = 0
    _fence(root)
    path.write_text(str(current + 1))
    return current + 1


class Lease:
    """One waiting/running run or seat. Owns `<name>.lock` (flock held for life) and
    `<name>.json` (state)."""

    def __init__(self, root: Path, kind: str, seq: int, data: dict) -> None:
        self.root = root
        self.name = f"{kind}-{seq:012d}-{os.getpid()}"
        self.lock_path = root / f"{self.name}.lock"
        self.json_path = root / f"{self.name}.json"
        self.data = dict(
            data, kind=kind, seq=seq, pid=os.getpid(), heartbeat=time.time()
        )
        self._fd: int | None = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        lock_file(
            self._fd, blocking=False
        )  # fresh, uniquely-named file: never contended
        _atomic_write(self.json_path, self.data)
        self._mutex = threading.Lock()

    def update(self, **fields) -> None:
        with self._mutex:
            if self._fd is None:
                return
            self.data.update(fields)
            self.data["heartbeat"] = time.time()
            try:
                _atomic_write(self.json_path, self.data)
            except OSError:
                pass

    def release(self) -> None:
        with self._mutex:
            if self._fd is None:
                return
            for path in (self.json_path, self.lock_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                unlock_file(self._fd)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None


def _owner_alive(lock_path: Path) -> bool:
    """False iff nobody holds the lease's flock (owner dead) or its lock file is gone."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            lock_file(fd, blocking=False)
        except OSError:
            return True  # held: the owner is alive
        unlock_file(fd)
        return False
    finally:
        os.close(fd)


def _scan(root: Path, cfg: Settings, now: float) -> list[dict]:
    """Live leases (dead owners' files removed). Caller holds the registry lock. Each entry
    gets `stale=True` when its heartbeat is older than `stale_lease_seconds`."""
    out: list[dict] = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    present = set(names)
    for name in names:
        if not name.endswith(".json") or not (
            name.startswith("run-") or name.startswith("seat-")
        ):
            continue
        json_path = root / name
        lock_path = root / (name[: -len(".json")] + ".lock")
        if not _owner_alive(lock_path):
            for path in (json_path, lock_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        data = _read_json(json_path)
        if data is None:
            continue
        data["stale"] = (
            now - float(data.get("heartbeat") or 0) > cfg.stale_lease_seconds
        )
        data["lease"] = name[: -len(".json")]
        out.append(data)
    # Orphan lock files (owner died between creating the lock and writing its json).
    for name in names:
        if (
            name.endswith(".lock")
            and name not in (_LOCK_FILE, _BREAK_FILE)
            and name[: -len(".lock")] + ".json" not in present
        ):
            lock_path = root / name
            if not _owner_alive(lock_path):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        # A writer killed mid-`_atomic_write` leaves its temp file behind.
        elif name.startswith(".") and name.endswith(".tmp"):
            try:
                if now - (root / name).stat().st_mtime > cfg.stale_lease_seconds:
                    (root / name).unlink()
            except OSError:
                pass
    out.sort(key=lambda d: d.get("seq", 0))
    return out


# ── machine budget ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeatCost:
    cpu: float
    mem_mb: float
    source: str  # "config" or "learned (N runs)"


def seat_cost(cfg: Settings, root: Path) -> SeatCost:
    """Per-seat cost estimate: learned p75 of recent runs when `learn` has >= 3 samples,
    else the configured defaults."""
    if cfg.learn:
        samples = _load_costs(root)[-cfg.learn_window :]
        cpus: list[float] = []
        mems: list[float] = []
        for s in samples:
            seats = s.get("seats") or 0
            if seats <= 0:
                continue
            cpus.append(max(0.0, (float(s.get("cpu_avg", 0)) - cfg.cpu_base) / seats))
            mems.append(
                max(0.0, (float(s.get("peak_rss_mb", 0)) - cfg.mem_base_mb) / seats)
            )
        if len(cpus) >= 3:
            return SeatCost(
                _percentile(cpus, 0.75),
                _percentile(mems, 0.75),
                f"learned ({len(cpus)} runs)",
            )
    return SeatCost(cfg.cpu_per_seat, float(cfg.mem_per_seat_mb), "config")


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (`q` in 0..1) of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(math.ceil(q * len(ordered))) - 1))]


def run_estimate(cfg: Settings, cost: SeatCost, seats: int) -> tuple[float, float]:
    seats = max(1, seats)
    return cfg.cpu_base + cost.cpu * seats, cfg.mem_base_mb + cost.mem_mb * seats


@dataclass(frozen=True)
class Verdict:
    admit: bool
    reason: str  # why not (empty when admitted)
    cpu_projected: float
    cpu_budget: float
    mem_projected_mb: float | None
    mem_budget_mb: float | None


def machine_verdict(
    cfg: Settings,
    host: HostSnapshot,
    running: list[dict],
    est_cpu: float,
    est_mem_mb: float,
    now: float,
) -> Verdict:
    """Would admitting one more run (estimate `est_cpu`/`est_mem_mb`) keep the host within
    `(1 - headroom)` of capacity, with memory-stall pressure at or below
    `max_memory_pressure`? `running` are the live, non-stale running run leases."""
    usable = 1.0 - cfg.headroom
    young = [
        r for r in running if now - float(r.get("admitted_at") or 0) < cfg.ramp_seconds
    ]
    cpu_proj = host.load1 + sum(float(r.get("est_cpu", 0)) for r in young) + est_cpu
    cpu_budget = usable * host.cpus
    mem_proj = mem_budget = None
    if host.mem_total_mb is not None and host.mem_available_mb is not None:
        mem_proj = (
            host.mem_total_mb
            - host.mem_available_mb
            + sum(float(r.get("est_mem_mb", 0)) for r in young)
            + est_mem_mb
        )
        mem_budget = usable * host.mem_total_mb
    n = len(running)
    if cfg.max_slots and n >= cfg.max_slots:
        return Verdict(
            False,
            f"max_slots={cfg.max_slots} reached",
            cpu_proj,
            cpu_budget,
            mem_proj,
            mem_budget,
        )
    if n < cfg.min_slots:
        return Verdict(True, "", cpu_proj, cpu_budget, mem_proj, mem_budget)
    eps = 1e-9
    if cpu_proj > cpu_budget + eps:
        return Verdict(False, "cpu", cpu_proj, cpu_budget, mem_proj, mem_budget)
    if mem_proj is not None and mem_budget is not None and mem_proj > mem_budget + eps:
        return Verdict(False, "memory", cpu_proj, cpu_budget, mem_proj, mem_budget)
    pressure = host.mem_pressure
    if (
        cfg.max_memory_pressure
        and pressure is not None
        and pressure > cfg.max_memory_pressure
    ):
        return Verdict(
            False,
            f"memory pressure {pressure:.1f}% > {cfg.max_memory_pressure:g}% "
            "(PSI some avg10: the host is already stalling on reclaim)",
            cpu_proj,
            cpu_budget,
            mem_proj,
            mem_budget,
        )
    return Verdict(True, "", cpu_proj, cpu_budget, mem_proj, mem_budget)


# ── provider budget ─────────────────────────────────────────────────────────────────────


def _load_provider_state(root: Path) -> dict:
    return _read_json(root / _PROVIDERS_FILE) or {}


def _prune_provider_state(state: dict, cfg: Settings, now: float) -> dict:
    """Live throttles/starts per provider. providers.json is shared, hand-editable state:
    any entry that is not the expected shape is dropped rather than crashing every run."""

    def _age(stamp) -> float:
        try:
            return now - float(stamp)
        except (TypeError, ValueError):
            return math.inf

    pruned: dict = {}
    for provider, entry in state.items():
        if not isinstance(entry, dict):
            continue
        throttles = entry.get("throttles")
        starts = entry.get("starts")
        live = {
            "throttles": [
                t
                for t in (throttles if isinstance(throttles, list) else [])
                if isinstance(t, dict)
                and _age(t.get("ts")) < cfg.throttle_cooldown_seconds
            ],
            "starts": [
                t for t in (starts if isinstance(starts, list) else []) if _age(t) < 60
            ],
        }
        if live["throttles"] or live["starts"]:
            pruned[provider] = live
    return pruned


def configured_limit(cfg: Settings, provider: str) -> float:
    """Concurrency after headroom; math.inf when unlimited."""
    base = cfg.provider_limit(provider).max_concurrent
    if base <= 0:
        return math.inf
    return max(1, math.floor(base * (1.0 - cfg.headroom) + 1e-9))


def effective_limit(cfg: Settings, provider: str, state: dict) -> float:
    limit = configured_limit(cfg, provider)
    for t in state.get(provider, {}).get("throttles", []):
        try:  # providers.json is shared state: skip an entry someone garbled
            value = float(t["limit"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            limit = min(limit, max(1, int(value)))
    return limit


def rpm_limit(cfg: Settings, provider: str) -> float:
    rpm = cfg.provider_limit(provider).rpm
    if rpm <= 0:
        return math.inf
    return max(1, math.floor(rpm * (1.0 - cfg.headroom) + 1e-9))


def routed(models: list[str]) -> list[str]:
    """The executors a TEXT panel's seats are dispatched to first: the head of each
    model's failover chain (unpaid and unavailable providers dropped, last-working
    first), the same chain panel dispatch walks — so a run reserves the providers it will
    actually call. A seat whose whole chain is excluded (an exhausted quota or a failed
    login recorded by `reviewlib.availability`) is not planned at all: it
    answers with a skip sentinel without a call, so it must not hold a provider
    reservation or a place in the queue behind other runs. Direct executors (the qa
    tester, vision calls) skip this."""
    try:
        from . import backends
        from .provider_failover import provider_chain

        out = []
        for model in models:
            chain = provider_chain(
                model,
                available=backends.backend_available,
                unpaid=backends.runtime_provider_marked_unpaid,
            )
            head = chain[0] if chain else model
            # only a seat that answers with a skip sentinel (no call) is left unplanned
            if backends.availability_exclusion(head) is None:
                out.append(head)
        return out
    except Exception:  # noqa: BLE001 — planning must never break a run
        return list(models)


def seats_by_provider(models: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for model in models:
        key = provider_key(model)
        out[key] = out.get(key, 0) + 1
    return out


def provider_verdict(
    cfg: Settings, state: dict, running: list[dict], need: dict[str, int]
) -> str:
    """Run-level provider reservation: "" when every provider this run needs still has
    room for its seats next to the seats already committed to running runs, else the
    reason. A run needing more seats of a provider than its whole limit reserves the limit
    (its seats then take turns at the seat-level gate) so it can never wait forever.
    Below `min_slots` running runs this never blocks."""
    if len(running) < cfg.min_slots:
        return ""
    for provider, count in sorted(need.items()):
        limit = effective_limit(cfg, provider, state)
        if limit == math.inf:
            continue
        committed = sum(
            int((r.get("provider_seats") or {}).get(provider, 0)) for r in running
        )
        if committed + min(count, limit) > limit:
            return (
                f"provider {provider} full ({committed}/{int(limit)} seats committed)"
            )
    return ""


def record_throttle(
    provider: str, *, cfg: Settings | None = None, now: float | None = None
) -> float:
    """A 429/overload reply from `provider`: lower its effective limit for the cooldown.
    Returns the new effective limit."""
    cfg = cfg or settings()
    if not cfg.enabled or cfg.throttle_cooldown_seconds <= 0:
        return math.inf
    now = time.time() if now is None else now
    root = registry_dir(cfg)
    with _registry_locked(root, cfg):
        state = _prune_provider_state(_load_provider_state(root), cfg, now)
        current = effective_limit(cfg, provider, state)
        if current == math.inf:
            inflight = sum(
                1
                for e in _scan(root, cfg, now)
                if e.get("kind") == "seat"
                and e.get("state") == "running"
                and e.get("provider") == provider
            )
            current = max(1, inflight)
        new = max(1, math.floor(current * cfg.throttle_backoff))
        entry = state.setdefault(provider, {"throttles": [], "starts": []})
        entry["throttles"].append({"ts": now, "limit": new})
        _fence(root)
        _atomic_write(root / _PROVIDERS_FILE, state)
    _warn(
        f"{provider} is throttling (429/overload) — its concurrent-seat limit drops to {new} "
        f"for {cfg.throttle_cooldown_seconds}s"
    )
    return new


# ── per-process heartbeat ───────────────────────────────────────────────────────────────

_HELD: set[Lease] = set()
_HELD_LOCK = threading.Lock()
_HEARTBEAT: threading.Thread | None = None


def _heartbeat_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        with _HELD_LOCK:
            leases = list(_HELD)
        for lease in leases:
            lease.update()


def _hold(lease: Lease, cfg: Settings) -> None:
    global _HEARTBEAT
    with _HELD_LOCK:
        _HELD.add(lease)
        if _HEARTBEAT is None or not _HEARTBEAT.is_alive():
            interval = max(1.0, min(cfg.poll_seconds, cfg.stale_lease_seconds / 4))
            _HEARTBEAT = threading.Thread(
                target=_heartbeat_loop, args=(interval,), daemon=True
            )
            _HEARTBEAT.start()


def _drop(lease: Lease) -> None:
    with _HELD_LOCK:
        _HELD.discard(lease)
    lease.release()


# ── run admission ───────────────────────────────────────────────────────────────────────

# The run lease this PROCESS holds (one review run per process). Seats dispatched from
# worker threads read it to label their lease; a nested run_slot in the same process is a
# no-op instead of queueing behind itself.
_CURRENT_RUN: Lease | None = None


def _fmt_secs(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _typical_run_seconds(mode: str, seats: int) -> float:
    try:
        from .stats import estimate_eta

        eta = estimate_eta(mode, seats)
    except Exception:  # noqa: BLE001 — ETA is advisory
        eta = None
    return float(eta["avg_seconds"]) if eta else 600.0


def _job_note(**fields) -> None:
    job_id = os.environ.get("REVIEW_JOB_ID")
    if not job_id:
        return
    try:
        from . import jobs

        jobs.write_job(job_id, **fields)
    except Exception:  # noqa: BLE001 — job bookkeeping is advisory here
        pass


class _Sampler(threading.Thread):
    """Samples this process tree's RSS/CPU while the run holds its lease (cost learning)."""

    def __init__(self, interval: float) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.stop = threading.Event()
        self.start_wall = time.monotonic()
        first = sample_tree(os.getpid())
        self.start_cpu = first[1] if first else None
        self.peak_rss = first[0] if first else 0.0
        self.last_cpu = self.start_cpu

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            self._sample()

    def _sample(self) -> None:
        got = sample_tree(os.getpid())
        if got is None:
            return
        self.peak_rss = max(self.peak_rss, got[0])
        self.last_cpu = got[1]

    def result(self) -> tuple[float, float, float] | None:
        self.stop.set()
        self._sample()
        if self.start_cpu is None or self.last_cpu is None:
            return None
        wall = time.monotonic() - self.start_wall
        return (
            self.peak_rss,
            max(0.0, self.last_cpu - self.start_cpu) / max(wall, 1e-6),
            wall,
        )


def _load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _load_costs(root: Path) -> list[dict]:
    return _load_jsonl(root / _COSTS_FILE)


def _append_ledger(path: Path, item: dict, keep: int) -> None:
    """Append one record to a registry ledger, trimming it to the last `keep` once it
    passes twice that. The caller holds registry.lock and has fenced it. Counting lines
    (no JSON parse) keeps the per-record cost small and bounded; only a trim parses."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")
    with open(path, "rb") as fh:
        lines = sum(1 for _ in fh)
    if lines > keep * 2:
        items = _load_jsonl(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i) + "\n" for i in items[-keep:]))
        os.replace(tmp, path)


def _record_cost(
    root: Path,
    cfg: Settings,
    seats: int,
    peak_rss: float,
    cpu_avg: float,
    wall: float,
) -> None:
    if wall < 30 or seats <= 0:
        return  # a near-instant run (clean tree, early error) says nothing about seat cost
    item = {
        "ts": time.time(),
        "seats": seats,
        "peak_rss_mb": round(peak_rss, 1),
        "cpu_avg": round(cpu_avg, 3),
        "seconds": round(wall, 1),
    }
    try:
        with _registry_locked(root, cfg):
            _fence(root)
            _append_ledger(root / _COSTS_FILE, item, _COSTS_KEEP)
    except OSError:
        pass


def _record_admission(root: Path, lease: Lease, now: float, running: int) -> None:
    """One line per admitted run: how long it queued and how many reviews were running
    host-wide once it started — the record `review jobs` summarizes. Called under
    registry.lock right after the admission decision; never raises."""
    try:
        _append_ledger(
            root / _ADMISSIONS_FILE,
            {
                "ts": now,
                "mode": lease.data.get("mode"),
                "task": lease.data.get("task"),
                "seats": lease.data.get("seats"),
                "queued_seconds": round(
                    max(0.0, now - float(lease.data.get("enqueued_at", now))), 1
                ),
                "running": running,
            },
            _ADMISSIONS_KEEP,
        )
    except (OSError, TypeError, ValueError):
        pass


@contextmanager
def run_slot(
    *,
    mode: str,
    models: list[str],
    later_models: list[str] | None = None,
    final_models: list[str] | None = None,
    task_code: str | None = None,
    cfg: Settings | None = None,
    probe: Callable[[], HostSnapshot] = probe_host,
    stream=None,
) -> Iterator[None]:
    """Hold a host-wide run slot for the `with` block, waiting FIFO until the machine
    budget admits this run. `later_models` and then `final_models` are dispatched by the
    same run after `models` (companion vision, then the text panel, then its moderator):
    admission reserves the peak of the phases up front, so the run never grows past what
    was checked. A no-op when disabled or when this process already holds one (a mode
    that nests another dispatch)."""
    global _CURRENT_RUN
    cfg = cfg or settings()
    if not cfg.enabled:
        yield
        return
    phases = [models, later_models or [], final_models or []]
    if _CURRENT_RUN is not None:
        _grow(_CURRENT_RUN, cfg, *phases)
        yield
        return
    out = stream if stream is not None else sys.stderr
    seats = max(1, *(len(p) for p in phases))
    provider_seats = _peak_seats(*phases)
    while True:
        try:
            root = registry_dir(cfg)
            cost = seat_cost(cfg, root)  # costs.jsonl is read outside the registry lock
            with _registry_locked(root, cfg):
                est_cpu, est_mem = run_estimate(cfg, cost, seats)
                lease = Lease(
                    root,
                    "run",
                    _next_seq(root),
                    {
                        "state": "waiting",
                        "mode": mode,
                        "task": task_code or "",
                        "cwd": os.getcwd(),
                        "seats": seats,
                        "providers": sorted(provider_seats),
                        "provider_seats": provider_seats,
                        "est_cpu": round(est_cpu, 3),
                        "est_mem_mb": round(est_mem, 1),
                        "enqueued_at": time.time(),
                    },
                )
            break
        except RegistryLost as exc:
            _warn(str(exc))
        except OSError as exc:
            _exit_if_deadline_passed(out, "waiting for the admission registry")
            _warn(f"registry unavailable ({exc}); running without host-wide admission")
            yield
            return
    _hold(lease, cfg)
    _CURRENT_RUN = lease
    sampler: _Sampler | None = None
    try:
        _wait_for_run_admission(
            lease, cfg, root, probe, out, mode, seats, est_cpu, est_mem
        )
        sampler = _Sampler(max(1.0, cfg.poll_seconds))
        sampler.start()
        yield
    finally:
        _CURRENT_RUN = None
        _drop(lease)
        if sampler is not None:
            measured = sampler.result()
            if measured is not None:
                # the lease's seat count, which `_grow` may have raised past `seats`
                _record_cost(
                    root, cfg, int(lease.data.get("seats") or seats), *measured
                )


def _peak_seats(*phases: list[str]) -> dict[str, int]:
    """Per-provider seats of sequential dispatches: the largest phase for each."""
    counts = [seats_by_provider(p) for p in phases]
    providers = {p for c in counts for p in c}
    return {p: max(c.get(p, 0) for c in counts) for p in providers}


def _grow(lease: Lease, cfg: Settings, *phases: list[str]) -> None:
    """A dispatch already holding this process's run slot (companion vision first, then
    the text panel and its moderator) needs more seats: raise the held lease's
    seats/estimates/provider reservations to cover them instead of queueing again behind
    itself."""
    need = _peak_seats(*phases)
    held = dict(lease.data.get("provider_seats") or {})
    merged = {p: max(held.get(p, 0), n) for p, n in {**held, **need}.items()}
    seats = max(int(lease.data.get("seats") or 1), *(len(p) for p in phases))
    if merged == held and seats == lease.data.get("seats"):
        return
    est_cpu, est_mem = run_estimate(cfg, seat_cost(cfg, lease.root), seats)
    lease.update(
        seats=seats,
        provider_seats=merged,
        providers=sorted(merged),
        est_cpu=round(est_cpu, 3),
        est_mem_mb=round(est_mem, 1),
    )


_SCOPE: ExitStack | None = None


@contextmanager
def dispatch_scope() -> Iterator[None]:
    """Bound for `hold_run_slot`: a run slot taken inside is released when this exits.
    Re-entrant (an inner scope defers to the outer one)."""
    global _SCOPE
    if _SCOPE is not None:
        yield
        return
    with ExitStack() as stack:
        _SCOPE = stack
        try:
            yield
        finally:
            _SCOPE = None


def hold_run_slot(
    *,
    mode: str,
    models: list[str],
    later_models: list[str] | None = None,
    final_models: list[str] | None = None,
    task_code: str | None = None,
) -> None:
    """Wait for this process's run slot NOW and keep it until the enclosing
    `dispatch_scope` ends — for work that precedes `_run_mode_with_stats` in the same
    dispatch (the companion vision fan-out). Pass the text panel that follows as
    `later_models` (and its moderator as `final_models`) so admission checks the whole
    run now. Outside any scope this is a no-op (nothing would release the slot)."""
    if _SCOPE is not None:
        _SCOPE.enter_context(
            run_slot(
                mode=mode,
                models=models,
                later_models=later_models,
                final_models=final_models,
                task_code=task_code,
            )
        )


def hold_in_dispatch(cm: AbstractContextManager[object]) -> bool:
    """Enter `cm` for the rest of the enclosing `dispatch_scope`; a run slot taken
    afterwards is released before it (LIFO). False (not entered) outside a scope."""
    if _SCOPE is None:
        return False
    _SCOPE.enter_context(cm)
    return True


def _wait_for_run_admission(
    lease, cfg, root, probe, out, mode, seats, est_cpu, est_mem
) -> None:
    from .backstop import BACKSTOP_EXIT_CODE, exempt_from_backstop
    from .process import run_deadline_remaining

    last_line = ""
    last_print = 0.0
    waited_from = time.monotonic()
    typical: float | None = None
    with exempt_from_backstop():
        while True:
            host = probe()  # /proc reads stay outside the registry lock
            now = time.time()
            try:
                with _registry_locked(root, cfg):
                    now = time.time()  # after a possibly contended acquire
                    entries = [
                        e for e in _scan(root, cfg, now) if e.get("kind") == "run"
                    ]
                    running = [
                        e
                        for e in entries
                        if e.get("state") == "running" and not e["stale"]
                    ]
                    waiting = [
                        e
                        for e in entries
                        if e.get("state") == "waiting" and not e["stale"]
                    ]
                    position = 1 + sum(
                        1 for e in waiting if e["seq"] < lease.data["seq"]
                    )
                    verdict = None
                    provider_block = ""
                    if position == 1:
                        verdict = machine_verdict(
                            cfg, host, running, est_cpu, est_mem, now
                        )
                        if verdict.admit:
                            state = _prune_provider_state(
                                _load_provider_state(root), cfg, now
                            )
                            provider_block = provider_verdict(
                                cfg, state, running, lease.data["provider_seats"]
                            )
                            if not provider_block:
                                _fence(root)
                                lease.update(state="running", admitted_at=now)
                                _record_admission(root, lease, now, len(running) + 1)
                                break
            except RegistryLost as exc:
                _warn(str(exc))
                continue
            except (
                OSError
            ) as exc:  # RegistryBusy, or the registry dir/fds gone mid-wait
                _exit_if_deadline_passed(out, "waiting for the admission registry")
                _warn(f"{exc}; running without host-wide admission")
                lease.update(state="running", admitted_at=now)
                running = []
                break
            if verdict is None:
                why = "queued behind earlier reviews (FIFO)"
            elif provider_block:
                why = provider_block
            elif verdict.reason in ("cpu", "memory"):
                mem = ""
                if verdict.mem_projected_mb is not None:
                    mem = (
                        f", mem {verdict.mem_projected_mb / 1024:.1f}/"
                        f"{verdict.mem_budget_mb / 1024:.1f} GiB"
                    )
                why = (
                    f"host budget full on {verdict.reason} (cpu {verdict.cpu_projected:.1f}/"
                    f"{verdict.cpu_budget:.1f}{mem}, headroom {cfg.headroom:.0%})"
                )
            else:
                why = verdict.reason
            if typical is None:  # a stats-file read: once per wait, not once per poll
                typical = _typical_run_seconds(mode, seats)
            eta = position * typical / max(1, len(running))
            line = (
                f"[review] admission: waiting — queue position {position}/{len(waiting)}, "
                f"{len(running)} running; {why}; ETA ~{_fmt_secs(eta)}"
            )
            mono = time.monotonic()
            if line != last_line or mono - last_print >= 60:
                print(line, file=out, flush=True)
                last_line, last_print = line, mono
                _job_note(admission="waiting", queue_position=position)
            # An explicit `just-ask --timeout N` bounds the queue wait too: give up at N
            # instead of idling until the N + grace watchdog.
            remaining = run_deadline_remaining()
            if remaining is not None and remaining <= 0:
                print(
                    f"[review] admission: --timeout reached after "
                    f"{_fmt_secs(time.monotonic() - waited_from)} still queued "
                    f"(position {position}/{len(waiting)}, {len(running)} running); "
                    f"no seat was started",
                    file=out,
                    flush=True,
                )
                raise SystemExit(BACKSTOP_EXIT_CODE)
            time.sleep(
                cfg.poll_seconds
                if remaining is None
                else max(0.01, min(cfg.poll_seconds, remaining))
            )
    waited = time.monotonic() - waited_from
    if last_line:
        print(
            f"[review] admission: admitted after {_fmt_secs(waited)} "
            f"({len(running) + 1} reviews running host-wide)",
            file=out,
            flush=True,
        )
    _job_note(admission="running", queue_position=0)


# ── seat (provider) admission ───────────────────────────────────────────────────────────


@contextmanager
def seat_slot(
    model: str,
    *,
    cfg: Settings | None = None,
    stream=None,
) -> Iterator[str | None]:
    """Hold a slot of `model`'s provider for one seat call. Yields None when admitted, or a
    reason string when the provider stayed saturated past `provider_wait_seconds` (the
    caller then returns an "unavailable" result so failover picks another seat)."""
    cfg = cfg or settings()
    if not cfg.enabled:
        yield None
        return
    out = stream if stream is not None else sys.stderr
    provider = provider_key(model)
    run = _CURRENT_RUN
    while True:
        try:
            root = registry_dir(cfg)
            with _registry_locked(root, cfg):
                lease = Lease(
                    root,
                    "seat",
                    _next_seq(root),
                    {
                        "state": "waiting",
                        "provider": provider,
                        "model": model,
                        "run": run.name if run is not None else "",
                        "enqueued_at": time.time(),
                    },
                )
            break
        except RegistryLost as exc:
            _warn(str(exc))
        except OSError as exc:
            from .process import run_deadline_expired

            if run_deadline_expired():
                reason = f"--timeout reached while waiting for a {provider} slot"
                print(f"[review] admission: {model}: {reason}", file=out, flush=True)
                yield reason
                return
            _warn(
                f"registry unavailable ({exc}); seat {model} runs without a provider slot"
            )
            yield None
            return
    _hold(lease, cfg)
    try:
        denied = _wait_for_seat(lease, cfg, root, provider, model, out)
        yield denied
    finally:
        _drop(lease)


def _wait_for_seat(lease, cfg, root, provider, model, out) -> str | None:
    from .backstop import exempt_from_backstop
    from .process import run_deadline_expired, run_deadline_remaining

    started = time.time()
    announced = False
    with exempt_from_backstop():
        while True:
            # An explicit `just-ask --timeout N` that passed while this seat queued: never
            # start a (paid) call after it, even if a slot just freed up.
            remaining = run_deadline_remaining()
            if remaining is not None and remaining <= 0:
                reason = f"--timeout reached while waiting for a {provider} slot"
                print(f"[review] admission: {model}: {reason}", file=out, flush=True)
                return reason
            now = time.time()
            try:
                with _registry_locked(root, cfg):
                    now = time.time()  # after a possibly contended acquire (rpm starts)
                    state = _prune_provider_state(_load_provider_state(root), cfg, now)
                    seats = [
                        e
                        for e in _scan(root, cfg, now)
                        if e.get("kind") == "seat"
                        and e.get("provider") == provider
                        and not e["stale"]
                    ]
                    running = sum(1 for e in seats if e.get("state") == "running")
                    ahead = sum(
                        1
                        for e in seats
                        if e.get("state") == "waiting" and e["seq"] < lease.data["seq"]
                    )
                    limit = effective_limit(cfg, provider, state)
                    rpm = rpm_limit(cfg, provider)
                    starts = state.get(provider, {}).get("starts", [])
                    if (
                        ahead == 0
                        and running < limit
                        and len(starts) < rpm
                        and not run_deadline_expired()
                    ):
                        _fence(root)
                        if (
                            rpm != math.inf
                        ):  # starts are only tracked where an rpm cap needs them
                            entry = state.setdefault(
                                provider, {"throttles": [], "starts": []}
                            )
                            entry["starts"].append(now)
                            _atomic_write(root / _PROVIDERS_FILE, state)
                        lease.update(state="running", admitted_at=now)
                        if announced:
                            print(
                                f"[review] admission: {model} got a {provider} slot "
                                f"after {_fmt_secs(now - started)}",
                                file=out,
                                flush=True,
                            )
                        return None
            except RegistryLost as exc:
                _warn(str(exc))
                continue
            except (
                OSError
            ) as exc:  # RegistryBusy, or the registry dir/fds gone mid-wait
                if run_deadline_expired():
                    reason = f"--timeout reached while waiting for a {provider} slot"
                    print(
                        f"[review] admission: {model}: {reason}", file=out, flush=True
                    )
                    return reason
                _warn(f"{exc}; seat {model} runs without a provider slot")
                lease.update(state="running", admitted_at=now)
                return None
            if cfg.provider_wait_seconds and now - started >= cfg.provider_wait_seconds:
                reason = (
                    f"provider {provider} saturated for {_fmt_secs(now - started)} "
                    f"({running}/{_fmt_limit(limit)} seats busy) — failing over"
                )
                print(f"[review] admission: {model}: {reason}", file=out, flush=True)
                return reason
            if not announced:
                rpm_txt = (
                    "" if rpm == math.inf else f", {len(starts)}/{int(rpm)} starts/min"
                )
                print(
                    f"[review] admission: {model} waiting for a {provider} slot "
                    f"({running}/{_fmt_limit(limit)} busy{rpm_txt}, {ahead} ahead)",
                    file=out,
                    flush=True,
                )
                announced = True
            pause = min(cfg.poll_seconds, 2.0)
            time.sleep(pause if remaining is None else max(0.01, min(pause, remaining)))


def _fmt_limit(limit: float) -> str:
    return "∞" if limit == math.inf else str(int(limit))


def note_result(model: str, result) -> None:
    """Throttle feedback from a finished seat call: a 429/overload lowers the provider's
    effective limit. Never raises."""
    try:
        cfg = settings()
        if not cfg.enabled:
            return
        from .retry import is_throttle

        if is_throttle(result):
            record_throttle(provider_key(model), cfg=cfg)
    except Exception:  # noqa: BLE001 — feedback must never break the seat
        pass


def note_failure_text(model: str, text: str | None) -> None:
    """Throttle feedback for transports that report a failure as plain text (vision
    verdict errors, the qa tester's stderr). Never raises."""
    try:
        cfg = settings()
        if not cfg.enabled or not text:
            return
        from .retry import text_is_throttle

        if text_is_throttle(text):
            record_throttle(provider_key(model), cfg=cfg)
    except Exception:  # noqa: BLE001 — feedback must never break the call
        pass


# ── status ──────────────────────────────────────────────────────────────────────────────


def recent_admissions(
    root: Path | None, now: float, window: float = _ADMISSIONS_WINDOW_SECONDS
) -> dict:
    """Queue wait and achieved parallelism of the runs admitted in the last `window`
    seconds, from admissions.jsonl. A run counts as queued once it waited 1 s or more (an
    immediate admission records ~0). The ledger is count-capped (_ADMISSIONS_KEEP, far
    above any day seen so far), so `complete` is False when it was trimmed past the
    window's start and the counts cover only its newest records."""
    waits: list[float] = []
    peak = 0
    records = _load_jsonl(root / _ADMISSIONS_FILE) if root is not None else []
    oldest = math.inf
    for item in records:
        try:
            ts = float(item["ts"])
            wait = float(item["queued_seconds"])
            running = int(item["running"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue  # a garbled or hand-edited line never breaks `review jobs`
        if not (math.isfinite(ts) and math.isfinite(wait)) or wait < 0 or running < 0:
            continue  # NaN/Infinity parse as floats; they would crash or skew the summary
        oldest = min(oldest, ts)
        if not 0 <= now - ts <= window:
            continue  # older than the window, or future-dated (clock skew, hand edit)
        waits.append(wait)
        peak = max(peak, running)
    summary: dict = {
        "window_hours": round(window / 3600, 1),
        "admitted": len(waits),
        "queued": sum(1 for w in waits if w >= 1.0),
        "peak_running": peak,
        # fewer lines than a trim leaves -> never trimmed; else the oldest kept record
        # must predate the window for the window to be fully covered
        "complete": len(records) < _ADMISSIONS_KEEP or oldest <= now - window,
    }
    if waits:
        summary.update(
            wait_p50_seconds=_percentile(waits, 0.5),
            wait_p90_seconds=_percentile(waits, 0.9),
            wait_max_seconds=max(waits),
        )
    return summary


def _provider_exclusions() -> dict:
    """Provider name -> `availability.Exclusion` for every provider whose quota/credits an
    earlier failure showed exhausted; shown by `review jobs` as an exclusion with its end
    time, not as a throttle. Never raises."""
    try:
        from . import availability

        return availability.provider_exclusions()
    except Exception:  # noqa: BLE001 — a display aid must never break `review jobs`
        return {}


def status_snapshot(
    cfg: Settings | None = None, probe: Callable[[], HostSnapshot] = probe_host
) -> dict:
    """Everything `review jobs` shows about admission, as plain data."""
    cfg = cfg or settings()
    try:
        root: Path | None = registry_dir(cfg)
    except OSError:
        root = None
    now = time.time()
    host = probe()
    snap: dict = {
        "enabled": cfg.enabled,
        "dir": str(root) if root is not None else "unavailable (no state dir)",
        "headroom": cfg.headroom,
        "host": {
            "cpus": host.cpus,
            "load1": round(host.load1, 2),
            "cpu_budget": round((1 - cfg.headroom) * host.cpus, 2),
            "mem_total_mb": None
            if host.mem_total_mb is None
            else round(host.mem_total_mb),
            "mem_available_mb": None
            if host.mem_available_mb is None
            else round(host.mem_available_mb),
            "mem_budget_mb": None
            if host.mem_total_mb is None
            else round((1 - cfg.headroom) * host.mem_total_mb),
            "mem_pressure": host.mem_pressure,
            "max_memory_pressure": cfg.max_memory_pressure,
        },
        "runs": [],
        "providers": {},
        "recent": recent_admissions(root, now),
    }
    if not cfg.enabled or root is None or not root.is_dir():
        return snap
    try:
        with _registry_locked(root, cfg):
            entries = _scan(root, cfg, now)
            state = _prune_provider_state(_load_provider_state(root), cfg, now)
            cost = seat_cost(cfg, root)
    except OSError:
        return snap
    snap["seat_cost"] = {
        "cpu": round(cost.cpu, 3),
        "mem_mb": round(cost.mem_mb),
        "source": cost.source,
    }
    waiting_rank = 0
    for e in entries:
        if e.get("kind") != "run":
            continue
        if e.get("state") == "waiting" and not e["stale"]:
            waiting_rank += 1
        snap["runs"].append(
            {
                "seq": e.get("seq"),
                "pid": e.get("pid"),
                "state": "stale" if e["stale"] else e.get("state"),
                "queue_position": waiting_rank
                if e.get("state") == "waiting" and not e["stale"]
                else None,
                "mode": e.get("mode"),
                "task": e.get("task"),
                "seats": e.get("seats"),
                "est_cpu": e.get("est_cpu"),
                "est_mem_mb": e.get("est_mem_mb"),
                "since": e.get("admitted_at") or e.get("enqueued_at"),
                "cwd": e.get("cwd"),
            }
        )
    providers = (
        {e.get("provider") for e in entries if e.get("kind") == "seat"}
        | set(state)
        | set(cfg.providers)
        | set(_provider_exclusions())
    )
    excluded = _provider_exclusions()
    for p in sorted(x for x in providers if x):
        seats = [
            e
            for e in entries
            if e.get("kind") == "seat" and e.get("provider") == p and not e["stale"]
        ]
        limit = effective_limit(cfg, p, state)
        snap["providers"][p] = {
            "running": sum(1 for e in seats if e.get("state") == "running"),
            "waiting": sum(1 for e in seats if e.get("state") == "waiting"),
            "limit": None if limit == math.inf else int(limit),
            "configured_limit": None
            if configured_limit(cfg, p) == math.inf
            else int(configured_limit(cfg, p)),
            "rpm_limit": None
            if rpm_limit(cfg, p) == math.inf
            else int(rpm_limit(cfg, p)),
            "starts_last_minute": len(state.get(p, {}).get("starts", [])),
            "throttled": bool(state.get(p, {}).get("throttles")),
            "excluded_until": excluded[p].until if p in excluded else None,
            "excluded_reason": (
                f"EXCLUDED, {excluded[p].describe()}" if p in excluded else None
            ),
        }
    return snap


def render_status(snap: dict) -> str:
    if not snap.get("enabled"):
        return "ADMISSION: disabled (admission.enabled / $REVIEW_ADMISSION) — reviews start immediately"
    h = snap["host"]
    lines = [
        f"ADMISSION (host-wide, headroom {snap['headroom']:.0%}; registry {snap['dir']})"
    ]
    mem = ""
    if h.get("mem_total_mb") is not None and h.get("mem_available_mb") is not None:
        used = h["mem_total_mb"] - h["mem_available_mb"]
        mem = f"; mem used {used / 1024:.1f}/{h['mem_budget_mb'] / 1024:.1f} GiB budget"
    if h.get("mem_pressure") is not None:
        limit = h.get("max_memory_pressure")
        bound = f"/{limit:g}%" if limit else " (check off)"
        mem += f"; memory pressure {h['mem_pressure']:.1f}%{bound}"
    lines.append(
        f"  host: load {h['load1']}/{h['cpu_budget']} cpu budget ({h['cpus']} cpus){mem}"
    )
    cost = snap.get("seat_cost")
    if cost:
        lines.append(
            f"  per-seat estimate: {cost['cpu']} cpu, {cost['mem_mb']} MiB ({cost['source']})"
        )
    runs = snap.get("runs") or []
    running = [r for r in runs if r["state"] == "running"]
    waiting = [r for r in runs if r["state"] == "waiting"]
    lines.append(f"  reviews: {len(running)} running, {len(waiting)} waiting")
    now = time.time()
    for r in runs:
        tag = (
            r["state"] if r["state"] != "waiting" else f"waiting #{r['queue_position']}"
        )
        age = _fmt_secs(now - float(r.get("since") or now))
        lines.append(
            f"    {tag:<11} pid={r['pid']} mode={r['mode']} task={r['task'] or '-'} "
            f"seats={r['seats']} est={r['est_cpu']}cpu/{r['est_mem_mb']}MiB for {age}"
        )
    recent = snap.get("recent")
    if recent:
        line = (
            f"  last {recent['window_hours']:g}h: {recent['admitted']} admitted, "
            f"{recent['queued']} queued"
        )
        if recent["admitted"]:
            line += (
                f" (wait p50 {_fmt_secs(recent['wait_p50_seconds'])}, "
                f"p90 {_fmt_secs(recent['wait_p90_seconds'])}, "
                f"max {_fmt_secs(recent['wait_max_seconds'])}); "
                f"peak {recent['peak_running']} running at once"
            )
        if not recent.get("complete", True):
            line += f" — ledger trimmed, only its newest {_ADMISSIONS_KEEP}+ records counted"
        lines.append(line)
    if snap.get("providers"):
        lines.append("  provider seats (running/limit, waiting):")
        for name, p in snap["providers"].items():
            limit = "∞" if p["limit"] is None else p["limit"]
            extra = " THROTTLED" if p["throttled"] else ""
            if p.get("excluded_until"):
                extra = f" {p['excluded_reason']}"
            rpm = (
                ""
                if p["rpm_limit"] is None
                else f", {p['starts_last_minute']}/{p['rpm_limit']} starts/min"
            )
            lines.append(
                f"    {name:<14} {p['running']}/{limit}, {p['waiting']} waiting{rpm}{extra}"
            )
    return "\n".join(lines)
