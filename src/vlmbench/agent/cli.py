"""CLI for Codex-assisted task compilation and structured design generation."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from vlmbench.pipeline.design import load_design_schema
from vlmbench.pipeline.design_generation import DesignGenerator
from vlmbench.pipeline.openai_design_client import OpenAIDesignClient, resolve_openai_key

from .codex_client import CodexTaskDesignClient
from .compiler import TaskDesignAgent, write_compilation_artifacts
from .settings import load_project_settings


def add_compile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    parser.add_argument("--task", type=Path, required=True, help="User-authored design.md.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--api-key-file",
        type=Path,
        help="Optional OpenAI API-key file; otherwise use OPENAI_API_KEY.",
    )
    parser.add_argument("--base-url", help="Override settings.json OpenAI base URL.")
    parser.add_argument("--agent-model", help="Override settings.json Codex model.")
    parser.add_argument("--reasoning-effort", help="Override settings.json reasoning effort.")
    parser.add_argument("--template", action="append", dest="template_ids")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved non-secret API configuration and exit without an API call.",
    )
    parser.add_argument("--max-clarification-rounds", type=int)
    parser.add_argument("--overwrite", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use Codex to compile a lightweight benchmark design into a canonical, "
            "filter-compatible task specification."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile")
    add_compile_arguments(compile_parser)

    build_parser = subparsers.add_parser(
        "build",
        help="Compile the task and then call the GPT structured-output design stage.",
    )
    add_compile_arguments(build_parser)
    build_parser.add_argument("--design-model")
    build_parser.add_argument(
        "--count",
        type=int,
        help="Structured designs to generate in this batch; prompted if omitted.",
    )
    build_parser.add_argument("--attempts-per-design", type=int)
    build_parser.add_argument("--max-output-tokens", type=int)
    build_parser.add_argument(
        "--schema",
        default=None,
        help="Override the agent-derived task-local schema.",
    )
    return parser.parse_args()


def clarification_answers(questions: list[str]) -> str:
    answers = []
    for index, question in enumerate(questions, start=1):
        print(f"{index}. {question}")
        answer = input("> ").strip()
        answers.append(f"Q: {question}\nA: {answer}")
    return "\n\n".join(answers)


def prompt_design_count() -> int:
    while True:
        raw = input("How many structured designs should this batch generate? ").strip()
        try:
            count = int(raw)
        except ValueError:
            print("Enter a positive integer.")
            continue
        if count >= 1:
            return count
        print("Enter a positive integer.")


def run_compilation(args: argparse.Namespace, *, api_key: str):
    if args.max_clarification_rounds < 0:
        raise ValueError("max-clarification-rounds cannot be negative.")
    with CodexTaskDesignClient(
        api_key=api_key,
        base_url=args.base_url,
        model=args.agent_model,
        reasoning_effort=args.reasoning_effort,
        working_directory=args.task.parent,
    ) as client:
        agent = TaskDesignAgent(client=client)
        clarification_context = ""
        compilation = agent.compile_file(
            args.task,
            template_ids=args.template_ids,
        )
        rounds = 0
        while (
            compilation.status == "needs_clarification"
            and not args.non_interactive
            and rounds < args.max_clarification_rounds
        ):
            clarification_context += (
                "\n\n" + clarification_answers(compilation.clarification_questions)
            )
            compilation = agent.compile_file(
                args.task,
                template_ids=args.template_ids,
                clarification_context=clarification_context,
            )
            rounds += 1

    paths = write_compilation_artifacts(
        task_path=args.task,
        output_dir=args.output_dir,
        compilation=compilation,
        overwrite=args.overwrite,
    )
    return compilation, paths


def main() -> int:
    args = parse_args()
    settings = load_project_settings(args.settings)
    args.base_url = args.base_url or settings.openai.base_url
    args.agent_model = args.agent_model or settings.openai.agent_model
    args.reasoning_effort = args.reasoning_effort or settings.agent.reasoning_effort
    if args.max_clarification_rounds is None:
        args.max_clarification_rounds = settings.agent.max_clarification_rounds
    if args.command == "build":
        args.design_model = args.design_model or settings.openai.design_model
        if args.count is None:
            if args.non_interactive:
                raise ValueError("--count is required for build with --non-interactive.")
            args.count = prompt_design_count()
        elif args.count < 1:
            raise ValueError("--count must be at least 1.")
        args.attempts_per_design = (
            args.attempts_per_design or settings.design_generation.attempts_per_design
        )
        args.max_output_tokens = (
            args.max_output_tokens or settings.design_generation.max_output_tokens
        )

    run_config = {
        "command": args.command,
        "base_url": args.base_url,
        "agent_model": args.agent_model,
        "reasoning_effort": args.reasoning_effort,
        "canonical_designs": 1,
        "structured_designs": args.count if args.command == "build" else 0,
        "images": 0,
    }
    if args.command == "build":
        run_config["design_model"] = args.design_model
    print(
        "[vlmbench-agent] resolved run configuration:\n"
        + json.dumps(run_config, indent=2, ensure_ascii=False),
        file=sys.stderr,
    )
    if args.dry_run:
        return 0

    api_key = resolve_openai_key(args.task, args.api_key_file)
    compilation, paths = run_compilation(args, api_key=api_key)
    payload = {
        "compilation": compilation.model_dump(
            mode="json",
            exclude={"canonical_design_markdown", "derived_schema_python"},
        ),
        "artifacts": paths,
    }
    if compilation.status != "ready":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2

    if args.command == "build":
        canonical_path = args.output_dir / "canonical_design.md"
        designs_path = args.output_dir / "designs.jsonl"
        schema_spec = args.schema or f"{args.output_dir / 'schema.py'}:TaskDesign"
        schema = load_design_schema(schema_spec)
        design_client = OpenAIDesignClient(
            api_key=api_key,
            base_url=args.base_url,
            model=args.design_model,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
        )
        summary = DesignGenerator(
            client=design_client,
            schema=schema,
            attempts_per_design=args.attempts_per_design,
        ).run(
            task_markdown=canonical_path,
            output=designs_path,
            count=args.count,
            overwrite=args.overwrite,
        )
        payload["design_generation"] = asdict(summary)
        payload["artifacts"]["designs"] = str(designs_path)
        payload["artifacts"]["design_schema"] = schema_spec

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
