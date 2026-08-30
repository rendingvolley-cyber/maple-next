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
    DEFAULT_SELECTION_FALLBACK_MODEL,
    DEFAULT_SELECTION_PRIMARY_MODEL,
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderConfigError,
    ProviderTransportError,
    SanitizedProviderResult,
    SelectionProviderConfig,
    load_provider_config_from_env,
    load_selection_provider_config_from_env,
)
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import (
    GeminiSelectionAdviceAdapter,
    is_selection_fallback_eligible,
)

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


def test_primary_success_atomically_persists_dispatched_model(tmp_path: Path) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="provider-body-cannot-select-model",
            )
        ]
    )
    repository, _application, controller = build_controller(
        tmp_path,
        transport,
        load_config=lambda: SelectionProviderConfig(api_key="test-key"),
    )
    ready_selection_open(controller)

    send_and_wait(qapp, controller)

    session = repository.load_active_session()
    assert session is not None
    assert session.current_selection_advice_id is not None
    job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert repository.selection_provider_attempt_audits(job.job_id) == [
        (1, DEFAULT_SELECTION_PRIMARY_MODEL, "SUCCEEDED", "")
    ]
    stored = repository.get_selection_advice(session.current_selection_advice_id)
    assert stored["model"] == DEFAULT_SELECTION_PRIMARY_MODEL
    repository.close()


def test_model_persistence_failure_rolls_back_entire_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="provider-body-cannot-select-model",
            )
        ]
    )
    repository, _application, controller = build_controller(
        tmp_path,
        transport,
        load_config=lambda: SelectionProviderConfig(api_key="test-key"),
    )
    ready_selection_open(controller)
    before = repository.load_active_session()
    assert before is not None
    original_append = repository.append_selection_advice

    def append_then_fail(*args: object, **kwargs: object) -> None:
        assert kwargs["model"] == DEFAULT_SELECTION_PRIMARY_MODEL
        original_append(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("INJECTED_MODEL_PERSISTENCE_FAILURE")

    monkeypatch.setattr(repository, "append_selection_advice", append_then_fail)

    with pytest.raises(RuntimeError, match="INJECTED_MODEL_PERSISTENCE_FAILURE"):
        controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.IN_FLIGHT
    advice_count = repository.connection.execute(
        "SELECT COUNT(*) FROM selection_advices WHERE job_id = ?", (job.job_id,)
    ).fetchone()[0]
    assert advice_count == 0
    assert repository.result_audits(job.job_id) == []
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
        # exact-type validation: selected_three must be a list of exact strings
        {"selected_three": "Meowscarada,Gholdengo,Dragonite", "lead": "Meowscarada"},
        {"selected_three": ("Meowscarada", "Gholdengo", "Dragonite"), "lead": "Meowscarada"},
        {"selected_three": ["Meowscarada", "Gholdengo", 3], "lead": "Meowscarada"},
        {"selected_three": ["Meowscarada", "Gholdengo", True], "lead": "Meowscarada"},
        {"selected_three": ["Meowscarada", "Gholdengo", None], "lead": "Meowscarada"},
        # exact-type validation: lead must be an exact string
        {"selected_three": ["Meowscarada", "Gholdengo", "Dragonite"], "lead": 1},
        {"selected_three": ["Meowscarada", "Gholdengo", "Dragonite"], "lead": True},
        {"selected_three": ["Meowscarada", "Gholdengo", "Dragonite"], "lead": None},
        # exact key set: unknown field must not be silently used
        {
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite"],
            "lead": "Meowscarada",
            "confidence": 0.9,
        },
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


# --- 14. transient failures cascade; non-transient parse errors fail closed --


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ProviderTransportError("GEMINI_TIMEOUT"), JobStatus.TIMED_OUT),
        (ProviderTransportError("GEMINI_HTTP_ERROR:500"), JobStatus.FAILED),
        (ProviderTransportError("GEMINI_NETWORK_ERROR:unreachable"), JobStatus.FAILED),
        (ProviderTransportError("GEMINI_RESPONSE_ENVELOPE_MALFORMED"), JobStatus.FAILED),
    ],
)
def test_transport_failure_uses_only_contract_allowed_fallback(
    tmp_path: Path, error: ProviderTransportError, expected_status: JobStatus
) -> None:
    qapp = qt_application()
    eligible = is_selection_fallback_eligible(str(error))
    transport = FakeSelectionAdviceTransport(responses=[error, error] if eligible else [error])
    repository, application, controller = build_controller(tmp_path, transport)
    ready_selection_open(controller)
    before = repository.load_active_session()
    assert before is not None

    results = send_and_wait(qapp, controller)

    assert transport.call_count == (2 if eligible else 1)
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is expected_status
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


