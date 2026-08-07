from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from vlmbench.data_model import MetadataRepository, validate_metadata, write_jsonl
from vlmbench.pipeline import (
    GeneratedImage,
    ImageConstructionRunner,
    PressureScreeningRunner,
)
from vlmbench.recipes import BenchmarkDesign


def png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


def paired_design() -> BenchmarkDesign:
    return BenchmarkDesign(
        concept_id="paired_001",
        pressure_test_type="context_prior",
        objective="Test visual updating after a controlled edit.",
        generation_mode="paired_edit",
        base_prompt="Generate a workshop containing one tape roll.",
        edit_prompt="Keep everything unchanged and replace the tape roll with dental floss.",
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
                "probe_id": "edited_target",
                "image_role": "edited",
                "question_type": "yes_no",
                "prompt": "Is dental floss visible?",
                "answer": "yes",
                "eval_type": "yes_no_exact",
            },
        ],
    )


def single_design() -> BenchmarkDesign:
    return BenchmarkDesign(
        concept_id="single_001",
        pressure_test_type="language_prior",
        objective="Test whether hidden content is guessed from context.",
        generation_mode="single_image",
        image_prompt="Generate an opaque closed mug on a morning office desk.",
        questions=[
            {
                "probe_id": "hidden_content",
                "image_role": "single",
                "question_type": "multiple_choice",
                "prompt": "What drink is inside the mug?",
                "answer": "B",
                "eval_type": "choice_exact",
                "options": {"A": "coffee", "B": "unknown", "C": "tea"},
            }
        ],
    )


class FakeImageClient:
    provider = "test"
    model = "fake-image-model"

    def __init__(self, *, fail_edits: int = 0) -> None:
        self.fail_edits = fail_edits
        self.generate_calls: list[str] = []
        self.edit_calls: list[tuple[Path, str]] = []

    def generate(self, *, prompt: str) -> GeneratedImage:
        self.generate_calls.append(prompt)
        return GeneratedImage(
            data=png_bytes((40, 120, 180)),
            mime_type="image/png",
            metadata={"fake": True},
        )

    def edit(self, *, source_image: Path, prompt: str) -> GeneratedImage:
        self.edit_calls.append((source_image, prompt))
        if self.fail_edits:
            self.fail_edits -= 1
            raise RuntimeError("simulated edit failure")
        return GeneratedImage(
            data=png_bytes((180, 80, 60)),
            mime_type="image/png",
            metadata={"fake": True},
        )


class FakePredictionClient:
    provider = "test"
    model = "fake-screening-model"

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def predict(self, *, image_path: Path, question: dict[str, str]) -> str:
        self.assert_image_exists(image_path)
        return self.answers[str(question["question_id"])]

    @staticmethod
    def assert_image_exists(path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)


class ImageConstructionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = MetadataRepository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_designs(self, designs: list[BenchmarkDesign]) -> Path:
        path = self.root / "designs.jsonl"
        write_jsonl(path, [design.model_dump(mode="json") for design in designs])
        return path

    def test_paired_and_single_designs_create_canonical_metadata(self) -> None:
        designs = self.write_designs([paired_design(), single_design()])
        client = FakeImageClient()
        runner = ImageConstructionRunner(
            repository=self.repository,
            client=client,
            workers=2,
        )

        summary = runner.run(designs_path=designs)

        self.assertEqual(summary.completed, 2)
        self.assertEqual(summary.base_generated, 1)
        self.assertEqual(summary.edited_generated, 1)
        self.assertEqual(summary.single_generated, 1)
        self.assertEqual(summary.errors, 0)
        self.assertEqual(len(client.generate_calls), 2)
        self.assertEqual(len(client.edit_calls), 1)

        assets = self.repository.load("assets")
        edits = self.repository.load("edits")
        candidates = self.repository.load("candidates")
        samples = self.repository.load("samples")
        questions = self.repository.load("questions")
        generation_results = self.repository.load("generation_results")
        self.assertEqual(len(assets), 3)
        self.assertEqual(len(edits), 2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(samples), 2)
        self.assertEqual(len(questions), 3)
        self.assertEqual(len(generation_results), 3)
        self.assertTrue(all(row["status"] == "generated" for row in candidates))
        self.assertTrue(
            all((self.root / row["path"]).exists() for row in assets)
        )

        issues = validate_metadata(
            root=self.root,
            assets=assets,
            edits=edits,
            candidates=candidates,
            samples=samples,
            questions=questions,
            generation_results=generation_results,
        )
        self.assertEqual(issues, [])

        second = runner.run(designs_path=designs)
        self.assertEqual(second.completed, 0)
        self.assertEqual(second.skipped_existing, 2)
        self.assertEqual(len(client.generate_calls), 2)
        self.assertEqual(len(client.edit_calls), 1)

    def test_failed_edit_preserves_base_and_resumes(self) -> None:
        designs = self.write_designs([paired_design()])
        failing_client = FakeImageClient(fail_edits=1)
        first_runner = ImageConstructionRunner(
            repository=self.repository,
            client=failing_client,
        )

        first = first_runner.run(designs_path=designs)

        self.assertEqual(first.errors, 1)
        self.assertEqual(first.base_generated, 1)
        self.assertEqual(first.edited_generated, 0)
        self.assertEqual(len(self.repository.load("assets")), 1)
        self.assertEqual(self.repository.load("samples")[0]["status"], "pending_edit")

        resume_client = FakeImageClient()
        second_runner = ImageConstructionRunner(
            repository=self.repository,
            client=resume_client,
        )
        second = second_runner.run(designs_path=designs)

        self.assertEqual(second.errors, 0)
        self.assertEqual(second.base_generated, 0)
        self.assertEqual(second.edited_generated, 1)
        self.assertEqual(resume_client.generate_calls, [])
        self.assertEqual(len(resume_client.edit_calls), 1)
        self.assertEqual(len(self.repository.load("candidates")), 1)

    def test_dry_run_needs_no_image_calls(self) -> None:
        designs = self.write_designs([paired_design(), single_design()])
        client = FakeImageClient()
        runner = ImageConstructionRunner(
            repository=self.repository,
            client=client,
        )

        summary = runner.run(designs_path=designs, variants=2, dry_run=True)

        self.assertEqual(summary.selected, 4)
        self.assertEqual(summary.planned, 4)
        self.assertTrue(summary.dry_run)
        self.assertEqual(client.generate_calls, [])
        self.assertEqual(client.edit_calls, [])

    def test_campaign_and_batch_are_attached_to_generated_records(self) -> None:
        designs = self.write_designs([single_design()])
        runner = ImageConstructionRunner(
            repository=self.repository,
            client=FakeImageClient(),
            campaign_id="campaign_001",
            batch_id="campaign_001__batch_001",
        )

        runner.run(designs_path=designs)

        for collection in [
            "assets",
            "edits",
            "candidates",
            "samples",
            "questions",
            "generation_results",
        ]:
            row = self.repository.load(collection)[0]
            self.assertEqual(row["metadata"]["campaign_id"], "campaign_001")
            self.assertEqual(row["metadata"]["batch_id"], "campaign_001__batch_001")

    def test_generated_metadata_flows_directly_into_screening(self) -> None:
        designs = self.write_designs([paired_design(), single_design()])
        image_runner = ImageConstructionRunner(
            repository=self.repository,
            client=FakeImageClient(),
        )
        image_runner.run(designs_path=designs)
        question_ids = {
            row["question_id"]: row for row in self.repository.load("questions")
        }
        answers = {
            question_id: str(row["answer"])
            for question_id, row in question_ids.items()
        }
        answers["single_001__v01__hidden_content"] = "A"
        screening_runner = PressureScreeningRunner(
            repository=self.repository,
            client=FakePredictionClient(answers),
        )

        summary = screening_runner.run()

        self.assertEqual(summary.rejected_correct, 1)
        self.assertEqual(summary.retained_failures, 1)
        statuses = {
            row["candidate_id"]: row["status"]
            for row in self.repository.load("candidates")
        }
        self.assertEqual(
            statuses["paired_001__v01_candidate_001"],
            "rejected_correct",
        )
        self.assertEqual(
            statuses["single_001__v01_candidate_001"],
            "retained_failure",
        )


if __name__ == "__main__":
    unittest.main()
