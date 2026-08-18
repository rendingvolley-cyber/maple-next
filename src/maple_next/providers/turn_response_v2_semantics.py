"""Request-aware semantic validation for Turn Advice response v2 (Gemini V2 Bundle 6).

Pure functions only: no persistence, no network, no clock reads. Strict
schema validity (:func:`~maple_next.providers.turn_response_v2.turn_advice_body_v2_from_dict`)
is a precondition, not a substitute, for what lives here -- the schema
parser cannot see the live request, so it cannot check whether a
prediction line's ``specific_action`` is actually grounded in confirmed
current-match evidence or the matched pinned population snapshot, rather
than general knowledge, strategic convenience, or imagined coverage.

Source-membership rule (spec sec. 8):

- A ``DAMAGING_MOVE``/``NON_DAMAGING_MOVE`` line's non-null ``specific_action``
  must name either a move the opponent was confirmed to use in a completed
  prior Turn (:mod:`maple_next.domain.battle_memory`), or a move present in
  the matched pinned ``opponent_intel_context`` population snapshot. No
  fuzzy matching -- an exact string membership check only.
- A ``SWITCH`` line's non-null ``specific_action`` must name a Pokemon
  species already confirmed as belonging to the opponent during this match
  (from completed-Turn battle memory), and must not be the opponent's
  current confirmed active. Never inferred from population partners.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from maple_next.domain.enums import ActionType
from maple_next.domain.opponent_intel_context import (
    COMPATIBILITY_MATCHED,
    CONTEXT_STATUS_AVAILABLE,
)
from maple_next.domain.turn_state import KnowledgeStatus
from maple_next.providers.turn_advice_rich_state import RichStateTurnAdviceRequest
from maple_next.providers.turn_response_v2 import PredictionLineV2, TurnAdviceBodyV2

_MOVE_CATEGORIES: Final[frozenset[str]] = frozenset({"DAMAGING_MOVE", "NON_DAMAGING_MOVE"})
_SWITCH_CATEGORY: Final[str] = "SWITCH"


class TurnAdviceV2SemanticResultCode(StrEnum):
    VALID = "VALID"
    SPECIFIC_ACTION_MOVE_UNSUPPORTED = "SPECIFIC_ACTION_MOVE_UNSUPPORTED"
    SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED = "SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED"
    SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE = (
        "SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE"
    )


_SANITIZED_REASON: Final[dict[TurnAdviceV2SemanticResultCode, str]] = {
    TurnAdviceV2SemanticResultCode.VALID: "TURN_ADVICE_V2_SEMANTICS_VALID",
    TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_MOVE_UNSUPPORTED: (
        "TURN_ADVICE_V2_SPECIFIC_ACTION_MOVE_UNSUPPORTED"
    ),
    TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED: (
        "TURN_ADVICE_V2_SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED"
    ),
    TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE: (
        "TURN_ADVICE_V2_SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE"
    ),
}


def sanitized_reason_for_v2_semantics(code: TurnAdviceV2SemanticResultCode) -> str:
    """Map a semantic result code to its fixed sanitized reason token."""

    return _SANITIZED_REASON[code]


def _confirmed_opponent_move_names(request: RichStateTurnAdviceRequest) -> frozenset[str]:
    """Move names the opponent was confirmed to use in a completed prior Turn."""

    return frozenset(
        turn.opponent_action.action_name
        for turn in request.battle_memory.turns
        if turn.opponent_action.knowledge_status is KnowledgeStatus.CONFIRMED
        and turn.opponent_action.action_type is ActionType.MOVE
        and turn.opponent_action.action_name is not None
    )


def _matched_population_move_names(request: RichStateTurnAdviceRequest) -> frozenset[str]:
    """Move names from the matched pinned population snapshot, or empty.

    Only trusted when the context is ``AVAILABLE`` *and* its regulation
    compatibility is ``MATCHED`` -- an ``UNAVAILABLE``/``MISMATCHED`` context
    carries no ``population`` payload at all (enforced upstream by
    :func:`~maple_next.domain.opponent_intel_context.validate_opponent_intel_context`).
    """

    context = request.opponent_intel_context
    if context["status"] != CONTEXT_STATUS_AVAILABLE:
        return frozenset()
    if context["compatibility"]["status"] != COMPATIBILITY_MATCHED:
        return frozenset()
    return frozenset(entry["name"] for entry in context["population"]["moves"])


def _confirmed_opponent_species_this_match(request: RichStateTurnAdviceRequest) -> frozenset[str]:
    """Every species confirmed as the opponent's active at some point this match.

    Drawn only from completed-Turn battle memory -- never from population
    partners, an OCR-only sighting, or an unconfirmed draft. A confirmed
    literal ``"UNKNOWN"`` (human-confirmed ignorance) is not a species name
    and is excluded.
    """

    species: set[str] = set()
    for turn in request.battle_memory.turns:
        if turn.turn_start_opponent_active.is_confirmed:
            value = turn.turn_start_opponent_active.value
            if value is not None and value != "UNKNOWN":
                species.add(value)
        if (
            turn.opponent_action.knowledge_status is KnowledgeStatus.CONFIRMED
            and turn.opponent_action.action_type is ActionType.SWITCH
            and turn.opponent_action.action_name is not None
        ):
            species.add(turn.opponent_action.action_name)
    return frozenset(species)


def _current_opponent_active_species(request: RichStateTurnAdviceRequest) -> str | None:
    """The opponent's current confirmed active, per the bound ``opponent_intel_context``.

    Reuses the same ``confirmed_active_species`` value the rich request is
    already bound to (see ``turn_advice_rich_state.build_rich_state_turn_advice_request``)
    rather than re-deriving it from the projection independently.
    """

    value = request.opponent_intel_context.get("confirmed_active_species")
    return value if isinstance(value, str) and value.strip() else None


def _check_prediction_line(
    line: PredictionLineV2, *, request: RichStateTurnAdviceRequest
) -> TurnAdviceV2SemanticResultCode:
    if line.specific_action is None:
        return TurnAdviceV2SemanticResultCode.VALID

    if line.category in _MOVE_CATEGORIES:
        allowed = _confirmed_opponent_move_names(request) | _matched_population_move_names(
            request
        )
        if line.specific_action not in allowed:
            return TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_MOVE_UNSUPPORTED
        return TurnAdviceV2SemanticResultCode.VALID

    if line.category == _SWITCH_CATEGORY:
        current_active = _current_opponent_active_species(request)
        if current_active is not None and line.specific_action == current_active:
            return TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_IS_CURRENT_ACTIVE
        if line.specific_action not in _confirmed_opponent_species_this_match(request):
            return TurnAdviceV2SemanticResultCode.SPECIFIC_ACTION_SWITCH_TARGET_UNCONFIRMED
        return TurnAdviceV2SemanticResultCode.VALID

    return TurnAdviceV2SemanticResultCode.VALID


def validate_turn_advice_v2_semantics(
    body: TurnAdviceBodyV2, *, request: RichStateTurnAdviceRequest
) -> TurnAdviceV2SemanticResultCode:
    """Request-aware semantic validation of every prediction line, fail closed.

    Checks ``opponent_prediction.primary`` and every entry of
    ``opponent_prediction.alternatives``; returns the first violation found,
    or ``VALID`` if every line's ``specific_action`` is properly grounded (or
    null, which is always valid).
    """

    for line in (body.opponent_prediction.primary, *body.opponent_prediction.alternatives):
        code = _check_prediction_line(line, request=request)
        if code is not TurnAdviceV2SemanticResultCode.VALID:
            return code
    return TurnAdviceV2SemanticResultCode.VALID
