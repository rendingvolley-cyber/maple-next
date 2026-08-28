"""Two-step operator flow for actual action -> turn result -> NEXT TURN.

The canonical Battle Record state model remains unchanged. This class only
splits the existing action/result workbench into two explicit operator steps:

1. enter actual actions/order and press 「結果記録」 (navigation only)
2. enter result deltas and press NEXT TURN (atomic write, then one Turn advance)
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QScrollArea

from maple_next.capture.contracts import VideoCaptureBackend
from maple_next.domain.enums import HpBucket
from maple_next.domain.opponent_intel import OpponentMetaProvider
from maple_next.domain.turn_state import ChangeObservation, FieldDelta, SideDelta
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.controller import OperatorView
from maple_next.ui.turn_state_flow import TurnStateFlowController

_NO_ACTIVE_CHANGE = "変化なし"
_T = TypeVar("_T")


class TwoStepBattleRecordUiWindow(BattleRecordUiWindow):
    """Battle Record UI with explicit action-entry and result-entry steps."""

    def __init__(
        self,
        controller: TurnStateFlowController,
        *,
        ocr_data_directory: Path,
        opponent_meta_provider: OpponentMetaProvider | None = None,
        capture_backend: VideoCaptureBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        # Base construction renders before returning, so these local fields
        # must exist before super().__init__ invokes our render override.
        self._two_step_result_entry = False
        self._two_step_identity: tuple[object, ...] | None = None
        super().__init__(
            controller,
            ocr_data_directory=ocr_data_directory,
            opponent_meta_provider=opponent_meta_provider,
            capture_backend=capture_backend,
            auto_start_capture=auto_start_capture,
        )
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
                "ひんしは canonical HP=0。通常の交代は前画面のSWITCHから自動反映。"
                "とんぼがえり等の技後交代はターン後Activeで指定します。"
                "Active変更時のHP・状態・能力ランクは交代後の最終状態です。"
            ),
        )
        faint_row = QHBoxLayout()
        self.self_fainted_button = QPushButton("自分ひんし (HP 0)")
        self.opponent_fainted_button = QPushButton("相手ひんし (HP 0)")
        self.self_fainted_button.clicked.connect(
            lambda _checked=False: self._set_result_fainted("self")
        )
        self.opponent_fainted_button.clicked.connect(
            lambda _checked=False: self._set_result_fainted("opponent")
        )
        faint_row.addWidget(self.self_fainted_button)
        faint_row.addWidget(self.opponent_fainted_button)
        faint_row.addStretch(1)
        quick_layout.addLayout(faint_row)

        active_row = QHBoxLayout()
        active_row.addWidget(QLabel("自分 ターン後Active"))
        self.result_self_active_box = QComboBox()
        self.result_self_active_box.addItem(_NO_ACTIVE_CHANGE)
        active_row.addWidget(self.result_self_active_box, 1)
        active_row.addWidget(QLabel("相手 ターン後Active"))
        self.result_opponent_active_box = QComboBox()
        self.result_opponent_active_box.addItem(_NO_ACTIVE_CHANGE)
        active_row.addWidget(self.result_opponent_active_box, 1)
        quick_layout.addLayout(active_row)

        self.two_step_result_error_label = QLabel("")
        self.two_step_result_error_label.setWordWrap(True)
        self.two_step_result_error_label.setStyleSheet("color: #dc2626; font-weight: 600;")
        quick_layout.addWidget(self.two_step_result_error_label)
        layout.addWidget(quick_card)

        # Reuse the accepted ActionResultDelta widgets. The base v5 surface
        # intentionally hid per-stage manual editing; result entry needs an
        # explicit human escape hatch, so expose the existing collapsed
        # detail sections here without creating parallel stage state.
        self.self_delta_editor.detail_section.setVisible(True)
        self.opponent_delta_editor.detail_section.setVisible(True)

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

    @staticmethod
    def _selected_active_change(box: QComboBox) -> str | None:
        value = box.currentText().strip()
        return None if not value or value == _NO_ACTIVE_CHANGE else value

    @staticmethod
    def _reset_combo_options(box: QComboBox, values: tuple[str, ...]) -> None:
        previous = box.currentText().strip()
        box.blockSignals(True)
        try:
            box.clear()
            box.addItem(_NO_ACTIVE_CHANGE)
            box.addItems(list(values))
            if previous and box.findText(previous) >= 0:
                box.setCurrentText(previous)
            else:
                box.setCurrentIndex(0)
        finally:
            box.blockSignals(False)

    @staticmethod
    def _overlay_field(base: FieldDelta[_T], manual: FieldDelta[_T]) -> FieldDelta[_T]:
        return base if manual.observation is ChangeObservation.UNCHANGED else manual

    @classmethod
    def _overlay_result_delta_on_switch_base(
        cls, base: SideDelta, manual: SideDelta
    ) -> SideDelta:
        """Retain explicit final-state edits on top of canonical switch state."""

        return SideDelta(
            active=base.active,
            hp_bucket=cls._overlay_field(base.hp_bucket, manual.hp_bucket),
            status=cls._overlay_field(base.status, manual.status),
            attack_stage=cls._overlay_field(base.attack_stage, manual.attack_stage),
            defense_stage=cls._overlay_field(base.defense_stage, manual.defense_stage),
            special_attack_stage=cls._overlay_field(
                base.special_attack_stage, manual.special_attack_stage
            ),
            special_defense_stage=cls._overlay_field(
                base.special_defense_stage, manual.special_defense_stage
            ),
            speed_stage=cls._overlay_field(base.speed_stage, manual.speed_stage),
            accuracy_stage=cls._overlay_field(base.accuracy_stage, manual.accuracy_stage),
            evasion_stage=cls._overlay_field(base.evasion_stage, manual.evasion_stage),
            side_effects=cls._overlay_field(base.side_effects, manual.side_effects),
        )

    @staticmethod
    def _faint_and_active_change_conflict(destination: str | None, manual: SideDelta) -> bool:
        # A single SideDelta describes the final active. Persisting active=B
        # together with HP=0 intended for outgoing A would falsely faint B.
        return (
            destination is not None
            and manual.hp_bucket.observation is ChangeObservation.CHANGED
            and manual.hp_bucket.after_value is HpBucket.ZERO
        )

    def _refresh_result_active_options(self, current: OperatorView) -> None:
        summary = self._bundle_c_controller.turn_state_summary()
        self_active: str | None = None
        opponent_active: str | None = None
        if summary.confirmed_state is not None:
            if summary.confirmed_state.self_side.active.is_confirmed:
                self_active = summary.confirmed_state.self_side.active.value
            if summary.confirmed_state.opponent_side.active.is_confirmed:
                opponent_active = summary.confirmed_state.opponent_side.active.value

        self_candidates = (
            current.applied_selection.selected_three
            if current.applied_selection is not None
            else current.self_team
        )
        self._reset_combo_options(
            self.result_self_active_box,
            tuple(name for name in self_candidates if name and name != self_active),
        )
        self._reset_combo_options(
            self.result_opponent_active_box,
            tuple(name for name in current.opponent_team if name and name != opponent_active),
        )

    def _back_to_action_entry(self, _checked: bool = False) -> None:
        self._two_step_result_entry = False
        self.two_step_result_error_label.clear()
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
        """Navigate to result entry. No action/result row is written here."""

        if not self._mutation_slots_allowed():
            return
        self._two_step_result_entry = True
        if hasattr(self, "two_step_action_summary"):
            self.two_step_action_summary.setText(self._action_summary_text())
            self.two_step_result_error_label.clear()
        self.render_view(self._bundle_c_controller.refresh())

    def _commit_action_and_result(self) -> OperatorView | None:
        opponent_type = self.opponent_action_type_box.currentText()
        if opponent_type == "選択してください":
            opponent_type = ""
        if opponent_type in {"NO ACTION", "UNKNOWN"}:
            self.opponent_action_name_input.clear()
            opponent_type = ""

        action_type = self.actual_action_type_box.currentText()
        action_name = self.actual_action_name_box.currentText().strip()
        opponent_name = self.opponent_action_name_input.text().strip()

        self_destination = (
            action_name
            if action_type == "SWITCH" and action_name
            else self._selected_active_change(self.result_self_active_box)
        )
        opponent_destination = (
            opponent_name
            if opponent_type == "SWITCH" and opponent_name
            else self._selected_active_change(self.result_opponent_active_box)
        )

        manual_self_delta = self.self_delta_editor.to_side_delta()
        manual_opponent_delta = self.opponent_delta_editor.to_side_delta()
        if self._faint_and_active_change_conflict(self_destination, manual_self_delta):
            self.two_step_result_error_label.setText(
                "自分側で「ひんし(HP 0)」とターン後Active変更は同時確定できません。"
                "ひんしを記録してNEXT TURNへ進み、"
                "次Turnの状態確認で交代後Activeを確定してください。"
            )
            return None
        if self._faint_and_active_change_conflict(
            opponent_destination, manual_opponent_delta
        ):
            self.two_step_result_error_label.setText(
                "相手側で「ひんし(HP 0)」とターン後Active変更は同時確定できません。"
                "ひんしを記録してNEXT TURNへ進み、"
                "次Turnの状態確認で交代後Activeを確定してください。"
            )
            return None
        self.two_step_result_error_label.clear()

        if self_destination:
            switch_base = self._bundle_c_controller.compute_confirmed_switch_side_delta(
                side="self", destination_pokemon_name=self_destination
            )
            self_side_delta = self._overlay_result_delta_on_switch_base(
                switch_base, manual_self_delta
            )
        else:
            self_side_delta = manual_self_delta

        if opponent_destination:
            switch_base = self._bundle_c_controller.compute_confirmed_switch_side_delta(
                side="opponent", destination_pokemon_name=opponent_destination
            )
            opponent_side_delta = self._overlay_result_delta_on_switch_base(
                switch_base, manual_opponent_delta
            )
        else:
            opponent_side_delta = manual_opponent_delta

        return self._bundle_c_controller.record_actual_action(
            action_type=action_type,
            action_name=self.actual_action_name_box.currentText(),
            human_confirmed=self.actual_action_confirm_checkbox.isChecked(),
            opponent_action_type=opponent_type,
            opponent_action_name=self.opponent_action_name_input.text(),
            action_order=self.action_order_box.currentText(),
            self_side_delta=self_side_delta,
            opponent_side_delta=opponent_side_delta,
            weather_delta=self.weather_delta_field.to_delta(),
            terrain_delta=self.terrain_delta_field.to_delta(),
        )

    def _on_next_turn(self, _checked: bool = False) -> None:
        """Commit action+result once; only a successful commit advances Turn."""

        current = self._bundle_c_controller.refresh()
        if (
            current.projection.primary_cta == "RECORD_ACTUAL_ACTION"
            and self._two_step_result_entry
        ):
            recorded = self._commit_action_and_result()
            if recorded is None:
                self._two_step_result_entry = True
                self.workbench_stack.setCurrentWidget(self.result_entry_workbench_page)
                return

            self.render_view(recorded)
            after_record = self._bundle_c_controller.refresh()
            if after_record.projection.primary_cta != "NEXT_TURN":
                self._two_step_result_entry = True
                self.render_view(after_record)
                return

            self._two_step_result_entry = False
            self.two_step_result_error_label.clear()
            advanced = self._bundle_c_controller.next_turn()
            self.render_view(advanced)
            return

        # Restart recovery may legitimately resume from canonical NEXT_TURN.
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
            self.result_self_active_box.setCurrentIndex(0)
            self.result_opponent_active_box.setCurrentIndex(0)
            self.two_step_result_error_label.clear()

        in_action_phase = projection.primary_cta == "RECORD_ACTUAL_ACTION"
        if not in_action_phase:
            self._two_step_result_entry = False
            return

        self._refresh_result_active_options(current)
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
            self.record_action_button.setEnabled(
                current.persistence_reads_allowed and projection.primary_cta_enabled
            )
            self.next_turn_button.setVisible(True)
            self.next_turn_button.setEnabled(False)
            self.review_state_event_button.setEnabled(False)
            self.record_action_button.setProperty("active", True)
            self.next_turn_button.setProperty("active", False)
            for button in (self.record_action_button, self.next_turn_button):
                button.style().unpolish(button)
                button.style().polish(button)
