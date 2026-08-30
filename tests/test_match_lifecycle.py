from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

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
from maple_next.ocr.contracts import OcrFieldKey
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import (
    _PERSISTENCE_RESULT_UNKNOWN_MESSAGE,
    MatchFlowController,
    MatchOperatorView,
)
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


def test_abort_success_with_refresh_failure_fails_closed_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed abort followed by a failed refresh is result-unknown."""

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
    original_refresh = controller.refresh
    refresh_failed = True

    def fail_first_refresh() -> MatchOperatorView:
        nonlocal refresh_failed
        if refresh_failed:
            refresh_failed = False
            raise sqlite3.OperationalError(
                "database is locked: SECRET_PATH SELECT battle_sessions"
            )
        return original_refresh()

    monkeypatch.setattr(controller, "refresh", fail_first_refresh)
    view = controller.abort_match(human_confirmed=True)

    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    assert view.projection.session_id is None
    assert view.projection.match_id is None
    assert view.projection.generation is None
    assert view.session_state == "PERSISTENCE_UNAVAILABLE"
    assert view.projection.provider_send_enabled is False
    assert view.projection.secondary_actions == ()
    assert view.error_message == _PERSISTENCE_RESULT_UNKNOWN_MESSAGE
    assert all(
        forbidden not in (view.error_message or "")
        for forbidden in (
            "OperationalError",
            "database is locked",
            "SECRET_PATH",
            "SELECT battle_sessions",
            str(repository.database_path),
        )
    )

    archived = repository.connection.execute(
        "SELECT state, active_slot, battle_revision FROM battle_sessions WHERE session_id = ?",
        (before.session_id,),
    ).fetchone()
    assert tuple(archived) == ("ABORTED", None, before.battle_revision + 1)
    assert repository.load_active_session() is None
    assert repository.count_sessions() == sessions_before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0

    recovered = controller.refresh()
    assert recovered.session_state is None
    assert recovered.persistence_reads_allowed is True
    assert recovered.projection.session_id is None
    assert recovered.projection.match_id is None
    assert recovered.projection.generation is None
    assert controller._last_safe_match_view == recovered
    assert safe_view.projection.session_id == before.session_id
    repository.close()


def test_ui_abort_success_with_refresh_failure_fails_closed_without_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human abort slot must render the sanitized no-cache view."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(
        tmp_path
    )
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)

    with patch("maple_next.ui.window.MapleMainWindow._start_capture"):
        window = MatchFlowWindow(controller)
    window.show()
    window._capture_polling_requested = False  # noqa: SLF001 - keep UI probe capture-free
    qapp.processEvents()
    before = repository.load_active_session()
    assert before is not None
    old_session_label = window.session_state_label.text()
    old_revision_label = window.battle_revision_label.text()
    original_refresh = controller.refresh
    refresh_failed = True

    def fail_first_refresh() -> MatchOperatorView:
        nonlocal refresh_failed
        if refresh_failed:
            refresh_failed = False
            raise sqlite3.OperationalError(
                "database is locked: SECRET_PATH SELECT battle_sessions"
            )
        return original_refresh()

    monkeypatch.setattr(controller, "refresh", fail_first_refresh)
    capture_spy = Mock(side_effect=AssertionError("capture must not be polled"))
    ocr_spy = Mock(side_effect=AssertionError("OCR must not be requested"))
    monkeypatch.setattr(window._capture_service, "latest_snapshot", capture_spy)
    monkeypatch.setattr(
        window._ocr_service,
        "request_candidates_from_capture_status",
        ocr_spy,
    )

    try:
        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
        ):
            window._on_abort_match()
            qapp.processEvents()

        captured = capsys.readouterr()
        for forbidden in (
            "Traceback",
            "OperationalError",
            "database is locked",
            "SECRET_PATH",
            "SELECT battle_sessions",
            str(repository.database_path),
        ):
            assert forbidden not in captured.out
            assert forbidden not in captured.err
            assert forbidden not in window.error_label.text()

        assert window.application_mode_label.text() == "PERSISTENCE_UNAVAILABLE"
        assert window.session_state_label.text() != old_session_label
        assert window.battle_revision_label.text() != old_revision_label
        assert window.match_id_label.text() in {"—", "窶・"}
        assert not window.confirm_facts_button.isEnabled()
        assert not window.gemini_send_button.isEnabled()
        assert not window.record_action_button.isEnabled()
        assert not window.reconnect_capture_button.isEnabled()
        assert not window.end_match_button.isEnabled()
        assert not window.abort_match_button.isEnabled()
        assert capture_spy.call_count == 0
        assert ocr_spy.call_count == 0
        assert selection_adapter.network_call_count == 0
        assert turn_adapter.network_call_count == 0

        archived = repository.connection.execute(
            "SELECT state, active_slot, battle_revision FROM battle_sessions WHERE session_id = ?",
            (before.session_id,),
        ).fetchone()
        assert tuple(archived) == ("ABORTED", None, before.battle_revision + 1)

        recovered = controller.refresh()
        assert recovered.session_state is None
        assert recovered.persistence_reads_allowed is True
        assert recovered.projection.session_id is None
        assert recovered.projection.match_id is None
        assert recovered.projection.generation is None
        window.render_view(recovered)
        qapp.processEvents()
        assert window.application_mode_label.text() != "PERSISTENCE_UNAVAILABLE"
        assert window.match_id_label.text() in {"—", "窶・"}
    finally:
        window.close()
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
    # Retrying abort (or any other match mutation) while the DB state cannot
    # be confirmed is no longer allowed: only a durable, successful refresh
    # may re-enable it.
    assert not window.abort_match_button.isEnabled()
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
    window.render_view(recovered)
    assert recovered.persistence_reads_allowed is True
    assert window.abort_match_button.isEnabled()
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


def test_pending_state_rejects_match_end_without_mutation(
    tmp_path: Path,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.start_turn_capture()
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="MATCH_END_NOT_ALLOWED_IN_CURRENT_STATE"):
        application.end_match(MatchOutcome.WIN, human_confirmed=True)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert repository.get_match_outcome(after.session_id) is None


@pytest.mark.parametrize("state_setup", ["reviewed", "advice_pending"])
def test_reviewed_states_allow_explicit_match_end(
    tmp_path: Path,
    state_setup: str,
) -> None:
    repository, application, _, _ = build_ready_application(tmp_path)
    application.start_turn_capture()
    confirm_turn(application)
    if state_setup == "advice_pending":
        application.request_turn_advice("explicit-pending")

    before = repository.load_active_session()
    assert before is not None
    outcome = application.end_match(MatchOutcome.WIN, human_confirmed=True)

    after = repository.load_active_session()
    assert after is not None
    assert outcome.outcome is MatchOutcome.WIN
    assert after.state is BattleState.MATCH_ENDED
    assert after.battle_revision == before.battle_revision + 1
    assert repository.get_match_outcome(after.session_id) is not None


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


def test_names_only_export_preserves_v2_contract_through_full_lifecycle(
    tmp_path: Path,
) -> None:
    fixed_timestamp = datetime(2025, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    uuid_values = iter(
        UUID(f"00000000-0000-0000-0000-{index:012d}") for index in range(1, 15)
    )

    class FixedClock:
        @staticmethod
        def now(_tz: object) -> datetime:
            return fixed_timestamp

    def next_uuid() -> UUID:
        return next(uuid_values)

    repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
    application = MatchApplication(repository, tmp_path / "user-data" / "exports")
    try:
        with (
            patch("maple_next.application.service.uuid4", side_effect=next_uuid),
            patch("maple_next.ui.dev_advice.uuid4", side_effect=next_uuid),
            patch("maple_next.application.match_service.uuid4", side_effect=next_uuid),
            patch("maple_next.application.service.datetime", FixedClock),
            patch("maple_next.persistence.base.datetime", FixedClock),
            patch("maple_next.application.match_service.datetime", FixedClock),
        ):
            application.new_match()
            application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
            selection_adapter = MockSelectionAdviceAdapter()
            selection_result = selection_adapter.submit(
                application,
                selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
                lead="Meowscarada",
            )
            assert selection_result.disposition is ResultDisposition.APPLIED
            application.apply_selection(
                selected_three=SELECTED_THREE,
                lead="Dondozo",
                human_confirmed=True,
            )
            record_one_turn(application, MockTurnAdviceAdapter())
            application.end_match(MatchOutcome.WIN, human_confirmed=True)
            record = application.export_match()

        encoded = Path(record.export_path).read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
        assert json.loads(encoded.decode("utf-8")) == payload
        assert payload["schema_version"] == "maple-match.v2"
        assert record.schema_version == "maple-match.v2"
        expected_payload = {
            "schema_version": "maple-match.v2",
            "match_id": "00000000-0000-0000-0000-000000000002",
            "session_id": "00000000-0000-0000-0000-000000000001",
            "generation": 1,
            "outcome": "WIN",
            "ended_at_utc": fixed_timestamp.isoformat(),
            "final_battle_revision": 9,
            "selection": {
                "self_team": list(SELF_TEAM),
                "opponent_team": list(OPPONENT_TEAM),
                "selected_three": list(SELECTED_THREE),
                "lead": "Dondozo",
            },
            "turns": [
                {
                    "turn_number": 1,
                    "reviewed_facts": {
                        "self_active": "Dondozo",
                        "opponent_active": "Garchomp",
                        "self_hp": "100",
                        "opponent_hp": "81-90",
                        "legal_moves": list(LEGAL_MOVES),
                        "legal_switches": list(LEGAL_SWITCHES),
                        "human_note": "manual review",
                        "provenance": "HUMAN_CONFIRMED",
                        "created_at_utc": fixed_timestamp.isoformat(),
                    },
                    "advice": {
                        "source_type": "MOCK",
                        "model": "mock-dev",
                        "recommended_action_type": "MOVE",
                        "recommended_action_name": "Protect",
                        "opponent_prediction": "Earthquake",
                        "rationale": "Scout before committing.",
                        "warnings": [],
                        "binding": "APPLIED",
                        "legality": "VALID",
                        "created_at_utc": fixed_timestamp.isoformat(),
                    },
                    "self_executed_action": {
                        "action_type": "MOVE",
                        "action_name": "Wave Crash",
                    },
                    "opponent_executed_action": None,
                    "action_order": "UNKNOWN",
                    "recorded_at_utc": fixed_timestamp.isoformat(),
                    "actual_action": {
                        "action_type": "MOVE",
                        "action_name": "Wave Crash",
                    },
                }
            ],
            "action_history": [
                {
                    "turn_number": 1,
                    "action_type": "MOVE",
                    "action_name": "Wave Crash",
                    "opponent_action_type": None,
                    "opponent_action_name": "",
                    "action_order": "UNKNOWN",
                }
            ],
        }
        assert payload == expected_payload
        assert all(
            detailed_field not in json.dumps(payload, ensure_ascii=False)
            for detailed_field in (
                "self_team_build",
                "self_team_build_sha256",
                "selected_three_builds",
                "self_active_build",
                "tera",
            )
        )
        assert record.sha256 == hashlib.sha256(encoded).hexdigest()
        assert encoded == MatchApplication._encode_payload(expected_payload)
        assert hashlib.sha256(encoded).hexdigest() == (
            "628a439f5ccf4597097f23155063056d426c3205b534c3bacd127ec89da2800e"
        )
    finally:
        repository.close()


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

    # Match-specific non-button controls (issue #31 05 REWORK item 17): the
    # outcome combobox and its confirmation checkbox must also fail closed,
    # even though they are not QPushButtons and cannot be exercised via a
    # synthetic .click().
    assert not window.outcome_box.isEnabled()
    assert not window.outcome_confirm_checkbox.isEnabled()

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


# --- Issue #31 (02 REWORK): preset-selection reads must fail closed -----------


def _build_preset_probe_window(
    tmp_path: Path,
    *,
    active_match: bool = True,
) -> tuple[
    SQLiteRepository,
    MatchApplication,
    MatchFlowController,
    MatchFlowWindow,
    QApplication,
]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    if active_match:
        repository, application, selection_adapter, turn_adapter = build_ready_application(
            tmp_path
        )
    else:
        repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
        application = MatchApplication(repository, tmp_path / "user-data" / "exports")
        selection_adapter = MockSelectionAdviceAdapter()
        turn_adapter = MockTurnAdviceAdapter()
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.save_self_team_preset("Alpha", SELF_TEAM)
    controller.save_self_team_preset("Beta", SELF_TEAM)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()
    assert window.self_team_preset_box.count() == 3
    return repository, application, controller, window, qapp


def _make_no_cache_persistence_unavailable_view(
    application: MatchApplication,
    repository: SQLiteRepository,
    controller: MatchFlowController,
) -> MatchOperatorView:
    controller._last_safe_match_view = None
    with (
        patch.object(
            application,
            "abort_match",
            side_effect=sqlite3.OperationalError("database is locked: SECRET_PATH"),
        ),
        patch.object(
            repository,
            "load_active_session",
            side_effect=sqlite3.DatabaseError("persistent refresh failure: SECRET_PATH"),
        ),
    ):
        view = controller.abort_match(human_confirmed=True)
    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    return view


def test_preset_selection_direct_handler_has_zero_db_reads_in_no_cache_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LunaMAX direct probe: the handler must return before any preset lookup."""

    repository, application, controller, window, qapp = _build_preset_probe_window(tmp_path)
    try:
        selected_index = window.self_team_preset_box.findText("Alpha")
        assert selected_index > 0
        window.self_team_preset_box.setCurrentIndex(selected_index)
        qapp.processEvents()
        name_before = window.self_team_preset_name.text()
        assert name_before == "Alpha"

        fallback_view = _make_no_cache_persistence_unavailable_view(
            application, repository, controller
        )
        controller_list_spy = patch.object(
            controller,
            "list_self_team_presets",
            wraps=controller.list_self_team_presets,
        )
        repository_list_spy = patch.object(
            repository,
            "list_self_team_presets",
            wraps=repository.list_self_team_presets,
        )
        refresh_spy = patch.object(controller, "refresh", wraps=controller.refresh)
        sql_trace: list[str] = []
        repository.connection.set_trace_callback(sql_trace.append)
        try:
            with (
                controller_list_spy as controller_list,
                repository_list_spy as repository_list,
                refresh_spy as refresh,
            ):
                window.render_view(fallback_view)
                qapp.processEvents()
                window._on_self_team_preset_selected(selected_index)
                window._refresh_self_team_presets()
                qapp.processEvents()

                assert controller_list.call_count == 0
                assert repository_list.call_count == 0
                assert refresh.call_count == 0
        finally:
            repository.connection.set_trace_callback(None)

        assert not any("self_team_presets" in statement.lower() for statement in sql_trace)
        assert not any(statement.lstrip().upper().startswith("SELECT") for statement in sql_trace)
        assert window.self_team_preset_name.text() == name_before
        preset_controls = (
            window.self_team_preset_box,
            window.self_team_preset_name,
            window.save_self_team_preset_button,
            window.use_self_team_preset_button,
            window.update_self_team_preset_button,
            window.delete_self_team_preset_button,
            window.import_self_team_button,
        )
        for control in preset_controls:
            assert not control.isEnabled()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        window.close()
        repository.close()


