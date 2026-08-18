"""Gemini V2 Bundle 6: strict ``maple-turn-advice-response.v2`` schema tests.

Mirrors the accept/reject pattern established by
``tests/test_issue31_lane_c_turn_response.py`` for the v1 schema: a valid
body dict is deep-copied and mutated one field at a time, then either
parses successfully or raises :class:`TurnAdviceSchemaError`.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V2,
    TurnAdviceSchemaError,
    canonical_turn_advice_v2_json,
    turn_advice_body_v2_from_canonical_json,
    turn_advice_body_v2_from_dict,
)

VALID_PROVIDER_BODY_V2_DICT: dict[str, Any] = {
    "response_schema_version": RESPONSE_SCHEMA_VERSION_V2,
    "recommended_action": {
        "action_id": "move-1",
        "action_type": "MOVE",
        "action_name": "10まんボルト",
    },
    "recommendation_robustness": "HIGH",
    "reasons": ["確定情報から見て有利な選択"],
    "opponent_prediction": {
        "primary": {
            "category": "DAMAGING_MOVE",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "相手はダメージ技を選択すると予想",
        },
        "alternatives": [],
    },
    "warnings": [],
}


def _valid() -> dict[str, Any]:
    return copy.deepcopy(VALID_PROVIDER_BODY_V2_DICT)


def _valid_with_low_robustness_and_warning() -> dict[str, Any]:
    data = _valid()
    data["recommendation_robustness"] = "LOW"
    data["warnings"] = ["相手の交代先が未確定"]
    return data


# =========================================================================
# Accept
# =========================================================================


def test_valid_primary_only_parses_successfully() -> None:
    body = turn_advice_body_v2_from_dict(_valid())
    assert body.recommendation_robustness == "HIGH"
    assert body.opponent_prediction.primary.category == "DAMAGING_MOVE"
    assert body.opponent_prediction.alternatives == ()


def test_primary_plus_one_alternative_accepted() -> None:
    data = _valid()
    data["opponent_prediction"]["alternatives"] = [
        {
            "category": "SWITCH",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "交代の可能性も残る",
        }
    ]
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    data["opponent_prediction"]["primary"]["support_basis"] = "PINNED_RULES"
    body = turn_advice_body_v2_from_dict(data)
    assert len(body.opponent_prediction.alternatives) == 1


def test_primary_plus_two_alternatives_accepted() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    data["opponent_prediction"]["primary"]["support_basis"] = "PINNED_RULES"
    data["opponent_prediction"]["alternatives"] = [
        {
            "category": "SWITCH",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "交代の可能性も残る",
        },
        {
            "category": "NON_DAMAGING_MOVE",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "補助技の可能性も残る",
        },
    ]
    body = turn_advice_body_v2_from_dict(data)
    assert len(body.opponent_prediction.alternatives) == 2


def test_unknown_correct_combination_accepted() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"] = {
        "category": "UNKNOWN",
        "specific_action": None,
        "support_basis": "NONE",
        "support": "LOW",
        "summary": "判断材料が不足",
    }
    body = turn_advice_body_v2_from_dict(data)
    assert body.opponent_prediction.primary.category == "UNKNOWN"


def test_low_robustness_with_actionable_warning_accepted() -> None:
    body = turn_advice_body_v2_from_dict(_valid_with_low_robustness_and_warning())
    assert body.recommendation_robustness == "LOW"
    assert body.warnings == ("相手の交代先が未確定",)


def test_confirmed_match_support_basis_allows_high_support() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support_basis"] = "CONFIRMED_MATCH"
    data["opponent_prediction"]["primary"]["support"] = "HIGH"
    data["opponent_prediction"]["primary"]["specific_action"] = "れいとうビーム"
    body = turn_advice_body_v2_from_dict(data)
    assert body.opponent_prediction.primary.support == "HIGH"


def test_population_prior_medium_support_accepted() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support_basis"] = "POPULATION_PRIOR"
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    data["opponent_prediction"]["primary"]["specific_action"] = "れいとうビーム"
    body = turn_advice_body_v2_from_dict(data)
    assert body.opponent_prediction.primary.support_basis == "POPULATION_PRIOR"


# =========================================================================
# Reject: primary/alternatives structure
# =========================================================================


def test_rejects_more_than_two_alternatives() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    alt = {
        "category": "SWITCH",
        "specific_action": None,
        "support_basis": "NONE",
        "support": "LOW",
        "summary": "交代の可能性",
    }
    data["opponent_prediction"]["alternatives"] = [
        alt,
        dict(alt, summary="別の交代"),
        dict(alt, summary="さらに別"),
    ]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_primary_with_specific_action_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"] = {
        "category": "UNKNOWN",
        "specific_action": "れいとうビーム",
        "support_basis": "NONE",
        "support": "LOW",
        "summary": "不明",
    }
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_primary_with_alternative_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"] = {
        "category": "UNKNOWN",
        "specific_action": None,
        "support_basis": "NONE",
        "support": "LOW",
        "summary": "不明",
    }
    data["opponent_prediction"]["alternatives"] = [
        {
            "category": "SWITCH",
            "specific_action": None,
            "support_basis": "NONE",
            "support": "LOW",
            "summary": "交代",
        }
    ]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_with_non_none_basis_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"] = {
        "category": "UNKNOWN",
        "specific_action": None,
        "support_basis": "GENERAL_KNOWLEDGE",
        "support": "LOW",
        "summary": "不明",
    }
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_with_medium_support_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"] = {
        "category": "UNKNOWN",
        "specific_action": None,
        "support_basis": "NONE",
        "support": "MEDIUM",
        "summary": "不明",
    }
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


# =========================================================================
# Reject: support / support_basis
# =========================================================================


def test_general_knowledge_with_medium_support_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support_basis"] = "GENERAL_KNOWLEDGE"
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_population_prior_with_high_support_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support_basis"] = "POPULATION_PRIOR"
    data["opponent_prediction"]["primary"]["support"] = "HIGH"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_none_basis_on_non_unknown_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support_basis"] = "NONE"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_specific_action_with_low_support_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "LOW"
    data["opponent_prediction"]["primary"]["specific_action"] = "れいとうビーム"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


# =========================================================================
# Reject: cross-field
# =========================================================================


def test_alternative_stronger_than_primary_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "LOW"
    data["opponent_prediction"]["alternatives"] = [
        {
            "category": "SWITCH",
            "specific_action": "ガブリアス",
            "support_basis": "CONFIRMED_MATCH",
            "support": "HIGH",
            "summary": "交代の可能性",
        }
    ]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_duplicate_category_and_action_line_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    data["opponent_prediction"]["primary"]["support_basis"] = "PINNED_RULES"
    line = {
        "category": "SWITCH",
        "specific_action": None,
        "support_basis": "NONE",
        "support": "LOW",
        "summary": "交代A",
    }
    data["opponent_prediction"]["alternatives"] = [line, dict(line, summary="交代B")]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_duplicate_summary_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["support"] = "MEDIUM"
    data["opponent_prediction"]["primary"]["support_basis"] = "PINNED_RULES"
    data["opponent_prediction"]["alternatives"] = [
        {
            "category": "SWITCH",
            "specific_action": None,
            "support_basis": "NONE",
            "support": "LOW",
            "summary": "相手はダメージ技を選択すると予想",
        }
    ]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_low_robustness_without_warning_rejected() -> None:
    data = _valid()
    data["recommendation_robustness"] = "LOW"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


# =========================================================================
# Reject: limits / blanks / unknown fields
# =========================================================================


def test_more_than_two_reasons_rejected() -> None:
    data = _valid()
    data["reasons"] = ["理由1", "理由2", "理由3"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_zero_reasons_rejected() -> None:
    data = _valid()
    data["reasons"] = []
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_more_than_two_warnings_rejected() -> None:
    data = _valid()
    data["warnings"] = ["警告1", "警告2", "警告3"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_blank_reason_rejected() -> None:
    data = _valid()
    data["reasons"] = ["   "]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_oversized_summary_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["summary"] = "あ" * 401
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _valid()
    data["extra_field"] = "surprise"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_unknown_prediction_line_field_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["confidence"] = 0.5
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_invalid_category_enum_rejected() -> None:
    data = _valid()
    data["opponent_prediction"]["primary"]["category"] = "STATUS_OR_SETUP"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_wrong_response_schema_version_rejected() -> None:
    data = _valid()
    data["response_schema_version"] = "maple-turn-advice-response.v1"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_missing_field_rejected() -> None:
    data = _valid()
    del data["recommendation_robustness"]
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_source_type_in_body_rejected() -> None:
    data = _valid()
    data["source_type"] = "GEMINI"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


def test_model_in_body_rejected() -> None:
    data = _valid()
    data["model"] = "gemini-3.5"
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_dict(data)


# =========================================================================
# Canonical JSON round-trip
# =========================================================================


def test_canonical_json_round_trip_is_stable() -> None:
    body = turn_advice_body_v2_from_dict(_valid_with_low_robustness_and_warning())
    encoded_once = canonical_turn_advice_v2_json(body)
    decoded = turn_advice_body_v2_from_canonical_json(encoded_once)
    encoded_twice = canonical_turn_advice_v2_json(decoded)
    assert encoded_once == encoded_twice
    assert decoded == body


def test_canonical_json_uses_stable_key_ordering_and_no_whitespace() -> None:
    body = turn_advice_body_v2_from_dict(_valid())
    encoded = canonical_turn_advice_v2_json(body)
    assert ", " not in encoded
    assert ": " not in encoded
    assert encoded.index('"opponent_prediction"') < encoded.index('"reasons"')


def test_corrupt_canonical_json_fails_closed() -> None:
    with pytest.raises(TurnAdviceSchemaError):
        turn_advice_body_v2_from_canonical_json('{"response_schema_version": "bogus"}')
