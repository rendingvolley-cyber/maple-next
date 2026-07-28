"""Test/dev-only mock Selection Advice adapter with no network path."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import ResultDisposition
from maple_next.workers.contracts.models import ResultEnvelope


@dataclass(frozen=True, slots=True)
class MockSelectionAdviceResult:
    result_id: str
    disposition: ResultDisposition


class MockSelectionAdviceAdapter:
    """Inject a versioned mock result through the existing application contract."""

    def __init__(self) -> None:
        self.network_call_count = 0

    def submit(
        self,
        application: BattleApplication,
        *,
        selected_three: tuple[str, str, str],
        lead: str,
    ) -> MockSelectionAdviceResult:
        job = application.request_selection_advice(f"mock-ui-{uuid4()}")
        result = ResultEnvelope(
            contract_version=job.contract_version,
            result_id=str(uuid4()),
            job_id=job.job_id,
            command_id=job.command_id,
            job_type=job.job_type,
            session_id=job.session_id,
            match_id=job.match_id,
            generation=job.generation,
            turn_number=job.turn_number,
            base_battle_revision=job.base_battle_revision,
            expected_state=job.expected_state,
            input_snapshot_id=job.input_snapshot_id,
            request_payload_hash=job.request_payload_hash,
            payload={"selected_three": list(selected_three), "lead": lead},
        )
        disposition = application.apply_selection_advice_result(result)
        return MockSelectionAdviceResult(result_id=result.result_id, disposition=disposition)
