"""Issue #31 Bundle C: Battle Record UI focused tests.

Exercises :mod:`maple_next.ui.turn_state_flow` and
:mod:`maple_next.ui.battle_record_ui` -- the first UI wiring of the
previously-unused Bundle A ``ConfirmedTurnState``/``ActionResultDelta``/
``NextTurnStateDraft`` domain model and the Bundle B rich-state provider
path. No real UGREEN/OBS/Gemini network access anywhere in this file --
only the fake/injected transport and a same-thread dispatch double.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.turn_state import ChangeObservation, KnowledgeStatus
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import ProviderTransportError, SanitizedProviderResult
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
)
from maple_next.ui.battle_record_ui import BattleRecordUiWindow, _DeltaIntField, _KnownIntField
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = (SELF_TEAM[0], SELF_TEAM[1], SELF_TEAM[2])


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class SyncDispatch:
    """Same-thread stand-in for ``TurnAdviceDispatch`` (mirrors existing tests)."""

    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self._transport = transport
        self._request = request
        self._config = config
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed

    def start(self) -> None:
        try:
            result = self._transport.send(self._request, self._config)
        except ProviderTransportError as exc:
            self._on_failed(str(exc))
        else:
            self._on_succeeded(result)


_BuiltWindow = tuple[
    SQLiteRepository, TurnStateFlowController, BattleRecordUiWindow, FakeTurnAdviceTransport
]


def build_window(tmp_path: Path) -> _BuiltWindow:
    qt_application()
    repository = SQLiteRepository(tmp_path / "bundle_c.db")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = FakeTurnAdviceTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(transport, dispatch_factory=SyncDispatch)
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        rich_adapter,
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir)
    return repository, controller, window, transport


def _advance_to_turn_capture_pending(controller: TurnStateFlowController) -> None:
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELECTED_THREE), SELECTED_THREE[0])
    controller.apply_selection(list(SELECTED_THREE), SELECTED_THREE[0], human_confirmed=True)
    controller.start_turn_capture()


def _fill_minimal_current_state(window: BattleRecordUiWindow) -> None:
    window.self_active_box.setCurrentText(SELECTED_THREE[0])
    window.opponent_active_input.setText(OPPONENT_TEAM[0])
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText("Flower Trick")
    window.move_inputs[1].setText("Knock Off")
    window.switch_checkboxes[1].setChecked(True)
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")


# --- window construction / layout ------------------------------------------


def test_window_constructs_with_fixed_header_body_and_bottom_bar(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    assert window.header_tabs.count() == 2
    assert window.header_tabs.tabText(1) == "バトルレコード"
    assert window.start_turn_button.text() == "Turn撮影"
    assert window.confirm_turn_facts_button.text() == "facts/state確定"
    assert window.record_action_button.text() == "行動・結果記録"
    assert window.next_turn_button.text() == "NEXT TURN"
    assert window.diagnostics_drawer is not None
    assert window.terminal_flow_drawer is not None
    repository.close()


def test_diagnostics_drawer_starts_collapsed(tmp_path: Path) -> None:
    repository, _controller, window, _transport = build_window(tmp_path)
    assert window.diagnostics_drawer.content.isHidden() is True
    window.diagnostics_drawer.toggle_button.setChecked(True)
    assert window.diagnostics_drawer.content.isHidden() is False
    repository.close()


# --- UNKNOWN/NONE semantics --------------------------------------------------


def test_known_int_field_defaults_unknown_not_zero() -> None:
    field = _KnownIntField()
    known = field.to_known()
    assert known.status is KnowledgeStatus.UNKNOWN
    assert known.value is None


def test_known_int_field_confirmed_zero_is_distinct_from_unknown() -> None:
    field = _KnownIntField()
    field.unknown_box.setChecked(False)
    field.spin.setValue(0)
    known = field.to_known()
    assert known.status is KnowledgeStatus.CONFIRMED
    assert known.value == 0


def test_delta_field_defaults_unknown_never_unchanged() -> None:
    field = _DeltaIntField()
    delta = field.to_delta()
    assert delta.observation is ChangeObservation.UNKNOWN
    assert delta.after_value is None


def test_delta_field_changed_unchanged_unknown_are_distinct() -> None:
    field = _DeltaIntField()
    field.mode_box.setCurrentText("CHANGED")
    field.spin.setValue(2)
    changed = field.to_delta()
    field.mode_box.setCurrentText("UNCHANGED")
    unchanged = field.to_delta()
    field.mode_box.setCurrentText("UNKNOWN")
    unknown = field.to_delta()
    assert changed.observation is ChangeObservation.CHANGED
    assert changed.after_value == 2
    assert unchanged.observation is ChangeObservation.UNCHANGED
    assert unchanged.after_value is None
    assert unknown.observation is ChangeObservation.UNKNOWN
    assert unknown.after_value is None


# --- confirm current state + legal actions (facts/state確定) ---------------


def test_confirm_current_state_persists_confirmed_turn_state_and_legal_actions(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    window._on_confirm_turn_facts()

    view = controller.refresh()
    assert view.projection.session_state == "TURN_REVIEWED"
    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None
    assert summary.confirmed_state.self_side.active.value == SELECTED_THREE[0]
    assert summary.confirmed_state.self_side.status.value == "NONE"
    # legal moves + one legal switch were confirmed as ConfirmedLegalActionSelection
    assert len(summary.confirmed_legal_actions) == 3
    repository.close()


def test_prefill_is_never_auto_confirmed(tmp_path: Path) -> None:
    """Typing into move/switch inputs alone must not create a confirmed selection."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window.move_inputs[0].setText("Flower Trick")
    window.switch_checkboxes[1].setChecked(True)

    summary = controller.turn_state_summary()
    assert summary.confirmed_legal_actions == ()
    repository.close()


