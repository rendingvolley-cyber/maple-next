"""Bundle B: rich-state Turn Advice request assembly (additive, versioned).

This module builds a strict, hashable, provider-ready request object out of
:mod:`maple_next.domain.turn_state_projection`. It performs no I/O, sends
nothing over the network, and does not touch:

- ``providers/turn_request.py`` (Initial Prompt v1 text, legacy
  ``maple-turn-advice.v1``/``.v2`` contract, response schema) -- not
  imported, not modified;
- ``providers/turn_response.py`` (response schema validation) -- not
  imported, not modified;
- ``providers/turn_transport.py`` (model routing / real HTTP transport) --
  not imported, not modified;
- the operator UI.

It reuses (imports, never modifies) the existing
:func:`maple_next.providers.turn_boundary.decide_turn_advice_dispatch` and
:class:`maple_next.providers.turn_boundary.DispatchDecision` so this
additive contract shares -- rather than reimplements -- the existing
trusted-human-activation / one-attempt / no-retry dispatch policy. A caller
must compute that ``DispatchDecision`` itself (exactly as the legacy lane
already requires) and pass it in; this module never decides dispatch on its
own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from maple_next.domain.turn_state import (
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    TurnIdentity,
)
from maple_next.domain.turn_state_projection import (
    RICH_STATE_PROJECTION_CONTRACT_VERSION,
    ProviderReadyGateError,
    RichStateProjection,
    build_rich_state_projection,
    compute_projection_hash,
    evaluate_provider_ready_gate,
    projection_to_canonical_dict,
)
from maple_next.providers.turn_boundary import DispatchDecision

#: Additive versioned request contract. Distinct from, and never a
#: replacement for, ``maple-turn-advice.v1``/``.v2`` in ``turn_request.py``.
RICH_STATE_REQUEST_CONTRACT_VERSION = RICH_STATE_PROJECTION_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class RichStateTurnAdviceRequest:
    """A provider-ready rich-state Turn Advice request. Never sent by this module."""

    contract_version: str
    projection: RichStateProjection
    reviewed_snapshot_hash: str
    request_hash: str


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def build_rich_state_turn_advice_request(
    *,
    confirmed_state: ConfirmedTurnState,
    confirmed_legal_actions: tuple[ConfirmedLegalActionSelection, ...],
    current_identity: TurnIdentity,
    latest_confirmed_state_id: str,
    latest_open_draft_turn_number: int | None,
    latest_open_draft_battle_revision: int | None,
    dispatch_decision: DispatchDecision,
) -> RichStateTurnAdviceRequest:
    """Build a provider-ready rich-state request, or fail closed.

    Requires an already-computed, already-allowed ``dispatch_decision`` from
    :func:`maple_next.providers.turn_boundary.decide_turn_advice_dispatch`
    (trusted-human-activation, current binding, no pending job, attempt not
    yet consumed) *and* an allowed
    :func:`maple_next.domain.turn_state_projection.evaluate_provider_ready_gate`
    outcome. Raises :class:`ProviderReadyGateError` if either check fails --
    this function never builds a request for a denied dispatch or a denied
    gate, and never retries.
    """

    if not dispatch_decision.allowed:
        raise ProviderReadyGateError(f"DISPATCH_NOT_ALLOWED:{dispatch_decision.reason_code}")

    gate = evaluate_provider_ready_gate(
        confirmed_state=confirmed_state,
        confirmed_legal_actions=confirmed_legal_actions,
        current_identity=current_identity,
        latest_confirmed_state_id=latest_confirmed_state_id,
        latest_open_draft_turn_number=latest_open_draft_turn_number,
        latest_open_draft_battle_revision=latest_open_draft_battle_revision,
    )
    if not gate.allowed:
        reason_text = ",".join(reason.value for reason in gate.denial_reasons)
        raise ProviderReadyGateError(f"PROVIDER_READY_GATE_DENIED:{reason_text}")

    projection = build_rich_state_projection(confirmed_state, confirmed_legal_actions)
    snapshot_hash = compute_projection_hash(projection)
    request_payload = {
        "contract_version": RICH_STATE_REQUEST_CONTRACT_VERSION,
        "reviewed_snapshot_hash": snapshot_hash,
        "projection": projection_to_canonical_dict(projection),
    }
    request_hash = hashlib.sha256(_canonical_json_bytes(request_payload)).hexdigest()

    return RichStateTurnAdviceRequest(
        contract_version=RICH_STATE_REQUEST_CONTRACT_VERSION,
        projection=projection,
        reviewed_snapshot_hash=snapshot_hash,
        request_hash=request_hash,
    )
