"""Default structured-output schema for natural-language task designs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GenerationMode = Literal["paired_edit", "single_image"]
QuestionType = Literal["yes_no", "multiple_choice", "open_generation"]
EvalType = Literal[
    "yes_no_exact",
    "choice_exact",
    "count_exact",
    "exact_match",
    "contains",
]
DesignImageRole = Literal["base", "edited", "single"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskAttribute(StrictModel):
    """Task-specific structured value such as source entity or target texture."""

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class ReviewTarget(StrictModel):
    """Human-review guidance for a semantically important image region."""

    label: str = Field(min_length=1)
    image_role: DesignImageRole
    description: str = Field(min_length=1)


class QuestionSpec(StrictModel):
    """One question to construct after visual assets are generated."""

    probe_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    image_role: DesignImageRole
    question_type: QuestionType
    prompt: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    eval_type: EvalType
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("answer")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        return value.strip()

    @field_validator("options")
    @classmethod
    def normalize_options(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, option in value.items():
            normalized_key = str(key).strip().upper()
            normalized_value = str(option).strip()
            if not normalized_key or not normalized_value:
                raise ValueError("Multiple-choice option keys and values cannot be empty.")
            if normalized_key in normalized:
                raise ValueError(f"Duplicate multiple-choice option: {normalized_key}")
            normalized[normalized_key] = normalized_value
        return normalized

    @model_validator(mode="after")
    def validate_question_contract(self) -> QuestionSpec:
        if self.question_type == "yes_no":
            self.answer = self.answer.lower()
            if self.answer not in {"yes", "no"}:
                raise ValueError("A yes/no answer must be 'yes' or 'no'.")
            if self.eval_type != "yes_no_exact":
                raise ValueError("A yes/no question must use yes_no_exact.")
            if self.options:
                raise ValueError("A yes/no question cannot define options.")
        elif self.question_type == "multiple_choice":
            if self.eval_type != "choice_exact":
                raise ValueError("A multiple-choice question must use choice_exact.")
            if len(self.options) < 2:
                raise ValueError("A multiple-choice question needs at least two options.")
            self.answer = self.answer.upper()
            if self.answer not in self.options:
                raise ValueError("The multiple-choice answer must be an option key.")
        else:
            if self.eval_type not in {"count_exact", "exact_match", "contains"}:
                raise ValueError(
                    "An open-generation question must use count_exact, exact_match, or contains."
                )
            if self.options:
                raise ValueError("An open-generation question cannot define options.")
        return self


class BenchmarkDesign(StrictModel):
    """Provider-neutral design generated from one natural-language task file."""

    concept_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    pressure_test_type: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    generation_mode: GenerationMode
    base_prompt: str = ""
    edit_prompt: str = ""
    image_prompt: str = ""
    task_attributes: list[TaskAttribute] = Field(default_factory=list)
    questions: list[QuestionSpec] = Field(min_length=1)
    review_targets: list[ReviewTarget] = Field(default_factory=list)
    generation_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_generation_contract(self) -> BenchmarkDesign:
        if self.generation_mode == "paired_edit":
            if not self.base_prompt.strip() or not self.edit_prompt.strip():
                raise ValueError("paired_edit requires base_prompt and edit_prompt.")
            if self.image_prompt.strip():
                raise ValueError("paired_edit cannot define image_prompt.")
            invalid_roles = {
                question.image_role
                for question in self.questions
                if question.image_role not in {"base", "edited"}
            }
            invalid_roles.update(
                target.image_role
                for target in self.review_targets
                if target.image_role not in {"base", "edited"}
            )
        else:
            if not self.image_prompt.strip():
                raise ValueError("single_image requires image_prompt.")
            if self.base_prompt.strip() or self.edit_prompt.strip():
                raise ValueError("single_image cannot define base_prompt or edit_prompt.")
            invalid_roles = {
                question.image_role
                for question in self.questions
                if question.image_role != "single"
            }
            invalid_roles.update(
                target.image_role
                for target in self.review_targets
                if target.image_role != "single"
            )
        if invalid_roles:
            raise ValueError(
                f"Image roles do not match generation_mode: {sorted(invalid_roles)}"
            )
        probe_ids = [question.probe_id for question in self.questions]
        if len(probe_ids) != len(set(probe_ids)):
            raise ValueError("Question probe_id values must be unique within a design.")
        attribute_names = [attribute.name for attribute in self.task_attributes]
        if len(attribute_names) != len(set(attribute_names)):
            raise ValueError("Task attribute names must be unique within a design.")
        return self

