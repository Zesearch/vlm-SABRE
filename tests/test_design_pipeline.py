from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from vlmbench.data_model import load_jsonl
from vlmbench.pipeline import DesignGenerator
from vlmbench.pipeline.design import load_design_schema
from vlmbench.pipeline.openai_design_client import OpenAIDesignClient, resolve_openai_key
from vlmbench.recipes import BenchmarkDesign, QuestionSpec, build_questions
from vlmbench.recipes.schema import StrictModel


class FixedYesNoQuestion(StrictModel):
    probe_id: str
    image_role: Literal["base", "edited"]
    question_type: Literal["yes_no"]
    prompt: str
    answer: Literal["yes", "no"]
    eval_type: Literal["yes_no_exact"]


class FixedContextDesign(BenchmarkDesign):
    pressure_test_type: Literal["context_prior"]
    generation_mode: Literal["paired_edit"]
    questions: list[FixedYesNoQuestion]


def paired_design(concept_id: str = "context_001") -> BenchmarkDesign:
    return BenchmarkDesign(
        concept_id=concept_id,
        pressure_test_type="context_prior",
        objective="Test whether the model follows the edited visual evidence.",
        generation_mode="paired_edit",
        base_prompt="Generate a dense workshop containing one tape roll.",
        edit_prompt="Keep the image unchanged and replace only the tape roll with dental floss.",
        task_attributes=[
            {"name": "source_entity", "value": "tape roll"},
            {"name": "target_entity", "value": "dental floss"},
        ],
        questions=[
            {
                "probe_id": "base_source",
                "image_role": "base",
                "question_type": "yes_no",
                "prompt": "Is a tape roll visible?",
                "answer": "yes",
                "eval_type": "yes_no_exact",
            },
            {
                "probe_id": "edited_target_choice",
                "image_role": "edited",
                "question_type": "multiple_choice",
                "prompt": "Which object is visible at the marked location?",
                "answer": "B",
                "eval_type": "choice_exact",
                "options": {"A": "tape roll", "B": "dental floss"},
            },
            {
                "probe_id": "edited_target_open",
                "image_role": "edited",
                "question_type": "open_generation",
                "prompt": "Name the object at the marked location.",
                "answer": "dental floss",
                "eval_type": "exact_match",
            },
        ],
    )


