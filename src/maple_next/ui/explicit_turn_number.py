"""Issue #8 explicit human Turn number input for the supported PySide6 path."""

from __future__ import annotations

import time
from collections.abc import Sequence

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFormLayout, QLabel, QLineEdit

from maple_next.capture.contracts import (
    CaptureStatus,
    CaptureStatusCode,
    FramePacket,
    VideoCaptureBackend,
)
from maple_next.capture.telemetry import SourceFpsSampler
from maple_next.ocr.contracts import OcrCandidateBackend
from maple_next.ui.controller import (
    OperatorInputError,
    OperatorView,
    SelectionFlowController,
    TurnFactsView,
)
from maple_next.ui.window import (
    _BATTLE_RECORD_TAB_INDEX,
    _CAPTURE_STATUS_LABELS,
    _PREVIEW_DISPLAY_LABEL,
    _PREVIEW_INVALID_LABEL,
    _PREVIEW_UNAVAILABLE_LABEL,
    MapleMainWindow,
)

_CAPTURE_TELEMETRY_INTERVAL_MS = 1000


def validate_explicit_turn_number(value: str, expected_turn_number: int | None) -> int:
    """Require the operator to enter the current canonical Turn number explicitly."""

    normalized = value.strip()
    if not normalized:
        raise OperatorInputError("Turn numberを入力してください。")
    try:
        turn_number = int(normalized)
    except ValueError as exc:
        raise OperatorInputError("Turn numberは1以上の整数で入力してください。") from exc
    if turn_number < 1:
        raise OperatorInputError("Turn numberは1以上の整数で入力してください。")
    if expected_turn_number is None:
        raise OperatorInputError("現在のTurn numberを確認できません。")
    if turn_number != expected_turn_number:
        raise OperatorInputError(
            f"Turn numberは画面上部の現在値 {expected_turn_number} と一致させてください。"
        )
    return turn_number


class ExplicitTurnNumberController(SelectionFlowController):
    """Adds explicit Turn-number validation without changing the canonical lifecycle."""

    def confirm_turn_facts_with_number(
        self,
        *,
        turn_number: str,
        self_active: str,
        opponent_active: str,
        self_hp: str,
        opponent_hp: str,
        legal_moves: Sequence[str],
        legal_switches: Sequence[str],
        human_note: str,
        human_confirmed: bool,
    ) -> OperatorView:
        try:
            validate_explicit_turn_number(turn_number, self.refresh().turn_number)
        except OperatorInputError as error:
            self._error_message = str(error)
            return self.refresh()
        return super().confirm_turn_facts(
            self_active=self_active,
            opponent_active=opponent_active,
            self_hp=self_hp,
            opponent_hp=opponent_hp,
            legal_moves=legal_moves,
            legal_switches=legal_switches,
            human_note=human_note,
            human_confirmed=human_confirmed,
        )


