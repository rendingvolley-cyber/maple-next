"""Deterministic image similarity matching for six opponent selection ROIs."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from maple_next.selection_roi.contracts import (
    SELECTION_SLOT_COUNT,
    UNKNOWN_LABEL,
    SelectionCandidateScore,
    SelectionRoiCrop,
    SelectionRoiError,
    SelectionSlotMatch,
)

_SIGNATURE_SIZE: Final[int] = 16
_DHASH_WIDTH: Final[int] = 9
_DHASH_HEIGHT: Final[int] = 8
_SUPPORTED_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
)


def _normalized_pixel_bytes(image: QImage) -> bytes:
    """Return deterministic full-pixel bytes for exact duplicate identity."""

    normalized = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixel_bytes = bytes(normalized.constBits())
    expected_size = normalized.width() * normalized.height() * 4
    if len(pixel_bytes) != expected_size:
        raise SelectionRoiError("selection ROI pixel buffer has unexpected padding")
    header = struct.pack(">II", normalized.width(), normalized.height())
    return header + pixel_bytes


@dataclass(frozen=True, slots=True)
class ImageFingerprint:
    exact_hash: str
    grayscale_signature: tuple[int, ...]
    dhash: int

    @classmethod
    def from_image(cls, image: QImage) -> ImageFingerprint:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            raise SelectionRoiError("selection ROI image is invalid")
        exact_hash = hashlib.sha256(_normalized_pixel_bytes(image)).hexdigest()
        grayscale = image.convertToFormat(QImage.Format.Format_Grayscale8)
        signature_image = grayscale.scaled(
            _SIGNATURE_SIZE,
            _SIGNATURE_SIZE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        signature = tuple(
            signature_image.pixelColor(x, y).red()
            for y in range(_SIGNATURE_SIZE)
            for x in range(_SIGNATURE_SIZE)
        )
        dhash_image = grayscale.scaled(
            _DHASH_WIDTH,
            _DHASH_HEIGHT,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        dhash = 0
        bit = 0
        for y in range(_DHASH_HEIGHT):
            for x in range(_DHASH_WIDTH - 1):
                if dhash_image.pixelColor(x, y).red() > dhash_image.pixelColor(x + 1, y).red():
                    dhash |= 1 << bit
                bit += 1
        return cls(
            exact_hash=exact_hash,
            grayscale_signature=signature,
            dhash=dhash,
        )


@dataclass(frozen=True, slots=True)
class ReferenceExample:
    label: str
    path: Path
    fingerprint: ImageFingerprint


def fingerprint_similarity(left: ImageFingerprint, right: ImageFingerprint) -> float:
    if left.exact_hash == right.exact_hash:
        return 1.0
    dhash_distance = (left.dhash ^ right.dhash).bit_count()
    dhash_score = 1.0 - dhash_distance / 64.0
    pixel_difference = sum(
        abs(left_value - right_value)
        for left_value, right_value in zip(
            left.grayscale_signature,
            right.grayscale_signature,
            strict=True,
        )
    )
    pixel_score = 1.0 - pixel_difference / (
        len(left.grayscale_signature) * 255.0
    )
    return max(0.0, min(1.0, 0.65 * dhash_score + 0.35 * pixel_score))


class ReferenceImageIndex:
    """Read-only index of human-confirmed labeled ROI images."""

    def __init__(self, labeled_root: Path) -> None:
        self._labeled_root = labeled_root
        self._signature: tuple[tuple[str, int, int], ...] = ()
        self._examples: tuple[ReferenceExample, ...] = ()

    @property
    def reference_count(self) -> int:
        return len(self._examples)

    def refresh(self) -> None:
        paths = tuple(
            sorted(
                (
                    path
                    for path in self._labeled_root.glob("*/*")
                    if path.is_file() and path.suffix.casefold() in _SUPPORTED_IMAGE_SUFFIXES
                ),
                key=lambda item: item.as_posix().casefold(),
            )
        )
        signature_items: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature_items.append(
                (path.relative_to(self._labeled_root).as_posix(), stat.st_size, stat.st_mtime_ns)
            )
        signature = tuple(signature_items)
        if signature == self._signature:
            return
        examples: list[ReferenceExample] = []
        for relative_path, _size, _mtime_ns in signature:
            path = self._labeled_root / relative_path
            label = path.parent.name.strip()
            image = QImage(str(path))
            if not label or image.isNull():
                continue
            try:
                fingerprint = ImageFingerprint.from_image(image)
            except SelectionRoiError:
                continue
            examples.append(
                ReferenceExample(
                    label=label,
                    path=path,
                    fingerprint=fingerprint,
                )
            )
        self._signature = signature
        self._examples = tuple(examples)

    def candidates_for(
        self,
        crop: SelectionRoiCrop,
        *,
        top_k: int,
    ) -> tuple[SelectionCandidateScore, ...]:
        fingerprint = ImageFingerprint.from_image(crop.image)
        best_by_label: dict[str, float] = {}
        counts_by_label: dict[str, int] = {}
        for example in self._examples:
            score = fingerprint_similarity(fingerprint, example.fingerprint)
            counts_by_label[example.label] = counts_by_label.get(example.label, 0) + 1
            previous = best_by_label.get(example.label)
            if previous is None or score > previous:
                best_by_label[example.label] = score
        ordered = sorted(
            (
                SelectionCandidateScore(
                    label=label,
                    score=score,
                    reference_count=counts_by_label[label],
                )
                for label, score in best_by_label.items()
            ),
            key=lambda item: (-item.score, item.label.casefold(), item.label),
        )
        return tuple(ordered[:top_k])


def assign_unique_team_candidates(
    crops: tuple[SelectionRoiCrop, ...],
    candidate_lists: tuple[tuple[SelectionCandidateScore, ...], ...],
    *,
    threshold: float,
) -> tuple[SelectionSlotMatch, ...]:
    if len(crops) != SELECTION_SLOT_COUNT or len(candidate_lists) != SELECTION_SLOT_COUNT:
        raise SelectionRoiError("selection matching requires exactly six slots")

    filtered_options: list[tuple[SelectionCandidateScore | None, ...]] = []
    for candidates in candidate_lists:
        accepted = tuple(candidate for candidate in candidates if candidate.score >= threshold)
        filtered_options.append((*accepted, None))

    best_score = -1.0
    best_assignment: tuple[SelectionCandidateScore | None, ...] | None = None

    def visit(
        slot_index: int,
        used_labels: frozenset[str],
        score: float,
        assignment: tuple[SelectionCandidateScore | None, ...],
    ) -> None:
        nonlocal best_score, best_assignment
        if slot_index == SELECTION_SLOT_COUNT:
            if score > best_score + 1e-12:
                best_score = score
                best_assignment = assignment
            return
        for option in filtered_options[slot_index]:
            if option is not None and option.label in used_labels:
                continue
            next_used = (
                used_labels
                if option is None
                else frozenset((*used_labels, option.label))
            )
            visit(
                slot_index + 1,
                next_used,
                score + (0.0 if option is None else option.score),
                (*assignment, option),
            )

    visit(0, frozenset(), 0.0, ())
    if best_assignment is None:
        best_assignment = (None,) * SELECTION_SLOT_COUNT

    results: list[SelectionSlotMatch] = []
    for crop, top_candidates, assigned in zip(
        crops,
        candidate_lists,
        best_assignment,
        strict=True,
    ):
        results.append(
            SelectionSlotMatch(
                slot=crop.slot,
                crop=crop.image,
                assigned_label=UNKNOWN_LABEL if assigned is None else assigned.label,
                assigned_score=0.0 if assigned is None else assigned.score,
                top_candidates=top_candidates,
            )
        )
    return tuple(results)


def match_selection_crops(
    crops: tuple[SelectionRoiCrop, ...],
    index: ReferenceImageIndex,
    *,
    threshold: float = 0.72,
    top_k: int = 3,
) -> tuple[SelectionSlotMatch, ...]:
    if not 0.0 <= threshold <= 1.0:
        raise SelectionRoiError("selection match threshold is invalid")
    if top_k <= 0:
        raise SelectionRoiError("selection match top_k is invalid")
    index.refresh()
    candidate_lists = tuple(index.candidates_for(crop, top_k=top_k) for crop in crops)
    return assign_unique_team_candidates(
        crops,
        candidate_lists,
        threshold=threshold,
    )
