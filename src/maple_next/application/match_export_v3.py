"""Bundle B: ``maple-match.v3`` export -- rich-state-contract matches only.

Strictly additive. This module never imports ``application/match_service.py``
and never changes the meaning, reader, or output of the legacy
``maple-match.v1``/``.v2`` exporter defined there -- that file has zero lines
changed by Bundle B. ``maple-match.v3`` is only ever produced for a match
that actually used the Bundle B rich-state contract (the caller supplies at
least one ``ConfirmedTurnRecord``); every other match keeps using the legacy
exporter unchanged.

v3 embeds versioned ``ConfirmedTurnState``/``ActionResultDelta`` records with
per-field provenance and knowledge (CONFIRMED/UNKNOWN, never coerced to a
default), the final confirmed legal actions, and a fixed evidence metadata
reference (id/relative-path/sha256 only -- never raw image bytes). It never
contains a raw provider request/response body, HTTP header, prompt string,
API key, or other secret; nothing in this module imports a provider
transport module.

Export safety mirrors the existing ``MatchApplication`` convention exactly:
a repository-external temporary file, ``fsync``, atomic ``os.replace``,
strict parse on read-back, idempotent re-export (same content -> same file,
no rewrite), and a stable SHA-256 over the canonical encoded bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.application.turn_legal_action_boundary import build_confirmed_legal_actions_input
from maple_next.domain.enums import ActionType
from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FixedEvidenceMetadata,
    TurnIdentity,
    TurnStateError,
    TurnStateIdentityError,
    field_delta_from_json,
    field_delta_to_json,
    known_from_json,
    known_to_json,
    side_delta_from_json,
    side_delta_to_json,
    side_state_from_json,
    side_state_to_json,
)

MATCH_EXPORT_SCHEMA_VERSION_V3 = "maple-match.v3"

#: Distinct from the Bundle B request/projection contract versions -- this
#: is what an exported rich turn's ``rich_state`` block advertises.
RICH_STATE_EXPORT_CONTRACT_VERSION = "maple-match-rich-state.v1"

_REQUIRED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "match_id",
        "generation",
        "outcome",
        "ended_at_utc",
        "final_battle_revision",
        "selection",
        "turns",
        "action_history",
    }
)

_REQUIRED_SELECTION_KEYS = frozenset({"self_team", "opponent_team", "selected_three", "lead"})

_REQUIRED_LEGACY_TURN_KEYS = frozenset(
    {
        "turn_number",
        "reviewed_facts",
        "advice",
        "self_executed_action",
        "opponent_executed_action",
        "action_order",
        "recorded_at_utc",
        "actual_action",
    }
)

#: Nested keys that must never appear anywhere in a v3 export -- a raw
#: provider request/response, prompt text, HTTP header, or credential.
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "headers",
        "endpoint",
        "prompt",
        "provider_request",
        "provider_response",
        "raw_request",
        "raw_response",
        "image_base64",
        "image_bytes",
        "base64",
        "generationConfig",
        "contents",
    }
)

_REQUIRED_RICH_STATE_KEYS = frozenset(
    {
        "contract_version",
        "confirmed_turn_state",
        "source_action_result_delta",
        "confirmed_legal_actions",
        "evidence",
    }
)


class MatchExportV3Error(Exception):
    """Fail-closed base error for the v3 export contract."""


@dataclass(frozen=True, slots=True)
class ConfirmedTurnRecord:
    """One turn's rich-state record bundle supplied by the caller for export.

    The caller is responsible for loading these already-persisted, already-
    confirmed objects (via the existing, unmodified
    ``TurnStateStoreMixin``); this module performs no persistence access of
    its own.
    """

    confirmed_state: ConfirmedTurnState
    source_delta: ActionResultDelta | None
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...]
    evidence: FixedEvidenceMetadata | None


def _identity_to_json(identity: TurnIdentity) -> dict[str, Any]:
    return {
        "session_id": identity.session_id,
        "match_id": identity.match_id,
        "generation": identity.generation,
        "turn_id": identity.turn_id,
        "turn_number": identity.turn_number,
        "battle_revision": identity.battle_revision,
    }


def _confirmed_state_to_json(state: ConfirmedTurnState) -> dict[str, Any]:
    return {
        "confirmed_state_id": state.confirmed_state_id,
        "previous_confirmed_state_id": state.previous_confirmed_state_id,
        "identity": _identity_to_json(state.identity),
        "self_side": side_state_to_json(state.self_side),
        "opponent_side": side_state_to_json(state.opponent_side),
        "weather": known_to_json(state.weather),
        "terrain": known_to_json(state.terrain),
        "confirmation": {
            "confirmed_by_human": state.confirmation.confirmed_by_human,
            "confirmed_at_utc": state.confirmation.confirmed_at_utc,
            "provenance": state.confirmation.provenance,
        },
        "evidence_id": state.evidence_id,
    }


def _delta_to_json(delta: ActionResultDelta) -> dict[str, Any]:
    return {
        "delta_id": delta.delta_id,
        "identity": _identity_to_json(delta.identity),
        "based_on_confirmed_state_id": delta.based_on_confirmed_state_id,
        "self_side": side_delta_to_json(delta.self_side),
        "opponent_side": side_delta_to_json(delta.opponent_side),
        "weather": field_delta_to_json(delta.weather),
        "terrain": field_delta_to_json(delta.terrain),
        "confirmation": {
            "confirmed_by_human": delta.confirmation.confirmed_by_human,
            "confirmed_at_utc": delta.confirmation.confirmed_at_utc,
            "provenance": delta.confirmation.provenance,
        },
    }


def _legal_action_to_json(selection: ConfirmedLegalActionSelection) -> dict[str, Any]:
    return {
        "confirmation_id": selection.confirmation_id,
        "identity": _identity_to_json(selection.identity),
        "action_type": selection.action_type.value,
        "action_name": selection.action_name,
        "confirmed_by_human": selection.confirmation.confirmed_by_human,
        "confirmed_at_utc": selection.confirmation.confirmed_at_utc,
        "provenance": selection.confirmation.provenance,
        "source_prefill_id": selection.source_prefill_id,
    }


def _evidence_to_json(evidence: FixedEvidenceMetadata) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "relative_path": evidence.relative_path,
        "sha256": evidence.sha256,
        "recorded_at_utc": evidence.recorded_at_utc,
    }


def build_match_export_v3_payload(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    outcome: MatchOutcomeRecord,
    turns: tuple[ConfirmedTurnRecord, ...],
    selection: dict[str, Any] | None = None,
    action_history: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build the v3 export payload. Fails closed on any identity mismatch.

    ``turns`` must be non-empty -- v3 is only produced for a match that
    actually used the rich-state contract. Each turn's confirmed legal
    actions and, when present, evidence and source delta are embedded
    verbatim (per-field knowledge/provenance intact, no backfill, no raw
    bytes/secrets).

    ``selection``/``action_history`` are optional here (this standalone
    builder predates the repository-backed integration in
    ``application/match_service.py``, whose payload always supplies real
    values for both) -- when omitted, a minimal but strictly valid
    placeholder is used so the result always satisfies
    :func:`parse_match_export_v3`'s required top-level keys.
    """

    if not turns:
        raise MatchExportV3Error("V3_EXPORT_REQUIRES_AT_LEAST_ONE_RICH_STATE_TURN")

    turn_payloads: list[dict[str, Any]] = []
    for record in turns:
        state = record.confirmed_state
        if state.identity.session_id != session_id or state.identity.match_id != match_id:
            raise MatchExportV3Error("V3_EXPORT_TURN_IDENTITY_MISMATCH")
        for legal_action_selection in record.confirmed_legal_actions:
            if legal_action_selection.identity != state.identity:
                raise MatchExportV3Error("V3_EXPORT_LEGAL_ACTION_IDENTITY_MISMATCH")
        if record.source_delta is not None and (
            record.source_delta.based_on_confirmed_state_id != state.previous_confirmed_state_id
        ):
            raise MatchExportV3Error("V3_EXPORT_SOURCE_DELTA_MISMATCH")

        turn_payloads.append(
            {
                "turn_number": state.identity.turn_number,
                "battle_revision": state.identity.battle_revision,
                "confirmed_turn_state": _confirmed_state_to_json(state),
                "source_action_result_delta": (
                    _delta_to_json(record.source_delta) if record.source_delta is not None else None
                ),
                "confirmed_legal_actions": [
                    _legal_action_to_json(s) for s in record.confirmed_legal_actions
                ],
                "evidence": (
                    _evidence_to_json(record.evidence) if record.evidence is not None else None
                ),
                "compatibility_reviewed_facts": {
                    "self_active": known_to_json(state.self_side.active),
                    "opponent_active": known_to_json(state.opponent_side.active),
                    "self_hp_bucket": known_to_json(state.self_side.hp_bucket),
                    "opponent_hp_bucket": known_to_json(state.opponent_side.hp_bucket),
                    "self_status": known_to_json(state.self_side.status),
                    "opponent_status": known_to_json(state.opponent_side.status),
                    "weather": known_to_json(state.weather),
                    "terrain": known_to_json(state.terrain),
                },
            }
        )

    return {
        "schema_version": MATCH_EXPORT_SCHEMA_VERSION_V3,
        "session_id": session_id,
        "match_id": match_id,
        "generation": generation,
        "outcome": outcome.outcome.value,
        "ended_at_utc": outcome.ended_at_utc,
        "final_battle_revision": outcome.final_battle_revision,
        "selection": (
            selection
            if selection is not None
            else {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""}
        ),
        "turns": turn_payloads,
        "action_history": list(action_history),
    }