class FakeDesignClient:
    provider = "test"
    model = "fake-design-model"

    def __init__(self, outputs: list[BenchmarkDesign | dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        output = self.outputs.pop(0)
        return schema.model_validate(output)


class DesignSchemaTests(unittest.TestCase):
    def test_schema_supports_all_question_types(self) -> None:
        design = paired_design()

        self.assertEqual(
            {question.question_type for question in design.questions},
            {"yes_no", "multiple_choice", "open_generation"},
        )

    def test_yes_no_contract_is_strict(self) -> None:
        with self.assertRaises(ValidationError):
            QuestionSpec(
                probe_id="invalid",
                image_role="base",
                question_type="yes_no",
                prompt="Is it visible?",
                answer="maybe",
                eval_type="yes_no_exact",
            )

    def test_multiple_choice_answer_must_reference_an_option(self) -> None:
        with self.assertRaises(ValidationError):
            QuestionSpec(
                probe_id="invalid_mcq",
                image_role="single",
                question_type="multiple_choice",
                prompt="Which answer is correct?",
                answer="C",
                eval_type="choice_exact",
                options={"A": "one", "B": "two"},
            )

    def test_single_image_rejects_edited_questions(self) -> None:
        with self.assertRaises(ValidationError):
            BenchmarkDesign(
                concept_id="language_001",
                pressure_test_type="language_prior",
                objective="Test unsupported inference.",
                generation_mode="single_image",
                image_prompt="Generate a closed opaque mug in a morning office.",
                questions=[
                    {
                        "probe_id": "hidden_content",
                        "image_role": "edited",
                        "question_type": "multiple_choice",
                        "prompt": "What is inside the mug?",
                        "answer": "B",
                        "eval_type": "choice_exact",
                        "options": {"A": "coffee", "B": "unknown"},
                    }
                ],
            )


class DesignGeneratorTests(unittest.TestCase):
    def test_exported_key_precedes_legacy_parent_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "nested" / "task.md"
            task.parent.mkdir()
            task.write_text("task", encoding="utf-8")
            (root / "openai_api_key.txt").write_text("legacy-key", encoding="utf-8")

            with patch.dict("os.environ", {"OPENAI_API_KEY": "exported-key"}):
                self.assertEqual(resolve_openai_key(task), "exported-key")

    def test_explicit_key_file_precedes_exported_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            key_file = root / "selected-key.txt"
            task.write_text("task", encoding="utf-8")
            key_file.write_text("explicit-key", encoding="utf-8")

            with patch.dict("os.environ", {"OPENAI_API_KEY": "exported-key"}):
                self.assertEqual(
                    resolve_openai_key(task, key_file),
                    "explicit-key",
                )

    def test_markdown_generates_validated_design_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            output = root / "designs.jsonl"
            task.write_text("Construct a context-prior pressure test.", encoding="utf-8")
            client = FakeDesignClient([paired_design()])
            generator = DesignGenerator(client=client)

            summary = generator.run(
                task_markdown=task,
                output=output,
                count=1,
            )

            self.assertEqual(summary.generated, 1)
            self.assertIn("context-prior pressure test", client.system_prompts[0])
            rows = load_jsonl(output)
            self.assertEqual(rows[0]["concept_id"], "context_001")
            self.assertEqual(len(rows[0]["questions"]), 3)

    def test_duplicate_concept_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            output = root / "designs.jsonl"
            task.write_text("Construct a pressure test.", encoding="utf-8")
            first = paired_design("context_001")
            client = FakeDesignClient(
                [
                    first,
                    paired_design("context_001"),
                    paired_design("context_002"),
                ]
            )
            generator = DesignGenerator(client=client, attempts_per_design=2)

            summary = generator.run(
                task_markdown=task,
                output=output,
                count=2,
            )

            self.assertEqual(summary.generated, 2)
            self.assertIn("Duplicate concept_id", client.user_prompts[-1])
            self.assertEqual(
                [row["concept_id"] for row in load_jsonl(output)],
                ["context_001", "context_002"],
            )

    def test_default_schema_can_be_loaded_by_cli_spec(self) -> None:
        schema = load_design_schema("vlmbench.recipes.schema:BenchmarkDesign")

        self.assertIs(schema, BenchmarkDesign)

    def test_openai_adapter_uses_structured_output_schema(self) -> None:
        client = OpenAIDesignClient(
            api_key="test-key",
            model="test-model",
            reasoning_effort="high",
        )

        class FakeResponses:
            def __init__(self) -> None:
                self.kwargs: dict[str, Any] = {}

            def parse(self, **kwargs: Any) -> Any:
                self.kwargs = kwargs
                return type("Response", (), {"output_parsed": paired_design()})()

        responses = FakeResponses()
        client.client = type("FakeOpenAI", (), {"responses": responses})()
        result = client.generate(
            system_prompt="Task instructions",
            user_prompt="Create one design",
            schema=FixedContextDesign,
        )

        self.assertEqual(result.concept_id, "context_001")
        self.assertIs(responses.kwargs["text_format"], FixedContextDesign)
        self.assertEqual(responses.kwargs["reasoning"], {"effort": "high"})


class QuestionBuilderTests(unittest.TestCase):
    def test_builder_reads_design_and_maps_assets(self) -> None:
        rows = build_questions(
            design=paired_design(),
            sample_id="sample_001",
            edit_id="edit_001",
            source_asset_id="asset_base",
            edited_asset_id="asset_edited",
        )

        self.assertEqual(rows[0].image_asset_id, "asset_base")
        self.assertEqual(rows[0].image_role, "source")
        self.assertEqual(rows[1].image_asset_id, "asset_edited")
        self.assertEqual(rows[1].options["B"], "dental floss")
        self.assertEqual(rows[2].eval_type, "exact_match")


if __name__ == "__main__":
    unittest.main()
