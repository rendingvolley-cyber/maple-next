"""Human-triggered one-frame Turn capture and candidate-only OCR UI."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    CaptureStatusCode,
    FrameKind,
    FramePacket,
    VideoCaptureBackend,
)
from maple_next.ocr.contracts import OcrCandidate, OcrFieldKey
from maple_next.turn_ocr import (
    TURN_SNAPSHOT_AUTO_FILL_CONFIDENCE,
    TURN_SNAPSHOT_MAX_FRAME_AGE_MS,
    TurnRoiConfigError,
    TurnSnapshotIdentity,
    TurnSnapshotOcrService,
    TurnSnapshotOcrWorker,
    TurnSnapshotRequest,
    TurnSnapshotResult,
    TurnSnapshotStatus,
    load_turn_roi_config,
)
from maple_next.ui.controller import OperatorView
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_snapshot_window import SelectionSnapshotMatchFlowWindow

_BATTLE_RECORD_TAB_INDEX = 1
_PLACEHOLDER = "選択してください"
_TURN_PENDING_STATE = "TURN_CAPTURE_PENDING"
_TURN_SNAPSHOT_ORIGIN_EMPTY = "未入力"
_TURN_SNAPSHOT_ORIGIN_CARRY_FORWARD = "前Turn確定値の引き継ぎ・未確認"
_TURN_SNAPSHOT_ORIGIN_CONFIRMED_DRAFT = "確定済み値の引き継ぎ・OCR上書き不可"
_TURN_SNAPSHOT_ORIGIN_OCR = "OCR候補・未確認"


class TurnSnapshotMatchFlowWindow(SelectionSnapshotMatchFlowWindow):
    """Freeze and OCR one immutable frame per explicit Turn transition."""

    def __init__(
        self,
        controller: MatchFlowController,
        *,
        ocr_data_directory: Path,
        capture_backend: VideoCaptureBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        self._turn_snapshot_ready = False
        self._turn_snapshot_generation = 0
        self._turn_snapshot_active_identity: TurnSnapshotIdentity | None = None
        self._turn_snapshot_worker: TurnSnapshotOcrWorker | None = None
        self._turn_snapshot_setting_fields = False
        self._turn_snapshot_transition_in_progress = False
        self._turn_snapshot_field_locks = {
            field.value: False
            for field in (
                OcrFieldKey.SELF_ACTIVE,
                OcrFieldKey.OPPONENT_ACTIVE,
                OcrFieldKey.SELF_HP,
                OcrFieldKey.OPPONENT_HP,
            )
        }
        self._turn_snapshot_origins = {
            field_key: _TURN_SNAPSHOT_ORIGIN_EMPTY
            for field_key in self._turn_snapshot_field_locks
        }
        super().__init__(
            controller,
            ocr_data_directory=ocr_data_directory,
            capture_backend=capture_backend,
            auto_start_capture=auto_start_capture,
        )

        config_path = ocr_data_directory / "turn" / "config" / "roi_config.json"
        try:
            config = load_turn_roi_config(config_path)
        except TurnRoiConfigError:
            self._turn_snapshot_worker = None
            self._set_turn_snapshot_status(
                TurnSnapshotStatus.OCR_UNAVAILABLE,
                "Turn ROI設定を読み込めません。Turn factsは手動入力できます。",
            )
        else:
            service = TurnSnapshotOcrService(config)
            self._turn_snapshot_worker = TurnSnapshotOcrWorker(service)
            self._turn_snapshot_worker.result_ready.connect(self._on_turn_snapshot_result)
            self.turn_snapshot_roi_label.setText(service.roi_config_provenance)

        self._capture_timer.stop()
        self._connect_turn_snapshot_manual_locks()
        self._turn_snapshot_ready = True
        self.render_view()

    # -- UI construction -----------------------------------------------------

    def _build_turn_facts_group(self) -> None:
        super()._build_turn_facts_group()
        self.turn_snapshot_group = QGroupBox("このTurnで固定した画像 — 後続映像では更新しません")
        layout = QVBoxLayout(self.turn_snapshot_group)
        self.turn_snapshot_status_label = QLabel("IDLE")
        self.turn_snapshot_status_label.setWordWrap(True)
        layout.addWidget(self.turn_snapshot_status_label)

        metadata = QFormLayout()
        self.turn_snapshot_identity_label = QLabel("Turn —")
        self.turn_snapshot_frame_label = QLabel("Frame —")
        self.turn_snapshot_roi_label = QLabel("ROI —")
        metadata.addRow("Identity", self.turn_snapshot_identity_label)
        metadata.addRow("Frame", self.turn_snapshot_frame_label)
        metadata.addRow("ROI", self.turn_snapshot_roi_label)
        layout.addLayout(metadata)
        # Exposed so a compacting subclass (Bundle C) can fully collapse
        # these diagnostic rows -- including their auto-generated row
        # labels -- via QFormLayout.setRowVisible instead of only hiding
        # the value widget, which otherwise leaves the row's height
        # reserved in this fixed, non-scrolling group.
        self._turn_snapshot_metadata_form = metadata

        self.turn_snapshot_image_label = QLabel("固定画像なし")
        self.turn_snapshot_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.turn_snapshot_image_label.setMinimumSize(320, 180)
        self.turn_snapshot_image_label.setStyleSheet(
            "QLabel { background: #111827; color: #d1d5db; border: 1px solid #374151; }"
        )
        layout.addWidget(self.turn_snapshot_image_label)

        crop_grid = QGridLayout()
        self._turn_snapshot_crop_labels: dict[str, QLabel] = {}
        self._turn_snapshot_crop_title_labels: dict[str, QLabel] = {}
        for index, (field_key, title) in enumerate(
            (
                (OcrFieldKey.SELF_ACTIVE.value, "自分 active ROI"),
                (OcrFieldKey.OPPONENT_ACTIVE.value, "相手 active ROI"),
                (OcrFieldKey.SELF_HP.value, "自分 HP ROI"),
                (OcrFieldKey.OPPONENT_HP.value, "相手 HP ROI"),
            )
        ):
            title_label = QLabel(title)
            crop_label = QLabel("—")
            crop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            crop_label.setMinimumSize(150, 48)
            crop_label.setStyleSheet(
                "QLabel { background: #f3f4f6; border: 1px solid #d1d5db; }"
            )
            row = (index // 2) * 2
            column = index % 2
            crop_grid.addWidget(title_label, row, column)
            crop_grid.addWidget(crop_label, row + 1, column)
            self._turn_snapshot_crop_labels[field_key] = crop_label
            self._turn_snapshot_crop_title_labels[field_key] = title_label
        layout.addLayout(crop_grid)

        origins = QFormLayout()
        self._turn_snapshot_origin_labels: dict[str, QLabel] = {}
        for field_key, title in (
            (OcrFieldKey.SELF_ACTIVE.value, "自分active origin"),
            (OcrFieldKey.OPPONENT_ACTIVE.value, "相手active origin"),
            (OcrFieldKey.SELF_HP.value, "自分HP origin"),
            (OcrFieldKey.OPPONENT_HP.value, "相手HP origin"),
        ):
            label = QLabel("未入力")
            self._turn_snapshot_origin_labels[field_key] = label
            origins.addRow(title, label)
        layout.addLayout(origins)
        # Exposed for the same reason as ``_turn_snapshot_metadata_form``
        # above -- lets Bundle C fully collapse these rows via
        # setRowVisible rather than leaving their height reserved.
        self._turn_snapshot_origins_form = origins

        self.retake_turn_snapshot_button = QPushButton("このTurnを撮り直す")
        self.retake_turn_snapshot_button.clicked.connect(self._on_retake_turn_snapshot)
        layout.addWidget(self.retake_turn_snapshot_button)
        self._center_column_layout.addWidget(self.turn_snapshot_group)

    def _connect_turn_snapshot_manual_locks(self) -> None:
        self.self_active_box.activated.connect(
            lambda _index: self._mark_turn_snapshot_manual(OcrFieldKey.SELF_ACTIVE.value)
        )
        self.opponent_active_input.textEdited.connect(
            lambda _text: self._mark_turn_snapshot_manual(OcrFieldKey.OPPONENT_ACTIVE.value)
        )
        self.self_hp_box.activated.connect(
            lambda _index: self._mark_turn_snapshot_manual(OcrFieldKey.SELF_HP.value)
        )
        self.opponent_hp_box.activated.connect(
            lambda _index: self._mark_turn_snapshot_manual(OcrFieldKey.OPPONENT_HP.value)
        )

    # -- continuous capture policy ------------------------------------------

    def _resume_capture_polling(self) -> None:
        """Keep live preview, but never run continuous Turn OCR."""

        if not self._persistence_reads_allowed or not self._capture_polling_requested:
            self._preview_timer.stop()
            self._capture_timer.stop()
            return
        if not self._capture_service_started:
            self._capture_service.start()
            self._capture_service_started = True
        if self.header_tabs.currentIndex() != _BATTLE_RECORD_TAB_INDEX:
            self._preview_timer.stop()
            self._capture_timer.stop()
            return
        self._capture_timer.stop()
        if not self._preview_timer.isActive():
            self._poll_capture_preview()
            self._preview_timer.start()

    def _poll_capture_status(self) -> None:
        if not self._persistence_reads_allowed:
            return
        self._poll_capture_preview()

    def _poll_capture_ocr(self) -> None:
        """No-op: Turn OCR is triggered only by human Turn buttons."""

        return

    # -- rendering -----------------------------------------------------------

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._controller.refresh()
        super().render_view(current)
        if not self._turn_snapshot_ready:
            return
        self._capture_timer.stop()
        self.start_turn_button.setText("TURN 1を撮影してOCR")
        self.next_turn_button.setText("NEXT TURNを撮影してOCR")
        turn_visible = current.projection.session_state in {
            "TURN_CAPTURE_PENDING",
            "TURN_REVIEWED",
            "TURN_RECORDED",
        }
        self.turn_snapshot_group.setVisible(turn_visible)
        self.ocr_candidates_group.setVisible(turn_visible)
        self.retake_turn_snapshot_button.setVisible(
            current.projection.session_state == _TURN_PENDING_STATE
        )
        self.retake_turn_snapshot_button.setEnabled(
            current.persistence_reads_allowed
            and current.projection.session_state == _TURN_PENDING_STATE
            and not self._turn_snapshot_transition_in_progress
        )
        if current.projection.session_state == _TURN_PENDING_STATE:
            self.primary_cta_label.setText("固定画像のTurn factsを確認")
            self.guidance_label.setText(
                "ボタンを押した瞬間の1枚だけを解析します。候補を確認・修正してから"
                "Turn factsを保存してください。"
            )
        self._render_turn_snapshot_origins()

    def _set_turn_snapshot_status(self, status: str, message: str) -> None:
        if not hasattr(self, "turn_snapshot_status_label"):
            return
        self.turn_snapshot_status_label.setText(f"{status}: {message}")

    def _render_turn_snapshot_origins(self) -> None:
        if not hasattr(self, "_turn_snapshot_origin_labels"):
            return
        for field_key, label in self._turn_snapshot_origin_labels.items():
            label.setText(
                self._turn_snapshot_origins.get(
                    field_key, _TURN_SNAPSHOT_ORIGIN_EMPTY
                )
            )

    @staticmethod
    def _set_image_label(label: QLabel, image: QImage, *, width: int, height: int) -> None:
        if image.isNull():
            label.clear()
            label.setText("—")
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            label.clear()
            label.setText("—")
            return
        label.setText("")
        label.setPixmap(
            pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _render_frozen_frame(self, frame: FramePacket) -> None:
        image = frame.image
        if not isinstance(image, QImage):
            return
        self._set_image_label(self.turn_snapshot_image_label, image, width=480, height=270)
        self.turn_snapshot_frame_label.setText(frame.frame_id)

    def _render_turn_snapshot_result_images(self, result: TurnSnapshotResult) -> None:
        self._set_image_label(
            self.turn_snapshot_image_label,
            result.frozen_image,
            width=480,
            height=270,
        )
        mapping = {
            OcrFieldKey.SELF_ACTIVE.value: "self_active",
            OcrFieldKey.OPPONENT_ACTIVE.value: "opponent_active",
            OcrFieldKey.SELF_HP.value: "self_hp",
            OcrFieldKey.OPPONENT_HP.value: "opponent_hp",
        }
        for field_key, crop_key in mapping.items():
            self._set_image_label(
                self._turn_snapshot_crop_labels[field_key],
                result.crops.get(crop_key, QImage()),
                width=180,
                height=64,
            )

    # -- snapshot identity and capture --------------------------------------

    @staticmethod
    def _base_turn_identity(view: OperatorView) -> tuple[object, ...] | None:
        projection = view.projection
        values = (
            projection.session_id,
            projection.match_id,
            projection.generation,
            projection.current_turn_id,
            projection.turn_number,
            projection.battle_revision,
        )
        if any(value is None for value in values):
            return None
        return values

    def _next_turn_snapshot_identity(
        self,
        view: OperatorView,
    ) -> TurnSnapshotIdentity | None:
        projection = view.projection
        base = self._base_turn_identity(view)
        if base is None:
            return None
        self._turn_snapshot_generation += 1
        assert projection.session_id is not None
        assert projection.match_id is not None
        assert projection.generation is not None
        assert projection.current_turn_id is not None
        assert projection.turn_number is not None
        assert projection.battle_revision is not None
        return TurnSnapshotIdentity(
            session_id=projection.session_id,
            match_id=projection.match_id,
            generation=projection.generation,
            turn_id=projection.current_turn_id,
            turn_number=projection.turn_number,
            battle_revision=projection.battle_revision,
            snapshot_generation=self._turn_snapshot_generation,
        )

    def _identity_is_current(
        self,
        identity: TurnSnapshotIdentity,
        view: OperatorView,
    ) -> bool:
        projection = view.projection
        return (
            projection.session_id == identity.session_id
            and projection.match_id == identity.match_id
            and projection.generation == identity.generation
            and projection.current_turn_id == identity.turn_id
            and projection.turn_number == identity.turn_number
            and projection.battle_revision == identity.battle_revision
            and self._turn_snapshot_active_identity == identity
        )

    def _freeze_turn_frame(self) -> tuple[FramePacket | None, str, str]:
        status, frame = self._capture_service.latest_snapshot()
        if (
            status.status != CaptureStatusCode.AVAILABLE
            or not status.available
            or not status.fresh
            or status.age_ms is None
            or status.age_ms > TURN_SNAPSHOT_MAX_FRAME_AGE_MS
            or frame is None
        ):
            status_code = (
                TurnSnapshotStatus.FRAME_STALE
                if status.available and status.age_ms is not None
                else TurnSnapshotStatus.NO_FRAME
            )
            return (
                None,
                status_code,
                status.operator_message
                or "Turnボタン押下時の新しい映像を取得できません。手動入力できます。",
            )
        image = frame.image
        if (
            frame.frame_kind is not FrameKind.CANONICAL
            or frame.width != CANONICAL_FRAME_WIDTH
            or frame.height != CANONICAL_FRAME_HEIGHT
            or not isinstance(image, QImage)
            or image.isNull()
        ):
            return (
                None,
                TurnSnapshotStatus.FRAME_NOT_CANONICAL,
                "Turn用映像が1280x720 canonical imageではありません。手動入力できます。",
            )
        frozen_image = image.copy()
        if frozen_image.isNull():
            return (
                None,
                TurnSnapshotStatus.NO_FRAME,
                "Turn用スクリーンショットを固定できません。手動入力できます。",
            )
        return (
            replace(frame, image=frozen_image),
            TurnSnapshotStatus.CAPTURED,
            f"押下時の1枚を固定しました（age={status.age_ms}ms）。解析します。",
        )

    def _reset_turn_snapshot_draft(self) -> None:
        self._latest_ocr_bundle = None
        for label in self._ocr_field_labels.values():
            label.setText("—")
        for button in self._ocr_adopt_buttons.values():
            button.setEnabled(False)
        for field_key in self._turn_snapshot_field_locks:
            self._turn_snapshot_field_locks[field_key] = False
            self._turn_snapshot_origins[field_key] = _TURN_SNAPSHOT_ORIGIN_EMPTY
        for label in self._turn_snapshot_crop_labels.values():
            label.clear()
            label.setText("—")
        self._render_turn_snapshot_origins()

    def _submit_frozen_turn_frame(
        self,
        *,
        frame: FramePacket | None,
        status: str,
        message: str,
        reset_draft: bool,
    ) -> None:
        current = self._controller.refresh()
        if current.projection.session_state != _TURN_PENDING_STATE:
            return
        identity = self._next_turn_snapshot_identity(current)
        if identity is None:
            return
        self._turn_snapshot_active_identity = identity
        if reset_draft:
            self._reset_turn_snapshot_draft()
        self.turn_snapshot_identity_label.setText(
            f"Turn {identity.turn_number} / snapshot {identity.snapshot_generation}"
        )
        self._set_turn_snapshot_status(status, message)
        if frame is None:
            self.turn_snapshot_frame_label.setText("—")
            return
        bound_frame = replace(
            frame,
            frame_id=(
                f"{frame.frame_id}:turn:{identity.turn_number}:"
                f"snapshot:{identity.snapshot_generation}"
            ),
        )
        self._render_frozen_frame(bound_frame)
        worker = self._turn_snapshot_worker
        if worker is None:
            self._set_turn_snapshot_status(
                TurnSnapshotStatus.OCR_UNAVAILABLE,
                "Turn OCR backendを開始できません。手動入力できます。",
            )
            return
        if current.applied_selection is None:
            self._set_turn_snapshot_status(
                TurnSnapshotStatus.OCR_UNAVAILABLE,
                "APPLY済み3体を読み込めないためOCR候補を制約できません。",
            )
            return
        request = TurnSnapshotRequest(
            identity=identity,
            frame=bound_frame,
            self_active_candidates=current.applied_selection.selected_three,
            opponent_active_candidates=current.opponent_team,
        )
        self._set_turn_snapshot_status(
            TurnSnapshotStatus.ANALYZING,
            "固定画像だけをバックグラウンド解析しています。",
        )
        worker.submit(request)

    # -- human-triggered transitions ----------------------------------------

    def _on_start_turn(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed() or self._turn_snapshot_transition_in_progress:
            return
        self._turn_snapshot_transition_in_progress = True
        try:
            frozen, status, message = self._freeze_turn_frame()
            previous = self._base_turn_identity(self._controller.refresh())
            super()._on_start_turn(_checked)
            current = self._controller.refresh()
            if (
                current.projection.session_state == _TURN_PENDING_STATE
                and self._base_turn_identity(current) != previous
            ):
                self._submit_frozen_turn_frame(
                    frame=frozen,
                    status=status,
                    message=message,
                    reset_draft=True,
                )
        finally:
            self._turn_snapshot_transition_in_progress = False
            self.render_view()

    def _on_next_turn(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed() or self._turn_snapshot_transition_in_progress:
            return
        self._turn_snapshot_transition_in_progress = True
        try:
            frozen, status, message = self._freeze_turn_frame()
            previous = self._base_turn_identity(self._controller.refresh())
            super()._on_next_turn(_checked)
            current = self._controller.refresh()
            if (
                current.projection.session_state == _TURN_PENDING_STATE
                and self._base_turn_identity(current) != previous
            ):
                self._submit_frozen_turn_frame(
                    frame=frozen,
                    status=status,
                    message=message,
                    reset_draft=True,
                )
        finally:
            self._turn_snapshot_transition_in_progress = False
            self.render_view()

    def _on_retake_turn_snapshot(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed() or self._turn_snapshot_transition_in_progress:
            return
        current = self._controller.refresh()
        if current.projection.session_state != _TURN_PENDING_STATE:
            return
        frozen, status, message = self._freeze_turn_frame()
        self._submit_frozen_turn_frame(
            frame=frozen,
            status=status,
            message=message,
            reset_draft=False,
        )
        self.render_view()

    # -- result application --------------------------------------------------

    def _on_turn_snapshot_result(self, payload: object) -> None:
        if not isinstance(payload, TurnSnapshotResult):
            return
        current = self._controller.refresh()
        if (
            not current.persistence_reads_allowed
            or current.projection.session_state != _TURN_PENDING_STATE
            or not self._identity_is_current(payload.identity, current)
        ):
            return
        self._latest_ocr_bundle = payload.bundle
        self._render_ocr_candidates(payload.bundle)
        self._render_turn_snapshot_result_images(payload)
        self.turn_snapshot_roi_label.setText(payload.roi_config_provenance)
        self._set_turn_snapshot_status(payload.status, payload.operator_message)
        self._auto_fill_turn_snapshot_candidates(payload.bundle.candidates)
        self._render_turn_snapshot_origins()

    def _auto_fill_turn_snapshot_candidates(
        self,
        candidates: tuple[OcrCandidate, ...],
    ) -> None:
        best_by_field: dict[str, OcrCandidate] = {}
        for candidate in candidates:
            current = best_by_field.get(candidate.field_key)
            if current is None or candidate.rank < current.rank:
                best_by_field[candidate.field_key] = candidate
        self._turn_snapshot_setting_fields = True
        try:
            for field_key, candidate in best_by_field.items():
                if candidate.confidence < TURN_SNAPSHOT_AUTO_FILL_CONFIDENCE:
                    continue
                if candidate.suggested_value == "UNKNOWN":
                    continue
                if self._turn_snapshot_field_locks.get(field_key, False):
                    continue
                origin = self._turn_snapshot_origins.get(
                    field_key, _TURN_SNAPSHOT_ORIGIN_EMPTY
                )
                if (
                    not self._turn_field_is_empty(field_key)
                    and origin != _TURN_SNAPSHOT_ORIGIN_CARRY_FORWARD
                ):
                    continue
                self._set_turn_field(field_key, candidate.suggested_value)
                self._turn_snapshot_origins[field_key] = _TURN_SNAPSHOT_ORIGIN_OCR
        finally:
            self._turn_snapshot_setting_fields = False

    def _bind_turn_snapshot_draft_field(
        self,
        field_key: str,
        value: str,
        *,
        ocr_replaceable: bool,
    ) -> None:
        """Bind a draft value without clobbering newer current-Turn input.

        An unchanged value carried from the previous confirmed state still
        awaits observation for this Turn, so a fresh identity-matched OCR
        candidate may replace it. A value explicitly established by the
        previous action result remains protected. Once OCR or the operator
        changes a field in this Turn, later renders must not restore the
        durable draft over that newer in-process value.
        """

        if self._turn_snapshot_field_locks.get(field_key, False):
            return
        origin = self._turn_snapshot_origins.get(
            field_key, _TURN_SNAPSHOT_ORIGIN_EMPTY
        )
        if origin not in {
            _TURN_SNAPSHOT_ORIGIN_EMPTY,
            _TURN_SNAPSHOT_ORIGIN_CARRY_FORWARD,
            _TURN_SNAPSHOT_ORIGIN_CONFIRMED_DRAFT,
        }:
            return
        self._set_turn_field(field_key, value)
        self._turn_snapshot_origins[field_key] = (
            _TURN_SNAPSHOT_ORIGIN_CARRY_FORWARD
            if ocr_replaceable
            else _TURN_SNAPSHOT_ORIGIN_CONFIRMED_DRAFT
        )

    def _turn_field_is_empty(self, field_key: str) -> bool:
        if field_key == OcrFieldKey.SELF_ACTIVE.value:
            return self.self_active_box.currentText() in {"", _PLACEHOLDER}
        if field_key == OcrFieldKey.OPPONENT_ACTIVE.value:
            return not self.opponent_active_input.text().strip()
        if field_key == OcrFieldKey.SELF_HP.value:
            return self.self_hp_box.currentText() in {"", _PLACEHOLDER}
        if field_key == OcrFieldKey.OPPONENT_HP.value:
            return self.opponent_hp_box.currentText() in {"", _PLACEHOLDER}
        return False

    def _set_turn_field(self, field_key: str, value: str) -> None:
        if field_key == OcrFieldKey.SELF_ACTIVE.value:
            self.self_active_box.setCurrentText(value)
        elif field_key == OcrFieldKey.OPPONENT_ACTIVE.value:
            self.opponent_active_input.setText(value)
        elif field_key == OcrFieldKey.SELF_HP.value:
            self.self_hp_box.setCurrentText(value)
        elif field_key == OcrFieldKey.OPPONENT_HP.value:
            self.opponent_hp_box.setCurrentText(value)

    def _mark_turn_snapshot_manual(self, field_key: str) -> None:
        if (
            not self._turn_snapshot_ready
            or self._turn_snapshot_setting_fields
            or self._last_rendered_session_state != _TURN_PENDING_STATE
        ):
            return
        self._turn_snapshot_field_locks[field_key] = True
        self._turn_snapshot_origins[field_key] = "手動入力・人間操作"
        self._render_turn_snapshot_origins()

    def _on_adopt_ocr_candidate(self, field_key: str) -> None:
        if not self._mutation_slots_allowed():
            return
        super()._on_adopt_ocr_candidate(field_key)
        self._turn_snapshot_field_locks[field_key] = True
        self._turn_snapshot_origins[field_key] = "OCR候補から選択・人間操作"
        self._render_turn_snapshot_origins()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        worker = self._turn_snapshot_worker
        self._turn_snapshot_worker = None
        if worker is not None:
            worker.close()
        super().closeEvent(event)
