from __future__ import annotations

from pathlib import Path

from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION_V2,
    CHAMPIONS_SCHEMA_VERSION_V3,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
    SelectionPackage,
    TeamSelectionProfile,
)
from maple_next.persistence.sqlite import SQLiteRepository


TEAM = ("A", "B", "C", "D", "E", "F")


def _members() -> tuple[ChampionsPokemonBuild, ...]:
    return tuple(
        ChampionsPokemonBuild(
            pokemon_name=name,
            moves=(f"move-{name}",),
            held_item=None,
            ability=f"ability-{name}",
            nature="Hardy",
            stat_points=ChampionsStatPoints(),
        )
        for name in TEAM
    )


def _v3_build() -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION_V3,
        game=CHAMPIONS_GAME,
        name="Tournament",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=_members(),
        selection_profile=TeamSelectionProfile(
            mode="fixed_packages",
            mixing_allowed=False,
            packages=(
                SelectionPackage(
                    package_id="P1",
                    name="one",
                    members=("A", "B", "C"),
                    intended_mega="A",
                    notes="",
                ),
                SelectionPackage(
                    package_id="P2",
                    name="two",
                    members=("D", "E", "F"),
                    intended_mega="E",
                    notes="",
                ),
            ),
        ),
    )


def _v2_build() -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION_V2,
        game=CHAMPIONS_GAME,
        name="Legacy",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=_members(),
    )


def _insert(repo: SQLiteRepository, preset_id: str, build: ChampionsTeamBuild) -> None:
    with repo.transaction():
        repo.insert_self_team_preset(
            preset_id=preset_id,
            name=preset_id,
            normalized_name=preset_id,
            self_team=TEAM,
            team_build=build,
        )


def test_v3_preset_round_trip_preserves_schema_and_profile(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "maple.db")
    build = _v3_build()
    _insert(repo, "v3", build)

    row = repo.connection.execute(
        "SELECT build_schema_version, team_build_json, team_build_sha256 "
        "FROM self_team_presets WHERE preset_id = 'v3'"
    ).fetchone()
    assert row is not None
    assert row["build_schema_version"] == CHAMPIONS_SCHEMA_VERSION_V3
    assert row["team_build_sha256"] == build.sha256()
    assert '"selection_profile"' in str(row["team_build_json"])

    loaded = repo.get_self_team_preset("v3")
    assert loaded is not None
    assert loaded.build_schema_version == CHAMPIONS_SCHEMA_VERSION_V3
    assert loaded.team_build == build
    assert loaded.team_build is not None
    assert loaded.team_build.selection_profile == build.selection_profile


def test_legacy_v2_metadata_mislabel_with_valid_v3_json_is_recovered(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "maple.db")
    build = _v3_build()
    _insert(repo, "mislabel", build)
    with repo.transaction():
        repo.connection.execute(
            "UPDATE self_team_presets SET build_schema_version = 'maple-team.v2' "
            "WHERE preset_id = 'mislabel'"
        )

    loaded = repo.get_self_team_preset("mislabel")
    assert loaded is not None
    assert loaded.build_schema_version == CHAMPIONS_SCHEMA_VERSION_V3
    assert loaded.team_build == build
    assert loaded.team_build is not None
    assert loaded.team_build.selection_profile is not None


def test_real_v2_preset_remains_v2(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "maple.db")
    build = _v2_build()
    _insert(repo, "v2", build)

    row = repo.connection.execute(
        "SELECT build_schema_version FROM self_team_presets WHERE preset_id = 'v2'"
    ).fetchone()
    assert row is not None
    assert row["build_schema_version"] == CHAMPIONS_SCHEMA_VERSION_V2

    loaded = repo.get_self_team_preset("v2")
    assert loaded is not None
    assert loaded.build_schema_version == CHAMPIONS_SCHEMA_VERSION_V2
    assert loaded.team_build == build
    assert loaded.team_build is not None
    assert loaded.team_build.selection_profile is None
