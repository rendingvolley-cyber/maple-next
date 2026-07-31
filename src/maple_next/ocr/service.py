"""OCR candidate orchestration.

Owns: frame-freshness gating before OCR is attempted, converting backend
exceptions into sanitized bundles, and stamping every candidate with its
source frame id.

Absolutely must NEVER: write to any repository, call a domain/application
command, send anything to a provider/network, or execute any automatic game
action. There is no code path in this module that reaches those things - it
only ever returns an OcrCandidateBundle value.
"""

from __future__ import annotations

from datetime import datetime

from maple_next.capture.contracts import CaptureStatus, FramePacket
from maple_next.ocr.contracts import (
    LOW_CONFIDENCE_THRESHOLD,
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateBackend,
    OcrCandidateBundle,
    OcrCandidateContext,
    OcrErrorCode,
    sanitized_ocr_message,
)


class OcrCandidateService:
    """Loosely coupled orchestrator around an OcrCandidateBackend."""

    def __init__(self, backend: OcrCandidateBackend) -> None:
        self._backend = backend

    def request_candidates(
        self,
        *,
        frame: FramePacket | None,
        frame_age_ms: int | None,
        fresh: bool,
        context: OcrCandidateContext,
    ) -> OcrCandidateBundle:
        """Produce a candidate bundle for the given (already-fetched) frame.

        Callers own frame retrieval (typically via a CaptureService) so this
        module never has to reach into capture internals.
        """

        if frame is None:
            return self._bundle(
                OcrBundleStatus.FRAME_UNAVAILABLE,
                frame_id=None,
                frame_captured_at_utc=None,
                frame_age_ms=None,
                candidates=(),
                error_code=None,
            )
        if not fresh:
            return self._bundle(
                OcrBundleStatus.FRAME_STALE,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                frame_age_ms=frame_age_ms,
                candidates=(),
                error_code=None,
            )

        try:
            available = self._backend.is_available()
        except Exception:  # noqa: BLE001 - backend must never crash the app
            available = False

        if not available:
            return self._bundle(
                OcrBundleStatus.OCR_UNAVAILABLE,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                frame_age_ms=frame_age_ms,
                candidates=(),
                error_code=OcrErrorCode.OCR_UNAVAILABLE,
            )

        try:
            candidates = self._backend.generate_candidates(frame, context)
        except Exception:  # noqa: BLE001 - never leak raw backend exceptions
            return self._bundle(
                OcrBundleStatus.OCR_FAILED,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                frame_age_ms=frame_age_ms,
                candidates=(),
                error_code=OcrErrorCode.OCR_FAILED,
            )

        if not candidates:
            return self._bundle(
                OcrBundleStatus.NO_CANDIDATES,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                frame_age_ms=frame_age_ms,
                candidates=(),
                error_code=OcrErrorCode.OCR_NO_CANDIDATES,
            )

        best_confidence = max(candidate.confidence for candidate in candidates)
        if best_confidence < LOW_CONFIDENCE_THRESHOLD:
            return self._bundle(
                OcrBundleStatus.LOW_CONFIDENCE,
                frame_id=frame.frame_id,
                frame_captured_at_utc=frame.captured_at_utc,
                frame_age_ms=frame_age_ms,
                candidates=candidates,
                error_code=OcrErrorCode.OCR_LOW_CONFIDENCE,
            )

        return self._bundle(
            OcrBundleStatus.CANDIDATES_READY,
            frame_id=frame.frame_id,
            frame_captured_at_utc=frame.captured_at_utc,
            frame_age_ms=frame_age_ms,
            candidates=candidates,
            error_code=None,
        )

    def request_candidates_from_capture_status(
        self,
        *,
        capture_status: CaptureStatus,
        frame: FramePacket | None,
        context: OcrCandidateContext,
    ) -> OcrCandidateBundle:
        """Convenience wrapper that derives freshness from a CaptureStatus."""

        return self.request_candidates(
            frame=frame,
            frame_age_ms=capture_status.age_ms,
            fresh=capture_status.fresh,
            context=context,
        )

    @staticmethod
    def _bundle(
        status: str,
        *,
        frame_id: str | None,
        frame_captured_at_utc: datetime | None,
        frame_age_ms: int | None,
        candidates: tuple[OcrCandidate, ...],
        error_code: str | None,
    ) -> OcrCandidateBundle:
        return OcrCandidateBundle(
            status=status,
            candidate_only=True,
            manual_entry_allowed=True,
            frame_id=frame_id,
            frame_captured_at_utc=frame_captured_at_utc,
            frame_age_ms=frame_age_ms,
            candidates=candidates,
            error_code=error_code,
            operator_message=sanitized_ocr_message(status, error_code),
        )


class UnavailableOcrCandidateBackend:
    """Production-safe fallback when no real OCR backend is wired in.

    Always reports unavailable; never attempts recognition. Exists so the
    service always has something concrete to depend on even before Lane 02
    plugs in a donor OCR implementation.
    """

    def is_available(self) -> bool:
        return False

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        return ()
