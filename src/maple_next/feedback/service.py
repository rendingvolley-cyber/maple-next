"""Match feedback publish orchestration: the only integration point the UI uses.

Reads the exact bytes the existing exporter (``application/match_service.py``)
already wrote and verified, validates them through
:mod:`maple_next.feedback.publisher` (which reuses the production strict
parsers), enqueues them locally via :mod:`maple_next.feedback.queue`, and --
only when explicitly enabled and authenticated -- attempts a GitHub publish
via :mod:`maple_next.feedback.github_client`. Every failure mode, including an
unexpected exception, degrades to :attr:`FeedbackStatus.PENDING`;
:meth:`FeedbackPublishService.handle_match_exported` never raises, never
calls Gemini/OCR, never touches the database, and never mutates match state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from maple_next.feedback.github_client import GitHubCliClient, GitHubPublishClient
from maple_next.feedback.publisher import (
    DEFAULT_MATCH_FEEDBACK_GITHUB_BRANCH,
    MATCH_FEEDBACK_GITHUB_BRANCH_ENV,
    MATCH_FEEDBACK_GITHUB_ENABLED_ENV,
    MATCH_FEEDBACK_GITHUB_REPO_ENV,
    build_latest_pointer_payload,
    build_remote_match_path,
    sha256_hex,
    validate_canonical_export,
)
from maple_next.feedback.queue import FeedbackConflictError, FeedbackQueue

_AUTHORIZED_VALUE = "1"
_LATEST_POINTER_PATH = "feedback/latest.json"


class FeedbackStatus(Enum):
    SYNCED = "synced"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class FeedbackPublishConfig:
    enabled: bool
    repo: str | None
    branch: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FeedbackPublishConfig:
        environment = env if env is not None else os.environ
        enabled = (
            environment.get(MATCH_FEEDBACK_GITHUB_ENABLED_ENV, "").strip() == _AUTHORIZED_VALUE
        )
        repo = environment.get(MATCH_FEEDBACK_GITHUB_REPO_ENV, "").strip() or None
        branch = (
            environment.get(MATCH_FEEDBACK_GITHUB_BRANCH_ENV, "").strip()
            or DEFAULT_MATCH_FEEDBACK_GITHUB_BRANCH
        )
        return cls(enabled=enabled, repo=repo, branch=branch)


class FeedbackPublishService:
    """MATCH -> EXPORT already happened; this handles PUBLISH only.

    Construct once per runtime (feedback directory + config) and call
    :meth:`handle_match_exported` after each successful
    ``MatchApplication.export_match()``.
    """

    def __init__(
        self,
        feedback_directory: Path,
        config: FeedbackPublishConfig,
        *,
        client_factory: Callable[[str, str], GitHubPublishClient] = GitHubCliClient,
    ) -> None:
        self._queue = FeedbackQueue(Path(feedback_directory))
        self._config = config
        self._client_factory = client_factory

    def status_for_match(self, match_id: str) -> FeedbackStatus | None:
        status = self._queue.status_for_match(match_id)
        if status is None:
            return None
        return FeedbackStatus(status)

    def handle_match_exported(
        self,
        *,
        match_id: str,
        ended_at_utc: str,
        outcome: str,
        export_path: str | Path,
    ) -> FeedbackStatus:
        """Never raises: any unexpected error degrades to ``PENDING``.

        This is what makes "GitHub failure must never make match export
        fail" an invariant rather than a hope -- the whole body is one
        try/except.
        """

        try:
            return self._handle_match_exported(
                match_id=match_id,
                ended_at_utc=ended_at_utc,
                outcome=outcome,
                export_path=export_path,
            )
        except Exception:
            return FeedbackStatus.PENDING

    def _handle_match_exported(
        self,
        *,
        match_id: str,
        ended_at_utc: str,
        outcome: str,
        export_path: str | Path,
    ) -> FeedbackStatus:
        encoded = Path(export_path).read_bytes()
        payload = validate_canonical_export(encoded)

        try:
            self._queue.enqueue_pending(match_id, encoded)
        except FeedbackConflictError:
            return FeedbackStatus.PENDING

        if self._queue.status_for_match(match_id) == "synced":
            # Idempotent retry: already published (by an earlier call with
            # identical bytes) -- never re-contacts GitHub for the same match.
            return FeedbackStatus.SYNCED

        if not self._config.enabled or not self._config.repo:
            return FeedbackStatus.PENDING

        client = self._client_factory(self._config.repo, self._config.branch)
        if not client.auth_status():
            return FeedbackStatus.PENDING
        if not client.ensure_branch_exists():
            return FeedbackStatus.PENDING

        remote_path = build_remote_match_path(match_id, ended_at_utc)
        digest = sha256_hex(encoded)
        upload = client.upload_file(remote_path, encoded, f"match-feedback: {match_id}")
        if not upload.ok:
            return FeedbackStatus.PENDING

        pointer = build_latest_pointer_payload(
            match_id=match_id,
            ended_at_utc=ended_at_utc,
            outcome=outcome,
            source_schema_version=str(payload["schema_version"]),
            match_path=remote_path,
            sha256=digest,
        )
        pointer_result = client.upsert_pointer_file(
            _LATEST_POINTER_PATH,
            _encode_pointer(pointer),
            f"match-feedback latest: {match_id}",
        )
        if not pointer_result.ok:
            return FeedbackStatus.PENDING

        self._queue.mark_published(match_id)
        return FeedbackStatus.SYNCED


def _encode_pointer(pointer: dict[str, object]) -> bytes:
    return (json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
