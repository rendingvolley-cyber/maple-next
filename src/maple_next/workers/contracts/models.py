"""Versioned immutable Job / Result envelopes.

This module intentionally has no persistence imports. A worker receives values only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from maple_next.domain.enums import BattleState, JobStatus, JobType


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    contract_version: str
    job_id: str
    command_id: str
    job_type: JobType
    session_id: str
    match_id: str
    generation: int
    turn_number: int | None
    base_battle_revision: int
    expected_state: BattleState
    input_snapshot_id: str
    request_payload_hash: str
    human_authorized_at: datetime
    status: JobStatus = JobStatus.QUEUED


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    contract_version: str
    result_id: str
    job_id: str
    command_id: str
    job_type: JobType
    session_id: str
    match_id: str
    generation: int
    turn_number: int | None
    base_battle_revision: int
    expected_state: BattleState
    input_snapshot_id: str
    request_payload_hash: str
    payload: dict[str, Any]
