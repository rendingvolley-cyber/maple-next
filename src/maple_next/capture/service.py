"""Capture orchestration service.

Owns: starting/stopping a VideoCaptureBackend, tracking the latest frame,
computing freshness against an injectable monotonic clock, and producing
sanitized CaptureStatus snapshots.

Does NOT own and must NEVER call: BattleApplication.start_turn_capture(),
confirm_turn_facts(), any repository write, advice invalidation, provider
sends, or turn increments. Those remain owned by the domain/application layer
(Lane 02 integration).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from maple_next.capture.contracts import (
    CAPTURE_DEVICE_ENV_VAR,
    DEFAULT_DEVICE_SELECTOR,
    DEFAULT_FRAME_FRESHNESS_MS,
    FRAME_SOURCE_UGREEN_DIRECT,
    CaptureErrorCode,
    CaptureStatus,
    CaptureStatusCode,
    FramePacket,
    VideoCaptureBackend,
    sanitized_capture_message,
)

try:  # pragma: no cover - only exercised when PySide6 is importable
    from PySide6.QtCore import QObject, Signal

    _QT_CORE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _QT_CORE_AVAILABLE = False

    class _QObjectFallback:
        """Fallback base class when PySide6 is not importable."""

    def _signal_fallback(*_args: object, **_kwargs: object) -> Any:
        return None

    QObject = _QObjectFallback  # type: ignore[assignment, misc]
    Signal = _signal_fallback  # type: ignore[assignment, misc]


def resolve_device_selector(explicit_selector: str | None = None) -> str:
    """Resolve the UGREEN device selector.

    Precedence: explicit constructor value > MAPLE_NEXT_CAPTURE_DEVICE env var
    > built-in default. The env var is optional and non-secret - discovery
    must work without it.
    """

    if explicit_selector:
        return explicit_selector
    from_env = os.environ.get(CAPTURE_DEVICE_ENV_VAR)
    if from_env:
        return from_env
    return DEFAULT_DEVICE_SELECTOR


class CaptureService(QObject):
    """Loosely coupled orchestrator around a VideoCaptureBackend.

    Qt signals (frame_ready, status_changed) are best-effort UI hooks; the
    service is fully usable without a Qt event loop via latest_status()/
    latest_frame() polling, which is what the test suite and the probe CLI use.
    """

    if _QT_CORE_AVAILABLE:
        frame_ready = Signal(object)
        status_changed = Signal(object)

    def __init__(
        self,
        backend: VideoCaptureBackend,
        *,
        selector: str | None = None,
        freshness_ms: int = DEFAULT_FRAME_FRESHNESS_MS,
        monotonic_clock: Callable[[], int] = time.monotonic_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if _QT_CORE_AVAILABLE:
            super().__init__()
        self._backend = backend
        self._selector = resolve_device_selector(selector)
        self._freshness_ms = freshness_ms
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._status = self._stopped_status()

    # -- public UI-facing interface -------------------------------------------------

    def start(self) -> CaptureStatus:
        if self._backend.is_running():
            return self.latest_status()
        self._set_status(self._build_status(CaptureStatusCode.STARTING))
        try:
            result = self._backend.start(self._selector, self._on_frame)
        except Exception:  # noqa: BLE001 - backend must never crash the app
            status = self._build_status(
                CaptureStatusCode.CAPTURE_ERROR,
                error_code=CaptureErrorCode.CAPTURE_FAILED,
            )
            self._set_status(status)
            return status

        if not result.device_found or not result.opened:
            code = (
                CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE
                if not result.device_found
                else CaptureErrorCode.CAPTURE_OPEN_FAILED
            )
            status_code = (
                CaptureStatusCode.DEVICE_UNAVAILABLE
                if not result.device_found
                else CaptureStatusCode.OPEN_FAILED
            )
            status = self._build_status(
                status_code,
                error_code=result.error_code or code,
                device_label=result.device_label,
            )
            self._set_status(status)
            return status

        status = self._build_status(
            CaptureStatusCode.FRAME_UNAVAILABLE,
            error_code=CaptureErrorCode.CAPTURE_FRAME_UNAVAILABLE,
            device_label=result.device_label,
        )
        self._set_status(status)
        return self.latest_status()

    def stop(self) -> CaptureStatus:
        with contextlib.suppress(Exception):  # stop() must be safe to call always
            self._backend.stop()
        status = self._stopped_status()
        self._set_status(status)
        return status

    def latest_status(self) -> CaptureStatus:
        if not self._backend.is_running():
            return self._status
        try:
            frame = self._backend.get_latest_frame()
        except Exception:  # noqa: BLE001
            status = self._build_status(
                CaptureStatusCode.CAPTURE_ERROR,
                error_code=CaptureErrorCode.CAPTURE_FAILED,
                device_label=self._status.device_label,
            )
            self._set_status(status)
            return status
        status = self._status_from_frame(frame)
        self._set_status(status)
        return status

    def latest_frame(self) -> FramePacket | None:
        try:
            return self._backend.get_latest_frame()
        except Exception:  # noqa: BLE001
            return None

    # -- internals --------------------------------------------------------------

    def _on_frame(self, frame: FramePacket) -> None:
        status = self._status_from_frame(frame)
        self._set_status(status)
        if _QT_CORE_AVAILABLE:
            self.frame_ready.emit(frame)

    def _status_from_frame(self, frame: FramePacket | None) -> CaptureStatus:
        if frame is None:
            return self._build_status(
                CaptureStatusCode.FRAME_UNAVAILABLE,
                error_code=CaptureErrorCode.CAPTURE_FRAME_UNAVAILABLE,
                device_label=self._status.device_label,
            )
        age_ms = self._age_ms(frame.captured_monotonic_ns)
        fresh = age_ms < self._freshness_ms
        if fresh:
            return CaptureStatus(
                status=CaptureStatusCode.AVAILABLE,
                available=True,
                manual_entry_allowed=True,
                source=FRAME_SOURCE_UGREEN_DIRECT,
                device_label=self._status.device_label,
                frame_id=frame.frame_id,
                captured_at_utc=frame.captured_at_utc,
                age_ms=age_ms,
                fresh=True,
                width=frame.width,
                height=frame.height,
                error_code=None,
                operator_message=None,
            )
        return CaptureStatus(
            status=CaptureStatusCode.FRAME_STALE,
            available=True,
            manual_entry_allowed=True,
            source=FRAME_SOURCE_UGREEN_DIRECT,
            device_label=self._status.device_label,
            frame_id=frame.frame_id,
            captured_at_utc=frame.captured_at_utc,
            age_ms=age_ms,
            fresh=False,
            width=frame.width,
            height=frame.height,
            error_code=CaptureErrorCode.CAPTURE_FRAME_STALE,
            operator_message=sanitized_capture_message(CaptureErrorCode.CAPTURE_FRAME_STALE),
        )

    def _age_ms(self, captured_monotonic_ns: int) -> int:
        now_ns = self._monotonic_clock()
        return max(0, (now_ns - captured_monotonic_ns) // 1_000_000)

    def _build_status(
        self,
        status_code: str,
        *,
        error_code: str | None = None,
        device_label: str | None = None,
    ) -> CaptureStatus:
        return CaptureStatus(
            status=status_code,
            available=status_code in {CaptureStatusCode.AVAILABLE, CaptureStatusCode.FRAME_STALE},
            manual_entry_allowed=True,
            source=FRAME_SOURCE_UGREEN_DIRECT,
            device_label=device_label,
            frame_id=None,
            captured_at_utc=None,
            age_ms=None,
            fresh=False,
            width=None,
            height=None,
            error_code=error_code,
            operator_message=sanitized_capture_message(error_code),
        )

    def _stopped_status(self) -> CaptureStatus:
        return CaptureStatus(
            status=CaptureStatusCode.STOPPED,
            available=False,
            manual_entry_allowed=True,
            source=FRAME_SOURCE_UGREEN_DIRECT,
            device_label=None,
            frame_id=None,
            captured_at_utc=None,
            age_ms=None,
            fresh=False,
            width=None,
            height=None,
            error_code=None,
            operator_message=None,
        )

    def _set_status(self, status: CaptureStatus) -> None:
        self._status = status
        if _QT_CORE_AVAILABLE:
            self.status_changed.emit(status)
