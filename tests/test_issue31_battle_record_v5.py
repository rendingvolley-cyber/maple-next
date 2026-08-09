"""Acceptance for Issue #31 Battle Record v5 HTML parity, through 5225359921."""

from __future__ import annotations

import ast
import json
import os
from dataclasses import fields
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QGroupBox, QLabel
from test_issue31_turn_state_ui_bundle_c import (
    SyncDispatch,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.application.match_service import MatchApplication
from maple_next.domain.effect_catalog import (
    EFFECT_CATALOG,
    SHOWDOWN_SOURCE_COMMIT,
    find_effect,
)
from maple_next.domain.opponent_intel import (
    LocalJsonOpponentMetaProvider,
    MatchOpponentFacts,
    build_opponent_intel,
    possible_abilities_for_species,
    species_has_entry_relevant_ability,
)
from maple_next.domain.species_ability_catalog import (
    SpeciesCatalogCoverageError,
    canonical_species_ability_catalog,
)
from maple_next.domain.turn_state import ChangeObservation
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_advice_rich_state import RichStateTurnAdviceRequest
from maple_next.providers.turn_transport import FakeTurnAdviceTransport
from maple_next.ui.battle_record_ui import BattleRecordUiWindow, _StateEventDialog
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController


def test_catalog_has_pinned_provenance_and_representative_exact_effects() -> None:
    assert len(EFFECT_CATALOG) >= 50
    assert len({entry.id for entry in EFFECT_CATALOG}) == len(EFFECT_CATALOG)
    assert all(entry.source_commit == SHOWDOWN_SOURCE_COMMIT for entry in EFFECT_CATALOG)
    assert all(find_effect(entry.id) is entry for entry in EFFECT_CATALOG)
    assert all(entry.source_reference.endswith(f"#{entry.id}") for entry in EFFECT_CATALOG)
    assert find_effect("Metal Sound") is find_effect("metalsound")


def test_visual_evidence_harness_has_no_operator_or_capture_calls() -> None:
    harness = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "issue31_field_blocker_visual_evidence.py"
    )
    tree = ast.parse(harness.read_text(encoding="utf-8"))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {
            "new_match",
            "new_match_after_export",
            "apply_selection",
            "start_turn_capture",
            "capture_current_frame",
            "record_actual_action",
            "start",
        }
    )
    assert find_effect("からをやぶる").deterministic_effects == (
        "攻撃+2",
        "防御-1",
        "特攻+2",
        "特防-1",
        "素早さ+2",
    )
    assert find_effect("りゅうのまい").deterministic_effects == ("攻撃+1", "素早さ+1")
    assert find_effect("つるぎのまい").deterministic_effects == ("攻撃+2",)
    assert find_effect("めいそう").deterministic_effects == ("特攻+1", "特防+1")
    assert find_effect("いかく").deterministic_effects == ("攻撃-1",)
    assert find_effect("あめふらし").deterministic_effects == ("天候:雨",)


