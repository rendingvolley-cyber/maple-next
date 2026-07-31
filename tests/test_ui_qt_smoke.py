from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
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
ADVICE_THREE = ("Meowscarada", "Gholdengo", "Dragonite")
HUMAN_THREE = ("Dondozo", "Flutter Mane", "Urshifu")


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def test_pyside6_window_supports_manual_selection_apply_flow(tmp_path: Path) -> None:
    qapp = qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
    )
    window = MapleMainWindow(controller)

    assert window.application_mode_label.text() == "NO_ACTIVE_MATCH"
    assert window.primary_cta_label.text() == "NEW MATCH"
    window.new_match_button.click()
    qapp.processEvents()
    assert repository.count_sessions() == 1

    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    qapp.processEvents()
    assert window.primary_cta_label.text() == "MOCK Selection Adviceを投入"

    for box, value in zip(window.mock_selection_boxes, ADVICE_THREE, strict=True):
        box.setCurrentText(value)
    window.mock_lead_box.setCurrentText("Meowscarada")
    window.mock_submit_button.click()
    qapp.processEvents()
    assert window.advice_three_label.text() == " / ".join(ADVICE_THREE)
    assert all(not checkbox.isChecked() for checkbox in window.actual_checkboxes)

    for checkbox in window.actual_checkboxes:
        checkbox.setChecked(checkbox.text() in HUMAN_THREE)
    window.actual_lead_box.setCurrentText("Flutter Mane")
    window.apply_confirm_checkbox.setChecked(True)
    qapp.processEvents()
    assert window.apply_button.isEnabled()
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert window.session_state_label.text() == "BATTLE_READY"
    assert window.primary_cta_label.text() == "START TURN CAPTURE"
    assert window.actual_three_label.text() == " / ".join(HUMAN_THREE)
    assert window.actual_lead_label.text() == "Flutter Mane"
    assert window.actual_backline_label.text() == "Dondozo / Urshifu"
    assert controller.network_call_count == 0
    window.close()
    repository.close()


def test_header_has_exactly_two_tabs_and_battle_record_uses_three_columns(
    tmp_path: Path,
) -> None:
    qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
    )
    window = MapleMainWindow(controller)

    assert window.header_tabs.count() == 2
    assert window.header_tabs.tabText(0) == "選出"
    assert window.header_tabs.tabText(1) == "バトルレコード"

    assert window.capture_status_group is not None
    assert "manual" in window.capture_status_label.text().lower()

    window.set_capture_status(available=True, detail="capture OK")
    assert window.capture_status_label.text() == "capture OK"
    window.set_capture_status(available=False)
    assert "manual" in window.capture_status_label.text().lower()

    window.close()
    repository.close()
