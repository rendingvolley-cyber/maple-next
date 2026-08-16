"""Shared Bundle 5 (Gemini V2) test helpers: hermetic opponent-INTEL fixtures.

Everything here is *fixture* data. No test in this repository may read or
write the operator's real provisioned artifact
(``%LOCALAPPDATA%\\MapleNext\\Battle1\\opponent_intel_db``); the autouse
``isolated_runtime_root`` fixture in ``tests/conftest.py`` redirects runtime
root resolution for the whole suite, and every helper below writes only into
a caller-supplied temporary directory.

The fixture snapshot is deliberately small and hand-built so the population
expectations in the Bundle 5 tests (top-8 truncation, preserved source
ordering, a genuinely null percentage, a legitimately empty category, an
absent species) are exact rather than incidental.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maple_next.domain.opponent_intel_context import (
    REASON_PIN_UNAVAILABLE,
    unavailable_opponent_intel_context,
)
from maple_next.domain.turn_state import ConfirmedTurnState
from maple_next.opponent_intel_db.generation_store import (
    DEFAULT_CATALOG_FILENAME,
    DEFAULT_SNAPSHOT_FILENAME,
)
from maple_next.opponent_intel_db.move_catalog_builder import MOVE_CATALOG_SCHEMA_VERSION
from maple_next.opponent_intel_db.runtime_intel import (
    PinnableGeneration,
    resolve_pinnable_generation,
)
from maple_next.opponent_intel_db.snapshot_store import SNAPSHOT_SCHEMA_VERSION

FIXTURE_SOURCE = "bundle5-fixture"
#: Matches Bundle 4's pinned ``effective_period.season_id`` (M-5) and
#: ``facts.battle_format`` (SINGLE) -- computed from canonical fields, so
#: these two strings are what MATCHED compatibility is proven against.
FIXTURE_SEASON = "M-5"
FIXTURE_FORMAT = "single"
FIXTURE_FETCHED_AT = "2026-08-11T12:17:49.394136+00:00"
FIXTURE_GENERATED_AT = "2026-08-11T12:20:00+00:00"

AMOONGUSS_ID = "amoonguss"
AMOONGUSS_DISPLAY = "モロバレル"
GARCHOMP_ID = "garchomp"
GARCHOMP_DISPLAY = "ガブリアス"

#: Confirmed in the Bundle 3 historical fixture's battle memory (Turn 6,
#: opponent MOVE) *and* present in the population prior -- the pair used to
#: prove both can coexist without authority confusion.
CONFIRMED_MEMORY_MOVE = "キノコのほうし"
#: High in the population prior and never confirmed in this match -- the
#: "prior only" move.
PRIOR_ONLY_MOVE = "ヘドロばくだん"
#: The 9th entry: proves top-8 truncation drops it rather than re-ranking.
TRUNCATED_MOVE = "タネばくだん"
#: Carries a genuinely null percentage inside the top 8.
NULL_PERCENTAGE_MOVE = "ヘドロウェーブ"


def _amoonguss_record() -> dict[str, Any]:
    return {
        "species_id": AMOONGUSS_ID,
        "display_name": AMOONGUSS_DISPLAY,
        "season": FIXTURE_SEASON,
        "format": FIXTURE_FORMAT,
        "source": FIXTURE_SOURCE,
        "source_url": "https://example.invalid/fixture/amoonguss",
        "source_updated_at": None,
        "fetched_at": FIXTURE_FETCHED_AT,
        "ranking": 12.0,
        # Nine entries on purpose: exactly one more than TOP_N_PER_CATEGORY.
        "moves": [
            {"name": CONFIRMED_MEMORY_MOVE, "percentage": 92.0},
            {"name": PRIOR_ONLY_MOVE, "percentage": 78.0},
            {"name": "まもる", "percentage": 61.0},
            {"name": "クリアスモッグ", "percentage": 44.0},
            {"name": "ギガドレイン", "percentage": 31.0},
            {"name": "いかりのこな", "percentage": 22.0},
            {"name": "やどりぎのタネ", "percentage": 12.0},
            {"name": NULL_PERCENTAGE_MOVE, "percentage": None},
            {"name": TRUNCATED_MOVE, "percentage": 3.0},
        ],
        "items": [
            {"name": "くろいヘドロ", "percentage": 55.0},
            {"name": "オボンのみ", "percentage": 20.0},
        ],
        "abilities": [
            {"name": "さいせいりょく", "percentage": 88.0},
            {"name": "ぼうじん", "percentage": 12.0},
        ],
        "natures": [
            {"name": "なまいき", "percentage": 40.0},
            {"name": "ずぶとい", "percentage": 30.0},
        ],
        "partners": [
            {"name": "ランドロス", "percentage": 24.0},
            {"name": "カイオーガ", "percentage": 11.0},
        ],
        "spreads": [],
    }


def _garchomp_record() -> dict[str, Any]:
    return {
        "species_id": GARCHOMP_ID,
        "display_name": GARCHOMP_DISPLAY,
        "season": FIXTURE_SEASON,
        "format": FIXTURE_FORMAT,
        "source": FIXTURE_SOURCE,
        "source_url": "https://example.invalid/fixture/garchomp",
        "source_updated_at": "2026-08-10T00:00:00+00:00",
        "fetched_at": FIXTURE_FETCHED_AT,
        "ranking": 3.0,
        "moves": [
            {"name": "じしん", "percentage": 90.0},
            {"name": "ドラゴンクロー", "percentage": 70.0},
        ],
        "items": [{"name": "いのちのたま", "percentage": 40.0}],
        "abilities": [{"name": "すながくれ", "percentage": 100.0}],
        # Legitimately empty category: must stay empty, never invented.
        "natures": [],
        "partners": [{"name": "モロバレル", "percentage": 18.0}],
        "spreads": [],
    }


_RECORD_BUILDERS = {AMOONGUSS_ID: _amoonguss_record, GARCHOMP_ID: _garchomp_record}


def fixture_snapshot_dict(
    *,
    species_ids: tuple[str, ...] = (AMOONGUSS_ID, GARCHOMP_ID),
    season: str = FIXTURE_SEASON,
    format: str = FIXTURE_FORMAT,
) -> dict[str, Any]:
    """One hermetic ``opponent-intel-snapshot.v1`` document, as a plain dict."""

    species: dict[str, Any] = {}
    for species_id in species_ids:
        record = _RECORD_BUILDERS[species_id]()
        record["season"] = season
        record["format"] = format
        species[species_id] = record
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source": FIXTURE_SOURCE,
        "season": season,
        "format": format,
        "fetched_at": FIXTURE_FETCHED_AT,
        "species": species,
    }


def _encode(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def fixture_catalog_dict(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The move catalog that exactly matches ``snapshot``.

    Built with a fixed ``generated_at`` (production's
    ``build_move_catalog`` stamps ``datetime.now``) so fixture bytes are
    reproducible and byte-level materialization assertions are meaningful.
    """

    counts: dict[str, int] = {}
    for record in snapshot["species"].values():
        for name in {entry["name"] for entry in record["moves"]}:
            counts[name] = counts.get(name, 0) + 1
    return {
        "schema_version": MOVE_CATALOG_SCHEMA_VERSION,
        "generated_at": FIXTURE_GENERATED_AT,
        "moves": [
            {"canonical_name": name, "seen_species_count": counts[name]}
            for name in sorted(counts)
        ],
    }


