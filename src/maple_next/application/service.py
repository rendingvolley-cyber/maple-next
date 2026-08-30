"""Application command service for the human-operated Battle-1 lifecycle."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar
from uuid import uuid4

from maple_next.application.projection import DomainProjection, project
from maple_next.application.turn_legal_action_boundary import build_confirmed_legal_actions_input
from maple_next.application.turn_provider_export_bridge import (
    load_bundle3_turn_context,
    load_champions_rules_context,
    load_champions_rules_season_id,
    load_opponent_intel_context,
)
from maple_next.domain.battle_memory import Bundle3ContextError
from maple_next.domain.champions_rules import (
    ChampionsRulesError,
    RulesPin,
    current_rules_pin_for_new_match,
)
from maple_next.domain.enums import (
    ActionOrder,
    ActionType,
    BattleState,
    HpBucket,
    JobStatus,
    JobType,
    ResultDisposition,
)
from maple_next.domain.legal_switches import (
    LegalSwitchConfirmation,
    LegalSwitchError,
    LegalSwitchStatus,
    derive_legal_switch_candidates,
    is_confirmed_fainted,
)
from maple_next.domain.legal_switches import (
    confirm_legal_switches as build_legal_switch_confirmation,
)
from maple_next.domain.mega_evolution import (
    MegaBattleState,
    MegaSide,
    deterministic_mega_form,
)
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    RecordedAction,
    ReviewedBoardSnapshot,
    SelectionFacts,
    TurnAdviceSnapshot,
    TurnFactsSnapshot,
)
from maple_next.domain.opponent_intel_context import (
    OpponentIntelContextError,
    OpponentIntelPin,
)
from maple_next.domain.team_build import ChampionsTeamBuild
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ChangeObservation,
    ConfirmationMeta,
    ConfirmedTurnState,
    FieldDelta,
    Known,
    NextTurnStateDraft,
    PokemonLocalMemory,
    ProvenanceStep,
    TurnIdentity,
    TurnStateIdentityError,
    TurnStateStaleError,
    derive_next_turn_state_draft,
    validate_turn_state_full_chain,
)
from maple_next.domain.turn_state_projection import ProviderReadyGateError
from maple_next.opponent_intel_db.generation_store import GenerationStoreError
from maple_next.opponent_intel_db.runtime_intel import resolve_pinnable_generation
from maple_next.opponent_intel_db.runtime_paths import (
    intel_db_directory,
    resolve_intel_runtime_root,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.selection_request import (
    SelectionAdviceRequest,
    build_selection_advice_request,
)
from maple_next.providers.selection_request import (
    request_payload_hash as compute_selection_request_payload_hash,
)
from maple_next.providers.turn_advice_rich_state import (
    RichStateTurnAdviceRequest,
    build_rich_state_turn_advice_request,
)
from maple_next.providers.turn_boundary import DispatchTrigger, decide_turn_advice_dispatch
from maple_next.providers.turn_request import (
    LegalAction,
    TurnAdviceRequest,
    build_turn_advice_request,
)
from maple_next.providers.turn_request import (
    request_payload_hash as compute_turn_request_payload_hash,
)
from maple_next.providers.turn_response import (
    NormalizedTurnAdviceResult,
    TurnAdviceSchemaError,
    turn_advice_body_from_dict,
)
from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V1,
    RESPONSE_SCHEMA_VERSION_V2,
    TurnAdviceBodyV2,
    canonical_turn_advice_v2_json,
    normalize_degradable_opponent_prediction_v2,
    turn_advice_body_v2_from_canonical_json,
    turn_advice_body_v2_from_dict,
)
from maple_next.providers.turn_response_v2_semantics import (
    TurnAdviceV2SemanticResultCode,
    sanitized_reason_for_v2_semantics,
    validate_turn_advice_v2_semantics,
)
from maple_next.providers.turn_validation import (
    TurnAdviceParseError,
    TurnAdviceResultCode,
    build_normalized_turn_advice_result,
    sanitized_reason_for,
    select_response_parser_version,
    validate_turn_advice_legality,
    validate_turn_advice_legality_v2,
    validate_turn_advice_result,
)
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

_MemoryValueT = TypeVar("_MemoryValueT")


def _apply_local_memory_delta(
    previous: Known[_MemoryValueT], delta: FieldDelta[_MemoryValueT]
) -> Known[_MemoryValueT]:
    """Project one confirmed result field into match-local memory."""

    if delta.observation is ChangeObservation.CHANGED:
        assert delta.after_value is not None
        return Known.confirmed(delta.after_value, provenance_chain=delta.provenance_chain)
    if delta.observation is ChangeObservation.UNCHANGED:
        return previous
    return Known.unknown(provenance_chain=(ProvenanceStep.UNKNOWN,))


class DomainError(RuntimeError):
    """Raised when a command violates the canonical transition contract."""


#: Every fixed sanitized token a Turn Advice v1/v2 validator can produce.
#: Built from the validators' own dicts (via their public accessor
#: functions), never hand-duplicated, so it can never drift out of sync with
#: what those modules actually emit.
_TURN_ADVICE_SANITIZED_REJECTION_TOKENS: Final[frozenset[str]] = frozenset(
    sanitized_reason_for(code) for code in TurnAdviceResultCode
) | frozenset(
    sanitized_reason_for_v2_semantics(code) for code in TurnAdviceV2SemanticResultCode
)


def _invalid_payload_reason(exc: Exception) -> str:
    """Sanitized ``async_job_results.reason`` for a rejected Turn Advice response.

    Never widens what reaches the audit row to arbitrary exception text --
    that would risk echoing provider-derived content. Only two sources are
    trusted to append detail beyond the generic ``INVALID_PAYLOAD`` marker:

    - ``TurnAdviceSchemaError``/``TurnAdviceParseError``: documented (at
      their definitions) to always carry one of a small fixed set of
      sanitized tokens, never raw provider text.
    - A plain ``ValueError`` whose message is exactly one of the legality/
      semantic validators' own fixed sanitized tokens (i.e. it was raised
      via ``sanitized_reason_for``/``sanitized_reason_for_v2_semantics``).

    Any other exception (KeyError, TypeError, a ValueError from somewhere
    else, ``ProviderReadyGateError``, ``DomainError``) collapses to the
    pre-existing plain ``INVALID_PAYLOAD``, exactly as before.
    """

    if isinstance(exc, (TurnAdviceSchemaError, TurnAdviceParseError)):
        return f"INVALID_PAYLOAD:{exc}"
    if isinstance(exc, ValueError) and str(exc) in _TURN_ADVICE_SANITIZED_REJECTION_TOKENS:
        return f"INVALID_PAYLOAD:{exc}"
    return "INVALID_PAYLOAD"


#: Audit token recorded on an otherwise-normal ``APPLIED`` disposition when
#: :func:`~maple_next.providers.turn_response_v2.turn_advice_body_v2_from_dict`
#: silently canonicalized a LOW-support prediction line's ``specific_action``
#: to ``null`` (see that function's own normalization). Purely a sanitized
#: diagnostic marker for the operator/audit trail -- never derived from or
#: containing any raw provider text itself.
NORMALIZED_LOW_SUPPORT_SPECIFIC_ACTION_TO_NULL: Final[str] = (
    "NORMALIZED_LOW_SUPPORT_SPECIFIC_ACTION_TO_NULL"
)

PREDICTION_DOWNGRADED_TO_UNKNOWN: Final[str] = "prediction_downgraded_to_unknown"


def _payload_had_low_support_specific_action(payload: Any) -> bool:
    """True if the raw v2 payload named a ``specific_action`` on a LOW-support
    prediction line -- the exact shape ``turn_advice_body_v2_from_dict``
    normalizes to ``null`` rather than reject.

    Read-only inspection of ``result.payload`` already held in memory by the
    caller; nothing here persists or widens retention of that raw payload,
    it only derives one sanitized boolean for the audit trail.
    """

    if not isinstance(payload, dict):
        return False
    opponent_prediction = payload.get("opponent_prediction")
    if not isinstance(opponent_prediction, dict):
        return False
    lines: list[Any] = [opponent_prediction.get("primary")]
    alternatives = opponent_prediction.get("alternatives")
    if isinstance(alternatives, list):
        lines.extend(alternatives)
    return any(
        isinstance(line, dict)
        and line.get("support") == "LOW"
        and line.get("specific_action") is not None
        for line in lines
    )


class TurnAdviceStructuredDataCorruptError(RuntimeError):
    """Gemini V2 Bundle 6: a V2-tagged advice row's ``advice_json`` is invalid.

    Raised by :func:`load_structured_turn_advice_v2`. Callers (UI rendering,
    match export v4) must catch this explicitly and refuse to render/export
    the row's structured detail -- never silently fall back to the row's
    flattened compatibility columns as though nothing were wrong, and never
    fabricate structured data that was never actually persisted.
    """


def load_structured_turn_advice_v2(advice: TurnAdviceSnapshot) -> TurnAdviceBodyV2:
    """Decode and strictly re-validate a persisted/exported V2 advice body.

    Re-runs the exact same strict schema parser used at apply-time
    (:func:`~maple_next.providers.turn_response_v2.turn_advice_body_v2_from_dict`)
    against ``advice.advice_json`` -- a canonical JSON blob is never trusted
    merely because it was already accepted once. Fails closed
    (:class:`TurnAdviceStructuredDataCorruptError`) on anything other than a
    row explicitly tagged :data:`RESPONSE_SCHEMA_VERSION_V2` with a valid,
    schema-conformant ``advice_json``.
    """

    if advice.response_schema_version != RESPONSE_SCHEMA_VERSION_V2 or advice.advice_json is None:
        raise TurnAdviceStructuredDataCorruptError(advice.turn_advice_id)
    try:
        return turn_advice_body_v2_from_canonical_json(advice.advice_json)
    except ValueError as exc:
        # Covers both json.JSONDecodeError (a ValueError subclass) and
        # TurnAdviceSchemaError (also a ValueError subclass).
        raise TurnAdviceStructuredDataCorruptError(advice.turn_advice_id) from exc


def _resolve_new_match_rules_pin() -> RulesPin:
    """Resolve the immutable Champions rules pin for a brand-new match.

    Bundle 4 (Gemini V2): every new match is pinned to the one checked-in
    official rules snapshot at creation time (never re-resolved from
    "whatever is on disk now" later -- see ``domain/champions_rules.py``).
    A malformed/tampered/unavailable snapshot fails match creation itself,
    closed, rather than creating a session with an unverifiable pin.
    """

    try:
        return current_rules_pin_for_new_match()
    except ChampionsRulesError as exc:
        raise DomainError(f"CHAMPIONS_RULES_SNAPSHOT_UNAVAILABLE:{exc}") from exc


def _resolve_new_match_opponent_intel_pin(intel_directory: Path) -> OpponentIntelPin:
    """Resolve the immutable opponent-INTEL pin for a brand-new match.

    Bundle 5 (Gemini V2): every new match records, exactly once, either the
    immutable generation its population INTEL is pinned to or an explicit
    ``UNAVAILABLE``. Once written it is never revised -- not by
    ``save_session``, not by later request construction -- so a newer
    global generation can never retroactively change what an in-progress
    match reasons under.

    Deliberately **fail-soft**, unlike the rules pin: population INTEL is
    advisory context, so a missing, incomplete, or untrustworthy artifact
    pins ``UNAVAILABLE`` instead of blocking match creation. What is *not*
    soft is the consequence: an ``UNAVAILABLE`` pin deterministically
    resolves to an ``UNAVAILABLE`` context for the whole match and never
    silently adopts whatever snapshot appears later.
    """

    try:
        pinnable = resolve_pinnable_generation(intel_directory)
    except (GenerationStoreError, OSError):
        return OpponentIntelPin.unavailable()
    if pinnable is None:
        return OpponentIntelPin.unavailable()
    return OpponentIntelPin.pinned(
        generation_id=pinnable.generation_id, snapshot_sha256=pinnable.snapshot_sha256
    )


def _reviewed_board_snapshot_from_turn_facts(facts: TurnFactsSnapshot) -> ReviewedBoardSnapshot:
    """Adapt the flat, human-confirmed :class:`TurnFactsSnapshot` into the
    richer Lane C :class:`ReviewedBoardSnapshot` shape.

    Only ``self_active``/``opponent_active``/``self_hp``/``opponent_hp`` are
    captured by the current human turn-facts review flow; every other
    reviewed field (status, stat stages, weather, terrain, side effects) is
    not yet collected anywhere in this codebase, so it is explicit
    ``"UNKNOWN"``/default rather than guessed.
    """

    return ReviewedBoardSnapshot(
        reviewed_board_id=facts.turn_facts_id,
        turn_id=facts.turn_id,
        self_active=facts.self_active,
        opponent_active=facts.opponent_active,
        self_hp=facts.self_hp,
        opponent_hp=facts.opponent_hp,
        self_status="UNKNOWN",
        opponent_status="UNKNOWN",
    )


def _legal_actions_from_turn_facts(facts: TurnFactsSnapshot) -> tuple[LegalAction, ...]:
    """Build the canonical Lane C legal-action list from reviewed turn facts."""

    moves = tuple(
        LegalAction(
            action_id=f"MOVE:{name}",
            action_type=ActionType.MOVE,
            action_name=name,
            owner_active=facts.self_active,
        )
        for name in facts.legal_moves
    )
    switches = tuple(
        LegalAction(
            action_id=f"SWITCH:{name}",
            action_type=ActionType.SWITCH,
            action_name=name,
            switch_target=name,
        )
        for name in facts.legal_switches
    )
    return moves + switches


class BattleApplication:
    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        opponent_intel_directory: str | Path | None = None,
    ) -> None:
        self.repository = repository
        #: Bundle 5 (Gemini V2). Where immutable opponent-INTEL generations
        #: live. ``None`` means "resolve the real runtime location on
        #: demand" (the same LOCALAPPDATA/env resolution the INTEL CLI and
        #: the Battle Record UI already use); tests and offline tooling
        #: pass an explicit directory so they never read or write the
        #: operator's provisioned runtime artifact.
        self._opponent_intel_directory = (
            Path(opponent_intel_directory).expanduser()
            if opponent_intel_directory is not None
            else None
        )

    @property
    def opponent_intel_directory(self) -> Path:
        if self._opponent_intel_directory is not None:
            return self._opponent_intel_directory
        return intel_db_directory(resolve_intel_runtime_root())

    def projection(self) -> DomainProjection:
        session = self.repository.load_active_session()
        latest_job = (
            self.repository.latest_provider_job(session.session_id) if session is not None else None
        )
        current_turn_number: int | None = None
        if session is not None and session.current_turn_id is not None:
            try:
                current_turn_number = self.repository.get_turn(session.current_turn_id).turn_number
            except KeyError:
                current_turn_number = None
        return project(session, latest_job, current_turn_number=current_turn_number)

    def mega_battle_state(self) -> MegaBattleState:
        """Read the persisted actual Mega resource state for the active match."""

        session = self._require_active_session()
        return self.repository.get_mega_state(session.session_id)

    def new_match(self) -> BattleSession:
        with self.repository.transaction():
            if self.repository.load_active_session() is not None:
                raise DomainError("ACTIVE_MATCH_EXISTS")
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

    def abort_match(self, *, human_confirmed: bool) -> BattleSession:
        """Preserve an abandoned match while releasing the active slot.

        This is the explicit recovery path for a stale active session. It
        changes only the session lifecycle fields; all canonical selection,
        advice, turn, and audit records remain available for provenance.
        """

        if not human_confirmed:
            raise DomainError("HUMAN_MATCH_ABORT_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_active_session()
            if session.state in {
                BattleState.MATCH_ENDED,
                BattleState.MATCH_EXPORTED,
                BattleState.ABORTED,
            }:
                raise DomainError("MATCH_ABORT_NOT_ALLOWED_IN_CURRENT_STATE")
            session.state = BattleState.ABORTED
            session.active_slot = None
            session.bump_battle()
            self.repository.save_session(session)
        return session

    def confirm_selection_facts(
        self,
        self_team: tuple[str, ...],
        opponent_team: tuple[str, ...],
        self_team_build: ChampionsTeamBuild | None = None,
    ) -> SelectionFacts:
        try:
            facts = SelectionFacts(
                str(uuid4()),
                self_team,
                opponent_team,
                self_team_build=self_team_build,
                self_team_build_sha256=(
                    self_team_build.sha256() if self_team_build is not None else None
                ),
            )
        except ValueError as exc:
            raise DomainError("SELF_TEAM_BUILD_MISMATCH") from exc
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_OPEN)
            self.repository.append_selection_facts(session.session_id, facts)
            session.current_reviewed_selection_id = facts.reviewed_selection_id
            session.current_selection_advice_id = None
            session.bump_battle()
            self.repository.save_session(session)
        return facts

    def request_selection_advice(self, command_id: str) -> JobEnvelope:
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_OPEN)
            if session.current_reviewed_selection_id is None:
                raise DomainError("REVIEWED_SELECTION_REQUIRED")
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.SELECTION_ADVICE
            )
            self._guard_provider_request(latest_job)
            selection_facts = self.repository.get_selection_facts(
                session.current_reviewed_selection_id
            )
            request = build_selection_advice_request(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                battle_revision=session.battle_revision,
                reviewed_selection_id=session.current_reviewed_selection_id,
                self_team=selection_facts.self_team,
                opponent_team=selection_facts.opponent_team,
                self_team_build=selection_facts.self_team_build,
            )
            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=str(uuid4()),
                command_id=command_id,
                job_type=JobType.SELECTION_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=None,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.SELECTION_OPEN,
                input_snapshot_id=session.current_reviewed_selection_id,
                request_payload_hash=compute_selection_request_payload_hash(request),
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def reserve_gemini_selection_attempt(self, command_id: str) -> JobEnvelope:
        """Atomically reserve and create a human-authorized production job.

        Distinct from :meth:`request_selection_advice` (still used by the
        mock/dev Selection Advice lane): for the production Gemini lane,
        the first reservation is unique for a Selection identity. The only
        replacement is a new command/job created by another human activation
        after the current job durably ended in an allowlisted transient
        failure. Local validation failures before the reservation point never
        write a ledger row or create a job.
        """

        job_id = str(uuid4())
        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_OPEN)
            if session.current_reviewed_selection_id is None:
                raise DomainError("REVIEWED_SELECTION_REQUIRED")
            reserved = self.repository.reserve_gemini_selection_attempt(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                battle_revision=session.battle_revision,
                reviewed_selection_id=session.current_reviewed_selection_id,
                job_id=job_id,
            )
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.SELECTION_ADVICE
            )
            if not reserved:
                resend_reason = self._gemini_selection_failure_reason(
                    session, latest_job, transient_only=True
                )
                if resend_reason is None or latest_job is None:
                    raise DomainError("GEMINI_SELECTION_ATTEMPT_CONSUMED")
                replaced = self.repository.replace_gemini_selection_attempt_reservation(
                    session_id=session.session_id,
                    match_id=session.match_id,
                    generation=session.generation,
                    battle_revision=session.battle_revision,
                    reviewed_selection_id=session.current_reviewed_selection_id,
                    expected_job_id=latest_job.job_id,
                    new_job_id=job_id,
                )
                if not replaced:
                    raise DomainError("GEMINI_SELECTION_ATTEMPT_CONSUMED")
            else:
                self._guard_provider_request(latest_job)
            selection_facts = self.repository.get_selection_facts(
                session.current_reviewed_selection_id
            )
            request = build_selection_advice_request(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                battle_revision=session.battle_revision,
                reviewed_selection_id=session.current_reviewed_selection_id,
                self_team=selection_facts.self_team,
                opponent_team=selection_facts.opponent_team,
                self_team_build=selection_facts.self_team_build,
            )
            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=job_id,
                command_id=command_id,
                job_type=JobType.SELECTION_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=None,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.SELECTION_OPEN,
                input_snapshot_id=session.current_reviewed_selection_id,
                request_payload_hash=compute_selection_request_payload_hash(request),
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def gemini_selection_attempt_consumed(self) -> bool:
        """Durable, restart-safe check for the session's current Selection identity.

        Always recomputed from the database; never derived from in-memory
        adapter state, so it is correct immediately after a fresh restart
        with a brand-new adapter instance.
        """

        session = self.repository.load_active_session()
        if session is None or session.current_reviewed_selection_id is None:
            return False
        return self.repository.gemini_selection_attempt_reserved(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            reviewed_selection_id=session.current_reviewed_selection_id,
        )

    def gemini_selection_resend_eligible(self) -> bool:
        """Whether the current identity permits a new human-explicit send."""

        session = self.repository.load_active_session()
        if session is None or session.current_reviewed_selection_id is None:
            return False
        latest_job = self.repository.latest_job_by_type(
            session.session_id, JobType.SELECTION_ADVICE
        )
        return (
            self._gemini_selection_failure_reason(
                session, latest_job, transient_only=True
            )
            is not None
        )

    def gemini_selection_last_failure_reason(self) -> str | None:
        """Restore the current Selection job's sanitized failure after restart."""

        session = self.repository.load_active_session()
        if session is None or session.current_reviewed_selection_id is None:
            return None
        latest_job = self.repository.latest_job_by_type(
            session.session_id, JobType.SELECTION_ADVICE
        )
        return self._gemini_selection_failure_reason(
            session, latest_job, transient_only=False
        )

    def _gemini_selection_failure_reason(
        self,
        session: BattleSession,
        latest_job: JobEnvelope | None,
        *,
        transient_only: bool,
    ) -> str | None:
        if (
            latest_job is None
            or latest_job.session_id != session.session_id
            or latest_job.match_id != session.match_id
            or latest_job.generation != session.generation
            or latest_job.base_battle_revision != session.battle_revision
            or latest_job.input_snapshot_id != session.current_reviewed_selection_id
            or latest_job.status not in {JobStatus.FAILED, JobStatus.TIMED_OUT}
        ):
            return None
        audits = self.repository.selection_provider_attempt_audits(latest_job.job_id)
        if not audits:
            return None
        _ordinal, _model, outcome, reason = audits[-1]
        if outcome != "FAILED" or not reason:
            return None
        if transient_only and not self._is_transient_selection_failure(reason):
            return None
        return reason

    @staticmethod
    def _is_transient_selection_failure(reason: str) -> bool:
        if reason == "GEMINI_TIMEOUT" or reason.startswith("GEMINI_NETWORK_ERROR"):
            return True
        if not reason.startswith("GEMINI_HTTP_ERROR:"):
            return False
        try:
            status = int(reason.split(":", 1)[1].split("|", 1)[0])
        except ValueError:
            return False
        return status in {408, 425, 429, 500, 502, 503, 504}

    def build_selection_advice_transport_request(
        self, job: JobEnvelope
    ) -> SelectionAdviceRequest:
        """Reconstruct the exact canonical request for an already-created job.

        Used by the UI thread, once, right before handing immutable values to
        the off-thread worker. Never called from the worker itself.
        """

        if job.job_type is not JobType.SELECTION_ADVICE:
            raise DomainError("JOB_TYPE_NOT_SELECTION_ADVICE")
        selection_facts = self.repository.get_selection_facts(job.input_snapshot_id)
        request = build_selection_advice_request(
            session_id=job.session_id,
            match_id=job.match_id,
            generation=job.generation,
            battle_revision=job.base_battle_revision,
            reviewed_selection_id=job.input_snapshot_id,
            self_team=selection_facts.self_team,
            opponent_team=selection_facts.opponent_team,
            self_team_build=selection_facts.self_team_build,
        )
        if compute_selection_request_payload_hash(request) != job.request_payload_hash:
            raise DomainError("REQUEST_PAYLOAD_HASH_MISMATCH")
        return request

    def mark_selection_advice_dispatched(self, job_id: str) -> None:
        """Transition exactly one QUEUED Selection Advice job to IN_FLIGHT.

        Guards against dispatching the same job twice: a second call raises
        ``DomainError`` instead of allowing a second transport.send().
        """

        with self.repository.transaction():
            job = self.repository.get_job(job_id)
            if job.job_type is not JobType.SELECTION_ADVICE:
                raise DomainError("JOB_TYPE_NOT_SELECTION_ADVICE")
            session = self._require_active_session()
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.SELECTION_ADVICE
            )
            if latest_job is None or latest_job.job_id != job.job_id:
                raise DomainError("JOB_ID_NOT_CURRENT")
            if job.status is not JobStatus.QUEUED:
                raise DomainError("JOB_NOT_DISPATCHABLE")
            self.repository.mark_job_in_flight(job.job_id)

    def fail_selection_advice_job(self, job_id: str, reason: str) -> None:
        """Fail-closed transport/network failure. Never mutates canonical state.

        Selection facts, session state, battle_revision, and the current
        advice id are left untouched; only the job's own status changes.
        ``GEMINI_TIMEOUT`` maps to ``TIMED_OUT``; every other transport or
        config failure reason maps to ``FAILED``.
        """

        target_status = JobStatus.TIMED_OUT if reason == "GEMINI_TIMEOUT" else JobStatus.FAILED
        with self.repository.transaction():
            try:
                job = self.repository.get_job(job_id)
            except KeyError:
                return
            if job.job_type is not JobType.SELECTION_ADVICE:
                return
            if job.status in {JobStatus.QUEUED, JobStatus.IN_FLIGHT}:
                self.repository.update_job_status(job.job_id, target_status)

    def apply_selection_advice_result(self, result: ResultEnvelope) -> ResultDisposition:
        with self.repository.transaction():
            job = self._load_result_job_or_audit(result)
            if job is None:
                return ResultDisposition.STALE_REJECTED
            if job.job_type is not JobType.SELECTION_ADVICE:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_TYPE_NOT_SELECTION_ADVICE"
                )
                return ResultDisposition.STALE_REJECTED
            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_job_by_type(
                    session.session_id, JobType.SELECTION_ADVICE
                )
                if session is not None
                else None
            )
            current_snapshot_id = (
                session.current_reviewed_selection_id if session is not None else None
            )
            reason = self._binding_failure_reason(
                session,
                latest_job,
                job,
                result,
                current_input_snapshot_id=current_snapshot_id,
                current_turn_number=None,
            )
            if reason is not None:
                self.repository.audit_result(result, ResultDisposition.STALE_REJECTED, reason)
                return ResultDisposition.STALE_REJECTED

            assert session is not None
            chosen_package: str | None = None
            chosen_package_name: str | None = None
            intended_mega: str | None = None
            selection_reason: str | None = None
            try:
                selection_facts = self.repository.get_selection_facts(job.input_snapshot_id)
                selection_profile = (
                    selection_facts.self_team_build.selection_profile
                    if selection_facts.self_team_build is not None
                    else None
                )
                payload = result.payload
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
                expected_keys = (
                    {
                        "chosen_package",
                        "selected_three",
                        "lead",
                        "intended_mega",
                        "selection_reason",
                    }
                    if selection_profile is not None
                    else {"selected_three", "lead"}
                )
                if set(payload) != expected_keys:
                    raise ValueError("payload fields do not match the current Selection contract")

                selected_three = payload["selected_three"]
                if not isinstance(selected_three, list):
                    raise ValueError("selected_three must be a list")
                if len(selected_three) != 3:
                    raise ValueError("selected_three must contain exactly three entries")
                if not all(
                    isinstance(name, str) and not isinstance(name, bool)
                    for name in selected_three
                ):
                    raise ValueError("selected_three entries must be strings")
                typed_three = (selected_three[0], selected_three[1], selected_three[2])

                lead = payload["lead"]
                if not isinstance(lead, str) or isinstance(lead, bool):
                    raise ValueError("lead must be a string")

                if len(set(typed_three)) != 3 or lead not in typed_three:
                    raise ValueError("illegal selection")
                if any(name not in selection_facts.self_team for name in typed_three):
                    raise ValueError("selection outside reviewed team")
                if selection_profile is not None:
                    raw_package = payload["chosen_package"]
                    if not isinstance(raw_package, str) or isinstance(raw_package, bool):
                        raise ValueError("chosen_package must be a string")
                    package = selection_profile.package_by_id(raw_package)
                    raw_intended_mega = payload["intended_mega"]
                    if raw_intended_mega is not None and (
                        not isinstance(raw_intended_mega, str)
                        or isinstance(raw_intended_mega, bool)
                    ):
                        raise ValueError("intended_mega must be a string or null")
                    raw_reason = payload["selection_reason"]
                    if (
                        not isinstance(raw_reason, str)
                        or isinstance(raw_reason, bool)
                        or not raw_reason.strip()
                        or len(raw_reason.strip()) > 500
                    ):
                        raise ValueError("selection_reason must be concise text")
                    if (
                        not selection_profile.mixing_allowed
                        and set(typed_three) != set(package.members)
                    ):
                        raise ValueError("selected_three mixes fixed packages")
                    if lead not in package.members:
                        raise ValueError("lead is outside chosen package")
                    if raw_intended_mega != package.intended_mega:
                        raise ValueError("intended_mega does not match chosen package")
                    chosen_package = package.package_id
                    chosen_package_name = package.name
                    intended_mega = raw_intended_mega
                    selection_reason = raw_reason.strip()
                backline_values = tuple(name for name in typed_three if name != lead)
                backline = (backline_values[0], backline_values[1])
            except (KeyError, IndexError, TypeError, ValueError):
                self.repository.audit_result(
                    result, ResultDisposition.INVALID_REJECTED, "INVALID_PAYLOAD"
                )
                self.repository.update_job_status(job.job_id, JobStatus.FAILED)
                return ResultDisposition.INVALID_REJECTED

            advice_id = result.result_id
            self.repository.append_selection_advice(
                advice_id,
                session.session_id,
                job.job_id,
                typed_three,
                lead,
                backline,
                source_type=result.source_type,
                model=result.model,
                chosen_package=chosen_package,
                chosen_package_name=chosen_package_name,
                intended_mega=intended_mega,
                selection_reason=selection_reason,
            )
            session.current_selection_advice_id = advice_id
            session.state = BattleState.SELECTION_ADVICE_READY
            session.bump_battle()
            self.repository.save_session(session)
            self.repository.update_job_status(job.job_id, JobStatus.SUCCEEDED)
            self.repository.audit_result(result, ResultDisposition.APPLIED, "BINDING_ACCEPTED")
        return ResultDisposition.APPLIED

    def apply_selection(
        self,
        *,
        selected_three: tuple[str, str, str],
        lead: str,
        human_confirmed: bool,
    ) -> AppliedSelectionSnapshot:
        if not human_confirmed:
            raise DomainError("HUMAN_APPLY_REQUIRED")

        with self.repository.transaction():
            session = self._require_session(BattleState.SELECTION_ADVICE_READY)
            if session.current_selection_advice_id is None:
                raise DomainError("CURRENT_SELECTION_ADVICE_REQUIRED")
            if session.current_reviewed_selection_id is None:
                raise DomainError("REVIEWED_SELECTION_UNAVAILABLE")

            try:
                selection_facts = self.repository.get_selection_facts(
                    session.current_reviewed_selection_id
                )
            except KeyError as exc:
                raise DomainError("REVIEWED_SELECTION_UNAVAILABLE") from exc

            if len(selected_three) != 3:
                raise DomainError("SELECTED_THREE_MUST_HAVE_EXACTLY_THREE")
            typed_three = (selected_three[0], selected_three[1], selected_three[2])
            if len(set(typed_three)) != 3:
                raise DomainError("DUPLICATE_SELECTION")
            if any(name not in selection_facts.self_team for name in typed_three):
                raise DomainError("SELECTION_OUTSIDE_REVIEWED_TEAM")
            if lead not in typed_three:
                raise DomainError("LEAD_NOT_IN_SELECTED_THREE")

            # Tournament P0 / maple-team.v3. Human APPLY may intentionally
            # differ from Gemini advice, but it must still obey the bound
            # human-authored Selection Profile. For fixed_packages with
            # mixing_allowed=false, any cross-package trio is invalid battle
            # state and must fail before the first durable applied-selection
            # write. This is defense in depth in addition to provider-result
            # validation; it also protects manual operator override paths.
            selection_profile = (
                selection_facts.self_team_build.selection_profile
                if selection_facts.self_team_build is not None
                else None
            )
            if selection_profile is not None and not selection_profile.mixing_allowed:
                matching_package = next(
                    (
                        package
                        for package in selection_profile.packages
                        if set(typed_three) == set(package.members)
                    ),
                    None,
                )
                if matching_package is None:
                    raise DomainError("SELECTION_MIXES_FIXED_PACKAGES")

            backline_values = tuple(name for name in typed_three if name != lead)
            backline = (backline_values[0], backline_values[1])
            snapshot = AppliedSelectionSnapshot(
                applied_selection_id=str(uuid4()),
                selected_three=typed_three,
                lead=lead,
                backline=backline,
                source_advice_id=session.current_selection_advice_id,
            )
            self.repository.append_applied_selection(session.session_id, snapshot)
            session.current_applied_selection_id = snapshot.applied_selection_id
            session.state = BattleState.BATTLE_READY
            session.bump_battle()
            self.repository.save_session(session)
        return snapshot

    def start_turn_capture(self) -> BattleTurn:
        with self.repository.transaction():
            session = self._require_session(BattleState.BATTLE_READY)
            if session.current_turn_id is not None:
                raise DomainError("CURRENT_TURN_ALREADY_EXISTS")
            turn = BattleTurn(turn_id=str(uuid4()), turn_number=1)
            self.repository.append_turn(session.session_id, turn)
            self._set_pending_turn(session, turn)
            self.repository.save_session(session)
        return turn

    def confirm_turn_facts(
        self,
        *,
        self_active: str,
        opponent_active: str,
        self_hp: HpBucket,
        opponent_hp: HpBucket,
        legal_moves: tuple[str, ...],
        legal_switches: tuple[str, ...],
        human_note: str,
        human_confirmed: bool,
    ) -> TurnFactsSnapshot:
        if not human_confirmed:
            raise DomainError("HUMAN_TURN_FACTS_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_active_session()
            if session.state not in {
                BattleState.TURN_CAPTURE_PENDING,
                BattleState.TURN_REVIEWED,
            }:
                raise DomainError("EXPECTED_TURN_CAPTURE_PENDING_OR_REVIEWED")
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_applied_selection_id is None:
                raise DomainError("APPLIED_SELECTION_REQUIRED")

            turn = self.repository.get_turn(session.current_turn_id)
            applied = self.repository.get_applied_selection(
                session.current_applied_selection_id
            )
            normalized_active = self_active.strip()
            normalized_opponent = opponent_active.strip()
            normalized_moves = tuple(name.strip() for name in legal_moves)
            normalized_switches = tuple(name.strip() for name in legal_switches)

            if normalized_active not in applied.selected_three:
                raise DomainError("SELF_ACTIVE_OUTSIDE_APPLIED_SELECTION")
            if any(name not in applied.selected_three for name in normalized_switches):
                raise DomainError("SWITCH_OUTSIDE_APPLIED_SELECTION")
            if normalized_active in normalized_switches:
                raise DomainError("SELF_ACTIVE_IN_SWITCH_CANDIDATES")

            previous_snapshot_id = session.current_reviewed_board_id
            try:
                snapshot = TurnFactsSnapshot(
                    turn_facts_id=str(uuid4()),
                    turn_id=turn.turn_id,
                    turn_number=turn.turn_number,
                    self_active=normalized_active,
                    opponent_active=normalized_opponent,
                    self_hp=self_hp,
                    opponent_hp=opponent_hp,
                    legal_moves=normalized_moves,
                    legal_switches=normalized_switches,
                    human_note=human_note.strip(),
                    previous_snapshot_id=previous_snapshot_id,
                )
            except ValueError as exc:
                raise DomainError(f"INVALID_TURN_FACTS:{exc}") from exc

            if previous_snapshot_id is not None:
                latest_turn_job = self.repository.latest_job_by_type(
                    session.session_id, JobType.TURN_ADVICE
                )
                if latest_turn_job is not None and latest_turn_job.status in {
                    JobStatus.QUEUED,
                    JobStatus.IN_FLIGHT,
                }:
                    self.repository.update_job_status(
                        latest_turn_job.job_id, JobStatus.CANCELLED
                    )

            self.repository.append_turn_facts(session.session_id, snapshot)
            session.current_reviewed_board_id = snapshot.turn_facts_id
            session.current_turn_advice_id = None
            session.state = BattleState.TURN_REVIEWED
            session.bump_battle()
            self.repository.save_session(session)
        return snapshot

    def _build_turn_advice_request(
        self,
        *,
        session_id: str,
        match_id: str,
        generation: int,
        battle_revision: int,
        facts: TurnFactsSnapshot,
        selected_three: tuple[str, str, str],
    ) -> TurnAdviceRequest:
        """Pure reconstruction of the canonical Lane C request from stored facts.

        Never touches the network, never mutates state. Called from both
        ``request_turn_advice`` (to compute the durable ledger identity and
        the job's recorded hash) and ``build_turn_advice_transport_request``
        (to hand the exact same request to the off-thread transport).
        """

        reviewed_snapshot = _reviewed_board_snapshot_from_turn_facts(facts)
        legal_actions = _legal_actions_from_turn_facts(facts)
        self_team_build: ChampionsTeamBuild | None = None
        session = self.repository.load_active_session()
        if session is not None and session.current_reviewed_selection_id is not None:
            selection_facts = self.repository.get_selection_facts(
                session.current_reviewed_selection_id
            )
            self_team_build = selection_facts.self_team_build
        return build_turn_advice_request(
            session_id=session_id,
            match_id=match_id,
            generation=generation,
            turn_number=facts.turn_number,
            battle_revision=battle_revision,
            reviewed_snapshot_id=facts.turn_facts_id,
            reviewed_snapshot=reviewed_snapshot,
            self_active=facts.self_active,
            selected_three=selected_three,
            legal_actions=legal_actions,
            self_team_build=self_team_build,
        )

    def request_turn_advice(self, command_id: str) -> JobEnvelope:
        """Reserve the one durable Turn Advice attempt and create its job.

        Every input to the dispatch decision (current binding, pending job,
        already-consumed attempt) is recomputed here from durable repository
        state inside one transaction — never from UI in-memory state — so a
        stale UI, a resend after restart, or a resend after a terminal
        failure can never produce a second attempt for the same Turn
        identity ``(session_id, match_id, generation, turn_number,
        battle_revision, reviewed_snapshot_id, request_payload_hash)``.
        """

        job_id = str(uuid4())
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_REVIEWED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_reviewed_board_id is None:
                raise DomainError("REVIEWED_TURN_FACTS_REQUIRED")
            if session.current_applied_selection_id is None:
                raise DomainError("APPLIED_SELECTION_REQUIRED")
            if session.current_turn_advice_id is not None:
                raise DomainError("CURRENT_TURN_ADVICE_EXISTS")

            turn = self.repository.get_turn(session.current_turn_id)
            facts = self.repository.get_turn_facts(session.current_reviewed_board_id)
            applied = self.repository.get_applied_selection(session.current_applied_selection_id)
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.TURN_ADVICE
            )
            self._guard_provider_request(latest_job)

            request = self._build_turn_advice_request(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                battle_revision=session.battle_revision,
                facts=facts,
                selected_three=applied.selected_three,
            )
            request_hash = compute_turn_request_payload_hash(request)

            has_pending_job = latest_job is not None and latest_job.status in {
                JobStatus.QUEUED,
                JobStatus.IN_FLIGHT,
            }
            attempt_consumed = self.repository.turn_advice_attempt_reserved(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=turn.turn_number,
                reviewed_snapshot_id=facts.turn_facts_id,
            )
            decision = decide_turn_advice_dispatch(
                trigger=DispatchTrigger.TRUSTED_HUMAN_ACTIVATION,
                is_current_binding=True,
                has_pending_job=has_pending_job,
                attempt_consumed=attempt_consumed,
            )
            if not decision.allowed:
                raise DomainError(decision.reason_code)

            reserved = self.repository.reserve_turn_advice_attempt(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=turn.turn_number,
                battle_revision=session.battle_revision,
                reviewed_snapshot_id=facts.turn_facts_id,
                request_payload_hash=request_hash,
                job_id=job_id,
            )
            if not reserved:
                raise DomainError("TURN_ADVICE_ATTEMPT_CONSUMED")

            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=job_id,
                command_id=command_id,
                job_type=JobType.TURN_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=turn.turn_number,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.TURN_REVIEWED,
                input_snapshot_id=facts.turn_facts_id,
                request_payload_hash=request_hash,
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def request_rich_turn_advice(self, command_id: str) -> JobEnvelope:
        """Forge-resistant durable application API for a rich-state Turn Advice request.

        Accepts no externally supplied ``BattleState``, legal actions, latest
        pointers, attempt status, pending-job status, or ``DispatchDecision``
        -- ``command_id`` is the only caller-supplied value. Every durable
        fact (session, current turn, latest ``ConfirmedTurnState``, final
        confirmed legal actions, any unresolved ``NextTurnStateDraft`` and
        its full chain, fixed evidence metadata) is loaded and validated
        from the repository inside one transaction, exactly like the legacy
        :meth:`request_turn_advice`. Reuses the existing
        ``turn_advice_attempt_ledger`` (no second ledger/dispatch policy)
        and the existing ``JobType.TURN_ADVICE`` job lane -- a rich request
        and a legacy request for the same session share the same pending-
        job/one-attempt binding slot.
        """

        job_id = str(uuid4())
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_REVIEWED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_applied_selection_id is None:
                raise DomainError("APPLIED_SELECTION_REQUIRED")

            turn = self.repository.get_turn(session.current_turn_id)
            current_identity = TurnIdentity(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_id=turn.turn_id,
                turn_number=turn.turn_number,
                battle_revision=session.battle_revision,
            )

            latest_state = self.repository.get_latest_confirmed_turn_state_for_identity(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
            )
            if latest_state is None:
                raise DomainError("NO_CONFIRMED_TURN_STATE")
            if latest_state.identity != current_identity:
                raise DomainError("CONFIRMED_STATE_NOT_CURRENT_BINDING")

            confirmed_legal_actions = (
                self.repository.list_confirmed_legal_action_selections_for_identity(
                    current_identity
                )
            )
            # Accepted Bundle A boundary: proves every selection is a
            # ConfirmedLegalActionSelection bound to this exact identity,
            # non-blank, non-duplicate, and MOVE/SWITCH-valid. Its return
            # value is not used further -- passing the boundary is the proof.
            build_confirmed_legal_actions_input(current_identity, confirmed_legal_actions)

            candidate_drafts = self._discover_candidate_open_drafts(latest_state)
            latest_open_draft: NextTurnStateDraft | None = None
            latest_open_draft_turn_number: int | None = None
            latest_open_draft_battle_revision: int | None = None
            if candidate_drafts:
                if len(candidate_drafts) > 1:
                    raise DomainError("CONTRADICTORY_DUPLICATE_OPEN_DRAFT_REJECTED")
                latest_open_draft = candidate_drafts[0]
                if (
                    latest_open_draft.identity.session_id != session.session_id
                    or latest_open_draft.identity.match_id != session.match_id
                    or latest_open_draft.identity.generation != session.generation
                ):
                    raise DomainError("FOREIGN_OPEN_DRAFT_REJECTED")
                try:
                    source_delta = self.repository.get_action_result_delta(
                        latest_open_draft.source_delta_id
                    )
                except KeyError as exc:
                    raise DomainError("OPEN_DRAFT_SOURCE_DELTA_MISSING") from exc
                try:
                    validate_turn_state_full_chain(latest_state, source_delta, latest_open_draft)
                except (TurnStateIdentityError, TurnStateStaleError) as exc:
                    raise DomainError(f"OPEN_DRAFT_CHAIN_INVALID:{exc}") from exc
                latest_open_draft_turn_number = latest_open_draft.identity.turn_number
                latest_open_draft_battle_revision = latest_open_draft.identity.battle_revision

            evidence = None
            if latest_state.evidence_id is not None:
                try:
                    evidence = self.repository.get_fixed_evidence_metadata(
                        latest_state.evidence_id
                    )
                except KeyError as exc:
                    raise DomainError("EVIDENCE_METADATA_MISSING") from exc

            applied = self.repository.get_applied_selection(session.current_applied_selection_id)
            mega_state = self.repository.get_mega_state(session.session_id)
            self_team_build_sha256: str | None = None
            if session.current_reviewed_selection_id is not None:
                selection_facts = self.repository.get_selection_facts(
                    session.current_reviewed_selection_id
                )
                self_team_build_sha256 = selection_facts.self_team_build_sha256

            self_active_known = latest_state.self_side.active
            if (
                not self_active_known.is_confirmed
                or not self_active_known.value
                or self_active_known.value == "UNKNOWN"
            ):
                raise DomainError("SELF_ACTIVE_UNKNOWN")

            legal_switch_confirmation = self.repository.get_legal_switch_confirmation(
                identity=current_identity,
                based_on_confirmed_state_id=latest_state.confirmed_state_id,
                applied_selection_id=applied.applied_selection_id,
            )

            # Bundle 3 (Gemini V2): confirmed prior battle memory + canonical
            # selected-three build context, loaded and validated by the one
            # shared helper that the offline rebuild path below also uses.
            try:
                bundle3_context = load_bundle3_turn_context(
                    self.repository,
                    session=session,
                    current_identity=current_identity,
                    current_confirmed_state=latest_state,
                    applied=applied,
                )
            except Bundle3ContextError as exc:
                raise DomainError(f"BUNDLE3_CONTEXT_INVALID:{exc}") from exc

            # Bundle 4 (Gemini V2): resolve the match's persisted rules pin
            # against the immutable checked-in official Champions rules
            # snapshot, through the one shared helper the offline rebuild
            # path below also uses. Fails closed on an unpinned match, a
            # pin that no longer matches the checked-in snapshot, or a
            # corrupted/tampered snapshot -- never falls back to sending a
            # request without a proven rules_context.
            try:
                rules_context = load_champions_rules_context(session)
                # Validation input only (never part of the canonical
                # request): the canonical pinned season the provider-ready
                # boundary revalidates an AVAILABLE INTEL context's MATCHED
                # compatibility claim against, alongside the battle format
                # rules_context already carries.
                rules_season_id = load_champions_rules_season_id(session)
            except ChampionsRulesError as exc:
                raise DomainError(f"CHAMPIONS_RULES_CONTEXT_INVALID:{exc}") from exc

            # Bundle 5 (Gemini V2): resolve the match's persisted INTEL pin
            # against immutable archived generation storage, through the
            # one shared helper the offline rebuild path below also uses.
            # Never resolves "the current generation", never contacts a
            # network, and never falls back to the legacy opponent-meta
            # cache. Fails closed when a PINNED generation is missing,
            # corrupt, or resolves to a different identity than pinned;
            # fails soft (deterministic UNAVAILABLE/MISMATCHED, request
            # still provider-ready) for an unpinned match, an unconfirmed
            # opponent active, an absent species, or a season/format
            # mismatch.
            try:
                opponent_intel_context = load_opponent_intel_context(
                    session,
                    confirmed_state=latest_state,
                    intel_directory=self.opponent_intel_directory,
                )
            except (OpponentIntelContextError, ChampionsRulesError) as exc:
                raise DomainError(f"OPPONENT_INTEL_CONTEXT_INVALID:{exc}") from exc

            try:
                request = build_rich_state_turn_advice_request(
                    confirmed_state=latest_state,
                    confirmed_legal_actions=confirmed_legal_actions,
                    current_identity=current_identity,
                    latest_confirmed_state_id=latest_state.confirmed_state_id,
                    latest_open_draft_turn_number=latest_open_draft_turn_number,
                    latest_open_draft_battle_revision=latest_open_draft_battle_revision,
                    legal_switch_confirmation=legal_switch_confirmation,
                    selected_three=applied.selected_three,
                    self_active=self_active_known.value,
                    bundle3_context=bundle3_context,
                    rules_context=rules_context,
                    opponent_intel_context=opponent_intel_context,
                    mega_state=mega_state,
                    rules_season_id=rules_season_id,
                    evidence=evidence,
                    self_team_build_sha256=self_team_build_sha256,
                    confirmed_fainted_members=self._confirmed_fainted_members(
                        current_identity, applied
                    ),
                )
            except ProviderReadyGateError as exc:
                raise DomainError("PROVIDER_READY_GATE_DENIED") from exc

            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.TURN_ADVICE
            )
            has_pending_job = latest_job is not None and latest_job.status in {
                JobStatus.QUEUED,
                JobStatus.IN_FLIGHT,
            }
            attempt_consumed = self.repository.turn_advice_attempt_reserved(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=current_identity.turn_number,
                reviewed_snapshot_id=latest_state.confirmed_state_id,
            )
            decision = decide_turn_advice_dispatch(
                trigger=DispatchTrigger.TRUSTED_HUMAN_ACTIVATION,
                is_current_binding=True,
                has_pending_job=has_pending_job,
                attempt_consumed=attempt_consumed,
            )
            if not decision.allowed:
                raise DomainError(decision.reason_code)

            reserved = self.repository.reserve_turn_advice_attempt(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=current_identity.turn_number,
                battle_revision=current_identity.battle_revision,
                reviewed_snapshot_id=latest_state.confirmed_state_id,
                request_payload_hash=request.request_hash,
                job_id=job_id,
            )
            if not reserved:
                raise DomainError("TURN_ADVICE_ATTEMPT_CONSUMED")

            job = JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=job_id,
                command_id=command_id,
                job_type=JobType.TURN_ADVICE,
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                turn_number=current_identity.turn_number,
                base_battle_revision=session.battle_revision,
                expected_state=BattleState.TURN_REVIEWED,
                input_snapshot_id=latest_state.confirmed_state_id,
                request_payload_hash=request.request_hash,
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.QUEUED,
            )
            self.repository.insert_job(job)
        return job

    def turn_advice_attempt_consumed(self) -> bool:
        """Durable, restart-safe check for the session's current Turn identity.

        Always recomputed from the database; correct immediately after a
        fresh restart with a brand-new application/adapter instance.
        """

        session = self.repository.load_active_session()
        if (
            session is None
            or session.current_turn_id is None
            or session.current_reviewed_board_id is None
        ):
            return False
        turn = self.repository.get_turn(session.current_turn_id)
        return self.repository.turn_advice_attempt_reserved(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            turn_number=turn.turn_number,
            reviewed_snapshot_id=session.current_reviewed_board_id,
        )

    def build_turn_advice_transport_request(self, job: JobEnvelope) -> TurnAdviceRequest:
        """Reconstruct the exact canonical Turn Advice request for a job.

        Used by the UI thread, once, right before handing immutable values to
        the off-thread fake/injected worker. Never called from the worker
        itself. Raises :class:`DomainError` if the reconstructed payload hash
        no longer matches the job's recorded hash (defense in depth against
        a stale/tampered job id).
        """

        if job.job_type is not JobType.TURN_ADVICE:
            raise DomainError("JOB_TYPE_NOT_TURN_ADVICE")
        facts = self.repository.get_turn_facts(job.input_snapshot_id)
        session = self._require_active_session()
        if session.current_applied_selection_id is None:
            raise DomainError("APPLIED_SELECTION_REQUIRED")
        applied = self.repository.get_applied_selection(session.current_applied_selection_id)
        request = self._build_turn_advice_request(
            session_id=job.session_id,
            match_id=job.match_id,
            generation=job.generation,
            battle_revision=job.base_battle_revision,
            facts=facts,
            selected_three=applied.selected_three,
        )
        if compute_turn_request_payload_hash(request) != job.request_payload_hash:
            raise DomainError("REQUEST_PAYLOAD_HASH_MISMATCH")
        return request

    def _discover_candidate_open_drafts(
        self, latest_state: ConfirmedTurnState
    ) -> tuple[NextTurnStateDraft, ...]:
        """Every OPEN draft candidate for ``latest_state``, found two independent ways.

        A draft is discoverable either by its own (possibly corrupted)
        ``based_on_confirmed_state_id`` column, or -- independently -- by
        being the referrer of a delta that is durably based on
        ``latest_state`` (``action_result_deltas.based_on_confirmed_state_id
        == latest_state.confirmed_state_id``). Taking the union means a
        draft whose own ``based_on_confirmed_state_id`` was corrupted but
        whose ``source_delta_id`` genuinely points at a current-chain delta
        cannot disappear as "no draft" -- it is still surfaced here, and
        :meth:`request_rich_turn_advice` then runs it through full-chain
        validation, which is what actually detects and rejects the
        corruption. "No draft" is only concluded when neither discovery
        path finds any candidate.
        """

        by_based_on = self.repository.list_candidate_next_turn_state_drafts_for_confirmed_state(
            latest_state.confirmed_state_id
        )
        deltas_based_on_current = self.repository.list_action_result_deltas_based_on(
            latest_state.confirmed_state_id
        )
        by_source_delta = self.repository.list_next_turn_state_drafts_by_source_delta_ids(
            tuple(delta.delta_id for delta in deltas_based_on_current)
        )
        candidates_by_draft_id: dict[str, NextTurnStateDraft] = {}
        for draft in (*by_based_on, *by_source_delta):
            candidates_by_draft_id[draft.draft_id] = draft
        return tuple(candidates_by_draft_id.values())

    def build_rich_turn_advice_transport_request(
        self, job: JobEnvelope
    ) -> RichStateTurnAdviceRequest:
        """Offline rebuild of the exact rich-state request for a job.

        Reloads every source value from durable repository state, rebuilds
        the same canonical rich request, and recomputes its hash against
        ``job.request_payload_hash``. Never reserves another attempt, never
        inserts another job, never marks dispatched, never sends. If durable
        state changed after job creation such that the rebuild would produce
        different bytes (or would no longer pass the provider-ready gate),
        this fails closed with :class:`DomainError` rather than silently
        rebuilding something different from what was authorized.
        """

        if job.job_type is not JobType.TURN_ADVICE:
            raise DomainError("JOB_TYPE_NOT_TURN_ADVICE")
        if not self.repository.match_uses_rich_state_contract(
            session_id=job.session_id, match_id=job.match_id, generation=job.generation
        ):
            raise DomainError("JOB_NOT_RICH_STATE_CONTRACT")

        try:
            confirmed_state = self.repository.get_confirmed_turn_state(job.input_snapshot_id)
        except KeyError as exc:
            raise DomainError("REBUILD_CONFIRMED_STATE_NOT_FOUND") from exc

        current_identity = TurnIdentity(
            session_id=job.session_id,
            match_id=job.match_id,
            generation=job.generation,
            turn_id=confirmed_state.identity.turn_id,
            turn_number=confirmed_state.identity.turn_number,
            battle_revision=job.base_battle_revision,
        )
        if confirmed_state.identity != current_identity:
            raise DomainError("REBUILD_STATE_IDENTITY_MISMATCH")

        confirmed_legal_actions = (
            self.repository.list_confirmed_legal_action_selections_for_identity(current_identity)
        )
        build_confirmed_legal_actions_input(current_identity, confirmed_legal_actions)

        latest_open_draft = self.repository.get_latest_next_turn_state_draft_for_identity(
            session_id=job.session_id, match_id=job.match_id, generation=job.generation
        )
        latest_open_draft_turn_number = (
            latest_open_draft.identity.turn_number if latest_open_draft is not None else None
        )
        latest_open_draft_battle_revision = (
            latest_open_draft.identity.battle_revision if latest_open_draft is not None else None
        )

        evidence = None
        if confirmed_state.evidence_id is not None:
            try:
                evidence = self.repository.get_fixed_evidence_metadata(confirmed_state.evidence_id)
            except KeyError as exc:
                raise DomainError("EVIDENCE_METADATA_MISSING") from exc

        session = self._require_active_session()
        if session.current_applied_selection_id is None:
            raise DomainError("APPLIED_SELECTION_REQUIRED")
        applied = self.repository.get_applied_selection(session.current_applied_selection_id)
        mega_state = self.repository.get_mega_state(session.session_id)
        self_team_build_sha256: str | None = None
        if session.current_reviewed_selection_id is not None:
            selection_facts = self.repository.get_selection_facts(
                session.current_reviewed_selection_id
            )
            self_team_build_sha256 = selection_facts.self_team_build_sha256

        self_active_known = confirmed_state.self_side.active
        if (
            not self_active_known.is_confirmed
            or not self_active_known.value
            or self_active_known.value == "UNKNOWN"
        ):
            raise DomainError("SELF_ACTIVE_UNKNOWN")

        legal_switch_confirmation = self.repository.get_legal_switch_confirmation(
            identity=current_identity,
            based_on_confirmed_state_id=confirmed_state.confirmed_state_id,
            applied_selection_id=applied.applied_selection_id,
        )

        # Bundle 3 (Gemini V2): rebuilt through the exact same shared helper
        # the authorizing path used, so identical durable state always
        # reproduces identical request bytes -- and a selection rebind or a
        # changed/corrupt history fails closed here instead of silently
        # rebuilding something else.
        try:
            bundle3_context = load_bundle3_turn_context(
                self.repository,
                session=session,
                current_identity=current_identity,
                current_confirmed_state=confirmed_state,
                applied=applied,
            )
        except Bundle3ContextError as exc:
            raise DomainError(f"REBUILD_BUNDLE3_CONTEXT_INVALID:{exc}") from exc

        # Bundle 4 (Gemini V2): rebuilt through the exact same shared helper
        # the authorizing path used -- identical persisted pin + identical
        # (unchanged) checked-in snapshot always reproduces byte-identical
        # ``rules_context``. A pin that no longer resolves fails closed here
        # instead of silently rebuilding under different rules.
        try:
            rules_context = load_champions_rules_context(session)
            # Same validation-only input as the authorizing path above, so
            # the rebuild boundary revalidates an AVAILABLE INTEL context's
            # MATCHED claim against both canonical rules axes too.
            rules_season_id = load_champions_rules_season_id(session)
        except ChampionsRulesError as exc:
            raise DomainError(f"REBUILD_CHAMPIONS_RULES_CONTEXT_INVALID:{exc}") from exc

        # Bundle 5 (Gemini V2): rebuilt through the exact same shared helper
        # the authorizing path used, resolving the exact *persisted* INTEL
        # generation (never "current"), so identical durable state always
        # reproduces byte-identical ``opponent_intel_context`` -- and a
        # missing/corrupt/substituted generation fails closed here instead
        # of silently rebuilding under different population data.
        try:
            opponent_intel_context = load_opponent_intel_context(
                session,
                confirmed_state=confirmed_state,
                intel_directory=self.opponent_intel_directory,
            )
        except (OpponentIntelContextError, ChampionsRulesError) as exc:
            raise DomainError(f"REBUILD_OPPONENT_INTEL_CONTEXT_INVALID:{exc}") from exc

        try:
            request = build_rich_state_turn_advice_request(
                confirmed_state=confirmed_state,
                confirmed_legal_actions=confirmed_legal_actions,
                current_identity=current_identity,
                latest_confirmed_state_id=confirmed_state.confirmed_state_id,
                latest_open_draft_turn_number=latest_open_draft_turn_number,
                latest_open_draft_battle_revision=latest_open_draft_battle_revision,
                legal_switch_confirmation=legal_switch_confirmation,
                selected_three=applied.selected_three,
                self_active=self_active_known.value,
                bundle3_context=bundle3_context,
                rules_context=rules_context,
                opponent_intel_context=opponent_intel_context,
                mega_state=mega_state,
                rules_season_id=rules_season_id,
                evidence=evidence,
                self_team_build_sha256=self_team_build_sha256,
                confirmed_fainted_members=self._confirmed_fainted_members(
                    current_identity, applied
                ),
            )
        except ProviderReadyGateError as exc:
            raise DomainError("REBUILD_STATE_NO_LONGER_PROVIDER_READY") from exc

        if request.request_hash != job.request_payload_hash:
            raise DomainError("REQUEST_PAYLOAD_HASH_MISMATCH")
        return request

    def mark_turn_advice_dispatched(self, job_id: str) -> None:
        """Transition exactly one QUEUED Turn Advice job to IN_FLIGHT.

        Guards against dispatching the same job twice: a second call raises
        ``DomainError`` instead of allowing a second transport.send().
        """

        with self.repository.transaction():
            job = self.repository.get_job(job_id)
            if job.job_type is not JobType.TURN_ADVICE:
                raise DomainError("JOB_TYPE_NOT_TURN_ADVICE")
            session = self._require_active_session()
            latest_job = self.repository.latest_job_by_type(
                session.session_id, JobType.TURN_ADVICE
            )
            if latest_job is None or latest_job.job_id != job.job_id:
                raise DomainError("JOB_ID_NOT_CURRENT")
            if job.status is not JobStatus.QUEUED:
                raise DomainError("JOB_NOT_DISPATCHABLE")
            self.repository.mark_job_in_flight(job.job_id)

    def fail_turn_advice_job(self, job_id: str, reason: str) -> None:
        """Fail-closed transport/config failure. Never mutates canonical state.

        Turn facts, session state, battle_revision, and the current advice id
        are left untouched; only the job's own status changes.
        ``GEMINI_TIMEOUT`` maps to ``TIMED_OUT``; every other transport or
        config failure reason maps to ``FAILED``.
        """

        target_status = JobStatus.TIMED_OUT if reason == "GEMINI_TIMEOUT" else JobStatus.FAILED
        with self.repository.transaction():
            try:
                job = self.repository.get_job(job_id)
            except KeyError:
                return
            if job.job_type is not JobType.TURN_ADVICE:
                return
            if job.status in {JobStatus.QUEUED, JobStatus.IN_FLIGHT}:
                self.repository.update_job_status(job.job_id, target_status)

    def apply_turn_advice_result(self, result: ResultEnvelope) -> ResultDisposition:
        with self.repository.transaction():
            job = self._load_result_job_or_audit(result)
            if job is None:
                return ResultDisposition.STALE_REJECTED
            if job.job_type is not JobType.TURN_ADVICE:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_TYPE_NOT_TURN_ADVICE"
                )
                return ResultDisposition.STALE_REJECTED
            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_job_by_type(session.session_id, JobType.TURN_ADVICE)
                if session is not None
                else None
            )
            current_turn_number: int | None = None
            if session is not None and session.current_turn_id is not None:
                current_turn_number = self.repository.get_turn(
                    session.current_turn_id
                ).turn_number
            current_snapshot_id = (
                session.current_reviewed_board_id if session is not None else None
            )
            reason = self._binding_failure_reason(
                session,
                latest_job,
                job,
                result,
                current_input_snapshot_id=current_snapshot_id,
                current_turn_number=current_turn_number,
            )
            if reason is not None:
                self.repository.audit_result(result, ResultDisposition.STALE_REJECTED, reason)
                return ResultDisposition.STALE_REJECTED

            assert session is not None
            assert session.current_turn_id is not None
            try:
                if session.current_applied_selection_id is None:
                    raise ValueError("applied selection required")
                applied = self.repository.get_applied_selection(
                    session.current_applied_selection_id
                )
                facts = self.repository.get_turn_facts(job.input_snapshot_id)
                request = self._build_turn_advice_request(
                    session_id=session.session_id,
                    match_id=session.match_id,
                    generation=session.generation,
                    battle_revision=job.base_battle_revision,
                    facts=facts,
                    selected_three=applied.selected_three,
                )

                source_type = str(result.source_type).strip()
                model = str(result.model).strip()
                # The legacy contract is always ``.v1``/``.v2`` -- this
                # always resolves "v1", routed through the same trusted
                # request/job-contract dispatcher the rich lane uses (Gemini
                # V2 Bundle 6), never from any claim inside the payload.
                parser_version = select_response_parser_version(request.contract_version)
                assert parser_version == "v1"
                body = turn_advice_body_from_dict(result.payload)
                normalized: NormalizedTurnAdviceResult = build_normalized_turn_advice_result(
                    request=request,
                    body=body,
                    request_payload_hash_value=result.request_payload_hash,
                    source_type=source_type,
                    model=model,
                )
                code = validate_turn_advice_result(request, normalized)
                if code is not TurnAdviceResultCode.VALID:
                    raise ValueError(sanitized_reason_for(code))

                recommended = body.recommended_action
                action_type = ActionType(recommended.action_type)
                action_name = recommended.action_name
                opponent_prediction = body.opponent_prediction.summary
                rationale = "; ".join(body.reasons)
                warnings = tuple(body.warnings)
                advice = TurnAdviceSnapshot(
                    turn_advice_id=result.result_id,
                    turn_id=session.current_turn_id,
                    turn_number=facts.turn_number,
                    job_id=job.job_id,
                    input_snapshot_id=facts.turn_facts_id,
                    action_type=action_type,
                    action_name=action_name,
                    opponent_prediction=opponent_prediction,
                    rationale=rationale,
                    is_mock=source_type != "GEMINI",
                    source_type=source_type,
                    model=model,
                    warnings=warnings,
                    response_schema_version=RESPONSE_SCHEMA_VERSION_V1,
                    advice_json=None,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                TurnAdviceSchemaError,
                TurnAdviceParseError,
            ):
                self.repository.audit_result(
                    result, ResultDisposition.INVALID_REJECTED, "INVALID_PAYLOAD"
                )
                self.repository.update_job_status(job.job_id, JobStatus.FAILED)
                return ResultDisposition.INVALID_REJECTED

            self.repository.append_turn_advice(session.session_id, advice)
            session.current_turn_advice_id = advice.turn_advice_id
            session.bump_battle()
            self.repository.save_session(session)
            self.repository.update_job_status(job.job_id, JobStatus.SUCCEEDED)
            self.repository.audit_result(result, ResultDisposition.APPLIED, "BINDING_ACCEPTED")
        return ResultDisposition.APPLIED

    def apply_rich_turn_advice_result(self, result: ResultEnvelope) -> ResultDisposition:
        """Versioned rich-result apply path. Never used for a legacy job.

        The legacy :meth:`apply_turn_advice_result` binds
        ``job.input_snapshot_id`` against ``session.current_reviewed_board_id``
        -- correct for the legacy per-turn-facts flow, but wrong for a rich
        job, whose ``input_snapshot_id`` is a ``ConfirmedTurnState.
        confirmed_state_id``. That field is never touched or repurposed here.

        The discriminator for "this is a rich result" is durable and
        unambiguous: the job's own ``(session_id, match_id, generation)``
        must already have a persisted ``ConfirmedTurnState`` row (see
        :meth:`~maple_next.persistence.turn_state_store.TurnStateStoreMixin.
        match_uses_rich_state_contract`), and ``job.input_snapshot_id`` must
        resolve to an actual ``ConfirmedTurnState`` via
        :meth:`build_rich_turn_advice_transport_request`. Nothing here
        infers rich status from caller-supplied result data.
        """

        with self.repository.transaction():
            job = self._load_result_job_or_audit(result)
            if job is None:
                return ResultDisposition.STALE_REJECTED
            if job.job_type is not JobType.TURN_ADVICE:
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_TYPE_NOT_TURN_ADVICE"
                )
                return ResultDisposition.STALE_REJECTED
            if not self.repository.match_uses_rich_state_contract(
                session_id=job.session_id, match_id=job.match_id, generation=job.generation
            ):
                self.repository.audit_result(
                    result, ResultDisposition.STALE_REJECTED, "JOB_NOT_RICH_STATE_CONTRACT"
                )
                return ResultDisposition.STALE_REJECTED
            if self.repository.has_applied_result(job.job_id):
                self.repository.audit_result(
                    result, ResultDisposition.DUPLICATE_IGNORED, "RESULT_ALREADY_APPLIED"
                )
                return ResultDisposition.DUPLICATE_IGNORED

            session = self.repository.load_active_session()
            latest_job = (
                self.repository.latest_job_by_type(session.session_id, JobType.TURN_ADVICE)
                if session is not None
                else None
            )
            current_identity: TurnIdentity | None = None
            latest_state: ConfirmedTurnState | None = None
            if session is not None and session.current_turn_id is not None:
                turn = self.repository.get_turn(session.current_turn_id)
                current_identity = TurnIdentity(
                    session_id=session.session_id,
                    match_id=session.match_id,
                    generation=session.generation,
                    turn_id=turn.turn_id,
                    turn_number=turn.turn_number,
                    battle_revision=session.battle_revision,
                )
                latest_state = self.repository.get_latest_confirmed_turn_state_for_identity(
                    session_id=session.session_id,
                    match_id=session.match_id,
                    generation=session.generation,
                )

            reason = self._rich_binding_failure_reason(
                session,
                latest_job,
                job,
                result,
                current_identity=current_identity,
                latest_state=latest_state,
            )
            if reason is not None:
                self.repository.audit_result(result, ResultDisposition.STALE_REJECTED, reason)
                return ResultDisposition.STALE_REJECTED

            assert session is not None
            assert session.current_turn_id is not None
            assert latest_state is not None
            applied_audit_suffix = ""
            try:
                rebuilt = self.build_rich_turn_advice_transport_request(job)
                source_type = str(result.source_type).strip()
                model = str(result.model).strip()
                if not source_type:
                    raise ValueError(sanitized_reason_for(TurnAdviceResultCode.SOURCE_INVALID))
                if not model:
                    raise ValueError(sanitized_reason_for(TurnAdviceResultCode.MODEL_INVALID))
                # ``turn_advices.input_snapshot_id`` has a durable FK against
                # the legacy ``reviewed_turn_facts`` table (schema v14) --
                # the rich ``ConfirmedTurnState.confirmed_state_id`` cannot
                # be stored there without a schema migration, which is out
                # of this narrow remediation's scope. The rich identity
                # binding above (job/result/latest-state confirmed_state_id
                # equality) is what actually authorizes this apply; this
                # column remains a legacy cross-reference for the existing
                # reader/export code that already expects it.
                if session.current_reviewed_board_id is None:
                    raise DomainError("REVIEWED_TURN_FACTS_REQUIRED_FOR_ADVICE_RECORD")

                # Gemini V2 Bundle 6: trusted response-parser selection keyed
                # on the rebuilt request's own contract version -- never on
                # any version claim inside ``result.payload`` itself.
                parser_version = select_response_parser_version(rebuilt.contract_version)
                if parser_version == "v2":
                    normalized_payload, prediction_downgraded = (
                        normalize_degradable_opponent_prediction_v2(result.payload)
                    )
                    body_v2 = turn_advice_body_v2_from_dict(normalized_payload)
                    if prediction_downgraded:
                        applied_audit_suffix = f":{PREDICTION_DOWNGRADED_TO_UNKNOWN}"
                    if _payload_had_low_support_specific_action(result.payload):
                        applied_audit_suffix += (
                            f":{NORMALIZED_LOW_SUPPORT_SPECIFIC_ACTION_TO_NULL}"
                        )
                    legality_code = validate_turn_advice_legality_v2(
                        rebuilt, body_v2.recommended_action
                    )
                    if legality_code is not TurnAdviceResultCode.VALID:
                        raise ValueError(sanitized_reason_for(legality_code))
                    semantic_code = validate_turn_advice_v2_semantics(body_v2, request=rebuilt)
                    if semantic_code is not TurnAdviceV2SemanticResultCode.VALID:
                        raise ValueError(sanitized_reason_for_v2_semantics(semantic_code))

                    recommended_v2 = body_v2.recommended_action
                    action_type = ActionType(recommended_v2.action_type)
                    action_name = recommended_v2.action_name
                    opponent_prediction = body_v2.opponent_prediction.primary.summary
                    rationale = "; ".join(body_v2.reasons)
                    warnings = tuple(body_v2.warnings)
                    advice = TurnAdviceSnapshot(
                        turn_advice_id=result.result_id,
                        turn_id=session.current_turn_id,
                        turn_number=rebuilt.identity.turn_number,
                        job_id=job.job_id,
                        input_snapshot_id=session.current_reviewed_board_id,
                        action_type=action_type,
                        action_name=action_name,
                        opponent_prediction=opponent_prediction,
                        rationale=rationale,
                        is_mock=source_type != "GEMINI",
                        source_type=source_type,
                        model=model,
                        warnings=warnings,
                        response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
                        advice_json=canonical_turn_advice_v2_json(body_v2),
                    )
                else:
                    body = turn_advice_body_from_dict(result.payload)
                    normalized = NormalizedTurnAdviceResult(
                        contract_version=rebuilt.contract_version,
                        job_type=rebuilt.job_type,
                        session_id=rebuilt.identity.session_id,
                        match_id=rebuilt.identity.match_id,
                        generation=rebuilt.identity.generation,
                        turn_number=rebuilt.identity.turn_number,
                        battle_revision=rebuilt.identity.battle_revision,
                        reviewed_snapshot_id=rebuilt.reviewed_confirmed_state_id,
                        reviewed_snapshot_hash=rebuilt.reviewed_snapshot_hash,
                        request_payload_hash=result.request_payload_hash,
                        source_type=source_type,
                        model=model,
                        advice=body,
                    )
                    legality_code = validate_turn_advice_legality(rebuilt, normalized)
                    if legality_code is not TurnAdviceResultCode.VALID:
                        raise ValueError(sanitized_reason_for(legality_code))

                    recommended = body.recommended_action
                    action_type = ActionType(recommended.action_type)
                    action_name = recommended.action_name
                    opponent_prediction = body.opponent_prediction.summary
                    rationale = "; ".join(body.reasons)
                    warnings = tuple(body.warnings)
                    advice = TurnAdviceSnapshot(
                        turn_advice_id=result.result_id,
                        turn_id=session.current_turn_id,
                        turn_number=rebuilt.identity.turn_number,
                        job_id=job.job_id,
                        input_snapshot_id=session.current_reviewed_board_id,
                        action_type=action_type,
                        action_name=action_name,
                        opponent_prediction=opponent_prediction,
                        rationale=rationale,
                        is_mock=source_type != "GEMINI",
                        source_type=source_type,
                        model=model,
                        warnings=warnings,
                        response_schema_version=RESPONSE_SCHEMA_VERSION_V1,
                        advice_json=None,
                    )
            except (
                KeyError,
                TypeError,
                ValueError,
                TurnAdviceSchemaError,
                TurnAdviceParseError,
                ProviderReadyGateError,
                DomainError,
            ) as exc:
                self.repository.audit_result(
                    result, ResultDisposition.INVALID_REJECTED, _invalid_payload_reason(exc)
                )
                self.repository.update_job_status(job.job_id, JobStatus.FAILED)
                return ResultDisposition.INVALID_REJECTED

            self.repository.append_turn_advice(session.session_id, advice)
            session.current_turn_advice_id = advice.turn_advice_id
            session.bump_battle()
            self.repository.save_session(session)
            self.repository.update_job_status(job.job_id, JobStatus.SUCCEEDED)
            self.repository.audit_result(
                result, ResultDisposition.APPLIED, f"BINDING_ACCEPTED{applied_audit_suffix}"
            )
        return ResultDisposition.APPLIED

    @staticmethod
    def _rich_binding_failure_reason(
        session: BattleSession | None,
        latest_job: JobEnvelope | None,
        job: JobEnvelope,
        result: ResultEnvelope,
        *,
        current_identity: TurnIdentity | None,
        latest_state: ConfirmedTurnState | None,
    ) -> str | None:
        """Rich-lane analogue of :meth:`_binding_failure_reason`.

        Binds against the durable latest ``ConfirmedTurnState`` instead of
        ``session.current_reviewed_board_id`` -- the only difference from
        the legacy check. Every other binding dimension (job currency,
        contract/command/job-type, session/match/generation, turn/revision,
        expected state, request hash) is preserved exactly.
        """

        checks = (
            (latest_job is not None and latest_job.job_id == job.job_id, "JOB_ID_NOT_CURRENT"),
            (
                job.status in {JobStatus.QUEUED, JobStatus.IN_FLIGHT},
                "JOB_NOT_ACCEPTING_RESULTS",
            ),
            (result.contract_version == job.contract_version, "CONTRACT_VERSION_MISMATCH"),
            (result.command_id == job.command_id, "COMMAND_ID_MISMATCH"),
            (result.job_type is job.job_type, "JOB_TYPE_MISMATCH"),
            (session is not None, "NO_ACTIVE_MATCH"),
            (session is not None and result.session_id == session.session_id, "SESSION_MISMATCH"),
            (session is not None and result.match_id == session.match_id, "MATCH_MISMATCH"),
            (
                session is not None and result.generation == session.generation,
                "GENERATION_MISMATCH",
            ),
            (
                current_identity is not None
                and result.turn_number == job.turn_number
                and result.turn_number == current_identity.turn_number,
                "TURN_MISMATCH",
            ),
            (
                session is not None
                and result.base_battle_revision == session.battle_revision
                and result.base_battle_revision == job.base_battle_revision,
                "BATTLE_REVISION_MISMATCH",
            ),
            (
                session is not None
                and result.expected_state is session.state
                and result.expected_state is job.expected_state,
                "EXPECTED_STATE_MISMATCH",
            ),
            (
                latest_state is not None
                and job.input_snapshot_id == result.input_snapshot_id
                and result.input_snapshot_id == latest_state.confirmed_state_id,
                "INPUT_SNAPSHOT_MISMATCH",
            ),
            (
                current_identity is not None
                and latest_state is not None
                and latest_state.identity == current_identity,
                "CONFIRMED_STATE_NOT_CURRENT_BINDING",
            ),
            (result.request_payload_hash == job.request_payload_hash, "REQUEST_HASH_MISMATCH"),
        )
        for ok, reason in checks:
            if not ok:
                return reason
        return None

    def record_actual_action(
        self,
        *,
        action_type: ActionType,
        action_name: str,
        human_confirmed: bool,
        opponent_action_type: ActionType | None = None,
        opponent_action_name: str | None = None,
        action_order: ActionOrder = ActionOrder.UNKNOWN,
    ) -> RecordedAction:
        """Explicit legacy-only compatibility operation.

        Bundle 1 Battle Record must use :meth:`record_rich_actual_action`,
        whose rich completion arguments cannot be omitted.
        """

        return self._record_actual_action(
            action_type=action_type,
            action_name=action_name,
            human_confirmed=human_confirmed,
            opponent_action_type=opponent_action_type,
            opponent_action_name=opponent_action_name,
            action_order=action_order,
            rich_completion=None,
        )

    def record_rich_actual_action(
        self,
        *,
        action_type: ActionType,
        action_name: str,
        human_confirmed: bool,
        opponent_action_type: ActionType | None,
        opponent_action_name: str | None,
        action_order: ActionOrder,
        completion_identity: TurnIdentity,
        action_result_delta: ActionResultDelta,
        rich_transaction_id: str,
        confirmed_mega_sides: tuple[MegaSide, ...] = (),
    ) -> RecordedAction:
        """Record one mandatory, identity-bound Bundle 1 completion."""

        if (
            completion_identity is None
            or action_result_delta is None
            or rich_transaction_id is None
        ):
            raise DomainError("INCOMPLETE_RICH_ACTION_COMPLETION")
        return self._record_actual_action(
            action_type=action_type,
            action_name=action_name,
            human_confirmed=human_confirmed,
            opponent_action_type=opponent_action_type,
            opponent_action_name=opponent_action_name,
            action_order=action_order,
            rich_completion=(
                completion_identity,
                action_result_delta,
                rich_transaction_id,
            ),
            confirmed_mega_sides=confirmed_mega_sides,
        )

    def _record_actual_action(
        self,
        *,
        action_type: ActionType,
        action_name: str,
        human_confirmed: bool,
        opponent_action_type: ActionType | None,
        opponent_action_name: str | None,
        action_order: ActionOrder,
        rich_completion: tuple[TurnIdentity, ActionResultDelta, str] | None,
        confirmed_mega_sides: tuple[MegaSide, ...] = (),
    ) -> RecordedAction:
        if not human_confirmed:
            raise DomainError("HUMAN_ACTION_CONFIRMATION_REQUIRED")
        if opponent_action_type is None and opponent_action_name is not None:
            raise DomainError("INVALID_UNKNOWN_OPPONENT_ACTION_NAME")
        if opponent_action_type is not None and (
            opponent_action_name is None or not opponent_action_name.strip()
        ):
            raise DomainError("INVALID_KNOWN_OPPONENT_ACTION_NAME")

        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_REVIEWED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            if session.current_reviewed_board_id is None:
                raise DomainError("REVIEWED_TURN_FACTS_REQUIRED")
            if session.current_turn_advice_id is None:
                raise DomainError("CURRENT_TURN_ADVICE_REQUIRED")
            if self.repository.has_recorded_action(session.current_turn_id):
                raise DomainError("ACTION_ALREADY_RECORDED")

            facts = self.repository.get_turn_facts(session.current_reviewed_board_id)
            legal_actions = (
                facts.legal_moves if action_type is ActionType.MOVE else facts.legal_switches
            )
            normalized_name = action_name.strip()
            if normalized_name not in legal_actions:
                raise DomainError("ACTION_OUTSIDE_REVIEWED_LEGAL_ACTIONS")
            normalized_opponent_name = (
                opponent_action_name.strip()
                if opponent_action_type is not None and opponent_action_name is not None
                else None
            )

            try:
                action = RecordedAction(
                    action_id=str(uuid4()),
                    turn_id=session.current_turn_id,
                    turn_number=facts.turn_number,
                    action_type=action_type,
                    action_name=normalized_name,
                    opponent_action_type=opponent_action_type,
                    opponent_action_name=normalized_opponent_name,
                    action_order=action_order,
                )
            except ValueError as exc:
                raise DomainError(f"INVALID_RECORDED_ACTION:{exc}") from exc
            based_on_state: ConfirmedTurnState | None = None
            if rich_completion is not None:
                completion_identity, action_result_delta, rich_transaction_id = rich_completion
                if not rich_transaction_id.strip():
                    raise DomainError("RICH_ACTION_COMPLETION_ID_REQUIRED")
                if completion_identity.turn_id != action.turn_id:
                    raise DomainError("ACTION_COMPLETION_TURN_MISMATCH")
                if action_result_delta.identity != completion_identity:
                    raise DomainError("ACTION_COMPLETION_DELTA_IDENTITY_MISMATCH")
                try:
                    based_on_state = self.repository.get_confirmed_turn_state(
                        action_result_delta.based_on_confirmed_state_id
                    )
                except KeyError as exc:
                    raise DomainError("BASED_ON_CONFIRMED_STATE_NOT_FOUND") from exc
                if based_on_state.identity != completion_identity:
                    raise DomainError("ACTION_COMPLETION_CONFIRMED_STATE_IDENTITY_MISMATCH")

            if confirmed_mega_sides and rich_completion is None:
                raise DomainError("MEGA_CONFIRMATION_REQUIRES_RICH_ACTION_COMPLETION")

            # Prepare the complete match-level actual Mega state before the
            # first durable action write. The target is only the active
            # Pokemon from the already validated human-confirmed state; no
            # Selection/OCR/general-knowledge fallback is permitted.
            prepared_mega_state: MegaBattleState | None = None
            if confirmed_mega_sides:
                if not all(isinstance(side, MegaSide) for side in confirmed_mega_sides):
                    raise DomainError("MEGA_SIDE_TYPE_INVALID")
                if len(set(confirmed_mega_sides)) != len(confirmed_mega_sides):
                    raise DomainError("MEGA_DUPLICATE_SIDE_CONFIRMATION")
                assert based_on_state is not None
                try:
                    prepared_mega_state = self.repository.get_mega_state(session.session_id)
                except (KeyError, ValueError) as exc:
                    raise DomainError("MEGA_STATE_LOAD_FAILED") from exc
                confirmed_at_utc = datetime.now(UTC).isoformat()
                for mega_side in confirmed_mega_sides:
                    side_state = (
                        based_on_state.self_side
                        if mega_side is MegaSide.SELF
                        else based_on_state.opponent_side
                    )
                    active_value = side_state.active.value
                    mega_active_name = (
                        active_value.strip() if isinstance(active_value, str) else ""
                    )
                    if (
                        not side_state.active.is_confirmed
                        or not mega_active_name
                        or mega_active_name == "UNKNOWN"
                    ):
                        raise DomainError(f"MEGA_ACTIVE_NOT_CONFIRMED:{mega_side.value}")
                    try:
                        prepared_mega_state = prepared_mega_state.record_use(
                            side=mega_side,
                            pokemon_name=mega_active_name,
                            current_form=deterministic_mega_form(mega_active_name),
                            confirmed_turn=based_on_state.identity.turn_number,
                            confirmed_at_utc=confirmed_at_utc,
                        )
                    except ValueError as exc:
                        raise DomainError(str(exc)) from exc

            # Every mandatory rich prerequisite above is validated before
            # the first durable write. The writes below still share the
            # same outer transaction for rollback on persistence failures.
            self.repository.append_recorded_action(session.session_id, action)
            if rich_completion is not None:
                self.repository.append_rich_action_completion(
                    transaction_id=rich_transaction_id,
                    identity=completion_identity,
                    own_action_type=action.action_type,
                    own_action_name=action.action_name,
                    opponent_action_type=action.opponent_action_type,
                    opponent_action_name=action.opponent_action_name,
                    action_order=action.action_order,
                    delta=action_result_delta,
                )
                # Result-confirmed HP/status belongs to the Pokemon that was
                # active at Turn start, even if the next captured screen
                # already shows its replacement.  Keep the existing
                # PokemonLocalMemory authority current in this same
                # transaction so faint/alive/remaining and future legal
                # switch derivation cannot lag behind the canonical delta.
                assert based_on_state is not None
                for side_name, previous_side, side_delta in (
                    ("SELF", based_on_state.self_side, action_result_delta.self_side),
                    (
                        "OPPONENT",
                        based_on_state.opponent_side,
                        action_result_delta.opponent_side,
                    ),
                ):
                    active_name = previous_side.active.value
                    if active_name is None or not previous_side.active.is_confirmed:
                        continue
                    self.repository.upsert_pokemon_local_state(
                        session_id=completion_identity.session_id,
                        match_id=completion_identity.match_id,
                        generation=completion_identity.generation,
                        side=side_name,
                        memory=PokemonLocalMemory(
                            pokemon_name=active_name,
                            hp_bucket=_apply_local_memory_delta(
                                previous_side.hp_bucket, side_delta.hp_bucket
                            ),
                            status=_apply_local_memory_delta(
                                previous_side.status, side_delta.status
                            ),
                        ),
                    )
                if prepared_mega_state is not None:
                    self.repository.update_mega_state(
                        session.session_id, prepared_mega_state
                    )
            session.state = BattleState.TURN_RECORDED
            session.bump_battle()
            self.repository.save_session(session)
        return action

    def next_turn_with_action_result(
        self,
        *,
        confirmed_state: ConfirmedTurnState,
        delta: ActionResultDelta,
        draft_id: str,
        derived_at_utc: str,
    ) -> BattleTurn:
        """Derive first, then atomically advance and persist the exact draft."""

        # Phase 1: read and purely derive while Turn N remains current. No
        # BEGIN IMMEDIATE is entered until the complete draft validates.
        session_before = self._require_session(BattleState.TURN_RECORDED)
        if session_before.current_turn_id is None:
            raise DomainError("CURRENT_TURN_REQUIRED")
        current_turn_before = self.repository.get_turn(session_before.current_turn_id)
        if confirmed_state.identity.turn_id != current_turn_before.turn_id:
            raise DomainError("CONFIRMED_STATE_NOT_CURRENT_BINDING")
        latest_state_before = self.repository.get_latest_confirmed_turn_state_for_identity(
            session_id=session_before.session_id,
            match_id=session_before.match_id,
            generation=session_before.generation,
        )
        if latest_state_before != confirmed_state:
            raise DomainError("CONFIRMED_STATE_NOT_CURRENT_BINDING")
        deltas_before = self.repository.list_action_result_deltas_based_on(
            confirmed_state.confirmed_state_id
        )
        if not deltas_before or deltas_before[-1] != delta:
            raise DomainError("ACTION_RESULT_DELTA_NOT_CURRENT_BINDING")

        turn = BattleTurn(
            turn_id=str(uuid4()),
            turn_number=current_turn_before.turn_number + 1,
        )
        next_identity = TurnIdentity(
            session_id=session_before.session_id,
            match_id=session_before.match_id,
            generation=session_before.generation,
            turn_id=turn.turn_id,
            turn_number=turn.turn_number,
            battle_revision=session_before.battle_revision + 1,
        )
        draft = derive_next_turn_state_draft(
            confirmed_state,
            delta,
            draft_id=draft_id,
            next_identity=next_identity,
            derived_at_utc=derived_at_utc,
        )
        validate_turn_state_full_chain(confirmed_state, delta, draft)

        # Phase 2: re-read every current binding after BEGIN. A stale Phase-1
        # snapshot fails closed; the draft is never recomputed in-transaction.
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_RECORDED)
            if (
                session.session_id != session_before.session_id
                or session.match_id != session_before.match_id
                or session.generation != session_before.generation
                or session.current_turn_id != session_before.current_turn_id
                or session.battle_revision != session_before.battle_revision
            ):
                raise DomainError("NEXT_TURN_PHASE1_BINDING_STALE")
            current_turn = self.repository.get_turn(session.current_turn_id or "")
            if current_turn != current_turn_before:
                raise DomainError("NEXT_TURN_PHASE1_BINDING_STALE")
            latest_state = self.repository.get_latest_confirmed_turn_state_for_identity(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
            )
            if latest_state != confirmed_state:
                raise DomainError("NEXT_TURN_PHASE1_BINDING_STALE")
            deltas = self.repository.list_action_result_deltas_based_on(
                confirmed_state.confirmed_state_id
            )
            if not deltas or deltas[-1] != delta:
                raise DomainError("NEXT_TURN_PHASE1_BINDING_STALE")
            self.repository.append_turn(session.session_id, turn)
            self._set_pending_turn(session, turn)
            if session.battle_revision != next_identity.battle_revision:
                raise DomainError("NEXT_TURN_REVISION_MISMATCH")
            self.repository.save_session(session)
            self.repository.upsert_next_turn_state_draft(draft)
        return turn

    def next_turn(self) -> BattleTurn:
        with self.repository.transaction():
            session = self._require_session(BattleState.TURN_RECORDED)
            if session.current_turn_id is None:
                raise DomainError("CURRENT_TURN_REQUIRED")
            current_turn = self.repository.get_turn(session.current_turn_id)
            turn = BattleTurn(
                turn_id=str(uuid4()),
                turn_number=current_turn.turn_number + 1,
            )
            self.repository.append_turn(session.session_id, turn)
            self._set_pending_turn(session, turn)
            self.repository.save_session(session)
        return turn

    def update_metadata(self) -> BattleSession:
        with self.repository.transaction():
            session = self._require_active_session()
            session.bump_metadata()
            self.repository.save_session(session)
        return session

    def recover_after_restart(self) -> None:
        with self.repository.transaction():
            self.repository.recover_unfinished_jobs()

    def _set_pending_turn(self, session: BattleSession, turn: BattleTurn) -> None:
        session.current_turn_id = turn.turn_id
        session.current_observation_id = None
        session.current_reviewed_board_id = None
        session.current_turn_advice_id = None
        session.state = BattleState.TURN_CAPTURE_PENDING
        session.bump_battle()

    # -- Bundle 2 (Gemini V2): explicit legal-switch confirmation ------------

    def _current_legal_switch_binding(
        self,
    ) -> tuple[TurnIdentity, AppliedSelectionSnapshot, ConfirmedTurnState]:
        """Shared read-only load of the identity/selection/state a legal-switch
        candidate derivation or confirmation must bind to. Fails closed if
        the current Turn has no matching binding for any of the three."""

        session = self._require_session(BattleState.TURN_REVIEWED)
        if session.current_turn_id is None:
            raise DomainError("CURRENT_TURN_REQUIRED")
        if session.current_applied_selection_id is None:
            raise DomainError("APPLIED_SELECTION_REQUIRED")
        turn = self.repository.get_turn(session.current_turn_id)
        identity = TurnIdentity(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            turn_id=turn.turn_id,
            turn_number=turn.turn_number,
            battle_revision=session.battle_revision,
        )
        latest_state = self.repository.get_latest_confirmed_turn_state_for_identity(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
        )
        if latest_state is None or latest_state.identity != identity:
            raise DomainError("CONFIRMED_STATE_NOT_CURRENT_BINDING")
        applied = self.repository.get_applied_selection(session.current_applied_selection_id)
        return identity, applied, latest_state

    def _self_local_memory_by_name(
        self, identity: TurnIdentity, applied: AppliedSelectionSnapshot
    ) -> dict[str, PokemonLocalMemory]:
        memory_by_name: dict[str, PokemonLocalMemory] = {}
        for name in applied.selected_three:
            memory = self.repository.get_pokemon_local_state(
                session_id=identity.session_id,
                match_id=identity.match_id,
                generation=identity.generation,
                side="SELF",
                pokemon_name=name,
            )
            if memory is not None:
                memory_by_name[name] = memory
        return memory_by_name

    def _confirmed_fainted_members(
        self, identity: TurnIdentity, applied: AppliedSelectionSnapshot
    ) -> frozenset[str]:
        """R3-C: the same match-local HP=0 facts used to build/validate a
        confirmation, recomputed fresh so the provider-ready gate can
        independently re-derive contextual legality rather than trusting a
        stored confirmation's contents."""

        memory_by_name = self._self_local_memory_by_name(identity, applied)
        return frozenset(
            name for name, memory in memory_by_name.items() if is_confirmed_fainted(memory)
        )

    def derive_legal_switch_candidates_for_current_turn(self) -> tuple[str, ...]:
        """Operator-facing prefill aid for the current Turn. Read-only, never
        itself a confirmation -- see :func:`maple_next.domain.legal_switches.
        derive_legal_switch_candidates`."""

        identity, applied, latest_state = self._current_legal_switch_binding()
        self_active = latest_state.self_side.active
        if not self_active.is_confirmed or not self_active.value:
            raise DomainError("SELF_ACTIVE_UNKNOWN")
        memory_by_name = self._self_local_memory_by_name(identity, applied)
        try:
            return derive_legal_switch_candidates(
                applied=applied,
                current_active_name=self_active.value,
                local_memory_by_name=memory_by_name,
            )
        except LegalSwitchError as exc:
            raise DomainError(f"LEGAL_SWITCH_CANDIDATES_UNAVAILABLE:{exc}") from exc

    def derive_legal_switch_candidates_for_active(
        self, candidate_active_name: str
    ) -> tuple[str, ...]:
        """Pre-confirmation prefill aid: candidates for a NOT-YET-confirmed
        active Pokemon, usable while still ``TURN_CAPTURE_PENDING`` (before
        any ``ConfirmedTurnState`` exists for this Turn). Read-only, never
        itself a confirmation -- the operator's later CONFIRM TURN FACTS
        click is what turns whatever is displayed from this into a real
        ``LegalSwitchConfirmation``, not this call.

        Deliberately does not require ``_current_legal_switch_binding()``'s
        ``TURN_REVIEWED`` state: the UI needs to show/refresh this prefill
        as the operator types the active box, before Turn facts are
        confirmed at all.
        """

        session = self._require_active_session()
        if session.current_turn_id is None:
            raise DomainError("CURRENT_TURN_REQUIRED")
        if session.current_applied_selection_id is None:
            raise DomainError("APPLIED_SELECTION_REQUIRED")
        turn = self.repository.get_turn(session.current_turn_id)
        identity = TurnIdentity(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            turn_id=turn.turn_id,
            turn_number=turn.turn_number,
            battle_revision=session.battle_revision,
        )
        applied = self.repository.get_applied_selection(session.current_applied_selection_id)
        name = candidate_active_name.strip()
        if not name or name not in applied.selected_three:
            raise DomainError("SELF_ACTIVE_UNKNOWN")
        memory_by_name = self._self_local_memory_by_name(identity, applied)
        try:
            return derive_legal_switch_candidates(
                applied=applied,
                current_active_name=name,
                local_memory_by_name=memory_by_name,
            )
        except LegalSwitchError as exc:
            raise DomainError(f"LEGAL_SWITCH_CANDIDATES_UNAVAILABLE:{exc}") from exc

    def confirm_legal_switches(
        self,
        *,
        legal_switches: tuple[str, ...],
        status: LegalSwitchStatus,
        human_confirmed: bool,
    ) -> LegalSwitchConfirmation:
        """Final, human-confirmed legal-switch set for the current Turn binding.

        The derived candidate list is only ever a prefill aid -- it is not
        consulted here. Explicit human confirmation is required and
        sufficient on its own, exactly like every other Bundle A/1
        confirmation. Fails closed (``DomainError``) on any hard-invalidity
        violation (current active included, a member outside the applied
        ``selected_three``, or a member with match-local confirmed HP = 0)
        without persisting anything.
        """

        if not human_confirmed:
            raise DomainError("HUMAN_ACTION_CONFIRMATION_REQUIRED")
        with self.repository.transaction():
            identity, applied, latest_state = self._current_legal_switch_binding()
            self_active = latest_state.self_side.active
            if (
                not self_active.is_confirmed
                or not self_active.value
                or self_active.value == "UNKNOWN"
            ):
                raise DomainError("SELF_ACTIVE_UNKNOWN")
            memory_by_name = self._self_local_memory_by_name(identity, applied)
            try:
                confirmation = build_legal_switch_confirmation(
                    confirmation_id=str(uuid4()),
                    identity=identity,
                    based_on_confirmed_state_id=latest_state.confirmed_state_id,
                    applied=applied,
                    current_active_name=self_active.value,
                    local_memory_by_name=memory_by_name,
                    legal_switches=legal_switches,
                    status=status,
                    confirmation=ConfirmationMeta(
                        confirmed_by_human=True,
                        confirmed_at_utc=datetime.now(UTC).isoformat(),
                        provenance="HUMAN_INPUT",
                    ),
                )
            except (LegalSwitchError, ValueError) as exc:
                raise DomainError(f"LEGAL_SWITCH_CONFIRMATION_REJECTED:{exc}") from exc
            self.repository.upsert_legal_switch_confirmation(confirmation)
            return confirmation

    def _load_result_job_or_audit(self, result: ResultEnvelope) -> JobEnvelope | None:
        try:
            return self.repository.get_job(result.job_id)
        except KeyError:
            self.repository.audit_result(
                result, ResultDisposition.STALE_REJECTED, "JOB_ID_MISMATCH"
            )
            return None

    @staticmethod
    def _guard_provider_request(latest_job: JobEnvelope | None) -> None:
        if latest_job is not None and latest_job.status in {
            JobStatus.QUEUED,
            JobStatus.IN_FLIGHT,
        }:
            raise DomainError("PROVIDER_REQUEST_PENDING")
        if latest_job is not None and latest_job.status is JobStatus.DELIVERY_UNKNOWN:
            raise DomainError("PROVIDER_DELIVERY_UNKNOWN")

    def _require_active_session(self) -> BattleSession:
        session = self.repository.load_active_session()
        if session is None:
            raise DomainError("NO_ACTIVE_MATCH")
        return session

    def _require_session(self, expected_state: BattleState) -> BattleSession:
        session = self._require_active_session()
        if session.state is not expected_state:
            raise DomainError(f"EXPECTED_{expected_state.value}")
        return session

    @staticmethod
    def payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _binding_failure_reason(
        session: BattleSession | None,
        latest_job: JobEnvelope | None,
        job: JobEnvelope,
        result: ResultEnvelope,
        *,
        current_input_snapshot_id: str | None,
        current_turn_number: int | None,
    ) -> str | None:
        checks = (
            (latest_job is not None and latest_job.job_id == job.job_id, "JOB_ID_NOT_CURRENT"),
            (
                job.status in {JobStatus.QUEUED, JobStatus.IN_FLIGHT},
                "JOB_NOT_ACCEPTING_RESULTS",
            ),
            (result.contract_version == job.contract_version, "CONTRACT_VERSION_MISMATCH"),
            (result.command_id == job.command_id, "COMMAND_ID_MISMATCH"),
            (result.job_type is job.job_type, "JOB_TYPE_MISMATCH"),
            (session is not None, "NO_ACTIVE_MATCH"),
            (session is not None and result.session_id == session.session_id, "SESSION_MISMATCH"),
            (session is not None and result.match_id == session.match_id, "MATCH_MISMATCH"),
            (
                session is not None and result.generation == session.generation,
                "GENERATION_MISMATCH",
            ),
            (
                result.turn_number == job.turn_number
                and result.turn_number == current_turn_number,
                "TURN_MISMATCH",
            ),
            (
                session is not None
                and result.base_battle_revision == session.battle_revision
                and result.base_battle_revision == job.base_battle_revision,
                "BATTLE_REVISION_MISMATCH",
            ),
            (
                session is not None
                and result.expected_state is session.state
                and result.expected_state is job.expected_state,
                "EXPECTED_STATE_MISMATCH",
            ),
            (
                result.input_snapshot_id == current_input_snapshot_id
                and result.input_snapshot_id == job.input_snapshot_id,
                "INPUT_SNAPSHOT_MISMATCH",
            ),
            (
                result.request_payload_hash == job.request_payload_hash,
                "PAYLOAD_HASH_MISMATCH",
            ),
        )
        for passed, reason in checks:
            if not passed:
                return reason
        return None
