"""Issue #31 Bundle B: Provider / Export Bridge Implementation.

Focused tests for the additive rich-state projection, provider-ready gate,
deterministic hashing, ``maple-match.v3`` export, and non-leak/regression
guarantees. Bundle A regression lives in
``test_issue31_turn_state_contract_bundle_a.py`` and legacy Turn/export
regression lives in ``test_turn_lifecycle.py`` / ``test_export_directory_boundary.py``;
all three run alongside this file, unmodified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maple_next.application.match_export_v3 import (
    MATCH_EXPORT_SCHEMA_VERSION_V3,
    ConfirmedTurnRecord,
    MatchExportV3Error,
    build_match_export_v3_payload,
    compute_payload_sha256,
    export_match_v3,
    parse_match_export_v3,
)
from maple_next.application.turn_provider_export_bridge import (
    build_provider_ready_rich_state_request,
)
from maple_next.domain.enums import ActionType, HpBucket, MatchOutcome
from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    FixedEvidenceMetadata,
    Known,
    LegalActionPrefillDraft,
    NextTurnStateDraft,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
)
from maple_next.domain.turn_state_projection import (
    RICH_STATE_PROJECTION_CONTRACT_VERSION,
    GateDenialReason,
    ProjectionSourceError,
    ProviderReadyGateError,
    build_rich_state_projection,
    compute_projection_hash,
    evaluate_provider_ready_gate,
    projection_to_canonical_dict,
)
from maple_next.providers import turn_request, turn_response, turn_transport
from maple_next.providers.turn_boundary import (
    DispatchDecision,
    DispatchTrigger,
    decide_turn_advice_dispatch,
)

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
CONFIRMED_AT = "2026-08-06T00:00:00+00:00"


def _identity(
    *,
    turn_number: int = 2,
    battle_revision: int = 1,
    session_id: str = "session-b1",
    match_id: str = "match-b1",
    generation: int = 9,
    turn_id: str = "turn-2",
) -> TurnIdentity:
    return TurnIdentity(
        session_id=session_id,
        match_id=match_id,
        generation=generation,
        turn_id=turn_id,
        turn_number=turn_number,
        battle_revision=battle_revision,
    )


def _confirmed_side(*, active: str = "Dondozo", stage: int = 0) -> SideState:
    return SideState(
        active=Known.confirmed(active, provenance_chain=_HUMAN),
        hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN),
        status=Known.confirmed("NONE", provenance_chain=_HUMAN),
        attack_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        defense_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        special_attack_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        special_defense_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        speed_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        accuracy_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        evasion_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        side_effects=Known.confirmed((), provenance_chain=_HUMAN),
    )


def _unknown_side() -> SideState:
    return SideState(
        active=Known.unknown(),
        hp_bucket=Known.unknown(),
        status=Known.unknown(),
        attack_stage=Known.unknown(),
        defense_stage=Known.unknown(),
        special_attack_stage=Known.unknown(),
        special_defense_stage=Known.unknown(),
        speed_stage=Known.unknown(),
        accuracy_stage=Known.unknown(),
        evasion_stage=Known.unknown(),
        side_effects=Known.unknown(),
    )


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=CONFIRMED_AT, provenance="HUMAN_CONFIRMED"
    )


def _confirmed_state(
    *,
    identity: TurnIdentity | None = None,
    confirmed_state_id: str = "state-1",
    self_side: SideState | None = None,
    opponent_side: SideState | None = None,
    weather: Known[str] | None = None,
    terrain: Known[str] | None = None,
    evidence_id: str | None = None,
) -> ConfirmedTurnState:
    return ConfirmedTurnState(
        confirmed_state_id=confirmed_state_id,
        identity=identity or _identity(),
        previous_confirmed_state_id="state-0",
        self_side=self_side or _confirmed_side(active="Dondozo"),
        opponent_side=opponent_side or _confirmed_side(active="Gholdengo"),
        weather=(
            weather if weather is not None else Known.confirmed("NONE", provenance_chain=_HUMAN)
        ),
        terrain=(
            terrain if terrain is not None else Known.confirmed("NONE", provenance_chain=_HUMAN)
        ),
        confirmation=_confirmation(),
        evidence_id=evidence_id,
    )


def _legal_action(
    *,
    identity: TurnIdentity | None = None,
    confirmation_id: str = "legal-1",
    action_type: ActionType = ActionType.MOVE,
    action_name: str = "Wave Crash",
) -> ConfirmedLegalActionSelection:
    return ConfirmedLegalActionSelection(
        confirmation_id=confirmation_id,
        identity=identity or _identity(),
        action_type=action_type,
        action_name=action_name,
        confirmation=_confirmation(),
    )


def _allowed_dispatch_decision() -> DispatchDecision:
    return decide_turn_advice_dispatch(
        trigger=DispatchTrigger.TRUSTED_HUMAN_ACTIVATION,
        is_current_binding=True,
        has_pending_job=False,
        attempt_consumed=False,
    )


# --- 1. Projection source restriction ---------------------------------------


def test_only_confirmed_turn_state_can_become_projection_source() -> None:
    state = _confirmed_state()
    actions = (_legal_action(),)
    projection = build_rich_state_projection(state, actions)
    assert projection.contract_version == RICH_STATE_PROJECTION_CONTRACT_VERSION
    assert projection.reviewed_confirmed_state_id == state.confirmed_state_id


def test_next_turn_state_draft_rejected_as_projection_source() -> None:
    draft = NextTurnStateDraft(
        draft_id="draft-1",
        identity=_identity(),
        based_on_confirmed_state_id="state-0",
        source_delta_id="delta-1",
        self_side=_confirmed_side(active="Dondozo"),
        opponent_side=_confirmed_side(active="Gholdengo"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(ProjectionSourceError):
        build_rich_state_projection(draft, (_legal_action(),))  # type: ignore[arg-type]


def test_ocr_candidate_dict_rejected_as_projection_source() -> None:
    ocr_candidate = {"self_active": "Dondozo", "opponent_active": "Gholdengo"}
    with pytest.raises(ProjectionSourceError):
        build_rich_state_projection(ocr_candidate, (_legal_action(),))  # type: ignore[arg-type]


def test_raw_action_result_delta_rejected_as_projection_source() -> None:
    delta = ActionResultDelta(
        delta_id="delta-1",
        identity=_identity(),
        based_on_confirmed_state_id="state-0",
        self_side=SideDelta(
            active=FieldDelta.unchanged(),
            hp_bucket=FieldDelta.unchanged(),
            status=FieldDelta.unchanged(),
            attack_stage=FieldDelta.unchanged(),
            defense_stage=FieldDelta.unchanged(),
            special_attack_stage=FieldDelta.unchanged(),
            special_defense_stage=FieldDelta.unchanged(),
            speed_stage=FieldDelta.unchanged(),
            accuracy_stage=FieldDelta.unchanged(),
            evasion_stage=FieldDelta.unchanged(),
            side_effects=FieldDelta.unchanged(),
        ),
        opponent_side=SideDelta(
            active=FieldDelta.unchanged(),
            hp_bucket=FieldDelta.unchanged(),
            status=FieldDelta.unchanged(),
            attack_stage=FieldDelta.unchanged(),
            defense_stage=FieldDelta.unchanged(),
            special_attack_stage=FieldDelta.unchanged(),
            special_defense_stage=FieldDelta.unchanged(),
            speed_stage=FieldDelta.unchanged(),
            accuracy_stage=FieldDelta.unchanged(),
            evasion_stage=FieldDelta.unchanged(),
            side_effects=FieldDelta.unchanged(),
        ),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    with pytest.raises(ProjectionSourceError):
        build_rich_state_projection(delta, (_legal_action(),))  # type: ignore[arg-type]


def test_prefill_draft_never_promoted_to_confirmed_legal_action() -> None:
    prefill = LegalActionPrefillDraft(
        prefill_id="prefill-1",
        identity=_identity(),
        based_on_confirmed_state_id="state-0",
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        derived_at_utc=CONFIRMED_AT,
        confidence=0.99,
    )
    with pytest.raises(ProjectionSourceError):
        build_rich_state_projection(_confirmed_state(), (prefill,))  # type: ignore[arg-type]


def test_unconfirmed_legal_action_rejected() -> None:
    with pytest.raises(ProjectionSourceError):
        build_rich_state_projection(_confirmed_state(), ())


# --- 2. Provider-ready gate ---------------------------------------------------


def test_gate_denies_self_active_unknown() -> None:
    state = _confirmed_state(self_side=_unknown_side())
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert not result.allowed
    assert GateDenialReason.SELF_ACTIVE_UNKNOWN in result.denial_reasons


def test_gate_denies_opponent_active_unknown() -> None:
    state = _confirmed_state(opponent_side=_unknown_side())
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert not result.allowed
    assert GateDenialReason.OPPONENT_ACTIVE_UNKNOWN in result.denial_reasons


def test_gate_denies_newer_open_draft() -> None:
    state = _confirmed_state()
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=state.identity.turn_number + 1,
        latest_open_draft_battle_revision=state.identity.battle_revision + 1,
    )
    assert not result.allowed
    assert GateDenialReason.NEWER_OPEN_DRAFT_EXISTS in result.denial_reasons


def test_gate_allows_open_draft_that_is_not_newer() -> None:
    state = _confirmed_state()
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=state.identity.turn_number,
        latest_open_draft_battle_revision=state.identity.battle_revision,
    )
    assert result.allowed


def test_gate_denies_identity_revision_mismatch() -> None:
    state = _confirmed_state()
    other_identity = _identity(battle_revision=state.identity.battle_revision + 1)
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=other_identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert not result.allowed
    assert GateDenialReason.IDENTITY_MISMATCH in result.denial_reasons


def test_gate_denies_stale_snapshot() -> None:
    state = _confirmed_state()
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id="a-newer-state-id",
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert not result.allowed
    assert GateDenialReason.STALE_CONFIRMED_STATE in result.denial_reasons


def test_gate_denies_unconfirmed_final_legal_actions() -> None:
    state = _confirmed_state()
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert not result.allowed
    assert GateDenialReason.LEGAL_ACTIONS_EMPTY in result.denial_reasons


def test_gate_allows_explicit_confirmed_unknown_hp_status_stages_weather_terrain() -> None:
    side = SideState(
        active=Known.confirmed("Dondozo", provenance_chain=_HUMAN),
        hp_bucket=Known.confirmed(HpBucket.UNKNOWN, provenance_chain=_HUMAN),
        status=Known.confirmed("UNKNOWN", provenance_chain=_HUMAN),
        attack_stage=Known.unknown(),
        defense_stage=Known.unknown(),
        special_attack_stage=Known.unknown(),
        special_defense_stage=Known.unknown(),
        speed_stage=Known.unknown(),
        accuracy_stage=Known.unknown(),
        evasion_stage=Known.unknown(),
        side_effects=Known.unknown(),
    )
    state = _confirmed_state(self_side=side, weather=Known.unknown(), terrain=Known.unknown())
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
    )
    assert result.allowed


# --- 3. Provider bridge: dispatch decision + gate composition ----------------


def test_bridge_builds_request_when_dispatch_and_gate_both_allow() -> None:
    state = _confirmed_state()
    request = build_provider_ready_rich_state_request(
        current_identity=state.identity,
        latest_confirmed_state=state,
        confirmed_legal_actions=(_legal_action(),),
        latest_open_draft=None,
        dispatch_decision=_allowed_dispatch_decision(),
    )
    assert request.contract_version == RICH_STATE_PROJECTION_CONTRACT_VERSION
    assert len(request.request_hash) == 64
    assert len(request.reviewed_snapshot_hash) == 64


def test_bridge_fails_closed_when_dispatch_denied_retry_trigger() -> None:
    denied = decide_turn_advice_dispatch(
        trigger=DispatchTrigger.RETRY,
        is_current_binding=True,
        has_pending_job=False,
        attempt_consumed=False,
    )
    with pytest.raises(ProviderReadyGateError):
        build_provider_ready_rich_state_request(
            current_identity=_identity(),
            latest_confirmed_state=_confirmed_state(),
            confirmed_legal_actions=(_legal_action(),),
            latest_open_draft=None,
            dispatch_decision=denied,
        )


def test_bridge_fails_closed_when_attempt_already_consumed() -> None:
    denied = decide_turn_advice_dispatch(
        trigger=DispatchTrigger.TRUSTED_HUMAN_ACTIVATION,
        is_current_binding=True,
        has_pending_job=False,
        attempt_consumed=True,
    )
    with pytest.raises(ProviderReadyGateError):
        build_provider_ready_rich_state_request(
            current_identity=_identity(),
            latest_confirmed_state=_confirmed_state(),
            confirmed_legal_actions=(_legal_action(),),
            latest_open_draft=None,
            dispatch_decision=denied,
        )


def test_bridge_fails_closed_on_newer_open_draft() -> None:
    state = _confirmed_state()
    newer_draft = NextTurnStateDraft(
        draft_id="draft-newer",
        identity=_identity(
            turn_number=state.identity.turn_number + 1,
            battle_revision=state.identity.battle_revision + 1,
        ),
        based_on_confirmed_state_id=state.confirmed_state_id,
        source_delta_id="delta-1",
        self_side=_confirmed_side(active="Dondozo"),
        opponent_side=_confirmed_side(active="Gholdengo"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(ProviderReadyGateError):
        build_provider_ready_rich_state_request(
            current_identity=state.identity,
            latest_confirmed_state=state,
            confirmed_legal_actions=(_legal_action(),),
            latest_open_draft=newer_draft,
            dispatch_decision=_allowed_dispatch_decision(),
        )


# --- 4. UNKNOWN-safety: no silent defaulting ----------------------------------


def test_unknown_stat_stage_not_coerced_to_zero() -> None:
    side = _confirmed_side(active="Dondozo")
    side = SideState(
        active=side.active,
        hp_bucket=side.hp_bucket,
        status=side.status,
        attack_stage=Known.unknown(),
        defense_stage=side.defense_stage,
        special_attack_stage=side.special_attack_stage,
        special_defense_stage=side.special_defense_stage,
        speed_stage=side.speed_stage,
        accuracy_stage=side.accuracy_stage,
        evasion_stage=side.evasion_stage,
        side_effects=side.side_effects,
    )
    state = _confirmed_state(self_side=side)
    projection = build_rich_state_projection(state, (_legal_action(),))
    assert projection.self_side.attack_stage.status.value == "UNKNOWN"
    assert projection.self_side.attack_stage.value is None
    canonical = projection_to_canonical_dict(projection)
    assert canonical["self_side"]["attack_stage"] == {
        "status": "UNKNOWN",
        "provenance_chain": ["UNKNOWN"],
    }


def test_unknown_status_weather_terrain_not_coerced_to_none() -> None:
    side = _confirmed_side(active="Dondozo")
    side = SideState(
        active=side.active,
        hp_bucket=side.hp_bucket,
        status=Known.unknown(),
        attack_stage=side.attack_stage,
        defense_stage=side.defense_stage,
        special_attack_stage=side.special_attack_stage,
        special_defense_stage=side.special_defense_stage,
        speed_stage=side.speed_stage,
        accuracy_stage=side.accuracy_stage,
        evasion_stage=side.evasion_stage,
        side_effects=side.side_effects,
    )
    state = _confirmed_state(self_side=side, weather=Known.unknown(), terrain=Known.unknown())
    projection = build_rich_state_projection(state, (_legal_action(),))
    assert projection.self_side.status.status.value == "UNKNOWN"
    assert projection.weather.status.value == "UNKNOWN"
    assert projection.terrain.status.value == "UNKNOWN"
    canonical = projection_to_canonical_dict(projection)
    assert canonical["weather"]["status"] == "UNKNOWN"
    assert "value" not in canonical["weather"]
    assert canonical["terrain"]["status"] == "UNKNOWN"


def test_unknown_side_effects_not_coerced_to_empty_set() -> None:
    side = _confirmed_side(active="Dondozo")
    side = SideState(
        active=side.active,
        hp_bucket=side.hp_bucket,
        status=side.status,
        attack_stage=side.attack_stage,
        defense_stage=side.defense_stage,
        special_attack_stage=side.special_attack_stage,
        special_defense_stage=side.special_defense_stage,
        speed_stage=side.speed_stage,
        accuracy_stage=side.accuracy_stage,
        evasion_stage=side.evasion_stage,
        side_effects=Known.unknown(),
    )
    state = _confirmed_state(self_side=side)
    projection = build_rich_state_projection(state, (_legal_action(),))
    assert projection.self_side.side_effects.status.value == "UNKNOWN"
    assert projection.self_side.side_effects.value is None
    canonical = projection_to_canonical_dict(projection)
    assert canonical["self_side"]["side_effects"] == {
        "status": "UNKNOWN",
        "provenance_chain": ["UNKNOWN"],
    }
    # explicitly not an empty list/collection sentinel
    assert canonical["self_side"]["side_effects"].get("value") != []


# --- 5. Deterministic hashing --------------------------------------------------


def test_projection_hash_deterministic_for_identical_input() -> None:
    state = _confirmed_state()
    actions = (_legal_action(),)
    projection_1 = build_rich_state_projection(state, actions)
    projection_2 = build_rich_state_projection(state, actions)
    assert compute_projection_hash(projection_1) == compute_projection_hash(projection_2)


def test_hash_changes_on_field_value_change() -> None:
    state_a = _confirmed_state(self_side=_confirmed_side(active="Dondozo"))
    state_b = _confirmed_state(self_side=_confirmed_side(active="Gholdengo"))
    hash_a = compute_projection_hash(build_rich_state_projection(state_a, (_legal_action(),)))
    hash_b = compute_projection_hash(build_rich_state_projection(state_b, (_legal_action(),)))
    assert hash_a != hash_b


def test_hash_changes_on_knowledge_status_change() -> None:
    confirmed_side = _confirmed_side(active="Dondozo")
    unknown_terrain_state = _confirmed_state(self_side=confirmed_side, terrain=Known.unknown())
    known_terrain_state = _confirmed_state(
        self_side=confirmed_side, terrain=Known.confirmed("NONE", provenance_chain=_HUMAN)
    )
    hash_unknown = compute_projection_hash(
        build_rich_state_projection(unknown_terrain_state, (_legal_action(),))
    )
    hash_known = compute_projection_hash(
        build_rich_state_projection(known_terrain_state, (_legal_action(),))
    )
    assert hash_unknown != hash_known


def test_hash_changes_on_identity_revision_change() -> None:
    identity_a = _identity(battle_revision=1)
    identity_b = _identity(battle_revision=2)
    state_a = _confirmed_state(identity=identity_a)
    state_b = _confirmed_state(identity=identity_b)
    hash_a = compute_projection_hash(
        build_rich_state_projection(state_a, (_legal_action(identity=identity_a),))
    )
    hash_b = compute_projection_hash(
        build_rich_state_projection(state_b, (_legal_action(identity=identity_b),))
    )
    assert hash_a != hash_b


def test_hash_changes_on_legal_action_change() -> None:
    state = _confirmed_state()
    hash_move = compute_projection_hash(
        build_rich_state_projection(state, (_legal_action(action_name="Wave Crash"),))
    )
    hash_switch = compute_projection_hash(
        build_rich_state_projection(
            state, (_legal_action(action_type=ActionType.SWITCH, action_name="Gholdengo"),)
        )
    )
    assert hash_move != hash_switch


# --- 6. Existing one-attempt / legality / binding / Prompt v1 regression -----


def test_existing_dispatch_policy_unchanged_retry_denied() -> None:
    decision = decide_turn_advice_dispatch(
        trigger=DispatchTrigger.RETRY,
        is_current_binding=True,
        has_pending_job=False,
        attempt_consumed=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == "DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION"


def test_existing_dispatch_policy_unchanged_one_attempt() -> None:
    decision = decide_turn_advice_dispatch(
        trigger=DispatchTrigger.TRUSTED_HUMAN_ACTIVATION,
        is_current_binding=True,
        has_pending_job=False,
        attempt_consumed=True,
    )
    assert decision.allowed is False
    assert decision.reason_code == "DENY_ATTEMPT_ALREADY_CONSUMED"


def test_prompt_v1_response_schema_and_model_routing_unchanged() -> None:
    assert turn_request.CONTRACT_VERSION == "maple-turn-advice.v1"
    assert turn_request.CONTRACT_VERSION_V2 == "maple-turn-advice.v2"
    assert turn_request.TURN_PROMPT_VERSION == "maple-turn-prompt.v1"
    assert "strict JSON only" in turn_request._TURN_INITIAL_PROMPT
    assert turn_transport.DEFAULT_TURN_MODEL == "gemini-3.5-flash-lite"
    assert hasattr(turn_response, "TurnAdviceSchemaError")


# --- 7. maple-match.v3 export --------------------------------------------------


def _outcome() -> MatchOutcomeRecord:
    return MatchOutcomeRecord(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=MatchOutcome.WIN,
        ended_at_utc=CONFIRMED_AT,
        final_battle_revision=3,
    )


def _turn_record() -> ConfirmedTurnRecord:
    state = _confirmed_state()
    return ConfirmedTurnRecord(
        confirmed_state=state,
        source_delta=None,
        confirmed_legal_actions=(_legal_action(),),
        evidence=FixedEvidenceMetadata(
            evidence_id="evidence-1",
            relative_path="evidence/match-b1/turn-2.png",
            sha256="a" * 64,
            recorded_at_utc=CONFIRMED_AT,
        ),
    )


def test_v3_requires_at_least_one_rich_state_turn() -> None:
    with pytest.raises(MatchExportV3Error):
        build_match_export_v3_payload(
            session_id="session-b1",
            match_id="match-b1",
            generation=9,
            outcome=_outcome(),
            turns=(),
        )


def test_v3_payload_has_no_raw_bytes_or_secrets() -> None:
    payload = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    encoded = json.dumps(payload)
    for forbidden in ("api_key", "Authorization", "x-goog-api-key", "generateContent", "raw_bytes"):
        assert forbidden not in encoded
    assert payload["turns"][0]["evidence"]["relative_path"] == "evidence/match-b1/turn-2.png"
    assert "image_bytes" not in encoded


def test_v3_strict_parse_rejects_wrong_schema_version() -> None:
    payload = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    payload["schema_version"] = "maple-match.v2"
    raw = json.dumps(payload).encode("utf-8")
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v3(raw)


def test_v3_strict_parse_rejects_missing_keys() -> None:
    minimal = {"schema_version": MATCH_EXPORT_SCHEMA_VERSION_V3}
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v3(json.dumps(minimal).encode())


def test_v3_strict_parse_accepts_valid_payload() -> None:
    payload = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    raw = json.dumps(payload).encode("utf-8")
    parsed = parse_match_export_v3(raw)
    assert parsed["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V3


def test_v3_export_atomic_write_idempotent_and_stable_hash(tmp_path: Path) -> None:
    payload = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    export_dir = tmp_path / "exports"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    path_1, hash_1 = export_match_v3(
        export_directory=export_dir, repository_root=repo_root, match_id="match-b1", payload=payload
    )
    assert path_1.exists()
    assert hash_1 == compute_payload_sha256(payload)

    path_2, hash_2 = export_match_v3(
        export_directory=export_dir, repository_root=repo_root, match_id="match-b1", payload=payload
    )
    assert path_2 == path_1
    assert hash_2 == hash_1
    # No stray temp files left behind.
    assert list(export_dir.glob(".*.tmp")) == []


def test_v3_export_rejects_directory_inside_repository(tmp_path: Path) -> None:
    payload = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    repo_root = tmp_path / "repo"
    inside_dir = repo_root / "exports"
    inside_dir.mkdir(parents=True)
    with pytest.raises(MatchExportV3Error):
        export_match_v3(
            export_directory=inside_dir,
            repository_root=repo_root,
            match_id="match-b1",
            payload=payload,
        )


def test_v3_export_rejects_content_mismatch_for_same_match_id(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    payload_1 = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(_turn_record(),),
    )
    export_match_v3(
        export_directory=export_dir,
        repository_root=repo_root,
        match_id="match-b1",
        payload=payload_1,
    )

    different_action = ConfirmedTurnRecord(
        confirmed_state=_confirmed_state(confirmed_state_id="state-2"),
        source_delta=None,
        confirmed_legal_actions=(
            _legal_action(confirmation_id="legal-2", action_name="Flip Turn"),
        ),
        evidence=None,
    )
    payload_2 = build_match_export_v3_payload(
        session_id="session-b1",
        match_id="match-b1",
        generation=9,
        outcome=_outcome(),
        turns=(different_action,),
    )
    with pytest.raises(MatchExportV3Error):
        export_match_v3(
            export_directory=export_dir,
            repository_root=repo_root,
            match_id="match-b1",
            payload=payload_2,
        )


# --- 8. Legacy maple-match.v1/v2 regression ------------------------------------


def test_legacy_match_export_schema_versions_unchanged() -> None:
    from maple_next.application import match_service

    assert match_service.MATCH_EXPORT_SCHEMA_VERSION == "maple-match.v2"
    assert match_service.MATCH_EXPORT_SCHEMA_VERSION_V1 == "maple-match.v1"


# --- 9. Legal action boundary adapter stays import-isolated (Bundle A reuse) --


def test_provider_export_bridge_never_imports_transport_or_network_code() -> None:
    import ast
    import inspect

    for module in (
        __import__(
            "maple_next.application.turn_provider_export_bridge", fromlist=["_"]
        ),
        __import__("maple_next.providers.turn_advice_rich_state", fromlist=["_"]),
        __import__("maple_next.domain.turn_state_projection", fromlist=["_"]),
        __import__("maple_next.application.match_export_v3", fromlist=["_"]),
    ):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            elif isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
        for name in imported_modules:
            assert "turn_transport" not in name
            assert "urllib" not in name
            assert "requests" not in name
            assert "socket" not in name
