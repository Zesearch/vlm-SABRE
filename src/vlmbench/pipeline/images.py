#!/usr/bin/env python3
"""Generate and edit images from validated benchmark designs."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from vlmbench.data_model import MetadataRepository
from vlmbench.campaign import CampaignManager
from vlmbench.data_model import BatchStatus
from vlmbench.pipeline.gemini_client import resolve_gemini_key
from vlmbench.pipeline.gemini_image_client import GeminiImageClient
from vlmbench.pipeline.image_construction import ImageConstructionRunner


class DryRunImageClient:
    provider = "dry_run"

    def __init__(self, model: str) -> None:
        self.model = model

    def generate(self, **_kwargs: Any) -> Any:
        raise RuntimeError("Dry-run client cannot generate images.")

    def edit(self, **_kwargs: Any) -> Any:
        raise RuntimeError("Dry-run client cannot edit images.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Base/Single images and controlled edits from validated design JSONL, "
            "then write canonical benchmark metadata."
        )
    )
    parser.add_argument("--designs", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Gemini image-model identifier.")
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--aspect-ratio", default="3:2")
    parser.add_argument("--resolution", default="2K")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=180_000)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--allow-1k", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--batch-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = MetadataRepository(args.dataset)
    if bool(args.campaign_id) != bool(args.batch_id):
        raise ValueError("--campaign-id and --batch-id must be supplied together")
    campaign_manager = CampaignManager(repository) if args.batch_id else None
    if campaign_manager:
        batch = campaign_manager.get_batch(args.batch_id)
        if batch.campaign_id != args.campaign_id:
            raise ValueError(
                f"Batch {args.batch_id} belongs to {batch.campaign_id}, not {args.campaign_id}"
            )
        if not args.dry_run:
            campaign_manager.set_batch_status(args.batch_id, BatchStatus.GENERATING)
    client = (
        DryRunImageClient(args.model)
        if args.dry_run
        else GeminiImageClient(
            api_key=resolve_gemini_key(repository.root, args.api_key_file),
            model=args.model,
            aspect_ratio=args.aspect_ratio,
            resolution=args.resolution,
            allow_1k=args.allow_1k,
            retries=args.retries,
            timeout_ms=args.timeout_ms,
        )
    )
    runner = ImageConstructionRunner(
        repository=repository,
        client=client,
        workers=args.workers,
        campaign_id=args.campaign_id,
        batch_id=args.batch_id,
    )
    summary = runner.run(
        designs_path=args.designs,
        variants=args.variants,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    if campaign_manager and not args.dry_run:
        campaign_manager.set_batch_status(args.batch_id, BatchStatus.SCREENING)
    print(json.dumps(asdict(summary), indent=2))
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
