"""Apply the tournament Result Entry memory-default hotfix deterministically.

Temporary bridge helper for the production checkout.  It patches only the
accepted tournament branch shape and fails closed if any expected source
fragment has drifted.
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
        '''class _DeltaIntField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(True)
        self.spin.setMaximumWidth(40)
        self.spin.valueChanged.connect(lambda _value: self.mode_box.setCurrentText("CHANGED"))
        layout.addWidget(self.spin)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)
''',
        '''class _DeltaIntField(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.mode_box = QComboBox()
        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNKNOWN")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.spin = QSpinBox()
        self.spin.setRange(-6, 6)
        self.spin.setEnabled(True)
        self.spin.setMaximumWidth(40)
        self.spin.valueChanged.connect(lambda _value: self.mode_box.setCurrentText("CHANGED"))
        layout.addWidget(self.spin)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        self.unknown_box.setChecked(True)
        layout.addWidget(self.unknown_box)
''',
        label="DeltaInt init",
    )
    text = replace_once(
        text,
        '''    def reset(self) -> None:
        """Back to a fresh UNCHANGED draft -- never carries a prior Turn's
        CHANGED value forward into a new Turn identity."""

        self.unknown_box.setChecked(False)
        self.spin.setValue(0)
        self.mode_box.setCurrentText("UNCHANGED")


class _DeltaTextField(QWidget):
''',
        '''    def reset(self) -> None:
        """Back to a fresh UNKNOWN draft until the operator observes a result."""

        self.spin.setValue(0)
        self.mode_box.setCurrentText("UNKNOWN")
        self.unknown_box.setChecked(True)


class _DeltaTextField(QWidget):
''',
        label="DeltaInt reset",
    )
    text = replace_once(
        text,
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText(placeholder)
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)
''',
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNKNOWN")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText(placeholder)
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNKNOWN")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        self.unknown_box.setChecked(True)
        layout.addWidget(self.unknown_box)
''',
        label="DeltaText init",
    )
    text = replace_once(
        text,
        '''    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.line.clear()
        self.mode_box.setCurrentText("UNCHANGED")


class _DeltaHpField(QWidget):
''',
        '''    def reset(self) -> None:
        self.line.clear()
        self.mode_box.setCurrentText("UNKNOWN")
        self.unknown_box.setChecked(True)


class _DeltaHpField(QWidget):
''',
        label="DeltaText reset",
    )
    text = replace_once(
        text,
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.value_box = QComboBox()
        self.value_box.addItem("—")
        for bucket in HpBucket:
            self.value_box.addItem(bucket.value)
        self.value_box.currentIndexChanged.connect(
            lambda index: self.mode_box.setCurrentText("CHANGED" if index > 0 else "UNCHANGED")
        )
        layout.addWidget(self.value_box)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)
''',
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNKNOWN")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.value_box = QComboBox()
        self.value_box.addItem("—")
        for bucket in HpBucket:
            self.value_box.addItem(bucket.value)
        self.value_box.currentIndexChanged.connect(
            lambda index: self.mode_box.setCurrentText("CHANGED" if index > 0 else "UNKNOWN")
        )
        layout.addWidget(self.value_box)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        self.unknown_box.setChecked(True)
        layout.addWidget(self.unknown_box)
''',
        label="DeltaHp init",
    )
    text = replace_once(
        text,
        '''    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.value_box.setCurrentIndex(0)
        self.mode_box.setCurrentText("UNCHANGED")


class _DeltaSideEffectsField(QWidget):
''',
        '''    def reset(self) -> None:
        self.value_box.setCurrentIndex(0)
        self.mode_box.setCurrentText("UNKNOWN")
        self.unknown_box.setChecked(True)


class _DeltaSideEffectsField(QWidget):
''',
        label="DeltaHp reset",
    )
    text = replace_once(
        text,
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNCHANGED")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText("やけど,こんらん")
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNCHANGED")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        layout.addWidget(self.unknown_box)
''',
        '''        self.mode_box.addItems(["UNKNOWN", "UNCHANGED", "CHANGED"])
        self.mode_box.setCurrentText("UNKNOWN")
        self.mode_box.setVisible(False)  # compatibility state, not an operator control
        self.line = QLineEdit()
        self.line.setPlaceholderText("やけど,こんらん")
        self.line.textEdited.connect(
            lambda text: self.mode_box.setCurrentText("CHANGED" if text.strip() else "UNKNOWN")
        )
        layout.addWidget(self.line, 1)
        self.unknown_box = QCheckBox("観測不能")
        self.unknown_box.toggled.connect(self._set_unknown)
        self.unknown_box.setChecked(True)
        layout.addWidget(self.unknown_box)
''',
        label="DeltaSideEffects init",
    )
    text = replace_once(
        text,
        '''    def reset(self) -> None:
        self.unknown_box.setChecked(False)
        self.line.clear()
        self.mode_box.setCurrentText("UNCHANGED")


def _add_compact_stage_grid(
''',
        '''    def reset(self) -> None:
        self.line.clear()
        self.mode_box.setCurrentText("UNKNOWN")
        self.unknown_box.setChecked(True)


def _add_compact_stage_grid(
''',
        label="DeltaSideEffects reset",
    )
    text = replace_once(
        text,
        '''    and never read from here (:meth:`to_side_delta` always reports
    ``active=UNCHANGED``; the caller substitutes the computed delta when a
    switch was confirmed).
''',
        '''    and never read from here (:meth:`to_side_delta` reports
    ``active=UNKNOWN`` unless the caller substitutes the computed delta for a
    confirmed SWITCH). Unobserved result fields never claim no-change.
''',
        label="SideDeltaEditor doc",
    )
    text = replace_once(
        text,
        '''    def to_side_delta(self) -> SideDelta:
        return SideDelta(
            active=FieldDelta.unchanged(),
''',
        '''    def to_side_delta(self) -> SideDelta:
        return SideDelta(
            active=FieldDelta.unknown(),
''',
        label="active default",
    )
    text = replace_once(
        text,
        '''        self._result_validation_error = ""
        self.self_delta_editor.reset()
        self.opponent_delta_editor.reset()
        for (side, field_name), value in stage_values.items():
''',
        '''        self._result_validation_error = ""
        self.self_delta_editor.reset()
        self.opponent_delta_editor.reset()

        # A negative human decision is field-specific evidence of no change.
        # Everything else stays UNKNOWN until an explicit positive event or
        # the next Turn's fresh OCR/manual confirmation establishes it.
        for candidate in self._result_candidates:
            if self._result_candidate_decisions.get(candidate.candidate_id) != "DID_NOT_OCCUR":
                continue
            editor = (
                self.self_delta_editor
                if candidate.target_side == "self"
                else self.opponent_delta_editor
            )
            if candidate.kind == "stage":
                field = editor.stage_fields.get(candidate.field_name)
                if field is not None:
                    field.unknown_box.setChecked(False)
                    field.mode_box.setCurrentText("UNCHANGED")
            elif candidate.kind == "status":
                editor.status_field.unknown_box.setChecked(False)
                editor.status_field.mode_box.setCurrentText("UNCHANGED")

        for (side, field_name), value in stage_values.items():
''',
        label="DID_NOT_OCCUR projection",
    )
    text = text.replace(
        "A fresh identity always starts its\n            # own result-delta draft UNCHANGED -- the CHANGED value",
        "A fresh identity always starts its\n            # own result-delta draft UNKNOWN -- the CHANGED value",
    )
    return text


def patch_tests(text: str) -> str:
    text = replace_once(
        text,
        '''    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (
        ChangeObservation.UNCHANGED
    )
''',
        '''    assert window.self_delta_editor.to_side_delta().defense_stage.observation is (
        ChangeObservation.UNKNOWN
    )
''',
        label="removed event becomes UNKNOWN",
    )
    text = replace_once(
        text,
        '''    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNCHANGED
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.defense_stage.value == 0
''',
        '''    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert not draft.self_side.defense_stage.is_confirmed
''',
        label="removed persisted event becomes UNKNOWN",
    )
    marker = '''def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    _open_result(window)
    assert window._result_events == []  # noqa: SLF001
    window.next_turn_button.click()
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    assert controller.turn_state_summary().open_draft is not None
    repository.close()
'''
    replacement = '''def test_no_event_result_entry_advances_without_dummy_result(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller)
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    _open_result(window)
    assert window._result_events == []  # noqa: SLF001
    window.next_turn_button.click()
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    persisted = repository.list_action_result_deltas_based_on(confirmed.confirmed_state_id)[-1]
    assert persisted.self_side.active.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.active.observation is ChangeObservation.UNKNOWN
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    assert persisted.self_side.defense_stage.observation is ChangeObservation.UNKNOWN
    assert not draft.self_side.active.is_confirmed
    assert not draft.self_side.hp_bucket.is_confirmed
    repository.close()


def test_opponent_faint_does_not_claim_unobserved_self_hp_unchanged(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _reach_action_entry(window, controller, own_move="Wave Crash")
    confirmed = controller.turn_state_summary().confirmed_state
    assert confirmed is not None
    _open_result(window)
    window.record_opponent_faint_button.click()
    window.next_turn_button.click()

    persisted = repository.list_action_result_deltas_based_on(confirmed.confirmed_state_id)[-1]
    assert persisted.opponent_side.hp_bucket.observation is ChangeObservation.CHANGED
    assert persisted.opponent_side.hp_bucket.after_value is HpBucket.ZERO
    assert persisted.self_side.hp_bucket.observation is ChangeObservation.UNKNOWN
    repository.close()
'''
    text = replace_once(text, marker, replacement, label="no-event regression expansion")
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
    print("RESULT_MEMORY_DEFAULT_HOTFIX_PATCHED")
    print(UI.relative_to(ROOT))
    print(TEST.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
