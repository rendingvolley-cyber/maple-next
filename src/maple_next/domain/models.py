"""Pure canonical domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from maple_next.domain.enums import BattleState, HpBucket

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CanonicalFact(Generic[T]):
    value: T
    source: str
    provenance: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class StatStages:
    attack: int = 0
    defense: int = 0
    special_attack: int = 0
    special_defense: int = 0
    speed: int = 0
    accuracy: int = 0
    evasion: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.attack,
            self.defense,
            self.special_attack,
            self.special_defense,
            self.speed,
            self.accuracy,
            self.evasion,
        ):
            if not -6 <= value <= 6:
                raise ValueError("stat stages must be between -6 and +6")

    def to_canonical_dict(self) -> dict[str, int]:
        return {
            "attack": self.attack,
            "defense": self.defense,
            "special_attack": self.special_attack,
            "special_defense": self.special_defense,
            "speed": self.speed,
            "accuracy": self.accuracy,
            "evasion": self.evasion,
        }


@dataclass(frozen=True, slots=True)
class SelectionFacts:
    reviewed_selection_id: str
    self_team: tuple[str, ...]
    opponent_team: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.self_team) != 6 or len(set(self.self_team)) != 6:
            raise ValueError("self team must contain six unique names")
        if len(self.opponent_team) != 6 or len(set(self.opponent_team)) != 6:
            raise ValueError("opponent team must contain six unique names")


@dataclass(frozen=True, slots=True)
class AppliedSelectionSnapshot:
    applied_selection_id: str
    selected_three: tuple[str, str, str]
    lead: str
    backline: tuple[str, str]
    source_advice_id: str

    def __post_init__(self) -> None:
        if len(set(self.selected_three)) != 3:
            raise ValueError("selected three must be unique")
        if self.lead not in self.selected_three:
            raise ValueError("lead must be selected")
        expected_backline = tuple(name for name in self.selected_three if name != self.lead)
        if set(self.backline) != set(expected_backline):
            raise ValueError("backline must be the other selected Pokemon")


@dataclass(frozen=True, slots=True)
class SemanticCorrection:
    correction_id: str
    field_path: str
    before: str
    after: str
    reason: str


@dataclass(frozen=True, slots=True)
class BoardReviewDraft:
    self_hp: CanonicalFact[HpBucket]
    opponent_hp: CanonicalFact[HpBucket]
    self_status: CanonicalFact[str]
    opponent_status: CanonicalFact[str]
    self_stages: CanonicalFact[StatStages]
    opponent_stages: CanonicalFact[StatStages]
    weather: CanonicalFact[str]
    terrain: CanonicalFact[str]
    self_side_effects: CanonicalFact[tuple[str, ...]]
    opponent_side_effects: CanonicalFact[tuple[str, ...]]
    needs_human_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class ReviewedBoardSnapshot:
    reviewed_board_id: str
    turn_id: str
    self_active: str
    opponent_active: str
    self_hp: HpBucket
    opponent_hp: HpBucket
    self_status: str
    opponent_status: str
    self_stages: StatStages = field(default_factory=StatStages)
    opponent_stages: StatStages = field(default_factory=StatStages)
    weather: str = "UNKNOWN"
    terrain: str = "UNKNOWN"
    self_side_effects: tuple[str, ...] = ()
    opponent_side_effects: tuple[str, ...] = ()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "reviewed_board_id": self.reviewed_board_id,
            "turn_id": self.turn_id,
            "self_active": self.self_active,
            "opponent_active": self.opponent_active,
            "self_hp": self.self_hp.value,
            "opponent_hp": self.opponent_hp.value,
            "self_status": self.self_status,
            "opponent_status": self.opponent_status,
            "self_stages": self.self_stages.to_canonical_dict(),
            "opponent_stages": self.opponent_stages.to_canonical_dict(),
            "weather": self.weather,
            "terrain": self.terrain,
            "self_side_effects": list(self.self_side_effects),
            "opponent_side_effects": list(self.opponent_side_effects),
        }


@dataclass(slots=True)
class BattleSession:
    session_id: str
    match_id: str
    generation: int
    state: BattleState
    battle_revision: int
    metadata_revision: int = 0
    current_reviewed_selection_id: str | None = None
    current_selection_advice_id: str | None = None
    current_applied_selection_id: str | None = None
    current_turn_id: str | None = None
    current_observation_id: str | None = None
    current_reviewed_board_id: str | None = None
    current_turn_advice_id: str | None = None
    active_slot: int | None = 1

    def bump_battle(self) -> None:
        self.battle_revision += 1

    def bump_metadata(self) -> None:
        self.metadata_revision += 1
