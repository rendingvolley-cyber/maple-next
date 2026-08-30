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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from maple_next.application.service import DomainError
from maple_next.capture.contracts import VideoCaptureBackend
from maple_next.domain.battle_events import (
    COMMON_STAGE_EVENT_PRESETS,
    MAJOR_STATUS_CLEAR_LABEL,
    MAJOR_STATUS_PRESETS,
    MAX_STAGE,
    MIN_STAGE,
    STAGE_EVENT_PRESETS,
    StageEventPreset,
    clamp_stage,
)
from maple_next.domain.effect_catalog import (
    EFFECT_CATALOG,
    EffectCatalogEntry,
    EffectTarget,
    EffectTiming,
    find_effect,
)
from maple_next.domain.enums import HpBucket, MatchOutcome
from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.domain.mega_evolution import MegaSide, deterministic_mega_form
from maple_next.domain.move_catalog import MoveMatcher, normalize_move_query
from maple_next.domain.opponent_intel import (
    ChainedOpponentMetaProvider,
    LocalJsonOpponentMetaProvider,
    MatchOpponentFacts,
    OpponentIntelView,
    OpponentMetaProvider,
    SnapshotOpponentMetaProvider,
    build_opponent_intel,
    species_has_entry_relevant_ability,
)
from maple_next.domain.species_ability_catalog import SpeciesCatalogCoverageError
from maple_next.domain.turn_state import (
    FieldDelta,
    Known,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
)
from maple_next.opponent_intel_db.generation_store import GenerationStoreError
from maple_next.opponent_intel_db.runtime_intel import (
    RuntimeIntelBundle,
    resolve_runtime_intel_bundle,
)
from maple_next.opponent_intel_db.runtime_paths import (
    intel_db_directory,
    resolve_intel_runtime_root,
)
from maple_next.selection_roi.contracts import SelectionSlotMatch
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.turn_ocr import TurnSnapshotStatus
from maple_next.ui.controller import OperatorView, TurnAdviceView
from maple_next.ui.move_autocomplete import MoveAutocompletePopup
from maple_next.ui.opponent_intel_charts import (
    ReadableRankedListWidget,
    render_entries_as_text,
    top_ranked_entries,
)
from maple_next.ui.turn_snapshot_official_window import TurnSnapshotMatchFlowWindow
from maple_next.ui.turn_snapshot_window import _TURN_SNAPSHOT_ORIGIN_OCR
from maple_next.ui.turn_state_flow import TurnStateFlowController, TurnStateSummaryView

_TURN_OCR_ERROR_STATUSES = frozenset(
    {
        TurnSnapshotStatus.NO_FRAME,
        TurnSnapshotStatus.FRAME_STALE,
        TurnSnapshotStatus.FRAME_NOT_CANONICAL,
        TurnSnapshotStatus.SCENE_NOT_READY,
        TurnSnapshotStatus.OCR_UNAVAILABLE,
        TurnSnapshotStatus.OCR_FAILED,
        TurnSnapshotStatus.STALE_RESULT_DISCARDED,
    }
)


def _normalize_move_name(name: str) -> str:
    return normalize_move_query(name)


#: Opponent INTEL chart visible limits -- tail data remains available in the
#: INTEL detail dialog, which reads the full unranked lists separately.
_CHART_MOVES_LIMIT = 5
_CHART_ABILITIES_LIMIT = 3
_CHART_ITEMS_LIMIT = 5

_RankedChartEntries = tuple[
    list[tuple[str, float | None, bool]],
    list[tuple[str, float | None, bool]],
    list[tuple[str, float | None, bool]],
]


def _ranked_chart_entries(view: OpponentIntelView) -> _RankedChartEntries:
    """Sorted, top-N, observed-flagged entries for the moves/abilities/items
    bar charts -- the one place visible-limit truncation happens, shared by
    the real chart build and its plain-text fail-soft fallback. A move is
    "observed" when actually used this match; an ability/item is "observed"
    when it is this match's human-confirmed one -- the same badge the bar
    chart already draws for moves, now applied consistently to all three."""

    meta = view.meta
    if meta is None:
        return [], [], []
    observed_moves = {_normalize_move_name(name) for name in view.observed_moves}
    move_entries = top_ranked_entries(
        [
            (entry.name, entry.percentage, _normalize_move_name(entry.name) in observed_moves)
            for entry in meta.moves
        ],
        _CHART_MOVES_LIMIT,
    )
    ability_entries = top_ranked_entries(
        [
            (
                entry.name,
                entry.percentage,
                view.ability_confirmed and entry.name == view.ability,
            )
            for entry in meta.abilities
        ],
        _CHART_ABILITIES_LIMIT,
    )
    item_entries = top_ranked_entries(
        [
            (entry.name, entry.percentage, view.item_confirmed and entry.name == view.item)
            for entry in meta.items
        ],
        _CHART_ITEMS_LIMIT,
    )
    return move_entries, ability_entries, item_entries


def _looks_stale(date_text: str | None) -> bool:
    if not date_text:
        return False
    try:
        parsed = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed) > timedelta(days=_STALE_SNAPSHOT_AGE_DAYS)

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

#: Tournament hotfix (直接スタット・ステージ入力): the direct-entry dialog's
#: own row labels/order -- matches the operator-facing spec exactly, kept
#: separate from ``_STAGE_FIELDS`` (used by the pre-existing compact grids)
#: so this new surface never changes wording anywhere else.
_DIRECT_STAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("attack_stage", "攻撃"),
    ("defense_stage", "防御"),
    ("special_attack_stage", "特攻"),
    ("special_defense_stage", "特防"),
    ("speed_stage", "素早さ"),
    ("accuracy_stage", "命中率"),
    ("evasion_stage", "回避率"),
)

_RICH_STATUS_LABELS = {
    "UNAVAILABLE": "利用不可",
    "IDLE": "待機中",
    "PENDING": "送信中…",
    "SUCCESS": "受領済み",
    "FAILED": "失敗",
    "STALE": "STALE（盤面が進んだため無効）",
    "INVALID_PAYLOAD": "INVALID_PAYLOAD（応答内容が不正）",
    "REJECTED": "REJECTED（拒否・重複）",
}

#: Statuses meaning the last real Turn Gemini send did not succeed. These
#: remain visible as an explicit retry state for the same human-controlled
#: send surface.
_TURN_ADVICE_FAILURE_STATUSES = frozenset({"FAILED", "STALE", "INVALID_PAYLOAD", "REJECTED"})

_TURN_ADVICE_PLAYER_FAILURE_MESSAGES = {
    "FAILED": "Gemini送信に失敗しました。再送してください。",
    "STALE": "盤面が更新されたため、この提案は無効です。",
    "INVALID_PAYLOAD": "Geminiの応答を使用できませんでした。再送してください。",
    "REJECTED": "Geminiの提案を使用できませんでした。再送してください。",
}

_TECHNICAL_PREDICTION_TOKENS = frozenset(
    {
        "DAMAGING_MOVE",
        "NON_DAMAGING_MOVE",
        "POPULATION_PRIOR",
        "support_basis",
        "support_level",
    }
)


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

    def reset(self) -> None:
        """Back to a fresh UNCHANGED draft -- never carries a prior Turn's
        CHANGED value forward into a new Turn identity."""

        self.unknown_box.setChecked(False)
        self.spin.setValue(0)
        self.mode_box.setCurrentText("UNCHANGED")


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

    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.line.clear()
        self.mode_box.setCurrentText("UNCHANGED")


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

    def set_fainted(self) -> None:
        """Quick-set this result-delta HP field to a confirmed faint.

        A faint is not a separate concept here: it is exactly
        ``hp_bucket -> HpBucket.ZERO`` on the same CHANGED ``FieldDelta``
        channel :meth:`to_delta` already produces, so the value flows
        through the unchanged "行動・結果記録" -> :class:`ActionResultDelta`
        persistence and, downstream, the existing
        ``domain.legal_switches.is_confirmed_fainted`` predicate. No faint
        flag, no second value.
        """

        self.unknown_box.setChecked(False)
        self.value_box.setCurrentText(HpBucket.ZERO.value)
        self.mode_box.setCurrentText("CHANGED")

    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.value_box.setCurrentIndex(0)
        self.mode_box.setCurrentText("UNCHANGED")


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

    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.line.clear()
        self.mode_box.setCurrentText("UNCHANGED")


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
    and never read from here. Legacy callers retain ``active=UNCHANGED``;
    Result Entry requests ``active=UNKNOWN`` for its intentionally unobserved
    event-only draft, while the caller still substitutes the computed delta
    when an explicit switch was confirmed.
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

    def mark_fainted(self) -> None:
        """Record this side's active Pokemon as fainted on the normal
        result-delta surface. Delegates to :meth:`_DeltaHpField.set_fainted`
        -- the only state touched is the existing HP ``FieldDelta`` this
        editor already contributes to :meth:`to_side_delta`; the operator's
        subsequent "行動・結果記録" click is still the sole persistence."""

        self.hp_field.set_fainted()

    def to_side_delta(self, *, unobserved_as_unknown: bool = False) -> SideDelta:
        return SideDelta(
            active=(
                FieldDelta.unknown()
                if unobserved_as_unknown
                else FieldDelta.unchanged()
            ),
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

    def reset(self, *, unobserved_as_unknown: bool = False) -> None:
        """Identity-bound reset (00 R2 lifecycle fix): every CHANGED/
        UNKNOWN value a human entered for one Turn's result must never
        survive into the next Turn's own result-delta draft. A confirmed
        CHANGED value from Turn N still carries forward correctly through
        the *domain projection* into Turn N+1's current STATE -- this only
        clears the separate, reusable result-delta editor widgets."""

        self.hp_field.reset()
        self.side_effects_field.reset()
        self.status_preset_box.setCurrentIndex(0)
        self.status_field.reset()
        self.event_preset_box.setCurrentIndex(0)
        self.event_preview_label.setText("")
        self.event_apply_button.setEnabled(False)
        self._pending_stage_preview = {}
        for field in self.stage_fields.values():
            field.reset()
        if unobserved_as_unknown:
            self.hp_field.mode_box.setCurrentText("UNKNOWN")
            self.status_field.mode_box.setCurrentText("UNKNOWN")
            self.side_effects_field.mode_box.setCurrentText("UNKNOWN")
            for field in self.stage_fields.values():
                field.mode_box.setCurrentText("UNKNOWN")


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


_TARGET_SIDE_LABELS: dict[str, str] = {"self": "自分", "opponent": "相手"}


@dataclass(frozen=True, slots=True)
class _ResultEventDraft:
    """UI-only description of one human-confirmed result event.

    Ordinary result events are projected back into the existing ``SideDelta``
    editors. A ``mega`` event is deliberately match-level draft metadata and
    is passed through the typed action commit boundary instead; it is never a
    SideDelta field or a persisted parallel UI model.
    """

    event_id: str
    target_side: str
    pokemon_name: str
    kind: str
    field_name: str
    value: int | str
    current_form: str | None = None
    source_move: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class _MoveResultCandidate:
    candidate_id: str
    source_side: str
    source_pokemon: str
    source_move: str
    target_side: str
    target_pokemon: str
    kind: str
    field_name: str
    value: int | str
    display_effect: str


class _MoveResultCandidateCard(QGroupBox):
    """Direct OCCURRED / DID_NOT_OCCUR control for one possible effect."""

    def __init__(
        self,
        candidate: _MoveResultCandidate,
        decide: Callable[[_MoveResultCandidate, str], None],
    ) -> None:
        super().__init__()
        self.candidate = candidate
        self.setObjectName("moveResultCandidate")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 4, 7, 4)
        description = QLabel(
            f"{_TARGET_SIDE_LABELS[candidate.target_side]}：{candidate.display_effect}\n"
            f"原因：{candidate.source_move}"
        )
        description.setWordWrap(True)
        layout.addWidget(description, 1)
        self.occurred_button = QPushButton("起きた")
        self.did_not_occur_button = QPushButton("起きてない")
        self.occurred_button.setCheckable(True)
        self.did_not_occur_button.setCheckable(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.occurred_button)
        group.addButton(self.did_not_occur_button)
        self.occurred_button.clicked.connect(
            lambda: decide(self.candidate, "OCCURRED")
        )
        self.did_not_occur_button.clicked.connect(
            lambda: decide(self.candidate, "DID_NOT_OCCUR")
        )
        layout.addWidget(self.occurred_button)
        layout.addWidget(self.did_not_occur_button)

    def set_decision(self, decision: str) -> None:
        self.occurred_button.setChecked(decision == "OCCURRED")
        self.did_not_occur_button.setChecked(decision == "DID_NOT_OCCUR")


