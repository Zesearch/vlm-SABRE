"""Construct visual assets and canonical metadata from validated designs."""

from __future__ import annotations

import concurrent.futures
import io
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from vlmbench.data_model import (
    Asset,
    Candidate,
    CandidateStatus,
    Edit,
    GenerationResult,
    MetadataRepository,
    Question,
    Sample,
    SampleStatus,
    load_jsonl,
    upsert_by_key,
)
from vlmbench.pipeline.image_provider import GeneratedImage, ImageGenerationClient
from vlmbench.pipeline.screening import safe_identifier
from vlmbench.recipes import BenchmarkDesign, build_questions


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class ImageConstructionSummary:
    selected: int
    planned: int
    completed: int
    skipped_existing: int
    base_generated: int
    edited_generated: int
    single_generated: int
    errors: int
    dry_run: bool


@dataclass
class ConstructionOutcome:
    sample_id: str
    assets: list[Asset] = field(default_factory=list)
    edit: Edit | None = None
    candidate: Candidate | None = None
    sample: Sample | None = None
    questions: list[Question] = field(default_factory=list)
    generation_results: list[GenerationResult] = field(default_factory=list)
    generated_stages: set[str] = field(default_factory=set)
    skipped_existing: bool = False
    overwritten_candidate_id: str = ""
    error: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inspect_image(data: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.size
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Image provider returned invalid image bytes: {exc}") from exc


def write_generated_image(
    *,
    root: Path,
    relative_stem: str,
    image: GeneratedImage,
) -> tuple[str, int, int]:
    extension = MIME_EXTENSIONS.get(image.mime_type)
    if not extension:
        raise ValueError(f"Unsupported generated image MIME type: {image.mime_type}")
    width, height = inspect_image(image.data)
    target = root / f"{relative_stem}{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{target.stem}_",
        suffix=target.suffix,
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(image.data)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target.relative_to(root).as_posix(), width, height


class ImageConstructionRunner:
    """Generate/edit images and register immediately screenable samples."""

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        client: ImageGenerationClient,
        workers: int = 1,
        campaign_id: str = "",
        batch_id: str = "",
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.repository = repository
        self.client = client
        self.workers = workers
        self.campaign_id = campaign_id
        self.batch_id = batch_id

    @staticmethod
    def _sample_id(design: BenchmarkDesign, variant: int) -> str:
        return safe_identifier(f"{design.concept_id}__v{variant:02d}")

    @staticmethod
    def _variant_prompt(prompt: str, variant: int) -> str:
        if variant == 1:
            return prompt
        return (
            prompt
            + f"\n\nComposition variation {variant}: preserve every benchmark requirement "
            "while using a meaningfully different camera position and scene arrangement."
        )

    def _design_metadata(self, design: BenchmarkDesign, variant: int) -> dict[str, Any]:
        metadata = {
            "concept_id": design.concept_id,
            "pressure_test_type": design.pressure_test_type,
            "generation_mode": design.generation_mode,
            "variant": variant,
            "task_attributes": {
                attribute.name: attribute.value
                for attribute in design.task_attributes
            },
            "review_targets": [
                target.model_dump(mode="json") for target in design.review_targets
            ],
        }
        if self.campaign_id:
            metadata["campaign_id"] = self.campaign_id
        if self.batch_id:
            metadata["batch_id"] = self.batch_id
        return metadata

    def _generation_result(
        self,
        *,
        design: BenchmarkDesign,
        sample_id: str,
        variant: int,
        stage: str,
        status: str,
        asset_id: str = "",
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            generation_id=f"{sample_id}__{stage}",
            concept_id=design.concept_id,
            sample_id=sample_id,
            variant=variant,
            generation_mode=design.generation_mode,
            stage=stage,
            provider=self.client.provider,
            model=self.client.model,
            status=status,
            asset_id=asset_id,
            error=error,
            created_at=utc_now(),
            metadata={
                **self._design_metadata(design, variant),
                **(metadata or {}),
            },
        )

    def _existing_complete(
        self,
        *,
        candidate_id: str,
        candidates_by_id: dict[str, dict[str, Any]],
        assets_by_id: dict[str, dict[str, Any]],
    ) -> bool:
        candidate = candidates_by_id.get(candidate_id)
        if not candidate:
            return False
        asset = assets_by_id.get(str(candidate.get("candidate_asset_id", "")))
        return bool(asset) and (self.repository.root / str(asset.get("path", ""))).exists()

    def _existing_outcome(
        self,
        *,
        design: BenchmarkDesign,
        sample_id: str,
        candidate_id: str,
        candidates_by_id: dict[str, dict[str, Any]],
        samples_by_id: dict[str, dict[str, Any]],
        questions_by_sample: dict[str, list[dict[str, Any]]],
    ) -> ConstructionOutcome:
        candidate = Candidate.from_dict(candidates_by_id[candidate_id])
        sample_row = samples_by_id.get(sample_id)
        if not sample_row:
            raise ValueError(f"Existing candidate {candidate_id} has no sample {sample_id}.")
        sample = Sample.from_dict(sample_row)
        return ConstructionOutcome(
            sample_id=sample_id,
            candidate=candidate,
            sample=sample,
            questions=[
                Question.from_dict(row) for row in questions_by_sample.get(sample_id, [])
            ],
            skipped_existing=True,
        )

    def _construct_single(
        self,
        *,
        design: BenchmarkDesign,
        variant: int,
        sample_id: str,
    ) -> ConstructionOutcome:
        now = utc_now()
        edit_id = f"{sample_id}_edit_001"
        candidate_id = f"{sample_id}_candidate_001"
        asset_id = f"{sample_id}_generated"
        outcome = ConstructionOutcome(sample_id=sample_id)
        try:
            request_prompt = self._variant_prompt(design.image_prompt, variant)
            generated = self.client.generate(prompt=request_prompt)
            relative_path, width, height = write_generated_image(
                root=self.repository.root,
                relative_stem=f"assets/generated/{sample_id}",
                image=generated,
            )
            asset = Asset(
                asset_id=asset_id,
                kind="generated_image",
                path=relative_path,
                mime_type=generated.mime_type,
                width=width,
                height=height,
                created_at=now,
                metadata={
                    **self._design_metadata(design, variant),
                    "prompt": request_prompt,
                },
            )
            edit = Edit(
                edit_id=edit_id,
                source_asset_id=asset_id,
                edit_type="generate_image",
                instruction=request_prompt,
                metadata=self._design_metadata(design, variant),
                created_at=now,
                updated_at=now,
            )
            candidate = Candidate(
                candidate_id=candidate_id,
                edit_id=edit_id,
                candidate_asset_id=asset_id,
                generator={
                    "provider": self.client.provider,
                    "model": self.client.model,
                    "method": "text_to_image",
                },
                status=CandidateStatus.GENERATED,
                prompt=request_prompt,
                created_at=now,
                updated_at=now,
                metadata=self._design_metadata(design, variant),
            )
            questions = build_questions(
                design=design,
                sample_id=sample_id,
                edit_id=edit_id,
                single_asset_id=asset_id,
            )
            for question in questions:
                question.metadata.update(
                    {
                        key: value
                        for key, value in {
                            "campaign_id": self.campaign_id,
                            "batch_id": self.batch_id,
                        }.items()
                        if value
                    }
                )
            sample = Sample(
                sample_id=sample_id,
                source_asset_id=asset_id,
                edit_ids=[edit_id],
                question_ids=[question.question_id for question in questions],
                tags=[design.pressure_test_type, "single_image"],
                split="generation",
                status=SampleStatus.CANDIDATE_READY,
                created_at=now,
                updated_at=now,
                metadata=self._design_metadata(design, variant),
            )
            outcome.assets.append(asset)
            outcome.edit = edit
            outcome.candidate = candidate
            outcome.sample = sample
            outcome.questions = questions
            outcome.generated_stages.add("single")
            outcome.generation_results.append(
                self._generation_result(
                    design=design,
                    sample_id=sample_id,
                    variant=variant,
                    stage="single",
                    status="ok",
                    asset_id=asset_id,
                    metadata={
                        **generated.metadata,
                        "path": relative_path,
                        "width": width,
                        "height": height,
                    },
                )
            )
            return outcome
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.generation_results.append(
                self._generation_result(
                    design=design,
                    sample_id=sample_id,
                    variant=variant,
                    stage="single",
                    status="failed",
                    error=outcome.error,
                )
            )
            return outcome

    def _construct_paired(
        self,
        *,
        design: BenchmarkDesign,
        variant: int,
        sample_id: str,
        assets_by_id: dict[str, dict[str, Any]],
    ) -> ConstructionOutcome:
        now = utc_now()
        base_asset_id = f"{sample_id}_base"
        candidate_asset_id = f"{sample_id}_candidate_001"
        edit_id = f"{sample_id}_edit_001"
        candidate_id = f"{sample_id}_candidate_001"
        outcome = ConstructionOutcome(sample_id=sample_id)
        base_asset_row = assets_by_id.get(base_asset_id)
        base_path = (
            self.repository.root / str(base_asset_row.get("path", ""))
            if base_asset_row
            else None
        )
        try:
            if not base_path or not base_path.exists():
                base_request_prompt = self._variant_prompt(
                    design.base_prompt,
                    variant,
                )
                generated_base = self.client.generate(prompt=base_request_prompt)
                relative_path, width, height = write_generated_image(
                    root=self.repository.root,
                    relative_stem=f"assets/base/{sample_id}",
                    image=generated_base,
                )
                base_asset = Asset(
                    asset_id=base_asset_id,
                    kind="base_image",
                    path=relative_path,
                    mime_type=generated_base.mime_type,
                    width=width,
                    height=height,
                    created_at=now,
                    metadata={
                        **self._design_metadata(design, variant),
                        "prompt": base_request_prompt,
                    },
                )
                base_path = self.repository.root / relative_path
                outcome.assets.append(base_asset)
                outcome.generated_stages.add("base")
                outcome.generation_results.append(
                    self._generation_result(
                        design=design,
                        sample_id=sample_id,
                        variant=variant,
                        stage="base",
                        status="ok",
                        asset_id=base_asset_id,
                        metadata={
                            **generated_base.metadata,
                            "path": relative_path,
                            "width": width,
                            "height": height,
                        },
                    )
                )
            else:
                base_asset = Asset.from_dict(base_asset_row)
                outcome.assets.append(base_asset)

            edit = Edit(
                edit_id=edit_id,
                source_asset_id=base_asset_id,
                edit_type="controlled_edit",
                instruction=design.edit_prompt,
                metadata=self._design_metadata(design, variant),
                created_at=now,
                updated_at=now,
            )
            outcome.edit = edit
            edited = self.client.edit(
                source_image=base_path,
                prompt=design.edit_prompt,
            )
            relative_path, width, height = write_generated_image(
                root=self.repository.root,
                relative_stem=f"assets/candidates/{sample_id}/candidate_001",
                image=edited,
            )
            candidate_asset = Asset(
                asset_id=candidate_asset_id,
                kind="candidate_image",
                path=relative_path,
                mime_type=edited.mime_type,
                width=width,
                height=height,
                created_at=now,
                metadata=self._design_metadata(design, variant),
            )
            candidate = Candidate(
                candidate_id=candidate_id,
                edit_id=edit_id,
                candidate_asset_id=candidate_asset_id,
                generator={
                    "provider": self.client.provider,
                    "model": self.client.model,
                    "method": "image_edit",
                },
                status=CandidateStatus.GENERATED,
                prompt=design.edit_prompt,
                created_at=now,
                updated_at=now,
                metadata=self._design_metadata(design, variant),
            )
            questions = build_questions(
                design=design,
                sample_id=sample_id,
                edit_id=edit_id,
                source_asset_id=base_asset_id,
                edited_asset_id=candidate_asset_id,
            )
            for question in questions:
                question.metadata.update(
                    {
                        key: value
                        for key, value in {
                            "campaign_id": self.campaign_id,
                            "batch_id": self.batch_id,
                        }.items()
                        if value
                    }
                )
            sample = Sample(
                sample_id=sample_id,
                source_asset_id=base_asset_id,
                edit_ids=[edit_id],
                question_ids=[question.question_id for question in questions],
                tags=[design.pressure_test_type, "paired_edit"],
                split="generation",
                status=SampleStatus.CANDIDATE_READY,
                created_at=now,
                updated_at=now,
                metadata=self._design_metadata(design, variant),
            )
            outcome.assets.append(candidate_asset)
            outcome.candidate = candidate
            outcome.sample = sample
            outcome.questions = questions
            outcome.generated_stages.add("edited")
            outcome.generation_results.append(
                self._generation_result(
                    design=design,
                    sample_id=sample_id,
                    variant=variant,
                    stage="edited",
                    status="ok",
                    asset_id=candidate_asset_id,
                    metadata={
                        **edited.metadata,
                        "path": relative_path,
                        "width": width,
                        "height": height,
                    },
                )
            )
            return outcome
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{type(exc).__name__}: {exc}"
            stage = "edited" if outcome.edit else "base"
            outcome.generation_results.append(
                self._generation_result(
                    design=design,
                    sample_id=sample_id,
                    variant=variant,
                    stage=stage,
                    status="failed",
                    error=outcome.error,
                )
            )
            if outcome.edit and outcome.assets:
                outcome.sample = Sample(
                    sample_id=sample_id,
                    source_asset_id=base_asset_id,
                    edit_ids=[edit_id],
                    tags=[design.pressure_test_type, "paired_edit"],
                    split="generation",
                    status=SampleStatus.PENDING_EDIT,
                    created_at=now,
                    updated_at=now,
                    metadata=self._design_metadata(design, variant),
                )
            return outcome

    def _construct_one(
        self,
        *,
        design: BenchmarkDesign,
        variant: int,
        overwrite: bool,
        assets_by_id: dict[str, dict[str, Any]],
        candidates_by_id: dict[str, dict[str, Any]],
        samples_by_id: dict[str, dict[str, Any]],
        questions_by_sample: dict[str, list[dict[str, Any]]],
    ) -> ConstructionOutcome:
        sample_id = self._sample_id(design, variant)
        candidate_id = f"{sample_id}_candidate_001"
        if not overwrite and self._existing_complete(
            candidate_id=candidate_id,
            candidates_by_id=candidates_by_id,
            assets_by_id=assets_by_id,
        ):
            return self._existing_outcome(
                design=design,
                sample_id=sample_id,
                candidate_id=candidate_id,
                candidates_by_id=candidates_by_id,
                samples_by_id=samples_by_id,
                questions_by_sample=questions_by_sample,
            )
        outcome = (
            self._construct_paired(
                design=design,
                variant=variant,
                sample_id=sample_id,
                assets_by_id={} if overwrite else assets_by_id,
            )
            if design.generation_mode == "paired_edit"
            else self._construct_single(
                design=design,
                variant=variant,
                sample_id=sample_id,
            )
        )
        if overwrite and candidate_id in candidates_by_id:
            outcome.overwritten_candidate_id = candidate_id
        return outcome

    def run(
        self,
        *,
        designs_path: Path,
        variants: int = 1,
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> ImageConstructionSummary:
        if variants < 1:
            raise ValueError("variants must be at least 1")
        designs = [
            BenchmarkDesign.model_validate(row) for row in load_jsonl(designs_path)
        ]
        if not designs:
            raise ValueError(f"No designs found: {designs_path}")

        assets = self.repository.load("assets")
        edits = self.repository.load("edits")
        candidates = self.repository.load("candidates")
        samples = self.repository.load("samples")
        questions = self.repository.load("questions")
        generation_rows = self.repository.load("generation_results")
        screening_rows = self.repository.load("screening_results")
        assets_by_id = {str(row.get("asset_id", "")): row for row in assets}
        candidates_by_id = {
            str(row.get("candidate_id", "")): row for row in candidates
        }
        samples_by_id = {str(row.get("sample_id", "")): row for row in samples}
        questions_by_sample: dict[str, list[dict[str, Any]]] = {}
        for question in questions:
            questions_by_sample.setdefault(str(question.get("sample_id", "")), []).append(
                question
            )

        work = [
            (design, variant)
            for design in designs
            for variant in range(1, variants + 1)
        ]
        sample_ids = [self._sample_id(design, variant) for design, variant in work]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Design concept_id and variant combinations must be unique.")
        complete_count = sum(
            self._existing_complete(
                candidate_id=f"{self._sample_id(design, variant)}_candidate_001",
                candidates_by_id=candidates_by_id,
                assets_by_id=assets_by_id,
            )
            for design, variant in work
        )
        planned = len(work) if overwrite else len(work) - complete_count
        if dry_run:
            return ImageConstructionSummary(
                selected=len(work),
                planned=planned,
                completed=0,
                skipped_existing=0 if overwrite else complete_count,
                base_generated=0,
                edited_generated=0,
                single_generated=0,
                errors=0,
                dry_run=True,
            )

        kwargs = {
            "overwrite": overwrite,
            "assets_by_id": assets_by_id,
            "candidates_by_id": candidates_by_id,
            "samples_by_id": samples_by_id,
            "questions_by_sample": questions_by_sample,
        }
        if self.workers == 1:
            outcomes = [
                self._construct_one(design=design, variant=variant, **kwargs)
                for design, variant in work
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [
                    executor.submit(
                        self._construct_one,
                        design=design,
                        variant=variant,
                        **kwargs,
                    )
                    for design, variant in work
                ]
                outcomes = [future.result() for future in futures]

        overwritten_candidate_ids = {
            outcome.overwritten_candidate_id
            for outcome in outcomes
            if outcome.overwritten_candidate_id
        }
        if overwritten_candidate_ids:
            screening_rows = [
                row
                for row in screening_rows
                if str(row.get("candidate_id", "")) not in overwritten_candidate_ids
            ]

        for outcome in outcomes:
            for asset in outcome.assets:
                assets = upsert_by_key(assets, "asset_id", asset.to_dict())
            if outcome.edit:
                edits = upsert_by_key(edits, "edit_id", outcome.edit.to_dict())
            if outcome.candidate:
                candidate = outcome.candidate
                if candidate.candidate_id in overwritten_candidate_ids:
                    candidate.screening_result_ids = []
                candidates = upsert_by_key(
                    candidates,
                    "candidate_id",
                    candidate.to_dict(),
                )
            if outcome.sample:
                samples = upsert_by_key(
                    samples,
                    "sample_id",
                    outcome.sample.to_dict(),
                )
            if outcome.questions and not outcome.skipped_existing:
                questions = [
                    row
                    for row in questions
                    if str(row.get("sample_id", "")) != outcome.sample_id
                ]
                questions.extend(question.to_dict() for question in outcome.questions)
            for result in outcome.generation_results:
                generation_rows = upsert_by_key(
                    generation_rows,
                    "generation_id",
                    result.to_dict(),
                )

        self.repository.write("assets", assets)
        self.repository.write("edits", edits)
        self.repository.write("candidates", candidates)
        self.repository.write("samples", samples)
        self.repository.write(
            "questions",
            sorted(questions, key=lambda row: str(row.get("question_id", ""))),
        )
        self.repository.write("generation_results", generation_rows)
        if not self.repository.exists("exports"):
            self.repository.write("exports", [])
        if overwritten_candidate_ids:
            self.repository.write("screening_results", screening_rows)

        return ImageConstructionSummary(
            selected=len(work),
            planned=planned,
            completed=sum(
                bool(outcome.candidate) and not outcome.skipped_existing
                for outcome in outcomes
            ),
            skipped_existing=sum(outcome.skipped_existing for outcome in outcomes),
            base_generated=sum(
                "base" in outcome.generated_stages for outcome in outcomes
            ),
            edited_generated=sum(
                "edited" in outcome.generated_stages for outcome in outcomes
            ),
            single_generated=sum(
                "single" in outcome.generated_stages for outcome in outcomes
            ),
            errors=sum(bool(outcome.error) for outcome in outcomes),
            dry_run=False,
        )
