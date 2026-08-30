"""Match feedback publishing: canonical-export validation and deterministic paths.

Reuses the existing ``maple-match.v3``/``.v4`` strict parsers (and the same
forbidden-key denylist, for legacy ``.v1``/``.v2`` exports) from
``application/match_export_v3.py`` rather than defining a second battle-truth
schema. This module never imports ``subprocess``, ``socket``, ``urllib``,
``sqlite3``, ``maple_next.persistence``, or any provider transport module --
it cannot itself reach the network, a database, or send a real provider
request. (It does transitively depend on ``match_export_v3.py``, which -- like
its own production caller -- imports the Turn Advice v2 *response schema*
module for validation only; that module is a data contract, not a transport.)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from maple_next.application.match_export_v3 import (
    MATCH_EXPORT_SCHEMA_VERSION_V3,
    MATCH_EXPORT_SCHEMA_VERSION_V4,
    MatchExportV3Error,
    _find_forbidden_key,
    parse_match_export_v3,
    parse_match_export_v4,
)

MATCH_FEEDBACK_GITHUB_ENABLED_ENV = "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED"
MATCH_FEEDBACK_GITHUB_REPO_ENV = "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO"
MATCH_FEEDBACK_GITHUB_BRANCH_ENV = "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH"
DEFAULT_MATCH_FEEDBACK_GITHUB_BRANCH = "match-feedback"

FEEDBACK_INDEX_SCHEMA_VERSION = "maple-feedback-index.v1"

#: Duplicated as bare string literals -- deliberately not imported from
#: ``application/match_service.py``, which pulls in ``SQLiteRepository`` and
#: would give this module a transitive database dependency it must never have.
MATCH_EXPORT_SCHEMA_VERSION_V1 = "maple-match.v1"
MATCH_EXPORT_SCHEMA_VERSION_V2 = "maple-match.v2"

_REQUIRED_LEGACY_IDENTITY_KEYS = frozenset(
    {"schema_version", "match_id", "ended_at_utc", "outcome"}
)

_STRICT_PARSERS = {
    MATCH_EXPORT_SCHEMA_VERSION_V3: parse_match_export_v3,
    MATCH_EXPORT_SCHEMA_VERSION_V4: parse_match_export_v4,
}


class FeedbackValidationError(ValueError):
    """Raised when export bytes are not a valid, unmodified canonical export."""


def validate_canonical_export(encoded: bytes) -> dict[str, Any]:
    """Validate ``encoded`` is an unmodified canonical match export.

    ``.v3``/``.v4`` payloads are re-validated through the exact production
    strict parser (full rich-state chain, forbidden-key scan). Legacy
    ``.v1``/``.v2`` payloads have no dedicated top-level parser anywhere in
    this codebase (only the rich-state contract does); for those, this reuses
    the same forbidden-key denylist and checks the same top-level identity
    keys the ``latest.json`` index needs. Returns the decoded payload
    unchanged -- this function never rewrites or strips anything.
    """

    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackValidationError("FEEDBACK_EXPORT_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise FeedbackValidationError("FEEDBACK_EXPORT_ROOT_MUST_BE_OBJECT")

    schema_version = payload.get("schema_version")
    strict_parser = (
        _STRICT_PARSERS.get(schema_version) if isinstance(schema_version, str) else None
    )
    if strict_parser is not None:
        try:
            strict_parser(encoded)
        except MatchExportV3Error as exc:
            raise FeedbackValidationError(f"FEEDBACK_EXPORT_PARSE_FAILED:{exc}") from exc
        return payload

    if schema_version not in {MATCH_EXPORT_SCHEMA_VERSION_V1, MATCH_EXPORT_SCHEMA_VERSION_V2}:
        raise FeedbackValidationError(
            f"FEEDBACK_EXPORT_UNKNOWN_SCHEMA_VERSION:{schema_version!r}"
        )

    missing = _REQUIRED_LEGACY_IDENTITY_KEYS - payload.keys()
    if missing:
        raise FeedbackValidationError(f"FEEDBACK_EXPORT_MISSING_KEYS:{sorted(missing)}")

    forbidden = _find_forbidden_key(payload)
    if forbidden is not None:
        raise FeedbackValidationError(f"FEEDBACK_EXPORT_FORBIDDEN_KEY:{forbidden}")

    return payload


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_timestamp_component(ended_at_utc: str) -> str:
    """Filesystem/URL-safe rendering of an ISO-8601 UTC timestamp."""

    return ended_at_utc.replace(":", "-").replace("+", "_")


def build_remote_match_path(match_id: str, ended_at_utc: str) -> str:
    """Deterministic ``feedback/matches/YYYY/MM/DD/<ended_at>_<match_id>.json`` path."""

    parsed = datetime.fromisoformat(ended_at_utc.replace("Z", "+00:00"))
    return (
        f"feedback/matches/{parsed.year:04d}/{parsed.month:02d}/{parsed.day:02d}/"
        f"{_safe_timestamp_component(ended_at_utc)}_{match_id}.json"
    )


def build_latest_pointer_payload(
    *,
    match_id: str,
    ended_at_utc: str,
    outcome: str,
    source_schema_version: str,
    match_path: str,
    sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": FEEDBACK_INDEX_SCHEMA_VERSION,
        "match_id": match_id,
        "ended_at_utc": ended_at_utc,
        "outcome": outcome,
        "source_schema_version": source_schema_version,
        "match_path": match_path,
        "sha256": sha256,
    }