def test_preset_selection_signal_has_zero_db_reads_when_selector_is_disabled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A programmatic currentIndexChanged must still hit the guarded handler."""

    repository, application, controller, window, qapp = _build_preset_probe_window(tmp_path)
    try:
        selected_index = window.self_team_preset_box.findText("Alpha")
        other_index = window.self_team_preset_box.findText("Beta")
        assert selected_index > 0
        assert other_index > 0
        assert selected_index != other_index
        window.self_team_preset_box.setCurrentIndex(selected_index)
        qapp.processEvents()
        name_before = window.self_team_preset_name.text()
        assert name_before == "Alpha"

        fallback_view = _make_no_cache_persistence_unavailable_view(
            application, repository, controller
        )
        window.render_view(fallback_view)
        qapp.processEvents()
        signal_spy = Mock()
        window.self_team_preset_box.currentIndexChanged.connect(signal_spy)
        controller_list_spy = patch.object(
            controller,
            "list_self_team_presets",
            wraps=controller.list_self_team_presets,
        )
        repository_list_spy = patch.object(
            repository,
            "list_self_team_presets",
            wraps=repository.list_self_team_presets,
        )
        refresh_spy = patch.object(controller, "refresh", wraps=controller.refresh)
        sql_trace: list[str] = []
        repository.connection.set_trace_callback(sql_trace.append)
        try:
            with (
                controller_list_spy as controller_list,
                repository_list_spy as repository_list,
                refresh_spy as refresh,
            ):
                window.self_team_preset_box.setCurrentIndex(other_index)
                qapp.processEvents()

                assert signal_spy.call_count == 1
                assert controller_list.call_count == 0
                assert repository_list.call_count == 0
                assert refresh.call_count == 0
        finally:
            repository.connection.set_trace_callback(None)

        assert not any("self_team_presets" in statement.lower() for statement in sql_trace)
        assert not any(statement.lstrip().upper().startswith("SELECT") for statement in sql_trace)
        assert window.self_team_preset_name.text() == name_before
        preset_controls = (
            window.self_team_preset_box,
            window.self_team_preset_name,
            window.save_self_team_preset_button,
            window.use_self_team_preset_button,
            window.update_self_team_preset_button,
            window.delete_self_team_preset_button,
            window.import_self_team_button,
        )
        for control in preset_controls:
            assert not control.isEnabled()

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        window.close()
        repository.close()


def test_preset_controls_reenable_after_persistence_recovery(tmp_path: Path) -> None:
    """Normal NO_ACTIVE_MATCH rendering restores the preset edit contract."""

    repository, _application, controller, window, qapp = _build_preset_probe_window(
        tmp_path, active_match=False
    )
    try:
        selected_index = window.self_team_preset_box.findText("Alpha")
        other_index = window.self_team_preset_box.findText("Beta")
        window.self_team_preset_box.setCurrentIndex(selected_index)
        qapp.processEvents()
        assert window.self_team_preset_name.text() == "Alpha"
        safe_view = controller.refresh()
        assert safe_view.persistence_reads_allowed is True

        cached_fallback = replace(safe_view, persistence_reads_allowed=False)
        window.render_view(cached_fallback)
        qapp.processEvents()
        for control in (
            window.self_team_preset_box,
            window.self_team_preset_name,
            window.save_self_team_preset_button,
            window.use_self_team_preset_button,
            window.update_self_team_preset_button,
            window.delete_self_team_preset_button,
            window.import_self_team_button,
        ):
            assert not control.isEnabled()

        recovered = controller.refresh()
        window.render_view(recovered)
        qapp.processEvents()
        assert recovered.persistence_reads_allowed is True
        assert window.self_team_preset_box.isEnabled()
        assert window.self_team_preset_name.isEnabled()
        assert window.save_self_team_preset_button.isEnabled()
        assert window.use_self_team_preset_button.isEnabled()
        assert window.update_self_team_preset_button.isEnabled()
        assert window.delete_self_team_preset_button.isEnabled()
        assert window.import_self_team_button.isEnabled()

        window.self_team_preset_box.setCurrentIndex(other_index)
        qapp.processEvents()
        assert window.self_team_preset_name.text() == "Beta"
    finally:
        window.close()
        repository.close()


# --- Issue #31 (05 REWORK): MatchFlowWindow-specific fail-closed controls ---
#
# The 05 independent re-verification of PR #39 reproduced a High finding:
# MatchFlowController.abort_match() was the only match-lifecycle command with
# a symmetric sqlite3.Error boundary and the only render path gated on
# OperatorView.persistence_reads_allowed. end_match/save_match_json/
# new_match_after_export caught only (DomainError, RuntimeError), and
# MatchFlowWindow.render_view() re-evaluated outcome/end/save/new-match/abort
# controls purely from session state, so a cached fallback view built from a
# pre-failure endable state (WIN/LOSE already selected and confirmed) could
# re-enable end_match_button and reach an unguarded sqlite3 write from a Qt
# slot. The tests below reproduce that exact scenario and its close cousins.


def test_cached_fallback_disables_match_lifecycle_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 8: the High finding itself, reproduced end-to-end."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.session_state == "TURN_RECORDED"
    window.render_view(safe_view)
    window.outcome_box.setCurrentText(MatchOutcome.WIN.value)
    window.outcome_confirm_checkbox.setChecked(True)
    qapp.processEvents()
    assert window.end_match_button.isEnabled()

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

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", load_active_session_selective_failure)

    fallback_view = controller.abort_match(human_confirmed=True)
    assert fallback_view.persistence_reads_allowed is False
    window.render_view(fallback_view)
    qapp.processEvents()

    # Display may be preserved...
    assert window.outcome_box.currentText() == MatchOutcome.WIN.value
    assert window.outcome_confirm_checkbox.isChecked() is True
    # ...but every control must fail closed.
    assert not window.outcome_box.isEnabled()
    assert not window.outcome_confirm_checkbox.isEnabled()
    assert not window.end_match_button.isEnabled()

    with patch.object(controller, "end_match", wraps=controller.end_match) as end_spy:
        window._on_end_match()
        qapp.processEvents()
        assert end_spy.call_count == 0

        window.end_match_button.click()
        qapp.processEvents()
        assert end_spy.call_count == 0

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.TURN_RECORDED
    assert repository.get_match_outcome(after.session_id) is None
    window.close()
    repository.close()


def test_cached_match_ended_fallback_disables_save(tmp_path: Path) -> None:
    """Section 9: a cached MATCH_ENDED view must fail closed on SAVE."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.session_state == "MATCH_ENDED"
    window.render_view(safe_view)
    assert window.save_match_button.isEnabled()
    outcome_before = window.match_outcome_label.text()
    match_id_before = window.match_id_label.text()
    export_directory = application.export_directory

    fallback_view = replace(
        safe_view,
        error_message=(
            "操作結果をデータベースから確認できません。復旧するまで操作を停止しています。"
        ),
        persistence_reads_allowed=False,
    )
    window.render_view(fallback_view)
    qapp.processEvents()

    assert window.match_outcome_label.text() == outcome_before
    assert window.match_id_label.text() == match_id_before
    assert not window.save_match_button.isEnabled()

    with patch.object(
        controller, "save_match_json", wraps=controller.save_match_json
    ) as save_spy:
        window._on_save_match()
        qapp.processEvents()
        assert save_spy.call_count == 0

        window.save_match_button.click()
        qapp.processEvents()
        assert save_spy.call_count == 0

    assert not export_directory.exists()
    session = repository.load_active_session()
    assert session is not None
    assert repository.get_match_export(session.session_id) is None
    window.close()
    repository.close()


