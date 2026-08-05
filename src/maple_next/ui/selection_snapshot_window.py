"""NEW MATCH-triggered Selection screenshot matching.

The live UGREEN capture worker may continue to own the device for preview, but
Selection ROI matching is event-driven: one immutable canonical frame is copied
at the instant the human presses NEW MATCH, and only that frozen frame is cropped
and matched for the new Selection identity. No Selection ROI timer is allowed.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QImage

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    CaptureStatusCode,
    FrameKind,
    FramePacket,
)
from maple_next.selection_roi.contracts import (
    SELECTION_SLOT_COUNT,
    SelectionMatchBundle,
)
from maple_next.ui.selection_roi_window import SelectionRoiMatchFlowWindow

_SELECTION_TAB_INDEX = 0


class SelectionSnapshotMatchFlowWindow(SelectionRoiMatchFlowWindow):
    """Run opponent Selection ROI matching once per explicit NEW MATCH click."""

    def __init__(
        self,
        controller: object,
        *,
        ocr_data_directory: Path,
    ) -> None:
        self._new_match_snapshot_counter = 0
        super().__init__(controller, ocr_data_directory=ocr_data_directory)  # type: ignore[arg-type]
        self._stop_selection_roi_timer()
        self._set_selection_roi_status(
            "NEW MATCHを押した瞬間の1枚を固定し、その画像だけで相手6体を解析します。"
        )

    def _stop_selection_roi_timer(self) -> None:
        timer = self._selection_roi_timer
        if timer is not None:
            timer.stop()

    def _sync_selection_roi_timer(self) -> None:
        """Continuous Selection ROI polling is intentionally disabled."""

        self._stop_selection_roi_timer()

    def _poll_selection_roi(self) -> None:
        """No-op safety override: Selection matching is NEW MATCH-triggered."""

        return

    def _freeze_frame_at_new_match(self) -> tuple[FramePacket | None, str]:
        status, frame = self._capture_service.latest_snapshot()
        if (
            not status.available
            or not status.fresh
            or status.status != CaptureStatusCode.AVAILABLE
            or frame is None
        ):
            return (
                None,
                status.operator_message
                or "NEW MATCH時の映像を取得できません。相手6体は手入力できます。",
            )
        if (
            frame.frame_kind is not FrameKind.CANONICAL
            or frame.width != CANONICAL_FRAME_WIDTH
            or frame.height != CANONICAL_FRAME_HEIGHT
            or not isinstance(frame.image, QImage)
            or frame.image.isNull()
        ):
            return (
                None,
                "NEW MATCH時の映像が1280x720 canonical frameではありません。手入力できます。",
            )

        frozen_image = frame.image.copy()
        if frozen_image.isNull():
            return (
                None,
                "NEW MATCH時のスクリーンショットを固定できません。手入力できます。",
            )
        self._new_match_snapshot_counter += 1
        frozen = FramePacket(
            frame_id=(
                f"{frame.frame_id}:new-match-snapshot:"
                f"{self._new_match_snapshot_counter}"
            ),
            source=frame.source,
            captured_at_utc=frame.captured_at_utc,
            captured_monotonic_ns=frame.captured_monotonic_ns,
            width=frame.width,
            height=frame.height,
            image=frozen_image,
            source_width=frame.source_width,
            source_height=frame.source_height,
            canonical_resize_count=frame.canonical_resize_count,
            content_rect=frame.content_rect,
            frame_kind=frame.frame_kind,
        )
        return frozen, "NEW MATCH時のスクリーンショットを固定しました。解析中です。"

    def _submit_snapshot_for_new_identity(
        self,
        *,
        frame: FramePacket | None,
        unavailable_message: str,
        previous_identity: tuple[str | None, str | None, int | None],
    ) -> None:
        current = self._controller.refresh()
        current_identity = self._selection_identity(current)
        if (
            current.projection.session_state != "SELECTION_OPEN"
            or current_identity == previous_identity
            or current_identity != self._selection_roi_render_identity
        ):
            return

        self.header_tabs.setCurrentIndex(_SELECTION_TAB_INDEX)
        if frame is None:
            self._set_selection_roi_status(unavailable_message)
            return
        worker = self._selection_roi_worker
        if worker is None:
            self._set_selection_roi_status(
                "Selection画像解析を開始できません。相手6体は手入力できます。"
            )
            return
        self._selection_roi_submitted_identities.clear()
        self._selection_roi_submitted_identities[frame.frame_id] = current_identity
        self._set_selection_roi_status(unavailable_message)
        worker.submit(frame)

    def _on_new_match(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        frozen, message = self._freeze_frame_at_new_match()
        previous_identity = self._selection_identity(self._controller.refresh())
        super()._on_new_match(_checked)
        self._submit_snapshot_for_new_identity(
            frame=frozen,
            unavailable_message=message,
            previous_identity=previous_identity,
        )

    def _on_new_match_after_export(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        frozen, message = self._freeze_frame_at_new_match()
        previous_identity = self._selection_identity(self._controller.refresh())
        super()._on_new_match_after_export(_checked)
        self._submit_snapshot_for_new_identity(
            frame=frozen,
            unavailable_message=message,
            previous_identity=previous_identity,
        )

    def _on_selection_roi_result(self, payload: object) -> None:
        """Accept the one frozen result while its new-match identity remains current.

        Unlike the former timer path, the result is not discarded merely because
        the operator switches tabs while the one screenshot is being processed.
        Identity, generation, and SELECTION_OPEN remain mandatory.
        """

        if not isinstance(payload, SelectionMatchBundle) or payload.frame_id is None:
            return
        submitted_identity = self._selection_roi_submitted_identities.pop(
            payload.frame_id,
            None,
        )
        current = self._controller.refresh()
        current_identity = self._selection_identity(current)
        if (
            submitted_identity is None
            or submitted_identity != current_identity
            or current_identity != self._selection_roi_render_identity
            or not current.persistence_reads_allowed
            or current.projection.session_state != "SELECTION_OPEN"
        ):
            return

        self._selection_roi_bundle = payload
        self._selection_roi_bundle_identity = submitted_identity
        self._selection_roi_slot_matches = {
            slot.slot: slot for slot in payload.slots
        }
        self._set_selection_roi_status(
            f"NEW MATCH時の固定画像を解析しました。{payload.operator_message} "
            f"参照画像={payload.reference_count}件 / "
            f"ROI={payload.roi_config_provenance or '未確認'}"
        )
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            match = self._selection_roi_slot_matches.get(slot)
            self._render_selection_roi_slot(slot, match)
            if match is not None:
                self._auto_fill_selection_roi_slot(slot, match)
            self._render_selection_input_origin(slot)
        self._update_selection_roi_buttons(current)
