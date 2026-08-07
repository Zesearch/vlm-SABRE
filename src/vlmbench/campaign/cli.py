#!/usr/bin/env python3
"""Manage target-driven, multi-batch benchmark construction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vlmbench.campaign import CampaignManager
from vlmbench.data_model import HumanReviewDecision, MetadataRepository


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--dataset", type=Path, required=True)
    init.add_argument("--campaign-id", required=True)
    init.add_argument("--target-accepted", type=int, required=True)
    init.add_argument("--batch-size", type=int, required=True)
    init.add_argument("--task-name", default="")

    start = commands.add_parser("start-batch")
    start.add_argument("--dataset", type=Path, required=True)
    start.add_argument("--campaign-id")
    start.add_argument("--size", type=int)

    status = commands.add_parser("status")
    status.add_argument("--dataset", type=Path, required=True)
    status.add_argument("--campaign-id")

    close = commands.add_parser("close-batch")
    close.add_argument("--dataset", type=Path, required=True)
    close.add_argument("--batch-id", required=True)

    review = commands.add_parser("review")
    review.add_argument("--dataset", type=Path, required=True)
    review.add_argument("--review-id", required=True)
    review.add_argument("--decision", choices=[item.value for item in HumanReviewDecision], required=True)
    review.add_argument("--reason", default="")
    review.add_argument("--notes", default="")
    return root


def main() -> int:
    args = parser().parse_args()
    manager = CampaignManager(MetadataRepository(args.dataset))
    if args.command == "init":
        result = manager.initialize(
            campaign_id=args.campaign_id,
            target_accepted=args.target_accepted,
            default_batch_size=args.batch_size,
            task_name=args.task_name,
        ).to_dict()
    elif args.command == "start-batch":
        result = manager.start_batch(
            campaign_id=args.campaign_id,
            planned_candidates=args.size,
        ).to_dict()
    elif args.command == "status":
        result = manager.progress(args.campaign_id).to_dict()
    elif args.command == "close-batch":
        result = manager.close_batch(args.batch_id).to_dict()
    else:
        result = manager.record_review(
            review_id=args.review_id,
            decision=args.decision,
            reason=args.reason,
            notes=args.notes,
        ).to_dict()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
