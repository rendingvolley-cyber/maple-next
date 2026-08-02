"""Mutable operator-owned self-team presets stored in the runtime SQLite DB."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from maple_next.domain.models import SelfTeamPreset
from maple_next.persistence.base import StoreBase


class TeamPresetStoreMixin(StoreBase):
    def insert_self_team_preset(
        self,
        *,
        preset_id: str,
        name: str,
        normalized_name: str,
        self_team: tuple[str, str, str, str, str, str],
    ) -> None:
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO self_team_presets (
                preset_id, name, normalized_name, self_team_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                preset_id,
                name,
                normalized_name,
                json.dumps(self_team, ensure_ascii=False),
                now,
                now,
            ),
        )

    def update_self_team_preset(
        self,
        *,
        preset_id: str,
        name: str,
        normalized_name: str,
        self_team: tuple[str, str, str, str, str, str],
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE self_team_presets
            SET name = ?, normalized_name = ?, self_team_json = ?, updated_at = ?
            WHERE preset_id = ?
            """,
            (
                name,
                normalized_name,
                json.dumps(self_team, ensure_ascii=False),
                self._now(),
                preset_id,
            ),
        )
        return cursor.rowcount == 1

    def delete_self_team_preset(self, preset_id: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM self_team_presets WHERE preset_id = ?", (preset_id,)
        )
        return cursor.rowcount == 1

    def list_self_team_presets(self) -> tuple[SelfTeamPreset, ...]:
        rows = self.connection.execute(
            "SELECT * FROM self_team_presets ORDER BY normalized_name, preset_id"
        ).fetchall()
        return tuple(self._preset_from_row(row) for row in rows)

    def get_self_team_preset(self, preset_id: str) -> SelfTeamPreset | None:
        row = self.connection.execute(
            "SELECT * FROM self_team_presets WHERE preset_id = ?", (preset_id,)
        ).fetchone()
        return None if row is None else self._preset_from_row(row)

    def set_last_used_self_team_preset(self, preset_id: str) -> None:
        self.connection.execute(
            """
            UPDATE operator_preferences
            SET last_used_self_team_preset_id = ?
            WHERE singleton_id = 1
            """,
            (preset_id,),
        )

    def get_last_used_self_team_preset(self) -> SelfTeamPreset | None:
        row = self.connection.execute(
            """
            SELECT preset.*
            FROM operator_preferences AS preference
            JOIN self_team_presets AS preset
              ON preset.preset_id = preference.last_used_self_team_preset_id
            WHERE preference.singleton_id = 1
            """
        ).fetchone()
        return None if row is None else self._preset_from_row(row)

    @staticmethod
    def _preset_from_row(row: sqlite3.Row) -> SelfTeamPreset:
        values = cast(list[str], json.loads(str(row["self_team_json"])))
        return SelfTeamPreset(
            preset_id=str(row["preset_id"]),
            name=str(row["name"]),
            self_team=(values[0], values[1], values[2], values[3], values[4], values[5]),
            created_at_utc=str(row["created_at"]),
            updated_at_utc=str(row["updated_at"]),
        )
