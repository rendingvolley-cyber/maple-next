from __future__ import annotations

import os
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.domain.team_build import CHAMPIONS_STAT_NAMES
from maple_next.ui.team_build_editor import ChampionsTeamBuildEditor


def _app() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def test_editor_has_six_members_and_stages_only_after_human_confirm() -> None:
    _app()
    names = ("A", "B", "C", "D", "E", "F")
    editor = ChampionsTeamBuildEditor(pokemon_names=names)
    try:
        assert len(editor.member_panels) == 6
        for panel, name in zip(editor.member_panels, names, strict=True):
            panel.ability_input.setText("Confirmed ability")
            panel.nature_input.setText("Serious")
            panel.move_inputs[0].setText(f"{name} move")
            assert set(panel.stat_spinboxes) == set(CHAMPIONS_STAT_NAMES)
        assert editor.confirm() is not None
        assert editor.staged_build is not None
        assert editor.staged_build.pokemon_names == names
    finally:
        editor.close()


def test_editor_rejects_persistence_fallback_direct_confirm() -> None:
    _app()
    editor = ChampionsTeamBuildEditor(
        pokemon_names=("A", "B", "C", "D", "E", "F"),
        persistence_reads_allowed=False,
    )
    try:
        assert editor.confirm() is None
        assert editor.staged_build is None
    finally:
        editor.close()


def test_editor_disables_save_when_one_member_exceeds_total_points() -> None:
    _app()
    editor = ChampionsTeamBuildEditor(
        pokemon_names=("A", "B", "C", "D", "E", "F"),
    )
    try:
        panel = editor.member_panels[0]
        panel.stat_spinboxes["hp"].setValue(32)
        panel.stat_spinboxes["attack"].setValue(32)
        panel.stat_spinboxes["speed"].setValue(3)
        assert not editor.save_button.isEnabled()
        assert "Too many" in panel.warning_label.text()
    finally:
        editor.close()
