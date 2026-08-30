"""Bundle B: ConfirmedTurnState -> Gemini-ready rich-state projection.

Pure domain-layer, additive contract for Issue #31 Bundle B. This module is
strictly independent of Bundle A's ``turn_state.py`` lifecycle logic (it only
imports the frozen dataclasses/enums defined there, never modifies them) and
of the legacy Lane C ``ReviewedBoardSnapshot`` provider projection in
``providers/turn_request.py`` / ``providers/turn_boundary.py`` (neither of
those modules is imported here, and neither imports this module). Nothing in
this module performs I/O, touches the network, or is wired into the operator
UI.

Only a :class:`~maple_next.domain.turn_state.ConfirmedTurnState` plus
human-confirmed
:class:`~maple_next.domain.turn_state.ConfirmedLegalActionSelection` entries
may become a :class:`RichStateProjection`. A ``NextTurnStateDraft``, an OCR
candidate, a raw ``ActionResultDelta`` used directly as current fact, a
turn-start free-text label, a ``human_note`` coerced into a canonical fact,
or an unconfirmed/prefill legal action can never reach this module's public
entry point -- :func:`build_rich_state_projection` fails closed
(:class:`ProjectionSourceError`) on any of those.

UNKNOWN-safety: every field elsewhere retains its ``Known[...]``
representation verbatim from the source ``ConfirmedTurnState``. Nothing here
coerces an UNKNOWN stat stage to ``0``, an UNKNOWN status/weather/terrain to
``NONE``, or UNKNOWN side effects to an empty collection. Field omission is
never inferred as UNKNOWN/NONE/empty/unchanged -- every field required by
:class:`~maple_next.domain.turn_state.SideState` must already be present on
the source ``ConfirmedTurnState`` (enforced by that dataclass's own
constructor), so no downstream guessing is possible here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from maple_next.domain.enums import ActionType
from maple_next.domain.legal_switches import LegalSwitchConfirmation, LegalSwitchStatus
from maple_next.domain.turn_state import (
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FixedEvidenceMetadata,
    Known,
    SideState,
    TurnIdentity,
    known_to_json,
    side_state_to_json,
)

#: Versioned additive contract identifier. Distinct from, and never a
#: replacement for, the legacy ``maple-turn-advice.v1``/``.v2`` contract
#: versions defined in ``providers/turn_request.py``.
RICH_STATE_PROJECTION_CONTRACT_VERSION = "maple-turn-advice-rich-state.v1"


class ProjectionError(Exception):
    """Fail-closed base error for the rich-state projection contract."""


class ProjectionSourceError(ProjectionError):
    """The candidate projection source was not a confirmed fact.

    Raised for any of: a non-``ConfirmedTurnState`` source, a non-human-
    confirmed state, or any legal action that is not itself a
    ``ConfirmedLegalActionSelection`` bound to the same identity.
    """


class ProviderReadyGateError(ProjectionError):
    """The provider-ready gate rejected a candidate rich-state request."""


@dataclass(frozen=True, slots=True)
class RichStateProjection:
    """Strict Gemini-ready projection of a single ``ConfirmedTurnState``.

    ``confirmed_legal_actions`` is the final, human-confirmed action set --
    never a prefill draft. All other fields are carried verbatim (including
    their ``Known`` CONFIRMED/UNKNOWN status and provenance chain) from the
    source ``ConfirmedTurnState``; this dataclass performs no coercion.
    """

    contract_version: str
    identity: TurnIdentity
    reviewed_confirmed_state_id: str
    previous_confirmed_state_id: str | None
    self_side: SideState
    opponent_side: SideState
    weather: Known[str]
    terrain: Known[str]
    state_confirmation: ConfirmationMeta
    evidence: FixedEvidenceMetadata | None
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...]

    def __post_init__(self) -> None:
        if self.contract_version != RICH_STATE_PROJECTION_CONTRACT_VERSION:
            raise ProjectionError("UNSUPPORTED_PROJECTION_CONTRACT_VERSION")
        if not self.reviewed_confirmed_state_id.strip():
            raise ProjectionError("REVIEWED_CONFIRMED_STATE_ID_REQUIRED")
        if not self.confirmed_legal_actions:
            raise ProjectionSourceError("NO_CONFIRMED_LEGAL_ACTIONS")
        for selection in self.confirmed_legal_actions:
            if not isinstance(selection, ConfirmedLegalActionSelection):
                raise ProjectionSourceError("UNCONFIRMED_LEGAL_ACTION_REJECTED")
            if selection.identity != self.identity:
                raise ProjectionSourceError("LEGAL_ACTION_IDENTITY_MISMATCH")


def build_rich_state_projection(
    confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    *,
    evidence: FixedEvidenceMetadata | None = None,
) -> RichStateProjection:
    """Build the strict projection. The only accepted source is exactly this pair.

    Fails closed with :class:`ProjectionSourceError` if either argument is
    not exactly the confirmed type it claims to be. This is a runtime
    ``isinstance`` check, not merely a static type hint, so it defends
    against a caller passing a ``NextTurnStateDraft``, an OCR candidate
    dict, a raw ``ActionResultDelta``, or an unconfirmed
    ``LegalActionPrefillDraft`` by duck-typing.

    ``evidence`` must be the caller-loaded ``FixedEvidenceMetadata`` whose
    ``evidence_id`` matches ``confirmed_state.evidence_id`` exactly (or
    ``None`` when the state carries no evidence reference at all) -- this
    function never hashes only ``evidence_id``, it hashes the full metadata.
    """

    if not isinstance(confirmed_state, ConfirmedTurnState):
        raise ProjectionSourceError("PROJECTION_SOURCE_MUST_BE_CONFIRMED_TURN_STATE")
    if not confirmed_state.confirmation.confirmed_by_human:
        # Unreachable via the real constructor (ConfirmationMeta itself fails
        # closed on construction) but re-checked here as defense in depth.
        raise ProjectionSourceError("CONFIRMED_STATE_NOT_HUMAN_CONFIRMED")
    for selection in confirmed_legal_actions:
        if not isinstance(selection, ConfirmedLegalActionSelection):
            raise ProjectionSourceError("UNCONFIRMED_LEGAL_ACTION_REJECTED")
    if confirmed_state.evidence_id is None:
        if evidence is not None:
            raise ProjectionSourceError("UNEXPECTED_EVIDENCE_FOR_STATE_WITHOUT_EVIDENCE_ID")
    else:
        if evidence is None or evidence.evidence_id != confirmed_state.evidence_id:
            raise ProjectionSourceError("EVIDENCE_METADATA_MISMATCH")

    return RichStateProjection(
        contract_version=RICH_STATE_PROJECTION_CONTRACT_VERSION,
        identity=confirmed_state.identity,
        reviewed_confirmed_state_id=confirmed_state.confirmed_state_id,
        previous_confirmed_state_id=confirmed_state.previous_confirmed_state_id,
        self_side=confirmed_state.self_side,
        opponent_side=confirmed_state.opponent_side,
        weather=confirmed_state.weather,
        terrain=confirmed_state.terrain,
        state_confirmation=confirmed_state.confirmation,
        evidence=evidence,
        confirmed_legal_actions=tuple(confirmed_legal_actions),
    )


class GateDenialReason(StrEnum):
    """Stable, sanitized denial reason codes for the provider-ready gate."""

    STATE_NOT_HUMAN_CONFIRMED = "STATE_NOT_HUMAN_CONFIRMED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    STALE_CONFIRMED_STATE = "STALE_CONFIRMED_STATE"
    NEWER_OPEN_DRAFT_EXISTS = "NEWER_OPEN_DRAFT_EXISTS"
    SELF_ACTIVE_UNKNOWN = "SELF_ACTIVE_UNKNOWN"
    OPPONENT_ACTIVE_UNKNOWN = "OPPONENT_ACTIVE_UNKNOWN"
    SELF_ACTIVE_LITERAL_UNKNOWN_VALUE = "SELF_ACTIVE_LITERAL_UNKNOWN_VALUE"
    OPPONENT_ACTIVE_LITERAL_UNKNOWN_VALUE = "OPPONENT_ACTIVE_LITERAL_UNKNOWN_VALUE"
    LEGAL_ACTIONS_EMPTY = "LEGAL_ACTIONS_EMPTY"
    LEGAL_ACTIONS_NOT_CONFIRMED = "LEGAL_ACTIONS_NOT_CONFIRMED"
    LEGAL_ACTIONS_IDENTITY_MISMATCH = "LEGAL_ACTIONS_IDENTITY_MISMATCH"
    LEGAL_SWITCH_ACTIONS_MISMATCH_CONFIRMATION = (
        "LEGAL_SWITCH_ACTIONS_MISMATCH_CONFIRMATION"
    )
    #: Bundle 2 (Gemini V2): no matching LegalSwitchConfirmation exists for
    #: this exact binding -- the historical ``legal_switches = []`` defect's
    #: fix. Fires for a genuinely never-addressed switch state just as much
    #: as for a stale confirmation from a superseded Turn/revision/roster.
    LEGAL_SWITCHES_UNRESOLVED = "LEGAL_SWITCHES_UNRESOLVED"
    LEGAL_SWITCHES_IDENTITY_MISMATCH = "LEGAL_SWITCHES_IDENTITY_MISMATCH"
    LEGAL_SWITCHES_STALE_BINDING = "LEGAL_SWITCHES_STALE_BINDING"
    #: R3-C defense-in-depth: the gate never trusts a bound-and-current
    #: LegalSwitchConfirmation's *contents* just because its binding
    #: checked out -- it independently re-derives contextual legality from
    #: ``selected_three``/current active/``confirmed_fainted_members`` on
    #: every evaluation, exactly like it already does for the confirmed
    #: state itself. This defends against a forged/corrupted row that
    #: happens to carry a correct binding.
    LEGAL_SWITCHES_APPLIED_SELECTION_MISSING = "LEGAL_SWITCHES_APPLIED_SELECTION_MISSING"
    LEGAL_SWITCHES_ACTIVE_OUTSIDE_SELECTED_THREE = "LEGAL_SWITCHES_ACTIVE_OUTSIDE_SELECTED_THREE"
    LEGAL_SWITCHES_MEMBER_OUTSIDE_SELECTED_THREE = "LEGAL_SWITCHES_MEMBER_OUTSIDE_SELECTED_THREE"
    LEGAL_SWITCHES_MEMBER_IS_ACTIVE = "LEGAL_SWITCHES_MEMBER_IS_ACTIVE"
    LEGAL_SWITCHES_MEMBER_CONFIRMED_FAINTED = "LEGAL_SWITCHES_MEMBER_CONFIRMED_FAINTED"


@dataclass(frozen=True, slots=True)
class ProviderReadyGateResult:
    """Pure gate outcome. ``allowed`` is ``True`` only if no reason fired."""

    allowed: bool
    denial_reasons: tuple[GateDenialReason, ...] = ()


def evaluate_provider_ready_gate(
    *,
    confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    current_identity: TurnIdentity,
    latest_confirmed_state_id: str,
    latest_open_draft_turn_number: int | None,
    latest_open_draft_battle_revision: int | None,
    legal_switch_confirmation: LegalSwitchConfirmation | None,
    selected_three: tuple[str, str, str] | None,
    confirmed_fainted_members: frozenset[str] = frozenset(),
) -> ProviderReadyGateResult:
    """Evaluate the minimum provider-ready gate. Deny-by-default, pure, no I/O.

    Only allows a rich-state request when every one of the following holds:

    - ``confirmed_state`` is human-confirmed;
    - ``confirmed_state.identity`` matches ``current_identity`` exactly
      (session/match/generation/turn/revision);
    - ``confirmed_state.confirmed_state_id`` equals
      ``latest_confirmed_state_id`` (not a stale/superseded snapshot);
    - no newer OPEN draft exists (``latest_open_draft_turn_number`` /
      ``latest_open_draft_battle_revision``, when supplied, must not be
      strictly newer than ``confirmed_state``'s own identity);
    - self active and opponent active are each CONFIRMED and not the
      literal text ``"UNKNOWN"`` (HP, status, stat stages, weather,
      terrain, and side effects may legitimately be an explicit confirmed
      UNKNOWN -- active alone may never be sent unknown);
    - ``confirmed_legal_actions`` is non-empty and every entry is a
      ``ConfirmedLegalActionSelection`` bound to ``current_identity``;
    - ``legal_switch_confirmation`` (Bundle 2) is not ``None`` and is bound
      to ``current_identity``. ``None`` means no matching
      :class:`~maple_next.domain.legal_switches.LegalSwitchConfirmation`
      was found for the caller's exact binding -- durably
      NOT_CAPTURED_OR_UNRESOLVED -- and blocks the gate exactly like an
      empty ``confirmed_legal_actions`` does. An empty
      ``legal_switch_confirmation.legal_switches`` alone never satisfies
      this gate; only an explicit ``CONFIRMED_NONE`` status does (the
      dataclass itself already refuses to construct any other empty-tuple/
      status combination, so this function only needs to check identity
      binding here);
    - (R3-C defense-in-depth) when a ``legal_switch_confirmation`` is
      otherwise valid, its *contents* are independently re-derived against
      ``selected_three``/the confirmed self active/``confirmed_fainted_
      members`` -- never trusted merely because the binding matched. A
      confirmation whose stored ``legal_switches`` includes the current
      active, a name outside ``selected_three``, or a confirmed-fainted
      member fails closed even if it was somehow persisted (corruption, a
      bypassed application layer, a stale faint state since revalidated).
      ``selected_three`` itself must be supplied and must contain the
      confirmed self active, or the gate fails closed regardless of
      whether a ``legal_switch_confirmation`` exists at all.

    The caller is responsible for supplying ``current_identity``,
    ``latest_confirmed_state_id``, and the latest-open-draft identity
    fields from durable state -- this function has no persistence access
    and cannot compute any of them itself.
    """

    reasons: list[GateDenialReason] = []

    if not isinstance(confirmed_state, ConfirmedTurnState) or not (
        confirmed_state.confirmation.confirmed_by_human
    ):
        reasons.append(GateDenialReason.STATE_NOT_HUMAN_CONFIRMED)
        return ProviderReadyGateResult(allowed=False, denial_reasons=tuple(reasons))

    if confirmed_state.identity != current_identity:
        reasons.append(GateDenialReason.IDENTITY_MISMATCH)

    if confirmed_state.confirmed_state_id != latest_confirmed_state_id:
        reasons.append(GateDenialReason.STALE_CONFIRMED_STATE)

    if (
        latest_open_draft_turn_number is not None
        and latest_open_draft_battle_revision is not None
        and (
            latest_open_draft_turn_number > confirmed_state.identity.turn_number
            or (
                latest_open_draft_turn_number == confirmed_state.identity.turn_number
                and latest_open_draft_battle_revision > confirmed_state.identity.battle_revision
            )
        )
    ):
        reasons.append(GateDenialReason.NEWER_OPEN_DRAFT_EXISTS)

    self_active = confirmed_state.self_side.active
    if not self_active.is_confirmed:
        reasons.append(GateDenialReason.SELF_ACTIVE_UNKNOWN)
    elif self_active.value == "UNKNOWN":
        reasons.append(GateDenialReason.SELF_ACTIVE_LITERAL_UNKNOWN_VALUE)

    opponent_active = confirmed_state.opponent_side.active
    if not opponent_active.is_confirmed:
        reasons.append(GateDenialReason.OPPONENT_ACTIVE_UNKNOWN)
    elif opponent_active.value == "UNKNOWN":
        reasons.append(GateDenialReason.OPPONENT_ACTIVE_LITERAL_UNKNOWN_VALUE)

    if not confirmed_legal_actions:
        reasons.append(GateDenialReason.LEGAL_ACTIONS_EMPTY)
    else:
        for selection in confirmed_legal_actions:
            if not isinstance(selection, ConfirmedLegalActionSelection):
                reasons.append(GateDenialReason.LEGAL_ACTIONS_NOT_CONFIRMED)
                break
        for selection in confirmed_legal_actions:
            if (
                isinstance(selection, ConfirmedLegalActionSelection)
                and selection.identity != current_identity
            ):
                reasons.append(GateDenialReason.LEGAL_ACTIONS_IDENTITY_MISMATCH)
                break

    if legal_switch_confirmation is None:
        reasons.append(GateDenialReason.LEGAL_SWITCHES_UNRESOLVED)
    else:
        if legal_switch_confirmation.identity != current_identity:
            reasons.append(GateDenialReason.LEGAL_SWITCHES_IDENTITY_MISMATCH)
        if legal_switch_confirmation.based_on_confirmed_state_id != latest_confirmed_state_id:
            reasons.append(GateDenialReason.LEGAL_SWITCHES_STALE_BINDING)

        # The provider-bound legal action list and the separately persisted
        # exact-binding switch confirmation are two views of one human fact.
        # They must agree before a request can be built; otherwise a
        # moves-only request could hide confirmed switch options from the
        # provider and make a legal SWITCH recommendation impossible.
        confirmed_switch_action_names = frozenset(
            selection.action_name
            for selection in confirmed_legal_actions
            if (
                isinstance(selection, ConfirmedLegalActionSelection)
                and selection.action_type is ActionType.SWITCH
            )
        )
        confirmed_switch_names = frozenset(legal_switch_confirmation.legal_switches)
        switch_sets_match = (
            confirmed_switch_action_names == confirmed_switch_names
            if legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
            else not confirmed_switch_action_names
        )
        if not switch_sets_match:
            reasons.append(GateDenialReason.LEGAL_SWITCH_ACTIONS_MISMATCH_CONFIRMATION)

    # R3-C defense-in-depth: independently re-derive contextual legality --
    # never trust a bound-and-current confirmation's stored contents alone.
    if selected_three is None:
        reasons.append(GateDenialReason.LEGAL_SWITCHES_APPLIED_SELECTION_MISSING)
    else:
        active_name = self_active.value if self_active.is_confirmed else None
        if active_name is None or active_name not in selected_three:
            reasons.append(GateDenialReason.LEGAL_SWITCHES_ACTIVE_OUTSIDE_SELECTED_THREE)
        if legal_switch_confirmation is not None:
            members = legal_switch_confirmation.legal_switches
            if any(name not in selected_three for name in members):
                reasons.append(GateDenialReason.LEGAL_SWITCHES_MEMBER_OUTSIDE_SELECTED_THREE)
            if active_name is not None and active_name in members:
                reasons.append(GateDenialReason.LEGAL_SWITCHES_MEMBER_IS_ACTIVE)
            if any(name in confirmed_fainted_members for name in members):
                reasons.append(GateDenialReason.LEGAL_SWITCHES_MEMBER_CONFIRMED_FAINTED)

    return ProviderReadyGateResult(allowed=not reasons, denial_reasons=tuple(reasons))


def _identity_to_json(identity: TurnIdentity) -> dict[str, object]:
    return {
        "session_id": identity.session_id,
        "match_id": identity.match_id,
        "generation": identity.generation,
        "turn_id": identity.turn_id,
        "turn_number": identity.turn_number,
        "battle_revision": identity.battle_revision,
    }


def _legal_action_to_json(selection: ConfirmedLegalActionSelection) -> dict[str, object]:
    return {
        "confirmation_id": selection.confirmation_id,
        "action_type": selection.action_type.value,
        "action_name": selection.action_name,
        "confirmed_at_utc": selection.confirmation.confirmed_at_utc,
        "provenance": selection.confirmation.provenance,
        "source_prefill_id": selection.source_prefill_id,
    }


def projection_to_canonical_dict(projection: RichStateProjection) -> dict[str, Any]:
    """Deterministic, JSON-compatible representation used for hashing.

    Identical ``RichStateProjection`` content always yields an identical
    dict (and therefore an identical hash); any meaningful difference in
    field value, knowledge status, provenance, identity, revision, or
    confirmed legal actions changes this dict.
    """

    sorted_actions = sorted(
        projection.confirmed_legal_actions,
        key=lambda selection: (
            selection.action_type.value,
            selection.confirmation_id,
            selection.action_name,
        ),
    )
    evidence = projection.evidence
    return {
        "contract_version": projection.contract_version,
        "identity": _identity_to_json(projection.identity),
        "reviewed_confirmed_state_id": projection.reviewed_confirmed_state_id,
        "previous_confirmed_state_id": projection.previous_confirmed_state_id,
        "self_side": side_state_to_json(projection.self_side),
        "opponent_side": side_state_to_json(projection.opponent_side),
        "weather": known_to_json(projection.weather),
        "terrain": known_to_json(projection.terrain),
        "state_confirmation": {
            "confirmed_by_human": projection.state_confirmation.confirmed_by_human,
            "confirmed_at_utc": projection.state_confirmation.confirmed_at_utc,
            "provenance": projection.state_confirmation.provenance,
        },
        "evidence": (
            {
                "evidence_id": evidence.evidence_id,
                "relative_path": evidence.relative_path,
                "sha256": evidence.sha256,
                "recorded_at_utc": evidence.recorded_at_utc,
            }
            if evidence is not None
            else None
        ),
        "confirmed_legal_actions": [_legal_action_to_json(s) for s in sorted_actions],
    }


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compute_projection_hash(projection: RichStateProjection) -> str:
    """SHA-256 over the canonical dict. Deterministic for identical input."""

    canonical = projection_to_canonical_dict(projection)
    return hashlib.sha256(_canonical_json_bytes(canonical)).hexdigest()
