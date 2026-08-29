"""Tournament P0: unsupported Turn prediction recovery, kept fail-closed elsewhere."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from maple_next.application.service import PREDICTION_DOWNGRADED_TO_UNKNOWN
from maple_next.domain.enums import ResultDisposition
from maple_next.providers.transport import SanitizedProviderResult
from maple_next.providers.turn_advice_rich_state import (
    _TURN_INITIAL_PROMPT_V2,
    RICH_STATE_REQUEST_CONTRACT_VERSION,
)
from maple_next.providers.turn_response import TurnAdviceSchemaError
from maple_next.providers.turn_response_v2 import (
    REQUESTED_OUTPUT_SCHEMA_V2,
    RESPONSE_SCHEMA_VERSION_V2,
    normalize_degradable_opponent_prediction_v2,
    turn_advice_body_v2_from_dict,
)
from tests.test_issue31_turn_state_bundle_b_second_remediation import (
    RichSessionFixture,
    _valid_result_envelope,
)
from tests.test_issue31_turn_state_ui_bundle_c import (
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_production_compatible_window,
)


def _ready_fixture(path: Path) -> RichSessionFixture:
    fixture = RichSessionFixture(path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.confirm_legal_switches()
    return fixture


def _result_with_prediction(fixture: RichSessionFixture, prediction: dict):
    job = fixture.application.request_rich_turn_advice("prediction-recovery")
    result = _valid_result_envelope(job)
    payload = copy.deepcopy(result.payload)
    payload["opponent_prediction"] = prediction
    return job, replace(result, payload=payload)


def _unsupported_prediction(*, basis="NONE", support="LOW") -> dict:
    primary = {
        "category": "DAMAGING_MOVE",
        "specific_action": None,
        "summary": "相手は攻撃技を選ぶ可能性があります",
    }
    if basis is not ...:
        primary["support_basis"] = basis
    if support is not ...:
        primary["support"] = support
    return {"primary": primary, "alternatives": []}


def test_exact_incident_shape_stays_strict_in_parser_but_apply_downgrades(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "incident")
    job, result = _result_with_prediction(fixture, _unsupported_prediction())
    original_payload = copy.deepcopy(result.payload)

    with pytest.raises(
        TurnAdviceSchemaError,
        match="non_unknown_prediction_support_basis_must_not_be_none",
    ):
        turn_advice_body_v2_from_dict(result.payload)

    assert fixture.application.apply_rich_turn_advice_result(result) is ResultDisposition.APPLIED
    assert result.payload == original_payload
    advice = fixture.repository.get_turn_advice(result.result_id)
    assert advice.action_name == "Wave Crash"
    assert advice.advice_json is not None
    persisted = turn_advice_body_v2_from_dict(json.loads(advice.advice_json))
    assert persisted.opponent_prediction.primary.category == "UNKNOWN"
    assert persisted.opponent_prediction.primary.specific_action is None
    assert persisted.opponent_prediction.primary.support_basis == "NONE"
    assert persisted.opponent_prediction.primary.support == "LOW"
    assert persisted.opponent_prediction.alternatives == ()
    assert fixture.repository.result_audits(job.job_id) == [
        ("APPLIED", f"BINDING_ACCEPTED:{PREDICTION_DOWNGRADED_TO_UNKNOWN}")
    ]


@pytest.mark.parametrize(
    ("basis", "support"),
    [(..., "LOW"), ("", "LOW"), ("UNKNOWN", "LOW"), ("PINNED_RULES", ...)],
)
def test_absent_blank_unknown_or_malformed_support_is_canonical_unknown(basis, support) -> None:
    payload = {
        "response_schema_version": "maple-turn-advice-response.v2",
        "recommended_action": {
            "action_id": "legal-move-1",
            "action_type": "MOVE",
            "action_name": "Wave Crash",
        },
        "recommendation_robustness": "HIGH",
        "reasons": ["有効な推奨です"],
        "opponent_prediction": _unsupported_prediction(basis=basis, support=support),
        "warnings": [],
    }
    normalized, changed = normalize_degradable_opponent_prediction_v2(payload)
    assert changed is True
    body = turn_advice_body_v2_from_dict(normalized)
    assert body.opponent_prediction.primary.category == "UNKNOWN"
    assert body.opponent_prediction.alternatives == ()


def test_prompt_and_requested_schema_state_the_same_unknown_fallback() -> None:
    line_schema = REQUESTED_OUTPUT_SCHEMA_V2["properties"]["opponent_prediction"][
        "properties"
    ]["primary"]
    assert "NONE only with category UNKNOWN" in line_schema["properties"]["support_basis"][
        "description"
    ]
    assert "UNKNOWN must use LOW" in line_schema["properties"]["support"]["description"]
    assert "entire opponent_prediction block" in _TURN_INITIAL_PROMPT_V2
    assert "recommended_action complete and legal" in _TURN_INITIAL_PROMPT_V2
    assert RICH_STATE_REQUEST_CONTRACT_VERSION == "maple-turn-advice.v8"
    assert RESPONSE_SCHEMA_VERSION_V2 == "maple-turn-advice-response.v2"


def test_valid_supported_prediction_is_preserved(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "supported")
    prediction = _unsupported_prediction(basis="GENERAL_KNOWLEDGE", support="LOW")
    job, result = _result_with_prediction(fixture, prediction)
    assert fixture.application.apply_rich_turn_advice_result(result) is ResultDisposition.APPLIED
    advice = fixture.repository.get_turn_advice(result.result_id)
    assert advice.advice_json is not None
    persisted = turn_advice_body_v2_from_dict(json.loads(advice.advice_json))
    assert persisted.opponent_prediction.primary.category == "DAMAGING_MOVE"
    assert fixture.repository.result_audits(job.job_id) == [("APPLIED", "BINDING_ACCEPTED")]


def test_illegal_primary_advice_is_still_invalid(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "illegal")
    job, result = _result_with_prediction(fixture, _unsupported_prediction())
    payload = copy.deepcopy(result.payload)
    payload["recommended_action"]["action_name"] = "Not A Legal Move"
    result = replace(result, payload=payload)
    assert fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.INVALID_REJECTED
    )
    assert fixture.repository.result_audits(job.job_id)[0][0] == "INVALID_REJECTED"


def test_stale_response_is_still_stale(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "stale")
    job, result = _result_with_prediction(fixture, _unsupported_prediction())
    result = replace(result, input_snapshot_id="stale-confirmed-state")
    assert fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )
    assert fixture.repository.result_audits(job.job_id)[0][0] == "STALE_REJECTED"


def test_unknown_top_level_fields_are_still_invalid(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "unknown-field")
    job, result = _result_with_prediction(fixture, _unsupported_prediction())
    payload = copy.deepcopy(result.payload)
    payload["unexpected"] = "must not be accepted"
    assert fixture.application.apply_rich_turn_advice_result(
        replace(result, payload=payload)
    ) is ResultDisposition.INVALID_REJECTED
    assert fixture.repository.result_audits(job.job_id) == [
        ("INVALID_REJECTED", "INVALID_PAYLOAD:top_level_unknown_fields")
    ]


def test_real_battle_record_window_applies_downgraded_prediction_without_real_send(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport, _adapter = (
        build_production_compatible_window(tmp_path)
    )

    def injected_send(request, config):
        del config
        transport.call_count += 1
        transport.last_request = request
        action = request.legal_actions[0]
        return SanitizedProviderResult(
            payload={
                "response_schema_version": "maple-turn-advice-response.v2",
                "recommended_action": {
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                    "action_name": action.action_name,
                },
                "recommendation_robustness": "HIGH",
                "reasons": ["合法な推奨行動を維持します"],
                "opponent_prediction": _unsupported_prediction(),
                "warnings": [],
            },
            source_type="GEMINI",
            model="injected-no-network-model",
        )

    transport.send = injected_send  # type: ignore[method-assign]
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.confirm_turn_facts_button.click()
    window.render_view()

    view = controller.refresh()
    assert transport.call_count == 1
    assert view.turn_advice is not None
    assert view.turn_advice.structured_v2 is not None
    assert view.turn_advice.structured_v2.opponent_prediction.primary.category == "UNKNOWN"
    assert "UNKNOWN" in window.turn_advice_prediction_label.text()

    window.close()
    repository.close()
