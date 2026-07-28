"""Shared persistence primitives for SQLite store mixins."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class StoreBase:
    connection: sqlite3.Connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
