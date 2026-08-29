"""Focused production-path integration coverage for Battle Mega events."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from maple_next.application.match_export_v3 import parse_match_export_v4
from maple_next.domain.enums import MatchOutcome
from maple_next.domain.mega_evolution import (
    MegaBattleState,
    MegaSide,
    mega_state_to_canonical_dict,
)
from maple_next.persistence.schema import SCHEMA_VERSION
from maple_next.providers.turn_advice_rich_state import (
    canonical_rich_request_dict,
    rich_request_payload_hash,
)
from maple_next.providers.turn_validation import select_response_parser_version
from tests.test_issue31_turn_state_ui_bundle_c import (
    _confirm_legal_switches_honestly,
    build_window,
)

SELF_TEAM = (
    "メタグロス",
    "ラグラージ",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Urshifu",
)
SELECTED_THREE = SELF_TEAM[:3]
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)
DEFAULT_OPPONENT_ACTIVE = OPPONENT_TEAM[0]
DEFAULT_MOVE = "Flower Trick"


def _result_summary_text(window) -> str:
    texts: list[str] = []
    for index in range(window.result_summary_layout.count()):
        item = window.result_summary_layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            texts.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(texts)


def _prepare_window(
    tmp_path: Path,
    *,
    self_active: str = SELECTED_THREE[0],
    opponent_active: str = DEFAULT_OPPONENT_ACTIVE,
):
    repository, controller, window, transport = build_window(tmp_path)
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELECTED_THREE), SELECTED_THREE[0])
    controller.apply_selection(list(SELECTED_THREE), SELECTED_THREE[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()
    _fill_confirmed_turn_facts(window, self_active=self_active, opponent_active=opponent_active)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    if transport.call_count == 0:
        window.mock_turn_action_type_box.setCurrentText("MOVE")
        window.mock_turn_action_name_box.setCurrentText(DEFAULT_MOVE)
        window.mock_turn_prediction_input.setText("fake prediction")
        window.mock_turn_rationale_input.setText("fake/injected result-entry test")
        window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    assert controller.refresh().turn_advice is not None, controller.refresh().error_message
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText(DEFAULT_MOVE)
    window.actual_action_confirm_checkbox.setChecked(True)
    return repository, controller, window, transport


def _fill_confirmed_turn_facts(window, *, self_active: str, opponent_active: str) -> None:
    window.self_active_box.setCurrentText(self_active)
    window.opponent_active_input.setText(opponent_active)
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText(DEFAULT_MOVE)
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")


def _open_result(window) -> None:
    window.record_action_button.click()
    assert window._result_entry_active is True  # noqa: SLF001
    assert window.action_result_step_stack.currentWidget() is window.result_workbench_page


def _stage_mega(window, side: MegaSide) -> None:
    button = window.self_mega_button if side is MegaSide.SELF else window.opponent_mega_button
    assert button.isEnabled()
    button.click()


def _commit_result_without_advancing(window) -> None:
    window._on_record_action()  # noqa: SLF001


def _set_selection_intent(repository, controller, intended_mega: str) -> None:
    session = repository.load_active_session()
    assert session is not None and session.current_selection_advice_id is not None
    with repository.transaction():
        repository.connection.execute(
            "UPDATE selection_advices SET intended_mega = ? WHERE advice_id = ?",
            (intended_mega, session.current_selection_advice_id),
        )
    assert controller.refresh().advice is not None
    assert controller.refresh().advice.intended_mega == intended_mega


def test_schema_23_migration_and_fresh_repository_default(tmp_path: Path) -> None:
    db_path = tmp_path / "mega-migration.db"
    from maple_next.application.match_service import MatchApplication
    from maple_next.persistence.sqlite import SQLiteRepository

    repository = SQLiteRepository(db_path)
    application = MatchApplication(repository, tmp_path / "export")
    session = application.new_match()
    columns = {
        str(row["name"])
        for row in repository.connection.execute("PRAGMA table_info(battle_sessions)")
    }
    assert SCHEMA_VERSION == 23
    assert "mega_state_json" in columns
    assert repository.get_mega_state(session.session_id) == MegaBattleState()
    repository.close()

    reopened = SQLiteRepository(db_path)
    assert reopened.get_mega_state(session.session_id) == MegaBattleState()
    reopened.close()


@pytest.mark.parametrize(
    ("pokemon_name", "expected_form"),
    [("メタグロス", "メガメタグロス"), ("ラグラージ", "メガラグラージ")],
)
def test_real_result_entry_self_mega_persists_known_form(
    tmp_path: Path, pokemon_name: str, expected_form: str
) -> None:
    repository, controller, window, _transport = _prepare_window(
        tmp_path, self_active=pokemon_name
    )
    try:
        _open_result(window)
        assert window.self_mega_button.isVisible()
        assert window.opponent_mega_button.isVisible()
        _stage_mega(window, MegaSide.SELF)
        summary = _result_summary_text(window)
        assert "✓ 自分：" in summary
        assert pokemon_name in summary
        assert expected_form in summary
        # Mega remains outside the existing SideDelta projection.
        assert window.self_delta_editor.to_side_delta().active.observation.value == "UNKNOWN"

        window.next_turn_button.click()
        state = controller.mega_battle_state()
        assert state.self_side.mega_used is True
        assert state.self_side.mega_pokemon == pokemon_name
        assert state.self_side.current_form == expected_form
        assert state.self_side.confirmed_turn == 1
        assert state.opponent_side.mega_used is False
    finally:
        window.close()
        repository.close()


def test_real_result_entry_opponent_unknown_form_is_not_guessed(tmp_path: Path) -> None:
    repository, controller, window, _transport = _prepare_window(tmp_path)
    try:
        _open_result(window)
        _stage_mega(window, MegaSide.OPPONENT)
        summary = _result_summary_text(window)
        assert "✓ 相手：Garchomp → メガ進化（形態未確定）" in summary
        assert "Mega Garchomp" not in summary
        _commit_result_without_advancing(window)
        state = controller.mega_battle_state()
        assert state.opponent_side.mega_used is True
        assert state.opponent_side.mega_pokemon == "Garchomp"
        assert state.opponent_side.current_form is None
    finally:
        window.close()
        repository.close()


def test_intended_mega_does_not_activate_and_actual_can_differ(tmp_path: Path) -> None:
    repository, controller, window, _transport = _prepare_window(
        tmp_path, self_active="ラグラージ"
    )
    try:
        _set_selection_intent(repository, controller, "メタグロス")
        assert controller.mega_battle_state() == MegaBattleState()
        _open_result(window)
        assert controller.mega_battle_state() == MegaBattleState()
        _stage_mega(window, MegaSide.SELF)
        _commit_result_without_advancing(window)
        state = controller.mega_battle_state()
        assert state.self_side.mega_pokemon == "ラグラージ"
        assert state.self_side.current_form == "メガラグラージ"
        assert repository.connection.execute(
            "SELECT intended_mega FROM selection_advices"
        ).fetchone()["intended_mega"] == "メタグロス"
    finally:
        window.close()
        repository.close()


def test_both_sides_use_once_and_persisted_use_disables_real_control(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = _prepare_window(tmp_path)
    try:
        _open_result(window)
        _stage_mega(window, MegaSide.SELF)
        _stage_mega(window, MegaSide.OPPONENT)
        _commit_result_without_advancing(window)
        state = controller.mega_battle_state()
        assert state.self_side.mega_used is True
        assert state.opponent_side.mega_used is True

        # Re-entering the real Result Entry surface after the resource is
        # spent must disable the corresponding controls; the pure domain
        # test separately proves a bypassed duplicate fails closed.
        controller.next_turn()
        window.render_view()
        _fill_confirmed_turn_facts(
            window, self_active="ラグラージ", opponent_active=DEFAULT_OPPONENT_ACTIVE
        )
        window._on_confirm_turn_facts()  # noqa: SLF001
        _confirm_legal_switches_honestly(window)
        window.mock_turn_action_type_box.setCurrentText("MOVE")
        window.mock_turn_action_name_box.setCurrentText(DEFAULT_MOVE)
        window.mock_turn_prediction_input.setText("fake prediction")
        window.mock_turn_rationale_input.setText("fake/injected result-entry test")
        window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
        window.actual_action_type_box.setCurrentText("MOVE")
        window.actual_action_name_box.setCurrentText(DEFAULT_MOVE)
        window.actual_action_confirm_checkbox.setChecked(True)
        _open_result(window)
        assert window.self_mega_button.isEnabled() is False
        assert window.opponent_mega_button.isEnabled() is False
        window.self_mega_button.click()
        assert not any(event.kind == "mega" for event in window._result_events)  # noqa: SLF001
    finally:
        window.close()
        repository.close()


def test_fresh_repository_reader_preserves_actual_mega_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = _prepare_window(tmp_path)
    session = repository.load_active_session()
    assert session is not None
    db_path = repository.database_path
    try:
        _open_result(window)
        _stage_mega(window, MegaSide.SELF)
        _commit_result_without_advancing(window)
        expected = controller.mega_battle_state()
    finally:
        window.close()
        repository.close()

    from maple_next.persistence.sqlite import SQLiteRepository

    reopened = SQLiteRepository(db_path)
    try:
        assert reopened.get_mega_state(session.session_id) == expected
    finally:
        reopened.close()


def test_next_rich_turn_request_is_v8_and_contains_actual_mega_state(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = _prepare_window(tmp_path)
    try:
        first_request = transport.calls[-1][0]
        _open_result(window)
        _stage_mega(window, MegaSide.SELF)
        _commit_result_without_advancing(window)
        controller.next_turn()
        window.render_view()
        _fill_confirmed_turn_facts(
            window, self_active="メタグロス", opponent_active=DEFAULT_OPPONENT_ACTIVE
        )
        window._on_confirm_turn_facts()  # noqa: SLF001
        _confirm_legal_switches_honestly(window)
        window.mock_turn_action_type_box.setCurrentText("MOVE")
        window.mock_turn_action_name_box.setCurrentText(DEFAULT_MOVE)
        window.mock_turn_prediction_input.setText("fake prediction")
        window.mock_turn_rationale_input.setText("fake/injected result-entry test")
        window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
        request = transport.calls[-1][0]
        assert request.contract_version == "maple-turn-advice.v8"
        canonical = canonical_rich_request_dict(request)
        assert canonical["mega_state"] == mega_state_to_canonical_dict(
            controller.mega_battle_state()
        )
        assert request.mega_state.self_side.mega_used is True
        assert first_request.request_hash != request.request_hash

        changed_state = MegaBattleState().record_use(
            side=MegaSide.OPPONENT,
            pokemon_name="Garchomp",
            current_form=None,
            confirmed_turn=1,
            confirmed_at_utc="2026-08-29T10:00:00+00:00",
        )
        changed_request = replace(request, mega_state=changed_state)
        assert rich_request_payload_hash(changed_request) != rich_request_payload_hash(request)
    finally:
        window.close()
        repository.close()


def test_historical_v7_and_current_v8_use_response_parser_v2() -> None:
    assert select_response_parser_version("maple-turn-advice.v7") == "v2"
    assert select_response_parser_version("maple-turn-advice.v8") == "v2"


def test_rich_match_export_contains_canonical_mega_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = _prepare_window(tmp_path)
    application = controller._application  # noqa: SLF001
    try:
        _open_result(window)
        _stage_mega(window, MegaSide.SELF)
        _commit_result_without_advancing(window)
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        record = application.export_match()
        raw = Path(record.export_path).read_bytes()
        payload = json.loads(raw)
        assert payload["mega_state"]["self"]["mega_used"] is True
        assert payload["mega_state"]["self"]["mega_pokemon"] == "メタグロス"
        assert payload["action_history"][0]["action_type"] == "MOVE"
        assert "mega" not in {
            str(item["action_type"]).lower() for item in payload["action_history"]
        }
        parse_match_export_v4(raw)
    finally:
        window.close()
        repository.close()
