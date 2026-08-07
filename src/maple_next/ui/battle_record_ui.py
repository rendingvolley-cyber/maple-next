"""Issue #31 Bundle C: Battle Record 3-column UI on top of the accepted stack.

Reuses every existing group-builder method and signal wiring from the
``MapleMainWindow`` -> ... -> ``TurnSnapshotMatchFlowWindow`` (official)
chain unchanged -- this module never re-implements Selection flow, capture/
OCR polling, match export, or the turn-snapshot fixed-image flow. It only:

- reparents the already-built group widgets into a fixed header, a fixed
  18/52/30 three-column body, a fixed bottom operation bar, and a
  collapsible diagnostics drawer;
- adds new widgets for the Bundle A ``ConfirmedTurnState``/
  ``ActionResultDelta`` human review/correction surfaces (SELF/OPPONENT
  stat stages, status, side effects, weather, terrain, and their
  CHANGED/UNCHANGED/UNKNOWN counterparts) that did not previously exist in
  any UI;
- overrides the *existing* handler method names (not the button wiring --
  the buttons stay connected to the same bound-method names, so Python's
  normal MRO dispatch already routes their clicks to the overrides below)
  to also gather the new widgets' values and pass them through to
  :class:`maple_next.ui.turn_state_flow.TurnStateFlowController`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from maple_next.domain.enums import HpBucket
from maple_next.domain.turn_state import (
    FieldDelta,
    Known,
    ProvenanceStep,
    SideDelta,
    SideState,
)
from maple_next.ui.controller import OperatorView
from maple_next.ui.turn_snapshot_official_window import TurnSnapshotMatchFlowWindow
from maple_next.ui.turn_state_flow import TurnStateFlowController, TurnStateSummaryView

_BATTLE_RECORD_TAB_INDEX = 1
_HUMAN_INPUT = (ProvenanceStep.HUMAN_INPUT,)

_STAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("attack_stage", "こうげき"),
    ("defense_stage", "ぼうぎょ"),
    ("special_attack_stage", "とくこう"),
    ("special_defense_stage", "とくぼう"),
    ("speed_stage", "すばやさ"),
    ("accuracy_stage", "命中率"),
    ("evasion_stage", "回避率"),
)

_RICH_STATUS_LABELS = {
    "UNAVAILABLE": "利用不可",
    "IDLE": "待機中",
    "PENDING": "送信中…",
    "SUCCESS": "受領済み",
    "FAILED": "失敗",
    "STALE_OR_INVALID": "STALE/INVALID",
}


# ---------------------------------------------------------------------------
# Small reusable Known[...]/FieldDelta[...] field widgets. Every widget below
# defaults to UNKNOWN -- nothing here ever silently starts at a coerced
# default such as stage 0 or status NONE.
# ---------------------------------------------------------------------------


class _KnownIntField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(False)
        self.unknown_box = QCheckBox("不明")
        self.unknown_box.setChecked(True)
        self.unknown_box.toggled.connect(lambda checked: self.spin.setEnabled(not checked))
        layout.addWidget(self.spin)
        layout.addWidget(self.unknown_box)

    def to_known(self) -> Known[int]:
        if self.unknown_box.isChecked():
            return Known.unknown()
        return Known.confirmed(self.spin.value(), provenance_chain=_HUMAN_INPUT)

    def set_known(self, known: Known[int]) -> None:
        if known.is_confirmed and known.value is not None:
            self.unknown_box.setChecked(False)
            self.spin.setValue(int(known.value))
        else:
            self.unknown_box.setChecked(True)


class _KnownTextField(QWidget):
    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.line = QLineEdit()
        self.line.setPlaceholderText(placeholder)
        self.line.setEnabled(False)
        self.unknown_box = QCheckBox("不明")
        self.unknown_box.setChecked(True)
        self.unknown_box.toggled.connect(lambda checked: self.line.setEnabled(not checked))
        layout.addWidget(self.line, 1)
        layout.addWidget(self.unknown_box)

    def to_known(self) -> Known[str]:
        if self.unknown_box.isChecked():
            return Known.unknown()
        text = self.line.text().strip()
        if not text:
            return Known.unknown()
        return Known.confirmed(text, provenance_chain=_HUMAN_INPUT)

    def set_known(self, known: Known[str]) -> None:
        if known.is_confirmed and known.value is not None:
            self.unknown_box.setChecked(False)
            self.line.setText(known.value)
        else:
            self.unknown_box.setChecked(True)
            self.line.clear()


class _KnownHpField(QComboBox):
    def __init__(self) -> None:
        super().__init__()
        self.addItem("不明")
        for bucket in HpBucket:
            self.addItem(bucket.value)

    def to_known(self) -> Known[HpBucket]:
        if self.currentIndex() <= 0:
            return Known.unknown()
        return Known.confirmed(HpBucket(self.currentText()), provenance_chain=_HUMAN_INPUT)

    def set_known(self, known: Known[HpBucket]) -> None:
        if known.is_confirmed and known.value is not None:
            self.setCurrentText(known.value.value)
        else:
            self.setCurrentIndex(0)


class _KnownSideEffectsField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.line = QLineEdit()
        self.line.setPlaceholderText("やけど,こんらん (カンマ区切り。なければ空欄のまま)")
        self.line.setEnabled(False)
        self.unknown_box = QCheckBox("不明")
        self.unknown_box.setChecked(True)
        self.unknown_box.toggled.connect(lambda checked: self.line.setEnabled(not checked))
        layout.addWidget(self.line, 1)
        layout.addWidget(self.unknown_box)

    def to_known(self) -> Known[tuple[str, ...]]:
        if self.unknown_box.isChecked():
            return Known.unknown()
        parts = tuple(part.strip() for part in self.line.text().split(",") if part.strip())
        return Known.confirmed(parts, provenance_chain=_HUMAN_INPUT)

    def set_known(self, known: Known[tuple[str, ...]]) -> None:
        if known.is_confirmed and known.value is not None:
            self.unknown_box.setChecked(False)
            self.line.setText(", ".join(known.value))
        else:
            self.unknown_box.setChecked(True)
            self.line.clear()


class _DeltaIntField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(False)
        self.mode_box.currentTextChanged.connect(
            lambda text: self.spin.setEnabled(text == "CHANGED")
        )
        layout.addWidget(self.mode_box)
        layout.addWidget(self.spin)

    def to_delta(self) -> FieldDelta[int]:
        mode = self.mode_box.currentText()
        if mode == "CHANGED":
            return FieldDelta.changed(self.spin.value(), provenance_chain=_HUMAN_INPUT)
        if mode == "UNCHANGED":
            return FieldDelta.unchanged()
        return FieldDelta.unknown()


class _DeltaTextField(QWidget):
    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.line = QLineEdit()
        self.line.setPlaceholderText(placeholder)
        self.line.setEnabled(False)
        self.mode_box.currentTextChanged.connect(
            lambda text: self.line.setEnabled(text == "CHANGED")
        )
        layout.addWidget(self.mode_box)
        layout.addWidget(self.line, 1)

    def to_delta(self) -> FieldDelta[str]:
        mode = self.mode_box.currentText()
        if mode == "CHANGED":
            text = self.line.text().strip()
            if not text:
                return FieldDelta.unknown()
            return FieldDelta.changed(text, provenance_chain=_HUMAN_INPUT)
        if mode == "UNCHANGED":
            return FieldDelta.unchanged()
        return FieldDelta.unknown()


class _DeltaHpField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.value_box = QComboBox()
        for bucket in HpBucket:
            self.value_box.addItem(bucket.value)
        self.value_box.setEnabled(False)
        self.mode_box.currentTextChanged.connect(
            lambda text: self.value_box.setEnabled(text == "CHANGED")
        )
        layout.addWidget(self.mode_box)
        layout.addWidget(self.value_box)

    def to_delta(self) -> FieldDelta[HpBucket]:
        mode = self.mode_box.currentText()
        if mode == "CHANGED":
            return FieldDelta.changed(
                HpBucket(self.value_box.currentText()), provenance_chain=_HUMAN_INPUT
            )
        if mode == "UNCHANGED":
            return FieldDelta.unchanged()
        return FieldDelta.unknown()


class _DeltaSideEffectsField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.line = QLineEdit()
        self.line.setPlaceholderText("やけど,こんらん")
        self.line.setEnabled(False)
        self.mode_box.currentTextChanged.connect(
            lambda text: self.line.setEnabled(text == "CHANGED")
        )
        layout.addWidget(self.mode_box)
        layout.addWidget(self.line, 1)

    def to_delta(self) -> FieldDelta[tuple[str, ...]]:
        mode = self.mode_box.currentText()
        if mode == "CHANGED":
            parts = tuple(part.strip() for part in self.line.text().split(",") if part.strip())
            return FieldDelta.changed(parts, provenance_chain=_HUMAN_INPUT)
        if mode == "UNCHANGED":
            return FieldDelta.unchanged()
        return FieldDelta.unknown()


class _SideStateEditor(QGroupBox):
    """One side's SELF/OPPONENT current-state review/correction fields."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        layout = QFormLayout(self)
        self.status_field = _KnownTextField("状態異常 (例: burn / paralysis)")
        layout.addRow("状態異常", self.status_field)
        self.stage_fields: dict[str, _KnownIntField] = {}
        for key, label in _STAGE_FIELDS:
            field = _KnownIntField()
            self.stage_fields[key] = field
            layout.addRow(label, field)
        self.side_effects_field = _KnownSideEffectsField()
        layout.addRow("その他の状態", self.side_effects_field)

    def to_side_state(self, *, active: Known[str], hp_bucket: Known[HpBucket]) -> SideState:
        return SideState(
            active=active,
            hp_bucket=hp_bucket,
            status=self.status_field.to_known(),
            attack_stage=self.stage_fields["attack_stage"].to_known(),
            defense_stage=self.stage_fields["defense_stage"].to_known(),
            special_attack_stage=self.stage_fields["special_attack_stage"].to_known(),
            special_defense_stage=self.stage_fields["special_defense_stage"].to_known(),
            speed_stage=self.stage_fields["speed_stage"].to_known(),
            accuracy_stage=self.stage_fields["accuracy_stage"].to_known(),
            evasion_stage=self.stage_fields["evasion_stage"].to_known(),
            side_effects=self.side_effects_field.to_known(),
        )

    def load_side_state(self, side: SideState) -> None:
        self.status_field.set_known(side.status)
        self.stage_fields["attack_stage"].set_known(side.attack_stage)
        self.stage_fields["defense_stage"].set_known(side.defense_stage)
        self.stage_fields["special_attack_stage"].set_known(side.special_attack_stage)
        self.stage_fields["special_defense_stage"].set_known(side.special_defense_stage)
        self.stage_fields["speed_stage"].set_known(side.speed_stage)
        self.stage_fields["accuracy_stage"].set_known(side.accuracy_stage)
        self.stage_fields["evasion_stage"].set_known(side.evasion_stage)
        self.side_effects_field.set_known(side.side_effects)

    def clear(self) -> None:
        self.status_field.set_known(Known.unknown())
        for field in self.stage_fields.values():
            field.set_known(Known.unknown())
        self.side_effects_field.set_known(Known.unknown())


