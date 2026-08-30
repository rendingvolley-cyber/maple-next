"""Injectable Selection Advice provider transport boundary.

Production networking (:class:`GeminiSelectionAdviceTransport`) and the
test-only fake transport are intentionally split so that no test ever needs
network access, an API key, or a real Gemini account. Nothing in this module
imports SQLite, the repository, or any PySide6 widget.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from maple_next.providers.selection_request import (
    SelectionAdviceRequest,
    build_provider_request_body,
)

#: Shared trusted source token used by Selection Advice. Turn Advice has its
#: own production boundary in ``turn_transport.py``.
GEMINI_SOURCE_TYPE = "GEMINI"

DEFAULT_SELECTION_PRIMARY_MODEL = "gemini-3.6-flash"
DEFAULT_SELECTION_FALLBACK_MODEL = "gemini-3.5-flash"
_DEFAULT_TIMEOUT_SECONDS = 30.0
#: Deliberately separate from Turn Advice's own
#: ``MAPLE_NEXT_GEMINI_TURN_AUTHORIZED`` flag in ``turn_transport.py``.
#: Selection and Turn are independent human/provider lanes -- merely having
#: an API key in the environment can never enable either lane's network
#: access on its own.
SELECTION_PROVIDER_AUTHORIZATION_ENV = "MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED"
_API_KEY_ENV = "MAPLE_NEXT_GEMINI_API_KEY"
_SELECTION_PRIMARY_MODEL_ENV = "MAPLE_NEXT_GEMINI_SELECTION_PRIMARY_MODEL"
_SELECTION_FALLBACK_MODEL_ENV = "MAPLE_NEXT_GEMINI_SELECTION_FALLBACK_MODEL"
_SELECTION_MODEL_CHAIN_ENV = "MAPLE_NEXT_GEMINI_SELECTION_MODEL_CHAIN"
_LEGACY_SELECTION_MODEL_CHAIN_ENV = "MAPLE_SELECTION_MODEL_CHAIN"
_TIMEOUT_ENV = "MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS"
_HTTP_ERROR_BODY_MAX_BYTES = 16_384
_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_STATUS_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_DOMAIN_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


class ProviderConfigError(RuntimeError):
    """Raised when provider runtime configuration is missing or invalid."""


class ProviderTransportError(RuntimeError):
    """Raised for network, HTTP, timeout, or outer-envelope transport failures.

    Never includes the API key, authorization header, or raw response body.
    """


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Runtime-injected provider configuration. Never part of the domain."""

    api_key: str
    model: str = DEFAULT_SELECTION_PRIMARY_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ProviderConfigError("GEMINI_API_KEY_MISSING")
        if not self.model.strip():
            raise ProviderConfigError("GEMINI_MODEL_MISSING")
        if self.timeout_seconds <= 0:
            raise ProviderConfigError("GEMINI_TIMEOUT_INVALID")


@dataclass(frozen=True, slots=True)
class SelectionProviderConfig:
    """Lane-specific Selection primary/fallback policy and shared credentials."""

    api_key: str
    primary_model: str = DEFAULT_SELECTION_PRIMARY_MODEL
    fallback_model: str = DEFAULT_SELECTION_FALLBACK_MODEL
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    additional_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ProviderConfig(
            api_key=self.api_key,
            model=self.primary_model,
            timeout_seconds=self.timeout_seconds,
        )
        if not self.fallback_model.strip():
            raise ProviderConfigError("GEMINI_MODEL_MISSING")
        if self.primary_model == self.fallback_model:
            raise ProviderConfigError("GEMINI_SELECTION_MODELS_NOT_DISTINCT")
        models = (self.primary_model, self.fallback_model, *self.additional_models)
        if any(not model.strip() for model in models) or len(set(models)) != len(models):
            raise ProviderConfigError("GEMINI_SELECTION_MODELS_NOT_DISTINCT")

    def primary(self) -> ProviderConfig:
        return ProviderConfig(self.api_key, self.primary_model, self.timeout_seconds)

    def fallback(self) -> ProviderConfig:
        return ProviderConfig(self.api_key, self.fallback_model, self.timeout_seconds)

    def chain(self) -> tuple[ProviderConfig, ...]:
        """Return the configured Selection-only model cascade in order."""

        return tuple(
            ProviderConfig(self.api_key, model, self.timeout_seconds)
            for model in (self.primary_model, self.fallback_model, *self.additional_models)
        )


