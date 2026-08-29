"""Tournament P0: v3 fixed Selection packages reach Gemini and the real UI."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import BattleApplication
from maple_next.domain.enums import JobType, ResultDisposition
from maple_next.domain.team_build import ChampionsTeamBuild
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.selection_request import (
    CONTRACT_VERSION_V2,
    CONTRACT_VERSION_V3,
    REQUESTED_OUTPUT_SCHEMA_V3,
    build_provider_prompt,
    request_payload_hash,
)
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.team_build_editor import ChampionsTeamBuildEditor
from maple_next.ui.team_import import TeamImportError, parse_team_import, read_team_import
from maple_next.ui.turn_state_flow import TurnStateFlowController
from maple_next.workers.contracts.models import ResultEnvelope

ROOT = Path(__file__).resolve().parents[1]
TOURNAMENT_BUILD = (
    ROOT / "data" / "teams" / "m-b-tournament-p1-metagross-p2-rain-v1.json"
)
OPPONENT_TEAM = ("カイリュー", "サーフゴー", "ガブリアス", "ゲンガー", "マリルリ", "バンギラス")
P1 = ("メタグロス", "サザンドラ", "アシレーヌ")
P2 = ("ペリッパー", "ラグラージ", "ブリジュラス")
MEMBER_MATRIX_SHA256 = "1ae02676e4fd58813d7710463e7e0ceebfc7c80f9e9a1e472c6501aea093a738"


class SyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)
        except ProviderTransportError as exc:
            self.on_failed(str(exc))
        else:
            self.on_succeeded(result)


def _build() -> ChampionsTeamBuild:
    imported = read_team_import(TOURNAMENT_BUILD)
    assert imported.team_build is not None
    return imported.team_build


def _result(job, payload: dict[str, object]) -> ResultEnvelope:
    return ResultEnvelope(
        contract_version=job.contract_version,
        result_id=f"result-{job.job_id}",
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
        payload=payload,
        source_type=GEMINI_SOURCE_TYPE,
        model="fake-selection-profile-v3",
    )


def _apply_payload(
    tmp_path: Path,
    build: ChampionsTeamBuild,
    payload: dict[str, object],
) -> tuple[ResultDisposition, SQLiteRepository]:
    repository = SQLiteRepository(tmp_path / "profile-validation.db")
    application = BattleApplication(repository)
    application.new_match()
    application.confirm_selection_facts(build.pokemon_names, OPPONENT_TEAM, build)
    job = application.request_selection_advice("profile-validation")
    return application.apply_selection_advice_result(_result(job, payload)), repository


def _profile_payload(
    package_id: str,
    selected_three: tuple[str, str, str],
    lead: str,
    intended_mega: str,
) -> dict[str, object]:
    return {
        "chosen_package": package_id,
        "selected_three": list(selected_three),
        "lead": lead,
        "intended_mega": intended_mega,
        "selection_reason": "相手6体に対して、この固定プランが最も一貫するため。",
    }


def test_v3_import_roundtrip_hash_and_request_are_profile_bound() -> None:
    build = _build()
    assert build.schema_version == "maple-team.v3"
    assert build.selection_profile is not None
    assert ChampionsTeamBuild.from_json_bytes(build.canonical_json_bytes()) == build

    canonical_members = json.dumps(
        build.to_canonical_dict()["members"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical_members).hexdigest() == MEMBER_MATRIX_SHA256

    first_package = build.selection_profile.packages[0]
    changed_package = replace(first_package, notes=f"{first_package.notes} hash-change")
    changed_profile = replace(
        build.selection_profile,
        packages=(changed_package, *build.selection_profile.packages[1:]),
    )
    assert replace(build, selection_profile=changed_profile).sha256() != build.sha256()


def test_v3_detail_editor_preserves_profile() -> None:
    qapp = cast(QApplication, QApplication.instance() or QApplication([]))
    assert qapp is not None
    build = _build()
    editor = ChampionsTeamBuildEditor(initial_build=build)
    try:
        edited = editor.build_from_inputs()
        assert edited.schema_version == "maple-team.v3"
        assert edited.selection_profile == build.selection_profile
        assert edited.members == build.members
    finally:
        editor.close()


@pytest.mark.parametrize(
    "mutation",
    ("duplicate-id", "outside-member", "outside-mega", "blank-name"),
)
def test_malformed_v3_selection_profile_fails_closed(mutation: str) -> None:
    payload = json.loads(TOURNAMENT_BUILD.read_text(encoding="utf-8"))
    packages = payload["selection_profile"]["packages"]
    if mutation == "duplicate-id":
        packages[1]["id"] = packages[0]["id"]
    elif mutation == "outside-member":
        packages[0]["members"][2] = "チーム外"
    elif mutation == "outside-mega":
        packages[0]["intended_mega"] = "ペリッパー"
    else:
        packages[0]["name"] = " "
    with pytest.raises(TeamImportError):
        parse_team_import(json.dumps(payload, ensure_ascii=False))


def test_v3_profile_survives_preset_and_new_match_binding(tmp_path: Path) -> None:
    build = _build()
    repository = SQLiteRepository(tmp_path / "profile-roundtrip.db")
    application = BattleApplication(repository)
    try:
        with repository.transaction():
            repository.insert_self_team_preset(
                preset_id="tournament-v3",
                name=build.name,
                normalized_name=build.name,
                self_team=build.pokemon_names,
                team_build=build,
            )
            repository.set_last_used_self_team_preset("tournament-v3")
        saved = repository.get_last_used_self_team_preset()
        assert saved is not None
        assert saved.team_build == build
        assert saved.team_build is not None
        assert saved.team_build.selection_profile == build.selection_profile

        application.new_match()
        application.confirm_selection_facts(build.pokemon_names, OPPONENT_TEAM, saved.team_build)
        session = repository.load_active_session()
        assert session is not None
        assert session.current_reviewed_selection_id is not None
        facts = repository.get_selection_facts(session.current_reviewed_selection_id)
        assert facts.self_team_build == build
        assert facts.self_team_build_sha256 == build.sha256()

        job = application.request_selection_advice("profile-request")
        request = application.build_selection_advice_transport_request(job)
        assert request.contract_version == CONTRACT_VERSION_V3
        assert request.selection_profile == build.selection_profile
        assert request.self_team_build == build
        assert request.self_team_build_sha256 == build.sha256()
        assert request.requested_output_schema == REQUESTED_OUTPUT_SCHEMA_V3
        assert request_payload_hash(request) == job.request_payload_hash
        prompt = build_provider_prompt(request)
        assert "human-authored Selection Profile is authoritative" in prompt
        assert "Do not mix Pokémon between packages" in prompt
        assert "moves, held items, abilities, natures" in prompt
        assert '"selection_profile"' in prompt
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_profile_payload("P1", P1, P1[1], "メタグロス"), ResultDisposition.APPLIED),
        (_profile_payload("P2", P2, P2[0], "ラグラージ"), ResultDisposition.APPLIED),
        (
            _profile_payload(
                "P2", ("ペリッパー", "ラグラージ", "メタグロス"), "ペリッパー", "ラグラージ"
            ),
            ResultDisposition.INVALID_REJECTED,
        ),
        (_profile_payload("P1", P2, P2[0], "メタグロス"), ResultDisposition.INVALID_REJECTED),
        (_profile_payload("P1", P1, P2[0], "メタグロス"), ResultDisposition.INVALID_REJECTED),
        (_profile_payload("P1", P1, P1[0], "ラグラージ"), ResultDisposition.INVALID_REJECTED),
    ],
    ids=("p1", "p2", "mixed", "wrong-package-members", "outside-lead", "wrong-mega"),
)
def test_fixed_package_response_validation(
    tmp_path: Path,
    payload: dict[str, object],
    expected: ResultDisposition,
) -> None:
    disposition, repository = _apply_payload(tmp_path, _build(), payload)
    try:
        assert disposition is expected
        session = repository.load_active_session()
        assert session is not None
        assert (session.current_selection_advice_id is not None) is (
            expected is ResultDisposition.APPLIED
        )
    finally:
        repository.close()


def test_v2_detailed_build_retains_free_selection(tmp_path: Path) -> None:
    v3_payload = json.loads(TOURNAMENT_BUILD.read_text(encoding="utf-8"))
    v3_payload["schema_version"] = "maple-team.v2"
    del v3_payload["selection_profile"]
    imported = parse_team_import(json.dumps(v3_payload, ensure_ascii=False))
    assert imported.team_build is not None
    assert imported.team_build.selection_profile is None

    free_three = (P1[0], P2[0], P2[2])
    disposition, repository = _apply_payload(
        tmp_path,
        imported.team_build,
        {"selected_three": list(free_three), "lead": free_three[0]},
    )
    try:
        assert disposition is ResultDisposition.APPLIED
        session = repository.load_active_session()
        assert session is not None
        assert session.current_selection_advice_id is not None
        job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
        assert job is not None
        request = BattleApplication(repository).build_selection_advice_transport_request(job)
        assert request.contract_version == CONTRACT_VERSION_V2
    finally:
        repository.close()


def test_real_battle_record_ui_shows_package_mega_and_reason(tmp_path: Path) -> None:
    qapp = cast(QApplication, QApplication.instance() or QApplication([]))
    build = _build()
    response = _profile_payload("P1", P1, P1[1], "メタグロス")
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload=response,
                source_type=GEMINI_SOURCE_TYPE,
                model="fake-selection-profile-v3",
            )
        ]
    )
    repository = SQLiteRepository(tmp_path / "profile-ui.db")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="fake", model="fake-selection-profile-v3"),
        dispatch_factory=SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        adapter,
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir)
    try:
        controller.new_match()
        controller.confirm_selection_facts(build.pokemon_names, OPPONENT_TEAM, build)
        window.render_view()
        controller.send_selection_advice_to_gemini(on_result=window.render_view)
        qapp.processEvents()

        assert transport.call_count == 1
        assert window.selection_v3_advice_package.text() == "P1 グロス軸"
        assert tuple(label.text() for label in window.selection_v3_advice_pick_labels) == P1
        assert window.selection_v3_advice_lead.text() == P1[1]
        assert window.selection_v3_advice_intended_mega.text() == "メタグロス"
        assert window.selection_v3_advice_reason.text() == response["selection_reason"]
    finally:
        window.close()
        repository.close()


def test_profile_invalid_ui_result_is_not_rendered(tmp_path: Path) -> None:
    build = _build()
    mixed = _profile_payload(
        "P2", ("ペリッパー", "ラグラージ", "メタグロス"), "ペリッパー", "ラグラージ"
    )
    transport = FakeSelectionAdviceTransport(
        responses=[SanitizedProviderResult(mixed, GEMINI_SOURCE_TYPE, "fake-profile-invalid")]
    )
    repository = SQLiteRepository(tmp_path / "profile-invalid-ui.db")
    application = BattleApplication(repository)
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="fake", model="fake-profile-invalid"),
        dispatch_factory=SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        adapter,
    )
    try:
        controller.new_match()
        controller.confirm_selection_facts(build.pokemon_names, OPPONENT_TEAM, build)
        controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
        view = controller.refresh()
        assert view.advice is None
        assert view.error_message == "Gemini推薦が現在の構築選出ルールと一致しませんでした。"
        assert controller.selection_advice_status().legality_status == "INVALID"
    finally:
        repository.close()
