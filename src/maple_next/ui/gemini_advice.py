"""Production Selection Advice adapter: human-only explicit Gemini send.

Distinct from :class:`maple_next.ui.dev_advice.MockSelectionAdviceAdapter`.
This adapter never auto-applies a Gemini suggestion, never retries, and
never falls back to a Maple-invented selection. Network I/O happens on a
worker thread (:mod:`maple_next.workers.selection_advice_worker`); this
adapter only orchestrates the existing UI-thread application contract
before and after that off-thread call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ResultDisposition
from maple_next.providers.selection_request import SelectionAdviceRequest
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderConfigError,
    SanitizedProviderResult,
    SelectionProviderTransport,
)
from maple_next.workers.contracts.models import ResultEnvelope
from maple_next.workers.selection_advice_worker import SelectionAdviceDispatch


class _DispatchLike(Protocol):
    def start(self) -> None: ...


class _DispatchFactory(Protocol):
    def __call__(
        self,
        transport: SelectionProviderTransport,
        request: SelectionAdviceRequest,
        config: ProviderConfig,
        *,
        on_succeeded: Callable[[SanitizedProviderResult], None],
        on_failed: Callable[[str], None],
    ) -> _DispatchLike: ...

_FAILURE_MESSAGES = {
    "GEMINI_API_KEY_MISSING": (
        "Gemini APIキーが設定されていないため送信できません。"
        "環境変数MAPLE_NEXT_GEMINI_API_KEYを設定してから再試行してください。"
    ),
    "GEMINI_MODEL_MISSING": "Geminiのmodel設定が空です。環境変数を確認してください。",
    "GEMINI_TIMEOUT_INVALID": "Geminiのtimeout設定が不正です。環境変数を確認してください。",
    "GEMINI_TIMEOUT": (
        "Gemini APIへの送信がtimeoutしました。SEND SELECTION TO GEMINIを再度押してください。"
    ),
    "GEMINI_RESPONSE_ENVELOPE_MALFORMED": (
        "Gemini APIの応答形式が不正でした。SEND SELECTION TO GEMINIを再度押してください。"
    ),
    "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED": "テスト用transportに応答が設定されていません。",
}


def describe_gemini_failure(reason: str) -> str:
    """Map a transport/config failure code to a corrective Japanese message."""

    if reason in _FAILURE_MESSAGES:
        return _FAILURE_MESSAGES[reason]
    if reason.startswith("GEMINI_HTTP_ERROR:"):
        status = reason.split(":", 1)[1]
        return f"Gemini APIがエラーを返しました（{status}）。時間をおいて再試行してください。"
    if reason.startswith("GEMINI_NETWORK_ERROR"):
        return "Gemini APIに接続できませんでした。ネットワークを確認して再試行してください。"
    return f"Gemini送信に失敗しました。もう一度確認してください: {reason}"


class GeminiSelectionAdviceAdapter:
    """Production Selection Advice send through the injectable transport."""

    def __init__(
        self,
        transport: SelectionProviderTransport,
        load_config: Callable[[], ProviderConfig],
        *,
        dispatch_factory: _DispatchFactory = SelectionAdviceDispatch,
    ) -> None:
        self._transport = transport
        self._load_config = load_config
        self._dispatch_factory = dispatch_factory
        self.network_call_count = 0
        self._active_dispatch: _DispatchLike | None = None

    def send(
        self,
        application: BattleApplication,
        *,
        on_applied: Callable[[ResultDisposition], None],
        on_failed: Callable[[str], None],
    ) -> None:
        """Create the immutable job, dispatch once off-thread, never block."""

        try:
            config = self._load_config()
        except ProviderConfigError as exc:
            on_failed(str(exc))
            return

        job = application.request_selection_advice(f"gemini-ui-{uuid4()}")
        try:
            request = application.build_selection_advice_transport_request(job)
            application.mark_selection_advice_dispatched(job.job_id)
        except DomainError as exc:
            application.fail_selection_advice_job(job.job_id, str(exc))
            on_failed(str(exc))
            return

        def handle_succeeded(result: SanitizedProviderResult) -> None:
            envelope = ResultEnvelope(
                contract_version=job.contract_version,
                result_id=str(uuid4()),
                job_id=job.job_id,
                command_id=job.command_id,
                job_type=job.job_type,
                session_id=job.session_id,
                match_id=job.match_id,
                generation=job.generation,
                turn_number=job.turn_number,
                base_battle_revision=job.base_battle_revision,
                expected_state=job.expected_state,
                input_snapshot_id=job.input_snapshot_id,
                request_payload_hash=job.request_payload_hash,
                payload=result.payload,
                source_type=result.source_type,
            )
            disposition = application.apply_selection_advice_result(envelope)
            on_applied(disposition)

        def handle_failed(reason: str) -> None:
            application.fail_selection_advice_job(job.job_id, reason)
            on_failed(reason)

        self.network_call_count += 1
        dispatch = self._dispatch_factory(
            self._transport,
            request,
            config,
            on_succeeded=handle_succeeded,
            on_failed=handle_failed,
        )
        self._active_dispatch = dispatch
        dispatch.start()
