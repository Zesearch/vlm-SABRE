"""Track cumulative accepted samples across lossy generation batches."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from vlmbench.data_model import (
    Batch,
    BatchStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    HumanReview,
    HumanReviewDecision,
    MetadataRepository,
    Sample,
    SampleStatus,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope(row: dict[str, Any], key: str) -> str:
    metadata = row.get("metadata") or {}
    return str(metadata.get(key, ""))


TERMINAL_SAMPLE_STATUSES = {
    SampleStatus.ACCEPTED.value,
    SampleStatus.REJECTED.value,
    SampleStatus.REJECTED_CORRECT.value,
    SampleStatus.RETIRED.value,
    SampleStatus.DELETED.value,
}

REVIEW_QUEUE_STATUSES = {
    SampleStatus.RETAINED_FAILURE.value,
    SampleStatus.HUMAN_REVIEW.value,
    SampleStatus.NEEDS_REPAIR.value,
}


@dataclass(frozen=True)
class CampaignProgress:
    campaign_id: str
    status: str
    target_accepted: int
    accepted: int
    remaining: int
    total_samples: int
    retained_failure: int
    pending_review: int
    needs_repair: int
    rejected_correct: int
    rejected_human: int
    in_pipeline: int
    batches: list[dict[str, Any]]
    next_batch_size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CampaignManager:
    """Own campaign, batch, review, and cumulative-progress metadata."""

    def __init__(self, repository: MetadataRepository) -> None:
        self.repository = repository

    def initialize(
        self,
        *,
        campaign_id: str,
        target_accepted: int,
        default_batch_size: int,
        task_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Campaign:
        if not campaign_id.strip():
            raise ValueError("campaign_id is required")
        if target_accepted < 1:
            raise ValueError("target_accepted must be at least 1")
        if default_batch_size < 1:
            raise ValueError("default_batch_size must be at least 1")
        if any(
            str(row.get("campaign_id", "")) == campaign_id
            for row in self.repository.load("campaigns")
        ):
            raise ValueError(f"Campaign already exists: {campaign_id}")
        now = utc_now()
        campaign = Campaign(
            campaign_id=campaign_id,
            task_name=task_name,
            target_accepted=target_accepted,
            default_batch_size=default_batch_size,
            status=CampaignStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self.repository.upsert("campaigns", campaign)
        return campaign

    def get_campaign(self, campaign_id: str | None = None) -> Campaign:
        campaigns = [Campaign.from_dict(row) for row in self.repository.load("campaigns")]
        if campaign_id:
            for campaign in campaigns:
                if campaign.campaign_id == campaign_id:
                    return campaign
            raise ValueError(f"Unknown campaign: {campaign_id}")
        active = [row for row in campaigns if row.status == CampaignStatus.ACTIVE]
        if len(active) == 1:
            return active[0]
        if len(campaigns) == 1:
            return campaigns[0]
        raise ValueError("Specify campaign_id when the dataset has zero or multiple campaigns.")

    def get_batch(self, batch_id: str) -> Batch:
        for row in self.repository.load("batches"):
            if str(row.get("batch_id", "")) == batch_id:
                return Batch.from_dict(row)
        raise ValueError(f"Unknown batch: {batch_id}")

    def start_batch(
        self,
        *,
        campaign_id: str | None = None,
        planned_candidates: int | None = None,
    ) -> Batch:
        campaign = self.get_campaign(campaign_id)
        if campaign.status == CampaignStatus.COMPLETE:
            raise ValueError(f"Campaign is already complete: {campaign.campaign_id}")
        batches = [
            Batch.from_dict(row)
            for row in self.repository.load("batches")
            if str(row.get("campaign_id", "")) == campaign.campaign_id
        ]
        open_batches = [batch for batch in batches if batch.status != BatchStatus.CLOSED]
        if open_batches:
            raise ValueError(
                f"Close current batch before starting another: {open_batches[-1].batch_id}"
            )
        size = planned_candidates or campaign.default_batch_size
        if size < 1:
            raise ValueError("planned_candidates must be at least 1")
        sequence = max((batch.sequence for batch in batches), default=0) + 1
        now = utc_now()
        batch = Batch(
            batch_id=f"{campaign.campaign_id}__batch_{sequence:03d}",
            campaign_id=campaign.campaign_id,
            sequence=sequence,
            planned_candidates=size,
            status=BatchStatus.CREATED,
            created_at=now,
            updated_at=now,
        )
        self.repository.upsert("batches", batch)
        return batch

    def set_batch_status(self, batch_id: str, status: BatchStatus | str) -> Batch:
        batch = self.get_batch(batch_id)
        status = status if isinstance(status, BatchStatus) else BatchStatus(status)
        if batch.status == BatchStatus.CLOSED and status != BatchStatus.CLOSED:
            raise ValueError(f"Closed batch cannot be reopened: {batch_id}")
        batch.status = status
        batch.updated_at = utc_now()
        self.repository.upsert("batches", batch)
        return batch

    def progress(self, campaign_id: str | None = None) -> CampaignProgress:
        campaign = self.get_campaign(campaign_id)
        samples = [
            row
            for row in self.repository.load("samples")
            if _scope(row, "campaign_id") == campaign.campaign_id
        ]
        counts: dict[str, int] = {}
        for row in samples:
            status = str(row.get("status", ""))
            counts[status] = counts.get(status, 0) + 1
        accepted = counts.get(SampleStatus.ACCEPTED.value, 0)
        remaining = max(campaign.target_accepted - accepted, 0)
        batch_rows = [
            row
            for row in self.repository.load("batches")
            if str(row.get("campaign_id", "")) == campaign.campaign_id
        ]
        summaries = []
        for batch_row in sorted(batch_rows, key=lambda row: int(row.get("sequence", 0))):
            batch_id = str(batch_row.get("batch_id", ""))
            scoped = [row for row in samples if _scope(row, "batch_id") == batch_id]
            summaries.append(
                {
                    **batch_row,
                    "sample_count": len(scoped),
                    "accepted": sum(
                        str(row.get("status", "")) == SampleStatus.ACCEPTED.value
                        for row in scoped
                    ),
                    "awaiting_human": sum(
                        str(row.get("status", "")) in REVIEW_QUEUE_STATUSES
                        for row in scoped
                    ),
                }
            )
        in_pipeline = sum(
            count
            for status, count in counts.items()
            if status not in TERMINAL_SAMPLE_STATUSES
            and status not in REVIEW_QUEUE_STATUSES
        )
        return CampaignProgress(
            campaign_id=campaign.campaign_id,
            status=campaign.status.value,
            target_accepted=campaign.target_accepted,
            accepted=accepted,
            remaining=remaining,
            total_samples=len(samples),
            retained_failure=counts.get(SampleStatus.RETAINED_FAILURE.value, 0),
            pending_review=counts.get(SampleStatus.HUMAN_REVIEW.value, 0),
            needs_repair=counts.get(SampleStatus.NEEDS_REPAIR.value, 0),
            rejected_correct=counts.get(SampleStatus.REJECTED_CORRECT.value, 0),
            rejected_human=counts.get(SampleStatus.REJECTED.value, 0),
            in_pipeline=in_pipeline,
            batches=summaries,
            next_batch_size=campaign.default_batch_size if remaining else 0,
        )

    def review_items(
        self,
        campaign_id: str | None = None,
        *,
        include_generated: bool = False,
    ) -> list[dict[str, Any]]:
        campaigns = self.repository.load("campaigns")
        campaign = self.get_campaign(campaign_id) if campaigns else None
        queue_statuses = set(REVIEW_QUEUE_STATUSES)
        if include_generated:
            queue_statuses.add(SampleStatus.CANDIDATE_READY.value)
        samples = [
            Sample.from_dict(row)
            for row in self.repository.load("samples")
            if (campaign is None or _scope(row, "campaign_id") == campaign.campaign_id)
            and str(row.get("status", "")) in queue_statuses
        ]
        assets = {
            str(row.get("asset_id", "")): row for row in self.repository.load("assets")
        }
        candidates = [Candidate.from_dict(row) for row in self.repository.load("candidates")]
        candidate_by_edit = {candidate.edit_id: candidate for candidate in candidates}
        questions = self.repository.load("questions")
        screenings = self.repository.load("screening_results")
        screening_by_candidate = {
            str(row.get("candidate_id", "")): row for row in screenings
        }
        human_review_by_sample = {
            str(row.get("sample_id", "")): row
            for row in self.repository.load("human_reviews")
        }
        items: list[dict[str, Any]] = []
        for sample in samples:
            candidate = next(
                (candidate_by_edit[edit_id] for edit_id in sample.edit_ids if edit_id in candidate_by_edit),
                None,
            )
            if candidate is None:
                continue
            source = assets.get(sample.source_asset_id, {})
            edited = assets.get(candidate.candidate_asset_id, {})
            sample_questions = [
                row for row in questions if str(row.get("sample_id", "")) == sample.sample_id
            ]
            screening = screening_by_candidate.get(candidate.candidate_id, {})
            predictions = {
                str(row.get("question_id", "")): row
                for row in screening.get("predictions", [])
            }
            probes: dict[str, Any] = {}
            for index, question in enumerate(sample_questions, start=1):
                question_id = str(question.get("question_id", ""))
                prediction = predictions.get(question_id, {})
                probes[f"probe_{index:02d}"] = {
                    "question_id": question_id,
                    "question": question.get("prompt", ""),
                    "expected_answer": question.get("answer"),
                    "question_type": question.get("question_type", ""),
                    "eval_type": question.get("eval_type", ""),
                    "options": question.get("options") or {},
                    "gemini_prediction": prediction.get("raw_prediction", ""),
                    "correct": prediction.get("correct"),
                }
            metadata = sample.metadata or {}
            attributes = metadata.get("task_attributes") or {}
            review_targets = metadata.get("review_targets") or []
            review_metadata = (
                human_review_by_sample.get(sample.sample_id, {}).get("metadata") or {}
            )
            sample_campaign_id = str(
                metadata.get("campaign_id", campaign.campaign_id if campaign else "")
            )
            entry_source = str(
                review_metadata.get("entry_source")
                or (
                    "generated_without_screening"
                    if sample.status == SampleStatus.CANDIDATE_READY
                    else "screened_review_queue"
                )
            )
            items.append(
                {
                    "review_id": sample.sample_id,
                    "sample_id": sample.sample_id,
                    "candidate_id": candidate.candidate_id,
                    "campaign_id": sample_campaign_id,
                    "batch_id": metadata.get("batch_id", ""),
                    "run": metadata.get("batch_id") or sample_campaign_id or "standalone",
                    "pair_id": sample.sample_id,
                    "priority": entry_source,
                    "entry_source": entry_source,
                    "base_image": source.get("path", ""),
                    "edited_image": edited.get("path", ""),
                    "source_entity": attributes.get(
                        "source_entity", attributes.get("source_object", "")
                    ),
                    "inserted_entity": attributes.get(
                        "inserted_entity",
                        attributes.get("target_entity", attributes.get("target_object", "")),
                    ),
                    "review_location": attributes.get("review_location", ""),
                    "source_cluster_description": attributes.get("source_cluster_description", ""),
                    "target_visual_strategy": attributes.get("target_visual_strategy", ""),
                    "human_verifiability": "; ".join(
                        str(target.get("description", ""))
                        for target in review_targets
                        if isinstance(target, dict)
                    ),
                    "probes": probes,
                    "sample_status": sample.status.value,
                }
            )
        return sorted(items, key=lambda row: (str(row["batch_id"]), str(row["review_id"])))

    def record_review(
        self,
        *,
        review_id: str,
        decision: HumanReviewDecision | str,
        reason: str = "",
        notes: str = "",
        metadata: dict[str, Any] | None = None,
        allow_generated: bool = False,
    ) -> HumanReview:
        decision = decision if isinstance(decision, HumanReviewDecision) else HumanReviewDecision(decision)
        sample_rows = self.repository.load("samples")
        sample_row = next(
            (row for row in sample_rows if str(row.get("sample_id", "")) == review_id),
            None,
        )
        if sample_row is None:
            raise ValueError(f"Unknown review_id: {review_id}")
        queue_statuses = set(REVIEW_QUEUE_STATUSES)
        if allow_generated:
            queue_statuses.add(SampleStatus.CANDIDATE_READY.value)
        if str(sample_row.get("status", "")) not in queue_statuses:
            raise ValueError(f"Sample is not in the human review queue: {review_id}")
        sample = Sample.from_dict(sample_row)
        candidate_rows = self.repository.load("candidates")
        candidate_row = next(
            (
                row
                for row in candidate_rows
                if str(row.get("edit_id", "")) in sample.edit_ids
            ),
            None,
        )
        if candidate_row is None:
            raise ValueError(f"No candidate found for sample: {review_id}")
        candidate = Candidate.from_dict(candidate_row)
        status_map = {
            HumanReviewDecision.ACCEPTED: (SampleStatus.ACCEPTED, CandidateStatus.ACCEPTED),
            HumanReviewDecision.REJECTED: (SampleStatus.REJECTED, CandidateStatus.REJECTED),
            HumanReviewDecision.NEEDS_REPAIR: (SampleStatus.NEEDS_REPAIR, CandidateStatus.NEEDS_REPAIR),
            HumanReviewDecision.PENDING: (SampleStatus.HUMAN_REVIEW, CandidateStatus.HUMAN_REVIEW),
        }
        sample_status, candidate_status = status_map[decision]
        now = utc_now()
        sample.status = sample_status
        sample.updated_at = now
        candidate.status = candidate_status
        candidate.updated_at = now
        if decision == HumanReviewDecision.ACCEPTED:
            sample.accepted_candidate_id = candidate.candidate_id
            sample.accepted_edited_asset_id = candidate.candidate_asset_id
        else:
            sample.accepted_candidate_id = ""
            sample.accepted_edited_asset_id = ""
        existing = next(
            (
                row
                for row in self.repository.load("human_reviews")
                if str(row.get("review_id", "")) == review_id
            ),
            None,
        )
        scope = sample.metadata or {}
        review = HumanReview(
            review_id=review_id,
            sample_id=sample.sample_id,
            candidate_id=candidate.candidate_id,
            campaign_id=str(scope.get("campaign_id", "")),
            batch_id=str(scope.get("batch_id", "")),
            decision=decision,
            reason=reason,
            notes=notes,
            created_at=str((existing or {}).get("created_at", now)),
            updated_at=now,
            metadata={**((existing or {}).get("metadata") or {}), **(metadata or {})},
        )
        self.repository.upsert("samples", sample)
        self.repository.upsert("candidates", candidate)
        self.repository.upsert("human_reviews", review)
        return review

    def close_batch(self, batch_id: str) -> Batch:
        batch = self.get_batch(batch_id)
        sample_rows = self.repository.load("samples")
        samples = [
            row
            for row in sample_rows
            if _scope(row, "batch_id") == batch_id
        ]
        unfinished = [
            str(row.get("sample_id", ""))
            for row in samples
            if str(row.get("status", "")) not in TERMINAL_SAMPLE_STATUSES
            and str(row.get("status", "")) != SampleStatus.PENDING_EDIT.value
        ]
        if unfinished:
            raise ValueError(
                f"Batch still has {len(unfinished)} unfinished sample(s); first: {unfinished[0]}"
            )
        now = utc_now()
        for row in samples:
            if str(row.get("status", "")) != SampleStatus.PENDING_EDIT.value:
                continue
            sample = Sample.from_dict(row)
            sample.status = SampleStatus.REJECTED
            sample.updated_at = now
            sample.metadata = {
                **sample.metadata,
                "rejection_reason": "generation_incomplete_when_batch_closed",
            }
            self.repository.upsert("samples", sample)
            row["status"] = SampleStatus.REJECTED.value
        batch.status = BatchStatus.CLOSED
        batch.updated_at = now
        batch.closed_at = now
        self.repository.upsert("batches", batch)
        progress = self.progress(batch.campaign_id)
        if progress.remaining == 0:
            campaign = self.get_campaign(batch.campaign_id)
            campaign.status = CampaignStatus.COMPLETE
            campaign.updated_at = now
            self.repository.upsert("campaigns", campaign)
        return batch
