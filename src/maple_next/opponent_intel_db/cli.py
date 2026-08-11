"""``update-opponent-intel`` CLI: the only place network access happens.

This entrypoint is meant to be run explicitly by the user before a battle
session, never during one. See the package docstring in ``__init__.py`` for
the isolation guarantee this structurally provides.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urljoin

from maple_next.opponent_intel_db import parser_champs_pokedb, parser_pokechamdb
from maple_next.opponent_intel_db.discovery import DiscoveryStatus, discover_all_species
from maple_next.opponent_intel_db.downloader import DownloadError, SnapshotDownloader
from maple_next.opponent_intel_db.generation_store import commit_generation
from maple_next.opponent_intel_db.move_catalog_builder import (
    build_move_catalog,
    encode_move_catalog,
    write_move_catalog_atomic,
)
from maple_next.opponent_intel_db.normalize import (
    NormalizationError,
    RankedEntry,
    SpeciesStatsRecord,
    species_record_from_parsed,
)
from maple_next.opponent_intel_db.runtime_paths import (
    ensure_intel_db_directory,
    resolve_intel_runtime_root,
)
from maple_next.opponent_intel_db.snapshot_store import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotDocument,
    encode_snapshot_document,
    write_snapshot_atomic,
)

logger = logging.getLogger("maple_next.opponent_intel_db")

DEFAULT_SEASON = "M-5"
DEFAULT_FORMAT = "single"
POKECHAMDB_BASE_URL = "https://pokechamdb.com"
CHAMPS_POKEDB_BASE_URL = "https://champs.pokedb.tokyo"
SNAPSHOT_FILENAME = "species_stats_snapshot.json"
MOVE_CATALOG_FILENAME = "move_catalog.json"


class CliError(Exception):
    """Fail-closed error for the CLI's primary-source path."""


class ImportStatus(Enum):
    """Outcome of one primary-source import attempt.

    Only :attr:`COMPLETE` may ever be promoted -- every other status leaves
    the previously active generation (if any) completely untouched.
    ``succeeded == attempted`` alone only proves internal consistency of
    whatever subset of species was discovered; it does NOT prove discovery
    itself was exhaustive, so completeness is split into two independently
    provable phases: discovery (:mod:`discovery`, ``DISCOVERY_INCOMPLETE`` /
    ``DISCOVERY_UNPROVABLE``) and per-species fetch (``FETCH_PARTIAL``).
    """

    DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"
    DISCOVERY_UNPROVABLE = "DISCOVERY_UNPROVABLE"
    FETCH_PARTIAL = "FETCH_PARTIAL"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class PrimaryImportResult:
    """A staging-only result: never written to disk until validated complete."""

    status: ImportStatus
    records: tuple[SpeciesStatsRecord, ...]
    attempted_species: int
    succeeded_species: int
    failed_species: tuple[str, ...]
    reason: str | None = None


def _list_page_url(season: str, format_: str) -> str:
    return f"{POKECHAMDB_BASE_URL}/?view=pokemon&format={format_}&season={season}"


def validate_records_for_promotion(records: Sequence[SpeciesStatsRecord]) -> str | None:
    """Defensive final check before a staged result may be promoted.

    Returns ``None`` when the staged records are safe to promote, or a
    reason string when they are not (duplicate species id, or a record
    missing a required identity field). This runs *in addition to* --
    never instead of -- the per-field validation already enforced when each
    record was normalized.
    """

    seen: set[str] = set()
    for record in records:
        if not record.species_id.strip() or not record.display_name.strip():
            return f"RECORD_MISSING_IDENTITY:{record.species_id!r}"
        if record.species_id in seen:
            return f"DUPLICATE_SPECIES_ID:{record.species_id}"
        seen.add(record.species_id)
    return None