def validate_confirmed_states_for_export(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    outcome: MatchOutcomeRecord,
    confirmed_states: tuple[ConfirmedTurnState, ...],
) -> None:
    """Fail-closed identity/chain validation before any v3 payload is built.

    Requires: the outcome's own identity matches the export session exactly;
    every confirmed state belongs to the exact session/match/generation;
    unique state ids; unique turn_number (Bundle A's chain rules increment
    turn_number and battle_revision together by exactly one on every
    transition, so a valid chain never has two different confirmed states
    for the same turn_number); a strictly non-decreasing
    (turn_number, battle_revision) ordering; every non-first state's
    ``previous_confirmed_state_id`` linking to the immediately preceding
    exported state; and no state whose ``battle_revision`` exceeds
    ``outcome.final_battle_revision``.
    """

    if outcome.session_id != session_id or outcome.match_id != match_id:
        raise MatchExportV3Error("V3_EXPORT_OUTCOME_SESSION_MATCH_MISMATCH")
    if outcome.generation != generation:
        raise MatchExportV3Error("V3_EXPORT_OUTCOME_GENERATION_MISMATCH")

    seen_state_ids: set[str] = set()
    seen_turn_numbers: set[int] = set()
    seen_turn_revisions: set[tuple[int, int]] = set()
    previous_key: tuple[int, int] | None = None
    previous_state: ConfirmedTurnState | None = None
    for state in confirmed_states:
        identity = state.identity
        if (
            identity.session_id != session_id
            or identity.match_id != match_id
            or identity.generation != generation
        ):
            raise MatchExportV3Error("V3_EXPORT_STATE_FOREIGN_IDENTITY")
        if state.confirmed_state_id in seen_state_ids:
            raise MatchExportV3Error("V3_EXPORT_DUPLICATE_STATE_ID")
        seen_state_ids.add(state.confirmed_state_id)
        if identity.turn_number in seen_turn_numbers:
            raise MatchExportV3Error("V3_EXPORT_CONTRADICTORY_STATE_SAME_TURN")
        seen_turn_numbers.add(identity.turn_number)
        key = (identity.turn_number, identity.battle_revision)
        if key in seen_turn_revisions:
            raise MatchExportV3Error("V3_EXPORT_DUPLICATE_TURN_REVISION")
        seen_turn_revisions.add(key)
        if previous_key is not None and key < previous_key:
            raise MatchExportV3Error("V3_EXPORT_STATE_ORDER_NOT_INCREASING")
        previous_key = key
        if identity.battle_revision > outcome.final_battle_revision:
            raise MatchExportV3Error("V3_EXPORT_STATE_BEYOND_FINAL_REVISION")
        if previous_state is not None and (
            state.previous_confirmed_state_id != previous_state.confirmed_state_id
        ):
            raise MatchExportV3Error("V3_EXPORT_BROKEN_PREVIOUS_STATE_LINKAGE")
        previous_state = state


