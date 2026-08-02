from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import ActionType, HpBucket
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


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def test_window_supports_one_manual_turn_and_action_history(tmp_path: Path) -> None:
    qapp = qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = MapleMainWindow(controller)

    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
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

    window.start_turn_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_CAPTURE_PENDING"
    assert window.turn_number_label.text() == "1"

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

    window.mock_turn_action_type_box.setCurrentText(ActionType.MOVE.value)
    window.mock_turn_action_name_box.setCurrentText("Protect")
    window.mock_turn_prediction_input.setText("Earthquake")
    window.mock_turn_rationale_input.setText("Scout first")
    window.mock_turn_submit_button.click()
    qapp.processEvents()
    assert window.turn_advice_action_label.text() == "MOVE: Protect"

    window.actual_action_type_box.setCurrentText(ActionType.MOVE.value)
    window.actual_action_name_box.setCurrentText("Wave Crash")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_action_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_RECORDED"
    assert window.action_history_label.text() == "Turn 1: 自分=MOVE Wave Crash / 順序=UNKNOWN"
    assert controller.network_call_count == 0

    window.next_turn_button.click()
    qapp.processEvents()
    assert window.session_state_label.text() == "TURN_CAPTURE_PENDING"
    assert window.turn_number_label.text() == "2"
    window.close()
    repository.close()
