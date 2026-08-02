"""Issue #31 pre-match preparation lobby hotfix.

Source of truth: GitHub issue #31 comment 5156988466 (VERDICT: REWORK_REQUIRED,
STATUS: PRE_MATCH_PREPARATION_LOCKED_BEHIND_NEW_MATCH).

``NO_ACTIVE_MATCH`` is a "対戦準備" (pre-match preparation) state: the human
must be able to see/edit their own 6 and run full preset CRUD before any
match exists, while the opponent's 6 and "6体を確認" stay hidden/disabled
until ``SELECTION_OPEN`` (i.e. after NEW MATCH). None of this may create an
active session, dispatch a job, attempt a provider send, or perform a
network/APPLY/turn/game action - preset operations are pure repository CRUD.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.window import MapleMainWindow

TEAM_ALPHA = ("A", "B", "C", "D", "E", "F")
TEAM_BETA = ("G", "H", "I", "J", "K", "L")
OPPONENT = ("O1", "O2", "O3", "O4", "O5", "O6")

_ACTIVITY_TABLES = (
    "battle_sessions",
    "async_jobs",
    "gemini_selection_attempt_ledger",
    "turn_advice_attempt_ledger",
    "provider_attempt_audits",
    "recorded_actions",
    "battle_turns",
)


def _qt_application() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def _build(tmp_path: Path) -> tuple[SQLiteRepository, SelectionFlowController, MapleMainWindow]:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "runtime.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application, repository, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter()
    )
    window = MapleMainWindow(controller, auto_start_capture=False)
    window.show()
    return repository, controller, window


def _assert_zero_activity(repository: SQLiteRepository) -> None:
    for table in _ACTIVITY_TABLES:
        count = repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, f"expected zero rows in {table}, found {count}"


def test_no_active_match_self_team_and_preset_ui_visible_and_enabled(tmp_path: Path) -> None:
    repository, _controller, window = _build(tmp_path)
    try:
        assert window.session_state_label.text() == "—"
        assert window.self_team_group.isVisible()
        assert window.self_team_presets_group.isVisible()
        for field in window.self_team_inputs:
            assert field.isEnabled()
        assert window.save_self_team_preset_button.isEnabled()
    finally:
        window.close()
        repository.close()


def test_no_active_match_opponent_and_confirm_hidden_or_disabled(tmp_path: Path) -> None:
    repository, _controller, window = _build(tmp_path)
    try:
        assert not window.opponent_facts_group.isVisible()
        assert not window.confirm_facts_button.isEnabled()
        for field in window.opponent_team_inputs:
            assert not field.isEnabled()
    finally:
        window.close()
        repository.close()


def test_no_active_match_preset_crud_succeeds_without_activity(tmp_path: Path) -> None:
    repository, controller, window = _build(tmp_path)
    try:
        window.self_team_inputs[0].setText(TEAM_ALPHA[0])
        for field, value in zip(window.self_team_inputs, TEAM_ALPHA, strict=True):
            field.setText(value)
        window.self_team_preset_name.setText("Alpha")
        window.save_self_team_preset_button.click()

        presets = controller.list_self_team_presets()
        assert [(item.name, item.self_team) for item in presets] == [("Alpha", TEAM_ALPHA)]
        preset_id = presets[0].preset_id

        box_index = window.self_team_preset_box.findData(preset_id)
        assert box_index != -1
        window.self_team_preset_box.setCurrentIndex(box_index)
        window.use_self_team_preset_button.click()
        assert tuple(field.text() for field in window.self_team_inputs) == TEAM_ALPHA

        window.self_team_preset_name.setText("Alpha updated")
        for field, value in zip(window.self_team_inputs, TEAM_BETA, strict=True):
            field.setText(value)
        window.update_self_team_preset_button.click()
        assert controller.list_self_team_presets()[0].self_team == TEAM_BETA

        window.delete_self_team_preset_button.click()
        assert controller.list_self_team_presets() == ()

        assert repository.load_active_session() is None
        assert controller.network_call_count == 0
        _assert_zero_activity(repository)
    finally:
        window.close()
        repository.close()


def test_last_used_preset_restores_on_no_active_match_startup(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    repository = SQLiteRepository(database)
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application, repository, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter()
    )
    controller.save_self_team_preset("Alpha", TEAM_ALPHA)
    controller.use_self_team_preset(controller.list_self_team_presets()[0].preset_id)
    repository.close()

    reopened = SQLiteRepository(database)
    reopened_application = BattleApplication(reopened)
    restarted = SelectionFlowController(
        reopened_application, reopened, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter()
    )
    _qt_application()
    window = MapleMainWindow(restarted, auto_start_capture=False)
    try:
        assert reopened.load_active_session() is None
        assert tuple(field.text() for field in window.self_team_inputs) == TEAM_ALPHA
        assert window.self_team_preset_name.text() == "Alpha"
        assert restarted.network_call_count == 0
        _assert_zero_activity(reopened)
    finally:
        window.close()
        reopened.close()


def test_preset_load_then_manual_override_survives_new_match(tmp_path: Path) -> None:
    repository, controller, window = _build(tmp_path)
    try:
        for field, value in zip(window.self_team_inputs, TEAM_ALPHA, strict=True):
            field.setText(value)
        window.self_team_preset_name.setText("Alpha")
        window.save_self_team_preset_button.click()
        preset_id = controller.list_self_team_presets()[0].preset_id
        box_index = window.self_team_preset_box.findData(preset_id)
        window.self_team_preset_box.setCurrentIndex(box_index)
        window.use_self_team_preset_button.click()
        assert tuple(field.text() for field in window.self_team_inputs) == TEAM_ALPHA

        window.self_team_inputs[0].setText("manual-for-this-match")

        window.new_match_button.click()

        assert window.self_team_inputs[0].text() == "manual-for-this-match"
        assert tuple(field.text() for field in window.self_team_inputs)[1:] == TEAM_ALPHA[1:]
        assert window.opponent_facts_group.isVisible()
    finally:
        window.close()
        repository.close()


def test_selection_open_exposes_opponent_inputs_and_confirm(tmp_path: Path) -> None:
    repository, controller, window = _build(tmp_path)
    try:
        window.new_match_button.click()
        assert window.opponent_facts_group.isVisible()
        for field in window.opponent_team_inputs:
            assert field.isEnabled()
        assert window.self_team_group.isVisible()
        for field, value in zip(window.self_team_inputs, TEAM_ALPHA, strict=True):
            field.setText(value)
        for field, value in zip(window.opponent_team_inputs, OPPONENT, strict=True):
            field.setText(value)
        assert window.confirm_facts_button.isEnabled()
        window.confirm_facts_button.click()
        assert controller.refresh().projection.current_reviewed_selection_id is not None
    finally:
        window.close()
        repository.close()


def test_confirmed_match_snapshot_unchanged_by_preset_update_and_delete(tmp_path: Path) -> None:
    repository, controller, window = _build(tmp_path)
    try:
        controller.save_self_team_preset("Alpha", TEAM_ALPHA)
        preset = controller.list_self_team_presets()[0]
        controller.use_self_team_preset(preset.preset_id)

        window.new_match_button.click()
        confirmed = controller.confirm_selection_facts(TEAM_ALPHA, OPPONENT)
        snapshot_id = confirmed.projection.current_reviewed_selection_id
        assert snapshot_id is not None

        controller.update_self_team_preset(preset.preset_id, "Alpha", TEAM_BETA)
        controller.delete_self_team_preset(preset.preset_id)

        snapshot = repository.get_selection_facts(snapshot_id)
        assert snapshot.self_team == TEAM_ALPHA
        assert snapshot.opponent_team == OPPONENT
    finally:
        window.close()
        repository.close()
