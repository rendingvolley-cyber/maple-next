"""Atomic multi-file "generation" commit for the snapshot + move catalog pair.

Two independent ``os.replace`` calls (write the snapshot, then write the
catalog) cannot guarantee a reader never observes a mismatched pairing -- a
crash, kill, or disk-full error between the two calls leaves whichever file
was replaced first paired with the *other* file's previous generation
(new snapshot + old catalog, or old snapshot + new catalog).

This module closes that gap: both files are built completely inside a
staging directory scoped to one generation id, fsynced, and a manifest
describing both (ids, filenames, SHA-256 hashes, schema versions, source
metadata) is written alongside them -- *then*, and only then, a single
small ``current_generation.json`` pointer file is atomically replaced to
make that whole generation visible in one indivisible step. A reader that
wants the coherent-generation guarantee resolves the active generation
through that pointer (:func:`read_current_generation`) rather than reading
the per-generation files directly.

If anything fails before the final pointer replace -- building a file,
fsyncing, writing the manifest -- the staging directory is discarded and
the previously active generation (the pointer, and whatever it currently
points at) is left completely untouched; there is nothing to "roll back"
in production because nothing production-visible was touched yet. A stale
staging directory left behind by an interrupted run is simply orphaned: the
pointer never referenced it, so it can never become active on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

GENERATIONS_DIRNAME = ".generations"
POINTER_FILENAME = "current_generation.json"
POINTER_SCHEMA_VERSION = "opponent-intel-generation-pointer.v1"
MANIFEST_FILENAME = "generation_manifest.json"
DEFAULT_SNAPSHOT_FILENAME = "species_stats_snapshot.json"
DEFAULT_CATALOG_FILENAME = "move_catalog.json"


class GenerationStoreError(Exception):
    """Raised when a committed generation cannot be trusted as-is.

    Deliberately fails closed rather than serving a corrupted, tampered, or
    partially-written generation -- callers should treat this the same as
    "no usable data available", never fall back to guessing.
    """


@dataclass(frozen=True, slots=True)
class GenerationPointer:
    generation_id: str
    snapshot_filename: str
    catalog_filename: str
    snapshot_sha256: str
    catalog_sha256: str
    snapshot_schema_version: str
    catalog_schema_version: str
    source: str
    created_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "pointer_schema_version": POINTER_SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "snapshot_filename": self.snapshot_filename,
            "catalog_filename": self.catalog_filename,
            "snapshot_sha256": self.snapshot_sha256,
            "catalog_sha256": self.catalog_sha256,
            "snapshot_schema_version": self.snapshot_schema_version,
            "catalog_schema_version": self.catalog_schema_version,
            "source": self.source,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_json_dict(data: Any) -> GenerationPointer:
        if not isinstance(data, dict):
            raise GenerationStoreError(
                f"generation pointer must be an object, got {type(data).__name__}"
            )
        required = (
            "generation_id",
            "snapshot_filename",
            "catalog_filename",
            "snapshot_sha256",
            "catalog_sha256",
            "snapshot_schema_version",
            "catalog_schema_version",
            "source",
            "created_at",
        )
        try:
            values = {key: str(data[key]) for key in required}
        except KeyError as exc:
            raise GenerationStoreError(f"generation pointer missing required key: {exc}") from exc
        return GenerationPointer(**values)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if temporary.exists():
        temporary.unlink()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory-entry durability; unsupported platforms no-op."""

    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def commit_generation(
    intel_directory: Path,
    *,
    snapshot_bytes: bytes,
    catalog_bytes: bytes,
    snapshot_schema_version: str,
    catalog_schema_version: str,
    source: str,
    created_at: str,
    snapshot_filename: str = DEFAULT_SNAPSHOT_FILENAME,
    catalog_filename: str = DEFAULT_CATALOG_FILENAME,
) -> GenerationPointer:
    """Stage both files, validate, then commit with one atomic pointer flip.

    Either this fully commits a brand-new generation (the previously active
    generation, if any, is superseded only by the final pointer replace) or
    it raises and leaves the previously active generation exactly as it
    was -- there is no state in between that a reader can observe.
    """

    generations_root = intel_directory / GENERATIONS_DIRNAME
    generations_root.mkdir(parents=True, exist_ok=True)
    generation_id = uuid4().hex
    staging_dir = generations_root / generation_id

    try:
        staging_dir.mkdir(parents=True, exist_ok=False)

        snapshot_path = staging_dir / snapshot_filename
        catalog_path = staging_dir / catalog_filename
        _atomic_write_bytes(snapshot_path, snapshot_bytes)
        _atomic_write_bytes(catalog_path, catalog_bytes)

        pointer = GenerationPointer(
            generation_id=generation_id,
            snapshot_filename=snapshot_filename,
            catalog_filename=catalog_filename,
            snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
            catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
            snapshot_schema_version=snapshot_schema_version,
            catalog_schema_version=catalog_schema_version,
            source=source,
            created_at=created_at,
        )
        manifest_bytes = (
            json.dumps(pointer.to_json_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(staging_dir / MANIFEST_FILENAME, manifest_bytes)
        _fsync_directory(staging_dir)
        _fsync_directory(generations_root)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # The single indivisible step that makes this generation visible. Every
    # file the generation needs already exists, fully written and fsynced,
    # before this line runs.
    pointer_path = intel_directory / POINTER_FILENAME
    _atomic_write_bytes(pointer_path, manifest_bytes)
    _fsync_directory(intel_directory)

    return pointer


@dataclass(frozen=True, slots=True)
class ActiveGeneration:
    pointer: GenerationPointer
    snapshot_path: Path
    catalog_path: Path


def read_current_generation(intel_directory: Path) -> ActiveGeneration | None:
    """Resolve the active generation through the pointer file, hash-verified.

    Returns ``None`` if no generation has ever been committed (no pointer
    file exists yet). Both returned paths always belong to the *same*
    committed generation id -- there is no code path that can return a
    snapshot path from one generation paired with a catalog path from
    another. Raises :class:`GenerationStoreError` if the pointer is
    corrupt, the generation's files are missing, or either file's content
    no longer matches the hash recorded at commit time (tampering or
    partial/corrupted disk state) -- callers must treat that as "no
    trustworthy data", not silently serve stale or mismatched content.
    """

    pointer_path = intel_directory / POINTER_FILENAME
    if not pointer_path.is_file():
        return None

    try:
        raw = json.loads(pointer_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GenerationStoreError(f"POINTER_NOT_VALID_JSON:{exc}") from exc
    pointer = GenerationPointer.from_json_dict(raw)

    generation_dir = intel_directory / GENERATIONS_DIRNAME / pointer.generation_id
    snapshot_path = generation_dir / pointer.snapshot_filename
    catalog_path = generation_dir / pointer.catalog_filename
    if not snapshot_path.is_file() or not catalog_path.is_file():
        raise GenerationStoreError(f"GENERATION_FILES_MISSING:{pointer.generation_id}")

    snapshot_bytes = snapshot_path.read_bytes()
    catalog_bytes = catalog_path.read_bytes()
    if hashlib.sha256(snapshot_bytes).hexdigest() != pointer.snapshot_sha256:
        raise GenerationStoreError(f"SNAPSHOT_HASH_MISMATCH:{pointer.generation_id}")
    if hashlib.sha256(catalog_bytes).hexdigest() != pointer.catalog_sha256:
        raise GenerationStoreError(f"CATALOG_HASH_MISMATCH:{pointer.generation_id}")

    return ActiveGeneration(pointer=pointer, snapshot_path=snapshot_path, catalog_path=catalog_path)
