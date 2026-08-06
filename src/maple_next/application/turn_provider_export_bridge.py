"""Bundle B: Provider / Export Bridge (Issue #31).

Application-layer orchestration that wires the pure domain projection
(``domain/turn_state_projection.py``) and pure provider request builder
(``providers/turn_advice_rich_state.py``) to already-loaded Bundle A
persistence objects. This module never performs a network send, never
touches the operator UI, never executes a game action, and never imports
anything from ``providers/turn_request.py``, ``providers/turn_boundary.py``'s
prompt builders, ``providers/turn_response.py``, or ``providers/turn_transport.py``
beyond the existing dispatch-decision type it reuses unchanged.

Callers (a future worker/UI integrator, not part of Bundle B) remain
responsible for:

- loading the latest ``ConfirmedTurnState`` and latest ``NextTurnStateDraft``
  (if any) via the existing, unmodified
  ``TurnStateStoreMixin.get_latest_confirmed_turn_state`` /
  ``get_latest_next_turn_state_draft``;
- supplying the final, human-confirmed
  ``ConfirmedLegalActionSelection`` tuple for the current identity;
- computing the ``DispatchDecision`` via the existing, unmodified
  ``decide_turn_advice_dispatch`` (trusted-human-activation / binding /
  pending-job / one-attempt);
- actually sending the resulting request to a provider transport, if ever --
  this module only builds the request.
"""

from __future__ import annotations

from maple_next.domain.turn_state import (
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    NextTurnStateDraft,
    TurnIdentity,
)
from maple_next.providers.turn_advice_rich_state import (
    RichStateTurnAdviceRequest,
    build_rich_state_turn_advice_request,
)
from maple_next.providers.turn_boundary import DispatchDecision


def build_provider_ready_rich_state_request(
    *,
    current_identity: TurnIdentity,
    latest_confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    latest_open_draft: NextTurnStateDraft | None,
    dispatch_decision: DispatchDecision,
) -> RichStateTurnAdviceRequest:
    """Build one provider-ready rich-state Turn Advice request, or fail closed.

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
        dispatch_decision=dispatch_decision,
    )
