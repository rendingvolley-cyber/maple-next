from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.opponent_intel_db.normalize import RankedEntry, SpeciesStatsRecord
from maple_next.opponent_intel_db.snapshot_store import (
    SnapshotDocument,
    SnapshotStoreError,
    read_snapshot,
    write_snapshot_atomic,
)


def make_record(species_id: str = "garchomp") -> SpeciesStatsRecord:
    return SpeciesStatsRecord(
        species_id=species_id,
        display_name="Garchomp",
        season="M-5",
        format="single",
        source="pokechamdb",
        source_url=f"https://pokechamdb.com/pokemon/{species_id}",
        source_updated_at=None,
        fetched_at="2026-08-11T00:00:00+00:00",
        ranking=1.0,
        moves=(RankedEntry("Earthquake", 99.0), RankedEntry("Scale Shot", 49.0)),
        items=(RankedEntry("Focus Sash", 40.0),),
        abilities=(RankedEntry("Rough Skin", 99.0),),
        natures=(RankedEntry("Jolly", 50.0),),
        partners=(RankedEntry("Primarina", None),),
        spreads=({"hp": 2, "attack": 252, "speed": 252, "adoption_rate": 49.1},),
    )


def test_species_stats_record_json_round_trip() -> None:
    record = make_record()
    round_tripped = SpeciesStatsRecord.from_json_dict(record.to_json_dict())
    assert round_tripped == record


def test_snapshot_document_json_round_trip() -> None:
    record = make_record()
    document = SnapshotDocument(
        schema_version="opponent-intel-snapshot.v1",
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
        species={record.species_id: record},
    )
    round_tripped = SnapshotDocument.from_json_dict(document.to_json_dict())
    assert round_tripped == document


def test_write_snapshot_atomic_and_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    records = [make_record("garchomp"), make_record("primarina")]
    write_snapshot_atomic(
        path,
        records,
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
    )

    document = read_snapshot(path)
    assert document is not None
    assert set(document.species) == {"garchomp", "primarina"}
    assert document.species["garchomp"].display_name == "Garchomp"
    assert document.source == "pokechamdb"


def test_read_snapshot_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert read_snapshot(tmp_path / "does-not-exist.json") is None


def test_read_snapshot_raises_for_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SnapshotStoreError):
        read_snapshot(path)


def test_write_snapshot_atomic_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    write_snapshot_atomic(
        path,
        [make_record()],
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
    )
    leftover = [entry for entry in tmp_path.iterdir() if entry.name != "snapshot.json"]
    assert leftover == []
