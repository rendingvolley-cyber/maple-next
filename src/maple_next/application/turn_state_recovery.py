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
)
from maple_next.persistence.sqlite import SQLiteRepository


@dataclass(frozen=True, slots=True)
class TurnStateRecovery:
    latest_confirmed_state: ConfirmedTurnState | None
    latest_delta: ActionResultDelta | None
    latest_draft: NextTurnStateDraft | None


def hydrate_turn_state(repository: SQLiteRepository, session_id: str) -> TurnStateRecovery:
    """Recover the latest confirmed state/delta/draft chain for a session.

    Fails closed (raises :class:`TurnStateStaleError`) if a hydrated draft is
    stale relative to the hydrated latest confirmed state, or if the delta it
    was derived from was not based on that confirmed state. Mismatched or
    stale persisted data is rejected rather than silently accepted after
    restart.
    """

    latest_confirmed = repository.get_latest_confirmed_turn_state(session_id)
    latest_draft = repository.get_latest_next_turn_state_draft(session_id)
    latest_delta: ActionResultDelta | None = None

    if latest_draft is not None:
        if latest_confirmed is None or (
            latest_draft.based_on_confirmed_state_id != latest_confirmed.confirmed_state_id
        ):
            raise TurnStateStaleError("HYDRATED_DRAFT_STALE")
        latest_delta = repository.get_action_result_delta(latest_draft.source_delta_id)
        if latest_delta.based_on_confirmed_state_id != latest_confirmed.confirmed_state_id:
            raise TurnStateStaleError("HYDRATED_DELTA_MISMATCH")

    return TurnStateRecovery(
        latest_confirmed_state=latest_confirmed,
        latest_delta=latest_delta,
        latest_draft=latest_draft,
    )
