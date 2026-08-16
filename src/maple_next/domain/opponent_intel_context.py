"""Bundle 5 (Gemini V2): opponent population INTEL as a non-authoritative prior.

This module is a pure, offline domain slice (no network I/O -- ever, and no
filesystem I/O either). It turns one *pinned*, already-validated population
snapshot plus one *confirmed* opponent active species into the compact
``opponent_intel_context`` dict a rich Turn Advice request embeds.

Authority ordering is the whole point of this bundle and is never relaxed:

    CONFIRMED MATCH FACTS
        > CANONICAL RULES / LEGAL POSSIBILITIES
            > POPULATION INTEL
                > UNKNOWN

Population INTEL is therefore always tagged ``authority =
POPULATION_PRIOR``. It describes what *the population* of that species
commonly runs in one archived snapshot; it never describes the confirmed
build of the opponent actually being battled, it never becomes a confirmed
fact, and it never overwrites, deduplicates, promotes, or demotes anything
in the Bundle 3 confirmed battle memory.

Two deliberate non-goals:

- **No inference.** Nothing here manufactures, renormalizes, re-ranks, or
  back-fills a statistic. ``percentage: null`` stays null, an empty
  category stays empty, and an absent species yields ``UNAVAILABLE`` rather
  than a guessed distribution.
- **No blending.** The only accepted input is the snapshot document the
  match pinned. The legacy ``opponent_meta_cache.json`` provider chain
  (``domain/opponent_intel.py``) is deliberately *not* imported here: a
  species missing from the pinned snapshot resolves to ``UNAVAILABLE``,
  never to a legacy cache entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from maple_next.opponent_intel_db.normalize import RankedEntry, SpeciesStatsRecord
from maple_next.opponent_intel_db.snapshot_store import SnapshotDocument

#: Compact contract embedded in a rich Turn Advice request (see
#: ``providers/turn_advice_rich_state.py``, contract v6).
OPPONENT_INTEL_CONTEXT_SCHEMA_VERSION: Final[str] = "maple-opponent-intel-context.v1"

#: The only authority level this context may ever claim. Constant, not
#: caller-supplied: population statistics can never be promoted to a
#: confirmed fact by any code path.
OPPONENT_INTEL_AUTHORITY: Final[str] = "POPULATION_PRIOR"

#: Context status values.
CONTEXT_STATUS_AVAILABLE: Final[str] = "AVAILABLE"
CONTEXT_STATUS_UNAVAILABLE: Final[str] = "UNAVAILABLE"
CONTEXT_STATUS_MISMATCHED: Final[str] = "MISMATCHED"
CONTEXT_STATUSES: Final[frozenset[str]] = frozenset(
    {CONTEXT_STATUS_AVAILABLE, CONTEXT_STATUS_UNAVAILABLE, CONTEXT_STATUS_MISMATCHED}
)

#: Rules/regulation compatibility status values.
COMPATIBILITY_MATCHED: Final[str] = "MATCHED"
COMPATIBILITY_MISMATCHED: Final[str] = "MISMATCHED"
COMPATIBILITY_UNAVAILABLE: Final[str] = "UNAVAILABLE"
COMPATIBILITY_STATUSES: Final[frozenset[str]] = frozenset(
    {COMPATIBILITY_MATCHED, COMPATIBILITY_MISMATCHED, COMPATIBILITY_UNAVAILABLE}
)

#: Durable per-match pin status values.
PIN_STATUS_PINNED: Final[str] = "PINNED"
PIN_STATUS_UNAVAILABLE: Final[str] = "UNAVAILABLE"
PIN_STATUSES: Final[frozenset[str]] = frozenset({PIN_STATUS_PINNED, PIN_STATUS_UNAVAILABLE})

#: Exactly the five population categories this bundle carries, in a fixed
#: order. Never extended at runtime and never partially emitted.
POPULATION_CATEGORIES: Final[tuple[str, ...]] = (
    "moves",
    "abilities",
    "items",
    "natures",
    "partners",
)

#: How many entries per category are carried, counted from the snapshot's
#: own ordering. The snapshot's order is preserved verbatim: the first
#: ``TOP_N_PER_CATEGORY`` entries are taken, nothing is re-sorted,
#: re-normalized, padded, or merged into an "other" bucket.
TOP_N_PER_CATEGORY: Final[int] = 8

#: Deterministic reason codes. A reason is either ``None`` (AVAILABLE) or
#: exactly one of these -- never free-form text, so the same durable state
#: always yields byte-identical context.
REASON_PIN_UNAVAILABLE: Final[str] = "INTEL_PIN_UNAVAILABLE"
REASON_OPPONENT_ACTIVE_NOT_CONFIRMED: Final[str] = "OPPONENT_ACTIVE_NOT_CONFIRMED"
REASON_SPECIES_NOT_IN_SNAPSHOT: Final[str] = "SPECIES_NOT_IN_SNAPSHOT"
REASON_COMPATIBILITY_MISMATCHED: Final[str] = "COMPATIBILITY_MISMATCHED"
REASON_SEASON_MISMATCH: Final[str] = "SEASON_MISMATCH"
REASON_FORMAT_MISMATCH: Final[str] = "FORMAT_MISMATCH"

#: The canonical "no human-confirmed value" token used across the turn-state
#: contract. A confirmed literal ``"UNKNOWN"`` opponent active is an
#: explicit human statement of ignorance, never a species to look up.
_UNKNOWN_SPECIES_TOKEN: Final[str] = "UNKNOWN"

#: Top-level keys every ``opponent_intel_context`` carries, always.
REQUIRED_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "context_schema_version",
        "status",
        "reason",
        "authority",
        "confirmed_active_species",
        "resolved_species",
        "compatibility",
        "snapshot",
        "population",
    }
)


class OpponentIntelContextError(Exception):
    """Fail-closed error for opponent-INTEL context resolution/validation.

    Raised only for situations the spec classifies as *fail-closed* (a
    pinned generation that is missing/corrupt, a pin/generation identity
    mismatch, a context that claims more than it can prove). Situations the
    spec classifies as *fail-soft* -- no pin, no species, empty category,
    null percentage, explicit season/format mismatch -- never raise: they
    produce a deterministic ``UNAVAILABLE``/``MISMATCHED`` context so Turn
    Advice stays provider-ready.
    """


@dataclass(frozen=True, slots=True)
class OpponentIntelPin:
    """The exact population-INTEL identity pinned to one match, persisted verbatim.

    Written exactly once, at match creation, and never rewritten (not by
    ``save_session``, not by later request construction). Holds only
    identity/hash values -- never population content -- so a match row can
    never drift into carrying its own private copy of the statistics.

    ``status == UNAVAILABLE`` is an explicit, durable statement that this
    match has *no* population INTEL, which is materially different from a
    pre-Bundle-5 row whose columns are all ``NULL``. Both resolve to the
    same deterministic ``UNAVAILABLE`` context (see
    :meth:`from_session_fields`) -- neither ever adopts whatever snapshot
    happens to be current now.
    """

    status: str
    generation_id: str | None = None
    snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.status not in PIN_STATUSES:
            raise OpponentIntelContextError(f"INTEL_PIN_STATUS_INVALID:{self.status!r}")
        if self.status == PIN_STATUS_PINNED:
            if not self.generation_id or not self.snapshot_sha256:
                raise OpponentIntelContextError("INTEL_PIN_PINNED_REQUIRES_IDENTITY")
        elif self.generation_id is not None or self.snapshot_sha256 is not None:
            raise OpponentIntelContextError("INTEL_PIN_UNAVAILABLE_MUST_NOT_CARRY_IDENTITY")

    @property
    def is_pinned(self) -> bool:
        return self.status == PIN_STATUS_PINNED

    @staticmethod
    def unavailable() -> OpponentIntelPin:
        return OpponentIntelPin(status=PIN_STATUS_UNAVAILABLE)

    @staticmethod
    def pinned(*, generation_id: str, snapshot_sha256: str) -> OpponentIntelPin:
        return OpponentIntelPin(
            status=PIN_STATUS_PINNED,
            generation_id=generation_id,
            snapshot_sha256=snapshot_sha256,
        )

    @staticmethod
    def from_session_fields(
        *,
        status: str | None,
        generation_id: str | None,
        snapshot_sha256: str | None,
    ) -> OpponentIntelPin:
        """Read one match's persisted pin columns, fail-soft for legacy rows.

        A pre-Bundle-5 match (all three columns ``NULL`` after the additive
        migration) deterministically resolves to ``UNAVAILABLE``. It is
        never backfilled with, and never silently adopts, the currently
        provisioned snapshot -- that would retroactively change what an
        already-played match was reasoned under.
        """

        if status is None:
            return OpponentIntelPin.unavailable()
        if status == PIN_STATUS_UNAVAILABLE:
            return OpponentIntelPin.unavailable()
        return OpponentIntelPin(
            status=status, generation_id=generation_id, snapshot_sha256=snapshot_sha256
        )


def resolve_species_record(
    document: SnapshotDocument, species: str
) -> SpeciesStatsRecord | None:
    """Resolve one confirmed species name against a pinned snapshot, deterministically.

    Match order (first hit wins, and every scan runs over ``sorted`` species
    ids so the result never depends on dict insertion order):

    1. exact ``species_id``;
    2. normalized ``species_id`` (lowercased, spaces -> hyphens);
    3. exact ``display_name``;
    4. case-insensitive ``species_id`` / ``display_name``.

    Returns ``None`` -- never a "closest" or fuzzy match -- when the species
    is genuinely absent. Fail-soft by design: an absent species is an
    ``UNAVAILABLE`` context, not an error and never a legacy-cache lookup.
    """

    key = species.strip()
    if not key or key == _UNKNOWN_SPECIES_TOKEN:
        return None

    direct = document.species.get(key)
    if direct is not None:
        return direct

    normalized = key.lower().replace(" ", "-")
    normalized_hit = document.species.get(normalized)
    if normalized_hit is not None:
        return normalized_hit

    for species_id in sorted(document.species):
        if document.species[species_id].display_name.strip() == key:
            return document.species[species_id]

    folded = key.lower()
    for species_id in sorted(document.species):
        record = document.species[species_id]
        if species_id.lower() == normalized or record.display_name.strip().lower() == folded:
            return record
    return None


def _canonical_entry(entry: RankedEntry) -> dict[str, Any]:
    return {"name": entry.name, "percentage": entry.percentage}


def _top_entries(entries: tuple[RankedEntry, ...]) -> list[dict[str, Any]]:
    """The first ``TOP_N_PER_CATEGORY`` entries, in snapshot order, verbatim.

    Explicitly *not* done here: re-sorting (including "stabilizing" ties
    alphabetically), recomputing or renormalizing percentages so a truncated
    list sums to 100, inventing a zero/"other" entry, padding a short list,
    or substituting a number for a genuine ``null`` percentage.
    """

    return [_canonical_entry(entry) for entry in entries[:TOP_N_PER_CATEGORY]]


def _population_from_record(record: SpeciesStatsRecord) -> dict[str, list[dict[str, Any]]]:
    return {
        "moves": _top_entries(record.moves),
        "abilities": _top_entries(record.abilities),
        "items": _top_entries(record.items),
        "natures": _top_entries(record.natures),
        "partners": _top_entries(record.partners),
    }


def _snapshot_identity(
    document: SnapshotDocument,
    *,
    generation_id: str,
    snapshot_sha256: str,
    source_updated_at: str | None,
) -> dict[str, Any]:
    """Identity/provenance only -- never the database, never raw source documents.

    Deliberately keeps ``fetched_at`` (when this archive was taken) with no
    arbitrary freshness expiry attached: a correctly-matching dated snapshot
    stays usable. ``source_updated_at`` stays ``null`` when the source never
    published one, rather than being back-filled from ``fetched_at``.
    """

    return {
        "schema_version": document.schema_version,
        "generation_id": generation_id,
        "snapshot_sha256": snapshot_sha256,
        "source": document.source,
        "season": document.season,
        "format": document.format,
        "fetched_at": document.fetched_at,
        "source_updated_at": source_updated_at,
    }


def _context(
    *,
    status: str,
    reason: str | None,
    confirmed_active_species: str | None,
    resolved_species: dict[str, Any] | None,
    compatibility: dict[str, Any],
    snapshot: dict[str, Any] | None,
    population: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    return {
        "context_schema_version": OPPONENT_INTEL_CONTEXT_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "authority": OPPONENT_INTEL_AUTHORITY,
        "confirmed_active_species": confirmed_active_species,
        "resolved_species": resolved_species,
        "compatibility": compatibility,
        "snapshot": snapshot,
        "population": population,
    }


def unavailable_opponent_intel_context(
    *,
    reason: str,
    confirmed_active_species: str | None = None,
    compatibility_status: str = COMPATIBILITY_UNAVAILABLE,
    compatibility_reason: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One deterministic ``UNAVAILABLE`` context. Never carries population data."""

    return _context(
        status=CONTEXT_STATUS_UNAVAILABLE,
        reason=reason,
        confirmed_active_species=confirmed_active_species,
        resolved_species=None,
        compatibility={"status": compatibility_status, "reason": compatibility_reason},
        snapshot=snapshot,
        population=None,
    )


