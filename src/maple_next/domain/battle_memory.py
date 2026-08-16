"""Bundle 3 (Gemini V2): confirmed prior battle memory + canonical selected-three builds.

This module is a **pure domain slice** (no I/O, no database table, no
schema migration). It answers exactly two Bundle 3 questions:

1. *What did the human actually confirm about the completed prior Turns of
   this match?* -- :class:`BattleMemory` / :func:`build_battle_memory`.
2. *What is the canonical build of exactly the three Pokemon that were
   actually brought?* -- :class:`SelectedBuildMember` /
   :func:`project_selected_three_builds`.

Both answers are assembled **only** from facts that already crossed an
explicit human-confirmation boundary:

- :class:`~maple_next.domain.turn_state.ConfirmedTurnState` (Turn-start
  state a human confirmed),
- the durable rich actual-action completion for a completed Turn
  (:class:`RichActionCompletionFacts`, a thin typed view of one
  ``rich_action_completions`` row -- the row itself is written only through
  the Bundle 1 Battle Record confirmation boundary),
- the reviewed
  :class:`~maple_next.domain.turn_state.ActionResultDelta` that completion
  is linked to,
- the reviewed :class:`~maple_next.domain.models.SelectionFacts` self-team
  build and its existing SHA-256.

Never accepted as truth here: raw provider advice, provider opponent
predictions, OCR candidates or prefill drafts, an unreviewed
``NextTurnStateDraft``, Opponent INTEL, or any inferred opponent action.
"Inferred" is not a representable knowledge status: when only inference
exists, the value is serialized as ``UNKNOWN``.

Result changes are **not** re-modelled here. Battle memory carries the
reviewed :class:`ActionResultDelta` itself, so switch / faint / HP / status
/ stage / side-effect consequences keep their existing canonical
``CHANGED`` / ``UNCHANGED`` / ``UNKNOWN`` semantics and their existing
provenance chains -- there is no second, parallel result-change
representation and no redundant event stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from maple_next.domain.enums import ActionOrder, ActionType
from maple_next.domain.team_build import ChampionsPokemonBuild, ChampionsTeamBuild
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmedTurnState,
    KnowledgeStatus,
    Known,
    TurnIdentity,
    field_delta_to_json,
    known_to_json,
    side_delta_to_json,
)


class Bundle3ContextError(Exception):
    """Fail-closed base error for Bundle 3 confirmed-context assembly."""


class SelectedBuildContextError(Bundle3ContextError):
    """The selected-three build context could not be proven. Fail closed."""


class BattleMemoryError(Bundle3ContextError):
    """Confirmed prior battle memory could not be proven. Fail closed."""


# --- Selected-three canonical build context ---------------------------------


class SelectedBuildStatus(StrEnum):
    """Whether a canonical detailed build exists for one selected member.

    ``CONFIRMED`` means a reviewed, SHA-validated
    :class:`~maple_next.domain.team_build.ChampionsPokemonBuild` exists for
    this exact name. ``UNKNOWN`` means only the name is canonical -- it is
    never a licence to substitute a party-six member, a recommendation, or
    Opponent INTEL for the missing detail.
    """

    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SelectedBuildMember:
    """One of exactly three actually-brought Pokemon and its canonical build."""

    pokemon_name: str
    build_status: SelectedBuildStatus
    build: ChampionsPokemonBuild | None

    def __post_init__(self) -> None:
        if not self.pokemon_name.strip():
            raise SelectedBuildContextError("SELECTED_BUILD_MEMBER_NAME_REQUIRED")
        if self.build_status is SelectedBuildStatus.CONFIRMED:
            if self.build is None:
                raise SelectedBuildContextError("CONFIRMED_SELECTED_BUILD_REQUIRES_BUILD")
            if self.build.pokemon_name != self.pokemon_name:
                raise SelectedBuildContextError("SELECTED_BUILD_NAME_MISMATCH")
        elif self.build is not None:
            raise SelectedBuildContextError("UNKNOWN_SELECTED_BUILD_MUST_NOT_CARRY_BUILD")


def project_selected_three_builds(
    *,
    selected_three: tuple[str, str, str],
    reviewed_self_team: tuple[str, ...],
    reviewed_self_team_build: ChampionsTeamBuild | None,
    reviewed_self_team_build_sha256: str | None,
) -> tuple[SelectedBuildMember, ...]:
    """Project exactly the applied ``selected_three``, in applied order.

    Fails closed when a selected name is outside the reviewed self team,
    when a detailed build's existing SHA-256 does not validate, when the
    detailed build's six names do not match the reviewed self team, or when
    a selected member is missing/duplicated/outside that build. Never falls
    back to the party six, to a recommendation, or to Opponent INTEL to fill
    a gap: a name with no canonical detail is reported as ``UNKNOWN`` with
    ``build=None`` (a non-blocking condition, not a substitution licence).
    """

    if len(selected_three) != 3 or len(set(selected_three)) != 3:
        raise SelectedBuildContextError("SELECTED_THREE_MUST_CONTAIN_THREE_UNIQUE_NAMES")
    for name in selected_three:
        if name not in reviewed_self_team:
            raise SelectedBuildContextError("SELECTED_MEMBER_OUTSIDE_REVIEWED_SELF_TEAM")

    if reviewed_self_team_build is None:
        if reviewed_self_team_build_sha256 is not None:
            raise SelectedBuildContextError("SELF_TEAM_BUILD_HASH_WITHOUT_BUILD")
        return tuple(
            SelectedBuildMember(
                pokemon_name=name, build_status=SelectedBuildStatus.UNKNOWN, build=None
            )
            for name in selected_three
        )

    if reviewed_self_team_build_sha256 is None:
        raise SelectedBuildContextError("SELF_TEAM_BUILD_WITHOUT_HASH")
    # Reuse the existing canonical SHA-256, never a freshly invented digest.
    if reviewed_self_team_build.sha256() != reviewed_self_team_build_sha256:
        raise SelectedBuildContextError("SELF_TEAM_BUILD_SHA256_MISMATCH")
    if reviewed_self_team_build.pokemon_names != tuple(reviewed_self_team):
        raise SelectedBuildContextError("SELF_TEAM_BUILD_NAMES_MISMATCH")
    try:
        # Existing canonical selection semantics -- missing, duplicate, or
        # outside-the-team members all raise here.
        members = reviewed_self_team_build.selected_members(selected_three)
    except ValueError as exc:
        raise SelectedBuildContextError(f"SELECTED_MEMBER_NOT_IN_TEAM_BUILD:{exc}") from exc
    if len(members) != 3:  # pragma: no cover - selected_members already enforces this
        raise SelectedBuildContextError("SELECTED_BUILD_PROJECTION_MUST_CONTAIN_THREE")
    return tuple(
        SelectedBuildMember(
            pokemon_name=member.pokemon_name,
            build_status=SelectedBuildStatus.CONFIRMED,
            build=member,
        )
        for member in members
    )


def selected_build_member_to_canonical_dict(member: SelectedBuildMember) -> dict[str, Any]:
    """Deterministic, JSON-compatible dict for one selected build member."""

    return {
        "pokemon_name": member.pokemon_name,
        "build_status": member.build_status.value,
        "build": member.build.to_canonical_dict() if member.build is not None else None,
    }


# --- Confirmed prior battle memory -------------------------------------------


@dataclass(frozen=True, slots=True)
class BattleMemoryAction:
    """One side's action for a completed Turn: confirmed, or genuinely unknown.

    ``UNKNOWN`` carries no ``action_type`` and no ``action_name``. There is
    deliberately no ``INFERRED`` status -- an inferred opponent action is
    never serialized as though a human had confirmed it.
    """

    knowledge_status: KnowledgeStatus
    action_type: ActionType | None = None
    action_name: str | None = None

    def __post_init__(self) -> None:
        if self.knowledge_status is KnowledgeStatus.CONFIRMED:
            if self.action_type is None:
                raise BattleMemoryError("CONFIRMED_ACTION_REQUIRES_ACTION_TYPE")
            if self.action_name is None or not self.action_name.strip():
                raise BattleMemoryError("CONFIRMED_ACTION_REQUIRES_ACTION_NAME")
        else:
            if self.action_type is not None or self.action_name is not None:
                raise BattleMemoryError("UNKNOWN_ACTION_MUST_NOT_CARRY_A_VALUE")

    @classmethod
    def confirmed(cls, action_type: ActionType, action_name: str) -> BattleMemoryAction:
        return cls(KnowledgeStatus.CONFIRMED, action_type, action_name)

    @classmethod
    def unknown(cls) -> BattleMemoryAction:
        return cls(KnowledgeStatus.UNKNOWN, None, None)


@dataclass(frozen=True, slots=True)
class RichActionCompletionFacts:
    """Thin typed view of one durable ``rich_action_completions`` row.

    Deliberately a read model only: it adds no new authority and creates no
    new persistence. The existing untyped completion getter is left exactly
    as it is.
    """

    transaction_id: str
    identity: TurnIdentity
    based_on_confirmed_state_id: str
    own_action_type: ActionType
    own_action_name: str
    opponent_action_type: ActionType | None
    opponent_action_name: str | None
    action_order: ActionOrder
    delta_id: str

    def __post_init__(self) -> None:
        if not self.transaction_id.strip():
            raise BattleMemoryError("COMPLETION_TRANSACTION_ID_REQUIRED")
        if not self.based_on_confirmed_state_id.strip():
            raise BattleMemoryError("COMPLETION_BASED_ON_CONFIRMED_STATE_ID_REQUIRED")
        if not self.own_action_name.strip():
            raise BattleMemoryError("COMPLETION_OWN_ACTION_NAME_REQUIRED")
        if not self.delta_id.strip():
            raise BattleMemoryError("COMPLETION_DELTA_ID_REQUIRED")


@dataclass(frozen=True, slots=True)
class BattleMemoryTurn:
    """One completed prior Turn, carrying only confirmed/reviewed facts."""

    identity: TurnIdentity
    reviewed_confirmed_state_id: str
    turn_start_self_active: Known[str]
    turn_start_opponent_active: Known[str]
    own_action: BattleMemoryAction
    opponent_action: BattleMemoryAction
    action_order: ActionOrder
    result: ActionResultDelta

    def __post_init__(self) -> None:
        if not self.reviewed_confirmed_state_id.strip():
            raise BattleMemoryError("MEMORY_REVIEWED_CONFIRMED_STATE_ID_REQUIRED")
        if self.own_action.knowledge_status is not KnowledgeStatus.CONFIRMED:
            raise BattleMemoryError("MEMORY_OWN_ACTION_MUST_BE_CONFIRMED")


@dataclass(frozen=True, slots=True)
class BattleMemory:
    """Completed prior Turns of the current match, strictly chronological.

    For current Turn N this holds exactly completed Turns 1..N-1, ordered by
    ``turn_number`` ascending, independent of database insertion order. Turn
    1 legitimately has an empty memory -- that is never a send blocker.
    """

    turns: tuple[BattleMemoryTurn, ...] = ()

    def __post_init__(self) -> None:
        expected = 1
        for row in self.turns:
            if row.identity.turn_number != expected:
                raise BattleMemoryError("MEMORY_TURNS_MUST_BE_CONTIGUOUS_AND_ASCENDING")
            expected += 1

    @property
    def turn_numbers(self) -> tuple[int, ...]:
        return tuple(row.identity.turn_number for row in self.turns)


def _action_to_canonical_dict(action: BattleMemoryAction) -> dict[str, Any]:
    return {
        "knowledge_status": action.knowledge_status.value,
        "action_type": action.action_type.value if action.action_type is not None else None,
        "action_name": action.action_name,
    }


def _result_delta_to_canonical_dict(delta: ActionResultDelta) -> dict[str, Any]:
    """Reuse the existing canonical delta serialization -- no parallel shape."""

    return {
        "delta_id": delta.delta_id,
        "based_on_confirmed_state_id": delta.based_on_confirmed_state_id,
        "self_side": side_delta_to_json(delta.self_side),
        "opponent_side": side_delta_to_json(delta.opponent_side),
        "weather": field_delta_to_json(delta.weather),
        "terrain": field_delta_to_json(delta.terrain),
        "confirmation": {
            "confirmed_by_human": delta.confirmation.confirmed_by_human,
            "confirmed_at_utc": delta.confirmation.confirmed_at_utc,
            "provenance": delta.confirmation.provenance,
        },
    }


def battle_memory_turn_to_canonical_dict(row: BattleMemoryTurn) -> dict[str, Any]:
    """Deterministic, JSON-compatible dict for one completed prior Turn."""

    return {
        "session_id": row.identity.session_id,
        "match_id": row.identity.match_id,
        "generation": row.identity.generation,
        "turn_id": row.identity.turn_id,
        "turn_number": row.identity.turn_number,
        "battle_revision": row.identity.battle_revision,
        "reviewed_confirmed_state_id": row.reviewed_confirmed_state_id,
        "turn_start_self_active": known_to_json(row.turn_start_self_active),
        "turn_start_opponent_active": known_to_json(row.turn_start_opponent_active),
        "own_action": _action_to_canonical_dict(row.own_action),
        "opponent_action": _action_to_canonical_dict(row.opponent_action),
        "action_order": row.action_order.value,
        "result": _result_delta_to_canonical_dict(row.result),
    }


def battle_memory_to_canonical_list(memory: BattleMemory) -> list[dict[str, Any]]:
    """Deterministic, chronological JSON-compatible battle memory."""

    return [battle_memory_turn_to_canonical_dict(row) for row in memory.turns]


def _require_same_match(
    identity: TurnIdentity, reference: TurnIdentity, *, error_code: str
) -> None:
    if (
        identity.session_id != reference.session_id
        or identity.match_id != reference.match_id
        or identity.generation != reference.generation
    ):
        raise BattleMemoryError(error_code)


def _walk_confirmed_state_chain(
    *,
    current_confirmed_state: ConfirmedTurnState,
    states_by_id: dict[str, ConfirmedTurnState],
) -> dict[str, int]:
    """Walk ``previous_confirmed_state_id`` back to the root, oldest-first index.

    A re-confirmation of the same Turn legitimately inserts an extra link in
    this chain, so position -- not ``turn_number`` arithmetic -- is what
    proves a prior reviewed state really precedes the current one. A missing
    link, or a cycle, fails closed.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    cursor: str | None = current_confirmed_state.confirmed_state_id
    while cursor is not None:
        if cursor in seen:
            raise BattleMemoryError("MEMORY_CONFIRMED_STATE_CHAIN_CYCLE")
        state = states_by_id.get(cursor)
        if state is None:
            raise BattleMemoryError("MEMORY_BROKEN_CONFIRMED_STATE_CHAIN")
        seen.add(cursor)
        ordered.append(cursor)
        cursor = state.previous_confirmed_state_id
    ordered.reverse()
    return {confirmed_state_id: index for index, confirmed_state_id in enumerate(ordered)}


