"""Shared lifecycle states for benchmark metadata records."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """Enum that serializes to its string value."""

    def __str__(self) -> str:
        return self.value


class CandidateStatus(StringEnum):
    """Lifecycle of a generated or edited image candidate."""

    CANDIDATE = "candidate"
    GENERATED = "generated"
    SCREENING = "screening"
    RETAINED_FAILURE = "retained_failure"
    REJECTED_CORRECT = "rejected_correct"
    HUMAN_REVIEW = "human_review"
    NEEDS_REPAIR = "needs_repair"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SampleStatus(StringEnum):
    """Lifecycle of a benchmark sample."""

    PENDING_EDIT = "pending_edit"
    CANDIDATE_READY = "candidate_ready"
    SCREENING = "screening"
    RETAINED_FAILURE = "retained_failure"
    REJECTED_CORRECT = "rejected_correct"
    HUMAN_REVIEW = "human_review"
    NEEDS_REPAIR = "needs_repair"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETIRED = "retired"
    DELETED = "deleted"


class ScreeningDecision(StringEnum):
    """Pressure-screening decision for a candidate."""

    RETAINED_FAILURE = "retained_failure"
    REJECTED_CORRECT = "rejected_correct"
    ERROR = "error"


class CampaignStatus(StringEnum):
    """Lifecycle of a multi-batch benchmark construction campaign."""

    ACTIVE = "active"
    COMPLETE = "complete"
    PAUSED = "paused"


class BatchStatus(StringEnum):
    """Lifecycle of one generation/screening/review batch."""

    CREATED = "created"
    GENERATING = "generating"
    SCREENING = "screening"
    HUMAN_REVIEW = "human_review"
    CLOSED = "closed"


class HumanReviewDecision(StringEnum):
    """Normalized human disposition for a retained model failure."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REPAIR = "needs_repair"
    PENDING = "pending"
