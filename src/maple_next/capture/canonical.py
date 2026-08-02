"""One-pass construction of the shared 1280x720 preview/OCR working frame."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    FramePacket,
)

_FULL_CANVAS_CONTENT_RECT = (0, 0, CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT)


def canonicalize_frame_packet(frame: FramePacket) -> FramePacket | None:
    """Return one immutable canonical packet, preserving the capture frame id.

    Exact 1280x720 frames are passed through without a resize. Every other
    drawable source uses exactly one smooth scale that keeps the source
    aspect ratio (never expanding beyond it), and the scaled result is
    letterboxed/pillarboxed onto a 1280x720 canvas without cropping a single
    source pixel. ``content_rect`` on the returned packet marks the region of
    the canvas that holds real source content; anything outside it is padding.
    """

    image = frame.image
    if not isinstance(image, QImage):
        if frame.width == CANONICAL_FRAME_WIDTH and frame.height == CANONICAL_FRAME_HEIGHT:
            return replace(
                frame,
                source_width=frame.source_width or frame.width,
                source_height=frame.source_height or frame.height,
                canonical_resize_count=0,
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
        return replace(
            frame,
            width=CANONICAL_FRAME_WIDTH,
            height=CANONICAL_FRAME_HEIGHT,
            source_width=source_width,
            source_height=source_height,
            canonical_resize_count=0,
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

    return replace(
        frame,
        width=CANONICAL_FRAME_WIDTH,
        height=CANONICAL_FRAME_HEIGHT,
        image=canvas,
        source_width=source_width,
        source_height=source_height,
        canonical_resize_count=1,
        content_rect=(left, top, scaled.width(), scaled.height()),
    )
