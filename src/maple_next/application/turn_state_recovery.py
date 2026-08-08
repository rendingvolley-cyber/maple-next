"""Bundle A restart/recovery hydration for the turn state contract.

This is a pure/application boundary Bundle C may call later. It does not
wire into ``BattleApplication`` or the operator UI, and it does not infer or
backfill any missing legacy data -- absence of rich-state rows simply means
the legacy Turn flow is in effect for that session.
"""

from __future__ import annotations

from dataclasses import dataclass

from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmedTurnState,
    NextTurnStateDraft,
    TurnStateStaleError,
    validate_turn_state_full_chain,
)
from maple_next.persistence.sqlite import SQLiteRepository


@dataclass(frozen=True, slots=True)
class TurnStateRecovery:
    latest_confirmed_state: ConfirmedTurnState | None
    latest_delta: ActionResultDelta | None
    latest_draft: NextTurnStateDraft | None


def hydrate_turn_state(repository: SQLiteRepository, session_id: str) -> TurnStateRecovery:
    """Recover the latest confirmed state/delta/draft chain for a session.

    Fails closed (raises :class:`TurnStateStaleError`/
    :class:`TurnStateIdentityError`) if a hydrated draft is stale relative to
    the hydrated latest confirmed state, or if the delta it was derived from
    does not full-chain-validate against that confirmed state (not merely a
    state-id string comparison). Mismatched or stale persisted data is
    rejected rather than silently accepted after restart.
    """

    latest_confirmed = repository.get_latest_confirmed_turn_state(session_id)
    latest_draft = repository.get_latest_next_turn_state_draft(session_id)
    latest_delta: ActionResultDelta | None = None

    if latest_draft is not None:
        if latest_confirmed is None:
            raise TurnStateStaleError("HYDRATED_DRAFT_STALE")
        latest_delta = repository.get_action_result_delta(latest_draft.source_delta_id)
        validate_turn_state_full_chain(latest_confirmed, latest_delta, latest_draft)

    return TurnStateRecovery(
        latest_confirmed_state=latest_confirmed,
        latest_delta=latest_delta,
        latest_draft=latest_draft,
    )
