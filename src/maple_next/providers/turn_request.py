"""Canonical, deterministic Turn Advice request construction.

Mirrors :mod:`maple_next.providers.selection_request` for the Turn Advice
lane. Every function here is pure: no network, no SQLite, no UI widget
access, no clock reads, and no API key / model / timeout values belong in
this module. The authoritative request-payload hash for Turn Advice jobs is
still computed by :meth:`maple_next.application.service.BattleApplication.payload_hash`
against ``TurnFactsSnapshot.to_canonical_dict()`` (see
``request_turn_advice``/``build_turn_advice_transport_request``); the
:class:`TurnAdviceRequest` built here is used only to render the
deterministic provider prompt/body for the fake/injected transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: Fixed and deterministic. Never derived from a live provider schema.
REQUESTED_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action_type": {"type": "string", "enum": ["MOVE", "SWITCH"]},
        "action_name": {"type": "string"},
        "opponent_prediction": {"type": "string"},
        "rationale": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action_type", "action_name", "opponent_prediction", "rationale"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class TurnAdviceRequest:
    """Immutable canonical request values for one Turn Advice job."""

    job_type: str
    session_id: str
    match_id: str
    generation: int
    turn_number: int
    battle_revision: int
    reviewed_turn_facts_id: str
    self_active: str
    opponent_active: str
    self_hp: str
    opponent_hp: str
    legal_moves: tuple[str, ...]
    legal_switches: tuple[str, ...]
    requested_output_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if self.job_type != "TURN_ADVICE":
            raise ValueError("job_type must be TURN_ADVICE")
        if self.turn_number < 1:
            raise ValueError("turn_number must be positive")
        if not self.legal_moves:
            raise ValueError("legal_moves must contain at least one entry")


def build_turn_advice_request(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    turn_number: int,
    battle_revision: int,
    reviewed_turn_facts_id: str,
    self_active: str,
    opponent_active: str,
    self_hp: str,
    opponent_hp: str,
    legal_moves: tuple[str, ...],
    legal_switches: tuple[str, ...],
) -> TurnAdviceRequest:
    """Build the canonical request from exact canonical-store values only."""

    return TurnAdviceRequest(
        job_type="TURN_ADVICE",
        session_id=session_id,
        match_id=match_id,
        generation=generation,
        turn_number=turn_number,
        battle_revision=battle_revision,
        reviewed_turn_facts_id=reviewed_turn_facts_id,
        self_active=self_active,
        opponent_active=opponent_active,
        self_hp=self_hp,
        opponent_hp=opponent_hp,
        legal_moves=tuple(legal_moves),
        legal_switches=tuple(legal_switches),
        requested_output_schema=REQUESTED_OUTPUT_SCHEMA,
    )


def build_provider_prompt(request: TurnAdviceRequest) -> str:
    """Deterministic natural-language prompt body. Pure function, no secrets."""

    moves = ", ".join(request.legal_moves)
    switches = ", ".join(request.legal_switches) if request.legal_switches else "(none)"
    return (
        "You are assisting a human Pokemon Champions player during a single "
        "Turn of an official match.\n"
        f"Your active Pokemon: {request.self_active} (HP bucket: {request.self_hp})\n"
        f"Opponent active Pokemon: {request.opponent_active} "
        f"(HP bucket: {request.opponent_hp})\n"
        f"Your legal moves this turn: {moves}\n"
        f"Your legal switch candidates this turn: {switches}\n"
        "Recommend exactly one action (a move you may use, or a switch "
        "candidate) for this turn.\n"
        "Respond with strict JSON only, matching exactly this shape, with no "
        "markdown, no code fence, and no additional commentary:\n"
        '{"action_type": "MOVE"|"SWITCH", "action_name": "<name>", '
        '"opponent_prediction": "<text>", "rationale": "<text>", '
        '"warnings": ["<text>", ...]}\n'
        "action_name must be taken only from the legal moves or legal switch "
        "candidates listed above, matching action_type."
    )


def build_provider_request_body(request: TurnAdviceRequest) -> dict[str, Any]:
    """Deterministic Gemini generateContent body. No model name or secrets here."""

    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": build_provider_prompt(request)}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": request.requested_output_schema,
        },
    }