class ExplicitTurnNumberWindow(MapleMainWindow):
    """Supported window with stable capture telemetry and explicit Turn input.

    Preview pixels still follow the fast latest-only path. Capture text is
    updated only on a state transition or by the one-second telemetry timer,
    and duplicate strings never call QLabel.setText(). This prevents the
    rapidly changing frame-age label from flickering without reducing video
    cadence.
    """

    def __init__(
        self,
        controller: ExplicitTurnNumberController,
        *,
        capture_backend: VideoCaptureBackend | None = None,
        ocr_backend: OcrCandidateBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        self._explicit_turn_controller = controller
        self.turn_number_input: QLineEdit | None = None

        # Primitive telemetry state is ready before MapleMainWindow starts its
        # capture lifecycle. The QTimer itself is created only after QWidget
        # construction has completed.
        self._capture_telemetry_timer: QTimer | None = None
        self._capture_telemetry_status: CaptureStatus | None = None
        self._capture_telemetry_last_status_code: str | None = None
        self._capture_telemetry_preview_installed = False
        self._capture_telemetry_set_text_count = 0
        self._capture_telemetry_clock = time.monotonic_ns
        self._source_fps_sampler = SourceFpsSampler()

        super().__init__(
            controller,
            capture_backend=capture_backend,
            ocr_backend=ocr_backend,
            auto_start_capture=auto_start_capture,
        )

        turn_number_input = QLineEdit()
        turn_number_input.setPlaceholderText("画面上部のTurn numberを入力")
        self.turn_number_input = turn_number_input
        layout = self.turn_facts_group.layout()
        if not isinstance(layout, QFormLayout):
            raise RuntimeError("Turn facts layout must be QFormLayout")
        layout.insertRow(0, "Turn number（人間入力）", turn_number_input)

        # A fixed minimum width prevents 9.9/29.9/100.0 and the initial dash
        # from repeatedly changing the surrounding layout width.
        self.capture_freshness_label.setMinimumWidth(230)
        self.capture_freshness_label.setWordWrap(False)
        self.capture_device_label.setMinimumWidth(230)
        self.capture_device_label.setWordWrap(False)

        telemetry_timer = QTimer(self)
        telemetry_timer.setInterval(_CAPTURE_TELEMETRY_INTERVAL_MS)
        telemetry_timer.timeout.connect(self._poll_capture_telemetry)
        self._capture_telemetry_timer = telemetry_timer
        self._sync_capture_telemetry_timer(force_restart=True)
        self.render_view()

    # -- capture telemetry ------------------------------------------------------

    @staticmethod
    def _capture_status_detail(
        status: CaptureStatus, *, preview_installed: bool
    ) -> tuple[bool, str]:
        if preview_installed:
            return True, _PREVIEW_DISPLAY_LABEL
        if status.status == CaptureStatusCode.AVAILABLE:
            return False, _PREVIEW_INVALID_LABEL
        return status.available, _CAPTURE_STATUS_LABELS.get(
            status.status, status.operator_message or _PREVIEW_UNAVAILABLE_LABEL
        )

    def _set_capture_label_text_if_changed(self, label: QLabel, text: str) -> bool:
        if label.text() == text:
            return False
        label.setText(text)
        self._capture_telemetry_set_text_count += 1
        return True

    def set_capture_status(self, *, available: bool, detail: str = "") -> None:
        """Update capture status only when its visible text actually changes."""

        if available:
            text = detail or "fresh frameを受信しています。"
        else:
            text = detail or "capture未接続 — manual-safe fallback。手動入力でturnを継続できます。"
        if hasattr(self, "capture_status_label"):
            self._set_capture_label_text_if_changed(self.capture_status_label, text)

    def _reset_capture_telemetry_window(
        self, *, clear_status_code: bool = True
    ) -> None:
        self._source_fps_sampler.reset()
        if clear_status_code:
            self._capture_telemetry_last_status_code = None

    def _sync_capture_telemetry_timer(self, *, force_restart: bool = False) -> None:
        timer = self._capture_telemetry_timer
        if timer is None:
            return
        should_run = (
            self._persistence_reads_allowed
            and self._capture_polling_requested
            and self.header_tabs.currentIndex() == _BATTLE_RECORD_TAB_INDEX
            and self._preview_timer.isActive()
        )
        if not should_run:
            timer.stop()
            self._reset_capture_telemetry_window()
            return
        if force_restart or not timer.isActive():
            timer.stop()
            self._reset_capture_telemetry_window()
            self._poll_capture_telemetry()
            if self._capture_telemetry_status is not None:
                self._capture_telemetry_last_status_code = (
                    self._capture_telemetry_status.status
                )
            timer.start()

    def _resume_capture_polling(self) -> None:
        super()._resume_capture_polling()
        self._sync_capture_telemetry_timer()

    def _on_header_tab_changed(self, index: int) -> None:
        super()._on_header_tab_changed(index)
        self._sync_capture_telemetry_timer(
            force_restart=index == _BATTLE_RECORD_TAB_INDEX
        )

    def _on_reconnect_capture(self, _checked: bool = False) -> None:
        timer = self._capture_telemetry_timer
        if timer is not None:
            timer.stop()
        self._reset_capture_telemetry_window()
        if hasattr(self, "capture_status_label"):
            self._set_capture_label_text_if_changed(
                self.capture_status_label, "capture再接続中…"
            )
            self._set_capture_label_text_if_changed(
                self.capture_freshness_label, "入力: — × — / — fps"
            )
        super()._on_reconnect_capture(_checked)
        self._sync_capture_telemetry_timer(force_restart=True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        timer = self._capture_telemetry_timer
        if timer is not None:
            timer.stop()
        self._reset_capture_telemetry_window()
        super().closeEvent(event)

    def render_view(self, view: OperatorView | None = None) -> None:
        super().render_view(view)
        self._sync_capture_telemetry_timer()

    def _poll_capture_preview(self) -> None:
        """Fast path: update pixels; update text only on a status transition."""

        if not self._persistence_reads_allowed:
            return
        status, frame = self._capture_service.latest_preview_snapshot()
        preview_installed = self._render_capture_snapshot(status, frame)
        previous_code = self._capture_telemetry_last_status_code
        self._capture_telemetry_status = status
        self._capture_telemetry_preview_installed = preview_installed
        if status.status != previous_code:
            if status.status == CaptureStatusCode.AVAILABLE:
                self._source_fps_sampler.reset()
            self._capture_telemetry_last_status_code = status.status
            self._render_capture_telemetry(status, measure_fps=False)

    def _render_capture_status(
        self, status: CaptureStatus, frame: FramePacket | None = None
    ) -> None:
        """Deterministic one-shot wrapper retained for focused tests/probes."""

        preview_installed = self._render_capture_snapshot(status, frame)
        self._capture_telemetry_status = status
        self._capture_telemetry_preview_installed = preview_installed
        self._capture_telemetry_last_status_code = status.status
        self._render_capture_telemetry(status, measure_fps=False)

    def _clear_capture_preview(self, *, placeholder: str) -> None:
        installed = self.capture_preview_label.pixmap()
        already_clear = (
            self._capture_preview_frame_id is None
            and self._capture_preview_pixmap.isNull()
            and (installed is None or installed.isNull())
            and self.capture_preview_label.text() == placeholder
        )
        if already_clear:
            return
        super()._clear_capture_preview(placeholder=placeholder)

    def _poll_capture_telemetry(self) -> None:
        if (
            not self._persistence_reads_allowed
            or self.header_tabs.currentIndex() != _BATTLE_RECORD_TAB_INDEX
        ):
            return
        status = self._capture_telemetry_status
        if status is None:
            return
        self._render_capture_telemetry(status, measure_fps=True)

    def _render_capture_telemetry(
        self, status: CaptureStatus, *, measure_fps: bool
    ) -> None:
        available, detail = self._capture_status_detail(
            status, preview_installed=self._capture_telemetry_preview_installed
        )
        self.set_capture_status(available=available, detail=detail)
        self._set_capture_label_text_if_changed(
            self.capture_device_label, f"デバイス: {status.device_label or '—'}"
        )

        metrics = self._capture_service.capture_metrics()
        resolution = metrics.get("selected_resolution")
        if (
            not isinstance(resolution, tuple)
            or len(resolution) != 2
            or not all(isinstance(value, int) and value > 0 for value in resolution)
        ):
            resolution = (
                (status.width, status.height)
                if status.width is not None and status.height is not None
                else None
            )
        resolution_text = (
            f"{resolution[0]}×{resolution[1]}" if resolution is not None else "— × —"
        )

        fps: float | None = None
        incoming_count = metrics.get("incoming_frame_count")
        if (
            measure_fps
            and status.status == CaptureStatusCode.AVAILABLE
            and isinstance(incoming_count, int)
        ):
            fps = self._source_fps_sampler.sample(
                frame_count=incoming_count,
                now_ns=self._capture_telemetry_clock(),
            )
        elif status.status != CaptureStatusCode.AVAILABLE:
            self._source_fps_sampler.reset()

        fps_text = f"{fps:.1f}" if fps is not None else "—"
        if status.status == CaptureStatusCode.FRAME_STALE:
            input_text = f"入力: {resolution_text} / stale"
        elif status.status == CaptureStatusCode.AVAILABLE:
            input_text = f"入力: {resolution_text} / {fps_text} fps"
        else:
            input_text = f"入力: {resolution_text} / — fps"
        self._set_capture_label_text_if_changed(self.capture_freshness_label, input_text)

    # -- explicit Turn input ----------------------------------------------------

    def _clear_turn_inputs(self) -> None:
        super()._clear_turn_inputs()
        if self.turn_number_input is not None:
            self.turn_number_input.clear()

    def _load_turn_facts(self, facts: TurnFactsView) -> None:
        super()._load_turn_facts(facts)
        if self.turn_number_input is not None:
            self.turn_number_input.setText(str(facts.turn_number))

    def _set_turn_facts_editable(self, enabled: bool) -> None:
        super()._set_turn_facts_editable(enabled)
        if self.turn_number_input is not None:
            self.turn_number_input.setEnabled(enabled)

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        turn_number_input = self.turn_number_input
        if turn_number_input is None:
            raise RuntimeError("Turn number input is not initialized")
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        switches = [
            checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()
        ]
        view = self._explicit_turn_controller.confirm_turn_facts_with_number(
            turn_number=turn_number_input.text(),
            self_active=self.self_active_box.currentText(),
            opponent_active=self.opponent_active_input.text(),
            self_hp=self.self_hp_box.currentText(),
            opponent_hp=self.opponent_hp_box.currentText(),
            legal_moves=moves,
            legal_switches=switches,
            human_note=self.turn_note_input.text(),
            human_confirmed=self.turn_facts_confirm_checkbox.isChecked(),
        )
        self.render_view(view)
