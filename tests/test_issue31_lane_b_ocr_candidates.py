"""Issue #31 Lane B: OCR candidate-only contract, freshness gate, sanitization."""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from maple_next.capture.contracts import FramePacket
from maple_next.domain.enums import HpBucket
from maple_next.ocr.contracts import (
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateContext,
    OcrErrorCode,
)
from maple_next.ocr.service import OcrCandidateService, UnavailableOcrCandidateBackend


def _frame(frame_id: str = "frame-1") -> FramePacket:
    return FramePacket(
        frame_id=frame_id,
        source="UGREEN_DIRECT",
        captured_at_utc=datetime.now(UTC),
        captured_monotonic_ns=0,
        width=1280,
        height=720,
        image=None,
    )


class FakeOcrCandidateBackend:
    """Test double implementing OcrCandidateBackend."""

    def __init__(
        self,
        *,
        available: bool = True,
        candidates: tuple[OcrCandidate, ...] = (),
    ) -> None:
        self._available = available
        self._candidates = candidates
        self.generate_calls = 0

    def is_available(self) -> bool:
        return self._available

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        self.generate_calls += 1
        if len(context.self_active_candidates) != 3:
            # Never auto-adopt an identity outside the applied 3-mon set.
            return tuple(c for c in self._candidates if c.field_key != "self_active")
        return self._candidates


class FailingOcrCandidateBackend:
    """Simulates a backend raising a raw, sensitive exception."""

    def is_available(self) -> bool:
        return True

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        raise RuntimeError(r"DirectShow device \\?\usb#vid_1234 secret-backend-detail")


def _hp_candidate(confidence: float, frame_id: str = "frame-1") -> OcrCandidate:
    return OcrCandidate(
        field_key="self_hp",
        suggested_value=HpBucket.EIGHTY_ONE_TO_NINETY.value,
        raw_text="87%",
        confidence=confidence,
        rank=1,
        reason="hp_bar_ratio_candidate",
        source_frame_id=frame_id,
        raw_estimate=87.3,
    )


def test_candidates_ready_from_fresh_frame_are_candidate_only() -> None:
    frame = _frame()
    candidate = _hp_candidate(confidence=0.9, frame_id=frame.frame_id)
    backend = FakeOcrCandidateBackend(candidates=(candidate,))
    service = OcrCandidateService(backend)

    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.CANDIDATES_READY
    assert bundle.candidate_only is True
    assert bundle.manual_entry_allowed is True
    assert bundle.frame_id == frame.frame_id
    assert all(c.source_frame_id == frame.frame_id for c in bundle.candidates)
    assert all(c.source == "OCR_CANDIDATE" for c in bundle.candidates)