def fixture_pair_bytes(snapshot: dict[str, Any]) -> tuple[bytes, bytes]:
    """The exact flat-pair bytes for one fixture snapshot dict."""

    return _encode(snapshot), _encode(fixture_catalog_dict(snapshot))


def write_flat_pair(intel_directory: Path, snapshot: dict[str, Any]) -> tuple[bytes, bytes]:
    """Write a fixture flat pair, exactly as a provisioned runtime would hold it."""

    intel_directory.mkdir(parents=True, exist_ok=True)
    snapshot_bytes, catalog_bytes = fixture_pair_bytes(snapshot)
    (intel_directory / DEFAULT_SNAPSHOT_FILENAME).write_bytes(snapshot_bytes)
    (intel_directory / DEFAULT_CATALOG_FILENAME).write_bytes(catalog_bytes)
    return snapshot_bytes, catalog_bytes


def provision_fixture_generation(
    intel_directory: Path, snapshot: dict[str, Any] | None = None
) -> PinnableGeneration:
    """Provision a flat fixture pair and archive it, exactly as production does."""

    write_flat_pair(intel_directory, snapshot or fixture_snapshot_dict())
    pinnable = resolve_pinnable_generation(intel_directory)
    assert pinnable is not None
    return pinnable


def unavailable_intel_context_for(confirmed_state: ConfirmedTurnState) -> dict[str, Any]:
    """A valid ``UNAVAILABLE`` context bound to a state's confirmed opponent active.

    The shape every pre-Bundle-5 rich-request-assembly test needs: no
    population data at all, but correctly bound to the reviewed confirmed
    opponent active so the request builder's binding check passes. Mirrors
    exactly what an unpinned match resolves to in production.
    """

    active = confirmed_state.opponent_side.active
    return unavailable_opponent_intel_context(
        reason=REASON_PIN_UNAVAILABLE,
        confirmed_active_species=active.value if active.is_confirmed else None,
        compatibility_reason=REASON_PIN_UNAVAILABLE,
    )
