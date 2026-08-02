"""Issue #31 Lane B: manual-only full-turn proof with capture/OCR unavailable.

This test does not modify the existing domain/controller/UI flow at all - it
only proves that when the new capture/ocr packages report unavailable, the
existing manual turn flow (already covered by test_ui_turn_qt_smoke.py)
still completes end to end with zero capture/OCR exceptions, zero network
calls, zero provider sends, and zero automatic game actions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.capture.contracts import CaptureErrorCode, CaptureStatusCode, DeviceOpenResult
from maple_next.capture.service import CaptureService
from maple_next.domain.enums import ActionType, HpBucket
from maple_next.ocr.contracts import OcrBundleStatus, OcrCandidateContext
from maple_next.ocr.service import OcrCandidateService, UnavailableOcrCandidateBackend
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.window import MapleMainWindow

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)


class UnavailableVideoCaptureBackend:
    """Always reports device unavailable, never raises."""

    def start(self, selector: str, on_frame: object | None = None) -> DeviceOpenResult:
        return DeviceOpenResult(
            opened=False,
            device_found=False,
            device_label=None,
            error_code=CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE,
        )

    def stop(self) -> None:
        pass

    def get_latest_frame(self) -> object | None:
        return None

    def is_running(self) -> bool:
        return False


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def test_manual_only_full_turn_with_capture_and_ocr_unavailable(tmp_path: Path) -> None:
    qapp = qt_application()

    # Independent, unavailable capture + OCR stack driven alongside the
    # existing manual UI flow - proves neither blocks nor is required.
    capture_service = CaptureService(UnavailableVideoCaptureBackend())
    ocr_service = OcrCandidateService(UnavailableOcrCandidateBackend())

    capture_status = capture_service.start()
    assert capture_status.status == CaptureStatusCode.DEVICE_UNAVAILABLE
    assert capture_status.manual_entry_allowed is True

    ocr_bundle = ocr_service.request_candidates(
        frame=capture_service.latest_frame(),
        frame_age_ms=capture_status.age_ms,
        fresh=capture_status.fresh,
        context=OcrCandidateContext(),
    )
    assert ocr_bundle.status in {OcrBundleStatus.FRAME_UNAVAILABLE, OcrBundleStatus.OCR_UNAVAILABLE}
    assert ocr_bundle.manual_entry_allowed is True
    assert ocr_bundle.candidate_only is True

    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = MapleMainWindow(controller)

    # NEW MATCH -> Selection facts confirm.
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()

    # MOCK Selection Advice -> human APPLY -> BATTLE_READY.
    for box, value in zip(
        window.mock_selection_boxes,
        ("Meowscarada", "Gholdengo", "Dragonite"),
        strict=True,
    ):
        box.setCurrentText(value)
    window.mock_lead_box.setCurrentText("Meowscarada")
    window.mock_submit_button.click()
    for checkbox in window.actual_checkboxes:
        checkbox.setChecked(checkbox.text() in {"Dondozo", "Flutter Mane", "Urshifu"})
    window.actual_lead_box.setCurrentText("Dondozo")
    window.apply_confirm_checkbox.setChecked(True)
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert window.session_state_label.text() == "BATTLE_READY"

    # START TURN CAPTURE -> capture/OCR unavailable (verified above) ->
    # TURN_CAPTURE_PENDING via the existing domain flow.
    window.start_turn_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_CAPTURE_PENDING"

    # Re-check capture/OCR unavailable status again mid-turn - still safe,
    # still manual.
    capture_status = capture_service.latest_status()
    assert capture_status.status == CaptureStatusCode.DEVICE_UNAVAILABLE
    assert capture_status.manual_entry_allowed is True

    # Manual entry of self active/opponent active/HP buckets/legal actions.
    window.self_active_box.setCurrentText("Dondozo")
    window.opponent_active_input.setText("Garchomp")
    window.self_hp_box.setCurrentText(HpBucket.FULL.value)
    window.opponent_hp_box.setCurrentText(HpBucket.EIGHTY_ONE_TO_NINETY.value)
    window.move_inputs[0].setText("Protect")
    window.move_inputs[1].setText("Wave Crash")
    for checkbox in window.switch_checkboxes:
        checkbox.setChecked(checkbox.text() in {"Flutter Mane", "Urshifu"})
    window.turn_facts_confirm_checkbox.setChecked(True)
    window.confirm_turn_facts_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_REVIEWED"

    # MOCK Turn Advice.
    window.mock_turn_action_type_box.setCurrentText(ActionType.MOVE.value)
    window.mock_turn_action_name_box.setCurrentText("Protect")
    window.mock_turn_prediction_input.setText("Earthquake")
    window.mock_turn_rationale_input.setText("Scout first")
    window.mock_turn_submit_button.click()
    qapp.processEvents()
    assert window.turn_advice_action_label.text() == "MOVE: Protect"

    # human records executed action -> TURN_RECORDED.
    window.actual_action_type_box.setCurrentText(ActionType.MOVE.value)
    window.actual_action_name_box.setCurrentText("Wave Crash")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_action_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_RECORDED"

    # NEXT TURN.
    window.next_turn_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_CAPTURE_PENDING"
    assert window.turn_number_label.text() == "2"

    assert controller.network_call_count == 0

    window.close()
    repository.close()
    capture_service.stop()


def test_capture_and_ocr_services_have_no_domain_repository_or_provider_hooks() -> None:
    capture_service = CaptureService(UnavailableVideoCaptureBackend())
    ocr_service = OcrCandidateService(UnavailableOcrCandidateBackend())
    forbidden_attrs = (
        "start_turn_capture",
        "confirm_turn_facts",
        "repository",
        "provider",
        "send",
        "record_actual_action",
    )
    for attr in forbidden_attrs:
        assert not hasattr(capture_service, attr)
        assert not hasattr(ocr_service, attr)
