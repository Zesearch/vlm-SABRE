#!/usr/bin/env python3
"""CLI for failure-only pressure screening."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from vlmbench.data_model import MetadataRepository
from vlmbench.campaign import CampaignManager
from vlmbench.data_model import BatchStatus
from vlmbench.pipeline.gemini_client import GeminiPredictionClient, resolve_gemini_key
from vlmbench.pipeline.screening import PressureScreeningRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Screen generated candidates with a VLM. Candidates are retained only "
            "when the screening model answers at least one required probe incorrectly."
        )
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Gemini model identifier.")
    parser.add_argument("--recipe-id", default="")
    parser.add_argument("--candidate-id", action="append", dest="candidate_ids")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--batch-id",
        help="Screen only candidates belonging to this campaign batch.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = MetadataRepository(args.dataset)
    candidate_ids = args.candidate_ids
    campaign_manager = CampaignManager(repository) if args.batch_id else None
    if args.batch_id:
        campaign_manager.get_batch(args.batch_id)
        sample_edit_ids = {
            str(edit_id)
            for sample in repository.load("samples")
            if str((sample.get("metadata") or {}).get("batch_id", "")) == args.batch_id
            for edit_id in (sample.get("edit_ids") or [])
        }
        batch_candidate_ids = [
            str(candidate.get("candidate_id", ""))
            for candidate in repository.load("candidates")
            if str(candidate.get("edit_id", "")) in sample_edit_ids
        ]
        if candidate_ids:
            requested = set(candidate_ids)
            candidate_ids = [value for value in batch_candidate_ids if value in requested]
        else:
            candidate_ids = batch_candidate_ids
        if not candidate_ids:
            raise ValueError(f"Batch has no candidates to screen: {args.batch_id}")
    client = GeminiPredictionClient(
        api_key=resolve_gemini_key(repository.root, args.api_key_file),
        model=args.model,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        timeout_ms=args.timeout_ms,
        retries=args.retries,
    )
    runner = PressureScreeningRunner(
        repository=repository,
        client=client,
        recipe_id=args.recipe_id,
        workers=args.workers,
    )
    summary = runner.run(
        candidate_ids=candidate_ids,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    if campaign_manager and not args.dry_run:
        campaign_manager.set_batch_status(args.batch_id, BatchStatus.HUMAN_REVIEW)
    print(json.dumps(asdict(summary), indent=2))
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
