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

import inspect
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import maple_next.application.service as application_service
from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import ActionOrder, ActionType, BattleState, HpBucket
from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.domain.models import RecordedAction
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedTurnState,
    FieldDelta,
    Known,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
    TurnStateError,
)
from maple_next.domain.turn_state_projection import GateDenialReason, evaluate_provider_ready_gate
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


def _submit_turn_advice(window: BattleRecordUiWindow) -> None:
    """Satisfy the domain's ``current_turn_advice_id`` precondition for
    record_actual_action via the legacy in-process MOCK adapter -- never
    the rich Gemini adapter/FakeTurnAdviceTransport. R1-B: this whole file
    must make zero provider dispatch attempts / transport calls; the one
    dedicated gate test below proves rejection via the pure
    ``evaluate_provider_ready_gate`` function instead of a legitimate send."""

    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("pred")
    window.mock_turn_rationale_input.setText("reason")
    window._on_submit_mock_turn()  # noqa: SLF001


def _confirm_and_send(window: BattleRecordUiWindow) -> None:
    """Confirm the (already-hydrated) current-state editor and clear the
    provider-ready gate for RECORD_ACTUAL_ACTION, without touching move/
    switch/status/weather -- those were already re-filled by the caller."""

    window._on_confirm_turn_facts()  # noqa: SLF001
    # Bundle 2 (Gemini V2): none of this file's fixtures exercise the
    # switch-legality dimension, so an explicit human "no legal switches"
    # confirmation for the current binding is the correct default here --
    # never a silent/implicit one. Every scenario below keeps asserting
    # zero provider dispatch on top of this.
    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
    )
    _submit_turn_advice(window)


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

    repository, controller, window, transport = build_window(tmp_path)
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
        window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
            legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
        )
        confirmed = controller.turn_state_summary().confirmed_state
        assert confirmed is not None
        assert confirmed.identity.turn_number == expected_turn_number
        assert confirmed.self_side.attack_stage.value == 2
        # provider-ready right after confirming, before the one-attempt
        # advice send moves battle_revision forward again.
        assert controller.turn_state_summary().provider_ready is True
        _submit_turn_advice(window)

        _record_action(window)
        window._on_next_turn()  # noqa: SLF001
        window.render_view()

    assert transport.call_count == 0
    repository.close()


# --- section 7A: HP result carries into the next Turn's draft ---------------


