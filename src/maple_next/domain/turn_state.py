"""Bundle A: Turn state / action-result delta / next-turn draft contract.

This module is a new, independent domain slice for Issue #31 Bundle A. It is
kept strictly separate from the legacy Turn flow in ``domain/models.py`` and
from the Lane C ``ReviewedBoardSnapshot`` provider projection: nothing here is
wired into provider requests, the operator UI, or the existing legal-action
route (``BattleApplication.record_actual_action``).

Core invariant:
    confirmed state (human-confirmed Turn-start state)
    + confirmed delta (human-confirmed observation of the completed Turn)
    -> next draft (never itself a confirmed fact, never provider-ready)
    -> requires a further explicit human confirmation to become a new
       confirmed state.

All types and functions here are pure (no I/O).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar

from maple_next.domain.enums import ActionType, HpBucket

T = TypeVar("T")

_MIN_STAGE = -6
_MAX_STAGE = 6


class TurnStateError(Exception):
    """Base fail-closed error for the turn state contract."""


class TurnStateIdentityError(TurnStateError):
    """Identity/revision binding did not match. Fail closed."""


class TurnStateStaleError(TurnStateError):
    """A draft/delta/confirmation was stale relative to current state."""


def _require(payload: dict[str, object], key: str) -> object:
    if key not in payload:
        raise TurnStateError(f"MISSING_FIELD:{key}")
    return payload[key]


def _validate_field_text(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("field text must be a non-empty string")


def _validate_stage_value(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("stat stage value must be an int")
    if not _MIN_STAGE <= value <= _MAX_STAGE:
        raise ValueError("stat stage must be between -6 and +6")


def _validate_side_effects(value: object) -> None:
    if not isinstance(value, tuple):
        raise ValueError("side effects must be a tuple")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("side effect entries must be non-empty strings")


class KnowledgeStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class ProvenanceStep(StrEnum):
    """One step in a field value's ordered provenance chain."""

    HUMAN_INPUT = "HUMAN_INPUT"
    OCR_CANDIDATE = "OCR_CANDIDATE"
    HUMAN_CORRECTION = "HUMAN_CORRECTION"
    PREVIOUS_CONFIRMED_CARRY_FORWARD = "PREVIOUS_CONFIRMED_CARRY_FORWARD"
    UNKNOWN = "UNKNOWN"


def _validate_provenance_chain(chain: object) -> None:
    if not isinstance(chain, tuple) or not chain:
        raise ValueError("provenance_chain must be a non-empty tuple")
    for step in chain:
        if not isinstance(step, ProvenanceStep):
            raise ValueError("provenance_chain entries must be ProvenanceStep values")


@dataclass(frozen=True, slots=True)
class Known(Generic[T]):
    """Per-field knowledge: CONFIRMED carries a value, UNKNOWN carries none.

    A CONFIRMED value may itself be the canonical text ``"UNKNOWN"`` only when
    a human explicitly confirms the underlying game fact is unknown to them.
    That is distinct from this wrapper's own ``UNKNOWN`` status, which means
    no human confirmation exists for the field at all (never a silent
    default such as stat stage 0, status ``NONE``, or an empty collection).

    ``provenance_chain`` is a non-empty, ordered record of how this value was
    arrived at (e.g. an OCR candidate a human then corrected). It is separate
    from -- and never a substitute for -- the record-level
    :class:`ConfirmationMeta`.
    """

    status: KnowledgeStatus
    value: T | None = None
    provenance_chain: tuple[ProvenanceStep, ...] = ()

    def __post_init__(self) -> None:
        if self.status is KnowledgeStatus.CONFIRMED and self.value is None:
            raise ValueError("CONFIRMED knowledge requires an explicit value")
        if self.status is KnowledgeStatus.UNKNOWN and self.value is not None:
            raise ValueError("UNKNOWN knowledge must not carry a value")
        _validate_provenance_chain(self.provenance_chain)

    @classmethod
    def confirmed(cls, value: T, *, provenance_chain: tuple[ProvenanceStep, ...]) -> Known[T]:
        return cls(KnowledgeStatus.CONFIRMED, value, provenance_chain)

    @classmethod
    def unknown(
        cls,
        *,
        provenance_chain: tuple[ProvenanceStep, ...] = (ProvenanceStep.UNKNOWN,),
    ) -> Known[T]:
        return cls(KnowledgeStatus.UNKNOWN, None, provenance_chain)

    @property
    def is_confirmed(self) -> bool:
        return self.status is KnowledgeStatus.CONFIRMED