_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_evidence_hash_shape(sha256: str) -> None:
    """Reject anything that is not exactly 64 valid hexadecimal characters.

    ``"z" * 64`` (right length, wrong alphabet) is rejected, not just a
    length check.
    """

    if not isinstance(sha256, str) or _HEX64_RE.match(sha256) is None:
        raise MatchExportV3Error("V3_EXPORT_INVALID_EVIDENCE_HASH_SHAPE")


def validate_delta_chain_for_export(
    *,
    session_id: str,
    match_id: str,
    generation: int,
    confirmed_states: tuple[ConfirmedTurnState, ...],
    deltas: tuple[ActionResultDelta, ...],
) -> dict[str, ActionResultDelta]:
    """Map each confirmed state id to its (at most one) source delta, fail closed.

    Never a dict comprehension that silently overwrites a duplicate:
    a duplicate ``delta_id``, two deltas claiming the same chain position
    (same ``based_on_confirmed_state_id``), a foreign session/match/
    generation, a delta whose based-on state isn't among the exported
    ``confirmed_states``, or a delta whose own identity doesn't match that
    based-on state's identity (Bundle A's convention: a delta shares the
    identity of the confirmed state it describes a change *from*) all fail
    the whole export closed rather than picking one silently.
    """

    confirmed_by_id = {state.confirmed_state_id: state for state in confirmed_states}
    seen_delta_ids: set[str] = set()
    delta_by_based_on: dict[str, ActionResultDelta] = {}
    for delta in deltas:
        if delta.delta_id in seen_delta_ids:
            raise MatchExportV3Error("V3_EXPORT_DUPLICATE_DELTA_ID")
        seen_delta_ids.add(delta.delta_id)
        identity = delta.identity
        if (
            identity.session_id != session_id
            or identity.match_id != match_id
            or identity.generation != generation
        ):
            raise MatchExportV3Error("V3_EXPORT_DELTA_FOREIGN_IDENTITY")
        if not delta.based_on_confirmed_state_id:
            raise MatchExportV3Error("V3_EXPORT_DELTA_MISSING_BASED_ON_STATE")
        based_on_state = confirmed_by_id.get(delta.based_on_confirmed_state_id)
        if based_on_state is None:
            raise MatchExportV3Error("V3_EXPORT_DELTA_BASED_ON_STATE_NOT_FOUND")
        if identity != based_on_state.identity:
            raise MatchExportV3Error("V3_EXPORT_DELTA_IDENTITY_MISMATCH")
        if delta.based_on_confirmed_state_id in delta_by_based_on:
            raise MatchExportV3Error("V3_EXPORT_AMBIGUOUS_DELTA_CHAIN_POSITION")
        delta_by_based_on[delta.based_on_confirmed_state_id] = delta
    return delta_by_based_on


