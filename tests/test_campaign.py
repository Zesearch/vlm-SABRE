from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from vlmbench.campaign import CampaignManager
from vlmbench.data_model import (
    Asset,
    Candidate,
    CandidateStatus,
    HumanReviewDecision,
    MetadataRepository,
    Question,
    Sample,
    SampleStatus,
    ScreeningDecision,
    ScreeningResult,
)


class CampaignManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = MetadataRepository(Path(self.temporary.name))
        self.manager = CampaignManager(self.repository)
        self.manager.initialize(
            campaign_id="context_prior_run",
            task_name="context prior",
            target_accepted=2,
            default_batch_size=100,
        )

    def tearDown(self) -> None:
        from vlmbench.web.authoring_app import server

        server.configure_review_mode(include_generated=False)
        self.temporary.cleanup()

    def add_sample(self, sample_id: str, batch_id: str, status: SampleStatus) -> None:
        edit_id = f"{sample_id}_edit"
        candidate_id = f"{sample_id}_candidate"
        source_asset_id = f"{sample_id}_base"
        candidate_asset_id = f"{sample_id}_edited"
        scope = {"campaign_id": "context_prior_run", "batch_id": batch_id}
        self.repository.upsert(
            "assets",
            Asset(asset_id=source_asset_id, path=f"assets/{source_asset_id}.png", metadata=scope),
        )
        self.repository.upsert(
            "assets",
            Asset(asset_id=candidate_asset_id, path=f"assets/{candidate_asset_id}.png", metadata=scope),
        )
        candidate_status = {
            SampleStatus.RETAINED_FAILURE: CandidateStatus.RETAINED_FAILURE,
            SampleStatus.REJECTED_CORRECT: CandidateStatus.REJECTED_CORRECT,
        }.get(status, CandidateStatus.GENERATED)
        self.repository.upsert(
            "candidates",
            Candidate(
                candidate_id=candidate_id,
                edit_id=edit_id,
                candidate_asset_id=candidate_asset_id,
                status=candidate_status,
                metadata=scope,
            ),
        )
        question_id = f"{sample_id}_question"
        self.repository.upsert(
            "questions",
            Question(
                question_id=question_id,
                sample_id=sample_id,
                edit_id=edit_id,
                image_asset_id=candidate_asset_id,
                image_role="edited",
                question_type="yes_no",
                prompt="Is the target visible?",
                answer="yes",
                eval_type="yes_no_exact",
                metadata=scope,
            ),
        )
        self.repository.upsert(
            "samples",
            Sample(
                sample_id=sample_id,
                source_asset_id=source_asset_id,
                edit_ids=[edit_id],
                question_ids=[question_id],
                status=status,
                metadata=scope,
            ),
        )
        if status in {SampleStatus.RETAINED_FAILURE, SampleStatus.REJECTED_CORRECT}:
            correct = status == SampleStatus.REJECTED_CORRECT
            self.repository.upsert(
                "screening_results",
                ScreeningResult(
                    screening_id=f"{sample_id}_screening",
                    candidate_id=candidate_id,
                    sample_id=sample_id,
                    predictions=[
                        {
                            "question_id": question_id,
                            "raw_prediction": "yes" if correct else "no",
                            "correct": correct,
                        }
                    ],
                    model_correct=correct,
                    decision=(
                        ScreeningDecision.REJECTED_CORRECT
                        if correct
                        else ScreeningDecision.RETAINED_FAILURE
                    ),
                ),
            )

    def test_lossy_batches_accumulate_only_human_accepted_samples(self) -> None:
        batch_1 = self.manager.start_batch(campaign_id="context_prior_run")
        self.assertEqual(batch_1.planned_candidates, 100)
        self.add_sample("kept_from_gate", batch_1.batch_id, SampleStatus.RETAINED_FAILURE)
        self.add_sample("model_got_right", batch_1.batch_id, SampleStatus.REJECTED_CORRECT)

        progress = self.manager.progress("context_prior_run")
        self.assertEqual(progress.accepted, 0)
        self.assertEqual(progress.remaining, 2)
        self.assertEqual(progress.retained_failure, 1)
        self.assertEqual(progress.rejected_correct, 1)
        self.assertEqual([row["review_id"] for row in self.manager.review_items("context_prior_run")], ["kept_from_gate"])

        review = self.manager.record_review(
            review_id="kept_from_gate",
            decision=HumanReviewDecision.ACCEPTED,
        )
        self.assertEqual(review.batch_id, batch_1.batch_id)
        sample = self.repository.load("samples")[0]
        samples = {row["sample_id"]: row for row in self.repository.load("samples")}
        self.assertEqual(samples["kept_from_gate"]["status"], "accepted")
        self.assertTrue(samples["kept_from_gate"]["accepted_candidate_id"])
        self.manager.close_batch(batch_1.batch_id)

        batch_2 = self.manager.start_batch(campaign_id="context_prior_run")
        self.assertEqual(batch_2.sequence, 2)
        self.add_sample("repairable", batch_2.batch_id, SampleStatus.RETAINED_FAILURE)
        self.manager.record_review(
            review_id="repairable",
            decision=HumanReviewDecision.NEEDS_REPAIR,
        )
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.manager.close_batch(batch_2.batch_id)

        self.manager.record_review(
            review_id="repairable",
            decision=HumanReviewDecision.ACCEPTED,
        )
        self.manager.close_batch(batch_2.batch_id)
        final = self.manager.progress("context_prior_run")
        self.assertEqual(final.accepted, 2)
        self.assertEqual(final.remaining, 0)
        self.assertEqual(final.status, "complete")
        self.assertEqual(final.next_batch_size, 0)

    def test_cannot_start_next_batch_before_current_is_closed(self) -> None:
        current = self.manager.start_batch(campaign_id="context_prior_run")
        with self.assertRaisesRegex(ValueError, current.batch_id):
            self.manager.start_batch(campaign_id="context_prior_run")

    def test_web_review_bridge_uses_canonical_queue_and_updates_progress(self) -> None:
        from vlmbench.web.authoring_app import server

        batch = self.manager.start_batch(campaign_id="context_prior_run")
        self.add_sample("web_review", batch.batch_id, SampleStatus.RETAINED_FAILURE)
        server.configure_paths(
            self.repository.root,
            "review_manifest.json",
            "review_decisions.jsonl",
            "repairs",
        )

        items = server.review_manifest_rows()
        self.assertEqual([row["review_id"] for row in items], ["web_review"])
        self.assertEqual(
            items[0]["probes"]["probe_01"]["gemini_prediction"],
            "no",
        )

        server.write_decisions(
            {
                "web_review": {
                    "review_id": "web_review",
                    "status": "keep",
                    "notes": "minor issue is acceptable",
                }
            }
        )
        self.assertEqual(self.manager.progress("context_prior_run").accepted, 1)
        self.assertEqual(server.review_manifest_rows(), [])
        self.assertEqual(server.review_decision_rows()[0]["status"], "keep")

    def test_review_decision_taxonomy_normalizes_legacy_statuses_and_reasons(self) -> None:
        from vlmbench.web.authoring_app import server

        batch = self.manager.start_batch(campaign_id="context_prior_run")
        self.add_sample("repairable", batch.batch_id, SampleStatus.RETAINED_FAILURE)
        self.add_sample("scene_changed", batch.batch_id, SampleStatus.RETAINED_FAILURE)
        server.configure_paths(
            self.repository.root,
            "review_manifest.json",
            "review_decisions.jsonl",
            "repairs",
        )

        server.write_decisions(
            {
                "repairable": {
                    "review_id": "repairable",
                    "status": "needs_recheck",
                    "repair_status": "needs_recheck",
                },
                "scene_changed": {
                    "review_id": "scene_changed",
                    "status": "reject_scene_changed",
                },
            }
        )

        samples = {
            row["sample_id"]: row
            for row in self.repository.load("samples")
        }
        self.assertEqual(samples["repairable"]["status"], "needs_repair")
        self.assertEqual(samples["scene_changed"]["status"], "rejected")

        decisions = {
            row["review_id"]: row
            for row in server.review_decision_rows()
        }
        self.assertEqual(decisions["repairable"]["status"], "needs_repair")
        self.assertEqual(decisions["repairable"]["repair_status"], "needs_repair")
        self.assertEqual(decisions["repairable"]["legacy_status"], "needs_recheck")
        self.assertEqual(
            decisions["repairable"]["legacy_repair_status"],
            "needs_recheck",
        )
        self.assertEqual(decisions["scene_changed"]["status"], "reject_edit_failed")
        self.assertEqual(decisions["scene_changed"]["reason"], "edit_failed")
        self.assertEqual(
            decisions["scene_changed"]["legacy_status"],
            "reject_scene_changed",
        )

        with self.assertRaisesRegex(ValueError, "Invalid review status"):
            server.write_decisions(
                {
                    "repairable": {
                        "review_id": "repairable",
                        "status": "reject",
                    }
                }
            )

    def test_closing_batch_records_unrepaired_generation_as_loss(self) -> None:
        batch = self.manager.start_batch(campaign_id="context_prior_run")
        self.add_sample("failed_edit", batch.batch_id, SampleStatus.PENDING_EDIT)

        self.manager.close_batch(batch.batch_id)

        sample = self.repository.load("samples")[0]
        self.assertEqual(sample["status"], "rejected")
        self.assertEqual(
            sample["metadata"]["rejection_reason"],
            "generation_incomplete_when_batch_closed",
        )

    def test_question_edits_persist_to_canonical_jsonl_with_audit_history(self) -> None:
        from vlmbench.web.authoring_app import server

        batch = self.manager.start_batch(campaign_id="context_prior_run")
        self.add_sample("editable", batch.batch_id, SampleStatus.RETAINED_FAILURE)
        server.configure_paths(
            self.repository.root,
            "review_manifest.json",
            "review_decisions.jsonl",
            "repairs",
        )

        result = server.update_review_questions(
            {
                "review_id": "editable",
                "questions": [
                    {
                        "question_id": "editable_question",
                        "prompt": "Is the replacement object clearly visible?",
                        "answer": "NO",
                    }
                ],
            }
        )

        question = self.repository.load("questions")[0]
        self.assertEqual(question["prompt"], "Is the replacement object clearly visible?")
        self.assertEqual(question["answer"], "no")
        self.assertTrue(question["metadata"]["human_edited"])
        history = question["metadata"]["question_edit_history"]
        self.assertEqual(history[-1]["previous_answer"], "yes")
        self.assertEqual(history[-1]["answer"], "no")
        self.assertEqual(
            result["probes"]["probe_01"]["expected_answer"],
            "no",
        )
        screening = self.repository.load("screening_results")[0]
        self.assertTrue(screening["metadata"]["question_content_edited_after_screening"])
        self.assertEqual(screening["predictions"][0]["raw_prediction"], "no")

        with self.assertRaisesRegex(ValueError, "must be yes or no"):
            server.update_review_questions(
                {
                    "review_id": "editable",
                    "questions": [
                        {
                            "question_id": "editable_question",
                            "prompt": "Still valid?",
                            "answer": "maybe",
                        }
                    ],
                }
            )
        self.assertEqual(self.repository.load("questions")[0]["answer"], "no")

    def test_generated_samples_can_explicitly_bypass_screening_for_review(self) -> None:
        from vlmbench.web.authoring_app import server

        self.add_sample("direct_review", "", SampleStatus.CANDIDATE_READY)
        self.repository.write("campaigns", [])
        server.configure_paths(
            self.repository.root,
            "review_manifest.json",
            "review_decisions.jsonl",
            "repairs",
        )

        server.configure_review_mode(include_generated=False)
        self.assertEqual(server.review_manifest_rows(), [])
        with self.assertRaisesRegex(ValueError, "human review queue"):
            self.manager.record_review(
                review_id="direct_review",
                decision=HumanReviewDecision.ACCEPTED,
            )

        server.configure_review_mode(include_generated=True)
        items = server.review_manifest_rows()
        self.assertEqual([row["review_id"] for row in items], ["direct_review"])
        self.assertEqual(items[0]["entry_source"], "generated_without_screening")
        self.assertEqual(items[0]["run"], "context_prior_run")
        self.assertEqual(
            items[0]["probes"]["probe_01"]["gemini_prediction"],
            "",
        )

        result = server.update_review_questions(
            {
                "review_id": "direct_review",
                "questions": [
                    {
                        "question_id": "direct_review_question",
                        "prompt": "Is the generated target clearly visible?",
                        "answer": "yes",
                    }
                ],
            }
        )
        self.assertEqual(
            result["probes"]["probe_01"]["question"],
            "Is the generated target clearly visible?",
        )

        server.write_decisions(
            {
                "direct_review": {
                    "review_id": "direct_review",
                    "status": "unsure",
                    "notes": "review started without screening",
                }
            }
        )
        pending_item = server.review_manifest_rows()[0]
        self.assertEqual(pending_item["entry_source"], "generated_without_screening")

        server.write_decisions(
            {
                "direct_review": {
                    "review_id": "direct_review",
                    "status": "keep",
                    "notes": "reviewed without screening",
                }
            }
        )
        sample = self.repository.load("samples")[0]
        self.assertEqual(sample["status"], "accepted")
        review = self.repository.load("human_reviews")[0]
        self.assertEqual(review["metadata"]["entry_source"], "generated_without_screening")
        self.assertTrue(review["metadata"]["filter_gate_bypassed"])

    def test_boxed_image_export_preserves_originals_and_draws_saved_boxes(self) -> None:
        from vlmbench.web.authoring_app import server

        batch = self.manager.start_batch(campaign_id="context_prior_run")
        self.add_sample("boxed", batch.batch_id, SampleStatus.RETAINED_FAILURE)
        assets = self.repository.root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        base = assets / "boxed_base.png"
        edited = assets / "boxed_edited.png"
        Image.new("RGB", (200, 100), "white").save(base)
        Image.new("RGB", (200, 100), "white").save(edited)
        server.configure_paths(
            self.repository.root,
            "review_manifest.json",
            "review_decisions.jsonl",
            "repairs",
        )

        result = server.export_boxed_images(
            {
                "review_id": "boxed",
                "base_bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                "edited_bbox": {"x": 0.2, "y": 0.1, "w": 0.4, "h": 0.5},
            }
        )

        exports = {row["role"]: row for row in result["exports"]}
        self.assertEqual(set(exports), {"base", "edited"})
        base_boxed = self.repository.root / exports["base"]["boxed_image"]
        edited_boxed = self.repository.root / exports["edited"]["boxed_image"]
        self.assertTrue(base_boxed.exists())
        self.assertTrue(edited_boxed.exists())
        with Image.open(base_boxed) as image:
            self.assertEqual(image.getpixel((20, 20)), (239, 100, 97))
        with Image.open(edited_boxed) as image:
            self.assertEqual(image.getpixel((40, 10)), (57, 218, 123))
        with Image.open(base) as image:
            self.assertEqual(image.getpixel((20, 20)), (255, 255, 255))

        with self.assertRaisesRegex(ValueError, "normalized image bounds"):
            server.export_boxed_images(
                {
                    "review_id": "boxed",
                    "base_bbox": {"x": 0.9, "y": 0.2, "w": 0.3, "h": 0.4},
                }
            )


if __name__ == "__main__":
    unittest.main()
