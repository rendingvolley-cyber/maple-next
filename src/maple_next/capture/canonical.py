"""One-pass construction of the shared 1280x720 OCR working frame."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    FramePacket,
    SourceFramePacket,
)

_FULL_CANVAS_CONTENT_RECT = (0, 0, CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT)


def _canonical_packet(
    frame: SourceFramePacket | FramePacket,
    *,
    image: object,
    source_width: int,
    source_height: int,
    resize_count: int,
    content_rect: tuple[int, int, int, int],
) -> FramePacket:
    return FramePacket(
        frame_id=frame.frame_id,
        source=frame.source,
        captured_at_utc=frame.captured_at_utc,
        captured_monotonic_ns=frame.captured_monotonic_ns,
        width=CANONICAL_FRAME_WIDTH,
        height=CANONICAL_FRAME_HEIGHT,
        image=image,
        source_width=source_width,
        source_height=source_height,
        canonical_resize_count=resize_count,
        content_rect=content_rect,
    )


def canonicalize_frame_packet(
    frame: SourceFramePacket | FramePacket,
) -> FramePacket | None:
    """Return one immutable canonical packet, preserving the source frame id.

    Exact 1280x720 frames are passed through without a resize. Every other
    drawable source uses exactly one smooth scale that keeps the source
    aspect ratio (never expanding beyond it), and the scaled result is
    letterboxed/pillarboxed onto a 1280x720 canvas without cropping a single
    source pixel. ``content_rect`` on the returned packet marks the region of
    the canvas that holds real source content; anything outside it is padding.

    ``FramePacket`` input remains accepted for compatibility with older test
    fakes, but the production backend and preview path use
    ``SourceFramePacket``. The return type is always canonical ``FramePacket``.
    """

    image = frame.image
    if not isinstance(image, QImage):
        if frame.width == CANONICAL_FRAME_WIDTH and frame.height == CANONICAL_FRAME_HEIGHT:
            source_width = (
                frame.source_width
                if isinstance(frame, FramePacket) and frame.source_width is not None
                else frame.width
            )
            source_height = (
                frame.source_height
                if isinstance(frame, FramePacket) and frame.source_height is not None
                else frame.height
            )
            return _canonical_packet(
                frame,
                image=image,
                source_width=source_width,
                source_height=source_height,
                resize_count=0,
                content_rect=_FULL_CANVAS_CONTENT_RECT,
            )
        return None
    if image.isNull():
        return None
    source_width = image.width()
    source_height = image.height()
    if source_width <= 0 or source_height <= 0:
        return None

    if source_width == CANONICAL_FRAME_WIDTH and source_height == CANONICAL_FRAME_HEIGHT:
        return _canonical_packet(
            frame,
            image=image,
            source_width=source_width,
            source_height=source_height,
            resize_count=0,
            content_rect=_FULL_CANVAS_CONTENT_RECT,
        )

    scaled = image.scaled(
        CANONICAL_FRAME_WIDTH,
        CANONICAL_FRAME_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.isNull():
        return None

    canvas = QImage(CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT, QImage.Format.Format_RGB32)
    canvas.fill(Qt.GlobalColor.black)
    left = max(0, (CANONICAL_FRAME_WIDTH - scaled.width()) // 2)
    top = max(0, (CANONICAL_FRAME_HEIGHT - scaled.height()) // 2)
    painter = QPainter(canvas)
    try:
        painter.drawImage(left, top, scaled)
    finally:
        painter.end()

    return _canonical_packet(
        frame,
        image=canvas,
        source_width=source_width,
        source_height=source_height,
        resize_count=1,
        content_rect=(left, top, scaled.width(), scaled.height()),
    )
