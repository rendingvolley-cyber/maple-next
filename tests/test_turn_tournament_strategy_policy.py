from __future__ import annotations

from maple_next.providers.turn_request import (
    TOURNAMENT_TURN_STRATEGY_POLICY_VERSION,
    _render_provider_request_body_from_prompt,
)

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _provider_text(prompt: str) -> str:
    body = _render_provider_request_body_from_prompt(prompt, _SCHEMA)
    return body["contents"][0]["parts"][0]["text"]


def test_current_rich_turn_body_appends_tournament_strategy_policy() -> None:
    prompt = (
        "Rich prompt\n\nCanonical request:\n"
        '{"contract_version":"maple-turn-advice.v7","job_type":"TURN_ADVICE"}'
    )

    text = _provider_text(prompt)

    assert text.startswith(prompt)
    assert TOURNAMENT_TURN_STRATEGY_POLICY_VERSION in text
    assert "Compare the best MOVE against every legal SWITCH" in text
    assert "type disadvantage alone" in text
    assert "let the current active be lost" in text
    assert "reduce unnecessary switching" in text
    assert "low-value loops" in text


def test_current_mega_rich_turn_body_keeps_tournament_strategy_policy() -> None:
    prompt = (
        "Mega rich prompt\n\nCanonical request:\n"
        '{"contract_version":"maple-turn-advice.v8","job_type":"TURN_ADVICE"}'
    )

    text = _provider_text(prompt)

    assert TOURNAMENT_TURN_STRATEGY_POLICY_VERSION in text
    assert "Compare the best MOVE against every legal SWITCH" in text


def test_legacy_turn_body_is_byte_for_byte_unchanged() -> None:
    prompt = (
        "Legacy prompt\n\nCanonical request:\n"
        '{"contract_version":"maple-turn-advice.v2","job_type":"TURN_ADVICE"}'
    )

    assert _provider_text(prompt) == prompt


def test_policy_does_not_change_response_schema() -> None:
    prompt = (
        "Rich prompt\n\nCanonical request:\n"
        '{"contract_version":"maple-turn-advice.v7","job_type":"TURN_ADVICE"}'
    )
    body = _render_provider_request_body_from_prompt(prompt, _SCHEMA)

    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] is _SCHEMA
