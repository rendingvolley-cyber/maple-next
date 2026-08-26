from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from maple_next.application.operator_match_service import OperatorMatchApplication
from maple_next.domain.enums import BattleState, MatchOutcome, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow

SELF_TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT_TEAM = ("O1", "O2", "O3", "O4", "O5", "O6")


def _application(tmp_path: Path) -> tuple[
    SQLiteRepository,
    OperatorMatchApplication,
    MockSelectionAdviceAdapter,
    MockTurnAdviceAdapter,
]:
    repository = SQLiteRepository(tmp_path / "runtime.db")
    application = OperatorMatchApplication(repository, tmp_path / "exports")
    selection_adapter = MockSelectionAdviceAdapter()
    turn_adapter = MockTurnAdviceAdapter()
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    result = selection_adapter.submit(
        application,
        selected_three=("A", "B", "C"),
        lead="A",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.apply_selection(
        selected_three=("A", "B", "C"),
        lead="A",
        human_confirmed=True,
    )
    return repository, application, selection_adapter, turn_adapter


def test_exported_match_release_preserves_terminal_identity_and_records(tmp_path: Path) -> None:
    repository, application, _, _ = _application(tmp_path)
    outcome = application.end_match(MatchOutcome.WIN, human_confirmed=True)
    export = application.export_match()
    before = repository.load_active_session()
    assert before is not None
    assert before.state is BattleState.MATCH_EXPORTED
    before_revision = before.battle_revision

    released = application.abort_match(human_confirmed=True)

    assert released.session_id == before.session_id
    assert released.state is BattleState.MATCH_EXPORTED
    assert released.active_slot is None
    assert released.battle_revision == before_revision
    assert repository.load_active_session() is None
    assert repository.get_match_outcome(before.session_id) == outcome
    assert repository.get_match_export(before.session_id) == export
    stored = repository.connection.execute(
        "SELECT state, active_slot, battle_revision FROM battle_sessions WHERE session_id = ?",
        (before.session_id,),
    ).fetchone()
    assert tuple(stored) == ("MATCH_EXPORTED", None, before_revision)
    repository.close()


def test_in_progress_release_keeps_existing_abort_semantics(tmp_path: Path) -> None:
    repository, application, _, _ = _application(tmp_path)
    before = repository.load_active_session()
    assert before is not None
    assert before.state is BattleState.BATTLE_READY

    released = application.abort_match(human_confirmed=True)

    assert released.state is BattleState.ABORTED
    assert released.active_slot is None
    assert released.battle_revision == before.battle_revision + 1
    assert repository.load_active_session() is None
    repository.close()


def test_terminal_release_returns_to_team_prep_and_enables_import(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = _application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    window = MatchFlowWindow(controller, auto_start_capture=False)
    window.show()
    qapp.processEvents()
    assert controller.refresh().session_state == "MATCH_EXPORTED"
    assert not window.import_self_team_button.isEnabled()

    released_view = controller.abort_match(human_confirmed=True)
    window.render_view(released_view)
    qapp.processEvents()

    assert released_view.session_state is None
    assert released_view.application_mode == "NO_ACTIVE_MATCH"
    assert window.import_self_team_button.isEnabled()
    assert window.self_team_preset_box.isEnabled()
    assert repository.load_active_session() is None
    window.close()
    repository.close()
