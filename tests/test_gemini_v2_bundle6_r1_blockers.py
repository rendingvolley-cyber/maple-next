"""Gemini V2 Bundle 6 R1: narrow closure of the three independently-found blockers.

1. Export reader (persistence/match_store.py::get_latest_turn_advice) must
   preserve response_schema_version/advice_json exactly like
   persistence/turn_store.py::get_turn_advice does.
2. A move present only in the matched pinned population snapshot (never
   independently confirmed) must be bound to support_basis=POPULATION_PRIOR;
   claiming CONFIRMED_MATCH (or any other basis) for it is rejected.
3. A v2-tagged row whose advice_json is corrupt must never render its
   flattened columns as advice content -- the UI must fail closed with an
   explicit unavailable state, never a silent v1-style fallback.

No Bundle 1-5 semantics touched; no already-accepted Bundle 6 contract
reopened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from maple_next.application.match_export_v3 import (
    MATCH_EXPORT_SCHEMA_VERSION_V3,
    MATCH_EXPORT_SCHEMA_VERSION_V4,
    parse_match_export_v3,
    parse_match_export_v4,
)
from maple_next.application.service import BattleApplication, TurnAdviceStructuredDataCorruptError
from maple_next.domain.battle_memory import BattleMemory, BattleMemoryAction, BattleMemoryTurn
from maple_next.domain.enums import ActionOrder, ActionType, HpBucket
from maple_next.domain.models import RecordedAction, TurnAdviceSnapshot, TurnFactsSnapshot
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    FieldDelta,
    Known,
    ProvenanceStep,
    SideDelta,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_response import RecommendedAction
from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V1,
    RESPONSE_SCHEMA_VERSION_V2,
    OpponentPredictionV2,
    PredictionLineV2,
    TurnAdviceBodyV2,
    canonical_turn_advice_v2_json,
    turn_advice_body_v2_from_dict,
)
from maple_next.providers.turn_response_v2_semantics import (
    TurnAdviceV2SemanticResultCode,
    validate_turn_advice_v2_semantics,
)
from maple_next.ui.controller import STRUCTURED_ADVICE_UNAVAILABLE_MESSAGE, SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from tests.test_issue31_turn_state_provider_export_bundle_b_remediation import (
    RichSessionFixture,
)

# =========================================================================
# Blocker 1: export reader preserves v2 metadata
# =========================================================================


def _seed_turn(repository: SQLiteRepository, *, session_id: str = "s1") -> None:
    repository.connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot) "
        f"VALUES ('{session_id}', 'm1', 1, 'BATTLE_READY', 1, 0, 1)"
    )
    repository.connection.execute(
        f"INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at) "
        f"VALUES ('t1', '{session_id}', 1, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.execute(
        "INSERT INTO reviewed_turn_facts "
        "(turn_facts_id, session_id, turn_id, turn_number, self_active, opponent_active, "
        "self_hp, opponent_hp, legal_moves_json, legal_switches_json, human_note, "
        "previous_snapshot_id, created_at) VALUES "
        f"('f1', '{session_id}', 't1', 1, 'A', 'B', '71-80', '41-50', '[]', '[]', '', "
        "NULL, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.commit()


_V2_BODY_DICT: dict[str, Any] = {
    "response_schema_version": RESPONSE_SCHEMA_VERSION_V2,
    "recommended_action": {
        "action_id": "move-1",
        "action_type": "MOVE",
        "action_name": "Make It Rain",
    },
    "recommendation_robustness": "HIGH",
    "reasons": ["確定情報から有利"],
    "opponent_prediction": {
        "primary": {
            "category": "DAMAGING_MOVE",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "相手はダメージ技を選択",
        },
        "alternatives": [],
    },
    "warnings": [],
}


def _v2_snapshot(*, advice_json: str, turn_advice_id: str = "adv1") -> TurnAdviceSnapshot:
    return TurnAdviceSnapshot(
        turn_advice_id=turn_advice_id,
        turn_id="t1",
        turn_number=1,
        job_id=f"job-{turn_advice_id}",
        input_snapshot_id="f1",
        action_type=ActionType.MOVE,
        action_name="Make It Rain",
        opponent_prediction="相手はダメージ技を選択",
        rationale="確定情報から有利",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
        response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
        advice_json=advice_json,
    )


def test_a_persist_valid_v2_advice(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_turn(repository)
    body = turn_advice_body_v2_from_dict(_V2_BODY_DICT)
    canonical = canonical_turn_advice_v2_json(body)
    repository.append_turn_advice("s1", _v2_snapshot(advice_json=canonical))
    stored = repository.get_turn_advice("adv1")
    assert stored.response_schema_version == RESPONSE_SCHEMA_VERSION_V2
    assert stored.advice_json == canonical
    repository.close()


def test_b_get_latest_turn_advice_preserves_v2_metadata(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_turn(repository)
    body = turn_advice_body_v2_from_dict(_V2_BODY_DICT)
    canonical = canonical_turn_advice_v2_json(body)
    repository.append_turn_advice("s1", _v2_snapshot(advice_json=canonical))

    latest = repository.get_latest_turn_advice("t1")
    assert latest is not None
    assert latest.response_schema_version == RESPONSE_SCHEMA_VERSION_V2
    assert latest.advice_json == canonical
    repository.close()


def _seed_turn_facts(fixture: RichSessionFixture) -> None:
    fixture.repository.append_turn_facts(
        fixture.session_id,
        TurnFactsSnapshot(
            turn_facts_id="facts-1",
            turn_id=fixture.turn_id,
            turn_number=fixture.turn_number,
            self_active="Dondozo",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Wave Crash",),
            legal_switches=("Gholdengo",),
        ),
    )
    fixture.repository.connection.commit()


def _end_and_export(fixture: RichSessionFixture) -> Any:
    from maple_next.domain.enums import BattleState, MatchOutcome

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
    assert session is not None
    session.state = BattleState.TURN_RECORDED
    fixture.repository.save_session(session)
    fixture.repository.connection.commit()
    fixture.application.end_match(MatchOutcome.WIN, human_confirmed=True)
    return fixture.application.export_match()


@pytest.fixture
def rich_fixture(tmp_path: Path) -> RichSessionFixture:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.repository.upsert_legal_switch_confirmation(fixture.legal_switch_confirmation())
    return fixture


def test_c_export_recognizes_v2_advice_and_emits_v4(rich_fixture: RichSessionFixture) -> None:
    _seed_turn_facts(rich_fixture)
    body = turn_advice_body_v2_from_dict(_V2_BODY_DICT)
    canonical = canonical_turn_advice_v2_json(body)
    rich_fixture.repository.append_turn_advice(
        rich_fixture.session_id,
        TurnAdviceSnapshot(
            turn_advice_id="adv-rich-1",
            turn_id=rich_fixture.turn_id,
            turn_number=rich_fixture.turn_number,
            job_id="job-rich-1",
            input_snapshot_id="facts-1",
            action_type=ActionType.MOVE,
            action_name="Make It Rain",
            opponent_prediction="相手はダメージ技を選択",
            rationale="確定情報から有利",
            is_mock=False,
            source_type="GEMINI",
            model="gemini-2.5-flash",
            warnings=(),
            response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
            advice_json=canonical,
        ),
    )
    rich_fixture.repository.connection.commit()

    record = _end_and_export(rich_fixture)
    assert record.schema_version == MATCH_EXPORT_SCHEMA_VERSION_V4

    with open(record.export_path, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    parse_match_export_v4(json.dumps(payload).encode("utf-8"))
    turn = next(t for t in payload["turns"] if t["turn_number"] == 1)
    assert turn["response_schema_version"] == RESPONSE_SCHEMA_VERSION_V2
    assert turn["structured_response"] == json.loads(canonical)


def test_d_structured_response_survives_exact_round_trip(
    rich_fixture: RichSessionFixture,
) -> None:
    _seed_turn_facts(rich_fixture)
    body = turn_advice_body_v2_from_dict(_V2_BODY_DICT)
    canonical = canonical_turn_advice_v2_json(body)
    rich_fixture.repository.append_turn_advice(
        rich_fixture.session_id,
        TurnAdviceSnapshot(
            turn_advice_id="adv-rich-2",
            turn_id=rich_fixture.turn_id,
            turn_number=rich_fixture.turn_number,
            job_id="job-rich-2",
            input_snapshot_id="facts-1",
            action_type=ActionType.MOVE,
            action_name="Make It Rain",
            opponent_prediction="相手はダメージ技を選択",
            rationale="確定情報から有利",
            is_mock=False,
            source_type="GEMINI",
            model="gemini-2.5-flash",
            warnings=(),
            response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
            advice_json=canonical,
        ),
    )
    rich_fixture.repository.connection.commit()

    record = _end_and_export(rich_fixture)
    with open(record.export_path, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    turn = next(t for t in payload["turns"] if t["turn_number"] == 1)
    reparsed = turn_advice_body_v2_from_dict(turn["structured_response"])
    assert reparsed == body


def test_e_v1_only_match_still_exports_v3(rich_fixture: RichSessionFixture) -> None:
    # No turn_advice row at all -- exactly the pre-R1 v1-only export path.
    _seed_turn_facts(rich_fixture)
    record = _end_and_export(rich_fixture)
    assert record.schema_version == MATCH_EXPORT_SCHEMA_VERSION_V3
    with open(record.export_path, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    parse_match_export_v3(json.dumps(payload).encode("utf-8"))
    turn = next(t for t in payload["turns"] if t["turn_number"] == 1)
    assert "structured_response" not in turn
    assert "response_schema_version" not in turn


def test_f_corrupt_v2_structured_export_fails_closed(rich_fixture: RichSessionFixture) -> None:
    _seed_turn_facts(rich_fixture)
    rich_fixture.repository.append_turn_advice(
        rich_fixture.session_id,
        TurnAdviceSnapshot(
            turn_advice_id="adv-rich-3",
            turn_id=rich_fixture.turn_id,
            turn_number=rich_fixture.turn_number,
            job_id="job-rich-3",
            input_snapshot_id="facts-1",
            action_type=ActionType.MOVE,
            action_name="Make It Rain",
            opponent_prediction="相手はダメージ技を選択",
            rationale="確定情報から有利",
            is_mock=False,
            source_type="GEMINI",
            model="gemini-2.5-flash",
            warnings=(),
            response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
            advice_json='{"response_schema_version": "maple-turn-advice-response.v2"}',
        ),
    )
    rich_fixture.repository.connection.commit()

    with pytest.raises(TurnAdviceStructuredDataCorruptError):
        _end_and_export(rich_fixture)


# =========================================================================
# Blocker 2: population-only move must bind to POPULATION_PRIOR
# =========================================================================

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
_CARRY = (ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,)
CONFIRMED_MEMORY_MOVE = "れいとうビーム"
POPULATION_ONLY_MOVE = "じしん"
CURRENT_OPPONENT_ACTIVE = "ランドロス"


@dataclass
class _FakeRequest:
    battle_memory: BattleMemory
    opponent_intel_context: dict[str, Any]


def _identity(turn_number: int) -> TurnIdentity:
    return TurnIdentity(
        session_id="session-1",
        match_id="match-1",
        generation=1,
        turn_id=f"turn-{turn_number}",
        turn_number=turn_number,
        battle_revision=turn_number,
    )


def _side_delta() -> SideDelta:
    unchanged = FieldDelta.unchanged(provenance_chain=_CARRY)
    return SideDelta(
        active=unchanged,
        hp_bucket=unchanged,
        status=unchanged,
        attack_stage=unchanged,
        defense_stage=unchanged,
        special_attack_stage=unchanged,
        special_defense_stage=unchanged,
        speed_stage=unchanged,
        accuracy_stage=unchanged,
        evasion_stage=unchanged,
        side_effects=unchanged,
    )


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True,
        confirmed_at_utc="2026-08-18T00:00:00+00:00",
        provenance="HUMAN_INPUT",
    )


def _delta(turn_number: int) -> ActionResultDelta:
    unchanged_field: FieldDelta[str] = FieldDelta.unchanged(provenance_chain=_CARRY)
    return ActionResultDelta(
        delta_id=f"delta-{turn_number}",
        identity=_identity(turn_number),
        based_on_confirmed_state_id=f"state-{turn_number}",
        self_side=_side_delta(),
        opponent_side=_side_delta(),
        weather=unchanged_field,
        terrain=unchanged_field,
        confirmation=_confirmation(),
    )


def _battle_memory_with_confirmed_move() -> BattleMemory:
    turn1 = BattleMemoryTurn(
        identity=_identity(1),
        reviewed_confirmed_state_id="state-1",
        turn_start_self_active=Known.confirmed("Gholdengo", provenance_chain=_HUMAN),
        turn_start_opponent_active=Known.confirmed(
            CURRENT_OPPONENT_ACTIVE, provenance_chain=_HUMAN
        ),
        own_action=BattleMemoryAction.confirmed(ActionType.MOVE, "10まんボルト"),
        opponent_action=BattleMemoryAction.confirmed(ActionType.MOVE, CONFIRMED_MEMORY_MOVE),
        action_order=ActionOrder.SELF_FIRST,
        result=_delta(1),
    )
    return BattleMemory(turns=(turn1,))


def _matched_opponent_intel_context() -> dict[str, Any]:
    return {
        "context_schema_version": "opponent-intel-context.v1",
        "status": "AVAILABLE",
        "reason": None,
        "authority": "POPULATION_PRIOR",
        "confirmed_active_species": CURRENT_OPPONENT_ACTIVE,
        "resolved_species": {"species_id": "landorus", "display_name": CURRENT_OPPONENT_ACTIVE},
        "compatibility": {"status": "MATCHED", "reason": None},
        "snapshot": {"generation_id": "gen-1", "format": "single", "season": "M-5"},
        "population": {
            "moves": [{"name": POPULATION_ONLY_MOVE, "percentage": 90.0}],
            "abilities": [],
            "items": [],
            "natures": [],
            "partners": [],
        },
    }


def _request() -> _FakeRequest:
    return _FakeRequest(
        battle_memory=_battle_memory_with_confirmed_move(),
        opponent_intel_context=_matched_opponent_intel_context(),
    )


def _prediction_line(**overrides: Any) -> PredictionLineV2:
    fields: dict[str, Any] = {
        "category": "DAMAGING_MOVE",
        "specific_action": None,
        "support_basis": "GENERAL_KNOWLEDGE",
        "support": "LOW",
        "summary": "予測",
    }
    fields.update(overrides)
    return PredictionLineV2(**fields)


def _body_with_primary(line: PredictionLineV2) -> TurnAdviceBodyV2:
    return TurnAdviceBodyV2(
        response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
        recommended_action=RecommendedAction(
            action_id="move-1", action_type="MOVE", action_name="10まんボルト"
        ),
        recommendation_robustness="HIGH",
        reasons=("確定情報に基づく判断",),
        opponent_prediction=OpponentPredictionV2(primary=line, alternatives=()),
        warnings=(),
    )


def test_a_population_only_move_claiming_confirmed_match_high_rejected() -> None:
    line = _prediction_line(
        specific_action=POPULATION_ONLY_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.MOVE_POPULATION_ONLY_BASIS_MISMATCH


def test_b_population_only_move_population_prior_medium_valid() -> None:
    line = _prediction_line(
        specific_action=POPULATION_ONLY_MOVE,
        support_basis="POPULATION_PRIOR",
        support="MEDIUM",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_c_population_only_move_population_prior_high_rejected_by_schema() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - TurnAdviceSchemaError, schema layer
        _prediction_line(
            specific_action=POPULATION_ONLY_MOVE,
            support_basis="POPULATION_PRIOR",
            support="HIGH",
        )


def test_d_confirmed_memory_move_confirmed_match_high_valid() -> None:
    line = _prediction_line(
        specific_action=CONFIRMED_MEMORY_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_e_unsupported_move_still_rejected() -> None:
    line = _prediction_line(
        specific_action="ほのおのうず",
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_MOVE_UNSUPPORTED


def test_move_present_in_both_confirmed_and_population_keeps_confirmed_authority() -> None:
    # CONFIRMED_MEMORY_MOVE is confirmed in battle memory; add it to the
    # population snapshot too, and confirm the confirmed-memory authority
    # path still allows CONFIRMED_MATCH/HIGH (spec item 4 -- do not weaken
    # the confirmed-memory path when a move also happens to appear in the
    # population prior).
    request = _FakeRequest(
        battle_memory=_battle_memory_with_confirmed_move(),
        opponent_intel_context={
            **_matched_opponent_intel_context(),
            "population": {
                "moves": [
                    {"name": CONFIRMED_MEMORY_MOVE, "percentage": 50.0},
                    {"name": POPULATION_ONLY_MOVE, "percentage": 90.0},
                ],
                "abilities": [],
                "items": [],
                "natures": [],
                "partners": [],
            },
        },
    )
    line = _prediction_line(
        specific_action=CONFIRMED_MEMORY_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=request)  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


# =========================================================================
# Blocker 3: corrupt V2 UI must fail closed, never fall back to flattened
# =========================================================================


def _controller_for(repository: SQLiteRepository) -> SelectionFlowController:
    application = BattleApplication(repository)
    return SelectionFlowController(application, repository, MockSelectionAdviceAdapter())


def _seed_current_session(repository: SQLiteRepository) -> None:
    repository.connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot, current_turn_id, current_turn_advice_id) "
        "VALUES ('s1', 'm1', 1, 'TURN_RECORDED', 1, 0, 1, 't1', 'adv1')"
    )
    repository.connection.execute(
        "INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at) "
        "VALUES ('t1', 's1', 1, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.execute(
        "INSERT INTO reviewed_turn_facts "
        "(turn_facts_id, session_id, turn_id, turn_number, self_active, opponent_active, "
        "self_hp, opponent_hp, legal_moves_json, legal_switches_json, human_note, "
        "previous_snapshot_id, created_at) VALUES "
        "('f1', 's1', 't1', 1, 'A', 'B', '71-80', '41-50', '[]', '[]', '', "
        "NULL, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.commit()


def test_a_valid_v2_row_renders_normal_structured_content(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_current_session(repository)
    body = turn_advice_body_v2_from_dict(_V2_BODY_DICT)
    canonical = canonical_turn_advice_v2_json(body)
    repository.append_turn_advice("s1", _v2_snapshot(advice_json=canonical))

    controller = _controller_for(repository)
    view = controller.refresh()
    assert view.turn_advice is not None
    assert view.turn_advice.unavailable_reason is None
    assert view.turn_advice.structured_v2 == body
    repository.close()


def test_b_corrupt_v2_row_never_renders_flattened_recommendation(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_current_session(repository)
    repository.append_turn_advice(
        "s1",
        _v2_snapshot(
            advice_json='{"response_schema_version": "maple-turn-advice-response.v2"}'
        ),
    )

    controller = _controller_for(repository)
    view = controller.refresh()
    assert view.turn_advice is not None
    # The flattened columns exist on the stored row (action_name="Make It
    # Rain", etc.) but must never surface through the view for a corrupt v2
    # row -- everything content-bearing is a safe placeholder instead.
    assert view.turn_advice.action_type == ""
    assert view.turn_advice.action_name == ""
    assert view.turn_advice.opponent_prediction == ""
    assert view.turn_advice.rationale == ""
    assert view.turn_advice.warnings == ()
    repository.close()


def test_c_corrupt_v2_row_exposes_explicit_unavailable_state(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_current_session(repository)
    repository.append_turn_advice(
        "s1",
        _v2_snapshot(advice_json="not even json"),
    )

    controller = _controller_for(repository)
    view = controller.refresh()
    assert view.turn_advice is not None
    assert view.turn_advice.unavailable_reason == STRUCTURED_ADVICE_UNAVAILABLE_MESSAGE
    repository.close()


def test_d_corrupt_v2_row_never_fabricates_structured_v2(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_current_session(repository)
    repository.append_turn_advice(
        "s1",
        _v2_snapshot(advice_json='{"garbage": true}'),
    )

    controller = _controller_for(repository)
    view = controller.refresh()
    assert view.turn_advice is not None
    assert view.turn_advice.structured_v2 is None
    repository.close()


def test_e_v1_row_renders_unchanged(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    _seed_current_session(repository)
    repository.append_turn_advice(
        "s1",
        TurnAdviceSnapshot(
            turn_advice_id="adv1",
            turn_id="t1",
            turn_number=1,
            job_id="job-adv1",
            input_snapshot_id="f1",
            action_type=ActionType.MOVE,
            action_name="Make It Rain",
            opponent_prediction="Opponent likely attacks",
            rationale="Best expected value",
            is_mock=False,
            source_type="GEMINI",
            model="gemini-2.5-flash",
            warnings=(),
        ),
    )

    controller = _controller_for(repository)
    view = controller.refresh()
    assert view.turn_advice is not None
    assert view.turn_advice.unavailable_reason is None
    assert view.turn_advice.response_schema_version == RESPONSE_SCHEMA_VERSION_V1
    assert view.turn_advice.action_name == "Make It Rain"
    assert view.turn_advice.opponent_prediction == "Opponent likely attacks"
    repository.close()
