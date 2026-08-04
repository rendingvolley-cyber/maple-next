"""Focused coverage for the opponent-team ROI image matcher."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter

from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.selection_roi.contracts import (
    SelectionCandidateScore,
    SelectionRoiConfig,
    SelectionRoiCrop,
    SelectionRoiError,
    SelectionRoiRect,
    UNKNOWN_LABEL,
)
from maple_next.selection_roi.matcher import (
    ReferenceImageIndex,
    assign_unique_team_candidates,
    match_selection_crops,
)
from maple_next.selection_roi.service import SelectionRoiService


ROI_RECTS = tuple(
    SelectionRoiRect(slot=index + 1, x=700, y=40 + index * 100, width=240, height=80)
    for index in range(6)
)
LABELS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")
COLORS = (
    QColor("#d33"),
    QColor("#3d3"),
    QColor("#33d"),
    QColor("#dd3"),
    QColor("#d3d"),
    QColor("#3dd"),
)


def _write_config(root: Path) -> None:
    path = root / "selection" / "config" / "roi_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "maple-selection-roi.v1",
                "canonical_width": 1280,
                "canonical_height": 720,
                "source_provenance": "synthetic-test",
                "slots": [
                    {
                        "slot": rect.slot,
                        "x": rect.x,
                        "y": rect.y,
                        "width": rect.width,
                        "height": rect.height,
                    }
                    for rect in ROI_RECTS
                ],
            }
        ),
        encoding="utf-8",
    )


def _selection_image() -> QImage:
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#111"))
    painter = QPainter(image)
    try:
        for rect, color in zip(ROI_RECTS, COLORS, strict=True):
            painter.fillRect(rect.x, rect.y, rect.width, rect.height, color)
            painter.fillRect(rect.x + 10, rect.y + 10, rect.slot * 12, 12, QColor("#fff"))
    finally:
        painter.end()
    return image


def _frame(image: QImage | None = None, *, width: int = 1280, height: int = 720) -> FramePacket:
    source = image if image is not None else _selection_image()
    return FramePacket(
        frame_id="selection-frame-1",
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=width,
        height=height,
        image=source,
        frame_kind=FrameKind.CANONICAL,
    )


def _seed_references(root: Path, image: QImage) -> None:
    config = SelectionRoiConfig(
        schema_version="maple-selection-roi.v1",
        canonical_width=1280,
        canonical_height=720,
        slots=ROI_RECTS,
        source_provenance="synthetic-test",
    )
    for label, rect in zip(LABELS, config.slots, strict=True):
        destination = root / "selection" / "reference" / "labeled" / label / "seed.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        assert image.copy(rect.x, rect.y, rect.width, rect.height).save(
            str(destination),
            "PNG",
        )


def test_noncanonical_frame_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "data" / "ocr"
    _write_config(root)
    service = SelectionRoiService(root)

    bundle = service.process_frame(_frame(width=1920, height=1080))

    assert bundle.status == "FRAME_NOT_CANONICAL"
    assert bundle.slots == ()
    assert list((root / "selection" / "captures").glob("*")) == []


def test_config_requires_six_in_bounds_slots(tmp_path: Path) -> None:
    path = tmp_path / "roi_config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "maple-selection-roi.v1",
                "canonical_width": 1280,
                "canonical_height": 720,
                "source_provenance": "broken",
                "slots": [
                    {"slot": 1, "x": 1270, "y": 0, "width": 20, "height": 20}
                ]
                * 6,
            }
        ),
        encoding="utf-8",
    )

    try:
        SelectionRoiConfig.load(path)
    except SelectionRoiError:
        pass
    else:
        raise AssertionError("invalid config must fail closed")


def test_exact_references_match_all_six_unique_slots(tmp_path: Path) -> None:
    root = tmp_path / "data" / "ocr"
    image = _selection_image()
    _write_config(root)
    _seed_references(root, image)
    service = SelectionRoiService(root)

    bundle = service.process_frame(_frame(image))

    assert bundle.status == "CANDIDATES_READY"
    assert tuple(slot.assigned_label for slot in bundle.slots) == LABELS
    assert all(slot.assigned_score == 1.0 for slot in bundle.slots)
    assert bundle.observation_id is not None
    observation_dir = root / "selection" / "captures" / bundle.observation_id
    assert sorted(path.name for path in observation_dir.glob("slot_*.png")) == [
        f"slot_{index:02d}.png" for index in range(1, 7)
    ]


def test_low_similarity_remains_unknown(tmp_path: Path) -> None:
    root = tmp_path / "data" / "ocr"
    _write_config(root)
    unrelated = QImage(240, 80, QImage.Format.Format_RGB32)
    unrelated.fill(QColor("#fff"))
    destination = root / "selection" / "reference" / "labeled" / "Other" / "seed.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert unrelated.save(str(destination), "PNG")
    service = SelectionRoiService(root, threshold=0.99)

    bundle = service.process_frame(_frame())

    assert all(slot.assigned_label == UNKNOWN_LABEL for slot in bundle.slots)


def test_global_assignment_does_not_duplicate_a_label() -> None:
    dummy = QImage(10, 10, QImage.Format.Format_RGB32)
    dummy.fill(QColor("#111"))
    crops = tuple(
        SelectionRoiCrop(
            slot=index + 1,
            image=dummy,
            rect=SelectionRoiRect(
                slot=index + 1,
                x=index * 10,
                y=0,
                width=10,
                height=10,
            ),
        )
        for index in range(6)
    )
    candidate_lists = (
        (
            SelectionCandidateScore("Same", 0.99, 3),
            SelectionCandidateScore("Alpha", 0.90, 1),
        ),
        (
            SelectionCandidateScore("Same", 0.98, 3),
            SelectionCandidateScore("Bravo", 0.89, 1),
        ),
        (SelectionCandidateScore("Charlie", 0.88, 1),),
        (SelectionCandidateScore("Delta", 0.87, 1),),
        (SelectionCandidateScore("Echo", 0.86, 1),),
        (SelectionCandidateScore("Foxtrot", 0.85, 1),),
    )

    assigned = assign_unique_team_candidates(
        crops,
        cast(tuple[tuple[SelectionCandidateScore, ...], ...], candidate_lists),
        threshold=0.72,
    )

    labels = [slot.assigned_label for slot in assigned if slot.assigned_label != UNKNOWN_LABEL]
    assert len(labels) == len(set(labels))
    assert "Same" in labels


def test_human_confirmation_is_the_only_reference_add_boundary(tmp_path: Path) -> None:
    root = tmp_path / "data" / "ocr"
    _write_config(root)
    service = SelectionRoiService(root)
    bundle = service.process_frame(_frame())

    assert bundle.observation_id is not None
    assert list((root / "selection" / "reference" / "labeled").glob("*/*")) == []
    assert not (root / "selection" / "feedback" / "selection_labels.jsonl").exists()

    result = service.confirm_observation(
        observation_id=bundle.observation_id,
        opponent_names=LABELS,
        reviewed_selection_id="selection-reviewed-1",
    )

    assert result.added_count == 6
    assert result.conflict_count == 0
    assert len(list((root / "selection" / "reference" / "labeled").glob("*/*.png"))) == 6
    feedback = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert len(feedback.read_text(encoding="utf-8").splitlines()) == 6

    duplicate = service.confirm_observation(
        observation_id=bundle.observation_id,
        opponent_names=LABELS,
        reviewed_selection_id="selection-reviewed-2",
    )
    assert duplicate.added_count == 0
    assert duplicate.duplicate_count == 6


def test_reference_index_uses_multiple_examples_per_label(tmp_path: Path) -> None:
    root = tmp_path / "labeled"
    label_dir = root / "Alpha"
    label_dir.mkdir(parents=True)
    first = QImage(40, 40, QImage.Format.Format_RGB32)
    first.fill(QColor("#111"))
    second = QImage(40, 40, QImage.Format.Format_RGB32)
    second.fill(QColor("#ddd"))
    assert first.save(str(label_dir / "a.png"), "PNG")
    assert second.save(str(label_dir / "b.png"), "PNG")
    index = ReferenceImageIndex(root)
    crop = SelectionRoiCrop(
        slot=1,
        image=second,
        rect=SelectionRoiRect(slot=1, x=0, y=0, width=40, height=40),
    )

    matches = match_selection_crops(
        (
            crop,
            *tuple(
                SelectionRoiCrop(
                    slot=index_value,
                    image=first,
                    rect=SelectionRoiRect(
                        slot=index_value,
                        x=0,
                        y=0,
                        width=40,
                        height=40,
                    ),
                )
                for index_value in range(2, 7)
            ),
        ),
        index,
        threshold=0.72,
        top_k=3,
    )

    assert matches[0].top_candidates[0].label == "Alpha"
    assert matches[0].top_candidates[0].reference_count == 2
