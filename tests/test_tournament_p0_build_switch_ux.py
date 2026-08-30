"""Tournament P0 -- TEAM BUILD SWITCH UX.

Live issue: on the Selection screen the operator opens 構築管理 but every
saved-build action is disabled with no explanation and no path to change the
build while a Selection/OCR/Gemini session already exists.

Root cause: ``MapleMainWindow.render_view`` sets
``self_team_editable = no_active_match or (primary_cta == "CONFIRM_SELECTION_FACTS")``.
Once selection facts are confirmed the match-bound self-team snapshot is
protected by disabling every preset control -- but that lock was a silent
dead-end.

Fix (UI-only, no Gemini/OCR/feedback/faint/schema changes):

* CASE B (active session, build locked): show 「対戦中のため構築を変更できません」
  and one explicit action 「この試合を破棄して構築を変更」 that runs the existing
  human-confirmed ``abort_match`` lifecycle, preserving saved builds and
  returning to the pre-match NO_ACTIVE_MATCH state.
* CASE A (no active session): the saved-build selector is usable, selecting a
  build enables 「この構築を採用」, and applying it replaces the self-team six via
  the existing preset path.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from test_issue31_selection_v3_accepted_ux import (
    OPPONENT_TEAM,
    _build_window,
    _ready_fake_gemini,
)

from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.turn_state_flow import TurnStateFlowController

NEW_BUILD = (
    "Incineroar",
    "Rillaboom",
    "Amoonguss",
    "Kyogre",
    "Chi-Yu",
    "Landorus",
)


def _save_named_build(
    window: BattleRecordUiWindow, name: str, team: tuple[str, ...]
) -> str:
    for field, value in zip(window.self_team_inputs, team, strict=True):
        field.setText(value)
    window.self_team_preset_name.setText(name)
    window._on_save_self_team_preset()  # noqa: SLF001
    for index in range(window.self_team_preset_box.count()):
        if window.self_team_preset_box.itemText(index) == name:
            return str(window.self_team_preset_box.itemData(index))
    raise AssertionError(f"saved build {name!r} not found in selector")


def test_case_b_active_session_shows_lock_reason_and_one_explicit_exit(
    tmp_path: Path,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        window.header_tabs.setCurrentIndex(0)
        window.show()
        QApplication.processEvents()

        _ready_fake_gemini(controller, window)
        window.render_view()
        assert controller.refresh().projection.session_state == "SELECTION_ADVICE_READY"

        # The build-management controls are locked ...
        assert not window.self_team_preset_box.isEnabled()
        assert not window.use_self_team_preset_button.isEnabled()
        assert not window.save_self_team_preset_button.isEnabled()
        # ... and the reason + the one explicit exit are now visible.
        assert window.selection_v3_build_lock_notice.isVisible()
        assert window.selection_v3_build_lock_notice.text() == "対戦中のため構築を変更できません"
        assert window.selection_v3_discard_match_button.isVisible()
        assert window.selection_v3_discard_match_button.isEnabled()
        assert (
            window.selection_v3_discard_match_button.text()
            == "この試合を破棄して構築を変更"
        )
        assert window.use_self_team_preset_button.text() == "この構築を採用"
    finally:
        window.close()
        repository.close()


def test_case_b_discard_aborts_session_preserves_builds_and_unlocks_selector(
    tmp_path: Path,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        window.header_tabs.setCurrentIndex(0)
        window.show()
        QApplication.processEvents()

        saved_id = _save_named_build(window, "Bench Build", NEW_BUILD)
        for field in window.self_team_inputs:
            field.clear()

        _ready_fake_gemini(controller, window)
        window.render_view()
        aborted_session_id = repository.load_active_session()
        assert aborted_session_id is not None

        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
        ):
            window.selection_v3_discard_match_button.click()
        QApplication.processEvents()

        current = controller.refresh()
        # 1. session discarded via the existing lifecycle ...
        assert current.projection.session_state is None
        assert current.projection.primary_cta == "CREATE_NEW_MATCH"
        # 2. the abandoned session is preserved, not deleted
        stored = repository.connection.execute(
            "SELECT state FROM battle_sessions WHERE session_id = ?",
            (aborted_session_id.session_id,),
        ).fetchone()
        assert stored is not None and stored[0] == "ABORTED"
        # 2. saved builds preserved
        names = {
            window.self_team_preset_box.itemText(i)
            for i in range(window.self_team_preset_box.count())
        }
        assert "Bench Build" in names
        # 3-4. pre-match build-selection state; selector usable again
        assert not window.selection_v3_build_lock_notice.isVisible()
        assert not window.selection_v3_discard_match_button.isVisible()
        assert window.self_team_preset_box.isEnabled()
        assert window.save_self_team_preset_button.isEnabled()

        # 5. selecting a saved build enables この構築を採用
        window.self_team_preset_box.setCurrentIndex(
            window.self_team_preset_box.findData(saved_id)
        )
        QApplication.processEvents()
        assert window.use_self_team_preset_button.isEnabled()

        # ... applying it replaces the self-team six via the existing path
        window.use_self_team_preset_button.click()
        QApplication.processEvents()
        assert [f.text() for f in window.self_team_inputs] == list(NEW_BUILD)
        assert [
            label.text() for label in window.selection_v3_team_name_labels
        ] == list(NEW_BUILD)

        # 6. the next NEW MATCH binds the newly adopted six
        controller.new_match()
        window.render_view()
        for slot, name in enumerate(OPPONENT_TEAM, start=1):
            window._set_selection_slot_value(  # noqa: SLF001
                slot, name, origin=SelectionInputOrigin.MANUAL_TEXT, user_locked=True
            )
        window._on_confirm_facts()  # noqa: SLF001
        QApplication.processEvents()

        bound = repository.load_active_session()
        assert bound is not None and bound.current_reviewed_selection_id is not None
        facts = repository.get_selection_facts(bound.current_reviewed_selection_id)
        assert facts is not None
        assert list(facts.self_team) == list(NEW_BUILD)
    finally:
        window.close()
        repository.close()


def test_case_a_no_active_session_selector_and_adopt_work(tmp_path: Path) -> None:
    repository, controller, window = _build_window(tmp_path)
    try:
        window.header_tabs.setCurrentIndex(0)
        window.show()
        QApplication.processEvents()
        assert controller.refresh().projection.session_state is None

        saved_id = _save_named_build(window, "Prematch Build", NEW_BUILD)
        for field in window.self_team_inputs:
            field.setText("")
        window.render_view()

        # No active session -> selector usable, no lock messaging.
        assert window.self_team_preset_box.isEnabled()
        assert not window.selection_v3_build_lock_notice.isVisible()
        assert not window.selection_v3_discard_match_button.isVisible()
        assert not window.use_self_team_preset_button.isEnabled()

        window.self_team_preset_box.setCurrentIndex(
            window.self_team_preset_box.findData(saved_id)
        )
        QApplication.processEvents()
        assert window.use_self_team_preset_button.isEnabled()

        window.use_self_team_preset_button.click()
        QApplication.processEvents()
        assert [f.text() for f in window.self_team_inputs] == list(NEW_BUILD)
    finally:
        window.close()
        repository.close()


def test_no_provider_send_during_build_switch(tmp_path: Path) -> None:
    repository, controller, window = _build_window(tmp_path)
    assert isinstance(controller, TurnStateFlowController)
    try:
        window.header_tabs.setCurrentIndex(0)
        window.show()
        QApplication.processEvents()
        saved_id = _save_named_build(window, "Quiet Build", NEW_BUILD)
        for field in window.self_team_inputs:
            field.clear()

        _ready_fake_gemini(controller, window)
        window.render_view()

        with (
            patch.object(
                QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
            ),
            patch.object(
                controller, "send_selection_advice_to_gemini"
            ) as send_advice_spy,
            patch.object(
                window, "_on_send_current_selection_to_gemini"
            ) as send_selection_spy,
        ):
            window.selection_v3_discard_match_button.click()
            QApplication.processEvents()
            window.self_team_preset_box.setCurrentIndex(
                window.self_team_preset_box.findData(saved_id)
            )
            QApplication.processEvents()
            window.use_self_team_preset_button.click()
            QApplication.processEvents()

        assert send_advice_spy.call_count == 0
        assert send_selection_spy.call_count == 0
    finally:
        window.close()
        repository.close()
