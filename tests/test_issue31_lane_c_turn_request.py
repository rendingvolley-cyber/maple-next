from __future__ import annotations

import json

import pytest

from maple_next.domain.enums import ActionType, HpBucket
from maple_next.domain.models import ReviewedBoardSnapshot
from maple_next.providers.turn_request import (
    CONTRACT_VERSION,
    JOB_TYPE,
    REQUESTED_OUTPUT_SCHEMA,
    LegalAction,
    canonical_request_dict,
    compute_reviewed_snapshot_hash,
    encode_canonical_request,
    request_payload_hash,
)
from tests.fixtures.turn_advice import (
    MOVE_ACTION_1,
    SELECTED_THREE,
    SELF_ACTIVE,
    build_sample_request,
    build_sample_reviewed_snapshot,
)


def test_canonical_dict_contains_expected_fields() -> None:
    request = build_sample_request()
    canonical = canonical_request_dict(request)
    assert canonical["contract_version"] == CONTRACT_VERSION
    assert canonical["job_type"] == JOB_TYPE
    assert canonical["session_id"] == "session-1"
    assert canonical["match_id"] == "match-1"
    assert canonical["generation"] == 3
    assert canonical["turn_number"] == 5
    assert canonical["battle_revision"] == 7
    assert canonical["reviewed_snapshot_id"] == "board-1"
    assert canonical["self_active"] == SELF_ACTIVE
    assert canonical["selected_three"] == list(SELECTED_THREE)
    assert canonical["requested_output_schema"] == REQUESTED_OUTPUT_SCHEMA
    assert len(canonical["legal_actions"]) == 3


def test_canonical_dict_never_contains_secrets_or_timing() -> None:
    canonical = canonical_request_dict(build_sample_request())
    forbidden_keys = {
        "api_key",
        "authorization",
        "model",
        "timeout",
        "timeout_seconds",
        "human_authorized_at",
        "created_at",
        "endpoint",
        "headers",
    }
    assert forbidden_keys.isdisjoint(canonical.keys())


def test_hp_buckets_are_preserved_verbatim_never_converted_to_numbers() -> None:
    snapshot = build_sample_reviewed_snapshot()
    canonical = canonical_request_dict(build_sample_request(reviewed_snapshot=snapshot))
    facts = canonical["reviewed_snapshot_facts"]
    assert facts["self_hp"] == "71-80"
    assert facts["opponent_hp"] == "41-50"

    unknown_snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board-2",
        turn_id="turn-2",
        self_active=SELF_ACTIVE,
        opponent_active="Garchomp",
        self_hp=HpBucket.UNKNOWN,
        opponent_hp=HpBucket.ZERO,
        self_status="",
        opponent_status="",
    )
    canonical_unknown = canonical_request_dict(
        build_sample_request(
            reviewed_snapshot_id="board-2",
            reviewed_snapshot=unknown_snapshot,
        )
    )
    facts_unknown = canonical_unknown["reviewed_snapshot_facts"]
    assert facts_unknown["self_hp"] == "UNKNOWN"
    assert facts_unknown["opponent_hp"] == "0"


def test_encoding_is_deterministic_across_dict_insertion_order() -> None:
    request = build_sample_request()
    first = encode_canonical_request(request)
    second = encode_canonical_request(request)
    assert first == second

    # Manually rebuild the same canonical dict with keys inserted in the
    # opposite order; ``sort_keys=True`` must make the encoded bytes
    # byte-identical regardless of Python dict insertion order.
    original = canonical_request_dict(request)
    reordered_dict = dict(reversed(list(original.items())))
    reordered_bytes = json.dumps(
        reordered_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert reordered_bytes == first


def test_request_payload_hash_is_deterministic_and_hex() -> None:
    request = build_sample_request()
    first = request_payload_hash(request)
    second = request_payload_hash(build_sample_request())
    assert first == second
    assert len(first) == 64
    int(first, 16)


def test_request_payload_hash_changes_when_any_bound_field_changes() -> None:
    baseline = request_payload_hash(build_sample_request())
    assert baseline != request_payload_hash(build_sample_request(session_id="other-session"))
    assert baseline != request_payload_hash(build_sample_request(match_id="other-match"))
    assert baseline != request_payload_hash(build_sample_request(generation=99))
    assert baseline != request_payload_hash(build_sample_request(turn_number=99))
    assert baseline != request_payload_hash(build_sample_request(battle_revision=99))


def test_reviewed_snapshot_hash_is_deterministic_and_changes_with_content() -> None:
    snapshot = build_sample_reviewed_snapshot()
    first = compute_reviewed_snapshot_hash(snapshot)
    second = compute_reviewed_snapshot_hash(build_sample_reviewed_snapshot())
    assert first == second
    assert len(first) == 64
    int(first, 16)

    changed = ReviewedBoardSnapshot(
        reviewed_board_id=snapshot.reviewed_board_id,
        turn_id=snapshot.turn_id,
        self_active=snapshot.self_active,
        opponent_active=snapshot.opponent_active,
        self_hp=HpBucket.FULL,
        opponent_hp=snapshot.opponent_hp,
        self_status=snapshot.self_status,
        opponent_status=snapshot.opponent_status,
    )
    assert compute_reviewed_snapshot_hash(changed) != first


def test_reviewed_snapshot_hash_bound_into_request_payload_hash() -> None:
    baseline = build_sample_request()
    different_snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board-1",
        turn_id="turn-1",
        self_active=SELF_ACTIVE,
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.FULL,
        self_status="",
        opponent_status="",
    )
    changed = build_sample_request(reviewed_snapshot=different_snapshot)
    assert baseline.reviewed_snapshot_hash != changed.reviewed_snapshot_hash
    assert request_payload_hash(baseline) != request_payload_hash(changed)


