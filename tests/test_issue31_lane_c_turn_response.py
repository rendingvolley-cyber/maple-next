from __future__ import annotations

import copy

import pytest

from maple_next.providers.turn_response import (
    NormalizedTurnAdviceResult,
    TurnAdviceSchemaError,
    turn_advice_body_from_dict,
)
from tests.fixtures.turn_advice import VALID_PROVIDER_BODY_DICT, build_sample_request


def _valid() -> dict[str, object]:
    return copy.deepcopy(VALID_PROVIDER_BODY_DICT)


def test_valid_body_parses_successfully() -> None:
    body = turn_advice_body_from_dict(_valid())
    assert body.recommended_action.action_id == "move-1"
    assert body.recommended_action.action_type == "MOVE"
    assert body.recommended_action.action_name == "Make It Rain"
    assert len(body.reasons) == 2
    assert len(body.warnings) == 1
    assert body.opponent_prediction.category == "MOVE"
    assert body.opponent_prediction.predicted_action is None
    assert body.opponent_prediction.confidence == 0.6


def test_rejects_extra_top_level_field() -> None:
    data = _valid()
    data["extra_field"] = "surprise"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_source_type_and_model_spoofed_in_provider_body() -> None:
    for spoofed_key, spoofed_value in (("source_type", "GEMINI"), ("model", "gemini-2.5-flash")):
        data = _valid()
        data[spoofed_key] = spoofed_value
        with pytest.raises(TurnAdviceSchemaError):
            turn_advice_body_from_dict(data)


def test_rejects_extra_nested_field_in_recommended_action() -> None:
    data = _valid()
    data["recommended_action"]["extra"] = "nope"  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_extra_nested_field_in_opponent_prediction() -> None:
    data = _valid()
    data["opponent_prediction"]["extra"] = "nope"  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_top_level_array() -> None:
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict([_valid()])


def test_rejects_missing_field() -> None:
    data = _valid()
    del data["warnings"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_missing_nested_field() -> None:
    data = _valid()
    del data["recommended_action"]["action_name"]  # type: ignore[arg-type]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_wrong_type_for_reasons() -> None:
    data = _valid()
    data["reasons"] = "not a list"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_empty_reason_string() -> None:
    data = _valid()
    data["reasons"] = ["   "]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_too_many_reasons() -> None:
    data = _valid()
    data["reasons"] = ["a", "b", "c", "d"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_zero_reasons() -> None:
    data = _valid()
    data["reasons"] = []
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_invalid_warning_type() -> None:
    data = _valid()
    data["warnings"] = [123]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_too_many_warnings() -> None:
    data = _valid()
    data["warnings"] = ["a", "b", "c", "d", "e", "f"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_confidence_out_of_range_high() -> None:
    data = _valid()
    data["opponent_prediction"]["confidence"] = 1.5  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_confidence_out_of_range_low() -> None:
    data = _valid()
    data["opponent_prediction"]["confidence"] = -0.1  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_confidence_as_bool() -> None:
    data = _valid()
    data["opponent_prediction"]["confidence"] = True  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_invalid_opponent_category() -> None:
    data = _valid()
    data["opponent_prediction"]["category"] = "SOMETHING_ELSE"  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_invalid_action_type() -> None:
    data = _valid()
    data["recommended_action"]["action_type"] = "MEGA_EVOLVE"  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_rejects_empty_action_name() -> None:
    data = _valid()
    data["recommended_action"]["action_name"] = ""  # type: ignore[index]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_from_dict(data)


def test_normalized_result_requires_non_empty_source_type_and_model() -> None:
    request = build_sample_request()
    body = turn_advice_body_from_dict(_valid())
    with pytest.raises(ValueError):
        NormalizedTurnAdviceResult(
            contract_version=request.contract_version,
            job_type=request.job_type,
            session_id=request.session_id,
            match_id=request.match_id,
            generation=request.generation,
            turn_number=request.turn_number,
            battle_revision=request.battle_revision,
            reviewed_snapshot_id=request.reviewed_snapshot_id,
            reviewed_snapshot_hash=request.reviewed_snapshot_hash,
            request_payload_hash="deadbeef",
            source_type="",
            model="gemini-2.5-flash",
            advice=body,
        )


def test_normalized_result_never_carries_a_raw_text_field() -> None:
    request = build_sample_request()
    body = turn_advice_body_from_dict(_valid())
    result = NormalizedTurnAdviceResult(
        contract_version=request.contract_version,
        job_type=request.job_type,
        session_id=request.session_id,
        match_id=request.match_id,
        generation=request.generation,
        turn_number=request.turn_number,
        battle_revision=request.battle_revision,
        reviewed_snapshot_id=request.reviewed_snapshot_id,
        reviewed_snapshot_hash=request.reviewed_snapshot_hash,
        request_payload_hash="deadbeef",
        source_type="GEMINI",
        model="gemini-2.5-flash",
        advice=body,
    )
    field_names = {f for f in result.__dataclass_fields__}
    assert "raw_text" not in field_names
    assert "raw_provider_text" not in field_names
    assert "provider_text" not in field_names
