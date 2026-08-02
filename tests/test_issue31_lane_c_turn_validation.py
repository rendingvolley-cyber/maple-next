from __future__ import annotations

import dataclasses

import pytest

from maple_next.providers.turn_request import request_payload_hash
from maple_next.providers.turn_validation import (
    TurnAdviceParseError,
    TurnAdviceResultCode,
    build_normalized_turn_advice_result,
    parse_turn_advice_body,
    sanitized_reason_for,
    validate_turn_advice_binding,
    validate_turn_advice_legality,
    validate_turn_advice_result,
)
from tests.fixtures.turn_advice import (
    TRUSTED_MODEL,
    TRUSTED_SOURCE_TYPE,
    VALID_PROVIDER_TEXT,
    build_sample_request,
)


def _build_valid_result(request=None):  # type: ignore[no-untyped-def]
    request = request or build_sample_request()
    body = parse_turn_advice_body(VALID_PROVIDER_TEXT)
    result = build_normalized_turn_advice_result(
        request=request,
        body=body,
        request_payload_hash_value=request_payload_hash(request),
        source_type=TRUSTED_SOURCE_TYPE,
        model=TRUSTED_MODEL,
    )
    return request, result


# --- parse_turn_advice_body -------------------------------------------------


def test_parse_rejects_non_json_text() -> None:
    with pytest.raises(TurnAdviceParseError, match="TURN_ADVICE_INVALID_JSON"):
        parse_turn_advice_body("not json at all")


def test_parse_rejects_markdown_wrapped_json() -> None:
    wrapped = "```json\n" + VALID_PROVIDER_TEXT + "\n```"
    with pytest.raises(TurnAdviceParseError, match="TURN_ADVICE_INVALID_JSON"):
        parse_turn_advice_body(wrapped)


def test_parse_rejects_top_level_json_array() -> None:
    with pytest.raises(TurnAdviceParseError, match="TURN_ADVICE_INVALID_JSON"):
        parse_turn_advice_body("[1, 2, 3]")


def test_parse_rejects_schema_violation_with_sanitized_reason() -> None:
    with pytest.raises(TurnAdviceParseError, match="TURN_ADVICE_SCHEMA_REJECTED"):
        parse_turn_advice_body('{"unexpected": true}')


def test_parse_accepts_valid_text() -> None:
    body = parse_turn_advice_body(VALID_PROVIDER_TEXT)
    assert body.recommended_action.action_id == "move-1"


def test_parse_error_message_never_contains_raw_provider_text() -> None:
    raw = "some raw provider garbage that must never leak: SECRET_TOKEN_XYZ"
    try:
        parse_turn_advice_body(raw)
    except TurnAdviceParseError as exc:
        assert "SECRET_TOKEN_XYZ" not in str(exc)
        assert str(exc) == "TURN_ADVICE_INVALID_JSON"
    else:  # pragma: no cover
        pytest.fail("expected TurnAdviceParseError")


# --- build_normalized_turn_advice_result ------------------------------------


def test_build_normalized_result_rejects_empty_source_type() -> None:
    request = build_sample_request()
    body = parse_turn_advice_body(VALID_PROVIDER_TEXT)
    with pytest.raises(ValueError, match="TURN_ADVICE_SOURCE_INVALID"):
        build_normalized_turn_advice_result(
            request=request,
            body=body,
            request_payload_hash_value=request_payload_hash(request),
            source_type="",
            model=TRUSTED_MODEL,
        )


def test_build_normalized_result_rejects_empty_model() -> None:
    request = build_sample_request()
    body = parse_turn_advice_body(VALID_PROVIDER_TEXT)
    with pytest.raises(ValueError, match="TURN_ADVICE_MODEL_INVALID"):
        build_normalized_turn_advice_result(
            request=request,
            body=body,
            request_payload_hash_value=request_payload_hash(request),
            source_type=TRUSTED_SOURCE_TYPE,
            model="",
        )


def test_normalized_result_carries_only_trusted_caller_supplied_source_and_model() -> None:
    _, result = _build_valid_result()
    assert result.source_type == TRUSTED_SOURCE_TYPE
    assert result.model == TRUSTED_MODEL


