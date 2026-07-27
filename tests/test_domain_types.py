from __future__ import annotations

import pytest

from maple_next.domain.enums import BattleState, HpBucket
from maple_next.domain.models import AppliedSelectionSnapshot, BattleSession, StatStages


def test_all_required_battle_states_exist() -> None:
    assert {state.value for state in BattleState} == {
        "SELECTION_OPEN",
        "SELECTION_ADVICE_READY",
        "BATTLE_READY",
        "TURN_CAPTURE_PENDING",
        "TURN_REVIEW_REQUIRED",
        "TURN_REVIEWED",
        "TURN_RECORDED",
        "MATCH_ENDED",
        "MATCH_EXPORTED",
        "ABORTED",
    }


def test_hp_bucket_is_single_canonical_enum() -> None:
    assert {bucket.value for bucket in HpBucket} == {
        "0",
        "1-10",
        "11-20",
        "21-30",
        "31-40",
        "41-50",
        "51-60",
        "61-70",
        "71-80",
        "81-90",
        "91-99",
        "100",
        "UNKNOWN",
    }


def test_stat_stage_bounds() -> None:
    StatStages(attack=-6, speed=6)
    with pytest.raises(ValueError):
        StatStages(attack=-7)


def test_applied_selection_validates_three_and_lead() -> None:
    AppliedSelectionSnapshot(
        applied_selection_id="applied-1",
        selected_three=("A", "B", "C"),
        lead="A",
        backline=("B", "C"),
        source_advice_id="advice-1",
    )
    with pytest.raises(ValueError):
        AppliedSelectionSnapshot(
            applied_selection_id="applied-2",
            selected_three=("A", "A", "C"),
            lead="A",
            backline=("A", "C"),
            source_advice_id="advice-2",
        )


def test_metadata_and_battle_revision_are_independent() -> None:
    session = BattleSession(
        session_id="s",
        match_id="m",
        generation=1,
        state=BattleState.SELECTION_OPEN,
        battle_revision=1,
    )
    session.bump_metadata()
    assert session.battle_revision == 1
    assert session.metadata_revision == 1
    session.bump_battle()
    assert session.battle_revision == 2
    assert session.metadata_revision == 1


def test_board_review_draft_covers_battle_effect_fields() -> None:
    from maple_next.domain.models import BoardReviewDraft, CanonicalFact

    draft = BoardReviewDraft(
        self_hp=CanonicalFact(HpBucket.UNKNOWN, "OCR", "self.hp"),
        opponent_hp=CanonicalFact(HpBucket.FULL, "OCR", "opponent.hp"),
        self_status=CanonicalFact("UNKNOWN", "OCR", "self.status"),
        opponent_status=CanonicalFact("NONE", "OCR", "opponent.status"),
        self_stages=CanonicalFact(StatStages(attack=-1), "CARRY_FORWARD", "turn-1"),
        opponent_stages=CanonicalFact(StatStages(), "OCR", "opponent.stages"),
        weather=CanonicalFact("RAIN", "CARRY_FORWARD", "turn-1"),
        terrain=CanonicalFact("NONE", "OCR", "field.terrain"),
        self_side_effects=CanonicalFact(("REFLECT",), "HUMAN", "self.side"),
        opponent_side_effects=CanonicalFact((), "OCR", "opponent.side"),
    )
    assert draft.needs_human_confirmation is True
    assert draft.self_stages.value.attack == -1
    assert draft.self_side_effects.value == ("REFLECT",)
