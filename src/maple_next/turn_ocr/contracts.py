"""Immutable contracts for one human-triggered Turn screenshot OCR request."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtGui import QImage

from maple_next.capture.contracts import FramePacket
from maple_next.ocr.contracts import OcrCandidateBundle

TURN_SNAPSHOT_MAX_FRAME_AGE_MS = 500
TURN_SNAPSHOT_AUTO_FILL_CONFIDENCE = 0.80
TURN_SNAPSHOT_DISPLAY_CONFIDENCE = 0.50


class TurnSnapshotStatus(StrEnum):
    IDLE = "IDLE"
    CAPTURED = "CAPTURED"
    ANALYZING = "ANALYZING"
    READY = "READY"
    NO_FRAME = "NO_FRAME"
    FRAME_STALE = "FRAME_STALE"
    FRAME_NOT_CANONICAL = "FRAME_NOT_CANONICAL"
    SCENE_NOT_READY = "SCENE_NOT_READY"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_FAILED = "OCR_FAILED"
    STALE_RESULT_DISCARDED = "STALE_RESULT_DISCARDED"


@dataclass(frozen=True, slots=True)
class TurnRoiRect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("turn OCR ROI must be a positive rectangle")
        if self.x + self.width > 1280 or self.y + self.height > 720:
            raise ValueError("turn OCR ROI exceeds the canonical 1280x720 canvas")


@dataclass(frozen=True, slots=True)
class TurnRoiConfig:
    contract_version: str
    canvas_width: int
    canvas_height: int
    layout: str
    provisional: bool
    self_active: TurnRoiRect
    opponent_active: TurnRoiRect
    self_hp: TurnRoiRect
    opponent_hp: TurnRoiRect
    source_path: Path

    def __post_init__(self) -> None:
        if self.contract_version != "maple-turn-roi.v1":
            raise ValueError("unsupported turn ROI contract version")
        if self.canvas_width != 1280 or self.canvas_height != 720:
            raise ValueError("turn ROI config must use the canonical 1280x720 canvas")
        if not self.layout.strip():
            raise ValueError("turn ROI layout must not be blank")


@dataclass(frozen=True, slots=True)
class TurnSnapshotIdentity:
    session_id: str
    match_id: str
    generation: int
    turn_id: str
    turn_number: int
    battle_revision: int
    snapshot_generation: int


@dataclass(frozen=True, slots=True)
class TurnSnapshotRequest:
    identity: TurnSnapshotIdentity
    frame: FramePacket
    self_active_candidates: tuple[str, ...]
    opponent_active_candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnSnapshotResult:
    identity: TurnSnapshotIdentity
    status: str
    bundle: OcrCandidateBundle
    frozen_image: QImage
    crops: dict[str, QImage]
    operator_message: str
    roi_config_provenance: str
