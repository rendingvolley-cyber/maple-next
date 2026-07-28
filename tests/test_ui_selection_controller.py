from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.application.projection import DomainProjection
from maple_next.application.service import BattleApplication
from maple_next.domain.enums import BattleState
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter

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


def build_controller(
    database_path: Path,
) -> tuple[SQLiteRepository, BattleApplication, SelectionFlowController]:
    repository = SQLiteRepository(database_path)
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
    )
    return repository, application, controller


def confirm_facts(controller: SelectionFlowController) -> None:
    controller.new_match()
    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    assert view.error_message is None


def receive_mock_advice(controller: SelectionFlowController) -> None:
    confirm_facts(controller)
    view = controller.submit_mock_advice(ADVICE_THREE, "Meowscarada")
    assert view.error_message is None


def test_startup_renders_no_active_match_projection(tmp_path: Path) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")
    view = controller.refresh()

    assert view.application_mode == "NO_ACTIVE_MATCH"
    assert view.session_state is None
    assert view.primary_cta == "CREATE_NEW_MATCH"
    assert view.provider_status == "NONE"
    assert view.battle_revision is None
    assert view.error_message is None
    assert repository.count_sessions() == 0


def test_new_match_creates_only_one_session(tmp_path: Path) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")

    first = controller.new_match()
    second = controller.new_match()

    assert first.session_state == "SELECTION_OPEN"
    assert repository.count_sessions() == 1
    assert second.error_message == "進行中の対戦があるため、NEW MATCHは作成できません。"
    assert repository.count_sessions() == 1


@pytest.mark.parametrize(
    ("self_entries", "message_fragment"),
    [
        (SELF_TEAM[:5], "6体ちょうど"),
        ((*SELF_TEAM, "Extra"), "6体ちょうど"),
        (("", *SELF_TEAM[1:]), "1番目が空欄"),
        ((SELF_TEAM[0], SELF_TEAM[0], *SELF_TEAM[2:]), "重複があります"),
    ],
)
def test_manual_team_validation_rejects_incomplete_blank_or_duplicate_values(
    tmp_path: Path,
    self_entries: tuple[str, ...],
    message_fragment: str,
) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")
    controller.new_match()
    before = repository.load_active_session()

    view = controller.confirm_selection_facts(self_entries, OPPONENT_TEAM)
    after = repository.load_active_session()

    assert view.error_message is not None
    assert message_fragment in view.error_message
    assert before is not None and after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_reviewed_selection_id is None


def test_confirmed_selection_facts_update_projection(tmp_path: Path) -> None:
    _, _, controller = build_controller(tmp_path / "maple.db")
    controller.new_match()

    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)

    assert view.error_message is None
    assert view.session_state == "SELECTION_OPEN"
    assert view.primary_cta == "REQUEST_SELECTION_ADVICE"
    assert view.battle_revision == 2
    assert view.self_team == SELF_TEAM
    assert view.opponent_team == OPPONENT_TEAM


def test_mock_advice_is_visible_and_explicitly_non_network(tmp_path: Path) -> None:
    _, _, controller = build_controller(tmp_path / "maple.db")
    confirm_facts(controller)

    view = controller.submit_mock_advice(ADVICE_THREE, "Meowscarada")

    assert view.error_message is None
    assert view.session_state == "SELECTION_ADVICE_READY"
    assert view.primary_cta == "APPLY_SELECTION"
    assert view.provider_status == "SUCCEEDED"
    assert view.advice is not None
    assert view.advice.is_mock is True
    assert view.advice.selected_three == ADVICE_THREE
    assert view.advice.lead == "Meowscarada"
    assert controller.network_call_count == 0


def test_human_can_apply_legal_selection_different_from_advice(tmp_path: Path) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")
    receive_mock_advice(controller)

    view = controller.apply_selection(
        HUMAN_THREE,
        "Flutter Mane",
        human_confirmed=True,
    )

    assert view.error_message is None
    assert view.session_state == "BATTLE_READY"
    assert view.primary_cta == "START_TURN_CAPTURE"
    assert view.applied_selection is not None
    assert view.applied_selection.selected_three == HUMAN_THREE
    assert view.applied_selection.lead == "Flutter Mane"
    assert view.applied_selection.backline == ("Dondozo", "Urshifu")
    assert view.advice is not None
    assert view.applied_selection.selected_three != view.advice.selected_three
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.BATTLE_READY


