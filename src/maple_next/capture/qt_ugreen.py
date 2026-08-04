"""Production UGREEN direct-capture backend using PySide6 Qt Multimedia.

No OBS anywhere in this file. No device is opened at import time, and no
capture starts on construction - only on an explicit start()/open() call.
Any hardware/driver failure here degrades to a sanitized DeviceOpenResult;
nothing raises up to the caller.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from maple_next.capture.contracts import DeviceOpenResult, SourceFramePacket
from maple_next.capture.format_policy import (
    apply_preferred_720p_format,
    select_exact_720p_format,
)

__all__ = [
    "QtMultimediaUgreenBackend",
    "select_exact_720p_format",
    "select_ugreen_device",
]

try:  # pragma: no cover - exercised only when PySide6 Multimedia is importable
    from PySide6.QtCore import QObject
    from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink

    _QT_MULTIMEDIA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _QT_MULTIMEDIA_AVAILABLE = False
    QObject = object  # type: ignore[assignment, misc]


# The UGREEN/Windows driver may ignore a requested 720p/30 camera format and
# still deliver 720p/60 callbacks. Maple does not need 60 image conversions or
# UI renders. Keep the newest frame only and admit at most about 30 distinct
# preview frames per second. 25/29.97/30 fps sources pass through unchanged.
_PREVIEW_PROCESSING_MAX_FPS = 30.0
_PREVIEW_PROCESSING_INTERVAL_NS = round(1_000_000_000 / _PREVIEW_PROCESSING_MAX_FPS)
_PREVIEW_PROCESSING_TOLERANCE_NS = 1_000_000


def select_ugreen_device(descriptions: Sequence[str], selector: str) -> int | None:
    """Pure, hardware-free selection: sanitized substring search.

    Returns the index of the first device whose description contains the
    selector (case-insensitive), or None when nothing matches. Never falls
    back to "the only camera present" when it doesn't match the selector.
    """

    needle = selector.strip().lower()
    if not needle:
        return None
    for index, description in enumerate(descriptions):
        if needle in description.lower():
            return index
    return None


class QtMultimediaUgreenBackend:
    """VideoCaptureBackend implementation backed by QMediaDevices/QCamera.

    The Qt callback never touches Python image data. It stores only the newest
    admitted QVideoFrame reference and advances a monotonically increasing
    sequence. The physical driver may callback at 60 fps even after accepting
    a 720p/30 format request; those raw callbacks are latest-only thinned to a
    maximum processing cadence of about 30 fps before any QImage conversion.

    ``get_latest_frame()`` performs ``toImage()``/``copy()`` lazily and at most
    once for each admitted callback sequence, whether conversion succeeds or
    fails. A stalled malformed frame therefore cannot create a 10 ms retry
    loop, while a later distinct valid callback recovers normally.

    The operator-selected low-load policy requests exact 1280x720 at about
    30 fps and falls back to Qt/driver auto-negotiation if that format cannot
    be applied. The processing cap remains active when the driver ignores the
    requested cadence.
    """

    def __init__(self) -> None:
        self._camera: object | None = None
        self._capture_session: object | None = None
        self._video_sink: object | None = None
        self._running = False
        self._device_label: str | None = None
        self._latest_frame: SourceFramePacket | None = None
        self._owner: QObject | None = None
        self._pending_qt_frame: object | None = None
        self._pending_frame_sequence = 0
        self._attempted_frame_sequence = 0
        self._incoming_frame_count = 0
        self._raw_callback_count = 0
        self._dropped_preview_frame_count = 0
        self._last_admitted_stream_ns: int | None = None
        self._conversion_attempt_count = 0
        self._successful_conversion_count = 0
        self._failed_conversion_count = 0
        self._selected_resolution: tuple[int, int] | None = None
        self._preferred_720p_applied = False

    def start(
        self,
        selector: str,
        on_frame: Callable[[SourceFramePacket], None] | None = None,
    ) -> DeviceOpenResult:
        if self._running:
            return DeviceOpenResult(
                opened=True,
                device_found=True,
                device_label=self._device_label,
                error_code=None,
            )

        if not _QT_MULTIMEDIA_AVAILABLE:
            return DeviceOpenResult(
                opened=False,
                device_found=False,
                device_label=None,
                error_code="CAPTURE_DEVICE_UNAVAILABLE",
            )

        try:
            devices = QMediaDevices.videoInputs()
            descriptions = [device.description() for device in devices]
            index = select_ugreen_device(descriptions, selector)
            if index is None:
                return DeviceOpenResult(
                    opened=False,
                    device_found=False,
                    device_label=None,
                    error_code="CAPTURE_DEVICE_UNAVAILABLE",
                )

            chosen = devices[index]
            self._device_label = "UGREEN capture device"

            self._owner = QObject()
            camera = QCamera(chosen, self._owner)
            self._preferred_720p_applied = apply_preferred_720p_format(camera, chosen)
            capture_session = QMediaCaptureSession(self._owner)
            video_sink = QVideoSink(self._owner)
            capture_session.setCamera(camera)
            capture_session.setVideoSink(video_sink)

            def _handle_frame(qt_frame: object) -> None:
                self._on_qt_frame(qt_frame, on_frame)

            video_sink.videoFrameChanged.connect(_handle_frame)
            camera.start()
            with contextlib.suppress(Exception):
                negotiated = camera.cameraFormat()
                resolution = negotiated.resolution()
                if resolution.width() > 0 and resolution.height() > 0:
                    self._selected_resolution = (resolution.width(), resolution.height())

            self._camera = camera
            self._capture_session = capture_session
            self._video_sink = video_sink
            self._running = True
            return DeviceOpenResult(
                opened=True,
                device_found=True,
                device_label=self._device_label,
                error_code=None,
            )
        except Exception:  # noqa: BLE001 - hardware/driver failure must not raise
            self._teardown()
            return DeviceOpenResult(
                opened=False,
                device_found=True,
                device_label=None,
                error_code="CAPTURE_OPEN_FAILED",
            )

    def stop(self) -> None:
        self._teardown()

    def get_latest_frame(self) -> SourceFramePacket | None:
        """Lazily convert the newest admitted callback, once per sequence."""

        pending = self._pending_qt_frame
        sequence = self._pending_frame_sequence
        if pending is None or sequence <= 0:
            return None
        if sequence == self._attempted_frame_sequence:
            return self._latest_frame

        # Mark attempted before entering driver/image code. Every failure path
        # is therefore cached for this exact callback sequence and cannot be
        # retried by the 10 ms preview poll. A later admitted callback advances
        # the sequence and is eligible for one fresh attempt.
        self._attempted_frame_sequence = sequence
        self._conversion_attempt_count += 1
        try:
            image = pending.toImage()  # type: ignore[attr-defined]
            detached = image.copy()
            if detached.isNull() or detached.width() <= 0 or detached.height() <= 0:
                self._failed_conversion_count += 1
                return self._latest_frame
            width = detached.width()
            height = detached.height()
        except Exception:  # noqa: BLE001 - never crash on a bad frame
            self._failed_conversion_count += 1
            return self._latest_frame

        packet = SourceFramePacket(
            frame_id=str(uuid.uuid4()),
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=width,
            height=height,
            image=detached,
        )
        self._latest_frame = packet
        self._successful_conversion_count += 1
        if self._selected_resolution is None:
            self._selected_resolution = (width, height)
        return packet

    def is_running(self) -> bool:
        return self._running

    def metrics(self) -> dict[str, object]:
        """Return sanitized counters without device identifiers or raw objects.

        ``incoming_frame_count`` is the admitted latest-only preview cadence
        used by the UI telemetry. ``raw_callback_count`` records all driver
        callbacks, including frames intentionally dropped before conversion.
        """

        return {
            "incoming_frame_count": self._incoming_frame_count,
            "raw_callback_count": self._raw_callback_count,
            "dropped_preview_frame_count": self._dropped_preview_frame_count,
            "preview_processing_max_fps": _PREVIEW_PROCESSING_MAX_FPS,
            # Backward-compatible key: successful source-frame conversions.
            "conversion_count": self._successful_conversion_count,
            "conversion_attempt_count": self._conversion_attempt_count,
            "successful_conversion_count": self._successful_conversion_count,
            "failed_conversion_count": self._failed_conversion_count,
            "selected_resolution": self._selected_resolution,
            "preferred_720p_applied": self._preferred_720p_applied,
            "preview_mode": "fallback",
        }

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _stream_timestamp_ns(qt_frame: object) -> int | None:
        """Read QVideoFrame stream time; legacy test fakes may omit it.

        QVideoFrame.startTime() is expressed in microseconds. A negative value
        means unavailable, in which case production falls back to monotonic
        time. Objects without a startTime method are treated as legacy fakes
        and bypass cadence thinning so existing contract probes stay stable.
        """

        method = getattr(qt_frame, "startTime", None)
        if not callable(method):
            return None
        try:
            start_time_us = int(method())
        except Exception:  # noqa: BLE001 - malformed driver metadata is safe
            start_time_us = -1
        if start_time_us >= 0:
            return start_time_us * 1_000
        return time.monotonic_ns()

    def _admit_preview_frame(self, qt_frame: object) -> bool:
        stream_ns = self._stream_timestamp_ns(qt_frame)
        if stream_ns is None:
            return True
        previous_ns = self._last_admitted_stream_ns
        if previous_ns is None or stream_ns < previous_ns:
            self._last_admitted_stream_ns = stream_ns
            return True
        elapsed_ns = stream_ns - previous_ns
        if elapsed_ns + _PREVIEW_PROCESSING_TOLERANCE_NS < _PREVIEW_PROCESSING_INTERVAL_NS:
            return False
        self._last_admitted_stream_ns = stream_ns
        return True

    def _on_qt_frame(
        self,
        qt_frame: object,
        _on_frame: Callable[[SourceFramePacket], None] | None,
    ) -> None:
        """Latest-only callback with a 30 fps pre-conversion admission cap."""

        if not self._running:
            return
        self._raw_callback_count += 1
        if not self._admit_preview_frame(qt_frame):
            self._dropped_preview_frame_count += 1
            return
        self._incoming_frame_count += 1
        self._pending_frame_sequence += 1
        self._pending_qt_frame = qt_frame

    def _teardown(self) -> None:
        if self._camera is not None:
            with contextlib.suppress(Exception):
                self._camera.stop()  # type: ignore[attr-defined]
        self._camera = None
        self._capture_session = None
        self._video_sink = None
        self._owner = None
        self._running = False
        self._latest_frame = None
        self._pending_qt_frame = None
        self._pending_frame_sequence = 0
        self._attempted_frame_sequence = 0
        self._incoming_frame_count = 0
        self._raw_callback_count = 0
        self._dropped_preview_frame_count = 0
        self._last_admitted_stream_ns = None
        self._conversion_attempt_count = 0
        self._successful_conversion_count = 0
        self._failed_conversion_count = 0
        self._selected_resolution = None
        self._preferred_720p_applied = False
