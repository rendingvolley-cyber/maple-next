from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from maple_next.application.match_service import (
    MATCH_EXPORT_SCHEMA_VERSION,
    MatchApplication,
)
from maple_next.application.service import DomainError
from maple_next.domain.enums import (
    ActionType,
    BattleState,
    HpBucket,
    MatchOutcome,
    ResultDisposition,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")
LEGAL_MOVES = ("Protect", "Wave Crash", "Earthquake")
LEGAL_SWITCHES = ("Flutter Mane", "Urshifu")


def build_ready_application(
    tmp_path: Path,
) -> tuple[
    SQLiteRepository,
    MatchApplication,
    MockSelectionAdviceAdapter,
    MockTurnAdviceAdapter,
]:
    repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
    application = MatchApplication(repository, tmp_path / "user-data" / "exports")
    selection_adapter = MockSelectionAdviceAdapter()
    turn_adapter = MockTurnAdviceAdapter()
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    result = selection_adapter.submit(
        application,
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.apply_selection(
        selected_three=SELECTED_THREE,
        lead="Dondozo",
        human_confirmed=True,
    )
    return repository, application, selection_adapter, turn_adapter


def build_selection_open_application(
    tmp_path: Path,
) -> tuple[
    SQLiteRepository,
    MatchApplication,
    MockSelectionAdviceAdapter,
    MockTurnAdviceAdapter,
]:
    """SELECTION_OPEN with facts confirmed but no advice yet.

    REQUEST_SELECTION_ADVICE / provider_send_enabled=True in this state, so
    it lets tests prove a Gemini send button that was genuinely enabled
    before a persistent DB failure becomes fail-closed afterward.
    """

    repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
    application = MatchApplication(repository, tmp_path / "user-data" / "exports")
    selection_adapter = MockSelectionAdviceAdapter()
    turn_adapter = MockTurnAdviceAdapter()
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    return repository, application, selection_adapter, turn_adapter


def session_record_counts(repository: SQLiteRepository, session_id: str) -> dict[str, int]:
    tables = (
        "reviewed_selection_facts",
        "selection_advices",
        "applied_selections",
        "battle_turns",
        "reviewed_turn_facts",
        "turn_advices",
        "recorded_actions",
        "async_jobs",
        "gemini_selection_attempt_ledger",
        "turn_advice_attempt_ledger",
        "match_outcomes",
        "match_exports",
    )
    counts = {
        table: int(
            repository.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        for table in tables
    }
    job_ids = tuple(
        row[0]
        for row in repository.connection.execute(
            "SELECT job_id FROM async_jobs WHERE session_id = ?",
            (session_id,),
        )
    )
    placeholders = ",".join("?" for _ in job_ids) or "NULL"
    counts["async_job_results"] = int(
        repository.connection.execute(
            f"SELECT COUNT(*) FROM async_job_results WHERE job_id IN ({placeholders})",
            job_ids,
        ).fetchone()[0]
    )
    counts["provider_attempt_audits"] = int(
        repository.connection.execute(
            f"SELECT COUNT(*) FROM provider_attempt_audits WHERE job_id IN ({placeholders})",
            job_ids,
        ).fetchone()[0]
    )
    return counts


def test_human_abort_preserves_history_and_releases_active_slot(tmp_path: Path) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application)
    turn_result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="No transport is used by this mock fixture.",
    )
    assert turn_result.disposition is ResultDisposition.APPLIED
    before = repository.load_active_session()
    assert before is not None
    assert before.state is BattleState.TURN_REVIEWED
    counts_before = session_record_counts(repository, before.session_id)
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0

    with pytest.raises(DomainError, match="HUMAN_MATCH_ABORT_CONFIRMATION_REQUIRED"):
        application.abort_match(human_confirmed=False)

    archived = application.abort_match(human_confirmed=True)

    assert archived.session_id == before.session_id
    assert archived.match_id == before.match_id
    assert archived.generation == before.generation
    assert archived.state is BattleState.ABORTED
    assert archived.active_slot is None
    assert repository.load_active_session() is None
    stored = repository.connection.execute(
        "SELECT state, active_slot, battle_revision FROM battle_sessions WHERE session_id = ?",
        (before.session_id,),
    ).fetchone()
    assert tuple(stored) == ("ABORTED", None, before.battle_revision + 1)
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    with pytest.raises(DomainError, match="NO_ACTIVE_MATCH"):
        application.abort_match(human_confirmed=True)
    assert session_record_counts(repository, before.session_id) == counts_before
    repository.close()


def test_abort_transaction_failure_rolls_back_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    before = repository.load_active_session()
    assert before is not None
    original_save_session = repository.save_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise RuntimeError("simulated abort persistence failure")

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    with pytest.raises(RuntimeError, match="simulated abort persistence failure"):
        application.abort_match(human_confirmed=True)

    after = repository.load_active_session()
    assert after is not None
    assert after.session_id == before.session_id
    assert after.match_id == before.match_id
    assert after.state is BattleState.BATTLE_READY
    assert after.active_slot == 1
    assert after.battle_revision == before.battle_revision
    repository.close()


def test_abort_sqlite_operational_error_rolls_back_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    original_save_session = repository.save_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError("database is locked: SECRET_PATH")

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        application.abort_match(human_confirmed=True)

    after = repository.load_active_session()
    assert after is not None
    assert after.session_id == before.session_id
    assert after.match_id == before.match_id
    assert after.state is before.state
    assert after.active_slot == before.active_slot
    assert after.battle_revision == before.battle_revision
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    assert repository.count_actions(before.session_id) == 0
    repository.close()


@pytest.mark.parametrize(
    "sqlite_error",
    (
        sqlite3.OperationalError("database is locked: SECRET_PATH"),
        sqlite3.DatabaseError("malformed database: SECRET_PATH"),
    ),
)
def test_abort_controller_sanitizes_sqlite_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_error: sqlite3.Error,
) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )

    def fail_abort(*, human_confirmed: bool) -> None:
        assert human_confirmed is True
        raise sqlite_error

    monkeypatch.setattr(application, "abort_match", fail_abort)
    view = controller.abort_match(human_confirmed=True)

    assert view.error_message == (
        "stale対戦の終了に失敗しました。canonical stateは変更されていません。"
    )
    assert "SECRET_PATH" not in view.error_message
    assert "database" not in view.error_message
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_abort_controller_preserves_last_safe_view_on_persistent_sqlite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    safe_view = controller.refresh()
    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    sessions_before = repository.count_sessions()
    original_save_session = repository.save_session
    original_load_active_session = repository.load_active_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError(
            "database is locked: SECRET_PATH UPDATE battle_sessions"
        )

    def fail_refresh_read() -> None:
        raise sqlite3.DatabaseError(
            "persistent read failure: SECRET_PATH SELECT battle_sessions"
        )

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    failed = controller.abort_match(human_confirmed=True)

    expected = (
        "stale対戦の終了に失敗しました。データベースを読み込めないため、"
        "直前の安全な画面を維持しています。"
    )
    assert failed == replace(
        safe_view, error_message=expected, persistence_reads_allowed=False
    )
    assert failed.persistence_reads_allowed is False
    assert controller._last_safe_match_view == safe_view
    assert failed.projection == safe_view.projection
    assert failed.session_state == safe_view.session_state == before.state.value
    assert failed.projection.session_id == before.session_id
    assert failed.projection.match_id == before.match_id
    assert failed.battle_revision == before.battle_revision
    assert all(
        forbidden not in failed.error_message
        for forbidden in (
            "DatabaseError",
            "OperationalError",
            "database is locked",
            "SECRET_PATH",
            "SELECT battle_sessions",
            "UPDATE battle_sessions",
            str(repository.database_path),
        )
    )

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after == before
    assert after.active_slot == 1
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == sessions_before

    recovered = controller.refresh()
    assert recovered.projection == safe_view.projection
    assert controller._last_safe_match_view == recovered
    assert session_record_counts(repository, before.session_id) == counts_before
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_abort_controller_without_safe_view_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )

    def fail_abort(*, human_confirmed: bool) -> None:
        assert human_confirmed is True
        raise sqlite3.OperationalError("database is locked: SECRET_PATH")

    def fail_refresh_read() -> None:
        raise sqlite3.DatabaseError("malformed database: SECRET_PATH SELECT")

    monkeypatch.setattr(application, "abort_match", fail_abort)
    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    failed = controller.abort_match(human_confirmed=True)

    assert failed.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert failed.session_state == "PERSISTENCE_UNAVAILABLE"
    assert failed.projection.session_id is None
    assert failed.projection.match_id is None
    assert failed.projection.generation is None
    assert failed.projection.primary_cta_enabled is False
    assert failed.projection.provider_send_enabled is False
    assert failed.projection.secondary_actions == ()
    assert failed.self_team == ()
    assert failed.opponent_team == ()
    assert failed.advice is None
    assert failed.applied_selection is None
    assert "SECRET_PATH" not in failed.error_message
    assert "database" not in failed.error_message.lower()
    assert controller._last_safe_match_view is None
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_refresh_database_error_does_not_replace_last_safe_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    safe_view = controller.refresh()

    def fail_mid_refresh(_session_id: str) -> None:
        raise sqlite3.DatabaseError("SECRET_PATH SELECT match_outcomes")

    monkeypatch.setattr(repository, "get_match_outcome", fail_mid_refresh)
    with pytest.raises(sqlite3.DatabaseError, match="SECRET_PATH"):
        controller.refresh()

    assert controller._last_safe_match_view == safe_view
    assert repository.load_active_session() is not None
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_abort_after_restart_is_fail_safe_and_sanitized(tmp_path: Path) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    session = repository.load_active_session()
    assert session is not None
    application.abort_match(human_confirmed=True)
    database_path = repository.database_path
    export_directory = application.export_directory
    repository.close()

    reopened = SQLiteRepository(database_path)
    restarted = MatchApplication(reopened, export_directory)
    restarted.recover_after_restart()
    controller = MatchFlowController(
        restarted,
        reopened,
        selection_adapter,
        turn_adapter,
    )
    view = controller.refresh()
    assert view.projection.application_mode == "NO_ACTIVE_MATCH"
    assert view.session_state is None
    failed = controller.abort_match(human_confirmed=True)
    assert failed.error_message == "現在、終了対象の対戦はありません。"
    archived = reopened.connection.execute(
        "SELECT state, active_slot FROM battle_sessions WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()
    assert tuple(archived) == ("ABORTED", None)
    reopened.close()


def test_ui_abort_is_human_confirmed_cancel_safe_and_not_automatic(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application)
    turn_result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="UI abort safety fixture.",
    )
    assert turn_result.disposition is ResultDisposition.APPLIED
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()
    abort_button = window.abort_match_button
    assert "終了" in abort_button.text()
    assert "選出" in abort_button.text()
    assert window.match_recovery_group.isVisible()

    with patch.object(application, "abort_match", wraps=application.abort_match) as abort_spy:
        window.render_view()
        controller.refresh()
        QTest.qWait(650)
        qapp.processEvents()
        assert abort_spy.call_count == 0

        dialog = window._build_abort_confirmation_dialog()
        assert dialog.standardButton(dialog.defaultButton()) == QMessageBox.StandardButton.Cancel
        assert dialog.standardButton(dialog.escapeButton()) == QMessageBox.StandardButton.Cancel
        dialog.deleteLater()

        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            abort_button.click()
            qapp.processEvents()
        assert abort_spy.call_count == 0
        still_active = repository.load_active_session()
        assert still_active is not None
        assert still_active.state is BattleState.TURN_REVIEWED

        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            abort_button.click()
            qapp.processEvents()
        assert abort_spy.call_count == 1

    view = controller.refresh()
    assert view.projection.application_mode == "NO_ACTIVE_MATCH"
    assert not window.match_recovery_group.isVisible()
    assert window.import_self_team_button.isVisible()
    assert window.import_self_team_button.isEnabled()
    assert not window.mock_group.isVisible()
    assert not window.advice_group.isVisible()
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    window.close()
    repository.close()


