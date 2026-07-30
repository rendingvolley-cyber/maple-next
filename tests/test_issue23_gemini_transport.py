from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from maple_next.providers.selection_request import (
    REQUESTED_OUTPUT_SCHEMA,
    build_provider_request_body,
    build_selection_advice_request,
    request_payload_hash,
)
from maple_next.providers.transport import (
    GeminiSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
)

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)


def _request() -> Any:
    return build_selection_advice_request(
        session_id="session-1",
        match_id="match-1",
        generation=1,
        battle_revision=1,
        reviewed_selection_id="reviewed-1",
        self_team=SELF_TEAM,
        opponent_team=OPPONENT_TEAM,
    )


def _http_error(body: bytes, *, code: int = 400) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid",
        code,
        "provider message must not escape",
        {"x-secret-header": "header-secret"},
        io.BytesIO(body),
    )


def _raise_http_error(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    *,
    code: int = 400,
) -> str:
    import maple_next.providers.transport as transport_module

    def raise_it(*_args: object, **_kwargs: object) -> object:
        raise _http_error(body, code=code)

    monkeypatch.setattr(transport_module.urllib.request, "urlopen", raise_it)
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError) as excinfo:
        transport.send(_request(), ProviderConfig(api_key="synthetic-secret-key"))
    return str(excinfo.value)


def test_provider_body_uses_raw_json_schema_field_without_changing_canonical_hash() -> None:
    request = _request()
    body = build_provider_request_body(request)
    generation_config = body["generationConfig"]

    assert "responseJsonSchema" in generation_config
    assert "responseSchema" not in generation_config
    assert generation_config["responseJsonSchema"] == REQUESTED_OUTPUT_SCHEMA
    assert generation_config["responseJsonSchema"]["type"] == "object"
    selected_three = generation_config["responseJsonSchema"]["properties"][
        "selected_three"
    ]
    assert selected_three["type"] == "array"
    assert selected_three["items"] == {"type": "string"}
    assert selected_three["minItems"] == 3
    assert selected_three["maxItems"] == 3
    assert generation_config["responseJsonSchema"]["additionalProperties"] is False
    assert request_payload_hash(request) == (
        "b8523e4d386cc0447d6cb689b103cbe7c14f73defa2418f8ef5a5e28c01b64d1"
    )


def test_invalid_argument_is_safely_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "raw-secret",
            }
        }
    ).encode()
    reason = _raise_http_error(monkeypatch, body)
    assert reason == "GEMINI_HTTP_ERROR:400|STATUS=INVALID_ARGUMENT"
    assert "raw-secret" not in reason


def test_api_key_invalid_errorinfo_is_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "key value must never escape",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                        "metadata": {
                            "service": "generativelanguage.googleapis.com",
                            "consumer": "projects/secret-consumer",
                        },
                    }
                ],
            }
        }
    ).encode()
    reason = _raise_http_error(monkeypatch, body)
    assert reason == (
        "GEMINI_HTTP_ERROR:400|STATUS=INVALID_ARGUMENT|REASON=API_KEY_INVALID|"
        "DOMAIN=googleapis.com|SERVICE=generativelanguage.googleapis.com"
    )
    assert "key value" not in reason
    assert "secret-consumer" not in reason


def test_failed_precondition_is_safely_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "error": {
                "status": "FAILED_PRECONDITION",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "FAILED_PRECONDITION",
                        "domain": "googleapis.com",
                        "metadata": {
                            "service": "generativelanguage.googleapis.com"
                        },
                    }
                ],
            }
        }
    ).encode()
    reason = _raise_http_error(monkeypatch, body)
    assert "STATUS=FAILED_PRECONDITION" in reason
    assert "REASON=FAILED_PRECONDITION" in reason


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"x" * 16_385,
        json.dumps({"error": {"details": "not-a-list"}}).encode(),
        json.dumps({"error": {"details": ["not-an-object"]}}).encode(),
        json.dumps({"unexpected": "shape"}).encode(),
    ],
)
def test_unknown_or_unsafe_http_error_body_fails_closed_to_generic(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    assert _raise_http_error(monkeypatch, body) == "GEMINI_HTTP_ERROR:400"


def test_unallowlisted_values_and_request_secrets_never_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers = [
        "human-message-secret",
        "arbitrary-metadata-secret",
        "header-secret",
        "synthetic-secret-key",
        "Your own confirmed team",
    ]
    body = json.dumps(
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": markers[0],
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                        "metadata": {
                            "service": "generativelanguage.googleapis.com",
                            "other": markers[1],
                        },
                    }
                ],
            }
        }
    ).encode()
    reason = _raise_http_error(monkeypatch, body)
    for marker in markers:
        assert marker not in reason
