"""Near-identical 2 fps samples do not create unbounded ROI observations."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage

from maple_next.capture.contracts import FrameKind, FramePacket
from maple_next.selection_roi.service import SelectionRoiService


def _write_config(root: Path) -> None:
    path = root / "selection" / "config" / "roi_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "maple-selection-roi.v1",
                "canonical_width": 1280,
                "canonical_height": 720,
                "source_provenance": "dedup-test",
                "slots": [
                    {
                        "slot": index + 1,
                        "x": 700,
                        "y": 40 + index * 100,
                        "width": 240,
                        "height": 80,
                    }
                    for index in range(6)
                ],
            }
        ),
        encoding="utf-8",
    )


def _frame(frame_id: str, *, changed_pixel: bool) -> FramePacket:
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("#222"))
    if changed_pixel:
        image.setPixelColor(701, 41, QColor("#232323"))
    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=time.monotonic_ns(),
        width=1280,
        height=720,
        image=image,
        frame_kind=FrameKind.CANONICAL,
    )


def test_near_duplicate_frame_reuses_one_observation(tmp_path: Path) -> None:
    root = tmp_path / "data" / "ocr"
    _write_config(root)
    service = SelectionRoiService(root)

    first = service.process_frame(_frame("first", changed_pixel=False))
    second = service.process_frame(_frame("second", changed_pixel=True))

    assert first.observation_id is not None
    assert second.observation_id == first.observation_id
    assert len(list((root / "selection" / "captures").glob("*"))) == 1
    assert service.metrics() == {
        "distinct_observation_count": 1,
        "near_duplicate_suppressed_count": 1,
    }