def fetch_primary_species(
    downloader: SnapshotDownloader,
    *,
    season: str,
    format_: str,
    species_filter: str | None,
) -> PrimaryImportResult:
    """Fetch + parse the primary (pokechamdb) source into a staged result.

    Discovery completeness is proven first (:func:`discover_all_species`) --
    a result is only ever eligible for promotion once discovery itself is
    proven exhaustive, *and* every discovered species detail page was
    successfully fetched and parsed, *and* the staged records pass a final
    validation pass. Raises :class:`CliError` only when an explicit
    ``--species`` filter matches nothing in the (already discovery-complete)
    list -- every other failure mode is reported as a
    :class:`PrimaryImportResult` status instead of an exception, so the
    caller can apply one uniform promotion gate.
    """

    list_url = _list_page_url(season, format_)
    discovery = discover_all_species(downloader, list_url)
    if discovery.status is not DiscoveryStatus.COMPLETE:
        status = (
            ImportStatus.DISCOVERY_INCOMPLETE
            if discovery.status is DiscoveryStatus.INCOMPLETE
            else ImportStatus.DISCOVERY_UNPROVABLE
        )
        return PrimaryImportResult(
            status=status,
            records=(),
            attempted_species=len(discovery.entries),
            succeeded_species=0,
            failed_species=(),
            reason=f"{discovery.reason} (pages_visited={discovery.pages_visited})",
        )

    stubs = list(discovery.entries)
    if species_filter is not None:
        stubs = [stub for stub in stubs if stub.species_id == species_filter]
        if not stubs:
            raise CliError(f"PRIMARY_SOURCE_SPECIES_NOT_FOUND:{species_filter}")

    records: list[SpeciesStatsRecord] = []
    failed_species: list[str] = []
    for stub in stubs:
        detail_url = urljoin(POKECHAMDB_BASE_URL, stub.detail_path)
        try:
            detail_html = downloader.get(detail_url)
        except DownloadError as exc:
            logger.warning(
                "species %s fetch failed, import is now incomplete (%s)", stub.species_id, exc
            )
            failed_species.append(stub.species_id)
            continue

        fetched_at = datetime.now(UTC).isoformat()
        try:
            parsed = parser_pokechamdb.parse_species_detail(
                detail_html,
                species_id=stub.species_id,
                display_name=stub.display_name,
                season=season,
                format=format_,
                source_url=detail_url,
                fetched_at=fetched_at,
                ranking=float(stub.rank),
            )
            record = species_record_from_parsed(parsed)
        except (parser_pokechamdb.ParseError, NormalizationError) as exc:
            logger.warning(
                "species %s parse failed, import is now incomplete (%s)", stub.species_id, exc
            )
            failed_species.append(stub.species_id)
            continue

        records.append(record)

    attempted = len(stubs)
    succeeded = len(records)

    if failed_species:
        return PrimaryImportResult(
            status=ImportStatus.FETCH_PARTIAL,
            records=tuple(records),
            attempted_species=attempted,
            succeeded_species=succeeded,
            failed_species=tuple(failed_species),
            reason=f"REQUIRED_SPECIES_FETCH_OR_PARSE_FAILED:{len(failed_species)}/{attempted}",
        )

    validation_failure = validate_records_for_promotion(records)
    if validation_failure is not None:
        return PrimaryImportResult(
            status=ImportStatus.VALIDATION_FAILURE,
            records=tuple(records),
            attempted_species=attempted,
            succeeded_species=succeeded,
            failed_species=(),
            reason=validation_failure,
        )

    return PrimaryImportResult(
        status=ImportStatus.COMPLETE,
        records=tuple(records),
        attempted_species=attempted,
        succeeded_species=succeeded,
        failed_species=(),
    )


def merge_secondary_supplement(
    downloader: SnapshotDownloader,
    records: list[SpeciesStatsRecord],
    *,
    species_filter: str | None,
) -> list[SpeciesStatsRecord]:
    """Best-effort: backfill natures/partners from champs.pokedb.tokyo.

    Any failure here (network, robots.txt, parsing) is caught and logged as
    a warning -- the caller never sees an exception from this function, and
    the original ``records`` are returned unchanged on failure.
    """

    merged: list[SpeciesStatsRecord] = []
    for record in records:
        if species_filter is not None and record.species_id != species_filter:
            merged.append(record)
            continue
        if record.natures and record.partners:
            merged.append(record)
            continue

        detail_url = f"{CHAMPS_POKEDB_BASE_URL}/pokemon/{record.species_id}"
        try:
            html = downloader.get(detail_url)
            supplement = parser_champs_pokedb.parse_species_supplement(
                html, species_id=record.species_id
            )
        except Exception as exc:  # noqa: BLE001 - secondary source is always best-effort
            logger.warning(
                "secondary source unavailable for species %s: %s", record.species_id, exc
            )
            merged.append(record)
            continue

        updated = record
        if not record.natures and supplement.get("natures"):
            updated = replace(
                updated,
                natures=tuple(RankedEntry.from_json_dict(item) for item in supplement["natures"]),
            )
        if not record.partners and supplement.get("partners"):
            updated = replace(
                updated,
                partners=tuple(
                    RankedEntry.from_json_dict(item) for item in supplement["partners"]
                ),
            )
        merged.append(updated)

    return merged


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maple_next.opponent_intel_db",
        description="Maple Next offline opponent-intel usage-stats database updater.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser(
        "update-opponent-intel",
        help="Fetch usage stats from pokechamdb.com (+ best-effort champs.pokedb.tokyo) "
        "and write a local offline snapshot.",
    )
    update_parser.add_argument(
        "--runtime-root",
        type=str,
        default=None,
        help="Override the runtime root (defaults to the standard resolution order).",
    )
    update_parser.add_argument(
        "--species",
        type=str,
        default=None,
        help="Restrict the import to a single species slug (for testing).",
    )
    update_parser.add_argument(
        "--min-interval-seconds",
        type=float,
        default=1.5,
        help="Minimum seconds between HTTP requests (default: 1.5).",
    )
    update_parser.add_argument(
        "--skip-secondary",
        action="store_true",
        help="Skip the best-effort champs.pokedb.tokyo supplement fetch entirely.",
    )
    update_parser.add_argument(
        "--season",
        type=str,
        default=DEFAULT_SEASON,
        help=f"Season identifier to fetch (default: {DEFAULT_SEASON}).",
    )
    update_parser.add_argument(
        "--format",
        dest="format_",
        type=str,
        default=DEFAULT_FORMAT,
        help=f"Format identifier to fetch (default: {DEFAULT_FORMAT}).",
    )
    return parser


