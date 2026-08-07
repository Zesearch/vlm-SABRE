"""Pressure screening: retain only candidates that make the model fail."""

from __future__ import annotations

import concurrent.futures
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from vlmbench.data_model import (
    CandidateStatus,
    MetadataRepository,
    SampleStatus,
    ScreeningDecision,
    ScreeningResult,
    upsert_by_key,
)
from vlmbench.eval import score_prediction


class PredictionClient(Protocol):
    """Provider adapter used by the screening runner."""

    provider: str
    model: str

    def predict(self, *, image_path: Path, question: dict[str, Any]) -> str:
        """Answer one benchmark question about one image."""


@dataclass(frozen=True)
class ScreeningRunSummary:
    selected: int
    processed: int
    retained_failures: int
    rejected_correct: int
    errors: int
    skipped_existing: int
    dry_run: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_identifier(value: str, max_length: int = 180) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")
    if len(normalized) <= max_length:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[: max_length - 13]}_{digest}"


class PressureScreeningRunner:
    """Run a model over candidate probes and apply the failure-only hard gate."""

    SCREENABLE_STATUSES = {
        CandidateStatus.CANDIDATE.value,
        CandidateStatus.GENERATED.value,
        CandidateStatus.SCREENING.value,
    }
    SAMPLE_MUTABLE_STATUSES = {
        SampleStatus.PENDING_EDIT.value,
        SampleStatus.CANDIDATE_READY.value,
        SampleStatus.SCREENING.value,
        SampleStatus.RETAINED_FAILURE.value,
        SampleStatus.REJECTED_CORRECT.value,
    }

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        client: PredictionClient,
        recipe_id: str = "",
        workers: int = 1,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.repository = repository
        self.client = client
        self.recipe_id = recipe_id
        self.workers = workers

    def _screening_id(self, candidate_id: str) -> str:
        parts = [candidate_id, self.recipe_id, self.client.provider, self.client.model]
        return safe_identifier("__".join(part for part in parts if part))

    @staticmethod
    def _sample_for_candidate(
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
    ) -> dict[str, Any]:
        edit_id = str(candidate.get("edit_id", ""))
        matches = [
            sample
            for sample in samples
            if edit_id and edit_id in {str(value) for value in (sample.get("edit_ids") or [])}
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Candidate {candidate.get('candidate_id', '')} must map to exactly one sample; "
                f"found {len(matches)}."
            )
        return matches[0]

    @staticmethod
    def _questions_for_sample(
        sample: dict[str, Any],
        questions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sample_id = str(sample.get("sample_id", ""))
        by_id = {str(row.get("question_id", "")): row for row in questions}
        question_ids = [str(value) for value in (sample.get("question_ids") or [])]
        if question_ids:
            selected = [by_id[question_id] for question_id in question_ids if question_id in by_id]
        else:
            selected = [
                row for row in questions if str(row.get("sample_id", "")) == sample_id
            ]
            selected.sort(key=lambda row: str(row.get("question_id", "")))
        if not selected:
            raise ValueError(f"Sample {sample_id} has no questions for screening.")
        return selected

    def _question_image_path(
        self,
        *,
        candidate: dict[str, Any],
        sample: dict[str, Any],
        question: dict[str, Any],
        assets_by_id: dict[str, dict[str, Any]],
    ) -> tuple[Path, str]:
        image_role = str(question.get("image_role", "")).lower()
        use_candidate = image_role in {"edited", "candidate", "generated"}
        if not use_candidate:
            accepted_asset_id = str(sample.get("accepted_edited_asset_id", ""))
            use_candidate = bool(accepted_asset_id) and (
                str(question.get("image_asset_id", "")) == accepted_asset_id
            )
        asset_id = (
            str(candidate.get("candidate_asset_id", ""))
            if use_candidate
            else str(question.get("image_asset_id", ""))
        )
        asset = assets_by_id.get(asset_id)
        if not asset:
            raise ValueError(
                f"Question {question.get('question_id', '')} references unknown image asset: {asset_id}"
            )
        image_path = self.repository.root / str(asset.get("path", ""))
        if not image_path.exists():
            raise FileNotFoundError(image_path)
        return image_path, asset_id

    def _screen_candidate(
        self,
        *,
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        assets_by_id: dict[str, dict[str, Any]],
    ) -> ScreeningResult:
        candidate_id = str(candidate.get("candidate_id", ""))
        screening_id = self._screening_id(candidate_id)
        sample_id = ""
        try:
            sample = self._sample_for_candidate(candidate, samples)
            sample_id = str(sample.get("sample_id", ""))
            selected_questions = self._questions_for_sample(sample, questions)
            predictions: list[dict[str, Any]] = []
            for question in selected_questions:
                image_path, resolved_asset_id = self._question_image_path(
                    candidate=candidate,
                    sample=sample,
                    question=question,
                    assets_by_id=assets_by_id,
                )
                raw_prediction = self.client.predict(
                    image_path=image_path,
                    question=question,
                )
                trace = score_prediction(question, raw_prediction)
                trace.update(
                    {
                        "image_asset_id": resolved_asset_id,
                        "image_role": str(question.get("image_role", "")),
                    }
                )
                predictions.append(trace)

            model_correct = all(bool(row["correct"]) for row in predictions)
            sample_metadata = sample.get("metadata") or {}
            return ScreeningResult.from_model_outcome(
                screening_id=screening_id,
                candidate_id=candidate_id,
                sample_id=sample_id,
                recipe_id=self.recipe_id,
                provider=self.client.provider,
                model=self.client.model,
                evaluator="all_questions_correct",
                predictions=predictions,
                model_correct=model_correct,
                created_at=utc_now(),
                metadata={
                    "question_count": len(predictions),
                    **{
                        key: sample_metadata[key]
                        for key in ("campaign_id", "batch_id")
                        if sample_metadata.get(key)
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            return ScreeningResult(
                screening_id=screening_id,
                candidate_id=candidate_id,
                sample_id=sample_id,
                recipe_id=self.recipe_id,
                provider=self.client.provider,
                model=self.client.model,
                evaluator="all_questions_correct",
                decision=ScreeningDecision.ERROR,
                error=str(exc),
                created_at=utc_now(),
            )

    @staticmethod
    def _update_sample_statuses(
        samples: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> None:
        for sample in samples:
            if str(sample.get("status", "")) not in PressureScreeningRunner.SAMPLE_MUTABLE_STATUSES:
                continue
            edit_ids = {str(value) for value in (sample.get("edit_ids") or [])}
            related = [
                candidate
                for candidate in candidates
                if str(candidate.get("edit_id", "")) in edit_ids
            ]
            statuses = {str(candidate.get("status", "")) for candidate in related}
            if CandidateStatus.RETAINED_FAILURE.value in statuses:
                sample["status"] = SampleStatus.RETAINED_FAILURE.value
                sample["updated_at"] = utc_now()
            elif statuses and statuses <= {CandidateStatus.REJECTED_CORRECT.value}:
                sample["status"] = SampleStatus.REJECTED_CORRECT.value
                sample["updated_at"] = utc_now()
            elif statuses & PressureScreeningRunner.SCREENABLE_STATUSES:
                sample["status"] = SampleStatus.CANDIDATE_READY.value

    def run(
        self,
        *,
        candidate_ids: list[str] | None = None,
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> ScreeningRunSummary:
        assets = self.repository.load("assets")
        candidates = self.repository.load("candidates")
        samples = self.repository.load("samples")
        questions = self.repository.load("questions")
        screening_rows = self.repository.load("screening_results")
        assets_by_id = {str(row.get("asset_id", "")): row for row in assets}
        requested = set(candidate_ids or [])
        previously_screened = {
            str(row.get("candidate_id", "")) for row in screening_rows
        }

        selected = [
            candidate
            for candidate in candidates
            if (not requested or str(candidate.get("candidate_id", "")) in requested)
            and (
                requested
                or (
                    overwrite
                    and str(candidate.get("candidate_id", "")) in previously_screened
                )
                or str(candidate.get("status", CandidateStatus.CANDIDATE.value))
                in self.SCREENABLE_STATUSES
            )
        ]
        found_ids = {str(candidate.get("candidate_id", "")) for candidate in selected}
        missing = requested - found_ids
        if missing:
            raise ValueError(f"Unknown candidate_id(s): {', '.join(sorted(missing))}")

        existing_ids = {str(row.get("screening_id", "")) for row in screening_rows}
        pending: list[dict[str, Any]] = []
        skipped_existing = 0
        for candidate in selected:
            if self._screening_id(str(candidate.get("candidate_id", ""))) in existing_ids and not overwrite:
                skipped_existing += 1
            else:
                pending.append(candidate)

        if self.workers == 1:
            results = [
                self._screen_candidate(
                    candidate=candidate,
                    samples=samples,
                    questions=questions,
                    assets_by_id=assets_by_id,
                )
                for candidate in pending
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [
                    executor.submit(
                        self._screen_candidate,
                        candidate=candidate,
                        samples=samples,
                        questions=questions,
                        assets_by_id=assets_by_id,
                    )
                    for candidate in pending
                ]
                results = [future.result() for future in futures]

        result_by_candidate = {result.candidate_id: result for result in results}
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id", ""))
            result = result_by_candidate.get(candidate_id)
            if not result or result.decision == ScreeningDecision.ERROR:
                continue
            candidate["status"] = result.decision.value
            result_ids = [str(value) for value in (candidate.get("screening_result_ids") or [])]
            if result.screening_id not in result_ids:
                result_ids.append(result.screening_id)
            candidate["screening_result_ids"] = sorted(result_ids)
            candidate["updated_at"] = result.created_at

        self._update_sample_statuses(samples, candidates)
        for result in results:
            screening_rows = upsert_by_key(
                screening_rows,
                "screening_id",
                result.to_dict(),
            )

        if not dry_run:
            self.repository.write("screening_results", screening_rows)
            self.repository.write("candidates", candidates)
            self.repository.write("samples", samples)

        return ScreeningRunSummary(
            selected=len(selected),
            processed=len(results),
            retained_failures=sum(
                result.decision == ScreeningDecision.RETAINED_FAILURE for result in results
            ),
            rejected_correct=sum(
                result.decision == ScreeningDecision.REJECTED_CORRECT for result in results
            ),
            errors=sum(result.decision == ScreeningDecision.ERROR for result in results),
            skipped_existing=skipped_existing,
            dry_run=dry_run,
        )
