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
from typing import TypeVar

from maple_next.capture.contracts import DeviceOpenResult, FramePacket

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


_CameraFormatT = TypeVar("_CameraFormatT")


def select_exact_720p_format(formats: Sequence[_CameraFormatT]) -> _CameraFormatT | None:
    """Return the first device format whose declared resolution is 1280x720."""

    for camera_format in formats:
        try:
            resolution = camera_format.resolution()  # type: ignore[attr-defined]
            if resolution.width() == 1280 and resolution.height() == 720:
                return camera_format
        except Exception:  # noqa: BLE001 - malformed driver format is ignored
            continue
    return None


class QtMultimediaUgreenBackend:
    """VideoCaptureBackend implementation backed by QMediaDevices/QCamera.

    The videoFrameChanged callback (which fires at the source's own cadence -
    up to 60x/sec) never touches Python image data: it only stores a
    reference to the newest QVideoFrame and bumps a counter. The expensive
    work - toImage()/copy()/FramePacket construction - happens lazily inside
    get_latest_frame(), and only once per distinct incoming frame (a repeat
    poll before the next frame arrives returns the cached FramePacket).
    Preview and OCR polling can therefore both call get_latest_frame() as
    often as they like without multiplying conversion cost.
    """

    def __init__(self) -> None:
        self._camera: object | None = None
        self._capture_session: object | None = None
        self._video_sink: object | None = None
        self._running = False
        self._device_label: str | None = None
        self._latest_frame: FramePacket | None = None
        self._owner: QObject | None = None
        self._pending_qt_frame: object | None = None
        self._converted_qt_frame: object | None = None
        self._incoming_frame_count = 0
        self._conversion_count = 0
        self._selected_resolution: tuple[int, int] | None = None

    def start(
        self, selector: str, on_frame: Callable[[FramePacket], None] | None = None
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
            # Deliberately do not call camera.setCameraFormat(): FPS
            # maximization is not a goal here, and forcing a format can
            # fight the driver's own negotiation. Auto-negotiation is left
            # in charge; whatever resolution/cadence the device actually
            # produces is what preview and (separately) OCR canonicalization
            # work with. select_exact_720p_format stays available as a pure
            # helper for a future deterministic fallback if a specific
            # driver is proven to need one, but nothing here calls it.
            camera = QCamera(chosen, self._owner)
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

    def get_latest_frame(self) -> FramePacket | None:
        """Pull (and lazily convert) the newest incoming frame.

        Safe to call at any polling rate from preview and/or OCR: a QImage
        conversion only happens the first time a *new* QVideoFrame is seen.
        Repeated calls between two incoming frames are effectively free -
        they just return the same cached FramePacket object.
        """

        pending = self._pending_qt_frame
        if pending is None:
            return None
        if pending is self._converted_qt_frame and self._latest_frame is not None:
            return self._latest_frame
        try:
            image = pending.toImage()  # type: ignore[attr-defined]
            detached = image.copy()
            if detached.isNull() or detached.width() <= 0 or detached.height() <= 0:
                return self._latest_frame
            width = detached.width()
            height = detached.height()
        except Exception:  # noqa: BLE001 - never crash on a bad frame
            return self._latest_frame

        packet = FramePacket(
            frame_id=str(uuid.uuid4()),
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=width,
            height=height,
            image=detached,
        )
        self._latest_frame = packet
        self._converted_qt_frame = pending
        self._conversion_count += 1
        return packet

    def is_running(self) -> bool:
        return self._running

    def metrics(self) -> dict[str, object]:
        """Sanitized, hardware-free counters for the local verification report.

        Never exposes a raw device id, driver object, or filesystem detail -
        only counters and the negotiated resolution.
        """

        return {
            "incoming_frame_count": self._incoming_frame_count,
            "conversion_count": self._conversion_count,
            "selected_resolution": self._selected_resolution,
            "preview_mode": "fallback",
        }

    # -- internals --------------------------------------------------------------

    def _on_qt_frame(
        self, qt_frame: object, _on_frame: Callable[[FramePacket], None] | None
    ) -> None:
        """Source-cadence callback: bookkeeping only, no image processing.

        This is invoked once per frame the driver produces (potentially 60x/
        sec). It must stay O(1) and allocation-free of Python image data;
        get_latest_frame() does the actual (lazy, cached) conversion work.
        The push-style ``_on_frame`` callback is intentionally never invoked
        from here - polling via get_latest_frame() is the only frame path,
        so nothing re-introduces a per-frame conversion cost.
        """

        if not self._running:
            return
        self._incoming_frame_count += 1
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
        self._converted_qt_frame = None
        self._incoming_frame_count = 0
        self._conversion_count = 0
        self._selected_resolution = None