# --- validate_turn_advice_legality ------------------------------------------


def test_legality_valid_for_exact_triple_match() -> None:
    request, result = _build_valid_result()
    assert validate_turn_advice_legality(request, result) is TurnAdviceResultCode.VALID


def test_legality_rejects_unknown_action_id() -> None:
    request, result = _build_valid_result()
    bad_action = dataclasses.replace(result.advice.recommended_action, action_id="not-a-real-id")
    bad_advice = dataclasses.replace(result.advice, recommended_action=bad_action)
    bad_result = dataclasses.replace(result, advice=bad_advice)
    assert validate_turn_advice_legality(request, bad_result) is TurnAdviceResultCode.ILLEGAL_ACTION


def test_legality_rejects_id_matching_but_name_mismatched() -> None:
    request, result = _build_valid_result()
    bad_action = dataclasses.replace(result.advice.recommended_action, action_name="Wrong Name")
    bad_advice = dataclasses.replace(result.advice, recommended_action=bad_action)
    bad_result = dataclasses.replace(result, advice=bad_advice)
    assert validate_turn_advice_legality(request, bad_result) is TurnAdviceResultCode.ILLEGAL_ACTION


def test_legality_rejects_id_matching_but_type_mismatched() -> None:
    request, result = _build_valid_result()
    # move-1 exists only as a MOVE; claiming it is a SWITCH must not match.
    bad_action = dataclasses.replace(result.advice.recommended_action, action_type="SWITCH")
    bad_advice = dataclasses.replace(result.advice, recommended_action=bad_action)
    bad_result = dataclasses.replace(result, advice=bad_advice)
    assert validate_turn_advice_legality(request, bad_result) is TurnAdviceResultCode.ILLEGAL_ACTION


def test_legality_rejects_switch_target_not_in_selected_three() -> None:
    from maple_next.domain.enums import ActionType
    from maple_next.providers.turn_request import LegalAction

    switch_action = LegalAction(
        action_id="switch-1",
        action_type=ActionType.SWITCH,
        action_name="Dragonite",
        switch_target="Dragonite",
    )
    request = build_sample_request(legal_actions=(switch_action,))
    body = parse_turn_advice_body(
        VALID_PROVIDER_TEXT.replace(
            '"action_id":"move-1","action_type":"MOVE","action_name":"Make It Rain"',
            '"action_id":"switch-1","action_type":"SWITCH","action_name":"Dragonite"',
        )
    )
    result = build_normalized_turn_advice_result(
        request=request,
        body=body,
        request_payload_hash_value=request_payload_hash(request),
        source_type=TRUSTED_SOURCE_TYPE,
        model=TRUSTED_MODEL,
    )
    # Legitimately legal here; now corrupt the request's own legal action
    # in-memory via object.__setattr__ (never reachable through the public
    # constructor, since TurnAdviceRequest.__post_init__ rejects this shape
    # on every construction, including dataclasses.replace) to prove the
    # validator itself re-checks ownership defensively rather than trusting
    # the request blindly.
    corrupted_action = dataclasses.replace(switch_action, switch_target="Urshifu")
    object.__setattr__(request, "legal_actions", (corrupted_action,))
    assert (
        validate_turn_advice_legality(request, result) is TurnAdviceResultCode.NON_OWNED_ACTION
    )


def test_legality_rejects_move_owner_mismatch_via_corrupted_request() -> None:
    request, result = _build_valid_result()
    recommended_id = result.advice.recommended_action.action_id
    move_action = next(a for a in request.legal_actions if a.action_id == recommended_id)
    corrupted_action = dataclasses.replace(move_action, owner_active="Dragonite")
    other_actions = tuple(
        a for a in request.legal_actions if a.action_id != move_action.action_id
    )
    # See the note in the SWITCH-ownership test above: this shape is
    # rejected by TurnAdviceRequest.__post_init__ on any normal
    # construction, so we bypass the frozen dataclass via object.__setattr__
    # to simulate a corrupted request reaching the validator.
    object.__setattr__(request, "legal_actions", (corrupted_action, *other_actions))
    assert (
        validate_turn_advice_legality(request, result) is TurnAdviceResultCode.NON_OWNED_ACTION
    )


