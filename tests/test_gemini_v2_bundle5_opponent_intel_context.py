"""Gemini V2 Bundle 5: opponent population INTEL as a non-authoritative prior.

Closes the audit gap where a rich Turn Advice request carried no population
context at all for the confirmed opponent active -- while making it
impossible for that population data to be mistaken for a confirmed fact.
The authority ordering this whole file exists to prove is:

    CONFIRMED MATCH FACTS
        > CANONICAL RULES / LEGAL POSSIBILITIES
            > POPULATION INTEL
                > UNKNOWN

Bundle 5 is **input context only**: the request contract advances
``maple-turn-advice.v5`` -> ``.v6`` by adding exactly one top-level field,
``opponent_intel_context``. Nothing about the response/output contract,
legal actions, confirmed state, battle memory, ``rules_context``, selection
logic, or OCR changes.

Hermetic by construction. Every test here uses fixture bytes from
``tests/fixtures/bundle5.py`` inside a temporary directory: the operator's
real provisioned artifact
(``%LOCALAPPDATA%\\MapleNext\\Battle1\\opponent_intel_db``) is never read,
never written, and -- via the autouse ``isolated_runtime_root`` fixture in
``tests/conftest.py`` -- not even resolvable from a test run. No network
call is made and no provider is contacted anywhere in this file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import BattleApplication, DomainError
from maple_next.application.turn_provider_export_bridge import load_opponent_intel_context
from maple_next.domain.champions_rules import load_bundled_snapshot
from maple_next.domain.enums import BattleState, JobStatus, MatchOutcome
from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.domain.models import BattleSession
from maple_next.domain.opponent_intel import LocalJsonOpponentMetaProvider
from maple_next.domain.opponent_intel_context import (
    COMPATIBILITY_MATCHED,
    COMPATIBILITY_MISMATCHED,
    CONTEXT_STATUS_AVAILABLE,
    CONTEXT_STATUS_MISMATCHED,
    CONTEXT_STATUS_UNAVAILABLE,
    OPPONENT_INTEL_AUTHORITY,
    OPPONENT_INTEL_CONTEXT_SCHEMA_VERSION,
    REASON_FORMAT_MISMATCH,
    REASON_OPPONENT_ACTIVE_NOT_CONFIRMED,
    REASON_PIN_UNAVAILABLE,
    REASON_SEASON_MISMATCH,
    REASON_SPECIES_NOT_IN_SNAPSHOT,
    TOP_N_PER_CATEGORY,
    OpponentIntelContextError,
    OpponentIntelPin,
    build_opponent_intel_context,
    validate_opponent_intel_context,
)
from maple_next.domain.turn_state import Known, ProvenanceStep
from maple_next.opponent_intel_db.generation_store import (
    DEFAULT_CATALOG_FILENAME,
    DEFAULT_SNAPSHOT_FILENAME,
    MANIFEST_FILENAME,
    POINTER_FILENAME,
    GenerationStoreError,
    commit_generation,
    generation_directory,
    read_generation,
)
from maple_next.opponent_intel_db.runtime_intel import (
    composite_pair_digest,
    load_pinned_generation,
    materialized_generation_id,
    resolve_pinnable_generation,
)
from maple_next.persistence.schema import SCHEMA_VERSION
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers import turn_request as legacy_turn_request
from maple_next.providers.turn_advice_rich_state import (
    RICH_STATE_REQUEST_CONTRACT_VERSION,
    RichStateRequestError,
    build_rich_provider_prompt,
    canonical_rich_request_dict,
    encode_canonical_rich_request,
    rich_request_payload_hash,
)
from tests.fixtures.bundle5 import (
    AMOONGUSS_DISPLAY,
    AMOONGUSS_ID,
    CONFIRMED_MEMORY_MOVE,
    FIXTURE_FETCHED_AT,
    FIXTURE_FORMAT,
    FIXTURE_SEASON,
    FIXTURE_SOURCE,
    GARCHOMP_ID,
    NULL_PERCENTAGE_MOVE,
    PRIOR_ONLY_MOVE,
    TRUNCATED_MOVE,
    fixture_pair_bytes,
    fixture_snapshot_dict,
    provision_fixture_generation,
    write_flat_pair,
)
from tests.test_gemini_v2_bundle3_confirmed_memory_build_context import (
    CURRENT_TURN_NUMBER,
    OPPONENT_ACTIVE_BY_TURN,
    Bundle3Fixture,
)

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)

#: The confirmed opponent active on the current Turn of the historical
#: fixture match (マスカーニャ / ハッサム / ガブリアス brought; opponent
#: switched to Amoonguss on Turn 4 and stayed).
CURRENT_OPPONENT = OPPONENT_ACTIVE_BY_TURN[CURRENT_TURN_NUMBER]
EARLIER_OPPONENT = OPPONENT_ACTIVE_BY_TURN[1]


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _available_fixture(tmp_path: Path, **kwargs: Any) -> Bundle3Fixture:
    """The historical match with a pinned generation that *contains* Amoonguss."""

    return Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict(), **kwargs)


def _species_missing_fixture(tmp_path: Path, **kwargs: Any) -> Bundle3Fixture:
    """The same match pinned to a generation that has no Amoonguss entry."""

    return Bundle3Fixture(
        tmp_path,
        intel_snapshot=fixture_snapshot_dict(species_ids=(GARCHOMP_ID,)),
        **kwargs,
    )


# =========================================================================
# A. IMMUTABLE MATERIALIZATION OF THE ACCEPTED FLAT PAIR
# =========================================================================


def test_flat_pair_is_materialized_byte_identically(tmp_path: Path) -> None:
    """Archiving copies the exact accepted bytes -- no re-derivation at all."""

    intel = tmp_path / "intel"
    snapshot_bytes, catalog_bytes = write_flat_pair(intel, fixture_snapshot_dict())
    pinnable = resolve_pinnable_generation(intel)
    assert pinnable is not None and pinnable.materialized

    directory = generation_directory(intel, pinnable.generation_id)
    assert (directory / DEFAULT_SNAPSHOT_FILENAME).read_bytes() == snapshot_bytes
    assert (directory / DEFAULT_CATALOG_FILENAME).read_bytes() == catalog_bytes
    assert pinnable.snapshot_sha256 == _sha256(snapshot_bytes)


def test_materialized_generation_id_reuses_the_existing_composite_convention(
    tmp_path: Path,
) -> None:
    """The id is content-derived and deterministic, never random.

    It is the *same* composite digest the pre-existing legacy runtime
    identity uses (``legacy:<digest>``); only the prefix differs, because a
    generation id is also a directory name and ``:`` is not a legal Windows
    path character.
    """

    intel = tmp_path / "intel"
    snapshot_bytes, catalog_bytes = write_flat_pair(intel, fixture_snapshot_dict())
    digest = composite_pair_digest(snapshot_bytes, catalog_bytes)

    expected = materialized_generation_id(snapshot_bytes, catalog_bytes)
    assert expected == f"materialized-{digest}"
    assert resolve_pinnable_generation(intel).generation_id == expected  # type: ignore[union-attr]


def test_materialization_is_idempotent_and_leaves_the_flat_pair_untouched(
    tmp_path: Path,
) -> None:
    intel = tmp_path / "intel"
    snapshot_bytes, catalog_bytes = write_flat_pair(intel, fixture_snapshot_dict())

    first = resolve_pinnable_generation(intel)
    second = resolve_pinnable_generation(intel)
    assert first is not None and second is not None
    assert first.generation_id == second.generation_id
    assert first.snapshot_sha256 == second.snapshot_sha256

    # The provisioned runtime files themselves are never rewritten...
    assert (intel / DEFAULT_SNAPSHOT_FILENAME).read_bytes() == snapshot_bytes
    assert (intel / DEFAULT_CATALOG_FILENAME).read_bytes() == catalog_bytes
    # ...and materializing never publishes a "current generation" pointer.
    assert not (intel / POINTER_FILENAME).exists()


def test_generation_is_addressable_by_id_independently_of_the_pointer(
    tmp_path: Path,
) -> None:
    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    pinnable = resolve_pinnable_generation(intel)
    assert pinnable is not None

    archived = read_generation(intel, pinnable.generation_id)
    assert archived.pointer.generation_id == pinnable.generation_id
    assert archived.pointer.source == FIXTURE_SOURCE
    bundle = load_pinned_generation(intel, pinnable.generation_id)
    assert bundle.snapshot_document.season == FIXTURE_SEASON
    assert AMOONGUSS_ID in bundle.snapshot_document.species


def test_a_newer_current_generation_does_not_disturb_an_archived_one(
    tmp_path: Path,
) -> None:
    """Publishing a new current generation leaves an existing pin resolvable."""

    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    pinned = resolve_pinnable_generation(intel)
    assert pinned is not None

    newer_snapshot, newer_catalog = fixture_pair_bytes(
        fixture_snapshot_dict(species_ids=(GARCHOMP_ID,))
    )
    commit_generation(
        intel,
        snapshot_bytes=newer_snapshot,
        catalog_bytes=newer_catalog,
        snapshot_schema_version="opponent-intel-snapshot.v1",
        catalog_schema_version="opponent-intel-move-catalog.v1",
        source=FIXTURE_SOURCE,
        created_at="2026-08-12T00:00:00+00:00",
    )
    assert (intel / POINTER_FILENAME).exists()

    still = load_pinned_generation(intel, pinned.generation_id)
    assert AMOONGUSS_ID in still.snapshot_document.species
    assert still.snapshot_sha256 == pinned.snapshot_sha256


def test_missing_pinned_generation_fails_closed(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    intel.mkdir()
    with pytest.raises(GenerationStoreError, match="GENERATION_NOT_FOUND"):
        load_pinned_generation(intel, "materialized-" + "0" * 64)


def test_corrupted_archived_generation_fails_closed(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    pinnable = resolve_pinnable_generation(intel)
    assert pinnable is not None

    archived_snapshot = (
        generation_directory(intel, pinnable.generation_id) / DEFAULT_SNAPSHOT_FILENAME
    )
    archived_snapshot.write_bytes(archived_snapshot.read_bytes() + b" ")
    with pytest.raises(GenerationStoreError, match="SNAPSHOT_HASH_MISMATCH"):
        load_pinned_generation(intel, pinnable.generation_id)


def test_manifest_naming_a_different_generation_fails_closed(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    pinnable = resolve_pinnable_generation(intel)
    assert pinnable is not None

    manifest_path = generation_directory(intel, pinnable.generation_id) / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generation_id"] = "materialized-" + "1" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(GenerationStoreError, match="GENERATION_MANIFEST_ID_MISMATCH"):
        load_pinned_generation(intel, pinnable.generation_id)


def test_incomplete_flat_pair_is_never_archived(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    (intel / DEFAULT_CATALOG_FILENAME).unlink()
    with pytest.raises(GenerationStoreError, match="LEGACY_PAIR_INCOMPLETE"):
        resolve_pinnable_generation(intel)


# =========================================================================
# B. DURABLE PER-MATCH PIN
# =========================================================================


def test_schema_version_advanced_for_the_additive_intel_pin() -> None:
    # Gemini V2 Bundle 6 additively raised this further, 20 -> 21, for the
    # versioned Turn Advice response contract (turn_advices.response_schema_
    # version / advice_json) -- the Bundle 5 opponent-INTEL pin columns
    # this test's name refers to are untouched.
    assert SCHEMA_VERSION == 21


def test_new_match_pins_the_accepted_generation(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    write_flat_pair(intel, fixture_snapshot_dict())
    expected = resolve_pinnable_generation(intel)
    assert expected is not None

    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository, opponent_intel_directory=intel)
    try:
        session = application.new_match()
        assert session.opponent_intel_pin_status == "PINNED"
        assert session.opponent_intel_generation_id == expected.generation_id
        assert session.opponent_intel_snapshot_sha256 == expected.snapshot_sha256
    finally:
        repository.close()


def test_new_match_after_export_pins_the_accepted_generation(
    tmp_path: Path, isolated_runtime_root: Path
) -> None:
    from tests.test_match_lifecycle import build_ready_application

    intel = isolated_runtime_root / "opponent_intel_db"
    write_flat_pair(intel, fixture_snapshot_dict())
    expected = resolve_pinnable_generation(intel)
    assert expected is not None

    repository, application, _, _ = build_ready_application(tmp_path)
    try:
        first = repository.load_active_session()
        assert first is not None
        assert first.opponent_intel_generation_id == expected.generation_id

        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        application.export_match()
        second = application.new_match_after_export()

        assert second.session_id != first.session_id
        assert second.opponent_intel_pin_status == "PINNED"
        assert second.opponent_intel_generation_id == expected.generation_id
        assert second.opponent_intel_snapshot_sha256 == expected.snapshot_sha256
    finally:
        repository.close()


def test_no_provisioned_snapshot_pins_unavailable(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(
        repository, opponent_intel_directory=tmp_path / "absent-intel"
    )
    try:
        session = application.new_match()
        assert session.opponent_intel_pin_status == "UNAVAILABLE"
        assert session.opponent_intel_generation_id is None
        assert session.opponent_intel_snapshot_sha256 is None
    finally:
        repository.close()


def test_unusable_artifact_pins_unavailable_instead_of_blocking_match_creation(
    tmp_path: Path,
) -> None:
    """Advisory context must never make starting a match fail."""

    intel = tmp_path / "intel"
    intel.mkdir()
    (intel / DEFAULT_SNAPSHOT_FILENAME).write_bytes(b"{ not json")
    (intel / DEFAULT_CATALOG_FILENAME).write_bytes(b"{ not json")

    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository, opponent_intel_directory=intel)
    try:
        session = application.new_match()
        assert session.opponent_intel_pin_status == "UNAVAILABLE"
    finally:
        repository.close()


def test_migrated_null_pin_resolves_unavailable_and_never_adopts_current(
    tmp_path: Path,
) -> None:
    """A pre-Bundle-5 match must not retroactively inherit today's snapshot."""

    intel = tmp_path / "intel"
    provision_fixture_generation(intel)

    fixture = _available_fixture(tmp_path)
    try:
        legacy_session = BattleSession(
            session_id="legacy",
            match_id="legacy-match",
            generation=1,
            state=BattleState.TURN_REVIEWED,
            battle_revision=1,
            rules_ruleset_id=fixture.repository.load_active_session().rules_ruleset_id,  # type: ignore[union-attr]
            rules_ruleset_version=(
                fixture.repository.load_active_session().rules_ruleset_version  # type: ignore[union-attr]
            ),
            rules_snapshot_id=fixture.repository.load_active_session().rules_snapshot_id,  # type: ignore[union-attr]
            rules_facts_sha256=(
                fixture.repository.load_active_session().rules_facts_sha256  # type: ignore[union-attr]
            ),
        )
        assert legacy_session.opponent_intel_pin_status is None
        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        context = load_opponent_intel_context(
            legacy_session, confirmed_state=state, intel_directory=intel
        )
        assert context["status"] == CONTEXT_STATUS_UNAVAILABLE
        assert context["reason"] == REASON_PIN_UNAVAILABLE
        assert context["population"] is None
        assert context["snapshot"] is None
    finally:
        fixture.close()


