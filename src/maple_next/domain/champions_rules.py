"""Bundle 4 (Gemini V2): immutable official Pokemon Champions rules snapshot.

This module is a pure, offline domain slice (no network I/O -- ever). It
loads, validates, and exposes exactly one checked-in immutable artifact:

    src/maple_next/data/champions_rules/pokemon-champions-ranked-single/
        regulation-m-b/rules.snapshot.json

That artifact was constructed **once, at implementation time**, from
first-party official Pokemon sources (see ``source_manifest.json`` next to
it and the paired ``evidence/*.html`` archives). Nothing in this module, or
anywhere downstream of it, performs a network fetch: at runtime the snapshot
is read from disk only, and a malformed or tampered snapshot fails closed
rather than silently degrading or falling back to generic Pokemon
knowledge.

Scope is deliberately narrow: only the facts actually asserted by the
official sources are represented (see ``coverage.authoritative_categories``
on the snapshot). Everything else -- switching mechanics, faint/replacement
flow, stat-stage arithmetic, status/weather/terrain mechanics, turn order,
type chart, move effects, damage formulas -- is intentionally *not*
asserted here (see ``coverage.intentionally_not_asserted``) and must never
be guessed into existence by this module or by a caller.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Deterministic snapshot contract version. Bumping this is a breaking change
#: to the on-disk artifact shape, not merely to its content.
SNAPSHOT_SCHEMA_VERSION = "maple-champions-rules-snapshot.v1"

#: Compact contract embedded in a rich Turn Advice request (see
#: ``providers/turn_advice_rich_state.py``, contract v5).
RULES_CONTEXT_SCHEMA_VERSION = "maple-champions-rules-context.v1"

#: The only ruleset/version this bundle ships evidence for. Deliberately not
#: a registry of many regulations -- broadening that is explicitly deferred
#: (see Bundle 4 spec, "IMPORTANT_NONBLOCKING / DEFER").
SUPPORTED_RULESET_ID = "pokemon-champions-ranked-single"
SUPPORTED_RULESET_VERSION = "M-B"
SUPPORTED_SCOPE = "ranked_single"

_MANDATORY_FACT_CATEGORIES: frozenset[str] = frozenset(
    {
        "battle_format",
        "pokemon_selected_to_battle",
        "mega_evolution_availability",
        "mega_evolution_use_limit",
        "duplicate_held_items",
        "timers",
    }
)

_ALLOWED_TOP_LEVEL_FACT_KEYS: frozenset[str] = frozenset(
    {
        "battle_format",
        "pokemon_selected_to_battle",
        "mega_evolution",
        "duplicate_held_items_allowed",
        "timers",
    }
)
_ALLOWED_MEGA_EVOLUTION_KEYS: frozenset[str] = frozenset(
    {"allowed", "max_uses_per_battle", "requires_mega_stone_for_eligible_pokemon"}
)
_ALLOWED_TIMER_KEYS: frozenset[str] = frozenset(
    {
        "total_time_seconds",
        "player_time_seconds",
        "turn_selection_seconds",
        "pokemon_selection_seconds",
    }
)
_ALLOWED_BATTLE_FORMATS: frozenset[str] = frozenset({"SINGLE", "DOUBLE"})
_REQUIRED_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "publisher",
        "url",
        "retrieved_at_utc",
        "archived_content_sha256",
        "archived_evidence_relative_path",
        "source_role",
    }
)

_RULESET_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "champions_rules"
    / "pokemon-champions-ranked-single"
    / "regulation-m-b"
)
_SNAPSHOT_PATH = _RULESET_ROOT / "rules.snapshot.json"

#: Public read-only handles onto the checked-in artifact location, exposed
#: only so tests can exercise tampering/corruption against a real, freshly
#: re-read copy of the actual bundled files without duplicating this
#: module's private path-construction logic.
BUNDLED_RULESET_ROOT = _RULESET_ROOT
BUNDLED_SNAPSHOT_PATH = _SNAPSHOT_PATH


class ChampionsRulesError(Exception):
    """Fail-closed base error for the Champions rules snapshot contract."""


class ChampionsRulesIntegrityError(ChampionsRulesError):
    """The on-disk snapshot (or its evidence) is malformed or tampered. Fail closed."""


class ChampionsRulesPinError(ChampionsRulesError):
    """A match's persisted rules pin could not be resolved. Fail closed."""


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class MegaEvolutionRule:
    allowed: bool
    max_uses_per_battle: int
    requires_mega_stone_for_eligible_pokemon: bool

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "max_uses_per_battle": self.max_uses_per_battle,
            "requires_mega_stone_for_eligible_pokemon": (
                self.requires_mega_stone_for_eligible_pokemon
            ),
        }