def test_legality_never_auto_corrects_or_substitutes() -> None:
    request, result = _build_valid_result()
    bad_action = dataclasses.replace(result.advice.recommended_action, action_id="not-real")
    bad_advice = dataclasses.replace(result.advice, recommended_action=bad_action)
    bad_result = dataclasses.replace(result, advice=bad_advice)
    code = validate_turn_advice_legality(request, bad_result)
    assert code is TurnAdviceResultCode.ILLEGAL_ACTION
    # The recommended action in the (rejected) result is untouched.
    assert bad_result.advice.recommended_action.action_id == "not-real"


# --- validate_turn_advice_binding / validate_turn_advice_result -------------


@pytest.mark.parametrize(
    ("field", "new_value", "expected_code"),
    [
        ("session_id", "other-session", TurnAdviceResultCode.SESSION_MISMATCH),
        ("match_id", "other-match", TurnAdviceResultCode.MATCH_MISMATCH),
        ("generation", 999, TurnAdviceResultCode.GENERATION_MISMATCH),
        ("turn_number", 999, TurnAdviceResultCode.TURN_MISMATCH),
        ("battle_revision", 999, TurnAdviceResultCode.REVISION_MISMATCH),
        ("reviewed_snapshot_id", "other-board", TurnAdviceResultCode.SNAPSHOT_ID_MISMATCH),
        (
            "reviewed_snapshot_hash",
            "0" * 64,
            TurnAdviceResultCode.SNAPSHOT_HASH_MISMATCH,
        ),
        (
            "request_payload_hash",
            "1" * 64,
            TurnAdviceResultCode.REQUEST_HASH_MISMATCH,
        ),
        ("job_type", "SELECTION_ADVICE", TurnAdviceResultCode.STALE_REJECTED),
        ("contract_version", "maple-turn-advice.v2", TurnAdviceResultCode.STALE_REJECTED),
    ],
)
def test_binding_rejects_each_mismatched_field_independently(
    field: str, new_value: object, expected_code: TurnAdviceResultCode
) -> None:
    request, result = _build_valid_result()
    mutated = dataclasses.replace(result, **{field: new_value})

    # Pure functions: mutation-by-replace produces a *new* object; the
    # original request and result are untouched.
    original_request_hash = request_payload_hash(request)

    code = validate_turn_advice_binding(request, mutated)
    assert code is expected_code

    # Confirm purity: neither validation call mutated shared state.
    assert request_payload_hash(request) == original_request_hash
    assert result.session_id == "session-1"


def test_binding_valid_when_everything_matches() -> None:
    request, result = _build_valid_result()
    assert validate_turn_advice_binding(request, result) is TurnAdviceResultCode.VALID


def test_validate_turn_advice_result_composes_binding_then_legality() -> None:
    request, result = _build_valid_result()
    assert validate_turn_advice_result(request, result) is TurnAdviceResultCode.VALID

    stale = dataclasses.replace(result, session_id="other-session")
    assert validate_turn_advice_result(request, stale) is TurnAdviceResultCode.SESSION_MISMATCH

    bad_action = dataclasses.replace(result.advice.recommended_action, action_id="not-real")
    bad_advice = dataclasses.replace(result.advice, recommended_action=bad_action)
    illegal = dataclasses.replace(result, advice=bad_advice)
    assert validate_turn_advice_result(request, illegal) is TurnAdviceResultCode.ILLEGAL_ACTION


def test_sanitized_reason_never_leaks_raw_text_and_covers_every_code() -> None:
    for code in TurnAdviceResultCode:
        reason = sanitized_reason_for(code)
        assert reason.startswith("TURN_ADVICE_")


def test_raw_provider_text_is_not_retained_anywhere_on_result() -> None:
    _, result = _build_valid_result()
    for field_name in result.__dataclass_fields__:
        value = getattr(result, field_name)
        if isinstance(value, str):
            assert value != VALID_PROVIDER_TEXT
    advice = result.advice
    for field_name in advice.__dataclass_fields__:
        value = getattr(advice, field_name)
        if isinstance(value, str):
            assert VALID_PROVIDER_TEXT not in value
