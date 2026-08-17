"""Gemini V2 Bundle 5 R1: opponent-INTEL identity/authority binding repair.

Independent verification of the Bundle 5 candidate found two related
authority-binding holes, plus one half-complete boundary defence. All three
let a context claim to describe the confirmed opponent while actually
carrying *some other* population data -- exactly the substitution the
Bundle 5 authority ordering

    CONFIRMED MATCH FACTS
        > CANONICAL RULES / LEGAL POSSIBILITIES
            > POPULATION INTEL
                > UNKNOWN

exists to make impossible.

1. **Snapshot map key vs record identity.** ``SnapshotDocument`` accepted a
   species map whose outer key ("amoonguss") disagreed with the embedded
   record's ``species_id`` ("garchomp"). Every resolver trusts the outer
   key, so a confirmed Amoonguss could resolve to Garchomp's statistics.
2. **Provider-boundary species defence in depth.** Nothing revalidated, at
   rich-request assembly, that an ``AVAILABLE`` context's
   ``resolved_species`` -- the species its ``population`` payload actually
   describes -- is the same species as its ``confirmed_active_species``.
3. **Season compatibility defence in depth.** The request boundary
   revalidated a ``MATCHED`` compatibility claim against the pinned battle
   format only. A forged ``AVAILABLE``/``MATCHED`` context carrying Season
   M-4 statistics passed against pinned rules Season M-5.

This file is hermetic: fixture bytes only, inside ``tmp_path``. No network
call, no provider send, and -- via the autouse ``isolated_runtime_root``
fixture in ``tests/conftest.py`` -- the operator's real provisioned
artifact is not even resolvable.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from maple_next.application.service import _resolve_new_match_opponent_intel_pin
from maple_next.domain.opponent_intel_context import (
    COMPATIBILITY_MATCHED,
    CONTEXT_STATUS_AVAILABLE,
    CONTEXT_STATUS_MISMATCHED,
    CONTEXT_STATUS_UNAVAILABLE,
    PIN_STATUS_UNAVAILABLE,
    REASON_PIN_UNAVAILABLE,
    OpponentIntelContextError,
    build_opponent_intel_context,
    resolve_species_record,
    species_identity_matches,
    validate_opponent_intel_context,
)
from maple_next.opponent_intel_db.generation_store import GenerationStoreError
from maple_next.opponent_intel_db.normalize import normalize_species_key
from maple_next.opponent_intel_db.runtime_intel import resolve_pinnable_generation
from maple_next.opponent_intel_db.snapshot_store import (
    SnapshotDocument,
    SnapshotStoreError,
    read_snapshot,
    write_snapshot_atomic,
)
from maple_next.providers.turn_advice_rich_state import RichStateRequestError
from tests.fixtures.bundle5 import (
    AMOONGUSS_DISPLAY,
    AMOONGUSS_ID,
    FIXTURE_SEASON,
    GARCHOMP_DISPLAY,
    GARCHOMP_ID,
    fixture_snapshot_dict,
    write_flat_pair,
)
from tests.test_gemini_v2_bundle3_confirmed_memory_build_context import (
    CURRENT_TURN_NUMBER,
    OPPONENT_ACTIVE_BY_TURN,
    Bundle3Fixture,
)

CURRENT_OPPONENT = OPPONENT_ACTIVE_BY_TURN[CURRENT_TURN_NUMBER]


def _available_fixture(tmp_path: Path) -> Bundle3Fixture:
    """The historical match with a pinned generation that contains Amoonguss."""

    return Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict())


def _key_mismatched_snapshot_dict() -> dict[str, Any]:
    """The exact corruption Blocker 1 describes.

    Outer key ``"amoonguss"``; the record filed under it is Garchomp's,
    ``species_id == "garchomp"``. Everything else about the document is
    perfectly well-formed, which is precisely why the old decoder accepted
    it.
    """

    document = fixture_snapshot_dict()
    document["species"] = {AMOONGUSS_ID: document["species"][GARCHOMP_ID]}
    return document


# =========================================================================
# 1. SNAPSHOT MAP KEY / RECORD IDENTITY
# =========================================================================


def test_species_key_disagreeing_with_embedded_species_id_fails_closed() -> None:
    """Outer key "amoonguss", embedded species_id "garchomp": reject the document."""

    corrupted = _key_mismatched_snapshot_dict()
    assert corrupted["species"][AMOONGUSS_ID]["species_id"] == GARCHOMP_ID

    with pytest.raises(SnapshotStoreError) as excinfo:
        SnapshotDocument.from_json_dict(corrupted)

    message = str(excinfo.value)
    assert AMOONGUSS_ID in message
    assert GARCHOMP_ID in message
    # Neither value was silently rewritten into the other to make the
    # document pass, and nothing was partially accepted.
    assert "does not match" in message


def test_key_mismatched_snapshot_file_fails_closed_on_read(tmp_path: Path) -> None:
    """The same rejection through the real file-reading path, not just the codec."""

    path = tmp_path / "species_stats_snapshot.json"
    path.write_text(
        json.dumps(_key_mismatched_snapshot_dict(), ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(SnapshotStoreError):
        read_snapshot(path)


def test_a_corrupted_key_can_never_resolve_a_wrong_species_record() -> None:
    """Resolution can never even be attempted: decode fails first.

    Proves the fix closes the *whole* path, not just the codec. Before the
    repair, ``resolve_species_record(document, "モロバレル")`` on the
    corrupted document would hand back the Garchomp record via the outer
    key, so an AVAILABLE context could carry Garchomp's population while
    naming a confirmed Amoonguss.
    """

    with pytest.raises(SnapshotStoreError):
        SnapshotDocument.from_json_dict(_key_mismatched_snapshot_dict())

    # And on a *legitimate* document the same lookup still works exactly as
    # it did before, returning the record whose species_id matches its key.
    document = SnapshotDocument.from_json_dict(fixture_snapshot_dict())
    record = resolve_species_record(document, CURRENT_OPPONENT)
    assert record is not None
    assert record.species_id == AMOONGUSS_ID
    assert document.species[AMOONGUSS_ID].species_id == AMOONGUSS_ID


def test_matching_keys_still_decode_exactly_as_before(tmp_path: Path) -> None:
    """Every legitimate snapshot behavior is preserved, byte for byte."""

    original = fixture_snapshot_dict()
    document = SnapshotDocument.from_json_dict(original)
    assert set(document.species) == {AMOONGUSS_ID, GARCHOMP_ID}
    assert document.to_json_dict() == original

    # Round-tripping through the real atomic writer/reader is unchanged.
    path = tmp_path / "species_stats_snapshot.json"
    write_snapshot_atomic(
        path,
        list(document.species.values()),
        source=document.source,
        season=document.season,
        format=document.format,
        fetched_at=document.fetched_at,
    )
    reread = read_snapshot(path)
    assert reread is not None
    assert reread.to_json_dict() == original


def test_species_key_identity_uses_the_shared_normalization_only() -> None:
    """Case/space equivalence is accepted; a different species never is."""

    assert normalize_species_key("  Amoonguss ") == AMOONGUSS_ID
    assert normalize_species_key("Iron Hands") == "iron-hands"

    tolerated = fixture_snapshot_dict(species_ids=(AMOONGUSS_ID,))
    tolerated["species"] = {"Amoonguss": tolerated["species"][AMOONGUSS_ID]}
    document = SnapshotDocument.from_json_dict(tolerated)
    assert document.species["Amoonguss"].species_id == AMOONGUSS_ID

    # No fuzzy tolerance whatsoever beyond that.
    hostile = fixture_snapshot_dict(species_ids=(AMOONGUSS_ID,))
    hostile["species"] = {"amoongus": hostile["species"][AMOONGUSS_ID]}
    with pytest.raises(SnapshotStoreError):
        SnapshotDocument.from_json_dict(hostile)


def test_corrupted_snapshot_cannot_generate_available_intel(tmp_path: Path) -> None:
    """End to end: a key-corrupted artifact yields UNAVAILABLE, never population."""

    intel_directory = tmp_path / "intel-db"
    write_flat_pair(intel_directory, _key_mismatched_snapshot_dict())

    # Archiving refuses to turn an untrustworthy pair into a generation.
    with pytest.raises(GenerationStoreError, match="SNAPSHOT_INVALID"):
        resolve_pinnable_generation(intel_directory)

    # The real match-creation path records that as an explicit UNAVAILABLE
    # pin (fail-soft pin, fail-closed consequence) rather than pinning
    # "whatever parsed".
    pin = _resolve_new_match_opponent_intel_pin(intel_directory)
    assert pin.status == PIN_STATUS_UNAVAILABLE
    assert pin.generation_id is None

    context = build_opponent_intel_context(
        confirmed_active_species=CURRENT_OPPONENT,
        pin=pin,
        document=None,
        generation_id=None,
        snapshot_sha256=None,
        rules_season_id=FIXTURE_SEASON,
        rules_battle_format="SINGLE",
    )
    assert context["status"] == CONTEXT_STATUS_UNAVAILABLE
    assert context["reason"] == REASON_PIN_UNAVAILABLE
    assert context["population"] is None
    assert context["resolved_species"] is None


# =========================================================================
# 2. PROVIDER-BOUNDARY SPECIES BINDING
# =========================================================================


def test_forged_available_context_with_foreign_resolved_species_is_blocked(
    tmp_path: Path,
) -> None:
    """resolved_species must be the confirmed active, not merely claim to be."""

    fixture = _available_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        assert forged["status"] == CONTEXT_STATUS_AVAILABLE
        assert forged["confirmed_active_species"] == CURRENT_OPPONENT
        # The context still names the confirmed Amoonguss, but the
        # population payload it carries now claims to describe Garchomp.
        forged["resolved_species"] = {
            "species_id": GARCHOMP_ID,
            "display_name": GARCHOMP_DISPLAY,
        }
        with pytest.raises(
            RichStateRequestError, match="AVAILABLE_CONTEXT_RESOLVED_SPECIES_NOT_CONFIRMED_ACTIVE"
        ):
            fixture.build_request_with_intel_context(forged)
    finally:
        fixture.close()


def test_forged_resolved_species_is_rejected_by_the_shared_validator() -> None:
    """The same rule, provable without a whole match fixture."""

    context = {
        "context_schema_version": "maple-opponent-intel-context.v1",
        "status": CONTEXT_STATUS_AVAILABLE,
        "reason": None,
        "authority": "POPULATION_PRIOR",
        "confirmed_active_species": AMOONGUSS_DISPLAY,
        "resolved_species": {"species_id": GARCHOMP_ID, "display_name": GARCHOMP_DISPLAY},
        "compatibility": {"status": COMPATIBILITY_MATCHED, "reason": None},
        "snapshot": {"generation_id": "materialized-aaa", "season": FIXTURE_SEASON},
        "population": {
            "moves": [],
            "abilities": [],
            "items": [],
            "natures": [],
            "partners": [],
        },
    }
    with pytest.raises(
        OpponentIntelContextError, match="AVAILABLE_CONTEXT_RESOLVED_SPECIES_NOT_CONFIRMED_ACTIVE"
    ):
        validate_opponent_intel_context(context)

    # The legitimate binding for the very same payload passes.
    context["resolved_species"] = {
        "species_id": AMOONGUSS_ID,
        "display_name": AMOONGUSS_DISPLAY,
    }
    validate_opponent_intel_context(context)


def test_legitimate_species_identity_equivalences_still_pass() -> None:
    """Exactly the equivalences resolve_species_record can produce, no more."""

    # canonical species_id, exactly and after the shared normalization
    assert species_identity_matches(
        AMOONGUSS_ID, species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert species_identity_matches(
        " Amoonguss ", species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert species_identity_matches(
        "Iron Hands", species_id="iron-hands", display_name="テツノブジン"
    )
    # exact display_name, and its case-folded form
    assert species_identity_matches(
        AMOONGUSS_DISPLAY, species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert species_identity_matches(
        "amoonguss ", species_id=AMOONGUSS_ID, display_name="Amoonguss"
    )
    # never a different species, never a prefix, never "UNKNOWN", never empty
    assert not species_identity_matches(
        GARCHOMP_DISPLAY, species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert not species_identity_matches(
        "amoong", species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert not species_identity_matches(
        "UNKNOWN", species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )
    assert not species_identity_matches(
        "   ", species_id=AMOONGUSS_ID, display_name=AMOONGUSS_DISPLAY
    )


def test_legitimate_available_context_still_builds_a_provider_ready_request(
    tmp_path: Path,
) -> None:
    """The real AVAILABLE path is entirely unaffected by the new checks."""

    fixture = _available_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_AVAILABLE
        assert context["resolved_species"] == {
            "species_id": AMOONGUSS_ID,
            "display_name": AMOONGUSS_DISPLAY,
        }
        request = fixture.build_request()
        assert request.opponent_intel_context == context
        assert request.request_hash

        job = fixture.application.request_rich_turn_advice("command-b5r1-available")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.opponent_intel_context == context
        assert rebuilt.request_hash == job.request_payload_hash
    finally:
        fixture.close()


# =========================================================================
# 3. SEASON + FORMAT COMPATIBILITY DEFENCE IN DEPTH
# =========================================================================


def test_forged_matched_claim_with_a_foreign_season_is_blocked(tmp_path: Path) -> None:
    """Rules pinned M-5, INTEL claiming M-4 with a forged MATCHED label."""

    fixture = _available_fixture(tmp_path)
    try:
        assert fixture.rules_season_id() == FIXTURE_SEASON == "M-5"
        forged = copy.deepcopy(fixture.opponent_intel_context())
        forged["snapshot"]["season"] = "M-4"
        # Deliberately left MATCHED: this is the caller-supplied label the
        # boundary must not trust.
        assert forged["compatibility"]["status"] == COMPATIBILITY_MATCHED
        assert forged["status"] == CONTEXT_STATUS_AVAILABLE
        with pytest.raises(
            RichStateRequestError,
            match="OPPONENT_INTEL_COMPATIBILITY_CLAIM_INVALID:SEASON_MISMATCH",
        ):
            fixture.build_request_with_intel_context(forged)
    finally:
        fixture.close()


def test_an_available_claim_with_no_canonical_season_fails_closed(tmp_path: Path) -> None:
    """The season input defaults to None, and that default is fail-closed."""

    fixture = _available_fixture(tmp_path)
    try:
        with pytest.raises(
            RichStateRequestError,
            match="OPPONENT_INTEL_COMPATIBILITY_CLAIM_INVALID:RULES_SEASON_NOT_SUPPLIED",
        ):
            fixture.build_request(rules_season_id="")
    finally:
        fixture.close()


def test_forged_matched_claim_with_a_foreign_format_is_still_blocked(
    tmp_path: Path,
) -> None:
    """Bundle 5's existing format half of the check is unchanged."""

    fixture = _available_fixture(tmp_path)
    try:
        forged = copy.deepcopy(fixture.opponent_intel_context())
        forged["snapshot"]["format"] = "double"
        with pytest.raises(
            RichStateRequestError, match="OPPONENT_INTEL_COMPATIBILITY_CLAIM_INVALID"
        ):
            fixture.build_request_with_intel_context(forged)
    finally:
        fixture.close()


