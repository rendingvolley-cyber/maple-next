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

from maple_next.domain.team_build import ChampionsTeamBuild, TeamSelectionProfile

CONTRACT_VERSION_V1: Final[str] = "maple-selection-advice.v1"
CONTRACT_VERSION_V2: Final[str] = "maple-selection-advice.v2"
CONTRACT_VERSION_V3: Final[str] = "maple-selection-advice.v3"
SELECTION_ADVICE_CONTRACT_VERSION_V1: Final[str] = CONTRACT_VERSION_V1
SELECTION_ADVICE_CONTRACT_VERSION_V2: Final[str] = CONTRACT_VERSION_V2
SELECTION_ADVICE_CONTRACT_VERSION_V3: Final[str] = CONTRACT_VERSION_V3
SELECTION_PROMPT_VERSION: Final[str] = "maple-selection-prompt.v1"
SELECTION_PROMPT_VERSION_V2: Final[str] = "maple-selection-prompt.v2"

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

REQUESTED_OUTPUT_SCHEMA_V3: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "chosen_package": {"type": "string"},
        "selected_three": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 3,
        },
        "lead": {"type": "string"},
        "intended_mega": {"type": ["string", "null"]},
        "selection_reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": [
        "chosen_package",
        "selected_three",
        "lead",
        "intended_mega",
        "selection_reason",
    ],
    "additionalProperties": False,
}

_SELECTION_INITIAL_PROMPT: Final[str] = (
    """You are assisting a human Pokémon Champions player during Team Selection
for one SINGLE_3 official match.

The request contains canonical facts confirmed by Maple.

Confirmed information may include self_team, opponent_team, and a detailed
self_team_build. self_team_build may be absent.

When self_team_build is absent, do not assume the player's moves, held items,
abilities, natures, or stat allocations.

The opponent's team contains names only. Do not state that an opponent has a
particular move, item, ability, nature, stat allocation, or role as a confirmed fact.

You may use general Pokémon Champions knowledge to interpret confirmed names,
but unconfirmed opponent details must remain uncertainty.

Before choosing, silently:
1. Evaluate all six members of self_team.
2. Consider the major threats represented by all six opponent names.
3. Compare multiple possible three-Pokémon combinations.
4. Prefer coherent and complementary purposes across the selected trio.
5. Avoid unnecessary role duplication that leaves a major threat unanswered.
6. Do not select only by input order, familiarity, apparent raw offense,
   or one favorable matchup.
7. Choose a lead useful against multiple plausible opponent leads.
8. Consider whether the other two selected Pokémon provide continuation
   when the lead matchup is unfavorable.
9. Use only exact names in self_team.
10. Select exactly three distinct Pokémon and one selected lead.

Follow requested_output_schema exactly.
Return strict JSON only. Do not add explanations, confidence, roles,
alternative teams, markdown, or additional fields."""
)

_FIXED_PACKAGE_SELECTION_PROMPT: Final[str] = (
    """You are assisting a human Pokémon Champions player during Team Selection
for one SINGLE_3 official match.

The request contains canonical facts confirmed by Maple, including the full
self_team_build and its human-authored selection_profile.
The human-authored Selection Profile is authoritative.
Its mode is fixed_packages and mixing_allowed is false.

You must choose exactly ONE defined package. Do not mix Pokémon between packages.
Compare the defined packages against all six confirmed opponent Pokémon. You are
choosing only (1) which package and (2) which member of that package should lead.
The package members and intended Mega are fixed by the human-authored build.

Use the confirmed self build information: moves, held items, abilities, natures,
and stat points. You may use general Pokémon Champions knowledge for confirmed
opponent names, but do not invent unconfirmed opponent moves, items, abilities,
natures, stat allocations, or roles as facts.

Copy chosen_package, selected_three, and intended_mega exactly from one package.
lead must be one member of that package. selection_reason must be concise.
Follow requested_output_schema exactly. Return strict JSON only, with no markdown
or additional fields."""
)


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
    selection_profile: TeamSelectionProfile | None = None

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
            if self.selection_profile is not None:
                raise ValueError("names-only selection request must not have a profile")
        else:
            if self.self_team_build.pokemon_names != tuple(self.self_team):
                raise ValueError("selection build names must match self_team")
            if self.self_team_build_sha256 != self.self_team_build.sha256():
                raise ValueError("selection build hash does not match build")
            if self.self_team_build.selection_profile is None:
                if self.contract_version != CONTRACT_VERSION_V2:
                    raise ValueError("legacy detailed selection request must use v2")
                if self.selection_profile is not None:
                    raise ValueError("v2 selection request must not have a profile")
            else:
                if self.contract_version != CONTRACT_VERSION_V3:
                    raise ValueError("profile selection request must use v3")
                if self.selection_profile != self.self_team_build.selection_profile:
                    raise ValueError("selection profile must match the bound build")
                if self.requested_output_schema != REQUESTED_OUTPUT_SCHEMA_V3:
                    raise ValueError("profile selection request must use v3 response schema")

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
        requested_output_schema=(
            REQUESTED_OUTPUT_SCHEMA_V3
            if self_team_build is not None
            and self_team_build.selection_profile is not None
            else REQUESTED_OUTPUT_SCHEMA
        ),
        contract_version=(
            CONTRACT_VERSION_V3
            if self_team_build is not None
            and self_team_build.selection_profile is not None
            else CONTRACT_VERSION_V2
            if self_team_build is not None
            else CONTRACT_VERSION_V1
        ),
        self_team_build=self_team_build,
        self_team_build_sha256=(
            self_team_build.sha256() if self_team_build is not None else None
        ),
        selection_profile=(
            self_team_build.selection_profile if self_team_build is not None else None
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
    if request.selection_profile is not None:
        payload["selection_profile"] = request.selection_profile.to_canonical_dict()
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
    """Build the deterministic, secret-free Initial Prompt v1."""

    canonical = json.dumps(
        canonical_request_dict(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prompt = (
        _FIXED_PACKAGE_SELECTION_PROMPT
        if request.selection_profile is not None
        else _SELECTION_INITIAL_PROMPT
    )
    return f"{prompt}\n\nCanonical request:\n{canonical}"


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
