"""Issue #31 Bundle B closure remediation.

Covers the three authorized closure gaps:

1. ``parse_match_export_v3`` full-document cross-object validation, run
   after every turn's ``rich_state`` block has already been reconstructed
   and validated in isolation.
2. Six additional forbidden keys (credential/credentials/secret/secrets/
   evidence_bytes/evidence_base64), recursively rejected at root, nested
   dict, and nested list placement.
3. Repository-backed ``maple-match.v3`` export delta discovery without an
   identity pre-filter, so a corrupt or foreign delta referencing a
   currently-exported confirmed state cannot disappear before
   ``validate_delta_chain_for_export`` runs.

No test in this file sends anything over a network or touches a real
provider.
"""

from __future__ import annotations

import json

import pytest

from maple_next.application.match_export_v3 import (
    RICH_STATE_EXPORT_CONTRACT_VERSION,
    MatchExportV3Error,
    parse_match_export_v3,
)
from maple_next.application.service import DomainError
from maple_next.domain.enums import ActionType, MatchOutcome
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmedTurnState,
    FieldDelta,
    Known,
)
from tests.test_issue31_turn_state_bundle_b_second_remediation import (
    _HUMAN,
    CONFIRMED_AT,
    RichSessionFixture,
    _confirmation,
    _confirmed_side,
    _unchanged_side_delta,
)
from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
    _known_json,
    _valid_confirmation_json,
    _valid_identity_json,
    _valid_side_delta_json,
    _valid_side_state_json,
)

# --- Shared multi-turn document builder --------------------------------------


def _state_json(*, state_id: str, turn_number: int, battle_revision: int, previous: str | None):
    return {
        "confirmed_state_id": state_id,
        "previous_confirmed_state_id": previous,
        "identity": _valid_identity_json(
            turn_id=f"turn-{turn_number}", turn_number=turn_number, battle_revision=battle_revision
        ),
        "self_side": _valid_side_state_json(),
        "opponent_side": _valid_side_state_json(),
        "weather": _known_json(),
        "terrain": _known_json(),
        "confirmation": _valid_confirmation_json(),
        "evidence_id": None,
    }


def _delta_json(*, delta_id: str, turn_number: int, battle_revision: int, based_on: str):
    return {
        "delta_id": delta_id,
        "identity": _valid_identity_json(
            turn_id=f"turn-{turn_number}", turn_number=turn_number, battle_revision=battle_revision
        ),
        "based_on_confirmed_state_id": based_on,
        "self_side": _valid_side_delta_json(),
        "opponent_side": _valid_side_delta_json(),
        "weather": {"observation": "UNCHANGED", "provenance_chain": ["HUMAN_INPUT"]},
        "terrain": {"observation": "UNCHANGED", "provenance_chain": ["HUMAN_INPUT"]},
        "confirmation": _valid_confirmation_json(),
    }


def _rich_state_block(*, state, delta=None):
    return {
        "contract_version": RICH_STATE_EXPORT_CONTRACT_VERSION,
        "confirmed_turn_state": state,
        "source_action_result_delta": delta,
        "confirmed_legal_actions": [],
        "evidence": None,
    }


def _legacy_turn(turn_number: int, rich_state: dict) -> dict:
    return {
        "turn_number": turn_number,
        "reviewed_facts": {},
        "advice": None,
        "self_executed_action": {},
        "opponent_executed_action": None,
        "action_order": "SELF_FIRST",
        "recorded_at_utc": CONFIRMED_AT,
        "actual_action": {},
        "rich_state": rich_state,
    }


def _valid_two_turn_payload(**overrides) -> dict:
    state_1 = _state_json(state_id="s1", turn_number=1, battle_revision=1, previous=None)
    delta_1 = _delta_json(delta_id="d1", turn_number=1, battle_revision=1, based_on="s1")
    state_2 = _state_json(state_id="s2", turn_number=2, battle_revision=2, previous="s1")

    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 2,
        "selection": {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""},
        "action_history": [],
        "turns": [
            _legacy_turn(1, _rich_state_block(state=state_1, delta=delta_1)),
            _legacy_turn(2, _rich_state_block(state=state_2)),
        ],
    }
    payload.update(overrides)
    return payload


