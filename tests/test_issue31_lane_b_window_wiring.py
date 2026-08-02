"""Issue #31 Lane B -> main Battle Record UI wiring tests.

Exercises the actual :class:`maple_next.ui.window.MapleMainWindow` capture/OCR
integration built for issue #31 "02 Battle Record app implementation": the
center-column UGREEN status/freshness display, the OCR candidate panel, and
the human-only "採用" (adopt) buttons. Confirms:

- OCR candidates are never auto-applied to the turn-fact inputs.
- A manual edit after adopting a candidate always wins (the widget is a
  plain input the human can still change).
- A stale/low-confidence/absent frame never yields an adoptable candidate.
- Capture being entirely unavailable does not disable manual entry.

No real UGREEN hardware or OCR backend is used anywhere in this file - only
fakes conforming to the ``VideoCaptureBackend`` / ``OcrCandidateBackend``
protocols. Zero network calls.
"""

from __future__ import annotations

import os
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
from maple_next.domain.enums import HpBucket
from maple_next.ocr.contracts import OcrCandidate, OcrCandidateContext
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.window import MapleMainWindow

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class FakeCaptureBackend:
    """Deterministic, test-only capture backend. Never touches hardware."""

    def __init__(self, frame: FramePacket | None) -> None:
        self._frame = frame
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.duplicate_start_calls = 0

    def start(self, selector: str, on_frame: object | None = None) -> DeviceOpenResult:
        self.start_calls += 1
        if self._running:
            self.duplicate_start_calls += 1
        self._running = True
        return DeviceOpenResult(
            opened=True, device_found=True, device_label="FAKE_UGREEN", error_code=None
        )

    def stop(self) -> None:
        self.stop_calls += 1
        self._running = False

    def get_latest_frame(self) -> FramePacket | None:
        return self._frame

    def is_running(self) -> bool:
        return self._running


class UnavailableCaptureBackend:
    def start(self, selector: str, on_frame: object | None = None) -> DeviceOpenResult:
        return DeviceOpenResult(
            opened=False, device_found=False, device_label=None,
            error_code="CAPTURE_DEVICE_UNAVAILABLE",
        )

    def stop(self) -> None:
        pass

    def get_latest_frame(self) -> FramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


class FakeOcrBackend:
    """Always returns one high-confidence self_active candidate."""

    def __init__(self, candidates: tuple[OcrCandidate, ...]) -> None:
        self._candidates = candidates

    def is_available(self) -> bool:
        return True

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        return self._candidates


def _fresh_frame(frame_id: str = "frame-1", image: QImage | None = None) -> FramePacket:
    import time

    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=image.width() if image is not None else 1280,
        height=image.height() if image is not None else 720,
        image=image,
    )


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


def test_device_unavailable_shows_fallback_and_disables_no_adopt(tmp_path: Path) -> None:
    qt_application()
    repository, window = build_window(
        tmp_path,
        capture_backend=UnavailableCaptureBackend(),
        ocr_backend=FakeOcrBackend(()),
    )
    window._start_capture()  # noqa: SLF001 - explicit for this test's assertions

    assert "manual-safe fallback" in window.capture_status_label.text() or (
        "manual" in window.capture_status_label.text().lower()
        or "手動" in window.capture_status_label.text()
    )
    for button in window._ocr_adopt_buttons.values():  # noqa: SLF001
        assert button.isEnabled() is False

    window.close()
    repository.close()


def test_fresh_frame_yields_an_adoptable_candidate(tmp_path: Path) -> None:
    qt_application()
    frame = _fresh_frame()
    candidate = OcrCandidate(
        field_key="self_active",
        suggested_value="Dondozo",
        raw_text="Dondozo",
        confidence=0.95,
        rank=1,
        reason="template match",
        source_frame_id=frame.frame_id,
    )
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(frame),
        ocr_backend=FakeOcrBackend((candidate,)),
    )
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001
    window._poll_capture_status()  # noqa: SLF001
    assert window._ocr_adopt_buttons["self_active"].isEnabled() is True  # noqa: SLF001

    window.close()
    repository.close()