def test_cached_match_exported_fallback_disables_new_match(tmp_path: Path) -> None:
    """Section 10: a cached MATCH_EXPORTED view must fail closed on NEW MATCH."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.session_state == "MATCH_EXPORTED"
    window.render_view(safe_view)
    assert window.new_match_after_export_button.isEnabled()
    export_file_before = window.export_file_label.text()
    export_hash_before = window.export_hash_label.text()

    sessions_before = repository.count_sessions()
    before_session = repository.load_active_session()
    assert before_session is not None

    fallback_view = replace(
        safe_view,
        error_message=(
            "操作結果をデータベースから確認できません。復旧するまで操作を停止しています。"
        ),
        persistence_reads_allowed=False,
    )
    window.render_view(fallback_view)
    qapp.processEvents()

    assert window.export_file_label.text() == export_file_before
    assert window.export_hash_label.text() == export_hash_before
    assert not window.new_match_after_export_button.isEnabled()

    with patch.object(
        controller, "new_match_after_export", wraps=controller.new_match_after_export
    ) as new_match_spy:
        window._on_new_match_after_export()
        qapp.processEvents()
        assert new_match_spy.call_count == 0

        window.new_match_after_export_button.click()
        qapp.processEvents()
        assert new_match_spy.call_count == 0

    after_session = repository.load_active_session()
    assert after_session == before_session
    assert after_session is not None
    assert after_session.active_slot == before_session.active_slot
    assert repository.count_sessions() == sessions_before
    window.close()
    repository.close()


def test_cached_fallback_abort_control_is_disabled_and_dialog_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 11: cached fallback also retracts the previous "abort is
    still retryable" behavior -- no dialog, no controller call, no click
    side effect, until a durable refresh succeeds again."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert "ABORT_MATCH" in safe_view.projection.secondary_actions
    window.render_view(safe_view)
    assert window.abort_match_button.isEnabled()

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

    fallback_view = controller.abort_match(human_confirmed=True)
    assert fallback_view.persistence_reads_allowed is False
    window.render_view(fallback_view)
    qapp.processEvents()

    assert not window.abort_match_button.isEnabled()

    with (
        patch.object(
            window,
            "_build_abort_confirmation_dialog",
            wraps=window._build_abort_confirmation_dialog,
        ) as dialog_spy,
        patch.object(controller, "abort_match", wraps=controller.abort_match) as abort_spy,
    ):
        window._on_abort_match()
        qapp.processEvents()
        assert dialog_spy.call_count == 0
        assert abort_spy.call_count == 0

        window.abort_match_button.click()
        qapp.processEvents()
        assert dialog_spy.call_count == 0
        assert abort_spy.call_count == 0

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    recovered = controller.refresh()
    window.render_view(recovered)
    assert recovered.persistence_reads_allowed is True
    assert "ABORT_MATCH" in recovered.projection.secondary_actions
    assert window.abort_match_button.isEnabled()
    window.close()
    repository.close()


def test_controller_end_match_sqlite_failure_is_sanitized_and_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 12: end_match now has abort_match's symmetric SQLite boundary."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()
    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)

    original_save_session = repository.save_session
    original_load_active_session = repository.load_active_session

    def save_then_fail(session: object) -> None:
        original_save_session(session)  # type: ignore[arg-type]
        raise sqlite3.OperationalError(
            "database is locked: SECRET_PATH UPDATE match_outcomes"
        )

    load_call_count = 0

    def load_active_session_selective_failure() -> object:
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 2:
            raise sqlite3.DatabaseError(
                "persistent refresh: SECRET_PATH SELECT battle_sessions"
            )
        return original_load_active_session()

    monkeypatch.setattr(repository, "save_session", save_then_fail)
    monkeypatch.setattr(repository, "load_active_session", load_active_session_selective_failure)

    view = controller.end_match(MatchOutcome.WIN.value, human_confirmed=True)

    expected = "勝敗の保存に失敗しました。canonical stateは変更されていません。"
    assert view.error_message == expected
    assert view.persistence_reads_allowed is False
    assert view.projection == controller._last_safe_match_view.projection  # type: ignore[union-attr]
    assert all(
        forbidden not in view.error_message
        for forbidden in (
            "OperationalError",
            "DatabaseError",
            "database is locked",
            "SECRET_PATH",
            "SELECT",
            "UPDATE",
        )
    )

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after is not None
    assert after.session_id == before.session_id
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.active_slot == before.active_slot
    assert repository.get_match_outcome(after.session_id) is None
    assert session_record_counts(repository, before.session_id) == counts_before
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_controller_export_sqlite_failure_is_sanitized_and_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 13: export command DB persistence fails after the atomic file
    write already succeeded. The DB row must not exist; whether the JSON
    file itself exists is checked as an observed fact, not assumed."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()
    before = repository.load_active_session()
    assert before is not None
    expected_export_path = application.export_directory / f"maple-match-{before.match_id}.json"

    original_append_match_export = repository.append_match_export
    original_load_active_session = repository.load_active_session

    # export_match() re-validates the active session a second time inside
    # its own transaction (race-condition guard) before calling
    # append_match_export, so a simple call-count trigger on
    # load_active_session would misfire on that internal re-check instead of
    # the post-command refresh. Use a flag set by the actual failure point
    # instead, so every load_active_session call up to and including that
    # internal re-check still succeeds, and only calls strictly after the
    # command has genuinely failed are affected.
    command_failed = False

    def append_then_fail(record: object) -> None:
        nonlocal command_failed
        command_failed = True
        raise sqlite3.OperationalError(
            "database is locked: SECRET_PATH INSERT match_exports"
        )

    def load_active_session_after_command_failure() -> object:
        if command_failed:
            raise sqlite3.DatabaseError(
                "persistent refresh: SECRET_PATH SELECT battle_sessions"
            )
        return original_load_active_session()

    monkeypatch.setattr(repository, "append_match_export", append_then_fail)
    monkeypatch.setattr(
        repository, "load_active_session", load_active_session_after_command_failure
    )

    view = controller.save_match_json()

    expected = "MATCH JSONの保存に失敗しました。MATCH_ENDEDを維持しています。"
    assert view.error_message == expected
    assert view.persistence_reads_allowed is False
    assert all(
        forbidden not in view.error_message
        for forbidden in (
            "OperationalError",
            "DatabaseError",
            "database is locked",
            "SECRET_PATH",
            "SELECT",
            "INSERT",
        )
    )

    # Observed fact (not assumed): MatchApplication.export_match() performs
    # the atomic filesystem write *before* the DB transaction that this test
    # fails, so the JSON file for this attempt does land on disk even though
    # the export DB row never commits. This is safe because a retried export
    # is idempotent: it verifies existing bytes against the recomputed
    # payload instead of blindly rewriting.
    assert expected_export_path.exists(), (
        "expected the atomic write to precede the failed DB transaction"
    )
    no_stray_temp_files = list(application.export_directory.glob(".*.tmp"))
    assert no_stray_temp_files == [], "atomic write must not leave temp files behind"

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.MATCH_ENDED
    assert repository.get_match_export(after.session_id) is None
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0

    monkeypatch.setattr(repository, "append_match_export", original_append_match_export)
    recovered = controller.save_match_json()
    assert recovered.session_state == "MATCH_EXPORTED"
    assert recovered.persistence_reads_allowed is True
    repository.close()


def test_controller_new_match_after_export_sqlite_failure_is_sanitized_and_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 14: NEW MATCH after export fails persistently mid-transaction."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()
    before = repository.load_active_session()
    assert before is not None
    sessions_before = repository.count_sessions()

    original_load_active_session = repository.load_active_session

    def insert_then_fail(session: object) -> None:
        raise sqlite3.OperationalError(
            "database is locked: SECRET_PATH INSERT battle_sessions"
        )

    load_call_count = 0

    def load_active_session_selective_failure() -> object:
        nonlocal load_call_count
        load_call_count += 1
        if load_call_count == 2:
            raise sqlite3.DatabaseError(
                "persistent refresh: SECRET_PATH SELECT battle_sessions"
            )
        return original_load_active_session()

    monkeypatch.setattr(repository, "insert_session", insert_then_fail)
    monkeypatch.setattr(repository, "load_active_session", load_active_session_selective_failure)

    view = controller.new_match_after_export()

    expected = "NEW MATCHの作成に失敗しました。前試合は変更されていません。"
    assert view.error_message == expected
    assert view.persistence_reads_allowed is False
    assert all(
        forbidden not in view.error_message
        for forbidden in (
            "OperationalError",
            "DatabaseError",
            "database is locked",
            "SECRET_PATH",
            "SELECT",
            "INSERT",
        )
    )

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after is not None
    assert after.session_id == before.session_id
    assert after.state is BattleState.MATCH_EXPORTED
    assert after.active_slot == before.active_slot
    assert after.generation == before.generation
    assert repository.count_sessions() == sessions_before
    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_end_match_success_with_refresh_failure_fails_closed_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 15 (end_match): the command really committed, so the returned
    view must not be the stale pre-command safe view -- it must fail closed
    to a no-cache, identity-free PERSISTENCE_UNAVAILABLE view instead."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()

    original_count_turns = repository.count_turns

    def fail_count_turns(session_id: str) -> int:
        raise sqlite3.DatabaseError("SECRET_PATH SELECT battle_turns")

    monkeypatch.setattr(repository, "count_turns", fail_count_turns)

    view = controller.end_match(MatchOutcome.WIN.value, human_confirmed=True)

    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    assert view.projection.session_id is None
    assert view.projection.match_id is None
    assert view.projection.generation is None
    assert view.projection.primary_cta_enabled is False
    assert view.projection.secondary_actions == ()
    assert "SECRET_PATH" not in (view.error_message or "")
    assert "SELECT" not in (view.error_message or "")

    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.MATCH_ENDED
    assert repository.get_match_outcome(session.session_id) is not None

    monkeypatch.setattr(repository, "count_turns", original_count_turns)
    recovered = controller.refresh()
    assert recovered.session_state == "MATCH_ENDED"
    assert recovered.persistence_reads_allowed is True
    repository.close()


def test_export_success_with_refresh_failure_fails_closed_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 15 (save_match_json): export really committed to the DB and
    disk; the returned view must not misrepresent it as still MATCH_ENDED."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()

    original_count_turns = repository.count_turns

    def fail_count_turns(session_id: str) -> int:
        raise sqlite3.DatabaseError("SECRET_PATH SELECT battle_turns")

    monkeypatch.setattr(repository, "count_turns", fail_count_turns)

    view = controller.save_match_json()

    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    assert view.projection.session_id is None
    assert "SECRET_PATH" not in (view.error_message or "")

    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.MATCH_EXPORTED
    assert repository.get_match_export(session.session_id) is not None

    monkeypatch.setattr(repository, "count_turns", original_count_turns)
    recovered = controller.refresh()
    assert recovered.session_state == "MATCH_EXPORTED"
    assert recovered.persistence_reads_allowed is True
    repository.close()


def test_new_match_success_with_refresh_failure_fails_closed_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 15 (new_match_after_export): the new session really exists;
    the returned view must not misrepresent it as still MATCH_EXPORTED."""

    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    controller.refresh()
    sessions_before = repository.count_sessions()

    original_count_turns = repository.count_turns

    def fail_count_turns(session_id: str) -> int:
        raise sqlite3.DatabaseError("SECRET_PATH SELECT battle_turns")

    monkeypatch.setattr(repository, "count_turns", fail_count_turns)

    view = controller.new_match_after_export()

    assert view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert view.persistence_reads_allowed is False
    assert view.projection.session_id is None
    assert "SECRET_PATH" not in (view.error_message or "")

    assert repository.count_sessions() == sessions_before + 1

    monkeypatch.setattr(repository, "count_turns", original_count_turns)
    recovered = controller.refresh()
    assert recovered.session_state == "SELECTION_OPEN"
    assert recovered.persistence_reads_allowed is True
    repository.close()