class _SideDeltaEditor(QGroupBox):
    """One side's SELF/OPPONENT CHANGED/UNCHANGED/UNKNOWN result fields."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        layout = QFormLayout(self)
        self.active_field = _DeltaTextField("交代後の active（変更時のみ）")
        layout.addRow("active", self.active_field)
        self.hp_field = _DeltaHpField()
        layout.addRow("HP bucket", self.hp_field)
        self.status_field = _DeltaTextField("状態異常（変更時のみ）")
        layout.addRow("状態異常", self.status_field)
        self.stage_fields: dict[str, _DeltaIntField] = {}
        for key, label in _STAGE_FIELDS:
            field = _DeltaIntField()
            self.stage_fields[key] = field
            layout.addRow(label, field)
        self.side_effects_field = _DeltaSideEffectsField()
        layout.addRow("その他の状態", self.side_effects_field)

    def to_side_delta(self) -> SideDelta:
        return SideDelta(
            active=self.active_field.to_delta(),
            hp_bucket=self.hp_field.to_delta(),
            status=self.status_field.to_delta(),
            attack_stage=self.stage_fields["attack_stage"].to_delta(),
            defense_stage=self.stage_fields["defense_stage"].to_delta(),
            special_attack_stage=self.stage_fields["special_attack_stage"].to_delta(),
            special_defense_stage=self.stage_fields["special_defense_stage"].to_delta(),
            speed_stage=self.stage_fields["speed_stage"].to_delta(),
            accuracy_stage=self.stage_fields["accuracy_stage"].to_delta(),
            evasion_stage=self.stage_fields["evasion_stage"].to_delta(),
            side_effects=self.side_effects_field.to_delta(),
        )


class _CollapsibleSection(QWidget):
    """Minimal expand/collapse container used for the diagnostics drawer
    and the match-terminal-flow panel -- keeps the normal operator screen
    from being pushed down by technical/dev-only or end-of-match content.
    """

    def __init__(self, title: str, *, start_expanded: bool = False) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self._title = title
        self.toggle_button = QPushButton()
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(start_expanded)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(4, 4, 4, 4)
        self.content.setVisible(start_expanded)
        self.toggle_button.toggled.connect(self._on_toggled)
        outer.addWidget(self.toggle_button)
        outer.addWidget(self.content)
        self._on_toggled(start_expanded)

    def _on_toggled(self, expanded: bool) -> None:
        self.content.setVisible(expanded)
        arrow = "▾" if expanded else "▸"
        self.toggle_button.setText(f"{arrow} {self._title}")

    def add_widget(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget)


class BattleRecordUiWindow(TurnSnapshotMatchFlowWindow):
    """Fixed header / 18-52-30 body / fixed bottom bar Battle Record UI."""

    def __init__(self, controller: TurnStateFlowController, *, ocr_data_directory: Path) -> None:
        super().__init__(controller, ocr_data_directory=ocr_data_directory)
        self._bundle_c_controller: TurnStateFlowController = controller
        self._build_bundle_c_state_widgets()
        self._restructure_battle_record_layout()
        self.render_view()

    # -- new Bundle A/B widgets ------------------------------------------------

    def _build_bundle_c_state_widgets(self) -> None:
        self.current_state_group = QGroupBox("現在のTurn state — 人間が確認・修正")
        state_layout = QVBoxLayout(self.current_state_group)
        top_row = QFormLayout()
        self.weather_field = _KnownTextField("天候 (例: sun / rain)")
        self.terrain_field = _KnownTextField("フィールド (例: electric_terrain)")
        top_row.addRow("天候", self.weather_field)
        top_row.addRow("フィールド", self.terrain_field)
        state_layout.addLayout(top_row)
        sides_row = QHBoxLayout()
        self.self_state_editor = _SideStateEditor("自分")
        self.opponent_state_editor = _SideStateEditor("相手")
        sides_row.addWidget(self.self_state_editor)
        sides_row.addWidget(self.opponent_state_editor)
        state_layout.addLayout(sides_row)
        self.current_state_draft_label = QLabel()
        self.current_state_draft_label.setWordWrap(True)
        self.current_state_draft_label.setStyleSheet("color: #b45309; font-weight: 600;")
        state_layout.addWidget(self.current_state_draft_label)

        self.action_result_delta_group = QGroupBox(
            "ActionResultDelta — CHANGED / UNCHANGED / UNKNOWN を明示"
        )
        delta_layout = QVBoxLayout(self.action_result_delta_group)
        delta_top_row = QFormLayout()
        self.weather_delta_field = _DeltaTextField("天候（変更時のみ）")
        self.terrain_delta_field = _DeltaTextField("フィールド（変更時のみ）")
        delta_top_row.addRow("天候", self.weather_delta_field)
        delta_top_row.addRow("フィールド", self.terrain_delta_field)
        delta_layout.addLayout(delta_top_row)
        delta_sides_row = QHBoxLayout()
        self.self_delta_editor = _SideDeltaEditor("自分の結果")
        self.opponent_delta_editor = _SideDeltaEditor("相手の結果")
        delta_sides_row.addWidget(self.self_delta_editor)
        delta_sides_row.addWidget(self.opponent_delta_editor)
        delta_layout.addLayout(delta_sides_row)

        self.rich_gemini_group = QGroupBox("Gemini Turn Advice — rich state")
        rich_layout = QFormLayout(self.rich_gemini_group)
        self.rich_gemini_status_label = QLabel()
        self.rich_gemini_denial_label = QLabel()
        self.rich_gemini_denial_label.setWordWrap(True)
        rich_layout.addRow("送信可否", self.rich_gemini_status_label)
        rich_layout.addRow("理由", self.rich_gemini_denial_label)

    # -- layout restructuring ---------------------------------------------------

    def _restructure_battle_record_layout(self) -> None:
        outer_layout = self.centralWidget().layout()
        assert outer_layout is not None
        status_frame = self.application_mode_label.parentWidget()
        assert status_frame is not None
        outer_layout.removeWidget(status_frame)

        left_container = self._left_column_layout.parentWidget()
        center_container = self._center_column_layout.parentWidget()
        right_container = self._right_column_layout.parentWidget()

        # Extract the 5 primary operation buttons from wherever the base
        # chain built them; the fixed bottom bar owns them from now on.
        self._extract_widget(self.ready_group, self.start_turn_button)
        self._extract_widget(self.turn_facts_group, self.confirm_turn_facts_button)
        self._extract_widget(self.actual_action_group, self.record_action_button)
        self._extract_widget(self.history_group, self.next_turn_button)
        gemini_send_button = getattr(self, "turn_gemini_send_button", None)
        if gemini_send_button is not None:
            self._extract_widget(self.turn_gemini_box, gemini_send_button)
            self.turn_gemini_box.setVisible(False)

        self.start_turn_button.setText("Turn撮影")
        self.confirm_turn_facts_button.setText("facts/state確定")
        self.record_action_button.setText("行動・結果記録")
        self.next_turn_button.setText("NEXT TURN")
        if gemini_send_button is not None:
            gemini_send_button.setText("Gemini送信")

        # -- diagnostics drawer: technical/dev-only material -------------------
        self.diagnostics_drawer = _CollapsibleSection("診断情報 / Diagnostics")
        self.diagnostics_drawer.add_widget(status_frame)
        ocr_group = getattr(self, "ocr_candidates_group", None)
        if ocr_group is not None:
            self._detach_from_parent_layout(ocr_group)
            self.diagnostics_drawer.add_widget(ocr_group)
        mock_turn_group = getattr(self, "mock_turn_group", None)
        if mock_turn_group is not None:
            self._detach_from_parent_layout(mock_turn_group)
            self.diagnostics_drawer.add_widget(mock_turn_group)
        capture_freshness = getattr(self, "capture_freshness_label", None)
        capture_device = getattr(self, "capture_device_label", None)
        turn_snapshot_frame_label = getattr(self, "turn_snapshot_frame_label", None)
        if turn_snapshot_frame_label is not None:
            frame_id_row = QWidget()
            frame_id_layout = QFormLayout(frame_id_row)
            if capture_freshness is not None:
                frame_id_layout.addRow("フレーム鮮度", QLabel(capture_freshness.text()))
            if capture_device is not None:
                frame_id_layout.addRow("キャプチャ機器", QLabel(capture_device.text()))
            self.diagnostics_drawer.add_widget(frame_id_row)

        # -- terminal-flow drawer: match end/export/recovery -------------------
        self.terminal_flow_drawer = _CollapsibleSection(
            "試合終了・Export・復旧"
        )
        for name in (
            "match_end_group",
            "match_summary_group",
            "match_export_group",
            "match_recovery_group",
        ):
            group = getattr(self, name, None)
            if group is not None:
                self._detach_from_parent_layout(group)
                self.terminal_flow_drawer.add_widget(group)

        # -- fixed header for the Battle Record tab -----------------------------
        header_widget = QWidget()
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.battle_context_label = QLabel()
        self.battle_context_label.setWordWrap(True)
        self.battle_context_label.setStyleSheet("font-weight: 600;")
        header_layout.addWidget(self.battle_context_label)
        drawer_row = QHBoxLayout()
        drawer_row.addWidget(self.diagnostics_drawer)
        drawer_row.addWidget(self.terminal_flow_drawer)
        drawer_row.addStretch(1)
        header_layout.addLayout(drawer_row)

        # -- left / center / right, plus the two new Bundle A/B groups ---------
        self._center_column_layout.addWidget(self.current_state_group)
        self._center_column_layout.addWidget(self.action_result_delta_group)
        self._right_column_layout.insertWidget(0, self.rich_gemini_group)

        # Only left (confirmed log) and right (Gemini detail) get their own
        # scroll container -- center is the primary work area and must never
        # scroll as a whole (spec Bundle C section 2/4).
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left_container)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_container)

        body_row = QHBoxLayout()
        body_row.addWidget(left_scroll, 18)
        body_row.addWidget(center_container, 52)
        body_row.addWidget(right_scroll, 30)

        bottom_bar = QWidget()
        bottom_bar_layout = QHBoxLayout(bottom_bar)
        bottom_bar_layout.addWidget(self.start_turn_button)
        bottom_bar_layout.addWidget(self.confirm_turn_facts_button)
        if gemini_send_button is not None:
            bottom_bar_layout.addWidget(gemini_send_button)
            self._bundle_c_gemini_send_button = gemini_send_button
        bottom_bar_layout.addWidget(self.record_action_button)
        bottom_bar_layout.addWidget(self.next_turn_button)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(6, 6, 6, 6)
        page_layout.addWidget(header_widget)
        page_layout.addLayout(body_row, 1)
        page_layout.addWidget(bottom_bar)

        self.header_tabs.removeTab(_BATTLE_RECORD_TAB_INDEX)
        self.header_tabs.insertTab(_BATTLE_RECORD_TAB_INDEX, page, "バトルレコード")

    @staticmethod
    def _extract_widget(container: QWidget, widget: QWidget) -> None:
        layout = container.layout()
        if layout is not None:
            layout.removeWidget(widget)

    @staticmethod
    def _detach_from_parent_layout(widget: QWidget) -> None:
        parent = widget.parentWidget()
        if parent is None:
            return
        parent_layout = parent.layout()
        if parent_layout is not None:
            parent_layout.removeWidget(widget)

    # -- render ------------------------------------------------------------------

    def render_view(self, view: OperatorView | None = None) -> None:
        super().render_view(view)
        if not hasattr(self, "current_state_group"):
            return
        current = view if view is not None else self._bundle_c_controller.refresh()
        projection = current.projection
        summary = self._bundle_c_controller.turn_state_summary()

        self.start_turn_button.setText("Turn撮影")
        self.confirm_turn_facts_button.setText("facts/state確定")
        self.record_action_button.setText("行動・結果記録")
        self.next_turn_button.setText("NEXT TURN")

        context_parts = [
            f"Turn {projection.turn_number}" if projection.turn_number else "Turn —",
            projection.session_state or "—",
            f"provider={projection.provider_status}",
        ]
        self.battle_context_label.setText(" / ".join(context_parts))

        turn_state = projection.session_state in {
            "TURN_CAPTURE_PENDING",
            "TURN_REVIEWED",
            "TURN_RECORDED",
        }
        self.current_state_group.setVisible(turn_state)
        editable = projection.session_state == "TURN_CAPTURE_PENDING"
        for widget in (
            self.weather_field,
            self.terrain_field,
            self.self_state_editor,
            self.opponent_state_editor,
        ):
            widget.setEnabled(editable and current.persistence_reads_allowed)

        draft_not_yet_promoted = summary.open_draft is not None and (
            summary.confirmed_state is None
            or summary.confirmed_state.identity != summary.open_draft.identity
        )
        if draft_not_yet_promoted:
            self.current_state_draft_label.setText(
                "未確認 draft からの引き継ぎです。"
                "人間の確認が必要です。"
            )
            self.current_state_draft_label.setVisible(True)
            if editable and self._last_rendered_session_state == "TURN_CAPTURE_PENDING":
                self._load_draft_into_state_editor(summary)
        else:
            self.current_state_draft_label.setVisible(False)

        legacy_gemini_box = getattr(self, "turn_gemini_box", None)
        if legacy_gemini_box is not None:
            legacy_gemini_box.setVisible(False)

        self.action_result_delta_group.setVisible(
            projection.primary_cta == "RECORD_ACTUAL_ACTION"
        )

        self.rich_gemini_group.setVisible(projection.primary_cta == "REQUEST_TURN_ADVICE")
        status = self._bundle_c_controller.rich_turn_advice_gemini_status()
        self.rich_gemini_status_label.setText(_RICH_STATUS_LABELS.get(status.status, status.status))
        if summary.provider_ready:
            self.rich_gemini_denial_label.setText("provider-ready")
        else:
            reasons = ", ".join(
                self._bundle_c_controller.denial_reason_message(code)
                for code in summary.provider_ready_denial_reasons
            )
            self.rich_gemini_denial_label.setText(reasons or "確認中")

        gemini_button = getattr(self, "_bundle_c_gemini_send_button", None)
        if gemini_button is not None:
            gemini_button.setEnabled(
                current.persistence_reads_allowed
                and projection.primary_cta == "REQUEST_TURN_ADVICE"
                and summary.provider_ready
                and status.status != "PENDING"
            )

        self.next_turn_button.setEnabled(
            self.next_turn_button.isEnabled() and projection.primary_cta == "NEXT_TURN"
        )

        if not current.persistence_reads_allowed:
            for lockable_widget in (
                self.weather_field,
                self.terrain_field,
                self.self_state_editor,
                self.opponent_state_editor,
                self.self_delta_editor,
                self.opponent_delta_editor,
                self.weather_delta_field,
                self.terrain_delta_field,
            ):
                lockable_widget.setEnabled(False)

    def _load_draft_into_state_editor(self, summary: TurnStateSummaryView) -> None:
        draft = summary.open_draft
        if draft is None:
            return
        self.weather_field.set_known(draft.weather)
        self.terrain_field.set_known(draft.terrain)
        self.self_state_editor.load_side_state(draft.self_side)
        self.opponent_state_editor.load_side_state(draft.opponent_side)

    # -- overridden handlers: gather the new widgets, then delegate ------------

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        switches = [
            checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()
        ]
        self_active_known = _active_known_from_combo(self.self_active_box)
        opponent_active_known = _active_known_from_line(self.opponent_active_input)
        self_hp_known = _hp_known_from_combo(self.self_hp_box)
        opponent_hp_known = _hp_known_from_combo(self.opponent_hp_box)
        self_side = self.self_state_editor.to_side_state(
            active=self_active_known, hp_bucket=self_hp_known
        )
        opponent_side = self.opponent_state_editor.to_side_state(
            active=opponent_active_known, hp_bucket=opponent_hp_known
        )
        view = self._bundle_c_controller.confirm_turn_facts(
            self_active=self.self_active_box.currentText(),
            opponent_active=self.opponent_active_input.text(),
            self_hp=self.self_hp_box.currentText(),
            opponent_hp=self.opponent_hp_box.currentText(),
            legal_moves=moves,
            legal_switches=switches,
            human_note=self.turn_note_input.text(),
            human_confirmed=True,
            self_side=self_side,
            opponent_side=opponent_side,
            weather=self.weather_field.to_known(),
            terrain=self.terrain_field.to_known(),
        )
        self.render_view(view)

    def _on_record_action(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        opponent_type = self.opponent_action_type_box.currentText()
        if opponent_type == "選択してください":
            opponent_type = ""
        view = self._bundle_c_controller.record_actual_action(
            action_type=self.actual_action_type_box.currentText(),
            action_name=self.actual_action_name_box.currentText(),
            human_confirmed=self.actual_action_confirm_checkbox.isChecked(),
            opponent_action_type=opponent_type,
            opponent_action_name=self.opponent_action_name_input.text(),
            action_order=self.action_order_box.currentText(),
            self_side_delta=self.self_delta_editor.to_side_delta(),
            opponent_side_delta=self.opponent_delta_editor.to_side_delta(),
            weather_delta=self.weather_delta_field.to_delta(),
            terrain_delta=self.terrain_delta_field.to_delta(),
        )
        self.render_view(view)

    def _on_trusted_send_turn_to_gemini(self) -> None:
        if not self._persistence_reads_allowed:
            return
        warnings = tuple(
            part.strip()
            for part in self.mock_turn_warnings_input.text().split(";")
            if part.strip()
        )
        view = self._bundle_c_controller.send_rich_turn_advice_to_gemini(
            action_type=self.mock_turn_action_type_box.currentText(),
            action_name=self.mock_turn_action_name_box.currentText(),
            opponent_prediction=self.mock_turn_prediction_input.text(),
            rationale=self.mock_turn_rationale_input.text(),
            warnings=warnings,
            on_result=self.render_view,
        )
        self.render_view(view)


def _active_known_from_combo(box: QComboBox) -> Known[str]:
    text = box.currentText().strip()
    if not text or text == "選択してください":
        return Known.unknown()
    return Known.confirmed(text, provenance_chain=_HUMAN_INPUT)


def _active_known_from_line(field: QLineEdit) -> Known[str]:
    text = field.text().strip()
    if not text:
        return Known.unknown()
    return Known.confirmed(text, provenance_chain=_HUMAN_INPUT)


def _hp_known_from_combo(box: QComboBox) -> Known[HpBucket]:
    text = box.currentText().strip()
    if not text or text == "選択してください":
        return Known.unknown()
    return Known.confirmed(HpBucket(text), provenance_chain=_HUMAN_INPUT)
