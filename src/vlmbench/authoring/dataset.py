#!/usr/bin/env python3
"""Utilities for the redesigned real-data authoring dataset layout."""

from __future__ import annotations

import argparse
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from vlmbench.data_model import (
    METADATA_FILES,
    MetadataRepository,
    validate_metadata,
    write_jsonl,
)

Q1Q4_PROBES = ("base_source", "base_target", "edited_source", "edited_target")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def next_sample_number(samples: list[dict[str, Any]]) -> int:
    values = []
    for sample in samples:
        sample_id = str(sample.get("sample_id", ""))
        if sample_id.startswith("real_"):
            try:
                values.append(int(sample_id.rsplit("_", 1)[1]))
            except ValueError:
                pass
    return (max(values) + 1) if values else 1


def image_info(path: Path) -> tuple[int, int, str]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
    return width, height, mime


class Dataset:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.repository = MetadataRepository(self.root)
        self.metadata = self.repository.metadata
        self.assets = self.repository.load("assets")
        self.edits = self.repository.load("edits")
        self.candidates = self.repository.load("candidates")
        self.samples = self.repository.load("samples")
        self.questions = self.repository.load("questions")
        self.generation_results = self.repository.load("generation_results")
        self.screening_results = self.repository.load("screening_results")
        self.asset_by_id = {row["asset_id"]: row for row in self.assets}
        self.edit_by_id = {row["edit_id"]: row for row in self.edits}
        self.candidate_by_id = {row["candidate_id"]: row for row in self.candidates}
        self.questions_by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for question in self.questions:
            self.questions_by_sample[str(question.get("sample_id", ""))].append(question)

    def asset_path(self, asset_id: str) -> Path:
        return self.root / self.asset_by_id[asset_id]["path"]

    def question_image_path(self, question: dict[str, Any]) -> str:
        asset_id = str(question.get("image_asset_id") or "")
        return str(self.asset_by_id[asset_id]["path"])


def validate_dataset(dataset: Dataset) -> list[str]:
    issues = validate_metadata(
        root=dataset.root,
        assets=dataset.assets,
        edits=dataset.edits,
        candidates=dataset.candidates,
        samples=dataset.samples,
        questions=dataset.questions,
        generation_results=dataset.generation_results,
        screening_results=dataset.screening_results,
    )
    return [str(issue) for issue in issues]


