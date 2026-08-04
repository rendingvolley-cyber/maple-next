"""Deterministic, fail-safe camera format preference helpers.

The operator requested a 1280x720 capture input to reduce per-frame image
conversion and scaling cost, while retaining the previously verified ~30 fps
cadence. The policy therefore requests an exact 720p format whose declared
cadence is approximately 30 fps. It never upgrades to a 60 fps format merely
because the device or current/default format reports 60 fps.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

_CameraFormatT = TypeVar("_CameraFormatT")

PREFERRED_720P_FPS = 30.0
_FPS_TOLERANCE = 1.0


def _safe_rate(camera_format: object, method_name: str) -> float | None:
    try:
        value = float(getattr(camera_format, method_name)())
    except Exception:  # noqa: BLE001 - malformed driver metadata is ignored
        return None
    return value if value > 0 else None


def select_exact_720p_format(
    formats: Sequence[_CameraFormatT],
    *,
    preferred_fps: float = PREFERRED_720P_FPS,
) -> _CameraFormatT | None:
    """Select exact 1280x720 at approximately ``preferred_fps``.

    Only formats whose declared maximum cadence is within one frame per second
    of the requested cadence are eligible. This deliberately rejects 720p/60
    when 720p/30 is requested instead of silently increasing capture load.
    Original device order is the deterministic final tie-breaker.
    """

    candidates: list[tuple[float, int, _CameraFormatT]] = []
    for index, camera_format in enumerate(formats):
        try:
            resolution = camera_format.resolution()  # type: ignore[attr-defined]
            if resolution.width() != 1280 or resolution.height() != 720:
                continue
        except Exception:  # noqa: BLE001 - malformed driver format is ignored
            continue

        maximum = _safe_rate(camera_format, "maxFrameRate")
        comparison = maximum
        if comparison is None:
            comparison = _safe_rate(camera_format, "minFrameRate")
        if comparison is None:
            continue

        distance = abs(comparison - preferred_fps)
        if distance > _FPS_TOLERANCE:
            continue
        candidates.append((distance, index, camera_format))

    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def apply_preferred_720p_format(camera: object, device: object) -> bool:
    """Request exact 720p/~30 fps, otherwise retain auto-negotiation.

    Returns ``True`` only when a suitable 720p format exists and
    ``setCameraFormat`` succeeds. A 720p/60-only device is not accepted for this
    low-load policy. Missing driver methods, malformed metadata, enumeration
    failures, and set failures all fall back to Qt/driver auto-negotiation.
    """

    try:
        formats = device.videoFormats()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - driver enumeration failure is safe
        return False

    selected = select_exact_720p_format(formats)
    if selected is None:
        return False
    try:
        camera.setCameraFormat(selected)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - retain auto-negotiation on set failure
        return False
    return True