def test_legal_action_requires_unique_action_id() -> None:
    duplicated = (MOVE_ACTION_1, MOVE_ACTION_1)
    with pytest.raises(ValueError, match="unique"):
        build_sample_request(legal_actions=duplicated)


def test_legal_actions_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_sample_request(legal_actions=())


def test_move_owner_active_must_exactly_equal_self_active() -> None:
    bad_move = LegalAction(
        action_id="move-x",
        action_type=ActionType.MOVE,
        action_name="Draco Meteor",
        owner_active="Dragonite",
    )
    with pytest.raises(ValueError, match="owner_active"):
        build_sample_request(legal_actions=(bad_move,))


def test_move_owner_active_no_alias_or_whitespace_trim_magic() -> None:
    # A trailing space is a *different* string; it must not be trimmed and
    # silently accepted as matching self_active.
    padded_move = LegalAction(
        action_id="move-y",
        action_type=ActionType.MOVE,
        action_name="Shadow Ball",
        owner_active=SELF_ACTIVE + " ",
    )
    with pytest.raises(ValueError, match="owner_active"):
        build_sample_request(legal_actions=(padded_move,))


def test_switch_target_must_be_in_selected_three() -> None:
    bad_switch = LegalAction(
        action_id="switch-x",
        action_type=ActionType.SWITCH,
        action_name="Urshifu",
        switch_target="Urshifu",
    )
    with pytest.raises(ValueError, match="selected_three"):
        build_sample_request(legal_actions=(bad_switch,))


def test_switch_target_must_not_equal_self_active() -> None:
    bad_switch = LegalAction(
        action_id="switch-x",
        action_type=ActionType.SWITCH,
        action_name=SELF_ACTIVE,
        switch_target=SELF_ACTIVE,
    )
    with pytest.raises(ValueError, match="self_active"):
        build_sample_request(legal_actions=(bad_switch,))


def test_move_legal_action_rejects_switch_target_field() -> None:
    with pytest.raises(ValueError, match="switch_target"):
        LegalAction(
            action_id="move-z",
            action_type=ActionType.MOVE,
            action_name="Shadow Ball",
            owner_active=SELF_ACTIVE,
            switch_target="Dragonite",
        )


def test_switch_legal_action_rejects_owner_active_field() -> None:
    with pytest.raises(ValueError, match="owner_active"):
        LegalAction(
            action_id="switch-z",
            action_type=ActionType.SWITCH,
            action_name="Dragonite",
            switch_target="Dragonite",
            owner_active=SELF_ACTIVE,
        )


def test_legal_action_rejects_empty_action_name() -> None:
    with pytest.raises(ValueError, match="action_name"):
        LegalAction(
            action_id="move-empty",
            action_type=ActionType.MOVE,
            action_name="   ",
            owner_active=SELF_ACTIVE,
        )


def test_self_active_must_be_in_selected_three() -> None:
    snapshot = build_sample_reviewed_snapshot()
    other_active_snapshot = ReviewedBoardSnapshot(
        reviewed_board_id=snapshot.reviewed_board_id,
        turn_id=snapshot.turn_id,
        self_active="Urshifu",
        opponent_active=snapshot.opponent_active,
        self_hp=snapshot.self_hp,
        opponent_hp=snapshot.opponent_hp,
        self_status=snapshot.self_status,
        opponent_status=snapshot.opponent_status,
    )
    with pytest.raises(ValueError, match="selected_three"):
        build_sample_request(
            self_active="Urshifu",
            reviewed_snapshot=other_active_snapshot,
            legal_actions=(MOVE_ACTION_1,),
        )


def test_self_active_must_match_reviewed_snapshot_self_active() -> None:
    with pytest.raises(ValueError, match="self_active"):
        build_sample_request(self_active="Dragonite")


def test_selected_three_must_be_three_distinct_names() -> None:
    with pytest.raises(ValueError, match="selected_three"):
        build_sample_request(selected_three=("Gholdengo", "Gholdengo", "Dondozo"))


def test_contract_version_and_job_type_are_fixed() -> None:
    request = build_sample_request()
    assert request.contract_version == "maple-turn-advice.v1"
    assert request.job_type == "TURN_ADVICE"


def test_encode_canonical_request_round_trips_as_valid_json() -> None:
    request = build_sample_request()
    decoded = json.loads(encode_canonical_request(request).decode("utf-8"))
    assert decoded == canonical_request_dict(request)
