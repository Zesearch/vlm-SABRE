#!/usr/bin/env python3
"""Serve the generic benchmark review and patch-repair interface."""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import math
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from vlmbench.data_model import MetadataRepository, upsert_by_key, write_jsonl as write_jsonl_atomic
from vlmbench.campaign import CampaignManager
from vlmbench.data_model import HumanReviewDecision


TOOL = Path(__file__).resolve().parent
STATIC_ROOT = TOOL / "static"
PROJECT_ROOT = TOOL.parents[4]
BENCHMARK_ROOT = PROJECT_ROOT
DATASET_ROOT = PROJECT_ROOT
MANIFEST = DATASET_ROOT / "review_manifest.json"
DECISIONS = DATASET_ROOT / "review_decisions.jsonl"
REPAIRS = DATASET_ROOT / "repairs"
UPLOAD_ROOT = TOOL / "repair_data" / "upload_repairs"
UPLOAD_SOURCES = UPLOAD_ROOT / "sources"
UPLOAD_REPAIRS = UPLOAD_ROOT / "repairs"
AUTHORING_INPUT = BENCHMARK_ROOT / "real_images"
AUTHORING_OUTPUT = BENCHMARK_ROOT / "real_authoring_dataset"
AUTHORING_STATE = AUTHORING_OUTPUT / "authoring.jsonl"
AUTHORING_CANDIDATES = AUTHORING_OUTPUT / "authoring_candidates"
AUTHORING_IMAGES = AUTHORING_OUTPUT / "images"
LETTERS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
AUTHORING_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
AUTHORING_UPLOAD_MAX_PIXELS = 80_000_000
AUTHORING_UPLOAD_LOCK = threading.Lock()
INCLUDE_GENERATED = False

