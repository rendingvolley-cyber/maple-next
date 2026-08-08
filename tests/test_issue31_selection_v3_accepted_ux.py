"""Focused tests for Issue #31 Selection functional UX v3."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QScrollArea

from maple_next.application.match_service import MatchApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.selection_roi.contracts import (
    SelectionCandidateScore,
    SelectionSlotMatch,
)
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Salamence",
    "Garchomp",
    "Dragonite",
    "Flutter Mane",
    "Tyranitar",
    "Pelipper",
)
GEMINI_THREE = SELF_TEAM[:3]


class _SyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)
        except ProviderTransportError as exc:
            self.on_failed(str(exc))
        else:
            self.on_succeeded(result)


def _qt_app() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, TurnStateFlowController, BattleRecordUiWindow]:
    _qt_app()
    repository = SQLiteRepository(tmp_path / "selection-v3.db")
    application = MatchApplication(repository, tmp_path / "export")
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": GEMINI_THREE[0]},
                source_type=GEMINI_SOURCE_TYPE,
                model="fixture-selection-v3",
            )
        ]
    )
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(
            api_key="fixture-only",
            model="fixture-selection-v3",
            timeout_seconds=5.0,
        ),
        dispatch_factory=_SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        adapter,
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        auto_start_capture=False,
    )
    return repository, controller, window


def _fixture_match(slot: int, name: str, color: str) -> SelectionSlotMatch:
    crop = QImage(220, 120, QImage.Format.Format_RGB32)
    crop.fill(QColor(color))
    return SelectionSlotMatch(
        slot=slot,
        crop=crop,
        assigned_label=name,
        assigned_score=0.91,
        top_candidates=(SelectionCandidateScore(name, 0.91, 3),),
    )


def _ready_fake_gemini(
    controller: TurnStateFlowController, window: BattleRecordUiWindow
) -> None:
    controller.new_match()
    window.render_view()
    for slot, name in enumerate(OPPONENT_TEAM, start=1):
        window._set_selection_slot_value(  # noqa: SLF001
            slot,
            name,
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
    for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(name)
    window._on_send_current_selection_to_gemini()  # noqa: SLF001
    window.render_view()


def test_fixed_three_region_layout_has_no_selection_new_match_or_page_scroll(
    tmp_path: Path,
) -> None:
    repository, _controller, window = _build_window(tmp_path)
    try:
        window.header_tabs.setCurrentIndex(0)
        window.show()
        QApplication.processEvents()
        scroll = window.header_tabs.widget(0)
        assert isinstance(scroll, QScrollArea)
        assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        widths = (
            window.selection_v3_left.width(),
            window.selection_v3_center.width(),
            window.selection_v3_right.width(),
        )
        assert widths[1] > widths[2] > widths[0]
        assert not window.new_match_button.isVisible()
        assert len(window.selection_v3_team_name_labels) == 6
        assert len(window.selection_v3_slot_badges) == 6
        assert len(window.selection_v3_actual_buttons) == 6
        assert not window.selection_v3_team_editor.isVisible()
        assert not window.selection_v3_management.isVisible()
    finally:
        window.close()
        repository.close()


def test_fixture_roi_crop_is_rendered_and_missing_crop_is_explicit(tmp_path: Path) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        window.render_view()
        window._render_selection_roi_slot(  # noqa: SLF001
            1, _fixture_match(1, OPPONENT_TEAM[0], "#d04444")
        )
        crop_pixmap = window._selection_roi_thumbnail_labels[1].pixmap()  # noqa: SLF001
        assert crop_pixmap is not None and not crop_pixmap.isNull()
        rendered = crop_pixmap.toImage().pixelColor(crop_pixmap.width() // 2, 1)
        assert rendered.red() > rendered.blue()

        window._render_selection_roi_slot(2, None)  # noqa: SLF001
        missing = window._selection_roi_thumbnail_labels[2]  # noqa: SLF001
        missing_pixmap = missing.pixmap()
        assert missing_pixmap is None or missing_pixmap.isNull()
        assert missing.text() == "ROI crop unavailable"
    finally:
        window.close()
        repository.close()


def test_selection_send_requires_six_human_confirmed_values(tmp_path: Path) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        window.render_view()
        for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
            field.setText(name)
        for slot, name in enumerate(OPPONENT_TEAM, start=1):
            window._set_selection_slot_value(  # noqa: SLF001
                slot,
                name,
                origin=SelectionInputOrigin.OCR_AUTO,
                user_locked=False,
            )
        current = controller.refresh()
        window._update_selection_roi_buttons(current)  # noqa: SLF001
        window._render_selection_v3(current)  # noqa: SLF001
        assert window.selection_v3_confirmed_status.text() == "確認済み 0 / 6"
        assert not window.selection_roi_send_button.isEnabled()

        for slot, name in enumerate(OPPONENT_TEAM, start=1):
            window._set_selection_slot_value(  # noqa: SLF001
                slot,
                name,
                origin=SelectionInputOrigin.MANUAL_TEXT,
                user_locked=True,
            )
        window._update_selection_roi_buttons(current)  # noqa: SLF001
        window._render_selection_v3(current)  # noqa: SLF001
        assert window.selection_v3_confirmed_status.text() == "確認済み 6 / 6"
        assert window.selection_roi_send_button.isEnabled()
    finally:
        window.close()
        repository.close()


def test_gemini_defaults_numbered_selection_and_human_toggle_renumbers(
    tmp_path: Path,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        _ready_fake_gemini(controller, window)
        assert controller.refresh().session_state == "SELECTION_ADVICE_READY"
        assert window._selection_v3_actual_order == list(GEMINI_THREE)  # noqa: SLF001
        assert [button.text().split()[0] for button in window.selection_v3_actual_buttons[:3]] == [
            "1",
            "2",
            "3",
        ]
        assert window.apply_button.isEnabled()
        assert not window.apply_confirm_checkbox.isVisible()
        assert not window.actual_lead_box.isVisible()

        window._toggle_selection_v3_actual(1)  # noqa: SLF001 - human card click
        assert window._selection_v3_actual_order == [SELF_TEAM[0], SELF_TEAM[2]]  # noqa: SLF001
        assert not window.apply_button.isEnabled()
        window._toggle_selection_v3_actual(3)  # noqa: SLF001 - human card click
        assert window._selection_v3_actual_order == [  # noqa: SLF001
            SELF_TEAM[0],
            SELF_TEAM[2],
            SELF_TEAM[3],
        ]
        assert window.selection_v3_actual_buttons[2].text().startswith("2")
        assert window.selection_v3_actual_buttons[3].text().startswith("3")
        assert window.apply_button.isEnabled()

        window._on_apply()  # noqa: SLF001 - explicit human confirmation simulation
        applied = controller.refresh().applied_selection
        assert applied is not None
        assert applied.selected_three == (SELF_TEAM[0], SELF_TEAM[2], SELF_TEAM[3])
        assert applied.lead == SELF_TEAM[0]
    finally:
        window.close()
        repository.close()
