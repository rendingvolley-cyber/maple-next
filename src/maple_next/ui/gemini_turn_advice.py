"""Production-pattern Turn Advice adapter: human-only explicit fake/injected send.

Issue #31 explicitly forbids ever sending a Turn Advice request to a real
provider. This module therefore mirrors
:mod:`maple_next.ui.gemini_advice` (the Selection-side production Gemini
adapter) architecturally — a single human-trusted activation dispatches
exactly one off-UI-thread transport call, guarded against duplicate
concurrent dispatch, and the result is only ever accepted through the
existing strict identity/hash/staleness/legality binding contract in
:meth:`maple_next.application.service.BattleApplication.apply_turn_advice_result`
— but the transport itself is always
:class:`maple_next.providers.turn_transport.FakeTurnAdviceTransport`. There
is no production HTTP transport for Turn Advice anywhere in this codebase.

Distinct from :class:`maple_next.ui.dev_advice.MockTurnAdviceAdapter`
(which applies a human-entered advice synchronously, in-process, with no
worker thread): this adapter exists to exercise — and let tests exercise —
the full off-thread dispatch/guard/staleness architecture that Issue #31
requires, without ever risking a real network call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ResultDisposition
from maple_next.providers.transport import ProviderConfig, SanitizedProviderResult
from maple_next.providers.turn_request import TurnAdviceRequest
from maple_next.providers.turn_transport import (
    TurnProviderTransport,
)
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope
from maple_next.workers.turn_advice_worker import TurnAdviceDispatch

#: Clearly distinguishable from any real Gemini model name. Never claims a
#: real provider was contacted.
FAKE_TURN_MODEL = "gemini-turn-fake-injected"


class _DispatchLike(Protocol):
    def start(self) -> None: ...


class _DispatchFactory(Protocol):
    def __call__(
        self,
        transport: TurnProviderTransport,
        request: TurnAdviceRequest,
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
        "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED",
        "GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT",
        "GEMINI_TURN_DISPATCH_BLOCKED",
        "GEMINI_TURN_RESULT_INVALID",
        "GEMINI_TURN_RESULT_STALE",
    }
)
_NETWORK_FAILURE = re.compile(r"^GEMINI_NETWORK_ERROR(?::[A-Za-z0-9._-]{1,64})?$")
_DISPLAY_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_FAILURE_MESSAGES = {
    "GEMINI_API_KEY_MISSING": "Gemini APIキーが設定されていないため送信できません。",
    "GEMINI_MODEL_MISSING": "Geminiのmodel設定が空です。",
    "GEMINI_TIMEOUT_INVALID": "Geminiのtimeout設定が不正です。",
    "GEMINI_TIMEOUT": "送信がtimeoutしました。",
    "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED": "Fake transportに応答が設定されていません。",
    "GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT": "Turn Adviceは処理中です。",
    "GEMINI_TURN_DISPATCH_BLOCKED": "現在のTurn identityでは送信を開始できません。",
    "GEMINI_TURN_RESULT_INVALID": "Turn Adviceが合法性検証を通過しませんでした。",
    "GEMINI_TURN_RESULT_STALE": "Turn Adviceは現在のTurn identityと一致しません。",
    "GEMINI_TURN_FAILURE_UNCLASSIFIED": "送信は安全な分類を取得できず失敗しました。",
}


def sanitize_gemini_turn_failure(reason: object) -> str:
    """Return only a small allowlisted failure classification for UI/storage."""

    if not isinstance(reason, str):
        return "GEMINI_TURN_FAILURE_UNCLASSIFIED"
    if reason in _EXACT_FAILURE_CODES:
        return reason
    if _NETWORK_FAILURE.fullmatch(reason):
        return reason
    if reason.startswith("GEMINI_NETWORK_ERROR"):
        return "GEMINI_NETWORK_ERROR"
    return "GEMINI_TURN_FAILURE_UNCLASSIFIED"


def describe_gemini_turn_failure(reason: str) -> str:
    """Map an allowlisted failure code to a non-secret operator message."""

    sanitized = sanitize_gemini_turn_failure(reason)
    if sanitized in _FAILURE_MESSAGES:
        return _FAILURE_MESSAGES[sanitized]
    if sanitized.startswith("GEMINI_NETWORK_ERROR"):
        return "接続できませんでした（GEMINI_NETWORK_ERROR）。"
    return _FAILURE_MESSAGES["GEMINI_TURN_FAILURE_UNCLASSIFIED"]


def _safe_display_token(value: object) -> str:
    if isinstance(value, str) and _DISPLAY_TOKEN.fullmatch(value):
        return value
    return "UNKNOWN"


def _raw_turn_payload(payload: object) -> dict[str, object]:
    """Pass through only a plain dict; anything else becomes an empty object.

    Real schema/legality/binding validation of the Lane C
    ``recommended_action``/``reasons``/``warnings``/``opponent_prediction``
    shape happens exactly once, in
    :meth:`maple_next.application.service.BattleApplication.apply_turn_advice_result`
    (via :func:`maple_next.providers.turn_response.turn_advice_body_from_dict`
    and :func:`maple_next.providers.turn_validation.validate_turn_advice_result`).
    This adapter never re-implements that logic — it only guards against a
    non-dict payload reaching the envelope.
    """

    if not isinstance(payload, dict):
        return {}
    return payload


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
        model=result.model,
    )


class GeminiTurnAdviceAdapter:
    """Fake/injected Turn Advice dispatch through the Selection-pattern architecture.

    Never contacts a real network endpoint. ``transport`` must be a
    :class:`~maple_next.providers.turn_transport.FakeTurnAdviceTransport`
    (or an equivalent test double implementing the same Protocol) seeded
    with the exact content to return, by the caller, before every ``send``.
    """

    def __init__(
        self,
        transport: TurnProviderTransport,
        *,
        dispatch_factory: _DispatchFactory = TurnAdviceDispatch,
        envelope_factory: _EnvelopeFactory = _default_envelope_factory,
    ) -> None:
        self._transport = transport
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

    def enqueue_response(self, response: SanitizedProviderResult | Exception) -> None:
        """Seed the exact fake/injected content the next ``send`` will return.

        Only meaningful when this adapter's transport is a
        :class:`~maple_next.providers.turn_transport.FakeTurnAdviceTransport`
        (or another double exposing the same ``responses`` list). Raises
        ``AttributeError`` if the configured transport has no such queue.
        """

        self._transport.responses.append(response)  # type: ignore[attr-defined]

    def send(
        self,
        application: BattleApplication,
        *,
        on_applied: Callable[[ResultDisposition], None],
        on_failed: Callable[[str], None],
    ) -> None:
        """Create one immutable job and dispatch once from an explicit command."""

        if self._in_flight:
            on_failed("GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT")
            return

        self.last_model = None
        self.last_source_type = None
        self.last_disposition = None
        self.last_failure_reason = None

        try:
            job = application.request_turn_advice(f"gemini-turn-ui-{uuid4()}")
        except DomainError:
            self.last_failure_reason = "GEMINI_TURN_DISPATCH_BLOCKED"
            on_failed("GEMINI_TURN_DISPATCH_BLOCKED")
            return
        self.last_job_id = job.job_id

        try:
            request = application.build_turn_advice_transport_request(job)
            application.mark_turn_advice_dispatched(job.job_id)
        except DomainError:
            application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_DISPATCH_BLOCKED")
            self.last_failure_reason = "GEMINI_TURN_DISPATCH_BLOCKED"
            on_failed("GEMINI_TURN_DISPATCH_BLOCKED")
            return

        def handle_succeeded(result: SanitizedProviderResult) -> None:
            self._in_flight = False
            sanitized_result = SanitizedProviderResult(
                payload=_raw_turn_payload(result.payload),
                source_type=_safe_display_token(result.source_type),
                model=_safe_display_token(result.model),
            )
            self.last_model = sanitized_result.model
            self.last_source_type = sanitized_result.source_type
            envelope = self._envelope_factory(job, sanitized_result)
            disposition = application.apply_turn_advice_result(envelope)
            self.last_disposition = disposition
            if disposition is ResultDisposition.APPLIED:
                pass
            elif disposition is ResultDisposition.STALE_REJECTED:
                application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_RESULT_STALE")
            else:
                application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_RESULT_INVALID")
            on_applied(disposition)

        def handle_failed(reason: str) -> None:
            self._in_flight = False
            sanitized = sanitize_gemini_turn_failure(reason)
            self.last_failure_reason = sanitized
            application.fail_turn_advice_job(job.job_id, sanitized)
            on_failed(sanitized)

        self.network_call_count += 1
        self.dispatch_count += 1
        self._in_flight = True
        dispatch = self._dispatch_factory(
            self._transport,
            request,
            ProviderConfig(api_key="unused-fake-transport-key"),
            on_succeeded=handle_succeeded,
            on_failed=handle_failed,
        )
        self._active_dispatch = dispatch
        dispatch.start()