def test_ui_abort_sqlite_failure_is_sanitized_and_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    original_save_session = repository.save_session
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()
    safe_view = controller.refresh()
    original_load_active_session = repository.load_active_session
    old_session_label = window.session_state_label.text()
    old_revision_label = window.battle_revision_label.text()
    old_advice = window.advice_three_label.text()

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError(
            f"database is locked: SECRET_PATH {repository.database_path} "
            "UPDATE battle_sessions"
        )

    def fail_refresh_read() -> None:
        raise sqlite3.DatabaseError(
            f"persistent read failure: SECRET_PATH {repository.database_path} "
            "SELECT battle_sessions"
        )

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    with patch.object(
        QMessageBox,
        "exec",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        window.abort_match_button.click()
        qapp.processEvents()

    expected = (
        "stale対戦の終了に失敗しました。データベースを読み込めないため、"
        "直前の安全な画面を維持しています。"
    )
    assert window.error_label.text() == expected
    assert all(
        forbidden not in window.error_label.text()
        for forbidden in (
            "database is locked",
            "SECRET_PATH",
            "UPDATE battle_sessions",
            "SELECT battle_sessions",
            str(repository.database_path),
        )
    )
    assert window.application_mode_label.text() == safe_view.application_mode
    assert window.session_state_label.text() == old_session_label
    assert window.battle_revision_label.text() == old_revision_label
    assert window.advice_three_label.text() == old_advice
    assert window.match_recovery_group.isVisible()
    assert window.abort_match_button.isVisible()
    assert window.abort_match_button.isEnabled()
    assert not window.import_self_team_button.isVisible()
    assert "NO_ACTIVE_MATCH" not in window.application_mode_label.text()

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    assert repository.count_actions(before.session_id) == 0
    recovered = controller.refresh()
    assert recovered.projection == safe_view.projection
    window.close()
    repository.close()


def confirm_turn(
    application: MatchApplication,
    *,
    note: str = "manual review",
    opponent_hp: HpBucket = HpBucket.EIGHTY_ONE_TO_NINETY,
) -> None:
    application.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=opponent_hp,
        legal_moves=LEGAL_MOVES,
        legal_switches=LEGAL_SWITCHES,
        human_note=note,
        human_confirmed=True,
    )


