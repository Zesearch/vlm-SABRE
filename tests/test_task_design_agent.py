from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from vlmbench.agent import (
    TaskDesignAgent,
    TaskDesignCompilation,
    TaskTemplate,
    TemplateRegistry,
    write_compilation_artifacts,
)
from vlmbench.agent.codex_client import CodexTaskDesignClient
from vlmbench.agent.cli import prompt_design_count


CANONICAL_MARKDOWN = """# Context test

## Task Objective
Test whether the model follows visual evidence.

## Target Failure Mode
The model answers from a context prior instead of the image.

## Image Construction
Use a Base/Edited pair and replace one object.

## Question Format
Ask four independent yes/no questions and require only yes or no.

## Evaluation Protocol
Use yes_no_exact for every question.

## Validity and Rejection Criteria
Reject ambiguous images, duplicate objects, and incomplete edits.

## Diversity Requirements
Vary scenes, source objects, target objects, and placements.
"""

DERIVED_SCHEMA = '''from typing import Literal

from pydantic import Field

from vlmbench.recipes.schema import BenchmarkDesign, ReviewTarget, StrictModel, TaskAttribute


class ContextQuestion(StrictModel):
    probe_id: str = Field(min_length=1)
    image_role: Literal["base", "edited"]
    question_type: Literal["yes_no"]
    prompt: str = Field(min_length=1)
    answer: Literal["yes", "no"]
    eval_type: Literal["yes_no_exact"]


class TaskDesign(BenchmarkDesign):
    pressure_test_type: Literal["context"]
    generation_mode: Literal["paired_edit"]
    base_prompt: str = Field(min_length=1)
    edit_prompt: str = Field(min_length=1)
    image_prompt: Literal[""]
    task_attributes: list[TaskAttribute]
    questions: list[ContextQuestion] = Field(min_length=4, max_length=4)
    review_targets: list[ReviewTarget]
    generation_notes: list[str]
'''


class FakeAgentClient:
    provider = "test"
    model = "fake-codex"

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        self.prompts.append(system_prompt + "\n" + user_prompt)
        return schema.model_validate(self.outputs.pop(0))


def ready_output() -> dict[str, Any]:
    return {
        "status": "ready",
        "task_name": "Context test",
        "canonical_design_markdown": CANONICAL_MARKDOWN,
        "selected_template_ids": [],
        "evaluation_protocols": [
            {
                "question_format": "yes_no",
                "evaluation_mode": "deterministic",
                "eval_type": "yes_no_exact",
                "answer_contract": "Answer only yes or no.",
                "filter_eligible": True,
                "rationale": "The reference answer is binary and image-grounded.",
            }
        ],
        "assumptions": [],
        "warnings": [],
        "clarification_questions": [],
        "derived_schema_python": DERIVED_SCHEMA,
        "schema_adaptations": [
            "Fixed the task to paired_edit with four deterministic yes/no probes."
        ],
    }


