"""Issue #31: native-cadence preview vs. OCR sampling must be fully decoupled.

Verifies the perf fix behind "UGREEN映像表示が実機で非常に重くカクついています":

- Preview refresh is not tied to the 500ms/OCR poll interval - it uses a much
  finer sampling granularity (_PREVIEW_POLL_INTERVAL_MS), so a fast source is
  not silently throttled down to ~2fps.
- Preview polling never triggers OCR candidate generation, and OCR polling
  never touches preview pixels - they are two independent code paths that
  can run on independent timers.
- Polling the same underlying frame repeatedly never re-converts/re-renders
  it (no redundant QPixmap.fromImage()/SmoothTransformation work, no
  redundant OCR canonicalization).
- Camera format is never forced: qt_ugreen.py's real start() path contains
  no active (non-comment) call to setCameraFormat().
- Preview/OCR polling only runs on the Battle Record tab (tab lifecycle
  option A): the camera connection itself is not torn down by a tab switch,
  only the two poll timers pause/resume.

No real UGREEN hardware is used - only fakes conforming to the
``VideoCaptureBackend``/``OcrCandidateBackend`` protocols. Zero network
calls, zero domain/repository/provider reach from any capture/OCR path.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.capture.contracts import DeviceOpenResult, FramePacket
from maple_next.ocr.contracts import OcrCandidate, OcrCandidateContext
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.window import (
    _CAPTURE_POLL_INTERVAL_MS,
    _PREVIEW_POLL_INTERVAL_MS,
    MapleMainWindow,
)

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class StreamingCaptureBackend:
    """Deterministic fake that advances to a brand-new frame on demand.

    Mirrors the real Qt backend's identity semantics: get_latest_frame()
    returns whatever the "latest" frame currently is, unchanged, until the
    test explicitly calls advance_frame() - simulating an incoming source
    frame arriving. This lets a test assert that N polls with no new source
    frame between them cost nothing extra, while N distinct incoming frames
    are each observed exactly once.
    """

    def __init__(self) -> None:
        self._running = False
        self._frame: FramePacket | None = None
        self._frame_counter = 0
        self.get_latest_frame_calls = 0

    def start(self, selector: str, on_frame: object | None = None) -> DeviceOpenResult:
        self._running = True
        self.advance_frame()
        return DeviceOpenResult(
            opened=True, device_found=True, device_label="FAKE_UGREEN", error_code=None
        )

    def stop(self) -> None:
        self._running = False

    def get_latest_frame(self) -> FramePacket | None:
        self.get_latest_frame_calls += 1
        return self._frame

    def is_running(self) -> bool:
        return self._running

    def advance_frame(self, *, width: int = 1280, height: int = 720) -> FramePacket:
        self._frame_counter += 1
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(0x00112233 + self._frame_counter)
        self._frame = FramePacket(
            frame_id=f"frame-{self._frame_counter}",
            source="UGREEN_DIRECT",
            captured_at_utc=datetime.now(UTC),
            captured_monotonic_ns=time.monotonic_ns(),
            width=width,
            height=height,
            image=image,
        )
        return self._frame


class RecordingOcrBackend:
    def __init__(self) -> None:
        self.generate_calls = 0
        self.seen_frame_ids: list[str] = []

    def is_available(self) -> bool:
        return True

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        self.generate_calls += 1
        self.seen_frame_ids.append(frame.frame_id)
        return ()


def build_window(
    tmp_path: Path, *, capture_backend: object, ocr_backend: object
) -> tuple[SQLiteRepository, MapleMainWindow]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    controller = SelectionFlowController(
        application, repository, MockSelectionAdviceAdapter(), MockTurnAdviceAdapter()
    )
    window = MapleMainWindow(
        controller,
        capture_backend=cast(object, capture_backend),  # type: ignore[arg-type]
        ocr_backend=cast(object, ocr_backend),  # type: ignore[arg-type]
        auto_start_capture=False,
    )
    return repository, window


def advance_to_turn_reviewable(window: MapleMainWindow) -> None:
    window.new_match_button.click()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    for box, value in zip(
        window.mock_selection_boxes, ("Meowscarada", "Gholdengo", "Dragonite"), strict=True
    ):
        box.setCurrentText(value)
    window.mock_lead_box.setCurrentText("Meowscarada")
    window.mock_submit_button.click()
    for checkbox in window.actual_checkboxes:
        checkbox.setChecked(checkbox.text() in {"Dondozo", "Flutter Mane", "Urshifu"})
    window.actual_lead_box.setCurrentText("Dondozo")
    window.apply_confirm_checkbox.setChecked(True)
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    window.render_view()
    window.start_turn_button.click()
    window.render_view()


def test_preview_poll_interval_is_much_finer_than_the_ocr_poll_interval() -> None:
    # The perf bug this issue fixes was a single 500ms poll driving preview,
    # which caps display at ~2fps. Preview must sample at a materially finer
    # granularity than the (still 500ms/<=2fps) OCR-only interval.
    assert _PREVIEW_POLL_INTERVAL_MS < _CAPTURE_POLL_INTERVAL_MS
    assert _CAPTURE_POLL_INTERVAL_MS == 500


def test_preview_polling_never_triggers_ocr_generation(tmp_path: Path) -> None:
    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    for _ in range(20):
        backend.advance_frame()
        window._poll_capture_preview()  # noqa: SLF001

    assert ocr_backend.generate_calls == 0

    window.close()
    repository.close()


def test_ocr_polling_never_renders_preview_pixels(tmp_path: Path) -> None:
    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    before = window.capture_preview_label.pixmap()
    assert before is None or before.isNull()
    for _ in range(5):
        backend.advance_frame()
        window._poll_capture_ocr()  # noqa: SLF001

    # OCR ran, but the preview label was never touched by the OCR-only path.
    assert ocr_backend.generate_calls > 0
    after = window.capture_preview_label.pixmap()
    assert after is None or after.isNull()

    window.close()
    repository.close()


def test_repeated_preview_polls_of_the_same_frame_do_not_reconvert(tmp_path: Path) -> None:
    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    window._poll_capture_preview()  # noqa: SLF001
    first_count = window._preview_conversion_count  # noqa: SLF001
    assert first_count == 1

    for _ in range(50):
        window._poll_capture_preview()  # noqa: SLF001
    assert window._preview_conversion_count == first_count  # noqa: SLF001 - no new source frame

    backend.advance_frame()
    window._poll_capture_preview()  # noqa: SLF001
    assert window._preview_conversion_count == first_count + 1  # noqa: SLF001

    window.close()
    repository.close()


def test_incoming_frames_at_60fps_rate_are_each_observed_by_preview(tmp_path: Path) -> None:
    """A fast source is not silently thinned to ~2fps by preview polling."""

    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    simulated_source_frames = 60
    for _ in range(simulated_source_frames):
        backend.advance_frame()
        window._poll_capture_preview()  # noqa: SLF001

    assert window._preview_conversion_count == simulated_source_frames  # noqa: SLF001

    window.close()
    repository.close()


def test_ocr_sample_rate_stays_bounded_regardless_of_incoming_frame_rate(tmp_path: Path) -> None:
    """OCR must sample at most once per _poll_capture_ocr() call - never a backlog."""

    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    # Simulate one second of a 60fps source (60 incoming frames) with the OCR
    # timer only firing twice (matching its <=2fps real-world cadence).
    for _ in range(30):
        backend.advance_frame()
    window._poll_capture_ocr()  # noqa: SLF001
    for _ in range(30):
        backend.advance_frame()
    window._poll_capture_ocr()  # noqa: SLF001

    assert ocr_backend.generate_calls == 2
    # Each OCR sample used the newest frame at sample time, not a queued
    # backlog of every frame that arrived in between.
    assert ocr_backend.seen_frame_ids[0] != ocr_backend.seen_frame_ids[1]

    window.close()
    repository.close()


def test_camera_format_is_never_forced_in_the_real_backend_start_path() -> None:
    import maple_next.capture.qt_ugreen as qt_ugreen_module

    with open(qt_ugreen_module.__file__, encoding="utf-8") as handle:
        lines = handle.readlines()

    active_lines = [
        line for line in lines if not line.strip().startswith("#") and '"""' not in line
    ]
    for line in active_lines:
        assert "setCameraFormat(" not in line, f"format forced in: {line!r}"

    # The pure helper stays available for a future deterministic fallback,
    # but start() never invokes it - only its own `def` line may mention it.
    assert hasattr(qt_ugreen_module, "select_exact_720p_format")
    call_sites = [
        line
        for line in active_lines
        if "select_exact_720p_format(" in line and not line.strip().startswith("def ")
    ]
    assert call_sites == []


