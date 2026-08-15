"""Issue #31 Bundle B final narrow remediation.

Covers the three remaining authorized gaps:

1. OPEN-draft discovery independent of a (possibly corrupted)
   ``draft.based_on_confirmed_state_id`` -- a draft discoverable only
   through its ``source_delta_id`` -> delta -> current-state relationship
   must not disappear as "no draft".
2. Repository-backed ``maple-match.v3`` export full-chain validation
   without "latest wins" dict-overwrite semantics.
3. A ``parse_match_export_v3`` that reconstructs real Bundle A objects
   (``ConfirmedTurnState``, ``ActionResultDelta``,
   ``ConfirmedLegalActionSelection``, ``FixedEvidenceMetadata``) via their
   own constructors and codecs, so their own invariants participate.

No test in this file sends anything over a network or touches a real
provider.
"""

from __future__ import annotations

import json

import pytest

from maple_next.application.match_export_v3 import (
    MatchExportV3Error,
    parse_match_export_v3,
    validate_confirmed_states_for_export,
    validate_delta_chain_for_export,
    validate_evidence_hash_shape,
    validate_legal_actions_for_export,
)
from maple_next.application.service import DomainError
from maple_next.domain.enums import ActionType, MatchOutcome
from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    Known,
    NextTurnStateDraft,
    TurnIdentity,
)

# Reuse the established fixture builder rather than duplicating it.
from tests.test_issue31_turn_state_bundle_b_second_remediation import (
    _HUMAN,
    CONFIRMED_AT,
    RichSessionFixture,
    _confirmation,
    _confirmed_side,
    _unchanged_side_delta,
)


def _outcome(**overrides) -> MatchOutcomeRecord:
    kwargs = dict(
        session_id="session-remediation-2",
        match_id="match-remediation-2",
        generation=9,
        outcome=MatchOutcome.WIN,
        ended_at_utc=CONFIRMED_AT,
        final_battle_revision=3,
    )
    kwargs.update(overrides)
    return MatchOutcomeRecord(**kwargs)


# --- 1. OPEN draft discovery independent of based_on_confirmed_state_id -----


