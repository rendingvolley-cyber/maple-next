"""Capture-side contracts: immutable status/frame types and backend Protocol.

These types are deliberately free of any UGREEN/Qt hardware behaviour. They exist
so the capture service, the Qt backend, and test fakes can all agree on a shape
without importing each other's internals.

Nothing in this module is allowed to call into domain/application/persistence
code. Capture is a leaf dependency: it only produces sanitized status snapshots
for a future integrator (Lane 02) to read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

#: Default UGREEN direct-capture source tag carried on every frame.
FRAME_SOURCE_UGREEN_DIRECT = "UGREEN_DIRECT"

#: Frames older than this (monotonic clock) are considered stale and must not
#: be handed to OCR candidate generation.
DEFAULT_FRAME_FRESHNESS_MS = 2000

#: Optional, non-secret override for the UGREEN device selector substring.
CAPTURE_DEVICE_ENV_VAR = "MAPLE_NEXT_CAPTURE_DEVICE"

#: Default selector substring used to find the UGREEN capture device among
#: the OS-reported video input descriptions.
DEFAULT_DEVICE_SELECTOR = "UGREEN"

#: Single canonical pixel coordinate space shared by OCR consumers.
CANONICAL_FRAME_WIDTH = 1280
CANONICAL_FRAME_HEIGHT = 720
CANONICAL_FRAME_ASPECT_RATIO = (16, 9)


class CaptureStatusCode(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    AVAILABLE = "AVAILABLE"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    OPEN_FAILED = "OPEN_FAILED"
    FRAME_UNAVAILABLE = "FRAME_UNAVAILABLE"
    FRAME_STALE = "FRAME_STALE"
    CAPTURE_ERROR = "CAPTURE_ERROR"


class CaptureErrorCode(StrEnum):
    """Fixed set of sanitized capture error codes. Never invent new ones ad hoc."""

    CAPTURE_DEVICE_UNAVAILABLE = "CAPTURE_DEVICE_UNAVAILABLE"
    CAPTURE_OPEN_FAILED = "CAPTURE_OPEN_FAILED"
    CAPTURE_FRAME_UNAVAILABLE = "CAPTURE_FRAME_UNAVAILABLE"
    CAPTURE_FRAME_STALE = "CAPTURE_FRAME_STALE"
    CAPTURE_FAILED = "CAPTURE_FAILED"


class FrameKind(StrEnum):
    """Explicit pixel-space identity carried by every frame packet."""

    SOURCE = "SOURCE"
    CANONICAL = "CANONICAL"


#: Fixed, sanitized Japanese operator messages. Never concatenate raw exception
#: text, device paths, or backend diagnostics into these strings.
CAPTURE_OPERATOR_MESSAGES: dict[str, str] = {
    CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE: (
        "UGREEN映像を取得できません。手動入力で続行できます。"
    ),
    CaptureErrorCode.CAPTURE_OPEN_FAILED: (
        "UGREEN映像を開始できません。手動入力で続行できます。"
    ),
    CaptureErrorCode.CAPTURE_FRAME_UNAVAILABLE: (
        "映像フレームを取得できません。手動入力で続行できます。"
    ),
    CaptureErrorCode.CAPTURE_FRAME_STALE: (
        "最新フレームが古いためOCR候補を使用しません。手動入力で続行できます。"
    ),
    CaptureErrorCode.CAPTURE_FAILED: (
        "映像取得でエラーが発生しました。手動入力で続行できます。"
    ),
}


def sanitized_capture_message(error_code: str | None) -> str | None:
    """Look up the fixed operator message for a capture error code.

    Returns None when there is no error (nothing to sanitize/display).
    """

    if error_code is None:
        return None
    return CAPTURE_OPERATOR_MESSAGES.get(
        error_code, "映像取得でエラーが発生しました。手動入力で続行できます。"
    )


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """Sanitized, immutable snapshot of capture state for UI/consumer display.

    manual_entry_allowed is always True: capture/OCR health must never gate a
    human's ability to enter turn facts by hand.
    """

    status: str
    available: bool
    manual_entry_allowed: bool
    source: str
    device_label: str | None
    frame_id: str | None
    captured_at_utc: datetime | None
    age_ms: int | None
    fresh: bool
    width: int | None
    height: int | None
    error_code: str | None
    operator_message: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "available": self.available,
            "manual_entry_allowed": self.manual_entry_allowed,
            "source": self.source,
            "device_label": self.device_label,
            "frame_id": self.frame_id,
            "captured_at_utc": (
                self.captured_at_utc.isoformat() if self.captured_at_utc else None
            ),
            "age_ms": self.age_ms,
            "fresh": self.fresh,
            "width": self.width,
            "height": self.height,
            "error_code": self.error_code,
            "operator_message": self.operator_message,
        }


@dataclass(frozen=True, slots=True)
class FramePacket:
    """A canonical 1280x720 OCR working frame.

    ``width``/``height`` describe the canonical canvas, never the raw capture
    dimensions. ``source_width``/``source_height`` preserve the source size and
    ``content_rect`` identifies the real pixels inside any letterbox/pillarbox
    padding. OCR consumers accept packets whose ``frame_kind`` is CANONICAL.
    """

    frame_id: str
    source: str
    captured_at_utc: datetime
    captured_monotonic_ns: int
    width: int
    height: int
    image: Any = None
    source_width: int | None = None
    source_height: int | None = None
    canonical_resize_count: int = 0
    #: (x, y, width, height) of the actual scaled source content inside the
    #: 1280x720 canonical canvas. Any area outside this rect is letterbox or
    #: pillarbox padding, not source content, and OCR/consumers must not treat
    #: it as captured pixels.
    content_rect: tuple[int, int, int, int] | None = None
    frame_kind: FrameKind = FrameKind.CANONICAL


@dataclass(frozen=True, slots=True)
class SourceFramePacket(FramePacket):
    """One raw source frame for preview and canonicalization input.

    The distinct runtime type and SOURCE kind make the boundary explicit while
    retaining ``FramePacket`` compatibility for existing display-only widgets.
    ``width``/``height`` and ``image`` describe exactly what the capture backend
    produced. OCR rejects SOURCE packets before backend recognition.
    """

    frame_kind: FrameKind = FrameKind.SOURCE


@dataclass(frozen=True, slots=True)
class DeviceOpenResult:
    """Result of a backend's attempt to open its capture device."""

    opened: bool
    device_found: bool
    device_label: str | None
    error_code: str | None


class VideoCaptureBackend(Protocol):
    """Dependency-inversion seam between the capture service and hardware.

    Implementations must never raise out of start()/stop()/get_latest_frame() -
    all failure must be reported through DeviceOpenResult / a None frame. The
    capture service treats any unexpected exception from a backend as a
    sanitized CAPTURE_FAILED result, but well-behaved backends should not rely
    on that safety net.
    """

    def start(
        self, selector: str, on_frame: Callable[[SourceFramePacket], None] | None = None
    ) -> DeviceOpenResult: ...

    def stop(self) -> None: ...

    def get_latest_frame(self) -> SourceFramePacket | None: ...

    def is_running(self) -> bool: ...