def test_hp_result_in_turn_n_appears_in_turn_n_plus_1_draft(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
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
    assert transport.call_count == 0
    repository.close()


# --- section 7B: confirmed active-identity change carries forward ----------


def test_confirmed_active_identity_change_in_turn_n_appears_in_turn_n_plus_1_draft(
    tmp_path: Path,
) -> None:
    repository, controller, window, transport = build_window(tmp_path)
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
    assert transport.call_count == 0
    repository.close()


# --- section 7C: status/side-effect result projects to the next Turn -------


def test_status_result_projects_to_the_immediately_following_turn(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
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
    assert transport.call_count == 0
    repository.close()


# --- section 7D: failed atomic persistence leaves no partial completion ----


def test_failed_delta_construction_leaves_legacy_action_unrecorded(tmp_path: Path) -> None:
    """A delta that fails to construct must never leave a dangling legacy
    action record behind -- the reordering fix (delta built *before* the
    legacy write) closes exactly this gap."""

    repository, controller, window, transport = build_window(tmp_path)
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
    assert transport.call_count == 0
    repository.close()


def test_next_turn_refuses_to_advance_when_confirmed_state_has_no_delta(
    tmp_path: Path,
) -> None:
    """If a ConfirmedTurnState exists but its ActionResultDelta was never
    durably recorded (e.g. a caller used the legacy-only record path mid
    rich-state match), NEXT TURN must fail closed rather than silently
    advance without deriving a draft."""

    repository, controller, window, transport = build_window(tmp_path)
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
    assert transport.call_count == 0
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
    assert transport.call_count == 0
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
    assert restarted_transport.call_count == 0
    restarted_repository.close()


# --- section 7F/G: provider-ready gate + zero dispatch count ---------------


def test_provider_ready_gate_rejects_required_scenarios_without_any_dispatch(
    tmp_path: Path,
) -> None:
    """R1-B: prove the gate rejects each required scenario by calling the
    pure ``evaluate_provider_ready_gate`` function directly -- never by
    issuing a legitimate provider dispatch first. No adapter/transport
    method is ever invoked anywhere in this test."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    # F1: missing next-turn state -- nothing confirmed yet for this Turn.
    missing_summary = controller.turn_state_summary()
    assert missing_summary.confirmed_state is None
    assert missing_summary.provider_ready is False

    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    window._bundle_c_controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=True
    )
    summary = controller.turn_state_summary()
    confirmed_state = summary.confirmed_state
    current_identity = summary.identity
    assert confirmed_state is not None
    assert current_identity is not None
    assert summary.confirmed_legal_actions
    # Sanity: the real, current confirmed state genuinely is ready -- every
    # rejection below comes from deliberately wrong gate *inputs*, not from
    # this fixture being broken.
    assert summary.provider_ready is True

    # F2: stale next-turn state -- a newer/different confirmed state has
    # since superseded this snapshot.
    stale_result = evaluate_provider_ready_gate(
        confirmed_state=confirmed_state,
        confirmed_legal_actions=summary.confirmed_legal_actions,
        current_identity=current_identity,
        latest_confirmed_state_id="some-other-confirmed-state-id",
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=None,
    )
    assert stale_result.allowed is False
    assert GateDenialReason.STALE_CONFIRMED_STATE in stale_result.denial_reasons

    # F3: unconfirmed next-turn state -- a newer OPEN draft already exists,
    # so this older confirmed state must not be treated as ready.
    unconfirmed_result = evaluate_provider_ready_gate(
        confirmed_state=confirmed_state,
        confirmed_legal_actions=summary.confirmed_legal_actions,
        current_identity=current_identity,
        latest_confirmed_state_id=confirmed_state.confirmed_state_id,
        latest_open_draft_turn_number=current_identity.turn_number + 1,
        latest_open_draft_battle_revision=current_identity.battle_revision + 1,
        legal_switch_confirmation=None,
    )
    assert unconfirmed_result.allowed is False
    assert GateDenialReason.NEWER_OPEN_DRAFT_EXISTS in unconfirmed_result.denial_reasons

    # F4: wrong TurnIdentity/revision -- the confirmed state's own identity
    # no longer matches the caller's claimed "current" identity.
    wrong_identity = replace(
        current_identity, battle_revision=current_identity.battle_revision + 5
    )
    wrong_identity_result = evaluate_provider_ready_gate(
        confirmed_state=confirmed_state,
        confirmed_legal_actions=summary.confirmed_legal_actions,
        current_identity=wrong_identity,
        latest_confirmed_state_id=confirmed_state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=None,
    )
    assert wrong_identity_result.allowed is False
    assert GateDenialReason.IDENTITY_MISMATCH in wrong_identity_result.denial_reasons

    # G: zero provider dispatch attempts / transport calls anywhere above.
    rich_adapter = controller._rich_turn_gemini_adapter  # noqa: SLF001
    assert rich_adapter is not None
    assert rich_adapter.dispatch_count == 0
    assert transport.call_count == 0
    repository.close()


# --- R1-A: UNKNOWN opponent action / action order still atomic -------------
#
# The historical Bundle 1 contract requires that a real Turn with a
# confirmed self action, an opponent action that is genuinely UNKNOWN, an
# UNKNOWN action order, and a confirmed ActionResultDelta all commit
# through the *same* single canonical atomic completion boundary as a
# fully-known Turn -- never a second, non-atomic path taken merely because
# the opponent's action was not observed.

_R1A_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
_R1A_CONFIRMED_AT = "2026-08-13T00:00:00+00:00"


def _r1a_identity(*, turn_id: str = "r1a-turn-1") -> TurnIdentity:
    return TurnIdentity(
        session_id="r1a-session",
        match_id="r1a-match",
        generation=9,
        turn_id=turn_id,
        turn_number=1,
        battle_revision=0,
    )


def _r1a_side_state(*, active: str) -> SideState:
    return SideState(
        active=Known.confirmed(active, provenance_chain=_R1A_HUMAN),
        hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_R1A_HUMAN),
        status=Known.confirmed("NONE", provenance_chain=_R1A_HUMAN),
        attack_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        defense_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        special_attack_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        special_defense_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        speed_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        accuracy_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        evasion_stage=Known.confirmed(0, provenance_chain=_R1A_HUMAN),
        side_effects=Known.confirmed((), provenance_chain=_R1A_HUMAN),
    )


def _r1a_unchanged_side_delta() -> SideDelta:
    return SideDelta(
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


def _r1a_confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=_R1A_CONFIRMED_AT, provenance="human_review"
    )


def _r1a_confirmed_state(identity: TurnIdentity, *, confirmed_state_id: str) -> ConfirmedTurnState:
    return ConfirmedTurnState(
        confirmed_state_id=confirmed_state_id,
        identity=identity,
        previous_confirmed_state_id=None,
        self_side=_r1a_side_state(active="Escavalier"),
        opponent_side=_r1a_side_state(active="Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_R1A_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_R1A_HUMAN),
        confirmation=_r1a_confirmation(),
    )


def _r1a_delta(identity: TurnIdentity, *, delta_id: str, based_on: str) -> ActionResultDelta:
    return ActionResultDelta(
        delta_id=delta_id,
        identity=identity,
        based_on_confirmed_state_id=based_on,
        self_side=_r1a_unchanged_side_delta(),
        opponent_side=_r1a_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_r1a_confirmation(),
    )


def test_r1a_known_opponent_action_completion_is_atomic(tmp_path: Path) -> None:
    """A: a fully-known Turn commits through record_rich_action_completion."""

    repository = SQLiteRepository(tmp_path / "r1a_known.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-known")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    delta = _r1a_delta(identity, delta_id="delta-known", based_on="cs-known")

    repository.record_rich_action_completion(
        transaction_id="txn-known",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )

    assert repository.get_action_result_delta("delta-known") == delta
    completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert completion is not None
    assert completion["opponent_action_type"] is ActionType.MOVE
    assert completion["opponent_action_name"] == "Earthquake"
    assert completion["action_order"] is ActionOrder.SELF_FIRST
    repository.close()


def test_r1a_unknown_opponent_action_completion_is_atomic(tmp_path: Path) -> None:
    """B: opponent action UNKNOWN commits through the same one atomic
    boundary as a known opponent action -- no separate path, and the
    explicit typed observation round-trips as None, not a magic string."""

    repository = SQLiteRepository(tmp_path / "r1a_unknown_opponent.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-unk")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    delta = _r1a_delta(identity, delta_id="delta-unk", based_on="cs-unk")

    repository.record_rich_action_completion(
        transaction_id="txn-unk",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )

    assert repository.get_action_result_delta("delta-unk") == delta
    completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert completion is not None
    assert completion["opponent_action_type"] is None
    assert completion["opponent_action_name"] is None

    # Storage-level: the same explicit "UNKNOWN" sentinel text this exact
    # table's own action_order column already uses -- never an empty
    # string smuggled through the NOT NULL columns as a magic value.
    raw_row = repository.connection.execute(
        "SELECT opponent_action_type, opponent_action_name FROM rich_action_completions"
        " WHERE turn_id = ?",
        (identity.turn_id,),
    ).fetchone()
    assert raw_row["opponent_action_type"] == "UNKNOWN"
    assert raw_row["opponent_action_name"] == "UNKNOWN"
    repository.close()


def test_r1a_unknown_action_order_completion_is_atomic(tmp_path: Path) -> None:
    """C: the historical-shape scenario -- confirmed self action, opponent
    action UNKNOWN, action order UNKNOWN, confirmed ActionResultDelta --
    all commit as one atomic completion."""

    repository = SQLiteRepository(tmp_path / "r1a_unknown_order.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-ord")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    delta = _r1a_delta(identity, delta_id="delta-ord", based_on="cs-ord")

    repository.record_rich_action_completion(
        transaction_id="txn-ord",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.UNKNOWN,
        delta=delta,
    )

    completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert completion is not None
    assert completion["opponent_action_type"] is None
    assert completion["action_order"] is ActionOrder.UNKNOWN
    repository.close()


def test_r1a_injected_failure_during_unknown_opponent_completion_rolls_back_totally(
    tmp_path: Path,
) -> None:
    """D: a failure mid-write for an UNKNOWN-opponent completion leaves
    neither the delta nor the completion committed -- the same
    all-or-nothing guarantee as the known-opponent case."""

    repository = SQLiteRepository(tmp_path / "r1a_unknown_fail.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-fail")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    delta = _r1a_delta(identity, delta_id="delta-fail", based_on="cs-fail")

    repository.record_rich_action_completion(
        transaction_id="txn-fail-1",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.UNKNOWN,
        delta=delta,
    )

    # Same turn_id (UNIQUE) forces this second UNKNOWN-opponent attempt to
    # fail entirely, even though its own delta INSERT already succeeded
    # inside the same inner transaction.
    with pytest.raises(sqlite3.IntegrityError):
        repository.record_rich_action_completion(
            transaction_id="txn-fail-2",
            identity=identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Protect",
            opponent_action_type=None,
            opponent_action_name=None,
            action_order=ActionOrder.UNKNOWN,
            delta=_r1a_delta(identity, delta_id="delta-fail-2", based_on="cs-fail"),
        )

    # No residue from the failed second attempt -- no action-only, no
    # delta-only -- and the first (successful) completion is untouched.
    with pytest.raises(KeyError):
        repository.get_action_result_delta("delta-fail-2")
    completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert completion is not None
    assert completion["transaction_id"] == "txn-fail-1"
    repository.close()


def test_r1a_restart_hydration_of_unknown_opponent_completion_is_identical(
    tmp_path: Path,
) -> None:
    """E: restart hydration of an UNKNOWN-opponent completion decodes back
    to the same explicit typed None -- not "UNKNOWN" text leaking through
    the read API, and byte-identical to the pre-restart read."""

    db_path = tmp_path / "r1a_unknown_hydrate.db"
    repository = SQLiteRepository(db_path)
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-hyd")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    delta = _r1a_delta(identity, delta_id="delta-hyd", based_on="cs-hyd")
    repository.record_rich_action_completion(
        transaction_id="txn-hyd",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.UNKNOWN,
        delta=delta,
    )
    before = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert before is not None
    repository.close()

    restarted = SQLiteRepository(db_path)
    after = restarted.get_rich_action_completion_by_turn(identity.turn_id)
    assert after == before
    assert after is not None
    assert after["opponent_action_type"] is None
    assert after["opponent_action_name"] is None
    restarted.close()


def test_r1a_no_plain_non_atomic_completion_path_reachable_from_normal_recording() -> None:
    """F (static): the controller delegates the rich values into the same
    application command as the legacy action instead of writing afterward."""

    source = inspect.getsource(TurnStateFlowController.record_actual_action)
    assert "action_result_delta=delta" in source
    assert "rich_transaction_id=" in source
    assert "record_rich_action_completion" not in source
    assert "append_action_result_delta" not in source


def test_r1a_normal_recording_with_unset_opponent_action_still_atomic(
    tmp_path: Path,
) -> None:
    """F (behavioral): the real Battle Record UI path, with the opponent
    action left unset (the routine "not observed" case), still produces a
    durable rich_action_completions row -- the normal path never silently
    falls back to a non-atomic delta-only insert. Zero provider dispatch."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)

    identity = controller.turn_state_summary().identity
    assert identity is not None
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("選択してください")
    window._on_record_action()  # noqa: SLF001

    completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
    assert completion is not None
    assert completion["opponent_action_type"] is None
    assert completion["opponent_action_name"] is None
    assert transport.call_count == 0
    repository.close()


# --- R2-A/B/C: outer atomicity, strict sentinel, coherent advance ----------


def _prepare_normal_recording(
    tmp_path: Path, *, known_opponent: bool
) -> tuple[
    SQLiteRepository,
    TurnStateFlowController,
    BattleRecordUiWindow,
    FakeTurnAdviceTransport,
]:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    if known_opponent:
        window.opponent_action_type_box.setCurrentText("MOVE")
        window.opponent_action_name_input.setText("Earthquake")
    else:
        window.opponent_action_type_box.setCurrentText("UNKNOWN")
    return repository, controller, window, transport


def _completion_counts(repository: SQLiteRepository) -> tuple[int, int, int]:
    return tuple(
        int(repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("recorded_actions", "action_result_deltas", "rich_action_completions")
    )  # type: ignore[return-value]


@pytest.mark.parametrize("known_opponent", [True, False], ids=["known", "unknown"])
@pytest.mark.parametrize(
    "fault",
    [
        "before_recorded_action",
        "after_recorded_action",
        "after_delta",
        "completion_constraint",
        "session_transition",
        "commit",
    ],
)
def test_r2_normal_battle_record_faults_roll_back_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    known_opponent: bool,
    fault: str,
) -> None:
    repository, controller, window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=known_opponent
    )
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None

    original_append_action = repository.append_recorded_action
    original_append_rich = repository.append_rich_action_completion
    original_save_session = repository.save_session
    original_transaction = repository.transaction

    if fault == "before_recorded_action":
        monkeypatch.setattr(
            repository,
            "append_recorded_action",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("BEFORE_ACTION")),
        )
    elif fault == "after_recorded_action":
        monkeypatch.setattr(
            repository,
            "append_rich_action_completion",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("AFTER_ACTION")),
        )
    elif fault == "after_delta":
        def fail_after_delta(**kwargs: object) -> None:
            repository.append_action_result_delta(cast(ActionResultDelta, kwargs["delta"]))
            raise RuntimeError("AFTER_DELTA")

        monkeypatch.setattr(repository, "append_rich_action_completion", fail_after_delta)
    elif fault == "completion_constraint":
        def fail_completion_constraint(**kwargs: object) -> None:
            original_append_rich(**kwargs)  # type: ignore[arg-type]
            second = dict(kwargs)
            second["transaction_id"] = "duplicate-turn-transaction"
            second["delta"] = replace(
                cast(ActionResultDelta, kwargs["delta"]), delta_id="duplicate-turn-delta"
            )
            original_append_rich(**second)  # type: ignore[arg-type]

        monkeypatch.setattr(
            repository, "append_rich_action_completion", fail_completion_constraint
        )
    elif fault == "session_transition":
        monkeypatch.setattr(
            repository,
            "save_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("SESSION_SAVE")),
        )
    else:
        @contextmanager
        def fail_commit():
            repository.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                repository.connection.rollback()
                raise RuntimeError("COMMIT_FAILURE")
            except BaseException:
                repository.connection.rollback()
                raise

        monkeypatch.setattr(repository, "transaction", fail_commit)

    window._on_record_action()  # noqa: SLF001
    assert _completion_counts(repository) == (0, 0, 0)
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.TURN_REVIEWED
    assert controller.turn_state_summary().identity == identity_before
    assert controller.turn_state_summary().provider_ready is False

    monkeypatch.setattr(repository, "append_recorded_action", original_append_action)
    monkeypatch.setattr(repository, "append_rich_action_completion", original_append_rich)
    monkeypatch.setattr(repository, "save_session", original_save_session)
    monkeypatch.setattr(repository, "transaction", original_transaction)
    window._on_record_action()  # noqa: SLF001

    assert _completion_counts(repository) == (1, 1, 1)
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.TURN_RECORDED
    action = repository.list_recorded_actions(session.session_id)[0]
    completion = repository.get_rich_action_completion_by_turn(action.turn_id)
    assert completion is not None
    assert completion["turn_id"] == action.turn_id == identity_before.turn_id
    assert completion["delta_id"] == controller.turn_state_summary().latest_delta.delta_id
    assert completion["opponent_action_type"] is (
        ActionType.MOVE if known_opponent else None
    )
    assert transport.call_count == 0
    repository.close()


