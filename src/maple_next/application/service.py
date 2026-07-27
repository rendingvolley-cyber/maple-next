"""Application command service for the mock Selection vertical slice."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from maple_next.application.projection import DomainProjection, project
from maple_next.domain.enums import BattleState, JobStatus, JobType, ResultDisposition
from maple_next.domain.models import AppliedSelectionSnapshot, BattleSession, SelectionFacts
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope


class DomainError(RuntimeError):
    """Raised when a command violates the canonical transition contract."""


class BattleApplication:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def projection(self) -> DomainProjection:
        session = self.repository.load_active_session()
        latest_job = (
            self.repository.latest_provider_job(session.session_id) if session is not None else None
        )
        return project(session, latest_job)

    def new_match(self) -> BattleSession:
        with self.repository.transaction():
            if self.repository.load_active_session() is not None:
                raise DomainError("ACTIVE_MATCH_EXISTS")
            session = BattleSession(
                session_id=str(uuid4()),
                match_id=str(uuid4()),
                generation=self.repository.next_generation(),
                state=BattleState.SELECTION_OPEN,
                battle_revision=1,
            )
            self.repository.insert_session(session)
        return session

    def confirm_selection_facts(
        self,
        self_team: tuple[str, ...],
        opponent_team: tuple[str, ...],
    ) -> SelectionFacts:
        facts = SelectionFacts(str(uuid4()), self_team, opponent_team)
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_OPEN)
            self.repository.append_selection_facts(session.session_id, facts)
            session.current_reviewed_selection_id = facts.reviewed_selection_id
            session.current_selection_advice_id = None
            session.bump_battle()
            self.repository.save_session(session)
        return facts

    def request_selection_advice(self, command_id: str) -> JobEnvelope:
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_OPEN)
            if session.current_reviewed_selection_id is None:
                raise DomainError("REVIEWED_SELECTION_REQUIRED")
            latest_job = self.repository.latest_provider_job(session.session_id)
            if latest_job is not None and latest_job.status in {
                JobStatus.QUEUED,
                JobStatus.IN_FLIGHT,
            }:
                raise DomainError("PROVIDER_REQUEST_PENDING")
            if latest_job is not None and latest_job.status is JobStatus.DELIVERY_UNKNOWN:
                raise DomainError("PROVIDER_DELIVERY_UNKNOWN")
            payload = {
                "reviewed_selection_id": session.current_reviewed_selection_id,
                "battle_revision": session.battle_revision,
            }
            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=str(uuid4()),
                command_id=command_id,
                job_type=JobType.SELECTION_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=None,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.SELECTION_OPEN,
                input_snapshot_id=session.current_reviewed_selection_id,
                request_payload_hash=self.payload_hash(payload),
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def apply_selection_advice_result(self, result: ResultEnvelope) -> ResultDisposition:
        with self.repository.transaction():
            try:
                job = self.repository.get_job(result.job_id)
            except KeyError:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_ID_MISMATCH"
                )
                return ResultDisposition.STALE_REJECTED

            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_provider_job(session.session_id)
                if session is not None
                else None
            )
            reason = self._binding_failure_reason(session, latest_job, job, result)
            if reason is not None:
                self.repository.audit_result(result, ResultDisposition.STALE_REJECTED, reason)
                return ResultDisposition.STALE_REJECTED

            assert session is not None
            try:
                selected_three = tuple(result.payload["selected_three"])
                lead = str(result.payload["lead"])
                if len(selected_three) != 3:
                    raise ValueError("selected_three")
                typed_three = (
                    str(selected_three[0]),
                    str(selected_three[1]),
                    str(selected_three[2]),
                )
                if len(set(typed_three)) != 3 or lead not in typed_three:
                    raise ValueError("illegal selection")
                selection_facts = self.repository.get_selection_facts(job.input_snapshot_id)
                if any(name not in selection_facts.self_team for name in typed_three):
                    raise ValueError("selection outside reviewed team")
                backline_values = tuple(name for name in typed_three if name != lead)
                backline = (backline_values[0], backline_values[1])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                self.repository.audit_result(
                    result, ResultDisposition.INVALID_REJECTED, f"INVALID_PAYLOAD:{exc}"
                )
                self.repository.update_job_status(job.job_id, JobStatus.FAILED)
                return ResultDisposition.INVALID_REJECTED

            advice_id = result.result_id
            self.repository.append_selection_advice(
                advice_id,
                session.session_id,
                job.job_id,
                typed_three,
                lead,
                backline,
            )
            session.current_selection_advice_id = advice_id
            session.state = BattleState.SELECTION_ADVICE_READY
            session.bump_battle()
            self.repository.save_session(session)
            self.repository.update_job_status(job.job_id, JobStatus.SUCCEEDED)
            self.repository.audit_result(result, ResultDisposition.APPLIED, "BINDING_ACCEPTED")
        return ResultDisposition.APPLIED

    def apply_selection(self, *, human_confirmed: bool) -> AppliedSelectionSnapshot:
        if not human_confirmed:
            raise DomainError("HUMAN_APPLY_REQUIRED")
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_ADVICE_READY)
            if session.current_selection_advice_id is None:
                raise DomainError("CURRENT_SELECTION_ADVICE_REQUIRED")
            advice = self.repository.get_selection_advice(session.current_selection_advice_id)
            snapshot = AppliedSelectionSnapshot(
                applied_selection_id=str(uuid4()),
                selected_three=advice["selected_three"],
                lead=advice["lead"],
                backline=advice["backline"],
                source_advice_id=session.current_selection_advice_id,
            )
            self.repository.append_applied_selection(session.session_id, snapshot)
            session.current_applied_selection_id = snapshot.applied_selection_id
            session.state = BattleState.BATTLE_READY
            session.bump_battle()
            self.repository.save_session(session)
        return snapshot

    def update_metadata(self) -> BattleSession:
        with self.repository.transaction():
            session = self._require_active_session()
            session.bump_metadata()
            self.repository.save_session(session)
        return session

    def recover_after_restart(self) -> None:
        with self.repository.transaction():
            self.repository.recover_unfinished_jobs()

    def _require_active_session(self) -> BattleSession:
        session = self.repository.load_active_session()
        if session is None:
            raise DomainError("NO_ACTIVE_MATCH")
        return session

    def _require_session(self, expected_state: BattleState) -> BattleSession:
        session = self._require_active_session()
        if session.state is not expected_state:
            raise DomainError(f"EXPECTED_{expected_state.value}")
        return session

    @staticmethod
    def payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _binding_failure_reason(
        session: BattleSession | None,
        latest_job: JobEnvelope | None,
        job: JobEnvelope,
        result: ResultEnvelope,
    ) -> str | None:
        checks = (
            (latest_job is not None and latest_job.job_id == job.job_id, "JOB_ID_NOT_CURRENT"),
            (result.command_id == job.command_id, "COMMAND_ID_MISMATCH"),
            (result.job_type is job.job_type, "JOB_TYPE_MISMATCH"),
            (session is not None, "NO_ACTIVE_MATCH"),
            (session is not None and result.session_id == session.session_id, "SESSION_MISMATCH"),
            (session is not None and result.match_id == session.match_id, "MATCH_MISMATCH"),
            (
                session is not None and result.generation == session.generation,
                "GENERATION_MISMATCH",
            ),
            (result.turn_number == job.turn_number, "TURN_MISMATCH"),
            (
                session is not None
                and result.base_battle_revision == session.battle_revision
                and result.base_battle_revision == job.base_battle_revision,
                "BATTLE_REVISION_MISMATCH",
            ),
            (
                session is not None
                and result.expected_state is session.state
                and result.expected_state is job.expected_state,
                "EXPECTED_STATE_MISMATCH",
            ),
            (
                session is not None
                and result.input_snapshot_id == session.current_reviewed_selection_id
                and result.input_snapshot_id == job.input_snapshot_id,
                "INPUT_SNAPSHOT_MISMATCH",
            ),
            (
                result.request_payload_hash == job.request_payload_hash,
                "PAYLOAD_HASH_MISMATCH",
            ),
        )
        for passed, reason in checks:
            if not passed:
                return reason
        return None
