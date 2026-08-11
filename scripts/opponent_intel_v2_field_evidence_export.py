"""Archive one fixed, exact completed-match export as sanitized GitHub evidence.

Scope is deliberately narrow: this script does not search the runtime root
for "the newest export" or otherwise guess which match to archive. The
target match was fixed by explicit user instruction (match_id, generation,
outcome, schema, turn count) and this script fails closed if the file at
that exact path does not match every one of those facts. It never falls
back to a different export, an older export, or a test fixture.

The original export file under the runtime root is never modified. A
sanitized copy plus a manifest describing anything redacted is written
into the git repository on a dedicated ``evidence/<match_id>`` branch,
never onto ``main``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from maple_next.application.match_export_v3 import (  # noqa: E402
    MatchExportV3Error,
    parse_match_export_v3,
)

TARGET_MATCH_ID = "2c77446d-3332-40fe-b28b-5c67dd4e130c"
TARGET_GENERATION = 20
TARGET_OUTCOME = "WIN"
TARGET_SCHEMA_VERSION = "maple-match.v3"
TARGET_TURN_COUNT = 7
TARGET_FILENAME = f"maple-match-{TARGET_MATCH_ID}.json"

EVIDENCE_BRANCH = f"evidence/{TARGET_MATCH_ID}"
EVIDENCE_DIRECTORY = REPO_ROOT / "evidence" / TARGET_MATCH_ID
EVIDENCE_JSON_PATH = EVIDENCE_DIRECTORY / "match.json"
EVIDENCE_MANIFEST_PATH = EVIDENCE_DIRECTORY / "manifest.json"

_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\"'\s]*)"  # Windows drive-letter path
    r"|(?:\\\\[^\"'\s]+)"  # UNC path
    r"|(?:/(?:Users|home|mnt|tmp|var|etc)/[^\"'\s]*)"  # POSIX absolute path
)


class FieldEvidenceError(Exception):
    """Fail-closed error for the field-evidence export workflow."""


def locate_target_export(runtime_root: Path) -> Path:
    """Return the exact target export path. Never substitutes another file."""

    candidate = runtime_root / "exports" / TARGET_FILENAME
    if not candidate.is_file():
        raise FieldEvidenceError(
            f"TARGET_EXPORT_NOT_FOUND: expected exact file {candidate} "
            "(no discovery/fallback to another export is permitted)"
        )
    return candidate


def load_and_verify_target(path: Path) -> tuple[bytes, dict[str, Any]]:
    """Strict-parse the export and assert every fixed target identity fact.

    Reuses ``parse_match_export_v3`` for schema/shape validation -- this
    already fails closed on any forbidden provider/prompt/secret key
    (api_key, authorization, headers, prompt, raw_request, raw_response,
    credentials, etc; see ``match_export_v3._FORBIDDEN_KEYS``).
    """

    original_bytes = path.read_bytes()
    try:
        payload = parse_match_export_v3(original_bytes)
    except MatchExportV3Error as exc:
        raise FieldEvidenceError(f"TARGET_EXPORT_FAILED_STRICT_PARSE:{exc}") from exc

    mismatches: list[str] = []
    if payload.get("match_id") != TARGET_MATCH_ID:
        mismatches.append(f"match_id={payload.get('match_id')!r}")
    if payload.get("generation") != TARGET_GENERATION:
        mismatches.append(f"generation={payload.get('generation')!r}")
    if payload.get("outcome") != TARGET_OUTCOME:
        mismatches.append(f"outcome={payload.get('outcome')!r}")
    if payload.get("schema_version") != TARGET_SCHEMA_VERSION:
        mismatches.append(f"schema_version={payload.get('schema_version')!r}")
    turns = payload.get("turns")
    turn_count = len(turns) if isinstance(turns, list) else None
    if turn_count != TARGET_TURN_COUNT:
        mismatches.append(f"turn_count={turn_count!r}")
    if mismatches:
        raise FieldEvidenceError(
            "TARGET_EXPORT_IDENTITY_MISMATCH: " + ", ".join(mismatches)
        )

    return original_bytes, payload


@dataclass(frozen=True, slots=True)
class Redaction:
    json_path: str
    reason: str


def _scrub(node: Any, *, path: str, redactions: list[Redaction]) -> Any:
    if isinstance(node, dict):
        return {
            key: _scrub(value, path=f"{path}.{key}", redactions=redactions)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            _scrub(item, path=f"{path}[{index}]", redactions=redactions)
            for index, item in enumerate(node)
        ]
    if isinstance(node, str) and _ABSOLUTE_PATH_RE.search(node):
        redactions.append(Redaction(json_path=path, reason="absolute_local_filesystem_path"))
        return "[REDACTED_LOCAL_PATH]"
    return node


def scrub_machine_local_fields(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Redaction]]:
    """Best-effort defense in depth: redact any stray absolute local path.

    ``parse_match_export_v3`` already guarantees no api_key/prompt/raw
    request-response/credential key exists anywhere in the payload (fail
    closed at parse time). This additionally scrubs absolute filesystem
    paths, which are not secrets but are unwanted machine-local noise in a
    shared analysis artifact.
    """

    redactions: list[Redaction] = []
    scrubbed = _scrub(payload, path="$", redactions=redactions)
    assert isinstance(scrubbed, dict)
    return scrubbed, redactions


def _encode(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def build_manifest(
    *,
    source_path: Path,
    original_bytes: bytes,
    sanitized_bytes: bytes,
    redactions: list[Redaction],
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "manifest_schema": "opponent-intel-v2-field-evidence-manifest.v1",
        "match_identity": {
            "match_id": payload["match_id"],
            "session_id": payload["session_id"],
            "generation": payload["generation"],
            "outcome": payload["outcome"],
            "schema_version": payload["schema_version"],
            "ended_at_utc": payload["ended_at_utc"],
            "turn_count": len(payload["turns"]),
        },
        "source_export_filename": source_path.name,
        "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "sanitized_sha256": hashlib.sha256(sanitized_bytes).hexdigest(),
        "sanitization_applied": bool(redactions),
        "removed_or_redacted_fields": [
            {"json_path": r.json_path, "reason": r.reason} for r in redactions
        ],
        "forbidden_key_scan": (
            "passed: parse_match_export_v3 fail-closed scan found no "
            "api_key/authorization/headers/prompt/raw_request/raw_response/"
            "credential/secret/base64 image field anywhere in this document"
        ),
    }


def write_evidence_files(sanitized_payload: dict[str, Any], manifest: dict[str, Any]) -> None:
    EVIDENCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    EVIDENCE_JSON_PATH.write_bytes(_encode(sanitized_payload))
    EVIDENCE_MANIFEST_PATH.write_bytes(_encode(manifest))


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise FieldEvidenceError(f"GIT_COMMAND_FAILED: git {' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()


def publish_evidence_branch() -> str:
    """Create/switch to the evidence branch, commit, and push it. Never touches main."""

    current_branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    if current_branch == "main":
        raise FieldEvidenceError("REFUSING_TO_OPERATE_ON_MAIN_DIRECTLY")

    branch_list = _run_git("branch", "--list", EVIDENCE_BRANCH)
    if branch_list:
        _run_git("checkout", EVIDENCE_BRANCH)
    else:
        _run_git("checkout", "-b", EVIDENCE_BRANCH, "main")

    _run_git(
        "add",
        str(EVIDENCE_JSON_PATH.relative_to(REPO_ROOT)),
        str(EVIDENCE_MANIFEST_PATH.relative_to(REPO_ROOT)),
    )
    status = _run_git("status", "--porcelain", "--", str(EVIDENCE_DIRECTORY.relative_to(REPO_ROOT)))
    if status:
        _run_git(
            "commit",
            "-m",
            f"Add sanitized field evidence for match {TARGET_MATCH_ID}",
        )
    commit_sha = _run_git("rev-parse", "HEAD")
    _run_git("push", "-u", "origin", EVIDENCE_BRANCH)
    _run_git("checkout", current_branch)
    return commit_sha


def main() -> int:
    from maple_next.opponent_intel_db.runtime_paths import resolve_intel_runtime_root  # noqa: E402

    runtime_root = resolve_intel_runtime_root()
    source_path = locate_target_export(runtime_root)
    original_bytes, payload = load_and_verify_target(source_path)
    sanitized_payload, redactions = scrub_machine_local_fields(payload)
    sanitized_bytes = _encode(sanitized_payload)
    manifest = build_manifest(
        source_path=source_path,
        original_bytes=original_bytes,
        sanitized_bytes=sanitized_bytes,
        redactions=redactions,
        payload=payload,
    )
    write_evidence_files(sanitized_payload, manifest)
    commit_sha = publish_evidence_branch()

    print(f"evidence_branch={EVIDENCE_BRANCH}")
    print(f"commit_sha={commit_sha}")
    print(f"json_path={EVIDENCE_JSON_PATH.relative_to(REPO_ROOT)}")
    print(f"manifest_path={EVIDENCE_MANIFEST_PATH.relative_to(REPO_ROOT)}")
    print(f"original_sha256={manifest['original_sha256']}")
    print(f"sanitized_sha256={manifest['sanitized_sha256']}")
    print(f"sanitization_applied={manifest['sanitization_applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
