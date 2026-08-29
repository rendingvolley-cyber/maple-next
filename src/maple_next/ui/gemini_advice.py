"""Production Selection Advice adapter: human-only explicit Gemini send.

Distinct from :class:`maple_next.ui.dev_advice.MockSelectionAdviceAdapter`.
This adapter never auto-applies a Selection to the game and never falls back
to a Maple-invented selection. One narrowly classified quota/rate/capacity
failure may dispatch the configured fallback model exactly once. Network I/O happens on a
worker thread (:mod:`maple_next.workers.selection_advice_worker`); this
adapter only orchestrates the existing UI-thread application contract before
and after that off-thread call.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ResultDisposition
from maple_next.domain.team_build import TeamSelectionProfile
from maple_next.providers.selection_request import SelectionAdviceRequest
from maple_next.providers.transport import (
    DEFAULT_SELECTION_FALLBACK_MODEL,
    ProviderConfig,
    ProviderConfigError,
    SanitizedProviderResult,
    SelectionProviderConfig,
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
        "GEMINI_SELECTION_NOT_AUTHORIZED",
        "GEMINI_API_KEY_MISSING",
        "GEMINI_MODEL_MISSING",
        "GEMINI_SELECTION_MODELS_NOT_DISTINCT",
        "GEMINI_TIMEOUT_INVALID",
        "GEMINI_TIMEOUT",
        "GEMINI_RESPONSE_ENVELOPE_MALFORMED",
        "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED",
        "GEMINI_DISPATCH_ALREADY_IN_FLIGHT",
        "GEMINI_DISPATCH_BLOCKED",
        "GEMINI_RESULT_INVALID",
        "GEMINI_RESULT_STALE",
        "GEMINI_SELECTION_ATTEMPT_CONSUMED",
    }
)
_HTTP_FAILURE = re.compile(
    r"^GEMINI_HTTP_ERROR:[0-9]{3}"
    r"(?:\|(?:STATUS|REASON|DOMAIN|SERVICE)=[A-Za-z0-9._-]{1,128})*$"
)
_NETWORK_FAILURE = re.compile(r"^GEMINI_NETWORK_ERROR(?::[A-Za-z0-9._-]{1,64})?$")
_DISPLAY_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_FALLBACK_REASONS = frozenset(
    {
        "RATE_LIMIT_EXCEEDED",
        "QUOTA_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "CAPACITY_EXHAUSTED",
        "MODEL_CAPACITY_EXHAUSTED",
    }
)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

SELECTION_HARD_DEADLINE_MS = 20_000
SELECTION_PER_ATTEMPT_TIMEOUT_MS = 7_500

_FAILURE_MESSAGES = {
    "GEMINI_SELECTION_NOT_AUTHORIZED": "Real Selection Adviceはまだ承認されていません。",
    "GEMINI_API_KEY_MISSING": "Gemini APIキーが設定されていないため送信できません。",
    "GEMINI_MODEL_MISSING": "Geminiのmodel設定が空です。",
    "GEMINI_SELECTION_MODELS_NOT_DISTINCT": (
        "Gemini Selectionのprimary/fallback modelは別々に設定してください。"
    ),
    "GEMINI_TIMEOUT_INVALID": "Geminiのtimeout設定が不正です。",
    "GEMINI_TIMEOUT": "Gemini APIへの送信がtimeoutしました。",
    "GEMINI_RESPONSE_ENVELOPE_MALFORMED": "Gemini APIの応答形式が不正でした。",
    "FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED": "テスト用transportに応答が設定されていません。",
    "GEMINI_DISPATCH_ALREADY_IN_FLIGHT": "Gemini Selection Adviceは処理中です。",
    "GEMINI_DISPATCH_BLOCKED": "現在のSelection identityでは送信を開始できません。",
    "GEMINI_RESULT_INVALID": "Gemini推薦が現在の構築選出ルールと一致しませんでした。",
    "GEMINI_RESULT_STALE": "Gemini Selection Adviceは現在のSelection identityと一致しません。",
    "GEMINI_SELECTION_ATTEMPT_CONSUMED": (
        "このSelectionではGemini送信を実行済みです。再送できません。"
    ),
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


def is_selection_fallback_eligible(reason: object) -> bool:
    """Classify only tournament-safe transient failures for model fallback."""

    sanitized = sanitize_gemini_failure(reason)
    if sanitized == "GEMINI_TIMEOUT" or sanitized.startswith("GEMINI_NETWORK_ERROR"):
        return True
    if not sanitized.startswith("GEMINI_HTTP_ERROR:"):
        return False
    fields = sanitized.split("|")
    try:
        status = int(fields[0].split(":", 1)[1])
    except (IndexError, ValueError):
        return False
    if status in _TRANSIENT_HTTP_STATUSES:
        return True
    classifications = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
    return (
        classifications.get("STATUS") == "RESOURCE_EXHAUSTED"
        or classifications.get("REASON") in _FALLBACK_REASONS
    )


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


def _safe_display_token(value: object) -> str:
    if isinstance(value, str) and _DISPLAY_TOKEN.fullmatch(value):
        return value
    return "UNKNOWN"


def _allowlisted_selection_payload(
    payload: object,
    self_team: tuple[str, ...],
    selection_profile: TeamSelectionProfile | None = None,
) -> dict[str, object]:
    """Reject extra keys and discard arbitrary values before DB/UI handling."""

    if not isinstance(payload, dict):
        return {}
    expected_keys = (
        {
            "chosen_package",
            "selected_three",
            "lead",
            "intended_mega",
            "selection_reason",
        }
        if selection_profile is not None
        else {"selected_three", "lead"}
    )
    if set(payload) != expected_keys:
        return {}
    safe: dict[str, object] = {}
    selected_three = payload.get("selected_three")
    if (
        isinstance(selected_three, list)
        and len(selected_three) <= len(self_team)
        and all(type(name) is str and name in self_team for name in selected_three)
    ):
        safe["selected_three"] = list(selected_three)
    lead = payload.get("lead")
    if type(lead) is str and lead in self_team:
        safe["lead"] = lead
    if selection_profile is not None:
        chosen_package = payload.get("chosen_package")
        package_ids = {package.package_id for package in selection_profile.packages}
        if type(chosen_package) is str and chosen_package in package_ids:
            safe["chosen_package"] = chosen_package
        intended_mega = payload.get("intended_mega")
        if intended_mega is None or (
            type(intended_mega) is str and intended_mega in self_team
        ):
            safe["intended_mega"] = intended_mega
        reason = payload.get("selection_reason")
        if type(reason) is str and 1 <= len(reason.strip()) <= 500:
            safe["selection_reason"] = reason.strip()
    return safe


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


class GeminiSelectionAdviceAdapter:
    """Production Selection Advice dispatch through an injectable transport."""

    def __init__(
        self,
        transport: SelectionProviderTransport,
        load_config: Callable[[], SelectionProviderConfig | ProviderConfig],
        *,
        dispatch_factory: _DispatchFactory = SelectionAdviceDispatch,
        envelope_factory: _EnvelopeFactory = _default_envelope_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._load_config = load_config
        self._dispatch_factory = dispatch_factory
        self._envelope_factory = envelope_factory
        self._clock = clock
        self.network_call_count = 0
        self.dispatch_count = 0
        self._active_dispatch: _DispatchLike | None = None
        self._in_flight = False
        self.last_job_id: str | None = None
        self.last_model: str | None = None
        self.last_source_type: str | None = None
        self.last_disposition: ResultDisposition | None = None
        self.last_failure_reason: str | None = None
        self.progress_message = ""
        self.model_chain: tuple[str, ...] = ()
        self._operator_identity: tuple[str, str, int] | None = None
        self._operator_state_invalidated = False

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    def clear_operator_state(self) -> None:
        """Forget display-only state at an explicit New Match boundary."""

        self.last_job_id = None
        self.last_model = None
        self.last_source_type = None
        self.last_disposition = None
        self.last_failure_reason = None
        self.progress_message = ""
        self.model_chain = ()
        self._operator_identity = None
        self._operator_state_invalidated = True

    def operator_state_matches(
        self, *, session_id: str | None, match_id: str | None, generation: int | None
    ) -> bool:
        if self._operator_state_invalidated:
            return False
        return self._operator_identity is None or (
            session_id is not None
            and match_id is not None
            and generation is not None
            and self._operator_identity == (session_id, match_id, generation)
        )

    def send(
        self,
        application: BattleApplication,
        *,
        on_applied: Callable[[ResultDisposition], None],
        on_failed: Callable[[str], None],
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        """Create one immutable job and dispatch once from an explicit command."""

        if self._in_flight:
            on_failed("GEMINI_DISPATCH_ALREADY_IN_FLIGHT")
            return

        cascade_deadline = self._clock() + (SELECTION_HARD_DEADLINE_MS / 1000.0)

        self.clear_operator_state()
        self._operator_state_invalidated = False
        projection = application.projection()
        if (
            projection.session_id is not None
            and projection.match_id is not None
            and projection.generation is not None
        ):
            self._operator_identity = (
                projection.session_id,
                projection.match_id,
                projection.generation,
            )
        try:
            loaded_config = self._load_config()
        except ProviderConfigError as exc:
            reason = sanitize_gemini_failure(str(exc))
            self.last_failure_reason = reason
            on_failed(reason)
            return

        if isinstance(loaded_config, SelectionProviderConfig):
            attempt_configs = loaded_config.chain()
        else:
            attempt_configs = (
                loaded_config,
                ProviderConfig(
                    api_key=loaded_config.api_key,
                    model=DEFAULT_SELECTION_FALLBACK_MODEL,
                    timeout_seconds=loaded_config.timeout_seconds,
                ),
            )
        self.model_chain = tuple(config.model for config in attempt_configs)

        try:
            job = application.reserve_gemini_selection_attempt(f"gemini-ui-{uuid4()}")
        except DomainError as exc:
            reason = (
                "GEMINI_SELECTION_ATTEMPT_CONSUMED"
                if str(exc) == "GEMINI_SELECTION_ATTEMPT_CONSUMED"
                else "GEMINI_DISPATCH_BLOCKED"
            )
            self.last_failure_reason = reason
            on_failed(reason)
            return
        self.last_job_id = job.job_id
        try:
            request = application.build_selection_advice_transport_request(job)
            application.mark_selection_advice_dispatched(job.job_id)
        except DomainError:
            application.fail_selection_advice_job(job.job_id, "GEMINI_DISPATCH_BLOCKED")
            self.last_failure_reason = "GEMINI_DISPATCH_BLOCKED"
            on_failed("GEMINI_DISPATCH_BLOCKED")
            return

        def handle_succeeded(
            result: SanitizedProviderResult,
            *,
            config: ProviderConfig,
            attempt_ordinal: int,
        ) -> None:
            self._in_flight = False
            self.progress_message = ""
            if self._clock() > cascade_deadline:
                application.repository.finish_selection_provider_attempt(
                    job_id=job.job_id,
                    attempt_ordinal=attempt_ordinal,
                    outcome="FAILED",
                    reason="GEMINI_TIMEOUT",
                )
                self.last_failure_reason = "GEMINI_TIMEOUT"
                application.fail_selection_advice_job(job.job_id, "GEMINI_TIMEOUT")
                on_failed("GEMINI_TIMEOUT")
                return
            application.repository.finish_selection_provider_attempt(
                job_id=job.job_id,
                attempt_ordinal=attempt_ordinal,
                outcome="SUCCEEDED",
            )
            sanitized_result = SanitizedProviderResult(
                payload=_allowlisted_selection_payload(
                    result.payload,
                    request.self_team,
                    request.selection_profile,
                ),
                source_type=_safe_display_token(result.source_type),
                model=_safe_display_token(config.model),
            )
            self.last_model = sanitized_result.model
            self.last_source_type = sanitized_result.source_type
            envelope = replace(
                self._envelope_factory(job, sanitized_result),
                model=sanitized_result.model,
            )
            disposition = application.apply_selection_advice_result(envelope)
            self.last_disposition = disposition
            if disposition is ResultDisposition.STALE_REJECTED:
                application.fail_selection_advice_job(job.job_id, "GEMINI_RESULT_STALE")
            elif disposition is not ResultDisposition.APPLIED:
                application.fail_selection_advice_job(job.job_id, "GEMINI_RESULT_INVALID")
            on_applied(disposition)

        def handle_failed(
            reason: str,
            *,
            config: ProviderConfig,
            attempt_ordinal: int,
        ) -> None:
            sanitized = sanitize_gemini_failure(reason)
            application.repository.finish_selection_provider_attempt(
                job_id=job.job_id,
                attempt_ordinal=attempt_ordinal,
                outcome="FAILED",
                reason=sanitized,
            )
            next_ordinal = attempt_ordinal + 1
            if (
                is_selection_fallback_eligible(sanitized)
                and next_ordinal <= len(attempt_configs)
                and self._clock() < cascade_deadline
            ):
                self.progress_message = (
                    f"モデル切替中… {next_ordinal}/{len(attempt_configs)}"
                )
                if on_progress is not None:
                    on_progress()
                dispatch_attempt(next_ordinal)
                return
            self._in_flight = False
            self.progress_message = ""
            self.last_failure_reason = sanitized
            application.fail_selection_advice_job(job.job_id, sanitized)
            on_failed(sanitized)

        def dispatch_attempt(attempt_ordinal: int) -> None:
            remaining_seconds = cascade_deadline - self._clock()
            if remaining_seconds <= 0:
                self._in_flight = False
                self.progress_message = ""
                self.last_failure_reason = "GEMINI_TIMEOUT"
                application.fail_selection_advice_job(job.job_id, "GEMINI_TIMEOUT")
                on_failed("GEMINI_TIMEOUT")
                return
            base_config = attempt_configs[attempt_ordinal - 1]
            config = replace(
                base_config,
                timeout_seconds=min(
                    base_config.timeout_seconds,
                    SELECTION_PER_ATTEMPT_TIMEOUT_MS / 1000.0,
                    remaining_seconds,
                ),
            )
            self.last_model = _safe_display_token(config.model)
            self.progress_message = (
                f"Gemini送信中… {attempt_ordinal}/{len(attempt_configs)}"
            )
            self._in_flight = True
            if on_progress is not None:
                on_progress()
            application.repository.start_selection_provider_attempt(
                job_id=job.job_id,
                attempt_ordinal=attempt_ordinal,
                model=_safe_display_token(config.model),
            )
            self.network_call_count += 1
            self.dispatch_count += 1
            dispatch = self._dispatch_factory(
                self._transport,
                request,
                config,
                on_succeeded=lambda result: handle_succeeded(
                    result, config=config, attempt_ordinal=attempt_ordinal
                ),
                on_failed=lambda reason: handle_failed(
                    reason, config=config, attempt_ordinal=attempt_ordinal
                ),
            )
            self._active_dispatch = dispatch
            self.progress_message = (
                f"Gemini応答待ち… {attempt_ordinal}/{len(attempt_configs)}"
            )
            if on_progress is not None:
                on_progress()
            dispatch.start()

        dispatch_attempt(1)
