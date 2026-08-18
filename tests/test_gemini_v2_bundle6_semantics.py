"""Gemini V2 Bundle 6: request-aware exact-action source-membership tests.

Builds a minimal, hand-constructed two-Turn ``BattleMemory`` plus an
``opponent_intel_context`` dict, wrapped in a lightweight stand-in exposing
only the two attributes :func:`validate_turn_advice_v2_semantics` actually
reads (``battle_memory``, ``opponent_intel_context``) -- it does not need a
full ``RichStateTurnAdviceRequest`` (projection/state_confirmation/etc. are
irrelevant to this pure check).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from maple_next.domain.battle_memory import BattleMemory, BattleMemoryAction, BattleMemoryTurn
from maple_next.domain.enums import ActionOrder, ActionType
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    FieldDelta,
    Known,
    ProvenanceStep,
    SideDelta,
    TurnIdentity,
)
from maple_next.providers.turn_response_v2 import (
    OpponentPredictionV2,
    PredictionLineV2,
    TurnAdviceBodyV2,
)
from maple_next.providers.turn_response_v2_semantics import (
    TurnAdviceV2SemanticResultCode,
    validate_turn_advice_v2_semantics,
)

_CARRY = (ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,)
_HUMAN = (ProvenanceStep.HUMAN_INPUT,)

TURN1_OPPONENT_ACTIVE = "ガブリアス"
TURN2_OPPONENT_ACTIVE = "ランドロス"
CONFIRMED_MEMORY_MOVE = "れいとうビーム"
POPULATION_ONLY_MOVE = "じしん"
UNSUPPORTED_MOVE = "ほのおのうず"
UNSEEN_SWITCH_TARGET = "サーフゴー"


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


def _battle_memory() -> BattleMemory:
    turn1 = BattleMemoryTurn(
        identity=_identity(1),
        reviewed_confirmed_state_id="state-1",
        turn_start_self_active=Known.confirmed("Gholdengo", provenance_chain=_HUMAN),
        turn_start_opponent_active=Known.confirmed(TURN1_OPPONENT_ACTIVE, provenance_chain=_HUMAN),
        own_action=BattleMemoryAction.confirmed(ActionType.MOVE, "10まんボルト"),
        opponent_action=BattleMemoryAction.confirmed(ActionType.MOVE, CONFIRMED_MEMORY_MOVE),
        action_order=ActionOrder.SELF_FIRST,
        result=_delta(1),
    )
    turn2 = BattleMemoryTurn(
        identity=_identity(2),
        reviewed_confirmed_state_id="state-2",
        turn_start_self_active=Known.confirmed("Gholdengo", provenance_chain=_HUMAN),
        turn_start_opponent_active=Known.confirmed(TURN2_OPPONENT_ACTIVE, provenance_chain=_HUMAN),
        own_action=BattleMemoryAction.confirmed(ActionType.MOVE, "ギガドレイン"),
        opponent_action=BattleMemoryAction.unknown(),
        action_order=ActionOrder.SELF_FIRST,
        result=_delta(2),
    )
    return BattleMemory(turns=(turn1, turn2))


def _matched_opponent_intel_context(*, confirmed_active_species: str) -> dict[str, Any]:
    return {
        "context_schema_version": "opponent-intel-context.v1",
        "status": "AVAILABLE",
        "reason": None,
        "authority": "POPULATION_PRIOR",
        "confirmed_active_species": confirmed_active_species,
        "resolved_species": {"species_id": "landorus", "display_name": confirmed_active_species},
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
        battle_memory=_battle_memory(),
        opponent_intel_context=_matched_opponent_intel_context(
            confirmed_active_species=TURN2_OPPONENT_ACTIVE
        ),
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
    from maple_next.providers.turn_response import RecommendedAction

    return TurnAdviceBodyV2(
        response_schema_version="maple-turn-advice-response.v2",
        recommended_action=RecommendedAction(
            action_id="move-1", action_type="MOVE", action_name="10まんボルト"
        ),
        recommendation_robustness="HIGH",
        reasons=("確定情報に基づく判断",),
        opponent_prediction=OpponentPredictionV2(primary=line, alternatives=()),
        warnings=(),
    )


# =========================================================================
# E. EXACT ACTION SOURCE MEMBERSHIP
# =========================================================================


def test_confirmed_memory_move_exact_action_allowed() -> None:
    line = _prediction_line(
        category="DAMAGING_MOVE",
        specific_action=CONFIRMED_MEMORY_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_matched_pinned_population_move_allowed() -> None:
    line = _prediction_line(
        category="NON_DAMAGING_MOVE",
        specific_action=POPULATION_ONLY_MOVE,
        support_basis="POPULATION_PRIOR",
        support="MEDIUM",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_unsupported_exact_move_rejected() -> None:
    line = _prediction_line(
        category="DAMAGING_MOVE",
        specific_action=UNSUPPORTED_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_MOVE_UNSUPPORTED


def test_general_knowledge_basis_cannot_ground_an_exact_move() -> None:
    # GENERAL_KNOWLEDGE always forces support=LOW (schema layer), which in
    # turn forces specific_action=None (also schema layer) -- so a
    # GENERAL_KNOWLEDGE-only exact move name is structurally unreachable.
    # This is the schema-level half of spec test #26; see
    # test_specific_action_with_low_support_rejected in
    # test_gemini_v2_bundle6_response_v2_schema.py for the direct proof.
    import pytest

    from maple_next.providers.turn_response import TurnAdviceSchemaError

    with pytest.raises(TurnAdviceSchemaError):
        _prediction_line(
            category="DAMAGING_MOVE",
            specific_action=UNSUPPORTED_MOVE,
            support_basis="GENERAL_KNOWLEDGE",
            support="LOW",
        )


def test_confirmed_opponent_switch_target_allowed() -> None:
    line = _prediction_line(
        category="SWITCH",
        specific_action=TURN1_OPPONENT_ACTIVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_unseen_inferred_opponent_switch_target_rejected() -> None:
    line = _prediction_line(
        category="SWITCH",
        specific_action=UNSEEN_SWITCH_TARGET,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED


def test_current_active_as_switch_target_rejected() -> None:
    line = _prediction_line(
        category="SWITCH",
        specific_action=TURN2_OPPONENT_ACTIVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE


def test_null_specific_action_always_valid() -> None:
    line = _prediction_line(category="SWITCH", specific_action=None, support="LOW")
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=_request())  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.VALID


def test_unavailable_population_context_grants_no_move_membership() -> None:
    request = _FakeRequest(
        battle_memory=_battle_memory(),
        opponent_intel_context={
            "context_schema_version": "opponent-intel-context.v1",
            "status": "UNAVAILABLE",
            "reason": "INTEL_PIN_UNAVAILABLE",
            "authority": "POPULATION_PRIOR",
            "confirmed_active_species": TURN2_OPPONENT_ACTIVE,
            "resolved_species": None,
            "compatibility": {"status": "UNAVAILABLE", "reason": "INTEL_PIN_UNAVAILABLE"},
            "snapshot": None,
            "population": None,
        },
    )
    line = _prediction_line(
        category="NON_DAMAGING_MOVE",
        specific_action=POPULATION_ONLY_MOVE,
        support_basis="CONFIRMED_MATCH",
        support="HIGH",
    )
    result = validate_turn_advice_v2_semantics(_body_with_primary(line), request=request)  # type: ignore[arg-type]
    assert result is TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_MOVE_UNSUPPORTED
