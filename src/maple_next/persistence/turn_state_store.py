"""Append-only persistence for the Bundle A turn state / delta / draft contract.

New, additive tables only (see ``schema.py``). Legacy tables (``battle_turns``,
``reviewed_turn_facts``, ``turn_advices``, ``recorded_actions``, ...) are
untouched by this module. Absence of any row in these new tables means the
legacy flow is in effect for that session/turn -- nothing here infers or
backfills legacy data.
"""

from __future__ import annotations

import json
import sqlite3

from maple_next.domain.enums import ActionOrder, ActionType
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    FixedEvidenceMetadata,
    Known,
    LegalActionPrefillDraft,
    NextTurnStateDraft,
    SideDelta,
    SideState,
    TurnIdentity,
    TurnStateIdentityError,
    TurnStateStaleError,
    field_delta_from_json,
    field_delta_to_json,
    known_from_json,
    known_to_json,
    side_delta_from_json,
    side_delta_to_json,
    side_state_from_json,
    side_state_to_json,
)
from maple_next.persistence.base import StoreBase


class TurnStateStoreMixin(StoreBase):
    # --- Fixed evidence metadata -----------------------------------------

    def append_fixed_evidence_metadata(self, metadata: FixedEvidenceMetadata) -> None:
        self.connection.execute(
            """
            INSERT INTO fixed_evidence_metadata (
                evidence_id, relative_path, sha256, recorded_at_utc, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                metadata.evidence_id,
                metadata.relative_path,
                metadata.sha256,
                metadata.recorded_at_utc,
                self._now(),
            ),
        )

    def get_fixed_evidence_metadata(self, evidence_id: str) -> FixedEvidenceMetadata:
        row = self.connection.execute(
            "SELECT * FROM fixed_evidence_metadata WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        return FixedEvidenceMetadata(
            evidence_id=str(row["evidence_id"]),
            relative_path=str(row["relative_path"]),
            sha256=str(row["sha256"]),
            recorded_at_utc=str(row["recorded_at_utc"]),
        )

    # --- Confirmed turn states (append-only) ------------------------------

    def append_confirmed_turn_state(self, state: ConfirmedTurnState) -> None:
        identity = state.identity
        self.connection.execute(
            """
            INSERT INTO confirmed_turn_states (
                confirmed_state_id, session_id, match_id, generation, turn_id,
                turn_number, battle_revision, previous_confirmed_state_id,
                self_side_json, opponent_side_json, weather_json, terrain_json,
                confirmed_by_human, confirmed_at_utc, provenance, evidence_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.confirmed_state_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                state.previous_confirmed_state_id,
                json.dumps(side_state_to_json(state.self_side), ensure_ascii=False),
                json.dumps(side_state_to_json(state.opponent_side), ensure_ascii=False),
                json.dumps(known_to_json(state.weather), ensure_ascii=False),
                json.dumps(known_to_json(state.terrain), ensure_ascii=False),
                int(state.confirmation.confirmed_by_human),
                state.confirmation.confirmed_at_utc,
                state.confirmation.provenance,
                state.evidence_id,
                self._now(),
            ),
        )

    @staticmethod
    def _confirmed_turn_state_from_row(row: sqlite3.Row) -> ConfirmedTurnState:
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        self_side: SideState = side_state_from_json(json.loads(str(row["self_side_json"])))
        opponent_side: SideState = side_state_from_json(
            json.loads(str(row["opponent_side_json"]))
        )
        weather: Known[str] = known_from_json(json.loads(str(row["weather_json"])))
        terrain: Known[str] = known_from_json(json.loads(str(row["terrain_json"])))
        confirmation = ConfirmationMeta(
            confirmed_by_human=bool(row["confirmed_by_human"]),
            confirmed_at_utc=str(row["confirmed_at_utc"]),
            provenance=str(row["provenance"]),
        )
        return ConfirmedTurnState(
            confirmed_state_id=str(row["confirmed_state_id"]),
            identity=identity,
            previous_confirmed_state_id=row["previous_confirmed_state_id"],
            self_side=self_side,
            opponent_side=opponent_side,
            weather=weather,
            terrain=terrain,
            confirmation=confirmation,
            evidence_id=row["evidence_id"],
        )

    def get_confirmed_turn_state(self, confirmed_state_id: str) -> ConfirmedTurnState:
        row = self.connection.execute(
            "SELECT * FROM confirmed_turn_states WHERE confirmed_state_id = ?",
            (confirmed_state_id,),
        ).fetchone()
        if row is None:
            raise KeyError(confirmed_state_id)
        return self._confirmed_turn_state_from_row(row)

    def get_latest_confirmed_turn_state(self, session_id: str) -> ConfirmedTurnState | None:
        row = self.connection.execute(
            """
            SELECT * FROM confirmed_turn_states
            WHERE session_id = ?
            ORDER BY turn_number DESC, battle_revision DESC, rowid DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._confirmed_turn_state_from_row(row)

    def get_latest_confirmed_turn_state_for_identity(
        self, *, session_id: str, match_id: str, generation: int
    ) -> ConfirmedTurnState | None:
        """Latest confirmed state scoped to the exact session/match/generation."""

        row = self.connection.execute(
            """
            SELECT * FROM confirmed_turn_states
            WHERE session_id = ? AND match_id = ? AND generation = ?
            ORDER BY turn_number DESC, battle_revision DESC, rowid DESC
            LIMIT 1
            """,
            (session_id, match_id, generation),
        ).fetchone()
        if row is None:
            return None
        return self._confirmed_turn_state_from_row(row)

    def list_confirmed_turn_states_for_match(
        self, *, session_id: str, match_id: str, generation: int
    ) -> tuple[ConfirmedTurnState, ...]:
        """All confirmed states for the exact match, oldest first, deterministic order."""

        rows = self.connection.execute(
            """
            SELECT * FROM confirmed_turn_states
            WHERE session_id = ? AND match_id = ? AND generation = ?
            ORDER BY turn_number ASC, battle_revision ASC, rowid ASC
            """,
            (session_id, match_id, generation),
        ).fetchall()
        return tuple(self._confirmed_turn_state_from_row(row) for row in rows)

    def match_uses_rich_state_contract(
        self, *, session_id: str, match_id: str, generation: int
    ) -> bool:
        """Whether this exact match has ever recorded a Bundle A confirmed turn state."""

        row = self.connection.execute(
            """
            SELECT 1 FROM confirmed_turn_states
            WHERE session_id = ? AND match_id = ? AND generation = ?
            LIMIT 1
            """,
            (session_id, match_id, generation),
        ).fetchone()
        return row is not None

    # --- Action result deltas (append-only) --------------------------------

    def append_action_result_delta(self, delta: ActionResultDelta) -> None:
        identity = delta.identity
        self.connection.execute(
            """
            INSERT INTO action_result_deltas (
                delta_id, session_id, match_id, generation, turn_id, turn_number,
                battle_revision, based_on_confirmed_state_id, self_side_json,
                opponent_side_json, weather_json, terrain_json, confirmed_by_human,
                confirmed_at_utc, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delta.delta_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                delta.based_on_confirmed_state_id,
                json.dumps(side_delta_to_json(delta.self_side), ensure_ascii=False),
                json.dumps(side_delta_to_json(delta.opponent_side), ensure_ascii=False),
                json.dumps(field_delta_to_json(delta.weather), ensure_ascii=False),
                json.dumps(field_delta_to_json(delta.terrain), ensure_ascii=False),
                int(delta.confirmation.confirmed_by_human),
                delta.confirmation.confirmed_at_utc,
                delta.confirmation.provenance,
                self._now(),
            ),
        )

    @staticmethod
    def _action_result_delta_from_row(row: sqlite3.Row) -> ActionResultDelta:
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        self_side: SideDelta = side_delta_from_json(json.loads(str(row["self_side_json"])))
        opponent_side: SideDelta = side_delta_from_json(
            json.loads(str(row["opponent_side_json"]))
        )
        weather: FieldDelta[str] = field_delta_from_json(json.loads(str(row["weather_json"])))
        terrain: FieldDelta[str] = field_delta_from_json(json.loads(str(row["terrain_json"])))
        confirmation = ConfirmationMeta(
            confirmed_by_human=bool(row["confirmed_by_human"]),
            confirmed_at_utc=str(row["confirmed_at_utc"]),
            provenance=str(row["provenance"]),
        )
        return ActionResultDelta(
            delta_id=str(row["delta_id"]),
            identity=identity,
            based_on_confirmed_state_id=str(row["based_on_confirmed_state_id"]),
            self_side=self_side,
            opponent_side=opponent_side,
            weather=weather,
            terrain=terrain,
            confirmation=confirmation,
        )

    def get_action_result_delta(self, delta_id: str) -> ActionResultDelta:
        row = self.connection.execute(
            "SELECT * FROM action_result_deltas WHERE delta_id = ?",
            (delta_id,),
        ).fetchone()
        if row is None:
            raise KeyError(delta_id)
        return self._action_result_delta_from_row(row)

    def list_action_result_deltas_for_match(
        self, *, session_id: str, match_id: str, generation: int
    ) -> tuple[ActionResultDelta, ...]:
        """All deltas for the exact match, oldest first, deterministic order."""

        rows = self.connection.execute(
            """
            SELECT * FROM action_result_deltas
            WHERE session_id = ? AND match_id = ? AND generation = ?
            ORDER BY turn_number ASC, battle_revision ASC, rowid ASC
            """,
            (session_id, match_id, generation),
        ).fetchall()
        return tuple(self._action_result_delta_from_row(row) for row in rows)

    # --- Next-turn drafts (upsert scoped to session/turn/revision) --------

    def upsert_next_turn_state_draft(self, draft: NextTurnStateDraft) -> None:
        identity = draft.identity
        self.connection.execute(
            """
            INSERT INTO next_turn_state_drafts (
                draft_id, session_id, match_id, generation, turn_id, turn_number,
                battle_revision, based_on_confirmed_state_id, source_delta_id,
                self_side_json, opponent_side_json, weather_json, terrain_json,
                derived_at_utc, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, turn_id, battle_revision) DO UPDATE SET
                draft_id = excluded.draft_id,
                based_on_confirmed_state_id = excluded.based_on_confirmed_state_id,
                source_delta_id = excluded.source_delta_id,
                self_side_json = excluded.self_side_json,
                opponent_side_json = excluded.opponent_side_json,
                weather_json = excluded.weather_json,
                terrain_json = excluded.terrain_json,
                derived_at_utc = excluded.derived_at_utc,
                created_at = excluded.created_at
            """,
            (
                draft.draft_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                draft.based_on_confirmed_state_id,
                draft.source_delta_id,
                json.dumps(side_state_to_json(draft.self_side), ensure_ascii=False),
                json.dumps(side_state_to_json(draft.opponent_side), ensure_ascii=False),
                json.dumps(known_to_json(draft.weather), ensure_ascii=False),
                json.dumps(known_to_json(draft.terrain), ensure_ascii=False),
                draft.derived_at_utc,
                self._now(),
            ),
        )

    @staticmethod
    def _next_turn_state_draft_from_row(row: sqlite3.Row) -> NextTurnStateDraft:
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        self_side: SideState = side_state_from_json(json.loads(str(row["self_side_json"])))
        opponent_side: SideState = side_state_from_json(
            json.loads(str(row["opponent_side_json"]))
        )
        weather: Known[str] = known_from_json(json.loads(str(row["weather_json"])))
        terrain: Known[str] = known_from_json(json.loads(str(row["terrain_json"])))
        return NextTurnStateDraft(
            draft_id=str(row["draft_id"]),
            identity=identity,
            based_on_confirmed_state_id=str(row["based_on_confirmed_state_id"]),
            source_delta_id=str(row["source_delta_id"]),
            self_side=self_side,
            opponent_side=opponent_side,
            weather=weather,
            terrain=terrain,
            derived_at_utc=str(row["derived_at_utc"]),
        )

    def get_next_turn_state_draft(self, draft_id: str) -> NextTurnStateDraft:
        row = self.connection.execute(
            "SELECT * FROM next_turn_state_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return self._next_turn_state_draft_from_row(row)

    def get_latest_next_turn_state_draft(self, session_id: str) -> NextTurnStateDraft | None:
        row = self.connection.execute(
            """
            SELECT * FROM next_turn_state_drafts
            WHERE session_id = ?
            ORDER BY turn_number DESC, battle_revision DESC, rowid DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._next_turn_state_draft_from_row(row)

    def get_latest_next_turn_state_draft_for_identity(
        self, *, session_id: str, match_id: str, generation: int
    ) -> NextTurnStateDraft | None:
        """Latest unresolved draft scoped to the exact session/match/generation."""

        row = self.connection.execute(
            """
            SELECT * FROM next_turn_state_drafts
            WHERE session_id = ? AND match_id = ? AND generation = ?
            ORDER BY turn_number DESC, battle_revision DESC, rowid DESC
            LIMIT 1
            """,
            (session_id, match_id, generation),
        ).fetchone()
        if row is None:
            return None
        return self._next_turn_state_draft_from_row(row)

    def list_next_turn_state_drafts_for_match(
        self, *, session_id: str, match_id: str, generation: int
    ) -> tuple[NextTurnStateDraft, ...]:
        """All drafts for the exact match, oldest first.

        Used to detect a foreign or corrupt draft.
        """

        rows = self.connection.execute(
            """
            SELECT * FROM next_turn_state_drafts
            WHERE session_id = ? AND match_id = ? AND generation = ?
            ORDER BY turn_number ASC, battle_revision ASC, rowid ASC
            """,
            (session_id, match_id, generation),
        ).fetchall()
        return tuple(self._next_turn_state_draft_from_row(row) for row in rows)

    # --- Legal action prefill drafts ---------------------------------------

    def append_legal_action_prefill_draft(self, prefill: LegalActionPrefillDraft) -> None:
        identity = prefill.identity
        self.connection.execute(
            """
            INSERT INTO legal_action_prefill_drafts (
                prefill_id, session_id, match_id, generation, turn_id, turn_number,
                battle_revision, based_on_confirmed_state_id, action_type,
                action_name, confidence, derived_at_utc, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prefill.prefill_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                prefill.based_on_confirmed_state_id,
                prefill.action_type.value,
                prefill.action_name,
                prefill.confidence,
                prefill.derived_at_utc,
                self._now(),
            ),
        )

    def get_legal_action_prefill_draft(self, prefill_id: str) -> LegalActionPrefillDraft:
        row = self.connection.execute(
            "SELECT * FROM legal_action_prefill_drafts WHERE prefill_id = ?",
            (prefill_id,),
        ).fetchone()
        if row is None:
            raise KeyError(prefill_id)
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        return LegalActionPrefillDraft(
            prefill_id=str(row["prefill_id"]),
            identity=identity,
            based_on_confirmed_state_id=str(row["based_on_confirmed_state_id"]),
            action_type=ActionType(str(row["action_type"])),
            action_name=str(row["action_name"]),
            derived_at_utc=str(row["derived_at_utc"]),
            confidence=row["confidence"],
        )

    # --- Confirmed legal action selections (append-only) -------------------

    def append_confirmed_legal_action_selection(
        self, selection: ConfirmedLegalActionSelection
    ) -> None:
        identity = selection.identity
        self.connection.execute(
            """
            INSERT INTO confirmed_legal_action_selections (
                confirmation_id, session_id, match_id, generation, turn_id,
                turn_number, battle_revision, action_type, action_name,
                confirmed_by_human, confirmed_at_utc, provenance,
                source_prefill_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                selection.confirmation_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                selection.action_type.value,
                selection.action_name,
                int(selection.confirmation.confirmed_by_human),
                selection.confirmation.confirmed_at_utc,
                selection.confirmation.provenance,
                selection.source_prefill_id,
                self._now(),
            ),
        )

    @staticmethod
    def _confirmed_legal_action_selection_from_row(
        row: sqlite3.Row,
    ) -> ConfirmedLegalActionSelection:
        identity = TurnIdentity(
            session_id=str(row["session_id"]),
            match_id=str(row["match_id"]),
            generation=int(row["generation"]),
            turn_id=str(row["turn_id"]),
            turn_number=int(row["turn_number"]),
            battle_revision=int(row["battle_revision"]),
        )
        confirmation = ConfirmationMeta(
            confirmed_by_human=bool(row["confirmed_by_human"]),
            confirmed_at_utc=str(row["confirmed_at_utc"]),
            provenance=str(row["provenance"]),
        )
        return ConfirmedLegalActionSelection(
            confirmation_id=str(row["confirmation_id"]),
            identity=identity,
            action_type=ActionType(str(row["action_type"])),
            action_name=str(row["action_name"]),
            confirmation=confirmation,
            source_prefill_id=row["source_prefill_id"],
        )

    def get_confirmed_legal_action_selection(
        self, confirmation_id: str
    ) -> ConfirmedLegalActionSelection:
        row = self.connection.execute(
            "SELECT * FROM confirmed_legal_action_selections WHERE confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(confirmation_id)
        return self._confirmed_legal_action_selection_from_row(row)

    def list_confirmed_legal_action_selections_for_identity(
        self, identity: TurnIdentity
    ) -> tuple[ConfirmedLegalActionSelection, ...]:
        """Every confirmed legal action for the exact turn identity, deterministic order.

        Ordered by ``confirmation_id`` (not SQLite ``rowid``) so the result
        is independent of insertion/row order.
        """

        rows = self.connection.execute(
            """
            SELECT * FROM confirmed_legal_action_selections
            WHERE session_id = ? AND match_id = ? AND generation = ?
              AND turn_id = ? AND turn_number = ? AND battle_revision = ?
            ORDER BY confirmation_id ASC
            """,
            (
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
            ),
        ).fetchall()
        return tuple(self._confirmed_legal_action_selection_from_row(row) for row in rows)

    # --- Rich action completion row write (private helper) -----------------
    #
    # Transaction ownership lives in ``SQLiteRepository.record_rich_action_
    # completion`` (see ``persistence/sqlite.py``); this mixin method only
    # validates binding and writes rows. It must never be called outside an
    # already-open transaction.

    def _record_rich_action_completion_row(
        self,
        *,
        transaction_id: str,
        identity: TurnIdentity,
        own_action_type: ActionType,
        own_action_name: str,
        opponent_action_type: ActionType,
        opponent_action_name: str,
        action_order: ActionOrder,
        delta: ActionResultDelta,
    ) -> None:
        """Validate full identity binding, then write the delta + completion rows.

        Binding requires ``identity`` to exactly match both ``delta.identity``
        and the identity of the ``ConfirmedTurnState`` referenced by
        ``delta.based_on_confirmed_state_id`` (session_id, match_id,
        generation, turn_id, turn_number, battle_revision). Any mismatch, or
        a missing based-on confirmed state, fails closed before any row is
        written.
        """

        if delta.identity != identity:
            raise TurnStateIdentityError("ACTION_COMPLETION_DELTA_IDENTITY_MISMATCH")
        try:
            based_on_state = self.get_confirmed_turn_state(delta.based_on_confirmed_state_id)
        except KeyError as exc:
            raise TurnStateStaleError("BASED_ON_CONFIRMED_STATE_NOT_FOUND") from exc
        if based_on_state.identity != identity:
            raise TurnStateIdentityError("ACTION_COMPLETION_CONFIRMED_STATE_IDENTITY_MISMATCH")

        self.append_action_result_delta(delta)
        self.connection.execute(
            """
            INSERT INTO rich_action_completions (
                transaction_id, session_id, match_id, generation, turn_id,
                turn_number, battle_revision, based_on_confirmed_state_id,
                own_action_type, own_action_name, opponent_action_type,
                opponent_action_name, action_order, delta_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                identity.session_id,
                identity.match_id,
                identity.generation,
                identity.turn_id,
                identity.turn_number,
                identity.battle_revision,
                delta.based_on_confirmed_state_id,
                own_action_type.value,
                own_action_name,
                opponent_action_type.value,
                opponent_action_name,
                action_order.value,
                delta.delta_id,
                self._now(),
            ),
        )

    def get_rich_action_completion_by_turn(self, turn_id: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM rich_action_completions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "transaction_id": str(row["transaction_id"]),
            "session_id": str(row["session_id"]),
            "match_id": row["match_id"],
            "generation": row["generation"],
            "turn_id": str(row["turn_id"]),
            "turn_number": int(row["turn_number"]),
            "battle_revision": row["battle_revision"],
            "based_on_confirmed_state_id": row["based_on_confirmed_state_id"],
            "own_action_type": ActionType(str(row["own_action_type"])),
            "own_action_name": str(row["own_action_name"]),
            "opponent_action_type": ActionType(str(row["opponent_action_type"])),
            "opponent_action_name": str(row["opponent_action_name"]),
            "action_order": ActionOrder(str(row["action_order"])),
            "delta_id": str(row["delta_id"]),
        }
