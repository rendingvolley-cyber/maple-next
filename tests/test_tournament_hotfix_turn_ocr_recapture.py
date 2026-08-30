"""Tournament production hotfix: current-Turn OCR recapture recovery.

Focused regressions for the explicit "このTurnを撮り直す" (retake) button
reachable from 「撮影画像を確認」: a failed first OCR attempt for the
current Turn, followed by an explicit human recapture, must get a fresh
OCR request identity bound to the SAME current Turn, and a successful
recapture result must update the current Turn's OCR-driven facts. A late
callback from the abandoned first attempt must never win, and NEXT TURN
must still invalidate anything still in flight for the old Turn.

Investigation note: this suite drives the real production handlers
(``_on_next_turn``/``_on_retake_turn_snapshot``/``_on_turn_snapshot_result``)
end to end, with a fresh-frame fake capture backend so the retake's own
``_freeze_turn_frame()`` succeeds for real. Every case below already passes
against the unmodified fresh-identity/``_identity_is_current`` mechanism in
``turn_snapshot_window.py``/``turn_snapshot_official_window.py`` -- the one
concrete gap this hotfix closes is that ``_on_retake_turn_snapshot`` did not
hold the same ``_turn_snapshot_transition_in_progress`` guard its sibling
``_on_start_turn``/``_on_next_turn`` handlers already hold around their own
freeze+submit, and this exact retake path had zero regression coverage.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from test_issue31_turn_ocr_origin_aware import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    _advance_to_second_turn,
    _result,
)
from test_issue31_turn_state_ui_bundle_c import build_window

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    DeviceOpenResult,
    SourceFramePacket,
)
from maple_next.ocr.contracts import OcrBundleStatus, OcrCandidateBundle, OcrFieldKey
from maple_next.turn_ocr.contracts import TurnSnapshotResult, TurnSnapshotStatus


class _FreshFrameCaptureBackend:
    """Always reports a genuinely fresh, canonical 1280x720 frame.

    A unique ``frame_id``/timestamp every call means ``_freeze_turn_frame``
    succeeds on every attempt (first capture *and* explicit recapture) --
    unlike ``CountingCaptureBackend`` (used by the other Bundle C suites),
    which never provides a frame at all and so cannot exercise the retake
    button's own real freeze step.
    """

    def __init__(self) -> None:
        self._running = False

    def start(self, selector: str, on_frame=None) -> DeviceOpenResult:
        del selector, on_frame
        self._running = True
        return DeviceOpenResult(True, True, "fake-device", None)

    def stop(self) -> None:
        self._running = False

    def get_latest_frame(self) -> SourceFramePacket | None:
        if not self._running:
            return None
        image = QImage(CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT, QImage.Format.Format_RGB32)
        image.fill(0x202020)
        return SourceFramePacket(
            frame_id=str(uuid.uuid4()),
            source="TEST",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=CANONICAL_FRAME_WIDTH,
            height=CANONICAL_FRAME_HEIGHT,
            image=image,
        )

    def is_running(self) -> bool:
        return self._running


class _NoopOcrWorker:
    """Stands in for ``TurnSnapshotOcrWorker`` so ``worker is not None``.

    Production's real worker requires a calibrated ``roi_config.json`` this
    test fixture's ``ocr_data_directory`` does not carry, which would make
    ``_turn_snapshot_worker`` None and short-circuit before the ANALYZING /
    "OCR中…" status this suite verifies is ever set. This double never
    actually processes anything -- results are injected directly via
    ``_on_turn_snapshot_result``, matching the rest of this test file and
    the existing ``test_issue31_turn_ocr_origin_aware.py`` suite.
    """

    def submit(self, request: object) -> None:
        del request

    def close(self) -> None:
        return None


def _build_recapture_window(tmp_path: Path):
    repository, controller, window, transport = build_window(
        tmp_path,
        capture_backend=_FreshFrameCaptureBackend(),
        auto_start_capture=False,
    )
    window._capture_service.start()  # noqa: SLF001
    window._turn_snapshot_worker = _NoopOcrWorker()  # noqa: SLF001
    _advance_to_second_turn(window, controller)
    return repository, controller, window, transport


def _failed_result(identity) -> TurnSnapshotResult:
    bundle = OcrCandidateBundle(
        status=OcrBundleStatus.OCR_FAILED,
        candidate_only=True,
        manual_entry_allowed=True,
        frame_id="failed-frame",
        frame_captured_at_utc=None,
        frame_age_ms=0,
        candidates=(),
        error_code="OCR_FAILED",
        operator_message="offline OCR-failure fixture",
    )
    return TurnSnapshotResult(
        identity=identity,
        status=TurnSnapshotStatus.OCR_FAILED,
        bundle=bundle,
        frozen_image=QImage(),
        crops={},
        operator_message="offline OCR-failure fixture",
        roi_config_provenance="roi_config.json:provisional",
    )


# --- 1 + 2: recapture gets a fresh id, bound to the current Turn, applies --


def test_explicit_recapture_after_failure_applies_to_current_turn(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_a = window._turn_snapshot_active_identity  # noqa: SLF001
    turn_before = controller.refresh().projection.turn_number

    window._on_turn_snapshot_result(_failed_result(identity_a))  # noqa: SLF001
    assert window._turn_ocr_status_code == TurnSnapshotStatus.OCR_FAILED  # noqa: SLF001

    window._on_retake_turn_snapshot()  # noqa: SLF001
    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001

    # 2. Fresh request identity, still bound to the same current Turn.
    assert identity_b != identity_a
    assert identity_b.turn_id == identity_a.turn_id
    assert identity_b.turn_number == identity_a.turn_number
    assert identity_b.snapshot_generation != identity_a.snapshot_generation
    assert controller.refresh().projection.turn_number == turn_before

    # 1. The second (successful) result applies to the current Turn.
    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001
    assert window.self_active_box.currentText() == SELECTED_THREE[1]
    assert window.opponent_active_input.text() == OPPONENT_TEAM[1]
    assert window.self_hp_box.currentText() == "81-90"
    assert window.opponent_hp_box.currentText() == "61-70"

    window.close()
    repository.close()


# --- 3: a late first-attempt callback after the second success is ignored --


def test_late_first_attempt_callback_after_second_success_is_ignored(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_a = window._turn_snapshot_active_identity  # noqa: SLF001

    window._on_retake_turn_snapshot()  # noqa: SLF001
    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001
    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001

    after_success = (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    )

    # The late A result carries stale candidates and a FAILED status --
    # neither may move anything now that B has already applied.
    window._on_turn_snapshot_result(_failed_result(identity_a))  # noqa: SLF001

    assert (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    ) == after_success
    assert window._turn_ocr_status_code == TurnSnapshotStatus.READY  # noqa: SLF001

    window.close()
    repository.close()


# --- 4: NEXT TURN before an old callback arrives -- old callback is inert --


def test_next_turn_before_old_callback_prevents_mutation_of_new_turn(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_turn2 = window._turn_snapshot_active_identity  # noqa: SLF001

    # Confirm + record an action so NEXT TURN is reachable again.
    from test_issue31_turn_state_ui_bundle_c import _fill_minimal_current_state

    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    from test_issue31_turn_ocr_origin_aware import _confirm_legal_switches_honestly

    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("p")
    window.mock_turn_rationale_input.setText("r")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001

    assert controller.refresh().projection.turn_number == 3
    identity_turn3 = window._turn_snapshot_active_identity  # noqa: SLF001
    assert identity_turn3.turn_id != identity_turn2.turn_id

    before = (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    )

    # A late Turn-2 result -- however "successful" -- must not touch Turn 3.
    window._on_turn_snapshot_result(_result(window, identity_turn2))  # noqa: SLF001

    assert (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
        window.self_hp_box.currentText(),
        window.opponent_hp_box.currentText(),
    ) == before

    window.close()
    repository.close()


# --- 5: recapture starts, then TurnIdentity changes -> stale recapture too -


def test_recapture_result_rejected_once_turn_identity_has_moved_on(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)

    window._on_retake_turn_snapshot()  # noqa: SLF001
    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001

    from test_issue31_turn_state_ui_bundle_c import _fill_minimal_current_state

    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    from test_issue31_turn_ocr_origin_aware import _confirm_legal_switches_honestly

    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("p")
    window.mock_turn_rationale_input.setText("r")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001
    assert controller.refresh().projection.turn_number == 3

    before = (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
    )
    # identity_b was current *when the recapture started*, but the Turn has
    # since moved on -- its own (still-pending) result must also be
    # rejected, not just an old pre-recapture attempt's.
    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001
    assert (
        window.self_active_box.currentText(),
        window.opponent_active_input.text(),
    ) == before

    window.close()
    repository.close()


# --- 6: OCR indicator recovers fail -> recapture-start -> success ----------


def test_ocr_status_indicator_recovers_through_fail_retry_success(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_a = window._turn_snapshot_active_identity  # noqa: SLF001

    window._on_turn_snapshot_result(_failed_result(identity_a))  # noqa: SLF001
    assert window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCRエラー"  # noqa: SLF001

    window._on_retake_turn_snapshot()  # noqa: SLF001
    assert window._turn_ocr_status_code == TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    assert window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCR中…"  # noqa: SLF001

    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001
    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001
    indicator = window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING")  # noqa: SLF001
    assert indicator in {"OCR完了", "OCR要確認"}

    window.close()
    repository.close()


# --- 7: existing human-reviewed fields survive a stale/old OCR result ------


def test_human_reviewed_field_not_replaced_by_stale_old_ocr(tmp_path: Path) -> None:
    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_a = window._turn_snapshot_active_identity  # noqa: SLF001

    # Operator manually reviews/locks the opponent's active Pokemon before
    # recapturing (e.g. they already know it from the broadcast overlay).
    window._set_turn_field(OcrFieldKey.OPPONENT_ACTIVE.value, "Garganacl")  # noqa: SLF001
    window._mark_turn_snapshot_manual(OcrFieldKey.OPPONENT_ACTIVE.value)  # noqa: SLF001

    window._on_retake_turn_snapshot()  # noqa: SLF001
    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001
    assert identity_b != identity_a

    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001

    # The fresh, successful recapture result still must not clobber the
    # human's own reviewed value for the field they locked.
    assert window.opponent_active_input.text() == "Garganacl"
    # Fields the operator never touched still update normally.
    assert window.self_active_box.currentText() == SELECTED_THREE[1]

    window.close()
    repository.close()


# --- P0 diagnostic: sanitized milestone trail covers the whole incident ----


def test_milestone_log_reconstructs_the_fail_then_recapture_incident(tmp_path: Path) -> None:
    """Proves the new sanitized diagnostic (added because production had
    zero durable audit trail for OCR capture/recapture requests -- see the
    module docstring) actually reconstructs a fail-then-recapture episode:
    every milestone in order, the late first-attempt callback correctly
    landing as CALLBACK_STALE, and no raw OCR text/confidence anywhere in
    the trail.
    """

    repository, controller, window, _transport = _build_recapture_window(tmp_path)
    identity_a = window._turn_snapshot_active_identity  # noqa: SLF001

    window._on_turn_snapshot_result(_failed_result(identity_a))  # noqa: SLF001
    window._on_retake_turn_snapshot()  # noqa: SLF001
    identity_b = window._turn_snapshot_active_identity  # noqa: SLF001
    window._on_turn_snapshot_result(_result(window, identity_b))  # noqa: SLF001
    # A late first-attempt callback arriving after B has already applied.
    window._on_turn_snapshot_result(_failed_result(identity_a))  # noqa: SLF001

    events = [entry.split(" ", 1)[0] for entry in window._turn_ocr_milestones]  # noqa: SLF001
    assert events == [
        "FRAME_FROZEN",  # NEXT TURN's own capture, from _advance_to_second_turn
        "OCR_SUBMITTED",
        "OCR_COMPLETED",  # A fails
        "CALLBACK_ACCEPTED",
        "FIELDS_APPLIED",
        "FIELDS_SUPPRESSED_BY_HUMAN_LOCK",
        "UI_REFRESHED",
        "RECAPTURE_REQUESTED",
        "FRAME_FROZEN",  # B
        "OCR_SUBMITTED",
        "OCR_COMPLETED",  # B succeeds
        "CALLBACK_ACCEPTED",
        "FIELDS_APPLIED",
        "FIELDS_SUPPRESSED_BY_HUMAN_LOCK",
        "UI_REFRESHED",
        "OCR_COMPLETED",  # late A
        "CALLBACK_STALE",
    ]
    full_log = "\n".join(window._turn_ocr_milestones)  # noqa: SLF001
    # Sanitized: identifiers/status codes/counts only, never OCR text.
    assert SELECTED_THREE[1] not in full_log
    assert OPPONENT_TEAM[1] not in full_log
    assert f"turn={identity_b.turn_number} gen={identity_b.snapshot_generation}" in full_log

    window.close()
    repository.close()
