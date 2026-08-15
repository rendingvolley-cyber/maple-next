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

from maple_next.domain.legal_switches import LegalSwitchStatus as _B2_LegalSwitchStatus

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import (
    DeviceOpenResult,
    SourceFramePacket,
    VideoCaptureBackend,
)
from maple_next.domain.turn_state import ChangeObservation, KnowledgeStatus
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.providers.turn_advice_rich_state import RichStateTurnAdviceRequest
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
)
from maple_next.ui.battle_record_ui import BattleRecordUiWindow, _DeltaIntField, _KnownIntField
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController


def _confirm_legal_switches_honestly(window) -> None:
    """Bundle 2 R1-F: an honest fixture default for tests that do not
    themselves exercise legal-switch behavior. Reviews the *real* derived
    candidates for the current binding and confirms exactly that set --
    never a fabricated CONFIRMED_NONE used only to clear the gate. When the
    team fixture genuinely has no legal switch candidates, this still
    produces CONFIRMED_NONE, but because it is actually empty."""

    controller = window._bundle_c_controller  # noqa: SLF001
    candidates = controller.derive_legal_switch_candidates()
    status = (
        _B2_LegalSwitchStatus.CONFIRMED_NONEMPTY
        if candidates
        else _B2_LegalSwitchStatus.CONFIRMED_NONE
    )
    controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=candidates, status=status, human_confirmed=True
    )


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


class ProductionCompatibleRichTransport:
    """Non-fake transport double; returns a request-bound sanitized result."""

    def __init__(self) -> None:
        self.call_count = 0

    def send(
        self, request: RichStateTurnAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        del config
        self.call_count += 1
        action = request.legal_actions[0]
        return SanitizedProviderResult(
            payload={
                "recommended_action": {
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                    "action_name": action.action_name,
                },
                "reasons": ["bounded non-network transport double"],
                "warnings": [],
                "opponent_prediction": {
                    "category": "UNKNOWN",
                    "predicted_action": "UNKNOWN",
                    "summary": "UNKNOWN",
                    "confidence": 0.0,
                },
            },
            source_type="GEMINI",
            model="bounded-test-model",
        )


class CountingCaptureBackend:
    def __init__(self) -> None:
        self.start_count = 0

    def start(self, selector: str, on_frame=None) -> DeviceOpenResult:
        del selector, on_frame
        self.start_count += 1
        return DeviceOpenResult(False, False, None, "CAPTURE_DEVICE_UNAVAILABLE")

    def stop(self) -> None:
        return None

    def get_latest_frame(self) -> SourceFramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


_BuiltWindow = tuple[
    SQLiteRepository, TurnStateFlowController, BattleRecordUiWindow, FakeTurnAdviceTransport
]


def build_window(
    tmp_path: Path,
    *,
    capture_backend: VideoCaptureBackend | None = None,
    auto_start_capture: bool = True,
) -> _BuiltWindow:
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
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        capture_backend=capture_backend,
        auto_start_capture=auto_start_capture,
    )
    return repository, controller, window, transport


def build_production_compatible_window(
    tmp_path: Path,
) -> tuple[
    SQLiteRepository,
    TurnStateFlowController,
    BattleRecordUiWindow,
    ProductionCompatibleRichTransport,
    GeminiRichTurnAdviceAdapter,
]:
    qt_application()
    repository = SQLiteRepository(tmp_path / "production-compatible.db")
    export_dir = tmp_path / "production-export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = ProductionCompatibleRichTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(
        transport,
        lambda: ProviderConfig(
            api_key="bounded-test-key",
            model="bounded-test-model",
            timeout_seconds=5.0,
        ),
        dispatch_factory=SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        rich_adapter,
    )
    ocr_dir = tmp_path / "production-ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    return repository, controller, window, transport, rich_adapter


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
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"
    assert window.record_action_button.text() == "行動・結果記録"
    assert window.next_turn_button.text() == "NEXT TURN"
    assert window.diagnostics_drawer is not None
    assert window.terminal_flow_drawer is not None
    repository.close()


def test_visible_v5_send_dispatches_production_compatible_transport_once(
    tmp_path: Path,
) -> None:
    """Bundle 2 (Gemini V2) R2: three distinct operator actions, none of
    which dispatch except the last.

    STEP 1 -- confirm Turn facts: persists the canonical confirmed state;
    legal switches remain NOT_CAPTURED_OR_UNRESOLVED; zero dispatch.

    STEP 2 -- explicitly confirm legal switches: persists the confirmation
    (never an automatic promotion of the derived candidate list);
    provider-ready becomes true; still zero dispatch -- legal-switch
    confirmation is purely a factual persistence operation and never itself
    reaches a transport.

    STEP 3 -- the existing, separate explicit Gemini-send control (the
    pre-existing ``TrustedSendButton`` this test's production-compatible
    transport double exists to prove is wired correctly -- v5 previously
    hid it because confirming facts used to double as sending): only this
    distinct action reaches the transport, exactly once. Its ``clicked``
    signal is deliberately inert for synthetic events (see
    ``TrustedSendButton``/``test_trusted_send_gate.py``), so the handler is
    invoked directly here, exactly as this suite's other trusted-button
    tests already do for the legacy send action.
    """

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    # -- STEP 1: confirm Turn facts. --
    assert not window.confirm_turn_facts_button.isHidden()
    assert window.confirm_turn_facts_button.isEnabled()
    window.confirm_turn_facts_button.click()

    assert transport.call_count == 0
    assert adapter.dispatch_count == 0
    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None
    assert summary.legal_switch_confirmation is None
    assert summary.legal_switch_candidates == ("Gholdengo", "Dragonite")
    assert summary.provider_ready is False
    assert "LEGAL_SWITCHES_UNRESOLVED" in summary.provider_ready_denial_reasons

    # -- STEP 2: explicitly confirm both real candidates. --
    for index in range(window.legal_switch_list.count()):
        window.legal_switch_list.item(index).setSelected(True)
    window._on_confirm_legal_switches_selected()  # noqa: SLF001

    assert transport.call_count == 0
    assert adapter.dispatch_count == 0
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo", "Dragonite")
    assert summary.provider_ready is True
    send_button = window._bundle_c_gemini_send_button  # noqa: SLF001
    assert send_button.isEnabled()

    # -- STEP 3: the distinct explicit send action. --
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001

    assert transport.call_count == 1
    assert adapter.dispatch_count == 1

    # -- Re-confirming facts (a fresh binding) again requires a fresh
    # legal-switch confirmation before another send is even possible --
    # never a second dispatch as a side effect of any confirm click. --
    window.confirm_turn_facts_button.click()
    assert transport.call_count == 1
    assert adapter.dispatch_count == 1
    repository.close()


