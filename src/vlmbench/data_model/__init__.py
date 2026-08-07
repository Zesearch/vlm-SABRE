"""Canonical metadata schema and persistence for VLM benchmark datasets."""

from .enums import (
    BatchStatus,
    CampaignStatus,
    CandidateStatus,
    HumanReviewDecision,
    SampleStatus,
    ScreeningDecision,
)
from .models import (
    Asset,
    Batch,
    Campaign,
    Candidate,
    Edit,
    ExportRecord,
    GenerationResult,
    HumanReview,
    Question,
    Sample,
    ScreeningResult,
)
from .repository import (
    ALL_METADATA_FILES,
    METADATA_FILES,
    OPTIONAL_METADATA_FILES,
    MetadataRepository,
    load_jsonl,
    upsert_by_key,
    write_jsonl,
)
from .validation import ValidationIssue, validate_metadata

__all__ = [
    "ALL_METADATA_FILES",
    "METADATA_FILES",
    "OPTIONAL_METADATA_FILES",
    "Asset",
    "Batch",
    "BatchStatus",
    "Campaign",
    "CampaignStatus",
    "Candidate",
    "CandidateStatus",
    "Edit",
    "ExportRecord",
    "GenerationResult",
    "HumanReview",
    "HumanReviewDecision",
    "MetadataRepository",
    "Question",
    "Sample",
    "SampleStatus",
    "ScreeningDecision",
    "ScreeningResult",
    "ValidationIssue",
    "load_jsonl",
    "upsert_by_key",
    "validate_metadata",
    "write_jsonl",
]