def validate_legal_actions_for_export(
    identity: TurnIdentity, actions: tuple[ConfirmedLegalActionSelection, ...]
) -> None:
    """Re-run the accepted Bundle A legal-action boundary at export time.

    Does not rely on validation having already happened when the actions
    were originally confirmed -- every export re-proves that every action
    is a ``ConfirmedLegalActionSelection`` bound to ``identity``, non-blank,
    non-duplicate, and MOVE/SWITCH-valid, then additionally rejects a
    duplicate ``confirmation_id`` (a distinct concern from the boundary's
    own duplicate-name check).
    """

    try:
        build_confirmed_legal_actions_input(identity, actions)
    except (TurnStateIdentityError, ValueError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_LEGAL_ACTION_BOUNDARY_REJECTED:{exc}") from exc

    seen_confirmation_ids: set[str] = set()
    for action in actions:
        if action.confirmation_id in seen_confirmation_ids:
            raise MatchExportV3Error("V3_EXPORT_DUPLICATE_LEGAL_ACTION_CONFIRMATION_ID")
        seen_confirmation_ids.add(action.confirmation_id)


def _rich_state_block(record: ConfirmedTurnRecord) -> dict[str, Any]:
    state = record.confirmed_state
    return {
        "contract_version": RICH_STATE_EXPORT_CONTRACT_VERSION,
        "confirmed_turn_state": _confirmed_state_to_json(state),
        "source_action_result_delta": (
            _delta_to_json(record.source_delta) if record.source_delta is not None else None
        ),
        "confirmed_legal_actions": [
            _legal_action_to_json(s) for s in record.confirmed_legal_actions
        ],
        "evidence": (
            _evidence_to_json(record.evidence) if record.evidence is not None else None
        ),
    }


def build_integrated_match_export_v3_payload(
    *,
    legacy_payload: dict[str, Any],
    rich_turns: dict[int, ConfirmedTurnRecord],
) -> dict[str, Any]:
    """Additively upgrade an already-built legacy export payload to v3.

    ``legacy_payload`` must be exactly what the unmodified legacy exporter
    (``application/match_service.py``) would build for this match (schema
    v1 or v2) -- every legacy top-level and per-turn field is retained
    verbatim. Only ``schema_version`` is overridden to
    :data:`MATCH_EXPORT_SCHEMA_VERSION_V3`, and each turn whose
    ``turn_number`` has a matching entry in ``rich_turns`` gains one
    additional ``rich_state`` key. Turns without a matching rich record are
    left completely untouched -- this function never backfills a missing
    legacy turn with a guessed rich value.
    """

    payload = dict(legacy_payload)
    payload["schema_version"] = MATCH_EXPORT_SCHEMA_VERSION_V3
    new_turns: list[dict[str, Any]] = []
    for turn_payload in legacy_payload["turns"]:
        turn_payload = dict(turn_payload)
        record = rich_turns.get(int(turn_payload["turn_number"]))
        if record is not None:
            turn_payload["rich_state"] = _rich_state_block(record)
        new_turns.append(turn_payload)
    payload["turns"] = new_turns
    return payload


def _encode_payload(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def compute_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_encode_payload(payload)).hexdigest()


def _find_forbidden_key(node: Any, *, path: str = "$") -> str | None:
    """Recursively search for any secret/provider/prompt/header field name.

    ``advice.source_type``/``advice.model`` are legitimate legacy fields and
    are not in :data:`_FORBIDDEN_KEYS`, so they are never falsely rejected.
    """

    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_KEYS:
                return f"{path}.{key}"
            found = _find_forbidden_key(value, path=f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found = _find_forbidden_key(item, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


_ALLOWED_IDENTITY_KEYS = frozenset(
    {"session_id", "match_id", "generation", "turn_id", "turn_number", "battle_revision"}
)
_ALLOWED_CONFIRMATION_KEYS = frozenset({"confirmed_by_human", "confirmed_at_utc", "provenance"})
_ALLOWED_CONFIRMED_STATE_KEYS = frozenset(
    {
        "confirmed_state_id",
        "previous_confirmed_state_id",
        "identity",
        "self_side",
        "opponent_side",
        "weather",
        "terrain",
        "confirmation",
        "evidence_id",
    }
)
_ALLOWED_DELTA_KEYS = frozenset(
    {
        "delta_id",
        "identity",
        "based_on_confirmed_state_id",
        "self_side",
        "opponent_side",
        "weather",
        "terrain",
        "confirmation",
    }
)
_ALLOWED_LEGAL_ACTION_KEYS = frozenset(
    {
        "confirmation_id",
        "identity",
        "action_type",
        "action_name",
        "confirmed_by_human",
        "confirmed_at_utc",
        "provenance",
        "source_prefill_id",
    }
)
_ALLOWED_EVIDENCE_KEYS = frozenset({"evidence_id", "relative_path", "sha256", "recorded_at_utc"})


def _reject_unexpected_keys(payload: Any, allowed: frozenset[str], *, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MatchExportV3Error(f"V3_EXPORT_{label}_MUST_BE_OBJECT")
    unexpected = set(payload.keys()) - allowed
    if unexpected:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_UNEXPECTED_KEYS:{sorted(unexpected)}")
    missing = allowed - payload.keys()
    if missing:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_MISSING_KEYS:{sorted(missing)}")
    return payload


def _validate_known_shape(payload: Any, *, label: str) -> None:
    if not isinstance(payload, dict):
        raise MatchExportV3Error(f"V3_EXPORT_{label}_MUST_BE_OBJECT")
    status = payload.get("status")
    if status not in {"CONFIRMED", "UNKNOWN"}:
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_INVALID_KNOWLEDGE_STATUS:{label}")
    if status == "UNKNOWN" and "value" in payload:
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_UNKNOWN_CARRIES_VALUE:{label}")
    allowed = {"status", "provenance_chain"} if status == "UNKNOWN" else {
        "status",
        "value",
        "provenance_chain",
    }
    _reject_unexpected_keys(payload, frozenset(allowed), label=f"RICH_STATE_KNOWN:{label}")
    if status == "CONFIRMED" and not payload.get("provenance_chain"):
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_MISSING_PROVENANCE:{label}")


def _validate_field_delta_shape(payload: Any, *, label: str) -> None:
    if not isinstance(payload, dict):
        raise MatchExportV3Error(f"V3_EXPORT_{label}_MUST_BE_OBJECT")
    observation = payload.get("observation")
    if observation not in {"CHANGED", "UNCHANGED", "UNKNOWN"}:
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_INVALID_OBSERVATION:{label}")
    if observation == "CHANGED" and payload.get("after_value") is None:
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_CHANGED_WITHOUT_VALUE:{label}")
    if observation != "CHANGED" and payload.get("after_value") is not None:
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_UNCHANGED_CARRIES_VALUE:{label}")
    allowed = (
        {"observation", "after_value", "provenance_chain"}
        if observation == "CHANGED"
        else {"observation", "provenance_chain"}
    )
    _reject_unexpected_keys(payload, frozenset(allowed), label=f"RICH_STATE_FIELD_DELTA:{label}")


_SIDE_STATE_FIELDS = (
    "active",
    "hp_bucket",
    "status",
    "attack_stage",
    "defense_stage",
    "special_attack_stage",
    "special_defense_stage",
    "speed_stage",
    "accuracy_stage",
    "evasion_stage",
    "side_effects",
)


def _validate_side_state_shape(payload: Any, *, label: str) -> None:
    _reject_unexpected_keys(
        payload, frozenset(_SIDE_STATE_FIELDS), label=f"RICH_STATE_SIDE:{label}"
    )
    for field_name in _SIDE_STATE_FIELDS:
        _validate_known_shape(payload[field_name], label=f"{label}.{field_name}")


def _validate_side_delta_shape(payload: Any, *, label: str) -> None:
    _reject_unexpected_keys(
        payload, frozenset(_SIDE_STATE_FIELDS), label=f"RICH_STATE_SIDE_DELTA:{label}"
    )
    for field_name in _SIDE_STATE_FIELDS:
        _validate_field_delta_shape(payload[field_name], label=f"{label}.{field_name}")


def _identity_from_json(payload: Any, *, label: str) -> TurnIdentity:
    _reject_unexpected_keys(payload, _ALLOWED_IDENTITY_KEYS, label=f"{label}_IDENTITY")
    try:
        return TurnIdentity(
            session_id=str(payload["session_id"]),
            match_id=str(payload["match_id"]),
            generation=int(payload["generation"]),
            turn_id=str(payload["turn_id"]),
            turn_number=int(payload["turn_number"]),
            battle_revision=int(payload["battle_revision"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_IDENTITY:{exc}") from exc


def _confirmation_from_json(payload: Any, *, label: str) -> ConfirmationMeta:
    _reject_unexpected_keys(payload, _ALLOWED_CONFIRMATION_KEYS, label=f"{label}_CONFIRMATION")
    try:
        return ConfirmationMeta(
            confirmed_by_human=bool(payload["confirmed_by_human"]),
            confirmed_at_utc=str(payload["confirmed_at_utc"]),
            provenance=str(payload["provenance"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_CONFIRMATION:{exc}") from exc


def _confirmed_state_from_json(payload: Any, *, turn_number: object) -> ConfirmedTurnState:
    label = f"RICH_STATE_STATE:turn={turn_number}"
    _reject_unexpected_keys(payload, _ALLOWED_CONFIRMED_STATE_KEYS, label=label)
    identity = _identity_from_json(payload["identity"], label=label)
    _validate_side_state_shape(payload["self_side"], label=f"{label}.self_side")
    _validate_side_state_shape(payload["opponent_side"], label=f"{label}.opponent_side")
    _validate_known_shape(payload["weather"], label=f"{label}.weather")
    _validate_known_shape(payload["terrain"], label=f"{label}.terrain")
    confirmation = _confirmation_from_json(payload["confirmation"], label=label)
    try:
        return ConfirmedTurnState(
            confirmed_state_id=str(payload["confirmed_state_id"]),
            identity=identity,
            previous_confirmed_state_id=payload["previous_confirmed_state_id"],
            self_side=side_state_from_json(payload["self_side"]),
            opponent_side=side_state_from_json(payload["opponent_side"]),
            weather=known_from_json(payload["weather"]),
            terrain=known_from_json(payload["terrain"]),
            confirmation=confirmation,
            evidence_id=payload["evidence_id"],
        )
    except (TurnStateError, ValueError, TypeError, KeyError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_STATE:{exc}") from exc


def _delta_from_json(payload: Any, *, turn_number: object) -> ActionResultDelta:
    label = f"RICH_STATE_DELTA:turn={turn_number}"
    _reject_unexpected_keys(payload, _ALLOWED_DELTA_KEYS, label=label)
    identity = _identity_from_json(payload["identity"], label=label)
    _validate_side_delta_shape(payload["self_side"], label=f"{label}.self_side")
    _validate_side_delta_shape(payload["opponent_side"], label=f"{label}.opponent_side")
    _validate_field_delta_shape(payload["weather"], label=f"{label}.weather")
    _validate_field_delta_shape(payload["terrain"], label=f"{label}.terrain")
    confirmation = _confirmation_from_json(payload["confirmation"], label=label)
    try:
        return ActionResultDelta(
            delta_id=str(payload["delta_id"]),
            identity=identity,
            based_on_confirmed_state_id=str(payload["based_on_confirmed_state_id"]),
            self_side=side_delta_from_json(payload["self_side"]),
            opponent_side=side_delta_from_json(payload["opponent_side"]),
            weather=field_delta_from_json(payload["weather"]),
            terrain=field_delta_from_json(payload["terrain"]),
            confirmation=confirmation,
        )
    except (TurnStateError, ValueError, TypeError, KeyError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_DELTA:{exc}") from exc


def _legal_action_from_json(
    payload: Any, *, turn_number: object
) -> ConfirmedLegalActionSelection:
    label = f"RICH_STATE_LEGAL_ACTION:turn={turn_number}"
    _reject_unexpected_keys(payload, _ALLOWED_LEGAL_ACTION_KEYS, label=label)
    identity = _identity_from_json(payload["identity"], label=label)
    confirmation = _confirmation_from_json(
        {
            "confirmed_by_human": payload["confirmed_by_human"],
            "confirmed_at_utc": payload["confirmed_at_utc"],
            "provenance": payload["provenance"],
        },
        label=label,
    )
    try:
        action_type = ActionType(str(payload["action_type"]))
    except ValueError as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_ACTION_TYPE:{exc}") from exc
    try:
        return ConfirmedLegalActionSelection(
            confirmation_id=str(payload["confirmation_id"]),
            identity=identity,
            action_type=action_type,
            action_name=str(payload["action_name"]),
            confirmation=confirmation,
            source_prefill_id=payload["source_prefill_id"],
        )
    except (TurnStateError, ValueError, TypeError, KeyError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_LEGAL_ACTION:{exc}") from exc


def _evidence_from_json(payload: Any, *, turn_number: object) -> FixedEvidenceMetadata:
    label = f"RICH_STATE_EVIDENCE:turn={turn_number}"
    _reject_unexpected_keys(payload, _ALLOWED_EVIDENCE_KEYS, label=label)
    validate_evidence_hash_shape(payload.get("sha256"))
    try:
        return FixedEvidenceMetadata(
            evidence_id=str(payload["evidence_id"]),
            relative_path=str(payload["relative_path"]),
            sha256=str(payload["sha256"]),
            recorded_at_utc=str(payload["recorded_at_utc"]),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise MatchExportV3Error(f"V3_EXPORT_{label}_INVALID_EVIDENCE:{exc}") from exc


def _validate_rich_state_block(block: Any, *, turn_number: object) -> None:
    """Strictly reconstruct every Bundle A object embedded in this rich turn.

    Delegates to the real domain constructors and codecs
    (:func:`known_from_json`, :func:`side_state_from_json`,
    :func:`field_delta_from_json`, :func:`side_delta_from_json`,
    ``ConfirmedTurnState``, ``ActionResultDelta``,
    ``ConfirmedLegalActionSelection``, ``FixedEvidenceMetadata``) so their
    own invariants participate in validation, rather than re-implementing a
    parallel, weaker subset. Also rejects any unexpected key in every fixed
    structure and cross-validates identity across the confirmed state,
    delta, legal actions, and evidence within this one turn.
    """

    _reject_unexpected_keys(
        block, _REQUIRED_RICH_STATE_KEYS, label=f"RICH_STATE:turn={turn_number}"
    )
    if block["contract_version"] != RICH_STATE_EXPORT_CONTRACT_VERSION:
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_CONTRACT_VERSION_MISMATCH:turn={turn_number}"
        )

    state = _confirmed_state_from_json(block["confirmed_turn_state"], turn_number=turn_number)

    delta_payload = block["source_action_result_delta"]
    delta = (
        _delta_from_json(delta_payload, turn_number=turn_number)
        if delta_payload is not None
        else None
    )
    if delta is not None and delta.identity != state.identity:
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_DELTA_STATE_IDENTITY_MISMATCH:turn={turn_number}"
        )

    legal_actions_payload = block["confirmed_legal_actions"]
    if not isinstance(legal_actions_payload, list):
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_LEGAL_ACTIONS_MUST_BE_LIST:turn={turn_number}"
        )
    legal_actions = tuple(
        _legal_action_from_json(action, turn_number=turn_number)
        for action in legal_actions_payload
    )
    seen_confirmation_ids: set[str] = set()
    for action in legal_actions:
        if action.identity != state.identity:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_LEGAL_ACTION_IDENTITY_MISMATCH:turn={turn_number}"
            )
        if action.confirmation_id in seen_confirmation_ids:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_DUPLICATE_LEGAL_ACTION:turn={turn_number}"
            )
        seen_confirmation_ids.add(action.confirmation_id)
    if legal_actions:
        try:
            build_confirmed_legal_actions_input(state.identity, legal_actions)
        except (TurnStateIdentityError, ValueError) as exc:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_LEGAL_ACTION_BOUNDARY_REJECTED:turn={turn_number}:{exc}"
            ) from exc

    evidence_payload = block["evidence"]
    if evidence_payload is not None:
        evidence = _evidence_from_json(evidence_payload, turn_number=turn_number)
        if state.evidence_id != evidence.evidence_id:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_EVIDENCE_ID_MISMATCH:turn={turn_number}"
            )


def parse_match_export_v3(raw: bytes) -> dict[str, Any]:
    """Strict parse: valid JSON object, all required keys, correct schema_version.

    When any turn carries a ``rich_state`` block, that block is validated
    recursively (required sub-keys, knowledge/delta invariants, evidence
    hash shape, no duplicate legal actions). The full payload is also
    scanned recursively for a forbidden provider/prompt/header/credential
    key -- a raw provider request/response, prompt text, or embedded image
    bytes/base64 fails the parse closed.
    """

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatchExportV3Error("V3_EXPORT_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise MatchExportV3Error("V3_EXPORT_ROOT_MUST_BE_OBJECT")
    missing = _REQUIRED_TOP_LEVEL_KEYS - payload.keys()
    if missing:
        raise MatchExportV3Error(f"V3_EXPORT_MISSING_KEYS:{sorted(missing)}")
    if payload["schema_version"] != MATCH_EXPORT_SCHEMA_VERSION_V3:
        raise MatchExportV3Error("V3_EXPORT_SCHEMA_VERSION_MISMATCH")

    forbidden = _find_forbidden_key(payload)
    if forbidden is not None:
        raise MatchExportV3Error(f"V3_EXPORT_FORBIDDEN_KEY:{forbidden}")

    selection = payload["selection"]
    if not isinstance(selection, dict):
        raise MatchExportV3Error("V3_EXPORT_SELECTION_MUST_BE_OBJECT")
    missing_selection = _REQUIRED_SELECTION_KEYS - selection.keys()
    if missing_selection:
        raise MatchExportV3Error(f"V3_EXPORT_SELECTION_MISSING_KEYS:{sorted(missing_selection)}")

    if not isinstance(payload["action_history"], list):
        raise MatchExportV3Error("V3_EXPORT_ACTION_HISTORY_MUST_BE_LIST")

    for turn_payload in payload["turns"]:
        if not isinstance(turn_payload, dict):
            raise MatchExportV3Error("V3_EXPORT_TURN_MUST_BE_OBJECT")
        # A turn carrying the legacy per-turn shape (produced by the
        # repository-backed integration in application/match_service.py)
        # must retain every legacy compatibility field. The older,
        # standalone rich-only turn shape (turn_number/confirmed_turn_state
        # directly on the turn, no "reviewed_facts") predates that
        # integration and is validated on its own terms below.
        if "reviewed_facts" in turn_payload:
            missing_turn_keys = _REQUIRED_LEGACY_TURN_KEYS - turn_payload.keys()
            if missing_turn_keys:
                raise MatchExportV3Error(
                    f"V3_EXPORT_TURN_MISSING_LEGACY_KEYS:{sorted(missing_turn_keys)}"
                )
        if "rich_state" in turn_payload:
            _validate_rich_state_block(
                turn_payload["rich_state"], turn_number=turn_payload.get("turn_number")
            )
    return payload


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_match_v3(
    *,
    export_directory: Path,
    repository_root: Path,
    match_id: str,
    payload: dict[str, Any],
) -> tuple[Path, str]:
    """Atomically write the v3 export file. Idempotent, stable-hash, repo-external.

    Returns ``(export_path, sha256)``. Re-exporting identical ``payload``
    content is a no-op that returns the same path/hash; a divergent payload
    for the same ``match_id`` fails closed rather than overwriting.
    """

    export_directory = export_directory.expanduser().resolve()
    repository_root = repository_root.expanduser().resolve()
    if export_directory.is_relative_to(repository_root):
        raise MatchExportV3Error("EXPORT_DIRECTORY_INSIDE_REPOSITORY")
    if payload.get("schema_version") != MATCH_EXPORT_SCHEMA_VERSION_V3:
        raise MatchExportV3Error("V3_EXPORT_SCHEMA_VERSION_MISMATCH")
    if payload.get("match_id") != match_id:
        raise MatchExportV3Error("V3_EXPORT_MATCH_ID_MISMATCH")

    encoded = _encode_payload(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    export_path = export_directory / f"maple-match-v3-{match_id}.json"

    export_directory.mkdir(parents=True, exist_ok=True)
    if export_path.exists():
        existing_bytes = export_path.read_bytes()
        if existing_bytes != encoded:
            raise MatchExportV3Error("V3_EXPORT_FILE_CONTENT_MISMATCH")
        return export_path, digest

    try:
        _atomic_write(export_path, encoded)
    except OSError as exc:
        raise MatchExportV3Error("V3_EXPORT_WRITE_FAILED") from exc
    return export_path, digest
