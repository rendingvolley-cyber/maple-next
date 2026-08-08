"""Pure policy for Selection ROI assisted input.

The matcher proposes values; this module decides only how those proposals may
populate editable UI fields. Canonical state and provider sends remain explicit
human actions owned by the window/controller layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from maple_next.selection_roi.contracts import UNKNOWN_LABEL, SelectionSlotMatch

AUTO_FILL_THRESHOLD: Final[float] = 0.80
CANDIDATE_BUTTON_THRESHOLD: Final[float] = 0.60
CANDIDATE_BUTTON_COUNT: Final[int] = 3


class SelectionInputOrigin(StrEnum):
    """How the current editable opponent-name value was produced."""

    EMPTY = "empty"
    OCR_AUTO = "ocr_auto"
    CANDIDATE_CLICK = "candidate_click"
    MANUAL_TEXT = "manual_text"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class SelectionSlotInputState:
    """UI-local state; never a canonical Selection snapshot by itself."""

    value: str = ""
    origin: SelectionInputOrigin = SelectionInputOrigin.EMPTY
    user_locked: bool = False


def should_auto_fill(
    state: SelectionSlotInputState,
    match: SelectionSlotMatch | None,
    *,
    threshold: float = AUTO_FILL_THRESHOLD,
) -> bool:
    """Allow one initial high-confidence fill, never an overwrite."""

    if state.value.strip() or state.user_locked or match is None:
        return False
    return (
        match.assigned_label != UNKNOWN_LABEL
        and match.assigned_score >= threshold
    )


def visible_candidates(
    match: SelectionSlotMatch | None,
    *,
    threshold: float = CANDIDATE_BUTTON_THRESHOLD,
    limit: int = CANDIDATE_BUTTON_COUNT,
) -> tuple[tuple[str, float, int], ...]:
    """Return candidate-chip data without adopting any value."""

    if match is None or limit <= 0:
        return ()
    return tuple(
        (candidate.label, candidate.score, candidate.reference_count)
        for candidate in match.top_candidates
        if candidate.score >= threshold
    )[:limit]
