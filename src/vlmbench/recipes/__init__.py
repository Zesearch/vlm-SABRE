"""Task-specific inputs consumed by the shared benchmark pipeline."""

from .question_builder import build_questions
from .schema import (
    BenchmarkDesign,
    QuestionSpec,
    ReviewTarget,
    TaskAttribute,
)

__all__ = [
    "BenchmarkDesign",
    "QuestionSpec",
    "ReviewTarget",
    "TaskAttribute",
    "build_questions",
]