class ChangeObservation(StrEnum):
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class FieldDelta(Generic[T]):
    """One tracked field's observation of a completed Turn's result.

    ``CHANGED`` must carry the explicit resulting value. ``UNCHANGED`` and
    ``UNKNOWN`` never carry a value: ``UNCHANGED`` means the lifecycle must
    carry forward the prior confirmed value, ``UNKNOWN`` means the lifecycle
    must produce an unknown draft value. Field omission (this object simply
    absent from an ``ActionResultDelta``) is a distinct, unrepresentable
    state -- it is not encoded as ``UNCHANGED``.
    """

    observation: ChangeObservation
    after_value: T | None = None
    provenance_chain: tuple[ProvenanceStep, ...] = ()

    def __post_init__(self) -> None:
        if self.observation is ChangeObservation.CHANGED and self.after_value is None:
            raise ValueError("CHANGED delta requires an explicit resulting value")
        if self.observation is not ChangeObservation.CHANGED and self.after_value is not None:
            raise ValueError("only a CHANGED delta may carry a resulting value")
        _validate_provenance_chain(self.provenance_chain)

    @classmethod
    def changed(cls, value: T, *, provenance_chain: tuple[ProvenanceStep, ...]) -> FieldDelta[T]:
        return cls(ChangeObservation.CHANGED, value, provenance_chain)

    @classmethod
    def unchanged(
        cls,
        *,
        provenance_chain: tuple[ProvenanceStep, ...] = (
            ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,
        ),
    ) -> FieldDelta[T]:
        return cls(ChangeObservation.UNCHANGED, None, provenance_chain)

    @classmethod
    def unknown(
        cls,
        *,
        provenance_chain: tuple[ProvenanceStep, ...] = (ProvenanceStep.UNKNOWN,),
    ) -> FieldDelta[T]:
        return cls(ChangeObservation.UNKNOWN, None, provenance_chain)


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Identity/revision binding shared by state, delta, and draft."""

    session_id: str
    match_id: str
    generation: int
    turn_id: str
    turn_number: int
    battle_revision: int

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.match_id.strip() or not self.turn_id.strip():
            raise ValueError("identity ids must be explicit")
        if self.turn_number < 1:
            raise ValueError("turn number must be positive")
        if self.battle_revision < 0:
            raise ValueError("battle revision must be non-negative")


@dataclass(frozen=True, slots=True)
class ConfirmationMeta:
    """Human-confirmation metadata. Never optional -- a draft never has one."""

    confirmed_by_human: bool
    confirmed_at_utc: str
    provenance: str

    def __post_init__(self) -> None:
        if not self.confirmed_by_human:
            raise ValueError("EXPLICIT_HUMAN_CONFIRMATION_REQUIRED")
        if not self.confirmed_at_utc.strip():
            raise ValueError("confirmation timestamp must be explicit")
        if not self.provenance.strip():
            raise ValueError("provenance must be explicit")


@dataclass(frozen=True, slots=True)
class SideState:
    """One side's (self or opponent) confirmed-knowledge state."""

    active: Known[str]
    hp_bucket: Known[HpBucket]
    status: Known[str]
    attack_stage: Known[int]
    defense_stage: Known[int]
    special_attack_stage: Known[int]
    special_defense_stage: Known[int]
    speed_stage: Known[int]
    accuracy_stage: Known[int]
    evasion_stage: Known[int]
    side_effects: Known[tuple[str, ...]]

    def __post_init__(self) -> None:
        if self.active.is_confirmed:
            _validate_field_text(self.active.value)
        if self.status.is_confirmed:
            _validate_field_text(self.status.value)
        for stage in self._stages():
            if stage.is_confirmed:
                _validate_stage_value(stage.value)
        if self.side_effects.is_confirmed:
            _validate_side_effects(self.side_effects.value)

    def _stages(self) -> tuple[Known[int], ...]:
        return (
            self.attack_stage,
            self.defense_stage,
            self.special_attack_stage,
            self.special_defense_stage,
            self.speed_stage,
            self.accuracy_stage,
            self.evasion_stage,
        )