@pytest.mark.parametrize(
    ("selected_three", "lead", "message_fragment"),
    [
        (("Dondozo", "Dondozo", "Urshifu"), "Dondozo", "重複"),
        (("Dondozo", "Flutter Mane", "MissingNo"), "Dondozo", "確認済み6体"),
        (HUMAN_THREE, "Meowscarada", "3体の中"),
    ],
)
def test_illegal_human_apply_is_rejected_without_canonical_mutation(
    tmp_path: Path,
    selected_three: tuple[str, str, str],
    lead: str,
    message_fragment: str,
) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")
    receive_mock_advice(controller)
    before = repository.load_active_session()
    assert before is not None

    view = controller.apply_selection(selected_three, lead, human_confirmed=True)
    after = repository.load_active_session()

    assert view.error_message is not None
    assert message_fragment in view.error_message
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_applied_selection_id is None
    row = repository.connection.execute("SELECT COUNT(*) FROM applied_selections").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_apply_persistence_failure_rolls_back_and_reports_japanese_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, controller = build_controller(tmp_path / "maple.db")
    receive_mock_advice(controller)
    before = repository.load_active_session()
    assert before is not None

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic save failure")

    monkeypatch.setattr(repository, "save_session", fail_save)
    view = controller.apply_selection(HUMAN_THREE, "Urshifu", human_confirmed=True)
    after = repository.load_active_session()

    assert view.error_message == "APPLYの保存に失敗しました。実際の選出は反映されていません。"
    assert after is not None
    assert after.state is before.state
    assert after.battle_revision == before.battle_revision
    assert after.current_applied_selection_id is None
    row = repository.connection.execute("SELECT COUNT(*) FROM applied_selections").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_battle_ready_view_exposes_actual_three_lead_backline_and_turn_cta(
    tmp_path: Path,
) -> None:
    _, _, controller = build_controller(tmp_path / "maple.db")
    receive_mock_advice(controller)

    view = controller.apply_selection(HUMAN_THREE, "Urshifu", human_confirmed=True)

    assert view.projection.message == "BATTLE_READY"
    assert view.primary_cta == "START_TURN_CAPTURE"
    assert view.applied_selection is not None
    assert view.applied_selection.selected_three == HUMAN_THREE
    assert view.applied_selection.lead == "Urshifu"
    assert view.applied_selection.backline == ("Dondozo", "Flutter Mane")


def test_restart_restores_same_session_actual_selection_and_cta(tmp_path: Path) -> None:
    database_path = tmp_path / "maple.db"
    repository, _, controller = build_controller(database_path)
    receive_mock_advice(controller)
    before = controller.apply_selection(HUMAN_THREE, "Urshifu", human_confirmed=True)
    before_session_id = before.projection.session_id
    before_applied_id = before.projection.current_applied_selection_id
    repository.close()

    (
        restarted_repository,
        restarted_application,
        restarted_controller,
    ) = build_controller(database_path)
    restarted_application.recover_after_restart()
    after = restarted_controller.refresh()

    assert after.projection.session_id == before_session_id
    assert after.projection.current_applied_selection_id == before_applied_id
    assert after.session_state == "BATTLE_READY"
    assert after.primary_cta == "START_TURN_CAPTURE"
    assert after.applied_selection is not None
    assert after.applied_selection.selected_three == HUMAN_THREE
    assert after.applied_selection.lead == "Urshifu"
    assert restarted_controller.network_call_count == 0
    restarted_repository.close()


def test_controller_renders_domain_projection_as_source_of_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, application, controller = build_controller(tmp_path / "maple.db")
    sentinel = DomainProjection(
        application_mode="SENTINEL_MODE",
        primary_cta="SENTINEL_CTA",
        primary_cta_enabled=False,
        secondary_actions=(),
        message="SENTINEL_MESSAGE",
        provider_status="SENTINEL_PROVIDER",
        provider_send_enabled=False,
        session_state="SENTINEL_STATE",
        battle_revision=77,
        metadata_revision=12,
        session_id=None,
        match_id=None,
        generation=None,
        current_reviewed_selection_id=None,
        current_selection_advice_id=None,
        current_applied_selection_id=None,
    )
    monkeypatch.setattr(application, "projection", lambda: sentinel)

    view = controller.refresh()

    assert view.projection is sentinel
    assert view.application_mode == "SENTINEL_MODE"
    assert view.session_state == "SENTINEL_STATE"
    assert view.primary_cta == "SENTINEL_CTA"
    assert view.provider_status == "SENTINEL_PROVIDER"
    assert view.battle_revision == 77


def test_complete_manual_flow_makes_zero_provider_network_calls(tmp_path: Path) -> None:
    _, _, controller = build_controller(tmp_path / "maple.db")

    controller.new_match()
    controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    controller.submit_mock_advice(ADVICE_THREE, "Dragonite")
    final_view = controller.apply_selection(HUMAN_THREE, "Dondozo", human_confirmed=True)

    assert final_view.session_state == "BATTLE_READY"
    assert controller.network_call_count == 0
