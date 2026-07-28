"""SQLite single-writer repository facade."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from maple_next.persistence.job_store import JobStoreMixin
from maple_next.persistence.schema import migrate
from maple_next.persistence.selection_store import SelectionStoreMixin
from maple_next.persistence.session_store import SessionStoreMixin
from maple_next.persistence.turn_store import TurnStoreMixin


class SQLiteRepository(
    SessionStoreMixin,
    JobStoreMixin,
    SelectionStoreMixin,
    TurnStoreMixin,
):
    """The only component allowed to hold a writable SQLite connection."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
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
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()