def record_one_turn(
    application: MatchApplication,
    turn_adapter: MockTurnAdviceAdapter,
) -> None:
    application.start_turn_capture()
    confirm_turn(application)
    result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout before committing.",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
    )


def load_export(record_path: str) -> dict[str, object]:
    return json.loads(Path(record_path).read_text(encoding="utf-8"))


def test_battle_ready_win_requires_human_confirmation(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="HUMAN_MATCH_OUTCOME_CONFIRMATION_REQUIRED"):
        application.end_match(MatchOutcome.WIN, human_confirmed=False)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.BATTLE_READY
    assert after.battle_revision == before.battle_revision
    assert repository.get_match_outcome(after.session_id) is None


def test_battle_ready_confirmed_win_transitions_to_match_ended(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    before = repository.load_active_session()
    assert before is not None

    outcome = application.end_match(MatchOutcome.WIN, human_confirmed=True)
    after = repository.load_active_session()

    assert after is not None
    assert after.state is BattleState.MATCH_ENDED
    assert outcome.outcome is MatchOutcome.WIN
    assert outcome.final_battle_revision == before.battle_revision + 1
    assert after.battle_revision == outcome.final_battle_revision


def test_turn_recorded_confirmed_lose_transitions_to_match_ended(tmp_path: Path) -> None:
    repository, application, _, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)

    outcome = application.end_match(MatchOutcome.LOSE, human_confirmed=True)

    assert outcome.outcome is MatchOutcome.LOSE
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.MATCH_ENDED
    assert repository.count_turns(session.session_id) == 1
    assert repository.count_actions(session.session_id) == 1


