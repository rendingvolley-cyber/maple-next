"""PySide6 Widgets shell for the manual Selection APPLY operator flow."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from maple_next.ui.controller import OperatorView, SelectionFlowController


_CTA_LABELS = {
    "CREATE_NEW_MATCH": "NEW MATCH",
    "CONFIRM_SELECTION_FACTS": "自分と相手の6体を確認",
    "REQUEST_SELECTION_ADVICE": "MOCK Selection Adviceを投入",
    "WAIT_SELECTION_ADVICE": "Selection Adviceを待機",
    "APPLY_SELECTION": "実際の選出を確認してAPPLY",
    "START_TURN_CAPTURE": "START TURN CAPTURE",
    "RESOLVE_DELIVERY_UNKNOWN": "provider結果不明を解決",
}

_MESSAGE_LABELS = {
    "NO_ACTIVE_MATCH": "対戦を開始してください。",
    "SELECTION_FACTS_REQUIRED": "自分と相手の6体を手動入力してください。",
    "SELECTION_FACTS_CONFIRMED": "確認済み6体を使ってMOCK Adviceを投入してください。",
    "SELECTION_ADVICE_PENDING": "MOCK Adviceを処理しています。",
    "SELECTION_ADVICE_READY": "実際に選んだ3体と先発を指定してください。",
    "BATTLE_READY": "Selection APPLYが完了しました。Turn captureは後続Issueです。",
    "PROVIDER_DELIVERY_UNKNOWN": "前回のprovider結果が不明です。新しい送信は停止中です。",
}


class MapleMainWindow(QMainWindow):
    """Thin renderer over SelectionFlowController and DomainProjection."""

    def __init__(self, controller: SelectionFlowController) -> None:
        super().__init__()
        self._controller = controller
        self._loaded_team: tuple[str, ...] = ()
        self._build_ui()
        self.render()

    def _build_ui(self) -> None:
        self.setWindowTitle("Maple Next — Battle Record")
        self.resize(920, 820)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        root = QWidget(scroll)
        self._root_layout = QVBoxLayout(root)
        self._root_layout.setContentsMargins(24, 24, 24, 24)
        self._root_layout.setSpacing(16)

        heading = QLabel("今なにをすべきか")
        heading.setObjectName("nextActionHeading")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.primary_cta_label = QLabel()
        self.primary_cta_label.setObjectName("primaryCta")
        self.primary_cta_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.guidance_label = QLabel()
        self.guidance_label.setWordWrap(True)
        self.guidance_label.setObjectName("guidance")

        self.error_label = QLabel()
        self.error_label.setObjectName("validationError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "QLabel { color: #9d1c1c; background: #fff0f0; border: 1px solid #e2a4a4; "
            "padding: 10px; border-radius: 4px; }"
        )

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QFormLayout(status_frame)
        self.application_mode_label = QLabel()
        self.session_state_label = QLabel()
        self.provider_status_label = QLabel()
        self.battle_revision_label = QLabel()
        status_layout.addRow("Application mode", self.application_mode_label)
        status_layout.addRow("Session state", self.session_state_label)
        status_layout.addRow("Provider status", self.provider_status_label)
        status_layout.addRow("Battle revision", self.battle_revision_label)

        self.new_match_button = QPushButton("NEW MATCH")
        self.new_match_button.setObjectName("newMatchButton")
        self.new_match_button.clicked.connect(self._on_new_match)

        self._root_layout.addWidget(heading)
        self._root_layout.addWidget(self.primary_cta_label)
        self._root_layout.addWidget(self.guidance_label)
        self._root_layout.addWidget(self.error_label)
        self._root_layout.addWidget(status_frame)
        self._root_layout.addWidget(self.new_match_button)

        self._build_selection_facts_group()
        self._build_mock_advice_group()
        self._build_advice_display_group()
        self._build_actual_selection_group()
        self._build_battle_ready_group()
        self._root_layout.addStretch(1)

        scroll.setWidget(root)
        self.setCentralWidget(scroll)

    def _build_selection_facts_group(self) -> None:
        self.selection_facts_group = QGroupBox("Selection facts — 手動入力")
        layout = QGridLayout(self.selection_facts_group)
        layout.addWidget(QLabel("自分の6体"), 0, 0)
        layout.addWidget(QLabel("相手の6体"), 0, 1)

        self.self_team_inputs: list[QLineEdit] = []
        self.opponent_team_inputs: list[QLineEdit] = []
        for index in range(6):
            self_input = QLineEdit()
            self_input.setPlaceholderText(f"自分 {index + 1}体目")
            opponent_input = QLineEdit()
            opponent_input.setPlaceholderText(f"相手 {index + 1}体目")
            self.self_team_inputs.append(self_input)
            self.opponent_team_inputs.append(opponent_input)
            layout.addWidget(self_input, index + 1, 0)
            layout.addWidget(opponent_input, index + 1, 1)

        self.confirm_facts_button = QPushButton("6体を確認")
        self.confirm_facts_button.setObjectName("confirmSelectionFactsButton")
        self.confirm_facts_button.clicked.connect(self._on_confirm_facts)
        layout.addWidget(self.confirm_facts_button, 7, 0, 1, 2)
        self._root_layout.addWidget(self.selection_facts_group)

    def _build_mock_advice_group(self) -> None:
        self.mock_group = QGroupBox("開発用 MOCK Selection Advice — ネットワーク送信なし")
        layout = QFormLayout(self.mock_group)
        self.mock_selection_boxes: list[QComboBox] = []
        for index in range(3):
            box = QComboBox()
            box.currentTextChanged.connect(self._update_mock_lead_options)
            self.mock_selection_boxes.append(box)
            layout.addRow(f"提案 {index + 1}体目", box)
        self.mock_lead_box = QComboBox()
        layout.addRow("提案先発", self.mock_lead_box)
        self.mock_submit_button = QPushButton("MOCK Adviceを投入")
        self.mock_submit_button.setObjectName("submitMockAdviceButton")
        self.mock_submit_button.clicked.connect(self._on_submit_mock)
        layout.addRow(self.mock_submit_button)
        self._root_layout.addWidget(self.mock_group)

    def _build_advice_display_group(self) -> None:
        self.advice_group = QGroupBox("受領したAdvice（MOCK）")
        layout = QFormLayout(self.advice_group)
        self.advice_three_label = QLabel()
        self.advice_lead_label = QLabel()
        layout.addRow("提案3体", self.advice_three_label)
        layout.addRow("提案先発", self.advice_lead_label)
        self._root_layout.addWidget(self.advice_group)

    def _build_actual_selection_group(self) -> None:
        self.actual_group = QGroupBox("実際の選出 — 人間が決定")
        layout = QVBoxLayout(self.actual_group)
        helper = QLabel("MOCK提案と異なる合法な3体・先発を選べます。自動コピーはしません。")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        grid = QGridLayout()
        self.actual_checkboxes: list[QCheckBox] = []
        for index in range(6):
            checkbox = QCheckBox()
            checkbox.toggled.connect(self._update_actual_controls)
            self.actual_checkboxes.append(checkbox)
            grid.addWidget(checkbox, index // 2, index % 2)
        layout.addLayout(grid)

        lead_row = QHBoxLayout()
        lead_row.addWidget(QLabel("実際の先発"))
        self.actual_lead_box = QComboBox()
        self.actual_lead_box.currentTextChanged.connect(self._update_actual_controls)
        lead_row.addWidget(self.actual_lead_box, 1)
        layout.addLayout(lead_row)

        self.apply_confirm_checkbox = QCheckBox("この3体と先発を実際の選出としてAPPLYします")
        self.apply_confirm_checkbox.toggled.connect(self._update_actual_controls)
        layout.addWidget(self.apply_confirm_checkbox)
        self.apply_button = QPushButton("APPLY")
        self.apply_button.setObjectName("applySelectionButton")
        self.apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self.apply_button)
        self._root_layout.addWidget(self.actual_group)

    def _build_battle_ready_group(self) -> None:
        self.ready_group = QGroupBox("BATTLE_READY")
        layout = QFormLayout(self.ready_group)
        self.actual_three_label = QLabel()
        self.actual_lead_label = QLabel()
        self.actual_backline_label = QLabel()
        layout.addRow("実際の3体", self.actual_three_label)
        layout.addRow("実際の先発", self.actual_lead_label)
        layout.addRow("控え", self.actual_backline_label)
        scope_note = QLabel("START TURN CAPTUREは次のIssueで実装します。現在は自動進行しません。")
        scope_note.setWordWrap(True)
        layout.addRow(scope_note)
        self._root_layout.addWidget(self.ready_group)

    def render(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._controller.refresh()
        projection = current.projection

        self.application_mode_label.setText(projection.application_mode)
        self.session_state_label.setText(projection.session_state or "—")
        self.provider_status_label.setText(projection.provider_status)
        self.battle_revision_label.setText(
            str(projection.battle_revision) if projection.battle_revision is not None else "—"
        )
        self.primary_cta_label.setText(
            _CTA_LABELS.get(projection.primary_cta, projection.primary_cta)
        )
        self.guidance_label.setText(_MESSAGE_LABELS.get(projection.message, projection.message))
        self.error_label.setText(current.error_message or "")
        self.error_label.setVisible(current.error_message is not None)

        self.new_match_button.setVisible(projection.primary_cta == "CREATE_NEW_MATCH")
        self.new_match_button.setEnabled(projection.primary_cta_enabled)

        selection_open = projection.session_state == "SELECTION_OPEN"
        self.selection_facts_group.setVisible(selection_open)
        facts_editable = projection.primary_cta == "CONFIRM_SELECTION_FACTS"
        self.confirm_facts_button.setEnabled(facts_editable)
        for field in (*self.self_team_inputs, *self.opponent_team_inputs):
            field.setEnabled(facts_editable)

        if current.self_team and current.self_team != self._loaded_team:
            self._loaded_team = current.self_team
            self._populate_team_controls(current.self_team)
            for field, value in zip(self.self_team_inputs, current.self_team, strict=True):
                field.setText(value)
            for field, value in zip(self.opponent_team_inputs, current.opponent_team, strict=True):
                field.setText(value)

        self.mock_group.setVisible(projection.primary_cta == "REQUEST_SELECTION_ADVICE")
        self.advice_group.setVisible(projection.current_selection_advice_id is not None)
        if current.advice is not None:
            self.advice_three_label.setText(" / ".join(current.advice.selected_three))
            self.advice_lead_label.setText(current.advice.lead)

        self.actual_group.setVisible(projection.primary_cta == "APPLY_SELECTION")
        self.ready_group.setVisible(projection.primary_cta == "START_TURN_CAPTURE")
        if current.applied_selection is not None:
            self.actual_three_label.setText(" / ".join(current.applied_selection.selected_three))
            self.actual_lead_label.setText(current.applied_selection.lead)
            self.actual_backline_label.setText(" / ".join(current.applied_selection.backline))
        self._update_actual_controls()

    def _populate_team_controls(self, team: Sequence[str]) -> None:
        for box in self.mock_selection_boxes:
            box.blockSignals(True)
            box.clear()
            box.addItem("選択してください")
            box.addItems(list(team))
            box.blockSignals(False)
        self.mock_lead_box.clear()
        self.mock_lead_box.addItem("選択してください")
        for checkbox, name in zip(self.actual_checkboxes, team, strict=True):
            checkbox.setText(name)
            checkbox.setChecked(False)
            checkbox.setVisible(True)
        self.actual_lead_box.clear()
        self.actual_lead_box.addItem("選択してください")
        self.apply_confirm_checkbox.setChecked(False)
        self._update_mock_lead_options()

    def _update_mock_lead_options(self, _text: str = "") -> None:
        selected = self._selected_combo_values(self.mock_selection_boxes)
        previous = self.mock_lead_box.currentText()
        self.mock_lead_box.blockSignals(True)
        self.mock_lead_box.clear()
        self.mock_lead_box.addItem("選択してください")
        for name in selected:
            if name not in {"", "選択してください"} and name not in self._combo_items(
                self.mock_lead_box
            ):
                self.mock_lead_box.addItem(name)
        if previous in self._combo_items(self.mock_lead_box):
            self.mock_lead_box.setCurrentText(previous)
        self.mock_lead_box.blockSignals(False)

    def _update_actual_controls(self, _value: object = None) -> None:
        selected = self._checked_actual_names()
        previous = self.actual_lead_box.currentText()
        self.actual_lead_box.blockSignals(True)
        self.actual_lead_box.clear()
        self.actual_lead_box.addItem("選択してください")
        self.actual_lead_box.addItems(selected)
        if previous in selected:
            self.actual_lead_box.setCurrentText(previous)
        self.actual_lead_box.blockSignals(False)
        lead = self.actual_lead_box.currentText()
        self.apply_button.setEnabled(
            len(selected) == 3
            and lead in selected
            and self.apply_confirm_checkbox.isChecked()
        )

    def _on_new_match(self, _checked: bool = False) -> None:
        self.render(self._controller.new_match())

    def _on_confirm_facts(self, _checked: bool = False) -> None:
        self_entries = [field.text() for field in self.self_team_inputs]
        opponent_entries = [field.text() for field in self.opponent_team_inputs]
        self.render(self._controller.confirm_selection_facts(self_entries, opponent_entries))

    def _on_submit_mock(self, _checked: bool = False) -> None:
        selected = self._selected_combo_values(self.mock_selection_boxes)
        self.render(self._controller.submit_mock_advice(selected, self.mock_lead_box.currentText()))

    def _on_apply(self, _checked: bool = False) -> None:
        self.render(
            self._controller.apply_selection(
                self._checked_actual_names(),
                self.actual_lead_box.currentText(),
                human_confirmed=self.apply_confirm_checkbox.isChecked(),
            )
        )

    def _checked_actual_names(self) -> list[str]:
        return [checkbox.text() for checkbox in self.actual_checkboxes if checkbox.isChecked()]

    @staticmethod
    def _selected_combo_values(boxes: Sequence[QComboBox]) -> list[str]:
        return [box.currentText() for box in boxes]

    @staticmethod
    def _combo_items(box: QComboBox) -> set[str]:
        return {box.itemText(index) for index in range(box.count())}
