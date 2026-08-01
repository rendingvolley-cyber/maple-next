"""Real-window trusted-click coverage for the fake/injected Turn Advice send.

Mirrors tests/test_gemini_selection_advice_window.py: a genuine QThread-backed
dispatch (no SyncDispatch stand-in here), driven by a real trusted mouse
click on the actual ``TrustedSendButton`` wired into the production window
class chain (``TurnAdviceIntegrationWindow``).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import SanitizedProviderResult
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
)
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL, GeminiTurnAdviceAdapter
from maple_next.ui.turn_advice_integration import (
    TurnAdviceIntegrationController,
    TurnAdviceIntegrationWindow,
)

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")
LEGAL_MOVES = ("Protect", "Wave Crash", "Earthquake")
LEGAL_SWITCHES = ("Flutter Mane", "Urshifu")


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def pump_until(qapp: QApplication, predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting for async Turn Advice result"
        qapp.processEvents()
        time.sleep(0.005)


def build_window(
    tmp_path: Path, transport: FakeTurnAdviceTransport
) -> tuple[SQLiteRepository, TurnAdviceIntegrationWindow]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    turn_gemini_adapter = GeminiTurnAdviceAdapter(transport)
    controller = TurnAdviceIntegrationController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=None,
        turn_gemini_adapter=turn_gemini_adapter,
    )
    window = TurnAdviceIntegrationWindow(controller)
    return repository, window


def advance_to_turn_reviewed(window: TurnAdviceIntegrationWindow) -> None:
    window.new_match_button.click()
    window.render_view()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    for index, box in enumerate(window.mock_selection_boxes):
        box.setCurrentText(("Meowscarada", "Gholdengo", "Dragonite")[index])
    window.mock_lead_box.setCurrentText("Meowscarada")
    window.mock_submit_button.click()
    window.render_view()

    for checkbox in window.actual_checkboxes:
        checkbox.setChecked(checkbox.text() in SELECTED_THREE)
    window.actual_lead_box.setCurrentText("Dondozo")
    window.apply_confirm_checkbox.setChecked(True)
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    window.render_view()

    window.start_turn_button.click()
    window.render_view()

    assert window.turn_number_input is not None
    window.turn_number_input.setText(str(window._controller.refresh().turn_number))  # noqa: SLF001
    window.self_active_box.setCurrentText("Dondozo")
    window.opponent_active_input.setText("Garchomp")
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    for field, value in zip(window.move_inputs, LEGAL_MOVES, strict=False):
        field.setText(value)
    for checkbox in window.switch_checkboxes:
        checkbox.setChecked(checkbox.text() in LEGAL_SWITCHES)
    window.turn_facts_confirm_checkbox.setChecked(True)
    window.confirm_turn_facts_button.click()
    window.render_view()

    view = window._controller.refresh()  # noqa: SLF001
    assert view.error_message is None
    assert view.projection.session_state == "TURN_REVIEWED"


def test_turn_gemini_group_visible_only_when_turn_facts_reviewed(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, window = build_window(tmp_path, transport)
    window.show()

    assert window.turn_gemini_box.isVisible() is False
    advance_to_turn_reviewed(window)
    assert window.turn_gemini_box.isVisible() is True
    assert window.turn_gemini_send_button.isEnabled() is True

    window.close()
    repository.close()


def test_trusted_click_sends_exactly_once_and_shows_result(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeTurnAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={
                    "recommended_action": {
                        "action_id": "MOVE:Wave Crash",
                        "action_type": "MOVE",
                        "action_name": "Wave Crash",
                    },
                    "reasons": ["STAB and pressure."],
                    "warnings": ["HP不明のためswitchも検討"],
                    "opponent_prediction": {
                        "category": "UNKNOWN",
                        "predicted_action": "Protect",
                        "summary": "Protect",
                        "confidence": 0.5,
                    },
                },
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=FAKE_TURN_MODEL,
            )
        ]
    )
    repository, window = build_window(tmp_path, transport)
    window.show()
    advance_to_turn_reviewed(window)

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Wave Crash")
    window.mock_turn_prediction_input.setText("Protect")
    window.mock_turn_rationale_input.setText("STAB and pressure.")

    assert window.turn_gemini_send_button.isEnabled()
    QTest.mouseClick(window.turn_gemini_send_button, Qt.MouseButton.LeftButton)
    assert window.turn_gemini_send_button.trusted_mouse_activation_count == 1

    pump_until(qapp, lambda: transport.call_count >= 1)
    pump_until(qapp, lambda: window._controller.refresh().turn_advice is not None)  # noqa: SLF001
    window.render_view()

    assert transport.call_count == 1
    view = window._controller.refresh()  # noqa: SLF001
    assert view.turn_advice is not None
    assert view.turn_advice.action_name == "Wave Crash"
    assert view.turn_advice.source_type == FAKE_TURN_ADVICE_SOURCE_TYPE
    assert view.turn_advice.model == FAKE_TURN_MODEL
    assert window.turn_advice_source_label.text() == FAKE_TURN_ADVICE_SOURCE_TYPE

    window.close()
    repository.close()


def test_synthetic_non_spontaneous_click_yields_zero_sends(tmp_path: Path) -> None:
    """Programmatic (non-OS) activation must never dispatch a real send."""

    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, window = build_window(tmp_path, transport)
    window.show()
    advance_to_turn_reviewed(window)

    # QPushButton.click() emits a synthetic (non-spontaneous) event; the
    # TrustedSendButton gate only reacts to genuinely spontaneous OS input.
    window.turn_gemini_send_button.click()

    assert transport.call_count == 0
    assert window.turn_gemini_send_button.trusted_mouse_activation_count == 0

    window.close()
    repository.close()
