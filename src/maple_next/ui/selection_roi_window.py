"""Selection-tab ROI candidate UI layered over the accepted MatchFlowWindow."""

from __future__ import annotations

from pathlib import Path
from typing import TypeAlias, cast

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from maple_next.capture.contracts import CaptureStatusCode
from maple_next.selection_roi.contracts import (
    SELECTION_SLOT_COUNT,
    UNKNOWN_LABEL,
    SelectionMatchBundle,
    SelectionRoiError,
    SelectionSlotMatch,
)
from maple_next.selection_roi.service import SelectionRoiService
from maple_next.selection_roi.worker import LatestOnlySelectionRoiWorker
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow

_SELECTION_ROI_INTERVAL_MS = 500
_SELECTION_TAB_INDEX = 0
_THUMBNAIL_SIZE = QSize(160, 90)
SelectionIdentity: TypeAlias = tuple[str | None, str | None, int | None]


class SelectionRoiMatchFlowWindow(MatchFlowWindow):
    """Adds human-only opponent-team image candidates to the Selection tab."""

    def __init__(
        self,
        controller: MatchFlowController,
        *,
        ocr_data_directory: Path,
    ) -> None:
        self._selection_roi_ready = False
        self._selection_roi_service = SelectionRoiService(ocr_data_directory)
        self._selection_roi_worker: LatestOnlySelectionRoiWorker | None = None
        self._selection_roi_timer: QTimer | None = None
        self._selection_roi_bundle: SelectionMatchBundle | None = None
        self._selection_roi_bundle_identity: SelectionIdentity | None = None
        self._selection_roi_render_identity: SelectionIdentity | None = None
        self._selection_roi_submitted_identities: dict[str, SelectionIdentity] = {}
        self._selection_roi_last_submitted_frame_id: str | None = None
        self._selection_roi_facts_editable = False
        self._selection_roi_last_status_text = ""
        self._selection_roi_slot_matches: dict[int, SelectionSlotMatch] = {}
        super().__init__(controller)
        self._build_selection_roi_group()
        worker = LatestOnlySelectionRoiWorker(self._selection_roi_service)
        worker.result_ready.connect(self._on_selection_roi_result)
        self._selection_roi_worker = worker
        timer = QTimer(self)
        timer.setInterval(_SELECTION_ROI_INTERVAL_MS)
        timer.timeout.connect(self._poll_selection_roi)
        self._selection_roi_timer = timer
        self._selection_roi_ready = True
        self.render_view()

    @staticmethod
    def _selection_identity(view: OperatorView) -> SelectionIdentity:
        projection = view.projection
        return (projection.session_id, projection.match_id, projection.generation)

    def _build_selection_roi_group(self) -> None:
        self.selection_roi_group = QGroupBox(
            "相手6体の画像候補 — ROI参照画像との一致・自動反映なし"
        )
        layout = QVBoxLayout(self.selection_roi_group)
        self.selection_roi_status_label = QLabel(
            "ROI設定と参照画像を確認しています。手入力は常に使用できます。"
        )
        self.selection_roi_status_label.setWordWrap(True)
        layout.addWidget(self.selection_roi_status_label)

        self._selection_roi_thumbnail_labels: dict[int, QLabel] = {}
        self._selection_roi_candidate_labels: dict[int, QLabel] = {}
        self._selection_roi_apply_buttons: dict[int, QPushButton] = {}

        form = QFormLayout()
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            thumbnail = QLabel("ROI —")
            thumbnail.setMinimumSize(_THUMBNAIL_SIZE)
            thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            candidate = QLabel("候補なし")
            candidate.setWordWrap(True)
            apply_button = QPushButton("入力欄へ反映")
            apply_button.setEnabled(False)
            apply_button.clicked.connect(
                lambda _checked=False, selected_slot=slot: self._apply_selection_roi_slot(
                    selected_slot
                )
            )
            row_layout.addWidget(thumbnail)
            row_layout.addWidget(candidate, 1)
            row_layout.addWidget(apply_button)
            self._selection_roi_thumbnail_labels[slot] = thumbnail
            self._selection_roi_candidate_labels[slot] = candidate
            self._selection_roi_apply_buttons[slot] = apply_button
            form.addRow(f"相手 slot {slot}", row)
        layout.addLayout(form)

        self.selection_roi_apply_all_button = QPushButton(
            "候補を空欄の相手6体入力へ反映"
        )
        self.selection_roi_apply_all_button.setEnabled(False)
        self.selection_roi_apply_all_button.clicked.connect(
            self._apply_selection_roi_all
        )
        layout.addWidget(self.selection_roi_apply_all_button)

        insert_index = max(0, self._selection_layout.count() - 1)
        self._selection_layout.insertWidget(insert_index, self.selection_roi_group)

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._controller.refresh()
        super().render_view(current)
        if not self._selection_roi_ready:
            return
        current_identity = self._selection_identity(current)
        if current_identity != self._selection_roi_render_identity:
            self._clear_selection_roi_candidates()
            self._selection_roi_render_identity = current_identity
        selection_open = current.projection.session_state == "SELECTION_OPEN"
        self._selection_roi_facts_editable = (
            current.persistence_reads_allowed
            and current.projection.primary_cta == "CONFIRM_SELECTION_FACTS"
        )
        self.selection_roi_group.setVisible(selection_open)
        self._update_selection_roi_buttons()
        self._sync_selection_roi_timer()

    def _on_header_tab_changed(self, index: int) -> None:
        super()._on_header_tab_changed(index)
        if self._selection_roi_ready:
            self._sync_selection_roi_timer()

    def _sync_selection_roi_timer(self) -> None:
        timer = self._selection_roi_timer
        if timer is None:
            return
        active = (
            self._persistence_reads_allowed
            and self.header_tabs.currentIndex() == _SELECTION_TAB_INDEX
            and self._last_rendered_session_state == "SELECTION_OPEN"
            and self._selection_roi_facts_editable
        )
        if active:
            if not timer.isActive():
                self._poll_selection_roi()
                timer.start()
        else:
            timer.stop()

    def _poll_selection_roi(self) -> None:
        if (
            not self._persistence_reads_allowed
            or self.header_tabs.currentIndex() != _SELECTION_TAB_INDEX
            or self._last_rendered_session_state != "SELECTION_OPEN"
            or not self._selection_roi_facts_editable
        ):
            return
        current = self._controller.refresh()
        current_identity = self._selection_identity(current)
        if (
            not current.persistence_reads_allowed
            or current.projection.session_state != "SELECTION_OPEN"
            or current_identity != self._selection_roi_render_identity
        ):
            return
        status, frame = self._capture_service.latest_snapshot()
        if (
            not status.available
            or not status.fresh
            or status.status != CaptureStatusCode.AVAILABLE
            or frame is None
        ):
            message = status.operator_message or (
                "選出映像がありません。相手6体は手入力で続行できます。"
            )
            self._set_selection_roi_status(message)
            return
        if frame.frame_id == self._selection_roi_last_submitted_frame_id:
            return
        worker = self._selection_roi_worker
        if worker is None:
            return
        self._selection_roi_last_submitted_frame_id = frame.frame_id
        self._selection_roi_submitted_identities[frame.frame_id] = current_identity
        while len(self._selection_roi_submitted_identities) > 4:
            oldest_frame_id = next(iter(self._selection_roi_submitted_identities))
            del self._selection_roi_submitted_identities[oldest_frame_id]
        worker.submit(frame)

    def _on_selection_roi_result(self, payload: object) -> None:
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
            or self.header_tabs.currentIndex() != _SELECTION_TAB_INDEX
        ):
            return
        self._selection_roi_bundle = payload
        self._selection_roi_bundle_identity = submitted_identity
        self._selection_roi_slot_matches = {
            slot.slot: slot for slot in payload.slots
        }
        self._set_selection_roi_status(
            f"{payload.operator_message} "
            f"参照画像={payload.reference_count}件 / "
            f"ROI={payload.roi_config_provenance or '未確認'}"
        )
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            match = self._selection_roi_slot_matches.get(slot)
            thumbnail_label = self._selection_roi_thumbnail_labels[slot]
            candidate_label = self._selection_roi_candidate_labels[slot]
            if match is None:
                thumbnail_label.clear()
                thumbnail_label.setText("ROI —")
                candidate_label.setText("候補なし")
                continue
            pixmap = QPixmap.fromImage(match.crop)
            if not pixmap.isNull():
                thumbnail_label.setText("")
                thumbnail_label.setPixmap(
                    pixmap.scaled(
                        _THUMBNAIL_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            top_text = " / ".join(
                f"{candidate.label} {candidate.score:.1%}"
                for candidate in match.top_candidates
            )
            if match.assigned_label == UNKNOWN_LABEL:
                candidate_label.setText(
                    f"判定: 未確定\n次点: {top_text or '参照画像なし'}"
                )
            else:
                candidate_label.setText(
                    f"判定: {match.assigned_label} {match.assigned_score:.1%}"
                    f"\n次点: {top_text or '—'}"
                )
        self._update_selection_roi_buttons()

    def _update_selection_roi_buttons(self) -> None:
        has_any = False
        identity_current = (
            self._selection_roi_bundle_identity is not None
            and self._selection_roi_bundle_identity == self._selection_roi_render_identity
        )
        for slot, button in self._selection_roi_apply_buttons.items():
            match = self._selection_roi_slot_matches.get(slot)
            enabled = (
                self._selection_roi_facts_editable
                and identity_current
                and match is not None
                and match.assigned_label != UNKNOWN_LABEL
            )
            button.setEnabled(enabled)
            has_any = has_any or enabled
        self.selection_roi_apply_all_button.setEnabled(
            self._selection_roi_facts_editable and identity_current and has_any
        )

    def _apply_selection_roi_slot(self, slot: int) -> None:
        if not self._mutation_slots_allowed() or not self._selection_roi_facts_editable:
            return
        if self._selection_roi_bundle_identity != self._selection_roi_render_identity:
            return
        match = self._selection_roi_slot_matches.get(slot)
        if match is None or match.assigned_label == UNKNOWN_LABEL:
            return
        self.opponent_team_inputs[slot - 1].setText(match.assigned_label)

    def _apply_selection_roi_all(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed() or not self._selection_roi_facts_editable:
            return
        if self._selection_roi_bundle_identity != self._selection_roi_render_identity:
            return
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            field = self.opponent_team_inputs[slot - 1]
            if field.text().strip():
                continue
            match = self._selection_roi_slot_matches.get(slot)
            if match is not None and match.assigned_label != UNKNOWN_LABEL:
                field.setText(match.assigned_label)

    def _on_confirm_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        before = self._controller.refresh()
        before_identity = self._selection_identity(before)
        bundle = self._selection_roi_bundle
        bundle_identity = self._selection_roi_bundle_identity
        names = tuple(field.text().strip() for field in self.opponent_team_inputs)
        super()._on_confirm_facts(_checked)
        after = self._controller.refresh()
        reviewed_selection_id = after.projection.current_reviewed_selection_id
        confirmed = (
            after.error_message is None
            and reviewed_selection_id is not None
            and reviewed_selection_id
            != before.projection.current_reviewed_selection_id
        )
        if (
            not confirmed
            or bundle is None
            or bundle.observation_id is None
            or bundle_identity != before_identity
        ):
            return
        typed_names = cast(
            tuple[str, str, str, str, str, str],
            names,
        )
        try:
            result = self._selection_roi_service.confirm_observation(
                observation_id=bundle.observation_id,
                opponent_names=typed_names,
                reviewed_selection_id=reviewed_selection_id,
            )
        except (OSError, SelectionRoiError):
            self._set_selection_roi_status(
                "相手6体は確認済みですが、ROI参照画像の保存に失敗しました。"
            )
            return
        self._set_selection_roi_status(
            "相手6体を確認しました。"
            f"参照追加={result.added_count} / "
            f"重複={result.duplicate_count} / "
            f"競合隔離={result.conflict_count}"
        )

    def _clear_selection_roi_candidates(self) -> None:
        self._selection_roi_bundle = None
        self._selection_roi_bundle_identity = None
        self._selection_roi_last_submitted_frame_id = None
        self._selection_roi_submitted_identities.clear()
        self._selection_roi_slot_matches.clear()
        if not hasattr(self, "_selection_roi_thumbnail_labels"):
            return
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            thumbnail = self._selection_roi_thumbnail_labels[slot]
            thumbnail.clear()
            thumbnail.setText("ROI —")
            self._selection_roi_candidate_labels[slot].setText("候補なし")
            self._selection_roi_apply_buttons[slot].setEnabled(False)
        self.selection_roi_apply_all_button.setEnabled(False)

    def _set_selection_roi_status(self, text: str) -> None:
        if text == self._selection_roi_last_status_text:
            return
        self._selection_roi_last_status_text = text
        self.selection_roi_status_label.setText(text)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        timer = self._selection_roi_timer
        if timer is not None:
            timer.stop()
        worker = self._selection_roi_worker
        if worker is not None:
            worker.close()
        super().closeEvent(event)
