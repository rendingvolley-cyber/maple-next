"""Deterministic, fail-safe camera format preference helpers.

The operator requested a 1280x720 capture input to reduce per-frame image
conversion and scaling cost. The policy does not target a particular FPS: when
multiple 720p formats exist, it prefers the frame-rate range closest to the
camera's current/default cadence and otherwise keeps device order stable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

_CameraFormatT = TypeVar("_CameraFormatT")


def _safe_rate(camera_format: object, method_name: str) -> float | None:
    try:
        value = float(getattr(camera_format, method_name)())
    except Exception:  # noqa: BLE001 - malformed driver metadata is ignored
        return None
    return value if value > 0 else None


def select_exact_720p_format(
    formats: Sequence[_CameraFormatT],
    *,
    preferred_fps: float | None = None,
) -> _CameraFormatT | None:
    """Select an exact 1280x720 format without maximizing or fixing FPS.

    With no usable cadence hint, the first valid 720p format in device order is
    returned. With a hint, a format whose declared range contains that cadence
    is preferred, then the smallest distance to its declared maximum, with
    original device order as the deterministic final tie-breaker.
    """

    candidates: list[tuple[int, _CameraFormatT, float | None, float | None]] = []
    for index, camera_format in enumerate(formats):
        try:
            resolution = camera_format.resolution()  # type: ignore[attr-defined]
            if resolution.width() != 1280 or resolution.height() != 720:
                continue
        except Exception:  # noqa: BLE001 - malformed driver format is ignored
            continue
        candidates.append(
            (
                index,
                camera_format,
                _safe_rate(camera_format, "minFrameRate"),
                _safe_rate(camera_format, "maxFrameRate"),
            )
        )

    if not candidates:
        return None
    if preferred_fps is None or preferred_fps <= 0:
        return candidates[0][1]

    def score(
        candidate: tuple[int, _CameraFormatT, float | None, float | None],
    ) -> tuple[int, float, int]:
        index, _camera_format, minimum, maximum = candidate
        contains = (
            minimum is not None
            and maximum is not None
            and minimum <= preferred_fps <= maximum
        )
        comparison = maximum if maximum is not None else minimum
        distance = abs(comparison - preferred_fps) if comparison is not None else float("inf")
        return (0 if contains else 1, distance, index)

    return min(candidates, key=score)[1]


def apply_preferred_720p_format(camera: object, device: object) -> bool:
    """Request exact 720p while preserving the default cadence when possible.

    Returns ``True`` only when ``setCameraFormat`` succeeds. Any missing driver
    method, malformed format metadata, or set failure falls back to Qt/driver
    auto-negotiation without raising.
    """

    preferred_fps: float | None = None
    try:
        current_format = camera.cameraFormat()  # type: ignore[attr-defined]
        preferred_fps = _safe_rate(current_format, "maxFrameRate")
    except Exception:  # noqa: BLE001 - absence of a default cadence is safe
        preferred_fps = None

    try:
        formats = device.videoFormats()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - driver enumeration failure is safe
        return False

    selected = select_exact_720p_format(formats, preferred_fps=preferred_fps)
    if selected is None:
        return False
    try:
        camera.setCameraFormat(selected)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - retain auto-negotiation on set failure
        return False
    return True
