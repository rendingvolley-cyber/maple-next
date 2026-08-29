"""Strict result-apply gate for Turn Advice. Pure functions only.

Mirrors the "strict result apply gate" pattern used for Selection Advice
(see ``application/service.py``'s ``_binding_failure_reason`` /
``apply_selection_advice_result``), but scoped entirely to the offline Lane
C contract: no persistence, no application-service coupling, no network.

Raw provider text is parsed and validated here and then discarded — it is
never attached to :class:`~maple_next.providers.turn_response.NormalizedTurnAdviceResult`
or to any value returned from this module. Only sanitized result codes
travel outward; no raw provider error text is ever concatenated into a
returned value.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final, Literal, Protocol, runtime_checkable

from maple_next.domain.enums import ActionType
from maple_next.providers.turn_advice_rich_state import RICH_STATE_REQUEST_CONTRACT_VERSION
from maple_next.providers.turn_request import (
    CONTRACT_VERSION,
    CONTRACT_VERSION_V2,
    TurnAdviceRequest,
    request_payload_hash,
)
from maple_next.providers.turn_response import (
    NormalizedTurnAdviceResult,
    RecommendedAction,
    TurnAdviceBody,
    TurnAdviceSchemaError,
    turn_advice_body_from_dict,
)


@runtime_checkable
class _LegalActionLike(Protocol):
    @property
    def action_id(self) -> str: ...
    @property
    def action_type(self) -> ActionType: ...
    @property
    def action_name(self) -> str: ...
    @property
    def owner_active(self) -> str | None: ...
    @property
    def switch_target(self) -> str | None: ...


@runtime_checkable
class LegalActionBindingRequest(Protocol):
    """Structural contract :func:`validate_turn_advice_legality` actually needs.

    Both the legacy :class:`~maple_next.providers.turn_request.TurnAdviceRequest`
    and the additive Bundle B
    :class:`~maple_next.providers.turn_advice_rich_state.RichStateTurnAdviceRequest`
    satisfy this shape, so the same legality check applies to both lanes
    without reimplementation.
    """

    @property
    def self_active(self) -> str: ...
    @property
    def selected_three(self) -> tuple[str, str, str]: ...
    @property
    def legal_actions(self) -> tuple[_LegalActionLike, ...]: ...


class TurnAdviceResultCode(StrEnum):
    VALID = "VALID"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    ILLEGAL_ACTION = "ILLEGAL_ACTION"
    NON_OWNED_ACTION = "NON_OWNED_ACTION"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    MATCH_MISMATCH = "MATCH_MISMATCH"
    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    TURN_MISMATCH = "TURN_MISMATCH"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    SNAPSHOT_ID_MISMATCH = "SNAPSHOT_ID_MISMATCH"
    SNAPSHOT_HASH_MISMATCH = "SNAPSHOT_HASH_MISMATCH"
    REQUEST_HASH_MISMATCH = "REQUEST_HASH_MISMATCH"
    SOURCE_INVALID = "SOURCE_INVALID"
    MODEL_INVALID = "MODEL_INVALID"
    STALE_REJECTED = "STALE_REJECTED"


#: Sanitized, fixed error tokens. Never built by string-formatting raw
#: provider text or exception details into the result.
_SANITIZED_REASON: Final[dict[TurnAdviceResultCode, str]] = {
    TurnAdviceResultCode.VALID: "TURN_ADVICE_VALID",
    TurnAdviceResultCode.INVALID_SCHEMA: "TURN_ADVICE_SCHEMA_REJECTED",
    TurnAdviceResultCode.ILLEGAL_ACTION: "TURN_ADVICE_ILLEGAL_ACTION",
    TurnAdviceResultCode.NON_OWNED_ACTION: "TURN_ADVICE_ILLEGAL_ACTION",
    TurnAdviceResultCode.SESSION_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.MATCH_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.GENERATION_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.TURN_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.REVISION_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.SNAPSHOT_ID_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.SNAPSHOT_HASH_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.REQUEST_HASH_MISMATCH: "TURN_ADVICE_STALE",
    TurnAdviceResultCode.SOURCE_INVALID: "TURN_ADVICE_SOURCE_INVALID",
    TurnAdviceResultCode.MODEL_INVALID: "TURN_ADVICE_MODEL_INVALID",
    TurnAdviceResultCode.STALE_REJECTED: "TURN_ADVICE_STALE",
}


class TurnAdviceParseError(ValueError):
    """Raised by :func:`parse_turn_advice_body`. Message is a sanitized token only."""


def sanitized_reason_for(code: TurnAdviceResultCode) -> str:
    """Map a result code to its fixed sanitized reason token."""

    return _SANITIZED_REASON[code]


#: Gemini V2 Bundle 6. Rich-state request contract versions that predate the
#: ``.v7``/response-schema-v2 boundary. These values never had named module
#: constants of their own -- each was superseded by the next bundle's raise
#: of ``RICH_STATE_REQUEST_CONTRACT_VERSION`` before this dispatcher existed
#: -- so they are listed here literally, purely to document (and unit-test)
#: that a historical rich request of any of these versions resolves to the
#: v1 parser, exactly like a legacy request.
_RICH_STATE_PRE_V7_CONTRACT_VERSIONS: Final[frozenset[str]] = frozenset(
    {
        "maple-turn-advice.v3",
        "maple-turn-advice.v4",
        "maple-turn-advice.v5",
        "maple-turn-advice.v6",
    }
)

# ``.v7`` was the immediately previous rich request contract and already
# paired with the structured response parser v2. It remains a historical
# contract after the current request moves to ``.v8``.
_RICH_STATE_V7_CONTRACT_VERSION = "maple-turn-advice.v7"


def select_response_parser_version(contract_version: str) -> Literal["v1", "v2"]:
    """Trusted response-parser selection, keyed on the REQUEST/job contract only.

    Never chooses a parser because the provider's own response body claims a
    version -- that value is not even trusted input until after the correct
    parser has already accepted it. Legacy ``maple-turn-advice.v1``/``.v2``
    and every pre-``.v7`` rich contract resolve to the v1 parser
    (:func:`~maple_next.providers.turn_response.turn_advice_body_from_dict`);
    only the current rich contract
    (historical ``.v7`` and
    :data:`~maple_next.providers.turn_advice_rich_state.RICH_STATE_REQUEST_CONTRACT_VERSION`,
    current ``.v8``) resolve to the v2 parser
    (:func:`~maple_next.providers.turn_response_v2.turn_advice_body_v2_from_dict`).
    Any other contract version fails closed.
    """

    if contract_version in {CONTRACT_VERSION, CONTRACT_VERSION_V2}:
        return "v1"
    if contract_version in _RICH_STATE_PRE_V7_CONTRACT_VERSIONS:
        return "v1"
    if contract_version in {
        _RICH_STATE_V7_CONTRACT_VERSION,
        RICH_STATE_REQUEST_CONTRACT_VERSION,
    }:
        return "v2"
    raise TurnAdviceParseError("TURN_ADVICE_CONTRACT_VERSION_UNSUPPORTED")


def parse_turn_advice_body(provider_text: str) -> TurnAdviceBody:
    """Parse and strictly validate raw provider text into a TurnAdviceBody.

    ``provider_text`` is consumed here only; the returned value never
    retains it. Markdown, code fences, non-JSON text, or a top-level JSON
    array/scalar all raise :class:`TurnAdviceParseError` with the sanitized
    message ``"TURN_ADVICE_INVALID_JSON"``. Any structurally-parseable JSON
    object that fails the strict schema raises
    ``"TURN_ADVICE_SCHEMA_REJECTED"``.
    """

    try:
        parsed = json.loads(provider_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TurnAdviceParseError("TURN_ADVICE_INVALID_JSON") from exc

    if not isinstance(parsed, dict):
        raise TurnAdviceParseError("TURN_ADVICE_INVALID_JSON")

    try:
        return turn_advice_body_from_dict(parsed)
    except TurnAdviceSchemaError as exc:
        raise TurnAdviceParseError("TURN_ADVICE_SCHEMA_REJECTED") from exc


def build_normalized_turn_advice_result(
    *,
    request: TurnAdviceRequest,
    body: TurnAdviceBody,
    request_payload_hash_value: str,
    source_type: str,
    model: str,
) -> NormalizedTurnAdviceResult:
    """Build the trusted normalized envelope.

    ``source_type`` and ``model`` must be supplied explicitly by a trusted
    caller (e.g. the transport boundary that actually made the call, or a
    test fixture) — they are never read from the provider's own JSON body,
    which does not even carry a slot for them (see
    ``turn_response.TOP_LEVEL_ALLOWED_KEYS``).
    """

    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError(sanitized_reason_for(TurnAdviceResultCode.SOURCE_INVALID))
    if not isinstance(model, str) or not model.strip():
        raise ValueError(sanitized_reason_for(TurnAdviceResultCode.MODEL_INVALID))

    return NormalizedTurnAdviceResult(
        contract_version=request.contract_version,
        job_type=request.job_type,
        session_id=request.session_id,
        match_id=request.match_id,
        generation=request.generation,
        turn_number=request.turn_number,
        battle_revision=request.battle_revision,
        reviewed_snapshot_id=request.reviewed_snapshot_id,
        reviewed_snapshot_hash=request.reviewed_snapshot_hash,
        request_payload_hash=request_payload_hash_value,
        source_type=source_type,
        model=model,
        advice=body,
    )


def validate_turn_advice_binding(
    expected: TurnAdviceRequest, result: NormalizedTurnAdviceResult
) -> TurnAdviceResultCode:
    """Compare every binding field of ``result`` against ``expected``.

    Pure comparison only — never mutates ``expected`` or ``result``. Returns
    the first mismatch found, in the field order documented on
    :class:`TurnAdviceResultCode`, or ``VALID`` if every field matches.
    """

    if result.contract_version != expected.contract_version:
        return TurnAdviceResultCode.STALE_REJECTED
    if result.job_type != expected.job_type:
        return TurnAdviceResultCode.STALE_REJECTED
    if result.session_id != expected.session_id:
        return TurnAdviceResultCode.SESSION_MISMATCH
    if result.match_id != expected.match_id:
        return TurnAdviceResultCode.MATCH_MISMATCH
    if result.generation != expected.generation:
        return TurnAdviceResultCode.GENERATION_MISMATCH
    if result.turn_number != expected.turn_number:
        return TurnAdviceResultCode.TURN_MISMATCH
    if result.battle_revision != expected.battle_revision:
        return TurnAdviceResultCode.REVISION_MISMATCH
    if result.reviewed_snapshot_id != expected.reviewed_snapshot_id:
        return TurnAdviceResultCode.SNAPSHOT_ID_MISMATCH
    if result.reviewed_snapshot_hash != expected.reviewed_snapshot_hash:
        return TurnAdviceResultCode.SNAPSHOT_HASH_MISMATCH
    if result.request_payload_hash != request_payload_hash(expected):
        return TurnAdviceResultCode.REQUEST_HASH_MISMATCH
    return TurnAdviceResultCode.VALID


def _check_recommended_action_legality(
    request: LegalActionBindingRequest, chosen: RecommendedAction
) -> TurnAdviceResultCode:
    """Exact 3-way legal-action match plus MOVE/SWITCH ownership re-check.

    ``chosen``'s ``action_id``, ``action_type``, and ``action_name`` must all
    match one legal action in ``request.legal_actions`` — matching on id
    alone (or any two of the three fields) is not enough. Never substitutes
    or auto-corrects a bad response with a legal one. Shared, unchanged core
    of both :func:`validate_turn_advice_legality` (v1) and
    :func:`validate_turn_advice_legality_v2` (Gemini V2 Bundle 6) — the v1
    response schema's ``RecommendedAction`` type is reused verbatim by v2, so
    the same exact-match check applies to both without reimplementation.
    """

    id_matches = [a for a in request.legal_actions if a.action_id == chosen.action_id]
    if not id_matches:
        return TurnAdviceResultCode.ILLEGAL_ACTION

    exact_matches = [
        a
        for a in id_matches
        if a.action_type.value == chosen.action_type and a.action_name == chosen.action_name
    ]
    if not exact_matches:
        return TurnAdviceResultCode.ILLEGAL_ACTION

    action = exact_matches[0]
    if action.action_type is ActionType.MOVE and action.owner_active != request.self_active:
        return TurnAdviceResultCode.NON_OWNED_ACTION
    if action.action_type is ActionType.SWITCH and (
        action.switch_target not in request.selected_three
        or action.switch_target == request.self_active
    ):
        return TurnAdviceResultCode.NON_OWNED_ACTION

    return TurnAdviceResultCode.VALID


def validate_turn_advice_legality(
    request: LegalActionBindingRequest, result: NormalizedTurnAdviceResult
) -> TurnAdviceResultCode:
    """Exact 3-way legal-action match plus MOVE/SWITCH ownership re-check.

    The recommended action's ``action_id``, ``action_type``, and
    ``action_name`` must all match one legal action in ``request.legal_actions``
    — matching on id alone (or any two of the three fields) is not enough.
    Never substitutes or auto-corrects a bad response with a legal one.
    """

    return _check_recommended_action_legality(request, result.advice.recommended_action)


def validate_turn_advice_legality_v2(
    request: LegalActionBindingRequest, recommended_action: RecommendedAction
) -> TurnAdviceResultCode:
    """Gemini V2 Bundle 6: identical legality check for a v2 response body.

    Takes ``recommended_action`` directly rather than a
    :class:`NormalizedTurnAdviceResult` — v2 bodies are never wrapped in that
    v1-typed envelope (spec: v1 classes are never weakened or repurposed for
    v2). Delegates to the exact same core as :func:`validate_turn_advice_legality`.
    """

    return _check_recommended_action_legality(request, recommended_action)


def validate_turn_advice_result(
    request: TurnAdviceRequest, result: NormalizedTurnAdviceResult
) -> TurnAdviceResultCode:
    """Compose binding validation and legality validation.

    Binding is checked first: a stale/mismatched result is rejected before
    its recommended action is ever compared against the legal-action list.
    """

    binding_code = validate_turn_advice_binding(request, result)
    if binding_code is not TurnAdviceResultCode.VALID:
        return binding_code
    return validate_turn_advice_legality(request, result)
