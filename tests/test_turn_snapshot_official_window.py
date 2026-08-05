"""Official Turn facts confirmation uses one human button, not two controls."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import HpBucket
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.turn_snapshot_official_window import TurnSnapshotMatchFlowWindow

SELF_TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT_TEAM = ("X", "Y", "Z", "U", "V", "W")


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _write_roi_config(root: Path) -> None:
    path = root / "turn" / "config" / "roi_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "contract_version": "maple-turn-roi.v1",
                "canvas_width": 1280,
                "canvas_height": 720,
                "layout": "test-layout",
                "provisional": True,
                "rois": {
                    "self_active": {"x": 10, "y": 10, "width": 100, "height": 20},
                    "opponent_active": {
                        "x": 1170,
                        "y": 10,
                        "width": 100,
                        "height": 20,
                    },
                    "self_hp": {"x": 10, "y": 680, "width": 160, "height": 20},
                    "opponent_hp": {
                        "x": 1110,
                        "y": 40,
                        "width": 160,
                        "height": 20,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, MatchFlowController, TurnSnapshotMatchFlowWindow]:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    ocr_root = tmp_path / "data" / "ocr"
    _write_roi_config(ocr_root)
    window = TurnSnapshotMatchFlowWindow(controller, ocr_data_directory=ocr_root)
    controller.new_match()
    controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    controller.submit_mock_advice(SELF_TEAM[:3], SELF_TEAM[0])
    controller.apply_selection(SELF_TEAM[:3], SELF_TEAM[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()
    return repository, controller, window


def test_official_turn_facts_button_replaces_duplicate_checkbox(tmp_path: Path) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        assert window.turn_facts_confirm_checkbox.isHidden()
        assert window.turn_facts_confirm_checkbox.isChecked() is False
        assert window.confirm_turn_facts_button.text() == "Turn factsを確認して保存"
        assert window.confirm_turn_facts_button.isEnabled()

        window.self_active_box.setCurrentText(SELF_TEAM[0])
        window.opponent_active_input.setText(OPPONENT_TEAM[0])
        window.self_hp_box.setCurrentText(HpBucket.FULL.value)
        window.opponent_hp_box.setCurrentText(HpBucket.FULL.value)
        window.move_inputs[0].setText("Move 1")
        window.switch_checkboxes[1].setChecked(True)

        window.confirm_turn_facts_button.click()

        current = controller.refresh()
        assert current.projection.session_state == "TURN_REVIEWED"
        assert current.projection.primary_cta == "REQUEST_TURN_ADVICE"
        assert current.turn_facts is not None
        assert current.turn_facts.self_active == SELF_TEAM[0]
        assert current.turn_facts.opponent_active == OPPONENT_TEAM[0]
    finally:
        window.close()
        repository.close()
