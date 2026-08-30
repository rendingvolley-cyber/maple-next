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
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from maple_next.domain.legal_switches import LegalSwitchStatus as _B2_LegalSwitchStatus

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import (
    DeviceOpenResult,
    SourceFramePacket,
    VideoCaptureBackend,
)
from maple_next.domain.enums import ResultDisposition
from maple_next.domain.turn_state import ChangeObservation, KnowledgeStatus
from maple_next.ocr.contracts import OcrCandidate, OcrCandidateBundle, OcrFieldKey
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
from maple_next.turn_ocr import TurnSnapshotIdentity, TurnSnapshotResult, TurnSnapshotStatus
from maple_next.ui.battle_record_ui import (
    BattleRecordUiWindow,
    _DeltaIntField,
    _KnownIntField,
)
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL
from maple_next.ui.turn_snapshot_window import _TURN_SNAPSHOT_ORIGIN_OCR
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController
from maple_next.workers.contracts.models import ResultEnvelope


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
        self.last_request: RichStateTurnAdviceRequest | None = None

    def send(
        self, request: RichStateTurnAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        del config
        self.call_count += 1
        self.last_request = request
        action = request.legal_actions[0]
        return SanitizedProviderResult(
            payload={
                "response_schema_version": "maple-turn-advice-response.v2",
                "recommended_action": {
                    "action_id": action.action_id,
                    "action_type": action.action_type.value,
                    "action_name": action.action_name,
                },
                "recommendation_robustness": "HIGH",
                "reasons": ["bounded non-network transport double"],
                "opponent_prediction": {
                    "primary": {
                        "category": "UNKNOWN",
                        "specific_action": None,
                        "support_basis": "NONE",
                        "support": "LOW",
                        "summary": "bounded non-network transport double",
                    },
                    "alternatives": [],
                },
                "warnings": [],
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
    # Check whichever switch slot is actually populated -- which candidate
    # (if any) lands at a given index depends on the current active/fainted
    # exclusion (see BattleRecordUiWindow._sync_switch_candidates), not a
    # fixed team position.
    for checkbox in window.switch_checkboxes:
        if checkbox.text():
            checkbox.setChecked(True)
            break
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
    assert window.record_action_button.text() == "結果記録"
    assert window.next_turn_button.text() == "NEXT TURN"
    assert window.diagnostics_drawer is not None
    assert window.terminal_flow_drawer is not None
    repository.close()


def test_confirm_turn_facts_requires_separate_explicit_send_when_provider_ready(
    tmp_path: Path,
) -> None:
    """Confirming facts persists the binding but never dispatches by itself.

    The separate SEND TURN TO GEMINI control is the only explicit provider
    action and sends exactly once from the newly confirmed revision.
    """

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.render_view()

    pre_confirm_revision = controller.turn_state_summary().identity.battle_revision

    assert not window.confirm_turn_facts_button.isHidden()
    assert window.confirm_turn_facts_button.isEnabled()
    window.confirm_turn_facts_button.click()

    assert transport.call_count == 0
    assert adapter.dispatch_count == 0
    assert window._bundle_c_gemini_send_button.isVisible()  # noqa: SLF001

    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001

    # Exactly one send, and it was built from the just-confirmed revision --
    # never the pre-confirm one. (The response's own successful apply below
    # advances battle_revision again, so this must be read from the
    # request the transport actually received, not from a summary taken
    # after the click -- by then the identity has already moved on.)
    assert transport.call_count == 1
    assert adapter.dispatch_count == 1
    assert transport.last_request is not None
    assert transport.last_request.identity.battle_revision != pre_confirm_revision

    # The response for that exact revision applied successfully -- not
    # STALE/INVALID.
    view = controller.refresh()
    assert view.error_message is None
    assert view.turn_advice is not None
    assert adapter.last_disposition is ResultDisposition.APPLIED
    repository.close()


def test_confirm_turn_facts_persists_without_dispatch_when_not_provider_ready(
    tmp_path: Path,
) -> None:
    """Confirming still succeeds even when the rich provider-ready gate is
    not satisfied -- but no send is attempted.

    This fixture's minimal filled-in state already satisfies every rich
    gate requirement once confirmed (proven by the positive-path test
    above), so "not provider-ready right after this exact confirm" is
    forced deterministically here via the same summary object the window
    itself reads -- exercising the actual branch in
    ``_on_confirm_turn_facts`` rather than fighting fixture internals to
    reproduce one specific denial reason.
    """

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.render_view()

    real_summary = window._bundle_c_controller.turn_state_summary  # noqa: SLF001
    not_ready_summary = replace(real_summary(), provider_ready=False)
    with patch.object(
        type(window._bundle_c_controller),  # noqa: SLF001
        "turn_state_summary",
        return_value=not_ready_summary,
    ):
        window.confirm_turn_facts_button.click()

    # Facts were still persisted by the (unpatched) confirm call inside the
    # click handler -- only the send decision was forced to see "not ready".
    view = controller.refresh()
    assert view.error_message is None
    assert view.projection.session_state == "TURN_REVIEWED"
    assert real_summary().confirmed_state is not None
    assert transport.call_count == 0
    assert adapter.dispatch_count == 0
    repository.close()


def test_confirm_turn_facts_does_not_duplicate_send_while_prior_send_in_flight(
    tmp_path: Path,
) -> None:
    """A press that lands while a previous send is still in flight must
    still persist the confirmed facts, but must not start a second
    concurrent dispatch for that press -- GeminiRichTurnAdviceAdapter's own
    in-flight guard covers the combined action exactly as it already
    covered the standalone SEND button."""

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.render_view()

    adapter._in_flight = True  # noqa: SLF001 - simulate a still-pending send
    window.confirm_turn_facts_button.click()

    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None  # facts still persisted
    assert transport.call_count == 0  # no dispatch happened
    assert adapter.dispatch_count == 0
    assert controller.refresh().error_message is None

    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    view = controller.refresh()
    assert view.error_message is not None  # the in-flight refusal surfaced, not silently dropped
    repository.close()


def test_stale_response_after_turn_advances_is_rejected(tmp_path: Path) -> None:
    """A Turn Advice result bound to a Turn identity that has since
    advanced must still be rejected as stale -- the combined confirm+send
    action does not weaken this pre-existing protection."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    # Confirm facts alone (legal switches not yet resolved for this binding)
    # -- provider_ready is False at this exact moment, so the combined
    # action correctly does not send yet.
    window._on_confirm_turn_facts()  # noqa: SLF001
    assert transport.call_count == 0
    _confirm_legal_switches_honestly(window)
    assert controller.turn_state_summary().provider_ready is True

    application = controller._application  # noqa: SLF001
    # A job requested for this now-provider-ready identity, but never
    # dispatched/applied -- representing a request that is still
    # outstanding when the Turn later advances.
    outstanding_job = application.request_rich_turn_advice("late-arriving-request")

    # Advance the Turn without a fresh rich-state confirm+send.
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window._on_record_action()
    controller.next_turn()

    late_envelope = ResultEnvelope(
        contract_version=outstanding_job.contract_version,
        result_id="late-result",
        job_id=outstanding_job.job_id,
        command_id=outstanding_job.command_id,
        job_type=outstanding_job.job_type,
        session_id=outstanding_job.session_id,
        match_id=outstanding_job.match_id,
        generation=outstanding_job.generation,
        turn_number=outstanding_job.turn_number,
        base_battle_revision=outstanding_job.base_battle_revision,
        expected_state=outstanding_job.expected_state,
        input_snapshot_id=outstanding_job.input_snapshot_id,
        request_payload_hash=outstanding_job.request_payload_hash,
        payload={
            "response_schema_version": "maple-turn-advice-response.v2",
            "recommended_action": {
                "action_id": "late",
                "action_type": "MOVE",
                "action_name": "Flower Trick",
            },
            "recommendation_robustness": "HIGH",
            "reasons": ["late"],
            "opponent_prediction": {
                "primary": {
                    "category": "UNKNOWN",
                    "specific_action": None,
                    "support_basis": "NONE",
                    "support": "LOW",
                    "summary": "late",
                },
                "alternatives": [],
            },
            "warnings": [],
        },
        source_type="GEMINI",
        model="late-model",
    )
    # Whatever the exact rejection reason, it must not be APPLIED -- the UI
    # (turn_advice_gemini_status) shows every non-APPLIED, non-None
    # disposition identically as STALE/INVALID, so this is the actual
    # user-facing contract this test protects.
    disposition = application.apply_rich_turn_advice_result(late_envelope)
    assert disposition != ResultDisposition.APPLIED
    repository.close()


def test_turn_ocr_status_indicator_transitions(tmp_path: Path) -> None:
    """Compact OCR status indicator: OCR待機/OCR中…/OCR完了/OCR要確認/OCRエラー.

    Pure-function check of the indicator's own state machine, independent
    of capture hardware -- the identity/staleness guard it relies on
    (``_on_turn_snapshot_result``'s existing ``_identity_is_current`` check)
    is unchanged production code, not re-tested here.
    """

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    assert window._turn_ocr_status_indicator_text("BATTLE_READY") == "OCR待機"  # noqa: SLF001
    assert window._turn_ocr_status_indicator_text(None) == "OCR待機"  # noqa: SLF001

    window._turn_ocr_status_code = TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    assert (
        window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCR中…"  # noqa: SLF001
    )

    window._turn_ocr_status_code = TurnSnapshotStatus.READY  # noqa: SLF001
    for key in window._turn_snapshot_origins:  # noqa: SLF001
        window._turn_snapshot_origins[key] = "確定済み値の引き継ぎ・OCR上書き不可"  # noqa: SLF001
    assert (
        window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCR完了"  # noqa: SLF001
    )

    any_key = next(iter(window._turn_snapshot_origins))  # noqa: SLF001
    window._turn_snapshot_origins[any_key] = _TURN_SNAPSHOT_ORIGIN_OCR  # noqa: SLF001
    assert (
        window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING")  # noqa: SLF001
        == "OCR要確認"
    )

    window._turn_ocr_status_code = TurnSnapshotStatus.OCR_FAILED  # noqa: SLF001
    assert (
        window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCRエラー"  # noqa: SLF001
    )

    # NEXT TURN's real path (_submit_frozen_turn_frame with reset_draft=True,
    # exercised by both _on_start_turn and _on_next_turn) always calls
    # _set_turn_snapshot_status for the fresh attempt before render_view can
    # run -- a stale prior-Turn error/READY code can never survive to be
    # read as the new Turn's status. Reproduce that exact call shape
    # directly (deterministic here; the full record-action/next-turn game
    # flow needs a live capture backend this fixture intentionally has
    # none of).
    window._reset_turn_snapshot_draft()  # noqa: SLF001
    window._set_turn_snapshot_status(  # noqa: SLF001
        TurnSnapshotStatus.CAPTURED, "fresh Turn capture"
    )
    assert window._turn_ocr_status_code == TurnSnapshotStatus.CAPTURED  # noqa: SLF001
    assert not any(  # noqa: SLF001
        origin == _TURN_SNAPSHOT_ORIGIN_OCR
        for origin in window._turn_snapshot_origins.values()  # noqa: SLF001
    )
    repository.close()


def test_stale_turn_ocr_result_does_not_mark_new_turn_complete(tmp_path: Path) -> None:
    """A late OCR callback bound to an old Turn identity must not overwrite
    the current Turn's OCR status -- exercises the real, unchanged
    ``_on_turn_snapshot_result`` / ``_identity_is_current`` guard."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    stale_identity = TurnSnapshotIdentity(
        session_id="stale-session",
        match_id="stale-match",
        generation=1,
        turn_id="stale-turn-id",
        turn_number=1,
        battle_revision=1,
        snapshot_generation=1,
    )
    # The current Turn is genuinely mid-analysis when the stale result
    # arrives -- exactly the race this guard exists for.
    window._turn_ocr_status_code = TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    assert window._turn_snapshot_active_identity != stale_identity  # noqa: SLF001

    stale_result = TurnSnapshotResult(
        identity=stale_identity,
        status=TurnSnapshotStatus.READY,
        bundle=OcrCandidateBundle(
            status="READY",
            candidate_only=True,
            manual_entry_allowed=True,
            frame_id="stale-frame",
            frame_captured_at_utc=None,
            frame_age_ms=None,
            candidates=(),
            error_code=None,
            operator_message="stale",
        ),
        frozen_image=QImage(),
        crops={},
        operator_message="stale result for an old Turn",
        roi_config_provenance="test",
    )
    window._on_turn_snapshot_result(stale_result)  # noqa: SLF001

    # Rejected: the current Turn's status must still read whatever it was
    # before the stale callback, never the stale result's READY/complete.
    assert window._turn_ocr_status_code == TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    assert (
        window._turn_ocr_status_indicator_text("TURN_CAPTURE_PENDING") == "OCR中…"  # noqa: SLF001
    )
    repository.close()


def _matching_turn_snapshot_identity(window) -> TurnSnapshotIdentity:
    """Mint an identity for the window's *current* Turn and bind it as the
    active in-flight snapshot -- mirrors what ``_submit_frozen_turn_frame``
    does on a real capture, without needing a capture backend."""

    current = window._controller.refresh()  # noqa: SLF001
    identity = window._next_turn_snapshot_identity(current)  # noqa: SLF001
    assert identity is not None
    window._turn_snapshot_active_identity = identity  # noqa: SLF001
    return identity


def test_turn_snapshot_result_review_needed_updates_label_without_render_view(
    tmp_path: Path,
) -> None:
    """Production bug: OCR-derived fields land via the real callback, but the
    compact status label previously only refreshed inside render_view(),
    which ``_on_turn_snapshot_result`` never calls. The label must leave
    "OCR中…" for "OCR要確認" as part of handling the callback itself."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    identity = _matching_turn_snapshot_identity(window)
    window._turn_ocr_status_code = TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    window.turn_ocr_status_indicator_label.setText("OCR中…")

    result = TurnSnapshotResult(
        identity=identity,
        status=TurnSnapshotStatus.READY,
        bundle=OcrCandidateBundle(
            status="READY",
            candidate_only=True,
            manual_entry_allowed=True,
            frame_id="frame-1",
            frame_captured_at_utc=None,
            frame_age_ms=None,
            candidates=(
                OcrCandidate(
                    field_key=OcrFieldKey.OPPONENT_HP.value,
                    suggested_value="100",
                    raw_text="100",
                    confidence=0.95,
                    rank=0,
                    reason="test candidate",
                    source_frame_id="frame-1",
                ),
            ),
            error_code=None,
            operator_message="ok",
        ),
        frozen_image=QImage(),
        crops={},
        operator_message="fresh result",
        roi_config_provenance="test",
    )

    window._on_turn_snapshot_result(result)  # noqa: SLF001

    assert window.opponent_hp_box.currentText() == "100"
    assert window._turn_snapshot_origins[OcrFieldKey.OPPONENT_HP.value] == (  # noqa: SLF001
        _TURN_SNAPSHOT_ORIGIN_OCR
    )
    assert window.turn_ocr_status_indicator_label.text() == "OCR要確認"
    repository.close()


def test_turn_snapshot_result_fully_usable_updates_label_without_render_view(
    tmp_path: Path,
) -> None:
    """Same production bug, other branch: a READY result that needs no
    human review must land on "OCR完了" immediately, without waiting for a
    later render_view() call."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    identity = _matching_turn_snapshot_identity(window)
    window._turn_ocr_status_code = TurnSnapshotStatus.ANALYZING  # noqa: SLF001
    window.turn_ocr_status_indicator_label.setText("OCR中…")

    result = TurnSnapshotResult(
        identity=identity,
        status=TurnSnapshotStatus.READY,
        bundle=OcrCandidateBundle(
            status="READY",
            candidate_only=True,
            manual_entry_allowed=True,
            frame_id="frame-2",
            frame_captured_at_utc=None,
            frame_age_ms=None,
            candidates=(),
            error_code=None,
            operator_message="ok",
        ),
        frozen_image=QImage(),
        crops={},
        operator_message="fresh result, nothing new to suggest",
        roi_config_provenance="test",
    )

    window._on_turn_snapshot_result(result)  # noqa: SLF001

    assert not any(  # noqa: SLF001
        origin == _TURN_SNAPSHOT_ORIGIN_OCR
        for origin in window._turn_snapshot_origins.values()  # noqa: SLF001
    )
    assert window.turn_ocr_status_indicator_label.text() == "OCR完了"
    repository.close()


def test_explicit_send_button_is_available_when_ready_and_hidden_after_success(
    tmp_path: Path,
) -> None:
    """The explicit SEND TURN TO GEMINI control appears only for a fresh,
    provider-ready binding and hides after a successful response."""

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window.render_view()

    button = window._bundle_c_gemini_send_button  # noqa: SLF001
    # Before facts are confirmed the binding is not provider-ready yet.
    assert not button.isVisible()

    window.confirm_turn_facts_button.click()
    assert transport.call_count == 0
    assert button.isVisible()
    assert button.isEnabled()
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    assert transport.call_count == 1
    assert adapter.last_disposition is ResultDisposition.APPLIED
    window.render_view()
    assert not button.isVisible()
    assert button.text() == "SEND TURN TO GEMINI"
    repository.close()


class _InvalidPayloadTransport:
    """Returns a payload that fails v2 schema validation -- a real,
    end-to-end INVALID_REJECTED, not a manually forced disposition."""

    def __init__(self) -> None:
        self.call_count = 0

    def send(self, request, config):  # noqa: ANN001
        del request, config
        self.call_count += 1
        return SanitizedProviderResult(
            payload={"this_is_not": "a valid turn advice response body"},
            source_type="GEMINI",
            model="bad-model",
        )


def test_gemini_resend_button_appears_only_after_a_failed_send(tmp_path: Path) -> None:
    """"Gemini再送信" is a temporary failure-state control, not a permanent
    second button: it appears, relabeled, only once CONFIRM TURN FACTS's
    own send genuinely fails (real end-to-end INVALID_REJECTED here, not a
    manually forced disposition) -- and the Turn stays REQUEST_TURN_ADVICE
    since nothing was actually applied."""

    qt_application()
    repository = SQLiteRepository(tmp_path / "resend-button.db")
    export_dir = tmp_path / "resend-export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = _InvalidPayloadTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="k", model="m", timeout_seconds=5.0),
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
    ocr_dir = tmp_path / "resend-ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    try:
        _advance_to_turn_capture_pending(controller)
        window.render_view()
        _fill_minimal_current_state(window)
        window.render_view()

        button = window._bundle_c_gemini_send_button  # noqa: SLF001
        assert not button.isVisible()

        window.confirm_turn_facts_button.click()
        assert button.isVisible()
        window._on_trusted_send_turn_to_gemini()  # noqa: SLF001

        assert transport.call_count == 1
        assert rich_adapter.last_disposition is ResultDisposition.INVALID_REJECTED
        assert controller.refresh().projection.primary_cta == "REQUEST_TURN_ADVICE"
        assert button.isVisible()
        assert button.text() == "Gemini再送信"
        assert window.gemini_empty_label.text() == (
            "Geminiの応答を使用できませんでした。再送してください。"
        )
        assert "INVALID_PAYLOAD" not in window.gemini_empty_label.text()
        assert "this_is_not" not in window.gemini_empty_label.text()
    finally:
        repository.close()


def test_invalid_payload_keeps_sanitized_audit_reason_off_player_surface(
    tmp_path: Path,
) -> None:
    """A rejected response keeps its sanitized reason in audit storage,
    while the live panel shows only one concise recovery action."""

    qt_application()
    repository = SQLiteRepository(tmp_path / "invalid-reason.db")
    export_dir = tmp_path / "invalid-reason-export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)
    transport = _InvalidPayloadTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="k", model="m", timeout_seconds=5.0),
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
    ocr_dir = tmp_path / "invalid-reason-ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_dir,
        capture_backend=CountingCaptureBackend(),
        auto_start_capture=False,
    )
    try:
        _advance_to_turn_capture_pending(controller)
        window.render_view()
        _fill_minimal_current_state(window)
        window.render_view()

        window.confirm_turn_facts_button.click()
        window._on_trusted_send_turn_to_gemini()  # noqa: SLF001

        assert transport.call_count == 1
        assert rich_adapter.last_disposition is ResultDisposition.INVALID_REJECTED
        # The real validator's own fixed sanitized token (not a manually
        # forced value) -- top_level_unknown_fields, from _reject_unknown_
        # keys rejecting this payload's "this_is_not" field.
        assert rich_adapter.last_invalid_payload_reason == (
            "INVALID_PAYLOAD:top_level_unknown_fields"
        )

        job_id = rich_adapter.last_job_id
        assert job_id is not None
        audits = repository.result_audits(job_id)
        assert audits[-1] == ("INVALID_REJECTED", "INVALID_PAYLOAD:top_level_unknown_fields")

        # No raw provider payload anywhere in the persisted audit row.
        row = repository.connection.execute(
            "SELECT reason, payload_json FROM async_job_results"
            " WHERE job_id = ? ORDER BY audit_id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        assert row["payload_json"] == "{}"
        assert "this_is_not" not in row["reason"]
        assert "a valid turn advice response body" not in row["reason"]

        window.render_view()
        assert not window.rich_gemini_status_label.isVisible()
        assert window.gemini_empty_label.text() == (
            "Geminiの応答を使用できませんでした。再送してください。"
        )
        assert "INVALID_PAYLOAD" not in window.gemini_empty_label.text()

        # Gemini再送信 (the human-controlled retry) is still available.
        button = window._bundle_c_gemini_send_button  # noqa: SLF001
        assert button.isVisible()
        assert button.text() == "Gemini再送信"
    finally:
        repository.close()


def test_turn_advice_status_shows_truthful_reason_not_generic_stale_invalid(
    tmp_path: Path,
) -> None:
    """The operator-visible send-status label distinguishes APPLIED, STALE,
    INVALID_PAYLOAD, and REJECTED instead of collapsing every non-applied
    disposition into one "STALE/INVALID" label."""

    repository, controller, window, _transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    projection = controller.refresh().projection
    adapter._operator_identity = (  # noqa: SLF001 - bind synthetic adapter state to this test match
        projection.session_id,
        projection.match_id,
        projection.generation,
    )
    adapter._operator_state_invalidated = False  # noqa: SLF001

    for disposition, expected_status in (
        (ResultDisposition.STALE_REJECTED, "STALE"),
        (ResultDisposition.INVALID_REJECTED, "INVALID_PAYLOAD"),
        (ResultDisposition.DUPLICATE_IGNORED, "REJECTED"),
        (ResultDisposition.APPLIED, "SUCCESS"),
    ):
        adapter.last_failure_reason = None
        adapter._in_flight = False  # noqa: SLF001
        adapter.last_disposition = disposition
        window.render_view()
        rich_status = window._bundle_c_controller.rich_turn_advice_gemini_status()  # noqa: SLF001
        assert rich_status.status == expected_status
        assert not window.rich_gemini_status_label.isVisible()
        assert not window.rich_gemini_denial_label.isVisible()
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
    # The explicit workbench selection is the canonical switch set, so both
    # visible candidates are persisted alongside the two legal moves.
    assert len(summary.confirmed_legal_actions) == 4
    assert {
        action.action_name
        for action in summary.confirmed_legal_actions
        if action.action_type.value == "SWITCH"
    } == {"Gholdengo", "Dragonite"}
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
