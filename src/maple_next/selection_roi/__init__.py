"""Human-confirmed opponent-team ROI image matching."""

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
    StoredSelectionObservation,
)
from maple_next.selection_roi.worker import LatestOnlySelectionRoiWorker

__all__ = [
    "FeedbackStoreResult",
    "ImageFingerprint",
    "LatestOnlySelectionRoiWorker",
    "ROI_CONFIG_SCHEMA",
    "ReferenceImageIndex",
    "SELECTION_SLOT_COUNT",
    "SelectionCandidateScore",
    "SelectionMatchBundle",
    "SelectionRoiConfig",
    "SelectionRoiCrop",
    "SelectionRoiError",
    "SelectionRoiPaths",
    "SelectionRoiRect",
    "SelectionRoiService",
    "SelectionSlotMatch",
    "StoredSelectionObservation",
    "UNKNOWN_LABEL",
    "assign_unique_team_candidates",
    "fingerprint_similarity",
    "match_selection_crops",
]
