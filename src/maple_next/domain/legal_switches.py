"""Bundle 2 (Gemini V2): explicit legal-switch confirmation status.

Closes the historical ``legal_switches = []`` ambiguity from the first real
Battle-1 evidence: "we did not capture/confirm legal switches" must never be
represented the same way as "there are definitely no legal switches". An
empty ``legal_switches`` tuple alone is never sufficient evidence of
``CONFIRMED_NONE`` -- every caller must also carry an explicit
:class:`LegalSwitchStatus`.

Reuses the existing binding/authority sources rather than inventing a
parallel identity system:

- :class:`~maple_next.domain.models.AppliedSelectionSnapshot` (already
  durable, already the accepted source of ``selected_three``/backline
  membership for legality checks elsewhere in ``application/service.py``);
- :class:`~maple_next.domain.turn_state.TurnIdentity` (the same identity/
  revision binding every other Bundle 1 confirmed fact uses);
- :class:`~maple_next.domain.turn_state.PokemonLocalMemory` (the existing
  match-local per-Pokemon HP/status memory) for confirmed-fainted detection.

This module never derives legal-switch membership from Opponent INTEL,
usage/population data, Gemini, general Pokemon knowledge, team-preview
guesses, unconfirmed OCR, or historical opponent data -- only match-local
factual state plus the final explicit human confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from maple_next.domain.enums import HpBucket
from maple_next.domain.models import AppliedSelectionSnapshot
from maple_next.domain.turn_state import ConfirmationMeta, PokemonLocalMemory, TurnIdentity


class LegalSwitchStatus(StrEnum):
    """Explicit three-state legal-switch truth.

    ``NOT_CAPTURED_OR_UNRESOLVED`` is never itself persisted as a
    :class:`LegalSwitchConfirmation` -- it is the logical value a caller
    must use when no matching confirmation exists for the current binding
    (see :meth:`LegalSwitchConfirmation.__post_init__`).
    """

    CONFIRMED_NONEMPTY = "CONFIRMED_NONEMPTY"
    CONFIRMED_NONE = "CONFIRMED_NONE"
    NOT_CAPTURED_OR_UNRESOLVED = "NOT_CAPTURED_OR_UNRESOLVED"


class LegalSwitchError(Exception):
    """Fail-closed base error for legal-switch confirmation."""


@dataclass(frozen=True, slots=True)
class LegalSwitchConfirmation:
    """A final, human-confirmed legal-switch set. Never true by omission.

    ``legal_switches = ()`` with ``status = CONFIRMED_NONE`` (operator
    explicitly confirmed zero legal switches) and the absence of any
    confirmation for this binding (NOT_CAPTURED_OR_UNRESOLVED, never
    constructed as an instance of this class) must remain distinguishable
    end to end -- this constructor is one half of that guarantee; the store
    layer's exact-binding lookup (:mod:`maple_next.persistence.
    legal_switch_store`) is the other half.
    """

    confirmation_id: str
    identity: TurnIdentity
    based_on_confirmed_state_id: str
    applied_selection_id: str
    legal_switches: tuple[str, ...]
    status: LegalSwitchStatus
    confirmation: ConfirmationMeta

    def __post_init__(self) -> None:
        if not self.confirmation_id.strip():
            raise ValueError("confirmation_id must be explicit")
        if not self.based_on_confirmed_state_id.strip():
            raise ValueError("based_on_confirmed_state_id must be explicit")
        if not self.applied_selection_id.strip():
            raise ValueError("applied_selection_id must be explicit")
        if not self.confirmation.confirmed_by_human:
            raise LegalSwitchError("LEGAL_SWITCH_CONFIRMATION_REQUIRES_HUMAN_CONFIRMATION")
        if self.status is LegalSwitchStatus.NOT_CAPTURED_OR_UNRESOLVED:
            raise LegalSwitchError("UNRESOLVED_IS_NEVER_A_PERSISTABLE_CONFIRMATION")
        if any(not name.strip() for name in self.legal_switches):
            raise LegalSwitchError("LEGAL_SWITCH_NAME_BLANK")
        if len(set(self.legal_switches)) != len(self.legal_switches):
            raise LegalSwitchError("DUPLICATE_LEGAL_SWITCH_NAME")
        if self.status is LegalSwitchStatus.CONFIRMED_NONEMPTY and not self.legal_switches:
            raise LegalSwitchError("CONFIRMED_NONEMPTY_REQUIRES_AT_LEAST_ONE_SWITCH")
        if self.status is LegalSwitchStatus.CONFIRMED_NONE and self.legal_switches:
            raise LegalSwitchError("CONFIRMED_NONE_REQUIRES_EMPTY_SWITCH_SET")


def _is_confirmed_fainted(memory: PokemonLocalMemory | None) -> bool:
    """Only an explicit confirmed HP=0 counts. Unknown health is never
    silently treated as fainted, and "never appeared this match" (no memory
    row at all) is unknown health, not fainted."""

    return (
        memory is not None
        and memory.hp_bucket.is_confirmed
        and memory.hp_bucket.value == HpBucket.ZERO
    )


def derive_legal_switch_candidates(
    *,
    applied: AppliedSelectionSnapshot,
    current_active_name: str,
    local_memory_by_name: Mapping[str, PokemonLocalMemory],
) -> tuple[str, ...]:
    """Operator-facing candidate set. A prefill/workbench aid, never a final confirmation.

    Authority order (locked): applied ``selected_three`` -> current confirmed
    active Pokemon -> match-local confirmed HP/faint state for the remaining
    selected members. Excludes the current active and any confirmed-fainted
    member; retains every other selected member, including one whose health
    is unknown -- unknown health is never silently promoted to a confirmed
    legal switch either. Population INTEL, Gemini, and OCR have zero
    influence here; only ``applied`` and ``local_memory_by_name`` are read.
    """

    candidates: list[str] = []
    for name in applied.selected_three:
        if name == current_active_name:
            continue
        if _is_confirmed_fainted(local_memory_by_name.get(name)):
            continue
        candidates.append(name)
    return tuple(candidates)


def confirm_legal_switches(
    *,
    confirmation_id: str,
    identity: TurnIdentity,
    based_on_confirmed_state_id: str,
    applied: AppliedSelectionSnapshot,
    current_active_name: str,
    local_memory_by_name: Mapping[str, PokemonLocalMemory],
    legal_switches: tuple[str, ...],
    status: LegalSwitchStatus,
    confirmation: ConfirmationMeta,
) -> LegalSwitchConfirmation:
    """Build one final, human-confirmed legal-switch set, or fail closed.

    Hard invalidity rules (never invented mechanics beyond these): the
    confirmed set may never contain the current confirmed active Pokemon, a
    member outside ``applied.selected_three``, or a member with match-local
    confirmed HP = 0. Human confirmation (``confirmation``) is the final
    authority -- the derived candidate list is only ever a prefill aid and
    is not consulted here.
    """

    seen: set[str] = set()
    for raw_name in legal_switches:
        name = raw_name.strip()
        if not name:
            raise LegalSwitchError("LEGAL_SWITCH_NAME_BLANK")
        if name in seen:
            raise LegalSwitchError("DUPLICATE_LEGAL_SWITCH_NAME")
        seen.add(name)
        if name not in applied.selected_three:
            raise LegalSwitchError("LEGAL_SWITCH_OUTSIDE_SELECTED_THREE")
        if name == current_active_name:
            raise LegalSwitchError("LEGAL_SWITCH_INCLUDES_CURRENT_ACTIVE")
        if _is_confirmed_fainted(local_memory_by_name.get(name)):
            raise LegalSwitchError("LEGAL_SWITCH_INCLUDES_CONFIRMED_FAINTED")

    return LegalSwitchConfirmation(
        confirmation_id=confirmation_id,
        identity=identity,
        based_on_confirmed_state_id=based_on_confirmed_state_id,
        applied_selection_id=applied.applied_selection_id,
        legal_switches=tuple(name.strip() for name in legal_switches),
        status=status,
        confirmation=confirmation,
    )