# --- Issue #31 (05 REWORK / LunaMAX High): direct private-slot invocation ---
#
# The independent re-verification called ``window._on_new_match()`` directly
# (bypassing ``button.click()``) against a PERSISTENCE_UNAVAILABLE render and
# observed the tmp SQLite session count go from 1 to 2 -- proving the fail-
# closed contract only covered "cannot click the button", not "cannot reach
# a mutation through any UI entry point while canonical state is unknown".
# Every private slot that can reach the controller/repository/provider/
# capture/filesystem/dialog now checks ``self._persistence_reads_allowed``
# (via ``_mutation_slots_allowed()`` in the base class) as its first
# statement. The tests below reproduce the exact probe and then generalize
# it into a full slot-matrix regression, both no-cache and cached-fallback.

_ALL_MUTATION_CONTROLLER_METHODS = (
    "new_match",
    "confirm_selection_facts",
    "submit_mock_advice",
    "send_selection_advice_to_gemini",
    "apply_selection",
    "apply_current_gemini_advice",
    "start_turn_capture",
    "confirm_turn_facts",
    "submit_mock_turn_advice",
    "record_actual_action",
    "next_turn",
    "save_self_team_preset",
    "use_self_team_preset",
    "update_self_team_preset",
    "delete_self_team_preset",
    "send_turn_advice_to_gemini",
    "selection_advice_status",
    "end_match",
    "abort_match",
    "save_match_json",
    "new_match_after_export",
)