def _configured_selection_models() -> tuple[str, ...] | None:
    """Load one existing comma-separated Gemini chain, excluding local sentinels."""

    raw = (
        os.environ.get(_SELECTION_MODEL_CHAIN_ENV, "").strip()
        or os.environ.get(_LEGACY_SELECTION_MODEL_CHAIN_ENV, "").strip()
    )
    if not raw:
        return None
    models: list[str] = []
    for value in raw.split(","):
        model = value.strip()
        if model == "maple_internal":
            break
        if model:
            models.append(model)
    if len(models) < 2:
        raise ProviderConfigError("GEMINI_SELECTION_MODELS_NOT_DISTINCT")
    return tuple(models)


def load_selection_provider_config_from_env() -> SelectionProviderConfig:
    """Fail closed unless real Selection Advice was explicitly authorized.

    The flag is deliberately separate from the API key. Merely having a key
    in the environment can never enable Selection Advice network access.
    Checked before the API key so an unauthorized runtime never even reveals
    whether a key is configured.
    """

    if os.environ.get(SELECTION_PROVIDER_AUTHORIZATION_ENV, "").strip() != "1":
        raise ProviderConfigError("GEMINI_SELECTION_NOT_AUTHORIZED")
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        raise ProviderConfigError("GEMINI_API_KEY_MISSING")
    configured_chain = _configured_selection_models()
    primary_model = (
        configured_chain[0]
        if configured_chain is not None
        else os.environ.get(_SELECTION_PRIMARY_MODEL_ENV, "").strip()
        or DEFAULT_SELECTION_PRIMARY_MODEL
    )
    fallback_model = (
        configured_chain[1]
        if configured_chain is not None
        else os.environ.get(_SELECTION_FALLBACK_MODEL_ENV, "").strip()
        or DEFAULT_SELECTION_FALLBACK_MODEL
    )
    raw_timeout = os.environ.get(_TIMEOUT_ENV, "").strip()
    try:
        timeout_seconds = float(raw_timeout) if raw_timeout else _DEFAULT_TIMEOUT_SECONDS
    except ValueError as exc:
        raise ProviderConfigError("GEMINI_TIMEOUT_INVALID") from exc
    return SelectionProviderConfig(
        api_key=api_key,
        primary_model=primary_model,
        fallback_model=fallback_model,
        timeout_seconds=timeout_seconds,
        additional_models=configured_chain[2:] if configured_chain is not None else (),
    )


def load_provider_config_from_env() -> ProviderConfig:
    """Backward-compatible Selection-primary loader for existing callers/tests."""

    return load_selection_provider_config_from_env().primary()


@dataclass(frozen=True, slots=True)
class SanitizedProviderResult:
    """Provider output handed back to the UI thread. Carries no secrets."""

    payload: dict[str, Any]
    source_type: str
    model: str


