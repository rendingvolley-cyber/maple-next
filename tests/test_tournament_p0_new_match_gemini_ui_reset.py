"""P0: a successful New Match is a hard Gemini operator-UI boundary."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    SELF_TEAM,
    SyncDispatch,
    _advance_to_turn_capture_pending,
    _confirm_legal_switches_honestly,
    _fill_minimal_current_state,
    qt_application,
)

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import MatchOutcome
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    SanitizedProviderResult,
)
from maple_next.providers.turn_transport import FakeTurnAdviceTransport
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController


class _SelectionSyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self._transport = transport
        self._request = request
        self._config = config
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed

    def start(self) -> None:
        try:
            self._on_succeeded(self._transport.send(self._request, self._config))
        except Exception as exc:  # fake transport only
            self._on_failed(str(exc))


def _build_window(tmp_path: Path):
    qt_application()
    repository = SQLiteRepository(tmp_path / "new-match-gemini-reset.db")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    selection_transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(SELECTED_THREE), "lead": SELECTED_THREE[0]},
                source_type=GEMINI_SOURCE_TYPE,
                model="selection-model-a",
            ),
            SanitizedProviderResult(
                payload={"selected_three": list(SELECTED_THREE), "lead": SELECTED_THREE[1]},
                source_type=GEMINI_SOURCE_TYPE,
                model="selection-model-b",
            ),
        ]
    )
    selection_adapter = GeminiSelectionAdviceAdapter(
        selection_transport,
        lambda: ProviderConfig(api_key="fake", model="selection-model-a"),
        dispatch_factory=_SelectionSyncDispatch,
    )
    turn_transport = FakeTurnAdviceTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(turn_transport, dispatch_factory=SyncDispatch)
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        selection_adapter,
        None,
        rich_adapter,
    )
    ocr_dir = tmp_path / "ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_dir)
    return repository, application, controller, window, selection_transport, turn_transport


def test_new_match_clears_foreign_selection_and_turn_gemini_operator_state(tmp_path: Path) -> None:
    (
        repository,
        application,
        controller,
        window,
        selection_transport,
        turn_transport,
    ) = _build_window(tmp_path)
    controller.new_match()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window._on_confirm_facts()  # noqa: SLF001
    controller.send_selection_advice_to_gemini(on_result=window.render_view)
    view_a = controller.refresh()
    old_session_id = view_a.projection.session_id
    old_match_id = view_a.projection.match_id
    assert view_a.advice is not None
    assert view_a.advice.selected_three == SELECTED_THREE
    assert controller.selection_advice_status().status == "SUCCESS"
    old_selection_advice_id = view_a.projection.current_selection_advice_id
    assert old_selection_advice_id is not None
    controller.apply_current_gemini_advice(human_confirmed=True)

    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("foreign-match prediction")
    window.mock_turn_rationale_input.setText("foreign-match rationale")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    view_a = controller.refresh()
    assert view_a.turn_advice is not None
    old_turn_advice_id = view_a.projection.current_turn_advice_id
    assert old_turn_advice_id is not None
    assert "Flower Trick" in window.turn_advice_action_label.text()
    assert controller.rich_turn_advice_gemini_status().status == "SUCCESS"

    # Normal terminal lifecycle: record the action, end, export, then use
    # the actual New Match button bound to the production window.
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()  # noqa: SLF001
    controller.end_match(MatchOutcome.WIN.value, human_confirmed=True)
    controller.save_match_json()
    window.render_view()
    window.new_match_after_export_button.click()
    QApplication.processEvents()

    view_b = controller.refresh()
    assert view_b.projection.session_state == "SELECTION_OPEN"
    assert view_b.projection.session_id != old_session_id
    assert view_b.projection.match_id != old_match_id
    assert view_b.advice is None
    assert view_b.turn_advice is None
    assert controller.selection_advice_status().status == "IDLE"
    assert controller.rich_turn_advice_gemini_status().status == "IDLE"
    assert window.selection_v3_advice_waiting.isVisible()
    assert all(label.text() == "—" for label in window.selection_v3_advice_pick_labels)
    assert window.selection_v3_advice_lead.text() == "—"
    assert window.turn_advice_action_label.text() == "—"
    assert window.turn_advice_rationale_label.text() == "—"
    assert window._bundle_c_gemini_send_button.isHidden()  # noqa: SLF001
    assert selection_transport.call_count == 1
    assert turn_transport.call_count == 1
    assert repository.get_selection_advice(old_selection_advice_id)["session_id"] == old_session_id
    assert repository.get_turn_advice(old_turn_advice_id).turn_advice_id == old_turn_advice_id

    # A newly bound Match B advice can render normally; Match A's content
    # never supplies this view.
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window._on_confirm_facts()  # noqa: SLF001
    controller.send_selection_advice_to_gemini(on_result=window.render_view)
    view_b_advice = controller.refresh()
    assert view_b_advice.advice is not None
    assert view_b_advice.advice.lead == SELECTED_THREE[1]
    assert controller.selection_advice_status().status == "SUCCESS"
    assert selection_transport.call_count == 2
    repository.close()
