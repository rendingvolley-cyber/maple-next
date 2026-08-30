"""Local match-feedback queue: ``pending/`` / ``published/`` directories.

Always rooted at a caller-supplied directory outside the repository (the
production caller uses the same runtime-root convention as the existing
database/exports/logs directories). This module never imports
``subprocess``, ``socket``, ``urllib``, ``sqlite3``, or
``maple_next.persistence`` -- it only ever reads/writes plain files under the
feedback directory it was given.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from maple_next.feedback.publisher import sha256_hex

PENDING_DIRECTORY_NAME = "pending"
PUBLISHED_DIRECTORY_NAME = "published"


class FeedbackConflictError(ValueError):
    """Same ``match_id``, different bytes -- never silently overwritten."""

    def __init__(self, match_id: str) -> None:
        self.match_id = match_id
        super().__init__(f"FEEDBACK_QUEUE_CONTENT_MISMATCH:{match_id}")


@dataclass(frozen=True, slots=True)
class FeedbackQueue:
    feedback_directory: Path

    @property
    def pending_dir(self) -> Path:
        return self.feedback_directory / PENDING_DIRECTORY_NAME

    @property
    def published_dir(self) -> Path:
        return self.feedback_directory / PUBLISHED_DIRECTORY_NAME

    def _pending_path(self, match_id: str) -> Path:
        return self.pending_dir / f"{match_id}.json"

    def _published_path(self, match_id: str) -> Path:
        return self.published_dir / f"{match_id}.json"

    def enqueue_pending(self, match_id: str, encoded: bytes) -> Path:
        """Write ``encoded`` for ``match_id`` into ``pending/``.

        Idempotent: identical bytes already present (pending or published) is
        a no-op that returns the existing path. Different bytes for the same
        ``match_id`` raises :class:`FeedbackConflictError` and preserves a
        separate diagnostic copy instead of overwriting either file.
        """

        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.published_dir.mkdir(parents=True, exist_ok=True)

        published_path = self._published_path(match_id)
        if published_path.exists():
            if published_path.read_bytes() == encoded:
                return published_path
            self._write_conflict_copy(match_id, encoded)
            raise FeedbackConflictError(match_id)

        pending_path = self._pending_path(match_id)
        if pending_path.exists():
            if pending_path.read_bytes() == encoded:
                return pending_path
            self._write_conflict_copy(match_id, encoded)
            raise FeedbackConflictError(match_id)

        _atomic_write(pending_path, encoded)
        return pending_path

    def _write_conflict_copy(self, match_id: str, encoded: bytes) -> Path:
        conflict_path = self.pending_dir / f"{match_id}.conflict-{sha256_hex(encoded)[:8]}.json"
        if not conflict_path.exists():
            _atomic_write(conflict_path, encoded)
        return conflict_path

    def mark_published(self, match_id: str) -> Path:
        """Move the pending file for ``match_id`` into ``published/``.

        Idempotent: if there is no pending file but a published one already
        exists (a repeated publish attempt), returns it unchanged rather than
        raising.
        """

        pending_path = self._pending_path(match_id)
        published_path = self._published_path(match_id)
        self.published_dir.mkdir(parents=True, exist_ok=True)
        if not pending_path.exists():
            if published_path.exists():
                return published_path
            raise FileNotFoundError(f"FEEDBACK_QUEUE_PENDING_FILE_MISSING:{match_id}")
        encoded = pending_path.read_bytes()
        _atomic_write(published_path, encoded)
        pending_path.unlink()
        return published_path

    def status_for_match(self, match_id: str) -> str | None:
        """Pure filesystem check: ``"synced"``, ``"pending"``, or ``None``."""

        if self._published_path(match_id).exists():
            return "synced"
        if self._pending_path(match_id).exists():
            return "pending"
        return None


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
