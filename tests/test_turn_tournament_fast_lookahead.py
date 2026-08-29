from __future__ import annotations

import pytest

from maple_next.providers.turn_transport import (
    DEFAULT_TURN_MODEL,
    TURN_PROVIDER_AUTHORIZATION_ENV,
    _append_tournament_fast_lookahead,
    load_authorized_turn_provider_config_from_env,
)


def test_fast_lookahead_requires_bounded_2_to_3_turn_read_and_one_conclusion() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "base rich prompt"}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    result = _append_tournament_fast_lookahead(body)
    prompt = result["contents"][0]["parts"][0]["text"]

    assert "roughly 2-3 turns" in prompt
    assert "1-2 opponent replies" in prompt
    assert "Do not attempt exhaustive multi-turn tree search" in prompt
    assert "single best legal" in prompt
    assert "never abstain" in prompt
    assert "Do not output the simulated line or hidden chain-of-thought" in prompt


def _authorized_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TURN_PROVIDER_AUTHORIZATION_ENV, "1")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "test-only-key")
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_TURN_MODEL", raising=False)


def test_tournament_turn_timeout_defaults_to_15_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_env(monkeypatch)
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS", raising=False)

    config = load_authorized_turn_provider_config_from_env()

    assert config.model == DEFAULT_TURN_MODEL == "gemini-3.5-flash-lite"
    assert config.timeout_seconds == 15.0


def test_tournament_turn_timeout_caps_older_30_second_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_env(monkeypatch)
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS", "30")

    config = load_authorized_turn_provider_config_from_env()

    assert config.timeout_seconds == 15.0


def test_tournament_turn_timeout_preserves_stricter_operator_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_env(monkeypatch)
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS", "10")

    config = load_authorized_turn_provider_config_from_env()

    assert config.timeout_seconds == 10.0
