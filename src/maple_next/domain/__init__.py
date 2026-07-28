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
    SemanticCorrection,
    StatStages,
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
    "SemanticCorrection",
    "StatStages",
]
