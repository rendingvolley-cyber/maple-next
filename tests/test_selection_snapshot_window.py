"""NEW MATCH binds the new generation, then reacquires and submits one frame.

The reacquire is a bounded one-shot wait, not a single poll: a fast-path
synchronous read is tried first, and only when that already yields a fresh
frame is it used directly. Otherwise the request stays armed for the first
CaptureService.frame_ready canonical frame, bounded by a timeout.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import (
    CaptureStatus,
    CaptureStatusCode,
    FrameKind,
    FramePacket,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.selection_roi.contracts import SelectionMatchBundle
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.selection_snapshot_window import SelectionSnapshotMatchFlowWindow


def _qt_application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _build_window(
    tmp_path: Path,
) -> tuple[SQLiteRepository, SelectionSnapshotMatchFlowWindow]:
    _qt_application()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = MatchApplication(repository, tmp_path / "exports")
    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    window = SelectionSnapshotMatchFlowWindow(
        controller,
        ocr_data_directory=tmp_path / "data" / "ocr",
    )
    return repository, window


def _available_snapshot(
    image: QImage,
    *,
    frame_id: str = "live-frame-1",
    monotonic_ns: int = 1_000_000,
) -> tuple[CaptureStatus, FramePacket]:
    captured_at = datetime.now(UTC)
    frame = FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=captured_at,
        captured_monotonic_ns=monotonic_ns,
        width=1280,
        height=720,
        image=image,
        source_width=1280,
        source_height=720,
        content_rect=(0, 0, 1280, 720),
        frame_kind=FrameKind.CANONICAL,
    )
    status = CaptureStatus(
        status=CaptureStatusCode.AVAILABLE,
        available=True,
        manual_entry_allowed=True,
        source="UGREEN_DIRECT",
        device_label="UGREEN test",
        frame_id=frame.frame_id,
        captured_at_utc=captured_at,
        age_ms=0,
        fresh=True,
        width=1280,
        height=720,
        error_code=None,
        operator_message=None,
    )
    return status, frame


def _unavailable_snapshot() -> tuple[CaptureStatus, None]:
    return (
        CaptureStatus(
            status=CaptureStatusCode.FRAME_UNAVAILABLE,
            available=False,
            manual_entry_allowed=True,
            source="UGREEN_DIRECT",
            device_label="UGREEN test",
            frame_id=None,
            captured_at_utc=None,
            age_ms=None,
            fresh=False,
            width=None,
            height=None,
            error_code="CAPTURE_FRAME_UNAVAILABLE",
            operator_message="映像フレームを取得できません。手動入力で続行できます。",
        ),
        None,
    )


def _sequential_live_feed(
    image: QImage,
) -> tuple[list[tuple[CaptureStatus, FramePacket]], object]:
    """A fake capture backend where every read observes a newer frame.

    Mirrors a continuously running UGREEN feed: each ``latest_snapshot()``
    call returns a distinct frame_id and a strictly increasing monotonic
    timestamp, so a post-transition reacquire is always demonstrably newer
    than whatever pre-transition baseline was read first.
    """

    calls: list[tuple[CaptureStatus, FramePacket]] = []

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        index = len(calls) + 1
        result = _available_snapshot(
            image,
            frame_id=f"live-frame-{index}",
            monotonic_ns=1_000_000 + index,
        )
        calls.append(result)
        return result

    return calls, latest_snapshot


def test_selection_roi_timer_never_runs_in_official_snapshot_window(
    tmp_path: Path,
) -> None:
    repository, window = _build_window(tmp_path)

    assert window._selection_roi_timer is not None  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001
    window._sync_selection_roi_timer()  # noqa: SLF001
    window._poll_selection_roi()  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_new_match_binds_generation_before_reacquiring_and_submits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case A: a fresh post-transition frame is bound to the new generation."""

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#112233"))
    calls, latest_snapshot = _sequential_live_feed(source_image)

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    before_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001
    window.new_match_button.click()
    after_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001

    # One pre-transition baseline read, one post-transition reacquire.
    assert len(calls) == 2
    assert after_identity != before_identity
    assert len(submitted) == 1
    frozen = submitted[0]
    assert frozen.frame_id.startswith("live-frame-2:new-match-snapshot:")
    assert frozen.image.pixelColor(0, 0) == QColor("#112233")
    assert (
        window._selection_roi_submitted_identities[frozen.frame_id]  # noqa: SLF001
        == after_identity
    )
    assert window._controller.refresh().projection.session_state == "SELECTION_OPEN"  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_new_match_does_not_submit_stale_pretransition_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case B: the fast-path reacquire observes the same frame as the baseline.

    This must not fail permanently - it stays armed for the async
    CaptureService.frame_ready fallback (covered by the decisive regression
    test below) until the bounded timeout, which is exercised here directly.
    """

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#445566"))
    calls = 0

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        nonlocal calls
        calls += 1
        # Capture backend has not produced a new frame since NEW MATCH was
        # pressed: baseline and reacquire observe the identical capture.
        return _available_snapshot(
            source_image, frame_id="stale-frame", monotonic_ns=42
        )

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()

    assert calls == 2
    assert submitted == []
    assert window._controller.refresh().projection.session_state == "SELECTION_OPEN"  # noqa: SLF001
    assert "新しい映像" in window.selection_roi_status_label.text()
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001
    assert window._new_match_reacquire_timer.isActive()  # noqa: SLF001

    # Bounded one-shot timeout: no further capture reads or retries, just a
    # single fail-closed transition to a truthful unavailable message.
    window._on_new_match_reacquire_timeout()  # noqa: SLF001
    assert calls == 2
    assert submitted == []
    assert window._new_match_reacquire_pending is None  # noqa: SLF001
    assert "取得できませんでした" in window.selection_roi_status_label.text()

    window.close()
    repository.close()


def test_new_match_without_frame_keeps_manual_selection_flow_and_submits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case C: no fresh frame at all -> truthful unavailable state, no retry loop."""

    repository, window = _build_window(tmp_path)
    submitted: list[FramePacket] = []
    calls = 0

    def latest_snapshot() -> tuple[CaptureStatus, None]:
        nonlocal calls
        calls += 1
        return _unavailable_snapshot()

    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()

    assert calls == 2
    assert submitted == []
    assert window._controller.refresh().projection.session_state == "SELECTION_OPEN"  # noqa: SLF001
    assert "手動入力" in window.selection_roi_status_label.text()
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001

    # No hidden retry/poll loop: further polling stays a no-op and issues no
    # additional capture reads.
    window._poll_selection_roi()  # noqa: SLF001
    assert calls == 2

    # The bounded timeout is the only thing that ever ends the wait - it also
    # issues no additional capture reads.
    window._on_new_match_reacquire_timeout()  # noqa: SLF001
    assert calls == 2
    assert submitted == []
    assert window._new_match_reacquire_pending is None  # noqa: SLF001

    window.close()
    repository.close()


