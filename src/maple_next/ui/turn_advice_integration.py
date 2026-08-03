"""Issue #31 app integration for human-triggered Gemini Turn Advice.

Mirrors :mod:`maple_next.ui.selection_advice_integration`. Adds a
human-only ``TrustedSendButton`` that dispatches exactly one Turn Advice
job through :class:`maple_next.ui.gemini_turn_advice.GeminiTurnAdviceAdapter`.
The existing strict identity/hash/staleness/legality binding contract in
``apply_turn_advice_result`` is reused unchanged for production and fake paths.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel, QVBoxLayout

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import ActionType, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import SanitizedProviderResult
from maple_next.providers.turn_transport import FAKE_TURN_ADVICE_SOURCE_TYPE
from maple_next.ui.controller import OperatorView
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.gemini_turn_advice import (
    FAKE_TURN_MODEL,
    GeminiTurnAdviceAdapter,
    describe_gemini_turn_failure,
)
from maple_next.ui.selection_advice_integration import (
    SelectionAdviceIntegrationController,
    SelectionAdviceIntegrationWindow,
)
from maple_next.ui.trusted_input import TrustedSendButton


@dataclass(frozen=True, slots=True)
class TurnAdviceGeminiStatusView:
    """Sanitized UI-only state for one human-triggered fake/injected send."""

    status: str
    source_type: str
    model: str
    sanitized_failure: str


class TurnAdviceIntegrationController(SelectionAdviceIntegrationController):
    """Adds explicit human-triggered Gemini Turn Advice."""

    def __init__(
        self,
        application: BattleApplication,
        repository: SQLiteRepository,
        mock_adapter: MockSelectionAdviceAdapter,
        mock_turn_adapter: MockTurnAdviceAdapter | None = None,
        gemini_adapter: GeminiSelectionAdviceAdapter | None = None,
        turn_gemini_adapter: GeminiTurnAdviceAdapter | None = None,
    ) -> None:
        super().__init__(application, repository, mock_adapter, mock_turn_adapter, gemini_adapter)
        self._turn_gemini_adapter = turn_gemini_adapter

    @property
    def turn_gemini_send_available(self) -> bool:
        return self._turn_gemini_adapter is not None

    @property
    def turn_gemini_uses_injected_transport(self) -> bool:
        adapter = self._turn_gemini_adapter
        return adapter is not None and adapter.uses_injected_transport

    def turn_advice_gemini_status(self) -> TurnAdviceGeminiStatusView:
        adapter = self._turn_gemini_adapter
        if adapter is None:
            return TurnAdviceGeminiStatusView(
                status="UNAVAILABLE", source_type="—", model="—", sanitized_failure=""
            )
        if adapter.in_flight:
            return TurnAdviceGeminiStatusView(
                status="PENDING",
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="",
            )
        if adapter.last_failure_reason is not None:
            return TurnAdviceGeminiStatusView(
                status="FAILED",
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure=adapter.last_failure_reason,
            )
        if adapter.last_disposition is ResultDisposition.APPLIED:
            return TurnAdviceGeminiStatusView(
                status="SUCCESS",
                source_type=adapter.last_source_type or FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="",
            )
        if adapter.last_disposition is not None:
            return TurnAdviceGeminiStatusView(
                status="STALE_OR_INVALID",
                source_type=adapter.last_source_type or FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="GEMINI_TURN_RESULT_STALE",
            )
        return TurnAdviceGeminiStatusView(
            status="IDLE", source_type=FAKE_TURN_ADVICE_SOURCE_TYPE, model="—", sanitized_failure=""
        )

    def send_turn_advice_to_gemini(
        self,
        *,
        action_type: str,
        action_name: str,
        opponent_prediction: str,
        rationale: str,
        warnings: tuple[str, ...],
        on_result: Callable[[OperatorView], None],
    ) -> OperatorView:
        """Human-only explicit send using a fake/injected transport.

        The human supplies the exact advice content (mirrors the MOCK Turn
        Advice fields) which is queued into the adapter's fake transport and
        dispatched exactly once off the UI thread through the same
        identity/hash/staleness/legality binding contract every other Turn
        Advice result must pass.
        """

        current = self.refresh()
        adapter = self._turn_gemini_adapter
        if adapter is None:
            self._error_message = "Gemini Turn Advice adapterが設定されていません。"
            return self.refresh()
        if current.projection.primary_cta != "REQUEST_TURN_ADVICE":
            self._error_message = "確認済みTurn factsが必要です。"
            return self.refresh()
        if not adapter.uses_injected_transport:
            return self._dispatch_turn_advice_to_gemini(adapter, on_result)
        try:
            typed_action = ActionType(action_type.strip())
        except ValueError:
            self._error_message = "行動種別はMOVEまたはSWITCHを選択してください。"
            return self.refresh()
        normalized_name = action_name.strip()
        normalized_prediction = opponent_prediction.strip()
        normalized_rationale = rationale.strip()
        if current.turn_facts is None:
            self._error_message = "確認済みTurn factsが必要です。"
            return self.refresh()
        legal_actions = (
            current.turn_facts.legal_moves
            if typed_action is ActionType.MOVE
            else current.turn_facts.legal_switches
        )
        if normalized_name not in legal_actions:
            self._error_message = "推奨actionは確認済み合法行動から選んでください。"
            return self.refresh()
        if not normalized_prediction or not normalized_rationale:
            self._error_message = "相手の次行動予測と理由を入力してください。"
            return self.refresh()

        action_id = f"{typed_action.value}:{normalized_name}"
        adapter.enqueue_response(
            SanitizedProviderResult(
                payload={
                    "recommended_action": {
                        "action_id": action_id,
                        "action_type": typed_action.value,
                        "action_name": normalized_name,
                    },
                    "reasons": [normalized_rationale],
                    "warnings": list(warnings),
                    "opponent_prediction": {
                        "category": "UNKNOWN",
                        "predicted_action": normalized_prediction,
                        "summary": normalized_prediction,
                        "confidence": 0.5,
                    },
                },
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=FAKE_TURN_MODEL,
            )
        )

        return self._dispatch_turn_advice_to_gemini(adapter, on_result)

    def _dispatch_turn_advice_to_gemini(
        self,
        adapter: GeminiTurnAdviceAdapter,
        on_result: Callable[[OperatorView], None],
    ) -> OperatorView:
        """Dispatch one request after fake seeding or through production transport."""

        callback_already_ran = False

        def handle_applied(disposition: ResultDisposition) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            if disposition is not ResultDisposition.APPLIED:
                self._error_message = "Turn Adviceを適用できませんでした。"
            else:
                self._error_message = None
            if callable(on_result):
                on_result(self.refresh())

        def handle_failed(reason: str) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            self._error_message = describe_gemini_turn_failure(reason)
            if callable(on_result):
                on_result(self.refresh())

        adapter.send(self._application, on_applied=handle_applied, on_failed=handle_failed)
        if not callback_already_ran:
            self._error_message = None
        return self.refresh()


class TurnAdviceIntegrationWindow(SelectionAdviceIntegrationWindow):
    """Adds the trusted Gemini Turn Advice send group."""

    def _build_mock_turn_advice_group(self) -> None:
        super()._build_mock_turn_advice_group()
        self._build_turn_gemini_send_group()

    def _build_turn_gemini_send_group(self) -> None:
        self.turn_gemini_box = QGroupBox(
            "Gemini Turn Advice（Fake/Injected — 実際のGemini APIには送信しません）"
        )
        layout = QVBoxLayout(self.turn_gemini_box)
        self.turn_gemini_helper_label = QLabel(
            "上のMOCK欄と同じ内容を、Selectionと同じ明示送信アーキテクチャ（identity/hash/"
            "staleness/legality検証つき）で適用します。自動送信・自動retryはありません。"
        )
        self.turn_gemini_helper_label.setWordWrap(True)
        layout.addWidget(self.turn_gemini_helper_label)
        self.turn_gemini_send_button = TrustedSendButton(
            "SEND TURN TO GEMINI（FAKE）", self._on_trusted_send_turn_to_gemini
        )
        layout.addWidget(self.turn_gemini_send_button)
        status_form = QFormLayout()
        self.turn_gemini_status_label = QLabel()
        self.turn_gemini_failure_label = QLabel()
        self.turn_gemini_failure_label.setWordWrap(True)
        status_form.addRow("Status", self.turn_gemini_status_label)
        status_form.addRow("Failure", self.turn_gemini_failure_label)
        layout.addLayout(status_form)
        self._right_column_layout.addWidget(self.turn_gemini_box)

    def render_view(self, view: OperatorView | None = None) -> None:
        super().render_view(view)
        current = view if view is not None else self._controller.refresh()
        controller = self._controller
        if not hasattr(controller, "turn_gemini_send_available"):
            return
        status = controller.turn_advice_gemini_status()  # type: ignore[attr-defined]
        available = controller.turn_gemini_send_available
        injected = controller.turn_gemini_uses_injected_transport  # type: ignore[attr-defined]
        if injected:
            self.turn_gemini_box.setTitle("Gemini Turn Advice（Fake/Injected）")
            self.turn_gemini_send_button.setText("SEND TURN TO GEMINI（FAKE）")
            self.turn_gemini_helper_label.setText(
                "上のMOCK欄の内容を、productionと同じbinding経路へ明示送信します。"
            )
        else:
            self.turn_gemini_box.setTitle("Gemini Turn Advice")
            self.turn_gemini_send_button.setText("SEND TURN TO GEMINI")
            self.turn_gemini_helper_label.setText(
                "確認済み盤面と合法行動をGeminiへ1回だけ送信します。ゲーム操作は行いません。"
            )
        self.turn_gemini_box.setVisible(
            available and current.projection.primary_cta == "REQUEST_TURN_ADVICE"
        )
        self.turn_gemini_send_button.setEnabled(
            current.persistence_reads_allowed
            and current.projection.primary_cta == "REQUEST_TURN_ADVICE"
            and status.status != "PENDING"
        )
        self.turn_gemini_status_label.setText(status.status)
        self.turn_gemini_failure_label.setText(status.sanitized_failure)
        self.turn_gemini_failure_label.setVisible(bool(status.sanitized_failure))

    def _on_trusted_send_turn_to_gemini(self) -> None:
        warnings = tuple(
            part.strip()
            for part in self.mock_turn_warnings_input.text().split(";")
            if part.strip()
        )
        view = self._controller.send_turn_advice_to_gemini(  # type: ignore[attr-defined]
            action_type=self.mock_turn_action_type_box.currentText(),
            action_name=self.mock_turn_action_name_box.currentText(),
            opponent_prediction=self.mock_turn_prediction_input.text(),
            rationale=self.mock_turn_rationale_input.text(),
            warnings=warnings,
            on_result=self.render_view,
        )
        self.render_view(view)