def test_legitimate_m5_single_still_matches_and_stays_available(tmp_path: Path) -> None:
    """M-5 + single against pinned M-5 + SINGLE: MATCHED, AVAILABLE, ready."""

    fixture = _available_fixture(tmp_path)
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_AVAILABLE
        assert context["compatibility"] == {"status": COMPATIBILITY_MATCHED, "reason": None}
        assert context["snapshot"]["season"] == FIXTURE_SEASON
        assert context["snapshot"]["format"] == "single"
        assert context["population"] is not None
        assert fixture.build_request().request_hash
    finally:
        fixture.close()


def test_real_loader_season_mismatch_stays_fail_soft_and_provider_ready(
    tmp_path: Path,
) -> None:
    """An honest mismatch is unchanged: MISMATCHED, population null, still ready."""

    fixture = Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict(season="M-4"))
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_MISMATCHED
        assert context["population"] is None
        assert context["resolved_species"] is None
        assert context["snapshot"]["season"] == "M-4"

        # The strengthened season check is scoped to AVAILABLE contexts, so
        # this still assembles and still dispatches.
        assert fixture.build_request().request_hash
        job = fixture.application.request_rich_turn_advice("command-b5r1-season-mismatch")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.opponent_intel_context == context
        assert rebuilt.request_hash == job.request_payload_hash
    finally:
        fixture.close()


def test_real_loader_format_mismatch_stays_fail_soft_and_provider_ready(
    tmp_path: Path,
) -> None:
    fixture = Bundle3Fixture(tmp_path, intel_snapshot=fixture_snapshot_dict(format="double"))
    try:
        context = fixture.opponent_intel_context()
        assert context["status"] == CONTEXT_STATUS_MISMATCHED
        assert context["population"] is None
        assert fixture.build_request().request_hash
    finally:
        fixture.close()
