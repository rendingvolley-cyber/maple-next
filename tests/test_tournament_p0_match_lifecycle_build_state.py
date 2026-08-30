"""Tournament P0: lifecycle CTA and build-preparation state stay consistent."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog
from test_issue31_field_entrypoints import (
    OPPONENT_TEAM,
    SELF_TEAM,
    _advance_to_battle_ready,
    _build_window,
    _show_tab,
)
from test_issue31_turn_state_ui_bundle_c import _fill_minimal_current_state

from maple_next.domain.enums import BattleState, MatchOutcome
from maple_next.selection_roi.input_policy import SelectionInputOrigin

IMPORTED_TEAM = (
    "Incineroar",
    "Rillaboom",
    "Amoonguss",
    "Kyogre",
    "Chi-Yu",
    "Landorus",
)


def test_active_turn_five_never_looks_like_idle_build_preparation(
    tmp_path: Path,
) -> None:
    repository, _application, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        _advance_to_battle_ready(controller, window)
        session = repository.load_active_session()
        assert session is not None
        turn_id = "tournament-p0-turn-5"
        with repository.transaction():
            repository.connection.execute(
                """
                INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at)
                VALUES (?, ?, 5, ?)
                """,
                (turn_id, session.session_id, repository._now()),  # noqa: SLF001
            )
            repository.save_session(
                replace(
                    session,
                    state=BattleState.TURN_RECORDED,
                    current_turn_id=turn_id,
                )
            )

        window.render_view()
        current = controller.refresh()
        assert current.projection.session_state == "TURN_RECORDED"
        assert current.projection.turn_number == 5

        _show_tab(window, 1)
        assert not window.new_match_button.isVisible()
        assert not window.new_match_after_export_button.isVisible()
        assert window.match_end_local_group.isVisible()

        _show_tab(window, 0)
        assert not window.import_self_team_button.isEnabled()
        assert not window.self_team_preset_box.isEnabled()
        assert window.selection_v3_build_lock_notice.isVisible()
        assert window.selection_v3_discard_match_button.isVisible()
        assert window.selection_v3_discard_match_button.isEnabled()
    finally:
        window.close()
        repository.close()


def test_turn_reviewed_can_record_match_win_without_recording_another_turn(
    tmp_path: Path,
) -> None:
    """A human may finish a won match immediately after Turn review."""

    repository, _application, controller, window = _build_window(tmp_path)
    try:
        controller.new_match()
        _advance_to_battle_ready(controller, window)
        controller.start_turn_capture()
        window.render_view()
        _fill_minimal_current_state(window)
        window._on_confirm_turn_facts()  # noqa: SLF001 - explicit human fixture
        window.render_view()

        assert controller.refresh().session_state == "TURN_REVIEWED"
        _show_tab(window, 1)
        assert window.match_end_local_group.isVisible()
        assert window.match_win_button.isEnabled()

        window.match_win_button.click()
        assert window.outcome_box.currentText() == MatchOutcome.WIN.value
        window.outcome_confirm_checkbox.click()
        assert window.end_match_button.isEnabled()
        window.end_match_button.click()

        current = controller.refresh()
        assert current.session_state == "MATCH_ENDED"
        assert current.outcome == MatchOutcome.WIN.value
    finally:
        window.close()
        repository.close()


def test_export_unlocks_import_and_next_match_binds_imported_team_without_restart(
    tmp_path: Path,
) -> None:
    repository, application, controller, window = _build_window(tmp_path)
    imported_path = tmp_path / "next-team.json"
    imported_path.write_text(
        json.dumps(
            {
                "schema_version": "maple-team.v1",
                "name": "Tournament Next Six",
                "pokemon": list(IMPORTED_TEAM),
            }
        ),
        encoding="utf-8",
    )
    try:
        controller.new_match()
        _advance_to_battle_ready(controller, window)
        window.render_view()
        assert not window.import_self_team_button.isEnabled()

        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        application.export_match()
        window.render_view()
        QApplication.processEvents()

        current = controller.refresh()
        assert current.projection.session_state == "MATCH_EXPORTED"
        assert current.projection.primary_cta == "NEW_MATCH"
        assert window.new_match_after_export_button.isVisible()
        assert window.new_match_after_export_button.isEnabled()
        assert window.import_self_team_button.isEnabled()
        assert window.self_team_preset_box.isEnabled()
        assert not window.selection_v3_build_lock_notice.isVisible()
        assert not window.selection_v3_discard_match_button.isVisible()

        with patch.object(
            QFileDialog,
            "getOpenFileName",
            return_value=(str(imported_path), "Maple JSON (*.json)"),
        ):
            window._on_import_self_team()  # noqa: SLF001
        assert tuple(field.text() for field in window.self_team_inputs) == IMPORTED_TEAM

        window._on_save_self_team_preset()  # noqa: SLF001
        preset_index = window.self_team_preset_box.findText("Tournament Next Six")
        assert preset_index >= 0
        for field, old_name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
            field.setText(old_name)
        window.self_team_preset_box.setCurrentIndex(preset_index)
        QApplication.processEvents()
        assert window.use_self_team_preset_button.isEnabled()
        window.use_self_team_preset_button.click()
        assert tuple(field.text() for field in window.self_team_inputs) == IMPORTED_TEAM

        _show_tab(window, 1)
        window.new_match_after_export_button.click()
        QApplication.processEvents()
        assert controller.refresh().projection.session_state == "SELECTION_OPEN"

        for slot, name in enumerate(OPPONENT_TEAM, start=1):
            window._set_selection_slot_value(  # noqa: SLF001
                slot,
                name,
                origin=SelectionInputOrigin.MANUAL_TEXT,
                user_locked=True,
            )
        window._on_confirm_facts()  # noqa: SLF001

        active = repository.load_active_session()
        assert active is not None
        assert active.current_reviewed_selection_id is not None
        facts = repository.get_selection_facts(active.current_reviewed_selection_id)
        assert facts.self_team == IMPORTED_TEAM
    finally:
        window.close()
        repository.close()
