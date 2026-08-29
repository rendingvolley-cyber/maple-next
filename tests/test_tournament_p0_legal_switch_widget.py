"""Tournament P0: the "交代できるポケモン" turn-facts switch widget.

Two real defects, proven and fixed together in
``BattleRecordUiWindow._sync_switch_candidates`` (``ui/battle_record_ui.py``):

1. ``self.switch_checkboxes`` were labeled with the raw ``selected_three``
   (all three members, including the current active Pokemon itself) --
   never excluding the active or a confirmed-fainted member. Fixed by
   re-deriving through the same canonical helper
   (``domain.legal_switches.derive_legal_switch_candidates``, via the
   controller's ``derive_legal_switch_candidates_for_active``) the sibling
   Legal Switch Confirmation workbench already used correctly.
2. ``self.parity_switch_chips`` -- the actual on-screen "交代できるポケモン"
   buttons in this v5 window -- were built exactly once at ``__init__`` time
   from ``checkbox.text()``, which is always empty then, and were never
   resynced afterward (``QCheckBox`` has no ``textChanged`` signal). Every
   real match therefore showed three permanently blank, unusable buttons --
   the exact live production symptom this file reproduces and fixes.

Reuses the existing ``test_issue31_turn_state_ui_bundle_c`` fixtures/team
constants -- no new team/session setup is invented here, and no fabricated
schema is introduced anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    SELF_TEAM,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.enums import HpBucket
from maple_next.domain.turn_state import Known, PokemonLocalMemory, ProvenanceStep

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)

# SELECTED_THREE == (Meowscarada, Gholdengo, Dragonite); SELF_TEAM's other
# three members (Dondozo, Flutter Mane, Urshifu) must never appear anywhere
# on this widget for any match built from this fixture.
_BACKLINE_NEVER_SELECTED = tuple(name for name in SELF_TEAM if name not in SELECTED_THREE)


def _checkbox_names(window) -> tuple[str, ...]:
    return tuple(cb.text() for cb in window.switch_checkboxes if cb.text())


def _chip_names(window) -> tuple[str, ...]:
    return tuple(chip.text() for chip in window.parity_switch_chips if chip.text())


def _mark_fainted(repository, session, *, name: str) -> None:
    with repository.transaction():
        repository.upsert_pokemon_local_state(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            side="SELF",
            memory=PokemonLocalMemory(
                pokemon_name=name,
                hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
                status=Known.confirmed("NONE", provenance_chain=_HUMAN),
            ),
        )


def test_1_candidates_exclude_active_and_never_leak_backline(tmp_path: Path) -> None:
    """selected_three=A,B,C; active=A; all alive -> visible switches exactly B,C.

    Also the smoking-gun assertion: the parity mirror buttons (the actual
    on-screen widget) must carry the same real names, not permanently blank
    text from their one-time construction.
    """

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)  # sets self_active_box to SELECTED_THREE[0]

    assert _checkbox_names(window) == ("Gholdengo", "Dragonite")
    assert _chip_names(window) == ("Gholdengo", "Dragonite")
    for stale_name in _BACKLINE_NEVER_SELECTED:
        assert stale_name not in _checkbox_names(window)
        assert stale_name not in _chip_names(window)
    repository.close()


def test_2_fainted_backline_member_excluded(tmp_path: Path) -> None:
    """Same state, Dragonite (C) confirmed-fainted -> exactly Gholdengo (B)."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    session = repository.load_active_session()
    assert session is not None
    _mark_fainted(repository, session, name="Dragonite")
    _fill_minimal_current_state(window)

    assert _checkbox_names(window) == ("Gholdengo",)
    assert _chip_names(window) == ("Gholdengo",)
    repository.close()