def summarize(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    asset_kinds = Counter(str(row.get("kind", "")) for row in dataset.assets)
    sample_status = Counter(str(row.get("status", "")) for row in dataset.samples)
    question_types = Counter(str(row.get("question_type", "")) for row in dataset.questions)
    eval_types = Counter(str(row.get("eval_type", "")) for row in dataset.questions)

    print(f"Dataset: {dataset.root}")
    print(f"Assets: {len(dataset.assets)} {dict(asset_kinds)}")
    print(f"Edits: {len(dataset.edits)}")
    print(f"Candidates: {len(dataset.candidates)}")
    print(f"Samples: {len(dataset.samples)} {dict(sample_status)}")
    print(f"Questions: {len(dataset.questions)} {dict(question_types)}")
    print(f"Eval types: {dict(eval_types)}")
    return 0


def validate(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    errors = validate_dataset(dataset)
    if errors:
        print(f"Validation failed: {len(errors)} issue(s)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validation passed.")
    return 0


def import_images(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    source_dir = args.source_dir or (dataset.root / "source_images")
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)

    existing_filenames = {
        str((row.get("metadata") or {}).get("original_filename") or row.get("source_filename") or "")
        for row in dataset.assets
        if str(row.get("kind", "")) == "source_image"
    }
    existing_paths = {str(row.get("path", "")) for row in dataset.assets}
    assets = list(dataset.assets)
    edits = list(dataset.edits)
    samples = list(dataset.samples)
    next_number = next_sample_number(samples)
    imported: list[str] = []
    skipped: list[str] = []

    for path in sorted(source_dir.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.name in existing_filenames:
            skipped.append(f"{path.name}: already registered")
            continue
        while True:
            sample_id = f"real_{next_number:03d}"
            next_number += 1
            source_rel = f"assets/source/{sample_id}.jpg"
            if source_rel not in existing_paths and not any(str(row.get("sample_id", "")) == sample_id for row in samples):
                break
        target = dataset.root / source_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            shutil.copy2(path, target)
        else:
            from PIL import Image

            with Image.open(path) as image:
                image.convert("RGB").save(target, quality=95)
        width, height, mime = image_info(target)
        source_asset_id = f"{sample_id}_source"
        edit_id = f"{sample_id}_edit_001"
        assets.append(
            {
                "asset_id": source_asset_id,
                "kind": "source_image",
                "path": source_rel,
                "mime_type": mime,
                "width": width,
                "height": height,
                "source_filename": path.name,
                "created_at": "",
                "metadata": {"original_filename": path.name},
            }
        )
        edits.append(
            {
                "edit_id": edit_id,
                "source_asset_id": source_asset_id,
                "edit_type": "swap_entity",
                "instruction": "",
                "bbox": None,
                "metadata": {"source_entity": "", "target_entity": "", "scene_description": ""},
                "created_at": "",
                "updated_at": "",
            }
        )
        samples.append(
            {
                "sample_id": sample_id,
                "source_asset_id": source_asset_id,
                "edit_ids": [edit_id],
                "accepted_candidate_id": "",
                "accepted_edited_asset_id": "",
                "question_ids": [],
                "tags": ["real_image", "entity_swap"],
                "split": "validation",
                "status": "pending_edit",
                "created_at": "",
                "updated_at": "",
                "metadata": {"original_filename": path.name},
            }
        )
        imported.append(f"{sample_id}: {path.name}")
        existing_filenames.add(path.name)
        existing_paths.add(source_rel)

    if not args.dry_run:
        write_jsonl(dataset.metadata / METADATA_FILES["assets"], sorted(assets, key=lambda row: str(row.get("asset_id", ""))))
        write_jsonl(dataset.metadata / METADATA_FILES["edits"], sorted(edits, key=lambda row: str(row.get("edit_id", ""))))
        write_jsonl(dataset.metadata / METADATA_FILES["samples"], sorted(samples, key=lambda row: str(row.get("sample_id", ""))))

    print(f"Imported {len(imported)} image(s).")
    for item in imported:
        print(f"- {item}")
    if skipped:
        print(f"Skipped {len(skipped)} image(s).")
        for item in skipped:
            print(f"- {item}")
    if args.dry_run:
        print("Dry run only; metadata was not changed.")
    return 0


def retire_samples(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    target_ids = set(args.sample_ids)
    if not target_ids:
        raise ValueError("Pass at least one sample_id.")
    samples = []
    changed = 0
    for sample in dataset.samples:
        if str(sample.get("sample_id", "")) in target_ids:
            sample = dict(sample)
            sample["status"] = args.status
            sample["split"] = "retired"
            sample.setdefault("metadata", {})
            sample["metadata"] = {**(sample.get("metadata") or {}), "retired_reason": args.reason}
            changed += 1
        samples.append(sample)
    missing = sorted(target_ids - {str(row.get("sample_id", "")) for row in dataset.samples})
    if not args.dry_run:
        write_jsonl(dataset.metadata / METADATA_FILES["samples"], sorted(samples, key=lambda row: str(row.get("sample_id", ""))))
    print(f"Retired {changed} sample(s).")
    if missing:
        print("Missing:")
        for sample_id in missing:
            print(f"- {sample_id}")
    if args.dry_run:
        print("Dry run only; metadata was not changed.")
    return 0


def default_q1q4_questions(dataset: Dataset, sample: dict[str, Any]) -> list[dict[str, Any]]:
    sample_id = str(sample["sample_id"])
    edit_ids = sample.get("edit_ids") or []
    edit = dataset.edit_by_id.get(str(edit_ids[0]), {}) if edit_ids else {}
    metadata = edit.get("metadata") or {}
    source = str(metadata.get("source_entity") or "").strip()
    target = str(metadata.get("target_entity") or "").strip()
    if not source or not target:
        raise ValueError(f"sample {sample_id} is missing source_entity or target_entity")
    source_asset_id = str(sample["source_asset_id"])
    edited_asset_id = str(sample.get("accepted_edited_asset_id") or "")
    if not edited_asset_id:
        raise ValueError(f"sample {sample_id} has no accepted edited asset")
    return [
        {
            "question_id": f"{sample_id}__base_source",
            "sample_id": sample_id,
            "edit_id": str(edit.get("edit_id") or ""),
            "image_asset_id": source_asset_id,
            "image_role": "source",
            "question_type": "yes_no",
            "prompt": f"Is the following object visible in this image: {source}?",
            "answer": "yes",
            "eval_type": "yes_no_exact",
            "metadata": {"probe": "base_source"},
        },
        {
            "question_id": f"{sample_id}__base_target",
            "sample_id": sample_id,
            "edit_id": str(edit.get("edit_id") or ""),
            "image_asset_id": source_asset_id,
            "image_role": "source",
            "question_type": "yes_no",
            "prompt": f"Is the following object visible in this image: {target}?",
            "answer": "no",
            "eval_type": "yes_no_exact",
            "metadata": {"probe": "base_target"},
        },
        {
            "question_id": f"{sample_id}__edited_source",
            "sample_id": sample_id,
            "edit_id": str(edit.get("edit_id") or ""),
            "image_asset_id": edited_asset_id,
            "image_role": "edited",
            "question_type": "yes_no",
            "prompt": f"Is the following object visible in this image: {source}?",
            "answer": "no",
            "eval_type": "yes_no_exact",
            "metadata": {"probe": "edited_source"},
        },
        {
            "question_id": f"{sample_id}__edited_target",
            "sample_id": sample_id,
            "edit_id": str(edit.get("edit_id") or ""),
            "image_asset_id": edited_asset_id,
            "image_role": "edited",
            "question_type": "yes_no",
            "prompt": f"Is the following object visible in this image: {target}?",
            "answer": "yes",
            "eval_type": "yes_no_exact",
            "metadata": {"probe": "edited_target"},
        },
    ]


def seed_q1q4(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    existing_by_id = {row["question_id"]: row for row in dataset.questions}
    seeded = 0
    skipped: list[str] = []
    for sample in dataset.samples:
        if not sample.get("accepted_edited_asset_id"):
            skipped.append(f"{sample.get('sample_id')}: no accepted edited asset")
            continue
        try:
            rows = default_q1q4_questions(dataset, sample)
        except ValueError as exc:
            skipped.append(str(exc))
            continue
        for row in rows:
            if row["question_id"] in existing_by_id and not args.overwrite:
                continue
            existing_by_id[row["question_id"]] = row
            seeded += 1
    questions = [existing_by_id[key] for key in sorted(existing_by_id)]
    write_jsonl(dataset.metadata / METADATA_FILES["questions"], questions)
    print(f"Seeded/updated {seeded} Q1-Q4 question row(s).")
    if skipped:
        print("Skipped:")
        for item in skipped:
            print(f"- {item}")
    return 0


def export_q1q4(args: argparse.Namespace) -> int:
    dataset = Dataset(args.dataset)
    errors = validate_dataset(dataset)
    if errors:
        print("Cannot export invalid dataset. Run validate for details.")
        return 1

    pairs: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    dataset_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    questions_by_sample_probe: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for question in dataset.questions:
        probe = str((question.get("metadata") or {}).get("probe") or "")
        if probe:
            questions_by_sample_probe[str(question.get("sample_id"))][probe] = question

    for sample in dataset.samples:
        sample_id = str(sample["sample_id"])
        edited_asset_id = str(sample.get("accepted_edited_asset_id") or "")
        if not edited_asset_id:
            skipped.append(f"{sample_id}: no accepted edited asset")
            continue
        probe_map = questions_by_sample_probe.get(sample_id, {})
        missing = [probe for probe in Q1Q4_PROBES if probe not in probe_map]
        if missing:
            skipped.append(f"{sample_id}: missing probes {','.join(missing)}")
            continue

        source_asset = dataset.asset_by_id[str(sample["source_asset_id"])]
        edited_asset = dataset.asset_by_id[edited_asset_id]
        edit_ids = sample.get("edit_ids") or []
        edit = dataset.edit_by_id.get(str(edit_ids[0]), {}) if edit_ids else {}
        metadata = edit.get("metadata") or {}
        pair = {
            "id": sample_id,
            "sample_group": dataset.root.name,
            "base_image": source_asset["path"],
            "edited_image": edited_asset["path"],
            "source_entity": metadata.get("source_entity", ""),
            "inserted_entity": metadata.get("target_entity", ""),
            "review_location": "",
            "scene_description": metadata.get("scene_description", ""),
            "base_bbox_normalized": edit.get("bbox"),
            "source_filename": source_asset.get("source_filename", ""),
            "edit_type": edit.get("edit_type", ""),
            "created_at": sample.get("created_at", ""),
            "updated_at": sample.get("updated_at", ""),
        }
        q_rows = []
        for probe in Q1Q4_PROBES:
            question = probe_map[probe]
            q_rows.append(
                {
                    "id": question["question_id"],
                    "pair_id": sample_id,
                    "probe": probe,
                    "image_role": "base" if probe.startswith("base_") else "edited",
                    "image": dataset.question_image_path(question),
                    "question": question["prompt"],
                    "answer": question["answer"],
                }
            )
        pairs.append(pair)
        questions.extend(q_rows)
        dataset_rows.append({**pair, "questions": q_rows})

    output = args.output or (dataset.root / "exports" / "q1q4_context_prior")
    write_jsonl(output / "pairs.jsonl", pairs)
    write_jsonl(output / "questions.jsonl", questions)
    write_jsonl(output / "dataset.jsonl", dataset_rows)
    print(f"Exported {len(pairs)} sample(s), {len(questions)} question(s) to {output}")
    if skipped:
        print("Skipped:")
        for item in skipped:
            print(f"- {item}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-data authoring dataset utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, fn in [
        ("summarize", summarize),
        ("validate", validate),
        ("seed-q1q4", seed_q1q4),
        ("export-q1q4", export_q1q4),
    ]:
        sub = subparsers.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        sub.set_defaults(func=fn)
        if name == "seed-q1q4":
            sub.add_argument("--overwrite", action="store_true")
        if name == "export-q1q4":
            sub.add_argument("--output", type=Path)

    sub = subparsers.add_parser("import-images")
    sub.add_argument("--dataset", type=Path, required=True)
    sub.add_argument("--source-dir", type=Path)
    sub.add_argument("--dry-run", action="store_true")
    sub.set_defaults(func=import_images)

    sub = subparsers.add_parser("retire-sample")
    sub.add_argument("--dataset", type=Path, required=True)
    sub.add_argument("sample_ids", nargs="+")
    sub.add_argument("--status", default="retired")
    sub.add_argument("--reason", default="")
    sub.add_argument("--dry-run", action="store_true")
    sub.set_defaults(func=retire_samples)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
