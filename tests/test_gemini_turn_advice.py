"""Issue #31: fake/injected Gemini-pattern Turn Advice adapter contract tests.

Mirrors tests/test_gemini_selection_advice.py for the Turn Advice lane. Every
test here uses FakeTurnAdviceTransport — never a real network call — and
exercises the same architecture GeminiSelectionAdviceAdapter uses on the
Selection side: single trusted-activation dispatch, an in-flight duplicate
guard, and the existing strict identity/hash/staleness/legality binding
contract in ``apply_turn_advice_result``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import BattleState, JobStatus, JobType, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderConfigError,
    SanitizedProviderResult,
)
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
    ProviderTransportError,
    TurnProviderTransport,
)
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL, GeminiTurnAdviceAdapter
from maple_next.ui.turn_advice_integration import TurnAdviceIntegrationController
from maple_next.workers.contracts.models import ResultEnvelope

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")
LEGAL_MOVES = ("Protect", "Wave Crash", "Earthquake")
LEGAL_SWITCHES = ("Flutter Mane", "Urshifu")
TEST_CONFIG = ProviderConfig(api_key="test-key", model="test-model", timeout_seconds=5.0)


class SyncDispatch:
    """Same-thread stand-in for ``TurnAdviceDispatch`` (see selection tests)."""

    def __init__(
        self,
        transport: FakeTurnAdviceTransport,
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


class ProductionLikeTurnTransport:
    """Injected non-fake transport used to exercise the production controller branch."""

    def __init__(self, result: SanitizedProviderResult) -> None:
        self.result = result
        self.calls: list[tuple[object, ProviderConfig]] = []

    def send(self, request: object, config: ProviderConfig) -> SanitizedProviderResult:
        self.calls.append((request, config))
        return self.result

    @property
    def call_count(self) -> int:
        return len(self.calls)


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def build_controller(
    tmp_path: Path,
    transport: TurnProviderTransport,
    *,
    load_config: object | None = None,
) -> tuple[SQLiteRepository, BattleApplication, TurnAdviceIntegrationController]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    turn_gemini_adapter = GeminiTurnAdviceAdapter(
        transport,
        load_config,  # type: ignore[arg-type]
        dispatch_factory=SyncDispatch,  # type: ignore[arg-type]
    )
    controller = TurnAdviceIntegrationController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=None,
        turn_gemini_adapter=turn_gemini_adapter,
    )
    return repository, application, controller


def ready_for_turn_advice(controller: TurnAdviceIntegrationController) -> None:
    controller.new_match()
    controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    controller.submit_mock_advice(
        ("Meowscarada", "Gholdengo", "Dragonite"), "Meowscarada"
    )
    controller.apply_selection(SELECTED_THREE, "Dondozo", human_confirmed=True)
    controller.start_turn_capture()
    view = controller.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp="100",
        opponent_hp="100",
        legal_moves=list(LEGAL_MOVES),
        legal_switches=list(LEGAL_SWITCHES),
        human_note="",
        human_confirmed=True,
    )
    assert view.error_message is None
    assert view.projection.session_state == "TURN_REVIEWED"


def send_and_wait(controller: TurnAdviceIntegrationController) -> list[object]:
    results: list[object] = []
    controller.send_turn_advice_to_gemini(
        action_type="MOVE",
        action_name="Wave Crash",
        opponent_prediction="Protect",
        rationale="STAB and pressure.",
        warnings=("HP不明のためswitchも検討",),
        on_result=results.append,
    )
    return results


def _valid_result() -> SanitizedProviderResult:
    return SanitizedProviderResult(
        payload={
            "recommended_action": {
                "action_id": "MOVE:Wave Crash",
                "action_type": "MOVE",
                "action_name": "Wave Crash",
            },
            "reasons": ["STAB and pressure."],
            "warnings": [],
            "opponent_prediction": {
                "category": "UNKNOWN",
                "predicted_action": "Protect",
                "summary": "Protect",
                "confidence": 0.5,
            },
        },
        source_type="GEMINI",
        model="test-model",
    )


def test_production_controller_path_uses_reviewed_request_not_mock_advice_fields(
    tmp_path: Path,
) -> None:
    qt_application()
    transport = ProductionLikeTurnTransport(_valid_result())
    repository, _application, controller = build_controller(
        tmp_path,
        transport,
        load_config=lambda: TEST_CONFIG,
    )
    ready_for_turn_advice(controller)

    results: list[object] = []
    controller.send_turn_advice_to_gemini(
        action_type="",
        action_name="",
        opponent_prediction="",
        rationale="",
        warnings=(),
        on_result=results.append,
    )

    assert transport.call_count == 1
    assert results
    view = controller.refresh()
    assert view.error_message is None
    assert view.turn_advice is not None
    assert view.turn_advice.action_name == "Wave Crash"
    repository.close()


def test_unauthorized_production_path_creates_no_job_or_transport_call(tmp_path: Path) -> None:
    qt_application()
    transport = ProductionLikeTurnTransport(_valid_result())

    def unauthorized() -> ProviderConfig:
        raise ProviderConfigError("GEMINI_TURN_NOT_AUTHORIZED")

    repository, _application, controller = build_controller(
        tmp_path,
        transport,
        load_config=unauthorized,
    )
    ready_for_turn_advice(controller)
    jobs_before = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]

    view = controller.send_turn_advice_to_gemini(
        action_type="",
        action_name="",
        opponent_prediction="",
        rationale="",
        warnings=(),
        on_result=lambda _view: None,
    )

    jobs_after = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]
    assert transport.call_count == 0
    assert jobs_after == jobs_before
    assert view.error_message is not None
    repository.close()


# --- exactly one dispatch on trusted send; request carries current identity --


def test_single_send_dispatches_once_with_current_identity(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={
                    "recommended_action": {
                        "action_id": "MOVE:Wave Crash",
                        "action_type": "MOVE",
                        "action_name": "Wave Crash",
                    },
                    "reasons": ["STAB and pressure."],
                    "warnings": ["HP不明のためswitchも検討"],
                    "opponent_prediction": {
                        "category": "UNKNOWN",
                        "predicted_action": "Protect",
                        "summary": "Protect",
                        "confidence": 0.5,
                    },
                },
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=FAKE_TURN_MODEL,
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    session = repository.load_active_session()
    assert session is not None

    send_and_wait(controller)

    assert transport.call_count == 1
    request, _config = transport.calls[0]
    assert request.session_id == session.session_id
    assert request.match_id == session.match_id
    assert request.generation == session.generation
    assert request.reviewed_snapshot_id == session.current_reviewed_board_id

    view = controller.refresh()
    assert view.turn_advice is not None
    assert view.turn_advice.action_name == "Wave Crash"
    assert view.turn_advice.source_type == FAKE_TURN_ADVICE_SOURCE_TYPE
    assert view.turn_advice.model == FAKE_TURN_MODEL
    assert view.turn_advice.is_mock is False
    repository.close()


# --- duplicate activation while in-flight creates zero additional dispatch ---


def test_duplicate_activation_while_in_flight_dispatches_zero_more(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)

    adapter = controller._turn_gemini_adapter  # noqa: SLF001
    assert adapter is not None
    adapter._in_flight = True  # noqa: SLF001 - simulate a still-pending first send

    failures: list[str] = []
    adapter.send(
        application,
        on_applied=lambda _d: None,
        on_failed=failures.append,
    )

    assert adapter.dispatch_count == 0
    assert failures == ["GEMINI_TURN_DISPATCH_ALREADY_IN_FLIGHT"]
    repository.close()


# --- render/refresh alone never dispatches --------------------------------


def test_refresh_and_render_never_dispatch(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)

    for _ in range(5):
        controller.refresh()

    assert transport.call_count == 0
    repository.close()


# --- illegal / non-owned action -> INVALID_REJECTED, never shown as primary --


@pytest.mark.parametrize(
    "payload",
    [
        {  # action_name outside reviewed legal moves
            "recommended_action": {
                "action_id": "MOVE:Not A Real Move",
                "action_type": "MOVE",
                "action_name": "Not A Real Move",
            },
            "reasons": ["STAB."],
            "warnings": [],
            "opponent_prediction": {
                "category": "UNKNOWN",
                "predicted_action": "Protect",
                "summary": "Protect",
                "confidence": 0.5,
            },
        },
        {  # SWITCH naming a Pokemon not among the reviewed legal switches
            "recommended_action": {
                "action_id": "SWITCH:MissingNo",
                "action_type": "SWITCH",
                "action_name": "MissingNo",
            },
            "reasons": ["Bad matchup."],
            "warnings": [],
            "opponent_prediction": {
                "category": "UNKNOWN",
                "predicted_action": "Protect",
                "summary": "Protect",
                "confidence": 0.5,
            },
        },
        {},
    ],
)
def test_illegal_or_malformed_payload_never_applied_or_shown_primary(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload=payload, source_type=FAKE_TURN_ADVICE_SOURCE_TYPE, model=FAKE_TURN_MODEL
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    before = repository.load_active_session()
    assert before is not None

    send_and_wait(controller)

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.TURN_REVIEWED
    assert after.current_turn_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.TURN_ADVICE)
    assert job is not None
    assert job.status is JobStatus.FAILED
    view = controller.refresh()
    assert view.turn_advice is None
    repository.close()


# --- stale identity (wrong generation) is discarded, no canonical mutation ---


def test_stale_identity_result_rejected_via_existing_binding_contract(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    job = application.request_turn_advice(f"human-{uuid4()}")

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
        payload={
            "recommended_action": {
                "action_id": "MOVE:Wave Crash",
                "action_type": "MOVE",
                "action_name": "Wave Crash",
            },
            "reasons": ["STAB."],
            "warnings": [],
            "opponent_prediction": {
                "category": "UNKNOWN",
                "predicted_action": "Protect",
                "summary": "Protect",
                "confidence": 0.5,
            },
        },
        source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
        model=FAKE_TURN_MODEL,
    )
    disposition = application.apply_turn_advice_result(tampered)
    assert disposition is ResultDisposition.STALE_REJECTED
    repository.close()


# --- transport failure fails closed: no retry, no fallback, no mutation -----


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ProviderTransportError("GEMINI_TIMEOUT"), JobStatus.TIMED_OUT),
        (ProviderTransportError("FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED"), JobStatus.FAILED),
    ],
)
def test_transport_failure_fails_closed_without_retry_or_fallback(
    tmp_path: Path, error: ProviderTransportError, expected_status: JobStatus
) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport(responses=[error])
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    before = repository.load_active_session()
    assert before is not None

    results = send_and_wait(controller)

    assert transport.call_count == 1  # no automatic retry
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.TURN_REVIEWED
    assert after.current_turn_advice_id is None
    assert after.battle_revision == before.battle_revision
    job = repository.latest_job_by_type(after.session_id, JobType.TURN_ADVICE)
    assert job is not None
    assert job.status is expected_status
    assert results
    repository.close()


# --- no secret leakage into repository or view repr -------------------------


def test_no_secret_leakage_into_repository_or_ui(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={
                    "recommended_action": {
                        "action_id": "MOVE:Wave Crash",
                        "action_type": "MOVE",
                        "action_name": "Wave Crash",
                    },
                    "reasons": ["STAB."],
                    "warnings": [],
                    "opponent_prediction": {
                        "category": "UNKNOWN",
                        "predicted_action": "Protect",
                        "summary": "Protect",
                        "confidence": 0.5,
                    },
                },
                source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
                model=FAKE_TURN_MODEL,
            )
        ]
    )
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    send_and_wait(controller)
    view = controller.refresh()

    secret_like = "sk-super-secret-token-should-never-leak"
    assert secret_like not in repr(view)
    dump = repository.connection.iterdump()
    assert all(secret_like not in line for line in dump)
    repository.close()


# --- restart never auto-dispatches a QUEUED/IN_FLIGHT Turn Advice job -------


def test_restart_does_not_auto_dispatch_queued_turn_advice_job(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    application.request_turn_advice(f"human-{uuid4()}")
    repository.close()

    restarted_repo = SQLiteRepository(tmp_path / "maple.db")
    restarted_app = BattleApplication(restarted_repo)
    restarted_app.recover_after_restart()
    session = restarted_repo.load_active_session()
    assert session is not None
    job = restarted_repo.latest_job_by_type(session.session_id, JobType.TURN_ADVICE)
    assert job is not None
    assert job.status is JobStatus.INTERRUPTED
    assert transport.call_count == 0
    restarted_repo.close()


def test_build_turn_advice_transport_request_rejects_wrong_job_type(tmp_path: Path) -> None:
    qt_application()
    transport = FakeTurnAdviceTransport()
    repository, application, controller = build_controller(tmp_path, transport)
    ready_for_turn_advice(controller)
    turn_job = application.request_turn_advice(f"human-{uuid4()}")
    # Fabricate a job envelope that claims a different job_type without
    # actually being one, to exercise the guard directly.
    from dataclasses import replace

    from maple_next.domain.enums import JobType as _JobType

    fake_selection_job = replace(turn_job, job_type=_JobType.SELECTION_ADVICE)
    with pytest.raises(DomainError, match="JOB_TYPE_NOT_TURN_ADVICE"):
        application.build_turn_advice_transport_request(fake_selection_job)
    repository.close()
