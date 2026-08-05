"""Pure coverage for provisional Turn ROI configuration and OCR candidates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.domain.enums import HpBucket
from maple_next.ocr.contracts import OcrFieldKey
from maple_next.turn_ocr.config import load_turn_roi_config
from maple_next.turn_ocr.contracts import TurnSnapshotIdentity, TurnSnapshotRequest
from maple_next.turn_ocr.hp_reader import hp_bucket_from_ratio, read_hp_bar
from maple_next.turn_ocr.name_recognizer import NameCandidateMatch
from maple_next.turn_ocr.service import TurnSnapshotOcrService


def _write_config(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "contract_version": "maple-turn-roi.v1",
                "canvas_width": 1280,
                "canvas_height": 720,
                "layout": "test-layout",
                "provisional": True,
                "rois": {
                    "self_active": {"x": 10, "y": 10, "width": 100, "height": 20},
                    "opponent_active": {
                        "x": 1170,
                        "y": 10,
                        "width": 100,
                        "height": 20,
                    },
                    "self_hp": {"x": 10, "y": 680, "width": 160, "height": 20},
                    "opponent_hp": {
                        "x": 1110,
                        "y": 40,
                        "width": 160,
                        "height": 20,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _identity() -> TurnSnapshotIdentity:
    return TurnSnapshotIdentity(
        session_id="session-1",
        match_id="match-1",
        generation=1,
        turn_id="turn-1",
        turn_number=1,
        battle_revision=4,
        snapshot_generation=1,
    )


def _frame(image: QImage, *, kind: FrameKind = FrameKind.CANONICAL) -> FramePacket:
    return FramePacket(
        frame_id="frame-1",
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=1,
        width=1280,
        height=720,
        image=image,
        source_width=1280,
        source_height=720,
        content_rect=(0, 0, 1280, 720),
        frame_kind=kind,
    )


def _paint_ready_scene(image: QImage) -> None:
    for x, y, width, height in (
        (10, 10, 100, 20),
        (1170, 10, 100, 20),
        (10, 680, 160, 20),
        (1110, 40, 160, 20),
    ):
        for dx in range(width):
            color = QColor("#10b981") if dx < width * 3 // 4 else QColor("#111827")
            for dy in range(height):
                image.setPixelColor(x + dx, y + dy, color)


def test_load_turn_roi_config_preserves_provisional_coordinates(tmp_path: Path) -> None:
    config = load_turn_roi_config(_write_config(tmp_path / "roi.json"))

    assert config.provisional is True
    assert config.self_active.x == 10
    assert config.opponent_hp.width == 160
    assert config.canvas_width == 1280
    assert config.canvas_height == 720


@pytest.mark.parametrize(
    ("ratio", "bucket"),
    [
        (0.0, HpBucket.ZERO),
        (0.05, HpBucket.ONE_TO_TEN),
        (0.15, HpBucket.ELEVEN_TO_TWENTY),
        (0.55, HpBucket.FIFTY_ONE_TO_SIXTY),
        (0.95, HpBucket.NINETY_ONE_TO_NINETY_NINE),
        (1.0, HpBucket.FULL),
    ],
)
def test_hp_bucket_from_ratio_uses_canonical_ranges(
    ratio: float,
    bucket: HpBucket,
) -> None:
    assert hp_bucket_from_ratio(ratio) is bucket


def test_hp_reader_detects_contiguous_green_fill() -> None:
    image = QImage(160, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#111827"))
    for x in range(120):
        for y in range(4, 16):
            image.setPixelColor(x, y, QColor("#10b981"))

    estimate = read_hp_bar(image)

    assert estimate.detected is True
    assert estimate.ratio is not None
    assert 0.70 <= estimate.ratio <= 0.80
    assert estimate.bucket is HpBucket.SEVENTY_ONE_TO_EIGHTY


def test_turn_service_uses_only_supplied_name_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_turn_roi_config(_write_config(tmp_path / "roi.json"))
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#202020"))
    _paint_ready_scene(image)
    seen: list[tuple[str, ...]] = []

    def fake_recognizer(
        _crop: QImage,
        candidates: tuple[str, ...],
        *,
        top_k: int,
    ) -> tuple[NameCandidateMatch, ...]:
        seen.append(candidates)
        assert top_k == 3
        return (NameCandidateMatch(candidates[0], 0.91, 1),)

    monkeypatch.setattr(
        "maple_next.turn_ocr.service.recognize_candidate_name",
        fake_recognizer,
    )
    request = TurnSnapshotRequest(
        identity=_identity(),
        frame=_frame(image),
        self_active_candidates=("A", "B", "C"),
        opponent_active_candidates=("X", "Y", "Z", "U", "V", "W"),
    )

    result = TurnSnapshotOcrService(config).process(request)

    assert seen == [("A", "B", "C"), ("X", "Y", "Z", "U", "V", "W")]
    best = {candidate.field_key: candidate for candidate in result.bundle.candidates}
    assert best[OcrFieldKey.SELF_ACTIVE.value].suggested_value == "A"
    assert best[OcrFieldKey.OPPONENT_ACTIVE.value].suggested_value == "X"
    assert set(result.crops) == {"self_active", "opponent_active", "self_hp", "opponent_hp"}


def test_turn_service_rejects_source_kind_frame(tmp_path: Path) -> None:
    config = load_turn_roi_config(_write_config(tmp_path / "roi.json"))
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#202020"))
    request = TurnSnapshotRequest(
        identity=_identity(),
        frame=_frame(image, kind=FrameKind.SOURCE),
        self_active_candidates=("A", "B", "C"),
        opponent_active_candidates=("X", "Y", "Z", "U", "V", "W"),
    )

    result = TurnSnapshotOcrService(config).process(request)

    assert result.status == "FRAME_NOT_CANONICAL"
    assert result.bundle.candidates == ()