def test_r2_known_switch_and_unknown_sentinel_round_trip(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "sentinel-roundtrip.db")
    for suffix, opponent_type, opponent_name in (
        ("switch", ActionType.SWITCH, "Garchomp"),
        ("unknown", None, None),
    ):
        identity = _r1a_identity(turn_id=f"turn-{suffix}")
        confirmed = _r1a_confirmed_state(identity, confirmed_state_id=f"cs-{suffix}")
        with repository.transaction():
            repository.append_confirmed_turn_state(confirmed)
        repository.record_rich_action_completion(
            transaction_id=f"txn-{suffix}",
            identity=identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Swords Dance",
            opponent_action_type=opponent_type,
            opponent_action_name=opponent_name,
            action_order=ActionOrder.UNKNOWN,
            delta=_r1a_delta(identity, delta_id=f"delta-{suffix}", based_on=f"cs-{suffix}"),
        )
        completion = repository.get_rich_action_completion_by_turn(identity.turn_id)
        assert completion is not None
        assert completion["opponent_action_type"] is opponent_type
        assert completion["opponent_action_name"] == (opponent_name or None)
    repository.close()


@pytest.mark.parametrize(
    ("stored_type", "stored_name"),
    [
        ("MOVE", "UNKNOWN"),
        ("SWITCH", "UNKNOWN"),
        ("UNKNOWN", "Earthquake"),
        ("UNKNOWN", ""),
        ("INVALID", "Earthquake"),
    ],
)
def test_r2_malformed_unknown_storage_fails_closed(
    tmp_path: Path, stored_type: str, stored_name: str
) -> None:
    repository = SQLiteRepository(tmp_path / "malformed-sentinel.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-malformed")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    repository.record_rich_action_completion(
        transaction_id="txn-malformed",
        identity=identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Swords Dance",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.UNKNOWN,
        delta=_r1a_delta(identity, delta_id="delta-malformed", based_on="cs-malformed"),
    )
    with repository.transaction():
        repository.connection.execute(
            "UPDATE rich_action_completions SET opponent_action_type = ?, "
            "opponent_action_name = ?",
            (stored_type, stored_name),
        )
    with pytest.raises(TurnStateError):
        repository.get_rich_action_completion_by_turn(identity.turn_id)
    repository.close()


