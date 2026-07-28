"""Application command service for the human-operated Battle-1 lifecycle."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from maple_next.application.projection import DomainProjection, project
from maple_next.domain.enums import (
    ActionType,
    BattleState,
    HpBucket,
    JobStatus,
    JobType,
    ResultDisposition,
)
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    RecordedAction,
    SelectionFacts,
    TurnAdviceSnapshot,
    TurnFactsSnapshot,
)
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
        current_turn_number: int | None = None
        if session is not None and session.current_turn_id is not None:
            try:
                current_turn_number = self.repository.get_turn(session.current_turn_id).turn_number
            except KeyError:
                current_turn_number = None
        return project(session, latest_job, current_turn_number=current_turn_number)

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
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.SELECTION_ADVICE
            )
            self._guard_provider_request(latest_job)
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
            job = self._load_result_job_or_audit(result)
            if job is None:
                return ResultDisposition.STALE_REJECTED
            if job.job_type is not JobType.SELECTION_ADVICE:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_TYPE_NOT_SELECTION_ADVICE"
                )
                return ResultDisposition.STALE_REJECTED
            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_job_by_type(
                    session.session_id, JobType.SELECTION_ADVICE
                )
                if session is not None
                else None
            )
            current_snapshot_id = (
                session.current_reviewed_selection_id if session is not None else None
            )
            reason = self._binding_failure_reason(
                session,
                latest_job,
                job,
                result,
                current_input_snapshot_id=current_snapshot_id,
                current_turn_number=None,
            )
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

    def apply_selection(
        self,
        *,
        selected_three: tuple[str, str, str],
        lead: str,
        human_confirmed: bool,
    ) -> AppliedSelectionSnapshot:
        if not human_confirmed:
            raise DomainError("HUMAN_APPLY_REQUIRED")

        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_ADVICE_READY)
            if session.current_selection_advice_id is None:
                raise DomainError("CURRENT_SELECTION_ADVICE_REQUIRED")
            if session.current_reviewed_selection_id is None:
                raise DomainError("REVIEWED_SELECTION_UNAVAILABLE")

            try:
                selection_facts = self.repository.get_selection_facts(
                    session.current_reviewed_selection_id
                )
            except KeyError as exc:
                raise DomainError("REVIEWED_SELECTION_UNAVAILABLE") from exc

            if len(selected_three) != 3:
                raise DomainError("SELECTED_THREE_MUST_HAVE_EXACTLY_THREE")
            typed_three = (selected_three[0], selected_three[1], selected_three[2])
            if len(set(typed_three)) != 3:
                raise DomainError("DUPLICATE_SELECTION")
            if any(name not in selection_facts.self_team for name in typed_three):
                raise DomainError("SELECTION_OUTSIDE_REVIEWED_TEAM")
            if lead not in typed_three:
                raise DomainError("LEAD_NOT_IN_SELECTED_THREE")

            backline_values = tuple(name for name in typed_three if name != lead)
            backline = (backline_values[0], backline_values[1])
            snapshot = AppliedSelectionSnapshot(
                applied_selection_id=str(uuid4()),
                selected_three=typed_three,
                lead=lead,
                backline=backline,
                source_advice_id=session.current_selection_advice_id,
            )
            self.repository.append_applied_selection(session.session_id, snapshot)
            session.current_applied_selection_id = snapshot.applied_selection_id
            session.state = BattleState.BATTLE_READY
            session.bump_battle()
            self.repository.save_session(session)
        return snapshot

    def start_turn_capture(self) -> BattleTurn:
        with self.repository.transaction():
            session = self._require_session(BattleState.BATTLE_READY)
            if session.current_turn_id is not None:
                raise DomainError("CURRENT_TURN_ALREADY_EXISTS")
            turn = BattleTurn(turn_id=str(uuid4()), turn_number=1)
            self.repository.append_turn(session.session_id, turn)
            self._set_pending_turn(session, turn)
            self.repository.save_session(session)
        return turn

    def confirm_turn_facts(
        self,
        *,
        self_active: str,
        opponent_active: str,
        self_hp: HpBucket,
        opponent_hp: HpBucket,
        legal_moves: tuple[str, ...],
        legal_switches: tuple[str, ...],
        human_note: str,
        human_confirmed: bool,
    ) -> TurnFactsSnapshot:
        if not human_confirmed:
            raise DomainError("HUMAN_TURN_FACTS_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_active_session()
            if session.state not in {
                BattleState.TURN_CAPTURE_PENDING,
                BattleState.TURN_REVIEWED,
            }:
                raise DomainError("EXPECTED_TURN_CAPTURE_PENDING_OR_REVIEWED")
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_applied_selection_id is None:
                raise DomainError("APPLIED_SELECTION_REQUIRED")

            turn = self.repository.get_turn(session.current_turn_id)
            applied = self.repository.get_applied_selection(
                session.current_applied_selection_id
            )
            normalized_active = self_active.strip()
            normalized_opponent = opponent_active.strip()
            normalized_moves = tuple(name.strip() for name in legal_moves)
            normalized_switches = tuple(name.strip() for name in legal_switches)

            if normalized_active not in applied.selected_three:
                raise DomainError("SELF_ACTIVE_OUTSIDE_APPLIED_SELECTION")
            if any(name not in applied.selected_three for name in normalized_switches):
                raise DomainError("SWITCH_OUTSIDE_APPLIED_SELECTION")
            if normalized_active in normalized_switches:
                raise DomainError("SELF_ACTIVE_IN_SWITCH_CANDIDATES")

            previous_snapshot_id = session.current_reviewed_board_id
            try:
                snapshot = TurnFactsSnapshot(
                    turn_facts_id=str(uuid4()),
                    turn_id=turn.turn_id,
                    turn_number=turn.turn_number,
                    self_active=normalized_active,
                    opponent_active=normalized_opponent,
                    self_hp=self_hp,
                    opponent_hp=opponent_hp,
                    legal_moves=normalized_moves,
                    legal_switches=normalized_switches,
                    human_note=human_note.strip(),
                    previous_snapshot_id=previous_snapshot_id,
                )
            except ValueError as exc:
                raise DomainError(f"INVALID_TURN_FACTS:{exc}") from exc

            if previous_snapshot_id is not None:
                latest_turn_job = self.repository.latest_job_by_type(
                    session.session_id, JobType.TURN_ADVICE
                )
                if latest_turn_job is not None and latest_turn_job.status in {
                    JobStatus.QUEUED,
                    JobStatus.IN_FLIGHT,
                }:
                    self.repository.update_job_status(
                        latest_turn_job.job_id, JobStatus.CANCELLED
                    )

            self.repository.append_turn_facts(session.session_id, snapshot)
            session.current_reviewed_board_id = snapshot.turn_facts_id
            session.current_turn_advice_id = None
            session.state = BattleState.TURN_REVIEWED
            session.bump_battle()
            self.repository.save_session(session)
        return snapshot

    def request_turn_advice(self, command_id: str) -> JobEnvelope:
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_REVIEWED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_reviewed_board_id is None:
                raise DomainError("REVIEWED_TURN_FACTS_REQUIRED")
            if session.current_turn_advice_id is not None:
                raise DomainError("CURRENT_TURN_ADVICE_EXISTS")

            turn = self.repository.get_turn(session.current_turn_id)
            facts = self.repository.get_turn_facts(session.current_reviewed_board_id)
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.TURN_ADVICE
            )
            self._guard_provider_request(latest_job)
            payload = facts.to_canonical_dict()
            payload["battle_revision"] = session.battle_revision
            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=str(uuid4()),
                command_id=command_id,
                job_type=JobType.TURN_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=turn.turn_number,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.TURN_REVIEWED,
                input_snapshot_id=facts.turn_facts_id,
                request_payload_hash=self.payload_hash(payload),
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def apply_turn_advice_result(self, result: ResultEnvelope) -> ResultDisposition:
        with self.repository.transaction():
            job = self._load_result_job_or_audit(result)
            if job is None:
                return ResultDisposition.STALE_REJECTED
            if job.job_type is not JobType.TURN_ADVICE:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_TYPE_NOT_TURN_ADVICE"
                )
                return ResultDisposition.STALE_REJECTED
            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_job_by_type(session.session_id, JobType.TURN_ADVICE)
                if session is not None
                else None
            )
            current_turn_number: int | None = None
            if session is not None and session.current_turn_id is not None:
                current_turn_number = self.repository.get_turn(
                    session.current_turn_id
                ).turn_number
            current_snapshot_id = (
                session.current_reviewed_board_id if session is not None else None
            )
            reason = self._binding_failure_reason(
                session,
                latest_job,
                job,
                result,
                current_input_snapshot_id=current_snapshot_id,
                current_turn_number=current_turn_number,
            )
            if reason is not None:
                self.repository.audit_result(result, ResultDisposition.STALE_REJECTED, reason)
                return ResultDisposition.STALE_REJECTED

            assert session is not None
            assert session.current_turn_id is not None
            try:
                action_type = ActionType(str(result.payload["action_type"]))
                action_name = str(result.payload["action_name"]).strip()
                opponent_prediction = str(result.payload["opponent_prediction"]).strip()
                rationale = str(result.payload["rationale"]).strip()
                facts = self.repository.get_turn_facts(job.input_snapshot_id)
                legal_actions = (
                    facts.legal_moves
                    if action_type is ActionType.MOVE
                    else facts.legal_switches
                )
                if action_name not in legal_actions:
                    raise ValueError("action outside reviewed legal actions")
                advice = TurnAdviceSnapshot(
                    turn_advice_id=result.result_id,
                    turn_id=session.current_turn_id,
                    turn_number=facts.turn_number,
                    job_id=job.job_id,
                    input_snapshot_id=facts.turn_facts_id,
                    action_type=action_type,
                    action_name=action_name,
                    opponent_prediction=opponent_prediction,
                    rationale=rationale,
                    is_mock=True,
                )
            except (KeyError, TypeError, ValueError) as exc:
                self.repository.audit_result(
                    result, ResultDisposition.INVALID_REJECTED, f"INVALID_PAYLOAD:{exc}"
                )
                self.repository.update_job_status(job.job_id, JobStatus.FAILED)
                return ResultDisposition.INVALID_REJECTED

            self.repository.append_turn_advice(session.session_id, advice)
            session.current_turn_advice_id = advice.turn_advice_id
            session.bump_battle()
            self.repository.save_session(session)
            self.repository.update_job_status(job.job_id, JobStatus.SUCCEEDED)
            self.repository.audit_result(result, ResultDisposition.APPLIED, "BINDING_ACCEPTED")
        return ResultDisposition.APPLIED

    def record_actual_action(
        self,
        *,
        action_type: ActionType,
        action_name: str,
        human_confirmed: bool,
    ) -> RecordedAction:
        if not human_confirmed:
            raise DomainError("HUMAN_ACTION_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_REVIEWED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_reviewed_board_id is None:
                raise DomainError("REVIEWED_TURN_FACTS_REQUIRED")
            if session.current_turn_advice_id is None:
                raise DomainError("CURRENT_TURN_ADVICE_REQUIRED")
            if self.repository.has_recorded_action(session.current_turn_id):
                raise DomainError("ACTION_ALREADY_RECORDED")

            facts = self.repository.get_turn_facts(session.current_reviewed_board_id)
            legal_actions = (
                facts.legal_moves if action_type is ActionType.MOVE else facts.legal_switches
            )
            normalized_name = action_name.strip()
            if normalized_name not in legal_actions:
                raise DomainError("ACTION_OUTSIDE_REVIEWED_LEGAL_ACTIONS")

            action = RecordedAction(
                action_id=str(uuid4()),
                turn_id=session.current_turn_id,
                turn_number=facts.turn_number,
                action_type=action_type,
                action_name=normalized_name,
            )
            self.repository.append_recorded_action(session.session_id, action)
            session.state = BattleState.TURN_RECORDED
            session.bump_battle()
            self.repository.save_session(session)
        return action

    def next_turn(self) -> BattleTurn:
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_RECORDED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            current_turn = self.repository.get_turn(session.current_turn_id)
            turn = BattleTurn(
                turn_id=str(uuid4()),
                turn_number=current_turn.turn_number + 1,
            )
            self.repository.append_turn(session.session_id, turn)
            self._set_pending_turn(session, turn)
            self.repository.save_session(session)
        return turn

    def update_metadata(self) -> BattleSession:
        with self.repository.transaction():
            session = self._require_active_session()
            session.bump_metadata()
            self.repository.save_session(session)
        return session

    def recover_after_restart(self) -> None:
        with self.repository.transaction():
            self.repository.recover_unfinished_jobs()

    def _set_pending_turn(self, session: BattleSession, turn: BattleTurn) -> None:
        session.current_turn_id = turn.turn_id
        session.current_observation_id = None
        session.current_reviewed_board_id = None
        session.current_turn_advice_id = None
        session.state = BattleState.TURN_CAPTURE_PENDING
        session.bump_battle()

    def _load_result_job_or_audit(self, result: ResultEnvelope) -> JobEnvelope | None:
        try:
            return self.repository.get_job(result.job_id)
        except KeyError:
            self.repository.audit_result(
                result, ResultDisposition.STALE_REJECTED, "JOB_ID_MISMATCH"
            )
            return None

    @staticmethod
    def _guard_provider_request(latest_job: JobEnvelope | None) -> None:
        if latest_job is not None and latest_job.status in {
            JobStatus.QUEUED,
            JobStatus.IN_FLIGHT,
        }:
            raise DomainError("PROVIDER_REQUEST_PENDING")
        if latest_job is not None and latest_job.status is JobStatus.DELIVERY_UNKNOWN:
            raise DomainError("PROVIDER_DELIVERY_UNKNOWN")

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
        *,
        current_input_snapshot_id: str | None,
        current_turn_number: int | None,
    ) -> str | None:
        checks = (
            (latest_job is not None and latest_job.job_id == job.job_id, "JOB_ID_NOT_CURRENT"),
            (
                job.status in {JobStatus.QUEUED, JobStatus.IN_FLIGHT},
                "JOB_NOT_ACCEPTING_RESULTS",
            ),
            (result.contract_version == job.contract_version, "CONTRACT_VERSION_MISMATCH"),
            (result.command_id == job.command_id, "COMMAND_ID_MISMATCH"),
            (result.job_type is job.job_type, "JOB_TYPE_MISMATCH"),
            (session is not None, "NO_ACTIVE_MATCH"),
            (session is not None and result.session_id == session.session_id, "SESSION_MISMATCH"),
            (session is not None and result.match_id == session.match_id, "MATCH_MISMATCH"),
            (
                session is not None and result.generation == session.generation,
                "GENERATION_MISMATCH",
            ),
            (
                result.turn_number == job.turn_number
                and result.turn_number == current_turn_number,
                "TURN_MISMATCH",
            ),
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
                result.input_snapshot_id == current_input_snapshot_id
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
