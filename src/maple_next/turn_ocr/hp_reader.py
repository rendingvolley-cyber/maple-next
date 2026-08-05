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
    if blue > red + 18 and blue > green + 12:
        return False
    green_fill = green >= red * 0.82 and green >= blue + 14
    yellow_fill = red >= 88 and green >= 68 and blue <= min(red, green) - 18
    red_fill = red >= 88 and red >= green + 18 and red >= blue + 24
    return green_fill or yellow_fill or red_fill


def _longest_fill_run(image: QImage, y: int) -> tuple[int, int, int] | None:
    best: tuple[int, int, int] | None = None
    start: int | None = None
    for x in range(image.width() + 1):
        filled = False
        if x < image.width():
            color = image.pixelColor(x, y)
            filled = _is_hp_fill_pixel(color.red(), color.green(), color.blue())
        if filled and start is None:
            start = x
        if not filled and start is not None:
            end = x - 1
            run = (start, end, end - start + 1)
            if best is None or run[2] > best[2]:
                best = run
            start = None
    return best


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def read_hp_bar(crop: QImage) -> HpEstimate:
    if crop.isNull() or crop.width() < 8 or crop.height() < 4:
        return HpEstimate(HpBucket.UNKNOWN, None, 0.0, False)
    image = crop.convertToFormat(QImage.Format.Format_RGB32)
    width = image.width()
    height = image.height()
    minimum_run = max(3, width // 10)
    row_runs: list[tuple[int, int, int]] = []
    colored_pixels = 0
    for y in range(height):
        run = _longest_fill_run(image, y)
        if run is not None and run[2] >= minimum_run and run[0] <= width // 3:
            row_runs.append(run)
        for x in range(width):
            color = image.pixelColor(x, y)
            if _is_hp_fill_pixel(color.red(), color.green(), color.blue()):
                colored_pixels += 1

    if not row_runs:
        # Absence is ambiguous between zero HP, an animation, and a bad ROI.
        # Never infer faint/0 from an empty-looking crop.
        return HpEstimate(HpBucket.UNKNOWN, None, 0.18, False)

    full_width_rows = [
        run
        for run in row_runs
        if run[0] <= max(2, width // 12)
        and run[1] >= width - 3
        and run[2] >= int(width * 0.78)
    ]
    required_full_rows = max(3, height // 6)
    if len(full_width_rows) >= required_full_rows:
        density = colored_pixels / max(1, width * height)
        confidence = min(0.98, 0.86 + min(0.12, density * 0.20))
        return HpEstimate(HpBucket.FULL, 1.0, confidence, True)

    # A thin decorative border can span the whole track even when the actual HP
    # fill is partial. If there are too few such rows to prove FULL, remove them
    # before estimating the continuous interior fill.
    usable_rows = [run for run in row_runs if run not in full_width_rows] or row_runs
    maximum_span = max(run[2] for run in usable_rows)
    strong_rows = [run for run in usable_rows if run[2] >= int(maximum_span * 0.78)]
    start = _median([run[0] for run in strong_rows])
    end = _median([run[1] for run in strong_rows])
    if start > width // 3 or end < start:
        return HpEstimate(HpBucket.UNKNOWN, None, 0.24, False)

    span = end - start + 1
    ratio = span / max(1, width - start)
    density = colored_pixels / max(1, width * height)
    row_support = len(strong_rows) / max(1, height)
    confidence = min(
        0.96,
        0.50 + 0.24 * min(1.0, row_support * 3.0) + 0.18 * min(1.0, density * 4.0),
    )
    if len(strong_rows) < 2 or density < 0.015:
        return HpEstimate(HpBucket.UNKNOWN, ratio, min(confidence, 0.45), False)
    return HpEstimate(hp_bucket_from_ratio(ratio), ratio, confidence, True)