def run_update_opponent_intel(args: argparse.Namespace) -> int:
    runtime_root = resolve_intel_runtime_root(args.runtime_root)
    intel_directory = ensure_intel_db_directory(runtime_root)

    downloader = SnapshotDownloader(min_interval_seconds=args.min_interval_seconds)

    try:
        result = fetch_primary_species(
            downloader,
            season=args.season,
            format_=args.format_,
            species_filter=args.species,
        )
    except CliError as exc:
        print(f"UPDATE_FAILED: {exc}", file=sys.stderr)
        return 1

    if result.status is not ImportStatus.COMPLETE:
        # Fail-closed promotion gate: a partial, source-failed, or
        # validation-failed staged result is never written to disk. Whatever
        # snapshot/move-catalog files already exist at ``intel_directory``
        # (a previous complete import, or nothing at all) are left exactly
        # as they were -- this function returns before touching either file.
        print(
            f"UPDATE_INCOMPLETE: status={result.status.value} "
            f"attempted={result.attempted_species} succeeded={result.succeeded_species} "
            f"failed_species={list(result.failed_species)} reason={result.reason}",
            file=sys.stderr,
        )
        return 1

    records = list(result.records)
    if not args.skip_secondary:
        records = merge_secondary_supplement(downloader, records, species_filter=args.species)

    fetched_at = datetime.now(UTC).isoformat()

    # Build both the snapshot document and its move catalog completely in
    # memory first -- neither is written anywhere until both are ready, so
    # the coherent-generation commit below always has a matched pair.
    document: SnapshotDocument = SnapshotDocument(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        source=parser_pokechamdb.SOURCE_NAME,
        season=args.season,
        format=args.format_,
        fetched_at=fetched_at,
        species={record.species_id: record for record in records},
    )
    snapshot_bytes = encode_snapshot_document(document)
    catalog: dict[str, Any] = build_move_catalog(document)
    catalog_bytes = encode_move_catalog(catalog)

    # Single indivisible commit: both files land in one staging generation
    # directory and become visible together via one atomic pointer replace.
    # A reader resolving through the pointer can never observe a mismatched
    # snapshot/catalog pairing.
    commit_generation(
        intel_directory,
        snapshot_bytes=snapshot_bytes,
        catalog_bytes=catalog_bytes,
        snapshot_schema_version=document.schema_version,
        catalog_schema_version=str(catalog["schema_version"]),
        source=parser_pokechamdb.SOURCE_NAME,
        created_at=fetched_at,
        snapshot_filename=SNAPSHOT_FILENAME,
        catalog_filename=MOVE_CATALOG_FILENAME,
    )

    # Flat-file compatibility mirror for existing readers (e.g. the battle
    # UI's SnapshotOpponentMetaProvider), written from the exact same
    # already-committed bytes. This mirror step is NOT the source of the
    # atomicity guarantee -- the generation pointer above already is -- it
    # is a best-effort convenience copy for callers that don't yet resolve
    # through the generation pointer.
    snapshot_path = intel_directory / SNAPSHOT_FILENAME
    catalog_path = intel_directory / MOVE_CATALOG_FILENAME
    write_snapshot_atomic(
        snapshot_path,
        records,
        source=parser_pokechamdb.SOURCE_NAME,
        season=args.season,
        format=args.format_,
        fetched_at=fetched_at,
    )
    write_move_catalog_atomic(catalog_path, catalog)

    print(
        f"species_imported={len(records)} "
        f"source={parser_pokechamdb.SOURCE_NAME} "
        f"season={args.season} "
        f"format={args.format_} "
        f"snapshot_path={snapshot_path}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "update-opponent-intel":
        return run_update_opponent_intel(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse.error() always raises SystemExit
