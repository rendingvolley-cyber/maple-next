"""Focused real-UI regression coverage for the three tournament P0 blockers."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.enums import ActionType, HpBucket
from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.domain.turn_state import ChangeObservation, Known, ProvenanceStep
from maple_next.ocr.contracts import (
    OCR_CANDIDATE_SOURCE,
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateBundle,
    OcrFieldKey,
)
from maple_next.turn_ocr.contracts import TurnSnapshotResult, TurnSnapshotStatus

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)


def _result_summary_text(window) -> str:
    texts: list[str] = []
    for index in range(window.result_summary_layout.count()):
        item = window.result_summary_layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            texts.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(texts)


def _reach_action_entry(window, controller, *, move: str = "Flower Trick") -> None:
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.move_inputs[0].setText(move)
    window._on_confirm_turn_facts()  # noqa: SLF001 - trusted UI seam
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText(move)
    window.mock_turn_prediction_input.setText("injected prediction")
    window.mock_turn_rationale_input.setText("injected regression rationale")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText(move)
    window.actual_action_confirm_checkbox.setChecked(True)


def _open_result(window) -> None:
    window.record_action_button.click()
    assert window._result_entry_active is True  # noqa: SLF001
    assert window.action_result_step_stack.currentWidget() is window.result_workbench_page


def _advance_after_self_faint(window, controller) -> None:
    _reach_action_entry(window, controller)
    _open_result(window)
    window.record_self_faint_button.click()
    window.next_turn_button.click()
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    window.render_view()


def _candidate(field: OcrFieldKey, value: str, *, frame_id: str) -> OcrCandidate:
    return OcrCandidate(
        field_key=field.value,
        suggested_value=value,
        raw_text=value,
        confidence=0.99,
        rank=1,
        reason="fresh current-turn regression candidate",
        source_frame_id=frame_id,
        source=OCR_CANDIDATE_SOURCE,
    )


def _ocr_result(identity, *candidates: OcrCandidate) -> TurnSnapshotResult:
    bundle = OcrCandidateBundle(
        status=OcrBundleStatus.CANDIDATES_READY,
        candidate_only=True,
        manual_entry_allowed=True,
        frame_id="fresh-current-turn-frame",
        frame_captured_at_utc=None,
        frame_age_ms=0,
        candidates=tuple(candidates),
        error_code=None,
        operator_message="fresh current-turn regression result",
    )
    return TurnSnapshotResult(
        identity=identity,
        status=TurnSnapshotStatus.READY,
        bundle=bundle,
        frozen_image=QImage(),
        crops={},
        operator_message="fresh current-turn regression result",
        roi_config_provenance="test fixture",
    )


def _confirm_current_state(window, controller, *, legal_switch_selection=None):
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    return controller.confirm_turn_facts(
        self_active=SELECTED_THREE[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp="100",
        opponent_hp="100",
        legal_moves=("Flower Trick", "Knock Off"),
        legal_switches=(),
        human_note="",
        human_confirmed=True,
        self_side=window.self_state_editor.to_side_state(
            active=Known.confirmed(SELECTED_THREE[0], provenance_chain=_HUMAN),
            hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN),
        ),
        opponent_side=window.opponent_state_editor.to_side_state(
            active=Known.confirmed(OPPONENT_TEAM[0], provenance_chain=_HUMAN),
            hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN),
        ),
        weather=window.weather_field.to_known(),
        terrain=window.terrain_field.to_known(),
        legal_switch_selection=legal_switch_selection,
    )


def test_real_result_entry_faint_controls_are_explicit_reversible_and_draft_only(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)
    window.show()
    QApplication.processEvents()

    assert window.record_self_faint_button.text() == "自分ひんし"
    assert window.record_opponent_faint_button.text() == "相手ひんし"
    assert window.record_self_faint_button.isVisible()
    assert window.record_opponent_faint_button.isVisible()
    assert window.record_self_faint_button.minimumWidth() >= 128
    assert window.record_opponent_faint_button.minimumWidth() >= 128
    assert not window.record_self_faint_button.isChecked()
    assert not window.record_opponent_faint_button.isChecked()
    assert (
        window.self_delta_editor.to_side_delta().hp_bucket.observation
        is ChangeObservation.UNKNOWN
    )
    assert (
        window.opponent_delta_editor.to_side_delta().hp_bucket.observation
        is ChangeObservation.UNKNOWN
    )

    window.record_self_faint_button.click()
    assert window.record_self_faint_button.isChecked()
    assert "✓ 自分：" in _result_summary_text(window)
    assert window.self_delta_editor.to_side_delta().hp_bucket.after_value is HpBucket.ZERO

    window.record_self_faint_button.click()
    assert not window.record_self_faint_button.isChecked()
    assert "✓ 自分：" not in _result_summary_text(window)
    assert (
        window.self_delta_editor.to_side_delta().hp_bucket.observation
        is ChangeObservation.UNKNOWN
    )

    window.record_opponent_faint_button.click()
    assert window.record_opponent_faint_button.isChecked()
    assert "✓ 相手：" in _result_summary_text(window)
    window.render_view()
    assert window.record_opponent_faint_button.isChecked()
    assert window.opponent_delta_editor.to_side_delta().hp_bucket.after_value is HpBucket.ZERO

    window.record_opponent_faint_button.click()
    window.render_view()
    assert not window.record_opponent_faint_button.isChecked()
    assert "✓ 相手：" not in _result_summary_text(window)
    assert (
        window.opponent_delta_editor.to_side_delta().hp_bucket.observation
        is ChangeObservation.UNKNOWN
    )

    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    window.next_turn_button.click()
    persisted = repository.list_action_result_deltas_based_on(confirmed.confirmed_state_id)[-1]
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert not window.record_self_faint_button.isChecked()
    assert not window.record_opponent_faint_button.isChecked()
    repository.close()


def test_fresh_current_turn_ocr_hp_100_replaces_result_zero_and_survives_renders(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path, auto_start_capture=False)
    _advance_after_self_faint(window, controller)

    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.hp_bucket.value is HpBucket.ZERO
    assert window.self_hp_box.currentText() == HpBucket.ZERO.value
    assert not window._turn_snapshot_field_locks[OcrFieldKey.SELF_HP.value]  # noqa: SLF001

    identity = window._turn_snapshot_active_identity  # noqa: SLF001
    assert identity is not None
    fresh = _ocr_result(
        identity,
        _candidate(OcrFieldKey.SELF_ACTIVE, SELECTED_THREE[0], frame_id="fresh-100"),
        _candidate(OcrFieldKey.SELF_HP, "100", frame_id="fresh-100"),
    )
    window._on_turn_snapshot_result(fresh)  # noqa: SLF001
    assert window.self_hp_box.currentText() == "100"

    window.render_view()
    assert window.self_hp_box.currentText() == "100"
    window.render_view()
    assert window.self_hp_box.currentText() == "100"
    repository.close()


def test_current_turn_human_hp_lock_beats_fresh_ocr_and_stale_ocr_is_rejected(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path, auto_start_capture=False)
    _advance_after_self_faint(window, controller)
    identity = window._turn_snapshot_active_identity  # noqa: SLF001
    assert identity is not None

    window._set_turn_field(OcrFieldKey.SELF_HP.value, "51-60")  # noqa: SLF001
    window._mark_turn_snapshot_manual(OcrFieldKey.SELF_HP.value)  # noqa: SLF001
    assert window._turn_snapshot_field_locks[OcrFieldKey.SELF_HP.value]  # noqa: SLF001
    window._on_turn_snapshot_result(  # noqa: SLF001
        _ocr_result(
            identity,
            _candidate(OcrFieldKey.SELF_HP, "100", frame_id="fresh-100"),
        )
    )
    assert window.self_hp_box.currentText() == "51-60"

    stale_identity = replace(identity, snapshot_generation=identity.snapshot_generation + 1)
    window._on_turn_snapshot_result(  # noqa: SLF001
        _ocr_result(
            stale_identity,
            _candidate(OcrFieldKey.SELF_HP, "0", frame_id="stale-zero"),
        )
    )
    window.render_view()
    assert window.self_hp_box.currentText() == "51-60"
    repository.close()


def test_exact_switch_confirmation_is_selectable_writable_and_survives_render(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path, auto_start_capture=False)
    view = _confirm_current_state(
        window,
        controller,
        legal_switch_selection=(SELECTED_THREE[1], SELECTED_THREE[2]),
    )
    window.render_view(view)

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("injected prediction")
    window.mock_turn_rationale_input.setText("injected rationale")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001 - injected transport
    window.render_view()

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == (
        SELECTED_THREE[1],
        SELECTED_THREE[2],
    )

    window.self_action_tabs["SWITCH"].click()
    rendered = tuple(
        window.self_switch_target_box.itemText(index)
        for index in range(window.self_switch_target_box.count())
    )
    assert rendered == (SELECTED_THREE[1], SELECTED_THREE[2])
    window.self_switch_target_box.setCurrentText(SELECTED_THREE[1])
    assert window.actual_action_type_box.currentText() == ActionType.SWITCH.value
    assert window.actual_action_name_box.currentText() == SELECTED_THREE[1]
    window.self_switch_target_box.setCurrentText(SELECTED_THREE[2])
    assert window.actual_action_name_box.currentText() == SELECTED_THREE[2]

    window.render_view()
    assert window.self_switch_target_box.currentText() == SELECTED_THREE[2]
    assert window.actual_action_type_box.currentText() == ActionType.SWITCH.value
    assert window.actual_action_name_box.currentText() == SELECTED_THREE[2]

    assert transport.call_count == 1
    request = transport.calls[0][0]
    assert {
        action.action_name
        for action in request.legal_actions
        if action.action_type is ActionType.SWITCH
    } == {SELECTED_THREE[1], SELECTED_THREE[2]}
    confirmed_switches = {
        selection.action_name
        for selection in repository.list_confirmed_legal_action_selections_for_identity(
            request.identity
        )
        if selection.action_type is ActionType.SWITCH
    }
    assert confirmed_switches == {SELECTED_THREE[1], SELECTED_THREE[2]}
    repository.close()


def test_unresolved_switches_are_not_presented_as_confirmed_none(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path, auto_start_capture=False)
    _confirm_current_state(window, controller, legal_switch_selection=None)
    window.render_view()
    window._set_self_action_type("SWITCH")  # noqa: SLF001
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert window.self_switch_target_box.count() == 0
    assert window.self_switch_unavailable_label.text() == "交代候補が未確認です"
    repository.close()


def test_confirmed_none_remains_explicit_zero_switch_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path, auto_start_capture=False)
    _confirm_current_state(window, controller, legal_switch_selection=())
    window.render_view()
    window._set_self_action_type("SWITCH")  # noqa: SLF001
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    assert window.self_switch_target_box.count() == 0
    assert window.self_switch_unavailable_label.text() == "交代できるポケモンはいません"
    repository.close()