@dataclass(frozen=True, slots=True)
class SideDelta:
    """One side's observed delta for a completed Turn."""

    active: FieldDelta[str]
    hp_bucket: FieldDelta[HpBucket]
    status: FieldDelta[str]
    attack_stage: FieldDelta[int]
    defense_stage: FieldDelta[int]
    special_attack_stage: FieldDelta[int]
    special_defense_stage: FieldDelta[int]
    speed_stage: FieldDelta[int]
    accuracy_stage: FieldDelta[int]
    evasion_stage: FieldDelta[int]
    side_effects: FieldDelta[tuple[str, ...]]

    def __post_init__(self) -> None:
        if self.active.observation is ChangeObservation.CHANGED:
            _validate_field_text(self.active.after_value)
        if self.status.observation is ChangeObservation.CHANGED:
            _validate_field_text(self.status.after_value)
        for stage in self._stages():
            if stage.observation is ChangeObservation.CHANGED:
                _validate_stage_value(stage.after_value)
        if self.side_effects.observation is ChangeObservation.CHANGED:
            _validate_side_effects(self.side_effects.after_value)

    def _stages(self) -> tuple[FieldDelta[int], ...]:
        return (
            self.attack_stage,
            self.defense_stage,
            self.special_attack_stage,
            self.special_defense_stage,
            self.speed_stage,
            self.accuracy_stage,
            self.evasion_stage,
        )


@dataclass(frozen=True, slots=True)
class FixedEvidenceMetadata:
    """Reference to a fixed evidence image stored outside the repository."""

    evidence_id: str
    relative_path: str
    sha256: str
    recorded_at_utc: str

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id must be explicit")
        if not self.relative_path.strip():
            raise ValueError("relative_path must be explicit")
        if not self.sha256.strip():
            raise ValueError("sha256 must be explicit")


@dataclass(frozen=True, slots=True)
class ConfirmedTurnState:
    """A human-confirmed Turn-start state. Append-only once persisted."""

    confirmed_state_id: str
    identity: TurnIdentity
    previous_confirmed_state_id: str | None
    self_side: SideState
    opponent_side: SideState
    weather: Known[str]
    terrain: Known[str]
    confirmation: ConfirmationMeta
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if not self.confirmed_state_id.strip():
            raise ValueError("confirmed_state_id must be explicit")
        if self.weather.is_confirmed:
            _validate_field_text(self.weather.value)
        if self.terrain.is_confirmed:
            _validate_field_text(self.terrain.value)


@dataclass(frozen=True, slots=True)
class ActionResultDelta:
    """A human-confirmed observation of the result of a completed Turn."""

    delta_id: str
    identity: TurnIdentity
    based_on_confirmed_state_id: str
    self_side: SideDelta
    opponent_side: SideDelta
    weather: FieldDelta[str]
    terrain: FieldDelta[str]
    confirmation: ConfirmationMeta

    def __post_init__(self) -> None:
        if not self.delta_id.strip():
            raise ValueError("delta_id must be explicit")
        if not self.based_on_confirmed_state_id.strip():
            raise ValueError("based_on_confirmed_state_id must be explicit")
        if self.weather.observation is ChangeObservation.CHANGED:
            _validate_field_text(self.weather.after_value)
        if self.terrain.observation is ChangeObservation.CHANGED:
            _validate_field_text(self.terrain.after_value)


@dataclass(frozen=True, slots=True)
class NextTurnStateDraft:
    """Derived from a confirmed state + confirmed delta. Never a fact.

    A draft is never provider-ready and always requires a further explicit
    human confirmation before it may become a :class:`ConfirmedTurnState`.
    """

    draft_id: str
    identity: TurnIdentity
    based_on_confirmed_state_id: str
    source_delta_id: str
    self_side: SideState
    opponent_side: SideState
    weather: Known[str]
    terrain: Known[str]
    derived_at_utc: str
    provider_ready: bool = False

    def __post_init__(self) -> None:
        if self.provider_ready:
            raise ValueError("NEXT_TURN_DRAFT_MUST_NOT_BE_PROVIDER_READY")
        if not self.draft_id.strip():
            raise ValueError("draft_id must be explicit")
        if not self.derived_at_utc.strip():
            raise ValueError("derived_at_utc must be explicit")