@pytest.mark.parametrize("fault", ["derive", "persist"])
def test_r2_next_turn_failure_does_not_advance_and_retry_is_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    repository, controller, window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=False
    )
    window._on_record_action()  # noqa: SLF001
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None

    original_derive = application_service.derive_next_turn_state_draft
    original_persist = repository.upsert_next_turn_state_draft
    if fault == "derive":
        monkeypatch.setattr(
            application_service,
            "derive_next_turn_state_draft",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TurnStateError("DERIVE_FAIL")),
        )
    else:
        monkeypatch.setattr(
            repository,
            "upsert_next_turn_state_draft",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("PERSIST_FAIL")),
        )

    window._on_next_turn()  # noqa: SLF001
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.TURN_RECORDED
    assert session.current_turn_id == identity_before.turn_id
    assert controller.turn_state_summary().identity == identity_before
    assert repository.get_latest_next_turn_state_draft(session.session_id) is None
    assert controller.turn_state_summary().provider_ready is False
    assert repository.connection.execute("SELECT COUNT(*) FROM battle_turns").fetchone()[0] == 1

    monkeypatch.setattr(application_service, "derive_next_turn_state_draft", original_derive)
    monkeypatch.setattr(repository, "upsert_next_turn_state_draft", original_persist)
    window._on_next_turn()  # noqa: SLF001
    after = controller.turn_state_summary()
    assert after.identity is not None
    assert after.identity.turn_number == identity_before.turn_number + 1
    assert after.open_draft is not None
    assert after.open_draft.identity == after.identity
    assert repository.connection.execute("SELECT COUNT(*) FROM battle_turns").fetchone()[0] == 2
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM next_turn_state_drafts"
    ).fetchone()[0] == 1
    assert transport.call_count == 0
    repository.close()


