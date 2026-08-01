"""Issue #31 lane "02": durable exactly-one Turn Advice attempt ledger.

Every input to ``decide_turn_advice_dispatch`` (is_current_binding,
has_pending_job, attempt_consumed) must be recomputed from durable
repository state — never from UI in-memory state — so that once the first
production attempt is consumed for a Turn identity ``(session_id, match_id,
generation, turn_number, battle_revision, reviewed_snapshot_id,
canonical request hash)``, every later retry/resend/fallback/second-send,
including a resend after a terminal failure or after a process restart, is
rejected with zero as the total count of additional attempts.

Uses only :class:`~maple_next.providers.turn_transport.FakeTurnAdviceTransport`.
No real network call is ever made anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import HpBucket, JobStatus, JobType, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")
LEGAL_MOVES = ("Protect", "Wave Crash", "Earthquake")
LEGAL_SWITCHES = ("Flutter Mane", "Urshifu")


def build_ready_application(database_path: Path) -> tuple[SQLiteRepository, BattleApplication]:
    repository = SQLiteRepository(database_path)
    application = BattleApplication(repository)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    selection_adapter = MockSelectionAdviceAdapter()
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
    application.start_turn_capture()
    application.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.EIGHTY_ONE_TO_NINETY,
        legal_moves=LEGAL_MOVES,
        legal_switches=LEGAL_SWITCHES,
        human_note="manual review",
        human_confirmed=True,
    )
    return repository, application


def test_immediate_duplicate_request_is_rejected(tmp_path: Path) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")

    job = application.request_turn_advice("human-1")
    assert job.status is JobStatus.QUEUED

    with pytest.raises(DomainError):
        application.request_turn_advice("human-2")

    jobs_for_type = [
        j
        for j in [repository.latest_job_by_type(job.session_id, JobType.TURN_ADVICE)]
        if j is not None
    ]
    assert len(jobs_for_type) == 1
    repository.close()


def test_resend_after_terminal_failure_is_rejected(tmp_path: Path) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")

    job = application.request_turn_advice("human-1")
    application.mark_turn_advice_dispatched(job.job_id)
    application.fail_turn_advice_job(job.job_id, "GEMINI_TIMEOUT")

    failed_job = repository.get_job(job.job_id)
    assert failed_job.status is JobStatus.TIMED_OUT

    # A second attempt for the exact same identity must be rejected even
    # though the first job has already reached a terminal state.
    with pytest.raises(DomainError):
        application.request_turn_advice("human-2")
    repository.close()


def test_resend_after_restart_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repository, application = build_ready_application(database_path)
    application.request_turn_advice("human-1")
    repository.close()

    restarted_repository = SQLiteRepository(database_path)
    restarted_application = BattleApplication(restarted_repository)
    restarted_application.recover_after_restart()

    with pytest.raises(DomainError):
        restarted_application.request_turn_advice("human-after-restart")
    restarted_repository.close()


def test_new_reviewed_facts_round_gets_a_fresh_attempt(tmp_path: Path) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")

    job = application.request_turn_advice("human-1")
    application.mark_turn_advice_dispatched(job.job_id)
    application.fail_turn_advice_job(job.job_id, "GEMINI_TIMEOUT")

    # Re-confirming turn facts creates a genuinely new reviewed_snapshot_id
    # (a new human review round), which is a new Turn identity and must be
    # allowed its own single attempt.
    application.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.FULL,
        legal_moves=LEGAL_MOVES,
        legal_switches=LEGAL_SWITCHES,
        human_note="corrected",
        human_confirmed=True,
    )
    new_job = application.request_turn_advice("human-2")
    assert new_job.status is JobStatus.QUEUED
    assert new_job.job_id != job.job_id
    repository.close()


def test_attempt_consumed_flag_is_recomputed_from_durable_state(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repository, application = build_ready_application(database_path)
    assert application.turn_advice_attempt_consumed() is False

    application.request_turn_advice("human-1")
    assert application.turn_advice_attempt_consumed() is True
    repository.close()

    # A brand-new application/repository instance (simulating a fresh
    # process) must recompute the same durable answer, not default to False.
    restarted_repository = SQLiteRepository(database_path)
    restarted_application = BattleApplication(restarted_repository)
    assert restarted_application.turn_advice_attempt_consumed() is True
    restarted_repository.close()
