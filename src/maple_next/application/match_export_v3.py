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
    field_delta_to_json,
    known_to_json,
    side_delta_to_json,
    side_state_to_json,
)

MATCH_EXPORT_SCHEMA_VERSION_V3 = "maple-match.v3"

_REQUIRED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "match_id",
        "generation",
        "outcome",
        "ended_at_utc",
        "final_battle_revision",
        "turns",
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


def _identity_to_json(state: ConfirmedTurnState) -> dict[str, Any]:
    identity = state.identity
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
        "identity": _identity_to_json(state),
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
        "action_type": selection.action_type.value,
        "action_name": selection.action_name,
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
) -> dict[str, Any]:
    """Build the v3 export payload. Fails closed on any identity mismatch.

    ``turns`` must be non-empty -- v3 is only produced for a match that
    actually used the rich-state contract. Each turn's confirmed legal
    actions and, when present, evidence and source delta are embedded
    verbatim (per-field knowledge/provenance intact, no backfill, no raw
    bytes/secrets).
    """

    if not turns:
        raise MatchExportV3Error("V3_EXPORT_REQUIRES_AT_LEAST_ONE_RICH_STATE_TURN")

    turn_payloads: list[dict[str, Any]] = []
    for record in turns:
        state = record.confirmed_state
        if state.identity.session_id != session_id or state.identity.match_id != match_id:
            raise MatchExportV3Error("V3_EXPORT_TURN_IDENTITY_MISMATCH")
        for selection in record.confirmed_legal_actions:
            if selection.identity != state.identity:
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
        "turns": turn_payloads,
    }


def _encode_payload(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def compute_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_encode_payload(payload)).hexdigest()


def parse_match_export_v3(raw: bytes) -> dict[str, Any]:
    """Strict parse: valid JSON object, all required keys, correct schema_version."""

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
