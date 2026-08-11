"""Build a flat, deduplicated move-name catalog from a snapshot document.

Consumed later by the UI layer for move-name autocomplete (Part D/E) -- kept
here, not in the UI, because it derives purely from the offline snapshot
with no runtime/battle dependency.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.opponent_intel_db.snapshot_store import SnapshotDocument

MOVE_CATALOG_SCHEMA_VERSION = "opponent-intel-move-catalog.v1"


def build_move_catalog(document: SnapshotDocument) -> dict[str, Any]:
    """Dedup every move name seen across every species' ``moves`` list.

    Returns a JSON-serializable dict:
    ``{"schema_version", "generated_at", "moves": [{"canonical_name", "seen_species_count"}, ...]}``
    sorted by ``canonical_name``.
    """

    species_count_by_move: dict[str, int] = {}
    for record in document.species.values():
        seen_this_species: set[str] = {entry.name for entry in record.moves}
        for move_name in seen_this_species:
            species_count_by_move[move_name] = species_count_by_move.get(move_name, 0) + 1

    moves = [
        {"canonical_name": name, "seen_species_count": species_count_by_move[name]}
        for name in sorted(species_count_by_move)
    ]

    return {
        "schema_version": MOVE_CATALOG_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "moves": moves,
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def encode_move_catalog(catalog: dict[str, Any]) -> bytes:
    """The exact canonical bytes a move catalog dict serializes to.

    Shared by :func:`write_move_catalog_atomic` and the atomic multi-file
    generation commit in ``generation_store.py`` for the same
    byte-identical-in-both-places reason as ``snapshot_store.
    encode_snapshot_document``.
    """

    text = json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def write_move_catalog_atomic(path: Path, catalog: dict[str, Any]) -> None:
    encoded = encode_move_catalog(catalog)
    _atomic_write(path, encoded)
