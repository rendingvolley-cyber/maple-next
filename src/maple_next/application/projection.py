"""Pure Domain Projection. UI code must render this contract without inference."""

from __future__ import annotations

from dataclasses import dataclass

from maple_next.domain.enums import BattleState, JobStatus
from maple_next.domain.models import BattleSession
from maple_next.workers.contracts.models import JobEnvelope


@dataclass(frozen=True, slots=True)
class DomainProjection:
    application_mode: str
    primary_cta: str
    primary_cta_enabled: bool
    secondary_actions: tuple[str, ...]
    message: str
    provider_status: str
    provider_send_enabled: bool
    session_state: str | None
    battle_revision: int | None
    metadata_revision: int | None
    session_id: str | None
    match_id: str | None
    generation: int | None
    current_reviewed_selection_id: str | None
    current_selection_advice_id: str | None
    current_applied_selection_id: str | None
    current_turn_id: str | None = None
    current_reviewed_board_id: str | None = None
    current_turn_advice_id: str | None = None
    turn_number: int | None = None


def _active_projection(
    session: BattleSession,
    provider_status: str,
    *,
    primary_cta: str,
    primary_cta_enabled: bool,
    secondary_actions: tuple[str, ...],
    message: str,
    provider_send_enabled: bool,
    current_turn_number: int | None,
) -> DomainProjection:
    return DomainProjection(
        application_mode="ACTIVE_MATCH",
        primary_cta=primary_cta,
        primary_cta_enabled=primary_cta_enabled,
        secondary_actions=secondary_actions,
        message=message,
        provider_status=provider_status,
        provider_send_enabled=provider_send_enabled,
        session_state=session.state.value,
        battle_revision=session.battle_revision,
        metadata_revision=session.metadata_revision,
        session_id=session.session_id,
        match_id=session.match_id,
        generation=session.generation,
        current_reviewed_selection_id=session.current_reviewed_selection_id,
        current_selection_advice_id=session.current_selection_advice_id,
        current_applied_selection_id=session.current_applied_selection_id,
        current_turn_id=session.current_turn_id,
        current_reviewed_board_id=session.current_reviewed_board_id,
        current_turn_advice_id=session.current_turn_advice_id,
        turn_number=current_turn_number,
    )


