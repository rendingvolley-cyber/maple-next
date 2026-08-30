"""Canonical human-confirmed Mega Evolution state for one battle.

Selection ``intended_mega`` is planning context only.  This module represents
what the operator actually confirmed happened during the battle.  The state is
match-level, survives switches/turns/restarts, and allows each side to consume
its Mega resource at most once.

The module is pure: no persistence, UI, provider transport, or game input.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

MEGA_STATE_SCHEMA_VERSION = "maple-mega-state.v1"
MEGA_EVENT_PROVENANCE = "HUMAN_CONFIRMED_RESULT_ENTRY"


class MegaEvolutionError(ValueError):
    """Fail-closed error for malformed or contradictory Mega state."""


class MegaSide(StrEnum):
    SELF = "SELF"
    OPPONENT = "OPPONENT"


@dataclass(frozen=True, slots=True)
class MegaSideState:
    """Actual Mega usage for one side of the current match.

    ``current_form`` may be ``None`` even when ``mega_used`` is true.  That
    means the operator confirmed that Mega Evolution occurred but Maple cannot
    safely name the exact resulting form.  It must never be guessed from
    Selection intent or general assumptions.
    """

    mega_used: bool = False
    mega_pokemon: str | None = None
    current_form: str | None = None
    confirmed_turn: int | None = None
    confirmed_at_utc: str | None = None
    provenance: str | None = None

    def __post_init__(self) -> None:
        if not self.mega_used:
            if any(
                value is not None
                for value in (
                    self.mega_pokemon,
                    self.current_form,
                    self.confirmed_turn,
                    self.confirmed_at_utc,
                    self.provenance,
                )
            ):
                raise MegaEvolutionError("UNUSED_MEGA_STATE_MUST_NOT_CARRY_EVENT_DATA")
            return

        if self.mega_pokemon is None or not self.mega_pokemon.strip():
            raise MegaEvolutionError("MEGA_POKEMON_REQUIRED")
        if self.current_form is not None and not self.current_form.strip():
            raise MegaEvolutionError("MEGA_CURRENT_FORM_MUST_BE_NONBLANK_OR_NULL")
        if self.confirmed_turn is None or self.confirmed_turn < 1:
            raise MegaEvolutionError("MEGA_CONFIRMED_TURN_REQUIRED")
        if self.confirmed_at_utc is None or not self.confirmed_at_utc.strip():
            raise MegaEvolutionError("MEGA_CONFIRMED_TIME_REQUIRED")
        if self.provenance != MEGA_EVENT_PROVENANCE:
            raise MegaEvolutionError("MEGA_PROVENANCE_INVALID")

    @classmethod
    def used(
        cls,
        *,
        pokemon_name: str,
        current_form: str | None,
        confirmed_turn: int,
        confirmed_at_utc: str,
    ) -> MegaSideState:
        return cls(
            mega_used=True,
            mega_pokemon=pokemon_name.strip(),
            current_form=current_form.strip() if current_form is not None else None,
            confirmed_turn=confirmed_turn,
            confirmed_at_utc=confirmed_at_utc.strip(),
            provenance=MEGA_EVENT_PROVENANCE,
        )


@dataclass(frozen=True, slots=True)
class MegaBattleState:
    """The current match's actual Mega resource state for both sides."""

    schema_version: str = MEGA_STATE_SCHEMA_VERSION
    self_side: MegaSideState = MegaSideState()
    opponent_side: MegaSideState = MegaSideState()

    def __post_init__(self) -> None:
        if self.schema_version != MEGA_STATE_SCHEMA_VERSION:
            raise MegaEvolutionError("MEGA_STATE_SCHEMA_VERSION_UNSUPPORTED")

    def side(self, side: MegaSide) -> MegaSideState:
        return self.self_side if side is MegaSide.SELF else self.opponent_side

    def record_use(
        self,
        *,
        side: MegaSide,
        pokemon_name: str,
        current_form: str | None,
        confirmed_turn: int,
        confirmed_at_utc: str,
    ) -> MegaBattleState:
        if self.side(side).mega_used:
            raise MegaEvolutionError(f"MEGA_ALREADY_USED:{side.value}")
        confirmed = MegaSideState.used(
            pokemon_name=pokemon_name,
            current_form=current_form,
            confirmed_turn=confirmed_turn,
            confirmed_at_utc=confirmed_at_utc,
        )
        if side is MegaSide.SELF:
            return replace(self, self_side=confirmed)
        return replace(self, opponent_side=confirmed)


