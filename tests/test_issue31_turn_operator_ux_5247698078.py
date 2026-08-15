"""Bounded offline regression for Issue #31 latest 00 comment 5247698078."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maple_next.domain.legal_switches import LegalSwitchStatus as _B2_LegalSwitchStatus

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QSizePolicy
from test_issue31_battle_record_v5 import _advance_to_action_result
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.opponent_intel import OpponentMetaSnapshot, RankedUsage
from maple_next.ocr.contracts import OCR_CANDIDATE_SOURCE, OcrCandidate, OcrFieldKey


class _StaticMetaProvider:
    def __init__(self, snapshot: OpponentMetaSnapshot | None) -> None:
        self.snapshot = snapshot

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        if self.snapshot is None or self.snapshot.species != species:
            return None
        return self.snapshot


def _reach_action_with_advice(window, *, warnings: str = "") -> None:
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=(), status=_B2_LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
    )
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("相手は交代を選ぶ可能性が高い")
    window.mock_turn_rationale_input.setText("対面有利を維持できる; 次の展開を限定できる")
    window.mock_turn_warnings_input.setText(warnings)
    window._on_trusted_send_turn_to_gemini()


def _opponent_identity_candidate(species: str) -> tuple[OcrCandidate, ...]:
    return (
        OcrCandidate(
            field_key=OcrFieldKey.OPPONENT_ACTIVE.value,
            suggested_value=species,
            raw_text="offline-current-turn-identity",
            confidence=0.99,
            rank=1,
            reason="offline timing regression",
            source_frame_id="fresh-current-turn-frame",
            source=OCR_CANDIDATE_SOURCE,
        ),
    )


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


def test_fresh_ocr_identity_immediately_projects_human_only_ability_prompt(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._turn_snapshot_origins[OcrFieldKey.OPPONENT_ACTIVE.value] = (  # noqa: SLF001
        "前Turn確定値の引き継ぎ・未確認"
    )

    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _opponent_identity_candidate("Salamence")
    )

    summary = controller.turn_state_summary()
    assert summary.pending_opponent_entry_event is None
    assert not window.parity_ability_card.isHidden()
    assert tuple(button.text() for button in window.parity_ability_buttons) == (
        "いかく",
        "じしんかじょう",
        "不明",
    )

    # OCR only projects candidates. The explicit human choice is staged
    # until Turn confirmation creates the exact canonical entry event.
    window._confirm_parity_ability("不明")  # noqa: SLF001
    assert window.parity_ability_card.isHidden()
    assert controller.turn_state_summary().pending_opponent_entry_event is None
    window._on_confirm_turn_facts()  # noqa: SLF001
    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=(), status=_B2_LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
    )
    summary = controller.turn_state_summary()
    assert summary.pending_opponent_entry_event is None
    assert summary.identity is not None
    events = repository.list_opponent_entry_events(
        session_id=summary.identity.session_id,
        match_id=summary.identity.match_id,
        generation=summary.identity.generation,
    )
    assert len(events) == 1
    assert events[0].handled_at_utc is not None
    repository.close()


def test_unresolved_ocr_identity_never_projects_generic_ability_candidates(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _opponent_identity_candidate("unresolved-species")
    )

    assert controller.turn_state_summary().pending_opponent_entry_event is None
    assert window.parity_ability_card.isHidden()
    repository.close()


def test_consumed_entry_is_not_reprompted_by_later_same_turn_ocr(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._turn_snapshot_origins[OcrFieldKey.OPPONENT_ACTIVE.value] = (  # noqa: SLF001
        "前Turn確定値の引き継ぎ・未確認"
    )
    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _opponent_identity_candidate("Salamence")
    )
    window._confirm_parity_ability("不明")  # noqa: SLF001
    window._on_confirm_turn_facts()  # noqa: SLF001
    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=(), status=_B2_LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
    )

    window._turn_snapshot_field_locks[OcrFieldKey.OPPONENT_ACTIVE.value] = False  # noqa: SLF001
    window._turn_snapshot_origins[OcrFieldKey.OPPONENT_ACTIVE.value] = (  # noqa: SLF001
        "OCR候補・未確認"
    )
    window._render_ability_resolution()  # noqa: SLF001

    assert window.parity_ability_card.isHidden()
    assert controller.turn_state_summary().pending_opponent_entry_event is None
    repository.close()


def test_right_rail_advice_and_waiting_surfaces_are_content_driven(tmp_path: Path) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)

    assert window.rich_gemini_group.sizePolicy().verticalPolicy() == (
        QSizePolicy.Policy.Maximum
    )
    assert window.turn_advice_group.sizePolicy().verticalPolicy() == (
        QSizePolicy.Policy.Maximum
    )
    assert window.turn_advice_primary_card.sizePolicy().verticalPolicy() == (
        QSizePolicy.Policy.Maximum
    )
    assert window.gemini_empty_label.maximumHeight() == 64
    assert window._right_column_layout.stretch(0) == 0
    assert window._right_column_layout.stretch(1) == 1
    assert window.opponent_intel_widget.sizePolicy().verticalPolicy() == (
        QSizePolicy.Policy.Expanding
    )
    assert window.opponent_intel_widget.maximumHeight() > 10_000
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


def test_move_assist_is_grounded_ordered_and_selector_refresh_does_not_mutate_draft(
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
    assert not hasattr(window, "opponent_action_completer")
    assert [
        window.opponent_action_suggestion_box.itemText(index)
        for index in range(window.opponent_action_suggestion_box.count())
    ] == ["Earthquake", "Protect"]
    assert window.opponent_action_suggestion_box.currentIndex() == -1
    assert window.parity_opponent_action_input.text() == "人間のfree text"
    window.opponent_action_suggestion_box.textActivated.emit("Earthquake")
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
    assert "Garchomp" not in {
        window.opponent_action_suggestion_box.itemText(index)
        for index in range(window.opponent_action_suggestion_box.count())
    }
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
    assert recorded.opponent_action_name is None
    repository.close()
