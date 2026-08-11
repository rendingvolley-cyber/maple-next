from __future__ import annotations

from pathlib import Path

from maple_next.opponent_intel_db.move_catalog_builder import (
    build_move_catalog,
    write_move_catalog_atomic,
)
from maple_next.opponent_intel_db.normalize import RankedEntry, SpeciesStatsRecord
from maple_next.opponent_intel_db.snapshot_store import SnapshotDocument


def make_record(species_id: str, moves: tuple[str, ...]) -> SpeciesStatsRecord:
    return SpeciesStatsRecord(
        species_id=species_id,
        display_name=species_id.title(),
        season="M-5",
        format="single",
        source="pokechamdb",
        source_url=f"https://pokechamdb.com/pokemon/{species_id}",
        source_updated_at=None,
        fetched_at="2026-08-11T00:00:00+00:00",
        ranking=1.0,
        moves=tuple(RankedEntry(name, 50.0) for name in moves),
    )


def make_document() -> SnapshotDocument:
    species = {
        "garchomp": make_record("garchomp", ("Earthquake", "Scale Shot", "Stealth Rock")),
        "primarina": make_record("primarina", ("Moonblast", "Scale Shot")),
        "dondozo": make_record("dondozo", ()),
    }
    return SnapshotDocument(
        schema_version="opponent-intel-snapshot.v1",
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
        species=species,
    )


def test_build_move_catalog_dedups_and_sorts() -> None:
    catalog = build_move_catalog(make_document())
    names = [entry["canonical_name"] for entry in catalog["moves"]]
    assert names == sorted(names)
    assert names == ["Earthquake", "Moonblast", "Scale Shot", "Stealth Rock"]


def test_build_move_catalog_seen_species_count() -> None:
    catalog = build_move_catalog(make_document())
    by_name = {entry["canonical_name"]: entry["seen_species_count"] for entry in catalog["moves"]}
    assert by_name["Scale Shot"] == 2
    assert by_name["Earthquake"] == 1
    assert by_name["Stealth Rock"] == 1


def test_build_move_catalog_schema_version_and_generated_at_present() -> None:
    catalog = build_move_catalog(make_document())
    assert catalog["schema_version"] == "opponent-intel-move-catalog.v1"
    assert isinstance(catalog["generated_at"], str) and catalog["generated_at"]


def test_move_within_single_species_counted_once() -> None:
    document = SnapshotDocument(
        schema_version="opponent-intel-snapshot.v1",
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
        species={
            "garchomp": SpeciesStatsRecord(
                species_id="garchomp",
                display_name="Garchomp",
                season="M-5",
                format="single",
                source="pokechamdb",
                source_url="https://pokechamdb.com/pokemon/garchomp",
                source_updated_at=None,
                fetched_at="2026-08-11T00:00:00+00:00",
                ranking=1.0,
                moves=(RankedEntry("Earthquake", 99.0), RankedEntry("Earthquake", 1.0)),
            )
        },
    )
    catalog = build_move_catalog(document)
    assert len(catalog["moves"]) == 1
    assert catalog["moves"][0]["seen_species_count"] == 1


def test_write_move_catalog_atomic(tmp_path: Path) -> None:
    catalog = build_move_catalog(make_document())
    path = tmp_path / "move_catalog.json"
    write_move_catalog_atomic(path, catalog)
    assert path.is_file()
    leftover = [entry for entry in tmp_path.iterdir() if entry.name != "move_catalog.json"]
    assert leftover == []