@dataclass(frozen=True, slots=True)
class TimersRule:
    total_time_seconds: int
    player_time_seconds: int
    turn_selection_seconds: int
    pokemon_selection_seconds: int

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "total_time_seconds": self.total_time_seconds,
            "player_time_seconds": self.player_time_seconds,
            "turn_selection_seconds": self.turn_selection_seconds,
            "pokemon_selection_seconds": self.pokemon_selection_seconds,
        }


@dataclass(frozen=True, slots=True)
class ChampionsRulesFacts:
    battle_format: str
    pokemon_selected_to_battle: int
    mega_evolution: MegaEvolutionRule
    duplicate_held_items_allowed: bool
    timers: TimersRule

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "battle_format": self.battle_format,
            "pokemon_selected_to_battle": self.pokemon_selected_to_battle,
            "mega_evolution": self.mega_evolution.to_canonical_dict(),
            "duplicate_held_items_allowed": self.duplicate_held_items_allowed,
            "timers": self.timers.to_canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class ChampionsRulesSource:
    publisher: str
    url: str
    retrieved_at_utc: str
    archived_content_sha256: str
    archived_evidence_relative_path: str
    source_role: str

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "publisher": self.publisher,
            "url": self.url,
            "retrieved_at_utc": self.retrieved_at_utc,
            "archived_content_sha256": self.archived_content_sha256,
            "source_role": self.source_role,
        }


@dataclass(frozen=True, slots=True)
class ChampionsRulesCoverage:
    authoritative_categories: tuple[str, ...]
    intentionally_not_asserted: tuple[str, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "authoritative_categories": list(self.authoritative_categories),
            "intentionally_not_asserted": list(self.intentionally_not_asserted),
        }


@dataclass(frozen=True, slots=True)
class ChampionsRulesSnapshot:
    """One fully validated, immutable official rules snapshot."""

    schema_version: str
    ruleset_id: str
    ruleset_version: str
    scope: str
    snapshot_id: str
    effective_period: dict[str, Any]
    sources: tuple[ChampionsRulesSource, ...]
    facts: ChampionsRulesFacts
    coverage: ChampionsRulesCoverage
    facts_content_sha256: str


@dataclass(frozen=True, slots=True)
class RulesPin:
    """The exact rules identity pinned to one match, persisted verbatim.

    Deliberately holds only identity/hash values -- never the facts
    themselves -- so a match row can never drift into carrying its own
    private copy of rules content that might diverge from the checked-in
    artifact.
    """

    ruleset_id: str
    ruleset_version: str
    rules_snapshot_id: str
    rules_facts_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            self.ruleset_id,
            self.ruleset_version,
            self.rules_snapshot_id,
            self.rules_facts_sha256,
        ):
            if not field_name or not field_name.strip():
                raise ChampionsRulesPinError("RULES_PIN_FIELD_REQUIRED")


