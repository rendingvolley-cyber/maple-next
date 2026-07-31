"""Issue #31 Lane B: capture contract, freshness, sanitization, lifecycle."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from maple_next.capture.contracts import (
    DEFAULT_FRAME_FRESHNESS_MS,
    CaptureErrorCode,
    CaptureStatusCode,
    DeviceOpenResult,
    FramePacket,
)
from maple_next.capture.qt_ugreen import select_ugreen_device
from maple_next.capture.service import CaptureService


class FakeVideoCaptureBackend:
    """Test double implementing VideoCaptureBackend without any Qt hardware."""

    def __init__(
        self,
        *,
        device_found: bool = True,
        open_should_fail: bool = False,
        device_label: str = "UGREEN Capture (fake)",
    ) -> None:
        self.device_found = device_found
        self.open_should_fail = open_should_fail
        self.device_label = device_label
        self._running = False
        self._frame: FramePacket | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self._on_frame: Callable[[FramePacket], None] | None = None

    def start(
        self, selector: str, on_frame: Callable[[FramePacket], None] | None = None
    ) -> DeviceOpenResult:
        self.start_calls += 1
        self._on_frame = on_frame
        if not self.device_found:
            return DeviceOpenResult(
                opened=False,
                device_found=False,
                device_label=None,
                error_code=CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE,
            )
        if self.open_should_fail:
            return DeviceOpenResult(
                opened=False,
                device_found=True,
                device_label=None,
                error_code=CaptureErrorCode.CAPTURE_OPEN_FAILED,
            )
        self._running = True
        return DeviceOpenResult(
            opened=True,
            device_found=True,
            device_label=self.device_label,
            error_code=None,
        )

    def stop(self) -> None:
        self.stop_calls += 1
        self._running = False
        self._frame = None

    def get_latest_frame(self) -> FramePacket | None:
        return self._frame

    def is_running(self) -> bool:
        return self._running

    def push_frame(self, *, monotonic_ns: int, width: int = 1280, height: int = 720) -> None:
        frame = FramePacket(
            frame_id=str(uuid.uuid4()),
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=monotonic_ns,
            width=width,
            height=height,
            image=None,
        )
        self._frame = frame
        if self._running and self._on_frame is not None:
            self._on_frame(frame)


class UnavailableVideoCaptureBackend:
    """Always reports device unavailable, never raises."""

    def start(
        self, selector: str, on_frame: Callable[[FramePacket], None] | None = None
    ) -> DeviceOpenResult:
        return DeviceOpenResult(
            opened=False,
            device_found=False,
            device_label=None,
            error_code=CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE,
        )

    def stop(self) -> None:
        pass

    def get_latest_frame(self) -> FramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


class RaisingOpenBackend:
    """Simulates a driver raising a raw, sensitive exception on open."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def start(
        self, selector: str, on_frame: Callable[[FramePacket], None] | None = None
    ) -> DeviceOpenResult:
        raise RuntimeError(r"DirectShow device \\?\usb#vid_1234 secret-backend-detail")

    def stop(self) -> None:
        self.stop_calls += 1

    def get_latest_frame(self) -> FramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


def test_device_selection_is_pure_and_substring_based() -> None:
    assert select_ugreen_device(["Integrated Webcam", "UGREEN Capture 4K"], "UGREEN") == 1
    assert select_ugreen_device(["Integrated Webcam"], "UGREEN") is None
    assert select_ugreen_device([], "UGREEN") is None
    # lower-case device description still matches (sanitized/case-insensitive).
    assert select_ugreen_device(["ugreen capture"], "UGREEN") == 0


def test_capture_available_happy_path() -> None:
    backend = FakeVideoCaptureBackend()
    clock = {"now": 1_000_000_000}
    service = CaptureService(backend, monotonic_clock=lambda: clock["now"])
    started = service.start()
    assert started.status == CaptureStatusCode.FRAME_UNAVAILABLE
    assert started.manual_entry_allowed is True

    backend.push_frame(monotonic_ns=clock["now"])
    status = service.latest_status()
    assert status.status == CaptureStatusCode.AVAILABLE
    assert status.fresh is True
    assert status.source == "UGREEN_DIRECT"
    assert status.frame_id is not None
    assert status.width == 1280
    assert status.height == 720
    assert status.manual_entry_allowed is True


def test_device_list_empty_reports_device_unavailable_without_exception() -> None:
    backend = FakeVideoCaptureBackend(device_found=False)
    service = CaptureService(backend)
    status = service.start()
    assert status.status == CaptureStatusCode.DEVICE_UNAVAILABLE
    assert status.manual_entry_allowed is True
    assert service.latest_frame() is None


