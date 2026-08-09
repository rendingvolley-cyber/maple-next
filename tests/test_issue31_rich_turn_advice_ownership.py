"""Regression coverage for the rich Turn Adviser QThread ownership fix.

Only a blocking in-memory transport and an isolated application stub are
used.  No provider or network request can be made by this module.
"""

from __future__ import annotations

import gc
import os
import threading
import time
import weakref
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import BattleState, JobType, ResultDisposition
from maple_next.providers.transport import ProviderConfig, SanitizedProviderResult
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter
from maple_next.workers.contracts.models import ResultEnvelope
from maple_next.workers.turn_advice_worker import TurnAdviceDispatch


class _BlockingTransport:
    """Fake transport that keeps the real worker QThread observably active."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.call_count = 0

    def send(self, request: object, config: ProviderConfig) -> SanitizedProviderResult:
        del request, config
        self.call_count += 1
        self.entered.set()
        assert self.release.wait(5), "test did not release blocking fake transport"
        return SanitizedProviderResult(
            payload={}, source_type="FAKE", model="ownership-regression"
        )


class _ApplicationStub:
    """Minimum isolated application contract required by the adapter."""

    def __init__(self) -> None:
        self.job = SimpleNamespace(
            contract_version="1",
            job_id="ownership-job",
            command_id="ownership-command",
            job_type=JobType.TURN_ADVICE,
            session_id="ownership-session",
            match_id="ownership-match",
            generation=1,
            turn_number=1,
            base_battle_revision=1,
            expected_state=BattleState.TURN_REVIEWED,
            input_snapshot_id="ownership-snapshot",
            request_payload_hash="0" * 64,
        )
        self.dispatched = False
        self.applied = False

    def request_rich_turn_advice(self, command_id: str) -> object:
        assert command_id.startswith("gemini-rich-turn-ui-")
        return self.job

    def build_rich_turn_advice_transport_request(self, job: object) -> object:
        assert job is self.job
        return object()

    def mark_turn_advice_dispatched(self, job_id: str) -> None:
        assert job_id == self.job.job_id
        self.dispatched = True

    def apply_rich_turn_advice_result(
        self, envelope: ResultEnvelope
    ) -> ResultDisposition:
        assert envelope.job_id == self.job.job_id
        self.applied = True
        return ResultDisposition.APPLIED

    def fail_turn_advice_job(self, job_id: str, reason: str) -> None:
        raise AssertionError(f"unexpected failure for {job_id}: {reason}")


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _pump_until(
    application: QApplication, predicate: object, timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting for Qt lifecycle"
        application.processEvents()
        time.sleep(0.005)


def test_rich_dispatch_is_retained_while_running_and_released_after_finish() -> None:
    qapp = _qt_application()
    transport = _BlockingTransport()
    application = _ApplicationStub()
    adapter = GeminiRichTurnAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="fake-test-key", model="fake-test-model"),
    )
    applied: list[ResultDisposition] = []

    adapter.send(
        cast(BattleApplication, application),
        on_applied=applied.append,
        on_failed=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
    )
    assert transport.entered.wait(5)
    dispatch = adapter._active_dispatch  # noqa: SLF001 - lifecycle invariant
    assert isinstance(dispatch, TurnAdviceDispatch)
    dispatch_ref = weakref.ref(dispatch)
    del dispatch
    gc.collect()

    retained = dispatch_ref()
    assert retained is not None
    assert retained._worker_thread.isRunning() is True  # noqa: SLF001
    assert adapter.in_flight is True
    del retained

    transport.release.set()
    _pump_until(qapp, lambda: adapter._active_dispatch is None)  # noqa: SLF001
    gc.collect()

    assert dispatch_ref() is None
    assert adapter.in_flight is False
    assert application.dispatched is True
    assert application.applied is True
    assert applied == [ResultDisposition.APPLIED]
    assert transport.call_count == 1
