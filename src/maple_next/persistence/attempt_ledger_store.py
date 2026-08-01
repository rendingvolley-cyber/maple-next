"""Durable exactly-one attempt ledger for the production Gemini Selection lane.

Distinct from ``async_jobs.dispatch_count`` (which guards a single job
against being dispatched twice): this ledger guards a Selection *identity*
(session/match/generation/battle_revision/reviewed_selection_id) against
ever producing a second production Gemini job, even after the first job
reaches a terminal state or the process restarts. Only the production
Gemini lane consults this table; the mock/dev Selection Advice lane never
calls these methods and is unaffected.
"""

from __future__ import annotations

import sqlite3

from maple_next.persistence.base import StoreBase

GEMINI_SELECTION_PRODUCTION_LANE = "GEMINI_SELECTION_PRODUCTION"
TURN_ADVICE_LANE = "TURN_ADVICE"


class AttemptLedgerStoreMixin(StoreBase):
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
