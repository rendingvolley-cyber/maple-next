"""Append-only Turn facts, mock advice, and human action persistence."""

from __future__ import annotations

import json
from typing import cast

from maple_next.domain.enums import ActionOrder, ActionType, HpBucket
from maple_next.domain.models import (
    BattleTurn,
    RecordedAction,
    TurnAdviceSnapshot,
    TurnFactsSnapshot,
)
from maple_next.persistence.base import StoreBase


class TurnStoreMixin(StoreBase):
    def append_turn(self, session_id: str, turn: BattleTurn) -> None:
        self.connection.execute(
            """
            INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (turn.turn_id, session_id, turn.turn_number, self._now()),
        )

    def get_turn(self, turn_id: str) -> BattleTurn:
        row = self.connection.execute(
            "SELECT turn_id, turn_number FROM battle_turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return BattleTurn(turn_id=str(row["turn_id"]), turn_number=int(row["turn_number"]))

    def append_turn_facts(self, session_id: str, snapshot: TurnFactsSnapshot) -> None:
        self.connection.execute(
            """
            INSERT INTO reviewed_turn_facts (
                turn_facts_id, session_id, turn_id, turn_number, self_active,
                opponent_active, self_hp, opponent_hp, legal_moves_json,
                legal_switches_json, human_note, previous_snapshot_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.turn_facts_id,
                session_id,
                snapshot.turn_id,
                snapshot.turn_number,
                snapshot.self_active,
                snapshot.opponent_active,
                snapshot.self_hp.value,
                snapshot.opponent_hp.value,
                json.dumps(snapshot.legal_moves, ensure_ascii=False),
                json.dumps(snapshot.legal_switches, ensure_ascii=False),
                snapshot.human_note,
                snapshot.previous_snapshot_id,
                self._now(),
            ),
        )

    def get_turn_facts(self, turn_facts_id: str) -> TurnFactsSnapshot:
        row = self.connection.execute(
            "SELECT * FROM reviewed_turn_facts WHERE turn_facts_id = ?",
            (turn_facts_id,),
        ).fetchone()
        if row is None:
            raise KeyError(turn_facts_id)
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

    def append_turn_advice(self, session_id: str, advice: TurnAdviceSnapshot) -> None:
        self.connection.execute(
            """
            INSERT INTO turn_advices (
                turn_advice_id, session_id, turn_id, turn_number, job_id,
                input_snapshot_id, action_type, action_name, opponent_prediction,
                rationale, is_mock, source_type, model, warnings_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advice.turn_advice_id,
                session_id,
                advice.turn_id,
                advice.turn_number,
                advice.job_id,
                advice.input_snapshot_id,
                advice.action_type.value,
                advice.action_name,
                advice.opponent_prediction,
                advice.rationale,
                int(advice.is_mock),
                advice.source_type,
                advice.model,
                json.dumps(list(advice.warnings), ensure_ascii=False),
                self._now(),
            ),
        )

    def get_turn_advice(self, turn_advice_id: str) -> TurnAdviceSnapshot:
        row = self.connection.execute(
            "SELECT * FROM turn_advices WHERE turn_advice_id = ?",
            (turn_advice_id,),
        ).fetchone()
        if row is None:
            raise KeyError(turn_advice_id)
        warnings = cast(list[str], json.loads(str(row["warnings_json"])))
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
            source_type=str(row["source_type"]),
            model=str(row["model"]),
            warnings=tuple(warnings),
        )

    def append_recorded_action(self, session_id: str, action: RecordedAction) -> None:
        self.connection.execute(
            """
            INSERT INTO recorded_actions (
                action_id, session_id, turn_id, turn_number, action_type,
                action_name, opponent_action_type, opponent_action_name,
                action_order, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.action_id,
                session_id,
                action.turn_id,
                action.turn_number,
                action.action_type.value,
                action.action_name,
                (
                    action.opponent_action_type.value
                    if action.opponent_action_type is not None
                    else None
                ),
                action.opponent_action_name,
                action.action_order.value,
                self._now(),
            ),
        )

    def has_recorded_action(self, turn_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM recorded_actions WHERE turn_id = ? LIMIT 1",
            (turn_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _recorded_action_from_row(row: object) -> RecordedAction:
        opponent_action_type_raw = row["opponent_action_type"]  # type: ignore[index]
        return RecordedAction(
            action_id=str(row["action_id"]),  # type: ignore[index]
            turn_id=str(row["turn_id"]),  # type: ignore[index]
            turn_number=int(row["turn_number"]),  # type: ignore[index]
            action_type=ActionType(str(row["action_type"])),  # type: ignore[index]
            action_name=str(row["action_name"]),  # type: ignore[index]
            opponent_action_type=(
                ActionType(str(opponent_action_type_raw))
                if opponent_action_type_raw is not None
                else None
            ),
            opponent_action_name=str(row["opponent_action_name"]),  # type: ignore[index]
            action_order=ActionOrder(str(row["action_order"])),  # type: ignore[index]
        )

    def list_recorded_actions(self, session_id: str) -> tuple[RecordedAction, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM recorded_actions
            WHERE session_id = ? ORDER BY turn_number, rowid
            """,
            (session_id,),
        ).fetchall()
        return tuple(self._recorded_action_from_row(row) for row in rows)
