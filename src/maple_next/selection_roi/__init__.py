"""Opponent-team Selection ROI matching and assisted input."""

from maple_next.selection_roi.contracts import (
    ROI_CONFIG_SCHEMA,
    SELECTION_SLOT_COUNT,
    UNKNOWN_LABEL,
    SelectionCandidateScore,
    SelectionMatchBundle,
    SelectionRoiConfig,
    SelectionRoiCrop,
    SelectionRoiError,
    SelectionRoiRect,
    SelectionSlotMatch,
)
from maple_next.selection_roi.input_policy import (
    AUTO_FILL_THRESHOLD,
    CANDIDATE_BUTTON_COUNT,
    CANDIDATE_BUTTON_THRESHOLD,
    SelectionInputOrigin,
    SelectionSlotInputState,
    should_auto_fill,
    visible_candidates,
)
from maple_next.selection_roi.matcher import (
    ImageFingerprint,
    ReferenceImageIndex,
    assign_unique_team_candidates,
    fingerprint_similarity,
    match_selection_crops,
)
from maple_next.selection_roi.service import (
    FeedbackStoreResult,
    SelectionRoiPaths,
    SelectionRoiService,
    SelectionSlotFeedback,
    StoredSelectionObservation,
)
from maple_next.selection_roi.worker import LatestOnlySelectionRoiWorker

__all__ = [
    "AUTO_FILL_THRESHOLD",
    "CANDIDATE_BUTTON_COUNT",
    "CANDIDATE_BUTTON_THRESHOLD",
    "FeedbackStoreResult",
    "ImageFingerprint",
    "LatestOnlySelectionRoiWorker",
    "ROI_CONFIG_SCHEMA",
    "ReferenceImageIndex",
    "SELECTION_SLOT_COUNT",
    "SelectionCandidateScore",
    "SelectionInputOrigin",
    "SelectionMatchBundle",
    "SelectionRoiConfig",
    "SelectionRoiCrop",
    "SelectionRoiError",
    "SelectionRoiPaths",
    "SelectionRoiRect",
    "SelectionRoiService",
    "SelectionSlotFeedback",
    "SelectionSlotInputState",
    "SelectionSlotMatch",
    "StoredSelectionObservation",
    "UNKNOWN_LABEL",
    "assign_unique_team_candidates",
    "fingerprint_similarity",
    "match_selection_crops",
    "should_auto_fill",
    "visible_candidates",
]
