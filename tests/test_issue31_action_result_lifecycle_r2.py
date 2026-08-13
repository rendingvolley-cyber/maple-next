"""R2 remediation: Action Result -> next-turn state lifecycle.

Historical real-match evidence: a human-confirmed Swords Dance on Turn 3
(self Attack stage CHANGED 0 -> +2) did not become Attack +2 in Turn 4. It
remained Attack 0 through Turn 4/5 and appeared late. Root cause was in the
UI/application capture/persistence lifecycle, not the domain projection
function:

- the result-delta editor widgets (``self_delta_editor`` /
  ``opponent_delta_editor`` / weather / terrain) were never reset per
  ``TurnIdentity``, so a CHANGED value could survive across Turns in the
  live Qt widgets;
- :meth:`TurnStateFlowController.next_turn` always advanced the legacy
  Turn even when no ``ActionResultDelta`` existed for the current
  confirmed state, silently skipping draft derivation instead of failing
  closed;
- the existing atomic ``record_rich_action_completion`` repository
  primitive (persistence/sqlite.py) was never wired into the normal
  Battle Record path.

This file exercises the fix for all three, plus the required A-G
regression set, at the real ``BattleRecordUiWindow``/
``TurnStateFlowController`` level -- no real UGREEN/OBS/Gemini network
access anywhere; only the fake/injected transport and a same-thread
dispatch double, mirroring the existing Bundle C test file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.turn_state import FieldDelta, ProvenanceStep, SideDelta
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import ProviderTransportError
from maple_next.providers.turn_transport import FakeTurnAdviceTransport
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
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


def build_window(tmp_path: Path, *, name: str = "lifecycle.db") -> _BuiltWindow:
    qt_application()
    repository = SQLiteRepository(tmp_path / name)
    export_dir = tmp_path / f"{name}-export"
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
    ocr_dir = tmp_path / f"{name}-ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir, auto_start_capture=False)
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
    window.move_inputs[1].setText("Swords Dance")
    window.switch_checkboxes[1].setChecked(True)
    window.self_state_editor.status_field.unknown_box.setChecked(False)
    window.self_state_editor.status_field.line.setText("NONE")
    window.opponent_state_editor.status_field.unknown_box.setChecked(False)
    window.opponent_state_editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")


def _send_mock_turn_advice(window: BattleRecordUiWindow) -> None:
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001


def _confirm_and_send(window: BattleRecordUiWindow) -> None:
    """Confirm the (already-hydrated) current-state editor and clear the
    provider-ready gate for RECORD_ACTUAL_ACTION, without touching move/
    switch/status/weather -- those were already re-filled by the caller."""

    window._on_confirm_turn_facts()  # noqa: SLF001
    _send_mock_turn_advice(window)


def _record_action(
    window: BattleRecordUiWindow, *, action_name: str = "Flower Trick"
) -> None:
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText(action_name)
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001


# --- section 6: historical regression ---------------------------------------


def test_historical_swords_dance_turn3_attack_plus2_projects_to_turn4_and_editors_reset(
    tmp_path: Path,
) -> None:
    """Focused synthetic reproduction of the accepted real-match defect.

    T3: self active is confirmed, Attack stage 0, self confirms つるぎのまい
    and the human-confirmed result is self Attack stage CHANGED to +2.
    Expected: T4's draft/current state is Attack +2 (never 0), the T4 and
    T5 result editors both start fresh/UNCHANGED, and no stale CHANGED:+2
    leaks through T5/T6/T7 even though the operator never re-enters it.
    """

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    # -- Turn 1, Turn 2: uneventful turns to reach a literal Turn 3. -------
    for _ in range(2):
        _fill_minimal_current_state(window)
        _confirm_and_send(window)
        _record_action(window)
        window._on_next_turn()  # noqa: SLF001
        window.render_view()

    turn3_identity = controller.turn_state_summary().identity
    assert turn3_identity is not None
    assert turn3_identity.turn_number == 3

    # -- Turn 3: confirm facts (Attack stage defaults to 0 for a fresh
    # identity with no open draft), then confirm つるぎのまい's result: self
    # Attack stage CHANGED to +2.
    _fill_minimal_current_state(window)
    assert (
        window.self_state_editor.stage_fields["attack_stage"].to_known().value == 0
    )
    _confirm_and_send(window)

    window.self_delta_editor.stage_fields["attack_stage"].spin.setValue(2)
    assert (
        window.self_delta_editor.stage_fields["attack_stage"].mode_box.currentText()
        == "CHANGED"
    )
    _record_action(window, action_name="Swords Dance")
    summary_t3 = controller.turn_state_summary()
    assert summary_t3.latest_delta is not None
    assert summary_t3.latest_delta.self_side.attack_stage.after_value == 2

    window._on_next_turn()  # noqa: SLF001
    window.render_view()

    # -- Turn 4: draft AND the hydrated current-state editor must already
    # carry Attack +2 forward -- never 0 -- and the result-delta editor for
    # this brand-new identity must start completely fresh.
    turn4_summary = controller.turn_state_summary()
    assert turn4_summary.identity is not None
    assert turn4_summary.identity.turn_number == 4
    assert turn4_summary.open_draft is not None
    assert turn4_summary.open_draft.self_side.attack_stage.value == 2
    assert (
        window.self_state_editor.stage_fields["attack_stage"].to_known().value == 2
    )
    assert (
        window.self_delta_editor.stage_fields["attack_stage"].mode_box.currentText()
        == "UNCHANGED"
    )
    assert window.self_delta_editor.stage_fields["attack_stage"].spin.value() == 0

    # Confirm Turn 4 facts (carries the hydrated Attack +2 into the new
    # ConfirmedTurnState) and record an action with no further stage
    # change -- the fresh editor must submit UNCHANGED, not a phantom
    # repeat of +2.
    _fill_minimal_current_state(window)
    _confirm_and_send(window)
    confirmed_t4 = controller.turn_state_summary().confirmed_state
    assert confirmed_t4 is not None
    assert confirmed_t4.identity.turn_number == 4
    assert confirmed_t4.self_side.attack_stage.value == 2

    _record_action(window)
    delta_t4 = controller.turn_state_summary().latest_delta
    assert delta_t4 is not None
    assert delta_t4.self_side.attack_stage.observation.name == "UNCHANGED"

    window._on_next_turn()  # noqa: SLF001
    window.render_view()

    # -- Turn 5, 6, 7: PROVIDER-READY confirmed state keeps Attack +2 (never
    # reverts to 0) purely through the persisted domain projection, and the
    # result editor keeps starting fresh every single Turn with zero
    # operator intervention -- no stale CHANGED:+2 ever leaks back in.
    for expected_turn_number in (5, 6, 7):
        turn_summary = controller.turn_state_summary()
        assert turn_summary.identity is not None
        assert turn_summary.identity.turn_number == expected_turn_number
        assert (
            window.self_delta_editor.stage_fields["attack_stage"].mode_box.currentText()
            == "UNCHANGED"
        )
        assert window.self_delta_editor.stage_fields["attack_stage"].spin.value() == 0

        _fill_minimal_current_state(window)
        window._on_confirm_turn_facts()  # noqa: SLF001
        confirmed = controller.turn_state_summary().confirmed_state
        assert confirmed is not None
        assert confirmed.identity.turn_number == expected_turn_number
        assert confirmed.self_side.attack_stage.value == 2
        # provider-ready right after confirming, before the one-attempt
        # advice send moves battle_revision forward again.
        assert controller.turn_state_summary().provider_ready is True
        _send_mock_turn_advice(window)

        _record_action(window)
        window._on_next_turn()  # noqa: SLF001
        window.render_view()

    repository.close()


# --- section 7A: HP result carries into the next Turn's draft ---------------


def test_hp_result_in_turn_n_appears_in_turn_n_plus_1_draft(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    window.self_delta_editor.hp_field.mode_box.setCurrentText("CHANGED")
    window.self_delta_editor.hp_field.value_box.setCurrentText("41-50")
    _record_action(window)
    window._on_next_turn()  # noqa: SLF001

    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.hp_bucket.value.value == "41-50"
    repository.close()


# --- section 7B: confirmed active-identity change carries forward ----------


def test_confirmed_active_identity_change_in_turn_n_appears_in_turn_n_plus_1_draft(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    window.actual_action_type_box.setCurrentText("SWITCH")
    window.actual_action_name_box.setCurrentText(SELECTED_THREE[1])
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001

    delta = controller.turn_state_summary().latest_delta
    assert delta is not None
    assert delta.self_side.active.after_value == SELECTED_THREE[1]

    window._on_next_turn()  # noqa: SLF001
    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.self_side.active.value == SELECTED_THREE[1]
    repository.close()


# --- section 7C: status/side-effect result projects to the next Turn -------


def test_status_result_projects_to_the_immediately_following_turn(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    window.opponent_delta_editor.status_field.mode_box.setCurrentText("CHANGED")
    window.opponent_delta_editor.status_field.line.setText("burn")
    _record_action(window)
    window._on_next_turn()  # noqa: SLF001

    draft = controller.turn_state_summary().open_draft
    assert draft is not None
    assert draft.opponent_side.status.value == "burn"
    repository.close()


# --- section 7D: failed atomic persistence leaves no partial completion ----


def test_failed_delta_construction_leaves_legacy_action_unrecorded(tmp_path: Path) -> None:
    """A delta that fails to construct must never leave a dangling legacy
    action record behind -- the reordering fix (delta built *before* the
    legacy write) closes exactly this gap."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    identity = controller.turn_state_summary().identity
    assert identity is not None

    unchanged_side = SideDelta(
        active=FieldDelta.unchanged(),
        hp_bucket=FieldDelta.unchanged(),
        status=FieldDelta.unchanged(),
        attack_stage=FieldDelta.unchanged(),
        defense_stage=FieldDelta.unchanged(),
        special_attack_stage=FieldDelta.unchanged(),
        special_defense_stage=FieldDelta.unchanged(),
        speed_stage=FieldDelta.unchanged(),
        accuracy_stage=FieldDelta.unchanged(),
        evasion_stage=FieldDelta.unchanged(),
        side_effects=FieldDelta.unchanged(),
    )
    # ``FieldDelta.changed("", ...)`` constructs fine on its own (a
    # CHANGED delta only requires a non-None value) -- it can only fail
    # once embedded in ``ActionResultDelta``, whose own validation rejects
    # blank CHANGED text. This exercises exactly the reordering fix: the
    # delta must be validated *before* the legacy action write, so this
    # failure must never leave a recorded action with no matching delta.
    invalid_weather_delta = FieldDelta.changed("", provenance_chain=(ProvenanceStep.HUMAN_INPUT,))
    view = controller.record_actual_action(
        action_type="MOVE",
        action_name="Flower Trick",
        human_confirmed=True,
        self_side_delta=unchanged_side,
        opponent_side_delta=unchanged_side,
        weather_delta=invalid_weather_delta,
        terrain_delta=FieldDelta.unchanged(),
    )
    assert view.error_message is not None
    assert not repository.has_recorded_action(identity.turn_id)
    assert view.projection.session_state != "TURN_RECORDED"
    repository.close()


