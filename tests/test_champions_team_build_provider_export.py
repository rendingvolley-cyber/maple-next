from __future__ import annotations

import json
from pathlib import Path

from maple_next.application.match_service import MATCH_EXPORT_SCHEMA_VERSION_V2, MatchApplication
from maple_next.domain.enums import ActionType, HpBucket, MatchOutcome, ResultDisposition
from maple_next.domain.models import ReviewedBoardSnapshot
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.selection_request import CONTRACT_VERSION_V2 as SELECTION_CONTRACT_V2
from maple_next.providers.selection_request import build_selection_advice_request
from maple_next.providers.selection_request import canonical_request_dict as selection_request_dict
from maple_next.providers.turn_request import CONTRACT_VERSION_V2 as TURN_CONTRACT_V2
from maple_next.providers.turn_request import LegalAction, build_turn_advice_request
from maple_next.providers.turn_request import canonical_request_dict as turn_request_dict
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter

TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT = ("O1", "O2", "O3", "O4", "O5", "O6")


def _member(name: str) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=(f"{name} move 1", f"{name} move 2"),
        held_item=None,
        ability=f"{name} ability",
        nature="Serious",
        stat_points=ChampionsStatPoints(),
    )


def _build() -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name="Detailed team",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(_member(name) for name in TEAM),
    )


def test_detailed_selection_and_turn_requests_bind_only_allowed_build_data() -> None:
    build = _build()
    selection = build_selection_advice_request(
        session_id="session",
        match_id="match",
        generation=1,
        battle_revision=2,
        reviewed_selection_id="selection",
        self_team=TEAM,
        opponent_team=OPPONENT,
        self_team_build=build,
    )
    selection_payload = selection_request_dict(selection)
    assert selection.contract_version == SELECTION_CONTRACT_V2
    assert selection_payload["self_team_build"] == build.to_canonical_dict()
    assert "opponent_team_build" not in selection_payload

    snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board",
        turn_id="turn",
        self_active="A",
        opponent_active="O1",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.FULL,
        self_status="NONE",
        opponent_status="NONE",
    )
    turn = build_turn_advice_request(
        session_id="session",
        match_id="match",
        generation=1,
        turn_number=1,
        battle_revision=3,
        reviewed_snapshot_id="board",
        reviewed_snapshot=snapshot,
        self_active="A",
        selected_three=("A", "C", "F"),
        legal_actions=(
            LegalAction("move", ActionType.MOVE, "A move 1", owner_active="A"),
            LegalAction("switch", ActionType.SWITCH, "C", switch_target="C"),
        ),
        self_team_build=build,
    )
    turn_payload = turn_request_dict(turn)
    assert turn.contract_version == TURN_CONTRACT_V2
    assert {member["pokemon"] for member in turn_payload["selected_three_builds"]} == {
        "A",
        "C",
        "F",
    }
    assert turn_payload["self_active_build"]["pokemon"] == "A"
    assert "B move 1" not in json.dumps(turn_payload)


def test_detailed_match_export_contains_immutable_build_snapshot(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "runtime.db")
    application = MatchApplication(repository, tmp_path / "exports")
    selection_adapter = MockSelectionAdviceAdapter()
    try:
        application.new_match()
        application.confirm_selection_facts(TEAM, OPPONENT, _build())
        advice = selection_adapter.submit(
            application,
            selected_three=("A", "C", "F"),
            lead="A",
        )
        assert advice.disposition is ResultDisposition.APPLIED
        application.apply_selection(
            selected_three=("A", "C", "F"),
            lead="A",
            human_confirmed=True,
        )
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        record = application.export_match()
        payload = json.loads(Path(record.export_path).read_text(encoding="utf-8"))
        assert payload["schema_version"] == MATCH_EXPORT_SCHEMA_VERSION_V2
        assert payload["selection"]["self_team_build"] == _build().to_canonical_dict()
        assert payload["selection"]["self_team_build_sha256"] == _build().sha256()
        assert {member["pokemon"] for member in payload["selection"]["selected_three_builds"]} == {
            "A",
            "C",
            "F",
        }
        assert "tera" not in json.dumps(payload)
    finally:
        repository.close()
