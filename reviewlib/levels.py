"""Capability levels: the shipped defaults (`levels.yaml`) merged with the user's `levels:`.

WHAT: a *level* names how much review a task needs (routine / standard / deep by default):
pool size, reasoning effort, a provider-neutral roster (role -> priority-ordered seats),
the quorum/brainstorm moderator chain and the next heavier level. Every concrete model and
provider route lives in the version-controlled `levels.yaml` next to this file plus the
user's `levels:` override; this module only loads, validates, merges and resolves them.

MERGE (user over shipped): mappings merge key by key, a list or scalar replaces, and
`null` deletes a key. So `levels: {routine: {effort: medium}}` changes one field and
`levels: {routine: {roles: {tests: null}}}` drops one role. A level name the shipped file
does not know is a NEW level and must be complete.

WHY a separate module with no `config` import: `config` builds `BoardReviewer` seats FROM
these levels, so the dependency points one way. Seat/alias expansion and role validation
(which need `config`'s alias table and `REVIEW_ROLES`) therefore live in `config.py`.
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass
from pathlib import Path

SHIPPED_LEVELS_PATH = Path(__file__).with_name("levels.yaml")

EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})

_LEVEL_KEYS = frozenset(
    {"aliases", "pool", "effort", "escalate_to", "roles", "moderators"}
)
_SEAT_KEYS = frozenset({"model", "name", "effort"})


class LevelError(ValueError):
    """A shipped or user `levels:` definition is invalid; the message names the key."""


@dataclass(frozen=True)
class LevelSeat:
    model: str
    name: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class Level:
    name: str
    aliases: tuple[str, ...]
    pool: int
    effort: str
    escalate_to: str | None
    roles: dict[str, tuple[LevelSeat, ...]]
    moderators: tuple[str, ...]


@dataclass(frozen=True)
class LevelSet:
    """Every level in force (shipped merged with the user's), plus the default one."""

    levels: dict[str, Level]
    default: str

    def names(self) -> tuple[str, ...]:
        return tuple(self.levels)

    def get(self, name: str) -> Level:
        """The level called `name` or carrying it as an alias (`light` -> routine)."""
        key = name.strip().lower() if isinstance(name, str) else name
        for level in self.levels.values():
            if key == level.name or key in level.aliases:
                return level
        known = ", ".join(
            f"{lv.name} (alias {', '.join(lv.aliases)})" if lv.aliases else lv.name
            for lv in self.levels.values()
        )
        raise LevelError(f"unknown level {name!r}; known levels: {known}")


@functools.lru_cache(maxsize=1)
def _shipped_raw() -> dict:
    import yaml

    try:
        data = yaml.safe_load(SHIPPED_LEVELS_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LevelError(
            f"shipped levels file {SHIPPED_LEVELS_PATH} is unreadable: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("levels"), dict):
        raise LevelError(
            f"shipped levels file {SHIPPED_LEVELS_PATH} has no `levels:` mapping"
        )
    return data


def _merge(base: object, override: object) -> object:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    merged = {k: copy.deepcopy(v) for k, v in base.items()}
    for key, value in override.items():
        if value is None and key in ("roles", "moderators", "aliases"):
            raise LevelError(
                f"`{key}` cannot be deleted from a level; override it with a value"
            )
        if value is None:
            merged.pop(key, None)
        elif key in merged:
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _seat(value: object, where: str) -> LevelSeat:
    if isinstance(value, str) and value.strip():
        return LevelSeat(model=value.strip())
    if not isinstance(value, dict):
        raise LevelError(
            f"{where}: a seat is a model string or a mapping, got {value!r}"
        )
    unknown = sorted(set(value) - _SEAT_KEYS)
    if unknown:
        raise LevelError(
            f"{where}: unknown seat key {unknown[0]!r}; allowed: model, name, effort"
        )
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        raise LevelError(
            f"{where}.model: must be a non-empty model string, got {model!r}"
        )
    name = value.get("name")
    if name is not None and not (isinstance(name, str) and name.strip()):
        raise LevelError(f"{where}.name: must be a non-empty string, got {name!r}")
    effort = value.get("effort")
    if effort is not None:
        effort = _effort(effort, f"{where}.effort")
    return LevelSeat(
        model=model.strip(), name=name.strip() if name else None, effort=effort
    )


def _effort(value: object, where: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in EFFORT_LEVELS:
        raise LevelError(
            f"{where}: unsupported effort {value!r}; expected one of "
            f"{', '.join(sorted(EFFORT_LEVELS))}"
        )
    return value.strip().lower()


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(x, str) and x.strip() for x in value)
    ):
        raise LevelError(f"{where}: must be a non-empty list of strings, got {value!r}")
    return tuple(x.strip() for x in value)


def _build_level(name: str, raw: object) -> Level:
    where = f"levels.{name}"
    if not isinstance(raw, dict):
        raise LevelError(f"{where}: must be a mapping, got {raw!r}")
    unknown = sorted(set(raw) - _LEVEL_KEYS)
    if unknown:
        raise LevelError(
            f"{where}.{unknown[0]}: unknown key; allowed: {', '.join(sorted(_LEVEL_KEYS))}"
        )
    for required in ("pool", "effort", "roles", "moderators"):
        if required not in raw:
            raise LevelError(
                f"{where}.{required}: required (a new level must be complete)"
            )
    pool = raw["pool"]
    if isinstance(pool, bool) or not isinstance(pool, int) or pool < 1:
        raise LevelError(f"{where}.pool: must be an integer >= 1, got {pool!r}")
    escalate_to = raw.get("escalate_to")
    if escalate_to is not None and not (
        isinstance(escalate_to, str) and escalate_to.strip()
    ):
        raise LevelError(
            f"{where}.escalate_to: must be a level name or null, got {escalate_to!r}"
        )
    roles_raw = raw["roles"]
    if not isinstance(roles_raw, dict) or not roles_raw:
        raise LevelError(f"{where}.roles: must be a non-empty mapping of role -> seats")
    roles: dict[str, tuple[LevelSeat, ...]] = {}
    for role, seats in roles_raw.items():
        if not isinstance(seats, list) or not seats:
            raise LevelError(f"{where}.roles.{role}: must be a non-empty list of seats")
        roles[str(role)] = tuple(
            _seat(s, f"{where}.roles.{role}[{i}]") for i, s in enumerate(seats)
        )
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or not all(
        isinstance(a, str) and a.strip() for a in aliases
    ):
        raise LevelError(f"{where}.aliases: must be a list of names, got {aliases!r}")
    return Level(
        name=name,
        aliases=tuple(a.strip().lower() for a in aliases),
        pool=pool,
        effort=_effort(raw["effort"], f"{where}.effort"),
        escalate_to=escalate_to.strip() if escalate_to else None,
        roles=roles,
        moderators=_string_list(raw["moderators"], f"{where}.moderators"),
    )


def resolve_levels(config: dict | None = None) -> LevelSet:
    """The shipped levels with `config`'s `levels:` / `default_level:` merged over them.
    `config=None` (or no such keys) is exactly the shipped defaults. Raises `LevelError`
    naming the offending key."""
    shipped = _shipped_raw()
    raw_levels = copy.deepcopy(shipped["levels"])
    user = (config or {}).get("levels")
    if user is not None:
        if not isinstance(user, dict):
            raise LevelError(
                f"levels: must be a mapping of level name -> overrides, got {user!r}"
            )
        for name, override in user.items():
            if not isinstance(override, dict):
                raise LevelError(f"levels.{name}: must be a mapping, got {override!r}")
            raw_levels[name] = _merge(raw_levels.get(name, {}), override)
    levels = {
        str(name): _build_level(str(name), raw) for name, raw in raw_levels.items()
    }
    seen: dict[str, str] = {}
    for level in levels.values():
        for label in (level.name, *level.aliases):
            if label in seen and seen[label] != level.name:
                raise LevelError(
                    f"levels.{level.name}: name/alias {label!r} is already used by level "
                    f"{seen[label]!r}"
                )
            seen[label] = level.name
    for level in levels.values():
        if level.escalate_to is not None and level.escalate_to not in levels:
            raise LevelError(
                f"levels.{level.name}.escalate_to: unknown level {level.escalate_to!r}; "
                f"known levels: {', '.join(levels)}"
            )
    default = (config or {}).get("default_level", shipped.get("default_level"))
    level_set = LevelSet(levels=levels, default="")
    try:
        default_name = level_set.get(default).name if isinstance(default, str) else None
    except LevelError as exc:
        raise LevelError(f"default_level: {exc}") from exc
    if default_name is None:
        raise LevelError(f"default_level: must be a level name, got {default!r}")
    return LevelSet(levels=levels, default=default_name)
