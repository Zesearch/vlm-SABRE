"""Strict derived-schema example shown to the Task Design Agent as few-shot.

The actual base reference is ``vlmbench.recipes.schema``. This example demonstrates
how to derive a concrete task schema from that stable reference without sending the
base model's dynamic question map to OpenAI.
"""

from typing import Literal

from pydantic import Field

from vlmbench.recipes.schema import BenchmarkDesign, ReviewTarget, StrictModel, TaskAttribute


class ContextYesNoQuestion(StrictModel):
    probe_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
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
    questions: list[ContextYesNoQuestion] = Field(min_length=4, max_length=4)
    review_targets: list[ReviewTarget]
    generation_notes: list[str]
