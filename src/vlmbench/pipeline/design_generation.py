"""Shared Markdown-to-structured-design orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from vlmbench.data_model import load_jsonl, write_jsonl
from vlmbench.recipes.schema import BenchmarkDesign
from vlmbench.pipeline.schema_validation import validate_openai_strict_model


class DesignModelClient(Protocol):
    provider: str
    model: str

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        """Generate one object constrained by the supplied Pydantic schema."""


@dataclass(frozen=True)
class DesignGenerationSummary:
    requested: int
    existing: int
    generated: int
    output: str


class DesignGenerator:
    """Generate validated design JSONL from a natural-language Markdown task."""

    def __init__(
        self,
        *,
        client: DesignModelClient,
        schema: type[BenchmarkDesign] = BenchmarkDesign,
        attempts_per_design: int = 3,
    ) -> None:
        if attempts_per_design < 1:
            raise ValueError("attempts_per_design must be at least 1")
        self.client = client
        self.schema = schema
        self.attempts_per_design = attempts_per_design

    @staticmethod
    def _system_prompt(markdown: str) -> str:
        return (
            markdown.strip()
            + "\n\n"
            + "Follow the supplied structured-output schema exactly. Produce one complete "
            "benchmark design. Do not add commentary outside the structured response."
        )

    @staticmethod
    def _user_prompt(
        *,
        index: int,
        count: int,
        existing: list[BenchmarkDesign],
        retry_feedback: str,
    ) -> str:
        existing_summary = [
            {
                "concept_id": design.concept_id,
                "pressure_test_type": design.pressure_test_type,
                "task_attributes": {
                    attribute.name: attribute.value
                    for attribute in design.task_attributes
                },
            }
            for design in existing
        ]
        prompt = (
            f"Create design {index} of {count}. It must be distinct from the existing "
            f"designs below.\n\nExisting designs:\n{existing_summary}"
        )
        if retry_feedback:
            prompt += (
                "\n\nThe previous attempt was rejected. Correct the following problem and "
                f"return a new valid design:\n{retry_feedback}"
            )
        return prompt

    def run(
        self,
        *,
        task_markdown: Path,
        output: Path,
        count: int,
        overwrite: bool = False,
    ) -> DesignGenerationSummary:
        if count < 1:
            raise ValueError("count must be at least 1")
        if not task_markdown.exists():
            raise FileNotFoundError(task_markdown)
        markdown = task_markdown.read_text(encoding="utf-8").strip()
        if not markdown:
            raise ValueError(f"Task Markdown is empty: {task_markdown}")

        # Fail locally before touching an existing output or making a billable
        # request. Fake/non-OpenAI clients remain provider-neutral in tests.
        if self.client.provider == "openai":
            validate_openai_strict_model(self.schema)

        if overwrite:
            write_jsonl(output, [])
        raw_existing = load_jsonl(output)
        designs = [self.schema.model_validate(row) for row in raw_existing]
        existing_count = len(designs)
        if existing_count >= count:
            return DesignGenerationSummary(
                requested=count,
                existing=existing_count,
                generated=0,
                output=str(output),
            )

        system_prompt = self._system_prompt(markdown)
        while len(designs) < count:
            retry_feedback = ""
            last_error: Exception | None = None
            for _attempt in range(1, self.attempts_per_design + 1):
                try:
                    parsed = self.client.generate(
                        system_prompt=system_prompt,
                        user_prompt=self._user_prompt(
                            index=len(designs) + 1,
                            count=count,
                            existing=designs,
                            retry_feedback=retry_feedback,
                        ),
                        schema=self.schema,
                    )
                    design = self.schema.model_validate(parsed)
                    if design.concept_id in {row.concept_id for row in designs}:
                        raise ValueError(
                            f"Duplicate concept_id generated: {design.concept_id}"
                        )
                    designs.append(design)
                    write_jsonl(
                        output,
                        [row.model_dump(mode="json") for row in designs],
                    )
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    retry_feedback = f"{type(exc).__name__}: {exc}"
            if last_error is not None:
                raise RuntimeError(
                    f"Could not generate design {len(designs) + 1} after "
                    f"{self.attempts_per_design} attempt(s): {last_error}"
                ) from last_error

        return DesignGenerationSummary(
            requested=count,
            existing=existing_count,
            generated=len(designs) - existing_count,
            output=str(output),
        )
