from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import JobStatus, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)
GEMINI_THREE = ("Meowscarada", "Gholdengo", "Dragonite")
TEST_CONFIG = ProviderConfig(
    api_key="synthetic-test-only",
    model="gemini-test-model",
    timeout_seconds=5.0,
)


class SyncDispatch:
    def __init__(
        self,
        transport: FakeSelectionAdviceTransport,
        request: object,
        config: ProviderConfig,
        *,
        on_succeeded: object,
        on_failed: object,
    ) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)  # type: ignore[arg-type]
        except ProviderTransportError as exc:
            self.on_failed(str(exc))  # type: ignore[operator]
        else:
            self.on_succeeded(result)  # type: ignore[operator]


class HeldDispatch(SyncDispatch):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.started = False

    def start(self) -> None:
        self.started = True

    def resolve(self) -> None:
        super().start()


class HeldDispatchFactory:
    def __init__(self) -> None:
        self.dispatches: list[HeldDispatch] = []

    def __call__(self, *args: object, **kwargs: object) -> HeldDispatch:
        dispatch = HeldDispatch(*args, **kwargs)
        self.dispatches.append(dispatch)
        return dispatch


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def valid_result() -> SanitizedProviderResult:
    return SanitizedProviderResult(
        payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
        source_type=GEMINI_SOURCE_TYPE,
        model="gemini-test-model",
    )


def make_envelope(job: JobEnvelope, result: SanitizedProviderResult) -> ResultEnvelope:
    return ResultEnvelope(
        contract_version=job.contract_version,
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload=result.payload,
        source_type=result.source_type,
    )


def build_controller(
    tmp_path: Path,
    transport: FakeSelectionAdviceTransport,
    *,
    dispatch_factory: object = SyncDispatch,
    envelope_factory: object = make_envelope,
) -> tuple[
    SQLiteRepository,
    MatchApplication,
    GeminiSelectionAdviceAdapter,
    MatchFlowController,
]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(
        repository,
        tmp_path / "exports",
        repository_root=tmp_path / "repository-root",
    )
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: TEST_CONFIG,
        dispatch_factory=dispatch_factory,  # type: ignore[arg-type]
        envelope_factory=envelope_factory,  # type: ignore[arg-type]
    )
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=adapter,
    )
    return repository, application, adapter, controller


def ready_selection(controller: MatchFlowController) -> None:
    controller.new_match()
    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    assert view.error_message is None
    assert view.session_state == "SELECTION_OPEN"


def test_happy_path_pending_success_and_human_apply_only(tmp_path: Path) -> None:
    factory = HeldDispatchFactory()
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path,
        transport,
        dispatch_factory=factory,
    )
    ready_selection(controller)
    before = repository.load_active_session()
    assert before is not None

    callbacks: list[object] = []
    pending = controller.send_selection_advice_to_gemini(on_result=callbacks.append)

    assert pending.provider_status == "IN_FLIGHT"
    assert controller.selection_advice_status().status == "PENDING"
    assert adapter.dispatch_count == 1
    assert transport.call_count == 0
    assert repository.get_applied_selection if True else False

    factory.dispatches[0].resolve()
    success = controller.refresh()
    status = controller.selection_advice_status()

    assert transport.call_count == 1
    request, _config = transport.calls[0]
    assert request.self_team == SELF_TEAM
    assert request.opponent_team == OPPONENT_TEAM
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.request_payload_hash
    assert success.advice is not None
    assert success.advice.selected_three == GEMINI_THREE
    assert success.advice.lead == "Meowscarada"
    assert status.status == "SUCCESS"
    assert status.model == "gemini-test-model"
    assert status.binding_status == "CURRENT"
    assert status.legality_status == "VALID"
    assert status.can_apply is True
    session_before_apply = repository.load_active_session()
    assert session_before_apply is not None
    assert session_before_apply.current_applied_selection_id is None

    not_applied = controller.apply_current_gemini_advice(human_confirmed=False)
    assert not_applied.applied_selection is None

    applied = controller.apply_current_gemini_advice(human_confirmed=True)
    assert applied.applied_selection is not None
    assert applied.applied_selection.selected_three == GEMINI_THREE
    assert applied.applied_selection.lead == "Meowscarada"
    assert adapter.dispatch_count == 1
    assert transport.call_count == 1
    repository.close()


