"""Canonical, deterministic Selection Advice request construction.

Every function here is pure: given the same canonical store values, the same
request, dict, encoding, and hash are produced every time. No network, no
SQLite, no UI widget access, no clock reads, and no API key / model /
timeout values belong in this module — those are transport configuration,
never part of the canonical strategic request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

#: Fixed and deterministic. Never derived from a live provider schema.
REQUESTED_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "selected_three": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
        "lead": {"type": "string"},
    },
    "required": ["selected_three", "lead"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class SelectionAdviceRequest:
    """Immutable canonical request values for one Selection Advice job."""

    job_type: str
    session_id: str
    match_id: str
    generation: int
    battle_revision: int
    reviewed_selection_id: str
    self_team: tuple[str, ...]
    opponent_team: tuple[str, ...]
    requested_output_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if self.job_type != "SELECTION_ADVICE":
            raise ValueError("job_type must be SELECTION_ADVICE")
        if len(self.self_team) != 6:
            raise ValueError("self_team must contain exactly six entries")
        if len(self.opponent_team) != 6:
            raise ValueError("opponent_team must contain exactly six entries")


def build_selection_advice_request(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    battle_revision: int,
    reviewed_selection_id: str,
    self_team: tuple[str, ...],
    opponent_team: tuple[str, ...],
) -> SelectionAdviceRequest:
    """Build the canonical request from exact canonical-store values only.

    Callers must pass the exact six-name tuples already stored in
    ``reviewed_selection_facts`` for ``reviewed_selection_id`` — this
    function does not strip, translate, alias, or otherwise normalize names.
    """

    return SelectionAdviceRequest(
        job_type="SELECTION_ADVICE",
        session_id=session_id,
        match_id=match_id,
        generation=generation,
        battle_revision=battle_revision,
        reviewed_selection_id=reviewed_selection_id,
        self_team=tuple(self_team),
        opponent_team=tuple(opponent_team),
        requested_output_schema=REQUESTED_OUTPUT_SCHEMA,
    )


def canonical_request_dict(request: SelectionAdviceRequest) -> dict[str, Any]:
    """Render the request as a plain dict. Key order does not affect hashing."""

    return {
        "job_type": request.job_type,
        "session_id": request.session_id,
        "match_id": request.match_id,
        "generation": request.generation,
        "battle_revision": request.battle_revision,
        "reviewed_selection_id": request.reviewed_selection_id,
        "self_team": list(request.self_team),
        "opponent_team": list(request.opponent_team),
        "requested_output_schema": request.requested_output_schema,
    }


def encode_canonical_request(request: SelectionAdviceRequest) -> bytes:
    """Deterministic encoding: sorted keys, no whitespace, explicit separators."""

    return json.dumps(
        canonical_request_dict(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_payload_hash(request: SelectionAdviceRequest) -> str:
    return hashlib.sha256(encode_canonical_request(request)).hexdigest()


def build_provider_prompt(request: SelectionAdviceRequest) -> str:
    """Deterministic natural-language prompt body. Pure function, no secrets."""

    self_team = ", ".join(request.self_team)
    opponent_team = ", ".join(request.opponent_team)
    return (
        "You are assisting a human Pokemon Champions player during Team "
        "Selection for a single official match.\n"
        f"Your own confirmed team (exact six, in order): {self_team}\n"
        f"The opponent's confirmed team (exact six, in order): {opponent_team}\n"
        "Choose exactly three Pokemon from your own team above to bring into "
        "the match, and choose which of those three should lead.\n"
        "Respond with strict JSON only, matching exactly this shape, with no "
        "markdown, no code fence, and no additional commentary:\n"
        '{"selected_three": ["<name>", "<name>", "<name>"], "lead": "<name>"}\n'
        "selected_three must contain three distinct names taken only from "
        "your own confirmed team above. lead must be one of those three names."
    )


def build_provider_request_body(request: SelectionAdviceRequest) -> dict[str, Any]:
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
