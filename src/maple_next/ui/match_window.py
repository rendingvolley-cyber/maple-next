"""PySide6 match completion, canonical export, and next-match controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
)

from maple_next.domain.enums import MatchOutcome
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController, MatchOperatorView
from maple_next.ui.selection_advice_integration import SelectionAdviceIntegrationWindow

_PLACEHOLDER = "選択してください"


class MatchFlowWindow(SelectionAdviceIntegrationWindow):
    """Supported operator window with explicit terminal match commands."""

    def __init__(self, controller: MatchFlowController) -> None:
        self._match_controller = controller
        self._match_widgets_ready = False
        super().__init__(controller)
        self._build_match_end_group()
        self._build_match_summary_group()
        self._build_match_export_group()
        self._match_widgets_ready = True
        self.render_view()

    def _insert_before_stretch(self, widget: QGroupBox) -> None:
        index = max(0, self._root_layout.count() - 1)
        self._root_layout.insertWidget(index, widget)

    def _build_match_end_group(self) -> None:
        self.match_end_group = QGroupBox("対戦結果 — 人間が二段階で確定")
        layout = QFormLayout(self.match_end_group)
        self.outcome_box = QComboBox()
        self.outcome_box.addItems(
            [_PLACEHOLDER, MatchOutcome.WIN.value, MatchOutcome.LOSE.value]
        )
        self.outcome_box.currentTextChanged.connect(self._update_end_match_button)
        self.outcome_confirm_checkbox = QCheckBox(
            "選択したWIN / LOSEを確定し、後から変更できないことを確認します"
        )
        self.outcome_confirm_checkbox.toggled.connect(self._update_end_match_button)
        self.end_match_button = QPushButton("WIN / LOSEを確定")
        self.end_match_button.clicked.connect(self._on_end_match)
        layout.addRow("結果", self.outcome_box)
        layout.addRow(self.outcome_confirm_checkbox)
        layout.addRow(self.end_match_button)
        self._insert_before_stretch(self.match_end_group)

    def _build_match_summary_group(self) -> None:
        self.match_summary_group = QGroupBox("確定済み対戦結果")
        layout = QFormLayout(self.match_summary_group)
        self.match_outcome_label = QLabel()
        self.match_id_label = QLabel()
        self.match_generation_label = QLabel()
        self.match_turn_count_label = QLabel()
        self.match_action_count_label = QLabel()
        self.save_match_button = QPushButton("SAVE MATCH JSON")
        self.save_match_button.clicked.connect(self._on_save_match)
        layout.addRow("結果", self.match_outcome_label)
        layout.addRow("Match ID", self.match_id_label)
        layout.addRow("Generation", self.match_generation_label)
        layout.addRow("記録済みTurn数", self.match_turn_count_label)
        layout.addRow("実行行動数", self.match_action_count_label)
        layout.addRow(self.save_match_button)
        self._insert_before_stretch(self.match_summary_group)

    def _build_match_export_group(self) -> None:
        self.match_export_group = QGroupBox("保存済みMATCH JSON")
        layout = QFormLayout(self.match_export_group)
        self.export_file_label = QLabel()
        self.export_hash_label = QLabel()
        self.export_hash_label.setTextInteractionFlags(
            self.export_hash_label.textInteractionFlags()
        )
        self.export_schema_label = QLabel()
        self.new_match_after_export_button = QPushButton("NEW MATCH")
        self.new_match_after_export_button.clicked.connect(
            self._on_new_match_after_export
        )
        layout.addRow("File", self.export_file_label)
        layout.addRow("SHA-256", self.export_hash_label)
        layout.addRow("Schema", self.export_schema_label)
        layout.addRow(self.new_match_after_export_button)
        self._insert_before_stretch(self.match_export_group)

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._match_controller.refresh()
        super().render_view(current)
        if not self._match_widgets_ready:
            return
        if not isinstance(current, MatchOperatorView):
            current = self._match_controller.refresh()

        state = current.session_state
        endable = state in {"BATTLE_READY", "TURN_RECORDED"}
        self.match_end_group.setVisible(endable)
        self.outcome_box.setEnabled(endable)
        self.outcome_confirm_checkbox.setEnabled(endable)
        self._update_end_match_button()
        if not endable:
            self.outcome_box.setCurrentIndex(0)
            self.outcome_confirm_checkbox.setChecked(False)

        terminal = state in {"MATCH_ENDED", "MATCH_EXPORTED"}
        self.match_summary_group.setVisible(terminal)
        self.match_outcome_label.setText(current.outcome or "—")
        self.match_id_label.setText(current.projection.match_id or "—")
        generation = current.projection.generation
        self.match_generation_label.setText("—" if generation is None else str(generation))
        self.match_turn_count_label.setText(str(current.turn_count))
        self.match_action_count_label.setText(str(current.action_count))
        self.save_match_button.setVisible(state == "MATCH_ENDED")
        self.save_match_button.setEnabled(state == "MATCH_ENDED")

        exported = state == "MATCH_EXPORTED"
        self.match_export_group.setVisible(exported)
        self.export_file_label.setText(
            Path(current.export_path).name if current.export_path else "—"
        )
        self.export_hash_label.setText(current.export_sha256 or "—")
        self.export_schema_label.setText(current.export_schema_version or "—")
        self.new_match_after_export_button.setEnabled(exported)

        if state == "MATCH_ENDED":
            self.primary_cta_label.setText("SAVE MATCH JSON")
            self.guidance_label.setText(
                "確定済みの対戦内容をcanonical JSONとして明示保存してください。"
            )
        elif state == "MATCH_EXPORTED":
            self.primary_cta_label.setText("NEW MATCH")
            self.guidance_label.setText(
                "MATCH JSONは保存済みです。次の対戦を始める場合はNEW MATCHを押してください。"
            )

    def _update_end_match_button(self, _value: object = None) -> None:
        if not self._match_widgets_ready:
            return
        self.end_match_button.setEnabled(
            self.outcome_box.isEnabled()
            and self.outcome_box.currentText() in {
                MatchOutcome.WIN.value,
                MatchOutcome.LOSE.value,
            }
            and self.outcome_confirm_checkbox.isChecked()
        )

    def _on_end_match(self, _checked: bool = False) -> None:
        view = self._match_controller.end_match(
            self.outcome_box.currentText(),
            human_confirmed=self.outcome_confirm_checkbox.isChecked(),
        )
        self.render_view(view)

    def _on_save_match(self, _checked: bool = False) -> None:
        self.render_view(self._match_controller.save_match_json())

    def _on_new_match_after_export(self, _checked: bool = False) -> None:
        self.render_view(self._match_controller.new_match_after_export())
