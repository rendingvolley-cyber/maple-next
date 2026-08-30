"""Issue #26 app integration for explicit Selection Gemini advice and APPLY."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout

from maple_next.application.service import DomainError
from maple_next.capture.contracts import VideoCaptureBackend
from maple_next.domain.enums import BattleState, JobStatus, ResultDisposition
from maple_next.providers.transport import GEMINI_SOURCE_TYPE
from maple_next.ui.controller import OperatorView
from maple_next.ui.explicit_turn_number import (
    ExplicitTurnNumberController,
    ExplicitTurnNumberWindow,
)


@dataclass(frozen=True, slots=True)
class SelectionAdviceStatusView:
    """Sanitized UI-only state for one human-triggered Selection request."""

    status: str
    source_type: str
    model: str
    binding_status: str
    legality_status: str
    sanitized_failure: str
    advice_id: str | None
    can_apply: bool


class SelectionAdviceIntegrationController(ExplicitTurnNumberController):
    """Adds current-identity Gemini display and explicit Advice adoption."""

    def selection_advice_status(self) -> SelectionAdviceStatusView:
        adapter = self._gemini_adapter
        projection = self._application.projection()
        if adapter is None:
            return SelectionAdviceStatusView(
                status="UNAVAILABLE",
                source_type="—",
                model="—",
                binding_status="NOT_CHECKED",
                legality_status="NOT_CHECKED",
                sanitized_failure="",
                advice_id=None,
                can_apply=False,
            )
        adapter_state_current = adapter.operator_state_matches(
            session_id=projection.session_id,
            match_id=projection.match_id,
            generation=projection.generation,
        )
        if adapter_state_current and adapter.in_flight:
            return SelectionAdviceStatusView(
                status="PENDING",
                source_type="GEMINI",
                model=adapter.last_model or "—",
                binding_status="PENDING",
                legality_status="PENDING",
                sanitized_failure="",
                advice_id=None,
                can_apply=False,
            )
        if adapter_state_current and adapter.last_failure_reason is not None:
            return SelectionAdviceStatusView(
                status="FAILED",
                source_type="GEMINI",
                model=adapter.last_model or "—",
                binding_status="REJECTED",
                legality_status="NOT_APPLICABLE",
                sanitized_failure=adapter.last_failure_reason,
                advice_id=None,
                can_apply=False,
            )
        durable_failure = self._application.gemini_selection_last_failure_reason()
        if durable_failure is not None:
            return SelectionAdviceStatusView(
                status="FAILED",
                source_type="GEMINI",
                model="—",
                binding_status="REJECTED",
                legality_status="NOT_APPLICABLE",
                sanitized_failure=durable_failure,
                advice_id=None,
                can_apply=False,
            )
        if adapter_state_current and adapter.last_disposition is ResultDisposition.STALE_REJECTED:
            return SelectionAdviceStatusView(
                status="STALE",
                source_type=adapter.last_source_type or "GEMINI",
                model=adapter.last_model or "—",
                binding_status="STALE_OR_MISMATCH",
                legality_status="NOT_APPLICABLE",
                sanitized_failure="GEMINI_RESULT_STALE",
                advice_id=None,
                can_apply=False,
            )
        if adapter_state_current and adapter.last_disposition in {
            ResultDisposition.INVALID_REJECTED,
            ResultDisposition.DUPLICATE_IGNORED,
        }:
            return SelectionAdviceStatusView(
                status="FAILED",
                source_type=adapter.last_source_type or "GEMINI",
                model=adapter.last_model or "—",
                binding_status="REJECTED",
                legality_status="INVALID",
                sanitized_failure="GEMINI_RESULT_INVALID",
                advice_id=None,
                can_apply=False,
            )

        advice_id = projection.current_selection_advice_id
        if advice_id is None:
            return SelectionAdviceStatusView(
                status="IDLE",
                source_type="GEMINI",
                model="—",
                binding_status="NOT_CHECKED",
                legality_status="NOT_CHECKED",
                sanitized_failure="",
                advice_id=None,
                can_apply=False,
            )

        stored = self._repository.get_selection_advice(advice_id)
        source_type = cast(str, stored["source_type"])
        model = cast(str, stored.get("model", "")) or adapter.last_model or "—"
        current = self._is_current_advice(stored)
        gemini = source_type == GEMINI_SOURCE_TYPE
        can_apply = (
            current
            and gemini
            and projection.session_state == BattleState.SELECTION_ADVICE_READY.value
        )
        return SelectionAdviceStatusView(
            status="SUCCESS" if current and gemini else "MISMATCH",
            source_type=source_type,
            model=model,
            binding_status="CURRENT" if current else "STALE_OR_MISMATCH",
            legality_status="VALID" if current else "NOT_APPLICABLE",
            sanitized_failure="" if current else "GEMINI_RESULT_STALE",
            advice_id=advice_id,
            can_apply=can_apply,
        )

    def apply_current_gemini_advice(self, *, human_confirmed: bool) -> OperatorView:
        """Adopt exactly the displayed current Gemini Advice after human APPLY."""

        status = self.selection_advice_status()
        if not human_confirmed:
            self._error_message = "APPLY前に確認チェックを入れてください。"
            return self.refresh()
        if not status.can_apply or status.advice_id is None:
            self._error_message = (
                "表示中のGemini Adviceが現在のSelection identityと一致しないため"
                "APPLYできません。"
            )
            return self.refresh()
        try:
            stored = self._repository.get_selection_advice(status.advice_id)
            selected_three = cast(tuple[str, str, str], stored["selected_three"])
            lead = cast(str, stored["lead"])
            self._application.apply_selection(
                selected_three=selected_three,
                lead=lead,
                human_confirmed=True,
            )
        except (DomainError, KeyError, RuntimeError):
            self._error_message = (
                "表示中のGemini AdviceをAPPLYできませんでした。"
                "canonical stateは変更されていません。"
            )
        else:
            self._error_message = None
        return self.refresh()

    def apply_selection_with_current_gemini_context(
        self,
        selected_three: Sequence[str],
        lead: str,
        *,
        human_confirmed: bool,
    ) -> OperatorView:
        """Apply a human-edited selection only while Gemini context is current.

        Selection v3 lets the operator modify Gemini's default numbered order.
        This controller-owned boundary preserves that choice while re-checking
        the authoritative advice identity immediately before the domain apply.
        """

        if not human_confirmed:
            self._error_message = "APPLY前に人間の明示確認が必要です。"
            return self.refresh()
        if len(selected_three) != 3:
            self._error_message = (
                "選出をAPPLYできませんでした。canonical stateは変更されていません。"
            )
            return self.refresh()
        selected = (selected_three[0], selected_three[1], selected_three[2])
        status = self.selection_advice_status()
        if not status.can_apply or status.advice_id is None:
            self._error_message = (
                "表示中のGemini Adviceが現在のSelection identityと一致しないため"
                "APPLYできません。"
            )
            return self.refresh()
        try:
            stored = self._repository.get_selection_advice(status.advice_id)
            if not self._is_current_advice(stored):
                self._error_message = (
                    "表示中のGemini Adviceが現在のSelection identityと一致しないため"
                    "APPLYできません。"
                )
                return self.refresh()
            self._application.apply_selection(
                selected_three=selected,
                lead=lead,
                human_confirmed=True,
            )
        except (DomainError, KeyError, RuntimeError):
            self._error_message = (
                "選出をAPPLYできませんでした。canonical stateは変更されていません。"
            )
        else:
            self._error_message = None
        return self.refresh()

    def _is_current_advice(self, stored: dict[str, object]) -> bool:
        session = self._repository.load_active_session()
        if session is None:
            return False
        try:
            job = self._repository.get_job(cast(str, stored["job_id"]))
        except (KeyError, TypeError):
            return False
        return all(
            (
                stored.get("advice_id") == session.current_selection_advice_id,
                stored.get("session_id") == session.session_id,
                job.session_id == session.session_id,
                job.match_id == session.match_id,
                job.generation == session.generation,
                job.input_snapshot_id == session.current_reviewed_selection_id,
                job.status is JobStatus.SUCCEEDED,
                session.battle_revision == job.base_battle_revision + 1,
            )
        )


class SelectionAdviceIntegrationWindow(ExplicitTurnNumberWindow):
    """Minimal Selection Gemini status and explicit APPLY presentation."""

    def __init__(
        self,
        controller: SelectionAdviceIntegrationController,
        *,
        capture_backend: VideoCaptureBackend | None = None,
        auto_start_capture: bool = True,
    ) -> None:
        self._selection_gemini_controller = controller
        self._selection_integration_ready = False
        self._loaded_gemini_advice_id: str | None = None
        super().__init__(
            controller,
            capture_backend=capture_backend,
            auto_start_capture=auto_start_capture,
        )
        self._selection_integration_ready = True
        self.render_view()

    def _build_gemini_send_group(self) -> None:
        super()._build_gemini_send_group()
        layout = self.gemini_group.layout()
        if not isinstance(layout, QVBoxLayout):
            raise RuntimeError("Gemini group layout must be QVBoxLayout")
        self.gemini_status_label = QLabel()
        self.gemini_failure_label = QLabel()
        self.gemini_failure_label.setWordWrap(True)
        layout.addWidget(self.gemini_status_label)
        layout.addWidget(self.gemini_failure_label)

    def _build_advice_display_group(self) -> None:
        super()._build_advice_display_group()
        layout = self.advice_group.layout()
        if not isinstance(layout, QFormLayout):
            raise RuntimeError("Selection Advice layout must be QFormLayout")
        self.advice_model_label = QLabel()
        self.advice_binding_label = QLabel()
        self.advice_legality_label = QLabel()
        self.advice_identity_label = QLabel()
        layout.addRow("Model", self.advice_model_label)
        layout.addRow("Binding", self.advice_binding_label)
        layout.addRow("Legality", self.advice_legality_label)
        layout.addRow("Identity", self.advice_identity_label)

    def render_view(self, view: OperatorView | None = None) -> None:
        current = view if view is not None else self._selection_gemini_controller.refresh()
        super().render_view(current)
        if not self._selection_integration_ready:
            return

        if current.persistence_reads_allowed:
            status = self._selection_gemini_controller.selection_advice_status()
        else:
            status = SelectionAdviceStatusView(
                status="UNAVAILABLE",
                source_type="—",
                model="—",
                binding_status="NOT_CHECKED",
                legality_status="NOT_CHECKED",
                sanitized_failure="",
                advice_id=None,
                can_apply=False,
            )
        selection_scope = (
            current.projection.current_reviewed_selection_id is not None
            and current.session_state in {"SELECTION_OPEN", "SELECTION_ADVICE_READY"}
        )
        self.gemini_group.setVisible(
            self._selection_gemini_controller.gemini_send_available and selection_scope
        )
        # A fallback/no-cache view must not trigger durable selection reads.
        # Resend and attempt status are persistence-backed; keep the send
        # surface fail-closed until a normal refresh restores a durable view.
        resend_eligible = False
        selection_attempt_consumed = True
        if current.persistence_reads_allowed:
            resend_eligible = bool(
                self._selection_gemini_controller.gemini_selection_resend_eligible()
            )
            selection_attempt_consumed = bool(
                self._selection_gemini_controller.gemini_selection_attempt_consumed()
            )
        self.gemini_send_button.setText(
            "Gemini再送" if resend_eligible else "SEND SELECTION TO GEMINI"
        )
        self.gemini_send_button.setEnabled(
            current.persistence_reads_allowed
            and current.primary_cta == "REQUEST_SELECTION_ADVICE"
            and current.projection.provider_send_enabled
            and status.status not in {"PENDING", "SUCCESS"}
            and (
                not selection_attempt_consumed
                or resend_eligible
            )
        )
        progress = self._selection_gemini_controller.gemini_selection_progress()
        self.gemini_status_label.setText(
            progress if status.status == "PENDING" and progress else f"Status: {status.status}"
        )
        self.gemini_failure_label.setText(
            f"Sanitized failure: {status.sanitized_failure}"
            if status.sanitized_failure
            else ""
        )
        self.gemini_failure_label.setVisible(bool(status.sanitized_failure))

        self.advice_model_label.setText(status.model)
        self.advice_binding_label.setText(status.binding_status)
        self.advice_legality_label.setText(status.legality_status)
        self.advice_identity_label.setText(status.status)

        if (
            status.status == "SUCCESS"
            and status.can_apply
            and status.advice_id is not None
            and status.advice_id != self._loaded_gemini_advice_id
            and current.advice is not None
        ):
            self._loaded_gemini_advice_id = status.advice_id
            selected = set(current.advice.selected_three)
            for checkbox in self.actual_checkboxes:
                checkbox.setChecked(checkbox.text() in selected)
            self.actual_lead_box.setCurrentText(current.advice.lead)
            self.apply_confirm_checkbox.setChecked(False)
        self._update_actual_controls()

    def _update_actual_controls(self, _value: object = None) -> None:
        super()._update_actual_controls(_value)
        if not self._selection_integration_ready:
            return
        if not getattr(self, "_persistence_reads_allowed", True):
            self.apply_button.setEnabled(False)
            return
        status = self._selection_gemini_controller.selection_advice_status()
        lock_to_gemini = status.source_type == GEMINI_SOURCE_TYPE and status.can_apply
        for checkbox in self.actual_checkboxes:
            checkbox.setEnabled(not lock_to_gemini)
        self.actual_lead_box.setEnabled(not lock_to_gemini)
        if lock_to_gemini:
            self.apply_button.setEnabled(
                self.apply_confirm_checkbox.isChecked() and status.can_apply
            )

    def _on_apply(self, _checked: bool = False) -> None:
        if not self._persistence_reads_allowed:
            return
        status = self._selection_gemini_controller.selection_advice_status()
        if status.source_type == GEMINI_SOURCE_TYPE:
            view = self._selection_gemini_controller.apply_current_gemini_advice(
                human_confirmed=self.apply_confirm_checkbox.isChecked()
            )
            self.render_view(view)
            return
        super()._on_apply(_checked)
