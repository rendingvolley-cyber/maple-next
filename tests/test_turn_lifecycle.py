from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ActionType, BattleState, HpBucket, JobType, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
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
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")
LEGAL_MOVES = ("Protect", "Wave Crash", "Earthquake")
LEGAL_SWITCHES = ("Flutter Mane", "Urshifu")


def build_ready_application(
    database_path: Path,
) -> tuple[SQLiteRepository, BattleApplication]:
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
    return repository, application


def confirm_turn(application: BattleApplication) -> None:
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


def valid_result(job: JobEnvelope, **payload_overrides: str) -> ResultEnvelope:
    payload = {
        "action_type": ActionType.MOVE.value,
        "action_name": "Protect",
        "opponent_prediction": "Earthquake",
        "rationale": "Scout before committing.",
    }
    payload.update(payload_overrides)
    return ResultEnvelope(
        contract_version=job.contract_version,
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
        payload=payload,
    )


def apply_mock_turn_advice(application: BattleApplication) -> None:
    adapter = MockTurnAdviceAdapter()
    result = adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout before committing.",
    )
    assert result.disposition is ResultDisposition.APPLIED
    assert adapter.network_call_count == 0


def test_turn_starts_only_on_explicit_command_and_first_number_is_one(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    before = repository.load_active_session()
    assert before is not None
    assert before.state is BattleState.BATTLE_READY
    assert before.current_turn_id is None

    turn = application.start_turn_capture()
    after = repository.load_active_session()

    assert turn.turn_number == 1
    assert after is not None
    assert after.state is BattleState.TURN_CAPTURE_PENDING
    assert after.current_turn_id == turn.turn_id
    with pytest.raises(DomainError, match="EXPECTED_BATTLE_READY"):
        application.start_turn_capture()


def test_turn_input_validation_rejects_active_moves_switches_and_invalid_hp(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    application.start_turn_capture()
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="SELF_ACTIVE_OUTSIDE_APPLIED_SELECTION"):
        application.confirm_turn_facts(
            self_active="Meowscarada",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Protect",),
            legal_switches=LEGAL_SWITCHES,
            human_note="",
            human_confirmed=True,
        )
    with pytest.raises(DomainError, match="INVALID_TURN_FACTS"):
        application.confirm_turn_facts(
            self_active="Dondozo",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Protect", "Protect"),
            legal_switches=LEGAL_SWITCHES,
            human_note="",
            human_confirmed=True,
        )
    with pytest.raises(DomainError, match="SELF_ACTIVE_IN_SWITCH_CANDIDATES"):
        application.confirm_turn_facts(
            self_active="Dondozo",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Protect",),
            legal_switches=("Dondozo",),
            human_note="",
            human_confirmed=True,
        )

    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    view = controller.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp="not-a-bucket",
        opponent_hp=HpBucket.FULL.value,
        legal_moves=("Protect",),
        legal_switches=LEGAL_SWITCHES,
        human_note="",
        human_confirmed=True,
    )
    assert view.error_message == "自分のHPは一覧から選択してください。"
    after = repository.load_active_session()
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_reviewed_board_id is None


def test_confirming_turn_facts_saves_immutable_snapshot_and_bumps_revision(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    application.start_turn_capture()
    before = repository.load_active_session()
    assert before is not None

    snapshot = application.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.EIGHTY_ONE_TO_NINETY,
        legal_moves=LEGAL_MOVES,
        legal_switches=LEGAL_SWITCHES,
        human_note="manual review",
        human_confirmed=True,
    )
    after = repository.load_active_session()

    assert after is not None
    assert after.state is BattleState.TURN_REVIEWED
    assert after.current_reviewed_board_id == snapshot.turn_facts_id
    assert after.battle_revision == before.battle_revision + 1
    assert repository.get_turn_facts(snapshot.turn_facts_id) == snapshot


def test_turn_fact_correction_appends_new_snapshot_without_overwriting_old(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    session = repository.load_active_session()
    assert session is not None
    assert session.current_reviewed_board_id is not None
    old_id = session.current_reviewed_board_id
    old_snapshot = repository.get_turn_facts(old_id)

    corrected = application.confirm_turn_facts(
        self_active="Flutter Mane",
        opponent_active="Garchomp",
        self_hp=HpBucket.SEVENTY_ONE_TO_EIGHTY,
        opponent_hp=HpBucket.EIGHTY_ONE_TO_NINETY,
        legal_moves=("Moonblast", "Protect"),
        legal_switches=("Dondozo", "Urshifu"),
        human_note="corrected active",
        human_confirmed=True,
    )

    assert corrected.turn_facts_id != old_id
    assert corrected.previous_snapshot_id == old_id
    assert repository.get_turn_facts(old_id) == old_snapshot
    current = repository.load_active_session()
    assert current is not None
    assert current.current_reviewed_board_id == corrected.turn_facts_id
    assert current.current_turn_advice_id is None


def test_mock_turn_advice_requires_explicit_request_and_network_count_stays_zero(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    before = repository.load_active_session()
    assert before is not None
    assert before.current_turn_advice_id is None

    adapter = MockTurnAdviceAdapter()
    result = adapter.submit(
        application,
        action_type=ActionType.MOVE,
        action_name="Protect",
        opponent_prediction="Earthquake",
        rationale="Scout before committing.",
    )

    assert result.disposition is ResultDisposition.APPLIED
    assert adapter.network_call_count == 0
    after = repository.load_active_session()
    assert after is not None
    assert after.current_turn_advice_id == result.result_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("command_id", "wrong-command"),
        ("job_type", JobType.SELECTION_ADVICE),
        ("session_id", "wrong-session"),
        ("match_id", "wrong-match"),
        ("generation", 999),
        ("turn_number", 999),
        ("base_battle_revision", 999),
        ("expected_state", BattleState.BATTLE_READY),
        ("input_snapshot_id", "wrong-snapshot"),
        ("request_payload_hash", "wrong-hash"),
    ],
)
def test_turn_result_strict_binding_rejects_each_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository, application = build_ready_application(tmp_path / f"{field}.db")
    confirm_turn(application)
    job = application.request_turn_advice(f"command-{field}")
    result = replace(valid_result(job), **{field: value})
    before = repository.load_active_session()

    disposition = application.apply_turn_advice_result(result)
    after = repository.load_active_session()

    assert disposition is ResultDisposition.STALE_REJECTED
    assert before is not None and after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_turn_advice_id is None


