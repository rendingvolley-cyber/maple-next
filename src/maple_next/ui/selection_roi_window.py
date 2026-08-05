"""Selection-tab ROI assisted input layered over the accepted MatchFlowWindow."""

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
from maple_next.selection_roi.input_policy import (
    AUTO_FILL_THRESHOLD,
    CANDIDATE_BUTTON_COUNT,
    SelectionInputOrigin,
    SelectionSlotInputState,
    should_auto_fill,
    visible_candidates,
)
from maple_next.selection_roi.service import (
    SelectionRoiService,
    SelectionSlotFeedback,
)
from maple_next.selection_roi.worker import LatestOnlySelectionRoiWorker
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow
from maple_next.ui.trusted_input import TrustedSendButton

_SELECTION_ROI_INTERVAL_MS = 500
_SELECTION_TAB_INDEX = 0
_THUMBNAIL_SIZE = QSize(160, 90)
SelectionIdentity: TypeAlias = tuple[str | None, str | None, int | None]
SelectionFeedbackTuple: TypeAlias = tuple[
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
]


class SelectionRoiMatchFlowWindow(MatchFlowWindow):
    """Adds editable opponent-team image assistance to the Selection tab.

    A score of at least 0.80 may fill an empty field once. Candidate chips and
    direct typing are always human-editable, and either human action locks that
    slot against later OCR overwrites. The only provider path remains a trusted
    OS-input button; its handler first creates the canonical Selection snapshot,
    then dispatches exactly one existing Gemini request.
    """

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
        self._selection_roi_input_states: dict[int, SelectionSlotInputState] = {
            slot: SelectionSlotInputState()
            for slot in range(1, SELECTION_SLOT_COUNT + 1)
        }
        self._selection_roi_candidate_values: dict[tuple[int, int], str] = {}
        self._selection_roi_feedback_recorded_ids: set[str] = set()
        super().__init__(controller)
        self._connect_selection_input_tracking()
        self._build_selection_roi_group()
        # The former explicit confirmation remains available internally for
        # compatibility, but the supported operator flow uses the single trusted
        # Gemini button below.
        self.confirm_facts_button.setVisible(False)
        self.gemini_group.setVisible(False)

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

    def _connect_selection_input_tracking(self) -> None:
        for slot, field in enumerate(self.opponent_team_inputs, start=1):
            field.textEdited.connect(
                lambda text, selected_slot=slot: self._on_opponent_text_edited(
                    selected_slot,
                    text,
                )
            )

    def _build_selection_roi_group(self) -> None:
        self.selection_roi_group = QGroupBox(
            "相手6体OCR補助 — 0.80以上は仮入力・いつでも修正可能"
        )
        layout = QVBoxLayout(self.selection_roi_group)
        self.selection_roi_status_label = QLabel(
            "ROI設定と参照画像を確認しています。候補は補助で、送信前に直接修正できます。"
        )
        self.selection_roi_status_label.setWordWrap(True)
        layout.addWidget(self.selection_roi_status_label)

        self._selection_roi_thumbnail_labels: dict[int, QLabel] = {}
        self._selection_roi_candidate_labels: dict[int, QLabel] = {}
        self._selection_roi_origin_labels: dict[int, QLabel] = {}
        self._selection_roi_candidate_buttons: dict[int, list[QPushButton]] = {}
        # Compatibility alias retained for focused tests and old private probes.
        self._selection_roi_apply_buttons: dict[int, QPushButton] = {}

        form = QFormLayout()
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)

            thumbnail = QLabel("ROI —")
            thumbnail.setMinimumSize(_THUMBNAIL_SIZE)
            thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)

            details = QWidget()
            details_layout = QVBoxLayout(details)
            details_layout.setContentsMargins(0, 0, 0, 0)
            candidate = QLabel("候補なし")
            candidate.setWordWrap(True)
            origin = QLabel("未入力")
            origin.setWordWrap(True)
            chips = QWidget()
            chips_layout = QHBoxLayout(chips)
            chips_layout.setContentsMargins(0, 0, 0, 0)
            chip_buttons: list[QPushButton] = []
            for candidate_index in range(CANDIDATE_BUTTON_COUNT):
                button = QPushButton()
                button.setVisible(False)
                button.setEnabled(False)
                button.setCheckable(True)
                button.clicked.connect(
                    lambda _checked=False,
                    selected_slot=slot,
                    selected_index=candidate_index: self._apply_candidate_chip(
                        selected_slot,
                        selected_index,
                    )
                )
                chips_layout.addWidget(button)
                chip_buttons.append(button)
            chips_layout.addStretch(1)
            details_layout.addWidget(candidate)
            details_layout.addWidget(origin)
            details_layout.addWidget(chips)

            row_layout.addWidget(thumbnail)
            row_layout.addWidget(details, 1)
            self._selection_roi_thumbnail_labels[slot] = thumbnail
            self._selection_roi_candidate_labels[slot] = candidate
            self._selection_roi_origin_labels[slot] = origin
            self._selection_roi_candidate_buttons[slot] = chip_buttons
            self._selection_roi_apply_buttons[slot] = chip_buttons[0]
            form.addRow(f"相手 slot {slot}", row)
        layout.addLayout(form)

        self.selection_roi_send_button = TrustedSendButton(
            "現在の6体でGeminiに送る",
            self._on_send_current_selection_to_gemini,
            self,
        )
        self.selection_roi_send_button.setEnabled(False)
        layout.addWidget(self.selection_roi_send_button)

        helper = QLabel(
            "0.80以上は空欄へ一度だけ仮入力します。候補ボタンまたは直接入力で修正すると、"
            "その枠は後続OCRで上書きされません。送信時の6体がcanonical値になります。"
        )
        helper.setWordWrap(True)
        layout.addWidget(helper)

        # Hidden compatibility control: no longer part of the supported UX.
        self.selection_roi_apply_all_button = QPushButton(
            "候補を空欄の相手6体入力へ反映"
        )
        self.selection_roi_apply_all_button.setVisible(False)
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

        # The assisted single-button path replaces the duplicate base controls,
        # while preserving their controller and trusted-send implementation.
        self.confirm_facts_button.setVisible(False)
        self.gemini_group.setVisible(False)

        current_identity = self._selection_identity(current)
        if current_identity != self._selection_roi_render_identity:
            self._clear_selection_roi_candidates()
            self._selection_roi_render_identity = current_identity
        self._sync_restored_input_states()

        selection_open = current.projection.session_state == "SELECTION_OPEN"
        self._selection_roi_facts_editable = (
            current.persistence_reads_allowed
            and current.projection.primary_cta == "CONFIRM_SELECTION_FACTS"
        )
        self.selection_roi_group.setVisible(selection_open)
        self._update_selection_roi_buttons(current)
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
            self._render_selection_roi_slot(slot, match)
            if match is not None:
                self._auto_fill_selection_roi_slot(slot, match)
            self._render_selection_input_origin(slot)
        self._update_selection_roi_buttons(current)

    def _render_selection_roi_slot(
        self,
        slot: int,
        match: SelectionSlotMatch | None,
    ) -> None:
        thumbnail_label = self._selection_roi_thumbnail_labels[slot]
        candidate_label = self._selection_roi_candidate_labels[slot]
        buttons = self._selection_roi_candidate_buttons[slot]
        for index, button in enumerate(buttons):
            self._selection_roi_candidate_values.pop((slot, index), None)
            button.setVisible(False)
            button.setEnabled(False)
            button.setChecked(False)
        if match is None:
            thumbnail_label.clear()
            thumbnail_label.setText("ROI —")
            candidate_label.setText("候補なし")
            return

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
        if match.assigned_label == UNKNOWN_LABEL:
            candidate_label.setText("Top1は仮入力閾値未満です。候補ボタンから選べます。")
        else:
            candidate_label.setText(
                f"Top1: {match.assigned_label} {match.assigned_score:.1%}"
            )

        current_value = self.opponent_team_inputs[slot - 1].text().strip()
        for index, (label, score, reference_count) in enumerate(
            visible_candidates(match)
        ):
            button = buttons[index]
            button.setText(f"{label} {score:.0%}・参照{reference_count}")
            button.setVisible(True)
            button.setChecked(current_value == label)
            self._selection_roi_candidate_values[(slot, index)] = label

    def _auto_fill_selection_roi_slot(
        self,
        slot: int,
        match: SelectionSlotMatch,
    ) -> None:
        field = self.opponent_team_inputs[slot - 1]
        state = self._state_for_current_field(slot)
        if not should_auto_fill(state, match):
            return
        self._set_selection_slot_value(
            slot,
            match.assigned_label,
            origin=SelectionInputOrigin.OCR_AUTO,
            user_locked=False,
        )
        for index, button in enumerate(self._selection_roi_candidate_buttons[slot]):
            label = self._selection_roi_candidate_values.get((slot, index))
            button.setChecked(label == field.text().strip())

    def _state_for_current_field(self, slot: int) -> SelectionSlotInputState:
        state = self._selection_roi_input_states[slot]
        current_value = self.opponent_team_inputs[slot - 1].text().strip()
        if state.value == current_value:
            return state
        if current_value:
            state = SelectionSlotInputState(
                value=current_value,
                origin=SelectionInputOrigin.RESTORED,
                user_locked=True,
            )
        else:
            state = SelectionSlotInputState()
        self._selection_roi_input_states[slot] = state
        return state

    def _sync_restored_input_states(self) -> None:
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            self._state_for_current_field(slot)
            self._render_selection_input_origin(slot)

    def _on_opponent_text_edited(self, slot: int, text: str) -> None:
        self._selection_roi_input_states[slot] = SelectionSlotInputState(
            value=text.strip(),
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
        self._render_selection_input_origin(slot)
        self._refresh_candidate_checks(slot)
        if self._selection_roi_ready:
            self._update_selection_roi_buttons()

    def _set_selection_slot_value(
        self,
        slot: int,
        value: str,
        *,
        origin: SelectionInputOrigin,
        user_locked: bool,
    ) -> None:
        normalized = value.strip()
        self._selection_roi_input_states[slot] = SelectionSlotInputState(
            value=normalized,
            origin=origin,
            user_locked=user_locked,
        )
        self.opponent_team_inputs[slot - 1].setText(normalized)
        self._render_selection_input_origin(slot)
        self._refresh_candidate_checks(slot)

    def _render_selection_input_origin(self, slot: int) -> None:
        if not hasattr(self, "_selection_roi_origin_labels"):
            return
        state = self._selection_roi_input_states[slot]
        labels = {
            SelectionInputOrigin.EMPTY: "未入力",
            SelectionInputOrigin.OCR_AUTO: (
                f"OCR {AUTO_FILL_THRESHOLD:.0%}以上・仮入力"
            ),
            SelectionInputOrigin.CANDIDATE_CLICK: "候補から選択・人間操作",
            SelectionInputOrigin.MANUAL_TEXT: "手動入力・人間操作",
            SelectionInputOrigin.RESTORED: "保存済み値から復元",
        }
        self._selection_roi_origin_labels[slot].setText(labels[state.origin])

    def _refresh_candidate_checks(self, slot: int) -> None:
        if not hasattr(self, "_selection_roi_candidate_buttons"):
            return
        current_value = self.opponent_team_inputs[slot - 1].text().strip()
        for index, button in enumerate(self._selection_roi_candidate_buttons[slot]):
            label = self._selection_roi_candidate_values.get((slot, index))
            button.setChecked(bool(label) and label == current_value)

    def _apply_candidate_chip(self, slot: int, candidate_index: int) -> None:
        if not self._mutation_slots_allowed() or not self._selection_roi_facts_editable:
            return
        if self._selection_roi_bundle_identity != self._selection_roi_render_identity:
            return
        label = self._selection_roi_candidate_values.get((slot, candidate_index))
        if not label:
            return
        self._set_selection_slot_value(
            slot,
            label,
            origin=SelectionInputOrigin.CANDIDATE_CLICK,
            user_locked=True,
        )
        self._update_selection_roi_buttons()

    def _update_selection_roi_buttons(
        self,
        current: OperatorView | None = None,
    ) -> None:
        if not hasattr(self, "selection_roi_send_button"):
            return
        view = current if current is not None else self._controller.refresh()
        identity_current = (
            self._selection_roi_bundle_identity is not None
            and self._selection_roi_bundle_identity == self._selection_roi_render_identity
        )
        has_any = False
        for slot, buttons in self._selection_roi_candidate_buttons.items():
            for index, button in enumerate(buttons):
                available = (slot, index) in self._selection_roi_candidate_values
                enabled = (
                    self._selection_roi_facts_editable
                    and identity_current
                    and available
                )
                button.setEnabled(enabled)
                has_any = has_any or enabled
        self.selection_roi_apply_all_button.setEnabled(
            self._selection_roi_facts_editable and identity_current and has_any
        )

        names = tuple(field.text().strip() for field in self.opponent_team_inputs)
        complete_unique = (
            len(names) == SELECTION_SLOT_COUNT
            and all(names)
            and len(set(names)) == SELECTION_SLOT_COUNT
        )
        projection = view.projection
        pre_confirm = projection.primary_cta == "CONFIRM_SELECTION_FACTS"
        ready_to_send = (
            projection.primary_cta == "REQUEST_SELECTION_ADVICE"
            and projection.provider_send_enabled
        )
        attempt_consumed = False
        if ready_to_send and view.persistence_reads_allowed:
            attempt_consumed = self._controller.gemini_selection_attempt_consumed()
        self.selection_roi_send_button.setEnabled(
            view.persistence_reads_allowed
            and projection.session_state == "SELECTION_OPEN"
            and self._controller.gemini_send_available
            and complete_unique
            and (pre_confirm or ready_to_send)
            and not attempt_consumed
        )

    def _apply_selection_roi_slot(self, slot: int) -> None:
        """Compatibility helper: explicit human top-assignment adoption."""

        if not self._mutation_slots_allowed() or not self._selection_roi_facts_editable:
            return
        if self._selection_roi_bundle_identity != self._selection_roi_render_identity:
            return
        match = self._selection_roi_slot_matches.get(slot)
        if match is None or match.assigned_label == UNKNOWN_LABEL:
            return
        self._set_selection_slot_value(
            slot,
            match.assigned_label,
            origin=SelectionInputOrigin.CANDIDATE_CLICK,
            user_locked=True,
        )

    def _apply_selection_roi_all(self, _checked: bool = False) -> None:
        """Compatibility helper: apply only the same >=0.80 auto-fill policy."""

        if not self._mutation_slots_allowed() or not self._selection_roi_facts_editable:
            return
        if self._selection_roi_bundle_identity != self._selection_roi_render_identity:
            return
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            match = self._selection_roi_slot_matches.get(slot)
            if match is not None:
                self._auto_fill_selection_roi_slot(slot, match)
        self._update_selection_roi_buttons()

    def _score_for_value(self, slot: int, value: str) -> float | None:
        match = self._selection_roi_slot_matches.get(slot)
        if match is None:
            return None
        if match.assigned_label == value:
            return match.assigned_score
        for candidate in match.top_candidates:
            if candidate.label == value:
                return candidate.score
        return None

    def _slot_feedback(self) -> SelectionFeedbackTuple:
        items: list[SelectionSlotFeedback] = []
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            value = self.opponent_team_inputs[slot - 1].text().strip()
            state = self._state_for_current_field(slot)
            items.append(
                SelectionSlotFeedback(
                    label=value,
                    value_origin=state.origin,
                    ocr_score=self._score_for_value(slot, value),
                )
            )
        return cast(SelectionFeedbackTuple, tuple(items))

    def _record_feedback_for_review(
        self,
        *,
        reviewed_selection_id: str,
        identity: SelectionIdentity,
        bundle: SelectionMatchBundle | None,
        bundle_identity: SelectionIdentity | None,
    ) -> None:
        if reviewed_selection_id in self._selection_roi_feedback_recorded_ids:
            return
        if (
            bundle is None
            or bundle.observation_id is None
            or bundle_identity != identity
        ):
            return
        session_id, match_id, generation = identity
        try:
            result = self._selection_roi_service.record_sent_observation(
                observation_id=bundle.observation_id,
                slot_feedback=self._slot_feedback(),
                reviewed_selection_id=reviewed_selection_id,
                session_id=session_id,
                match_id=match_id,
                generation=generation,
            )
        except (OSError, SelectionRoiError):
            self._set_selection_roi_status(
                "6体はcanonical保存済みですが、ROI学習画像の保存に失敗しました。"
            )
            return
        self._selection_roi_feedback_recorded_ids.add(reviewed_selection_id)
        self._set_selection_roi_status(
            "送信値を保存しました。"
            f"trusted追加={result.added_count} / "
            f"provisional追加={result.provisional_count} / "
            f"昇格={result.promoted_count} / "
            f"重複={result.duplicate_count} / "
            f"競合隔離={result.conflict_count}"
        )

    def _on_send_current_selection_to_gemini(self) -> None:
        """Trusted human action: canonicalize current fields, then send once."""

        if not self._mutation_slots_allowed():
            return
        before = self._controller.refresh()
        if (
            before.projection.session_state != "SELECTION_OPEN"
            or not self._controller.gemini_send_available
        ):
            return
        before_identity = self._selection_identity(before)
        bundle = self._selection_roi_bundle
        bundle_identity = self._selection_roi_bundle_identity

        current = before
        if before.projection.current_reviewed_selection_id is None:
            # Deliberately bypass this subclass's compatibility confirmation hook;
            # feedback belongs to this explicit send action and is recorded once.
            super()._on_confirm_facts(False)
            current = self._controller.refresh()
            if (
                current.error_message is not None
                or current.projection.current_reviewed_selection_id is None
                or self._selection_identity(current) != before_identity
            ):
                self.render_view(current)
                return

        reviewed_selection_id = current.projection.current_reviewed_selection_id
        if reviewed_selection_id is None:
            return
        self._record_feedback_for_review(
            reviewed_selection_id=reviewed_selection_id,
            identity=before_identity,
            bundle=bundle,
            bundle_identity=bundle_identity,
        )
        # Reuse the accepted trusted-send/controller/provider path. No retry,
        # fallback beyond its existing bounded policy, APPLY, or game action is
        # introduced here.
        super()._on_trusted_send_to_gemini()
        self.render_view(self._controller.refresh())

    def _on_confirm_facts(self, _checked: bool = False) -> None:
        """Compatibility-only explicit confirmation path, hidden in the UI."""

        if not self._mutation_slots_allowed():
            return
        before = self._controller.refresh()
        identity = self._selection_identity(before)
        bundle = self._selection_roi_bundle
        bundle_identity = self._selection_roi_bundle_identity
        super()._on_confirm_facts(_checked)
        after = self._controller.refresh()
        reviewed_selection_id = after.projection.current_reviewed_selection_id
        confirmed = (
            after.error_message is None
            and reviewed_selection_id is not None
            and reviewed_selection_id
            != before.projection.current_reviewed_selection_id
        )
        if confirmed and reviewed_selection_id is not None:
            self._record_feedback_for_review(
                reviewed_selection_id=reviewed_selection_id,
                identity=identity,
                bundle=bundle,
                bundle_identity=bundle_identity,
            )

    def _clear_selection_roi_candidates(self) -> None:
        self._selection_roi_bundle = None
        self._selection_roi_bundle_identity = None
        self._selection_roi_last_submitted_frame_id = None
        self._selection_roi_submitted_identities.clear()
        self._selection_roi_slot_matches.clear()
        self._selection_roi_candidate_values.clear()
        self._selection_roi_input_states = {
            slot: SelectionSlotInputState()
            for slot in range(1, SELECTION_SLOT_COUNT + 1)
        }
        if not hasattr(self, "_selection_roi_thumbnail_labels"):
            return
        for slot in range(1, SELECTION_SLOT_COUNT + 1):
            thumbnail = self._selection_roi_thumbnail_labels[slot]
            thumbnail.clear()
            thumbnail.setText("ROI —")
            self._selection_roi_candidate_labels[slot].setText("候補なし")
            self._selection_roi_origin_labels[slot].setText("未入力")
            for button in self._selection_roi_candidate_buttons[slot]:
                button.setVisible(False)
                button.setEnabled(False)
                button.setChecked(False)
        self.selection_roi_apply_all_button.setEnabled(False)
        self.selection_roi_send_button.setEnabled(False)

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
