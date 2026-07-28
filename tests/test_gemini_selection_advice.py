from __future__ import annotations

import os
import time
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import BattleState, JobStatus, JobType, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.selection_request import request_payload_hash
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderConfigError,
    ProviderTransportError,
    SanitizedProviderResult,
    load_provider_config_from_env,
)
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
GEMINI_THREE = ("Meowscarada", "Gholdengo", "Dragonite")
HUMAN_THREE = ("Dondozo", "Flutter Mane", "Urshifu")

TEST_CONFIG = ProviderConfig(api_key="test-key", model="test-model", timeout_seconds=5.0)


class SyncDispatch:
    """Test-only same-thread stand-in for ``SelectionAdviceDispatch``.

    These tests exercise the application/binding/adapter contract (request
    shape, strict validation, fail-closed behavior, restart recovery) which
    does not depend on which thread ``transport.send`` actually runs on.
    Real cross-thread dispatch (the production ``QThread``-based worker) is
    covered separately and narrowly in test_gemini_selection_advice_window.py.
    """

    def __init__(
        self,
        transport: FakeSelectionAdviceTransport,
        request: object,
        config: ProviderConfig,
        *,
        on_succeeded: object,
        on_failed: object,
    ) -> None:
        self._transport = transport
        self._request = request
        self._config = config
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed

    def start(self) -> None:
        try:
            result = self._transport.send(self._request, self._config)  # type: ignore[arg-type]
        except ProviderTransportError as exc:
            self._on_failed(str(exc))  # type: ignore[operator]
        else:
            self._on_succeeded(result)  # type: ignore[operator]


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


def build_controller(
    tmp_path: Path,
    transport: FakeSelectionAdviceTransport,
    *,
    load_config: object = lambda: TEST_CONFIG,
) -> tuple[SQLiteRepository, BattleApplication, SelectionFlowController]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    gemini_adapter = GeminiSelectionAdviceAdapter(
        transport,
        load_config,  # type: ignore[arg-type]
        dispatch_factory=SyncDispatch,  # type: ignore[arg-type]
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=gemini_adapter,
    )
    return repository, application, controller


def ready_selection_open(controller: SelectionFlowController) -> None:
    controller.new_match()
    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    assert view.error_message is None
    assert view.projection.session_state == "SELECTION_OPEN"


def send_and_wait(
    qapp: QApplication,
    controller: SelectionFlowController,
) -> list[object]:
    results: list[object] = []
    controller.send_selection_advice_to_gemini(on_result=results.append)
    pump_until(qapp, lambda: len(results) >= 1)
    return results


# --- 1. reviewed facts required before send; network 0 ----------------------


