from __future__ import annotations

from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)
from maple_next.ui.team_import import ImportedTeam, encode_team_export, parse_team_import

TEAM = ("A", "B", "C", "D", "E", "F")


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


def test_v2_team_import_export_round_trip_is_deterministic() -> None:
    build = _build()
    imported = parse_team_import(build.canonical_json())
    assert imported.team_build == build
    assert imported.status == "DETAILED"
    encoded = encode_team_export(imported)
    assert encoded.endswith(b"\n")
    assert parse_team_import(encoded.decode("utf-8")).team_build == build
    assert encode_team_export(imported) == encoded
    assert ImportedTeam(pokemon=TEAM, team_build=build).team_build == build
