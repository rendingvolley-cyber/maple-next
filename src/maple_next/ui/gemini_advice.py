"""Production Selection Advice adapter: human-only explicit Gemini send.

Distinct from :class:`maple_next.ui.dev_advice.MockSelectionAdviceAdapter`.
This adapter never auto-applies a Selection to the game, never retries, and
never falls back to a Maple-invented selection. Network I/O happens on a
worker thread (:mod:`maple_next.workers.selection_advice_worker`); this
adapter only orchestrates the existing UI-thread application contract before
and after that off-thread call.
"""

from __future__ import annotations

import re
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
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope
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


class _EnvelopeFactory(Protocol):
    def __call__(
        self,
        job: JobEnvelope,
        result: SanitizedProviderResult,
    ) -> ResultEnvelope: ...


_EXACT_FAILURE_CODES = frozenset(
    {
        "GEMINI_API_KEY_MISSING",
        "GEMINI_MODEL_MISSING",
        "GEMINI_TIMEOUT_INVALID",
        "GEMINI_TIMEOUT",
        "GEMINI_RESPONSE_ENVELOPE_MALFORMED",
        "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED",
        "GEMINI_DISPATCH_ALREADY_IN_FLIGHT",
        "GEMINI_DISPATCH_BLOCKED",
        "GEMINI_RESULT_INVALID",
        "GEMINI_RESULT_STALE",
    }
)
_HTTP_FAILURE = re.compile(
    r"^GEMINI_HTTP_ERROR:[0-9]{3}"
    r"(?:\|(?:STATUS|REASON|DOMAIN|SERVICE)=[A-Za-z0-9._-]{1,128})*$"
)
_NETWORK_FAILURE = re.compile(r"^GEMINI_NETWORK_ERROR(?::[A-Za-z0-9._-]{1,64})?$")

_FAILURE_MESSAGES = {
    "GEMINI_API_KEY_MISSING": "Gemini APIキーが設定されていないため送信できません。",
    "GEMINI_MODEL_MISSING": "Geminiのmodel設定が空です。",
    "GEMINI_TIMEOUT_INVALID": "Geminiのtimeout設定が不正です。",
    "GEMINI_TIMEOUT": "Gemini APIへの送信がtimeoutしました。",
    "GEMINI_RESPONSE_ENVELOPE_MALFORMED": "Gemini APIの応答形式が不正でした。",
    "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED": "テスト用transportに応答が設定されていません。",
    "GEMINI_DISPATCH_ALREADY_IN_FLIGHT": "Gemini Selection Adviceは処理中です。",
    "GEMINI_DISPATCH_BLOCKED": "現在のSelection identityでは送信を開始できません。",
    "GEMINI_RESULT_INVALID": "Gemini Selection Adviceが合法性検証を通過しませんでした。",
    "GEMINI_RESULT_STALE": "Gemini Selection Adviceは現在のSelection identityと一致しません。",
    "GEMINI_FAILURE_UNCLASSIFIED": "Gemini送信は安全な分類を取得できず失敗しました。",
}


def sanitize_gemini_failure(reason: object) -> str:
    """Return only a small allowlisted failure classification for UI/storage."""

    if not isinstance(reason, str):
        return "GEMINI_FAILURE_UNCLASSIFIED"
    if reason in _EXACT_FAILURE_CODES:
        return reason
    if _HTTP_FAILURE.fullmatch(reason):
        return reason
    if _NETWORK_FAILURE.fullmatch(reason):
        return reason
    if reason.startswith("GEMINI_NETWORK_ERROR"):
        return "GEMINI_NETWORK_ERROR"
    return "GEMINI_FAILURE_UNCLASSIFIED"