def test_two_rapid_new_matches_reject_late_result_from_first_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case D: a late result bound to generation 1 cannot populate generation 2."""

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#223344"))
    calls, latest_snapshot = _sequential_live_feed(source_image)

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()
    first_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001
    # Free the active slot the way a real operator would (abort), then press
    # NEW MATCH again to bind a genuinely distinct generation. The domain
    # layer forbids two concurrently active sessions, so this is the
    # supported path to a second generation, not a UI shortcut.
    window._controller.abort_match(human_confirmed=True)  # noqa: SLF001
    window.render_view(window._controller.refresh())  # noqa: SLF001
    window.new_match_button.click()
    second_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001

    assert first_identity != second_identity
    assert len(submitted) == 2
    first_frame, second_frame = submitted

    stale_bundle = SelectionMatchBundle(
        status="OK",
        operator_message="ok",
        frame_id=first_frame.frame_id,
        observation_id=None,
        slots=(),
        reference_count=0,
        roi_config_provenance="test",
    )
    # Simulate the first submission's result still being in flight when it
    # finally arrives, to exercise the generation guard directly rather than
    # relying only on the incidental dict-clearing side effect of the second
    # submission.
    window._selection_roi_submitted_identities[first_frame.frame_id] = (  # noqa: SLF001
        first_identity
    )
    window._on_selection_roi_result(stale_bundle)  # noqa: SLF001
    assert window._selection_roi_bundle is None  # noqa: SLF001

    fresh_bundle = SelectionMatchBundle(
        status="OK",
        operator_message="ok",
        frame_id=second_frame.frame_id,
        observation_id=None,
        slots=(),
        reference_count=0,
        roi_config_provenance="test",
    )
    window._on_selection_roi_result(fresh_bundle)  # noqa: SLF001
    assert window._selection_roi_bundle is fresh_bundle  # noqa: SLF001
    assert window._selection_roi_bundle_identity == second_identity  # noqa: SLF001

    window.close()
    repository.close()


def test_new_match_async_frame_ready_submits_first_fresh_frame_after_stale_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mandatory decisive regression.

    baseline A -> NEW MATCH -> immediate post-bind latest_snapshot() still
    returns A -> zero ROI submits so far -> later CaptureService.frame_ready
    emits fresh B -> exactly one ROI submit for B, bound to the new
    generation. This is the exact field failure mode: a single synchronous
    reacquire read taken microseconds after generation-bind is not
    guaranteed to already reflect a new capture, so the request must stay
    armed for the capture backend's own frame_ready signal instead of
    failing closed immediately.
    """

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#a1b2c3"))
    baseline_status, baseline_frame = _available_snapshot(
        source_image, frame_id="frame-A", monotonic_ns=100
    )

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        # Every synchronous read - baseline and the immediate post-bind fast
        # path - observes the identical pre-transition frame A.
        return baseline_status, baseline_frame

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()
    new_generation_identity = window._selection_identity(  # noqa: SLF001
        window._controller.refresh()
    )

    # Zero ROI submits so far: the fast path only ever saw the stale baseline.
    assert submitted == []
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001
    assert window._new_match_reacquire_timer.isActive()  # noqa: SLF001

    _fresh_status, fresh_frame_b = _available_snapshot(
        source_image, frame_id="frame-B", monotonic_ns=200
    )
    window._capture_service.frame_ready.emit(fresh_frame_b)  # noqa: SLF001

    assert len(submitted) == 1
    frozen = submitted[0]
    assert frozen.frame_id.startswith("frame-B:new-match-snapshot:")
    assert (
        window._selection_roi_submitted_identities[frozen.frame_id]  # noqa: SLF001
        == new_generation_identity
    )
    assert window._new_match_reacquire_pending is None  # noqa: SLF001
    assert not window._new_match_reacquire_timer.isActive()  # noqa: SLF001

    # A second, older-or-equal frame arriving afterwards changes nothing:
    # already disarmed, exactly one submit total.
    window._capture_service.frame_ready.emit(fresh_frame_b)  # noqa: SLF001
    assert len(submitted) == 1

    window.close()
    repository.close()


