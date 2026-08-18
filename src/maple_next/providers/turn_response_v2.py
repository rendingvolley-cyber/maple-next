"""Strict Turn Advice provider-response schema v2 (Gemini V2 Bundle 6).

Additive sibling of ``turn_response.py``. The v1 schema, dataclasses, and
parser in that module are never mutated or repointed here — this module
defines an independent ``maple-turn-advice-response.v2`` contract with its
own strict, ``additionalProperties: false`` schema and its own parser
(:func:`turn_advice_body_v2_from_dict`).

``RecommendedAction`` (and the shared low-level primitives
``TurnAdviceSchemaError`` / ``_require_dict`` / ``_require_non_empty_str`` /
``_reject_unknown_keys`` / ``_reasons_from_list`` / ``_warnings_from_list`` /
``_recommended_action_from_dict``) are imported, never redefined, from
``turn_response.py`` — the recommended-action shape is identical between v1
and v2 by design, so the existing exact three-field legality validation
(:func:`maple_next.providers.turn_validation.validate_turn_advice_legality`)
applies unchanged to both.

This module performs pure schema validation only. Request-aware semantic
checks (exact-move/switch-target source membership, alternative-vs-primary
support ordering against the live request) live in
``turn_response_v2_semantics.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from maple_next.providers.turn_response import (
    ACTION_ID_MAX_LENGTH,
    ACTION_NAME_MAX_LENGTH,
    RECOMMENDED_ACTION_ALLOWED_KEYS,
    RecommendedAction,
    TurnAdviceSchemaError,
    _reasons_from_list,
    _recommended_action_from_dict,
    _reject_unknown_keys,
    _require_dict,
    _require_non_empty_str,
    _warnings_from_list,
)

__all__ = [
    "ACTION_ID_MAX_LENGTH",
    "ACTION_NAME_MAX_LENGTH",
    "RECOMMENDED_ACTION_ALLOWED_KEYS",
    "RESPONSE_SCHEMA_VERSION_V1",
    "RESPONSE_SCHEMA_VERSION_V2",
    "REQUESTED_OUTPUT_SCHEMA_V2",
    "RecommendedAction",
    "PredictionLineV2",
    "OpponentPredictionV2",
    "TurnAdviceBodyV2",
    "TurnAdviceSchemaError",
    "turn_advice_body_v2_from_dict",
    "turn_advice_body_v2_to_canonical_dict",
    "canonical_turn_advice_v2_json",
    "turn_advice_body_v2_from_canonical_json",
]

#: Tag applied to every historical/pre-Bundle-6 persisted advice row. Never
#: produced by this module's parser (which only ever emits
#: :data:`RESPONSE_SCHEMA_VERSION_V2` bodies) -- it exists here purely as the
#: sibling constant persistence code tags legacy rows with.
RESPONSE_SCHEMA_VERSION_V1: Final[str] = "maple-turn-advice-response.v1"
RESPONSE_SCHEMA_VERSION_V2: Final[str] = "maple-turn-advice-response.v2"

ALLOWED_PREDICTION_CATEGORIES_V2: Final[frozenset[str]] = frozenset(
    {"DAMAGING_MOVE", "NON_DAMAGING_MOVE", "SWITCH", "UNKNOWN"}
)
ALLOWED_SUPPORT_BASIS: Final[frozenset[str]] = frozenset(
    {"CONFIRMED_MATCH", "PINNED_RULES", "POPULATION_PRIOR", "GENERAL_KNOWLEDGE", "NONE"}
)
ALLOWED_SUPPORT_LEVELS: Final[frozenset[str]] = frozenset({"LOW", "MEDIUM", "HIGH"})
ALLOWED_ROBUSTNESS_LEVELS: Final[frozenset[str]] = frozenset({"LOW", "MEDIUM", "HIGH"})

#: Ordinal ordering used to compare an alternative's support against the
#: primary's. Never used as a numeric probability -- purely a total order
#: over the three fixed labels.
_SUPPORT_ORDER: Final[dict[str, int]] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

REASONS_MIN_V2: Final[int] = 1
REASONS_MAX_V2: Final[int] = 2
WARNINGS_MAX_V2: Final[int] = 2
ALTERNATIVES_MAX_V2: Final[int] = 2

REASON_MAX_LENGTH_V2: Final[int] = 280
WARNING_MAX_LENGTH_V2: Final[int] = 280
SUMMARY_MAX_LENGTH_V2: Final[int] = 400
SPECIFIC_ACTION_MAX_LENGTH_V2: Final[int] = 128

TOP_LEVEL_ALLOWED_KEYS_V2: Final[frozenset[str]] = frozenset(
    {
        "response_schema_version",
        "recommended_action",
        "recommendation_robustness",
        "reasons",
        "opponent_prediction",
        "warnings",
    }
)
OPPONENT_PREDICTION_ALLOWED_KEYS_V2: Final[frozenset[str]] = frozenset(
    {"primary", "alternatives"}
)
PREDICTION_LINE_ALLOWED_KEYS_V2: Final[frozenset[str]] = frozenset(
    {"category", "specific_action", "support_basis", "support", "summary"}
)


@dataclass(frozen=True, slots=True)
class PredictionLineV2:
    """One opponent-prediction line: primary or one of up to two alternatives."""

    category: str
    specific_action: str | None
    support_basis: str
    support: str
    summary: str

    def __post_init__(self) -> None:
        if self.category not in ALLOWED_PREDICTION_CATEGORIES_V2:
            raise TurnAdviceSchemaError("prediction_line_category_invalid")
        if self.support_basis not in ALLOWED_SUPPORT_BASIS:
            raise TurnAdviceSchemaError("prediction_line_support_basis_invalid")
        if self.support not in ALLOWED_SUPPORT_LEVELS:
            raise TurnAdviceSchemaError("prediction_line_support_invalid")

        if self.category == "UNKNOWN":
            if self.specific_action is not None:
                raise TurnAdviceSchemaError("unknown_prediction_specific_action_must_be_null")
            if self.support_basis != "NONE":
                raise TurnAdviceSchemaError("unknown_prediction_support_basis_must_be_none")
            if self.support != "LOW":
                raise TurnAdviceSchemaError("unknown_prediction_support_must_be_low")
        elif self.support_basis == "NONE":
            # NONE is reserved for UNKNOWN/LOW combinations only (spec sec. 10).
            raise TurnAdviceSchemaError("non_unknown_prediction_support_basis_must_not_be_none")

        if self.support_basis == "GENERAL_KNOWLEDGE" and self.support != "LOW":
            raise TurnAdviceSchemaError("general_knowledge_support_must_be_low")
        if self.support_basis == "POPULATION_PRIOR" and self.support == "HIGH":
            raise TurnAdviceSchemaError("population_prior_support_must_not_be_high")
        if self.support == "LOW" and self.specific_action is not None:
            raise TurnAdviceSchemaError("low_support_specific_action_must_be_null")


@dataclass(frozen=True, slots=True)
class OpponentPredictionV2:
    """Exactly one primary prediction plus zero to two alternatives."""

    primary: PredictionLineV2
    alternatives: tuple[PredictionLineV2, ...]

    def __post_init__(self) -> None:
        if len(self.alternatives) > ALTERNATIVES_MAX_V2:
            raise TurnAdviceSchemaError("alternatives_count_invalid")
        if self.primary.category == "UNKNOWN" and self.alternatives:
            raise TurnAdviceSchemaError("unknown_primary_must_have_no_alternatives")

        for alternative in self.alternatives:
            if alternative.category == "UNKNOWN":
                raise TurnAdviceSchemaError("alternative_must_not_be_unknown")
            if _SUPPORT_ORDER[alternative.support] > _SUPPORT_ORDER[self.primary.support]:
                raise TurnAdviceSchemaError("alternative_support_exceeds_primary")

        seen_pairs: set[tuple[str, str | None]] = set()
        seen_summaries: set[str] = set()
        for line in (self.primary, *self.alternatives):
            pair = (line.category, line.specific_action)
            if pair in seen_pairs:
                raise TurnAdviceSchemaError("duplicate_prediction_line")
            seen_pairs.add(pair)
            if line.summary in seen_summaries:
                raise TurnAdviceSchemaError("duplicate_prediction_summary")
            seen_summaries.add(line.summary)


@dataclass(frozen=True, slots=True)
class TurnAdviceBodyV2:
    """The complete strict ``maple-turn-advice-response.v2`` body."""

    response_schema_version: str
    recommended_action: RecommendedAction
    recommendation_robustness: str
    reasons: tuple[str, ...]
    opponent_prediction: OpponentPredictionV2
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.response_schema_version != RESPONSE_SCHEMA_VERSION_V2:
            raise TurnAdviceSchemaError("response_schema_version_invalid")
        if self.recommendation_robustness not in ALLOWED_ROBUSTNESS_LEVELS:
            raise TurnAdviceSchemaError("recommendation_robustness_invalid")
        if not (REASONS_MIN_V2 <= len(self.reasons) <= REASONS_MAX_V2):
            raise TurnAdviceSchemaError("reasons_count_invalid")
        if len(self.warnings) > WARNINGS_MAX_V2:
            raise TurnAdviceSchemaError("warnings_count_invalid")
        if self.recommendation_robustness == "LOW" and not self.warnings:
            raise TurnAdviceSchemaError("low_robustness_requires_warning")


#: Inlined verbatim into both ``opponent_prediction.primary`` and
#: ``opponent_prediction.alternatives.items`` below -- deliberately not a
#: ``$defs`` reference, since current provider structured-schema support is
#: not proven in this repository (spec sec. 15).
_PREDICTION_LINE_SCHEMA_V2: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": sorted(ALLOWED_PREDICTION_CATEGORIES_V2),
        },
        "specific_action": {"type": ["string", "null"]},
        "support_basis": {
            "type": "string",
            "enum": sorted(ALLOWED_SUPPORT_BASIS),
        },
        "support": {"type": "string", "enum": sorted(ALLOWED_SUPPORT_LEVELS)},
        "summary": {"type": "string"},
    },
    "required": ["category", "specific_action", "support_basis", "support", "summary"],
    "additionalProperties": False,
}

#: Fixed and deterministic, mirroring ``turn_request.REQUESTED_OUTPUT_SCHEMA``'s
#: style. Never derived from a live provider schema.
REQUESTED_OUTPUT_SCHEMA_V2: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "response_schema_version": {
            "type": "string",
            "enum": [RESPONSE_SCHEMA_VERSION_V2],
        },
        "recommended_action": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
                "action_type": {"type": "string", "enum": ["MOVE", "SWITCH"]},
                "action_name": {"type": "string"},
            },
            "required": ["action_id", "action_type", "action_name"],
            "additionalProperties": False,
        },
        "recommendation_robustness": {
            "type": "string",
            "enum": sorted(ALLOWED_ROBUSTNESS_LEVELS),
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": REASONS_MIN_V2,
            "maxItems": REASONS_MAX_V2,
        },
        "opponent_prediction": {
            "type": "object",
            "properties": {
                "primary": dict(_PREDICTION_LINE_SCHEMA_V2),
                "alternatives": {
                    "type": "array",
                    "items": dict(_PREDICTION_LINE_SCHEMA_V2),
                    "minItems": 0,
                    "maxItems": ALTERNATIVES_MAX_V2,
                },
            },
            "required": ["primary", "alternatives"],
            "additionalProperties": False,
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": WARNINGS_MAX_V2,
        },
    },
    "required": [
        "response_schema_version",
        "recommended_action",
        "recommendation_robustness",
        "reasons",
        "opponent_prediction",
        "warnings",
    ],
    "additionalProperties": False,
}


def _response_schema_version_from_value(value: Any) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TurnAdviceSchemaError("response_schema_version_must_be_string")
    return value


def _recommendation_robustness_from_value(value: Any) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TurnAdviceSchemaError("recommendation_robustness_must_be_string")
    return value


def _prediction_line_from_dict(data: Any) -> PredictionLineV2:
    obj = _require_dict(data, what="prediction_line")
    _reject_unknown_keys(obj, PREDICTION_LINE_ALLOWED_KEYS_V2, what="prediction_line")

    category = obj["category"]
    if not isinstance(category, str) or isinstance(category, bool):
        raise TurnAdviceSchemaError("prediction_line_category_must_be_string")

    specific_action_raw = obj["specific_action"]
    specific_action: str | None
    if specific_action_raw is None:
        specific_action = None
    elif isinstance(specific_action_raw, str) and not isinstance(specific_action_raw, bool):
        if not specific_action_raw.strip():
            raise TurnAdviceSchemaError("specific_action_must_be_non_empty")
        if len(specific_action_raw) > SPECIFIC_ACTION_MAX_LENGTH_V2:
            raise TurnAdviceSchemaError("specific_action_too_long")
        specific_action = specific_action_raw
    else:
        raise TurnAdviceSchemaError("specific_action_must_be_string_or_null")

    support_basis = obj["support_basis"]
    if not isinstance(support_basis, str) or isinstance(support_basis, bool):
        raise TurnAdviceSchemaError("support_basis_must_be_string")

    support = obj["support"]
    if not isinstance(support, str) or isinstance(support, bool):
        raise TurnAdviceSchemaError("support_must_be_string")

    summary = _require_non_empty_str(
        obj["summary"], what="summary", max_length=SUMMARY_MAX_LENGTH_V2
    )

    return PredictionLineV2(
        category=category,
        specific_action=specific_action,
        support_basis=support_basis,
        support=support,
        summary=summary,
    )


def _opponent_prediction_v2_from_dict(data: Any) -> OpponentPredictionV2:
    obj = _require_dict(data, what="opponent_prediction")
    _reject_unknown_keys(obj, OPPONENT_PREDICTION_ALLOWED_KEYS_V2, what="opponent_prediction")

    primary = _prediction_line_from_dict(obj["primary"])

    alternatives_raw = obj["alternatives"]
    if not isinstance(alternatives_raw, list):
        raise TurnAdviceSchemaError("alternatives_must_be_array")
    alternatives = tuple(_prediction_line_from_dict(item) for item in alternatives_raw)

    return OpponentPredictionV2(primary=primary, alternatives=alternatives)


def turn_advice_body_v2_from_dict(data: Any) -> TurnAdviceBodyV2:
    """Strictly validate a parsed provider JSON object into a TurnAdviceBodyV2.

    Rejects unknown fields at every level, missing fields, wrong types,
    invalid enum values, and every cross-field combination forbidden by the
    v2 contract (UNKNOWN combinations, support/support_basis constraints,
    alternative-vs-primary ordering, duplicates, LOW-robustness-without-
    warning). Never repairs a malformed body -- any violation raises
    :class:`TurnAdviceSchemaError`.
    """

    obj = _require_dict(data, what="top_level")
    _reject_unknown_keys(obj, TOP_LEVEL_ALLOWED_KEYS_V2, what="top_level")

    response_schema_version = _response_schema_version_from_value(
        obj["response_schema_version"]
    )
    recommended_action = _recommended_action_from_dict(obj["recommended_action"])
    recommendation_robustness = _recommendation_robustness_from_value(
        obj["recommendation_robustness"]
    )
    reasons = _reasons_from_list(obj["reasons"])
    opponent_prediction = _opponent_prediction_v2_from_dict(obj["opponent_prediction"])
    warnings = _warnings_from_list(obj["warnings"])

    return TurnAdviceBodyV2(
        response_schema_version=response_schema_version,
        recommended_action=recommended_action,
        recommendation_robustness=recommendation_robustness,
        reasons=reasons,
        opponent_prediction=opponent_prediction,
        warnings=warnings,
    )


def _prediction_line_to_canonical_dict(line: PredictionLineV2) -> dict[str, Any]:
    return {
        "category": line.category,
        "specific_action": line.specific_action,
        "support_basis": line.support_basis,
        "support": line.support,
        "summary": line.summary,
    }


def turn_advice_body_v2_to_canonical_dict(body: TurnAdviceBodyV2) -> dict[str, Any]:
    """Render an accepted ``TurnAdviceBodyV2`` as a plain, canonical dict.

    Used only for persistence (``advice_json``) and export
    (``structured_response``) of an already-accepted body -- never for
    parsing raw provider text (see :func:`turn_advice_body_v2_from_dict`).
    """

    return {
        "response_schema_version": body.response_schema_version,
        "recommended_action": {
            "action_id": body.recommended_action.action_id,
            "action_type": body.recommended_action.action_type,
            "action_name": body.recommended_action.action_name,
        },
        "recommendation_robustness": body.recommendation_robustness,
        "reasons": list(body.reasons),
        "opponent_prediction": {
            "primary": _prediction_line_to_canonical_dict(body.opponent_prediction.primary),
            "alternatives": [
                _prediction_line_to_canonical_dict(line)
                for line in body.opponent_prediction.alternatives
            ],
        },
        "warnings": list(body.warnings),
    }


def canonical_turn_advice_v2_json(body: TurnAdviceBodyV2) -> str:
    """Deterministic encoding: sorted keys, no whitespace, explicit separators.

    Matches the canonical-JSON idiom used throughout the request/hash code
    (e.g. ``turn_advice_rich_state._canonical_json_bytes``): ``sort_keys=True``,
    fixed ``separators``, ``ensure_ascii=False`` so Japanese text is stored
    verbatim rather than ``\\uXXXX``-escaped.
    """

    return json.dumps(
        turn_advice_body_v2_to_canonical_dict(body),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def turn_advice_body_v2_from_canonical_json(text: str) -> TurnAdviceBodyV2:
    """Strictly re-parse a previously-persisted/exported canonical v2 body.

    Reuses the exact same strict parser as raw provider text
    (:func:`turn_advice_body_v2_from_dict`) -- a canonical JSON blob is
    re-validated on every read, never trusted merely because it was already
    accepted once. Raises :class:`TurnAdviceSchemaError` (via the parser) on
    invalid JSON shape, and :class:`json.JSONDecodeError` on malformed JSON
    text; callers persisting/reading this value are expected to catch both
    and fail closed rather than fall back to any other representation.
    """

    return turn_advice_body_v2_from_dict(json.loads(text))
