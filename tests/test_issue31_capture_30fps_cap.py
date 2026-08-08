"""Focused coverage for the UGREEN 720p/60 driver fallback cap."""

from __future__ import annotations

from PySide6.QtGui import QImage

from maple_next.capture.qt_ugreen import QtMultimediaUgreenBackend


class TimedQtFrame:
    def __init__(self, start_time_us: int) -> None:
        self._start_time_us = start_time_us
        self.to_image_calls = 0

    def startTime(self) -> int:  # noqa: N802 - mirrors Qt API
        return self._start_time_us

    def toImage(self) -> QImage:  # noqa: N802 - mirrors Qt API
        self.to_image_calls += 1
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(0x00112233)
        return image


class UntimedQtFrame:
    """Legacy fake without QVideoFrame.startTime()."""

    def __init__(self) -> None:
        self.to_image_calls = 0

    def toImage(self) -> QImage:  # noqa: N802 - mirrors Qt API
        self.to_image_calls += 1
        image = QImage(1280, 720, QImage.Format.Format_RGB32)
        image.fill(0x00112233)
        return image


def _run_timed_source(source_fps: int, frame_count: int) -> QtMultimediaUgreenBackend:
    backend = QtMultimediaUgreenBackend()
    backend._running = True  # noqa: SLF001 - focused backend state-machine probe
    interval_us = round(1_000_000 / source_fps)
    for index in range(frame_count):
        frame = TimedQtFrame(index * interval_us)
        backend._on_qt_frame(frame, None)  # noqa: SLF001
        backend.get_latest_frame()
    return backend


def test_60fps_driver_callbacks_are_processed_at_about_30fps() -> None:
    backend = _run_timed_source(60, 60)
    metrics = backend.metrics()

    assert metrics["raw_callback_count"] == 60
    assert 29 <= metrics["incoming_frame_count"] <= 31
    assert 29 <= metrics["successful_conversion_count"] <= 31
    assert 29 <= metrics["dropped_preview_frame_count"] <= 31
    assert metrics["preview_processing_max_fps"] == 30.0


def test_30fps_source_is_not_thinned() -> None:
    backend = _run_timed_source(30, 30)
    metrics = backend.metrics()

    assert metrics["raw_callback_count"] == 30
    assert metrics["incoming_frame_count"] == 30
    assert metrics["successful_conversion_count"] == 30
    assert metrics["dropped_preview_frame_count"] == 0


def test_25fps_source_is_not_thinned() -> None:
    backend = _run_timed_source(25, 25)
    metrics = backend.metrics()

    assert metrics["raw_callback_count"] == 25
    assert metrics["incoming_frame_count"] == 25
    assert metrics["successful_conversion_count"] == 25
    assert metrics["dropped_preview_frame_count"] == 0


def test_latest_only_cap_does_not_build_a_backlog() -> None:
    backend = QtMultimediaUgreenBackend()
    backend._running = True  # noqa: SLF001
    interval_us = round(1_000_000 / 60)

    frames = [TimedQtFrame(index * interval_us) for index in range(60)]
    for frame in frames:
        backend._on_qt_frame(frame, None)  # noqa: SLF001

    packet = backend.get_latest_frame()
    assert packet is not None
    assert sum(frame.to_image_calls for frame in frames) == 1
    assert frames[-1].to_image_calls in {0, 1}
    assert backend.metrics()["conversion_attempt_count"] == 1


def test_legacy_untimed_fakes_keep_existing_contract_behavior() -> None:
    backend = QtMultimediaUgreenBackend()
    backend._running = True  # noqa: SLF001

    frames = [UntimedQtFrame() for _ in range(3)]
    for frame in frames:
        backend._on_qt_frame(frame, None)  # noqa: SLF001
        backend.get_latest_frame()

    assert backend.metrics()["raw_callback_count"] == 3
    assert backend.metrics()["incoming_frame_count"] == 3
    assert backend.metrics()["successful_conversion_count"] == 3
    assert backend.metrics()["dropped_preview_frame_count"] == 0


def test_stop_resets_cap_counters() -> None:
    backend = _run_timed_source(60, 10)
    assert backend.metrics()["raw_callback_count"] == 10

    backend.stop()
    metrics = backend.metrics()
    assert metrics["raw_callback_count"] == 0
    assert metrics["incoming_frame_count"] == 0
    assert metrics["dropped_preview_frame_count"] == 0