def test_selection_config_has_exact_lane_specific_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "1")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_MODEL", "must-not-route-either-lane")
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_SELECTION_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_SELECTION_FALLBACK_MODEL", raising=False)

    config = load_selection_provider_config_from_env()

    assert config.primary_model == DEFAULT_SELECTION_PRIMARY_MODEL
    assert config.fallback_model == DEFAULT_SELECTION_FALLBACK_MODEL


@pytest.mark.parametrize(
    ("reason", "eligible"),
    [
        ("GEMINI_HTTP_ERROR:429", True),
        ("GEMINI_HTTP_ERROR:429|STATUS=RESOURCE_EXHAUSTED", True),
        ("GEMINI_HTTP_ERROR:503|REASON=MODEL_CAPACITY_EXHAUSTED", True),
        ("GEMINI_HTTP_ERROR:503|STATUS=UNAVAILABLE", False),
        ("GEMINI_HTTP_ERROR:503|DOMAIN=RESOURCE_EXHAUSTED", False),
        ("GEMINI_HTTP_ERROR:500", False),
        ("GEMINI_NETWORK_ERROR", False),
        ("GEMINI_TIMEOUT", False),
        ("GEMINI_RESPONSE_ENVELOPE_MALFORMED", False),
        ("GEMINI_FAILURE_UNCLASSIFIED", False),
    ],
)
def test_selection_fallback_classifier_is_narrow(reason: str, eligible: bool) -> None:
    assert is_selection_fallback_eligible(reason) is eligible


def test_selection_eligible_failure_uses_one_bounded_fallback_and_audits_both(
    tmp_path: Path,
) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            ProviderTransportError("GEMINI_HTTP_ERROR:429|STATUS=RESOURCE_EXHAUSTED"),
            SanitizedProviderResult(
                payload={"selected_three": list(GEMINI_THREE), "lead": "Meowscarada"},
                source_type=GEMINI_SOURCE_TYPE,
                model="provider-body-cannot-select-model",
            ),
        ]
    )
    routing = SelectionProviderConfig(api_key="test-key")
    repository, _application, controller = build_controller(
        tmp_path, transport, load_config=lambda: routing
    )
    ready_selection_open(controller)

    send_and_wait(qapp, controller)

    assert [config.model for _request, config in transport.calls] == [
        DEFAULT_SELECTION_PRIMARY_MODEL,
        DEFAULT_SELECTION_FALLBACK_MODEL,
    ]
    session = repository.load_active_session()
    assert session is not None
    job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert repository.selection_provider_attempt_audits(job.job_id) == [
        (
            1,
            DEFAULT_SELECTION_PRIMARY_MODEL,
            "FAILED",
            "GEMINI_HTTP_ERROR:429|STATUS=RESOURCE_EXHAUSTED",
        ),
        (2, DEFAULT_SELECTION_FALLBACK_MODEL, "SUCCEEDED", ""),
    ]
    stored = repository.get_selection_advice(cast(str, session.current_selection_advice_id))
    assert stored["model"] == DEFAULT_SELECTION_FALLBACK_MODEL
    repository.close()


def test_selection_second_fallback_failure_stops_at_exactly_two_attempts(
    tmp_path: Path,
) -> None:
    qapp = qt_application()
    transport = FakeSelectionAdviceTransport(
        responses=[
            ProviderTransportError("GEMINI_HTTP_ERROR:429"),
            ProviderTransportError("GEMINI_HTTP_ERROR:429"),
        ]
    )
    repository, _application, controller = build_controller(
        tmp_path,
        transport,
        load_config=lambda: SelectionProviderConfig(api_key="test-key"),
    )
    ready_selection_open(controller)

    send_and_wait(qapp, controller)

    assert transport.call_count == 2
    session = repository.load_active_session()
    assert session is not None
    job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert len(repository.selection_provider_attempt_audits(job.job_id)) == 2
    assert job.status is JobStatus.FAILED
    repository.close()


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


@pytest.fixture
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: any accidental real HTTP attempt fails the test instantly
    instead of hanging on a live socket/timeout."""

    import urllib.request

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("real network access attempted during a hermetic test")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


# --- R3R3: Selection provider authorization safety gate ---------------------
#
# Incident: a runtime with MAPLE_NEXT_GEMINI_TURN_AUTHORIZED=0 but a real
# MAPLE_NEXT_GEMINI_API_KEY in the environment still made one live Selection
# Advice HTTP request (GEMINI_TIMEOUT after 30s), because
# load_selection_provider_config_from_env() only ever checked the API key.
# Merely possessing a key must never authorize network access -- Selection
# now requires its own explicit MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED=="1",
# independent of the Turn lane's own MAPLE_NEXT_GEMINI_TURN_AUTHORIZED.


def test_selection_auth_absent_with_key_present_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", raising=False)
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    with pytest.raises(ProviderConfigError, match="GEMINI_SELECTION_NOT_AUTHORIZED"):
        load_selection_provider_config_from_env()


def test_selection_auth_zero_with_key_present_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "0")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    with pytest.raises(ProviderConfigError, match="GEMINI_SELECTION_NOT_AUTHORIZED"):
        load_selection_provider_config_from_env()


def test_selection_authorized_but_key_missing_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "1")
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError, match="GEMINI_API_KEY_MISSING"):
        load_selection_provider_config_from_env()


def test_selection_authorized_and_key_present_returns_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "1")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    config = load_selection_provider_config_from_env()
    assert config.api_key == "real-looking-key"
    assert config.primary_model == DEFAULT_SELECTION_PRIMARY_MODEL


def test_legacy_load_provider_config_from_env_inherits_selection_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backward-compatible loader must not be a bypass: an API key alone
    must never return a network-capable ProviderConfig through this path
    either."""

    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "0")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    with pytest.raises(ProviderConfigError, match="GEMINI_SELECTION_NOT_AUTHORIZED"):
        load_provider_config_from_env()