# --- 1. Full-document cross-object validation --------------------------------


def test_valid_multi_turn_document_passes() -> None:
    payload = _valid_two_turn_payload()
    parsed = parse_match_export_v3(json.dumps(payload).encode("utf-8"))
    assert len(parsed["turns"]) == 2


def test_document_rejects_top_level_session_mismatch() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["rich_state"]["confirmed_turn_state"]["identity"]["session_id"] = "wrong"
    payload["turns"][0]["rich_state"]["source_action_result_delta"]["identity"]["session_id"] = (
        "wrong"
    )
    with pytest.raises(MatchExportV3Error, match="STATE_FOREIGN_IDENTITY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_top_level_match_mismatch() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["match_id"] = "wrong"
    with pytest.raises(MatchExportV3Error, match="STATE_FOREIGN_IDENTITY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_top_level_generation_mismatch() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["generation"] = 999
    with pytest.raises(MatchExportV3Error, match="STATE_FOREIGN_IDENTITY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_legacy_turn_number_mismatch() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["turn_number"] = 5  # rich state still claims turn_number 2
    with pytest.raises(MatchExportV3Error, match="LEGACY_TURN_NUMBER_MISMATCH"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_broken_previous_state_linkage() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"][
        "previous_confirmed_state_id"
    ] = "state-WRONG"
    with pytest.raises(MatchExportV3Error, match="BROKEN_PREVIOUS_STATE_LINKAGE"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_invalid_turn_progression() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["turn_number"] = 3
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["turn_number"] = 3
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["turn_id"] = "turn-3"
    with pytest.raises(MatchExportV3Error, match="INVALID_TURN_PROGRESSION"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_invalid_revision_progression() -> None:
    # 00 design decision (Issue #31 comments 5217661584 / 5217996240):
    # battle_revision is a durable global mutation-revision counter, so the
    # chain rule is "strictly greater than previous", not "exactly +1". A
    # larger revision (e.g. 9 > 1) is therefore no longer invalid on its
    # own -- the corrupt case that must still fail is a revision that is
    # NOT greater than the previous turn's own revision (state_1's is 1).
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["battle_revision"] = 1
    payload["final_battle_revision"] = 1
    with pytest.raises(MatchExportV3Error, match="INVALID_REVISION_PROGRESSION"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_duplicate_state_id() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["confirmed_state_id"] = "s1"
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_STATE_ID"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_duplicate_state_turn_position() -> None:
    # Distinct state_id, but claims the same turn_number as an earlier turn --
    # requires distinct turn_id to avoid legacy-turn-number mismatch first,
    # and distinct confirmed_state_id to avoid duplicate-id firing first.
    state_1 = _state_json(state_id="s1", turn_number=1, battle_revision=1, previous=None)
    delta_1 = _delta_json(delta_id="d1", turn_number=1, battle_revision=1, based_on="s1")
    state_2 = _state_json(state_id="s2", turn_number=1, battle_revision=2, previous="s1")
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 2,
        "selection": {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""},
        "action_history": [],
        "turns": [
            _legacy_turn(1, _rich_state_block(state=state_1, delta=delta_1)),
            _legacy_turn(1, _rich_state_block(state=state_2)),
        ],
    }
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_STATE_TURN_POSITION"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_duplicate_delta_id() -> None:
    state_1 = _state_json(state_id="s1", turn_number=1, battle_revision=1, previous=None)
    delta_1 = _delta_json(delta_id="dup", turn_number=1, battle_revision=1, based_on="s1")
    state_2 = _state_json(state_id="s2", turn_number=2, battle_revision=2, previous="s1")
    delta_2 = _delta_json(delta_id="dup", turn_number=2, battle_revision=2, based_on="s2")
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 2,
        "selection": {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""},
        "action_history": [],
        "turns": [
            _legacy_turn(1, _rich_state_block(state=state_1, delta=delta_1)),
            _legacy_turn(2, _rich_state_block(state=state_2, delta=delta_2)),
        ],
    }
    with pytest.raises(MatchExportV3Error, match="DUPLICATE_DELTA_ID"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_delta_based_on_not_matching_same_turn_state() -> None:
    payload = _valid_two_turn_payload()
    # Delta embedded alongside turn 1's state claims to be based on a
    # different state than the one it's embedded with.
    payload["turns"][0]["rich_state"]["source_action_result_delta"][
        "based_on_confirmed_state_id"
    ] = "s-OTHER"
    with pytest.raises(MatchExportV3Error, match="DELTA_BASED_ON_MISMATCH"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_delta_identity_vs_state_identity_contradiction() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["rich_state"]["source_action_result_delta"]["identity"]["turn_id"] = (
        "turn-DIFFERENT"
    )
    with pytest.raises(MatchExportV3Error, match="DELTA_STATE_IDENTITY_MISMATCH"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_legal_action_identity_vs_state_identity_contradiction() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["rich_state"]["confirmed_legal_actions"] = [
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
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_revision_beyond_final_battle_revision() -> None:
    payload = _valid_two_turn_payload()
    payload["final_battle_revision"] = 1  # turn 2's state is at revision 2
    with pytest.raises(MatchExportV3Error, match="STATE_BEYOND_FINAL_REVISION"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_evidence_id_present_but_evidence_absent() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["rich_state"]["confirmed_turn_state"]["evidence_id"] = "ev-1"
    with pytest.raises(MatchExportV3Error, match="EVIDENCE_REQUIRED_BUT_ABSENT"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_document_rejects_cross_turn_foreign_identity() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][1]["rich_state"]["confirmed_turn_state"]["identity"]["session_id"] = (
        "other-session"
    )
    with pytest.raises(MatchExportV3Error, match="STATE_FOREIGN_IDENTITY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_single_turn_document_still_passes_unaffected() -> None:
    """A single-turn document has no adjacent-turn checks to run at all."""

    state_1 = _state_json(state_id="s1", turn_number=1, battle_revision=1, previous=None)
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "selection": {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""},
        "action_history": [],
        "turns": [_legacy_turn(1, _rich_state_block(state=state_1))],
    }
    parse_match_export_v3(json.dumps(payload).encode("utf-8"))


# --- 2. Extended forbidden-key set -------------------------------------------


_NEW_FORBIDDEN_KEYS = (
    "credential",
    "credentials",
    "secret",
    "secrets",
    "evidence_bytes",
    "evidence_base64",
)


@pytest.mark.parametrize("forbidden_key", _NEW_FORBIDDEN_KEYS)
def test_forbidden_key_rejected_at_root(forbidden_key: str) -> None:
    payload = _valid_two_turn_payload()
    payload[forbidden_key] = "nope"
    with pytest.raises(MatchExportV3Error, match="FORBIDDEN_KEY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("forbidden_key", _NEW_FORBIDDEN_KEYS)
def test_forbidden_key_rejected_in_nested_dict(forbidden_key: str) -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["rich_state"]["confirmed_turn_state"][forbidden_key] = "nope"
    with pytest.raises(MatchExportV3Error, match="FORBIDDEN_KEY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("forbidden_key", _NEW_FORBIDDEN_KEYS)
def test_forbidden_key_rejected_in_nested_list(forbidden_key: str) -> None:
    payload = _valid_two_turn_payload()
    payload["action_history"] = [{"turn_number": 1, forbidden_key: "nope"}]
    with pytest.raises(MatchExportV3Error, match="FORBIDDEN_KEY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_advice_model_and_source_type_still_accepted() -> None:
    payload = _valid_two_turn_payload()
    payload["turns"][0]["advice"] = {"model": "gemini-2.5-flash", "source_type": "GEMINI"}
    parse_match_export_v3(json.dumps(payload).encode("utf-8"))


# --- 3. Delta candidate discovery without identity pre-filter ----------------


def test_repository_export_rejects_current_chain_delta_with_foreign_session(
    tmp_path,
) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    corrupt_delta = ActionResultDelta(
        delta_id="delta-foreign-session",
        identity=fixture.identity(session_id="foreign-session"),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(corrupt_delta)
    fixture.repository.connection.commit()

    # Prove the candidate helper actually surfaces the corrupted row.
    candidates = fixture.repository.list_action_result_delta_candidates_for_confirmed_states(
        (fixture.confirmed_state_id,)
    )
    assert any(d.delta_id == "delta-foreign-session" for d in candidates)

    _prepare_for_export(fixture)
    with pytest.raises(DomainError, match="V3_EXPORT_DELTA_VALIDATION_FAILED"):
        fixture.application.export_match()


def test_repository_export_rejects_current_chain_delta_with_foreign_match(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    corrupt_delta = ActionResultDelta(
        delta_id="delta-foreign-match",
        identity=fixture.identity(match_id="foreign-match"),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(corrupt_delta)
    fixture.repository.connection.commit()

    _prepare_for_export(fixture)
    with pytest.raises(DomainError, match="V3_EXPORT_DELTA_VALIDATION_FAILED"):
        fixture.application.export_match()


def test_repository_export_rejects_current_chain_delta_with_corrupt_turn_id(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    corrupt_delta = ActionResultDelta(
        delta_id="delta-corrupt-turn-id",
        identity=fixture.identity(turn_id="turn-CORRUPT"),
        based_on_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(corrupt_delta)
    fixture.repository.connection.commit()

    _prepare_for_export(fixture)
    with pytest.raises(DomainError, match="V3_EXPORT_DELTA_VALIDATION_FAILED"):
        fixture.application.export_match()


def test_repository_export_unrelated_other_match_delta_does_not_block(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()

    # A wholly unrelated confirmed state (different session/match) with its
    # own delta -- must never influence this export.
    other_state = ConfirmedTurnState(
        confirmed_state_id="state-other-match",
        identity=fixture.identity(
            session_id="other-session", match_id="other-match", turn_id="turn-o", battle_revision=0
        ),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    fixture.repository.append_confirmed_turn_state(other_state)
    unrelated_delta = ActionResultDelta(
        delta_id="delta-unrelated",
        identity=other_state.identity,
        based_on_confirmed_state_id="state-other-match",
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    fixture.repository.append_action_result_delta(unrelated_delta)
    fixture.repository.connection.commit()

    candidates = fixture.repository.list_action_result_delta_candidates_for_confirmed_states(
        (fixture.confirmed_state_id,)
    )
    assert all(d.delta_id != "delta-unrelated" for d in candidates)

    _prepare_for_export(fixture)
    record = fixture.application.export_match()
    assert record.schema_version == "maple-match.v3"


def _prepare_for_export(fixture: RichSessionFixture) -> None:
    from datetime import UTC

    from maple_next.domain.enums import ActionOrder, BattleState
    from maple_next.domain.models import RecordedAction

    fixture.repository.append_recorded_action(
        fixture.session_id,
        RecordedAction(
            action_id="action-1",
            turn_id=fixture.turn_id,
            turn_number=fixture.turn_number,
            action_type=ActionType.MOVE,
            action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
        ),
    )
    fixture.repository.connection.commit()
    session = fixture.repository.load_active_session()
    session.state = BattleState.TURN_RECORDED
    fixture.repository.save_session(session)
    fixture.repository.connection.commit()
    fixture.application.end_match(MatchOutcome.WIN, human_confirmed=True)
    del UTC