REVIEW_UI_STATUSES = frozenset(
    {
        "keep",
        "needs_repair",
        "reject_edit_failed",
        "reject_base_invalid",
        "unsure",
    }
)
LEGACY_REVIEW_UI_STATUSES = {
    "needs_recheck": "needs_repair",
    "reject_scene_changed": "reject_edit_failed",
}
REJECTION_REASONS = {
    "reject_edit_failed": "edit_failed",
    "reject_base_invalid": "base_invalid",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def canonical_review_enabled() -> bool:
    repository = MetadataRepository(DATASET_ROOT)
    return (
        repository.exists("assets")
        and repository.exists("samples")
        and repository.exists("candidates")
        and repository.exists("questions")
    )


def review_queue_statuses() -> set[str]:
    statuses = {"retained_failure", "human_review", "needs_repair"}
    if INCLUDE_GENERATED:
        statuses.add("candidate_ready")
    return statuses


def review_manifest_rows() -> list[dict[str, Any]]:
    if canonical_review_enabled():
        repository = MetadataRepository(DATASET_ROOT)
        manager = CampaignManager(repository)
        rows: list[dict[str, Any]] = []
        campaigns = repository.load("campaigns")
        if campaigns:
            for campaign in campaigns:
                rows.extend(
                    manager.review_items(
                        str(campaign.get("campaign_id", "")),
                        include_generated=INCLUDE_GENERATED,
                    )
                )
        else:
            rows.extend(manager.review_items(include_generated=INCLUDE_GENERATED))
        return rows
    if not MANIFEST.exists():
        return []
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Review manifest must contain a JSON list: {MANIFEST}")
    return payload


def _normalized_ui_status(status: Any) -> str:
    value = str(status or "unsure").strip() or "unsure"
    value = LEGACY_REVIEW_UI_STATUSES.get(value, value)
    if value not in REVIEW_UI_STATUSES:
        allowed = ", ".join(sorted(REVIEW_UI_STATUSES))
        raise ValueError(f"Invalid review status {value!r}; expected one of: {allowed}")
    return value


def _normalized_review_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    original_status = str(payload.get("status") or "unsure").strip() or "unsure"
    status = _normalized_ui_status(original_status)
    if original_status != status:
        payload.setdefault("legacy_status", original_status)
    payload["status"] = status

    if payload.get("repair_status") == "needs_recheck":
        payload.setdefault("legacy_repair_status", "needs_recheck")
        payload["repair_status"] = "needs_repair"

    payload["reason"] = REJECTION_REASONS.get(status, "")
    return payload


def review_decision_rows() -> list[dict[str, Any]]:
    if not canonical_review_enabled():
        return [_normalized_review_payload(row) for row in load_jsonl(DECISIONS)]
    status_map = {
        HumanReviewDecision.ACCEPTED.value: "keep",
        HumanReviewDecision.REJECTED.value: "reject_edit_failed",
        HumanReviewDecision.NEEDS_REPAIR.value: "needs_repair",
        HumanReviewDecision.PENDING.value: "unsure",
    }
    output = []
    for row in MetadataRepository(DATASET_ROOT).load("human_reviews"):
        metadata = row.get("metadata") or {}
        payload = dict(metadata.get("ui_payload") or {})
        payload["status"] = metadata.get("ui_status") or status_map.get(
            str(row.get("decision", "")), "unsure"
        )
        payload = _normalized_review_payload(payload)
        payload.update(
            {
                "review_id": row.get("review_id", ""),
                "reason": payload.get("reason") or row.get("reason", ""),
                "notes": row.get("notes", ""),
                "updated_at": row.get("updated_at", ""),
            }
        )
        output.append(payload)
    return output


def _normalized_review_decision(status: str) -> HumanReviewDecision:
    status = _normalized_ui_status(status)
    if status == "keep":
        return HumanReviewDecision.ACCEPTED
    if status.startswith("reject_"):
        return HumanReviewDecision.REJECTED
    if status == "needs_repair":
        return HumanReviewDecision.NEEDS_REPAIR
    return HumanReviewDecision.PENDING


def update_review_questions(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist human edits to canonical questions.jsonl."""

    if not canonical_review_enabled():
        raise ValueError(
            "Question editing requires a canonical dataset with metadata/questions.jsonl."
        )
    review_id = str(payload.get("review_id", "")).strip()
    submitted = payload.get("questions")
    if not review_id:
        raise ValueError("review_id is required")
    if not isinstance(submitted, list) or not submitted:
        raise ValueError("questions must be a non-empty list")

    repository = MetadataRepository(DATASET_ROOT)
    sample = next(
        (
            row
            for row in repository.load("samples")
            if str(row.get("sample_id", "")) == review_id
        ),
        None,
    )
    if sample is None:
        raise ValueError(f"Unknown review_id: {review_id}")
    if str(sample.get("status", "")) not in review_queue_statuses():
        raise ValueError(f"Sample is not editable in the review queue: {review_id}")

    questions = repository.load("questions")
    sample_question_ids = {
        str(value) for value in (sample.get("question_ids") or []) if str(value)
    }
    if not sample_question_ids:
        sample_question_ids = {
            str(row.get("question_id", ""))
            for row in questions
            if str(row.get("sample_id", "")) == review_id
        }
    submitted_ids = [str(row.get("question_id", "")).strip() for row in submitted]
    if any(not question_id for question_id in submitted_ids):
        raise ValueError("Every edited question must include question_id")
    if len(submitted_ids) != len(set(submitted_ids)):
        raise ValueError("Duplicate question_id in question update")
    if set(submitted_ids) != sample_question_ids:
        raise ValueError(
            "Question update must include exactly the existing questions for this sample."
        )

    edits_by_id = {str(row["question_id"]): row for row in submitted}
    now = utc_now()
    changed_ids: list[str] = []
    updated_questions: list[dict[str, Any]] = []
    for row in questions:
        question_id = str(row.get("question_id", ""))
        edit = edits_by_id.get(question_id)
        if edit is None:
            updated_questions.append(row)
            continue
        if str(row.get("sample_id", "")) != review_id:
            raise ValueError(f"Question does not belong to sample {review_id}: {question_id}")
        prompt = str(edit.get("prompt", edit.get("question", ""))).strip()
        answer = str(edit.get("answer", "")).strip()
        if not prompt:
            raise ValueError(f"Question text cannot be empty: {question_id}")
        question_type = str(row.get("question_type", ""))
        if question_type == "yes_no":
            answer = answer.lower()
            if answer not in {"yes", "no"}:
                raise ValueError(f"Yes/no answer must be yes or no: {question_id}")
        elif question_type == "multiple_choice":
            answer = answer.upper()
            options = row.get("options") or {}
            if answer not in {str(key).upper() for key in options}:
                raise ValueError(
                    f"Multiple-choice answer must be an existing option key: {question_id}"
                )
        elif not answer:
            raise ValueError(f"Answer cannot be empty: {question_id}")

        previous_prompt = str(row.get("prompt", ""))
        previous_answer = row.get("answer")
        if prompt != previous_prompt or answer != str(previous_answer):
            metadata = dict(row.get("metadata") or {})
            history = list(metadata.get("question_edit_history") or [])
            history.append(
                {
                    "edited_at": now,
                    "previous_prompt": previous_prompt,
                    "previous_answer": previous_answer,
                    "prompt": prompt,
                    "answer": answer,
                }
            )
            metadata.update(
                {
                    "human_edited": True,
                    "last_human_edit_at": now,
                    "question_edit_history": history,
                }
            )
            row = {**row, "prompt": prompt, "answer": answer, "updated_at": now, "metadata": metadata}
            changed_ids.append(question_id)
        updated_questions.append(row)

    repository.write(
        "questions",
        sorted(updated_questions, key=lambda row: str(row.get("question_id", ""))),
    )
    if changed_ids:
        screenings = repository.load("screening_results")
        for screening in screenings:
            if str(screening.get("sample_id", "")) != review_id:
                continue
            metadata = dict(screening.get("metadata") or {})
            metadata.update(
                {
                    "question_content_edited_after_screening": True,
                    "question_content_last_edited_at": now,
                    "edited_question_ids": sorted(
                        set(metadata.get("edited_question_ids") or []) | set(changed_ids)
                    ),
                }
            )
            screening["metadata"] = metadata
        repository.write("screening_results", screenings)

    scope = sample.get("metadata") or {}
    campaign_id = str(scope.get("campaign_id", ""))
    refreshed = CampaignManager(repository).review_items(
        campaign_id or None,
        include_generated=INCLUDE_GENERATED,
    )
    item = next((row for row in refreshed if row["review_id"] == review_id), None)
    if item is None:
        raise ValueError(f"Updated sample is no longer available in review queue: {review_id}")
    return {
        "review_id": review_id,
        "changed_question_ids": changed_ids,
        "probes": item["probes"],
        "updated_at": now,
    }


def write_decisions(rows: dict[str, dict[str, Any]]) -> None:
    normalized_rows = {
        review_id: _normalized_review_payload(row)
        for review_id, row in rows.items()
    }
    if canonical_review_enabled():
        manager = CampaignManager(MetadataRepository(DATASET_ROOT))
        samples = {
            str(row.get("sample_id", "")): str(row.get("status", ""))
            for row in manager.repository.load("samples")
        }
        existing_review_metadata = {
            str(row.get("sample_id", "")): row.get("metadata") or {}
            for row in manager.repository.load("human_reviews")
        }
        queue_statuses = review_queue_statuses()
        for review_id, row in normalized_rows.items():
            original_status = samples.get(review_id)
            if original_status not in queue_statuses:
                continue
            prior_metadata = existing_review_metadata.get(review_id, {})
            entry_source = str(
                prior_metadata.get("entry_source")
                or (
                    "generated_without_screening"
                    if original_status == "candidate_ready"
                    else "screened_review_queue"
                )
            )
            filter_gate_bypassed = bool(
                prior_metadata.get("filter_gate_bypassed")
                or original_status == "candidate_ready"
            )
            manager.record_review(
                review_id=review_id,
                decision=_normalized_review_decision(row["status"]),
                reason=str(row["reason"]),
                notes=str(row.get("notes", "")),
                metadata={
                    "ui_status": row["status"],
                    "ui_payload": row,
                    "entry_source": entry_source,
                    "filter_gate_bypassed": filter_gate_bypassed,
                },
                allow_generated=INCLUDE_GENERATED,
            )
        return
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="review_", suffix=".jsonl", dir=DECISIONS.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for review_id in sorted(normalized_rows):
            handle.write(json.dumps(normalized_rows[review_id], ensure_ascii=False) + "\n")
    os.replace(name, DECISIONS)


def save_image_as_target(source: Path, target: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=f"{target.stem}__", suffix=target.suffix or ".jpg", dir=target.parent)
    os.close(fd)
    temp_path = Path(name)
    try:
        suffix = (target.suffix or ".jpg").lower()
        with Image.open(source) as image:
            if suffix in {".jpg", ".jpeg"}:
                image = image.convert("RGB")
                image.save(temp_path, quality=95)
            else:
                image.save(temp_path)
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    return "".join(ch if ch in LETTERS else "_" for ch in value)[:180]


def resolve_key(name: str) -> str:
    filename = "gemini_api_key.txt" if name == "GEMINI_API_KEY" else ""
    candidates = [parent / filename for parent in [TOOL, DATASET_ROOT, BENCHMARK_ROOT, *BENCHMARK_ROOT.parents] if filename]
    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8").strip().strip("\"'")
            if "=" in key:
                key = key.split("=", 1)[1].strip()
            if key:
                return key
    key = os.environ.get(name, "").strip().strip("\"'")
    if key:
        return key
    raise EnvironmentError(f"Set {name} or place {filename} in this folder or a parent folder.")


def rel_to_dataset(path: Path) -> str:
    return path.resolve().relative_to(DATASET_ROOT.resolve()).as_posix()


def rel_to_tool(path: Path) -> str:
    return path.resolve().relative_to(TOOL.resolve()).as_posix()


def dataset_path(rel_path: str) -> Path:
    path = (DATASET_ROOT / rel_path).resolve()
    try:
        path.relative_to(DATASET_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes dataset root: {rel_path}") from exc
    return path


def tool_path(rel_path: str) -> Path:
    path = (TOOL / rel_path).resolve()
    try:
        path.relative_to(TOOL.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes tool root: {rel_path}") from exc
    return path


def resolve_dataset_file(dataset_root: Path, filename: str) -> Path:
    primary = dataset_root / filename
    legacy = dataset_root / "tools" / "review_repair" / filename
    return primary if primary.exists() or not legacy.exists() else legacy


def configure_paths(dataset_root: Path, manifest: str, decisions: str, repairs: str) -> None:
    global DATASET_ROOT, MANIFEST, DECISIONS, REPAIRS
    DATASET_ROOT = dataset_root.resolve()
    MANIFEST = resolve_dataset_file(DATASET_ROOT, manifest)
    DECISIONS = (DATASET_ROOT / decisions).resolve()
    REPAIRS = (DATASET_ROOT / repairs).resolve()


def configure_review_mode(*, include_generated: bool = False) -> None:
    global INCLUDE_GENERATED
    INCLUDE_GENERATED = include_generated


def configure_authoring(authoring_input: Path, authoring_output: Path) -> None:
    global AUTHORING_INPUT, AUTHORING_OUTPUT, AUTHORING_STATE, AUTHORING_CANDIDATES, AUTHORING_IMAGES
    AUTHORING_INPUT = authoring_input.resolve()
    AUTHORING_OUTPUT = authoring_output.resolve()
    AUTHORING_STATE = AUTHORING_OUTPUT / "authoring.jsonl"
    AUTHORING_CANDIDATES = AUTHORING_OUTPUT / "authoring_candidates"
    AUTHORING_IMAGES = AUTHORING_OUTPUT / "images"


def rel_to_authoring_output(path: Path) -> str:
    return path.resolve().relative_to(AUTHORING_OUTPUT.resolve()).as_posix()


def authoring_output_path(rel_path: str) -> Path:
    path = (AUTHORING_OUTPUT / rel_path).resolve()
    try:
        path.relative_to(AUTHORING_OUTPUT.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes authoring output root: {rel_path}") from exc
    return path


def authoring_input_path(filename: str) -> Path:
    path = (AUTHORING_INPUT / filename).resolve()
    try:
        path.relative_to(AUTHORING_INPUT.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes authoring input root: {filename}") from exc
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported authoring image type: {filename}")
    return path


def authoring_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(AUTHORING_STATE):
        records[str(row["original_filename"])] = row
    return records


def latest_authoring_candidates() -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    if not AUTHORING_CANDIDATES.exists():
        return candidates
    for path in AUTHORING_CANDIDATES.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if "__edited_candidate" not in path.name or "__patch_repaired" in path.name:
            continue
        item_id = path.name.split("__", 1)[0]
        stat = path.stat()
        rel_path = rel_to_authoring_output(path)
        sidecar = path.with_suffix(path.suffix + ".json")
        metadata = {}
        if sidecar.exists():
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        row = {
            "candidate_image": rel_path,
            "candidate_url": f"/authoring_output/{quote(rel_path)}",
            "candidate_filename": path.name,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "mtime": stat.st_mtime,
            "metadata": metadata,
        }
        if item_id not in candidates or stat.st_mtime > float(candidates[item_id].get("mtime", 0)):
            candidates[item_id] = row
    for row in candidates.values():
        row.pop("mtime", None)
    return candidates


def authoring_match_thumbnail(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.draft("RGB", (128, 128))
        image = image.convert("RGB")
        image.thumbnail((128, 128))
        thumb = Image.new("RGB", (128, 128), (0, 0, 0))
        thumb.paste(image, ((128 - image.width) // 2, (128 - image.height) // 2))
        return thumb


def image_mean_abs_diff(left: Image.Image, right: Image.Image) -> float:
    diff = ImageChops.difference(left, right)
    return float(sum(ImageStat.Stat(diff).mean) / 3.0)


def visually_match_authoring_candidates(files: list[Path], candidates: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    scores: list[tuple[float, str, str]] = []
    base_thumbs: dict[str, Image.Image] = {}
    candidate_thumbs: dict[str, Image.Image] = {}
    for index, base in enumerate(files, start=1):
        item_id = f"real_{index:03d}"
        try:
            base_thumbs[item_id] = authoring_match_thumbnail(base)
        except Exception:
            continue
    for candidate_id, row in candidates.items():
        try:
            candidate_thumbs[candidate_id] = authoring_match_thumbnail(authoring_output_path(str(row["candidate_image"])))
        except Exception:
            continue
    for index, base in enumerate(files, start=1):
        item_id = f"real_{index:03d}"
        base_thumb = base_thumbs.get(item_id)
        if base_thumb is None:
            continue
        for candidate_id, row in candidates.items():
            candidate_thumb = candidate_thumbs.get(candidate_id)
            if candidate_thumb is None:
                continue
            score = image_mean_abs_diff(base_thumb, candidate_thumb)
            scores.append((score, item_id, candidate_id))

    matched_items: set[str] = set()
    matched_candidates: set[str] = set()
    matches: dict[str, dict[str, Any]] = {}
    for score, item_id, candidate_id in sorted(scores, key=lambda row: row[0]):
        if score > 30:
            break
        if item_id in matched_items or candidate_id in matched_candidates:
            continue
        row = dict(candidates[candidate_id])
        row["match_source_id"] = candidate_id
        row["match_score"] = score
        row["match_mode"] = "visual" if candidate_id != item_id else "id"
        matches[item_id] = row
        matched_items.add(item_id)
        matched_candidates.add(candidate_id)
    return matches


def write_authoring_records(records: dict[str, dict[str, Any]]) -> None:
    AUTHORING_OUTPUT.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="authoring_", suffix=".jsonl", dir=AUTHORING_OUTPUT)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for filename in sorted(records):
            handle.write(json.dumps(records[filename], ensure_ascii=False) + "\n")
    os.replace(name, AUTHORING_STATE)


def authoring_metadata_dir() -> Path:
    return MetadataRepository(AUTHORING_OUTPUT).metadata


def authoring_metadata_enabled() -> bool:
    repository = MetadataRepository(AUTHORING_OUTPUT)
    return repository.exists("assets") and repository.exists("samples")


def load_authoring_metadata(name: str) -> list[dict[str, Any]]:
    return MetadataRepository(AUTHORING_OUTPUT).load(name)


def write_authoring_metadata(name: str, rows: list[dict[str, Any]]) -> None:
    MetadataRepository(AUTHORING_OUTPUT).write(name, rows)


def asset_url(asset: dict[str, Any]) -> str:
    return f"/authoring_output/{quote(str(asset['path']))}"


def image_info(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        width, height = image.size
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
    return width, height, mime


def authoring_source_path_from_payload(payload: dict[str, Any]) -> Path:
    rel = str(payload.get("source_image") or "").strip()
    if rel:
        return authoring_output_path(rel)
    return authoring_input_path(str(payload["original_filename"]))


def metadata_questions_to_legacy(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for question in questions:
        metadata = question.get("metadata") or {}
        image_role = str(question.get("image_role") or "")
        rows.append(
            {
                "id": question.get("question_id", ""),
                "pair_id": question.get("sample_id", ""),
                "probe": metadata.get("probe") or safe_name(str(question.get("question_id", ""))),
                "image_role": "edited" if image_role == "edited" else "base",
                "question": question.get("prompt", ""),
                "answer": question.get("answer", ""),
                "question_type": question.get("question_type", "yes_no"),
                "eval_type": question.get("eval_type", "yes_no_exact"),
            }
        )
    return rows


def normalize_flexible_authoring_questions(record: dict[str, Any], questions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = questions or default_authoring_questions(record)
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        question = str(row.get("question") or row.get("prompt") or "").strip()
        if not question:
            raise ValueError(f"Question {index} is empty.")
        question_type = str(row.get("question_type") or "yes_no").strip() or "yes_no"
        eval_type = str(row.get("eval_type") or "").strip()
        if not eval_type:
            if question_type == "yes_no":
                eval_type = "yes_no_exact"
            elif question_type == "multiple_choice":
                eval_type = "choice_exact"
            else:
                eval_type = "manual"
        answer = str(row.get("answer") or "").strip()
        if question_type == "yes_no":
            answer = answer.lower()
            if answer not in {"yes", "no"}:
                raise ValueError(f"Question {index} yes/no answer must be yes or no.")
        elif eval_type in {"choice_exact", "contains"} and not answer:
            raise ValueError(f"Question {index} answer is required for {eval_type}.")
        image_role = str(row.get("image_role") or "base")
        probe = safe_name(str(row.get("probe") or f"custom_{index:02d}")) or f"custom_{index:02d}"
        normalized.append(
            {
                "probe": probe,
                "image_role": "edited" if image_role == "edited" else "base",
                "question_type": question_type,
                "question": question,
                "answer": answer,
                "eval_type": eval_type,
            }
        )
    return normalized


def legacy_questions_to_metadata(
    *,
    sample_id: str,
    edit_id: str,
    source_asset_id: str,
    edited_asset_id: str,
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(questions, start=1):
        probe = safe_name(str(row.get("probe") or f"q{index:02d}")) or f"q{index:02d}"
        role = "edited" if str(row.get("image_role") or "base") == "edited" else "source"
        rows.append(
            {
                "question_id": f"{sample_id}__{probe}",
                "sample_id": sample_id,
                "edit_id": edit_id,
                "image_asset_id": edited_asset_id if role == "edited" else source_asset_id,
                "image_role": role,
                "question_type": str(row.get("question_type") or "yes_no"),
                "prompt": str(row.get("question") or row.get("prompt") or "").strip(),
                "answer": str(row.get("answer") or "").strip().lower()
                if str(row.get("question_type") or "yes_no") == "yes_no"
                else str(row.get("answer") or "").strip(),
                "eval_type": str(row.get("eval_type") or "yes_no_exact"),
                "metadata": {"probe": probe},
            }
        )
    return rows


def new_authoring_items() -> dict[str, Any]:
    assets = load_authoring_metadata("assets")
    edits = load_authoring_metadata("edits")
    candidates = load_authoring_metadata("candidates")
    samples = load_authoring_metadata("samples")
    questions = load_authoring_metadata("questions")
    asset_by_id = {str(row.get("asset_id", "")): row for row in assets}
    edit_by_id = {str(row.get("edit_id", "")): row for row in edits}
    candidate_by_id = {str(row.get("candidate_id", "")): row for row in candidates}
    candidates_by_edit: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidates_by_edit.setdefault(str(candidate.get("edit_id", "")), []).append(candidate)
    questions_by_sample: dict[str, list[dict[str, Any]]] = {}
    for question in questions:
        questions_by_sample.setdefault(str(question.get("sample_id", "")), []).append(question)

    items: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda row: str(row.get("sample_id", ""))):
        sample_id = str(sample.get("sample_id") or "")
        if str(sample.get("status") or "") in {"retired", "deleted", "rejected"}:
            continue
        source_asset = asset_by_id.get(str(sample.get("source_asset_id") or ""))
        if not sample_id or not source_asset:
            continue
        edit_ids = [str(value) for value in (sample.get("edit_ids") or [])]
        edit = edit_by_id.get(edit_ids[0], {}) if edit_ids else {}
        edit_metadata = edit.get("metadata") or {}
        accepted_asset = asset_by_id.get(str(sample.get("accepted_edited_asset_id") or ""))
        accepted_candidate = candidate_by_id.get(str(sample.get("accepted_candidate_id") or ""))
        latest_candidate = accepted_candidate or {}
        if not latest_candidate and edit:
            pool = candidates_by_edit.get(str(edit.get("edit_id", "")), [])
            if pool:
                latest_candidate = sorted(pool, key=lambda row: str(row.get("created_at", "")))[-1]
        candidate_payload: dict[str, Any] = {}
        if latest_candidate:
            candidate_asset = asset_by_id.get(str(latest_candidate.get("candidate_asset_id") or ""))
            if candidate_asset:
                candidate_payload = {
                    "candidate_image": candidate_asset["path"],
                    "candidate_url": asset_url(candidate_asset),
                    "candidate_filename": Path(str(candidate_asset["path"])).name,
                    "updated_at": latest_candidate.get("created_at", ""),
                    "metadata": {"edit": {"bbox": edit.get("bbox"), **edit_metadata}},
                }

        original_filename = str(
            (sample.get("metadata") or {}).get("original_filename")
            or source_asset.get("source_filename")
            or Path(str(source_asset["path"])).name
        )
        record: dict[str, Any] = {
            "id": sample_id,
            "original_filename": original_filename,
            "source_image": source_asset["path"],
            "base_image": source_asset["path"],
            "base_url": asset_url(source_asset),
            "source_entity": edit_metadata.get("source_entity", ""),
            "target_entity": edit_metadata.get("target_entity", ""),
            "scene_description": edit_metadata.get("scene_description", ""),
            "review_location": edit_metadata.get("review_location", ""),
            "source_bbox": edit.get("bbox"),
            "edit_type": edit.get("edit_type", "swap_entity"),
            "prompt": edit.get("instruction", ""),
            "questions": metadata_questions_to_legacy(questions_by_sample.get(sample_id, [])),
            "created_at": sample.get("created_at", ""),
            "updated_at": sample.get("updated_at", ""),
        }
        if accepted_asset:
            record.update(
                {
                    "edited_image": accepted_asset["path"],
                    "edited_url": asset_url(accepted_asset),
                    "candidate_image": candidate_payload.get("candidate_image", accepted_asset["path"]),
                }
            )
        items.append(
            {
                "id": sample_id,
                "original_filename": original_filename,
                "source_image": source_asset["path"],
                "source_url": asset_url(source_asset),
                "saved": bool(accepted_asset),
                "record": record,
                "latest_candidate": candidate_payload,
            }
        )
    return {
        "items": items,
        "input_root": str(AUTHORING_INPUT),
        "output_root": str(AUTHORING_OUTPUT),
        "state_file": str(authoring_metadata_dir()),
        "storage": "metadata",
    }


def next_candidate_index(candidates: list[dict[str, Any]], sample_id: str) -> int:
    prefix = f"{sample_id}_cand_"
    values = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", ""))
        if candidate_id.startswith(prefix):
            try:
                values.append(int(candidate_id.rsplit("_", 1)[1]))
            except ValueError:
                pass
    return (max(values) + 1) if values else 1


def copy_candidate_into_metadata_assets(
    *,
    sample_id: str,
    source_filename: str,
    candidate_path: Path,
    candidate_index: int,
) -> dict[str, Any]:
    asset_id = f"{sample_id}_candidate_{candidate_index:03d}"
    target = AUTHORING_OUTPUT / "assets" / "candidates" / sample_id / f"candidate_{candidate_index:03d}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    if candidate_path.resolve() != target.resolve():
        save_image_as_target(candidate_path, target)
    width, height, mime = image_info(target)
    return {
        "asset_id": asset_id,
        "kind": "candidate_image",
        "path": rel_to_authoring_output(target),
        "mime_type": mime,
        "width": width,
        "height": height,
        "source_filename": source_filename,
        "created_at": utc_now(),
        "metadata": {},
    }


def sync_new_authoring_candidate(payload: dict[str, Any], edit_record: dict[str, Any]) -> dict[str, Any]:
    if not authoring_metadata_enabled():
        return edit_record
    sample_id = safe_name(str(payload.get("id") or edit_record.get("edit_id") or "")) or safe_name(Path(str(payload.get("original_filename", "real"))).stem)
    assets = load_authoring_metadata("assets")
    edits = load_authoring_metadata("edits")
    candidates = load_authoring_metadata("candidates")
    samples = load_authoring_metadata("samples")
    sample = next((row for row in samples if str(row.get("sample_id", "")) == sample_id), None)
    if not sample:
        raise ValueError(f"Unknown metadata sample_id: {sample_id}")
    source_asset_id = str(sample.get("source_asset_id") or "")
    edit_id = str((sample.get("edit_ids") or [f"{sample_id}_edit_001"])[0])
    source_filename = str(payload.get("original_filename") or "")
    candidate_index = next_candidate_index(candidates, sample_id)
    candidate_path = authoring_output_path(str(edit_record["candidate_image"]))
    candidate_asset = copy_candidate_into_metadata_assets(
        sample_id=sample_id,
        source_filename=source_filename,
        candidate_path=candidate_path,
        candidate_index=candidate_index,
    )
    candidate_id = f"{sample_id}_cand_{candidate_index:03d}"
    now = utc_now()
    metadata = {
        "source_entity": str(payload.get("source_entity") or "").strip(),
        "target_entity": str(payload.get("target_entity") or "").strip(),
        "scene_description": str(payload.get("scene_description") or "").strip(),
        "review_location": str(payload.get("review_location") or "").strip(),
    }
    edit = {
        "edit_id": edit_id,
        "source_asset_id": source_asset_id,
        "edit_type": str(payload.get("edit_type") or "swap_entity"),
        "instruction": str(payload.get("prompt") or "").strip(),
        "bbox": payload.get("bbox"),
        "metadata": metadata,
        "created_at": str(next((row.get("created_at") for row in edits if row.get("edit_id") == edit_id), "")) or now,
        "updated_at": now,
    }
    candidate = {
        "candidate_id": candidate_id,
        "edit_id": edit_id,
        "candidate_asset_id": candidate_asset["asset_id"],
        "generator": {
            "provider": "google",
            "model": str(payload.get("model") or "gemini-3.1-flash-image-preview"),
            "method": "patch_repair_soft_blend",
        },
        "status": "candidate",
        "prompt": str(edit_record.get("prompt") or ""),
        "artifacts": {
            key: value
            for key, value in edit_record.items()
            if key.endswith("_image") or key.endswith("_path")
        },
        "created_at": now,
        "metadata": {"edit_type": edit.get("edit_type", "")},
    }
    for row in samples:
        if str(row.get("sample_id", "")) == sample_id and not row.get("accepted_edited_asset_id"):
            row["status"] = "candidate_ready"
            row["updated_at"] = now
    write_authoring_metadata("assets", upsert_by_key(assets, "asset_id", candidate_asset))
    write_authoring_metadata("edits", upsert_by_key(edits, "edit_id", edit))
    write_authoring_metadata("candidates", upsert_by_key(candidates, "candidate_id", candidate))
    write_authoring_metadata("samples", sorted(samples, key=lambda row: str(row.get("sample_id", ""))))
    updated = dict(edit_record)
    updated["candidate_id"] = candidate_id
    updated["candidate_asset_id"] = candidate_asset["asset_id"]
    updated["candidate_image"] = candidate_asset["path"]
    updated["candidate_url"] = asset_url(candidate_asset)
    return updated


def save_new_authoring_item(payload: dict[str, Any]) -> dict[str, Any]:
    sample_id = safe_name(str(payload.get("id") or "")) or safe_name(Path(str(payload.get("original_filename", "real"))).stem)
    source = authoring_source_path_from_payload(payload)
    source_entity = str(payload.get("source_entity") or "").strip()
    target_entity = str(payload.get("target_entity") or "").strip()
    scene_description = str(payload.get("scene_description") or "").strip()
    if not source_entity or not scene_description:
        raise ValueError("Source entity and scene description are required.")
    if str(payload.get("edit_type") or "swap_entity") == "swap_entity" and not target_entity:
        raise ValueError("Target entity is required for swap_entity.")
    candidate_rel = str(payload.get("candidate_image") or "").strip()
    if not candidate_rel:
        raise ValueError("Run an edit candidate before saving.")
    candidate = authoring_output_path(candidate_rel)
    if not candidate.exists():
        raise FileNotFoundError(candidate)

    assets = load_authoring_metadata("assets")
    edits = load_authoring_metadata("edits")
    candidates = load_authoring_metadata("candidates")
    samples = load_authoring_metadata("samples")
    questions = load_authoring_metadata("questions")
    asset_by_path = {str(row.get("path", "")): row for row in assets}
    sample = next((row for row in samples if str(row.get("sample_id", "")) == sample_id), None)
    if not sample:
        raise ValueError(f"Unknown metadata sample_id: {sample_id}")
    source_asset_id = str(sample.get("source_asset_id") or f"{sample_id}_source")
    source_asset = next((row for row in assets if str(row.get("asset_id", "")) == source_asset_id), None)
    source_filename = str(payload.get("original_filename") or (source_asset or {}).get("source_filename") or source.name)
    now = utc_now()
    if not source_asset:
        source_target = AUTHORING_OUTPUT / "assets" / "source" / f"{sample_id}.jpg"
        save_image_as_target(source, source_target)
        width, height, mime = image_info(source_target)
        source_asset = {
            "asset_id": source_asset_id,
            "kind": "source_image",
            "path": rel_to_authoring_output(source_target),
            "mime_type": mime,
            "width": width,
            "height": height,
            "source_filename": source_filename,
            "created_at": now,
            "metadata": {"original_filename": source_filename},
        }
        assets = upsert_by_key(assets, "asset_id", source_asset)

    candidate_asset = asset_by_path.get(candidate_rel)
    candidate_row = next(
        (row for row in candidates if str(row.get("candidate_asset_id", "")) == str((candidate_asset or {}).get("asset_id", ""))),
        None,
    )
    if not candidate_asset:
        candidate_index = next_candidate_index(candidates, sample_id)
        candidate_asset = copy_candidate_into_metadata_assets(
            sample_id=sample_id,
            source_filename=source_filename,
            candidate_path=candidate,
            candidate_index=candidate_index,
        )
        candidate_id = f"{sample_id}_cand_{candidate_index:03d}"
        candidate_row = {
            "candidate_id": candidate_id,
            "edit_id": str((sample.get("edit_ids") or [f"{sample_id}_edit_001"])[0]),
            "candidate_asset_id": candidate_asset["asset_id"],
            "generator": {"provider": "unknown", "model": "", "method": "manual_or_imported"},
            "status": "candidate",
            "prompt": str(payload.get("prompt") or ""),
            "artifacts": {},
            "created_at": now,
            "metadata": {},
        }
        assets = upsert_by_key(assets, "asset_id", candidate_asset)
        candidates = upsert_by_key(candidates, "candidate_id", candidate_row)
    if candidate_asset and not candidate_row:
        candidate_index = next_candidate_index(candidates, sample_id)
        candidate_row = {
            "candidate_id": f"{sample_id}_cand_{candidate_index:03d}",
            "edit_id": str((sample.get("edit_ids") or [f"{sample_id}_edit_001"])[0]),
            "candidate_asset_id": candidate_asset["asset_id"],
            "generator": {"provider": "unknown", "model": "", "method": "metadata_recovery"},
            "status": "candidate",
            "prompt": str(payload.get("prompt") or ""),
            "artifacts": {},
            "created_at": now,
            "metadata": {},
        }
        candidates = upsert_by_key(candidates, "candidate_id", candidate_row)

    edited_target = AUTHORING_OUTPUT / "assets" / "edited" / f"{sample_id}.jpg"
    edited_target.parent.mkdir(parents=True, exist_ok=True)
    save_image_as_target(candidate, edited_target)
    width, height, mime = image_info(edited_target)
    edited_asset = {
        "asset_id": f"{sample_id}_edited",
        "kind": "edited_image",
        "path": rel_to_authoring_output(edited_target),
        "mime_type": mime,
        "width": width,
        "height": height,
        "source_filename": source_filename,
        "created_at": now,
        "metadata": {"accepted_candidate_id": str(candidate_row.get("candidate_id", ""))},
    }
    edit_id = str((sample.get("edit_ids") or [f"{sample_id}_edit_001"])[0])
    edit = {
        "edit_id": edit_id,
        "source_asset_id": source_asset_id,
        "edit_type": str(payload.get("edit_type") or "swap_entity"),
        "instruction": str(payload.get("prompt") or "").strip(),
        "bbox": payload.get("bbox"),
        "metadata": {
            "source_entity": source_entity,
            "target_entity": target_entity,
            "scene_description": scene_description,
            "review_location": str(payload.get("review_location") or "").strip(),
        },
        "created_at": str(next((row.get("created_at") for row in edits if row.get("edit_id") == edit_id), "")) or now,
        "updated_at": now,
    }
    accepted_candidate_id = str(candidate_row.get("candidate_id", ""))
    for row in candidates:
        if str(row.get("edit_id", "")) == edit_id and str(row.get("candidate_id", "")) == accepted_candidate_id:
            row["status"] = "accepted"
        elif str(row.get("edit_id", "")) == edit_id and str(row.get("status", "")) == "accepted":
            row["status"] = "candidate"
    raw_questions = normalize_flexible_authoring_questions(
        {
            "id": sample_id,
            "base_image": source_asset["path"],
            "edited_image": edited_asset["path"],
            "source_entity": source_entity,
            "target_entity": target_entity,
            "scene_description": scene_description,
        },
        payload.get("questions"),
    )
    sample_questions = legacy_questions_to_metadata(
        sample_id=sample_id,
        edit_id=edit_id,
        source_asset_id=source_asset_id,
        edited_asset_id=edited_asset["asset_id"],
        questions=raw_questions,
    )
    question_ids = [row["question_id"] for row in sample_questions]
    questions = [row for row in questions if str(row.get("sample_id", "")) != sample_id] + sample_questions
    for row in samples:
        if str(row.get("sample_id", "")) == sample_id:
            row.update(
                {
                    "source_asset_id": source_asset_id,
                    "edit_ids": [edit_id],
                    "accepted_candidate_id": accepted_candidate_id,
                    "accepted_edited_asset_id": edited_asset["asset_id"],
                    "question_ids": question_ids,
                    "status": "accepted",
                    "updated_at": now,
                    "metadata": {"original_filename": source_filename},
                }
            )
    assets = upsert_by_key(upsert_by_key(assets, "asset_id", source_asset), "asset_id", edited_asset)
    edits = upsert_by_key(edits, "edit_id", edit)
    write_authoring_metadata("assets", assets)
    write_authoring_metadata("edits", edits)
    write_authoring_metadata("candidates", sorted(candidates, key=lambda row: str(row.get("candidate_id", ""))))
    write_authoring_metadata("samples", sorted(samples, key=lambda row: str(row.get("sample_id", ""))))
    write_authoring_metadata("questions", sorted(questions, key=lambda row: str(row.get("question_id", ""))))

    record = {
        "id": sample_id,
        "original_filename": source_filename,
        "source_image": source_asset["path"],
        "base_image": source_asset["path"],
        "edited_image": edited_asset["path"],
        "base_url": asset_url(source_asset),
        "edited_url": asset_url(edited_asset),
        "source_entity": source_entity,
        "target_entity": target_entity,
        "scene_description": scene_description,
        "review_location": edit["metadata"]["review_location"],
        "source_bbox": payload.get("bbox"),
        "edit_type": edit["edit_type"],
        "candidate_image": candidate_rel,
        "questions": metadata_questions_to_legacy(sample_questions),
        "created_at": str(payload.get("created_at") or now),
        "updated_at": now,
    }
    return {"record": record, "export": export_new_authoring_dataset()}


def default_authoring_questions(record: dict[str, Any]) -> list[dict[str, Any]]:
    item_id = str(record["id"])
    source = str(record.get("source_entity") or "source object").strip()
    target = str(record.get("target_entity") or "target object").strip()
    scene = str(record.get("scene_description") or "image").strip()
    base_image = record.get("base_image") or f"images/{item_id}__base.jpg"
    edited_image = record.get("edited_image") or f"images/{item_id}__edited.jpg"
    return [
        {
            "id": f"{item_id}__base_source",
            "pair_id": item_id,
            "probe": "base_source",
            "image_role": "base",
            "image": base_image,
            "question": f"Is there a {source} visible in this {scene}?",
            "answer": "yes",
        },
        {
            "id": f"{item_id}__base_target",
            "pair_id": item_id,
            "probe": "base_target",
            "image_role": "base",
            "image": base_image,
            "question": f"Is there a {target} visible in this {scene}?",
            "answer": "no",
        },
        {
            "id": f"{item_id}__edited_source",
            "pair_id": item_id,
            "probe": "edited_source",
            "image_role": "edited",
            "image": edited_image,
            "question": f"Is there a {source} visible in this {scene}?",
            "answer": "no",
        },
        {
            "id": f"{item_id}__edited_target",
            "pair_id": item_id,
            "probe": "edited_target",
            "image_role": "edited",
            "image": edited_image,
            "question": f"Is there a {target} visible in this {scene}?",
            "answer": "yes",
        },
    ]


def normalize_authoring_questions(record: dict[str, Any], questions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not questions:
        return default_authoring_questions(record)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(questions, start=1):
        image_role = str(row.get("image_role") or "base")
        image = record["edited_image"] if image_role == "edited" else record["base_image"]
        probe = safe_name(str(row.get("probe") or f"custom_{index:02d}")) or f"custom_{index:02d}"
        answer = str(row.get("answer") or "").strip().lower()
        if answer not in {"yes", "no"}:
            raise ValueError(f"Question {index} answer must be yes or no.")
        question = str(row.get("question") or "").strip()
        if not question:
            raise ValueError(f"Question {index} is empty.")
        rows.append(
            {
                "id": f"{record['id']}__{probe}",
                "pair_id": record["id"],
                "probe": probe,
                "image_role": image_role,
                "image": image,
                "question_type": str(row.get("question_type") or "yes_no"),
                "question": question,
                "answer": answer,
                "eval_type": str(row.get("eval_type") or "yes_no_exact"),
            }
        )
    return rows


def authoring_pair(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "sample_group": AUTHORING_OUTPUT.name,
        "base_image": record["base_image"],
        "edited_image": record["edited_image"],
        "source_entity": record["source_entity"],
        "inserted_entity": record["target_entity"],
        "review_location": record.get("review_location", ""),
        "scene_description": record["scene_description"],
        "base_bbox_normalized": record.get("source_bbox"),
        "source_filename": record["original_filename"],
        "edit_type": record.get("edit_type", "swap_entity"),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
    }


def export_authoring_dataset() -> dict[str, Any]:
    records = [row for row in authoring_records().values() if row.get("base_image") and row.get("edited_image")]
    records.sort(key=lambda row: str(row["id"]))
    pairs = [authoring_pair(row) for row in records]
    questions: list[dict[str, Any]] = []
    dataset: list[dict[str, Any]] = []
    for record, pair in zip(records, pairs):
        qs = record.get("questions") or default_authoring_questions(record)
        questions.extend(qs)
        dataset.append({**pair, "questions": qs})

    AUTHORING_OUTPUT.mkdir(parents=True, exist_ok=True)
    outputs = {
        "pairs": AUTHORING_OUTPUT / "pairs.jsonl",
        "questions": AUTHORING_OUTPUT / "questions.jsonl",
        "dataset": AUTHORING_OUTPUT / "dataset.jsonl",
    }
    for key, path in outputs.items():
        rows = pairs if key == "pairs" else questions if key == "questions" else dataset
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "pairs": rel_to_authoring_output(outputs["pairs"]),
        "questions": rel_to_authoring_output(outputs["questions"]),
        "dataset": rel_to_authoring_output(outputs["dataset"]),
        "pair_count": len(pairs),
        "question_count": len(questions),
    }


def export_new_authoring_dataset() -> dict[str, Any]:
    assets = load_authoring_metadata("assets")
    edits = load_authoring_metadata("edits")
    samples = load_authoring_metadata("samples")
    questions = load_authoring_metadata("questions")
    asset_by_id = {str(row.get("asset_id", "")): row for row in assets}
    edit_by_id = {str(row.get("edit_id", "")): row for row in edits}

    generic_questions: list[dict[str, Any]] = []
    generic_dataset: list[dict[str, Any]] = []
    accepted_sample_ids = {
        str(sample.get("sample_id", ""))
        for sample in samples
        if sample.get("accepted_edited_asset_id") and str(sample.get("status", "")) == "accepted"
    }
    for question in sorted(questions, key=lambda row: str(row.get("question_id", ""))):
        sample_id = str(question.get("sample_id", ""))
        if sample_id not in accepted_sample_ids:
            continue
        image_asset = asset_by_id.get(str(question.get("image_asset_id", "")))
        if not image_asset:
            continue
        row = {
            "id": question.get("question_id", ""),
            "sample_id": sample_id,
            "image": image_asset.get("path", ""),
            "question": question.get("prompt", ""),
            "answer": question.get("answer", ""),
            "question_type": question.get("question_type", ""),
            "eval_type": question.get("eval_type", ""),
            "image_role": question.get("image_role", ""),
            "metadata": question.get("metadata", {}),
        }
        generic_questions.append(row)
        generic_dataset.append(row)

    generic_output_dir = AUTHORING_OUTPUT / "exports" / "generic_questions"
    write_jsonl_atomic(generic_output_dir / "questions.jsonl", generic_questions)
    write_jsonl_atomic(generic_output_dir / "dataset.jsonl", generic_dataset)

    questions_by_sample_probe: dict[str, dict[str, dict[str, Any]]] = {}
    for question in questions:
        probe = str((question.get("metadata") or {}).get("probe") or "")
        if probe:
            questions_by_sample_probe.setdefault(str(question.get("sample_id", "")), {})[probe] = question

    required = ("base_source", "base_target", "edited_source", "edited_target")
    pairs: list[dict[str, Any]] = []
    question_rows: list[dict[str, Any]] = []
    dataset_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for sample in sorted(samples, key=lambda row: str(row.get("sample_id", ""))):
        sample_id = str(sample.get("sample_id", ""))
        source_asset = asset_by_id.get(str(sample.get("source_asset_id", "")))
        edited_asset = asset_by_id.get(str(sample.get("accepted_edited_asset_id", "")))
        if not source_asset or not edited_asset:
            skipped.append(f"{sample_id}: no accepted source/edited asset")
            continue
        probe_map = questions_by_sample_probe.get(sample_id, {})
        missing = [probe for probe in required if probe not in probe_map]
        if missing:
            skipped.append(f"{sample_id}: missing probes {','.join(missing)}")
            continue
        edit_ids = sample.get("edit_ids") or []
        edit = edit_by_id.get(str(edit_ids[0]), {}) if edit_ids else {}
        metadata = edit.get("metadata") or {}
        pair = {
            "id": sample_id,
            "sample_group": AUTHORING_OUTPUT.name,
            "base_image": source_asset["path"],
            "edited_image": edited_asset["path"],
            "source_entity": metadata.get("source_entity", ""),
            "inserted_entity": metadata.get("target_entity", ""),
            "review_location": metadata.get("review_location", ""),
            "scene_description": metadata.get("scene_description", ""),
            "base_bbox_normalized": edit.get("bbox"),
            "source_filename": source_asset.get("source_filename", ""),
            "edit_type": edit.get("edit_type", ""),
            "created_at": sample.get("created_at", ""),
            "updated_at": sample.get("updated_at", ""),
        }
        rows = []
        for probe in required:
            question = probe_map[probe]
            image_asset = asset_by_id.get(str(question.get("image_asset_id", "")), {})
            row = {
                "id": question["question_id"],
                "pair_id": sample_id,
                "probe": probe,
                "image_role": "base" if probe.startswith("base_") else "edited",
                "image": image_asset.get("path", ""),
                "question": question.get("prompt", ""),
                "answer": question.get("answer", ""),
            }
            rows.append(row)
        pairs.append(pair)
        question_rows.extend(rows)
        dataset_rows.append({**pair, "questions": rows})

    output_dir = AUTHORING_OUTPUT / "exports" / "q1q4_context_prior"
    write_jsonl_atomic(output_dir / "pairs.jsonl", pairs)
    write_jsonl_atomic(output_dir / "questions.jsonl", question_rows)
    write_jsonl_atomic(output_dir / "dataset.jsonl", dataset_rows)

    exports = load_authoring_metadata("exports")
    q1q4_export_row = {
        "export_id": "q1q4_context_prior_latest",
        "format": "legacy_q1q4_jsonl",
        "path": rel_to_authoring_output(output_dir),
        "pair_count": len(pairs),
        "question_count": len(question_rows),
        "skipped": skipped,
        "created_at": utc_now(),
    }
    generic_export_row = {
        "export_id": "generic_questions_latest",
        "format": "generic_questions_jsonl",
        "path": rel_to_authoring_output(generic_output_dir),
        "pair_count": len({row["sample_id"] for row in generic_questions}),
        "question_count": len(generic_questions),
        "created_at": utc_now(),
    }
    exports = upsert_by_key(exports, "export_id", q1q4_export_row)
    exports = upsert_by_key(exports, "export_id", generic_export_row)
    write_authoring_metadata("exports", exports)
    return {
        "pairs": rel_to_authoring_output(output_dir / "pairs.jsonl"),
        "questions": rel_to_authoring_output(output_dir / "questions.jsonl"),
        "dataset": rel_to_authoring_output(output_dir / "dataset.jsonl"),
        "pair_count": len(pairs),
        "question_count": len(question_rows),
        "generic_questions": rel_to_authoring_output(generic_output_dir / "questions.jsonl"),
        "generic_dataset": rel_to_authoring_output(generic_output_dir / "dataset.jsonl"),
        "generic_pair_count": len({row["sample_id"] for row in generic_questions}),
        "generic_question_count": len(generic_questions),
        "skipped": skipped,
    }


def run_authoring_edit(payload: dict[str, Any]) -> dict[str, Any]:
    source = authoring_source_path_from_payload(payload)
    box = payload.get("bbox")
    if not box:
        raise ValueError("Draw a source box before running authoring edit.")
    edit_type = str(payload.get("edit_type") or "swap_entity")
    source_entity = str(payload.get("source_entity") or "").strip()
    target_entity = str(payload.get("target_entity") or "").strip()
    if not source_entity:
        raise ValueError("Source entity is required.")
    if edit_type == "swap_entity" and not target_entity:
        raise ValueError("Target entity is required for swap edit.")
    repair_type = "swap_entity" if edit_type == "swap_entity" else "remove_entity"
    reviewer_prompt = reviewer_instruction(
        repair_type=repair_type,
        entity_to_remove=source_entity,
        source_entity=source_entity,
        target_entity=target_entity,
        prompt=str(payload.get("prompt") or "").strip(),
    )
    AUTHORING_CANDIDATES.mkdir(parents=True, exist_ok=True)
    item_id = safe_name(str(payload.get("id") or source.stem)) or safe_name(source.stem)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = safe_name(f"{item_id}__{stamp}__{repair_type}")
    mask_path, marked_path, xy = make_mask_and_marked(source, box, stem, float(payload.get("pad_pct", 0.012)), AUTHORING_CANDIDATES)
    (
        patch_path,
        patch_marked_path,
        patch_mask_path,
        blend_mask_path,
        _target_xy,
        patch_xy,
    ) = make_patch_assets(
        source,
        box,
        stem,
        float(payload.get("pad_pct", 0.012)),
        float(payload.get("patch_margin_pct", 0.035)),
        AUTHORING_CANDIDATES,
    )
    output, repaired_patch_path, returned_mime = repair_patch_with_gemini(
        source=source,
        marked_patch=patch_marked_path,
        blend_mask=blend_mask_path,
        patch_xy=patch_xy,
        destination_stem=AUTHORING_CANDIDATES / f"{stem}__edited_candidate",
        prompt=reviewer_prompt,
        model=str(payload.get("model") or "gemini-3.1-flash-image-preview"),
        repair_type=repair_type,
    )
    edit_record = {
        "edit_id": stem,
        "edit_type": edit_type,
        "source_image": str(source.name),
        "source_image_path": rel_to_authoring_output(source) if authoring_metadata_enabled() else str(source.name),
        "bbox": box,
        "source_entity": source_entity,
        "target_entity": target_entity,
        "scene_description": str(payload.get("scene_description") or "").strip(),
        "review_location": str(payload.get("review_location") or "").strip(),
        "pixel_bbox": {"x1": xy[0], "y1": xy[1], "x2": xy[2], "y2": xy[3]},
        "candidate_image": rel_to_authoring_output(output),
        "candidate_url": f"/authoring_output/{quote(rel_to_authoring_output(output))}",
        "marked_image": rel_to_authoring_output(marked_path),
        "mask_path": rel_to_authoring_output(mask_path),
        "patch_image": rel_to_authoring_output(patch_path),
        "patch_marked_image": rel_to_authoring_output(patch_marked_path),
        "patch_mask_path": rel_to_authoring_output(patch_mask_path),
        "blend_mask_path": rel_to_authoring_output(blend_mask_path),
        "repaired_patch_image": rel_to_authoring_output(repaired_patch_path),
        "returned_mime_type": returned_mime,
        "prompt": reviewer_prompt,
        "created_at": utc_now(),
    }
    sidecar = output.with_suffix(output.suffix + ".json")
    sidecar.write_text(json.dumps({"payload": payload, "edit": edit_record}, ensure_ascii=False, indent=2), encoding="utf-8")
    return sync_new_authoring_candidate(payload, edit_record)


def save_uploaded_image(filename: str, data_url: str) -> Path:
    prefix, separator, encoded = data_url.partition(",")
    if not separator or not prefix.startswith("data:image/"):
        raise ValueError("Upload must be an image data URL.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("Upload image data is not valid base64.") from exc
    UPLOAD_SOURCES.mkdir(parents=True, exist_ok=True)
    stem = safe_name(Path(filename or "uploaded_image").stem) or "uploaded_image"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = UPLOAD_SOURCES / f"{stamp}__{stem}.jpg"
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        image.save(target, quality=95)
    return target


def decode_authoring_upload(data_url: str) -> tuple[Image.Image, str]:
    prefix, separator, encoded = data_url.partition(",")
    if not separator or not prefix.startswith("data:image/"):
        raise ValueError("Upload must be an image data URL.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("Upload image data is not valid base64.") from exc
    if not raw:
        raise ValueError("Uploaded image is empty.")
    if len(raw) > AUTHORING_UPLOAD_MAX_BYTES:
        raise ValueError("Uploaded image exceeds the 30 MB limit.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            detected = str(source.format or "").upper()
            if detected not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Unsupported image format. Use JPEG, PNG, or WebP.")
            if source.width * source.height > AUTHORING_UPLOAD_MAX_PIXELS:
                raise ValueError("Uploaded image dimensions are too large.")
            image = ImageOps.exif_transpose(source).copy()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Upload is not a valid JPEG, PNG, or WebP image.") from exc
    suffix = ".jpg" if detected == "JPEG" else ".png" if detected == "PNG" else ".webp"
    return image, suffix


def unique_authoring_input_path(filename: str, suffix: str) -> Path:
    AUTHORING_INPUT.mkdir(parents=True, exist_ok=True)
    stem = safe_name(Path(filename or "real_image").stem).strip("_") or "real_image"
    candidate = AUTHORING_INPUT / f"{stem}{suffix}"
    number = 2
    while candidate.exists():
        candidate = AUTHORING_INPUT / f"{stem}__{number}{suffix}"
        number += 1
    return candidate


def save_authoring_input_image(image: Image.Image, target: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=f"{target.stem}_", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        if target.suffix.lower() in {".jpg", ".jpeg"}:
            image.convert("RGB").save(temporary, format="JPEG", quality=95)
        elif target.suffix.lower() == ".png":
            image.save(temporary, format="PNG")
        else:
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            image.convert(mode).save(temporary, format="WEBP", quality=95)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def next_authoring_sample_id(samples: list[dict[str, Any]], assets: list[dict[str, Any]]) -> str:
    used_sample_ids = {str(row.get("sample_id", "")) for row in samples}
    used_paths = {str(row.get("path", "")) for row in assets}
    numbers = []
    for sample_id in used_sample_ids:
        if sample_id.startswith("real_"):
            try:
                numbers.append(int(sample_id.rsplit("_", 1)[1]))
            except ValueError:
                pass
    number = max(numbers, default=0) + 1
    while True:
        sample_id = f"real_{number:03d}"
        if sample_id not in used_sample_ids and f"assets/source/{sample_id}.jpg" not in used_paths:
            return sample_id
        number += 1


def register_authoring_metadata_upload(source: Path, client_filename: str) -> str:
    assets = load_authoring_metadata("assets")
    edits = load_authoring_metadata("edits")
    samples = load_authoring_metadata("samples")
    sample_id = next_authoring_sample_id(samples, assets)
    source_asset_id = f"{sample_id}_source"
    edit_id = f"{sample_id}_edit_001"
    source_target = AUTHORING_OUTPUT / "assets" / "source" / f"{sample_id}.jpg"
    source_target.parent.mkdir(parents=True, exist_ok=True)
    save_image_as_target(source, source_target)
    width, height, mime = image_info(source_target)
    now = utc_now()
    asset = {
        "asset_id": source_asset_id,
        "kind": "source_image",
        "path": rel_to_authoring_output(source_target),
        "mime_type": mime,
        "width": width,
        "height": height,
        "source_filename": source.name,
        "created_at": now,
        "metadata": {
            "original_filename": source.name,
            "client_filename": Path(client_filename).name,
            "ingestion": "authoring_upload",
        },
    }
    edit = {
        "edit_id": edit_id,
        "source_asset_id": source_asset_id,
        "edit_type": "swap_entity",
        "instruction": "",
        "bbox": None,
        "metadata": {"source_entity": "", "target_entity": "", "scene_description": ""},
        "created_at": now,
        "updated_at": now,
    }
    sample = {
        "sample_id": sample_id,
        "source_asset_id": source_asset_id,
        "edit_ids": [edit_id],
        "accepted_candidate_id": "",
        "accepted_edited_asset_id": "",
        "question_ids": [],
        "tags": ["real_image", "entity_swap"],
        "split": "validation",
        "status": "pending_edit",
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "original_filename": source.name,
            "client_filename": Path(client_filename).name,
            "ingestion": "authoring_upload",
        },
    }
    try:
        write_authoring_metadata("assets", upsert_by_key(assets, "asset_id", asset))
        write_authoring_metadata("edits", upsert_by_key(edits, "edit_id", edit))
        write_authoring_metadata("samples", upsert_by_key(samples, "sample_id", sample))
    except Exception:
        source_target.unlink(missing_ok=True)
        write_authoring_metadata("assets", assets)
        write_authoring_metadata("edits", edits)
        write_authoring_metadata("samples", samples)
        raise
    return sample_id


def save_authoring_upload(filename: str, data_url: str) -> dict[str, Any]:
    image, suffix = decode_authoring_upload(data_url)
    with AUTHORING_UPLOAD_LOCK:
        target = unique_authoring_input_path(filename, suffix)
        save_authoring_input_image(image, target)
        try:
            sample_id = register_authoring_metadata_upload(target, filename) if authoring_metadata_enabled() else ""
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return {
        "item_id": sample_id,
        "original_filename": target.name,
        "client_filename": Path(filename).name,
        "storage": "metadata" if sample_id else "directory",
    }


def run_uploaded_patch_repair(payload: dict[str, Any]) -> dict[str, Any]:
    source_rel = str(payload["source_image"])
    source = tool_path(source_rel)
    if not source.exists():
        raise FileNotFoundError(source)
    box = payload.get("bbox")
    if not box:
        raise ValueError("Draw a repair box before repairing the uploaded image.")
    repair_type = str(payload.get("repair_type", "remove_entity"))
    reviewer_prompt = reviewer_instruction(
        repair_type=repair_type,
        entity_to_remove=str(payload.get("entity_to_remove", "")).strip(),
        source_entity=str(payload.get("source_entity", "")).strip(),
        target_entity=str(payload.get("target_entity", "")).strip(),
        prompt=str(payload.get("prompt", "")).strip(),
    )
    pad_pct = float(payload.get("pad_pct", 0.012))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = safe_name(f"upload__{stamp}__gemini_patch")
    mask_path, marked_path, xy = make_mask_and_marked(source, box, stem, pad_pct, UPLOAD_REPAIRS)
    output_stem = UPLOAD_REPAIRS / f"{stem}__repaired"
    (
        patch_path,
        patch_marked_path,
        patch_mask_path,
        blend_mask_path,
        _target_xy,
        patch_xy,
    ) = make_patch_assets(
        source,
        box,
        stem,
        pad_pct,
        float(payload.get("patch_margin_pct", 0.035)),
        UPLOAD_REPAIRS,
    )
    output, repaired_patch_path, returned_mime = repair_patch_with_gemini(
        source=source,
        marked_patch=patch_marked_path,
        blend_mask=blend_mask_path,
        patch_xy=patch_xy,
        destination_stem=output_stem,
        prompt=reviewer_prompt,
        model=str(payload.get("model") or "gemini-3.1-flash-image-preview"),
        repair_type=repair_type,
    )
    return {
        "repair_id": stem,
        "repair_type": repair_type,
        "engine": "gemini_local_patch_edit",
        "source_image": source_rel,
        "bbox": box,
        "pixel_bbox": {"x1": xy[0], "y1": xy[1], "x2": xy[2], "y2": xy[3]},
        "mask_path": rel_to_tool(mask_path),
        "marked_image": rel_to_tool(marked_path),
        "patch_image": rel_to_tool(patch_path),
        "patch_marked_image": rel_to_tool(patch_marked_path),
        "patch_mask_path": rel_to_tool(patch_mask_path),
        "blend_mask_path": rel_to_tool(blend_mask_path),
        "repaired_patch_image": rel_to_tool(repaired_patch_path),
        "repaired_image": rel_to_tool(output),
        "repaired_url": f"/tools/review_repair/{rel_to_tool(output)}",
        "returned_mime_type": returned_mime,
        "prompt": reviewer_prompt,
        "created_at": utc_now(),
    }


def bbox_to_pixels(box: dict[str, Any], width: int, height: int, pad_pct: float) -> tuple[int, int, int, int]:
    x1 = float(box["x"]) * width
    y1 = float(box["y"]) * height
    x2 = (float(box["x"]) + float(box["w"])) * width
    y2 = (float(box["y"]) + float(box["h"])) * height
    pad = max(2, round(min(width, height) * pad_pct))
    return (
        max(0, round(min(x1, x2)) - pad),
        max(0, round(min(y1, y2)) - pad),
        min(width, round(max(x1, x2)) + pad),
        min(height, round(max(y1, y2)) + pad),
    )


def normalized_box_to_pixels(
    box: dict[str, Any], width: int, height: int
) -> tuple[int, int, int, int]:
    required = {"x", "y", "w", "h"}
    if not isinstance(box, dict) or not required.issubset(box):
        raise ValueError("Bounding box must contain x, y, w, and h")
    values = {key: float(box[key]) for key in required}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Bounding-box coordinates must be finite")
    if values["w"] <= 0 or values["h"] <= 0:
        raise ValueError("Bounding-box width and height must be positive")
    if (
        values["x"] < 0
        or values["y"] < 0
        or values["x"] + values["w"] > 1.000001
        or values["y"] + values["h"] > 1.000001
    ):
        raise ValueError("Bounding box must stay within normalized image bounds")
    x1 = max(0, min(width - 1, round(values["x"] * width)))
    y1 = max(0, min(height - 1, round(values["y"] * height)))
    x2 = max(x1 + 1, min(width - 1, round((values["x"] + values["w"]) * width)))
    y2 = max(y1 + 1, min(height - 1, round((values["y"] + values["h"]) * height)))
    return x1, y1, x2, y2


def review_image_sources(review_id: str) -> tuple[str, str]:
    if canonical_review_enabled():
        repository = MetadataRepository(DATASET_ROOT)
        sample = next(
            (
                row
                for row in repository.load("samples")
                if str(row.get("sample_id", "")) == review_id
            ),
            None,
        )
        if sample is None:
            raise ValueError(f"Unknown review_id: {review_id}")
        assets = {
            str(row.get("asset_id", "")): row for row in repository.load("assets")
        }
        edit_ids = {str(value) for value in sample.get("edit_ids") or []}
        candidate = next(
            (
                row
                for row in repository.load("candidates")
                if str(row.get("edit_id", "")) in edit_ids
            ),
            None,
        )
        if candidate is None:
            raise ValueError(f"No edited candidate found for sample: {review_id}")
        base = assets.get(str(sample.get("source_asset_id", "")), {})
        edited = assets.get(str(candidate.get("candidate_asset_id", "")), {})
        decision = next(
            (
                row
                for row in review_decision_rows()
                if str(row.get("review_id", "")) == review_id
            ),
            {},
        )
        base_rel = str(base.get("path", ""))
        edited_rel = str(decision.get("repaired_edited_image") or edited.get("path", ""))
    else:
        item = next(
            (row for row in review_manifest_rows() if str(row.get("review_id", "")) == review_id),
            None,
        )
        if item is None:
            raise ValueError(f"Unknown review_id: {review_id}")
        base_rel = str(item.get("base_image", ""))
        edited_rel = str(item.get("edited_image", ""))
    if not base_rel or not edited_rel:
        raise ValueError(f"Base and Edited image paths are required: {review_id}")
    return base_rel, edited_rel


def save_boxed_image(
    *,
    source: Path,
    target: Path,
    box: dict[str, Any],
    color: tuple[int, int, int],
) -> dict[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGBA")
        width, height = image.size
        xy = normalized_box_to_pixels(box, width, height)
        line_width = max(4, round(min(width, height) * 0.004))
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle(xy, fill=(*color, 28), outline=(*color, 255), width=line_width)
        output = Image.alpha_composite(image, overlay).convert("RGB")
        fd, name = tempfile.mkstemp(
            prefix=f"{target.stem}__", suffix=".png", dir=target.parent
        )
        os.close(fd)
        temp_path = Path(name)
        try:
            output.save(temp_path, format="PNG", optimize=True)
            os.replace(temp_path, target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
    return {"x1": xy[0], "y1": xy[1], "x2": xy[2], "y2": xy[3]}


def export_boxed_images(payload: dict[str, Any]) -> dict[str, Any]:
    review_id = str(payload.get("review_id", "")).strip()
    if not review_id:
        raise ValueError("review_id is required")
    base_rel, edited_rel = review_image_sources(review_id)
    boxes = {
        "base": payload.get("base_bbox"),
        "edited": payload.get("edited_bbox"),
    }
    if not any(boxes.values()):
        raise ValueError("Draw at least one Base or Edited bounding box before exporting")
    sources = {"base": base_rel, "edited": edited_rel}
    colors = {"base": (239, 100, 97), "edited": (57, 218, 123)}
    export_root = DATASET_ROOT / "exports" / "boxed"
    stem = safe_name(review_id)
    exports: list[dict[str, Any]] = []
    for role in ("base", "edited"):
        box = boxes[role]
        if not box:
            continue
        source = dataset_path(sources[role])
        if not source.exists():
            raise FileNotFoundError(source)
        target = export_root / f"{stem}__{role}_boxed.png"
        pixel_bbox = save_boxed_image(
            source=source,
            target=target,
            box=box,
            color=colors[role],
        )
        rel_path = rel_to_dataset(target)
        exports.append(
            {
                "role": role,
                "source_image": sources[role],
                "boxed_image": rel_path,
                "boxed_url": f"/{quote(rel_path)}",
                "bbox": box,
                "pixel_bbox": pixel_bbox,
            }
        )
    return {"review_id": review_id, "exports": exports, "exported_at": utc_now()}


def expand_xy(xy: tuple[int, int, int, int], width: int, height: int, margin_pct: float) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = xy
    margin = max(24, round(max(x2 - x1, y2 - y1, min(width, height) * 0.015) + min(width, height) * margin_pct))
    return (
        max(0, x1 - margin),
        max(0, y1 - margin),
        min(width, x2 + margin),
        min(height, y2 + margin),
    )


def make_mask_and_marked(
    source: Path,
    box: dict[str, Any],
    stem: str,
    pad_pct: float,
    repairs_dir: Path | None = None,
) -> tuple[Path, Path, tuple[int, int, int, int]]:
    repairs_dir = repairs_dir or REPAIRS
    repairs_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        width, height = image.size
        xy = bbox_to_pixels(box, width, height, pad_pct)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rectangle(xy, fill=255)
        marked = image.copy()
        draw = ImageDraw.Draw(marked)
        line_width = max(1, round(min(width, height) * 0.0012))
        draw.rectangle(xy, outline="#ff2d2d", width=line_width)
        mask_path = repairs_dir / f"{stem}__mask.png"
        marked_path = repairs_dir / f"{stem}__marked.jpg"
        mask.save(mask_path)
        marked.save(marked_path, quality=95)
    return mask_path, marked_path, xy


def make_patch_assets(
    source: Path,
    box: dict[str, Any],
    stem: str,
    pad_pct: float,
    margin_pct: float,
    repairs_dir: Path | None = None,
) -> tuple[Path, Path, Path, Path, tuple[int, int, int, int], tuple[int, int, int, int]]:
    repairs_dir = repairs_dir or REPAIRS
    repairs_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        width, height = image.size
        target_xy = bbox_to_pixels(box, width, height, pad_pct)
        patch_xy = expand_xy(target_xy, width, height, margin_pct)
        patch = image.crop(patch_xy)
        local_target = (
            target_xy[0] - patch_xy[0],
            target_xy[1] - patch_xy[1],
            target_xy[2] - patch_xy[0],
            target_xy[3] - patch_xy[1],
        )
        local_mask = Image.new("L", patch.size, 0)
        ImageDraw.Draw(local_mask).rectangle(local_target, fill=255)
        marked = patch.copy()
        draw = ImageDraw.Draw(marked)
        line_width = max(1, round(min(patch.size) * 0.003))
        draw.rectangle(local_target, outline="#ff2d2d", width=line_width)

        # The final blend mask is softer and a little larger than the exact repair rectangle.
        blend_mask = Image.new("L", patch.size, 0)
        blur_pad = max(8, round(min(width, height) * 0.006))
        blend_xy = (
            max(0, local_target[0] - blur_pad),
            max(0, local_target[1] - blur_pad),
            min(patch.size[0], local_target[2] + blur_pad),
            min(patch.size[1], local_target[3] + blur_pad),
        )
        ImageDraw.Draw(blend_mask).rectangle(blend_xy, fill=255)
        try:
            from PIL import ImageFilter

            blend_mask = blend_mask.filter(ImageFilter.GaussianBlur(radius=max(6, blur_pad // 2)))
        except Exception:
            pass

        patch_path = repairs_dir / f"{stem}__patch.jpg"
        marked_path = repairs_dir / f"{stem}__patch_marked.jpg"
        local_mask_path = repairs_dir / f"{stem}__patch_mask.png"
        blend_mask_path = repairs_dir / f"{stem}__blend_mask.png"
        patch.save(patch_path, quality=95)
        marked.save(marked_path, quality=95)
        local_mask.save(local_mask_path)
        blend_mask.save(blend_mask_path)
    return patch_path, marked_path, local_mask_path, blend_mask_path, target_xy, patch_xy


def repair_with_opencv(source: Path, mask: Path, destination: Path, radius: int) -> None:
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "OpenCV is not installed. Install opencv-python to use Fast Repair, "
            "or use Gemini Semantic Repair."
        ) from exc
    image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    mask_image = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE)
    if image is None or mask_image is None:
        raise RuntimeError("Could not read source image or mask for OpenCV repair.")
    repaired = cv2.inpaint(image, mask_image, int(radius), cv2.INPAINT_TELEA)
    if not cv2.imwrite(str(destination), repaired):
        raise RuntimeError(f"Could not save repaired image: {destination}")


def response_image(response: Any) -> tuple[bytes, str]:
    for candidate in response.candidates or []:
        for part in ((candidate.content.parts if candidate.content else []) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data and str(inline.mime_type or "").startswith("image/"):
                return bytes(inline.data), str(inline.mime_type)
    raise RuntimeError("Gemini returned no image.")


def extension(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")


def repair_system_prompt(repair_type: str, *, patch: bool) -> str:
    scope = "this cropped patch from a larger benchmark image" if patch else "a benchmark image"
    location = "in the crop" if patch else "in the image"
    shared = (
        f"You are editing only {scope}. The thin red rectangle is a temporary review marker, "
        "not part of the scene. Remove the red rectangle itself. Keep everything outside the "
        "marked region unchanged, including lighting, shadows, camera, perspective, background "
        "clutter, and all intended edited content. "
    )
    if repair_type == "swap_entity":
        return (
            shared
            + "Replace only the object or content inside the marked region according to the reviewer "
            f"instruction. The replacement must be clearly visible and naturally integrated {location}. "
            "Match local scale, viewpoint, occlusion, texture, lighting, and shadows. Remove visible traces "
            "of the original object inside the region, but do not erase the requested replacement."
        )
    if repair_type == "clean_artifact":
        return (
            shared
            + "Clean only visual artifacts inside the marked region, such as seams, ghosting, warped texture, "
            "blur, halos, or unnatural edges. Preserve the intended object identity and scene semantics. "
            "Do not remove, add, or replace any object unless the reviewer explicitly asks for it."
        )
    if repair_type == "restore_background":
        return (
            shared
            + "Restore the marked region into a natural continuation of the surrounding background. Remove "
            "unwanted remnants, silhouettes, shadows, labels, or corrupted textures inside the region. "
            "Do not add a new foreground object."
        )
    return (
        shared
        + "Remove only the object inside the marked region and fill that area with visually consistent "
        f"local background. Keep all other objects {location} unchanged. Do not add any new instance of "
        "the removed object."
    )


def reviewer_instruction(
    *,
    repair_type: str,
    entity_to_remove: str,
    source_entity: str,
    target_entity: str,
    prompt: str,
) -> str:
    prompt = prompt.strip()
    if repair_type == "swap_entity":
        if not target_entity:
            raise ValueError("Target entity is required for Swap entity repair.")
        source_text = source_entity or "the object"
        instruction = (
            f"Replace the {source_text} inside the marked region with {target_entity}. "
            f"The new {target_entity} must be clearly visible and naturally integrated with the scene. "
            "Match the surrounding lighting, perspective, scale, shadows, and occlusion. "
            f"Remove all visible traces of the {source_text}. "
            "Keep everything outside the marked region unchanged. "
        )
        return instruction + prompt
    if repair_type == "clean_artifact":
        default = (
            "Clean the visual artifact inside the marked region while preserving the intended object, "
            "background, and scene content. Do not change the meaning of the edited image."
        )
        return prompt or default
    if repair_type == "restore_background":
        if entity_to_remove:
            instruction = (
                f"Restore the marked region to natural background after removing the {entity_to_remove}. "
                f"Do not leave any visible part, silhouette, shadow, label, or texture of the {entity_to_remove}. "
            )
            return instruction + prompt
        default = (
            "Restore the marked region to a natural continuation of the surrounding background. "
            "Remove any leftover artifact or corrupted texture inside the marked region."
        )
        return prompt or default
    if entity_to_remove:
        instruction = (
            f"Remove the {entity_to_remove} inside the marked region. "
            f"Do not leave any visible part, silhouette, shadow, label, or texture of the {entity_to_remove}. "
        )
        return instruction + prompt
    return prompt or (
        "Remove only the object inside the marked red region and fill the area naturally. "
        "Preserve the intended edited content and the rest of the image."
    )


def repair_with_gemini(marked: Path, destination_stem: Path, prompt: str, model: str, repair_type: str) -> tuple[Path, str]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=resolve_key("GEMINI_API_KEY"))
    image_part = types.Part.from_bytes(data=marked.read_bytes(), mime_type="image/jpeg")
    full_prompt = repair_system_prompt(repair_type, patch=False) + "\n\nReviewer instruction: " + prompt.strip()
    response = client.models.generate_content(
        model=model,
        contents=[full_prompt, image_part],
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    data, mime = response_image(response)
    destination = destination_stem.with_suffix(extension(mime))
    destination.write_bytes(data)
    with Image.open(marked) as source_image, Image.open(destination) as repaired_image:
        if repaired_image.size != source_image.size:
            repaired_image = repaired_image.convert("RGB").resize(source_image.size, Image.Resampling.LANCZOS)
            repaired_image.save(destination, quality=95)
    return destination, mime


def repair_patch_with_gemini(
    *,
    source: Path,
    marked_patch: Path,
    blend_mask: Path,
    patch_xy: tuple[int, int, int, int],
    destination_stem: Path,
    prompt: str,
    model: str,
    repair_type: str,
) -> tuple[Path, Path, str]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=resolve_key("GEMINI_API_KEY"))
    image_part = types.Part.from_bytes(data=marked_patch.read_bytes(), mime_type="image/jpeg")
    full_prompt = repair_system_prompt(repair_type, patch=True) + "\n\nReviewer instruction: " + prompt.strip()
    response = client.models.generate_content(
        model=model,
        contents=[full_prompt, image_part],
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    data, mime = response_image(response)
    repaired_patch_path = destination_stem.with_name(destination_stem.name + "__patch_repaired").with_suffix(extension(mime))
    repaired_patch_path.write_bytes(data)

    with Image.open(marked_patch) as original_patch, Image.open(repaired_patch_path) as repaired_patch:
        if repaired_patch.size != original_patch.size:
            repaired_patch = repaired_patch.convert("RGB").resize(original_patch.size, Image.Resampling.LANCZOS)
            repaired_patch.save(repaired_patch_path, quality=95)

    with Image.open(source) as original, Image.open(repaired_patch_path) as repaired_patch, Image.open(blend_mask) as mask:
        original = original.convert("RGB")
        repaired_patch = repaired_patch.convert("RGB")
        mask = mask.convert("L")
        x1, y1, x2, y2 = patch_xy
        base_patch = original.crop(patch_xy)
        blended_patch = Image.composite(repaired_patch, base_patch, mask)
        output = original.copy()
        output.paste(blended_patch, (x1, y1))
        destination = destination_stem.with_suffix(".jpg")
        output.save(destination, quality=95)
    return destination, repaired_patch_path, mime


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DATASET_ROOT), **kwargs)

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        static_prefix = "/static/"
        if parsed_path.startswith(static_prefix):
            rel = unquote(parsed_path[len(static_prefix):])
            static_path = (STATIC_ROOT / rel).resolve()
            try:
                static_path.relative_to(STATIC_ROOT.resolve())
            except ValueError:
                return str(STATIC_ROOT / "__not_found__")
            return str(static_path)
        tool_prefix = "/tools/review_repair/"
        if parsed_path.startswith(tool_prefix):
            rel = unquote(parsed_path[len(tool_prefix):])
            tool_path = (TOOL / rel).resolve()
            try:
                tool_path.relative_to(TOOL.resolve())
            except ValueError:
                return str(TOOL / "__not_found__")
            return str(tool_path)
        input_prefix = "/authoring_input/"
        if parsed_path.startswith(input_prefix):
            rel = unquote(parsed_path[len(input_prefix):])
            input_path = (AUTHORING_INPUT / rel).resolve()
            try:
                input_path.relative_to(AUTHORING_INPUT.resolve())
            except ValueError:
                return str(AUTHORING_INPUT / "__not_found__")
            return str(input_path)
        output_prefix = "/authoring_output/"
        if parsed_path.startswith(output_prefix):
            rel = unquote(parsed_path[len(output_prefix):])
            output_path = (AUTHORING_OUTPUT / rel).resolve()
            try:
                output_path.relative_to(AUTHORING_OUTPUT.resolve())
            except ValueError:
                return str(AUTHORING_OUTPUT / "__not_found__")
            return str(output_path)
        return super().translate_path(path)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/items":
            items = review_manifest_rows()
            for item in items:
                item["base_url"] = f"/{item['base_image']}"
                item["edited_url"] = f"/{item['edited_image']}"
            self.send_json(items)
            return
        if path == "/api/decisions":
            self.send_json(review_decision_rows())
            return
        if path == "/api/authoring/items":
            self.handle_authoring_items()
            return
        if path == "/":
            self.path = "/static/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/upload-image":
            self.handle_upload_image()
            return
        if route == "/api/authoring/upload":
            self.handle_authoring_upload()
            return
        if route == "/api/upload-repair":
            self.handle_upload_repair()
            return
        if route == "/api/authoring/edit":
            self.handle_authoring_edit()
            return
        if route == "/api/authoring/save":
            self.handle_authoring_save()
            return
        if route == "/api/authoring/export":
            self.handle_authoring_export()
            return
        if route == "/api/repair":
            self.handle_repair()
            return
        if route == "/api/reset-repair":
            self.handle_reset_repair()
            return
        if route == "/api/accept-repair":
            self.handle_accept_repair()
            return
        if route == "/api/questions":
            self.handle_question_update()
            return
        if route == "/api/export-boxed-images":
            self.handle_boxed_image_export()
            return
        if route != "/api/decision":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            valid = {row["review_id"] for row in review_manifest_rows()}
            if payload["review_id"] not in valid:
                raise ValueError(f"Unknown review_id: {payload['review_id']}")
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            decisions = {row["review_id"]: row for row in review_decision_rows()}
            decisions[payload["review_id"]] = payload
            write_decisions(decisions)
            self.send_json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_question_update(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self.send_json({"ok": True, **update_review_questions(payload)})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_boxed_image_export(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self.send_json({"ok": True, **export_boxed_images(payload)})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_authoring_items(self) -> None:
        try:
            if authoring_metadata_enabled():
                self.send_json(new_authoring_items())
                return
            if not AUTHORING_INPUT.exists():
                self.send_json({"items": [], "input_root": str(AUTHORING_INPUT), "output_root": str(AUTHORING_OUTPUT)})
                return
            records = authoring_records()
            latest_candidates = latest_authoring_candidates()
            files = [
                path
                for path in sorted(AUTHORING_INPUT.iterdir(), key=lambda p: p.name.lower())
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            ]
            matched_candidates = visually_match_authoring_candidates(files, latest_candidates)
            items = []
            for index, path in enumerate(files, start=1):
                record = records.get(path.name, {})
                item_id = record.get("id") or f"real_{index:03d}"
                items.append(
                    {
                        "id": item_id,
                        "original_filename": path.name,
                        "source_url": f"/authoring_input/{quote(path.name)}",
                        "saved": bool(record),
                        "record": record,
                        "latest_candidate": matched_candidates.get(item_id, {}),
                    }
                )
            self.send_json(
                {
                    "items": items,
                    "input_root": str(AUTHORING_INPUT),
                    "output_root": str(AUTHORING_OUTPUT),
                    "state_file": str(AUTHORING_STATE),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_authoring_upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            upload = save_authoring_upload(
                str(payload.get("filename", "")),
                str(payload.get("data_url", "")),
            )
            self.send_json({"ok": True, "upload": upload})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_authoring_edit(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            edit_record = run_authoring_edit(payload)
            self.send_json({"ok": True, "edit": edit_record})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_authoring_save(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if authoring_metadata_enabled():
                result = save_new_authoring_item(payload)
                self.send_json({"ok": True, **result})
                return
            original_filename = str(payload["original_filename"])
            source = authoring_input_path(original_filename)
            item_id = safe_name(str(payload.get("id") or source.stem)) or safe_name(source.stem)
            source_entity = str(payload.get("source_entity") or "").strip()
            target_entity = str(payload.get("target_entity") or "").strip()
            scene_description = str(payload.get("scene_description") or "").strip()
            if not source_entity or not target_entity or not scene_description:
                raise ValueError("Source entity, target entity, and scene description are required.")
            bbox = payload.get("bbox")
            candidate_rel = str(payload.get("candidate_image") or "").strip()
            if not candidate_rel:
                files = [
                    path
                    for path in sorted(AUTHORING_INPUT.iterdir(), key=lambda p: p.name.lower())
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                ]
                latest = visually_match_authoring_candidates(files, latest_authoring_candidates()).get(item_id, {})
                candidate_rel = str(latest.get("candidate_image") or "").strip()
            if not candidate_rel:
                raise ValueError("Run an edit candidate before saving.")
            candidate = authoring_output_path(candidate_rel)
            if not candidate.exists():
                raise FileNotFoundError(candidate)

            AUTHORING_IMAGES.mkdir(parents=True, exist_ok=True)
            base_target = AUTHORING_IMAGES / f"{item_id}__base.jpg"
            edited_target = AUTHORING_IMAGES / f"{item_id}__edited.jpg"
            save_image_as_target(source, base_target)
            save_image_as_target(candidate, edited_target)
            now = utc_now()
            record = {
                "id": item_id,
                "original_filename": original_filename,
                "base_image": rel_to_authoring_output(base_target),
                "edited_image": rel_to_authoring_output(edited_target),
                "base_url": f"/authoring_output/{quote(rel_to_authoring_output(base_target))}",
                "edited_url": f"/authoring_output/{quote(rel_to_authoring_output(edited_target))}",
                "source_entity": source_entity,
                "target_entity": target_entity,
                "scene_description": scene_description,
                "review_location": str(payload.get("review_location") or "").strip(),
                "source_bbox": bbox,
                "edit_type": str(payload.get("edit_type") or "swap_entity"),
                "candidate_image": candidate_rel,
                "edit_history": payload.get("edit_history") or [],
                "created_at": str(payload.get("created_at") or now),
                "updated_at": now,
            }
            record["questions"] = normalize_flexible_authoring_questions(record, payload.get("questions"))
            records = authoring_records()
            records[original_filename] = record
            write_authoring_records(records)
            export_info = export_authoring_dataset()
            self.send_json({"ok": True, "record": record, "export": export_info})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_authoring_export(self) -> None:
        try:
            export = export_new_authoring_dataset() if authoring_metadata_enabled() else export_authoring_dataset()
            self.send_json({"ok": True, "export": export})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_upload_image(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            saved = save_uploaded_image(str(payload.get("filename", "")), str(payload.get("data_url", "")))
            rel = rel_to_tool(saved)
            self.send_json(
                {
                    "ok": True,
                    "source_image": rel,
                    "source_url": f"/tools/review_repair/{rel}",
                    "filename": saved.name,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_upload_repair(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            repair_record = run_uploaded_patch_repair(payload)
            self.send_json({"ok": True, "repair": repair_record})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_repair(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            review_id = str(payload["review_id"])
            engine = str(payload.get("engine", "gemini")).strip().lower()
            repair_type = str(payload.get("repair_type", "remove_entity"))
            entity_to_remove = str(payload.get("entity_to_remove", "")).strip()
            source_entity = str(payload.get("source_entity", "")).strip()
            target_entity = str(payload.get("target_entity", "")).strip()
            reviewer_prompt = reviewer_instruction(
                repair_type=repair_type,
                entity_to_remove=entity_to_remove,
                source_entity=source_entity,
                target_entity=target_entity,
                prompt=str(payload.get("prompt", "")).strip(),
            )
            pad_pct = float(payload.get("pad_pct", 0.012))
            manifest = {
                row["review_id"]: row
                for row in review_manifest_rows()
            }
            if review_id not in manifest:
                raise ValueError(f"Unknown review_id: {review_id}")
            decisions = {row["review_id"]: row for row in review_decision_rows()}
            decision = decisions.get(review_id, {"review_id": review_id})
            box = payload.get("bbox") or decision.get("edited_bbox")
            if not box:
                raise ValueError("Draw an Edited image box before repairing.")
            item = manifest[review_id]
            source_rel = item["edited_image"]
            source = dataset_path(source_rel)
            if not source.exists():
                raise FileNotFoundError(source)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            stem = safe_name(f"{review_id}__{stamp}__{engine}")
            mask_path, marked_path, xy = make_mask_and_marked(source, box, stem, pad_pct)
            output_stem = REPAIRS / f"{stem}__repaired"
            patch_record: dict[str, Any] = {}
            if engine in {"opencv", "fast"}:
                if repair_type == "swap_entity":
                    raise ValueError("Swap entity requires a Gemini engine, not Fast local inpaint.")
                output = output_stem.with_suffix(source.suffix or ".jpg")
                repair_with_opencv(source, mask_path, output, int(payload.get("radius", 5)))
                returned_mime = "image/jpeg" if output.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                engine_name = "opencv_inpaint"
            elif engine in {"gemini", "semantic"}:
                output, returned_mime = repair_with_gemini(
                    marked_path,
                    output_stem,
                    reviewer_prompt,
                    str(payload.get("model") or "gemini-3.1-flash-image-preview"),
                    repair_type,
                )
                engine_name = "gemini_semantic_edit"
            elif engine in {"gemini_patch", "patch", "local_patch"}:
                (
                    patch_path,
                    patch_marked_path,
                    patch_mask_path,
                    blend_mask_path,
                    _target_xy,
                    patch_xy,
                ) = make_patch_assets(
                    source,
                    box,
                    stem,
                    pad_pct,
                    float(payload.get("patch_margin_pct", 0.035)),
                )
                output, repaired_patch_path, returned_mime = repair_patch_with_gemini(
                    source=source,
                    marked_patch=patch_marked_path,
                    blend_mask=blend_mask_path,
                    patch_xy=patch_xy,
                    destination_stem=output_stem,
                    prompt=reviewer_prompt,
                    model=str(payload.get("model") or "gemini-3.1-flash-image-preview"),
                    repair_type=repair_type,
                )
                engine_name = "gemini_local_patch_edit"
                patch_record = {
                    "patch_image": rel_to_dataset(patch_path),
                    "patch_marked_image": rel_to_dataset(patch_marked_path),
                    "patch_mask_path": rel_to_dataset(patch_mask_path),
                    "blend_mask_path": rel_to_dataset(blend_mask_path),
                    "repaired_patch_image": rel_to_dataset(repaired_patch_path),
                    "patch_pixel_bbox": {"x1": patch_xy[0], "y1": patch_xy[1], "x2": patch_xy[2], "y2": patch_xy[3]},
                }
            else:
                raise ValueError("engine must be 'opencv', 'gemini', or 'gemini_patch'.")
            repair_record = {
                "repair_id": stem,
                "repair_type": repair_type,
                "engine": engine_name,
                "source_image": source_rel,
                "bbox": box,
                "pixel_bbox": {"x1": xy[0], "y1": xy[1], "x2": xy[2], "y2": xy[3]},
                "mask_path": rel_to_dataset(mask_path),
                "marked_image": rel_to_dataset(marked_path),
                "repaired_image": rel_to_dataset(output),
                "returned_mime_type": returned_mime,
                "prompt": reviewer_prompt,
                "entity_to_remove": entity_to_remove,
                "source_entity": source_entity,
                "target_entity": target_entity,
                "created_at": utc_now(),
            }
            repair_record.update(patch_record)
            history = list(decision.get("repair_history") or [])
            history.append(repair_record)
            decision.update(
                {
                    "review_id": review_id,
                    "run": item.get("run"),
                    "pair_id": item.get("pair_id"),
                    "priority": item.get("priority"),
                    "status": "needs_repair",
                    "edited_bbox": box,
                    "repaired_edited_image": repair_record["repaired_image"],
                    "repair_status": "needs_repair",
                    "repair_type": repair_type,
                    "repair_entity": entity_to_remove,
                    "repair_source_entity": source_entity,
                    "repair_target_entity": target_entity,
                    "repair_prompt": reviewer_prompt,
                    "repair_history": history,
                    "updated_at": utc_now(),
                }
            )
            decisions[review_id] = decision
            write_decisions(decisions)
            self.send_json(
                {
                    "ok": True,
                    "review_id": review_id,
                    "repaired_url": f"/{repair_record['repaired_image']}",
                    "repair": repair_record,
                    "decision": decision,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_reset_repair(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            review_id = str(payload["review_id"])
            manifest = {
                row["review_id"]: row
                for row in review_manifest_rows()
            }
            if review_id not in manifest:
                raise ValueError(f"Unknown review_id: {review_id}")
            decisions = {row["review_id"]: row for row in review_decision_rows()}
            decision = decisions.get(review_id, {"review_id": review_id})
            decision.pop("repaired_edited_image", None)
            decision["repair_status"] = "reset_to_original"
            decision["updated_at"] = utc_now()
            decisions[review_id] = decision
            write_decisions(decisions)
            self.send_json({"ok": True, "decision": decision, "edited_url": f"/{manifest[review_id]['edited_image']}"})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def handle_accept_repair(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            review_id = str(payload["review_id"])
            manifest = {
                row["review_id"]: row
                for row in review_manifest_rows()
            }
            if review_id not in manifest:
                raise ValueError(f"Unknown review_id: {review_id}")
            decisions = {row["review_id"]: row for row in review_decision_rows()}
            decision = decisions.get(review_id)
            if not decision or not decision.get("repaired_edited_image"):
                raise ValueError("No repaired edited image is available to accept.")

            item = manifest[review_id]
            edited_path = dataset_path(item["edited_image"])
            repaired_path = dataset_path(str(decision["repaired_edited_image"]))
            if not edited_path.exists():
                raise FileNotFoundError(edited_path)
            if not repaired_path.exists():
                raise FileNotFoundError(repaired_path)

            REPAIRS.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup_path = REPAIRS / f"{safe_name(review_id)}__{stamp}__previous_edited{edited_path.suffix or '.jpg'}"
            shutil.copy2(edited_path, backup_path)
            save_image_as_target(repaired_path, edited_path)

            accept_record = {
                "accepted_at": utc_now(),
                "accepted_repaired_image": rel_to_dataset(repaired_path),
                "backup_previous_edited_image": rel_to_dataset(backup_path),
                "target_edited_image": item["edited_image"],
            }
            accepted = list(decision.get("accepted_repairs") or [])
            accepted.append(accept_record)
            decision.pop("repaired_edited_image", None)
            decision.update(
                {
                    "review_id": review_id,
                    "run": item.get("run"),
                    "pair_id": item.get("pair_id"),
                    "priority": item.get("priority"),
                    "status": "keep"
                    if decision.get("status") in {None, "", "needs_repair", "needs_recheck"}
                    else decision.get("status"),
                    "repair_status": "accepted_into_edited_image",
                    "accepted_repairs": accepted,
                    "updated_at": utc_now(),
                }
            )
            decisions[review_id] = decision
            write_decisions(decisions)
            self.send_json(
                {
                    "ok": True,
                    "review_id": review_id,
                    "edited_url": f"/{item['edited_image']}",
                    "backup_previous_edited_image": rel_to_dataset(backup_path),
                    "decision": decision,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--dataset-root",
        default=".",
        help="Dataset/subset root. Image paths in the manifest are resolved relative to this folder.",
    )
    parser.add_argument("--manifest", default="review_manifest.json")
    parser.add_argument("--decisions", default="review_decisions.jsonl")
    parser.add_argument("--repairs", default="repairs")
    parser.add_argument(
        "--include-generated",
        action="store_true",
        help=(
            "Include candidate_ready samples in human review without running the filter gate. "
            "Their audit records are marked generated_without_screening."
        ),
    )
    parser.add_argument("--authoring-input", default="real_images")
    parser.add_argument("--authoring-output", default="real_authoring_dataset")
    args = parser.parse_args()
    configure_paths(Path(args.dataset_root), args.manifest, args.decisions, args.repairs)
    configure_review_mode(include_generated=args.include_generated)
    configure_authoring(Path(args.authoring_input), Path(args.authoring_output))
    if not DATASET_ROOT.exists():
        raise FileNotFoundError(f"Dataset root does not exist: {DATASET_ROOT}")
    if not MANIFEST.exists() and not canonical_review_enabled():
        print(
            f"Review manifest not found: {MANIFEST}. "
            "Review Dataset mode will be empty until a manifest is provided."
        )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Review UI: http://{args.host}:{args.port}")
    print(f"Dataset root: {DATASET_ROOT}")
    print(f"Manifest: {MANIFEST}")
    print(f"Decisions: {DECISIONS}")
    print(f"Repairs: {REPAIRS}")
    print(
        "Review queue: "
        + (
            "screened failures + generated candidates"
            if INCLUDE_GENERATED
            else "screened failures only"
        )
    )
    print(f"Authoring input: {AUTHORING_INPUT}")
    print(f"Authoring output: {AUTHORING_OUTPUT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
