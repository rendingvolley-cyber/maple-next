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
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    SanitizedProviderResult,
)
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.window import MapleMainWindow

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
GEMINI_THREE = ("Meowscarada", "Gholdengo", "Dragonite")


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def pump_until(qapp: QApplication, predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting for async Gemini result"
        qapp.processEvents()
        time.sleep(0.005)


def build_window(
    tmp_path: Path, transport: FakeSelectionAdviceTransport
) -> tuple[SQLiteRepository, MapleMainWindow]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    gemini_adapter = GeminiSelectionAdviceAdapter(
        transport, lambda: ProviderConfig(api_key="test-key", model="test-model")
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=gemini_adapter,
    )
    window = MapleMainWindow(controller)
    return repository, window


def test_gemini_group_hidden_until_reviewed_facts_confirmed(tmp_path: Path) -> None:
    qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, window = build_window(tmp_path, transport)
    window.show()

    window.new_match_button.click()
    window.render_view()
    assert window.gemini_group.isVisible() is False

    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    assert window.gemini_group.isVisible() is True
    assert window.gemini_send_button.isEnabled() is True
    window.close()
    repository.close()


def test_trusted_click_on_real_window_sends_exactly_once_and_shows_gemini_source(
    tmp_path: Path,
) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, window = build_window(tmp_path, transport)
    window.show()

    window.new_match_button.click()
    window.render_view()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    assert window.gemini_send_button.isEnabled()
    QTest.mouseClick(window.gemini_send_button, Qt.MouseButton.LeftButton)

    # Immediately after the trusted click, the send is in flight: disabled, no advice yet.
    assert window.gemini_send_button.trusted_mouse_activation_count == 1

    pump_until(qapp, lambda: transport.call_count >= 1)
    pump_until(qapp, lambda: window._controller.refresh().advice is not None)  # noqa: SLF001
    window.render_view()

    assert transport.call_count == 1
    assert window.advice_source_label.text() == GEMINI_SOURCE_TYPE
    assert window.advice_three_label.text() == " / ".join(GEMINI_THREE)
    window.close()
    repository.close()


def test_gemini_send_button_disabled_while_pending(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, window = build_window(tmp_path, transport)
    window.show()
    window.new_match_button.click()
    window.render_view()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    QTest.mouseClick(window.gemini_send_button, Qt.MouseButton.LeftButton)
    # The dispatch is asynchronous; before the fake transport resolves, the
    # button must already be disabled by the pending-state re-render.
    assert window.gemini_send_button.isEnabled() is False
    qapp.processEvents()
    window.close()
    repository.close()
