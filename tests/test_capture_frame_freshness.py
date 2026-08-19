"""is_frame_newer_than: the freshness proof used by the NEW MATCH reacquire seam."""

from __future__ import annotations

from datetime import UTC, datetime

from maple_next.capture.contracts import FrameKind, FramePacket, is_frame_newer_than


def _frame(*, frame_id: str, monotonic_ns: int) -> FramePacket:
    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=monotonic_ns,
        width=1280,
        height=720,
        image=None,
        frame_kind=FrameKind.CANONICAL,
    )


def test_any_frame_is_newer_than_no_baseline() -> None:
    frame = _frame(frame_id="a", monotonic_ns=100)
    assert is_frame_newer_than(frame, None) is True


def test_distinct_id_and_later_timestamp_is_newer() -> None:
    baseline = _frame(frame_id="a", monotonic_ns=100)
    frame = _frame(frame_id="b", monotonic_ns=200)
    assert is_frame_newer_than(frame, baseline) is True


def test_identical_frame_is_not_newer() -> None:
    baseline = _frame(frame_id="a", monotonic_ns=100)
    same = _frame(frame_id="a", monotonic_ns=100)
    assert is_frame_newer_than(same, baseline) is False


def test_same_id_with_later_timestamp_is_not_newer() -> None:
    """Same frame_id means the same underlying capture regardless of clock noise."""

    baseline = _frame(frame_id="a", monotonic_ns=100)
    frame = _frame(frame_id="a", monotonic_ns=999)
    assert is_frame_newer_than(frame, baseline) is False


def test_distinct_id_with_earlier_or_equal_timestamp_is_not_newer() -> None:
    baseline = _frame(frame_id="a", monotonic_ns=100)
    earlier = _frame(frame_id="b", monotonic_ns=99)
    equal = _frame(frame_id="b", monotonic_ns=100)
    assert is_frame_newer_than(earlier, baseline) is False
    assert is_frame_newer_than(equal, baseline) is False