def _bind_delta_to_confirmed_state(previous: ConfirmedTurnState, delta: ActionResultDelta) -> None:
    if delta.based_on_confirmed_state_id != previous.confirmed_state_id:
        raise TurnStateStaleError("DELTA_NOT_BASED_ON_CONFIRMED_STATE")
    identity = delta.identity
    reference = previous.identity
    if identity.session_id != reference.session_id:
        raise TurnStateIdentityError("SESSION_MISMATCH")
    if identity.match_id != reference.match_id:
        raise TurnStateIdentityError("MATCH_MISMATCH")
    if identity.generation != reference.generation:
        raise TurnStateIdentityError("GENERATION_MISMATCH")
    if identity.turn_id != reference.turn_id or identity.turn_number != reference.turn_number:
        raise TurnStateIdentityError("TURN_MISMATCH")
    if identity.battle_revision != reference.battle_revision:
        raise TurnStateIdentityError("REVISION_MISMATCH")


def _bind_next_identity(previous_identity: TurnIdentity, next_identity: TurnIdentity) -> None:
    """Bind a draft's identity to the confirmed state it is derived from.

    ``battle_revision`` is a durable *global* mutation-revision counter
    (bumped by every accepted state-changing command across the whole
    session -- Selection facts, Turn facts, Turn Advice application, actual
    action recording, Turn advancement, ...), not a Turn-scoped sequence.
    00 design decision (Issue #31 comment 5217661584): the next-turn rule is
    therefore "strictly greater than the previous confirmed state's
    revision", not "exactly +1" -- a real derivation may legitimately
    observe several durable bumps between one Turn's ConfirmedTurnState and
    the next Turn's identity. ``turn_number`` remains exactly +1: each Turn
    transition advances the Turn sequence by exactly one, regardless of how
    many global mutation revisions occurred within it.
    """

    if next_identity.session_id != previous_identity.session_id:
        raise TurnStateIdentityError("SESSION_MISMATCH")
    if next_identity.match_id != previous_identity.match_id:
        raise TurnStateIdentityError("MATCH_MISMATCH")
    if next_identity.generation != previous_identity.generation:
        raise TurnStateIdentityError("GENERATION_MISMATCH")
    if next_identity.turn_number != previous_identity.turn_number + 1:
        raise TurnStateIdentityError("TURN_MISMATCH")
    if next_identity.battle_revision <= previous_identity.battle_revision:
        raise TurnStateIdentityError("REVISION_MISMATCH")
    if next_identity.turn_id == previous_identity.turn_id:
        raise TurnStateIdentityError("TURN_ID_NOT_ADVANCED")


def validate_turn_state_full_chain(
    confirmed: ConfirmedTurnState,
    delta: ActionResultDelta,
    draft: NextTurnStateDraft,
) -> None:
    """Pure full-chain validation: confirmed state -> confirmed delta -> draft.

    Shared by hydration (restart recovery) and promotion
    (:func:`confirm_next_turn_state`) so both paths reject the same set of
    corruption/staleness cases: wrong delta, wrong source delta id, wrong
    session/match/generation, wrong Turn id/number, wrong battle revision,
    and a superseded confirmed state. Fails closed on any mismatch.
    """

    _bind_delta_to_confirmed_state(confirmed, delta)
    if draft.based_on_confirmed_state_id != confirmed.confirmed_state_id:
        raise TurnStateStaleError("DRAFT_NOT_BASED_ON_CONFIRMED_STATE")
    if draft.source_delta_id != delta.delta_id:
        raise TurnStateStaleError("DRAFT_SOURCE_DELTA_MISMATCH")
    _bind_next_identity(confirmed.identity, draft.identity)


def _derive_field(previous: Known[T], delta: FieldDelta[T]) -> Known[T]:
    if delta.observation is ChangeObservation.CHANGED:
        assert delta.after_value is not None
        return Known.confirmed(delta.after_value, provenance_chain=delta.provenance_chain)
    if delta.observation is ChangeObservation.UNCHANGED:
        return Known(
            status=previous.status,
            value=previous.value,
            provenance_chain=(
                *previous.provenance_chain,
                ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,
            ),
        )
    return Known.unknown(provenance_chain=(ProvenanceStep.UNKNOWN,))


