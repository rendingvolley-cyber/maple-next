"""Offline opponent INTEL contracts for Battle Record v5.

The battle runtime receives a local-cache provider.  This module contains no
HTTP client and cannot fetch population data during a turn.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
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
    natures: tuple[RankedUsage, ...] = ()
    partners: tuple[RankedUsage, ...] = ()
    source_url: str = ""
    source_updated_at: str | None = None
    fetched_at: str = ""
    ranking: float | None = None


class OpponentMetaProvider(Protocol):
    """Read-only local boundary. Implementations must not use the network."""

    def get(self, species: str) -> OpponentMetaSnapshot | None: ...


class ChainedOpponentMetaProvider:
    """Tries each provider in order, returning the first non-``None`` result.

    Used to prefer the pokechamdb-backed :class:`SnapshotOpponentMetaProvider`
    while falling back to a legacy :class:`LocalJsonOpponentMetaProvider`
    cache when no snapshot has been downloaded yet -- never raises on its
    own, since every ``OpponentMetaProvider.get`` implementation already
    fails soft to ``None``.
    """

    def __init__(self, providers: Sequence[OpponentMetaProvider]) -> None:
        self._providers = tuple(providers)

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        for provider in self._providers:
            result = provider.get(species)
            if result is not None:
                return result
        return None


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


class SnapshotOpponentMetaProvider:
    """Reads population statistics from the opponent-intel-db snapshot file.

    Fails soft: any missing file, unreadable/malformed document, or unknown
    species id simply yields ``None`` (INTEL shows no population stats),
    never an exception. This mirrors the read-only, no-network contract of
    :class:`OpponentMetaProvider`.
    """

    def __init__(self, snapshot_path: Path) -> None:
        self._snapshot_path = snapshot_path

    @staticmethod
    def _ranked(entries: Sequence[object] | None) -> tuple[RankedUsage, ...]:
        if entries is None:
            return ()
        result: list[RankedUsage] = []
        for entry in entries:
            name = str(getattr(entry, "name", "")).strip()
            if not name:
                continue
            percentage = getattr(entry, "percentage", None)
            result.append(
                RankedUsage(name, float(percentage) if percentage is not None else None)
            )
        return tuple(result)

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        # Import kept local to this method (rather than module top-level) so
        # nothing here forces a hard dependency for callers that never use
        # this provider -- consistent with the read-only, no-network-client
        # helpers this module is documented to allow importing from UI code.
        from maple_next.opponent_intel_db.snapshot_store import (
            SnapshotStoreError,
            read_snapshot,
        )

        species_key = species.strip()
        if not species_key:
            return None
        try:
            document = read_snapshot(self._snapshot_path)
        except (SnapshotStoreError, OSError, ValueError):
            return None
        if document is None:
            return None

        normalized_key = species_key.lower().replace(" ", "-")
        record = document.species.get(species_key) or document.species.get(normalized_key)
        if record is None:
            for candidate_id, candidate_record in document.species.items():
                if candidate_id.lower() == normalized_key:
                    record = candidate_record
                    break
                if candidate_record.display_name.strip().lower() == species_key.lower():
                    record = candidate_record
                    break
        if record is None:
            return None

        regulation = f"{record.season}/{record.format}".strip("/") or record.season
        return OpponentMetaSnapshot(
            species=record.display_name or record.species_id,
            regulation=regulation,
            snapshot_date=record.fetched_at,
            source=record.source,
            moves=self._ranked(record.moves),
            abilities=self._ranked(record.abilities),
            items=self._ranked(record.items),
            natures=self._ranked(record.natures),
            partners=self._ranked(record.partners),
            source_url=record.source_url,
            source_updated_at=record.source_updated_at,
            fetched_at=record.fetched_at,
            ranking=record.ranking,
        )


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