def test_window_open_render_refresh_and_repeat_activation_do_not_auto_send(
    tmp_path: Path,
) -> None:
    qt_application()
    factory = HeldDispatchFactory()
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path,
        transport,
        dispatch_factory=factory,
    )
    window = MatchFlowWindow(controller)
    window.show()
    window.render_view()
    controller.refresh()
    window.render_view()

    assert adapter.dispatch_count == 0
    assert transport.call_count == 0

    ready_selection(controller)
    window.render_view()
    controller.send_selection_advice_to_gemini(on_result=window.render_view)
    duplicate = controller.send_selection_advice_to_gemini(on_result=window.render_view)

    assert duplicate.error_message is not None
    assert adapter.dispatch_count == 1
    assert len(factory.dispatches) == 1
    assert transport.call_count == 0
    window.render_view()
    controller.refresh()
    assert adapter.dispatch_count == 1

    factory.dispatches[0].resolve()
    window.render_view()
    assert transport.call_count == 1
    assert adapter.dispatch_count == 1
    assert window.gemini_status_label.text() == "Status: SUCCESS"
    assert window.advice_model_label.text() == "gemini-test-model"
    assert window.advice_binding_label.text() == "CURRENT"
    assert window.advice_legality_label.text() == "VALID"
    assert window.apply_button.isEnabled() is False

    window.apply_confirm_checkbox.setChecked(True)
    assert window.apply_button.isEnabled() is True
    window.apply_button.click()
    current = controller.refresh()
    assert current.applied_selection is not None
    assert current.applied_selection.selected_three == GEMINI_THREE
    assert transport.call_count == 1
    window.close()
    repository.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("session_id", "wrong-session"),
        ("match_id", "wrong-match"),
        ("generation", 999),
        ("base_battle_revision", 999),
        ("input_snapshot_id", "wrong-reviewed-selection"),
    ],
)
def test_wrong_identity_result_is_stale_and_never_applicable(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    def tampered_factory(
        job: JobEnvelope,
        result: SanitizedProviderResult,
    ) -> ResultEnvelope:
        return replace(make_envelope(job, result), **{field: replacement})

    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path,
        transport,
        envelope_factory=tampered_factory,
    )
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    status = controller.selection_advice_status()
    session = repository.load_active_session()
    assert session is not None
    job = repository.get_job(cast(str, adapter.last_job_id))

    assert transport.call_count == 1
    assert status.status == "STALE"
    assert status.can_apply is False
    assert session.current_selection_advice_id is None
    assert session.current_applied_selection_id is None
    assert job.status is JobStatus.FAILED
    repository.close()


def test_state_change_while_in_flight_discards_old_result(tmp_path: Path) -> None:
    factory = HeldDispatchFactory()
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, application, adapter, controller = build_controller(
        tmp_path,
        transport,
        dispatch_factory=factory,
    )
    ready_selection(controller)
    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    changed_self = (*SELF_TEAM[:-1], "Kingambit")
    application.confirm_selection_facts(changed_self, OPPONENT_TEAM)
    factory.dispatches[0].resolve()

    status = controller.selection_advice_status()
    session = repository.load_active_session()
    assert session is not None
    assert status.status == "STALE"
    assert status.can_apply is False
    assert session.current_selection_advice_id is None
    assert session.current_applied_selection_id is None
    assert adapter.dispatch_count == 1
    assert transport.call_count == 1
    repository.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"selected_three": ["Meowscarada", "Gholdengo"], "lead": "Meowscarada"},
        {
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite", "Dondozo"],
            "lead": "Meowscarada",
        },
        {
            "selected_three": ["Meowscarada", "Meowscarada", "Dragonite"],
            "lead": "Meowscarada",
        },
        {
            "selected_three": ["Meowscarada", "Gholdengo", "MissingNo"],
            "lead": "Meowscarada",
        },
        {
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite"],
            "lead": "Dondozo",
        },
    ],
)
def test_invalid_selection_never_becomes_current_or_applicable(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    result = SanitizedProviderResult(
        payload=payload,
        source_type=GEMINI_SOURCE_TYPE,
        model="gemini-test-model",
    )
    transport = FakeSelectionAdviceTransport(responses=[result])
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    status = controller.selection_advice_status()
    session = repository.load_active_session()
    assert session is not None

    assert status.status == "FAILED"
    assert status.legality_status == "INVALID"
    assert status.can_apply is False
    assert session.current_selection_advice_id is None
    assert session.current_applied_selection_id is None
    assert adapter.dispatch_count == 1
    assert transport.call_count == 1
    repository.close()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("GEMINI_TIMEOUT", "GEMINI_TIMEOUT"),
        (
            "GEMINI_HTTP_ERROR:400|STATUS=INVALID_ARGUMENT|REASON=API_KEY_INVALID",
            "GEMINI_HTTP_ERROR:400|STATUS=INVALID_ARGUMENT|REASON=API_KEY_INVALID",
        ),
        ("GEMINI_NETWORK_ERROR:secret/path and raw message", "GEMINI_NETWORK_ERROR"),
        ("totally raw provider secret", "GEMINI_FAILURE_UNCLASSIFIED"),
    ],
)
def test_failure_display_is_sanitized_and_secret_never_persisted(
    tmp_path: Path,
    reason: str,
    expected: str,
) -> None:
    transport = FakeSelectionAdviceTransport(
        responses=[ProviderTransportError(reason)]
    )
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    result_views: list[object] = []
    controller.send_selection_advice_to_gemini(on_result=result_views.append)
    status = controller.selection_advice_status()
    database_dump = "\n".join(repository.connection.iterdump())

    assert status.status == "FAILED"
    assert status.sanitized_failure == expected
    assert status.can_apply is False
    assert adapter.dispatch_count == 1
    assert transport.call_count == 1
    if reason != expected:
        assert reason not in database_dump
        assert reason not in controller.refresh().error_message
    repository.close()


def test_non_gemini_source_is_not_gemini_success(tmp_path: Path) -> None:
    result = SanitizedProviderResult(
        payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
        source_type="MAPLE_INTERNAL",
        model="internal-model",
    )
    transport = FakeSelectionAdviceTransport(responses=[result])
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    status = controller.selection_advice_status()

    assert status.status == "MISMATCH"
    assert status.source_type == "MAPLE_INTERNAL"
    assert status.can_apply is False
    assert adapter.dispatch_count == 1
    assert transport.call_count == 1
    repository.close()
