"""Selection ROI crop, capture, matching, and human-confirmed feedback storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from PySide6.QtGui import QImage

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    FrameKind,
    FramePacket,
)
from maple_next.selection_roi.contracts import (
    SELECTION_SLOT_COUNT,
    SelectionMatchBundle,
    SelectionRoiConfig,
    SelectionRoiCrop,
    SelectionRoiError,
    SelectionSlotMatch,
    safe_label_directory,
)
from maple_next.selection_roi.matcher import (
    ImageFingerprint,
    ReferenceImageIndex,
    fingerprint_similarity,
    match_selection_crops,
)

_CONFIG_RELATIVE_PATH: Final[Path] = Path("selection/config/roi_config.json")
_LABELED_RELATIVE_PATH: Final[Path] = Path("selection/reference/labeled")
_UNLABELED_RELATIVE_PATH: Final[Path] = Path("selection/reference/unlabeled")
_CAPTURES_RELATIVE_PATH: Final[Path] = Path("selection/captures")
_QUARANTINE_RELATIVE_PATH: Final[Path] = Path("selection/quarantine")
_FEEDBACK_RELATIVE_PATH: Final[Path] = Path("selection/feedback/selection_labels.jsonl")
_MANIFEST_RELATIVE_PATH: Final[Path] = Path(
    "selection/manifests/selection_roi_manifest.jsonl"
)
_NEAR_DUPLICATE_THRESHOLD: Final[float] = 0.995


@dataclass(frozen=True, slots=True)
class SelectionRoiPaths:
    root: Path
    config_file: Path
    labeled_root: Path
    unlabeled_root: Path
    captures_root: Path
    quarantine_root: Path
    feedback_file: Path
    manifest_file: Path

    @classmethod
    def from_root(cls, root: Path) -> SelectionRoiPaths:
        resolved = root.expanduser().resolve()
        return cls(
            root=resolved,
            config_file=resolved / _CONFIG_RELATIVE_PATH,
            labeled_root=resolved / _LABELED_RELATIVE_PATH,
            unlabeled_root=resolved / _UNLABELED_RELATIVE_PATH,
            captures_root=resolved / _CAPTURES_RELATIVE_PATH,
            quarantine_root=resolved / _QUARANTINE_RELATIVE_PATH,
            feedback_file=resolved / _FEEDBACK_RELATIVE_PATH,
            manifest_file=resolved / _MANIFEST_RELATIVE_PATH,
        )

    def ensure_runtime_directories(self) -> None:
        for directory in (
            self.labeled_root,
            self.unlabeled_root,
            self.captures_root,
            self.quarantine_root,
            self.feedback_file.parent,
            self.manifest_file.parent,
            self.config_file.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class StoredSelectionObservation:
    observation_id: str
    frame_id: str
    captured_at_utc: datetime
    slot_files: tuple[Path, ...]
    slot_hashes: tuple[str, ...]
    fingerprints: tuple[ImageFingerprint, ...]
    matches: tuple[SelectionSlotMatch, ...]


@dataclass(frozen=True, slots=True)
class FeedbackStoreResult:
    added_count: int
    duplicate_count: int
    conflict_count: int


class SelectionRoiService:
    """Thread-safe, candidate-only image matcher and local feedback store."""

    def __init__(
        self,
        data_root: Path,
        *,
        threshold: float = 0.72,
        top_k: int = 3,
    ) -> None:
        self.paths = SelectionRoiPaths.from_root(data_root)
        self.paths.ensure_runtime_directories()
        self._threshold = threshold
        self._top_k = top_k
        self._index = ReferenceImageIndex(self.paths.labeled_root)
        self._lock = threading.RLock()
        self._config_signature: tuple[int, int] | None = None
        self._config: SelectionRoiConfig | None = None
        self._observations: dict[str, StoredSelectionObservation] = {}
        self._last_distinct_observation: StoredSelectionObservation | None = None
        self._near_duplicate_suppressed_count = 0

    def process_frame(self, frame: FramePacket) -> SelectionMatchBundle:
        with self._lock:
            if (
                frame.frame_kind is not FrameKind.CANONICAL
                or frame.width != CANONICAL_FRAME_WIDTH
                or frame.height != CANONICAL_FRAME_HEIGHT
                or not isinstance(frame.image, QImage)
                or frame.image.isNull()
            ):
                return self._empty_bundle(
                    status="FRAME_NOT_CANONICAL",
                    message="選出ROIは1280x720 canonical frameのみ使用します。",
                    frame_id=frame.frame_id,
                )
            try:
                config = self._load_config()
                crops = self._crop_frame(frame.image, config)
                matches = match_selection_crops(
                    crops,
                    self._index,
                    threshold=self._threshold,
                    top_k=self._top_k,
                )
                observation = self._store_observation(
                    frame=frame,
                    matches=matches,
                    config=config,
                )
            except SelectionRoiError as error:
                return self._empty_bundle(
                    status="ROI_UNAVAILABLE",
                    message=str(error),
                    frame_id=frame.frame_id,
                )
            except OSError:
                return self._empty_bundle(
                    status="ROI_STORAGE_FAILED",
                    message="選出ROI候補を保存できません。手入力で続行できます。",
                    frame_id=frame.frame_id,
                )
            return SelectionMatchBundle(
                status="CANDIDATES_READY",
                operator_message=(
                    "相手6枠の画像候補です。自動反映・自動確定は行いません。"
                ),
                frame_id=frame.frame_id,
                observation_id=observation.observation_id,
                slots=matches,
                reference_count=self._index.reference_count,
                roi_config_provenance=config.source_provenance,
            )

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "distinct_observation_count": len(self._observations),
                "near_duplicate_suppressed_count": self._near_duplicate_suppressed_count,
            }

    def confirm_observation(
        self,
        *,
        observation_id: str,
        opponent_names: tuple[str, str, str, str, str, str],
        reviewed_selection_id: str | None,
    ) -> FeedbackStoreResult:
        with self._lock:
            observation = self._observations.get(observation_id)
            if observation is None or reviewed_selection_id is None:
                return FeedbackStoreResult(added_count=0, duplicate_count=0, conflict_count=0)
            added = 0
            duplicates = 0
            conflicts = 0
            feedback_rows: list[dict[str, object]] = []
            for slot, source_path, crop_hash, label in zip(
                range(1, SELECTION_SLOT_COUNT + 1),
                observation.slot_files,
                observation.slot_hashes,
                opponent_names,
                strict=True,
            ):
                safe_label = safe_label_directory(label)
                existing_labels = self._labels_for_hash(crop_hash)
                if existing_labels and safe_label not in existing_labels:
                    conflicts += 1
                    conflict_dir = self.paths.quarantine_root / "label_conflicts"
                    conflict_dir.mkdir(parents=True, exist_ok=True)
                    conflict_path = conflict_dir / f"{crop_hash}_slot_{slot:02d}.png"
                    if not conflict_path.exists():
                        shutil.copy2(source_path, conflict_path)
                    disposition = "CONFLICT"
                else:
                    label_dir = self.paths.labeled_root / safe_label
                    label_dir.mkdir(parents=True, exist_ok=True)
                    destination = label_dir / f"{crop_hash}.png"
                    if destination.exists():
                        duplicates += 1
                        disposition = "DUPLICATE"
                    else:
                        shutil.copy2(source_path, destination)
                        added += 1
                        disposition = "ADDED"
                feedback_rows.append(
                    {
                        "schema_version": "maple-selection-roi-feedback.v1",
                        "reviewed_selection_id": reviewed_selection_id,
                        "observation_id": observation_id,
                        "frame_id": observation.frame_id,
                        "slot": slot,
                        "label": label,
                        "safe_label_directory": safe_label,
                        "crop_hash": crop_hash,
                        "disposition": disposition,
                        "recorded_at_utc": datetime.now(UTC).isoformat(),
                    }
                )
            self._append_jsonl(self.paths.feedback_file, feedback_rows)
            self._index.refresh()
            return FeedbackStoreResult(
                added_count=added,
                duplicate_count=duplicates,
                conflict_count=conflicts,
            )

    def latest_observation(self, observation_id: str) -> StoredSelectionObservation | None:
        with self._lock:
            return self._observations.get(observation_id)

    def _load_config(self) -> SelectionRoiConfig:
        try:
            stat = self.paths.config_file.stat()
        except OSError as error:
            raise SelectionRoiError(
                "選出ROI設定がありません。手入力で続行できます。"
            ) from error
        signature = (stat.st_size, stat.st_mtime_ns)
        if signature != self._config_signature or self._config is None:
            self._config = SelectionRoiConfig.load(self.paths.config_file)
            self._config_signature = signature
        return self._config

    @staticmethod
    def _crop_frame(
        image: QImage,
        config: SelectionRoiConfig,
    ) -> tuple[SelectionRoiCrop, ...]:
        if image.width() != CANONICAL_FRAME_WIDTH or image.height() != CANONICAL_FRAME_HEIGHT:
            raise SelectionRoiError("選出ROI source image must be 1280x720")
        crops: list[SelectionRoiCrop] = []
        for rect in config.slots:
            crop = image.copy(rect.x, rect.y, rect.width, rect.height)
            if crop.isNull() or crop.width() != rect.width or crop.height() != rect.height:
                raise SelectionRoiError("選出ROI crop failed")
            crops.append(SelectionRoiCrop(slot=rect.slot, image=crop, rect=rect))
        return tuple(crops)

    def _store_observation(
        self,
        *,
        frame: FramePacket,
        matches: tuple[SelectionSlotMatch, ...],
        config: SelectionRoiConfig,
    ) -> StoredSelectionObservation:
        fingerprints = tuple(ImageFingerprint.from_image(match.crop) for match in matches)
        previous = self._last_distinct_observation
        if previous is not None and self._near_duplicate(
            fingerprints,
            previous.fingerprints,
        ):
            self._near_duplicate_suppressed_count += 1
            return previous

        observation_id = self._observation_id(fingerprints)
        existing = self._observations.get(observation_id)
        if existing is not None:
            self._last_distinct_observation = existing
            return existing

        observation_dir = self.paths.captures_root / observation_id
        observation_dir.mkdir(parents=True, exist_ok=True)
        slot_files: list[Path] = []
        for match in matches:
            destination = observation_dir / f"slot_{match.slot:02d}.png"
            if not destination.exists():
                self._save_png_atomic(match.crop, destination)
            slot_files.append(destination)
        observation = StoredSelectionObservation(
            observation_id=observation_id,
            frame_id=frame.frame_id,
            captured_at_utc=frame.captured_at_utc,
            slot_files=tuple(slot_files),
            slot_hashes=tuple(item.exact_hash for item in fingerprints),
            fingerprints=fingerprints,
            matches=matches,
        )
        self._observations[observation_id] = observation
        self._last_distinct_observation = observation
        manifest_marker = observation_dir / ".manifest-recorded"
        if not manifest_marker.exists():
            self._append_jsonl(
                self.paths.manifest_file,
                [
                    {
                        "schema_version": "maple-selection-roi-manifest.v1",
                        "observation_id": observation_id,
                        "frame_id": frame.frame_id,
                        "captured_at_utc": frame.captured_at_utc.isoformat(),
                        "roi_config_schema": config.schema_version,
                        "roi_config_provenance": config.source_provenance,
                        "slot_hashes": list(observation.slot_hashes),
                        "assigned_labels": [match.assigned_label for match in matches],
                        "assigned_scores": [
                            round(match.assigned_score, 6) for match in matches
                        ],
                    }
                ],
            )
            manifest_marker.write_text("recorded\n", encoding="utf-8")
        return observation

    @staticmethod
    def _near_duplicate(
        current: tuple[ImageFingerprint, ...],
        previous: tuple[ImageFingerprint, ...],
    ) -> bool:
        if len(current) != SELECTION_SLOT_COUNT or len(previous) != SELECTION_SLOT_COUNT:
            return False
        return all(
            fingerprint_similarity(current_item, previous_item)
            >= _NEAR_DUPLICATE_THRESHOLD
            for current_item, previous_item in zip(current, previous, strict=True)
        )

    @staticmethod
    def _observation_id(
        fingerprints: tuple[ImageFingerprint, ...],
    ) -> str:
        if len(fingerprints) != SELECTION_SLOT_COUNT:
            raise SelectionRoiError("selection observation must contain six crops")
        joined = "|".join(item.exact_hash for item in fingerprints)
        return hashlib.sha256(joined.encode("ascii")).hexdigest()

    def _labels_for_hash(self, crop_hash: str) -> set[str]:
        labels: set[str] = set()
        for candidate in self.paths.labeled_root.glob(f"*/{crop_hash}.png"):
            if candidate.is_file():
                labels.add(candidate.parent.name)
        return labels

    @staticmethod
    def _save_png_atomic(image: QImage, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp.png")
        if not image.save(str(temporary), b"PNG"):
            raise OSError("image save failed")
        temporary.replace(destination)

    @staticmethod
    def _append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")

    def _empty_bundle(
        self,
        *,
        status: str,
        message: str,
        frame_id: str | None,
    ) -> SelectionMatchBundle:
        return SelectionMatchBundle(
            status=status,
            operator_message=message,
            frame_id=frame_id,
            observation_id=None,
            slots=(),
            reference_count=self._index.reference_count,
            roi_config_provenance="",
        )
