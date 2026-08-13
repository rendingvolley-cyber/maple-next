"""SQLite single-writer repository facade."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from maple_next.domain.enums import ActionOrder, ActionType
from maple_next.domain.turn_state import ActionResultDelta, TurnIdentity
from maple_next.persistence.attempt_ledger_store import AttemptLedgerStoreMixin
from maple_next.persistence.job_store import JobStoreMixin
from maple_next.persistence.match_store import MatchStoreMixin
from maple_next.persistence.schema import migrate
from maple_next.persistence.selection_store import SelectionStoreMixin
from maple_next.persistence.session_store import SessionStoreMixin
from maple_next.persistence.team_preset_store import TeamPresetStoreMixin
from maple_next.persistence.turn_state_store import TurnStateStoreMixin
from maple_next.persistence.turn_store import TurnStoreMixin


class SQLiteRepository(
    SessionStoreMixin,
    JobStoreMixin,
    SelectionStoreMixin,
    TurnStoreMixin,
    MatchStoreMixin,
    AttemptLedgerStoreMixin,
    TeamPresetStoreMixin,
    TurnStateStoreMixin,
):
    """The only component allowed to hold a writable SQLite connection."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        migrate(self.connection)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit all writes in one main-process transaction or roll them all back."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def record_rich_action_completion(
        self,
        *,
        transaction_id: str,
        identity: TurnIdentity,
        own_action_type: ActionType,
        own_action_name: str,
        opponent_action_type: ActionType | None,
        opponent_action_name: str,
        action_order: ActionOrder,
        delta: ActionResultDelta,
    ) -> None:
        """Public transaction owner for the rich action completion write.

        Owns transaction begin/commit/rollback, binding validation, the
        delta INSERT, and the completion INSERT as a single unit: any
        failure (binding mismatch, missing confirmed state, constraint
        violation) leaves neither the delta nor the completion committed.
        Callers must not wrap this in their own ``transaction()`` block.

        ``opponent_action_type=None`` is the explicit typed observation
        that the opponent's action was genuinely not confirmed -- the same
        shape ``BattleApplication.record_actual_action`` already uses for
        this case. It is still committed through this one boundary, not a
        separate non-atomic path.
        """

        with self.transaction():
            self.append_rich_action_completion(
                transaction_id=transaction_id,
                identity=identity,
                own_action_type=own_action_type,
                own_action_name=own_action_name,
                opponent_action_type=opponent_action_type,
                opponent_action_name=opponent_action_name,
                action_order=action_order,
                delta=delta,
            )

    def append_rich_action_completion(
        self,
        *,
        transaction_id: str,
        identity: TurnIdentity,
        own_action_type: ActionType,
        own_action_name: str,
        opponent_action_type: ActionType | None,
        opponent_action_name: str,
        action_order: ActionOrder,
        delta: ActionResultDelta,
    ) -> None:
        """Append a rich completion inside the caller's active transaction.

        The application Battle Record command uses this transaction-aware
        form so the legacy action, rich result, and session transition share
        one commit boundary. Standalone callers should continue to use
        :meth:`record_rich_action_completion`.
        """

        if not self.connection.in_transaction:
            raise RuntimeError("RICH_ACTION_COMPLETION_TRANSACTION_REQUIRED")
        self._record_rich_action_completion_row(
            transaction_id=transaction_id,
            identity=identity,
            own_action_type=own_action_type,
            own_action_name=own_action_name,
            opponent_action_type=opponent_action_type,
            opponent_action_name=opponent_action_name,
            action_order=action_order,
            delta=delta,
        )

    def close(self) -> None:
        self.connection.close()
