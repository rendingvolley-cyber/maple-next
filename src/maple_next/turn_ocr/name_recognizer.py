"""Candidate-constrained active-name recognition without unrestricted text output.

The live Pokémon Champions HUD uses bright italic text on saturated team-color
plates. Comparing raw edge maps lets those plates dominate the score, so this
module first isolates the low-saturation bright glyph interior, normalizes the
glyph bounding box, and then compares it with several Windows/Japanese font
variants. The recognizer still cannot invent a name outside the supplied,
human-confirmed candidate pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

_MASK_WIDTH: Final[int] = 128
_MASK_HEIGHT: Final[int] = 32
_FONT_FAMILIES: Final[tuple[str, ...]] = (
    "Yu Gothic UI",
    "Meiryo UI",
    "Meiryo",
    "MS Gothic",
    "Sans Serif",
)
_FONT_STRETCHES: Final[tuple[int, ...]] = (85, 100, 115)
Mask = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class NameCandidateMatch:
    label: str
    score: float
    rank: int


def _is_bright_glyph(red: int, green: int, blue: int) -> bool:
    maximum = max(red, green, blue)
    minimum = min(red, green, blue)
    brightness = (red + green + blue) // 3
    return maximum >= 145 and brightness >= 130 and maximum - minimum <= 85


def _normalize_glyph_mask(image: QImage) -> Mask:
    source = image.convertToFormat(QImage.Format.Format_RGB32)
    width = source.width()
    height = source.height()
    points: list[tuple[int, int]] = []
    source_mask = [[False for _x in range(width)] for _y in range(height)]
    for y in range(height):
        for x in range(width):
            color = source.pixelColor(x, y)
            if _is_bright_glyph(color.red(), color.green(), color.blue()):
                source_mask[y][x] = True
                points.append((x, y))
    if len(points) < 8:
        return (0,) * (_MASK_WIDTH * _MASK_HEIGHT)

    x_min = min(point[0] for point in points)
    x_max = max(point[0] for point in points)
    y_min = min(point[1] for point in points)
    y_max = max(point[1] for point in points)
    glyph_width = x_max - x_min + 1
    glyph_height = y_max - y_min + 1
    if glyph_width <= 0 or glyph_height <= 0:
        return (0,) * (_MASK_WIDTH * _MASK_HEIGHT)

    scale = min(
        (_MASK_WIDTH - 4) / glyph_width,
        (_MASK_HEIGHT - 4) / glyph_height,
    )
    target_width = max(1, min(_MASK_WIDTH - 4, round(glyph_width * scale)))
    target_height = max(1, min(_MASK_HEIGHT - 4, round(glyph_height * scale)))
    x_offset = 2
    y_offset = (_MASK_HEIGHT - target_height) // 2
    normalized = [0] * (_MASK_WIDTH * _MASK_HEIGHT)
    for target_y in range(target_height):
        source_y = y_min + min(
            glyph_height - 1,
            ((2 * target_y + 1) * glyph_height) // (2 * target_height),
        )
        for target_x in range(target_width):
            source_x = x_min + min(
                glyph_width - 1,
                ((2 * target_x + 1) * glyph_width) // (2 * target_width),
            )
            if source_mask[source_y][source_x]:
                normalized[(y_offset + target_y) * _MASK_WIDTH + x_offset + target_x] = 1
    return _dilate(tuple(normalized))


def _dilate(mask: Mask) -> Mask:
    output = [0] * len(mask)
    for y in range(_MASK_HEIGHT):
        for x in range(_MASK_WIDTH):
            if not mask[y * _MASK_WIDTH + x]:
                continue
            for neighbour_y in range(max(0, y - 1), min(_MASK_HEIGHT, y + 2)):
                for neighbour_x in range(max(0, x - 1), min(_MASK_WIDTH, x + 2)):
                    output[neighbour_y * _MASK_WIDTH + neighbour_x] = 1
    return tuple(output)


def _profile_cosine(left: Mask, right: Mask, *, horizontal: bool) -> float:
    length = _MASK_WIDTH if horizontal else _MASK_HEIGHT
    left_profile = [0.0] * length
    right_profile = [0.0] * length
    for y in range(_MASK_HEIGHT):
        for x in range(_MASK_WIDTH):
            index = y * _MASK_WIDTH + x
            profile_index = x if horizontal else y
            left_profile[profile_index] += float(left[index])
            right_profile[profile_index] += float(right[index])
    dot = sum(a * b for a, b in zip(left_profile, right_profile, strict=True))
    left_norm = sqrt(sum(value * value for value in left_profile))
    right_norm = sqrt(sum(value * value for value in right_profile))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _glyph_similarity(left: Mask, right: Mask) -> float:
    left_count = sum(left)
    right_count = sum(right)
    if left_count < 12 or right_count < 12:
        return 0.0
    left_dilated = _dilate(left)
    right_dilated = _dilate(right)
    left_covered = sum(
        1
        for left_pixel, right_pixel in zip(left, right_dilated, strict=True)
        if left_pixel and right_pixel
    ) / left_count
    right_covered = sum(
        1
        for left_pixel, right_pixel in zip(left_dilated, right, strict=True)
        if left_pixel and right_pixel
    ) / right_count
    coverage = (
        0.0
        if left_covered + right_covered == 0.0
        else 2.0 * left_covered * right_covered / (left_covered + right_covered)
    )
    horizontal = _profile_cosine(left, right, horizontal=True)
    vertical = _profile_cosine(left, right, horizontal=False)
    return max(0.0, min(1.0, 0.75 * coverage + 0.15 * horizontal + 0.10 * vertical))


def _render_candidate(
    label: str,
    *,
    family: str,
    italic: bool,
    stretch: int,
) -> QImage:
    image = QImage(256, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = QFont(family)
        font.setBold(True)
        font.setItalic(italic)
        font.setStretch(stretch)
        pixel_size = 30
        font.setPixelSize(pixel_size)
        metrics = QFontMetrics(font)
        while pixel_size > 10 and metrics.horizontalAdvance(label) > image.width() - 8:
            pixel_size -= 1
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(
            4,
            0,
            image.width() - 8,
            image.height(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )
    finally:
        painter.end()
    return image


def recognize_candidate_name(
    crop: QImage,
    candidates: tuple[str, ...],
    *,
    top_k: int = 3,
) -> tuple[NameCandidateMatch, ...]:
    """Rank only supplied names after isolating the bright game-text glyphs."""

    normalized = tuple(dict.fromkeys(name.strip() for name in candidates if name.strip()))
    if crop.isNull() or not normalized or top_k <= 0:
        return ()
    crop_mask = _normalize_glyph_mask(crop)
    if sum(crop_mask) < 12:
        return ()

    raw_scores: list[tuple[str, float]] = []
    for label in normalized:
        best = 0.0
        for family in _FONT_FAMILIES:
            for italic in (True, False):
                for stretch in _FONT_STRETCHES:
                    candidate_mask = _normalize_glyph_mask(
                        _render_candidate(
                            label,
                            family=family,
                            italic=italic,
                            stretch=stretch,
                        )
                    )
                    best = max(best, _glyph_similarity(crop_mask, candidate_mask))
        raw_scores.append((label, best))
    raw_scores.sort(key=lambda item: (-item[1], item[0].casefold(), item[0]))

    top_margin = (
        raw_scores[0][1] - raw_scores[1][1]
        if len(raw_scores) > 1
        else raw_scores[0][1]
    )
    ranked: list[NameCandidateMatch] = []
    for index, (label, raw_score) in enumerate(raw_scores[:top_k]):
        calibrated = min(0.96, max(0.0, raw_score * 1.03))
        if index == 0 and top_margin < 0.03:
            calibrated = min(calibrated, 0.79)
        ranked.append(NameCandidateMatch(label=label, score=calibrated, rank=index + 1))
    return tuple(ranked)
