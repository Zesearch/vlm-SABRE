from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vlmbench.data_model import (
    Asset,
    Candidate,
    CandidateStatus,
    MetadataRepository,
    ScreeningDecision,
    ScreeningResult,
    validate_metadata,
)


class MetadataRecordTests(unittest.TestCase):
    def test_candidate_round_trip_preserves_extension_fields(self) -> None:
        candidate = Candidate.from_dict(
            {
                "candidate_id": "sample_001_cand_001",
                "edit_id": "sample_001_edit_001",
                "candidate_asset_id": "sample_001_candidate_001",
                "status": "candidate",
                "legacy_score": 0.75,
            }
        )

        self.assertEqual(candidate.status, CandidateStatus.CANDIDATE)
        self.assertEqual(candidate.to_dict()["status"], "candidate")
        self.assertEqual(candidate.to_dict()["legacy_score"], 0.75)

    def test_asset_uses_canonical_identifier(self) -> None:
        asset = Asset(asset_id="asset_001", kind="source_image", path="assets/source.jpg")

        self.assertEqual(asset.record_id, "asset_001")


class ScreeningResultTests(unittest.TestCase):
    def test_model_failure_is_retained(self) -> None:
        result = ScreeningResult.from_model_outcome(
            screening_id="screen_001",
            candidate_id="candidate_001",
            model_correct=False,
        )

        self.assertEqual(result.decision, ScreeningDecision.RETAINED_FAILURE)

    def test_model_success_is_rejected(self) -> None:
        result = ScreeningResult.from_model_outcome(
            screening_id="screen_002",
            candidate_id="candidate_002",
            model_correct=True,
        )

        self.assertEqual(result.decision, ScreeningDecision.REJECTED_CORRECT)

    def test_conflicting_decision_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ScreeningResult(
                screening_id="screen_003",
                candidate_id="candidate_003",
                model_correct=True,
                decision=ScreeningDecision.RETAINED_FAILURE,
            )


class MetadataRepositoryTests(unittest.TestCase):
    def test_repository_round_trip_and_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = MetadataRepository(Path(directory))
            repository.write(
                "assets",
                [Asset(asset_id="asset_001", kind="source_image", path="assets/source.jpg")],
            )
            repository.upsert(
                "assets",
                Asset(asset_id="asset_001", kind="source_image", path="assets/source-v2.jpg"),
            )

            rows = repository.load("assets")
            records = repository.load_records("assets")
            self.assertEqual(rows[0]["path"], "assets/source-v2.jpg")
            self.assertIsInstance(records[0], Asset)

    def test_cross_record_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = MetadataRepository(root)
            source_path = root / "assets" / "source.jpg"
            candidate_path = root / "assets" / "candidate.jpg"
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(b"source")
            candidate_path.write_bytes(b"candidate")

            assets = [
                {"asset_id": "source", "kind": "source_image", "path": "assets/source.jpg"},
                {"asset_id": "candidate_asset", "kind": "candidate_image", "path": "assets/candidate.jpg"},
            ]
            edits = [{"edit_id": "edit", "source_asset_id": "source", "edit_type": "swap_entity"}]
            candidates = [
                {
                    "candidate_id": "candidate",
                    "edit_id": "edit",
                    "candidate_asset_id": "candidate_asset",
                    "status": "candidate",
                }
            ]
            samples = [
                {
                    "sample_id": "sample",
                    "source_asset_id": "source",
                    "edit_ids": ["edit"],
                    "accepted_candidate_id": "candidate",
                    "accepted_edited_asset_id": "candidate_asset",
                    "question_ids": ["question"],
                    "status": "accepted",
                }
            ]
            questions = [
                {
                    "question_id": "question",
                    "sample_id": "sample",
                    "edit_id": "edit",
                    "image_asset_id": "candidate_asset",
                    "question_type": "yes_no",
                    "eval_type": "yes_no_exact",
                }
            ]

            for name, rows in {
                "assets": assets,
                "edits": edits,
                "candidates": candidates,
                "samples": samples,
                "questions": questions,
                "exports": [],
            }.items():
                repository.write(name, rows)

            issues = validate_metadata(
                root=root,
                assets=assets,
                edits=edits,
                candidates=candidates,
                samples=samples,
                questions=questions,
                screening_results=[
                    {
                        "screening_id": "screening",
                        "candidate_id": "candidate",
                        "model_correct": False,
                        "decision": "retained_failure",
                    }
                ],
            )
            self.assertEqual(issues, [])

            questions[0]["image_asset_id"] = "missing"
            issues = validate_metadata(
                root=root,
                assets=assets,
                edits=edits,
                candidates=candidates,
                samples=samples,
                questions=questions,
            )
            self.assertTrue(any(issue.code == "unknown_asset" for issue in issues))

            screening_issues = validate_metadata(
                root=root,
                assets=assets,
                edits=edits,
                candidates=candidates,
                samples=samples,
                questions=questions,
                screening_results=[
                    {
                        "screening_id": "invalid_screening",
                        "candidate_id": "candidate",
                        "model_correct": True,
                        "decision": "retained_failure",
                    }
                ],
            )
            self.assertTrue(
                any(issue.code == "invalid_screening_decision" for issue in screening_issues)
            )


if __name__ == "__main__":
    unittest.main()