def test_3_active_change_recomputes_candidates(tmp_path: Path) -> None:
    """Active changes from Meowscarada to Gholdengo -> exactly Meowscarada, Dragonite."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    assert _checkbox_names(window) == ("Gholdengo", "Dragonite")

    window.self_active_box.setCurrentText("Gholdengo")

    assert _checkbox_names(window) == ("Meowscarada", "Dragonite")
    assert _chip_names(window) == ("Meowscarada", "Dragonite")
    repository.close()


def test_4_previous_match_selected_three_never_leaks_into_new_match(tmp_path: Path) -> None:
    """A prior match's selected_three must never reappear once a new match
    with a different selected_three is underway -- proven by actually
    running two sequential matches in the same window instance."""

    repository, controller, window, _transport = build_window(tmp_path)

    # Match 1: a different selected_three (the tail of SELF_TEAM), taken all
    # the way to TURN_CAPTURE_PENDING so the widget is genuinely populated
    # (derive_legal_switch_candidates_for_active correctly requires an
    # active turn -- there is nothing to leak before one exists).
    first_three = SELF_TEAM[3:6]
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(first_three), first_three[0])
    controller.apply_selection(list(first_three), first_three[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()
    window.self_active_box.setCurrentText(first_three[0])
    assert set(_checkbox_names(window)) == {first_three[1], first_three[2]}

    abort_view = controller.abort_match(human_confirmed=True)
    assert abort_view.error_message is None, abort_view.error_message
    window.render_view(abort_view)

    # Match 2: the standard SELECTED_THREE fixture (head of SELF_TEAM).
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELECTED_THREE), SELECTED_THREE[0])
    controller.apply_selection(list(SELECTED_THREE), SELECTED_THREE[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()
    _fill_minimal_current_state(window)

    visible = set(_checkbox_names(window)) | set(_chip_names(window))
    for stale_name in first_three:
        assert stale_name not in visible
    assert _checkbox_names(window) == ("Gholdengo", "Dragonite")
    assert _chip_names(window) == ("Gholdengo", "Dragonite")
    repository.close()


def test_7_rerender_preserves_same_candidates_after_confirm(tmp_path: Path) -> None:
    """post-confirm / rerender -> the same current-match candidates remain visible."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    before = _checkbox_names(window)
    assert before == ("Gholdengo", "Dragonite")

    window._on_confirm_turn_facts()  # noqa: SLF001
    window.render_view()
    window.render_view()

    assert _checkbox_names(window) == before
    assert _chip_names(window) == before
    repository.close()


def test_action_selector_uses_exact_confirmation_when_legacy_facts_are_empty(
    tmp_path: Path,
) -> None:
    """P0 production regression: the real action selector must use the
    exact confirmed binding even when the legacy reviewed-facts switch list
    is empty, while retaining the active-Pokemon exclusion."""

    self_team = ("メタグロス", "サザンドラ", "アシレーヌ", "ドリュウズ", "ロトム", "ガブリアス")
    opponent_team = (
        "カイリュー",
        "ハバタクカミ",
        "パオジアン",
        "サーフゴー",
        "ウーラオス",
        "ガチグマ",
    )
    selected_three = self_team[:3]

    repository, controller, window, _transport = build_window(tmp_path)
    controller.new_match()
    controller.confirm_selection_facts(list(self_team), list(opponent_team))
    controller.submit_mock_advice(list(selected_three), selected_three[0])
    controller.apply_selection(list(selected_three), selected_three[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()

    window.self_active_box.setCurrentText("メタグロス")
    window.opponent_active_input.setText("カイリュー")
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText("バレットパンチ")
    window.move_inputs[1].setText("コメットパンチ")
    for checkbox in window.switch_checkboxes:
        checkbox.setChecked(False)
    for index in range(window.legal_switch_list.count()):
        window.legal_switch_list.item(index).setSelected(True)
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")

    # This is the real production handler. It stores legacy legal_switches
    # from the unchecked memo boxes as (), but stores the separately bound
    # exact confirmation from the visible Legal Switch workbench.
    window._on_confirm_turn_facts()  # noqa: SLF001
    window.render_view()

    session = repository.load_active_session()
    assert session is not None and session.current_reviewed_board_id is not None
    reviewed = repository.get_turn_facts(session.current_reviewed_board_id)
    assert reviewed.legal_switches == ()
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.legal_switches == (
        "サザンドラ",
        "アシレーヌ",
    )

    window._set_self_action_type("SWITCH")  # noqa: SLF001
    window.render_view()

    rendered = tuple(
        window.self_switch_target_box.itemText(index)
        for index in range(window.self_switch_target_box.count())
    )
    assert rendered == ("サザンドラ", "アシレーヌ")
    assert "メタグロス" not in rendered
    assert window.self_switch_unavailable_label.text() != "交代できる候補がありません"
    assert window.self_switch_unavailable_label.isHidden()

    # An explicit zero confirmation remains a different state from the
    # unresolved/legacy empty list and is the only normal zero-switch message.
    window._on_confirm_legal_switches_none()  # noqa: SLF001
    window._set_self_action_type("SWITCH")  # noqa: SLF001
    window.render_view()
    assert window.self_switch_target_box.count() == 0
    assert window.self_switch_unavailable_label.text() == "交代できるポケモンはいません"
    assert not window.self_switch_unavailable_label.isHidden()
    repository.close()
