"""Gemini V2 Bundle 6: response-parser version dispatch and legality reuse.

``select_response_parser_version`` is a pure function keyed on the trusted
request/job contract version -- never on any claim inside a provider
response body. ``validate_turn_advice_legality_v2`` reuses the exact same
core as the v1 :func:`validate_turn_advice_legality` (structurally, via
:class:`~maple_next.providers.turn_response.RecommendedAction`), so these
tests reuse the same legacy-request fixture the v1 legality tests use --
the Protocol only needs ``self_active``/``selected_three``/``legal_actions``,
which a legacy ``TurnAdviceRequest`` already provides.
"""

from __future__ import annotations

import pytest

from maple_next.providers.turn_advice_rich_state import RICH_STATE_REQUEST_CONTRACT_VERSION
from maple_next.providers.turn_request import CONTRACT_VERSION, CONTRACT_VERSION_V2
from maple_next.providers.turn_response import RecommendedAction
from maple_next.providers.turn_validation import (
    TurnAdviceParseError,
    TurnAdviceResultCode,
    select_response_parser_version,
    validate_turn_advice_legality_v2,
)
from tests.fixtures.turn_advice import LEGAL_ACTIONS, build_sample_request

# =========================================================================
# A. VERSION DISPATCH
# =========================================================================


def test_legacy_v1_contract_dispatches_to_v1_parser() -> None:
    assert select_response_parser_version(CONTRACT_VERSION) == "v1"


def test_legacy_v2_contract_dispatches_to_v1_parser() -> None:
    assert select_response_parser_version(CONTRACT_VERSION_V2) == "v1"


@pytest.mark.parametrize(
    "historical_version",
    [
        "maple-turn-advice.v3",
        "maple-turn-advice.v4",
        "maple-turn-advice.v5",
        "maple-turn-advice.v6",
    ],
)
def test_pre_v7_rich_contract_dispatches_to_v1_parser(historical_version: str) -> None:
    assert select_response_parser_version(historical_version) == "v1"


def test_v7_rich_contract_dispatches_to_v2_parser() -> None:
    assert RICH_STATE_REQUEST_CONTRACT_VERSION == "maple-turn-advice.v7"
    assert select_response_parser_version(RICH_STATE_REQUEST_CONTRACT_VERSION) == "v2"


def test_unsupported_contract_version_fails_closed() -> None:
    with pytest.raises(TurnAdviceParseError):
        select_response_parser_version("maple-turn-advice.v99")


def test_empty_contract_version_fails_closed() -> None:
    with pytest.raises(TurnAdviceParseError):
        select_response_parser_version("")


# =========================================================================
# B. LEGAL ACTION (v2 legality reuse)
# =========================================================================


def test_valid_exact_recommendation_accepted() -> None:
    request = build_sample_request()
    action = RecommendedAction(action_id="move-1", action_type="MOVE", action_name="Make It Rain")
    assert validate_turn_advice_legality_v2(request, action) is TurnAdviceResultCode.VALID


def test_action_id_mismatch_rejected() -> None:
    request = build_sample_request()
    action = RecommendedAction(
        action_id="move-nope", action_type="MOVE", action_name="Make It Rain"
    )
    code = validate_turn_advice_legality_v2(request, action)
    assert code is TurnAdviceResultCode.ILLEGAL_ACTION


def test_action_type_mismatch_rejected() -> None:
    request = build_sample_request()
    action = RecommendedAction(
        action_id="move-1", action_type="SWITCH", action_name="Make It Rain"
    )
    code = validate_turn_advice_legality_v2(request, action)
    assert code is TurnAdviceResultCode.ILLEGAL_ACTION


def test_action_name_mismatch_rejected() -> None:
    request = build_sample_request()
    action = RecommendedAction(
        action_id="move-1", action_type="MOVE", action_name="Not A Real Move"
    )
    code = validate_turn_advice_legality_v2(request, action)
    assert code is TurnAdviceResultCode.ILLEGAL_ACTION


def test_switch_target_ownership_enforced() -> None:
    request = build_sample_request()
    assert LEGAL_ACTIONS[2].action_type.value == "SWITCH"
    action = RecommendedAction(
        action_id="switch-1", action_type="SWITCH", action_name="Dragonite"
    )
    assert validate_turn_advice_legality_v2(request, action) is TurnAdviceResultCode.VALID
