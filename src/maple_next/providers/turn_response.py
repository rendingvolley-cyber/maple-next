"""Strict Turn Advice provider-response schema and trusted normalized envelope.

Everything here is pure data validation: no network, no persistence, no
clock reads. The raw provider dict is walked field by field with
``additionalProperties`` effectively false at every level (top-level and
every nested object) — unknown keys are rejected, not ignored.

``source_type`` and ``model`` are never read from the provider's own JSON
body. They do not appear anywhere in :data:`ALLOWED_TOP_LEVEL_KEYS` (or any
nested allow-list), so a provider body that includes either key is rejected
by :func:`turn_advice_body_from_dict` as an unknown top-level field before
any downstream code has a chance to trust it. The trusted normalized
envelope (:class:`NormalizedTurnAdviceResult`) only carries ``source_type``
/ ``model`` when they are injected explicitly by a trusted caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

ALLOWED_ACTION_TYPES: Final[frozenset[str]] = frozenset({"MOVE", "SWITCH"})
ALLOWED_OPPONENT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"MOVE", "SWITCH", "STATUS_OR_SETUP", "UNKNOWN"}
)

REASONS_MIN: Final[int] = 1
REASONS_MAX: Final[int] = 3
WARNINGS_MAX: Final[int] = 5
REASON_MAX_LENGTH: Final[int] = 280
WARNING_MAX_LENGTH: Final[int] = 280
SUMMARY_MAX_LENGTH: Final[int] = 400
ACTION_ID_MAX_LENGTH: Final[int] = 128
ACTION_NAME_MAX_LENGTH: Final[int] = 128
PREDICTED_ACTION_MAX_LENGTH: Final[int] = 128

TOP_LEVEL_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"recommended_action", "reasons", "warnings", "opponent_prediction"}
)
RECOMMENDED_ACTION_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"action_id", "action_type", "action_name"}
)
OPPONENT_PREDICTION_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"category", "predicted_action", "summary", "confidence"}
)


class TurnAdviceSchemaError(ValueError):
    """Raised when a provider body fails strict schema validation.

    The message is always one of a small fixed set of sanitized tokens
    (see ``turn_validation.py``) — never the raw provider text.
    """


def _require_dict(value: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TurnAdviceSchemaError(f"{what}_must_be_object")
    return value


def _require_non_empty_str(value: Any, *, what: str, max_length: int) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TurnAdviceSchemaError(f"{what}_must_be_string")
    if not value.strip():
        raise TurnAdviceSchemaError(f"{what}_must_be_non_empty")
    if len(value) > max_length:
        raise TurnAdviceSchemaError(f"{what}_too_long")
    return value


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], *, what: str) -> None:
    extra = set(data.keys()) - allowed
    if extra:
        raise TurnAdviceSchemaError(f"{what}_unknown_fields")
    missing = allowed - set(data.keys())
    if missing:
        raise TurnAdviceSchemaError(f"{what}_missing_fields")


@dataclass(frozen=True, slots=True)
class RecommendedAction:
    action_id: str
    action_type: str
    action_name: str

    def __post_init__(self) -> None:
        if self.action_type not in ALLOWED_ACTION_TYPES:
            raise TurnAdviceSchemaError("recommended_action_type_invalid")


@dataclass(frozen=True, slots=True)
class OpponentPrediction:
    category: str
    predicted_action: str | None
    summary: str
    confidence: float

    def __post_init__(self) -> None:
        if self.category not in ALLOWED_OPPONENT_CATEGORIES:
            raise TurnAdviceSchemaError("opponent_prediction_category_invalid")
        if not (0.0 <= self.confidence <= 1.0):
            raise TurnAdviceSchemaError("opponent_prediction_confidence_out_of_range")


@dataclass(frozen=True, slots=True)
class TurnAdviceBody:
    recommended_action: RecommendedAction
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    opponent_prediction: OpponentPrediction

    def __post_init__(self) -> None:
        if not (REASONS_MIN <= len(self.reasons) <= REASONS_MAX):
            raise TurnAdviceSchemaError("reasons_count_invalid")
        if len(self.warnings) > WARNINGS_MAX:
            raise TurnAdviceSchemaError("warnings_count_invalid")


def _recommended_action_from_dict(data: Any) -> RecommendedAction:
    obj = _require_dict(data, what="recommended_action")
    _reject_unknown_keys(obj, RECOMMENDED_ACTION_ALLOWED_KEYS, what="recommended_action")
    action_id = _require_non_empty_str(
        obj["action_id"], what="action_id", max_length=ACTION_ID_MAX_LENGTH
    )
    action_type = obj["action_type"]
    if not isinstance(action_type, str) or isinstance(action_type, bool):
        raise TurnAdviceSchemaError("action_type_must_be_string")
    action_name = _require_non_empty_str(
        obj["action_name"], what="action_name", max_length=ACTION_NAME_MAX_LENGTH
    )
    return RecommendedAction(action_id=action_id, action_type=action_type, action_name=action_name)


def _reasons_from_list(data: Any) -> tuple[str, ...]:
    if not isinstance(data, list):
        raise TurnAdviceSchemaError("reasons_must_be_array")
    return tuple(
        _require_non_empty_str(item, what="reason", max_length=REASON_MAX_LENGTH)
        for item in data
    )


def _warnings_from_list(data: Any) -> tuple[str, ...]:
    if not isinstance(data, list):
        raise TurnAdviceSchemaError("warnings_must_be_array")
    return tuple(
        _require_non_empty_str(item, what="warning", max_length=WARNING_MAX_LENGTH)
        for item in data
    )


def _opponent_prediction_from_dict(data: Any) -> OpponentPrediction:
    obj = _require_dict(data, what="opponent_prediction")
    _reject_unknown_keys(obj, OPPONENT_PREDICTION_ALLOWED_KEYS, what="opponent_prediction")

    category = obj["category"]
    if not isinstance(category, str) or isinstance(category, bool):
        raise TurnAdviceSchemaError("opponent_prediction_category_must_be_string")

    predicted_action_raw = obj["predicted_action"]
    predicted_action: str | None
    if predicted_action_raw is None:
        predicted_action = None
    elif isinstance(predicted_action_raw, str) and not isinstance(predicted_action_raw, bool):
        if len(predicted_action_raw) > PREDICTED_ACTION_MAX_LENGTH:
            raise TurnAdviceSchemaError("predicted_action_too_long")
        predicted_action = predicted_action_raw
    else:
        raise TurnAdviceSchemaError("predicted_action_must_be_string_or_null")

    summary = _require_non_empty_str(
        obj["summary"], what="summary", max_length=SUMMARY_MAX_LENGTH
    )

    confidence_raw = obj["confidence"]
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise TurnAdviceSchemaError("confidence_must_be_number")
    confidence = float(confidence_raw)

    return OpponentPrediction(
        category=category,
        predicted_action=predicted_action,
        summary=summary,
        confidence=confidence,
    )


def turn_advice_body_from_dict(data: Any) -> TurnAdviceBody:
    """Strictly validate a parsed provider JSON object into a TurnAdviceBody.

    Rejects unknown fields at every level (top-level and nested), wrong
    types, out-of-range values, and booleans passed where numbers/strings
    are required. Never accepts ``source_type`` or ``model`` — those are
    not in :data:`TOP_LEVEL_ALLOWED_KEYS` and trigger
    ``top_level_unknown_fields`` like any other unexpected key.
    """

    obj = _require_dict(data, what="top_level")
    _reject_unknown_keys(obj, TOP_LEVEL_ALLOWED_KEYS, what="top_level")

    recommended_action = _recommended_action_from_dict(obj["recommended_action"])
    reasons = _reasons_from_list(obj["reasons"])
    warnings = _warnings_from_list(obj["warnings"])
    opponent_prediction = _opponent_prediction_from_dict(obj["opponent_prediction"])

    return TurnAdviceBody(
        recommended_action=recommended_action,
        reasons=reasons,
        warnings=warnings,
        opponent_prediction=opponent_prediction,
    )


@dataclass(frozen=True, slots=True)
class NormalizedTurnAdviceResult:
    """Trusted, normalized Turn Advice result. Never carries raw provider text.

    ``source_type`` and ``model`` are only ever populated from explicit
    trusted-caller parameters (see ``turn_validation.build_normalized_turn_advice_result``)
    — never from the provider's own JSON body.
    """

    contract_version: str
    job_type: str
    session_id: str
    match_id: str
    generation: int
    turn_number: int
    battle_revision: int
    reviewed_snapshot_id: str
    reviewed_snapshot_hash: str
    request_payload_hash: str
    source_type: str
    model: str
    advice: TurnAdviceBody

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise ValueError("source_type must be non-empty")
        if not self.model.strip():
            raise ValueError("model must be non-empty")