def _patch_all_controller_methods(
    stack: ExitStack, controller: MatchFlowController
) -> dict[str, Mock]:
    spies: dict[str, Mock] = {}
    for name in _ALL_MUTATION_CONTROLLER_METHODS:
        spy = Mock(side_effect=AssertionError(f"{name} must not be called"))
        stack.enter_context(patch.object(controller, name, spy))
        spies[name] = spy
    return spies


def _invoke_every_direct_mutation_slot(
    window: MatchFlowWindow, qapp: QApplication
) -> None:
    """Directly call every mutation-reaching private slot with safe dummy
    arguments -- the same style of call the LunaMAX probe used, not a
    ``button.click()``."""

    window.mock_selection_boxes[0].setCurrentText("Meowscarada")
    window._on_new_match()
    window._on_confirm_facts()
    window._on_submit_mock()
    window._on_trusted_send_to_gemini()
    window.actual_checkboxes[0].setChecked(True)
    window.apply_confirm_checkbox.setChecked(True)
    window._on_apply()
    window._on_start_turn()
    window.turn_facts_confirm_checkbox.setChecked(True)
    window._on_confirm_turn_facts()
    window._on_submit_mock_turn()
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()
    window._on_next_turn()
    window.self_team_preset_name.setText("dummy preset")
    window._on_save_self_team_preset()
    window._on_use_self_team_preset()
    window._on_update_self_team_preset()
    window._on_delete_self_team_preset()
    window._on_import_self_team()
    window._on_reconnect_capture()
    window._on_adopt_ocr_candidate(OcrFieldKey.SELF_ACTIVE.value)
    window._on_trusted_send_turn_to_gemini()
    window.outcome_box.setCurrentText(MatchOutcome.WIN.value)
    window.outcome_confirm_checkbox.setChecked(True)
    window._on_end_match()
    window._on_abort_match()
    window._on_save_match()
    window._on_new_match_after_export()
    qapp.processEvents()


