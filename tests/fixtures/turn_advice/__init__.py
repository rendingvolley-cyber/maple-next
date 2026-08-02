"""Offline, secret-free fixtures for the Turn Advice contract (issue #31 Lane C).

Nothing in this package touches the network, SQLite, or any live provider.
All values below are literal, deterministic Python values used to build a
canonical ``TurnAdviceRequest`` for tests.
"""

from __future__ import annotations

from maple_next.domain.enums import ActionType, HpBucket
from maple_next.domain.models import ReviewedBoardSnapshot, StatStages
from maple_next.providers.turn_request import (
    LegalAction,
    TurnAdviceRequest,
    build_turn_advice_request,
)

SESSION_ID = "session-1"
MATCH_ID = "match-1"
GENERATION = 3
TURN_NUMBER = 5
BATTLE_REVISION = 7
REVIEWED_SNAPSHOT_ID = "board-1"
SELF_ACTIVE = "Gholdengo"
SELECTED_THREE = ("Gholdengo", "Dragonite", "Dondozo")

MOVE_ACTION_1 = LegalAction(
    action_id="move-1",
    action_type=ActionType.MOVE,
    action_name="Make It Rain",
    owner_active=SELF_ACTIVE,
)
MOVE_ACTION_2 = LegalAction(
    action_id="move-2",
    action_type=ActionType.MOVE,
    action_name="Shadow Ball",
    owner_active=SELF_ACTIVE,
)
SWITCH_ACTION_1 = LegalAction(
    action_id="switch-1",
    action_type=ActionType.SWITCH,
    action_name="Dragonite",
    switch_target="Dragonite",
)

LEGAL_ACTIONS = (MOVE_ACTION_1, MOVE_ACTION_2, SWITCH_ACTION_1)


def build_sample_reviewed_snapshot() -> ReviewedBoardSnapshot:
    return ReviewedBoardSnapshot(
        reviewed_board_id=REVIEWED_SNAPSHOT_ID,
        turn_id="turn-1",
        self_active=SELF_ACTIVE,
        opponent_active="Garchomp",
        self_hp=HpBucket.SEVENTY_ONE_TO_EIGHTY,
        opponent_hp=HpBucket.FORTY_ONE_TO_FIFTY,
        self_status="",
        opponent_status="",
        self_stages=StatStages(),
        opponent_stages=StatStages(speed=1),
        weather="UNKNOWN",
        terrain="UNKNOWN",
        self_side_effects=(),
        opponent_side_effects=("Stealth Rock",),
    )


def build_sample_request(**overrides: object) -> TurnAdviceRequest:
    kwargs: dict[str, object] = {
        "session_id": SESSION_ID,
        "match_id": MATCH_ID,
        "generation": GENERATION,
        "turn_number": TURN_NUMBER,
        "battle_revision": BATTLE_REVISION,
        "reviewed_snapshot_id": REVIEWED_SNAPSHOT_ID,
        "reviewed_snapshot": build_sample_reviewed_snapshot(),
        "self_active": SELF_ACTIVE,
        "selected_three": SELECTED_THREE,
        "legal_actions": LEGAL_ACTIONS,
    }
    kwargs.update(overrides)
    return build_turn_advice_request(**kwargs)  # type: ignore[arg-type]


#: A strictly valid provider response body matching the recommended-move action above.
VALID_PROVIDER_BODY_DICT: dict[str, object] = {
    "recommended_action": {
        "action_id": "move-1",
        "action_type": "MOVE",
        "action_name": "Make It Rain",
    },
    "reasons": ["Highest confirmed damage vs opponent_active", "Self is not threatened this turn"],
    "warnings": ["Opponent may have a resist berry"],
    "opponent_prediction": {
        "category": "MOVE",
        "predicted_action": None,
        "summary": "Opponent likely attacks with a confirmed STAB move",
        "confidence": 0.6,
    },
}

VALID_PROVIDER_TEXT = (
    '{"recommended_action":{"action_id":"move-1","action_type":"MOVE",'
    '"action_name":"Make It Rain"},'
    '"reasons":["Highest confirmed damage vs opponent_active",'
    '"Self is not threatened this turn"],'
    '"warnings":["Opponent may have a resist berry"],'
    '"opponent_prediction":{"category":"MOVE","predicted_action":null,'
    '"summary":"Opponent likely attacks with a confirmed STAB move",'
    '"confidence":0.6}}'
)

TRUSTED_SOURCE_TYPE = "GEMINI"
TRUSTED_MODEL = "gemini-2.5-flash"
