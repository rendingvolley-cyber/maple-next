"""Gemini V2 Bundle 2 R1: real Battle Record UI/controller legal-switch lifecycle.

Exercises the actual production path -- ``BattleRecordUiWindow`` /
``TurnStateFlowController`` -- rather than domain-only calls, per the R1
requirement that the UI lifecycle itself (candidate display, unresolved vs.
confirmed-none visual state, TurnIdentity/revision invalidation, restart
hydration) be proven through the real handlers. Reuses the existing
``test_issue31_turn_state_ui_bundle_c`` fixtures/team constants -- no new
team/session setup is invented here.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_action_result_lifecycle_r2 import (
    _submit_turn_advice as _lifecycle_submit_turn_advice,
)
from test_issue31_turn_state_ui_bundle_c import (
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.domain.turn_state import TurnIdentity
from maple_next.persistence.sqlite import SQLiteRepository


def test_1_confirm_facts_with_legal_switches_unresolved(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None
    assert summary.legal_switch_candidates == ("Gholdengo", "Dragonite")
    assert summary.legal_switch_confirmation is None
    assert summary.provider_ready is False
    assert "LEGAL_SWITCHES_UNRESOLVED" in summary.provider_ready_denial_reasons
    assert window.legal_switch_status_label.text() == "未確認 (UNRESOLVED)"
    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}
    assert transport.call_count == 0
    repository.close()


def test_2_confirm_two_candidates_selected_is_confirmed_nonempty(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    # Direct controller call (never the UI click cascade) proves confirming
    # switches is itself only a factual persistence operation.
    view = controller.confirm_legal_switches(
        legal_switches=("Gholdengo", "Dragonite"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    assert view.error_message is None
    assert transport.call_count == 0

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo", "Dragonite")
    assert summary.provider_ready is True
    repository.close()


def test_3_explicit_confirm_none_is_visually_distinct_from_unresolved(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    unresolved_label = window.legal_switch_status_label.text()

    window._on_confirm_legal_switches_none()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    confirmed_none_label = window.legal_switch_status_label.text()
    assert confirmed_none_label != unresolved_label
    assert "CONFIRMED_NONE" in confirmed_none_label
    assert transport.call_count == 0  # not yet provider-ready overall (no legal move confirmed)
    repository.close()


def test_4_new_turn_identity_invalidates_previous_ui_confirmation(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Gholdengo",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    window.render_view()
    assert "CONFIRMED_NONEMPTY" in window.legal_switch_status_label.text()
    turn1_identity = controller.turn_state_summary().identity
    assert turn1_identity is not None

    # Advance to the next Turn -- a fresh TurnIdentity/binding -- via the
    # same real record-action/next-turn path Bundle 1's own lifecycle tests
    # use (satisfying the legacy MOCK Turn Advice + rich completion
    # prerequisites first).
    _lifecycle_submit_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("選択してください")
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001
    window.render_view()

    turn2_identity = controller.turn_state_summary().identity
    assert turn2_identity is not None
    assert turn2_identity != turn1_identity

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert window.legal_switch_status_label.text() == "未確認 (UNRESOLVED)"
    assert transport.call_count == 0
    repository.close()


def test_5_same_binding_render_preserves_unfinished_selection(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    # Operator selects a candidate but has not yet clicked confirm.
    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(True)

    # An unrelated same-binding re-render (e.g. editing another field) must
    # not discard the unfinished selection.
    window.render_view()

    selected_names = {
        window.legal_switch_list.item(i).text()
        for i in range(window.legal_switch_list.count())
        if window.legal_switch_list.item(i).isSelected()
    }
    assert selected_names == {"Gholdengo"}
    repository.close()


def test_6_restart_same_binding_hydrates_exact_confirmed_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Dragonite",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    db_path = repository.database_path
    repository.close()

    restarted = SQLiteRepository(db_path)
    from maple_next.application.match_service import MatchApplication
    from maple_next.providers.turn_transport import FakeTurnAdviceTransport
    from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
    from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

    class _SyncDispatch:
        def __init__(self, transport, request, config, *, on_succeeded, on_failed):
            self._transport, self._request, self._config = transport, request, config
            self._on_succeeded, self._on_failed = on_succeeded, on_failed

        def start(self) -> None:
            self._on_succeeded(self._transport.send(self._request, self._config))

    restarted_application = MatchApplication(restarted, tmp_path / "restart-export")
    restarted_controller = TurnStateFlowController(
        restarted_application,
        restarted,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        GeminiRichTurnAdviceAdapter(FakeTurnAdviceTransport(), dispatch_factory=_SyncDispatch),
    )
    summary = restarted_controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.legal_switches == ("Dragonite",)
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    restarted.close()


def test_7_restart_stale_binding_is_unresolved(tmp_path: Path) -> None:
    """A restart whose reopened session no longer matches the persisted
    confirmation's binding (here: a different session entirely, standing in
    for "the persisted confirmation's revision/state predates what's now
    current") must show unresolved -- never resurrect a stale confirmation."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Gholdengo",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    db_path = repository.database_path
    repository.close()

    restarted = SQLiteRepository(db_path)
    stale_lookup = restarted.get_legal_switch_confirmation(
        identity=TurnIdentity(
            session_id="a-different-session",
            match_id="a-different-match",
            generation=1,
            turn_id="turn-x",
            turn_number=1,
            battle_revision=1,
        ),
        based_on_confirmed_state_id="whatever",
        applied_selection_id="whatever",
    )
    assert stale_lookup is None
    restarted.close()
