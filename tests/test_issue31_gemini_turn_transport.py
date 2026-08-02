"""Production Turn Advice transport tests with a fully injected HTTP seam."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from maple_next.__main__ import build_turn_gemini_adapter
from maple_next.application.service import BattleApplication
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderConfigError,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.providers.turn_request import (
    REQUESTED_OUTPUT_SCHEMA,
    build_provider_prompt,
    build_provider_request_body,
)
from maple_next.providers.turn_transport import (
    GEMINI_TURN_SOURCE_TYPE,
    TURN_PROVIDER_AUTHORIZATION_ENV,
    GeminiTurnAdviceTransport,
    load_authorized_turn_provider_config_from_env,
)
from tests.fixtures.turn_advice import VALID_PROVIDER_BODY_DICT, build_sample_request


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _gemini_envelope(payload: object) -> bytes:
    return json.dumps(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
    ).encode("utf-8")


def test_turn_provider_prompt_and_body_are_deterministic_and_secret_free() -> None:
    request = build_sample_request()
    prompt = build_provider_prompt(request)
    body = build_provider_request_body(request)

    assert prompt == build_provider_prompt(build_sample_request())
    assert "legal_actions" in prompt
    assert "Do not execute" in prompt
    assert "api_key" not in prompt.lower()
    assert body["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseJsonSchema": REQUESTED_OUTPUT_SCHEMA,
    }


def test_authorization_is_separate_from_api_key_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "present-but-not-authorized")
    monkeypatch.delenv(TURN_PROVIDER_AUTHORIZATION_ENV, raising=False)

    with pytest.raises(ProviderConfigError, match="GEMINI_TURN_NOT_AUTHORIZED"):
        load_authorized_turn_provider_config_from_env()


def test_authorized_config_loads_only_after_exact_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TURN_PROVIDER_AUTHORIZATION_ENV, "1")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_MODEL", "test-model")

    config = load_authorized_turn_provider_config_from_env()

    assert config.api_key == "test-key"
    assert config.model == "test-model"


def test_official_adapter_is_production_and_unauthorized_send_stops_before_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TURN_PROVIDER_AUTHORIZATION_ENV, raising=False)
    adapter = build_turn_gemini_adapter()
    failures: list[str] = []

    adapter.send(
        cast(BattleApplication, object()),
        on_applied=lambda _result: pytest.fail("result must not apply"),
        on_failed=failures.append,
    )

    assert adapter.uses_injected_transport is False
    assert adapter.dispatch_count == 0
    assert adapter.network_call_count == 0
    assert failures == ["GEMINI_TURN_NOT_AUTHORIZED"]


def test_production_adapter_rejects_injected_response() -> None:
    adapter = build_turn_gemini_adapter()
    with pytest.raises(RuntimeError, match="DOES_NOT_ACCEPT"):
        adapter.enqueue_response(
            SanitizedProviderResult(payload={}, source_type="GEMINI", model="test")
        )


def test_transport_posts_exactly_once_and_parses_strict_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[urllib.request.Request, float]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        calls.append((request, timeout))
        return _Response(_gemini_envelope(VALID_PROVIDER_BODY_DICT))

    monkeypatch.setattr("maple_next.providers.turn_transport.urllib.request.urlopen", fake_urlopen)
    transport = GeminiTurnAdviceTransport()
    result = transport.send(
        build_sample_request(),
        ProviderConfig(api_key="secret-test-key", model="test-model", timeout_seconds=7.0),
    )

    assert len(calls) == 1
    request, timeout = calls[0]
    assert timeout == 7.0
    assert request.method == "POST"
    assert request.headers["X-goog-api-key"] == "secret-test-key"
    sent_body = json.loads(cast(bytes, request.data).decode("utf-8"))
    assert sent_body == build_provider_request_body(build_sample_request())
    assert "secret-test-key" not in cast(bytes, request.data).decode("utf-8")
    assert result.payload == VALID_PROVIDER_BODY_DICT
    assert result.source_type == GEMINI_TURN_SOURCE_TYPE
    assert result.model == "test-model"


@pytest.mark.parametrize(
    "inner",
    ["not-json", [], "scalar"],
)
def test_non_object_inner_payload_fails_closed_to_empty_payload(
    monkeypatch: pytest.MonkeyPatch, inner: Any
) -> None:
    text = inner if isinstance(inner, str) else json.dumps(inner)
    envelope = json.dumps(
        {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    ).encode("utf-8")
    monkeypatch.setattr(
        "maple_next.providers.turn_transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(envelope),
    )

    result = GeminiTurnAdviceTransport().send(
        build_sample_request(), ProviderConfig(api_key="secret", model="m")
    )

    assert result.payload == {}


def test_network_failure_is_sanitized_and_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail_once(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("raw-host-and-secret-marker")

    monkeypatch.setattr("maple_next.providers.turn_transport.urllib.request.urlopen", fail_once)

    with pytest.raises(ProviderTransportError) as captured:
        GeminiTurnAdviceTransport().send(
            build_sample_request(), ProviderConfig(api_key="secret", model="m")
        )

    assert calls == 1
    assert str(captured.value) == "GEMINI_NETWORK_ERROR"
    assert "raw-host" not in str(captured.value)


def test_malformed_outer_envelope_never_leaks_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_marker = "raw-provider-secret-marker"
    monkeypatch.setattr(
        "maple_next.providers.turn_transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(secret_marker.encode("utf-8")),
    )

    with pytest.raises(ProviderTransportError) as captured:
        GeminiTurnAdviceTransport().send(
            build_sample_request(), ProviderConfig(api_key="secret", model="m")
        )

    assert str(captured.value) == "GEMINI_RESPONSE_ENVELOPE_MALFORMED"
    assert secret_marker not in str(captured.value)
    assert captured.value.__cause__ is None
