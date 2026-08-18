"""Gemini V2 Bundle 6: ``maple-match.v4`` export -- structured_response embedding.

Pure, dict-level tests of the parser/round-trip contract:
:func:`parse_match_export_v3` stays completely unchanged, and
:func:`parse_match_export_v4` accepts an otherwise-identical v3 document
plus an additive per-turn ``structured_response``/``response_schema_version``
pair, failing closed on a corrupt or incomplete one.
"""

from __future__ import annotations

import copy
import json

import pytest

from maple_next.application.match_export_v3 import (
    MATCH_EXPORT_SCHEMA_VERSION_V3,
    MATCH_EXPORT_SCHEMA_VERSION_V4,
    MatchExportV3Error,
    parse_match_export_v3,
    parse_match_export_v4,
)
from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V2,
    canonical_turn_advice_v2_json,
    turn_advice_body_v2_from_dict,
)

_VALID_V2_BODY: dict[str, object] = {
    "response_schema_version": RESPONSE_SCHEMA_VERSION_V2,
    "recommended_action": {
        "action_id": "move-1",
        "action_type": "MOVE",
        "action_name": "Make It Rain",
    },
    "recommendation_robustness": "HIGH",
    "reasons": ["確定情報から有利"],
    "opponent_prediction": {
        "primary": {
            "category": "DAMAGING_MOVE",
            "specific_action": None,
            "support_basis": "GENERAL_KNOWLEDGE",
            "support": "LOW",
            "summary": "相手はダメージ技を選択",
        },
        "alternatives": [],
    },
    "warnings": [],
}


def _base_turn(turn_number: int = 1) -> dict[str, object]:
    return {
        "turn_number": turn_number,
        "reviewed_facts": {
            "self_active": "Gholdengo",
            "opponent_active": "Garchomp",
            "self_hp": "71-80",
            "opponent_hp": "41-50",
            "legal_moves": [],
            "legal_switches": [],
            "human_note": "",
            "provenance": "HUMAN_CONFIRMED",
            "created_at_utc": "2026-08-18T00:00:00+00:00",
        },
        "advice": {
            "source_type": "GEMINI",
            "model": "gemini-2.5-flash",
            "recommended_action_type": "MOVE",
            "recommended_action_name": "Make It Rain",
            "opponent_prediction": "相手はダメージ技を選択",
            "rationale": "確定情報から有利",
            "warnings": [],
            "binding": "APPLIED",
            "legality": "VALID",
            "created_at_utc": "2026-08-18T00:00:00+00:00",
        },
        "self_executed_action": {"action_type": "MOVE", "action_name": "Make It Rain"},
        "opponent_executed_action": None,
        "action_order": "SELF_FIRST",
        "recorded_at_utc": "2026-08-18T00:00:01+00:00",
        "actual_action": {"action_type": "MOVE", "action_name": "Make It Rain"},
    }


def _base_payload(*, schema_version: str, turns: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "session_id": "session-1",
        "match_id": "match-1",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": "2026-08-18T00:05:00+00:00",
        "final_battle_revision": 3,
        "selection": {
            "self_team": ["Gholdengo", "Dragonite", "Dondozo"],
            "opponent_team": ["Garchomp"],
            "selected_three": ["Gholdengo", "Dragonite", "Dondozo"],
            "lead": "Gholdengo",
        },
        "turns": turns,
        "action_history": [],
    }


def _encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


# =========================================================================
# I. EXPORT
# =========================================================================


def test_old_v3_export_remains_readable() -> None:
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V3, turns=[_base_turn()])
    parsed = parse_match_export_v3(_encode(payload))
    assert parsed["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V3
    assert "structured_response" not in parsed["turns"][0]


def test_v2_containing_match_exports_v4_and_round_trips() -> None:
    body = turn_advice_body_v2_from_dict(_VALID_V2_BODY)
    structured = json.loads(canonical_turn_advice_v2_json(body))
    turn = _base_turn()
    turn["response_schema_version"] = RESPONSE_SCHEMA_VERSION_V2
    turn["structured_response"] = structured
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[turn])

    parsed = parse_match_export_v4(_encode(payload))
    assert parsed["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V4
    assert parsed["turns"][0]["structured_response"] == structured
    # v3's flattened advice fields are still fully populated alongside it.
    assert parsed["turns"][0]["advice"]["recommended_action_name"] == "Make It Rain"


def test_v4_mixed_v1_and_v2_turns_round_trips() -> None:
    body = turn_advice_body_v2_from_dict(_VALID_V2_BODY)
    structured = json.loads(canonical_turn_advice_v2_json(body))
    v1_turn = _base_turn(1)
    v2_turn = _base_turn(2)
    v2_turn["response_schema_version"] = RESPONSE_SCHEMA_VERSION_V2
    v2_turn["structured_response"] = structured
    payload = _base_payload(
        schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[v1_turn, v2_turn]
    )
    parsed = parse_match_export_v4(_encode(payload))
    assert "structured_response" not in parsed["turns"][0]
    assert parsed["turns"][1]["structured_response"] == structured


def test_v4_corrupt_structured_response_fails_closed() -> None:
    turn = _base_turn()
    turn["response_schema_version"] = RESPONSE_SCHEMA_VERSION_V2
    turn["structured_response"] = {"response_schema_version": RESPONSE_SCHEMA_VERSION_V2}
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[turn])
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v4(_encode(payload))


def test_v4_incomplete_structured_response_fields_fails_closed() -> None:
    body = turn_advice_body_v2_from_dict(_VALID_V2_BODY)
    structured = json.loads(canonical_turn_advice_v2_json(body))
    turn = _base_turn()
    turn["structured_response"] = structured
    # response_schema_version deliberately omitted.
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[turn])
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v4(_encode(payload))


def test_v4_wrong_response_schema_version_tag_fails_closed() -> None:
    body = turn_advice_body_v2_from_dict(_VALID_V2_BODY)
    structured = json.loads(canonical_turn_advice_v2_json(body))
    turn = _base_turn()
    turn["response_schema_version"] = "maple-turn-advice-response.v1"
    turn["structured_response"] = structured
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[turn])
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v4(_encode(payload))


def test_v4_parser_rejects_v3_schema_version() -> None:
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V3, turns=[_base_turn()])
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v4(_encode(payload))


def test_v3_parser_rejects_v4_schema_version() -> None:
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V4, turns=[_base_turn()])
    with pytest.raises(MatchExportV3Error):
        parse_match_export_v3(_encode(payload))


def test_historical_v3_export_bytes_are_never_rewritten_by_v4_support() -> None:
    payload = _base_payload(schema_version=MATCH_EXPORT_SCHEMA_VERSION_V3, turns=[_base_turn()])
    encoded = _encode(payload)
    parsed_once = parse_match_export_v3(copy.deepcopy(encoded))
    parsed_twice = parse_match_export_v3(copy.deepcopy(encoded))
    assert parsed_once == parsed_twice
    assert parsed_once["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V3
