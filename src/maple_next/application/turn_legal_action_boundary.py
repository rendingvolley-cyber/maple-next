"""Bundle A: confirmed legal-action boundary adapter (Issue #31, point 5).

Pure application-layer conversion from already-confirmed legal-action
selections into the value shape ``confirm_turn_facts()`` expects for its
legal-action inputs (``legal_moves`` / ``legal_switches`` tuples on
``TurnFactsSnapshot``). This module never imports a provider module, never
builds a request, and never sends anything over the network -- it only
classifies already-confirmed selections. It does not replace or bypass the
existing legality/applied-selection-membership validation on the existing
route; that final validation stays exactly where it already lives.
"""

from __future__ import annotations

from dataclasses import dataclass

from maple_next.domain.enums import ActionType
from maple_next.domain.turn_state import (
    ConfirmedLegalActionSelection,
    TurnIdentity,
    TurnStateIdentityError,
)


@dataclass(frozen=True, slots=True)
class ConfirmedLegalActionsInput:
    """The ``confirm_turn_facts()``-shaped output of the boundary adapter."""

    legal_moves: tuple[str, ...]
    legal_switches: tuple[str, ...]


def build_confirmed_legal_actions_input(
    identity: TurnIdentity,
    selections: tuple[ConfirmedLegalActionSelection, ...],
) -> ConfirmedLegalActionsInput:
    """Convert confirmed legal-action selections into ``confirm_turn_facts()`` input.

    Fails closed:

    - rejects any object that is not a :class:`ConfirmedLegalActionSelection`
      (in particular, a ``LegalActionPrefillDraft`` is never silently
      promoted to a confirmed selection here)
    - rejects any selection whose identity does not exactly match
      ``identity`` (session/match/generation/turn id/turn number/battle
      revision) -- this also rejects a stale/superseded revision, since a
      superseded selection's identity will not match the current one
    - rejects a blank action name
    - rejects a duplicate action name within the same action type
    """

    seen_moves: set[str] = set()
    seen_switches: set[str] = set()
    legal_moves: list[str] = []
    legal_switches: list[str] = []

    for selection in selections:
        if not isinstance(selection, ConfirmedLegalActionSelection):
            raise TurnStateIdentityError("UNCONFIRMED_OBJECT_REJECTED")
        if selection.identity != identity:
            raise TurnStateIdentityError("LEGAL_ACTION_IDENTITY_MISMATCH")
        name = selection.action_name.strip()
        if not name:
            raise ValueError("LEGAL_ACTION_NAME_BLANK")
        if selection.action_type is ActionType.MOVE:
            if name in seen_moves:
                raise ValueError("DUPLICATE_LEGAL_MOVE")
            seen_moves.add(name)
            legal_moves.append(name)
        elif selection.action_type is ActionType.SWITCH:
            if name in seen_switches:
                raise ValueError("DUPLICATE_LEGAL_SWITCH")
            seen_switches.add(name)
            legal_switches.append(name)
        else:  # pragma: no cover - ActionType is exhaustively MOVE/SWITCH today
            raise ValueError(f"UNSUPPORTED_ACTION_TYPE:{selection.action_type}")

    return ConfirmedLegalActionsInput(
        legal_moves=tuple(legal_moves),
        legal_switches=tuple(legal_switches),
    )
