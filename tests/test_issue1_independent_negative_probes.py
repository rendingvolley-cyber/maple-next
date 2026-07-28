from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import BattleState, JobStatus, ResultDisposition
from maple_next.domain.models import BattleSession
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

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
ADVICE_THREE = ("Meowscarada", "Gholdengo", "Dragonite")
HUMAN_THREE = ("Dondozo", "Flutter Mane", "Urshifu")


def _setup_request(
    tmp_path: Path,
) -> tuple[SQLiteRepository, BattleApplication, JobEnvelope]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    job = application.request_selection_advice("human-command-1")
    return repository, application, job


def _valid_result(job: JobEnvelope, **changes: object) -> ResultEnvelope:
    result = ResultEnvelope(
        contract_version="maple-worker.v1",
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload={"selected_three": list(ADVICE_THREE), "lead": "Meowscarada"},
    )
    return replace(result, **changes)


def _session_signature(session: BattleSession) -> tuple[object, ...]:
    return (
        session.session_id,
        session.match_id,
        session.generation,
        session.state,
        session.battle_revision,
        session.metadata_revision,
        session.current_reviewed_selection_id,
        session.current_selection_advice_id,
        session.current_applied_selection_id,
        session.current_turn_id,
        session.current_observation_id,
        session.current_reviewed_board_id,
        session.current_turn_advice_id,
    )


def _active_session(repository: SQLiteRepository) -> BattleSession:
    session = repository.load_active_session()
    assert session is not None
    return session


def _row_count(repository: SQLiteRepository, table: str) -> int:
    allowed_tables = {"applied_selections", "selection_advices"}
    assert table in allowed_tables
    row = repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_probe_01_duplicate_result_is_audited_without_second_canonical_update(
    tmp_path: Path,
) -> None:
    repository, application, job = _setup_request(tmp_path)
    result = _valid_result(job)

    assert application.apply_selection_advice_result(result) is ResultDisposition.APPLIED
    after_first = _active_session(repository)
    first_signature = _session_signature(after_first)
    first_advice = repository.get_selection_advice(result.result_id)

    assert (
        application.apply_selection_advice_result(result)
        is ResultDisposition.DUPLICATE_IGNORED
    )
    after_duplicate = _active_session(repository)

    assert _session_signature(after_duplicate) == first_signature
    assert after_duplicate.current_selection_advice_id == result.result_id
    assert repository.get_selection_advice(result.result_id) == first_advice
    assert repository.get_job(job.job_id).status is JobStatus.SUCCEEDED
    assert repository.result_audits(job.job_id) == [
        ("APPLIED", "BINDING_ACCEPTED"),
        ("DUPLICATE_IGNORED", "RESULT_ALREADY_APPLIED"),
    ]


def test_probe_02_changed_job_id_is_stale_without_domain_mutation(tmp_path: Path) -> None:
    repository, application, job = _setup_request(tmp_path)
    before = _session_signature(_active_session(repository))
    result = _valid_result(job, job_id="tampered-job-id")

    assert application.apply_selection_advice_result(result) is ResultDisposition.STALE_REJECTED

    assert _session_signature(_active_session(repository)) == before
    assert repository.get_job(job.job_id).status is JobStatus.QUEUED
    assert repository.result_audits("tampered-job-id") == [
        ("STALE_REJECTED", "JOB_ID_MISMATCH")
    ]
    with pytest.raises(KeyError):
        repository.get_selection_advice(result.result_id)


def test_probe_03_changed_command_id_is_stale_without_domain_mutation(tmp_path: Path) -> None:
    repository, application, job = _setup_request(tmp_path)
    before = _session_signature(_active_session(repository))
    result = _valid_result(job, command_id="tampered-command-id")

    assert application.apply_selection_advice_result(result) is ResultDisposition.STALE_REJECTED

    assert _session_signature(_active_session(repository)) == before
    assert repository.get_job(job.job_id).status is JobStatus.QUEUED
    assert repository.result_audits(job.job_id) == [
        ("STALE_REJECTED", "COMMAND_ID_MISMATCH")
    ]
    with pytest.raises(KeyError):
        repository.get_selection_advice(result.result_id)


def test_probe_04_changed_payload_hash_is_stale_without_domain_mutation(tmp_path: Path) -> None:
    repository, application, job = _setup_request(tmp_path)
    before = _session_signature(_active_session(repository))
    result = _valid_result(job, request_payload_hash="tampered-payload-hash")

    assert application.apply_selection_advice_result(result) is ResultDisposition.STALE_REJECTED

    assert _session_signature(_active_session(repository)) == before
    assert repository.get_job(job.job_id).status is JobStatus.QUEUED
    assert repository.result_audits(job.job_id) == [
        ("STALE_REJECTED", "PAYLOAD_HASH_MISMATCH")
    ]
    with pytest.raises(KeyError):
        repository.get_selection_advice(result.result_id)


def test_probe_05_old_result_is_stale_after_battle_revision_only_advances(
    tmp_path: Path,
) -> None:
    repository, application, job = _setup_request(tmp_path)
    before = _active_session(repository)
    before_metadata_revision = before.metadata_revision
    before_reviewed_selection_id = before.current_reviewed_selection_id

    with repository.transaction():
        current = _active_session(repository)
        current.bump_battle()
        repository.save_session(current)

    advanced = _active_session(repository)
    assert advanced.battle_revision == job.base_battle_revision + 1
    assert advanced.metadata_revision == before_metadata_revision
    assert advanced.current_reviewed_selection_id == before_reviewed_selection_id

    result = _valid_result(job)
    assert application.apply_selection_advice_result(result) is ResultDisposition.STALE_REJECTED

    after = _active_session(repository)
    assert _session_signature(after) == _session_signature(advanced)
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert repository.get_job(job.job_id).status is JobStatus.QUEUED
    assert repository.result_audits(job.job_id) == [
        ("STALE_REJECTED", "BATTLE_REVISION_MISMATCH")
    ]


