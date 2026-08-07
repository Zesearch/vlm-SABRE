from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from vlmbench.data_model import MetadataRepository
from vlmbench.pipeline import PressureScreeningRunner


class FakePredictionClient:
    provider = "test"
    model = "fake-vlm"

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[tuple[Path, str]] = []

    def predict(self, *, image_path: Path, question: dict[str, Any]) -> str:
        question_id = str(question["question_id"])
        self.calls.append((image_path, question_id))
        return self.answers[question_id]


class PressureScreeningRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = MetadataRepository(self.root)
        for filename in [
            "fail_source.jpg",
            "fail_candidate.jpg",
            "correct_source.jpg",
            "correct_candidate.jpg",
        ]:
            path = self.root / "assets" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(filename.encode("ascii"))

        self.repository.write(
            "assets",
            [
                {
                    "asset_id": "fail_source",
                    "kind": "source_image",
                    "path": "assets/fail_source.jpg",
                },
                {
                    "asset_id": "fail_candidate_asset",
                    "kind": "candidate_image",
                    "path": "assets/fail_candidate.jpg",
                },
                {
                    "asset_id": "correct_source",
                    "kind": "source_image",
                    "path": "assets/correct_source.jpg",
                },
                {
                    "asset_id": "correct_candidate_asset",
                    "kind": "candidate_image",
                    "path": "assets/correct_candidate.jpg",
                },
            ],
        )
        self.repository.write(
            "edits",
            [
                {
                    "edit_id": "fail_edit",
                    "source_asset_id": "fail_source",
                    "edit_type": "swap_entity",
                },
                {
                    "edit_id": "correct_edit",
                    "source_asset_id": "correct_source",
                    "edit_type": "swap_entity",
                },
            ],
        )
        self.repository.write(
            "candidates",
            [
                {
                    "candidate_id": "fail_candidate",
                    "edit_id": "fail_edit",
                    "candidate_asset_id": "fail_candidate_asset",
                    "status": "candidate",
                },
                {
                    "candidate_id": "correct_candidate",
                    "edit_id": "correct_edit",
                    "candidate_asset_id": "correct_candidate_asset",
                    "status": "candidate",
                },
            ],
        )
        self.repository.write(
            "samples",
            [
                {
                    "sample_id": "fail_sample",
                    "source_asset_id": "fail_source",
                    "edit_ids": ["fail_edit"],
                    "question_ids": ["fail_base", "fail_edited"],
                    "status": "candidate_ready",
                },
                {
                    "sample_id": "correct_sample",
                    "source_asset_id": "correct_source",
                    "edit_ids": ["correct_edit"],
                    "question_ids": ["correct_base", "correct_edited"],
                    "status": "candidate_ready",
                },
            ],
        )
        self.repository.write(
            "questions",
            [
                {
                    "question_id": "fail_base",
                    "sample_id": "fail_sample",
                    "edit_id": "fail_edit",
                    "image_asset_id": "fail_source",
                    "image_role": "source",
                    "question_type": "yes_no",
                    "prompt": "Is the source visible?",
                    "answer": "yes",
                    "eval_type": "yes_no_exact",
                },
                {
                    "question_id": "fail_edited",
                    "sample_id": "fail_sample",
                    "edit_id": "fail_edit",
                    "image_asset_id": "fail_source",
                    "image_role": "edited",
                    "question_type": "yes_no",
                    "prompt": "Is the source still visible?",
                    "answer": "no",
                    "eval_type": "yes_no_exact",
                },
                {
                    "question_id": "correct_base",
                    "sample_id": "correct_sample",
                    "edit_id": "correct_edit",
                    "image_asset_id": "correct_source",
                    "image_role": "source",
                    "question_type": "yes_no",
                    "prompt": "Is the source visible?",
                    "answer": "yes",
                    "eval_type": "yes_no_exact",
                },
                {
                    "question_id": "correct_edited",
                    "sample_id": "correct_sample",
                    "edit_id": "correct_edit",
                    "image_asset_id": "correct_source",
                    "image_role": "edited",
                    "question_type": "yes_no",
                    "prompt": "Is the source still visible?",
                    "answer": "no",
                    "eval_type": "yes_no_exact",
                },
            ],
        )
        self.repository.write("exports", [])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_failure_only_hard_gate_and_state_updates(self) -> None:
        client = FakePredictionClient(
            {
                "fail_base": "yes",
                "fail_edited": "yes",
                "correct_base": "yes",
                "correct_edited": "no",
            }
        )
        runner = PressureScreeningRunner(
            repository=self.repository,
            client=client,
            recipe_id="context_prior",
            workers=2,
        )

        summary = runner.run()

        self.assertEqual(summary.retained_failures, 1)
        self.assertEqual(summary.rejected_correct, 1)
        self.assertEqual(summary.errors, 0)
        candidates = {
            row["candidate_id"]: row for row in self.repository.load("candidates")
        }
        samples = {row["sample_id"]: row for row in self.repository.load("samples")}
        self.assertEqual(candidates["fail_candidate"]["status"], "retained_failure")
        self.assertEqual(candidates["correct_candidate"]["status"], "rejected_correct")
        self.assertEqual(samples["fail_sample"]["status"], "retained_failure")
        self.assertEqual(samples["correct_sample"]["status"], "rejected_correct")

        edited_calls = {
            question_id: path.name
            for path, question_id in client.calls
            if question_id.endswith("edited")
        }
        self.assertEqual(edited_calls["fail_edited"], "fail_candidate.jpg")
        self.assertEqual(edited_calls["correct_edited"], "correct_candidate.jpg")

        results = {
            row["candidate_id"]: row
            for row in self.repository.load("screening_results")
        }
        self.assertFalse(results["fail_candidate"]["model_correct"])
        self.assertEqual(results["fail_candidate"]["decision"], "retained_failure")
        self.assertTrue(results["correct_candidate"]["model_correct"])
        self.assertEqual(results["correct_candidate"]["decision"], "rejected_correct")

    def test_existing_screening_is_skipped_without_overwrite(self) -> None:
        client = FakePredictionClient(
            {
                "fail_base": "yes",
                "fail_edited": "yes",
                "correct_base": "yes",
                "correct_edited": "no",
            }
        )
        runner = PressureScreeningRunner(
            repository=self.repository,
            client=client,
            recipe_id="context_prior",
        )
        runner.run()

        second = runner.run(
            candidate_ids=["fail_candidate", "correct_candidate"],
        )

        self.assertEqual(second.processed, 0)
        self.assertEqual(second.skipped_existing, 2)

        overwritten = runner.run(overwrite=True)
        self.assertEqual(overwritten.processed, 2)
        self.assertEqual(overwritten.skipped_existing, 0)


if __name__ == "__main__":
    unittest.main()
