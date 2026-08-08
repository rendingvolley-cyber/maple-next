"""Acceptance for Issue #31 Battle Record v5 HTML parity, through 5225359921."""

from __future__ import annotations

import json
import os
from dataclasses import fields
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QGroupBox, QLabel
from test_issue31_turn_state_ui_bundle_c import (
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.effect_catalog import (
    EFFECT_CATALOG,
    SHOWDOWN_SOURCE_COMMIT,
    find_effect,
)
from maple_next.domain.opponent_intel import (
    LocalJsonOpponentMetaProvider,
    MatchOpponentFacts,
    build_opponent_intel,
)
from maple_next.domain.turn_state import ChangeObservation
from maple_next.providers.turn_advice_rich_state import RichStateTurnAdviceRequest
from maple_next.ui.battle_record_ui import _StateEventDialog


def test_catalog_has_pinned_provenance_and_representative_exact_effects() -> None:
    assert len(EFFECT_CATALOG) >= 50
    assert len({entry.id for entry in EFFECT_CATALOG}) == len(EFFECT_CATALOG)
    assert all(entry.source_commit == SHOWDOWN_SOURCE_COMMIT for entry in EFFECT_CATALOG)
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
    assert not window.ability_resolution_group.isHidden()
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
    confirmed = build_opponent_intel(
        species="ボーマンダ",
        match_facts=MatchOpponentFacts(ability="じしんかじょう", moves=("まもる",)),
        provider=provider,
    )
    assert confirmed.ability == "じしんかじょう"
    assert confirmed.moves == ("まもる",)
    assert confirmed.meta is not None


def test_intel_meta_is_not_part_of_rich_gemini_contract() -> None:
    names = {field.name for field in fields(RichStateTurnAdviceRequest)}
    assert "opponent_intel" not in names
    assert "opponent_meta" not in names
    assert "population_statistics" not in names
