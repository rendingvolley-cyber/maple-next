"""Persistence for immutable match outcomes, exports, and export views."""

from __future__ import annotations

import json
from typing import cast

from maple_next.domain.enums import ActionType, HpBucket, MatchOutcome
from maple_next.domain.match_models import MatchExportRecord, MatchOutcomeRecord
from maple_next.domain.models import BattleTurn, RecordedAction, TurnAdviceSnapshot, TurnFactsSnapshot
from maple_next.persistence.base import StoreBase


class MatchStoreMixin(StoreBase):
    def append_match_outcome(self, record: MatchOutcomeRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO match_outcomes (
                session_id, match_id, generation, outcome, ended_at_utc,
                final_battle_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.match_id,
                record.generation,
                record.outcome.value,
                record.ended_at_utc,
                record.final_battle_revision,
                self._now(),
            ),
        )

    def get_match_outcome(self, session_id: str) -> MatchOutcomeRecord | None:
        row = self.connection.execute(
            "SELECT * FROM match_outcomes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return MatchOutcomeRecord(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            outcome=MatchOutcome(str(row["outcome"])),
            ended_at_utc=str(row["ended_at_utc"]),
            final_battle_revision=int(row["final_battle_revision"]),
        )

    def append_match_export(self, record: MatchExportRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO match_exports (
                session_id, match_id, schema_version, export_path, sha256,
                exported_at_utc, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.match_id,
                record.schema_version,
                record.export_path,
                record.sha256,
                record.exported_at_utc,
                self._now(),
            ),
        )

    def get_match_export(self, session_id: str) -> MatchExportRecord | None:
        row = self.connection.execute(
            "SELECT * FROM match_exports WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return MatchExportRecord(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            schema_version=str(row["schema_version"]),
            export_path=str(row["export_path"]),
            sha256=str(row["sha256"]),
            exported_at_utc=str(row["exported_at_utc"]),
        )

    def count_turns(self, session_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM battle_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def count_actions(self, session_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM recorded_actions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def list_turns(self, session_id: str) -> tuple[BattleTurn, ...]:
        rows = self.connection.execute(
            """
            SELECT turn_id, turn_number FROM battle_turns
            WHERE session_id = ? ORDER BY turn_number
            """,
            (session_id,),
        ).fetchall()
        return tuple(
            BattleTurn(turn_id=str(row["turn_id"]), turn_number=int(row["turn_number"]))
            for row in rows
        )

    def get_latest_turn_facts(self, turn_id: str) -> TurnFactsSnapshot | None:
        row = self.connection.execute(
            """
            SELECT * FROM reviewed_turn_facts
            WHERE turn_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        legal_moves = cast(list[str], json.loads(str(row["legal_moves_json"])))
        legal_switches = cast(list[str], json.loads(str(row["legal_switches_json"])))
        return TurnFactsSnapshot(
            turn_facts_id=str(row["turn_facts_id"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            self_active=str(row["self_active"]),
            opponent_active=str(row["opponent_active"]),
            self_hp=HpBucket(str(row["self_hp"])),
            opponent_hp=HpBucket(str(row["opponent_hp"])),
            legal_moves=tuple(legal_moves),
            legal_switches=tuple(legal_switches),
            human_note=str(row["human_note"]),
            previous_snapshot_id=row["previous_snapshot_id"],
        )

    def get_latest_turn_advice(self, turn_id: str) -> TurnAdviceSnapshot | None:
        row = self.connection.execute(
            """
            SELECT * FROM turn_advices
            WHERE turn_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return TurnAdviceSnapshot(
            turn_advice_id=str(row["turn_advice_id"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            job_id=str(row["job_id"]),
            input_snapshot_id=str(row["input_snapshot_id"]),
            action_type=ActionType(str(row["action_type"])),
            action_name=str(row["action_name"]),
            opponent_prediction=str(row["opponent_prediction"]),
            rationale=str(row["rationale"]),
            is_mock=bool(row["is_mock"]),
        )

    def get_recorded_action_for_turn(self, turn_id: str) -> RecordedAction | None:
        row = self.connection.execute(
            "SELECT * FROM recorded_actions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return RecordedAction(
            action_id=str(row["action_id"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            action_type=ActionType(str(row["action_type"])),
            action_name=str(row["action_name"]),
        )