@pytest.mark.parametrize("state_setup", ["pending", "reviewed", "advice_pending"])
def test_pending_and_reviewed_states_reject_match_end_without_mutation(
    tmp_path: Path,
    state_setup: str,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.start_turn_capture()
    if state_setup in {"reviewed", "advice_pending"}:
        confirm_turn(application)
    if state_setup == "advice_pending":
        application.request_turn_advice("explicit-pending")
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="MATCH_END_NOT_ALLOWED_IN_CURRENT_STATE"):
        application.end_match(MatchOutcome.WIN, human_confirmed=True)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert repository.get_match_outcome(after.session_id) is None


def test_outcome_is_immutable_and_duplicate_end_does_not_bump_revision(
    tmp_path: Path,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    first = application.end_match(MatchOutcome.WIN, human_confirmed=True)
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="MATCH_OUTCOME_ALREADY_SET"):
        application.end_match(MatchOutcome.LOSE, human_confirmed=True)

    after = repository.load_active_session()
    stored = repository.get_match_outcome(first.session_id)
    assert after is not None
    assert stored is not None
    assert stored.outcome is MatchOutcome.WIN
    assert after.battle_revision == before.battle_revision


def test_match_ended_projection_restores_counts_and_save_cta(tmp_path: Path) -> None:
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )

    view = controller.refresh()

    assert view.session_state == "MATCH_ENDED"
    assert view.primary_cta == "SAVE_MATCH_JSON"
    assert view.outcome == "WIN"
    assert view.turn_count == 1
    assert view.action_count == 1
    assert view.export_path is None


