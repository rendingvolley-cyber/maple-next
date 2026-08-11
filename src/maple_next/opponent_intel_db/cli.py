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
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from maple_next.opponent_intel_db import parser_champs_pokedb, parser_pokechamdb
from maple_next.opponent_intel_db.downloader import DownloadError, SnapshotDownloader
from maple_next.opponent_intel_db.move_catalog_builder import (
    build_move_catalog,
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
    read_snapshot,
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


def _list_page_url(season: str, format_: str) -> str:
    return f"{POKECHAMDB_BASE_URL}/?view=pokemon&format={format_}&season={season}"


def fetch_primary_species(
    downloader: SnapshotDownloader,
    *,
    season: str,
    format_: str,
    species_filter: str | None,
) -> list[SpeciesStatsRecord]:
    """Fetch + parse the primary (pokechamdb) source.

    Raises :class:`CliError` only if the primary source fails completely
    (list page unreachable/unparsable, or zero species end up importable).
    A partial result -- some species parsed, others individually skipped --
    is returned rather than raised.
    """

    list_url = _list_page_url(season, format_)
    try:
        list_html = downloader.get(list_url)
    except DownloadError as exc:
        raise CliError(f"PRIMARY_SOURCE_LIST_PAGE_UNREACHABLE:{exc}") from exc

    try:
        stubs = parser_pokechamdb.parse_species_list(list_html)
    except parser_pokechamdb.ParseError as exc:
        raise CliError(f"PRIMARY_SOURCE_LIST_PAGE_UNPARSABLE:{exc}") from exc

    if species_filter is not None:
        stubs = [stub for stub in stubs if stub.species_id == species_filter]
        if not stubs:
            raise CliError(f"PRIMARY_SOURCE_SPECIES_NOT_FOUND:{species_filter}")

    records: list[SpeciesStatsRecord] = []
    for stub in stubs:
        detail_url = urljoin(POKECHAMDB_BASE_URL, stub.detail_path)
        try:
            detail_html = downloader.get(detail_url)
        except DownloadError as exc:
            logger.warning("skipping species %s: fetch failed (%s)", stub.species_id, exc)
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
            logger.warning("skipping species %s: parse failed (%s)", stub.species_id, exc)
            continue

        records.append(record)

    if not records:
        raise CliError("PRIMARY_SOURCE_YIELDED_ZERO_SPECIES")
    return records


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
        records = fetch_primary_species(
            downloader,
            season=args.season,
            format_=args.format_,
            species_filter=args.species,
        )
    except CliError as exc:
        print(f"UPDATE_FAILED: {exc}", file=sys.stderr)
        return 1

    if not args.skip_secondary:
        records = merge_secondary_supplement(downloader, records, species_filter=args.species)

    fetched_at = datetime.now(UTC).isoformat()
    snapshot_path = intel_directory / SNAPSHOT_FILENAME
    write_snapshot_atomic(
        snapshot_path,
        records,
        source=parser_pokechamdb.SOURCE_NAME,
        season=args.season,
        format=args.format_,
        fetched_at=fetched_at,
    )

    document = read_snapshot(snapshot_path)
    assert document is not None  # we just wrote it
    catalog: dict[str, Any] = build_move_catalog(document)
    catalog_path = intel_directory / MOVE_CATALOG_FILENAME
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
