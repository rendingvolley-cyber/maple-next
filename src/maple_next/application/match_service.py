"""Explicit match completion, canonical JSON export, and next-match commands."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.application.projection import DomainProjection
from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import BattleState, MatchOutcome
from maple_next.domain.match_models import MatchExportRecord, MatchOutcomeRecord
from maple_next.domain.models import BattleSession
from maple_next.persistence.sqlite import SQLiteRepository

MATCH_EXPORT_SCHEMA_VERSION = "maple-match.v1"


class MatchApplication(BattleApplication):
    """Adds human-confirmed terminal commands without changing Turn semantics."""

    def __init__(self, repository: SQLiteRepository, export_directory: str | Path) -> None:
        super().__init__(repository)
        self.export_directory = Path(export_directory)

    def projection(self) -> DomainProjection:
        projection = super().projection()
        session = self.repository.load_active_session()
        if session is None:
            return projection
        if session.state is BattleState.MATCH_ENDED:
            return replace(
                projection,
                primary_cta="SAVE_MATCH_JSON",
                primary_cta_enabled=True,
                secondary_actions=(),
                message="MATCH_ENDED",
                provider_send_enabled=False,
            )
        if session.state is BattleState.MATCH_EXPORTED:
            return replace(
                projection,
                primary_cta="NEW_MATCH",
                primary_cta_enabled=True,
                secondary_actions=(),
                message="MATCH_EXPORTED",
                provider_send_enabled=False,
            )
        if session.state in {BattleState.BATTLE_READY, BattleState.TURN_RECORDED}:
            return replace(
                projection,
                secondary_actions=(*projection.secondary_actions, "END_MATCH"),
            )
        return projection

    def end_match(
        self,
        outcome: MatchOutcome,
        *,
        human_confirmed: bool,
    ) -> MatchOutcomeRecord:
        if not human_confirmed:
            raise DomainError("HUMAN_MATCH_OUTCOME_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_active_session()
            existing = self.repository.get_match_outcome(session.session_id)
            if existing is not None:
                raise DomainError("MATCH_OUTCOME_ALREADY_SET")
            if session.state not in {BattleState.BATTLE_READY, BattleState.TURN_RECORDED}:
                raise DomainError("MATCH_END_NOT_ALLOWED_IN_CURRENT_STATE")

            final_revision = session.battle_revision + 1
            record = MatchOutcomeRecord(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                outcome=outcome,
                ended_at_utc=datetime.now(UTC).isoformat(),
                final_battle_revision=final_revision,
            )
            self.repository.append_match_outcome(record)
            session.state = BattleState.MATCH_ENDED
            session.bump_battle()
            self.repository.save_session(session)
        return record

    def export_match(self) -> MatchExportRecord:
        session = self._require_active_session()
        if session.state is BattleState.MATCH_EXPORTED:
            existing = self.repository.get_match_export(session.session_id)
            if existing is None:
                raise DomainError("MATCH_EXPORT_RECORD_MISSING")
            self._verify_existing_export(existing)
            return existing
        if session.state is not BattleState.MATCH_ENDED:
            raise DomainError("EXPECTED_MATCH_ENDED")

        outcome = self.repository.get_match_outcome(session.session_id)
        if outcome is None:
            raise DomainError("MATCH_OUTCOME_REQUIRED")
        payload = self._build_export_payload(session, outcome)
        encoded = self._encode_payload(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        export_path = self.export_directory / f"maple-match-{session.match_id}.json"

        existing_record = self.repository.get_match_export(session.session_id)
        if existing_record is not None:
            self._verify_existing_export(existing_record, expected_bytes=encoded)
            return existing_record

        try:
            self.export_directory.mkdir(parents=True, exist_ok=True)
            if export_path.exists():
                if export_path.read_bytes() != encoded:
                    raise DomainError("EXPORT_FILE_CONTENT_MISMATCH")
            else:
                self._atomic_write(export_path, encoded)
        except DomainError:
            raise
        except OSError as exc:
            raise DomainError("EXPORT_WRITE_FAILED") from exc

        record = MatchExportRecord(
            session_id=session.session_id,
            match_id=session.match_id,
            schema_version=MATCH_EXPORT_SCHEMA_VERSION,
            export_path=str(export_path),
            sha256=digest,
            exported_at_utc=datetime.now(UTC).isoformat(),
        )
        with self.repository.transaction():
            current = self._require_session(BattleState.MATCH_ENDED)
            if current.session_id != session.session_id:
                raise DomainError("MATCH_CHANGED_DURING_EXPORT")
            existing_record = self.repository.get_match_export(current.session_id)
            if existing_record is not None:
                self._verify_existing_export(existing_record, expected_bytes=encoded)
                return existing_record
            self.repository.append_match_export(record)
            current.state = BattleState.MATCH_EXPORTED
            current.bump_battle()
            self.repository.save_session(current)
        return record

    def new_match_after_export(self) -> BattleSession:
        with self.repository.transaction():
            previous = self._require_session(BattleState.MATCH_EXPORTED)
            if self.repository.get_match_export(previous.session_id) is None:
                raise DomainError("MATCH_EXPORT_RECORD_MISSING")
            previous.active_slot = None
            self.repository.save_session(previous)
            session = BattleSession(
                session_id=str(uuid4()),
                match_id=str(uuid4()),
                generation=self.repository.next_generation(),
                state=BattleState.SELECTION_OPEN,
                battle_revision=1,
            )
            self.repository.insert_session(session)
        return session

    def _build_export_payload(
        self,
        session: BattleSession,
        outcome: MatchOutcomeRecord,
    ) -> dict[str, Any]:
        if session.current_reviewed_selection_id is None:
            raise DomainError("REVIEWED_SELECTION_UNAVAILABLE")
        if session.current_applied_selection_id is None:
            raise DomainError("APPLIED_SELECTION_REQUIRED")
        selection_facts = self.repository.get_selection_facts(
            session.current_reviewed_selection_id
        )
        applied = self.repository.get_applied_selection(
            session.current_applied_selection_id
        )

        turns: list[dict[str, Any]] = []
        for turn in self.repository.list_turns(session.session_id):
            facts = self.repository.get_latest_turn_facts(turn.turn_id)
            advice = self.repository.get_latest_turn_advice(turn.turn_id)
            actual = self.repository.get_recorded_action_for_turn(turn.turn_id)
            if facts is None:
                raise DomainError("CANONICAL_TURN_FACTS_MISSING")
            if actual is None:
                raise DomainError("CANONICAL_ACTUAL_ACTION_MISSING")
            turns.append(
                {
                    "turn_number": turn.turn_number,
                    "reviewed_facts": {
                        "self_active": facts.self_active,
                        "opponent_active": facts.opponent_active,
                        "self_hp": facts.self_hp.value,
                        "opponent_hp": facts.opponent_hp.value,
                        "legal_moves": list(facts.legal_moves),
                        "legal_switches": list(facts.legal_switches),
                        "human_note": facts.human_note,
                    },
                    "advice": (
                        {
                            "source_type": "MOCK" if advice.is_mock else "UNKNOWN",
                            "recommended_action_type": advice.action_type.value,
                            "recommended_action_name": advice.action_name,
                            "opponent_prediction": advice.opponent_prediction,
                            "rationale": advice.rationale,
                        }
                        if advice is not None
                        else None
                    ),
                    "actual_action": {
                        "action_type": actual.action_type.value,
                        "action_name": actual.action_name,
                    },
                }
            )

        action_history = [
            {
                "turn_number": action.turn_number,
                "action_type": action.action_type.value,
                "action_name": action.action_name,
            }
            for action in self.repository.list_recorded_actions(session.session_id)
        ]
        return {
            "schema_version": MATCH_EXPORT_SCHEMA_VERSION,
            "match_id": session.match_id,
            "session_id": session.session_id,
            "generation": session.generation,
            "outcome": outcome.outcome.value,
            "ended_at_utc": outcome.ended_at_utc,
            "final_battle_revision": outcome.final_battle_revision,
            "selection": {
                "self_team": list(selection_facts.self_team),
                "opponent_team": list(selection_facts.opponent_team),
                "selected_three": list(applied.selected_three),
                "lead": applied.lead,
            },
            "turns": turns,
            "action_history": action_history,
        }

    @staticmethod
    def _encode_payload(payload: dict[str, Any]) -> bytes:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _verify_existing_export(
        self,
        record: MatchExportRecord,
        *,
        expected_bytes: bytes | None = None,
    ) -> None:
        path = Path(record.export_path)
        expected_path = self.export_directory / f"maple-match-{record.match_id}.json"
        if path != expected_path:
            raise DomainError("EXPORT_PATH_MISMATCH")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise DomainError("EXPORT_FILE_UNREADABLE") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != record.sha256:
            raise DomainError("EXPORT_HASH_MISMATCH")
        if expected_bytes is not None and content != expected_bytes:
            raise DomainError("EXPORT_FILE_CONTENT_MISMATCH")
