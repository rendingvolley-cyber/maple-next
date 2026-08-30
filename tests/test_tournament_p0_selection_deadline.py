from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import JobStatus, JobType
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
    SelectionProviderConfig,
    load_selection_provider_config_from_env,
)
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import (
    SELECTION_HARD_DEADLINE_MS,
    SELECTION_PER_ATTEMPT_TIMEOUT_MS,
    GeminiSelectionAdviceAdapter,
)

SELF_TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT_TEAM = ("G", "H", "I", "J", "K", "L")
MODELS = ("model-1", "model-2", "model-3", "model-4")


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class TimedOutcome:
    seconds: float
    value: SanitizedProviderResult | Exception


@dataclass
class TimedFakeTransport:
    clock: FakeClock
    outcomes: list[TimedOutcome]
    calls: list[ProviderConfig] = field(default_factory=list)
    active_calls: int = 0
    max_active_calls: int = 0

    def send(self, _request: object, config: ProviderConfig) -> SanitizedProviderResult:
        self.calls.append(config)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            outcome = self.outcomes.pop(0)
            elapsed = min(outcome.seconds, config.timeout_seconds)
            self.clock.advance(elapsed)
            if outcome.seconds > config.timeout_seconds:
                raise ProviderTransportError("GEMINI_TIMEOUT")
            if isinstance(outcome.value, Exception):
                raise outcome.value
            return outcome.value
        finally:
            self.active_calls -= 1


class SyncDispatch:
    def __init__(
        self,
        transport: TimedFakeTransport,
        request: object,
        config: ProviderConfig,
        *,
        on_succeeded: Callable[[SanitizedProviderResult], None],
        on_failed: Callable[[str], None],
    ) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)
        except ProviderTransportError as exc:
            self.on_failed(str(exc))
        else:
            self.on_succeeded(result)


class HeldDispatch(SyncDispatch):
    def start(self) -> None:
        return

    def release(self) -> None:
        super().start()


@dataclass
class HeldDispatchFactory:
    dispatches: list[HeldDispatch] = field(default_factory=list)

    def __call__(self, *args: object, **kwargs: object) -> HeldDispatch:
        dispatch = HeldDispatch(*args, **kwargs)  # type: ignore[arg-type]
        self.dispatches.append(dispatch)
        return dispatch


def valid_result() -> SanitizedProviderResult:
    return SanitizedProviderResult(
        payload={"selected_three": ["A", "B", "C"], "lead": "A"},
        source_type=GEMINI_SOURCE_TYPE,
        model="ignored-provider-model",
    )


def routing() -> SelectionProviderConfig:
    return SelectionProviderConfig(
        api_key="test-key",
        primary_model=MODELS[0],
        fallback_model=MODELS[1],
        additional_models=MODELS[2:],
        timeout_seconds=30.0,
    )


def build_controller(
    database_path: Path,
    transport: TimedFakeTransport,
    clock: FakeClock,
    *,
    dispatch_factory: object = SyncDispatch,
) -> tuple[
    SQLiteRepository,
    BattleApplication,
    GeminiSelectionAdviceAdapter,
    SelectionFlowController,
]:
    repository = SQLiteRepository(database_path)
    application = BattleApplication(repository)
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        routing,
        dispatch_factory=dispatch_factory,  # type: ignore[arg-type]
        clock=clock,
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=adapter,
    )
    return repository, application, adapter, controller


def ready(controller: SelectionFlowController) -> None:
    controller.new_match()
    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    assert view.error_message is None