def _derive_side(previous: SideState, delta: SideDelta) -> SideState:
    return SideState(
        active=_derive_field(previous.active, delta.active),
        hp_bucket=_derive_field(previous.hp_bucket, delta.hp_bucket),
        status=_derive_field(previous.status, delta.status),
        attack_stage=_derive_field(previous.attack_stage, delta.attack_stage),
        defense_stage=_derive_field(previous.defense_stage, delta.defense_stage),
        special_attack_stage=_derive_field(
            previous.special_attack_stage, delta.special_attack_stage
        ),
        special_defense_stage=_derive_field(
            previous.special_defense_stage, delta.special_defense_stage
        ),
        speed_stage=_derive_field(previous.speed_stage, delta.speed_stage),
        accuracy_stage=_derive_field(previous.accuracy_stage, delta.accuracy_stage),
        evasion_stage=_derive_field(previous.evasion_stage, delta.evasion_stage),
        side_effects=_derive_field(previous.side_effects, delta.side_effects),
    )


def derive_next_turn_state_draft(
    previous: ConfirmedTurnState,
    delta: ActionResultDelta,
    *,
    draft_id: str,
    next_identity: TurnIdentity,
    derived_at_utc: str,
) -> NextTurnStateDraft:
    """Pure lifecycle derivation: confirmed state + confirmed delta -> draft.

    Fails closed (raises :class:`TurnStateError`) on any identity, session,
    match, generation, turn, or revision mismatch, or when the delta is not
    based on this exact confirmed state.
    """

    _bind_delta_to_confirmed_state(previous, delta)
    _bind_next_identity(previous.identity, next_identity)
    return NextTurnStateDraft(
        draft_id=draft_id,
        identity=next_identity,
        based_on_confirmed_state_id=previous.confirmed_state_id,
        source_delta_id=delta.delta_id,
        self_side=_derive_side(previous.self_side, delta.self_side),
        opponent_side=_derive_side(previous.opponent_side, delta.opponent_side),
        weather=_derive_field(previous.weather, delta.weather),
        terrain=_derive_field(previous.terrain, delta.terrain),
        derived_at_utc=derived_at_utc,
    )


def confirm_next_turn_state(
    draft: NextTurnStateDraft,
    *,
    new_confirmed_state_id: str,
    latest_confirmed_state: ConfirmedTurnState,
    source_delta: ActionResultDelta,
    confirmation: ConfirmationMeta,
    evidence_id: str | None = None,
) -> ConfirmedTurnState:
    """Human confirmation that promotes a draft into a new confirmed state.

    Validates the full ``latest_confirmed_state -> source_delta -> draft``
    chain (not just a state-id string comparison) and fails closed on stale
    state, wrong delta, wrong source delta id, wrong session/match/
    generation, wrong Turn id/number, wrong battle revision, or a superseded
    confirmed state. The promoted state's identity always equals the draft's
    identity.
    """

    validate_turn_state_full_chain(latest_confirmed_state, source_delta, draft)
    return ConfirmedTurnState(
        confirmed_state_id=new_confirmed_state_id,
        identity=draft.identity,
        previous_confirmed_state_id=draft.based_on_confirmed_state_id,
        self_side=draft.self_side,
        opponent_side=draft.opponent_side,
        weather=draft.weather,
        terrain=draft.terrain,
        confirmation=confirmation,
        evidence_id=evidence_id,
    )


@dataclass(frozen=True, slots=True)
class LegalActionPrefillDraft:
    """A prefilled move/switch suggestion. Draft-only, never a legal action.

    High confidence does not change this: a prefill never crosses the
    existing legal-action boundary automatically, regardless of confidence.
    """

    prefill_id: str
    identity: TurnIdentity
    based_on_confirmed_state_id: str
    action_type: ActionType
    action_name: str
    derived_at_utc: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.prefill_id.strip():
            raise ValueError("prefill_id must be explicit")
        if not self.action_name.strip():
            raise ValueError("prefill action_name must be explicit")
        if not self.based_on_confirmed_state_id.strip():
            raise ValueError("based_on_confirmed_state_id must be explicit")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ConfirmedLegalActionSelection:
    """A final, human-confirmed legal action. May feed the existing route."""

    confirmation_id: str
    identity: TurnIdentity
    action_type: ActionType
    action_name: str
    confirmation: ConfirmationMeta
    source_prefill_id: str | None = None

    def __post_init__(self) -> None:
        if not self.confirmation_id.strip():
            raise ValueError("confirmation_id must be explicit")
        if not self.action_name.strip():
            raise ValueError("action_name must be explicit")


