from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage

from maple_next.capture.canonical import canonicalize_frame_packet
from maple_next.capture.contracts import DeviceOpenResult, FramePacket
from maple_next.capture.qt_ugreen import select_exact_720p_format
from maple_next.capture.service import CaptureService
from maple_next.ocr.contracts import CanonicalOcrRoi, OcrCandidateContext
from maple_next.ocr.service import OcrCandidateService


def _packet(image: QImage, frame_id: str = "frame-1") -> FramePacket:
    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=1,
        width=image.width(),
        height=image.height(),
        image=image,
    )


class _Format:
    def __init__(self, width: int, height: int) -> None:
        self._resolution = QSize(width, height)

    def resolution(self) -> QSize:
        return self._resolution


class _RecordingOcrBackend:
    def __init__(self) -> None:
        self.frame: FramePacket | None = None

    def is_available(self) -> bool:
        return True

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[object, ...]:
        self.frame = frame
        return ()


def test_exact_720p_device_format_is_selected_without_fps_requirement() -> None:
    formats = (_Format(2560, 1440), _Format(1280, 720), _Format(1920, 1080))
    assert select_exact_720p_format(formats) is formats[1]
    assert select_exact_720p_format((_Format(2560, 1440),)) is None


def test_exact_720p_packet_is_not_resized() -> None:
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    packet = _packet(image)
    canonical = canonicalize_frame_packet(packet)
    assert canonical is not None
    assert canonical.image is image
    assert canonical.width == 1280
    assert canonical.height == 720
    assert canonical.canonical_resize_count == 0


def test_non_720p_packet_is_smoothly_canonicalized_once() -> None:
    image = QImage(2560, 1440, QImage.Format.Format_RGB32)
    canonical = canonicalize_frame_packet(_packet(image, "native-1440p"))
    assert canonical is not None
    assert canonical.frame_id == "native-1440p"
    assert (canonical.width, canonical.height) == (1280, 720)
    assert (canonical.source_width, canonical.source_height) == (2560, 1440)
    assert canonical.canonical_resize_count == 1


def test_ocr_rejects_noncanonical_frame_before_backend() -> None:
    backend = _RecordingOcrBackend()
    service = OcrCandidateService(backend)  # type: ignore[arg-type]
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    packet = _packet(image)
    bundle = service.request_candidates(
        frame=packet,
        frame_age_ms=0,
        fresh=True,
        context=OcrCandidateContext(),
    )
    assert bundle.status == "FRAME_NOT_CANONICAL"
    assert bundle.manual_entry_allowed is True
    assert backend.frame is None


def test_roi_contract_uses_1280x720_or_normalized_coordinates() -> None:
    assert CanonicalOcrRoi(0, 0, 1280, 720) == CanonicalOcrRoi.from_normalized(
        x=0.0, y=0.0, width=1.0, height=1.0
    )
    assert CanonicalOcrRoi.from_normalized(
        x=0.5, y=0.5, width=0.25, height=0.25
    ) == CanonicalOcrRoi(640, 360, 320, 180)
    with pytest.raises(ValueError):
        CanonicalOcrRoi(1200, 700, 100, 30)


# -- non-16:9 nondestructive letterbox/pillarbox coverage -----------------------

_RED = QColor(255, 0, 0)
_BLUE = QColor(0, 0, 255)
_GREEN = QColor(0, 255, 0)
_YELLOW = QColor(255, 255, 0)


def _bordered_image(width: int, height: int) -> QImage:
    """A source image with a distinct marker color on each of its four edges."""

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    for x in range(width):
        image.setPixelColor(x, 0, _RED)
        image.setPixelColor(x, height - 1, _BLUE)
    for y in range(height):
        image.setPixelColor(0, y, _GREEN)
        image.setPixelColor(width - 1, y, _YELLOW)
    return image


def _is_marker_color(actual: QColor, expected: QColor, threshold: int = 100) -> bool:
    """True if the marker's channels dominate, tolerating smooth-scale blending
    with the adjacent black background (the marker is one pixel wide)."""

    for channel in ("red", "green", "blue"):
        value = getattr(actual, channel)()
        if getattr(expected, channel)() > 0:
            if value < threshold:
                return False
        elif value >= threshold:
            return False
    return True


