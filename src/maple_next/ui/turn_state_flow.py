"""Issue #31 Bundle C: thin UI-facing bridge to the accepted Bundle A/B contracts.

Nothing in this module invents new domain semantics. It only:

- builds :class:`~maple_next.domain.turn_state.ConfirmedTurnState`,
  :class:`~maple_next.domain.turn_state.ActionResultDelta`, and
  :class:`~maple_next.domain.turn_state.ConfirmedLegalActionSelection`
  records from already-validated ``SideState``/``SideDelta``/``Known``/
  ``FieldDelta`` values the UI layer constructed, using the accepted
  dataclass constructors (which themselves fail closed on invalid input),
  and persists them through the existing repository primitives in
  :mod:`maple_next.persistence.turn_state_store`;
- derives ``NextTurnStateDraft`` using the accepted pure
  :func:`~maple_next.domain.turn_state.derive_next_turn_state_draft`
  function, never a bespoke reimplementation;
- dispatches a rich-state Turn Advice request through the accepted
  :meth:`~maple_next.application.service.BattleApplication.request_rich_turn_advice`
  / ``build_rich_turn_advice_transport_request`` / ``apply_rich_turn_advice_result``
  application API, mirroring :class:`maple_next.ui.gemini_turn_advice.
  GeminiTurnAdviceAdapter`'s human-trusted, one-attempt, fake/injected-
  transport-only architecture for the legacy Turn Advice path.

This module never accepts/computes a ``DispatchDecision`` itself, never talks
to a real network, and never auto-promotes a draft or an OCR/prefill
candidate into a confirmed fact -- every write here requires an explicit,
already-validated human confirmation supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypeVar
from uuid import NAMESPACE_URL, uuid4, uuid5
from weakref import ReferenceType, ref

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.battle_events import (
    STAGE_EVENT_PRESETS,
    STAGE_EVENT_PRESETS_BY_KEY,
    STAGE_FIELD_NAMES,
    StageEventPreset,
    apply_stage_event,
)
from maple_next.domain.enums import ActionType, ResultDisposition
from maple_next.domain.legal_switches import (
    LegalSwitchConfirmation,
    LegalSwitchError,
    LegalSwitchStatus,
    derive_legal_switch_candidates,
    is_confirmed_fainted,
)
from maple_next.domain.opponent_intel import (
    MatchOpponentFacts,
    possible_abilities_for_species,
    species_has_entry_relevant_ability,
)
from maple_next.domain.species_ability_catalog import (
    SpeciesCatalogCoverageError,
    canonical_species_ability_catalog,
)
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    Known,
    NextTurnStateDraft,
    OpponentEntryEvent,
    PokemonLocalMemory,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
    TurnStateError,
    confirm_legal_action_selection,
)
from maple_next.domain.turn_state_projection import (
    GateDenialReason,
    ProviderReadyGateResult,
    evaluate_provider_ready_gate,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderConfigError,
    SanitizedProviderResult,
)
from maple_next.providers.turn_advice_rich_state import RichStateTurnAdviceRequest
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
    TurnProviderTransport,
)
from maple_next.ui.controller import OperatorView
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.gemini_turn_advice import (
    FAKE_TURN_MODEL,
    GeminiTurnAdviceAdapter,
    describe_gemini_turn_failure,
    sanitize_gemini_turn_failure,
)
from maple_next.ui.match_controller import MatchFlowController
from maple_next.workers.contracts.models import ResultEnvelope
from maple_next.workers.turn_advice_worker import TurnAdviceDispatch


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _confirmation_meta(provenance: str = "HUMAN_INPUT") -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True,
        confirmed_at_utc=_now_iso(),
        provenance=provenance,
    )


_HUMAN_INPUT_CHAIN = (ProvenanceStep.HUMAN_INPUT,)
_CARRY_FORWARD_CHAIN = (ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,)


def _stage_value(known: Known[int]) -> int:
    """0 for an UNKNOWN stage -- stages are always CONFIRMED once Turn 1
    initializes them; this default only guards a genuinely missing value."""

    return known.value if known.is_confirmed and known.value is not None else 0


_MemoryT = TypeVar("_MemoryT")


def _known_to_restoring_field_delta(known: Known[_MemoryT]) -> FieldDelta[_MemoryT]:
    """A per-Pokemon memory value becomes a CHANGED delta carrying forward
    provenance, or UNKNOWN when no memory exists yet for that Pokemon."""

    if known.is_confirmed and known.value is not None:
        return FieldDelta.changed(known.value, provenance_chain=_CARRY_FORWARD_CHAIN)
    return FieldDelta.unknown()


@dataclass(frozen=True, slots=True)
class TurnStateSummaryView:
    """Read-only projection of the Bundle A/B rich-state records for one turn.

    Every field is either ``None`` (nothing persisted yet for this identity)
    or the exact accepted domain object -- this view performs no coercion of
    UNKNOWN/NONE and never infers a value from an absent record.
    """

    identity: TurnIdentity | None
    confirmed_state: ConfirmedTurnState | None
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...]
    latest_delta: ActionResultDelta | None
    open_draft: NextTurnStateDraft | None
    pending_opponent_entry_event: OpponentEntryEvent | None
    provider_ready: bool
    provider_ready_denial_reasons: tuple[str, ...]
    #: Bundle 2 (Gemini V2). ``None`` means NOT_CAPTURED_OR_UNRESOLVED for the
    #: current binding -- never inferred from an empty candidate list.
    legal_switch_confirmation: LegalSwitchConfirmation | None = None
    #: Operator-facing prefill aid derived from the current binding. Never
    #: itself a confirmation.
    legal_switch_candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RichTurnAdviceGeminiStatusView:
    """Sanitized UI-only state for one human-triggered rich-state send."""

    status: str
    source_type: str
    model: str
    sanitized_failure: str


class _DispatchLike(Protocol):
    def start(self) -> None: ...


class GeminiRichTurnAdviceAdapter:
    """Rich-state counterpart of :class:`GeminiTurnAdviceAdapter`.

    Same human-trusted, one-attempt, off-UI-thread dispatch architecture,
    but built on :meth:`BattleApplication.request_rich_turn_advice` /
    :meth:`BattleApplication.build_rich_turn_advice_transport_request` /
    :meth:`BattleApplication.apply_rich_turn_advice_result` instead of the
    legacy per-turn-facts methods. Shares the same durable
    ``turn_advice_attempt_ledger``/``JobType.TURN_ADVICE`` lane, so a rich
    request and a legacy request for the same session still bind to the
    same one-attempt slot -- this adapter never adds a second ledger.
    """

    def __init__(
        self,
        transport: TurnProviderTransport,
        load_config: Callable[[], ProviderConfig] | None = None,
        *,
        dispatch_factory: Callable[..., _DispatchLike] = TurnAdviceDispatch,
    ) -> None:
        self._transport = transport
        self._load_config = load_config or (
            lambda: ProviderConfig(
                api_key="unused-fake-transport-key",
                model=FAKE_TURN_MODEL,
            )
        )
        self._dispatch_factory = dispatch_factory
        self.dispatch_count = 0
        self._active_dispatch: _DispatchLike | None = None
        self._in_flight = False
        self.last_job_id: str | None = None
        self.last_model: str | None = None
        self.last_source_type: str | None = None
        self.last_disposition: ResultDisposition | None = None
        self.last_failure_reason: str | None = None

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    @property
    def uses_injected_transport(self) -> bool:
        return isinstance(self._transport, FakeTurnAdviceTransport)

    def enqueue_response(self, response: SanitizedProviderResult | Exception) -> None:
        if not self.uses_injected_transport:
            raise RuntimeError("TURN_PROVIDER_DOES_NOT_ACCEPT_INJECTED_RESPONSE")
        self._transport.responses.append(response)  # type: ignore[attr-defined]

    def send(
        self,
        application: BattleApplication,
        *,
        on_applied: Callable[[ResultDisposition], None],
        on_failed: Callable[[str], None],
    ) -> None:
        if self._in_flight or self._active_dispatch is not None:
            on_failed("GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT")
            return

        self.last_model = None
        self.last_source_type = None
        self.last_disposition = None
        self.last_failure_reason = None

        try:
            config = self._load_config()
        except ProviderConfigError as exc:
            reason = sanitize_gemini_turn_failure(str(exc))
            self.last_failure_reason = reason
            on_failed(reason)
            return

        try:
            job = application.request_rich_turn_advice(f"gemini-rich-turn-ui-{uuid4()}")
        except DomainError as exc:
            reason = sanitize_gemini_turn_failure(str(exc))
            self.last_failure_reason = reason
            on_failed(reason)
            return
        self.last_job_id = job.job_id

        try:
            request: RichStateTurnAdviceRequest = (
                application.build_rich_turn_advice_transport_request(job)
            )
            application.mark_turn_advice_dispatched(job.job_id)
        except DomainError:
            application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_DISPATCH_BLOCKED")
            self.last_failure_reason = "GEMINI_TURN_DISPATCH_BLOCKED"
            on_failed("GEMINI_TURN_DISPATCH_BLOCKED")
            return

        def handle_succeeded(result: SanitizedProviderResult) -> None:
            self._in_flight = False
            payload = result.payload if isinstance(result.payload, dict) else {}
            sanitized_result = SanitizedProviderResult(
                payload=payload,
                source_type=str(result.source_type or "UNKNOWN"),
                model=str(result.model or "UNKNOWN"),
            )
            self.last_model = sanitized_result.model
            self.last_source_type = sanitized_result.source_type
            envelope = ResultEnvelope(
                contract_version=job.contract_version,
                result_id=str(uuid4()),
                job_id=job.job_id,
                command_id=job.command_id,
                job_type=job.job_type,
                session_id=job.session_id,
                match_id=job.match_id,
                generation=job.generation,
                turn_number=job.turn_number,
                base_battle_revision=job.base_battle_revision,
                expected_state=job.expected_state,
                input_snapshot_id=job.input_snapshot_id,
                request_payload_hash=job.request_payload_hash,
                payload=sanitized_result.payload,
                source_type=sanitized_result.source_type,
                model=sanitized_result.model,
            )
            disposition = application.apply_rich_turn_advice_result(envelope)
            self.last_disposition = disposition
            if disposition is ResultDisposition.APPLIED:
                pass
            elif disposition is ResultDisposition.STALE_REJECTED:
                application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_RESULT_STALE")
            else:
                application.fail_turn_advice_job(job.job_id, "GEMINI_TURN_RESULT_INVALID")
            on_applied(disposition)

        def handle_failed(reason: str) -> None:
            self._in_flight = False
            sanitized = sanitize_gemini_turn_failure(reason)
            self.last_failure_reason = sanitized
            application.fail_turn_advice_job(job.job_id, sanitized)
            on_failed(sanitized)

        self.dispatch_count += 1
        self._in_flight = True
        dispatch: _DispatchLike = self._dispatch_factory(
            self._transport,
            request,
            config,
            on_succeeded=handle_succeeded,
            on_failed=handle_failed,
        )
        self._active_dispatch = dispatch
        add_finished_callback = getattr(dispatch, "add_finished_callback", None)
        if callable(add_finished_callback):
            has_finished_callback = True
            dispatch_ref = ref(dispatch)
            add_finished_callback(partial(self._release_dispatch, dispatch_ref))
        else:
            has_finished_callback = False
        dispatch.start()
        if not has_finished_callback and not self._in_flight:
            # Same-thread test doubles complete inside ``start`` and have no
            # QThread lifecycle.  Their terminal callback has already run.
            self._active_dispatch = None

    def _release_dispatch(
        self, dispatch_ref: ReferenceType[_DispatchLike]
    ) -> None:
        """Release only the dispatch whose worker thread just terminated."""

        dispatch = dispatch_ref()
        if dispatch is not None and self._active_dispatch is dispatch:
            self._active_dispatch = None


_RICH_GATE_MESSAGES: dict[str, str] = {
    GateDenialReason.STATE_NOT_HUMAN_CONFIRMED.value: "現在stateが人間により確認されていません。",
    GateDenialReason.IDENTITY_MISMATCH.value: "確認済みstateが現在のTurn identityと一致しません。",
    GateDenialReason.STALE_CONFIRMED_STATE.value: "確認済みstateが最新ではありません。",
    GateDenialReason.NEWER_OPEN_DRAFT_EXISTS.value: "より新しい未確定draftが存在します。",
    GateDenialReason.SELF_ACTIVE_UNKNOWN.value: "自分のactiveが未確認です。",
    GateDenialReason.OPPONENT_ACTIVE_UNKNOWN.value: "相手のactiveが未確認です。",
    GateDenialReason.SELF_ACTIVE_LITERAL_UNKNOWN_VALUE.value: (
        "自分のactiveがUNKNOWNのままです。"
    ),
    GateDenialReason.OPPONENT_ACTIVE_LITERAL_UNKNOWN_VALUE.value: (
        "相手のactiveがUNKNOWNのままです。"
    ),
    GateDenialReason.LEGAL_ACTIONS_EMPTY.value: "確定済みの合法行動がありません。",
    GateDenialReason.LEGAL_ACTIONS_NOT_CONFIRMED.value: "合法行動が確定されていません。",
    GateDenialReason.LEGAL_ACTIONS_IDENTITY_MISMATCH.value: (
        "合法行動が現在のTurn identityと一致しません。"
    ),
}

class TurnStateFlowController(MatchFlowController):
    """Adds Bundle A/B rich-state confirmation, delta, draft, and send flows."""

    def __init__(
        self,
        application: MatchApplication,
        repository: SQLiteRepository,
        mock_adapter: MockSelectionAdviceAdapter,
        mock_turn_adapter: MockTurnAdviceAdapter | None = None,
        gemini_adapter: GeminiSelectionAdviceAdapter | None = None,
        turn_gemini_adapter: GeminiTurnAdviceAdapter | None = None,
        rich_turn_gemini_adapter: GeminiRichTurnAdviceAdapter | None = None,
    ) -> None:
        super().__init__(
            application,
            repository,
            mock_adapter,
            mock_turn_adapter,
            gemini_adapter,
            turn_gemini_adapter,
        )
        self._rich_turn_gemini_adapter = rich_turn_gemini_adapter

    # -- Battle Record v5 match-scoped opponent ability memory ---------------

    def opponent_ability_candidates(self, species: str) -> tuple[str, ...]:
        """Species possibilities plus explicit unresolved choice, offline."""

        try:
            possible = possible_abilities_for_species(species)
        except SpeciesCatalogCoverageError:
            return ()
        return (*possible, "不明") if possible else ()

    def ocr_opponent_entry_ability_candidates(self, species: str) -> tuple[str, ...]:
        """Project an immediate, read-only prompt for a fresh OCR identity.

        This does not create or consume an entry event and never confirms an
        ability.  It only exposes the same legal candidate set while the
        current Turn is still awaiting human fact confirmation.  Durable
        event creation remains exclusively in ``confirm_turn_facts``.
        """

        identity = self._safe_current_identity()
        if identity is None:
            return ()
        candidates = self.opponent_ability_candidates(species)
        if len(candidates) <= 1:
            return ()
        try:
            entry_relevant = species_has_entry_relevant_ability(species)
            opponent_entity_id = self.opponent_entity_id_for_species(species)
        except SpeciesCatalogCoverageError:
            return ()
        if not entry_relevant:
            return ()

        events = self._repository.list_opponent_entry_events(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        if any(event.turn_id == identity.turn_id for event in events):
            return ()

        previous_state = self._repository.get_latest_confirmed_turn_state_for_identity(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        if previous_state is not None:
            if previous_state.identity.turn_number >= identity.turn_number:
                return ()
            previous_active = previous_state.opponent_side.active
            if (
                previous_active.is_confirmed
                and previous_active.value is not None
                and self._same_opponent_species(previous_active.value, species)
            ):
                return ()

        remembered = self._repository.get_opponent_ability_memory(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
            opponent_entity_id=opponent_entity_id,
        )
        return () if remembered is not None else candidates

    @staticmethod
    def opponent_entity_id_for_species(species: str) -> str:
        """Stable match-local entity key derived from the canonical species ID."""

        record = canonical_species_ability_catalog().resolve_species(species)
        return f"opponent-species:{record.species_id}"

    def opponent_ability_for_entity(self, opponent_entity_id: str) -> str | None:
        identity = self._safe_current_identity()
        if identity is None:
            return None
        return self._repository.get_opponent_ability_memory(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
            opponent_entity_id=opponent_entity_id,
        )

    @staticmethod
    def _same_opponent_species(left: str, right: str) -> bool:
        """Compare confirmed species by canonical entity, with exact fallback.

        The fallback supports explicitly recorded species outside the local
        catalog without ever merging two different spellings or inventing an
        identity for them.
        """

        left_name = left.strip()
        right_name = right.strip()
        if not left_name or not right_name:
            return False
        catalog = canonical_species_ability_catalog()
        try:
            left_id = catalog.resolve_species(left_name).species_id
            right_id = catalog.resolve_species(right_name).species_id
        except SpeciesCatalogCoverageError:
            return left_name == right_name
        return left_id == right_id

    def opponent_match_facts(
        self, species: str, *, remembered_ability: str | None = None
    ) -> MatchOpponentFacts:
        """Confirmed facts for one current opponent entity in this exact match.

        Human-recorded opponent MOVE names are joined to the confirmed state
        for the same ``turn_id``. Exact session/match/generation filtering and
        species/entity comparison prevent observations leaking across an
        opponent, match, session, or generation. There is deliberately no
        inference from advice, notes, deltas, OCR, or population metadata.
        """

        identity = self._safe_current_identity()
        species_name = species.strip()
        if identity is None or not species_name:
            return MatchOpponentFacts(ability=remembered_ability)
        states = self._repository.list_confirmed_turn_states_for_match(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        if not states:
            return MatchOpponentFacts(ability=remembered_ability)
        latest_active = states[-1].opponent_side.active
        if (
            not latest_active.is_confirmed
            or latest_active.value is None
            or not self._same_opponent_species(latest_active.value, species_name)
        ):
            return MatchOpponentFacts(ability=remembered_ability)

        state_by_turn_id: dict[str, ConfirmedTurnState] = {}
        for state in states:
            # Ordered oldest -> newest, so a same-Turn human correction wins.
            state_by_turn_id[state.identity.turn_id] = state

        moves: list[str] = []
        for action in self._repository.list_recorded_actions(identity.session_id):
            if action.opponent_action_type is not ActionType.MOVE:
                continue
            action_state = state_by_turn_id.get(action.turn_id)
            if action_state is None:
                continue
            active = action_state.opponent_side.active
            if (
                not active.is_confirmed
                or active.value is None
                or not self._same_opponent_species(active.value, species_name)
            ):
                continue
            assert action.opponent_action_name is not None
            move = action.opponent_action_name.strip()
            if move and move not in moves:
                moves.append(move)
        return MatchOpponentFacts(ability=remembered_ability, moves=tuple(moves))

    def rich_turn_advice_is_injected(self) -> bool:
        """True only for the fake/injected transport authorized in v5."""

        return (
            self._rich_turn_gemini_adapter is not None
            and self._rich_turn_gemini_adapter.uses_injected_transport
        )

    def confirm_opponent_ability(
        self,
        *,
        opponent_entity_id: str,
        species: str,
        ability: str,
        entry_event_id: str | None = None,
    ) -> str | None:
        """Remember a human answer for this match/entity.

        ``不明`` remains unresolved (no persistence row), so it can never be
        mistaken for a confirmed ability and may be corrected later.
        """

        identity = self._safe_current_identity()
        if identity is None:
            raise TurnStateError("NO_ACTIVE_TURN_IDENTITY")
        normalized = ability.strip()
        allowed = self.opponent_ability_candidates(species)
        if normalized not in allowed:
            raise ValueError("ability is not a possible human choice for this species")
        with self._repository.transaction():
            if entry_event_id is not None:
                pending = self._repository.get_pending_opponent_entry_event(
                    session_id=identity.session_id,
                    match_id=identity.match_id,
                    generation=identity.generation,
                )
                if pending is None or pending.event_id != entry_event_id:
                    raise TurnStateError("OPPONENT_ENTRY_EVENT_STALE")
                if pending.opponent_entity_id != opponent_entity_id:
                    raise TurnStateError("OPPONENT_ENTRY_ENTITY_MISMATCH")
            self._repository.set_opponent_ability_memory(
                session_id=identity.session_id,
                match_id=identity.match_id,
                generation=identity.generation,
                opponent_entity_id=opponent_entity_id,
                species=species,
                ability=None if normalized == "不明" else normalized,
            )
            if entry_event_id is not None:
                self._repository.mark_opponent_entry_event_handled(
                    event_id=entry_event_id,
                    handled_at_utc=_now_iso(),
                )
        return None if normalized == "不明" else normalized

    def handle_opponent_entry_event(self, event_id: str) -> None:
        """Dismiss a nonqualifying/unresolved genuine event exactly once."""

        identity = self._safe_current_identity()
        if identity is None:
            raise TurnStateError("NO_ACTIVE_TURN_IDENTITY")
        with self._repository.transaction():
            pending = self._repository.get_pending_opponent_entry_event(
                session_id=identity.session_id,
                match_id=identity.match_id,
                generation=identity.generation,
            )
            if pending is None or pending.event_id != event_id:
                raise TurnStateError("OPPONENT_ENTRY_EVENT_STALE")
            self._repository.mark_opponent_entry_event_handled(
                event_id=event_id,
                handled_at_utc=_now_iso(),
            )

    # -- identity/read helpers ------------------------------------------------

    def _safe_current_identity(self) -> TurnIdentity | None:
        session = self._repository.load_active_session()
        if session is None or session.current_turn_id is None:
            return None
        turn = self._repository.get_turn(session.current_turn_id)
        return TurnIdentity(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            turn_id=turn.turn_id,
            turn_number=turn.turn_number,
            battle_revision=session.battle_revision,
        )

    def turn_state_summary(self) -> TurnStateSummaryView:
        """Pure read-side projection. Never mutates, never dispatches."""

        identity = self._safe_current_identity()
        if identity is None:
            return TurnStateSummaryView(
                identity=None,
                confirmed_state=None,
                confirmed_legal_actions=(),
                latest_delta=None,
                open_draft=None,
                pending_opponent_entry_event=None,
                provider_ready=False,
                provider_ready_denial_reasons=(),
            )
        confirmed_state = self._repository.get_latest_confirmed_turn_state_for_identity(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        confirmed_legal_actions = (
            self._repository.list_confirmed_legal_action_selections_for_identity(identity)
        )
        latest_delta: ActionResultDelta | None = None
        if confirmed_state is not None:
            deltas = self._repository.list_action_result_deltas_based_on(
                confirmed_state.confirmed_state_id
            )
            latest_delta = deltas[-1] if deltas else None
        open_draft = self._repository.get_latest_next_turn_state_draft_for_identity(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        pending_opponent_entry_event = self._repository.get_pending_opponent_entry_event(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        provider_ready = False
        denial_reasons: tuple[str, ...] = ()
        legal_switch_confirmation: LegalSwitchConfirmation | None = None
        legal_switch_candidates: tuple[str, ...] = ()
        selected_three: tuple[str, str, str] | None = None
        confirmed_fainted_members: frozenset[str] = frozenset()
        if confirmed_state is not None:
            session = self._repository.load_active_session()
            if session is not None and session.current_applied_selection_id is not None:
                applied = self._repository.get_applied_selection(
                    session.current_applied_selection_id
                )
                selected_three = applied.selected_three
                legal_switch_confirmation = self._repository.get_legal_switch_confirmation(
                    identity=identity,
                    based_on_confirmed_state_id=confirmed_state.confirmed_state_id,
                    applied_selection_id=applied.applied_selection_id,
                )
                memory_by_name = {
                    name: memory
                    for name in applied.selected_three
                    if (
                        memory := self._repository.get_pokemon_local_state(
                            session_id=identity.session_id,
                            match_id=identity.match_id,
                            generation=identity.generation,
                            side="SELF",
                            pokemon_name=name,
                        )
                    )
                    is not None
                }
                confirmed_fainted_members = frozenset(
                    name for name, memory in memory_by_name.items() if is_confirmed_fainted(memory)
                )
                self_active = confirmed_state.self_side.active
                if self_active.is_confirmed and self_active.value:
                    try:
                        legal_switch_candidates = derive_legal_switch_candidates(
                            applied=applied,
                            current_active_name=self_active.value,
                            local_memory_by_name=memory_by_name,
                        )
                    except LegalSwitchError:
                        # R3-B: active is confirmed but not a member of
                        # selected_three -- fail closed to no candidates
                        # rather than crash this read-only summary.
                        legal_switch_candidates = ()
            gate: ProviderReadyGateResult = evaluate_provider_ready_gate(
                confirmed_state=confirmed_state,
                confirmed_legal_actions=confirmed_legal_actions,
                current_identity=identity,
                latest_confirmed_state_id=confirmed_state.confirmed_state_id,
                latest_open_draft_turn_number=(
                    open_draft.identity.turn_number if open_draft is not None else None
                ),
                latest_open_draft_battle_revision=(
                    open_draft.identity.battle_revision if open_draft is not None else None
                ),
                legal_switch_confirmation=legal_switch_confirmation,
                selected_three=selected_three,
                confirmed_fainted_members=confirmed_fainted_members,
            )
            provider_ready = gate.allowed
            denial_reasons = tuple(reason.value for reason in gate.denial_reasons)
        return TurnStateSummaryView(
            identity=identity,
            confirmed_state=confirmed_state,
            confirmed_legal_actions=confirmed_legal_actions,
            latest_delta=latest_delta,
            open_draft=open_draft,
            pending_opponent_entry_event=pending_opponent_entry_event,
            provider_ready=provider_ready,
            provider_ready_denial_reasons=denial_reasons,
            legal_switch_confirmation=legal_switch_confirmation,
            legal_switch_candidates=legal_switch_candidates,
        )

    @staticmethod
    def denial_reason_message(reason_code: str) -> str:
        return _RICH_GATE_MESSAGES.get(reason_code, reason_code)

    # -- confirm current state + legal actions --------------------------------

    def confirm_turn_facts(
        self,
        *,
        self_active: str,
        opponent_active: str,
        self_hp: str,
        opponent_hp: str,
        legal_moves: Sequence[str],
        legal_switches: Sequence[str],
        human_note: str,
        human_confirmed: bool,
        self_side: SideState | None = None,
        opponent_side: SideState | None = None,
        weather: Known[str] | None = None,
        terrain: Known[str] | None = None,
    ) -> OperatorView:
        """Confirm legacy Turn facts, then persist the Bundle A rich state.

        Backward compatible: any caller that omits ``self_side``/
        ``opponent_side``/``weather``/``terrain`` (every existing test and
        UI path that predates Bundle C) gets byte-identical behavior to the
        inherited :meth:`MatchFlowController.confirm_turn_facts` -- nothing
        below is reached. Only Bundle C's own window passes those four,
        and only once the legacy call above has durably transitioned the
        session to ``TURN_REVIEWED`` does this also persist a new
        :class:`ConfirmedTurnState` and one
        :class:`ConfirmedLegalActionSelection` per confirmed legal move/
        switch, bound to that exact Turn identity. A failure in the legacy
        step (validation, staleness, missing applied selection) leaves
        every rich-state table untouched.
        """

        before_error = self._error_message
        view = super().confirm_turn_facts(
            self_active=self_active,
            opponent_active=opponent_active,
            self_hp=self_hp,
            opponent_hp=opponent_hp,
            legal_moves=legal_moves,
            legal_switches=legal_switches,
            human_note=human_note,
            human_confirmed=human_confirmed,
        )
        if self_side is None or opponent_side is None or weather is None or terrain is None:
            return view
        if view.error_message is not None and view.error_message != before_error:
            return view
        if view.projection.session_state != "TURN_REVIEWED":
            return view
        identity = self._safe_current_identity()
        if identity is None:
            return view
        confirmed_state_id = str(uuid4())
        previous_state = self._repository.get_latest_confirmed_turn_state_for_identity(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        try:
            state = ConfirmedTurnState(
                confirmed_state_id=confirmed_state_id,
                identity=identity,
                previous_confirmed_state_id=(
                    previous_state.confirmed_state_id if previous_state is not None else None
                ),
                self_side=self_side,
                opponent_side=opponent_side,
                weather=weather,
                terrain=terrain,
                confirmation=_confirmation_meta(),
            )
        except (ValueError, TurnStateError) as exc:
            self._error_message = f"current state を保存できません: {exc}"
            return self.refresh()

        confirmation = _confirmation_meta()
        selections = []
        for name in legal_moves:
            if not name.strip():
                continue
            selections.append(
                confirm_legal_action_selection(
                    confirmation_id=str(uuid4()),
                    identity=identity,
                    action_type=ActionType.MOVE,
                    action_name=name,
                    confirmation=confirmation,
                )
            )
        for name in legal_switches:
            if not name.strip():
                continue
            selections.append(
                confirm_legal_action_selection(
                    confirmation_id=str(uuid4()),
                    identity=identity,
                    action_type=ActionType.SWITCH,
                    action_name=name,
                    confirmation=confirmation,
                )
            )
        with self._repository.transaction():
            self._repository.append_confirmed_turn_state(state)
            entry_event = self._build_opponent_entry_event(state, previous_state)
            if entry_event is not None:
                prior_pending = self._repository.get_pending_opponent_entry_event(
                    session_id=identity.session_id,
                    match_id=identity.match_id,
                    generation=identity.generation,
                )
                if prior_pending is not None:
                    self._repository.mark_opponent_entry_event_handled(
                        event_id=prior_pending.event_id,
                        handled_at_utc=_now_iso(),
                    )
                self._repository.append_opponent_entry_event(entry_event)
            for selection in selections:
                self._repository.append_confirmed_legal_action_selection(selection)
            self._save_pokemon_local_memory(identity, "SELF", self_side)
            self._save_pokemon_local_memory(identity, "OPPONENT", opponent_side)
        self._error_message = None
        return self.refresh()

    def _build_opponent_entry_event(
        self,
        state: ConfirmedTurnState,
        previous_state: ConfirmedTurnState | None,
    ) -> OpponentEntryEvent | None:
        """Create an event only from a genuine human-confirmed state transition."""

        active = state.opponent_side.active
        if not active.is_confirmed or active.value is None:
            return None
        species_name = active.value.strip()
        if not species_name or species_name == "UNKNOWN":
            return None
        if previous_state is not None:
            previous_active = previous_state.opponent_side.active
            previous_name = (
                previous_active.value.strip()
                if previous_active.is_confirmed and previous_active.value is not None
                else ""
            )
            # Same-Turn re-confirmation is correction, never a field entry.
            if state.identity.turn_number <= previous_state.identity.turn_number:
                return None
            if previous_name == species_name:
                return None

        catalog = canonical_species_ability_catalog()
        try:
            species = catalog.resolve_species(species_name)
        except SpeciesCatalogCoverageError:
            species_id = None
            entity_fragment = "".join(
                character for character in species_name.lower() if character.isalnum()
            ) or "unknown"
            opponent_entity_id = f"opponent-unresolved:{entity_fragment}"
        else:
            species_id = species.species_id
            opponent_entity_id = f"opponent-species:{species.species_id}"
        identity = state.identity
        ordinal = self._repository.next_opponent_entry_ordinal(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        event_key = (
            f"maple-opponent-entry:{identity.session_id}:{identity.match_id}:"
            f"{identity.generation}:{ordinal}"
        )
        return OpponentEntryEvent(
            event_id=str(uuid5(NAMESPACE_URL, event_key)),
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
            entry_ordinal=ordinal,
            confirmed_state_id=state.confirmed_state_id,
            turn_id=identity.turn_id,
            turn_number=identity.turn_number,
            species_id=species_id,
            species_name=species_name,
            opponent_entity_id=opponent_entity_id,
        )

    def _save_pokemon_local_memory(
        self, identity: TurnIdentity, side: str, side_state: SideState
    ) -> None:
        """Snapshot this confirmed state's active Pokemon HP/status into
        match-local memory (event-entry UI v3), so a later ordinary switch
        back to this same Pokemon can restore it. A no-op when the active
        Pokemon's name is not itself confirmed -- memory is always keyed by
        an explicit Pokemon name, never guessed."""

        active = side_state.active
        if not active.is_confirmed or active.value is None:
            return
        self._repository.upsert_pokemon_local_state(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
            side=side,
            memory=PokemonLocalMemory(
                pokemon_name=active.value,
                hp_bucket=side_state.hp_bucket,
                status=side_state.status,
            ),
        )

    # -- event-entry UI v3: ability-stage events + confirmed-switch transition -

    @staticmethod
    def stage_event_presets() -> tuple[StageEventPreset, ...]:
        return STAGE_EVENT_PRESETS

    def preview_stage_event(
        self, *, side: str, preset_key: str
    ) -> dict[str, tuple[int, int]]:
        """Read-only: {stage_field_name: (current, candidate)} for every
        field the preset touches. Never mutates anything -- selecting a
        preset alone must not change canonical state."""

        preset = STAGE_EVENT_PRESETS_BY_KEY.get(preset_key)
        if preset is None:
            return {}
        summary = self.turn_state_summary()
        if summary.confirmed_state is None:
            return {}
        side_state = (
            summary.confirmed_state.self_side
            if side == "self"
            else summary.confirmed_state.opponent_side
        )
        current_stages = {
            name: _stage_value(getattr(side_state, name)) for name in STAGE_FIELD_NAMES
        }
        candidate = apply_stage_event(current_stages, preset)
        return {name: (current_stages[name], candidate[name]) for name in candidate}

    def compute_confirmed_switch_side_delta(
        self, *, side: str, destination_pokemon_name: str
    ) -> SideDelta:
        """The SideDelta an ordinary confirmed switch produces for one side.

        Active identity changes to the destination, every ability stage
        resets to 0 (active-slot-only, never carried across a switch),
        side-wide effects remain unchanged, and HP/major-status are restored
        from this match's per-Pokemon memory for the destination Pokemon --
        UNKNOWN (never a guessed default) when it has not appeared before
        this match. Read-only against persistence; writes nothing.
        """

        identity = self._safe_current_identity()
        if identity is None:
            raise TurnStateError("NO_ACTIVE_TURN_IDENTITY")
        name = destination_pokemon_name.strip()
        if not name:
            raise ValueError("destination_pokemon_name must be explicit")
        db_side = "SELF" if side == "self" else "OPPONENT"
        memory = self._repository.get_pokemon_local_state(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
            side=db_side,
            pokemon_name=name,
        )
        if memory is not None:
            hp_delta = _known_to_restoring_field_delta(memory.hp_bucket)
            status_delta = _known_to_restoring_field_delta(memory.status)
        else:
            hp_delta = FieldDelta.unknown()
            status_delta = FieldDelta.unknown()
        stage_deltas = {
            field_name: FieldDelta.changed(0, provenance_chain=_HUMAN_INPUT_CHAIN)
            for field_name in STAGE_FIELD_NAMES
        }
        return SideDelta(
            active=FieldDelta.changed(name, provenance_chain=_HUMAN_INPUT_CHAIN),
            hp_bucket=hp_delta,
            status=status_delta,
            side_effects=FieldDelta.unchanged(),
            **stage_deltas,
        )

    # -- Bundle 2 (Gemini V2): explicit legal-switch confirmation --------------

    def derive_legal_switch_candidates(self) -> tuple[str, ...]:
        """Operator-facing prefill aid for the current Turn's factual
        workbench. Read-only; never itself a confirmation."""

        return self._application.derive_legal_switch_candidates_for_current_turn()

    def confirm_legal_switches(
        self, *, legal_switches: tuple[str, ...], status: LegalSwitchStatus
    ) -> OperatorView:
        """Persist the operator's explicit legal-switch confirmation for the
        current Turn binding. Fails closed on any hard-invalidity violation,
        surfaced the same way every other operator command here surfaces a
        rejection -- via ``error_message`` on the refreshed view, never a
        raw exception reaching the UI layer."""

        try:
            self._application.confirm_legal_switches(
                legal_switches=legal_switches, status=status, human_confirmed=True
            )
        except DomainError as error:
            self._error_message = f"legal switch confirmation を保存できません: {error}"
        else:
            self._error_message = None
        return self.refresh()

    # -- confirm action-result delta -------------------------------------------

    def record_actual_action(
        self,
        *,
        action_type: str,
        action_name: str,
        human_confirmed: bool,
        opponent_action_type: str = "",
        opponent_action_name: str | None = None,
        action_order: str = "UNKNOWN",
        self_side_delta: SideDelta | None = None,
        opponent_side_delta: SideDelta | None = None,
        weather_delta: FieldDelta[str] | None = None,
        terrain_delta: FieldDelta[str] | None = None,
    ) -> OperatorView:
        """Atomically record the legacy action and the Bundle A result.

        Backward compatible: omitting the four delta kwargs (every existing
        caller) reproduces :meth:`MatchFlowController.record_actual_action`
        exactly. When supplied, the delta is bound to whatever
        :class:`ConfirmedTurnState` is currently latest for this Turn
        identity. The application command owns one transaction containing
        ``recorded_actions``, the delta, the rich completion, and the
        ``TURN_RECORDED`` session transition.
        """

        rich_delta_requested = all(
            value is not None
            for value in (self_side_delta, opponent_side_delta, weather_delta, terrain_delta)
        )
        if not rich_delta_requested:
            self._error_message = "行動結果の必須項目が不足しています。"
            return self.refresh()
        identity = self._safe_current_identity()
        if identity is None:
            self._error_message = "現在のTurn identityがありません。"
            return self.refresh()
        latest_state = self._repository.get_latest_confirmed_turn_state_for_identity(
            session_id=identity.session_id,
            match_id=identity.match_id,
            generation=identity.generation,
        )
        if latest_state is None or (
            latest_state.identity.session_id != identity.session_id
            or latest_state.identity.match_id != identity.match_id
            or latest_state.identity.generation != identity.generation
            or latest_state.identity.turn_id != identity.turn_id
            or latest_state.identity.turn_number != identity.turn_number
        ):
            self._error_message = "現在のTurnに一致する確定状態がありません。"
            return self.refresh()

        # Build and validate the delta *before* touching the legacy action
        # record (00 R2 lifecycle fix, closing the atomicity gap where the
        # legacy write could commit while the rich delta failed to
        # construct, leaving a "action persisted, delta lost" partial
        # completion that the operator could still advance past). Nothing
        # here depends on the legacy call's own result -- only on
        # ``latest_state`` (already read above) and the caller-supplied
        # delta pieces -- so validating first costs nothing.
        delta: ActionResultDelta | None = None
        assert self_side_delta is not None
        assert opponent_side_delta is not None
        assert weather_delta is not None
        assert terrain_delta is not None
        try:
            delta = ActionResultDelta(
                delta_id=str(uuid4()),
                identity=latest_state.identity,
                based_on_confirmed_state_id=latest_state.confirmed_state_id,
                self_side=self_side_delta,
                opponent_side=opponent_side_delta,
                weather=weather_delta,
                terrain=terrain_delta,
                confirmation=_confirmation_meta(),
            )
        except (ValueError, TurnStateError) as exc:
            self._error_message = f"action result delta を保存できません: {exc}"
            return self.refresh()

        before_error = self._error_message
        view = super().record_rich_actual_action(
            action_type=action_type,
            action_name=action_name,
            human_confirmed=human_confirmed,
            opponent_action_type=opponent_action_type,
            opponent_action_name=opponent_action_name,
            action_order=action_order,
            completion_identity=delta.identity,
            action_result_delta=delta,
            rich_transaction_id=str(uuid4()),
        )
        if view.error_message is not None and view.error_message != before_error:
            return view
        if view.projection.session_state != "TURN_RECORDED":
            return view
        return view

    # -- NEXT TURN + draft derivation ------------------------------------------

    def next_turn(self) -> OperatorView:
        """Derive and atomically persist the next Turn and Bundle A draft.

        00 design decision (Issue #31 comment 5217661584, closing the
        DESIGN_CONFLICT raised in comment 5217523903): ``battle_revision``
        is a durable *global* mutation-revision counter, not a Turn-scoped
        sequence, so :func:`~maple_next.domain.turn_state._bind_next_identity`
        now requires the next identity's revision to be *strictly greater
        than* the previous confirmed state's revision, not exactly +1. This
        method uses only the real, durable session identity (``turn_id``/
        ``turn_number``/``battle_revision`` as of right after the legacy
        :meth:`next_turn` transition) -- never a fabricated value. Because
        the legacy ``confirm_turn_facts``/``apply_rich_turn_advice_result``/
        ``record_actual_action``/``next_turn`` sequence only ever
        *increases* ``session.battle_revision`` (each calls
        ``session.bump_battle()``, which is always ``+= 1``), the real
        revision at this point is always strictly greater than whatever was
        stamped on the Turn's ``ConfirmedTurnState`` -- so this now
        genuinely succeeds under the accepted rule, not just in principle.

        Draft derivation happens before either the new Turn or draft is
        written. Derivation, Turn/session advancement, and draft persistence
        share one application transaction, so any failure preserves the
        current Turn identity.
        """

        identity_before = self._safe_current_identity()
        latest_state = None
        delta = None
        if identity_before is not None:
            latest_state = self._repository.get_latest_confirmed_turn_state_for_identity(
                session_id=identity_before.session_id,
                match_id=identity_before.match_id,
                generation=identity_before.generation,
            )
            if latest_state is not None:
                deltas = self._repository.list_action_result_deltas_based_on(
                    latest_state.confirmed_state_id
                )
                delta = deltas[-1] if deltas else None

        if latest_state is not None and delta is None:
            # The rich-state contract is active for this Turn (a
            # ConfirmedTurnState exists) but no ActionResultDelta was ever
            # durably recorded for it -- e.g. the rich delta write failed
            # after the legacy action already committed, or a caller
            # skipped the rich path entirely mid-match. Advancing now would
            # silently skip draft derivation while still moving the legacy
            # Turn forward, leaving Turn N+1 hydrating from a stale or
            # absent draft (00 R2 lifecycle fix). Fail closed: neither the
            # legacy Turn transition nor a partial/absent draft may
            # proceed. The operator must resolve this Turn (re-run the
            # delta capture, or restart the match if truly unrecoverable)
            # before NEXT TURN succeeds again.
            self._error_message = (
                "行動結果(delta)が未確定のため、NEXT TURNへ進めません。"
            )
            return self.refresh()

        if latest_state is None or delta is None:
            return super().next_turn()
        try:
            self._application.next_turn_with_action_result(
                confirmed_state=latest_state,
                delta=delta,
                draft_id=str(uuid4()),
                derived_at_utc=_now_iso(),
            )
        except (DomainError, TurnStateError, RuntimeError):
            self._error_message = (
                "NEXT TURN縺ｨ豁｡Turn state draft縺ｮ菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆縲・"
            )
            return self.refresh()
        self._error_message = None
        return self.refresh()

    # -- rich-state Gemini send -------------------------------------------------

    @property
    def rich_turn_gemini_send_available(self) -> bool:
        return self._rich_turn_gemini_adapter is not None

    @property
    def rich_turn_gemini_uses_injected_transport(self) -> bool:
        adapter = self._rich_turn_gemini_adapter
        return adapter is not None and adapter.uses_injected_transport

    def rich_turn_advice_gemini_status(self) -> RichTurnAdviceGeminiStatusView:
        adapter = self._rich_turn_gemini_adapter
        if adapter is None:
            return RichTurnAdviceGeminiStatusView(
                status="UNAVAILABLE", source_type="—", model="—", sanitized_failure=""
            )
        if adapter.in_flight:
            return RichTurnAdviceGeminiStatusView(
                status="PENDING",
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="",
            )
        if adapter.last_failure_reason is not None:
            return RichTurnAdviceGeminiStatusView(
                status="FAILED",
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure=adapter.last_failure_reason,
            )
        if adapter.last_disposition is ResultDisposition.APPLIED:
            return RichTurnAdviceGeminiStatusView(
                status="SUCCESS",
                source_type=adapter.last_source_type or FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="",
            )
        if adapter.last_disposition is not None:
            return RichTurnAdviceGeminiStatusView(
                status="STALE_OR_INVALID",
                source_type=adapter.last_source_type or FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=adapter.last_model or "—",
                sanitized_failure="GEMINI_TURN_RESULT_STALE",
            )
        return RichTurnAdviceGeminiStatusView(
            status="IDLE", source_type=FAKE_TURN_ADVICE_SOURCE_TYPE, model="—", sanitized_failure=""
        )

    def send_rich_turn_advice_to_gemini(
        self,
        *,
        action_type: str,
        action_name: str,
        opponent_prediction: str,
        rationale: str,
        warnings: tuple[str, ...],
        on_result: Callable[[OperatorView], None],
    ) -> OperatorView:
        """Trusted-human-click-only send of the confirmed rich state.

        Refuses to even build a fake payload unless ``action_name`` is one
        of the *confirmed* legal actions for the current Turn identity
        (never the unconfirmed prefill draft) and the provider-ready gate
        (recomputed fresh, not cached) allows it.
        """

        adapter = self._rich_turn_gemini_adapter
        if adapter is None:
            self._error_message = "Gemini Turn Advice adapterが設定されていません。"
            return self.refresh()
        summary = self.turn_state_summary()
        if not summary.provider_ready:
            reasons = ", ".join(
                self.denial_reason_message(code)
                for code in summary.provider_ready_denial_reasons
            ) or "provider-ready条件を満たしていません。"
            self._error_message = reasons
            return self.refresh()
        if not adapter.uses_injected_transport:
            return self._dispatch_rich_turn_advice(adapter, on_result)

        try:
            typed_action = ActionType(action_type.strip())
        except ValueError:
            self._error_message = "行動種別はMOVEまたはSWITCHを選択してください。"
            return self.refresh()
        normalized_name = action_name.strip()
        matching_selection = next(
            (
                selection
                for selection in summary.confirmed_legal_actions
                if selection.action_type == typed_action
                and selection.action_name == normalized_name
            ),
            None,
        )
        if matching_selection is None:
            self._error_message = "推奨actionは確定済み合法行動から選んでください。"
            return self.refresh()
        normalized_prediction = opponent_prediction.strip()
        normalized_rationale = rationale.strip()
        if not normalized_prediction or not normalized_rationale:
            self._error_message = "相手の次行動予測と理由を入力してください。"
            return self.refresh()

        # The rich legality re-check matches on the *confirmed selection's*
        # own id (== RichLegalAction.action_id, built from
        # ConfirmedLegalActionSelection.confirmation_id) plus an exact
        # type/name match -- never a synthetic "TYPE:NAME" string.
        action_id = matching_selection.confirmation_id
        # Gemini V2 Bundle 6: the rich lane's contract is now ``.v7``
        # (response schema v2) -- this injected/mock payload must match that
        # shape exactly, the same way it always matched whatever schema was
        # current for every prior bundle. ``UNKNOWN``/``NONE``/``LOW`` mirror
        # the old ``category="UNKNOWN"`` mock semantics exactly: this manual
        # operator entry never claims a classified category, a grounded
        # exact action, or graded evidence.
        adapter.enqueue_response(
            SanitizedProviderResult(
                payload={
                    "response_schema_version": "maple-turn-advice-response.v2",
                    "recommended_action": {
                        "action_id": action_id,
                        "action_type": typed_action.value,
                        "action_name": normalized_name,
                    },
                    "recommendation_robustness": "HIGH",
                    "reasons": [normalized_rationale[:280]],
                    "opponent_prediction": {
                        "primary": {
                            "category": "UNKNOWN",
                            "specific_action": None,
                            "support_basis": "NONE",
                            "support": "LOW",
                            "summary": normalized_prediction[:400],
                        },
                        "alternatives": [],
                    },
                    "warnings": list(warnings)[:2],
                },
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=FAKE_TURN_MODEL,
            )
        )
        return self._dispatch_rich_turn_advice(adapter, on_result)

    def _dispatch_rich_turn_advice(
        self,
        adapter: GeminiRichTurnAdviceAdapter,
        on_result: Callable[[OperatorView], None],
    ) -> OperatorView:
        callback_already_ran = False

        def handle_applied(disposition: ResultDisposition) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            if disposition is not ResultDisposition.APPLIED:
                self._error_message = "Turn Adviceを適用できませんでした。"
            else:
                self._error_message = None
            if callable(on_result):
                on_result(self.refresh())

        def handle_failed(reason: str) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            self._error_message = describe_gemini_turn_failure(reason)
            if callable(on_result):
                on_result(self.refresh())

        adapter.send(self._application, on_applied=handle_applied, on_failed=handle_failed)
        if not callback_already_ran:
            self._error_message = None
        return self.refresh()