def test_production_compatible_send_denied_reaches_zero_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_network: None
) -> None:
    """R3R3 decisive incident regression: the exact incident environment
    (a real-looking API key present, Selection authorization absent/0, Turn
    authorization also 0) driven through the real env loader and the real
    explicit-send adapter path -- not an injected fake config -- must
    produce zero transport calls and zero new async job rows, and must
    never surface as a timeout/network-error label."""

    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "0")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_TURN_AUTHORIZED", "0")

    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(
        tmp_path, transport, load_config=load_selection_provider_config_from_env
    )
    ready_selection_open(controller)

    jobs_before = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]
    audits_before = repository.connection.execute(
        "SELECT COUNT(*) FROM provider_attempt_audits"
    ).fetchone()[0]

    view = controller.send_selection_advice_to_gemini(on_result=lambda _v: None)
    qapp.processEvents()

    # HTTP/provider transport: zero attempts, zero fallback attempts (the
    # fake transport records every call regardless of which model/lane).
    assert transport.call_count == 0

    jobs_after = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]
    audits_after = repository.connection.execute(
        "SELECT COUNT(*) FROM provider_attempt_audits"
    ).fetchone()[0]
    assert jobs_after == jobs_before
    assert audits_after == audits_before

    assert view.error_message is not None
    assert "GEMINI_TIMEOUT" not in view.error_message
    assert "NETWORK_ERROR" not in view.error_message
    repository.close()


def test_fake_authorized_send_reaches_exactly_one_injectable_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair must not break the normal authorized lane: with
    MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED=1 and a fake (non-real) key, the
    real env loader must still resolve and the existing human Selection
    Advice action must still reach the injectable fake transport exactly
    once -- never real Gemini."""

    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "fake-hermetic-key")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "1")

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
    repository, application, controller = build_controller(
        tmp_path, transport, load_config=load_selection_provider_config_from_env
    )
    ready_selection_open(controller)

    results = send_and_wait(qapp, controller)

    assert transport.call_count == 1
    assert results[0].error_message is None  # type: ignore[attr-defined]
    repository.close()


@pytest.mark.parametrize(
    ("selection_auth", "turn_auth", "selection_allowed", "turn_allowed"),
    [
        ("0", "0", False, False),
        ("1", "0", True, False),
        ("0", "1", False, True),
        ("1", "1", True, True),
    ],
)
def test_selection_and_turn_authorization_are_independent(
    monkeypatch: pytest.MonkeyPatch,
    selection_auth: str,
    turn_auth: str,
    selection_allowed: bool,
    turn_allowed: bool,
) -> None:
    from maple_next.providers.turn_transport import load_authorized_turn_provider_config_from_env

    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "real-looking-key")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", selection_auth)
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_TURN_AUTHORIZED", turn_auth)

    if selection_allowed:
        load_selection_provider_config_from_env()
    else:
        with pytest.raises(ProviderConfigError, match="GEMINI_SELECTION_NOT_AUTHORIZED"):
            load_selection_provider_config_from_env()

    if turn_allowed:
        load_authorized_turn_provider_config_from_env()
    else:
        with pytest.raises(ProviderConfigError, match="GEMINI_TURN_NOT_AUTHORIZED"):
            load_authorized_turn_provider_config_from_env()


def test_confirm_and_review_actions_never_dispatch_when_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_network: None
) -> None:
    """Non-send actions (new_match/confirm_selection_facts) must never touch
    the provider loader at all, authorized or not."""

    monkeypatch.delenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", raising=False)
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_API_KEY", raising=False)

    transport = FakeSelectionAdviceTransport()
    repository, application, controller = build_controller(
        tmp_path, transport, load_config=load_selection_provider_config_from_env
    )
    ready_selection_open(controller)

    assert transport.call_count == 0
    jobs = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]
    assert jobs == 0
    repository.close()
    repository.close()
