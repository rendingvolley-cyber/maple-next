"""Mutable operator-owned self-team presets stored in runtime SQLite."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from maple_next.domain.models import SelfTeamPreset
from maple_next.domain.team_build import ChampionsTeamBuild
from maple_next.persistence.base import StoreBase


class TeamPresetStoreMixin(StoreBase):
    def insert_self_team_preset(
        self,
        *,
        preset_id: str,
        name: str,
        normalized_name: str,
        self_team: tuple[str, str, str, str, str, str],
        team_build: ChampionsTeamBuild | None = None,
    ) -> None:
        build_schema_version, build_json, build_sha256 = self._build_storage_values(
            self_team, team_build
        )
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO self_team_presets (
                preset_id, name, normalized_name, self_team_json,
                build_schema_version, team_build_json, team_build_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                preset_id,
                name,
                normalized_name,
                json.dumps(self_team, ensure_ascii=False),
                build_schema_version,
                build_json,
                build_sha256,
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
        team_build: ChampionsTeamBuild | None = None,
    ) -> bool:
        build_schema_version, build_json, build_sha256 = self._build_storage_values(
            self_team, team_build
        )
        cursor = self.connection.execute(
            """
            UPDATE self_team_presets
            SET name = ?, normalized_name = ?, self_team_json = ?,
                build_schema_version = ?, team_build_json = ?,
                team_build_sha256 = ?, updated_at = ?
            WHERE preset_id = ?
            """,
            (
                name,
                normalized_name,
                json.dumps(self_team, ensure_ascii=False),
                build_schema_version,
                build_json,
                build_sha256,
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
    def _build_storage_values(
        self_team: tuple[str, str, str, str, str, str],
        team_build: ChampionsTeamBuild | None,
    ) -> tuple[str, str | None, str | None]:
        if team_build is None:
            return "maple-team.v1", None, None
        if team_build.pokemon_names != tuple(self_team):
            raise ValueError("team build names must match self_team")
        return (
            "maple-team.v2",
            team_build.canonical_json_bytes().decode("utf-8"),
            team_build.sha256(),
        )

    @staticmethod
    def _preset_from_row(row: sqlite3.Row) -> SelfTeamPreset:
        try:
            values = cast(list[str], json.loads(str(row["self_team_json"])))
            if not isinstance(values, list) or len(values) != 6:
                raise ValueError("stored names are invalid")
            self_team = tuple(values)
            if any(not isinstance(value, str) or not value.strip() for value in self_team):
                raise ValueError("stored names are invalid")

            build_schema_version = str(row["build_schema_version"] or "maple-team.v1")
            raw_build = row["team_build_json"]
            raw_hash = row["team_build_sha256"]
            team_build: ChampionsTeamBuild | None = None
            team_build_sha256: str | None = None
            if build_schema_version == "maple-team.v2":
                if raw_build is None or raw_hash is None:
                    raise ValueError("stored detailed preset is incomplete")
                team_build = ChampionsTeamBuild.from_json_bytes(str(raw_build).encode("utf-8"))
                if team_build.pokemon_names != self_team:
                    raise ValueError("stored detailed preset names do not match")
                team_build_sha256 = str(raw_hash)
                if team_build.sha256() != team_build_sha256:
                    raise ValueError("stored detailed preset hash does not match")
            elif build_schema_version != "maple-team.v1":
                raise ValueError("stored preset schema is invalid")
            elif raw_build is not None or raw_hash is not None:
                raise ValueError("stored names-only preset contains detailed data")

            return SelfTeamPreset(
                preset_id=str(row["preset_id"]),
                name=str(row["name"]),
                self_team=(
                    self_team[0],
                    self_team[1],
                    self_team[2],
                    self_team[3],
                    self_team[4],
                    self_team[5],
                ),
                created_at_utc=str(row["created_at"]),
                updated_at_utc=str(row["updated_at"]),
                build_schema_version=build_schema_version,
                team_build=team_build,
                team_build_sha256=team_build_sha256,
            )
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            # Do not expose raw JSON or SQLite details to the operator/UI.
            raise ValueError("stored team preset is invalid") from exc
