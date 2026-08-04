"""Selection ROI UI remains candidate-only until human confirmation."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_roi_window import SelectionRoiMatchFlowWindow

SELF_TEAM = ("One", "Two", "Three", "Four", "Five", "Six")
OPPONENT_TEAM = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")
COLORS = ("#d33", "#3d3", "#33d", "#dd3", "#d3d", "#3dd")


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _write_assets(root: Path) -> QImage:
    config_path = root / "selection" / "config" / "roi_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    slots = [
        {
            "slot": index + 1,
            "x": 700,
            "y": 40 + index * 100,
            "width": 240,
            "height": 80,
        }
        for index in range(6)
    ]
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "maple-selection-roi.v1",
                "canonical_width": 1280,
                "canonical_height": 720,
                "source_provenance": "ui-test",
                "slots": slots,
            }
        ),
        encoding="utf-8",
    )
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#111"))
    painter = QPainter(image)
    try:
        for slot, color in zip(slots, COLORS, strict=True):
            painter.fillRect(
                slot["x"],
                slot["y"],
                slot["width"],
                slot["height"],
                QColor(color),
            )
            painter.fillRect(
                slot["x"] + 10,
                slot["y"] + 10,
                slot["slot"] * 12,
                12,
                QColor("#fff"),
            )
    finally:
        painter.end()
    for label, slot in zip(OPPONENT_TEAM, slots, strict=True):
        destination = root / "selection" / "reference" / "labeled" / label / "seed.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        crop = image.copy(
            slot["x"],
            slot["y"],
            slot["width"],
            slot["height"],
        )
        assert crop.save(str(destination), "PNG")
    return image


def _frame(image: QImage) -> FramePacket:
    return FramePacket(
        frame_id="selection-ui-frame",
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=1280,
        height=720,
        image=image,
        frame_kind=FrameKind.CANONICAL,
    )


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, SelectionRoiMatchFlowWindow, Path]:
    root = tmp_path / "data" / "ocr"
    qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = SelectionRoiMatchFlowWindow(
        controller,
        ocr_data_directory=root,
    )
    return repository, window, root


def test_candidates_never_auto_fill_and_human_buttons_control_adoption(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    window._on_selection_roi_result(bundle)  # noqa: SLF001

    assert [field.text() for field in window.opponent_team_inputs] == [""] * 6
    window.selection_roi_apply_all_button.click()
    assert tuple(field.text() for field in window.opponent_team_inputs) == OPPONENT_TEAM

    window.opponent_team_inputs[0].setText("ManualOverride")
    window.selection_roi_apply_all_button.click()
    assert window.opponent_team_inputs[0].text() == "ManualOverride"

    window._apply_selection_roi_slot(1)  # noqa: SLF001 - explicit human-slot path
    assert window.opponent_team_inputs[0].text() == "Alpha"

    window.close()
    repository.close()


def test_feedback_is_written_only_after_successful_existing_confirm_button(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    window._on_selection_roi_result(bundle)  # noqa: SLF001
    window.selection_roi_apply_all_button.click()

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert not feedback_path.exists()

    window.confirm_facts_button.click()

    assert feedback_path.exists()
    assert len(feedback_path.read_text(encoding="utf-8").splitlines()) == 6
    assert window._controller.refresh().projection.current_reviewed_selection_id is not None  # noqa: SLF001

    window.close()
    repository.close()


def test_failed_confirmation_adds_no_feedback(tmp_path: Path) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    window._on_selection_roi_result(bundle)  # noqa: SLF001
    for field in window.opponent_team_inputs:
        field.setText("Duplicate")

    window.confirm_facts_button.click()

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert not feedback_path.exists()
    assert window._controller.refresh().projection.current_reviewed_selection_id is None  # noqa: SLF001

    window.close()
    repository.close()
