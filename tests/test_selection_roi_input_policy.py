"""Pure tests for Selection ROI assisted-input policy."""

from __future__ import annotations

from PySide6.QtGui import QImage

from maple_next.selection_roi.contracts import (
    UNKNOWN_LABEL,
    SelectionCandidateScore,
    SelectionSlotMatch,
)
from maple_next.selection_roi.input_policy import (
    AUTO_FILL_THRESHOLD,
    CANDIDATE_BUTTON_THRESHOLD,
    SelectionInputOrigin,
    SelectionSlotInputState,
    should_auto_fill,
    visible_candidates,
)


def _match(*, assigned_label: str, assigned_score: float) -> SelectionSlotMatch:
    return SelectionSlotMatch(
        slot=1,
        crop=QImage(2, 2, QImage.Format.Format_RGB32),
        assigned_label=assigned_label,
        assigned_score=assigned_score,
        top_candidates=(
            SelectionCandidateScore("Alpha", 0.91, 8),
            SelectionCandidateScore("Bravo", 0.72, 4),
            SelectionCandidateScore("Charlie", 0.59, 2),
        ),
    )


def test_auto_fill_requires_empty_unlocked_field_and_score_at_least_point_80() -> None:
    empty = SelectionSlotInputState()
    assert should_auto_fill(
        empty,
        _match(assigned_label="Alpha", assigned_score=AUTO_FILL_THRESHOLD),
    )
    assert not should_auto_fill(
        empty,
        _match(assigned_label="Alpha", assigned_score=AUTO_FILL_THRESHOLD - 0.001),
    )
    assert not should_auto_fill(
        SelectionSlotInputState(
            value="Manual",
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        ),
        _match(assigned_label="Alpha", assigned_score=1.0),
    )
    assert not should_auto_fill(
        SelectionSlotInputState(
            value="",
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        ),
        _match(assigned_label="Alpha", assigned_score=1.0),
    )
    assert not should_auto_fill(
        empty,
        _match(assigned_label=UNKNOWN_LABEL, assigned_score=1.0),
    )


def test_candidate_chips_show_only_top_three_at_or_above_point_60() -> None:
    candidates = visible_candidates(
        _match(assigned_label="Alpha", assigned_score=0.91)
    )
    assert candidates == (
        ("Alpha", 0.91, 8),
        ("Bravo", 0.72, 4),
    )
    assert all(score >= CANDIDATE_BUTTON_THRESHOLD for _label, score, _count in candidates)
