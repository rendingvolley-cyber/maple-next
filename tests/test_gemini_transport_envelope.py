from __future__ import annotations

import json
from typing import Any

import pytest

from maple_next.providers.selection_request import build_selection_advice_request
from maple_next.providers.transport import (
    GeminiSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
)

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def _request() -> object:
    return build_selection_advice_request(
        session_id="session-1",
        match_id="match-1",
        generation=1,
        battle_revision=1,
        reviewed_selection_id="reviewed-1",
        self_team=SELF_TEAM,
        opponent_team=OPPONENT_TEAM,
    )


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _envelope_bytes(text_value: Any) -> bytes:
    envelope = {
        "candidates": [{"content": {"parts": [{"text": text_value}]}}],
    }
    return json.dumps(envelope).encode("utf-8")


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    import maple_next.providers.transport as transport_module

    monkeypatch.setattr(
        transport_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(body),
    )


def test_outer_text_as_exact_string_parses_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = {"selected_three": ["Meowscarada", "Gholdengo", "Dragonite"], "lead": "Meowscarada"}
    _patch_urlopen(monkeypatch, _envelope_bytes(json.dumps(inner)))
    transport = GeminiSelectionAdviceTransport()
    result = transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]
    assert result.payload == inner
    assert result.source_type == "GEMINI"


@pytest.mark.parametrize(
    "text_value",
    [123, 1.5, True, None, {"nested": "object"}, ["a", "list"]],
)
def test_outer_text_not_a_string_is_rejected_as_malformed_envelope(
    monkeypatch: pytest.MonkeyPatch, text_value: object
) -> None:
    _patch_urlopen(monkeypatch, _envelope_bytes(text_value))
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="GEMINI_RESPONSE_ENVELOPE_MALFORMED"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


def test_missing_candidates_is_rejected_as_malformed_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, json.dumps({}).encode("utf-8"))
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="GEMINI_RESPONSE_ENVELOPE_MALFORMED"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


def test_raw_response_body_never_appears_in_the_raised_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "sk-should-never-leak-into-error-text"
    _patch_urlopen(monkeypatch, _envelope_bytes({"leaked": secret_marker}))
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError) as excinfo:
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]
    assert secret_marker not in str(excinfo.value)