def test_explicitly_unavailable_pin_is_deterministic_and_provider_ready(
    tmp_path: Path,
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        session.opponent_intel_pin_status = "UNAVAILABLE"
        session.opponent_intel_generation_id = None
        session.opponent_intel_snapshot_sha256 = None
        first = load_opponent_intel_context(
            session, confirmed_state=state, intel_directory=fixture.intel_directory
        )
        second = load_opponent_intel_context(
            session, confirmed_state=state, intel_directory=fixture.intel_directory
        )
        assert first == second
        assert first["status"] == CONTEXT_STATUS_UNAVAILABLE
        assert first["population"] is None
    finally:
        fixture.close()


def test_save_session_never_repins_intel(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        pinned_generation = session.opponent_intel_generation_id

        # Even an in-memory attempt to rewrite the pin must not reach disk.
        session.opponent_intel_generation_id = "materialized-" + "9" * 64
        session.opponent_intel_pin_status = "UNAVAILABLE"
        session.bump_battle()
        fixture.repository.save_session(session)
        fixture.repository.connection.commit()

        reloaded = fixture.repository.load_active_session()
        assert reloaded is not None
        assert reloaded.opponent_intel_generation_id == pinned_generation
        assert reloaded.opponent_intel_pin_status == "PINNED"
    finally:
        fixture.close()


def test_existing_match_ignores_a_newer_current_generation(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        before = fixture.opponent_intel_context()
        newer_snapshot, newer_catalog = fixture_pair_bytes(
            fixture_snapshot_dict(species_ids=(GARCHOMP_ID,))
        )
        commit_generation(
            fixture.intel_directory,
            snapshot_bytes=newer_snapshot,
            catalog_bytes=newer_catalog,
            snapshot_schema_version="opponent-intel-snapshot.v1",
            catalog_schema_version="opponent-intel-move-catalog.v1",
            source=FIXTURE_SOURCE,
            created_at="2026-08-12T00:00:00+00:00",
        )
        after = fixture.opponent_intel_context()
        assert after == before
        assert after["status"] == CONTEXT_STATUS_AVAILABLE
    finally:
        fixture.close()


def test_restart_resolves_the_exact_pinned_generation(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        before = fixture.opponent_intel_context()
        db_path = fixture.db_path
        intel_directory = fixture.intel_directory
        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
    finally:
        fixture.close()

    restarted = SQLiteRepository(db_path)
    try:
        session = restarted.load_active_session()
        assert session is not None
        after = load_opponent_intel_context(
            session, confirmed_state=state, intel_directory=intel_directory
        )
        assert after == before
        assert _canonical_bytes(after) == _canonical_bytes(before)
    finally:
        restarted.close()


def test_pinned_generation_missing_blocks_the_request(tmp_path: Path) -> None:
    import shutil

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        shutil.rmtree(
            generation_directory(
                fixture.intel_directory, session.opponent_intel_generation_id or ""
            )
        )
        with pytest.raises(DomainError, match="OPPONENT_INTEL_CONTEXT_INVALID"):
            fixture.application.request_rich_turn_advice("command-missing-generation")
    finally:
        fixture.close()


def test_pinned_snapshot_hash_mismatch_blocks_the_request(tmp_path: Path) -> None:
    """A pin whose recorded hash no longer matches the archive fails closed."""

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        fixture.repository.connection.execute(
            "UPDATE battle_sessions SET opponent_intel_snapshot_sha256 = ? "
            "WHERE session_id = ?",
            ("0" * 64, session.session_id),
        )
        fixture.repository.connection.commit()
        with pytest.raises(DomainError, match="OPPONENT_INTEL_CONTEXT_INVALID"):
            fixture.application.request_rich_turn_advice("command-hash-mismatch")
    finally:
        fixture.close()


def test_resolved_generation_differing_from_the_pin_fails_closed() -> None:
    pin = OpponentIntelPin.pinned(generation_id="materialized-aaa", snapshot_sha256="a" * 64)
    document_pair = fixture_snapshot_dict()
    from maple_next.opponent_intel_db.snapshot_store import SnapshotDocument

    document = SnapshotDocument.from_json_dict(document_pair)
    with pytest.raises(OpponentIntelContextError, match="RESOLVED_GENERATION_DIFFERS_FROM_PIN"):
        build_opponent_intel_context(
            confirmed_active_species=CURRENT_OPPONENT,
            pin=pin,
            document=document,
            generation_id="materialized-bbb",
            snapshot_sha256="a" * 64,
            rules_season_id=FIXTURE_SEASON,
            rules_battle_format="SINGLE",
        )


# =========================================================================
# C. CONFIRMED ACTIVE ONLY
# =========================================================================


def test_confirmed_opponent_active_has_available_population_intel(
    tmp_path: Path,
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_AVAILABLE
        assert context["authority"] == OPPONENT_INTEL_AUTHORITY
        assert context["context_schema_version"] == OPPONENT_INTEL_CONTEXT_SCHEMA_VERSION
        assert context["confirmed_active_species"] == CURRENT_OPPONENT
        assert context["resolved_species"] == {
            "species_id": AMOONGUSS_ID,
            "display_name": AMOONGUSS_DISPLAY,
        }
        assert context["snapshot"]["source"] == FIXTURE_SOURCE
        assert context["snapshot"]["fetched_at"] == FIXTURE_FETCHED_AT
        assert context["snapshot"]["source_updated_at"] is None
    finally:
        fixture.close()


def test_unconfirmed_opponent_active_can_never_be_available(tmp_path: Path) -> None:
    """An OCR-only / unreviewed opponent identity yields no population data."""

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        unreviewed = replace(
            state,
            opponent_side=replace(
                state.opponent_side,
                active=Known.unknown(provenance_chain=(ProvenanceStep.OCR_CANDIDATE,)),
            ),
        )
        context = load_opponent_intel_context(
            session, confirmed_state=unreviewed, intel_directory=fixture.intel_directory
        )
        assert context["status"] == CONTEXT_STATUS_UNAVAILABLE
        assert context["reason"] == REASON_OPPONENT_ACTIVE_NOT_CONFIRMED
        assert context["confirmed_active_species"] is None
        assert context["population"] is None
    finally:
        fixture.close()


def test_confirmed_active_change_rebinds_the_context(tmp_path: Path) -> None:
    """A confirmed opponent switch rebinds INTEL to the *new* active."""

    fixture = _available_fixture(tmp_path)
    try:
        earlier = fixture.opponent_intel_context(1)
        current = fixture.opponent_intel_context(CURRENT_TURN_NUMBER)
        assert earlier["confirmed_active_species"] == EARLIER_OPPONENT
        assert current["confirmed_active_species"] == CURRENT_OPPONENT
        assert earlier["resolved_species"] == {
            "species_id": GARCHOMP_ID,
            "display_name": "ガブリアス",
        }
        assert current["resolved_species"]["species_id"] == AMOONGUSS_ID
        assert earlier["population"] != current["population"]
        assert _canonical_bytes(earlier) != _canonical_bytes(current)
    finally:
        fixture.close()


def test_context_species_differing_from_confirmed_active_fails_closed(
    tmp_path: Path,
) -> None:
    """A stale/forged binding must fail before any provider dispatch."""

    fixture = _available_fixture(tmp_path)
    try:
        stale = fixture.opponent_intel_context(1)
        assert stale["confirmed_active_species"] == EARLIER_OPPONENT
        with pytest.raises(RichStateRequestError, match="OPPONENT_INTEL_CONTEXT_SPECIES_MISMATCH"):
            fixture.build_request_with_intel_context(stale)
    finally:
        fixture.close()


def test_species_missing_from_the_pinned_snapshot_stays_provider_ready(
    tmp_path: Path,
) -> None:
    """Amoonguss absent from the pinned snapshot: no data, and no invention."""

    fixture = _species_missing_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_UNAVAILABLE
        assert context["reason"] == REASON_SPECIES_NOT_IN_SNAPSHOT
        assert context["resolved_species"] is None
        assert context["population"] is None
        # Compatibility is still MATCHED -- the snapshot fits the rules, it
        # simply has no entry for this species.
        assert context["compatibility"]["status"] == COMPATIBILITY_MATCHED

        job = fixture.application.request_rich_turn_advice("command-missing-species")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.opponent_intel_context == context
        assert rebuilt.request_hash == job.request_payload_hash
    finally:
        fixture.close()


# =========================================================================
# D. RULES / REGULATION COMPATIBILITY
# =========================================================================


def test_m5_single_matches_the_pinned_m_b_ranked_single_rules(tmp_path: Path) -> None:
    """Compatibility is computed from canonical fields, not a display string."""

    rules = load_bundled_snapshot()
    assert rules.effective_period["season_id"] == FIXTURE_SEASON
    assert rules.facts.battle_format == "SINGLE"
    assert FIXTURE_FORMAT == "single"

    fixture = _available_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["compatibility"] == {"status": COMPATIBILITY_MATCHED, "reason": None}
        assert context["snapshot"]["season"] == FIXTURE_SEASON
        assert context["snapshot"]["format"] == FIXTURE_FORMAT
    finally:
        fixture.close()


def test_different_season_is_mismatched_with_no_population(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict(season="M-4"))
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_MISMATCHED
        assert context["compatibility"] == {
            "status": COMPATIBILITY_MISMATCHED,
            "reason": REASON_SEASON_MISMATCH,
        }
        assert context["population"] is None
        assert context["resolved_species"] is None
        # Provenance is still recorded for audit, and Turn Advice still works.
        assert context["snapshot"]["season"] == "M-4"
        job = fixture.application.request_rich_turn_advice("command-season-mismatch")
        assert job.request_payload_hash
    finally:
        fixture.close()


def test_different_format_is_mismatched_with_no_population(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict(format="double"))
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_MISMATCHED
        assert context["compatibility"]["reason"] == REASON_FORMAT_MISMATCH
        assert context["population"] is None
        job = fixture.application.request_rich_turn_advice("command-format-mismatch")
        assert job.request_payload_hash
    finally:
        fixture.close()


def test_no_arbitrary_freshness_expiry_is_applied(tmp_path: Path) -> None:
    """A correctly-matching dated snapshot stays usable; fetched_at is kept."""

    fixture = _available_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_AVAILABLE
        assert context["snapshot"]["fetched_at"] == FIXTURE_FETCHED_AT
        assert "expires_at" not in context["snapshot"]
    finally:
        fixture.close()


def test_a_matched_claim_inconsistent_with_pinned_rules_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        forged["snapshot"]["format"] = "double"
        assert forged["compatibility"]["status"] == COMPATIBILITY_MATCHED
        with pytest.raises(
            RichStateRequestError, match="OPPONENT_INTEL_COMPATIBILITY_CLAIM_INVALID"
        ):
            fixture.build_request_with_intel_context(forged)
    finally:
        fixture.close()


# =========================================================================
# E. POPULATION DATA RULES
# =========================================================================


def test_top_eight_per_category_in_snapshot_order_without_renormalizing(
    tmp_path: Path,
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        population = fixture.opponent_intel_context()["population"]
        moves = population["moves"]
        assert len(moves) == TOP_N_PER_CATEGORY
        source_moves = fixture_snapshot_dict()["species"][AMOONGUSS_ID]["moves"]
        assert moves == source_moves[:TOP_N_PER_CATEGORY]
        # The 9th entry is dropped, not merged into an "other" bucket, and
        # the surviving percentages are left exactly as published.
        assert TRUNCATED_MOVE not in [entry["name"] for entry in moves]
        assert not any(entry["name"] == "other" for entry in moves)
        assert moves[0]["percentage"] == 92.0
        assert sum(e["percentage"] or 0 for e in moves) != pytest.approx(100.0)
    finally:
        fixture.close()


def test_null_percentages_are_preserved_as_null(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        moves = fixture.opponent_intel_context()["population"]["moves"]
        nulls = [e for e in moves if e["name"] == NULL_PERCENTAGE_MOVE]
        assert len(nulls) == 1
        assert nulls[0]["percentage"] is None
    finally:
        fixture.close()


def test_a_legitimately_empty_category_stays_empty(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        earlier = fixture.opponent_intel_context(1)
        assert earlier["status"] == CONTEXT_STATUS_AVAILABLE
        assert earlier["population"]["natures"] == []
        assert earlier["population"]["moves"]
    finally:
        fixture.close()


def test_every_population_category_is_present_and_bounded(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        population = fixture.opponent_intel_context()["population"]
        assert set(population) == {"moves", "abilities", "items", "natures", "partners"}
        for entries in population.values():
            assert len(entries) <= TOP_N_PER_CATEGORY
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda p: p["moves"].append({"name": "x", "percentage": 1.0}),
            "POPULATION_CATEGORY_TOO_LONG",
        ),
        (
            lambda p: p["moves"].__setitem__(0, {"name": "x", "percentage": 150.0}),
            "POPULATION_ENTRY_PERCENTAGE_OUT_OF_RANGE",
        ),
        (
            lambda p: p["moves"].__setitem__(0, {"name": "", "percentage": 1.0}),
            "POPULATION_ENTRY_NAME_INVALID",
        ),
        (
            lambda p: p["moves"].__setitem__(0, {"name": "x", "percentage": "high"}),
            "POPULATION_ENTRY_PERCENTAGE_INVALID",
        ),
        (lambda p: p.__setitem__("items", {}), "POPULATION_CATEGORY_NOT_LIST"),
        (lambda p: p.pop("natures"), "POPULATION_CATEGORIES_INVALID"),
    ],
)
def test_malformed_available_population_data_fails_closed(
    tmp_path: Path, mutate: Any, match: str
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        mutate(forged["population"])
        with pytest.raises(OpponentIntelContextError, match=match):
            validate_opponent_intel_context(forged)
        with pytest.raises(RichStateRequestError, match="OPPONENT_INTEL_CONTEXT_INVALID"):
            fixture.build_request_with_intel_context(forged)
    finally:
        fixture.close()


def test_population_attached_to_an_unavailable_status_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _species_missing_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        forged["population"] = {
            "moves": [{"name": PRIOR_ONLY_MOVE, "percentage": 78.0}],
            "abilities": [],
            "items": [],
            "natures": [],
            "partners": [],
        }
        with pytest.raises(
            OpponentIntelContextError, match="NON_AVAILABLE_CONTEXT_MUST_NOT_CARRY_POPULATION"
        ):
            validate_opponent_intel_context(forged)
    finally:
        fixture.close()


def test_authority_can_never_be_escalated(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        forged["authority"] = "CONFIRMED_FACT"
        with pytest.raises(OpponentIntelContextError, match="AUTHORITY_INVALID"):
            validate_opponent_intel_context(forged)
    finally:
        fixture.close()


# =========================================================================
# F. REQUEST CONTRACT v6 / CANONICALIZATION
# =========================================================================


def test_request_contract_is_v6() -> None:
    # Gemini V2 Bundle 6 raised this further, .v6 -> .v7 (response schema
    # v2); Bundle 5's single-field addition this test's name refers to
    # (opponent_intel_context) is proven elsewhere in this file and remains
    # untouched.
    assert RICH_STATE_REQUEST_CONTRACT_VERSION == "maple-turn-advice.v7"


def test_v6_preserves_every_v5_field_and_adds_exactly_one(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        canonical = canonical_rich_request_dict(fixture.build_request())
        for field in (
            # Bundle 1/2 ...
            "contract_version",
            "prompt_version",
            "job_type",
            "session_id",
            "match_id",
            "generation",
            "turn_id",
            "turn_number",
            "battle_revision",
            "reviewed_confirmed_state_id",
            "previous_confirmed_state_id",
            "reviewed_snapshot_hash",
            "reviewed_state",
            "selected_three",
            "self_active",
            "legal_actions",
            "legal_switches",
            "legal_switches_status",
            "requested_output_schema",
            "state_confirmation",
            "evidence",
            "self_team_build_sha256",
            # Bundle 3 ...
            "applied_selection_id",
            "reviewed_selection_id",
            "selected_three_builds",
            "battle_memory",
            # Bundle 4 ...
            "rules_context",
            # ... and exactly one Bundle 5 addition.
            "opponent_intel_context",
        ):
            assert field in canonical
        # Gemini V2 Bundle 6 raised the contract further (.v6 -> .v7); every
        # Bundle 5 field this test proves is present is still untouched.
        assert canonical["contract_version"] == "maple-turn-advice.v7"
    finally:
        fixture.close()


def test_intel_context_participates_in_canonical_bytes_and_hash(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        request = fixture.build_request()
        canonical = canonical_rich_request_dict(request)
        assert canonical["opponent_intel_context"] == request.opponent_intel_context
        assert PRIOR_ONLY_MOVE in encode_canonical_rich_request(request).decode("utf-8")
        assert request.request_hash == rich_request_payload_hash(request)
    finally:
        fixture.close()


def test_same_state_and_pin_produce_byte_identical_requests(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        first = fixture.build_request()
        second = fixture.build_request()
        assert encode_canonical_rich_request(first) == encode_canonical_rich_request(second)
        assert first.request_hash == second.request_hash
    finally:
        fixture.close()


def test_a_different_pinned_intel_snapshot_changes_the_hash(tmp_path: Path) -> None:
    available = _available_fixture(tmp_path, db_name="b5-available.db")
    missing = _species_missing_fixture(tmp_path, db_name="b5-missing.db")
    try:
        assert available.opponent_intel_context() != missing.opponent_intel_context()
        rebuilt = replace(
            available.build_request(),
            opponent_intel_context=missing.opponent_intel_context(),
        )
        rebuilt = replace(rebuilt, request_hash=rich_request_payload_hash(rebuilt))
        assert rebuilt.request_hash != available.build_request().request_hash
    finally:
        available.close()
        missing.close()


def test_a_different_confirmed_active_changes_the_hash(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        original = fixture.build_request()
        rebound = replace(original, opponent_intel_context=fixture.opponent_intel_context(1))
        rebound = replace(rebound, request_hash=rich_request_payload_hash(rebound))
        assert rebound.request_hash != original.request_hash
    finally:
        fixture.close()


def test_initial_and_rebuild_produce_the_same_context_and_hash(tmp_path: Path) -> None:
    """One shared loader: authorize once, rebuild offline, identical bytes."""

    fixture = _available_fixture(tmp_path)
    try:
        job = fixture.application.request_rich_turn_advice("command-b5-shared-loader")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.request_hash == job.request_payload_hash
        assert rebuilt.opponent_intel_context == fixture.opponent_intel_context()
        assert rebuilt.opponent_intel_context["status"] == CONTEXT_STATUS_AVAILABLE
    finally:
        fixture.close()


# =========================================================================
# G. NO LEGACY FALLBACK, NO BLENDING
# =========================================================================


def test_legacy_local_json_cache_is_never_used_by_the_gemini_request(
    tmp_path: Path,
) -> None:
    """The legacy opponent-meta cache must not fill a gap in the pinned snapshot."""

    fixture = _species_missing_fixture(tmp_path)
    try:
        legacy_cache_path = tmp_path / "opponent_meta_cache.json"
        legacy_cache_path.write_text(
            json.dumps(
                {
                    "regulation": "M-5",
                    "snapshot_date": "2026-08-01",
                    "source": "legacy-cache",
                    "species": {
                        CURRENT_OPPONENT: {
                            "moves": [{"name": "LEGACY_CACHE_MOVE", "percentage": 99.0}],
                            "abilities": [],
                            "items": [],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # The legacy provider genuinely *does* hold an entry for this species...
        legacy = LocalJsonOpponentMetaProvider(legacy_cache_path)
        assert legacy.get(CURRENT_OPPONENT) is not None

        # ...and the Gemini request still resolves UNAVAILABLE, with no
        # legacy value anywhere in the canonical bytes.
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_UNAVAILABLE
        encoded = encode_canonical_rich_request(fixture.build_request()).decode("utf-8")
        assert "LEGACY_CACHE_MOVE" not in encoded
        assert "legacy-cache" not in encoded
    finally:
        fixture.close()


# =========================================================================
# H. PROMPT AUTHORITY
# =========================================================================


def test_intel_context_is_declared_a_population_prior_only() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert "opponent_intel_context" in prompt
    assert "population-level" in prompt
    assert "statistical prior only" in prompt
    assert "never describes the confirmed actual build of this opponent" in prompt


def test_confirmed_facts_and_battle_memory_override_the_prior() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert "Confirmed current-match facts and battle_memory always override it." in prompt
    assert "Low usage or a missing entry does not mean impossible" in prompt
    assert "rank plausible possibilities" in prompt
    assert (
        "Never state that this opponent has a move, item, ability, nature, or partner"
        in prompt
    )


def test_bundle4_rules_authority_paragraph_is_unchanged_and_separate() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert (
        "When the canonical request includes rules_context, it is authoritative for Pokémon"
        in prompt
    )
    assert "Never fabricate a Champions-specific mechanic" in prompt
    # Two distinct paragraphs, not one merged authority statement.
    assert prompt.index("rules_context") < prompt.index("opponent_intel_context")


def test_rich_prompt_carries_both_authority_paragraphs(tmp_path: Path) -> None:
    """The rich prompt still embeds the full canonical request either way.

    Gemini V2 Bundle 6 gave the rich lane its own independent Initial
    Prompt v2 text (see ``providers/turn_advice_rich_state.py``'s module
    docstring) -- it is deliberately no longer byte-identical to the
    legacy/pre-v7 shared ``_TURN_INITIAL_PROMPT``. What this test actually
    proves -- both Bundle 4's ``rules_context`` and Bundle 5's
    ``opponent_intel_context`` reach the provider inside the canonical
    request JSON -- still holds under prompt v2, and prompt v2's own text
    (asserted in ``test_gemini_v2_bundle6_*`` prompt-authority tests) still
    states the same confirmed > pinned rules > population prior > general
    knowledge authority ordering Bundle 5 introduced here.
    """

    fixture = _available_fixture(tmp_path)
    try:
        prompt = build_rich_provider_prompt(fixture.build_request())
        assert "opponent_intel_context" in prompt
        assert "rules_context" in prompt
    finally:
        fixture.close()


# =========================================================================
# I. BATTLE MEMORY REMAINS AUTHORITATIVE
# =========================================================================


def test_prior_only_and_confirmed_moves_coexist_without_authority_confusion(
    tmp_path: Path,
) -> None:
    """The decisive Bundle 5 scenario, on the historical マスカーニャ match.

    ``キノコのほうし`` is *confirmed* in this match (Bundle 3 battle memory,
    Turn 6) and also appears in the population prior. ``ヘドロばくだん`` is
    high in the population prior and never confirmed. Both are visible to
    Gemini simultaneously, in separate fields with separate authority.
    """

    fixture = _available_fixture(tmp_path)
    try:
        request = fixture.build_request()
        canonical = canonical_rich_request_dict(request)

        memory_moves = {
            turn["opponent_action"]["action_name"]
            for turn in canonical["battle_memory"]
            if turn["opponent_action"]["action_name"]
        }
        assert CONFIRMED_MEMORY_MOVE in memory_moves
        assert PRIOR_ONLY_MOVE not in memory_moves

        population_moves = [
            entry["name"] for entry in canonical["opponent_intel_context"]["population"]["moves"]
        ]
        assert CONFIRMED_MEMORY_MOVE in population_moves
        assert PRIOR_ONLY_MOVE in population_moves

        # Separate fields, separate authority -- population never annotated
        # as confirmed, memory never annotated as a prior.
        assert canonical["opponent_intel_context"]["authority"] == OPPONENT_INTEL_AUTHORITY
        assert "authority" not in json.dumps(canonical["battle_memory"], ensure_ascii=False)
    finally:
        fixture.close()


def test_intel_never_rewrites_or_deduplicates_battle_memory(tmp_path: Path) -> None:
    available = _available_fixture(tmp_path, db_name="b5-mem-available.db")
    missing = _species_missing_fixture(tmp_path, db_name="b5-mem-missing.db")
    try:
        with_intel = canonical_rich_request_dict(available.build_request())
        without_intel = canonical_rich_request_dict(missing.build_request())
        assert with_intel["opponent_intel_context"] != without_intel["opponent_intel_context"]
        # Every confirmed/legal/rules field is byte-identical either way.
        for field in (
            "battle_memory",
            "legal_actions",
            "legal_switches",
            "legal_switches_status",
            "reviewed_state",
            "selected_three",
            "selected_three_builds",
            "self_active",
            "rules_context",
            "requested_output_schema",
        ):
            assert _canonical_bytes(with_intel[field]) == _canonical_bytes(
                without_intel[field]
            )
    finally:
        available.close()
        missing.close()


# =========================================================================
# J. MATCH EXPORT
# =========================================================================


def test_match_export_records_the_exact_intel_pin(tmp_path: Path) -> None:
    """The exported audit field carries the pin identity plus its provenance.

    The provenance values are read back out of the archived generation the
    match pinned -- never re-derived from "current" data.
    """

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        pin = fixture.application._opponent_intel_pin_export(session)  # noqa: SLF001
        assert pin == {
            "status": "PINNED",
            "generation_id": session.opponent_intel_generation_id,
            "snapshot_schema_version": "opponent-intel-snapshot.v1",
            "snapshot_sha256": session.opponent_intel_snapshot_sha256,
            "source": FIXTURE_SOURCE,
            "season": FIXTURE_SEASON,
            "format": FIXTURE_FORMAT,
            "fetched_at": FIXTURE_FETCHED_AT,
        }
        # Identity/provenance only: never the population database itself,
        # never a raw source document.
        encoded = json.dumps(pin, ensure_ascii=False)
        assert PRIOR_ONLY_MOVE not in encoded
        assert "species" not in encoded
        assert "population" not in encoded
        assert "moves" not in encoded
    finally:
        fixture.close()


def test_match_export_pin_is_attached_beside_the_bundle4_rules_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both audit fields are attached by the same v3 export assembly step.

    The heavy repository-content validators the v3 builder runs first are
    stubbed out so this exercises exactly the audit-field attachment tail
    (which is where Bundle 4 put ``rules_pin`` and Bundle 5 puts
    ``opponent_intel_pin``) without seeding a fully exportable match.
    """

    from maple_next.application import match_service

    monkeypatch.setattr(
        match_service, "validate_confirmed_states_for_export", lambda **kwargs: None
    )
    monkeypatch.setattr(match_service, "validate_delta_chain_for_export", lambda **kwargs: {})
    monkeypatch.setattr(
        match_service, "validate_legal_actions_for_export", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        match_service,
        "build_integrated_match_export_v3_payload",
        lambda *, legacy_payload, rich_turns: dict(legacy_payload),
    )

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        outcome = MatchOutcomeRecord(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            outcome=MatchOutcome.WIN,
            ended_at_utc="2026-08-16T12:00:00+00:00",
            final_battle_revision=CURRENT_TURN_NUMBER,
        )
        payload = fixture.application._build_export_payload_v3(  # noqa: SLF001
            session, outcome, {"schema_version": "maple-match.v2"}
        )
        assert payload["rules_pin"]["ruleset_version"] == "M-B"
        assert payload["opponent_intel_pin"]["status"] == "PINNED"
        assert (
            payload["opponent_intel_pin"]["generation_id"]
            == session.opponent_intel_generation_id
        )
    finally:
        fixture.close()


def test_match_export_records_an_unavailable_pin_without_inventing_provenance(
    tmp_path: Path,
) -> None:
    fixture = _species_missing_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        session.opponent_intel_pin_status = "UNAVAILABLE"
        session.opponent_intel_generation_id = None
        session.opponent_intel_snapshot_sha256 = None
        assert fixture.application._opponent_intel_pin_export(session) == {  # noqa: SLF001
            "status": "UNAVAILABLE",
            "generation_id": None,
            "snapshot_schema_version": None,
            "snapshot_sha256": None,
            "source": None,
            "season": None,
            "format": None,
            "fetched_at": None,
        }
    finally:
        fixture.close()


def test_pre_bundle5_match_exports_no_intel_pin_object(tmp_path: Path) -> None:
    """A match that predates Bundle 5 exports ``null``, not a guessed pin."""

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        session.opponent_intel_pin_status = None
        session.opponent_intel_generation_id = None
        session.opponent_intel_snapshot_sha256 = None
        assert fixture.application._opponent_intel_pin_export(session) is None  # noqa: SLF001
    finally:
        fixture.close()


def test_export_pin_reports_null_provenance_when_the_archive_is_gone(
    tmp_path: Path,
) -> None:
    """Unresolvable archive: identity preserved, provenance null, never guessed."""

    import shutil

    fixture = _available_fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        shutil.rmtree(
            generation_directory(
                fixture.intel_directory, session.opponent_intel_generation_id or ""
            )
        )
        pin = fixture.application._opponent_intel_pin_export(session)  # noqa: SLF001
        assert pin["status"] == "PINNED"
        assert pin["generation_id"] == session.opponent_intel_generation_id
        assert pin["snapshot_sha256"] == session.opponent_intel_snapshot_sha256
        assert pin["source"] is None
        assert pin["season"] is None
        assert pin["format"] is None
        assert pin["fetched_at"] is None
        assert pin["snapshot_schema_version"] is None
    finally:
        fixture.close()


# =========================================================================
# K. NO NETWORK, NO PROVIDER SEND
# =========================================================================


def test_request_construction_performs_zero_network_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any socket use anywhere in the Bundle 5 path is an outright failure."""

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Bundle 5 request construction must never use the network")

    fixture = _available_fixture(tmp_path)
    try:
        monkeypatch.setattr(socket, "create_connection", _forbidden)
        monkeypatch.setattr(socket.socket, "connect", _forbidden)
        monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)

        job = fixture.application.request_rich_turn_advice("command-b5-no-network")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.opponent_intel_context["status"] == CONTEXT_STATUS_AVAILABLE
    finally:
        fixture.close()


def test_no_provider_is_ever_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building/rebuilding a request never sends and never dispatches a job."""

    from maple_next.providers import turn_transport

    def _forbidden_send(*args: object, **kwargs: object) -> None:
        raise AssertionError("Bundle 5 must never send to a provider")

    monkeypatch.setattr(
        turn_transport.GeminiTurnAdviceTransport, "send", _forbidden_send, raising=True
    )

    fixture = _available_fixture(tmp_path)
    try:
        job = fixture.application.request_rich_turn_advice("command-b5-no-send")
        fixture.application.build_rich_turn_advice_transport_request(job)
        stored = fixture.repository.get_job(job.job_id)
        assert stored.status is JobStatus.QUEUED
    finally:
        fixture.close()


# =========================================================================
# L. NO EFFECT ON LEGAL/CONFIRMED STATE
# =========================================================================


def test_intel_does_not_alter_legal_actions_switches_or_confirmed_state(
    tmp_path: Path,
) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        request = fixture.build_request()
        identity = fixture.identity(CURRENT_TURN_NUMBER)
        confirmed_actions = (
            fixture.repository.list_confirmed_legal_action_selections_for_identity(identity)
        )
        confirmation = fixture.repository.get_legal_switch_confirmation(
            identity=identity,
            based_on_confirmed_state_id=fixture.confirmed_state_id(CURRENT_TURN_NUMBER),
            applied_selection_id=fixture.applied_selection_id,
        )
        assert confirmation is not None
        assert request.legal_switches == confirmation.legal_switches
        assert {action.action_name for action in request.legal_actions} == {
            selection.action_name for selection in confirmed_actions
        }
        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        assert state.opponent_side.active.value == CURRENT_OPPONENT
        assert state.opponent_side.active.is_confirmed
    finally:
        fixture.close()


def test_selected_three_and_applied_bring_are_untouched(tmp_path: Path) -> None:
    fixture = _available_fixture(tmp_path)
    try:
        request = fixture.build_request()
        assert request.selected_three == ("マスカーニャ", "ハッサム", "ガブリアス")
        assert request.applied_selection_id == fixture.applied_selection_id
    finally:
        fixture.close()


def test_application_defaults_to_the_real_runtime_location_without_touching_it(
    tmp_path: Path, isolated_runtime_root: Path
) -> None:
    """The default directory is resolved, not hard-coded -- and stays isolated."""

    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    try:
        assert application.opponent_intel_directory == (
            isolated_runtime_root / "opponent_intel_db"
        )
    finally:
        repository.close()
