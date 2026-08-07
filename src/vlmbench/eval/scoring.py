"""Deterministic scoring helpers shared by screening and evaluation."""

from __future__ import annotations

import re
from typing import Any


NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_yes_no(value: Any) -> str:
    text = normalize_text(value)
    match = re.search(r"\b(yes|no)\b", text)
    return match.group(1) if match else text


def normalize_count(value: Any) -> str:
    text = normalize_text(value).replace("×", "x")
    numbers = re.findall(r"\b\d+\b", text)
    if numbers:
        return numbers[0]
    for word, digit in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return digit
    return text


def normalize_choice(value: Any, options: dict[str, Any] | None = None) -> str:
    text = normalize_text(value)
    letter = re.search(r"(?:^|\b)([a-z])(?:\b|$)", text)
    if letter:
        key = letter.group(1).upper()
        if not options or key in options:
            return key
    for key, option in (options or {}).items():
        if normalize_text(option) == text:
            return str(key).upper()
    return text.upper()


def infer_eval_type(question: dict[str, Any]) -> str:
    eval_type = str(question.get("eval_type") or "").strip()
    if eval_type:
        return eval_type
    question_type = str(question.get("question_type") or "").strip()
    return {
        "yes_no": "yes_no_exact",
        "count": "count_exact",
        "multiple_choice": "choice_exact",
        "short_answer": "exact_match",
    }.get(question_type, "exact_match")


def score_prediction(question: dict[str, Any], raw_prediction: Any) -> dict[str, Any]:
    """Score one prediction and return an auditable normalized trace."""

    eval_type = infer_eval_type(question)
    expected = question.get("answer", "")
    options = question.get("options") or {}
    if expected is None or not str(expected).strip():
        raise ValueError(
            f"Question {question.get('question_id', '')} has no expected answer."
        )

    if eval_type == "yes_no_exact":
        prediction_normalized = normalize_yes_no(raw_prediction)
        expected_normalized = normalize_yes_no(expected)
        correct = prediction_normalized == expected_normalized
    elif eval_type == "count_exact":
        prediction_normalized = normalize_count(raw_prediction)
        expected_normalized = normalize_count(expected)
        correct = prediction_normalized == expected_normalized
    elif eval_type in {"choice_exact", "multiple_choice_exact"}:
        prediction_normalized = normalize_choice(raw_prediction, options)
        expected_normalized = normalize_choice(expected, options)
        correct = prediction_normalized == expected_normalized
    elif eval_type == "contains":
        prediction_normalized = normalize_text(raw_prediction)
        expected_normalized = normalize_text(expected)
        correct = bool(expected_normalized) and expected_normalized in prediction_normalized
    elif eval_type in {"exact", "exact_match", "short_answer_exact"}:
        prediction_normalized = normalize_text(raw_prediction)
        expected_normalized = normalize_text(expected)
        correct = prediction_normalized == expected_normalized
    else:
        raise ValueError(f"Unsupported eval_type for automated screening: {eval_type}")

    return {
        "question_id": str(question.get("question_id") or question.get("id") or ""),
        "eval_type": eval_type,
        "raw_prediction": str(raw_prediction or "").strip(),
        "prediction_normalized": prediction_normalized,
        "expected": expected,
        "expected_normalized": expected_normalized,
        "correct": correct,
    }
