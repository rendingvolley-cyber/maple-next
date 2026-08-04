"""Small, deterministic capture telemetry helpers.

The preview hot path must not format or repaint text. ``SourceFpsSampler`` is
called only by the low-frequency telemetry path and reports observed incoming
source cadence; it never fixes, caps, or interpolates video FPS.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SourceFpsSampler:
    """Measure incoming-frame cadence over a monotonic time window."""

    minimum_window_ns: int = 1_000_000_000
    _baseline_count: int | None = None
    _baseline_ns: int | None = None

    def reset(self) -> None:
        self._baseline_count = None
        self._baseline_ns = None

    def sample(self, *, frame_count: int, now_ns: int) -> float | None:
        """Return frames/second once a full window has elapsed.

        Counter resets and non-increasing clocks fail closed by starting a new
        window and returning ``None``. The baseline advances only after a
        completed sample window, so early timer wakeups do not distort the
        measurement.
        """

        if frame_count < 0 or now_ns < 0:
            self.reset()
            return None
        if self._baseline_count is None or self._baseline_ns is None:
            self._baseline_count = frame_count
            self._baseline_ns = now_ns
            return None
        if frame_count < self._baseline_count or now_ns <= self._baseline_ns:
            self._baseline_count = frame_count
            self._baseline_ns = now_ns
            return None

        elapsed_ns = now_ns - self._baseline_ns
        if elapsed_ns < self.minimum_window_ns:
            return None

        delta_frames = frame_count - self._baseline_count
        self._baseline_count = frame_count
        self._baseline_ns = now_ns
        return delta_frames * 1_000_000_000 / elapsed_ns