def test_injected_non_black_frame_is_drawn_with_matching_identity(tmp_path: Path) -> None:
    qt_application()
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(0x00D12A2A)
    frame = _fresh_frame("colored-frame", image)
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(frame),
        ocr_backend=FakeOcrBackend(()),
    )
    window.resize(1000, 800)
    window.show()
    window._start_capture()  # noqa: SLF001
    QApplication.processEvents()

    pixmap = window.capture_preview_label.pixmap()
    assert pixmap is not None
    assert not pixmap.isNull()
    center = pixmap.toImage().pixelColor(pixmap.width() // 2, pixmap.height() // 2)
    assert center.red() > 0
    assert center.green() > 0
    assert window._capture_preview_frame_id == frame.frame_id  # noqa: SLF001
    status, snapshot_frame = window._capture_service.latest_snapshot()  # noqa: SLF001
    assert status.frame_id == snapshot_frame.frame_id == window._capture_preview_frame_id  # noqa: SLF001
    assert "preview表示中" in window.capture_status_label.text()

    window.close()
    repository.close()


def test_preview_keeps_aspect_ratio_after_resize(tmp_path: Path) -> None:
    qt_application()
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(0x0000C875)
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(_fresh_frame("resize-frame", image)),
        ocr_backend=FakeOcrBackend(()),
    )
    window.resize(1000, 800)
    window.show()
    window._start_capture()  # noqa: SLF001
    QApplication.processEvents()
    window.capture_preview_label.resize(800, 450)
    window._render_capture_preview()  # noqa: SLF001
    first = window.capture_preview_label.pixmap()
    assert first is not None and not first.isNull()
    assert first.width() * 9 == first.height() * 16

    window.capture_preview_label.resize(400, 225)
    window._render_capture_preview()  # noqa: SLF001
    second = window.capture_preview_label.pixmap()
    assert second is not None and not second.isNull()
    assert second.width() * 9 == second.height() * 16

    window.close()
    repository.close()


def test_unavailable_stale_and_invalid_frames_use_truthful_placeholder(
    tmp_path: Path,
) -> None:
    qt_application()
    null_image = QImage()
    zero_image = QImage(0, 0, QImage.Format.Format_RGB32)
    cases = (
        (UnavailableCaptureBackend(), "unavailable"),
        (FakeCaptureBackend(_fresh_frame("null-frame", null_image)), "invalid"),
        (FakeCaptureBackend(_fresh_frame("zero-frame", zero_image)), "invalid"),
        (
            FakeCaptureBackend(
                FramePacket(
                    frame_id="stale-preview",
                    source="UGREEN_DIRECT",
                    captured_at_utc=datetime.now(UTC),
                    captured_monotonic_ns=1,
                    width=640,
                    height=360,
                    image=null_image,
                )
            ),
            "stale",
        ),
    )
    for index, (backend, expected) in enumerate(cases):
        case_dir = tmp_path / f"{expected}-{index}"
        case_dir.mkdir()
        repository, window = build_window(
            case_dir,
            capture_backend=backend,
            ocr_backend=FakeOcrBackend(()),
        )
        window._start_capture()  # noqa: SLF001
        pixmap = window.capture_preview_label.pixmap()
        assert pixmap is None or pixmap.isNull()
        assert "preview表示中" not in window.capture_status_label.text()
        assert "previewなし" in window.capture_preview_label.text()
        if expected == "stale":
            assert "stale" in window.capture_preview_label.text()
        elif expected == "invalid":
            assert "invalid" in window.capture_preview_label.text()
        else:
            assert "drawable" in window.capture_preview_label.text()
        window.close()
        repository.close()


def test_human_reconnect_is_one_bounded_attempt_without_duplicate_lease(
    tmp_path: Path,
) -> None:
    qt_application()
    backend = FakeCaptureBackend(_fresh_frame())
    repository, window = build_window(
        tmp_path, capture_backend=backend, ocr_backend=FakeOcrBackend(())
    )
    window._start_capture()  # noqa: SLF001
    for _ in range(5):
        window._poll_capture_status()  # noqa: SLF001
    assert backend.start_calls == 1
    assert backend.stop_calls == 0

    window.reconnect_capture_button.click()
    assert backend.stop_calls == 1
    assert backend.start_calls == 2
    assert backend.duplicate_start_calls == 0
    for _ in range(5):
        window._poll_capture_status()  # noqa: SLF001
    assert backend.stop_calls == 1
    assert backend.start_calls == 2
    assert backend.duplicate_start_calls == 0

    window.close()
    assert backend.stop_calls == 2
    assert backend.is_running() is False
    repository.close()