class _ManualResultDialog(QDialog):
    """Small manual fallback for canonical stage and major-status events."""

    def __init__(
        self,
        parent: QWidget,
        *,
        add_event: Callable[[str, str, int | str], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("手入力で結果を追加")
        self.setModal(False)
        self._add_event = add_event
        layout = QFormLayout(self)
        self.target_box = QComboBox()
        self.target_box.addItem("自分", "self")
        self.target_box.addItem("相手", "opponent")
        self.kind_box = QComboBox()
        self.kind_box.addItem("能力変化", "stage")
        self.kind_box.addItem("状態変化", "status")
        self.stage_box = QComboBox()
        for field_name, label in _DIRECT_STAGE_FIELDS:
            self.stage_box.addItem(label, field_name)
        self.amount_box = QComboBox()
        for amount in (-2, -1, 1, 2):
            self.amount_box.addItem(f"{amount:+d}", amount)
        self.status_box = QComboBox()
        self.status_box.addItems(MAJOR_STATUS_PRESETS)
        layout.addRow("対象", self.target_box)
        layout.addRow("種類", self.kind_box)
        layout.addRow("能力", self.stage_box)
        layout.addRow("変化量", self.amount_box)
        layout.addRow("状態", self.status_box)
        buttons = QHBoxLayout()
        add_button = QPushButton("追加")
        cancel_button = QPushButton("キャンセル")
        add_button.clicked.connect(self._on_add)
        cancel_button.clicked.connect(self.close)
        buttons.addWidget(add_button)
        buttons.addWidget(cancel_button)
        layout.addRow(buttons)
        self.kind_box.currentIndexChanged.connect(self._sync_kind)
        self._sync_kind()

    def _sync_kind(self, _index: int = 0) -> None:
        is_stage = self.kind_box.currentData() == "stage"
        self.stage_box.setVisible(is_stage)
        self.amount_box.setVisible(is_stage)
        self.status_box.setVisible(not is_stage)

    def _on_add(self, _checked: bool = False) -> None:
        side = str(self.target_box.currentData())
        if self.kind_box.currentData() == "stage":
            self._add_event(
                side,
                str(self.stage_box.currentData()),
                int(self.amount_box.currentData()),
            )
        else:
            self._add_event(side, "status", self.status_box.currentText())
        self.accept()


class _DirectStageEditorDialog(QDialog):
    """Tournament hotfix: the first-visible surface behind "＋ 状態変化を
    記録" for the Action Result draft. No effect-catalog search, no
    sub-menu -- target side, common ability-change presets, and direct
    per-stat +1/-1 rank buttons are all on screen immediately.

    This dialog never persists anything itself. Every preset click and
    every +1/-1 click only edits an in-dialog pending-changes draft. In the
    redesigned Result Entry, "適用" hands those values to its canonical
    result-event draft callback; the legacy action surface still writes the
    existing ``_SideDeltaEditor`` widgets directly. Both ultimately use the
    same ``SideDelta`` persistence path and invent no second battle model.

    A field whose current confirmed stage is UNKNOWN never has an assumed
    baseline of 0: its row shows "現在ランク不明" and its +1/-1 buttons
    stay disabled until a known value exists (via the secondary
    その他/詳細 route's manual entry) or a preset that does not touch it.

    Presets are atomic: a preset touches several fields at once (e.g.
    からをやぶる touches five), and a real ability-stage move either
    happens in full or not at all. If any one required baseline is
    UNKNOWN, or any one component would exceed +6/-6, *none* of the
    preset's fields are queued -- a partial apply would misrepresent the
    actual battle state (e.g. recording からをやぶる's +2/+2/+2 without
    its -1/-1 drops). Direct individual +1/-1 entry is unaffected by this
    rule -- it only ever touches the one field the operator clicked.
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        self_editor: _SideDeltaEditor,
        opponent_editor: _SideDeltaEditor,
        known_stages_fn: Callable[[str], dict[str, Known[int]]],
        open_legacy: Callable[[], None],
        apply_stage_changes: Callable[[dict[str, dict[str, int]]], bool] | None = None,
        initial_target: str = "self",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("状態変化を記録")
        self.setModal(False)
        self._editors: dict[str, _SideDeltaEditor] = {
            "self": self_editor,
            "opponent": opponent_editor,
        }
        self._known: dict[str, dict[str, Known[int]]] = {
            "self": known_stages_fn("self"),
            "opponent": known_stages_fn("opponent"),
        }
        self._apply_stage_changes = apply_stage_changes
        self._pending: dict[str, dict[str, int]] = {"self": {}, "opponent": {}}
        self._target = initial_target if initial_target in ("self", "opponent") else "self"

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("対象"))
        target_row = QHBoxLayout()
        self.target_self_button = QPushButton(_TARGET_SIDE_LABELS["self"])
        self.target_opponent_button = QPushButton(_TARGET_SIDE_LABELS["opponent"])
        self.target_self_button.setCheckable(True)
        self.target_opponent_button.setCheckable(True)
        self._target_group = QButtonGroup(self)
        self._target_group.setExclusive(True)
        self._target_group.addButton(self.target_self_button)
        self._target_group.addButton(self.target_opponent_button)
        (
            self.target_self_button
            if self._target == "self"
            else self.target_opponent_button
        ).setChecked(True)
        self.target_self_button.clicked.connect(lambda: self._set_target("self"))
        self.target_opponent_button.clicked.connect(lambda: self._set_target("opponent"))
        target_row.addWidget(self.target_self_button)
        target_row.addWidget(self.target_opponent_button)
        target_row.addStretch(1)
        layout.addLayout(target_row)

        preset_group = QGroupBox("よく使う能力変化")
        preset_layout = QHBoxLayout(preset_group)
        for preset in COMMON_STAGE_EVENT_PRESETS:
            button = QPushButton(preset.label)
            button.clicked.connect(
                lambda _checked=False, candidate=preset: self._on_preset_clicked(candidate)
            )
            preset_layout.addWidget(button)
        layout.addWidget(preset_group)

        direct_group = QGroupBox("能力ランクを直接変更")
        direct_grid = QGridLayout(direct_group)
        self._value_labels: dict[str, QLabel] = {}
        self._minus_buttons: dict[str, QPushButton] = {}
        self._plus_buttons: dict[str, QPushButton] = {}
        for row, (field_name, label) in enumerate(_DIRECT_STAGE_FIELDS):
            direct_grid.addWidget(QLabel(label), row, 0)
            value_label = QLabel()
            value_label.setMinimumWidth(90)
            direct_grid.addWidget(value_label, row, 1)
            minus_button = QPushButton("-1")
            minus_button.clicked.connect(
                lambda _checked=False, name=field_name: self._on_adjust(name, -1)
            )
            direct_grid.addWidget(minus_button, row, 2)
            plus_button = QPushButton("+1")
            plus_button.clicked.connect(
                lambda _checked=False, name=field_name: self._on_adjust(name, 1)
            )
            direct_grid.addWidget(plus_button, row, 3)
            self._value_labels[field_name] = value_label
            self._minus_buttons[field_name] = minus_button
            self._plus_buttons[field_name] = plus_button
        layout.addWidget(direct_group)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 9px; color: #2563eb;")
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("変更予定:"))
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        layout.addWidget(self.pending_label)

        actions = QHBoxLayout()
        self.apply_button = QPushButton("適用")
        self.apply_button.clicked.connect(self._on_apply)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.close)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

        legacy_row = QHBoxLayout()
        legacy_row.addStretch(1)
        self.legacy_button = QPushButton("その他 / 詳細")
        self.legacy_button.clicked.connect(lambda: open_legacy())
        legacy_row.addWidget(self.legacy_button)
        layout.addLayout(legacy_row)

        self._refresh()

    def _set_target(self, side: str) -> None:
        self._target = side
        self._refresh()

    def _known_value(self, side: str, field_name: str) -> int | None:
        known = self._known[side].get(field_name)
        if known is not None and known.is_confirmed and known.value is not None:
            return known.value
        return None

    def _effective_baseline(self, side: str, field_name: str) -> int | None:
        pending_value = self._pending[side].get(field_name)
        if pending_value is not None:
            return pending_value
        return self._known_value(side, field_name)

    def _preset_plan(
        self, side: str, preset: StageEventPreset
    ) -> tuple[dict[str, int], list[str], list[str]]:
        """Read-only: the candidate absolute stage values a preset *would*
        set for ``side``, plus any fields blocked by an UNKNOWN baseline or
        a +6/-6 boundary violation. Never mutates ``self._pending`` --
        computing the whole plan up front, before touching any state, is
        what makes "block the whole preset" possible instead of a previous
        field's queued value surviving a later field's failure."""

        candidates: dict[str, int] = {}
        unknown_fields: list[str] = []
        overflow_fields: list[str] = []
        for field_name, delta in preset.deltas:
            baseline = self._effective_baseline(side, field_name)
            if baseline is None:
                unknown_fields.append(field_name)
                continue
            raw = baseline + delta
            if raw > MAX_STAGE or raw < MIN_STAGE:
                overflow_fields.append(field_name)
                continue
            candidates[field_name] = raw
        return candidates, unknown_fields, overflow_fields

    def _on_preset_clicked(self, preset: StageEventPreset) -> None:
        side = self._target
        candidates, unknown_fields, overflow_fields = self._preset_plan(side, preset)
        if unknown_fields or overflow_fields:
            # Atomic: からをやぶる etc. either queues all of its fields or
            # none of them -- never a partial move that would misrepresent
            # what actually happened in the battle. The existing draft
            # (self._pending) is left completely untouched.
            label_by_key = dict(_DIRECT_STAGE_FIELDS)
            messages = []
            if unknown_fields:
                names = "、".join(label_by_key.get(name, name) for name in unknown_fields)
                messages.append(
                    f"現在ランク不明の能力があるためプリセットを適用できません（{names}）。"
                )
            if overflow_fields:
                names = "、".join(label_by_key.get(name, name) for name in overflow_fields)
                messages.append(
                    f"上限(+6)/下限(-6)を超えるためプリセットを適用できません（{names}）。"
                )
            self.status_label.setText(f"{preset.label}: " + " ".join(messages))
            self._refresh()
            return
        self._pending[side].update(candidates)
        target_label = _TARGET_SIDE_LABELS[side]
        self.status_label.setText(f"{preset.label} を{target_label}に反映しました。")
        self._refresh()

    def _on_adjust(self, field_name: str, delta: int) -> None:
        side = self._target
        baseline = self._effective_baseline(side, field_name)
        if baseline is None:
            return
        self._pending[side][field_name] = clamp_stage(baseline + delta)
        self._refresh()

    def _refresh(self) -> None:
        for button, side in (
            (self.target_self_button, "self"),
            (self.target_opponent_button, "opponent"),
        ):
            button.setChecked(side == self._target)
        side = self._target
        for field_name, _label in _DIRECT_STAGE_FIELDS:
            known_value = self._known_value(side, field_name)
            pending_value = self._pending[side].get(field_name)
            value_label = self._value_labels[field_name]
            if pending_value is not None:
                known_display = "不明" if known_value is None else f"{known_value:+d}"
                value_label.setText(f"{known_display} → {pending_value:+d}")
            elif known_value is not None:
                value_label.setText(f"{known_value:+d}")
            else:
                value_label.setText("現在ランク不明")
            effective = pending_value if pending_value is not None else known_value
            self._minus_buttons[field_name].setEnabled(
                effective is not None and effective > MIN_STAGE
            )
            self._plus_buttons[field_name].setEnabled(
                effective is not None and effective < MAX_STAGE
            )
        self.pending_label.setText(self._pending_summary())
        self.apply_button.setEnabled(any(self._pending.values()))

    def _pending_summary(self) -> str:
        label_by_key = dict(_DIRECT_STAGE_FIELDS)
        lines: list[str] = []
        for side in ("self", "opponent"):
            pending = self._pending[side]
            if not pending:
                continue
            parts = []
            for field_name, candidate in pending.items():
                known_value = self._known_value(side, field_name)
                current_display = "不明" if known_value is None else f"{known_value:+d}"
                label = label_by_key.get(field_name, field_name)
                parts.append(f"{label} {current_display}→{candidate:+d}")
            lines.append(f"{_TARGET_SIDE_LABELS[side]}: " + " / ".join(parts))
        return "\n".join(lines) if lines else "（変更予定なし）"

    def _on_apply(self, _checked: bool = False) -> None:
        if self._apply_stage_changes is not None:
            pending = {side: dict(changes) for side, changes in self._pending.items()}
            if self._apply_stage_changes(pending):
                self.accept()
            return
        for side, editor in self._editors.items():
            for field_name, candidate in self._pending[side].items():
                field = editor.stage_fields[field_name]
                field.mode_box.setCurrentText("CHANGED")
                field.spin.setValue(candidate)
        self.accept()


_STALE_SNAPSHOT_AGE_DAYS = 14  # freshness warning threshold; never blocks any Turn control.


class _OpponentIntelWidget(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Opponent INTEL")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        head = QHBoxLayout()
        self.species_label = QLabel("不明")
        self.species_label.setObjectName("intelSpecies")
        species_font = self.species_label.font()
        species_font.setBold(True)
        species_font.setPointSize(species_font.pointSize() + 3)
        self.species_label.setFont(species_font)
        self.simple_badge = QLabel("簡易")
        self.simple_badge.setProperty("badge", True)
        head.addWidget(self.species_label, 1)
        head.addWidget(self.simple_badge)
        layout.addLayout(head)
        self.context_label = QLabel("active opponent / 対戦情報優先")
        self.context_label.setProperty("muted", True)
        layout.addWidget(self.context_label)

        # Confirmed-this-match fact chips: always visually above/more
        # prominent than the population-statistics charts below.
        self.fact_chip_row = QHBoxLayout()
        self.fact_chip_row.setSpacing(6)
        self.fact_chips: dict[str, QLabel] = {}
        for key in ("ability", "item", "moves"):
            chip = QLabel()
            chip.setProperty("factChip", True)
            chip_font = chip.font()
            chip_font.setBold(True)
            chip.setFont(chip_font)
            chip.setWordWrap(True)
            self.fact_chips[key] = chip
            self.fact_chip_row.addWidget(chip)
        self.fact_chip_row.addStretch(1)
        layout.addLayout(self.fact_chip_row)

        self.facts_label = QLabel("この対戦で判明：特性 不明 / 持ち物 不明 / 観測技 不明")
        self.facts_label.setWordWrap(True)
        self.facts_label.setObjectName("intelFacts")
        layout.addWidget(self.facts_label)

        # Population-statistics charts: secondary to the fact chips above.
        # Vertically stacked categories (moves / abilities / items), never
        # side-by-side columns -- a 3-across layout leaves too little
        # horizontal room for a full Japanese move/item name and forces
        # truncation. Each row below shows the complete name; only the
        # visible-limit truncation in _ranked_chart_entries (top N per
        # category, unchanged) decides which rows appear at all.
        self.chart_section = QWidget()
        self.chart_layout = QVBoxLayout(self.chart_section)
        self.chart_layout.setSpacing(8)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chart_section, 1)

        self.footer_label = QLabel("")
        self.footer_label.setWordWrap(True)
        self.footer_label.setProperty("muted", True)
        self.footer_label.setObjectName("intelFooter")
        layout.addWidget(self.footer_label)

        self.detail_button = QPushButton("INTEL詳細を表示")
        layout.addWidget(self.detail_button)
        self._view: OpponentIntelView | None = None
        self._detail_dialog: QDialog | None = None
        self._detail_sections: dict[str, QGroupBox] = {}
        self.detail_button.clicked.connect(self._open_detail)

    def render_intel(self, view: OpponentIntelView) -> None:
        self._view = view
        self.species_label.setText(view.species)
        moves = ", ".join(view.observed_moves) or "不明"
        self.facts_label.setText(
            f"この対戦で判明：特性 {view.ability} / 持ち物 {view.item} / 観測技 {moves}"
        )
        moves_confirmed = bool(view.observed_moves)
        self.fact_chips["ability"].setText(
            f"特性: {view.ability} {'✓' if view.ability_confirmed else '(未確認)'}"
        )
        self.fact_chips["item"].setText(
            f"持ち物: {view.item} {'✓' if view.item_confirmed else '(未確認)'}"
        )
        self.fact_chips["moves"].setText(
            f"観測技: {moves} {'✓' if moves_confirmed else '(未確認)'}"
        )
        self._render_charts(view)
        self._render_footer(view)

    def _clear_chart_layout(self) -> None:
        while self.chart_layout.count():
            item = self.chart_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_charts(self, view: OpponentIntelView) -> None:
        """Fail-soft: any chart-construction exception falls back to plain text."""

        self._clear_chart_layout()
        try:
            self._build_charts(view)
        except Exception:
            self._clear_chart_layout()
            fallback = QLabel(self._fallback_chart_text(view))
            fallback.setWordWrap(True)
            self.chart_layout.addWidget(fallback)

    @staticmethod
    def _fallback_chart_text(view: OpponentIntelView) -> str:
        meta = view.meta
        if meta is None:
            return "データなし"
        move_entries, ability_entries, item_entries = _ranked_chart_entries(view)
        return "\n\n".join(
            (
                "採用技:\n" + render_entries_as_text(move_entries),
                "特性:\n" + render_entries_as_text(ability_entries),
                "持ち物:\n" + render_entries_as_text(item_entries),
            )
        )

    def _build_charts(self, view: OpponentIntelView) -> None:
        move_entries, ability_entries, item_entries = _ranked_chart_entries(view)

        for title, entries in (
            ("採用技", move_entries),
            ("特性", ability_entries),
            ("持ち物", item_entries),
        ):
            group = QGroupBox(title)
            layout = QVBoxLayout(group)
            chart = ReadableRankedListWidget()
            chart.set_entries(entries)
            layout.addWidget(chart)
            self.chart_layout.addWidget(group)

    def _render_footer(self, view: OpponentIntelView) -> None:
        meta = view.meta
        if meta is None:
            self.footer_label.setText("データソース: データなし")
            return
        parts = [meta.regulation or "データなし", meta.source or "データなし"]
        if meta.source_updated_at:
            parts.append(f"source updated: {meta.source_updated_at}")
        if meta.fetched_at:
            parts.append(f"local snapshot fetched: {meta.fetched_at}")
        text = " / ".join(part for part in parts if part)
        if _looks_stale(meta.fetched_at) or _looks_stale(meta.source_updated_at):
            text += "  ⚠ データが古い可能性があります"
        self.footer_label.setText(text)

    def _open_detail(self, _checked: bool = False) -> None:
        if self._view is None:
            return
        view = self._view
        dialog = QDialog(self)
        dialog.setWindowTitle("Opponent INTEL 詳細")
        dialog.setObjectName("intelDetailDialog")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Opponent INTEL — 詳細")
        title.setProperty("dialogTitle", True)
        subtitle = QLabel("通常画面では簡易表示。必要な時だけ展開。")
        subtitle.setProperty("muted", True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(dialog.close)
        header.addLayout(titles, 1)
        header.addWidget(close_button)
        layout.addLayout(header)
        selector = QHBoxLayout()
        self.selector_buttons: list[QPushButton] = []
        selector_labels = (view.species, "相手2", "相手3", "未判明", "未判明", "未判明")
        for index, label in enumerate(selector_labels):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setProperty("chip", True)
            selector.addWidget(button)
            self.selector_buttons.append(button)
        selector.addStretch(1)
        layout.addLayout(selector)
        if view.meta is None:
            placeholder_ranking = [f"取得データ {rank}位 --%" for rank in range(1, 5)]
            placeholder_ranking.append("取得データ 5位 --% / データなし")
            move_ranking = ", ".join(placeholder_ranking)
            ability_names = view.possible_abilities or ("未判明",)
            ability_ranking = ", ".join(
                (*(f"{name} --%" for name in ability_names), "データなし")
            )
            item_ranking = ", ".join(placeholder_ranking)
            source = regulation = snapshot = "データなし"
        else:
            move_ranking = _format_rankings(view.meta.moves)
            ability_ranking = _format_rankings(view.meta.abilities)
            item_ranking = _format_rankings(view.meta.items)
            source = view.meta.source or "データなし"
            regulation = view.meta.regulation or "データなし"
            snapshot = view.meta.snapshot_date or "データなし"
        possible = ", ".join(view.possible_abilities) or "データなし"
        top_sections: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
            (
                "current_match_facts",
                "この対戦で確定・観測した事実",
                (
                    ("ability", view.ability),
                    ("item", view.item),
                    ("moves", ", ".join(view.observed_moves) or "不明"),
                ),
            ),
            (
                "source",
                "データ情報",
                (("source", source), ("regulation", regulation), ("snapshot", snapshot)),
            ),
        )
        self._detail_sections = {}
        top_row = QHBoxLayout()
        for key, section_title, rows in top_sections:
            section = QGroupBox(section_title)
            section.setObjectName(f"intelDetail_{key}")
            form = QFormLayout(section)
            for label, value in rows:
                value_label = QLabel(value)
                value_label.setWordWrap(True)
                form.addRow(label, value_label)
            self._detail_sections[key] = section
            top_row.addWidget(section, 1)
        layout.addLayout(top_row)
        ranking_row = QHBoxLayout()
        ranking_specs = (
            ("moves", "採用技ランキング", move_ranking),
            (
                "abilities",
                "特性",
                ability_ranking
                if view.meta is None
                else f"possible: {possible}\nusage: {ability_ranking}",
            ),
            ("items", "持ち物ランキング", item_ranking),
        )
        for key, section_title, value in ranking_specs:
            section = QGroupBox(section_title)
            section.setObjectName(f"intelDetail_{key}")
            section_layout = QVBoxLayout(section)
            # Full ranking, never truncated -- the main panel's top-N bars
            # are the only intentionally-limited view; this dialog exists
            # specifically to expose the rest. Scrolls rather than
            # overflowing the dialog's fixed size when the source data has
            # many entries.
            lines = value.split(", ") if value != "データなし" else ["データなし"]
            rows_container = QWidget()
            rows_layout = QVBoxLayout(rows_container)
            rows_layout.setContentsMargins(0, 0, 0, 0)
            rows_layout.setSpacing(1)
            for line in lines:
                row = QLabel(line)
                row.setProperty("rankRow", True)
                rows_layout.addWidget(row)
            rows_layout.addStretch(1)
            rows_scroll = QScrollArea()
            rows_scroll.setWidgetResizable(True)
            rows_scroll.setWidget(rows_container)
            section_layout.addWidget(rows_scroll)
            self._detail_sections[key] = section
            ranking_row.addWidget(section, 1)
        layout.addLayout(ranking_row, 1)
        layout.addStretch(1)
        dialog.setStyleSheet(
            "QDialog { background: #0b1220; color: #dbeafe; }"
            "QLabel { color: #dbeafe; font-size: 11px; }"
            "QGroupBox { background: #111827; border: 1px solid #334155; "
            "border-radius: 6px; margin-top: 8px; padding: 7px; color: #93c5fd; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
        dialog.resize(1420, 422)
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
        capture_backend: VideoCaptureBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        # State refreshes must not override a tab the operator deliberately
        # selected on this explicit two-tab operator surface.
        self._preserve_operator_tab_selection = True
        super().__init__(
            controller,
            ocr_data_directory=ocr_data_directory,
            capture_backend=capture_backend,
            auto_start_capture=auto_start_capture,
        )
        self._apply_official_windows_font()
        self._bundle_c_controller: TurnStateFlowController = controller
        self._runtime_intel_bundle = self._resolve_runtime_intel_bundle()
        self._opponent_meta_provider: OpponentMetaProvider
        if opponent_meta_provider is None:
            providers: list[OpponentMetaProvider] = []
            if self._runtime_intel_bundle is not None:
                providers.append(
                    SnapshotOpponentMetaProvider(
                        self._runtime_intel_bundle.snapshot_path,
                        document=self._runtime_intel_bundle.snapshot_document,
                    )
                )
            providers.append(
                LocalJsonOpponentMetaProvider(ocr_data_directory / "opponent_meta_cache.json")
            )
            self._opponent_meta_provider = ChainedOpponentMetaProvider(tuple(providers))
        else:
            self._opponent_meta_provider = opponent_meta_provider
        self._evidence_dialog: QDialog | None = None
        self._state_event_dialog: QDialog | None = None
        self._active_ability_entry_event_id: str | None = None
        self._provisional_ability_species: str | None = None
        self._pending_ocr_ability_confirmation: tuple[str, str] | None = None
        self._turn_ocr_status_code: str = TurnSnapshotStatus.IDLE
        self._move_matcher_cache = self._matcher_from_runtime_bundle(
            self._runtime_intel_bundle
        )
        self.opponent_move_autocomplete = MoveAutocompletePopup(
            self.opponent_action_name_input, self._load_move_matcher
        )
        self._build_bundle_c_state_widgets()
        self._restructure_battle_record_layout()
        # Normal operator landing starts in Battle Record. Explicit NEW MATCH
        # navigation still selects Selection in SelectionSnapshotWindow.
        self.header_tabs.setCurrentIndex(_BATTLE_RECORD_TAB_INDEX)
        self.actual_action_type_box.currentTextChanged.connect(
            lambda _text: self._sync_parity_action_selection()
        )
        self.actual_action_name_box.currentTextChanged.connect(
            lambda _text: self._sync_parity_action_selection()
        )
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
        self.review_state_event_button.clicked.connect(self._open_common_state_event_dialog)
        editor_layout.addWidget(self.review_state_event_button)

        # Tournament P0: the "交代できるポケモン" turn-facts checkboxes
        # (self.switch_checkboxes, mirrored by self.parity_switch_chips) were
        # labeled once from the raw selected_three and never re-derived
        # against active/fainted, and the parity mirror buttons' text was
        # never resynced after their one-time construction at all -- see
        # _sync_switch_candidates. Tracks the last-applied derived candidate
        # tuple so an unrelated re-render doesn't wipe an in-progress
        # operator checkmark.
        self._switch_checkbox_last_candidates: tuple[str, ...] | None = None

        # Bundle 2 (Gemini V2) R3R1: editable legal-switch prefill/confirm
        # workbench. Factual only -- no strategy content, no Opponent
        # INTEL, no mechanics inference. Before CONFIRM TURN FACTS this
        # list shows a CANDIDATE prefill (selected_three - active -
        # confirmed-fainted) the operator can correct; it is never itself a
        # confirmation. The same CONFIRM TURN FACTS click reads exactly
        # this visible/edited selection and persists it as the final
        # CONFIRMED_NONEMPTY/CONFIRMED_NONE legal-switch confirmation, in
        # the same revision as the rest of Turn facts -- no second click.
        # The two buttons below remain for an explicit correction after
        # that confirmation has already landed.
        self._legal_switch_prefill_candidates: tuple[str, ...] = ()
        self._legal_switch_prefill_active_text: str | None = None
        #: R3R2: the complete canonical TurnIdentity the current prefill/edit
        #: was derived under. A new session/match/generation/turn/revision
        #: must invalidate the prefill even when the active Pokemon name is
        #: unchanged -- see ``_render_legal_switch_workbench``.
        self._legal_switch_prefill_identity: TurnIdentity | None = None
        self.legal_switch_group = QGroupBox("交代可能なポケモン（Legal Switches）")
        self.legal_switch_group.setToolTip(
            "候補は自動で表示されますが、それ自体は確定ではありません。"
            "必要ならチェックを直してからCONFIRM TURN FACTSを押すと、"
            "その時点で見えている選択がそのまま確定されます。"
            "確定後の修正は下のリストとボタンで行えます。"
        )
        legal_switch_layout = QVBoxLayout(self.legal_switch_group)
        legal_switch_layout.setContentsMargins(2, 2, 2, 2)
        legal_switch_layout.setSpacing(2)
        self.legal_switch_status_label = QLabel("未確認 (UNRESOLVED)")
        legal_switch_layout.addWidget(self.legal_switch_status_label)
        self.legal_switch_list = QListWidget()
        self.legal_switch_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.legal_switch_list.setMaximumHeight(70)
        legal_switch_layout.addWidget(self.legal_switch_list)
        legal_switch_buttons_row = QHBoxLayout()
        self.confirm_legal_switches_selected_button = QPushButton("選択した交代先を確定（修正）")
        self.confirm_legal_switches_selected_button.clicked.connect(
            self._on_confirm_legal_switches_selected
        )
        self.confirm_legal_switches_none_button = QPushButton("交代先なしを確定（修正）")
        self.confirm_legal_switches_none_button.clicked.connect(
            self._on_confirm_legal_switches_none
        )
        legal_switch_buttons_row.addWidget(self.confirm_legal_switches_selected_button)
        legal_switch_buttons_row.addWidget(self.confirm_legal_switches_none_button)
        legal_switch_layout.addLayout(legal_switch_buttons_row)
        editor_layout.addWidget(self.legal_switch_group)
        # Refresh the pre-confirmation prefill whenever the operator's
        # in-progress active-Pokemon choice changes -- mirrors the existing
        # _on_turn_active_changed/_prefill_legal_moves_for_active pattern
        # for legal moves.
        self.self_active_box.currentTextChanged.connect(
            self._on_self_active_changed_for_legal_switches
        )

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

        # Faint / KO quick-record (Tournament P0). No catalog, search, or
        # submenu: each button only quick-sets its side's result-delta HP to
        # HpBucket.ZERO on the editor above (_SideDeltaEditor.mark_fainted).
        # Canonical persistence is unchanged -- the operator's existing
        # "行動・結果記録" click reads to_side_delta() and writes the one
        # ActionResultDelta, from which is_confirmed_fainted() then governs
        # legal switches, the provider-ready gate, and match export.
        faint_row = QHBoxLayout()
        self.record_opponent_faint_button = QPushButton("相手ひんし")
        self.record_opponent_faint_button.setProperty("faintControl", True)
        self.record_opponent_faint_button.setMinimumSize(128, 36)
        self.record_opponent_faint_button.setAccessibleName("相手ひんし")
        self.record_opponent_faint_button.setToolTip(
            "相手の場のポケモンをHP0（ひんし）として結果に記録します（行動・結果記録で確定）。"
        )
        self.record_opponent_faint_button.clicked.connect(self._on_record_opponent_faint)
        self.record_self_faint_button = QPushButton("自分ひんし")
        self.record_self_faint_button.setProperty("faintControl", True)
        self.record_self_faint_button.setMinimumSize(128, 36)
        self.record_self_faint_button.setAccessibleName("自分ひんし")
        self.record_self_faint_button.setToolTip(
            "自分の場のポケモンをHP0（ひんし）として結果に記録します（行動・結果記録で確定）。"
        )
        self.record_self_faint_button.clicked.connect(self._on_record_self_faint)
        faint_row.addWidget(self.record_opponent_faint_button)
        faint_row.addWidget(self.record_self_faint_button)
        faint_row.addStretch(1)
        delta_layout.addLayout(faint_row)

        self.result_effect_candidate = _EffectCandidateCard(self._apply_result_effect)
        delta_layout.addWidget(self.result_effect_candidate)
        self.result_state_event_button = QPushButton("＋ 状態変化を記録")
        self.result_state_event_button.clicked.connect(self._open_direct_stage_editor_dialog)
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
        central_widget = self.centralWidget()
        assert central_widget is not None
        outer_layout = central_widget.layout()
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
        assert left_container is not None
        assert center_container is not None
        assert right_container is not None
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
        self._extract_widget(self.match_export_group, self.new_match_after_export_button)
        outer_layout.removeWidget(self.new_match_button)
        gemini_send_button = getattr(self, "turn_gemini_send_button", None)
        if gemini_send_button is not None:
            self._extract_widget(self.turn_gemini_box, gemini_send_button)
            self.turn_gemini_box.setVisible(False)

        self.start_turn_button.setText("Turn撮影")
        # CONFIRM TURN FACTS persists reviewed facts. Provider dispatch is a
        # separate explicit action on the trusted SEND TURN TO GEMINI control.
        self.confirm_turn_facts_button.setText("CONFIRM TURN FACTS")
        self.record_action_button.setText("行動・結果記録")
        self.next_turn_button.setText("NEXT TURN")

        if gemini_send_button is not None:
            # Reuse the pre-existing trusted send control and place it in the
            # rich-state status group, where it remains adjacent to the
            # readiness/denial state it acts on.
            gemini_send_button.setText("SEND TURN TO GEMINI")
            self._rich_gemini_layout.addRow(gemini_send_button)
            self._bundle_c_gemini_send_button = gemini_send_button
        self.turn_facts_confirm_checkbox.setVisible(False)

        # Compact 2-column reflow: same widgets, same signal wiring, just
        # laid out 2-per-row instead of 1-per-row so the fixed,
        # non-scrolling center column can fit 1280x720/1440x900.
        switch_widget = self.switch_checkboxes[0].parentWidget()
        # These 3 boxes only feed the legacy Turn-facts memo field, not the
        # actual send gate below -- label them as such so they never read as
        # unexplained blank/undifferentiated boxes next to the real,
        # CONFIRM TURN FACTS-driven "Legal Switches" workbench.
        for checkbox in self.switch_checkboxes:
            checkbox.setToolTip(
                "記録用メモのみ。実際の送信可否は下部の「Legal Switches」欄"
                "（CONFIRM TURN FACTSで自動確定）で判定されます。"
            )
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

        # P0 diagnostic (OCR recapture incident follow-up): sanitized
        # capture/OCR milestone trail -- see
        # TurnSnapshotMatchFlowWindow._record_turn_ocr_milestone. Read-only
        # display; refreshed in render_view() below.
        self.turn_ocr_milestone_log_label = QLabel("(まだ記録なし)")
        self.turn_ocr_milestone_log_label.setWordWrap(True)
        self.turn_ocr_milestone_log_label.setStyleSheet("font-size: 9px; font-family: monospace;")
        milestone_group = QGroupBox("Turn OCR milestones（診断専用・生データなし）")
        milestone_layout = QVBoxLayout(milestone_group)
        milestone_layout.addWidget(self.turn_ocr_milestone_log_label)
        self.diagnostics_drawer.add_widget(milestone_group)

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

        # -- terminal-flow drawer: export/recovery -----------------------------
        # MATCH END is composed beside the lifecycle bar below instead of
        # being hidden behind this remote drawer.
        self._detach_from_parent_layout(self.match_end_group)
        self.match_end_group.setVisible(False)
        self.terminal_flow_drawer = _CollapsibleSection("Export・復旧")
        for name in (
            "match_summary_group",
            "match_export_group",
            "match_recovery_group",
        ):
            group = getattr(self, name, None)
            if group is not None:
                self._detach_from_parent_layout(group)
                self.terminal_flow_drawer.add_widget(group)
        self._left_column_layout.addWidget(self.terminal_flow_drawer)

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
        self._compose_turn_advice_hierarchy()
        self._rich_gemini_layout.addRow(self.turn_advice_group)
        # Keep the status/gate labels alive for the existing controller-bound
        # display contract, but move them to an invisible owner. They are
        # audit/operator diagnostics, not live player advice.
        self.rich_gemini_status_audit = QWidget(self)
        self.rich_gemini_status_audit.setVisible(False)
        status_audit_layout = QVBoxLayout(self.rich_gemini_status_audit)
        status_audit_layout.setContentsMargins(0, 2, 0, 0)
        status_audit_layout.setSpacing(1)
        for field in (self.rich_gemini_status_label, self.rich_gemini_denial_label):
            old_label = self._rich_gemini_layout.labelForField(field)
            if old_label is not None:
                old_label.setVisible(False)
            self._rich_gemini_layout.setRowVisible(field, False)
            status_audit_layout.addWidget(field)
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

            self.evidence_open_button = QPushButton("撮影画像を確認")
            self.evidence_open_button.clicked.connect(self._on_open_evidence_overlay)
            self.evidence_status_label = QLabel()
            self.evidence_status_label.setWordWrap(False)
            self.evidence_status_label.setObjectName("liveToolStatus")

            # These are current-turn tools, not phase-form fields. Keep them
            # immediately below LIVE in every phase; render_view only changes
            # whether state mutation is currently allowed.
            self._detach_from_parent_layout(self.review_state_event_button)
            self._detach_from_parent_layout(self.result_state_event_button)
            self.result_state_event_button.setVisible(False)
            self.live_tools_bar = QWidget()
            self.live_tools_bar.setObjectName("liveToolsBar")
            live_tools_layout = QHBoxLayout(self.live_tools_bar)
            live_tools_layout.setContentsMargins(6, 3, 6, 3)
            live_tools_layout.setSpacing(6)
            live_tools_layout.addWidget(self.evidence_open_button)
            live_tools_layout.addWidget(self.review_state_event_button)
            live_tools_layout.addWidget(self.evidence_status_label, 1)
            self._center_column_layout.insertWidget(1, self.live_tools_bar)

            fixed_image_facts_row = QWidget()
            fixed_image_facts_layout = QHBoxLayout(fixed_image_facts_row)
            fixed_image_facts_layout.setContentsMargins(0, 0, 0, 0)
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
        self.action_result_step_stack = QStackedWidget()
        self.action_entry_step_page = QWidget()
        action_entry_step_layout = QVBoxLayout(self.action_entry_step_page)
        action_entry_step_layout.setContentsMargins(0, 0, 0, 0)
        self._detach_from_parent_layout(self.actual_action_group)
        action_entry_step_layout.addWidget(self.actual_action_group)
        action_entry_step_layout.addStretch(1)
        self.action_result_step_stack.addWidget(self.action_entry_step_page)
        action_page_layout.addWidget(self.action_result_step_stack)

        # Tournament P0 Result Entry: action input and result confirmation
        # are two genuinely separate operator pages.  The generic delta
        # editors remain alive as the canonical SideDelta projection target,
        # but are no longer visible in the normal result workflow.
        self._result_entry_active = False
        self._result_event_sequence = 0
        self._result_events: list[_ResultEventDraft] = []
        self._result_candidate_decisions: dict[str, str] = {}
        self._result_candidate_cards: dict[str, _MoveResultCandidateCard] = {}
        self._result_candidates: tuple[_MoveResultCandidate, ...] = ()
        self._result_hidden_holder = QWidget(self)
        self._result_hidden_holder.setVisible(False)
        hidden_result_layout = QVBoxLayout(self._result_hidden_holder)
        self._detach_from_parent_layout(self.action_result_delta_group)
        hidden_result_layout.addWidget(self.action_result_delta_group)
        self.self_delta_editor.setVisible(False)
        self.opponent_delta_editor.setVisible(False)
        self.weather_delta_field.setVisible(False)
        self.terrain_delta_field.setVisible(False)

        self.result_workbench_page = QWidget()
        result_page_layout = QVBoxLayout(self.result_workbench_page)
        result_page_layout.setContentsMargins(6, 4, 6, 4)
        result_page_layout.setSpacing(4)
        result_title = QLabel("結果記録")
        result_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        result_page_layout.addWidget(result_title)
        self.result_actions_label = QLabel()
        self.result_actions_label.setWordWrap(True)
        result_page_layout.addWidget(self.result_actions_label)

        candidates_group = QGroupBox("起こり得る結果")
        self.result_candidates_layout = QVBoxLayout(candidates_group)
        self.result_candidates_layout.setContentsMargins(4, 4, 4, 4)
        self.result_candidates_layout.setSpacing(2)
        result_page_layout.addWidget(candidates_group)

        faint_group = QGroupBox("ひんし")
        faint_layout = QHBoxLayout(faint_group)
        self._detach_from_parent_layout(self.record_self_faint_button)
        self._detach_from_parent_layout(self.record_opponent_faint_button)
        self.record_self_faint_button.setToolTip("Turn開始時の自分Activeをひんしとして記録")
        self.record_opponent_faint_button.setToolTip("Turn開始時の相手Activeをひんしとして記録")
        faint_layout.addWidget(self.record_self_faint_button)
        faint_layout.addWidget(self.record_opponent_faint_button)
        faint_layout.addStretch(1)
        result_page_layout.addWidget(faint_group)

        mega_group = QGroupBox("メガ進化")
        mega_group.setObjectName("megaEvolutionResultGroup")
        mega_layout = QHBoxLayout(mega_group)
        mega_layout.setContentsMargins(4, 3, 4, 3)
        mega_layout.setSpacing(4)
        self.self_mega_button = QPushButton("自分メガ進化")
        self.self_mega_button.setObjectName("selfMegaEvolutionButton")
        self.self_mega_button.setCheckable(True)
        self.self_mega_button.setToolTip(
            "このTurnの自分Activeが実際にメガ進化した事実を記録します。"
        )
        self.self_mega_button.clicked.connect(
            lambda _checked=False: self._toggle_mega_event(MegaSide.SELF)
        )
        self.opponent_mega_button = QPushButton("相手メガ進化")
        self.opponent_mega_button.setObjectName("opponentMegaEvolutionButton")
        self.opponent_mega_button.setCheckable(True)
        self.opponent_mega_button.setToolTip(
            "このTurnの相手Activeが実際にメガ進化した事実を記録します。"
        )
        self.opponent_mega_button.clicked.connect(
            lambda _checked=False: self._toggle_mega_event(MegaSide.OPPONENT)
        )
        mega_layout.addWidget(self.self_mega_button)
        mega_layout.addWidget(self.opponent_mega_button)
        mega_layout.addStretch(1)
        self.mega_result_group = mega_group
        result_page_layout.addWidget(mega_group)

        self.manual_result_button = QPushButton("＋手入力で結果を追加")
        self.manual_result_button.clicked.connect(self._open_manual_result_dialog)
        result_page_layout.addWidget(self.manual_result_button)

        summary_group = QGroupBox("このTurnの結果")
        self.result_summary_layout = QVBoxLayout(summary_group)
        self.result_summary_layout.setContentsMargins(4, 4, 4, 4)
        self.result_summary_empty_label = QLabel("追加イベントなし")
        self.result_summary_layout.addWidget(self.result_summary_empty_label)
        result_page_layout.addWidget(summary_group)

        result_navigation = QHBoxLayout()
        self.back_to_action_button = QPushButton("← 行動入力に戻る")
        self.back_to_action_button.clicked.connect(self._on_back_to_action_entry)
        result_navigation.addWidget(self.back_to_action_button)
        result_navigation.addStretch(1)
        result_page_layout.addLayout(result_navigation)
        self.action_result_step_stack.addWidget(self.result_workbench_page)

        # Base construction connected these shared buttons to the old
        # persist/advance handlers.  Rebind only their genuine UI clicks:
        # result navigation performs no write; the Result-page NEXT TURN
        # click performs the existing atomic action+delta write and then one
        # existing atomic turn advance.  Direct handler calls remain useful
        # to legacy focused tests and internal recovery tools.
        self.record_action_button.clicked.disconnect()
        self.record_action_button.clicked.connect(self._on_open_result_entry)
        self.next_turn_button.clicked.disconnect()
        self.next_turn_button.clicked.connect(self._on_result_next_turn)

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
        bottom_bar_layout.addWidget(self.new_match_button)
        bottom_bar_layout.addWidget(self.new_match_after_export_button)
        bottom_bar_layout.addWidget(self.start_turn_button)
        bottom_bar_layout.addWidget(self.confirm_turn_facts_button)
        bottom_bar_layout.addWidget(self.record_action_button)
        bottom_bar_layout.addWidget(self.next_turn_button)
        self.field_new_match_buttons = (
            self.new_match_button,
            self.new_match_after_export_button,
        )
        self.lifecycle_buttons = (
            self.start_turn_button,
            self.confirm_turn_facts_button,
            self.record_action_button,
            self.next_turn_button,
        )
        for lifecycle_button in (*self.field_new_match_buttons, *self.lifecycle_buttons):
            lifecycle_button.setProperty("lifecycle", True)
            lifecycle_button.setMinimumHeight(40)

        page = QWidget()
        self.battle_record_page = page
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(2, 2, 2, 2)
        page_layout.setSpacing(3)
        page_layout.addWidget(header_widget)
        page_layout.addLayout(body_row, 1)
        self._build_local_match_end_area(page_layout)
        page_layout.addWidget(bottom_bar)
        body_row.setSpacing(4)
        bottom_bar_layout.setContentsMargins(0, 0, 0, 0)
        bottom_bar_layout.setSpacing(4)
        header_layout.setSpacing(2)
        # The completed HTML uses readable, restrained cards. Since the
        # center now renders only one lifecycle surface at a time, the old
        # dense 9px legacy-graft styling is no longer necessary.
        page.setStyleSheet(
            "QWidget { background: #0b1220; color: #dbeafe; }"
            "QScrollArea { border: none; background: #0b1220; }"
            "QGroupBox { background: #111827; margin-top: 7px; padding: 5px; "
            "font-size: 11px; border: 1px solid #334155; border-radius: 6px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
            "QLabel { background: transparent; color: #dbeafe; font-size: 11px; }"
            "QPushButton { background: #1e293b; color: #e2e8f0; border: 1px solid #475569; "
            "border-radius: 5px; padding: 4px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #334155; border-color: #60a5fa; }"
            "QPushButton:disabled { background: #111827; color: #64748b; border-color: #243244; }"
            "QPushButton[lifecycle=\"true\"] { font-size: 13px; font-weight: 700; "
            "min-height: 30px; }"
            "QPushButton[lifecycle=\"true\"][active=\"true\"] { background: #22c55e; "
            "color: #03140a; border-color: #4ade80; }"
            "QGroupBox#matchEndLocalGroup { border-color: #9a6334; background: #17130f; }"
            "QPushButton[matchOutcome=\"true\"]:checked { background: #2563eb; color: white; "
            "border-color: #60a5fa; font-weight: 800; }"
            "QPushButton[destructive=\"true\"] { background: #7f1d1d; color: #fee2e2; "
            "border-color: #b45353; font-weight: 800; }"
            "QPushButton[destructive=\"true\"]:disabled { background: #241719; "
            "color: #765b60; border-color: #493238; }"
            "QWidget#liveToolsBar { background: #111827; border: 1px solid #334155; "
            "border-radius: 6px; }"
            "QLabel#liveToolStatus { color: #94a3b8; }"
            "QComboBox, QLineEdit, QSpinBox { background: #0f172a; color: #f8fafc; "
            "border: 1px solid #475569; border-radius: 4px; }"
            "QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled { color: #64748b; "
            "border-color: #243244; }"
            "QCheckBox { background: transparent; color: #dbeafe; font-size: 11px; "
            "padding: 2px 4px; }"
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

        self._apply_html_parity_composition(
            page=page,
            page_layout=page_layout,
            header_widget=header_widget,
            body_row=body_row,
            bottom_bar=bottom_bar,
            bottom_bar_layout=bottom_bar_layout,
            left_container=left_container,
            center_container=center_container,
            right_container=right_container,
        )

        self.header_tabs.removeTab(_BATTLE_RECORD_TAB_INDEX)
        self.header_tabs.insertTab(_BATTLE_RECORD_TAB_INDEX, page, "バトルレコード")

    def _build_local_match_end_area(self, page_layout: QVBoxLayout) -> None:
        """Compose one local, controller-bound outcome -> MATCH END surface."""

        self.match_end_local_group = QGroupBox("MATCH END")
        self.match_end_local_group.setObjectName("matchEndLocalGroup")
        local_layout = QHBoxLayout(self.match_end_local_group)
        local_layout.setContentsMargins(10, 5, 10, 5)
        local_layout.setSpacing(8)

        self.match_outcome_button_group = QButtonGroup(self.match_end_local_group)
        self.match_outcome_button_group.setExclusive(True)
        self.match_win_button = QPushButton("WIN")
        self.match_loss_button = QPushButton("LOSS")
        for button, outcome in (
            (self.match_win_button, MatchOutcome.WIN.value),
            (self.match_loss_button, MatchOutcome.LOSE.value),
        ):
            button.setCheckable(True)
            button.setProperty("matchOutcome", True)
            self.match_outcome_button_group.addButton(button)
            button.clicked.connect(
                lambda _checked=False, value=outcome: self.outcome_box.setCurrentText(value)
            )
            local_layout.addWidget(button)

        # outcome_box remains the established result state. The visible
        # buttons are projections of, and write directly to, that value.
        self.outcome_box.setVisible(False)
        self.outcome_box.currentTextChanged.connect(self._sync_match_outcome_buttons)
        self._detach_from_parent_layout(self.outcome_confirm_checkbox)
        self.outcome_confirm_checkbox.setText("選択した結果で試合を終了することを確認")
        local_layout.addWidget(self.outcome_confirm_checkbox, 1)
        self._detach_from_parent_layout(self.end_match_button)
        self.end_match_button.setText("試合終了")
        self.end_match_button.setProperty("destructive", True)
        local_layout.addWidget(self.end_match_button)
        page_layout.addWidget(self.match_end_local_group)
        self._sync_match_outcome_buttons(self.outcome_box.currentText())

    def _sync_match_outcome_buttons(self, outcome: str) -> None:
        if not hasattr(self, "match_win_button"):
            return
        self.match_win_button.setChecked(outcome == MatchOutcome.WIN.value)
        self.match_loss_button.setChecked(outcome == MatchOutcome.LOSE.value)

    def _apply_html_parity_composition(
        self,
        *,
        page: QWidget,
        page_layout: QVBoxLayout,
        header_widget: QWidget,
        body_row: QHBoxLayout,
        bottom_bar: QWidget,
        bottom_bar_layout: QHBoxLayout,
        left_container: QWidget,
        center_container: QWidget,
        right_container: QWidget,
    ) -> None:
        """Recompose the operator surface against the binding v5 HTML.

        Existing controller-bound inputs remain the source of truth, but
        legacy forms are moved into a hidden holder. The visible tree below
        is built from the HTML's cards, chips, tabs, and fixed stage rows.
        """

        # 58px persistent application header: brand / two tabs / utilities.
        page_layout.removeWidget(header_widget)
        header_widget.setVisible(False)
        central_widget = self.centralWidget()
        assert central_widget is not None
        outer_layout = central_widget.layout()
        if outer_layout is not None:
            outer_layout.setContentsMargins(0, 0, 0, 0)
            outer_layout.setSpacing(0)
        tab_bar = self.header_tabs.tabBar()
        tab_bar.setFixedHeight(58)
        tab_bar.hide()
        self.header_tabs.setDocumentMode(True)
        brand_corner = QWidget(self.header_tabs)
        brand_corner.setFixedSize(290, 58)
        brand_corner.move(0, 0)
        brand_layout = QHBoxLayout(brand_corner)
        brand_layout.setContentsMargins(18, 0, 12, 0)
        brand = QLabel("MAPLE")
        brand.setObjectName("mapleBrand")
        brand_layout.addWidget(brand)
        selection_tab = QPushButton("選出")
        battle_tab = QPushButton("バトルレコード")
        for button in (selection_tab, battle_tab):
            button.setCheckable(True)
            brand_layout.addWidget(button)
        brand_layout.addStretch(1)
        selection_tab.clicked.connect(lambda: self.header_tabs.setCurrentIndex(0))
        battle_tab.clicked.connect(lambda: self.header_tabs.setCurrentIndex(1))

        def sync_header_tabs(index: int) -> None:
            selection_tab.setChecked(index == 0)
            battle_tab.setChecked(index == 1)

        self.header_tabs.currentChanged.connect(sync_header_tabs)
        sync_header_tabs(self.header_tabs.currentIndex())
        brand_corner.show()
        brand_corner.raise_()
        self.parity_brand_corner = brand_corner

        utility_corner = QWidget(self.header_tabs)
        utility_corner.setFixedSize(407, 58)
        utility_corner.move(1513, 0)
        utility_layout = QHBoxLayout(utility_corner)
        utility_layout.setContentsMargins(8, 0, 18, 0)
        utility_layout.setSpacing(8)
        self.battle_context_label.setText("Match 未取得   Turn —")
        self.header_phase_badge = QPushButton("撮影待ち")
        self.header_phase_badge.setEnabled(False)
        export_button = QPushButton("Export・復旧")
        more_button = QPushButton("…")

        def open_terminal_flow() -> None:
            self.terminal_flow_drawer.setVisible(True)
            self.terminal_flow_drawer.toggle_button.setChecked(True)

        export_button.clicked.connect(open_terminal_flow)
        more_button.clicked.connect(
            lambda: self.diagnostics_drawer.toggle_button.setChecked(True)
        )
        utility_layout.addWidget(self.battle_context_label)
        utility_layout.addWidget(self.header_phase_badge)
        utility_layout.addWidget(export_button)
        utility_layout.addWidget(more_button)
        utility_corner.show()
        utility_corner.raise_()
        self.parity_utility_corner = utility_corner
        self.header_export_button = export_button

        def sync_contextual_header(index: int) -> None:
            # Selection has no Turn/Battle utility context.  The same widget is
            # restored unchanged for Battle Record, preserving its accepted
            # pixels and controls.
            utility_corner.setVisible(index == _BATTLE_RECORD_TAB_INDEX)

        self.header_tabs.currentChanged.connect(sync_contextual_header)
        sync_contextual_header(self.header_tabs.currentIndex())

        self.header_tabs.setStyleSheet(
            "QTabWidget { background: #081421; }"
            "QTabWidget::pane { border: 0; background: #07101a; }"
            "QTabBar { background: #081421; }"
            "QTabBar::tab { background: #0b1724; color: #edf5fb; border: 1px solid #314b64; "
            "border-radius: 3px; min-width: 78px; padding: 8px 12px; margin: 14px 2px; }"
            "QTabBar::tab:selected { background: #1a3c5d; font-weight: 800; }"
            "QWidget { color: #edf5fb; }"
            "QLabel#mapleBrand { font-size: 16px; font-weight: 900; letter-spacing: 2px; }"
            "QPushButton { background: #0b1724; color: #edf5fb; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 7px 10px; }"
            "QPushButton:disabled { color: #91a8bb; }"
        )

        # Binding stage geometry: body with no gutters and a 66px footer.
        page_layout.setContentsMargins(0, 58, 0, 0)
        page_layout.setSpacing(0)
        body_row.setSpacing(0)
        bottom_bar.setFixedHeight(66)
        self.parity_bottom_bar = bottom_bar
        self.parity_body_row = body_row
        bottom_bar_layout.setContentsMargins(15, 14, 15, 14)
        bottom_bar_layout.setSpacing(8)
        for layout in (
            self._left_column_layout,
            self._right_column_layout,
        ):
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(10)
        self.history_group.setTitle("CONFIRMED HISTORY")
        self._center_column_layout.setContentsMargins(12, 12, 12, 12)
        self._center_column_layout.setSpacing(10)

        # LIVE shell and its exact toolbar ordering.
        self.capture_status_group.setTitle("UGREEN LIVE")
        self.capture_status_group.setObjectName("liveShell")
        self.reconnect_capture_button.setVisible(False)
        self.capture_status_label.setVisible(False)
        old_tools = self.live_tools_bar
        self._detach_from_parent_layout(old_tools)
        old_tools.setVisible(False)
        self.live_tools_bar = QWidget()
        self.live_tools_bar.setObjectName("liveToolsBar")
        tools = QHBoxLayout(self.live_tools_bar)
        tools.setContentsMargins(8, 4, 8, 4)
        tools.setSpacing(6)
        self.live_current_state_label = QLabel("自分: —  HP—    相手: —  HP—")
        self.live_current_state_label.setProperty("muted", True)
        tools.addWidget(self.live_current_state_label, 1)
        self.turn_ocr_status_indicator_label = QLabel("OCR待機")
        self.turn_ocr_status_indicator_label.setProperty("statusChip", True)
        tools.addWidget(self.turn_ocr_status_indicator_label)
        self.evidence_open_button.setText("撮影画像を確認")
        self.review_state_event_button.setText("＋ 状態変化を記録")
        self.review_state_event_button.setProperty("strong", True)
        tools.addWidget(self.evidence_open_button)
        tools.addWidget(self.review_state_event_button)
        self._center_column_layout.insertWidget(1, self.live_tools_bar)

        # Keep domain-bound legacy groups alive but outside the visible tree.
        self._legacy_parity_holder = QWidget(self)
        self._legacy_parity_holder.setVisible(False)
        legacy_layout = QVBoxLayout(self._legacy_parity_holder)
        for legacy in (
            self.turn_facts_group,
            self.current_state_group,
            self.actual_action_group,
            self.action_result_delta_group,
        ):
            self._detach_from_parent_layout(legacy)
            legacy_layout.addWidget(legacy)

        for old_page in (
            self.capture_workbench_page,
            self.review_workbench_page,
            self.action_workbench_page,
            self.recorded_workbench_page,
        ):
            self.workbench_stack.removeWidget(old_page)
            old_page.setParent(self._legacy_parity_holder)
        self.workbench_stack.setMinimumHeight(350)
        self.workbench_stack.setMaximumHeight(350)
        self.workbench_stack.setObjectName("htmlParityWorkbench")

        self.capture_workbench_page = self._build_parity_capture_page()
        self.review_workbench_page = self._build_parity_review_page()
        parity_action_entry_page = self._build_parity_action_page()
        self.action_entry_step_page = parity_action_entry_page
        self.action_result_step_stack = QStackedWidget()
        self.action_result_step_stack.addWidget(parity_action_entry_page)
        # Result Entry is the second step inside the one canonical Action
        # lifecycle workbench.  Keeping this nested preserves the accepted
        # four outer lifecycle pages while making the genuine parity UI (not
        # the hidden legacy composition) display the new result surface.
        self.action_result_step_stack.addWidget(self.result_workbench_page)
        action_step_wrapper = QWidget()
        action_step_layout = QVBoxLayout(action_step_wrapper)
        action_step_layout.setContentsMargins(0, 0, 0, 0)
        action_step_layout.addWidget(self.action_result_step_stack)
        self.action_workbench_page = action_step_wrapper
        self.recorded_workbench_page = self._build_parity_recorded_page()
        for workbench_page in (
            self.capture_workbench_page,
            self.review_workbench_page,
            self.action_workbench_page,
            self.recorded_workbench_page,
        ):
            self.workbench_stack.addWidget(workbench_page)

        # The right rail is content-driven above and flexible below.  Advice
        # retains its visual hierarchy without claiming blank vertical space;
        # INTEL receives the remaining useful rail height.
        self.rich_gemini_group.setObjectName("geminiPanel")
        self.rich_gemini_group.setTitle("GEMINI TURN ADVICE")
        self.rich_gemini_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self.gemini_empty_label = QLabel("Turn撮影後に確認へ")
        self.gemini_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.gemini_empty_label.setObjectName("geminiEmpty")
        self.gemini_empty_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.gemini_empty_label.setMaximumHeight(64)
        self._rich_gemini_layout.addRow(self.gemini_empty_label)
        self.opponent_intel_widget.setMinimumHeight(245)
        self.opponent_intel_widget.setMaximumHeight(16777215)
        self.opponent_intel_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._right_column_layout.setStretch(0, 0)
        self._right_column_layout.setStretch(1, 1)

        # HTML palette and component language. No bright green state.
        page.setStyleSheet(
            "QWidget { background: #07101a; color: #edf5fb; font-size: 10px; }"
            "QScrollArea { border: 0; background: #07101a; }"
            "QScrollBar:vertical { background: #091623; width: 10px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #91a8bb; min-height: 30px; "
            "border-radius: 4px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QGroupBox { background: #0b1724; border: 1px solid #20384e; border-radius: 4px; "
            "margin-top: 9px; padding: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; "
            "color: #91a8bb; font-size: 9px; }"
            "QLabel { background: transparent; color: #edf5fb; }"
            "QLabel[muted=\"true\"] { color: #91a8bb; font-size: 9px; }"
            "QLabel[badge=\"true\"] { color: #91a8bb; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 4px 6px; font-size: 9px; }"
            "QLabel[cardTitle=\"true\"] { font-weight: 800; }"
            "QLabel#intelSpecies, QLabel[dialogTitle=\"true\"] { font-size: 13px; "
            "font-weight: 800; }"
            "QLabel#intelFacts { background: #0e2032; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 8px; font-weight: 700; }"
            "QLabel#geminiEmpty { border: 1px dashed #314b64; color: #91a8bb; }"
            "QWidget#advicePrimaryCard { background: #123456; border: 2px solid #6ea8d8; "
            "border-radius: 5px; }"
            "QLabel#advicePrimaryAction { font-size: 24px; font-weight: 900; color: #ffffff; }"
            "QWidget#advicePredictionCard { background: #0e2032; border-left: 3px solid #6a89a5; }"
            "QLabel#advicePrediction { font-size: 13px; font-weight: 700; }"
            "QLabel#adviceReasons { font-size: 12px; line-height: 1.25; }"
            "QWidget#adviceWarningCard { background: #361b1f; border: 1px solid #d66a72; "
            "border-radius: 4px; }"
            "QLabel#adviceWarnings { color: #ffd7da; font-size: 12px; font-weight: 800; }"
            "QWidget[audit=\"true\"], QGroupBox[audit=\"true\"] { color: #91a8bb; "
            "font-size: 9px; }"
            "QWidget[parityCard=\"true\"], QWidget[miniCard=\"true\"] { background: #0b1724; "
            "border: 1px solid #20384e; border-radius: 4px; }"
            "QWidget[candidateArea=\"true\"] { background: #0d2235; border: 1px solid #4a6984; "
            "border-radius: 4px; }"
            "QPushButton { background: #0e2032; color: #edf5fb; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 6px 8px; }"
            "QPushButton:hover { background: #132c45; }"
            "QPushButton:disabled { color: #91a8bb; background: #091623; border-color: #20384e; }"
            "QPushButton:checked, QPushButton[selected=\"true\"], QPushButton[strong=\"true\"] { "
            "background: #1a3c5d; font-weight: 800; }"
            "QPushButton[faintControl=\"true\"] { min-width: 128px; min-height: 36px; "
            "padding: 7px 14px; font-size: 13px; font-weight: 800; "
            "background: #26384d; color: #f8fafc; border: 2px solid #64748b; }"
            "QPushButton[faintControl=\"true\"]:checked { background: #991b1b; "
            "color: #ffffff; border-color: #f87171; }"
            "QPushButton[faintControl=\"true\"]:disabled { background: #17212d; "
            "color: #718096; border-color: #334155; }"
            "QPushButton[lifecycle=\"true\"] { min-height: 28px; font-size: 11px; "
            "font-weight: 800; }"
            "QPushButton[lifecycle=\"true\"][active=\"true\"] { background: #1a3c5d; "
            "color: #edf5fb; border-color: #314b64; }"
            "QComboBox, QLineEdit { background: #0e2032; color: #edf5fb; "
            "border: 1px solid #314b64; "
            "border-radius: 3px; padding: 5px 7px; min-height: 24px; }"
            "QWidget#liveToolsBar { background: #081522; border: 1px solid #314b64; "
            "border-radius: 0; }"
            "QGroupBox#liveShell { background: #040b12; border: 1px solid #314b64; }"
            "QGroupBox#geminiPanel { background: #081421; }"
            "QStackedWidget#htmlParityWorkbench { background: #091623; border: 1px solid #314b64; "
            "border-radius: 4px; }"
        )

        self._apply_selection_v3_composition()

    def _compose_turn_advice_hierarchy(self) -> None:
        """Compose only the information needed during live play.

        The labels for robustness, alternatives, and provenance remain
        controller-bound objects so the existing read path and persistence
        contract stay intact, but they are owned by an invisible holder and
        are deliberately absent from the normal Battle Record composition.
        """

        form = self.turn_advice_group.layout()
        assert isinstance(form, QFormLayout)
        bound_labels = (
            self.turn_advice_action_label,
            self.turn_advice_robustness_label,
            self.turn_advice_prediction_label,
            self.turn_advice_alternatives_label,
            self.turn_advice_rationale_label,
            self.turn_advice_warnings_label,
            self.turn_advice_source_label,
            self.turn_advice_model_label,
            self.turn_advice_binding_label,
            self.turn_advice_legality_label,
            self.turn_advice_schema_version_label,
        )
        player_labels = (
            self.turn_advice_action_label,
            self.turn_advice_prediction_label,
            self.turn_advice_rationale_label,
            self.turn_advice_warnings_label,
        )
        hidden_labels = tuple(field for field in bound_labels if field not in player_labels)
        for field in bound_labels:
            row_label = form.labelForField(field)
            if row_label is not None:
                row_label.setVisible(False)
        for field in hidden_labels:
            form.setRowVisible(field, False)

        hidden_holder = QWidget(self)
        hidden_holder.setVisible(False)
        hidden_layout = QVBoxLayout(hidden_holder)
        hidden_layout.setContentsMargins(0, 0, 0, 0)
        for field in hidden_labels:
            hidden_layout.addWidget(field)
        self._turn_advice_hidden_labels = hidden_holder

        self.turn_advice_primary_card = QWidget()
        self.turn_advice_primary_card.setObjectName("advicePrimaryCard")
        self.turn_advice_primary_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        primary_layout = QVBoxLayout(self.turn_advice_primary_card)
        primary_layout.setContentsMargins(14, 12, 14, 12)
        primary_layout.setSpacing(4)
        primary_heading = QLabel("推奨行動")
        primary_heading.setProperty("muted", True)
        self.turn_advice_action_label.setObjectName("advicePrimaryAction")
        self.turn_advice_action_label.setWordWrap(True)
        self.turn_advice_action_label.setMinimumHeight(36)
        self.turn_advice_action_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        primary_layout.addWidget(primary_heading)
        primary_layout.addWidget(self.turn_advice_action_label)

        prediction_card = QWidget()
        prediction_card.setObjectName("advicePredictionCard")
        prediction_layout = QVBoxLayout(prediction_card)
        prediction_layout.setContentsMargins(10, 7, 10, 7)
        prediction_layout.setSpacing(2)
        prediction_heading = QLabel("相手予測")
        prediction_heading.setProperty("muted", True)
        self.turn_advice_prediction_label.setObjectName("advicePrediction")
        prediction_layout.addWidget(prediction_heading)
        prediction_layout.addWidget(self.turn_advice_prediction_label)
        prediction_card.setVisible(False)
        self.turn_advice_prediction_card = prediction_card

        reasons_card = QWidget()
        reasons_layout = QVBoxLayout(reasons_card)
        reasons_layout.setContentsMargins(4, 6, 4, 4)
        reasons_layout.setSpacing(3)
        reasons_heading = QLabel("判断理由")
        reasons_heading.setProperty("cardTitle", True)
        self.turn_advice_rationale_label.setObjectName("adviceReasons")
        reasons_layout.addWidget(reasons_heading)
        reasons_layout.addWidget(self.turn_advice_rationale_label)

        self.turn_advice_warning_card = QWidget()
        self.turn_advice_warning_card.setObjectName("adviceWarningCard")
        warning_layout = QVBoxLayout(self.turn_advice_warning_card)
        warning_layout.setContentsMargins(10, 8, 10, 8)
        warning_layout.setSpacing(3)
        warning_heading = QLabel("警告")
        warning_heading.setProperty("cardTitle", True)
        self.turn_advice_warnings_label.setObjectName("adviceWarnings")
        warning_layout.addWidget(warning_heading)
        warning_layout.addWidget(self.turn_advice_warnings_label)
        self.turn_advice_warning_card.setVisible(False)

        self.turn_advice_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        form.addRow(self.turn_advice_primary_card)
        form.addRow(reasons_card)
        form.addRow(prediction_card)
        form.addRow(self.turn_advice_warning_card)

    def _apply_selection_v3_composition(self) -> None:
        """Build the accepted Selection UX from the existing bound controls."""

        selection_scroll = self.header_tabs.widget(0)
        if not isinstance(selection_scroll, QScrollArea):
            raise RuntimeError("Selection tab must be a QScrollArea")
        selection_page = selection_scroll.widget()
        if selection_page is None:
            raise RuntimeError("Selection page is missing")
        selection_scroll.setObjectName("selectionV3Scroll")
        selection_scroll.setWidgetResizable(True)
        selection_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        selection_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        selection_page.setObjectName("selectionV3Page")

        # Compatibility-only Selection controls remain alive in this hidden
        # owner. NEW MATCH is intentionally not reparented here: its two
        # canonical state-specific buttons live only in the Battle Record
        # lifecycle bar.
        self._selection_v3_legacy_holder = QWidget(self)
        self._selection_v3_legacy_holder.setVisible(False)
        legacy_layout = QVBoxLayout(self._selection_v3_legacy_holder)

        while self._selection_layout.count():
            self._selection_layout.takeAt(0)
        self._selection_layout.setContentsMargins(0, 58, 0, 0)
        self._selection_layout.setSpacing(0)

        body = QWidget(selection_page)
        body.setObjectName("selectionV3Body")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        left = QWidget(body)
        left.setObjectName("selectionV3Left")
        left_layout = QVBoxLayout(left)
        center = QWidget(body)
        center.setObjectName("selectionV3Center")
        center_layout = QVBoxLayout(center)
        right = QWidget(body)
        right.setObjectName("selectionV3Right")
        right_layout = QVBoxLayout(right)
        for layout in (left_layout, center_layout, right_layout):
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)
        self.selection_v3_left = left
        self.selection_v3_center = center
        self.selection_v3_right = right

        self._build_selection_v3_left(left_layout)
        self._build_selection_v3_center(center_layout)
        self._build_selection_v3_right(right_layout)
        body_layout.addWidget(left, 24)
        body_layout.addWidget(center, 44)
        body_layout.addWidget(right, 32)
        self._selection_layout.addWidget(body, 1)

        # Old group shells and MOCK surface stay alive only as hidden owners of
        # compatibility labels not used by the v3 workbench.
        for legacy in (
            self.self_team_group,
            self.self_team_presets_group,
            self.opponent_facts_group,
            self.selection_roi_group,
            self.mock_group,
            self.gemini_group,
            self.advice_group,
            self.actual_group,
        ):
            legacy_layout.addWidget(legacy)
        selection_page.setStyleSheet(self._selection_v3_style())
        self._selection_v3_ready = True

    @staticmethod
    def _selection_v3_heading(title: str, subtitle: str) -> QWidget:
        heading = QWidget()
        heading.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(heading)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setProperty("sectionTitle", True)
        subtitle_label = QLabel(subtitle)
        subtitle_label.setProperty("muted", True)
        subtitle_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return heading

    @staticmethod
    def _selection_v3_card() -> tuple[QWidget, QVBoxLayout]:
        card = QWidget()
        card.setProperty("selectionCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        return card, layout

    def _build_selection_v3_left(self, layout: QVBoxLayout) -> None:
        layout.addWidget(
            self._selection_v3_heading(
                "自分のPT登録・確認", "現在の構築を確認し、必要なときだけ編集します。"
            )
        )
        build_bar, build_bar_layout = self._selection_v3_card()
        build_row = QHBoxLayout()
        build_title = QLabel("選択中の構築")
        build_title.setProperty("muted", True)
        self.selection_v3_build_name = QLabel("未選択")
        self.selection_v3_build_name.setProperty("cardTitle", True)
        build_row.addWidget(build_title)
        build_row.addWidget(self.selection_v3_build_name, 1)
        build_bar_layout.addLayout(build_row)
        layout.addWidget(build_bar)

        self.selection_v3_team_name_labels: list[QLabel] = []
        self.selection_v3_team_detail_labels: list[QLabel] = []
        team_grid = QGridLayout()
        team_grid.setSpacing(6)
        for index in range(6):
            card, card_layout = self._selection_v3_card()
            name = QLabel(f"Slot {index + 1}")
            name.setProperty("cardTitle", True)
            detail = QLabel("詳細情報なし")
            detail.setProperty("muted", True)
            detail.setWordWrap(True)
            card_layout.addWidget(name)
            card_layout.addWidget(detail)
            team_grid.addWidget(card, index // 2, index % 2)
            self.selection_v3_team_name_labels.append(name)
            self.selection_v3_team_detail_labels.append(detail)
        layout.addLayout(team_grid)

        actions = QHBoxLayout()
        self.selection_v3_change_button = QPushButton("変更")
        self.selection_v3_change_button.setCheckable(True)
        self.selection_v3_change_button.toggled.connect(self._toggle_selection_v3_team_editor)
        self.edit_self_team_build_button.setText("詳細編集")
        # Reparented from window.py's preset group; the v3 workbench frames the
        # action as adopting a saved build into the current self-team six.
        self.use_self_team_preset_button.setText("この構築を採用")
        self.selection_v3_manage_button = QPushButton("構築管理…")
        self.selection_v3_manage_button.setCheckable(True)
        self.selection_v3_manage_button.toggled.connect(self._toggle_selection_v3_management)
        for button in (
            self.selection_v3_change_button,
            self.edit_self_team_build_button,
            self.selection_v3_manage_button,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)

        # BUILD SWITCH SAFETY (Tournament P0): once selection facts are
        # confirmed the base render (window.py) locks every build-management
        # control to protect the match-bound self-team snapshot. That lock was
        # a silent dead-end -- this notice plus one explicit lifecycle action
        # give the operator a visible reason and a safe way forward without
        # ever mutating the active match's bound team in place.
        self.selection_v3_build_lock_notice = QLabel("対戦中のため構築を変更できません")
        self.selection_v3_build_lock_notice.setWordWrap(True)
        self.selection_v3_build_lock_notice.setProperty("diagnostic", True)
        self.selection_v3_build_lock_notice.setVisible(False)
        layout.addWidget(self.selection_v3_build_lock_notice)
        self.selection_v3_discard_match_button = QPushButton(
            "この試合を破棄して構築を変更"
        )
        self.selection_v3_discard_match_button.clicked.connect(
            self._on_selection_v3_discard_match_for_build_change
        )
        self.selection_v3_discard_match_button.setVisible(False)
        layout.addWidget(self.selection_v3_discard_match_button)

        self.selection_v3_team_editor = QWidget()
        editor_grid = QGridLayout(self.selection_v3_team_editor)
        editor_grid.setContentsMargins(0, 0, 0, 0)
        editor_grid.setSpacing(5)
        for index, field in enumerate(self.self_team_inputs):
            field.setPlaceholderText(f"Slot {index + 1}")
            editor_grid.addWidget(field, index // 2, index % 2)
        self.selection_v3_team_editor.setVisible(False)
        layout.addWidget(self.selection_v3_team_editor)

        self.selection_v3_management = QWidget()
        management_layout = QVBoxLayout(self.selection_v3_management)
        management_layout.setContentsMargins(0, 0, 0, 0)
        management_layout.setSpacing(5)
        management_layout.addWidget(self.self_team_preset_box)
        management_layout.addWidget(self.self_team_preset_name)
        management_grid = QGridLayout()
        management_buttons = (
            self.save_self_team_preset_button,
            self.use_self_team_preset_button,
            self.update_self_team_preset_button,
            self.delete_self_team_preset_button,
            self.import_self_team_button,
            self.export_self_team_button,
        )
        for index, button in enumerate(management_buttons):
            management_grid.addWidget(button, index // 2, index % 2)
        management_layout.addLayout(management_grid)
        self.selection_v3_management.setVisible(False)
        layout.addWidget(self.selection_v3_management)
        layout.addStretch(1)

    def _build_selection_v3_center(self, layout: QVBoxLayout) -> None:
        layout.addWidget(
            self._selection_v3_heading(
                "相手6体 OCR / ROI確認・修正",
                "画像・OCR候補・人の確定値を分けて確認します。",
            )
        )
        status_row = QHBoxLayout()
        self.selection_v3_confirmed_status = QLabel("確認済み 0 / 6")
        self.selection_v3_confirmed_status.setProperty("statusChip", True)
        self.selection_v3_ocr_detail_button = QPushButton("OCR詳細")
        self.selection_v3_ocr_detail_button.setCheckable(True)
        self.selection_v3_ocr_detail_button.toggled.connect(
            self.selection_roi_status_label.setVisible
        )
        status_row.addWidget(self.selection_v3_confirmed_status)
        status_row.addStretch(1)
        status_row.addWidget(self.selection_v3_ocr_detail_button)
        layout.addLayout(status_row)
        self.selection_roi_status_label.setVisible(False)
        self.selection_roi_status_label.setProperty("diagnostic", True)
        layout.addWidget(self.selection_roi_status_label)

        self.selection_v3_slot_badges: list[QLabel] = []
        self.selection_v3_manual_buttons: list[QPushButton] = []
        self.selection_v3_opponent_cards: list[QWidget] = []
        grid = QGridLayout()
        grid.setSpacing(7)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        for slot in range(1, 7):
            card, card_layout = self._selection_v3_card()
            card.setObjectName(f"selectionOpponentCard{slot}")
            card.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            header_widget = QWidget()
            header_widget.setMaximumHeight(26)
            header = QHBoxLayout(header_widget)
            header.setContentsMargins(0, 0, 0, 0)
            title = QLabel(f"SLOT {slot}")
            title.setProperty("cardTitle", True)
            badge = QLabel("未確認")
            badge.setProperty("statusChip", True)
            badge.setMaximumHeight(24)
            header.addWidget(title)
            header.addStretch(1)
            header.addWidget(badge)
            card_layout.addWidget(header_widget)

            thumbnail = self._selection_roi_thumbnail_labels[slot]
            thumbnail.setMinimumSize(150, 84)
            thumbnail.setMaximumHeight(84)
            thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumbnail.setProperty("roiCrop", True)
            card_layout.addWidget(thumbnail)
            candidate = self._selection_roi_candidate_labels[slot]
            candidate.setProperty("candidate", True)
            card_layout.addWidget(candidate)
            origin = self._selection_roi_origin_labels[slot]
            origin.setProperty("muted", True)
            card_layout.addWidget(origin)
            candidate_row = QHBoxLayout()
            for button in self._selection_roi_candidate_buttons[slot]:
                candidate_row.addWidget(button)
            candidate_row.addStretch(1)
            card_layout.addLayout(candidate_row)

            manual_row = QHBoxLayout()
            field = self.opponent_team_inputs[slot - 1]
            field.setPlaceholderText("ポケモン名を手入力")
            manual = QPushButton("手入力")
            manual.clicked.connect(
                lambda _checked=False, selected_slot=slot: (
                    self._activate_selection_v3_manual_input(selected_slot)
                )
            )
            manual_row.addWidget(field, 1)
            manual_row.addWidget(manual)
            card_layout.addLayout(manual_row)
            grid.addWidget(card, (slot - 1) // 3, (slot - 1) % 3)
            self.selection_v3_opponent_cards.append(card)
            self.selection_v3_slot_badges.append(badge)
            self.selection_v3_manual_buttons.append(manual)
        layout.addLayout(grid)

        send_row = QHBoxLayout()
        send_row.addStretch(1)
        self.selection_roi_send_button.setText("この6体でGeminiに送る")
        self.selection_roi_send_button.setMaximumWidth(300)
        send_row.addWidget(self.selection_roi_send_button)
        layout.addLayout(send_row)
        layout.addStretch(1)

    def _build_selection_v3_right(self, layout: QVBoxLayout) -> None:
        layout.addWidget(
            self._selection_v3_heading(
                "Gemini推薦", "現在の対戦だけに束縛された有効な推薦を表示します。"
            )
        )
        advice_card, advice_layout = self._selection_v3_card()
        self.selection_v3_advice_waiting = QLabel("6体確認後、送信を待っています。")
        self.selection_v3_advice_waiting.setProperty("muted", True)
        advice_layout.addWidget(self.selection_v3_advice_waiting)
        advice_grid = QGridLayout()
        advice_grid.addWidget(QLabel("選出プラン"), 0, 0)
        self.selection_v3_advice_package = QLabel("—")
        self.selection_v3_advice_package.setProperty("cardTitle", True)
        advice_grid.addWidget(self.selection_v3_advice_package, 0, 1)
        self.selection_v3_advice_pick_labels: list[QLabel] = []
        for index in range(3):
            number = QLabel(str(index + 1))
            number.setProperty("orderBadge", True)
            value = QLabel("—")
            value.setProperty("cardTitle", True)
            advice_grid.addWidget(number, index + 1, 0)
            advice_grid.addWidget(value, index + 1, 1)
            self.selection_v3_advice_pick_labels.append(value)
        advice_grid.addWidget(QLabel("推奨先発"), 4, 0)
        self.selection_v3_advice_lead = QLabel("—")
        advice_grid.addWidget(self.selection_v3_advice_lead, 4, 1)
        advice_grid.addWidget(QLabel("Mega予定"), 5, 0)
        self.selection_v3_advice_intended_mega = QLabel("—")
        advice_grid.addWidget(self.selection_v3_advice_intended_mega, 5, 1)
        advice_grid.addWidget(QLabel("選出理由"), 6, 0)
        self.selection_v3_advice_reason = QLabel("—")
        self.selection_v3_advice_reason.setWordWrap(True)
        self.selection_v3_advice_reason.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        advice_grid.addWidget(self.selection_v3_advice_reason, 6, 1)
        advice_grid.addWidget(QLabel("提供元 / モデル"), 7, 0)
        self.selection_v3_advice_provider_model = QLabel("—")
        self.selection_v3_advice_provider_model.setWordWrap(True)
        advice_grid.addWidget(self.selection_v3_advice_provider_model, 7, 1)
        advice_grid.addWidget(QLabel("Source / validity"), 8, 0)
        self.selection_v3_advice_validity = QLabel("WAITING")
        self.selection_v3_advice_validity.setProperty("muted", True)
        advice_grid.addWidget(self.selection_v3_advice_validity, 8, 1)
        advice_layout.addLayout(advice_grid)
        layout.addWidget(advice_card)

        layout.addWidget(
            self._selection_v3_heading(
                "実際の選出", "カードを押して選出を変更。1 が先発です。"
            )
        )
        actual_card, actual_layout = self._selection_v3_card()
        self.selection_v3_actual_buttons: list[QPushButton] = []
        actual_grid = QGridLayout()
        actual_grid.setSpacing(6)
        for index in range(6):
            button = QPushButton(f"—  Slot {index + 1}")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, selected_index=index: (
                    self._toggle_selection_v3_actual(selected_index)
                )
            )
            actual_grid.addWidget(button, index // 2, index % 2)
            self.selection_v3_actual_buttons.append(button)
        actual_layout.addLayout(actual_grid)
        self.selection_v3_actual_status = QLabel("3体を選ぶと確定できます。")
        self.selection_v3_actual_status.setProperty("muted", True)
        actual_layout.addWidget(self.selection_v3_actual_status)
        self.apply_button.setText("この選出を確定")
        self.apply_button.setMaximumWidth(220)
        confirm_row = QHBoxLayout()
        confirm_row.addStretch(1)
        confirm_row.addWidget(self.apply_button)
        actual_layout.addLayout(confirm_row)
        layout.addWidget(actual_card)
        layout.addStretch(1)

        hidden = QWidget(self._selection_v3_legacy_holder)
        hidden_layout = QVBoxLayout(hidden)
        for checkbox in self.actual_checkboxes:
            hidden_layout.addWidget(checkbox)
        hidden_layout.addWidget(self.actual_lead_box)
        hidden_layout.addWidget(self.apply_confirm_checkbox)
        holder_layout = self._selection_v3_legacy_holder.layout()
        if holder_layout is not None:
            holder_layout.addWidget(hidden)
        self._selection_v3_actual_order: list[str] = []
        self._selection_v3_loaded_advice_id: str | None = None
        self._selection_v3_advice_can_apply = False

    def _toggle_selection_v3_team_editor(self, checked: bool) -> None:
        self.selection_v3_team_editor.setVisible(checked)
        self.selection_v3_change_button.setText("編集を閉じる" if checked else "変更")

    def _toggle_selection_v3_management(self, checked: bool) -> None:
        self.selection_v3_management.setVisible(checked)
        self.selection_v3_manage_button.setText(
            "構築管理を閉じる" if checked else "構築管理…"
        )

    def _on_selection_v3_discard_match_for_build_change(
        self, _checked: bool = False
    ) -> None:
        """Operator chose to leave the active match so a build swap is safe.

        Delegates to the existing human-confirmed abort lifecycle
        (``MatchFlowWindow._on_abort_match`` -> ``abort_match``): the current
        session moves to ABORTED, every saved build and canonical record is
        preserved, and the refreshed render returns to the pre-match
        NO_ACTIVE_MATCH state where the build selector and この構築を採用 are
        enabled again. The active match's bound team is never mutated here.
        """

        if not self._mutation_slots_allowed():
            return
        self._on_abort_match()

    def _activate_selection_v3_manual_input(self, slot: int) -> None:
        field = self.opponent_team_inputs[slot - 1]
        field.setReadOnly(False)
        field.setFocus(Qt.FocusReason.OtherFocusReason)
        field.selectAll()

    def _toggle_selection_v3_actual(self, index: int) -> None:
        if index >= len(self.actual_checkboxes):
            return
        name = self.actual_checkboxes[index].text().strip()
        if not name:
            return
        if name in self._selection_v3_actual_order:
            self._selection_v3_actual_order.remove(name)
        elif len(self._selection_v3_actual_order) < 3:
            self._selection_v3_actual_order.append(name)
        self._sync_selection_v3_actual_controls()

    def _sync_selection_v3_actual_controls(self) -> None:
        order = self._selection_v3_actual_order
        valid = len(order) == 3
        for checkbox, button in zip(
            self.actual_checkboxes, self.selection_v3_actual_buttons, strict=True
        ):
            name = checkbox.text().strip()
            number = order.index(name) + 1 if name in order else None
            checkbox.blockSignals(True)
            checkbox.setChecked(number is not None)
            checkbox.blockSignals(False)
            button.setText(f"{number if number is not None else '—'}  {name or '—'}")
            button.setChecked(number is not None)
            button.setProperty("selectionOrder", number or 0)
            button.style().unpolish(button)
            button.style().polish(button)

        self.actual_lead_box.blockSignals(True)
        self.actual_lead_box.clear()
        self.actual_lead_box.addItems(order)
        if order:
            self.actual_lead_box.setCurrentText(order[0])
        self.actual_lead_box.blockSignals(False)
        self.apply_confirm_checkbox.blockSignals(True)
        self.apply_confirm_checkbox.setChecked(valid)
        self.apply_confirm_checkbox.blockSignals(False)
        can_apply = (
            valid
            and getattr(self, "_persistence_reads_allowed", True)
            and self._last_rendered_session_state == "SELECTION_ADVICE_READY"
            and self._selection_v3_advice_can_apply
        )
        self.apply_button.setEnabled(can_apply)
        self.selection_v3_actual_status.setText(
            "1 が先発です。この3体で確定できます。"
            if valid
            else f"あと {3 - len(order)} 体選んでください。"
        )

    def _render_selection_v3(self, current: OperatorView) -> None:
        if not getattr(self, "_selection_v3_ready", False):
            return
        entered_team = tuple(field.text().strip() for field in self.self_team_inputs)
        draft_is_authoritative = getattr(self, "_self_team_editable", True)
        if draft_is_authoritative:
            team = entered_team
            build = self._staged_self_team_build
        else:
            team = current.self_team or entered_team
            build = current.self_team_build or self._staged_self_team_build
        draft_display_name = getattr(self, "_self_team_draft_display_name", None)
        if draft_is_authoritative and draft_display_name:
            build_name = draft_display_name
        elif build is not None:
            build_name = build.name
        elif not draft_is_authoritative and len(current.self_team) == 6:
            build_name = "現在のPT（名前登録）"
        elif all(entered_team):
            build_name = "編集中のPT（未確定）"
        else:
            build_name = "PT未登録"
        self.selection_v3_build_name.setText(build_name)

        # BUILD SWITCH SAFETY (Tournament P0): the base render sets
        # ``_self_team_editable`` False exactly once the current session has a
        # bound self-team snapshot (selection facts confirmed and beyond).
        # Surface why the build controls are locked and offer the one safe
        # explicit exit -- discard the match, then swap the build pre-match.
        session_active = current.projection.session_state is not None
        build_change_locked = session_active and not getattr(
            self, "_self_team_editable", True
        )
        self.selection_v3_build_lock_notice.setVisible(build_change_locked)
        self.selection_v3_discard_match_button.setVisible(build_change_locked)
        self.selection_v3_discard_match_button.setEnabled(
            build_change_locked and current.persistence_reads_allowed
        )

        for index, (name_label, detail_label) in enumerate(
            zip(
                self.selection_v3_team_name_labels,
                self.selection_v3_team_detail_labels,
                strict=True,
            )
        ):
            name = team[index] if index < len(team) and team[index] else f"Slot {index + 1}"
            name_label.setText(name)
            detail = "詳細情報なし"
            if build is not None and name in build.pokemon_names:
                member = build.member_by_name(name)
                item = member.held_item or "持ち物なし"
                detail = f"{item} · {member.ability}\n{' / '.join(member.moves)}"
            detail_label.setText(detail)

        # OCR_AUTO is included here because it is only ever assigned by
        # should_auto_fill() when the OCR top candidate's confidence is
        # already >= AUTO_FILL_THRESHOLD (80.0%) -- an auto-filled slot is
        # therefore just as confirmed as one a human typed or clicked.
        confirmed_origins = {
            SelectionInputOrigin.CANDIDATE_CLICK,
            SelectionInputOrigin.MANUAL_TEXT,
            SelectionInputOrigin.RESTORED,
            SelectionInputOrigin.OCR_AUTO,
        }
        confirmed_count = 0
        for slot in range(1, 7):
            state = self._state_for_current_field(slot)
            value = self.opponent_team_inputs[slot - 1].text().strip()
            confirmed = bool(value) and state.origin in confirmed_origins
            confirmed_count += int(confirmed)
            badge = self.selection_v3_slot_badges[slot - 1]
            badge.setText("確認済み" if confirmed else "要確認")
            badge.setProperty("confirmed", confirmed)
            badge.style().unpolish(badge)
            badge.style().polish(badge)
            field = self.opponent_team_inputs[slot - 1]
            field.setReadOnly(confirmed)
            card = self.selection_v3_opponent_cards[slot - 1]
            card.setMaximumHeight(184 if confirmed else 258)
            self.selection_v3_manual_buttons[slot - 1].setText(
                "修正" if confirmed else "手入力"
            )
            candidate_visible = not confirmed
            self._selection_roi_candidate_labels[slot].setVisible(candidate_visible)
            self._selection_roi_origin_labels[slot].setVisible(not confirmed)
            for button in self._selection_roi_candidate_buttons[slot]:
                button.setVisible(candidate_visible and bool(button.text()))
        self.selection_v3_confirmed_status.setText(f"確認済み {confirmed_count} / 6")
        if not self.selection_v3_ocr_detail_button.isChecked():
            self.selection_roi_status_label.setVisible(False)
        self.selection_roi_send_button.setVisible(
            current.projection.session_state == "SELECTION_OPEN"
        )
        self.selection_roi_send_button.setEnabled(
            self.selection_roi_send_button.isEnabled() and confirmed_count == 6
        )

        advice_id = current.projection.current_selection_advice_id
        advice = current.advice
        advice_status = (
            self._bundle_c_controller.selection_advice_status()
            if current.persistence_reads_allowed
            else None
        )
        current_valid_advice = bool(
            advice_status is not None
            and advice_status.status == "SUCCESS"
            and advice_status.can_apply
            and advice_status.advice_id is not None
            and advice_status.advice_id == advice_id
            and advice is not None
        )
        display_advice = bool(
            advice_status is not None
            and advice_status.advice_id is not None
            and advice_status.advice_id == advice_id
            and advice is not None
        )
        display_binding = (
            advice_status.binding_status if advice_status is not None else "NOT_CHECKED"
        )
        display_legality = (
            advice_status.legality_status if advice_status is not None else "NOT_CHECKED"
        )
        if advice_status is not None and not advice_status.can_apply:
            if display_binding == "CURRENT":
                display_binding = "NOT_CURRENT"
            if display_legality == "VALID":
                display_legality = "NOT_APPLICABLE"
        self._selection_v3_advice_can_apply = current_valid_advice
        self.selection_v3_advice_waiting.setVisible(not display_advice)
        if display_advice and advice is not None and advice_status is not None:
            for label, name in zip(
                self.selection_v3_advice_pick_labels,
                advice.selected_three,
                strict=True,
            ):
                label.setText(name)
            package_label = advice.chosen_package or "—"
            if advice.chosen_package_name:
                package_label = f"{package_label} {advice.chosen_package_name}"
            self.selection_v3_advice_package.setText(package_label)
            self.selection_v3_advice_lead.setText(advice.lead)
            self.selection_v3_advice_intended_mega.setText(advice.intended_mega or "—")
            self.selection_v3_advice_reason.setText(
                advice.selection_reason or "（理由情報なし）"
            )
            self.selection_v3_advice_provider_model.setText(
                f"{advice_status.source_type} · {advice_status.model}"
            )
            self.selection_v3_advice_validity.setText(
                f"{advice_status.source_type} · {display_binding} · {display_legality}"
            )
            if current_valid_advice and advice_id != self._selection_v3_loaded_advice_id:
                self._selection_v3_loaded_advice_id = advice_id
                self._selection_v3_actual_order = [
                    advice.lead,
                    *(name for name in advice.selected_three if name != advice.lead),
                ]
        else:
            for label in self.selection_v3_advice_pick_labels:
                label.setText("—")
            self.selection_v3_advice_package.setText("—")
            self.selection_v3_advice_lead.setText("—")
            self.selection_v3_advice_intended_mega.setText("—")
            self.selection_v3_advice_reason.setText("—")
            self.selection_v3_advice_provider_model.setText("—")
            if advice_status is None:
                self.selection_v3_advice_validity.setText(
                    "UNAVAILABLE · NOT_CHECKED · NOT_CHECKED"
                )
            else:
                self.selection_v3_advice_validity.setText(
                    f"{advice_status.source_type} · {display_binding} · {display_legality}"
                )
        self._sync_selection_v3_actual_controls()

    def _render_selection_roi_slot(
        self, slot: int, match: SelectionSlotMatch | None
    ) -> None:
        super()._render_selection_roi_slot(slot, match)
        thumbnail = self._selection_roi_thumbnail_labels[slot]
        if match is None or match.crop.isNull() or thumbnail.pixmap() is None:
            thumbnail.clear()
            thumbnail.setText("ROI crop unavailable")

    def _on_apply(self, _checked: bool = False) -> None:
        """Apply the visible numbered selection after its explicit click."""

        if not getattr(self, "_selection_v3_ready", False):
            super()._on_apply(_checked)
            return
        if not self._mutation_slots_allowed() or len(self._selection_v3_actual_order) != 3:
            return
        view = self._bundle_c_controller.apply_selection_with_current_gemini_context(
            self._selection_v3_actual_order,
            self._selection_v3_actual_order[0],
            human_confirmed=True,
        )
        self.render_view(view)
        if view.projection.session_state == "BATTLE_READY":
            self.header_tabs.setCurrentIndex(_BATTLE_RECORD_TAB_INDEX)

    @staticmethod
    def _selection_v3_style() -> str:
        return (
            "QWidget#selectionV3Page, QWidget#selectionV3Body { background: #07101a; "
            "color: #edf5fb; font-size: 10px; }"
            "QWidget#selectionV3Left, QWidget#selectionV3Right { background: #081421; }"
            "QWidget#selectionV3Center { background: #07101a; border-left: 1px solid #20384e; "
            "border-right: 1px solid #20384e; }"
            "QLabel { background: transparent; color: #edf5fb; }"
            "QLabel[sectionTitle=\"true\"] { font-size: 13px; font-weight: 800; }"
            "QLabel[cardTitle=\"true\"] { font-weight: 800; }"
            "QLabel[muted=\"true\"] { color: #91a8bb; font-size: 9px; }"
            "QLabel[statusChip=\"true\"] { color: #91a8bb; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 3px 6px; }"
            "QLabel[statusChip=\"true\"][confirmed=\"true\"] { color: #edf5fb; "
            "background: #1a3c5d; }"
            "QLabel[orderBadge=\"true\"] { min-width: 20px; max-width: 20px; "
            "background: #1a3c5d; border-radius: 3px; font-weight: 800; "
            "qproperty-alignment: AlignCenter; }"
            "QLabel[roiCrop=\"true\"] { background: #06101a; color: #60778b; "
            "border: 1px dashed #314b64; border-radius: 3px; }"
            "QLabel[candidate=\"true\"] { color: #b9cadd; }"
            "QLabel[diagnostic=\"true\"] { background: #091623; color: #91a8bb; "
            "border: 1px solid #20384e; padding: 5px; }"
            "QWidget[selectionCard=\"true\"] { background: #0b1724; "
            "border: 1px solid #20384e; border-radius: 4px; }"
            "QLineEdit, QComboBox { background: #0e2032; color: #edf5fb; "
            "border: 1px solid #314b64; border-radius: 3px; padding: 4px 6px; "
            "min-height: 22px; max-height: 26px; }"
            "QPushButton { background: #0e2032; color: #edf5fb; border: 1px solid #314b64; "
            "border-radius: 3px; padding: 5px 7px; }"
            "QPushButton:hover { background: #132c45; }"
            "QPushButton:checked { background: #1a3c5d; font-weight: 800; }"
            "QPushButton[selectionOrder=\"1\"] { border-color: #7dd3fc; }"
            "QPushButton:disabled { color: #60778b; background: #091623; "
            "border-color: #20384e; }"
        )

    @staticmethod
    def _parity_card(title: str, *, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
        card = QWidget()
        card.setProperty("parityCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(7)
        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setProperty("cardTitle", True)
        header.addWidget(title_label)
        header.addStretch(1)
        layout.addLayout(header)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setProperty("muted", True)
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)
        return card, layout

    @staticmethod
    def _parity_page(title: str, hint: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 13px; font-weight: 800;")
        hint_label = QLabel(hint)
        hint_label.setProperty("muted", True)
        hint_label.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(hint_label)
        scroll.setWidget(content)
        return scroll, layout

    def _build_parity_capture_page(self) -> QScrollArea:
        page, layout = self._parity_page(
            "Turn撮影待ち",
            "編集UIはまだ開きません。LIVEを見て必要なタイミングで撮影。",
        )
        summary, summary_layout = self._parity_card("持ち越しstate")
        self.capture_state_summary = QLabel("自分 HP75–100　 相手 HP75–100　 天候なし")
        self.capture_state_summary.setProperty("muted", True)
        summary_layout.addWidget(self.capture_state_summary)
        layout.addWidget(summary)
        empty = QLabel("Turn撮影後に「Turn確認 → Gemini送信」を1回だけ行います")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setObjectName("geminiEmpty")
        empty.setMinimumHeight(190)
        layout.addWidget(empty, 1)
        return page

    def _build_parity_review_page(self) -> QScrollArea:
        page, layout = self._parity_page(
            "Turn確認 → Gemini送信",
            "OCR修正・合法行動・登場時state候補をここでまとめて確認。SENDそのものが最終確定。",
        )
        facts_row = QHBoxLayout()
        for side, title, active, hp, state_editor in (
            ("self", "自分 facts", self.self_active_box, self.self_hp_box, self.self_state_editor),
            (
                "opponent",
                "相手 facts",
                self.opponent_active_input,
                self.opponent_hp_box,
                self.opponent_state_editor,
            ),
        ):
            card, card_layout = self._parity_card(title)
            badge = QLabel("OCR→human review")
            badge.setProperty("badge", True)
            card_layout.insertWidget(1, badge)
            form = QFormLayout()
            form.setContentsMargins(0, 0, 0, 0)
            self._detach_from_parent_layout(active)
            self._detach_from_parent_layout(hp)
            status = QComboBox()
            status.addItems(("なし", "まひ", "やけど", "どく", "ねむり", "不明"))
            status.currentTextChanged.connect(
                lambda value, editor=state_editor: editor.status_field.set_known(
                    Known.confirmed(value, provenance_chain=_HUMAN_INPUT)
                )
            )
            form.addRow("Active", active)
            form.addRow("HP", hp)
            form.addRow("Status", status)
            card_layout.addLayout(form)
            facts_row.addWidget(card, 1)
            if side == "self":
                self.parity_self_status_box = status
            else:
                self.parity_opponent_status_box = status
        layout.addLayout(facts_row)

        ability_card, ability_layout = self._parity_card(
            "相手の特性候補 — 登場時確認",
            subtitle="登場直後の変化を確認し、可能な特性から選択してください。",
        )
        self.parity_ability_card = ability_card
        ability_row = QHBoxLayout()
        self.parity_ability_row = ability_row
        self.parity_ability_buttons: list[QPushButton] = []
        ability_row.addStretch(1)
        ability_layout.addLayout(ability_row)
        layout.addWidget(ability_card)

        self._detach_from_parent_layout(self.review_effect_candidate)
        self.review_effect_candidate.setProperty("candidateArea", True)
        layout.addWidget(self.review_effect_candidate)

        legal_card, legal_layout = self._parity_card("Geminiへ渡す合法行動")
        self.parity_move_chips = []
        move_row = QHBoxLayout()
        move_row.addWidget(QLabel("使用できる技"))
        for index, field in enumerate(self.move_inputs):
            button = QPushButton(field.text() or f"技{index + 1}")
            button.setCheckable(True)
            button.setChecked(bool(field.text()))
            button.setEnabled(bool(field.text()))
            field.textChanged.connect(
                lambda text, chip=button: self._sync_legal_chip(chip, text)
            )
            button.toggled.connect(
                lambda checked, input_field=field: (
                    None if checked else input_field.clear()
                )
            )
            move_row.addWidget(button)
            self.parity_move_chips.append(button)
        move_row.addStretch(1)
        legal_layout.addLayout(move_row)
        switch_row = QHBoxLayout()
        switch_row.addWidget(QLabel("交代できるポケモン"))
        self.parity_switch_chips = []
        for checkbox in self.switch_checkboxes:
            button = QPushButton(checkbox.text())
            button.setCheckable(True)
            button.setChecked(checkbox.isChecked())
            button.toggled.connect(checkbox.setChecked)
            checkbox.toggled.connect(button.setChecked)
            switch_row.addWidget(button)
            self.parity_switch_chips.append(button)
        switch_row.addStretch(1)
        legal_layout.addLayout(switch_row)
        layout.addWidget(legal_card)
        layout.addStretch(1)
        return page

    def _build_parity_action_page(self) -> QScrollArea:
        page, layout = self._parity_page(
            "行動・結果記録",
            "実際の技/交代・行動順・変わった結果だけ記録。",
        )
        state_card, state_layout = self._parity_card("Turn開始state")
        self.action_state_summary = QLabel("天候なし　 Gemini回答済み")
        self.action_state_summary.setProperty("muted", True)
        state_layout.addWidget(self.action_state_summary)
        layout.addWidget(state_card)
        actions_row = QHBoxLayout()

        self_card, self_layout = self._parity_card("自分の実際の行動")
        self.self_action_tabs = {}
        tab_row = QHBoxLayout()
        for action_type, label in (("MOVE", "技"), ("SWITCH", "交代")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, value=action_type: self._set_self_action_type(value)
            )
            tab_row.addWidget(button)
            self.self_action_tabs[action_type] = button
        tab_row.addStretch(1)
        self_layout.addLayout(tab_row)
        move_grid = QGridLayout()
        self.self_action_move_buttons = []
        for index, field in enumerate(self.move_inputs):
            button = QPushButton(field.text() or f"技{index + 1}")
            button.setCheckable(True)
            button.setEnabled(bool(field.text()))
            field.textChanged.connect(
                lambda text, chip=button: self._sync_action_chip(chip, text)
            )
            button.clicked.connect(
                lambda _checked=False, input_field=field: self._choose_self_move(
                    input_field.text()
                )
            )
            move_grid.addWidget(button, index // 4, index % 4)
            self.self_action_move_buttons.append(button)
        self_layout.addLayout(move_grid)
        switch_target_row = QHBoxLayout()
        self.self_switch_target_label = QLabel("交代先")
        self.self_switch_target_box = QComboBox()
        self.self_switch_target_box.setPlaceholderText("交代先を選択")
        self.self_switch_target_box.currentTextChanged.connect(self._choose_self_switch)
        self.self_switch_unavailable_label = QLabel("交代できる候補がありません")
        self.self_switch_unavailable_label.setProperty("muted", True)
        switch_target_row.addWidget(self.self_switch_target_label)
        switch_target_row.addWidget(self.self_switch_target_box, 1)
        switch_target_row.addWidget(self.self_switch_unavailable_label)
        self_layout.addLayout(switch_target_row)
        actions_row.addWidget(self_card, 1)

        opponent_card, opponent_layout = self._parity_card("相手の実際の行動")
        self.opponent_action_tabs = {}
        opponent_tabs = QHBoxLayout()
        for action_type, label in (
            ("MOVE", "技"),
            ("SWITCH", "交代"),
            ("NO ACTION", "行動なし"),
            ("UNKNOWN", "不明"),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, value=action_type: self._set_opponent_action_type(value)
            )
            opponent_tabs.addWidget(button)
            self.opponent_action_tabs[action_type] = button
        opponent_layout.addLayout(opponent_tabs)
        opponent_action_row = QHBoxLayout()
        self.parity_opponent_action_input = QLineEdit()
        self.parity_opponent_action_input.setPlaceholderText("相手の実際の技・交代先を入力")
        self.opponent_action_suggestion_box = QComboBox(opponent_card)
        self.opponent_action_suggestion_box.setPlaceholderText("候補を選択")
        self.opponent_action_suggestion_box.setMinimumContentsLength(12)
        self.opponent_action_suggestion_box.textActivated.connect(
            self._select_opponent_action_suggestion
        )
        self.opponent_move_suggestions: tuple[str, ...] = ()
        self.opponent_switch_suggestions: tuple[str, ...] = ()
        self.parity_opponent_action_input.textChanged.connect(
            self.opponent_action_name_input.setText
        )
        self.opponent_action_name_input.textChanged.connect(
            self.parity_opponent_action_input.setText
        )
        opponent_action_row.addWidget(self.opponent_action_suggestion_box)
        opponent_action_row.addWidget(self.parity_opponent_action_input, 1)
        opponent_layout.addLayout(opponent_action_row)
        actions_row.addWidget(opponent_card, 1)
        layout.addLayout(actions_row)

        self._detach_from_parent_layout(self.result_effect_candidate)
        self.result_effect_candidate.setProperty("candidateArea", True)
        layout.addWidget(self.result_effect_candidate)

        result_card, result_layout = self._parity_card("結果")
        order_row = QHBoxLayout()
        self.parity_order_buttons = {}
        for value, label in (
            ("SELF_FIRST", "自分→相手"),
            ("OPPONENT_FIRST", "相手→自分"),
            ("UNKNOWN", "順序不明"),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, order=value: self.action_order_box.setCurrentText(order)
            )
            order_row.addWidget(button)
            self.parity_order_buttons[value] = button
        order_row.addStretch(1)
        result_layout.addLayout(order_row)
        layout.addWidget(result_card)
        layout.addStretch(1)
        return page

    def _build_parity_recorded_page(self) -> QScrollArea:
        page, layout = self._parity_page(
            "このTurnは記録済みです",
            "確認済み内容だけをcompact summaryとして表示します。",
        )
        self.recorded_summary_label = QLabel()
        self.recorded_summary_label.setWordWrap(True)
        for title in ("Actions", "State events", "Next state"):
            card, card_layout = self._parity_card(title)
            summary = self.recorded_summary_label if title == "Actions" else QLabel("—")
            card_layout.addWidget(summary)
            layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _confirm_parity_ability(self, ability: str) -> None:
        if self.opponent_ability_box.findText(ability) < 0:
            self.opponent_ability_box.addItem(ability)
        self.opponent_ability_box.setCurrentText(ability)
        if (
            self._active_ability_entry_event_id is None
            and self._provisional_ability_species is not None
        ):
            # The OCR projection is intentionally read-only: the human's
            # explicit choice is staged until confirming the Turn creates
            # the canonical entry event, then applied to that exact event.
            self._pending_ocr_ability_confirmation = (
                self._provisional_ability_species,
                ability,
            )
            self.ability_resolution_group.setVisible(False)
            self.parity_ability_card.setVisible(False)
            return
        self._on_confirm_opponent_ability()

    @staticmethod
    def _sync_legal_chip(button: QPushButton, text: str) -> None:
        button.setText(text or "未取得")
        button.setEnabled(bool(text))
        button.setChecked(bool(text))

    @staticmethod
    def _sync_action_chip(button: QPushButton, text: str) -> None:
        button.setText(text or "未取得")
        button.setEnabled(bool(text))

    def _set_self_action_type(self, action_type: str) -> None:
        self.actual_action_type_box.setCurrentText(action_type)
        self.actual_action_confirm_checkbox.setChecked(True)
        self._sync_parity_action_selection()

    def _choose_self_move(self, move: str) -> None:
        if not move:
            return
        self._set_self_action_type("MOVE")
        self.actual_action_name_box.setCurrentText(move)
        self._sync_parity_action_selection()

    def _choose_self_switch(self, target: str) -> None:
        if not target:
            return
        self._set_self_action_type("SWITCH")
        self.actual_action_name_box.setCurrentText(target)

    def _set_opponent_action_type(self, action_type: str) -> None:
        self.opponent_action_type_box.setCurrentText(action_type)
        if action_type not in {"MOVE", "SWITCH"}:
            self.opponent_action_name_input.clear()
        self._show_opponent_action_suggestions(action_type)
        self._sync_parity_action_selection()

    def _select_opponent_action_suggestion(self, value: str) -> None:
        """Apply a candidate only after the human activates it."""

        if self.opponent_action_type_box.currentText() not in {"MOVE", "SWITCH"}:
            return
        self.parity_opponent_action_input.setText(value)

    @staticmethod
    def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))

    def _show_opponent_action_suggestions(self, action_type: str) -> None:
        suggestions = (
            self.opponent_move_suggestions
            if action_type == "MOVE"
            else self.opponent_switch_suggestions
            if action_type == "SWITCH"
            else ()
        )
        # Rebuilding this explicitly human-operated selector is presentation-only.
        # Blocking signals and leaving no current index ensures candidate display
        # never writes the authoritative free-text draft.
        self.opponent_action_suggestion_box.blockSignals(True)
        try:
            self.opponent_action_suggestion_box.clear()
            self.opponent_action_suggestion_box.addItems(list(suggestions))
            self.opponent_action_suggestion_box.setCurrentIndex(-1)
        finally:
            self.opponent_action_suggestion_box.blockSignals(False)
        self.opponent_action_suggestion_box.setEnabled(
            action_type in {"MOVE", "SWITCH"} and bool(suggestions)
        )

    def _refresh_opponent_action_assist(
        self,
        current: OperatorView,
        *,
        species: str,
        match_facts: MatchOpponentFacts,
    ) -> None:
        confirmed_state = self._bundle_c_controller.turn_state_summary().confirmed_state
        confirmed_active = (
            confirmed_state.opponent_side.active if confirmed_state is not None else None
        )
        current_is_confirmed = bool(
            confirmed_active is not None
            and confirmed_active.is_confirmed
            and confirmed_active.value is not None
            and self._bundle_c_controller._same_opponent_species(
                confirmed_active.value, species
            )
        )
        if not current_is_confirmed:
            self.opponent_move_suggestions = ()
            self.opponent_switch_suggestions = ()
            self._show_opponent_action_suggestions(
                self.opponent_action_type_box.currentText()
            )
            return
        meta = self._opponent_meta_provider.get(species.strip())
        meta_moves = tuple(entry.name for entry in meta.moves) if meta is not None else ()
        self.opponent_move_suggestions = self._ordered_unique(
            (*match_facts.moves, *meta_moves)
        )
        self.opponent_switch_suggestions = self._ordered_unique(
            tuple(
                member
                for member in current.opponent_team
                if not self._bundle_c_controller._same_opponent_species(member, species)
            )
        )
        self._show_opponent_action_suggestions(
            self.opponent_action_type_box.currentText()
        )

    def _sync_parity_action_selection(self) -> None:
        own = self.actual_action_type_box.currentText()
        opponent = self.opponent_action_type_box.currentText()
        for value, button in self.self_action_tabs.items():
            button.setChecked(value == own)
        selected_move = self.actual_action_name_box.currentText() if own == "MOVE" else ""
        for button, field in zip(
            self.self_action_move_buttons, self.move_inputs, strict=True
        ):
            button.setChecked(bool(selected_move) and field.text() == selected_move)
        for value, button in self.opponent_action_tabs.items():
            button.setChecked(value == opponent)
        order = self.action_order_box.currentText()
        for value, button in self.parity_order_buttons.items():
            button.setChecked(value == order)
        switch_target = self.actual_action_name_box.currentText() if own == "SWITCH" else ""
        self.self_switch_target_box.blockSignals(True)
        try:
            self.self_switch_target_box.setCurrentText(switch_target)
        finally:
            self.self_switch_target_box.blockSignals(False)

    def _refresh_self_switch_targets(
        self, current: OperatorView, summary: TurnStateSummaryView | None = None
    ) -> None:
        """Refresh the actual SELF action selector from the exact binding.

        ``TurnFactsView.legal_switches`` is a legacy reviewed-facts field and
        may be empty even after the human has confirmed the legal-switch set
        in the Bundle 2 exact binding. An absent/mismatched confirmation is
        deliberately kept distinct from ``CONFIRMED_NONE``.
        """

        if summary is None:
            summary = self._bundle_c_controller.turn_state_summary()
        confirmation = summary.legal_switch_confirmation
        confirmed_state = summary.confirmed_state
        confirmation_is_current = bool(
            confirmation is not None
            and summary.identity is not None
            and confirmed_state is not None
            and confirmation.identity == confirmed_state.identity
            and self._same_turn_binding_for_ui(confirmation.identity, summary.identity)
            and confirmation.based_on_confirmed_state_id
            == confirmed_state.confirmed_state_id
            and current.projection.current_applied_selection_id
            == confirmation.applied_selection_id
        )
        if not confirmation_is_current:
            confirmation = None

        if confirmation is None:
            legal_switches: tuple[str, ...] = ()
            unavailable_message = "交代候補が未確認です"
        elif confirmation.status is LegalSwitchStatus.CONFIRMED_NONE:
            legal_switches = ()
            unavailable_message = "交代できるポケモンはいません"
        else:
            # The exact confirmation is the source of truth. The current
            # summary's derived candidates are used only as a safety filter
            # for active/selected-three/confirmed-fainted invariants; they
            # never fill or replace the confirmed set.
            safe_candidates = set(summary.legal_switch_candidates)
            legal_switches = tuple(
                name for name in confirmation.legal_switches if name in safe_candidates
            )
            unavailable_message = "" if legal_switches else "交代候補を確認してください"
        selected = (
            self.actual_action_name_box.currentText()
            if self.actual_action_type_box.currentText() == "SWITCH"
            else ""
        )
        self.self_switch_target_box.blockSignals(True)
        try:
            self.self_switch_target_box.clear()
            self.self_switch_target_box.addItems(list(legal_switches))
            if selected in legal_switches:
                self.self_switch_target_box.setCurrentText(selected)
            else:
                self.self_switch_target_box.setCurrentIndex(-1)
        finally:
            self.self_switch_target_box.blockSignals(False)
        has_legal_switch = bool(legal_switches)
        self.self_switch_target_box.setEnabled(has_legal_switch)
        self.self_switch_unavailable_label.setText(unavailable_message)
        self.self_switch_unavailable_label.setVisible(not has_legal_switch)

    @staticmethod
    def _same_turn_binding_for_ui(left: TurnIdentity, right: TurnIdentity) -> bool:
        """Allow same-Turn metadata revisions without accepting stale Turns."""

        return (
            left.session_id == right.session_id
            and left.match_id == right.match_id
            and left.generation == right.generation
            and left.turn_id == right.turn_id
            and left.turn_number == right.turn_number
        )

    @staticmethod
    def _player_turn_advice_action(advice: TurnAdviceView) -> str:
        if advice.unavailable_reason is not None:
            return "Geminiの応答を使用できませんでした。再送してください。"
        if advice.action_type == "SWITCH":
            return f"交代 → {advice.action_name}"
        return advice.action_name

    @staticmethod
    def _player_turn_advice_prediction(advice: TurnAdviceView) -> str:
        if advice.structured_v2 is not None:
            line = advice.structured_v2.opponent_prediction.primary
            if line.category == "UNKNOWN":
                return ""
            prediction = line.summary.strip()
        else:
            prediction = advice.opponent_prediction.strip()
            if prediction.upper() in {"UNKNOWN", "—"}:
                return ""
        if not prediction or any(token in prediction for token in _TECHNICAL_PREDICTION_TOKENS):
            return ""
        return prediction

    def _render_turn_advice_player_surface(
        self, advice: TurnAdviceView, summary: TurnStateSummaryView
    ) -> None:
        """Render only live decision content; keep provenance out of view."""

        if advice.unavailable_reason is not None:
            self.turn_advice_action_label.setText(
                "Geminiの応答を使用できませんでした。再送してください。"
            )
            self.turn_advice_rationale_label.setText("")
            self.turn_advice_prediction_label.setText("")
            self.turn_advice_prediction_card.setVisible(False)
            self.turn_advice_warning_card.setVisible(False)
            return

        self.turn_advice_action_label.setText(self._player_turn_advice_action(advice))
        self.turn_advice_rationale_label.setText(advice.rationale.strip())
        prediction = self._player_turn_advice_prediction(advice)
        self.turn_advice_prediction_label.setText(prediction)
        self.turn_advice_prediction_card.setVisible(bool(prediction))

        actionable_warning = ""
        if (
            advice.action_type == "SWITCH"
            and summary.legal_switch_confirmation is None
        ):
            actionable_warning = "交代候補を確認してください"
        self.turn_advice_warnings_label.setText(actionable_warning)
        self.turn_advice_warning_card.setVisible(bool(actionable_warning))

    def _capture_action_result_draft(self) -> tuple[str, str, bool, str, str, str]:
        return (
            self.actual_action_type_box.currentText(),
            self.actual_action_name_box.currentText(),
            self.actual_action_confirm_checkbox.isChecked(),
            self.opponent_action_type_box.currentText(),
            self.opponent_action_name_input.text(),
            self.action_order_box.currentText(),
        )

    def _restore_action_result_draft(
        self, draft: tuple[str, str, bool, str, str, str]
    ) -> None:
        own_type, own_name, confirmed, opponent_type, opponent_name, order = draft
        self.actual_action_type_box.setCurrentText(own_type)
        self.actual_action_name_box.setCurrentText(own_name)
        self.opponent_action_type_box.setCurrentText(opponent_type)
        self.opponent_action_name_input.setText(opponent_name)
        self.action_order_box.setCurrentText(order)
        self.actual_action_confirm_checkbox.setChecked(confirmed)

    def _reset_action_result_delta_editors(self) -> None:
        """Clear every result-delta widget and pending effect candidate.

        Called whenever ``render_view`` observes a new Turn identity (see
        ``same_action_result_identity`` above) -- distinct from
        ``_capture_action_result_draft``/``_restore_action_result_draft``,
        which only preserve the action-selection boxes across an in-phase
        re-render of the *same* Turn.
        """

        self.self_delta_editor.reset()
        self.opponent_delta_editor.reset()
        self.weather_delta_field.reset()
        self.terrain_delta_field.reset()
        self.result_effect_candidate.clear()

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

    # -- Turn OCR compact status indicator (display-only) ------------------------

    def _set_turn_snapshot_status(self, status: str, message: str) -> None:
        # Display bookkeeping only: records the raw status code the base
        # class already computed (via the identity-gated capture/OCR flow
        # unchanged below) so the compact indicator can reflect it. Never
        # itself decides freshness/identity -- that guarantee already comes
        # from _on_turn_snapshot_result's existing _identity_is_current()
        # check, which is what determines whether this even gets called for
        # a given result.
        self._turn_ocr_status_code = status
        super()._set_turn_snapshot_status(status, message)

    def _on_turn_snapshot_result(self, payload: object) -> None:
        # The base callback (_identity_is_current-gated) has already updated
        # _turn_ocr_status_code / _turn_snapshot_origins by the time this
        # returns, but it never calls render_view() -- so without this, the
        # compact indicator stays on "OCR中…" until something unrelated
        # happens to trigger a full re-render, even though the OCR-derived
        # fields are already visible. Refresh just this label immediately.
        super()._on_turn_snapshot_result(payload)
        self._refresh_turn_ocr_status_indicator()
        self._refresh_turn_ocr_milestone_log()

    def _refresh_turn_ocr_status_indicator(self) -> None:
        if not hasattr(self, "turn_ocr_status_indicator_label"):
            return
        if not hasattr(self, "_bundle_c_controller"):
            return
        session_state = self._bundle_c_controller.refresh().projection.session_state
        self.turn_ocr_status_indicator_label.setText(
            self._turn_ocr_status_indicator_text(session_state)
        )

    def _refresh_turn_ocr_milestone_log(self) -> None:
        if not hasattr(self, "turn_ocr_milestone_log_label"):
            return
        milestones = getattr(self, "_turn_ocr_milestones", None)
        if not milestones:
            self.turn_ocr_milestone_log_label.setText("(まだ記録なし)")
            return
        self.turn_ocr_milestone_log_label.setText("\n".join(milestones))

    def _turn_ocr_status_indicator_text(self, session_state: str | None) -> str:
        if session_state != "TURN_CAPTURE_PENDING":
            return "OCR待機"
        code = self._turn_ocr_status_code
        if code in _TURN_OCR_ERROR_STATUSES:
            return "OCRエラー"
        if code == TurnSnapshotStatus.READY:
            needs_review = any(
                origin == _TURN_SNAPSHOT_ORIGIN_OCR
                for origin in self._turn_snapshot_origins.values()
            )
            return "OCR要確認" if needs_review else "OCR完了"
        # IDLE / CAPTURED / ANALYZING: a capture has been requested for this
        # Turn and OCR has not yet produced (or failed to produce) a result.
        return "OCR中…"

    # -- render ------------------------------------------------------------------

    def render_view(self, view: OperatorView | None = None) -> None:
        # MapleMainWindow renders once from its constructor, before this
        # subclass has installed its Battle Record controller/widgets.
        if not hasattr(self, "_bundle_c_controller"):
            super().render_view(view)
            return
        current = view if view is not None else self._bundle_c_controller.refresh()
        projection = current.projection
        action_result_identity = (
            projection.session_id,
            projection.match_id,
            projection.generation,
            projection.current_turn_id,
            projection.current_reviewed_board_id,
        )
        action_result_phase = projection.primary_cta == "RECORD_ACTUAL_ACTION"
        same_action_result_identity = (
            getattr(self, "_action_result_draft_identity", None) == action_result_identity
        )
        preserve_action_result_draft = (
            action_result_phase
            and getattr(self, "_action_result_phase_active", False)
            and same_action_result_identity
        )
        action_result_draft = (
            self._capture_action_result_draft() if preserve_action_result_draft else None
        )
        if not same_action_result_identity:
            # 00 R2 lifecycle fix (Issue #31 historical defect: a Turn 3
            # human-confirmed Swords Dance CHANGED:+2 stayed visible in
            # this editor into later Turns because nothing here was ever
            # bound to TurnIdentity). A fresh identity always starts its
            # own result-delta draft UNCHANGED -- the CHANGED value the
            # human already confirmed for the prior Turn still carries
            # forward correctly through the persisted domain projection
            # into the *current state* editor; only this reusable capture
            # widget needs clearing.
            self._reset_action_result_delta_editors()
            if hasattr(self, "_result_entry_active"):
                self._result_entry_active = False
                self._result_events.clear()
                self._result_candidate_decisions.clear()

        super().render_view(current)
        if hasattr(self, "match_end_local_group"):
            endable = projection.session_state in {
                "BATTLE_READY",
                "TURN_REVIEWED",
                "TURN_RECORDED",
            }
            self.match_end_group.setVisible(False)
            self.match_end_local_group.setVisible(endable)
            for outcome_button in (self.match_win_button, self.match_loss_button):
                outcome_button.setEnabled(endable and current.persistence_reads_allowed)
            self._sync_match_outcome_buttons(self.outcome_box.currentText())
        if action_result_draft is not None:
            self._restore_action_result_draft(action_result_draft)
        self._action_result_phase_active = action_result_phase
        self._action_result_draft_identity = action_result_identity
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
        summary = self._bundle_c_controller.turn_state_summary()
        self._render_selection_v3(current)
        if hasattr(self, "legal_switch_group"):
            self._render_legal_switch_workbench(summary)
        self._sync_switch_candidates(self.self_active_box.currentText())

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
        # Bundle 2 (Gemini V2) R3-A: this re-render every refresh -- must
        # stay the stable factual label. Re-labeling it "SEND TURN TO
        # GEMINI" here would be false: confirming facts never sends (see
        # R2). Only the distinct explicit-send control below may say that.
        self.confirm_turn_facts_button.setText("CONFIRM TURN FACTS")
        self.record_action_button.setText("結果記録")
        self.next_turn_button.setText("NEXT TURN")

        active_button_by_cta = {
            "CREATE_NEW_MATCH": self.new_match_button,
            "NEW_MATCH": self.new_match_after_export_button,
            "START_TURN_CAPTURE": self.start_turn_button,
            "CONFIRM_TURN_FACTS": self.confirm_turn_facts_button,
            "RECORD_ACTUAL_ACTION": self.record_action_button,
            "NEXT_TURN": self.next_turn_button,
        }
        active_button = active_button_by_cta.get(projection.primary_cta)
        for lifecycle_button in (*self.field_new_match_buttons, *self.lifecycle_buttons):
            lifecycle_button.setProperty("active", lifecycle_button is active_button)
            lifecycle_button.style().unpolish(lifecycle_button)
            lifecycle_button.style().polish(lifecycle_button)

        turn_text = projection.turn_number if projection.turn_number is not None else "—"
        match_text = f"#{projection.match_id}" if projection.match_id else "未取得"
        self.battle_context_label.setText(f"Match {match_text}   Turn {turn_text}")
        phase_labels = {
            "START_TURN_CAPTURE": "撮影待ち",
            "CONFIRM_TURN_FACTS": "Turn確認",
            "REQUEST_TURN_ADVICE": "送信待ち",
            "RECORD_ACTUAL_ACTION": "行動・結果記録",
            "NEXT_TURN": "記録済み",
        }
        self.header_phase_badge.setText(phase_labels.get(projection.primary_cta, "確認中"))

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
        self.evidence_open_button.setEnabled(turn_state)
        self._refresh_turn_ocr_milestone_log()
        self.review_state_event_button.setEnabled(
            current.persistence_reads_allowed
            and (editable or projection.primary_cta == "RECORD_ACTUAL_ACTION")
        )

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

        # The generic HP/Active/state delta editor is intentionally absent
        # from the normal Result Entry workflow.  Its hidden widgets remain
        # the projection target for the existing canonical SideDelta path.
        self.action_result_delta_group.setVisible(False)

        # The center follows the completed HTML's lifecycle composition:
        # LIVE is persistent and exactly one compact work surface sits below
        # it. None of the existing controller/domain operations are changed.
        if projection.primary_cta == "START_TURN_CAPTURE":
            workbench_page = self.capture_workbench_page
        elif projection.primary_cta == "RECORD_ACTUAL_ACTION":
            workbench_page = self.action_workbench_page
        elif projection.primary_cta == "NEXT_TURN":
            workbench_page = self.recorded_workbench_page
        else:
            workbench_page = self.review_workbench_page
        self.workbench_stack.setCurrentWidget(workbench_page)
        if not self._result_entry_active:
            self.action_result_step_stack.setCurrentWidget(self.action_entry_step_page)
        self.workbench_stack.setMinimumHeight(350)
        self.workbench_stack.setMaximumHeight(350)
        self.recorded_summary_label.setText(
            f"Turn {projection.turn_number} の行動と結果を保存しました。"
            "左の確定履歴を確認し、次のTurnへ進んでください。"
        )
        if self._result_entry_active and projection.primary_cta == "RECORD_ACTUAL_ACTION":
            self._show_result_entry_page(current.persistence_reads_allowed)

        # The v5 right rail never changes hierarchy: Gemini owns the upper
        # slot even while empty/waiting, with compact INTEL directly below.
        status = self._bundle_c_controller.rich_turn_advice_gemini_status()
        # Keep the existing controller-bound status values populated for the
        # hidden audit owner, but never compose them into the live player
        # panel. Success is represented by the advice itself; failures use
        # one concise recovery message below.
        status_text = _RICH_STATUS_LABELS.get(status.status, status.status)
        self.rich_gemini_status_label.setText(f"送信状態: {status_text}")
        self.rich_gemini_denial_label.setText("")
        failure_message = _TURN_ADVICE_PLAYER_FAILURE_MESSAGES.get(status.status)
        advice_visible = (
            projection.primary_cta in {"RECORD_ACTUAL_ACTION", "NEXT_TURN"}
            and current.turn_advice is not None
        )
        self.rich_gemini_group.setVisible(True)
        self.gemini_empty_label.setVisible(not advice_visible)
        self.turn_advice_group.setVisible(advice_visible)
        if not advice_visible:
            self.gemini_empty_label.setText(
                failure_message
                or (
                    "Turn撮影後に確認へ"
                    if projection.primary_cta == "START_TURN_CAPTURE"
                    else "SEND TURN TO GEMINI 待ち"
                )
            )
        if current.turn_advice is not None:
            self._render_turn_advice_player_surface(current.turn_advice, summary)
        self._refresh_turn_ocr_status_indicator()

        gemini_button = getattr(self, "_bundle_c_gemini_send_button", None)
        if gemini_button is not None:
            # Sending is a separate trusted human action. Keep the control
            # available whenever the freshly confirmed binding is provider
            # ready, and relabel it as a retry only after a failed send.
            failed_last_send = status.status in _TURN_ADVICE_FAILURE_STATUSES
            send_available = (
                projection.primary_cta == "REQUEST_TURN_ADVICE"
                and current.persistence_reads_allowed
                and summary.provider_ready
                and status.status != "PENDING"
                and status.status != "SUCCESS"
            )
            gemini_button.setText("Gemini再送信" if failed_last_send else "SEND TURN TO GEMINI")
            gemini_button.setVisible(send_available)
            gemini_button.setEnabled(send_available)

        species = self.opponent_active_input.text().strip()
        try:
            entity_id = self._opponent_entity_id(species)
        except SpeciesCatalogCoverageError:
            remembered_ability = None
        else:
            remembered_ability = self._bundle_c_controller.opponent_ability_for_entity(entity_id)
        self._render_ability_resolution()
        match_facts = self._render_opponent_intel(species, remembered_ability)
        self._refresh_opponent_action_assist(
            current, species=species, match_facts=match_facts
        )
        self._refresh_self_switch_targets(current, summary)
        self._update_v5_action_disclosure()
        self._sync_parity_action_selection()
        self.live_current_state_label.setText(
            f"自分: {self.self_active_box.currentText() or '—'}  "
            f"HP{self.self_hp_box.currentText() or '—'}"
            f"    相手: {self.opponent_active_input.text() or '—'}  "
            f"HP{self.opponent_hp_box.currentText() or '—'}"
        )

        if self._result_entry_active and projection.primary_cta == "RECORD_ACTUAL_ACTION":
            # Result Entry is a second step of the current action lifecycle;
            # its NEXT TURN click performs the atomic result commit first.
            # A harmless render must not disable that live control merely
            # because the legacy primary CTA is still RECORD_ACTUAL_ACTION.
            self.next_turn_button.setEnabled(current.persistence_reads_allowed)
        else:
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
            self._bind_turn_snapshot_draft_field(
                "self_active",
                self_active.value,
                # Every value in an OPEN draft belongs to the previous Turn
                # (including a result-derived CHANGED ZERO). It is only a
                # starting point for this new board capture; current-Turn
                # human input remains locked and current-Turn OCR may replace
                # the carry-forward value.
                ocr_replaceable=True,
            )
        opponent_active = draft.opponent_side.active
        if opponent_active.is_confirmed and opponent_active.value is not None:
            self._bind_turn_snapshot_draft_field(
                "opponent_active",
                opponent_active.value,
                ocr_replaceable=True,
            )
        self_hp = draft.self_side.hp_bucket
        if self_hp.is_confirmed and self_hp.value is not None:
            self._bind_turn_snapshot_draft_field(
                "self_hp",
                self_hp.value.value,
                ocr_replaceable=True,
            )
        opponent_hp = draft.opponent_side.hp_bucket
        if opponent_hp.is_confirmed and opponent_hp.value is not None:
            self._bind_turn_snapshot_draft_field(
                "opponent_hp",
                opponent_hp.value.value,
                ocr_replaceable=True,
            )

    # -- overridden handlers: gather the new widgets, then delegate ------------

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        # A hidden/excluded slot (see _sync_switch_candidates) can only be
        # checked if something checked it before it was hidden -- guard
        # against ever submitting a blank switch name regardless.
        switches = [
            checkbox.text()
            for checkbox in self.switch_checkboxes
            if checkbox.isChecked() and checkbox.text()
        ]
        # The exact legal-switch candidates currently visible/edited in the
        # workbench list -- CONFIRM TURN FACTS confirms exactly this, never
        # a freshly re-derived set. See confirm_turn_facts's
        # legal_switch_selection parameter.
        legal_switch_selection = tuple(
            item.text() for item in self.legal_switch_list.selectedItems()
        )
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
            legal_switch_selection=legal_switch_selection,
        )
        pending_confirmation = self._pending_ocr_ability_confirmation
        if pending_confirmation is not None:
            species, ability = pending_confirmation
            event = self._bundle_c_controller.turn_state_summary().pending_opponent_entry_event
            if event is not None and event.species_name == species:
                confirmed = self._bundle_c_controller.confirm_opponent_ability(
                    opponent_entity_id=event.opponent_entity_id,
                    species=event.species_name,
                    ability=ability,
                    entry_event_id=event.event_id,
                )
                self._pending_ocr_ability_confirmation = None
                self._provisional_ability_species = None
                self._active_ability_entry_event_id = None
                if confirmed is not None:
                    entry = find_effect(confirmed)
                    if entry is not None:
                        self.review_effect_candidate.propose(
                            entry, prefix=f"相手の{entry.display_name_ja}"
                        )
                view = self._bundle_c_controller.refresh()
        self.render_view(view)
        # CONFIRM TURN FACTS is a factual persistence action only. Provider
        # dispatch remains a separate trusted human action so a re-render,
        # retry, or legal-switch correction cannot silently double-send.

    def _current_result_active_names(self) -> dict[str, str]:
        summary = self._bundle_c_controller.turn_state_summary()
        state = summary.confirmed_state
        if state is None:
            return {"self": "不明", "opponent": "不明"}
        return {
            "self": state.self_side.active.value or "不明",
            "opponent": state.opponent_side.active.value or "不明",
        }

    def _on_open_result_entry(self, _checked: bool = False) -> None:
        """Navigate from Action Entry to Result Entry without persistence."""

        if not self._mutation_slots_allowed():
            return
        action_type = self.actual_action_type_box.currentText()
        action_name = self.actual_action_name_box.currentText().strip()
        opponent_type = self.opponent_action_type_box.currentText()
        opponent_name = self.opponent_action_name_input.text().strip()
        if action_type not in {"MOVE", "SWITCH"} or not action_name:
            self.error_label.setText("自分の実行行動を選択してください。")
            return
        if not self.actual_action_confirm_checkbox.isChecked():
            self.error_label.setText("実行行動を人間が確認してください。")
            return
        if opponent_type in {"MOVE", "SWITCH"} and not opponent_name:
            self.error_label.setText("相手の実行行動名を入力してください。")
            return
        self.error_label.clear()
        self._result_entry_active = True
        self._result_events.clear()
        self._result_candidate_decisions.clear()
        self._result_validation_error = ""
        self._build_move_result_candidates()
        self._project_result_events()
        self.weather_delta_field.mode_box.setCurrentText("UNKNOWN")
        self.terrain_delta_field.mode_box.setCurrentText("UNKNOWN")
        self._show_result_entry_page()

    def _on_back_to_action_entry(self, _checked: bool = False) -> None:
        self._result_entry_active = False
        self.render_view()

    def _show_result_entry_page(self, persistence_reads_allowed: bool = True) -> None:
        self.workbench_stack.setCurrentWidget(self.action_workbench_page)
        self.action_result_step_stack.setCurrentWidget(self.result_workbench_page)
        self.record_action_button.setText("結果記録")
        self.record_action_button.setEnabled(False)
        self.next_turn_button.setVisible(True)
        self.next_turn_button.setEnabled(True)
        self.next_turn_button.setProperty("active", True)
        self.next_turn_button.style().unpolish(self.next_turn_button)
        self.next_turn_button.style().polish(self.next_turn_button)
        self.record_self_faint_button.setCheckable(True)
        self.record_opponent_faint_button.setCheckable(True)
        self.record_self_faint_button.setText("自分ひんし")
        self.record_opponent_faint_button.setText("相手ひんし")
        self.record_self_faint_button.setVisible(True)
        self.record_opponent_faint_button.setVisible(True)
        self.record_self_faint_button.setEnabled(persistence_reads_allowed)
        self.record_opponent_faint_button.setEnabled(persistence_reads_allowed)
        self.record_self_faint_button.setChecked(
            any(
                event.kind == "faint" and event.target_side == "self"
                for event in self._result_events
            )
        )
        self.record_opponent_faint_button.setChecked(
            any(
                event.kind == "faint" and event.target_side == "opponent"
                for event in self._result_events
            )
        )
        self.mega_result_group.setVisible(True)
        self._render_mega_controls(persistence_reads_allowed)

    def _current_result_active_for_mega(self, side: MegaSide) -> str | None:
        """Return only a nonblank, human-confirmed active Pokemon name."""

        confirmed = self._bundle_c_controller.turn_state_summary().confirmed_state
        if confirmed is None:
            return None
        known = (
            confirmed.self_side.active
            if side is MegaSide.SELF
            else confirmed.opponent_side.active
        )
        if not known.is_confirmed or not isinstance(known.value, str):
            return None
        active_name = known.value.strip()
        if not active_name or active_name == "UNKNOWN":
            return None
        return active_name

    def _staged_mega_event(self, side: MegaSide) -> _ResultEventDraft | None:
        target_side = "self" if side is MegaSide.SELF else "opponent"
        return next(
            (
                event
                for event in self._result_events
                if event.kind == "mega" and event.target_side == target_side
            ),
            None,
        )

    def _render_mega_controls(self, persistence_reads_allowed: bool = True) -> None:
        """Render controls from persisted resource state plus draft events."""

        if not hasattr(self, "mega_result_group"):
            return
        try:
            persisted = self._bundle_c_controller.mega_battle_state()
        except (DomainError, KeyError, ValueError, RuntimeError):
            for button in (self.self_mega_button, self.opponent_mega_button):
                button.setChecked(False)
                button.setEnabled(False)
            return

        for side, button in (
            (MegaSide.SELF, self.self_mega_button),
            (MegaSide.OPPONENT, self.opponent_mega_button),
        ):
            staged = self._staged_mega_event(side)
            already_used = persisted.side(side).mega_used
            button.setChecked(staged is not None)
            button.setEnabled(
                self._result_entry_active
                and persistence_reads_allowed
                and not already_used
                and (staged is not None or self._current_result_active_for_mega(side) is not None)
            )

    def _toggle_mega_event(self, side: MegaSide) -> None:
        """Stage/cancel one explicit actual Mega confirmation for this Turn."""

        if not self._result_entry_active:
            return
        try:
            persisted = self._bundle_c_controller.mega_battle_state()
        except (DomainError, KeyError, ValueError, RuntimeError):
            self._render_mega_controls(False)
            return
        if persisted.side(side).mega_used:
            self._render_mega_controls()
            return

        existing = self._staged_mega_event(side)
        if existing is not None:
            self._result_events.remove(existing)
            self._project_result_events()
            self._render_mega_controls()
            return

        active_name = self._current_result_active_for_mega(side)
        if active_name is None:
            self._result_validation_error = (
                "確認済みのActiveがないため、メガ進化を記録できません。"
            )
            self._render_result_summary()
            self._render_mega_controls()
            return

        current_form = deterministic_mega_form(active_name)
        self._result_event_sequence += 1
        self._result_events.append(
            _ResultEventDraft(
                event_id=f"mega-{self._result_event_sequence}",
                target_side="self" if side is MegaSide.SELF else "opponent",
                pokemon_name=active_name,
                kind="mega",
                field_name="mega",
                value=current_form or "MEGA",
                current_form=current_form,
            )
        )
        self._project_result_events()
        self._render_mega_controls()

    def _confirmed_mega_sides(self) -> tuple[MegaSide, ...]:
        """Derive typed Mega confirmations from the canonical Result draft."""

        sides: list[MegaSide] = []
        for event in self._result_events:
            if event.kind == "mega":
                sides.append(
                    MegaSide.SELF if event.target_side == "self" else MegaSide.OPPONENT
                )
        return tuple(sides)

    def _build_move_result_candidates(self) -> None:
        while self.result_candidates_layout.count():
            item = self.result_candidates_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._result_candidate_cards.clear()
        active_names = self._current_result_active_names()
        own_move = self.actual_action_name_box.currentText().strip()
        opponent_move = self.opponent_action_name_input.text().strip()
        own_display = own_move or "—"
        opponent_display = opponent_move or "UNKNOWN"
        self.result_actions_label.setText(
            f"このTurnの行動    自分：{own_display}    相手：{opponent_display}"
        )
        candidates: list[_MoveResultCandidate] = []
        action_specs = (
            ("self", self.actual_action_type_box.currentText(), own_move),
            ("opponent", self.opponent_action_type_box.currentText(), opponent_move),
        )
        for source_side, action_type, move_name in action_specs:
            if action_type != "MOVE" or not move_name:
                continue
            entry = find_effect(move_name)
            if entry is None:
                continue
            if entry.target is EffectTarget.BATTLEFIELD:
                continue
            target_side = source_side
            if entry.target is EffectTarget.OPPONENT:
                target_side = "opponent" if source_side == "self" else "self"
            for effect_index, effect in enumerate(entry.deterministic_effects):
                field_name: str
                value: int | str
                kind: str
                stage = self._stage_effect(effect)
                if stage is not None:
                    field_name, value = stage
                    kind = "stage"
                elif effect in {*MAJOR_STATUS_PRESETS, "もうどく"}:
                    field_name, value, kind = "status", effect, "status"
                else:
                    continue
                candidate = _MoveResultCandidate(
                    candidate_id=f"{source_side}:{entry.id}:{effect_index}",
                    source_side=source_side,
                    source_pokemon=active_names[source_side],
                    source_move=move_name,
                    target_side=target_side,
                    target_pokemon=active_names[target_side],
                    kind=kind,
                    field_name=field_name,
                    value=value,
                    display_effect=effect,
                )
                candidates.append(candidate)
                card = _MoveResultCandidateCard(candidate, self._decide_result_candidate)
                self._result_candidate_cards[candidate.candidate_id] = card
                self.result_candidates_layout.addWidget(card)
        self._result_candidates = tuple(candidates)
        if not candidates:
            self.result_candidates_layout.addWidget(
                QLabel("既存metadataから提示できる候補はありません。必要なら手入力してください。")
            )

    def _decide_result_candidate(
        self, candidate: _MoveResultCandidate, decision: str
    ) -> None:
        self._result_events = [
            event
            for event in self._result_events
            if event.candidate_id != candidate.candidate_id
        ]
        self._result_candidate_decisions[candidate.candidate_id] = decision
        if decision == "OCCURRED":
            self._result_event_sequence += 1
            self._result_events.append(
                _ResultEventDraft(
                    event_id=f"candidate-{self._result_event_sequence}",
                    target_side=candidate.target_side,
                    pokemon_name=candidate.target_pokemon,
                    kind=candidate.kind,
                    field_name=candidate.field_name,
                    value=candidate.value,
                    source_move=candidate.source_move,
                    candidate_id=candidate.candidate_id,
                )
            )
        if not self._project_result_events():
            self._result_events = [
                event
                for event in self._result_events
                if event.candidate_id != candidate.candidate_id
            ]
            self._result_candidate_decisions[candidate.candidate_id] = "UNDECIDED"
            self._project_result_events()
        for candidate_id, card in self._result_candidate_cards.items():
            card.set_decision(self._result_candidate_decisions.get(candidate_id, "UNDECIDED"))

    def _open_manual_result_dialog(self, _checked: bool = False) -> None:
        dialog = _ManualResultDialog(self, add_event=self._add_manual_result_event)
        self._manual_result_dialog = dialog
        dialog.show()

    def _add_manual_result_event(
        self, target_side: str, field_name: str, value: int | str
    ) -> None:
        self._result_event_sequence += 1
        names = self._current_result_active_names()
        event = _ResultEventDraft(
            event_id=f"manual-{self._result_event_sequence}",
            target_side=target_side,
            pokemon_name=names[target_side],
            kind="status" if field_name == "status" else "stage",
            field_name=field_name,
            value=value,
        )
        self._result_events.append(event)
        if not self._project_result_events():
            self._result_events.remove(event)
            self._project_result_events()

    def _result_stage_known_values(self, side: str) -> dict[str, Known[int]]:
        """Return the effective Result Entry draft stages for direct editing.

        Canonical confirmed state remains the baseline. Already-staged result
        events are folded in only for the dialog's next preview, so reopening
        the dialog composes with (rather than duplicates) earlier applies.
        """

        values = dict(self._bundle_c_controller.stage_known_values(side=side))
        totals: dict[str, int] = {}
        for event in self._result_events:
            if event.kind == "stage" and event.target_side == side:
                totals[event.field_name] = totals.get(event.field_name, 0) + int(event.value)
        for field_name, amount in totals.items():
            known = values.get(field_name)
            if known is not None and known.is_confirmed and known.value is not None:
                values[field_name] = Known.confirmed(
                    known.value + amount,
                    provenance_chain=_HUMAN_INPUT,
                )
        return values

    def _apply_direct_result_stage_changes(
        self, pending: dict[str, dict[str, int]]
    ) -> bool:
        """Convert explicit dialog applies into canonical Result Entry events."""

        names = self._current_result_active_names()
        staged: list[_ResultEventDraft] = []
        for side in ("self", "opponent"):
            effective = self._result_stage_known_values(side)
            for field_name, candidate in pending[side].items():
                known = effective.get(field_name)
                if known is None or not known.is_confirmed or known.value is None:
                    return False
                amount = candidate - known.value
                if amount == 0:
                    continue
                self._result_event_sequence += 1
                staged.append(
                    _ResultEventDraft(
                        event_id=f"direct-{self._result_event_sequence}",
                        target_side=side,
                        pokemon_name=names[side],
                        kind="stage",
                        field_name=field_name,
                        value=amount,
                    )
                )
        self._result_events.extend(staged)
        if self._project_result_events():
            return True
        staged_ids = {event.event_id for event in staged}
        self._result_events = [
            event for event in self._result_events if event.event_id not in staged_ids
        ]
        self._project_result_events()
        return False

    def _toggle_faint_event(self, target_side: str) -> None:
        existing = next(
            (
                event
                for event in self._result_events
                if event.kind == "faint" and event.target_side == target_side
            ),
            None,
        )
        if existing is not None:
            self._result_events.remove(existing)
        else:
            self._result_event_sequence += 1
            self._result_events.append(
                _ResultEventDraft(
                    event_id=f"faint-{self._result_event_sequence}",
                    target_side=target_side,
                    pokemon_name=self._current_result_active_names()[target_side],
                    kind="faint",
                    field_name="hp_bucket",
                    value=HpBucket.ZERO.value,
                )
            )
        self._project_result_events()

    def _project_result_events(self) -> bool:
        stage_totals: dict[tuple[str, str], int] = {}
        for event in self._result_events:
            if event.kind == "stage":
                key = (event.target_side, event.field_name)
                stage_totals[key] = stage_totals.get(key, 0) + int(event.value)
        stage_values: dict[tuple[str, str], int] = {}
        for (side, field_name), amount in stage_totals.items():
            known = self._bundle_c_controller.stage_known_values(side=side).get(field_name)
            if known is None or not known.is_confirmed or known.value is None:
                self._result_validation_error = "現在ランク不明のため能力変化を追加できません。"
                self._render_result_summary()
                return False
            candidate_value = known.value + amount
            if candidate_value < MIN_STAGE or candidate_value > MAX_STAGE:
                self._result_validation_error = "能力ランクが -6～+6 の範囲を超えます。"
                self._render_result_summary()
                return False
            stage_values[(side, field_name)] = candidate_value

        self._result_validation_error = ""
        self.self_delta_editor.reset()
        self.opponent_delta_editor.reset()

        for editor in (self.self_delta_editor, self.opponent_delta_editor):
            editor.hp_field.mode_box.setCurrentText("UNKNOWN")
            editor.status_field.mode_box.setCurrentText("UNKNOWN")
            editor.side_effects_field.mode_box.setCurrentText("UNKNOWN")
            for field in editor.stage_fields.values():
                field.mode_box.setCurrentText("UNKNOWN")
        self.weather_delta_field.mode_box.setCurrentText("UNKNOWN")
        self.terrain_delta_field.mode_box.setCurrentText("UNKNOWN")

        for (side, field_name), value in stage_values.items():
            editor = self.self_delta_editor if side == "self" else self.opponent_delta_editor
            field = editor.stage_fields[field_name]
            field.mode_box.setCurrentText("CHANGED")
            field.spin.setValue(value)
        for event in self._result_events:
            editor = (
                self.self_delta_editor
                if event.target_side == "self"
                else self.opponent_delta_editor
            )
            if event.kind == "status":
                editor.status_field.mode_box.setCurrentText("CHANGED")
                editor.status_field.line.setText(str(event.value))
            elif event.kind == "faint":
                editor.mark_fainted()
            elif event.kind == "mega":
                # Mega is a match-level resource, not a SideDelta field.
                continue
        for result_candidate in self._result_candidates:
            if (
                self._result_candidate_decisions.get(result_candidate.candidate_id)
                != "DID_NOT_OCCUR"
            ):
                continue
            editor = (
                self.self_delta_editor
                if result_candidate.target_side == "self"
                else self.opponent_delta_editor
            )
            if result_candidate.kind == "stage":
                field = editor.stage_fields[result_candidate.field_name]
                if field.mode_box.currentText() != "CHANGED":
                    field.mode_box.setCurrentText("UNCHANGED")
            elif result_candidate.kind == "status" and (
                editor.status_field.mode_box.currentText() != "CHANGED"
            ):
                editor.status_field.mode_box.setCurrentText("UNCHANGED")
        self.record_self_faint_button.setChecked(
            any(
                event.kind == "faint" and event.target_side == "self"
                for event in self._result_events
            )
        )
        self.record_opponent_faint_button.setChecked(
            any(
                event.kind == "faint" and event.target_side == "opponent"
                for event in self._result_events
            )
        )
        self._render_result_summary()
        return True

    def _render_result_summary(self) -> None:
        while self.result_summary_layout.count() > 1:
            item = self.result_summary_layout.takeAt(1)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.result_summary_empty_label.setVisible(not self._result_events)
        if self._result_validation_error:
            self.result_summary_empty_label.setVisible(True)
            self.result_summary_empty_label.setText(self._result_validation_error)
        else:
            self.result_summary_empty_label.setText("追加イベントなし")
        labels = dict(_DIRECT_STAGE_FIELDS)
        for event in self._result_events:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            if event.kind == "stage":
                detail = f"{labels[event.field_name]} {int(event.value):+d}"
            elif event.kind == "status":
                detail = f"状態：{event.value}"
            elif event.kind == "mega":
                detail = (
                    f"→ {event.current_form}"
                    if event.current_form is not None
                    else "→ メガ進化（形態未確定）"
                )
            else:
                detail = "ひんし"
            source = f"  原因：{event.source_move}" if event.source_move else ""
            label = QLabel(
                f"✓ {_TARGET_SIDE_LABELS[event.target_side]}：{event.pokemon_name} "
                f"{detail}{source}"
            )
            remove_button = QPushButton("取消")
            remove_button.clicked.connect(
                lambda _checked=False, event_id=event.event_id: self._remove_result_event(event_id)
            )
            row_layout.addWidget(label, 1)
            row_layout.addWidget(remove_button)
            self.result_summary_layout.addWidget(row)

    def _remove_result_event(self, event_id: str) -> None:
        removed = next((event for event in self._result_events if event.event_id == event_id), None)
        self._result_events = [event for event in self._result_events if event.event_id != event_id]
        if removed is not None and removed.candidate_id is not None:
            self._result_candidate_decisions[removed.candidate_id] = "UNDECIDED"
            card = self._result_candidate_cards.get(removed.candidate_id)
            if card is not None:
                card.set_decision("UNDECIDED")
        self._project_result_events()

    def _on_result_next_turn(self, _checked: bool = False) -> None:
        if not self._result_entry_active:
            super()._on_next_turn(_checked)
            return
        self._on_record_action()
        if self._bundle_c_controller.refresh().projection.session_state != "TURN_RECORDED":
            self._show_result_entry_page()
            return
        self._result_entry_active = False
        super()._on_next_turn(_checked)
        if self._bundle_c_controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING":
            self._result_events.clear()
            self._result_candidate_decisions.clear()

    def _on_record_opponent_faint(self, _checked: bool = False) -> None:
        """相手ひんし: quick-set the opponent result-delta HP to HpBucket.ZERO.

        Draft-only, exactly like every other control on this surface -- the
        canonical write is still the operator's "行動・結果記録" click, which
        reads ``opponent_delta_editor.to_side_delta()``. From there the
        existing lifecycle (ActionResultDelta -> next confirmed SideState ->
        PokemonLocalMemory) and ``is_confirmed_fainted`` do the rest.
        """

        if self._result_entry_active:
            self._toggle_faint_event("opponent")
        else:
            self.opponent_delta_editor.mark_fainted()

    def _on_record_self_faint(self, _checked: bool = False) -> None:
        """自分ひんし: quick-set the self result-delta HP to HpBucket.ZERO.

        See :meth:`_on_record_opponent_faint`. Once persisted and carried
        into match-local memory, the fainted self Pokemon is excluded from
        legal-switch candidates by the existing
        ``domain.legal_switches.derive_legal_switch_candidates``.
        """

        if self._result_entry_active:
            self._toggle_faint_event("self")
        else:
            self.self_delta_editor.mark_fainted()

    def _on_record_action(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        opponent_type = self.opponent_action_type_box.currentText()
        if opponent_type == "選択してください":
            opponent_type = ""
        if opponent_type in {"NO ACTION", "UNKNOWN"}:
            # Preserve the established persisted unknown/no-action semantic
            # (no typed opponent action) without carrying a stale prior name.
            self.opponent_action_name_input.clear()
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
            self_side_delta = self.self_delta_editor.to_side_delta(
                unobserved_as_unknown=self._result_entry_active
            )
        if opponent_type == "SWITCH" and opponent_name:
            opponent_side_delta = self._bundle_c_controller.compute_confirmed_switch_side_delta(
                side="opponent", destination_pokemon_name=opponent_name
            )
        else:
            opponent_side_delta = self.opponent_delta_editor.to_side_delta(
                unobserved_as_unknown=self._result_entry_active
            )

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
            confirmed_mega_sides=self._confirmed_mega_sides(),
        )
        self.render_view(view)

    # -- Battle Record v5 interaction helpers --------------------------------

    @staticmethod
    def _opponent_entity_id(species: str) -> str:
        return TurnStateFlowController.opponent_entity_id_for_species(species)

    def _render_ability_resolution(self) -> None:
        summary = self._bundle_c_controller.turn_state_summary()
        event = summary.pending_opponent_entry_event
        if event is None:
            self._active_ability_entry_event_id = None
            species = self.opponent_active_input.text().strip()
            pending = self._pending_ocr_ability_confirmation
            if pending is not None and pending[0] != species:
                self._pending_ocr_ability_confirmation = None
                pending = None
            candidates: tuple[str, ...] = ()
            if self._turn_field_has_fresh_ocr_origin("opponent_active"):
                candidates = self._bundle_c_controller.ocr_opponent_entry_ability_candidates(
                    species
                )
            should_ask = bool(candidates) and pending is None
            self._provisional_ability_species = species if should_ask else None
            self.ability_resolution_group.setVisible(should_ask)
            self.parity_ability_card.setVisible(should_ask)
            if should_ask:
                self._set_ability_candidates(candidates)
            return
        self._provisional_ability_species = None
        if event.species_id is None:
            self._bundle_c_controller.handle_opponent_entry_event(event.event_id)
            self._active_ability_entry_event_id = None
            self.ability_resolution_group.setVisible(False)
            self.parity_ability_card.setVisible(False)
            return
        remembered = self._bundle_c_controller.opponent_ability_for_entity(
            event.opponent_entity_id
        )
        candidates = self._bundle_c_controller.opponent_ability_candidates(event.species_name)
        try:
            entry_relevant = species_has_entry_relevant_ability(event.species_name)
        except SpeciesCatalogCoverageError:
            entry_relevant = False
        should_ask = remembered is None and entry_relevant and len(candidates) > 1
        if not should_ask:
            self._bundle_c_controller.handle_opponent_entry_event(event.event_id)
            self._active_ability_entry_event_id = None
        else:
            self._active_ability_entry_event_id = event.event_id
        self.ability_resolution_group.setVisible(should_ask)
        self.parity_ability_card.setVisible(should_ask)
        if not should_ask:
            return
        self._set_ability_candidates(candidates)

    def _set_ability_candidates(self, candidates: tuple[str, ...]) -> None:
        current_items = tuple(
            self.opponent_ability_box.itemText(index)
            for index in range(self.opponent_ability_box.count())
        )
        if current_items != candidates:
            self.opponent_ability_box.clear()
            self.opponent_ability_box.addItems(candidates)
        if tuple(button.text() for button in self.parity_ability_buttons) != candidates:
            for button in self.parity_ability_buttons:
                self.parity_ability_row.removeWidget(button)
                button.deleteLater()
            self.parity_ability_buttons = []
            for index, ability in enumerate(candidates):
                button = QPushButton(ability)
                button.clicked.connect(
                    lambda _checked=False, value=ability: self._confirm_parity_ability(value)
                )
                self.parity_ability_row.insertWidget(index, button)
                self.parity_ability_buttons.append(button)

    def _on_opponent_species_changed(self, species: str) -> None:
        try:
            entity_id = self._opponent_entity_id(species)
        except SpeciesCatalogCoverageError:
            remembered = None
        else:
            remembered = self._bundle_c_controller.opponent_ability_for_entity(entity_id)
        self._render_ability_resolution()
        match_facts = self._render_opponent_intel(species, remembered)
        if hasattr(self, "opponent_action_suggestion_box"):
            self._refresh_opponent_action_assist(
                self._bundle_c_controller.refresh(),
                species=species,
                match_facts=match_facts,
            )

    def _render_opponent_intel(
        self, species: str, remembered: str | None
    ) -> MatchOpponentFacts:
        match_facts = self._bundle_c_controller.opponent_match_facts(
            species, remembered_ability=remembered
        )
        view = build_opponent_intel(
            species=species,
            match_facts=match_facts,
            provider=self._opponent_meta_provider,
        )
        self.opponent_intel_widget.render_intel(view)
        self._refresh_move_autocomplete_boosts(match_facts, view)
        return match_facts

    # -- move autocomplete (Bundle D/E) ---------------------------------------

    @staticmethod
    def _resolve_runtime_intel_bundle() -> RuntimeIntelBundle | None:
        intel_directory = intel_db_directory(resolve_intel_runtime_root())
        try:
            return resolve_runtime_intel_bundle(intel_directory)
        except (GenerationStoreError, OSError, ValueError):
            return None

    @staticmethod
    def _matcher_from_runtime_bundle(bundle: RuntimeIntelBundle | None) -> MoveMatcher:
        if bundle is None:
            return MoveMatcher([])
        return MoveMatcher(list(bundle.catalog_names))

    def _load_move_matcher(self) -> MoveMatcher:
        """Return the matcher pinned to this window's resolved generation."""

        return self._move_matcher_cache

    def _refresh_move_autocomplete_boosts(
        self, match_facts: MatchOpponentFacts, view: OpponentIntelView
    ) -> None:
        popup = getattr(self, "opponent_move_autocomplete", None)
        if popup is None:
            return
        boosts: dict[str, float] = {}
        # Highest boost: moves this exact match/species has already observed.
        for move in match_facts.moves:
            boosts[move] = 100.0
        # Medium boost: current species' population usage, scaled down.
        if view.meta is not None:
            for entry in view.meta.moves:
                if entry.percentage is None:
                    continue
                candidate_boost = float(entry.percentage) / 10.0
                boosts[entry.name] = max(boosts.get(entry.name, 0.0), candidate_boost)
        popup.set_boosts(boosts)

    def _on_confirm_opponent_ability(self, _checked: bool = False) -> None:
        summary = self._bundle_c_controller.turn_state_summary()
        event = summary.pending_opponent_entry_event
        if event is None or event.event_id != self._active_ability_entry_event_id:
            return
        species = event.species_name
        ability = self.opponent_ability_box.currentText()
        confirmed = self._bundle_c_controller.confirm_opponent_ability(
            opponent_entity_id=event.opponent_entity_id,
            species=species,
            ability=ability,
            entry_event_id=event.event_id,
        )
        self._pending_ocr_ability_confirmation = None
        self._provisional_ability_species = None
        self._active_ability_entry_event_id = None
        self.ability_resolution_group.setVisible(False)
        self.parity_ability_card.setVisible(False)
        if confirmed is not None:
            entry = find_effect(confirmed)
            if entry is not None:
                self.review_effect_candidate.propose(entry, prefix=f"相手の{entry.display_name_ja}")
        self.render_view()

    def _on_confirm_legal_switches_selected(self, _checked: bool = False) -> None:
        """Bundle 2: explicit human confirmation of one or more legal switches.

        Never automatic -- reads exactly the operator's current list
        selection. An empty selection here is a no-op (use the dedicated
        "none" button instead, never an implicit empty-selection confirm).
        """

        selected = [item.text() for item in self.legal_switch_list.selectedItems()]
        if not selected:
            return
        view = self._bundle_c_controller.confirm_legal_switches(
            legal_switches=tuple(selected), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
        )
        self.render_view(view)

    def _on_confirm_legal_switches_none(self, _checked: bool = False) -> None:
        """Bundle 2: explicit human confirmation of zero legal switches.

        A separate, deliberate action from simply leaving the list
        unselected -- CONFIRMED_NONE must never be inferred from an empty
        UI selection alone.
        """

        view = self._bundle_c_controller.confirm_legal_switches(
            legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE
        )
        self.render_view(view)

    def _on_self_active_changed_for_legal_switches(self, _text: str = "") -> None:
        """Nudge the legal-switch prefill to refresh when the operator's
        in-progress active-Pokemon choice changes. Display-only -- never
        persists anything; see :meth:`_render_legal_switch_workbench`."""

        self._sync_switch_candidates(_text)
        if self._last_rendered_session_state != "TURN_CAPTURE_PENDING":
            return
        self._render_legal_switch_workbench(self._bundle_c_controller.turn_state_summary())

    def _sync_switch_candidates(self, active_name: str) -> None:
        """Keep the turn-facts switch checkboxes/mirror chips on the
        canonical ``selected_three - active - confirmed-fainted`` derivation.

        Tournament P0 fix. Two separate defects, fixed together:

        1. ``self.switch_checkboxes`` were labeled with the raw
           ``selected_three`` (all three members, including the active
           Pokemon itself) by ``_populate_turn_selection_controls`` -- never
           excluding the current active or a confirmed-fainted member. This
           re-derives via the exact same canonical helper
           (``domain.legal_switches.derive_legal_switch_candidates``, via the
           controller's already-existing ``derive_legal_switch_candidates_
           for_active``) the sibling Legal Switch Confirmation workbench
           already uses -- one canonical derivation, not a second one.
        2. ``self.parity_switch_chips`` (the actual on-screen "交代できる
           ポケモン" buttons in this v5 window) are built exactly once at
           ``__init__`` time from ``checkbox.text()``, which is always empty
           at that point -- QCheckBox has no textChanged signal, so nothing
           ever resynced their label afterward. Every real match therefore
           showed three permanently blank/unlabeled buttons, which is why
           the field operator could never correctly identify -- and always
           ended up confirming zero -- legal switches. This is the only
           place either widget's text is set again after construction.

        Text/visibility are reapplied unconditionally on every call --
        cheap, idempotent, and necessary because
        ``_populate_turn_selection_controls`` (base class) still writes the
        raw, unfiltered ``selected_three`` onto the same checkboxes earlier
        in the same render pass; this must always run after it and win.
        Only the *checked* state is preserved across calls where the
        derived candidate tuple is unchanged, so an unrelated re-render
        (typing in another field) never wipes an in-progress operator
        checkmark.
        """

        if not hasattr(self, "_bundle_c_controller"):
            return
        name = active_name.strip()
        candidates: tuple[str, ...] = ()
        if name:
            try:
                candidates = self._bundle_c_controller.derive_legal_switch_candidates_for_active(
                    name
                )
            except DomainError:
                candidates = ()
        candidates_changed = candidates != self._switch_checkbox_last_candidates
        self._switch_checkbox_last_candidates = candidates
        for index, checkbox in enumerate(self.switch_checkboxes):
            candidate_name = candidates[index] if index < len(candidates) else ""
            checkbox.setText(candidate_name)
            checkbox.setVisible(bool(candidate_name))
            if candidates_changed:
                checkbox.setChecked(False)
        if hasattr(self, "parity_switch_chips"):
            for index, chip in enumerate(self.parity_switch_chips):
                candidate_name = candidates[index] if index < len(candidates) else ""
                chip.setText(candidate_name)
                chip.setVisible(bool(candidate_name))
                if candidates_changed:
                    chip.setChecked(False)

    def _render_legal_switch_workbench(self, summary: TurnStateSummaryView) -> None:
        """R3R1: before CONFIRM TURN FACTS, show an editable CANDIDATE
        PREFILL (never itself a confirmation) for whatever active Pokemon
        is currently displayed; CONFIRM TURN FACTS reads exactly this
        visible/edited selection to persist the final confirmation (see
        ``_on_confirm_turn_facts``). After confirmation, reflect (and allow
        the two buttons below to override) the actual persisted
        ``LegalSwitchConfirmation`` -- never re-derived candidates.

        A new TurnIdentity/binding (session/match/generation/turn/revision --
        even with the identical active Pokemon name) or a fresh active
        choice always replaces whatever was shown before; an unrelated
        same-identity, same-active re-render (e.g. editing another field)
        preserves the operator's own edit.
        """

        confirmation = summary.legal_switch_confirmation
        session_state = self._last_rendered_session_state
        pre_confirm_editable = confirmation is None and session_state == "TURN_CAPTURE_PENDING"
        fresh_prefill = False
        if pre_confirm_editable:
            current_identity = summary.identity
            active_text = self.self_active_box.currentText().strip()
            identity_changed = current_identity != self._legal_switch_prefill_identity
            active_changed = active_text != self._legal_switch_prefill_active_text
            if identity_changed or active_changed:
                self._legal_switch_prefill_identity = current_identity
                self._legal_switch_prefill_active_text = active_text
                fresh_prefill = True
                if not active_text or self.self_active_box.currentIndex() <= 0:
                    self._legal_switch_prefill_candidates = ()
                else:
                    try:
                        self._legal_switch_prefill_candidates = (
                            self._bundle_c_controller.derive_legal_switch_candidates_for_active(
                                active_text
                            )
                        )
                    except DomainError:
                        self._legal_switch_prefill_candidates = ()
            candidates = self._legal_switch_prefill_candidates
        else:
            # Facts are already confirmed (or no Turn/candidate context
            # exists at all) -- ``summary.legal_switch_candidates`` is
            # already canonically re-derived from ``applied.selected_three``
            # / the confirmed active / confirmed-fainted members (see
            # ``TurnStateFlowController.turn_state_summary``), independent
            # of whether a ``LegalSwitchConfirmation`` itself exists yet for
            # this exact binding. Never collapse an unresolved-but-derivable
            # candidate set to an empty list here -- that silently hid a
            # real, non-fainted backline behind a blank workbench, which is
            # the historical defect this method exists to prevent.
            candidates = summary.legal_switch_candidates
            current_identity = summary.identity
            if confirmation is None:
                fresh_prefill = current_identity != self._legal_switch_prefill_identity
                self._legal_switch_prefill_identity = current_identity
            else:
                self._legal_switch_prefill_identity = None
            self._legal_switch_prefill_active_text = None

        previously_selected = {item.text() for item in self.legal_switch_list.selectedItems()}
        self.legal_switch_list.clear()
        for name in candidates:
            self.legal_switch_list.addItem(name)
        if confirmation is not None:
            for index in range(self.legal_switch_list.count()):
                item = self.legal_switch_list.item(index)
                item.setSelected(item.text() in confirmation.legal_switches)
        elif fresh_prefill:
            # A fresh prefill -- either a newly-chosen active pre-confirm, or
            # the first render of a new-identity unresolved binding: every
            # candidate starts selected, since a prefill is not yet a
            # deliberate operator removal of anything.
            for index in range(self.legal_switch_list.count()):
                self.legal_switch_list.item(index).setSelected(True)
        else:
            # Same-binding re-render (e.g. an unrelated field edit) keeps
            # the operator's unfinished selection; a genuinely new
            # candidate set (new TurnIdentity/binding/active) cannot
            # contain it.
            for index in range(self.legal_switch_list.count()):
                item = self.legal_switch_list.item(index)
                item.setSelected(item.text() in previously_selected)

        if confirmation is None:
            self.legal_switch_status_label.setText(
                "未確認 (この一覧のままCONFIRM TURN FACTSで確定します)"
                if pre_confirm_editable
                else "未確認 (UNRESOLVED)"
            )
        elif confirmation.status is LegalSwitchStatus.CONFIRMED_NONE:
            self.legal_switch_status_label.setText("確定: 交代先なし (CONFIRMED_NONE)")
        else:
            names = "、".join(confirmation.legal_switches)
            self.legal_switch_status_label.setText(f"確定 (CONFIRMED_NONEMPTY): {names}")
        self.legal_switch_group.setEnabled(
            session_state in {"TURN_CAPTURE_PENDING", "TURN_REVIEWED"}
        )

    def _open_state_event_dialog(self, context: str) -> None:
        callback = self._apply_review_effect if context == "review" else self._apply_result_effect
        dialog = _StateEventDialog(self, context=context, apply_callback=callback)
        self._state_event_dialog = dialog
        dialog.show()

    def _open_common_state_event_dialog(self, _checked: bool = False) -> None:
        primary_cta = self._bundle_c_controller.refresh().projection.primary_cta
        if primary_cta == "RECORD_ACTUAL_ACTION":
            # The production composition keeps ``review_state_event_button``
            # as the one visible ＋状態変化 entrypoint and hides the duplicate
            # result button.  Route that actual visible button to the direct
            # Action Result stage editor once the lifecycle reaches result
            # entry; its その他/詳細 action still opens the existing effect
            # catalog dialog below.
            self._open_direct_stage_editor_dialog()
            return
        self._open_state_event_dialog("review")

    def _open_direct_stage_editor_dialog(self, _checked: bool = False) -> None:
        """Tournament hotfix primary route for the Action Result draft's
        "＋ 状態変化を記録" -- target + common presets + direct per-stat
        rank buttons are the first visible surface, no catalog search
        required. The pre-existing catalog dialog stays reachable from
        this dialog's own "その他 / 詳細" button."""

        dialog = _DirectStageEditorDialog(
            self,
            self_editor=self.self_delta_editor,
            opponent_editor=self.opponent_delta_editor,
            known_stages_fn=(
                self._result_stage_known_values
                if self._result_entry_active
                else lambda side: self._bundle_c_controller.stage_known_values(side=side)
            ),
            open_legacy=lambda: self._open_state_event_dialog("result"),
            apply_stage_changes=(
                self._apply_direct_result_stage_changes if self._result_entry_active else None
            ),
        )
        self._direct_stage_editor_dialog = dialog
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
        switch_selected = own_type == "SWITCH"
        self.self_switch_target_label.setVisible(switch_selected)
        self.self_switch_target_box.setVisible(switch_selected)
        self.self_switch_unavailable_label.setVisible(
            switch_selected and self.self_switch_target_box.count() == 0
        )
        opponent_type = self.opponent_action_type_box.currentText()
        self.opponent_action_name_input.setVisible(opponent_type in {"MOVE", "SWITCH"})
        opponent_editable = opponent_type in {"MOVE", "SWITCH"}
        self.parity_opponent_action_input.setEnabled(opponent_editable)
        self.opponent_action_suggestion_box.setVisible(opponent_editable)
        if not opponent_editable and self.opponent_action_name_input.text():
            self.opponent_action_name_input.clear()
        self._show_opponent_action_suggestions(opponent_type)

    def _apply_review_effect(self, entry: EffectCatalogEntry) -> None:
        self._apply_effect(entry, source_side="opponent", result_phase=False)

    def _apply_result_effect(self, entry: EffectCatalogEntry) -> None:
        source_side = (
            "opponent"
            if self.opponent_action_type_box.currentText() == "MOVE"
            and find_effect(self.opponent_action_name_input.text()) == entry
            else "self"
        )
        if self._result_entry_active:
            target_side = source_side
            if entry.target is EffectTarget.OPPONENT:
                target_side = "self" if source_side == "opponent" else "opponent"
            names = self._current_result_active_names()
            staged: list[_ResultEventDraft] = []
            for effect in entry.deterministic_effects:
                stage = self._stage_effect(effect)
                if stage is not None:
                    field_name, amount = stage
                    kind = "stage"
                    value: int | str = amount
                elif effect in {*MAJOR_STATUS_PRESETS, "もうどく"}:
                    field_name, value, kind = "status", effect, "status"
                else:
                    continue
                self._result_event_sequence += 1
                staged.append(
                    _ResultEventDraft(
                        event_id=f"catalog-{self._result_event_sequence}",
                        target_side=target_side,
                        pokemon_name=names[target_side],
                        kind=kind,
                        field_name=field_name,
                        value=value,
                        source_move=entry.display_name_ja,
                    )
                )
            if staged:
                self._result_events.extend(staged)
                if not self._project_result_events():
                    staged_ids = {event.event_id for event in staged}
                    self._result_events = [
                        event for event in self._result_events if event.event_id not in staged_ids
                    ]
                    self._project_result_events()
                return
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