# --- R3: mandatory rich boundary, typed UNKNOWN, derive-before-BEGIN -------


def _assert_failed_rich_recording(
    repository: SQLiteRepository,
    controller: TurnStateFlowController,
    identity_before: TurnIdentity,
) -> None:
    assert _completion_counts(repository) == (0, 0, 0)
    session = repository.load_active_session()
    assert session is not None
    assert session.state is BattleState.TURN_REVIEWED
    assert controller.turn_state_summary().identity == identity_before
    assert controller.turn_state_summary().provider_ready is False


@pytest.mark.parametrize("fault", ["missing_state", "missing_delta", "wrong_identity"])
def test_r3_real_battle_record_requires_complete_rich_prerequisites_and_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    repository, controller, window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=False
    )
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None
    original_latest = repository.get_latest_confirmed_turn_state_for_identity
    original_side_delta = window.self_delta_editor.to_side_delta

    if fault == "missing_state":
        monkeypatch.setattr(
            repository, "get_latest_confirmed_turn_state_for_identity", lambda **_kwargs: None
        )
    elif fault == "missing_delta":
        monkeypatch.setattr(window.self_delta_editor, "to_side_delta", lambda: None)
    else:
        latest = original_latest(
            session_id=identity_before.session_id,
            match_id=identity_before.match_id,
            generation=identity_before.generation,
        )
        assert latest is not None
        wrong = replace(
            latest,
            identity=replace(latest.identity, turn_id="wrong-turn", turn_number=99),
        )
        monkeypatch.setattr(
            repository,
            "get_latest_confirmed_turn_state_for_identity",
            lambda **_kwargs: wrong,
        )

    window._on_record_action()  # noqa: SLF001
    _assert_failed_rich_recording(repository, controller, identity_before)

    monkeypatch.setattr(repository, "get_latest_confirmed_turn_state_for_identity", original_latest)
    monkeypatch.setattr(window.self_delta_editor, "to_side_delta", original_side_delta)
    window._on_record_action()  # noqa: SLF001
    assert _completion_counts(repository) == (1, 1, 1)
    session = repository.load_active_session()
    assert session is not None and session.state is BattleState.TURN_RECORDED
    assert transport.call_count == 0
    repository.close()


