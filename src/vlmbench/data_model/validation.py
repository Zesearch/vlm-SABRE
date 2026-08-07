"""Cross-record validation for canonical benchmark metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .repository import METADATA_FILES


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    record_type: str = ""
    record_id: str = ""
    field: str = ""

    def __str__(self) -> str:
        return self.message


def _id_map(
    rows: Iterable[dict[str, Any]],
    *,
    collection: str,
    id_field: str,
    issues: list[ValidationIssue],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = str(row.get(id_field, ""))
        if not record_id:
            issues.append(
                ValidationIssue(
                    "missing_id",
                    f"{collection[:-1]} row missing {id_field}",
                    collection,
                    field=id_field,
                )
            )
            continue
        if record_id in indexed:
            issues.append(
                ValidationIssue(
                    "duplicate_id",
                    f"duplicate {collection[:-1]} {id_field}: {record_id}",
                    collection,
                    record_id,
                    id_field,
                )
            )
        indexed[record_id] = row
    return indexed


def validate_metadata(
    *,
    root: Path,
    assets: list[dict[str, Any]],
    edits: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    generation_results: list[dict[str, Any]] | None = None,
    screening_results: list[dict[str, Any]] | None = None,
    require_metadata_files: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    metadata = root / "metadata"

    if require_metadata_files:
        for filename in METADATA_FILES.values():
            path = metadata / filename
            if not path.exists():
                issues.append(
                    ValidationIssue(
                        "missing_metadata_file",
                        f"missing metadata file: {path.relative_to(root)}",
                        field=filename,
                    )
                )

    asset_by_id = _id_map(assets, collection="assets", id_field="asset_id", issues=issues)
    edit_by_id = _id_map(edits, collection="edits", id_field="edit_id", issues=issues)
    candidate_by_id = _id_map(
        candidates,
        collection="candidates",
        id_field="candidate_id",
        issues=issues,
    )
    sample_by_id = _id_map(samples, collection="samples", id_field="sample_id", issues=issues)
    question_by_id = _id_map(
        questions,
        collection="questions",
        id_field="question_id",
        issues=issues,
    )
    screening_by_id = _id_map(
        screening_results or [],
        collection="screening_results",
        id_field="screening_id",
        issues=issues,
    )
    generation_by_id = _id_map(
        generation_results or [],
        collection="generation_results",
        id_field="generation_id",
        issues=issues,
    )

    for asset_id, asset in asset_by_id.items():
        rel_path = str(asset.get("path", ""))
        if not rel_path:
            issues.append(
                ValidationIssue("missing_path", f"asset {asset_id} missing path", "assets", asset_id, "path")
            )
        elif not (root / rel_path).exists():
            issues.append(
                ValidationIssue(
                    "missing_asset_file",
                    f"asset {asset_id} file missing: {rel_path}",
                    "assets",
                    asset_id,
                    "path",
                )
            )

    for edit_id, edit in edit_by_id.items():
        source_asset_id = str(edit.get("source_asset_id", ""))
        if source_asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"edit {edit_id} references unknown source asset: {source_asset_id}",
                    "edits",
                    edit_id,
                    "source_asset_id",
                )
            )

    for candidate_id, candidate in candidate_by_id.items():
        edit_id = str(candidate.get("edit_id", ""))
        asset_id = str(candidate.get("candidate_asset_id", ""))
        if edit_id not in edit_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_edit",
                    f"candidate {candidate_id} references unknown edit: {edit_id}",
                    "candidates",
                    candidate_id,
                    "edit_id",
                )
            )
        if asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"candidate {candidate_id} references unknown asset: {asset_id}",
                    "candidates",
                    candidate_id,
                    "candidate_asset_id",
                )
            )
        for screening_id in candidate.get("screening_result_ids") or []:
            if str(screening_id) not in screening_by_id:
                issues.append(
                    ValidationIssue(
                        "unknown_screening_result",
                        f"candidate {candidate_id} references unknown screening result: {screening_id}",
                        "candidates",
                        candidate_id,
                        "screening_result_ids",
                    )
                )

    for sample_id, sample in sample_by_id.items():
        source_asset_id = str(sample.get("source_asset_id", ""))
        if source_asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"sample {sample_id} references unknown source asset: {source_asset_id}",
                    "samples",
                    sample_id,
                    "source_asset_id",
                )
            )
        for edit_id in sample.get("edit_ids") or []:
            if str(edit_id) not in edit_by_id:
                issues.append(
                    ValidationIssue(
                        "unknown_edit",
                        f"sample {sample_id} references unknown edit: {edit_id}",
                        "samples",
                        sample_id,
                        "edit_ids",
                    )
                )
        edited_asset_id = str(sample.get("accepted_edited_asset_id", ""))
        if edited_asset_id and edited_asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"sample {sample_id} references unknown edited asset: {edited_asset_id}",
                    "samples",
                    sample_id,
                    "accepted_edited_asset_id",
                )
            )
        candidate_id = str(sample.get("accepted_candidate_id", ""))
        if candidate_id and candidate_id not in candidate_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_candidate",
                    f"sample {sample_id} references unknown candidate: {candidate_id}",
                    "samples",
                    sample_id,
                    "accepted_candidate_id",
                )
            )
        for question_id in sample.get("question_ids") or []:
            if str(question_id) not in question_by_id:
                issues.append(
                    ValidationIssue(
                        "unknown_question",
                        f"sample {sample_id} references unknown question: {question_id}",
                        "samples",
                        sample_id,
                        "question_ids",
                    )
                )

    for question_id, question in question_by_id.items():
        sample_id = str(question.get("sample_id", ""))
        asset_id = str(question.get("image_asset_id", ""))
        edit_id = str(question.get("edit_id", ""))
        if sample_id and sample_id not in sample_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_sample",
                    f"question {question_id} references unknown sample: {sample_id}",
                    "questions",
                    question_id,
                    "sample_id",
                )
            )
        if asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"question {question_id} references unknown asset: {asset_id}",
                    "questions",
                    question_id,
                    "image_asset_id",
                )
            )
        if edit_id and edit_id not in edit_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_edit",
                    f"question {question_id} references unknown edit: {edit_id}",
                    "questions",
                    question_id,
                    "edit_id",
                )
            )
        if not str(question.get("question_type", "")):
            issues.append(
                ValidationIssue(
                    "missing_question_type",
                    f"question {question_id} missing question_type",
                    "questions",
                    question_id,
                    "question_type",
                )
            )
        if not str(question.get("eval_type", "")):
            issues.append(
                ValidationIssue(
                    "missing_eval_type",
                    f"question {question_id} missing eval_type",
                    "questions",
                    question_id,
                    "eval_type",
                )
            )

    for screening_id, screening in screening_by_id.items():
        candidate_id = str(screening.get("candidate_id", ""))
        sample_id = str(screening.get("sample_id", ""))
        if candidate_id not in candidate_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_candidate",
                    f"screening result {screening_id} references unknown candidate: {candidate_id}",
                    "screening_results",
                    screening_id,
                    "candidate_id",
                )
            )
        if sample_id and sample_id not in sample_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_sample",
                    f"screening result {screening_id} references unknown sample: {sample_id}",
                    "screening_results",
                    screening_id,
                    "sample_id",
                )
            )
        for prediction in screening.get("predictions") or []:
            question_id = str(prediction.get("question_id", ""))
            if question_id and question_id not in question_by_id:
                issues.append(
                    ValidationIssue(
                        "unknown_question",
                        f"screening result {screening_id} references unknown question: {question_id}",
                        "screening_results",
                        screening_id,
                        "predictions",
                    )
                )
        model_correct = screening.get("model_correct")
        decision = str(screening.get("decision", ""))
        if model_correct is True and decision != "rejected_correct":
            issues.append(
                ValidationIssue(
                    "invalid_screening_decision",
                    f"screening result {screening_id} must reject a correct model response",
                    "screening_results",
                    screening_id,
                    "decision",
                )
            )
        if model_correct is False and decision != "retained_failure":
            issues.append(
                ValidationIssue(
                    "invalid_screening_decision",
                    f"screening result {screening_id} must retain a model failure",
                    "screening_results",
                    screening_id,
                    "decision",
                )
            )

    for generation_id, generation in generation_by_id.items():
        status = str(generation.get("status", ""))
        asset_id = str(generation.get("asset_id", ""))
        error = str(generation.get("error", ""))
        if status not in {"ok", "failed"}:
            issues.append(
                ValidationIssue(
                    "invalid_generation_status",
                    f"generation result {generation_id} has invalid status: {status}",
                    "generation_results",
                    generation_id,
                    "status",
                )
            )
        if status == "ok" and asset_id not in asset_by_id:
            issues.append(
                ValidationIssue(
                    "unknown_asset",
                    f"generation result {generation_id} references unknown asset: {asset_id}",
                    "generation_results",
                    generation_id,
                    "asset_id",
                )
            )
        if status == "failed" and not error:
            issues.append(
                ValidationIssue(
                    "missing_generation_error",
                    f"failed generation result {generation_id} has no error",
                    "generation_results",
                    generation_id,
                    "error",
                )
            )

    return issues
