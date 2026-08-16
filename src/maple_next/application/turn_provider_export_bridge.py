"""Bundle B: Provider / Export Bridge (Issue #31).

Application-layer orchestration that wires the pure domain projection
(``domain/turn_state_projection.py``) and pure provider request builder
(``providers/turn_advice_rich_state.py``) to already-loaded Bundle A
persistence objects. This module never performs a network send, never
touches the operator UI, never executes a game action, and never imports
anything from ``providers/turn_response.py`` or ``providers/turn_transport.py``.

This module's builder is a **pure function, not an authorization API**. It
must never accept, compute, or manufacture a ``DispatchDecision`` -- that
was a forge-resistance hole (any caller could pass
``DispatchDecision(allowed=True, ...)`` and obtain a provider-ready request
without ever touching durable state). Only the durable application API,
``application.service.BattleApplication.request_rich_turn_advice``, may
authorize dispatch and create a job; it does so by loading every fact
itself from the repository inside one transaction, then calling
:func:`build_pure_rich_state_request_from_loaded_state` below with those
loaded values.

Bundle 3 (Gemini V2) adds one **read-only** loader to this module,
:func:`load_bundle3_turn_context`. It is the single shared place where the
confirmed prior battle memory and the canonical selected-three build
context are loaded from durable state and validated, and it is called
identically by initial rich request creation and by offline rebuild/export
verification so those two paths cannot drift apart. It reads; it never
writes, never sends, and never authorizes anything. The pure builder below
remains pure -- it receives the already-validated
:class:`~maple_next.domain.battle_memory.Bundle3TurnContext` as a value.

Callers of this module remain responsible for:

- loading the latest ``ConfirmedTurnState`` and latest ``NextTurnStateDraft``
  (if any) via the existing, unmodified
  ``TurnStateStoreMixin.get_latest_confirmed_turn_state`` /
  ``get_latest_next_turn_state_draft`` (or their identity-scoped variants);
- supplying the final, human-confirmed
  ``ConfirmedLegalActionSelection`` tuple for the current identity, already
  validated through ``turn_legal_action_boundary.build_confirmed_legal_actions_input``;
- actually sending the resulting request to a provider transport, if ever --
  this module only builds the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from maple_next.domain.battle_memory import (
    Bundle3ContextError,
    Bundle3TurnContext,
    SelectedBuildContextError,
    build_battle_memory,
    project_selected_three_builds,
)
from maple_next.domain.champions_rules import (
    ChampionsRulesError,
    RulesPin,
    resolve_pinned_rules_context,
)
from maple_next.domain.enums import JobType
from maple_next.domain.legal_switches import LegalSwitchConfirmation
from maple_next.domain.models import AppliedSelectionSnapshot, BattleSession
from maple_next.domain.turn_state import (
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FixedEvidenceMetadata,
    NextTurnStateDraft,
    TurnIdentity,
)
from maple_next.providers.turn_advice_rich_state import (
    RichStateTurnAdviceRequest,
    build_rich_state_turn_advice_request,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle / typing only
    from maple_next.persistence.sqlite import SQLiteRepository


def load_bundle3_turn_context(
    repository: SQLiteRepository,
    *,
    session: BattleSession,
    current_identity: TurnIdentity,
    current_confirmed_state: ConfirmedTurnState,
    applied: AppliedSelectionSnapshot,
) -> Bundle3TurnContext:
    """The one shared Bundle 3 loader/validator. Read-only, fails closed.

    Used identically by (1) initial rich request creation
    (``BattleApplication.request_rich_turn_advice``) and (2) offline rebuild
    / export verification
    (``BattleApplication.build_rich_turn_advice_transport_request``), so a
    single set of business rules governs both and the rebuilt request bytes
    can never diverge from the authorized ones.

    Authoritative sources, all pre-existing -- no parallel store is
    introduced and no schema migration is required:

    - actual bring: ``BattleSession.current_applied_selection_id`` ->
      ``applied_selections`` -> :class:`AppliedSelectionSnapshot`;
    - applied/reviewed binding: that snapshot's ``source_advice_id`` ->
      ``selection_advices`` -> its Selection Advice ``async_jobs`` row ->
      ``job.input_snapshot_id`` -> ``BattleSession.
      current_reviewed_selection_id`` -> the exact reviewed
      :class:`~maple_next.domain.models.SelectionFacts`;
    - canonical build: ``SelectionFacts.self_team_build`` plus its existing
      SHA-256;
    - completed-turn truth: ``rich_action_completions`` plus each row's
      linked reviewed ``action_result_deltas`` row, anchored on the
      ``confirmed_turn_states`` chain.

    Raises :class:`Bundle3ContextError` (or one of its subclasses) on any
    broken link. It never writes, never sends, and never reserves anything.
    """

    reviewed_selection_id = session.current_reviewed_selection_id
    if reviewed_selection_id is None:
        raise Bundle3ContextError("REVIEWED_SELECTION_REQUIRED")

    try:
        advice = repository.get_selection_advice(applied.source_advice_id)
    except KeyError as exc:
        raise Bundle3ContextError("APPLIED_SELECTION_SOURCE_ADVICE_NOT_FOUND") from exc
    if str(advice["session_id"]) != session.session_id:
        raise Bundle3ContextError("SOURCE_ADVICE_FOREIGN_SESSION")

    try:
        advice_job = repository.get_job(str(advice["job_id"]))
    except KeyError as exc:
        raise Bundle3ContextError("SOURCE_ADVICE_JOB_NOT_FOUND") from exc
    if advice_job.job_type is not JobType.SELECTION_ADVICE:
        raise Bundle3ContextError("SOURCE_ADVICE_JOB_TYPE_MISMATCH")
    if (
        advice_job.session_id != session.session_id
        or advice_job.match_id != session.match_id
        or advice_job.generation != session.generation
    ):
        raise Bundle3ContextError("SOURCE_ADVICE_JOB_FOREIGN_MATCH")
    if advice_job.input_snapshot_id != reviewed_selection_id:
        raise Bundle3ContextError("SOURCE_ADVICE_JOB_NOT_BOUND_TO_REVIEWED_SELECTION")

    try:
        facts = repository.get_selection_facts(reviewed_selection_id)
    except KeyError as exc:
        raise Bundle3ContextError("REVIEWED_SELECTION_FACTS_NOT_FOUND") from exc
    if facts.reviewed_selection_id != reviewed_selection_id:
        raise Bundle3ContextError("REVIEWED_SELECTION_FACTS_IDENTITY_MISMATCH")

    if any(name not in facts.self_team for name in applied.selected_three):
        raise SelectedBuildContextError("APPLIED_SELECTION_OUTSIDE_REVIEWED_SELF_TEAM")
    selected_three_builds = project_selected_three_builds(
        selected_three=applied.selected_three,
        reviewed_self_team=facts.self_team,
        reviewed_self_team_build=facts.self_team_build,
        reviewed_self_team_build_sha256=facts.self_team_build_sha256,
    )

    match_states = repository.list_confirmed_turn_states_for_match(
        session_id=current_identity.session_id,
        match_id=current_identity.match_id,
        generation=current_identity.generation,
    )
    confirmed_state_ids = tuple(state.confirmed_state_id for state in match_states)
    completion_candidates = (
        repository.list_rich_action_completion_candidates_for_confirmed_states(
            confirmed_state_ids
        )
    )
    delta_candidates = repository.list_action_result_delta_candidates_for_confirmed_states(
        confirmed_state_ids
    )
    memory = build_battle_memory(
        current_identity=current_identity,
        current_confirmed_state=current_confirmed_state,
        match_confirmed_states=match_states,
        completion_candidates=completion_candidates,
        delta_candidates=delta_candidates,
    )
    return Bundle3TurnContext(
        applied_selection_id=applied.applied_selection_id,
        reviewed_selection_id=reviewed_selection_id,
        selected_three_builds=selected_three_builds,
        battle_memory=memory,
    )


def load_champions_rules_context(session: BattleSession) -> dict[str, Any]:
    """The one shared Bundle 4 loader/resolver. Read-only, fails closed.

    Resolves ``session``'s persisted rules pin (set once at match creation
    -- see ``application.service._resolve_new_match_rules_pin``) against
    the immutable checked-in official Champions rules snapshot, and returns
    the compact ``rules_context`` dict a rich request embeds. Used
    identically by initial rich request creation
    (``BattleApplication.request_rich_turn_advice``) and by offline rebuild
    / export verification
    (``BattleApplication.build_rich_turn_advice_transport_request``), so
    both paths can never resolve a different rules context for the same
    match.

    Raises :class:`~maple_next.domain.champions_rules.ChampionsRulesError`
    (or one of its subclasses) when the match has no pin, the pin does not
    match the currently checked-in snapshot, or the checked-in snapshot
    itself fails validation. Never performs a network fetch -- the checked-
    in snapshot is the only input.
    """

    if (
        session.rules_ruleset_id is None
        or session.rules_ruleset_version is None
        or session.rules_snapshot_id is None
        or session.rules_facts_sha256 is None
    ):
        raise ChampionsRulesError("MATCH_RULES_UNPINNED")
    pin = RulesPin(
        ruleset_id=session.rules_ruleset_id,
        ruleset_version=session.rules_ruleset_version,
        rules_snapshot_id=session.rules_snapshot_id,
        rules_facts_sha256=session.rules_facts_sha256,
    )
    return resolve_pinned_rules_context(pin)


def build_pure_rich_state_request_from_loaded_state(
    *,
    current_identity: TurnIdentity,
    latest_confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    latest_open_draft: NextTurnStateDraft | None,
    legal_switch_confirmation: LegalSwitchConfirmation | None,
    selected_three: tuple[str, str, str],
    self_active: str,
    bundle3_context: Bundle3TurnContext,
    rules_context: dict[str, Any],
    evidence: FixedEvidenceMetadata | None = None,
    self_team_build_sha256: str | None = None,
    confirmed_fainted_members: frozenset[str] = frozenset(),
) -> RichStateTurnAdviceRequest:
    """Build one provider-ready rich-state Turn Advice request, or fail closed.

    Pure and non-authorizing: this function never accepts a
    ``DispatchDecision`` and never checks pending-job/one-attempt/trusted-
    trigger state -- those are exclusively
    ``BattleApplication.request_rich_turn_advice``'s responsibility. It only
    re-validates the domain-source provider-ready gate as defense in depth.

    ``latest_confirmed_state`` is treated as both the projection source and
    the "current latest" reference -- callers must load it fresh (not from a
    cache) immediately before calling this function, the same discipline the
    legacy lane already requires for binding currency.
    """

    latest_open_draft_turn_number = (
        latest_open_draft.identity.turn_number if latest_open_draft is not None else None
    )
    latest_open_draft_battle_revision = (
        latest_open_draft.identity.battle_revision if latest_open_draft is not None else None
    )

    return build_rich_state_turn_advice_request(
        confirmed_state=latest_confirmed_state,
        confirmed_legal_actions=confirmed_legal_actions,
        current_identity=current_identity,
        latest_confirmed_state_id=latest_confirmed_state.confirmed_state_id,
        latest_open_draft_turn_number=latest_open_draft_turn_number,
        latest_open_draft_battle_revision=latest_open_draft_battle_revision,
        legal_switch_confirmation=legal_switch_confirmation,
        selected_three=selected_three,
        self_active=self_active,
        bundle3_context=bundle3_context,
        rules_context=rules_context,
        evidence=evidence,
        self_team_build_sha256=self_team_build_sha256,
        confirmed_fainted_members=confirmed_fainted_members,
    )
