"""One-pass construction of the shared 1280x720 preview/OCR working frame."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    FramePacket,
)


def canonicalize_frame_packet(frame: FramePacket) -> FramePacket | None:
    """Return one immutable canonical packet, preserving the capture frame id.

    Exact 1280x720 frames are passed through without a resize. Every other
    drawable source uses exactly one smooth scale; a centered crop may follow
    for a non-16:9 source, but never another scale or codec operation.
    """

    image = frame.image
    if not isinstance(image, QImage):
        if frame.width == CANONICAL_FRAME_WIDTH and frame.height == CANONICAL_FRAME_HEIGHT:
            return replace(
                frame,
                source_width=frame.source_width or frame.width,
                source_height=frame.source_height or frame.height,
                canonical_resize_count=0,
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
        )

    scaled = image.scaled(
        CANONICAL_FRAME_WIDTH,
        CANONICAL_FRAME_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.isNull():
        return None
    left = max(0, (scaled.width() - CANONICAL_FRAME_WIDTH) // 2)
    top = max(0, (scaled.height() - CANONICAL_FRAME_HEIGHT) // 2)
    canonical = scaled.copy(left, top, CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT)
    if canonical.isNull():
        return None
    return replace(
        frame,
        width=CANONICAL_FRAME_WIDTH,
        height=CANONICAL_FRAME_HEIGHT,
        image=canonical,
        source_width=source_width,
        source_height=source_height,
        canonical_resize_count=1,
    )