def evaluate_rules_compatibility(
    document: SnapshotDocument, *, rules_season_id: str, rules_battle_format: str
) -> tuple[str, str | None]:
    """Compare pinned INTEL against pinned rules using canonical fields only.

    Never compares a rendered/display string: the snapshot's own ``season``
    and ``format`` fields are compared against the pinned Champions rules
    snapshot's ``effective_period.season_id`` and ``facts.battle_format``.
    Format comparison is case-folded because the two artifacts legitimately
    spell the same canonical value differently (``"single"`` vs
    ``"SINGLE"``); season comparison is exact.
    """

    if document.season.strip() != rules_season_id.strip():
        return COMPATIBILITY_MISMATCHED, REASON_SEASON_MISMATCH
    if document.format.strip().upper() != rules_battle_format.strip().upper():
        return COMPATIBILITY_MISMATCHED, REASON_FORMAT_MISMATCH
    return COMPATIBILITY_MATCHED, None


def build_opponent_intel_context(
    *,
    confirmed_active_species: str | None,
    pin: OpponentIntelPin,
    document: SnapshotDocument | None,
    generation_id: str | None,
    snapshot_sha256: str | None,
    rules_season_id: str,
    rules_battle_format: str,
) -> dict[str, Any]:
    """Build the one compact ``opponent_intel_context`` for a rich request.

    Pure: identical inputs always produce an identical dict (and therefore
    identical canonical request bytes). Fail-soft everywhere the spec says
    fail-soft -- unpinned match, unconfirmed opponent active, absent
    species, empty category, null percentage, explicit season/format
    mismatch all produce a deterministic ``UNAVAILABLE``/``MISMATCHED``
    context so Turn Advice still constructs.

    ``document``/``generation_id``/``snapshot_sha256`` must already have
    been resolved *from the match's own pin* and hash-verified by the
    caller (``application/turn_provider_export_bridge.py``); this function
    never reads the filesystem and never resolves "whatever generation is
    current now".
    """

    if not pin.is_pinned:
        return unavailable_opponent_intel_context(
            reason=REASON_PIN_UNAVAILABLE,
            confirmed_active_species=confirmed_active_species,
            compatibility_reason=REASON_PIN_UNAVAILABLE,
        )
    if document is None or generation_id is None or snapshot_sha256 is None:
        # A PINNED match whose generation did not resolve must never
        # degrade into a soft "no data" answer -- that is exactly the
        # silent-substitution failure this bundle exists to prevent.
        raise OpponentIntelContextError("PINNED_GENERATION_NOT_RESOLVED")
    if generation_id != pin.generation_id:
        raise OpponentIntelContextError("RESOLVED_GENERATION_DIFFERS_FROM_PIN")
    if snapshot_sha256 != pin.snapshot_sha256:
        raise OpponentIntelContextError("PINNED_SNAPSHOT_HASH_MISMATCH")

    compatibility_status, compatibility_reason = evaluate_rules_compatibility(
        document, rules_season_id=rules_season_id, rules_battle_format=rules_battle_format
    )
    identity = _snapshot_identity(
        document,
        generation_id=generation_id,
        snapshot_sha256=snapshot_sha256,
        source_updated_at=None,
    )
    if compatibility_status != COMPATIBILITY_MATCHED:
        # Explicitly different season or format: population is null, but the
        # request stays provider-ready and the pinned identity is still
        # reported for audit.
        return _context(
            status=CONTEXT_STATUS_MISMATCHED,
            reason=REASON_COMPATIBILITY_MISMATCHED,
            confirmed_active_species=confirmed_active_species,
            resolved_species=None,
            compatibility={"status": compatibility_status, "reason": compatibility_reason},
            snapshot=identity,
            population=None,
        )

    compatibility = {"status": compatibility_status, "reason": compatibility_reason}
    if not confirmed_active_species or confirmed_active_species == _UNKNOWN_SPECIES_TOKEN:
        return unavailable_opponent_intel_context(
            reason=REASON_OPPONENT_ACTIVE_NOT_CONFIRMED,
            confirmed_active_species=confirmed_active_species,
            compatibility_status=compatibility_status,
            compatibility_reason=compatibility_reason,
            snapshot=identity,
        )

    record = resolve_species_record(document, confirmed_active_species)
    if record is None:
        return unavailable_opponent_intel_context(
            reason=REASON_SPECIES_NOT_IN_SNAPSHOT,
            confirmed_active_species=confirmed_active_species,
            compatibility_status=compatibility_status,
            compatibility_reason=compatibility_reason,
            snapshot=identity,
        )

    return _context(
        status=CONTEXT_STATUS_AVAILABLE,
        reason=None,
        confirmed_active_species=confirmed_active_species,
        resolved_species={
            "species_id": record.species_id,
            "display_name": record.display_name,
        },
        compatibility=compatibility,
        snapshot=_snapshot_identity(
            document,
            generation_id=generation_id,
            snapshot_sha256=snapshot_sha256,
            source_updated_at=record.source_updated_at,
        ),
        population=_population_from_record(record),
    )


