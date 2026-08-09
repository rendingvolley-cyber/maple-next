"""Generate Maple's canonical offline species/ability catalog.

The input files must be the two exact Pokemon Showdown sources pinned below.
This generator deliberately rejects every other byte sequence, so regeneration
can never drift to a floating upstream revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

SHOWDOWN_COMMIT = "6a1836dd71c0718e923206f3d089e61074410868"
POKEDEX_PATH = "data/pokedex.ts"
ABILITIES_PATH = "data/abilities.ts"
POKEDEX_SHA256 = "0aba8712ababae8e356bdd9d6c6ffa73a5169d39a3ac831a853ae88fe653e6bc"
ABILITIES_SHA256 = "175067b1e827ca11ae4728169215c9bdd27ac4cb3e35d2e83eaaaee7e9e8c842"

_TOP_LEVEL_ENTRY = re.compile(r"^\t([a-z0-9]+): \{$", re.MULTILINE)
_NAME = re.compile(r'^\t\tname: "([^"]+)",$', re.MULTILINE)
_NUM = re.compile(r"^\t\tnum: (-?\d+),$", re.MULTILINE)
_ABILITIES = re.compile(r"^\t\tabilities: \{([^}]*)\},$", re.MULTILINE)
_QUOTED_VALUE = re.compile(r'\b(?:0|1|H|S): "([^"]+)"')
_BASE_SPECIES = re.compile(r'^\t\tbaseSpecies: "([^"]+)",$', re.MULTILINE)
_NONSTANDARD = re.compile(r'^\t\tisNonstandard:', re.MULTILINE)
_ENTRY_HOOK = re.compile(
    r"^\t\t(onStart|onSwitchIn|onAnySwitchIn)\s*\(", re.MULTILINE
)

# Complete human-reviewed classification for every top-level entry hook in
# the exact pinned abilities.ts.  True values are limited to effects Maple's
# canonical board model can represent: stages, weather, terrain, major
# status, HP, side conditions, or an active forme identity.  The generator
# requires exact key equality with the source-discovered hook set, preventing
# a newly added or silently omitted hook from passing regeneration.
ENTRY_HOOK_CLASSIFICATION: dict[str, tuple[bool, str]] = {
    "airlock": (False, "weather suppression is not a modeled weather mutation"),
    "anticipation": (False, "announcement only"),
    "asoneglastrier": (False, "entry announcement has no modeled state mutation"),
    "asonespectrier": (False, "entry announcement has no modeled state mutation"),
    "aurabreak": (False, "damage modifier is not modeled board state"),
    "beadsofruin": (False, "effective-stat modifier is not a stat stage"),
    "cloudnine": (False, "weather suppression is not a modeled weather mutation"),
    "comatose": (False, "passive status semantics are not a major-status mutation"),
    "commander": (True, "STAT_STAGE"),
    "costar": (True, "STAT_STAGE"),
    "curiousmedicine": (True, "STAT_STAGE"),
    "darkaura": (False, "damage modifier is not modeled board state"),
    "dauntlessshield": (True, "STAT_STAGE"),
    "deltastream": (True, "WEATHER"),
    "desolateland": (True, "WEATHER"),
    "download": (True, "STAT_STAGE"),
    "drizzle": (True, "WEATHER"),
    "drought": (True, "WEATHER"),
    "electricsurge": (True, "TERRAIN"),
    "embodyaspectcornerstone": (True, "STAT_STAGE"),
    "embodyaspecthearthflame": (True, "STAT_STAGE"),
    "embodyaspectteal": (True, "STAT_STAGE"),
    "embodyaspectwellspring": (True, "STAT_STAGE"),
    "fairyaura": (False, "damage modifier is not modeled board state"),
    "flowergift": (True, "ACTIVE_FORM"),
    "forecast": (True, "ACTIVE_FORM"),
    "forewarn": (False, "move revelation is not modeled board state"),
    "frisk": (False, "item revelation is opponent intel, not board-state mutation"),
    "gluttony": (False, "berry threshold setup is not observable entry state"),
    "gorillatactics": (False, "effective-stat modifier is not a stat stage"),
    "grassysurge": (True, "TERRAIN"),
    "hadronengine": (True, "TERRAIN"),
    "hospitality": (True, "HP"),
    "iceface": (True, "ACTIVE_FORM"),
    "imposter": (False, "transform state is outside Maple's current board model"),
    "intimidate": (True, "STAT_STAGE"),
    "intrepidsword": (True, "STAT_STAGE"),
    "klutz": (False, "item suppression is not modeled board state"),
    "mimicry": (False, "type mutation is outside Maple's current board model"),
    "mistysurge": (True, "TERRAIN"),
    "moldbreaker": (False, "announcement only"),
    "neutralizinggas": (False, "ability suppression is outside Maple's board model"),
    "opportunist": (True, "STAT_STAGE"),
    "orichalcumpulse": (True, "WEATHER"),
    "pastelveil": (True, "MAJOR_STATUS"),
    "pressure": (False, "announcement only"),
    "primordialsea": (True, "WEATHER"),
    "protosynthesis": (False, "effective-stat modifier is not a stat stage"),
    "psychicsurge": (True, "TERRAIN"),
    "quarkdrive": (False, "effective-stat modifier is not a stat stage"),
    "sandstream": (True, "WEATHER"),
    "schooling": (True, "ACTIVE_FORM"),
    "screencleaner": (True, "SIDE_CONDITION"),
    "shieldsdown": (True, "ACTIVE_FORM"),
    "slowstart": (False, "effective-stat modifier is not a stat stage"),
    "snowwarning": (True, "WEATHER"),
    "supersweetsyrup": (True, "STAT_STAGE"),
    "supremeoverlord": (False, "damage modifier is not modeled board state"),
    "swordofruin": (False, "effective-stat modifier is not a stat stage"),
    "tabletsofruin": (False, "effective-stat modifier is not a stat stage"),
    "terashift": (True, "ACTIVE_FORM"),
    "teravolt": (False, "announcement only"),
    "trace": (False, "copied-ability state is outside Maple's board model"),
    "truant": (False, "turn cadence is not an entry board-state mutation"),
    "turboblaze": (False, "announcement only"),
    "unnerve": (False, "announcement only"),
    "vesselofruin": (False, "effective-stat modifier is not a stat stage"),
    "windrider": (True, "STAT_STAGE"),
    "zerotohero": (False, "forme changes on switch-out, not this entry hook"),
}

JAPANESE_ABILITY_LABELS = {
    "intimidate": "いかく",
    "moxie": "じしんかじょう",
    "sandforce": "すなのちから",
    "sheerforce": "ちからずく",
    "innerfocus": "せいしんりょく",
    "multiscale": "マルチスケイル",
    "keeneye": "するどいめ",
    "raindish": "あめうけざら",
    "drizzle": "あめふらし",
    "whitesmoke": "しろいけむり",
    "drought": "ひでり",
    "shellarmor": "シェルアーマー",
    "sandstream": "すなおこし",
    "unnerve": "きんちょうかん",
    "snowcloak": "ゆきがくれ",
    "snowwarning": "ゆきふらし",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_source(path: Path, expected_sha256: str) -> str:
    actual = _sha256(path)
    if actual != expected_sha256:
        raise ValueError(f"unexpected pinned source sha256 for {path}: {actual}")
    return path.read_text(encoding="utf-8")


def _entries(source: str) -> dict[str, str]:
    """Split exact Showdown tables at their one-tab top-level entries."""

    matches = list(_TOP_LEVEL_ENTRY.finditer(source))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        result[match.group(1)] = source[match.start() : end]
    return result


def _to_id(name: str) -> str:
    return "".join(character for character in name.lower() if character.isalnum())


def _entry_hook_abilities(ability_entries: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        ability_id
        for ability_id, block in ability_entries.items()
        if _ENTRY_HOOK.search(block)
    )


def generate(pokedex_path: Path, abilities_path: Path) -> dict[str, Any]:
    pokedex_source = _assert_source(pokedex_path, POKEDEX_SHA256)
    abilities_source = _assert_source(abilities_path, ABILITIES_SHA256)
    ability_entries = _entries(abilities_source)
    species_entries = _entries(pokedex_source)

    abilities: dict[str, dict[str, object]] = {}
    for ability_id, block in ability_entries.items():
        name_match = _NAME.search(block)
        if name_match is None:
            raise ValueError(f"ability has no canonical name: {ability_id}")
        abilities[ability_id] = {
            "name": name_match.group(1),
            "display_name": JAPANESE_ABILITY_LABELS.get(ability_id, name_match.group(1)),
            "entry_hooks": sorted(set(_ENTRY_HOOK.findall(block))),
        }

    discovered_hooks = set(_entry_hook_abilities(ability_entries))
    classified_hooks = set(ENTRY_HOOK_CLASSIFICATION)
    if discovered_hooks != classified_hooks:
        raise ValueError(
            "entry-hook classification coverage mismatch: "
            f"missing={sorted(discovered_hooks - classified_hooks)}, "
            f"extra={sorted(classified_hooks - discovered_hooks)}"
        )
    for ability_id, (is_observable, classification) in ENTRY_HOOK_CLASSIFICATION.items():
        abilities[ability_id]["entry_observable"] = is_observable
        abilities[ability_id]["entry_classification"] = classification

    raw_species: dict[str, dict[str, object]] = {}
    for species_id, block in species_entries.items():
        num_match = _NUM.search(block)
        if _NONSTANDARD.search(block) or num_match is None or int(num_match.group(1)) <= 0:
            continue
        name_match = _NAME.search(block)
        abilities_match = _ABILITIES.search(block)
        if name_match is None:
            raise ValueError(f"supported species entry has no name: {species_id}")
        base_species_match = _BASE_SPECIES.search(block)
        ability_ids = (
            tuple(
                dict.fromkeys(
                    _to_id(name) for name in _QUOTED_VALUE.findall(abilities_match.group(1))
                )
            )
            if abilities_match is not None
            else ()
        )
        raw_species[species_id] = {
            "name": name_match.group(1),
            "ability_ids": list(ability_ids),
            "base_species_id": (
                _to_id(base_species_match.group(1)) if base_species_match is not None else None
            ),
        }

    def resolve_abilities(species_id: str, stack: tuple[str, ...] = ()) -> tuple[str, ...]:
        if species_id in stack:
            raise ValueError(f"cyclic base species chain: {(*stack, species_id)}")
        entry = raw_species[species_id]
        explicit = tuple(str(value) for value in cast(list[object], entry["ability_ids"]))
        if explicit:
            return explicit
        base_species_id = entry["base_species_id"]
        if not isinstance(base_species_id, str) or base_species_id not in raw_species:
            raise ValueError(f"supported species has no ability source: {species_id}")
        return resolve_abilities(base_species_id, (*stack, species_id))

    species: dict[str, dict[str, object]] = {}
    for species_id, raw_entry in raw_species.items():
        ability_ids = resolve_abilities(species_id)
        missing = tuple(ability_id for ability_id in ability_ids if ability_id not in abilities)
        if missing:
            raise ValueError(f"unknown abilities for {species_id}: {missing}")
        species[species_id] = {
            "name": raw_entry["name"],
            "ability_ids": list(ability_ids),
        }

    return {
        "schema_version": "maple-showdown-canonical.v1",
        "source": {
            "repository": "https://github.com/smogon/pokemon-showdown",
            "commit": SHOWDOWN_COMMIT,
            "pokedex_path": POKEDEX_PATH,
            "pokedex_sha256": POKEDEX_SHA256,
            "abilities_path": ABILITIES_PATH,
            "abilities_sha256": ABILITIES_SHA256,
        },
        "scope": {
            "rule": (
                "official Pokedex entries with num > 0 and without isNonstandard "
                "at the pinned commit"
            ),
            "species_count": len(species),
            "ability_count": len(abilities),
            "entry_hook_ability_count": len(_entry_hook_abilities(ability_entries)),
            "entry_observable_ability_count": sum(
                1 for observable, _reason in ENTRY_HOOK_CLASSIFICATION.values() if observable
            ),
        },
        "species": species,
        "abilities": abilities,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pokedex", type=Path)
    parser.add_argument("abilities", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--report-entry-hooks", action="store_true")
    args = parser.parse_args()
    payload = generate(args.pokedex, args.abilities)
    if args.report_entry_hooks:
        hooks = {
            ability_id: ability
            for ability_id, ability in payload["abilities"].items()
            if ability["entry_hooks"]
        }
        print(json.dumps(hooks, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
