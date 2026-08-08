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

from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
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
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from maple_next.domain.battle_events import (
    MAJOR_STATUS_CLEAR_LABEL,
    MAJOR_STATUS_PRESETS,
    STAGE_EVENT_PRESETS,
)
from maple_next.domain.effect_catalog import (
    EFFECT_CATALOG,
    EffectCatalogEntry,
    EffectTarget,
    EffectTiming,
    find_effect,
)
from maple_next.domain.enums import HpBucket
from maple_next.domain.opponent_intel import (
    LocalJsonOpponentMetaProvider,
    MatchOpponentFacts,
    OpponentIntelView,
    OpponentMetaProvider,
    build_opponent_intel,
)
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
# Official Battle Record window client size, fixed (5224282289 withdraws
# the earlier resizable-1920x1080-default/1280x720-floor contract from
# 5223723582/5224228965 for this single-operator, single-environment
# surface). _MINIMUM_SUPPORTED_WIDTH/HEIGHT remain only as the lower bound
# used internally by the fixed Turn image width interpolation below --
# they no longer describe a reachable window size.
_DEFAULT_LAUNCH_WIDTH = 1920
_DEFAULT_LAUNCH_HEIGHT = 1080
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
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(True)
        self.spin.setMaximumWidth(40)
        self.spin.valueChanged.connect(lambda _value: self.mode_box.setCurrentText("CHANGED"))
        layout.addWidget(self.spin)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)

    def _set_unknown(self, checked: bool) -> None:
        self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")
        self.spin.setEnabled(not checked)

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
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText(placeholder)
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)

    def _set_unknown(self, checked: bool) -> None:
        self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")
        self.line.setEnabled(not checked)

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
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.value_box = QComboBox()
        self.value_box.addItem("—")
        for bucket in HpBucket:
            self.value_box.addItem(bucket.value)
        self.value_box.currentIndexChanged.connect(
            lambda index: self.mode_box.setCurrentText("CHANGED" if index > 0 else "UNCHANGED")
        )
        layout.addWidget(self.value_box)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)

    def _set_unknown(self, checked: bool) -> None:
        self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")
        self.value_box.setEnabled(not checked)

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
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText("やけど,こんらん")
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)

    def _set_unknown(self, checked: bool) -> None:
        self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")
        self.line.setEnabled(not checked)

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
) -> QWidget:
    """4-columns-wide grid, label beside (not above) its field -- ~1 row of
    height per 4 fields instead of 2 (a label-over-field row pair each),
    so the two stacked stage grids in the fixed, non-scrolling center
    column (self/opponent state or self/opponent result-delta) claim
    roughly half the vertical space they used to for the same 7 fields.
    """

    container = QWidget()
    grid = QGridLayout(container)
    grid.setContentsMargins(0, 0, 0, 0)
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
    layout.addWidget(container)
    return container


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
        self.stage_grid_widget = _add_compact_stage_grid(
            layout, self.stage_fields, _STAGE_FIELDS
        )

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
    """One side's SELF/OPPONENT result-of-Turn editor.

    Event-entry UI v3 (5224627634): the normal surface is a status-preset
    quick-set + an ability-stage event candidate -> preview -> human Apply
    flow (this class's own ``event_apply_button`` -- populating these
    widgets is *not* a canonical write; only the existing bottom-bar
    "行動・結果記録" click, which reads :meth:`to_side_delta`, is). Manual
    per-stage +/- editing moves into a collapsed "詳細修正" section. There
    is deliberately no active-identity input anywhere in this editor --
    active identity changes only via a human-confirmed actual SWITCH action,
    computed automatically by
    :meth:`~maple_next.ui.turn_state_flow.TurnStateFlowController.compute_confirmed_switch_side_delta`
    and never read from here (:meth:`to_side_delta` always reports
    ``active=UNCHANGED``; the caller substitutes the computed delta when a
    switch was confirmed).
    """

    def __init__(
        self,
        title: str,
        *,
        side: str,
        preview_fn: Callable[..., dict[str, tuple[int, int]]],
    ) -> None:
        super().__init__(title)
        self._side = side
        self._preview_fn = preview_fn
        self._pending_stage_preview: dict[str, tuple[int, int]] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("HP"))
        self.hp_field = _DeltaHpField()
        top_row.addWidget(self.hp_field, 1)
        top_row.addWidget(QLabel("その他"))
        self.side_effects_field = _DeltaSideEffectsField()
        top_row.addWidget(self.side_effects_field, 1)
        layout.addLayout(top_row)

        status_row = QHBoxLayout()
        self.status_preset_label = QLabel("状態異常")
        status_row.addWidget(self.status_preset_label)
        self.status_preset_box = QComboBox()
        self.status_preset_box.addItem("選択してください")
        self.status_preset_box.addItems(MAJOR_STATUS_PRESETS)
        self.status_preset_box.addItem(MAJOR_STATUS_CLEAR_LABEL)
        status_row.addWidget(self.status_preset_box, 1)
        self.status_apply_button = QPushButton("状態を適用")
        self.status_apply_button.clicked.connect(self._on_apply_status_preset)
        status_row.addWidget(self.status_apply_button)
        layout.addLayout(status_row)
        # Underlying CHANGED/UNCHANGED/UNKNOWN delta source -- the preset
        # button above just populates it; manual text entry stays available
        # for anything a preset doesn't cover.
        self.status_field = _DeltaTextField("変更時のみ（手動）")
        layout.addWidget(self.status_field)

        event_row = QHBoxLayout()
        self.event_preset_label = QLabel("能力変化")
        event_row.addWidget(self.event_preset_label)
        self.event_preset_box = QComboBox()
        self.event_preset_box.addItem("選択してください", "")
        for preset in STAGE_EVENT_PRESETS:
            self.event_preset_box.addItem(preset.label, preset.key)
        event_row.addWidget(self.event_preset_box, 1)
        self.event_preview_button = QPushButton("プレビュー")
        self.event_preview_button.clicked.connect(self._on_preview_stage_event)
        event_row.addWidget(self.event_preview_button)
        self.event_apply_button = QPushButton("適用")
        self.event_apply_button.setEnabled(False)
        self.event_apply_button.clicked.connect(self._on_apply_stage_event)
        event_row.addWidget(self.event_apply_button)
        layout.addLayout(event_row)
        self.event_preview_label = QLabel("")
        self.event_preview_label.setWordWrap(True)
        self.event_preview_label.setStyleSheet("font-size: 9px; color: #2563eb;")
        layout.addWidget(self.event_preview_label)

        self.stage_fields: dict[str, _DeltaIntField] = {}
        for key, _label in _STAGE_FIELDS:
            self.stage_fields[key] = _DeltaIntField()
        self.detail_section = _CollapsibleSection("詳細修正 — 能力ランク手動編集")
        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        _add_compact_stage_grid(detail_layout, self.stage_fields, _STAGE_FIELDS)
        self.detail_section.add_widget(detail_widget)
        layout.addWidget(self.detail_section)

        # v5 operator composition: catalog/context cards are the primary
        # route. These legacy preset/manual-stage controls stay available to
        # tests/domain wiring but are not permanently exposed in the normal
        # workbench; the timing-grouped fallback dialog owns that role.
        for legacy_control in (
            self.status_preset_label,
            self.status_preset_box,
            self.status_apply_button,
            self.event_preset_label,
            self.event_preset_box,
            self.event_preview_button,
            self.event_apply_button,
            self.event_preview_label,
            self.detail_section,
        ):
            legacy_control.setVisible(False)

    def _on_preview_stage_event(self, _checked: bool = False) -> None:
        preset_key = self.event_preset_box.currentData()
        if not preset_key:
            self.event_preview_label.setText("")
            self.event_apply_button.setEnabled(False)
            self._pending_stage_preview = {}
            return
        preview = self._preview_fn(side=self._side, preset_key=preset_key)
        self._pending_stage_preview = preview
        if not preview:
            self.event_preview_label.setText("確定済みのcurrent stateがまだありません。")
            self.event_apply_button.setEnabled(False)
            return
        label_by_key = dict(_STAGE_FIELDS)
        parts = []
        for field_name, (current, candidate) in preview.items():
            diff = candidate - current
            sign = "+" if diff > 0 else ""
            label = label_by_key.get(field_name, field_name)
            parts.append(f"{label} {current}→{candidate} ({sign}{diff})")
        self.event_preview_label.setText(" / ".join(parts))
        self.event_apply_button.setEnabled(True)

    def _on_apply_stage_event(self, _checked: bool = False) -> None:
        for field_name, (_current, candidate) in self._pending_stage_preview.items():
            field = self.stage_fields.get(field_name)
            if field is None:
                continue
            field.mode_box.setCurrentText("CHANGED")
            field.spin.setValue(candidate)
        self.event_apply_button.setEnabled(False)
        if self.event_preview_label.text():
            self.event_preview_label.setText(self.event_preview_label.text() + "  [適用済み]")

    def _on_apply_status_preset(self, _checked: bool = False) -> None:
        text = self.status_preset_box.currentText()
        if text == "選択してください":
            return
        value = "なし" if text == MAJOR_STATUS_CLEAR_LABEL else text
        self.status_field.mode_box.setCurrentText("CHANGED")
        self.status_field.line.setText(value)

    def to_side_delta(self) -> SideDelta:
        return SideDelta(
            active=FieldDelta.unchanged(),
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


class _EffectCandidateCard(QGroupBox):
    """Human Apply/Reject boundary; displaying a hit never mutates state."""

    def __init__(self, apply_callback: Callable[[EffectCatalogEntry], None]) -> None:
        super().__init__("Mapleの状態変化候補")
        self._entry: EffectCatalogEntry | None = None
        self._apply_callback = apply_callback
        layout = QHBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label, 1)
        self.apply_button = QPushButton("適用")
        self.reject_button = QPushButton("違う")
        self.apply_button.clicked.connect(self._apply)
        self.reject_button.clicked.connect(self.clear)
        layout.addWidget(self.apply_button)
        layout.addWidget(self.reject_button)
        self.setVisible(False)

    @property
    def pending_entry(self) -> EffectCatalogEntry | None:
        return self._entry

    def propose(self, entry: EffectCatalogEntry, *, prefix: str = "") -> None:
        self._entry = entry
        lead = f"{prefix} → " if prefix else ""
        self.summary_label.setText(f"{lead}{entry.summary}")
        self.setVisible(True)

    def clear(self, _checked: bool = False) -> None:
        self._entry = None
        self.summary_label.clear()
        self.setVisible(False)

    def _apply(self, _checked: bool = False) -> None:
        if self._entry is None:
            return
        entry = self._entry
        self._apply_callback(entry)
        self.clear()


