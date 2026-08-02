"""PySide6 Widgets shell for the human-operated Battle-1 flow."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from maple_next.capture.contracts import (
    CaptureStatus,
    CaptureStatusCode,
    FramePacket,
    VideoCaptureBackend,
)
from maple_next.capture.qt_ugreen import QtMultimediaUgreenBackend
from maple_next.capture.service import CaptureService
from maple_next.domain.enums import ActionOrder, ActionType, HpBucket
from maple_next.ocr.contracts import (
    OcrCandidate,
    OcrCandidateBackend,
    OcrCandidateBundle,
    OcrCandidateContext,
    OcrFieldKey,
)
from maple_next.ocr.service import OcrCandidateService, UnavailableOcrCandidateBackend
from maple_next.ui.controller import OperatorView, SelectionFlowController, TurnFactsView
from maple_next.ui.trusted_input import TrustedSendButton

#: Poll interval for capture status / OCR candidate refresh. Display-only:
#: never triggers a Turn Advice send, a domain command, or a canonical write.
_CAPTURE_POLL_INTERVAL_MS = 500

_CAPTURE_STATUS_LABELS: dict[str, str] = {
    CaptureStatusCode.STOPPED: (
        "capture未接続 — manual-safe fallback。手動入力でturnを継続できます。"
    ),
    CaptureStatusCode.STARTING: "capture起動中…",
    CaptureStatusCode.AVAILABLE: "fresh frameを受信しています。",
    CaptureStatusCode.DEVICE_UNAVAILABLE: (
        "UGREENデバイスが見つかりません — manual-safe fallback。"
    ),
    CaptureStatusCode.OPEN_FAILED: "capture映像を開始できません — manual-safe fallback。",
    CaptureStatusCode.FRAME_UNAVAILABLE: (
        "映像フレームがまだありません — manual-safe fallback。"
    ),
    CaptureStatusCode.FRAME_STALE: "最新フレームが古いためOCR候補は使用しません。",
    CaptureStatusCode.CAPTURE_ERROR: "capture映像でエラーが発生しました — manual-safe fallback。",
}

_CTA_LABELS = {
    "CREATE_NEW_MATCH": "NEW MATCH",
    "CONFIRM_SELECTION_FACTS": "自分と相手の6体を確認",
    "REQUEST_SELECTION_ADVICE": "MOCK Selection Adviceを投入",
    "WAIT_SELECTION_ADVICE": "Selection Adviceを待機",
    "APPLY_SELECTION": "実際の選出を確認してAPPLY",
    "START_TURN_CAPTURE": "START TURN CAPTURE",
    "CONFIRM_TURN_FACTS": "Turn factsを確認",
    "REQUEST_TURN_ADVICE": "MOCK Turn Adviceを要求",
    "WAIT_TURN_ADVICE": "Turn Adviceを待機",
    "RECORD_ACTUAL_ACTION": "実際の行動を記録",
    "NEXT_TURN": "NEXT TURN",
    "RESOLVE_DELIVERY_UNKNOWN": "provider結果不明を解決",
}

_MESSAGE_LABELS = {
    "NO_ACTIVE_MATCH": "対戦を開始してください。",
    "SELECTION_FACTS_REQUIRED": "自分と相手の6体を手動入力してください。",
    "SELECTION_FACTS_CONFIRMED": "確認済み6体を使ってMOCK Adviceを投入してください。",
    "SELECTION_ADVICE_PENDING": "MOCK Adviceを処理しています。",
    "SELECTION_ADVICE_READY": "実際に選んだ3体と先発を指定してください。",
    "BATTLE_READY": "人間がSTART TURN CAPTUREを押してください。自動開始はしません。",
    "TURN_FACTS_REQUIRED": "現在のTurn factsを手動入力し、確認してください。",
    "TURN_FACTS_REVIEWED": "確認済みfactsを使ってMOCK Turn Adviceを明示要求してください。",
    "TURN_ADVICE_PENDING": "MOCK Turn Adviceを処理しています。自動retryはしません。",
    "TURN_ADVICE_READY": "助言とは別に、人間が実際に選んだ合法行動を記録してください。",
    "TURN_RECORDED": "実際の行動を記録しました。人間がNEXT TURNを押してください。",
    "PROVIDER_DELIVERY_UNKNOWN": "前回のprovider結果が不明です。新しい送信は停止中です。",
}

_PLACEHOLDER = "選択してください"
_PREVIEW_DISPLAY_LABEL = "preview表示中"
_PREVIEW_UNAVAILABLE_LABEL = (
    "previewなし — drawable frameがありません。manual-safe fallback。"
)
_PREVIEW_STALE_LABEL = "previewなし — frameがstaleです。manual-safe fallback。"
_PREVIEW_INVALID_LABEL = "previewなし — frame imageがinvalidです。manual-safe fallback。"


class _AspectRatioPreviewLabel(QLabel):
    """A QLabel whose layout slot follows the Battle Record 16:9 preview."""

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return max(1, round(width * 9 / 16))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(640, 360)


class MapleMainWindow(QMainWindow):
    """Thin renderer over SelectionFlowController and DomainProjection."""

    def __init__(
        self,
        controller: SelectionFlowController,
        *,
        capture_backend: VideoCaptureBackend | None = None,
        ocr_backend: OcrCandidateBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._loaded_team: tuple[str, ...] = ()
        self._loaded_applied_three: tuple[str, ...] = ()
        self._loaded_turn_number: int | None = None
        self._loaded_turn_facts_id: str | None = None
        self._current_turn_facts: TurnFactsView | None = None

        # -- Lane B (issue #31): UGREEN capture + OCR candidates -----------------
        # capture_service/ocr_service are display/candidate-only: nothing here
        # ever writes to the repository, calls a domain command, or auto-adopts
        # a candidate into canonical turn facts. Manual entry always works,
        # capture present or not (CaptureStatus.manual_entry_allowed is
        # always True upstream).
        self._capture_service = CaptureService(capture_backend or QtMultimediaUgreenBackend())
        self._ocr_service = OcrCandidateService(ocr_backend or UnavailableOcrCandidateBackend())
        self._latest_ocr_bundle: OcrCandidateBundle | None = None
        self._capture_preview_pixmap = QPixmap()
        self._capture_preview_frame_id: str | None = None
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(_CAPTURE_POLL_INTERVAL_MS)
        self._capture_timer.timeout.connect(self._poll_capture_status)
        frame_ready = getattr(self._capture_service, "frame_ready", None)
        if frame_ready is not None:
            frame_ready.connect(self._on_frame_ready)

        self._build_ui()
        self.render_view()

        if auto_start_capture:
            self._start_capture()

    def _start_capture(self) -> None:
        """Human has opened the Battle Record screen; begin passive capture.

        Never raises and never blocks turn-fact entry: failures degrade to a
        sanitized status string, and ``manual_entry_allowed`` stays True.
        """

        self._capture_service.start()
        self._poll_capture_status()
        self._capture_timer.start()

    def _on_reconnect_capture(self, _checked: bool = False) -> None:
        """Perform exactly one human-requested capture reacquisition attempt."""

        self._capture_timer.stop()
        self._capture_service.stop()
        self._capture_service.start()
        self._poll_capture_status()
        self._capture_timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Clean shutdown: stop capture and release the device on app exit."""

        self._capture_timer.stop()
        self._capture_service.stop()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "capture_preview_label"):
            self._render_capture_preview()

    def _build_ui(self) -> None:
        self.setWindowTitle("Maple Next — Battle Record")
        self.resize(1180, 960)

        outer = QWidget(self)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(24, 24, 24, 24)
        outer_layout.setSpacing(16)

        heading = QLabel("今なにをすべきか")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.primary_cta_label = QLabel()
        self.primary_cta_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.guidance_label = QLabel()
        self.guidance_label.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "QLabel { color: #9d1c1c; background: #fff0f0; border: 1px solid #e2a4a4; "
            "padding: 10px; border-radius: 4px; }"
        )

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QFormLayout(status_frame)
        self.application_mode_label = QLabel()
        self.session_state_label = QLabel()
        self.provider_status_label = QLabel()
        self.battle_revision_label = QLabel()
        self.turn_number_label = QLabel()
        status_layout.addRow("Application mode", self.application_mode_label)
        status_layout.addRow("Session state", self.session_state_label)
        status_layout.addRow("Provider status", self.provider_status_label)
        status_layout.addRow("Battle revision", self.battle_revision_label)
        status_layout.addRow("Turn number", self.turn_number_label)

        self.new_match_button = QPushButton("NEW MATCH")
        self.new_match_button.clicked.connect(self._on_new_match)

        for widget in (
            heading,
            self.primary_cta_label,
            self.guidance_label,
            self.error_label,
            status_frame,
            self.new_match_button,
        ):
            outer_layout.addWidget(widget)

        self.header_tabs = QTabWidget()
        outer_layout.addWidget(self.header_tabs, 1)

        selection_scroll = QScrollArea()
        selection_scroll.setWidgetResizable(True)
        selection_page = QWidget()
        self._selection_layout = QVBoxLayout(selection_page)
        self._selection_layout.setContentsMargins(4, 4, 4, 4)
        self._selection_layout.setSpacing(16)
        selection_scroll.setWidget(selection_page)
        self.header_tabs.addTab(selection_scroll, "選出")

        battle_scroll = QScrollArea()
        battle_scroll.setWidgetResizable(True)
        battle_page = QWidget()
        self._root_layout = QVBoxLayout(battle_page)
        self._root_layout.setContentsMargins(4, 4, 4, 4)
        self._root_layout.setSpacing(16)
        battle_scroll.setWidget(battle_page)
        self.header_tabs.addTab(battle_scroll, "バトルレコード")

        self._build_selection_facts_group()
        self._build_mock_advice_group()
        self._build_gemini_send_group()
        self._build_advice_display_group()
        self._build_actual_selection_group()
        self._selection_layout.addStretch(1)

        self._build_battle_ready_group()
        self._build_battle_record_columns()
        self._root_layout.addStretch(1)

        self.setCentralWidget(outer)

    def _build_battle_record_columns(self) -> None:
        """左: 確定turn log / 中央: 映像・事実・行動入力 / 右: Gemini Turn Advice."""
        columns = QHBoxLayout()
        columns.setSpacing(16)

        left_widget = QWidget()
        self._left_column_layout = QVBoxLayout(left_widget)
        self._left_column_layout.setContentsMargins(0, 0, 0, 0)
        columns.addWidget(left_widget, 1)

        center_widget = QWidget()
        self._center_column_layout = QVBoxLayout(center_widget)
        self._center_column_layout.setContentsMargins(0, 0, 0, 0)
        columns.addWidget(center_widget, 2)

        right_widget = QWidget()
        self._right_column_layout = QVBoxLayout(right_widget)
        self._right_column_layout.setContentsMargins(0, 0, 0, 0)
        columns.addWidget(right_widget, 1)

        self._root_layout.addLayout(columns)

        self._build_action_history_group()

        self._build_capture_status_group()
        self._build_turn_facts_group()
        self._build_actual_action_group()

        self._build_mock_turn_advice_group()
        self._build_turn_advice_display_group()

        self._left_column_layout.addStretch(1)
        self._center_column_layout.addStretch(1)
        self._right_column_layout.addStretch(1)

    def _build_capture_status_group(self) -> None:
        self.capture_status_group = QGroupBox("UGREEN映像")
        layout = QVBoxLayout(self.capture_status_group)
        self.capture_preview_label = _AspectRatioPreviewLabel(_PREVIEW_UNAVAILABLE_LABEL)
        self.capture_preview_label.setObjectName("capturePreviewLabel")
        self.capture_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_preview_label.setMinimumSize(320, 180)
        self.capture_preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.capture_preview_label.setStyleSheet(
            "QLabel { background: #111827; color: #d1d5db; border: 1px solid #374151; "
            "padding: 12px; }"
        )
        layout.addWidget(self.capture_preview_label)

        self.reconnect_capture_button = QPushButton("UGREEN 再接続 / capture再開始")
        self.reconnect_capture_button.setObjectName("reconnectCaptureButton")
        self.reconnect_capture_button.clicked.connect(self._on_reconnect_capture)
        layout.addWidget(self.reconnect_capture_button)

        self.capture_status_label = QLabel(
            "capture未接続 — manual-safe fallback。手動入力でturnを継続できます。"
        )
        self.capture_status_label.setWordWrap(True)
        self.capture_freshness_label = QLabel("フレーム: —")
        self.capture_device_label = QLabel("デバイス: —")
        layout.addWidget(self.capture_status_label)
        layout.addWidget(self.capture_freshness_label)
        layout.addWidget(self.capture_device_label)
        self._center_column_layout.addWidget(self.capture_status_group)

        self.ocr_candidates_group = QGroupBox("OCR候補 — 参考情報のみ・自動採用しません")
        ocr_layout = QFormLayout(self.ocr_candidates_group)
        self.ocr_status_label = QLabel("—")
        self.ocr_status_label.setWordWrap(True)
        ocr_layout.addRow("状態", self.ocr_status_label)

        self._ocr_field_labels: dict[str, QLabel] = {}
        self._ocr_adopt_buttons: dict[str, QPushButton] = {}
        for field_key in (
            OcrFieldKey.SELF_ACTIVE,
            OcrFieldKey.OPPONENT_ACTIVE,
            OcrFieldKey.SELF_HP,
            OcrFieldKey.OPPONENT_HP,
        ):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            candidate_label = QLabel("—")
            candidate_label.setWordWrap(True)
            adopt_button = QPushButton("採用")
            adopt_button.setEnabled(False)
            adopt_button.clicked.connect(
                lambda _checked=False, key=field_key.value: self._on_adopt_ocr_candidate(key)
            )
            row_layout.addWidget(candidate_label, 1)
            row_layout.addWidget(adopt_button)
            self._ocr_field_labels[field_key.value] = candidate_label
            self._ocr_adopt_buttons[field_key.value] = adopt_button
            ocr_layout.addRow(field_key.value, row)
        self._center_column_layout.addWidget(self.ocr_candidates_group)

    def set_capture_status(self, *, available: bool, detail: str = "") -> None:
        """Sanitized capture-adapter status hook (no device I/O in this module)."""

        if available:
            text = detail or "fresh frameを受信しています。"
        else:
            text = detail or "capture未接続 — manual-safe fallback。手動入力でturnを継続できます。"
        self.capture_status_label.setText(text)

    # -- Lane B (issue #31): capture/OCR polling and human-only adoption --------

    def _poll_capture_status(self) -> None:
        """Timer-driven, display-only refresh. Never a Turn Advice trigger."""

        status, frame = self._capture_service.latest_snapshot()
        self._render_capture_status(status, frame)

        frame_for_ocr = frame if status.available else None
        context = OcrCandidateContext(
            self_active_candidates=self._loaded_applied_three,
            opponent_active_candidates=(),
        )
        bundle = self._ocr_service.request_candidates_from_capture_status(
            capture_status=status, frame=frame_for_ocr, context=context
        )
        self._latest_ocr_bundle = bundle
        self._render_ocr_candidates(bundle)

    def _on_frame_ready(self, _frame: object) -> None:
        """Render the callback's newest snapshot without waiting for the poll timer."""

        status, frame = self._capture_service.latest_snapshot()
        self._render_capture_status(status, frame)

    def _render_capture_status(
        self, status: CaptureStatus, frame: FramePacket | None = None
    ) -> None:
        preview_installed = self._render_capture_snapshot(status, frame)
        if preview_installed:
            detail = _PREVIEW_DISPLAY_LABEL
            available = True
        elif status.status == CaptureStatusCode.AVAILABLE:
            detail = (
                _PREVIEW_UNAVAILABLE_LABEL
                if frame is None
                else _PREVIEW_INVALID_LABEL
            )
            available = False
        else:
            detail = _CAPTURE_STATUS_LABELS.get(status.status, status.operator_message or "")
            available = status.available
        self.set_capture_status(available=available, detail=detail)
        self.capture_device_label.setText(f"デバイス: {status.device_label or '—'}")
        if status.age_ms is None:
            self.capture_freshness_label.setText("フレーム: —")
        else:
            freshness = "fresh" if status.fresh else "stale"
            self.capture_freshness_label.setText(f"フレーム: {status.age_ms}ms前 ({freshness})")

    @staticmethod
    def _detached_qimage(image: object) -> QImage | None:
        """Return a validated QImage detached from a producer-owned buffer."""

        if isinstance(image, QImage):
            detached = image.copy()
        elif isinstance(image, QPixmap):
            detached = image.toImage().copy()
        else:
            return None
        if detached.isNull() or detached.width() <= 0 or detached.height() <= 0:
            return None
        return detached

    def _clear_capture_preview(self, *, placeholder: str) -> None:
        self._capture_preview_pixmap = QPixmap()
        self._capture_preview_frame_id = None
        self.capture_preview_label.clear()
        self.capture_preview_label.setText(placeholder)

    def _render_capture_preview(self) -> bool:
        if self._capture_preview_pixmap.isNull():
            return False
        target_size = self.capture_preview_label.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            target_size = self.capture_preview_label.sizeHint()
        scaled = self._capture_preview_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).copy()
        if scaled.isNull():
            return False
        self.capture_preview_label.setText("")
        self.capture_preview_label.setPixmap(scaled)
        installed = self.capture_preview_label.pixmap()
        return installed is not None and not installed.isNull()

    def _render_capture_snapshot(
        self, status: CaptureStatus, frame: object | None
    ) -> bool:
        """Render pixels only when fresh metadata and the same frame are valid."""

        if not status.fresh or not isinstance(frame, FramePacket):
            placeholder = (
                _PREVIEW_STALE_LABEL
                if not status.fresh and status.status == CaptureStatusCode.FRAME_STALE
                else _PREVIEW_UNAVAILABLE_LABEL
            )
            self._clear_capture_preview(placeholder=placeholder)
            return False

        if status.frame_id != frame.frame_id:
            self._clear_capture_preview(placeholder=_PREVIEW_INVALID_LABEL)
            return False
        detached = self._detached_qimage(frame.image)
        if detached is None:
            self._clear_capture_preview(placeholder=_PREVIEW_INVALID_LABEL)
            return False
        pixmap = QPixmap.fromImage(detached).copy()
        if pixmap.isNull():
            self._clear_capture_preview(placeholder=_PREVIEW_INVALID_LABEL)
            return False
        self._capture_preview_pixmap = pixmap
        self._capture_preview_frame_id = frame.frame_id
        if not self._render_capture_preview():
            self._clear_capture_preview(placeholder=_PREVIEW_INVALID_LABEL)
            return False
        return True

    def _render_ocr_candidates(self, bundle: OcrCandidateBundle) -> None:
        self.ocr_status_label.setText(bundle.operator_message or bundle.status)
        best_by_field: dict[str, OcrCandidate] = {}
        for candidate in bundle.candidates:
            current = best_by_field.get(candidate.field_key)
            if current is None or candidate.rank < current.rank:
                best_by_field[candidate.field_key] = candidate

        editable = getattr(self, "_turn_facts_editable", False)
        for field_key, label in self._ocr_field_labels.items():
            best_candidate = best_by_field.get(field_key)
            button = self._ocr_adopt_buttons[field_key]
            if best_candidate is None:
                label.setText("—")
                button.setEnabled(False)
                continue
            label.setText(
                f"{best_candidate.suggested_value} (confidence={best_candidate.confidence:.2f})"
            )
            button.setEnabled(editable)

    def _on_adopt_ocr_candidate(self, field_key: str) -> None:
        """Human-triggered only: copy one OCR candidate into the manual input.

        Never called by the timer/poll path itself. The human must still
        press "Turn factsを確認／修正" to save anything — this only fills
        the input widget, exactly like typing the value by hand, and manual
        edits after adoption always win.
        """

        if self._latest_ocr_bundle is None:
            return
        candidates = [c for c in self._latest_ocr_bundle.candidates if c.field_key == field_key]
        if not candidates:
            return
        best = min(candidates, key=lambda c: c.rank)
        if field_key == OcrFieldKey.SELF_ACTIVE.value:
            self.self_active_box.setCurrentText(best.suggested_value)
        elif field_key == OcrFieldKey.OPPONENT_ACTIVE.value:
            self.opponent_active_input.setText(best.suggested_value)
        elif field_key == OcrFieldKey.SELF_HP.value:
            self.self_hp_box.setCurrentText(best.suggested_value)
        elif field_key == OcrFieldKey.OPPONENT_HP.value:
            self.opponent_hp_box.setCurrentText(best.suggested_value)

    def _build_selection_facts_group(self) -> None:
        self.selection_facts_group = QGroupBox("Selection facts — 手動入力")
        layout = QGridLayout(self.selection_facts_group)
        layout.addWidget(QLabel("自分の6体"), 0, 0)
        layout.addWidget(QLabel("相手の6体"), 0, 1)
        self.self_team_inputs: list[QLineEdit] = []
        self.opponent_team_inputs: list[QLineEdit] = []
        for index in range(6):
            self_input = QLineEdit()
            self_input.setPlaceholderText(f"自分 {index + 1}体目")
            opponent_input = QLineEdit()
            opponent_input.setPlaceholderText(f"相手 {index + 1}体目")
            self.self_team_inputs.append(self_input)
            self.opponent_team_inputs.append(opponent_input)
            layout.addWidget(self_input, index + 1, 0)
            layout.addWidget(opponent_input, index + 1, 1)
        self.confirm_facts_button = QPushButton("6体を確認")
        self.confirm_facts_button.clicked.connect(self._on_confirm_facts)
        layout.addWidget(self.confirm_facts_button, 7, 0, 1, 2)
        self._selection_layout.addWidget(self.selection_facts_group)

    def _build_mock_advice_group(self) -> None:
        self.mock_group = QGroupBox("開発用 MOCK Selection Advice — ネットワーク送信なし")
        layout = QFormLayout(self.mock_group)
        self.mock_selection_boxes: list[QComboBox] = []
        for index in range(3):
            box = QComboBox()
            box.currentTextChanged.connect(self._update_mock_lead_options)
            self.mock_selection_boxes.append(box)
            layout.addRow(f"提案 {index + 1}体目", box)
        self.mock_lead_box = QComboBox()
        layout.addRow("提案先発", self.mock_lead_box)
        self.mock_submit_button = QPushButton("MOCK Adviceを投入")
        self.mock_submit_button.clicked.connect(self._on_submit_mock)
        layout.addRow(self.mock_submit_button)
        self._selection_layout.addWidget(self.mock_group)

    def _build_gemini_send_group(self) -> None:
        self.gemini_group = QGroupBox("Gemini Selection Advice — 人間による明示送信")
        layout = QVBoxLayout(self.gemini_group)
        helper = QLabel(
            "確認済みの自分6体・相手6体をそのままGeminiへ送信します。"
            "自動送信・自動retryはありません。"
        )
        helper.setWordWrap(True)
        layout.addWidget(helper)
        self.gemini_send_button = TrustedSendButton(
            "SEND SELECTION TO GEMINI", self._on_trusted_send_to_gemini
        )
        layout.addWidget(self.gemini_send_button)
        self._selection_layout.addWidget(self.gemini_group)

    def _build_advice_display_group(self) -> None:
        self.advice_group = QGroupBox("受領したSelection Advice")
        layout = QFormLayout(self.advice_group)
        self.advice_source_label = QLabel()
        self.advice_three_label = QLabel()
        self.advice_lead_label = QLabel()
        layout.addRow("提供元", self.advice_source_label)
        layout.addRow("提案3体", self.advice_three_label)
        layout.addRow("提案先発", self.advice_lead_label)
        self._selection_layout.addWidget(self.advice_group)

    def _build_actual_selection_group(self) -> None:
        self.actual_group = QGroupBox("実際の選出 — 人間が決定")
        layout = QVBoxLayout(self.actual_group)
        helper = QLabel("MOCK提案と異なる合法な3体・先発を選べます。自動コピーはしません。")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        grid = QGridLayout()
        self.actual_checkboxes: list[QCheckBox] = []
        for index in range(6):
            checkbox = QCheckBox()
            checkbox.toggled.connect(self._update_actual_controls)
            self.actual_checkboxes.append(checkbox)
            grid.addWidget(checkbox, index // 2, index % 2)
        layout.addLayout(grid)
        lead_row = QHBoxLayout()
        lead_row.addWidget(QLabel("実際の先発"))
        self.actual_lead_box = QComboBox()
        self.actual_lead_box.currentTextChanged.connect(self._update_actual_controls)
        lead_row.addWidget(self.actual_lead_box, 1)
        layout.addLayout(lead_row)
        self.apply_confirm_checkbox = QCheckBox("この3体と先発を実際の選出としてAPPLYします")
        self.apply_confirm_checkbox.toggled.connect(self._update_actual_controls)
        layout.addWidget(self.apply_confirm_checkbox)
        self.apply_button = TrustedSendButton("APPLY", self._on_apply)
        layout.addWidget(self.apply_button)
        self._selection_layout.addWidget(self.actual_group)

    def _build_battle_ready_group(self) -> None:
        self.ready_group = QGroupBox("APPLY済みSelection")
        layout = QFormLayout(self.ready_group)
        self.actual_three_label = QLabel()
        self.actual_lead_label = QLabel()
        self.actual_backline_label = QLabel()
        layout.addRow("実際の3体", self.actual_three_label)
        layout.addRow("実際の先発", self.actual_lead_label)
        layout.addRow("控え", self.actual_backline_label)
        self.start_turn_button = QPushButton("START TURN CAPTURE")
        self.start_turn_button.clicked.connect(self._on_start_turn)
        layout.addRow(self.start_turn_button)
        self._root_layout.addWidget(self.ready_group)

    def _build_turn_facts_group(self) -> None:
        self.turn_facts_group = QGroupBox("Turn facts — 人間が確認・修正")
        layout = QFormLayout(self.turn_facts_group)
        self.self_active_box = QComboBox()
        self.opponent_active_input = QLineEdit()
        self.opponent_active_input.setPlaceholderText("相手のactive")
        self.self_hp_box = QComboBox()
        self.opponent_hp_box = QComboBox()
        for box in (self.self_hp_box, self.opponent_hp_box):
            box.addItem(_PLACEHOLDER)
            box.addItems([bucket.value for bucket in HpBucket])
        layout.addRow("自分のactive", self.self_active_box)
        layout.addRow("相手のactive", self.opponent_active_input)
        layout.addRow("自分のHP bucket", self.self_hp_box)
        layout.addRow("相手のHP bucket", self.opponent_hp_box)

        self.move_inputs: list[QLineEdit] = []
        for index in range(4):
            field = QLineEdit()
            field.setPlaceholderText(f"合法move {index + 1}")
            self.move_inputs.append(field)
            layout.addRow(f"合法move {index + 1}", field)

        switch_widget = QWidget()
        switch_layout = QHBoxLayout(switch_widget)
        switch_layout.setContentsMargins(0, 0, 0, 0)
        self.switch_checkboxes: list[QCheckBox] = []
        for _index in range(3):
            checkbox = QCheckBox()
            self.switch_checkboxes.append(checkbox)
            switch_layout.addWidget(checkbox)
        layout.addRow("合法switch候補", switch_widget)

        self.turn_note_input = QLineEdit()
        self.turn_note_input.setPlaceholderText("任意の人間確認メモ")
        layout.addRow("確認メモ", self.turn_note_input)
        self.turn_facts_confirm_checkbox = QCheckBox("このTurn factsを確認して保存します")
        self.turn_facts_confirm_checkbox.toggled.connect(self._update_turn_fact_button)
        layout.addRow(self.turn_facts_confirm_checkbox)
        self.confirm_turn_facts_button = QPushButton("Turn factsを確認／修正")
        self.confirm_turn_facts_button.clicked.connect(self._on_confirm_turn_facts)
        layout.addRow(self.confirm_turn_facts_button)
        self._center_column_layout.addWidget(self.turn_facts_group)

    def _build_mock_turn_advice_group(self) -> None:
        self.mock_turn_group = QGroupBox("開発用 MOCK Turn Advice — ネットワーク送信なし")
        layout = QFormLayout(self.mock_turn_group)
        self.mock_turn_action_type_box = QComboBox()
        self.mock_turn_action_type_box.addItems(
            [_PLACEHOLDER, ActionType.MOVE.value, ActionType.SWITCH.value]
        )
        self.mock_turn_action_type_box.currentTextChanged.connect(
            self._update_mock_turn_action_options
        )
        self.mock_turn_action_name_box = QComboBox()
        self.mock_turn_prediction_input = QLineEdit()
        self.mock_turn_prediction_input.setPlaceholderText("相手の次行動予測")
        self.mock_turn_rationale_input = QLineEdit()
        self.mock_turn_rationale_input.setPlaceholderText("簡潔な理由")
        self.mock_turn_warnings_input = QLineEdit()
        self.mock_turn_warnings_input.setPlaceholderText("任意の警告（; 区切り）")
        layout.addRow("推奨action種別", self.mock_turn_action_type_box)
        layout.addRow("推奨action", self.mock_turn_action_name_box)
        layout.addRow("相手の次行動予測", self.mock_turn_prediction_input)
        layout.addRow("理由", self.mock_turn_rationale_input)
        layout.addRow("警告", self.mock_turn_warnings_input)
        self.mock_turn_submit_button = QPushButton("MOCK Turn Adviceを投入")
        self.mock_turn_submit_button.clicked.connect(self._on_submit_mock_turn)
        layout.addRow(self.mock_turn_submit_button)
        self._right_column_layout.addWidget(self.mock_turn_group)

    def _build_turn_advice_display_group(self) -> None:
        self.turn_advice_group = QGroupBox("受領したTurn Advice")
        layout = QFormLayout(self.turn_advice_group)
        self.turn_advice_action_label = QLabel()
        self.turn_advice_prediction_label = QLabel()
        self.turn_advice_prediction_label.setWordWrap(True)
        self.turn_advice_rationale_label = QLabel()
        self.turn_advice_rationale_label.setWordWrap(True)
        self.turn_advice_warnings_label = QLabel()
        self.turn_advice_warnings_label.setWordWrap(True)
        self.turn_advice_source_label = QLabel()
        self.turn_advice_model_label = QLabel()
        self.turn_advice_binding_label = QLabel()
        self.turn_advice_legality_label = QLabel()
        layout.addRow("推奨action", self.turn_advice_action_label)
        layout.addRow("相手予測", self.turn_advice_prediction_label)
        layout.addRow("理由", self.turn_advice_rationale_label)
        layout.addRow("警告", self.turn_advice_warnings_label)
        layout.addRow("Source", self.turn_advice_source_label)
        layout.addRow("Model", self.turn_advice_model_label)
        layout.addRow("Binding", self.turn_advice_binding_label)
        layout.addRow("Legality", self.turn_advice_legality_label)
        self._right_column_layout.addWidget(self.turn_advice_group)

    def _build_actual_action_group(self) -> None:
        self.actual_action_group = QGroupBox("実際の行動 — 人間が記録")
        layout = QFormLayout(self.actual_action_group)
        note = QLabel("MOCK助言と異なる合法行動を選べます。助言を自動コピーしません。")
        note.setWordWrap(True)
        layout.addRow(note)
        self.actual_action_type_box = QComboBox()
        self.actual_action_type_box.addItems(
            [_PLACEHOLDER, ActionType.MOVE.value, ActionType.SWITCH.value]
        )
        self.actual_action_type_box.currentTextChanged.connect(
            self._update_actual_action_options
        )
        self.actual_action_name_box = QComboBox()
        self.actual_action_confirm_checkbox = QCheckBox("この行動を実際の操作として記録します")
        self.actual_action_confirm_checkbox.toggled.connect(
            self._update_actual_action_button
        )
        self.actual_action_name_box.currentTextChanged.connect(
            self._update_actual_action_button
        )
        layout.addRow("行動種別", self.actual_action_type_box)
        layout.addRow("実際の行動", self.actual_action_name_box)

        self.opponent_action_type_box = QComboBox()
        self.opponent_action_type_box.addItems(
            [_PLACEHOLDER, ActionType.MOVE.value, ActionType.SWITCH.value]
        )
        self.opponent_action_name_input = QLineEdit()
        self.opponent_action_name_input.setPlaceholderText("相手の実際の行動（任意）")
        self.action_order_box = QComboBox()
        self.action_order_box.addItems([order.value for order in ActionOrder])
        layout.addRow("相手の行動種別（任意）", self.opponent_action_type_box)
        layout.addRow("相手の実際の行動（任意）", self.opponent_action_name_input)
        layout.addRow("行動順序", self.action_order_box)

        layout.addRow(self.actual_action_confirm_checkbox)
        self.record_action_button = QPushButton("実際の行動を記録")
        self.record_action_button.clicked.connect(self._on_record_action)
        layout.addRow(self.record_action_button)
        self._center_column_layout.addWidget(self.actual_action_group)

    def _build_action_history_group(self) -> None:
        self.history_group = QGroupBox("Action-only history")
        layout = QVBoxLayout(self.history_group)
        self.action_history_label = QLabel()
        self.action_history_label.setWordWrap(True)
        layout.addWidget(self.action_history_label)
        self.next_turn_button = QPushButton("NEXT TURN")
        self.next_turn_button.clicked.connect(self._on_next_turn)
        layout.addWidget(self.next_turn_button)
        self._left_column_layout.addWidget(self.history_group)

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._controller.refresh()
        projection = current.projection
        self.application_mode_label.setText(projection.application_mode)
        self.session_state_label.setText(projection.session_state or "—")
        self.provider_status_label.setText(projection.provider_status)
        revision = "—" if projection.battle_revision is None else str(projection.battle_revision)
        self.battle_revision_label.setText(revision)
        turn_number = "—" if projection.turn_number is None else str(projection.turn_number)
        self.turn_number_label.setText(turn_number)
        self.primary_cta_label.setText(
            _CTA_LABELS.get(projection.primary_cta, projection.primary_cta)
        )
        self.guidance_label.setText(_MESSAGE_LABELS.get(projection.message, projection.message))
        self.error_label.setText(current.error_message or "")
        self.error_label.setVisible(current.error_message is not None)

        self.new_match_button.setVisible(projection.primary_cta == "CREATE_NEW_MATCH")
        self.new_match_button.setEnabled(projection.primary_cta_enabled)
        selection_open = projection.session_state == "SELECTION_OPEN"
        self.selection_facts_group.setVisible(selection_open)

        battle_record_states = {
            "BATTLE_READY",
            "TURN_CAPTURE_PENDING",
            "TURN_REVIEW_REQUIRED",
            "TURN_REVIEWED",
            "TURN_RECORDED",
            "MATCH_ENDED",
            "MATCH_EXPORTED",
        }
        if projection.session_state in battle_record_states:
            self.header_tabs.setCurrentIndex(1)
        elif projection.session_state in {
            "SELECTION_OPEN",
            "SELECTION_ADVICE_READY",
            None,
        }:
            self.header_tabs.setCurrentIndex(0)
        facts_editable = projection.primary_cta == "CONFIRM_SELECTION_FACTS"
        self.confirm_facts_button.setEnabled(facts_editable)
        for field in (*self.self_team_inputs, *self.opponent_team_inputs):
            field.setEnabled(facts_editable)

        if current.self_team and current.self_team != self._loaded_team:
            self._loaded_team = current.self_team
            self._populate_team_controls(current.self_team)
            for field, value in zip(self.self_team_inputs, current.self_team, strict=True):
                field.setText(value)
            for field, value in zip(
                self.opponent_team_inputs, current.opponent_team, strict=True
            ):
                field.setText(value)

        self.mock_group.setVisible(projection.primary_cta == "REQUEST_SELECTION_ADVICE")
        self.gemini_group.setVisible(
            self._controller.gemini_send_available
            and projection.primary_cta == "REQUEST_SELECTION_ADVICE"
        )
        self.gemini_send_button.setEnabled(
            projection.primary_cta == "REQUEST_SELECTION_ADVICE"
            and projection.provider_send_enabled
            and not self._controller.gemini_selection_attempt_consumed()
        )
        self.advice_group.setVisible(projection.current_selection_advice_id is not None)
        if current.advice is not None:
            self.advice_source_label.setText(current.advice.source_type)
            self.advice_three_label.setText(" / ".join(current.advice.selected_three))
            self.advice_lead_label.setText(current.advice.lead)
        self.actual_group.setVisible(projection.primary_cta == "APPLY_SELECTION")

        self.ready_group.setVisible(current.applied_selection is not None)
        self.start_turn_button.setVisible(projection.primary_cta == "START_TURN_CAPTURE")
        self.start_turn_button.setEnabled(projection.primary_cta_enabled)
        if current.applied_selection is not None:
            self.actual_three_label.setText(" / ".join(current.applied_selection.selected_three))
            self.actual_lead_label.setText(current.applied_selection.lead)
            self.actual_backline_label.setText(" / ".join(current.applied_selection.backline))
            if current.applied_selection.selected_three != self._loaded_applied_three:
                self._loaded_applied_three = current.applied_selection.selected_three
                self._populate_turn_selection_controls(
                    current.applied_selection.selected_three
                )

        turn_state = projection.session_state in {
            "TURN_CAPTURE_PENDING",
            "TURN_REVIEWED",
            "TURN_RECORDED",
        }
        self.turn_facts_group.setVisible(turn_state)
        turn_editable = projection.session_state in {
            "TURN_CAPTURE_PENDING",
            "TURN_REVIEWED",
        }
        self._set_turn_facts_editable(turn_editable)

        if projection.turn_number != self._loaded_turn_number:
            self._loaded_turn_number = projection.turn_number
            self._loaded_turn_facts_id = None
            if current.turn_facts is None:
                self._clear_turn_inputs()
        if (
            current.turn_facts is not None
            and current.turn_facts.snapshot_id != self._loaded_turn_facts_id
        ):
            self._loaded_turn_facts_id = current.turn_facts.snapshot_id
            self._load_turn_facts(current.turn_facts)
        self._current_turn_facts = current.turn_facts

        self.mock_turn_group.setVisible(projection.primary_cta == "REQUEST_TURN_ADVICE")
        self.turn_advice_group.setVisible(current.turn_advice is not None)
        if current.turn_advice is not None:
            self.turn_advice_action_label.setText(
                f"{current.turn_advice.action_type}: {current.turn_advice.action_name}"
            )
            self.turn_advice_prediction_label.setText(
                current.turn_advice.opponent_prediction
            )
            self.turn_advice_rationale_label.setText(current.turn_advice.rationale)
            self.turn_advice_warnings_label.setText(
                "; ".join(current.turn_advice.warnings) or "—"
            )
            self.turn_advice_source_label.setText(current.turn_advice.source_type)
            self.turn_advice_model_label.setText(current.turn_advice.model)
            self.turn_advice_binding_label.setText("CURRENT")
            self.turn_advice_legality_label.setText("VALID")

        self.actual_action_group.setVisible(
            projection.primary_cta == "RECORD_ACTUAL_ACTION"
        )
        if projection.primary_cta == "RECORD_ACTUAL_ACTION":
            self.actual_action_type_box.setCurrentIndex(0)
            self.actual_action_name_box.clear()
            self.actual_action_name_box.addItem(_PLACEHOLDER)
            self.actual_action_confirm_checkbox.setChecked(False)
            self.opponent_action_type_box.setCurrentIndex(0)
            self.opponent_action_name_input.clear()
            self.action_order_box.setCurrentText(ActionOrder.UNKNOWN.value)

        history_lines = [
            f"Turn {item.turn_number}: 自分={item.action_type} {item.action_name}"
            + (
                f" / 相手={item.opponent_action_type} {item.opponent_action_name}"
                if item.opponent_action_type is not None
                else ""
            )
            + f" / 順序={item.action_order}"
            for item in current.action_history
        ]
        self.action_history_label.setText(
            "\n".join(history_lines) or "記録済みactionはありません。"
        )
        self.history_group.setVisible(bool(current.action_history) or turn_state)
        self.next_turn_button.setVisible(projection.primary_cta == "NEXT_TURN")
        self.next_turn_button.setEnabled(projection.primary_cta_enabled)

        self._update_actual_controls()
        self._update_turn_fact_button()
        self._update_mock_turn_action_options()
        self._update_actual_action_button()

    def _populate_team_controls(self, team: Sequence[str]) -> None:
        for box in self.mock_selection_boxes:
            box.blockSignals(True)
            box.clear()
            box.addItem(_PLACEHOLDER)
            box.addItems(list(team))
            box.blockSignals(False)
        self.mock_lead_box.clear()
        self.mock_lead_box.addItem(_PLACEHOLDER)
        for checkbox, name in zip(self.actual_checkboxes, team, strict=True):
            checkbox.setText(name)
            checkbox.setChecked(False)
            checkbox.setVisible(True)
        self.actual_lead_box.clear()
        self.actual_lead_box.addItem(_PLACEHOLDER)
        self.apply_confirm_checkbox.setChecked(False)
        self._update_mock_lead_options()

    def _populate_turn_selection_controls(self, selected_three: Sequence[str]) -> None:
        self.self_active_box.clear()
        self.self_active_box.addItem(_PLACEHOLDER)
        self.self_active_box.addItems(list(selected_three))
        for checkbox, name in zip(self.switch_checkboxes, selected_three, strict=True):
            checkbox.setText(name)
            checkbox.setChecked(False)
            checkbox.setVisible(True)

    def _clear_turn_inputs(self) -> None:
        self.self_active_box.setCurrentIndex(0)
        self.opponent_active_input.clear()
        self.self_hp_box.setCurrentIndex(0)
        self.opponent_hp_box.setCurrentIndex(0)
        for field in self.move_inputs:
            field.clear()
        for checkbox in self.switch_checkboxes:
            checkbox.setChecked(False)
        self.turn_note_input.clear()
        self.turn_facts_confirm_checkbox.setChecked(False)
        self.mock_turn_action_type_box.setCurrentIndex(0)
        self.mock_turn_prediction_input.clear()
        self.mock_turn_rationale_input.clear()
        self.mock_turn_warnings_input.clear()

    def _load_turn_facts(self, facts: TurnFactsView) -> None:
        self.self_active_box.setCurrentText(facts.self_active)
        self.opponent_active_input.setText(facts.opponent_active)
        self.self_hp_box.setCurrentText(facts.self_hp)
        self.opponent_hp_box.setCurrentText(facts.opponent_hp)
        for index, field in enumerate(self.move_inputs):
            field.setText(facts.legal_moves[index] if index < len(facts.legal_moves) else "")
        for checkbox in self.switch_checkboxes:
            checkbox.setChecked(checkbox.text() in facts.legal_switches)
        self.turn_note_input.setText(facts.human_note)
        self.turn_facts_confirm_checkbox.setChecked(False)

    def _set_turn_facts_editable(self, enabled: bool) -> None:
        self._turn_facts_editable = enabled
        for widget in (
            self.self_active_box,
            self.opponent_active_input,
            self.self_hp_box,
            self.opponent_hp_box,
            *self.move_inputs,
            *self.switch_checkboxes,
            self.turn_note_input,
            self.turn_facts_confirm_checkbox,
        ):
            widget.setEnabled(enabled)
        self.confirm_turn_facts_button.setVisible(enabled)

    def _update_mock_lead_options(self, _text: str = "") -> None:
        selected = self._selected_combo_values(self.mock_selection_boxes)
        previous = self.mock_lead_box.currentText()
        self.mock_lead_box.blockSignals(True)
        self.mock_lead_box.clear()
        self.mock_lead_box.addItem(_PLACEHOLDER)
        for name in selected:
            if name not in {"", _PLACEHOLDER} and name not in self._combo_items(
                self.mock_lead_box
            ):
                self.mock_lead_box.addItem(name)
        if previous in self._combo_items(self.mock_lead_box):
            self.mock_lead_box.setCurrentText(previous)
        self.mock_lead_box.blockSignals(False)

    def _update_actual_controls(self, _value: object = None) -> None:
        selected = self._checked_actual_names()
        previous = self.actual_lead_box.currentText()
        self.actual_lead_box.blockSignals(True)
        self.actual_lead_box.clear()
        self.actual_lead_box.addItem(_PLACEHOLDER)
        self.actual_lead_box.addItems(selected)
        if previous in selected:
            self.actual_lead_box.setCurrentText(previous)
        self.actual_lead_box.blockSignals(False)
        lead = self.actual_lead_box.currentText()
        enabled = (
            len(selected) == 3
            and lead in selected
            and self.apply_confirm_checkbox.isChecked()
        )
        self.apply_button.setEnabled(enabled)

    def _update_turn_fact_button(self, _checked: bool = False) -> None:
        self.confirm_turn_facts_button.setEnabled(
            self.turn_facts_confirm_checkbox.isEnabled()
            and self.turn_facts_confirm_checkbox.isChecked()
        )

    def _turn_action_names(self, action_type: str) -> tuple[str, ...]:
        if self._current_turn_facts is None:
            return ()
        if action_type == ActionType.MOVE.value:
            return self._current_turn_facts.legal_moves
        if action_type == ActionType.SWITCH.value:
            return self._current_turn_facts.legal_switches
        return ()

    def _update_mock_turn_action_options(self, _text: str = "") -> None:
        previous = self.mock_turn_action_name_box.currentText()
        names = self._turn_action_names(self.mock_turn_action_type_box.currentText())
        self.mock_turn_action_name_box.clear()
        self.mock_turn_action_name_box.addItem(_PLACEHOLDER)
        self.mock_turn_action_name_box.addItems(list(names))
        if previous in names:
            self.mock_turn_action_name_box.setCurrentText(previous)

    def _update_actual_action_options(self, _text: str = "") -> None:
        names = self._turn_action_names(self.actual_action_type_box.currentText())
        self.actual_action_name_box.clear()
        self.actual_action_name_box.addItem(_PLACEHOLDER)
        self.actual_action_name_box.addItems(list(names))
        self._update_actual_action_button()

    def _update_actual_action_button(self, _value: object = None) -> None:
        self.record_action_button.setEnabled(
            self.actual_action_confirm_checkbox.isChecked()
            and self.actual_action_name_box.currentText() != _PLACEHOLDER
        )

    def _on_new_match(self, _checked: bool = False) -> None:
        self.render_view(self._controller.new_match())

    def _on_confirm_facts(self, _checked: bool = False) -> None:
        self_entries = [field.text() for field in self.self_team_inputs]
        opponent_entries = [field.text() for field in self.opponent_team_inputs]
        view = self._controller.confirm_selection_facts(self_entries, opponent_entries)
        self.render_view(view)

    def _on_submit_mock(self, _checked: bool = False) -> None:
        selected = self._selected_combo_values(self.mock_selection_boxes)
        view = self._controller.submit_mock_advice(
            selected, self.mock_lead_box.currentText()
        )
        self.render_view(view)

    def _on_trusted_send_to_gemini(self) -> None:
        view = self._controller.send_selection_advice_to_gemini(on_result=self.render_view)
        self.render_view(view)

    def _on_apply(self, _checked: bool = False) -> None:
        view = self._controller.apply_selection(
            self._checked_actual_names(),
            self.actual_lead_box.currentText(),
            human_confirmed=self.apply_confirm_checkbox.isChecked(),
        )
        self.render_view(view)

    def _on_start_turn(self, _checked: bool = False) -> None:
        self.render_view(self._controller.start_turn_capture())

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        switches = [
            checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()
        ]
        view = self._controller.confirm_turn_facts(
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

    def _on_submit_mock_turn(self, _checked: bool = False) -> None:
        warnings = tuple(
            part.strip()
            for part in self.mock_turn_warnings_input.text().split(";")
            if part.strip()
        )
        view = self._controller.submit_mock_turn_advice(
            action_type=self.mock_turn_action_type_box.currentText(),
            action_name=self.mock_turn_action_name_box.currentText(),
            opponent_prediction=self.mock_turn_prediction_input.text(),
            rationale=self.mock_turn_rationale_input.text(),
            warnings=warnings,
        )
        self.render_view(view)

    def _on_record_action(self, _checked: bool = False) -> None:
        opponent_type = self.opponent_action_type_box.currentText()
        if opponent_type == _PLACEHOLDER:
            opponent_type = ""
        view = self._controller.record_actual_action(
            action_type=self.actual_action_type_box.currentText(),
            action_name=self.actual_action_name_box.currentText(),
            human_confirmed=self.actual_action_confirm_checkbox.isChecked(),
            opponent_action_type=opponent_type,
            opponent_action_name=self.opponent_action_name_input.text(),
            action_order=self.action_order_box.currentText(),
        )
        self.render_view(view)

    def _on_next_turn(self, _checked: bool = False) -> None:
        self.render_view(self._controller.next_turn())

    def _checked_actual_names(self) -> list[str]:
        return [checkbox.text() for checkbox in self.actual_checkboxes if checkbox.isChecked()]

    @staticmethod
    def _selected_combo_values(boxes: Sequence[QComboBox]) -> list[str]:
        return [box.currentText() for box in boxes]

    @staticmethod
    def _combo_items(box: QComboBox) -> set[str]:
        return {box.itemText(index) for index in range(box.count())}