def test_next_turn_refuses_to_advance_when_confirmed_state_has_no_delta(
    tmp_path: Path,
) -> None:
    """If a ConfirmedTurnState exists but its ActionResultDelta was never
    durably recorded (e.g. a caller used the legacy-only record path mid
    rich-state match), NEXT TURN must fail closed rather than silently
    advance without deriving a draft."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    # Legacy-only record: omits the four delta kwargs entirely. This
    # itself legitimately bumps battle_revision (recording the action is
    # not the bug); the identity snapshot for this test is taken *after*
    # it, right before the refused ``next_turn()`` call.
    controller.record_actual_action(
        action_type="MOVE",
        action_name="Flower Trick",
        human_confirmed=True,
    )
    assert controller.turn_state_summary().latest_delta is None
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None

    view = controller.next_turn()
    assert view.error_message is not None

    identity_after = controller.turn_state_summary().identity
    assert identity_after == identity_before
    assert controller.turn_state_summary().open_draft is None
    repository.close()


# --- section 7E: restart hydration equals uninterrupted next-turn draft ----


def test_restart_hydration_after_historical_scenario_equals_uninterrupted_draft(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "hydrate_r2.db"
    repository = SQLiteRepository(db_path)
    export_dir = tmp_path / "hydrate_r2-export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = FakeTurnAdviceTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(transport, dispatch_factory=SyncDispatch)
    controller = TurnStateFlowController(
        application, repository, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter(),
        None, None, rich_adapter,
    )
    ocr_dir = tmp_path / "hydrate_r2-ocr"
    ocr_dir.mkdir()
    qt_application()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir, auto_start_capture=False)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)
    window.self_delta_editor.stage_fields["attack_stage"].spin.setValue(2)
    _record_action(window, action_name="Swords Dance")
    window._on_next_turn()  # noqa: SLF001

    before = controller.turn_state_summary()
    assert before.open_draft is not None
    assert before.open_draft.self_side.attack_stage.value == 2
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
    restarted_window = BattleRecordUiWindow(
        restarted_controller, ocr_data_directory=ocr_dir, auto_start_capture=False
    )
    restarted_window.render_view()

    after = restarted_controller.turn_state_summary()
    assert after.open_draft is not None
    assert after.open_draft.draft_id == before_draft_id
    assert after.open_draft.identity == before_identity
    assert after.open_draft.self_side.attack_stage.value == 2
    assert restarted_window.self_state_editor.stage_fields["attack_stage"].to_known().value == 2
    # No Qt widget carried this across the restart -- it was re-derived
    # purely from the persisted ConfirmedTurnState + ActionResultDelta.
    assert (
        restarted_window.self_delta_editor.stage_fields["attack_stage"].mode_box.currentText()
        == "UNCHANGED"
    )
    restarted_repository.close()


# --- section 7F/G: provider dispatch gate + zero dispatch count ------------


def test_provider_dispatch_rejects_stale_and_wrong_identity_and_never_dispatches(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    assert controller.turn_state_summary().provider_ready is True
    _send_mock_turn_advice(window)
    rich_adapter = controller._rich_turn_gemini_adapter  # noqa: SLF001
    assert rich_adapter is not None
    call_count_after_legitimate_send = transport.call_count
    dispatch_count_after_legitimate_send = rich_adapter.dispatch_count
    assert call_count_after_legitimate_send == 1

    # Advance the underlying Turn *without* re-confirming rich facts for
    # the new identity -- the previous ConfirmedTurnState is now stale/
    # foreign relative to the current TurnIdentity.
    _record_action(window)
    controller.next_turn()

    stale_summary = controller.turn_state_summary()
    assert stale_summary.confirmed_state is not None
    assert stale_summary.confirmed_state.identity != stale_summary.identity
    assert stale_summary.provider_ready is False
    assert "IDENTITY_MISMATCH" in stale_summary.provider_ready_denial_reasons

    result = window._bundle_c_controller.send_rich_turn_advice_to_gemini(  # noqa: SLF001
        action_type="MOVE",
        action_name="Flower Trick",
        opponent_prediction="pred",
        rationale="reason",
        warnings=(),
        on_result=lambda _v: None,
    )
    assert result.error_message is not None
    # The gate rejection happened before any provider dispatch was ever
    # attempted for the stale state -- no additional call/dispatch beyond
    # the one legitimate send above.
    assert transport.call_count == call_count_after_legitimate_send
    assert rich_adapter.dispatch_count == dispatch_count_after_legitimate_send
    repository.close()
