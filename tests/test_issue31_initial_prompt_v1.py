from __future__ import annotations

import hashlib
import json

from maple_next.domain.enums import ActionType, HpBucket
from maple_next.domain.models import ReviewedBoardSnapshot, StatStages
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)
from maple_next.providers.selection_request import (
    REQUESTED_OUTPUT_SCHEMA as SELECTION_OUTPUT_SCHEMA,
    SELECTION_PROMPT_VERSION,
    build_provider_prompt as build_selection_prompt,
    build_selection_advice_request,
    encode_canonical_request as encode_selection_request,
    request_payload_hash as selection_request_hash,
)
from maple_next.providers.transport import (
    DEFAULT_SELECTION_FALLBACK_MODEL,
    DEFAULT_SELECTION_PRIMARY_MODEL,
    SelectionProviderConfig,
)
from maple_next.providers.turn_request import (
    REQUESTED_OUTPUT_SCHEMA as TURN_OUTPUT_SCHEMA,
    TURN_PROMPT_VERSION,
    LegalAction,
    build_provider_prompt as build_turn_prompt,
    build_turn_advice_request,
    encode_canonical_request as encode_turn_request,
    request_payload_hash as turn_request_hash,
)
from maple_next.providers.turn_transport import DEFAULT_TURN_MODEL

SELF_TEAM = (
    "ピカチュウ",
    "サーフゴー",
    "カイリュー",
    "ヘイラッシャ",
    "ハバタクカミ",
    "ウーラオス",
)
OPPONENT_TEAM = (
    "ガブリアス",
    "サーフゴー",
    "カイリュー",
    "キョジオーン",
    "テツノツツミ",
    "モロバレル",
)
SELECTED_THREE = (SELF_TEAM[0], SELF_TEAM[1], SELF_TEAM[2])
_PROMPT_MARKER = "\n\nCanonical request:\n"


def _member(name: str, index: int) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=(f"{name}の技A", f"{name}の技B"),
        held_item=None if index == 0 else f"{name}の道具",
        ability=f"{name}の特性",
        nature="まじめ",
        stat_points=ChampionsStatPoints(hp=10, speed=10),
    )


def _team_build() -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name="Unicode detailed team",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(_member(name, index) for index, name in enumerate(SELF_TEAM)),
    )


def _selection_request(
    build: ChampionsTeamBuild | None = None,
):
    return build_selection_advice_request(
        session_id="session-初期",
        match_id="match-選出",
        generation=7,
        battle_revision=11,
        reviewed_selection_id="selection-確認済み",
        self_team=SELF_TEAM,
        opponent_team=OPPONENT_TEAM,
        self_team_build=build,
    )


def _turn_request(
    build: ChampionsTeamBuild | None = None,
):
    snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board-確認済み",
        turn_id="turn-1",
        self_active=SELF_TEAM[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp=HpBucket.FIFTY_ONE_TO_SIXTY,
        opponent_hp=HpBucket.UNKNOWN,
        self_status="NONE",
        opponent_status="UNKNOWN",
        self_stages=StatStages(speed=1),
        opponent_stages=StatStages(defense=-1),
        weather="RAIN",
        terrain="UNKNOWN",
        self_side_effects=("REFLECT",),
        opponent_side_effects=("STEALTH_ROCK",),
    )
    return build_turn_advice_request(
        session_id="session-初期",
        match_id="match-選出",
        generation=7,
        turn_number=1,
        battle_revision=12,
        reviewed_snapshot_id=snapshot.reviewed_board_id,
        reviewed_snapshot=snapshot,
        self_active=SELF_TEAM[0],
        selected_three=SELECTED_THREE,
        legal_actions=(
            LegalAction(
                action_id="move-10まんボルト",
                action_type=ActionType.MOVE,
                action_name="10まんボルト",
                owner_active=SELF_TEAM[0],
            ),
            LegalAction(
                action_id="switch-サーフゴー",
                action_type=ActionType.SWITCH,
                action_name=SELF_TEAM[1],
                switch_target=SELF_TEAM[1],
            ),
        ),
        self_team_build=build,
    )


def _canonical_payload(prompt: str) -> dict[str, object]:
    prefix, payload = prompt.split(_PROMPT_MARKER, maxsplit=1)
    assert prefix
    decoded = json.loads(payload)
    assert isinstance(decoded, dict)
    return decoded


def test_prompt_versions_are_fixed() -> None:
    assert SELECTION_PROMPT_VERSION == "maple-selection-prompt.v1"
    assert TURN_PROMPT_VERSION == "maple-turn-prompt.v1"


def test_same_canonical_request_produces_same_prompt_bytes() -> None:
    selection_first = build_selection_prompt(_selection_request()).encode("utf-8")
    selection_second = build_selection_prompt(_selection_request()).encode("utf-8")
    turn_first = build_turn_prompt(_turn_request()).encode("utf-8")
    turn_second = build_turn_prompt(_turn_request()).encode("utf-8")

    assert selection_first == selection_second
    assert turn_first == turn_second


