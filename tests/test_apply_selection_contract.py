from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import BattleState
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import ResultEnvelope

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPP_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
ADVICE_THREE = ("Meowscarada", "Gholdengo", "Dragonite")
HUMAN_THREE = ("Dondozo", "Flutter Mane", "Urshifu")


def ready_for_apply(tmp_path: Path) -> tuple[SQLiteRepository, BattleApplication, str]:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    app.confirm_selection_facts(SELF_TEAM, OPP_TEAM)
    job = app.request_selection_advice("human-command-1")
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
    app.apply_selection_advice_result(result)
    return repo, app, result.result_id


def applied_row_count(repo: SQLiteRepository) -> int:
    row = repo.connection.execute("SELECT COUNT(*) FROM applied_selections").fetchone()
    assert row is not None
    return int(row[0])


def assert_apply_rejected_without_mutation(
    repo: SQLiteRepository,
    app: BattleApplication,
    *,
    selected_three: tuple[str, str, str],
    lead: str,
    human_confirmed: bool,
    error: str,
) -> None:
    before = repo.load_active_session()
    assert before is not None
    before_count = applied_row_count(repo)

    with pytest.raises(DomainError, match=error):
        app.apply_selection(
            selected_three=selected_three,
            lead=lead,
            human_confirmed=human_confirmed,
        )

    after = repo.load_active_session()
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_applied_selection_id == before.current_applied_selection_id
    assert applied_row_count(repo) == before_count


def test_human_can_apply_same_selection_as_advice(tmp_path: Path) -> None:
    repo, app, advice_id = ready_for_apply(tmp_path)
    snapshot = app.apply_selection(
        selected_three=ADVICE_THREE,
        lead="Meowscarada",
        human_confirmed=True,
    )

    persisted = repo.get_applied_selection(snapshot.applied_selection_id)
    assert persisted.selected_three == ADVICE_THREE
    assert persisted.lead == "Meowscarada"
    assert persisted.backline == ("Gholdengo", "Dragonite")
    assert persisted.source_advice_id == advice_id


def test_human_can_apply_different_legal_selection_than_advice(tmp_path: Path) -> None:
    repo, app, advice_id = ready_for_apply(tmp_path)
    snapshot = app.apply_selection(
        selected_three=HUMAN_THREE,
        lead="Flutter Mane",
        human_confirmed=True,
    )

    persisted = repo.get_applied_selection(snapshot.applied_selection_id)
    assert persisted.selected_three == HUMAN_THREE
    assert persisted.lead == "Flutter Mane"
    assert persisted.backline == ("Dondozo", "Urshifu")
    assert persisted.source_advice_id == advice_id
    assert persisted.selected_three != ADVICE_THREE


def test_restart_preserves_human_actual_selection_and_battle_ready(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repo, app, _ = ready_for_apply(tmp_path)
    snapshot = app.apply_selection(
        selected_three=HUMAN_THREE,
        lead="Urshifu",
        human_confirmed=True,
    )
    before = repo.load_active_session()
    assert before is not None
    repo.close()

    restarted_repo = SQLiteRepository(database_path)
    restarted = restarted_repo.load_active_session()
    assert restarted is not None
    assert restarted.session_id == before.session_id
    assert restarted.state is BattleState.BATTLE_READY
    assert restarted.current_applied_selection_id == snapshot.applied_selection_id
    persisted = restarted_repo.get_applied_selection(snapshot.applied_selection_id)
    assert persisted.selected_three == HUMAN_THREE
    assert persisted.lead == "Urshifu"
    assert persisted.backline == ("Dondozo", "Flutter Mane")


@pytest.mark.parametrize(
    ("selected_three", "error"),
    [
        (cast(tuple[str, str, str], ("Meowscarada", "Gholdengo")), "EXACTLY_THREE"),
        (
            cast(
                tuple[str, str, str],
                ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo"),
            ),
            "EXACTLY_THREE",
        ),
        (
            ("Meowscarada", "Meowscarada", "Dragonite"),
            "DUPLICATE_SELECTION",
        ),
        (
            ("Meowscarada", "Gholdengo", "MissingNo"),
            "SELECTION_OUTSIDE_REVIEWED_TEAM",
        ),
    ],
)
def test_invalid_selected_three_rejected_without_partial_write(
    tmp_path: Path,
    selected_three: tuple[str, str, str],
    error: str,
) -> None:
    repo, app, _ = ready_for_apply(tmp_path)
    assert_apply_rejected_without_mutation(
        repo,
        app,
        selected_three=selected_three,
        lead=selected_three[0],
        human_confirmed=True,
        error=error,
    )


def test_lead_outside_selected_three_rejected_without_partial_write(tmp_path: Path) -> None:
    repo, app, _ = ready_for_apply(tmp_path)
    assert_apply_rejected_without_mutation(
        repo,
        app,
        selected_three=ADVICE_THREE,
        lead="Dondozo",
        human_confirmed=True,
        error="LEAD_NOT_IN_SELECTED_THREE",
    )


def test_human_confirmed_false_rejected_without_partial_write(tmp_path: Path) -> None:
    repo, app, _ = ready_for_apply(tmp_path)
    assert_apply_rejected_without_mutation(
        repo,
        app,
        selected_three=ADVICE_THREE,
        lead="Meowscarada",
        human_confirmed=False,
        error="HUMAN_APPLY_REQUIRED",
    )


def test_missing_reviewed_selection_fails_closed_without_partial_write(tmp_path: Path) -> None:
    repo, app, _ = ready_for_apply(tmp_path)
    session = repo.load_active_session()
    assert session is not None
    assert session.current_reviewed_selection_id is not None
    with repo.transaction():
        repo.connection.execute(
            "DELETE FROM reviewed_selection_facts WHERE reviewed_selection_id = ?",
            (session.current_reviewed_selection_id,),
        )

    assert_apply_rejected_without_mutation(
        repo,
        app,
        selected_three=ADVICE_THREE,
        lead="Meowscarada",
        human_confirmed=True,
        error="REVIEWED_SELECTION_UNAVAILABLE",
    )


def test_apply_selection_rolls_back_append_and_session_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, app, _ = ready_for_apply(tmp_path)
    before = repo.load_active_session()
    assert before is not None

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic save failure")

    monkeypatch.setattr(repo, "save_session", fail_save)
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        app.apply_selection(
            selected_three=HUMAN_THREE,
            lead="Flutter Mane",
            human_confirmed=True,
        )

    after = repo.load_active_session()
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_applied_selection_id is None
    assert applied_row_count(repo) == 0
