"""Canonical enumerations."""

from enum import StrEnum


class BattleState(StrEnum):
    SELECTION_OPEN = "SELECTION_OPEN"
    SELECTION_ADVICE_READY = "SELECTION_ADVICE_READY"
    BATTLE_READY = "BATTLE_READY"
    TURN_CAPTURE_PENDING = "TURN_CAPTURE_PENDING"
    TURN_REVIEW_REQUIRED = "TURN_REVIEW_REQUIRED"
    TURN_REVIEWED = "TURN_REVIEWED"
    TURN_RECORDED = "TURN_RECORDED"
    MATCH_ENDED = "MATCH_ENDED"
    MATCH_EXPORTED = "MATCH_EXPORTED"
    ABORTED = "ABORTED"


class MatchOutcome(StrEnum):
    WIN = "WIN"
    LOSE = "LOSE"


class HpBucket(StrEnum):
    ZERO = "0"
    ONE_TO_TEN = "1-10"
    ELEVEN_TO_TWENTY = "11-20"
    TWENTY_ONE_TO_THIRTY = "21-30"
    THIRTY_ONE_TO_FORTY = "31-40"
    FORTY_ONE_TO_FIFTY = "41-50"
    FIFTY_ONE_TO_SIXTY = "51-60"
    SIXTY_ONE_TO_SEVENTY = "61-70"
    SEVENTY_ONE_TO_EIGHTY = "71-80"
    EIGHTY_ONE_TO_NINETY = "81-90"
    NINETY_ONE_TO_NINETY_NINE = "91-99"
    FULL = "100"
    UNKNOWN = "UNKNOWN"


class ActionType(StrEnum):
    MOVE = "MOVE"
    SWITCH = "SWITCH"


class ActionOrder(StrEnum):
    SELF_FIRST = "SELF_FIRST"
    OPPONENT_FIRST = "OPPONENT_FIRST"
    UNKNOWN = "UNKNOWN"


class JobType(StrEnum):
    CAPTURE = "CAPTURE"
    OCR = "OCR"
    SELECTION_ADVICE = "SELECTION_ADVICE"
    TURN_ADVICE = "TURN_ADVICE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.TIMED_OUT,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.DELIVERY_UNKNOWN,
        }


class ResultDisposition(StrEnum):
    APPLIED = "APPLIED"
    STALE_REJECTED = "STALE_REJECTED"
    INVALID_REJECTED = "INVALID_REJECTED"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
