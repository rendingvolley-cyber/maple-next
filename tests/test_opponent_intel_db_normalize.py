from __future__ import annotations

import pytest

from maple_next.opponent_intel_db.normalize import (
    NormalizationError,
    RankedEntry,
    SpeciesStatsRecord,
    species_record_from_parsed,
)

VALID_PARSED = {
    "species_id": "garchomp",
    "display_name": "Garchomp",
    "season": "M-5",
    "format": "single",
    "source": "pokechamdb",
    "source_url": "https://pokechamdb.com/pokemon/garchomp",
    "source_updated_at": None,
    "fetched_at": "2026-08-11T00:00:00+00:00",
    "ranking": 1.0,
    "moves": [{"name": "Earthquake", "percentage": 99.0}],
    "items": [{"name": "Focus Sash", "percentage": 40.0}],
    "abilities": [{"name": "Rough Skin", "percentage": 99.0}],
    "natures": [{"name": "Jolly", "percentage": 50.0}],
    "partners": [{"name": "Primarina", "percentage": None}],
    "spreads": [],
}


def test_species_record_from_parsed_builds_expected_record() -> None:
    record = species_record_from_parsed(VALID_PARSED)
    assert record.species_id == "garchomp"
    assert record.moves == (RankedEntry("Earthquake", 99.0),)
    assert record.partners == (RankedEntry("Primarina", None),)


def test_missing_required_field_raises_normalization_error() -> None:
    bad = dict(VALID_PARSED)
    del bad["species_id"]
    with pytest.raises(NormalizationError):
        species_record_from_parsed(bad)


def test_blank_required_field_raises_normalization_error() -> None:
    bad = dict(VALID_PARSED)
    bad["display_name"] = "   "
    with pytest.raises(NormalizationError):
        species_record_from_parsed(bad)


def test_non_numeric_percentage_raises_normalization_error() -> None:
    bad = dict(VALID_PARSED)
    bad["moves"] = [{"name": "Earthquake", "percentage": "ninety-nine"}]
    with pytest.raises(NormalizationError):
        species_record_from_parsed(bad)


def test_malformed_ranked_entry_missing_name_raises() -> None:
    bad = dict(VALID_PARSED)
    bad["items"] = [{"percentage": 40.0}]
    with pytest.raises(NormalizationError):
        species_record_from_parsed(bad)


def test_caller_can_skip_malformed_species_and_continue() -> None:
    """Mirrors how cli.py loops over multiple parsed species: catch, log, continue."""

    parsed_batch = [
        VALID_PARSED,
        {**VALID_PARSED, "species_id": "broken", "moves": "not-a-list"},
        {**VALID_PARSED, "species_id": "primarina", "display_name": "Primarina"},
    ]
    records: list[SpeciesStatsRecord] = []
    for parsed in parsed_batch:
        try:
            records.append(species_record_from_parsed(parsed))
        except NormalizationError:
            continue

    assert {record.species_id for record in records} == {"garchomp", "primarina"}


def test_ranked_entry_json_round_trip() -> None:
    entry = RankedEntry("Earthquake", 99.5)
    assert RankedEntry.from_json_dict(entry.to_json_dict()) == entry
