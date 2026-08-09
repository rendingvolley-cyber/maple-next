"""Off-UI-thread Turn Advice dispatch — fake/injected transport only.

Mirrors :mod:`maple_next.workers.selection_advice_worker`. This worker
receives only immutable request values and a transport. It never imports the
persistence package, never opens a SQLite connection, and never re-reads the
repository — every value it needs was already resolved on the UI thread
before dispatch. It calls ``transport.send`` exactly once.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

from maple_next.providers.transport import ProviderConfig, SanitizedProviderResult
from maple_next.providers.turn_request import TurnAdviceRequest
from maple_next.providers.turn_transport import (
    ProviderTransportError,
    TurnProviderTransport,
)


class TurnAdviceWorker(QObject):
    """Runs exactly one ``transport.send`` call, then emits a result signal."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        transport: TurnProviderTransport,
        request: TurnAdviceRequest,
        config: ProviderConfig,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._request = request
        self._config = config
        self.send_call_count = 0

    def run(self) -> None:
        self.send_call_count += 1
        try:
            result: SanitizedProviderResult = self._transport.send(
                self._request, self._config
            )
        except ProviderTransportError as exc:
            self.failed.emit(str(exc))
        except Exception:  # noqa: BLE001 - never leak an unexpected raw error to UI
            self.failed.emit("GEMINI_TURN_TRANSPORT_UNEXPECTED_ERROR")
        else:
            self.succeeded.emit(result)


class TurnAdviceDispatch(QObject):
    """Owns the QThread/QObject pair for exactly one in-flight Turn dispatch.

    See :class:`maple_next.workers.selection_advice_worker.SelectionAdviceDispatch`
    for the rationale behind keeping this a ``QObject`` on the UI thread.
    """

    def __init__(
        self,
        transport: TurnProviderTransport,
        request: TurnAdviceRequest,
        config: ProviderConfig,
        *,
        on_succeeded: Callable[[SanitizedProviderResult], None],
        on_failed: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._worker_thread = QThread()
        self.worker = TurnAdviceWorker(transport, request, config)
        self.worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self.worker.run)
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed
        self.worker.succeeded.connect(self._handle_succeeded)
        self.worker.failed.connect(self._handle_failed)

    def start(self) -> None:
        self._worker_thread.start()

    def add_finished_callback(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` only after the owned worker thread has stopped.

        UI adapters use this terminal lifecycle hook to retain the dispatch
        strongly for the complete lifetime of its ``QThread``.  Releasing an
        in-flight dispatch earlier can destroy a running Qt thread and abort
        the whole process before Python can report a traceback.
        """

        self._worker_thread.finished.connect(callback)

    def wait_until_finished(self, timeout_ms: int = 5000) -> bool:
        """Test-only helper: block until the worker thread has stopped."""

        return bool(self._worker_thread.wait(timeout_ms))

    def _handle_succeeded(self, result: object) -> None:
        self._on_succeeded(result)  # type: ignore[arg-type]
        self._shutdown()

    def _handle_failed(self, reason: str) -> None:
        self._on_failed(reason)
        self._shutdown()

    def _shutdown(self) -> None:
        self._worker_thread.quit()
        self._worker_thread.wait()
        self._worker_thread.deleteLater()
        self.worker.deleteLater()