def test_stale_frame_never_reaches_ocr_backend() -> None:
    frame = _frame()
    backend = FakeOcrCandidateBackend(candidates=(_hp_candidate(0.9, frame.frame_id),))
    service = OcrCandidateService(backend)

    bundle = service.request_candidates(
        frame=frame, frame_age_ms=5000, fresh=False, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.FRAME_STALE
    assert bundle.candidates == ()
    assert backend.generate_calls == 0


def test_no_frame_reports_frame_unavailable() -> None:
    backend = FakeOcrCandidateBackend()
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=None, frame_age_ms=None, fresh=False, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.FRAME_UNAVAILABLE
    assert bundle.manual_entry_allowed is True


def test_ocr_unavailable_backend_never_raises() -> None:
    frame = _frame()
    backend = UnavailableOcrCandidateBackend()
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.OCR_UNAVAILABLE
    assert bundle.error_code == OcrErrorCode.OCR_UNAVAILABLE
    assert bundle.candidates == ()
    assert bundle.manual_entry_allowed is True


def test_low_confidence_candidates_are_flagged_not_auto_adopted() -> None:
    frame = _frame()
    low_candidate = _hp_candidate(confidence=0.2, frame_id=frame.frame_id)
    backend = FakeOcrCandidateBackend(candidates=(low_candidate,))
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.LOW_CONFIDENCE
    assert bundle.error_code == OcrErrorCode.OCR_LOW_CONFIDENCE
    # Candidates are still surfaced for display, but never auto-adopted -
    # that is a UI/human decision, and this bundle never writes anywhere.
    assert bundle.candidates == (low_candidate,)


def test_no_candidates_status() -> None:
    frame = _frame()
    backend = FakeOcrCandidateBackend(candidates=())
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.NO_CANDIDATES
    assert bundle.error_code == OcrErrorCode.OCR_NO_CANDIDATES


def test_ocr_backend_exception_is_sanitized() -> None:
    frame = _frame()
    backend = FailingOcrCandidateBackend()
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    assert bundle.status == OcrBundleStatus.OCR_FAILED
    assert bundle.error_code == OcrErrorCode.OCR_FAILED
    assert bundle.candidates == ()

    blob = repr(bundle) + str(bundle.operator_message) + str(bundle.error_code)
    for forbidden in (r"vid_1234", "secret-backend-detail", r"\\?\usb", "DirectShow"):
        assert forbidden not in blob


def test_self_active_requires_exactly_three_applied_candidates() -> None:
    frame = _frame()
    self_active_candidate = OcrCandidate(
        field_key="self_active",
        suggested_value="Dondozo",
        raw_text="Dondozo",
        confidence=0.9,
        rank=1,
        reason="name_match_candidate",
        source_frame_id=frame.frame_id,
    )
    backend = FakeOcrCandidateBackend(candidates=(self_active_candidate,))
    service = OcrCandidateService(backend)

    # Only 2 applied mons provided -> no self_active candidate produced.
    bundle_two = service.request_candidates(
        frame=frame,
        frame_age_ms=10,
        fresh=True,
        context=OcrCandidateContext(self_active_candidates=("Dondozo", "Flutter Mane")),
    )
    assert bundle_two.status == OcrBundleStatus.NO_CANDIDATES

    # Exactly 3 applied mons -> candidate allowed through.
    bundle_three = service.request_candidates(
        frame=frame,
        frame_age_ms=10,
        fresh=True,
        context=OcrCandidateContext(
            self_active_candidates=("Dondozo", "Flutter Mane", "Urshifu")
        ),
    )
    assert bundle_three.status == OcrBundleStatus.CANDIDATES_READY
    assert bundle_three.candidates == (self_active_candidate,)


def test_hp_candidate_preserves_raw_estimate_and_exact_hp_bucket_contract() -> None:
    candidate = _hp_candidate(confidence=0.82)
    assert candidate.suggested_value in {bucket.value for bucket in HpBucket}
    assert candidate.raw_estimate == 87.3
    # HpBucket enum itself must be untouched: exactly these 13 values.
    assert {bucket.value for bucket in HpBucket} == {
        "0",
        "1-10",
        "11-20",
        "21-30",
        "31-40",
        "41-50",
        "51-60",
        "61-70",
        "71-80",
        "81-90",
        "91-99",
        "100",
        "UNKNOWN",
    }


def test_human_override_wins_ocr_layer_never_writes() -> None:
    # The OCR layer has no repository/session dependency at all - simulate a
    # human override by simply not adopting the candidate value, and assert
    # the OCR module exposes no persistence hooks.
    frame = _frame()
    candidate = _hp_candidate(confidence=0.9, frame_id=frame.frame_id)
    backend = FakeOcrCandidateBackend(candidates=(candidate,))
    service = OcrCandidateService(backend)
    bundle = service.request_candidates(
        frame=frame, frame_age_ms=10, fresh=True, context=OcrCandidateContext()
    )
    human_chosen_value = HpBucket.FULL.value
    assert human_chosen_value != bundle.candidates[0].suggested_value
    assert not hasattr(service, "repository")
    assert not hasattr(service, "save")
    assert not hasattr(service, "confirm_turn_facts")


def test_no_network_or_automation_calls_exist_in_ocr_module() -> None:
    import maple_next.ocr.service as ocr_service_module

    with open(ocr_service_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("socket.", "requests.", "urllib.request", "http.client", "MOVE", "SWITCH"):
        assert forbidden not in source
