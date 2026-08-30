"""Tournament P0: FAINT / KO recording from the normal Action Result surface.

Adds two always-available controls to the production Battle Record Action
Result UI (``ui/battle_record_ui.py``):

    [ 相手ひんし ]   [ 自分ひんし ]

Neither button invents a faint concept. Each only quick-sets its side's
existing result-delta HP field to ``HpBucket.ZERO`` (``_DeltaHpField.
set_fainted`` -> ``_SideDeltaEditor.mark_fainted``). The operator's
unchanged "行動・結果記録" click is still the sole persistence: it reads
``to_side_delta()`` and writes the one canonical
:class:`~maple_next.domain.turn_state.ActionResultDelta`. From there the
existing lifecycle (delta -> next :class:`ConfirmedTurnState` ->
:class:`PokemonLocalMemory`) and the single canonical predicate
``domain.legal_switches.is_confirmed_fainted`` govern legal switches, the
provider-ready gate, and match export.

Reuses the ``test_issue31_turn_state_ui_bundle_c`` fixtures verbatim -- no
new team/session/schema is introduced.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    _advance_to_turn_capture_pending,
    _confirm_legal_switches_honestly,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.enums import HpBucket
from maple_next.domain.legal_switches import is_confirmed_fainted
from maple_next.domain.turn_state import (
    ChangeObservation,
    KnowledgeStatus,
    known_to_json,
    side_delta_to_json,
)

_FAINTED = SELECTED_THREE[0]  # Meowscarada -- the self Pokemon we faint
_SWITCH_IN = SELECTED_THREE[1]  # Gholdengo
_BACKLINE = SELECTED_THREE[2]  # Dragonite


def _advance_to_record_action_phase(window, controller) -> None:
    """Drive the real UI to the phase where the Action Result surface shows."""

    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.render_view()


# --- 1 & 2: both buttons visible on the ACTUAL production Action Result UI ---


def test_faint_buttons_present_and_visible_on_production_action_result_surface(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_action_button.click()

    assert window.record_opponent_faint_button.text() == "相手ひんし"
    assert window.record_self_faint_button.text() == "自分ひんし"
    # They live directly on the genuine Result Entry page, not in a dialog
    # or behind the removed generic HP editor.
    assert window.action_result_step_stack.currentWidget() is window.result_workbench_page
    assert window.record_opponent_faint_button.parentWidget().title() == "ひんし"
    assert window.record_self_faint_button.parentWidget().title() == "ひんし"
    assert window.action_result_delta_group.isHidden()
    assert not window.record_opponent_faint_button.isHidden()
    assert not window.record_self_faint_button.isHidden()
    repository.close()


# --- 3 & 4: each button feeds the existing canonical HP delta channel -------


def test_self_faint_button_sets_zero_hp_on_the_existing_delta_channel(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)

    window.record_self_faint_button.click()

    self_delta = window.self_delta_editor.to_side_delta()
    assert self_delta.hp_bucket.observation is ChangeObservation.CHANGED
    assert self_delta.hp_bucket.after_value is HpBucket.ZERO
    # No parallel faint field: the faint lives only on hp_bucket, every
    # other channel of the same SideDelta is still an ordinary UNCHANGED.
    assert self_delta.active.observation is ChangeObservation.UNCHANGED
    assert self_delta.status.observation is ChangeObservation.UNCHANGED
    # The opponent side was not touched.
    opp_delta = window.opponent_delta_editor.to_side_delta()
    assert opp_delta.hp_bucket.observation is ChangeObservation.UNCHANGED
    repository.close()


def test_opponent_faint_button_sets_zero_hp_on_the_existing_delta_channel(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)

    window.record_opponent_faint_button.click()

    opp_delta = window.opponent_delta_editor.to_side_delta()
    assert opp_delta.hp_bucket.observation is ChangeObservation.CHANGED
    assert opp_delta.hp_bucket.after_value is HpBucket.ZERO
    self_delta = window.self_delta_editor.to_side_delta()
    assert self_delta.hp_bucket.observation is ChangeObservation.UNCHANGED
    repository.close()


# --- 4 & 6: opponent faint persists via 行動・結果記録 and survives NEXT TURN --


def test_opponent_faint_persists_through_record_action_and_survives_next_turn(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)

    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_opponent_faint_button.click()
    window._on_record_action()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.latest_delta is not None
    opp_hp = summary.latest_delta.opponent_side.hp_bucket
    assert opp_hp.observation is ChangeObservation.CHANGED
    assert opp_hp.after_value is HpBucket.ZERO

    # NEXT TURN: the canonical apply-delta lifecycle carries HP=0 into the
    # derived draft's opponent SideState -- no revival, no re-derivation.
    window._on_next_turn()  # noqa: SLF001
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.opponent_side.hp_bucket.status is KnowledgeStatus.CONFIRMED
    assert draft.opponent_side.hp_bucket.value is HpBucket.ZERO
    repository.close()


# --- 5: a fainted self Pokemon disappears from legal-switch candidates ------


def test_self_faint_excludes_pokemon_from_legal_switch_candidates(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)

    # Turn 1: our active Pokemon faints. Recorded on the normal surface.
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_self_faint_button.click()
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001

    # Turn 2 begins as the forced-switch turn: the fainted Pokemon is still
    # the field Pokemon (draft carry-forward) at HP=0. Confirming those
    # turn-start facts snapshots HP=0 into match-local memory through the
    # one existing writer (_save_pokemon_local_memory).
    window.render_view()
    window.self_active_box.setCurrentText(_FAINTED)
    window.opponent_active_input.setText(OPPONENT_TEAM[0])
    window.self_hp_box.setCurrentText("0")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText("Flower Trick")
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")
    window._on_confirm_turn_facts()  # noqa: SLF001

    # Canonical faint truth is now visible through the existing predicate.
    session = repository.load_active_session()
    memory = repository.get_pokemon_local_state(
        session_id=session.session_id,
        match_id=session.match_id,
        generation=session.generation,
        side="SELF",
        pokemon_name=_FAINTED,
    )
    assert is_confirmed_fainted(memory) is True

    # And the fainted Pokemon is gone from the legal-switch candidate
    # derivation for any later active -- via domain.legal_switches, not a
    # second code path.
    for_switch_in = controller.derive_legal_switch_candidates_for_active(_SWITCH_IN)
    assert _FAINTED not in for_switch_in
    assert for_switch_in == (_BACKLINE,)
    repository.close()


# --- 7: the result reaches match export through existing canonical fields ---


def test_faint_serializes_through_existing_canonical_export_fields(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_record_action_phase(window, controller)

    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.record_opponent_faint_button.click()
    window._on_record_action()  # noqa: SLF001

    delta = controller.turn_state_summary().latest_delta
    assert delta is not None
    # side_delta_to_json is exactly what match_export_v3._delta_to_json
    # embeds for source_action_result_delta -- no bespoke faint key.
    exported_delta = side_delta_to_json(delta.opponent_side)
    assert exported_delta["hp_bucket"] == {
        "observation": "CHANGED",
        "after_value": "0",
        "provenance_chain": ["HUMAN_INPUT"],
    }

    window._on_next_turn()  # noqa: SLF001
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    # known_to_json on opponent_side.hp_bucket is exactly the confirmed-turn
    # block's "opponent_hp_bucket" export field.
    assert known_to_json(draft.opponent_side.hp_bucket)["value"] == "0"
    repository.close()


# --- 8: no duplicate faint model / schema was introduced -------------------


def test_no_duplicate_faint_truth_source_added() -> None:
    """The domain still exposes exactly one faint predicate and no faint
    flag was bolted onto the persisted models."""

    from maple_next.domain import turn_state

    for model_name in ("SideDelta", "SideState", "PokemonLocalMemory", "ActionResultDelta"):
        model = getattr(turn_state, model_name)
        fields = set(getattr(model, "__dataclass_fields__", {}))
        assert not {"fainted", "is_fainted", "faint", "ko", "knocked_out"} & fields, (
            f"{model_name} gained a parallel faint field: {fields}"
        )

    # Faint remains expressed purely as HpBucket.ZERO on the existing
    # hp_bucket channel; the single predicate is is_confirmed_fainted.
    from maple_next.domain import legal_switches

    assert callable(legal_switches.is_confirmed_fainted)
