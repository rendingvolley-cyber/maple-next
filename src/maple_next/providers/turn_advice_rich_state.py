"""Bundle B: rich-state Turn Advice request assembly (additive, versioned).

This module builds a strict, hashable, provider-ready request object out of
:mod:`maple_next.domain.turn_state_projection`. It performs no I/O, sends
nothing over the network, and does not touch:

- ``providers/turn_response.py`` (response schema validation) -- not
  imported, not modified;
- ``providers/turn_transport.py`` (model routing / real HTTP transport) --
  not imported, not modified;
- the operator UI.

It reuses (imports, never modifies) ``_TURN_INITIAL_PROMPT`` indirectly via
the shared renderer functions in ``providers/turn_request.py``
(``_render_provider_prompt_from_canonical_request`` /
``_render_provider_request_body_from_prompt``), plus
``TURN_PROMPT_VERSION`` and ``REQUESTED_OUTPUT_SCHEMA`` from that same
module, so this additive contract never copies or re-implements the
Initial Prompt v1 text or the legacy response schema.

This module intentionally does not accept or manufacture a
``DispatchDecision``. Dispatch authorization (trusted-human-activation,
current binding, no pending job, attempt not yet consumed) is exclusively
the responsibility of the durable application-layer API in
``application/service.py`` (``BattleApplication.request_rich_turn_advice``).
A pure request builder that trusted an externally supplied
``DispatchDecision`` was a forge-resistance hole -- any caller could
construct ``DispatchDecision(allowed=True, ...)`` and obtain a
provider-ready request without ever touching durable state. This module's
public builder therefore only re-checks the *domain-source* gate
(:func:`maple_next.domain.turn_state_projection.evaluate_provider_ready_gate`)
as defense in depth; it never decides dispatch.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Final

from maple_next.domain.battle_memory import (
    BattleMemory,
    Bundle3TurnContext,
    SelectedBuildMember,
    battle_memory_to_canonical_list,
    selected_build_member_to_canonical_dict,
)
from maple_next.domain.champions_rules import RULES_CONTEXT_SCHEMA_VERSION
from maple_next.domain.enums import ActionType
from maple_next.domain.legal_switches import LegalSwitchConfirmation
from maple_next.domain.opponent_intel_context import (
    CONTEXT_STATUS_AVAILABLE,
    OpponentIntelContextError,
    validate_opponent_intel_context,
)
from maple_next.domain.turn_state import (
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FixedEvidenceMetadata,
    TurnIdentity,
)
from maple_next.domain.turn_state_projection import (
    ProviderReadyGateError,
    RichStateProjection,
    build_rich_state_projection,
    compute_projection_hash,
    evaluate_provider_ready_gate,
    projection_to_canonical_dict,
)
from maple_next.providers.turn_request import (
    REQUESTED_OUTPUT_SCHEMA,
    TURN_PROMPT_VERSION,
    _render_provider_prompt_from_canonical_request,
    _render_provider_request_body_from_prompt,
)

#: Additive, versioned request contract. Distinct from the legacy
#: ``maple-turn-advice.v1``/``.v2`` contracts in ``turn_request.py`` *and*
#: from the projection contract version
#: (``RICH_STATE_PROJECTION_CONTRACT_VERSION``, ``maple-turn-advice-rich-
#: state.v1``) that it embeds.
#:
#: Bundle 3 (Gemini V2) raised this from ``.v3`` to ``.v4``: every ``.v3``
#: field is preserved verbatim, and ``applied_selection_id``,
#: ``reviewed_selection_id``, ``selected_three_builds``, and
#: ``battle_memory`` were added. All four participate in canonical
#: serialization, request bytes, ``request_hash``, and deterministic
#: rebuild.
#:
#: Bundle 4 (Gemini V2) raises this from ``.v4`` to ``.v5``: every ``.v4``
#: field is preserved verbatim, and exactly one top-level field,
#: ``rules_context``, is added -- the compact, immutable official Champions
#: rules snapshot pinned to this match (see
#: ``domain/champions_rules.py``). It participates in canonical
#: serialization, request bytes, ``request_hash``, and deterministic
#: rebuild exactly like every other field: a rules version/content/hash
#: change changes the request hash.
#:
#: Bundle 5 (Gemini V2) raises this from ``.v5`` to ``.v6``: every ``.v5``
#: field is preserved verbatim with unchanged semantics, and exactly one
#: top-level field, ``opponent_intel_context``, is added -- the compact,
#: non-authoritative opponent *population* prior resolved from the
#: immutable INTEL generation pinned to this match (see
#: ``domain/opponent_intel_context.py``). It participates in canonical
#: serialization, request bytes, ``request_hash``, and deterministic
#: rebuild exactly like every other field: a different pinned INTEL
#: snapshot, or a different confirmed opponent active, changes the hash.
#: Bundle 5 is input context only -- it does not touch the response/output
#: contract, legal actions, confirmed state, or battle memory.
RICH_STATE_REQUEST_CONTRACT_VERSION = "maple-turn-advice.v6"

#: Top-level keys ``rules_context`` must carry. Defence in depth: the value
#: is always actually produced by
#: ``domain.champions_rules.build_rules_context``, but this request
#: contract fails closed on a malformed/incomplete dict rather than trusting
#: any caller-supplied shape.
_REQUIRED_RULES_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "context_schema_version",
        "ruleset_id",
        "ruleset_version",
        "snapshot_id",
        "facts_content_sha256",
        "scope",
        "facts",
        "sources",
    }
)

#: Fixed job type, matching the legacy Turn Advice job lane exactly. Rich
#: requests are dispatched through the same ``TURN_ADVICE`` job type as the
#: legacy lane; ``contract_version`` is what distinguishes a rich request's
#: job envelope from a legacy one.
RICH_STATE_JOB_TYPE = "TURN_ADVICE"

#: Rich-provider-only presentation constraint. This is deliberately appended
#: at the canonical rich prompt boundary rather than changing the shared
#: Initial Prompt, the response schema, or any machine-facing request value.
_RICH_JAPANESE_HUMAN_TEXT_INSTRUCTION: Final[str] = """Human-facing language requirement:
Write opponent_prediction.summary, every reasons item, and every warnings item in concise,
decision-oriented, non-repetitive natural Japanese that a human can scan during the current turn.
Even when the canonical request contains English text, these human-facing fields must be Japanese.
Use at most two reasons and include only the most important decision factors.
Add a warning only for a concrete, actionable current-turn risk; otherwise return an empty warnings
array. Do not repeat a reason as a warning or inflate warnings with generic uncertainty.
Do not translate or alter machine/contract values, including recommended_action action_id,
action_type, or action_name; opponent_prediction category, predicted_action, or confidence;
source/model/binding/legality values; IDs; hashes; or enum-like tokens."""


class RichStateRequestError(Exception):
    """Fail-closed base error for rich-state request assembly."""


@dataclass(frozen=True, slots=True)
class RichLegalAction:
    """One canonical legal action derived from a confirmed human selection.

    ``action_id`` is always the source ``ConfirmedLegalActionSelection.
    confirmation_id`` -- never a freshly minted or re-derived identifier.
    ``owner_active``/``switch_target`` are derived deterministically from
    ``action_type`` (MOVE carries ``owner_active``, SWITCH carries
    ``switch_target``), mirroring the legacy ``LegalAction`` convention.
    """

    action_id: str
    action_type: ActionType
    action_name: str
    confirmed_at_utc: str
    provenance: str
    source_prefill_id: str | None
    owner_active: str | None = None
    switch_target: str | None = None

    def __post_init__(self) -> None:
        if self.action_type is ActionType.MOVE:
            if not self.owner_active:
                raise RichStateRequestError("RICH_MOVE_ACTION_REQUIRES_OWNER_ACTIVE")
            if self.switch_target is not None:
                raise RichStateRequestError("RICH_MOVE_ACTION_MUST_NOT_CARRY_SWITCH_TARGET")
        elif self.action_type is ActionType.SWITCH:
            if not self.switch_target:
                raise RichStateRequestError("RICH_SWITCH_ACTION_REQUIRES_SWITCH_TARGET")
            if self.owner_active is not None:
                raise RichStateRequestError("RICH_SWITCH_ACTION_MUST_NOT_CARRY_OWNER_ACTIVE")
        else:  # pragma: no cover - ActionType is exhaustive today
            raise RichStateRequestError("UNSUPPORTED_ACTION_TYPE")


def _map_confirmed_selection_to_rich_legal_action(
    selection: ConfirmedLegalActionSelection, *, self_active_name: str
) -> RichLegalAction:
    """Map one accepted ``ConfirmedLegalActionSelection`` to a ``RichLegalAction``.

    Never derives an action from notes, OCR, deltas, prior advice, or a
    prefill draft -- the only accepted source is a confirmed selection that
    has already passed the Bundle A legal-action boundary.
    """

    if selection.action_type is ActionType.MOVE:
        owner_active: str | None = self_active_name
        switch_target: str | None = None
    elif selection.action_type is ActionType.SWITCH:
        owner_active = None
        switch_target = selection.action_name
    else:  # pragma: no cover - ActionType is exhaustive today
        raise RichStateRequestError("UNSUPPORTED_ACTION_TYPE")
    return RichLegalAction(
        action_id=selection.confirmation_id,
        action_type=selection.action_type,
        action_name=selection.action_name,
        confirmed_at_utc=selection.confirmation.confirmed_at_utc,
        provenance=selection.confirmation.provenance,
        source_prefill_id=selection.source_prefill_id,
        owner_active=owner_active,
        switch_target=switch_target,
    )


@dataclass(frozen=True, slots=True)
class RichStateTurnAdviceRequest:
    """A complete, provider-ready rich-state Turn Advice request. Never sent by this module."""

    contract_version: str
    prompt_version: str
    job_type: str
    identity: TurnIdentity
    reviewed_confirmed_state_id: str
    previous_confirmed_state_id: str | None
    projection: RichStateProjection
    reviewed_snapshot_hash: str
    selected_three: tuple[str, str, str]
    self_active: str
    legal_actions: tuple[RichLegalAction, ...]
    #: Bundle 2 (Gemini V2). Always the exact confirmed
    #: ``legal_switch_confirmation.legal_switches`` -- never re-derived from
    #: ``legal_actions`` here, so the request can never silently diverge from
    #: what was actually confirmed.
    legal_switches: tuple[str, ...]
    #: ``CONFIRMED_NONEMPTY`` or ``CONFIRMED_NONE`` only. A provider-ready
    #: request can never carry ``NOT_CAPTURED_OR_UNRESOLVED`` -- the gate
    #: above blocks construction before this field could ever be set to it.
    legal_switches_status: str
    requested_output_schema: dict[str, Any]
    state_confirmation: ConfirmationMeta
    evidence: FixedEvidenceMetadata | None
    self_team_build_sha256: str | None
    #: Bundle 3 (Gemini V2). The exact applied bring and the exact reviewed
    #: SelectionFacts the build context below was proven against, so a
    #: request can never be silently re-bound to a different selection.
    applied_selection_id: str
    reviewed_selection_id: str
    #: Exactly three entries, in applied ``selected_three`` order. Never the
    #: party six, never a recommendation, never Opponent INTEL.
    selected_three_builds: tuple[SelectedBuildMember, ...]
    #: Completed prior Turns 1..N-1 only -- human-confirmed/reviewed facts.
    battle_memory: BattleMemory
    #: Bundle 4 (Gemini V2). The compact, immutable official Champions rules
    #: snapshot pinned to this match (``domain.champions_rules.
    #: build_rules_context``). Authoritative for Pokemon Champions-specific
    #: battle rules within its declared coverage; never raw HTML, never a
    #: live network fetch. Validated in ``__post_init__`` below.
    rules_context: dict[str, Any]
    #: Bundle 5 (Gemini V2). The compact opponent *population* prior
    #: resolved from the immutable INTEL generation pinned to this match
    #: (``domain.opponent_intel_context.build_opponent_intel_context``).
    #: Explicitly non-authoritative (``authority = POPULATION_PRIOR``): it
    #: never describes the confirmed opponent build, never overrides a
    #: confirmed match fact or battle memory, and is ``UNAVAILABLE`` /
    #: ``MISMATCHED`` with ``population = None`` whenever it cannot be
    #: proven. Validated in ``__post_init__`` below.
    opponent_intel_context: dict[str, Any]
    request_hash: str

    def __post_init__(self) -> None:
        missing = _REQUIRED_RULES_CONTEXT_KEYS - self.rules_context.keys()
        if missing:
            raise RichStateRequestError(f"RULES_CONTEXT_MISSING_KEYS:{sorted(missing)}")
        if self.rules_context["context_schema_version"] != RULES_CONTEXT_SCHEMA_VERSION:
            raise RichStateRequestError("RULES_CONTEXT_UNSUPPORTED_SCHEMA_VERSION")
        # Bundle 5 defence in depth: a request must never be assembled from
        # a population context that claims more than it can prove (an
        # AVAILABLE status with malformed/over-length/out-of-range data, a
        # population payload attached to an UNAVAILABLE/MISMATCHED status,
        # or an authority claim other than POPULATION_PRIOR).
        try:
            validate_opponent_intel_context(self.opponent_intel_context)
        except OpponentIntelContextError as exc:
            raise RichStateRequestError(f"OPPONENT_INTEL_CONTEXT_INVALID:{exc}") from exc
        if self.opponent_intel_context["status"] == CONTEXT_STATUS_AVAILABLE:
            # A MATCHED compatibility claim must be consistent with the
            # rules actually pinned to this match, not merely asserted.
            snapshot_format = str(self.opponent_intel_context["snapshot"]["format"]).upper()
            pinned_format = str(self.rules_context["facts"]["battle_format"]).upper()
            if snapshot_format != pinned_format:
                raise RichStateRequestError("OPPONENT_INTEL_COMPATIBILITY_CLAIM_INVALID")


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_legal_action_dict(action: RichLegalAction) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type.value,
        "action_name": action.action_name,
        "confirmed_at_utc": action.confirmed_at_utc,
        "provenance": action.provenance,
        "source_prefill_id": action.source_prefill_id,
        "owner_active": action.owner_active,
        "switch_target": action.switch_target,
    }


def canonical_rich_request_dict(request: RichStateTurnAdviceRequest) -> dict[str, Any]:
    """Deterministic, JSON-compatible dict of the complete rich request.

    Sort keys, no whitespace, fixed separators, deterministic legal-action
    ordering (``(action_type, action_id, action_name)``) -- identical input
    always yields identical bytes. Never includes a model name, endpoint,
    header, or credential.
    """

    sorted_actions = sorted(
        request.legal_actions,
        key=lambda action: (action.action_type.value, action.action_id, action.action_name),
    )
    payload: dict[str, Any] = {
        "contract_version": request.contract_version,
        "prompt_version": request.prompt_version,
        "job_type": request.job_type,
        "session_id": request.identity.session_id,
        "match_id": request.identity.match_id,
        "generation": request.identity.generation,
        "turn_id": request.identity.turn_id,
        "turn_number": request.identity.turn_number,
        "battle_revision": request.identity.battle_revision,
        "reviewed_confirmed_state_id": request.reviewed_confirmed_state_id,
        "previous_confirmed_state_id": request.previous_confirmed_state_id,
        "reviewed_snapshot_hash": request.reviewed_snapshot_hash,
        "reviewed_state": projection_to_canonical_dict(request.projection),
        "selected_three": list(request.selected_three),
        "self_active": request.self_active,
        "legal_actions": [_canonical_legal_action_dict(a) for a in sorted_actions],
        "legal_switches": sorted(request.legal_switches),
        "legal_switches_status": request.legal_switches_status,
        "requested_output_schema": request.requested_output_schema,
        "state_confirmation": {
            "confirmed_by_human": request.state_confirmation.confirmed_by_human,
            "confirmed_at_utc": request.state_confirmation.confirmed_at_utc,
            "provenance": request.state_confirmation.provenance,
        },
        "evidence": (
            {
                "evidence_id": request.evidence.evidence_id,
                "relative_path": request.evidence.relative_path,
                "sha256": request.evidence.sha256,
                "recorded_at_utc": request.evidence.recorded_at_utc,
            }
            if request.evidence is not None
            else None
        ),
        "self_team_build_sha256": request.self_team_build_sha256,
        # Bundle 3. ``selected_three_builds`` keeps applied ``selected_three``
        # order (deterministic, and the order the operator actually brought);
        # ``battle_memory`` keeps strict chronological Turn order. Neither is
        # re-sorted here, so both are stable across restarts and independent
        # of database insertion order.
        "applied_selection_id": request.applied_selection_id,
        "reviewed_selection_id": request.reviewed_selection_id,
        "selected_three_builds": [
            selected_build_member_to_canonical_dict(member)
            for member in request.selected_three_builds
        ],
        "battle_memory": battle_memory_to_canonical_list(request.battle_memory),
        # Bundle 4. The pinned official Champions rules snapshot, embedded
        # verbatim (already a plain, JSON-primitive-only dict from
        # ``domain.champions_rules.build_rules_context``) -- never raw
        # HTML/source documents, never re-derived or re-fetched here.
        "rules_context": request.rules_context,
        # Bundle 5. The pinned opponent population prior, embedded verbatim
        # (already a plain, JSON-primitive-only dict from
        # ``domain.opponent_intel_context.build_opponent_intel_context``,
        # with deterministic top-8-in-snapshot-order categories) -- never
        # the population database itself, never raw source documents,
        # never re-derived or re-fetched here.
        "opponent_intel_context": request.opponent_intel_context,
    }
    return payload


def encode_canonical_rich_request(request: RichStateTurnAdviceRequest) -> bytes:
    return _canonical_json_bytes(canonical_rich_request_dict(request))


def rich_request_payload_hash(request: RichStateTurnAdviceRequest) -> str:
    return hashlib.sha256(encode_canonical_rich_request(request)).hexdigest()


def build_rich_provider_prompt(request: RichStateTurnAdviceRequest) -> str:
    """Build the rich request's prompt via the shared Initial Prompt v1 renderer.

    Delegates to the same private renderer the legacy
    ``turn_request.build_provider_prompt`` uses -- ``_TURN_INITIAL_PROMPT``
    is never copied or independently reimplemented here.
    """

    prompt = _render_provider_prompt_from_canonical_request(
        canonical_rich_request_dict(request)
    )
    return f"{prompt}\n\n{_RICH_JAPANESE_HUMAN_TEXT_INSTRUCTION}"


def build_rich_provider_request_body(request: RichStateTurnAdviceRequest) -> dict[str, Any]:
    """Build the rich request's provider body via the shared body renderer."""

    return _render_provider_request_body_from_prompt(
        build_rich_provider_prompt(request), request.requested_output_schema
    )


