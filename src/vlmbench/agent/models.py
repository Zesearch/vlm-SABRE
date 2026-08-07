"""Structured contract returned by the Codex task-design agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


QuestionFormat = Literal["yes_no", "multiple_choice", "open_generation"]
EvaluationMode = Literal["deterministic", "llm_judge", "manual"]
CompilationStatus = Literal["ready", "needs_clarification"]

DETERMINISTIC_EVALUATORS = {
    "yes_no_exact",
    "choice_exact",
    "count_exact",
    "exact_match",
    "contains",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationProtocol(StrictModel):
    """How one family of generated questions will be scored."""

    question_format: QuestionFormat
    evaluation_mode: EvaluationMode
    eval_type: str = Field(min_length=1)
    answer_contract: str = Field(min_length=1)
    filter_eligible: bool
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_filter_contract(self) -> EvaluationProtocol:
        if self.evaluation_mode == "deterministic":
            if self.eval_type not in DETERMINISTIC_EVALUATORS:
                raise ValueError(
                    f"Unsupported deterministic evaluator: {self.eval_type}"
                )
            if not self.filter_eligible:
                raise ValueError(
                    "A supported deterministic evaluator must be filter eligible."
                )
        elif self.filter_eligible:
            raise ValueError(
                "LLM-judge and manual evaluation are not filter eligible in the initial release."
            )
        return self


class TaskDesignCompilation(StrictModel):
    """Auditable result of compiling a user's lightweight task design."""

    status: CompilationStatus
    task_name: str = Field(min_length=1)
    # OpenAI strict structured outputs require every property to be required.
    # Empty strings/lists represent values that do not apply to this status.
    canonical_design_markdown: str
    selected_template_ids: list[str]
    evaluation_protocols: list[EvaluationProtocol]
    assumptions: list[str]
    warnings: list[str]
    clarification_questions: list[str]
    derived_schema_python: str
    schema_adaptations: list[str]

    @model_validator(mode="after")
    def validate_status_contract(self) -> TaskDesignCompilation:
        if self.status == "ready":
            if not self.canonical_design_markdown.strip():
                raise ValueError("A ready compilation requires canonical_design_markdown.")
            if not self.evaluation_protocols:
                raise ValueError("A ready compilation requires an evaluation protocol.")
            if self.clarification_questions:
                raise ValueError(
                    "A ready compilation cannot contain clarification questions."
                )
            if not self.derived_schema_python.strip():
                raise ValueError("A ready compilation requires a derived task schema.")
            unsupported = [
                protocol.eval_type
                for protocol in self.evaluation_protocols
                if not protocol.filter_eligible
            ]
            if unsupported:
                raise ValueError(
                    "A ready compilation must be compatible with the automated filter gate."
                )
        elif not self.clarification_questions:
            raise ValueError(
                "needs_clarification requires at least one clarification question."
            )
        elif self.derived_schema_python.strip():
            raise ValueError(
                "needs_clarification cannot emit a task schema before decisions are resolved."
            )
        return self