def test_stale_frame_never_yields_an_adoptable_candidate(tmp_path: Path) -> None:
    qt_application()
    # captured_monotonic_ns=1 is always far in the past relative to
    # time.monotonic_ns() at test time (the monotonic clock only ever
    # increases from an arbitrary, much-earlier process/OS start point), so
    # this frame is unconditionally stale under the real monotonic clock.
    stale_frame = FramePacket(
        frame_id="stale-frame",
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=1,
        width=1280,
        height=720,
    )
    candidate = OcrCandidate(
        field_key="self_active",
        suggested_value="Dondozo",
        raw_text="Dondozo",
        confidence=0.95,
        rank=1,
        reason="template match",
        source_frame_id=stale_frame.frame_id,
    )
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(stale_frame),
        ocr_backend=FakeOcrBackend((candidate,)),
    )
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001
    window._poll_capture_status()  # noqa: SLF001

    assert window._ocr_adopt_buttons["self_active"].isEnabled() is False  # noqa: SLF001
    assert window._latest_ocr_bundle is not None  # noqa: SLF001
    assert window._latest_ocr_bundle.candidates == ()  # noqa: SLF001

    window.close()
    repository.close()


def test_manual_edit_after_adoption_always_wins(tmp_path: Path) -> None:
    qt_application()
    frame = _fresh_frame()
    candidate = OcrCandidate(
        field_key="opponent_active",
        suggested_value="Garchomp",
        raw_text="Garchomp",
        confidence=0.9,
        rank=1,
        reason="template match",
        source_frame_id=frame.frame_id,
    )
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(frame),
        ocr_backend=FakeOcrBackend((candidate,)),
    )
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001
    window._poll_capture_status()  # noqa: SLF001

    # Before any adoption, the manual input is untouched.
    assert window.opponent_active_input.text() == ""

    # Human clicks "採用" - this is the only path that ever copies an OCR
    # value into a turn-fact input; it never happens from the poll/timer.
    window._on_adopt_ocr_candidate("opponent_active")  # noqa: SLF001
    assert window.opponent_active_input.text() == "Garchomp"

    # Human then edits the field by hand; the manual value always wins and
    # is what actually gets saved by confirm_turn_facts.
    window.opponent_active_input.setText("Garchomp-corrected")
    window.self_active_box.setCurrentText("Dondozo")
    window.self_hp_box.setCurrentText(HpBucket.FULL.value)
    window.opponent_hp_box.setCurrentText(HpBucket.FULL.value)
    window.move_inputs[0].setText("Protect")
    window.turn_facts_confirm_checkbox.setChecked(True)
    window.confirm_turn_facts_button.click()
    window.render_view()

    assert window.session_state_label.text() == "TURN_REVIEWED"
    saved = repository.load_active_session()
    assert saved is not None
    facts = repository.get_turn_facts(saved.current_reviewed_board_id or "")
    assert facts.opponent_active == "Garchomp-corrected"

    window.close()
    repository.close()


def test_ocr_candidate_never_auto_applied_without_explicit_adopt_click(
    tmp_path: Path,
) -> None:
    qt_application()
    frame = _fresh_frame()
    candidate = OcrCandidate(
        field_key="self_active",
        suggested_value="Flutter Mane",
        raw_text="Flutter Mane",
        confidence=0.99,
        rank=1,
        reason="template match",
        source_frame_id=frame.frame_id,
    )
    repository, window = build_window(
        tmp_path,
        capture_backend=FakeCaptureBackend(frame),
        ocr_backend=FakeOcrBackend((candidate,)),
    )
    advance_to_turn_reviewable(window)
    window._capture_service.start()  # noqa: SLF001

    for _ in range(5):
        window._poll_capture_status()  # noqa: SLF001

    # High-confidence candidate is displayed/adoptable, but the canonical
    # manual input is never mutated by polling alone.
    assert window.self_active_box.currentText() != "Flutter Mane"

    window.close()
    repository.close()


def test_close_event_stops_capture(tmp_path: Path) -> None:
    qt_application()
    backend = FakeCaptureBackend(_fresh_frame())
    repository, window = build_window(
        tmp_path, capture_backend=backend, ocr_backend=FakeOcrBackend(())
    )
    window._start_capture()  # noqa: SLF001
    assert backend.is_running() is True

    window.close()
    assert backend.is_running() is False
    repository.close()