class SelectionProviderTransport(Protocol):
    """Injectable transport boundary. Production and fake implementations."""

    def send(
        self, request: SelectionAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult: ...


def _safe_token(
    value: object,
    *,
    allowed_chars: frozenset[str],
    max_length: int,
) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if any(char not in allowed_chars for char in value):
        return None
    return value


def _sanitized_http_error_reason(exc: urllib.error.HTTPError) -> str:
    """Return a small allowlisted HTTP classification without raw provider data."""

    generic = f"GEMINI_HTTP_ERROR:{exc.code}"
    try:
        raw_body = exc.read(_HTTP_ERROR_BODY_MAX_BYTES + 1)
    except (OSError, ValueError):
        return generic
    if not isinstance(raw_body, bytes) or not raw_body:
        return generic
    if len(raw_body) > _HTTP_ERROR_BODY_MAX_BYTES:
        return generic

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return generic
    if not isinstance(payload, dict):
        return generic
    error = payload.get("error")
    if not isinstance(error, dict):
        return generic

    status = _safe_token(
        error.get("status"),
        allowed_chars=_STATUS_TOKEN_CHARS,
        max_length=64,
    )
    if error.get("status") is not None and status is None:
        return generic

    details = error.get("details")
    if details is not None and not isinstance(details, list):
        return generic

    reason: str | None = None
    domain: str | None = None
    service: str | None = None
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                return generic
            if detail.get("@type") != _ERROR_INFO_TYPE:
                continue

            reason = _safe_token(
                detail.get("reason"),
                allowed_chars=_STATUS_TOKEN_CHARS,
                max_length=64,
            )
            domain = _safe_token(
                detail.get("domain"),
                allowed_chars=_DOMAIN_TOKEN_CHARS,
                max_length=128,
            )
            metadata = detail.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                return generic
            if isinstance(metadata, dict):
                service = _safe_token(
                    metadata.get("service"),
                    allowed_chars=_DOMAIN_TOKEN_CHARS,
                    max_length=128,
                )
                if metadata.get("service") is not None and service is None:
                    return generic
            if detail.get("reason") is not None and reason is None:
                return generic
            if detail.get("domain") is not None and domain is None:
                return generic
            break

    fields: list[str] = []
    if status is not None:
        fields.append(f"STATUS={status}")
    if reason is not None:
        fields.append(f"REASON={reason}")
    if domain is not None:
        fields.append(f"DOMAIN={domain}")
    if service is not None:
        fields.append(f"SERVICE={service}")
    if not fields:
        return generic
    return f"{generic}|{'|'.join(fields)}"


class GeminiSelectionAdviceTransport:
    """Production transport. The only module allowed to call the Gemini API."""

    def __init__(self, *, endpoint_template: str | None = None) -> None:
        self._endpoint_template = (
            endpoint_template
            or "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        )

    def send(
        self, request: SelectionAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        body = build_provider_request_body(request)
        endpoint = self._endpoint_template.format(model=config.model)
        http_request = urllib.request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": config.api_key,
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - explicit HTTPS endpoint
                http_request, timeout=config.timeout_seconds
            ) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            reason = _sanitized_http_error_reason(exc)
            raise ProviderTransportError(reason) from None
        except urllib.error.URLError as exc:
            # urlopen(..., timeout=...) reports a connect/read timeout by
            # wrapping it as URLError(reason=<TimeoutError instance>), not by
            # raising TimeoutError directly. In Python 3.11, socket.timeout
            # is TimeoutError itself (unified in 3.10), so this isinstance
            # check also covers the socket.timeout case.
            if isinstance(exc.reason, TimeoutError):
                raise ProviderTransportError("GEMINI_TIMEOUT") from None
            raise ProviderTransportError(f"GEMINI_NETWORK_ERROR:{exc.reason}") from None
        except TimeoutError as exc:
            raise ProviderTransportError("GEMINI_TIMEOUT") from exc

        text = self._extract_text(raw_body)
        payload = self._parse_selection_payload(text)
        return SanitizedProviderResult(
            payload=payload, source_type=GEMINI_SOURCE_TYPE, model=config.model
        )

    @staticmethod
    def _extract_text(raw_body: bytes) -> str:
        try:
            envelope = json.loads(raw_body.decode("utf-8"))
            candidate = envelope["candidates"][0]
            part = candidate["content"]["parts"][0]
            text = part["text"]
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            UnicodeDecodeError,
        ) as exc:
            raise ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED") from exc
        if not isinstance(text, str):
            raise ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED")
        return text

    @staticmethod
    def _parse_selection_payload(text: str) -> dict[str, Any]:
        """Best-effort strict-JSON parse of the model's inner response text.

        Markdown or free-text responses do not raise here — they surface as
        an empty/partial payload so the existing strict binding contract in
        ``apply_selection_advice_result`` rejects them as INVALID_REJECTED
        rather than this transport inventing a fallback selection.
        """

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed


@dataclass
class FakeSelectionAdviceTransport:
    """Test-only transport. Records every call; never touches the network."""

    responses: list[SanitizedProviderResult | Exception] = field(default_factory=list)
    calls: list[tuple[SelectionAdviceRequest, ProviderConfig]] = field(default_factory=list)

    def send(
        self, request: SelectionAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        self.calls.append((request, config))
        if not self.responses:
            raise ProviderTransportError("FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def call_count(self) -> int:
        return len(self.calls)
