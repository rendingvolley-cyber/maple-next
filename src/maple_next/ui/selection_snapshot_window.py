"""NEW MATCH-triggered Selection screenshot matching.

The live UGREEN capture worker may continue to own the device for preview, but
Selection ROI matching is event-driven: once NEW MATCH binds the new
match/generation, one immutable canonical frame is reacquired through the
capture abstraction and copied, and only that frozen frame is cropped and
matched for the new Selection identity. No Selection ROI timer is allowed.
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
    VideoCaptureBackend,
    is_frame_newer_than,
)
from maple_next.selection_roi.contracts import (
    SELECTION_SLOT_COUNT,
    SelectionMatchBundle,
)
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_roi_window import SelectionRoiMatchFlowWindow

_SELECTION_TAB_INDEX = 0


class SelectionSnapshotMatchFlowWindow(SelectionRoiMatchFlowWindow):
    """Run opponent Selection ROI matching once per explicit NEW MATCH click."""

    def __init__(
        self,
        controller: MatchFlowController,
        *,
        ocr_data_directory: Path,
        capture_backend: VideoCaptureBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        self._new_match_snapshot_counter = 0
        super().__init__(
            controller,
            ocr_data_directory=ocr_data_directory,
            capture_backend=capture_backend,
            auto_start_capture=auto_start_capture,
        )
        self._stop_selection_roi_timer()
        self._hide_legacy_selection_controls()
        self._autoload_last_used_self_team_preset()
        self._set_selection_roi_status(
            "NEW MATCHを押した瞬間の1枚を固定し、その画像だけで相手6体を解析します。"
        )
        self.render_view()

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._controller.refresh()
        super().render_view(current)
        self._hide_legacy_selection_controls()
        if current.projection.session_state == "SELECTION_OPEN":
            self.primary_cta_label.setText("現在の6体をGeminiに送る")
            self.guidance_label.setText(
                "NEW MATCH時の固定画像から相手6体を確認し、必要なら修正してから"
                "Geminiへ送信してください。"
            )

    def _hide_legacy_selection_controls(self) -> None:
        """The official operator surface never shows MOCK Selection controls."""

        if hasattr(self, "mock_group"):
            self.mock_group.setVisible(False)
        if hasattr(self, "gemini_group"):
            self.gemini_group.setVisible(False)
        if hasattr(self, "confirm_facts_button"):
            self.confirm_facts_button.setVisible(False)

    def _autoload_last_used_self_team_preset(self) -> None:
        """Populate the last-used team once without overwriting operator edits.

        Presets already live in runtime SQLite with a persistent last-used id.
        Auto-load only when there is no canonical/current team and all six editor
        fields are empty.  After that the fields stay visible and editable; NEW
        MATCH snapshots whatever the operator currently sees rather than silently
        reapplying the preset.
        """

        if not self._mutation_slots_allowed():
            return
        current = self._controller.refresh()
        if current.self_team or any(field.text().strip() for field in self.self_team_inputs):
            return
        preset = self._controller.last_used_self_team_preset()
        if preset is None:
            return
        self._copy_self_team_to_inputs(preset.self_team)
        self.self_team_preset_name.setText(preset.name)
        self._staged_self_team_build = preset.team_build
        self._controller.stage_self_team_build(preset.team_build)
        self.team_build_status_label.setText(preset.status)
        self._refresh_self_team_presets(preset.preset_id)

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

    def _capture_new_match_baseline(self) -> FramePacket | None:
        """Read the pre-transition frame solely as a staleness baseline.

        This value is never frozen or submitted to Selection ROI matching. It
        exists only so the post-transition reacquire below can prove it
        observed a frame that is demonstrably newer, rather than resubmitting
        whatever the capture backend already had cached before NEW MATCH.
        """

        _status, frame = self._capture_service.latest_snapshot()
        return frame

    def _reacquire_frame_after_new_match(
        self, baseline: FramePacket | None
    ) -> tuple[FramePacket | None, str]:
        """Obtain ONE fresh canonical frame after the new generation is bound.

        Fails closed (returns no frame) rather than fabricating a crop or
        reusing the pre-transition baseline when no fresh frame is available.
        """

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
                or "NEW MATCH後の映像を取得できません。相手6体は手入力できます。",
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
                "NEW MATCH後の映像が1280x720 canonical frameではありません。手入力できます。",
            )
        if not is_frame_newer_than(frame, baseline):
            return (
                None,
                "NEW MATCH後の新しい映像をまだ取得できません。相手6体は手入力できます。",
            )

        frozen_image = frame.image.copy()
        if frozen_image.isNull():
            return (
                None,
                "NEW MATCH後のスクリーンショットを固定できません。手入力できます。",
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
        return frozen, "NEW MATCH後のスクリーンショットを固定しました。解析中です。"

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
        self._autoload_last_used_self_team_preset()
        baseline = self._capture_new_match_baseline()
        previous_identity = self._selection_identity(self._controller.refresh())
        super()._on_new_match(_checked)
        frozen, message = self._reacquire_frame_after_new_match(baseline)
        self._submit_snapshot_for_new_identity(
            frame=frozen,
            unavailable_message=message,
            previous_identity=previous_identity,
        )

    def _on_new_match_after_export(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        self._autoload_last_used_self_team_preset()
        baseline = self._capture_new_match_baseline()
        previous_identity = self._selection_identity(self._controller.refresh())
        super()._on_new_match_after_export(_checked)
        frozen, message = self._reacquire_frame_after_new_match(baseline)
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