def test_visual_evidence_manifest_records_authoritative_projection_and_event(
    tmp_path: Path,
) -> None:
    from scripts.issue31_field_blocker_visual_evidence import MATCH_ID, main

    output_directory = tmp_path / "field-blocker-evidence"
    assert main(output_directory) == 0
    manifest = json.loads(
        (output_directory / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["dragonite"]["projection_match_id"] == MATCH_ID
    assert manifest["dragonite"]["canonical_entry_event_identity"] is None
    assert manifest["salamence"]["projection_match_id"] == MATCH_ID
    assert isinstance(manifest["salamence"]["canonical_entry_event_identity"], str)

    repository = SQLiteRepository(output_directory / "salamence" / "evidence.db")
    session = repository.load_active_session()
    assert session is not None
    persisted_events = repository.list_opponent_entry_events(
        session_id=session.session_id,
        match_id=session.match_id,
        generation=session.generation,
    )
    assert manifest["salamence"]["projection_match_id"] == session.match_id
    assert manifest["salamence"]["canonical_entry_event_identity"] == (
        persisted_events[0].event_id
    )
    repository.close()

    assert manifest["operator_commands"] == {"new_match": 0, "apply": 0, "capture": 0}
    assert manifest["real_provider_send"] == 0
    assert manifest["network_send"] == 0
    assert manifest["game_action"] == 0
    for state in (manifest["dragonite"], manifest["salamence"]):
        assert state["selection_provider_calls"] == 0
        assert state["turn_provider_calls"] == 0
        assert state["capture_start"] == 0


def test_fixed_geometry_exact_four_lifecycle_buttons_and_no_legacy_phase(
    tmp_path: Path,
) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)
    assert window.minimumSize().width() == window.maximumSize().width() == 1920
    assert window.minimumSize().height() == window.maximumSize().height() == 1080
    assert tuple(button.text() for button in window.lifecycle_buttons) == (
        "Turn撮影",
        "SEND TURN TO GEMINI",
        "行動・結果記録",
        "NEXT TURN",
    )
    assert "facts/state確定" not in {button.text() for button in window.lifecycle_buttons}
    assert window._bundle_c_gemini_send_button.isHidden()
    repository.close()


def test_visual_remediation_uses_one_four_phase_workbench_surface(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    assert window.workbench_stack.count() == 4
    assert window.workbench_stack.currentWidget() is window.review_workbench_page
    assert window.workbench_stack.maximumHeight() == 350
    assert window.diagnostics_drawer.isHidden()
    assert window.terminal_flow_drawer.isHidden()
    assert window.turn_facts_confirm_checkbox.isHidden()

    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("opponent move")
    window.mock_turn_rationale_input.setText("fake/injected test")
    window._on_trusted_send_turn_to_gemini()
    assert window.workbench_stack.currentWidget() is window.action_workbench_page
    assert not window.action_result_delta_group.isVisible()
    assert set(window.self_action_tabs) == {"MOVE", "SWITCH"}
    assert not window.current_state_group.isVisible()
    repository.close()


def test_visual_remediation_hides_legacy_state_grids_and_presets(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    assert window.self_state_editor.stage_grid_widget.isHidden()
    assert window.opponent_state_editor.stage_grid_widget.isHidden()
    for editor in (window.self_delta_editor, window.opponent_delta_editor):
        assert editor.status_preset_box.isHidden()
        assert editor.event_preset_box.isHidden()
        assert editor.event_preview_button.isHidden()
        assert editor.event_apply_button.isHidden()
        assert editor.detail_section.isHidden()
    repository.close()


def test_final_visual_right_rail_keeps_gemini_above_intel_while_waiting(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    controller.new_match()
    controller.confirm_selection_facts(
        ["Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu"],
        ["Salamence", "Gholdengo", "Dragonite", "Flutter Mane", "Tyranitar", "Pelipper"],
    )
    controller.submit_mock_advice(
        ["Meowscarada", "Gholdengo", "Dragonite"], "Meowscarada"
    )
    controller.apply_selection(
        ["Meowscarada", "Gholdengo", "Dragonite"], "Meowscarada", human_confirmed=True
    )
    window.render_view()

    assert not window.rich_gemini_group.isHidden()
    assert window._right_column_layout.indexOf(window.rich_gemini_group) == 0
    assert window._right_column_layout.indexOf(window.opponent_intel_widget) == 1
    repository.close()


def test_final_visual_common_tools_live_directly_below_live_surface(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    assert window.evidence_open_button.parentWidget() is window.live_tools_bar
    assert window.review_state_event_button.parentWidget() is window.live_tools_bar
    assert window.result_state_event_button.isHidden()
    assert window.evidence_open_button.isEnabled()
    assert window.review_state_event_button.isEnabled()
    repository.close()


def test_final_visual_intel_detail_has_structured_fail_soft_sections(tmp_path: Path) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)
    window.opponent_intel_widget.detail_button.click()

    assert set(window.opponent_intel_widget._detail_sections) == {
        "current_match_facts",
        "moves",
        "abilities",
        "items",
        "source",
    }
    for key in ("moves", "abilities", "items", "source"):
        section = window.opponent_intel_widget._detail_sections[key]
        text = " ".join(label.text() for label in section.findChildren(QLabel))
        assert "データなし" in text
    repository.close()


def test_final_visual_uses_dark_cards_and_active_lifecycle_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    style = window.battle_record_page.styleSheet()
    assert "#07101a" in style
    assert "#091623" in style
    assert window.confirm_turn_facts_button.property("active") is True
    assert window.start_turn_button.property("active") is False
    repository.close()


def test_combined_review_contains_ocr_legal_actions_ability_and_state_helper(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window.opponent_active_input.setText("ボーマンダ")
    assert not window.current_state_editor_container.isVisible()
    assert window.parity_self_status_box.isVisible()
    assert window.parity_opponent_status_box.isVisible()
    assert len(window.move_inputs) >= 1
    assert len(window.switch_checkboxes) >= 1
    assert window.ability_resolution_group.isHidden()
    assert window.review_state_event_button.text() == "＋ 状態変化を記録"
    repository.close()


def test_catalog_candidate_requires_human_apply_before_even_draft_widgets_change(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    before = controller.turn_state_summary().confirmed_state
    entry = find_effect("いかく")
    assert entry is not None
    window.review_effect_candidate.propose(entry)
    assert controller.turn_state_summary().confirmed_state is before
    assert window.self_state_editor.stage_fields["attack_stage"].spin.value() == 0
    window.review_effect_candidate.apply_button.click()
    assert window.self_state_editor.stage_fields["attack_stage"].spin.value() == -1
    assert controller.turn_state_summary().confirmed_state is before
    repository.close()


def test_match_scoped_ability_memory_reuses_known_and_unknown_remains_unresolved(
    tmp_path: Path,
) -> None:
    repository, controller, _window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    entity = "opponent-party-slot-1"
    assert controller.opponent_ability_for_entity(entity) is None
    assert (
        controller.confirm_opponent_ability(
            opponent_entity_id=entity, species="ボーマンダ", ability="いかく"
        )
        == "いかく"
    )
    assert controller.opponent_ability_for_entity(entity) == "いかく"
    assert (
        controller.confirm_opponent_ability(
            opponent_entity_id=entity, species="ボーマンダ", ability="不明"
        )
        is None
    )
    assert controller.opponent_ability_for_entity(entity) is None
    repository.close()


def test_real_projection_match_id_replaces_demo_header(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    assert window.battle_context_label.text() == "Match 未取得   Turn —"
    view = controller.new_match()
    window.render_view(view)

    assert view.projection.match_id is not None
    assert view.projection.match_id in window.battle_context_label.text()
    assert "#demo" not in window.battle_context_label.text()
    repository.close()


def test_entry_ability_catalog_is_species_bound_and_fail_closed() -> None:
    catalog = canonical_species_ability_catalog()
    assert catalog.source_commit == SHOWDOWN_SOURCE_COMMIT
    assert catalog.source_pokedex_path == "data/pokedex.ts"
    assert catalog.source_abilities_path == "data/abilities.ts"
    assert catalog.species_count == 1380
    assert catalog.ability_count == 320
    assert len(catalog.entry_hook_ability_ids) == 69
    assert len(catalog.entry_observable_ability_ids) == 36
    assert all(
        ability_id in catalog.abilities
        for species in catalog.species.values()
        for ability_id in species.ability_ids
    )
    supported_by_ability = {
        ability_id: {
            species.species_id
            for species in catalog.species.values()
            if ability_id in species.ability_ids
        }
        for ability_id in catalog.entry_observable_ability_ids
    }
    assert all(supported_by_ability.values())
    assert all(
        catalog.abilities[ability_id].entry_classification is not None
        for ability_id in catalog.entry_hook_ability_ids
    )
    assert possible_abilities_for_species("Salamence") == ("いかく", "じしんかじょう")
    assert possible_abilities_for_species("Pelipper") == (
        "するどいめ",
        "あめふらし",
        "あめうけざら",
    )
    assert set(possible_abilities_for_species("Salamence")).isdisjoint(
        {"するどいめ", "あめうけざら", "あめふらし"}
    )
    with pytest.raises(SpeciesCatalogCoverageError):
        possible_abilities_for_species("unresolved-species")
    assert species_has_entry_relevant_ability("Salamence") is True
    assert species_has_entry_relevant_ability("Dragonite") is False


def _confirm_initial_entry(window, species: str) -> None:
    _fill_minimal_current_state(window)
    window.opponent_active_input.setText(species)
    assert window.parity_ability_card.isHidden()
    window._on_confirm_turn_facts()


def _advance_with_confirmed_opponent_switch(window, controller, species: str) -> None:
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("opponent switch")
    window.mock_turn_rationale_input.setText("canonical transition fixture")
    window._on_trusted_send_turn_to_gemini()
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("SWITCH")
    window.opponent_action_name_input.setText(species)
    window._on_record_action()
    window._on_next_turn()
    window.render_view()
    window.move_inputs[0].setText("Flower Trick")
    window.opponent_hp_box.setCurrentText("100")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("opponent move")
    window.mock_turn_rationale_input.setText("canonical transition fixture")
    window._on_confirm_turn_facts()
    assert controller.refresh().projection.session_state == "TURN_REVIEWED"


def test_entry_prompt_is_event_triggered_species_bound_and_not_generic(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    window.opponent_active_input.setText("Salamence")
    assert window.parity_ability_card.isHidden()
    assert controller.turn_state_summary().pending_opponent_entry_event is None

    _confirm_initial_entry(window, "Salamence")
    event = controller.turn_state_summary().pending_opponent_entry_event
    assert event is not None
    assert event.entry_ordinal == 1
    assert event.species_id == "salamence"
    assert not window.parity_ability_card.isHidden()
    assert tuple(button.text() for button in window.parity_ability_buttons) == (
        "いかく",
        "じしんかじょう",
        "不明",
    )

    window.render_view()
    assert controller.turn_state_summary().pending_opponent_entry_event == event
    assert not window.parity_ability_card.isHidden()

    window._confirm_parity_ability("不明")
    assert window.parity_ability_card.isHidden()
    _advance_with_confirmed_opponent_switch(window, controller, "Pelipper")
    second_event = controller.turn_state_summary().pending_opponent_entry_event
    assert second_event is not None
    assert second_event.entry_ordinal == 2
    assert second_event.species_id == "pelipper"
    assert tuple(button.text() for button in window.parity_ability_buttons) == (
        "するどいめ",
        "あめふらし",
        "あめうけざら",
        "不明",
    )
    assert "いかく" not in {button.text() for button in window.parity_ability_buttons}

    assert controller.opponent_ability_candidates("unresolved-species") == ()
    repository.close()


def test_confirmed_entry_is_not_reprompted_but_unresolved_reentry_is(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    _confirm_initial_entry(window, "Salamence")
    window._confirm_parity_ability("いかく")
    _advance_with_confirmed_opponent_switch(window, controller, "Pelipper")
    window._confirm_parity_ability("不明")
    _advance_with_confirmed_opponent_switch(window, controller, "Salamence")
    assert window.parity_ability_card.isHidden()

    events = repository.list_opponent_entry_events(
        session_id=controller.turn_state_summary().identity.session_id,
        match_id=controller.turn_state_summary().identity.match_id,
        generation=controller.turn_state_summary().identity.generation,
    )
    assert tuple(event.entry_ordinal for event in events) == (1, 2, 3)
    repository.close()


def test_unresolved_entry_can_prompt_on_later_genuine_reentry(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_initial_entry(window, "Salamence")
    window._confirm_parity_ability("不明")

    _advance_with_confirmed_opponent_switch(window, controller, "Dragonite")
    assert window.parity_ability_card.isHidden()
    _advance_with_confirmed_opponent_switch(window, controller, "Salamence")

    event = controller.turn_state_summary().pending_opponent_entry_event
    assert event is not None
    assert event.entry_ordinal == 3
    assert event.species_id == "salamence"
    assert not window.parity_ability_card.isHidden()
    repository.close()


@pytest.mark.parametrize("species", ["Dragonite", "unresolved-species"])
def test_nonqualifying_or_unresolved_confirmed_entry_is_hidden_and_handled(
    tmp_path: Path, species: str
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_initial_entry(window, species)

    assert window.parity_ability_card.isHidden()
    summary = controller.turn_state_summary()
    assert summary.identity is not None
    assert summary.pending_opponent_entry_event is None
    events = repository.list_opponent_entry_events(
        session_id=summary.identity.session_id,
        match_id=summary.identity.match_id,
        generation=summary.identity.generation,
    )
    assert len(events) == 1
    assert events[0].handled_at_utc is not None
    repository.close()


def test_restart_hydrates_same_pending_entry_without_inventing_event(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_initial_entry(window, "Salamence")
    before = controller.turn_state_summary().pending_opponent_entry_event
    assert before is not None
    database_path = repository.database_path
    window.close()
    repository.close()

    restarted_repository = SQLiteRepository(database_path)
    restarted_application = MatchApplication(restarted_repository, tmp_path / "export")
    restarted_adapter = GeminiRichTurnAdviceAdapter(
        FakeTurnAdviceTransport(), dispatch_factory=SyncDispatch
    )
    restarted_controller = TurnStateFlowController(
        restarted_application,
        restarted_repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        restarted_adapter,
    )
    restarted_window = BattleRecordUiWindow(
        restarted_controller,
        ocr_data_directory=tmp_path / "ocr",
        auto_start_capture=False,
    )
    restarted_window.render_view()

    after = restarted_controller.turn_state_summary().pending_opponent_entry_event
    assert after == before
    assert not restarted_window.parity_ability_card.isHidden()
    assert len(
        restarted_repository.list_opponent_entry_events(
            session_id=after.session_id,
            match_id=after.match_id,
            generation=after.generation,
        )
    ) == 1
    restarted_repository.close()


def test_action_result_progressive_disclosure_and_catalog_apply(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.actual_action_type_box.setCurrentText("SWITCH")
    window._update_v5_action_disclosure()
    assert not window.actual_action_name_box.isHidden()
    window.opponent_action_type_box.setCurrentText("NO ACTION")
    window._update_v5_action_disclosure()
    assert window.opponent_action_name_input.isHidden()
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.opponent_action_name_input.setText("りゅうのまい")
    assert window.result_effect_candidate.pending_entry is find_effect("りゅうのまい")
    window.result_effect_candidate.apply_button.click()
    assert (
        window.opponent_delta_editor.stage_fields["attack_stage"].to_delta().observation
        is ChangeObservation.CHANGED
    )
    repository.close()


def _advance_to_action_result(window: BattleRecordUiWindow) -> None:
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("manual test prediction")
    window.mock_turn_rationale_input.setText("fake transport only")
    window._on_trusted_send_turn_to_gemini()
    assert window._bundle_c_controller.refresh().projection.primary_cta == (
        "RECORD_ACTUAL_ACTION"
    )


def test_action_result_draft_survives_same_identity_render(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)

    window.self_action_move_buttons[0].click()
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.parity_opponent_action_input.setText("manual opponent move")
    window.action_order_box.setCurrentText("OPPONENT_FIRST")
    assert window.actual_action_confirm_checkbox.isChecked()
    assert window.record_action_button.isEnabled()

    window.render_view()

    assert window.actual_action_type_box.currentText() == "MOVE"
    assert window.actual_action_name_box.currentText() == "Flower Trick"
    assert window.actual_action_confirm_checkbox.isChecked()
    assert window.record_action_button.isEnabled()
    assert window.opponent_action_type_box.currentText() == "MOVE"
    assert window.parity_opponent_action_input.text() == "manual opponent move"
    assert window.action_order_box.currentText() == "OPPONENT_FIRST"
    assert transport.call_count == 1
    repository.close()


def test_opponent_action_is_manual_without_invented_move_candidates(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)

    assert not hasattr(window, "parity_opponent_move_box")
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.parity_opponent_action_input.setText("人間が確認した技")
    window.render_view()
    assert window.opponent_action_name_input.text() == "人間が確認した技"
    assert window.parity_opponent_action_input.text() == "人間が確認した技"

    window.parity_opponent_unknown_button.click()
    window.render_view()
    assert window.opponent_action_name_input.text() == "不明"
    assert window.parity_opponent_action_input.text() == "不明"
    repository.close()


def test_self_switch_selector_is_limited_to_reviewed_legal_targets(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)

    legal_switches = window._bundle_c_controller.refresh().turn_facts
    assert legal_switches is not None
    expected = legal_switches.legal_switches
    actual = tuple(
        window.self_switch_target_box.itemText(index)
        for index in range(window.self_switch_target_box.count())
    )
    assert actual == expected
    window.self_action_tabs["SWITCH"].click()
    window.self_switch_target_box.setCurrentText(expected[0])
    window.render_view()
    assert window.actual_action_type_box.currentText() == "SWITCH"
    assert window.actual_action_name_box.currentText() == expected[0]
    assert window.self_switch_target_box.currentText() == expected[0]
    repository.close()


def test_self_switch_selector_explicitly_reports_empty_legal_targets(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    for checkbox in window.switch_checkboxes:
        checkbox.setChecked(False)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("manual test prediction")
    window.mock_turn_rationale_input.setText("fake transport only")
    window._on_trusted_send_turn_to_gemini()

    window.self_action_tabs["SWITCH"].click()
    assert window.self_switch_target_box.count() == 0
    assert not window.self_switch_target_box.isEnabled()
    assert not window.self_switch_unavailable_label.isHidden()
    repository.close()


def test_no_explicit_unchanged_changed_controls_are_exposed(tmp_path: Path) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)
    for editor in (window.self_delta_editor, window.opponent_delta_editor):
        assert editor.hp_field.mode_box.isHidden()
        assert editor.status_field.mode_box.isHidden()
        assert all(field.mode_box.isHidden() for field in editor.stage_fields.values())
    repository.close()


def test_timing_grouped_state_dialog_previews_before_apply(tmp_path: Path) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)
    applied: list[str] = []
    dialog = _StateEventDialog(
        window,
        context="review",
        apply_callback=lambda entry: applied.append(entry.id),
    )
    titles = {group.title() for group in dialog.findChildren(QGroupBox)}
    assert "登場・ターン開始でよく起きること" in titles
    assert "行動後によく起きること" in titles
    entry = find_effect("あめふらし")
    assert entry is not None
    dialog._preview(entry)
    assert applied == []
    dialog.apply_button.click()
    assert applied == ["drizzle"]
    repository.close()


def test_local_meta_cache_and_absent_cache_fail_soft_without_runtime_network(
    tmp_path: Path,
) -> None:
    missing = LocalJsonOpponentMetaProvider(tmp_path / "missing.json")
    absent = build_opponent_intel(
        species="ボーマンダ", match_facts=MatchOpponentFacts(), provider=missing
    )
    assert absent.data_status == "データなし"

    cache = tmp_path / "meta.json"
    cache.write_text(
        json.dumps(
            {
                "regulation": "Champions test",
                "snapshot_date": "2026-08-08",
                "source": "verified-local-fixture",
                "species": {
                    "ボーマンダ": {
                        "moves": [{"name": "りゅうのまい", "percentage": 40.0}],
                        "abilities": [{"name": "いかく", "percentage": 70.0}],
                        "items": [{"name": "ラムのみ", "percentage": 20.0}],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    provider = LocalJsonOpponentMetaProvider(cache)
    meta_only = build_opponent_intel(
        species="ボーマンダ",
        match_facts=MatchOpponentFacts(),
        provider=provider,
    )
    assert meta_only.observed_moves == ()
    assert meta_only.meta is not None
    assert tuple(entry.name for entry in meta_only.meta.moves) == ("りゅうのまい",)
    confirmed = build_opponent_intel(
        species="ボーマンダ",
        match_facts=MatchOpponentFacts(ability="じしんかじょう", moves=("まもる",)),
        provider=provider,
    )
    assert confirmed.ability == "じしんかじょう"
    assert confirmed.observed_moves == ("まもる",)
    assert confirmed.meta is not None


def test_intel_meta_is_not_part_of_rich_gemini_contract() -> None:
    names = {field.name for field in fields(RichStateTurnAdviceRequest)}
    assert "opponent_intel" not in names
    assert "opponent_meta" not in names
    assert "population_statistics" not in names
