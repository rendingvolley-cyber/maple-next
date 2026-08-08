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

from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
# Default official-launch client size (visual-parity remediation,
# 5223723582): resizable, not fixed -- the human can freely resize the
# window. 1280x720 remains the supported functional-minimum viewport
# (mock 5203292374/5203546707) and is exercised by manual resize, not by
# this constant.
_DEFAULT_LAUNCH_WIDTH = 1440
_DEFAULT_LAUNCH_HEIGHT = 900
_MINIMUM_SUPPORTED_WIDTH = 1280
_MINIMUM_SUPPORTED_HEIGHT = 720
# Fixed Turn image thumbnail width band from the accepted mock: ~230px at
# 1440x900 down to ~185px at 1280x720. A single bounded width (rather than
# a hardcoded pixel) lets Qt's layout shrink it within that band as the
# window is resized between the two targets.
_FIXED_TURN_IMAGE_MAX_WIDTH = 230
_FIXED_TURN_IMAGE_MIN_WIDTH = 185
# Column-ratio floors (5224076761): approximate the mock's 18/52/30 body
# split at both the 1280 and 1440 default widths (1280*0.18=230,
# 1440*0.18=259; 1280*0.30=384, 1440*0.30=432) without pinning an exact
# percentage, so center can still flex for its own content.
_LEFT_COLUMN_MINIMUM_WIDTH = 220
_RIGHT_COLUMN_MINIMUM_WIDTH = 340
# Bounds a single reflowed RECORD_ACTUAL_ACTION field so 6-across does not
# balloon center's minimum width past its share and collapse the left/right
# internal-scroll columns below their floor above.
_ACTUAL_ACTION_FIELD_MAX_WIDTH = 120
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
        layout.setSpacing(2)
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(False)
        self.spin.setMaximumWidth(46)
        self.unknown_box = QCheckBox("?")
        self.unknown_box.setToolTip("不明")
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
        layout.setSpacing(1)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setMaximumWidth(64)
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(False)
        self.spin.setMaximumWidth(40)
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


def _add_compact_stage_grid(
    layout: QVBoxLayout, fields: Mapping[str, QWidget], stage_names: tuple[tuple[str, str], ...]
) -> None:
    """4-columns-wide grid, label beside (not above) its field -- ~1 row of
    height per 4 fields instead of 2 (a label-over-field row pair each),
    so the two stacked stage grids in the fixed, non-scrolling center
    column (self/opponent state or self/opponent result-delta) claim
    roughly half the vertical space they used to for the same 7 fields.
    """

    grid = QGridLayout()
    grid.setHorizontalSpacing(3)
    grid.setVerticalSpacing(0)
    columns = 4
    for index, (key, label) in enumerate(stage_names):
        row, col = divmod(index, columns)
        cell = QWidget()
        cell_layout = QHBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(1)
        label_widget = QLabel(label)
        label_widget.setStyleSheet("font-size: 9px;")
        cell_layout.addWidget(label_widget)
        cell_layout.addWidget(fields[key])
        grid.addWidget(cell, row, col)
    layout.addLayout(grid)