def test_r3_application_mandatory_rich_operation_rejects_incomplete_arguments(
    tmp_path: Path,
) -> None:
    repository, controller, _window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=False
    )
    identity = controller.turn_state_summary().identity
    assert identity is not None
    application = controller._application  # noqa: SLF001

    with pytest.raises(DomainError, match="INCOMPLETE_RICH_ACTION_COMPLETION"):
        application.record_rich_actual_action(
            action_type=ActionType.MOVE,
            action_name="Flower Trick",
            human_confirmed=True,
            opponent_action_type=None,
            opponent_action_name=None,
            action_order=ActionOrder.UNKNOWN,
            completion_identity=identity,
            action_result_delta=cast(ActionResultDelta, None),
            rich_transaction_id="r3-incomplete",
        )

    _assert_failed_rich_recording(repository, controller, identity)
    assert transport.call_count == 0
    repository.close()


def test_r3_unknown_is_none_none_before_persistence_after_read_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "r3-unknown-runtime.db"
    repository, controller, window, transport = build_window(tmp_path, name=db_path.name)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("UNKNOWN")

    observed_runtime_pairs: list[tuple[ActionType | None, str | None]] = []
    original_append = repository.append_recorded_action

    def capture_runtime_pair(session_id: str, action: object) -> None:
        typed_action = cast("RecordedAction", action)
        observed_runtime_pairs.append(
            (typed_action.opponent_action_type, typed_action.opponent_action_name)
        )
        original_append(session_id, typed_action)

    monkeypatch.setattr(repository, "append_recorded_action", capture_runtime_pair)
    window._on_record_action()  # noqa: SLF001
    assert observed_runtime_pairs == [(None, None)]
    action = repository.list_recorded_actions(
        cast(str, repository.load_active_session().session_id)  # type: ignore[union-attr]
    )[0]
    assert (action.opponent_action_type, action.opponent_action_name) == (None, None)
    turn_id = action.turn_id
    completion = repository.get_rich_action_completion_by_turn(turn_id)
    assert completion is not None
    assert (completion["opponent_action_type"], completion["opponent_action_name"]) == (
        None,
        None,
    )
    repository.close()

    restarted = SQLiteRepository(db_path)
    restarted_action = restarted.get_recorded_action_for_turn(turn_id)
    restarted_completion = restarted.get_rich_action_completion_by_turn(turn_id)
    assert restarted_action is not None and restarted_completion is not None
    assert (
        restarted_action.opponent_action_type,
        restarted_action.opponent_action_name,
    ) == (None, None)
    assert (
        restarted_completion["opponent_action_type"],
        restarted_completion["opponent_action_name"],
    ) == (None, None)
    assert transport.call_count == 0
    restarted.close()


