"""Exact ROI identity hashes full normalized image content, not grayscale only."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage

from maple_next.selection_roi.matcher import ImageFingerprint


def _solid(color: str) -> QImage:
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def test_same_pixels_have_same_exact_hash() -> None:
    first = _solid("#ff0000")
    second = first.copy()

    assert ImageFingerprint.from_image(first).exact_hash == (
        ImageFingerprint.from_image(second).exact_hash
    )


def test_different_color_pixels_have_different_exact_hash() -> None:
    first = _solid("#ff0000")
    second = _solid("#00ff00")

    assert ImageFingerprint.from_image(first).exact_hash != (
        ImageFingerprint.from_image(second).exact_hash
    )