def _validate_entries(entries: Any, *, category: str) -> None:
    if not isinstance(entries, list):
        raise OpponentIntelContextError(f"POPULATION_CATEGORY_NOT_LIST:{category}")
    if len(entries) > TOP_N_PER_CATEGORY:
        raise OpponentIntelContextError(f"POPULATION_CATEGORY_TOO_LONG:{category}")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "percentage"}:
            raise OpponentIntelContextError(f"POPULATION_ENTRY_SHAPE_INVALID:{category}")
        name = entry["name"]
        if not isinstance(name, str) or not name.strip():
            raise OpponentIntelContextError(f"POPULATION_ENTRY_NAME_INVALID:{category}")
        percentage = entry["percentage"]
        if percentage is None:
            continue
        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
            raise OpponentIntelContextError(f"POPULATION_ENTRY_PERCENTAGE_INVALID:{category}")
        value = float(percentage)
        if not math.isfinite(value) or not 0.0 <= value <= 100.0:
            raise OpponentIntelContextError(
                f"POPULATION_ENTRY_PERCENTAGE_OUT_OF_RANGE:{category}"
            )


def validate_opponent_intel_context(context: Any) -> None:
    """Fail closed on any context that claims more than it can prove.

    Defence in depth at the provider-request boundary: the value is always
    actually produced by :func:`build_opponent_intel_context`, but a rich
    request must never be assembled from a caller-supplied dict that (for
    example) claims ``AVAILABLE`` while carrying malformed, out-of-range,
    over-length, or entirely absent population data.
    """

    if not isinstance(context, dict):
        raise OpponentIntelContextError("OPPONENT_INTEL_CONTEXT_NOT_OBJECT")
    missing = REQUIRED_CONTEXT_KEYS - context.keys()
    if missing:
        raise OpponentIntelContextError(f"OPPONENT_INTEL_CONTEXT_MISSING_KEYS:{sorted(missing)}")
    extra = context.keys() - REQUIRED_CONTEXT_KEYS
    if extra:
        raise OpponentIntelContextError(f"OPPONENT_INTEL_CONTEXT_UNKNOWN_KEYS:{sorted(extra)}")
    if context["context_schema_version"] != OPPONENT_INTEL_CONTEXT_SCHEMA_VERSION:
        raise OpponentIntelContextError("OPPONENT_INTEL_CONTEXT_UNSUPPORTED_SCHEMA_VERSION")
    if context["authority"] != OPPONENT_INTEL_AUTHORITY:
        raise OpponentIntelContextError("OPPONENT_INTEL_CONTEXT_AUTHORITY_INVALID")

    status = context["status"]
    if status not in CONTEXT_STATUSES:
        raise OpponentIntelContextError(f"OPPONENT_INTEL_CONTEXT_STATUS_INVALID:{status!r}")

    compatibility = context["compatibility"]
    if not isinstance(compatibility, dict) or set(compatibility) != {"status", "reason"}:
        raise OpponentIntelContextError("OPPONENT_INTEL_COMPATIBILITY_SHAPE_INVALID")
    if compatibility["status"] not in COMPATIBILITY_STATUSES:
        raise OpponentIntelContextError("OPPONENT_INTEL_COMPATIBILITY_STATUS_INVALID")

    population = context["population"]
    if status != CONTEXT_STATUS_AVAILABLE:
        if population is not None:
            raise OpponentIntelContextError("NON_AVAILABLE_CONTEXT_MUST_NOT_CARRY_POPULATION")
        if context["resolved_species"] is not None:
            raise OpponentIntelContextError("NON_AVAILABLE_CONTEXT_MUST_NOT_RESOLVE_SPECIES")
        return

    if compatibility["status"] != COMPATIBILITY_MATCHED:
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_REQUIRES_MATCHED_COMPATIBILITY")
    if context["reason"] is not None:
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_MUST_NOT_CARRY_REASON")
    species = context["confirmed_active_species"]
    if not isinstance(species, str) or not species.strip():
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_REQUIRES_CONFIRMED_ACTIVE_SPECIES")
    resolved = context["resolved_species"]
    if not isinstance(resolved, dict) or set(resolved) != {"species_id", "display_name"}:
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_RESOLVED_SPECIES_INVALID")
    snapshot = context["snapshot"]
    if not isinstance(snapshot, dict) or not snapshot.get("generation_id"):
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_REQUIRES_SNAPSHOT_IDENTITY")
    if not isinstance(population, dict) or set(population) != set(POPULATION_CATEGORIES):
        raise OpponentIntelContextError("AVAILABLE_CONTEXT_POPULATION_CATEGORIES_INVALID")
    for category in POPULATION_CATEGORIES:
        _validate_entries(population[category], category=category)
