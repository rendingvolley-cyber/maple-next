"""Focused regression coverage for PR #41 independent-verification rework."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    DeviceOpenResult,
    FrameKind,
    FramePacket,
    SourceFramePacket,
)
from maple_next.capture.qt_ugreen import QtMultimediaUgreenBackend
from maple_next.capture.service import CaptureService
from maple_next.capture.telemetry import SourceFpsSampler
from maple_next.ocr.contracts import OcrBundleStatus, OcrCandidateContext
from maple_next.ocr.service import OcrCandidateService
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.explicit_turn_number import (
    ExplicitTurnNumberController,
    ExplicitTurnNumberWindow,
)


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class FakeQtFrame:
    def __init__(self, image: QImage | None = None, *, raises: bool = False) -> None:
        self.image = image
        self.raises = raises
        self.to_image_calls = 0

    def toImage(self) -> QImage:  # noqa: N802 - mirrors Qt API
        self.to_image_calls += 1
        if self.raises:
            raise RuntimeError("driver frame failure")
        return self.image if self.image is not None else QImage()


def valid_image(width: int = 640, height: int = 360) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x00112233)
    return image


def test_failed_qt_frame_is_attempted_once_and_next_valid_frame_recovers() -> None:
    backend = QtMultimediaUgreenBackend()
    backend._running = True  # noqa: SLF001 - real-backend state-machine probe

    first = FakeQtFrame(valid_image())
    backend._on_qt_frame(first, None)  # noqa: SLF001
    packet_a = backend.get_latest_frame()
    assert packet_a is not None
    assert packet_a.frame_kind is FrameKind.SOURCE

    bad = FakeQtFrame(raises=True)
    backend._on_qt_frame(bad, None)  # noqa: SLF001
    for _ in range(75):
        assert backend.get_latest_frame() is packet_a
    assert bad.to_image_calls == 1
    assert backend.metrics()["failed_conversion_count"] == 1

    final = FakeQtFrame(valid_image(1280, 720))
    backend._on_qt_frame(final, None)  # noqa: SLF001
    packet_c = backend.get_latest_frame()
    assert packet_c is not None
    assert packet_c is not packet_a
    assert packet_c.width == 1280
    assert packet_c.height == 720
    assert final.to_image_calls == 1
    assert backend.metrics()["conversion_attempt_count"] == 3
    assert backend.metrics()["successful_conversion_count"] == 2


def test_null_qt_frame_is_attempted_once_and_teardown_resets_counters() -> None:
    backend = QtMultimediaUgreenBackend()
    backend._running = True  # noqa: SLF001
    null_frame = FakeQtFrame(QImage())
    backend._on_qt_frame(null_frame, None)  # noqa: SLF001

    for _ in range(75):
        assert backend.get_latest_frame() is None
    assert null_frame.to_image_calls == 1
    assert backend.metrics()["conversion_attempt_count"] == 1
    assert backend.metrics()["failed_conversion_count"] == 1

    backend.stop()
    assert backend.metrics()["conversion_attempt_count"] == 0
    assert backend.metrics()["failed_conversion_count"] == 0
    assert backend.metrics()["incoming_frame_count"] == 0


class SourceBackend:
    def __init__(self, frame: SourceFramePacket) -> None:
        self.frame = frame
        self.running = False
        self.incoming_frame_count = 1

    def start(self, selector: str, on_frame: object | None = None) -> DeviceOpenResult:
        self.running = True
        return DeviceOpenResult(True, True, "FAKE_UGREEN", None)

    def stop(self) -> None:
        self.running = False

    def get_latest_frame(self) -> SourceFramePacket:
        return self.frame

    def is_running(self) -> bool:
        return self.running

    def metrics(self) -> dict[str, object]:
        return {
            "incoming_frame_count": self.incoming_frame_count,
            "selected_resolution": (self.frame.width, self.frame.height),
            "preview_mode": "fallback",
        }

    def advance(self) -> None:
        self.incoming_frame_count += 1
        self.frame = SourceFramePacket(
            frame_id=f"source-{self.incoming_frame_count}",
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=self.frame.width,
            height=self.frame.height,
            image=self.frame.image,
        )


def source_packet() -> SourceFramePacket:
    return SourceFramePacket(
        frame_id="source-1",
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=640,
        height=480,
        image=valid_image(640, 480),
    )


def test_preview_returns_source_packet_and_ocr_returns_canonical_packet() -> None:
    packet = source_packet()
    clock = [packet.captured_monotonic_ns]
    backend = SourceBackend(packet)
    service = CaptureService(backend, monotonic_clock=lambda: clock[0])
    service.start()

    preview_status, preview = service.latest_preview_snapshot()
    assert preview_status.fresh is True
    assert type(preview) is SourceFramePacket
    assert preview is not None
    assert preview.frame_kind is FrameKind.SOURCE
    assert (preview.width, preview.height) == (640, 480)

    ocr_status, canonical = service.latest_snapshot()
    assert ocr_status.fresh is True
    assert type(canonical) is FramePacket
    assert canonical is not None
    assert canonical.frame_kind is FrameKind.CANONICAL
    assert (canonical.width, canonical.height) == (
        CANONICAL_FRAME_WIDTH,
        CANONICAL_FRAME_HEIGHT,
    )
    assert (canonical.source_width, canonical.source_height) == (640, 480)
    assert canonical.content_rect is not None

    again = service.latest_snapshot()[1]
    assert again is canonical
    assert service.capture_metrics()["ocr_conversion_count"] == 1


class RecordingOcrBackend:
    def __init__(self) -> None:
        self.available_calls = 0
        self.generate_calls = 0

    def is_available(self) -> bool:
        self.available_calls += 1
        return True

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple:
        self.generate_calls += 1
        return ()


def test_ocr_rejects_source_kind_before_backend_access() -> None:
    backend = RecordingOcrBackend()
    service = OcrCandidateService(backend)
    source = source_packet()
    bundle = service.request_candidates(
        frame=source,
        frame_age_ms=0,
        fresh=True,
        context=OcrCandidateContext((), ()),
    )
    assert bundle.status == OcrBundleStatus.FRAME_NOT_CANONICAL
    assert backend.available_calls == 0
    assert backend.generate_calls == 0


@pytest.mark.parametrize("frames, expected", [(25, 25.0), (30, 30.0), (60, 60.0)])
def test_source_fps_sampler_reports_observed_cadence(frames: int, expected: float) -> None:
    sampler = SourceFpsSampler()
    assert sampler.sample(frame_count=100, now_ns=2_000_000_000) is None
    assert sampler.sample(frame_count=100 + frames, now_ns=3_000_000_000) == expected


def test_source_fps_sampler_resets_on_counter_or_clock_rollback() -> None:
    sampler = SourceFpsSampler()
    assert sampler.sample(frame_count=20, now_ns=2_000_000_000) is None
    assert sampler.sample(frame_count=10, now_ns=3_000_000_000) is None
    assert sampler.sample(frame_count=20, now_ns=2_000_000_000) is None


def build_explicit_window(
    tmp_path: Path, backend: SourceBackend
) -> tuple[SQLiteRepository, ExplicitTurnNumberWindow]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = ExplicitTurnNumberController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = ExplicitTurnNumberWindow(
        controller,
        capture_backend=backend,
        auto_start_capture=False,
    )
    return repository, window


def test_fast_preview_does_not_repaint_telemetry_and_one_second_tick_reports_fps(
    tmp_path: Path,
) -> None:
    qt_application()
    backend = SourceBackend(source_packet())
    repository, window = build_explicit_window(tmp_path, backend)
    now_ns = [10_000_000_000]
    window._capture_telemetry_clock = lambda: now_ns[0]  # noqa: SLF001
    window.header_tabs.setCurrentIndex(1)
    window._start_capture()  # noqa: SLF001

    # Establish the one-second sampling baseline, then prove fast preview
    # updates do not touch telemetry text or call setText repeatedly.
    window._poll_capture_telemetry()  # noqa: SLF001
    text_before = window.capture_freshness_label.text()
    set_text_before = window._capture_telemetry_set_text_count  # noqa: SLF001
    for _ in range(30):
        backend.advance()
        window._poll_capture_preview()  # noqa: SLF001
    assert window.capture_freshness_label.text() == text_before
    assert window._capture_telemetry_set_text_count == set_text_before  # noqa: SLF001

    now_ns[0] += 1_000_000_000
    window._poll_capture_telemetry()  # noqa: SLF001
    assert window.capture_freshness_label.text() == "入力: 640×480 / 30.0 fps"
    update_count = window._capture_telemetry_set_text_count  # noqa: SLF001
    window._poll_capture_telemetry()  # noqa: SLF001
    assert window._capture_telemetry_set_text_count == update_count  # noqa: SLF001

    window.close()
    repository.close()


def test_tab_switch_stops_telemetry_and_restarts_with_a_fresh_window(tmp_path: Path) -> None:
    qt_application()
    backend = SourceBackend(source_packet())
    repository, window = build_explicit_window(tmp_path, backend)
    window.header_tabs.setCurrentIndex(1)
    window._start_capture()  # noqa: SLF001
    timer = window._capture_telemetry_timer  # noqa: SLF001
    assert timer is not None and timer.isActive()

    window.header_tabs.setCurrentIndex(0)
    assert timer.isActive() is False
    assert window._source_fps_sampler._baseline_count is None  # noqa: SLF001

    window.header_tabs.setCurrentIndex(1)
    assert timer.isActive() is True
    assert "— fps" in window.capture_freshness_label.text()

    window.close()
    repository.close()
