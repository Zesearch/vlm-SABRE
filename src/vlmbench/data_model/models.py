"""Canonical metadata records used across authoring and evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar, TypeVar

from .enums import (
    BatchStatus,
    CampaignStatus,
    CandidateStatus,
    HumanReviewDecision,
    SampleStatus,
    ScreeningDecision,
)


RecordT = TypeVar("RecordT", bound="MetadataRecord")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass
class MetadataRecord:
    """Base class with lossless conversion to and from JSON dictionaries."""

    extra: dict[str, Any] = field(default_factory=dict, repr=False, kw_only=True)
    id_field: ClassVar[str] = ""

    @classmethod
    def from_dict(cls: type[RecordT], row: dict[str, Any]) -> RecordT:
        known = {item.name for item in fields(cls) if item.name != "extra"}
        payload = {key: value for key, value in row.items() if key in known}
        extra = {key: value for key, value in row.items() if key not in known}
        return cls(**payload, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        extra = payload.pop("extra", {})
        payload = _json_value(payload)
        payload.update(_json_value(extra))
        return payload

    @property
    def record_id(self) -> str:
        return str(getattr(self, self.id_field, ""))


@dataclass
class Asset(MetadataRecord):
    asset_id: str = ""
    kind: str = ""
    path: str = ""
    mime_type: str = ""
    width: int | None = None
    height: int | None = None
    source_filename: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "asset_id"


@dataclass
class Edit(MetadataRecord):
    edit_id: str = ""
    source_asset_id: str = ""
    edit_type: str = ""
    instruction: str = ""
    bbox: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    id_field: ClassVar[str] = "edit_id"


@dataclass
class Candidate(MetadataRecord):
    candidate_id: str = ""
    edit_id: str = ""
    candidate_asset_id: str = ""
    generator: dict[str, Any] = field(default_factory=dict)
    status: CandidateStatus = CandidateStatus.CANDIDATE
    prompt: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    screening_result_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "candidate_id"

    def __post_init__(self) -> None:
        if not isinstance(self.status, CandidateStatus):
            self.status = CandidateStatus(str(self.status))


@dataclass
class Question(MetadataRecord):
    question_id: str = ""
    sample_id: str = ""
    edit_id: str = ""
    image_asset_id: str = ""
    image_role: str = ""
    question_type: str = ""
    prompt: str = ""
    answer: Any = ""
    eval_type: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "question_id"


@dataclass
class Sample(MetadataRecord):
    sample_id: str = ""
    source_asset_id: str = ""
    edit_ids: list[str] = field(default_factory=list)
    accepted_candidate_id: str = ""
    accepted_edited_asset_id: str = ""
    question_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    split: str = ""
    status: SampleStatus = SampleStatus.PENDING_EDIT
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "sample_id"

    def __post_init__(self) -> None:
        if not isinstance(self.status, SampleStatus):
            self.status = SampleStatus(str(self.status))


@dataclass
class ScreeningResult(MetadataRecord):
    """Aggregate pressure-screening result for one candidate."""

    screening_id: str = ""
    candidate_id: str = ""
    sample_id: str = ""
    recipe_id: str = ""
    provider: str = ""
    model: str = ""
    evaluator: str = ""
    predictions: list[dict[str, Any]] = field(default_factory=list)
    model_correct: bool | None = None
    decision: ScreeningDecision = ScreeningDecision.ERROR
    error: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "screening_id"

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ScreeningDecision):
            self.decision = ScreeningDecision(str(self.decision))
        if self.model_correct is not None:
            expected = (
                ScreeningDecision.REJECTED_CORRECT
                if self.model_correct
                else ScreeningDecision.RETAINED_FAILURE
            )
            if self.decision not in {expected, ScreeningDecision.ERROR}:
                raise ValueError(
                    "Screening decision conflicts with model_correct: "
                    f"{self.decision.value} vs {self.model_correct}"
                )

    @classmethod
    def from_model_outcome(
        cls,
        *,
        screening_id: str,
        candidate_id: str,
        model_correct: bool,
        **kwargs: Any,
    ) -> ScreeningResult:
        decision = (
            ScreeningDecision.REJECTED_CORRECT
            if model_correct
            else ScreeningDecision.RETAINED_FAILURE
        )
        return cls(
            screening_id=screening_id,
            candidate_id=candidate_id,
            model_correct=model_correct,
            decision=decision,
            **kwargs,
        )


@dataclass
class ExportRecord(MetadataRecord):
    export_id: str = ""
    format: str = ""
    path: str = ""
    pair_count: int = 0
    question_count: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "export_id"


@dataclass
class GenerationResult(MetadataRecord):
    """One image-generation or image-editing attempt."""

    generation_id: str = ""
    concept_id: str = ""
    sample_id: str = ""
    variant: int = 1
    generation_mode: str = ""
    stage: str = ""
    provider: str = ""
    model: str = ""
    status: str = ""
    asset_id: str = ""
    error: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "generation_id"


@dataclass
class Campaign(MetadataRecord):
    """A task-level run whose target is the final accepted sample count."""

    campaign_id: str = ""
    task_name: str = ""
    target_accepted: int = 0
    default_batch_size: int = 0
    status: CampaignStatus = CampaignStatus.ACTIVE
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "campaign_id"

    def __post_init__(self) -> None:
        if not isinstance(self.status, CampaignStatus):
            self.status = CampaignStatus(str(self.status))


@dataclass
class Batch(MetadataRecord):
    """One explicitly started tranche within a campaign."""

    batch_id: str = ""
    campaign_id: str = ""
    sequence: int = 0
    planned_candidates: int = 0
    status: BatchStatus = BatchStatus.CREATED
    created_at: str = ""
    updated_at: str = ""
    closed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "batch_id"

    def __post_init__(self) -> None:
        if not isinstance(self.status, BatchStatus):
            self.status = BatchStatus(str(self.status))


@dataclass
class HumanReview(MetadataRecord):
    """Latest human decision for one canonical sample."""

    review_id: str = ""
    sample_id: str = ""
    candidate_id: str = ""
    campaign_id: str = ""
    batch_id: str = ""
    decision: HumanReviewDecision = HumanReviewDecision.PENDING
    reason: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    id_field: ClassVar[str] = "review_id"

    def __post_init__(self) -> None:
        if not isinstance(self.decision, HumanReviewDecision):
            self.decision = HumanReviewDecision(str(self.decision))
