"""Candidate-constrained active-name recognition without unrestricted text output.

This initial implementation renders each already-confirmed candidate name into
several Windows/Japanese font variants and compares polarity-independent edge
signatures. It deliberately cannot invent a Pokemon name outside the supplied
candidate pool. Repository-owned labeled active-name reference images can be
added later without changing the surrounding Turn snapshot contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

_SIGNATURE_WIDTH: Final[int] = 160
_SIGNATURE_HEIGHT: Final[int] = 32
_FONT_FAMILIES: Final[tuple[str, ...]] = (
    "Yu Gothic UI",
    "Meiryo UI",
    "Meiryo",
    "MS Gothic",
    "Sans Serif",
)


@dataclass(frozen=True, slots=True)
class NameCandidateMatch:
    label: str
    score: float
    rank: int


def _edge_signature(image: QImage) -> tuple[int, ...]:
    grayscale = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        _SIGNATURE_WIDTH,
        _SIGNATURE_HEIGHT,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    values = [
        grayscale.pixelColor(x, y).red()
        for y in range(_SIGNATURE_HEIGHT)
        for x in range(_SIGNATURE_WIDTH)
    ]
    edges: list[int] = []
    for y in range(_SIGNATURE_HEIGHT):
        for x in range(_SIGNATURE_WIDTH):
            index = y * _SIGNATURE_WIDTH + x
            current = values[index]
            right = values[index + 1] if x + 1 < _SIGNATURE_WIDTH else current
            down = (
                values[index + _SIGNATURE_WIDTH]
                if y + 1 < _SIGNATURE_HEIGHT
                else current
            )
            edges.append(1 if abs(current - right) + abs(current - down) >= 42 else 0)
    return tuple(edges)


def _signature_similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    left_count = sum(left)
    right_count = sum(right)
    if left_count < 6 or right_count < 6:
        return 0.0
    intersection = sum(1 for a, b in zip(left, right, strict=True) if a and b)
    union = sum(1 for a, b in zip(left, right, strict=True) if a or b)
    jaccard = intersection / union if union else 0.0
    density_score = 1.0 - abs(left_count - right_count) / max(left_count, right_count)
    return max(0.0, min(1.0, 0.82 * jaccard + 0.18 * density_score))


def _render_candidate(label: str, *, family: str, pixel_size: int) -> QImage:
    image = QImage(
        _SIGNATURE_WIDTH,
        _SIGNATURE_HEIGHT,
        QImage.Format.Format_RGB32,
    )
    image.fill(QColor("black"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = QFont(family)
        font.setBold(True)
        font.setPixelSize(pixel_size)
        metrics = QFontMetrics(font)
        while pixel_size > 8 and metrics.horizontalAdvance(label) > _SIGNATURE_WIDTH - 4:
            pixel_size -= 1
            font.setPixelSize(pixel_size)
            metrics = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(
            2,
            0,
            _SIGNATURE_WIDTH - 4,
            _SIGNATURE_HEIGHT,
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
    """Rank only the supplied candidate names against one frozen name ROI."""

    normalized = tuple(dict.fromkeys(name.strip() for name in candidates if name.strip()))
    if crop.isNull() or not normalized or top_k <= 0:
        return ()
    crop_signature = _edge_signature(crop)
    scored: list[tuple[str, float]] = []
    for label in normalized:
        best = 0.0
        for family in _FONT_FAMILIES:
            for size in (12, 14, 16, 18, 20):
                candidate_signature = _edge_signature(
                    _render_candidate(label, family=family, pixel_size=size)
                )
                best = max(best, _signature_similarity(crop_signature, candidate_signature))
        # The recognizer is deliberately conservative. A near-perfect template
        # match may approach 0.95, but weak visual overlap must remain below the
        # 0.50 display threshold rather than becoming an invented certainty.
        calibrated = min(0.95, best * 1.18)
        scored.append((label, calibrated))
    scored.sort(key=lambda item: (-item[1], item[0].casefold(), item[0]))
    return tuple(
        NameCandidateMatch(label=label, score=score, rank=index + 1)
        for index, (label, score) in enumerate(scored[:top_k])
    )
