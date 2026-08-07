"""OpenAI structured-output adapter for task-design generation."""

from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel

from vlmbench.pipeline.schema_validation import validate_openai_strict_model


def clean_api_key(raw: str) -> str:
    key = raw.strip()
    if "=" in key:
        key = key.split("=", 1)[1].strip()
    if (key.startswith('"') and key.endswith('"')) or (
        key.startswith("'") and key.endswith("'")
    ):
        key = key[1:-1].strip()
    return key


def resolve_openai_key(task_path: Path, explicit: Path | None = None) -> str:
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        key = clean_api_key(explicit.read_text(encoding="utf-8"))
        if not key:
            raise EnvironmentError(f"OpenAI API-key file is empty: {explicit}")
        return key

    key = clean_api_key(os.environ.get("OPENAI_API_KEY", ""))
    if key:
        return key

    candidates = (
        parent / "openai_api_key.txt" for parent in [task_path.parent, *task_path.parents]
    )
    for path in candidates:
        if path.exists():
            key = clean_api_key(path.read_text(encoding="utf-8"))
            if key:
                return key
    raise EnvironmentError("Set OPENAI_API_KEY or pass --api-key-file.")


class OpenAIDesignClient:
    provider = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str,
        reasoning_effort: str = "high",
        max_output_tokens: int = 24_000,
    ) -> None:
        if not model.strip():
            raise ValueError("OpenAI model is required.")
        self.model = model.strip()
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("OpenAI base URL is required.")
        self.reasoning_effort = reasoning_effort.strip()
        self.max_output_tokens = max_output_tokens
        self.client = OpenAI(api_key=api_key, base_url=self.base_url)

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        validate_openai_strict_model(schema)
        kwargs = {
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "text_format": schema,
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        response = self.client.responses.parse(**kwargs)
        if response.output_parsed is None:
            raise RuntimeError("OpenAI returned no parsed design.")
        return response.output_parsed
