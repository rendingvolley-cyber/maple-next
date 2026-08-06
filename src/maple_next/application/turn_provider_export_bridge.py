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


def build_pure_rich_state_request_from_loaded_state(
    *,
    current_identity: TurnIdentity,
    latest_confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    latest_open_draft: NextTurnStateDraft | None,
    selected_three: tuple[str, str, str],
    self_active: str,
    evidence: FixedEvidenceMetadata | None = None,
    self_team_build_sha256: str | None = None,
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
        selected_three=selected_three,
        self_active=self_active,
        evidence=evidence,
        self_team_build_sha256=self_team_build_sha256,
    )