def test_probe_06_queued_provider_job_restarts_interrupted_without_dispatch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "maple.db"
    repository, _, job = _setup_request(tmp_path)
    before = _session_signature(_active_session(repository))
    assert repository.get_job_dispatch_count(job.job_id) == 0
    repository.close()

    restarted = SQLiteRepository(database_path)
    application = BattleApplication(restarted)
    application.recover_after_restart()

    assert _session_signature(_active_session(restarted)) == before
    assert restarted.get_job(job.job_id).status is JobStatus.INTERRUPTED
    assert restarted.get_job_dispatch_count(job.job_id) == 0
    assert restarted.result_audits(job.job_id) == []  # No result exists in this restart probe.
    projection = application.projection()
    assert projection.provider_send_enabled is True
    assert projection.session_id == job.session_id
    assert projection.match_id == job.match_id
    assert projection.generation == job.generation


def test_probe_07_in_flight_provider_job_becomes_delivery_unknown_and_blocks_send(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "maple.db"
    repository, _, job = _setup_request(tmp_path)
    repository.set_job_status_for_test(job.job_id, JobStatus.IN_FLIGHT)
    before = _session_signature(_active_session(repository))
    assert repository.get_job_dispatch_count(job.job_id) == 0
    repository.close()

    restarted = SQLiteRepository(database_path)
    application = BattleApplication(restarted)
    application.recover_after_restart()

    assert _session_signature(_active_session(restarted)) == before
    assert restarted.get_job(job.job_id).status is JobStatus.DELIVERY_UNKNOWN
    assert restarted.get_job_dispatch_count(job.job_id) == 0
    assert restarted.result_audits(job.job_id) == []  # Delivery is unknown; no result was applied.
    projection = application.projection()
    assert projection.primary_cta == "RESOLVE_DELIVERY_UNKNOWN"
    assert projection.provider_send_enabled is False
    assert projection.session_id == job.session_id
    assert projection.match_id == job.match_id
    assert projection.generation == job.generation


def test_probe_08_apply_selection_exception_rolls_back_all_canonical_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, application, job = _setup_request(tmp_path)
    assert application.apply_selection_advice_result(_valid_result(job)) is ResultDisposition.APPLIED
    before = _session_signature(_active_session(repository))
    before_rows = _row_count(repository, "applied_selections")

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic save failure")

    monkeypatch.setattr(repository, "save_session", fail_save)
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        application.apply_selection(
            selected_three=HUMAN_THREE,
            lead="Flutter Mane",
            human_confirmed=True,
        )

    after = _active_session(repository)
    assert _session_signature(after) == before
    assert after.state is BattleState.SELECTION_ADVICE_READY
    assert after.current_applied_selection_id is None
    assert _row_count(repository, "applied_selections") == before_rows


def test_probe_09_second_active_session_is_rejected_and_first_remains_canonical(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    first = application.new_match()
    before = _session_signature(_active_session(repository))

    with pytest.raises(DomainError, match="ACTIVE_MATCH_EXISTS"):
        application.new_match()

    assert _session_signature(_active_session(repository)) == before
    assert repository.count_sessions() == 1
    row = repository.connection.execute(
        "SELECT COUNT(*) FROM battle_sessions WHERE active_slot = 1"
    ).fetchone()
    assert row is not None
    assert int(row[0]) == 1
    assert first.session_id == before[0]
    assert first.match_id == before[1]
    assert first.generation == before[2]

    second = BattleSession(
        session_id="second-session",
        match_id="second-match",
        generation=first.generation + 1,
        state=BattleState.SELECTION_OPEN,
        battle_revision=1,
    )
    with pytest.raises(sqlite3.IntegrityError), repository.transaction():
        repository.insert_session(second)

    assert repository.count_sessions() == 1
    assert _session_signature(_active_session(repository)) == before


def test_probe_10_metadata_revision_does_not_stale_existing_advice_binding(
    tmp_path: Path,
) -> None:
    repository, application, job = _setup_request(tmp_path)
    before = _active_session(repository)
    before_battle_revision = before.battle_revision
    before_metadata_revision = before.metadata_revision
    before_current_ids = _session_signature(before)[6:]

    application.update_metadata()
    metadata_updated = _active_session(repository)
    assert metadata_updated.session_id == before.session_id
    assert metadata_updated.match_id == before.match_id
    assert metadata_updated.generation == before.generation
    assert metadata_updated.state is BattleState.SELECTION_OPEN
    assert metadata_updated.battle_revision == before_battle_revision
    assert metadata_updated.metadata_revision == before_metadata_revision + 1
    assert _session_signature(metadata_updated)[6:] == before_current_ids

    result = _valid_result(job)
    assert application.apply_selection_advice_result(result) is ResultDisposition.APPLIED

    after = _active_session(repository)
    assert after.session_id == before.session_id
    assert after.match_id == before.match_id
    assert after.generation == before.generation
    assert after.state is BattleState.SELECTION_ADVICE_READY
    assert after.battle_revision == before_battle_revision + 1
    assert after.metadata_revision == before_metadata_revision + 1
    assert after.current_selection_advice_id == result.result_id
    assert repository.get_job(job.job_id).status is JobStatus.SUCCEEDED
    assert repository.result_audits(job.job_id) == [("APPLIED", "BINDING_ACCEPTED")]
