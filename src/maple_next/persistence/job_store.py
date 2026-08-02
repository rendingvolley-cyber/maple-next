"""Immutable async Job ledger and Result audit persistence."""

from __future__ import annotations

from datetime import datetime

from maple_next.domain.enums import BattleState, JobStatus, JobType, ResultDisposition
from maple_next.persistence.base import StoreBase
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

# Fixed canonical value stored in the NOT NULL async_job_results.payload_json
# column. Raw/untrusted provider payloads must never be persisted there.
CANONICAL_EMPTY_PAYLOAD_JSON = "{}"


class JobStoreMixin(StoreBase):
    def insert_job(self, job: JobEnvelope) -> None:
        self.connection.execute(
            """
            INSERT INTO async_jobs (
                job_id, command_id, job_type, session_id, match_id, generation,
                turn_number, base_battle_revision, expected_state, input_snapshot_id,
                request_payload_hash, human_authorized_at, status, dispatch_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                job.job_id,
                job.command_id,
                job.job_type.value,
                job.session_id,
                job.match_id,
                job.generation,
                job.turn_number,
                job.base_battle_revision,
                job.expected_state.value,
                job.input_snapshot_id,
                job.request_payload_hash,
                job.human_authorized_at.isoformat(),
                job.status.value,
                self._now(),
                self._now(),
            ),
        )

    def get_job(self, job_id: str) -> JobEnvelope:
        row = self.connection.execute(
            "SELECT * FROM async_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobEnvelope(
            contract_version="maple-worker.v1",
            job_id=str(row["job_id"]),
            command_id=str(row["command_id"]),
            job_type=JobType(str(row["job_type"])),
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_number=row["turn_number"],
            base_battle_revision=int(row["base_battle_revision"]),
            expected_state=BattleState(str(row["expected_state"])),
            input_snapshot_id=str(row["input_snapshot_id"]),
            request_payload_hash=str(row["request_payload_hash"]),
            human_authorized_at=datetime.fromisoformat(str(row["human_authorized_at"])),
            status=JobStatus(str(row["status"])),
        )

    def update_job_status(self, job_id: str, status: JobStatus) -> None:
        self.connection.execute(
            "UPDATE async_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (status.value, self._now(), job_id),
        )

    def mark_job_in_flight(self, job_id: str) -> None:
        """Transition to IN_FLIGHT and record exactly one dispatch attempt."""

        self.connection.execute(
            """
            UPDATE async_jobs
            SET status = ?, dispatch_count = dispatch_count + 1, updated_at = ?
            WHERE job_id = ?
            """,
            (JobStatus.IN_FLIGHT.value, self._now(), job_id),
        )

    def set_job_status_for_test(self, job_id: str, status: JobStatus) -> None:
        """Synthetic-only transition used to model a process crash boundary."""

        with self.connection:
            self.update_job_status(job_id, status)

    def get_job_dispatch_count(self, job_id: str) -> int:
        row = self.connection.execute(
            "SELECT dispatch_count FROM async_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return int(row["dispatch_count"])

    def latest_job_by_type(self, session_id: str, job_type: JobType) -> JobEnvelope | None:
        row = self.connection.execute(
            """
            SELECT job_id FROM async_jobs
            WHERE session_id = ? AND job_type = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (session_id, job_type.value),
        ).fetchone()
        return self.get_job(str(row["job_id"])) if row is not None else None

    def latest_provider_job(self, session_id: str) -> JobEnvelope | None:
        row = self.connection.execute(
            """
            SELECT job_id FROM async_jobs
            WHERE session_id = ? AND job_type IN (?, ?)
            ORDER BY rowid DESC LIMIT 1
            """,
            (session_id, JobType.SELECTION_ADVICE.value, JobType.TURN_ADVICE.value),
        ).fetchone()
        return self.get_job(str(row["job_id"])) if row is not None else None

    def recover_unfinished_jobs(self) -> None:
        rows = self.connection.execute(
            "SELECT job_id, job_type, status FROM async_jobs WHERE status IN (?, ?)",
            (JobStatus.QUEUED.value, JobStatus.IN_FLIGHT.value),
        ).fetchall()
        for row in rows:
            job_type = JobType(str(row["job_type"]))
            current = JobStatus(str(row["status"]))
            recovered = JobStatus.INTERRUPTED
            if (
                job_type in {JobType.SELECTION_ADVICE, JobType.TURN_ADVICE}
                and current is JobStatus.IN_FLIGHT
            ):
                recovered = JobStatus.DELIVERY_UNKNOWN
            self.connection.execute(
                "UPDATE async_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (recovered.value, self._now(), str(row["job_id"])),
            )

    def has_applied_result(self, job_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM async_job_results
            WHERE job_id = ? AND disposition = ? LIMIT 1
            """,
            (job_id, ResultDisposition.APPLIED.value),
        ).fetchone()
        return row is not None

    def audit_result(
        self,
        result: ResultEnvelope,
        disposition: ResultDisposition,
        reason: str,
    ) -> None:
        """Record a fixed, sanitized audit row for a provider result.

        Never persists ``result.payload``: raw/untrusted provider bodies must
        not reach ``async_job_results``. ``payload_json`` is schema-required
        (``NOT NULL``) so a fixed canonical empty object is stored instead of
        any provider-derived content; ``reason`` must already be one of the
        caller's fixed sanitized codes, never dynamic exception text.
        """

        self.connection.execute(
            """
            INSERT INTO async_job_results (
                result_id, job_id, disposition, reason, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                result.result_id,
                result.job_id,
                disposition.value,
                reason,
                CANONICAL_EMPTY_PAYLOAD_JSON,
                self._now(),
            ),
        )

    def result_audits(self, job_id: str) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            """
            SELECT disposition, reason FROM async_job_results
            WHERE job_id = ? ORDER BY audit_id
            """,
            (job_id,),
        ).fetchall()
        return [(str(row["disposition"]), str(row["reason"])) for row in rows]
