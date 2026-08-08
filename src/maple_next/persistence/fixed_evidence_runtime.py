"""Repository-external fixed image evidence runtime helper (Bundle A).

Stores evidence image bytes under a runtime root outside the git repository
and tracks a SHA-256-verifiable :class:`FixedEvidenceMetadata` reference.
This module never invokes capture or OCR and never touches UGREEN/OBS -- it
only manages bytes some other, out-of-scope, process has already produced.

Missing or unreadable evidence is manual-safe: callers get a typed
:class:`EvidenceValidationResult` rather than a raised exception, so manual
continuation of the legacy flow is never blocked by evidence trouble.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePath
from uuid import uuid4

from maple_next.domain.turn_state import FixedEvidenceMetadata

_UNSAFE_PATH_CHARACTERS = ("/", "\\", ":")


class EvidencePathError(ValueError):
    """A path/filename failed root-confinement validation. Fail closed."""


def _validate_safe_filename_component(value: str, *, label: str) -> None:
    """Reject anything that is not a single, safe filename component.

    Rejects: empty/blank strings, ``.``/``..``, any path separator or drive
    marker (covers absolute POSIX paths, Windows drive paths, and UNC
    paths), and any value ``PurePath`` itself considers absolute.
    """

    if not isinstance(value, str) or not value.strip():
        raise EvidencePathError(f"{label}_EMPTY")
    if value in (".", ".."):
        raise EvidencePathError(f"{label}_DOT_SEGMENT")
    if any(character in value for character in _UNSAFE_PATH_CHARACTERS):
        raise EvidencePathError(f"{label}_UNSAFE_CHARACTER")
    if PurePath(value).is_absolute():
        raise EvidencePathError(f"{label}_ABSOLUTE_NOT_ALLOWED")


def _resolve_confined_path(runtime_root: Path, filename: str, *, label: str) -> Path:
    """Resolve ``filename`` under ``runtime_root``, failing closed on escape.

    ``filename`` must be a single safe component (validated first). The
    resolved target is then required to sit inside the resolved runtime
    root -- resolution follows symlinks, so a symlink inside the root that
    points outside it is rejected too, not just literal ``..`` traversal.
    """

    _validate_safe_filename_component(filename, label=label)
    resolved_root = runtime_root.resolve()
    resolved_target = (resolved_root / filename).resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise EvidencePathError(f"{label}_ESCAPES_RUNTIME_ROOT")
    return resolved_target


class EvidenceValidationStatus(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    UNREADABLE = "UNREADABLE"
    SHA_MISMATCH = "SHA_MISMATCH"


@dataclass(frozen=True, slots=True)
class EvidenceValidationResult:
    status: EvidenceValidationStatus
    metadata: FixedEvidenceMetadata

    @property
    def is_valid(self) -> bool:
        return self.status is EvidenceValidationStatus.VALID


class FixedEvidenceRuntime:
    """Deterministic, atomic byte storage for a single fixed evidence image."""

    def __init__(self, runtime_root: Path, repository_root: Path) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.repository_root = repository_root.expanduser().resolve()
        if self.runtime_root == self.repository_root or self.runtime_root.is_relative_to(
            self.repository_root
        ):
            raise ValueError("EVIDENCE_ROOT_INSIDE_REPOSITORY")
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def write_evidence(
        self, content: bytes, *, evidence_id: str | None = None
    ) -> FixedEvidenceMetadata:
        resolved_id = evidence_id if evidence_id is not None else str(uuid4())
        _validate_safe_filename_component(resolved_id, label="EVIDENCE_ID")
        digest = hashlib.sha256(content).hexdigest()
        filename = f"{resolved_id}.bin"
        destination = _resolve_confined_path(self.runtime_root, filename, label="EVIDENCE_PATH")
        temporary_name = f".{filename}.{uuid4().hex}.tmp"
        temporary = _resolve_confined_path(
            self.runtime_root, temporary_name, label="EVIDENCE_TMP_PATH"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return FixedEvidenceMetadata(
            evidence_id=resolved_id,
            relative_path=filename,
            sha256=digest,
            recorded_at_utc=datetime.now(UTC).isoformat(),
        )

    def validate(self, metadata: FixedEvidenceMetadata) -> EvidenceValidationResult:
        path = _resolve_confined_path(
            self.runtime_root, metadata.relative_path, label="EVIDENCE_PATH"
        )
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return EvidenceValidationResult(EvidenceValidationStatus.MISSING, metadata)
        except OSError:
            return EvidenceValidationResult(EvidenceValidationStatus.UNREADABLE, metadata)
        digest = hashlib.sha256(content).hexdigest()
        if digest != metadata.sha256:
            return EvidenceValidationResult(EvidenceValidationStatus.SHA_MISMATCH, metadata)
        return EvidenceValidationResult(EvidenceValidationStatus.VALID, metadata)