def test_provider_ready_only_after_confirmation(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    assert controller.turn_state_summary().provider_ready is False

    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()

    assert controller.turn_state_summary().provider_ready is True
    repository.close()


# --- rich-state Gemini send: trusted-human-click-only, one attempt ----------


def test_rich_gemini_send_requires_confirmed_legal_action_not_prefill(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()

    transport.responses.append(
        SanitizedProviderResult(
            payload={
                "recommended_action": {
                    "action_id": "not-a-real-confirmation-id",
                    "action_type": "MOVE",
                    "action_name": "Flower Trick",
                },
                "reasons": ["x"],
                "warnings": [],
                "opponent_prediction": {
                    "category": "UNKNOWN",
                    "predicted_action": "?",
                    "summary": "?",
                    "confidence": 0.5,
                },
            },
            source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
            model=FAKE_TURN_MODEL,
        )
    )
    view = controller.send_rich_turn_advice_to_gemini(
        action_type="MOVE",
        action_name="Not A Legal Move",
        opponent_prediction="pred",
        rationale="reason",
        warnings=(),
        on_result=lambda _v: None,
    )
    assert view.error_message is not None
    assert transport.call_count == 0
    repository.close()


def test_rich_gemini_send_applies_and_populates_turn_advice(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("opponent switches")
    window.mock_turn_rationale_input.setText("best coverage")
    window._on_trusted_send_turn_to_gemini()

    view = controller.refresh()
    assert view.error_message is None
    assert view.turn_advice is not None
    assert view.turn_advice.action_name == "Flower Trick"
    assert view.turn_advice.source_type == "GEMINI"
    assert transport.call_count == 1
    repository.close()


def test_duplicate_gemini_activation_while_in_flight_is_blocked(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()

    rich_adapter = controller._rich_turn_gemini_adapter  # noqa: SLF001
    assert rich_adapter is not None
    rich_adapter._in_flight = True  # noqa: SLF001 - simulate a still-pending send

    failures = []
    rich_adapter.send(
        controller._application,  # noqa: SLF001
        on_applied=lambda _d: None,
        on_failed=failures.append,
    )
    assert failures == ["GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT"]
    assert transport.call_count == 0
    repository.close()


# --- action + result delta, NEXT TURN, draft lifecycle ----------------------


def test_record_action_persists_delta_and_next_turn_advances_with_durable_identity_only(
    tmp_path: Path,
) -> None:
    """NEXT TURN always advances the legacy Turn using only durable identity.

    DESIGN_CONFLICT (00 comment 5217523903): draft auto-derivation is a
    documented no-op under the current legacy bump pattern -- see the
    docstring on ``TurnStateFlowController.next_turn``. This test proves
    the *durable-identity-only* behavior: no draft is fabricated with an
    invented revision, NEXT TURN itself still succeeds, and the confirmed
    state correctly becomes non-provider-ready (IDENTITY_MISMATCH) once the
    real session identity has moved on.
    """

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()

    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.self_delta_editor.hp_field.mode_box.setCurrentText("CHANGED")
    window.self_delta_editor.hp_field.value_box.setCurrentText("81-90")
    window.opponent_delta_editor.hp_field.mode_box.setCurrentText("UNCHANGED")
    window._on_record_action()

    recorded_view = controller.refresh()
    assert recorded_view.projection.session_state == "TURN_RECORDED"
    summary = controller.turn_state_summary()
    assert summary.latest_delta is not None
    assert summary.latest_delta.self_side.hp_bucket.observation is ChangeObservation.CHANGED

    window._on_next_turn()
    next_view = controller.refresh()
    # NEXT TURN itself (the legacy state-machine transition) still succeeds
    # even though the Bundle A draft could not be derived durably.
    assert next_view.projection.session_state == "TURN_CAPTURE_PENDING"
    assert next_view.error_message is None

    summary_after_next = controller.turn_state_summary()
    # No draft is fabricated with an invented revision -- fail closed, not
    # silently "working" with a fake identity.
    assert summary_after_next.open_draft is None
    # The old ConfirmedTurnState[T] is still the latest row for this
    # session, but it is now bound to a superseded (T, not T+1) identity.
    assert summary_after_next.confirmed_state is not None
    assert summary_after_next.confirmed_state.identity != summary_after_next.identity
    assert summary_after_next.provider_ready is False
    assert "IDENTITY_MISMATCH" in summary_after_next.provider_ready_denial_reasons
    repository.close()


def test_current_state_editor_shows_no_draft_carry_forward_after_next_turn(
    tmp_path: Path,
) -> None:
    """Companion to the DESIGN_CONFLICT test above: the draft banner/carry-
    forward prefill never appears, because no draft was ever persisted --
    the panel must not fabricate a "draft exists" UI state either.
    """

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()
    window._on_next_turn()

    window.render_view()
    summary = controller.turn_state_summary()
    assert summary.open_draft is None
    assert window.current_state_draft_label.isHidden() is True
    # Session is back to needing a fresh, fully human-entered current state
    # for the new Turn -- not provider-ready until re-confirmed.
    assert summary.confirmed_state is not None
    assert summary.confirmed_state.identity != summary.identity
    assert summary.provider_ready is False
    repository.close()


# --- restart hydration -------------------------------------------------------


def test_restart_hydration_reloads_confirmed_state_and_legal_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "hydrate.db"
    repository = SQLiteRepository(db_path)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = FakeTurnAdviceTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(transport, dispatch_factory=SyncDispatch)
    controller = TurnStateFlowController(
        application, repository, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter(),
        None, None, rich_adapter,
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    qt_application()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    before = controller.turn_state_summary()
    assert before.confirmed_state is not None
    repository.close()

    restarted_repository = SQLiteRepository(db_path)
    restarted_application = MatchApplication(restarted_repository, export_dir)
    restarted_transport = FakeTurnAdviceTransport()
    restarted_rich_adapter = GeminiRichTurnAdviceAdapter(
        restarted_transport, dispatch_factory=SyncDispatch
    )
    restarted_controller = TurnStateFlowController(
        restarted_application,
        restarted_repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        restarted_rich_adapter,
    )
    restarted_window = BattleRecordUiWindow(restarted_controller, ocr_data_directory=ocr_dir)
    restarted_window.render_view()
    after = restarted_controller.turn_state_summary()
    assert after.confirmed_state is not None
    assert after.confirmed_state.confirmed_state_id == before.confirmed_state.confirmed_state_id
    assert len(after.confirmed_legal_actions) == len(before.confirmed_legal_actions)
    restarted_repository.close()


# --- stale/foreign identity fail-closed -------------------------------------


def test_stale_confirmed_state_is_not_provider_ready(tmp_path: Path) -> None:
    """A confirmed state persisted for an older identity must fail the gate."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()
    first_summary = controller.turn_state_summary()
    assert first_summary.provider_ready is True

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()

    # Advance the underlying Turn without going through the rich-state flow
    # again (simulating a foreign/older confirmed state being current).
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()
    controller.next_turn()

    stale_summary = controller.turn_state_summary()
    # The old ConfirmedTurnState is now bound to a superseded Turn identity;
    # the gate must fail closed rather than treat it as still current.
    assert stale_summary.confirmed_state is not None
    assert stale_summary.confirmed_state.identity != stale_summary.identity
    assert stale_summary.provider_ready is False
    assert "IDENTITY_MISMATCH" in stale_summary.provider_ready_denial_reasons
    repository.close()