def test_second_new_match_replaces_still_armed_first_pending_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second NEW MATCH cancels an unresolved first wait, not just its result."""

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#334455"))
    stale_status, stale_frame = _available_snapshot(
        source_image, frame_id="frame-A", monotonic_ns=100
    )

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        return stale_status, stale_frame

    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()
    first_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001
    assert window._new_match_reacquire_pending.target_identity == first_identity  # noqa: SLF001

    window._controller.abort_match(human_confirmed=True)  # noqa: SLF001
    window.render_view(window._controller.refresh())  # noqa: SLF001
    window.new_match_button.click()
    second_identity = window._selection_identity(window._controller.refresh())  # noqa: SLF001

    assert first_identity != second_identity
    assert submitted == []
    # Replaced, not merely appended to: the still-armed request now belongs
    # to the second generation only.
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001
    assert window._new_match_reacquire_pending.target_identity == second_identity  # noqa: SLF001

    _fresh_status, fresh_frame_b = _available_snapshot(
        source_image, frame_id="frame-B", monotonic_ns=200
    )
    window._capture_service.frame_ready.emit(fresh_frame_b)  # noqa: SLF001

    assert len(submitted) == 1
    assert (
        window._selection_roi_submitted_identities[submitted[0].frame_id]  # noqa: SLF001
        == second_identity
    )

    window.close()
    repository.close()


def test_successful_one_shot_submission_does_not_enable_continuous_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case E: a successful reacquire+result never re-arms the Selection timer."""

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#556677"))
    _calls, latest_snapshot = _sequential_live_feed(source_image)

    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    submitted: list[FramePacket] = []
    monkeypatch.setattr(window._selection_roi_worker, "submit", submitted.append)  # noqa: SLF001

    window.new_match_button.click()
    assert len(submitted) == 1
    frame = submitted[0]
    bundle = SelectionMatchBundle(
        status="OK",
        operator_message="ok",
        frame_id=frame.frame_id,
        observation_id=None,
        slots=(),
        reference_count=0,
        roi_config_provenance="test",
    )
    window._on_selection_roi_result(bundle)  # noqa: SLF001

    assert not window._selection_roi_timer.isActive()  # noqa: SLF001
    window.header_tabs.setCurrentIndex(0)
    window._sync_selection_roi_timer()  # noqa: SLF001
    assert not window._selection_roi_timer.isActive()  # noqa: SLF001

    window.close()
    repository.close()


def test_close_event_disarms_a_still_armed_reacquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the window while armed leaves no dangling timer behind."""

    repository, window = _build_window(tmp_path)
    source_image = QImage(1280, 720, QImage.Format.Format_RGB32)
    source_image.fill(QColor("#667788"))
    stale_status, stale_frame = _available_snapshot(
        source_image, frame_id="frame-A", monotonic_ns=100
    )

    def latest_snapshot() -> tuple[CaptureStatus, FramePacket]:
        return stale_status, stale_frame

    monkeypatch.setattr(window._capture_service, "latest_snapshot", latest_snapshot)  # noqa: SLF001
    assert window._selection_roi_worker is not None  # noqa: SLF001
    monkeypatch.setattr(window._selection_roi_worker, "submit", lambda _frame: None)  # noqa: SLF001

    window.new_match_button.click()
    assert window._new_match_reacquire_pending is not None  # noqa: SLF001
    assert window._new_match_reacquire_timer.isActive()  # noqa: SLF001

    window.close()

    assert window._new_match_reacquire_pending is None  # noqa: SLF001
    assert not window._new_match_reacquire_timer.isActive()  # noqa: SLF001

    repository.close()