@pytest.mark.parametrize(
    ("opponent_type", "opponent_name"),
    [
        (None, "Earthquake"),
        (ActionType.MOVE, None),
        (ActionType.MOVE, ""),
        (None, ""),
    ],
)
def test_r3_runtime_unknown_pairs_fail_closed(
    tmp_path: Path,
    opponent_type: ActionType | None,
    opponent_name: str | None,
) -> None:
    repository = SQLiteRepository(tmp_path / "r3-runtime-pairs.db")
    identity = _r1a_identity()
    confirmed = _r1a_confirmed_state(identity, confirmed_state_id="cs-runtime-pairs")
    with repository.transaction():
        repository.append_confirmed_turn_state(confirmed)
    with pytest.raises(TurnStateError):
        repository.record_rich_action_completion(
            transaction_id="txn-runtime-pairs",
            identity=identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Swords Dance",
            opponent_action_type=opponent_type,
            opponent_action_name=opponent_name,
            action_order=ActionOrder.UNKNOWN,
            delta=_r1a_delta(
                identity, delta_id="delta-runtime-pairs", based_on="cs-runtime-pairs"
            ),
        )
    assert _completion_counts(repository)[1:] == (0, 0)
    repository.close()


def test_r3_next_turn_derives_before_transaction_and_derivation_failure_enters_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, controller, window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=False
    )
    window._on_record_action()  # noqa: SLF001
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None
    events: list[str] = []
    original_derive = application_service.derive_next_turn_state_draft
    original_transaction = repository.transaction

    def observed_derive(*args: object, **kwargs: object):
        events.append("derive")
        return original_derive(*args, **kwargs)  # type: ignore[arg-type]

    @contextmanager
    def observed_transaction():
        events.append("transaction")
        with original_transaction():
            yield

    monkeypatch.setattr(application_service, "derive_next_turn_state_draft", observed_derive)
    monkeypatch.setattr(repository, "transaction", observed_transaction)
    window._on_next_turn()  # noqa: SLF001
    assert events[:2] == ["derive", "transaction"]

    # Start another recorded Turn and prove a pure derivation failure does
    # not enter the repository transaction at all.
    window.render_view()
    _fill_minimal_current_state(window)
    _confirm_and_send(window)
    _record_action(window)
    events.clear()
    monkeypatch.setattr(
        application_service,
        "derive_next_turn_state_draft",
        lambda *_args, **_kwargs: (
            events.append("derive"),
            (_ for _ in ()).throw(TurnStateError("DERIVE_FAIL")),
        )[1],
    )
    identity_second = controller.turn_state_summary().identity
    window._on_next_turn()  # noqa: SLF001
    assert events == ["derive"]
    assert controller.turn_state_summary().identity == identity_second
    assert transport.call_count == 0
    repository.close()


