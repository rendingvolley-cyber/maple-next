"""Persistence for canonical match-level actual Mega Evolution state."""

from __future__ import annotations

import json

from maple_next.domain.mega_evolution import (
    MegaBattleState,
    MegaEvolutionError,
    mega_state_from_canonical_dict,
    mega_state_to_canonical_dict,
)
from maple_next.persistence.base import StoreBase


class MegaStoreMixin(StoreBase):
    """Read/write the one canonical Mega state bound to a battle session.

    The column lives on ``battle_sessions`` so the fact has exactly match
    lifetime.  Writes are transaction-aware and must participate in the
    application command that records the human-confirmed Result Entry.
    """

    def get_mega_state(self, session_id: str) -> MegaBattleState:
        row = self.connection.execute(
            "SELECT mega_state_json FROM battle_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        raw = row["mega_state_json"]
        try:
            payload = json.loads(str(raw))
            return mega_state_from_canonical_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError, MegaEvolutionError) as exc:
            raise ValueError("stored mega state is invalid") from exc

    def update_mega_state(self, session_id: str, state: MegaBattleState) -> None:
        """Persist Mega state inside the caller's existing transaction only."""

        if not self.connection.in_transaction:
            raise RuntimeError("MEGA_STATE_TRANSACTION_REQUIRED")
        encoded = json.dumps(
            mega_state_to_canonical_dict(state),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cursor = self.connection.execute(
            "UPDATE battle_sessions SET mega_state_json = ? WHERE session_id = ?",
            (encoded, session_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(session_id)