def test_export_occurs_only_after_explicit_save_command(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    export_directory = application.export_directory

    assert not export_directory.exists()
    session = repository.load_active_session()
    assert session is not None
    assert repository.get_match_export(session.session_id) is None

    record = application.export_match()

    assert Path(record.export_path).is_file()
    assert repository.load_active_session().state is BattleState.MATCH_EXPORTED  # type: ignore[union-attr]


def test_export_allowlist_and_schema_for_zero_turn_match(tmp_path: Path) -> None:
    _, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    payload = load_export(record.export_path)

    assert set(payload) == {
        "schema_version",
        "match_id",
        "session_id",
        "generation",
        "outcome",
        "ended_at_utc",
        "final_battle_revision",
        "selection",
        "turns",
        "action_history",
    }
    assert payload["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION
    assert set(payload["selection"]) == {  # type: ignore[arg-type]
        "self_team",
        "opponent_team",
        "selected_three",
        "lead",
    }
    assert payload["turns"] == []
    assert payload["action_history"] == []


def test_export_contains_only_latest_corrected_turn_facts(tmp_path: Path) -> None:
    _, application, _, turn_adapter = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application, note="old snapshot", opponent_hp=HpBucket.FULL)
    confirm_turn(application, note="latest snapshot", opponent_hp=HpBucket.FIFTY_ONE_TO_SIXTY)
    result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout.",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
    )
    application.end_match(MatchOutcome.WIN, human_confirmed=True)

    record = application.export_match()
    payload = load_export(record.export_path)
    turn = payload["turns"][0]  # type: ignore[index]
    reviewed = turn["reviewed_facts"]

    assert reviewed["human_note"] == "latest snapshot"
    assert reviewed["opponent_hp"] == "51-60"
    assert "old snapshot" not in Path(record.export_path).read_text(encoding="utf-8")


def test_turn_export_uses_mock_source_and_action_only_history(tmp_path: Path) -> None:
    _, application, _, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.LOSE, human_confirmed=True)
    record = application.export_match()
    payload = load_export(record.export_path)
    turn = payload["turns"][0]  # type: ignore[index]

    assert turn["advice"]["source_type"] == "MOCK"
    assert turn["actual_action"] == {
        "action_type": "MOVE",
        "action_name": "Wave Crash",
    }
    assert payload["action_history"] == [
        {
            "turn_number": 1,
            "action_type": "MOVE",
            "action_name": "Wave Crash",
            "opponent_action_type": None,
            "opponent_action_name": "",
            "action_order": "UNKNOWN",
        }
    ]


def test_turn_export_includes_model_and_warnings(tmp_path: Path) -> None:
    _, application, _, turn_adapter = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application)
    result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout before committing.",
        warnings=("HP不明のためswitchも検討", "追加warning"),
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
    )
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    payload = load_export(record.export_path)
    turn = payload["turns"][0]  # type: ignore[index]

    assert turn["advice"]["model"] == "mock-dev"
    assert turn["advice"]["warnings"] == ["HP不明のためswitchも検討", "追加warning"]


def test_export_includes_opponent_action_and_action_order(tmp_path: Path) -> None:
    from maple_next.domain.enums import ActionOrder

    _, application, _, turn_adapter = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application)
    result = turn_adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout before committing.",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
        opponent_action_type=ActionType.SWITCH,
        opponent_action_name="Garganacl",
        action_order=ActionOrder.SELF_FIRST,
    )
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    payload = load_export(record.export_path)
    turn = payload["turns"][0]  # type: ignore[index]

    assert turn["self_executed_action"] == {"action_type": "MOVE", "action_name": "Wave Crash"}
    assert turn["opponent_executed_action"] == {
        "action_type": "SWITCH",
        "action_name": "Garganacl",
    }
    assert turn["action_order"] == "SELF_FIRST"
    assert turn["reviewed_facts"]["provenance"] == "HUMAN_CONFIRMED"
    assert turn["advice"]["binding"] == "APPLIED"
    assert turn["advice"]["legality"] == "VALID"

    history_entry = payload["action_history"][0]  # type: ignore[index]
    assert history_entry["opponent_action_type"] == "SWITCH"
    assert history_entry["opponent_action_name"] == "Garganacl"
    assert history_entry["action_order"] == "SELF_FIRST"


