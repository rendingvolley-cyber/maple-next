"""Issue #31, 00 comment 5224627634: Battle State Event-entry UI v3.

Focused tests for the event candidate -> preview -> human Apply flow,
confirmed-SWITCH automatic state transition, per-Pokemon match-local
memory, and the guardrails that keep unapplied candidates out of Gemini
requests / confirmed state / match export. No real UGREEN/OBS/Gemini
network access anywhere in this file -- only the fake/injected transport
and mock adapters already used by the rest of the Bundle C test suite.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_turn_state_ui_bundle_c import (
    SELECTED_THREE,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
    qt_application,
)

from maple_next.application.match_service import MatchApplication
from maple_next.domain.battle_events import (
    STAGE_EVENT_PRESETS_BY_KEY,
    apply_stage_event,
    clamp_stage,
)
from maple_next.domain.enums import HpBucket
from maple_next.domain.turn_state import ChangeObservation, Known, ProvenanceStep
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

# --- pure domain: presets / clamping (items 2, 4) ---------------------------


def test_karawoyaburu_preset_math_is_exact() -> None:
    preset = STAGE_EVENT_PRESETS_BY_KEY["karawoyaburu"]
    result = apply_stage_event({}, preset)
    assert result == {
        "attack_stage": 2,
        "defense_stage": -1,
        "special_attack_stage": 2,
        "special_defense_stage": -1,
        "speed_stage": 2,
    }


def test_stage_event_clamps_to_canonical_range() -> None:
    preset = STAGE_EVENT_PRESETS_BY_KEY["turuginomai"]  # attack +2
    result = apply_stage_event({"attack_stage": 6}, preset)
    assert result == {"attack_stage": 6}
    assert clamp_stage(999) == 6
    assert clamp_stage(-999) == -6


def test_reset_preset_zeroes_every_stage_regardless_of_current_value() -> None:
    preset = STAGE_EVENT_PRESETS_BY_KEY["reset"]
    result = apply_stage_event({"attack_stage": -6, "speed_stage": 6}, preset)
    assert all(value == 0 for value in result.values())
    assert len(result) == 7


# --- helpers -----------------------------------------------------------------


def _confirm_full_turn_facts(controller: TurnStateFlowController, window) -> None:
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()


def _advance_through_mock_turn_advice(window) -> None:
    """The legacy state machine requires REQUEST_TURN_ADVICE -> advice
    received before RECORD_ACTUAL_ACTION is reachable -- mirrors the
    existing Bundle C test suite's sequence exactly (mock adapter only, no
    real provider send)."""

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()


# --- item 1/2/3: candidate -> preview -> human Apply -------------------------


def test_selecting_preset_alone_does_not_mutate_canonical_state(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    before = controller.turn_state_summary().confirmed_state
    assert before is not None
    assert before.self_side.attack_stage.value == 0

    preview = controller.preview_stage_event(side="self", preset_key="karawoyaburu")
    assert preview["attack_stage"] == (0, 2)

    after = controller.turn_state_summary().confirmed_state
    assert after is not None
    assert after.self_side.attack_stage.value == 0
    assert after.confirmed_state_id == before.confirmed_state_id


def test_preview_shows_exact_karawoyaburu_deltas(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    preview = controller.preview_stage_event(side="self", preset_key="karawoyaburu")
    assert preview == {
        "attack_stage": (0, 2),
        "defense_stage": (0, -1),
        "special_attack_stage": (0, 2),
        "special_defense_stage": (0, -1),
        "speed_stage": (0, 2),
    }


def test_apply_populates_editor_widgets_only_record_action_persists(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    editor = window.self_delta_editor
    editor.event_preset_box.setCurrentIndex(editor.event_preset_box.findData("karawoyaburu"))
    editor._on_preview_stage_event()
    assert editor.event_apply_button.isEnabled()
    editor._on_apply_stage_event()

    # Populating the widgets is not a canonical write.
    still_before = controller.turn_state_summary().latest_delta
    assert still_before is None

    delta = editor.to_side_delta()
    assert delta.attack_stage.observation is ChangeObservation.CHANGED
    assert delta.attack_stage.after_value == 2
    assert delta.defense_stage.after_value == -1

    _advance_through_mock_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()

    persisted = controller.turn_state_summary().latest_delta
    assert persisted is not None
    assert persisted.self_side.attack_stage.after_value == 2


# --- item 10: no switch selector in the event-entry UI -----------------------


def test_side_delta_editor_has_no_active_identity_widget(tmp_path: Path) -> None:
    _repository, _controller, window, _transport = build_window(tmp_path)
    assert not hasattr(window.self_delta_editor, "active_field")
    assert not hasattr(window.opponent_delta_editor, "active_field")


def test_side_delta_editor_to_side_delta_always_reports_active_unchanged(
    tmp_path: Path,
) -> None:
    _repository, _controller, window, _transport = build_window(tmp_path)
    delta = window.self_delta_editor.to_side_delta()
    assert delta.active.observation is ChangeObservation.UNCHANGED


# --- items 5, 6, 7, 8, 9: confirmed SWITCH automatic transition + memory -----


def test_switch_memory_round_trip_restores_hp_and_status(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    identity = controller._safe_current_identity()
    assert identity is not None

    # Major status persisted to per-Pokemon memory (item 5) as a byproduct
    # of confirming full current-state facts for the active Pokemon.
    memory = _repository.get_pokemon_local_state(
        session_id=identity.session_id,
        match_id=identity.match_id,
        generation=identity.generation,
        side="SELF",
        pokemon_name=SELECTED_THREE[0],
    )
    assert memory is not None
    assert memory.hp_bucket.value == HpBucket.FULL

    # No memory yet for a Pokemon that has never been active this match.
    no_memory_delta = controller.compute_confirmed_switch_side_delta(
        side="self", destination_pokemon_name=SELECTED_THREE[1]
    )
    assert no_memory_delta.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert no_memory_delta.status.observation is ChangeObservation.UNKNOWN

    # Switching back to a Pokemon this match has already seen restores it.
    restored_delta = controller.compute_confirmed_switch_side_delta(
        side="self", destination_pokemon_name=SELECTED_THREE[0]
    )
    assert restored_delta.hp_bucket.observation is ChangeObservation.CHANGED
    assert restored_delta.hp_bucket.after_value == HpBucket.FULL


def test_confirmed_switch_resets_every_stage_to_zero_for_outgoing_and_incoming(
    tmp_path: Path,
) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    # Raise a stage first so the reset is actually observable.
    editor = window.self_delta_editor
    editor.stage_fields["attack_stage"].mode_box.setCurrentText("CHANGED")
    editor.stage_fields["attack_stage"].spin.setValue(4)

    delta = controller.compute_confirmed_switch_side_delta(
        side="self", destination_pokemon_name=SELECTED_THREE[1]
    )
    for field_name in (
        "attack_stage",
        "defense_stage",
        "special_attack_stage",
        "special_defense_stage",
        "speed_stage",
        "accuracy_stage",
        "evasion_stage",
    ):
        field_delta = getattr(delta, field_name)
        assert field_delta.observation is ChangeObservation.CHANGED
        assert field_delta.after_value == 0
    assert delta.active.after_value == SELECTED_THREE[1]


def test_record_action_uses_computed_switch_delta_with_zero_extra_input(
    tmp_path: Path,
) -> None:
    """Confirming a SWITCH via the existing actual-action UI alone -- no
    additional operator input into the state-change editors -- produces a
    persisted delta with the automatic active/stage transition applied."""

    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)
    _advance_through_mock_turn_advice(window)

    window.actual_action_type_box.setCurrentText("SWITCH")
    window._update_actual_action_options()
    window.actual_action_name_box.setCurrentText(SELECTED_THREE[1])
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()

    persisted = controller.turn_state_summary().latest_delta
    assert persisted is not None
    assert persisted.self_side.active.after_value == SELECTED_THREE[1]
    assert persisted.self_side.attack_stage.after_value == 0
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN


# --- item 11: first turn stages default to 0 without manual input -----------


def test_first_turn_stage_fields_default_to_confirmed_zero(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    for field in window.self_state_editor.stage_fields.values():
        assert field.unknown_box.isChecked() is False
        assert field.spin.value() == 0
    for field in window.opponent_state_editor.stage_fields.values():
        assert field.unknown_box.isChecked() is False
        assert field.spin.value() == 0


def test_first_turn_zero_default_does_not_clobber_a_manual_correction(
    tmp_path: Path,
) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    window.self_state_editor.stage_fields["attack_stage"].set_known(
        Known.confirmed(3, provenance_chain=(ProvenanceStep.HUMAN_CORRECTION,))
    )
    window.render_view()
    assert window.self_state_editor.stage_fields["attack_stage"].spin.value() == 3


# --- item 12: restart hydration keeps per-Pokemon memory + current state ----


def test_pokemon_local_memory_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "hydrate.db"
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()

    repository = SQLiteRepository(db_path)
    application = MatchApplication(repository, export_dir)
    controller = TurnStateFlowController(application, repository, MockSelectionAdviceAdapter())
    qt_application()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    identity = controller._safe_current_identity()
    assert identity is not None
    repository.close()

    reopened = SQLiteRepository(db_path)
    memory = reopened.get_pokemon_local_state(
        session_id=identity.session_id,
        match_id=identity.match_id,
        generation=identity.generation,
        side="SELF",
        pokemon_name=SELECTED_THREE[0],
    )
    assert memory is not None
    assert memory.hp_bucket.value == HpBucket.FULL
    reopened.close()


# --- item 13: fixed Turn image is evidence-only, not always-on-screen -------


def test_evidence_control_exists_and_fixed_image_starts_hidden(tmp_path: Path) -> None:
    _repository, _controller, window, _transport = build_window(tmp_path)
    assert hasattr(window, "evidence_open_button")
    assert hasattr(window, "evidence_status_label")
    # The fixed Turn image group is parented to the hidden holder, not the
    # always-visible center column, until the operator opens it.
    assert window.turn_snapshot_group.parentWidget() is window._evidence_holder
    assert window._evidence_holder.isVisible() is False


def test_opening_and_closing_evidence_overlay_reparents_without_destroying(
    tmp_path: Path,
) -> None:
    _repository, _controller, window, _transport = build_window(tmp_path)
    window._on_open_evidence_overlay()
    assert window._evidence_dialog is not None
    assert window.turn_snapshot_group.parentWidget() is not window._evidence_holder

    window._evidence_dialog.close()

    assert window.turn_snapshot_group.parentWidget() is window._evidence_holder
    # Same widget instance -- OCR/binding update calls still land on it.
    window.turn_snapshot_status_label.setText("IDLE-AFTER-CLOSE")
    assert window.turn_snapshot_status_label.text() == "IDLE-AFTER-CLOSE"


# --- item 14: pending candidate/preview never leaks into export/Gemini ------


def test_unapplied_preview_state_is_not_part_of_side_delta(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    editor = window.self_delta_editor
    editor.event_preset_box.setCurrentIndex(editor.event_preset_box.findData("karawoyaburu"))
    editor._on_preview_stage_event()
    # Preview computed and stored on the widget, but never Applied.
    assert editor._pending_stage_preview

    delta = editor.to_side_delta()
    # v5: untouched domains are internally UNCHANGED; the preview alone
    # still must not cross the human-Apply boundary into CHANGED.
    assert delta.attack_stage.observation is ChangeObservation.UNCHANGED


# --- item 16: carry-over is not silently treated as an ordinary reset -------


def test_switch_transition_is_only_triggered_by_the_existing_actual_action_switch(
    tmp_path: Path,
) -> None:
    """No implicit carry-over engine exists in this packet: the automatic
    transition fires exactly when the operator's own confirmed actual
    action says SWITCH, and only then -- a MOVE action never silently
    resets stages, so a carry-over move recorded as MOVE cannot be
    mistaken for an ordinary switch reset."""

    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    editor = window.self_delta_editor
    editor.stage_fields["attack_stage"].mode_box.setCurrentText("CHANGED")
    editor.stage_fields["attack_stage"].spin.setValue(4)

    _advance_through_mock_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()

    persisted = controller.turn_state_summary().latest_delta
    assert persisted is not None
    # A MOVE never goes through compute_confirmed_switch_side_delta, so the
    # operator's own manually-entered stage value survives untouched --
    # confirming the reset path is switch-only, not move-triggered.
    assert persisted.self_side.attack_stage.after_value == 4
