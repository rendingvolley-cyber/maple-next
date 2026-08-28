"""Regression tests for action input -> result entry -> NEXT TURN flow.

No real capture device or provider network is used. The tests exercise the
real SQLite repository, TurnStateFlowController, and Qt window offscreen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import HpBucket
from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_transport import FakeTurnAdviceTransport
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.two_step_battle_record_ui import TwoStepBattleRecordUiWindow
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = (SELF_TEAM[0], SELF_TEAM[1], SELF_TEAM[2])


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def build_window(tmp_path: Path) -> tuple[
    SQLiteRepository,
    TurnStateFlowController,
    TwoStepBattleRecordUiWindow,
    FakeTurnAdviceTransport,
]:
    qt_application()
    repository = SQLiteRepository(tmp_path / "two-step.db")
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = FakeTurnAdviceTransport()
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        rich_turn_gemini_adapter=GeminiRichTurnAdviceAdapter(transport),
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    window = TwoStepBattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        auto_start_capture=False,
    )
    return repository, controller, window, transport


def advance_to_action_phase(
    controller: TurnStateFlowController, window: TwoStepBattleRecordUiWindow
) -> None:
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELECTED_THREE), SELECTED_THREE[0])
    controller.apply_selection(list(SELECTED_THREE), SELECTED_THREE[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()

    window.self_active_box.setCurrentText(SELECTED_THREE[0])
    window.opponent_active_input.setText(OPPONENT_TEAM[0])
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText("Flower Trick")
    window.move_inputs[1].setText("Swords Dance")
    window.switch_checkboxes[1].setChecked(True)
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")
    window._on_confirm_turn_facts()  # noqa: SLF001

    candidates = controller.derive_legal_switch_candidates()
    controller.confirm_legal_switches(
        legal_switches=candidates,
        status=(
            LegalSwitchStatus.CONFIRMED_NONEMPTY
            if candidates
            else LegalSwitchStatus.CONFIRMED_NONE
        ),
    )

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("opponent move")
    window.mock_turn_rationale_input.setText("test")
    window._on_submit_mock_turn()  # noqa: SLF001
    window.render_view()

    assert controller.refresh().projection.primary_cta == "RECORD_ACTUAL_ACTION"


def fill_action(window: TwoStepBattleRecordUiWindow, *, confirmed: bool = True) -> None:
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(confirmed)
    window.opponent_action_type_box.setCurrentText("NO ACTION")
    window.action_order_box.setCurrentText("SELF_FIRST")


def test_result_button_only_navigates_then_next_turn_commits_faint_and_stage_change(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(controller, window)
    fill_action(window)

    before = controller.turn_state_summary()
    assert before.identity is not None
    assert before.identity.turn_number == 1
    assert before.latest_delta is None

    window._on_record_action()  # noqa: SLF001

    after_result_button = controller.turn_state_summary()
    assert after_result_button.identity == before.identity
    assert after_result_button.latest_delta is None
    assert controller.refresh().projection.primary_cta == "RECORD_ACTUAL_ACTION"
    assert window._two_step_result_entry is True  # noqa: SLF001
    assert window.workbench_stack.currentWidget() is window.result_entry_workbench_page
    assert window.next_turn_button.isEnabled()

    window.opponent_fainted_button.click()
    window.self_delta_editor.stage_fields["attack_stage"].spin.setValue(2)
    assert window.opponent_delta_editor.hp_field.value_box.currentText() == HpBucket.ZERO.value

    window._on_next_turn()  # noqa: SLF001

    next_summary = controller.turn_state_summary()
    assert next_summary.identity is not None
    assert next_summary.identity.turn_number == 2
    assert next_summary.open_draft is not None
    assert next_summary.open_draft.opponent_side.hp_bucket.value is HpBucket.ZERO
    assert next_summary.open_draft.self_side.attack_stage.value == 2
    assert transport.call_count == 0
    repository.close()


def test_move_result_can_record_post_move_active_change(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(controller, window)
    fill_action(window)

    window._on_record_action()  # noqa: SLF001
    assert window.result_self_active_box.findText(SELECTED_THREE[1]) >= 0
    window.result_self_active_box.setCurrentText(SELECTED_THREE[1])

    window._on_next_turn()  # noqa: SLF001

    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.active.value == SELECTED_THREE[1]
    assert transport.call_count == 0
    repository.close()


def test_invalid_action_confirmation_does_not_advance_from_result_page(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(controller, window)
    fill_action(window, confirmed=False)

    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001

    after = controller.turn_state_summary()
    assert after.identity == identity_before
    assert after.latest_delta is None
    view = controller.refresh()
    assert view.projection.primary_cta == "RECORD_ACTUAL_ACTION"
    assert view.error_message is not None
    assert window._two_step_result_entry is True  # noqa: SLF001
    assert window.workbench_stack.currentWidget() is window.result_entry_workbench_page
    assert transport.call_count == 0
    repository.close()
