"""Focused offline regression coverage for Issue #31 NEXT TURN OCR binding."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    CountingCaptureBackend,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.ocr.contracts import (
    OCR_CANDIDATE_SOURCE,
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateBundle,
    OcrFieldKey,
)
from maple_next.turn_ocr.contracts import (
    TurnSnapshotResult,
    TurnSnapshotStatus,
)


def _candidate(field_key: str, value: str) -> OcrCandidate:
    return OcrCandidate(
        field_key=field_key,
        suggested_value=value,
        raw_text="offline-origin-aware-fixture",
        confidence=0.99,
        rank=1,
        reason="offline current-turn fixture",
        source_frame_id="fresh-current-turn-frame",
        source=OCR_CANDIDATE_SOURCE,
    )


def _fresh_candidates() -> tuple[OcrCandidate, ...]:
    return (
        _candidate(OcrFieldKey.SELF_ACTIVE.value, SELECTED_THREE[1]),
        _candidate(OcrFieldKey.OPPONENT_ACTIVE.value, OPPONENT_TEAM[1]),
        _candidate(OcrFieldKey.SELF_HP.value, "81-90"),
        _candidate(OcrFieldKey.OPPONENT_HP.value, "61-70"),
    )


def _single_hp_candidate(field: OcrFieldKey, value: str) -> tuple[OcrCandidate, ...]:
    return (_candidate(field.value, value),)


def _advance_to_second_turn(window, controller) -> None:
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001 - trusted offline UI seam
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("offline prediction")
    window.mock_turn_rationale_input.setText("offline rationale")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001

    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001 - trusted offline UI seam
    window._on_next_turn()  # noqa: SLF001 - trusted offline UI seam

    assert controller.refresh().projection.turn_number == 2
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    assert window._turn_snapshot_active_identity is not None  # noqa: SLF001


def _result(window, identity) -> TurnSnapshotResult:
    bundle = OcrCandidateBundle(
        status=OcrBundleStatus.CANDIDATES_READY,
        candidate_only=True,
        manual_entry_allowed=True,
        frame_id="fresh-current-turn-frame",
        frame_captured_at_utc=None,
        frame_age_ms=0,
        candidates=_fresh_candidates(),
        error_code=None,
        operator_message="offline candidate fixture",
    )
    return TurnSnapshotResult(
        identity=identity,
        status=TurnSnapshotStatus.READY,
        bundle=bundle,
        frozen_image=QImage(),
        crops={},
        operator_message="offline candidate fixture",
        roi_config_provenance="roi_config.json:provisional",
    )


def test_fresh_current_turn_ocr_replaces_all_carry_forward_fields(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)

    assert all(
        "引き継ぎ・未確認" in window._turn_snapshot_origins[field.value]  # noqa: SLF001
        for field in OcrFieldKey
    )

    window._auto_fill_turn_snapshot_candidates(_fresh_candidates())  # noqa: SLF001

    assert window.self_active_box.currentText() == SELECTED_THREE[1]
    assert window.opponent_active_input.text() == OPPONENT_TEAM[1]
    assert window.self_hp_box.currentText() == "81-90"
    assert window.opponent_hp_box.currentText() == "61-70"

    # An ordinary render must not restore the durable carry-forward draft
    # over the newer current-Turn OCR values.
    window.render_view()
    assert window.self_active_box.currentText() == SELECTED_THREE[1]
    assert window.opponent_active_input.text() == OPPONENT_TEAM[1]
    assert window.self_hp_box.currentText() == "81-90"
    assert window.opponent_hp_box.currentText() == "61-70"

    window.close()
    repository.close()


def test_current_turn_human_locks_survive_later_fresh_ocr_and_remain_editable(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)

    human_values = {
        OcrFieldKey.SELF_ACTIVE.value: SELECTED_THREE[2],
        OcrFieldKey.OPPONENT_ACTIVE.value: OPPONENT_TEAM[2],
        OcrFieldKey.SELF_HP.value: "51-60",
        OcrFieldKey.OPPONENT_HP.value: "31-40",
    }
    for field_key, value in human_values.items():
        window._set_turn_field(field_key, value)  # noqa: SLF001
        window._mark_turn_snapshot_manual(field_key)  # noqa: SLF001

    window._auto_fill_turn_snapshot_candidates(_fresh_candidates())  # noqa: SLF001

    assert window.self_active_box.currentText() == SELECTED_THREE[2]
    assert window.opponent_active_input.text() == OPPONENT_TEAM[2]
    assert window.self_hp_box.currentText() == "51-60"
    assert window.opponent_hp_box.currentText() == "31-40"
    assert all(window._turn_snapshot_field_locks.values())  # noqa: SLF001
    assert window.self_active_box.isEnabled()
    assert window.opponent_active_input.isEnabled()
    assert window.self_hp_box.isEnabled()
    assert window.opponent_hp_box.isEnabled()

    window.close()
    repository.close()


def test_opponent_hp_updates_independently_without_changing_names_or_self_hp(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)
    before_names = (window.self_active_box.currentText(), window.opponent_active_input.text())
    before_self_hp = window.self_hp_box.currentText()

    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _single_hp_candidate(OcrFieldKey.OPPONENT_HP, "1-10")
    )

    assert (window.self_active_box.currentText(), window.opponent_active_input.text()) == (
        before_names
    )
    assert window.self_hp_box.currentText() == before_self_hp
    assert window.opponent_hp_box.currentText() == "1-10"
    window.close()
    repository.close()


def test_self_hp_updates_independently_without_changing_names_or_opponent_hp(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)
    before_names = (window.self_active_box.currentText(), window.opponent_active_input.text())
    before_opponent_hp = window.opponent_hp_box.currentText()

    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _single_hp_candidate(OcrFieldKey.SELF_HP, "61-70")
    )

    assert (window.self_active_box.currentText(), window.opponent_active_input.text()) == (
        before_names
    )
    assert window.self_hp_box.currentText() == "61-70"
    assert window.opponent_hp_box.currentText() == before_opponent_hp
    window.close()
    repository.close()


def test_current_turn_human_opponent_hp_remains_locked_against_later_ocr(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)
    window._set_turn_field(OcrFieldKey.OPPONENT_HP.value, "31-40")  # noqa: SLF001
    window._mark_turn_snapshot_manual(OcrFieldKey.OPPONENT_HP.value)  # noqa: SLF001

    window._auto_fill_turn_snapshot_candidates(  # noqa: SLF001
        _single_hp_candidate(OcrFieldKey.OPPONENT_HP, "1-10")
    )

    assert window.opponent_hp_box.currentText() == "31-40"
    assert window._turn_snapshot_field_locks[OcrFieldKey.OPPONENT_HP.value]  # noqa: SLF001
    window.close()
    repository.close()


def test_stale_and_wrong_turn_results_cannot_overwrite_current_turn_fields(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(
        tmp_path,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    _advance_to_second_turn(window, controller)
    identity = window._turn_snapshot_active_identity  # noqa: SLF001
    assert identity is not None

    original = (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    )
    stale_identity = replace(
        identity,
        snapshot_generation=max(0, identity.snapshot_generation - 1),
    )
    wrong_turn_identity = replace(
        identity,
        turn_id="wrong-turn-id",
        turn_number=identity.turn_number + 1,
    )

    window._on_turn_snapshot_result(_result(window, stale_identity))  # noqa: SLF001
    window._on_turn_snapshot_result(_result(window, wrong_turn_identity))  # noqa: SLF001

    assert (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    ) == original

    window.close()
    repository.close()
