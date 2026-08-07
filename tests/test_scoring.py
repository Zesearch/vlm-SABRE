from __future__ import annotations

import unittest

from vlmbench.eval import score_prediction


class ScoringTests(unittest.TestCase):
    def test_yes_no_scoring(self) -> None:
        result = score_prediction(
            {"question_id": "q1", "answer": "no", "eval_type": "yes_no_exact"},
            "No, it is not visible.",
        )
        self.assertTrue(result["correct"])
        self.assertEqual(result["prediction_normalized"], "no")

    def test_count_scoring_accepts_number_words(self) -> None:
        result = score_prediction(
            {"question_id": "q2", "answer": "5", "eval_type": "count_exact"},
            "five",
        )
        self.assertTrue(result["correct"])

    def test_choice_scoring_accepts_option_text(self) -> None:
        result = score_prediction(
            {
                "question_id": "q3",
                "answer": "B",
                "eval_type": "choice_exact",
                "options": {"A": "coffee", "B": "unknown", "C": "tea"},
            },
            "unknown",
        )
        self.assertTrue(result["correct"])
        self.assertEqual(result["prediction_normalized"], "B")

    def test_unsupported_evaluator_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            score_prediction(
                {"question_id": "q4", "answer": "x", "eval_type": "manual"},
                "x",
            )

    def test_missing_expected_answer_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            score_prediction(
                {"question_id": "q5", "answer": "", "eval_type": "exact_match"},
                "prediction",
            )


if __name__ == "__main__":
    unittest.main()