def test_auto_start_false_keeps_capture_backend_start_count_zero(tmp_path: Path) -> None:
    backend = CountingCaptureBackend()
    repository, _controller, window, _transport = build_window(
        tmp_path,
        capture_backend=backend,
        auto_start_capture=False,
    )
    assert backend.start_count == 0
    window.close()
    assert backend.start_count == 0
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


def test_delta_field_defaults_to_v5_internal_unchanged() -> None:
    field = _DeltaIntField()
    delta = field.to_delta()
    assert delta.observation is ChangeObservation.UNCHANGED
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

    window._on_confirm_turn_facts()  # noqa: SLF001

    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001

        legal_switches=(), status=_B2_LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True

    )

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
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)

    assert controller.turn_state_summary().provider_ready is True
    repository.close()


# --- rich-state Gemini send: trusted-human-click-only, one attempt ----------


def test_rich_gemini_send_requires_confirmed_legal_action_not_prefill(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)

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
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)

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
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)

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


def test_record_action_persists_delta_and_next_turn_derives_durable_draft(
    tmp_path: Path,
) -> None:
    """NEXT TURN derives and persists a real Bundle A draft.

    00 design decision (comment 5217661584, closing the DESIGN_CONFLICT
    from comment 5217523903): battle_revision is a durable global
    mutation-revision counter, so the next-turn rule is "strictly greater
    than previous", not "exactly +1". Using only the real, durable session
    identity (never a fabricated value), draft derivation now succeeds.
    """

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    confirmed_before = controller.turn_state_summary().confirmed_state
    assert confirmed_before is not None

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
    assert next_view.projection.session_state == "TURN_CAPTURE_PENDING"
    assert next_view.error_message is None

    summary_after_next = controller.turn_state_summary()
    draft = summary_after_next.open_draft
    assert draft is not None
    # Durable only: real turn_id/turn_number/battle_revision, strictly
    # greater revision than the confirmed state it derives from -- no
    # fabricated "previous + 1" anywhere.
    assert draft.based_on_confirmed_state_id == confirmed_before.confirmed_state_id
    assert draft.identity.turn_number == confirmed_before.identity.turn_number + 1
    assert draft.identity.battle_revision > confirmed_before.identity.battle_revision
    assert draft.identity == summary_after_next.identity
    assert draft.provider_ready is False
    # The delta's CHANGED self HP carried into the draft's self_side.
    assert draft.self_side.hp_bucket.value.value == "81-90"
    repository.close()


def test_current_state_editor_shows_draft_carry_forward_after_next_turn(
    tmp_path: Path,
) -> None:
    """The draft banner appears and prefills the editor, but the draft is
    never presented as a confirmed current state -- explicit human
    re-confirmation is still required before it becomes provider-ready.
    """

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    # Event-entry UI v3 (5224627634): _SideDeltaEditor no longer has an
    # active-identity input at all -- to_side_delta() always reports
    # active=UNCHANGED, which is exactly what carries the active Pokemon
    # forward into the draft for this non-SWITCH (MOVE) actual action.
    window._on_record_action()
    window._on_next_turn()

    window.render_view()
    summary = controller.turn_state_summary()
    assert summary.open_draft is not None
    assert window.current_state_draft_label.isHidden() is False
    # The draft's carried-forward SELF active/HP were loaded into the
    # editor widgets for human review -- not auto-confirmed.
    assert window.self_active_box.currentText() == SELECTED_THREE[0]
    # No new ConfirmedTurnState for this new identity exists yet, so the
    # panel must not present the draft as confirmed/provider-ready.
    assert summary.confirmed_state is not None
    assert summary.confirmed_state.identity != summary.open_draft.identity
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
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
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


def test_restart_hydration_reloads_the_same_next_turn_state_draft(tmp_path: Path) -> None:
    """A NextTurnStateDraft persisted before restart hydrates identically after."""

    db_path = tmp_path / "hydrate_draft.db"
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
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
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

    before = controller.turn_state_summary()
    assert before.open_draft is not None
    before_draft_id = before.open_draft.draft_id
    before_identity = before.open_draft.identity
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
    assert after.open_draft is not None
    assert after.open_draft.draft_id == before_draft_id
    assert after.open_draft.identity == before_identity
    # The draft banner/carry-forward prefill re-appears identically after
    # a fresh restart, from the same persisted row -- not recomputed.
    assert restarted_window.current_state_draft_label.isHidden() is False
    assert restarted_window.self_active_box.currentText() == SELECTED_THREE[0]
    restarted_repository.close()


# --- stale/foreign identity fail-closed -------------------------------------


def test_stale_confirmed_state_is_not_provider_ready(tmp_path: Path) -> None:
    """A confirmed state persisted for an older identity must fail the gate."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
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
