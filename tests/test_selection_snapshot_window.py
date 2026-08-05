"""NEW MATCH captures exactly one immutable Selection screenshot."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import (
    CaptureStatus,
    CaptureStatusCode,
    FrameKind,
    FramePacket,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_snapshot_window import SelectionSnapshotMatchFlowWindow


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, SelectionSnapshotMatchFlowWindow]:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = SelectionSnapshotMatchFlowWindow(
        controller,
        ocr_data_directory=tmp_path / "data" / "ocr",
    )
    return repository, window


def _available_snapshot(image: QImage) -> tuple[CaptureStatus, FramePacket]:
    captured_at = datetime.now(UTC)
    frame = FramePacket(
        frame_id="live-frame-1",
        source="UGREEN_DIRECT",
        captured_at_utc=captured_at,
        captured_monotonic_ns=time.monotonic_ns(),
        width=1280,
        height=720,
        image=image,
        source_width=1280,
        source_height=720,
        content_rect=(0, 0, 1280, 720),
        frame_kind=FrameKind.CANONICAL,
    )
    status = CaptureStatus(
        status=CaptureStatusCode.AVAILABLE,
        available=True,
        manual_entry_allowed=True,
        source="UGREEN_DIRECT",
        device_label="UGREEN test",
        frame_id=frame.frame_id,
        captured_at_utc=captured_at,
        age_ms=0,
        fresh=True,
        width=1280,
        height=720,
        error_code=None,
        operator_message=None,
    )
    return status, frame


def _unavailable_snapshot() -> tuple[CaptureStatus, None]:
    return (
        CaptureStatus(
            status=CaptureStatusCode.FRAME_UNAVAILABLE,
            available=False,
            manual_entry_allowed=True,
            source="UGREEN_DIRECT",
            device_label="UGREEN test",
            frame_id=None,
            captured_at_utc=None,
            age_ms=None,
            fresh=False,
            width=None,
            height=None,
            error_code="CAPTURE_FRAME_UNAVAILABLE",
            operator_message="映像フレームを取得できません。手動入力で続行できます。",
        ),
        None,
    )


def test_selection_roi_timer_never_runs_in_official_snapshot_window(
    tmp_path: Path,
) -> None:
    repository, window = _build_window(tmp_path)

    assert window._selection_roi_timer is not None  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001
    window._sync_selection_roi_timer()  # noqa: SLF001
    window._poll_selection_roi()  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_new_match_freezes_one_frame_and_submits_it_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#112233"))
    calls = 0

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        nonlocal calls
        calls += 1
        return _available_snapshot(source_image)

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()
    source_image.fill(QColor("#ffffff"))

    assert calls == 1
    assert len(submitted) == 1
    frozen = submitted[0]
    assert frozen.frame_id.startswith("live-frame-1:new-match-snapshot:")
    assert frozen.image.pixelColor(0, 0) == QColor("#112233")
    assert window._controller.refresh().projection.session_state == "SELECTION_OPEN"  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_new_match_without_frame_keeps_manual_selection_flow_and_submits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, window = _build_window(tmp_path)
    submitted: list[FramePacket] = []
    monkeypatch.setattr(
        window._capture_service,  # noqa: SLF001
        "latest_snapshot",
        _unavailable_snapshot,
    )
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()

    assert submitted == []
    assert window._controller.refresh().projection.session_state == "SELECTION_OPEN"  # noqa: SLF001
    assert "手動入力" in window.selection_roi_status_label.text()
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()
