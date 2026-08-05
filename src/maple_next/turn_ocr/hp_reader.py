"""Conservative HP-bar ratio and canonical bucket estimation."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QImage

from maple_next.domain.enums import HpBucket


@dataclass(frozen=True, slots=True)
class HpEstimate:
    bucket: HpBucket
    ratio: float | None
    confidence: float
    detected: bool


def hp_bucket_from_ratio(ratio: float) -> HpBucket:
    bounded = max(0.0, min(1.0, ratio))
    percent = bounded * 100.0
    if percent <= 0.0:
        return HpBucket.ZERO
    if percent <= 10.0:
        return HpBucket.ONE_TO_TEN
    if percent <= 20.0:
        return HpBucket.ELEVEN_TO_TWENTY
    if percent <= 30.0:
        return HpBucket.TWENTY_ONE_TO_THIRTY
    if percent <= 40.0:
        return HpBucket.THIRTY_ONE_TO_FORTY
    if percent <= 50.0:
        return HpBucket.FORTY_ONE_TO_FIFTY
    if percent <= 60.0:
        return HpBucket.FIFTY_ONE_TO_SIXTY
    if percent <= 70.0:
        return HpBucket.SIXTY_ONE_TO_SEVENTY
    if percent <= 80.0:
        return HpBucket.SEVENTY_ONE_TO_EIGHTY
    if percent <= 90.0:
        return HpBucket.EIGHTY_ONE_TO_NINETY
    if percent < 99.5:
        return HpBucket.NINETY_ONE_TO_NINETY_NINE
    return HpBucket.FULL


def _is_hp_fill_pixel(red: int, green: int, blue: int) -> bool:
    maximum = max(red, green, blue)
    minimum = min(red, green, blue)
    if maximum < 58 or maximum - minimum < 22:
        return False
    # Exclude the saturated blue HUD/background family before testing the
    # green/yellow/red HP colors.
    if blue > red + 18 and blue > green + 12:
        return False
    green_fill = green >= red * 0.82 and green >= blue + 14
    yellow_fill = red >= 88 and green >= 68 and blue <= min(red, green) - 18
    red_fill = red >= 88 and red >= green + 18 and red >= blue + 24
    return green_fill or yellow_fill or red_fill


def read_hp_bar(crop: QImage) -> HpEstimate:
    if crop.isNull() or crop.width() < 8 or crop.height() < 4:
        return HpEstimate(HpBucket.UNKNOWN, None, 0.0, False)
    image = crop.convertToFormat(QImage.Format.Format_RGB32)
    width = image.width()
    height = image.height()
    minimum_pixels_per_column = max(1, height // 7)
    active_columns: list[int] = []
    colored_pixels = 0
    for x in range(width):
        count = 0
        for y in range(height):
            color = image.pixelColor(x, y)
            if _is_hp_fill_pixel(color.red(), color.green(), color.blue()):
                count += 1
        colored_pixels += count
        if count >= minimum_pixels_per_column:
            active_columns.append(x)

    if not active_columns:
        # An empty-looking ROI is ambiguous between zero HP, an animation, and
        # an incorrect ROI. Never auto-assert a faint from absence alone.
        return HpEstimate(HpBucket.UNKNOWN, None, 0.18, False)

    start = min(active_columns)
    end = max(active_columns)
    if start > width // 3:
        return HpEstimate(HpBucket.UNKNOWN, None, 0.24, False)
    span = end - start + 1
    continuity = len(active_columns) / span
    ratio = span / max(1, width - start)
    density = colored_pixels / max(1, width * height)
    confidence = min(
        0.96,
        0.42 + 0.34 * continuity + 0.20 * min(1.0, density * 4.0),
    )
    if continuity < 0.58 or density < 0.015:
        return HpEstimate(HpBucket.UNKNOWN, ratio, min(confidence, 0.45), False)
    return HpEstimate(hp_bucket_from_ratio(ratio), ratio, confidence, True)
