"""JSONL persistence for canonical benchmark metadata."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Asset,
    Batch,
    Campaign,
    Candidate,
    Edit,
    ExportRecord,
    GenerationResult,
    HumanReview,
    MetadataRecord,
    Question,
    Sample,
    ScreeningResult,
)


METADATA_FILES = {
    "assets": "assets.jsonl",
    "edits": "edits.jsonl",
    "candidates": "candidates.jsonl",
    "samples": "samples.jsonl",
    "questions": "questions.jsonl",
    "exports": "exports.jsonl",
}
OPTIONAL_METADATA_FILES = {
    "generation_results": "generation_results.jsonl",
    "screening_results": "screening_results.jsonl",
    "campaigns": "campaigns.jsonl",
    "batches": "batches.jsonl",
    "human_reviews": "human_reviews.jsonl",
}
ALL_METADATA_FILES = {**METADATA_FILES, **OPTIONAL_METADATA_FILES}

RECORD_TYPES: dict[str, type[MetadataRecord]] = {
    "assets": Asset,
    "edits": Edit,
    "candidates": Candidate,
    "samples": Sample,
    "questions": Question,
    "exports": ExportRecord,
    "generation_results": GenerationResult,
    "screening_results": ScreeningResult,
    "campaigns": Campaign,
    "batches": Batch,
    "human_reviews": HumanReview,
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path} at line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object in {path} at line {line_number}.")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any] | MetadataRecord]) -> None:
    """Atomically write dictionaries or canonical records as JSONL."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f"{path.stem}_", suffix=".jsonl", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                payload = row.to_dict() if isinstance(row, MetadataRecord) else row
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def upsert_by_key(
    rows: Iterable[dict[str, Any]],
    key: str,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    value = str(row.get(key, ""))
    if not value:
        raise ValueError(f"Cannot upsert a row without {key}.")
    output = [existing for existing in rows if str(existing.get(key, "")) != value]
    output.append(row)
    return sorted(output, key=lambda item: str(item.get(key, "")))


class MetadataRepository:
    """Read and write the canonical metadata directory for one dataset."""

    def __init__(self, dataset_root: Path) -> None:
        self.root = dataset_root.resolve()
        self.metadata = self.root / "metadata"

    def path(self, name: str) -> Path:
        try:
            filename = ALL_METADATA_FILES[name]
        except KeyError as exc:
            raise KeyError(f"Unknown metadata collection: {name}") from exc
        return self.metadata / filename

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def load(self, name: str) -> list[dict[str, Any]]:
        return load_jsonl(self.path(name))

    def load_records(self, name: str) -> list[MetadataRecord]:
        record_type = RECORD_TYPES[name]
        return [record_type.from_dict(row) for row in self.load(name)]

    def write(self, name: str, rows: Iterable[dict[str, Any] | MetadataRecord]) -> None:
        write_jsonl(self.path(name), rows)

    def upsert(self, name: str, row: dict[str, Any] | MetadataRecord) -> list[dict[str, Any]]:
        record_type = RECORD_TYPES[name]
        payload = row.to_dict() if isinstance(row, MetadataRecord) else row
        updated = upsert_by_key(self.load(name), record_type.id_field, payload)
        self.write(name, updated)
        return updated
