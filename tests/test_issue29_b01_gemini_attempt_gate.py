"""Issue #29 B-01: durable exactly-one production Gemini Selection attempt gate.

Only the production Gemini Selection lane is covered here. The mock/dev
Selection Advice lane (:class:`MockSelectionAdviceAdapter`) never calls
``reserve_gemini_selection_attempt`` and is untouched by this gate; its own
retry semantics are exercised elsewhere and are not part of this file.

All transports are fake/injected and all persistence is a temporary SQLite
file. No real provider, API key, ``.env``, or network access ever occurs.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import JobStatus, JobType
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderConfigError,
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
    """Synchronous stand-in dispatch: no QThread, resolves immediately."""

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
    """Dispatch-and-hold: lets a test assert IN_FLIGHT state before resolving."""

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


def pump_until(qapp: QApplication, predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting for async Gemini result"
        qapp.processEvents()
        time.sleep(0.005)


def valid_result() -> SanitizedProviderResult:
    return SanitizedProviderResult(
        payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
        source_type=GEMINI_SOURCE_TYPE,
        model="gemini-test-model",
    )


def invalid_result() -> SanitizedProviderResult:
    return SanitizedProviderResult(
        payload={"selected_three": ["Not-On-Team"], "lead": "Not-On-Team"},
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


def stale_envelope_factory(job: JobEnvelope, result: SanitizedProviderResult) -> ResultEnvelope:
    """Tamper the session identity so the result is rejected as stale."""

    return replace(make_envelope(job, result), session_id=str(uuid4()))


def build_controller(
    tmp_path: Path,
    transport: FakeSelectionAdviceTransport,
    *,
    dispatch_factory: object = SyncDispatch,
    envelope_factory: object = make_envelope,
    load_config: object = lambda: TEST_CONFIG,
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
        load_config,  # type: ignore[arg-type]
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


def job_count(repository: SQLiteRepository) -> int:
    row = repository.connection.execute(
        "SELECT COUNT(*) FROM async_jobs WHERE job_type = ?",
        (JobType.SELECTION_ADVICE.value,),
    ).fetchone()
    return int(row[0])


def ledger_row_count(repository: SQLiteRepository) -> int:
    row = repository.connection.execute(
        "SELECT COUNT(*) FROM gemini_selection_attempt_ledger"
    ).fetchone()
    return int(row[0])


def _assert_second_activation_zero_delta(
    controller: MatchFlowController,
    repository: SQLiteRepository,
    adapter: GeminiSelectionAdviceAdapter,
    transport: FakeSelectionAdviceTransport,
) -> None:
    jobs_before = job_count(repository)
    dispatch_before = adapter.dispatch_count
    network_before = adapter.network_call_count
    calls_before = transport.call_count

    assert controller.gemini_selection_attempt_consumed() is True
    result = controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    assert result.error_message is not None
    assert job_count(repository) == jobs_before
    assert adapter.dispatch_count == dispatch_before
    assert adapter.network_call_count == network_before
    assert transport.call_count == calls_before


# ---------------------------------------------------------------------------
# Terminal matrix: success, network failure, HTTP failure, timeout, invalid
# response, stale response.
# ---------------------------------------------------------------------------


def test_second_activation_after_success_yields_zero_new_attempts(tmp_path: Path) -> None:
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    assert controller.selection_advice_status().status == "SUCCESS"

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_network_failure_yields_zero_new_attempts(
    tmp_path: Path,
) -> None:
    transport = FakeSelectionAdviceTransport(
        responses=[ProviderTransportError("GEMINI_NETWORK_ERROR:refused")]
    )
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    first = controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    assert first.error_message is not None
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.status is JobStatus.FAILED

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_http_failure_yields_zero_new_attempts(tmp_path: Path) -> None:
    transport = FakeSelectionAdviceTransport(
        responses=[ProviderTransportError("GEMINI_HTTP_ERROR:503")]
    )
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.status is JobStatus.FAILED

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_timeout_yields_zero_new_attempts(tmp_path: Path) -> None:
    transport = FakeSelectionAdviceTransport(responses=[ProviderTransportError("GEMINI_TIMEOUT")])
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.status is JobStatus.TIMED_OUT

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_invalid_response_yields_zero_new_attempts(
    tmp_path: Path,
) -> None:
    transport = FakeSelectionAdviceTransport(responses=[invalid_result()])
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.status is JobStatus.FAILED

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_stale_response_yields_zero_new_attempts(tmp_path: Path) -> None:
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path, transport, envelope_factory=stale_envelope_factory
    )
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job = repository.get_job(cast(str, adapter.last_job_id))
    assert job.status is JobStatus.FAILED

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_reload_then_trusted_reactivation_yields_zero_new_attempts(tmp_path: Path) -> None:
    qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[ProviderTransportError("GEMINI_NETWORK_ERROR:refused")]
    )
    repository, _application, adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    window = MatchFlowWindow(controller)
    window.show()
    controller.send_selection_advice_to_gemini(on_result=window.render_view)
    window.render_view()

    for _ in range(3):
        window.render_view()
        controller.refresh()

    assert window.gemini_send_button.isEnabled() is False
    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    window.close()
    repository.close()


# ---------------------------------------------------------------------------
# Process restart matrix
# ---------------------------------------------------------------------------


def test_process_restart_after_failure_yields_zero_new_attempts(tmp_path: Path) -> None:
    qapp = qt_application()
    database_path = tmp_path / "maple.db"
    exports_path = tmp_path / "exports"
    repo_root = tmp_path / "repository-root"

    transport = FakeSelectionAdviceTransport(
        responses=[ProviderTransportError("GEMINI_NETWORK_ERROR:refused")]
    )
    repository = SQLiteRepository(database_path)
    application = MatchApplication(repository, exports_path, repository_root=repo_root)
    adapter = GeminiSelectionAdviceAdapter(transport, lambda: TEST_CONFIG)
    controller = MatchFlowController(
        application, repository, MockSelectionAdviceAdapter(), gemini_adapter=adapter
    )
    window = MatchFlowWindow(controller)
    window.show()

    ready_selection(controller)
    controller.send_selection_advice_to_gemini(on_result=window.render_view)
    pump_until(qapp, lambda: window._controller.refresh().error_message is not None)  # noqa: SLF001
    window.render_view()
    assert transport.call_count == 1
    assert window.gemini_send_button.isEnabled() is False

    window.close()
    repository.close()

    restarted_transport = FakeSelectionAdviceTransport()
    restarted_repository = SQLiteRepository(database_path)
    restarted_application = MatchApplication(
        restarted_repository, exports_path, repository_root=repo_root
    )
    restarted_application.recover_after_restart()
    restarted_adapter = GeminiSelectionAdviceAdapter(
        restarted_transport, lambda: TEST_CONFIG
    )
    restarted_controller = MatchFlowController(
        restarted_application,
        restarted_repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=restarted_adapter,
    )
    restarted_window = MatchFlowWindow(restarted_controller)
    restarted_window.show()
    restarted_window.render_view()

    assert restarted_window.gemini_send_button.isEnabled() is False
    _assert_second_activation_zero_delta(
        restarted_controller, restarted_repository, restarted_adapter, restarted_transport
    )

    restarted_window.close()
    restarted_repository.close()


# ---------------------------------------------------------------------------
# FAILED / TIMED_OUT / INTERRUPTED job hydration (models a crash boundary via
# the existing synthetic-only ``set_job_status_for_test`` seam).
# ---------------------------------------------------------------------------


def _hydrate_in_flight_job_and_assert_second_activation_blocked(
    tmp_path: Path, hydrated_status: JobStatus
) -> None:
    factory = HeldDispatchFactory()
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path, transport, dispatch_factory=factory
    )
    ready_selection(controller)

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job_id = adapter.last_job_id
    assert job_id is not None
    assert repository.get_job(job_id).status is JobStatus.IN_FLIGHT

    repository.set_job_status_for_test(job_id, hydrated_status)
    assert repository.get_job(job_id).status is hydrated_status

    _assert_second_activation_zero_delta(controller, repository, adapter, transport)
    repository.close()


def test_second_activation_after_failed_job_hydration_yields_zero_new_attempts(
    tmp_path: Path,
) -> None:
    _hydrate_in_flight_job_and_assert_second_activation_blocked(tmp_path, JobStatus.FAILED)


def test_second_activation_after_timed_out_job_hydration_yields_zero_new_attempts(
    tmp_path: Path,
) -> None:
    _hydrate_in_flight_job_and_assert_second_activation_blocked(tmp_path, JobStatus.TIMED_OUT)


def test_second_activation_after_interrupted_job_hydration_yields_zero_new_attempts(
    tmp_path: Path,
) -> None:
    _hydrate_in_flight_job_and_assert_second_activation_blocked(tmp_path, JobStatus.INTERRUPTED)


# ---------------------------------------------------------------------------
# Missing config semantics: unconsumed until a production job is actually
# reserved.
# ---------------------------------------------------------------------------


def test_missing_config_never_consumes_an_attempt(tmp_path: Path) -> None:
    def failing_config() -> ProviderConfig:
        raise ProviderConfigError("GEMINI_API_KEY_MISSING")

    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, _application, adapter, controller = build_controller(
        tmp_path, transport, load_config=failing_config
    )
    ready_selection(controller)

    first = controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    assert first.error_message is not None
    assert ledger_row_count(repository) == 0
    assert job_count(repository) == 0
    assert adapter.dispatch_count == 0
    assert adapter.network_call_count == 0
    assert transport.call_count == 0
    assert controller.gemini_selection_attempt_consumed() is False

    working_adapter = GeminiSelectionAdviceAdapter(
        transport, lambda: TEST_CONFIG, dispatch_factory=SyncDispatch
    )
    controller._gemini_adapter = working_adapter  # noqa: SLF001 - swap in a working config
    second = controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    assert second.error_message is None
    assert job_count(repository) == 1
    assert working_adapter.dispatch_count == 1
    assert transport.call_count == 1
    repository.close()


# ---------------------------------------------------------------------------
# Atomic duplicate reservation
# ---------------------------------------------------------------------------


def test_duplicate_reservation_for_same_identity_succeeds_at_most_once(tmp_path: Path) -> None:
    transport = FakeSelectionAdviceTransport(responses=[valid_result()])
    repository, application, _adapter, controller = build_controller(tmp_path, transport)
    ready_selection(controller)

    first_job = application.reserve_gemini_selection_attempt("cmd-1")
    assert isinstance(first_job, JobEnvelope)

    raised = False
    try:
        application.reserve_gemini_selection_attempt("cmd-2")
    except DomainError as error:
        raised = True
        assert str(error) == "GEMINI_SELECTION_ATTEMPT_CONSUMED"
    assert raised is True

    assert ledger_row_count(repository) == 1
    assert job_count(repository) == 1
    repository.close()