class _StateEventDialog(QDialog):
    """Timing-grouped fallback. Preset click previews; Apply is separate."""

    def __init__(
        self,
        parent: QWidget,
        *,
        context: str,
        apply_callback: Callable[[EffectCatalogEntry], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("状態変化を記録")
        self.setModal(False)
        self._pending: EffectCatalogEntry | None = None
        self._apply_callback = apply_callback
        layout = QVBoxLayout(self)
        self.context_label = QLabel(
            "Turn確認: 登場・ターン開始を優先"
            if context == "review"
            else "行動・結果: 行動後を優先"
        )
        layout.addWidget(self.context_label)

        ordered_timings: tuple[tuple[EffectTiming, str], ...] = (
            (EffectTiming.SWITCH_IN, "登場・ターン開始でよく起きること"),
            (EffectTiming.AFTER_ACTION, "行動後によく起きること"),
        )
        if context == "result":
            ordered_timings = tuple(reversed(ordered_timings))
        for timing, title in ordered_timings:
            group = QGroupBox(title)
            row = QHBoxLayout(group)
            shown = 0
            for entry in EFFECT_CATALOG:
                if entry.timing is not timing or shown >= 10:
                    continue
                button = QPushButton(entry.display_name_ja)
                button.clicked.connect(
                    lambda _checked=False, candidate=entry: self._preview(candidate)
                )
                row.addWidget(button)
                shown += 1
            layout.addWidget(group)

        details = _CollapsibleSection("その他・詳細修正")
        details.add_widget(QLabel("catalogにない例外は、各入力欄へ人間が直接記録してください。"))
        layout.addWidget(details)
        self.preview_label = QLabel("候補を選ぶと、ここにpreviewします。")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        actions = QHBoxLayout()
        self.apply_button = QPushButton("適用")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.close)
        actions.addWidget(self.apply_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.resize(1050, 430)

    def _preview(self, entry: EffectCatalogEntry) -> None:
        self._pending = entry
        self.preview_label.setText(f"preview: {entry.display_name_ja} → {entry.summary}")
        self.apply_button.setEnabled(True)

    def _apply(self, _checked: bool = False) -> None:
        if self._pending is None:
            return
        self._apply_callback(self._pending)
        self.accept()


class _OpponentIntelWidget(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Opponent INTEL")
        layout = QVBoxLayout(self)
        self.species_label = QLabel("相手 active: 不明")
        self.facts_label = QLabel("この試合: 特性 不明 / 道具 不明 / 技 不明")
        self.facts_label.setWordWrap(True)
        self.summary_label = QLabel("move / ability / item: データなし")
        self.summary_label.setWordWrap(True)
        self.detail_button = QPushButton("INTEL詳細を表示")
        layout.addWidget(self.species_label)
        layout.addWidget(self.facts_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_button)
        self._view: OpponentIntelView | None = None
        self._detail_dialog: QDialog | None = None
        self.detail_button.clicked.connect(self._open_detail)

    def render_intel(self, view: OpponentIntelView) -> None:
        self._view = view
        self.species_label.setText(f"相手 active: {view.species}")
        moves = ", ".join(view.moves) or "不明"
        self.facts_label.setText(
            f"この試合優先: 特性 {view.ability} / 道具 {view.item} / 技 {moves}"
        )
        if view.meta is None:
            self.summary_label.setText("move / ability / item: データなし")
        else:
            move_summary = ", ".join(entry.name for entry in view.meta.moves[:3]) or "データなし"
            ability_summary = (
                ", ".join(entry.name for entry in view.meta.abilities[:2]) or "データなし"
            )
            item_summary = ", ".join(entry.name for entry in view.meta.items[:2]) or "データなし"
            self.summary_label.setText(
                f"move: {move_summary}\nability: {ability_summary}\nitem: {item_summary}"
            )

    def _open_detail(self, _checked: bool = False) -> None:
        if self._view is None:
            return
        view = self._view
        dialog = QDialog(self)
        dialog.setWindowTitle("Opponent INTEL 詳細")
        layout = QVBoxLayout(dialog)
        tabs = QLabel(f"相手個体: {view.species}")
        layout.addWidget(tabs)
        details = QTextEdit()
        details.setReadOnly(True)
        if view.meta is None:
            source_text = "source/regulation/snapshot: データなし"
            ranking_text = "usage ranking: データなし"
        else:
            source_text = (
                f"source: {view.meta.source}\nregulation: {view.meta.regulation}\n"
                f"snapshot: {view.meta.snapshot_date}"
            )
            ranking_text = (
                f"moves: {_format_rankings(view.meta.moves)}\n"
                f"abilities: {_format_rankings(view.meta.abilities)}\n"
                f"items: {_format_rankings(view.meta.items)}"
            )
        possible = ", ".join(view.possible_abilities) or "データなし"
        details.setPlainText(
            f"current-match facts\nability: {view.ability}\nitem: {view.item}\n"
            f"moves: {', '.join(view.moves) or '不明'}\n\npossible abilities: {possible}\n\n"
            f"{ranking_text}\n\n{source_text}"
        )
        layout.addWidget(details)
        dialog.resize(620, 520)
        dialog.setModal(False)
        self._detail_dialog = dialog
        dialog.show()


def _format_rankings(entries: tuple[object, ...]) -> str:
    formatted: list[str] = []
    for entry in entries:
        name = str(getattr(entry, "name", ""))
        percentage = getattr(entry, "percentage", None)
        formatted.append(name if percentage is None else f"{name} {percentage:.1f}%")
    return ", ".join(formatted) or "データなし"


class BattleRecordUiWindow(TurnSnapshotMatchFlowWindow):
    """Fixed header / 18-52-30 body / fixed bottom bar Battle Record UI."""

    def __init__(
        self,
        controller: TurnStateFlowController,
        *,
        ocr_data_directory: Path,
        opponent_meta_provider: OpponentMetaProvider | None = None,
    ) -> None:
        super().__init__(controller, ocr_data_directory=ocr_data_directory)
        self._apply_official_windows_font()
        self._bundle_c_controller: TurnStateFlowController = controller
        self._opponent_meta_provider = opponent_meta_provider or LocalJsonOpponentMetaProvider(
            ocr_data_directory / "opponent_meta_cache.json"
        )
        self._evidence_dialog: QDialog | None = None
        self._state_event_dialog: QDialog | None = None
        self._build_bundle_c_state_widgets()
        self._restructure_battle_record_layout()
        self._apply_default_launch_geometry()
        self.render_view()

    def _apply_official_windows_font(self) -> None:
        """Ensure the official Windows runtime can render Japanese labels.

        Some embedded/offscreen Qt runtimes do not enumerate Windows fonts
        automatically even though Meiryo is installed. Linux and other
        environments simply retain their normal application font.
        """

        font_path = Path("C:/Windows/Fonts/meiryo.ttc")
        if not font_path.is_file():
            return
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id < 0:
            return
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            self.setFont(QFont(families[0], 9))

    def _apply_default_launch_geometry(self) -> None:
        """Official Battle Record window client size is fixed at 1920x1080
        (5224282289 -- withdraws the earlier "1920x1080 default, resizable,
        1280x720 floor with screen-too-small degrade" contract for this
        single-operator, single-environment surface). Ordinary operator
        resize is not possible; there is no screen-size degrade path.
        """

        self.setFixedSize(_DEFAULT_LAUNCH_WIDTH, _DEFAULT_LAUNCH_HEIGHT)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        # The window is fixed-size (5224282289); this override only exists
        # to keep the base class's own resizeEvent chain (LIVE preview
        # re-render) intact. The fixed Turn image no longer sits inline at
        # a width that tracked window size -- it now lives in the on-demand
        # evidence overlay (5224627634 section B).
        super().resizeEvent(event)

    def _on_open_evidence_overlay(self, _checked: bool = False) -> None:
        turn_snapshot_group = getattr(self, "turn_snapshot_group", None)
        if turn_snapshot_group is None:
            return
        self._detach_from_parent_layout(turn_snapshot_group)
        dialog = QDialog(self)
        dialog.setWindowTitle("撮影画像を確認 — このTurnで固定した画像")
        dialog_layout = QVBoxLayout(dialog)
        turn_snapshot_group.setMinimumWidth(320)
        turn_snapshot_group.setMaximumWidth(16777215)
        self.turn_snapshot_image_label.setMaximumWidth(16777215)
        dialog_layout.addWidget(turn_snapshot_group)
        dialog.resize(420, 560)
        dialog.setModal(False)
        dialog.finished.connect(
            lambda _result, widget=turn_snapshot_group: self._on_close_evidence_overlay(widget)
        )
        self._evidence_dialog = dialog
        dialog.show()

    def _on_close_evidence_overlay(self, turn_snapshot_group: QWidget) -> None:
        self._detach_from_parent_layout(turn_snapshot_group)
        self.turn_snapshot_image_label.setMaximumWidth(_FIXED_TURN_IMAGE_MAX_WIDTH - 12)
        holder_layout = self._evidence_holder.layout()
        if holder_layout is not None:
            holder_layout.addWidget(turn_snapshot_group)
        self._evidence_dialog = None

    # -- new Bundle A/B widgets ------------------------------------------------

    def _build_bundle_c_state_widgets(self) -> None:
        self.current_state_group = QGroupBox("Turn確認 — 状態 / OCR修正")
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
        self.self_state_editor.stage_grid_widget.setVisible(False)
        self.opponent_state_editor.stage_grid_widget.setVisible(False)
        sides_row.addWidget(self.self_state_editor)
        sides_row.addWidget(self.opponent_state_editor)
        editor_layout.addLayout(sides_row)
        self.ability_resolution_group = QGroupBox("相手の特性候補")
        ability_layout = QHBoxLayout(self.ability_resolution_group)
        self.opponent_ability_box = QComboBox()
        self.confirm_opponent_ability_button = QPushButton("特性を確認")
        self.confirm_opponent_ability_button.clicked.connect(self._on_confirm_opponent_ability)
        ability_layout.addWidget(self.opponent_ability_box, 1)
        ability_layout.addWidget(self.confirm_opponent_ability_button)
        editor_layout.addWidget(self.ability_resolution_group)
        self.review_effect_candidate = _EffectCandidateCard(self._apply_review_effect)
        editor_layout.addWidget(self.review_effect_candidate)
        self.review_state_event_button = QPushButton("＋ 状態変化を記録")
        self.review_state_event_button.clicked.connect(
            lambda: self._open_state_event_dialog("review")
        )
        editor_layout.addWidget(self.review_state_event_button)
        state_layout.addWidget(self.current_state_editor_container)

        self.action_result_delta_group = QGroupBox("結果 — 変わった項目だけ記録")
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
        self.self_delta_editor = _SideDeltaEditor(
            "自分の結果",
            side="self",
            preview_fn=self._bundle_c_controller.preview_stage_event,
        )
        self.opponent_delta_editor = _SideDeltaEditor(
            "相手の結果",
            side="opponent",
            preview_fn=self._bundle_c_controller.preview_stage_event,
        )
        delta_sides_row.addWidget(self.self_delta_editor)
        delta_sides_row.addWidget(self.opponent_delta_editor)
        delta_layout.addLayout(delta_sides_row)
        self.result_effect_candidate = _EffectCandidateCard(self._apply_result_effect)
        delta_layout.addWidget(self.result_effect_candidate)
        self.result_state_event_button = QPushButton("＋ 状態変化を記録")
        self.result_state_event_button.clicked.connect(
            lambda: self._open_state_event_dialog("result")
        )
        delta_layout.addWidget(self.result_state_event_button)

        self.rich_gemini_group = QGroupBox("Gemini Turn Advice — rich state")
        rich_layout = QFormLayout(self.rich_gemini_group)
        self._rich_gemini_layout = rich_layout
        self.rich_gemini_status_label = QLabel()
        self.rich_gemini_denial_label = QLabel()
        self.rich_gemini_denial_label.setWordWrap(True)
        rich_layout.addRow("送信可否", self.rich_gemini_status_label)
        rich_layout.addRow("理由", self.rich_gemini_denial_label)
        self.opponent_intel_widget = _OpponentIntelWidget()

        self.actual_action_type_box.currentTextChanged.connect(self._update_v5_action_disclosure)
        self.opponent_action_type_box.currentTextChanged.connect(self._update_v5_action_disclosure)
        for opponent_type in ("NO ACTION", "UNKNOWN"):
            if self.opponent_action_type_box.findText(opponent_type) < 0:
                self.opponent_action_type_box.addItem(opponent_type)
        self.actual_action_name_box.currentTextChanged.connect(self._propose_actual_action_effect)
        self.opponent_action_name_input.textChanged.connect(self._propose_opponent_action_effect)
        self.opponent_active_input.textChanged.connect(self._on_opponent_species_changed)

    # -- layout restructuring ---------------------------------------------------

    def _restructure_battle_record_layout(self) -> None:
        outer_layout = self.centralWidget().layout()
        assert outer_layout is not None
        status_frame = self.application_mode_label.parentWidget()
        assert status_frame is not None
        outer_layout.removeWidget(status_frame)

        # Top guidance removal (5224224375): the "今なにをすべきか" heading,
        # the state-specific subtitle, and the paragraph under it are
        # removed entirely -- not hidden -- so their vertical space returns
        # to the body/LIVE area below. The fixed four-button footer's
        # visible/enabled state plus the compact Turn/state/provider line
        # and per-section labels already carry this information; no
        # replacement banner or text is added. error_label (anomaly
        # alerts) and the window title / tab bar are unaffected.
        for guidance_widget in (
            getattr(self, "_top_guidance_heading_label", None),
            self.primary_cta_label,
            self.guidance_label,
        ):
            if guidance_widget is None:
                continue
            outer_layout.removeWidget(guidance_widget)
            guidance_widget.setVisible(False)
            guidance_widget.setParent(None)
        # Shared (both-tabs) outer chrome starts at a very roomy 24px
        # margin / 16px spacing -- tighten it so the fixed header above
        # the 3-column body doesn't eat into the 720px budget.
        outer_layout.setContentsMargins(8, 8, 8, 8)
        outer_layout.setSpacing(4)

        left_container = self._left_column_layout.parentWidget()
        center_container = self._center_column_layout.parentWidget()
        right_container = self._right_column_layout.parentWidget()
        self.history_group.setTitle("確定履歴 / Latest confirmed state")
        history_layout = self.history_group.layout()
        if isinstance(history_layout, QVBoxLayout):
            self.latest_confirmed_state_label = QLabel("確定済みstateはまだありません。")
            self.latest_confirmed_state_label.setWordWrap(True)
            history_layout.insertWidget(0, self.latest_confirmed_state_label)

        # Extract the four v5 lifecycle buttons. The legacy standalone
        # Gemini button remains callable internally but is not an operator
        # phase/button: SEND itself confirms the review and dispatches.
        self._extract_widget(self.ready_group, self.start_turn_button)
        self._extract_widget(self.turn_facts_group, self.confirm_turn_facts_button)
        self._extract_widget(self.actual_action_group, self.record_action_button)
        self._extract_widget(self.history_group, self.next_turn_button)
        gemini_send_button = getattr(self, "turn_gemini_send_button", None)
        if gemini_send_button is not None:
            self._extract_widget(self.turn_gemini_box, gemini_send_button)
            self.turn_gemini_box.setVisible(False)

        self.start_turn_button.setText("Turn撮影")
        self.confirm_turn_facts_button.setText("SEND TURN TO GEMINI")
        self.record_action_button.setText("行動・結果記録")
        self.next_turn_button.setText("NEXT TURN")
        if gemini_send_button is not None:
            gemini_send_button.setVisible(False)
            self._bundle_c_gemini_send_button = gemini_send_button
        self.turn_facts_confirm_checkbox.setVisible(False)

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
                columns=2,
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
        self.terminal_flow_drawer = _CollapsibleSection("試合終了・Export・復旧")
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
        self.battle_context_label.setWordWrap(False)
        self.battle_context_label.setStyleSheet("font-weight: 600;")
        header_layout.addWidget(self.battle_context_label)
        self.diagnostics_drawer.setVisible(False)
        self.terminal_flow_drawer.setVisible(False)

        # -- phase-specific center workbench / clean right rail ---------------
        for non_preview_row in (self.current_state_group, self.action_result_delta_group):
            non_preview_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self._detach_from_parent_layout(self.turn_advice_group)
        self._rich_gemini_layout.addRow(self.turn_advice_group)
        self._detach_from_parent_layout(self.rich_gemini_group)
        self._detach_from_parent_layout(self.opponent_intel_widget)
        self._right_column_layout.insertWidget(0, self.rich_gemini_group)
        self._right_column_layout.insertWidget(1, self.opponent_intel_widget)

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

        # Fixed Turn image becomes an on-demand evidence overlay (5224627634
        # section B) instead of an always-visible thumbnail: the normal
        # center-column facts row keeps only a compact "撮影画像を確認"
        # control + short status text, and the actual (large) fixed-image
        # group only appears in a dialog when that button is clicked. The
        # same widget instance is reparented back to a hidden holder (never
        # destroyed, never re-created) on close, so it keeps receiving
        # every existing OCR/binding update call exactly as before
        # regardless of which parent currently displays it -- LIVE reclaims
        # the space this thumbnail used to occupy inline.
        turn_snapshot_group = getattr(self, "turn_snapshot_group", None)
        if turn_snapshot_group is not None:
            self._detach_from_parent_layout(turn_snapshot_group)
            self._detach_from_parent_layout(self.turn_facts_group)
            # The base class's QVBoxLayout still reserves its default
            # inter-item spacing around the now-empty (rows hidden, not
            # removed) metadata/crop/origins sub-layouts above -- zero it
            # out so only real content (status, image, retake button)
            # contributes height inside the dialog.
            snapshot_layout = turn_snapshot_group.layout()
            if snapshot_layout is not None:
                snapshot_layout.setSpacing(0)
            self.turn_facts_group.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
            )
            self._evidence_holder = QWidget(self)
            self._evidence_holder.setVisible(False)
            evidence_holder_layout = QVBoxLayout(self._evidence_holder)
            evidence_holder_layout.addWidget(turn_snapshot_group)

            evidence_control = QWidget()
            evidence_control_layout = QVBoxLayout(evidence_control)
            evidence_control_layout.setContentsMargins(0, 0, 0, 0)
            evidence_control_layout.setSpacing(1)
            self.evidence_open_button = QPushButton("撮影画像を確認")
            self.evidence_open_button.clicked.connect(self._on_open_evidence_overlay)
            evidence_control_layout.addWidget(self.evidence_open_button)
            self.evidence_status_label = QLabel()
            self.evidence_status_label.setWordWrap(True)
            self.evidence_status_label.setStyleSheet("font-size: 9px;")
            evidence_control_layout.addWidget(self.evidence_status_label)
            evidence_control_layout.addStretch(1)
            evidence_control.setMaximumWidth(_FIXED_TURN_IMAGE_MIN_WIDTH)

            fixed_image_facts_row = QWidget()
            fixed_image_facts_layout = QHBoxLayout(fixed_image_facts_row)
            fixed_image_facts_layout.setContentsMargins(0, 0, 0, 0)
            fixed_image_facts_layout.addWidget(evidence_control, 0)
            fixed_image_facts_layout.addWidget(self.turn_facts_group, 1)
            self._review_facts_row = fixed_image_facts_row

        # The accepted v5 HTML is a lifecycle workbench, not a collection of
        # simultaneously visible legacy groups. Exactly one page is rendered
        # below LIVE at any time; signal wiring and domain objects are reused.
        self.workbench_stack = QStackedWidget()
        self.workbench_stack.setObjectName("battleRecordLifecycleWorkbench")
        self.workbench_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )

        self.capture_workbench_page = QWidget()
        capture_page_layout = QVBoxLayout(self.capture_workbench_page)
        capture_page_layout.setContentsMargins(6, 6, 6, 6)
        capture_title = QLabel("Turn撮影")
        capture_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        capture_page_layout.addWidget(capture_title)
        self.capture_phase_hint = QLabel(
            "UGREEN LIVEを確認し、準備ができたら下の「Turn撮影」を押します。"
            "映像がなくてもmanual-safe入力で続行できます。"
        )
        self.capture_phase_hint.setWordWrap(True)
        capture_page_layout.addWidget(self.capture_phase_hint)
        capture_page_layout.addStretch(1)

        self.review_workbench_page = QWidget()
        review_page_layout = QVBoxLayout(self.review_workbench_page)
        review_page_layout.setContentsMargins(0, 0, 0, 0)
        review_page_layout.setSpacing(3)
        review_page_layout.addWidget(self._review_facts_row)
        review_page_layout.addWidget(self.current_state_group)

        self.action_workbench_page = QWidget()
        action_page_layout = QVBoxLayout(self.action_workbench_page)
        action_page_layout.setContentsMargins(0, 0, 0, 0)
        action_page_layout.setSpacing(3)
        self._detach_from_parent_layout(self.actual_action_group)
        action_page_layout.addWidget(self.actual_action_group)
        action_page_layout.addWidget(self.action_result_delta_group)

        self.recorded_workbench_page = QWidget()
        recorded_page_layout = QVBoxLayout(self.recorded_workbench_page)
        recorded_page_layout.setContentsMargins(8, 8, 8, 8)
        recorded_title = QLabel("このTurnは記録済みです")
        recorded_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        self.recorded_summary_label = QLabel()
        self.recorded_summary_label.setWordWrap(True)
        recorded_page_layout.addWidget(recorded_title)
        recorded_page_layout.addWidget(self.recorded_summary_label)
        recorded_page_layout.addStretch(1)

        for workbench_page in (
            self.capture_workbench_page,
            self.review_workbench_page,
            self.action_workbench_page,
            self.recorded_workbench_page,
        ):
            self.workbench_stack.addWidget(workbench_page)
        self._center_column_layout.addWidget(self.workbench_stack, 0)

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
        bottom_bar_layout.addWidget(self.record_action_button)
        bottom_bar_layout.addWidget(self.next_turn_button)
        self.lifecycle_buttons = (
            self.start_turn_button,
            self.confirm_turn_facts_button,
            self.record_action_button,
            self.next_turn_button,
        )
        for lifecycle_button in self.lifecycle_buttons:
            lifecycle_button.setProperty("lifecycle", True)
            lifecycle_button.setMinimumHeight(40)

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
        # The completed HTML uses readable, restrained cards. Since the
        # center now renders only one lifecycle surface at a time, the old
        # dense 9px legacy-graft styling is no longer necessary.
        page.setStyleSheet(
            "QGroupBox { margin-top: 7px; padding: 5px; font-size: 11px; "
            "border: 1px solid #cbd5e1; border-radius: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
            "QLabel { font-size: 11px; }"
            "QPushButton { padding: 4px 8px; font-size: 11px; }"
            "QPushButton[lifecycle=\"true\"] { font-size: 13px; font-weight: 700; }"
            "QComboBox, QLineEdit, QSpinBox, QCheckBox { font-size: 11px; padding: 2px 4px; }"
            "QComboBox, QLineEdit, QSpinBox { min-height: 22px; max-height: 26px; }"
        )
        self.battle_context_label.setMaximumHeight(22)
        capture_status_layout = self.capture_status_group.layout()
        if capture_status_layout is not None:
            capture_status_layout.setContentsMargins(4, 4, 4, 4)
            capture_status_layout.setSpacing(2)
        self.reconnect_capture_button.setMaximumHeight(24)
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
            retake_button.setMaximumHeight(24)
        self._center_column_layout.setSpacing(0)
        self._center_column_layout.setContentsMargins(0, 0, 0, 0)

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
        # left the screen. v5 reparented the four lifecycle operations into
        # the fixed bottom bar, so that base-class
        # visibility toggle now hides a slot in the bar entirely instead of
        # just disabling it -- the fixed four-operation bar must always show
        # all four slots; only enabled/disabled
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

        # The base class's
        # setEnabled(...) for these three buttons was written for a UI
        # where each button's own group was also hidden outside its cta
        # (e.g. confirm_turn_facts_button stays enabled through the whole
        # TURN_CAPTURE_PENDING/TURN_REVIEWED "editable" window, which is a
        # legitimate secondary CORRECT_TURN_FACTS affordance there but not
        # a state the fixed 5-slot bar should show as a live primary
        # operation once the actual primary_cta has moved on to
        # RECORD_ACTUAL_ACTION). Re-gate strictly to "this button's slot is
        # current canonical primary_cta", reusing primary_cta_enabled and
        # each button's own existing readiness condition -- no new legal
        # operation is invented here.
        self.start_turn_button.setEnabled(
            current.persistence_reads_allowed
            and projection.primary_cta == "START_TURN_CAPTURE"
            and projection.primary_cta_enabled
        )
        self.confirm_turn_facts_button.setEnabled(
            current.persistence_reads_allowed
            and projection.primary_cta == "CONFIRM_TURN_FACTS"
            and projection.primary_cta_enabled
        )
        self.record_action_button.setEnabled(
            self.record_action_button.isEnabled()
            and projection.primary_cta == "RECORD_ACTUAL_ACTION"
        )

        self.start_turn_button.setText("Turn撮影")
        self.confirm_turn_facts_button.setText("SEND TURN TO GEMINI")
        self.record_action_button.setText("行動・結果記録")
        self.next_turn_button.setText("NEXT TURN")

        context_parts = [
            f"Turn {projection.turn_number}" if projection.turn_number else "Turn —",
            projection.session_state or "—",
            f"provider={projection.provider_status}",
        ]
        self.battle_context_label.setText(" / ".join(context_parts))

        confirmed_state = summary.confirmed_state
        if confirmed_state is None:
            self.latest_confirmed_state_label.setText(
                "確定済みstateはまだありません。撮影後に確認した内容だけがここへ残ります。"
            )
        else:
            self_active = confirmed_state.self_side.active.value or "不明"
            opponent_active = confirmed_state.opponent_side.active.value or "不明"
            self_hp = confirmed_state.self_side.hp_bucket.value
            opponent_hp = confirmed_state.opponent_side.hp_bucket.value
            self.latest_confirmed_state_label.setText(
                f"Turn {confirmed_state.identity.turn_number}\n"
                f"自分: {self_active} / HP {self_hp.value if self_hp else '不明'}\n"
                f"相手: {opponent_active} / HP {opponent_hp.value if opponent_hp else '不明'}"
            )

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

        # First-turn ability stages default to 0 without manual input
        # (5224627634 section G): applies only while genuinely nothing has
        # been confirmed yet for this identity and there is no open draft to
        # carry forward from -- and only to fields still at their own
        # UNKNOWN default, so it never clobbers a value the operator already
        # touched (including a correction back to "not applicable" via a
        # UNKNOWN choice).
        if editable and summary.confirmed_state is None and summary.open_draft is None:
            self._apply_first_turn_zero_stage_defaults(self.self_state_editor)
            self._apply_first_turn_zero_stage_defaults(self.opponent_state_editor)

        evidence_status_label = getattr(self, "evidence_status_label", None)
        turn_snapshot_status_label = getattr(self, "turn_snapshot_status_label", None)
        if evidence_status_label is not None and turn_snapshot_status_label is not None:
            evidence_status_label.setText(turn_snapshot_status_label.text())

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
                "未確認 draft からの引き継ぎです。人間の確認が必要です。"
            )
            self.current_state_draft_label.setVisible(True)
            if editable and self._last_rendered_session_state == "TURN_CAPTURE_PENDING":
                self._load_draft_into_state_editor(summary)
        else:
            self.current_state_draft_label.setVisible(False)

        legacy_gemini_box = getattr(self, "turn_gemini_box", None)
        if legacy_gemini_box is not None:
            legacy_gemini_box.setVisible(False)

        self.action_result_delta_group.setVisible(projection.primary_cta == "RECORD_ACTUAL_ACTION")

        # The center follows the completed HTML's lifecycle composition:
        # LIVE is persistent and exactly one compact work surface sits below
        # it. None of the existing controller/domain operations are changed.
        if projection.primary_cta == "START_TURN_CAPTURE":
            workbench_page = self.capture_workbench_page
            workbench_height = 92
        elif projection.primary_cta == "RECORD_ACTUAL_ACTION":
            workbench_page = self.action_workbench_page
            workbench_height = 350
        elif projection.primary_cta == "NEXT_TURN":
            workbench_page = self.recorded_workbench_page
            workbench_height = 112
        else:
            workbench_page = self.review_workbench_page
            workbench_height = 425
        self.workbench_stack.setCurrentWidget(workbench_page)
        self.workbench_stack.setMaximumHeight(workbench_height)
        self.recorded_summary_label.setText(
            f"Turn {projection.turn_number} の行動と結果を保存しました。"
            "左の確定履歴を確認し、次のTurnへ進んでください。"
        )

        self.rich_gemini_group.setVisible(turn_state)
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
            gemini_button.setVisible(False)

        species = self.opponent_active_input.text().strip()
        entity_id = self._opponent_entity_id(species)
        remembered_ability = self._bundle_c_controller.opponent_ability_for_entity(entity_id)
        self._render_ability_resolution(species, remembered_ability, editable)
        self._render_opponent_intel(species, remembered_ability)
        self._update_v5_action_disclosure()

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

    @staticmethod
    def _apply_first_turn_zero_stage_defaults(editor: _SideStateEditor) -> None:
        for field in editor.stage_fields.values():
            if field.unknown_box.isChecked():
                field.set_known(Known.confirmed(0, provenance_chain=_HUMAN_INPUT))

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
        switches = [checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()]
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
        # v5 has no separate facts/state phase. This trusted click is the
        # final pre-send confirmation and may dispatch only through the
        # fake/injected transport authorized for this implementation bundle.
        if (
            view.error_message is None
            and view.projection.session_state == "TURN_REVIEWED"
            and self._bundle_c_controller.rich_turn_advice_is_injected()
        ):
            self._on_trusted_send_turn_to_gemini()

    def _on_record_action(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        opponent_type = self.opponent_action_type_box.currentText()
        if opponent_type == "選択してください":
            opponent_type = ""
        if opponent_type in {"NO ACTION", "UNKNOWN"}:
            self.opponent_action_name_input.setText(opponent_type)
            opponent_type = ""
        action_type = self.actual_action_type_box.currentText()
        action_name = self.actual_action_name_box.currentText().strip()
        opponent_name = self.opponent_action_name_input.text().strip()

        # Ordinary confirmed SWITCH -> automatic state transition
        # (5224627634 section F): the destination is already the human's
        # own confirmed actual-action selection above, so this requires no
        # additional operator input. The state-change editors below never
        # accept a manual active-identity edit -- see _SideDeltaEditor.
        if action_type == "SWITCH" and action_name:
            self_side_delta = self._bundle_c_controller.compute_confirmed_switch_side_delta(
                side="self", destination_pokemon_name=action_name
            )
        else:
            self_side_delta = self.self_delta_editor.to_side_delta()
        if opponent_type == "SWITCH" and opponent_name:
            opponent_side_delta = self._bundle_c_controller.compute_confirmed_switch_side_delta(
                side="opponent", destination_pokemon_name=opponent_name
            )
        else:
            opponent_side_delta = self.opponent_delta_editor.to_side_delta()

        view = self._bundle_c_controller.record_actual_action(
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
        self.render_view(view)

    # -- Battle Record v5 interaction helpers --------------------------------

    @staticmethod
    def _opponent_entity_id(species: str) -> str:
        return f"opponent-active:{species.strip() or 'unknown'}"

    def _render_ability_resolution(
        self, species: str, remembered: str | None, editable: bool
    ) -> None:
        candidates = self._bundle_c_controller.opponent_ability_candidates(species)
        should_ask = editable and remembered is None and len(candidates) > 1
        self.ability_resolution_group.setVisible(should_ask)
        if not should_ask:
            return
        current_items = tuple(
            self.opponent_ability_box.itemText(index)
            for index in range(self.opponent_ability_box.count())
        )
        if current_items != candidates:
            self.opponent_ability_box.clear()
            self.opponent_ability_box.addItems(candidates)

    def _on_opponent_species_changed(self, species: str) -> None:
        remembered = self._bundle_c_controller.opponent_ability_for_entity(
            self._opponent_entity_id(species)
        )
        summary = self._bundle_c_controller.turn_state_summary()
        editable = summary.identity is not None and summary.confirmed_state is None
        self._render_ability_resolution(species, remembered, editable)
        self._render_opponent_intel(species, remembered)

    def _render_opponent_intel(self, species: str, remembered: str | None) -> None:
        self.opponent_intel_widget.render_intel(
            build_opponent_intel(
                species=species,
                match_facts=MatchOpponentFacts(ability=remembered),
                provider=self._opponent_meta_provider,
            )
        )

    def _on_confirm_opponent_ability(self, _checked: bool = False) -> None:
        species = self.opponent_active_input.text().strip()
        ability = self.opponent_ability_box.currentText()
        confirmed = self._bundle_c_controller.confirm_opponent_ability(
            opponent_entity_id=self._opponent_entity_id(species),
            species=species,
            ability=ability,
        )
        self.ability_resolution_group.setVisible(confirmed is None)
        if confirmed is not None:
            entry = find_effect(confirmed)
            if entry is not None:
                self.review_effect_candidate.propose(entry, prefix=f"相手の{entry.display_name_ja}")
        self.render_view()

    def _open_state_event_dialog(self, context: str) -> None:
        callback = self._apply_review_effect if context == "review" else self._apply_result_effect
        dialog = _StateEventDialog(self, context=context, apply_callback=callback)
        self._state_event_dialog = dialog
        dialog.show()

    def _propose_actual_action_effect(self, name: str) -> None:
        if self.actual_action_type_box.currentText() != "MOVE":
            return
        entry = find_effect(name)
        if entry is not None:
            self.result_effect_candidate.propose(entry, prefix=f"自分 {entry.display_name_ja}")

    def _propose_opponent_action_effect(self, name: str) -> None:
        if self.opponent_action_type_box.currentText() != "MOVE":
            return
        entry = find_effect(name)
        if entry is not None:
            self.result_effect_candidate.propose(entry, prefix=f"相手 {entry.display_name_ja}")

    def _update_v5_action_disclosure(self, _value: str = "") -> None:
        own_type = self.actual_action_type_box.currentText()
        self.actual_action_name_box.setVisible(own_type in {"MOVE", "SWITCH"})
        opponent_type = self.opponent_action_type_box.currentText()
        self.opponent_action_name_input.setVisible(opponent_type in {"MOVE", "SWITCH"})
        if opponent_type in {"NO ACTION", "UNKNOWN"}:
            self.opponent_action_name_input.setText(opponent_type)

    def _apply_review_effect(self, entry: EffectCatalogEntry) -> None:
        self._apply_effect(entry, source_side="opponent", result_phase=False)

    def _apply_result_effect(self, entry: EffectCatalogEntry) -> None:
        source_side = (
            "opponent"
            if self.opponent_action_type_box.currentText() == "MOVE"
            and find_effect(self.opponent_action_name_input.text()) == entry
            else "self"
        )
        self._apply_effect(entry, source_side=source_side, result_phase=True)

    def _apply_effect(
        self, entry: EffectCatalogEntry, *, source_side: str, result_phase: bool
    ) -> None:
        target_side = source_side
        if entry.target is EffectTarget.OPPONENT:
            target_side = "self" if source_side == "opponent" else "opponent"
        if result_phase:
            side_editor = (
                self.self_delta_editor if target_side == "self" else self.opponent_delta_editor
            )
            self._apply_effects_to_delta(entry, side_editor)
        else:
            state_editor = (
                self.self_state_editor if target_side == "self" else self.opponent_state_editor
            )
            self._apply_effects_to_state(entry, state_editor)
        self._apply_field_effects(entry, result_phase=result_phase)

    @staticmethod
    def _stage_effect(effect: str) -> tuple[str, int] | None:
        mapping = {
            "攻撃": "attack_stage",
            "防御": "defense_stage",
            "特攻": "special_attack_stage",
            "特防": "special_defense_stage",
            "素早さ": "speed_stage",
            "命中": "accuracy_stage",
            "回避": "evasion_stage",
        }
        for label, field_name in mapping.items():
            if effect.startswith(label) and effect[len(label) :] in {
                "+1",
                "+2",
                "+3",
                "-1",
                "-2",
                "-3",
            }:
                return field_name, int(effect[len(label) :])
        return None

    def _apply_effects_to_state(self, entry: EffectCatalogEntry, editor: _SideStateEditor) -> None:
        if any("能力変化を0" in effect for effect in entry.deterministic_effects):
            for field in editor.stage_fields.values():
                field.set_known(Known.confirmed(0, provenance_chain=_HUMAN_INPUT))
        for effect in entry.deterministic_effects:
            stage = self._stage_effect(effect)
            if stage is not None:
                name, amount = stage
                field = editor.stage_fields[name]
                current = field.spin.value() if not field.unknown_box.isChecked() else 0
                field.set_known(
                    Known.confirmed(
                        max(-6, min(6, current + amount)),
                        provenance_chain=_HUMAN_INPUT,
                    )
                )
            elif effect in {"やけど", "まひ", "もうどく", "ねむり"}:
                editor.status_field.set_known(
                    Known.confirmed(effect, provenance_chain=_HUMAN_INPUT)
                )

    def _apply_effects_to_delta(self, entry: EffectCatalogEntry, editor: _SideDeltaEditor) -> None:
        if any("能力変化を0" in effect for effect in entry.deterministic_effects):
            for field in editor.stage_fields.values():
                field.mode_box.setCurrentText("CHANGED")
                field.spin.setValue(0)
        for effect in entry.deterministic_effects:
            stage = self._stage_effect(effect)
            if stage is not None:
                name, amount = stage
                field = editor.stage_fields[name]
                field.mode_box.setCurrentText("CHANGED")
                field.spin.setValue(max(-6, min(6, field.spin.value() + amount)))
            elif effect in {"やけど", "まひ", "もうどく", "ねむり"}:
                editor.status_field.mode_box.setCurrentText("CHANGED")
                editor.status_field.line.setText(effect)

    def _apply_field_effects(self, entry: EffectCatalogEntry, *, result_phase: bool) -> None:
        for effect in entry.deterministic_effects:
            if effect.startswith("天候:"):
                value = effect.removeprefix("天候:")
                if result_phase:
                    self.weather_delta_field.mode_box.setCurrentText("CHANGED")
                    self.weather_delta_field.line.setText(value)
                else:
                    self.weather_field.set_known(
                        Known.confirmed(value, provenance_chain=_HUMAN_INPUT)
                    )
            elif effect.startswith("場:"):
                value = effect.removeprefix("場:")
                if result_phase:
                    self.terrain_delta_field.mode_box.setCurrentText("CHANGED")
                    self.terrain_delta_field.line.setText(value)
                else:
                    self.terrain_field.set_known(
                        Known.confirmed(value, provenance_chain=_HUMAN_INPUT)
                    )

    def _on_trusted_send_turn_to_gemini(self) -> None:
        if not self._persistence_reads_allowed:
            return
        warnings = tuple(
            part.strip() for part in self.mock_turn_warnings_input.text().split(";") if part.strip()
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
