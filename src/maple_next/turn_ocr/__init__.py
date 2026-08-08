"""Human-triggered, candidate-only Turn screenshot OCR."""

from maple_next.turn_ocr.config import TurnRoiConfigError, load_turn_roi_config
from maple_next.turn_ocr.contracts import (
    TURN_SNAPSHOT_AUTO_FILL_CONFIDENCE,
    TURN_SNAPSHOT_DISPLAY_CONFIDENCE,
    TURN_SNAPSHOT_MAX_FRAME_AGE_MS,
    TurnRoiConfig,
    TurnRoiRect,
    TurnSnapshotIdentity,
    TurnSnapshotRequest,
    TurnSnapshotResult,
    TurnSnapshotStatus,
)
from maple_next.turn_ocr.hp_reader import HpEstimate, hp_bucket_from_ratio, read_hp_bar
from maple_next.turn_ocr.service import TurnSnapshotOcrService
from maple_next.turn_ocr.worker import TurnSnapshotOcrWorker

__all__ = [
    "TURN_SNAPSHOT_AUTO_FILL_CONFIDENCE",
    "TURN_SNAPSHOT_DISPLAY_CONFIDENCE",
    "TURN_SNAPSHOT_MAX_FRAME_AGE_MS",
    "HpEstimate",
    "TurnRoiConfig",
    "TurnRoiConfigError",
    "TurnRoiRect",
    "TurnSnapshotIdentity",
    "TurnSnapshotOcrService",
    "TurnSnapshotOcrWorker",
    "TurnSnapshotRequest",
    "TurnSnapshotResult",
    "TurnSnapshotStatus",
    "hp_bucket_from_ratio",
    "load_turn_roi_config",
    "read_hp_bar",
]
