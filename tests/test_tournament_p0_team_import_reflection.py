"""Tournament P0: an imported team is the immediate pre-match UI draft."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog
from test_issue31_field_entrypoints import (
    OPPONENT_TEAM,
    SELF_TEAM,
    _advance_to_battle_ready,
    _build_window,
)

from maple_next.domain.enums import MatchOutcome
from maple_next.ui.team_import import read_team_import

IMPORTED_TEAM = ("G", "H", "I", "J", "K", "L")
ACTUAL_TOURNAMENT_SIX = (
    "メタグロス",
    "サザンドラ",
    "アシレーヌ",
    "ペリッパー",
    "ラグラージ",
    "ブリジュラス",
)
TOURNAMENT_BUILD_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "teams"
    / "m-b-tournament-p1-metagross-p2-rain-v1.json"
)


def _detailed_team_payload() -> dict[str, object]:
    return {
        "schema_version": "maple-team.v2",
        "game": "pokemon-champions",
        "name": "Imported Tournament Six",
        "battle_format": "SINGLE_3",
        "members": [
            {
                "pokemon": name,
                "moves": [f"Move {index}", f"Coverage {index}"],
                "held_item": f"Item {index}",
                "ability": f"Ability {index}",
                "nature": "Hardy",
                "stat_points": {
                    "hp": 1,
                    "attack": 2,
                    "defense": 3,
                    "special_attack": 4,
                    "special_defense": 5,
                    "speed": 6,
                },
            }
            for index, name in enumerate(IMPORTED_TEAM, start=1)
        ],
    }


def _actual_tournament_team_payload() -> dict[str, object]:
    payload = json.loads(TOURNAMENT_BUILD_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_final_ui_gate_reflects_actual_tournament_six_immediately(
    tmp_path: Path,
) -> None:
    repository, application, controller, window = _build_window(tmp_path)
    source = tmp_path / "actual-tournament-team.json"
    source.write_text(
        json.dumps(_actual_tournament_team_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
            field.setText(name)
        window.self_team_preset_name.setText("Old selected preset")
        window._on_save_self_team_preset()  # noqa: SLF001
        old_index = window.self_team_preset_box.findText("Old selected preset")
        window.self_team_preset_box.setCurrentIndex(old_index)
        window._on_use_self_team_preset()  # noqa: SLF001

        controller.new_match()
        _advance_to_battle_ready(controller, window)
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        application.export_match()
        window.render_view()
        QApplication.processEvents()

        assert controller.refresh().projection.session_state == "MATCH_EXPORTED"
        visible_before = tuple(
            label.text() for label in window.selection_v3_team_name_labels
        )
        assert visible_before == SELF_TEAM
        assert window.selection_v3_build_name.text() == "Old selected preset"

        with patch.object(
            QFileDialog,
            "getOpenFileName",
            return_value=(str(source), "Maple JSON (*.json)"),
        ):
            window._on_import_self_team()  # noqa: SLF001
        QApplication.processEvents()

        visible_after = tuple(
            label.text() for label in window.selection_v3_team_name_labels
        )
        draft_after = tuple(field.text() for field in window.self_team_inputs)
        assert visible_after == ACTUAL_TOURNAMENT_SIX
        assert draft_after == ACTUAL_TOURNAMENT_SIX
        assert not set(SELF_TEAM).intersection(visible_after)
        assert window.selection_v3_build_name.text() == (
            "M-B大会用 P1グロス / P2雨 仮組みv1"
        )
        assert window._selected_self_team_preset_id() is None  # noqa: SLF001
    finally:
        window.close()
        repository.close()


def test_match_exported_import_replaces_old_visible_draft_and_preserves_details(
    tmp_path: Path,
) -> None:
    repository, application, controller, window = _build_window(tmp_path)
    imported_path = tmp_path / "real-team.json"
    imported_path.write_text(
        json.dumps(_detailed_team_payload()), encoding="utf-8"
    )
    try:
        for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
            field.setText(name)
        window.self_team_preset_name.setText("Old selected preset")
        window._on_save_self_team_preset()  # noqa: SLF001
        old_index = window.self_team_preset_box.findText("Old selected preset")
        window.self_team_preset_box.setCurrentIndex(old_index)
        window._on_use_self_team_preset()  # noqa: SLF001

        controller.new_match()
        _advance_to_battle_ready(controller, window)
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        application.export_match()
        window.render_view()
        QApplication.processEvents()

        assert controller.refresh().projection.session_state == "MATCH_EXPORTED"
        assert tuple(label.text() for label in window.selection_v3_team_name_labels) == SELF_TEAM

        with patch.object(
            QFileDialog,
            "getOpenFileName",
            return_value=(str(imported_path), "Maple JSON (*.json)"),
        ):
            window._on_import_self_team()  # noqa: SLF001
        QApplication.processEvents()

        assert tuple(field.text() for field in window.self_team_inputs) == IMPORTED_TEAM
        assert tuple(label.text() for label in window.selection_v3_team_name_labels) == (
            IMPORTED_TEAM
        )
        assert window.selection_v3_build_name.text() == "Imported Tournament Six"
        assert window._selected_self_team_preset_id() is None  # noqa: SLF001
        assert window._staged_self_team_build is not None  # noqa: SLF001
        first_member = window._staged_self_team_build.member_by_name("G")  # noqa: SLF001
        assert first_member.moves == ("Move 1", "Coverage 1")
        assert first_member.held_item == "Item 1"
        assert first_member.ability == "Ability 1"

        # A normal render must not restore the completed match's A-F snapshot.
        window.render_view()
        QApplication.processEvents()
        assert tuple(field.text() for field in window.self_team_inputs) == IMPORTED_TEAM
        assert tuple(label.text() for label in window.selection_v3_team_name_labels) == (
            IMPORTED_TEAM
        )
        assert window._staged_self_team_build is not None  # noqa: SLF001
        assert window._staged_self_team_build.pokemon_names == IMPORTED_TEAM  # noqa: SLF001

        window._on_save_self_team_preset()  # noqa: SLF001
        saved = next(
            preset
            for preset in controller.list_self_team_presets()
            if preset.name == "Imported Tournament Six"
        )
        assert saved.self_team == IMPORTED_TEAM
        assert saved.team_build is not None
        assert saved.team_build.member_by_name("G") == first_member

        saved_index = window.self_team_preset_box.findText("Imported Tournament Six")
        window.self_team_preset_box.setCurrentIndex(saved_index)
        window._on_use_self_team_preset()  # noqa: SLF001
        window.new_match_after_export_button.click()
        QApplication.processEvents()
        assert controller.refresh().projection.session_state == "SELECTION_OPEN"
        for field, name in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
            field.setText(name)
        window._on_confirm_facts()  # noqa: SLF001
        confirmed = controller.refresh()
        assert confirmed.self_team == IMPORTED_TEAM
        assert confirmed.self_team_build is not None
        assert confirmed.self_team_build.member_by_name("G") == first_member
    finally:
        window.close()
        repository.close()


def test_actual_tournament_matrix_survives_parser_draft_save_adopt_and_binding(
    tmp_path: Path,
) -> None:
    repository, _application, controller, window = _build_window(tmp_path)
    source = tmp_path / "actual-tournament-team.json"
    payload = _actual_tournament_team_payload()
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    imported = read_team_import(source)
    assert imported.team_build is not None
    expected_build = imported.team_build
    assert expected_build.to_canonical_dict() == payload

    try:
        with patch.object(
            QFileDialog,
            "getOpenFileName",
            return_value=(str(source), "Maple JSON (*.json)"),
        ):
            window._on_import_self_team()  # noqa: SLF001

        assert window._staged_self_team_build == expected_build  # noqa: SLF001
        window._on_save_self_team_preset()  # noqa: SLF001
        saved = next(
            preset
            for preset in controller.list_self_team_presets()
            if preset.name == expected_build.name
        )
        assert saved.team_build == expected_build

        selected_index = window.self_team_preset_box.findText(expected_build.name)
        window.self_team_preset_box.setCurrentIndex(selected_index)
        window._on_use_self_team_preset()  # noqa: SLF001
        selected = controller.last_used_self_team_preset()
        assert selected is not None
        assert selected.team_build == expected_build

        window._on_new_match()  # noqa: SLF001
        for field, name in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
            field.setText(name)
        window._on_confirm_facts()  # noqa: SLF001
        bound = controller.refresh().self_team_build
        assert bound == expected_build
        assert controller.network_call_count == 0
    finally:
        window.close()
        repository.close()
