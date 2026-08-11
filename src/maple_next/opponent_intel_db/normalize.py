"""Normalized dataclasses for opponent usage statistics + strict JSON codec.

``SpeciesStatsRecord`` is the canonical in-memory shape produced by the
per-source parsers (``parser_pokechamdb.py`` / ``parser_champs_pokedb.py``)
after they've been validated and merged. Conversion to/from JSON is strict:
malformed input raises :class:`NormalizationError` rather than silently
coercing bad data into a record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class NormalizationError(ValueError):
    """Raised when parser output or a JSON document cannot be normalized."""


@dataclass(frozen=True, slots=True)
class RankedEntry:
    name: str
    percentage: float | None

    def to_json_dict(self) -> dict[str, Any]:
        return {"name": self.name, "percentage": self.percentage}

    @staticmethod
    def from_json_dict(data: Any) -> RankedEntry:
        if not isinstance(data, dict):
            raise NormalizationError(f"RankedEntry must be an object, got {type(data).__name__}")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise NormalizationError(f"RankedEntry.name must be a non-empty string, got {name!r}")
        percentage = data.get("percentage")
        if percentage is not None and not isinstance(percentage, (int, float)):
            raise NormalizationError(
                f"RankedEntry.percentage must be numeric or null, got {percentage!r}"
            )
        return RankedEntry(name=name, percentage=None if percentage is None else float(percentage))


def _ranked_entries_from_json(data: Any, *, field_name: str) -> tuple[RankedEntry, ...]:
    if not isinstance(data, list):
        raise NormalizationError(f"{field_name} must be a list, got {type(data).__name__}")
    return tuple(RankedEntry.from_json_dict(item) for item in data)


def _dicts_from_json(data: Any, *, field_name: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(data, list):
        raise NormalizationError(f"{field_name} must be a list, got {type(data).__name__}")
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise NormalizationError(
                f"{field_name} entries must be objects, got {type(item).__name__}"
            )
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SpeciesStatsRecord:
    species_id: str
    display_name: str
    season: str
    format: str
    source: str
    source_url: str
    source_updated_at: str | None
    fetched_at: str
    ranking: float | None
    moves: tuple[RankedEntry, ...] = field(default_factory=tuple)
    items: tuple[RankedEntry, ...] = field(default_factory=tuple)
    abilities: tuple[RankedEntry, ...] = field(default_factory=tuple)
    natures: tuple[RankedEntry, ...] = field(default_factory=tuple)
    partners: tuple[RankedEntry, ...] = field(default_factory=tuple)
    spreads: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "species_id": self.species_id,
            "display_name": self.display_name,
            "season": self.season,
            "format": self.format,
            "source": self.source,
            "source_url": self.source_url,
            "source_updated_at": self.source_updated_at,
            "fetched_at": self.fetched_at,
            "ranking": self.ranking,
            "moves": [entry.to_json_dict() for entry in self.moves],
            "items": [entry.to_json_dict() for entry in self.items],
            "abilities": [entry.to_json_dict() for entry in self.abilities],
            "natures": [entry.to_json_dict() for entry in self.natures],
            "partners": [entry.to_json_dict() for entry in self.partners],
            "spreads": list(self.spreads),
        }

    @staticmethod
    def from_json_dict(data: Any) -> SpeciesStatsRecord:
        if not isinstance(data, dict):
            raise NormalizationError(
                f"SpeciesStatsRecord must be an object, got {type(data).__name__}"
            )

        required_strings = (
            "species_id",
            "display_name",
            "season",
            "format",
            "source",
            "source_url",
            "fetched_at",
        )
        values: dict[str, str] = {}
        for key in required_strings:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise NormalizationError(
                    f"SpeciesStatsRecord.{key} must be a non-empty string, got {value!r}"
                )
            values[key] = value

        source_updated_at = data.get("source_updated_at")
        if source_updated_at is not None and not isinstance(source_updated_at, str):
            raise NormalizationError(
                f"SpeciesStatsRecord.source_updated_at must be a string or null, "
                f"got {source_updated_at!r}"
            )

        ranking = data.get("ranking")
        if ranking is not None and not isinstance(ranking, (int, float)):
            raise NormalizationError(
                f"SpeciesStatsRecord.ranking must be numeric or null, got {ranking!r}"
            )

        return SpeciesStatsRecord(
            species_id=values["species_id"],
            display_name=values["display_name"],
            season=values["season"],
            format=values["format"],
            source=values["source"],
            source_url=values["source_url"],
            source_updated_at=source_updated_at,
            fetched_at=values["fetched_at"],
            ranking=None if ranking is None else float(ranking),
            moves=_ranked_entries_from_json(data.get("moves", []), field_name="moves"),
            items=_ranked_entries_from_json(data.get("items", []), field_name="items"),
            abilities=_ranked_entries_from_json(data.get("abilities", []), field_name="abilities"),
            natures=_ranked_entries_from_json(data.get("natures", []), field_name="natures"),
            partners=_ranked_entries_from_json(data.get("partners", []), field_name="partners"),
            spreads=_dicts_from_json(data.get("spreads", []), field_name="spreads"),
        )


def species_record_from_parsed(parsed: dict[str, Any]) -> SpeciesStatsRecord:
    """Convert one parser-produced species dict into a validated record.

    Parsers (``parser_pokechamdb.py`` / ``parser_champs_pokedb.py``) hand
    back plain dicts shaped like the JSON encoding above; this simply routes
    through the same strict validation as ``from_json_dict`` so a single
    malformed species can be skipped by the caller (catch
    ``NormalizationError``) without aborting the whole import.
    """

    return SpeciesStatsRecord.from_json_dict(parsed)
