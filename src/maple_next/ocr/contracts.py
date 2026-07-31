"""OCR-side contracts: candidate-only types and backend Protocol.

Everything produced here is a *candidate* for a human to confirm or reject.
Nothing in this module (or in ocr/service.py) may write to a repository, call
a domain command, or otherwise treat OCR output as canonical. See
docs in ocr/service.py for the enforcement of that rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from maple_next.capture.contracts import FramePacket
from maple_next.domain.enums import HpBucket

#: Fixed candidate source tag. Never rename to "accepted"/"canonical" - OCR
#: output is never either of those things.
OCR_CANDIDATE_SOURCE = "OCR_CANDIDATE"


class OcrFieldKey(StrEnum):
    """Known turn-fact fields an OCR candidate may target.

    Kept generic/extensible per spec, but only these four are populated by
    this candidate; no per-Pokemon-name special casing is implemented.
    """

    SELF_ACTIVE = "self_active"
    OPPONENT_ACTIVE = "opponent_active"
    SELF_HP = "self_hp"
    OPPONENT_HP = "opponent_hp"


class OcrBundleStatus(StrEnum):
    CANDIDATES_READY = "CANDIDATES_READY"
    NO_CANDIDATES = "NO_CANDIDATES"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_FAILED = "OCR_FAILED"
    FRAME_UNAVAILABLE = "FRAME_UNAVAILABLE"
    FRAME_STALE = "FRAME_STALE"


class OcrErrorCode(StrEnum):
    """Fixed set of sanitized OCR error codes."""

    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_NO_CANDIDATES = "OCR_NO_CANDIDATES"
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"
    OCR_FAILED = "OCR_FAILED"


OCR_OPERATOR_MESSAGES: dict[str, str] = {
    OcrErrorCode.OCR_UNAVAILABLE: "OCR機能を利用できません。手動入力で続行できます。",
    OcrErrorCode.OCR_NO_CANDIDATES: "OCR候補がありません。手動入力で続行できます。",
    OcrErrorCode.OCR_LOW_CONFIDENCE: (
        "OCR候補の確信度が低いため採用しません。手動入力で続行できます。"
    ),
    OcrErrorCode.OCR_FAILED: "OCR処理でエラーが発生しました。手動入力で続行できます。",
}

#: Sanitized messages for non-error statuses that still need operator text.
OCR_STATUS_MESSAGES: dict[str, str] = {
    OcrBundleStatus.FRAME_UNAVAILABLE: (
        "映像フレームが無いためOCR候補を生成できません。手動入力で続行できます。"
    ),
    OcrBundleStatus.FRAME_STALE: (
        "最新フレームが古いためOCR候補を使用しません。手動入力で続行できます。"
    ),
}


def sanitized_ocr_message(status: str, error_code: str | None) -> str | None:
    if error_code is not None:
        return OCR_OPERATOR_MESSAGES.get(
            error_code, "OCR処理でエラーが発生しました。手動入力で続行できます。"
        )
    return OCR_STATUS_MESSAGES.get(status)


#: Minimum confidence for a candidate to count toward CANDIDATES_READY instead
#: of LOW_CONFIDENCE. This is a safety gate, not an accuracy tuning knob.
LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class OcrCandidate:
    """A single, non-canonical suggestion for one turn-fact field.

    raw_estimate carries an optional unrounded measurement (e.g. an HP bar
    ratio) alongside the bucketed suggested_value; it is never itself treated
    as an exact canonical value.
    """

    field_key: str
    suggested_value: str
    raw_text: str
    confidence: float
    rank: int
    reason: str
    source_frame_id: str
    source: str = OCR_CANDIDATE_SOURCE
    raw_estimate: float | None = None


@dataclass(frozen=True, slots=True)
class OcrCandidateBundle:
    status: str
    candidate_only: bool
    manual_entry_allowed: bool
    frame_id: str | None
    frame_captured_at_utc: datetime | None
    frame_age_ms: int | None
    candidates: tuple[OcrCandidate, ...]
    error_code: str | None
    operator_message: str | None


@dataclass(frozen=True, slots=True)
class OcrCandidateContext:
    """Caller-provided context that bounds what OCR is allowed to suggest.

    self_active_candidates must be exactly the 3 already-APPLIED mons for a
    self-active suggestion to be produced at all - no fallback to a full
    6-mon party, and no identity auto-adoption when this isn't exactly 3.
    opponent_active_candidates, when present, are the human-confirmed
    opponent candidate pool; when absent, no opponent name candidate/
    auto-confirmation is produced.
    """

    self_active_candidates: tuple[str, ...] = ()
    opponent_active_candidates: tuple[str, ...] = ()


def hp_bucket_values() -> tuple[str, ...]:
    """Exact, unmodified HpBucket value set - reused as-is, never redefined."""

    return tuple(bucket.value for bucket in HpBucket)


class OcrCandidateBackend(Protocol):
    """Dependency-inversion seam for candidate generation.

    Implementations must never raise for "no result" cases - return an empty
    tuple instead. The service still guards against unexpected exceptions and
    converts them into a sanitized OCR_FAILED bundle.
    """

    def is_available(self) -> bool: ...

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]: ...
