"""Bundle 2 (Gemini V2): persistence for explicit legal-switch confirmations.

New, additive table only (see ``schema.py``). Absence of a row for a given
binding means the durable truth is ``NOT_CAPTURED_OR_UNRESOLVED`` -- this
module never infers or backfills a confirmation. Restart-safe: hydration is
an exact-binding lookup, never a fuzzy "names happen to match" resurrection
of a stale confirmation.
"""

from __future__ import annotations

import json
import sqlite3

from maple_next.domain.legal_switches import LegalSwitchConfirmation, LegalSwitchStatus
from maple_next.domain.turn_state import ConfirmationMeta, TurnIdentity
from maple_next.persistence.base import StoreBase


class LegalSwitchStoreMixin(StoreBase):
    def upsert_legal_switch_confirmation(self, confirmation: LegalSwitchConfirmation) -> None:
        """Insert, or update in place when this exact binding was already confirmed."""

        identity = confirmation.identity
        self.connection.execute(
            """
            INSERT INTO legal_switch_confirmations (
                confirmation_id, session_id, match_id, generation, turn_id,
                turn_number, battle_revision, based_on_confirmed_state_id,
                applied_selection_id, legal_switches_json, status,
                confirmed_by_human, confirmed_at_utc, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                session_id, match_id, generation, turn_id, turn_number,
                battle_revision, based_on_confirmed_state_id, applied_selection_id
            ) DO UPDATE SET
                confirmation_id = excluded.confirmation_id,
                legal_switches_json = excluded.legal_switches_json,
                status = excluded.status,
                confirmed_by_human = excluded.confirmed_by_human,
                confirmed_at_utc = excluded.confirmed_at_utc,
                provenance = excluded.provenance,
                created_at = excluded.created_at
            """,
            (
                confirmation.confirmation_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                confirmation.based_on_confirmed_state_id,
                confirmation.applied_selection_id,
                json.dumps(list(confirmation.legal_switches)),
                confirmation.status.value,
                int(confirmation.confirmation.confirmed_by_human),
                confirmation.confirmation.confirmed_at_utc,
                confirmation.confirmation.provenance,
                self._now(),
            ),
        )

    @staticmethod
    def _legal_switch_confirmation_from_row(row: sqlite3.Row) -> LegalSwitchConfirmation:
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        confirmation = ConfirmationMeta(
            confirmed_by_human=bool(row["confirmed_by_human"]),
            confirmed_at_utc=str(row["confirmed_at_utc"]),
            provenance=str(row["provenance"]),
        )
        return LegalSwitchConfirmation(
            confirmation_id=str(row["confirmation_id"]),
            identity=identity,
            based_on_confirmed_state_id=str(row["based_on_confirmed_state_id"]),
            applied_selection_id=str(row["applied_selection_id"]),
            legal_switches=tuple(json.loads(str(row["legal_switches_json"]))),
            status=LegalSwitchStatus(str(row["status"])),
            confirmation=confirmation,
        )

    def get_legal_switch_confirmation(
        self,
        *,
        identity: TurnIdentity,
        based_on_confirmed_state_id: str,
        applied_selection_id: str,
    ) -> LegalSwitchConfirmation | None:
        """Exact-match lookup only.

        Any mismatch in identity, ``based_on_confirmed_state_id``, or
        ``applied_selection_id`` is a miss -- the caller must treat a miss as
        NOT_CAPTURED_OR_UNRESOLVED, never fall back to a stale confirmation
        from a prior Turn/revision/roster binding.
        """

        row = self.connection.execute(
            """
            SELECT * FROM legal_switch_confirmations
            WHERE session_id = ? AND match_id = ? AND generation = ?
              AND turn_id = ? AND turn_number = ? AND battle_revision = ?
              AND based_on_confirmed_state_id = ? AND applied_selection_id = ?
            """,
            (
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                based_on_confirmed_state_id,
                applied_selection_id,
            ),
        ).fetchone()
        if row is None:
            return None
        return self._legal_switch_confirmation_from_row(row)
