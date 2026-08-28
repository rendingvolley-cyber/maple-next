"""Two-step operator flow for actual action -> turn result -> NEXT TURN.

This is intentionally a UI-only orchestration layer on top of the existing
BattleRecordUiWindow / ActionResultDelta contract. It does not add a new
canonical battle state and does not change provider or persistence semantics.

Operator flow for RECORD_ACTUAL_ACTION:

1. enter actual self/opponent action + order
2. press 「結果記録」 to move to the result-entry page (no durable write)
3. enter HP/faint/status/stat-stage/weather/terrain deltas
4. press NEXT TURN; the existing rich action+delta write runs once, and only
   after that succeeds does the existing next_turn command run
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea

from maple_next.domain.enums import HpBucket
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.controller import OperatorView


class TwoStepBattleRecordUiWindow(BattleRecordUiWindow):
    """Battle Record UI with a local two-step action/result workbench."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        # ``BattleRecordUiWindow.__init__`` renders before returning. These
        # fields must therefore exist before ``super().__init__`` so our
        # render override can safely run during base construction.
        self._two_step_result_entry = False
        self._two_step_identity: tuple[object, ...] | None = None
        super().__init__(*args, **kwargs)

        self.result_entry_workbench_page = self._build_two_step_result_page()
        self.workbench_stack.addWidget(self.result_entry_workbench_page)
        self.render_view()

    def _build_two_step_result_page(self) -> QScrollArea:
        page, layout = self._parity_page(
            "結果記録",
            "ターン終了後に変わった事実だけ入力。入力完了後は NEXT TURN。",
        )

        summary_card, summary_layout = self._parity_card("このTurnの実行行動")
        self.two_step_action_summary = QLabel("—")
        self.two_step_action_summary.setWordWrap(True)
        summary_layout.addWidget(self.two_step_action_summary)
        layout.addWidget(summary_card)

        quick_card, quick_layout = self._parity_card(
            "クイック入力",
            subtitle=(
                "ひんしは canonical HP=0 として記録します。"
                "交代は前画面の実行行動から反映します。"
            ),
        )
        quick_row = QHBoxLayout()
        self.self_fainted_button = QPushButton("自分ひんし (HP 0)")
        self.opponent_fainted_button = QPushButton("相手ひんし (HP 0)")
        self.self_fainted_button.clicked.connect(
            lambda _checked=False: self._set_result_fainted("self")
        )
        self.opponent_fainted_button.clicked.connect(
            lambda _checked=False: self._set_result_fainted("opponent")
        )
        quick_row.addWidget(self.self_fainted_button)
        quick_row.addWidget(self.opponent_fainted_button)
        quick_row.addStretch(1)
        quick_layout.addLayout(quick_row)
        layout.addWidget(quick_card)

        # These are the already-accepted ActionResultDelta inputs. Reparenting
        # them here changes presentation only; the parent class still reads
        # the exact same widgets in its canonical record_actual_action path.
        self._detach_from_parent_layout(self.result_effect_candidate)
        self.result_effect_candidate.setProperty("candidateArea", True)
        layout.addWidget(self.result_effect_candidate)

        self._detach_from_parent_layout(self.action_result_delta_group)
        self.action_result_delta_group.setTitle("ターン終了後の変化")
        layout.addWidget(self.action_result_delta_group)

        back_row = QHBoxLayout()
        self.back_to_action_button = QPushButton("← 行動入力に戻る")
        self.back_to_action_button.clicked.connect(self._back_to_action_entry)
        back_row.addWidget(self.back_to_action_button)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        layout.addStretch(1)
        return page

    def _set_result_fainted(self, side: str) -> None:
        editor = self.self_delta_editor if side == "self" else self.opponent_delta_editor
        editor.hp_field.unknown_box.setChecked(False)
        editor.hp_field.value_box.setCurrentText(HpBucket.ZERO.value)

    def _back_to_action_entry(self, _checked: bool = False) -> None:
        self._two_step_result_entry = False
        self.render_view(self._bundle_c_controller.refresh())

    def _action_summary_text(self) -> str:
        own_type = self.actual_action_type_box.currentText().strip() or "未入力"
        own_name = self.actual_action_name_box.currentText().strip() or "—"
        opponent_type = self.opponent_action_type_box.currentText().strip() or "未入力"
        opponent_name = self.opponent_action_name_input.text().strip() or "—"
        order = self.action_order_box.currentText().strip() or "UNKNOWN"
        return (
            f"自分: {own_type} / {own_name}\n"
            f"相手: {opponent_type} / {opponent_name}\n"
            f"行動順: {order}"
        )

    def _on_record_action(self, _checked: bool = False) -> None:
        """First step: navigate to result entry without a durable write."""

        if not self._mutation_slots_allowed():
            return
        self._two_step_result_entry = True
        if hasattr(self, "two_step_action_summary"):
            self.two_step_action_summary.setText(self._action_summary_text())
        self.render_view(self._bundle_c_controller.refresh())

    def _on_next_turn(self, _checked: bool = False) -> None:
        """Result step: commit action+delta once, then advance one Turn."""

        current = self._bundle_c_controller.refresh()
        if (
            current.projection.primary_cta == "RECORD_ACTUAL_ACTION"
            and self._two_step_result_entry
        ):
            # Reuse the accepted parent write path exactly as-is. It reads the
            # action widgets and ActionResultDelta widgets and writes them in
            # the existing mandatory-rich transaction.
            super()._on_record_action(False)
            after_record = self._bundle_c_controller.refresh()
            if after_record.projection.primary_cta != "NEXT_TURN":
                # Validation/persistence failed: stay on result entry so the
                # operator can correct it; never advance a partially recorded
                # Turn.
                self._two_step_result_entry = True
                self.render_view(after_record)
                return

            self._two_step_result_entry = False
            advanced = self._bundle_c_controller.next_turn()
            self.render_view(advanced)
            return

        # Restart/recovery may legitimately land on canonical NEXT_TURN after
        # the record already exists. Preserve the normal accepted behavior.
        super()._on_next_turn(_checked)

    def render_view(self, view: OperatorView | None = None) -> None:
        super().render_view(view)
        if not hasattr(self, "result_entry_workbench_page"):
            return

        current = view if view is not None else self._bundle_c_controller.refresh()
        projection = current.projection
        identity = (
            projection.session_id,
            projection.match_id,
            projection.generation,
            projection.current_turn_id,
            projection.current_reviewed_board_id,
        )
        if identity != self._two_step_identity:
            self._two_step_identity = identity
            self._two_step_result_entry = False

        in_action_phase = projection.primary_cta == "RECORD_ACTUAL_ACTION"
        if not in_action_phase:
            self._two_step_result_entry = False
            return

        if self._two_step_result_entry:
            self.two_step_action_summary.setText(self._action_summary_text())
            self.workbench_stack.setCurrentWidget(self.result_entry_workbench_page)
            self.header_phase_badge.setText("結果記録")

            self.record_action_button.setText("結果記録")
            self.record_action_button.setEnabled(False)
            self.next_turn_button.setVisible(True)
            self.next_turn_button.setEnabled(current.persistence_reads_allowed)
            self.review_state_event_button.setEnabled(current.persistence_reads_allowed)

            self.record_action_button.setProperty("active", False)
            self.next_turn_button.setProperty("active", True)
            for button in (self.record_action_button, self.next_turn_button):
                button.style().unpolish(button)
                button.style().polish(button)
        else:
            self.workbench_stack.setCurrentWidget(self.action_workbench_page)
            self.header_phase_badge.setText("行動入力")
            self.record_action_button.setText("結果記録")
            self.next_turn_button.setVisible(True)
            self.next_turn_button.setEnabled(False)
            self.review_state_event_button.setEnabled(False)

            self.record_action_button.setProperty("active", True)
            self.next_turn_button.setProperty("active", False)
            for button in (self.record_action_button, self.next_turn_button):
                button.style().unpolish(button)
                button.style().polish(button)