def confirm_legal_action_selection(
    *,
    confirmation_id: str,
    identity: TurnIdentity,
    action_type: ActionType,
    action_name: str,
    confirmation: ConfirmationMeta,
    prefill: LegalActionPrefillDraft | None = None,
    latest_confirmed_state_id: str | None = None,
) -> ConfirmedLegalActionSelection:
    """Create a final legal-action confirmation.

    Explicit human confirmation (``confirmation``) is required and sufficient
    on its own: a prefill is optional context, never a bypass. When a
    prefill is supplied it must match the turn identity and must still be
    based on the current latest confirmed state, or this fails closed as
    stale.
    """

    if prefill is not None:
        if prefill.identity != identity:
            raise TurnStateIdentityError("PREFILL_IDENTITY_MISMATCH")
        if (
            latest_confirmed_state_id is None
            or prefill.based_on_confirmed_state_id != latest_confirmed_state_id
        ):
            raise TurnStateStaleError("STALE_PREFILL_CONFIRMATION")
    return ConfirmedLegalActionSelection(
        confirmation_id=confirmation_id,
        identity=identity,
        action_type=action_type,
        action_name=action_name.strip(),
        confirmation=confirmation,
        source_prefill_id=prefill.prefill_id if prefill is not None else None,
    )


# --- Pure JSON-compatible (de)serialization -------------------------------
#
# Explicit key access via ``_require`` below means a missing key always
# raises rather than silently defaulting (e.g. a missing observation must
# never be read back as UNCHANGED).


def _provenance_chain_to_json(chain: tuple[ProvenanceStep, ...]) -> list[str]:
    return [step.value for step in chain]


def _provenance_chain_from_json(payload: dict[str, object]) -> tuple[ProvenanceStep, ...]:
    # Fail closed: a missing provenance_chain key is never silently defaulted.
    raw = _require(payload, "provenance_chain")
    if not isinstance(raw, list) or not raw:
        raise TurnStateError("PROVENANCE_CHAIN_MUST_BE_A_NON_EMPTY_LIST")
    return tuple(ProvenanceStep(str(item)) for item in raw)


def known_to_json(known: Known[Any]) -> dict[str, object]:
    provenance_chain = _provenance_chain_to_json(known.provenance_chain)
    if known.status is KnowledgeStatus.UNKNOWN:
        return {"status": "UNKNOWN", "provenance_chain": provenance_chain}
    value = known.value
    if isinstance(value, tuple):
        value = list(value)
    elif isinstance(value, StrEnum):
        value = value.value
    return {"status": "CONFIRMED", "value": value, "provenance_chain": provenance_chain}


def known_from_json(
    payload: dict[str, object],
    *,
    decode_value: Callable[[Any], Any] = str,
) -> Known[Any]:
    status = str(_require(payload, "status"))
    provenance_chain = _provenance_chain_from_json(payload)
    if status == "UNKNOWN":
        return Known(KnowledgeStatus.UNKNOWN, None, provenance_chain)
    if status != "CONFIRMED":
        raise TurnStateError(f"UNKNOWN_KNOWLEDGE_STATUS:{status}")
    raw_value = _require(payload, "value")
    decoded = decode_value(raw_value) if callable(decode_value) else raw_value
    return Known(KnowledgeStatus.CONFIRMED, decoded, provenance_chain)


def field_delta_to_json(delta: FieldDelta[Any]) -> dict[str, object]:
    payload: dict[str, object] = {
        "observation": delta.observation.value,
        "provenance_chain": _provenance_chain_to_json(delta.provenance_chain),
    }
    if delta.observation is ChangeObservation.CHANGED:
        value = delta.after_value
        if isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, StrEnum):
            value = value.value
        payload["after_value"] = value
    return payload


