from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maple_next.application.service import BattleApplication
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)
from maple_next.persistence.sqlite import SQLiteRepository

TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT = ("O1", "O2", "O3", "O4", "O5", "O6")


def _member(name: str, *, speed: int = 0) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=(f"{name} move 1", f"{name} move 2"),
        held_item=None,
        ability=f"{name} ability",
        nature="Serious",
        stat_points=ChampionsStatPoints(speed=speed),
    )


def _build(*, speed: int = 0) -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name="Detailed team",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(_member(name, speed=speed) for name in TEAM),
    )


@pytest.mark.parametrize("value", [-1, 33])
def test_stat_point_per_stat_limits(value: int) -> None:
    with pytest.raises(ValueError):
        ChampionsStatPoints(hp=value)


def test_stat_points_reject_bool_and_total_overflow() -> None:
    with pytest.raises(ValueError):
        ChampionsStatPoints(hp=True)
    valid = ChampionsStatPoints(hp=32, attack=32, speed=2)
    assert valid.total == 66
    assert valid.remaining == 0
    with pytest.raises(ValueError):
        ChampionsStatPoints(hp=32, attack=32, speed=3)


def test_team_build_is_canonical_hashable_and_has_no_tera_field() -> None:
    build = _build(speed=2)
    payload = build.to_canonical_dict()
    assert set(payload) == {"schema_version", "game", "name", "battle_format", "members"}
    assert all("tera" not in json.dumps(member) for member in payload["members"])
    assert hashlib.sha256(build.canonical_json_bytes()).hexdigest() == build.sha256()
    assert ChampionsTeamBuild.from_json_bytes(build.canonical_json_bytes()) == build
    assert build.selected_members(("C", "A", "F"))[1].pokemon_name == "A"
    assert len(build.unspent_point_warnings()) == 6


def test_selection_snapshot_keeps_detailed_build_after_preset_update(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "runtime.db")
    application = BattleApplication(repository)
    try:
        application.new_match()
        facts = application.confirm_selection_facts(TEAM, OPPONENT, _build())
        snapshot = repository.get_selection_facts(facts.reviewed_selection_id)
        assert snapshot.self_team_build == _build()
        assert snapshot.self_team_build_sha256 == _build().sha256()
    finally:
        repository.close()
