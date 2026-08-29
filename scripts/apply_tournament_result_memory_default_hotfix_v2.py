"""Apply the tournament Result Entry memory-default hotfix deterministically.

V2 intentionally patches executable behavior only.  It does not depend on
comments/docstrings, because the production checkout may contain harmless text
drift while the accepted code shape remains the same.

The patch is fail-closed and writes only the two authorized files after every
required code/test transformation has been validated in memory.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "maple_next" / "ui" / "battle_record_ui.py"
TEST = ROOT / "tests" / "test_tournament_p0_result_entry_redesign.py"


def _section(text: str, start: str, end: str, *, label: str) -> tuple[str, str, str]:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: section start not found")
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        raise RuntimeError(f"{label}: section end not found")
    return text[:start_index], text[start_index:end_index], text[end_index:]


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def _patch_delta_int(section: str) -> str:
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaInt initial mode",
    )
    section = _replace_once(
        section,
        'self.unknown_box.toggled.connect(self._set_unknown)\n        layout.addWidget(self.unknown_box)',
        'self.unknown_box.toggled.connect(self._set_unknown)\n        self.unknown_box.setChecked(True)\n        layout.addWidget(self.unknown_box)',
        label="DeltaInt initial unknown",
    )
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaInt unknown toggle",
    )
    section = _replace_once(
        section,
        '        self.unknown_box.setChecked(False)\n        self.spin.setValue(0)\n        self.mode_box.setCurrentText("UNCHANGED")',
        '        self.spin.setValue(0)\n        self.mode_box.setCurrentText("UNKNOWN")\n        self.unknown_box.setChecked(True)',
        label="DeltaInt reset",
    )
    return section


def _patch_delta_text(section: str) -> str:
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaText initial mode",
    )
    section = _replace_once(
        section,
        'lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")',
        'lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNKNOWN")',
        label="DeltaText edit semantics",
    )
    section = _replace_once(
        section,
        'self.unknown_box.toggled.connect(self._set_unknown)\n        layout.addWidget(self.unknown_box)',
        'self.unknown_box.toggled.connect(self._set_unknown)\n        self.unknown_box.setChecked(True)\n        layout.addWidget(self.unknown_box)',
        label="DeltaText initial unknown",
    )
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaText unknown toggle",
    )
    section = _replace_once(
        section,
        '        self.unknown_box.setChecked(False)\n        self.line.clear()\n        self.mode_box.setCurrentText("UNCHANGED")',
        '        self.line.clear()\n        self.mode_box.setCurrentText("UNKNOWN")\n        self.unknown_box.setChecked(True)',
        label="DeltaText reset",
    )
    return section


def _patch_delta_hp(section: str) -> str:
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaHp initial mode",
    )
    section = _replace_once(
        section,
        'lambda index: self.mode_box.setCurrentText("CHANGED" if index > 0 else "UNCHANGED")',
        'lambda index: self.mode_box.setCurrentText("CHANGED" if index > 0 else "UNKNOWN")',
        label="DeltaHp selection semantics",
    )
    section = _replace_once(
        section,
        'self.unknown_box.toggled.connect(self._set_unknown)\n        layout.addWidget(self.unknown_box)',
        'self.unknown_box.toggled.connect(self._set_unknown)\n        self.unknown_box.setChecked(True)\n        layout.addWidget(self.unknown_box)',
        label="DeltaHp initial unknown",
    )
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaHp unknown toggle",
    )
    section = _replace_once(
        section,
        '        self.unknown_box.setChecked(False)\n        self.value_box.setCurrentIndex(0)\n        self.mode_box.setCurrentText("UNCHANGED")',
        '        self.value_box.setCurrentIndex(0)\n        self.mode_box.setCurrentText("UNKNOWN")\n        self.unknown_box.setChecked(True)',
        label="DeltaHp reset",
    )
    return section


def _patch_delta_side_effects(section: str) -> str:
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaSideEffects initial mode",
    )
    section = _replace_once(
        section,
        'lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")',
        'lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNKNOWN")',
        label="DeltaSideEffects edit semantics",
    )
    section = _replace_once(
        section,
        'self.unknown_box.toggled.connect(self._set_unknown)\n        layout.addWidget(self.unknown_box)',
        'self.unknown_box.toggled.connect(self._set_unknown)\n        self.unknown_box.setChecked(True)\n        layout.addWidget(self.unknown_box)',
        label="DeltaSideEffects initial unknown",
    )
    section = _replace_once(
        section,
        'self.mode_box.setCurrentText("UNKNOWN" if checked else "UNCHANGED")',
        'self.mode_box.setCurrentText("UNKNOWN")',
        label="DeltaSideEffects unknown toggle",
    )
    section = _replace_once(
        section,
        '        self.unknown_box.setChecked(False)\n        self.line.clear()\n        self.mode_box.setCurrentText("UNCHANGED")',
        '        self.line.clear()\n        self.mode_box.setCurrentText("UNKNOWN")\n        self.unknown_box.setChecked(True)',
        label="DeltaSideEffects reset",
    )
    return section


def patch_ui(text: str) -> str:
    before, block, after = _section(
        text,
        "class _DeltaIntField(QWidget):",
        "class _DeltaTextField(QWidget):",
        label="DeltaInt",
    )
    text = before + _patch_delta_int(block) + after

    before, block, after = _section(
        text,
        "class _DeltaTextField(QWidget):",
        "class _DeltaHpField(QWidget):",
        label="DeltaText",
    )
    text = before + _patch_delta_text(block) + after

    before, block, after = _section(
        text,
        "class _DeltaHpField(QWidget):",
        "class _DeltaSideEffectsField(QWidget):",
        label="DeltaHp",
    )
    text = before + _patch_delta_hp(block) + after

    before, block, after = _section(
        text,
        "class _DeltaSideEffectsField(QWidget):",
        "def _add_compact_stage_grid(",
        label="DeltaSideEffects",
    )
    text = before + _patch_delta_side_effects(block) + after

    text = _replace_once(
        text,
        "            active=FieldDelta.unchanged(),\n            hp_bucket=self.hp_field.to_delta(),",
        "            active=FieldDelta.unknown(),\n            hp_bucket=self.hp_field.to_delta(),",
        label="SideDelta active default",
    )

    projection_anchor = (
        '        self._result_validation_error = ""\n'
        '        self.self_delta_editor.reset()\n'
        '        self.opponent_delta_editor.reset()\n'
        '        for (side, field_name), value in stage_values.items():\n'
    )
    projection_replacement = (
        '        self._result_validation_error = ""\n'
        '        self.self_delta_editor.reset()\n'
        '        self.opponent_delta_editor.reset()\n\n'
        '        # A negative human decision is field-specific evidence of no change.\n'
        '        # Unrelated result fields remain UNKNOWN.\n'
        '        for candidate in self._result_candidates:\n'
        '            if (\n'
        '                self._result_candidate_decisions.get(candidate.candidate_id)\n'
        '                != "DID_NOT_OCCUR"\n'
        '            ):\n'
        '                continue\n'
        '            editor = (\n'
        '                self.self_delta_editor\n'
        '                if candidate.target_side == "self"\n'
        '                else self.opponent_delta_editor\n'
        '            )\n'
        '            if candidate.kind == "stage":\n'
        '                field = editor.stage_fields.get(candidate.field_name)\n'
        '                if field is not None:\n'
        '                    field.unknown_box.setChecked(False)\n'
        '                    field.mode_box.setCurrentText("UNCHANGED")\n'
        '            elif candidate.kind == "status":\n'
        '                editor.status_field.unknown_box.setChecked(False)\n'
        '                editor.status_field.mode_box.setCurrentText("UNCHANGED")\n\n'
        '        for (side, field_name), value in stage_values.items():\n'
    )
    text = _replace_once(
        text,
        projection_anchor,
        projection_replacement,
        label="DID_NOT_OCCUR projection",
    )
    return text


def patch_tests(text: str) -> str:
    text = _replace_once(
        text,
        "    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (\n"
        "        ChangeObservation.UNCHANGED\n"
        "    )\n",
        "    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (\n"
        "        ChangeObservation.UNKNOWN\n"
        "    )\n",
        label="removed event becomes UNKNOWN",
    )
    text = _replace_once(
        text,
        "    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNCHANGED\n"
        "    draft = controller.turn_state_summary().open_draft\n"
        "    assert draft is not None\n"
        "    assert draft.self_side.defense_stage.value == 0\n",
        "    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN\n"
        "    draft = controller.turn_state_summary().open_draft\n"
        "    assert draft is not None\n"
        "    assert not draft.self_side.defense_stage.is_confirmed\n",
        label="removed persisted event becomes UNKNOWN",
    )

    marker = (
        "def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:\n"
        "    repository, controller, window, _transport = build_window(tmp_path)\n"
        "    _reach_action_entry(window, controller)\n"
        "    _open_result(window)\n"
        "    assert window._result_events == []  # noqa: SLF001\n"
        "    window.next_turn_button.click()\n"
        "    assert controller.refresh().projection.session_state == \"TURN_CAPTURE_PENDING\"\n"
        "    assert controller.turn_state_summary().open_draft is not None\n"
        "    repository.close()\n"
    )
    replacement = (
        "def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:\n"
        "    repository, controller, window, _transport = build_window(tmp_path)\n"
        "    _reach_action_entry(window, controller)\n"
        "    confirmed = controller.turn_state_summary().confirmed_state\n"
        "    assert confirmed is not None\n"
        "    _open_result(window)\n"
        "    assert window._result_events == []  # noqa: SLF001\n"
        "    window.next_turn_button.click()\n"
        "    assert controller.refresh().projection.session_state == \"TURN_CAPTURE_PENDING\"\n"
        "    draft = controller.turn_state_summary().open_draft\n"
        "    assert draft is not None\n"
        "    persisted = repository.list_action_result_deltas_based_on(\n"
        "        confirmed.confirmed_state_id\n"
        "    )[-1]\n"
        "    assert persisted.self_side.active.observation is ChangeObservation.UNKNOWN\n"
        "    assert persisted.opponent_side.active.observation is ChangeObservation.UNKNOWN\n"
        "    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n"
        "    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n"
        "    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN\n"
        "    assert not draft.self_side.active.is_confirmed\n"
        "    assert not draft.self_side.hp_bucket.is_confirmed\n"
        "    repository.close()\n\n\n"
        "def test_opponent_faint_does_not_claim_unobserved_self_hp_unchanged(\n"
        "    tmp_path: Path,\n"
        ") -> None:\n"
        "    repository, controller, window, _transport = build_window(tmp_path)\n"
        "    _reach_action_entry(window, controller, own_move=\"Wave Crash\")\n"
        "    confirmed = controller.turn_state_summary().confirmed_state\n"
        "    assert confirmed is not None\n"
        "    _open_result(window)\n"
        "    window.record_opponent_faint_button.click()\n"
        "    window.next_turn_button.click()\n\n"
        "    persisted = repository.list_action_result_deltas_based_on(\n"
        "        confirmed.confirmed_state_id\n"
        "    )[-1]\n"
        "    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.CHANGED\n"
        "    assert persisted.opponent_side.hp_bucket.after_value is HpBucket.ZERO\n"
        "    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n"
        "    repository.close()\n"
    )
    text = _replace_once(text, marker, replacement, label="no-event regressions")
    return text


def main() -> int:
    ui_before = UI.read_text(encoding="utf-8")
    test_before = TEST.read_text(encoding="utf-8")
    ui_after = patch_ui(ui_before)
    test_after = patch_tests(test_before)
    if ui_after == ui_before or test_after == test_before:
        raise RuntimeError("patch produced no effective change")
    UI.write_text(ui_after, encoding="utf-8", newline="\n")
    TEST.write_text(test_after, encoding="utf-8", newline="\n")
    print("RESULT_MEMORY_DEFAULT_HOTFIX_PATCHED_V2")
    print(UI.relative_to(ROOT))
    print(TEST.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