def build_battle_memory(
    *,
    current_identity: TurnIdentity,
    current_confirmed_state: ConfirmedTurnState,
    match_confirmed_states: tuple[ConfirmedTurnState, ...],
    completion_candidates: tuple[RichActionCompletionFacts, ...],
    delta_candidates: tuple[ActionResultDelta, ...],
) -> BattleMemory:
    """Assemble confirmed memory for completed Turns 1..N-1, or fail closed.

    Candidates are deliberately passed in **unfiltered** (keyed only by the
    confirmed-state ids of this match) so a corrupt or foreign row cannot
    disappear before validation ever sees it -- hiding an integrity problem
    is exactly the failure mode this gate exists to prevent.

    Fails closed on: a foreign session/match/generation row; a completion or
    delta for the current or a future Turn; a duplicate Turn, completion, or
    delta; a missing completion or missing linked reviewed delta for an
    otherwise completed prior Turn; an identity/revision disagreement
    between a completion, its delta, and its reviewed confirmed state; and a
    broken prior confirmed-state chain.
    """

    if current_confirmed_state.identity != current_identity:
        raise BattleMemoryError("MEMORY_CURRENT_STATE_IDENTITY_MISMATCH")

    states_by_id: dict[str, ConfirmedTurnState] = {}
    for state in match_confirmed_states:
        _require_same_match(
            state.identity, current_identity, error_code="MEMORY_FOREIGN_CONFIRMED_STATE"
        )
        if state.confirmed_state_id in states_by_id:
            raise BattleMemoryError("MEMORY_DUPLICATE_CONFIRMED_STATE")
        states_by_id[state.confirmed_state_id] = state
    if states_by_id.get(current_confirmed_state.confirmed_state_id) != current_confirmed_state:
        raise BattleMemoryError("MEMORY_CURRENT_STATE_NOT_IN_MATCH_HISTORY")

    chain_position = _walk_confirmed_state_chain(
        current_confirmed_state=current_confirmed_state, states_by_id=states_by_id
    )
    current_position = chain_position[current_confirmed_state.confirmed_state_id]

    completion_by_turn: dict[int, RichActionCompletionFacts] = {}
    for completion in completion_candidates:
        _require_same_match(
            completion.identity, current_identity, error_code="MEMORY_FOREIGN_COMPLETION"
        )
        if completion.identity.turn_number >= current_identity.turn_number:
            raise BattleMemoryError("MEMORY_CURRENT_OR_FUTURE_TURN_COMPLETION")
        if completion.identity.turn_number in completion_by_turn:
            raise BattleMemoryError("MEMORY_DUPLICATE_TURN_COMPLETION")
        completion_by_turn[completion.identity.turn_number] = completion

    deltas_by_id: dict[str, ActionResultDelta] = {}
    deltas_per_state: dict[str, int] = {}
    for delta in delta_candidates:
        if delta.delta_id in deltas_by_id:
            raise BattleMemoryError("MEMORY_DUPLICATE_DELTA_ID")
        deltas_by_id[delta.delta_id] = delta
        deltas_per_state[delta.based_on_confirmed_state_id] = (
            deltas_per_state.get(delta.based_on_confirmed_state_id, 0) + 1
        )

    rows: list[BattleMemoryTurn] = []
    previous_position = -1
    for turn_number in range(1, current_identity.turn_number):
        turn_completion = completion_by_turn.get(turn_number)
        if turn_completion is None:
            raise BattleMemoryError("MEMORY_MISSING_COMPLETED_TURN_COMPLETION")

        reviewed_state_id = turn_completion.based_on_confirmed_state_id
        reviewed_state = states_by_id.get(reviewed_state_id)
        if reviewed_state is None:
            raise BattleMemoryError("MEMORY_COMPLETION_CONFIRMED_STATE_NOT_FOUND")
        if reviewed_state.identity != turn_completion.identity:
            raise BattleMemoryError("MEMORY_COMPLETION_CONFIRMED_STATE_IDENTITY_MISMATCH")

        position = chain_position.get(reviewed_state_id)
        if position is None:
            raise BattleMemoryError("MEMORY_CONFIRMED_STATE_OUTSIDE_CURRENT_CHAIN")
        if position <= previous_position or position >= current_position:
            raise BattleMemoryError("MEMORY_BROKEN_CONFIRMED_STATE_CHAIN")
        previous_position = position

        linked_delta = deltas_by_id.get(turn_completion.delta_id)
        if linked_delta is None:
            raise BattleMemoryError("MEMORY_MISSING_LINKED_ACTION_RESULT_DELTA")
        if linked_delta.based_on_confirmed_state_id != reviewed_state_id:
            raise BattleMemoryError("MEMORY_DELTA_NOT_BASED_ON_REVIEWED_STATE")
        if linked_delta.identity != turn_completion.identity:
            raise BattleMemoryError("MEMORY_DELTA_IDENTITY_MISMATCH")
        if deltas_per_state.get(reviewed_state_id, 0) != 1:
            raise BattleMemoryError("MEMORY_AMBIGUOUS_ACTION_RESULT_DELTA")

        opponent_action = (
            BattleMemoryAction.confirmed(
                turn_completion.opponent_action_type, turn_completion.opponent_action_name or ""
            )
            if turn_completion.opponent_action_type is not None
            else BattleMemoryAction.unknown()
        )
        rows.append(
            BattleMemoryTurn(
                identity=turn_completion.identity,
                reviewed_confirmed_state_id=reviewed_state_id,
                turn_start_self_active=reviewed_state.self_side.active,
                turn_start_opponent_active=reviewed_state.opponent_side.active,
                own_action=BattleMemoryAction.confirmed(
                    turn_completion.own_action_type, turn_completion.own_action_name
                ),
                opponent_action=opponent_action,
                action_order=turn_completion.action_order,
                result=linked_delta,
            )
        )
    return BattleMemory(turns=tuple(rows))


@dataclass(frozen=True, slots=True)
class Bundle3TurnContext:
    """Everything Bundle 3 adds to one rich Turn Advice request.

    Assembled once by the shared application helper and reused unchanged by
    both initial request creation and offline rebuild/export verification,
    so a single set of business rules governs both paths.
    """

    applied_selection_id: str
    reviewed_selection_id: str
    selected_three_builds: tuple[SelectedBuildMember, ...]
    battle_memory: BattleMemory

    def __post_init__(self) -> None:
        if not self.applied_selection_id.strip():
            raise Bundle3ContextError("APPLIED_SELECTION_ID_REQUIRED")
        if not self.reviewed_selection_id.strip():
            raise Bundle3ContextError("REVIEWED_SELECTION_ID_REQUIRED")
        if len(self.selected_three_builds) != 3:
            raise SelectedBuildContextError("SELECTED_THREE_BUILDS_MUST_CONTAIN_THREE")
        names = tuple(member.pokemon_name for member in self.selected_three_builds)
        if len(set(names)) != 3:
            raise SelectedBuildContextError("SELECTED_THREE_BUILDS_MUST_BE_DISTINCT")
