"""Off-UI-thread Selection Advice dispatch.

This worker receives only immutable request values and a transport. It never
imports the persistence package, never opens a SQLite connection, and never
re-reads the repository — every value it needs was already resolved on the
UI thread before dispatch. It calls ``transport.send`` exactly once.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

from maple_next.providers.selection_request import SelectionAdviceRequest
from maple_next.providers.transport import (
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
    SelectionProviderTransport,
)


class SelectionAdviceWorker(QObject):
    """Runs exactly one ``transport.send`` call, then emits a result signal."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        transport: SelectionProviderTransport,
        request: SelectionAdviceRequest,
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
            self.failed.emit("GEMINI_TRANSPORT_UNEXPECTED_ERROR")
        else:
            self.succeeded.emit(result)


class SelectionAdviceDispatch(QObject):
    """Owns the QThread/QObject pair for exactly one in-flight dispatch.

    ``on_succeeded``/``on_failed`` run on the UI thread. This object itself
    must stay a ``QObject`` living on the UI thread: Qt's automatic
    queued-vs-direct connection choice is based on the *receiver's* thread
    affinity, and only a ``QObject`` has one. A plain Python receiver would
    make PySide6 fall back to a direct connection and run the callback on the
    worker thread instead, which would violate the single-writer contract.
    """

    def __init__(
        self,
        transport: SelectionProviderTransport,
        request: SelectionAdviceRequest,
        config: ProviderConfig,
        *,
        on_succeeded: Callable[[SanitizedProviderResult], None],
        on_failed: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._worker_thread = QThread()
        self.worker = SelectionAdviceWorker(transport, request, config)
        self.worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self.worker.run)
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed
        self.worker.succeeded.connect(self._handle_succeeded)
        self.worker.failed.connect(self._handle_failed)

    def start(self) -> None:
        self._worker_thread.start()

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
        # Block until the worker thread's event loop actually exits before
        # scheduling deletion. The worker has already emitted its result and
        # is idle, so this returns immediately; it exists to guarantee no
        # QThread is ever left half-torn-down across repeated dispatches.
        self._worker_thread.quit()
        self._worker_thread.wait()
        self._worker_thread.deleteLater()
        self.worker.deleteLater()
