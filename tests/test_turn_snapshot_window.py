"""Turn buttons freeze exactly one canonical frame before domain transition."""

from __future__ import annotations

import json
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
from maple_next.domain.enums import HpBucket
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.turn_ocr.contracts import TurnSnapshotRequest
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.turn_snapshot_window import TurnSnapshotMatchFlowWindow

SELF_TEAM = ("A", "B", "C", "D", "E", "F")
OPPONENT_TEAM = ("X", "Y", "Z", "U", "V", "W")


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _write_roi_config(root: Path) -> None:
    path = root / "turn" / "config" / "roi_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "contract_version": "maple-turn-roi.v1",
                "canvas_width": 1280,
                "canvas_height": 720,
                "layout": "test-layout",
                "provisional": True,
                "rois": {
                    "self_active": {"x": 10, "y": 10, "width": 100, "height": 20},
                    "opponent_active": {
                        "x": 1170,
                        "y": 10,
                        "width": 100,
                        "height": 20,
                    },
                    "self_hp": {"x": 10, "y": 680, "width": 160, "height": 20},
                    "opponent_hp": {
                        "x": 1110,
                        "y": 40,
                        "width": 160,
                        "height": 20,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, MatchFlowController, TurnSnapshotMatchFlowWindow]:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    ocr_root = tmp_path / "data" / "ocr"
    _write_roi_config(ocr_root)
    window = TurnSnapshotMatchFlowWindow(controller, ocr_data_directory=ocr_root)
    controller.new_match()
    controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    controller.submit_mock_advice(SELF_TEAM[:3], SELF_TEAM[0])
    controller.apply_selection(SELF_TEAM[:3], SELF_TEAM[0], human_confirmed=True)
    window.render_view()
    return repository, controller, window


def _available_snapshot(image: QImage, *, age_ms: int = 0) -> tuple[CaptureStatus, FramePacket]:
    captured_at = datetime.now(UTC)
    frame = FramePacket(
        frame_id="live-turn-frame",
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
        age_ms=age_ms,
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


def test_start_turn_freezes_one_frame_and_submits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    source = QImage(1280, 720, QImage.Format.Format_RGB32)
    source.fill(QColor("#112233"))
    calls = 0

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        nonlocal calls
        calls += 1
        return _available_snapshot(source)

    submitted: list[TurnSnapshotRequest] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._turn_snapshot_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._turn_snapshot_worker, "submit", submitted.append)  # noqa: SLF001

    window.start_turn_button.click()
    source.fill(QColor("white"))

    assert calls == 1
    assert len(submitted) == 1
    request = submitted[0]
    assert request.identity.turn_number == 1
    assert request.frame.image.pixelColor(0, 0) == QColor("#112233")
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    assert not window._capture_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_start_turn_without_frame_keeps_manual_turn_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    submitted: list[TurnSnapshotRequest] = []
    monkeypatch.setattr(
        window._capture_service,  # noqa: SLF001
        "latest_snapshot",
        _unavailable_snapshot,
    )
    assert window._turn_snapshot_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._turn_snapshot_worker, "submit", submitted.append)  # noqa: SLF001

    window.start_turn_button.click()

    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"
    assert submitted == []
    assert "手動入力" in window.turn_snapshot_status_label.text()

    window.close()
    repository.close()


def test_next_turn_freezes_one_new_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, controller, window = _build_window(tmp_path)
    first = QImage(1280, 720, QImage.Format.Format_RGB32)
    first.fill(QColor("#223344"))
    monkeypatch.setattr(
        window._capture_service,  # noqa: SLF001
        "latest_snapshot",
        lambda: _available_snapshot(first),
    )
    assert window._turn_snapshot_worker is not None  # noqa: SLF001
    submitted: list[TurnSnapshotRequest] = []
    monkeypatch.setattr(window._turn_snapshot_worker, "submit", submitted.append)  # noqa: SLF001
    window.start_turn_button.click()

    controller.confirm_turn_facts(
        self_active=SELF_TEAM[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp=HpBucket.FULL.value,
        opponent_hp=HpBucket.FULL.value,
        legal_moves=("Move 1",),
        legal_switches=(SELF_TEAM[1], SELF_TEAM[2]),
        human_note="",
        human_confirmed=True,
    )
    controller.submit_mock_turn_advice(
        action_type="MOVE",
        action_name="Move 1",
        opponent_prediction="Move X",
        rationale="test",
    )
    controller.record_actual_action(
        action_type="MOVE",
        action_name="Move 1",
        human_confirmed=True,
    )
    window.render_view()

    second = QImage(1280, 720, QImage.Format.Format_RGB32)
    second.fill(QColor("#334455"))
    monkeypatch.setattr(
        window._capture_service,  # noqa: SLF001
        "latest_snapshot",
        lambda: _available_snapshot(second),
    )
    window.next_turn_button.click()

    assert len(submitted) == 2
    assert submitted[-1].identity.turn_number == 2
    assert submitted[-1].frame.image.pixelColor(0, 0) == QColor("#334455")
    assert controller.refresh().projection.session_state == "TURN_CAPTURE_PENDING"

    window.close()
    repository.close()