def test_export_excludes_internal_and_sensitive_fields(tmp_path: Path) -> None:
    repository, application, _, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    text = Path(record.export_path).read_text(encoding="utf-8")

    forbidden = (
        "request_payload_hash",
        "payload_hash",
        "job_id",
        "turn_facts_id",
        "turn_advice_id",
        "API_KEY",
        "MAPLE_NEXT_DB",
        str(repository.database_path),
    )
    assert all(value not in text for value in forbidden)


def test_atomic_write_failure_keeps_match_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    before = repository.load_active_session()
    assert before is not None

    def fail_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(application, "_atomic_write", fail_write)
    with pytest.raises(DomainError, match="EXPORT_WRITE_FAILED"):
        application.export_match()

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.MATCH_ENDED
    assert after.battle_revision == before.battle_revision
    assert repository.get_match_export(after.session_id) is None


def test_existing_mismatched_file_fails_closed(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    session = repository.load_active_session()
    assert session is not None
    application.export_directory.mkdir(parents=True)
    target = application.export_directory / f"maple-match-{session.match_id}.json"
    target.write_text("not canonical", encoding="utf-8")

    with pytest.raises(DomainError, match="EXPORT_FILE_CONTENT_MISMATCH"):
        application.export_match()

    assert repository.load_active_session().state is BattleState.MATCH_ENDED  # type: ignore[union-attr]
    assert repository.get_match_export(session.session_id) is None


def test_export_is_idempotent_with_same_path_hash_and_one_file(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)

    first = application.export_match()
    before = repository.load_active_session()
    second = application.export_match()
    after = repository.load_active_session()

    assert first.export_path == second.export_path
    assert first.sha256 == second.sha256
    assert len(list(application.export_directory.glob("*.json"))) == 1
    assert before is not None and after is not None
    assert after.battle_revision == before.battle_revision


def test_modified_export_after_success_is_rejected(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    before = repository.load_active_session()
    Path(record.export_path).write_text("tampered", encoding="utf-8")

    with pytest.raises(DomainError, match="EXPORT_HASH_MISMATCH"):
        application.export_match()

    after = repository.load_active_session()
    assert before is not None and after is not None
    assert after.state is BattleState.MATCH_EXPORTED
    assert after.battle_revision == before.battle_revision


def test_restart_restores_match_ended_outcome_counts_and_cta(tmp_path: Path) -> None:
    repository, application, _, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.LOSE, human_confirmed=True)
    database_path = repository.database_path
    export_directory = application.export_directory
    repository.close()

    reopened = SQLiteRepository(database_path)
    restarted = MatchApplication(reopened, export_directory)
    restarted.recover_after_restart()
    controller = MatchFlowController(
        restarted,
        reopened,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    view = controller.refresh()

    assert view.session_state == "MATCH_ENDED"
    assert view.primary_cta == "SAVE_MATCH_JSON"
    assert view.outcome == "LOSE"
    assert view.turn_count == 1
    assert view.action_count == 1
    reopened.close()


def test_restart_restores_export_record_and_new_match_cta(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    database_path = repository.database_path
    export_directory = application.export_directory
    repository.close()

    reopened = SQLiteRepository(database_path)
    restarted = MatchApplication(reopened, export_directory)
    controller = MatchFlowController(
        restarted,
        reopened,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    view = controller.refresh()

    assert view.session_state == "MATCH_EXPORTED"
    assert view.primary_cta == "NEW_MATCH"
    assert view.export_path == record.export_path
    assert view.export_sha256 == record.sha256
    reopened.close()


def test_new_match_requires_export_and_increments_generation_once(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    ended = application.end_match(MatchOutcome.WIN, human_confirmed=True)
    with pytest.raises(DomainError, match="EXPECTED_MATCH_EXPORTED"):
        application.new_match_after_export()
    export = application.export_match()

    new_session = application.new_match_after_export()

    assert new_session.state is BattleState.SELECTION_OPEN
    assert new_session.generation == ended.generation + 1
    assert repository.count_sessions() == 2
    assert repository.get_match_outcome(ended.session_id) == ended
    assert repository.get_match_export(ended.session_id) == export
    with pytest.raises(DomainError, match="EXPECTED_MATCH_EXPORTED"):
        application.new_match_after_export()
    assert repository.count_sessions() == 2


def test_exported_match_rejects_terminal_and_turn_mutations(tmp_path: Path) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="MATCH_OUTCOME_ALREADY_SET"):
        application.end_match(MatchOutcome.LOSE, human_confirmed=True)
    with pytest.raises(DomainError, match="EXPECTED_BATTLE_READY"):
        application.start_turn_capture()
    with pytest.raises(DomainError, match="EXPECTED_TURN_RECORDED"):
        application.next_turn()

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.MATCH_EXPORTED
    assert after.battle_revision == before.battle_revision


def test_export_writes_only_to_injected_user_data_directory(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository-root"
    repository_root.mkdir()
    repository, application, _, _ = build_ready_application(tmp_path)
    before = tuple(repository_root.rglob("*"))
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    record = application.export_match()
    after = tuple(repository_root.rglob("*"))

    assert before == after == ()
    assert Path(record.export_path).is_relative_to(tmp_path / "user-data" / "exports")
    assert not Path(record.export_path).is_relative_to(repository_root)
    repository.close()


def test_ui_requires_two_stage_outcome_then_explicit_save_and_new_match(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(
        application,
        repository,
        selection_adapter,
        turn_adapter,
    )
    window = MatchFlowWindow(controller)

    window.outcome_box.setCurrentText("WIN")
    assert not window.end_match_button.isEnabled()
    window.outcome_confirm_checkbox.setChecked(True)
    assert window.end_match_button.isEnabled()
    window.end_match_button.click()
    qapp.processEvents()
    assert controller.refresh().session_state == "MATCH_ENDED"
    assert window.save_match_button.isVisible()

    window.save_match_button.click()
    qapp.processEvents()
    assert controller.refresh().session_state == "MATCH_EXPORTED"
    assert window.new_match_after_export_button.isVisible()

    window.new_match_after_export_button.click()
    qapp.processEvents()
    assert controller.refresh().session_state == "SELECTION_OPEN"
    window.close()
    repository.close()


def test_mock_adapters_remain_network_free(tmp_path: Path) -> None:
    _, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()

    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0


_TRIPLE_FAILURE_FORBIDDEN_TOKENS = (
    "Traceback",
    "DatabaseError",
    "OperationalError",
    "database is locked",
    "persistent refresh failure",
    "renderer status read",
    "SECRET_PATH",
    "SELECT",
    "UPDATE",
)


def test_ui_abort_triple_db_failure_renders_without_further_persistence_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #31: fallback rendering must not perform a third DB read.

    Reproduces the reported Qt slot path exactly:
    ``_on_abort_match`` -> ``abort_match`` (transaction failure) ->
    ``refresh`` (canonical read failure) -> cached fallback view render ->
    (bug) ``gemini_selection_attempt_consumed`` -> a third DB read that used
    to leak a raw ``sqlite3.DatabaseError`` into the Qt slot.
    """

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    old_session_label = window.session_state_label.text()
    old_revision_label = window.battle_revision_label.text()
    old_advice_label = window.advice_three_label.text()

    original_save_session = repository.save_session
    original_load_active_session = repository.load_active_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError(
            "database is locked: SECRET_PATH UPDATE battle_sessions"
        )

    load_call_count = 0

    def load_active_session_selective_failure() -> object:
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 2:
            raise sqlite3.DatabaseError(
                "persistent refresh failure: SECRET_PATH SELECT battle_sessions"
            )
        return original_load_active_session()

    reserved_spy = Mock(
        side_effect=sqlite3.DatabaseError(
            "renderer status read: SECRET_PATH SELECT attempt_ledger"
        )
    )

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", load_active_session_selective_failure)
    monkeypatch.setattr(repository, "gemini_selection_attempt_reserved", reserved_spy)

    with patch.object(
        QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
    ):
        window._on_abort_match()
        qapp.processEvents()

    captured = capsys.readouterr()
    for token in _TRIPLE_FAILURE_FORBIDDEN_TOKENS:
        assert token not in captured.out, token
        assert token not in captured.err, token
        assert token not in window.error_label.text(), token

    assert reserved_spy.call_count == 0
    assert window.isVisible()
    assert window.session_state_label.text() == old_session_label
    assert window.battle_revision_label.text() == old_revision_label
    assert window.advice_three_label.text() == old_advice_label
    assert "NO_ACTIVE_MATCH" not in window.application_mode_label.text()
    assert not window.import_self_team_button.isVisible()
    assert window.match_recovery_group.isVisible()
    assert not window.gemini_send_button.isEnabled()
    assert not window.turn_gemini_send_button.isEnabled()

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    monkeypatch.setattr(repository, "gemini_selection_attempt_reserved", Mock(return_value=False))
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0

    recovered = controller.refresh()
    window.render_view(recovered)
    assert recovered.session_state == before.state.value
    assert recovered.persistence_reads_allowed is True
    window.close()
    repository.close()


def test_normal_render_keeps_durable_gate(tmp_path: Path) -> None:
    """A normal (persistence_reads_allowed=True) render still reads the DB.

    The Selection-integration renderer's status lookup
    (``selection_advice_status`` -> ``application.projection`` ->
    ``repository.load_active_session``) is the durable, restart-safe path
    this fix must not weaken for ordinary rendering.
    """

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)

    view = controller.refresh()
    assert view.persistence_reads_allowed is True

    load_spy = Mock(wraps=repository.load_active_session)
    with patch.object(repository, "load_active_session", load_spy):
        window.render_view(view)

    assert load_spy.call_count >= 1
    window.close()
    repository.close()


def test_cached_fallback_forces_gemini_selection_send_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a previously-send-enabled projection must fail closed once cached."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_selection_open_application(
        tmp_path
    )
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.primary_cta == "REQUEST_SELECTION_ADVICE"
    assert safe_view.projection.provider_send_enabled is True
    assert window.gemini_send_button.isEnabled()

    team_group_visible_before = window.self_team_group.isVisible()

    original_save_session = repository.save_session
    original_load_active_session = repository.load_active_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError("database is locked: SECRET_PATH")

    load_call_count = 0

    def load_active_session_selective_failure() -> object:
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 2:
            raise sqlite3.DatabaseError("persistent refresh failure: SECRET_PATH SELECT")
        return original_load_active_session()

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", load_active_session_selective_failure)

    view = controller.abort_match(human_confirmed=True)
    assert view.persistence_reads_allowed is False
    assert view.projection == safe_view.projection

    window.render_view(view)

    assert not window.gemini_send_button.isEnabled()
    assert not window.turn_gemini_send_button.isEnabled()
    assert window.self_team_group.isVisible() == team_group_visible_before

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    window.close()
    repository.close()


def test_no_cache_persistence_unavailable_view_disables_all_mutation_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    original_load_active_session = repository.load_active_session

    # Simulate "no safe cache has ever been built" without racing construction.
    controller._last_safe_match_view = None

    def fail_abort(*, human_confirmed: bool) -> None:
        assert human_confirmed is True
        raise sqlite3.OperationalError("database is locked: SECRET_PATH")

    def fail_refresh_read() -> object:
        raise sqlite3.DatabaseError("persistent refresh failure: SECRET_PATH SELECT")

    reserved_spy = Mock(
        side_effect=sqlite3.DatabaseError("renderer status read: SECRET_PATH SELECT")
    )
    monkeypatch.setattr(application, "abort_match", fail_abort)
    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    monkeypatch.setattr(repository, "gemini_selection_attempt_reserved", reserved_spy)

    view = controller.abort_match(human_confirmed=True)

    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    assert view.projection.session_id is None
    assert view.projection.match_id is None
    assert view.projection.generation is None
    assert view.self_team == ()
    assert view.opponent_team == ()
    assert view.advice is None
    assert view.applied_selection is None

    window.render_view(view)
    qapp.processEvents()

    captured = capsys.readouterr()
    for token in _TRIPLE_FAILURE_FORBIDDEN_TOKENS:
        assert token not in captured.out, token
        assert token not in captured.err, token
    assert reserved_spy.call_count == 0

    mutation_buttons = (
        window.new_match_button,
        window.confirm_facts_button,
        window.save_self_team_preset_button,
        window.use_self_team_preset_button,
        window.update_self_team_preset_button,
        window.delete_self_team_preset_button,
        window.import_self_team_button,
        window.mock_submit_button,
        window.gemini_send_button,
        window.apply_button,
        window.start_turn_button,
        window.confirm_turn_facts_button,
        window.mock_turn_submit_button,
        window.record_action_button,
        window.next_turn_button,
        window.reconnect_capture_button,
        window.turn_gemini_send_button,
        window.abort_match_button,
        window.end_match_button,
        window.save_match_button,
        window.new_match_after_export_button,
    )
    for button in mutation_buttons:
        assert not button.isEnabled(), button.text()

    for button in mutation_buttons:
        button.click()
    qapp.processEvents()

    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    assert reserved_spy.call_count == 0

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    window.close()
    repository.close()