def _assert_all_edge_markers_preserved(source_width: int, source_height: int) -> None:
    image = _bordered_image(source_width, source_height)
    packet = _packet(image, f"marker-{source_width}x{source_height}")
    canonical = canonicalize_frame_packet(packet)

    assert canonical is not None
    assert (canonical.width, canonical.height) == (1280, 720)
    assert canonical.canonical_resize_count == 1
    assert canonical.content_rect is not None

    left, top, content_width, content_height = canonical.content_rect
    assert left >= 0 and top >= 0
    assert left + content_width <= 1280
    assert top + content_height <= 720

    out = canonical.image
    mid_x = left + content_width // 2
    mid_y = top + content_height // 2

    assert _is_marker_color(out.pixelColor(mid_x, top), _RED), "top marker was lost"
    assert _is_marker_color(
        out.pixelColor(mid_x, top + content_height - 1), _BLUE
    ), "bottom marker was lost"
    assert _is_marker_color(out.pixelColor(left, mid_y), _GREEN), "left marker was lost"
    assert _is_marker_color(
        out.pixelColor(left + content_width - 1, mid_y), _YELLOW
    ), "right marker was lost"


def test_4_3_input_preserves_top_bottom_left_right_markers() -> None:
    _assert_all_edge_markers_preserved(800, 600)


def test_portrait_input_preserves_all_edge_markers() -> None:
    _assert_all_edge_markers_preserved(720, 1280)


def test_ultrawide_input_preserves_all_edge_markers() -> None:
    _assert_all_edge_markers_preserved(2560, 1080)


@pytest.mark.parametrize(
    ("source_width", "source_height", "expected_resize_count"),
    [
        (1280, 720, 0),
        (1920, 1080, 1),
        (2560, 1440, 1),
        (800, 600, 1),
        (720, 1280, 1),
        (2560, 1080, 1),
    ],
)
def test_canonical_output_is_always_1280x720(
    source_width: int, source_height: int, expected_resize_count: int
) -> None:
    image = QImage(source_width, source_height, QImage.Format.Format_RGB32)
    canonical = canonicalize_frame_packet(_packet(image))
    assert canonical is not None
    assert (canonical.width, canonical.height) == (1280, 720)
    assert canonical.canonical_resize_count == expected_resize_count


class _FakeCaptureBackend:
    """Minimal VideoCaptureBackend double for same-frame cache/parity checks."""

    def __init__(self) -> None:
        self._running = False
        self._frame: FramePacket | None = None
        self._on_frame: Callable[[FramePacket], None] | None = None

    def start(
        self, selector: str, on_frame: Callable[[FramePacket], None] | None = None
    ) -> DeviceOpenResult:
        self._on_frame = on_frame
        self._running = True
        return DeviceOpenResult(
            opened=True, device_found=True, device_label="fake", error_code=None
        )

    def stop(self) -> None:
        self._running = False

    def get_latest_frame(self) -> FramePacket | None:
        return self._frame

    def is_running(self) -> bool:
        return self._running

    def push_frame(self, image: QImage) -> None:
        frame = FramePacket(
            frame_id=str(uuid.uuid4()),
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=image.width(),
            height=image.height(),
            image=image,
        )
        self._frame = frame
        if self._on_frame is not None:
            self._on_frame(frame)


def test_same_frame_cache_returns_identical_canonical_object() -> None:
    backend = _FakeCaptureBackend()
    service = CaptureService(backend)
    service.start()
    backend.push_frame(QImage(800, 600, QImage.Format.Format_RGB32))

    _status_a, frame_a = service.latest_snapshot()
    _status_b, frame_b = service.latest_snapshot()

    assert frame_a is not None
    assert frame_a is frame_b, "same source frame_id must not be re-canonicalized"


def test_preview_and_ocr_share_same_canonical_frame_and_frame_id() -> None:
    backend = _FakeCaptureBackend()
    service = CaptureService(backend)
    service.start()
    backend.push_frame(QImage(720, 1280, QImage.Format.Format_RGB32))

    preview_status, preview_frame = service.latest_snapshot()
    ocr_frame = service.latest_frame()

    assert preview_frame is not None
    assert ocr_frame is preview_frame
    assert preview_status.frame_id == ocr_frame.frame_id
    assert ocr_frame.canonical_resize_count == 1
    assert ocr_frame.content_rect is not None
