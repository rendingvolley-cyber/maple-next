"""Latest-only selection ROI worker keeps at most one pending frame."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage

from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.selection_roi.contracts import SelectionMatchBundle
from maple_next.selection_roi.service import SelectionRoiService
from maple_next.selection_roi.worker import LatestOnlySelectionRoiWorker


class BlockingService:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.frame_ids: list[str] = []

    def process_frame(self, frame: FramePacket) -> SelectionMatchBundle:
        self.frame_ids.append(frame.frame_id)
        self.started.set()
        self.release.wait(timeout=2.0)
        return SelectionMatchBundle(
            status="CANDIDATES_READY",
            operator_message="test",
            frame_id=frame.frame_id,
            observation_id=None,
            slots=(),
            reference_count=0,
            roi_config_provenance="test",
        )


def _frame(frame_id: str) -> FramePacket:
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(0)
    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=1280,
        height=720,
        image=image,
        frame_kind=FrameKind.CANONICAL,
    )


def test_latest_only_worker_replaces_pending_frame(tmp_path: Path) -> None:
    del tmp_path
    service = BlockingService()
    worker = LatestOnlySelectionRoiWorker(
        cast(SelectionRoiService, service)
    )
    try:
        worker.submit(_frame("one"))
        assert service.started.wait(timeout=1.0)
        worker.submit(_frame("two"))
        worker.submit(_frame("three"))
        metrics = worker.metrics()
        assert metrics["pending_count"] == 1
        assert metrics["replaced_pending_count"] >= 1
        service.release.set()
        deadline = time.monotonic() + 2.0
        while worker.metrics()["completed_count"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.frame_ids == ["one", "three"]
    finally:
        service.release.set()
        worker.close()
