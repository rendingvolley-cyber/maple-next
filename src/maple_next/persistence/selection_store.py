"""Append-only Selection advice and human APPLY persistence."""

from __future__ import annotations

import json
from typing import Any, cast

from maple_next.domain.models import AppliedSelectionSnapshot
from maple_next.persistence.base import StoreBase


class SelectionStoreMixin(StoreBase):
    def append_selection_advice(
        self,
        advice_id: str,
        session_id: str,
        job_id: str,
        selected_three: tuple[str, str, str],
        lead: str,
        backline: tuple[str, str],
        source_type: str = "MOCK",
        model: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO selection_advices (
                advice_id, session_id, job_id, selected_three_json,
                lead, backline_json, source_type, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advice_id,
                session_id,
                job_id,
                json.dumps(selected_three, ensure_ascii=False),
                lead,
                json.dumps(backline, ensure_ascii=False),
                source_type,
                model,
                self._now(),
            ),
        )

    def set_selection_advice_model(self, advice_id: str, model: str) -> None:
        """Attach the sanitized provider model after strict advice acceptance."""

        cursor = self.connection.execute(
            "UPDATE selection_advices SET model = ? WHERE advice_id = ?",
            (model, advice_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(advice_id)

    def get_selection_advice(self, advice_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM selection_advices WHERE advice_id = ?", (advice_id,)
        ).fetchone()
        if row is None:
            raise KeyError(advice_id)
        selected_three = cast(list[str], json.loads(str(row["selected_three_json"])))
        backline = cast(list[str], json.loads(str(row["backline_json"])))
        return {
            "advice_id": advice_id,
            "session_id": str(row["session_id"]),
            "job_id": str(row["job_id"]),
            "selected_three": (selected_three[0], selected_three[1], selected_three[2]),
            "lead": str(row["lead"]),
            "backline": (backline[0], backline[1]),
            "source_type": str(row["source_type"]),
            "model": str(row["model"]),
        }

    def append_applied_selection(
        self, session_id: str, snapshot: AppliedSelectionSnapshot
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO applied_selections (
                applied_selection_id, session_id, selected_three_json,
                lead, backline_json, source_advice_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.applied_selection_id,
                session_id,
                json.dumps(snapshot.selected_three, ensure_ascii=False),
                snapshot.lead,
                json.dumps(snapshot.backline, ensure_ascii=False),
                snapshot.source_advice_id,
                self._now(),
            ),
        )

    def get_applied_selection(self, applied_selection_id: str) -> AppliedSelectionSnapshot:
        row = self.connection.execute(
            "SELECT * FROM applied_selections WHERE applied_selection_id = ?",
            (applied_selection_id,),
        ).fetchone()
        if row is None:
            raise KeyError(applied_selection_id)
        selected_three = cast(list[str], json.loads(str(row["selected_three_json"])))
        backline = cast(list[str], json.loads(str(row["backline_json"])))
        return AppliedSelectionSnapshot(
            applied_selection_id=applied_selection_id,
            selected_three=(selected_three[0], selected_three[1], selected_three[2]),
            lead=str(row["lead"]),
            backline=(backline[0], backline[1]),
            source_advice_id=str(row["source_advice_id"]),
        )
