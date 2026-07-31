"""Candidate-only OCR package.

OCR output is never canonical. Every OcrCandidateBundle carries
candidate_only=True and manual_entry_allowed=True, and nothing in this
package writes to a repository, calls a domain command, or sends anything
over a network.

Public surface for a future integrator (Lane 02):

    OcrCandidateService(backend).request_candidates(...) -> OcrCandidateBundle
"""

from maple_next.ocr.contracts import (
    LOW_CONFIDENCE_THRESHOLD,
    OCR_CANDIDATE_SOURCE,
    OCR_OPERATOR_MESSAGES,
    OcrBundleStatus,
    OcrCandidate,
    OcrCandidateBackend,
    OcrCandidateBundle,
    OcrCandidateContext,
    OcrErrorCode,
    OcrFieldKey,
    hp_bucket_values,
)
from maple_next.ocr.service import OcrCandidateService, UnavailableOcrCandidateBackend

__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "OCR_CANDIDATE_SOURCE",
    "OCR_OPERATOR_MESSAGES",
    "OcrBundleStatus",
    "OcrCandidate",
    "OcrCandidateBackend",
    "OcrCandidateBundle",
    "OcrCandidateContext",
    "OcrCandidateService",
    "OcrErrorCode",
    "OcrFieldKey",
    "UnavailableOcrCandidateBackend",
    "hp_bucket_values",
]