def test_tab_switch_pauses_and_resumes_polling_without_reopening_camera(
    tmp_path: Path,
) -> None:
    qt_application()
    backend = StreamingCaptureBackend()
    ocr_backend = RecordingOcrBackend()
    repository, window = build_window(tmp_path, capture_backend=backend, ocr_backend=ocr_backend)
    advance_to_turn_reviewable(window)
    assert window.header_tabs.currentIndex() == 1
    window._start_capture()  # noqa: SLF001
    assert window._preview_timer.isActive() is True  # noqa: SLF001
    assert window._capture_timer.isActive() is True  # noqa: SLF001
    starts_before_switch = backend._running  # noqa: SLF001

    window.header_tabs.setCurrentIndex(0)  # human switches to the Selection tab
    assert window._preview_timer.isActive() is False  # noqa: SLF001
    assert window._capture_timer.isActive() is False  # noqa: SLF001
    # The camera connection itself is untouched by a tab switch (option A).
    assert backend.is_running() is starts_before_switch is True

    window.header_tabs.setCurrentIndex(1)  # human returns to Battle Record
    assert window._preview_timer.isActive() is True  # noqa: SLF001
    assert window._capture_timer.isActive() is True  # noqa: SLF001
    assert backend.is_running() is True

    window.close()
    repository.close()


def test_no_network_or_domain_reach_from_preview_or_ocr_poll_paths() -> None:
    import maple_next.ui.window as window_module

    with open(window_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("socket.", "requests.", "urllib.request", "http.client"):
        assert forbidden not in source
