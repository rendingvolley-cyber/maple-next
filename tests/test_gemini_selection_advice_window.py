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

    # Drain the event loop until the worker thread's result has been
    # delivered and handled on the UI thread. SelectionAdviceDispatch only
    # calls QThread.quit()/wait() from inside that handler, so returning
    # before this predicate is true risks tearing down (via GC or process
    # exit) a QThread that is still running -- a fatal, untraceable native
    # crash in PySide6/Qt rather than a Python exception.
    #
    # The one production Gemini attempt is durably consumed once dispatched
    # (Issue #29 B-01), so a terminal failure must not re-enable the send
    # button; the completion signal here is the sanitized failure message,
    # not a re-enabled button.
    pump_until(qapp, lambda: window._controller.refresh().error_message is not None)  # noqa: SLF001
    window.render_view()
    assert window.gemini_send_button.isEnabled() is False

    window.close()
    repository.close()


def test_apply_then_restart_reload_shows_same_applied_selection(tmp_path: Path) -> None:
    """Full human flow through the real window, then a fresh process restart.

    launch -> reviewed 6v6 -> trusted Gemini send -> success -> trusted
    explicit APPLY -> close (simulating app exit) -> reopen a brand new
    window/application/repository on the same database file -> the
    restarted app must display the exact same applied selected_three/lead
    without any new provider dispatch.
    """

    database_path = tmp_path / "maple.db"
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
    repository = SQLiteRepository(database_path)
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
    pump_until(qapp, lambda: transport.call_count >= 1)
    pump_until(qapp, lambda: window._controller.refresh().advice is not None)  # noqa: SLF001
    window.render_view()
    assert transport.call_count == 1

    for checkbox in window.actual_checkboxes:
        checkbox.setChecked(checkbox.text() in GEMINI_THREE)
    window.actual_lead_box.setCurrentText("Meowscarada")
    window.apply_confirm_checkbox.setChecked(True)
    assert window.apply_button.isEnabled() is True
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    window.render_view()

    before = window._controller.refresh()  # noqa: SLF001
    assert before.session_state == "BATTLE_READY"
    assert before.applied_selection is not None
    assert before.applied_selection.selected_three == GEMINI_THREE
    assert before.applied_selection.lead == "Meowscarada"
    assert transport.call_count == 1

    session_id = repository.load_active_session().session_id  # type: ignore[union-attr]
    window.close()
    repository.close()

    restarted_repository = SQLiteRepository(database_path)
    restarted_application = BattleApplication(restarted_repository)
    restarted_application.recover_after_restart()
    restarted_transport = FakeSelectionAdviceTransport()
    restarted_gemini_adapter = GeminiSelectionAdviceAdapter(
        restarted_transport, lambda: ProviderConfig(api_key="test-key", model="test-model")
    )
    restarted_controller = SelectionFlowController(
        restarted_application,
        restarted_repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=restarted_gemini_adapter,
    )
    restarted_window = MapleMainWindow(restarted_controller)
    restarted_window.show()
    restarted_window.render_view()

    after = restarted_controller.refresh()
    assert after.session_state == "BATTLE_READY"
    assert after.applied_selection is not None
    assert after.applied_selection.selected_three == GEMINI_THREE
    assert after.applied_selection.lead == "Meowscarada"
    restarted_session = restarted_repository.load_active_session()
    assert restarted_session is not None
    assert restarted_session.session_id == session_id
    assert restarted_window.actual_three_label.text() == " / ".join(GEMINI_THREE)
    assert restarted_window.actual_lead_label.text() == "Meowscarada"
    assert restarted_transport.call_count == 0

    restarted_window.close()
    restarted_repository.close()
