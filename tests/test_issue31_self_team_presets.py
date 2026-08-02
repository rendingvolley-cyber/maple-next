from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.window import MapleMainWindow

TEAM_ALPHA = ("A", "B", "C", "D", "E", "F")
TEAM_BETA = ("G", "H", "I", "J", "K", "L")
OPPONENT = ("O1", "O2", "O3", "O4", "O5", "O6")


def _controller(repository: SQLiteRepository) -> SelectionFlowController:
    return SelectionFlowController(
        BattleApplication(repository), repository, MockSelectionAdviceAdapter()
    )


def _qt_application() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def test_save_list_use_update_delete_and_restart_last_used(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    repository = SQLiteRepository(database)
    controller = _controller(repository)

    controller.save_self_team_preset("Alpha", TEAM_ALPHA)
    presets = controller.list_self_team_presets()
    assert [(item.name, item.self_team) for item in presets] == [("Alpha", TEAM_ALPHA)]
    preset_id = presets[0].preset_id

    used = controller.use_self_team_preset(preset_id)
    assert used is not None and used.self_team == TEAM_ALPHA
    controller.update_self_team_preset(preset_id, "Alpha updated", TEAM_BETA)
    assert controller.list_self_team_presets()[0].self_team == TEAM_BETA
    repository.close()

    reopened = SQLiteRepository(database)
    restarted = _controller(reopened)
    restored = restarted.last_used_self_team_preset()
    assert restored is not None
    assert restored.name == "Alpha updated"
    assert restored.self_team == TEAM_BETA
    restarted.delete_self_team_preset(restored.preset_id)
    assert restarted.list_self_team_presets() == ()
    assert restarted.last_used_self_team_preset() is None
    reopened.close()


def test_preset_validation_is_sanitized_and_does_not_dispatch(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "runtime.db")
    controller = _controller(repository)
    initial_network_count = controller.network_call_count

    view = controller.save_self_team_preset("", TEAM_ALPHA)
    assert view.error_message == "構築名を入力してください。"
    controller.save_self_team_preset("Alpha", TEAM_ALPHA)
    duplicate = controller.save_self_team_preset(" alpha ", TEAM_BETA)
    assert duplicate.error_message == "同じ名前の構築が既にあります。"
    incomplete = controller.save_self_team_preset("Incomplete", (*TEAM_ALPHA[:5], ""))
    assert incomplete.error_message is not None
    assert controller.network_call_count == initial_network_count == 0
    assert len(controller.list_self_team_presets()) == 1
    repository.close()


def test_confirmed_match_snapshot_survives_preset_update_and_delete(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "runtime.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application, repository, MockSelectionAdviceAdapter()
    )
    application.new_match()
    controller.save_self_team_preset("Alpha", TEAM_ALPHA)
    preset = controller.list_self_team_presets()[0]
    controller.use_self_team_preset(preset.preset_id)
    confirmed = controller.confirm_selection_facts(TEAM_ALPHA, OPPONENT)
    snapshot_id = confirmed.projection.current_reviewed_selection_id
    assert snapshot_id is not None

    controller.update_self_team_preset(preset.preset_id, "Alpha", TEAM_BETA)
    controller.delete_self_team_preset(preset.preset_id)
    snapshot = repository.get_selection_facts(snapshot_id)
    assert snapshot.self_team == TEAM_ALPHA
    assert snapshot.opponent_team == OPPONENT
    repository.close()


def test_window_restores_last_used_team_and_keeps_one_match_manual_edits(
    tmp_path: Path,
) -> None:
    _qt_application()
    database = tmp_path / "runtime.db"
    repository = SQLiteRepository(database)
    controller = _controller(repository)
    controller.save_self_team_preset("Alpha", TEAM_ALPHA)
    controller.use_self_team_preset(controller.list_self_team_presets()[0].preset_id)
    repository.close()

    reopened = SQLiteRepository(database)
    restarted = _controller(reopened)
    window = MapleMainWindow(restarted, auto_start_capture=False)
    assert tuple(field.text() for field in window.self_team_inputs) == TEAM_ALPHA
    window.self_team_inputs[0].setText("manual-for-this-match")
    assert window.self_team_inputs[0].text() == "manual-for-this-match"
    assert restarted.list_self_team_presets()[0].self_team == TEAM_ALPHA
    assert restarted.network_call_count == 0
    window.close()
    reopened.close()
