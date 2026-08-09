"""Offline opponent INTEL contracts for Battle Record v5.

The battle runtime receives a local-cache provider.  This module contains no
HTTP client and cannot fetch population data during a turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from maple_next.domain.species_ability_catalog import (
    UNKNOWN_ABILITY_LABEL,
    canonical_species_ability_catalog,
)

UNKNOWN_ABILITY = UNKNOWN_ABILITY_LABEL


def possible_abilities_for_species(species: str) -> tuple[str, ...]:
    """Return only canonical legal abilities for one resolved species."""

    return tuple(
        ability.display_name
        for ability in canonical_species_ability_catalog().legal_abilities(species)
    )


def possible_ability_ids_for_species(species: str) -> tuple[str, ...]:
    """Canonical legal ability IDs, independent from localized presentation."""

    return tuple(
        ability.ability_id
        for ability in canonical_species_ability_catalog().legal_abilities(species)
    )


def species_has_entry_relevant_ability(species: str) -> bool:
    """Whether a legal ability can visibly mutate supported state on entry."""

    return canonical_species_ability_catalog().species_has_entry_observable_ability(species)


@dataclass(frozen=True, slots=True)
class RankedUsage:
    name: str
    percentage: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ranked usage name must be explicit")
        if self.percentage is not None and not 0 <= self.percentage <= 100:
            raise ValueError("usage percentage must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class OpponentMetaSnapshot:
    species: str
    regulation: str
    snapshot_date: str
    source: str
    moves: tuple[RankedUsage, ...] = ()
    abilities: tuple[RankedUsage, ...] = ()
    items: tuple[RankedUsage, ...] = ()


class OpponentMetaProvider(Protocol):
    """Read-only local boundary. Implementations must not use the network."""

    def get(self, species: str) -> OpponentMetaSnapshot | None: ...


class LocalJsonOpponentMetaProvider:
    """Loads an optional regulation-tagged JSON cache once from disk."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self._entries: dict[str, OpponentMetaSnapshot] = {}
        if cache_path is not None and cache_path.is_file():
            self._entries = self._load(cache_path)

    @staticmethod
    def _ranked(raw: object) -> tuple[RankedUsage, ...]:
        if not isinstance(raw, list):
            return ()
        result: list[RankedUsage] = []
        for item in raw:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                continue
            percentage_raw = item.get("percentage")
            percentage = float(percentage_raw) if percentage_raw is not None else None
            result.append(RankedUsage(str(item["name"]), percentage))
        return tuple(result)

    @classmethod
    def _load(cls, path: Path) -> dict[str, OpponentMetaSnapshot]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("species"), dict):
            return {}
        result: dict[str, OpponentMetaSnapshot] = {}
        regulation = str(raw.get("regulation", ""))
        snapshot_date = str(raw.get("snapshot_date", ""))
        source = str(raw.get("source", ""))
        for species, entry in raw["species"].items():
            if not isinstance(entry, dict):
                continue
            result[str(species)] = OpponentMetaSnapshot(
                species=str(species),
                regulation=regulation,
                snapshot_date=snapshot_date,
                source=source,
                moves=cls._ranked(entry.get("moves")),
                abilities=cls._ranked(entry.get("abilities")),
                items=cls._ranked(entry.get("items")),
            )
        return result

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        return self._entries.get(species.strip())


@dataclass(frozen=True, slots=True)
class MatchOpponentFacts:
    ability: str | None = None
    item: str | None = None
    moves: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpponentIntelView:
    species: str
    ability: str
    item: str
    observed_moves: tuple[str, ...]
    possible_abilities: tuple[str, ...]
    meta: OpponentMetaSnapshot | None

    @property
    def data_status(self) -> str:
        return "データなし" if self.meta is None else self.meta.source


def build_opponent_intel(
    *,
    species: str,
    match_facts: MatchOpponentFacts,
    provider: OpponentMetaProvider,
) -> OpponentIntelView:
    """Apply precedence: match facts > legal possibilities > population meta."""

    species_key = species.strip()
    meta = provider.get(species_key)
    try:
        possible = possible_abilities_for_species(species_key)
    except LookupError:
        possible = ()
    ability = match_facts.ability
    if not ability:
        ability = " / ".join(possible) if possible else "不明"
    item = match_facts.item or "不明"
    return OpponentIntelView(
        species=species_key or "不明",
        ability=ability,
        item=item,
        observed_moves=match_facts.moves,
        possible_abilities=possible,
        meta=meta,
    )
