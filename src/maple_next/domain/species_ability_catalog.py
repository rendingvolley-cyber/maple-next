"""Pinned canonical species abilities and entry-observable classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

CATALOG_RESOURCE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "pokemon_showdown_6a1836dd_catalog.json"
)
UNKNOWN_ABILITY_LABEL = "不明"

# Input aliases only.  Legality and classification always come from the
# generated canonical IDs in the pinned artifact.
_SPECIES_INPUT_ALIASES = {
    "ボーマンダ": "salamence",
    "ランドロス": "landorus",
    "ギャラドス": "gyarados",
    "カイリュー": "dragonite",
    "ペリッパー": "pelipper",
    "コータス": "torkoal",
    "バンギラス": "tyranitar",
    "キュウコン（アローラ）": "ninetalesalola",
}


class SpeciesCatalogCoverageError(LookupError):
    """A supplied species cannot be resolved in the complete pinned scope."""


@dataclass(frozen=True, slots=True)
class AbilityCatalogRecord:
    ability_id: str
    name: str
    display_name: str
    entry_hooks: tuple[str, ...]
    entry_observable: bool
    entry_classification: str | None


@dataclass(frozen=True, slots=True)
class SpeciesAbilityRecord:
    species_id: str
    name: str
    ability_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalSpeciesAbilityCatalog:
    source_commit: str
    source_pokedex_path: str
    source_abilities_path: str
    species: dict[str, SpeciesAbilityRecord]
    abilities: dict[str, AbilityCatalogRecord]
    entry_hook_ability_ids: frozenset[str]
    entry_observable_ability_ids: frozenset[str]

    @property
    def species_count(self) -> int:
        return len(self.species)

    @property
    def ability_count(self) -> int:
        return len(self.abilities)

    def resolve_species(self, value: str) -> SpeciesAbilityRecord:
        raw = value.strip()
        species_id = _SPECIES_INPUT_ALIASES.get(raw, _to_id(raw))
        record = self.species.get(species_id)
        if record is None:
            raise SpeciesCatalogCoverageError(f"PINNED_SPECIES_UNRESOLVED:{raw or '<empty>'}")
        return record

    def legal_abilities(self, value: str) -> tuple[AbilityCatalogRecord, ...]:
        species = self.resolve_species(value)
        try:
            return tuple(self.abilities[ability_id] for ability_id in species.ability_ids)
        except KeyError as exc:  # guarded by generator; fail explicit if artifact is corrupted
            raise SpeciesCatalogCoverageError(
                f"PINNED_ABILITY_UNRESOLVED:{species.species_id}:{exc.args[0]}"
            ) from exc

    def species_has_entry_observable_ability(self, value: str) -> bool:
        return any(ability.entry_observable for ability in self.legal_abilities(value))


def _to_id(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


@lru_cache(maxsize=1)
def canonical_species_ability_catalog() -> CanonicalSpeciesAbilityCatalog:
    payload = cast(
        dict[str, object],
        json.loads(CATALOG_RESOURCE.read_text(encoding="utf-8")),
    )
    source = cast(dict[str, object], payload["source"])
    raw_abilities = cast(dict[str, dict[str, object]], payload["abilities"])
    raw_species = cast(dict[str, dict[str, object]], payload["species"])
    abilities = {
        ability_id: AbilityCatalogRecord(
            ability_id=ability_id,
            name=str(raw["name"]),
            display_name=str(raw["display_name"]),
            entry_hooks=tuple(str(value) for value in cast(list[object], raw["entry_hooks"])),
            entry_observable=bool(raw.get("entry_observable", False)),
            entry_classification=(
                str(raw["entry_classification"])
                if raw.get("entry_classification") is not None
                else None
            ),
        )
        for ability_id, raw in raw_abilities.items()
    }
    species = {
        species_id: SpeciesAbilityRecord(
            species_id=species_id,
            name=str(raw["name"]),
            ability_ids=tuple(
                str(value) for value in cast(list[object], raw["ability_ids"])
            ),
        )
        for species_id, raw in raw_species.items()
    }
    entry_hooks = frozenset(
        ability_id for ability_id, ability in abilities.items() if ability.entry_hooks
    )
    entry_observable = frozenset(
        ability_id for ability_id, ability in abilities.items() if ability.entry_observable
    )
    if any(
        ability.entry_hooks and ability.entry_classification is None
        for ability in abilities.values()
    ):
        raise ValueError("pinned entry-hook ability lacks explicit classification")
    return CanonicalSpeciesAbilityCatalog(
        source_commit=str(source["commit"]),
        source_pokedex_path=str(source["pokedex_path"]),
        source_abilities_path=str(source["abilities_path"]),
        species=species,
        abilities=abilities,
        entry_hook_ability_ids=entry_hooks,
        entry_observable_ability_ids=entry_observable,
    )