def test_draft_with_corrupt_based_on_still_found_via_delta_and_rejected(
    tmp_path,
) -> None:
    """The core fix: a corrupt based_on_confirmed_state_id must not hide the draft."""

    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    # Delta genuinely based on the current confirmed state (discoverable
    # through list_action_result_deltas_based_on).
    delta = ActionResultDelta(
        delta_id="delta-1",
        identity=fixture.identity(),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(delta)
    fixture.repository.connection.commit()

    # A decoy confirmed state must exist to satisfy the drafts table's FK
    # on based_on_confirmed_state_id -- the corruption under test is that
    # the draft points at a real-but-wrong state, not a nonexistent one.
    decoy_state = ConfirmedTurnState(
        confirmed_state_id="state-CORRUPTED",
        identity=fixture.identity(turn_number=1, battle_revision=0),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    fixture.repository.append_confirmed_turn_state(decoy_state)
    fixture.repository.connection.commit()

    # Draft's OWN based_on_confirmed_state_id column is corrupted (does not
    # equal the current confirmed_state_id), but its source_delta_id
    # genuinely points at the delta above. The old
    # `WHERE based_on_confirmed_state_id = ?` query would never surface
    # this row at all.
    draft = NextTurnStateDraft(
        draft_id="draft-corrupt-based-on",
        identity=fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2"),
        based_on_confirmed_state_id="state-CORRUPTED",
        source_delta_id="delta-1",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    fixture.repository.upsert_next_turn_state_draft(draft)
    fixture.repository.connection.commit()

    # Confirm the pure based_on-keyed query alone would have found nothing --
    # proving this test exercises the delta-relationship discovery path.
    assert (
        fixture.repository.list_candidate_next_turn_state_drafts_for_confirmed_state(
            fixture.confirmed_state_id
        )
        == ()
    )

    with pytest.raises(DomainError, match="OPEN_DRAFT_CHAIN_INVALID"):
        fixture.application.request_rich_turn_advice("command-1")


def test_unrelated_draft_from_another_chain_does_not_block(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.confirm_legal_switches()

    # No delta/draft exists at all for this chain -- both discovery paths
    # must independently confirm "no candidate", and the request must
    # succeed normally rather than being blocked by anything foreign.
    discovered = fixture.repository.list_candidate_next_turn_state_drafts_for_confirmed_state(
        fixture.confirmed_state_id
    )
    assert discovered == ()
    deltas = fixture.repository.list_action_result_deltas_based_on(fixture.confirmed_state_id)
    assert deltas == ()

    job = fixture.application.request_rich_turn_advice("command-1")
    assert job.session_id == fixture.session_id


def test_ambiguous_duplicate_candidates_rejected(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    delta = ActionResultDelta(
        delta_id="delta-1",
        identity=fixture.identity(),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(delta)
    fixture.repository.connection.commit()

    next_identity = fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2")
    draft_a = NextTurnStateDraft(
        draft_id="draft-a",
        identity=next_identity,
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        source_delta_id="delta-1",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    fixture.repository.upsert_next_turn_state_draft(draft_a)
    fixture.repository.connection.commit()

    # A second delta based on the SAME current state, referenced by a
    # second, different draft_id -- both are legitimate discovery
    # candidates for this confirmed state, but two different drafts
    # claiming the same chain position is itself a contradiction.
    delta_2 = ActionResultDelta(
        delta_id="delta-2",
        identity=fixture.identity(),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    # source_delta_id is UNIQUE per next_turn_state_drafts row only via
    # (session_id, turn_id, battle_revision) -- use a distinct turn_id so
    # both rows can coexist.
    fixture.repository.append_action_result_delta(delta_2)
    fixture.repository.connection.commit()
    draft_b = NextTurnStateDraft(
        draft_id="draft-b",
        identity=fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2-b"),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        source_delta_id="delta-2",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    fixture.repository.upsert_next_turn_state_draft(draft_b)
    fixture.repository.connection.commit()

    with pytest.raises(DomainError, match="CONTRADICTORY_DUPLICATE_OPEN_DRAFT_REJECTED"):
        fixture.application.request_rich_turn_advice("command-1")


# --- 2. v3 export full-chain validation (pure unit tests) --------------------


def _state(confirmed_state_id: str, *, turn_number: int, battle_revision: int, previous=None):
    return ConfirmedTurnState(
        confirmed_state_id=confirmed_state_id,
        identity=TurnIdentity(
            session_id="s",
            match_id="m",
            generation=1,
            turn_id=f"turn-{turn_number}",
            turn_number=turn_number,
            battle_revision=battle_revision,
        ),
        previous_confirmed_state_id=previous,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )


def test_export_rejects_duplicate_delta_id() -> None:
    state = _state("s1", turn_number=1, battle_revision=1)
    delta_a = ActionResultDelta(
        delta_id="d1",
        identity=state.identity,
        based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    delta_b = ActionResultDelta(
        delta_id="d1",
        identity=state.identity,
        based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_DELTA_ID"):
        validate_delta_chain_for_export(
            session_id="s", match_id="m", generation=1,
            confirmed_states=(state,), deltas=(delta_a, delta_b),
        )


def test_export_rejects_ambiguous_delta_chain_position() -> None:
    state = _state("s1", turn_number=1, battle_revision=1)
    delta_a = ActionResultDelta(
        delta_id="d1", identity=state.identity, based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(), opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    delta_b = ActionResultDelta(
        delta_id="d2", identity=state.identity, based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(), opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="AMBIGUOUS_DELTA_CHAIN_POSITION"):
        validate_delta_chain_for_export(
            session_id="s", match_id="m", generation=1,
            confirmed_states=(state,), deltas=(delta_a, delta_b),
        )


def test_export_rejects_delta_identity_mismatch() -> None:
    state = _state("s1", turn_number=1, battle_revision=1)
    wrong_identity = TurnIdentity(
        session_id="s", match_id="m", generation=1, turn_id="turn-DIFFERENT",
        turn_number=1, battle_revision=1,
    )
    delta = ActionResultDelta(
        delta_id="d1", identity=wrong_identity, based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(), opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="DELTA_IDENTITY_MISMATCH"):
        validate_delta_chain_for_export(
            session_id="s", match_id="m", generation=1,
            confirmed_states=(state,), deltas=(delta,),
        )


def test_export_rejects_delta_based_on_state_not_found() -> None:
    state = _state("s1", turn_number=1, battle_revision=1)
    delta = ActionResultDelta(
        delta_id="d1", identity=state.identity, based_on_confirmed_state_id="s-MISSING",
        self_side=_unchanged_side_delta(), opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="DELTA_BASED_ON_STATE_NOT_FOUND"):
        validate_delta_chain_for_export(
            session_id="s", match_id="m", generation=1,
            confirmed_states=(state,), deltas=(delta,),
        )


def test_export_rejects_delta_foreign_identity() -> None:
    state = _state("s1", turn_number=1, battle_revision=1)
    foreign_identity = TurnIdentity(
        session_id="foreign", match_id="m", generation=1, turn_id="turn-1",
        turn_number=1, battle_revision=1,
    )
    delta = ActionResultDelta(
        delta_id="d1", identity=foreign_identity, based_on_confirmed_state_id="s1",
        self_side=_unchanged_side_delta(), opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="DELTA_FOREIGN_IDENTITY"):
        validate_delta_chain_for_export(
            session_id="s", match_id="m", generation=1,
            confirmed_states=(state,), deltas=(delta,),
        )


def test_export_rejects_contradictory_state_same_turn() -> None:
    outcome = _outcome(session_id="s", match_id="m", generation=1, final_battle_revision=5)
    state_a = _state("s1", turn_number=1, battle_revision=1)
    state_b = _state("s2", turn_number=1, battle_revision=2)
    with pytest.raises(MatchExportV3Error, match="CONTRADICTORY_STATE_SAME_TURN"):
        validate_confirmed_states_for_export(
            session_id="s", match_id="m", generation=1,
            outcome=outcome, confirmed_states=(state_a, state_b),
        )


def test_export_rejects_broken_previous_state_linkage() -> None:
    outcome = _outcome(session_id="s", match_id="m", generation=1, final_battle_revision=5)
    state_1 = _state("s1", turn_number=1, battle_revision=1, previous=None)
    state_2 = _state("s2", turn_number=2, battle_revision=2, previous="state-WRONG")
    with pytest.raises(MatchExportV3Error, match="BROKEN_PREVIOUS_STATE_LINKAGE"):
        validate_confirmed_states_for_export(
            session_id="s", match_id="m", generation=1,
            outcome=outcome, confirmed_states=(state_1, state_2),
        )


def test_export_rejects_state_beyond_final_revision() -> None:
    outcome = _outcome(session_id="s", match_id="m", generation=1, final_battle_revision=1)
    state = _state("s1", turn_number=2, battle_revision=2)
    with pytest.raises(MatchExportV3Error, match="STATE_BEYOND_FINAL_REVISION"):
        validate_confirmed_states_for_export(
            session_id="s", match_id="m", generation=1,
            outcome=outcome, confirmed_states=(state,),
        )


def test_export_legal_actions_invokes_accepted_boundary_and_rejects_blank() -> None:
    identity = TurnIdentity(
        session_id="s", match_id="m", generation=1, turn_id="t1", turn_number=1, battle_revision=1
    )
    blank_free = ConfirmedLegalActionSelection(
        confirmation_id="a1", identity=identity, action_type=ActionType.MOVE,
        action_name="Wave Crash", confirmation=_confirmation(),
    )
    validate_legal_actions_for_export(identity, (blank_free,))  # does not raise

    duplicate = ConfirmedLegalActionSelection(
        confirmation_id="a1", identity=identity, action_type=ActionType.SWITCH,
        action_name="Gholdengo", confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_LEGAL_ACTION_CONFIRMATION_ID"):
        validate_legal_actions_for_export(identity, (blank_free, duplicate))


def test_export_legal_action_identity_mismatch_rejected_by_boundary() -> None:
    identity = TurnIdentity(
        session_id="s", match_id="m", generation=1, turn_id="t1", turn_number=1, battle_revision=1
    )
    foreign_identity = TurnIdentity(
        session_id="s", match_id="m", generation=1, turn_id="t2", turn_number=2, battle_revision=2
    )
    foreign_action = ConfirmedLegalActionSelection(
        confirmation_id="a1", identity=foreign_identity, action_type=ActionType.MOVE,
        action_name="Wave Crash", confirmation=_confirmation(),
    )
    with pytest.raises(MatchExportV3Error, match="LEGAL_ACTION_BOUNDARY_REJECTED"):
        validate_legal_actions_for_export(identity, (foreign_action,))


def test_export_rejects_non_hex_evidence_sha() -> None:
    with pytest.raises(MatchExportV3Error, match="INVALID_EVIDENCE_HASH_SHAPE"):
        validate_evidence_hash_shape("z" * 64)


def test_export_accepts_valid_hex_evidence_sha() -> None:
    validate_evidence_hash_shape("a" * 64)  # does not raise


def test_export_full_flow_battle_turn_binding_mismatch(tmp_path) -> None:
    """Repository BattleTurn turn_number mismatch must fail closed at export time."""

    from maple_next.domain.enums import ActionOrder, BattleState
    from maple_next.domain.models import RecordedAction

    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    # Corrupt the repository so the BattleTurn for turn-1 claims turn_number=99,
    # while the confirmed state's identity still says turn_number=1.
    fixture.repository.connection.execute(
        "UPDATE battle_turns SET turn_number = 99 WHERE turn_id = ?", (fixture.turn_id,)
    )
    fixture.repository.connection.commit()

    fixture.repository.append_recorded_action(
        fixture.session_id,
        RecordedAction(
            action_id="action-1", turn_id=fixture.turn_id, turn_number=1,
            action_type=ActionType.MOVE, action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE, opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
        ),
    )
    fixture.repository.connection.commit()
    session = fixture.repository.load_active_session()
    session.state = BattleState.TURN_RECORDED
    fixture.repository.save_session(session)
    fixture.repository.connection.commit()
    fixture.application.end_match(MatchOutcome.WIN, human_confirmed=True)

    with pytest.raises(DomainError, match="V3_EXPORT_STATE_TURN_NUMBER_MISMATCH"):
        fixture.application.export_match()


# --- 3. Strict parser via Bundle A reconstruction ----------------------------


def _minimal_selection() -> dict:
    return {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""}


def _base_v3_payload(rich_state: dict) -> dict:
    return {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "selection": _minimal_selection(),
        "action_history": [],
        "turns": [{"turn_number": 1, "rich_state": rich_state}],
    }


def test_parser_rejects_unexpected_key_in_side_state() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_turn_state"]["self_side"]["unexpected_extra_field"] = "nope"
    with pytest.raises(MatchExportV3Error, match="UNEXPECTED_KEYS"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_invalid_action_type_in_legal_action() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_confirmation_json,
        _valid_identity_json,
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_legal_actions"] = [
        {
            "confirmation_id": "a1",
            "identity": _valid_identity_json(),
            "action_type": "TELEPORT",
            "action_name": "Wave Crash",
            **_valid_confirmation_json(),
            "source_prefill_id": None,
        }
    ]
    with pytest.raises(MatchExportV3Error, match="INVALID_ACTION_TYPE"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_legal_action_not_confirmed_by_human() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_identity_json,
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_legal_actions"] = [
        {
            "confirmation_id": "a1",
            "identity": _valid_identity_json(),
            "action_type": "MOVE",
            "action_name": "Wave Crash",
            "confirmed_by_human": False,
            "confirmed_at_utc": CONFIRMED_AT,
            "provenance": "HUMAN_CONFIRMED",
            "source_prefill_id": None,
        }
    ]
    with pytest.raises(MatchExportV3Error, match="INVALID_CONFIRMATION"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_legal_action_identity_mismatch() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_confirmation_json,
        _valid_identity_json,
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_legal_actions"] = [
        {
            "confirmation_id": "a1",
            "identity": _valid_identity_json(turn_id="turn-DIFFERENT"),
            "action_type": "MOVE",
            "action_name": "Wave Crash",
            **_valid_confirmation_json(),
            "source_prefill_id": None,
        }
    ]
    with pytest.raises(MatchExportV3Error, match="LEGAL_ACTION_IDENTITY_MISMATCH"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_duplicate_legal_action_confirmation_id() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_confirmation_json,
        _valid_identity_json,
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    action = {
        "confirmation_id": "a1",
        "identity": _valid_identity_json(),
        "action_type": "MOVE",
        "action_name": "Wave Crash",
        **_valid_confirmation_json(),
        "source_prefill_id": None,
    }
    rich_state["confirmed_legal_actions"] = [action, dict(action)]
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_LEGAL_ACTION"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_delta_state_identity_mismatch() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_confirmation_json,
        _valid_identity_json,
        _valid_rich_state_block,
        _valid_side_delta_json,
    )

    rich_state = _valid_rich_state_block()
    rich_state["source_action_result_delta"] = {
        "delta_id": "d1",
        "identity": _valid_identity_json(turn_id="turn-DIFFERENT"),
        "based_on_confirmed_state_id": "s0",
        "self_side": _valid_side_delta_json(),
        "opponent_side": _valid_side_delta_json(),
        "weather": {"observation": "UNCHANGED", "provenance_chain": ["HUMAN_INPUT"]},
        "terrain": {"observation": "UNCHANGED", "provenance_chain": ["HUMAN_INPUT"]},
        "confirmation": _valid_confirmation_json(),
    }
    with pytest.raises(MatchExportV3Error, match="DELTA_STATE_IDENTITY_MISMATCH"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_evidence_id_mismatch() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_turn_state"]["evidence_id"] = "ev-1"
    rich_state["evidence"] = {
        "evidence_id": "ev-DIFFERENT",
        "relative_path": "evidence/x.png",
        "sha256": "a" * 64,
        "recorded_at_utc": CONFIRMED_AT,
    }
    with pytest.raises(MatchExportV3Error, match="EVIDENCE_ID_MISMATCH"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_rejects_non_hex_evidence_sha_in_document() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    rich_state["confirmed_turn_state"]["evidence_id"] = "ev-1"
    rich_state["evidence"] = {
        "evidence_id": "ev-1",
        "relative_path": "evidence/x.png",
        "sha256": "z" * 64,
        "recorded_at_utc": CONFIRMED_AT,
    }
    with pytest.raises(MatchExportV3Error, match="INVALID_EVIDENCE_HASH_SHAPE"):
        parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))


def test_parser_accepts_fully_valid_rich_state_block() -> None:
    from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
        _valid_rich_state_block,
    )

    rich_state = _valid_rich_state_block()
    parsed = parse_match_export_v3(json.dumps(_base_v3_payload(rich_state)).encode("utf-8"))
    assert parsed["schema_version"] == "maple-match.v3"
