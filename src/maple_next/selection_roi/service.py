"""Selection ROI crop, matching, and origin-aware feedback storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeAlias, cast

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
    normalize_selection_label,
    safe_label_directory,
)
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.selection_roi.matcher import (
    ImageFingerprint,
    ReferenceImageIndex,
    fingerprint_similarity,
    match_selection_crops,
)

_CONFIG_RELATIVE_PATH: Final[Path] = Path("selection/config/roi_config.json")
_LABELED_RELATIVE_PATH: Final[Path] = Path("selection/reference/labeled")
_PROVISIONAL_RELATIVE_PATH: Final[Path] = Path("selection/reference/provisional")
_UNLABELED_RELATIVE_PATH: Final[Path] = Path("selection/reference/unlabeled")
_CAPTURES_RELATIVE_PATH: Final[Path] = Path("selection/captures")
_QUARANTINE_RELATIVE_PATH: Final[Path] = Path("selection/quarantine")
_FEEDBACK_RELATIVE_PATH: Final[Path] = Path("selection/feedback/selection_labels.jsonl")
_MANIFEST_RELATIVE_PATH: Final[Path] = Path(
    "selection/manifests/selection_roi_manifest.jsonl"
)
_NEAR_DUPLICATE_THRESHOLD: Final[float] = 0.995
_PROMOTION_MIN_DISTINCT_MATCHES: Final[int] = 3
_PROMOTION_TRUSTED_SIMILARITY: Final[float] = 0.85
_PROMOTION_MIN_LABEL_MARGIN: Final[float] = 0.05
_SUPPORTED_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
)
_VALID_PROVISIONAL_EVIDENCE_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {"ADDED_PROVISIONAL", "DUPLICATE"}
)


@dataclass(frozen=True, slots=True)
class SelectionRoiPaths:
    root: Path
    config_file: Path
    labeled_root: Path
    provisional_root: Path
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
            provisional_root=resolved / _PROVISIONAL_RELATIVE_PATH,
            unlabeled_root=resolved / _UNLABELED_RELATIVE_PATH,
            captures_root=resolved / _CAPTURES_RELATIVE_PATH,
            quarantine_root=resolved / _QUARANTINE_RELATIVE_PATH,
            feedback_file=resolved / _FEEDBACK_RELATIVE_PATH,
            manifest_file=resolved / _MANIFEST_RELATIVE_PATH,
        )

    def ensure_runtime_directories(self) -> None:
        for directory in (
            self.labeled_root,
            self.provisional_root,
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
class SelectionSlotFeedback:
    """Editable value and provenance at explicit Gemini-send time."""

    label: str
    value_origin: SelectionInputOrigin
    ocr_score: float | None = None


SelectionFeedbackTuple: TypeAlias = tuple[
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
    SelectionSlotFeedback,
]


@dataclass(frozen=True, slots=True)
class FeedbackStoreResult:
    added_count: int
    duplicate_count: int
    conflict_count: int
    provisional_count: int = 0
    promoted_count: int = 0


class SelectionRoiService:
    """Thread-safe candidate matcher and local feedback store.

    Candidate-chip and directly typed values enter the trusted index. Untouched
    OCR auto-fill values remain provisional and can be promoted only after
    repeated evidence from distinct matches agrees with an existing trusted
    label. Exact and perceptual conflicts across labels are quarantined.
    """

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
                    "相手6枠の画像候補です。0.80以上は空欄へ仮入力され、いつでも修正できます。"
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
        """Compatibility path for the legacy explicit confirmation hook."""

        feedback = cast(
            SelectionFeedbackTuple,
            tuple(
                SelectionSlotFeedback(
                    label=label,
                    value_origin=SelectionInputOrigin.MANUAL_TEXT,
                )
                for label in opponent_names
            ),
        )
        return self.record_sent_observation(
            observation_id=observation_id,
            slot_feedback=feedback,
            reviewed_selection_id=reviewed_selection_id,
            session_id=None,
            match_id=None,
            generation=None,
        )

    def record_sent_observation(
        self,
        *,
        observation_id: str,
        slot_feedback: SelectionFeedbackTuple,
        reviewed_selection_id: str | None,
        session_id: str | None,
        match_id: str | None,
        generation: int | None,
    ) -> FeedbackStoreResult:
        """Store six crops only after explicit send-time canonicalization."""

        with self._lock:
            observation = self._observations.get(observation_id)
            if observation is None or reviewed_selection_id is None:
                return FeedbackStoreResult(0, 0, 0)

            added = 0
            provisional = 0
            duplicates = 0
            conflicts = 0
            feedback_rows: list[dict[str, object]] = []
            promotion_labels: set[str] = set()

            for slot, source_path, crop_hash, fingerprint, item in zip(
                range(1, SELECTION_SLOT_COUNT + 1),
                observation.slot_files,
                observation.slot_hashes,
                observation.fingerprints,
                slot_feedback,
                strict=True,
            ):
                label = item.label.strip()
                safe_label = safe_label_directory(label)
                trust_state = (
                    "TRUSTED"
                    if item.value_origin
                    in {
                        SelectionInputOrigin.CANDIDATE_CLICK,
                        SelectionInputOrigin.MANUAL_TEXT,
                    }
                    else "PROVISIONAL"
                )

                matching_labels = self._labels_for_fingerprint(fingerprint)
                conflicting_labels = matching_labels - {safe_label}
                if conflicting_labels:
                    conflicts += 1
                    self._quarantine_conflict(source_path, crop_hash, slot)
                    disposition = "CONFLICT"
                elif safe_label in matching_labels:
                    duplicates += 1
                    disposition = "DUPLICATE"
                    if trust_state == "PROVISIONAL":
                        promotion_labels.add(safe_label)
                else:
                    target_root = (
                        self.paths.labeled_root
                        if trust_state == "TRUSTED"
                        else self.paths.provisional_root
                    )
                    label_dir = target_root / safe_label
                    label_dir.mkdir(parents=True, exist_ok=True)
                    destination = label_dir / f"{crop_hash}.png"
                    shutil.copy2(source_path, destination)
                    if trust_state == "TRUSTED":
                        added += 1
                        disposition = "ADDED_TRUSTED"
                    else:
                        provisional += 1
                        promotion_labels.add(safe_label)
                        disposition = "ADDED_PROVISIONAL"

                feedback_rows.append(
                    {
                        "schema_version": "maple-selection-roi-feedback.v2",
                        "reviewed_selection_id": reviewed_selection_id,
                        "session_id": session_id,
                        "match_id": match_id,
                        "generation": generation,
                        "observation_id": observation_id,
                        "frame_id": observation.frame_id,
                        "slot": slot,
                        "label": label,
                        "safe_label_directory": safe_label,
                        "crop_hash": crop_hash,
                        "value_origin": item.value_origin.value,
                        "ocr_score": item.ocr_score,
                        "trust_state": trust_state,
                        "disposition": disposition,
                        "conflicting_labels": sorted(conflicting_labels),
                        "recorded_at_utc": datetime.now(UTC).isoformat(),
                    }
                )

            self._append_jsonl(self.paths.feedback_file, feedback_rows)
            promoted = sum(
                self._promote_eligible_provisional(label)
                for label in sorted(promotion_labels)
            )
            self._index.refresh()
            return FeedbackStoreResult(
                added_count=added,
                duplicate_count=duplicates,
                conflict_count=conflicts,
                provisional_count=provisional,
                promoted_count=promoted,
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

    def _promote_eligible_provisional(self, safe_label: str) -> int:
        trusted_dir = self.paths.labeled_root / safe_label
        provisional_dir = self.paths.provisional_root / safe_label
        if not trusted_dir.is_dir() or not provisional_dir.is_dir():
            return 0
        distinct_matches = self._provisional_match_ids(safe_label)
        if len(distinct_matches) < _PROMOTION_MIN_DISTINCT_MATCHES:
            return 0

        promoted = 0
        promotion_rows: list[dict[str, object]] = []
        for source_path in sorted(provisional_dir.glob("*.png")):
            image = QImage(str(source_path))
            if image.isNull():
                continue
            try:
                fingerprint = ImageFingerprint.from_image(image)
            except SelectionRoiError:
                continue

            trusted_labels = self._labels_for_fingerprint(
                fingerprint,
                roots=(self.paths.labeled_root,),
            )
            if trusted_labels - {safe_label}:
                self._quarantine_conflict(
                    source_path,
                    fingerprint.exact_hash,
                    0,
                )
                continue
            if safe_label in trusted_labels:
                continue

            same_score, other_score = self._best_trusted_scores(
                fingerprint,
                safe_label,
            )
            if (
                same_score < _PROMOTION_TRUSTED_SIMILARITY
                or same_score - other_score < _PROMOTION_MIN_LABEL_MARGIN
            ):
                continue
            destination = trusted_dir / source_path.name
            shutil.copy2(source_path, destination)
            promoted += 1
            promotion_rows.append(
                {
                    "schema_version": "maple-selection-roi-feedback.v2",
                    "safe_label_directory": safe_label,
                    "crop_hash": fingerprint.exact_hash,
                    "trust_state": "TRUSTED",
                    "disposition": "PROMOTED_FROM_PROVISIONAL",
                    "same_label_similarity": round(same_score, 6),
                    "other_label_similarity": round(other_score, 6),
                    "distinct_match_count": len(distinct_matches),
                    "recorded_at_utc": datetime.now(UTC).isoformat(),
                }
            )
        if promotion_rows:
            self._append_jsonl(self.paths.feedback_file, promotion_rows)
        return promoted

    def _provisional_match_ids(self, safe_label: str) -> set[str]:
        if not self.paths.feedback_file.exists():
            return set()
        match_ids: set[str] = set()
        try:
            lines = self.paths.feedback_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("safe_label_directory") == safe_label
                and payload.get("trust_state") == "PROVISIONAL"
                and payload.get("disposition")
                in _VALID_PROVISIONAL_EVIDENCE_DISPOSITIONS
                and isinstance(payload.get("match_id"), str)
                and payload["match_id"]
            ):
                match_ids.add(str(payload["match_id"]))
        return match_ids

    def _best_trusted_scores(
        self,
        fingerprint: ImageFingerprint,
        safe_label: str,
    ) -> tuple[float, float]:
        same_score = 0.0
        other_score = 0.0
        for path in self._reference_paths((self.paths.labeled_root,)):
            image = QImage(str(path))
            if image.isNull():
                continue
            try:
                label = normalize_selection_label(path.parent.name)
                score = fingerprint_similarity(
                    fingerprint,
                    ImageFingerprint.from_image(image),
                )
            except SelectionRoiError:
                continue
            if label == safe_label:
                same_score = max(same_score, score)
            else:
                other_score = max(other_score, score)
        return same_score, other_score

    def _labels_for_fingerprint(
        self,
        fingerprint: ImageFingerprint,
        *,
        roots: tuple[Path, ...] | None = None,
    ) -> set[str]:
        labels: set[str] = set()
        selected_roots = roots or (
            self.paths.labeled_root,
            self.paths.provisional_root,
        )
        for path in self._reference_paths(selected_roots):
            image = QImage(str(path))
            if image.isNull():
                continue
            try:
                existing = ImageFingerprint.from_image(image)
                label = normalize_selection_label(path.parent.name)
            except SelectionRoiError:
                continue
            if (
                existing.exact_hash == fingerprint.exact_hash
                or fingerprint_similarity(fingerprint, existing)
                >= _NEAR_DUPLICATE_THRESHOLD
            ):
                labels.add(label)
        return labels

    @staticmethod
    def _reference_paths(roots: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(
            sorted(
                (
                    path
                    for root in roots
                    for path in root.glob("*/*")
                    if path.is_file()
                    and path.suffix.casefold() in _SUPPORTED_IMAGE_SUFFIXES
                ),
                key=lambda item: item.as_posix().casefold(),
            )
        )

    def _quarantine_conflict(self, source_path: Path, crop_hash: str, slot: int) -> None:
        conflict_dir = self.paths.quarantine_root / "label_conflicts"
        conflict_dir.mkdir(parents=True, exist_ok=True)
        slot_suffix = f"slot_{slot:02d}" if slot > 0 else "promotion"
        conflict_path = conflict_dir / f"{crop_hash}_{slot_suffix}.png"
        if not conflict_path.exists():
            shutil.copy2(source_path, conflict_path)

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

    @staticmethod
    def _save_png_atomic(image: QImage, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp.png")
        if not image.save(str(temporary)):
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
