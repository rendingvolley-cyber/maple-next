"""Issue #8 explicit human Turn number input for the supported PySide6 path."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QFormLayout, QLineEdit

from maple_next.ui.controller import (
    OperatorInputError,
    OperatorView,
    SelectionFlowController,
    TurnFactsView,
)
from maple_next.ui.window import MapleMainWindow


def validate_explicit_turn_number(value: str, expected_turn_number: int | None) -> int:
    """Require the operator to enter the current canonical Turn number explicitly."""

    normalized = value.strip()
    if not normalized:
        raise OperatorInputError("Turn numberを入力してください。")
    try:
        turn_number = int(normalized)
    except ValueError as exc:
        raise OperatorInputError("Turn numberは1以上の整数で入力してください。") from exc
    if turn_number < 1:
        raise OperatorInputError("Turn numberは1以上の整数で入力してください。")
    if expected_turn_number is None:
        raise OperatorInputError("現在のTurn numberを確認できません。")
    if turn_number != expected_turn_number:
        raise OperatorInputError(
            f"Turn numberは画面上部の現在値 {expected_turn_number} と一致させてください。"
        )
    return turn_number


class ExplicitTurnNumberController(SelectionFlowController):
    """Adds explicit Turn-number validation without changing the canonical lifecycle."""

    def confirm_turn_facts_with_number(
        self,
        *,
        turn_number: str,
        self_active: str,
        opponent_active: str,
        self_hp: str,
        opponent_hp: str,
        legal_moves: Sequence[str],
        legal_switches: Sequence[str],
        human_note: str,
        human_confirmed: bool,
    ) -> OperatorView:
        try:
            validate_explicit_turn_number(turn_number, self.refresh().turn_number)
        except OperatorInputError as error:
            self._error_message = str(error)
            return self.refresh()
        return super().confirm_turn_facts(
            self_active=self_active,
            opponent_active=opponent_active,
            self_hp=self_hp,
            opponent_hp=opponent_hp,
            legal_moves=legal_moves,
            legal_switches=legal_switches,
            human_note=human_note,
            human_confirmed=human_confirmed,
        )


class ExplicitTurnNumberWindow(MapleMainWindow):
    """Supported window with a human-entered Turn number field in Turn facts."""

    def __init__(self, controller: ExplicitTurnNumberController) -> None:
        self._explicit_turn_controller = controller
        self.turn_number_input: QLineEdit | None = None
        super().__init__(controller)
        turn_number_input = QLineEdit()
        turn_number_input.setPlaceholderText("画面上部のTurn numberを入力")
        self.turn_number_input = turn_number_input
        layout = self.turn_facts_group.layout()
        if not isinstance(layout, QFormLayout):
            raise RuntimeError("Turn facts layout must be QFormLayout")
        layout.insertRow(0, "Turn number（人間入力）", turn_number_input)
        self.render_view()

    def _clear_turn_inputs(self) -> None:
        super()._clear_turn_inputs()
        if self.turn_number_input is not None:
            self.turn_number_input.clear()

    def _load_turn_facts(self, facts: TurnFactsView) -> None:
        super()._load_turn_facts(facts)
        if self.turn_number_input is not None:
            self.turn_number_input.setText(str(facts.turn_number))

    def _set_turn_facts_editable(self, enabled: bool) -> None:
        super()._set_turn_facts_editable(enabled)
        if self.turn_number_input is not None:
            self.turn_number_input.setEnabled(enabled)

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        turn_number_input = self.turn_number_input
        if turn_number_input is None:
            raise RuntimeError("Turn number input is not initialized")
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        switches = [
            checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()
        ]
        view = self._explicit_turn_controller.confirm_turn_facts_with_number(
            turn_number=turn_number_input.text(),
            self_active=self.self_active_box.currentText(),
            opponent_active=self.opponent_active_input.text(),
            self_hp=self.self_hp_box.currentText(),
            opponent_hp=self.opponent_hp_box.currentText(),
            legal_moves=moves,
            legal_switches=switches,
            human_note=self.turn_note_input.text(),
            human_confirmed=self.turn_facts_confirm_checkbox.isChecked(),
        )
        self.render_view(view)