def latest_audits(
    repository: SQLiteRepository,
) -> tuple[JobStatus, list[tuple[int, str, str, str]]]:
    session = repository.load_active_session()
    assert session is not None
    job = repository.latest_job_by_type(session.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    return job.status, repository.selection_provider_attempt_audits(job.job_id)


def test_cases_a_b_c_finish_in_budget_with_sequential_automatic_fallback(
    tmp_path: Path,
) -> None:
    scenarios = [
        ([TimedOutcome(2.0, valid_result())], 2.0, 1),
        ([TimedOutcome(30.0, valid_result()), TimedOutcome(2.0, valid_result())], 9.5, 2),
        (
            [
                TimedOutcome(30.0, valid_result()),
                TimedOutcome(30.0, valid_result()),
                TimedOutcome(4.9, valid_result()),
            ],
            19.9,
            3,
        ),
    ]
    for index, (outcomes, expected_seconds, expected_calls) in enumerate(scenarios):
        clock = FakeClock()
        start = clock()
        transport = TimedFakeTransport(clock, outcomes)
        repository, _application, _adapter, controller = build_controller(
            tmp_path / f"case-{index}.db", transport, clock
        )
        ready(controller)
        controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

        assert clock() - start == pytest.approx(expected_seconds)
        assert len(transport.calls) == expected_calls
        assert transport.max_active_calls == 1
        status, audits = latest_audits(repository)
        assert status is JobStatus.SUCCEEDED
        assert len(audits) == expected_calls
        repository.close()


def test_case_d_all_transient_failures_stop_at_hard_deadline_and_restore_resend(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "case-d.db"
    clock = FakeClock()
    start = clock()
    transport = TimedFakeTransport(
        clock,
        [TimedOutcome(30.0, valid_result()) for _ in MODELS],
    )
    repository, application, _adapter, controller = build_controller(
        database_path, transport, clock
    )
    ready(controller)
    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    assert (clock() - start) * 1000 <= SELECTION_HARD_DEADLINE_MS
    assert [round(call.timeout_seconds * 1000) for call in transport.calls] == [7500, 7500, 5000]
    assert transport.max_active_calls == 1
    status, audits = latest_audits(repository)
    assert status is JobStatus.TIMED_OUT
    assert len(audits) == 3
    assert application.gemini_selection_last_failure_reason() == "GEMINI_TIMEOUT"
    assert controller.gemini_selection_resend_eligible() is True
    repository.close()

    restarted_clock = FakeClock()
    restarted_transport = TimedFakeTransport(restarted_clock, [TimedOutcome(1.0, valid_result())])
    restarted_repository, restarted_application, _adapter, restarted_controller = (
        build_controller(database_path, restarted_transport, restarted_clock)
    )
    restarted_application.recover_after_restart()
    assert restarted_application.gemini_selection_last_failure_reason() == "GEMINI_TIMEOUT"
    assert restarted_controller.gemini_selection_resend_eligible() is True
    restarted_repository.close()


def test_case_e_invalid_response_fails_closed_without_cascade(tmp_path: Path) -> None:
    clock = FakeClock()
    invalid = SanitizedProviderResult(payload={}, source_type=GEMINI_SOURCE_TYPE, model="ignored")
    transport = TimedFakeTransport(clock, [TimedOutcome(1.0, invalid)])
    repository, _application, _adapter, controller = build_controller(
        tmp_path / "case-e.db", transport, clock
    )
    ready(controller)
    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    assert len(transport.calls) == 1
    status, audits = latest_audits(repository)
    assert status is JobStatus.FAILED
    assert len(audits) == 1
    assert controller.gemini_selection_resend_eligible() is False
    repository.close()


def test_case_f_double_activation_while_pending_creates_no_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    transport = TimedFakeTransport(clock, [TimedOutcome(1.0, valid_result())])
    factory = HeldDispatchFactory()
    repository, _application, adapter, controller = build_controller(
        tmp_path / "case-f.db", transport, clock, dispatch_factory=factory
    )
    ready(controller)
    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    assert controller.gemini_selection_progress() == "Gemini応答待ち… 1/4"
    dispatches_before = adapter.dispatch_count
    jobs_before = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]

    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)

    assert adapter.dispatch_count == dispatches_before == 1
    jobs_after = repository.connection.execute("SELECT COUNT(*) FROM async_jobs").fetchone()[0]
    assert jobs_after == jobs_before
    assert len(factory.dispatches) == 1
    assert len(transport.calls) == 0
    factory.dispatches[0].release()
    assert len(transport.calls) == 1
    repository.close()


def test_deadline_constants_match_tournament_contract() -> None:
    assert SELECTION_HARD_DEADLINE_MS == 20_000
    assert SELECTION_PER_ATTEMPT_TIMEOUT_MS == 7_500


def test_existing_configured_chain_is_reused_without_local_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_SELECTION_AUTHORIZED", "1")
    monkeypatch.setenv("MAPLE_NEXT_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv(
        "MAPLE_SELECTION_MODEL_CHAIN",
        "model-1,model-2,model-3,model-4,maple_internal",
    )
    monkeypatch.delenv("MAPLE_NEXT_GEMINI_SELECTION_MODEL_CHAIN", raising=False)

    config = load_selection_provider_config_from_env()

    assert tuple(item.model for item in config.chain()) == MODELS


def test_existing_two_attempt_audit_table_is_widened_without_losing_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    clock = FakeClock()
    transport = TimedFakeTransport(clock, [TimedOutcome(1.0, valid_result())])
    repository, _application, adapter, controller = build_controller(
        database_path, transport, clock
    )
    ready(controller)
    controller.send_selection_advice_to_gemini(on_result=lambda _view: None)
    job_id = adapter.last_job_id
    assert job_id is not None
    original_audits = repository.selection_provider_attempt_audits(job_id)
    repository.close()

    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        ALTER TABLE provider_attempt_audits RENAME TO provider_attempt_audits_wide;
        CREATE TABLE provider_attempt_audits (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal IN (1, 2)),
            model TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('STARTED', 'SUCCEEDED', 'FAILED')),
            reason TEXT NOT NULL DEFAULT '',
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NULL,
            UNIQUE(job_id, lane, attempt_ordinal),
            FOREIGN KEY(job_id) REFERENCES async_jobs(job_id)
        );
        INSERT INTO provider_attempt_audits SELECT * FROM provider_attempt_audits_wide;
        DROP TABLE provider_attempt_audits_wide;
        UPDATE schema_meta SET schema_version = 21 WHERE singleton_id = 1;
        """
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRepository(database_path)
    assert migrated.selection_provider_attempt_audits(job_id) == original_audits
    migrated.start_selection_provider_attempt(
        job_id=job_id,
        attempt_ordinal=3,
        model=MODELS[2],
    )
    assert migrated.selection_provider_attempt_audits(job_id)[-1][:3] == (
        3,
        MODELS[2],
        "STARTED",
    )
    migrated.close()