def test_selection_names_only_prompt_has_v1_policy_and_unicode_request() -> None:
    prompt = build_selection_prompt(_selection_request())
    payload = _canonical_payload(prompt)

    assert "Evaluate all six members of self_team." in prompt
    assert "Compare multiple possible three-Pokémon combinations." in prompt
    assert "all six opponent names" in prompt
    assert "input order" in prompt
    assert "one favorable matchup" in prompt
    assert "When self_team_build is absent, do not assume" in prompt
    assert "opponent has a" in prompt
    assert "role as a confirmed fact" in prompt
    assert "Use only exact names in self_team." in prompt
    assert "Return strict JSON only." in prompt
    assert payload["self_team"] == list(SELF_TEAM)
    assert payload["opponent_team"] == list(OPPONENT_TEAM)
    assert "self_team_build" not in payload
    assert "ピカチュウ" in prompt
    assert "ガブリアス" in prompt


def test_selection_detailed_build_keeps_v2_canonical_payload() -> None:
    build = _team_build()
    request = _selection_request(build)
    payload = _canonical_payload(build_selection_prompt(request))

    assert request.requested_output_schema == SELECTION_OUTPUT_SCHEMA
    assert payload["contract_version"] == "maple-selection-advice.v2"
    assert payload["self_team_build"] == build.to_canonical_dict()
    assert payload["self_team_build_sha256"] == build.sha256()
    assert "opponent_team_build" not in payload


def test_turn_names_only_prompt_compares_every_legal_action_conservatively() -> None:
    prompt = build_turn_prompt(_turn_request())
    payload = _canonical_payload(prompt)
    legal_actions = payload["legal_actions"]

    assert "HP values are buckets, not exact percentages." in prompt
    assert "Do not assume the current HP or status of a benched switch target." in prompt
    assert "opponent's build and remaining selected Pokémon are not confirmed" in prompt
    assert "silently compare every legal_actions entry" in prompt
    assert "MOVE, SWITCH," in prompt
    assert "STATUS_OR_SETUP" in prompt
    assert "Use UNKNOWN when evidence is insufficient." in prompt
    assert "Set predicted_action to null" in prompt
    assert "Copy action_id, action_type, and action_name exactly." in prompt
    assert "return strict JSON only." in prompt
    assert isinstance(legal_actions, list)
    assert {action["action_type"] for action in legal_actions} == {"MOVE", "SWITCH"}
    assert payload["reviewed_snapshot_facts"]["self_hp"] == "51-60"
    assert payload["reviewed_snapshot_facts"]["opponent_hp"] == "UNKNOWN"
    assert "selected_three_builds" not in payload


def test_turn_detailed_build_keeps_v2_canonical_payload() -> None:
    build = _team_build()
    request = _turn_request(build)
    payload = _canonical_payload(build_turn_prompt(request))

    assert request.requested_output_schema == TURN_OUTPUT_SCHEMA
    assert payload["contract_version"] == "maple-turn-advice.v2"
    assert payload["self_active_build"]["pokemon"] == SELF_TEAM[0]
    assert {
        member["pokemon"] for member in payload["selected_three_builds"]
    } == set(SELECTED_THREE)
    assert "Confirmed player build details when present." in build_turn_prompt(request)


def test_prompt_generation_does_not_change_request_hashes_or_schemas() -> None:
    selection = _selection_request(_team_build())
    turn = _turn_request(_team_build())
    selection_before = selection_request_hash(selection)
    turn_before = turn_request_hash(turn)

    build_selection_prompt(selection)
    build_turn_prompt(turn)

    assert selection_request_hash(selection) == selection_before
    assert turn_request_hash(turn) == turn_before
    assert selection_before == hashlib.sha256(encode_selection_request(selection)).hexdigest()
    assert turn_before == hashlib.sha256(encode_turn_request(turn)).hexdigest()
    assert selection.requested_output_schema == SELECTION_OUTPUT_SCHEMA
    assert turn.requested_output_schema == TURN_OUTPUT_SCHEMA


def test_prompts_do_not_leak_secrets_models_transport_or_runtime_paths() -> None:
    prompts = (
        build_selection_prompt(_selection_request(_team_build())),
        build_turn_prompt(_turn_request(_team_build())),
    )
    forbidden = (
        "MAPLE_NEXT_GEMINI_API_KEY",
        "x-goog-api-key",
        "Authorization:",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "generativelanguage.googleapis.com",
        "C:\\Users\\",
        ".env",
    )
    for prompt in prompts:
        for token in forbidden:
            assert token not in prompt


def test_model_routing_constants_and_selection_fallback_config_are_unchanged() -> None:
    assert DEFAULT_SELECTION_PRIMARY_MODEL == "gemini-3.6-flash"
    assert DEFAULT_SELECTION_FALLBACK_MODEL == "gemini-3.5-flash"
    assert DEFAULT_TURN_MODEL == "gemini-3.5-flash-lite"

    config = SelectionProviderConfig(api_key="test-only-placeholder")
    assert config.primary().model == DEFAULT_SELECTION_PRIMARY_MODEL
    assert config.fallback().model == DEFAULT_SELECTION_FALLBACK_MODEL