def test_send_before_reviewed_facts_is_refused_with_zero_network(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    controller.new_match()  # SELECTION_OPEN but no reviewed facts yet

    view = controller.send_selection_advice_to_gemini(on_result=lambda _v: None)
    qapp.processEvents()

    assert view.error_message is not None
    assert transport.call_count == 0
    repository.close()


# --- 9/10. fake transport receives canonical 6v6 + fixed identity, deterministic hash --


def test_fake_transport_request_contains_canonical_teams_and_identity(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    session = repository.load_active_session()
    assert session is not None

    send_and_wait(qapp, controller)

    assert transport.call_count == 1
    request, config = transport.calls[0]
    assert request.self_team == SELF_TEAM
    assert request.opponent_team == OPPONENT_TEAM
    assert request.session_id == session.session_id
    assert request.match_id == session.match_id
    assert request.generation == session.generation
    assert request.reviewed_selection_id == session.current_reviewed_selection_id
    assert config.api_key == "test-key"

    job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.request_payload_hash == request_payload_hash(request)
    repository.close()


# --- 11. strict valid JSON result applied via existing ResultEnvelope path ---


def test_valid_gemini_result_is_applied_and_marked_gemini_source(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)

    send_and_wait(qapp, controller)
    view = controller.refresh()

    assert view.projection.session_state == "SELECTION_ADVICE_READY"
    assert view.advice is not None
    assert view.advice.selected_three == GEMINI_THREE
    assert view.advice.lead == "Meowscarada"
    assert view.advice.source_type == GEMINI_SOURCE_TYPE
    repository.close()


# --- 12. duplicate / outside-team / lead-mismatch / malformed -> INVALID_REJECTED ---


@pytest.mark.parametrize(
    "payload",
    [
        {"selected_three": ["Meowscarada", "Meowscarada", "Dragonite"], "lead": "Meowscarada"},
        {"selected_three": ["Meowscarada", "Gholdengo", "MissingNo"], "lead": "Meowscarada"},
        {"selected_three": ["Meowscarada", "Gholdengo", "Dragonite"], "lead": "Dondozo"},
        {},
        {"selected_three": ["Meowscarada", "Gholdengo"], "lead": "Meowscarada"},
    ],
)
def test_invalid_gemini_payload_rejected_without_state_mutation(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload=payload, source_type=GEMINI_SOURCE_TYPE, model="test-model"
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    before = repository.load_active_session()
    assert before is not None

    send_and_wait(qapp, controller)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.FAILED
    repository.close()


# --- 13. stale revision / snapshot / wrong identity result -> STALE_REJECTED ---


def test_stale_identity_result_rejected_via_existing_binding_contract(tmp_path: Path) -> None:
    from uuid import uuid4

    from maple_next.workers.contracts.models import ResultEnvelope

    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    session = repository.load_active_session()
    assert session is not None
    job = application.request_selection_advice("human-1")

    tampered = ResultEnvelope(
        contract_version=job.contract_version,
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation + 1,  # wrong identity
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
        source_type=GEMINI_SOURCE_TYPE,
    )
    disposition = application.apply_selection_advice_result(tampered)
    assert disposition is ResultDisposition.STALE_REJECTED
    qapp.processEvents()
    repository.close()


# --- 14. timeout / HTTP error / parse error -> fail-closed, no retry, no fallback ---


@pytest.mark.parametrize(
    "error",
    [
        ProviderTransportError("GEMINI_TIMEOUT"),
        ProviderTransportError("GEMINI_HTTP_ERROR:500"),
        ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED"),
    ],
)
def test_transport_failure_fails_closed_without_retry_or_fallback(
    tmp_path: Path, error: ProviderTransportError
) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(responses=[error])
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    before = repository.load_active_session()
    assert before is not None

    results = send_and_wait(qapp, controller)

    assert transport.call_count == 1  # no automatic retry
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.FAILED
    view = controller.refresh()
    assert view.error_message is not None
    assert results
    repository.close()


# --- 15. missing API key -> zero network, Japanese corrective error ----------


def test_missing_api_key_yields_zero_network_and_japanese_error(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()

    def raise_missing_key() -> ProviderConfig:
        raise ProviderConfigError("GEMINI_API_KEY_MISSING")

    repository, application, controller = build_controller(
        tmp_path, transport, load_config=raise_missing_key
    )
    ready_selection_open(controller)
    jobs_before = repository.connection.execute(
        "SELECT COUNT(*) FROM async_jobs"
    ).fetchone()[0]

    view = controller.send_selection_advice_to_gemini(on_result=lambda _v: None)
    qapp.processEvents()

    assert transport.call_count == 0
    jobs_after = repository.connection.execute(
        "SELECT COUNT(*) FROM async_jobs"
    ).fetchone()[0]
    assert jobs_after == jobs_before
    assert view.error_message is not None
    assert any(ch >= "぀" for ch in view.error_message)  # contains Japanese text
    repository.close()


def test_load_provider_config_from_env_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        load_provider_config_from_env()


# --- 16. API key / auth header / raw response never leak to UI/DB/log -------


def test_no_secret_leakage_into_repository_or_ui(tmp_path: Path) -> None:
    qapp = qt_application()
    secret = "sk-super-secret-token-should-never-leak"
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, application, controller = build_controller(
        tmp_path, transport, load_config=lambda: ProviderConfig(api_key=secret, model="m")
    )
    ready_selection_open(controller)
    send_and_wait(qapp, controller)
    view = controller.refresh()

    assert secret not in repr(view)
    assert secret not in repr(view.advice)
    dump = repository.connection.iterdump()
    assert all(secret not in line for line in dump)
    repository.close()


# --- 17. pending / DELIVERY_UNKNOWN disables the send CTA --------------------


def test_provider_send_disabled_while_pending(tmp_path: Path) -> None:
    qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    session = repository.load_active_session()
    assert session is not None
    job = application.request_selection_advice("human-1")
    application.mark_selection_advice_dispatched(job.job_id)

    view = controller.refresh()
    assert view.projection.provider_send_enabled is False
    repository.close()


def test_delivery_unknown_disables_send(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    session = repository.load_active_session()
    assert session is not None
    job = application.request_selection_advice("human-1")
    repository.set_job_status_for_test(job.job_id, JobStatus.IN_FLIGHT)
    repository.close()

    restarted_repo = SQLiteRepository(tmp_path / "maple.db")
    restarted_app = BattleApplication(restarted_repo)
    restarted_app.recover_after_restart()
    projection = restarted_app.projection()
    assert projection.primary_cta == "RESOLVE_DELIVERY_UNKNOWN"
    assert projection.provider_send_enabled is False
    qapp.processEvents()
    restarted_repo.close()


# --- 18. restart never auto-dispatches a QUEUED/IN_FLIGHT job ---------------


def test_restart_does_not_auto_dispatch_queued_job(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    application.request_selection_advice("human-1")
    repository.close()

    restarted_repo = SQLiteRepository(tmp_path / "maple.db")
    restarted_app = BattleApplication(restarted_repo)
    restarted_app.recover_after_restart()
    session = restarted_repo.load_active_session()
    assert session is not None
    job = restarted_repo.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.INTERRUPTED
    assert transport.call_count == 0
    qapp.processEvents()
    restarted_repo.close()


# --- 19. human APPLY can diverge from the Gemini suggestion ------------------


def test_human_can_apply_different_legal_selection_than_gemini(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    send_and_wait(qapp, controller)

    view = controller.apply_selection(HUMAN_THREE, "Flutter Mane", human_confirmed=True)
    assert view.error_message is None
    assert view.applied_selection is not None
    assert view.applied_selection.selected_three == HUMAN_THREE
    assert view.applied_selection.selected_three != GEMINI_THREE
    repository.close()


# --- 20. Turn Advice remains MOCK; zero production Turn provider sends ------


def test_turn_advice_stays_mock_alongside_gemini_selection(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="test-model",
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    send_and_wait(qapp, controller)
    controller.apply_selection(GEMINI_THREE, "Meowscarada", human_confirmed=True)
    controller.start_turn_capture()
    view = controller.confirm_turn_facts(
        self_active="Meowscarada",
        opponent_active="Garchomp",
        self_hp="100",
        opponent_hp="100",
        legal_moves=["Flower Trick"],
        legal_switches=["Gholdengo"],
        human_note="",
        human_confirmed=True,
    )
    assert view.error_message is None
    view = controller.submit_mock_turn_advice(
        action_type="MOVE",
        action_name="Flower Trick",
        opponent_prediction="Switch",
        rationale="STAB",
    )
    assert view.error_message is None
    assert view.turn_advice is not None
    assert view.turn_advice.is_mock is True

    # Selection network came from Gemini adapter (fake transport); Turn network is
    # exclusively the mock adapter, which never touches a network transport at all.
    assert transport.call_count == 1
    assert controller._mock_turn_adapter.network_call_count == 0  # noqa: SLF001
    repository.close()