class TaskDesignAgentTests(unittest.TestCase):
    def test_compiles_with_only_the_selected_template(self) -> None:
        registry = TemplateRegistry(
            [
                TaskTemplate(
                    template_id="context",
                    title="Context",
                    description="Context edits",
                    keywords=("context prior",),
                    content="CONTEXT TEMPLATE SENTINEL",
                ),
                TaskTemplate(
                    template_id="other",
                    title="Other",
                    description="Other task",
                    keywords=("texture",),
                    content="OTHER TEMPLATE SENTINEL",
                ),
            ]
        )
        client = FakeAgentClient([ready_output()])
        agent = TaskDesignAgent(client=client, registry=registry)

        result = agent.compile_text("Build a context prior test with yes/no questions.")

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.selected_template_ids, ["context"])
        self.assertIn("CONTEXT TEMPLATE SENTINEL", client.prompts[0])
        self.assertNotIn("OTHER TEMPLATE SENTINEL", client.prompts[0])
        self.assertIn("class BenchmarkDesign", client.prompts[0])
        self.assertIn("class TaskDesign(BenchmarkDesign)", client.prompts[0])

    def test_legacy_context_template_id_resolves_to_public_name(self) -> None:
        registry = TemplateRegistry(
            [
                TaskTemplate(
                    template_id="context",
                    title="Context",
                    description="Context edits",
                    keywords=("context",),
                    content="CONTEXT TEMPLATE SENTINEL",
                )
            ]
        )

        selected = registry.select("ignored", requested=["context_prior", "context"])

        self.assertEqual([template.template_id for template in selected], ["context"])

    def test_missing_decision_is_returned_as_clarification(self) -> None:
        output = {
            "status": "needs_clarification",
            "task_name": "Unspecified task",
            "canonical_design_markdown": "",
            "selected_template_ids": [],
            "evaluation_protocols": [],
            "assumptions": [],
            "warnings": [],
            "clarification_questions": ["What question format should be used?"],
            "derived_schema_python": "",
            "schema_adaptations": [],
        }
        result = TaskDesignAgent(
            client=FakeAgentClient([output]),
            registry=TemplateRegistry([]),
        ).compile_text("Test a visual capability.")

        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(len(result.clarification_questions), 1)

    def test_ready_markdown_requires_all_sections(self) -> None:
        output = ready_output()
        output["canonical_design_markdown"] = "# Broken\n\n## Task Objective\nTest."
        agent = TaskDesignAgent(
            client=FakeAgentClient([output]),
            registry=TemplateRegistry([]),
        )

        with self.assertRaisesRegex(ValueError, "missing required section"):
            agent.compile_text("A complete task description.")

    def test_non_deterministic_evaluator_cannot_claim_filter_eligibility(self) -> None:
        output = ready_output()
        output["evaluation_protocols"][0].update(
            {
                "evaluation_mode": "llm_judge",
                "eval_type": "llm_judge",
                "filter_eligible": True,
            }
        )
        with self.assertRaises(ValueError):
            TaskDesignCompilation.model_validate(output)

    def test_artifacts_preserve_raw_and_canonical_designs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "input.md"
            output = root / "compiled"
            task.write_text("# Raw user design\n", encoding="utf-8")
            compilation = TaskDesignCompilation.model_validate(ready_output())

            paths = write_compilation_artifacts(
                task_path=task,
                output_dir=output,
                compilation=compilation,
            )

            self.assertEqual(
                (output / "design.md").read_text(encoding="utf-8"),
                "# Raw user design\n",
            )
            self.assertEqual(
                (output / "canonical_design.md").read_text(encoding="utf-8").strip(),
                CANONICAL_MARKDOWN.strip(),
            )
            self.assertEqual(
                (output / "schema.py").read_text(encoding="utf-8").strip(),
                DERIVED_SCHEMA.strip(),
            )
            self.assertIn("compilation_report", paths)


class CodexTaskDesignClientTests(unittest.TestCase):
    def test_sdk_adapter_requests_structured_read_only_turn(self) -> None:
        calls: dict[str, Any] = {}

        class FakeResult:
            final_response = json.dumps(ready_output())

        class FakeTurn:
            def run(self) -> FakeResult:
                return FakeResult()

        class FakeThread:
            def turn(self, prompt: str, *, output_schema: dict[str, Any]) -> FakeTurn:
                calls["prompt"] = prompt
                calls["output_schema"] = output_schema
                return FakeTurn()

        class FakeCodex:
            def __init__(self, config: object) -> None:
                calls["constructed"] = True
                calls["config"] = config

            def __enter__(self) -> FakeCodex:
                return self

            def __exit__(self, *_args: object) -> None:
                calls["closed"] = True

            def login_api_key(self, api_key: str) -> None:
                calls["api_key"] = api_key

            def thread_start(self, **kwargs: Any) -> FakeThread:
                calls["thread_start"] = kwargs
                return FakeThread()

        fake_module = types.ModuleType("openai_codex")
        fake_module.Codex = FakeCodex
        fake_module.CodexConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_module.Sandbox = types.SimpleNamespace(read_only="read-only")

        with patch.dict("sys.modules", {"openai_codex": fake_module}):
            with CodexTaskDesignClient(
                api_key="test-api-key",
                base_url="https://api.openai.com/v1",
                model="codex-model",
                working_directory=Path.cwd(),
            ) as client:
                result = client.generate(
                    system_prompt="Compile the task.",
                    user_prompt="A user design.",
                    schema=TaskDesignCompilation,
                )

        self.assertEqual(result.status, "ready")
        self.assertEqual(calls["api_key"], "test-api-key")
        self.assertEqual(
            calls["config"].config_overrides,
            ('openai_base_url="https://api.openai.com/v1"',),
        )
        self.assertEqual(calls["thread_start"]["sandbox"], "read-only")
        self.assertTrue(calls["thread_start"]["ephemeral"])
        self.assertEqual(calls["thread_start"]["model"], "codex-model")
        self.assertEqual(calls["output_schema"]["type"], "object")
        self.assertTrue(calls["closed"])

    def test_sdk_adapter_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "API key is required"):
            CodexTaskDesignClient(
                api_key="   ",
                base_url="https://api.openai.com/v1",
                model="codex-model",
            )

    def test_compilation_schema_is_openai_strict(self) -> None:
        schema = TaskDesignCompilation.model_json_schema()
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_design_count_is_prompted_until_positive(self) -> None:
        with patch("builtins.input", side_effect=["not-a-number", "0", "12"]):
            self.assertEqual(prompt_design_count(), 12)


if __name__ == "__main__":
    unittest.main()
