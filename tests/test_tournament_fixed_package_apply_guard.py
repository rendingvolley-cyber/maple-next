from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION_V3,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
    SelectionPackage,
    TeamSelectionProfile,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import ResultEnvelope

P1 = ("メタグロス", "サザンドラ", "アシレーヌ")
P2 = ("ペリッパー", "ラグラージ", "ブリジュラス")
SELF_TEAM = P1 + P2
OPP_TEAM = ("カビゴン", "ユキノオー", "ドドゲザン", "ガブリアス", "サーフゴー", "カイリュー")


def _member(name: str) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=("テストわざ",),
        held_item=None,
        ability="テストとくせい",
        nature="テストせいかく",
        stat_points=ChampionsStatPoints(),
    )


def _build() -> ChampionsTeamBuild:
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION_V3,
        game=CHAMPIONS_GAME,
        name="大会用固定2パッケージ",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(_member(name) for name in SELF_TEAM),
        selection_profile=TeamSelectionProfile(
            mode="fixed_packages",
            mixing_allowed=False,
            packages=(
                SelectionPackage(
                    package_id="P1",
                    name="グロス軸",
                    members=P1,
                    intended_mega="メタグロス",
                    notes="",
                ),
                SelectionPackage(
                    package_id="P2",
                    name="雨軸",
                    members=P2,
                    intended_mega="ラグラージ",
                    notes="",
                ),
            ),
        ),
    )


def _ready(tmp_path: Path) -> tuple[SQLiteRepository, BattleApplication]:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    facts = app.confirm_selection_facts(SELF_TEAM, OPP_TEAM, _build())
    job = app.request_selection_advice("fixed-package-test")
    result = ResultEnvelope(
        contract_version="maple-worker.v1",
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=facts.reviewed_selection_id,
        request_payload_hash=job.request_payload_hash,
        payload={
            "chosen_package": "P2",
            "selected_three": list(P2),
            "lead": "ペリッパー",
            "intended_mega": "ラグラージ",
            "selection_reason": "雨軸を選択。",
        },
    )
    disposition = app.apply_selection_advice_result(result)
    assert disposition.value == "APPLIED"
    return repo, app


def test_fixed_package_profile_rejects_mixed_human_apply_before_write(tmp_path: Path) -> None:
    repo, app = _ready(tmp_path)
    before = repo.load_active_session()
    assert before is not None
    mixed = ("ペリッパー", "ラグラージ", "メタグロス")

    with pytest.raises(DomainError, match="SELECTION_MIXES_FIXED_PACKAGES"):
        app.apply_selection(selected_three=mixed, lead="ペリッパー", human_confirmed=True)

    after = repo.load_active_session()
    assert after is not None
    assert after.current_applied_selection_id is None
    assert after.state == before.state
    count = repo.connection.execute("SELECT COUNT(*) FROM applied_selections").fetchone()
    assert count is not None and int(count[0]) == 0


def test_fixed_package_profile_allows_either_complete_package_as_human_override(
    tmp_path: Path,
) -> None:
    repo, app = _ready(tmp_path)
    snapshot = app.apply_selection(
        selected_three=P1,
        lead="メタグロス",
        human_confirmed=True,
    )
    assert snapshot.selected_three == P1
    assert snapshot.lead == "メタグロス"
    persisted = repo.get_applied_selection(snapshot.applied_selection_id)
    assert persisted.selected_three == P1


def test_names_only_legacy_apply_behavior_remains_free_selection(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "legacy.db")
    app = BattleApplication(repo)
    app.new_match()
    app.confirm_selection_facts(SELF_TEAM, OPP_TEAM)
    job = app.request_selection_advice("legacy-test")
    result = ResultEnvelope(
        contract_version="maple-worker.v1",
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload={"selected_three": list(P2), "lead": "ペリッパー"},
    )
    disposition = app.apply_selection_advice_result(result)
    assert disposition.value == "APPLIED"
    mixed = ("ペリッパー", "ラグラージ", "メタグロス")
    snapshot = app.apply_selection(selected_three=mixed, lead="ペリッパー", human_confirmed=True)
    assert snapshot.selected_three == mixed
