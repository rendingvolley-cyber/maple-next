"""Apply the tournament Result Entry memory-default hotfix, v3.

V3 is intentionally minimal: it leaves the reusable hidden delta widgets'
legacy defaults untouched and changes only the normal Result Entry projection.
Every Result Entry projection starts from UNKNOWN for unobserved fields, then
explicit events overwrite the exact affected fields with CHANGED, while a
human DID_NOT_OCCUR decision marks only that candidate field UNCHANGED.

The patch is fail-closed: every required transformation is validated in memory
before either authorized file is written.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "maple_next" / "ui" / "battle_record_ui.py"
TEST = ROOT / "tests" / "test_tournament_p0_result_entry_redesign.py"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def patch_ui(text: str) -> str:
    text = replace_once(
        text,
        """    def to_side_delta(self) -> SideDelta:\n        return SideDelta(\n            active=FieldDelta.unchanged(),\n            hp_bucket=self.hp_field.to_delta(),\n""",
        """    def to_side_delta(self) -> SideDelta:\n        return SideDelta(\n            active=FieldDelta.unknown(),\n            hp_bucket=self.hp_field.to_delta(),\n""",
        label="SideDelta active default",
    )

    anchor = """        self._result_validation_error = \"\"\n        self.self_delta_editor.reset()\n        self.opponent_delta_editor.reset()\n        for (side, field_name), value in stage_values.items():\n"""
    replacement = """        self._result_validation_error = \"\"\n        self.self_delta_editor.reset()\n        self.opponent_delta_editor.reset()\n\n        # Result Entry is an event recorder. Resetting the reusable hidden\n        # editors must not assert that every unobserved field stayed the same.\n        # Start the ordinary Result Entry projection at UNKNOWN; explicit\n        # confirmed events below overwrite only the exact affected fields.\n        for editor in (self.self_delta_editor, self.opponent_delta_editor):\n            editor.hp_field.mode_box.setCurrentText(\"UNKNOWN\")\n            editor.status_field.mode_box.setCurrentText(\"UNKNOWN\")\n            editor.side_effects_field.mode_box.setCurrentText(\"UNKNOWN\")\n            for field in editor.stage_fields.values():\n                field.mode_box.setCurrentText(\"UNKNOWN\")\n        self.weather_delta_field.mode_box.setCurrentText(\"UNKNOWN\")\n        self.terrain_delta_field.mode_box.setCurrentText(\"UNKNOWN\")\n\n        # DID_NOT_OCCUR is positive human evidence only for the specific\n        # candidate field. Unrelated result fields remain UNKNOWN.\n        for candidate in self._result_candidates:\n            if (\n                self._result_candidate_decisions.get(candidate.candidate_id)\n                != \"DID_NOT_OCCUR\"\n            ):\n                continue\n            editor = (\n                self.self_delta_editor\n                if candidate.target_side == \"self\"\n                else self.opponent_delta_editor\n            )\n            if candidate.kind == \"stage\":\n                field = editor.stage_fields.get(candidate.field_name)\n                if field is not None:\n                    field.mode_box.setCurrentText(\"UNCHANGED\")\n            elif candidate.kind == \"status\":\n                editor.status_field.mode_box.setCurrentText(\"UNCHANGED\")\n\n        for (side, field_name), value in stage_values.items():\n"""
    text = replace_once(text, anchor, replacement, label="Result Entry UNKNOWN projection")
    return text


def patch_tests(text: str) -> str:
    text = replace_once(
        text,
        """    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (\n        ChangeObservation.UNCHANGED\n    )\n""",
        """    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (\n        ChangeObservation.UNKNOWN\n    )\n""",
        label="removed event draft becomes UNKNOWN",
    )
    text = replace_once(
        text,
        """    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNCHANGED\n    draft = controller.turn_state_summary().open_draft\n    assert draft is not None\n    assert draft.self_side.defense_stage.value == 0\n""",
        """    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN\n    draft = controller.turn_state_summary().open_draft\n    assert draft is not None\n    assert not draft.self_side.defense_stage.is_confirmed\n""",
        label="removed event persisted becomes UNKNOWN",
    )

    no_proc_anchor = """    assert delta.self_side.special_defense_stage.observation is ChangeObservation.UNCHANGED\n    repository.close()\n"""
    no_proc_replacement = """    assert delta.self_side.special_defense_stage.observation is ChangeObservation.UNCHANGED\n    assert delta.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n    assert delta.self_side.active.observation is ChangeObservation.UNKNOWN\n    assert delta.self_side.attack_stage.observation is ChangeObservation.UNKNOWN\n    repository.close()\n"""
    text = replace_once(
        text,
        no_proc_anchor,
        no_proc_replacement,
        label="DID_NOT_OCCUR field-specific regression",
    )

    old_no_event = """def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:\n    repository, controller, window, _transport = build_window(tmp_path)\n    _reach_action_entry(window, controller)\n    _open_result(window)\n    assert window._result_events == []  # noqa: SLF001\n    window.next_turn_button.click()\n    assert controller.refresh().projection.session_state == \"TURN_CAPTURE_PENDING\"\n    assert controller.turn_state_summary().open_draft is not None\n    repository.close()\n"""
    new_no_event = """def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:\n    repository, controller, window, _transport = build_window(tmp_path)\n    _reach_action_entry(window, controller)\n    confirmed = controller.turn_state_summary().confirmed_state\n    assert confirmed is not None\n    _open_result(window)\n    assert window._result_events == []  # noqa: SLF001\n    window.next_turn_button.click()\n    assert controller.refresh().projection.session_state == \"TURN_CAPTURE_PENDING\"\n    draft = controller.turn_state_summary().open_draft\n    assert draft is not None\n    persisted = repository.list_action_result_deltas_based_on(\n        confirmed.confirmed_state_id\n    )[-1]\n    assert persisted.self_side.active.observation is ChangeObservation.UNKNOWN\n    assert persisted.opponent_side.active.observation is ChangeObservation.UNKNOWN\n    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN\n    assert persisted.opponent_side.status.observation is ChangeObservation.UNKNOWN\n    assert persisted.weather.observation is ChangeObservation.UNKNOWN\n    assert persisted.terrain.observation is ChangeObservation.UNKNOWN\n    assert not draft.self_side.active.is_confirmed\n    assert not draft.self_side.hp_bucket.is_confirmed\n    repository.close()\n\n\ndef test_opponent_faint_keeps_unobserved_self_hp_unknown(tmp_path: Path) -> None:\n    repository, controller, window, _transport = build_window(tmp_path)\n    _reach_action_entry(window, controller, own_move=\"Wave Crash\")\n    confirmed = controller.turn_state_summary().confirmed_state\n    assert confirmed is not None\n    _open_result(window)\n    window.record_opponent_faint_button.click()\n    window.next_turn_button.click()\n\n    persisted = repository.list_action_result_deltas_based_on(\n        confirmed.confirmed_state_id\n    )[-1]\n    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.CHANGED\n    assert persisted.opponent_side.hp_bucket.after_value is HpBucket.ZERO\n    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN\n    repository.close()\n"""
    text = replace_once(text, old_no_event, new_no_event, label="no-event and faint regressions")
    return text


def main() -> int:
    ui_before = UI.read_text(encoding="utf-8")
    test_before = TEST.read_text(encoding="utf-8")

    ui_after = patch_ui(ui_before)
    test_after = patch_tests(test_before)

    if ui_after == ui_before:
        raise RuntimeError("UI patch produced no effective change")
    if test_after == test_before:
        raise RuntimeError("test patch produced no effective change")

    # Write only after all required transformations succeeded in memory.
    UI.write_text(ui_after, encoding="utf-8", newline="\n")
    TEST.write_text(test_after, encoding="utf-8", newline="\n")

    print("RESULT_MEMORY_DEFAULT_HOTFIX_PATCHED_V3")
    print(UI.relative_to(ROOT))
    print(TEST.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
