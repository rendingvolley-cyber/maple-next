"""Focused regressions for post-action active changes on the two-step result page."""

from __future__ import annotations

from pathlib import Path

from maple_next.domain.enums import HpBucket
from tests.test_two_step_action_result_ui import (
    SELECTED_THREE,
    advance_to_action_phase,
    build_window,
    fill_action,
)


def test_result_page_exposes_manual_stage_editors(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(repository, controller, window)
    fill_action(window)

    window._on_record_action()  # noqa: SLF001

    assert not window.self_delta_editor.detail_section.isHidden()
    assert not window.opponent_delta_editor.detail_section.isHidden()
    assert "能力ランク" in window.self_delta_editor.detail_section.toggle_button.text()
    assert "能力ランク" in window.opponent_delta_editor.detail_section.toggle_button.text()
    assert transport.call_count == 0
    repository.close()


def test_post_move_active_change_keeps_explicit_final_hp_status_and_stage(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(repository, controller, window)
    fill_action(window)

    window._on_record_action()  # noqa: SLF001
    window.result_self_active_box.setCurrentText(SELECTED_THREE[1])

    # Explicit result observations describe the final active after the pivot.
    # They must overlay, not be discarded by, the switch base delta.
    window.self_delta_editor.hp_field.value_box.setCurrentText(HpBucket.SEVENTY_ONE_TO_EIGHTY.value)
    window.self_delta_editor.status_field.mode_box.setCurrentText("CHANGED")
    window.self_delta_editor.status_field.line.setText("burn")
    window.self_delta_editor.stage_fields["attack_stage"].spin.setValue(-1)

    window._on_next_turn()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.identity is not None
    assert summary.identity.turn_number == 2
    assert summary.open_draft is not None
    assert summary.open_draft.self_side.active.value == SELECTED_THREE[1]
    assert summary.open_draft.self_side.hp_bucket.value is HpBucket.SEVENTY_ONE_TO_EIGHTY
    assert summary.open_draft.self_side.status.value == "burn"
    assert summary.open_draft.self_side.attack_stage.value == -1
    assert transport.call_count == 0
    repository.close()


def test_faint_and_replacement_are_not_misrecorded_as_replacement_hp_zero(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    advance_to_action_phase(repository, controller, window)
    fill_action(window)

    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None

    window._on_record_action()  # noqa: SLF001
    window.self_fainted_button.click()
    window.result_self_active_box.setCurrentText(SELECTED_THREE[1])
    window._on_next_turn()  # noqa: SLF001

    # One SideDelta cannot truthfully mean "old active fainted" and also
    # "replacement is the final active with HP 0". Fail closed on the result
    # page; the operator records the faint first, then confirms the forced
    # replacement as the next Turn's current active.
    summary = controller.turn_state_summary()
    assert summary.identity == identity_before
    assert summary.latest_delta is None
    assert controller.refresh().projection.primary_cta == "RECORD_ACTUAL_ACTION"
    assert window._two_step_result_entry is True  # noqa: SLF001
    assert window.workbench_stack.currentWidget() is window.result_entry_workbench_page
    assert "同時確定できません" in window.two_step_result_error_label.text()
    assert transport.call_count == 0
    repository.close()