def _require_str(payload: dict[str, Any], key: str, *, container: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:{container}.{key}")
    return value


def _require_int(payload: dict[str, Any], key: str, *, container: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:{container}.{key}")
    return value


def _require_bool(payload: dict[str, Any], key: str, *, container: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:{container}.{key}")
    return value


def _require_dict(payload: dict[str, Any], key: str, *, container: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:{container}.{key}")
    return value


def _parse_facts(raw: dict[str, Any]) -> ChampionsRulesFacts:
    extra = set(raw.keys()) - _ALLOWED_TOP_LEVEL_FACT_KEYS
    if extra:
        raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_UNSUPPORTED_FACT_KEYS:{sorted(extra)}")

    battle_format = _require_str(raw, "battle_format", container="facts")
    if battle_format not in _ALLOWED_BATTLE_FORMATS:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_INVALID:facts.battle_format")

    selected_to_battle = _require_int(raw, "pokemon_selected_to_battle", container="facts")
    if selected_to_battle <= 0:
        raise ChampionsRulesIntegrityError(
            "RULES_SNAPSHOT_INVALID:facts.pokemon_selected_to_battle"
        )

    mega_raw = _require_dict(raw, "mega_evolution", container="facts")
    mega_extra = set(mega_raw.keys()) - _ALLOWED_MEGA_EVOLUTION_KEYS
    if mega_extra:
        raise ChampionsRulesIntegrityError(
            f"RULES_SNAPSHOT_UNSUPPORTED_FACT_KEYS:mega_evolution:{sorted(mega_extra)}"
        )
    mega_allowed = _require_bool(mega_raw, "allowed", container="facts.mega_evolution")
    mega_max_uses = _require_int(
        mega_raw, "max_uses_per_battle", container="facts.mega_evolution"
    )
    if mega_max_uses < 0:
        raise ChampionsRulesIntegrityError(
            "RULES_SNAPSHOT_INVALID:facts.mega_evolution.max_uses_per_battle"
        )
    mega_requires_stone = _require_bool(
        mega_raw,
        "requires_mega_stone_for_eligible_pokemon",
        container="facts.mega_evolution",
    )

    duplicate_held_items_allowed = _require_bool(
        raw, "duplicate_held_items_allowed", container="facts"
    )

    timers_raw = _require_dict(raw, "timers", container="facts")
    timers_extra = set(timers_raw.keys()) - _ALLOWED_TIMER_KEYS
    if timers_extra:
        raise ChampionsRulesIntegrityError(
            f"RULES_SNAPSHOT_UNSUPPORTED_FACT_KEYS:timers:{sorted(timers_extra)}"
        )
    timer_values: dict[str, int] = {}
    for timer_key in _ALLOWED_TIMER_KEYS:
        value = _require_int(timers_raw, timer_key, container="facts.timers")
        if value <= 0:
            raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:facts.timers.{timer_key}")
        timer_values[timer_key] = value

    return ChampionsRulesFacts(
        battle_format=battle_format,
        pokemon_selected_to_battle=selected_to_battle,
        mega_evolution=MegaEvolutionRule(
            allowed=mega_allowed,
            max_uses_per_battle=mega_max_uses,
            requires_mega_stone_for_eligible_pokemon=mega_requires_stone,
        ),
        duplicate_held_items_allowed=duplicate_held_items_allowed,
        timers=TimersRule(
            total_time_seconds=timer_values["total_time_seconds"],
            player_time_seconds=timer_values["player_time_seconds"],
            turn_selection_seconds=timer_values["turn_selection_seconds"],
            pokemon_selection_seconds=timer_values["pokemon_selection_seconds"],
        ),
    )


def _parse_sources(
    raw: list[Any], *, evidence_root: Path | None
) -> tuple[ChampionsRulesSource, ...]:
    if not isinstance(raw, list) or not raw:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_SOURCES_REQUIRED")
    sources: list[ChampionsRulesSource] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:sources[{index}]")
        missing = _REQUIRED_SOURCE_KEYS - entry.keys()
        if missing:
            raise ChampionsRulesIntegrityError(
                f"RULES_SNAPSHOT_SOURCE_MISSING_KEYS:sources[{index}]:{sorted(missing)}"
            )
        container = f"sources[{index}]"
        publisher = _require_str(entry, "publisher", container=container)
        url = _require_str(entry, "url", container=container)
        if not url.startswith("https://"):
            raise ChampionsRulesIntegrityError(f"RULES_SNAPSHOT_INVALID:{container}.url")
        retrieved_at_utc = _require_str(entry, "retrieved_at_utc", container=container)
        archived_content_sha256 = _require_str(
            entry, "archived_content_sha256", container=container
        )
        if len(archived_content_sha256) != 64:
            raise ChampionsRulesIntegrityError(
                f"RULES_SNAPSHOT_INVALID:{container}.archived_content_sha256"
            )
        relative_path = _require_str(
            entry, "archived_evidence_relative_path", container=container
        )
        source_role = _require_str(entry, "source_role", container=container)

        if evidence_root is not None:
            evidence_path = (evidence_root / relative_path).resolve()
            if not evidence_path.is_relative_to(evidence_root.resolve()):
                raise ChampionsRulesIntegrityError(
                    f"RULES_SNAPSHOT_EVIDENCE_PATH_ESCAPE:{container}"
                )
            try:
                evidence_bytes = evidence_path.read_bytes()
            except OSError as exc:
                raise ChampionsRulesIntegrityError(
                    f"RULES_SNAPSHOT_EVIDENCE_MISSING:{container}"
                ) from exc
            if _sha256_hex(evidence_bytes) != archived_content_sha256:
                raise ChampionsRulesIntegrityError(
                    f"RULES_SNAPSHOT_EVIDENCE_HASH_MISMATCH:{container}"
                )

        sources.append(
            ChampionsRulesSource(
                publisher=publisher,
                url=url,
                retrieved_at_utc=retrieved_at_utc,
                archived_content_sha256=archived_content_sha256,
                archived_evidence_relative_path=relative_path,
                source_role=source_role,
            )
        )
    return tuple(sources)


def _parse_coverage(raw: dict[str, Any]) -> ChampionsRulesCoverage:
    authoritative = raw.get("authoritative_categories")
    not_asserted = raw.get("intentionally_not_asserted")
    if not isinstance(authoritative, list) or not all(
        isinstance(item, str) for item in authoritative
    ):
        raise ChampionsRulesIntegrityError(
            "RULES_SNAPSHOT_INVALID:coverage.authoritative_categories"
        )
    if not isinstance(not_asserted, list) or not all(
        isinstance(item, str) for item in not_asserted
    ):
        raise ChampionsRulesIntegrityError(
            "RULES_SNAPSHOT_INVALID:coverage.intentionally_not_asserted"
        )
    authoritative_set = set(authoritative)
    not_asserted_set = set(not_asserted)
    if authoritative_set != _MANDATORY_FACT_CATEGORIES:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_AUTHORITATIVE_CATEGORIES_MISMATCH")
    if authoritative_set & not_asserted_set:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_COVERAGE_CATEGORY_OVERLAP")
    # Preserve on-disk list order (not re-sorted) -- the snapshot_id/
    # facts_content_sha256 recomputation below must reproduce the exact
    # bytes that were hashed when the artifact was constructed, and
    # ``json.dumps(..., sort_keys=True)`` only sorts dict keys, never list
    # elements.
    return ChampionsRulesCoverage(
        authoritative_categories=tuple(authoritative),
        intentionally_not_asserted=tuple(not_asserted),
    )


def compute_facts_content_sha256(facts: ChampionsRulesFacts) -> str:
    """Deterministic SHA-256 over the canonical facts dict only."""

    return _sha256_hex(_canonical_bytes(facts.to_canonical_dict()))


def _compute_snapshot_id(
    *,
    schema_version: str,
    ruleset_id: str,
    ruleset_version: str,
    scope: str,
    effective_period: dict[str, Any],
    facts_content_sha256: str,
    coverage: ChampionsRulesCoverage,
) -> str:
    identity = {
        "schema_version": schema_version,
        "ruleset_id": ruleset_id,
        "ruleset_version": ruleset_version,
        "scope": scope,
        "effective_period": effective_period,
        "facts_content_sha256": facts_content_sha256,
        "coverage": coverage.to_canonical_dict(),
    }
    return _sha256_hex(_canonical_bytes(identity))


def parse_and_validate_snapshot(
    data: dict[str, Any], *, evidence_root: Path | None
) -> ChampionsRulesSnapshot:
    """Strictly parse and validate one rules snapshot dict. Fails closed.

    ``evidence_root``, when given, is the directory that
    ``archived_evidence_relative_path`` entries are resolved against; each
    referenced evidence file is read and its SHA-256 is compared to the
    recorded ``archived_content_sha256`` -- a missing file, an escaping
    path, or a hash mismatch all fail closed. Pass ``None`` only when
    evidence integrity has already been proven by a prior call (e.g. in a
    test that only wants to exercise fact-content tampering).
    """

    if not isinstance(data, dict):
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_ROOT_MUST_BE_OBJECT")

    schema_version = _require_str(data, "schema_version", container="root")
    if schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_UNSUPPORTED_SCHEMA_VERSION")

    ruleset_id = _require_str(data, "ruleset_id", container="root")
    ruleset_version = _require_str(data, "ruleset_version", container="root")
    if (ruleset_id, ruleset_version) != (SUPPORTED_RULESET_ID, SUPPORTED_RULESET_VERSION):
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_UNRECOGNIZED_RULESET")

    scope = _require_str(data, "scope", container="root")
    if scope != SUPPORTED_SCOPE:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_UNRECOGNIZED_SCOPE")

    snapshot_id = _require_str(data, "snapshot_id", container="root")
    effective_period = _require_dict(data, "effective_period", container="root")

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list):
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_INVALID:sources")
    sources = _parse_sources(raw_sources, evidence_root=evidence_root)

    raw_facts = _require_dict(data, "facts", container="root")
    facts = _parse_facts(raw_facts)

    raw_coverage = _require_dict(data, "coverage", container="root")
    coverage = _parse_coverage(raw_coverage)

    facts_content_sha256 = _require_str(data, "facts_content_sha256", container="root")
    if len(facts_content_sha256) != 64:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_INVALID:facts_content_sha256")
    if compute_facts_content_sha256(facts) != facts_content_sha256:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_FACTS_HASH_MISMATCH")

    expected_snapshot_id = _compute_snapshot_id(
        schema_version=schema_version,
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
        scope=scope,
        effective_period=effective_period,
        facts_content_sha256=facts_content_sha256,
        coverage=coverage,
    )
    if snapshot_id != expected_snapshot_id:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_ID_MISMATCH")

    return ChampionsRulesSnapshot(
        schema_version=schema_version,
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
        scope=scope,
        snapshot_id=snapshot_id,
        effective_period=effective_period,
        sources=sources,
        facts=facts,
        coverage=coverage,
        facts_content_sha256=facts_content_sha256,
    )


def load_raw_bundled_snapshot_dict() -> dict[str, Any]:
    """Read the checked-in snapshot JSON as a plain dict, unvalidated.

    Test-only convenience: exists so tampering/corruption tests can start
    from a real, freshly re-read copy of the actual bundled artifact (then
    mutate a field and feed it to :func:`parse_and_validate_snapshot`)
    instead of hand-rolling a parallel snapshot shape that could quietly
    drift from the real one. Production code never calls this -- it always
    goes through :func:`load_bundled_snapshot`, which validates.
    """

    data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_ROOT_MUST_BE_OBJECT")
    return data


@lru_cache(maxsize=1)
def load_bundled_snapshot() -> ChampionsRulesSnapshot:
    """Load and validate the one checked-in M-B Single snapshot. No network I/O.

    Cached for the process lifetime: the artifact is immutable checked-in
    repository content, never rewritten at runtime, so re-parsing it on
    every call would be pure overhead. Tests that need to exercise
    tampering/corruption call :func:`parse_and_validate_snapshot` directly
    on an in-memory (optionally mutated) copy instead of mutating this
    cache's on-disk source.
    """

    try:
        raw_text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_FILE_UNREADABLE") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ChampionsRulesIntegrityError("RULES_SNAPSHOT_FILE_INVALID_JSON") from exc
    return parse_and_validate_snapshot(data, evidence_root=_RULESET_ROOT)


def snapshot_to_rules_pin(snapshot: ChampionsRulesSnapshot) -> RulesPin:
    """The identity/hash values a NEW match pins, derived from a validated snapshot."""

    return RulesPin(
        ruleset_id=snapshot.ruleset_id,
        ruleset_version=snapshot.ruleset_version,
        rules_snapshot_id=snapshot.snapshot_id,
        rules_facts_sha256=snapshot.facts_content_sha256,
    )


def current_rules_pin_for_new_match() -> RulesPin:
    """Resolve the rules pin a brand-new match should be created with.

    Loads and validates the one bundled snapshot; a malformed or tampered
    snapshot fails closed here, before any session row is ever inserted, so
    a match can never be created carrying an unverifiable rules pin.
    """

    return snapshot_to_rules_pin(load_bundled_snapshot())


def verify_pin_against_snapshot(pin: RulesPin, snapshot: ChampionsRulesSnapshot) -> None:
    """Fail closed unless ``snapshot`` is exactly what ``pin`` was pinned to.

    This is what keeps an existing match's rules from silently changing: a
    newer/different snapshot appearing on disk (different ruleset/version,
    different ``snapshot_id``, or different ``facts_content_sha256``) never
    substitutes for what the match actually pinned -- it raises instead.
    """

    if (snapshot.ruleset_id, snapshot.ruleset_version) != (pin.ruleset_id, pin.ruleset_version):
        raise ChampionsRulesPinError("PINNED_RULESET_NOT_AVAILABLE")
    if snapshot.snapshot_id != pin.rules_snapshot_id:
        raise ChampionsRulesPinError("PINNED_SNAPSHOT_ID_MISMATCH")
    if snapshot.facts_content_sha256 != pin.rules_facts_sha256:
        raise ChampionsRulesPinError("PINNED_FACTS_HASH_MISMATCH")


def build_rules_context(snapshot: ChampionsRulesSnapshot) -> dict[str, Any]:
    """Compact, immutable ``rules_context`` dict for a provider-ready request.

    Deliberately excludes raw HTML/source documents and local filesystem
    paths -- only compact facts plus enough source provenance (publisher,
    URL, retrieval time, archived-evidence hash, role) for later audit.
    Every value here is a JSON primitive so it participates in canonical
    serialization exactly like the rest of the rich request.
    """

    return {
        "context_schema_version": RULES_CONTEXT_SCHEMA_VERSION,
        "ruleset_id": snapshot.ruleset_id,
        "ruleset_version": snapshot.ruleset_version,
        "snapshot_id": snapshot.snapshot_id,
        "facts_content_sha256": snapshot.facts_content_sha256,
        "scope": snapshot.scope,
        "facts": snapshot.facts.to_canonical_dict(),
        "sources": [source.to_canonical_dict() for source in snapshot.sources],
    }


def resolve_pinned_rules_context(pin: RulesPin) -> dict[str, Any]:
    """Resolve one match's persisted pin to its full ``rules_context`` dict.

    The single shared resolver used by both initial rich request creation
    and offline rebuild -- given the same pin and the same (immutable,
    unchanged) bundled snapshot, this always returns byte-identical
    ``rules_context`` content. Fails closed
    (:class:`ChampionsRulesPinError`/:class:`ChampionsRulesIntegrityError`)
    on any mismatch or corruption rather than ever silently substituting a
    different ruleset/version/snapshot.
    """

    snapshot = load_bundled_snapshot()
    verify_pin_against_snapshot(pin, snapshot)
    return build_rules_context(snapshot)
