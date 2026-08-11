"""Bounded offline regression for Issue #31 latest 00 comment 5247698078."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_battle_record_v5 import _advance_to_action_result
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.opponent_intel import OpponentMetaSnapshot, RankedUsage


class _StaticMetaProvider:
    def __init__(self, snapshot: OpponentMetaSnapshot | None) -> None:
        self.snapshot = snapshot

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        if self.snapshot is None or self.snapshot.species != species:
            return None
        return self.snapshot


def _reach_action_with_advice(window, *, warnings: str = "") -> None:
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("相手は交代を選ぶ可能性が高い")
    window.mock_turn_rationale_input.setText("対面有利を維持できる; 次の展開を限定できる")
    window.mock_turn_warnings_input.setText(warnings)
    window._on_trusted_send_turn_to_gemini()


def test_gemini_recommendation_is_primary_and_audit_is_secondary(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _reach_action_with_advice(window, warnings="相手の先制技に注意")

    assert window.turn_advice_action_label.objectName() == "advicePrimaryAction"
    assert window.turn_advice_action_label.font().pointSize() >= (
        window.turn_advice_source_label.font().pointSize()
    )
    assert window.turn_advice_prediction_label.text() == "相手は交代を選ぶ可能性が高い"
    assert "対面有利" in window.turn_advice_rationale_label.text()
    assert window.turn_advice_warning_card.isVisible()
    assert window.turn_advice_warnings_label.text() == "相手の先制技に注意"
    assert window.turn_advice_audit_group.isAncestorOf(window.turn_advice_source_label)
    assert window.turn_advice_audit_group.isAncestorOf(window.turn_advice_model_label)
    assert not window.turn_advice_audit_group.isChecked()

    repository.close()


def test_warning_card_is_absent_when_response_has_no_warning(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _reach_action_with_advice(window)

    assert window.turn_advice_warnings_label.text() == "—"
    assert window.turn_advice_warning_card.isHidden()
    repository.close()


def test_recorded_opponent_move_feeds_exact_current_intel_without_meta(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)
    window.self_action_move_buttons[0].click()
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("じしん")
    window._on_record_action()

    assert window.opponent_intel_widget._view is not None
    assert window.opponent_intel_widget._view.observed_moves == ("じしん",)
    assert "じしん" in window.opponent_intel_widget.facts_label.text()
    assert window.opponent_intel_widget._view.possible_abilities
    assert controller.opponent_match_facts("Dragonite").moves == ()

    window._on_next_turn()
    assert controller.opponent_match_facts("Garchomp").moves == ("じしん",)
    repository.close()


def test_move_assist_is_grounded_ordered_and_model_refresh_does_not_mutate_draft(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window._opponent_meta_provider = _StaticMetaProvider(
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="local",
            snapshot_date="2026-08-11",
            source="offline-test-cache",
            moves=(RankedUsage("Earthquake", 80.0), RankedUsage("Protect", 25.0)),
        )
    )
    window.render_view()
    _advance_to_action_result(window)
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("人間のfree text")

    window.render_view()

    assert window.opponent_move_suggestions == ("Earthquake", "Protect")
    assert window._opponent_action_suggestion_model.stringList() == [
        "Earthquake",
        "Protect",
    ]
    assert window.parity_opponent_action_input.text() == "人間のfree text"
    window.opponent_action_completer.activated[str].emit("Earthquake")
    assert window.opponent_action_name_input.text() == "Earthquake"
    repository.close()


def test_switch_assist_is_confirmed_roster_minus_current_active(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)

    window.opponent_action_tabs["SWITCH"].click()

    assert window.opponent_switch_suggestions == tuple(
        member for member in OPPONENT_TEAM if member != "Garchomp"
    )
    assert "Garchomp" not in window._opponent_action_suggestion_model.stringList()
    repository.close()


@pytest.mark.parametrize("opponent_type", ["UNKNOWN", "NO ACTION"])
def test_unknown_and_no_action_clear_stale_name_before_persistence(
    tmp_path: Path, opponent_type: str
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _advance_to_action_result(window)
    window.self_action_move_buttons[0].click()
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("stale move")

    window.opponent_action_tabs[opponent_type].click()
    assert window.parity_opponent_action_input.text() == ""
    assert not hasattr(window, "parity_opponent_unknown_button")
    assert sum(
        button.text() == "不明" for button in window.opponent_action_tabs.values()
    ) == 1
    window._on_record_action()

    session = repository.load_active_session()
    assert session is not None and session.current_turn_id is not None
    recorded = repository.get_recorded_action_for_turn(session.current_turn_id)
    assert recorded is not None
    assert recorded.opponent_action_type is None
    assert recorded.opponent_action_name == ""
    repository.close()