def test_unknown_job_and_duplicate_result_are_rejected_without_second_apply(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    job = application.request_turn_advice("command-1")
    unknown = replace(valid_result(job), job_id="unknown-job")
    assert application.apply_turn_advice_result(unknown) is ResultDisposition.STALE_REJECTED

    result = valid_result(job)
    assert application.apply_turn_advice_result(result) is ResultDisposition.APPLIED
    session = repository.load_active_session()
    assert session is not None
    revision = session.battle_revision
    assert application.apply_turn_advice_result(result) is ResultDisposition.DUPLICATE_IGNORED
    duplicate_session = repository.load_active_session()
    assert duplicate_session is not None
    assert duplicate_session.battle_revision == revision


def test_correction_makes_old_turn_result_stale(tmp_path: Path) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    job = application.request_turn_advice("old-command")
    old_result = valid_result(job)

    application.confirm_turn_facts(
        self_active="Flutter Mane",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.FULL,
        legal_moves=("Moonblast",),
        legal_switches=("Dondozo", "Urshifu"),
        human_note="correction",
        human_confirmed=True,
    )
    before = repository.load_active_session()

    disposition = application.apply_turn_advice_result(old_result)
    after = repository.load_active_session()

    assert disposition is ResultDisposition.STALE_REJECTED
    assert before is not None and after is not None
    assert after.current_reviewed_board_id == before.current_reviewed_board_id
    assert after.current_turn_advice_id is None
    assert after.battle_revision == before.battle_revision


def test_illegal_advice_payload_is_rejected_without_canonical_mutation(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    job = application.request_turn_advice("illegal-advice")
    before = repository.load_active_session()

    disposition = application.apply_turn_advice_result(
        valid_result(job, action_name="Illegal Move")
    )
    after = repository.load_active_session()

    assert disposition is ResultDisposition.INVALID_REJECTED
    assert before is not None and after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_turn_advice_id is None


def test_human_can_record_legal_action_different_from_mock_advice(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    apply_mock_turn_advice(application)

    action = application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
    )
    session = repository.load_active_session()

    assert action.action_name == "Wave Crash"
    assert session is not None
    assert session.state is BattleState.TURN_RECORDED
    history = repository.list_recorded_actions(session.session_id)
    assert history == (action,)


def test_illegal_and_duplicate_action_record_leave_history_unchanged(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    apply_mock_turn_advice(application)
    session = repository.load_active_session()
    assert session is not None

    with pytest.raises(DomainError, match="ACTION_OUTSIDE_REVIEWED_LEGAL_ACTIONS"):
        application.record_actual_action(
            action_type=ActionType.MOVE,
            action_name="Illegal Move",
            human_confirmed=True,
        )
    assert repository.list_recorded_actions(session.session_id) == ()

    application.record_actual_action(
        action_type=ActionType.SWITCH,
        action_name="Flutter Mane",
        human_confirmed=True,
    )
    history = repository.list_recorded_actions(session.session_id)
    with pytest.raises(DomainError, match="EXPECTED_TURN_REVIEWED"):
        application.record_actual_action(
            action_type=ActionType.SWITCH,
            action_name="Flutter Mane",
            human_confirmed=True,
        )
    assert repository.list_recorded_actions(session.session_id) == history


def test_next_turn_is_explicit_and_double_command_cannot_increment_twice(
    tmp_path: Path,
) -> None:
    repository, application = build_ready_application(tmp_path / "maple.db")
    confirm_turn(application)
    apply_mock_turn_advice(application)
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Protect",
        human_confirmed=True,
    )

    turn = application.next_turn()
    assert turn.turn_number == 2
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.TURN_CAPTURE_PENDING
    assert session.current_reviewed_board_id is None
    assert session.current_turn_advice_id is None
    with pytest.raises(DomainError, match="EXPECTED_TURN_RECORDED"):
        application.next_turn()
    assert repository.get_turn(session.current_turn_id or "").turn_number == 2


def test_restart_restores_turn_facts_advice_history_and_cta(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repository, application = build_ready_application(database_path)
    confirm_turn(application)
    apply_mock_turn_advice(application)
    application.record_actual_action(
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        human_confirmed=True,
    )
    before = application.projection()
    repository.close()

    restarted_repository = SQLiteRepository(database_path)
    restarted_application = BattleApplication(restarted_repository)
    restarted_application.recover_after_restart()
    controller = SelectionFlowController(
        restarted_application,
        restarted_repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    after = controller.refresh()

    assert after.projection.session_id == before.session_id
    assert after.session_state == "TURN_RECORDED"
    assert after.turn_number == 1
    assert after.turn_facts is not None
    assert after.turn_facts.self_active == "Dondozo"
    assert after.turn_advice is not None
    assert after.turn_advice.action_name == "Protect"
    assert after.action_history[0].action_name == "Wave Crash"
    assert after.primary_cta == "NEXT_TURN"
    assert controller.network_call_count == 0
    restarted_repository.close()
