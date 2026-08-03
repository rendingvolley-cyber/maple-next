"""Pokemon Champions detailed team-build value objects.

The module is deliberately independent of persistence, the UI, and provider
transport.  A detailed build is an immutable canonical snapshot: callers may
construct drafts in the UI, but only a fully valid instance can cross a
persistence or provider boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

CHAMPIONS_SCHEMA_VERSION: Final[str] = "maple-team.v2"
CHAMPIONS_GAME: Final[str] = "pokemon-champions"
CHAMPIONS_BATTLE_FORMAT: Final[str] = "SINGLE_3"
CHAMPIONS_STAT_POINT_PER_STAT_MAX: Final[int] = 32
CHAMPIONS_STAT_POINT_TOTAL_MAX: Final[int] = 66
CHAMPIONS_STAT_NAMES: Final[tuple[str, ...]] = (
    "hp",
    "attack",
    "defense",
    "special_attack",
    "special_defense",
    "speed",
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _require_stat(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if not 0 <= value <= CHAMPIONS_STAT_POINT_PER_STAT_MAX:
        raise ValueError(
            f"{field_name} must be between 0 and "
            f"{CHAMPIONS_STAT_POINT_PER_STAT_MAX}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ChampionsStatPoints:
    """The six integer stat-point allocations used by Pokemon Champions."""

    hp: int = 0
    attack: int = 0
    defense: int = 0
    special_attack: int = 0
    special_defense: int = 0
    speed: int = 0

    def __post_init__(self) -> None:
        for field_name in CHAMPIONS_STAT_NAMES:
            value = _require_stat(getattr(self, field_name), field_name)
            # ``dataclass(frozen=True)`` does not coerce values.  The local
            # binding above is intentionally only a validation step: values
            # are retained exactly as supplied after strict type checking.
            if value != getattr(self, field_name):  # pragma: no cover
                raise ValueError(f"{field_name} must be canonical")
        if self.total > CHAMPIONS_STAT_POINT_TOTAL_MAX:
            raise ValueError(
                "total stat points must be at most "
                f"{CHAMPIONS_STAT_POINT_TOTAL_MAX}"
            )

    @property
    def total(self) -> int:
        return sum(getattr(self, field_name) for field_name in CHAMPIONS_STAT_NAMES)

    @property
    def remaining(self) -> int:
        return CHAMPIONS_STAT_POINT_TOTAL_MAX - self.total

    def to_canonical_dict(self) -> dict[str, int]:
        return {field_name: getattr(self, field_name) for field_name in CHAMPIONS_STAT_NAMES}

    @classmethod
    def from_dict(cls, payload: object) -> ChampionsStatPoints:
        if not isinstance(payload, dict) or set(payload) != set(CHAMPIONS_STAT_NAMES):
            raise ValueError("stat_points fields are invalid")
        return cls(
            **{
                field_name: _require_stat(payload[field_name], field_name)
                for field_name in CHAMPIONS_STAT_NAMES
            }
        )


@dataclass(frozen=True, slots=True)
class ChampionsPokemonBuild:
    """One fully specified Pokemon Champions member build."""

    pokemon_name: str
    moves: tuple[str, ...]
    held_item: str | None
    ability: str
    nature: str
    stat_points: ChampionsStatPoints

    def __post_init__(self) -> None:
        pokemon_name = _require_text(self.pokemon_name, "pokemon_name")
        if pokemon_name != self.pokemon_name:
            object.__setattr__(self, "pokemon_name", pokemon_name)

        if not isinstance(self.moves, tuple):
            object.__setattr__(self, "moves", tuple(self.moves))
        if not 1 <= len(self.moves) <= 4:
            raise ValueError("moves must contain between 1 and 4 entries")
        normalized_moves = tuple(_require_text(move, "move") for move in self.moves)
        if len(set(normalized_moves)) != len(normalized_moves):
            raise ValueError("moves must be unique")
        if normalized_moves != self.moves:
            object.__setattr__(self, "moves", normalized_moves)

        held_item = self.held_item
        if held_item == "\u306a\u3057":
            held_item = None
        if held_item == "なし":
            held_item = None
        elif held_item is not None:
            held_item = _require_text(held_item, "held_item")
        if held_item != self.held_item:
            object.__setattr__(self, "held_item", held_item)

        ability = _require_text(self.ability, "ability")
        nature = _require_text(self.nature, "nature")
        if ability != self.ability:
            object.__setattr__(self, "ability", ability)
        if nature != self.nature:
            object.__setattr__(self, "nature", nature)
        if not isinstance(self.stat_points, ChampionsStatPoints):
            raise ValueError("stat_points must be ChampionsStatPoints")

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the deterministic member object; no tera field exists."""

        return {
            "pokemon": self.pokemon_name,
            "moves": list(self.moves),
            "held_item": self.held_item,
            "ability": self.ability,
            "nature": self.nature,
            "stat_points": self.stat_points.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> ChampionsPokemonBuild:
        if not isinstance(payload, dict):
            raise ValueError("member must be an object")
        expected = {"pokemon", "moves", "held_item", "ability", "nature", "stat_points"}
        if set(payload) != expected:
            raise ValueError("member fields are invalid")
        raw_moves = payload["moves"]
        if not isinstance(raw_moves, list):
            raise ValueError("moves must be an array")
        raw_stats = payload["stat_points"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(CHAMPIONS_STAT_NAMES):
            raise ValueError("stat_points fields are invalid")
        stats = ChampionsStatPoints.from_dict(raw_stats)
        held_item = payload["held_item"]
        if held_item is not None and (
            not isinstance(held_item, str) or isinstance(held_item, bool)
        ):
            raise ValueError("held_item must be a string or null")
        return cls(
            pokemon_name=payload["pokemon"],
            moves=tuple(raw_moves),
            held_item=held_item,
            ability=payload["ability"],
            nature=payload["nature"],
            stat_points=stats,
        )

    from_canonical_dict = from_dict


@dataclass(frozen=True, slots=True)
class ChampionsTeamBuild:
    """A strict six-member Pokemon Champions v2 team build."""

    schema_version: str
    game: str
    name: str
    battle_format: str
    members: tuple[ChampionsPokemonBuild, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CHAMPIONS_SCHEMA_VERSION:
            raise ValueError("schema_version must be maple-team.v2")
        if self.game != CHAMPIONS_GAME:
            raise ValueError("game must be pokemon-champions")
        if self.battle_format != CHAMPIONS_BATTLE_FORMAT:
            raise ValueError("battle_format must be SINGLE_3")
        name = _require_text(self.name, "name")
        if name != self.name:
            object.__setattr__(self, "name", name)
        if not isinstance(self.members, tuple):
            object.__setattr__(self, "members", tuple(self.members))
        if len(self.members) != 6:
            raise ValueError("members must contain exactly six entries")
        if not all(isinstance(member, ChampionsPokemonBuild) for member in self.members):
            raise ValueError("members must contain ChampionsPokemonBuild entries")
        names = tuple(member.pokemon_name for member in self.members)
        if len(set(names)) != 6:
            raise ValueError("pokemon names must be unique")

    @property
    def pokemon_names(self) -> tuple[str, str, str, str, str, str]:
        names = tuple(member.pokemon_name for member in self.members)
        return (names[0], names[1], names[2], names[3], names[4], names[5])

    def member_by_name(self, pokemon_name: str) -> ChampionsPokemonBuild:
        for member in self.members:
            if member.pokemon_name == pokemon_name:
                return member
        raise KeyError(pokemon_name)

    def selected_members(
        self,
        selected_names: Sequence[str] | str,
        *additional_names: str,
    ) -> tuple[ChampionsPokemonBuild, ...]:
        if additional_names:
            if not isinstance(selected_names, str):
                raise ValueError("selected members must contain exactly three names")
            selected_values: Sequence[str] = (selected_names, *additional_names)
        elif isinstance(selected_names, str):
            raise ValueError("selected members must contain exactly three names")
        else:
            selected_values = selected_names
        if len(selected_values) != 3 or len(set(selected_values)) != 3:
            raise ValueError("selected members must contain exactly three unique names")
        try:
            return tuple(self.member_by_name(name) for name in selected_values)
        except KeyError as exc:
            raise ValueError("selected member is outside the team") from exc

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "game": self.game,
            "name": self.name,
            "battle_format": self.battle_format,
            "members": [member.to_canonical_dict() for member in self.members],
        }

    def canonical_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def canonical_bytes(self) -> bytes:
        return self.canonical_json_bytes()

    def canonical_json(self) -> str:
        return self.canonical_json_bytes().decode("utf-8")

    def to_canonical_json(self) -> str:
        return self.canonical_json()

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json_bytes()).hexdigest()

    def sha256_hex(self) -> str:
        return self.sha256()

    @property
    def team_build_sha256(self) -> str:
        return self.sha256()

    def unspent_point_warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        for member in self.members:
            remaining = member.stat_points.remaining
            if remaining > 0:
                warnings.append(f"{member.pokemon_name}: {remaining} stat points unspent")
        return tuple(warnings)

    @property
    def unspent_warnings(self) -> tuple[str, ...]:
        return self.unspent_point_warnings()

    @classmethod
    def from_dict(cls, payload: object) -> ChampionsTeamBuild:
        if not isinstance(payload, dict):
            raise ValueError("team build must be an object")
        expected = {"schema_version", "game", "name", "battle_format", "members"}
        if set(payload) != expected:
            raise ValueError("team build fields are invalid")
        raw_members = payload["members"]
        if not isinstance(raw_members, list):
            raise ValueError("members must be an array")
        return cls(
            schema_version=payload["schema_version"],
            game=payload["game"],
            name=payload["name"],
            battle_format=payload["battle_format"],
            members=tuple(ChampionsPokemonBuild.from_dict(member) for member in raw_members),
        )

    from_canonical_dict = from_dict

    @classmethod
    def from_json_bytes(cls, value: bytes) -> ChampionsTeamBuild:
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid team build JSON") from exc
        return cls.from_dict(payload)

    @classmethod
    def from_json(cls, value: str) -> ChampionsTeamBuild:
        return cls.from_json_bytes(value.encode("utf-8"))


def build_champions_team(
    *, name: str, members: tuple[ChampionsPokemonBuild, ...] | list[ChampionsPokemonBuild]
) -> ChampionsTeamBuild:
    """Convenience constructor for callers that should not repeat constants."""

    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name=name,
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(members),
    )