def test_built_in_camera_only_present_never_auto_falls_back() -> None:
    # A backend that only "sees" a built-in webcam must report the device it
    # was asked for (UGREEN) as not found - never silently substitute it.
    backend = FakeVideoCaptureBackend(device_found=False)
    service = CaptureService(backend, selector="UGREEN")
    status = service.start()
    assert status.status == CaptureStatusCode.DEVICE_UNAVAILABLE
    assert status.error_code == CaptureErrorCode.CAPTURE_DEVICE_UNAVAILABLE


def test_backend_open_raises_is_sanitized_and_manual_continues() -> None:
    backend = RaisingOpenBackend()
    service = CaptureService(backend)
    status = service.start()
    assert status.status == CaptureStatusCode.CAPTURE_ERROR
    assert status.manual_entry_allowed is True

    serialized = str(status.as_dict())
    blob = repr(status) + serialized + str(status.operator_message) + str(status.error_code)
    for forbidden in (r"vid_1234", "secret-backend-detail", r"\\?\usb", "DirectShow"):
        assert forbidden not in blob


def test_camera_started_but_no_frame_reports_frame_unavailable() -> None:
    backend = FakeVideoCaptureBackend()
    service = CaptureService(backend)
    status = service.start()
    assert status.status == CaptureStatusCode.FRAME_UNAVAILABLE
    assert status.manual_entry_allowed is True
    assert service.latest_frame() is None


def test_freshness_boundary_with_injected_clock() -> None:
    backend = FakeVideoCaptureBackend()
    clock = {"now": 0}
    service = CaptureService(
        backend,
        freshness_ms=DEFAULT_FRAME_FRESHNESS_MS,
        monotonic_clock=lambda: clock["now"],
    )
    service.start()
    backend.push_frame(monotonic_ns=0)

    # age < threshold -> fresh
    clock["now"] = (DEFAULT_FRAME_FRESHNESS_MS - 1) * 1_000_000
    status = service.latest_status()
    assert status.fresh is True
    assert status.status == CaptureStatusCode.AVAILABLE

    # age == threshold -> stale
    clock["now"] = DEFAULT_FRAME_FRESHNESS_MS * 1_000_000
    status = service.latest_status()
    assert status.fresh is False
    assert status.status == CaptureStatusCode.FRAME_STALE

    # age > threshold -> stale
    clock["now"] = (DEFAULT_FRAME_FRESHNESS_MS + 500) * 1_000_000
    status = service.latest_status()
    assert status.fresh is False
    assert status.status == CaptureStatusCode.FRAME_STALE


def test_no_frame_at_all_is_frame_unavailable() -> None:
    backend = FakeVideoCaptureBackend()
    service = CaptureService(backend)
    service.start()
    status = service.latest_status()
    assert status.status == CaptureStatusCode.FRAME_UNAVAILABLE
    assert status.fresh is False


def test_lifecycle_start_twice_does_not_spin_two_workers() -> None:
    backend = FakeVideoCaptureBackend()
    service = CaptureService(backend)
    service.start()
    service.start()
    assert backend.start_calls == 1


def test_lifecycle_stop_twice_raises_nothing() -> None:
    backend = FakeVideoCaptureBackend()
    service = CaptureService(backend)
    service.start()
    service.stop()
    service.stop()  # must not raise
    assert backend.stop_calls == 2


def test_callbacks_after_close_are_ignored() -> None:
    backend = FakeVideoCaptureBackend()
    service = CaptureService(backend)
    service.start()
    service.stop()
    # Pushing a frame after stop() must not resurrect AVAILABLE state, since
    # the service treats a non-running backend's cached status as terminal.
    backend.push_frame(monotonic_ns=time.monotonic_ns())
    status = service.latest_status()
    assert status.status == CaptureStatusCode.STOPPED


def test_manual_entry_allowed_is_always_true_regardless_of_status() -> None:
    for backend in (
        FakeVideoCaptureBackend(),
        FakeVideoCaptureBackend(device_found=False),
        FakeVideoCaptureBackend(open_should_fail=True),
        UnavailableVideoCaptureBackend(),
        RaisingOpenBackend(),
    ):
        service = CaptureService(backend)
        status = service.start()
        assert status.manual_entry_allowed is True
        service.stop()


def test_no_network_or_automation_calls_exist_in_capture_module() -> None:
    import maple_next.capture.service as capture_service_module

    with open(capture_service_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("socket.", "requests.", "urllib.request", "http.client", "MOVE", "SWITCH"):
        assert forbidden not in source
