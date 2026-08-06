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
from pathlib import Path
from uuid import uuid4

from maple_next.domain.turn_state import FixedEvidenceMetadata


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
        resolved_id = evidence_id or str(uuid4())
        digest = hashlib.sha256(content).hexdigest()
        destination = self.runtime_root / f"{resolved_id}.bin"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
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
            relative_path=str(destination.relative_to(self.runtime_root)),
            sha256=digest,
            recorded_at_utc=datetime.now(UTC).isoformat(),
        )

    def validate(self, metadata: FixedEvidenceMetadata) -> EvidenceValidationResult:
        path = self.runtime_root / metadata.relative_path
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
