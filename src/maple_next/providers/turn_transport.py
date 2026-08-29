"""Injectable Gemini Turn Advice transport boundary.

The production transport is inert until a trusted human activation reaches
the adapter and the runtime authorization flag is explicitly enabled. Tests
use :class:`FakeTurnAdviceTransport`; they never require network or secrets.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from maple_next.providers.transport import (
    ProviderConfig,
    ProviderConfigError,
    SanitizedProviderResult,
    _sanitized_http_error_reason,
)
from maple_next.providers.transport import (
    ProviderTransportError as ProviderTransportError,
)
from maple_next.providers.turn_advice_rich_state import (
    RichStateTurnAdviceRequest,
    build_rich_provider_request_body,
)
from maple_next.providers.turn_request import (
    TurnAdviceRequest,
    build_provider_request_body,
)

TurnAdviceTransportRequest = TurnAdviceRequest | RichStateTurnAdviceRequest

GEMINI_TURN_SOURCE_TYPE = "GEMINI"
FAKE_TURN_ADVICE_SOURCE_TYPE = GEMINI_TURN_SOURCE_TYPE
TURN_PROVIDER_AUTHORIZATION_ENV = "MAPLE_NEXT_GEMINI_TURN_AUTHORIZED"
TURN_PROVIDER_MODEL_ENV = "MAPLE_NEXT_GEMINI_TURN_MODEL"
DEFAULT_TURN_MODEL = "gemini-3.5-flash-lite"
_API_KEY_ENV = "MAPLE_NEXT_GEMINI_API_KEY"
_TIMEOUT_ENV = "MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TOURNAMENT_TIMEOUT_SECONDS = 15.0

_TOURNAMENT_FAST_LOOKAHEAD_POLICY = """Tournament fast-lookahead requirement:
- Reach one concrete recommendation quickly; never abstain, defer the strategic choice to the
  human, or return without exactly one legal recommended_action merely because information is
  uncertain.
- Use bounded forward lookahead of roughly 2-3 turns from the current decision. This means the
  current action, the opponent's most important plausible reply, and the next meaningful
  continuation/revenge/switch sequence. Do not attempt exhaustive multi-turn tree search.
- Branch only on the 1-2 opponent replies that could materially change the recommendation.
  Collapse low-impact possibilities into the existing uncertainty/robustness handling instead
  of exploring every possible move or switch.
- Prefer the action whose short line leaves the best practical position and endgame resources
  across those important replies. Include switch cost, sacrifice/revenge sequencing, setup
  payoff, priority/speed-control preservation, and a clean finishing route when relevant.
- If uncertainty remains after this bounded comparison, still choose the single best legal
  action. Express the uncertainty through recommendation_robustness, opponent_prediction, and
  warnings; uncertainty is never a reason to omit or postpone the recommendation.
- Keep the returned JSON concise. Do not output the simulated line or hidden chain-of-thought;
  return only the strict response contract requested by the surrounding prompt."""


def load_authorized_turn_provider_config_from_env() -> ProviderConfig:
    """Fail closed unless real Turn Advice was explicitly authorized.

    The flag is deliberately separate from the API key. Merely having a key
    in the environment can never enable Turn Advice network access.
    Tournament Turn Advice is capped at 15 seconds even if an older runtime
    environment still carries a larger timeout value.
    """

    if os.environ.get(TURN_PROVIDER_AUTHORIZATION_ENV, "").strip() != "1":
        raise ProviderConfigError("GEMINI_TURN_NOT_AUTHORIZED")
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        raise ProviderConfigError("GEMINI_API_KEY_MISSING")
    model = os.environ.get(TURN_PROVIDER_MODEL_ENV, "").strip() or DEFAULT_TURN_MODEL
    raw_timeout = os.environ.get(_TIMEOUT_ENV, "").strip()
    try:
        requested_timeout = (
            float(raw_timeout) if raw_timeout else _DEFAULT_TIMEOUT_SECONDS
        )
    except ValueError as exc:
        raise ProviderConfigError("GEMINI_TIMEOUT_INVALID") from exc
    timeout_seconds = min(requested_timeout, _MAX_TOURNAMENT_TIMEOUT_SECONDS)
    return ProviderConfig(api_key=api_key, model=model, timeout_seconds=timeout_seconds)


def _append_tournament_fast_lookahead(body: dict[str, Any]) -> dict[str, Any]:
    """Append bounded-lookahead instructions to a fresh rich Turn body.

    The canonical request/hash and response schema are untouched. The helper
    mutates only the provider-bound prompt body built for this one dispatch.
    """

    try:
        part = body["contents"][0]["parts"][0]
        prompt = part["text"]
    except (KeyError, IndexError, TypeError):
        return body
    if not isinstance(prompt, str):
        return body
    part["text"] = f"{prompt}\n\n{_TOURNAMENT_FAST_LOOKAHEAD_POLICY}"
    return body


class TurnProviderTransport(Protocol):
    """Injectable transport boundary for production and fake implementations."""

    def send(
        self, request: TurnAdviceTransportRequest, config: ProviderConfig
    ) -> SanitizedProviderResult: ...


class GeminiTurnAdviceTransport:
    """Production Gemini transport for a single reviewed Turn Advice request.

    One call to :meth:`send` performs at most one HTTP request. There is no
    retry, resend, fallback model, or game-operation capability here.
    """

    def __init__(self, *, endpoint_template: str | None = None) -> None:
        self._endpoint_template = (
            endpoint_template
            or "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        )

    def send(
        self, request: TurnAdviceTransportRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        if isinstance(request, RichStateTurnAdviceRequest):
            body = _append_tournament_fast_lookahead(
                build_rich_provider_request_body(request)
            )
        else:
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
            with urllib.request.urlopen(  # noqa: S310 - fixed production HTTPS endpoint
                http_request, timeout=config.timeout_seconds
            ) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            raise ProviderTransportError(_sanitized_http_error_reason(exc)) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ProviderTransportError("GEMINI_TIMEOUT") from None
            raise ProviderTransportError("GEMINI_NETWORK_ERROR") from None
        except TimeoutError:
            raise ProviderTransportError("GEMINI_TIMEOUT") from None

        text = self._extract_text(raw_body)
        payload = self._parse_turn_payload(text)
        return SanitizedProviderResult(
            payload=payload,
            source_type=GEMINI_TURN_SOURCE_TYPE,
            model=config.model,
        )

    @staticmethod
    def _extract_text(raw_body: bytes) -> str:
        try:
            envelope = json.loads(raw_body.decode("utf-8"))
            text = envelope["candidates"][0]["content"]["parts"][0]["text"]
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            UnicodeDecodeError,
        ):
            raise ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED") from None
        if not isinstance(text, str):
            raise ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED")
        return text

    @staticmethod
    def _parse_turn_payload(text: str) -> dict[str, Any]:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed


@dataclass
class FakeTurnAdviceTransport:
    """Fake/injected transport. Records calls and never touches the network."""

    responses: list[SanitizedProviderResult | Exception] = field(default_factory=list)
    calls: list[tuple[TurnAdviceTransportRequest, ProviderConfig]] = field(
        default_factory=list
    )

    def send(
        self, request: TurnAdviceTransportRequest, config: ProviderConfig
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
