"""Tournament production hotfix: direct stat-stage entry + common presets.

Focused tests for the new first-visible "＋ 状態変化を記録" surface
(``_DirectStageEditorDialog``) added on top of the existing canonical
SideDelta/Action Result mechanism. No new state model, no persistence path
other than the existing bottom-bar "行動・結果記録" -> ``_on_record_action``
flow already exercised by ``test_issue31_event_entry_ui_v3.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton
from test_issue31_event_entry_ui_v3 import (
    _advance_through_mock_turn_advice,
    _confirm_full_turn_facts,
)
from test_issue31_turn_state_ui_bundle_c import (
    _advance_to_turn_capture_pending,
    build_window,
)

from maple_next.domain.battle_events import COMMON_STAGE_EVENT_PRESETS
from maple_next.domain.turn_state import ChangeObservation, Known, ProvenanceStep
from maple_next.ui.battle_record_ui import _DirectStageEditorDialog, _StateEventDialog


def _open_dialog(window) -> _DirectStageEditorDialog:
    window._open_direct_stage_editor_dialog()  # noqa: SLF001
    return window._direct_stage_editor_dialog  # noqa: SLF001


def test_actual_visible_button_opens_direct_surface_and_preserves_legacy_path(
    tmp_path: Path,
) -> None:
    """Real production composition, signal and phase -- no direct handler call."""

    _repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("fixture prediction")
    window.mock_turn_rationale_input.setText("fixture rationale")
    window._on_submit_mock_turn()  # noqa: SLF001 - network-free mock transition
    window.render_view()
    window.header_tabs.setCurrentIndex(1)
    window.show()
    QApplication.processEvents()

    assert controller.refresh().projection.primary_cta == "RECORD_ACTUAL_ACTION"
    assert window.review_state_event_button.isVisible()
    assert window.review_state_event_button.isEnabled()
    assert window.result_state_event_button.isHidden()

    # Click the one button the operator actually sees.  The displayed object
    # must be the direct editor, not a test-created or hidden duplicate.
    window.review_state_event_button.click()
    QApplication.processEvents()
    dialog = window._direct_stage_editor_dialog  # noqa: SLF001
    assert isinstance(dialog, _DirectStageEditorDialog)
    assert dialog.isVisible()
    assert dialog._value_labels["special_attack_stage"].text() == "+0"  # noqa: SLF001
    assert dialog._plus_buttons["special_attack_stage"].isVisible()  # noqa: SLF001
    assert dialog._plus_buttons["special_attack_stage"].text() == "+1"  # noqa: SLF001
    shell_smash = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "からをやぶる"
    )
    assert shell_smash.isVisible()

    dialog._on_adjust("special_attack_stage", 1)  # noqa: SLF001
    dialog._on_apply()  # noqa: SLF001
    delta = window.self_delta_editor.to_side_delta()
    assert delta.special_attack_stage.observation is ChangeObservation.CHANGED
    assert delta.special_attack_stage.after_value == 1

    # 特性 / 技 remains available as the direct surface's secondary route.
    window.review_state_event_button.click()
    QApplication.processEvents()
    second_dialog = window._direct_stage_editor_dialog  # noqa: SLF001
    second_dialog.legacy_button.click()
    QApplication.processEvents()
    legacy = window._state_event_dialog  # noqa: SLF001
    assert isinstance(legacy, _StateEventDialog)
    assert legacy.isVisible()
    legacy_labels = {button.text() for button in legacy.findChildren(QPushButton)}
    assert "いかく" in legacy_labels  # ability catalog path
    assert "からをやぶる" in legacy_labels  # move catalog path
    assert transport.calls == []


# --- common presets are data, in the exact spec order -----------------------


def test_common_presets_are_the_exact_spec_set_in_order() -> None:
    assert [preset.label for preset in COMMON_STAGE_EVENT_PRESETS] == [
        "からをやぶる",
        "りゅうのまい",
        "ちょうのまい",
        "ビルドアップ",
        "めいそう",
        "つるぎのまい",
        "わるだくみ",
        "こうそくいどう",
    ]
    # The bulk zero-out preset is not a "common move effect" button.
    assert "reset" not in [preset.key for preset in COMMON_STAGE_EVENT_PRESETS]


# --- acceptance 1: Flare Song (self, 特攻 +1) --------------------------------


def test_flare_song_path_self_special_attack_plus_one(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    assert dialog.target_self_button.isChecked()
    dialog._on_adjust("special_attack_stage", 1)  # noqa: SLF001
    assert dialog._pending["self"]["special_attack_stage"] == 1  # noqa: SLF001
    dialog._on_apply()  # noqa: SLF001

    delta = window.self_delta_editor.to_side_delta()
    assert delta.special_attack_stage.observation is ChangeObservation.CHANGED
    assert delta.special_attack_stage.after_value == 1
    # Nothing else on the self side moved.
    assert delta.attack_stage.observation is ChangeObservation.UNCHANGED


# --- acceptance 2: Shell Smash (からをやぶる) preview + apply ----------------


def test_shell_smash_preset_preview_and_apply_matches_side_delta(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    karawoyaburu = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "からをやぶる")
    dialog._on_preset_clicked(karawoyaburu)  # noqa: SLF001
    assert dialog._pending["self"] == {  # noqa: SLF001
        "attack_stage": 2,
        "defense_stage": -1,
        "special_attack_stage": 2,
        "special_defense_stage": -1,
        "speed_stage": 2,
    }
    dialog._on_apply()  # noqa: SLF001

    delta = window.self_delta_editor.to_side_delta()
    assert delta.attack_stage.after_value == 2
    assert delta.defense_stage.after_value == -1
    assert delta.special_attack_stage.after_value == 2
    assert delta.special_defense_stage.after_value == -1
    assert delta.speed_stage.after_value == 2


# --- acceptance 3: Dragon Dance (りゅうのまい) -------------------------------


def test_dragon_dance_preset_atk_plus_one_spe_plus_one(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    ryuunomai = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "りゅうのまい")
    dialog._on_preset_clicked(ryuunomai)  # noqa: SLF001
    assert dialog._pending["self"] == {"attack_stage": 1, "speed_stage": 1}  # noqa: SLF001


# --- acceptance 4: opponent edit must use opponent SideDelta ----------------


def test_opponent_target_writes_opponent_side_delta_not_self(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    dialog._set_target("opponent")  # noqa: SLF001
    assert dialog.target_opponent_button.isChecked()
    dialog._on_adjust("special_attack_stage", -1)  # noqa: SLF001
    dialog._on_apply()  # noqa: SLF001

    opponent_delta = window.opponent_delta_editor.to_side_delta()
    self_delta = window.self_delta_editor.to_side_delta()
    assert opponent_delta.special_attack_stage.observation is ChangeObservation.CHANGED
    assert opponent_delta.special_attack_stage.after_value == -1
    assert self_delta.special_attack_stage.observation is ChangeObservation.UNCHANGED


# --- acceptance 5: bounds clamp instead of silently overflowing -------------


def test_plus_one_at_max_stage_is_blocked_not_silently_overflowed(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    for _ in range(6):
        dialog._on_adjust("attack_stage", 1)  # noqa: SLF001
    assert dialog._pending["self"]["attack_stage"] == 6  # noqa: SLF001
    assert dialog._plus_buttons["attack_stage"].isEnabled() is False  # noqa: SLF001
    dialog._on_adjust("attack_stage", 1)  # noqa: SLF001 -- extra click must not overflow past 6
    assert dialog._pending["self"]["attack_stage"] == 6  # noqa: SLF001


def test_minus_one_at_min_stage_is_blocked_not_silently_underflowed(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    for _ in range(6):
        dialog._on_adjust("attack_stage", -1)  # noqa: SLF001
    assert dialog._pending["self"]["attack_stage"] == -6  # noqa: SLF001
    assert dialog._minus_buttons["attack_stage"].isEnabled() is False  # noqa: SLF001
    dialog._on_adjust("attack_stage", -1)  # noqa: SLF001
    assert dialog._pending["self"]["attack_stage"] == -6  # noqa: SLF001


# --- acceptance 6: unknown baseline is never silently treated as 0 ----------


def test_unknown_baseline_shows_unknown_label_and_blocks_direct_buttons(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    def known_stages_fn(side: str) -> dict[str, Known[int]]:
        # Real confirmed values for every field except one deliberately
        # forced UNKNOWN, to exercise the "never assume 0" rule without
        # round-tripping a hand-built ConfirmedTurnState through the repo.
        values = dict(controller.stage_known_values(side=side))
        if side == "self":
            values["special_attack_stage"] = Known.unknown()
        return values

    dialog = _DirectStageEditorDialog(
        window,
        self_editor=window.self_delta_editor,
        opponent_editor=window.opponent_delta_editor,
        known_stages_fn=known_stages_fn,
        open_legacy=lambda: None,
    )
    assert dialog._value_labels["special_attack_stage"].text() == "現在ランク不明"  # noqa: SLF001
    assert dialog._plus_buttons["special_attack_stage"].isEnabled() is False  # noqa: SLF001
    assert dialog._minus_buttons["special_attack_stage"].isEnabled() is False  # noqa: SLF001

    dialog._on_adjust("special_attack_stage", 1)  # noqa: SLF001
    assert "special_attack_stage" not in dialog._pending["self"]  # noqa: SLF001

    meisou = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "めいそう")
    dialog._on_preset_clicked(meisou)  # noqa: SLF001
    # めいそう touches special_attack_stage (unknown) and
    # special_defense_stage (known 0). Presets are atomic: one unknown
    # required baseline blocks the *whole* preset, so special_defense_stage
    # must not be populated either, even though its own baseline is known.
    assert dialog._pending["self"] == {}  # noqa: SLF001


# --- atomic presets: known/unknown/overflow -------------------------------


def test_shell_smash_all_known_populates_atomically(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    karawoyaburu = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "からをやぶる")
    dialog._on_preset_clicked(karawoyaburu)  # noqa: SLF001
    assert dialog._pending["self"] == {  # noqa: SLF001
        "attack_stage": 2,
        "defense_stage": -1,
        "special_attack_stage": 2,
        "special_defense_stage": -1,
        "speed_stage": 2,
    }


def test_shell_smash_one_unknown_baseline_blocks_the_whole_preset(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    def known_stages_fn(side: str) -> dict[str, Known[int]]:
        values = dict(controller.stage_known_values(side=side))
        if side == "self":
            # Only one of からをやぶる's five required fields is unknown.
            values["defense_stage"] = Known.unknown()
        return values

    dialog = _DirectStageEditorDialog(
        window,
        self_editor=window.self_delta_editor,
        opponent_editor=window.opponent_delta_editor,
        known_stages_fn=known_stages_fn,
        open_legacy=lambda: None,
    )
    karawoyaburu = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "からをやぶる")
    dialog._on_preset_clicked(karawoyaburu)  # noqa: SLF001

    # Zero preset changes queued -- not even the four fields whose own
    # baseline was known -- and the existing (empty) draft is unchanged.
    assert dialog._pending["self"] == {}  # noqa: SLF001
    assert dialog._pending["opponent"] == {}  # noqa: SLF001
    assert "現在ランク不明の能力があるためプリセットを適用できません" in dialog.status_label.text()
    assert "防御" in dialog.status_label.text()

    dialog._on_apply()  # noqa: SLF001
    delta = window.self_delta_editor.to_side_delta()
    assert delta.attack_stage.observation is ChangeObservation.UNCHANGED
    assert delta.defense_stage.observation is ChangeObservation.UNCHANGED
    assert delta.special_attack_stage.observation is ChangeObservation.UNCHANGED


def test_shell_smash_one_component_would_overflow_blocks_the_whole_preset(
    tmp_path: Path,
) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    def known_stages_fn(side: str) -> dict[str, Known[int]]:
        values = dict(controller.stage_known_values(side=side))
        if side == "self":
            # attack_stage is already +6; からをやぶる's +2 would overflow
            # it to +8. The other four required fields are all known and
            # would individually be fine.
            values["attack_stage"] = Known.confirmed(
                6, provenance_chain=(ProvenanceStep.HUMAN_INPUT,)
            )
        return values

    dialog = _DirectStageEditorDialog(
        window,
        self_editor=window.self_delta_editor,
        opponent_editor=window.opponent_delta_editor,
        known_stages_fn=known_stages_fn,
        open_legacy=lambda: None,
    )
    karawoyaburu = next(p for p in COMMON_STAGE_EVENT_PRESETS if p.label == "からをやぶる")
    dialog._on_preset_clicked(karawoyaburu)  # noqa: SLF001

    assert dialog._pending["self"] == {}  # noqa: SLF001
    assert "上限(+6)/下限(-6)を超えるためプリセットを適用できません" in dialog.status_label.text()
    assert "攻撃" in dialog.status_label.text()

    dialog._on_apply()  # noqa: SLF001
    delta = window.self_delta_editor.to_side_delta()
    assert delta.defense_stage.observation is ChangeObservation.UNCHANGED
    assert delta.special_attack_stage.observation is ChangeObservation.UNCHANGED


# --- acceptance 7: 行動・結果記録 still performs canonical persistence ------


def test_action_result_record_still_persists_after_dialog_apply(tmp_path: Path) -> None:
    _repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _confirm_full_turn_facts(controller, window)

    dialog = _open_dialog(window)
    dialog._on_adjust("special_attack_stage", 1)  # noqa: SLF001
    dialog._on_apply()  # noqa: SLF001

    # Populating the editor via the dialog is not itself a canonical write.
    assert controller.turn_state_summary().latest_delta is None

    _advance_through_mock_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001

    persisted = controller.turn_state_summary().latest_delta
    assert persisted is not None
    assert persisted.self_side.special_attack_stage.after_value == 1
