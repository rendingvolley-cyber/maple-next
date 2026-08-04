"""Pure domain types for Maple Next."""

from maple_next.domain.enums import (
    BattleState,
    HpBucket,
    JobStatus,
    JobType,
    ResultDisposition,
)
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BoardReviewDraft,
    CanonicalFact,
    ReviewedBoardSnapshot,
    SelectionFacts,
    SelfTeamPreset,
    SemanticCorrection,
    StatStages,
)
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    CHAMPIONS_STAT_POINT_PER_STAT_MAX,
    CHAMPIONS_STAT_POINT_TOTAL_MAX,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)

__all__ = [
    "AppliedSelectionSnapshot",
    "BattleSession",
    "BattleState",
    "BoardReviewDraft",
    "CanonicalFact",
    "HpBucket",
    "JobStatus",
    "JobType",
    "ResultDisposition",
    "ReviewedBoardSnapshot",
    "SelectionFacts",
    "SelfTeamPreset",
    "SemanticCorrection",
    "StatStages",
    "CHAMPIONS_BATTLE_FORMAT",
    "CHAMPIONS_GAME",
    "CHAMPIONS_SCHEMA_VERSION",
    "CHAMPIONS_STAT_POINT_PER_STAT_MAX",
    "CHAMPIONS_STAT_POINT_TOTAL_MAX",
    "ChampionsPokemonBuild",
    "ChampionsStatPoints",
    "ChampionsTeamBuild",
]
