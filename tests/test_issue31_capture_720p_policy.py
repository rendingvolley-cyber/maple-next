"""Focused coverage for the operator-selected low-load 720p/30 capture policy."""

from __future__ import annotations

from maple_next.capture.format_policy import (
    PREFERRED_720P_FPS,
    apply_preferred_720p_format,
    select_exact_720p_format,
)


class FakeResolution:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class FakeFormat:
    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        minimum_fps: float,
        maximum_fps: float,
    ) -> None:
        self.name = name
        self._resolution = FakeResolution(width, height)
        self._minimum_fps = minimum_fps
        self._maximum_fps = maximum_fps

    def resolution(self) -> FakeResolution:
        return self._resolution

    def minFrameRate(self) -> float:  # noqa: N802 - mirrors Qt API
        return self._minimum_fps

    def maxFrameRate(self) -> float:  # noqa: N802 - mirrors Qt API
        return self._maximum_fps


class BrokenFormat:
    def resolution(self) -> FakeResolution:
        raise RuntimeError("broken driver metadata")


class FakeDevice:
    def __init__(self, formats: list[object], *, raises: bool = False) -> None:
        self._formats = formats
        self._raises = raises

    def videoFormats(self) -> list[object]:  # noqa: N802 - mirrors Qt API
        if self._raises:
            raise RuntimeError("format enumeration failed")
        return self._formats


class FakeCamera:
    def __init__(self, *, set_raises: bool = False) -> None:
        self._set_raises = set_raises
        self.set_calls: list[object] = []

    def setCameraFormat(self, selected: object) -> None:  # noqa: N802 - mirrors Qt API
        if self._set_raises:
            raise RuntimeError("driver rejected format")
        self.set_calls.append(selected)


def test_720p_policy_selects_30_instead_of_60() -> None:
    format_720p_60 = FakeFormat("720p-60", 1280, 720, 60.0, 60.0)
    format_720p_30 = FakeFormat("720p-30", 1280, 720, 30.0, 30.0)

    selected = select_exact_720p_format([format_720p_60, format_720p_30])

    assert PREFERRED_720P_FPS == 30.0
    assert selected is format_720p_30


def test_720p_policy_accepts_ntsc_like_2997() -> None:
    format_720p_2997 = FakeFormat("720p-29.97", 1280, 720, 29.97, 29.97)

    assert select_exact_720p_format([format_720p_2997]) is format_720p_2997


def test_720p_policy_rejects_60_only_instead_of_increasing_load() -> None:
    format_720p_60 = FakeFormat("720p-60", 1280, 720, 60.0, 60.0)

    assert select_exact_720p_format([format_720p_60]) is None


def test_720p_policy_rejects_unrelated_25_and_60_candidates() -> None:
    format_720p_25 = FakeFormat("720p-25", 1280, 720, 25.0, 25.0)
    format_720p_60 = FakeFormat("720p-60", 1280, 720, 60.0, 60.0)

    assert select_exact_720p_format([format_720p_25, format_720p_60]) is None


def test_720p_policy_ignores_non_720p_and_malformed_formats() -> None:
    other = FakeFormat("1440p", 2560, 1440, 30.0, 30.0)
    chosen = FakeFormat("720p", 1280, 720, 30.0, 30.0)

    assert select_exact_720p_format([BrokenFormat(), other, chosen]) is chosen


def test_apply_720p_requests_exact_30_format_once() -> None:
    chosen = FakeFormat("720p-30", 1280, 720, 30.0, 30.0)
    camera = FakeCamera()
    device = FakeDevice([FakeFormat("720p-60", 1280, 720, 60.0, 60.0), chosen])

    assert apply_preferred_720p_format(camera, device) is True
    assert camera.set_calls == [chosen]


def test_apply_720p_falls_back_when_only_60fps_exists() -> None:
    camera = FakeCamera()
    device = FakeDevice([FakeFormat("720p-60", 1280, 720, 60.0, 60.0)])

    assert apply_preferred_720p_format(camera, device) is False
    assert camera.set_calls == []


def test_apply_720p_falls_back_safely_when_no_exact_format_exists() -> None:
    camera = FakeCamera()
    device = FakeDevice([FakeFormat("1080p", 1920, 1080, 30.0, 30.0)])

    assert apply_preferred_720p_format(camera, device) is False
    assert camera.set_calls == []


def test_apply_720p_falls_back_safely_on_driver_failures() -> None:
    chosen = FakeFormat("720p", 1280, 720, 30.0, 30.0)

    enumeration_camera = FakeCamera()
    assert apply_preferred_720p_format(
        enumeration_camera,
        FakeDevice([chosen], raises=True),
    ) is False
    assert enumeration_camera.set_calls == []

    rejected_camera = FakeCamera(set_raises=True)
    assert apply_preferred_720p_format(rejected_camera, FakeDevice([chosen])) is False
    assert rejected_camera.set_calls == []