def project(
    session: BattleSession | None,
    latest_provider_job: JobEnvelope | None,
    *,
    current_turn_number: int | None = None,
) -> DomainProjection:
    if session is None:
        return DomainProjection(
            application_mode="NO_ACTIVE_MATCH",
            primary_cta="CREATE_NEW_MATCH",
            primary_cta_enabled=True,
            secondary_actions=(),
            message="NO_ACTIVE_MATCH",
            provider_status="NONE",
            provider_send_enabled=False,
            session_state=None,
            battle_revision=None,
            metadata_revision=None,
            session_id=None,
            match_id=None,
            generation=None,
            current_reviewed_selection_id=None,
            current_selection_advice_id=None,
            current_applied_selection_id=None,
        )

    provider_status = latest_provider_job.status.value if latest_provider_job else "NONE"

    if latest_provider_job and latest_provider_job.status is JobStatus.DELIVERY_UNKNOWN:
        return _active_projection(
            session,
            provider_status,
            primary_cta="RESOLVE_DELIVERY_UNKNOWN",
            primary_cta_enabled=True,
            secondary_actions=("ABORT_MATCH",),
            message="PROVIDER_DELIVERY_UNKNOWN",
            provider_send_enabled=False,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.SELECTION_OPEN:
        if session.current_reviewed_selection_id is None:
            return _active_projection(
                session,
                provider_status,
                primary_cta="CONFIRM_SELECTION_FACTS",
                primary_cta_enabled=True,
                secondary_actions=("ABORT_MATCH",),
                message="SELECTION_FACTS_REQUIRED",
                provider_send_enabled=False,
                current_turn_number=current_turn_number,
            )
        if latest_provider_job and latest_provider_job.status in {
            JobStatus.QUEUED,
            JobStatus.IN_FLIGHT,
        }:
            return _active_projection(
                session,
                provider_status,
                primary_cta="WAIT_SELECTION_ADVICE",
                primary_cta_enabled=False,
                secondary_actions=("ABORT_MATCH",),
                message="SELECTION_ADVICE_PENDING",
                provider_send_enabled=False,
                current_turn_number=current_turn_number,
            )
        return _active_projection(
            session,
            provider_status,
            primary_cta="REQUEST_SELECTION_ADVICE",
            primary_cta_enabled=True,
            secondary_actions=("SET_HUMAN_SELECTION_CANDIDATE", "ABORT_MATCH"),
            message="SELECTION_FACTS_CONFIRMED",
            provider_send_enabled=True,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.SELECTION_ADVICE_READY:
        return _active_projection(
            session,
            provider_status,
            primary_cta="APPLY_SELECTION",
            primary_cta_enabled=True,
            secondary_actions=("REQUEST_SELECTION_ADVICE", "ABORT_MATCH"),
            message="SELECTION_ADVICE_READY",
            provider_send_enabled=True,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.BATTLE_READY:
        return _active_projection(
            session,
            provider_status,
            primary_cta="START_TURN_CAPTURE",
            primary_cta_enabled=True,
            secondary_actions=("ABORT_MATCH",),
            message="BATTLE_READY",
            provider_send_enabled=False,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.TURN_CAPTURE_PENDING:
        return _active_projection(
            session,
            provider_status,
            primary_cta="CONFIRM_TURN_FACTS",
            primary_cta_enabled=True,
            secondary_actions=("ABORT_MATCH",),
            message="TURN_FACTS_REQUIRED",
            provider_send_enabled=False,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.TURN_REVIEWED:
        if latest_provider_job and latest_provider_job.status in {
            JobStatus.QUEUED,
            JobStatus.IN_FLIGHT,
        }:
            return _active_projection(
                session,
                provider_status,
                primary_cta="WAIT_TURN_ADVICE",
                primary_cta_enabled=False,
                secondary_actions=("CORRECT_TURN_FACTS", "ABORT_MATCH"),
                message="TURN_ADVICE_PENDING",
                provider_send_enabled=False,
                current_turn_number=current_turn_number,
            )
        if session.current_turn_advice_id is None:
            return _active_projection(
                session,
                provider_status,
                primary_cta="REQUEST_TURN_ADVICE",
                primary_cta_enabled=True,
                secondary_actions=("CORRECT_TURN_FACTS", "ABORT_MATCH"),
                message="TURN_FACTS_REVIEWED",
                provider_send_enabled=True,
                current_turn_number=current_turn_number,
            )
        return _active_projection(
            session,
            provider_status,
            primary_cta="RECORD_ACTUAL_ACTION",
            primary_cta_enabled=True,
            secondary_actions=("CORRECT_TURN_FACTS", "ABORT_MATCH"),
            message="TURN_ADVICE_READY",
            provider_send_enabled=False,
            current_turn_number=current_turn_number,
        )

    if session.state is BattleState.TURN_RECORDED:
        return _active_projection(
            session,
            provider_status,
            primary_cta="NEXT_TURN",
            primary_cta_enabled=True,
            secondary_actions=("ABORT_MATCH",),
            message="TURN_RECORDED",
            provider_send_enabled=False,
            current_turn_number=current_turn_number,
        )

    return _active_projection(
        session,
        provider_status,
        primary_cta="NO_ACTION_IN_BATTLE1_SCOPE",
        primary_cta_enabled=False,
        secondary_actions=("ABORT_MATCH",),
        message="OUTSIDE_BATTLE1_MANUAL_SCOPE",
        provider_send_enabled=False,
        current_turn_number=current_turn_number,
    )
