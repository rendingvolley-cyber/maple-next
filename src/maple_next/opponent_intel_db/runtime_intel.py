"""Resolve one immutable, validated Opponent INTEL runtime bundle.

The generation pointer is authoritative whenever it exists.  Legacy flat
files are accepted only when no pointer exists and both files form a
structurally valid, mutually consistent pair.  Parsed content is captured at
resolution time so one Battle Record window cannot mix files across updates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maple_next.opponent_intel_db.generation_store import (
    DEFAULT_CATALOG_FILENAME,
    DEFAULT_SNAPSHOT_FILENAME,
    POINTER_FILENAME,
    GenerationStoreError,
    read_current_generation,
)
from maple_next.opponent_intel_db.move_catalog_builder import MOVE_CATALOG_SCHEMA_VERSION
from maple_next.opponent_intel_db.snapshot_store import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotDocument,
    SnapshotStoreError,
)


@dataclass(frozen=True, slots=True)
class RuntimeIntelBundle:
    """One session-pinned snapshot/catalog pair and its parsed content."""

    generation_id: str
    snapshot_path: Path
    catalog_path: Path
    snapshot_document: SnapshotDocument
    catalog_names: tuple[str, ...]
    is_legacy: bool


def _decode_snapshot(content: bytes) -> SnapshotDocument:
    try:
        raw = json.loads(content)
        document = SnapshotDocument.from_json_dict(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, SnapshotStoreError) as exc:
        raise GenerationStoreError(f"SNAPSHOT_INVALID:{exc}") from exc
    if document.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise GenerationStoreError(
            f"SNAPSHOT_SCHEMA_UNSUPPORTED:{document.schema_version!r}"
        )
    return document


def _decode_catalog(content: bytes) -> tuple[tuple[str, ...], dict[str, int]]:
    try:
        raw: Any = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GenerationStoreError(f"CATALOG_INVALID_JSON:{exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != MOVE_CATALOG_SCHEMA_VERSION:
        raise GenerationStoreError("CATALOG_SCHEMA_INVALID")
    moves = raw.get("moves")
    if not isinstance(moves, list):
        raise GenerationStoreError("CATALOG_MOVES_NOT_LIST")
    counts: dict[str, int] = {}
    for entry in moves:
        if not isinstance(entry, dict):
            raise GenerationStoreError("CATALOG_ENTRY_NOT_OBJECT")
        name = entry.get("canonical_name")
        count = entry.get("seen_species_count")
        if not isinstance(name, str) or not name.strip():
            raise GenerationStoreError("CATALOG_NAME_INVALID")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise GenerationStoreError("CATALOG_SPECIES_COUNT_INVALID")
        if name in counts:
            raise GenerationStoreError("CATALOG_DUPLICATE_NAME")
        counts[name] = count
    return tuple(counts), counts


def _expected_catalog_counts(document: SnapshotDocument) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in document.species.values():
        for move_name in {entry.name for entry in record.moves}:
            counts[move_name] = counts.get(move_name, 0) + 1
    return counts


def _validated_bundle(
    *,
    generation_id: str | None,
    snapshot_path: Path,
    catalog_path: Path,
    is_legacy: bool,
) -> RuntimeIntelBundle:
    try:
        snapshot_bytes = snapshot_path.read_bytes()
        catalog_bytes = catalog_path.read_bytes()
    except OSError as exc:
        raise GenerationStoreError(f"RUNTIME_BUNDLE_READ_FAILED:{exc}") from exc
    document = _decode_snapshot(snapshot_bytes)
    catalog_names, catalog_counts = _decode_catalog(catalog_bytes)
    if catalog_counts != _expected_catalog_counts(document):
        raise GenerationStoreError("SNAPSHOT_CATALOG_CONTENT_MISMATCH")
    resolved_generation_id = generation_id or (
        "legacy:" + hashlib.sha256(snapshot_bytes + b"\0" + catalog_bytes).hexdigest()
    )
    return RuntimeIntelBundle(
        generation_id=resolved_generation_id,
        snapshot_path=snapshot_path,
        catalog_path=catalog_path,
        snapshot_document=document,
        catalog_names=catalog_names,
        is_legacy=is_legacy,
    )


def resolve_runtime_intel_bundle(intel_directory: Path) -> RuntimeIntelBundle | None:
    """Resolve once, failing closed on corrupt generation or partial legacy data."""

    pointer_path = intel_directory / POINTER_FILENAME
    if pointer_path.exists():
        active = read_current_generation(intel_directory)
        if active is None:  # pragma: no cover - pointer existence makes this impossible
            raise GenerationStoreError("POINTER_EXISTS_BUT_NO_ACTIVE_GENERATION")
        return _validated_bundle(
            generation_id=active.pointer.generation_id,
            snapshot_path=active.snapshot_path,
            catalog_path=active.catalog_path,
            is_legacy=False,
        )

    snapshot_path = intel_directory / DEFAULT_SNAPSHOT_FILENAME
    catalog_path = intel_directory / DEFAULT_CATALOG_FILENAME
    snapshot_exists = snapshot_path.is_file()
    catalog_exists = catalog_path.is_file()
    if not snapshot_exists and not catalog_exists:
        return None
    if not snapshot_exists or not catalog_exists:
        raise GenerationStoreError("LEGACY_PAIR_INCOMPLETE")

    return _validated_bundle(
        generation_id=None,
        snapshot_path=snapshot_path,
        catalog_path=catalog_path,
        is_legacy=True,
    )
