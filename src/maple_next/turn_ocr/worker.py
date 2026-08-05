"""Single-threaded latest-request worker for human-triggered Turn OCR."""

from __future__ import annotations

import threading
from dataclasses import replace

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from maple_next.turn_ocr.contracts import TurnSnapshotRequest, TurnSnapshotResult
from maple_next.turn_ocr.service import TurnSnapshotOcrService


class TurnSnapshotOcrWorker(QObject):
    """One daemon worker with a replaceable pending request and no retry."""

    result_ready = Signal(object)

    def __init__(self, service: TurnSnapshotOcrService) -> None:
        super().__init__()
        self._service = service
        self._condition = threading.Condition()
        self._pending: TurnSnapshotRequest | None = None
        self._closing = False
        self._submitted_count = 0
        self._completed_count = 0
        self._replaced_pending_count = 0
        self._thread = threading.Thread(
            target=self._run,
            name="maple-turn-snapshot-ocr-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, request: TurnSnapshotRequest) -> None:
        image = request.frame.image
        if not isinstance(image, QImage) or image.isNull():
            return
        detached = image.copy()
        frozen_request = replace(
            request,
            frame=replace(request.frame, image=detached),
        )
        with self._condition:
            if self._closing:
                return
            self._submitted_count += 1
            if self._pending is not None:
                self._replaced_pending_count += 1
            self._pending = frozen_request
            self._condition.notify()

    def metrics(self) -> dict[str, int]:
        with self._condition:
            return {
                "submitted_count": self._submitted_count,
                "completed_count": self._completed_count,
                "replaced_pending_count": self._replaced_pending_count,
                "pending_count": 0 if self._pending is None else 1,
            }

    def close(self) -> None:
        with self._condition:
            self._closing = True
            self._pending = None
            self._condition.notify_all()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closing:
                    self._condition.wait()
                if self._closing:
                    return
                request = self._pending
                self._pending = None
            if request is None:
                continue
            try:
                result: TurnSnapshotResult = self._service.process(request)
            except Exception:  # noqa: BLE001 - raw OCR exceptions never reach UI
                result = self._service.failed_result(request)
            with self._condition:
                if self._closing:
                    return
                self._completed_count += 1
            self.result_ready.emit(result)
