"""Selection ROI UI assisted-input and feedback behavior."""

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
from maple_next.selection_roi.contracts import SelectionMatchBundle
from maple_next.selection_roi.input_policy import SelectionInputOrigin
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


def _frame(image: QImage, *, frame_id: str = "selection-ui-frame") -> FramePacket:
    return FramePacket(
        frame_id=frame_id,
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


def _install_current_bundle(
    window: SelectionRoiMatchFlowWindow,
    bundle: SelectionMatchBundle,
) -> None:
    assert bundle.frame_id is not None
    current = window._controller.refresh()  # noqa: SLF001
    identity = window._selection_identity(current)  # noqa: SLF001
    window._selection_roi_submitted_identities[bundle.frame_id] = identity  # noqa: SLF001
    window._on_selection_roi_result(bundle)  # noqa: SLF001


def test_high_confidence_candidates_auto_fill_once_and_human_changes_lock(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)

    assert tuple(field.text() for field in window.opponent_team_inputs) == OPPONENT_TEAM
    assert {
        state.origin for state in window._selection_roi_input_states.values()  # noqa: SLF001
    } == {SelectionInputOrigin.OCR_AUTO}

    window.opponent_team_inputs[0].setText("ManualOverride")
    window._on_opponent_text_edited(1, "ManualOverride")  # noqa: SLF001
    match = window._selection_roi_slot_matches[1]  # noqa: SLF001
    window._auto_fill_selection_roi_slot(1, match)  # noqa: SLF001
    assert window.opponent_team_inputs[0].text() == "ManualOverride"
    assert (
        window._selection_roi_input_states[1].origin  # noqa: SLF001
        is SelectionInputOrigin.MANUAL_TEXT
    )

    window._apply_candidate_chip(1, 0)  # noqa: SLF001
    assert window.opponent_team_inputs[0].text() == "Alpha"
    assert (
        window._selection_roi_input_states[1].origin  # noqa: SLF001
        is SelectionInputOrigin.CANDIDATE_CLICK
    )

    window.close()
    repository.close()


def test_candidate_chips_are_visible_and_confirm_button_is_not_supported_ui(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)

    assert not window.confirm_facts_button.isVisible()
    assert all(
        window._selection_roi_candidate_buttons[slot][0].isVisible()  # noqa: SLF001
        for slot in range(1, 7)
    )
    assert all(
        "参照" in window._selection_roi_candidate_buttons[slot][0].text()  # noqa: SLF001
        for slot in range(1, 7)
    )

    window.close()
    repository.close()


def test_feedback_is_written_after_successful_compatibility_confirm(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert not feedback_path.exists()

    window.confirm_facts_button.click()

    assert feedback_path.exists()
    rows = [json.loads(line) for line in feedback_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 6
    assert {row["schema_version"] for row in rows} == {
        "maple-selection-roi-feedback.v2"
    }
    assert {row["value_origin"] for row in rows} == {"ocr_auto"}
    assert {row["trust_state"] for row in rows} == {"PROVISIONAL"}
    reviewed_selection_id = (  # noqa: SLF001
        window._controller.refresh().projection.current_reviewed_selection_id
    )
    assert reviewed_selection_id is not None

    window.close()
    repository.close()


def test_candidate_click_is_stored_as_trusted_feedback(tmp_path: Path) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)

    for slot in range(1, 7):
        window._apply_candidate_chip(slot, 0)  # noqa: SLF001
    window.confirm_facts_button.click()

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    rows = [json.loads(line) for line in feedback_path.read_text(encoding="utf-8").splitlines()]
    assert {row["value_origin"] for row in rows} == {"candidate_click"}
    assert {row["trust_state"] for row in rows} == {"TRUSTED"}

    window.close()
    repository.close()


def test_failed_confirmation_adds_no_feedback(tmp_path: Path) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)
    for field in window.opponent_team_inputs:
        field.setText("Duplicate")

    window.confirm_facts_button.click()

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert not feedback_path.exists()
    reviewed_selection_id = (  # noqa: SLF001
        window._controller.refresh().projection.current_reviewed_selection_id
    )
    assert reviewed_selection_id is None

    window.close()
    repository.close()


def test_stale_match_identity_never_labels_old_crops(tmp_path: Path) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)

    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)
    window._selection_roi_bundle_identity = (  # noqa: SLF001
        "old-session",
        "old-match",
        99,
    )

    window.confirm_facts_button.click()

    feedback_path = root / "selection" / "feedback" / "selection_labels.jsonl"
    assert not feedback_path.exists()
    reviewed_selection_id = (  # noqa: SLF001
        window._controller.refresh().projection.current_reviewed_selection_id
    )
    assert reviewed_selection_id is not None

    window.close()
    repository.close()


def test_send_handler_without_gemini_adapter_does_not_confirm_or_send(
    tmp_path: Path,
) -> None:
    repository, window, root = _build_window(tmp_path)
    image = _write_assets(root)
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    bundle = window._selection_roi_service.process_frame(_frame(image))  # noqa: SLF001
    _install_current_bundle(window, bundle)

    window._on_send_current_selection_to_gemini()  # noqa: SLF001

    current = window._controller.refresh()  # noqa: SLF001
    assert current.projection.current_reviewed_selection_id is None
    assert window._controller.network_call_count == 0  # noqa: SLF001

    window.close()
    repository.close()
