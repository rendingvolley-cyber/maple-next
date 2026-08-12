"""Production Battle Record consumes one immutable INTEL generation bundle."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_turn_state_ui_bundle_c import build_window

from maple_next.opponent_intel_db import generation_store as gs
from maple_next.opponent_intel_db.move_catalog_builder import (
    MOVE_CATALOG_SCHEMA_VERSION,
    build_move_catalog,
    encode_move_catalog,
)
from maple_next.opponent_intel_db.normalize import RankedEntry, SpeciesStatsRecord
from maple_next.opponent_intel_db.runtime_intel import resolve_runtime_intel_bundle
from maple_next.opponent_intel_db.runtime_paths import RUNTIME_ROOT_ENV_VAR
from maple_next.opponent_intel_db.snapshot_store import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotDocument,
    encode_snapshot_document,
)


def _pair(species_id: str, move_name: str) -> tuple[bytes, bytes]:
    record = SpeciesStatsRecord(
        species_id=species_id,
        display_name=species_id,
        season="M-5",
        format="single",
        source="fixture",
        source_url=f"https://example.invalid/{species_id}",
        source_updated_at=None,
        fetched_at="2026-08-12T00:00:00+00:00",
        ranking=1.0,
        moves=(RankedEntry(move_name, 50.0),),
    )
    document = SnapshotDocument(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        source="fixture",
        season="M-5",
        format="single",
        fetched_at="2026-08-12T00:00:00+00:00",
        species={species_id: record},
    )
    return encode_snapshot_document(document), encode_move_catalog(build_move_catalog(document))


def _commit(intel: Path, species_id: str, move_name: str) -> gs.GenerationPointer:
    snapshot, catalog = _pair(species_id, move_name)
    return gs.commit_generation(
        intel,
        snapshot_bytes=snapshot,
        catalog_bytes=catalog,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        catalog_schema_version=MOVE_CATALOG_SCHEMA_VERSION,
        source="fixture",
        created_at="2026-08-12T00:00:00+00:00",
    )


def _assert_window_pair(window, species_id: str, move_name: str) -> None:
    assert window._runtime_intel_bundle is not None  # noqa: SLF001
    assert window._opponent_meta_provider.get(species_id) is not None  # noqa: SLF001
    ranked = window._load_move_matcher().rank(move_name)  # noqa: SLF001
    assert ranked and ranked[0].canonical_name == move_name


def _build_runtime_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    root = tmp_path / "runtime"
    monkeypatch.setenv(RUNTIME_ROOT_ENV_VAR, str(root))
    window_root = tmp_path / name
    window_root.mkdir()
    return build_window(window_root, auto_start_capture=False)


def test_open_window_pins_g1_while_new_window_resolves_g2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intel = tmp_path / "runtime" / "opponent_intel_db"
    g1 = _commit(intel, "oldmon", "Old Move")
    repo1, _controller1, window1, _transport1 = _build_runtime_window(
        tmp_path, monkeypatch, "window-1"
    )
    assert window1._runtime_intel_bundle.generation_id == g1.generation_id  # noqa: SLF001
    _assert_window_pair(window1, "oldmon", "Old Move")

    g2 = _commit(intel, "newmon", "New Move")
    _assert_window_pair(window1, "oldmon", "Old Move")
    assert window1._opponent_meta_provider.get("newmon") is None  # noqa: SLF001
    assert all(  # noqa: SLF001
        candidate.canonical_name != "New Move"
        for candidate in window1._load_move_matcher().rank("New Move")
    )

    repo2, _controller2, window2, _transport2 = _build_runtime_window(
        tmp_path, monkeypatch, "window-2"
    )
    assert window2._runtime_intel_bundle.generation_id == g2.generation_id  # noqa: SLF001
    _assert_window_pair(window2, "newmon", "New Move")

    window1.close()
    window2.close()
    repo1.close()
    repo2.close()


@pytest.mark.parametrize(
    "mirror_state",
    ["snapshot-new-catalog-old", "snapshot-old-catalog-new", "interrupted-between"],
)
def test_mixed_flat_mirrors_never_affect_pointer_selected_production_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mirror_state: str,
) -> None:
    intel = tmp_path / "runtime" / "opponent_intel_db"
    old_snapshot, old_catalog = _pair("oldmon", "Old Move")
    new_snapshot, new_catalog = _pair("newmon", "New Move")
    _commit(intel, "newmon", "New Move")
    if mirror_state == "snapshot-new-catalog-old":
        snapshot_bytes, catalog_bytes = new_snapshot, old_catalog
    elif mirror_state == "snapshot-old-catalog-new":
        snapshot_bytes, catalog_bytes = old_snapshot, new_catalog
    else:
        snapshot_bytes, catalog_bytes = new_snapshot, old_catalog
    (intel / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(snapshot_bytes)
    (intel / gs.DEFAULT_CATALOG_FILENAME).write_bytes(catalog_bytes)

    repo, _controller, window, _transport = _build_runtime_window(
        tmp_path, monkeypatch, "window"
    )
    _assert_window_pair(window, "newmon", "New Move")
    assert window._opponent_meta_provider.get("oldmon") is None  # noqa: SLF001

    window.close()
    repo.close()


@pytest.mark.parametrize("corruption", ["pointer", "snapshot-hash", "catalog-hash"])
def test_corrupt_generation_fails_closed_without_flat_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    intel = tmp_path / "runtime" / "opponent_intel_db"
    pointer = _commit(intel, "newmon", "New Move")
    flat_snapshot, flat_catalog = _pair("legacymon", "Legacy Move")
    (intel / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(flat_snapshot)
    (intel / gs.DEFAULT_CATALOG_FILENAME).write_bytes(flat_catalog)
    if corruption == "pointer":
        (intel / gs.POINTER_FILENAME).write_text("not json", encoding="utf-8")
    else:
        filename = (
            gs.DEFAULT_SNAPSHOT_FILENAME
            if corruption == "snapshot-hash"
            else gs.DEFAULT_CATALOG_FILENAME
        )
        target = intel / gs.GENERATIONS_DIRNAME / pointer.generation_id / filename
        target.write_bytes(b"tampered")

    repo, _controller, window, _transport = _build_runtime_window(
        tmp_path, monkeypatch, "window"
    )
    assert window._runtime_intel_bundle is None  # noqa: SLF001
    assert window._opponent_meta_provider.get("legacymon") is None  # noqa: SLF001
    assert window._load_move_matcher().rank("Legacy Move") == []  # noqa: SLF001

    window.close()
    repo.close()


def test_pointer_missing_with_two_valid_legacy_files_builds_labeled_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intel = tmp_path / "runtime" / "opponent_intel_db"
    intel.mkdir(parents=True)
    snapshot, catalog = _pair("legacymon", "Legacy Move")
    (intel / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(snapshot)
    (intel / gs.DEFAULT_CATALOG_FILENAME).write_bytes(catalog)

    bundle = resolve_runtime_intel_bundle(intel)

    assert bundle is not None
    assert bundle.is_legacy
    assert bundle.generation_id.startswith("legacy:")
    assert bundle.snapshot_document.species["legacymon"].display_name == "legacymon"
    assert bundle.catalog_names == ("Legacy Move",)

    repo, _controller, window, _transport = _build_runtime_window(
        tmp_path, monkeypatch, "window"
    )
    assert window._runtime_intel_bundle is not None  # noqa: SLF001
    assert window._runtime_intel_bundle.is_legacy  # noqa: SLF001
    _assert_window_pair(window, "legacymon", "Legacy Move")
    window.close()
    repo.close()


@pytest.mark.parametrize("present", ["snapshot", "catalog"])
def test_pointer_missing_with_only_one_legacy_file_fails_closed(
    tmp_path: Path, present: str
) -> None:
    intel = tmp_path / "intel"
    intel.mkdir()
    snapshot, catalog = _pair("legacymon", "Legacy Move")
    if present == "snapshot":
        (intel / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(snapshot)
    else:
        (intel / gs.DEFAULT_CATALOG_FILENAME).write_bytes(catalog)

    with pytest.raises(gs.GenerationStoreError, match="LEGACY_PAIR_INCOMPLETE"):
        resolve_runtime_intel_bundle(intel)


def test_legacy_snapshot_catalog_content_mismatch_fails_closed(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    intel.mkdir()
    snapshot, _catalog = _pair("legacymon", "Legacy Move")
    _other_snapshot, other_catalog = _pair("othermon", "Other Move")
    (intel / gs.DEFAULT_SNAPSHOT_FILENAME).write_bytes(snapshot)
    (intel / gs.DEFAULT_CATALOG_FILENAME).write_bytes(other_catalog)

    with pytest.raises(gs.GenerationStoreError, match="CONTENT_MISMATCH"):
        resolve_runtime_intel_bundle(intel)
