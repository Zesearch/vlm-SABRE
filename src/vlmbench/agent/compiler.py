"""Compile a lightweight user task into a canonical, filter-compatible design."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol, TypeVar
from importlib.resources import files

from pydantic import BaseModel

from .models import TaskDesignCompilation
from .templates import TaskTemplate, TemplateRegistry
from vlmbench.pipeline.schema_validation import validate_task_schema_text


ModelT = TypeVar("ModelT", bound=BaseModel)

REQUIRED_SECTIONS = (
    "Task Objective",
    "Target Failure Mode",
    "Image Construction",
    "Question Format",
    "Evaluation Protocol",
    "Validity and Rejection Criteria",
    "Diversity Requirements",
)


class TaskDesignModelClient(Protocol):
    provider: str
    model: str

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[ModelT],
    ) -> ModelT:
        """Return one schema-constrained task-design compilation."""


class TaskDesignAgent:
    """Use Codex to compile and validate one user-authored task design."""

    SYSTEM_PROMPT = """You compile lightweight VLM benchmark task descriptions into canonical designs.

Preserve the user's intent. Never invent a missing decision that changes the task. If a required
decision is absent or ambiguous, return status=needs_clarification and ask short, concrete questions.

The canonical Markdown must contain these exact H2 sections in this order:
1. Task Objective
2. Target Failure Mode
3. Image Construction
4. Question Format
5. Evaluation Protocol
6. Validity and Rejection Criteria
7. Diversity Requirements
8. Additional Constraints (optional)

Question Format must state the response shape and reference-answer contract. Evaluation Protocol
must select one of the supported deterministic evaluators: yes_no_exact, choice_exact, count_exact,
exact_match, or contains. The initial automated filter gate does not support LLM-judge or manual
evaluation. If a proposed semantic open-generation question cannot be scored reliably by a supported
evaluator, return needs_clarification and ask the user to constrain it before continuing.

Use templates only as references. Do not copy task-specific entities or scenes unless the user asks
for them.

Derive a concrete task-local Pydantic schema from the supplied base schema reference. The base file
is a few-shot example, never a universal runtime schema and never modified. Return the complete
derived Python source in derived_schema_python without Markdown fences. It must:
- define a class named TaskDesign that subclasses BenchmarkDesign;
- use only declarative Pydantic fields and the imports demonstrated by the strict derivation example;
- preserve exactly the stable top-level and question envelopes inherited from BenchmarkDesign;
- put task-specific values in task_attributes rather than adding top-level fields;
- replace generic question shapes with concrete task-specific question models;
- use fixed object properties, never dict, Dict, Mapping, Any, or arbitrary option keys;
- make the task's generation mode, question format, answer contract, and evaluator concrete;
- remain compatible with OpenAI strict structured outputs.

If a fixed schema cannot be derived because the question format, number of multiple-choice options,
image roles, or answer contract is ambiguous, return needs_clarification instead of guessing.
Return only the requested structured object."""

    def __init__(
        self,
        *,
        client: TaskDesignModelClient,
        registry: TemplateRegistry | None = None,
    ) -> None:
        self.client = client
        self.registry = registry or TemplateRegistry()

    @staticmethod
    def _template_context(templates: list[TaskTemplate]) -> str:
        if not templates:
            return "No specialized template was selected. Use the canonical section contract."
        blocks = []
        for template in templates:
            blocks.append(
                f"### Template: {template.template_id} — {template.title}\n\n"
                f"{template.content.strip()}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _base_schema_context() -> str:
        return (
            files("vlmbench.recipes")
            .joinpath("schema.py")
            .read_text(encoding="utf-8")
            .strip()
        )

    @staticmethod
    def _derived_schema_example() -> str:
        return (
            files("vlmbench.agent")
            .joinpath("templates", "base_schema.py")
            .read_text(encoding="utf-8")
            .strip()
        )

    @staticmethod
    def _normalize_heading(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    @classmethod
    def validate_canonical_markdown(cls, markdown: str) -> None:
        headings = [
            match.group(1).strip()
            for match in re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
        ]
        normalized = {cls._normalize_heading(heading) for heading in headings}
        missing = [
            section
            for section in REQUIRED_SECTIONS
            if cls._normalize_heading(section) not in normalized
        ]
        if missing:
            raise ValueError(
                "Canonical design is missing required section(s): "
                + ", ".join(missing)
            )

    def compile_text(
        self,
        markdown: str,
        *,
        template_ids: list[str] | None = None,
        clarification_context: str = "",
    ) -> TaskDesignCompilation:
        if not markdown.strip():
            raise ValueError("Task design Markdown cannot be empty.")
        templates = self.registry.select(markdown, requested=template_ids)
        user_prompt = (
            "## User-authored design.md\n\n"
            f"{markdown.strip()}\n\n"
            "## Selected reference templates\n\n"
            f"{self._template_context(templates)}\n\n"
            "## Base schema reference (few-shot only; derive a task-local schema)\n\n"
            f"```python\n{self._base_schema_context()}\n```\n\n"
            "## Example of a strict task-local derivation\n\n"
            f"```python\n{self._derived_schema_example()}\n```"
        )
        if clarification_context.strip():
            user_prompt += (
                "\n\n## User clarifications\n\n" + clarification_context.strip()
            )
        compilation = self.client.generate(
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=TaskDesignCompilation,
        )
        selected_ids = [template.template_id for template in templates]
        if compilation.selected_template_ids != selected_ids:
            compilation = compilation.model_copy(
                update={"selected_template_ids": selected_ids}
            )
        if compilation.status == "ready":
            self.validate_canonical_markdown(compilation.canonical_design_markdown)
            validate_task_schema_text(compilation.derived_schema_python)
        return compilation

    def compile_file(
        self,
        task_path: Path,
        *,
        template_ids: list[str] | None = None,
        clarification_context: str = "",
    ) -> TaskDesignCompilation:
        if not task_path.exists():
            raise FileNotFoundError(task_path)
        return self.compile_text(
            task_path.read_text(encoding="utf-8"),
            template_ids=template_ids,
            clarification_context=clarification_context,
        )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def write_compilation_artifacts(
    *,
    task_path: Path,
    output_dir: Path,
    compilation: TaskDesignCompilation,
    overwrite: bool = False,
) -> dict[str, str]:
    """Preserve the input and write the canonical design plus an audit report."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "design": output_dir / "design.md",
        "canonical_design": output_dir / "canonical_design.md",
        "schema": output_dir / "schema.py",
        "compilation_report": output_dir / "compilation_report.json",
    }
    targets = [paths["design"], paths["compilation_report"]]
    if compilation.canonical_design_markdown.strip():
        targets.append(paths["canonical_design"])
    if compilation.derived_schema_python.strip():
        targets.append(paths["schema"])
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Refusing to overwrite existing artifact(s): "
            + ", ".join(str(path) for path in existing)
        )

    _atomic_write_text(paths["design"], task_path.read_text(encoding="utf-8"))
    if compilation.canonical_design_markdown.strip():
        _atomic_write_text(
            paths["canonical_design"],
            compilation.canonical_design_markdown.rstrip() + "\n",
        )
    if compilation.derived_schema_python.strip():
        _atomic_write_text(
            paths["schema"],
            compilation.derived_schema_python.rstrip() + "\n",
        )
    report = compilation.model_dump(
        mode="json",
        exclude={"canonical_design_markdown", "derived_schema_python"},
    )
    report["schema_class_name"] = "TaskDesign"
    _atomic_write_text(
        paths["compilation_report"],
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
    )
    return {name: str(path) for name, path in paths.items() if path.exists()}
