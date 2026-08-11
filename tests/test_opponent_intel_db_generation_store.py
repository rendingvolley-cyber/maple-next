"""Coherent-generation commit: a reader must never observe a mismatched
snapshot/catalog pairing (new snapshot + old catalog, or vice versa).
Regression coverage for ``generation_store.commit_generation`` /
``read_current_generation``, including injected mid-commit failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.opponent_intel_db import generation_store as gs

SNAPSHOT_V1 = b'{"schema_version": "opponent-intel-snapshot.v1", "species": {}}\n'
CATALOG_V1 = b'{"schema_version": "opponent-intel-move-catalog.v1", "moves": []}\n'
SNAPSHOT_V2 = b'{"schema_version": "opponent-intel-snapshot.v1", "species": {"a": 1}}\n'
CATALOG_V2 = b'{"schema_version": "opponent-intel-move-catalog.v1", "moves": ["a"]}\n'


def _commit_v1(intel_directory: Path) -> gs.GenerationPointer:
    return gs.commit_generation(
        intel_directory,
        snapshot_bytes=SNAPSHOT_V1,
        catalog_bytes=CATALOG_V1,
        snapshot_schema_version="opponent-intel-snapshot.v1",
        catalog_schema_version="opponent-intel-move-catalog.v1",
        source="pokechamdb",
        created_at="2026-08-01T00:00:00+00:00",
    )


def test_successful_update_switches_both_files_to_the_same_new_generation(
    tmp_path: Path,
) -> None:
    intel_directory = tmp_path / "intel"
    old_pointer = _commit_v1(intel_directory)

    new_pointer = gs.commit_generation(
        intel_directory,
        snapshot_bytes=SNAPSHOT_V2,
        catalog_bytes=CATALOG_V2,
        snapshot_schema_version="opponent-intel-snapshot.v1",
        catalog_schema_version="opponent-intel-move-catalog.v1",
        source="pokechamdb",
        created_at="2026-08-11T00:00:00+00:00",
    )

    assert new_pointer.generation_id != old_pointer.generation_id
    active = gs.read_current_generation(intel_directory)
    assert active is not None
    assert active.pointer.generation_id == new_pointer.generation_id
    assert active.snapshot_path.read_bytes() == SNAPSHOT_V2
    assert active.catalog_path.read_bytes() == CATALOG_V2


def test_read_consistency_snapshot_and_catalog_always_same_generation(
    tmp_path: Path,
) -> None:
    intel_directory = tmp_path / "intel"
    _commit_v1(intel_directory)
    gs.commit_generation(
        intel_directory,
        snapshot_bytes=SNAPSHOT_V2,
        catalog_bytes=CATALOG_V2,
        snapshot_schema_version="opponent-intel-snapshot.v1",
        catalog_schema_version="opponent-intel-move-catalog.v1",
        source="pokechamdb",
        created_at="2026-08-11T00:00:00+00:00",
    )

    active = gs.read_current_generation(intel_directory)
    assert active is not None
    generation_dir_snapshot = active.snapshot_path.parent
    generation_dir_catalog = active.catalog_path.parent
    # Both files always resolve from the identical generation directory --
    # there is no code path that can mix generations.
    assert generation_dir_snapshot == generation_dir_catalog
    assert generation_dir_snapshot.name == active.pointer.generation_id


def test_read_consistency_tampered_snapshot_hash_fails_closed(tmp_path: Path) -> None:
    intel_directory = tmp_path / "intel"
    pointer = _commit_v1(intel_directory)

    generation_dir = intel_directory / gs.GENERATIONS_DIRNAME / pointer.generation_id
    (generation_dir / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(b"tampered bytes")

    with pytest.raises(gs.GenerationStoreError, match="SNAPSHOT_HASH_MISMATCH"):
        gs.read_current_generation(intel_directory)


def test_injected_snapshot_stage_failure_leaves_old_generation_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intel_directory = tmp_path / "intel"
    old_pointer = _commit_v1(intel_directory)

    real_write = gs._atomic_write_bytes

    def failing_write(path: Path, content: bytes) -> None:
        if path.name == gs.DEFAULT_SNAPSHOT_FILENAME:
            raise OSError("simulated disk failure while staging the snapshot")
        real_write(path, content)

    monkeypatch.setattr(gs, "_atomic_write_bytes", failing_write)

    with pytest.raises(OSError, match="simulated disk failure"):
        gs.commit_generation(
            intel_directory,
            snapshot_bytes=SNAPSHOT_V2,
            catalog_bytes=CATALOG_V2,
            snapshot_schema_version="opponent-intel-snapshot.v1",
            catalog_schema_version="opponent-intel-move-catalog.v1",
            source="pokechamdb",
            created_at="2026-08-11T00:00:00+00:00",
        )

    active = gs.read_current_generation(intel_directory)
    assert active is not None
    assert active.pointer.generation_id == old_pointer.generation_id
    assert active.snapshot_path.read_bytes() == SNAPSHOT_V1
    assert active.catalog_path.read_bytes() == CATALOG_V1
    # The failed staging directory must not linger as a second, orphaned
    # generation directory that anything could later mistake for valid.
    assert not (intel_directory / gs.GENERATIONS_DIRNAME).exists() or len(
        list((intel_directory / gs.GENERATIONS_DIRNAME).iterdir())
    ) == 1


def test_injected_catalog_stage_failure_leaves_old_generation_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intel_directory = tmp_path / "intel"
    old_pointer = _commit_v1(intel_directory)

    real_write = gs._atomic_write_bytes

    def failing_write(path: Path, content: bytes) -> None:
        if path.name == gs.DEFAULT_CATALOG_FILENAME:
            raise OSError("simulated disk failure while staging the catalog")
        real_write(path, content)

    monkeypatch.setattr(gs, "_atomic_write_bytes", failing_write)

    with pytest.raises(OSError, match="simulated disk failure"):
        gs.commit_generation(
            intel_directory,
            snapshot_bytes=SNAPSHOT_V2,
            catalog_bytes=CATALOG_V2,
            snapshot_schema_version="opponent-intel-snapshot.v1",
            catalog_schema_version="opponent-intel-move-catalog.v1",
            source="pokechamdb",
            created_at="2026-08-11T00:00:00+00:00",
        )

    active = gs.read_current_generation(intel_directory)
    assert active is not None
    assert active.pointer.generation_id == old_pointer.generation_id
    assert active.snapshot_path.read_bytes() == SNAPSHOT_V1
    assert active.catalog_path.read_bytes() == CATALOG_V1


def test_injected_final_commit_failure_leaves_old_generation_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intel_directory = tmp_path / "intel"
    old_pointer = _commit_v1(intel_directory)

    real_write = gs._atomic_write_bytes

    def failing_write(path: Path, content: bytes) -> None:
        if path.name == gs.POINTER_FILENAME:
            raise OSError("simulated disk failure during the final pointer commit")
        real_write(path, content)

    monkeypatch.setattr(gs, "_atomic_write_bytes", failing_write)

    with pytest.raises(OSError, match="simulated disk failure"):
        gs.commit_generation(
            intel_directory,
            snapshot_bytes=SNAPSHOT_V2,
            catalog_bytes=CATALOG_V2,
            snapshot_schema_version="opponent-intel-snapshot.v1",
            catalog_schema_version="opponent-intel-move-catalog.v1",
            source="pokechamdb",
            created_at="2026-08-11T00:00:00+00:00",
        )

    # The pointer itself was never touched (its own atomic replace never
    # completed), so the old generation is still exactly what it resolves.
    active = gs.read_current_generation(intel_directory)
    assert active is not None
    assert active.pointer.generation_id == old_pointer.generation_id
    assert active.snapshot_path.read_bytes() == SNAPSHOT_V1
    assert active.catalog_path.read_bytes() == CATALOG_V1


def test_interrupted_stale_staging_data_never_becomes_active_automatically(
    tmp_path: Path,
) -> None:
    intel_directory = tmp_path / "intel"
    old_pointer = _commit_v1(intel_directory)

    # Simulate an interrupted run: a generation directory exists on disk
    # (fully populated, even) but was never referenced by a pointer commit.
    orphan_id = "orphan-generation-never-committed"
    orphan_dir = intel_directory / gs.GENERATIONS_DIRNAME / orphan_id
    orphan_dir.mkdir(parents=True)
    (orphan_dir / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(SNAPSHOT_V2)
    (orphan_dir / gs.DEFAULT_CATALOG_FILENAME).write_bytes(CATALOG_V2)

    active = gs.read_current_generation(intel_directory)
    assert active is not None
    assert active.pointer.generation_id == old_pointer.generation_id
    assert active.pointer.generation_id != orphan_id
    assert active.snapshot_path.read_bytes() == SNAPSHOT_V1


def test_read_current_generation_returns_none_when_never_committed(tmp_path: Path) -> None:
    intel_directory = tmp_path / "intel"
    intel_directory.mkdir(parents=True)
    assert gs.read_current_generation(intel_directory) is None


def test_pointer_and_manifest_content_are_identical(tmp_path: Path) -> None:
    intel_directory = tmp_path / "intel"
    pointer = _commit_v1(intel_directory)

    pointer_bytes = (intel_directory / gs.POINTER_FILENAME).read_bytes()
    manifest_bytes = (
        intel_directory / gs.GENERATIONS_DIRNAME / pointer.generation_id / gs.MANIFEST_FILENAME
    ).read_bytes()
    assert pointer_bytes == manifest_bytes