def describe_gemini_failure(reason: str) -> str:
    """Map an allowlisted failure code to a non-secret operator message."""

    sanitized = sanitize_gemini_failure(reason)
    if sanitized in _FAILURE_MESSAGES:
        return _FAILURE_MESSAGES[sanitized]
    if sanitized.startswith("GEMINI_HTTP_ERROR:"):
        return f"Gemini APIがエラーを返しました（{sanitized}）。"
    if sanitized.startswith("GEMINI_NETWORK_ERROR"):
        return "Gemini APIに接続できませんでした（GEMINI_NETWORK_ERROR）。"
    return _FAILURE_MESSAGES["GEMINI_FAILURE_UNCLASSIFIED"]


def _default_envelope_factory(
    job: JobEnvelope,
    result: SanitizedProviderResult,
) -> ResultEnvelope:
    return ResultEnvelope(
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


class GeminiSelectionAdviceAdapter:
    """Production Selection Advice dispatch through an injectable transport."""

    def __init__(
        self,
        transport: SelectionProviderTransport,
        load_config: Callable[[], ProviderConfig],
        *,
        dispatch_factory: _DispatchFactory = SelectionAdviceDispatch,
        envelope_factory: _EnvelopeFactory = _default_envelope_factory,
    ) -> None:
        self._transport = transport
        self._load_config = load_config
        self._dispatch_factory = dispatch_factory
        self._envelope_factory = envelope_factory
        self.network_call_count = 0
        self.dispatch_count = 0
        self._active_dispatch: _DispatchLike | None = None
        self._in_flight = False
        self.last_job_id: str | None = None
        self.last_model: str | None = None
        self.last_source_type: str | None = None
        self.last_disposition: ResultDisposition | None = None
        self.last_failure_reason: str | None = None

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    def send(
        self,
        application: BattleApplication,
        *,
        on_applied: Callable[[ResultDisposition], None],
        on_failed: Callable[[str], None],
    ) -> None:
        """Create one immutable job and dispatch once from an explicit command."""

        if self._in_flight:
            on_failed("GEMINI_DISPATCH_ALREADY_IN_FLIGHT")
            return

        self.last_model = None
        self.last_source_type = None
        self.last_disposition = None
        self.last_failure_reason = None
        try:
            config = self._load_config()
        except ProviderConfigError as exc:
            reason = sanitize_gemini_failure(str(exc))
            self.last_failure_reason = reason
            on_failed(reason)
            return

        job = application.request_selection_advice(f"gemini-ui-{uuid4()}")
        self.last_job_id = job.job_id
        try:
            request = application.build_selection_advice_transport_request(job)
            application.mark_selection_advice_dispatched(job.job_id)
        except DomainError:
            application.fail_selection_advice_job(job.job_id, "GEMINI_DISPATCH_BLOCKED")
            self.last_failure_reason = "GEMINI_DISPATCH_BLOCKED"
            on_failed("GEMINI_DISPATCH_BLOCKED")
            return

        def handle_succeeded(result: SanitizedProviderResult) -> None:
            self._in_flight = False
            self.last_model = result.model
            self.last_source_type = result.source_type
            envelope = self._envelope_factory(job, result)
            disposition = application.apply_selection_advice_result(envelope)
            self.last_disposition = disposition
            if disposition is ResultDisposition.APPLIED:
                with application.repository.transaction():
                    application.repository.set_selection_advice_model(
                        envelope.result_id,
                        result.model,
                    )
            elif disposition is ResultDisposition.STALE_REJECTED:
                application.fail_selection_advice_job(job.job_id, "GEMINI_RESULT_STALE")
            else:
                application.fail_selection_advice_job(job.job_id, "GEMINI_RESULT_INVALID")
            on_applied(disposition)

        def handle_failed(reason: str) -> None:
            self._in_flight = False
            sanitized = sanitize_gemini_failure(reason)
            self.last_failure_reason = sanitized
            application.fail_selection_advice_job(job.job_id, sanitized)
            on_failed(sanitized)

        self.network_call_count += 1
        self.dispatch_count += 1
        self._in_flight = True
        dispatch = self._dispatch_factory(
            self._transport,
            request,
            config,
            on_succeeded=handle_succeeded,
            on_failed=handle_failed,
        )
        self._active_dispatch = dispatch
        dispatch.start()