def field_delta_from_json(
    payload: dict[str, object],
    *,
    decode_value: Callable[[Any], Any] = str,
) -> FieldDelta[Any]:
    observation = ChangeObservation(str(_require(payload, "observation")))
    provenance_chain = _provenance_chain_from_json(payload)
    if observation is not ChangeObservation.CHANGED:
        return FieldDelta(observation, None, provenance_chain)
    raw_value = _require(payload, "after_value")
    decoded = decode_value(raw_value) if callable(decode_value) else raw_value
    return FieldDelta(ChangeObservation.CHANGED, decoded, provenance_chain)


def _decode_side_effects(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise TurnStateError("SIDE_EFFECTS_MUST_BE_A_LIST")
    return tuple(str(item) for item in raw)


def side_state_to_json(side: SideState) -> dict[str, object]:
    return {
        "active": known_to_json(side.active),
        "hp_bucket": known_to_json(side.hp_bucket),
        "status": known_to_json(side.status),
        "attack_stage": known_to_json(side.attack_stage),
        "defense_stage": known_to_json(side.defense_stage),
        "special_attack_stage": known_to_json(side.special_attack_stage),
        "special_defense_stage": known_to_json(side.special_defense_stage),
        "speed_stage": known_to_json(side.speed_stage),
        "accuracy_stage": known_to_json(side.accuracy_stage),
        "evasion_stage": known_to_json(side.evasion_stage),
        "side_effects": known_to_json(side.side_effects),
    }


def side_state_from_json(payload: dict[str, object]) -> SideState:
    def field(name: str, decode: Callable[[Any], Any] = str) -> Known[Any]:
        raw = _require(payload, name)
        if not isinstance(raw, dict):
            raise TurnStateError(f"INVALID_FIELD_SHAPE:{name}")
        return known_from_json(raw, decode_value=decode)

    return SideState(
        active=field("active"),
        hp_bucket=field("hp_bucket", HpBucket),
        status=field("status"),
        attack_stage=field("attack_stage", int),
        defense_stage=field("defense_stage", int),
        special_attack_stage=field("special_attack_stage", int),
        special_defense_stage=field("special_defense_stage", int),
        speed_stage=field("speed_stage", int),
        accuracy_stage=field("accuracy_stage", int),
        evasion_stage=field("evasion_stage", int),
        side_effects=field("side_effects", _decode_side_effects),
    )


def side_delta_to_json(side: SideDelta) -> dict[str, object]:
    return {
        "active": field_delta_to_json(side.active),
        "hp_bucket": field_delta_to_json(side.hp_bucket),
        "status": field_delta_to_json(side.status),
        "attack_stage": field_delta_to_json(side.attack_stage),
        "defense_stage": field_delta_to_json(side.defense_stage),
        "special_attack_stage": field_delta_to_json(side.special_attack_stage),
        "special_defense_stage": field_delta_to_json(side.special_defense_stage),
        "speed_stage": field_delta_to_json(side.speed_stage),
        "accuracy_stage": field_delta_to_json(side.accuracy_stage),
        "evasion_stage": field_delta_to_json(side.evasion_stage),
        "side_effects": field_delta_to_json(side.side_effects),
    }


def side_delta_from_json(payload: dict[str, object]) -> SideDelta:
    def field(name: str, decode: Callable[[Any], Any] = str) -> FieldDelta[Any]:
        raw = _require(payload, name)
        if not isinstance(raw, dict):
            raise TurnStateError(f"INVALID_FIELD_SHAPE:{name}")
        return field_delta_from_json(raw, decode_value=decode)

    return SideDelta(
        active=field("active"),
        hp_bucket=field("hp_bucket", HpBucket),
        status=field("status"),
        attack_stage=field("attack_stage", int),
        defense_stage=field("defense_stage", int),
        special_attack_stage=field("special_attack_stage", int),
        special_defense_stage=field("special_defense_stage", int),
        speed_stage=field("speed_stage", int),
        accuracy_stage=field("accuracy_stage", int),
        evasion_stage=field("evasion_stage", int),
        side_effects=field("side_effects", _decode_side_effects),
    )
