from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from maple_next.capture.canonical import canonicalize_frame_packet
from maple_next.capture.contracts import FramePacket
from maple_next.capture.qt_ugreen import select_exact_720p_format
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
