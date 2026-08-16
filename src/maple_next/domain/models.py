"""Pure canonical domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from maple_next.domain.enums import ActionOrder, ActionType, BattleState, HpBucket
from maple_next.domain.team_build import ChampionsTeamBuild

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
    self_team_build: ChampionsTeamBuild | None = None
    self_team_build_sha256: str | None = None

    def __post_init__(self) -> None:
        if len(self.self_team) != 6 or len(set(self.self_team)) != 6:
            raise ValueError("self team must contain six unique names")
        if len(self.opponent_team) != 6 or len(set(self.opponent_team)) != 6:
            raise ValueError("opponent team must contain six unique names")
        if self.self_team_build is None:
            if self.self_team_build_sha256 is not None:
                raise ValueError("names-only selection facts must not have a build hash")
        else:
            if self.self_team_build.pokemon_names != tuple(self.self_team):
                raise ValueError("self team build names must match selection facts")
            expected_hash = self.self_team_build.sha256()
            if self.self_team_build_sha256 != expected_hash:
                raise ValueError("self team build hash does not match canonical build")

    def to_canonical_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reviewed_selection_id": self.reviewed_selection_id,
            "self_team": list(self.self_team),
            "opponent_team": list(self.opponent_team),
        }
        if self.self_team_build is not None:
            payload["self_team_build"] = self.self_team_build.to_canonical_dict()
            payload["self_team_build_sha256"] = self.self_team_build_sha256
        return payload


@dataclass(frozen=True, slots=True)
class SelfTeamPreset:
    """Named reusable six-member team, separate from immutable match facts."""

    preset_id: str
    name: str
    self_team: tuple[str, str, str, str, str, str]
    created_at_utc: str
    updated_at_utc: str
    build_schema_version: str = "maple-team.v1"
    team_build: ChampionsTeamBuild | None = None
    team_build_sha256: str | None = None

    @property
    def status(self) -> str:
        return "DETAILED" if self.team_build is not None else "NAMES_ONLY"

    @property
    def detail_status(self) -> str:
        return self.status

    def to_canonical_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.build_schema_version,
            "name": self.name,
            "pokemon": list(self.self_team),
        }
        if self.team_build is not None:
            payload["game"] = self.team_build.game
            payload["battle_format"] = self.team_build.battle_format
            payload["members"] = [
                member.to_canonical_dict() for member in self.team_build.members
            ]
            payload["team_build_sha256"] = self.team_build_sha256
        return payload

    def __post_init__(self) -> None:
        if (
            len(self.self_team) != 6
            or len(set(self.self_team)) != 6
            or any(not isinstance(name, str) or not name.strip() for name in self.self_team)
        ):
            raise ValueError("preset team must contain six unique names")
        if self.team_build is None:
            if self.build_schema_version != "maple-team.v1":
                raise ValueError("names-only preset must use maple-team.v1")
            if self.team_build_sha256 is not None:
                raise ValueError("names-only preset must not have a build hash")
            return
        if self.build_schema_version != "maple-team.v2":
            raise ValueError("detailed preset must use maple-team.v2")
        if self.team_build.pokemon_names != tuple(self.self_team):
            raise ValueError("preset names and build members must match")
        if self.team_build_sha256 != self.team_build.sha256():
            raise ValueError("preset build hash does not match canonical build")


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
class BattleTurn:
    turn_id: str
    turn_number: int

    def __post_init__(self) -> None:
        if self.turn_number < 1:
            raise ValueError("turn number must be positive")


@dataclass(frozen=True, slots=True)
class TurnFactsSnapshot:
    turn_facts_id: str
    turn_id: str
    turn_number: int
    self_active: str
    opponent_active: str
    self_hp: HpBucket
    opponent_hp: HpBucket
    legal_moves: tuple[str, ...]
    legal_switches: tuple[str, ...]
    human_note: str = ""
    previous_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if self.turn_number < 1:
            raise ValueError("turn number must be positive")
        if not self.self_active.strip() or not self.opponent_active.strip():
            raise ValueError("active names must be explicit")
        if not 1 <= len(self.legal_moves) <= 4:
            raise ValueError("legal moves must contain one to four names")
        if any(not name.strip() for name in self.legal_moves):
            raise ValueError("legal moves cannot contain blanks")
        if len(set(self.legal_moves)) != len(self.legal_moves):
            raise ValueError("legal moves must be unique")
        if any(not name.strip() for name in self.legal_switches):
            raise ValueError("legal switches cannot contain blanks")
        if len(set(self.legal_switches)) != len(self.legal_switches):
            raise ValueError("legal switches must be unique")
        if self.self_active in self.legal_switches:
            raise ValueError("self active cannot be a switch candidate")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "turn_facts_id": self.turn_facts_id,
            "turn_id": self.turn_id,
            "turn_number": self.turn_number,
            "self_active": self.self_active,
            "opponent_active": self.opponent_active,
            "self_hp": self.self_hp.value,
            "opponent_hp": self.opponent_hp.value,
            "legal_moves": list(self.legal_moves),
            "legal_switches": list(self.legal_switches),
            "human_note": self.human_note,
            "previous_snapshot_id": self.previous_snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class TurnAdviceSnapshot:
    turn_advice_id: str
    turn_id: str
    turn_number: int
    job_id: str
    input_snapshot_id: str
    action_type: ActionType
    action_name: str
    opponent_prediction: str
    rationale: str
    is_mock: bool = True
    source_type: str = "MOCK"
    model: str = "mock-dev"
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.turn_number < 1:
            raise ValueError("turn number must be positive")
        if not self.action_name.strip():
            raise ValueError("advice action must be explicit")
        if not self.opponent_prediction.strip():
            raise ValueError("opponent prediction must be explicit")
        if not self.rationale.strip():
            raise ValueError("advice rationale must be explicit")
        if not self.source_type.strip():
            raise ValueError("advice source_type must be explicit")


@dataclass(frozen=True, slots=True)
class RecordedAction:
    action_id: str
    turn_id: str
    turn_number: int
    action_type: ActionType
    action_name: str
    opponent_action_type: ActionType | None = None
    opponent_action_name: str | None = None
    action_order: ActionOrder = ActionOrder.UNKNOWN

    def __post_init__(self) -> None:
        if self.turn_number < 1:
            raise ValueError("turn number must be positive")
        if not self.action_name.strip():
            raise ValueError("recorded action must be explicit")
        if self.opponent_action_type is None and self.opponent_action_name is not None:
            raise ValueError("unknown opponent action name must be None")
        if self.opponent_action_type is not None and (
            self.opponent_action_name is None or not self.opponent_action_name.strip()
        ):
            raise ValueError("opponent action name must be explicit when type is provided")


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
    #: Bundle 4 (Gemini V2): the immutable official Champions rules snapshot
    #: pinned to this match at creation time. All four are set together (or
    #: all left ``None`` for a legacy/pre-Bundle-4 match) -- never
    #: individually, and never updated after ``insert_session``. See
    #: ``domain/champions_rules.py`` for the pin/resolve contract.
    rules_ruleset_id: str | None = None
    rules_ruleset_version: str | None = None
    rules_snapshot_id: str | None = None
    rules_facts_sha256: str | None = None

    def bump_battle(self) -> None:
        self.battle_revision += 1

    def bump_metadata(self) -> None:
        self.metadata_revision += 1
