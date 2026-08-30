"""QtMultimediaUgreenBackend production callback -> CaptureService.frame_ready.

Repairs the R2 field blocker: the production backend accepted an on_frame
callback but never invoked it on an admitted frame, so
CaptureService.frame_ready could never fire outside a test that manually
emitted the signal. These tests drive the real QtMultimediaUgreenBackend and
CaptureService classes end to end with a hermetic fake QVideoFrame - no real
hardware, and no manual frame_ready.emit(...) anywhere in this file.
"""

from __future__ import annotations

import os
from typing import cast

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.capture.qt_ugreen import QtMultimediaUgreenBackend
from maple_next.capture.service import CaptureService


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class _TimedQtFrame:
    """Hermetic QVideoFrame fake driving the real backend admission path."""

    def __init__(self, start_time_us: int, image: QImage) -> None:
        self._start_time_us = start_time_us
        self._image = image
        self.to_image_calls = 0

    def startTime(self) -> int:  # noqa: N802 - mirrors Qt API
        return self._start_time_us

    def toImage(self) -> QImage:  # noqa: N802 - mirrors Qt API
        self.to_image_calls += 1
        return self._image


def _valid_image() -> QImage:
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(0x00112233)
    return image


def _null_image() -> QImage:
    return QImage()


def _running_backend_and_service() -> tuple[QtMultimediaUgreenBackend, CaptureService]:
    _qt_application()
    backend = QtMultimediaUgreenBackend()
    service = CaptureService(backend)
    # Bypass real QMediaDevices/QCamera hardware discovery only - the same
    # focused backend state-machine probe pattern already used throughout
    # tests/test_issue31_capture_30fps_cap.py. Everything downstream of this
    # (admission, conversion, the on_frame callback, canonicalization, and
    # frame_ready) is the genuine production code path.
    backend._running = True  # noqa: SLF001
    return backend, service


def test_decisive_production_backend_admission_reaches_frame_ready() -> None:
    """QtMultimediaUgreenBackend -> callback -> CaptureService -> frame_ready.

    No manual CaptureService.frame_ready.emit(...) anywhere in this test.
    """

    backend, service = _running_backend_and_service()
    emitted: list[FramePacket] = []
    service.frame_ready.connect(emitted.append)

    frame_a = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(frame_a, service._on_frame)  # noqa: SLF001

    assert len(emitted) == 1
    canonical_a = emitted[0]
    assert canonical_a.frame_kind is FrameKind.CANONICAL
    assert backend.metrics()["successful_conversion_count"] == 1
    assert backend.metrics()["conversion_attempt_count"] == 1

    # Well past the ~33.3ms/30fps admission gate, so frame B is admitted.
    frame_b = _TimedQtFrame(50_000, _valid_image())
    backend._on_qt_frame(frame_b, service._on_frame)  # noqa: SLF001

    assert len(emitted) == 2
    canonical_b = emitted[1]
    assert canonical_b.frame_id != canonical_a.frame_id
    assert canonical_b.captured_monotonic_ns > canonical_a.captured_monotonic_ns
    assert backend.metrics()["successful_conversion_count"] == 2
    assert backend.metrics()["conversion_attempt_count"] == 2

    # get_latest_frame() after the callback returns cached B with no
    # additional conversion.
    cached = backend.get_latest_frame()
    assert cached is not None
    assert cached.frame_id == canonical_b.frame_id
    assert backend.metrics()["successful_conversion_count"] == 2
    assert backend.metrics()["conversion_attempt_count"] == 2


def test_dropped_by_30fps_frame_never_converts_or_calls_back() -> None:
    """Case A: a frame thinned by the 30fps gate never reaches the callback."""

    backend, service = _running_backend_and_service()
    emitted: list[FramePacket] = []
    service.frame_ready.connect(emitted.append)

    frame_a = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(frame_a, service._on_frame)  # noqa: SLF001
    assert len(emitted) == 1

    # 5ms later: well inside the ~33.3ms/30fps admission window.
    frame_dropped = _TimedQtFrame(5_000, _valid_image())
    backend._on_qt_frame(frame_dropped, service._on_frame)  # noqa: SLF001

    assert len(emitted) == 1
    assert frame_dropped.to_image_calls == 0
    assert backend.metrics()["dropped_preview_frame_count"] == 1
    assert backend.metrics()["successful_conversion_count"] == 1
    assert backend.metrics()["conversion_attempt_count"] == 1


def test_failed_conversion_does_not_call_back_or_emit_stale_packet() -> None:
    """Case B: a conversion failure never forwards the old cached packet."""

    backend, service = _running_backend_and_service()
    emitted: list[FramePacket] = []
    service.frame_ready.connect(emitted.append)

    good_frame = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(good_frame, service._on_frame)  # noqa: SLF001
    assert len(emitted) == 1
    good_packet = backend.get_latest_frame()
    assert good_packet is not None

    broken_frame = _TimedQtFrame(50_000, _null_image())
    backend._on_qt_frame(broken_frame, service._on_frame)  # noqa: SLF001

    # No new callback/frame_ready for the failed conversion...
    assert len(emitted) == 1
    assert backend.metrics()["failed_conversion_count"] == 1
    assert backend.metrics()["successful_conversion_count"] == 1
    # ...but the previously cached packet is still readable, unfabricated.
    still_cached = backend.get_latest_frame()
    assert still_cached is good_packet


def test_recovery_after_failed_conversion_calls_back_exactly_once() -> None:
    """Case C: the next valid frame after a failure recovers normally."""

    backend, service = _running_backend_and_service()
    emitted: list[FramePacket] = []
    service.frame_ready.connect(emitted.append)

    good_frame = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(good_frame, service._on_frame)  # noqa: SLF001
    assert len(emitted) == 1

    broken_frame = _TimedQtFrame(50_000, _null_image())
    backend._on_qt_frame(broken_frame, service._on_frame)  # noqa: SLF001
    assert len(emitted) == 1

    recovered_frame = _TimedQtFrame(100_000, _valid_image())
    backend._on_qt_frame(recovered_frame, service._on_frame)  # noqa: SLF001

    assert len(emitted) == 2
    assert backend.metrics()["successful_conversion_count"] == 2
    assert backend.metrics()["failed_conversion_count"] == 1


def test_repeated_get_latest_frame_for_same_sequence_does_not_reconvert_or_recallback() -> None:
    """Case D: repeated pulls of the same admitted sequence never double-fire."""

    backend, service = _running_backend_and_service()
    emitted: list[FramePacket] = []
    service.frame_ready.connect(emitted.append)

    frame_a = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(frame_a, service._on_frame)  # noqa: SLF001
    assert len(emitted) == 1

    for _ in range(5):
        backend.get_latest_frame()
        service.latest_snapshot()

    assert len(emitted) == 1
    assert frame_a.to_image_calls == 1
    assert backend.metrics()["successful_conversion_count"] == 1
    assert backend.metrics()["conversion_attempt_count"] == 1


def test_on_frame_none_still_caches_and_converts_safely() -> None:
    """Case E: no registered callback -> no exception, still pull-convertible."""

    backend, _service = _running_backend_and_service()

    frame_a = _TimedQtFrame(0, _valid_image())
    backend._on_qt_frame(frame_a, None)  # noqa: SLF001

    # Admission still happened...
    assert backend.metrics()["incoming_frame_count"] == 1
    # ...but conversion is not forced eagerly without a callback to notify.
    assert backend.metrics()["successful_conversion_count"] == 0

    packet = backend.get_latest_frame()
    assert packet is not None
    assert backend.metrics()["successful_conversion_count"] == 1
