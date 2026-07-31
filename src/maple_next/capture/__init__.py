"""Safe UGREEN direct-capture package.

Public surface for a future integrator (Lane 02):

    CaptureService(backend).start() -> CaptureStatus
    CaptureService(backend).stop() -> CaptureStatus
    CaptureService(backend).latest_status() -> CaptureStatus
    CaptureService(backend).latest_frame() -> FramePacket | None

Production backend: QtMultimediaUgreenBackend (PySide6 Qt Multimedia).
Manual entry must always remain possible regardless of capture health -
CaptureStatus.manual_entry_allowed is always True.
"""

from maple_next.capture.contracts import (
    CAPTURE_OPERATOR_MESSAGES,
    DEFAULT_DEVICE_SELECTOR,
    DEFAULT_FRAME_FRESHNESS_MS,
    FRAME_SOURCE_UGREEN_DIRECT,
    CaptureErrorCode,
    CaptureStatus,
    CaptureStatusCode,
    DeviceOpenResult,
    FramePacket,
    VideoCaptureBackend,
)
from maple_next.capture.service import CaptureService, resolve_device_selector

__all__ = [
    "CAPTURE_OPERATOR_MESSAGES",
    "DEFAULT_DEVICE_SELECTOR",
    "DEFAULT_FRAME_FRESHNESS_MS",
    "FRAME_SOURCE_UGREEN_DIRECT",
    "CaptureErrorCode",
    "CaptureService",
    "CaptureStatus",
    "CaptureStatusCode",
    "DeviceOpenResult",
    "FramePacket",
    "VideoCaptureBackend",
    "resolve_device_selector",
]
