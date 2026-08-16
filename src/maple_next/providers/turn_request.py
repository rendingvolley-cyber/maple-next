"""Canonical, deterministic Turn Advice request construction.

Mirrors the pattern established by ``selection_request.py``: every function
here is pure. Given the same canonical store values, the same request, dict,
encoding, and hash are produced every time. No network, no SQLite, no UI
widget access, no clock reads, and no API key / model / timeout values
belong in this module — those are transport configuration, never part of
the canonical strategic request.

This module is intentionally independent of ``workers.contracts.models`` and
of ``application.service`` — it defines its own self-contained offline
contract for Lane C of issue #31, built only on the read-only domain enums
(:class:`~maple_next.domain.enums.ActionType`,
:class:`~maple_next.domain.enums.HpBucket`) and the existing
:class:`~maple_next.domain.models.ReviewedBoardSnapshot` /
:class:`~maple_next.domain.models.StatStages` value objects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

from maple_next.domain.enums import ActionType
from maple_next.domain.models import ReviewedBoardSnapshot
from maple_next.domain.team_build import (
    ChampionsPokemonBuild,
    ChampionsTeamBuild,
)

#: Fixed contract identifiers. Bound and checked on every result.
CONTRACT_VERSION: Final[str] = "maple-turn-advice.v1"
CONTRACT_VERSION_V2: Final[str] = "maple-turn-advice.v2"
TURN_ADVICE_CONTRACT_VERSION_V1: Final[str] = CONTRACT_VERSION
TURN_ADVICE_CONTRACT_VERSION_V2: Final[str] = CONTRACT_VERSION_V2
TURN_PROMPT_VERSION: Final[str] = "maple-turn-prompt.v1"
JOB_TYPE: Final[str] = "TURN_ADVICE"

#: Fixed and deterministic. Never derived from a live provider schema.
REQUESTED_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
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
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
        },
        "opponent_prediction": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["MOVE", "SWITCH", "STATUS_OR_SETUP", "UNKNOWN"],
                },
                "predicted_action": {"type": ["string", "null"]},
                "summary": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["category", "predicted_action", "summary", "confidence"],
            "additionalProperties": False,
        },
    },
    "required": ["recommended_action", "reasons", "warnings", "opponent_prediction"],
    "additionalProperties": False,
}

_TURN_INITIAL_PROMPT: Final[str] = (
    """You are advising a human Pokémon Champions player for exactly one reviewed turn.

The request contains canonical facts confirmed by Maple.
HP values are buckets, not exact percentages. Do not convert a bucket to exact HP.

Only the current active Pokémon's HP and status are confirmed.
Do not assume the current HP or status of a benched switch target.

The opponent's build and remaining selected Pokémon are not confirmed.
Do not state opponent moves, item, ability, nature, stat allocation, speed relation,
damage range, or remaining team members as confirmed facts.

When the canonical request includes rules_context, it is authoritative for Pokémon
Champions-specific battle rules within its declared coverage; general Pokémon knowledge
and model training data must never override it.
You may use general Pokémon knowledge only as unconfirmed background to interpret
confirmed Pokémon and move names -- uncertainty must not be presented as confirmed fact.
Do not present a Pokémon Champions-specific rule or mechanic as confirmed fact unless it
is present in rules_context. When a recommendation materially depends on a Champions-specific
rule or mechanic that is absent from rules_context, surface that uncertainty as a warning
instead of asserting it. Never fabricate a Champions-specific mechanic, timer, format
detail, or restriction that is not present in rules_context.

Before choosing, silently compare every legal_actions entry.
For each action, consider:
1. Immediate value in the confirmed current board state.
2. Risk of losing the current active Pokémon or a necessary team resource.
3. Robustness against plausible opponent categories: MOVE, SWITCH,
   and STATUS_OR_SETUP.
4. Confirmed HP buckets, status, stat stages, weather, terrain, and side effects.
5. Confirmed player build details when present.
6. Whether a SWITCH is justified despite unknown bench HP/status.
7. Whether missing opponent information could change the conclusion.

Do not automatically prefer a broadly useful utility move, the highest apparent
damage move, staying in, switching, or an action recommended previously.
The same action may still be correct repeatedly when the confirmed board supports it.

Choose exactly one action from legal_actions with the best expected match value
across plausible opponent action categories supported by confirmed information.
Copy action_id, action_type, and action_name exactly.

Give 1-3 concise reasons based on decisive confirmed factors.
Give 0-5 concise warnings when uncertainty could materially change the recommendation.
Do not expose hidden chain-of-thought.

Predict the opponent conservatively. Use UNKNOWN when evidence is insufficient.
Set predicted_action to null unless a specific action is sufficiently supported.

