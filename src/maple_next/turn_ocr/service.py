"""Pure processing of one immutable Turn screenshot into human-review candidates."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtGui import QImage

from maple_next.capture.contracts import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    FrameKind,
)
from maple_next.ocr.contracts import (
    LOW_CONFIDENCE_THRESHOLD,
    OCR_CANDIDATE_SOURCE,
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateBundle,
    OcrErrorCode,
)
from maple_next.turn_ocr.contracts import (
    TURN_SNAPSHOT_DISPLAY_CONFIDENCE,
    TurnRoiConfig,
    TurnRoiRect,
    TurnSnapshotRequest,
    TurnSnapshotResult,
    TurnSnapshotStatus,
)
from maple_next.turn_ocr.hp_reader import read_hp_bar
from maple_next.turn_ocr.name_recognizer import recognize_candidate_name


def _crop(image: QImage, rect: TurnRoiRect) -> QImage:
    return image.copy(rect.x, rect.y, rect.width, rect.height)


def _contrast_span(image: QImage) -> int:
    if image.isNull():
        return 0
    grayscale = image.convertToFormat(QImage.Format.Format_Grayscale8)
    minimum = 255
    maximum = 0
    step_x = max(1, grayscale.width() // 48)
    step_y = max(1, grayscale.height() // 16)
    for y in range(0, grayscale.height(), step_y):
        for x in range(0, grayscale.width(), step_x):
            value = grayscale.pixelColor(x, y).red()
            minimum = min(minimum, value)
            maximum = max(maximum, value)
    return maximum - minimum


def _bundle(
    *,
    status: str,
    frame_id: str | None,
    frame_captured_at_utc: datetime | None,
    candidates: tuple[OcrCandidate, ...],
    error_code: str | None,
    operator_message: str,
) -> OcrCandidateBundle:
    return OcrCandidateBundle(
        status=status,
        candidate_only=True,
        manual_entry_allowed=True,
        frame_id=frame_id,
        frame_captured_at_utc=frame_captured_at_utc,
        frame_age_ms=0,
        candidates=candidates,
        error_code=error_code,
        operator_message=operator_message,
    )


class TurnSnapshotOcrService:
    """Candidate-only OCR service for exactly one frozen Turn frame."""

    def __init__(self, config: TurnRoiConfig) -> None:
        self._config = config

    @property
    def roi_config_provenance(self) -> str:
        suffix = "provisional" if self._config.provisional else "calibrated"
        return f"{self._config.source_path.name}:{suffix}"

    def process(self, request: TurnSnapshotRequest) -> TurnSnapshotResult:
        frame = request.frame
        image = frame.image
        if (
            frame.frame_kind is not FrameKind.CANONICAL
            or frame.width != CANONICAL_FRAME_WIDTH
            or frame.height != CANONICAL_FRAME_HEIGHT
            or not isinstance(image, QImage)
            or image.isNull()
        ):
            bundle = _bundle(
                status=OcrBundleStatus.FRAME_NOT_CANONICAL,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                candidates=(),
                error_code=None,
                operator_message=(
                    "Turn OCR用フレームが1280x720 canonical imageではありません。"
                    "手動入力で続行できます。"
                ),
            )
            return TurnSnapshotResult(
                identity=request.identity,
                status=TurnSnapshotStatus.FRAME_NOT_CANONICAL,
                bundle=bundle,
                frozen_image=QImage(),
                crops={},
                operator_message=bundle.operator_message or "",
                roi_config_provenance=self.roi_config_provenance,
            )

        frozen = image.copy()
        if frame.content_rect is not None:
            content_x, content_y, content_width, content_height = frame.content_rect
            content_right = content_x + content_width
            content_bottom = content_y + content_height
            for rect in (
                self._config.self_active,
                self._config.opponent_active,
                self._config.self_hp,
                self._config.opponent_hp,
            ):
                if (
                    rect.x < content_x
                    or rect.y < content_y
                    or rect.x + rect.width > content_right
                    or rect.y + rect.height > content_bottom
                ):
                    bundle = _bundle(
                        status=TurnSnapshotStatus.SCENE_NOT_READY,
                        frame_id=frame.frame_id,
                        frame_captured_at_utc=frame.captured_at_utc,
                        candidates=(),
                        error_code=None,
                        operator_message=(
                            "Turn ROIが実映像領域外にあるため解析しません。"
                            "手動入力で続行できます。"
                        ),
                    )
                    return TurnSnapshotResult(
                        identity=request.identity,
                        status=TurnSnapshotStatus.SCENE_NOT_READY,
                        bundle=bundle,
                        frozen_image=frozen,
                        crops={},
                        operator_message=bundle.operator_message or "",
                        roi_config_provenance=self.roi_config_provenance,
                    )
        crops = {
            "self_active": _crop(frozen, self._config.self_active),
            "opponent_active": _crop(frozen, self._config.opponent_active),
            "self_hp": _crop(frozen, self._config.self_hp),
            "opponent_hp": _crop(frozen, self._config.opponent_hp),
        }
        self_hp_estimate = read_hp_bar(crops["self_hp"])
        opponent_hp_estimate = read_hp_bar(crops["opponent_hp"])
        name_regions_ready = (
            _contrast_span(crops["self_active"]) >= 7
            and _contrast_span(crops["opponent_active"]) >= 7
        )
        hp_regions_ready = (
            self_hp_estimate.detected or _contrast_span(crops["self_hp"]) >= 7
        ) and (
            opponent_hp_estimate.detected
            or _contrast_span(crops["opponent_hp"]) >= 7
        )
        if not name_regions_ready or not hp_regions_ready:
            bundle = _bundle(
                status=TurnSnapshotStatus.SCENE_NOT_READY,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                candidates=(),
                error_code=None,
                operator_message=(
                    "Turn用の名前欄とHPバーを確認できません。固定画像を確認し、"
                    "必要なら明示的に撮り直すか手動入力してください。"
                ),
            )
            return TurnSnapshotResult(
                identity=request.identity,
                status=TurnSnapshotStatus.SCENE_NOT_READY,
                bundle=bundle,
                frozen_image=frozen,
                crops=crops,
                operator_message=bundle.operator_message or "",
                roi_config_provenance=self.roi_config_provenance,
            )

        candidates: list[OcrCandidate] = []
        for field_key, crop_key, names in (
            ("self_active", "self_active", request.self_active_candidates),
            ("opponent_active", "opponent_active", request.opponent_active_candidates),
        ):
            for match in recognize_candidate_name(crops[crop_key], names, top_k=3):
                candidates.append(
                    OcrCandidate(
                        field_key=field_key,
                        suggested_value=match.label,
                        raw_text="candidate-constrained-template-match",
                        confidence=match.score,
                        rank=match.rank,
                        reason="固定ROIと確認済み候補名テンプレートの画像類似度",
                        source_frame_id=frame.frame_id,
                        source=OCR_CANDIDATE_SOURCE,
                    )
                )

        for field_key, estimate in (
            ("self_hp", self_hp_estimate),
            ("opponent_hp", opponent_hp_estimate),
        ):
            candidates.append(
                OcrCandidate(
                    field_key=field_key,
                    suggested_value=estimate.bucket.value,
                    raw_text=(
                        "hp-bar-unavailable"
                        if estimate.ratio is None
                        else f"hp-ratio={estimate.ratio:.4f}"
                    ),
                    confidence=estimate.confidence,
                    rank=1,
                    reason="固定HPバーROIの連続充填率をcanonical bucketへ変換",
                    source_frame_id=frame.frame_id,
                    source=OCR_CANDIDATE_SOURCE,
                    raw_estimate=estimate.ratio,
                )
            )

        ordered = tuple(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.confidence >= TURN_SNAPSHOT_DISPLAY_CONFIDENCE
                ),
                key=lambda item: (item.field_key, item.rank),
            )
        )
        best_confidence = max((candidate.confidence for candidate in ordered), default=0.0)
        if not ordered:
            status = OcrBundleStatus.NO_CANDIDATES
            error_code: str | None = OcrErrorCode.OCR_NO_CANDIDATES
            message = "Turn OCR候補がありません。手動入力で続行できます。"
            result_status = TurnSnapshotStatus.OCR_UNAVAILABLE
        elif best_confidence < LOW_CONFIDENCE_THRESHOLD:
            status = OcrBundleStatus.LOW_CONFIDENCE
            error_code = OcrErrorCode.OCR_LOW_CONFIDENCE
            message = "Turn OCR候補の確信度が低いため自動入力しません。手動確認してください。"
            result_status = TurnSnapshotStatus.READY
        else:
            status = OcrBundleStatus.CANDIDATES_READY
            error_code = None
            message = "固定画像からTurn OCR候補を生成しました。人間確認前の候補です。"
            result_status = TurnSnapshotStatus.READY
        bundle = _bundle(
            status=status,
            frame_id=frame.frame_id,
            frame_captured_at_utc=frame.captured_at_utc,
            candidates=ordered,
            error_code=error_code,
            operator_message=message,
        )
        return TurnSnapshotResult(
            identity=request.identity,
            status=result_status,
            bundle=bundle,
            frozen_image=frozen,
            crops=crops,
            operator_message=message,
            roi_config_provenance=self.roi_config_provenance,
        )

    def failed_result(self, request: TurnSnapshotRequest) -> TurnSnapshotResult:
        frame = request.frame
        image = frame.image if isinstance(frame.image, QImage) else QImage()
        bundle = _bundle(
            status=OcrBundleStatus.OCR_FAILED,
            frame_id=frame.frame_id,
            frame_captured_at_utc=frame.captured_at_utc,
            candidates=(),
            error_code=OcrErrorCode.OCR_FAILED,
            operator_message="Turn OCR処理に失敗しました。手動入力で続行できます。",
        )
        return TurnSnapshotResult(
            identity=request.identity,
            status=TurnSnapshotStatus.OCR_FAILED,
            bundle=bundle,
            frozen_image=image.copy() if not image.isNull() else QImage(),
            crops={},
            operator_message=bundle.operator_message or "",
            roi_config_provenance=self.roi_config_provenance,
        )