def test_direct_on_new_match_slot_no_cache_zero_mutation_luna_max_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 7: the exact LunaMAX probe. Before: session count = 1. Direct
    call: ``window._on_new_match()``. After: session count must still = 1."""

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
    assert repository.count_sessions() == 1

    original_load_active_session = repository.load_active_session
    controller._last_safe_match_view = None

    def fail_refresh_read() -> object:
        raise sqlite3.DatabaseError("persistent refresh failure: SECRET_PATH SELECT")

    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    fallback_view = controller.abort_match(human_confirmed=True)
    assert fallback_view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert fallback_view.persistence_reads_allowed is False
    window.render_view(fallback_view)
    qapp.processEvents()

    with patch.object(
        controller, "new_match", wraps=controller.new_match
    ) as new_match_spy:
        window._on_new_match()
        qapp.processEvents()
        assert new_match_spy.call_count == 0

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == 1
    window.close()
    repository.close()


def test_direct_slot_matrix_zero_mutation_no_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Section 8: every mutation-reaching private slot, called directly
    (not via button.click()) against a no-cache PERSISTENCE_UNAVAILABLE
    render, must reach zero controller/DB/provider/filesystem/dialog
    mutation."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    sessions_before = repository.count_sessions()

    original_load_active_session = repository.load_active_session
    controller._last_safe_match_view = None

    def fail_refresh_read() -> object:
        raise sqlite3.DatabaseError("persistent refresh failure: SECRET_PATH SELECT")

    monkeypatch.setattr(repository, "load_active_session", fail_refresh_read)
    fallback_view = controller.abort_match(human_confirmed=True)
    assert fallback_view.application_mode == "PERSISTENCE_UNAVAILABLE"
    assert fallback_view.persistence_reads_allowed is False

    window.render_view(fallback_view)
    qapp.processEvents()

    with (
        ExitStack() as stack,
        patch(
            "maple_next.ui.window.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ) as file_dialog_spy,
        patch(
            "maple_next.ui.match_window.QMessageBox.exec",
            side_effect=AssertionError("dialog must not be shown"),
        ),
    ):
        spies = _patch_all_controller_methods(stack, controller)
        capture_start_spy = stack.enter_context(
            patch.object(window._capture_service, "start")
        )
        capture_stop_spy = stack.enter_context(
            patch.object(window._capture_service, "stop")
        )
        _invoke_every_direct_mutation_slot(window, qapp)
        for name, spy in spies.items():
            assert spy.call_count == 0, name
        assert file_dialog_spy.call_count == 0
        assert capture_start_spy.call_count == 0
        assert capture_stop_spy.call_count == 0
        assert selection_adapter.network_call_count == 0
        assert turn_adapter.network_call_count == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    monkeypatch.setattr(repository, "load_active_session", original_load_active_session)
    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == sessions_before
    window.close()
    repository.close()


def test_direct_slot_matrix_zero_mutation_cached_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Section 9: same matrix, but against a *cached* fallback view that
    still carries an old, "operable-looking" projection (provider send
    enabled, WIN/LOSE already selected+confirmed, ABORT_MATCH available)
    -- exactly the shape of a real pre-failure safe view."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.session_state == "TURN_RECORDED"
    window.render_view(safe_view)
    qapp.processEvents()

    # Populate widgets exactly like a human mid-operation would, while the
    # view was still durable: WIN selected+confirmed, an APPLY-eligible
    # selection ticked, turn facts confirmed, an action confirmed.
    window.outcome_box.setCurrentText(MatchOutcome.WIN.value)
    window.outcome_confirm_checkbox.setChecked(True)
    window.apply_confirm_checkbox.setChecked(True)
    window.turn_facts_confirm_checkbox.setChecked(True)
    window.actual_action_confirm_checkbox.setChecked(True)
    qapp.processEvents()
    assert window.end_match_button.isEnabled()

    before = repository.load_active_session()
    assert before is not None
    counts_before = session_record_counts(repository, before.session_id)
    sessions_before = repository.count_sessions()

    cached_projection = replace(
        safe_view.projection,
        provider_send_enabled=True,
        secondary_actions=("ABORT_MATCH",),
    )
    fallback_view = replace(
        safe_view,
        projection=cached_projection,
        error_message=(
            "操作結果をデータベースから確認できません。復旧するまで操作を停止しています。"
        ),
        persistence_reads_allowed=False,
    )
    window.render_view(fallback_view)
    qapp.processEvents()

    assert window.outcome_box.currentText() == MatchOutcome.WIN.value
    assert window.outcome_confirm_checkbox.isChecked() is True
    assert not window.end_match_button.isEnabled()
    assert not window.abort_match_button.isEnabled()

    with (
        ExitStack() as stack,
        patch(
            "maple_next.ui.window.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ) as file_dialog_spy,
        patch(
            "maple_next.ui.match_window.QMessageBox.exec",
            side_effect=AssertionError("dialog must not be shown"),
        ),
    ):
        spies = _patch_all_controller_methods(stack, controller)
        capture_start_spy = stack.enter_context(
            patch.object(window._capture_service, "start")
        )
        capture_stop_spy = stack.enter_context(
            patch.object(window._capture_service, "stop")
        )
        _invoke_every_direct_mutation_slot(window, qapp)
        for name, spy in spies.items():
            assert spy.call_count == 0, name
        assert file_dialog_spy.call_count == 0
        assert capture_start_spy.call_count == 0
        assert capture_stop_spy.call_count == 0
        assert selection_adapter.network_call_count == 0
        assert turn_adapter.network_call_count == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    after = repository.load_active_session()
    assert after == before
    assert session_record_counts(repository, before.session_id) == counts_before
    assert repository.count_sessions() == sessions_before
    window.close()
    repository.close()


def test_selection_apply_guard_skips_status_read_when_persistence_unavailable(
    tmp_path: Path,
) -> None:
    """Section 3: ``SelectionAdviceIntegrationWindow._on_apply`` must not
    even read ``selection_advice_status()`` while
    ``persistence_reads_allowed`` is False -- that DB-backed read is itself
    forbidden, not only the APPLY mutation it would gate."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    window.render_view(safe_view)
    fallback_view = replace(safe_view, persistence_reads_allowed=False)
    window.render_view(fallback_view)
    qapp.processEvents()

    with (
        patch.object(
            controller, "selection_advice_status"
        ) as status_spy,
        patch.object(controller, "apply_current_gemini_advice") as apply_gemini_spy,
        patch.object(controller, "apply_selection") as apply_spy,
    ):
        window._on_apply()
        qapp.processEvents()
        assert status_spy.call_count == 0
        assert apply_gemini_spy.call_count == 0
        assert apply_spy.call_count == 0

    repository.close()