The human alone decides and operates the game.
Do not execute a move, switch, keyboard input, controller input, or any other game action.
Follow requested_output_schema exactly and return strict JSON only."""
)


@dataclass(frozen=True, slots=True)
class LegalAction:
    """One canonical legal action offered to the human this turn.

    No alias, translation, or whitespace-trimming normalization is applied
    anywhere in this module: names must already be exact canonical-store
    values. ``owner_active`` is required for MOVE and must be omitted (left
    ``None``) for SWITCH; ``switch_target`` is required for SWITCH and must
    be omitted for MOVE.
    """

    action_id: str
    action_type: ActionType
    action_name: str
    owner_active: str | None = None
    switch_target: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("action_id must be non-empty")
        if not self.action_name or not self.action_name.strip():
            raise ValueError("action_name must be non-empty")
        if self.action_type is ActionType.MOVE:
            if self.owner_active is None or not self.owner_active.strip():
                raise ValueError("MOVE legal action requires owner_active")
            if self.switch_target is not None:
                raise ValueError("MOVE legal action must not carry switch_target")
        elif self.action_type is ActionType.SWITCH:
            if self.switch_target is None or not self.switch_target.strip():
                raise ValueError("SWITCH legal action requires switch_target")
            if self.owner_active is not None:
                raise ValueError("SWITCH legal action must not carry owner_active")
        else:  # pragma: no cover - ActionType is exhaustive today
            raise ValueError("action_type must be MOVE or SWITCH")


@dataclass(frozen=True, slots=True)
class TurnAdviceRequest:
    """Immutable canonical request values for one Turn Advice job."""

    contract_version: str
    job_type: str
    session_id: str
    match_id: str
    generation: int
    turn_number: int
    battle_revision: int
    reviewed_snapshot_id: str
    reviewed_snapshot_hash: str
    reviewed_snapshot: ReviewedBoardSnapshot
    self_active: str
    selected_three: tuple[str, str, str]
    legal_actions: tuple[LegalAction, ...]
    requested_output_schema: dict[str, Any]
    self_team_build: ChampionsTeamBuild | None = None
    self_team_build_sha256: str | None = None
    selected_three_builds: tuple[ChampionsPokemonBuild, ...] = ()
    self_active_build: ChampionsPokemonBuild | None = None

    def __post_init__(self) -> None:
        if self.contract_version not in {CONTRACT_VERSION, CONTRACT_VERSION_V2}:
            raise ValueError("contract_version must be a fixed Turn Advice contract version")
        if self.job_type != JOB_TYPE:
            raise ValueError("job_type must be TURN_ADVICE")
        if self.turn_number < 1:
            raise ValueError("turn_number must be positive")
        if len(set(self.selected_three)) != 3:
            raise ValueError("selected_three must contain three distinct names")
        if not self.self_active or not self.self_active.strip():
            raise ValueError("self_active must be explicit")
        if self.self_active != self.reviewed_snapshot.self_active:
            raise ValueError("self_active must match reviewed_snapshot.self_active exactly")
        if self.self_active not in self.selected_three:
            raise ValueError("self_active must be one of the applied selected_three")
        if not self.legal_actions:
            raise ValueError("legal_actions must not be empty")

        action_ids = [action.action_id for action in self.legal_actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("legal_actions must have unique action_id values")

        for action in self.legal_actions:
            if action.action_type is ActionType.MOVE:
                if action.owner_active != self.self_active:
                    raise ValueError("MOVE.owner_active must equal self_active exactly")
            elif action.action_type is ActionType.SWITCH:
                if action.switch_target not in self.selected_three:
                    raise ValueError("SWITCH.switch_target must be in selected_three")
                if action.switch_target == self.self_active:
                    raise ValueError("SWITCH.switch_target must not equal self_active")

        if self.self_team_build is None:
            if (
                self.self_team_build_sha256 is not None
                or self.selected_three_builds
                or self.self_active_build is not None
            ):
                raise ValueError("names-only turn request must not carry build details")
            if self.contract_version != CONTRACT_VERSION:
                raise ValueError("names-only turn request must use v1")
        else:
            if self.contract_version != CONTRACT_VERSION_V2:
                raise ValueError("detailed turn request must use v2")
            if self.self_team_build.pokemon_names != tuple(
                self.self_team_build.pokemon_names
            ):
                raise ValueError("invalid self team build")
            if self.self_team_build_sha256 != self.self_team_build.sha256():
                raise ValueError("self team build hash does not match build")
            expected_builds = self.self_team_build.selected_members(self.selected_three)
            if tuple(self.selected_three_builds) != expected_builds:
                raise ValueError("selected_three_builds do not match selected_three")
            if self.self_active_build != self.self_team_build.member_by_name(self.self_active):
                raise ValueError("self_active_build does not match self_active")


def compute_reviewed_snapshot_hash(reviewed_snapshot: ReviewedBoardSnapshot) -> str:
    """Deterministic SHA-256 of the reviewed-snapshot canonical content only."""

    encoded = json.dumps(
        reviewed_snapshot.to_canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_turn_advice_request(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    turn_number: int,
    battle_revision: int,
    reviewed_snapshot_id: str,
    reviewed_snapshot: ReviewedBoardSnapshot,
    self_active: str,
    selected_three: tuple[str, str, str],
    legal_actions: tuple[LegalAction, ...],
    self_team_build: ChampionsTeamBuild | None = None,
) -> TurnAdviceRequest:
    """Build the canonical request from exact canonical-store values only.

    Callers must pass the exact ``ReviewedBoardSnapshot`` and legal action
    values already stored for ``reviewed_snapshot_id`` — this function does
    not strip, translate, alias, or otherwise normalize names.
    """

    return TurnAdviceRequest(
        contract_version=(
            CONTRACT_VERSION_V2 if self_team_build is not None else CONTRACT_VERSION
        ),
        job_type=JOB_TYPE,
        session_id=session_id,
        match_id=match_id,
        generation=generation,
        turn_number=turn_number,
        battle_revision=battle_revision,
        reviewed_snapshot_id=reviewed_snapshot_id,
        reviewed_snapshot_hash=compute_reviewed_snapshot_hash(reviewed_snapshot),
        reviewed_snapshot=reviewed_snapshot,
        self_active=self_active,
        selected_three=selected_three,
        legal_actions=tuple(legal_actions),
        requested_output_schema=REQUESTED_OUTPUT_SCHEMA,
        self_team_build=self_team_build,
        self_team_build_sha256=(
            self_team_build.sha256() if self_team_build is not None else None
        ),
        selected_three_builds=(
            self_team_build.selected_members(selected_three)
            if self_team_build is not None
            else ()
        ),
        self_active_build=(
            self_team_build.member_by_name(self_active)
            if self_team_build is not None
            else None
        ),
    )


def _canonical_legal_action_dict(action: LegalAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type.value,
        "action_name": action.action_name,
        "owner_active": action.owner_active,
        "switch_target": action.switch_target,
    }


def canonical_request_dict(request: TurnAdviceRequest) -> dict[str, Any]:
    """Render the request as a plain dict. Key order does not affect hashing."""

    payload: dict[str, Any] = {
        "contract_version": request.contract_version,
        "job_type": request.job_type,
        "session_id": request.session_id,
        "match_id": request.match_id,
        "generation": request.generation,
        "turn_number": request.turn_number,
        "battle_revision": request.battle_revision,
        "reviewed_snapshot_id": request.reviewed_snapshot_id,
        "reviewed_snapshot_hash": request.reviewed_snapshot_hash,
        "reviewed_snapshot_facts": request.reviewed_snapshot.to_canonical_dict(),
        "self_active": request.self_active,
        "selected_three": list(request.selected_three),
        "legal_actions": [_canonical_legal_action_dict(a) for a in request.legal_actions],
        "requested_output_schema": request.requested_output_schema,
    }
    if request.self_team_build is not None:
        active_build = request.self_active_build
        if active_build is None:
            raise ValueError("detailed turn request is missing self_active_build")
        payload["contract_version"] = request.contract_version
        payload["self_team_build_sha256"] = request.self_team_build_sha256
        payload["selected_three_builds"] = [
            build.to_canonical_dict() for build in request.selected_three_builds
        ]
        payload["self_active_build"] = active_build.to_canonical_dict()
    return payload


def encode_canonical_request(request: TurnAdviceRequest) -> bytes:
    """Deterministic encoding: sorted keys, no whitespace, explicit separators."""

    return json.dumps(
        canonical_request_dict(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_payload_hash(request: TurnAdviceRequest) -> str:
    return hashlib.sha256(encode_canonical_request(request)).hexdigest()


def _render_provider_prompt_from_canonical_request(
    canonical_request: dict[str, Any],
) -> str:
    """Shared, secret-free Initial Prompt v1 renderer.

    Both the legacy ``TurnAdviceRequest`` prompt builder and the additive
    Bundle B rich-state request prompt builder
    (``providers/turn_advice_rich_state.py``) delegate to this single
    renderer so ``_TURN_INITIAL_PROMPT`` is never copied or independently
    reimplemented. Given the same canonical request dict, the output is
    byte-for-byte identical regardless of caller.
    """

    canonical = json.dumps(
        canonical_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{_TURN_INITIAL_PROMPT}\n\nCanonical request:\n{canonical}"


def _render_provider_request_body_from_prompt(
    prompt: str, requested_output_schema: dict[str, Any]
) -> dict[str, Any]:
    """Shared Gemini ``generateContent`` body renderer. No secrets, no model/endpoint."""

    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": requested_output_schema,
        },
    }


def build_provider_prompt(request: TurnAdviceRequest) -> str:
    """Build the deterministic, secret-free Initial Prompt v1."""

    return _render_provider_prompt_from_canonical_request(canonical_request_dict(request))


def build_provider_request_body(request: TurnAdviceRequest) -> dict[str, Any]:
    """Build the deterministic Gemini generateContent body without secrets."""

    return _render_provider_request_body_from_prompt(
        build_provider_prompt(request), request.requested_output_schema
    )
