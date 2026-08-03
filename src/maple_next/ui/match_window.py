"""PySide6 match completion, canonical export, and next-match controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
)

from maple_next.domain.enums import MatchOutcome
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController, MatchOperatorView
from maple_next.ui.turn_advice_integration import TurnAdviceIntegrationWindow

_PLACEHOLDER = "選択してください"


class MatchFlowWindow(TurnAdviceIntegrationWindow):
    """Supported operator window with explicit terminal match commands."""

    def __init__(self, controller: MatchFlowController) -> None:
        self._match_controller = controller
        self._match_widgets_ready = False
        super().__init__(controller)
        self._build_match_end_group()
        self._build_match_summary_group()
        self._build_match_export_group()
        self._build_match_recovery_group()
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

    def _build_match_recovery_group(self) -> None:
        self.match_recovery_group = QGroupBox("stale対戦の復旧")
        layout = QFormLayout(self.match_recovery_group)
        self.abort_match_button = QPushButton("古い対戦を終了して選出へ戻る")
        self.abort_match_button.clicked.connect(self._on_abort_match)
        layout.addRow(
            QLabel("人間の明示操作でのみ終了します。履歴は保持されます。")
        )
        layout.addRow(self.abort_match_button)
        self._insert_before_stretch(self.match_recovery_group)

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._match_controller.refresh()
        super().render_view(current)
        if not self._match_widgets_ready:
            return
        if not isinstance(current, MatchOperatorView):
            if not current.persistence_reads_allowed:
                # A durable read is exactly what a persistence_reads_allowed=False
                # view must never trigger -- render safe placeholders instead of
                # refreshing, and never fabricate match-specific identity.
                self._render_match_placeholder_fallback()
                return
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

        recoverable = "ABORT_MATCH" in current.projection.secondary_actions
        self.match_recovery_group.setVisible(recoverable)
        self.abort_match_button.setEnabled(recoverable)

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

        if not current.persistence_reads_allowed:
            self._disable_match_mutation_controls()

    def _render_match_placeholder_fallback(self) -> None:
        """persistence_reads_allowed=False with a non-MatchOperatorView view.

        No match-specific data (outcome, export summary, ...) is available
        without a durable read, so every match-only group is hidden rather
        than showing fabricated or stale identity, and every match mutation
        control is force-disabled.
        """

        self.match_end_group.setVisible(False)
        self.match_summary_group.setVisible(False)
        self.match_export_group.setVisible(False)
        self.match_recovery_group.setVisible(False)
        self._disable_match_mutation_controls()

    def _disable_match_mutation_controls(self) -> None:
        """Fail-closed: force-disable every match-lifecycle control this
        subclass owns.

        The base ``MapleMainWindow._disable_mutation_controls`` cannot see
        these widgets, and this method's own state-based enablement above
        runs *before* this call within the same render pass -- so this must
        run last, unconditionally, whenever the rendered view was built
        without a durable DB read (cached safe fallback or no-cache
        PERSISTENCE_UNAVAILABLE). Retrying abort, or any other match
        mutation, while canonical state cannot be confirmed is not allowed;
        recovery only happens through a normal, durable refresh.
        """

        for widget in (
            self.outcome_box,
            self.outcome_confirm_checkbox,
            self.end_match_button,
            self.save_match_button,
            self.new_match_after_export_button,
            self.abort_match_button,
        ):
            widget.setEnabled(False)

    def _update_end_match_button(self, _value: object = None) -> None:
        if not self._match_widgets_ready:
            return
        if not self._persistence_reads_allowed:
            self.end_match_button.setEnabled(False)
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
        if not self._persistence_reads_allowed:
            return
        view = self._match_controller.end_match(
            self.outcome_box.currentText(),
            human_confirmed=self.outcome_confirm_checkbox.isChecked(),
        )
        self.render_view(view)

    def _build_abort_confirmation_dialog(self) -> QMessageBox:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("古い対戦の復旧")
        dialog.setText("現在の対戦を終了して、選出画面へ戻りますか？")
        dialog.setInformativeText(
            "session / matchの履歴は保持されます。provider送信、APPLY、turn記録は行いません。"
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
        return dialog

    def _on_abort_match(self, _checked: bool = False) -> None:
        if not self._persistence_reads_allowed:
            return
        dialog = self._build_abort_confirmation_dialog()
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return
        view = self._match_controller.abort_match(
            human_confirmed=True,
        )
        self.render_view(view)

    def _on_save_match(self, _checked: bool = False) -> None:
        if not self._persistence_reads_allowed:
            return
        self.render_view(self._match_controller.save_match_json())

    def _on_new_match_after_export(self, _checked: bool = False) -> None:
        if not self._persistence_reads_allowed:
            return
        self.render_view(self._match_controller.new_match_after_export())
