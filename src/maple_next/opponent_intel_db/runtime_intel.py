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
    materialize_generation,
    read_current_generation,
    read_generation,
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
    #: SHA-256 of the exact snapshot bytes this bundle was validated from.
    #: Bundle 5 (Gemini V2) pins this value per match, so a later
    #: substitution of different bytes under the same id fails closed.
    snapshot_sha256: str


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


def composite_pair_digest(snapshot_bytes: bytes, catalog_bytes: bytes) -> str:
    """The one deterministic identity convention for a flat snapshot/catalog pair.

    Factored out (behavior unchanged) so the legacy runtime identity
    ``legacy:<digest>`` and the Bundle 5 archived generation id
    :func:`materialized_generation_id` are provably derived from the exact
    same bytes, in the exact same way -- rather than each inventing its own
    convention.
    """

    return hashlib.sha256(snapshot_bytes + b"\0" + catalog_bytes).hexdigest()


def materialized_generation_id(snapshot_bytes: bytes, catalog_bytes: bytes) -> str:
    """The deterministic archived-generation id for one exact flat pair.

    Reuses :func:`composite_pair_digest` verbatim, so the id is
    reproducibly tied to the exact accepted snapshot + catalog bytes and
    never random. The ``legacy:`` runtime-identity spelling cannot be
    reused literally because a generation id is also a *directory name*
    and ``:`` is not a legal path character on Windows; the prefix is
    therefore ``materialized-``, over the identical digest.
    """

    return f"materialized-{composite_pair_digest(snapshot_bytes, catalog_bytes)}"


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
        "legacy:" + composite_pair_digest(snapshot_bytes, catalog_bytes)
    )
    return RuntimeIntelBundle(
        generation_id=resolved_generation_id,
        snapshot_path=snapshot_path,
        catalog_path=catalog_path,
        snapshot_document=document,
        catalog_names=catalog_names,
        is_legacy=is_legacy,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
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


@dataclass(frozen=True, slots=True)
class PinnableGeneration:
    """The immutable generation identity a brand-new match may pin.

    ``generation_id`` always addresses an archived generation directory
    that :func:`load_pinned_generation` can resolve *without* consulting
    ``current_generation.json``, so a later pointer flip can never change
    what an existing match resolves.
    """

    generation_id: str
    snapshot_sha256: str
    #: ``True`` when this identity was created by archiving the currently
    #: provisioned flat pair (rather than already existing as a committed,
    #: pointer-published generation).
    materialized: bool


def resolve_pinnable_generation(intel_directory: Path) -> PinnableGeneration | None:
    """Resolve -- archiving first if required -- the generation a new match pins.

    Bundle 5 (Gemini V2). Two accepted shapes, no others:

    - a pointer-published generation already exists: its id is already
      immutable and id-addressable, so it is pinned as-is and **nothing is
      written**;
    - only the flat ``species_stats_snapshot.json`` /``move_catalog.json``
      pair exists (the currently provisioned runtime shape): its exact
      bytes are validated and then archived verbatim under a deterministic,
      content-derived generation id. No download, no source parsing, no
      re-normalization, no mutation of the flat files, and no pointer flip.

    Returns ``None`` when no opponent-INTEL artifact is provisioned at all.
    Raises :class:`GenerationStoreError` when an artifact exists but cannot
    be trusted (corrupt, incomplete pair, mismatched catalog) -- callers at
    match-creation time treat that as "pin UNAVAILABLE", never as "pin
    whatever parsed".
    """

    pointer_path = intel_directory / POINTER_FILENAME
    if pointer_path.exists():
        active = read_current_generation(intel_directory)
        if active is None:  # pragma: no cover - pointer existence makes this impossible
            raise GenerationStoreError("POINTER_EXISTS_BUT_NO_ACTIVE_GENERATION")
        return PinnableGeneration(
            generation_id=active.pointer.generation_id,
            snapshot_sha256=active.pointer.snapshot_sha256,
            materialized=False,
        )

    snapshot_path = intel_directory / DEFAULT_SNAPSHOT_FILENAME
    catalog_path = intel_directory / DEFAULT_CATALOG_FILENAME
    snapshot_exists = snapshot_path.is_file()
    catalog_exists = catalog_path.is_file()
    if not snapshot_exists and not catalog_exists:
        return None
    if not snapshot_exists or not catalog_exists:
        raise GenerationStoreError("LEGACY_PAIR_INCOMPLETE")

    # Validate exactly as the runtime resolver does before anything is
    # archived: an unusable pair must never become an immutable generation.
    bundle = _validated_bundle(
        generation_id=None,
        snapshot_path=snapshot_path,
        catalog_path=catalog_path,
        is_legacy=True,
    )
    snapshot_bytes = snapshot_path.read_bytes()
    catalog_bytes = catalog_path.read_bytes()
    generation_id = materialized_generation_id(snapshot_bytes, catalog_bytes)
    manifest = materialize_generation(
        intel_directory,
        generation_id=generation_id,
        snapshot_bytes=snapshot_bytes,
        catalog_bytes=catalog_bytes,
        snapshot_schema_version=bundle.snapshot_document.schema_version,
        catalog_schema_version=MOVE_CATALOG_SCHEMA_VERSION,
        source=bundle.snapshot_document.source,
        # Deterministic on purpose: the archive records when the data was
        # fetched, never when this process happened to run, so the same
        # bytes always produce a byte-identical manifest.
        created_at=bundle.snapshot_document.fetched_at,
    )
    return PinnableGeneration(
        generation_id=manifest.generation_id,
        snapshot_sha256=manifest.snapshot_sha256,
        materialized=True,
    )


def load_pinned_generation(intel_directory: Path, generation_id: str) -> RuntimeIntelBundle:
    """Load the exact archived generation named by a match's pin. Fails closed.

    Never consults ``current_generation.json`` and never falls back to the
    flat pair: a match resolves the generation it pinned, or nothing. The
    archived bytes are hash-verified against the generation's own manifest
    and then re-validated through the same strict decoder the runtime
    resolver uses, so a corrupt or substituted archive raises
    :class:`GenerationStoreError` instead of yielding degraded content.
    """

    archived = read_generation(intel_directory, generation_id)
    return _validated_bundle(
        generation_id=archived.pointer.generation_id,
        snapshot_path=archived.snapshot_path,
        catalog_path=archived.catalog_path,
        is_legacy=False,
    )
