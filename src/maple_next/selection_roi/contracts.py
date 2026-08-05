"""Contracts for opponent-team ROI image matching and assisted input."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PySide6.QtGui import QImage

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
)

ROI_CONFIG_SCHEMA: Final[str] = "maple-selection-roi.v1"
SELECTION_SLOT_COUNT: Final[int] = 6
UNKNOWN_LABEL: Final[str] = "UNKNOWN"
_UNSAFE_LABEL_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'[<>:"/\\|?*\x00-\x1f]'
)
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_OPEN_PAREN_SPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s*\(\s*")
_CLOSE_PAREN_SPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s*\)")
_WINDOWS_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class SelectionRoiError(ValueError):
    """Sanitized, expected ROI/matcher configuration error."""


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionRoiError(f"selection ROI {key} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class SelectionRoiRect:
    slot: int
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not 1 <= self.slot <= SELECTION_SLOT_COUNT:
            raise SelectionRoiError("selection ROI slot must be 1..6")
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise SelectionRoiError("selection ROI rectangle is invalid")
        if self.x + self.width > CANONICAL_FRAME_WIDTH:
            raise SelectionRoiError("selection ROI exceeds canonical width")
        if self.y + self.height > CANONICAL_FRAME_HEIGHT:
            raise SelectionRoiError("selection ROI exceeds canonical height")


@dataclass(frozen=True, slots=True)
class SelectionRoiConfig:
    schema_version: str
    canonical_width: int
    canonical_height: int
    slots: tuple[SelectionRoiRect, ...]
    source_provenance: str

    @classmethod
    def load(cls, path: Path) -> SelectionRoiConfig:
        try:
            raw_payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SelectionRoiError("selection ROI config could not be loaded") from error
        if not isinstance(raw_payload, dict):
            raise SelectionRoiError("selection ROI config must be an object")
        payload = {str(key): value for key, value in raw_payload.items()}
        schema_version = payload.get("schema_version")
        source_provenance = payload.get("source_provenance", "")
        raw_slots = payload.get("slots")
        if not isinstance(schema_version, str) or schema_version != ROI_CONFIG_SCHEMA:
            raise SelectionRoiError("selection ROI config schema is unsupported")
        canonical_width = _required_int(payload, "canonical_width")
        canonical_height = _required_int(payload, "canonical_height")
        if canonical_width != CANONICAL_FRAME_WIDTH or canonical_height != CANONICAL_FRAME_HEIGHT:
            raise SelectionRoiError("selection ROI config must target 1280x720")
        if not isinstance(source_provenance, str):
            raise SelectionRoiError("selection ROI config provenance is invalid")
        if not isinstance(raw_slots, list) or len(raw_slots) != SELECTION_SLOT_COUNT:
            raise SelectionRoiError("selection ROI config must contain exactly six slots")
        slots: list[SelectionRoiRect] = []
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, dict):
                raise SelectionRoiError("selection ROI slot must be an object")
            slot_payload = {str(key): value for key, value in raw_slot.items()}
            slots.append(
                SelectionRoiRect(
                    slot=_required_int(slot_payload, "slot"),
                    x=_required_int(slot_payload, "x"),
                    y=_required_int(slot_payload, "y"),
                    width=_required_int(slot_payload, "width"),
                    height=_required_int(slot_payload, "height"),
                )
            )
        slots.sort(key=lambda item: item.slot)
        if tuple(item.slot for item in slots) != tuple(range(1, SELECTION_SLOT_COUNT + 1)):
            raise SelectionRoiError("selection ROI slots must be unique and complete")
        return cls(
            schema_version=schema_version,
            canonical_width=canonical_width,
            canonical_height=canonical_height,
            slots=tuple(slots),
            source_provenance=source_provenance,
        )


@dataclass(frozen=True, slots=True)
class SelectionRoiCrop:
    slot: int
    image: QImage
    rect: SelectionRoiRect


@dataclass(frozen=True, slots=True)
class SelectionCandidateScore:
    label: str
    score: float
    reference_count: int


@dataclass(frozen=True, slots=True)
class SelectionSlotMatch:
    slot: int
    crop: QImage
    assigned_label: str
    assigned_score: float
    top_candidates: tuple[SelectionCandidateScore, ...]


@dataclass(frozen=True, slots=True)
class SelectionMatchBundle:
    status: str
    operator_message: str
    frame_id: str | None
    observation_id: str | None
    slots: tuple[SelectionSlotMatch, ...]
    reference_count: int
    roi_config_provenance: str

    @property
    def candidate_only(self) -> bool:
        return True


def normalize_selection_label(label: str) -> str:
    """Normalize harmless corpus naming drift without changing the species name.

    Historical assets contain directory variants such as ``イダイトウ (オス)``
    and ``イダイトウ(オス)``. Treat those as one matcher label while preserving
    ordinary spaces and parentheses in the operator-visible name.
    """

    normalized = unicodedata.normalize("NFKC", label).strip()
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized)
    normalized = _OPEN_PAREN_SPACE_PATTERN.sub("(", normalized)
    normalized = _CLOSE_PAREN_SPACE_PATTERN.sub(")", normalized)
    if not normalized or normalized in {".", ".."}:
        raise SelectionRoiError("selection label is empty")
    return normalized


def safe_label_directory(label: str) -> str:
    """Return a Windows-safe directory while retaining the display label.

    Unlike the earlier broad sanitizer, this keeps legitimate Pokémon-form
    punctuation such as parentheses. That prevents newly learned images from
    creating underscore labels that the matcher would later show to the user.
    """

    normalized = normalize_selection_label(label)
    safe = _UNSAFE_LABEL_PATH_PATTERN.sub("_", normalized).rstrip(" .")
    safe = safe[:80].rstrip(" .")
    if not safe or safe in {".", ".."}:
        raise SelectionRoiError("selection label is invalid")
    if safe.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise SelectionRoiError("selection label is reserved")
    return safe
