from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import BattleState, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.explicit_turn_number import (
    ExplicitTurnNumberController,
    ExplicitTurnNumberWindow,
    validate_explicit_turn_number,
)

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
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")


def build_ready_controller(
    database_path: Path,
) -> tuple[SQLiteRepository, ExplicitTurnNumberController]:
    repository = SQLiteRepository(database_path)
    application = BattleApplication(repository)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    selection_adapter = MockSelectionAdviceAdapter()
    result = selection_adapter.submit(
        application,
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.apply_selection(
        selected_three=SELECTED_THREE,
        lead="Dondozo",
        human_confirmed=True,
    )
    controller = ExplicitTurnNumberController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
    )
    return repository, controller


def valid_turn_facts(turn_number: str) -> dict[str, object]:
    return {
        "turn_number": turn_number,
        "self_active": "Dondozo",
        "opponent_active": "Garchomp",
        "self_hp": "100",
        "opponent_hp": "81-90",
        "legal_moves": ("Protect", "Wave Crash"),
        "legal_switches": ("Flutter Mane", "Urshifu"),
        "human_note": "manual review",
        "human_confirmed": True,
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "入力してください"),
        ("abc", "1以上の整数"),
        ("0", "1以上の整数"),
        ("2", "現在値 1"),
    ],
)
def test_explicit_turn_number_validation_rejects_missing_invalid_or_mismatch(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_explicit_turn_number(value, 1)


def test_controller_rejects_turn_number_mismatch_without_canonical_mutation(
    tmp_path: Path,
) -> None:
    repository, controller = build_ready_controller(tmp_path / "maple.db")
    controller.start_turn_capture()
    before = repository.load_active_session()
    assert before is not None

    view = controller.confirm_turn_facts_with_number(**valid_turn_facts("2"))
    after = repository.load_active_session()

    assert view.error_message is not None
    assert "現在値 1" in view.error_message
    assert after is not None
    assert after.state is BattleState.TURN_CAPTURE_PENDING
    assert after.battle_revision == before.battle_revision
    assert after.current_reviewed_board_id is None


def test_controller_accepts_explicit_current_turn_number(tmp_path: Path) -> None:
    repository, controller = build_ready_controller(tmp_path / "maple.db")
    controller.start_turn_capture()

    view = controller.confirm_turn_facts_with_number(**valid_turn_facts("1"))

    assert view.error_message is None
    assert view.session_state == "TURN_REVIEWED"
    assert view.turn_facts is not None
    assert view.turn_facts.turn_number == 1
    assert repository.count_turn_facts(view.projection.session_id or "") == 1


def test_correction_with_wrong_turn_number_keeps_previous_snapshot(
    tmp_path: Path,
) -> None:
    repository, controller = build_ready_controller(tmp_path / "maple.db")
    controller.start_turn_capture()
    first = controller.confirm_turn_facts_with_number(**valid_turn_facts("1"))
    assert first.turn_facts is not None
    before = repository.load_active_session()
    assert before is not None

    rejected = controller.confirm_turn_facts_with_number(**valid_turn_facts("2"))
    after = repository.load_active_session()

    assert rejected.error_message is not None
    assert after is not None
    assert after.current_reviewed_board_id == before.current_reviewed_board_id
    assert after.battle_revision == before.battle_revision
    assert repository.count_turn_facts(after.session_id) == 1


def test_supported_window_requires_human_turn_number_input(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, controller = build_ready_controller(tmp_path / "maple.db")
    window = ExplicitTurnNumberWindow(controller)
    window.start_turn_button.click()
    qapp.processEvents()

    turn_number_input = window.turn_number_input
    assert turn_number_input is not None
    assert turn_number_input.text() == ""
    window.self_active_box.setCurrentText("Dondozo")
    window.opponent_active_input.setText("Garchomp")
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("81-90")
    window.move_inputs[0].setText("Protect")
    for checkbox in window.switch_checkboxes:
        checkbox.setChecked(checkbox.text() in {"Flutter Mane", "Urshifu"})
    window.turn_facts_confirm_checkbox.setChecked(True)

    window.confirm_turn_facts_button.click()
    qapp.processEvents()
    assert "Turn numberを入力してください" in window.error_label.text()
    assert controller.refresh().session_state == "TURN_CAPTURE_PENDING"

    turn_number_input.setText("1")
    window.confirm_turn_facts_button.click()
    qapp.processEvents()
    assert controller.refresh().session_state == "TURN_REVIEWED"
    window.close()
    repository.close()
