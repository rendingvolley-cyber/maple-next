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

from maple_next.domain.team_build import ChampionsTeamBuild

CONTRACT_VERSION_V1: Final[str] = "maple-selection-advice.v1"
CONTRACT_VERSION_V2: Final[str] = "maple-selection-advice.v2"
SELECTION_ADVICE_CONTRACT_VERSION_V1: Final[str] = CONTRACT_VERSION_V1
SELECTION_ADVICE_CONTRACT_VERSION_V2: Final[str] = CONTRACT_VERSION_V2

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
    contract_version: str = CONTRACT_VERSION_V1
    self_team_build: ChampionsTeamBuild | None = None
    self_team_build_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.job_type != "SELECTION_ADVICE":
            raise ValueError("job_type must be SELECTION_ADVICE")
        if len(self.self_team) != 6:
            raise ValueError("self_team must contain exactly six entries")
        if len(self.opponent_team) != 6:
            raise ValueError("opponent_team must contain exactly six entries")
        if self.self_team_build is None:
            if self.self_team_build_sha256 is not None:
                raise ValueError("names-only selection request must not have a build hash")
            if self.contract_version != CONTRACT_VERSION_V1:
                raise ValueError("names-only selection request must use v1")
        else:
            if self.contract_version != CONTRACT_VERSION_V2:
                raise ValueError("detailed selection request must use v2")
            if self.self_team_build.pokemon_names != tuple(self.self_team):
                raise ValueError("selection build names must match self_team")
            if self.self_team_build_sha256 != self.self_team_build.sha256():
                raise ValueError("selection build hash does not match build")

    @property
    def request_version(self) -> str:
        return self.contract_version


def build_selection_advice_request(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    battle_revision: int,
    reviewed_selection_id: str,
    self_team: tuple[str, ...],
    opponent_team: tuple[str, ...],
    self_team_build: ChampionsTeamBuild | None = None,
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
        contract_version=(
            CONTRACT_VERSION_V2
            if self_team_build is not None
            else CONTRACT_VERSION_V1
        ),
        self_team_build=self_team_build,
        self_team_build_sha256=(
            self_team_build.sha256() if self_team_build is not None else None
        ),
    )


def canonical_request_dict(request: SelectionAdviceRequest) -> dict[str, Any]:
    """Render the request as a plain dict. Key order does not affect hashing."""

    payload: dict[str, Any] = {
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
    if request.self_team_build is not None:
        payload["contract_version"] = request.contract_version
        payload["self_team_build"] = request.self_team_build.to_canonical_dict()
        payload["self_team_build_sha256"] = request.self_team_build_sha256
    return payload


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
    prompt = (
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
    if request.self_team_build is not None:
        details = json.dumps(
            request.self_team_build.to_canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt += (
            "\nDetailed self-team build (confirmed; use only these values): "
            f"{details}\n"
            f"self_team_build_sha256={request.self_team_build_sha256}"
        )
    return prompt


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