def test_turn_gemini_send_guard_skips_dispatch_when_persistence_unavailable(
    tmp_path: Path,
) -> None:
    """Section 4: ``TurnAdviceIntegrationWindow._on_trusted_send_turn_to_gemini``
    must not build warnings or reach ``send_turn_advice_to_gemini`` (adapter
    enqueue / transport send / retry / callback registration) while
    ``persistence_reads_allowed`` is False."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    record_one_turn(application, turn_adapter)
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    window.render_view(safe_view)
    fallback_view = replace(safe_view, persistence_reads_allowed=False)
    window.render_view(fallback_view)
    qapp.processEvents()

    with patch.object(controller, "send_turn_advice_to_gemini") as send_spy:
        window._on_trusted_send_turn_to_gemini()
        qapp.processEvents()
        assert send_spy.call_count == 0

    assert selection_adapter.network_call_count == 0
    assert turn_adapter.network_call_count == 0
    repository.close()


def test_normal_view_mutation_slots_still_reach_controller(tmp_path: Path) -> None:
    """Section 10 (guard-scoping check): the guard added above must not
    fire on a normal, durable render -- ``persistence_reads_allowed=True``
    must let the base NEW MATCH slot reach the controller exactly as
    before this change."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qapp = QApplication.instance() or QApplication([])
    repository, application, selection_adapter, turn_adapter = build_ready_application(tmp_path)
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    controller = MatchFlowController(application, repository, selection_adapter, turn_adapter)
    window = MatchFlowWindow(controller)
    window.show()
    qapp.processEvents()

    safe_view = controller.refresh()
    assert safe_view.session_state == "MATCH_EXPORTED"
    window.render_view(safe_view)
    qapp.processEvents()
    assert window._persistence_reads_allowed is True

    sessions_before = repository.count_sessions()
    with patch.object(
        controller, "new_match_after_export", wraps=controller.new_match_after_export
    ) as new_match_spy:
        window._on_new_match_after_export()
        qapp.processEvents()
        assert new_match_spy.call_count == 1

    assert repository.count_sessions() == sessions_before + 1
    window.close()
    repository.close()
