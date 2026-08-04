"""Latest-only background worker for selection ROI matching."""

from __future__ import annotations

import threading
from dataclasses import replace

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from maple_next.capture.contracts import FramePacket
from maple_next.selection_roi.contracts import SelectionMatchBundle
from maple_next.selection_roi.service import SelectionRoiService


class LatestOnlySelectionRoiWorker(QObject):
    """Single worker thread with one replaceable pending frame and no queue."""

    result_ready = Signal(object)

    def __init__(self, service: SelectionRoiService) -> None:
        super().__init__()
        self._service = service
        self._condition = threading.Condition()
        self._pending: FramePacket | None = None
        self._closing = False
        self._submitted_count = 0
        self._replaced_pending_count = 0
        self._completed_count = 0
        self._thread = threading.Thread(
            target=self._run,
            name="maple-selection-roi-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, frame: FramePacket) -> None:
        image = frame.image
        if not isinstance(image, QImage) or image.isNull():
            return
        detached = image.copy()
        request = replace(frame, image=detached)
        with self._condition:
            if self._closing:
                return
            self._submitted_count += 1
            if self._pending is not None:
                self._replaced_pending_count += 1
            self._pending = request
            self._condition.notify()

    def metrics(self) -> dict[str, int]:
        with self._condition:
            return {
                "submitted_count": self._submitted_count,
                "replaced_pending_count": self._replaced_pending_count,
                "completed_count": self._completed_count,
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
                frame = self._pending
                self._pending = None
            if frame is None:
                continue
            bundle: SelectionMatchBundle = self._service.process_frame(frame)
            with self._condition:
                if self._closing:
                    return
                self._completed_count += 1
            self.result_ready.emit(bundle)
