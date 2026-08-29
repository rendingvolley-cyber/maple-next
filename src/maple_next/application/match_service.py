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

from maple_next.application.match_export_v3 import (
    MATCH_EXPORT_SCHEMA_VERSION_V4,
    ConfirmedTurnRecord,
    MatchExportV3Error,
    build_integrated_match_export_v3_payload,
    parse_match_export_v3,
    parse_match_export_v4,
    validate_confirmed_states_for_export,
    validate_delta_chain_for_export,
    validate_evidence_hash_shape,
    validate_legal_actions_for_export,
)
from maple_next.application.projection import DomainProjection
from maple_next.application.service import (
    BattleApplication,
    DomainError,
    _resolve_new_match_opponent_intel_pin,
    _resolve_new_match_rules_pin,
    load_structured_turn_advice_v2,
)
from maple_next.domain.enums import BattleState, MatchOutcome
from maple_next.domain.match_models import MatchExportRecord, MatchOutcomeRecord
from maple_next.domain.mega_evolution import mega_state_to_canonical_dict
from maple_next.domain.models import BattleSession
from maple_next.opponent_intel_db.generation_store import GenerationStoreError
from maple_next.opponent_intel_db.runtime_intel import load_pinned_generation
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V2,
    turn_advice_body_v2_to_canonical_dict,
)

MATCH_EXPORT_SCHEMA_VERSION = "maple-match.v2"
MATCH_EXPORT_SCHEMA_VERSION_V1 = "maple-match.v1"
MATCH_EXPORT_SCHEMA_VERSION_V2 = MATCH_EXPORT_SCHEMA_VERSION
MATCH_EXPORT_SCHEMA_VERSION_DETAILED = MATCH_EXPORT_SCHEMA_VERSION_V2


