"""Atomic local snapshot persistence for opponent usage statistics.

The atomic-write idiom (temp file + fsync + ``os.replace``) mirrors
``src/maple_next/application/match_export_v3.py::_atomic_write`` exactly, so
a failed/interrupted write can never corrupt a previously-good snapshot: the
old file is only ever replaced by a single atomic rename of a fully-written
temp file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.opponent_intel_db.normalize import NormalizationError, SpeciesStatsRecord

SNAPSHOT_SCHEMA_VERSION = "opponent-intel-snapshot.v1"


class SnapshotStoreError(Exception):
    """Raised when a snapshot file exists but cannot be parsed as valid JSON/shape."""


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


@dataclass(frozen=True, slots=True)
class SnapshotDocument:
    schema_version: str
    source: str
    season: str
    format: str
    fetched_at: str
    species: dict[str, SpeciesStatsRecord]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "season": self.season,
            "format": self.format,
            "fetched_at": self.fetched_at,
            "species": {
                species_id: record.to_json_dict() for species_id, record in self.species.items()
            },
        }

    @staticmethod
    def from_json_dict(data: Any) -> SnapshotDocument:
        if not isinstance(data, dict):
            raise SnapshotStoreError(
                f"snapshot document must be an object, got {type(data).__name__}"
            )
        try:
            schema_version = data["schema_version"]
            source = data["source"]
            season = data["season"]
            format_ = data["format"]
            fetched_at = data["fetched_at"]
            species_raw = data["species"]
        except KeyError as exc:
            raise SnapshotStoreError(f"snapshot document missing required key: {exc}") from exc

        if not isinstance(species_raw, dict):
            raise SnapshotStoreError(
                f"snapshot document 'species' must be an object, got {type(species_raw).__name__}"
            )

        try:
            species = {
                species_id: SpeciesStatsRecord.from_json_dict(record)
                for species_id, record in species_raw.items()
            }
        except NormalizationError as exc:
            raise SnapshotStoreError(
                f"snapshot document has a malformed species record: {exc}"
            ) from exc

        return SnapshotDocument(
            schema_version=schema_version,
            source=source,
            season=season,
            format=format_,
            fetched_at=fetched_at,
            species=species,
        )


def write_snapshot_atomic(
    path: Path,
    records: list[SpeciesStatsRecord],
    *,
    source: str,
    season: str,
    format: str,
    fetched_at: str,
) -> None:
    """Atomically write ``records`` as a :class:`SnapshotDocument` to ``path``.

    On any error building/encoding the payload, ``path`` is left completely
    untouched -- encoding happens fully in memory before the atomic replace.
    """

    document = SnapshotDocument(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        source=source,
        season=season,
        format=format,
        fetched_at=fetched_at,
        species={record.species_id: record for record in records},
    )
    text = json.dumps(document.to_json_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    encoded = text.encode("utf-8")
    _atomic_write(path, encoded)


def read_snapshot(path: Path) -> SnapshotDocument | None:
    """Read a snapshot document, returning ``None`` if it doesn't exist.

    Raises :class:`SnapshotStoreError` if the file exists but is not valid
    JSON or does not match the expected shape -- this lets callers
    distinguish "no snapshot yet" from "snapshot is broken".
    """

    if not path.is_file():
        return None
    raw_bytes = path.read_bytes()
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise SnapshotStoreError(f"snapshot file is not valid JSON: {exc}") from exc
    return SnapshotDocument.from_json_dict(data)
