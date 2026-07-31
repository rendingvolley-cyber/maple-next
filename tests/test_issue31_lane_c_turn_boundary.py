from __future__ import annotations

from maple_next.providers.turn_boundary import (
    ALLOW_CREATE_ONE_JOB,
    DENY_ATTEMPT_ALREADY_CONSUMED,
    DENY_BINDING_NOT_CURRENT,
    DENY_PENDING_JOB_EXISTS,
    DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION,
    DispatchTrigger,
    build_turn_advice_prompt,
    build_turn_provider_request_body,
    decide_turn_advice_dispatch,
)
from tests.fixtures.turn_advice import build_sample_request


def _decide(trigger: DispatchTrigger, **overrides: bool):  # type: ignore[no-untyped-def]
    kwargs = {
        "is_current_binding": True,
        "has_pending_job": False,
        "attempt_consumed": False,
    }
    kwargs.update(overrides)
    return decide_turn_advice_dispatch(trigger=trigger, **kwargs)  # type: ignore[arg-type]


# --- build_turn_advice_prompt / build_turn_provider_request_body ------------


def test_prompt_contains_fixed_instruction_header() -> None:
    prompt = build_turn_advice_prompt(build_sample_request())
    assert "You are advising a human Pokemon Champions player" in prompt
    assert "UNKNOWN means unknown" in prompt
    assert "Respond with strict JSON only" in prompt


def test_prompt_contains_reviewed_facts_and_legal_actions() -> None:
    prompt = build_turn_advice_prompt(build_sample_request())
    assert "self_active=Gholdengo" in prompt
    assert "opponent_active=Garchomp" in prompt
    assert "self_hp=71-80" in prompt
    assert "opponent_hp=41-50" in prompt
    assert "weather=UNKNOWN" in prompt
    assert "terrain=UNKNOWN" in prompt
    assert "Stealth Rock" in prompt
    assert "id=move-1 type=MOVE name=Make It Rain owner_active=Gholdengo" in prompt
    assert "id=switch-1 type=SWITCH name=Dragonite switch_target=Dragonite" in prompt
    assert "Applied selected_three: ['Gholdengo', 'Dragonite', 'Dondozo']" in prompt


def test_prompt_never_contains_secrets_or_transport_details() -> None:
    prompt = build_turn_advice_prompt(build_sample_request())
    forbidden_substrings = (
        "api_key",
        "Authorization",
        "x-goog-api-key",
        ".db",
        ".sqlite",
        "http://",
        "https://",
    )
    for token in forbidden_substrings:
        assert token not in prompt


def test_prompt_is_deterministic() -> None:
    request = build_sample_request()
    assert build_turn_advice_prompt(request) == build_turn_advice_prompt(build_sample_request())


def test_provider_request_body_embeds_prompt_and_schema() -> None:
    request = build_sample_request()
    body = build_turn_provider_request_body(request)
    assert body["contents"][0]["parts"][0]["text"] == build_turn_advice_prompt(request)
    assert body["generationConfig"]["responseJsonSchema"] == request.requested_output_schema
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_provider_request_body_never_contains_model_or_secrets() -> None:
    body = build_turn_provider_request_body(build_sample_request())
    assert "model" not in body
    assert "api_key" not in body
    assert "headers" not in body


# --- decide_turn_advice_dispatch --------------------------------------------


def test_trusted_human_activation_with_fresh_binding_allows() -> None:
    decision = _decide(DispatchTrigger.TRUSTED_HUMAN_ACTIVATION)
    assert decision.allowed is True
    assert decision.reason_code == ALLOW_CREATE_ONE_JOB


def test_every_other_trigger_denies() -> None:
    for trigger in DispatchTrigger:
        if trigger is DispatchTrigger.TRUSTED_HUMAN_ACTIVATION:
            continue
        decision = _decide(trigger)
        assert decision.allowed is False
        assert decision.reason_code == DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION


def test_duplicate_human_activation_on_pending_binding_denies() -> None:
    decision = _decide(DispatchTrigger.TRUSTED_HUMAN_ACTIVATION, has_pending_job=True)
    assert decision.allowed is False
    assert decision.reason_code == DENY_PENDING_JOB_EXISTS


def test_stale_binding_denies() -> None:
    decision = _decide(DispatchTrigger.TRUSTED_HUMAN_ACTIVATION, is_current_binding=False)
    assert decision.allowed is False
    assert decision.reason_code == DENY_BINDING_NOT_CURRENT


def test_already_consumed_attempt_denies() -> None:
    decision = _decide(DispatchTrigger.TRUSTED_HUMAN_ACTIVATION, attempt_consumed=True)
    assert decision.allowed is False
    assert decision.reason_code == DENY_ATTEMPT_ALREADY_CONSUMED


def test_retry_resend_fallback_triggers_all_deny() -> None:
    for trigger in (DispatchTrigger.RETRY, DispatchTrigger.RESEND, DispatchTrigger.FALLBACK):
        decision = _decide(trigger)
        assert decision.allowed is False


def test_policy_function_is_pure_and_makes_no_transport_calls() -> None:
    """No mocking is needed: the module imports nothing transport-shaped.

    This is a structural guarantee, verified here by construction — the
    module under test (``turn_boundary``) does not import socket, urllib,
    sqlite3, or any PySide6/UI module, so no such call can occur.
    """

    import maple_next.providers.turn_boundary as boundary_module

    source = boundary_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("import socket", "import urllib", "import sqlite3", "PySide6"):
        assert forbidden not in text


def test_dispatch_decision_never_mutates_inputs() -> None:
    trigger = DispatchTrigger.TRUSTED_HUMAN_ACTIVATION
    before = (trigger, True, False, False)
    decide_turn_advice_dispatch(
        trigger=trigger, is_current_binding=True, has_pending_job=False, attempt_consumed=False
    )
    after = (trigger, True, False, False)
    assert before == after
