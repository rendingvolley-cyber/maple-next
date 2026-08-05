"""A new Selection identity never inherits the previous opponent six."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_roi_window import SelectionRoiMatchFlowWindow


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def test_new_match_clears_stale_opponent_fields_and_unlocks_auto_fill(
    tmp_path: Path,
) -> None:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = SelectionRoiMatchFlowWindow(
        controller,
        ocr_data_directory=tmp_path / "data" / "ocr",
    )
    for index, field in enumerate(window.opponent_team_inputs, start=1):
        field.setText(f"OldOpponent{index}")

    window.new_match_button.click()

    assert [field.text() for field in window.opponent_team_inputs] == [""] * 6
    assert {
        state.origin for state in window._selection_roi_input_states.values()  # noqa: SLF001
    } == {SelectionInputOrigin.EMPTY}
    assert not any(
        state.user_locked
        for state in window._selection_roi_input_states.values()  # noqa: SLF001
    )

    window.close()
    repository.close()
