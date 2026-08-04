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

    The source-cadence callback never touches Python image data: it stores only
    the newest QVideoFrame reference and advances a monotonically increasing
    sequence. ``get_latest_frame()`` performs ``toImage()``/``copy()`` lazily
    and at most once for each callback sequence, whether conversion succeeds
    or fails. A stalled malformed frame therefore cannot create a 10 ms retry
    loop, while a later distinct valid callback recovers normally.

    The operator-selected low-load policy requests exact 1280x720 input. It
    preserves the default cadence when choosing between multiple 720p formats
    and falls back to Qt/driver auto-negotiation if 720p cannot be applied.
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
        """Lazily convert the newest callback frame, at most once per sequence."""

        pending = self._pending_qt_frame
        sequence = self._pending_frame_sequence
        if pending is None or sequence <= 0:
            return None
        if sequence == self._attempted_frame_sequence:
            return self._latest_frame

        # Mark attempted before entering driver/image code. Every failure path
        # is therefore cached for this exact callback sequence and cannot be
        # retried by the 10 ms preview poll. A later callback advances the
        # sequence and is eligible for one fresh attempt.
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
        """Return sanitized counters without device identifiers or raw objects."""

        return {
            "incoming_frame_count": self._incoming_frame_count,
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

    def _on_qt_frame(
        self,
        qt_frame: object,
        _on_frame: Callable[[SourceFramePacket], None] | None,
    ) -> None:
        """Source-cadence callback: latest-only bookkeeping, no image work."""

        if not self._running:
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
        self._conversion_attempt_count = 0
        self._successful_conversion_count = 0
        self._failed_conversion_count = 0
        self._selected_resolution = None
        self._preferred_720p_applied = False
