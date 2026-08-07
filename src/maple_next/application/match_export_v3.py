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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FixedEvidenceMetadata,
    TurnIdentity,
    field_delta_to_json,
    known_to_json,
    side_delta_to_json,
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
    unique state ids; unique (turn_number, battle_revision) pairs; a
    strictly non-decreasing (turn_number, battle_revision) ordering; and no
    state whose ``battle_revision`` exceeds ``outcome.final_battle_revision``.
    """

    if outcome.session_id != session_id or outcome.match_id != match_id:
        raise MatchExportV3Error("V3_EXPORT_OUTCOME_SESSION_MATCH_MISMATCH")
    if outcome.generation != generation:
        raise MatchExportV3Error("V3_EXPORT_OUTCOME_GENERATION_MISMATCH")

    seen_state_ids: set[str] = set()
    seen_turn_revisions: set[tuple[int, int]] = set()
    previous_key: tuple[int, int] | None = None
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
        key = (identity.turn_number, identity.battle_revision)
        if key in seen_turn_revisions:
            raise MatchExportV3Error("V3_EXPORT_DUPLICATE_TURN_REVISION")
        seen_turn_revisions.add(key)
        if previous_key is not None and key < previous_key:
            raise MatchExportV3Error("V3_EXPORT_STATE_ORDER_NOT_INCREASING")
        previous_key = key
        if identity.battle_revision > outcome.final_battle_revision:
            raise MatchExportV3Error("V3_EXPORT_STATE_BEYOND_FINAL_REVISION")


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


def _validate_rich_state_block(block: Any, *, turn_number: object) -> None:
    if not isinstance(block, dict):
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_MUST_BE_OBJECT:turn={turn_number}")
    missing = _REQUIRED_RICH_STATE_KEYS - block.keys()
    if missing:
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_MISSING_KEYS:turn={turn_number}:{sorted(missing)}"
        )
    if block["contract_version"] != RICH_STATE_EXPORT_CONTRACT_VERSION:
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_CONTRACT_VERSION_MISMATCH:turn={turn_number}"
        )
    state = block["confirmed_turn_state"]
    if not isinstance(state, dict):
        raise MatchExportV3Error(f"V3_EXPORT_RICH_STATE_STATE_MUST_BE_OBJECT:turn={turn_number}")
    required_state_keys = {
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
    missing_state = required_state_keys - state.keys()
    if missing_state:
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_STATE_MISSING_KEYS:turn={turn_number}:{sorted(missing_state)}"
        )
    for known_field in ("weather", "terrain"):
        known = state[known_field]
        if not isinstance(known, dict) or "status" not in known:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_INVALID_KNOWLEDGE:turn={turn_number}:{known_field}"
            )
        if known["status"] not in {"CONFIRMED", "UNKNOWN"}:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_INVALID_KNOWLEDGE_STATUS:turn={turn_number}:{known_field}"
            )
        if known["status"] == "UNKNOWN" and known.get("value") is not None:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_UNKNOWN_CARRIES_VALUE:turn={turn_number}:{known_field}"
            )
        if known["status"] == "CONFIRMED" and not known.get("provenance_chain"):
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_MISSING_PROVENANCE:turn={turn_number}:{known_field}"
            )
    delta = block["source_action_result_delta"]
    if delta is not None:
        if not isinstance(delta, dict) or "based_on_confirmed_state_id" not in delta:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_INVALID_DELTA:turn={turn_number}"
            )
        for field_delta_field in ("weather", "terrain"):
            field_delta = delta.get(field_delta_field)
            if not isinstance(field_delta, dict) or "observation" not in field_delta:
                raise MatchExportV3Error(
                    f"V3_EXPORT_RICH_STATE_INVALID_FIELD_DELTA:turn={turn_number}:{field_delta_field}"
                )
            observation = field_delta["observation"]
            if observation not in {"CHANGED", "UNCHANGED", "UNKNOWN"}:
                raise MatchExportV3Error(
                    f"V3_EXPORT_RICH_STATE_INVALID_OBSERVATION:turn={turn_number}:{field_delta_field}"
                )
            if observation == "CHANGED" and field_delta.get("after_value") is None:
                raise MatchExportV3Error(
                    f"V3_EXPORT_RICH_STATE_CHANGED_WITHOUT_VALUE:turn={turn_number}:{field_delta_field}"
                )
            if observation != "CHANGED" and field_delta.get("after_value") is not None:
                raise MatchExportV3Error(
                    f"V3_EXPORT_RICH_STATE_UNCHANGED_CARRIES_VALUE:turn={turn_number}:{field_delta_field}"
                )
    legal_actions = block["confirmed_legal_actions"]
    if not isinstance(legal_actions, list):
        raise MatchExportV3Error(
            f"V3_EXPORT_RICH_STATE_LEGAL_ACTIONS_MUST_BE_LIST:turn={turn_number}"
        )
    seen_confirmation_ids: set[object] = set()
    for action in legal_actions:
        if not isinstance(action, dict) or not action.get("confirmation_id"):
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_MALFORMED_LEGAL_ACTION:turn={turn_number}"
            )
        if action["confirmation_id"] in seen_confirmation_ids:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_DUPLICATE_LEGAL_ACTION:turn={turn_number}"
            )
        seen_confirmation_ids.add(action["confirmation_id"])
    evidence = block["evidence"]
    if evidence is not None:
        if state["evidence_id"] != evidence.get("evidence_id"):
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_EVIDENCE_ID_MISMATCH:turn={turn_number}"
            )
        sha256 = evidence.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise MatchExportV3Error(
                f"V3_EXPORT_RICH_STATE_INVALID_EVIDENCE_HASH:turn={turn_number}"
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