class MatchApplication(BattleApplication):
    """Adds human-confirmed terminal commands without changing Turn semantics."""

    def __init__(
        self,
        repository: SQLiteRepository,
        export_directory: str | Path,
        *,
        repository_root: str | Path | None = None,
        opponent_intel_directory: str | Path | None = None,
    ) -> None:
        super().__init__(repository, opponent_intel_directory=opponent_intel_directory)
        self.export_directory = Path(export_directory).expanduser().resolve()
        self.repository_root = (
            Path(repository_root).expanduser().resolve()
            if repository_root is not None
            else Path(__file__).resolve().parents[3]
        )

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
            self._require_export_directory_outside_repository()
            self._verify_existing_export(existing)
            return existing
        if session.state is not BattleState.MATCH_ENDED:
            raise DomainError("EXPECTED_MATCH_ENDED")

        self._require_export_directory_outside_repository()
        outcome = self.repository.get_match_outcome(session.session_id)
        if outcome is None:
            raise DomainError("MATCH_OUTCOME_REQUIRED")
        legacy_payload = self._build_export_payload(session, outcome)

        uses_rich_state = self.repository.match_uses_rich_state_contract(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
        )
        if uses_rich_state:
            payload = self._build_export_payload_v3(session, outcome, legacy_payload)
            encoded = self._encode_payload(payload)
            # Gemini V2 Bundle 6: ``_build_export_payload_v3`` only ever sets
            # ``schema_version`` to ``.v4`` when at least one turn actually
            # carries a v2 ``structured_response`` -- every other rich-state
            # match is still ``.v3`` and keeps using the unchanged v3 parser.
            parse_rich_export = (
                parse_match_export_v4
                if payload["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V4
                else parse_match_export_v3
            )
            try:
                parse_rich_export(encoded)
            except MatchExportV3Error as exc:
                raise DomainError(f"V3_EXPORT_PRE_WRITE_PARSE_FAILED:{exc}") from exc
        else:
            payload = legacy_payload
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

        if uses_rich_state:
            try:
                read_back = export_path.read_bytes()
                parse_rich_export(read_back)
            except OSError as exc:
                raise DomainError("V3_EXPORT_READ_BACK_FAILED") from exc
            except MatchExportV3Error as exc:
                raise DomainError(f"V3_EXPORT_READ_BACK_PARSE_FAILED:{exc}") from exc
            if hashlib.sha256(read_back).hexdigest() != digest:
                raise DomainError("V3_EXPORT_READ_BACK_HASH_MISMATCH")

        record = MatchExportRecord(
            session_id=session.session_id,
            match_id=session.match_id,
            schema_version=str(payload["schema_version"]),
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
            rules_pin = _resolve_new_match_rules_pin()
            intel_pin = _resolve_new_match_opponent_intel_pin(self.opponent_intel_directory)
            session = BattleSession(
                session_id=str(uuid4()),
                match_id=str(uuid4()),
                generation=self.repository.next_generation(),
                state=BattleState.SELECTION_OPEN,
                battle_revision=1,
                rules_ruleset_id=rules_pin.ruleset_id,
                rules_ruleset_version=rules_pin.ruleset_version,
                rules_snapshot_id=rules_pin.rules_snapshot_id,
                rules_facts_sha256=rules_pin.rules_facts_sha256,
                opponent_intel_pin_status=intel_pin.status,
                opponent_intel_generation_id=intel_pin.generation_id,
                opponent_intel_snapshot_sha256=intel_pin.snapshot_sha256,
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
        selection_facts = self.repository.get_selection_facts(session.current_reviewed_selection_id)
        applied = self.repository.get_applied_selection(session.current_applied_selection_id)

        detailed_build = selection_facts.self_team_build
        export_schema_version = (
            MATCH_EXPORT_SCHEMA_VERSION_V2
            if detailed_build is not None
            else MATCH_EXPORT_SCHEMA_VERSION
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
            turn_payload: dict[str, Any] = {
                "turn_number": turn.turn_number,
                "reviewed_facts": {
                    "self_active": facts.self_active,
                    "opponent_active": facts.opponent_active,
                    "self_hp": facts.self_hp.value,
                    "opponent_hp": facts.opponent_hp.value,
                    "legal_moves": list(facts.legal_moves),
                    "legal_switches": list(facts.legal_switches),
                    "human_note": facts.human_note,
                    "provenance": "HUMAN_CONFIRMED",
                    "created_at_utc": self.repository.get_turn_facts_created_at(
                        facts.turn_facts_id
                    ),
                },
                "advice": (
                    {
                        "source_type": advice.source_type,
                        "model": advice.model,
                        "recommended_action_type": advice.action_type.value,
                        "recommended_action_name": advice.action_name,
                        "opponent_prediction": advice.opponent_prediction,
                        "rationale": advice.rationale,
                        "warnings": list(advice.warnings),
                        "binding": "APPLIED",
                        "legality": "VALID",
                        "created_at_utc": self.repository.get_turn_advice_created_at(
                            advice.turn_advice_id
                        ),
                    }
                    if advice is not None
                    else None
                ),
                "self_executed_action": {
                    "action_type": actual.action_type.value,
                    "action_name": actual.action_name,
                },
                "opponent_executed_action": (
                    {
                        "action_type": actual.opponent_action_type.value,
                        "action_name": actual.opponent_action_name,
                    }
                    if actual.opponent_action_type is not None
                    else None
                ),
                "action_order": actual.action_order.value,
                "recorded_at_utc": self.repository.get_recorded_action_created_at(actual.action_id),
                # Retained for backward compatibility with earlier exports.
                "actual_action": {
                    "action_type": actual.action_type.value,
                    "action_name": actual.action_name,
                },
            }
            # Gemini V2 Bundle 6: additive, present only for a turn whose
            # advice was accepted under the v2 response contract. A v1-advice
            # turn (or a turn with no advice at all) gains neither key, so a
            # match with only v1 advice keeps exporting byte-identically to
            # before this bundle. Never a fallback to flattened fields on
            # corruption -- a corrupt advice_json fails the whole export
            # closed (propagates TurnAdviceStructuredDataCorruptError) rather
            # than silently omitting the structured detail.
            if advice is not None and advice.response_schema_version == RESPONSE_SCHEMA_VERSION_V2:
                turn_payload["response_schema_version"] = advice.response_schema_version
                turn_payload["structured_response"] = turn_advice_body_v2_to_canonical_dict(
                    load_structured_turn_advice_v2(advice)
                )
            if detailed_build is not None:
                active_build = detailed_build.member_by_name(facts.self_active)
                turn_payload["reviewed_facts"]["self_active_build"] = (
                    active_build.to_canonical_dict()
                )
            turns.append(turn_payload)

        action_history = [
            {
                "turn_number": action.turn_number,
                "action_type": action.action_type.value,
                "action_name": action.action_name,
                "opponent_action_type": (
                    action.opponent_action_type.value
                    if action.opponent_action_type is not None
                    else None
                ),
                # Preserve the accepted v2 export representation while the
                # in-process domain contract uses typed None/None.
                "opponent_action_name": action.opponent_action_name or "",
                "action_order": action.action_order.value,
            }
            for action in self.repository.list_recorded_actions(session.session_id)
        ]
        selection_payload: dict[str, Any] = {
            "self_team": list(selection_facts.self_team),
            "opponent_team": list(selection_facts.opponent_team),
            "selected_three": list(applied.selected_three),
            "lead": applied.lead,
        }
        if detailed_build is not None:
            selection_payload["self_team_build"] = detailed_build.to_canonical_dict()
            selection_payload["self_team_build_sha256"] = selection_facts.self_team_build_sha256
            selection_payload["selected_three_builds"] = [
                member.to_canonical_dict()
                for member in detailed_build.selected_members(applied.selected_three)
            ]
        return {
            "schema_version": export_schema_version,
            "match_id": session.match_id,
            "session_id": session.session_id,
            "generation": session.generation,
            "outcome": outcome.outcome.value,
            "ended_at_utc": outcome.ended_at_utc,
            "final_battle_revision": outcome.final_battle_revision,
            "selection": selection_payload,
            "turns": turns,
            "action_history": action_history,
        }

    def _build_export_payload_v3(
        self,
        session: BattleSession,
        outcome: MatchOutcomeRecord,
        legacy_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Additively upgrade the legacy payload to ``maple-match.v3``.

        Only reached when :meth:`match_uses_rich_state_contract` is true for
        this exact session/match/generation -- every other match keeps using
        ``legacy_payload`` (schema v1/v2) completely unchanged.
        """

        confirmed_states = self.repository.list_confirmed_turn_states_for_match(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
        )
        try:
            validate_confirmed_states_for_export(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                outcome=outcome,
                confirmed_states=confirmed_states,
            )
        except MatchExportV3Error as exc:
            raise DomainError(f"V3_EXPORT_STATE_VALIDATION_FAILED:{exc}") from exc

        # Every exported state's turn_id/turn_number must correspond to a
        # real repository BattleTurn belonging to this exact session --
        # never inferred from the confirmed state row alone.
        turns_by_id = {
            turn.turn_id: turn for turn in self.repository.list_turns(session.session_id)
        }
        for state in confirmed_states:
            turn = turns_by_id.get(state.identity.turn_id)
            if turn is None:
                raise DomainError("V3_EXPORT_STATE_TURN_ID_NOT_FOUND")
            if turn.turn_number != state.identity.turn_number:
                raise DomainError("V3_EXPORT_STATE_TURN_NUMBER_MISMATCH")

        # Candidate deltas are loaded by based_on_confirmed_state_id alone
        # (never pre-filtered by the delta's own session/match/generation/
        # turn/revision, since those columns are themselves under
        # validation below) so a corrupt or foreign delta that references
        # one of our exported states cannot disappear before
        # validate_delta_chain_for_export ever sees it.
        delta_candidates = self.repository.list_action_result_delta_candidates_for_confirmed_states(
            tuple(state.confirmed_state_id for state in confirmed_states)
        )
        try:
            delta_by_based_on = validate_delta_chain_for_export(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                confirmed_states=confirmed_states,
                deltas=delta_candidates,
            )
        except MatchExportV3Error as exc:
            raise DomainError(f"V3_EXPORT_DELTA_VALIDATION_FAILED:{exc}") from exc

        rich_turns: dict[int, ConfirmedTurnRecord] = {}
        for state in confirmed_states:
            legal_actions = self.repository.list_confirmed_legal_action_selections_for_identity(
                state.identity
            )
            try:
                validate_legal_actions_for_export(state.identity, legal_actions)
            except MatchExportV3Error as exc:
                raise DomainError(f"V3_EXPORT_LEGAL_ACTION_VALIDATION_FAILED:{exc}") from exc

            evidence = None
            if state.evidence_id is not None:
                try:
                    evidence = self.repository.get_fixed_evidence_metadata(state.evidence_id)
                except KeyError as exc:
                    raise DomainError("V3_EXPORT_EVIDENCE_METADATA_MISSING") from exc
                if evidence.evidence_id != state.evidence_id:
                    raise DomainError("V3_EXPORT_EVIDENCE_ID_MISMATCH")
                try:
                    validate_evidence_hash_shape(evidence.sha256)
                except MatchExportV3Error as exc:
                    raise DomainError(f"V3_EXPORT_INVALID_EVIDENCE_HASH:{exc}") from exc

            # Fail closed rather than "latest wins": Bundle A's chain rules
            # guarantee at most one confirmed state per turn_number, but
            # this is re-checked explicitly here as defense in depth rather
            # than trusting a plain dict assignment to hide a violation.
            if state.identity.turn_number in rich_turns:
                raise DomainError("V3_EXPORT_CONTRADICTORY_RICH_STATE_SAME_TURN")
            rich_turns[state.identity.turn_number] = ConfirmedTurnRecord(
                confirmed_state=state,
                source_delta=delta_by_based_on.get(state.confirmed_state_id),
                confirmed_legal_actions=legal_actions,
                evidence=evidence,
            )

        try:
            payload = build_integrated_match_export_v3_payload(
                legacy_payload=legacy_payload, rich_turns=rich_turns
            )
        except MatchExportV3Error as exc:
            raise DomainError(f"V3_EXPORT_BUILD_FAILED:{exc}") from exc

        # Tournament Battle Mega: actual human-confirmed match-level state is
        # additive audit/context in rich exports. It is never represented as
        # an action-history entry or provider payload.
        payload["mega_state"] = mega_state_to_canonical_dict(
            self.repository.get_mega_state(session.session_id)
        )

        # Bundle 4 (Gemini V2): small additive audit field -- the exact
        # rules identity this match was pinned to, so a later audit can
        # answer "what exact rules snapshot did Gemini reason under?"
        # without re-parsing every turn's request. Only the identity/hash
        # values are recorded here (never the rules facts/sources
        # themselves -- those live in the checked-in snapshot, addressable
        # by this same identity). ``None`` for a legacy/pre-Bundle-4 match
        # that was never pinned; this key is additive and optional, never
        # required by ``parse_match_export_v3``.
        payload["rules_pin"] = (
            {
                "ruleset_id": session.rules_ruleset_id,
                "ruleset_version": session.rules_ruleset_version,
                "rules_snapshot_id": session.rules_snapshot_id,
                "rules_facts_sha256": session.rules_facts_sha256,
            }
            if session.rules_ruleset_id is not None
            else None
        )
        # Bundle 5 (Gemini V2): the equally small additive audit field for
        # population INTEL -- identity/provenance only, so a later audit can
        # answer "what exact population snapshot was visible to Gemini, as a
        # prior?". The population database itself and any raw source
        # document are deliberately never exported. ``None`` for a
        # legacy/pre-Bundle-5 match that was never pinned; additive and
        # optional, never required by ``parse_match_export_v3``.
        payload["opponent_intel_pin"] = self._opponent_intel_pin_export(session)
        # Gemini V2 Bundle 6: ``legacy_payload["turns"]`` (reused verbatim by
        # ``build_integrated_match_export_v3_payload`` above) already carries
        # ``structured_response`` on any turn whose advice was accepted
        # under the v2 response contract. Bumping to ``.v4`` here -- after
        # the v3 payload is otherwise fully built -- is the only schema_version
        # change; every other v3 key/shape is untouched, and a match with no
        # v2 advice at all stays ``.v3`` exactly as before this bundle.
        if any("structured_response" in turn for turn in payload.get("turns", ())):
            payload["schema_version"] = MATCH_EXPORT_SCHEMA_VERSION_V4
        return payload

    def _opponent_intel_pin_export(self, session: BattleSession) -> dict[str, Any] | None:
        """Identity/provenance of the match's INTEL pin, for audit only.

        The four provenance values (``source``/``season``/``format``/
        ``fetched_at``) plus ``snapshot_schema_version`` live inside the
        archived generation, so they are read back from it. If that archive
        is no longer resolvable, they are reported as ``null`` rather than
        guessed -- the durable pin identity is still recorded exactly as
        persisted. Export is an audit action, not a provider dispatch, so
        an unresolvable archive does not block preserving the match record.
        """

        if session.opponent_intel_pin_status is None:
            return None
        record: dict[str, Any] = {
            "status": session.opponent_intel_pin_status,
            "generation_id": session.opponent_intel_generation_id,
            "snapshot_schema_version": None,
            "snapshot_sha256": session.opponent_intel_snapshot_sha256,
            "source": None,
            "season": None,
            "format": None,
            "fetched_at": None,
        }
        generation_id = session.opponent_intel_generation_id
        if generation_id is None:
            return record
        try:
            bundle = load_pinned_generation(self.opponent_intel_directory, generation_id)
        except (GenerationStoreError, OSError):
            return record
        document = bundle.snapshot_document
        record["snapshot_schema_version"] = document.schema_version
        record["source"] = document.source
        record["season"] = document.season
        record["format"] = document.format
        record["fetched_at"] = document.fetched_at
        return record

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

    def _require_export_directory_outside_repository(self) -> None:
        if self.export_directory.is_relative_to(self.repository_root):
            raise DomainError("EXPORT_DIRECTORY_INSIDE_REPOSITORY")

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