# Tournament self-team forms that are deterministic and currently required.
# This is deliberately not a universal Mega database.  Unknown species/forms
# return None instead of being invented.
_DETERMINISTIC_MEGA_FORMS: dict[str, str] = {
    "メタグロス": "メガメタグロス",
    "ラグラージ": "メガラグラージ",
    "Metagross": "Mega Metagross",
    "Swampert": "Mega Swampert",
}


def deterministic_mega_form(pokemon_name: str) -> str | None:
    """Return a pinned known single form, or ``None`` when not safely known."""

    return _DETERMINISTIC_MEGA_FORMS.get(pokemon_name.strip())


def _side_to_dict(side: MegaSideState) -> dict[str, Any]:
    return {
        "mega_used": side.mega_used,
        "mega_pokemon": side.mega_pokemon,
        "current_form": side.current_form,
        "confirmed_turn": side.confirmed_turn,
        "confirmed_at_utc": side.confirmed_at_utc,
        "provenance": side.provenance,
    }


def mega_state_to_canonical_dict(state: MegaBattleState) -> dict[str, Any]:
    """Deterministic JSON-compatible representation of actual Mega state."""

    return {
        "schema_version": state.schema_version,
        "self": _side_to_dict(state.self_side),
        "opponent": _side_to_dict(state.opponent_side),
    }


def _side_from_dict(payload: object, *, label: str) -> MegaSideState:
    if not isinstance(payload, dict):
        raise MegaEvolutionError(f"MEGA_SIDE_MUST_BE_OBJECT:{label}")
    expected = {
        "mega_used",
        "mega_pokemon",
        "current_form",
        "confirmed_turn",
        "confirmed_at_utc",
        "provenance",
    }
    if set(payload) != expected:
        raise MegaEvolutionError(f"MEGA_SIDE_FIELDS_INVALID:{label}")
    mega_used = payload["mega_used"]
    if not isinstance(mega_used, bool):
        raise MegaEvolutionError(f"MEGA_USED_MUST_BE_BOOL:{label}")
    pokemon = payload["mega_pokemon"]
    current_form = payload["current_form"]
    confirmed_turn = payload["confirmed_turn"]
    confirmed_at_utc = payload["confirmed_at_utc"]
    provenance = payload["provenance"]
    if pokemon is not None and not isinstance(pokemon, str):
        raise MegaEvolutionError(f"MEGA_POKEMON_INVALID:{label}")
    if current_form is not None and not isinstance(current_form, str):
        raise MegaEvolutionError(f"MEGA_CURRENT_FORM_INVALID:{label}")
    if confirmed_turn is not None and (
        not isinstance(confirmed_turn, int) or isinstance(confirmed_turn, bool)
    ):
        raise MegaEvolutionError(f"MEGA_CONFIRMED_TURN_INVALID:{label}")
    if confirmed_at_utc is not None and not isinstance(confirmed_at_utc, str):
        raise MegaEvolutionError(f"MEGA_CONFIRMED_TIME_INVALID:{label}")
    if provenance is not None and not isinstance(provenance, str):
        raise MegaEvolutionError(f"MEGA_PROVENANCE_TYPE_INVALID:{label}")
    return MegaSideState(
        mega_used=mega_used,
        mega_pokemon=pokemon,
        current_form=current_form,
        confirmed_turn=confirmed_turn,
        confirmed_at_utc=confirmed_at_utc,
        provenance=provenance,
    )


def mega_state_from_canonical_dict(payload: object) -> MegaBattleState:
    """Strictly decode persisted/provider/export Mega state.

    ``{}`` is accepted only as the additive-migration representation of a
    historical/current match with no recorded Mega event yet.
    """

    if payload == {}:
        return MegaBattleState()
    if not isinstance(payload, dict):
        raise MegaEvolutionError("MEGA_STATE_MUST_BE_OBJECT")
    if set(payload) != {"schema_version", "self", "opponent"}:
        raise MegaEvolutionError("MEGA_STATE_FIELDS_INVALID")
    if payload["schema_version"] != MEGA_STATE_SCHEMA_VERSION:
        raise MegaEvolutionError("MEGA_STATE_SCHEMA_VERSION_UNSUPPORTED")
    return MegaBattleState(
        schema_version=MEGA_STATE_SCHEMA_VERSION,
        self_side=_side_from_dict(payload["self"], label="SELF"),
        opponent_side=_side_from_dict(payload["opponent"], label="OPPONENT"),
    )
