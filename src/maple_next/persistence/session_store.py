"""BattleSession and immutable reviewed Selection fact persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from maple_next.domain.enums import BattleState
from maple_next.domain.models import BattleSession, SelectionFacts
from maple_next.domain.team_build import ChampionsTeamBuild
from maple_next.persistence.base import StoreBase


class SessionStoreMixin(StoreBase):
    def count_sessions(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM battle_sessions").fetchone()
        assert row is not None
        return int(row["count"])

    def next_generation(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 AS generation FROM battle_sessions"
        ).fetchone()
        assert row is not None
        return int(row["generation"])

    def insert_session(self, session: BattleSession) -> None:
        self.connection.execute(
            """
            INSERT INTO battle_sessions (
                session_id, match_id, generation, state, battle_revision, metadata_revision,
                current_reviewed_selection_id, current_selection_advice_id,
                current_applied_selection_id, current_turn_id, current_observation_id,
                current_reviewed_board_id, current_turn_advice_id, active_slot,
                rules_ruleset_id, rules_ruleset_version, rules_snapshot_id, rules_facts_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._session_values(session),
        )

    def save_session(self, session: BattleSession) -> None:
        self.connection.execute(
            """
            UPDATE battle_sessions SET
                state = ?, battle_revision = ?, metadata_revision = ?,
                current_reviewed_selection_id = ?, current_selection_advice_id = ?,
                current_applied_selection_id = ?, current_turn_id = ?,
                current_observation_id = ?, current_reviewed_board_id = ?,
                current_turn_advice_id = ?, active_slot = ?
            WHERE session_id = ?
            """,
            (
                session.state.value,
                session.battle_revision,
                session.metadata_revision,
                session.current_reviewed_selection_id,
                session.current_selection_advice_id,
                session.current_applied_selection_id,
                session.current_turn_id,
                session.current_observation_id,
                session.current_reviewed_board_id,
                session.current_turn_advice_id,
                session.active_slot,
                session.session_id,
            ),
        )

    @staticmethod
    def _session_values(session: BattleSession) -> tuple[object, ...]:
        return (
            session.session_id,
            session.match_id,
            session.generation,
            session.state.value,
            session.battle_revision,
            session.metadata_revision,
            session.current_reviewed_selection_id,
            session.current_selection_advice_id,
            session.current_applied_selection_id,
            session.current_turn_id,
            session.current_observation_id,
            session.current_reviewed_board_id,
            session.current_turn_advice_id,
            session.active_slot,
            session.rules_ruleset_id,
            session.rules_ruleset_version,
            session.rules_snapshot_id,
            session.rules_facts_sha256,
        )

    def load_active_session(self) -> BattleSession | None:
        row = self.connection.execute(
            "SELECT * FROM battle_sessions WHERE active_slot = 1"
        ).fetchone()
        return self._row_to_session(row) if row is not None else None

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> BattleSession:
        return BattleSession(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            state=BattleState(str(row["state"])),
            battle_revision=int(row["battle_revision"]),
            metadata_revision=int(row["metadata_revision"]),
            current_reviewed_selection_id=row["current_reviewed_selection_id"],
            current_selection_advice_id=row["current_selection_advice_id"],
            current_applied_selection_id=row["current_applied_selection_id"],
            current_turn_id=row["current_turn_id"],
            current_observation_id=row["current_observation_id"],
            current_reviewed_board_id=row["current_reviewed_board_id"],
            current_turn_advice_id=row["current_turn_advice_id"],
            active_slot=row["active_slot"],
            rules_ruleset_id=row["rules_ruleset_id"],
            rules_ruleset_version=row["rules_ruleset_version"],
            rules_snapshot_id=row["rules_snapshot_id"],
            rules_facts_sha256=row["rules_facts_sha256"],
        )

    def append_selection_facts(self, session_id: str, facts: SelectionFacts) -> None:
        build_json = (
            facts.self_team_build.canonical_json_bytes().decode("utf-8")
            if facts.self_team_build is not None
            else None
        )
        self.connection.execute(
            """
            INSERT INTO reviewed_selection_facts (
                reviewed_selection_id, session_id, self_team_json,
                opponent_team_json, self_team_build_json,
                self_team_build_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                facts.reviewed_selection_id,
                session_id,
                json.dumps(facts.self_team, ensure_ascii=False),
                json.dumps(facts.opponent_team, ensure_ascii=False),
                build_json,
                facts.self_team_build_sha256,
                self._now(),
            ),
        )

    def get_selection_facts(self, reviewed_selection_id: str) -> SelectionFacts:
        row = self.connection.execute(
            "SELECT * FROM reviewed_selection_facts WHERE reviewed_selection_id = ?",
            (reviewed_selection_id,),
        ).fetchone()
        if row is None:
            raise KeyError(reviewed_selection_id)
        try:
            self_team = cast(list[str], json.loads(str(row["self_team_json"])))
            opponent_team = cast(list[str], json.loads(str(row["opponent_team_json"])))
            raw_build = row["self_team_build_json"]
            raw_hash = row["self_team_build_sha256"]
            team_build: ChampionsTeamBuild | None = None
            team_build_sha256: str | None = None
            if raw_build is None:
                if raw_hash is not None:
                    raise ValueError("selection build hash without build")
            else:
                if raw_hash is None:
                    raise ValueError("selection build without hash")
                team_build = ChampionsTeamBuild.from_json_bytes(str(raw_build).encode("utf-8"))
                team_build_sha256 = str(raw_hash)
                if team_build.sha256() != team_build_sha256:
                    raise ValueError("selection build hash mismatch")
            return SelectionFacts(
                reviewed_selection_id=reviewed_selection_id,
                self_team=tuple(self_team),
                opponent_team=tuple(opponent_team),
                self_team_build=team_build,
                self_team_build_sha256=team_build_sha256,
            )
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise ValueError("stored selection facts are invalid") from exc