class _SideStateEditor(QGroupBox):
    """One side's SELF/OPPONENT current-state review/correction fields."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("状態異常"))
        self.status_field = _KnownTextField("burn / paralysis")
        status_row.addWidget(self.status_field, 1)
        status_row.addWidget(QLabel("その他"))
        self.side_effects_field = _KnownSideEffectsField()
        status_row.addWidget(self.side_effects_field, 1)
        layout.addLayout(status_row)
        self.stage_fields: dict[str, _KnownIntField] = {}
        for key, _label in _STAGE_FIELDS:
            self.stage_fields[key] = _KnownIntField()
        _add_compact_stage_grid(layout, self.stage_fields, _STAGE_FIELDS)

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)
        # active/HP and status/other share one row (4 fields) instead of
        # two stacked rows -- this editor only ever appears in the
        # RECORD_ACTUAL_ACTION completion phase, where LIVE must stay the
        # dominant region even with this editor also on screen.
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("active"))
        self.active_field = _DeltaTextField("変更時のみ")
        top_row.addWidget(self.active_field, 1)
        top_row.addWidget(QLabel("HP"))
        self.hp_field = _DeltaHpField()
        top_row.addWidget(self.hp_field, 1)
        top_row.addWidget(QLabel("状態異常"))
        self.status_field = _DeltaTextField("変更時のみ")
        top_row.addWidget(self.status_field, 1)
        top_row.addWidget(QLabel("その他"))
        self.side_effects_field = _DeltaSideEffectsField()
        top_row.addWidget(self.side_effects_field, 1)
        layout.addLayout(top_row)
        self.stage_fields: dict[str, _DeltaIntField] = {}
        for key, _label in _STAGE_FIELDS:
            self.stage_fields[key] = _DeltaIntField()
        _add_compact_stage_grid(layout, self.stage_fields, _STAGE_FIELDS)

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
        self._apply_default_launch_geometry()
        self.render_view()

    def _apply_default_launch_geometry(self) -> None:
        """Default official-launch client size is 1440x900 (resizable).

        Not a fixed size -- ``setMinimumSize`` only floors manual resize at
        the 1280x720 functional-minimum viewport; the human can resize
        larger or down to that floor freely. When the available screen work
        area is smaller than 1440x900, degrade to the largest safe size
        that still keeps at least the 1280x720 floor when the screen can
        offer it, rather than opening off-screen.
        """

        self.setMinimumSize(_MINIMUM_SUPPORTED_WIDTH, _MINIMUM_SUPPORTED_HEIGHT)
        target_width, target_height = _DEFAULT_LAUNCH_WIDTH, _DEFAULT_LAUNCH_HEIGHT
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            target_width = min(target_width, available.width())
            target_height = min(target_height, available.height())
            target_width = max(target_width, min(_MINIMUM_SUPPORTED_WIDTH, available.width()))
            target_height = max(
                target_height, min(_MINIMUM_SUPPORTED_HEIGHT, available.height())
            )
        self.resize(target_width, target_height)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "turn_snapshot_group"):
            self._apply_fixed_turn_image_width()

    def _apply_fixed_turn_image_width(self) -> None:
        """Fixed Turn image thumbnail width tracks the mock's two named
        targets (~230px @1440x900, ~185px @1280x720), interpolating between
        them for in-between window widths rather than pinning to whichever
        bound a plain min/max-width pair happens to resolve to under the
        row's HBoxLayout stretch.
        """

        width = self.width()
        if width >= _DEFAULT_LAUNCH_WIDTH:
            target = _FIXED_TURN_IMAGE_MAX_WIDTH
        elif width <= _MINIMUM_SUPPORTED_WIDTH:
            target = _FIXED_TURN_IMAGE_MIN_WIDTH
        else:
            span = _DEFAULT_LAUNCH_WIDTH - _MINIMUM_SUPPORTED_WIDTH
            ratio = (width - _MINIMUM_SUPPORTED_WIDTH) / span
            target = round(
                _FIXED_TURN_IMAGE_MIN_WIDTH
                + ratio * (_FIXED_TURN_IMAGE_MAX_WIDTH - _FIXED_TURN_IMAGE_MIN_WIDTH)
            )
        self.turn_snapshot_group.setFixedWidth(target)

    # -- new Bundle A/B widgets ------------------------------------------------

    def _build_bundle_c_state_widgets(self) -> None:
        self.current_state_group = QGroupBox("現在のTurn state — 人間が確認・修正")
        state_layout = QVBoxLayout(self.current_state_group)
        state_layout.setContentsMargins(2, 2, 2, 2)
        state_layout.setSpacing(0)
        self.current_state_draft_label = QLabel()
        self.current_state_draft_label.setWordWrap(True)
        self.current_state_draft_label.setStyleSheet("color: #b45309; font-weight: 600;")
        state_layout.addWidget(self.current_state_draft_label)
        # Read-only one-line summary shown once the state is already
        # confirmed for this Turn -- the full editor only needs screen
        # space while it is actually editable (TURN_CAPTURE_PENDING), so
        # it collapses the rest of the time to keep the fixed-height,
        # never-scrolling center column workable.
        self.current_state_summary_label = QLabel()
        self.current_state_summary_label.setWordWrap(True)
        state_layout.addWidget(self.current_state_summary_label)

        self.current_state_editor_container = QWidget()
        editor_layout = QVBoxLayout(self.current_state_editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(2)
        top_row = QHBoxLayout()
        self.weather_field = _KnownTextField("天候 (sun/rain)")
        self.terrain_field = _KnownTextField("フィールド")
        top_row.addWidget(QLabel("天候"))
        top_row.addWidget(self.weather_field, 1)
        top_row.addWidget(QLabel("フィールド"))
        top_row.addWidget(self.terrain_field, 1)
        editor_layout.addLayout(top_row)
        sides_row = QHBoxLayout()
        self.self_state_editor = _SideStateEditor("自分")
        self.opponent_state_editor = _SideStateEditor("相手")
        sides_row.addWidget(self.self_state_editor)
        sides_row.addWidget(self.opponent_state_editor)
        editor_layout.addLayout(sides_row)
        state_layout.addWidget(self.current_state_editor_container)

        self.action_result_delta_group = QGroupBox(
            "ActionResultDelta — CHANGED / UNCHANGED / UNKNOWN を明示"
        )
        delta_layout = QVBoxLayout(self.action_result_delta_group)
        delta_layout.setContentsMargins(2, 2, 2, 2)
        delta_layout.setSpacing(0)
        delta_top_row = QHBoxLayout()
        self.weather_delta_field = _DeltaTextField("天候（変更時のみ）")
        self.terrain_delta_field = _DeltaTextField("フィールド（変更時のみ）")
        delta_top_row.addWidget(QLabel("天候"))
        delta_top_row.addWidget(self.weather_delta_field, 1)
        delta_top_row.addWidget(QLabel("フィールド"))
        delta_top_row.addWidget(self.terrain_delta_field, 1)
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
        # Shared (both-tabs) outer chrome starts at a very roomy 24px
        # margin / 16px spacing -- tighten it so the fixed header above
        # the 3-column body doesn't eat into the 720px budget.
        outer_layout.setContentsMargins(8, 8, 8, 8)
        outer_layout.setSpacing(4)

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

        # Compact 2-column reflow: same widgets, same signal wiring, just
        # laid out 2-per-row instead of 1-per-row so the fixed,
        # non-scrolling center column can fit 1280x720/1440x900.
        switch_widget = self.switch_checkboxes[0].parentWidget()
        turn_facts_form = self.turn_facts_group.layout()
        if isinstance(turn_facts_form, QFormLayout) and switch_widget is not None:
            self._reflow_form_into_grid(
                turn_facts_form,
                [
                    self.self_active_box,
                    self.opponent_active_input,
                    self.self_hp_box,
                    self.opponent_hp_box,
                    *self.move_inputs,
                    switch_widget,
                    self.turn_note_input,
                ],
            )
        actual_action_form = self.actual_action_group.layout()
        if isinstance(actual_action_form, QFormLayout):
            # 3 columns (2 rows for 6 fields) instead of 2 (3 rows) -- this
            # group is only ever shown in the RECORD_ACTUAL_ACTION
            # completion phase, where LIVE must stay the dominant region
            # even with ActionResultDelta also on screen (5223937478 item
            # 3/5), so it gets the same row-count trim.
            self._reflow_form_into_grid(
                actual_action_form,
                [
                    self.actual_action_type_box,
                    self.actual_action_name_box,
                    self.opponent_action_type_box,
                    self.opponent_action_name_input,
                    self.action_order_box,
                    self.actual_action_confirm_checkbox,
                ],
                columns=6,
            )
            # An unbounded QComboBox/QLineEdit sizeHint here (content-driven,
            # easily 150px+ each) times 6-across was what pushed center's
            # minimum width past its 52% share and collapsed left/right
            # below their floor -- bound each field so the row's total
            # width demand stays reasonable (5224076761 item 2).
            for reflowed_field in (
                self.actual_action_type_box,
                self.actual_action_name_box,
                self.opponent_action_type_box,
                self.opponent_action_name_input,
                self.action_order_box,
            ):
                reflowed_field.setMaximumWidth(_ACTUAL_ACTION_FIELD_MAX_WIDTH)

        # These two forms still carry Qt's roomy default (9,9,9,9) content
        # margins -- every other group in this fixed, non-scrolling column
        # was already tightened, but these two were not; closing that gap
        # is part of keeping RECORD_ACTUAL_ACTION's non-LIVE editors as
        # small as the mock's progressive-disclosure intent while LIVE
        # stays the dominant region (5223937478 item 3/4).
        for compact_form in (turn_facts_form, actual_action_form):
            if compact_form is not None:
                compact_form.setContentsMargins(3, 3, 3, 3)
                compact_form.setSpacing(0)

        # -- diagnostics drawer: technical/dev-only material --------------------
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
        turn_snapshot_identity_label = getattr(self, "turn_snapshot_identity_label", None)
        turn_snapshot_roi_label = getattr(self, "turn_snapshot_roi_label", None)
        if turn_snapshot_frame_label is not None:
            frame_id_row = QWidget()
            frame_id_layout = QFormLayout(frame_id_row)
            if capture_freshness is not None:
                frame_id_layout.addRow("フレーム鮮度", QLabel(capture_freshness.text()))
            if capture_device is not None:
                frame_id_layout.addRow("キャプチャ機器", QLabel(capture_device.text()))
            frame_id_layout.addRow("Turn Frame", QLabel(turn_snapshot_frame_label.text()))
            if turn_snapshot_identity_label is not None:
                frame_id_layout.addRow("Identity", QLabel(turn_snapshot_identity_label.text()))
            if turn_snapshot_roi_label is not None:
                frame_id_layout.addRow("ROI", QLabel(turn_snapshot_roi_label.text()))
            self.diagnostics_drawer.add_widget(frame_id_row)

        # These specific detail rows are always visible below the two
        # compact evidentiary thumbnails and duplicated (above) in the
        # diagnostics drawer -- hide the originals in center so the
        # thumbnails' own status text is the only thing shown there.
        # Hiding only the value widget leaves QFormLayout's own
        # auto-generated row label (e.g. "Identity") visible and the row's
        # height still reserved in this fixed, non-scrolling group, so the
        # row label must be hidden via setRowVisible too.
        metadata_form = getattr(self, "_turn_snapshot_metadata_form", None)
        for detail_label in (
            capture_freshness,
            capture_device,
            turn_snapshot_frame_label,
            turn_snapshot_identity_label,
            turn_snapshot_roi_label,
        ):
            if detail_label is not None:
                detail_label.setVisible(False)
                if metadata_form is not None and metadata_form.indexOf(detail_label) != -1:
                    metadata_form.setRowVisible(detail_label, False)

        # ROI crop images and per-field origin/provenance rows are raw
        # capture diagnostics -- keep them out of the fixed, non-scrolling
        # center column entirely (spec: "4 ROI crops", "field origins" move
        # to the diagnostics drawer). The fixed Turn image itself (the
        # evidence thumbnail) stays in center.
        crop_labels = getattr(self, "_turn_snapshot_crop_labels", None)
        crop_title_labels = getattr(self, "_turn_snapshot_crop_title_labels", None)
        origin_labels = getattr(self, "_turn_snapshot_origin_labels", None)
        origins_form = getattr(self, "_turn_snapshot_origins_form", None)
        if crop_labels or origin_labels:
            roi_widget = QWidget()
            roi_layout = QVBoxLayout(roi_widget)
            roi_layout.setContentsMargins(0, 0, 0, 0)
            if crop_labels:
                # The grid title labels (e.g. "自分 active ROI") are plain
                # QLabels the original crop_grid still owns -- only the
                # value labels get reparented into the drawer below, so the
                # titles must be hidden explicitly or they keep occupying a
                # row in the original, now half-empty grid.
                for title_label in (crop_title_labels or {}).values():
                    title_label.setVisible(False)
                for label in crop_labels.values():
                    label.setVisible(False)
                    label.setParent(None)
                crop_row = QHBoxLayout()
                for label in crop_labels.values():
                    label.setVisible(True)
                    crop_row.addWidget(label)
                roi_layout.addLayout(crop_row)
            if origin_labels:
                new_origins_form = QFormLayout()
                for field_key, label in origin_labels.items():
                    # QFormLayout.addRow(str, widget) auto-creates the row's
                    # title label; reparenting only the value label away
                    # leaves that auto title behind, still visible, in the
                    # original origins form -- hide it there before moving
                    # the value label to the drawer's new form.
                    if origins_form is not None:
                        original_title = origins_form.labelForField(label)
                        if original_title is not None:
                            original_title.setVisible(False)
                    label.setVisible(False)
                    label.setParent(None)
                    label.setVisible(True)
                    new_origins_form.addRow(field_key, label)
                roi_layout.addLayout(new_origins_form)
            self.diagnostics_drawer.add_widget(roi_widget)

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
        # Preferred/Minimum (never shrink below sizeHint) on every non-live
        # row -- only the live preview (Expanding, below) is allowed to give
        # up space first when the fixed-height center column runs tight, so
        # these editable facts/state rows never get compressed into an
        # unusable, overlapping size.
        for non_preview_row in (self.current_state_group, self.action_result_delta_group):
            non_preview_row.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
            )
        self._center_column_layout.addWidget(self.current_state_group)
        self._center_column_layout.addWidget(self.action_result_delta_group)
        self._right_column_layout.insertWidget(0, self.rich_gemini_group)

        # UGREEN LIVE is the largest region of the center column (mock
        # 5203292374/5203546707) -- full width, no height cap, a high
        # stretch weight so it claims the majority of available vertical
        # space at both 1440x900 (~420-450px target) and, after manual
        # resize, 1280x720 (~280-310px target). ``_AspectRatioPreviewLabel``
        # already keeps it at a 16:9 heightForWidth.
        self._detach_from_parent_layout(self.capture_status_group)
        # The base class leaves capture_preview_label's vertical size policy
        # at Preferred, which pins a heightForWidth widget to a single
        # computed height regardless of stretch weight or leftover column
        # space. Bundle C is the one place that needs it to actually grow
        # into the "largest region" role from the mock, so promote it to
        # Expanding here rather than in shared base UI code other windows
        # also use.
        self.capture_preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.actual_action_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        self._center_column_layout.insertWidget(0, self.capture_status_group, 20)

        # Fixed Turn image sits beside the current Turn facts, mock-style
        # (thumbnail column ~185-230px wide, facts filling the remainder),
        # not stacked with live preview.
        turn_snapshot_group = getattr(self, "turn_snapshot_group", None)
        if turn_snapshot_group is not None:
            self._detach_from_parent_layout(turn_snapshot_group)
            self._detach_from_parent_layout(self.turn_facts_group)
            # Initial width; kept in sync with the live window width by
            # resizeEvent -> _apply_fixed_turn_image_width thereafter.
            turn_snapshot_group.setFixedWidth(_FIXED_TURN_IMAGE_MAX_WIDTH)
            # The base class's QVBoxLayout still reserves its default
            # inter-item spacing around the now-empty (rows hidden, not
            # removed) metadata/crop/origins sub-layouts above -- zero it
            # out so only real content (status, image, retake button)
            # contributes height.
            snapshot_layout = turn_snapshot_group.layout()
            if snapshot_layout is not None:
                snapshot_layout.setSpacing(0)
            for never_shrink in (turn_snapshot_group, self.turn_facts_group):
                never_shrink.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
                )
            self.turn_snapshot_image_label.setMaximumWidth(_FIXED_TURN_IMAGE_MAX_WIDTH - 12)
            self.turn_snapshot_image_label.setMinimumSize(
                _FIXED_TURN_IMAGE_MIN_WIDTH - 12, 104
            )
            fixed_image_facts_row = QWidget()
            fixed_image_facts_layout = QHBoxLayout(fixed_image_facts_row)
            fixed_image_facts_layout.setContentsMargins(0, 0, 0, 0)
            fixed_image_facts_layout.addWidget(turn_snapshot_group, 0)
            fixed_image_facts_layout.addWidget(self.turn_facts_group, 1)
            self._center_column_layout.insertWidget(1, fixed_image_facts_row)

        # Only left (confirmed log) and right (Gemini detail) get their own
        # scroll container -- center is the primary work area and must never
        # scroll as a whole (spec Bundle C section 2/4).
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left_container)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_container)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Floor widths so a wide row inside center (e.g. RECORD_ACTUAL_ACTION's
        # reflowed editors) cannot squeeze these two internal-scroll columns
        # into an unreadable sliver -- 18/52/30 is a target ratio, not just a
        # stretch-factor hint, once center's own minimum width is respected.
        left_scroll.setMinimumWidth(_LEFT_COLUMN_MINIMUM_WIDTH)
        right_scroll.setMinimumWidth(_RIGHT_COLUMN_MINIMUM_WIDTH)

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
        page_layout.setContentsMargins(2, 2, 2, 2)
        page_layout.setSpacing(3)
        page_layout.addWidget(header_widget)
        page_layout.addLayout(body_row, 1)
        page_layout.addWidget(bottom_bar)
        body_row.setSpacing(4)
        bottom_bar_layout.setContentsMargins(0, 0, 0, 0)
        bottom_bar_layout.setSpacing(4)
        header_layout.setSpacing(2)
        # Compact chrome app-wide within this tab: every QGroupBox/QLabel's
        # margins and spacing add up across the ~6 groups always live in
        # the center column -- this is what actually makes 900/720px
        # reachable without dropping any operator-visible field.
        page.setStyleSheet(
            "QGroupBox { margin-top: 2px; padding: 0px; font-size: 9px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 2px; }"
            "QLabel { font-size: 10px; }"
            "QPushButton { padding: 1px 5px; font-size: 10px; }"
            "QComboBox, QLineEdit, QSpinBox, QCheckBox { font-size: 10px; padding: 0px 2px; }"
            "QComboBox, QLineEdit, QSpinBox { min-height: 15px; max-height: 17px; }"
        )
        self.battle_context_label.setMaximumHeight(16)
        capture_status_layout = self.capture_status_group.layout()
        if capture_status_layout is not None:
            capture_status_layout.setContentsMargins(4, 4, 4, 4)
            capture_status_layout.setSpacing(2)
        self.reconnect_capture_button.setMaximumHeight(16)
        self.capture_status_label.setMaximumHeight(28)
        turn_snapshot_status_label = getattr(self, "turn_snapshot_status_label", None)
        if turn_snapshot_status_label is not None:
            turn_snapshot_status_label.setMaximumHeight(28)
        turn_snapshot_group_widget = getattr(self, "turn_snapshot_group", None)
        if turn_snapshot_group_widget is not None:
            turn_snapshot_layout = turn_snapshot_group_widget.layout()
            if turn_snapshot_layout is not None:
                turn_snapshot_layout.setContentsMargins(4, 4, 4, 4)
                turn_snapshot_layout.setSpacing(2)
        retake_button = getattr(self, "retake_turn_snapshot_button", None)
        if retake_button is not None:
            retake_button.setMaximumHeight(16)
        self._center_column_layout.setSpacing(0)
        self._center_column_layout.setContentsMargins(0, 0, 0, 0)

        # The shared (both-tabs) primary-CTA/guidance labels sit above the
        # tab widget itself -- shrinking their font is the last bit of
        # fixed header overhead available to reclaim for 720px-tall
        # viewports, without touching Selection-tab layout/behavior.
        self.primary_cta_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.guidance_label.setStyleSheet("font-size: 10px;")
        self.guidance_label.setMaximumHeight(28)

        self.header_tabs.removeTab(_BATTLE_RECORD_TAB_INDEX)
        self.header_tabs.insertTab(_BATTLE_RECORD_TAB_INDEX, page, "バトルレコード")

    @staticmethod
    def _reflow_form_into_grid(
        form: QFormLayout, widgets: list[QWidget], *, columns: int = 2
    ) -> None:
        """Move existing rows (widget + its auto-created label, if any) out
        of a QFormLayout into a compact N-per-row QGridLayout, then embed
        that grid back as a single full-width row of the same form.

        Reparents only -- every widget keeps its identity, its connected
        signals, and its place in the form's tab order group; nothing here
        creates a new input, changes a label's text, or touches a slot.
        """

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(0)
        for index, widget in enumerate(widgets):
            label = form.labelForField(widget)
            form.removeWidget(widget)
            if label is not None:
                form.removeWidget(label)
            row, col = divmod(index, columns)
            col_base = col * 2
            if label is not None:
                grid.addWidget(label, row, col_base)
                grid.addWidget(widget, row, col_base + 1)
            else:
                grid.addWidget(widget, row, col_base, 1, 2)
        form.addRow(grid)

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
        # The base render_view calls setVisible(...) tied to primary_cta on
        # these three buttons because, in the original (pre-Bundle-C)
        # layout, hiding the button was how their whole one-button group
        # left the screen. Bundle C already reparented all five primary
        # operations into the fixed bottom bar, so that base-class
        # visibility toggle now hides a slot in the bar entirely instead of
        # just disabling it -- the fixed 5-operation bar must always show
        # all five slots (5223937478/5224076761); only enabled/disabled
        # reflects whether the state currently allows that action.
        for always_visible_operation in (
            self.start_turn_button,
            self.confirm_turn_facts_button,
            self.next_turn_button,
        ):
            always_visible_operation.setVisible(True)
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
        # Full editor only while actually editable; a compact one-line
        # summary the rest of the time so it never dominates the fixed,
        # non-scrolling center column once already confirmed.
        self.current_state_editor_container.setVisible(editable)
        self.current_state_summary_label.setVisible(not editable)
        if not editable and summary.confirmed_state is not None:
            state = summary.confirmed_state
            self_active_value = state.self_side.active.value
            opponent_active_value = state.opponent_side.active.value
            self_active_text = self_active_value if self_active_value is not None else "不明"
            opponent_active_text = (
                opponent_active_value if opponent_active_value is not None else "不明"
            )
            self.current_state_summary_label.setText(
                f"確定済み state — 自分: {self_active_text} / 相手: {opponent_active_text}"
            )
        elif not editable:
            self.current_state_summary_label.setText("確定済み stateはまだありません。")

        # Collapse the legal-action prefill rows (moves/switches) once they
        # are no longer editable -- same fixed-height rationale as above.
        turn_facts_form = self.turn_facts_group.layout()
        if isinstance(turn_facts_form, QFormLayout):
            for row_widget in (*self.move_inputs, *self.switch_checkboxes):
                row_widget.setVisible(editable)
                row_label = turn_facts_form.labelForField(row_widget)
                if row_label is not None:
                    row_label.setVisible(editable)

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
        self_active = draft.self_side.active
        if self_active.is_confirmed and self_active.value is not None:
            self.self_active_box.setCurrentText(self_active.value)
        opponent_active = draft.opponent_side.active
        if opponent_active.is_confirmed and opponent_active.value is not None:
            self.opponent_active_input.setText(opponent_active.value)
        self_hp = draft.self_side.hp_bucket
        if self_hp.is_confirmed and self_hp.value is not None:
            self.self_hp_box.setCurrentText(self_hp.value.value)
        opponent_hp = draft.opponent_side.hp_bucket
        if opponent_hp.is_confirmed and opponent_hp.value is not None:
            self.opponent_hp_box.setCurrentText(opponent_hp.value.value)

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
