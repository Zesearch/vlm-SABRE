#!/usr/bin/env python3
"""Generate structured benchmark designs from a natural-language Markdown task."""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import asdict
from pathlib import Path

from vlmbench.pipeline.design_generation import DesignGenerator
from vlmbench.pipeline.openai_design_client import OpenAIDesignClient, resolve_openai_key
from vlmbench.pipeline.schema_validation import load_task_schema_file
from vlmbench.recipes.schema import BenchmarkDesign


def load_design_schema(spec: str) -> type[BenchmarkDesign]:
    module_name, separator, class_name = spec.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError("Schema must use the form package.module:ClassName.")
    schema_path = Path(module_name)
    if schema_path.suffix == ".py" or schema_path.exists():
        return load_task_schema_file(schema_path, class_name=class_name)
    module = importlib.import_module(module_name)
    schema = getattr(module, class_name, None)
    if not isinstance(schema, type) or not issubclass(schema, BenchmarkDesign):
        raise TypeError("Custom schema must be a BenchmarkDesign subclass.")
    return schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate schema-validated design JSONL from a natural-language task "
            "description in Markdown."
        )
    )
    parser.add_argument("--task", type=Path, required=True, help="Task-design Markdown file.")
    parser.add_argument("--output", type=Path, required=True, help="Output designs JSONL.")
    parser.add_argument("--model", required=True, help="OpenAI design-model identifier.")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument(
        "--schema",
        required=True,
        help=(
            "Task-specific BenchmarkDesign subclass as schema.py:TaskDesign "
            "or package.module:ClassName."
        ),
    )
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--attempts-per-design", type=int, default=3)
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=24_000)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = OpenAIDesignClient(
        api_key=resolve_openai_key(args.task, args.api_key_file),
        base_url=args.base_url,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=args.max_output_tokens,
    )
    generator = DesignGenerator(
        client=client,
        schema=load_design_schema(args.schema),
        attempts_per_design=args.attempts_per_design,
    )
    summary = generator.run(
        task_markdown=args.task,
        output=args.output,
        count=args.count,
        overwrite=args.overwrite,
    )
    print(json.dumps(asdict(summary), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
