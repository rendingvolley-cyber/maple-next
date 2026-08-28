"""Durable exactly-one attempt ledger for the production Gemini Selection lane.

Distinct from ``async_jobs.dispatch_count`` (which guards a single job
against being dispatched twice): this ledger guards a Selection *identity*
(session/match/generation/battle_revision/reviewed_selection_id) against
concurrent or unapproved production jobs. A verified transient terminal
failure may move the reservation to a new human-explicit job. Only the
production Gemini lane consults this table; the mock/dev lane is unaffected.
"""

from __future__ import annotations

import sqlite3

from maple_next.persistence.base import StoreBase

GEMINI_SELECTION_PRODUCTION_LANE = "GEMINI_SELECTION_PRODUCTION"
TURN_ADVICE_LANE = "TURN_ADVICE"
SELECTION_PROVIDER_ATTEMPT_LANE = "GEMINI_SELECTION_PROVIDER"


class AttemptLedgerStoreMixin(StoreBase):
    def start_selection_provider_attempt(
        self, *, job_id: str, attempt_ordinal: int, model: str
    ) -> None:
        """Persist sanitized evidence before one Selection provider call starts."""

        self.connection.execute(
            """
            INSERT INTO provider_attempt_audits (
                job_id, lane, attempt_ordinal, model, outcome, reason,
                started_at_utc, completed_at_utc
            ) VALUES (?, ?, ?, ?, 'STARTED', '', ?, NULL)
            """,
            (job_id, SELECTION_PROVIDER_ATTEMPT_LANE, attempt_ordinal, model, self._now()),
        )
        self.connection.commit()

    def finish_selection_provider_attempt(
        self, *, job_id: str, attempt_ordinal: int, outcome: str, reason: str = ""
    ) -> None:
        """Finish one attempt using only allowlisted outcome/reason metadata."""

        if outcome not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("invalid provider attempt outcome")
        cursor = self.connection.execute(
            """
            UPDATE provider_attempt_audits
            SET outcome = ?, reason = ?, completed_at_utc = ?
            WHERE job_id = ? AND lane = ? AND attempt_ordinal = ?
              AND outcome = 'STARTED'
            """,
            (
                outcome,
                reason,
                self._now(),
                job_id,
                SELECTION_PROVIDER_ATTEMPT_LANE,
                attempt_ordinal,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("PROVIDER_ATTEMPT_AUDIT_NOT_STARTED")
        self.connection.commit()

    def selection_provider_attempt_audits(
        self, job_id: str
    ) -> list[tuple[int, str, str, str]]:
        """Return ordered, credential-free Selection attempt evidence."""

        rows = self.connection.execute(
            """
            SELECT attempt_ordinal, model, outcome, reason
            FROM provider_attempt_audits
            WHERE job_id = ? AND lane = ?
            ORDER BY attempt_ordinal
            """,
            (job_id, SELECTION_PROVIDER_ATTEMPT_LANE),
        ).fetchall()
        return [
            (
                int(row["attempt_ordinal"]),
                str(row["model"]),
                str(row["outcome"]),
                str(row["reason"]),
            )
            for row in rows
        ]

    def reserve_gemini_selection_attempt(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        battle_revision: int,
        reviewed_selection_id: str,
        job_id: str,
    ) -> bool:
        """Atomically reserve the one durable production Gemini attempt.

        Returns True only on the call that performs the reservation for this
        exact identity. Returns False (no row written) if this identity was
        already reserved, by this call or a previous one, including across a
        restart.
        """

        try:
            self.connection.execute(
                """
                INSERT INTO gemini_selection_attempt_ledger (
                    session_id, match_id, generation, battle_revision,
                    reviewed_selection_id, lane, job_id, consumed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    match_id,
                    generation,
                    battle_revision,
                    reviewed_selection_id,
                    GEMINI_SELECTION_PRODUCTION_LANE,
                    job_id,
                    self._now(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def gemini_selection_attempt_reserved(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        reviewed_selection_id: str,
    ) -> bool:
        """Has this Selection round already reserved its one production attempt?

        Deliberately does not filter by ``battle_revision``: a successful
        attempt bumps the session's battle_revision as a side effect of
        applying the result, but ``reviewed_selection_id`` stays fixed for
        the entire round (it only changes when the human re-confirms
        Selection facts, which is a genuinely new identity). Filtering on
        the reservation-time battle_revision here would make this check
        forget that a round already consumed its attempt the moment the
        result was applied.
        """

        row = self.connection.execute(
            """
            SELECT 1 FROM gemini_selection_attempt_ledger
            WHERE session_id = ? AND match_id = ? AND generation = ?
            AND reviewed_selection_id = ? AND lane = ?
            LIMIT 1
            """,
            (
                session_id,
                match_id,
                generation,
                reviewed_selection_id,
                GEMINI_SELECTION_PRODUCTION_LANE,
            ),
        ).fetchone()
        return row is not None

    def replace_gemini_selection_attempt_reservation(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        battle_revision: int,
        reviewed_selection_id: str,
        expected_job_id: str,
        new_job_id: str,
    ) -> bool:
        """Move one current Selection reservation to a new explicit-send job.

        The caller must establish transient-failure eligibility inside the
        same ``BEGIN IMMEDIATE`` transaction. Matching ``expected_job_id``
        makes a concurrent/double activation a zero-row update.
        """

        cursor = self.connection.execute(
            """
            UPDATE gemini_selection_attempt_ledger
            SET job_id = ?, consumed_at_utc = ?
            WHERE session_id = ? AND match_id = ? AND generation = ?
              AND battle_revision = ? AND reviewed_selection_id = ?
              AND lane = ? AND job_id = ?
            """,
            (
                new_job_id,
                self._now(),
                session_id,
                match_id,
                generation,
                battle_revision,
                reviewed_selection_id,
                GEMINI_SELECTION_PRODUCTION_LANE,
                expected_job_id,
            ),
        )
        return cursor.rowcount == 1

    def reserve_turn_advice_attempt(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        turn_number: int,
        battle_revision: int,
        reviewed_snapshot_id: str,
        request_payload_hash: str,
        job_id: str,
    ) -> bool:
        """Atomically reserve the one durable Turn Advice attempt for this identity.

        Sibling of :meth:`reserve_gemini_selection_attempt`, scoped to Turn
        identity ``(session_id, match_id, generation, turn_number,
        battle_revision, reviewed_snapshot_id)``. Returns True only on the
        call that performs the reservation; False if already reserved,
        including across a process restart.
        """

        try:
            self.connection.execute(
                """
                INSERT INTO turn_advice_attempt_ledger (
                    session_id, match_id, generation, turn_number, battle_revision,
                    reviewed_snapshot_id, request_payload_hash, lane, job_id, consumed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    match_id,
                    generation,
                    turn_number,
                    battle_revision,
                    reviewed_snapshot_id,
                    request_payload_hash,
                    TURN_ADVICE_LANE,
                    job_id,
                    self._now(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def turn_advice_attempt_reserved(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        turn_number: int,
        reviewed_snapshot_id: str,
    ) -> bool:
        """Has this Turn round already reserved its one Turn Advice attempt?

        Deliberately does not filter by ``battle_revision`` — same rationale
        as :meth:`gemini_selection_attempt_reserved`: ``reviewed_snapshot_id``
        stays fixed for the whole round even as ``battle_revision`` bumps
        when the reserved attempt's result is later applied.
        """

        row = self.connection.execute(
            """
            SELECT 1 FROM turn_advice_attempt_ledger
            WHERE session_id = ? AND match_id = ? AND generation = ?
            AND turn_number = ? AND reviewed_snapshot_id = ? AND lane = ?
            LIMIT 1
            """,
            (
                session_id,
                match_id,
                generation,
                turn_number,
                reviewed_snapshot_id,
                TURN_ADVICE_LANE,
            ),
        ).fetchone()
        return row is not None
