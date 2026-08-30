"""Focused acceptance coverage for the production Result Entry redesign."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from test_issue31_turn_state_ui_bundle_c import (
    _advance_to_turn_capture_pending,
    _confirm_legal_switches_honestly,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.enums import HpBucket
from maple_next.domain.legal_switches import is_confirmed_fainted
from maple_next.domain.turn_state import ChangeObservation


def _reach_action_entry(window, controller, *, own_move: str = "Flower Trick") -> None:
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.move_inputs[0].setText(own_move)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText(own_move)
    window.mock_turn_prediction_input.setText("fake prediction")
    window.mock_turn_rationale_input.setText("fake/injected result-entry test")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText(own_move)
    window.actual_action_confirm_checkbox.setChecked(True)


def _open_result(window) -> None:
    window.record_action_button.click()
    assert window._result_entry_active is True  # noqa: SLF001
    assert window.action_result_step_stack.currentWidget() is window.result_workbench_page


def _result_summary_text(window) -> str:
    texts: list[str] = []
    for index in range(window.result_summary_layout.count()):
        item = window.result_summary_layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            texts.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(texts)


def test_real_result_page_has_only_event_controls_and_navigation(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)
    window.header_tabs.setCurrentWidget(window.battle_record_page)
    window.show()
    QApplication.processEvents()

    assert window.result_actions_label.text().startswith("このTurnの行動")
    assert window.record_self_faint_button.isVisible()
    assert window.record_opponent_faint_button.isVisible()
    assert window.manual_result_button.isVisible()
    assert window.result_summary_empty_label.text() == "追加イベントなし"
    assert window.action_result_delta_group.isHidden()
    assert window.self_delta_editor.isHidden()
    assert window.opponent_delta_editor.isHidden()
    assert window.next_turn_button.isEnabled()
    assert transport.call_count == 1  # injected fake transport only
    repository.close()


def test_torch_song_requires_occurred_then_persists_plus_one(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller, own_move="Torch Song")
    before = controller.turn_state_summary().confirmed_state
    assert before is not None
    assert before.self_side.special_attack_stage.value == 0
    assert controller.turn_state_summary().latest_delta is None

    _open_result(window)
    card = window._result_candidate_cards["self:torchsong:0"]  # noqa: SLF001
    assert "特攻+1" in card.findChild(type(window.result_actions_label)).text()
    # Candidate visibility alone never mutates canonical state.
    assert controller.turn_state_summary().latest_delta is None
    card.occurred_button.click()
    assert window.self_delta_editor.to_side_delta().special_attack_stage.after_value == 1

    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.special_attack_stage.value == 1
    repository.close()


def test_explicit_screech_apply_stages_summary_and_persists_defense_minus_two(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller, own_move="Screech")
    _open_result(window)

    assert "防御-2" in window.result_effect_candidate.summary_label.text()
    window.result_effect_candidate.apply_button.click()

    delta = window.opponent_delta_editor.to_side_delta()
    assert delta.defense_stage.observation is ChangeObservation.CHANGED
    assert delta.defense_stage.after_value == -2
    assert "✓ 相手：" in _result_summary_text(window)
    assert "防御 -2" in _result_summary_text(window)

    window.next_turn_button.click()
    persisted = repository.list_action_result_deltas_based_on(
        controller.turn_state_summary().confirmed_state.confirmed_state_id
    )[-1]
    assert persisted.opponent_side.defense_stage.observation is ChangeObservation.CHANGED
    assert persisted.opponent_side.defense_stage.after_value == -2
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.opponent_side.defense_stage.value == -2
    repository.close()


def test_direct_speed_plus_two_apply_uses_result_draft_and_mutates_once(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)

    window.review_state_event_button.click()
    dialog = window._direct_stage_editor_dialog  # noqa: SLF001
    dialog._on_adjust("speed_stage", 1)  # noqa: SLF001
    dialog._on_adjust("speed_stage", 1)  # noqa: SLF001
    dialog.apply_button.click()

    delta = window.self_delta_editor.to_side_delta()
    assert delta.speed_stage.observation is ChangeObservation.CHANGED
    assert delta.speed_stage.after_value == 2
    assert "素早さ +2" in _result_summary_text(window)

    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.speed_stage.value == 2
    repository.close()


def test_direct_apply_projects_all_seven_stage_controls(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)

    window.review_state_event_button.click()
    dialog = window._direct_stage_editor_dialog  # noqa: SLF001
    expected = {
        "attack_stage": 1,
        "defense_stage": -1,
        "special_attack_stage": 1,
        "special_defense_stage": -1,
        "speed_stage": 1,
        "accuracy_stage": 1,
        "evasion_stage": -1,
    }
    for field_name, amount in expected.items():
        dialog._on_adjust(field_name, amount)  # noqa: SLF001
    dialog.apply_button.click()

    side_delta = window.self_delta_editor.to_side_delta()
    for field_name, value in expected.items():
        field_delta = getattr(side_delta, field_name)
        assert field_delta.observation is ChangeObservation.CHANGED
        assert field_delta.after_value == value

    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    for field_name, value in expected.items():
        assert getattr(draft.self_side, field_name).value == value
    repository.close()


def test_manual_defense_minus_two_can_be_removed_before_next_turn(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)

    window.manual_result_button.click()
    dialog = window._manual_result_dialog  # noqa: SLF001
    dialog.stage_box.setCurrentIndex(dialog.stage_box.findData("defense_stage"))
    dialog.amount_box.setCurrentIndex(dialog.amount_box.findData(-2))
    add_button = next(
        button for button in dialog.findChildren(QPushButton) if button.text() == "追加"
    )
    add_button.click()
    assert window.self_delta_editor.to_side_delta().defense_stage.after_value == -2
    assert "防御 -2" in _result_summary_text(window)

    event_id = window._result_events[-1].event_id  # noqa: SLF001
    window._remove_result_event(event_id)  # noqa: SLF001
    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (
        ChangeObservation.UNKNOWN
    )
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    window.next_turn_button.click()
    persisted = repository.list_action_result_deltas_based_on(confirmed.confirmed_state_id)[-1]
    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert not draft.self_side.defense_stage.is_confirmed
    repository.close()


def test_probabilistic_no_proc_writes_no_delta(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.opponent_action_name_input.setText("Shadow Ball")
    _open_result(window)

    card = window._result_candidate_cards["opponent:shadowball:0"]  # noqa: SLF001
    card.did_not_occur_button.click()
    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.special_defense_stage.value == 0
    delta = repository.list_action_result_deltas_based_on(
        controller.turn_state_summary().confirmed_state.confirmed_state_id
    )[-1]
    assert delta.self_side.special_defense_stage.observation is ChangeObservation.UNCHANGED
    assert delta.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert delta.self_side.active.observation is ChangeObservation.UNKNOWN
    assert delta.self_side.attack_stage.observation is ChangeObservation.UNKNOWN
    repository.close()


def test_probabilistic_proc_manual_stage_and_status_persist(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.opponent_action_name_input.setText("Shadow Ball")
    _open_result(window)
    window._result_candidate_cards["opponent:shadowball:0"].occurred_button.click()  # noqa: SLF001
    window._add_manual_result_event("opponent", "special_defense_stage", -1)  # noqa: SLF001
    window._add_manual_result_event("opponent", "status", "やけど")  # noqa: SLF001

    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.special_defense_stage.value == -1
    assert draft.opponent_side.special_defense_stage.value == -1
    assert draft.opponent_side.status.value == "やけど"
    repository.close()


def test_double_faint_and_event_removal_share_existing_hp_zero(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    self_active = confirmed.self_side.active.value
    opponent_active = confirmed.opponent_side.active.value
    assert self_active is not None and opponent_active is not None
    _open_result(window)
    window.record_self_faint_button.click()
    window.record_opponent_faint_button.click()
    assert len(window._result_events) == 2  # noqa: SLF001
    removed_id = window._result_events[0].event_id  # noqa: SLF001
    window._remove_result_event(removed_id)  # noqa: SLF001
    assert len(window._result_events) == 1  # noqa: SLF001
    window.record_self_faint_button.click()
    assert len(window._result_events) == 2  # noqa: SLF001

    window.next_turn_button.click()
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.hp_bucket.value is HpBucket.ZERO
    assert draft.opponent_side.hp_bucket.value is HpBucket.ZERO
    session = repository.load_active_session()
    assert session is not None
    self_memory = repository.get_pokemon_local_state(
        session_id=session.session_id,
        match_id=session.match_id,
        generation=session.generation,
        side="SELF",
        pokemon_name=self_active,
    )
    opponent_memory = repository.get_pokemon_local_state(
        session_id=session.session_id,
        match_id=session.match_id,
        generation=session.generation,
        side="OPPONENT",
        pokemon_name=opponent_active,
    )
    assert is_confirmed_fainted(self_memory)
    assert is_confirmed_fainted(opponent_memory)
    rows = repository.connection.execute(
        "SELECT side, pokemon_name, COUNT(*) AS n FROM pokemon_local_state "
        "WHERE hp_bucket_json LIKE '%\"value\": \"0\"%' "
        "GROUP BY side, pokemon_name"
    ).fetchall()
    assert {(row["side"], row["pokemon_name"], row["n"]) for row in rows} >= {
        ("SELF", self_active, 1),
        ("OPPONENT", opponent_active, 1),
    }
    applied = controller.refresh().applied_selection
    assert applied is not None
    replacement = next(name for name in applied.selected_three if name != self_active)
    future_candidates = controller.derive_legal_switch_candidates_for_active(replacement)
    assert self_active not in future_candidates
    assert len(applied.selected_three) - 1 == 2  # one unique confirmed faint, exactly once
    repository.close()


def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    _open_result(window)
    assert window._result_events == []  # noqa: SLF001
    window.next_turn_button.click()
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    persisted = repository.list_action_result_deltas_based_on(
        confirmed.confirmed_state_id
    )[-1]
    assert persisted.self_side.active.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.active.observation is ChangeObservation.UNKNOWN
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.status.observation is ChangeObservation.UNKNOWN
    assert persisted.weather.observation is ChangeObservation.UNKNOWN
    assert persisted.terrain.observation is ChangeObservation.UNKNOWN
    assert not draft.self_side.active.is_confirmed
    assert not draft.self_side.hp_bucket.is_confirmed
    repository.close()


def test_opponent_faint_keeps_unobserved_self_hp_unknown(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller, own_move="Wave Crash")
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    _open_result(window)
    window.record_opponent_faint_button.click()
    window.next_turn_button.click()

    persisted = repository.list_action_result_deltas_based_on(
        confirmed.confirmed_state_id
    )[-1]
    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.CHANGED
    assert persisted.opponent_side.hp_bucket.after_value is HpBucket.ZERO
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    repository.close()
