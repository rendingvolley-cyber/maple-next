"""Field-readiness regressions for mandatory Issue #31 operator entrypoints."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import BattleState, MatchOutcome
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Salamence",
    "Garchomp",
    "Dragonite",
    "Flutter Mane",
    "Tyranitar",
    "Pelipper",
)
SELECTED_THREE = SELF_TEAM[:3]


class _SyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)
        except ProviderTransportError as exc:
            self.on_failed(str(exc))
        else:
            self.on_succeeded(result)


def _qt_app() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def _build_window(
    root: Path, *, suffix: str = ""
) -> tuple[
    SQLiteRepository,
    MatchApplication,
    TurnStateFlowController,
    BattleRecordUiWindow,
]:
    _qt_app()
    repository = SQLiteRepository(root / "field-entrypoints.db")
    export_dir = root / "export"
    export_dir.mkdir(exist_ok=True)
    application = MatchApplication(repository, export_dir)
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(SELECTED_THREE), "lead": SELECTED_THREE[0]},
                source_type=GEMINI_SOURCE_TYPE,
                model="fixture-field-entrypoints",
            )
        ]
    )
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(
            api_key="fixture-only",
            model="fixture-field-entrypoints",
            timeout_seconds=5.0,
        ),
        dispatch_factory=_SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        adapter,
    )
    ocr_dir = root / f"ocr{suffix}"
    ocr_dir.mkdir(exist_ok=True)
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        auto_start_capture=False,
    )
    return repository, application, controller, window


def _show_tab(window: BattleRecordUiWindow, index: int) -> None:
    window.header_tabs.setCurrentIndex(index)
    window.show()
    QApplication.processEvents()


def _fill_selection(window: BattleRecordUiWindow) -> None:
    for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(name)
    for slot, name in enumerate(OPPONENT_TEAM, start=1):
        window._set_selection_slot_value(  # noqa: SLF001 - explicit human fixture
            slot,
            name,
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
    window.render_view()


def _advance_to_battle_ready(
    controller: TurnStateFlowController, window: BattleRecordUiWindow
) -> None:
    if controller.refresh().session_state is None:
        controller.new_match()
    window.render_view()
    _fill_selection(window)
    window._on_send_current_selection_to_gemini()  # noqa: SLF001 - fake trusted path
    window.render_view()
    window._on_apply()  # noqa: SLF001 - explicit human confirmation fixture
    assert controller.refresh().session_state == "BATTLE_READY"


def _assert_no_destructive_new_match(window: BattleRecordUiWindow) -> None:
    assert not window.new_match_button.isEnabled()
    assert not window.new_match_after_export_button.isEnabled()


def test_no_active_match_field_button_uses_canonical_handler_once_and_hydrates(
    tmp_path: Path,
) -> None:
    repository, _application, controller, window = _build_window(tmp_path)
    try:
        _show_tab(window, 1)
        assert window.parity_bottom_bar.isAncestorOf(window.new_match_button)
        assert window.new_match_button.isVisible()
        assert window.new_match_button.isEnabled()
        assert not window.new_match_after_export_button.isVisible()

        _show_tab(window, 0)
        assert not window.new_match_button.isVisible()
        _show_tab(window, 1)

        with patch.object(controller, "new_match", wraps=controller.new_match) as new_spy:
            window.new_match_button.click()
            window.new_match_button.click()

        session = repository.load_active_session()
        assert session is not None
        assert new_spy.call_count == 1
        assert repository.count_sessions() == 1
        assert session.generation == 1
        assert session.state is BattleState.SELECTION_OPEN
        assert window.header_tabs.currentIndex() == 0
        assert window.selection_v3_center.isVisible()
        assert not window.new_match_button.isVisible()
        _assert_no_destructive_new_match(window)
    finally:
        window.close()
        repository.close()

    restarted_repository, _app, restarted_controller, restarted_window = _build_window(
        tmp_path, suffix="-restart"
    )
    try:
        assert restarted_controller.refresh().session_state == "SELECTION_OPEN"
        _show_tab(restarted_window, 1)
        _assert_no_destructive_new_match(restarted_window)
    finally:
        restarted_window.close()
        restarted_repository.close()


def test_exported_match_field_button_uses_after_export_handler_once(tmp_path: Path) -> None:
    repository, application, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        _advance_to_battle_ready(controller, window)
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        application.export_match()
        window.render_view()
        _show_tab(window, 1)

        assert not window.new_match_button.isVisible()
        assert window.parity_bottom_bar.isAncestorOf(window.new_match_after_export_button)
        assert window.new_match_after_export_button.isVisible()
        assert window.new_match_after_export_button.isEnabled()

        with patch.object(
            controller,
            "new_match_after_export",
            wraps=controller.new_match_after_export,
        ) as new_spy:
            window.new_match_after_export_button.click()
            window.new_match_after_export_button.click()

        session = repository.load_active_session()
        assert session is not None
        assert new_spy.call_count == 1
        assert repository.count_sessions() == 2
        assert session.generation == 2
        assert session.state is BattleState.SELECTION_OPEN
        assert window.header_tabs.currentIndex() == 0
        _assert_no_destructive_new_match(window)
    finally:
        window.close()
        repository.close()


def test_active_unfinished_states_never_enable_new_match_bypass(tmp_path: Path) -> None:
    repository, application, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        window.render_view()
        _assert_no_destructive_new_match(window)

        _advance_to_battle_ready(controller, window)
        _show_tab(window, 1)
        _assert_no_destructive_new_match(window)

        controller.start_turn_capture()
        window.render_view()
        _assert_no_destructive_new_match(window)

        session = repository.load_active_session()
        assert session is not None
        with repository.transaction():
            repository.save_session(replace(session, state=BattleState.BATTLE_READY))
        application.end_match(MatchOutcome.LOSE, human_confirmed=True)
        window.render_view()
        _assert_no_destructive_new_match(window)
    finally:
        window.close()
        repository.close()


def test_mandatory_field_controls_exist_gate_and_connect_to_canonical_handlers(
    tmp_path: Path,
) -> None:
    repository, _application, controller, window = _build_window(tmp_path)
    try:
        # NEW MATCH: canonical controller method is reached by the Battle-only field button.
        _show_tab(window, 1)
        with patch.object(controller, "new_match", wraps=controller.new_match) as new_spy:
            window.new_match_button.click()
        assert new_spy.call_count == 1

        # Selection Gemini send: accepted trusted handler, visible only on Selection,
        # and enabled only after all six human-confirmed values exist.
        _fill_selection(window)
        _show_tab(window, 0)
        assert window.selection_roi_send_button.isVisible()
        assert window.selection_roi_send_button.isEnabled()
        assert window.selection_roi_send_button._on_trusted_activate.__self__ is window  # noqa: SLF001
        assert (
            window.selection_roi_send_button._on_trusted_activate.__func__.__name__  # noqa: SLF001
            == "_on_send_current_selection_to_gemini"
        )
        window._on_send_current_selection_to_gemini()  # noqa: SLF001 - fake transport

        # Selection confirm/APPLY: accepted fail-closed handler remains bound.
        window.render_view()
        assert window.apply_button.isVisible()
        assert window.apply_button.isEnabled()
        assert window.apply_button._on_trusted_activate.__self__ is window  # noqa: SLF001
        assert window.apply_button._on_trusted_activate.__func__.__name__ == "_on_apply"  # noqa: SLF001
        window._on_apply()  # noqa: SLF001 - explicit human fixture

        # Turn record: fixed Battle lifecycle slot is present. A RECORD projection
        # plus completed human fields enables it and its click reaches the canonical
        # controller method exactly once.
        current = controller.refresh()
        record_view = replace(
            current,
            projection=replace(
                current.projection,
                primary_cta="RECORD_ACTUAL_ACTION",
                primary_cta_enabled=True,
                session_state="TURN_REVIEWED",
            ),
        )
        with patch.object(controller, "refresh", return_value=record_view):
            window.render_view(record_view)
        window.actual_action_type_box.setCurrentText("MOVE")
        window.actual_action_name_box.clear()
        window.actual_action_name_box.addItem("Tackle")
        window.actual_action_name_box.setCurrentText("Tackle")
        window.actual_action_confirm_checkbox.setChecked(True)
        window._update_actual_action_button()  # noqa: SLF001 - readiness fixture
        _show_tab(window, 1)
        assert window.record_action_button.isVisible()
        assert window.record_action_button.isEnabled()
        with patch.object(
            controller, "record_actual_action", return_value=record_view
        ) as record_spy:
            window.record_action_button.click()
        assert record_spy.call_count == 1

        # MATCH END: visible through the accepted terminal drawer only in an
        # endable state; completing the human outcome controls enables it and
        # its click reaches the canonical match controller exactly once.
        battle_ready = controller.refresh()
        window.render_view(battle_ready)
        window.header_export_button.click()
        window.outcome_box.setCurrentText(MatchOutcome.WIN.value)
        window.outcome_confirm_checkbox.setChecked(True)
        QApplication.processEvents()
        assert window.end_match_button.isVisible()
        assert window.end_match_button.isEnabled()
        with patch.object(controller, "end_match", return_value=battle_ready) as end_spy:
            window.end_match_button.click()
        assert end_spy.call_count == 1
    finally:
        window.close()
        repository.close()