def build_rich_state_turn_advice_request(
    *,
    confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    current_identity: TurnIdentity,
    latest_confirmed_state_id: str,
    latest_open_draft_turn_number: int | None,
    latest_open_draft_battle_revision: int | None,
    legal_switch_confirmation: LegalSwitchConfirmation | None,
    selected_three: tuple[str, str, str],
    self_active: str,
    bundle3_context: Bundle3TurnContext,
    rules_context: dict[str, Any],
    opponent_intel_context: dict[str, Any],
    evidence: FixedEvidenceMetadata | None = None,
    self_team_build_sha256: str | None = None,
    confirmed_fainted_members: frozenset[str] = frozenset(),
) -> RichStateTurnAdviceRequest:
    """Build a complete, provider-ready rich-state request, or fail closed.

    This is a **pure builder, not an authorization API**. It re-validates
    the domain-source provider-ready gate
    (:func:`evaluate_provider_ready_gate`) as defense in depth, but it does
    not accept, compute, or manufacture a ``DispatchDecision`` -- dispatch
    authorization (trusted-human-activation / current-binding / no-pending-
    job / one-attempt) is exclusively
    ``application.service.BattleApplication.request_rich_turn_advice``'s
    responsibility. Calling this function alone never creates a job and
    never reserves an attempt.

    ``bundle3_context`` is required, never defaulted: it is produced by the
    one shared application helper
    (``application.turn_provider_export_bridge.load_bundle3_turn_context``),
    which has already proven the applied -> advice -> job -> reviewed
    ``SelectionFacts`` binding and the completed-prior-Turn memory against
    durable state. The Bundle 3 checks below run *after* the existing
    Bundle 1/2 provider-ready gate, as part of rich request construction --
    the older gate is left exactly as it is.

    ``rules_context`` is likewise required, never defaulted: it is produced
    by the one shared application helper
    (``application.turn_provider_export_bridge.load_champions_rules_context``),
    which resolves the match's persisted rules pin against the immutable
    checked-in snapshot and fails closed on any mismatch/corruption before
    this function is ever called. This builder only re-validates the
    dict's shape (:class:`RichStateTurnAdviceRequest`'s ``__post_init__``)
    as defense in depth -- it never re-derives or re-fetches rules content.

    ``opponent_intel_context`` is required in exactly the same way, and for
    exactly the same reason: it is produced by the one shared application
    helper
    (``application.turn_provider_export_bridge.load_opponent_intel_context``),
    which resolves the match's *persisted* INTEL generation pin against
    immutable archived generation storage. This builder additionally proves
    that the context is bound to the reviewed confirmed opponent active
    (below) and re-validates its shape, but never resolves, re-reads, or
    re-derives population data itself.
    """

    gate = evaluate_provider_ready_gate(
        confirmed_state=confirmed_state,
        confirmed_legal_actions=confirmed_legal_actions,
        current_identity=current_identity,
        latest_confirmed_state_id=latest_confirmed_state_id,
        latest_open_draft_turn_number=latest_open_draft_turn_number,
        latest_open_draft_battle_revision=latest_open_draft_battle_revision,
        legal_switch_confirmation=legal_switch_confirmation,
        selected_three=selected_three,
        confirmed_fainted_members=confirmed_fainted_members,
    )
    if not gate.allowed:
        reason_text = ",".join(reason.value for reason in gate.denial_reasons)
        raise ProviderReadyGateError(f"PROVIDER_READY_GATE_DENIED:{reason_text}")

    if self_active not in selected_three:
        raise RichStateRequestError("SELF_ACTIVE_NOT_IN_SELECTED_THREE")
    if len(set(selected_three)) != 3:
        raise RichStateRequestError("SELECTED_THREE_MUST_BE_DISTINCT")
    # The gate above already required a non-None, current-binding
    # confirmation to reach this point.
    assert legal_switch_confirmation is not None

    # --- Bundle 3 context validation, after the existing gate --------------
    # Provider-boundary defense in depth: the shared application helper has
    # already proven this context against durable state, but a request must
    # never be assembled from a context bound to a different bring, a
    # different match, or a not-yet-completed Turn.
    context_names = tuple(
        member.pokemon_name for member in bundle3_context.selected_three_builds
    )
    if context_names != selected_three:
        raise RichStateRequestError("SELECTED_THREE_BUILDS_NOT_BOUND_TO_APPLIED_SELECTION")
    for memory_turn in bundle3_context.battle_memory.turns:
        if (
            memory_turn.identity.session_id != current_identity.session_id
            or memory_turn.identity.match_id != current_identity.match_id
            or memory_turn.identity.generation != current_identity.generation
        ):
            raise RichStateRequestError("BATTLE_MEMORY_FOREIGN_IDENTITY")
        if memory_turn.identity.turn_number >= current_identity.turn_number:
            raise RichStateRequestError("BATTLE_MEMORY_CURRENT_OR_FUTURE_TURN")

    # --- Bundle 5 context binding, after the Bundle 3 checks --------------
    # The population prior may only ever be bound to the *reviewed
    # confirmed* opponent active. Anything else -- an OCR-only identity, an
    # unconfirmed draft, a team-preview member, an inferred backline, or
    # the prior active after a confirmed switch -- must fail closed here,
    # before any provider dispatch, rather than quietly advising against
    # the wrong Pokemon.
    opponent_active = confirmed_state.opponent_side.active
    expected_active = opponent_active.value if opponent_active.is_confirmed else None
    if opponent_intel_context["confirmed_active_species"] != expected_active:
        raise RichStateRequestError("OPPONENT_INTEL_CONTEXT_SPECIES_MISMATCH")

    projection = build_rich_state_projection(
        confirmed_state, confirmed_legal_actions, evidence=evidence
    )
    projection_hash = compute_projection_hash(projection)

    legal_actions = tuple(
        sorted(
            (
                _map_confirmed_selection_to_rich_legal_action(
                    selection, self_active_name=self_active
                )
                for selection in confirmed_legal_actions
            ),
            key=lambda action: (action.action_type.value, action.action_id, action.action_name),
        )
    )

    request = RichStateTurnAdviceRequest(
        contract_version=RICH_STATE_REQUEST_CONTRACT_VERSION,
        prompt_version=TURN_PROMPT_VERSION,
        job_type=RICH_STATE_JOB_TYPE,
        identity=current_identity,
        reviewed_confirmed_state_id=projection.reviewed_confirmed_state_id,
        previous_confirmed_state_id=projection.previous_confirmed_state_id,
        projection=projection,
        reviewed_snapshot_hash=projection_hash,
        selected_three=selected_three,
        self_active=self_active,
        legal_actions=legal_actions,
        legal_switches=legal_switch_confirmation.legal_switches,
        legal_switches_status=legal_switch_confirmation.status.value,
        requested_output_schema=REQUESTED_OUTPUT_SCHEMA,
        state_confirmation=confirmed_state.confirmation,
        evidence=evidence,
        self_team_build_sha256=self_team_build_sha256,
        applied_selection_id=bundle3_context.applied_selection_id,
        reviewed_selection_id=bundle3_context.reviewed_selection_id,
        selected_three_builds=bundle3_context.selected_three_builds,
        battle_memory=bundle3_context.battle_memory,
        rules_context=rules_context,
        opponent_intel_context=opponent_intel_context,
        request_hash="",
    )
    return replace(request, request_hash=rich_request_payload_hash(request))