@pytest.mark.parametrize("fault", ["stale_revalidation", "persist", "commit"])
def test_r3_next_turn_phase2_faults_rollback_and_retry_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    repository, controller, window, transport = _prepare_normal_recording(
        tmp_path, known_opponent=False
    )
    window._on_record_action()  # noqa: SLF001
    identity_before = controller.turn_state_summary().identity
    assert identity_before is not None
    original_transaction = repository.transaction
    original_persist = repository.upsert_next_turn_state_draft

    if fault == "stale_revalidation":
        @contextmanager
        def stale_transaction():
            with original_transaction():
                repository.connection.execute(
                    "UPDATE battle_sessions SET battle_revision = battle_revision + 1"
                )
                yield

        monkeypatch.setattr(repository, "transaction", stale_transaction)
    elif fault == "persist":
        monkeypatch.setattr(
            repository,
            "upsert_next_turn_state_draft",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("PERSIST_FAIL")),
        )
    else:
        @contextmanager
        def fail_commit():
            repository.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                repository.connection.rollback()
                raise RuntimeError("COMMIT_FAIL")
            except BaseException:
                repository.connection.rollback()
                raise

        monkeypatch.setattr(repository, "transaction", fail_commit)

    window._on_next_turn()  # noqa: SLF001
    session = repository.load_active_session()
    assert session is not None and session.state is BattleState.TURN_RECORDED
    assert controller.turn_state_summary().identity == identity_before
    assert repository.connection.execute("SELECT COUNT(*) FROM battle_turns").fetchone()[0] == 1
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM next_turn_state_drafts"
    ).fetchone()[0] == 0

    monkeypatch.setattr(repository, "transaction", original_transaction)
    monkeypatch.setattr(repository, "upsert_next_turn_state_draft", original_persist)
    window._on_next_turn()  # noqa: SLF001
    assert repository.connection.execute("SELECT COUNT(*) FROM battle_turns").fetchone()[0] == 2
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM next_turn_state_drafts"
    ).fetchone()[0] == 1
    assert transport.call_count == 0
    repository.close()
