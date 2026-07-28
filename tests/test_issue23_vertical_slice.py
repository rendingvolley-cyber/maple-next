from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import BattleState, HpBucket, JobStatus, ResultDisposition
from maple_next.domain.models import ReviewedBoardSnapshot
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPP_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def setup_request(
    tmp_path: Path,
) -> tuple[SQLiteRepository, BattleApplication, JobEnvelope]:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    app.confirm_selection_facts(SELF_TEAM, OPP_TEAM)
    job = app.request_selection_advice("human-command-1")
    return repo, app, job


def valid_result(job: JobEnvelope, **changes: object) -> ResultEnvelope:
    base = ResultEnvelope(
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
        payload={
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite"],
            "lead": "Meowscarada",
        },
    )
    return replace(base, **changes)


def test_no_active_match_projection_and_only_new_match_creates_session(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    assert app.projection().application_mode == "NO_ACTIVE_MATCH"
    app.new_match()
    assert repo.count_sessions() == 1
    with pytest.raises(DomainError, match="ACTIVE_MATCH_EXISTS"):
        app.new_match()
    assert repo.count_sessions() == 1


def test_correct_mock_result_applies_once_and_duplicate_is_ignored(tmp_path: Path) -> None:
    repo, app, job = setup_request(tmp_path)
    result = valid_result(job)
    assert app.apply_selection_advice_result(result) is ResultDisposition.APPLIED
    session = repo.load_active_session()
    assert session is not None
    revision = session.battle_revision
    assert app.apply_selection_advice_result(result) is ResultDisposition.DUPLICATE_IGNORED
    session = repo.load_active_session()
    assert session is not None
    assert session.battle_revision == revision
    assert repo.result_audits(job.job_id) == [
        ("APPLIED", "BINDING_ACCEPTED"),
        ("DUPLICATE_IGNORED", "RESULT_ALREADY_APPLIED"),
    ]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("job_id", "missing-job", "JOB_ID_MISMATCH"),
        ("command_id", "wrong-command", "COMMAND_ID_MISMATCH"),
        ("base_battle_revision", 999, "BATTLE_REVISION_MISMATCH"),
        ("input_snapshot_id", "wrong-snapshot", "INPUT_SNAPSHOT_MISMATCH"),
    ],
)
def test_stale_binding_is_audited_without_domain_mutation(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    repo, app, job = setup_request(tmp_path)
    before = repo.load_active_session()
    result = valid_result(job, **{field: value})
    assert app.apply_selection_advice_result(result) is ResultDisposition.STALE_REJECTED
    after = repo.load_active_session()
    assert after is not None and before is not None
    assert after.state == before.state
    assert after.battle_revision == before.battle_revision
    audit_job_id = "missing-job" if field == "job_id" else job.job_id
    assert repo.result_audits(audit_job_id) == [("STALE_REJECTED", reason)]


def test_metadata_revision_does_not_stale_advice_binding(tmp_path: Path) -> None:
    repo, app, job = setup_request(tmp_path)
    active = repo.load_active_session()
    assert active is not None
    original_battle_revision = active.battle_revision
    app.update_metadata()
    session = repo.load_active_session()
    assert session is not None
    assert session.battle_revision == original_battle_revision
    assert session.metadata_revision == 1
    assert app.apply_selection_advice_result(valid_result(job)) is ResultDisposition.APPLIED


def test_queued_provider_job_restart_has_zero_dispatch_and_becomes_interrupted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "maple.db"
    repo, _, job = setup_request(tmp_path)
    repo.close()

    restarted = SQLiteRepository(database_path)
    app = BattleApplication(restarted)
    app.recover_after_restart()
    assert restarted.get_job(job.job_id).status is JobStatus.INTERRUPTED
    assert restarted.get_job_dispatch_count(job.job_id) == 0
    assert app.projection().provider_send_enabled is True


def test_in_flight_provider_restart_is_delivery_unknown_and_send_disabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "maple.db"
    repo, _, job = setup_request(tmp_path)
    repo.set_job_status_for_test(job.job_id, JobStatus.IN_FLIGHT)
    repo.close()

    restarted = SQLiteRepository(database_path)
    app = BattleApplication(restarted)
    app.recover_after_restart()
    projection = app.projection()
    assert restarted.get_job(job.job_id).status is JobStatus.DELIVERY_UNKNOWN
    assert restarted.get_job_dispatch_count(job.job_id) == 0
    assert projection.primary_cta == "RESOLVE_DELIVERY_UNKNOWN"
    assert projection.provider_send_enabled is False


def test_apply_selection_reaches_battle_ready_and_preserves_exact_three(tmp_path: Path) -> None:
    repo, app, job = setup_request(tmp_path)
    app.apply_selection_advice_result(valid_result(job))
    applied = app.apply_selection(
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
        human_confirmed=True,
    )
    session = repo.load_active_session()
    assert session is not None
    assert session.state is BattleState.BATTLE_READY
    assert session.current_applied_selection_id == applied.applied_selection_id
    persisted = repo.get_applied_selection(applied.applied_selection_id)
    assert persisted.selected_three == ("Meowscarada", "Gholdengo", "Dragonite")
    assert persisted.lead == "Meowscarada"


def test_apply_requires_explicit_human_confirmation(tmp_path: Path) -> None:
    _, app, job = setup_request(tmp_path)
    app.apply_selection_advice_result(valid_result(job))
    with pytest.raises(DomainError, match="HUMAN_APPLY_REQUIRED"):
        app.apply_selection(
            selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
            lead="Meowscarada",
            human_confirmed=False,
        )


def test_restart_restores_same_session_ids_state_and_turn_cta(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repo, app, job = setup_request(tmp_path)
    app.apply_selection_advice_result(valid_result(job))
    app.apply_selection(
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
        human_confirmed=True,
    )
    before = app.projection()
    repo.close()

    restarted_repo = SQLiteRepository(database_path)
    restarted_app = BattleApplication(restarted_repo)
    after = restarted_app.projection()
    assert after == before
    assert after.session_state == "BATTLE_READY"
    assert after.primary_cta == "START_TURN_CAPTURE"


def test_unknown_hp_is_not_coerced_to_zero_or_none() -> None:
    snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board-1",
        turn_id="turn-1",
        self_active="Meowscarada",
        opponent_active="Garchomp",
        self_hp=HpBucket.UNKNOWN,
        opponent_hp=HpBucket.ZERO,
        self_status="UNKNOWN",
        opponent_status="NONE",
    )
    payload = snapshot.to_canonical_dict()
    assert payload["self_hp"] == "UNKNOWN"
    assert payload["opponent_hp"] == "0"
    assert payload["self_status"] == "UNKNOWN"


def test_worker_contract_has_no_sqlite_write_path() -> None:
    import inspect

    import maple_next.workers.contracts.models as contracts

    source = inspect.getsource(contracts)
    assert "sqlite3" not in source
    assert "SQLiteRepository" not in source
    assert not hasattr(contracts.JobEnvelope, "connection")


def test_pending_provider_request_blocks_a_second_human_request(tmp_path: Path) -> None:
    _, app, _ = setup_request(tmp_path)
    with pytest.raises(DomainError, match="PROVIDER_REQUEST_PENDING"):
        app.request_selection_advice("human-command-2")


def test_old_provider_result_is_stale_after_a_new_human_request(tmp_path: Path) -> None:
    repo, app, old_job = setup_request(tmp_path)
    repo.set_job_status_for_test(old_job.job_id, JobStatus.INTERRUPTED)
    new_job = app.request_selection_advice("human-command-2")
    assert new_job.job_id != old_job.job_id

    disposition = app.apply_selection_advice_result(valid_result(old_job))
    assert disposition is ResultDisposition.STALE_REJECTED
    assert repo.result_audits(old_job.job_id) == [
        ("STALE_REJECTED", "JOB_ID_NOT_CURRENT")
    ]


def test_delivery_unknown_blocks_new_provider_command(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repo, _, job = setup_request(tmp_path)
    repo.set_job_status_for_test(job.job_id, JobStatus.IN_FLIGHT)
    repo.close()

    restarted = SQLiteRepository(database_path)
    app = BattleApplication(restarted)
    app.recover_after_restart()
    with pytest.raises(DomainError, match="PROVIDER_DELIVERY_UNKNOWN"):
        app.request_selection_advice("human-command-2")


def test_result_acceptance_rolls_back_as_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, app, job = setup_request(tmp_path)
    result = valid_result(job)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(repo, "audit_result", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        app.apply_selection_advice_result(result)

    session = repo.load_active_session()
    assert session is not None
    assert session.state is BattleState.SELECTION_OPEN
    assert session.current_selection_advice_id is None
    assert repo.get_job(job.job_id).status is JobStatus.QUEUED
    with pytest.raises(KeyError):
        repo.get_selection_advice(result.result_id)


def test_sqlite_constraint_allows_at_most_one_active_session(tmp_path: Path) -> None:
    import sqlite3

    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    duplicate = app.projection()
    assert duplicate.session_id is not None

    from maple_next.domain.models import BattleSession

    second = BattleSession(
        session_id="second-session",
        match_id="second-match",
        generation=2,
        state=BattleState.SELECTION_OPEN,
        battle_revision=1,
    )
    with pytest.raises(sqlite3.IntegrityError), repo.transaction():
        repo.insert_session(second)
