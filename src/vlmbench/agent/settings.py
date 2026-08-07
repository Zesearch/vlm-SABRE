"""Visible, repository-level settings for task-design API calls."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAISettings(SettingsModel):
    base_url: str = Field(min_length=1)
    agent_model: str = Field(min_length=1)
    design_model: str = Field(min_length=1)


class AgentSettings(SettingsModel):
    reasoning_effort: str = Field(min_length=1)
    max_clarification_rounds: int = Field(ge=0)


class DesignGenerationSettings(SettingsModel):
    attempts_per_design: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)


class ProjectSettings(SettingsModel):
    openai: OpenAISettings
    agent: AgentSettings
    design_generation: DesignGenerationSettings


def load_project_settings(path: Path) -> ProjectSettings:
    if not path.exists():
        raise FileNotFoundError(
            f"Settings file not found: {path}. Run from the repository root or pass --settings."
        )
    return ProjectSettings.model_validate_json(path.read_text(encoding="utf-8"))
